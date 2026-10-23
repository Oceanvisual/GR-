"""Stage protocol for the runner: each stage reads earlier outputs and returns its own."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from gr.domain.enums import StageName
from gr.domain.run import AnalysisRun, FailedItem
from gr.storage.repositories import Repositories


class StageError(Exception):
    """A stage failure with a code for `StageState.error` (e.g. `stage_failed`)."""

    def __init__(self, code: str, message_ru: str) -> None:
        super().__init__(f"{code}: {message_ru}")
        self.code = code
        self.message_ru = message_ru


@dataclass(frozen=True)
class StageResult:
    output: dict[str, Any]
    # Items the stage skipped after an error (≤ 30 % of processes): the run is incomplete.
    failed_items: list[FailedItem] = field(default_factory=list)


@dataclass(frozen=True)
class StageContext:
    run: AnalysisRun
    repos: Repositories
    report_progress: Callable[[int, int], Awaitable[None]]

    async def output_of(self, stage: StageName) -> dict[str, Any]:
        output = await self.repos.stage_outputs.get(self.run.id, stage)
        if output is None:
            raise StageError("stage_failed", f"Нет результата этапа {stage.value}.")
        return output


class Stage(Protocol):
    name: StageName

    async def run(self, ctx: StageContext) -> StageResult: ...
