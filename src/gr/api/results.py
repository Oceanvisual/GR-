"""Initiative read API (T042); calculations are added by US4 and US5."""

from typing import Annotated

from fastapi import APIRouter, Depends

from gr.api.deps import StateDep, require_user
from gr.domain.enums import LegalGate, Scenario
from gr.services.results import InitiativeDetail, InitiativeTable, ResultsService

router = APIRouter(prefix="/api/v1/analyses", dependencies=[Depends(require_user)])


def get_service(state: StateDep) -> ResultsService:
    return ResultsService(state)


ServiceDep = Annotated[ResultsService, Depends(get_service)]


@router.get("/{run_id}/initiatives", response_model=InitiativeTable)
async def initiatives(
    run_id: str,
    service: ServiceDep,
    scenario: Scenario = Scenario.BASE,
    process_id: str | None = None,
    gate: LegalGate | None = None,
    include_rejected: bool = False,
) -> InitiativeTable:
    return await service.table(
        run_id,
        scenario=scenario,
        process_id=process_id,
        gate=gate,
        include_rejected=include_rejected,
    )


@router.get("/{run_id}/initiatives/{initiative_id}", response_model=InitiativeDetail)
async def initiative(run_id: str, initiative_id: str, service: ServiceDep) -> InitiativeDetail:
    return await service.detail(run_id, initiative_id)
