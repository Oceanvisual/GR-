"""Restore Word auto-numbering from numbering.xml (R-05): `%1.%2.` templates, counters, formats."""

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Word's russianLower/Upper skip ё, й, ъ, ы, ь.
_RUSSIAN = "абвгдежзиклмнопрстуфхцчшщэюя"
_ROMAN = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]
SUPPORTED_FORMATS = frozenset(
    {
        "decimal",
        "decimalZero",
        "lowerLetter",
        "upperLetter",
        "lowerRoman",
        "upperRoman",
        "russianLower",
        "russianUpper",
        "bullet",
        "none",
    }
)
BULLET = "–"


def _attr(element: Element | None, name: str = "val") -> str | None:
    return None if element is None else element.get(f"{W}{name}")


def _roman(n: int) -> str:
    out = []
    for value, symbol in _ROMAN:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def _letters(n: int, alphabet: str) -> str:
    # Word repeats the letter: a … z, aa … zz, aaa …
    size = len(alphabet)
    return alphabet[(n - 1) % size] * ((n - 1) // size + 1)


def format_number(n: int, fmt: str) -> str:
    if n < 1 and fmt not in ("decimal", "decimalZero", "none", "bullet"):
        return str(n)
    match fmt:
        case "decimalZero":
            return f"{n:02d}"
        case "lowerLetter":
            return _letters(n, "abcdefghijklmnopqrstuvwxyz")
        case "upperLetter":
            return _letters(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        case "lowerRoman":
            return _roman(n).lower()
        case "upperRoman":
            return _roman(n)
        case "russianLower":
            return _letters(n, _RUSSIAN)
        case "russianUpper":
            return _letters(n, _RUSSIAN.upper())
        case "bullet":
            return BULLET
        case "none":
            return ""
        case _:
            return str(n)


@dataclass(frozen=True)
class Level:
    start: int = 1
    fmt: str = "decimal"
    text: str = "%1."
    is_legal: bool = False


@dataclass(frozen=True)
class Num:
    abstract_id: str
    start_overrides: dict[int, int] = field(default_factory=dict)


@dataclass
class NumberingDefinitions:
    abstracts: dict[str, dict[int, Level]] = field(default_factory=dict)
    nums: dict[str, Num] = field(default_factory=dict)


def parse_numbering(root: Element | None) -> NumberingDefinitions:
    defs = NumberingDefinitions()
    if root is None:
        return defs
    for abstract in root.iter(f"{W}abstractNum"):
        levels: dict[int, Level] = {}
        for lvl in abstract.iter(f"{W}lvl"):
            ilvl = int(_attr(lvl, "ilvl") or 0)
            start = _attr(lvl.find(f"{W}start"))
            levels[ilvl] = Level(
                start=int(start) if start and start.lstrip("-").isdigit() else 1,
                fmt=_attr(lvl.find(f"{W}numFmt")) or "decimal",
                text=_attr(lvl.find(f"{W}lvlText")) or "",
                is_legal=lvl.find(f"{W}isLgl") is not None,
            )
        defs.abstracts[_attr(abstract, "abstractNumId") or ""] = levels
    for num in root.iter(f"{W}num"):
        overrides: dict[int, int] = {}
        for override in num.iter(f"{W}lvlOverride"):
            start = _attr(override.find(f"{W}startOverride"))
            if start is not None and start.lstrip("-").isdigit():
                overrides[int(_attr(override, "ilvl") or 0)] = int(start)
        defs.nums[_attr(num, "numId") or ""] = Num(
            abstract_id=_attr(num.find(f"{W}abstractNumId")) or "",
            start_overrides=overrides,
        )
    return defs


@dataclass
class _Counters:
    values: dict[int, int] = field(default_factory=dict)


class NumberingState:
    """Counters across the document. Lists that share an abstractNum continue one count unless
    the num restarts it with startOverride (Word behaviour)."""

    def __init__(self, defs: NumberingDefinitions) -> None:
        self.defs = defs
        self._counters: dict[str, _Counters] = {}
        self.warnings: set[str] = set()

    def _levels(self, num: Num) -> dict[int, Level]:
        return self.defs.abstracts.get(num.abstract_id, {})

    def _start(self, num: Num, ilvl: int) -> int:
        if ilvl in num.start_overrides:
            return num.start_overrides[ilvl]
        return self._levels(num).get(ilvl, Level()).start

    def next_marker(self, num_id: str, ilvl: int) -> str | None:
        """Advance the counter for (num, level) and render its marker; None if not a list."""
        num = self.defs.nums.get(num_id)
        if num_id in ("", "0") or num is None:
            return None
        levels = self._levels(num)
        level = levels.get(ilvl)
        if level is None:
            return None
        key = f"num:{num_id}" if num.start_overrides else f"abs:{num.abstract_id}"
        counters = self._counters.setdefault(key, _Counters())
        current = counters.values.get(ilvl)
        counters.values[ilvl] = self._start(num, ilvl) if current is None else current + 1
        for deeper in [lvl for lvl in counters.values if lvl > ilvl]:
            del counters.values[deeper]

        if level.fmt not in SUPPORTED_FORMATS:
            self.warnings.add(f"формат нумерации {level.fmt} показан как десятичный")
        if level.fmt == "bullet":
            return BULLET
        marker = level.text
        for k in range(8, -1, -1):  # %10 does not exist; replace high placeholders first
            placeholder = f"%{k + 1}"
            if placeholder not in marker:
                continue
            value = counters.values.get(k, self._start(num, k))
            k_level = levels.get(k, Level())
            # isLgl: the level shows every number in Arabic digits ("I." above → "1.1.").
            fmt = "decimal" if level.is_legal else k_level.fmt
            if fmt == "bullet":
                fmt = "decimal"
            marker = marker.replace(placeholder, format_number(value, fmt))
        return marker.strip() or None
