"""Document upload and reading (api.md § Документы; FR-001…FR-006, FR-008, FR-013, NFR-008).

Parsing runs in a worker process (NFR-014). A document is stored only after its text and
structure are, so a stored document is always complete. A PDF with pages without text is
stored with what it has and, when tesseract is there, its pages are recognised in the
background (T070): batch by batch in worker processes, so the progress shows; then the file is
parsed again with the recognised pages and its text and structure are replaced at once.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from gr.api.deps import AppState
from gr.domain.document import (
    Document,
    DocumentMetadata,
    Fragment,
    IngestionDiagnostics,
    MetaField,
    OcrInfo,
    OcrProgress,
)
from gr.domain.enums import DocFormat, OcrStatus, RunStatus, TextLayerStatus
from gr.domain.ids import new_id
from gr.errors import AppError
from gr.ingestion.ocr import OcrPage, TesseractEngine
from gr.ingestion.ocr_probe import OcrAvailability
from gr.ingestion.pdf import OcrOptions
from gr.logging import get_logger
from gr.pipeline.workers import (
    WorkerCrashedError,
    WorkerError,
    WorkerTaskError,
    WorkerTimeoutError,
)
from gr.services.parsing import ParseFailure, ParseLimits, parse_file, recognize_pages
from gr.storage.repositories import ConflictError, DocumentText, NotFoundError

log = get_logger(__name__)

CHUNK = 1 << 16
OCR_BATCH = 5  # pages per worker process: the progress moves between batches
OCR_WORKER_MARGIN_S = 30.0  # rendering and process start, beyond the page timeouts
ACTIVE_OCR = (OcrStatus.QUEUED, OcrStatus.RUNNING)
_WORKER_FAILURES: dict[type[WorkerError], str] = {
    WorkerTimeoutError: "пачка страниц распознавалась дольше отведённого времени",
    WorkerCrashedError: "процесс распознавания аварийно завершился",
    WorkerTaskError: "страницы не удалось подготовить к распознаванию",
}
PREVIEW_CHARS = 160
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f]")
MAX_META_CHARS = 2000
MetaValue = Annotated[str, Field(max_length=MAX_META_CHARS)]
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


class FragmentRef(BaseModel):
    id: str
    label: str
    heading: str | None


class FragmentWithContext(BaseModel):
    fragment: Fragment
    path: list[FragmentRef] = []  # ancestors below the root, outermost first
    parent: Fragment | None = None
    previous: Fragment | None = None
    next: Fragment | None = None


class MetadataPatch(BaseModel):
    fields: dict[MetaName, MetaValue | None] = Field(default_factory=dict)
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

    @property
    def recognising(self) -> bool:
        """Pages without text are being recognised: the upload answers 202 (api.md)."""
        return self.document.diagnostics.ocr.status in ACTIVE_OCR


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
            convert_timeout_s=settings.parse_timeout_s / 2,  # the worker outlives LibreOffice
        )

    @property
    def tmp_dir(self) -> Path:
        return self.state.settings.data_dir / "tmp"

    async def ingest(self, upload: Upload, user: str, source_note: str | None) -> IngestResult:
        try:
            # The same file uploaded twice at once is parsed once; the second gets the first.
            async with self.state.upload_locks.hold(upload.sha256):
                return await self._ingest(upload, user, source_note)
        finally:
            await discard(upload.path)

    async def _ingest(self, upload: Upload, user: str, source_note: str | None) -> IngestResult:
        existing = await self.repos.documents.find_by_sha256(upload.sha256)
        if existing is not None:
            return IngestResult(existing, duplicate=True)
        doc_id = new_id("doc")
        parsed = await self._parse(upload, doc_id)
        diagnostics = self._ocr_plan(parsed.diagnostics)
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
            diagnostics=diagnostics,
            metadata=parsed.metadata,
            structure_mode=parsed.structure_mode,
            user_source=source_note or None,
        )
        # The original goes into the content-addressed store first: a stored file without a
        # row is harmless and reused, a row without its file could never be repaired.
        await self.state.files.adopt(upload.path, upload.sha256)
        text = DocumentText(doc_id=doc_id, full_text=parsed.text, page_offsets=parsed.page_starts)
        try:
            await self.repos.documents.add_parsed(document, text, parsed.fragments)
        except ConflictError:
            # Stored by another process meanwhile: nothing of ours was written.
            existing = await self.repos.documents.find_by_sha256(upload.sha256)
            if existing is None:
                raise
            return IngestResult(existing, duplicate=True)
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
            ocr=diagnostics.ocr.status.value,
        )
        if diagnostics.ocr.status == OcrStatus.QUEUED:
            self.submit_ocr(doc_id)
        return IngestResult(document, duplicate=False)

    async def _parse(self, upload: Upload, doc_id: str) -> Any:
        return await self._parse_path(str(upload.path), doc_id)

    async def _parse_path(self, path: str, doc_id: str, ocr: OcrOptions | None = None) -> Any:
        try:
            result = await self.state.workers.run(parse_file, path, doc_id, self.limits, ocr)
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

    # --- OCR (T070) ---------------------------------------------------------------------

    def _ocr_plan(self, diagnostics: IngestionDiagnostics) -> IngestionDiagnostics:
        """Pages without text: queued when the engine is there, otherwise why not (R-04)."""
        if not diagnostics.empty_pages or diagnostics.ocr.status != OcrStatus.NOT_NEEDED:
            return diagnostics
        pages = diagnostics.empty_pages
        if self.state.ocr.available:
            total = min(len(pages), self.state.settings.ocr_max_pages)
            ocr = OcrInfo(status=OcrStatus.QUEUED, pages=pages[:total],
                          progress=OcrProgress(done=0, total=total))  # fmt: skip
        else:
            ocr = OcrInfo(status=OcrStatus.UNAVAILABLE, pages=pages,
                          error=_unavailable(self.state.ocr))  # fmt: skip
        return _with_ocr(diagnostics, ocr)

    def submit_ocr(self, doc_id: str) -> None:
        self.state.ocr_jobs.submit(doc_id, lambda: self.recognize(doc_id))

    def _editing(self, doc_id: str) -> AbstractAsyncContextManager[None]:
        """One read-modify-write of a document at a time: OCR state, its result, a user's edit."""
        return self.state.upload_locks.hold(f"document:{doc_id}")

    async def _update_ocr(self, doc_id: str, **changes: Any) -> None:
        """Only the OCR state changes: an edit of the requisites made meanwhile stays."""
        async with self._editing(doc_id):
            current = await self.get(doc_id)
            ocr = current.diagnostics.ocr.model_copy(update=changes)
            await self.repos.documents.update(
                current.model_copy(update={"diagnostics": _with_ocr(current.diagnostics, ocr)})
            )

    async def recognize(self, doc_id: str) -> None:
        """Recognise the pages without text of a stored PDF and parse it again (T070).

        Any failure ends in `failed` with its reason, and the user may retry; only a stop of the
        service leaves the document `running`, and the next start resumes it."""
        try:
            await self._recognize(doc_id)
        except Exception as error:  # an unforeseen one too: never `running` for good
            log.error("ocr.crashed", doc_id=doc_id, error=type(error).__name__)
            with contextlib.suppress(Exception):
                reason = "распознавание прервано: внутренняя ошибка"
                await self._update_ocr(doc_id, status=OcrStatus.FAILED, error=reason)

    async def _recognize(self, doc_id: str) -> None:
        document = await self.get(doc_id)
        if document.diagnostics.ocr.status not in ACTIVE_OCR:
            return
        settings = self.state.settings
        numbers = document.diagnostics.empty_pages[: settings.ocr_max_pages]
        engine = self.state.ocr_engine or TesseractEngine(
            settings.ocr_lang, settings.ocr_page_timeout_s, version=self.state.ocr.version
        )
        path = str(self.state.files.path_for(document.sha256))
        done: dict[int, OcrPage] = {}

        def progress() -> OcrProgress:
            return OcrProgress(done=len(done), total=len(numbers))

        await self._update_ocr(doc_id, status=OcrStatus.RUNNING, pages=numbers,
                               progress=progress(), error=None)  # fmt: skip
        log.info("ocr.started", doc_id=doc_id, pages=len(numbers))
        try:
            for start in range(0, len(numbers), OCR_BATCH):
                batch = numbers[start : start + OCR_BATCH]
                pages = await self.state.workers.run(
                    recognize_pages, path, batch, engine, settings.ocr_dpi,
                    timeout_s=len(batch) * settings.ocr_page_timeout_s + OCR_WORKER_MARGIN_S,
                )  # fmt: skip
                done.update({page.number: page for page in pages})
                await self._update_ocr(doc_id, progress=progress())
            options = OcrOptions(dpi=settings.ocr_dpi, max_pages=settings.ocr_max_pages,
                                 min_confidence=settings.ocr_min_confidence, done=done,
                                 version=engine.version)  # fmt: skip
            parsed = await self._parse_path(path, doc_id, options)
        except (AppError, WorkerError) as error:
            reason = (
                error.message_ru
                if isinstance(error, AppError)
                else _WORKER_FAILURES.get(type(error), "ошибка распознавания")
            )
            await self._update_ocr(doc_id, status=OcrStatus.FAILED,
                                   error=f"распознавание прервано: {reason}")  # fmt: skip
            log.warning("ocr.failed", doc_id=doc_id, error=type(error).__name__)
            return
        ocr = parsed.diagnostics.ocr
        if ocr.status != OcrStatus.DONE:  # nothing recognised: the pages still wait for it
            ocr = ocr.model_copy(update={"pages": numbers})
        ocr = ocr.model_copy(update={"progress": progress()})
        text = DocumentText(doc_id=doc_id, full_text=parsed.text, page_offsets=parsed.page_starts)
        async with self._editing(doc_id):
            current = await self.get(doc_id)
            updated = Document.model_validate(
                {
                    **current.model_dump(),
                    "page_count": parsed.page_count,
                    "char_count": len(parsed.text),
                    "diagnostics": _with_ocr(parsed.diagnostics, ocr).model_dump(),
                    "metadata": _kept_metadata(current.metadata, parsed.metadata).model_dump(),
                    "structure_mode": parsed.structure_mode,
                }
            )
            await self.repos.documents.replace_parsed(updated, text, parsed.fragments)
        log.info("ocr.finished", doc_id=doc_id, status=ocr.status.value, pages=len(ocr.pages),
                 confidence=ocr.mean_confidence, fragments=len(parsed.fragments))  # fmt: skip

    async def retry_ocr(self, doc_id: str) -> Document:
        """After a failure, or once the engine is installed (api.md: POST /documents/{id}/ocr).

        The recognised text replaces the document's, so not once it was analysed: the citations
        of an analysis point into the text it read. The lock of analysis starts keeps one from
        slipping in between the check and the queue."""
        async with self.state.upload_locks.hold(f"analysis:{doc_id}"):
            document = await self.get(doc_id)
            if document.diagnostics.ocr.status not in (OcrStatus.FAILED, OcrStatus.UNAVAILABLE):
                raise AppError(
                    "validation_error", "Повторить OCR можно только после ошибки распознавания."
                )
            if not self.state.ocr.available:
                raise AppError("validation_error", _unavailable(self.state.ocr))
            if await self.repos.runs.list_for_document(doc_id):
                raise AppError(
                    "validation_error",
                    "Документ уже анализировался: новый текст разошёлся бы с цитатами анализа. "
                    "Загрузите файл заново после удаления документа.",
                )
            numbers = document.diagnostics.empty_pages[: self.state.settings.ocr_max_pages]
            await self._update_ocr(doc_id, status=OcrStatus.QUEUED, pages=numbers, error=None,
                                   progress=OcrProgress(done=0, total=len(numbers)))  # fmt: skip
        self.submit_ocr(doc_id)
        log.info("ocr.retried", doc_id=doc_id)
        return await self.get(doc_id)

    async def recover_ocr(self) -> list[str]:
        """On startup: documents whose OCR was queued or cut off go back to the queue."""
        resumed = []
        for document in await self.repos.documents.list():
            if document.diagnostics.ocr.status not in ACTIVE_OCR:
                continue
            if self.state.ocr.available:
                self.submit_ocr(document.id)
                resumed.append(document.id)
            else:
                await self._update_ocr(document.id, status=OcrStatus.UNAVAILABLE,
                                       error=_unavailable(self.state.ocr))  # fmt: skip
        if resumed:
            log.info("ocr.resumed_on_start", documents=len(resumed))
        return resumed

    # --- reading -----------------------------------------------------------------------

    async def get(self, doc_id: str) -> Document:
        try:
            return await self.repos.documents.get(doc_id)
        except NotFoundError:
            raise AppError("not_found", "Документ не найден.") from None

    async def list(self) -> list[DocumentSummary]:
        summaries = []
        latest = await self.repos.runs.latest_by_document()
        for document in await self.repos.documents.list():
            run = latest.get(document.id)
            summaries.append(
                DocumentSummary(
                    id=document.id,
                    filename=document.filename,
                    format=document.format,
                    uploaded_at=document.uploaded_at,
                    title=document.metadata.title.value,
                    text_layer=document.diagnostics.text_layer,
                    last_run=LastRun(id=run.id, status=run.status) if run else None,
                )
            )
        return summaries

    async def patch_metadata(self, doc_id: str, patch: MetadataPatch, user: str) -> Document:
        async with self._editing(doc_id):
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
        return _tree(document, await self.repos.fragments.list_for_document(doc_id))

    async def page(
        self, doc_id: str, fragment_id: str | None
    ) -> tuple[Document, StructureTree, FragmentWithContext | None]:
        """Everything the document screen shows, reading the document and its fragments once."""
        document = await self.get(doc_id)
        fragments = await self.repos.fragments.list_for_document(doc_id)
        selected = _with_context(_find(fragments, fragment_id), fragments) if fragment_id else None
        return document, _tree(document, fragments), selected

    async def fragment(
        self, doc_id: str, fragment_id: str, with_context: bool
    ) -> FragmentWithContext:
        await self.get(doc_id)
        if not with_context:
            try:
                fragment = await self.repos.fragments.get(doc_id, fragment_id)
            except NotFoundError:
                raise AppError("not_found", "Фрагмент не найден.") from None
            return FragmentWithContext(fragment=fragment)
        fragments = await self.repos.fragments.list_for_document(doc_id)
        return _with_context(_find(fragments, fragment_id), fragments)


def _find(fragments: list[Fragment], fragment_id: str) -> Fragment:
    found = next((f for f in fragments if f.id == fragment_id), None)
    if found is None:
        raise AppError("not_found", "Фрагмент не найден.")
    return found


def _tree(document: Document, fragments: list[Fragment]) -> StructureTree:
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


def _with_context(fragment: Fragment, fragments: list[Fragment]) -> FragmentWithContext:
    """The fragment with its heading path, parent and neighbours among its siblings."""
    by_id = {f.id: f for f in fragments}
    siblings = [f for f in fragments if f.parent_id == fragment.parent_id]
    index = next(i for i, f in enumerate(siblings) if f.id == fragment.id)
    path: list[FragmentRef] = []
    ancestor = by_id.get(fragment.parent_id or "")
    while ancestor is not None and ancestor.parent_id is not None:
        path.insert(0, FragmentRef(id=ancestor.id, label=ancestor.label, heading=ancestor.heading))
        ancestor = by_id.get(ancestor.parent_id)
    return FragmentWithContext(
        fragment=fragment,
        path=path,
        parent=by_id.get(fragment.parent_id or ""),
        previous=siblings[index - 1] if index > 0 else None,
        next=siblings[index + 1] if index + 1 < len(siblings) else None,
    )


def _unavailable(ocr: OcrAvailability) -> str:
    return f"OCR недоступен: {ocr.reason or 'движок распознавания не найден.'}"


def _with_ocr(diagnostics: IngestionDiagnostics, ocr: OcrInfo) -> IngestionDiagnostics:
    """`analyzable` follows the OCR state: it is derived when the model is validated."""
    return IngestionDiagnostics.model_validate(
        {**diagnostics.model_dump(), "ocr": ocr.model_dump()}
    )


def _kept_metadata(current: DocumentMetadata, found: DocumentMetadata) -> DocumentMetadata:
    """Requisites of the recognised text; those a user entered meanwhile stay (FR-006)."""
    return DocumentMetadata(
        **{
            name: getattr(current, name)
            if getattr(current, name).source == "user"
            else getattr(found, name)
            for name in DocumentMetadata.model_fields
        }
    )
