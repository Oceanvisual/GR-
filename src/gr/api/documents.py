"""Documents API (contracts/api.md § Документы)."""

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from gr.api.deps import StateDep, UserDep, require_user
from gr.domain.document import Document
from gr.errors import AppError
from gr.services.documents import (
    CHUNK,
    DocumentService,
    DocumentSummary,
    FragmentWithContext,
    MetadataPatch,
    StructureTree,
    Upload,
    receive_upload,
)

FORM_OVERHEAD = 64 * 1024  # multipart boundaries, headers and the small text fields

router = APIRouter(prefix="/api/v1/documents", dependencies=[Depends(require_user)])


def get_service(state: StateDep) -> DocumentService:
    return DocumentService(state)


ServiceDep = Annotated[DocumentService, Depends(get_service)]


def _too_large(max_bytes: int) -> AppError:
    return AppError(
        "file_too_large", f"Файл больше {max_bytes // (1 << 20)} МБ.", {"max_bytes": max_bytes}
    )


async def _limited(request: Request, limit: int, max_bytes: int) -> AsyncGenerator[bytes, None]:
    """The request body, cut off as soon as it exceeds the limit (no Content-Length trusted)."""
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _too_large(max_bytes)
        yield chunk


async def _file_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while data := await file.read(CHUNK):
        yield data


async def read_upload(request: Request, service: DocumentService) -> tuple[Upload, str | None]:
    """The uploaded file (in a temporary file) and the optional source note of the form."""
    max_bytes = service.state.settings.max_upload_bytes
    limit = max_bytes + FORM_OVERHEAD
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(max_bytes)  # refused before reading the body
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise AppError("validation_error", "Ожидается multipart/form-data с полем file.")
    parser = MultiPartParser(
        request.headers, _limited(request, limit, max_bytes), max_files=1, max_fields=5
    )
    try:
        form = await parser.parse()
    except MultiPartException:
        raise AppError("validation_error", "Не удалось прочитать форму загрузки.") from None
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise AppError("validation_error", "Не передан файл (поле file).")
        note = form.get("source_note")
        upload = await receive_upload(_file_chunks(file), file.filename, service.tmp_dir, max_bytes)
    finally:
        await form.close()
    return upload, note.strip() if isinstance(note, str) else None


@router.post("", status_code=201, response_model=Document)
async def upload_document(
    request: Request, response: Response, service: ServiceDep, user: UserDep
) -> Document:
    upload, note = await read_upload(request, service)
    result = await service.ingest(upload, user, note)
    if result.duplicate:
        response.status_code = 200
        response.headers["X-Duplicate-Of"] = result.document.id
    return result.document


@router.get("", response_model=list[DocumentSummary])
async def list_documents(service: ServiceDep) -> list[DocumentSummary]:
    return await service.list()


@router.get("/{doc_id}", response_model=Document)
async def get_document(doc_id: str, service: ServiceDep) -> Document:
    return await service.get(doc_id)


@router.patch("/{doc_id}/metadata", response_model=Document)
async def patch_metadata(
    doc_id: str, patch: MetadataPatch, service: ServiceDep, user: UserDep
) -> Document:
    return await service.patch_metadata(doc_id, patch, user)


@router.get("/{doc_id}/structure", response_model=StructureTree)
async def get_structure(doc_id: str, service: ServiceDep) -> StructureTree:
    return await service.structure(doc_id)


@router.get("/{doc_id}/fragments/{fragment_id:path}", response_model=FragmentWithContext)
async def get_fragment(
    doc_id: str,
    fragment_id: str,
    service: ServiceDep,
    with_context: Annotated[bool, Query()] = False,
) -> FragmentWithContext:
    return await service.fragment(doc_id, fragment_id, with_context)
