"""Expert decisions over the analysis (FR-080…FR-083, data-model §8, T050)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from initiative_builders import initiative

from gr.analysis.overlay import (
    EDITABLE,
    EffectiveState,
    ReviewError,
    apply_reviews,
    check_event,
    relation_key,
)
from gr.domain.document import Citation, GroundingResult
from gr.domain.enums import (
    AutomationLevel,
    DeadlineUnit,
    GroundingStatus,
    InitiativeRelation,
    KnowledgeLayer,
    LegalGate,
    OperationType,
    ProblemPattern,
    ReviewStatus,
    SolutionCategory,
)
from gr.domain.initiative import Finding, Initiative, InitiativeSet
from gr.domain.process import (
    CoverageReport,
    DecisionOutcome,
    DecisionPoint,
    Grounded,
    Operation,
    Process,
    ProcessModel,
)
from gr.domain.review import EntityType, ReviewAction, ReviewEvent

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def op(process: str, n: int, kind: OperationType) -> Operation:
    return Operation(id=f"{process}.op_{n}", seq=n, name=f"Операция {n}", action=text("действие"),
                     operation_type=kind)  # fmt: skip


def model() -> ProcessModel:
    decision = DecisionPoint(
        id="proc_1.dp_1",
        after_operation_id="proc_1.op_3",
        condition=text("есть основания для отказа"),
        outcomes=[DecisionOutcome(label="да", ends_process=True),
                  DecisionOutcome(label="нет", ends_process=True)],
    )  # fmt: skip
    return ProcessModel(
        processes=[
            Process(
                id="proc_1",
                name=text("Выдача пропуска"),
                operations=[
                    op("proc_1", 1, OperationType.REGISTRATION),
                    op("proc_1", 2, OperationType.COMPLETENESS_CHECK),
                    op("proc_1", 3, OperationType.DECISION),
                ],
                decision_points=[decision],
            ),
            Process(
                id="proc_2",
                name=text("Учёт"),
                operations=[op("proc_2", 1, OperationType.RECORD_KEEPING)],
            ),
        ],
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )


def finding(key: str, process: str, *ops: str) -> Finding:
    return Finding(id=key, process_id=process, operation_ids=list(ops),
                   pattern=ProblemPattern.DUPLICATE_CHECKS, description="x")  # fmt: skip


def proposal(
    key: str, process: str, ops: list[str], findings: list[str], **updates: Any
) -> Initiative:
    return initiative(
        SolutionCategory.CLASSIC_AUTOMATION,
        id=key,
        process_id=process,
        operation_ids=ops,
        finding_ids=findings,
        reduction_potential=[
            {"operation_id": o, "level": "medium", "justification": "x"} for o in ops
        ],
        **updates,
    )


def outputs() -> InitiativeSet:
    return InitiativeSet(
        findings=[
            finding("f1", "proc_1", "proc_1.op_2"),
            finding("f2", "proc_1", "proc_1.op_3"),
            finding("f3", "proc_2", "proc_2.op_1"),
        ],
        initiatives=[
            proposal("ini_a", "proc_1", ["proc_1.op_2"], ["f1"],
                     automation_level="autonomous", automation_level_proposed="autonomous"),
            proposal("ini_b", "proc_1", ["proc_1.op_2", "proc_1.op_3"], ["f1", "f2"]),
            proposal("ini_c", "proc_2", ["proc_2.op_1"], ["f3"]),
        ],
    )  # fmt: skip


def event(
    n: int,
    entity_type: EntityType,
    entity_id: str,
    action: ReviewAction,
    patch: dict[str, Any] | None = None,
    comment: str | None = None,
) -> ReviewEvent:
    return ReviewEvent(id=f"rev_{n:03d}", run_id="run_1", entity_type=entity_type,
                       entity_id=entity_id, action=action, patch=patch, comment=comment,
                       user="expert", created_at=T0 + timedelta(minutes=n))  # fmt: skip


def state(*events: ReviewEvent) -> EffectiveState:
    return apply_reviews(model(), outputs(), list(events))


def by_id(found: EffectiveState, key: str) -> Initiative:
    return next(i for i in found.initiatives if i.id == key)


def operation(found: EffectiveState, key: str) -> Operation:
    return next(o for p in found.model.processes for o in p.operations if o.id == key)


# --- no decisions, order, idempotence -------------------------------------------------------


def test_without_decisions_the_outputs_come_back_with_the_rules_applied() -> None:
    found = state()
    assert found.model == model()
    assert found.excluded == {} and found.stale_initiative_ids == []
    assert [i.id for i in found.calculable()] == ["ini_a", "ini_b", "ini_c"]
    assert all(i.gate is not None and i.tech_guard is not None for i in found.initiatives)
    # ini_a and ini_b share an operation and a finding: alternatives (FR-048).
    assert {r.relation for r in by_id(found, "ini_a").relations} == {InitiativeRelation.ALTERNATIVE}


def test_decisions_apply_in_the_order_they_were_made_and_once() -> None:
    first = event(1, "process", "proc_1", "edit", {"name": "Пропуск"})
    second = event(2, "process", "proc_1", "edit", {"name": "Выдача разового пропуска"})
    forward = state(first, second)
    assert forward == state(second, first) == state(first, second, second)
    assert forward.model.processes[0].name.value == "Выдача разового пропуска"
    assert forward.model.processes[0].review.last_event_id == "rev_002"


# --- edits ----------------------------------------------------------------------------------


def test_an_edit_is_a_fact_only_with_a_quote() -> None:
    plain = state(event(1, "process", "proc_1", "edit", {"name": "Пропуск"}))
    process = plain.model.processes[0]
    assert process.name.layer == KnowledgeLayer.INTERPRETATION and process.name.citations == []
    assert process.review.status == ReviewStatus.EDITED
    assert process.review.edited_fields == ["name"]
    citation = Citation(
        fragment_id="body/3.2",
        quote="Выдача пропуска",
        grounding=GroundingResult(status=GroundingStatus.VERIFIED, score=100, matched_start=0,
                                  matched_end=15),
    )  # fmt: skip
    quoted = state(
        event(
            1,
            "process",
            "proc_1",
            "edit",
            {"name": {"value": "Выдача пропуска", "citation": citation.model_dump()}},
        )
    )
    name = quoted.model.processes[0].name  # fmt: skip
    assert name.layer == KnowledgeLayer.FACT and name.citations == [citation]


def test_a_corrected_deadline_is_marked_and_dependent_initiatives_go_stale() -> None:
    # Q8: «исправить срок операции» → «исправлено», dependent initiatives «требуют пересмотра».
    patch = {"deadline": {"value": 5, "unit": "working_days", "anchor": "со дня регистрации"}}
    found = state(event(1, "operation", "proc_1.op_2", "edit", patch))
    corrected = operation(found, "proc_1.op_2")
    assert corrected.deadline is not None
    assert (corrected.deadline.value, corrected.deadline.unit) == (5, DeadlineUnit.WORKING_DAYS)
    assert corrected.review.status == ReviewStatus.EDITED
    assert corrected.review.edited_fields == ["deadline"]
    assert found.stale_initiative_ids == ["ini_a", "ini_b"]
    assert by_id(found, "ini_a").stale and not by_id(found, "ini_c").stale


def test_a_confirmation_changes_nothing_downstream() -> None:
    found = state(event(1, "operation", "proc_1.op_2", "confirm"))
    assert operation(found, "proc_1.op_2").review.status == ReviewStatus.CONFIRMED
    assert found.stale_initiative_ids == []


# --- rejection ------------------------------------------------------------------------------


def test_a_rejected_initiative_leaves_the_calculations_but_stays_visible() -> None:
    found = state(event(1, "initiative", "ini_b", "reject", comment="дублирует ini_a"))
    assert found.excluded == {"ini_b": "Отклонена экспертом."}
    assert [i.id for i in found.calculable()] == ["ini_a", "ini_c"]
    assert by_id(found, "ini_b").review.status == ReviewStatus.REJECTED
    assert by_id(found, "ini_a").relations == []  # no alternative left


def test_a_rejected_operation_is_dropped_from_initiatives() -> None:
    found = state(event(1, "operation", "proc_1.op_3", "reject", comment="не операция"))
    trimmed = by_id(found, "ini_b")
    assert trimmed.operation_ids == ["proc_1.op_2"]
    assert [r.operation_id for r in trimmed.reduction_potential] == ["proc_1.op_2"]
    assert trimmed.stale and "ini_b" not in found.excluded
    both = state(
        event(1, "operation", "proc_1.op_3", "reject", comment="не операция"),
        event(2, "operation", "proc_1.op_2", "reject", comment="не операция"),
    )
    assert both.excluded == {
        "ini_a": "Все операции инициативы отклонены экспертом.",
        "ini_b": "Все операции инициативы отклонены экспертом.",
    }
    assert [o.id for o in both.calculation_model().processes[0].operations] == ["proc_1.op_1"]
    assert both.calculation_model().processes[0].decision_points == []  # it followed op_3


def test_a_rejected_process_takes_its_operations_findings_and_initiatives() -> None:
    found = state(event(1, "process", "proc_2", "reject", comment="не процесс акта"))
    assert found.excluded == {"ini_c": "Все операции инициативы отклонены экспертом."}
    assert [p.id for p in found.calculation_model().processes] == ["proc_1"]


def test_a_rejected_finding_unlinks_its_initiatives() -> None:
    found = state(event(1, "finding", "f1", "reject", comment="проблемы нет"))
    assert found.excluded == {"ini_a": "Все проблемы инициативы отклонены экспертом."}
    assert by_id(found, "ini_b").finding_ids == ["f2"] and by_id(found, "ini_b").stale


# --- rules recomputed -----------------------------------------------------------------------


def test_rules_follow_the_effective_model_and_the_experts_gate() -> None:
    significant = state(
        event(1, "operation", "proc_1.op_2", "edit", {"legally_significant": True}),
        event(2, "initiative", "ini_c", "set_gate", {"status": "blocked"}),
    )
    capped = by_id(significant, "ini_a")
    assert capped.automation_level == AutomationLevel.HUMAN_APPROVAL and capped.autonomy_capped
    assert capped.gate is not None and capped.gate.status == LegalGate.ALLOWED_WITH_HITL
    blocked = by_id(significant, "ini_c")
    assert blocked.gate is not None and blocked.gate.status == LegalGate.BLOCKED
    assert "Эксперт запретил инициативу." in blocked.gate.reasons
    assert blocked.review.status == ReviewStatus.PENDING  # a gate is not a review status
    lifted = apply_reviews(
        model(),
        outputs(),
        [event(2, "initiative", "ini_c", "set_gate", {"status": "blocked"}),
         event(3, "initiative", "ini_c", "set_gate", {"status": "allowed"})],
    )  # fmt: skip
    lifted_gate = by_id(lifted, "ini_c").gate
    assert lifted_gate is not None and lifted_gate.status == LegalGate.ALLOWED


def test_the_expert_overrides_a_relation() -> None:
    pair = relation_key("ini_b", "ini_a")
    assert pair == "ini_a|ini_b"
    found = state(event(1, "relation", pair, "edit", {"relation": "complementary"}))
    (relation,) = by_id(found, "ini_a").relations
    assert (relation.initiative_id, relation.relation, relation.source) == (
        "ini_b",
        InitiativeRelation.COMPLEMENTARY,
        "expert",
    )


# --- what is refused ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (event(1, "operation", "proc_1.op_1", "edit", {"id": "x"}), "нельзя исправить"),
        (event(1, "initiative", "ini_a", "edit", {"tech_guard": {}}), "нельзя исправить"),
        (event(1, "process", "proc_1", "set_gate", {"status": "blocked"}), "только инициативе"),
        (event(1, "initiative", "ini_a", "set_gate", {"status": "maybe"}), "неизвестный статус"),
        (event(1, "process", "proc_1", "edit", {"name": None}), "нельзя удалить"),
        (event(1, "operation", "proc_1.op_1", "edit", {"deadline": {"value": 3, "unit": "eons"}}),
         "недопустимое значение"),
        (event(1, "initiative", "ini_a", "edit", {"ratings": {"beauty": {"level": "high"}}}),
         "неизвестный критерий"),
        (event(1, "initiative", "ini_a", "edit", {"ratings": {"feasibility": "high"}}),
         "ожидается"),
        (event(1, "relation", "ini_a|ini_b", "edit", {"relation": "rival"}), "complementary"),
        (event(1, "relation", "ini_a|ini_a", "edit", {"relation": "alternative"}), "двумя"),
        (event(1, "assumption_set", "assumptions", "edit", {"x": 1}), "версиями"),
    ],
)  # fmt: skip
def test_edits_outside_the_whitelist_or_with_bad_values_are_refused(
    given: ReviewEvent, message: str
) -> None:
    with pytest.raises(ReviewError, match=message):
        check_event(given) if given.entity_type == "assumption_set" else state(given)


def test_transitions_follow_the_diagram() -> None:
    reject = event(1, "operation", "proc_1.op_1", "reject", comment="ошибка")
    with pytest.raises(ReviewError):
        state(reject, event(2, "operation", "proc_1.op_1", "edit", {"name": "x"}))
    found = state(reject, event(2, "operation", "proc_1.op_1", "reopen"),
                  event(3, "operation", "proc_1.op_1", "confirm"))  # fmt: skip
    assert operation(found, "proc_1.op_1").review.status == ReviewStatus.CONFIRMED


def test_decisions_on_elements_of_an_earlier_version_are_kept_aside() -> None:
    found = state(event(1, "initiative", "ini_gone", "reject", comment="старая версия"))
    assert found.excluded == {} and found == state()


def test_the_outputs_are_never_changed() -> None:
    source_model, source_set = model(), outputs()
    before = (source_model.model_dump_json(), source_set.model_dump_json())
    apply_reviews(source_model, source_set, [
        event(1, "operation", "proc_1.op_2", "edit", {"name": "Сверка"}),
        event(2, "initiative", "ini_b", "reject", comment="нет"),
    ])  # fmt: skip
    assert (source_model.model_dump_json(), source_set.model_dump_json()) == before
    assert set(EDITABLE) == {"process", "operation", "decision_point", "finding", "initiative",
                             "relation"}  # fmt: skip
