"""The portfolio on 200 random analyses (data-model §7 properties, T065).

Each case is built from its own `random.Random(SEED + n)`. Findings come from a small pool and
operations overlap, so alternative and complementary initiatives are common; some initiatives
are rejected, blocked or Not Recommended, and a third of the assumptions get user values.
"""

import random
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache

from initiative_builders import initiative

from gr.analysis.defaults import build_assumptions
from gr.analysis.economics import compute_solo
from gr.analysis.portfolio import build_portfolio
from gr.analysis.relations import alternative_components, infer_relations
from gr.domain.assumptions import SHARE_PARAMETERS, AssumptionSet, Triple
from gr.domain.economics import EconomicsResult
from gr.domain.enums import (
    AssumptionSource,
    InitiativeRelation,
    KnowledgeLayer,
    LegalGate,
    Level5,
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

SEED = 20261022
CASES = 200
NOW = datetime(2026, 10, 22, tzinfo=UTC)
TINY = Decimal("1e-18")  # the share g_i / Σ g is a division: equal up to 28-digit rounding

Case = tuple[ProcessModel, list[Initiative], AssumptionSet, list[PriorityResult]]


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def random_value(rng: random.Random, parameter: str) -> Decimal:
    if parameter in SHARE_PARAMETERS:
        return Decimal(rng.randint(0, 100)) / 100
    if parameter == "fte_hours_per_year":
        return Decimal(rng.randint(1, 2500))
    return Decimal(rng.randint(0, 100_000)) / rng.choice((1, 10, 100))


@cache
def case(n: int) -> Case:
    rng = random.Random(SEED + n)
    processes: list[Process] = []
    initiatives: list[Initiative] = []
    for p in range(1, rng.randint(1, 2) + 1):
        pid = f"proc_{p}"
        operations = [
            Operation(id=f"{pid}.op_{o}", seq=o, name=f"Операция {o}", action=text("действие"),
                      operation_type=rng.choice(list(OperationType)))
            for o in range(1, rng.randint(2, 5) + 1)
        ]  # fmt: skip
        processes.append(Process(id=pid, name=text(f"Процесс {p}"), operations=operations))
        for k in range(1, rng.randint(1, 6) + 1):
            chosen = rng.sample(operations, rng.randint(1, min(3, len(operations))))
            initiatives.append(
                initiative(
                    rng.choice([c for c in SolutionCategory if c != SolutionCategory.NO_CHANGE]),
                    id=f"ini_{pid}_{k}",
                    process_id=pid,
                    finding_ids=[f"f{rng.randint(1, 3)}"],
                    operation_ids=[op.id for op in chosen],
                    reduction_potential=[
                        {"operation_id": op.id, "level": rng.choice(list(Level5)).value,
                         "justification": "x"}
                        for op in chosen
                    ],
                    review=ReviewState(status=ReviewStatus.REJECTED)
                    if rng.random() < 0.1
                    else ReviewState(),
                )
            )  # fmt: skip
    initiatives = infer_relations(initiatives)
    model = ProcessModel(
        processes=processes, coverage=CoverageReport(fragments_total=1, fragments_used=1)
    )
    defaults = build_assumptions(model, initiatives, run_id="run_1", now=NOW)
    items = []
    for item in defaults.items:
        if item.parameter != "horizon_years" and rng.random() < 0.3:
            low, base, high = (random_value(rng, item.parameter) for _ in range(3))
            item = item.model_copy(
                update={
                    "values": Triple(conservative=low, base=base, optimistic=high),
                    "source": AssumptionSource.USER,
                }
            )
        items.append(item)
    assumptions = defaults.model_copy(update={"items": items})
    active = [i for i in initiatives if i.review.status != ReviewStatus.REJECTED]
    order = rng.sample(active, len(active))
    priorities = []
    for rank, proposal in enumerate(order, 1):
        blocked = rng.random() < 0.1
        priorities.append(
            PriorityResult(
                initiative_id=proposal.id,
                criteria={},
                score=Decimal(100 - rank),
                ease=Decimal("0.5"),
                group=PriorityGroup.NOT_RECOMMENDED
                if blocked or rng.random() < 0.2
                else rng.choice([PriorityGroup.QUICK_WIN, PriorityGroup.STRATEGIC,
                                 PriorityGroup.COMPLEX]),
                group_rule=1 if blocked else 4,
                group_reason="причина",
                gate=LegalGate.BLOCKED if blocked else LegalGate.ALLOWED,
                rank=None if blocked else rank,
                assumptions_version=1,
                priority_config_version=1,
            )
        )  # fmt: skip
    return model, initiatives, assumptions, priorities


def result(found: Case, scenario: Scenario = Scenario.BASE) -> EconomicsResult:
    model, initiatives, assumptions, priorities = found
    solo = compute_solo(model, initiatives, assumptions, scenario)
    return build_portfolio(initiatives, assumptions, solo, priorities)


def shuffled(n: int) -> Case:
    model, initiatives, assumptions, priorities = case(n)
    rng = random.Random(-SEED - n)
    items = rng.sample(assumptions.items, len(assumptions.items))
    return (
        model,
        rng.sample(initiatives, len(initiatives)),
        assumptions.model_copy(update={"items": items}),
        rng.sample(priorities, len(priorities)),
    )


def test_the_portfolio_is_the_sum_of_the_attributed_effects() -> None:
    members_seen = 0
    for n in range(CASES):
        for scenario in Scenario:
            found = result(case(n), scenario)
            assert found.portfolio is not None
            attributed = [e.attributed for e in found.per_initiative if e.attributed]
            members_seen += len(attributed)
            if not attributed:
                assert found.portfolio.total is None, n
                continue
            total = found.portfolio.total
            assert total is not None
            for name in ("hours", "money", "opex"):
                for year, value in enumerate(getattr(total, name)):
                    assert value == sum(getattr(e, name)[year] for e in attributed), (n, name)
            assert total.capex == sum(e.capex for e in attributed), n
    assert members_seen > CASES  # the generator does produce portfolios


def test_combined_reduction_and_attribution_bounds() -> None:
    for n in range(CASES):
        found = result(case(n))
        for overlap in found.overlaps:
            assert all(0 <= r <= 1 for r in overlap.combined_reduction), n
        for economics in found.per_initiative:
            if economics.attributed is None:
                continue
            # 1 − Π (1 − g) ≤ Σ g: an initiative never gets more than it would alone.
            for alone, shared in zip(economics.solo.hours, economics.attributed.hours,
                                     strict=True):  # fmt: skip
                assert shared <= alone + TINY, (n, economics.initiative_id)


def test_membership_follows_the_rules() -> None:
    for n in range(CASES):
        _, initiatives, _, priorities = case(n)
        found = result(case(n))
        by_id = {p.initiative_id: p for p in priorities}
        members = {e.initiative_id for e in found.per_initiative if e.in_portfolio}
        assert found.portfolio is not None and sorted(members) == found.portfolio.initiative_ids
        for key in members:
            assert by_id[key].gate != LegalGate.BLOCKED, n
            assert by_id[key].group != PriorityGroup.NOT_RECOMMENDED, n
        rejected = {i.id for i in initiatives if i.review.status == ReviewStatus.REJECTED}
        assert not members & rejected
        for item in initiatives:
            if item.id in members:  # no two alternatives in the portfolio
                partners = {
                    r.initiative_id
                    for r in item.relations
                    if r.relation == InitiativeRelation.ALTERNATIVE
                }
                assert not partners & members, (n, item.id)
        eligible = {
            key for key, p in by_id.items()
            if p.gate != LegalGate.BLOCKED and p.group != PriorityGroup.NOT_RECOMMENDED
        }  # fmt: skip
        for component in alternative_components(initiatives):
            if set(component) & eligible:  # one member from every group with a candidate
                assert len(set(component) & members) == 1, (n, component)
        for economics in found.per_initiative:
            assert economics.in_portfolio == (economics.excluded_reason is None), n


def test_overlaps_are_the_operations_of_several_members() -> None:
    for n in range(CASES):
        _, initiatives, _, _ = case(n)
        found = result(case(n))
        members = {e.initiative_id for e in found.per_initiative if e.in_portfolio}
        users: dict[str, set[str]] = defaultdict(set)
        for item in initiatives:
            if item.id in members:
                for op in item.operation_ids:
                    users[op].add(item.id)
        expected = {op: sorted(keys) for op, keys in users.items() if len(keys) > 1}
        assert {o.operation_id: o.initiative_ids for o in found.overlaps} == expected, n


def test_order_of_the_input_and_repeated_calls_do_not_matter() -> None:
    for n in range(CASES):
        expected = result(case(n)).model_dump_json()
        assert result(shuffled(n)).model_dump_json() == expected, n
        assert result(case(n)).model_dump_json() == expected, n


def test_membership_is_the_same_in_every_scenario() -> None:
    for n in range(CASES):
        members = {
            tuple(found.portfolio.initiative_ids)
            for scenario in Scenario
            if (found := result(case(n), scenario)).portfolio is not None
        }
        assert len(members) == 1, n
