"""Document, intermediate representation and structure fragments (data-model.md §2)."""

import re
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from gr.domain.base import DomainModel
from gr.domain.enums import (
    DocFormat,
    FragmentKind,
    GroundingStatus,
    NumberingProfile,
    OcrStatus,
    StructureMode,
    TextLayerStatus,
    TextSource,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TableCell(DomainModel):
    table: int = Field(ge=0)
    row: int = Field(ge=0)
    col: int = Field(ge=0)


class Block(DomainModel):
    """One unit of the intermediate representation: paragraph, table cell, list item (R-22)."""

    text: str
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    indent: float | None = None
    font_size: float | None = Field(default=None, gt=0)
    bold: bool | None = None
    align: Literal["left", "center", "right", "justify"] | None = None
    style: str | None = None
    list_level: int | None = Field(default=None, ge=0)
    list_marker: str | None = None
    table: TableCell | None = None
    html_tag: str | None = None
    text_source: TextSource = TextSource.NATIVE

    @field_validator("bbox")
    @classmethod
    def _bbox_ordered(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if value is not None:
            x0, y0, x1, y1 = value
            if x0 > x1 or y0 > y1:
                raise ValueError("bbox must be (x0, y0, x1, y1) with x0 <= x1 and y0 <= y1")
        return value


class OcrProgress(DomainModel):
    done: int = Field(ge=0)  # pages recognised so far
    total: int = Field(ge=0)


class OcrInfo(DomainModel):
    status: OcrStatus = OcrStatus.NOT_NEEDED
    pages: list[int] = []  # recognised (done) or waiting for OCR (queued, running, failed)
    progress: OcrProgress | None = None
    mean_confidence: float | None = Field(default=None, ge=0, le=100)
    low_confidence_pages: list[int] = []
    engine_version: str | None = None
    error: str | None = None


class RemovedNoise(DomainModel):
    spps_lines: int = Field(default=0, ge=0)
    header_footer_lines: int = Field(default=0, ge=0)
    editorial_notes: int = Field(default=0, ge=0)


class StructureChoice(DomainModel):
    """Which structure strategy won and how the competitors scored (R-22)."""

    strategy: str
    confidence: float = Field(ge=0, le=1)
    scores: dict[str, float] = {}
    failed_strategies: dict[str, str] = {}


class IngestionDiagnostics(DomainModel):
    text_layer: TextLayerStatus
    empty_pages: list[int] = []
    encoding: str | None = None
    warnings: list[str] = []
    numbering_restored: bool = False
    numbering_profile: NumberingProfile = NumberingProfile.GENERIC
    ocr: OcrInfo = OcrInfo()
    removed_noise: RemovedNoise = RemovedNoise()
    structure: StructureChoice | None = None
    columns_pages: list[int] = []
    analyzable: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_analyzable(cls, data: Any) -> Any:
        # Derived, never trusted from input: a stored payload cannot claim analyzability.
        if isinstance(data, dict):
            data = {**data, "analyzable": _is_analyzable(data)}
        return data


def _is_analyzable(data: dict[str, Any]) -> bool:
    text_layer = TextLayerStatus(data["text_layer"]) if "text_layer" in data else None
    ocr = data.get("ocr")
    if isinstance(ocr, OcrInfo):
        ocr_status = ocr.status
    elif isinstance(ocr, dict) and "status" in ocr:
        ocr_status = OcrStatus(ocr["status"])
    else:
        ocr_status = OcrStatus.NOT_NEEDED
    if ocr_status in (OcrStatus.QUEUED, OcrStatus.RUNNING):
        return False
    if text_layer in (TextLayerStatus.NEEDS_OCR, TextLayerStatus.EMPTY):
        return ocr_status == OcrStatus.DONE
    return text_layer is not None


class RawDocument(DomainModel):
    format: DocFormat
    blocks: list[Block]
    page_count: int | None = Field(default=None, ge=0)
    diagnostics: IngestionDiagnostics


class GroundingResult(DomainModel):
    status: GroundingStatus
    score: float = Field(ge=0, le=100)
    matched_start: int | None = Field(default=None, ge=0)
    matched_end: int | None = Field(default=None, ge=0)
    original_fragment_id: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.matched_start is None) != (self.matched_end is None):
            raise ValueError("matched_start and matched_end are set together")
        if (
            self.matched_start is not None
            and self.matched_end is not None
            and self.matched_start >= self.matched_end
        ):
            raise ValueError("matched_start must be < matched_end")
        if self.status == GroundingStatus.RELOCATED and not self.original_fragment_id:
            raise ValueError("relocated citation must keep original_fragment_id")
        return self


class Citation(DomainModel):
    fragment_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    grounding: GroundingResult


class MetaField(DomainModel):
    value: str | None = None
    citation: Citation | None = None
    source: Literal["document", "user"] = "document"


class DocumentMetadata(DomainModel):
    act_type: MetaField = MetaField()
    issuer: MetaField = MetaField()
    date: MetaField = MetaField()
    number: MetaField = MetaField()
    title: MetaField = MetaField()
    edition_date: MetaField = MetaField()
    minjust_registration: MetaField = MetaField()


class Fragment(DomainModel):
    id: str = Field(min_length=1)
    doc_id: str
    kind: FragmentKind
    number: str | None = None
    label: str
    heading: str | None = None
    text: str
    start: int = Field(ge=0)
    end: int
    pages: list[int] = []
    parent_id: str | None = None
    order: int = Field(ge=0)
    text_source: TextSource = TextSource.NATIVE
    repealed: bool = False
    notes: list[str] = []
    approved_doc: str | None = None

    @model_validator(mode="after")
    def _span(self) -> Self:
        if self.start >= self.end:
            raise ValueError(f"fragment {self.id}: start must be < end")
        if any(page < 1 for page in self.pages):
            raise ValueError(f"fragment {self.id}: pages are 1-based")
        if self.parent_id == self.id:
            raise ValueError(f"fragment {self.id} cannot be its own parent")
        return self


class Document(DomainModel):
    id: str = Field(pattern=r"^doc_[0-9A-Z]{26}$")
    filename: str = Field(min_length=1, max_length=255)
    format: DocFormat
    size_bytes: int = Field(ge=0)
    sha256: str
    uploaded_at: datetime
    uploaded_by: str
    page_count: int | None = Field(default=None, ge=0)
    char_count: int = Field(ge=0)
    diagnostics: IngestionDiagnostics
    metadata: DocumentMetadata = DocumentMetadata()
    structure_mode: StructureMode
    user_source: str | None = None
    relevance_checked_at: date | None = None
    relevance_note: str | None = None

    @field_validator("filename")
    @classmethod
    def _no_path(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in (".", "..") or "\x00" in value:
            raise ValueError("filename must not contain a path")
        return value

    @field_validator("sha256")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value
