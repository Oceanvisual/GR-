"""Analysis API (contracts/api.md § Анализ)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.process import ProcessModel
from gr.domain.run import AnalysisRun
from gr.services.analyses import (
    AnalysisRunSummary,
    AnalysisService,
    ProcessDetail,
    ProcessVersion,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


def get_service(state: StateDep) -> AnalysisService:
    return AnalysisService(state)


ServiceDep = Annotated[AnalysisService, Depends(get_service)]


@router.post("/documents/{doc_id}/analyses", status_code=202, response_model=AnalysisRun)
async def start_analysis(doc_id: str, service: ServiceDep, user: UserDep) -> AnalysisRun:
    return await service.start(doc_id, user)


@router.get("/documents/{doc_id}/analyses", response_model=list[AnalysisRunSummary])
async def list_analyses(doc_id: str, service: ServiceDep) -> list[AnalysisRunSummary]:
    return await service.list_for_document(doc_id)


@router.get("/analyses/{run_id}", response_model=AnalysisRun)
async def get_analysis(run_id: str, service: ServiceDep) -> AnalysisRun:
    return await service.get(run_id)


@router.post("/analyses/{run_id}/resume", status_code=202, response_model=AnalysisRun)
async def resume_analysis(run_id: str, service: ServiceDep) -> AnalysisRun:
    return await service.resume(run_id)


@router.post("/analyses/{run_id}/cancel", response_model=AnalysisRun)
async def cancel_analysis(run_id: str, service: ServiceDep) -> AnalysisRun:
    return await service.cancel(run_id)


@router.get("/analyses/{run_id}/processes", response_model=ProcessModel)
async def get_processes(run_id: str, service: ServiceDep) -> ProcessModel:
    return await service.processes(run_id)


@router.get("/analyses/{run_id}/processes/{process_id}", response_model=ProcessDetail)
async def get_process(run_id: str, process_id: str, service: ServiceDep) -> ProcessDetail:
    return await service.process(run_id, process_id)


@router.post(
    "/analyses/{run_id}/processes/{process_id}/regenerate-initiatives",
    status_code=202,
    response_model=AnalysisRun,
)
async def regenerate_initiatives(
    run_id: str, process_id: str, service: ServiceDep, user: UserDep
) -> AnalysisRun:
    return await service.regenerate(run_id, process_id, user)


@router.get(
    "/analyses/{run_id}/processes/{process_id}/initiative-versions",
    response_model=list[ProcessVersion],
)
async def initiative_versions(
    run_id: str, process_id: str, service: ServiceDep
) -> list[ProcessVersion]:
    return await service.versions(run_id, process_id)
