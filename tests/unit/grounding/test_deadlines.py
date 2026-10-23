"""Russian numerals and deadlines in quotes (R-11, FR-025, T025)."""

import pytest

from gr.domain.enums import DeadlineUnit as U
from gr.grounding.deadlines import ParsedDeadline, check_deadline, parse_deadline
from gr.grounding.numerals_ru import read_number, word_values


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # the table of tasks.md
        ("в течение двадцати дней со дня регистрации", [(20, U.DAYS)]),
        ("не более 2 рабочих дня", [(2, U.WORKING_DAYS)]),
        ("в течение трёх рабочих дней", [(3, U.WORKING_DAYS)]),
        ("не позднее следующего рабочего дня", [(None, U.NEXT_WORKING_DAY)]),
        ("время ожидания в очереди не превышает 15 минут", [(15, U.MINUTES)]),
        ("в срок 10 календарных дней", [(10, U.CALENDAR_DAYS)]),
        ("в течение одного месяца", [(1, U.MONTHS)]),
        # digits with the numeral in brackets, endings and compound adjectives
        ("не позднее 10 (десяти) рабочих дней", [(10, U.WORKING_DAYS)]),
        ("в течение 2-х рабочих дней", [(2, U.WORKING_DAYS)]),
        ("в трехдневный срок", [(3, U.DAYS)]),
        ("в 5-дневный срок", [(5, U.DAYS)]),
        ("в двухмесячный срок", [(2, U.MONTHS)]),
        # compound numerals, other cases
        ("в течение ста восьмидесяти дней", [(180, U.DAYS)]),
        ("в течение двадцати пяти календарных дней", [(25, U.CALENDAR_DAYS)]),
        ("в течение сорока восьми часов", [(48, U.HOURS)]),
        ("за тридцать дней до окончания", [(30, U.DAYS)]),
        ("не позднее чем за пять рабочих дней", [(5, U.WORKING_DAYS)]),
        ("в течение девяноста календарных дней", [(90, U.CALENDAR_DAYS)]),
        # a unit without a number is one unit
        ("в течение месяца", [(1, U.MONTHS)]),
        ("в течение рабочего дня", [(1, U.WORKING_DAYS)]),
        # weeks and years have no unit of their own
        ("в течение 2 недель", [(2, U.OTHER)]),
        # several deadlines in one quote, in order
        (
            "регистрируется в течение 1 рабочего дня, рассматривается в течение 30 дней",
            [(1, U.WORKING_DAYS), (30, U.DAYS)],
        ),
        # no deadline
        ("осуществляет одно из следующих действий", []),
        ("Пункт 2 и подпункт 5 настоящего порядка", []),
        ("не позднее дня, следующего за днем поступления", []),
    ],
)
def test_parse_deadline(quote: str, expected: list[tuple[int | None, U]]) -> None:
    assert parse_deadline(quote) == [ParsedDeadline(value, unit) for value, unit in expected]


@pytest.mark.parametrize(
    ("value", "unit", "quote"),
    [
        (20, U.DAYS, "в течение двадцати дней"),
        (20, U.CALENDAR_DAYS, "в течение двадцати дней"),  # unqualified days read as calendar
        (3, U.WORKING_DAYS, "в течение трёх рабочих дней"),
        (None, U.NEXT_WORKING_DAY, "не позднее следующего рабочего дня"),
        (30, U.DAYS, "регистрируется в течение 1 рабочего дня, рассматривается 30 дней"),
        (None, U.OTHER, "в разумный срок"),  # nothing numeric claimed, nothing to check
    ],
)
def test_check_deadline_passes(value: int | None, unit: U, quote: str) -> None:
    result = check_deadline(value, unit, quote)
    assert result.ok and result.issue is None


@pytest.mark.parametrize(
    ("value", "unit", "quote"),
    [
        (5, U.WORKING_DAYS, "в течение трёх рабочих дней"),  # wrong value
        (3, U.CALENDAR_DAYS, "в течение трёх рабочих дней"),  # wrong unit
        (3, U.DAYS, "в течение трёх рабочих дней"),  # working days are not plain days
        (10, U.DAYS, "в разумный срок"),  # not in the quote at all
        (1, U.DAYS, "не позднее следующего рабочего дня"),
    ],
)
def test_check_deadline_mismatch(value: int, unit: U, quote: str) -> None:
    result = check_deadline(value, unit, quote)
    assert not result.ok and result.issue == "deadline_not_in_quote"


def test_numeral_forms_and_compounds() -> None:
    words = word_values()
    assert words["одного"] == 1 and words["двух"] == 2 and words["восьми"] == 8
    assert words["пятнадцати"] == 15 and words["сорока"] == 40 and words["пятисот"] == 500
    assert read_number(["двухсот", "пятидесяти", "трех"], 0) == (253, 3)
    assert read_number(["двадцать", "двадцать"], 0) == (20, 1)  # two numbers, not one
    assert read_number(["пятнадцати", "трех"], 0) == (15, 1)
    assert read_number(["заявления"], 0) is None
