"""S9: the priority group of each initiative (FR-063, FR-064, spec «Правила группировки», T048).

The rules apply in order and the first that holds decides; every initiative gets exactly one
group, the number of the rule and a reason with the numbers it was decided on:
1. legal gate blocked → Not Recommended;
2. solo cumulative base effect ≤ 0, or score below the cutoff → Not Recommended;
3. the act has to change → Complex Initiatives;
4. ease ≥ the ease threshold → Quick Wins;
5. normalised effect ≥ the effect threshold → Strategic Projects;
6. otherwise → Complex Initiatives.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from gr.analysis.scoring import ScoreCard, rank
from gr.domain.enums import LegalGate, PriorityGroup
from gr.domain.priority import PriorityConfig, PriorityResult


def _number(value: Decimal, digits: int = 2) -> str:
    """A number inside a reason, in the notation of the screens."""
    rounded = value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
    return format(rounded.normalize(), "f").replace(".", ",")


def assign_group(card: ScoreCard, config: PriorityConfig) -> tuple[PriorityGroup, int, str]:
    """(group, rule number, reason) by the first rule that holds."""
    effect = card.criteria["effect"].normalized
    if card.gate == LegalGate.BLOCKED:
        return PriorityGroup.NOT_RECOMMENDED, 1, "Недопустимо: юридическое ограничение."
    if card.effect <= 0:
        return (
            PriorityGroup.NOT_RECOMMENDED,
            2,
            "Автономный эффект за горизонт (базовый сценарий) не положительный: внедрение "
            "не окупается.",
        )
    if card.score < config.cutoff_score:
        return (
            PriorityGroup.NOT_RECOMMENDED,
            2,
            f"Балл {_number(card.score, 1)} ниже порога отсечения "
            f"{_number(config.cutoff_score, 1)}.",
        )
    if card.gate == LegalGate.REQUIRES_REGULATORY_CHANGE:
        return (
            PriorityGroup.COMPLEX,
            3,
            "Требует изменения НПА: внедрение зависит от нормотворчества.",
        )
    if card.ease >= config.ease_threshold:
        return (
            PriorityGroup.QUICK_WIN,
            4,
            f"Лёгкость внедрения {_number(card.ease)} ≥ порога {_number(config.ease_threshold)}.",
        )
    if effect >= config.effect_threshold:
        return (
            PriorityGroup.STRATEGIC,
            5,
            f"Нормированный эффект {_number(effect)} ≥ порога значимого эффекта "
            f"{_number(config.effect_threshold)}.",
        )
    return (
        PriorityGroup.COMPLEX,
        6,
        f"Высокая сложность при умеренном эффекте: лёгкость {_number(card.ease)} < "
        f"{_number(config.ease_threshold)}, эффект {_number(effect)} < "
        f"{_number(config.effect_threshold)}.",
    )


def build_priorities(
    cards: Sequence[ScoreCard],
    config: PriorityConfig,
    *,
    assumptions_version: int,
    config_version: int,
) -> list[PriorityResult]:
    """Score, rank and group of every card, best first; blocked ones last, by id."""
    ranks = rank(cards)
    results = []
    for card in cards:
        group, rule, reason = assign_group(card, config)
        results.append(
            PriorityResult(
                initiative_id=card.initiative_id,
                criteria=card.criteria,
                score=card.score,
                ease=card.ease,
                group=group,
                group_rule=rule,
                group_reason=reason,
                gate=card.gate,
                rank=ranks[card.initiative_id],
                assumptions_version=assumptions_version,
                priority_config_version=config_version,
            )
        )
    return sorted(results, key=lambda r: (r.rank is None, r.rank or 0, r.initiative_id))
