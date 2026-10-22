"""Quote verification (R-11, FR-025, T024)."""

import random
import time
from functools import cache
from pathlib import Path

import pytest

from gr.domain.document import Fragment
from gr.domain.enums import GroundingStatus
from gr.grounding.normalize import normalize_for_match
from gr.grounding.quotes import QuoteIndex, load_config, verify_quote
from gr.ingestion.docx import DocxExtractor
from gr.ingestion.txt import TxtExtractor
from gr.services.parsing import ParseLimits, parse_raw

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)

REGULATION = """\
ПОРЯДОК ВЫДАЧИ ПРОПУСКОВ
1. Общие положения
1.1. Пропуск выдаётся на основании заявления, поданного в уполномоченный орган.
1.2. Заявление регистрируется в течение одного рабочего дня со дня его поступления.
2. Рассмотрение заявления
2.1. Специалист проверяет комплектность документов и наличие оснований для отказа.
2.2. Решение о выдаче пропуска принимается в течение десяти рабочих дней.
2.3. Об отказе в выдаче пропуска заявитель уведомляется в письменной форме «по почте».
"""


@cache
def small() -> tuple[QuoteIndex, dict[str, Fragment], str]:
    parsed = parse_raw(TxtExtractor().extract(REGULATION.encode()), "doc_T", LIMITS)
    return QuoteIndex(parsed.fragments), {f.label: f for f in parsed.fragments}, parsed.text


@cache
def reg261() -> tuple[QuoteIndex, list[Fragment], str]:
    raw = DocxExtractor().extract((FIXTURES / "npa" / "reg261.docx").read_bytes())
    parsed = parse_raw(raw, "doc_R", LIMITS)
    return QuoteIndex(parsed.fragments), parsed.fragments, parsed.text


def check(quote: str, label: str) -> tuple[GroundingStatus, float, str, str | None]:
    index, by_label, _ = small()
    match = index.verify(quote, by_label[label].id)
    grounding = match.grounding
    return grounding.status, grounding.score, match.fragment_id, grounding.original_fragment_id


def test_index_text_is_the_document_text() -> None:
    index, _, text = small()
    assert index.text == text


def test_exact_quote_is_verified_with_its_offsets() -> None:
    index, by_label, text = small()
    quote = "регистрируется в течение одного рабочего дня"
    match = index.verify(quote, by_label["п. 1.2"].id)
    grounding = match.grounding
    assert (grounding.status, grounding.score) == (GroundingStatus.VERIFIED, 100)
    assert match.fragment_id == by_label["п. 1.2"].id and grounding.original_fragment_id is None
    assert text[grounding.matched_start : grounding.matched_end] == quote
    assert grounding.status.passes


@pytest.mark.parametrize(
    "quote",
    [
        "РЕГИСТРИРУЕТСЯ   в течение одного рабочего дня",
        "Пропуск выдается на основании заявления",  # ё → е
        'уведомляется в письменной форме "по почте"',  # other quotes
        "уведомляется в письменной форме „по почте“",
        "регистри-\nруется в течение одного рабочего дня",  # line-break hyphen
    ],
)
def test_normalisation_differences_are_exact(quote: str) -> None:
    label = "п. 1.1" if "Пропуск" in quote else "п. 2.3" if "почте" in quote else "п. 1.2"
    status, score, _, _ = check(quote, label)
    assert (status, score) == (GroundingStatus.VERIFIED, 100)


def test_quote_from_a_child_verifies_against_the_parent() -> None:
    status, score, fragment_id, _ = check("принимается в течение десяти рабочих дней", "п. 2")
    _, by_label, _ = small()
    assert (status, score) == (GroundingStatus.VERIFIED, 100)
    assert fragment_id == by_label["п. 2"].id  # the citation stays where the model put it


def test_quote_from_a_neighbouring_point_is_relocated() -> None:
    _, by_label, _ = small()
    status, score, fragment_id, original = check(
        "принимается в течение десяти рабочих дней", "п. 2.1"
    )
    assert status == GroundingStatus.RELOCATED and status.passes and score == 100
    assert fragment_id == by_label["п. 2.2"].id and original == by_label["п. 2.1"].id


def test_unknown_fragment_id_is_relocated_or_unverified() -> None:
    index, by_label, _ = small()
    found = index.verify("принимается в течение десяти рабочих дней", "body/99")
    assert found.grounding.status == GroundingStatus.RELOCATED
    assert found.fragment_id == by_label["п. 2.2"].id
    missing = index.verify("документы хранятся в архиве пятьдесят лет", "body/99")
    assert missing.grounding.status == GroundingStatus.UNVERIFIED
    assert missing.grounding.reason == "фрагмент не найден"


def test_close_paraphrase_is_partial_and_does_not_pass() -> None:
    status, score, _, _ = check(
        "Специалист проверяет полноту документов и наличие оснований", "п. 2.1"
    )
    assert status == GroundingStatus.PARTIAL and not status.passes
    assert load_config().fuzzy_partial <= score < load_config().fuzzy_ok


def test_invented_text_is_unverified() -> None:
    status, score, _, _ = check("специалист обязан немедленно вернуть все документы", "п. 2.1")
    assert status == GroundingStatus.UNVERIFIED and score < load_config().fuzzy_partial


@pytest.mark.parametrize("quote", ["в течение", "", "   …   ", "дней" * 200])
def test_short_or_long_quotes_are_invalid(quote: str) -> None:
    status, score, _, _ = check(quote, "п. 1.2")
    assert (status, score) == (GroundingStatus.INVALID_QUOTE, 0)


def test_elided_quote_parts_must_appear_in_order() -> None:
    in_order = check("Специалист проверяет … наличие оснований для отказа", "п. 2.1")
    assert in_order[:2] == (GroundingStatus.VERIFIED, 100)
    reversed_parts = check("наличие оснований для отказа ... Специалист проверяет", "п. 2.1")
    assert not reversed_parts[0].passes


def test_elided_quote_spans_from_first_to_last_part() -> None:
    index, by_label, text = small()
    grounding = index.verify(
        "Пропуск выдаётся … в уполномоченный орган", by_label["п. 1.1"].id
    ).grounding
    quoted = text[grounding.matched_start : grounding.matched_end]
    assert quoted.startswith("Пропуск") and quoted.endswith("орган")


def test_single_call_helper() -> None:
    _, by_label, _ = small()
    fragments = list(by_label.values())
    match = verify_quote("Заявление регистрируется в течение", by_label["п. 1.2"].id, fragments)
    assert match.grounding.status == GroundingStatus.VERIFIED


# --- the real regulation -------------------------------------------------------------------


def test_reg261_relocation_finds_the_right_point_far_away() -> None:
    index, fragments, _ = reg261()
    target = next(f for f in fragments if f.label == "п. 3.5.1")
    first = next(f for f in fragments if f.label == "п. 1.1")
    quote = target.text[12:110]
    match = index.verify(quote, first.id)
    assert match.grounding.status == GroundingStatus.RELOCATED
    assert match.fragment_id == target.id


def test_500_quotes_on_a_long_document_within_budget() -> None:
    """SC: 500 quotes on a document of ~130 thousand characters in at most 5 seconds."""
    _, fragments, _ = reg261()
    rng = random.Random(7)
    texts = [f for f in fragments if len(f.text) >= 120]
    started = time.perf_counter()
    index = QuoteIndex(fragments)  # building the index is part of the budget
    statuses: list[GroundingStatus] = []
    for i in range(500):
        fragment = rng.choice(texts)
        start = rng.randrange(0, len(fragment.text) - 100)
        quote = fragment.text[start : start + rng.randint(40, 100)]
        cited = fragment.id
        if i % 4 == 1:
            quote = quote.replace(" ", "  ").replace("е", "ё")  # normalisation only
        elif i % 4 == 2:
            cited = rng.choice(texts).id  # wrong reference: relocation over the whole text
        elif i % 4 == 3:
            words = quote.split()
            rng.shuffle(words)
            quote = " ".join(words)  # garbled: full fuzzy search, then a refusal
        statuses.append(index.verify(quote, cited).grounding.status)
    elapsed = time.perf_counter() - started
    assert elapsed <= 5.0, f"{elapsed:.2f} s"
    passed = sum(1 for i, s in enumerate(statuses) if i % 4 != 3 and s.passes)
    assert passed >= 370  # exact and relocated quotes pass (a few short ones may repeat)


def test_normalised_quote_length_counts() -> None:
    # 12 characters after normalisation is the lower bound, spaces collapsed first.
    assert len(normalize_for_match("рабочего   дня")) == 12
    assert check("рабочего   дня", "п. 1.2")[0] == GroundingStatus.VERIFIED
    assert check("абочего дня", "п. 1.2")[0] == GroundingStatus.INVALID_QUOTE  # 11


def test_reg261_relocation_tolerates_a_typo() -> None:
    index, fragments, _ = reg261()
    target = next(f for f in fragments if f.label == "п. 3.5.1")
    first = next(f for f in fragments if f.label == "п. 1.1")
    quote = target.text[12:110]
    typo = quote[:40] + ("а" if quote[40] != "а" else "о") + quote[41:]
    match = index.verify(typo, first.id)
    assert match.grounding.status == GroundingStatus.RELOCATED
    assert match.fragment_id == target.id and 92 <= match.grounding.score < 100
