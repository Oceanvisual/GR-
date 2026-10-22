"""Numbering profiles (FR-010, FR-011, FR-016, R-06, T068): the profile is chosen by the tree a
document gets — articles make a law, numbered points a regulation — and «§» is a level of its own
between a chapter and its articles."""

import pytest
from structure_helpers import FIXTURES

from gr.domain.document import Block, IngestionDiagnostics, RawDocument
from gr.domain.enums import DocFormat, FragmentKind, NumberingProfile, TextLayerStatus
from gr.services.parsing import ParsedDocument, ParseLimits, parse_file, parse_raw

LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)


def parse(*texts: str) -> ParsedDocument:
    raw = RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text=text) for text in texts],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    return parse_raw(raw, "d", LIMITS)


@pytest.mark.parametrize(
    ("path", "profile"),
    [
        ("synthetic/law_like.txt", NumberingProfile.LAW),
        ("corpus/fz_59_2006.docx", NumberingProfile.LAW),
        ("corpus/lenoblast_93oz_2003.pdf", NumberingProfile.LAW),
        ("synthetic/regulation.txt", NumberingProfile.REGULATION),
        ("npa/reg261.docx", NumberingProfile.REGULATION),
        ("synthetic/multi_approved.docx", NumberingProfile.REGULATION),
        ("synthetic/no_numbering.txt", NumberingProfile.GENERIC),
    ],
)
def test_the_profile_follows_the_tree(path: str, profile: NumberingProfile) -> None:
    parsed = parse_file(str(FIXTURES / path), "d", LIMITS)
    assert isinstance(parsed, ParsedDocument)
    assert parsed.diagnostics.numbering_profile == profile


def test_paragraph_sections_stand_between_chapters_and_articles() -> None:
    parsed = parse(
        "Глава 1. Общие положения",
        "§ 1. Предмет регулирования",
        "Статья 1. Цель закона",
        "1. Закон устанавливает порядок выдачи пропусков.",
        "2. Закон действует на всей территории области.",
        "§ 2. Основные понятия",
        "Статья 2. Понятия",
        "В законе используются понятия из федерального закона.",
        "Глава 2. Порядок выдачи",
        "§ 1. Заявление",
        "Статья 3. Подача заявления",
        "Заявление подаётся лично.",
    )
    by_label = {f.label: f for f in parsed.fragments}
    by_id = {f.id: f for f in parsed.fragments}
    first = by_label["§ 1 гл. 1"]
    assert first.kind == FragmentKind.PARAGRAPH_SECTION and first.heading == (
        "§ 1. Предмет регулирования"
    )
    assert by_id[first.parent_id or ""].label == "гл. 1"
    assert by_label["ст. 1"].parent_id == first.id
    assert by_label["ч. 2 ст. 1"].parent_id == by_label["ст. 1"].id
    assert by_label["ст. 2"].parent_id == by_label["§ 2 гл. 1"].id
    second = by_label["§ 1 гл. 2"]  # «§ 1» again in the next chapter: its own fragment
    assert by_id[second.parent_id or ""].label == "гл. 2"
    assert by_label["ст. 3"].parent_id == second.id
    assert parsed.diagnostics.numbering_profile == NumberingProfile.LAW


def test_a_chapter_after_articles_returns_to_its_section() -> None:
    parsed = parse(
        "Раздел I. Общая часть",
        "Глава 1. Основы",
        "Статья 1. Цель",
        "Закон устанавливает порядок.",
        "Глава 2. Понятия",
        "Статья 2. Термины",
        "Термины используются в значениях федерального закона.",
    )
    by_label = {f.label: f for f in parsed.fragments}
    section = by_label["разд. I"]
    assert by_label["гл. 1"].parent_id == section.id
    assert by_label["гл. 2"].parent_id == section.id  # not the root: the section goes on


def test_a_sign_inside_a_sentence_is_no_section() -> None:
    parsed = parse(
        "1. Общие положения.",
        "1.1. Порядок установлен § 3 Положения и действует бессрочно.",
        "1.2. Срок — десять дней.",
    )
    assert all(f.kind != FragmentKind.PARAGRAPH_SECTION for f in parsed.fragments)
    assert parsed.diagnostics.numbering_profile == NumberingProfile.REGULATION


def test_an_appendix_header_split_into_paragraphs_opens_the_approved_document() -> None:
    parsed = parse(
        "ПОСТАНОВЛЕНИЕ",
        "1. Утвердить Положение о комиссии.",
        "2. Контроль исполнения оставляю за собой.",
        "Приложение",
        "к постановлению главы поселения",
        "от 3 марта 2021 года № 5",
        "Положение",
        "о комиссии по соблюдению требований",
        "1. Комиссия образуется постановлением главы.",
        "2. В состав комиссии входят пять человек.",
        "3. Заседание правомочно при двух третях состава.",
    )
    approved = next(f for f in parsed.fragments if f.kind == FragmentKind.APPROVED_DOCUMENT)
    points = [f for f in parsed.fragments if f.kind == FragmentKind.POINT]
    inside = [f.number for f in points if f.parent_id == approved.id]
    assert inside == ["1", "2", "3"]  # numbered anew: not a continuation of the order's points
    assert approved.heading == "Положение о комиссии по соблюдению требований"
