"""PDF with a text layer → IR blocks (R-04, T072). Runs only inside a worker process (T076).

Lines are PDFium's own reconstruction (`get_text_range`, split on CRLF); geometry comes from
the boxes of each line's characters. Pages without text are reported for OCR.
"""

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from gr.domain.document import IngestionDiagnostics, RawDocument, RemovedNoise
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.ingestion.base import IngestionError
from gr.ingestion.pdf_layout import Line, build_blocks

_PASSWORD_ERROR = 4  # FPDF_ERR_PASSWORD
# Fewer letters/digits than this on a page with an image: a scan (perhaps with a stamp or a
# page number in its text layer) that needs OCR. Without an image a short page is just short.
MIN_PAGE_CHARS = 20
DEFAULT_FONT_SIZE = 12.0


def _page_lines(page: pdfium.PdfPage, number: int) -> tuple[list[Line], int]:
    """Lines of one page and the count of its letters and digits."""
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
            stripped = raw_line.strip()
            if not stripped:
                continue
            first = start + (len(raw_line) - len(raw_line.lstrip()))
            last = start + len(raw_line.rstrip()) - 1
            boxes = [textpage.get_charbox(i) for i in range(first, last + 1)]
            boxes = [b for b in boxes if b[2] > b[0] or b[3] > b[1]] or boxes
            size = float(pdfium_raw.FPDFText_GetFontSize(textpage.raw, first))
            if not size > 0:  # a damaged font dictionary: fall back to the glyph height
                size = max(b[3] for b in boxes) - min(b[1] for b in boxes)
            if not size > 0:
                size = DEFAULT_FONT_SIZE
            lines.append(
                Line(
                    text=stripped,
                    page=number,
                    x0=boxes[0][0],
                    x1=max(b[2] for b in boxes),
                    top=max(b[3] for b in boxes),
                    bottom=min(b[1] for b in boxes),
                    size=size,
                    page_width=width,
                    page_height=height,
                )
            )
        return lines, meaningful
    finally:
        textpage.close()


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

    def __init__(self, max_pages: int = 300) -> None:
        self.max_pages = max_pages

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
        finally:
            document.close()

        if total and len(broken) == total:
            raise IngestionError("corrupted_file", "Файл повреждён: страницы PDF не читаются.")
        layout = build_blocks(pages)
        status = text_layer_status(len(empty), total)
        if status == TextLayerStatus.OK and not layout.blocks:
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
            blocks=layout.blocks,
            page_count=total,
            diagnostics=IngestionDiagnostics(
                text_layer=status,
                empty_pages=empty,
                warnings=warnings,
                removed_noise=RemovedNoise(header_footer_lines=layout.header_footer_lines),
                columns_pages=layout.columns_pages,
            ),
        )
