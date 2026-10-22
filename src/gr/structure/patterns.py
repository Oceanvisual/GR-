"""Structure patterns from config/structure_patterns.toml and the classification of a paragraph."""

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
_TERMINAL = tuple(".;:,!?")


class Marker(StrEnum):
    APPROVED = "approved"
    ACT_APPENDIX = "act_appendix"
    ARTICLE = "article"
    APPENDIX = "appendix"
    SECTION = "section"
    CHAPTER = "chapter"
    PARAGRAPH_SECTION = "paragraph_section"
    POINT = "point"
    SUBPOINT = "subpoint"
    LETTER = "letter"
    LIST_ITEM = "list_item"
    TEXT = "text"


@dataclass(frozen=True)
class Match:
    marker: Marker
    number: str | None = None
    title: str = ""
    has_dot: bool = True


@dataclass(frozen=True)
class Patterns:
    approved: re.Pattern[str]
    approved_max: int
    act_appendix: re.Pattern[str]
    bare_appendix: re.Pattern[str]
    act_appendix_tail: re.Pattern[str]
    article: re.Pattern[str]
    article_max: int
    appendix: re.Pattern[str]
    appendix_max: int
    section: re.Pattern[str]
    section_max: int
    chapter: re.Pattern[str]
    chapter_max: int
    paragraph_section: re.Pattern[str]
    paragraph_section_max: int
    point: re.Pattern[str]
    subpoint: re.Pattern[str]
    letter: re.Pattern[str]
    list_item: re.Pattern[str]
    heading_max: int


@cache
def load_patterns() -> Patterns:
    raw: dict[str, Any] = tomllib.loads(
        resources.files("gr.config").joinpath("structure_patterns.toml").read_text("utf-8")
    )
    return Patterns(
        approved=re.compile(raw["approved"]["pattern"]),
        approved_max=raw["approved"]["max_chars"],
        act_appendix=re.compile(raw["approved"]["act_appendix"]),
        bare_appendix=re.compile(raw["approved"]["bare_appendix"]),
        act_appendix_tail=re.compile(raw["approved"]["act_appendix_tail"]),
        article=re.compile(raw["article"]["pattern"]),
        article_max=raw["article"]["max_chars"],
        appendix=re.compile(raw["appendix"]["pattern"]),
        appendix_max=raw["appendix"]["max_chars"],
        section=re.compile(raw["section"]["pattern"]),
        section_max=raw["section"]["max_chars"],
        chapter=re.compile(raw["chapter"]["pattern"]),
        chapter_max=raw["chapter"]["max_chars"],
        paragraph_section=re.compile(raw["paragraph_section"]["pattern"]),
        paragraph_section_max=raw["paragraph_section"]["max_chars"],
        point=re.compile(raw["point"]["pattern"]),
        subpoint=re.compile(raw["subpoint"]["numeric"]),
        letter=re.compile(raw["subpoint"]["letter"]),
        list_item=re.compile(raw["list_item"]["pattern"]),
        heading_max=raw["heading"]["max_chars"],
    )


def roman_to_int(value: str) -> int | None:
    """Strict Roman numeral (I–CC); None for strings like 'IIII' or 'VX'."""
    total, previous = 0, 0
    for char in reversed(value):
        current = _ROMAN.get(char)
        if current is None:
            return None
        total = total - current if current < previous else total + current
        previous = max(previous, current)
    return total if 0 < total <= 200 and int_to_roman(total) == value else None


def int_to_roman(value: int) -> str:
    out = []
    for number, symbol in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                           (5, "V"), (4, "IV"), (1, "I")):  # fmt: skip
        while value >= number:
            out.append(symbol)
            value -= number
    return "".join(out)


def classify(text: str, patterns: Patterns | None = None) -> Match:
    p = patterns or load_patterns()
    if len(text) <= p.approved_max and p.approved.match(text):
        return Match(Marker.APPROVED)
    if len(text) <= p.appendix_max and p.act_appendix.match(text):
        return Match(Marker.ACT_APPENDIX)
    if len(text) <= p.article_max and (m := p.article.match(text)):
        number = m.group("number").replace("_", ".")
        return Match(Marker.ARTICLE, number, m.group("title").strip())
    if len(text) <= p.appendix_max and (m := p.appendix.match(text)):
        return Match(Marker.APPENDIX, m.group("number"))
    if len(text) <= p.section_max and (m := p.section.match(text)):
        number = m.group("word") or m.group("roman")
        if number.isdigit() or roman_to_int(number) is not None:
            return Match(Marker.SECTION, number, m.group("title").strip())
    if len(text) <= p.chapter_max and (m := p.chapter.match(text)):
        return Match(Marker.CHAPTER, m.group("number"), m.group("title").strip())
    if len(text) <= p.paragraph_section_max and (m := p.paragraph_section.match(text)):
        return Match(Marker.PARAGRAPH_SECTION, m.group("number"), (m.group("title") or "").strip())
    if m := p.point.match(text):
        number, has_dot = m.group("number"), m.group("dot") is not None
        if has_dot or "." in number:
            return Match(Marker.POINT, number, has_dot=has_dot)
    if m := p.subpoint.match(text):
        return Match(Marker.SUBPOINT, m.group("number"))
    if m := p.letter.match(text):
        return Match(Marker.LETTER, m.group("number"))
    if p.list_item.match(text):
        return Match(Marker.LIST_ITEM)
    return Match(Marker.TEXT)


def opens_act_appendix(text: str, following: str, patterns: Patterns | None = None) -> bool:
    """«Приложение» alone, followed by «к приказу …»: the header of an approved document whose
    parts the layout put into paragraphs of their own."""
    p = patterns or load_patterns()
    return bool(p.bare_appendix.match(text.strip()) and p.act_appendix_tail.match(following))


def looks_like_heading(text: str, patterns: Patterns | None = None) -> bool:
    p = patterns or load_patterns()
    stripped = text.strip()
    if not stripped or len(stripped) > p.heading_max or stripped.endswith(_TERMINAL):
        return False
    first = stripped.lstrip('«"(')[:1]
    return first.isalpha() and first.isupper() and not set(stripped) <= set("_ .-")


def starts_lowercase(text: str) -> bool:
    first = text.lstrip('«"(')[:1]
    return first.isalpha() and first.islower()
