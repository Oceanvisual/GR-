"""Expert decisions over the analysis (FR-080…FR-083, data-model §8, T050).

`apply_reviews(model, initiatives, events)` rebuilds the effective state from the outputs of S5
and S7 and the append-only review log, in the order the decisions were made (created_at, then
id; an event id counts once). Nothing is stored: every read rebuilds it, and the outputs stay
as the model produced them (FR-081: the original value is kept).

- confirm, edit, reject and reopen move the review status by the diagram of data-model §8;
- edit changes only the fields of the entity's whitelist; an edited value is a fact when the
  expert gave a quote (checked when the decision was saved), an interpretation otherwise;
- reject leaves the element out of the calculations: a rejected process takes its operations,
  decision points and findings with it; an initiative loses its links to rejected operations
  and findings, and one left with no operation (or no finding) is out too;
- a change of a process, an operation or a finding marks the initiatives built on it stale,
  «требует пересмотра»: they are regenerated only on request (T052);
- what follows from the rules is recomputed: autonomy cap, tech guard, legal gate (with the
  expert's gate) and relations (with the expert's overrides).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from gr.analysis.gating import cap_autonomy, compute_gate
from gr.analysis.relations import ExpertRelation, infer_relations
from gr.analysis.tech_guard import ProblemCatalog, evaluate
from gr.domain.enums import (
    Confidence,
    InitiativeRelation,
    KnowledgeLayer,
    LegalGate,
    ReviewStatus,
    SolutionCategory,
)
from gr.domain.initiative import Finding, Initiative, InitiativeSet
from gr.domain.process import DecisionPoint, Operation, Process, ProcessModel
from gr.domain.review import EntityType, ReviewEvent, ReviewState
from gr.domain.run import InvalidTransitionError

# The fields an expert may edit, per entity. Ids, links, citations and computed fields are not
# among them: links change by regeneration (T052), computed fields follow from the rules.
EDITABLE: dict[EntityType, frozenset[str]] = {
    "process": frozenset({"name", "goal", "trigger", "result"}),
    "operation": frozenset(
        {
            "name",
            "action",
            "performer",
            "object",
            "deadline",
            "operation_type",
            "legally_significant",
            "handles_personal_data",
            "is_loop_back",
        }
    ),
    "decision_point": frozenset({"condition"}),
    "finding": frozenset({"pattern", "description"}),
    "initiative": frozenset(
        {
            "title",
            "description",
            "architecture_idea",
            "tech_rationale",
            "solution_category",
            "automation_level",
            "ratings",
            "risks",
            "limitations",
        }
    ),
    "relation": frozenset({"relation"}),
}
# Grounded fields: a new value with an optional citation; the required ones cannot be removed.
GROUNDED: dict[str, frozenset[str]] = {
    "process": frozenset({"name", "goal", "trigger", "result"}),
    "operation": frozenset({"action", "performer"}),
    "decision_point": frozenset({"condition"}),
}
REQUIRED = frozenset({"name", "action", "condition"})
# A change of these makes the initiatives built on them stale (FR-083).
UPSTREAM: frozenset[EntityType] = frozenset({"process", "operation", "finding"})
CHANGING = frozenset({"edit", "reject", "reopen"})


class ReviewError(ValueError):
    """A decision that cannot be applied: a field outside the whitelist, a bad value, a
    transition the diagram forbids (api.md: 400 validation_error)."""


class EffectiveState(BaseModel):
    """The analysis as the expert left it."""

    model_config = ConfigDict(frozen=True)

    model: ProcessModel  # every element with its review status and edits
    findings: list[Finding]
    initiatives: list[Initiative]  # calculable ones with their links pruned and rules applied
    excluded: dict[str, str]  # initiative id → why it is out of the calculations
    stale_initiative_ids: list[str]

    def calculable(self) -> list[Initiative]:
        return [i for i in self.initiatives if i.id not in self.excluded]

    def calculation_model(self) -> ProcessModel:
        """The model without rejected processes, operations and decision points."""
        processes = []
        for process in self.model.processes:
            if process.review.status == ReviewStatus.REJECTED:
                continue
            operations = [o for o in process.operations if not _rejected(o)]
            kept = {o.id for o in operations}
            processes.append(
                process.model_copy(
                    update={
                        "operations": [
                            o.model_copy(
                                update={"depends_on": [d for d in o.depends_on if d in kept]}
                            )
                            for o in operations
                        ],
                        "decision_points": [
                            dp
                            for dp in process.decision_points
                            if not _rejected(dp) and dp.after_operation_id in kept
                        ],
                    }
                )
            )
        return self.model.model_copy(update={"processes": processes})


def _rejected(item: Operation | DecisionPoint | Process | Finding | Initiative) -> bool:
    return item.review.status == ReviewStatus.REJECTED


def relation_key(left: str, right: str) -> str:
    """`entity_id` of a relation decision: the two initiative ids, sorted, joined by «|»."""
    return "|".join(sorted((left, right)))


def check_event(event: ReviewEvent) -> None:
    """The decision itself is well formed (its target is checked when it is applied)."""
    if event.entity_type not in EDITABLE:
        raise ReviewError(f"решения по «{event.entity_type}» хранятся версиями, не в журнале")
    if event.entity_type == "relation" and event.action != "edit":
        raise ReviewError("связь можно только переопределить (edit)")
    if event.action == "set_gate":
        if event.entity_type != "initiative":
            raise ReviewError("статус допустимости задаётся только инициативе")
        if set(event.patch or {}) != {"status"}:
            raise ReviewError("set_gate: в patch нужен ровно один ключ status")
        try:
            LegalGate((event.patch or {})["status"])
        except ValueError:
            raise ReviewError("set_gate: неизвестный статус допустимости") from None
    if event.action == "edit":
        unknown = set(event.patch or {}) - EDITABLE[event.entity_type]
        if unknown:
            raise ReviewError(f"поля нельзя исправить: {', '.join(sorted(unknown))}")
    if event.entity_type == "relation":
        kind = (event.patch or {}).get("relation")
        if kind not in {r.value for r in InitiativeRelation}:
            raise ReviewError("relation: alternative или complementary")
        if len(set(event.entity_id.split("|"))) != 2:
            raise ReviewError("связь задаётся двумя разными инициативами «a|b»")


def _grounded(field: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        if field in REQUIRED:
            raise ReviewError(f"{field}: значение нельзя удалить")
        return None
    if isinstance(value, str):
        value = {"value": value}
    if not isinstance(value, Mapping) or not set(value) <= {"value", "citation"}:
        raise ReviewError(f"{field}: ожидается строка или {{value, citation}}")
    citation = value.get("citation")
    return {
        "value": value.get("value"),
        "citations": [citation] if citation else [],
        "layer": KnowledgeLayer.FACT if citation else KnowledgeLayer.INTERPRETATION,
        "confidence": Confidence.HIGH,
    }


def _deadline(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not set(value) <= {"value", "unit", "anchor", "raw"}:
        raise ReviewError("deadline: ожидается {value, unit, anchor?, raw?}")
    raw = value.get("raw") or f"{value.get('value')} {value.get('unit')}"
    return {
        "raw": _grounded("deadline", raw),
        "value": value.get("value"),
        "unit": value.get("unit"),
        "anchor": value.get("anchor"),
        "parsed_from_quote": False,
    }


def _edited[M: BaseModel](item: M, entity: str, patch: Mapping[str, Any]) -> M:
    data = item.model_dump()
    for field, value in patch.items():
        if field in GROUNDED.get(entity, ()):
            data[field] = _grounded(field, value)
        elif field == "deadline":
            data[field] = _deadline(value)
        elif field == "ratings":
            shape = "ratings: ожидается {критерий: {level|bucket, justification}}"
            if not isinstance(value, Mapping) or not all(
                isinstance(v, Mapping) for v in value.values()
            ):
                raise ReviewError(shape)
            if set(value) - set(data["ratings"]):
                raise ReviewError("ratings: неизвестный критерий")
            data["ratings"] = data["ratings"] | {
                criterion: data["ratings"][criterion] | dict(rating)
                for criterion, rating in value.items()
            }
        elif field == "automation_level":  # the proposal; the cap is applied again below
            data |= {"automation_level_proposed": value, "automation_level": value,
                     "autonomy_capped": False}  # fmt: skip
        else:
            data[field] = value
    try:
        return type(item).model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(sorted(patch))
        raise ReviewError(f"{fields}: недопустимое значение ({exc.errors()[0]['msg']})") from None


def apply_reviews(
    model: ProcessModel,
    initiatives: InitiativeSet,
    events: Sequence[ReviewEvent],
    *,
    catalog: ProblemCatalog | None = None,
) -> EffectiveState:
    ordered = sorted(
        {e.id: e for e in events if e.entity_type in EDITABLE}.values(),
        key=lambda e: (e.created_at, e.id),
    )
    processes = {p.id: p for p in model.processes}
    operations = {o.id: o for p in model.processes for o in p.operations}
    points = {d.id: d for p in model.processes for d in p.decision_points}
    findings = {f.id: f for f in initiatives.findings}
    proposals = {i.id: i for i in initiatives.initiatives}
    stores: dict[str, dict[str, Any]] = {
        "process": processes,
        "operation": operations,
        "decision_point": points,
        "finding": findings,
        "initiative": proposals,
    }
    expert_gate: dict[str, LegalGate] = {}
    overrides: dict[str, InitiativeRelation] = {}
    touched: dict[str, set[str]] = {"process": set(), "operation": set(), "finding": set()}
    for event in ordered:
        check_event(event)
        if event.entity_type == "relation":
            overrides[event.entity_id] = InitiativeRelation((event.patch or {})["relation"])
            continue
        store = stores[event.entity_type]
        item = store.get(event.entity_id)
        if item is None:
            continue  # an element of an earlier version (regenerated initiatives, T052)
        try:
            state: ReviewState = item.review.apply(event)
        except InvalidTransitionError as exc:
            raise ReviewError(f"{event.entity_id}: {exc}") from None
        if event.action == "edit":
            item = _edited(item, event.entity_type, event.patch or {})
        if event.action == "set_gate":
            expert_gate[event.entity_id] = LegalGate((event.patch or {})["status"])
        store[event.entity_id] = item.model_copy(update={"review": state})
        if event.entity_type in UPSTREAM and event.action in CHANGING:
            touched[event.entity_type].add(event.entity_id)

    effective_model = model.model_copy(
        update={
            "processes": [
                processes[p.id].model_copy(
                    update={
                        "operations": [operations[o.id] for o in p.operations],
                        "decision_points": [points[d.id] for d in p.decision_points],
                    }
                )
                for p in model.processes
            ]
        }
    )
    dead_processes = {k for k, p in processes.items() if _rejected(p)}
    process_of = {o.id: p.id for p in model.processes for o in p.operations}
    dead_operations = {
        k for k, o in operations.items() if _rejected(o) or process_of[k] in dead_processes
    }
    alive_findings = {
        k: f for k, f in findings.items() if not _rejected(f) and f.process_id not in dead_processes
    }
    stale = {
        i.id
        for i in proposals.values()
        if i.process_id in touched["process"]
        or set(i.operation_ids) & touched["operation"]
        or set(i.finding_ids) & touched["finding"]
    }
    excluded: dict[str, str] = {}
    effective: dict[str, Initiative] = {}
    for key, item in proposals.items():
        if _rejected(item):
            excluded[key] = "Отклонена экспертом."
            effective[key] = item
            continue
        ops = [o for o in item.operation_ids if o not in dead_operations]
        linked = [f for f in item.finding_ids if f in alive_findings]
        needs_finding = item.solution_category != SolutionCategory.NO_CHANGE
        if not ops or (needs_finding and not linked):
            excluded[key] = (
                "Все операции инициативы отклонены экспертом."
                if not ops
                else "Все проблемы инициативы отклонены экспертом."
            )
            effective[key] = item.model_copy(update={"stale": True, "relations": []})
            continue
        pruned = item.model_copy(
            update={
                "operation_ids": ops,
                "finding_ids": linked,
                "reduction_potential": [
                    r for r in item.reduction_potential if r.operation_id in ops
                ],
                "stale": item.stale or key in stale,
            }
        )
        acted_on = [operations[o] for o in ops]
        pruned = cap_autonomy(pruned, acted_on)
        guard = evaluate(
            pruned, list(alive_findings.values()), operations=acted_on, catalog=catalog
        )
        pruned = pruned.model_copy(update={"tech_guard": guard})
        gate = compute_gate(pruned, acted_on, expert_gate=expert_gate.get(key))
        effective[key] = pruned.model_copy(update={"gate": gate})
    calculable = [effective[k] for k in proposals if k not in excluded]
    keys = {i.id for i in calculable}
    experts: list[ExpertRelation] = []
    for pair, kind in sorted(overrides.items()):
        left, right = pair.split("|")
        if {left, right} <= keys:
            experts.append(ExpertRelation(left_id=left, right_id=right, relation=kind))
    for related in infer_relations(calculable, overrides=experts):
        effective[related.id] = related
    return EffectiveState(
        model=effective_model,
        findings=list(findings.values()),
        initiatives=[effective[k] for k in proposals],
        excluded=excluded,
        stale_initiative_ids=sorted(stale | {k for k, i in effective.items() if i.stale}),
    )
