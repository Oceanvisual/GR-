"""Editable economic assumptions in three scenarios (data-model.md §5)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.document import Citation
from gr.domain.enums import AssumptionSource, Scenario

AssumptionScope = Literal["global", "process", "operation", "initiative", "initiative_operation"]

# Parameter name (last segment of the key) → scope it belongs to.
PARAMETER_SCOPES: dict[str, AssumptionScope] = {
    "hourly_cost": "global",
    "overhead_ratio": "global",
    "fte_hours_per_year": "global",
    "horizon_years": "global",
    "volume_per_year": "process",
    "hourly_cost_override": "process",
    "time_hours": "operation",
    "case_share": "operation",
    "reduction_share": "initiative_operation",
    "review_hours": "initiative_operation",
    "variable_cost_per_exec": "initiative_operation",
    "capex": "initiative",
    "fixed_opex_per_year": "initiative",
    "adoption_y1": "initiative",
    "adoption_y2": "initiative",
    "adoption_y3": "initiative",
}
SHARE_PARAMETERS = frozenset(
    {"overhead_ratio", "case_share", "reduction_share", "adoption_y1", "adoption_y2", "adoption_y3"}
)


class Triple(DomainModel):
    conservative: Decimal = Field(ge=0)
    base: Decimal = Field(ge=0)
    optimistic: Decimal = Field(ge=0)

    def get(self, scenario: Scenario) -> Decimal:
        value: Decimal = getattr(self, scenario.value)
        return value

    @property
    def as_tuple(self) -> tuple[Decimal, Decimal, Decimal]:
        return (self.conservative, self.base, self.optimistic)


class Assumption(DomainModel):
    key: str = Field(min_length=1)
    scope: AssumptionScope
    target_id: str | None = None
    unit: str
    values: Triple
    source: AssumptionSource
    source_citation: Citation | None = None
    requires_input: bool = False
    note: str | None = None
    updated_by: str
    updated_at: datetime

    @property
    def parameter(self) -> str:
        return self.key.rsplit(".", 1)[-1]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        parameter = self.parameter
        expected_scope = PARAMETER_SCOPES.get(parameter)
        if expected_scope is None:
            raise ValueError(f"unknown assumption parameter {parameter!r}")
        if expected_scope != self.scope:
            raise ValueError(f"{parameter} belongs to scope {expected_scope}, not {self.scope}")
        if (self.scope == "global") != (self.target_id is None):
            raise ValueError("target_id is required for every scope except global")
        if parameter in SHARE_PARAMETERS and any(v > 1 for v in self.values.as_tuple):
            raise ValueError(f"{parameter} is a share and must be within [0; 1]")
        if parameter == "horizon_years":
            if len(set(self.values.as_tuple)) != 1:
                raise ValueError("horizon_years is the same in all scenarios")
            if self.values.base not in (1, 2, 3):
                raise ValueError("horizon_years must be 1, 2 or 3")
        if self.source == AssumptionSource.DOCUMENT and self.source_citation is None:
            raise ValueError("an assumption taken from the document needs its citation")
        return self


class AssumptionSet(DomainModel):
    run_id: str
    version: int = Field(ge=1)
    items: list[Assumption]
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [item.key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("assumption keys must be unique within a set")
        return self

    def by_key(self) -> dict[str, Assumption]:
        return {item.key: item for item in self.items}
