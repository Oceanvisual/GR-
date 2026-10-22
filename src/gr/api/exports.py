"""Export API (contracts/api.md § Экспорт, FR-092, T055): files to save, the schema to check.

The JSON and the CSV come as attachments named after the run and the scenario; the JSON Schema
is generated from the export model, so the published schema and the files cannot drift apart.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.enums import Scenario
from gr.errors import ERROR_RESPONSES
from gr.export.json_export import AnalysisExport, export_schema
from gr.services.exports import ExportService

router = APIRouter(
    prefix="/api/v1", dependencies=[Depends(require_user)], responses=ERROR_RESPONSES
)
CSV: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {}}, "description": "CSV: UTF-8 с BOM, разделитель «;»"}
}


def get_service(state: StateDep) -> ExportService:
    return ExportService(state)


ServiceDep = Annotated[ExportService, Depends(get_service)]


def _attachment(content: bytes | str, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/analyses/{run_id}/export.json", response_model=AnalysisExport)
async def export_json(
    run_id: str, service: ServiceDep, user: UserDep, scenario: Scenario = Scenario.BASE
) -> Response:
    found = await service.analysis(run_id, scenario, user)
    return _attachment(
        found.model_dump_json(), "application/json", f"analysis-{run_id}-{scenario}.json"
    )


@router.get("/analyses/{run_id}/export/initiatives.csv", response_class=Response, responses=CSV)
async def export_initiatives(
    run_id: str, service: ServiceDep, scenario: Scenario = Scenario.BASE
) -> Response:
    data = await service.initiatives_csv(run_id, scenario)
    return _attachment(data, "text/csv; charset=utf-8", f"initiatives-{run_id}-{scenario}.csv")


@router.get("/analyses/{run_id}/export/operations.csv", response_class=Response, responses=CSV)
async def export_operations(run_id: str, service: ServiceDep) -> Response:
    data = await service.operations_csv(run_id)
    return _attachment(data, "text/csv; charset=utf-8", f"operations-{run_id}.csv")


@router.get("/schema/analysis-export.json")
async def analysis_export_schema() -> JSONResponse:
    return JSONResponse(export_schema(), media_type="application/schema+json")
