"""Priority Score configuration and result (data-model.md §6)."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel, VersionedRecord
from gr.domain.enums import DurationBucket, KnowledgeLayer, LegalGate, Level5, PriorityGroup

Criterion = Literal["effect", "feasibility", "data_availability", "integration", "risk", "duration"]
CRITERIA: tuple[Criterion, ...] = (
    "effect",
    "feasibility",
    "data_availability",
    "integration",
    "risk",
    "duration",
)

UnitDecimal = Annotated[Decimal, Field(ge=0, le=1)]


def _default_weights() -> dict[Criterion, Decimal]:
    return {
        "effect": Decimal(30),
        "feasibility": Decimal(20),
        "data_availability": Decimal(15),
        "integration": Decimal(10),
        "risk": Decimal(15),
        "duration": Decimal(10),
    }


def _default_level_map() -> dict[Level5, Decimal]:
    return {
        Level5.VERY_LOW: Decimal(0),
        Level5.LOW: Decimal("0.25"),
        Level5.MEDIUM: Decimal("0.5"),
        Level5.HIGH: Decimal("0.75"),
        Level5.VERY_HIGH: Decimal(1),
    }


def _default_duration_map() -> dict[DurationBucket, Decimal]:
    return {
        DurationBucket.LT_3M: Decimal("1.0"),
        DurationBucket.M3_6: Decimal("0.7"),
        DurationBucket.M6_12: Decimal("0.4"),
        DurationBucket.GT_12M: Decimal("0.1"),
    }


class PriorityConfig(DomainModel):
    weights: dict[Criterion, Decimal] = Field(default_factory=_default_weights)
    level_map: dict[Level5, Decimal] = Field(default_factory=_default_level_map)
    duration_map: dict[DurationBucket, Decimal] = Field(default_factory=_default_duration_map)
    effect_ref: Decimal = Field(default=Decimal(5_000_000), gt=0)
    tech_guard_penalty: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    cutoff_score: Decimal = Field(default=Decimal(35), ge=0, le=100)
    ease_threshold: Decimal = Field(default=Decimal("0.6"), ge=0, le=1)
    effect_threshold: Decimal = Field(default=Decimal("0.4"), ge=0, le=1)

    @model_validator(mode="after")
    def _complete_and_bounded(self) -> Self:
        if set(self.weights) != set(CRITERIA):
            raise ValueError(f"weights must cover exactly {list(CRITERIA)}")
        if any(w < 0 for w in self.weights.values()) or sum(self.weights.values()) <= 0:
            raise ValueError("weights must be >= 0 with a positive sum")
        for name, mapping, keys in (
            ("level_map", self.level_map, set(Level5)),
            ("duration_map", self.duration_map, set(DurationBucket)),
        ):
            if set(mapping) != keys:
                raise ValueError(f"{name} must cover every value")
            if any(not 0 <= v <= 1 for v in mapping.values()):
                raise ValueError(f"{name} values must be within [0; 1]")
        return self


class PriorityConfigVersion(VersionedRecord):
    config: PriorityConfig


class CriterionScore(DomainModel):
    raw: str | Decimal | None
    normalized: UnitDecimal
    weight: Decimal = Field(ge=0)
    contribution: Decimal


class PriorityResult(DomainModel):
    initiative_id: str
    criteria: dict[Criterion, CriterionScore]
    score: Decimal = Field(ge=0, le=100)
    ease: UnitDecimal
    group: PriorityGroup
    group_rule: int = Field(ge=1)
    group_reason: str
    gate: LegalGate
    rank: int | None = Field(default=None, ge=1)
    layer: Literal[KnowledgeLayer.HYPOTHESIS] = KnowledgeLayer.HYPOTHESIS
    assumptions_version: int = Field(ge=1)
    priority_config_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _blocked_unranked(self) -> Self:
        if self.gate == LegalGate.BLOCKED:
            if self.rank is not None:
                raise ValueError("a blocked initiative is not ranked")
            if self.group != PriorityGroup.NOT_RECOMMENDED:
                raise ValueError("a blocked initiative is always not_recommended")
        return self
