"""Common base for domain models: immutable, no unknown fields (data-model.md)."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
