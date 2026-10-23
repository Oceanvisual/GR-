"""What is not the text of the act, taken out before the structure is built (FR-014, R-20, T067).

- Service lines of reference systems (a banner, «Документ предоставлен…», «Дата сохранения…»)
  are dropped.
- Generated contents entries with Word TOC styles, and their immediately preceding heading,
  are dropped before their duplicate numbers can become structural units.
- A list of changing documents leaves the text and becomes a note of where it stands: the act
  or the approved document.
- An editorial note — «(в ред. …)», «(п. 2.6 введён …)», «(Абзац в редакции, введенной …)», a
  block of its own or the end of one after the sentence — leaves the text and becomes a note of
  the unit it is about: the one before it (the unit the note names, if it names one), or the
  next one for notes that stand before a provision («КонсультантПлюс: примечание»).
- A unit whose own text says it lost force, or that a note after it says lost force, is marked
  repealed: it stays visible and is never analysed.

Running headers and page numbers of a PDF are dropped by the PDF adapter (pdf_layout.py), which
still sees the page geometry. Quotes are checked against the cleaned text and the model reads
the same text, so a date of an amending act never becomes a «fact» of a process. The patterns
are in config/noise_patterns.toml.
"""

import re
import tomllib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

from gr.domain.document import Block, Fragment
from gr.domain.enums import FragmentKind
from gr.domain.ir import IRText

MAX_NOTE_BLOCKS = 4  # a note split by the layout into lines: at most this many blocks
_FLAGS = re.I
_PUNCTUATION_RE = re.compile(r"[\s\W_]+")
_NUMBER_RE = re.compile(
    r"^\s*(?:(?:статья|глава|раздел)\s+[\dIVXLC]+(?:\.\d+)*\.?|[\dIVXLC]+(?:\.\d+)*[.)]|[а-яa-z]\))\s*",
    _FLAGS,
)
_NAMED_RE = re.compile(
    r"^\(\s*(?:п|пп|подп|ч|ст|пункт|подпункт|часть|статья)\.?\s+(?P<number>\d+(?:\.\d+)*)\b", _FLAGS
)
_UNIT_RE = re.compile(r"^\(\s*(?P<unit>\w+)", _FLAGS)
# «(Статья в редакции …)» after its last part is about the article, not the part.
_UNIT_KINDS = {
    "статья": FragmentKind.ARTICLE,
    "часть": FragmentKind.PART,
    "пункт": FragmentKind.POINT,
    "подпункт": FragmentKind.SUBPOINT,
    "раздел": FragmentKind.SECTION,
    "глава": FragmentKind.CHAPTER,
}


@dataclass(frozen=True)
class NoiseConfig:
    service: tuple[re.Pattern[str], ...]
    contents_style: re.Pattern[str]
    contents_heading: re.Pattern[str]
    note: re.Pattern[str]
    before: re.Pattern[str]
    before_end: re.Pattern[str]  # a heading of such a note left at the end of a paragraph
    repeals: re.Pattern[str]
    change_single: re.Pattern[str]
    change_start: re.Pattern[str]
    change_entry: re.Pattern[str]
    separator: re.Pattern[str]
    repealed: re.Pattern[str]


@cache
def load_noise() -> NoiseConfig:
    raw = tomllib.loads(
        resources.files("gr.config").joinpath("noise_patterns.toml").read_text("utf-8")
    )
    notes, changes = raw["notes"], raw["change_list"]
    return NoiseConfig(
        service=tuple(re.compile(p, _FLAGS) for p in raw["service"]["phrases"]),
        contents_style=re.compile(raw["contents"]["style"], _FLAGS),
        contents_heading=re.compile(raw["contents"]["heading"], _FLAGS),
        note=re.compile(rf"^\(\s*{notes['unit']}?{notes['event']}", _FLAGS),
        before=re.compile("^(?:" + "|".join(notes["before"]) + ")", _FLAGS),
        before_end=re.compile(r"\s*(?:" + "|".join(notes["before"]) + r")\.?\s*$", _FLAGS),
        repeals=re.compile(notes["repeals"], _FLAGS),
        change_single=re.compile(changes["single"], _FLAGS),
        change_start=re.compile(changes["start"], _FLAGS),
        change_entry=re.compile(changes["entry"], _FLAGS),
        separator=re.compile(changes["separator"], _FLAGS),
        repealed=re.compile(raw["repealed"]["own_text"], _FLAGS),
    )


@dataclass(frozen=True)
class Note:
    text: str
    block: int | None  # index in the cleaned blocks of the unit it is about; None: the act
    repeals: bool = False  # it says that unit lost force


@dataclass(frozen=True)
class Cleaned:
    blocks: list[Block]
    notes: list[Note] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # removed service and contents lines
    editorial_notes: int = 0
    contents_lines: int = 0

    @property
    def spps_lines(self) -> int:
        return len(self.dropped) - self.contents_lines


def _depth(text: str) -> int:
    return text.count("(") - text.count(")")


def _service_only(text: str, config: NoiseConfig) -> bool:
    rest = text
    for pattern in config.service:
        rest = pattern.sub(" ", rest)
    return rest != text and not _PUNCTUATION_RE.sub("", rest)


def _contents_entry(block: Block, config: NoiseConfig) -> bool:
    return block.table is None and bool(
        config.contents_style.fullmatch((block.style or "").strip())
    )


def _contents_heading(blocks: Sequence[Block], index: int, config: NoiseConfig) -> bool:
    block = blocks[index]
    if block.table is not None or not config.contents_heading.fullmatch(block.text.strip()):
        return False
    following = next((b for b in blocks[index + 1 :] if b.text.strip()), None)
    return following is not None and _contents_entry(following, config)


def split_trailing(text: str, config: NoiseConfig | None = None) -> tuple[str, str | None]:
    """«…дней. (в ред. …)» → («…дней.», «(в ред. …)»). Only after the end of a sentence: a
    parenthesis inside one («…приказом (в редакции …).») is the act's own reference."""
    config = config or load_noise()
    stripped = text.rstrip()
    end = stripped.rstrip(".;")
    if not end.endswith(")"):
        return text, None
    depth = 0
    for start in range(len(end) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(end[start], 0)
        if depth == 0:
            break
    else:
        return text, None
    note, body = end[start:], end[:start].rstrip()
    if not body or body[-1] not in ".;:" or not config.note.match(note):
        return text, None
    return body, note


def clean(blocks: Sequence[Block], config: NoiseConfig | None = None) -> Cleaned:
    config = config or load_noise()
    kept: list[Block] = []
    last: int | None = None  # the last kept block with text: what a note after it is about
    notes: list[Note] = []
    waiting: list[str] = []  # notes that stand before the next provision
    dropped: list[str] = []
    editorial = 0
    contents = 0

    def absorb(start: int) -> tuple[int, str]:
        """A note from `start`, with the following blocks while its parenthesis is open."""
        parts = [blocks[start].text.strip()]
        end = start + 1
        while _depth(" ".join(parts)) > 0 and end < len(blocks) and end - start < MAX_NOTE_BLOCKS:
            parts.append(blocks[end].text.strip())
            end += 1
        return end, " ".join(p for p in parts if p)

    def change_list(start: int) -> tuple[int, str]:
        """Kodeks: a heading, separators, «Документ с изменениями, внесенными:» and entries."""
        parts, end, entries = [blocks[start].text.strip()], start + 1, 0
        while end < len(blocks):
            text = blocks[end].text.strip()
            if config.separator.match(text):
                end += 1
                if entries:  # the line that closes the list
                    break
            elif config.change_start.match(text) or config.change_entry.match(text):
                entries += bool(config.change_entry.match(text))
                parts.append(text)
                end += 1
            elif not text:
                end += 1
            else:
                break
        return end, " ".join(parts)

    def previous() -> int | None:
        return last

    i = 0
    while i < len(blocks):
        block = blocks[i]
        text = block.text.strip()
        if _contents_entry(block, config) or _contents_heading(blocks, i, config):
            dropped.append(text)
            contents += 1
            i += 1
            continue
        if text and _service_only(text, config):
            dropped.append(text)
            i += 1
            continue
        if config.change_single.match(text):
            i, found = absorb(i)
            notes.append(Note(found, previous()))
            editorial += 1
            continue
        if config.change_start.match(text):
            i, found = change_list(i)
            notes.append(Note(found, previous()))
            editorial += 1
            continue
        if config.before.match(text):
            i, found = absorb(i)
            if config.before_end.fullmatch(found) and i < len(blocks):  # the heading alone:
                i, rest = absorb(i)  # its text is the next block
                found = f"{found} {rest}"
            waiting.append(found)
            editorial += 1
            continue
        if config.note.match(text):
            i, found = absorb(i)
            notes.append(Note(found, previous(), repeals=bool(config.repeals.match(found))))
            editorial += 1
            continue
        heading = config.before_end.search(text)
        if heading is not None and heading.start() > 0 and i + 1 < len(blocks):
            # The layout glued a note's heading to the paragraph above; its text comes next.
            kept.append(block.model_copy(update={"text": text[: heading.start()]}))
            last = len(kept) - 1
            i, rest = absorb(i + 1)
            waiting.append(f"{heading.group(0).strip()} {rest}")
            editorial += 1
            continue
        body, trailing = split_trailing(block.text, config)
        kept.append(block if trailing is None else block.model_copy(update={"text": body}))
        if trailing is not None:
            notes.append(Note(trailing, len(kept) - 1))
            editorial += 1
        if text:
            last = len(kept) - 1
            notes += [Note(found, last) for found in waiting]
            waiting.clear()
        i += 1
    notes += [Note(found, previous()) for found in waiting]  # nothing came after them
    return Cleaned(kept, notes, dropped, editorial, contents)


def _number(value: str | None) -> str:
    return (value or "").strip().rstrip(".)")


def _about(deepest: Fragment, note: str, by_id: dict[str, Fragment]) -> str:
    """The unit a note after `deepest` is about: the one it names by number («(п. 2.6 в ред.
    …)»), the nearest of the kind it names («(Статья в редакции …)»), or `deepest` itself."""
    named = _NAMED_RE.match(note)
    unit = _UNIT_RE.match(note)
    kind = _UNIT_KINDS.get(unit["unit"].lower()) if unit else None
    current: Fragment | None = deepest
    while current is not None:
        if named and _number(current.number) == named["number"]:
            return current.id
        if not named and kind is not None and current.kind == kind:
            return current.id
        current = by_id.get(current.parent_id or "")
    return deepest.id


def place_notes(
    fragments: Sequence[Fragment],
    ir: IRText,
    notes: Sequence[Note],
    config: NoiseConfig | None = None,
) -> list[Fragment]:
    """The notes on the fragments they are about; repealed units marked."""
    config = config or load_noise()
    starts = {span.block: span.start for span in ir.spans}
    by_id = {f.id: f for f in fragments}
    smallest_first = sorted(fragments, key=lambda f: (f.end - f.start, -f.order))
    root = next(f.id for f in fragments if f.parent_id is None)
    found: dict[str, list[str]] = defaultdict(list)
    repealed: set[str] = set()
    for note in notes:
        target = root
        offset = starts.get(note.block) if note.block is not None else None
        if offset is not None:
            target = _about(next(f for f in smallest_first if f.start <= offset < f.end),
                            note.text, by_id)  # fmt: skip
        found[target].append(note.text)
        if note.repeals:
            repealed.add(target)
    for fragment in fragments:
        own = _NUMBER_RE.sub("", fragment.text, count=1)
        if fragment.kind != FragmentKind.ROOT and config.repealed.match(own):
            repealed.add(fragment.id)
    return [
        f.model_copy(
            update={"notes": [*f.notes, *found[f.id]], "repealed": f.repealed or f.id in repealed}
        )
        if f.id in found or f.id in repealed
        else f
        for f in fragments
    ]
