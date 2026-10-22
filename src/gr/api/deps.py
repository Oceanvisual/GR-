"""Application state shared by API and web routes, and the dependencies that expose it."""

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request

from gr.auth import BasicAuthenticator
from gr.ingestion.ocr import OcrEngine
from gr.ingestion.ocr_probe import OcrAvailability
from gr.pipeline.ocr_jobs import OcrJobs
from gr.pipeline.runner import JobRunner
from gr.pipeline.workers import CpuWorkers
from gr.services.locks import KeyedLocks
from gr.settings import Settings
from gr.storage.db import Database
from gr.storage.files import FileStore
from gr.storage.repositories import Repositories


@dataclass
class AppState:
    settings: Settings
    db: Database
    repos: Repositories
    files: FileStore
    workers: CpuWorkers
    runner: JobRunner
    ocr: OcrAvailability
    upload_locks: KeyedLocks = field(default_factory=KeyedLocks)
    ocr_jobs: OcrJobs = field(default_factory=OcrJobs)
    ocr_engine: OcrEngine | None = None  # None: the system tesseract of OCR_LANG


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.gr
    return state


async def require_user(request: Request) -> str:
    authenticator: BasicAuthenticator = request.app.state.authenticator
    return await authenticator(request)


StateDep = Annotated[AppState, Depends(get_state)]
UserDep = Annotated[str, Depends(require_user)]
