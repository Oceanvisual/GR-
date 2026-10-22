"""Initiative table and evidence-rich detail, using the same service as the API."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from gr.api.deps import StateDep, require_user
from gr.domain.enums import LegalGate, PriorityGroup, Scenario
from gr.errors import AppError
from gr.services.analyses import AnalysisService
from gr.services.results import MODEL_DISCLAIMER, InitiativeDetail, ResultsService
from gr.web.labels import LABELS
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
    group: str | None = None,
) -> Response:
    try:
        try:
            selected_gate = LegalGate(gate) if gate else None
            selected_group = PriorityGroup(group) if group else None
        except ValueError:
            raise AppError("validation_error", "Неизвестный фильтр.") from None
        table = await ResultsService(state).table(
            run_id,
            scenario=scenario,
            process_id=process_id or None,
            gate=selected_gate,
            group=selected_group,
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
            "group": group,
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
            "assumption_labels": assumption_labels(detail),
        },
    )


def assumption_labels(detail: InitiativeDetail) -> dict[str, str]:
    """Readable names of the assumptions on the card: the operation by its name, not its id."""
    operations = {op.id: f"{op.seq + 1} · {op.name}" for op in detail.process.operations}
    names: dict[str, str] = {}
    for item in detail.assumptions:
        label = LABELS["assumption"][item.parameter]
        target = item.target_id or ""
        if item.scope == "operation":
            label = f"{operations.get(target, target)}: {label.lower()}"
        elif item.scope == "initiative_operation":
            op = target.split("/", 1)[-1]
            label = f"{operations.get(op, op)}: {label.lower()}"
        elif item.scope == "process":
            label = f"{label} («{detail.process.name.value}»)"
        names[item.key] = label
    return names
