"""Initiatives, assumptions, economics and priority API (T042, T045, T049).

Assumptions and the priority configuration are versioned: `GET` returns
`ETag: "<version>"`, a change needs `If-Match` with the version it was based on and answers
409 when another change came first.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.assumptions import AssumptionSet
from gr.domain.economics import EconomicsResult
from gr.domain.enums import LegalGate, PriorityGroup, Scenario
from gr.domain.priority import PriorityConfig
from gr.errors import ERROR_RESPONSES, AppError
from gr.services.results import (
    AssumptionsPatch,
    AssumptionsUpdate,
    InitiativeDetail,
    InitiativeTable,
    PriorityUpdate,
    ResultsService,
)

router = APIRouter(
    prefix="/api/v1/analyses", dependencies=[Depends(require_user)], responses=ERROR_RESPONSES
)


def get_service(state: StateDep) -> ResultsService:
    return ResultsService(state)


ServiceDep = Annotated[ResultsService, Depends(get_service)]


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    """`"4"`, `4` or `W/"4"`; anything else, or nothing, is refused."""
    if not value:
        raise AppError(
            "validation_error", "Нужен заголовок If-Match с версией, на которой основано изменение."
        )
    version = value.strip().removeprefix("W/").strip('"')
    if not version.isdigit():
        raise AppError("validation_error", "If-Match: ожидается номер версии.")
    return int(version)


@router.get("/{run_id}/initiatives", response_model=InitiativeTable)
async def initiatives(
    run_id: str,
    service: ServiceDep,
    scenario: Scenario = Scenario.BASE,
    process_id: str | None = None,
    gate: LegalGate | None = None,
    group: PriorityGroup | None = None,
    include_rejected: bool = False,
) -> InitiativeTable:
    return await service.table(
        run_id,
        scenario=scenario,
        process_id=process_id,
        gate=gate,
        group=group,
        include_rejected=include_rejected,
    )


@router.get("/{run_id}/initiatives/{initiative_id}", response_model=InitiativeDetail)
async def initiative(run_id: str, initiative_id: str, service: ServiceDep) -> InitiativeDetail:
    return await service.detail(run_id, initiative_id)


@router.get("/{run_id}/economics", response_model=EconomicsResult)
async def economics(
    run_id: str, service: ServiceDep, scenario: Scenario = Scenario.BASE
) -> EconomicsResult:
    return await service.economics(run_id, scenario)


@router.get("/{run_id}/assumptions", response_model=AssumptionSet)
async def assumptions(run_id: str, service: ServiceDep, response: Response) -> AssumptionSet:
    current = await service.assumptions(run_id)
    response.headers["ETag"] = etag(current.version)
    return current


@router.patch("/{run_id}/assumptions", response_model=AssumptionsUpdate)
async def update_assumptions(
    run_id: str,
    body: AssumptionsPatch,
    service: ServiceDep,
    user: UserDep,
    response: Response,
    scenario: Scenario = Scenario.BASE,
    if_match: Annotated[str | None, Header()] = None,
) -> AssumptionsUpdate:
    update = await service.update_assumptions(
        run_id, body, version=parse_if_match(if_match), user=user, scenario=scenario
    )
    response.headers["ETag"] = etag(update.assumption_set.version)
    return update


@router.get("/{run_id}/priority-config", response_model=PriorityConfig)
async def priority_config(run_id: str, service: ServiceDep, response: Response) -> PriorityConfig:
    current = await service.priority_config(run_id)
    response.headers["ETag"] = etag(current.version)
    return current.config


@router.put("/{run_id}/priority-config", response_model=PriorityUpdate)
async def update_priority_config(
    run_id: str,
    body: PriorityConfig,
    service: ServiceDep,
    user: UserDep,
    response: Response,
    scenario: Scenario = Scenario.BASE,
    if_match: Annotated[str | None, Header()] = None,
) -> PriorityUpdate:
    update = await service.update_priority_config(
        run_id, body, version=parse_if_match(if_match), user=user, scenario=scenario
    )
    response.headers["ETag"] = etag(update.version)
    return update
