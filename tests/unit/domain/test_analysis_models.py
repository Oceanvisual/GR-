"""Invariants of data-model §3–§6 (T008)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from gr.domain.assumptions import Assumption, AssumptionSet, Triple
from gr.domain.document import Citation, GroundingResult
from gr.domain.enums import (
    AssumptionSource,
    AutomationLevel,
    DurationBucket,
    GroundingStatus,
    KnowledgeLayer,
    LegalGate,
    Level5,
    OperationType,
    PriorityGroup,
    RegulatoryMode,
    Scenario,
    SolutionCategory,
)
from gr.domain.initiative import Finding, Initiative
from gr.domain.priority import PriorityConfig, PriorityResult
from gr.domain.process import (
    CoverageReport,
    DecisionOutcome,
    DecisionPoint,
    Grounded,
    Operation,
    Process,
    derive_legally_significant,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)
CITE = Citation(
    fragment_id="body/1",
    quote="принимает заявление",
    grounding=GroundingResult(status=GroundingStatus.VERIFIED, score=100),
)


def _fact(value: str = "x") -> Grounded[str]:
    return Grounded[str](value=value, citations=[CITE], layer=KnowledgeLayer.FACT)


def _operation(op_id: str = "proc_1.op_1", **overrides: Any) -> Operation:
    fields: dict[str, Any] = {
        "id": op_id,
        "seq": 1,
        "name": "Приём",
        "action": _fact(),
        "operation_type": OperationType.INTAKE,
    }
    return Operation(**(fields | overrides))


RATING = {"level": Level5.MEDIUM, "justification": "—"}
RATINGS = {
    "feasibility": RATING,
    "data_availability": RATING,
    "integration_complexity": RATING,
    "legal_risk": RATING,
    "operational_risk": RATING,
    "duration": {"bucket": DurationBucket.M3_6, "justification": "—"},
}


def _initiative(**overrides: Any) -> Initiative:
    fields: dict[str, Any] = {
        "id": "ini_1",
        "process_id": "proc_1",
        "finding_ids": ["fnd_1"],
        "operation_ids": ["proc_1.op_1"],
        "title": "t",
        "description": "d",
        "solution_category": SolutionCategory.CLASSIC_AUTOMATION,
        "architecture_idea": "a",
        "tech_rationale": "r",
        "alternatives_considered": [{"category": SolutionCategory.LLM_RAG, "why_not": "дорого"}],
        "regulatory_mode": RegulatoryMode.WITHIN_CURRENT,
        "automation_level": AutomationLevel.ASSISTIVE,
        "automation_level_proposed": AutomationLevel.ASSISTIVE,
        "reduction_potential": [
            {"operation_id": "proc_1.op_1", "level": Level5.HIGH, "justification": "—"}
        ],
        "ratings": RATINGS,
    }
    return Initiative.model_validate(fields | overrides)


def _assumption(key: str, scope: str, values: tuple[str, str, str], **overrides: Any) -> Assumption:
    fields: dict[str, Any] = {
        "key": key,
        "scope": scope,
        "target_id": None if scope == "global" else "proc_1",
        "unit": "—",
        "values": dict(zip(("conservative", "base", "optimistic"), values, strict=True)),
        "source": AssumptionSource.DEFAULT_PROFILE,
        "updated_by": "system",
        "updated_at": NOW,
    }
    return Assumption.model_validate(fields | overrides)


# --- grounded values and processes -----------------------------------------------------------


def test_fact_requires_citation() -> None:
    with pytest.raises(ValidationError, match="citation"):
        Grounded[str](value="x", layer=KnowledgeLayer.FACT)
    for layer in (KnowledgeLayer.INTERPRETATION, KnowledgeLayer.HYPOTHESIS):
        assert Grounded[str](value="x", layer=layer).citations == []


def test_grounded_is_generic() -> None:
    with pytest.raises(ValidationError):
        Grounded[int].model_validate({"value": "не число", "layer": "interpretation"})


def test_decision_point_needs_two_outcomes() -> None:
    one = [DecisionOutcome(label="да")]
    with pytest.raises(ValidationError, match="at least 2"):
        DecisionPoint(id="dp_1", after_operation_id="proc_1.op_1", condition=_fact(), outcomes=one)
    two = [*one, DecisionOutcome(label="нет", ends_process=True)]
    point = DecisionPoint(
        id="dp_1", after_operation_id="proc_1.op_1", condition=_fact(), outcomes=two
    )
    assert len(point.outcomes) == 2


def test_operation_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationError, match="itself"):
        _operation(depends_on=["proc_1.op_1"])


def test_process_ids_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Process(id="proc_1", name=_fact(), operations=[_operation(), _operation()])
    process = Process(id="proc_1", name=_fact(), operations=[_operation()])
    assert Process.model_validate_json(process.model_dump_json()) == process


@pytest.mark.parametrize(
    ("flag", "op_type", "expected"),
    [
        (False, OperationType.DECISION, True),
        (False, OperationType.SIGNING, True),
        (False, OperationType.INTAKE, False),
        (True, OperationType.INTAKE, True),
    ],
)
def test_legally_significant_rule(flag: bool, op_type: OperationType, expected: bool) -> None:
    assert derive_legally_significant(flag, op_type) is expected


def test_coverage_bounded() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        CoverageReport(fragments_total=1, fragments_used=2)


# --- findings and initiatives ----------------------------------------------------------------


def test_finding_is_hypothesis_with_operations() -> None:
    with pytest.raises(ValidationError):
        Finding(id="f", process_id="p", operation_ids=[], pattern="other", description="d")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Finding.model_validate(
            {
                "id": "f",
                "process_id": "p",
                "operation_ids": ["o"],
                "pattern": "other",
                "description": "d",
                "layer": "fact",
            }
        )


def test_valid_initiative_round_trips() -> None:
    initiative = _initiative()
    assert Initiative.model_validate_json(initiative.model_dump_json()) == initiative


def test_requires_change_implies_legal_review() -> None:
    provisions = [{"fragment_id": "body/1", "change_nature": "исключить"}]
    with pytest.raises(ValidationError, match="requires_legal_review"):
        _initiative(regulatory_mode=RegulatoryMode.REQUIRES_CHANGE, provisions_to_revise=provisions)
    with pytest.raises(ValidationError, match="provision"):
        _initiative(regulatory_mode=RegulatoryMode.REQUIRES_CHANGE, requires_legal_review=True)
    ok = _initiative(
        regulatory_mode=RegulatoryMode.REQUIRES_CHANGE,
        requires_legal_review=True,
        provisions_to_revise=provisions,
    )
    assert ok.requires_legal_review


def test_findings_and_alternatives_required_except_no_change() -> None:
    with pytest.raises(ValidationError, match="finding"):
        _initiative(finding_ids=[])
    with pytest.raises(ValidationError, match="alternative"):
        _initiative(alternatives_considered=[])
    no_change = _initiative(
        solution_category=SolutionCategory.NO_CHANGE, finding_ids=[], alternatives_considered=[]
    )
    assert no_change.solution_category == SolutionCategory.NO_CHANGE


def test_reduction_potential_covers_each_operation() -> None:
    with pytest.raises(ValidationError, match="reduction_potential"):
        _initiative(operation_ids=["proc_1.op_1", "proc_1.op_2"])
    with pytest.raises(ValidationError):
        _initiative(operation_ids=[])


def test_autonomy_cap_consistent() -> None:
    with pytest.raises(ValidationError, match="autonomy_capped"):
        _initiative(autonomy_capped=True)
    capped = _initiative(
        autonomy_capped=True,
        automation_level_proposed=AutomationLevel.AUTONOMOUS,
        automation_level=AutomationLevel.HUMAN_APPROVAL,
    )
    assert capped.autonomy_capped


# --- assumptions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["overhead_ratio", "op.proc_1.op_1.case_share", "ini.ini_1.adoption_y2"],
)
def test_shares_within_unit_interval(key: str) -> None:
    scope = {"overhead_ratio": "global", "case_share": "operation", "adoption_y2": "initiative"}
    parameter = key.rsplit(".", 1)[-1]
    with pytest.raises(ValidationError, match="share"):
        _assumption(key, scope[parameter], ("0.5", "1", "1.01"))
    assert _assumption(key, scope[parameter], ("0", "0.5", "1")).values.base == Decimal("0.5")


def test_triple_non_negative_and_scenario_access() -> None:
    with pytest.raises(ValidationError):
        Triple(conservative=Decimal(-1), base=Decimal(1), optimistic=Decimal(1))
    triple = Triple(conservative=Decimal(1), base=Decimal(2), optimistic=Decimal(3))
    assert [triple.get(s) for s in Scenario] == [Decimal(1), Decimal(2), Decimal(3)]


def test_horizon_years_single_value_in_range() -> None:
    with pytest.raises(ValidationError, match="same"):
        _assumption("horizon_years", "global", ("2", "3", "3"))
    with pytest.raises(ValidationError, match="1, 2 or 3"):
        _assumption("horizon_years", "global", ("4", "4", "4"))
    assert _assumption("horizon_years", "global", ("3", "3", "3")).values.base == 3


def test_assumption_scope_and_target() -> None:
    with pytest.raises(ValidationError, match="scope"):
        _assumption("op.proc_1.op_1.time_hours", "process", ("1", "1", "1"))
    with pytest.raises(ValidationError, match="unknown"):
        _assumption("op.proc_1.op_1.magic", "operation", ("1", "1", "1"))
    with pytest.raises(ValidationError, match="target_id"):
        _assumption("hourly_cost", "global", ("1", "1", "1"), target_id="proc_1")
    with pytest.raises(ValidationError, match="target_id"):
        _assumption("proc.proc_1.volume_per_year", "process", ("1", "1", "1"), target_id=None)


def test_document_sourced_assumption_needs_citation() -> None:
    with pytest.raises(ValidationError, match="citation"):
        _assumption("hourly_cost", "global", ("1", "1", "1"), source=AssumptionSource.DOCUMENT)


def test_assumption_set_unique_keys() -> None:
    item = _assumption("hourly_cost", "global", ("1", "1", "1"))
    with pytest.raises(ValidationError, match="unique"):
        AssumptionSet(run_id="r", version=1, items=[item, item], created_by="u", created_at=NOW)
    with pytest.raises(ValidationError):
        AssumptionSet(run_id="r", version=0, items=[item], created_by="u", created_at=NOW)


# --- priority --------------------------------------------------------------------------------


def test_default_priority_config_valid() -> None:
    config = PriorityConfig()
    assert sum(config.weights.values()) == 100
    assert config.cutoff_score == 35


def test_priority_weights_rules() -> None:
    weights = dict(PriorityConfig().weights)
    with pytest.raises(ValidationError, match="positive sum"):
        PriorityConfig(weights=dict.fromkeys(weights, Decimal(0)))
    with pytest.raises(ValidationError, match="positive sum"):
        PriorityConfig(weights=weights | {"effect": Decimal(-1)})
    with pytest.raises(ValidationError, match="cover"):
        PriorityConfig(weights={k: v for k, v in weights.items() if k != "risk"})


def test_priority_maps_complete_and_bounded() -> None:
    level_map = dict(PriorityConfig().level_map)
    with pytest.raises(ValidationError, match="level_map"):
        PriorityConfig(level_map=level_map | {Level5.HIGH: Decimal("1.5")})
    with pytest.raises(ValidationError, match="level_map"):
        PriorityConfig(level_map={k: v for k, v in level_map.items() if k != Level5.LOW})


def test_blocked_initiative_not_ranked() -> None:
    fields: dict[str, Any] = {
        "initiative_id": "ini_1",
        "criteria": {},
        "score": Decimal(80),
        "ease": Decimal("0.5"),
        "group": PriorityGroup.NOT_RECOMMENDED,
        "group_rule": 1,
        "group_reason": "юридическое ограничение",
        "gate": LegalGate.BLOCKED,
        "assumptions_version": 1,
        "priority_config_version": 1,
    }
    assert PriorityResult(**fields).rank is None
    with pytest.raises(ValidationError, match="ranked"):
        PriorityResult(**(fields | {"rank": 1}))
    with pytest.raises(ValidationError, match="not_recommended"):
        PriorityResult(**(fields | {"group": PriorityGroup.QUICK_WIN}))
