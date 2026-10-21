"""Normalization for structure offsets and quote matching (R-11, T019)."""

import random

import pytest

from gr.grounding.normalize import (
    normalize_document_text,
    normalize_for_match,
    normalize_for_match_mapped,
)

MATCH_CASES = [
    ("Ёлка", "елка"),
    ("«Заявление»", '"заявление"'),
    ("„Решение“ и “отказ”", '"решение" и "отказ"'),
    ("срок — 5 дней – не более", "срок - 5 дней - не более"),
    ("минус − и дефис ‑", "минус - и дефис -"),
    ("пере­нос", "перенос"),
    ("пере￾\nнос", "перенос"),  # typeset PDF hyphen marker at the line end (T072)
    ("сло-\nво", "слово"),
    ("сло- \n  во", "слово"),
    ("N 210-\nФЗ", "n 210-фз"),
    ("от 11.09.2020 № 261", "от 11.09.2020 n 261"),
    ("от 11.09.2020 N 261", "от 11.09.2020 n 261"),
    ("много   пробелов\t\nи nbsp", "много пробелов и nbsp"),
    ("  края  ", "края"),
    ("ﬁ лигатура", "fi лигатура"),
    ("ё комбинированная", "е комбинированная"),
    ("нулевой​ширины", "нулевойширины"),
    ("тире\n- в начале строки", "тире - в начале строки"),
]


@pytest.mark.parametrize(("raw", "expected"), MATCH_CASES)
def test_match_normalization(raw: str, expected: str) -> None:
    assert normalize_for_match(raw) == expected


@pytest.mark.parametrize(("raw", "_expected"), MATCH_CASES)
def test_match_normalization_idempotent(raw: str, _expected: str) -> None:
    once = normalize_for_match(raw)
    assert normalize_for_match(once) == once


def test_match_map_points_into_source() -> None:
    raw = "Заявитель  представляет «заявление» в течение 5 дней"
    norm = normalize_for_match_mapped(raw)
    start = norm.text.index('"заявление"')
    src_start, src_end = norm.source_span(start, start + len('"заявление"'))
    assert raw[src_start:src_end] == "«заявление»"
    assert norm.from_source(src_start) == start
    assert norm.to_source(len(norm.text)) == len(raw)


def test_match_map_across_joined_hyphen() -> None:
    raw = "в сро-\nки уста-\nновленные"
    norm = normalize_for_match_mapped(raw)
    assert norm.text == "в сроки установленные"
    start = norm.text.index("установленные")
    assert raw[slice(*norm.source_span(start, len(norm.text)))] == "уста-\nновленные"


DOC_CASES = [
    ("a\r\nb\rc", "a\nb\nc"),
    ("  отступ\n  строки  ", "отступ\nстроки"),
    ("абзац\n\n\n\nследующий", "абзац\n\nследующий"),
    ("пере­нос", "перенос"),
    ("NBSP и тонкий", "NBSP и тонкий"),
    ("Регистр «кавычки» — тире", "Регистр «кавычки» — тире"),
    ("№ 261", "№ 261"),
    ("ё", "ё"),
    ("табу\tляция", "табу ляция"),
    ("управляющий\x07символ", "управляющийсимвол"),
    ("\n\nначало и конец\n\n", "начало и конец"),
]


@pytest.mark.parametrize(("raw", "expected"), DOC_CASES)
def test_document_normalization(raw: str, expected: str) -> None:
    assert normalize_document_text(raw).text == expected


@pytest.mark.parametrize(("raw", "_expected"), DOC_CASES)
def test_document_normalization_idempotent(raw: str, _expected: str) -> None:
    once = normalize_document_text(raw).text
    assert normalize_document_text(once).text == once


def test_document_map_remaps_page_starts() -> None:
    raw = "стр. 1  текст\r\n\r\n\r\nстр. 2 текст"
    norm = normalize_document_text(raw)
    page2 = raw.index("стр. 2")
    assert norm.text[norm.from_source(page2) :].startswith("стр. 2")
    assert norm.from_source(0) == 0


def test_random_inputs_keep_invariants() -> None:
    rng = random.Random(11)
    alphabet = "абвёЁ N№-—–«»\"' \t\n\r ­￾1290.,;"
    for _ in range(500):
        raw = "".join(rng.choices(alphabet, k=rng.randint(0, 60)))
        for norm in (normalize_document_text(raw), normalize_for_match_mapped(raw)):
            assert len(norm.source) == len(norm.text)
            assert list(norm.source) == sorted(norm.source)
            assert all(0 <= i < len(raw) for i in norm.source)
        doc = normalize_document_text(raw).text
        assert normalize_document_text(doc).text == doc
        match = normalize_for_match(raw)
        assert normalize_for_match(match) == match
        assert "\n" not in match and "  " not in match
