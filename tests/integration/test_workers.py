"""CPU workers: crashes and hangs stay inside their own process (NFR-014, T076)."""

import asyncio
import os
import time

import pytest

from gr.pipeline.workers import (
    CpuWorkers,
    WorkerCrashedError,
    WorkerTaskError,
    WorkerTimeoutError,
)

# Worker functions must be module-level: the spawned child imports them by name.


def square(x: int) -> int:
    return x * x


def crash() -> None:
    os._exit(1)


def hang() -> None:
    while True:
        time.sleep(0.1)


def burn(seconds: float) -> int:
    end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < end:
        n += 1
    return n


def fail() -> None:
    raise ValueError("файл повреждён")


def pid() -> int:
    return os.getpid()


async def test_result_comes_back() -> None:
    workers = CpuWorkers(max_workers=2, default_timeout_s=30)
    assert await workers.run(square, 7) == 49
    assert await workers.run(pid) != os.getpid()


async def test_exception_is_reported_with_type() -> None:
    workers = CpuWorkers(max_workers=1, default_timeout_s=30)
    with pytest.raises(WorkerTaskError) as exc:
        await workers.run(fail)
    assert exc.value.exc_type == "ValueError"
    assert "файл повреждён" in exc.value.message


async def test_crash_fails_only_its_task() -> None:
    workers = CpuWorkers(max_workers=2, default_timeout_s=30)
    results = await asyncio.gather(
        workers.run(crash), workers.run(burn, 0.5), return_exceptions=True
    )
    assert isinstance(results[0], WorkerCrashedError)
    assert isinstance(results[1], int) and results[1] > 0
    assert await workers.run(square, 3) == 9  # the service keeps working


async def test_timeout_kills_only_its_process() -> None:
    workers = CpuWorkers(max_workers=2, default_timeout_s=30)
    started = time.monotonic()
    results = await asyncio.gather(
        workers.run(hang, timeout_s=1), workers.run(burn, 0.3), return_exceptions=True
    )
    assert isinstance(results[0], WorkerTimeoutError)
    assert isinstance(results[1], int)
    assert time.monotonic() - started < 10
    assert await workers.run(square, 4) == 16


async def test_event_loop_stays_responsive_during_heavy_task() -> None:
    workers = CpuWorkers(max_workers=1, default_timeout_s=30)
    heavy = asyncio.create_task(workers.run(burn, 1.5))
    await asyncio.sleep(0.5)  # let the worker start
    lags = []
    for _ in range(10):
        before = time.monotonic()
        await asyncio.sleep(0.01)
        lags.append(time.monotonic() - before)
    assert max(lags) < 0.2
    assert await heavy > 0


async def test_concurrency_bounded() -> None:
    workers = CpuWorkers(max_workers=2, default_timeout_s=30)
    started = time.monotonic()
    await asyncio.gather(*(workers.run(burn, 0.5) for _ in range(4)))
    assert time.monotonic() - started >= 1.0  # two waves of two


async def test_cancellation_kills_worker() -> None:
    workers = CpuWorkers(max_workers=1, default_timeout_s=30)
    task = asyncio.create_task(workers.run(hang))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await workers.run(square, 5) == 25  # the slot was released
