"""Expert review: append-only events and the status they produce (data-model.md §8)."""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.enums import ReviewStatus
from gr.domain.run import InvalidTransitionError

EntityType = Literal[
    "process",
    "operation",
    "decision_point",
    "finding",
    "initiative",
    "relation",
    "assumption_set",
    "priority_config",
]
ReviewAction = Literal["confirm", "edit", "reject", "reopen", "set_gate"]

_S = ReviewStatus
# Diagram in data-model §8; a repeated edit keeps the element `edited`.
# `set_gate` changes the initiative's gate, not its review status.
REVIEW_TRANSITIONS: dict[tuple[ReviewStatus, ReviewAction], ReviewStatus] = {
    (_S.PENDING, "confirm"): _S.CONFIRMED,
    (_S.PENDING, "edit"): _S.EDITED,
    (_S.PENDING, "reject"): _S.REJECTED,
    (_S.CONFIRMED, "edit"): _S.EDITED,
    (_S.CONFIRMED, "reject"): _S.REJECTED,
    (_S.CONFIRMED, "reopen"): _S.PENDING,
    (_S.EDITED, "edit"): _S.EDITED,
    (_S.EDITED, "reject"): _S.REJECTED,
    (_S.EDITED, "reopen"): _S.PENDING,
    (_S.REJECTED, "reopen"): _S.PENDING,
}


def next_review_status(current: ReviewStatus, action: ReviewAction) -> ReviewStatus:
    if action == "set_gate":
        return current
    try:
        return REVIEW_TRANSITIONS[(current, action)]
    except KeyError:
        raise InvalidTransitionError("review", current, action) from None


class ReviewEvent(DomainModel):
    id: str = Field(min_length=1)
    run_id: str
    entity_type: EntityType
    entity_id: str = Field(min_length=1)
    action: ReviewAction
    patch: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    comment: str | None = None
    user: str
    created_at: datetime

    @model_validator(mode="after")
    def _action_payload(self) -> Self:
        if self.action == "reject" and not (self.comment and self.comment.strip()):
            raise ValueError("reject requires a comment")
        if self.action in ("edit", "set_gate") and not self.patch:
            raise ValueError(f"{self.action} requires a non-empty patch")
        return self


class ReviewState(DomainModel):
    status: ReviewStatus = ReviewStatus.PENDING
    last_event_id: str | None = None
    edited_fields: list[str] = []

    def apply(self, event: ReviewEvent) -> "ReviewState":
        status = next_review_status(self.status, event.action)
        edited = list(self.edited_fields)
        if event.action == "edit" and event.patch:
            # Reopen keeps the list: earlier edits stay applied by the overlay.
            edited += [field for field in event.patch if field not in edited]
        return ReviewState(status=status, last_event_id=event.id, edited_fields=edited)
