"""Deadlines in quotes (R-11, FR-025): «10 (десяти) рабочих дней», «трехдневный срок»,
«не позднее следующего рабочего дня». A deadline the model reports must be read from its quote.

A deadline is calendar duration, never labour time (FR-029); this module only reads it.
"""

import re
from dataclasses import dataclass

from gr.domain.enums import DeadlineUnit
from gr.grounding.numerals_ru import compound_stems, read_number

_WORD_RE = re.compile(r"\d+-[а-я]+|\d+|[а-яa-z]+")
_DIGITS_WITH_ENDING_RE = re.compile(r"(\d+)-[а-я]{1,3}")  # «2-х», «3-м»
_PAREN_NUMERAL_RE = re.compile(r"(\d+)\s*\(\s*([а-я ]+?)\s*\)")  # «10 (десяти)»

_DAY = ("день", "дня", "дней", "днем", "дням", "днями", "днях", "дни")
_UNIT_NOUNS: dict[str, DeadlineUnit] = {
    **dict.fromkeys(_DAY, DeadlineUnit.DAYS),
    **dict.fromkeys(
        ("минута", "минуты", "минут", "минуту", "минутами", "минутах"), DeadlineUnit.MINUTES
    ),
    **dict.fromkeys(("час", "часа", "часов", "часам", "часами", "часах"), DeadlineUnit.HOURS),
    **dict.fromkeys(
        ("месяц", "месяца", "месяцев", "месяцам", "месяцами", "месяцах"), DeadlineUnit.MONTHS
    ),
    # Weeks and years have no unit of their own (data-model): `other`, the value as written.
    **dict.fromkeys(("неделя", "недели", "недель", "неделю", "неделями"), DeadlineUnit.OTHER),
    **dict.fromkeys(("год", "года", "лет", "годам", "годами"), DeadlineUnit.OTHER),
}
_WORKING = ("рабочий", "рабочего", "рабочих", "рабочим", "рабочими", "рабочем", "рабочие")
_CALENDAR = ("календарный", "календарного", "календарных", "календарным", "календарными")
_NEXT = ("следующий", "следующего", "следующем", "следующему")
_DURING = ("течение", "срок")  # «в течение месяца», «в срок, не превышающий»
_ADJECTIVE_UNITS: dict[str, DeadlineUnit] = {
    "дневн": DeadlineUnit.DAYS,
    "месячн": DeadlineUnit.MONTHS,
    "часов": DeadlineUnit.HOURS,
    "минутн": DeadlineUnit.MINUTES,
    "недельн": DeadlineUnit.OTHER,
}
_ADJECTIVE_RE = re.compile(r"^(\d+|[а-я]+?)-?(" + "|".join(_ADJECTIVE_UNITS) + r")[а-я]*$")


@dataclass(frozen=True)
class ParsedDeadline:
    value: int | None  # None only for «следующий рабочий день»
    unit: DeadlineUnit


@dataclass(frozen=True)
class DeadlineCheck:
    ok: bool
    found: list[ParsedDeadline]
    issue: str | None = None  # `deadline_not_in_quote` when not ok


def _words(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    text = _PAREN_NUMERAL_RE.sub(r"\1", text)  # the digits already say it
    words = _WORD_RE.findall(text)
    return [m.group(1) if (m := _DIGITS_WITH_ENDING_RE.fullmatch(w)) else w for w in words]


def _at(words: list[str], i: int, options: tuple[str, ...]) -> bool:
    return i < len(words) and words[i] in options


def _unit_after(words: list[str], i: int) -> tuple[DeadlineUnit, int] | None:
    """The unit noun at `words[i]`, possibly after «рабочих» / «календарных»."""
    kind: DeadlineUnit | None = None
    if i < len(words) and words[i] in _WORKING:
        kind, i = DeadlineUnit.WORKING_DAYS, i + 1
    elif i < len(words) and words[i] in _CALENDAR:
        kind, i = DeadlineUnit.CALENDAR_DAYS, i + 1
    if i >= len(words) or words[i] not in _UNIT_NOUNS:
        return None
    unit = _UNIT_NOUNS[words[i]]
    if kind is not None and unit == DeadlineUnit.DAYS:
        unit = kind
    return unit, i + 1


def _adjective(word: str) -> ParsedDeadline | None:
    """«трехдневный», «10-дневный», «двухмесячный» срок."""
    match = _ADJECTIVE_RE.match(word)
    if match is None:
        return None
    head, unit = match.group(1), _ADJECTIVE_UNITS[match.group(2)]
    value = int(head) if head.isdigit() else compound_stems().get(head)
    return ParsedDeadline(value, unit) if value is not None else None


def parse_deadline(quote: str) -> list[ParsedDeadline]:
    """Every deadline written in the quote, in order of appearance."""
    words = _words(quote)
    found: list[ParsedDeadline] = []
    i = 0
    while i < len(words):
        word = words[i]
        # «следующего рабочего дня»: the next working day, no number.
        if word in _NEXT and _at(words, i + 1, _WORKING) and _at(words, i + 2, _DAY):
            found.append(ParsedDeadline(None, DeadlineUnit.NEXT_WORKING_DAY))
            i += 3
            continue
        number = (int(word), 1) if word.isdigit() else read_number(words, i)
        if number is not None:
            value, used = number
            unit = _unit_after(words, i + used)
            if unit is not None:
                found.append(ParsedDeadline(value, unit[0]))
                i = unit[1]
                continue
        # «в течение месяца», «в течение рабочего дня»: one unit without a number.
        if word in _DURING:
            unit = _unit_after(words, i + 1)
            if unit is not None:
                found.append(ParsedDeadline(1, unit[0]))
                i = unit[1]
                continue
        adjective = _adjective(word)
        if adjective is not None:
            found.append(adjective)
        i += 1
    return found


def _same(model: ParsedDeadline, quoted: ParsedDeadline) -> bool:
    if model.value != quoted.value:
        return False
    # Unqualified «дней» in the act: the model may read them as calendar days.
    if quoted.unit == DeadlineUnit.DAYS:
        return model.unit in (DeadlineUnit.DAYS, DeadlineUnit.CALENDAR_DAYS)
    return model.unit == quoted.unit


def check_deadline(value: int | None, unit: DeadlineUnit, quote: str) -> DeadlineCheck:
    """Is the deadline the model reported written in its quote (value and unit)?"""
    found = parse_deadline(quote)
    if value is None and unit == DeadlineUnit.OTHER:
        return DeadlineCheck(ok=True, found=found)  # nothing numeric was claimed
    claimed = ParsedDeadline(value, unit)
    if any(_same(claimed, quoted) for quoted in found):
        return DeadlineCheck(ok=True, found=found)
    return DeadlineCheck(ok=False, found=found, issue="deadline_not_in_quote")
