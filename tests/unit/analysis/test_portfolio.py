"""Portfolio membership, combination and attribution (FR-054, FR-055, data-model §7, T065)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from initiative_builders import initiative

from gr.analysis.defaults import build_assumptions
from gr.analysis.economics import compute_solo
from gr.analysis.portfolio import build_portfolio, membership
from gr.analysis.relations import infer_relations
from gr.domain.assumptions import (
    AssumptionSet,
    Triple,
    global_key,
    initiative_key,
    initiative_operation_key,
    operation_key,
    process_key,
)
from gr.domain.economics import EconomicsResult
from gr.domain.enums import (
    KnowledgeLayer,
    LegalGate,
    OperationType,
    PriorityGroup,
    ReviewStatus,
    Scenario,
    SolutionCategory,
)
from gr.domain.initiative import Initiative
from gr.domain.priority import PriorityResult
from gr.domain.process import CoverageReport, Grounded, Operation, Process, ProcessModel
from gr.domain.review import ReviewState

NOW = datetime(2026, 10, 22, tzinfo=UTC)
OPS = [f"proc_1.op_{n}" for n in (1, 2, 3)]


def model() -> ProcessModel:
    text = Grounded[str](value="действие", layer=KnowledgeLayer.INTERPRETATION)
    operations = [
        Operation(id=op, seq=n, name=f"Операция {n}", action=text,
                  operation_type=OperationType.DATA_VERIFICATION)
        for n, op in enumerate(OPS)
    ]  # fmt: skip
    return ProcessModel(
        processes=[Process(id="proc_1", name=text, operations=operations)],
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )


def make(key: str, finding: str, *ops: str, **updates: Any) -> Initiative:
    return initiative(
        SolutionCategory.CLASSIC_AUTOMATION,
        id=key,
        process_id="proc_1",
        finding_ids=[finding],
        operation_ids=list(ops),
        reduction_potential=[
            {"operation_id": op, "level": "medium", "justification": "x"} for op in ops
        ],
        **updates,
    )


def priority(
    key: str,
    rank: int,
    group: PriorityGroup = PriorityGroup.QUICK_WIN,
    gate: LegalGate = LegalGate.ALLOWED,
) -> PriorityResult:
    blocked = gate == LegalGate.BLOCKED
    return PriorityResult(
        initiative_id=key,
        criteria={},
        score=Decimal(90 - rank),
        ease=Decimal("0.7"),
        group=PriorityGroup.NOT_RECOMMENDED if blocked else group,
        group_rule=1 if blocked else 4,
        group_reason="причина группы",
        gate=gate,
        rank=None if blocked else rank,
        assumptions_version=1,
        priority_config_version=1,
    )


def assumptions(initiatives: list[Initiative], values: dict[str, str]) -> AssumptionSet:
    """The profile, with the given keys set to one value in all three scenarios."""
    found = build_assumptions(model(), initiatives, run_id="run_1", now=NOW)
    items = [
        i.model_copy(update={"values": Triple(conservative=d, base=d, optimistic=d)})
        if (d := Decimal(values[i.key]) if i.key in values else None) is not None
        else i
        for i in found.items
    ]
    return found.model_copy(update={"items": items})


def simple(initiatives: list[Initiative], reductions: dict[tuple[str, str], str]) -> AssumptionSet:
    """One year, 1 000 cases, 1 h an operation, 1 000 ₽ an hour, full adoption, no costs."""
    values = {
        global_key("horizon_years"): "1",
        global_key("hourly_cost"): "1000",
        global_key("overhead_ratio"): "0",
        process_key("proc_1", "volume_per_year"): "1000",
    }
    for op in OPS:
        values |= {operation_key(op, "time_hours"): "1", operation_key(op, "case_share"): "1"}
    for item in initiatives:
        values |= {
            initiative_key(item.id, "capex"): "0",
            initiative_key(item.id, "fixed_opex_per_year"): "0",
            initiative_key(item.id, "adoption_y1"): "1",
        }
        for op in item.operation_ids:
            values |= {
                initiative_operation_key(item.id, op, "reduction_share"): reductions[(item.id, op)],
                initiative_operation_key(item.id, op, "review_hours"): "0",
                initiative_operation_key(item.id, op, "variable_cost_per_exec"): "0",
            }
    return assumptions(initiatives, values)


def portfolio(
    initiatives: list[Initiative],
    found: AssumptionSet,
    priorities: list[PriorityResult],
    scenario: Scenario = Scenario.BASE,
) -> EconomicsResult:
    linked = infer_relations(initiatives)
    solo = compute_solo(model(), linked, found, scenario)
    return build_portfolio(linked, found, solo, priorities)


def test_fifty_and_forty_percent_combine_into_seventy() -> None:
    # US4 scenario 3: two complementary initiatives (different findings) on one operation.
    a, b = make("a", "f1", OPS[0]), make("b", "f2", OPS[0])
    found = simple([a, b], {("a", OPS[0]): "0.5", ("b", OPS[0]): "0.4"})
    result = portfolio([a, b], found, [priority("a", 1), priority("b", 2)])
    solo = {e.initiative_id: e.solo.hours[0] for e in result.per_initiative}
    assert solo == {"a": 500, "b": 400}  # the solo effects double count: 900 h
    (overlap,) = result.overlaps
    assert (overlap.operation_id, overlap.initiative_ids) == (OPS[0], ["a", "b"])
    assert overlap.combined_reduction == [Decimal("0.7")]  # 1 − 0.5 × 0.6
    attributed = {e.initiative_id: e.attributed for e in result.per_initiative}
    assert attributed["a"] is not None and attributed["b"] is not None
    # Shares of 700 h by 0.5 : 0.4.
    assert round(attributed["a"].hours[0], 6) == Decimal("388.888889")
    assert round(attributed["b"].hours[0], 6) == Decimal("311.111111")
    assert result.portfolio is not None and result.portfolio.total is not None
    total = result.portfolio.total
    assert total.hours == [attributed["a"].hours[0] + attributed["b"].hours[0]]
    assert round(total.hours[0], 18) == 700 and round(total.money[0], 12) == 700_000


def test_one_of_the_alternatives_the_best_eligible() -> None:
    # Same finding, same operation: alternatives; only the better ranked one is in.
    a, c = make("a", "f1", OPS[1]), make("c", "f1", OPS[1])
    found = simple([a, c], {("a", OPS[1]): "0.5", ("c", OPS[1]): "0.3"})
    result = portfolio([a, c], found, [priority("a", 2), priority("c", 1)])
    assert result.portfolio is not None and result.portfolio.initiative_ids == ["c"]
    excluded = next(e for e in result.per_initiative if e.initiative_id == "a")
    assert not excluded.in_portfolio and excluded.attributed is None
    assert excluded.excluded_reason == "Альтернатива инициативе c с лучшим баллом."
    assert result.overlaps == []  # alternatives never combine
    # A blocked best does not leave the group empty: the next eligible one goes in.
    blocked = portfolio([a, c], found, [priority("a", 2), priority("c", 1, gate=LegalGate.BLOCKED)])
    assert blocked.portfolio is not None and blocked.portfolio.initiative_ids == ["a"]


def test_not_recommended_blocked_and_rejected_stay_out() -> None:
    items = [
        make("a", "f1", OPS[0]),
        make("n", "f2", OPS[1]),
        make("x", "f3", OPS[2]),
        make("r", "f4", OPS[2], review=ReviewState(status=ReviewStatus.REJECTED)),
    ]
    found = simple(items, {(i.id, i.operation_ids[0]): "0.5" for i in items})
    priorities = [
        priority("a", 1),
        priority("n", 2, group=PriorityGroup.NOT_RECOMMENDED),
        priority("x", 0, gate=LegalGate.BLOCKED),
    ]
    reasons = membership(infer_relations(items), priorities)
    assert reasons == {
        "a": None,
        "n": "Not Recommended: причина группы",
        "x": "Недопустимо: юридическое ограничение.",
    }  # the rejected one is not even considered
    result = portfolio(items, found, priorities)
    assert [e.initiative_id for e in result.per_initiative] == ["a", "n", "x"]
    assert result.portfolio is not None and result.portfolio.initiative_ids == ["a"]


def test_without_overlap_the_attributed_effect_is_the_solo_one() -> None:
    a, b = make("a", "f1", OPS[0], OPS[1]), make("b", "f2", OPS[2])
    found = assumptions([a, b], {})  # the profile: three years, review time, costs
    result = portfolio([a, b], found, [priority("a", 1), priority("b", 2)])
    for economics in result.per_initiative:
        assert economics.attributed == economics.solo
    assert result.overlaps == []


def test_an_empty_portfolio_has_no_total() -> None:
    a = make("a", "f1", OPS[0])
    found = simple([a], {("a", OPS[0]): "0.5"})
    result = portfolio([a], found, [priority("a", 1, group=PriorityGroup.NOT_RECOMMENDED)])
    assert result.portfolio is not None
    assert result.portfolio.initiative_ids == [] and result.portfolio.total is None


def test_membership_is_the_same_in_every_scenario() -> None:
    a, b, c = make("a", "f1", OPS[0]), make("b", "f2", OPS[0]), make("c", "f1", OPS[0])
    found = assumptions([a, b, c], {})
    priorities = [priority("a", 1), priority("b", 2), priority("c", 3)]
    members = {
        scenario: portfolio([a, b, c], found, priorities, scenario).portfolio
        for scenario in Scenario
    }
    assert {tuple(m.initiative_ids) for m in members.values() if m} == {("a", "b")}
    conservative = members[Scenario.CONSERVATIVE]
    optimistic = members[Scenario.OPTIMISTIC]
    assert conservative and optimistic and conservative.total and optimistic.total
    assert conservative.total.hours != optimistic.total.hours  # the scenario columns differ
