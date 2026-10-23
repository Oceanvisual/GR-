"""Solo economics on 200 random analyses (data-model §7 properties, T044).

Each case is built from its own `random.Random(SEED + n)`, so a failure names the case to
replay. Assumptions start from the profile and a random third of them get user values.
"""

import random
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache

from initiative_builders import initiative

from gr.analysis.defaults import build_assumptions
from gr.analysis.economics import compute_solo
from gr.domain.assumptions import SHARE_PARAMETERS, Assumption, AssumptionSet, Triple
from gr.domain.economics import EconomicsResult
from gr.domain.enums import (
    AssumptionSource,
    KnowledgeLayer,
    Level5,
    OperationType,
    ReviewStatus,
    Scenario,
    SolutionCategory,
)
from gr.domain.initiative import Initiative
from gr.domain.process import CoverageReport, Grounded, Operation, Process, ProcessModel
from gr.domain.review import ReviewState

SEED = 20261022
CASES = 200
NOW = datetime(2026, 10, 22, tzinfo=UTC)

Case = tuple[ProcessModel, list[Initiative], AssumptionSet]


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def number(rng: random.Random, parameter: str) -> Decimal:
    if parameter in SHARE_PARAMETERS:
        return Decimal(rng.randint(0, 100)) / 100
    if parameter == "fte_hours_per_year":
        return Decimal(rng.randint(1, 2500))
    return Decimal(rng.randint(0, 1_000_000)) / rng.choice((1, 10, 100))


def user_value(item: Assumption, rng: random.Random) -> Assumption:
    low, base, high = (number(rng, item.parameter) for _ in range(3))
    return item.model_copy(
        update={
            "values": Triple(conservative=low, base=base, optimistic=high),
            "source": AssumptionSource.USER,
            "requires_input": False,
        }
    )


def random_initiative(rng: random.Random, key: str, process: Process) -> Initiative:
    chosen = rng.sample(process.operations, rng.randint(1, len(process.operations)))
    ratings = initiative(SolutionCategory.HYBRID).ratings.model_dump(mode="json")
    ratings["integration_complexity"]["level"] = rng.choice(list(Level5)).value
    status = rng.choice((ReviewStatus.PENDING, ReviewStatus.CONFIRMED, ReviewStatus.REJECTED))
    return initiative(
        rng.choice(list(SolutionCategory)),
        id=key,
        process_id=process.id,
        operation_ids=[op.id for op in chosen],
        reduction_potential=[
            {"operation_id": op.id, "level": rng.choice(list(Level5)).value, "justification": "x"}
            for op in chosen
        ],
        ratings=ratings,
        review=ReviewState(status=status),
    )


@cache
def case(n: int) -> Case:
    rng = random.Random(SEED + n)
    processes: list[Process] = []
    initiatives: list[Initiative] = []
    for p in range(1, rng.randint(1, 3) + 1):
        pid = f"proc_{p}"
        operations = [
            Operation(
                id=f"{pid}.op_{o}",
                seq=o,
                name=f"Операция {o}",
                action=text("действие"),
                operation_type=rng.choice(list(OperationType)),
            )
            for o in range(1, rng.randint(1, 5) + 1)
        ]
        process = Process(id=pid, name=text(f"Процесс {p}"), operations=operations)
        processes.append(process)
        initiatives += [
            random_initiative(rng, f"ini_{pid}_{k}", process)
            for k in range(1, rng.randint(0, 3) + 1)
        ]
    model = ProcessModel(
        processes=processes, coverage=CoverageReport(fragments_total=1, fragments_used=1)
    )
    defaults = build_assumptions(model, initiatives, run_id="run_1", now=NOW)
    horizon = Decimal(rng.randint(1, 3))
    items = []
    for item in defaults.items:
        if item.parameter == "horizon_years":
            item = item.model_copy(
                update={"values": Triple(conservative=horizon, base=horizon, optimistic=horizon)}
            )
        elif rng.random() < 0.3:
            item = user_value(item, rng)
        items.append(item)
    return model, initiatives, defaults.model_copy(update={"items": items})


def shuffled(n: int) -> Case:
    model, initiatives, assumptions = case(n)
    rng = random.Random(-SEED - n)
    processes = rng.sample(model.processes, len(model.processes))
    items = rng.sample(assumptions.items, len(assumptions.items))
    return (
        model.model_copy(update={"processes": processes}),
        rng.sample(initiatives, len(initiatives)),
        assumptions.model_copy(update={"items": items}),
    )


def solo(found: Case, scenario: Scenario) -> EconomicsResult:
    return compute_solo(found[0], found[1], found[2], scenario)


def test_result_does_not_depend_on_the_order_of_the_input() -> None:
    for n in range(CASES):
        for scenario in Scenario:
            expected = solo(case(n), scenario).model_dump_json()
            assert solo(shuffled(n), scenario).model_dump_json() == expected, (n, scenario)


def test_repeated_calls_give_identical_results() -> None:
    for n in range(CASES):
        for scenario in Scenario:
            assert solo(case(n), scenario) == solo(case(n), scenario), (n, scenario)


def test_every_effect_adds_up() -> None:
    covered = 0
    for n in range(CASES):
        _, initiatives, assumptions = case(n)
        horizon = int(assumptions.by_key()["horizon_years"].values.base)
        found = solo(case(n), Scenario.BASE)
        expected = sorted(i.id for i in initiatives if i.review.status != ReviewStatus.REJECTED)
        assert [e.initiative_id for e in found.per_initiative] == expected, n
        for economics in found.per_initiative:
            effect = economics.solo
            assert len(effect.hours) == horizon, n
            assert effect.cum_net == sum(effect.net, Decimal(0)), n
            assert effect.net[0] == effect.money[0] - effect.opex[0] - effect.capex, n
            later = zip(effect.money[1:], effect.opex[1:], strict=True)
            assert effect.net[1:] == [money - opex for money, opex in later], n
            covered += 1
    assert covered > CASES  # the generator does produce initiatives
