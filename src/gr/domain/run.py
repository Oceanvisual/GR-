"""Analysis run and its stages (data-model.md §9)."""

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.enums import RunStatus, StageName, StageStatus

# queued → running → succeeded | failed | interrupted; failed/interrupted → running (resume);
# queued | running → cancelled.
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.CANCELLED}
    ),
    RunStatus.FAILED: frozenset({RunStatus.RUNNING}),
    RunStatus.INTERRUPTED: frozenset({RunStatus.RUNNING}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}
ACTIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})
STAGE_ORDER: tuple[StageName, ...] = tuple(StageName)


class InvalidTransitionError(ValueError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity}: transition {current} → {target} is not allowed")
        self.current = current
        self.target = target


class ErrorInfo(DomainModel):
    code: str
    message: str


class StageProgress(DomainModel):
    done: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.done > self.total:
            raise ValueError("progress.done must not exceed progress.total")
        return self


class StageState(DomainModel):
    name: StageName
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)
    error: ErrorInfo | None = None
    progress: StageProgress | None = None


class RunConfig(DomainModel):
    """Everything that determines the run's output, frozen at start for reproducibility."""

    models: dict[str, str] = {}
    prompt_versions: dict[str, str] = {}
    structured_mode: str = "auto"
    temperature: float = Field(default=0.0, ge=0, le=2)
    chunking: dict[str, int] = {}
    grounding_thresholds: dict[str, float] = {}
    assumption_profile_hash: str | None = None


class UsageTotals(DomainModel):
    calls: int = Field(default=0, ge=0)
    in_tokens: int = Field(default=0, ge=0)
    out_tokens: int = Field(default=0, ge=0)
    estimated: bool = False
    cost: Decimal | None = Field(default=None, ge=0)


class FailedItem(DomainModel):
    stage: StageName
    process_key: str
    error_code: str


class Completeness(DomainModel):
    complete: bool = True
    failed_items: list[FailedItem] = []


def initial_stages() -> list[StageState]:
    return [StageState(name=name) for name in STAGE_ORDER]


class AnalysisRun(DomainModel):
    id: str = Field(pattern=r"^run_[0-9A-Z]{26}$")
    doc_id: str
    parent_run_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    created_by: str
    created_at: datetime
    stages: list[StageState] = Field(default_factory=initial_stages)
    config_snapshot: RunConfig = RunConfig()
    usage: UsageTotals = UsageTotals()
    error: ErrorInfo | None = None
    completeness: Completeness = Completeness()

    @model_validator(mode="after")
    def _stages_in_order(self) -> Self:
        if tuple(stage.name for stage in self.stages) != STAGE_ORDER:
            raise ValueError(f"stages must be exactly {[s.value for s in STAGE_ORDER]}")
        if self.status == RunStatus.FAILED and self.error is None:
            raise ValueError("failed run must carry an error")
        return self

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_RUN_STATUSES

    def transition(self, target: RunStatus, *, error: ErrorInfo | None = None) -> "AnalysisRun":
        """Copy of the run in `target` status; raises on a transition data-model forbids."""
        if target not in RUN_TRANSITIONS[self.status]:
            raise InvalidTransitionError("run", self.status, target)
        if target == RunStatus.FAILED and error is None:
            raise ValueError("transition to failed requires an error")
        return self.model_copy(
            update={"status": target, "error": error if target == RunStatus.FAILED else None}
        )

    def first_unfinished_stage(self) -> StageName | None:
        """Where `resume` starts: the first stage that has not succeeded or been skipped."""
        done = {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
        return next((stage.name for stage in self.stages if stage.status not in done), None)

    def with_stage(self, stage: StageState) -> "AnalysisRun":
        stages = [stage if s.name == stage.name else s for s in self.stages]
        return self.model_copy(update={"stages": stages})

    def stage(self, name: StageName) -> StageState:
        return next(stage for stage in self.stages if stage.name == name)
