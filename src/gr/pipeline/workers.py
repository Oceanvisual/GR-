"""CPU-bound work (parsing, page rendering, OCR, quote checks) off the event loop (NFR-014).

Each task gets its own spawned process, at most PARSE_WORKERS at a time. A pool would be
cheaper to start, but in `ProcessPoolExecutor` one dead worker breaks the whole pool and fails
every task in flight; here a crash or a timeout kills only its own process.
"""

import asyncio
import contextlib
import multiprocessing
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from gr.logging import get_logger

log = get_logger(__name__)

_MESSAGE_LIMIT = 500


class WorkerError(Exception):
    code = "worker_failed"


class WorkerTimeoutError(WorkerError):
    code = "worker_timeout"


class WorkerCrashedError(WorkerError):
    code = "worker_crashed"


class WorkerTaskError(WorkerError):
    """The function raised inside the worker; carries the exception type and message."""

    code = "worker_task_failed"

    def __init__(self, exc_type: str, message: str) -> None:
        super().__init__(f"{exc_type}: {message}")
        self.exc_type = exc_type
        self.message = message


def _child_main(conn: Connection, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        result = fn(*args)
        conn.send((True, result))
    except BaseException as exc:
        detail = "".join(traceback.format_exception_only(exc)).strip()[:_MESSAGE_LIMIT]
        # Broken pipe: the parent sees EOF and reports a crash instead.
        with contextlib.suppress(Exception):
            conn.send((False, (type(exc).__name__, detail)))
    finally:
        conn.close()


def _receive(conn: Connection) -> tuple[bool, Any]:
    try:
        message: tuple[bool, Any] = conn.recv()
        return message
    except EOFError:
        return False, None


class CpuWorkers:
    def __init__(self, max_workers: int, default_timeout_s: float) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._semaphore = asyncio.Semaphore(max_workers)
        self.default_timeout_s = default_timeout_s

    async def run[R](self, fn: Callable[..., R], *args: Any, timeout_s: float | None = None) -> R:
        """Run module-level `fn(*args)` in a fresh process; raise WorkerError on failure."""
        limit = self.default_timeout_s if timeout_s is None else timeout_s
        async with self._semaphore:
            receiver, sender = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=_child_main, args=(sender, fn, args), daemon=True
            )
            process.start()
            sender.close()  # only the child holds the sending end: its exit means EOF
            try:
                ok, payload = await asyncio.wait_for(asyncio.to_thread(_receive, receiver), limit)
            except TimeoutError:
                log.warning("worker.timeout", fn=fn.__name__, timeout_s=limit)
                raise WorkerTimeoutError(f"{fn.__name__} exceeded {limit:g} s") from None
            finally:
                if process.is_alive():
                    process.kill()
                await asyncio.to_thread(process.join, 5)
                receiver.close()
        if ok:
            result: R = payload
            return result
        if payload is None:
            log.warning("worker.crashed", fn=fn.__name__, exitcode=process.exitcode)
            raise WorkerCrashedError(f"{fn.__name__} worker exited with {process.exitcode}")
        exc_type, message = payload
        raise WorkerTaskError(exc_type, message)
