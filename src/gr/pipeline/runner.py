"""In-process job runner for analyses (R-03): queue, concurrency limit, per-stage persistence,
resume after a crash or restart, cancellation."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from gr.domain.enums import RunStatus, StageName, StageStatus
from gr.domain.run import AnalysisRun, Completeness, ErrorInfo, StageProgress, UsageTotals
from gr.llm.base import LLMError
from gr.logging import bind_context, clear_context, get_logger
from gr.pipeline.stages import Stage, StageContext, StageError
from gr.storage.repositories import Repositories

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobRunner:
    def __init__(
        self,
        repos: Repositories,
        stages: Sequence[Stage],
        *,
        max_concurrent: int,
        clock: Callable[[], datetime] = _utcnow,
        pricing: Callable[[UsageTotals], UsageTotals] = lambda usage: usage,
    ) -> None:
        order = [stage.name for stage in stages]
        if order != sorted(order, key=list(StageName).index) or len(set(order)) != len(order):
            raise ValueError("stages must be unique and in pipeline order")
        self.repos = repos
        self.stages = list(stages)
        self.clock = clock
        self.pricing = pricing
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()

    # --- lifecycle -------------------------------------------------------------------------

    async def recover(self) -> list[str]:
        """On startup: runs left `running` by a dead process become `interrupted`; queued
        runs are picked up again."""
        interrupted: list[str] = []
        for run in await self.repos.runs.list_by_status([RunStatus.RUNNING]):
            await self.repos.runs.update(_reset_running(run).transition(RunStatus.INTERRUPTED))
            interrupted.append(run.id)
        for run in await self.repos.runs.list_by_status([RunStatus.QUEUED]):
            self._spawn(run.id)
        if interrupted:
            log.warning("runner.interrupted_on_start", runs=len(interrupted))
        return interrupted

    async def shutdown(self) -> None:
        """Stop work without marking runs cancelled: they become `interrupted`."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- commands --------------------------------------------------------------------------

    async def submit(self, run: AnalysisRun) -> AnalysisRun:
        await self.repos.runs.add(run)
        self._spawn(run.id)
        return run

    async def resume(self, run_id: str) -> AnalysisRun:
        run = await self.repos.runs.get(run_id)
        resumed = run.transition(RunStatus.RUNNING)  # only from failed or interrupted
        await self.repos.runs.update(resumed)
        self._spawn(run_id)
        return resumed

    async def cancel(self, run_id: str) -> AnalysisRun:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            self._cancel_requested.add(run_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return await self.repos.runs.get(run_id)
        run = await self.repos.runs.get(run_id)
        cancelled = run.transition(RunStatus.CANCELLED)
        await self.repos.runs.update(cancelled)
        return cancelled

    def is_tracking(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # --- execution -------------------------------------------------------------------------

    def _spawn(self, run_id: str) -> None:
        if self.is_tracking(run_id):
            return
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task[None]) -> None:
        # Only this task's own entry: a resumed run may already track a newer task.
        run_id = task.get_name().removeprefix("run:")
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]

    async def _execute(self, run_id: str) -> None:
        run: AnalysisRun | None = None
        try:
            async with self._semaphore:
                run = await self.repos.runs.get(run_id)
                bind_context(run_id=run_id, doc_id=run.doc_id)
                if run.status == RunStatus.QUEUED:
                    run = run.transition(RunStatus.RUNNING)
                    await self.repos.runs.update(run)
                if run.status != RunStatus.RUNNING:
                    return
                run = await self._run_stages(run)
        except asyncio.CancelledError:
            # Shielded: the write must finish even though this task is being cancelled.
            await asyncio.shield(self._stop(run_id, user_cancel=run_id in self._cancel_requested))
            self._cancel_requested.discard(run_id)
            raise
        except Exception as exc:
            log.error("runner.crashed", exc_type=type(exc).__name__, exc_info=exc)
            if run is not None:
                await self._crash(run_id)
        finally:
            clear_context()

    async def _run_stages(self, run: AnalysisRun) -> AnalysisRun:
        done = await self.repos.stage_outputs.stages_done(run.id)
        completeness = run.completeness
        for stage in self.stages:
            state = run.stage(stage.name)
            if state.status in (StageStatus.SUCCEEDED, StageStatus.SKIPPED):
                continue
            if stage.name in done:
                # Output was saved but the status update was lost (crash in between).
                run = await self._save(
                    run.with_stage(
                        state.model_copy(update={"status": StageStatus.SUCCEEDED, "error": None})
                    )
                )
                continue

            bind_context(stage=stage.name.value)
            run = await self._save(
                run.with_stage(
                    state.model_copy(
                        update={
                            "status": StageStatus.RUNNING,
                            "started_at": self.clock(),
                            "finished_at": None,
                            "attempts": state.attempts + 1,
                            "error": None,
                            "progress": None,
                        }
                    )
                )
            )
            log.info("stage.started", attempt=run.stage(stage.name).attempts)
            try:
                result = await stage.run(
                    StageContext(
                        run=run,
                        repos=self.repos,
                        report_progress=self._reporter(run.id, stage.name),
                    )
                )
            except (LLMError, StageError) as exc:
                return await self._fail(
                    run, stage.name, ErrorInfo(code=exc.code, message=exc.message_ru)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("stage.crashed", exc_type=type(exc).__name__, exc_info=exc)
                return await self._fail(
                    run,
                    stage.name,
                    ErrorInfo(code="stage_failed", message="Внутренняя ошибка этапа."),
                )

            await self.repos.stage_outputs.put(run.id, stage.name, result.output)
            run = await self.repos.runs.get(run.id)  # pick up progress written meanwhile
            if result.failed_items:
                completeness = Completeness(
                    complete=False, failed_items=[*completeness.failed_items, *result.failed_items]
                )
            run = await self._save(
                run.with_stage(
                    run.stage(stage.name).model_copy(
                        update={"status": StageStatus.SUCCEEDED, "finished_at": self.clock()}
                    )
                ).model_copy(update={"completeness": completeness})
            )
            log.info("stage.succeeded", failed_items=len(result.failed_items))

        usage = self.pricing(await self.repos.llm_calls.usage_for_run(run.id))
        implemented = {stage.name for stage in self.stages}
        for state in run.stages:  # stages this build does not have yet (S6, S7 before US3)
            if state.name not in implemented and state.status == StageStatus.PENDING:
                run = run.with_stage(state.model_copy(update={"status": StageStatus.SKIPPED}))
        final = run.model_copy(update={"usage": usage}).transition(RunStatus.SUCCEEDED)
        await self.repos.runs.update(final)
        log.info("run.succeeded", llm_calls=usage.calls)
        return final

    def _reporter(self, run_id: str, stage: StageName) -> Callable[[int, int], Awaitable[None]]:
        async def report(done: int, total: int) -> None:
            run = await self.repos.runs.get(run_id)
            state = run.stage(stage)
            progress = StageProgress(done=done, total=total)
            await self.repos.runs.update(
                run.with_stage(state.model_copy(update={"progress": progress}))
            )

        return report

    async def _save(self, run: AnalysisRun) -> AnalysisRun:
        await self.repos.runs.update(run)
        return run

    async def _fail(self, run: AnalysisRun, stage: StageName, error: ErrorInfo) -> AnalysisRun:
        state = run.stage(stage).model_copy(
            update={"status": StageStatus.FAILED, "finished_at": self.clock(), "error": error}
        )
        usage = self.pricing(await self.repos.llm_calls.usage_for_run(run.id))
        failed = (
            run.with_stage(state)
            .model_copy(update={"usage": usage})
            .transition(RunStatus.FAILED, error=error)
        )
        await self.repos.runs.update(failed)
        log.warning("stage.failed", error_code=error.code)
        return failed

    async def _crash(self, run_id: str) -> None:
        """Mark a run failed after an unexpected error, from its stored state: the stages
        saved before the error stay as they are, the one in progress fails."""
        error = ErrorInfo(code="stage_failed", message="Внутренняя ошибка анализа.")
        try:
            current = await self.repos.runs.get(run_id)
            if current.status != RunStatus.RUNNING:
                return
            now = self.clock()
            failed_stage = {"status": StageStatus.FAILED, "finished_at": now, "error": error}
            stages = [
                s.model_copy(update=failed_stage) if s.status == StageStatus.RUNNING else s
                for s in current.stages
            ]
            failed = current.model_copy(update={"stages": stages})
            await self.repos.runs.update(failed.transition(RunStatus.FAILED, error=error))
        except Exception as exc:
            # The store itself is failing: recover() marks the run interrupted on restart.
            log.error("runner.crash_not_saved", exc_type=type(exc).__name__)

    async def _stop(self, run_id: str, *, user_cancel: bool) -> None:
        current = await self.repos.runs.get(run_id)
        if not current.is_active:
            return
        target = RunStatus.CANCELLED if user_cancel else RunStatus.INTERRUPTED
        if current.status == RunStatus.QUEUED and target == RunStatus.INTERRUPTED:
            return  # never started: stays queued and is picked up by recover()
        await self.repos.runs.update(_reset_running(current).transition(target))
        log.info("run.stopped", status=target.value)


def _reset_running(run: AnalysisRun) -> AnalysisRun:
    """A stage cut off mid-way is not done: it goes back to pending and reruns on resume."""
    stages = [
        s.model_copy(update={"status": StageStatus.PENDING, "started_at": None, "progress": None})
        if s.status == StageStatus.RUNNING
        else s
        for s in run.stages
    ]
    return run.model_copy(update={"stages": stages})
