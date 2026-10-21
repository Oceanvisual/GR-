"""Document upload and reading (api.md § Документы; FR-001…FR-006, FR-013, NFR-008).

Parsing runs in a worker process (NFR-014). A document is stored only after its text and
structure are, so a stored document is always complete.
"""

import asyncio
import hashlib
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from gr.api.deps import AppState
from gr.domain.document import Document, DocumentMetadata, Fragment, MetaField
from gr.domain.enums import DocFormat, RunStatus, TextLayerStatus
from gr.domain.ids import new_id
from gr.errors import AppError
from gr.logging import get_logger
from gr.pipeline.workers import WorkerCrashedError, WorkerTaskError, WorkerTimeoutError
from gr.services.parsing import ParseFailure, ParseLimits, parse_file
from gr.storage.repositories import ConflictError, DocumentText, NotFoundError

log = get_logger(__name__)

CHUNK = 1 << 16
PREVIEW_CHARS = 160
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f]")
MetaName = Literal[
    "act_type", "issuer", "date", "number", "title", "edition_date", "minjust_registration"
]


# --- response models -----------------------------------------------------------------------


class LastRun(BaseModel):
    id: str
    status: RunStatus


class DocumentSummary(BaseModel):
    id: str
    filename: str
    format: DocFormat
    uploaded_at: datetime
    title: str | None
    text_layer: TextLayerStatus
    last_run: LastRun | None = None


class FragmentNode(BaseModel):
    """A fragment without its full text, plus child ids and a preview (api.md)."""

    id: str
    kind: str
    number: str | None
    label: str
    heading: str | None
    start: int
    end: int
    pages: list[int]
    parent_id: str | None
    order: int
    text_source: str
    repealed: bool
    notes: list[str]
    approved_doc: str | None
    children: list[str]
    text_preview: str


class StructureTree(BaseModel):
    mode: str
    stats: dict[str, int]
    nodes: list[FragmentNode]


class FragmentWithContext(BaseModel):
    fragment: Fragment
    parent: Fragment | None = None
    previous: Fragment | None = None
    next: Fragment | None = None


class MetadataPatch(BaseModel):
    fields: dict[MetaName, str | None] = Field(default_factory=dict)
    user_source: str | None = Field(default=None, max_length=2000)
    relevance_checked_at: date | None = None
    relevance_note: str | None = Field(default=None, max_length=2000)


@dataclass(frozen=True)
class Upload:
    path: Path
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class IngestResult:
    document: Document
    duplicate: bool


# --- helpers -------------------------------------------------------------------------------


def safe_filename(name: str | None) -> str:
    """Base name only, no control characters, at most 255 characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = _UNSAFE_FILENAME.sub("", base).strip().strip(".")
    return (base or "document")[:255]


def _open_temp(tmp_dir: Path) -> tuple[int, Path]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix=".incoming-")
    return fd, Path(name)


async def discard(path: Path) -> None:
    await asyncio.to_thread(path.unlink, missing_ok=True)


async def receive_upload(
    chunks: AsyncIterator[bytes], filename: str | None, tmp_dir: Path, max_bytes: int
) -> Upload:
    """Write a stream to a temporary file, hashing as it goes; stop past the size limit."""
    fd, path = await asyncio.to_thread(_open_temp, tmp_dir)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise AppError(
                        "file_too_large",
                        f"Файл больше {max_bytes // (1 << 20)} МБ.",
                        {"max_bytes": max_bytes},
                    )
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        await discard(path)
        raise
    if size == 0:
        await discard(path)
        raise AppError("empty_document", "Файл пустой.")
    name = safe_filename(filename)
    return Upload(path=path, filename=name, size=size, sha256=digest.hexdigest())


class DocumentService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.repos = state.repos
        settings = state.settings
        self.limits = ParseLimits(
            max_pages=settings.max_pages,
            max_chars=settings.max_chars,
            max_unzipped_bytes=settings.max_unzipped_bytes,
        )

    @property
    def tmp_dir(self) -> Path:
        return self.state.settings.data_dir / "tmp"

    async def ingest(self, upload: Upload, user: str, source_note: str | None) -> IngestResult:
        try:
            existing = await self.repos.documents.find_by_sha256(upload.sha256)
            if existing is not None:
                return IngestResult(existing, duplicate=True)
            doc_id = new_id("doc")
            parsed = await self._parse(upload, doc_id)
            document = Document(
                id=doc_id,
                filename=upload.filename,
                format=parsed.format,
                size_bytes=upload.size,
                sha256=upload.sha256,
                uploaded_at=datetime.now(UTC),
                uploaded_by=user,
                page_count=parsed.page_count,
                char_count=len(parsed.text),
                diagnostics=parsed.diagnostics,
                metadata=parsed.metadata,
                structure_mode=parsed.structure_mode,
                user_source=source_note or None,
            )
            await self.repos.texts.put(
                DocumentText(doc_id=doc_id, full_text=parsed.text, page_offsets=parsed.page_starts)
            )
            await self.repos.fragments.replace_for_document(doc_id, parsed.fragments)
            try:
                await self.repos.documents.add(document)
            except ConflictError:
                # The same file finished uploading in parallel: keep that one.
                await self.repos.fragments.replace_for_document(doc_id, [])
                existing = await self.repos.documents.find_by_sha256(upload.sha256)
                if existing is None:
                    raise
                return IngestResult(existing, duplicate=True)
            await self.state.files.adopt(upload.path, upload.sha256)
            log.info(
                "document.uploaded",
                doc_id=doc_id,
                format=parsed.format.value,
                pages=parsed.page_count,
                chars=len(parsed.text),
                fragments=len(parsed.fragments),
                text_layer=parsed.diagnostics.text_layer.value,
                strategy=parsed.diagnostics.structure.strategy
                if parsed.diagnostics.structure
                else None,
            )
            return IngestResult(document, duplicate=False)
        finally:
            await discard(upload.path)

    async def _parse(self, upload: Upload, doc_id: str) -> Any:
        try:
            result = await self.state.workers.run(parse_file, str(upload.path), doc_id, self.limits)
        except WorkerTimeoutError:
            raise AppError(
                "corrupted_file", "Разбор файла занял слишком много времени.", {"reason": "timeout"}
            ) from None
        except (WorkerCrashedError, WorkerTaskError) as exc:
            log.error("document.parse_failed", error=type(exc).__name__)
            raise AppError(
                "corrupted_file", "Файл не удалось разобрать.", {"reason": "parser_error"}
            ) from None
        if isinstance(result, ParseFailure):
            raise AppError(result.code, result.message, result.details)
        return result

    # --- reading -----------------------------------------------------------------------

    async def get(self, doc_id: str) -> Document:
        try:
            return await self.repos.documents.get(doc_id)
        except NotFoundError:
            raise AppError("not_found", "Документ не найден.") from None

    async def list(self) -> list[DocumentSummary]:
        summaries = []
        for document in await self.repos.documents.list():
            runs = await self.repos.runs.list_for_document(document.id)
            summaries.append(
                DocumentSummary(
                    id=document.id,
                    filename=document.filename,
                    format=document.format,
                    uploaded_at=document.uploaded_at,
                    title=document.metadata.title.value,
                    text_layer=document.diagnostics.text_layer,
                    last_run=LastRun(id=runs[0].id, status=runs[0].status) if runs else None,
                )
            )
        return summaries

    async def patch_metadata(self, doc_id: str, patch: MetadataPatch, user: str) -> Document:
        document = await self.get(doc_id)
        metadata: dict[str, MetaField] = {
            name: getattr(document.metadata, name) for name in DocumentMetadata.model_fields
        }
        for name, value in patch.fields.items():
            # A value entered by the user carries no citation and says so (FR-006).
            metadata[name] = MetaField(value=value, source="user")
        update: dict[str, Any] = {"metadata": DocumentMetadata(**metadata)}
        extra = {"user_source", "relevance_checked_at", "relevance_note"}
        for attribute in patch.model_fields_set & extra:
            update[attribute] = getattr(patch, attribute)
        updated = Document.model_validate({**document.model_dump(), **update})
        await self.repos.documents.update(updated)
        log.info(
            "document.metadata_edited",
            doc_id=doc_id,
            user=user,
            fields=sorted(patch.fields) + sorted(set(update) - {"metadata"}),
        )
        return updated

    async def structure(self, doc_id: str) -> StructureTree:
        document = await self.get(doc_id)
        fragments = await self.repos.fragments.list_for_document(doc_id)
        children: dict[str, list[str]] = {}
        for fragment in fragments:
            if fragment.parent_id:
                children.setdefault(fragment.parent_id, []).append(fragment.id)
        nodes = [
            FragmentNode(
                **fragment.model_dump(exclude={"text", "doc_id"}, mode="json"),
                children=children.get(fragment.id, []),
                text_preview=fragment.text[:PREVIEW_CHARS],
            )
            for fragment in fragments
        ]
        stats = {
            "sections": sum(1 for f in fragments if f.kind.value in ("section", "chapter")),
            "articles": sum(1 for f in fragments if f.kind.value == "article"),
            "points": sum(1 for f in fragments if f.kind.value in ("point", "part")),
            "subpoints": sum(1 for f in fragments if f.kind.value == "subpoint"),
            "pages": document.page_count or 0,
            "chars": document.char_count,
        }
        return StructureTree(mode=document.structure_mode.value, stats=stats, nodes=nodes)

    async def fragment(
        self, doc_id: str, fragment_id: str, with_context: bool
    ) -> FragmentWithContext:
        await self.get(doc_id)
        try:
            fragment = await self.repos.fragments.get(doc_id, fragment_id)
        except NotFoundError:
            raise AppError("not_found", "Фрагмент не найден.") from None
        if not with_context:
            return FragmentWithContext(fragment=fragment)
        fragments = await self.repos.fragments.list_for_document(doc_id)
        by_id = {f.id: f for f in fragments}
        siblings = [f for f in fragments if f.parent_id == fragment.parent_id]
        index = next(i for i, f in enumerate(siblings) if f.id == fragment.id)
        return FragmentWithContext(
            fragment=fragment,
            parent=by_id.get(fragment.parent_id or ""),
            previous=siblings[index - 1] if index > 0 else None,
            next=siblings[index + 1] if index + 1 < len(siblings) else None,
        )
