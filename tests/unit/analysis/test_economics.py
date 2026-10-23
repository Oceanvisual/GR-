"""Solo economics on hand-computed examples (FR-053, spec «Экономические формулы», T044).

Each example spells out the arithmetic; values sit in one scenario column and the others
hold different numbers, so reading the wrong column shows.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from initiative_builders import initiative

from gr.analysis.defaults import build_assumptions
from gr.analysis.economics import AssumptionValidationError, compute_solo
from gr.domain.assumptions import (
    PARAMETER_SCOPES,
    Assumption,
    AssumptionSet,
    Triple,
    global_key,
    initiative_key,
    initiative_operation_key,
    initiative_operation_target,
    operation_key,
    process_key,
)
from gr.domain.economics import Effect
from gr.domain.enums import (
    AssumptionSource,
    KnowledgeLayer,
    OperationType,
    ReviewStatus,
    Scenario,
    SolutionCategory,
)
from gr.domain.initiative import Initiative
from gr.domain.process import CoverageReport, Grounded, Operation, Process, ProcessModel
from gr.domain.review import ReviewState

NOW = datetime(2026, 10, 22, tzinfo=UTC)
# One value in all scenarios, or (conservative, base, optimistic).
Value = str | tuple[str, str, str]


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def model() -> ProcessModel:
    operations = [
        Operation(
            id=f"proc_1.op_{n}",
            seq=n,
            name=f"Операция {n}",
            action=text("действие"),
            operation_type=OperationType.DATA_VERIFICATION,
        )
        for n in (1, 2)
    ]
    return ProcessModel(
        processes=[Process(id="proc_1", name=text("Выдача разрешения"), operations=operations)],
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )


def rag(key: str = "ini_1", *ops: str, **updates: Any) -> Initiative:
    ops = ops or ("proc_1.op_1",)
    return initiative(
        SolutionCategory.LLM_RAG,
        id=key,
        process_id="proc_1",
        operation_ids=list(ops),
        reduction_potential=[
            {"operation_id": op, "level": "medium", "justification": "x"} for op in ops
        ],
        **updates,
    )


def item(key: str, value: Value, *, requires_input: bool = False) -> Assumption:
    parameter = key.rsplit(".", 1)[-1]
    scope = PARAMETER_SCOPES[parameter]
    parts = key.split(".")  # see the key format in domain/assumptions.py
    target: str | None = None
    if scope in ("process", "initiative"):
        target = parts[1]
    elif scope == "operation":
        target = ".".join(parts[1:-1])
    elif scope == "initiative_operation":
        target = initiative_operation_target(parts[1], ".".join(parts[2:-1]))
    low, base, high = (value, value, value) if isinstance(value, str) else value
    return Assumption(
        key=key,
        scope=scope,
        target_id=target,
        unit="—",
        values=Triple(conservative=Decimal(low), base=Decimal(base), optimistic=Decimal(high)),
        source=AssumptionSource.USER,
        requires_input=requires_input,
        updated_by="expert",
        updated_at=NOW,
    )


def assumptions(values: dict[str, Value], version: int = 4) -> AssumptionSet:
    return AssumptionSet(
        run_id="run_1",
        version=version,
        items=[item(key, value) for key, value in values.items()],
        created_by="expert",
        created_at=NOW,
    )


def io(op: str, parameter: str, key: str = "ini_1") -> str:
    return initiative_operation_key(key, op, parameter)


def ini(parameter: str, key: str = "ini_1") -> str:
    return initiative_key(key, parameter)


def one_operation(
    horizon: str = "1", overrides: dict[str, Value] | None = None
) -> dict[str, Value]:
    """Example 1 in the base column: 1 000 cases, 0.5 h, reduction 0.4, adoption 0.5."""
    op = "proc_1.op_1"
    values: dict[str, Value] = {
        global_key("hourly_cost"): ("900", "1000", "1100"),
        global_key("overhead_ratio"): ("0.1", "0.2", "0.3"),
        global_key("fte_hours_per_year"): "2000",
        global_key("horizon_years"): horizon,
        process_key("proc_1", "volume_per_year"): ("700", "1000", "1300"),
        operation_key(op, "time_hours"): ("0.4", "0.5", "0.6"),
        operation_key(op, "case_share"): "1",
        io(op, "reduction_share"): ("0.3", "0.4", "0.5"),
        io(op, "review_hours"): ("0.1", "0.05", "0.01"),
        io(op, "variable_cost_per_exec"): ("3", "2", "1"),
        ini("capex"): ("60000", "50000", "40000"),
        ini("fixed_opex_per_year"): ("12000", "10000", "8000"),
        ini("adoption_y1"): ("0.4", "0.5", "0.6"),
        ini("adoption_y2"): "0.9",
        ini("adoption_y3"): "1",
    }
    return values | (overrides or {})


def solo(
    values: dict[str, Value], scenario: Scenario = Scenario.BASE, *initiatives: Initiative
) -> Effect:
    found = compute_solo(model(), list(initiatives) or [rag()], assumptions(values), scenario)
    return found.per_initiative[0].solo


def d(value: str) -> Decimal:
    return Decimal(value)


# --- hand-computed examples ----------------------------------------------------------------


def test_example_1_one_operation_one_year() -> None:
    # N = 1000 × 1 = 1000; per execution 0.5 × 0.5 × 0.4 − 0.5 × 0.05 = 0.075 h → H = 75 h.
    # C = 1000 × (1 + 0.2) = 1200 → M = 90 000. E = 10 000 + 1000 × 0.5 × 2 = 11 000.
    # Net = 90 000 − 11 000 − 50 000 = 29 000; ROI = 29 000 / 61 000.
    # Payback: −50 000 + m × 79 000 / 12 ≥ 0 → m = 8.
    effect = solo(one_operation())
    assert effect.hours == [d("75")]
    assert effect.fte == [d("0.0375")]
    assert effect.money == [d("90000")]
    assert effect.opex == [d("11000")]
    assert effect.capex == d("50000")
    assert effect.net == [d("29000")]
    assert effect.cum_net == d("29000")
    assert effect.roi is not None and round(effect.roi, 6) == d("0.475410")
    assert effect.payback_months == 8


def test_example_2_two_operations_three_years_process_rate() -> None:
    a, b = "proc_1.op_1", "proc_1.op_2"
    values = one_operation(
        "3",
        {
            global_key("overhead_ratio"): "0.3",
            global_key("fte_hours_per_year"): "1970",
            process_key("proc_1", "volume_per_year"): ("1", "2000", "3"),
            process_key("proc_1", "hourly_cost_override"): ("1", "1500", "3"),
            operation_key(a, "time_hours"): "2",
            io(a, "reduction_share"): "0.5",
            io(a, "review_hours"): "0.1",
            io(a, "variable_cost_per_exec"): "10",
            operation_key(b, "time_hours"): "1",
            operation_key(b, "case_share"): ("1", "0.25", "1"),
            io(b, "reduction_share"): "0.8",
            io(b, "review_hours"): "0",
            io(b, "variable_cost_per_exec"): "0",
            ini("capex"): ("1", "300000", "3"),
            ini("fixed_opex_per_year"): ("1", "60000", "3"),
            ini("adoption_y1"): "0.5",
            ini("adoption_y2"): "0.9",
            ini("adoption_y3"): "1",
        },
    )
    # N_a = 2000, N_b = 2000 × 0.25 = 500; C = 1500 × 1.3 = 1950 (the process rate, not 1000).
    # y1, a = 0.5: H = 2000 × (2×0.5×0.5 − 0.5×0.1) + 500 × (1×0.5×0.8) = 900 + 200 = 1100.
    # y2, a = 0.9: H = 2000 × (0.9 − 0.09) + 500 × 0.72 = 1620 + 360 = 1980.
    # y3, a = 1.0: H = 2000 × (1.0 − 0.1) + 500 × 0.8 = 1800 + 400 = 2200.
    # E = 60 000 + 2000 × a × 10: 70 000, 78 000, 80 000.
    # M = H × 1950: 2 145 000, 3 861 000, 4 290 000.
    # Net: 2 145 000 − 70 000 − 300 000 = 1 775 000; 3 783 000; 4 210 000 → Σ 9 768 000.
    # ROI = (10 296 000 − 300 000 − 228 000) / 528 000 = 18.5.
    # Payback: −300 000 × 12 + m × 2 075 000 ≥ 0 → m = 2.
    effect = solo(values, Scenario.BASE, rag("ini_1", a, b))
    assert effect.hours == [d("1100"), d("1980"), d("2200")]
    assert [round(f, 6) for f in effect.fte] == [d("0.558376"), d("1.005076"), d("1.116751")]
    assert effect.money == [d("2145000"), d("3861000"), d("4290000")]
    assert effect.opex == [d("70000"), d("78000"), d("80000")]
    assert effect.net == [d("1775000"), d("3783000"), d("4210000")]
    assert effect.cum_net == d("9768000")
    assert effect.roi == d("18.5")
    assert effect.payback_months == 2


def test_example_3_negative_effect_in_the_conservative_scenario() -> None:
    op = "proc_1.op_1"
    values = one_operation(
        "2",
        {
            global_key("hourly_cost"): "1000",
            global_key("overhead_ratio"): "0",
            process_key("proc_1", "volume_per_year"): ("100", "1000", "1000"),
            operation_key(op, "time_hours"): ("0.1", "1", "1"),
            io(op, "reduction_share"): ("0.1", "0.5", "0.5"),
            io(op, "review_hours"): ("0.5", "0", "0"),
            io(op, "variable_cost_per_exec"): ("0", "0", "0"),
            ini("capex"): ("0", "0", "0"),
            ini("fixed_opex_per_year"): ("1000", "0", "0"),
            ini("adoption_y1"): ("1", "1", "1"),
            ini("adoption_y2"): ("1", "1", "1"),
        },
    )
    # Review costs more than it saves: H = 100 × (0.1 × 1 × 0.1 − 1 × 0.5) = −49 h a year.
    # M = −49 000; E = 1000; Net = −50 000 a year, Σ −100 000. Nothing is clipped at zero.
    # ROI = (−98 000 − 0 − 2000) / (0 + 2000) = −50. It never pays back.
    effect = solo(values, Scenario.CONSERVATIVE)
    assert effect.hours == [d("-49"), d("-49")]
    assert effect.fte == [d("-0.0245"), d("-0.0245")]
    assert effect.money == [d("-49000"), d("-49000")]
    assert effect.net == [d("-50000"), d("-50000")]
    assert effect.cum_net == d("-100000")
    assert effect.roi == d("-50")
    assert effect.payback_months is None
    assert solo(values, Scenario.BASE).hours == [d("500"), d("500")]  # the other column


# --- ROI and payback edges -----------------------------------------------------------------


def test_roi_is_undefined_without_any_costs_and_payback_is_immediate() -> None:
    no_costs = one_operation(
        overrides={
            ini("capex"): "0",
            ini("fixed_opex_per_year"): "0",
            io("proc_1.op_1", "variable_cost_per_exec"): "0",
        }
    )
    effect = solo(no_costs)
    assert effect.roi is None
    assert effect.payback_months == 1


def test_payback_can_fall_in_a_later_year() -> None:
    # N = 100, t = 1.2, reduction 1, no review: H = 120 × a → 60 h, then 120 h; C = 1000.
    # −120 000 + 60 000 by the end of year 1; then 10 000 a month → month 12 + 6 = 18.
    op = "proc_1.op_1"
    values = one_operation(
        "2",
        {
            global_key("hourly_cost"): "1000",
            global_key("overhead_ratio"): "0",
            process_key("proc_1", "volume_per_year"): "100",
            operation_key(op, "time_hours"): "1.2",
            io(op, "reduction_share"): "1",
            io(op, "review_hours"): "0",
            io(op, "variable_cost_per_exec"): "0",
            ini("capex"): "120000",
            ini("fixed_opex_per_year"): "0",
            ini("adoption_y1"): "0.5",
            ini("adoption_y2"): "1",
        },
    )
    assert solo(values).payback_months == 18
    too_dear = values | {ini("capex"): "1000000"}
    assert solo(too_dear).payback_months is None


# --- inputs --------------------------------------------------------------------------------


def test_rejected_initiatives_are_left_out_and_order_is_by_id() -> None:
    values = one_operation()
    values |= {k.replace("ini_1", "ini_0"): v for k, v in values.items() if "ini_1" in k}
    rejected = rag("ini_2", review=ReviewState(status=ReviewStatus.REJECTED))
    found = compute_solo(model(), [rag("ini_1"), rejected, rag("ini_0")], assumptions(values),
                         Scenario.BASE)  # fmt: skip
    assert [e.initiative_id for e in found.per_initiative] == ["ini_0", "ini_1"]
    assert found.layer == KnowledgeLayer.ASSUMPTION and found.assumptions_version == 4
    assert found.scenario == Scenario.BASE


def test_missing_assumptions_are_listed_not_taken_as_zero() -> None:
    values = one_operation()
    del values[ini("capex")]
    del values[operation_key("proc_1.op_1", "time_hours")]
    with pytest.raises(AssumptionValidationError) as error:
        solo(values)
    assert list(error.value.problems) == [ini("capex"), "op.proc_1.op_1.time_hours"]


def test_zero_working_time_of_an_fte_is_refused() -> None:
    with pytest.raises(AssumptionValidationError, match="fte_hours_per_year"):
        solo(one_operation(overrides={global_key("fte_hours_per_year"): "0"}))


# --- warnings ------------------------------------------------------------------------------


def test_warnings_for_inputs_still_required_and_scenarios_out_of_order() -> None:
    values = one_operation(
        overrides={
            ini("capex"): ("40000", "50000", "60000"),  # conservative cheaper: out of order
            operation_key("proc_1.op_1", "time_hours"): ("0.6", "0.5", "0.4"),  # also reversed
        }
    )
    found_set = assumptions(values)
    flagged = [
        i.model_copy(update={"requires_input": True})
        if i.key in (process_key("proc_1", "volume_per_year"), "op.proc_1.op_1.case_share")
        else i
        for i in found_set.items
    ]
    unused = item(process_key("proc_9", "volume_per_year"), "1", requires_input=True)
    found_set = found_set.model_copy(update={"items": [*flagged, unused]})
    warnings = compute_solo(model(), [rag()], found_set, Scenario.BASE).warnings
    assert warnings == [
        "Объёмы процессов не заданы: используются заглушки («Выдача разрешения»).",
        "Доля случаев после точек решения не задана (операций: 1): "
        "использовано значение по умолчанию.",
        f"Сценарии не по порядку: {ini('capex')} — значения должны идти от "
        "консервативного к оптимистичному.",
        "Сценарии не по порядку: op.proc_1.op_1.time_hours — значения должны идти от "
        "консервативного к оптимистичному.",
    ]


def test_defaults_from_the_profile_are_enough_to_compute() -> None:
    initiatives = [rag("ini_1", "proc_1.op_1", "proc_1.op_2")]
    defaults = build_assumptions(model(), initiatives, run_id="run_1", now=NOW)
    found = compute_solo(model(), initiatives, defaults, Scenario.BASE)
    (economics,) = found.per_initiative
    assert len(economics.solo.hours) == 3  # the profile horizon
    assert economics.attributed is None and not economics.in_portfolio  # portfolio: T065
    assert any("используются заглушки" in w for w in found.warnings)


def test_assumption_keys_name_exactly_what_the_effect_is_computed_from() -> None:
    values = one_operation()
    found = compute_solo(model(), [rag()], assumptions(values), Scenario.BASE)
    (economics,) = found.per_initiative
    keys = economics.assumption_keys
    assert "hourly_cost" in keys and ini("adoption_y2") not in keys  # a one-year horizon
    assert solo({k: v for k, v in values.items() if k in keys}) == economics.solo
    rate = process_key("proc_1", "hourly_cost_override")
    priced = compute_solo(model(), [rag()], assumptions(values | {rate: "1500"}), Scenario.BASE)
    assert rate in priced.per_initiative[0].assumption_keys
    assert "hourly_cost" not in priced.per_initiative[0].assumption_keys
