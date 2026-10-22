"""S10: portfolio, combination and attribution (FR-054, FR-055, data-model §7 step 3, T065).

Membership (the same in every scenario: it comes from the base-scenario priorities): not
rejected by the expert, gate not blocked, group not Not Recommended, and from each group of
alternatives (connected by `alternative` relations) only the best ranked of those still
eligible — a blocked or weak alternative does not leave the group empty.

On an operation o changed by several members their reductions combine multiplicatively,
R_y = 1 − Π (1 − g_{i,y}) ≤ 1, and each member gets the share g_i / Σ g of it:
H^attr_{i,o,y} = N_o × (t_o × R_y × g_{i,y} / Σ g − a_{i,y} × c_{i,o}) (0 gross when Σ g = 0).
Review time and costs stay with their initiative. The portfolio total is the sum of the
attributed effects, never of the solo ones.
"""

from collections import defaultdict
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from gr.analysis.economics import InitiativeTerms, Terms, assemble, effect_of, read_terms
from gr.analysis.relations import alternative_components
from gr.domain.assumptions import AssumptionSet
from gr.domain.economics import EconomicsResult, Effect, Overlap, PortfolioEconomics
from gr.domain.enums import LegalGate, PriorityGroup, ReviewStatus
from gr.domain.initiative import Initiative
from gr.domain.priority import PriorityResult

_ZERO = Decimal(0)
_ONE = Decimal(1)


def membership(
    initiatives: Sequence[Initiative], priorities: Sequence[PriorityResult]
) -> dict[str, str | None]:
    """Initiative id → None when it is in the portfolio, else why it is not."""
    by_id = {p.initiative_id: p for p in priorities}
    active = [i for i in initiatives if i.review.status != ReviewStatus.REJECTED]
    reasons: dict[str, str | None] = {}
    eligible: set[str] = set()
    for item in active:
        found = by_id.get(item.id)
        if found is None:
            reasons[item.id] = "Нет оценки приоритета."
        elif found.gate == LegalGate.BLOCKED:
            reasons[item.id] = "Недопустимо: юридическое ограничение."
        elif found.group == PriorityGroup.NOT_RECOMMENDED:
            reasons[item.id] = f"Not Recommended: {found.group_reason}"
        else:
            eligible.add(item.id)
    for component in alternative_components(initiatives):
        candidates = [key for key in component if key in eligible]
        if len(candidates) < 2:
            continue
        best = min(candidates, key=lambda key: (by_id[key].rank or 0, key))
        for key in candidates:
            if key != best:
                eligible.discard(key)
                reasons[key] = f"Альтернатива инициативе {best} с лучшим баллом."
    reasons.update(dict.fromkeys(eligible))
    return dict(sorted(reasons.items()))


def _attributed_hours(
    members: dict[str, InitiativeTerms], horizon: int
) -> tuple[dict[str, list[Decimal]], list[Overlap]]:
    by_operation: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for key, terms in members.items():
        for n, op in enumerate(terms.operations):
            by_operation[op.operation_id].append((key, n))
    hours = {key: [_ZERO] * horizon for key in members}
    overlaps: list[Overlap] = []
    for operation_id, users in sorted(by_operation.items()):
        combined: list[Decimal] = []
        for year in range(horizon):
            shares = {
                key: members[key].adoption[year] * members[key].operations[n].reduction
                for key, n in users
            }
            untouched = _ONE
            for share in shares.values():
                untouched *= _ONE - share
            reduction = _ONE - untouched
            total = sum(shares.values(), _ZERO)
            combined.append(reduction)
            for key, n in users:
                op = members[key].operations[n]
                gross = op.time * reduction * shares[key] / total if total else _ZERO
                review = members[key].adoption[year] * op.review
                hours[key][year] += op.executions * (gross - review)
        if len(users) > 1:
            overlaps.append(
                Overlap(
                    operation_id=operation_id,
                    initiative_ids=sorted({key for key, _ in users}),
                    combined_reduction=combined,
                )
            )
    return hours, overlaps


def _total(effects: list[Effect], fte_hours: Decimal, horizon: int) -> Effect:
    def per_year(name: str) -> list[Decimal]:
        return [sum((getattr(e, name)[y] for e in effects), _ZERO) for y in range(horizon)]

    return assemble(
        per_year("hours"),
        per_year("money"),
        per_year("opex"),
        sum((e.capex for e in effects), _ZERO),
        fte_hours,
    )


def build_portfolio(
    initiatives: Sequence[Initiative],
    assumptions: AssumptionSet,
    solo: EconomicsResult,
    priorities: Sequence[PriorityResult],
) -> EconomicsResult:
    """`solo` of one scenario completed with membership, attributed effects, the portfolio
    total and the overlaps. `priorities` are those of the base scenario (FR-061)."""
    terms: Terms = read_terms(initiatives, assumptions, solo.scenario)
    reasons = membership(initiatives, priorities)
    members = {key: terms.initiatives[key] for key, why in reasons.items() if why is None}
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        hours, overlaps = _attributed_hours(members, terms.horizon)
        attributed = {
            key: effect_of(hours[key], found, terms.fte_hours) for key, found in members.items()
        }
        total = (
            _total([attributed[key] for key in sorted(attributed)], terms.fte_hours, terms.horizon)
            if attributed
            else None
        )
    per_initiative = [
        item.model_copy(
            update={
                "attributed": attributed.get(item.initiative_id),
                "in_portfolio": item.initiative_id in attributed,
                "excluded_reason": reasons.get(item.initiative_id),
            }
        )
        for item in solo.per_initiative
    ]
    return solo.model_copy(
        update={
            "per_initiative": per_initiative,
            "portfolio": PortfolioEconomics(initiative_ids=sorted(attributed), total=total),
            "overlaps": overlaps,
        }
    )
