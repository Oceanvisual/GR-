"""T038: ordered rules accumulate reasons, and only affected operations cap autonomy."""

import pytest
from initiative_builders import initiative

from gr.analysis.gating import cap_autonomy, compute_gate
from gr.domain.enums import (
    AutomationLevel,
    KnowledgeLayer,
    LegalGate,
    OperationType,
    SolutionCategory,
)
from gr.domain.process import Grounded, Operation


def operation(*, legal: bool = False, personal: bool = False, key: str = "op1") -> Operation:
    return Operation(
        id=key,
        seq=1,
        name="x",
        action=Grounded[str](value="x", layer=KnowledgeLayer.INTERPRETATION),
        operation_type=OperationType.DECISION if legal else OperationType.OTHER,
        legally_significant=legal,
        handles_personal_data=personal,
    )


def test_legal_decision_caps_autonomy_without_mutating_proposal() -> None:
    item = initiative(
        SolutionCategory.AI_AGENT,
        automation_level=AutomationLevel.AUTONOMOUS,
        automation_level_proposed=AutomationLevel.AUTONOMOUS,
    )
    before = item.model_dump_json()
    ops = [operation(legal=True)]
    capped = cap_autonomy(item, ops)
    assert capped.automation_level == AutomationLevel.HUMAN_APPROVAL
    assert capped.automation_level_proposed == AutomationLevel.AUTONOMOUS
    assert capped.autonomy_capped
    assert cap_autonomy(capped, ops) == capped
    assert item.model_dump_json() == before
    assert compute_gate(capped, ops).status == LegalGate.ALLOWED_WITH_HITL


def test_personal_data_requires_hitl_without_legal_autonomy_cap() -> None:
    item = initiative(
        SolutionCategory.AI_AGENT,
        automation_level=AutomationLevel.AUTONOMOUS,
        automation_level_proposed=AutomationLevel.AUTONOMOUS,
    )
    result = cap_autonomy(item, [operation(personal=True)])
    assert not result.autonomy_capped and result.automation_level == AutomationLevel.AUTONOMOUS
    assert compute_gate(result, [operation(personal=True)]).status == LegalGate.ALLOWED_WITH_HITL


def test_unrelated_legal_operations_do_not_change_the_gate() -> None:
    item = initiative(SolutionCategory.CLASSIC_AUTOMATION)
    ops = [operation(), operation(legal=True, key="other")]
    assert compute_gate(item, ops).status == LegalGate.ALLOWED
    assert not cap_autonomy(item, ops).autonomy_capped


def test_all_rules_accumulate_reasons_in_precedence_order() -> None:
    item = initiative(SolutionCategory.LLM_RAG)
    data = item.model_dump(mode="json")
    data.update(
        {
            "regulatory_mode": "requires_change",
            "requires_legal_review": True,
            "provisions_to_revise": [{"fragment_id": "f", "change_nature": "x"}],
            "automation_level_proposed": "autonomous",
            "automation_level": "human_approval",
            "autonomy_capped": True,
        }
    )
    data["ratings"]["legal_risk"]["level"] = "very_high"
    item = type(item).model_validate(data)
    ops = [operation(legal=True, personal=True)]
    gate = compute_gate(item, ops, expert_gate=LegalGate.BLOCKED)
    assert gate.status == LegalGate.BLOCKED
    assert len(gate.reasons) == 6
    keywords = [
        "Эксперт",
        "критическая",
        "изменение НПА",
        "юридически значимые",
        "персональных",
        "автономности",
    ]
    assert all(word in reason for word, reason in zip(keywords, gate.reasons, strict=True))
    # Explicit permission cannot override a critical risk automatically.
    assert compute_gate(item, ops, expert_gate=LegalGate.ALLOWED).status == LegalGate.BLOCKED
    data["ratings"]["legal_risk"]["level"] = "high"
    item = type(item).model_validate(data)
    assert compute_gate(item, ops).status == LegalGate.REQUIRES_REGULATORY_CHANGE


@pytest.mark.parametrize("func", [cap_autonomy, compute_gate])
def test_missing_affected_operation_is_an_error(func: object) -> None:
    item = initiative(SolutionCategory.CLASSIC_AUTOMATION)
    with pytest.raises(ValueError, match="unknown operation"):
        if func is cap_autonomy:
            cap_autonomy(item, [])
        else:
            compute_gate(item, [])
