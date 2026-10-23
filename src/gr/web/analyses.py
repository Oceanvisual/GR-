"""Analysis screens (contracts/ui.md 2a, 3): start, status with polling, the process map.

They use the same service as /api/v1/analyses. Forms post and redirect (no JavaScript needed);
the status block polls itself through HTMX while the run is active. A regeneration of the
initiatives of a process (FR-083) starts from the process map and shows its status like an
analysis does.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from gr.analysis.mermaid import process_to_mermaid
from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.enums import StageName, StageStatus
from gr.domain.run import AnalysisRun
from gr.errors import AppError
from gr.services.analyses import AnalysisService
from gr.web.routes import templates

router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)
COVERAGE_LIST_LIMIT = 50


def get_service(state: StateDep) -> AnalysisService:
    return AnalysisService(state)


ServiceDep = Annotated[AnalysisService, Depends(get_service)]


def _error(request: Request, error: AppError, back: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"error": error, "back": back}, status_code=error.status
    )


def _run_context(run: AnalysisRun) -> dict[str, object]:
    """The results of a regeneration are read in its analysis."""
    validated = run.stage(StageName.VALIDATE).status == StageStatus.SUCCEEDED
    return {
        "run": run,
        "results_id": run.parent_run_id or run.id,
        "map_ready": validated,
        "initiatives_ready": run.stage(StageName.POSTPROCESS).status == StageStatus.SUCCEEDED,
    }


@router.post("/documents/{doc_id}/analyses", response_class=HTMLResponse)
async def start_analysis(
    request: Request, doc_id: str, service: ServiceDep, user: UserDep
) -> Response:
    try:
        run = await service.start(doc_id, user)
    except AppError as error:
        return _error(request, error, back=f"/documents/{doc_id}#runs")
    return RedirectResponse(f"/analyses/{run.id}", status_code=303)


@router.get("/analyses/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str, state: StateDep, service: ServiceDep) -> Response:
    try:
        run = await service.get(run_id)
        document = await state.repos.documents.get(run.doc_id)
        process_name = None
        if run.parent_run_id is not None:  # a regeneration: name the process it remakes
            model = await service.processes(run.parent_run_id)
            found = next((p for p in model.processes if p.id == run.process_id), None)
            process_name = found.name.value if found else run.process_id
    except AppError as error:
        return _error(request, error)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            **_run_context(run),
            "document": document,
            "process_name": process_name,
            "llm_policy": state.settings.llm_data_policy,
        },
    )


@router.get("/analyses/{run_id}/status", response_class=HTMLResponse)
async def run_status(request: Request, run_id: str, service: ServiceDep) -> Response:
    """The status block alone, for HTMX polling."""
    try:
        run = await service.get(run_id)
    except AppError as error:
        return _error(request, error)
    return templates.TemplateResponse(request, "partials/run_status.html", _run_context(run))


@router.post("/analyses/{run_id}/resume", response_class=HTMLResponse)
async def resume(request: Request, run_id: str, service: ServiceDep) -> Response:
    try:
        await service.resume(run_id)
    except AppError as error:
        return _error(request, error, back=f"/analyses/{run_id}")
    return RedirectResponse(f"/analyses/{run_id}", status_code=303)


@router.post("/analyses/{run_id}/cancel", response_class=HTMLResponse)
async def cancel(request: Request, run_id: str, service: ServiceDep) -> Response:
    try:
        await service.cancel(run_id)
    except AppError as error:
        return _error(request, error, back=f"/analyses/{run_id}")
    return RedirectResponse(f"/analyses/{run_id}", status_code=303)


@router.get("/analyses/{run_id}/processes", response_class=HTMLResponse)
async def processes_page(
    request: Request,
    run_id: str,
    state: StateDep,
    service: ServiceDep,
    process: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        outputs = await service.outputs(run_id)
        run, model = outputs.run, outputs.effective.model
        document = await state.repos.documents.get(run.doc_id)
    except AppError as error:
        return _error(request, error, back=f"/analyses/{run_id}")
    fragments = await state.repos.fragments.list_for_document(run.doc_id)
    selected = next((p for p in model.processes if p.id == process), None)
    if selected is None and model.processes:
        selected = model.processes[0]
    unverified = [u for u in model.unverified if selected and u.process_id == selected.id]
    initiatives = None  # of the selected process, once S7 is done
    if selected is not None and outputs.stored.analysis is not None:
        own = [i for i in outputs.effective.initiatives if i.process_id == selected.id]
        initiatives = {
            "version": outputs.stored.version_of(selected.id),
            "count": len(own),
            "stale": sum(i.stale for i in own),
        }
    return templates.TemplateResponse(
        request,
        "processes.html",
        {
            "run": run,
            "document": document,
            "model": model,
            "selected": selected,
            "mermaid": process_to_mermaid(selected) if selected else "",
            "unverified": unverified,
            "initiatives": initiatives,
            "fragment_labels": {f.id: f.label for f in fragments},
            "uncovered": model.coverage.uncovered_action_fragments[:COVERAGE_LIST_LIMIT],
            "llm_policy": state.settings.llm_data_policy,
        },
    )


@router.post("/analyses/{run_id}/processes/{process_id}/regenerate", response_class=HTMLResponse)
async def regenerate(
    request: Request, run_id: str, process_id: str, service: ServiceDep, user: UserDep
) -> Response:
    try:
        run = await service.regenerate(run_id, process_id, user)
    except AppError as error:
        return _error(request, error, back=f"/analyses/{run_id}/processes?process={process_id}")
    return RedirectResponse(f"/analyses/{run.id}", status_code=303)
