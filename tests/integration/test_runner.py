"""JobRunner on fake stages over a temporary SQLite (R-03, T014)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gr.domain.enums import RunStatus, StageName, StageStatus
from gr.domain.ids import new_id
from gr.domain.run import AnalysisRun, FailedItem, InvalidTransitionError
from gr.llm.base import LLMTimeoutError
from gr.pipeline.runner import JobRunner
from gr.pipeline.stages import StageContext, StageResult
from gr.storage.db import Database
from gr.storage.repositories import LlmCall, Repositories

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class FakeStage:
    def __init__(self, name: StageName) -> None:
        self.name = name
        self.calls = 0
        self.fail_with: list[Exception] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.failed_items: list[FailedItem] = []
        self.on_run: Callable[[StageContext], object] | None = None

    async def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        self.started.set()
        if self.on_run is not None:
            result = self.on_run(ctx)
            if asyncio.iscoroutine(result):
                await result
        if self.fail_with:
            raise self.fail_with.pop(0)
        if self.gate is not None:
            await self.gate.wait()
        return StageResult(
            output={"stage": self.name.value, "n": self.calls}, failed_items=self.failed_items
        )


@pytest.fixture
async def repos(tmp_path: Path) -> AsyncIterator[Repositories]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'gr.db'}")
    await database.init()
    yield Repositories(database)
    await database.dispose()


def make_stages() -> dict[StageName, FakeStage]:
    return {name: FakeStage(name) for name in StageName}


def make_runner(
    repos: Repositories, stages: dict[StageName, FakeStage], max_concurrent: int = 2
) -> JobRunner:
    return JobRunner(repos, list(stages.values()), max_concurrent=max_concurrent)


def new_run(doc_id: str = "doc_1") -> AnalysisRun:
    return AnalysisRun(id=new_id("run"), doc_id=doc_id, created_by="expert", created_at=NOW)


def statuses(run: AnalysisRun) -> list[StageStatus]:
    return [s.status for s in run.stages]


async def test_all_stages_run_in_order(repos: Repositories) -> None:
    stages = make_stages()

    async def spend_tokens(ctx: StageContext) -> None:
        await ctx.repos.llm_calls.add(
            LlmCall(
                id=new_id("llm"),
                run_id=ctx.run.id,
                stage="discover",
                model="m",
                attempt=1,
                status="ok",
                in_tokens=7,
                out_tokens=3,
                latency_ms=1,
                request_hash="0" * 64,
                created_at=NOW,
            )
        )

    stages[StageName.DISCOVER].on_run = spend_tokens
    runner = make_runner(repos, stages)
    run = await runner.submit(new_run())
    await runner.wait(run.id)

    stored = await repos.runs.get(run.id)
    assert stored.status == RunStatus.SUCCEEDED
    assert statuses(stored) == [StageStatus.SUCCEEDED] * 5
    assert all(s.attempts == 1 and s.finished_at is not None for s in stored.stages)
    assert await repos.stage_outputs.stages_done(run.id) == set(StageName)
    assert (stored.usage.calls, stored.usage.in_tokens) == (1, 7)
    assert stored.completeness.complete


async def test_crash_after_stage_two_resumes_from_stage_three(repos: Repositories) -> None:
    stages = make_stages()
    stages[StageName.VALIDATE].gate = asyncio.Event()  # stage 3 hangs: the process "dies"
    first = make_runner(repos, stages)
    run = await first.submit(new_run())
    await stages[StageName.VALIDATE].started.wait()
    await first.shutdown()

    stored = await repos.runs.get(run.id)
    assert stored.status == RunStatus.INTERRUPTED
    assert statuses(stored)[:3] == [
        StageStatus.SUCCEEDED,
        StageStatus.SUCCEEDED,
        StageStatus.PENDING,
    ]

    stages[StageName.VALIDATE].gate = None
    second = make_runner(repos, stages)
    await second.recover()
    await second.resume(run.id)
    await second.wait(run.id)

    assert (await repos.runs.get(run.id)).status == RunStatus.SUCCEEDED
    assert [stages[n].calls for n in StageName] == [1, 1, 2, 1, 1]


async def test_recover_marks_running_as_interrupted_and_requeues(repos: Repositories) -> None:
    dead = new_run("doc_dead").transition(RunStatus.RUNNING)
    queued = new_run("doc_queued")
    await repos.runs.add(dead)
    await repos.runs.add(queued)
    stages = make_stages()
    runner = make_runner(repos, stages)
    assert await runner.recover() == [dead.id]
    await runner.wait(queued.id)
    assert (await repos.runs.get(dead.id)).status == RunStatus.INTERRUPTED
    assert (await repos.runs.get(queued.id)).status == RunStatus.SUCCEEDED


async def test_stage_error_fails_run_with_code_and_retry_continues(repos: Repositories) -> None:
    stages = make_stages()
    stages[StageName.DETAIL].fail_with = [LLMTimeoutError()]
    runner = make_runner(repos, stages)
    run = await runner.submit(new_run())
    await runner.wait(run.id)

    failed = await repos.runs.get(run.id)
    assert failed.status == RunStatus.FAILED
    assert failed.error is not None and failed.error.code == "llm_timeout"
    detail = failed.stage(StageName.DETAIL)
    assert detail.status == StageStatus.FAILED and detail.error is not None
    assert failed.first_unfinished_stage() == StageName.DETAIL

    await runner.resume(run.id)
    await runner.wait(run.id)
    done = await repos.runs.get(run.id)
    assert done.status == RunStatus.SUCCEEDED and done.error is None
    assert done.stage(StageName.DETAIL).attempts == 2
    assert stages[StageName.DISCOVER].calls == 1


async def test_unexpected_exception_is_stage_failed(repos: Repositories) -> None:
    stages = make_stages()
    stages[StageName.OPPORTUNITIES].fail_with = [KeyError("bug")]
    runner = make_runner(repos, stages)
    run = await runner.submit(new_run())
    await runner.wait(run.id)
    failed = await repos.runs.get(run.id)
    assert failed.error is not None and failed.error.code == "stage_failed"
    assert "bug" not in failed.error.message


async def test_resume_only_from_failed_or_interrupted(repos: Repositories) -> None:
    runner = make_runner(repos, make_stages())
    run = await runner.submit(new_run())
    await runner.wait(run.id)
    with pytest.raises(InvalidTransitionError):
        await runner.resume(run.id)


async def test_cancel_running_and_queued(repos: Repositories) -> None:
    stages = make_stages()
    stages[StageName.DISCOVER].gate = asyncio.Event()
    runner = make_runner(repos, stages, max_concurrent=1)
    running = await runner.submit(new_run("doc_a"))
    queued = await runner.submit(new_run("doc_b"))
    await stages[StageName.DISCOVER].started.wait()

    assert (await runner.cancel(queued.id)).status == RunStatus.CANCELLED
    cancelled = await runner.cancel(running.id)
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.stage(StageName.DISCOVER).status == StageStatus.PENDING
    assert stages[StageName.DISCOVER].calls == 1  # the queued run never started
    with pytest.raises(InvalidTransitionError):
        await runner.cancel(running.id)


async def test_concurrency_limit(repos: Repositories) -> None:
    stages = make_stages()
    active = 0
    peak = 0
    release = asyncio.Event()

    async def hold(_ctx: StageContext) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    stages[StageName.DISCOVER].on_run = hold
    runner = make_runner(repos, stages, max_concurrent=2)
    runs = [await runner.submit(new_run(f"doc_{i}")) for i in range(5)]
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert peak == 2
    release.set()
    await asyncio.gather(*(runner.wait(r.id) for r in runs))
    assert peak == 2
    assert {(await repos.runs.get(r.id)).status for r in runs} == {RunStatus.SUCCEEDED}


async def test_progress_and_partial_failures(repos: Repositories) -> None:
    stages = make_stages()
    seen: list[tuple[int, int]] = []

    async def report(ctx: StageContext) -> None:
        await ctx.report_progress(1, 3)
        progress = (await ctx.repos.runs.get(ctx.run.id)).stage(StageName.DETAIL).progress
        assert progress is not None
        seen.append((progress.done, progress.total))

    stages[StageName.DETAIL].on_run = report
    stages[StageName.DETAIL].failed_items = [
        FailedItem(stage=StageName.DETAIL, process_key="p2", error_code="llm_invalid_output")
    ]
    runner = make_runner(repos, stages)
    run = await runner.submit(new_run())
    await runner.wait(run.id)
    stored = await repos.runs.get(run.id)
    assert seen == [(1, 3)]
    assert stored.stage(StageName.DETAIL).progress is not None
    assert not stored.completeness.complete
    assert stored.completeness.failed_items[0].process_key == "p2"


async def test_saved_output_is_not_recomputed(repos: Repositories) -> None:
    stages = make_stages()
    run = new_run().transition(RunStatus.RUNNING).transition(RunStatus.INTERRUPTED)
    await repos.runs.add(run)
    await repos.stage_outputs.put(run.id, StageName.DISCOVER, {"stage": "discover"})
    runner = make_runner(repos, stages)
    await runner.resume(run.id)
    await runner.wait(run.id)
    assert stages[StageName.DISCOVER].calls == 0
    assert (await repos.runs.get(run.id)).status == RunStatus.SUCCEEDED


def test_stages_must_be_ordered(repos: Repositories) -> None:
    stages = list(make_stages().values())
    with pytest.raises(ValueError, match="order"):
        JobRunner(repos, list(reversed(stages)), max_concurrent=1)
