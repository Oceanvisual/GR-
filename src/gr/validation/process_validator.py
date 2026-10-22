"""S5 validate: LLM answers → the verified process model (llm.md § S4 «Валидация», FR-024…FR-028).

Deterministic, no LLM:
1. every quoted fact is verified (R-11); an element whose main quote fails goes to
   `unverified`, and an operation with an unverified action leaves the model as a whole;
2. a deadline must be read from its quote (value and unit), a performer must be named in it;
3. the operation graph: references exist, cycles only through a loop-back step;
4. duplicate processes and operations are merged;
5. answer keys become stable ids (`proc_1.op_3`) in execution order; confidence is computed;
6. the coverage report lists action fragments that no process uses.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from gr.domain.document import Fragment
from gr.domain.enums import Confidence, DeadlineUnit, KnowledgeLayer
from gr.domain.process import (
    Deadline,
    DecisionOutcome,
    DecisionPoint,
    DocumentRef,
    Grounded,
    IssueCode,
    Operation,
    Process,
    ProcessModel,
    Role,
    UnverifiedElement,
    UnverifiedItem,
    ValidationIssue,
    ValidationReport,
    derive_legally_significant,
)
from gr.extraction.detailing import DetailedProcess, DetailingResult
from gr.extraction.discovery import DiscoveredProcess, DiscoveryResult
from gr.extraction.mapping import Checked, FactMapper
from gr.extraction.schemas import DeadlineOut, DecisionPointOut, DetailOut, Fact, OperationOut
from gr.grounding.deadlines import check_deadline
from gr.grounding.roles import role_in_quote
from gr.validation.confidence import element_confidence, process_confidence
from gr.validation.coverage import coverage_report
from gr.validation.dedupe import dedupe_operations, dedupe_processes
from gr.validation.graph import Node, check_graph

_ERRORS: frozenset[IssueCode] = frozenset({"cycle", "no_operations"})
_MANDATORY = {"yes": True, "no": False, "unknown": None}


@dataclass
class _Report:
    """Issues and unverified claims of one process, plus the fragments its facts cite."""

    process_id: str
    issues: list[ValidationIssue] = field(default_factory=list)
    unverified: list[UnverifiedItem] = field(default_factory=list)
    cited: set[str] = field(default_factory=set)

    def issue(self, code: IssueCode, element_id: str, message: str) -> None:
        severity: Literal["error", "warning"] = "error" if code in _ERRORS else "warning"
        self.issues.append(
            ValidationIssue(code=code, severity=severity, element_id=element_id, message=message)
        )

    def reject(
        self,
        kind: UnverifiedElement,
        element_id: str,
        value: str,
        checked: Checked | None,
        reason: str,
    ) -> None:
        self.unverified.append(
            UnverifiedItem(
                element_type=kind,
                element_id=element_id,
                process_id=self.process_id,
                value=value,
                citation=checked.citation if checked else None,
                reason=reason,
            )
        )


class ProcessValidator:
    def __init__(self, fragments: Sequence[Fragment]) -> None:
        self.fragments = fragments
        self.mapper = FactMapper(fragments)

    # --- facts ---------------------------------------------------------------------------

    def _fact(
        self, fact: Fact, report: _Report, kind: UnverifiedElement, element_id: str
    ) -> tuple[Grounded[str] | None, Checked]:
        checked = self.mapper.check(fact)
        if not checked.passed:
            report.issue("unverified_quote", element_id, checked.reason or "цитата не подтверждена")
            report.reject(kind, element_id, checked.text, checked, checked.reason or "")
            return None, checked
        report.cited.add(checked.citation.fragment_id)
        if checked.relocated:
            target = self.mapper.fragments.get(checked.citation.fragment_id)
            name = target.label if target else checked.citation.fragment_id
            report.issue("relocated_citation", element_id, f"Ссылка исправлена на «{name}».")
        return self.mapper.grounded(checked), checked

    def _first(
        self, facts: list[Fact], report: _Report, kind: UnverifiedElement, element_id: str
    ) -> Grounded[str] | None:
        """An optional field: a list of at most one fact."""
        return self._fact(facts[0], report, kind, element_id)[0] if facts else None

    def _deadline(
        self, out: DeadlineOut, report: _Report, element_id: str
    ) -> tuple[Deadline | None, Checked]:
        raw, checked = self._fact(out.raw, report, "deadline", element_id)
        if raw is None:
            return None, checked
        value = out.value if out.value >= 0 else None
        verdict = check_deadline(value, out.unit, out.raw.quote)
        if not verdict.ok:
            claimed = f"{value if value is not None else ''} {out.unit.value}".strip()
            reason = "срок не найден в цитате"
            report.issue("deadline_not_in_quote", element_id, f"Срок «{claimed}»: {reason}.")
            report.reject("deadline", element_id, f"{raw.value} ({claimed})", checked, reason)
            return None, checked
        parsed = value is not None or out.unit == DeadlineUnit.NEXT_WORKING_DAY
        deadline = Deadline(
            raw=raw,
            value=value,
            unit=out.unit,
            anchor=out.anchor.strip() or None,
            parsed_from_quote=parsed,
        )
        return deadline, checked

    # --- operations ----------------------------------------------------------------------

    def _operations(
        self, detail: DetailOut, report: _Report
    ) -> tuple[list[Operation], dict[str, str]]:
        """Verified operations in execution order and the answer key → operation id map."""
        pid = report.process_id
        detail, merged = dedupe_operations(detail, self.mapper.resolve)
        for duplicate, kept in merged:
            report.issue(
                "duplicate_merged", f"{pid}.{kept}", f"Операция {duplicate} слита с {kept}."
            )

        verified: dict[str, tuple[OperationOut, Grounded[str], Checked]] = {}
        dropped: dict[str, list[str]] = {}
        for op in detail.operations:
            action, checked = self._fact(op.action, report, "operation", f"{pid}.{op.key}")
            if action is None:
                dropped[op.key] = list(op.depends_on)
            else:
                verified[op.key] = (op, action, checked)

        def bridged(dependencies: list[str], seen: frozenset[str] = frozenset()) -> list[str]:
            """A dependency on an unverified operation passes to that operation's own ones."""
            out: list[str] = []
            for key in dependencies:
                if key in dropped and key not in seen:
                    out += bridged(dropped[key], seen | {key})
                elif key not in dropped:
                    out.append(key)
            return out

        nodes = [
            Node(op.key, tuple(bridged(list(op.depends_on))), op.is_loop_back)
            for op, _, _ in verified.values()
        ]
        graph = check_graph(nodes)
        for key, missing in graph.dangling:
            report.issue(
                "dangling_dependency",
                f"{pid}.{key}",
                f"Ссылка на несуществующую операцию {missing}.",
            )
        ids = {key: f"{pid}.op_{n}" for n, key in enumerate(graph.order, 1)}
        for cycle in graph.cycles:
            report.issue(
                "cycle",
                ids[cycle[0]],
                "Цикл без возврата на доработку: " + " → ".join(ids[k] for k in cycle),
            )

        operations = []
        for seq, key in enumerate(graph.order):
            op, action, checked = verified[key]
            operations.append(
                self._operation(op, action, checked, ids, graph.depends_on[key], seq, report)
            )
        return operations, ids

    def _operation(
        self,
        op: OperationOut,
        action: Grounded[str],
        action_checked: Checked,
        ids: dict[str, str],
        depends_on: list[str],
        seq: int,
        report: _Report,
    ) -> Operation:
        op_id = ids[op.key]
        confidences = [action_checked.confidence]
        secondary_failed = False

        performer: Grounded[str] | None = None
        if op.performer:
            fact = op.performer[0]
            performer, checked = self._fact(fact, report, "performer", f"{op_id}.performer")
            if performer is not None and not role_in_quote(fact.text, fact.quote):
                reason = "исполнитель не назван в цитате"
                report.issue("performer_not_in_quote", op_id, f"«{fact.text[:80]}»: {reason}.")
                report.reject("performer", f"{op_id}.performer", fact.text, checked, reason)
                performer = None
            if performer is None:
                secondary_failed = True
            else:
                confidences.append(checked.confidence)

        deadline: Deadline | None = None
        if op.deadline:
            deadline, checked = self._deadline(op.deadline[0], report, f"{op_id}.deadline")
            if deadline is None:
                secondary_failed = True
            else:
                confidences.append(checked.confidence)

        conditions: list[Grounded[str]] = []
        for n, fact in enumerate(op.conditions, 1):
            condition, checked = self._fact(fact, report, "condition", f"{op_id}.condition_{n}")
            if condition is None:
                secondary_failed = True
            else:
                conditions.append(condition)
                confidences.append(checked.confidence)

        return Operation(
            id=op_id,
            seq=seq,
            name=op.name.strip() or action.value,
            action=action,
            object=op.object.strip() or None,
            performer=performer,
            inputs=[self.mapper.interpretation(t) for t in op.inputs if t.strip()],
            outputs=[self.mapper.interpretation(t) for t in op.outputs if t.strip()],
            depends_on=[ids[k] for k in depends_on],
            deadline=deadline,
            conditions=conditions,
            information_systems=[
                self.mapper.interpretation(t) for t in op.information_systems if t.strip()
            ],
            operation_type=op.operation_type,
            legally_significant=derive_legally_significant(
                op.legally_significant, op.operation_type
            ),
            handles_personal_data=op.handles_personal_data,
            is_loop_back=op.is_loop_back,
            confidence=element_confidence(confidences, secondary_failed=secondary_failed),
        )

    def _decision_points(
        self, points: list[DecisionPointOut], ids: dict[str, str], report: _Report
    ) -> list[DecisionPoint]:
        pid = report.process_id
        result: list[DecisionPoint] = []
        for point in points:
            element_id = f"{pid}.{point.key}"
            after = ids.get(point.after_operation)
            if after is None or len(point.outcomes) < 2:
                reason = (
                    "нет операции, после которой принимается решение"
                    if after is None
                    else "меньше двух исходов"
                )
                report.issue("invalid_decision_point", element_id, f"Точка решения: {reason}.")
                report.reject("decision_point", element_id, point.condition.text, None, reason)
                continue
            condition, _ = self._fact(point.condition, report, "decision_point", element_id)
            if condition is None:
                continue
            outcomes = []
            for outcome in point.outcomes:
                target = ids.get(outcome.next_operation) if outcome.next_operation else None
                if outcome.next_operation and target is None:
                    message = f"Исход ведёт к несуществующей операции {outcome.next_operation}."
                    report.issue("dangling_dependency", element_id, message)
                outcomes.append(
                    DecisionOutcome(
                        label=outcome.label.strip() or "—",
                        next_operation_id=target,
                        ends_process=outcome.ends_process,
                    )
                )
            criteria = []
            for n, fact in enumerate(point.criteria, 1):
                criterion, _ = self._fact(fact, report, "criterion", f"{element_id}.criterion_{n}")
                if criterion is not None:
                    criteria.append(criterion)
            result.append(
                DecisionPoint(
                    id=f"{pid}.dp_{len(result) + 1}",
                    after_operation_id=after,
                    condition=condition,
                    outcomes=outcomes,
                    criteria=criteria,
                )
            )
        return result

    # --- processes -----------------------------------------------------------------------

    def _process(
        self, pid: str, detail: DetailOut, found: DiscoveredProcess
    ) -> tuple[Process, _Report]:
        report = _Report(pid)
        head = detail.process
        name, name_checked = self._fact(head.name, report, "process_name", f"{pid}.name")
        if name is None:  # the process exists (S3 evidence), its wording is not a fact
            name = self.mapper.interpretation(found.name or head.name.text)
        result = self._first(head.result, report, "result", f"{pid}.result")
        if result is None and found.result:
            result = self.mapper.interpretation(found.result)
        if result is None:
            report.issue("missing_result", pid, "У процесса не указан результат.")

        participants: list[Role] = []
        for n, fact in enumerate(head.participants, 1):
            role, _ = self._fact(fact, report, "participant", f"{pid}.role_{n}")
            if role is not None:
                participants.append(Role(id=f"{pid}.role_{len(participants) + 1}", name=role))
        inputs: list[DocumentRef] = []
        for n, document in enumerate(head.inputs, 1):
            ref, _ = self._fact(document.name, report, "input", f"{pid}.input_{n}")
            if ref is not None:
                inputs.append(DocumentRef(name=ref, mandatory=_MANDATORY[document.mandatory]))
        systems = [
            system
            for n, fact in enumerate(head.information_systems, 1)
            if (system := self._fact(fact, report, "information_system", f"{pid}.is_{n}")[0])
        ]
        deadlines = [
            deadline
            for n, out in enumerate(head.deadlines, 1)
            if (deadline := self._deadline(out, report, f"{pid}.deadline_{n}")[0])
        ]

        operations, ids = self._operations(detail, report)
        if not operations:
            report.issue("no_operations", pid, "У процесса нет подтверждённых операций.")
        points = self._decision_points(detail.decision_points, ids, report)
        process = Process(
            id=pid,
            name=name,
            goal=self._first(head.goal, report, "goal", f"{pid}.goal"),
            trigger=self._first(head.trigger, report, "trigger", f"{pid}.trigger"),
            participants=participants,
            inputs=inputs,
            operations=operations,
            decision_points=points,
            deadlines=deadlines,
            information_systems=systems,
            result=result,
            validation=ValidationReport(issues=report.issues),
            confidence=process_confidence(
                name_checked.confidence if name_checked.passed else Confidence.LOW,
                [op.confidence for op in operations],
            ),
        )
        return process, report

    def validate(self, discovery: DiscoveryResult, detailing: DetailingResult) -> ProcessModel:
        found = {p.key: p for p in discovery.processes}
        names = {key: p.name for key, p in found.items()}
        order = {p.key: n for n, p in enumerate(discovery.processes)}
        detailed = sorted(detailing.processes, key=lambda p: order.get(p.process_key, len(order)))
        detailed, merged = dedupe_processes(detailed, names)
        merged_into: dict[str, list[str]] = {}
        for duplicate, kept in merged:
            merged_into.setdefault(kept, []).append(duplicate)

        unverified: list[UnverifiedItem] = []
        # A process with neither a verified name nor a verified operation is not in the act
        # (an obedient answer to an injected instruction, say): it goes to «Не подтверждено».
        survivors: list[DetailedProcess] = []
        for item in detailed:
            trial, _ = self._process(item.process_key, item.detail, found[item.process_key])
            if trial.operations or trial.name.layer == KnowledgeLayer.FACT:
                survivors.append(item)
                continue
            unverified.append(
                UnverifiedItem(
                    element_type="process_name",
                    element_id=item.process_key,
                    process_id=item.process_key,
                    value=trial.name.value,
                    citation=found[item.process_key].evidence,
                    reason="ни название, ни одна операция процесса не подтверждены текстом акта",
                )
            )

        pids = {d.process_key: f"proc_{n}" for n, d in enumerate(survivors, 1)}
        processes: list[Process] = []
        cited: set[str] = set()
        used: list[str] = []
        for item in survivors:
            candidate = found[item.process_key]
            process, report = self._process(pids[item.process_key], item.detail, candidate)
            for duplicate in merged_into.get(item.process_key, []):
                message = f"Процесс «{names[duplicate][:80]}» слит с этим."
                report.issue("duplicate_merged", process.id, message)
                used += [
                    *found[duplicate].core_fragment_ids,
                    *found[duplicate].related_fragment_ids,
                ]
            parent = pids.get(candidate.parent_key or "")
            processes.append(
                process.model_copy(
                    update={
                        "validation": ValidationReport(issues=report.issues),
                        "related_process_ids": [parent] if parent else [],
                    }
                )
            )
            unverified += report.unverified
            cited |= report.cited
            used += [*candidate.core_fragment_ids, *candidate.related_fragment_ids]

        return ProcessModel(
            processes=processes,
            unverified=unverified,
            coverage=coverage_report(
                self.fragments, used, cited, discovery.non_process_fragment_ids
            ),
        )


def validate(
    discovery: DiscoveryResult, detailing: DetailingResult, fragments: Sequence[Fragment]
) -> ProcessModel:
    return ProcessValidator(fragments).validate(discovery, detailing)
