"""Is a number a plausible next item? Keeps «10 дней», «2020 г.» out of points."""

from enum import StrEnum

MAX_COMPONENT = 300
MAX_DEPTH = 5
MAX_GAP = 2  # 3.5 → 3.7 is accepted: an item may be missing (repealed, extraction error)
RUSSIAN_LETTERS = "абвгдежзиклмнопрстуфхцчшщэюя"  # no ё, й, ъ, ы, ь in legal lists
LATIN_LETTERS = "abcdefghijklmnopqrstuvwxyz"


class Verdict(StrEnum):
    OK = "ok"
    DUPLICATE = "duplicate"
    REJECT = "reject"


def parse_number(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split(".") if part)


def _ones(parts: tuple[int, ...]) -> bool:
    return all(part == 1 for part in parts)


def plausible_point(
    last: tuple[int, ...] | None,
    candidate: tuple[int, ...],
    *,
    section: int | None = None,
    section_changed: bool = False,
) -> Verdict:
    """Decimal points: the next sibling at any level (gap ≤ MAX_GAP), the first child, or the first
    point of a new section numbered after it («3.1» in section III)."""
    if not candidate or len(candidate) > MAX_DEPTH:
        return Verdict.REJECT
    if any(part == 0 or part > MAX_COMPONENT for part in candidate):
        return Verdict.REJECT
    if last is None:
        first_ok = candidate[-1] <= 2 or (len(candidate) == 1 and candidate[0] <= 3)
        if section is not None and candidate[0] == section and _ones(candidate[1:]):
            first_ok = True
        return Verdict.OK if first_ok else Verdict.REJECT
    if candidate == last:
        return Verdict.DUPLICATE
    for level in range(len(last)):
        for step in range(1, MAX_GAP + 1):
            base = (*last[:level], last[level] + step)
            if candidate[: len(base)] == base and _ones(candidate[len(base) :]):
                return Verdict.OK
    if (
        len(candidate) > len(last)
        and candidate[: len(last)] == last
        and _ones(candidate[len(last) :])
    ):
        return Verdict.OK
    if section_changed and section is not None and candidate[0] == section and _ones(candidate[1:]):
        return Verdict.OK
    return Verdict.REJECT


def plausible_subpoint(last: tuple[int, ...] | None, candidate: tuple[int, ...]) -> Verdict:
    """Numbered subpoints «1)», «2)», inserted «3.1)»."""
    if not candidate or candidate[0] == 0 or candidate[0] > MAX_COMPONENT:
        return Verdict.REJECT
    if last is None:
        return Verdict.OK if candidate[0] <= 2 and len(candidate) == 1 else Verdict.REJECT
    if candidate == last:
        return Verdict.DUPLICATE
    if len(candidate) == 1 and 0 < candidate[0] - last[0] <= MAX_GAP:
        return Verdict.OK
    if len(candidate) == 2 and candidate[0] == last[0]:
        previous = last[1] if len(last) == 2 else 0
        return Verdict.OK if 0 < candidate[1] - previous <= MAX_GAP else Verdict.REJECT
    return Verdict.REJECT


def plausible_letter(last: str | None, candidate: str) -> Verdict:
    alphabet = RUSSIAN_LETTERS if candidate in RUSSIAN_LETTERS else LATIN_LETTERS
    if candidate not in alphabet:
        return Verdict.REJECT
    index = alphabet.index(candidate)
    if last is None or last not in alphabet:
        return Verdict.OK if index <= 1 else Verdict.REJECT
    if candidate == last:
        return Verdict.DUPLICATE
    return Verdict.OK if 0 < index - alphabet.index(last) <= MAX_GAP else Verdict.REJECT
