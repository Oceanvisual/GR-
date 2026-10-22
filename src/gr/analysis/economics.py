"""S8: the solo economic effect of each initiative (FR-053, spec «Экономические формулы», T044).

For operation o of process p and initiative i in year y of the horizon:
N_o = V_p × q_o; g = a_y × r_{i,o}; H_{i,y} = Σ_o N_o × (t_o × g − a_y × c_{i,o});
M = H × h_p × (1 + k); E = F_i + Σ_o N_o × a_y × u_{i,o}; Net = M − E − K in year 1.

Pure and deterministic: Decimal at 28 digits, no rounding (display rounds), no LLM. Every
value comes from the scenario column of the assumption set; a missing key is an error, never
a silent zero. Negative effects (review costing more than the time saved, costs above the
savings) are reported as they are. The result does not depend on the order of the input.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from gr.domain.assumptions import (
    Assumption,
    AssumptionSet,
    global_key,
    initiative_key,
    initiative_operation_key,
    operation_key,
    process_key,
)
from gr.domain.economics import EconomicsResult, Effect, InitiativeEconomics
from gr.domain.enums import ReviewStatus, Scenario
from gr.domain.initiative import Initiative
from gr.domain.process import ProcessModel

_MONTHS = 12
_ZERO = Decimal(0)
# Which way a parameter helps the effect: the conservative value should not beat the
# optimistic one (FR-057 warns, it does not refuse). Unlisted parameters have no direction.
_MORE_IS_BETTER = frozenset(
    {
        "hourly_cost",
        "hourly_cost_override",
        "overhead_ratio",
        "volume_per_year",
        "case_share",
        "time_hours",
        "reduction_share",
        "adoption_y1",
        "adoption_y2",
        "adoption_y3",
    }
)
_LESS_IS_BETTER = frozenset(
    {"review_hours", "variable_cost_per_exec", "capex", "fixed_opex_per_year"}
)


class AssumptionValidationError(ValueError):
    """Assumptions the calculation cannot use: key → reason (api.md: 400 validation_error)."""

    def __init__(self, problems: dict[str, str]) -> None:
        self.problems = dict(sorted(problems.items()))
        super().__init__("; ".join(f"{key}: {why}" for key, why in self.problems.items()))


class _Values:
    """The assumption set read in one scenario; remembers what was used and what is missing."""

    def __init__(self, assumptions: AssumptionSet, scenario: Scenario) -> None:
        self.items = assumptions.by_key()
        self.scenario = scenario
        self.used: dict[str, Assumption] = {}
        self.problems: dict[str, str] = {}

    def get(self, key: str) -> Decimal:
        value = self.optional(key)
        if value is None:
            self.problems[key] = "нет значения"
            return _ZERO
        return value

    def optional(self, key: str) -> Decimal | None:
        item = self.items.get(key)
        if item is None:
            return None
        self.used[key] = item
        return item.values.get(self.scenario)


def _payback(capex: Decimal, money: list[Decimal], opex: list[Decimal]) -> int | None:
    """First month whose cumulative cash flow −K + Σ (M_y − E_y) / 12 reaches zero.

    Kept in twelfths, so the comparison with zero is exact (no division by 12)."""
    balance = -capex * _MONTHS  # twelve times the cumulative flow before the current year
    for year, (inflow, outflow) in enumerate(zip(money, opex, strict=True)):
        flow = inflow - outflow
        for month in range(1, _MONTHS + 1):
            if balance + month * flow >= 0:
                return year * _MONTHS + month
        balance += _MONTHS * flow
    return None


def _solo(
    initiative: Initiative, values: _Values, horizon: int, rate: Decimal, fte_hours: Decimal
) -> Effect:
    key = initiative.id
    volume = values.get(process_key(initiative.process_id, "volume_per_year"))
    adoption = [values.get(initiative_key(key, f"adoption_y{y}")) for y in range(1, horizon + 1)]
    capex = values.get(initiative_key(key, "capex"))
    fixed = values.get(initiative_key(key, "fixed_opex_per_year"))
    hours = [_ZERO] * horizon
    opex = [fixed] * horizon
    for op in initiative.operation_ids:
        executions = volume * values.get(operation_key(op, "case_share"))
        time = values.get(operation_key(op, "time_hours"))
        reduction = values.get(initiative_operation_key(key, op, "reduction_share"))
        review = values.get(initiative_operation_key(key, op, "review_hours"))
        variable = values.get(initiative_operation_key(key, op, "variable_cost_per_exec"))
        for year, share in enumerate(adoption):
            hours[year] += executions * (time * share * reduction - share * review)
            opex[year] += executions * share * variable
    money = [h * rate for h in hours]
    net = [m - e for m, e in zip(money, opex, strict=True)]
    net[0] -= capex  # the implementation cost falls in year 1
    costs = capex + sum(opex, _ZERO)
    return Effect(
        hours=hours,
        fte=[h / fte_hours for h in hours],
        money=money,
        opex=opex,
        capex=capex,
        net=net,
        cum_net=sum(net, _ZERO),
        roi=(sum(money, _ZERO) - costs) / costs if costs else None,
        payback_months=_payback(capex, money, opex),
    )


def _warnings(used: dict[str, Assumption], model: ProcessModel) -> list[str]:
    names = {p.id: p.name.value for p in model.processes}
    warnings: list[str] = []
    branched = 0
    for key, item in sorted(used.items()):
        if not item.requires_input:
            continue
        if item.parameter == "volume_per_year":
            name = names.get(item.target_id or "", item.target_id)
            warnings.append(f"Объём процесса «{name}» не задан: используется модельная заглушка.")
        elif item.parameter == "case_share":
            branched += 1
        else:
            warnings.append(f"Требуется ввод значения: {key}.")
    if branched:
        warnings.append(
            f"Доля случаев после точек решения не задана (операций: {branched}): "
            "использовано значение по умолчанию."
        )
    for key, item in sorted(used.items()):
        low, base, high = item.values.as_tuple
        parameter = item.parameter
        if (parameter in _MORE_IS_BETTER and not low <= base <= high) or (
            parameter in _LESS_IS_BETTER and not low >= base >= high
        ):
            warnings.append(
                f"Сценарии не по порядку: {key} — консервативное значение выгоднее оптимистичного."
            )
    return warnings


def compute_solo(
    model: ProcessModel,
    initiatives: Sequence[Initiative],
    assumptions: AssumptionSet,
    scenario: Scenario,
) -> EconomicsResult:
    """The solo effect of every initiative not rejected by the expert, as if it were the only
    one (step 1 of data-model §7). `model` names the processes in the warnings."""
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        values = _Values(assumptions, scenario)
        horizon = int(values.get(global_key("horizon_years")))
        hourly = values.get(global_key("hourly_cost"))
        overhead = values.get(global_key("overhead_ratio"))
        fte_hours = values.get(global_key("fte_hours_per_year"))
        if global_key("fte_hours_per_year") in values.used and not fte_hours:
            values.problems["fte_hours_per_year"] = "фонд рабочего времени должен быть больше 0"
        if values.problems:
            raise AssumptionValidationError(values.problems)
        active = sorted(
            (i for i in initiatives if i.review.status != ReviewStatus.REJECTED),
            key=lambda i: i.id,
        )
        per_initiative = []
        for initiative in active:
            override = values.optional(process_key(initiative.process_id, "hourly_cost_override"))
            rate = (hourly if override is None else override) * (1 + overhead)
            solo = _solo(initiative, values, horizon, rate, fte_hours)
            per_initiative.append(InitiativeEconomics(initiative_id=initiative.id, solo=solo))
        if values.problems:
            raise AssumptionValidationError(values.problems)
        return EconomicsResult(
            scenario=scenario,
            assumptions_version=assumptions.version,
            per_initiative=per_initiative,
            warnings=_warnings(values.used, model),
        )
