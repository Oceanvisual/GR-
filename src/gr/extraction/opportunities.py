"""S6: optimization hypotheses from effective process models, with per-process resume."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from gr.analysis.tech_guard import ProblemCatalog, load_catalog
from gr.domain.enums import ReviewStatus, StageName
from gr.domain.process import Process, ProcessModel
from gr.extraction.detailing import FAILURE_SHARE, ItemStore
from gr.extraction.prompt_files import load_prompt
from gr.extraction.render import escape, render_document
from gr.extraction.schemas import OpportunitiesOut
from gr.llm.base import LLMError, StructuredLLM
from gr.pipeline.stages import StageError

# The layer of the model's elements is for the reader (FR-093): the prompt stays as it was.
_NOT_FOR_S6: dict[str, Any] = {
    "layer": True,
    "operations": {"__all__": {"layer"}},
    "decision_points": {"__all__": {"layer"}},
}


def s6_payload(process: Process, catalog: ProblemCatalog) -> str:
    """The process and the problem catalog, as S6 receives them."""
    return json.dumps(
        {
            "process": process.model_dump(mode="json", exclude=_NOT_FOR_S6),
            "problem_catalog": catalog.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )


class OpportunityDraft(BaseModel):
    process_id: str
    answer: OpportunitiesOut


class OpportunityFailure(BaseModel):
    process_id: str
    code: str


class OpportunitiesResult(BaseModel):
    processes: list[OpportunityDraft] = []
    failed: list[OpportunityFailure] = []
    prompt_version: str


def effective_process(process: Process) -> Process | None:
    if process.review.status == ReviewStatus.REJECTED:
        return None
    operations = [o for o in process.operations if o.review.status != ReviewStatus.REJECTED]
    if not operations:
        return None
    ids = {o.id for o in operations}
    return process.model_copy(
        update={
            "operations": [
                o.model_copy(update={"depends_on": [k for k in o.depends_on if k in ids]})
                for o in operations
            ],
            "decision_points": [
                d
                for d in process.decision_points
                if d.review.status != ReviewStatus.REJECTED and d.after_operation_id in ids
            ],
        }
    )


async def extract_opportunities(
    model: ProcessModel,
    llm: StructuredLLM,
    *,
    run_id: str,
    max_tokens: int,
    concurrency: int,
    store: ItemStore | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    catalog: ProblemCatalog | None = None,
) -> OpportunitiesResult:
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    prompt = load_prompt("opportunities")
    catalog = catalog or load_catalog()
    processes = [p for source in model.processes if (p := effective_process(source)) is not None]
    semaphore = asyncio.Semaphore(concurrency)
    finished = 0

    async def one(process: Process) -> OpportunityDraft | OpportunityFailure:
        nonlocal finished
        try:
            if store is not None and (saved := await store.get(process.id)) is not None:
                return OpportunityDraft.model_validate(saved)
            async with semaphore:
                payload = s6_payload(process, catalog)
                # Quotes in the model are document text: a tag in them must not close the block.
                answer = await llm.generate(
                    schema=OpportunitiesOut,
                    system=prompt.system,
                    user=f"<task>{prompt.task}</task>\n" + render_document(escape(payload), {}),
                    stage=StageName.OPPORTUNITIES,
                    max_tokens=max_tokens,
                    run_id=run_id,
                )
                result = OpportunityDraft(process_id=process.id, answer=answer.value)
            if store is not None:
                await store.put(process.id, result.model_dump(mode="json"))
            return result
        except LLMError as exc:
            return OpportunityFailure(process_id=process.id, code=exc.code)
        finally:
            finished += 1
            if progress is not None:
                await progress(finished, len(processes))

    results = await asyncio.gather(*(one(p) for p in processes))
    failed = [r for r in results if isinstance(r, OpportunityFailure)]
    if results and len(failed) / len(results) > FAILURE_SHARE:
        raise StageError(
            failed[0].code, f"Не удалось получить инициативы для {len(failed)} процессов."
        )
    return OpportunitiesResult(
        processes=[r for r in results if isinstance(r, OpportunityDraft)],
        failed=failed,
        prompt_version=prompt.version,
    )
