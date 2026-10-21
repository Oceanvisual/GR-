"""PDF extractor and paragraph layout (R-04, T072, T016)."""

from pathlib import Path

import pytest
from fpdf import FPDF
from fpdf.enums import EncryptionMethod
from PIL import Image

from gr.domain.enums import TextLayerStatus
from gr.grounding.normalize import normalize_for_match
from gr.ingestion.base import IngestionError
from gr.ingestion.docx import DocxExtractor
from gr.ingestion.ir import full_text
from gr.ingestion.pdf import PdfExtractor, text_layer_status
from gr.ingestion.pdf_layout import (
    Line,
    build_blocks,
    detect_columns,
    merge_line_pieces,
    strip_running_headers,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"


def pdf(path: Path, **kwargs: int) -> list[str]:
    return [b.text for b in PdfExtractor(**kwargs).extract(path.read_bytes()).blocks]


def lost_paragraphs(pdf_path: Path, docx_path: Path) -> list[str]:
    """DOCX paragraphs of the same document that the PDF does not contain whole."""
    pdf_text = " ¶ ".join(normalize_for_match(t) for t in pdf(pdf_path))
    paragraphs = DocxExtractor().extract(docx_path.read_bytes()).blocks
    return [
        b.text
        for b in paragraphs
        if b.table is None and len(b.text) > 25 and normalize_for_match(b.text) not in pdf_text
    ]


# --- real and synthetic files -----------------------------------------------------------


@pytest.mark.parametrize("name", ["regulation.pdf", "headers.pdf"])
def test_synthetic_pdf_keeps_every_docx_paragraph(name: str) -> None:
    assert lost_paragraphs(SYNTHETIC / name, SYNTHETIC / "regulation_manual.docx") == []


def test_running_headers_and_page_numbers_removed() -> None:
    raw = PdfExtractor().extract((SYNTHETIC / "headers.pdf").read_bytes())
    text = full_text(raw).text
    assert "Страница" not in text
    assert raw.diagnostics.removed_noise.header_footer_lines >= 6
    assert not any(b.text.startswith("10 календарных дней") for b in raw.blocks)
    assert "в течение 10 календарных дней" in text


def test_reg261_pdf_keeps_every_docx_paragraph() -> None:
    npa = FIXTURES / "npa"
    assert lost_paragraphs(npa / "reg261.pdf", npa / "reg261.docx") == []


def test_reg261_pdf_diagnostics() -> None:
    raw = PdfExtractor().extract((FIXTURES / "npa" / "reg261.pdf").read_bytes())
    text = full_text(raw).text
    assert raw.page_count == 47 and raw.diagnostics.text_layer == TextLayerStatus.OK
    assert "Страница 6 из 47" not in text and "Дата сохранения" not in text
    assert "информационно-технологическое" in text  # a real hyphen PDFium marked as soft
    assert all(b.page is not None and b.bbox is not None for b in raw.blocks)
    assert [b.page for b in raw.blocks] == sorted(b.page or 0 for b in raw.blocks)


def test_portal_pdf_split_mid_word() -> None:
    raw = PdfExtractor().extract((FIXTURES / "corpus" / "lenoblast_93oz_2003.pdf").read_bytes())
    text = full_text(raw).text
    assert "docs.cntd.ru" not in text
    assert "О защите населения и территорий Ленинградской области" in text


def test_scan_needs_ocr() -> None:
    raw = PdfExtractor().extract((SYNTHETIC / "scan.pdf").read_bytes())
    assert raw.diagnostics.text_layer == TextLayerStatus.NEEDS_OCR
    assert raw.diagnostics.empty_pages == [1]
    assert raw.blocks == [] and not raw.diagnostics.analyzable


def test_corrupted_pdf() -> None:
    with pytest.raises(IngestionError) as exc:
        PdfExtractor().extract((SYNTHETIC / "corrupted.pdf").read_bytes())
    assert exc.value.code == "corrupted_file"


def _small_pdf(
    text: str, owner_password: str | None = None, user_password: str | None = None
) -> bytes:
    document = FPDF()
    document.add_page()
    document.set_font("helvetica", size=12)
    document.cell(text=text)
    if owner_password:
        document.set_encryption(
            owner_password=owner_password,
            user_password=user_password,
            encryption_method=EncryptionMethod.RC4,
        )
    return bytes(document.output())


def test_password_protected_pdf() -> None:
    with pytest.raises(IngestionError) as exc:
        PdfExtractor().extract(_small_pdf("secret", owner_password="o", user_password="u"))
    assert exc.value.code == "encrypted_file"


def test_owner_password_only_pdf_is_readable() -> None:
    raw = PdfExtractor().extract(_small_pdf("Copy protected text of the act", owner_password="o"))
    assert raw.blocks[0].text == "Copy protected text of the act"


def test_page_limit() -> None:
    with pytest.raises(IngestionError) as exc:
        PdfExtractor(max_pages=2).extract((SYNTHETIC / "headers.pdf").read_bytes())
    assert exc.value.code == "document_too_long" and exc.value.details["pages"] == 3


@pytest.mark.parametrize(
    ("empty", "total", "status"),
    [
        (0, 0, TextLayerStatus.EMPTY),
        (0, 5, TextLayerStatus.OK),
        (2, 5, TextLayerStatus.PARTIAL),
        (5, 5, TextLayerStatus.NEEDS_OCR),
    ],
)
def test_text_layer_status(empty: int, total: int, status: TextLayerStatus) -> None:
    assert text_layer_status(empty, total) == status


# --- layout rules on synthetic geometry -------------------------------------------------

H = 842.0


def line(
    text: str, top: float, x0: float = 57, x1: float = 560, page: int = 1, size: float = 12
) -> Line:
    return Line(text, page, x0, x1, top, top - 10, size, 595.0, H)


def body(*rows: tuple[str, float, float, float]) -> list[Line]:
    return [line(text, top, x0, x1) for text, top, x0, x1 in rows]


def texts(pages: list[list[Line]]) -> list[str]:
    return [b.text for b in build_blocks(pages).blocks]


def test_first_line_indent_starts_paragraph() -> None:
    page = body(
        ("Первый абзац начинается здесь и продолжается", 700, 85, 560),
        ("на следующей строке без отступа.", 686, 57, 300),
        ("Второй абзац с отступом первой строки", 672, 85, 560),
        ("и его продолжение.", 658, 57, 200),
    )
    assert texts([page]) == [
        "Первый абзац начинается здесь и продолжается на следующей строке без отступа.",
        "Второй абзац с отступом первой строки и его продолжение.",
    ]


def test_wrapped_number_does_not_start_paragraph() -> None:
    page = body(
        ("Заявление рассматривается в течение", 700, 85, 560),
        ("10 дней со дня регистрации, утверждённой в", 686, 57, 560),
        ("2020 г. порядком.", 672, 57, 200),
    )
    assert texts([page]) == [
        "Заявление рассматривается в течение 10 дней со дня регистрации, утверждённой в "
        "2020 г. порядком."
    ]


def test_list_item_after_finished_sentence() -> None:
    page = body(
        ("Заявитель представляет документы:", 700, 57, 560),
        ("1) заявление;", 686, 57, 200),
        ("2) копию паспорта.", 672, 57, 220),
    )
    assert texts([page]) == [
        "Заявитель представляет документы:",
        "1) заявление;",
        "2) копию паспорта.",
    ]


def test_abbreviation_is_not_a_sentence_end() -> None:
    page = body(
        ("(Собрание законодательства Российской Федерации, 2018, N 32, ст.", 700, 57, 560),
        ("5135) и направляет решение заявителю.", 686, 57, 300),
    )
    assert len(texts([page])) == 1


def test_edition_note_is_its_own_paragraph() -> None:
    page = body(
        ("Срок составляет 5 рабочих дней со дня поступления заявления в орган.", 700, 85, 560),
        ("(п. 2.6 в ред. Приказа от 30.05.2023 N 108)", 686, 57, 400),
    )
    assert len(texts([page])) == 2


def test_hyphens_depend_on_document_density() -> None:
    word_pdf = [body(("информационно￾технологическое взаимодействие", 700, 57, 560))]
    assert texts(word_pdf) == ["информационно-технологическое взаимодействие"]
    book_pages = [
        body(*[(f"строка произо￾шедшими номер {i}", 700 - 14 * i, 57, 560) for i in range(3)])
    ]
    assert texts(book_pages)[0].startswith("строка произошедшими номер 0")


def test_line_end_hyphen_rules() -> None:
    page = body(
        ("Закон N 210-", 700, 85, 560),
        ("ФЗ и пере­", 686, 57, 560),
        ("нос, а также тире -", 672, 57, 560),
        ("продолжение.", 658, 57, 200),
    )
    assert texts([page]) == ["Закон N 210-ФЗ и перенос, а также тире - продолжение."]


def test_blank_gap_breaks_but_joined_double_line_does_not() -> None:
    joined = line("строка, которую PDFium склеил из двух визуальных", 700)
    joined = Line(joined.text, 1, 57, 560, 700, 676, 12, 595.0, H)  # spans two visual lines
    page = [
        joined,
        line("продолжение абзаца.", 672, x1=200),
        line("Новый абзац после пробела.", 630),
    ]
    assert texts([page]) == [
        "строка, которую PDFium склеил из двух визуальных продолжение абзаца.",
        "Новый абзац после пробела.",
    ]


def test_centred_multiline_heading_stays_together() -> None:
    page = [
        line("III. Состав, последовательность и сроки", 700, 150, 467),
        line("административных процедур", 686, 200, 417),
        line("3.1. Предоставление услуги включает процедуры, указанные", 660, 85, 560),
        *[line(f"в настоящем разделе, строка {i}", 646 - 14 * i, 57, 560) for i in range(4)],
    ]
    blocks = build_blocks([page]).blocks
    assert blocks[0].text == "III. Состав, последовательность и сроки административных процедур"
    assert blocks[0].align == "center"


def test_paragraph_continues_across_pages() -> None:
    first = [line("Абзац начинается на одной странице и", 100, 85, 560, page=1)]
    second = [line("заканчивается на другой.", 790, 57, 300, page=2)]
    blocks = build_blocks([first, second]).blocks
    assert [b.text for b in blocks] == [
        "Абзац начинается на одной странице и заканчивается на другой."
    ]
    assert blocks[0].page == 1


def test_superscript_piece_is_reattached() -> None:
    pieces = [
        Line("«О нормативных правовых актах»1", 1, 54, 340, 411, 401, 11, 420, 595),
        Line(". Его", 1, 344, 370, 408, 398, 11, 420, 595),
    ]
    assert [ln.text for ln in merge_line_pieces(pieces)] == ["«О нормативных правовых актах»1. Его"]


def test_headers_by_parity_and_page_numbers() -> None:
    pages = []
    for number in range(1, 7):
        header = "Проект закона" if number % 2 == 0 else "Предисловие"
        pages.append(
            [
                line(f"{number} {header}", 830),
                line(f"Текст страницы {number}.", 500),
                line(f"- {number} -", 20),
            ]
        )
    kept, removed = strip_running_headers(pages)
    assert [[ln.text for ln in lines] for lines in kept] == [
        [f"Текст страницы {n}."] for n in range(1, 7)
    ]
    assert removed == 12


def test_short_documents_keep_their_lines() -> None:
    pages = [[line("Заголовок", 830), line("Текст.", 500)] for _ in range(2)]
    assert strip_running_headers(pages)[1] == 0


def test_columns_detected() -> None:
    rows = []
    for i in range(6):
        rows += [
            line(f"левая колонка {i}", 700 - 14 * i, 57, 280),
            line(f"правая {i}", 700 - 14 * i, 310, 540),
        ]
    assert detect_columns(rows)
    assert not detect_columns([line("обычный текст", 700 - 14 * i) for i in range(6)])


def _two_pages(second: str, *, image_on_second: bool = False) -> bytes:
    document = FPDF()
    document.set_font("helvetica", size=12)
    document.add_page()
    document.multi_cell(w=0, text="The first page of the act holds the main body of the text. " * 8)
    document.add_page()
    if image_on_second:
        document.image(Image.new("L", (200, 280), color=200), x=10, y=10, w=190)
    document.cell(text=second)
    return bytes(document.output())


def test_short_last_page_keeps_its_text() -> None:
    raw = PdfExtractor().extract(_two_pages("In force from 1 May."))
    assert full_text(raw).text.endswith("text. In force from 1 May.")  # continues the paragraph
    assert raw.diagnostics.text_layer == TextLayerStatus.OK and raw.diagnostics.empty_pages == []


def test_scanned_page_with_a_stamp_still_needs_ocr() -> None:
    raw = PdfExtractor().extract(_two_pages("Signed", image_on_second=True))
    assert raw.diagnostics.empty_pages == [2]
    assert raw.diagnostics.text_layer == TextLayerStatus.PARTIAL
    assert full_text(raw).text.endswith(" Signed")  # the native line is kept, OCR adds the rest
