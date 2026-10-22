"""PDF → IR blocks (R-04, T072, T069). Runs only inside a worker process (T076).

Lines are PDFium's own reconstruction (`get_text_range`, split on CRLF); geometry comes from
the boxes of each line's characters. Pages without text are reported for OCR; given an OCR
engine, they are rendered here (the only module that may run PDFium) and recognised, and their
lines go through the same layout as the others, as text of source `ocr`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import fmean

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from gr.domain.document import IngestionDiagnostics, OcrInfo, RawDocument, RemovedNoise
from gr.domain.enums import DocFormat, OcrStatus, TextLayerStatus, TextSource
from gr.ingestion.base import IngestionError
from gr.ingestion.ocr import OcrEngine, OcrPage, PageImage
from gr.ingestion.pdf_layout import COLUMN_GUTTER, Line, build_blocks

_PASSWORD_ERROR = 4  # FPDF_ERR_PASSWORD
# Fewer letters/digits than this on a page with an image: a scan (perhaps with a stamp or a
# page number in its text layer) that needs OCR. Without an image a short page is just short.
MIN_PAGE_CHARS = 20
DEFAULT_FONT_SIZE = 12.0


def _page_lines(page: pdfium.PdfPage, number: int) -> tuple[list[Line], int]:
    """Lines of one page and the count of its letters and digits.

    PDFium joins text at one height into one line even across a column gutter, so a line is cut
    where the gap between two glyphs is wider than COLUMN_GUTTER font sizes (T079); the pieces
    of a line that is not in columns are joined again by the layout (merge_line_pieces).
    """
    width, height = page.get_size()
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range()
        meaningful = sum(1 for ch in text if ch.isalnum())
        lines: list[Line] = []
        offset = 0
        for raw_line in text.split("\r\n"):
            start, end = offset, offset + len(raw_line)
            offset = end + 2
            if not raw_line.strip():
                continue
            first = start + (len(raw_line) - len(raw_line.lstrip()))
            last = start + len(raw_line.rstrip()) - 1
            size = float(pdfium_raw.FPDFText_GetFontSize(textpage.raw, first))
            for piece_first, piece_last in _pieces(textpage, text, first, last, size):
                lines.append(_line(textpage, text, piece_first, piece_last, size, number,
                                   (width, height)))  # fmt: skip
        return lines, meaningful
    finally:
        textpage.close()


def _pieces(
    textpage: pdfium.PdfTextPage, text: str, first: int, last: int, size: float
) -> list[tuple[int, int]]:
    """Character ranges of a PDFium line, cut at gaps as wide as a column gutter."""
    if not size > 0:
        return [(first, last)]
    pieces: list[tuple[int, int]] = []
    piece_first, previous, right = first, None, 0.0
    for index in range(first, last + 1):
        if text[index].isspace():
            continue
        box = textpage.get_charbox(index)
        if box[2] <= box[0]:
            continue  # no width: a generated or invisible character
        if previous is not None and box[0] - right > COLUMN_GUTTER * size:
            pieces.append((piece_first, previous))
            piece_first = index
        previous, right = index, box[2]
    pieces.append((piece_first, last))
    return pieces


def _line(
    textpage: pdfium.PdfTextPage,
    text: str,
    first: int,
    last: int,
    size: float,
    number: int,
    page_size: tuple[float, float],
) -> Line:
    boxes = [textpage.get_charbox(i) for i in range(first, last + 1)]
    boxes = [b for b in boxes if b[2] > b[0] or b[3] > b[1]] or boxes
    if not size > 0:  # a damaged font dictionary: fall back to the glyph height
        size = max(b[3] for b in boxes) - min(b[1] for b in boxes)
    if not size > 0:
        size = DEFAULT_FONT_SIZE
    return Line(
        text=text[first : last + 1].strip(),
        page=number,
        x0=boxes[0][0],
        x1=max(b[2] for b in boxes),
        top=max(b[3] for b in boxes),
        bottom=min(b[1] for b in boxes),
        size=size,
        page_width=page_size[0],
        page_height=page_size[1],
    )


@dataclass(frozen=True)
class OcrOptions:
    engine: OcrEngine | None = None  # None: only the pages of `done` are known
    dpi: int = 300
    max_pages: int = 150
    min_confidence: float = 60.0  # a page below it is reported as poorly recognised
    done: Mapping[int, OcrPage] = field(default_factory=dict)  # recognised earlier (T070)
    version: str | None = None  # of the engine that recognised `done`


def render_page(page: pdfium.PdfPage, number: int, dpi: int) -> PageImage:
    """The page in shades of grey, one byte per pixel (no Pillow: R-22)."""
    width_pt, height_pt = page.get_size()
    bitmap = page.render(scale=dpi / 72, grayscale=True)
    try:
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        raw = bytes(bitmap.buffer)
    finally:
        bitmap.close()
    rows = (
        raw
        if stride == width
        else b"".join(raw[row * stride : row * stride + width] for row in range(height))
    )
    return PageImage(number, dpi, width, height, rows, width_pt, height_pt)


def _recognize_page(
    document: pdfium.PdfDocument, number: int, engine: OcrEngine, dpi: int
) -> OcrPage:
    page = document[number - 1]
    try:
        image = render_page(page, number, dpi)
    finally:
        page.close()
    return engine.recognize(image)


def recognize_file(path: str, numbers: list[int], engine: OcrEngine, dpi: int) -> list[OcrPage]:
    """Some pages of a stored PDF, in a worker process: OCR of a scan goes page batch by page
    batch, so its progress can be shown (T070)."""
    document = pdfium.PdfDocument(path)
    try:
        return [_recognize_page(document, number, engine, dpi) for number in numbers]
    finally:
        document.close()


def _recognize(
    document: pdfium.PdfDocument, numbers: list[int], options: OcrOptions
) -> tuple[dict[int, list[Line]], OcrInfo]:
    """The lines of each recognised page, and what OCR reports for the document."""
    found: dict[int, list[Line]] = {}
    confidence: dict[int, float] = {}
    errors: list[str] = []
    for number in numbers[: options.max_pages]:
        if number in options.done:
            result = options.done[number]
        elif options.engine is None:
            result = OcrPage(number, error="страница не распознана")
        else:
            result = _recognize_page(document, number, options.engine, options.dpi)
        if result.error is not None:
            errors.append(f"стр. {number}: {result.error}")
            continue
        found[number] = result.lines
        if result.confidence is not None:
            confidence[number] = result.confidence
    if skipped := numbers[options.max_pages :]:
        errors.append(f"страницы после {options.max_pages}-й не распознаны (лимит OCR_MAX_PAGES): "
                      f"{len(skipped)}")  # fmt: skip
    info = OcrInfo(
        status=OcrStatus.DONE if found else OcrStatus.FAILED,
        pages=sorted(found),
        mean_confidence=round(fmean(confidence.values()), 1) if confidence else None,
        low_confidence_pages=sorted(n for n, c in confidence.items() if c < options.min_confidence),
        engine_version=options.version or (options.engine.version if options.engine else None),
        error="; ".join(errors)[:1000] or None,
    )
    return found, info


def _has_image(document: pdfium.PdfDocument, index: int) -> bool:
    try:
        page = document[index]
    except pdfium.PdfiumError:
        return False
    try:
        images = page.get_objects(filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,))
        return next(iter(images), None) is not None
    finally:
        page.close()


def text_layer_status(empty_pages: int, total: int) -> TextLayerStatus:
    if total == 0:
        return TextLayerStatus.EMPTY
    if empty_pages == 0:
        return TextLayerStatus.OK
    if empty_pages == total:
        return TextLayerStatus.NEEDS_OCR
    return TextLayerStatus.PARTIAL


class PdfExtractor:
    format = DocFormat.PDF

    def __init__(self, max_pages: int = 300, ocr: OcrOptions | None = None) -> None:
        self.max_pages = max_pages
        self.ocr = ocr  # None: pages without text are only reported

    def extract(self, data: bytes) -> RawDocument:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == _PASSWORD_ERROR:
                raise IngestionError("encrypted_file", "PDF защищён паролем.") from None
            raise IngestionError("corrupted_file", "Файл повреждён: PDF не читается.") from None
        try:
            total = len(document)
            if total > self.max_pages:
                raise IngestionError(
                    "document_too_long",
                    f"В PDF {total} страниц, допустимо не больше {self.max_pages}.",
                    {"pages": total, "max_pages": self.max_pages},
                )
            pages: list[list[Line]] = []
            empty: list[int] = []
            broken: list[int] = []
            for index in range(total):
                try:
                    page = document[index]
                except pdfium.PdfiumError:
                    broken.append(index + 1)  # a damaged page object: keep the other pages
                    pages.append([])
                    continue
                try:
                    lines, meaningful = _page_lines(page, index + 1)
                except pdfium.PdfiumError:
                    broken.append(index + 1)
                    pages.append([])
                    continue
                finally:
                    page.close()
                if meaningful < MIN_PAGE_CHARS and _has_image(document, index):
                    empty.append(index + 1)  # its few native lines are kept all the same
                pages.append(lines)
            recognized: dict[int, list[Line]] = {}
            ocr = OcrInfo()
            if self.ocr is not None and empty:
                recognized, ocr = _recognize(document, empty, self.ocr)
                for number, lines in recognized.items():
                    pages[number - 1] = lines  # the scan's own text replaces its few native lines
        finally:
            document.close()

        if total and len(broken) == total:
            raise IngestionError("corrupted_file", "Файл повреждён: страницы PDF не читаются.")
        layout = build_blocks(pages)
        blocks = [
            block.model_copy(update={"text_source": TextSource.OCR})
            if block.page in recognized
            else block
            for block in layout.blocks
        ]
        status = text_layer_status(len(empty), total)
        if status == TextLayerStatus.OK and not blocks:
            status = TextLayerStatus.EMPTY
        warnings = []
        if broken:
            warnings.append("повреждённые страницы пропущены: " + ", ".join(map(str, broken)))
        if layout.columns_pages:
            warnings.append(
                "многоколоночная вёрстка на страницах " + ", ".join(map(str, layout.columns_pages))
            )
        return RawDocument(
            format=DocFormat.PDF,
            blocks=blocks,
            page_count=total,
            diagnostics=IngestionDiagnostics(
                text_layer=status,
                empty_pages=empty,
                ocr=ocr,
                warnings=warnings,
                removed_noise=RemovedNoise(header_footer_lines=layout.header_footer_lines),
                columns_pages=layout.columns_pages,
            ),
        )
