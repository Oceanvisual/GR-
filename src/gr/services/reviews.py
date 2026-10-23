"""Expert review (api.md § Экспертная проверка, FR-080…FR-083, T051).

A decision joins the log only if the whole log still applies with it: every read replays the
log over the stored outputs (overlay.py), so one event it cannot apply would break every screen
of the run. Decisions of a run are therefore written one at a time. A quote the expert gives is
checked against the act like the model's quotes (FR-025) and stored with its result; a quote
the act does not contain is refused, so an edited value is a fact only with a real basis.
Without a fragment the quote is looked for in the whole act, and the smallest fragment that
contains it is its basis. Clearing the legal significance of an operation needs a comment
(data-model §3), as a rejection does.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gr.analysis.overlay import GROUNDED, EffectiveState, ReviewError, check_event, relation_key
from gr.api.deps import AppState
from gr.domain.document import Citation
from gr.domain.enums import InitiativeRelation
from gr.domain.ids import new_id
from gr.domain.initiative import Finding, Initiative
from gr.domain.process import DecisionPoint, Operation, Process
from gr.domain.review import EntityType, ReviewAction, ReviewEvent
from gr.errors import AppError
from gr.grounding.quotes import QuoteIndex
from gr.services.analyses import AnalysisService, Outputs

# Assumptions and the priority configuration change through their own versioned endpoints.
ReviewedType = Literal[
    "process", "operation", "decision_point", "finding", "initiative", "relation"
]


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: ReviewedType
    entity_id: str = Field(min_length=1, max_length=300)
    action: ReviewAction
    patch: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=2000)


class RelationDecision(BaseModel):
    """A relation of two initiatives as the expert set it."""

    initiative_ids: list[str]
    relation: InitiativeRelation


class ReviewResult(BaseModel):
    event: ReviewEvent
    entity: Process | Operation | DecisionPoint | Finding | Initiative | RelationDecision
    stale_initiative_ids: list[str]  # of the run, after the decision


def _previous(state: EffectiveState, body: ReviewIn, patch: Mapping[str, Any]) -> dict[str, Any]:
    """The values the decision replaces, for the history «было → стало»."""
    if body.entity_type == "relation":
        left, right = body.entity_id.split("|")
        found = state.element("initiative", left)
        kind = next(
            (r.relation for r in getattr(found, "relations", []) if r.initiative_id == right),
            None,
        )
        return {"relation": kind}
    item = state.element(body.entity_type, body.entity_id)
    assert item is not None
    if body.action == "set_gate":
        gate = getattr(item, "gate", None)
        return {"status": gate.status if gate else None}
    current = item.model_dump(mode="json")
    previous = {field: current.get(field) for field in patch}
    ratings = patch.get("ratings")
    if isinstance(ratings, Mapping):  # only the criteria the expert rated again
        previous["ratings"] = {c: current["ratings"].get(c) for c in ratings}
    return previous


class ReviewService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.analyses = AnalysisService(state)

    async def add(self, run_id: str, body: ReviewIn, user: str) -> ReviewResult:
        comment = (body.comment or "").strip() or None
        if body.action == "reject" and comment is None:
            raise AppError("validation_error", "Отклонение требует комментария: укажите причину.")
        if body.entity_type == "relation" and len(parts := body.entity_id.split("|")) == 2:
            body = body.model_copy(update={"entity_id": relation_key(*parts)})
        async with self.state.upload_locks.hold(f"reviews:{run_id}"):
            outputs = await self.analyses.outputs(run_id)
            event = await self._event(outputs, body, comment, user)
            try:
                after = outputs.stored.effective([*outputs.events, event])
            except ReviewError as exc:
                raise AppError("validation_error", f"Решение не применяется: {exc}.") from None
            await self.state.repos.reviews.add(event)
        if body.entity_type == "relation":
            entity: Any = RelationDecision(
                initiative_ids=body.entity_id.split("|"),
                relation=InitiativeRelation((event.patch or {})["relation"]),
            )
        else:
            entity = after.element(body.entity_type, body.entity_id)
        return ReviewResult(
            event=event, entity=entity, stale_initiative_ids=after.stale_initiative_ids
        )

    async def _event(
        self, outputs: Outputs, body: ReviewIn, comment: str | None, user: str
    ) -> ReviewEvent:
        state = outputs.effective
        targets = body.entity_id.split("|") if body.entity_type == "relation" else [body.entity_id]
        kind = "initiative" if body.entity_type == "relation" else body.entity_type
        if any(state.element(kind, key) is None for key in targets):
            raise AppError("not_found", "Элемент анализа не найден.")
        operation = state.element(body.entity_type, body.entity_id)
        cleared = (body.patch or {}).get("legally_significant") is False
        if cleared and getattr(operation, "legally_significant", False) and comment is None:
            raise AppError(
                "validation_error",
                "Снять юридическую значимость операции можно только с комментарием.",
            )
        patch = await self._quoted(outputs, body)
        try:
            event = ReviewEvent(
                id=new_id("rev"),
                run_id=outputs.run.id,
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                action=body.action,
                patch=patch,
                comment=comment,
                user=user,
                created_at=datetime.now(UTC),
            )
            check_event(event)
        except ValidationError:
            raise AppError("validation_error", f"Для «{body.action}» нужен patch.") from None
        except ReviewError as exc:
            raise AppError("validation_error", f"Решение не применяется: {exc}.") from None
        if body.action in ("edit", "set_gate") and patch:
            event = event.model_copy(update={"previous": _previous(state, body, patch)})
        return event

    async def _quoted(self, outputs: Outputs, body: ReviewIn) -> dict[str, Any] | None:
        """The patch with each citation checked against the act and stored with its result."""
        if not body.patch:
            return body.patch
        grounded = GROUNDED.get(body.entity_type, frozenset())
        quoted = {
            field: value
            for field, value in body.patch.items()
            if field in grounded and isinstance(value, Mapping) and value.get("citation")
        }
        if not quoted:
            return body.patch
        fragments = await self.state.repos.fragments.list_for_document(outputs.run.doc_id)
        index = QuoteIndex(fragments)
        repealed = {f.id for f in fragments if f.repealed}
        patch = dict(body.patch)
        problems: dict[str, str] = {}
        for field, value in quoted.items():
            cited = value["citation"]
            if not (
                isinstance(cited, Mapping)
                and "quote" in cited
                and set(cited) <= {"fragment_id", "quote"}
                and all(isinstance(cited[k], str) and cited[k].strip() for k in cited)
            ):
                problems[field] = "цитата: ожидается {fragment_id?, quote}"
                continue
            match = index.verify(cited["quote"], cited.get("fragment_id"))
            grounding = match.grounding
            if not grounding.status.passes:
                problems[field] = grounding.reason or "цитата не найдена в тексте акта"
            elif match.fragment_id in repealed:
                problems[field] = "фрагмент утратил силу"
            else:
                citation = Citation(fragment_id=match.fragment_id, quote=cited["quote"].strip(),
                                    grounding=grounding)  # fmt: skip
                patch[field] = {**value, "citation": citation.model_dump(mode="json")}
        if problems:
            raise AppError(
                "validation_error", "Цитата не подтверждена текстом акта.", {"fields": problems}
            )
        return patch

    async def history(
        self,
        run_id: str,
        *,
        entity_type: EntityType | None = None,
        entity_id: str | None = None,
        user: str | None = None,
    ) -> list[ReviewEvent]:
        """The log of the run, the latest decision first (api.md)."""
        await self.analyses.get(run_id)
        events = await self.state.repos.reviews.list_for_run(run_id, entity_id)
        found = [
            e
            for e in events
            if (entity_type is None or e.entity_type == entity_type)
            and (user is None or e.user == user)
        ]
        return sorted(found, key=lambda e: (e.created_at, e.id), reverse=True)
