"""PDF pages set in columns are read column by column (FR-019, R-22, T079)."""

from pathlib import Path

import pytest
from fpdf import FPDF

from gr.ingestion.pdf import PdfExtractor
from gr.ingestion.pdf_layout import Line, build_blocks, column_starts, in_reading_order

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
FONT = next(
    (path for path in (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                       Path("/System/Library/Fonts/Supplemental/Arial.ttf")) if path.exists()),
    Path("absent"),
)  # fmt: skip
H = 842.0


def _words(text: str) -> str:
    return " ".join(text.split())


def line(text: str, top: float, x0: float, x1: float, size: float = 11) -> Line:
    return Line(text, 1, x0, x1, top, top - 9, size, 595.0, H)


def test_two_columns_written_row_by_row_are_read_in_order() -> None:
    raw = PdfExtractor().extract((SYNTHETIC / "two_columns.pdf").read_bytes())
    expected = (SYNTHETIC / "two_columns.expected.txt").read_text(encoding="utf-8")
    assert _words(" ".join(block.text for block in raw.blocks)) == _words(expected)
    assert raw.diagnostics.columns_pages == [1]
    assert any("многоколоночная вёрстка" in warning for warning in raw.diagnostics.warnings)
    texts = [block.text for block in raw.blocks]
    # A paragraph of the left column does not swallow the item that tops the right one.
    assert "II. Стандарт предоставления услуги" in texts
    assert any(text.startswith("2.1. Срок предоставления услуги") for text in texts)
    # The line across the page stays between the two runs of columns, as its own paragraph.
    across = next(i for i, text in enumerate(texts) if text.startswith("Ниже текст снова"))
    assert texts[across - 1].startswith("2) копию документа")
    assert texts[across + 1].startswith("3) сведения о транспортном средстве")


@pytest.mark.parametrize(
    "path",
    ["synthetic/regulation.pdf", "synthetic/headers.pdf", "npa/reg261.pdf",
     "corpus/lenoblast_93oz_2003.pdf"],
)  # fmt: skip
def test_pages_in_one_column_have_no_columns(path: str) -> None:
    raw = PdfExtractor().extract((FIXTURES / path).read_bytes())
    assert raw.diagnostics.columns_pages == []


def _pdf(rows: list[tuple[str, str]], prose: int = 6) -> bytes:
    """A page of prose in one column, then rows of two cells on the same height."""
    document = FPDF(format="A4")
    document.add_page()
    document.add_font("Main", fname=str(FONT))
    document.set_font("Main", size=11)
    for _ in range(prose):
        document.multi_cell(180, 6.5, "Пункт регламента набран на всю ширину страницы. " * 3)
    for left, right in rows:
        y = document.get_y()
        document.set_xy(15, y)
        document.cell(60, 6.5, left)
        document.set_xy(140, y)
        document.cell(55, 6.5, right)
        document.ln(6.5)
    return bytes(document.output())


@pytest.mark.skipif(not FONT.exists(), reason="a Cyrillic font is needed")
def test_a_signature_line_with_a_wide_gap_stays_one_line() -> None:
    data = _pdf([("Начальник Управления", "И.И. Иванов")])
    raw = PdfExtractor().extract(data)
    assert raw.diagnostics.columns_pages == []
    assert any(_words(b.text).endswith("Начальник Управления И.И. Иванов") for b in raw.blocks)


@pytest.mark.skipif(not FONT.exists(), reason="a Cyrillic font is needed")
def test_short_cells_side_by_side_are_a_table_not_columns() -> None:
    rows = [(f"Показатель {n}", f"{n * 10} дней") for n in range(1, 9)]
    raw = PdfExtractor().extract(_pdf(rows))
    assert raw.diagnostics.columns_pages == []
    text = _words(" ".join(b.text for b in raw.blocks))
    assert "Показатель 1 10 дней" in text and "Показатель 8 80 дней" in text  # row by row


def test_columns_start_where_wide_lines_stand_beside_others() -> None:
    lines = []
    for row in range(8):
        lines += [
            line(f"левая колонка, строка {row} текста", 700 - 14 * row, 46, 250),
            line(f"правая колонка, строка {row} текста", 700 - 14 * row, 315, 520),
        ]
    assert column_starts(lines) == [315]
    ordered = in_reading_order(lines, [315])
    assert [ln.text for ln in ordered[:8]] == [
        f"левая колонка, строка {r} текста" for r in range(8)
    ]
    assert {ln.column for ln in ordered[:8]} == {0} and {ln.column for ln in ordered[8:]} == {1}


def test_an_indented_first_line_is_no_column() -> None:
    lines = []
    for row in range(12):
        x0 = 82 if row % 3 == 0 else 46  # first lines of paragraphs, indented
        lines.append(line(f"текст абзаца на всю ширину страницы, строка {row}", 700 - 14 * row,
                          x0, 540))  # fmt: skip
    assert column_starts(lines) == []


def test_three_columns() -> None:
    lines = []
    for row in range(6):
        for x0 in (40, 220, 400):
            lines.append(line(f"колонка {x0}: строка {row} набранного текста", 700 - 14 * row,
                              x0, x0 + 160))  # fmt: skip
    starts = column_starts(lines)
    assert starts == [220, 400]
    columns = [ln.column for ln in in_reading_order(lines, starts)]
    assert columns == [0] * 6 + [1] * 6 + [2] * 6


def test_paragraphs_of_a_column_are_measured_by_the_column() -> None:
    # Right-column lines start 270 pt right of the page margin: measured against the page they
    # would all look indented, so every line would start a paragraph.
    left = [("1.1. Срок предоставления услуги не", 700), ("превышает десяти рабочих дней.", 686)]
    right = [("1.2. Заявление подаётся лично или", 700), ("по почте в отдел пропусков.", 686)]
    rows = [line(text, top, 46, 250) for text, top in left]
    rows += [line(text, top, 315, 520) for text, top in right]
    rows += [line(f"строка {n} левой колонки текста", 672 - 14 * n, 46, 250) for n in range(5)]
    rows += [line(f"строка {n} правой колонки текста", 672 - 14 * n, 315, 520) for n in range(5)]
    blocks = [b.text for b in build_blocks([sorted(rows, key=lambda ln: -ln.top)]).blocks]
    assert len(blocks) == 2  # one paragraph a column, not one a line
    assert blocks[0].startswith("1.1. Срок предоставления услуги не превышает десяти рабочих")
    assert blocks[1].startswith("1.2. Заявление подаётся лично или по почте в отдел пропусков.")
