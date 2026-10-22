"""One repository per table. Domain models go in and out; rows stay inside this module.

`review_events`, `assumption_sets` and `priority_configs` are append-only: their repositories
expose no update or delete.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from gr.domain.assumptions import AssumptionSet
from gr.domain.base import VersionedRecord
from gr.domain.document import Document, Fragment
from gr.domain.enums import RunStatus, StageName
from gr.domain.priority import PriorityConfigVersion
from gr.domain.review import ReviewEvent
from gr.domain.run import ACTIVE_RUN_STATUSES, AnalysisRun, UsageTotals
from gr.storage import db as t
from gr.storage.db import Database


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    """A uniqueness or append-only rule would be broken (duplicate, stale version)."""


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


class _Repository:
    def __init__(self, database: Database) -> None:
        self.db = database


class DocumentRepository(_Repository):
    async def add(self, document: Document) -> None:
        try:
            async with self.db.write() as conn:
                await conn.execute(
                    t.documents.insert().values(
                        id=document.id,
                        sha256=document.sha256,
                        uploaded_at=_utc(document.uploaded_at),
                        payload=_json(document),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"document {document.id} or its sha256 already exists") from exc

    async def add_parsed(
        self, document: Document, text: "DocumentText", fragments: Sequence[Fragment]
    ) -> None:
        """The document with its text and structure in one transaction: all or nothing."""
        if text.doc_id != document.id or any(f.doc_id != document.id for f in fragments):
            raise ValueError("text and fragments must belong to the document")
        try:
            async with self.db.write() as conn:
                await conn.execute(
                    t.documents.insert().values(
                        id=document.id,
                        sha256=document.sha256,
                        uploaded_at=_utc(document.uploaded_at),
                        payload=_json(document),
                    )
                )
                await conn.execute(
                    t.document_texts.insert().values(
                        doc_id=text.doc_id, full_text=text.full_text, page_offsets=text.page_offsets
                    )
                )
                if fragments:
                    await conn.execute(
                        t.fragments.insert(),
                        [
                            {"doc_id": f.doc_id, "id": f.id, "order": f.order, "payload": _json(f)}
                            for f in fragments
                        ],
                    )
        except IntegrityError as exc:
            raise ConflictError(f"document {document.id} or its sha256 already exists") from exc

    async def update(self, document: Document) -> None:
        async with self.db.write() as conn:
            result = await conn.execute(
                update(t.documents)
                .where(t.documents.c.id == document.id)
                .values(payload=_json(document))
            )
        if result.rowcount == 0:
            raise NotFoundError(document.id)

    async def get(self, doc_id: str) -> Document:
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(t.documents.c.payload).where(t.documents.c.id == doc_id)
            )
        if payload is None:
            raise NotFoundError(doc_id)
        return Document.model_validate(payload)

    async def find_by_sha256(self, sha256: str) -> Document | None:
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(t.documents.c.payload).where(t.documents.c.sha256 == sha256)
            )
        return None if payload is None else Document.model_validate(payload)

    async def list(self) -> list[Document]:
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(t.documents.c.payload).order_by(t.documents.c.uploaded_at.desc())
            )
            return [Document.model_validate(payload) for payload in rows]


class FragmentRepository(_Repository):
    async def replace_for_document(self, doc_id: str, items: Sequence[Fragment]) -> None:
        """Structure is rebuilt as a whole (first parse, OCR); never patched piecemeal."""
        if any(fragment.doc_id != doc_id for fragment in items):
            raise ValueError("every fragment must belong to the document being replaced")
        async with self.db.write() as conn:
            await conn.execute(delete(t.fragments).where(t.fragments.c.doc_id == doc_id))
            if items:
                await conn.execute(
                    t.fragments.insert(),
                    [
                        {"doc_id": doc_id, "id": f.id, "order": f.order, "payload": _json(f)}
                        for f in items
                    ],
                )

    async def list_for_document(self, doc_id: str) -> list[Fragment]:
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(t.fragments.c.payload)
                .where(t.fragments.c.doc_id == doc_id)
                .order_by(t.fragments.c.order)
            )
            return [Fragment.model_validate(payload) for payload in rows]

    async def get(self, doc_id: str, fragment_id: str) -> Fragment:
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(t.fragments.c.payload).where(
                    t.fragments.c.doc_id == doc_id, t.fragments.c.id == fragment_id
                )
            )
        if payload is None:
            raise NotFoundError(f"{doc_id}/{fragment_id}")
        return Fragment.model_validate(payload)


class DocumentText(BaseModel):
    doc_id: str
    full_text: str
    page_offsets: list[int] = []


class DocumentTextRepository(_Repository):
    async def put(self, text: DocumentText) -> None:
        async with self.db.write() as conn:
            await conn.execute(
                delete(t.document_texts).where(t.document_texts.c.doc_id == text.doc_id)
            )
            await conn.execute(
                t.document_texts.insert().values(
                    doc_id=text.doc_id, full_text=text.full_text, page_offsets=text.page_offsets
                )
            )

    async def get(self, doc_id: str) -> DocumentText:
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    select(t.document_texts).where(t.document_texts.c.doc_id == doc_id)
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(doc_id)
        return DocumentText(
            doc_id=row.doc_id, full_text=row.full_text, page_offsets=row.page_offsets
        )


class RunRepository(_Repository):
    async def add(self, run: AnalysisRun) -> None:
        try:
            async with self.db.write() as conn:
                await conn.execute(
                    t.analysis_runs.insert().values(
                        id=run.id,
                        doc_id=run.doc_id,
                        status=run.status.value,
                        created_at=_utc(run.created_at),
                        payload=_json(run),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"run {run.id} already exists") from exc

    async def update(self, run: AnalysisRun) -> None:
        async with self.db.write() as conn:
            result = await conn.execute(
                update(t.analysis_runs)
                .where(t.analysis_runs.c.id == run.id)
                .values(status=run.status.value, payload=_json(run))
            )
        if result.rowcount == 0:
            raise NotFoundError(run.id)

    async def get(self, run_id: str) -> AnalysisRun:
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(t.analysis_runs.c.payload).where(t.analysis_runs.c.id == run_id)
            )
        if payload is None:
            raise NotFoundError(run_id)
        return AnalysisRun.model_validate(payload)

    async def list_for_document(self, doc_id: str) -> list[AnalysisRun]:
        return await self._select(t.analysis_runs.c.doc_id == doc_id)

    async def latest_by_document(self) -> dict[str, AnalysisRun]:
        """The newest run of every document, in one query (the documents list)."""
        latest: dict[str, AnalysisRun] = {}
        for run in await self._select():
            latest.setdefault(run.doc_id, run)  # rows come newest first
        return latest

    async def list_by_status(self, statuses: Iterable[RunStatus]) -> list[AnalysisRun]:
        return await self._select(t.analysis_runs.c.status.in_([s.value for s in statuses]))

    async def active_for_document(self, doc_id: str) -> AnalysisRun | None:
        runs = await self._select(
            t.analysis_runs.c.doc_id == doc_id,
            t.analysis_runs.c.status.in_([s.value for s in ACTIVE_RUN_STATUSES]),
        )
        return runs[0] if runs else None

    async def _select(self, *conditions: Any) -> list[AnalysisRun]:
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(t.analysis_runs.c.payload)
                .where(*conditions)
                .order_by(t.analysis_runs.c.created_at.desc(), t.analysis_runs.c.id.desc())
            )
            return [AnalysisRun.model_validate(payload) for payload in rows]


class StageOutputRepository(_Repository):
    """Outputs are written once, when the stage succeeds, and never changed (FR-030)."""

    async def put(self, run_id: str, stage: StageName, payload: dict[str, Any]) -> None:
        try:
            async with self.db.write() as conn:
                await conn.execute(
                    t.stage_outputs.insert().values(
                        run_id=run_id,
                        stage=stage.value,
                        payload=payload,
                        created_at=datetime.now(UTC),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"output of {stage} for {run_id} is already stored") from exc

    async def get(self, run_id: str, stage: StageName) -> dict[str, Any] | None:
        async with self.db.read() as conn:
            payload: dict[str, Any] | None = await conn.scalar(
                select(t.stage_outputs.c.payload).where(
                    t.stage_outputs.c.run_id == run_id, t.stage_outputs.c.stage == stage.value
                )
            )
        return payload

    async def stages_done(self, run_id: str) -> set[StageName]:
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(t.stage_outputs.c.stage).where(t.stage_outputs.c.run_id == run_id)
            )
            return {StageName(stage) for stage in rows}


class LlmCall(BaseModel):
    id: str
    run_id: str | None = None
    stage: str | None = None
    model: str
    attempt: int = Field(ge=1)
    status: str
    in_tokens: int = Field(ge=0)
    out_tokens: int = Field(ge=0)
    tokens_estimated: bool = False
    latency_ms: int = Field(ge=0)
    request_hash: str
    response: dict[str, Any] | None = None
    created_at: datetime


class LlmCallRepository(_Repository):
    async def add(self, call: LlmCall) -> None:
        values = call.model_dump()
        values["created_at"] = _utc(call.created_at)
        values["tokens_estimated"] = int(call.tokens_estimated)
        async with self.db.write() as conn:
            await conn.execute(t.llm_calls.insert().values(**values))

    async def list_for_run(self, run_id: str) -> list[LlmCall]:
        async with self.db.read() as conn:
            rows = (
                await conn.execute(
                    select(t.llm_calls)
                    .where(t.llm_calls.c.run_id == run_id)
                    .order_by(t.llm_calls.c.created_at, t.llm_calls.c.id)
                )
            ).mappings()
            return [LlmCall.model_validate(dict(row)) for row in rows]

    async def usage_for_run(self, run_id: str) -> UsageTotals:
        c = t.llm_calls.c
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(c.in_tokens), 0),
                        func.coalesce(func.sum(c.out_tokens), 0),
                        func.coalesce(func.max(c.tokens_estimated), 0),
                    ).where(c.run_id == run_id)
                )
            ).one()
        return UsageTotals(
            calls=row[0], in_tokens=row[1], out_tokens=row[2], estimated=bool(row[3])
        )


class LlmCacheRepository(_Repository):
    async def get(self, request_hash: str) -> dict[str, Any] | None:
        async with self.db.read() as conn:
            payload: dict[str, Any] | None = await conn.scalar(
                select(t.llm_cache.c.response).where(t.llm_cache.c.request_hash == request_hash)
            )
        return payload

    async def put(self, request_hash: str, model: str, response: dict[str, Any]) -> None:
        """First writer wins: identical requests produce interchangeable responses."""
        async with self.db.write() as conn:
            exists = await conn.scalar(
                select(t.llm_cache.c.request_hash).where(t.llm_cache.c.request_hash == request_hash)
            )
            if exists is None:
                await conn.execute(
                    t.llm_cache.insert().values(
                        request_hash=request_hash,
                        model=model,
                        response=response,
                        created_at=datetime.now(UTC),
                    )
                )


class ReviewEventRepository(_Repository):
    async def add(self, review: ReviewEvent) -> None:
        try:
            async with self.db.write() as conn:
                await conn.execute(
                    t.review_events.insert().values(
                        id=review.id,
                        run_id=review.run_id,
                        entity_type=review.entity_type,
                        entity_id=review.entity_id,
                        created_at=_utc(review.created_at),
                        payload=_json(review),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"review event {review.id} already exists") from exc

    async def list_for_run(self, run_id: str, entity_id: str | None = None) -> list[ReviewEvent]:
        c = t.review_events.c
        conditions = [c.run_id == run_id]
        if entity_id is not None:
            conditions.append(c.entity_id == entity_id)
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(c.payload).where(*conditions).order_by(c.created_at, c.id)
            )
            return [ReviewEvent.model_validate(payload) for payload in rows]


class _VersionedRepository[M: VersionedRecord](_Repository):
    table: Table
    model: type[M]

    async def add(self, item: M) -> None:
        """Insert the next version; a stale or duplicate version is a conflict (If-Match)."""
        c = self.table.c
        try:
            async with self.db.write() as conn:
                latest = await conn.scalar(
                    select(func.max(c.version)).where(c.run_id == item.run_id)
                )
                if item.version != (latest or 0) + 1:
                    raise ConflictError(
                        f"{self.table.name} for {item.run_id}: version {item.version} "
                        f"does not follow {latest or 0}"
                    )
                await conn.execute(
                    self.table.insert().values(
                        run_id=item.run_id,
                        version=item.version,
                        payload=_json(item),
                        created_by=item.created_by,
                        created_at=_utc(item.created_at),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"{self.table.name} version {item.version} exists") from exc

    async def latest(self, run_id: str) -> M | None:
        c = self.table.c
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(c.payload).where(c.run_id == run_id).order_by(c.version.desc()).limit(1)
            )
        return None if payload is None else self.model.model_validate(payload)

    async def get(self, run_id: str, version: int) -> M:
        c = self.table.c
        async with self.db.read() as conn:
            payload = await conn.scalar(
                select(c.payload).where(c.run_id == run_id, c.version == version)
            )
        if payload is None:
            raise NotFoundError(f"{self.table.name} {run_id} v{version}")
        return self.model.model_validate(payload)

    async def versions(self, run_id: str) -> list[int]:
        c = self.table.c
        async with self.db.read() as conn:
            rows = await conn.scalars(
                select(c.version).where(c.run_id == run_id).order_by(c.version)
            )
            return list(rows)


class AssumptionSetRepository(_VersionedRepository[AssumptionSet]):
    table = t.assumption_sets
    model = AssumptionSet


class PriorityConfigRepository(_VersionedRepository[PriorityConfigVersion]):
    table = t.priority_configs
    model = PriorityConfigVersion


class Repositories:
    """All repositories over one database; what services and the runner receive."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self.documents = DocumentRepository(database)
        self.fragments = FragmentRepository(database)
        self.texts = DocumentTextRepository(database)
        self.runs = RunRepository(database)
        self.stage_outputs = StageOutputRepository(database)
        self.llm_calls = LlmCallRepository(database)
        self.llm_cache = LlmCacheRepository(database)
        self.reviews = ReviewEventRepository(database)
        self.assumptions = AssumptionSetRepository(database)
        self.priority_configs = PriorityConfigRepository(database)
