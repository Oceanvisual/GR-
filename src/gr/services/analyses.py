"""Analysis runs (api.md § Анализ): start with the checks, status, resume, cancel, results.

The run config is frozen at start (models, prompt versions, chunking and grounding
thresholds) so a result can be traced to what produced it (FR-030). Token use of an active
run is summed live; cost comes from LLM_PRICE_* when both are set. Results are read with the
expert's decisions applied (data-model §8): the stage outputs stay as stored.

Initiatives of a process are made again only on request (FR-083): a regeneration run takes
the process as the expert left it, runs S6 and S7 for it alone and stores the next version;
the latest version of every process is the effective one, the earlier ones stay readable.
One run at a time per document: an analysis or a regeneration.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from gr.analysis.mermaid import process_to_mermaid
from gr.analysis.overlay import EffectiveState
from gr.api.deps import AppState
from gr.domain.enums import RunStatus, StageName, StageStatus
from gr.domain.ids import new_id
from gr.domain.initiative import InitiativeSet
from gr.domain.process import Process, ProcessModel
from gr.domain.review import ReviewEvent
from gr.domain.run import (
    AnalysisRun,
    Completeness,
    InvalidTransitionError,
    RunConfig,
    StageState,
    UsageTotals,
)
from gr.errors import AppError
from gr.extraction.opportunities import effective_process
from gr.extraction.prompt_files import load_prompt
from gr.extraction.render import load_chunking
from gr.grounding.quotes import load_config
from gr.grounding.roles import load_role_config
from gr.llm.base import ExternalLLMNotAllowedError, LLMNotConfiguredError
from gr.llm.policy import check_data_policy
from gr.logging import get_logger
from gr.pipeline.outputs import StoredOutputs, load_outputs
from gr.settings import Settings
from gr.storage.repositories import NotFoundError

log = get_logger(__name__)

_MTOK = Decimal(1_000_000)


class StageSummary(BaseModel):
    name: StageName
    status: StageStatus


class AnalysisRunSummary(BaseModel):
    """A version of the analysis in the document's list (FR-084)."""

    id: str
    status: RunStatus
    created_at: datetime
    created_by: str
    stages: list[StageSummary]
    usage: UsageTotals
    completeness: Completeness


class ProcessDetail(Process):
    """A process with its flowchart."""

    mermaid: str


class ProcessVersion(BaseModel):
    """One version of the initiatives of a process, as S7 stored it."""

    version: int
    run_id: str  # the analysis (version 1) or the regeneration that made it
    created_at: datetime
    current: bool  # the effective one: the latest
    result: InitiativeSet
    predecessors: dict[str, str] = {}  # new initiative → the replaced one it takes values of
    without_successor: list[str] = []  # replaced initiatives whose user values stay behind


@dataclass(frozen=True)
class Outputs:
    """What the stages of an analysis stored, the review log and the analysis as the expert
    left it."""

    run: AnalysisRun
    stored: StoredOutputs
    events: list[ReviewEvent]
    effective: EffectiveState

    @property
    def model(self) -> ProcessModel:  # S5 as stored
        assert self.stored.model is not None
        return self.stored.model

    @property
    def result(self) -> InitiativeSet:  # the latest version of every process, as stored
        return self.stored.initiatives


def price(usage: UsageTotals, settings: Settings) -> UsageTotals:
    """Usage with its cost from LLM_PRICE_* (per million tokens), if both are set."""
    if settings.llm_price_input_per_mtok is None or settings.llm_price_output_per_mtok is None:
        return usage
    cost = (
        usage.in_tokens * settings.llm_price_input_per_mtok
        + usage.out_tokens * settings.llm_price_output_per_mtok
    ) / _MTOK
    return usage.model_copy(update={"cost": cost.quantize(Decimal("0.0001"))})


def run_config(settings: Settings) -> RunConfig:
    quotes = load_config()
    return RunConfig(
        models={
            "extraction": settings.llm_model_extraction or "",
            "analysis": settings.llm_model_analysis or "",
        },
        prompt_versions={
            stage: load_prompt(stage).version for stage in ("discover", "detail", "opportunities")
        },
        structured_mode=settings.llm_structured_mode,
        chunking=dict(asdict(load_chunking())),
        grounding_thresholds={
            "quote_fuzzy_ok": quotes.fuzzy_ok,
            "quote_fuzzy_partial": quotes.fuzzy_partial,
            "quote_min_chars": quotes.min_chars,
            "quote_max_chars": quotes.max_chars,
            "role_min_share": load_role_config().min_share,
        },
    )


def _check_policy(settings: Settings) -> None:
    """Before anything is queued: the document may only go where the policy allows (R-09)."""
    try:
        check_data_policy(settings)
    except LLMNotConfiguredError as exc:
        raise AppError("llm_not_configured", exc.message_ru) from None
    except ExternalLLMNotAllowedError as exc:
        raise AppError("external_llm_not_allowed", exc.message_ru) from None


class AnalysisService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.repos = state.repos
        self.settings = state.settings

    async def start(self, doc_id: str, user: str) -> AnalysisRun:
        try:
            document = await self.repos.documents.get(doc_id)
        except NotFoundError:
            raise AppError("not_found", "Документ не найден.") from None
        if not document.diagnostics.analyzable:
            raise AppError("document_not_analyzable")
        _check_policy(self.settings)
        # One start at a time per document: the active-run check and the insert go together.
        async with self.state.upload_locks.hold(f"analysis:{doc_id}"):
            active = await self.repos.runs.active_for_document(doc_id)
            if active is not None:
                raise AppError("run_in_progress", details={"run_id": active.id})
            run = AnalysisRun(
                id=new_id("run"),
                doc_id=doc_id,
                created_by=user,
                created_at=datetime.now(UTC),
                config_snapshot=run_config(self.settings),
            )
            await self.state.runner.submit(run)
        log.info("analysis.started", run_id=run.id, doc_id=doc_id, user=user)
        return run

    async def get(self, run_id: str) -> AnalysisRun:
        try:
            run = await self.repos.runs.get(run_id)
        except NotFoundError:
            raise AppError("not_found", "Анализ не найден.") from None
        if run.is_active:  # tokens spent so far, for the status screen
            usage = await self.repos.llm_calls.usage_for_run(run_id)
            run = run.model_copy(update={"usage": price(usage, self.settings)})
        return run

    async def resume(self, run_id: str) -> AnalysisRun:
        run = await self.get(run_id)
        if run.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED):
            raise AppError(
                "validation_error", "Продолжить можно только прерванный или упавший анализ."
            )
        _check_policy(self.settings)
        async with self.state.upload_locks.hold(f"analysis:{run.doc_id}"):
            active = await self.repos.runs.active_for_document(run.doc_id)
            if active is not None:
                raise AppError("run_in_progress", details={"run_id": active.id})
            return await self.state.runner.resume(run_id)

    async def cancel(self, run_id: str) -> AnalysisRun:
        await self.get(run_id)
        try:
            return await self.state.runner.cancel(run_id)
        except InvalidTransitionError:
            raise AppError("validation_error", "Анализ уже завершён, отменять нечего.") from None

    async def list_for_document(self, doc_id: str) -> list[AnalysisRunSummary]:
        try:
            await self.repos.documents.get(doc_id)
        except NotFoundError:
            raise AppError("not_found", "Документ не найден.") from None
        return [
            AnalysisRunSummary(
                id=run.id,
                status=run.status,
                created_at=run.created_at,
                created_by=run.created_by,
                stages=[StageSummary(name=s.name, status=s.status) for s in run.stages],
                usage=run.usage,
                completeness=run.completeness,
            )
            for run in await self.repos.runs.list_for_document(doc_id)
        ]

    async def outputs(self, run_id: str, *, initiatives: bool = False) -> Outputs:
        """The stored outputs with the decisions of the review log over them; `initiatives`:
        S7 must be done."""
        run = await self.get(run_id)
        if run.parent_run_id is not None:
            raise AppError(
                "not_found",
                "Это перегенерация инициатив процесса: результаты — в исходном анализе.",
                {"run_id": run.parent_run_id},
            )
        stored = await load_outputs(self.repos, run)
        if stored.analysis is None and initiatives:
            raise AppError("not_found", "Инициативы ещё не готовы.")
        if stored.model is None:
            raise AppError("not_found", "Карта процессов ещё не готова.")
        events = await self.repos.reviews.list_for_run(run_id)
        return Outputs(run, stored, events, stored.effective(events))

    async def regenerate(self, run_id: str, process_id: str, user: str) -> AnalysisRun:
        """Make the initiatives of a process again from the process as the expert left it
        (FR-083): a regeneration run, S6 and S7 of that process alone."""
        outputs = await self.outputs(run_id, initiatives=True)
        process = next((p for p in outputs.effective.model.processes if p.id == process_id), None)
        if process is None:
            raise AppError("not_found", "Процесс не найден.")
        if effective_process(process) is None:
            raise AppError(
                "validation_error",
                "Процесс или все его операции отклонены экспертом: перегенерировать нечего.",
            )
        _check_policy(self.settings)
        analysis = outputs.run
        async with self.state.upload_locks.hold(f"analysis:{analysis.doc_id}"):
            active = await self.repos.runs.active_for_document(analysis.doc_id)
            if active is not None:
                raise AppError("run_in_progress", details={"run_id": active.id})
            reused = (StageName.DISCOVER, StageName.DETAIL)  # the map comes from the analysis
            run = AnalysisRun(
                id=new_id("run"),
                doc_id=analysis.doc_id,
                parent_run_id=analysis.id,
                process_id=process_id,
                created_by=user,
                created_at=datetime.now(UTC),
                stages=[
                    StageState(name=name, status=StageStatus.SKIPPED)
                    if name in reused
                    else StageState(name=name)
                    for name in StageName
                ],
                config_snapshot=run_config(self.settings),
            )
            await self.state.runner.submit(run)
        log.info(
            "initiatives.regeneration_started",
            run_id=run.id,
            parent_run_id=analysis.id,
            process_id=process_id,
            user=user,
        )
        return run

    async def versions(self, run_id: str, process_id: str) -> list[ProcessVersion]:
        """Every version of the initiatives of a process, the first one first."""
        outputs = await self.outputs(run_id, initiatives=True)
        if process_id not in {p.id for p in outputs.model.processes}:
            raise AppError("not_found", "Процесс не найден.")
        stored = outputs.stored
        latest = stored.version_of(process_id)
        assert stored.analysis is not None
        made = outputs.run.stage(StageName.POSTPROCESS).finished_at or outputs.run.created_at
        found = [
            ProcessVersion(
                version=1, run_id=run_id, created_at=made, current=latest == 1,
                result=_of_process(stored.analysis, process_id),
            )
        ]  # fmt: skip
        for item in stored.regenerations:
            if item.version.process_id == process_id:
                found.append(
                    ProcessVersion(
                        version=item.version.version,
                        run_id=item.run.id,
                        created_at=item.run.created_at,
                        current=item.version.version == latest,
                        result=item.version.result,
                        predecessors=item.version.predecessors,
                        without_successor=item.version.without_successor,
                    )
                )
        return sorted(found, key=lambda v: v.version)

    async def processes(self, run_id: str) -> ProcessModel:
        """The process map with the expert's decisions (api.md `ProcessMap`)."""
        return (await self.outputs(run_id)).effective.model

    async def process(self, run_id: str, process_id: str) -> ProcessDetail:
        model = await self.processes(run_id)
        found = next((p for p in model.processes if p.id == process_id), None)
        if found is None:
            raise AppError("not_found", "Процесс не найден.")
        return ProcessDetail.model_validate(
            {**found.model_dump(), "mermaid": process_to_mermaid(found)}
        )


def _of_process(result: InitiativeSet, process_id: str) -> InitiativeSet:
    return InitiativeSet(
        findings=[f for f in result.findings if f.process_id == process_id],
        initiatives=[i for i in result.initiatives if i.process_id == process_id],
        no_change=[n for n in result.no_change if n.process_id == process_id],
        issues=[i for i in result.issues if i.process_id == process_id],
    )
