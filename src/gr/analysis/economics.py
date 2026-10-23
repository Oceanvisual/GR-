"""S8: the solo economic effect of each initiative (FR-053, spec «Экономические формулы», T044).

For operation o of process p and initiative i in year y of the horizon:
N_o = V_p × q_o; g = a_y × r_{i,o}; H_{i,y} = Σ_o N_o × (t_o × g − a_y × c_{i,o});
M = H × h_p × (1 + k); E = F_i + Σ_o N_o × a_y × u_{i,o}; Net = M − E − K in year 1.

Pure and deterministic: Decimal at 28 digits, no rounding (display rounds), no LLM. Every
value comes from the scenario column of the assumption set; a missing key is an error, never
a silent zero. Negative effects (review costing more than the time saved, costs above the
savings) are reported as they are. The result does not depend on the order of the input.
The terms read here also feed the portfolio (portfolio.py), which only changes the hours.
"""

from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class OperationTerms:
    """One operation of an initiative: N_o, t_o, r_{i,o}, c_{i,o}, u_{i,o}."""

    operation_id: str
    executions: Decimal
    time: Decimal
    reduction: Decimal
    review: Decimal
    variable: Decimal


@dataclass(frozen=True)
class InitiativeTerms:
    initiative_id: str
    rate: Decimal  # full hourly cost of the process: h_p × (1 + k)
    adoption: tuple[Decimal, ...]  # a_y, one per year of the horizon
    capex: Decimal
    fixed: Decimal
    operations: tuple[OperationTerms, ...]
    keys: tuple[str, ...]  # the assumptions read for this initiative

    def opex(self) -> list[Decimal]:
        return [
            self.fixed + sum((op.executions * a * op.variable for op in self.operations), _ZERO)
            for a in self.adoption
        ]

    def solo_hours(self) -> list[Decimal]:
        return [
            sum(
                (op.executions * (op.time * a * op.reduction - a * op.review)
                 for op in self.operations),
                _ZERO,
            )
            for a in self.adoption
        ]  # fmt: skip


@dataclass(frozen=True)
class Terms:
    """Everything the effects of one scenario are computed from."""

    scenario: Scenario
    horizon: int
    fte_hours: Decimal
    initiatives: dict[str, InitiativeTerms]  # not rejected by the expert
    used: dict[str, Assumption]


class _Values:
    """The assumption set read in one scenario; remembers what was used and what is missing."""

    def __init__(self, assumptions: AssumptionSet, scenario: Scenario) -> None:
        self.items = assumptions.by_key()
        self.scenario = scenario
        self.used: dict[str, Assumption] = {}
        self.problems: dict[str, str] = {}
        self.trace: list[str] = []  # keys read since the last reset: one initiative's inputs

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
        self.trace.append(key)
        return item.values.get(self.scenario)


def _initiative_terms(
    initiative: Initiative, values: _Values, horizon: int, hourly: Decimal, overhead: Decimal
) -> InitiativeTerms:
    key = initiative.id
    values.trace = []
    override = values.optional(process_key(initiative.process_id, "hourly_cost_override"))
    volume = values.get(process_key(initiative.process_id, "volume_per_year"))
    adoption = tuple(
        values.get(initiative_key(key, f"adoption_y{y}")) for y in range(1, horizon + 1)
    )
    capex = values.get(initiative_key(key, "capex"))
    fixed = values.get(initiative_key(key, "fixed_opex_per_year"))
    operations = tuple(
        OperationTerms(
            operation_id=op,
            executions=volume * values.get(operation_key(op, "case_share")),
            time=values.get(operation_key(op, "time_hours")),
            reduction=values.get(initiative_operation_key(key, op, "reduction_share")),
            review=values.get(initiative_operation_key(key, op, "review_hours")),
            variable=values.get(initiative_operation_key(key, op, "variable_cost_per_exec")),
        )
        for op in initiative.operation_ids
    )
    shared = [global_key(k) for k in ("horizon_years", "overhead_ratio", "fte_hours_per_year")]
    hourly_key = [] if override is not None else [global_key("hourly_cost")]
    return InitiativeTerms(
        initiative_id=key,
        rate=(hourly if override is None else override) * (1 + overhead),
        adoption=adoption,
        capex=capex,
        fixed=fixed,
        operations=operations,
        keys=tuple(dict.fromkeys([*shared, *hourly_key, *values.trace])),
    )


def read_terms(
    initiatives: Sequence[Initiative], assumptions: AssumptionSet, scenario: Scenario
) -> Terms:
    """The inputs of every initiative not rejected by the expert, in one scenario."""
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
        found = {
            i.id: _initiative_terms(i, values, horizon, hourly, overhead)
            for i in sorted(initiatives, key=lambda i: i.id)
            if i.review.status != ReviewStatus.REJECTED
        }
        if values.problems:
            raise AssumptionValidationError(values.problems)
    return Terms(scenario, horizon, fte_hours, found, values.used)


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


def assemble(
    hours: list[Decimal],
    money: list[Decimal],
    opex: list[Decimal],
    capex: Decimal,
    fte_hours: Decimal,
) -> Effect:
    """The indicators of an effect from its hours, money and costs per year."""
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
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


def effect_of(hours: list[Decimal], terms: InitiativeTerms, fte_hours: Decimal) -> Effect:
    """The effect of an initiative given its released hours per year (solo or attributed)."""
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        money = [h * terms.rate for h in hours]
        return assemble(hours, money, terms.opex(), terms.capex, fte_hours)


def _warnings(used: dict[str, Assumption], model: ProcessModel) -> list[str]:
    names = {p.id: p.name.value for p in model.processes}
    warnings: list[str] = []
    volumes: list[str] = []
    other: list[str] = []
    branched = 0
    for key, item in sorted(used.items()):
        if not item.requires_input:
            continue
        if item.parameter == "volume_per_year":
            volumes.append(f"«{names.get(item.target_id or '', item.target_id)}»")
        elif item.parameter == "case_share":
            branched += 1
        else:
            other.append(f"Требуется ввод значения: {key}.")
    if volumes:  # the wording of contracts/ui.md
        warnings.append(
            f"Объёмы процессов не заданы: используются заглушки ({', '.join(volumes)})."
        )
    warnings += other
    if branched:
        warnings.append(
            f"Доля случаев после точек решения не задана (операций: {branched}): "
            "использовано значение по умолчанию."
        )
    warnings += [
        f"Сценарии не по порядку: {key} — значения должны идти от консервативного к оптимистичному."
        for key, item in sorted(used.items())
        if out_of_order(item)
    ]
    return warnings


def out_of_order(item: Assumption) -> bool:
    """A scenario better than the next one for the effect (FR-057: a warning, not a refusal)."""
    low, base, high = item.values.as_tuple
    if item.parameter in _MORE_IS_BETTER:
        return not low <= base <= high
    if item.parameter in _LESS_IS_BETTER:
        return not low >= base >= high
    return False


def compute_solo(
    model: ProcessModel,
    initiatives: Sequence[Initiative],
    assumptions: AssumptionSet,
    scenario: Scenario,
) -> EconomicsResult:
    """The solo effect of every initiative not rejected by the expert, as if it were the only
    one (step 1 of data-model §7). `model` names the processes in the warnings."""
    terms = read_terms(initiatives, assumptions, scenario)
    per_initiative = [
        InitiativeEconomics(
            initiative_id=key,
            solo=effect_of(found.solo_hours(), found, terms.fte_hours),
            assumption_keys=list(found.keys),
        )
        for key, found in terms.initiatives.items()
    ]
    return EconomicsResult(
        scenario=scenario,
        assumptions_version=assumptions.version,
        per_initiative=per_initiative,
        warnings=_warnings(terms.used, model),
    )
