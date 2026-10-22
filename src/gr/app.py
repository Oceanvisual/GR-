"""FastAPI application: lifespan wiring, middleware, routers, static files."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import gr
from gr.api import analyses, documents, health, results, reviews
from gr.api.deps import AppState
from gr.auth import BasicAuthenticator, SameOriginGuard
from gr.errors import install_error_handlers
from gr.ingestion.ocr_probe import probe_tesseract
from gr.llm.base import StructuredLLM
from gr.llm.factory import build_llm
from gr.logging import configure_logging, get_logger
from gr.pipeline.analysis import AnalysisSettings, analysis_stages
from gr.pipeline.runner import JobRunner
from gr.pipeline.stages import Stage
from gr.pipeline.workers import CpuWorkers
from gr.services.analyses import price
from gr.settings import Settings, get_settings
from gr.storage.db import Database
from gr.storage.files import FileStore
from gr.storage.repositories import Repositories
from gr.web import analyses as web_analyses
from gr.web import results as web_results
from gr.web import routes as web
from gr.web import settings as web_settings

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    stages: Sequence[Stage] | None = None,
    llm: StructuredLLM | None = None,
) -> FastAPI:
    """`stages` replaces the pipeline (tests of the runner); `llm` replaces the model client
    of the default pipeline (FakeLLM in tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(
            settings.resolved_database_url, busy_timeout_ms=settings.sqlite_busy_timeout_ms
        )
        await db.init()
        repos = Repositories(db)
        pipeline = (
            stages
            if stages is not None
            else analysis_stages(
                llm or build_llm(settings, repos),
                AnalysisSettings(
                    max_output_tokens=settings.llm_max_output_tokens,
                    concurrency=settings.llm_concurrency,
                ),
            )
        )
        runner = JobRunner(
            repos,
            pipeline,
            max_concurrent=settings.max_concurrent_analyses,
            pricing=lambda usage: price(usage, settings),
        )
        app.state.gr = AppState(
            settings=settings,
            db=db,
            repos=repos,
            files=FileStore(settings.data_dir / "files"),
            workers=CpuWorkers(settings.parse_workers, settings.parse_timeout_s),
            runner=runner,
            ocr=probe_tesseract(settings.ocr_lang, enabled=settings.ocr_enabled),
        )
        await runner.recover()
        log.info(
            "app.started",
            env=settings.app_env,
            llm_policy=settings.llm_data_policy,
            ocr=app.state.gr.ocr.available,
        )
        try:
            yield
        finally:
            await runner.shutdown()
            await db.dispose()

    app = FastAPI(
        title="AI-анализ НПА",
        version=gr.__version__,
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    app.state.authenticator = BasicAuthenticator(settings)
    install_error_handlers(app)
    public_url = str(settings.app_public_url) if settings.app_public_url else None
    app.add_middleware(SameOriginGuard, public_url=public_url)
    app.include_router(health.liveness)
    app.include_router(health.readiness)
    app.include_router(documents.router)
    app.include_router(analyses.router)
    app.include_router(results.router)
    app.include_router(reviews.router)
    app.include_router(web.router)
    app.include_router(web_analyses.router)
    app.include_router(web_results.router)
    app.include_router(web_settings.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")
    return app
