"""HTML screens (contracts/ui.md). Each screen uses the same services as /api/v1."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from gr.api.deps import StateDep, UserDep, require_user
from gr.api.documents import read_upload
from gr.domain.document import Document, DocumentMetadata, Fragment
from gr.errors import AppError
from gr.services.documents import (
    DocumentService,
    FragmentWithContext,
    MetadataPatch,
    MetaName,
)
from gr.services.parsing import registry
from gr.structure.scoring import MIN_CONFIDENCE
from gr.web.labels import LABELS

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
TREE_OPEN_DEPTH = 1  # tree levels expanded on page load
FORM_TEXT_LIMIT = 2000

templates = Jinja2Templates(directory=TEMPLATES_DIR)  # autoescape is on for .html
templates.env.globals["labels"] = LABELS
templates.env.globals["min_confidence"] = MIN_CONFIDENCE
templates.env.filters["path_quote"] = lambda value: quote(str(value), safe="/")
router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)


def get_service(state: StateDep) -> DocumentService:
    return DocumentService(state)


ServiceDep = Annotated[DocumentService, Depends(get_service)]


@dataclass(frozen=True)
class Highlight:
    """Fragment text split around a quote; each part is rendered escaped."""

    before: str
    quote: str
    after: str


def highlight(fragment: Fragment, start: int | None, end: int | None) -> Highlight:
    """Mark `[start, end)` (document offsets) inside the fragment's own text, if it falls there."""
    text = fragment.text
    if start is None or end is None:
        return Highlight(text, "", "")
    lo = max(start - fragment.start, 0)
    hi = min(end - fragment.start, len(text))
    if lo >= hi:
        return Highlight(text, "", "")
    return Highlight(text[:lo], text[lo:hi], text[hi:])


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value[:FORM_TEXT_LIMIT] or None


def patch_from_form(document: Document, form: Mapping[str, str]) -> MetadataPatch:
    """Only the fields the user changed: an unchanged requisite keeps its citation (FR-006)."""
    fields: dict[MetaName, str | None] = {}
    for name in DocumentMetadata.model_fields:
        if name not in form:
            continue
        value = _clean(form[name])
        if value != getattr(document.metadata, name).value:
            fields[cast(MetaName, name)] = value
    extra: dict[str, Any] = {}
    for name in ("user_source", "relevance_note"):
        if name in form and _clean(form[name]) != getattr(document, name):
            extra[name] = _clean(form[name])
    if "relevance_checked_at" in form:
        raw = form["relevance_checked_at"].strip()
        try:
            checked = date.fromisoformat(raw) if raw else None
        except ValueError:
            raise AppError(
                "validation_error", "Дата проверки актуальности — в формате ГГГГ-ММ-ДД."
            ) from None
        if checked != document.relevance_checked_at:
            extra["relevance_checked_at"] = checked
    return MetadataPatch(fields=fields, **extra)


def document_url(doc_id: str, fragment_id: str | None = None) -> str:
    url = f"/documents/{doc_id}"
    return f"{url}?fragment={quote(fragment_id, safe='/')}#fragment" if fragment_id else url


# --- screen 1: documents ---------------------------------------------------------------------


async def _documents_page(
    request: Request, state: StateDep, service: DocumentService, *, error: AppError | None = None
) -> HTMLResponse:
    settings = state.settings
    return templates.TemplateResponse(
        request,
        "documents.html",
        {
            "documents": await service.list(),
            "llm_policy": settings.llm_data_policy,
            "formats": registry(service.limits).supported(),
            "limits": {
                "max_upload_mb": settings.max_upload_mb,
                "max_pages": settings.max_pages,
                "max_chars": settings.max_chars,
            },
            "error": error,
        },
        status_code=error.status if error else 200,
    )


@router.get("/", response_class=HTMLResponse)
async def documents_page(request: Request, state: StateDep, service: ServiceDep) -> HTMLResponse:
    return await _documents_page(request, state, service)


@router.post("/documents", response_class=HTMLResponse)
async def upload_from_form(
    request: Request, state: StateDep, service: ServiceDep, user: UserDep
) -> Response:
    try:
        upload, note = await read_upload(request, service)
        result = await service.ingest(upload, user, note)
    except AppError as error:
        return await _documents_page(request, state, service, error=error)
    url = document_url(result.document.id) + ("?duplicate=1" if result.duplicate else "")
    return RedirectResponse(url, status_code=303)


# --- screen 2: document ----------------------------------------------------------------------


def _error_page(request: Request, error: AppError) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"error": error}, status_code=error.status
    )


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def document_page(
    request: Request,
    doc_id: str,
    state: StateDep,
    service: ServiceDep,
    fragment: Annotated[str | None, Query()] = None,
    start: Annotated[int | None, Query(ge=0)] = None,
    end: Annotated[int | None, Query(ge=0)] = None,
    duplicate: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    try:
        document = await service.get(doc_id)
        tree = await service.structure(doc_id)
        selected = await service.fragment(doc_id, fragment, True) if fragment else None
    except AppError as error:
        return _error_page(request, error)
    runs = await state.repos.runs.list_for_document(doc_id)
    return templates.TemplateResponse(
        request,
        "document.html",
        {
            "document": document,
            "doc_id": doc_id,
            "tree": tree,
            "by_id": {node.id: node for node in tree.nodes},
            "open_depth": TREE_OPEN_DEPTH,
            "runs": runs,
            "selected": selected,
            "highlight": highlight(selected.fragment, start, end) if selected else None,
            "duplicate": duplicate,
            "llm_policy": state.settings.llm_data_policy,
        },
    )


@router.post("/documents/{doc_id}/metadata", response_class=HTMLResponse)
async def edit_metadata(
    request: Request, doc_id: str, service: ServiceDep, user: UserDep
) -> Response:
    try:
        document = await service.get(doc_id)
        form = await request.form(max_files=0, max_fields=20)
        values = {key: value for key, value in form.items() if isinstance(value, str)}
        patch = patch_from_form(document, values)
        if patch.fields or patch.model_fields_set - {"fields"}:
            await service.patch_metadata(doc_id, patch, user)
    except AppError as error:
        return _error_page(request, error)
    return RedirectResponse(f"/documents/{doc_id}#metadata", status_code=303)


# --- fragment view (HTMX partial) ------------------------------------------------------------


@router.get("/fragments/{doc_id}/{fragment_id:path}", response_class=HTMLResponse)
async def fragment_partial(
    request: Request,
    doc_id: str,
    fragment_id: str,
    service: ServiceDep,
    start: Annotated[int | None, Query(ge=0)] = None,
    end: Annotated[int | None, Query(ge=0)] = None,
) -> HTMLResponse:
    try:
        selected: FragmentWithContext = await service.fragment(doc_id, fragment_id, True)
    except AppError as error:
        return _error_page(request, error)
    return templates.TemplateResponse(
        request,
        "partials/fragment.html",
        {
            "doc_id": doc_id,
            "selected": selected,
            "highlight": highlight(selected.fragment, start, end),
        },
    )
