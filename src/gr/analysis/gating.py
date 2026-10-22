"""Autonomy cap and accumulated admissibility reasons (FR-045, FR-063, T038)."""

from collections.abc import Sequence

from gr.domain.enums import AutomationLevel, LegalGate, Level5, RegulatoryMode
from gr.domain.initiative import Gate, Initiative
from gr.domain.process import Operation


def _affected(initiative: Initiative, operations: Sequence[Operation]) -> list[Operation]:
    selected = [op for op in operations if op.id in initiative.operation_ids]
    if {op.id for op in selected} != set(initiative.operation_ids):
        raise ValueError("initiative refers to an unknown operation")
    if len({op.id for op in selected}) != len(selected):
        raise ValueError("operation ids must be unique")
    return selected


def cap_autonomy(initiative: Initiative, operations: Sequence[Operation]) -> Initiative:
    affected = _affected(initiative, operations)
    capped = initiative.automation_level_proposed == AutomationLevel.AUTONOMOUS and any(
        op.legally_significant for op in affected
    )
    return initiative.model_copy(
        update={
            "automation_level": AutomationLevel.HUMAN_APPROVAL
            if capped
            else initiative.automation_level_proposed,
            "autonomy_capped": capped,
        }
    )


def compute_gate(
    initiative: Initiative,
    operations: Sequence[Operation],
    *,
    expert_gate: LegalGate | None = None,
) -> Gate:
    """Highest-precedence rule wins; all applicable reasons remain in the specified order.

    Only an expert's blocked gate overrides automatic rules; an allowed override cannot
    silently clear critical risk or a required regulatory change.
    """
    affected = _affected(initiative, operations)
    rules: list[tuple[bool, LegalGate, str]] = [
        (expert_gate == LegalGate.BLOCKED, LegalGate.BLOCKED, "Эксперт запретил инициативу."),
        (
            initiative.ratings.legal_risk.level == Level5.VERY_HIGH,
            LegalGate.BLOCKED,
            "Оценка юридического риска — критическая; требуется юридическая экспертиза.",
        ),
        (
            initiative.regulatory_mode == RegulatoryMode.REQUIRES_CHANGE,
            LegalGate.REQUIRES_REGULATORY_CHANGE,
            "Требуется изменение НПА.",
        ),
        (
            any(op.legally_significant for op in affected),
            LegalGate.ALLOWED_WITH_HITL,
            "Затронуты юридически значимые операции; требуется контроль человеком.",
        ),
        (
            any(op.handles_personal_data for op in affected),
            LegalGate.ALLOWED_WITH_HITL,
            "Затронута обработка персональных данных; требуется контроль человеком.",
        ),
        (
            initiative.autonomy_capped,
            LegalGate.ALLOWED_WITH_HITL,
            "Уровень автономности ограничен; решение подтверждает человек.",
        ),
    ]
    matched = [(status, reason) for applies, status, reason in rules if applies]
    return Gate(
        status=matched[0][0] if matched else LegalGate.ALLOWED,
        reasons=[reason for _, reason in matched],
    )
