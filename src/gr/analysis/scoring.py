"""S9: Priority Score of each initiative and its rank (FR-060…FR-062, FR-065, data-model §6).

Each criterion is normalised to [0; 1] on its own: the effect against an absolute reference
(the solo cumulative effect of the base scenario, so a score never depends on the other
initiatives, FR-061), the qualitative levels of S6 by the level map, integration, risk and
duration inverted. score = 100 × Σ w·s / Σ w, rounded half to even to 0.1; the contribution of
each criterion keeps full precision, so the breakdown adds up to the score. The group comes
from grouping.py (T048); a blocked initiative gets no rank (FR-063).
"""

import tomllib
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import cache
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from gr.domain.economics import EconomicsResult
from gr.domain.enums import LegalGate, Level5, Scenario
from gr.domain.initiative import Initiative
from gr.domain.priority import CRITERIA, Criterion, CriterionScore, PriorityConfig

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TENTH = Decimal("0.1")


class ScoreCard(BaseModel):
    """The scored criteria of one initiative, before grouping."""

    model_config = ConfigDict(frozen=True)

    initiative_id: str
    criteria: dict[Criterion, CriterionScore]
    score: Decimal
    ease: Decimal
    effect: Decimal  # cumulative base solo effect over the horizon (the tie-break of FR-065)
    gate: LegalGate


@cache
def load_priority_config(path: Path | None = None) -> PriorityConfig:
    """Bundled defaults, or an explicit TOML file; numbers are read as exact decimals."""
    text = (
        path.read_text("utf-8")
        if path
        else resources.files("gr.config").joinpath("priority_default.toml").read_text("utf-8")
    )
    return PriorityConfig.model_validate(tomllib.loads(text, parse_float=Decimal))


def _clamp(value: Decimal) -> Decimal:
    return min(max(value, _ZERO), _ONE)


def _normalised(
    initiative: Initiative, effect: Decimal, config: PriorityConfig
) -> dict[Criterion, tuple[str | Decimal, Decimal]]:
    """Criterion → (raw value, normalised value)."""
    ratings = initiative.ratings
    level = config.level_map
    penalty = (
        config.tech_guard_penalty
        if initiative.tech_guard and initiative.tech_guard.requires_justification
        else _ZERO
    )
    worst = max(ratings.legal_risk.level, ratings.operational_risk.level, key=lambda x: level[x])
    return {
        "effect": (effect, _clamp(effect / config.effect_ref)),
        "feasibility": (
            ratings.feasibility.level.value,
            max(_ZERO, level[ratings.feasibility.level] - penalty),
        ),
        "data_availability": (
            ratings.data_availability.level.value,
            level[ratings.data_availability.level],
        ),
        "integration": (
            ratings.integration_complexity.level.value,
            _ONE - level[ratings.integration_complexity.level],
        ),
        "risk": (Level5(worst).value, _ONE - level[worst]),
        "duration": (ratings.duration.bucket.value, config.duration_map[ratings.duration.bucket]),
    }


def score_card(initiative: Initiative, effect: Decimal, config: PriorityConfig) -> ScoreCard:
    """The score of one initiative from its own ratings and solo effect only (FR-061)."""
    if initiative.gate is None:
        raise ValueError(f"initiative {initiative.id} has no legal gate yet (S7)")
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        values = _normalised(initiative, effect, config)
        total = sum(config.weights.values(), _ZERO)
        criteria = {
            name: CriterionScore(
                raw=values[name][0],
                normalized=values[name][1],
                weight=config.weights[name],
                contribution=100 * config.weights[name] * values[name][1] / total,
            )
            for name in CRITERIA
        }
        exact = sum((c.contribution for c in criteria.values()), _ZERO)
        ease = (
            criteria["feasibility"].normalized
            + criteria["integration"].normalized
            + criteria["duration"].normalized
        ) / 3
    return ScoreCard(
        initiative_id=initiative.id,
        criteria=criteria,
        score=exact.quantize(_TENTH, rounding=ROUND_HALF_EVEN),
        ease=ease,
        effect=effect,
        gate=initiative.gate.status,
    )


def score_initiatives(
    initiatives: Sequence[Initiative], economics: EconomicsResult, config: PriorityConfig
) -> list[ScoreCard]:
    """Cards of every initiative with solo economics (rejected ones have none), by id."""
    if economics.scenario != Scenario.BASE:
        raise ValueError("the score uses the base scenario (FR-061)")
    effects = {e.initiative_id: e.solo.cum_net for e in economics.per_initiative}
    return sorted(
        (score_card(i, effects[i.id], config) for i in initiatives if i.id in effects),
        key=lambda card: card.initiative_id,
    )


def rank(cards: Sequence[ScoreCard]) -> dict[str, int | None]:
    """1 is the best: score, then effect, then id (FR-065); blocked ones are not ranked."""
    ranked = sorted(
        (card for card in cards if card.gate != LegalGate.BLOCKED),
        key=lambda card: (-card.score, -card.effect, card.initiative_id),
    )
    ranks: dict[str, int | None] = {card.initiative_id: None for card in cards}
    ranks.update({card.initiative_id: n for n, card in enumerate(ranked, 1)})
    return ranks
