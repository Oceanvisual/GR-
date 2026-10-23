"""OCR of pages without a text layer (FR-008, R-04, SC-013, T069).

Without tesseract the engine is checked through a stand-in command; the tests of the real
engine carry the `ocr` marker and are skipped where tesseract is not installed (the Docker
image and CI have it).
"""

import os
import random
import shutil
import stat
import sys
import unicodedata
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from rapidfuzz.distance import Levenshtein

from gr.domain.enums import OcrStatus, TextLayerStatus, TextSource
from gr.domain.ir import full_text
from gr.ingestion.ocr import OcrPage, PageImage, TesseractEngine, ocr_available, parse_tsv
from gr.ingestion.pdf import OcrOptions, PdfExtractor, render_page
from gr.ingestion.pdf_layout import Line

SYNTHETIC = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"
HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)
TSV = "\n".join([
    HEADER,
    "1\t1\t0\t0\t0\t0\t0\t0\t600\t800\t-1\t",
    "4\t1\t1\t1\t1\t0\t60\t100\t300\t30\t-1\t",
    "5\t1\t1\t1\t1\t1\t60\t100\t120\t30\t96.5\t1.1.",
    "5\t1\t1\t1\t1\t2\t200\t102\t160\t28\t93.5\tПропуск",
    "5\t1\t1\t1\t2\t1\t60\t150\t200\t30\t40\tвыдаётся",
    "5\t1\t1\t1\t2\t2\t270\t150\t10\t30\t-1\t ",
])  # fmt: skip
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed"
)


def image(number: int = 1, dpi: int = 144) -> PageImage:
    return PageImage(number=number, dpi=dpi, width=2, height=2, pixels=b"\x00\xff\xff\x00",
                     page_width=300.0, page_height=400.0)  # fmt: skip


def test_tsv_words_become_lines_in_pdf_points() -> None:
    page = parse_tsv(TSV, image())
    assert [line.text for line in page.lines] == ["1.1. Пропуск", "выдаётся"]
    first = page.lines[0]
    scale = 72 / 144  # pixels at 144 dpi → points
    assert (first.x0, first.x1) == (60 * scale, 360 * scale)
    assert first.top == 400 - 100 * scale and first.bottom == 400 - 130 * scale  # y up
    assert first.page == 1 and first.size == 30 * scale
    assert page.confidence == round((96.5 + 93.5 + 40) / 3, 1)  # words only, -1 left out


def test_a_page_is_a_pgm_image() -> None:
    assert image().pgm() == b"P5\n2 2\n255\n\x00\xff\xff\x00"


def test_a_scanned_page_renders_in_grey_without_pillow() -> None:
    document = pdfium.PdfDocument(str(SYNTHETIC / "scan.pdf"))
    try:
        page = document[0]
        rendered = render_page(page, 1, 72)
        page.close()
    finally:
        document.close()
    # A4 at 72 dpi: a pixel per point, give or take the rounding of PDFium.
    assert abs(rendered.width - rendered.page_width) <= 1
    assert abs(rendered.height - rendered.page_height) <= 1
    assert len(rendered.pixels) == rendered.width * rendered.height
    assert rendered.pgm().startswith(f"P5\n{rendered.width} {rendered.height}\n255\n".encode())
    assert len(set(rendered.pixels)) > 2  # shades of grey, not an empty page


def stand_in(tmp_path: Path, body: str) -> str:
    """A `tesseract` that runs `body` with Python: reads the image, answers like the CLI."""
    script = tmp_path / "tesseract"
    script.write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdin.buffer.read()\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_the_engine_reads_tsv_from_the_command(tmp_path: Path) -> None:
    binary = stand_in(tmp_path, f"sys.stdout.write({TSV!r})")
    page = TesseractEngine("rus+eng", 10, binary=binary).recognize(image())
    assert page.error is None and page.lines[0].text == "1.1. Пропуск"


@pytest.mark.parametrize(
    ("body", "timeout_s", "error"),
    [
        ("time.sleep(30)", 0.5, "дольше 0.5 с"),  # a hung page is an error, not a hang
        # A loaded machine may take a while to start the stand-in: its timeout is generous.
        ("sys.stderr.write('Error opening data file rus.traineddata'); sys.exit(1)", 30,
         "rus.traineddata"),
    ],
)  # fmt: skip
def test_a_page_that_fails_is_reported_not_raised(
    tmp_path: Path, body: str, timeout_s: float, error: str
) -> None:
    page = TesseractEngine("rus", timeout_s, binary=stand_in(tmp_path, body)).recognize(image(3))
    assert page.number == 3 and page.lines == [] and page.error is not None
    assert error in page.error


def test_without_the_binary_a_page_is_an_error(tmp_path: Path) -> None:
    page = TesseractEngine("rus", 1, binary=str(tmp_path / "absent")).recognize(image())
    assert page.error is not None and "не запускается" in page.error


def test_without_tesseract_ocr_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    found = ocr_available("rus+eng")
    assert not found.available and found.reason == "tesseract не установлен."
    assert not ocr_available("rus", enabled=False).available


class Scripted:
    """An engine that answers per page: lines with a confidence, or an error."""

    version = "stand-in 1.0"

    def __init__(self, confidence: float | None = 95.0, error: str | None = None) -> None:
        self.confidence, self.error, self.seen = confidence, error, list[int]()

    def recognize(self, page: PageImage) -> OcrPage:
        self.seen.append(page.number)
        if self.error is not None:
            return OcrPage(page.number, error=self.error)
        line = Line(text="1.1. Пропуск выдаётся в течение пяти рабочих дней.", page=page.number,
                    x0=60, x1=500, top=700, bottom=688, size=12, page_width=page.page_width,
                    page_height=page.page_height)  # fmt: skip
        return OcrPage(page.number, [line], self.confidence)


def test_pages_without_text_are_recognised_as_ocr_text() -> None:
    engine = Scripted(confidence=55.0)
    options = OcrOptions(engine=engine, dpi=72, min_confidence=60)
    raw = PdfExtractor(ocr=options).extract((SYNTHETIC / "scan.pdf").read_bytes())
    assert engine.seen == [1]
    assert raw.diagnostics.text_layer == TextLayerStatus.NEEDS_OCR  # what the file is
    ocr = raw.diagnostics.ocr
    assert (ocr.status, ocr.pages, ocr.mean_confidence) == (OcrStatus.DONE, [1], 55.0)
    assert ocr.low_confidence_pages == [1] and ocr.engine_version == "stand-in 1.0"
    assert raw.diagnostics.analyzable  # recognised: the document can be analysed
    assert {b.text_source for b in raw.blocks} == {TextSource.OCR}
    assert "Пропуск выдаётся" in full_text(raw).text


def test_a_failed_recognition_leaves_the_document_unanalysable() -> None:
    options = OcrOptions(engine=Scripted(error="распознавание дольше 60 с"), dpi=72)
    raw = PdfExtractor(ocr=options).extract((SYNTHETIC / "scan.pdf").read_bytes())
    ocr = raw.diagnostics.ocr
    assert ocr.status == OcrStatus.FAILED and ocr.error == "стр. 1: распознавание дольше 60 с"
    assert not raw.diagnostics.analyzable


def test_pages_over_the_limit_are_not_recognised() -> None:
    engine = Scripted()
    options = OcrOptions(engine=engine, dpi=72, max_pages=0)
    raw = PdfExtractor(ocr=options).extract((SYNTHETIC / "scan.pdf").read_bytes())
    assert engine.seen == [] and raw.diagnostics.ocr.status == OcrStatus.FAILED
    assert "OCR_MAX_PAGES" in (raw.diagnostics.ocr.error or "")


def test_a_text_pdf_is_not_recognised() -> None:
    engine = Scripted()
    raw = PdfExtractor(ocr=OcrOptions(engine=engine)).extract(
        (SYNTHETIC / "regulation.pdf").read_bytes()
    )
    assert engine.seen == [] and raw.diagnostics.ocr.status == OcrStatus.NOT_NEEDED


# --- the real engine (marker `ocr`) -------------------------------------------------------


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@pytest.mark.ocr
@needs_tesseract
def test_the_synthetic_scan_is_recognised_with_few_errors() -> None:
    engine = TesseractEngine(os.environ.get("OCR_LANG", "rus+eng"), 60)
    raw = PdfExtractor(ocr=OcrOptions(engine=engine)).extract((SYNTHETIC / "scan.pdf").read_bytes())
    expected = _normalized((SYNTHETIC / "scan.expected.txt").read_text(encoding="utf-8"))
    found = _normalized(" ".join(block.text for block in raw.blocks))
    cer = Levenshtein.distance(found, expected) / len(expected)
    assert cer <= 0.03, f"CER {cer:.2%}"  # SC-013
    assert raw.diagnostics.ocr.mean_confidence is not None
    assert raw.diagnostics.ocr.mean_confidence >= 80


@pytest.mark.ocr
@needs_tesseract
def test_a_noisy_page_has_a_low_confidence() -> None:
    noise = random.Random(7).randbytes(600 * 800)
    page = PageImage(1, 150, 600, 800, noise, 288.0, 384.0)
    found = TesseractEngine(os.environ.get("OCR_LANG", "rus+eng"), 60).recognize(page)
    assert found.error is None
    assert found.confidence is None or found.confidence < 60
