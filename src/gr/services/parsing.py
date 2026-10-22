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
    RawDocument,
    StructureChoice,
)
from gr.domain.enums import DocFormat, StructureMode, TextLayerStatus
from gr.ingestion.base import IngestionError
from gr.ingestion.convert import CONVERTED, ConvertingExtractor
from gr.ingestion.detect import detect_format
from gr.ingestion.docx import DocxExtractor, ZipLimits
from gr.ingestion.html import HtmlExtractor
from gr.ingestion.ocr import OcrEngine, OcrPage
from gr.ingestion.pdf import OcrOptions, PdfExtractor, recognize_file
from gr.ingestion.registry import ExtractorRegistry
from gr.ingestion.txt import TxtExtractor
from gr.structure.builder import EmptyTextError, build_structure
from gr.structure.metadata import extract_metadata


@dataclass(frozen=True)
class ParseLimits:
    max_pages: int
    max_chars: int
    max_unzipped_bytes: int
    convert_timeout_s: float = 60.0  # LibreOffice, within the worker's own timeout (T075)


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


def registry(limits: ParseLimits, ocr: OcrOptions | None = None) -> ExtractorRegistry:
    docx = DocxExtractor(ZipLimits(max_unzipped_bytes=limits.max_unzipped_bytes))
    return ExtractorRegistry(
        [
            TxtExtractor(),
            docx,
            PdfExtractor(max_pages=limits.max_pages, ocr=ocr),
            HtmlExtractor(),
            *(ConvertingExtractor(fmt, docx, timeout_s=limits.convert_timeout_s)
              for fmt in CONVERTED),
        ]
    )  # fmt: skip


def parse_file(
    path: str, doc_id: str, limits: ParseLimits, ocr: OcrOptions | None = None
) -> ParsedDocument | ParseFailure:
    """`ocr`: pages without text are recognised (or were: `OcrOptions.done`); without it they
    are only reported, and the document service decides when to recognise them (T070)."""
    try:
        return _parse(Path(path).read_bytes(), doc_id, limits, ocr)
    except IngestionError as exc:
        details = dict(exc.details)
        if exc.code == "unsupported_format":
            details.setdefault("supported", registry(limits).supported())
        return ParseFailure(exc.code, exc.message_ru, details)


def recognize_pages(path: str, numbers: list[int], engine: OcrEngine, dpi: int) -> list[OcrPage]:
    """A batch of pages of a stored PDF, in a worker process (T070)."""
    return recognize_file(path, numbers, engine, dpi)


def _parse(data: bytes, doc_id: str, limits: ParseLimits, ocr: OcrOptions | None) -> ParsedDocument:
    fmt = detect_format(data)
    return parse_raw(registry(limits, ocr).get(fmt).extract(data), doc_id, limits)


def parse_raw(raw: RawDocument, doc_id: str, limits: ParseLimits) -> ParsedDocument:
    """Extracted blocks → structure and requisites. Raises `IngestionError` only."""
    fmt = raw.format
    diagnostics = raw.diagnostics
    try:
        # Emptiness is judged after normalisation: invisible characters alone are no text.
        structure = build_structure(raw, doc_id)
    except EmptyTextError:
        if diagnostics.text_layer == TextLayerStatus.OK:
            raise IngestionError("empty_document", "В документе нет текста.") from None
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
    if len(structure.text.text) > limits.max_chars:
        raise IngestionError(
            "document_too_long",
            f"Документ длиннее {limits.max_chars} знаков.",
            {"chars": len(structure.text.text), "max_chars": limits.max_chars},
        )
    warnings = [*diagnostics.warnings, *structure.warnings]
    removed = diagnostics.removed_noise.model_copy(
        update={
            "spps_lines": structure.removed.spps_lines,
            "editorial_notes": structure.removed.editorial_notes,
        }
    )
    diagnostics = IngestionDiagnostics.model_validate(
        {
            **diagnostics.model_dump(),
            "warnings": warnings,
            "structure": StructureChoice.model_validate(structure.choice.model_dump()),
            "removed_noise": removed.model_dump(),
            "numbering_profile": structure.profile,
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
