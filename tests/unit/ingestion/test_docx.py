"""DOCX extractor and Word numbering (R-04, R-05, T017)."""

import io
import time
import zipfile
from pathlib import Path

import pytest

from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError
from gr.ingestion.detect import detect_format
from gr.ingestion.docx import DocxExtractor, ZipLimits
from gr.ingestion.docx_numbering import (
    Level,
    Num,
    NumberingDefinitions,
    NumberingState,
    format_number,
)
from gr.ingestion.ir import full_text

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def extract(name: str) -> list[str]:
    return [b.text for b in DocxExtractor().extract((SYNTHETIC / name).read_bytes()).blocks]


def make_docx(body: str, **parts: str) -> bytes:
    """Minimal package: only the parts the extractor reads."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{W_NS}" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            f"<w:body>{body}</w:body></w:document>",
        )
        for name, xml in parts.items():
            archive.writestr(f"word/{name}.xml", xml)
    return buffer.getvalue()


def p(text: str, ppr: str = "", rpr: str = "") -> str:
    run = f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{run}</w:p>"


# --- synthetic fixtures and the real regulation -----------------------------------------


def test_autonumbering_matches_manual_numbering() -> None:
    manual = extract("regulation_manual.docx")
    auto = DocxExtractor().extract((SYNTHETIC / "regulation_autonum.docx").read_bytes())
    assert [b.text for b in auto.blocks] == manual
    assert auto.diagnostics.numbering_restored
    numbered = [b for b in auto.blocks if b.list_marker]
    assert numbered and all(b.list_level is not None for b in numbered)


def test_manual_numbering_is_not_flagged_as_restored() -> None:
    raw = DocxExtractor().extract((SYNTHETIC / "regulation_manual.docx").read_bytes())
    assert not raw.diagnostics.numbering_restored


def test_tracked_changes_keep_insertions_drop_deletions() -> None:
    text = "\n".join(extract("tracked_changes.docx"))
    assert "Срок выдачи пропуска составляет пять рабочих дней." in text
    assert "десять" not in text


def test_tables_become_cells_in_order() -> None:
    raw = DocxExtractor().extract((SYNTHETIC / "table.docx").read_bytes())
    cells = [b for b in raw.blocks if b.table is not None]
    assert cells and {b.table.table for b in cells if b.table} == {0}
    assert raw.blocks[0].text.startswith("III.") and raw.blocks[-1].text.startswith("3.2.")
    first_row = [b.text for b in cells if b.table and b.table.row == 0]
    assert " | ".join(first_row) in full_text(raw).text


def test_headers_and_footers_are_not_read() -> None:
    raw = DocxExtractor().extract((SYNTHETIC / "spps_export.docx").read_bytes())
    text = full_text(raw).text
    assert "Дата сохранения" not in text and "Страница 1 из 1" not in text
    assert raw.diagnostics.removed_noise.header_footer_lines >= 2


def test_reg261_docx() -> None:
    data = (FIXTURES / "npa" / "reg261.docx").read_bytes()
    assert detect_format(data) == DocFormat.DOCX
    raw = DocxExtractor().extract(data)
    text = full_text(raw).text
    assert len(text) > 100_000
    assert "3.5.1. Специалист" in text
    assert "Документ предоставлен" not in text.split("\n", 3)[0]  # banner lives in the header
    assert raw.diagnostics.removed_noise.header_footer_lines > 0
    assert sum(1 for b in raw.blocks if b.table is not None) > 0


def test_corpus_law_docx() -> None:
    raw = DocxExtractor().extract((FIXTURES / "corpus" / "fz_59_2006.docx").read_bytes())
    text = full_text(raw).text
    assert "Статья 1." in text and "Статья 18." in text


# --- safety ------------------------------------------------------------------------------


def test_zip_bomb_rejected_before_inflating() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\x00" * (8 << 20))
    started = time.perf_counter()
    with pytest.raises(IngestionError) as exc:
        DocxExtractor(ZipLimits(max_unzipped_bytes=4 << 20)).extract(buffer.getvalue())
    assert exc.value.code == "corrupted_file" and exc.value.details["reason"] == "zip_limits"
    assert time.perf_counter() - started < 1


def test_compression_ratio_limit() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\x00" * (4 << 20))  # ratio ~1000
    with pytest.raises(IngestionError) as exc:
        DocxExtractor().extract(buffer.getvalue())
    assert exc.value.details["reason"] == "zip_limits"


def test_too_many_entries() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(30):
            archive.writestr(f"x/{i}", b"")
    with pytest.raises(IngestionError) as exc:
        DocxExtractor(ZipLimits(max_entries=10)).extract(buffer.getvalue())
    assert exc.value.details["reason"] == "zip_limits"


def test_encrypted_ooxml() -> None:
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + "EncryptionInfo".encode("utf-16-le")
    for call in (detect_format, DocxExtractor().extract):
        with pytest.raises(IngestionError) as exc:
            call(data)
        assert exc.value.code == "encrypted_file"


def test_entity_declarations_rejected() -> None:
    body = '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="' + W_NS + '"/>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", body)
    with pytest.raises(IngestionError) as exc:
        DocxExtractor().extract(buffer.getvalue())
    assert exc.value.code == "corrupted_file"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"PK\x03\x04 broken", "corrupted_file"),
        (make_docx(p("  ")), "empty_document"),
    ],
)
def test_broken_and_empty(data: bytes, code: str) -> None:
    with pytest.raises(IngestionError) as exc:
        DocxExtractor().extract(data)
    assert exc.value.code == code


def test_missing_document_part() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/styles.xml", "<x/>")
    with pytest.raises(IngestionError) as exc:
        DocxExtractor().extract(buffer.getvalue())
    assert exc.value.code == "corrupted_file"


# --- inline content ---------------------------------------------------------------------


def test_hyperlinks_fields_tabs_and_hyphens() -> None:
    body = (
        "<w:p>"
        '<w:r><w:t xml:space="preserve">См. </w:t></w:r>'
        '<w:hyperlink r:id="x" xmlns:r="r"><w:r><w:t>пункт 2.1</w:t></w:r></w:hyperlink>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t xml:space="preserve"> (стр. 5)</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "<w:r><w:tab/><w:t>N</w:t><w:noBreakHyphen/><w:t>ФЗ</w:t><w:br/><w:t>дальше</w:t></w:r>"
        "</w:p>"
    )
    [block] = DocxExtractor().extract(make_docx(body)).blocks
    assert block.text == "См. пункт 2.1 (стр. 5)\tN-ФЗ дальше"
    assert "PAGE" not in block.text


BOX = "<w:txbxContent><w:p><w:r><w:t>Надпись в рамке</w:t></w:r></w:p></w:txbxContent>"


def test_text_boxes_once_and_footnotes_at_the_end() -> None:
    textbox = (
        "<w:p><w:r><w:t>Основной текст.</w:t></w:r><w:r><mc:AlternateContent>"
        f"<mc:Choice>{BOX}</mc:Choice><mc:Fallback>{BOX}</mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )
    footnotes = (
        f'<w:footnotes xmlns:w="{W_NS}">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t>---</w:t></w:r></w:p></w:footnote>'
        '<w:footnote w:id="1"><w:p><w:r><w:t>Текст сноски.</w:t></w:r></w:p></w:footnote>'
        "</w:footnotes>"
    )
    blocks = [
        b.text for b in DocxExtractor().extract(make_docx(textbox, footnotes=footnotes)).blocks
    ]
    assert blocks == ["Основной текст.", "[Надпись] Надпись в рамке", "[Сноска 1] Текст сноски."]


def test_paragraph_attributes() -> None:
    styles = (
        f'<w:styles xmlns:w="{W_NS}">'
        '<w:style w:styleId="H1"><w:name w:val="heading 1"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr>'
        '<w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
        "</w:styles>"
    )
    body = p("Заголовок", '<w:pStyle w:val="H1"/>') + p(
        "Абзац", '<w:jc w:val="both"/><w:ind w:firstLine="709"/>'
    )
    heading, paragraph = DocxExtractor().extract(make_docx(body, styles=styles)).blocks
    assert (heading.style, heading.bold, heading.align, heading.font_size) == (
        "heading 1",
        True,
        "center",
        14.0,
    )
    assert (paragraph.bold, paragraph.align, paragraph.indent) == (False, "justify", 35.45)


def test_numbering_from_style() -> None:
    numbering = (
        f'<w:numbering xmlns:w="{W_NS}">'
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="russianLower"/><w:lvlText w:val="%1)"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="5"><w:abstractNumId w:val="1"/></w:num></w:numbering>'
    )
    styles = (
        f'<w:styles xmlns:w="{W_NS}"><w:style w:styleId="L"><w:name w:val="List"/>'
        '<w:pPr><w:numPr><w:numId w:val="5"/></w:numPr></w:pPr></w:style></w:styles>'
    )
    body = "".join(p(t, '<w:pStyle w:val="L"/>') for t in ("первый", "второй"))
    raw = DocxExtractor().extract(make_docx(body, styles=styles, numbering=numbering))
    assert [b.text for b in raw.blocks] == ["а) первый", "б) второй"]
    assert raw.diagnostics.numbering_restored


# --- numbering engine -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "fmt", "expected"),
    [
        (4, "decimal", "4"),
        (4, "upperRoman", "IV"),
        (14, "lowerRoman", "xiv"),
        (3, "lowerLetter", "c"),
        (27, "lowerLetter", "aa"),
        (6, "russianLower", "е"),
        (7, "russianLower", "ж"),
        (10, "russianLower", "к"),  # й is skipped
        (2, "russianUpper", "Б"),
        (5, "decimalZero", "05"),
        (1, "bullet", "–"),
        (1, "none", ""),
        (3, "ordinal", "3"),
    ],
)
def test_format_number(n: int, fmt: str, expected: str) -> None:
    assert format_number(n, fmt) == expected


def _defs(levels: dict[int, Level], nums: dict[str, Num]) -> NumberingDefinitions:
    return NumberingDefinitions(abstracts={"a": levels}, nums=nums)


def test_levels_reset_and_legal_style() -> None:
    state = NumberingState(
        _defs(
            {
                0: Level(fmt="upperRoman", text="%1."),
                1: Level(text="%1.%2.", is_legal=True),
                2: Level(text="%1.%2.%3.", is_legal=True),
            },
            {"1": Num("a")},
        )
    )
    sequence = [(0,), (1,), (2,), (2,), (1,), (2,), (0,), (1,)]
    markers = [state.next_marker("1", s[0]) for s in sequence]
    assert markers == ["I.", "1.1.", "1.1.1.", "1.1.2.", "1.2.", "1.2.1.", "II.", "2.1."]


def test_lists_share_abstract_counter_unless_restarted() -> None:
    state = NumberingState(
        _defs(
            {0: Level(text="%1.")},
            {"1": Num("a"), "2": Num("a"), "3": Num("a", start_overrides={0: 1})},
        )
    )
    assert [state.next_marker(n, 0) for n in ("1", "1", "2", "3", "3")] == [
        "1.",
        "2.",
        "3.",
        "1.",
        "2.",
    ]


def test_unknown_lists_and_unsupported_formats() -> None:
    state = NumberingState(_defs({0: Level(fmt="chineseCounting", text="%1.")}, {"1": Num("a")}))
    assert state.next_marker("0", 0) is None
    assert state.next_marker("9", 0) is None
    assert state.next_marker("1", 3) is None
    assert state.next_marker("1", 0) == "1."
    assert state.warnings


DECIMAL_NUMBERING = (
    f'<w:numbering xmlns:w="{W_NS}">'
    '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
    '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
    '<w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num></w:numbering>'
)
NUMBERED = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr>'
DELETED_MARK = '<w:rPr><w:del w:id="1" w:author="a" w:date="2024-01-01T00:00:00Z"/></w:rPr>'


def test_paragraph_with_deleted_mark_does_not_take_a_number() -> None:
    deleted_run = '<w:del w:id="2" w:author="a"><w:r><w:delText>удалено</w:delText></w:r></w:del>'
    body = (
        p("первый", NUMBERED)
        + f"<w:p><w:pPr>{NUMBERED}{DELETED_MARK}</w:pPr>{deleted_run}</w:p>"
        + p("второй", NUMBERED)
        + p("начало", NUMBERED + DELETED_MARK)
        + p("конец", NUMBERED)
    )
    raw = DocxExtractor().extract(make_docx(body, numbering=DECIMAL_NUMBERING))
    assert [b.text for b in raw.blocks] == ["1. первый", "2. второй", "3. начало конец"]


def test_numbering_counts_through_table_cells() -> None:
    cell = "<w:tc>" + p("в таблице", NUMBERED) + "</w:tc>"
    body = p("до таблицы", NUMBERED) + f"<w:tbl><w:tr>{cell}</w:tr></w:tbl>" + p("после", NUMBERED)
    raw = DocxExtractor().extract(make_docx(body, numbering=DECIMAL_NUMBERING))
    assert [b.text for b in raw.blocks] == ["1. до таблицы", "2. в таблице", "3. после"]
