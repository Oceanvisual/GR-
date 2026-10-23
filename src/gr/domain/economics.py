"""Economic effect of initiatives: computed from the assumptions, never stored (data-model §7).

Money is in the analysis currency, hours per year; lists hold one value per year of the
horizon (index 0 is year 1). Values keep full precision: rounding belongs to display.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.enums import KnowledgeLayer, Scenario


class Effect(DomainModel):
    hours: list[Decimal]
    fte: list[Decimal]
    money: list[Decimal]
    opex: list[Decimal]
    capex: Decimal
    net: list[Decimal]
    cum_net: Decimal
    roi: Decimal | None = None  # None: no costs at all, ROI «не определён»
    payback_months: int | None = Field(default=None, ge=1)  # None: «не окупается за горизонт»

    @model_validator(mode="after")
    def _same_years(self) -> Self:
        years = {len(self.hours), len(self.fte), len(self.money), len(self.opex), len(self.net)}
        if len(years) != 1 or not 1 <= years.pop() <= 3:
            raise ValueError("per-year values must cover the same horizon of 1–3 years")
        return self


class InitiativeEconomics(DomainModel):
    initiative_id: str
    solo: Effect
    assumption_keys: list[str] = []  # the assumptions the effect was computed from (FR-070)
    # The portfolio part (attribution, membership) comes with build_portfolio (T065).
    attributed: Effect | None = None
    in_portfolio: bool = False
    excluded_reason: str | None = None


class Overlap(DomainModel):
    """An operation that several initiatives of the portfolio change (FR-054)."""

    operation_id: str
    initiative_ids: list[str] = Field(min_length=2)
    combined_reduction: list[Decimal]  # R_y = 1 − Π (1 − g_i), one per year, ≤ 1


class PortfolioEconomics(DomainModel):
    initiative_ids: list[str]  # the members, by id
    total: Effect | None = None  # the sum of attributed effects; None for an empty portfolio


class EconomicsResult(DomainModel):
    scenario: Scenario
    layer: Literal[KnowledgeLayer.ASSUMPTION] = KnowledgeLayer.ASSUMPTION
    assumptions_version: int = Field(ge=1)
    per_initiative: list[InitiativeEconomics] = []
    portfolio: PortfolioEconomics | None = None  # None until build_portfolio (T065)
    overlaps: list[Overlap] = []
    warnings: list[str] = []
