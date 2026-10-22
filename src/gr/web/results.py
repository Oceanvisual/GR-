"""Initiative table and evidence-rich detail, using the same service as the API."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from gr.api.deps import StateDep, require_user
from gr.domain.enums import LegalGate, Scenario
from gr.errors import AppError
from gr.services.analyses import AnalysisService
from gr.services.results import MODEL_DISCLAIMER, ResultsService
from gr.web.routes import templates

router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)


@router.get("/analyses/{run_id}/initiatives")
async def initiatives_page(
    request: Request,
    run_id: str,
    state: StateDep,
    scenario: Scenario = Scenario.BASE,
    process_id: str | None = None,
    gate: str | None = None,
) -> Response:
    try:
        try:
            selected_gate = LegalGate(gate) if gate else None
        except ValueError:
            raise AppError("validation_error", "Неизвестный статус допустимости.") from None
        table = await ResultsService(state).table(
            run_id, scenario=scenario, process_id=process_id or None, gate=selected_gate
        )
        run = await AnalysisService(state).get(run_id)
        model = await AnalysisService(state).processes(run_id)
    except AppError as exc:
        return templates.TemplateResponse(
            request, "error.html", {"error": exc}, status_code=exc.status
        )
    return templates.TemplateResponse(
        request,
        "initiatives.html",
        {
            "table": table,
            "run": run,
            "processes": model.processes,
            "process_names": {p.id: p.name.value for p in model.processes},
            "process_id": process_id,
            "gate": gate,
        },
    )


@router.get("/analyses/{run_id}/initiatives/{initiative_id}")
async def initiative_page(
    request: Request, run_id: str, initiative_id: str, state: StateDep
) -> Response:
    try:
        service = ResultsService(state)
        detail = await service.detail(run_id, initiative_id)
        run = await service.analyses.get(run_id)
        fragments = await state.repos.fragments.list_for_document(run.doc_id)
    except AppError as exc:
        return templates.TemplateResponse(
            request, "error.html", {"error": exc}, status_code=exc.status
        )
    return templates.TemplateResponse(
        request,
        "initiative.html",
        {
            "detail": detail,
            "run": run,
            "fragment_labels": {f.id: f.label for f in fragments},
            "disclaimer": MODEL_DISCLAIMER,
        },
    )
