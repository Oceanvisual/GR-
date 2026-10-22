"""Adapter from the eval to the pipeline: a document in, the verified process model out.

The application runs in-process with its own data directory (`evals/.data`, so the results
can be opened in the UI with DATA_DIR=evals/.data) and the LLM settings from the
environment. The document goes through the same services as an upload and an analysis
start from the API. The LLM cache is off unless asked: the eval measures what the model
answers now, not what it answered last time.
"""

import asyncio
import hashlib
import shutil
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evals.budget import Budget, BudgetedLLM
from evals.metrics import FragmentNode, PredDeadline, Prediction, PredOperation, PredProcess
from evals.metrics_opportunities import PredOpportunities, to_opportunities
from gr.api.deps import AppState
from gr.app import create_app
from gr.domain.document import Citation, Fragment
from gr.domain.enums import KnowledgeLayer, RunStatus
from gr.domain.process import Deadline, Grounded, Process, ProcessModel
from gr.llm.base import StructuredLLM
from gr.llm.cache import CachedLLM
from gr.llm.openai_compat import OpenAICompatibleLLM
from gr.pipeline.analysis import AnalysisSettings, analysis_stages
from gr.services.analyses import AnalysisService
from gr.services.documents import DocumentService, Upload
from gr.settings import Settings
from gr.storage.db import Database
from gr.storage.repositories import LlmCallRepository

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "evals" / ".data"
USER = "eval"


class EvalRunError(RuntimeError):
    """The document could not be analysed: the eval reports it for this document."""


class Outcome(BaseModel):
    prediction: Prediction
    run_id: str
    status: str
    complete: bool
    failed_processes: int
    usage: dict[str, float]
    models: dict[str, str]
    prompt_versions: dict[str, str]
    opportunities: PredOpportunities | None = None  # with S6 (--s6, T066)


# --- the process model → prediction --------------------------------------------------------


def _citations(node: Any) -> Iterator[Citation]:
    if isinstance(node, Citation):
        yield node
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from _citations(getattr(node, name))
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _citations(item)


def _labels(grounded: Grounded[Any] | None, label_of: dict[str, str]) -> list[str]:
    if grounded is None or grounded.layer != KnowledgeLayer.FACT:
        return []
    return [label_of[c.fragment_id] for c in grounded.citations if c.fragment_id in label_of]


def _deadline(deadline: Deadline, full_text: str) -> PredDeadline | None:
    if deadline.raw.layer != KnowledgeLayer.FACT:
        return None
    spans = [
        full_text[c.grounding.matched_start : c.grounding.matched_end]
        for c in deadline.raw.citations
        if c.grounding.matched_start is not None and c.grounding.matched_end is not None
    ]
    return PredDeadline(value=deadline.value, unit=deadline.unit.value, spans=spans)


def _process(process: Process, label_of: dict[str, str], full_text: str) -> PredProcess:
    operations = [
        PredOperation(name=op.name, labels=_labels(op.action, label_of))
        for op in process.operations
    ]
    deadlines = [
        found
        for d in [*process.deadlines, *(op.deadline for op in process.operations if op.deadline)]
        if (found := _deadline(d, full_text)) is not None
    ]
    cited = _labels(process.name, label_of) + [lab for op in operations for lab in op.labels]
    return PredProcess(
        name=process.name.value,
        labels=sorted(set(cited)),
        operations=operations,
        deadlines=deadlines,
    )


def to_prediction(model: ProcessModel, fragments: Sequence[Fragment], full_text: str) -> Prediction:
    index = {f.id: n for n, f in enumerate(fragments)}
    nodes = [
        FragmentNode(label=f.label, parent=index.get(f.parent_id) if f.parent_id else None)
        for f in fragments
    ]
    label_of = {f.id: f.label for f in fragments}
    citations = list(_citations(model.processes))
    return Prediction(
        fragments=nodes,
        processes=[_process(p, label_of, full_text) for p in model.processes],
        citations_passed=sum(c.grounding.status.passes for c in citations),
        citations_total=len(citations),
        unverified=len(model.unverified),
        fragments_used=model.coverage.fragments_used,
        fragments_total=model.coverage.fragments_total,
    )


# --- running the pipeline ------------------------------------------------------------------


def llm_overrides(
    budget: Budget | None, *, s6: bool, discovery_model: str | None
) -> dict[str, Any]:
    """Settings over the environment: a budget pins its default model; S3 may have its own."""
    overrides: dict[str, Any] = {}
    if budget is not None:
        overrides = {"llm_model_extraction": budget.allowed_model, "llm_max_retries": 0}
        if s6:  # S6 runs on the analysis model: the budget's default model
            overrides["llm_model_analysis"] = budget.allowed_model
    if discovery_model:  # LLM_MODEL_DISCOVERY, the rest unchanged
        overrides["llm_model_discovery"] = discovery_model
    return overrides


@asynccontextmanager
async def pipeline(
    *,
    cached: bool,
    budget: Budget | None = None,
    s6: bool = False,
    discovery_model: str | None = None,
) -> AsyncIterator[AppState]:
    settings = Settings(  # LLM_* from the environment, like the service itself
        data_dir=DATA_DIR,
        llm_cache_enabled=cached,
        log_level="WARNING",
        **llm_overrides(budget, s6=s6, discovery_model=discovery_model),
    )
    # A separate handle to the same DB stores raw calls for exact provider billing.
    db = Database(settings.resolved_database_url)
    if budget is None:
        client = OpenAICompatibleLLM(settings, LlmCallRepository(db))
    else:
        guarded = BudgetedLLM(settings, LlmCallRepository(db))
        guarded.budget = budget
        client = guarded
    llm: StructuredLLM = client
    if cached:
        from gr.storage.repositories import LlmCacheRepository

        llm = CachedLLM(client, LlmCacheRepository(db), client.model_for, client.options_for)
    try:
        stages = analysis_stages(
            llm,
            AnalysisSettings(
                max_output_tokens=settings.llm_max_output_tokens,
                concurrency=settings.llm_concurrency,
            ),
            include_opportunities=s6,
        )
        app = create_app(settings, stages=stages)
        async with app.router.lifespan_context(app):
            yield app.state.gr
    finally:
        await client.client.close()
        await db.dispose()


@asynccontextmanager
async def judge_client(model: str, budget: Budget | None = None) -> AsyncIterator[StructuredLLM]:
    """The judge's LLM (T066): the endpoint of the environment, the judge's model for S6."""
    overrides: dict[str, Any] = {"llm_model_analysis": model}
    if budget is not None:
        overrides["llm_max_retries"] = 0
    settings = Settings(data_dir=DATA_DIR, log_level="WARNING", **overrides)
    if budget is None:
        client = OpenAICompatibleLLM(settings)
    else:
        guarded = BudgetedLLM(settings)
        guarded.budget = budget
        client = guarded
    try:
        yield client
    finally:
        await client.client.close()


def _staged_upload(path: Path, tmp_dir: Path) -> Upload:
    """A copy of the file as an upload: ingest removes the upload file when done."""
    data = path.read_bytes()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    copy = tmp_dir / f".eval-{path.name}"
    shutil.copyfile(path, copy)
    return Upload(
        path=copy, filename=path.name, size=len(data), sha256=hashlib.sha256(data).hexdigest()
    )


async def _ingest(state: AppState, path: Path) -> str:
    upload = await asyncio.to_thread(_staged_upload, path, state.settings.data_dir / "tmp")
    result = await DocumentService(state).ingest(upload, USER, "eval")
    if not result.document.diagnostics.analyzable:
        raise EvalRunError(f"{path.name}: документ не пригоден для анализа")
    return result.document.id


async def analyse(state: AppState, path: Path, *, s6: bool = False) -> Outcome:
    doc_id = await _ingest(state, path)
    service = AnalysisService(state)
    started = time.perf_counter()
    run = await service.start(doc_id, USER)
    await state.runner.wait(run.id)
    latency = time.perf_counter() - started
    run = await service.get(run.id)
    if run.status != RunStatus.SUCCEEDED:
        reason = run.error.message if run.error else run.status.value
        raise EvalRunError(f"{path.name}: анализ {run.id} завершился со статусом {reason}")
    model = await service.processes(run.id)
    fragments = await state.repos.fragments.list_for_document(doc_id)
    full_text = (await state.repos.texts.get(doc_id)).full_text
    usage: dict[str, float] = {
        "calls": run.usage.calls,
        "in_tokens": run.usage.in_tokens,
        "out_tokens": run.usage.out_tokens,
        "latency_s": round(latency, 1),
    }
    if run.usage.cost is not None:
        usage["cost"] = float(run.usage.cost)
    opportunities = None
    if s6:
        outputs = await service.outputs(run.id, initiatives=True)
        opportunities = to_opportunities(outputs.effective.result, len(model.processes))
    return Outcome(
        prediction=to_prediction(model, fragments, full_text),
        run_id=run.id,
        status=run.status.value,
        complete=run.completeness.complete,
        failed_processes=len(run.completeness.failed_items),
        usage=usage,
        models=dict(run.config_snapshot.models),
        prompt_versions=dict(run.config_snapshot.prompt_versions),
        opportunities=opportunities,
    )
