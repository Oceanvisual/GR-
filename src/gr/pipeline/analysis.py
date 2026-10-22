"""Stages for the runner: S3 discover, S4 detail, S5 validate, S6 opportunities, S7 postprocess
(contracts/llm.md, R-03).

Each stage reads the document and the outputs of earlier stages, and returns its own output,
which the runner stores once. S4 and S6 also store every finished process, so a resumed run
redoes only the rest, and report processes they could not handle as failed items of the run.
S7 also saves the first version of the assumption set, from the profile.

A regeneration run (FR-083, T052) skips S3 and S4: its S5 takes the process as the expert left
it in the analysis, S6 works from that, and S7 makes the next version of the process's
initiatives, with ids unlike the replaced ones and the initiatives each new one replaces.
"""

from dataclasses import dataclass
from typing import Any

from gr.analysis.defaults import build_assumptions
from gr.analysis.versions import successors
from gr.domain.document import Document, Fragment
from gr.domain.enums import StageName
from gr.domain.initiative import InitiativeSet, InitiativeVersion
from gr.domain.process import ProcessModel
from gr.domain.run import AnalysisRun, FailedItem
from gr.extraction.detailing import DetailingResult, detail_processes
from gr.extraction.discovery import DiscoveryResult, discover
from gr.extraction.opportunities import (
    OpportunitiesResult,
    effective_process,
    extract_opportunities,
)
from gr.llm.base import StructuredLLM
from gr.pipeline.outputs import StoredOutputs, load_outputs
from gr.pipeline.stages import Stage, StageContext, StageError, StageResult
from gr.validation.postprocess import postprocess
from gr.validation.process_validator import validate


@dataclass(frozen=True)
class AnalysisSettings:
    max_output_tokens: int
    concurrency: int


async def _document(ctx: StageContext) -> tuple[Document, list[Fragment]]:
    document = await ctx.repos.documents.get(ctx.run.doc_id)
    fragments = await ctx.repos.fragments.list_for_document(ctx.run.doc_id)
    if not fragments:
        raise StageError("document_not_analyzable", "У документа нет разобранной структуры.")
    return document, fragments


def _output(model: Any) -> dict[str, Any]:
    payload: dict[str, Any] = model.model_dump(mode="json")
    return payload


async def _analysis(ctx: StageContext) -> tuple[AnalysisRun, StoredOutputs]:
    """For a regeneration: the analysis it remakes initiatives of, and what it stored."""
    assert ctx.run.parent_run_id is not None
    parent = await ctx.repos.runs.get(ctx.run.parent_run_id)
    stored = await load_outputs(ctx.repos, parent)
    if stored.model is None or stored.analysis is None:
        raise StageError("stage_failed", "Инициативы анализа ещё не готовы.")
    return parent, stored


def _failures(name: StageName, result: InitiativeSet) -> list[FailedItem]:
    serious = {"no_result", "missing_provision", "invalid_initiative", "empty_initiative"}
    failures = {(i.process_id, i.code) for i in result.issues if i.code in serious}
    return [
        FailedItem(stage=name, process_key=pid, error_code=code) for pid, code in sorted(failures)
    ]


class DiscoverStage:
    name = StageName.DISCOVER

    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    async def run(self, ctx: StageContext) -> StageResult:
        document, fragments = await _document(ctx)
        result = await discover(fragments, document.metadata, self.llm, run_id=ctx.run.id)
        return StageResult(output=_output(result))


class DetailStage:
    name = StageName.DETAIL

    def __init__(self, llm: StructuredLLM, settings: AnalysisSettings) -> None:
        self.llm = llm
        self.settings = settings

    async def run(self, ctx: StageContext) -> StageResult:
        document, fragments = await _document(ctx)
        found = DiscoveryResult.model_validate(await ctx.output_of(StageName.DISCOVER))
        result = await detail_processes(
            found,
            fragments,
            document.metadata,
            self.llm,
            run_id=ctx.run.id,
            max_tokens=self.settings.max_output_tokens,
            concurrency=self.settings.concurrency,
            store=ctx.repos.stage_items.for_stage(ctx.run.id, self.name),
            progress=ctx.report_progress,
        )
        failed = [
            FailedItem(stage=self.name, process_key=f.process_key, error_code=f.code)
            for f in result.failed
        ]
        return StageResult(output=_output(result), failed_items=failed)


class ValidateStage:
    name = StageName.VALIDATE

    async def run(self, ctx: StageContext) -> StageResult:
        if ctx.run.parent_run_id is not None:
            return await self._snapshot(ctx)
        _, fragments = await _document(ctx)
        found = DiscoveryResult.model_validate(await ctx.output_of(StageName.DISCOVER))
        detailed = DetailingResult.model_validate(await ctx.output_of(StageName.DETAIL))
        return StageResult(output=_output(validate(found, detailed, fragments)))

    async def _snapshot(self, ctx: StageContext) -> StageResult:
        """A regeneration: the process as the expert left it, the model S6 works from."""
        parent, stored = await _analysis(ctx)
        model = stored.effective(await ctx.repos.reviews.list_for_run(parent.id)).model
        process = next((p for p in model.processes if p.id == ctx.run.process_id), None)
        if process is None or effective_process(process) is None:
            raise StageError(
                "stage_failed", "Процесс или все его операции отклонены: перегенерировать нечего."
            )
        unverified = [u for u in model.unverified if u.process_id == process.id]
        snapshot = model.model_copy(update={"processes": [process], "unverified": unverified})
        return StageResult(output=_output(snapshot))


class OpportunitiesStage:
    name = StageName.OPPORTUNITIES

    def __init__(self, llm: StructuredLLM, settings: AnalysisSettings) -> None:
        self.llm, self.settings = llm, settings

    async def run(self, ctx: StageContext) -> StageResult:
        model = ProcessModel.model_validate(await ctx.output_of(StageName.VALIDATE))
        result = await extract_opportunities(
            model,
            self.llm,
            run_id=ctx.run.id,
            max_tokens=self.settings.max_output_tokens,
            concurrency=self.settings.concurrency,
            store=ctx.repos.stage_items.for_stage(ctx.run.id, self.name),
            progress=ctx.report_progress,
        )
        return StageResult(
            output=_output(result),
            failed_items=[
                FailedItem(stage=self.name, process_key=f.process_id, error_code=f.code)
                for f in result.failed
            ],
        )


class PostprocessStage:
    name = StageName.POSTPROCESS

    async def run(self, ctx: StageContext) -> StageResult:
        _, fragments = await _document(ctx)
        model = ProcessModel.model_validate(await ctx.output_of(StageName.VALIDATE))
        drafts = OpportunitiesResult.model_validate(await ctx.output_of(StageName.OPPORTUNITIES))
        if ctx.run.parent_run_id is not None:
            return await self._version(ctx, model, drafts, fragments)
        result = postprocess(model, drafts, fragments)
        # Assumption set v1 from the profile (llm.md § S6, validation 7); a resumed run that
        # adds nothing new keeps the stored version.
        existing = await ctx.repos.assumptions.latest(ctx.run.id)
        assumptions = build_assumptions(
            model, result.initiatives, existing=existing, run_id=ctx.run.id
        )
        if assumptions is not existing:
            await ctx.repos.assumptions.add(assumptions)
        return StageResult(output=_output(result), failed_items=_failures(self.name, result))

    async def _version(
        self,
        ctx: StageContext,
        model: ProcessModel,
        drafts: OpportunitiesResult,
        fragments: list[Fragment],
    ) -> StageResult:
        """A regeneration: the next version of the process's initiatives. The assumptions of
        the analysis are not touched here: a read fills the keys of the new initiatives, the
        user's values of the replaced ones first (data-model §8)."""
        parent, stored = await _analysis(ctx)
        process_id = ctx.run.process_id
        assert process_id is not None
        version = stored.version_of(process_id) + 1
        result = postprocess(model, drafts, fragments, version=version)
        if not result.initiatives and not result.no_change:
            # Nothing usable came back: the previous version stays the effective one.
            raise StageError(
                "llm_invalid_output",
                "Модель не дала ни одной допустимой инициативы: действует прежняя версия.",
            )
        events = await ctx.repos.reviews.list_for_run(parent.id)
        previous = [i for i in stored.effective(events).initiatives if i.process_id == process_id]
        found = successors(previous, result.initiatives)
        taken_at = ctx.run.stage(StageName.VALIDATE).started_at or ctx.run.created_at
        output = InitiativeVersion(
            process_id=process_id,
            version=version,
            based_on=taken_at,
            result=result,
            predecessors=found,
            without_successor=sorted({i.id for i in previous} - set(found.values())),
        )
        return StageResult(output=_output(output), failed_items=_failures(self.name, result))


def analysis_stages(
    llm: StructuredLLM, settings: AnalysisSettings, *, include_opportunities: bool = True
) -> list[Stage]:
    stages: list[Stage] = [DiscoverStage(llm), DetailStage(llm, settings), ValidateStage()]
    if include_opportunities:
        stages += [OpportunitiesStage(llm, settings), PostprocessStage()]
    return stages
