"""Structure building: tree, ids, labels, offsets, numbering plausibility (R-06, T020)."""

import json
from pathlib import Path

import pytest
from structure_helpers import FIXTURES, extract

from gr.domain.document import Block, IngestionDiagnostics, RawDocument, TableCell
from gr.domain.enums import DocFormat, FragmentKind, StructureMode, TextLayerStatus
from gr.structure.builder import StructureResult, build_structure
from gr.structure.invariants import check_invariants
from gr.structure.numbering import Verdict, plausible_letter, plausible_point, plausible_subpoint
from gr.structure.patterns import Marker, classify, roman_to_int

SYNTHETIC = FIXTURES / "synthetic"
STRUCTURAL = {"section", "chapter", "article", "part", "point", "subpoint", "appendix",
              "approved_document", "preamble"}  # fmt: skip


def build(path: Path) -> StructureResult:
    return build_structure(extract(path), "doc_x")


def raw(*texts: str) -> RawDocument:
    return RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text=t) for t in texts],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )


def tree(result: StructureResult) -> list[tuple[str, str, str]]:
    return [(f.id, f.kind.value, f.label) for f in result.fragments]


def skeleton(result: StructureResult) -> list[str]:
    return [f"{f.kind.value} {f.label}" for f in result.fragments if f.kind.value in STRUCTURAL]


# --- every fixture -----------------------------------------------------------------------

ALL_FIXTURES = sorted(
    p
    for p in FIXTURES.glob("*/*")
    if p.suffix in (".txt", ".docx", ".pdf") and "scan" not in p.name and "corrupted" not in p.name
)


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.name)
def test_invariants_on_every_fixture(path: Path) -> None:
    result = build(path)
    assert check_invariants(result.fragments, result.text.text) == []


# --- synthetic regulation: golden tree and format independence -------------------------


def test_synthetic_tree_matches_golden() -> None:
    golden = json.loads((SYNTHETIC / "regulation.structure.json").read_text(encoding="utf-8"))
    assert [list(t) for t in tree(build(SYNTHETIC / "regulation.txt"))] == golden


@pytest.mark.parametrize("name", ["regulation_manual.docx", "regulation_autonum.docx"])
def test_docx_variants_give_the_same_tree(name: str) -> None:
    assert tree(build(SYNTHETIC / name)) == tree(build(SYNTHETIC / "regulation.txt"))


@pytest.mark.parametrize("name", ["regulation.pdf", "headers.pdf"])
def test_pdf_gives_the_same_skeleton(name: str) -> None:
    assert skeleton(build(SYNTHETIC / name)) == skeleton(build(SYNTHETIC / "regulation.txt"))


def test_offsets_and_subtree_text() -> None:
    result = build(SYNTHETIC / "regulation.txt")
    text = result.text.text
    by_id = {f.id: f for f in result.fragments}
    point = by_id["body/II/2.2"]
    assert point.text.startswith("2.2. Для получения пропуска")
    subtree = text[point.start : point.end]
    assert "1) заявление по форме" in subtree and "3) сведения о транспортном" in subtree
    assert by_id["body/II/2.2/sp2"].label == "пп. 2 п. 2.2"
    assert by_id["body/III/3.1/a2"].kind == FragmentKind.LIST_ITEM
    assert by_id["body/III/3.1/a2"].label == "абз. 2 п. 3.1"
    assert (
        by_id["body"].heading is not None and "АДМИНИСТРАТИВНЫЙ РЕГЛАМЕНТ" in by_id["body"].heading
    )


def test_pdf_fragments_carry_pages() -> None:
    result = build(SYNTHETIC / "headers.pdf")
    assert result.stats["pages"] == 3
    last_point = [f for f in result.fragments if f.kind == FragmentKind.POINT][-1]
    assert last_point.pages and last_point.pages[-1] == 3
    assert result.fragments[0].pages == [1, 2, 3]


# --- numbering ----------------------------------------------------------------------------


def test_wrapped_numbers_are_text() -> None:
    result = build_structure(
        raw(
            "I. Общие положения",
            "1.1. Заявление рассматривается в течение",
            "10 дней со дня регистрации.",
            "2020 г. — год утверждения порядка.",
            "16.05.2011 N 373 — дата постановления.",
            "1.2. Срок продлевается.",
        ),
        "d",
    )
    assert [f.label for f in result.fragments if f.kind == FragmentKind.POINT] == [
        "п. 1.1",
        "п. 1.2",
    ]


def test_duplicates_get_suffixes_and_warnings() -> None:
    result = build(SYNTHETIC / "dup_numbering.txt")
    ids = [f.id for f in result.fragments]
    assert len(ids) == len(set(ids))
    assert "I/1.2~2" in ids and "I/1.3/sp1~2" in ids
    assert any("1.2" in w for w in result.warnings)


def test_no_numbering_falls_back_to_paragraphs() -> None:
    result = build(SYNTHETIC / "no_numbering.txt")
    assert result.mode == StructureMode.PARAGRAPHS and result.strategy == "paragraphs"
    assert result.warnings
    assert [f.id for f in result.fragments[1:]] == ["p1", "p2", "p3", "p4"]


def test_paragraph_labels_include_page() -> None:
    blocks = [Block(text=f"Абзац номер {i} без нумерации.", page=i) for i in range(1, 4)]
    document = RawDocument(
        format=DocFormat.PDF,
        blocks=blocks,
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    result = build_structure(document, "d")
    assert [f.label for f in result.fragments[1:]] == [
        "абз. 1, стр. 1",
        "абз. 2, стр. 2",
        "абз. 3, стр. 3",
    ]


@pytest.mark.parametrize(
    ("last", "candidate", "verdict"),
    [
        (None, (1,), Verdict.OK),
        (None, (1, 1), Verdict.OK),
        (None, (7,), Verdict.REJECT),
        ((3, 5), (3, 5, 1), Verdict.OK),
        ((3, 5), (3, 6), Verdict.OK),
        ((3, 5), (4, 1), Verdict.OK),
        ((3, 5), (3, 7), Verdict.OK),  # one missing point tolerated
        ((3, 5), (3, 9), Verdict.REJECT),
        ((3, 5), (10,), Verdict.REJECT),
        ((3, 5), (3, 5), Verdict.DUPLICATE),
        ((3, 5, 1), (3, 6), Verdict.OK),
        ((3, 8, 3), (4, 1), Verdict.OK),
        ((2,), (300,), Verdict.REJECT),
        ((2,), (3, 0), Verdict.REJECT),
    ],
)
def test_point_plausibility(
    last: tuple[int, ...] | None, candidate: tuple[int, ...], verdict: Verdict
) -> None:
    assert plausible_point(last, candidate) == verdict


def test_new_section_may_restart_numbering() -> None:
    assert plausible_point((2, 27), (3, 1), section=3, section_changed=True) == Verdict.OK
    assert plausible_point((2, 27), (5, 1), section=3, section_changed=True) == Verdict.REJECT
    assert plausible_point(None, (3, 1), section=3) == Verdict.OK


def test_subpoint_and_letter_plausibility() -> None:
    assert plausible_subpoint(None, (1,)) == Verdict.OK
    assert plausible_subpoint((3,), (3, 1)) == Verdict.OK  # inserted «3.1)»
    assert plausible_subpoint((3, 1), (4,)) == Verdict.OK
    assert plausible_subpoint((3,), (9,)) == Verdict.REJECT
    assert plausible_letter(None, "а") == Verdict.OK
    assert plausible_letter("и", "к") == Verdict.OK  # й is skipped in legal lists
    assert plausible_letter("а", "ж") == Verdict.REJECT


@pytest.mark.parametrize(
    ("text", "marker", "number"),
    [
        ("УТВЕРЖДЕН приказом от 1.01.2024 № 1", Marker.APPROVED, None),
        ("Утверждено", Marker.APPROVED, None),
        ("Утвержденный порядок применяется", Marker.TEXT, None),
        ("Приложение N 1 к приказу от 1.01.2024 N 5", Marker.ACT_APPENDIX, None),
        ("Приложение № 2 к Административному регламенту", Marker.APPENDIX, "2"),
        ("Приложение: копия паспорта", Marker.TEXT, None),
        ("III. Состав процедур", Marker.SECTION, "III"),
        ("Раздел 2. Сроки", Marker.SECTION, "2"),
        ("IIII. Не раздел", Marker.TEXT, None),
        ("Глава 3. Порядок", Marker.CHAPTER, "3"),
        ("СТАТЬЯ 3_1. ФУНКЦИОНИРОВАНИЕ", Marker.ARTICLE, "3.1"),
        ("3.1.1 Организация услуги", Marker.POINT, "3.1.1"),
        ("12. Пункт", Marker.POINT, "12"),
        ("10 дней со дня", Marker.TEXT, None),
        ("14.1) подпункт", Marker.SUBPOINT, "14.1"),
        ("б) подпункт", Marker.LETTER, "б"),
        ("– элемент перечня", Marker.LIST_ITEM, None),
    ],
)
def test_classification(text: str, marker: Marker, number: str | None) -> None:
    match = classify(text)
    assert match.marker == marker
    if number is not None:
        assert match.number == number


def test_roman_numerals() -> None:
    assert [roman_to_int(v) for v in ("I", "IV", "IX", "XIV", "VX", "IIII")] == [
        1,
        4,
        9,
        14,
        None,
        None,
    ]


# --- other document shapes ----------------------------------------------------------------


def test_law_articles_parts_and_points() -> None:
    by_id = {f.id: f for f in build(SYNTHETIC / "law_like.txt").fragments}
    assert by_id["art2"].kind == FragmentKind.ARTICLE and by_id["art2"].label == "ст. 2"
    assert by_id["art2/2"].kind == FragmentKind.PART and by_id["art2/2"].label == "ч. 2 ст. 2"
    assert by_id["art2/2/sp1"].label == "п. 1 ч. 2 ст. 2"


def test_composite_order_has_one_root_per_approved_document() -> None:
    result = build(SYNTHETIC / "multi_approved.docx")
    roots = [f for f in result.fragments if f.kind == FragmentKind.APPROVED_DOCUMENT]
    assert [f.approved_doc for f in roots] == ["ПОРЯДОК учёта посетителей", "ПОЛОЖЕНИЕ о комиссии"]
    points = [f.label for f in result.fragments if f.id in ("body/1", "body2/1")]
    assert points == ["п. 1 (ПОРЯДОК учёта посетителей)", "п. 1 (ПОЛОЖЕНИЕ о комиссии)"]


def test_table_rows_are_single_fragments() -> None:
    blocks = [
        Block(text="I. Сроки"),
        Block(text="1.1. Сроки указаны в таблице:"),
        Block(text="Процедура", table=TableCell(table=0, row=0, col=0)),
        Block(text="Срок", table=TableCell(table=0, row=0, col=1)),
        Block(text="Приём", table=TableCell(table=0, row=1, col=0)),
        Block(text="1 день", table=TableCell(table=0, row=1, col=1)),
    ]
    document = RawDocument(
        format=DocFormat.DOCX,
        blocks=blocks,
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    rows = [
        f.text for f in build_structure(document, "d").fragments if f.kind == FragmentKind.PARAGRAPH
    ]
    assert rows == ["Процедура | Срок", "Приём | 1 день"]


def test_unstyled_short_line_does_not_close_a_point() -> None:
    result = build_structure(
        raw(
            "I. Общие положения",
            "1.1. Основания для отказа:",
            "1) первое;",
            "Примечание редакции без точки",
            "2) второе;",
        ),
        "d",
    )
    labels = [f.label for f in result.fragments]
    assert "пп. 2 п. 1.1" in labels


def test_empty_document_is_rejected() -> None:
    with pytest.raises(ValueError, match="no text"):
        build_structure(raw("   "), "d")
