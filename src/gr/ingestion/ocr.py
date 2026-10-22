"""Recognition of PDF pages without a text layer by the system tesseract (FR-008, R-04, T069).

A page is rendered by PDFium (pdf.py, the only module that may run it) in OCR_DPI shades of
grey and given to tesseract as a PGM image on stdin: no Pillow (R-22), no shell, a timeout per
page. The whole job runs in a worker process (T076), so a synchronous call is enough there.
tesseract's TSV gives the words with their boxes and confidences; the words become lines in PDF
points, so a recognised page goes through the same paragraph layout as a page with a text layer
(pdf_layout.py), and the same cleaning and structure after it. The confidence of a page is the
mean confidence of its words.
"""

import shutil
import subprocess
from dataclasses import dataclass, field
from statistics import fmean
from typing import Protocol

from gr.ingestion.ocr_probe import OcrAvailability, probe_tesseract
from gr.ingestion.pdf_layout import Line

PSM_AUTO = 3  # automatic page segmentation: as good as 6 on one column, safer on two (T073)
_TSV_COLUMNS = 12
_WORD_LEVEL = "5"


@dataclass(frozen=True)
class PageImage:
    """A page rendered in shades of grey: one byte per pixel, rows without padding."""

    number: int  # 1-based
    dpi: int
    width: int
    height: int
    pixels: bytes
    page_width: float  # points
    page_height: float

    def pgm(self) -> bytes:
        return f"P5\n{self.width} {self.height}\n255\n".encode("ascii") + self.pixels


@dataclass(frozen=True)
class OcrPage:
    number: int
    lines: list[Line] = field(default_factory=list)
    confidence: float | None = None  # mean confidence of the words, 0–100
    error: str | None = None  # why the page was not recognised


class OcrEngine(Protocol):
    @property
    def version(self) -> str | None: ...  # of the engine, for the diagnostics

    def recognize(self, image: PageImage) -> OcrPage: ...


def parse_tsv(tsv: str, image: PageImage) -> OcrPage:
    """tesseract's words → lines in PDF points (origin at the bottom, as PDFium's own lines)."""
    scale = 72 / image.dpi
    words_by_line: dict[tuple[str, str, str], list[tuple[str, int, int, int, int]]] = {}
    confidences: list[float] = []
    for row in tsv.splitlines()[1:]:
        cells = row.split("\t")
        if len(cells) != _TSV_COLUMNS or cells[0] != _WORD_LEVEL or not cells[11].strip():
            continue
        left, top, width, height = (int(value) for value in cells[6:10])
        key = (cells[2], cells[3], cells[4])  # block, paragraph, line: tesseract's reading order
        words_by_line.setdefault(key, []).append((cells[11].strip(), left, top, width, height))
        if (confidence := float(cells[10])) >= 0:
            confidences.append(confidence)
    lines = []
    for words in words_by_line.values():
        x0, x1 = min(w[1] for w in words), max(w[1] + w[3] for w in words)
        top, bottom = min(w[2] for w in words), max(w[2] + w[4] for w in words)
        lines.append(
            Line(
                text=" ".join(w[0] for w in words),
                page=image.number,
                x0=x0 * scale,
                x1=x1 * scale,
                top=image.page_height - top * scale,
                bottom=image.page_height - bottom * scale,
                size=(bottom - top) * scale,
                page_width=image.page_width,
                page_height=image.page_height,
            )
        )
    return OcrPage(image.number, lines, round(fmean(confidences), 1) if confidences else None)


class TesseractEngine:
    """The tesseract command line: `stdin stdout -l <lang> --psm 3 --dpi <dpi> tsv`."""

    def __init__(
        self,
        lang: str,
        timeout_s: float,
        *,
        binary: str | None = None,
        version: str | None = None,
        psm: int = PSM_AUTO,
    ) -> None:
        self.lang, self.timeout_s, self.psm, self.version = lang, timeout_s, psm, version
        self.binary = binary or shutil.which("tesseract") or "tesseract"

    def recognize(self, image: PageImage) -> OcrPage:
        command = [self.binary, "stdin", "stdout", "-l", self.lang, "--psm", str(self.psm),
                   "--dpi", str(image.dpi), "tsv"]  # fmt: skip
        try:
            done = subprocess.run(
                command, input=image.pgm(), capture_output=True, timeout=self.timeout_s, check=False
            )  # the child is killed when the timeout expires
        except subprocess.TimeoutExpired:
            return OcrPage(image.number, error=f"распознавание дольше {self.timeout_s:g} с")
        except OSError as exc:
            return OcrPage(image.number, error=f"tesseract не запускается: {exc.strerror or exc}")
        if done.returncode != 0:
            last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
            reason = f": {last[0][:200]}" if last else ""
            return OcrPage(image.number, error=f"tesseract завершился с ошибкой{reason}")
        return parse_tsv(done.stdout.decode("utf-8", "replace"), image)


def ocr_available(lang: str, *, enabled: bool = True) -> OcrAvailability:
    """Is tesseract installed with every language of OCR_LANG (`rus+eng`)?"""
    return probe_tesseract(lang, enabled=enabled)
