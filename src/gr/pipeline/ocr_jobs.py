"""Background OCR of uploaded scans (FR-008, R-04, T070): a task per document, at most
OCR_CONCURRENCY at a time.

The state of the work lives in the document (`diagnostics.ocr`), not here: a stop cancels the
tasks and leaves their documents `queued` or `running`, and the next start submits them again
(the document service does). One document is never recognised twice at once.
"""

import asyncio
from collections.abc import Awaitable, Callable

from gr.logging import get_logger

log = get_logger(__name__)


class OcrJobs:
    def __init__(self, concurrency: int = 1) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def submit(self, doc_id: str, work: Callable[[], Awaitable[None]]) -> None:
        if self.is_tracking(doc_id):
            return

        async def run() -> None:
            async with self._semaphore:
                await work()

        task = asyncio.create_task(run(), name=f"ocr:{doc_id}")
        self._tasks[doc_id] = task
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        doc_id = task.get_name().removeprefix("ocr:")
        if self._tasks.get(doc_id) is task:
            del self._tasks[doc_id]
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("ocr.task_crashed", doc_id=doc_id, exc_type=type(error).__name__)

    def is_tracking(self, doc_id: str) -> bool:
        task = self._tasks.get(doc_id)
        return task is not None and not task.done()

    async def wait(self, doc_id: str) -> None:
        task = self._tasks.get(doc_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
