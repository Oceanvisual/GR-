"""Document requisites from the text (FR-006, R-07): kind of act, issuer, date, number, title,
edition date, Ministry of Justice registration. Deterministic; each value carries an exact quote
with its offsets and the smallest fragment containing it, so it passes the same check as any
other citation (R-11). Anything not found stays None: the user can enter it."""

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from gr.domain.document import Citation, DocumentMetadata, Fragment, GroundingResult, MetaField
from gr.domain.enums import FragmentKind, GroundingStatus

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
    "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}  # fmt: skip
DATE = r"(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4})"
_YEAR_WORD = r"(?:\s*(?:г\.|года|г\b))?"
_NUMBER = r"(?:N|№)\s*(?P<number>\d[\w\-/.]*?)(?=[\s,;)(\"«»]|$)"
DATE_NUMBER_RE = re.compile(rf"\bот\s+(?P<date>{DATE}){_YEAR_WORD}\s*{_NUMBER}", re.I)
SIGNATURE_RE = re.compile(rf"(?P<date>{DATE}){_YEAR_WORD}\s*\n?\s*{_NUMBER}", re.I)
# «Приказ Минздрава России № 12н от 3 марта 2021 г.»: the order of web pages; trusted on
# the act line only, elsewhere it cites another act as often as not.
NUMBER_DATE_RE = re.compile(rf"{_NUMBER}\s+от\s+(?P<date>{DATE})", re.I)
LEADING_DATE_NUMBER_RE = re.compile(rf"^(?P<date>{DATE}){_YEAR_WORD}\s*\|?\s*{_NUMBER}", re.I)
EDITION_RE = re.compile(rf"\(ред\.\s*от\s+(?P<date>{DATE}){_YEAR_WORD}\)", re.I)
CHANGES_RE = re.compile(rf"с изменениями на\s+(?P<date>{DATE})", re.I)
MINJUST_RE = re.compile(
    rf"Зарегистрирован\w*\s+в\s+(?:Минюсте(?:\s+России)?|Министерстве\s+юстиции\s+"
    rf"Российской\s+Федерации)\s+(?P<date>{DATE}){_YEAR_WORD}\s*,?\s*"
    rf"(?:регистрационный\s+)?(?:N|№)\s*(?P<number>\d+)",
    re.I,
)
TITLE_START_RE = re.compile(r"^(?:О|ОБ|Об)\s")
COVER_TITLE_RE = re.compile(r"\"(?P<title>(?:О|Об)\s[^\"]+)\"")
TAIL_CHARS = 1500
_K = FragmentKind
_TITLE_STOP = ("Список изменяющих", "В соответствии", "Принят", "Документ предоставлен")


@dataclass(frozen=True)
class MetadataConfig:
    head_chars: int
    act_types: list[str]
    issuer_words: list[str]
    amending_prefixes: list[str]


@cache
def load_config() -> MetadataConfig:
    raw: dict[str, Any] = tomllib.loads(
        resources.files("gr.config").joinpath("structure_patterns.toml").read_text("utf-8")
    )["metadata"]
    return MetadataConfig(
        head_chars=raw["head_chars"],
        act_types=sorted(raw["act_types"], key=len, reverse=True),
        issuer_words=raw["issuer_words"],
        amending_prefixes=raw["amending_prefixes"],
    )


@dataclass(frozen=True)
class Line:
    start: int
    end: int
    text: str


def normalize_date(value: str) -> str:
    """«11 сентября 2020» or «11.09.2020» → «11.09.2020»."""
    parts = value.split()
    if len(parts) == 3 and parts[1].lower() in MONTHS:
        return f"{int(parts[0]):02d}.{MONTHS[parts[1].lower()]:02d}.{parts[2]}"
    day, month, year = value.split(".")
    return f"{int(day):02d}.{int(month):02d}.{year}"


def _capitals(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) >= 0.9 * len(letters)


class _Extractor:
    def __init__(self, fragments: list[Fragment], text: str, config: MetadataConfig) -> None:
        self.fragments = fragments
        self.text = text
        self.config = config
        self.lines: list[Line] = []
        offset = 0
        for raw in text[: config.head_chars].split("\n"):
            self.lines.append(Line(offset, offset + len(raw), raw))
            offset += len(raw) + 1

    @property
    def body_start(self) -> int:
        body = (_K.SECTION, _K.CHAPTER, _K.ARTICLE, _K.POINT, _K.PART, _K.APPROVED_DOCUMENT)
        starts = [f.start for f in self.fragments if f.kind in body and f.parent_id != "preamble"]
        return min(starts, default=len(self.text))

    def signature(self) -> tuple[MetaField, MetaField] | None:
        """Date and number in the signature block at the end, possibly on two lines."""
        offset = max(0, len(self.text) - TAIL_CHARS)
        found = None
        for match in SIGNATURE_RE.finditer(self.text, offset):
            if not self.text[max(0, match.start() - 4) : match.start()].rstrip().endswith("от"):
                found = match  # the last one wins: the signature closes the act
        if found is None:
            return None
        date = normalize_date(found.group("date"))
        return (
            self.cite(found.start("date"), found.end("date"), date),
            self.cite(found.start("number"), found.end("number"), found.group("number")),
        )

    def cite(self, start: int, end: int, value: str) -> MetaField:
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        owner = min(
            (f for f in self.fragments if f.start <= start and end <= f.end),
            key=lambda f: f.end - f.start,
        )
        citation = Citation(
            fragment_id=owner.id,
            quote=self.text[start:end],
            grounding=GroundingResult(
                status=GroundingStatus.VERIFIED, score=100, matched_start=start, matched_end=end
            ),
        )
        return MetaField(value=value, citation=citation)

    # --- kind of act and issuer --------------------------------------------------------

    def act_line(self) -> tuple[int, str, int] | None:
        """(line index, act type, offset of the act word in the line); official form first."""
        official = None
        cover = None
        for index, line in enumerate(self.lines):
            stripped = line.text.strip()
            for act in self.config.act_types:
                upper = stripped.upper()
                position = upper.find(act)
                if position < 0:
                    continue
                before, after = stripped[:position], stripped[position + len(act) :]
                in_capitals = stripped[position : position + len(act)] == act
                standalone = not after.strip() or re.match(r"^\s+от\s", after)
                # «ЗАКОН СЕВЕРНОЙ ОБЛАСТИ»: the subject follows the kind of act.
                subject_after = position == 0 and _capitals(after) and not before
                if (
                    in_capitals
                    and (standalone or subject_after)
                    and (not before or _capitals(before))
                ):
                    official = official or (index, act, position)
                elif position == 0 and re.search(r"\bот\s+\d", after):
                    cover = cover or (index, act, position)
                break
            if official:
                return official
        return cover

    def issuer(self, act_index: int, act_offset: int) -> MetaField:
        line = self.lines[act_index]
        prefix = line.text[:act_offset].strip()
        if prefix:  # «СЕВЕРНАЯ ОБЛАСТЬ ОБЛАСТНОЙ ЗАКОН»
            return self.cite(line.start, line.start + act_offset, prefix)
        act_end = act_offset + len(self.act_word(line.text, act_offset))
        suffix = line.text[act_end:].strip()
        if (
            suffix and _capitals(suffix) and not re.match(r"от\s", suffix)
        ):  # «ЗАКОН СЕВЕРНОЙ ОБЛАСТИ»
            return self.cite(line.start + act_end, line.end, suffix)
        block: list[Line] = []
        for previous in reversed(self.lines[max(0, act_index - 5) : act_index]):
            text = previous.text.strip()
            if not text or re.search(r"\d", text) or len(text) > 150:
                break
            block.insert(0, previous)
            if text.split()[0].upper() in self.config.issuer_words:
                break
        if not block:
            return MetaField()
        if block[0].text.split()[0].upper() not in self.config.issuer_words:
            block = block[-1:] if _capitals(block[-1].text) else []
        if not block:
            return MetaField()
        value = " ".join(b.text.strip() for b in block)
        return self.cite(block[0].start, block[-1].end, value)

    # --- date and number ---------------------------------------------------------------

    def act_word(self, text: str, offset: int) -> str:
        return next((a for a in self.config.act_types if text.upper().startswith(a, offset)), "")

    def _own_line(self, line: Line) -> bool:
        stripped = line.text.lstrip()
        if stripped.startswith(tuple(self.config.amending_prefixes)):
            return False
        return "Зарегистрирован" not in stripped and "Список изменяющих" not in stripped

    def date_number(self, act_index: int | None) -> tuple[MetaField, MetaField]:
        # Near the act line first; then the heading part before the body (later lines cite
        # other acts); then a signature line at the end («5 мая 2005 года N 12-оз»).
        windows: list[tuple[list[Line], bool]] = []
        if act_index is not None:
            windows.append((self.lines[act_index : act_index + 3], True))
        windows.append(([line for line in self.lines if line.end <= self.body_start], False))
        for window, act_lines in windows:
            for line in window:
                if not self._own_line(line):
                    continue
                indent = len(line.text) - len(line.text.lstrip())
                stripped = line.text.strip()
                match = (
                    DATE_NUMBER_RE.search(stripped)
                    or LEADING_DATE_NUMBER_RE.match(stripped)
                    or (NUMBER_DATE_RE.search(stripped) if act_lines else None)
                )
                if match is None:
                    continue
                base = line.start + indent
                date = normalize_date(match.group("date"))
                return (
                    self.cite(base + match.start("date"), base + match.end("date"), date),
                    self.cite(base + match.start("number"), base + match.end("number"),
                              match.group("number")),
                )  # fmt: skip
        return self.signature() or (MetaField(), MetaField())

    # --- title, edition, registration --------------------------------------------------

    def title(self, act_index: int | None) -> MetaField:
        start_index = act_index if act_index is not None else 0
        for index in range(start_index, min(start_index + 5, len(self.lines))):
            first = self.lines[index]
            if not TITLE_START_RE.match(first.text.strip()):
                continue
            block = [first]
            for following in self.lines[index + 1 : index + 10]:
                text = following.text.strip()
                if not text or text.startswith(_TITLE_STOP) or len(text) > 200:
                    break
                carried = _capitals(first.text) and _capitals(text)
                if not (carried or text[:1].islower()):
                    break
                block.append(following)
            value = " ".join(line.text.strip() for line in block)
            return self.cite(block[0].start, block[-1].end, value)
        for line in self.lines[:3]:  # a reference system's cover line: «… "Об утверждении …"»
            if match := COVER_TITLE_RE.search(line.text):
                return self.cite(
                    line.start + match.start("title"),
                    line.start + match.end("title"),
                    match.group("title"),
                )
        return MetaField()

    def edition_date(self) -> MetaField:
        head = self.text[: self.config.head_chars]
        for pattern in (EDITION_RE, CHANGES_RE):
            if match := pattern.search(head):
                return self.cite(
                    match.start("date"), match.end("date"), normalize_date(match.group("date"))
                )
        # The list of changing documents is a note, not text (cleanup.py): nothing to quote.
        return MetaField()

    def minjust(self) -> MetaField:
        if match := MINJUST_RE.search(self.text[: self.config.head_chars]):
            value = f"{normalize_date(match.group('date'))} № {match.group('number')}"
            return self.cite(match.start(), match.end(), value)
        return MetaField()


def extract_metadata(fragments: list[Fragment], text: str) -> DocumentMetadata:
    extractor = _Extractor(fragments, text, load_config())
    act = extractor.act_line()
    act_index = act[0] if act else None
    if act is not None:
        index, act_type, offset = act
        line = extractor.lines[index]
        act_field = extractor.cite(
            line.start + offset, line.start + offset + len(act_type), act_type.capitalize()
        )
        issuer = extractor.issuer(index, offset)
    else:
        act_field, issuer = MetaField(), MetaField()
    date, number = extractor.date_number(act_index)
    return DocumentMetadata(
        act_type=act_field,
        issuer=issuer,
        date=date,
        number=number,
        title=extractor.title(act_index),
        edition_date=extractor.edition_date(),
        minjust_registration=extractor.minjust(),
    )
