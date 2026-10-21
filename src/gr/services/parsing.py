"""Whole-file parsing for a worker process (T076): format detection, extraction, structure and
requisites. Module-level and picklable; failures come back as values so their codes survive the
process boundary."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gr.domain.document import (
    DocumentMetadata,
    Fragment,
    IngestionDiagnostics,
    OcrInfo,
    StructureChoice,
)
from gr.domain.enums import DocFormat, OcrStatus, StructureMode, TextLayerStatus
from gr.ingestion.base import IngestionError
from gr.ingestion.detect import detect_format
from gr.ingestion.docx import DocxExtractor, ZipLimits
from gr.ingestion.pdf import PdfExtractor
from gr.ingestion.registry import ExtractorRegistry
from gr.ingestion.txt import TxtExtractor
from gr.structure.builder import build_structure
from gr.structure.metadata import extract_metadata

OCR_NOT_CONNECTED = (
    "Распознавание сканов (OCR) пока не подключено: страницы без текста не разобраны."
)


@dataclass(frozen=True)
class ParseLimits:
    max_pages: int
    max_chars: int
    max_unzipped_bytes: int


@dataclass(frozen=True)
class ParsedDocument:
    format: DocFormat
    page_count: int | None
    diagnostics: IngestionDiagnostics
    metadata: DocumentMetadata
    structure_mode: StructureMode
    fragments: list[Fragment]
    text: str
    page_starts: list[int]
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseFailure:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def registry(limits: ParseLimits) -> ExtractorRegistry:
    return ExtractorRegistry(
        [
            TxtExtractor(),
            DocxExtractor(ZipLimits(max_unzipped_bytes=limits.max_unzipped_bytes)),
            PdfExtractor(max_pages=limits.max_pages),
        ]
    )


def _ocr_info(diagnostics: IngestionDiagnostics) -> OcrInfo:
    if diagnostics.text_layer in (TextLayerStatus.OK,) or not diagnostics.empty_pages:
        return diagnostics.ocr
    return OcrInfo(
        status=OcrStatus.UNAVAILABLE, pages=diagnostics.empty_pages, error=OCR_NOT_CONNECTED
    )


def parse_file(path: str, doc_id: str, limits: ParseLimits) -> ParsedDocument | ParseFailure:
    try:
        return _parse(Path(path).read_bytes(), doc_id, limits)
    except IngestionError as exc:
        details = dict(exc.details)
        if exc.code == "unsupported_format":
            details.setdefault("supported", registry(limits).supported())
        return ParseFailure(exc.code, exc.message_ru, details)


def _parse(data: bytes, doc_id: str, limits: ParseLimits) -> ParsedDocument:
    fmt = detect_format(data)
    raw = registry(limits).get(fmt).extract(data)
    diagnostics = raw.diagnostics.model_copy(update={"ocr": _ocr_info(raw.diagnostics)})
    if not any(block.text.strip() for block in raw.blocks):
        if diagnostics.text_layer == TextLayerStatus.OK:
            raise IngestionError("empty_document", "В документе нет текста.")
        # A scan: kept with its diagnostics so the user sees why it cannot be analysed.
        return ParsedDocument(
            format=fmt,
            page_count=raw.page_count,
            diagnostics=IngestionDiagnostics.model_validate(diagnostics.model_dump()),
            metadata=DocumentMetadata(),
            structure_mode=StructureMode.PARAGRAPHS,
            fragments=[],
            text="",
            page_starts=[],
        )
    structure = build_structure(raw, doc_id)
    if len(structure.text.text) > limits.max_chars:
        raise IngestionError(
            "document_too_long",
            f"Документ длиннее {limits.max_chars} знаков.",
            {"chars": len(structure.text.text), "max_chars": limits.max_chars},
        )
    warnings = [*diagnostics.warnings, *structure.warnings]
    diagnostics = IngestionDiagnostics.model_validate(
        {
            **diagnostics.model_dump(),
            "warnings": warnings,
            "structure": StructureChoice.model_validate(structure.choice.model_dump()),
        }
    )
    return ParsedDocument(
        format=fmt,
        page_count=raw.page_count,
        diagnostics=diagnostics,
        metadata=extract_metadata(structure.fragments, structure.text.text),
        structure_mode=structure.mode,
        fragments=structure.fragments,
        text=structure.text.text,
        page_starts=list(structure.text.page_starts),
        stats=structure.stats,
    )
