"""Common base for domain models: immutable, no unknown fields (data-model.md)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VersionedRecord(DomainModel):
    """A per-run set saved as a new version on every change (assumptions, priority config)."""

    run_id: str
    version: int = Field(ge=1)
    created_by: str
    created_at: datetime
