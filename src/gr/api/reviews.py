"""Expert review API (contracts/api.md § Экспертная проверка, T051)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.review import EntityType, ReviewEvent
from gr.errors import ERROR_RESPONSES
from gr.services.reviews import ReviewIn, ReviewResult, ReviewService

router = APIRouter(
    prefix="/api/v1/analyses", dependencies=[Depends(require_user)], responses=ERROR_RESPONSES
)


def get_service(state: StateDep) -> ReviewService:
    return ReviewService(state)


ServiceDep = Annotated[ReviewService, Depends(get_service)]


@router.post("/{run_id}/reviews", status_code=201, response_model=ReviewResult)
async def add_review(
    run_id: str, body: ReviewIn, service: ServiceDep, user: UserDep
) -> ReviewResult:
    return await service.add(run_id, body, user)


@router.get("/{run_id}/reviews", response_model=list[ReviewEvent])
async def reviews(
    run_id: str,
    service: ServiceDep,
    entity_type: EntityType | None = None,
    entity_id: str | None = None,
    user: str | None = None,
) -> list[ReviewEvent]:
    return await service.history(run_id, entity_type=entity_type, entity_id=entity_id, user=user)
