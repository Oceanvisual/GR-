"""Russian cardinal numerals in every case, 1–999, as words (R-11: deadlines in quotes).

Text is expected lower-cased with ё replaced by е. Compound numbers are read greedily from
the largest order down: «ста восьмидесяти пяти» → 185.
"""

from functools import cache

# Each value with every case form it takes (feminine and neuter forms of 1 and 2 included).
_UNITS: dict[int, tuple[str, ...]] = {
    1: ("один", "одного", "одному", "одним", "одном", "одна", "одной", "одну", "одною", "одно"),
    2: ("два", "две", "двух", "двум", "двумя"),
    3: ("три", "трех", "трем", "тремя"),
    4: ("четыре", "четырех", "четырем", "четырьмя"),
}
# 5–20 and 30 decline like «пять»: nominative -ь, oblique -и, instrumental -ью.
_SOFT_STEMS: dict[int, str] = {
    5: "пят",
    6: "шест",
    7: "сем",
    8: "восем",
    9: "девят",
    10: "десят",
    11: "одиннадцат",
    12: "двенадцат",
    13: "тринадцат",
    14: "четырнадцат",
    15: "пятнадцат",
    16: "шестнадцат",
    17: "семнадцат",
    18: "восемнадцат",
    19: "девятнадцат",
    20: "двадцат",
    30: "тридцат",
}
_TENS: dict[int, tuple[str, ...]] = {
    40: ("сорок", "сорока"),
    50: ("пятьдесят", "пятидесяти", "пятьюдесятью"),
    60: ("шестьдесят", "шестидесяти", "шестьюдесятью"),
    70: ("семьдесят", "семидесяти", "семьюдесятью"),
    80: ("восемьдесят", "восьмидесяти", "восемьюдесятью"),
    90: ("девяносто", "девяноста"),
}
_HUNDREDS: dict[int, tuple[str, ...]] = {
    100: ("сто", "ста"),
    200: ("двести", "двухсот", "двумстам", "двумястами", "двухстах"),
    300: ("триста", "трехсот", "тремстам", "тремястами", "трехстах"),
    400: ("четыреста", "четырехсот", "четыремстам", "четырьмястами", "четырехстах"),
    500: ("пятьсот", "пятисот", "пятистам", "пятьюстами", "пятистах"),
    600: ("шестьсот", "шестисот", "шестистам", "шестьюстами", "шестистах"),
    700: ("семьсот", "семисот", "семистам", "семьюстами", "семистах"),
    800: ("восемьсот", "восьмисот", "восьмистам", "восьмьюстами", "восьмистах"),
    900: ("девятьсот", "девятисот", "девятистам", "девятьюстами", "девятистах"),
}


def _soft_forms(value: int, stem: str) -> tuple[str, ...]:
    forms = [stem + "ь", stem + "и", stem + "ью"]
    if value == 8:
        forms += ["восьми", "восьмью"]  # the stem alternates: восемь → восьми
    return tuple(forms)


@cache
def word_values() -> dict[str, int]:
    """Every numeral word form → its value."""
    table: dict[str, int] = {}
    for value, forms in _UNITS.items():
        table.update(dict.fromkeys(forms, value))
    for value, stem in _SOFT_STEMS.items():
        table.update(dict.fromkeys(_soft_forms(value, stem), value))
    for group in (_TENS, _HUNDREDS):
        for value, forms in group.items():
            table.update(dict.fromkeys(forms, value))
    return table


@cache
def compound_stems() -> dict[str, int]:
    """Numeral stems of compound adjectives: «пяти|дневный», «трех|месячный», «одно|дневный»."""
    stems = {form: value for form, value in word_values().items() if form.endswith(("и", "х"))}
    stems |= {"одно": 1, "двух": 2, "сорока": 40, "девяноста": 90, "ста": 100}
    return stems


def _order(value: int) -> int:
    return 100 if value >= 100 else 10 if value >= 20 else 1


def read_number(words: list[str], start: int) -> tuple[int, int] | None:
    """The numeral starting at `words[start]`: (value, number of words), or None."""
    table = word_values()
    total = 0
    used = 0
    last_order = 1000
    while start + used < len(words):
        value = table.get(words[start + used])
        if value is None:
            break
        order = _order(value)
        if order >= last_order:
            break  # «двадцати двадцати» or «пяти десяти» are two numbers, not one
        total += value
        used += 1
        last_order = order if value >= 20 else 0  # after a unit or a teen nothing can follow
    return (total, used) if used else None
