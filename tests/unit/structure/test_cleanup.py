"""What is not the act's text (FR-014, FR-015, R-20, T067): service lines, change lists,
editorial notes, repealed units; running headers of a PDF."""

from pathlib import Path

import pytest
from structure_helpers import FIXTURES, extract

from gr.domain.document import Block, Fragment, IngestionDiagnostics, RawDocument, TableCell
from gr.domain.enums import DocFormat, FragmentKind, TextLayerStatus
from gr.extraction.render import in_force
from gr.services.parsing import ParseLimits, parse_raw
from gr.structure.builder import StructureResult, build_structure
from gr.structure.cleanup import clean, split_trailing
from gr.structure.tree import normalized_blocks

SYNTHETIC = FIXTURES / "synthetic"
EDIT = "(в ред. Приказа Управления охраны объектов Северной области от 10.05.2024 N 44)"
LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)
SERVICE_TEXT = (
    "Документ предоставлен",
    "Список изменяющих",
    "КонсультантПлюс:",
    "Информация об изменяющих документах",
    "(в ред. ",
    "(Абзац в редакции",
)


def build(*texts: str) -> StructureResult:
    raw = RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text=t) for t in texts],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    return build_structure(raw, "d")


def by_label(result: StructureResult) -> dict[str, Fragment]:
    return {f.label: f for f in result.fragments}


# --- the fixtures of the task ------------------------------------------------------------


def test_a_reference_system_export_keeps_only_the_act_in_its_points() -> None:
    result = build_structure(extract(SYNTHETIC / "spps_export.docx"), "d")
    text = result.text.text
    for noise in ("Документ предоставлен", "Список изменяющих", "(в ред.", "КонсультантПлюс"):
        assert noise not in text, noise
    found = by_label(result)
    assert found["п. 1.2"].notes == [EDIT]  # the note after the point it changed
    assert found["п. 1.2"].text.endswith("семи рабочих дней.")
    (remark,) = found["п. 1.4"].notes  # a remark stands before the point it is about
    assert remark.startswith("КонсультантПлюс: примечание. П. 1.4 применяется")
    root_notes = [n for f in result.fragments for n in f.notes if "Список изменяющих" in n]
    assert root_notes == ["Список изменяющих документов " + EDIT]
    assert found["п. 1.3"].repealed and "Утратил силу" in found["п. 1.3"].text
    assert found["п. 1.3"].id not in in_force(result.fragments)  # never analysed
    assert found["п. 1.4"].id in in_force(result.fragments)
    assert (result.removed.spps_lines, result.removed.editorial_notes) == (1, 3)
    # The cover line stays: it holds the requisites, each with its quote (FR-006).
    assert text.startswith("Приказ Управления охраны объектов Северной области от 15.01.2024 N 15")


def test_the_counters_reach_the_diagnostics() -> None:
    parsed = parse_raw(extract(SYNTHETIC / "spps_export.docx"), "d", LIMITS)
    removed = parsed.diagnostics.removed_noise
    assert (removed.spps_lines, removed.editorial_notes) == (1, 3)
    assert removed.header_footer_lines >= 2  # the page header and footer of the DOCX


def test_running_headers_go_and_a_point_split_by_a_page_is_whole() -> None:
    result = build_structure(extract(SYNTHETIC / "headers.pdf"), "d")
    text = result.text.text
    assert "Страница" not in text and "Административный регламент\n" not in text
    point = by_label(result)["п. 3.4.2"]
    # «10 календарных дней» starts a line of the PDF: joined, not a new item numbered 10.
    assert "в течение 10 календарных дней со дня принятия решения" in point.text


@pytest.mark.parametrize(
    "name", ["regulation.txt", "regulation_manual.docx", "regulation.pdf", "law_like.txt"]
)
def test_a_document_without_noise_is_not_changed(name: str) -> None:
    blocks = normalized_blocks(extract(SYNTHETIC / name))
    cleaned = clean(blocks)
    assert cleaned.blocks == blocks and cleaned.notes == []
    assert (cleaned.spps_lines, cleaned.editorial_notes) == (0, 0)


@pytest.mark.parametrize(
    "path", ["npa/reg261.docx", "npa/reg261.pdf", "corpus/fz_59_2006.docx",
             "corpus/lenoblast_93oz_2003.pdf"],
)  # fmt: skip
def test_real_exports_lose_their_service_text_and_keep_it_as_notes(path: str) -> None:
    result = build_structure(extract(FIXTURES / path), "d")
    text = result.text.text
    for noise in SERVICE_TEXT:
        assert noise not in text, noise
    notes = [n for f in result.fragments for n in f.notes]
    assert len(notes) == result.removed.editorial_notes > 0


def test_notes_land_on_the_unit_they_name() -> None:
    law = build_structure(extract(FIXTURES / "corpus" / "fz_59_2006.docx"), "d")
    found = by_label(law)
    assert any(n.startswith("(часть 4 введена") for n in found["ч. 4 ст. 1"].notes)
    assert any(n.startswith("(п. 1 в ред.") for n in found["п. 1 ст. 4"].notes)
    regional = by_label(build_structure(extract(FIXTURES / "corpus" / "lenoblast_93oz_2003.pdf"),
                                        "d"))  # fmt: skip
    assert any(n.startswith("(Статья в редакции") for n in regional["ст. 1"].notes)
    assert regional["разд. IV"].repealed  # «(Раздел утратил силу …)» after its heading
    assert regional["абз. 2 ст. 7"].repealed  # «Абзац утратил силу с …» in its place


# --- the rules ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "note",
    [
        "(в ред. Приказа Росимущества от 30.05.2023 N 108)",
        "(п. 2.6 в ред. Приказа Росимущества от 30.05.2023 N 108)",
        "(часть 4 введена Федеральным законом от 07.05.2013 N 80-ФЗ)",
        "(абзац введен Приказом от 01.02.2020 N 5)",
        "(с изм., внесенными Постановлением Конституционного Суда РФ от 18.07.2012 N 19-П)",
        "(Абзац десятый в редакции, введенной в действие с 3 января 2010 года Законом от "
        "11 декабря 2009 года N 104-оз)",
        "(Часть дополнительно включена Законом Ленинградской области от 21 июня 2022 года N 65-оз)",
        "(Раздел утратил силу с 28 декабря 2021 года - Закон от 28 декабря 2021 года N 167-оз)",
    ],
)
def test_editorial_notes_leave_the_text(note: str) -> None:
    result = build("I. Общие положения", "1.1. Срок — десять дней.", note, "1.2. Пропуск.")
    assert note not in result.text.text
    assert by_label(result)["п. 1.1"].notes == [note] or by_label(result)["разд. I"].notes == [note]


@pytest.mark.parametrize(
    "text",
    [
        "(ред. от 30.05.2023)",  # the edition of the act on its cover: a requisite
        "(Принят Законодательным собранием 11 ноября 2003 года)",
        "(далее - заявитель)",
        "(введение в действие — с 1 января)",
        "(изменение сроков допускается по заявлению)",
        "(исключение составляют выходные дни)",
    ],
)
def test_parentheses_of_the_act_stay(text: str) -> None:
    result = build("I. Общие положения", "1.1. Срок — десять дней.", text)
    assert text in result.text.text and result.removed.editorial_notes == 0


def test_a_note_at_the_end_of_a_sentence_leaves_it_and_a_reference_inside_one_stays() -> None:
    body, note = split_trailing(f"1.2. Срок — семь дней. {EDIT}")
    assert (body, note) == ("1.2. Срок — семь дней.", EDIT)
    inside = "1.2. По форме, утвержденной приказом от 01.01.2020 N 1 (в ред. приказа N 2)."
    assert split_trailing(inside) == (inside, None)


def test_a_note_split_by_the_layout_is_taken_whole() -> None:
    result = build(
        "СТАТЬЯ 3. ПОДСИСТЕМА",
        "(Наименование в редакции, введенной в действие с 28 декабря 2021 года Законом от 28",
        "декабря 2021 года N 167-оз. - См. предыдущую редакцию)",
        "1. Подсистема объединяет органы управления.",
    )
    assert "декабря 2021 года N 167-оз" not in result.text.text
    (note,) = [n for f in result.fragments for n in f.notes]
    assert note.endswith("См. предыдущую редакцию)") and result.removed.editorial_notes == 1


def test_a_list_of_changing_documents_between_separators() -> None:
    result = build(
        "ЛЕНИНГРАДСКАЯ ОБЛАСТЬ ОБЛАСТНОЙ ЗАКОН",
        "Информация об изменяющих документах",
        "______________________________",
        "Документ с изменениями, внесенными:",
        "Законом Ленинградской области от 13 сентября 2004 года N 60-оз;",
        "Законом Ленинградской области от 21 июля 2005 года N 61-оз.",
        "______________________________",
        "Статья 1. Правовое регулирование",
        "1. Законодательство состоит из федеральных законов.",
    )
    assert "Законом Ленинградской области" not in result.text.text
    assert "____" not in result.text.text and result.removed.editorial_notes == 1
    (note,) = [n for f in result.fragments for n in f.notes]
    assert note.startswith("Информация об изменяющих документах Документ с изменениями")


def test_a_remark_whose_heading_the_layout_split_or_glued() -> None:
    alone = build("1.1. Срок.", "КонсультантПлюс: примечание.", "П. 1.2 применяется с учетом.",
                  "1.2. Пропуск.")  # fmt: skip
    assert by_label(alone)["п. 1.2"].notes == [
        "КонсультантПлюс: примечание. П. 1.2 применяется с учетом."
    ]
    glued = build("1.1. Срок десять дней; КонсультантПлюс: примечание.",
                  "П. 1.2 применяется с учетом.", "1.2. Пропуск.")  # fmt: skip
    found = by_label(glued)
    assert found["п. 1.1"].text == "1.1. Срок десять дней;"
    assert found["п. 1.2"].notes == ["КонсультантПлюс: примечание. П. 1.2 применяется с учетом."]
    assert "КонсультантПлюс" not in glued.text.text


@pytest.mark.parametrize(
    ("text", "repealed"),
    [
        ("1.2. Утратил силу. - Приказ от 10.05.2024 N 44.", True),
        ("1.2. Исключен.", True),
        ("1.2. Исключение составляют выходные дни.", False),
        ("1.2. Срок не продлевается, за исключением случаев болезни.", False),
    ],
)
def test_a_point_that_lost_force_is_marked(text: str, repealed: bool) -> None:
    found = by_label(build("I. Общие положения", "1.1. Срок.", text, "1.3. Пропуск."))
    assert found["п. 1.2"].repealed is repealed
    assert not found["п. 1.1"].repealed and not found["п. 1.3"].repealed


def test_service_lines_alone_are_dropped_and_mixed_lines_stay() -> None:
    result = build(
        "Документ предоставлен КонсультантПлюс  www.consultant.ru  Дата сохранения: 03.06.2026",
        "Приказ от 11.09.2020 N 261 (ред. от 30.05.2023) Документ предоставлен КонсультантПлюс",
        "1.1. Срок.",
    )
    assert result.removed.spps_lines == 1
    assert result.text.text.startswith("Приказ от 11.09.2020 N 261 (ред. от 30.05.2023)")


def test_notes_with_nothing_before_them_are_the_acts() -> None:
    result = build("Список изменяющих документов (в ред. Приказа от 10.05.2024 N 44)", "1.1. Срок.")
    root = next(f for f in result.fragments if f.kind == FragmentKind.ROOT)
    assert root.notes == ["Список изменяющих документов (в ред. Приказа от 10.05.2024 N 44)"]


def test_patterns_are_configuration() -> None:
    source = Path(__file__).resolve().parents[3] / "src" / "gr" / "config" / "noise_patterns.toml"
    assert "КонсультантПлюс" in source.read_text(encoding="utf-8")  # FR-014: configurable


@pytest.mark.parametrize("style", ["toc 1", "TOC2", "Оглавление 1", "Содержание 2"])
def test_generated_contents_do_not_duplicate_sections_or_eat_the_body(style: str) -> None:
    blocks = [
        Block(text="Содержание"),
        Block(text=""),
        Block(text="I. Общие положения 1", style=style),
        Block(text="II. Порядок работы 2", style=style),
        Block(text="I. Общие положения", style="heading 1"),
        Block(text="1.1. Первый пункт."),
        Block(text="II. Порядок работы", style="heading 1"),
        Block(text="2.1. Второй пункт."),
    ]
    raw = RawDocument(
        format=DocFormat.DOCX,
        blocks=blocks,
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    result = build_structure(raw, "d")
    assert [f.number for f in result.fragments if f.kind == FragmentKind.SECTION] == ["I", "II"]
    assert [f.number for f in result.fragments if f.kind == FragmentKind.POINT] == ["1.1", "2.1"]
    assert result.text.text == "\n".join(b.text for b in blocks[4:])
    assert result.removed.contents_lines == 3
    assert (result.removed.spps_lines, result.removed.editorial_notes) == (0, 0)
    assert clean(blocks).dropped == [blocks[i].text for i in (0, 2, 3)]


@pytest.mark.parametrize("heading", ["Содержание", "Оглавление", "Contents", "Table of contents"])
def test_a_contents_heading_without_generated_entries_is_kept(heading: str) -> None:
    blocks = [Block(text=heading), Block(text="I. Общие положения"), Block(text="1.1. Пункт.")]
    cleaned = clean(blocks)
    assert cleaned.blocks == blocks
    assert cleaned.contents_lines == 0


def test_contents_styles_in_table_cells_do_not_remove_table_data() -> None:
    blocks = [
        Block(text="Содержание", table=TableCell(table=0, row=0, col=0)),
        Block(text="I. Работа 1", style="toc 1", table=TableCell(table=0, row=1, col=0)),
    ]
    cleaned = clean(blocks)
    assert cleaned.blocks == blocks
    assert cleaned.contents_lines == 0


def test_the_professional_standard_keeps_only_its_body_sections() -> None:
    raw = extract(FIXTURES / "corpus" / "mintrud_572n_2020_appendix.docx")
    parsed = parse_raw(raw, "d", LIMITS)
    approved = next(f for f in parsed.fragments if f.kind == FragmentKind.APPROVED_DOCUMENT)
    assert approved.parent_id == "root"
    assert [f.number for f in parsed.fragments if f.kind == FragmentKind.SECTION] == [
        "I",
        "II",
        "III",
        "IV",
    ]
    assert [f.number for f in parsed.fragments if f.kind == FragmentKind.POINT] == [
        "3.1",
        "3.1.1",
        "3.1.2",
        "3.1.3",
        "3.2",
        "3.2.1",
        "3.2.2",
        "3.2.3",
        "3.3",
        "3.3.1",
        "3.3.2",
        "3.4",
        "3.4.1",
        "3.4.2",
        "3.4.3",
        "4.1",
        "4.2",
    ]
    assert "Содержание" not in parsed.text
    assert parsed.diagnostics.removed_noise.contents_lines == 9
