"""US2 stages for the runner: S3 discover, S4 detail, S5 validate (contracts/llm.md, R-03).

Each stage reads the document and the outputs of earlier stages, and returns its own output,
which the runner stores once. S4 also stores every finished process, so a resumed run redoes
only the rest, and reports processes it could not detail as failed items of the run.
"""

from dataclasses import dataclass
from typing import Any

from gr.domain.document import Document, Fragment
from gr.domain.enums import StageName
from gr.domain.process import ProcessModel
from gr.domain.run import FailedItem
from gr.extraction.detailing import DetailingResult, detail_processes
from gr.extraction.discovery import DiscoveryResult, discover
from gr.extraction.opportunities import OpportunitiesResult, extract_opportunities
from gr.llm.base import StructuredLLM
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
        _, fragments = await _document(ctx)
        found = DiscoveryResult.model_validate(await ctx.output_of(StageName.DISCOVER))
        detailed = DetailingResult.model_validate(await ctx.output_of(StageName.DETAIL))
        return StageResult(output=_output(validate(found, detailed, fragments)))


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
        result = postprocess(model, drafts, fragments)
        serious = {"no_result", "missing_provision", "invalid_initiative", "empty_initiative"}
        failures = {(i.process_id, i.code) for i in result.issues if i.code in serious}
        return StageResult(
            output=_output(result),
            failed_items=[
                FailedItem(stage=self.name, process_key=pid, error_code=code)
                for pid, code in sorted(failures)
            ],
        )


def analysis_stages(
    llm: StructuredLLM, settings: AnalysisSettings, *, include_opportunities: bool = True
) -> list[Stage]:
    stages: list[Stage] = [DiscoverStage(llm), DetailStage(llm, settings), ValidateStage()]
    if include_opportunities:
        stages += [OpportunitiesStage(llm, settings), PostprocessStage()]
    return stages
