"""Seeded mutations of parsed documents and raw files for the invariant tests (R-22, SC-014).

Block mutations work on the intermediate representation, so one set covers every format;
byte mutations break the files themselves. Everything is driven by `random.Random(seed)`:
a failing case is reproduced by its seed alone.
"""

import random
import re
from collections.abc import Callable
from pathlib import Path

from gr.domain.document import Block, RawDocument, TableCell

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_NUMBER_RE = re.compile(r"^\s*((?:\d+\.)*\d+[.)]|[а-яa-z]\)|[IVXLC]+\.|Статья\s+\d+\.?)\s*")
_WORDS = [
    "заявитель",
    "заявление",
    "специалист",
    "орган",
    "срок",
    "рабочих",
    "дней",
    "документы",
    "регистрация",
    "решение",
    "отказ",
    "уведомление",
    "пункт",
    "статья",
    "раздел",
    "подпункт",
    "в",
    "течение",
    "не",
    "позднее",
]
# Odd but valid text: combining marks, invisible and bidirectional marks, other scripts,
# line separators, digits of other scripts, control characters, private use, emoji.
_ODD_CHARS = "́̈​‍⁠﻿­‎‏‮  \x00\x07\x0b\x0c\x1b\x7f\x85　�￾№§«»—–…ёЁ٠١१١٢三四😀👍🏽𝔸𝟙ﬁ²½Ⅻ"

BlockMutation = Callable[[random.Random, list[Block]], list[Block]]


def _numbered(blocks: list[Block]) -> list[int]:
    return [i for i, block in enumerate(blocks) if _NUMBER_RE.match(block.text)]


def _with_text(block: Block, text: str) -> Block:
    return block.model_copy(update={"text": text})


def shuffle_numbering(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Numbers stay, but move to other paragraphs."""
    indexes = _numbered(blocks)
    prefixes = [_NUMBER_RE.match(blocks[i].text).group(0) for i in indexes]  # type: ignore[union-attr]
    rng.shuffle(prefixes)
    out = list(blocks)
    for i, prefix in zip(indexes, prefixes, strict=True):
        out[i] = _with_text(out[i], prefix + _NUMBER_RE.sub("", out[i].text, count=1))
    return out


def break_numbering(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Gaps, repeats, reversed and absurd numbers in place of some of the real ones."""
    out = list(blocks)
    for i in _numbered(blocks):
        if rng.random() < 0.3:
            number = rng.choice(["1.", "1.1.", "99.", "0.", "2.2.2.2.2.2.2.", "3)", "я)", "MMXX."])
            out[i] = _with_text(out[i], f"{number} {_NUMBER_RE.sub('', out[i].text, count=1)}")
    return out


def drop_headings(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Remove most short unnumbered lines, which are where headings live."""
    return [
        block
        for block in blocks
        if len(block.text) > 120 or _NUMBER_RE.match(block.text) or rng.random() < 0.3
    ]


def empty_pages(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Blank blocks and page numbers that skip several pages."""
    out: list[Block] = []
    shift = 0
    for block in blocks:
        if rng.random() < 0.05:
            out.append(Block(text=rng.choice(["", " ", "\n\n", "\t"]), page=block.page))
        if block.page is not None and rng.random() < 0.03:
            shift += rng.randint(1, 4)
        page = block.page + shift if block.page is not None else None
        out.append(block.model_copy(update={"page": page}))
    return out


def giant_paragraph(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """One paragraph of 200 thousand characters, with numbering-like tokens inside."""
    words: list[str] = []
    size = 0
    while size < 200_000:
        word = rng.choice([*_WORDS, "1.1.", "ст.", "N", "5)", "-"])
        words.append(word)
        size += len(word) + 1
    position = rng.randrange(len(blocks) + 1)
    return [*blocks[:position], Block(text=" ".join(words)), *blocks[position:]]


def random_unicode(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Odd characters sprinkled into text, including at the very start and end of blocks."""
    out = list(blocks)
    for _ in range(max(1, len(blocks) // 5)):
        i = rng.randrange(len(out))
        text = out[i].text
        position = rng.choice([0, len(text), rng.randint(0, len(text))])
        junk = "".join(rng.choice(_ODD_CHARS) for _ in range(rng.randint(1, 6)))
        out[i] = _with_text(out[i], text[:position] + junk + text[position:])
    return out


def junk_blocks(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Blocks of only punctuation, only numbers or only invisible characters."""
    out = list(blocks)
    for _ in range(rng.randint(1, 8)):
        junk = rng.choice(["1.", "...", "—", "|", "1.1.1.1.1.1.1.1.", "​​", "﻿", "*"])
        out.insert(rng.randrange(len(out) + 1), Block(text=junk))
    return out


def split_and_merge(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Paragraphs broken at random places and neighbours glued together."""
    out: list[Block] = []
    for block in blocks:
        if out and rng.random() < 0.1:
            out[-1] = _with_text(out[-1], out[-1].text + rng.choice([" ", "\n", ""]) + block.text)
        elif len(block.text) > 10 and rng.random() < 0.1:
            cut = rng.randrange(1, len(block.text))
            out += [_with_text(block, block.text[:cut]), _with_text(block, block.text[cut:])]
        else:
            out.append(block)
    return out


def duplicate_section(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """A run of blocks repeated: the same numbers appear twice."""
    start = rng.randrange(len(blocks))
    end = min(len(blocks), start + rng.randint(1, 40))
    return [*blocks[:end], *blocks[start:end], *blocks[end:]]


def restyle(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Random heading styles, sizes, bold, alignment, list levels and table cells."""
    out = []
    for block in blocks:
        if rng.random() < 0.2:
            block = block.model_copy(
                update={
                    "bold": rng.choice([True, False, None]),
                    "font_size": rng.choice([None, 6.0, 12.0, 30.0]),
                    "align": rng.choice([None, "center", "left", "right"]),
                    "style": rng.choice([None, "Heading 1", "Заголовок 3", "Title", "List"]),
                    "list_level": rng.choice([None, 0, 1, 7]),
                    "table": rng.choice(
                        [None, TableCell(table=0, row=rng.randint(0, 2), col=rng.randint(0, 2))]
                    ),
                }
            )
        out.append(block)
    return out


def reverse_order(rng: random.Random, blocks: list[Block]) -> list[Block]:
    """Paragraphs in reverse: every numbering transition goes backwards."""
    return list(reversed(blocks))


BLOCK_MUTATIONS: dict[str, BlockMutation] = {
    "shuffle_numbering": shuffle_numbering,
    "break_numbering": break_numbering,
    "drop_headings": drop_headings,
    "empty_pages": empty_pages,
    "giant_paragraph": giant_paragraph,
    "random_unicode": random_unicode,
    "junk_blocks": junk_blocks,
    "split_and_merge": split_and_merge,
    "duplicate_section": duplicate_section,
    "restyle": restyle,
    "reverse_order": reverse_order,
}
HEAVY = {"giant_paragraph"}  # used on a fixed share of seeds only, to keep the suite fast


def mutate_blocks(raw: RawDocument, seed: int) -> tuple[RawDocument, list[str]]:
    """One to three mutations chosen by the seed; returns the new document and their names."""
    rng = random.Random(seed)
    light = sorted(set(BLOCK_MUTATIONS) - HEAVY)
    names = rng.sample(light, rng.randint(1, 3))
    if seed % 25 == 0:
        names.append("giant_paragraph")
    blocks = list(raw.blocks)
    for name in names:
        if blocks:
            blocks = BLOCK_MUTATIONS[name](rng, blocks)
    pages = [block.page for block in blocks if block.page is not None]
    page_count = max([raw.page_count or 0, *pages]) if raw.page_count is not None else None
    return raw.model_copy(update={"blocks": blocks, "page_count": page_count}), names


# --- byte mutations ------------------------------------------------------------------------


def mutate_bytes(data: bytes, seed: int) -> tuple[bytes, str]:
    """A damaged copy of a file: truncated, flipped, spliced or padded."""
    rng = random.Random(seed)
    kind = rng.choice(["truncate", "flip", "splice", "zero_run", "prefix", "duplicate"])
    buffer = bytearray(data)
    if kind == "truncate":
        buffer = buffer[: rng.randrange(1, len(buffer))]
    elif kind == "flip":
        for _ in range(rng.randint(1, 50)):
            buffer[rng.randrange(len(buffer))] = rng.randrange(256)
    elif kind == "splice":
        start = rng.randrange(len(buffer))
        buffer[start : start + rng.randint(1, 4096)] = rng.randbytes(rng.randint(0, 4096))
    elif kind == "zero_run":
        start = rng.randrange(len(buffer))
        buffer[start : start + 2048] = bytes(min(2048, len(buffer) - start))
    elif kind == "prefix":
        buffer = bytearray(rng.randbytes(rng.randint(1, 64))) + buffer
    else:
        start = rng.randrange(len(buffer))
        buffer += buffer[start : start + 8192]
    return bytes(buffer), kind


def mutate_text_bytes(text: str, seed: int) -> tuple[bytes, str]:
    """A text file in an unusual encoding, line-ending style or with stray bytes."""
    rng = random.Random(seed)
    kind = rng.choice(["cp1251", "utf-16", "utf-8-sig", "crlf", "cr", "stray", "nul", "mixed"])
    if kind == "cp1251":
        return text.encode("cp1251", errors="replace"), kind
    if kind == "utf-16":
        return text.encode("utf-16"), kind
    if kind == "utf-8-sig":
        return text.encode("utf-8-sig"), kind
    if kind == "crlf":
        return text.replace("\n", "\r\n").encode(), kind
    if kind == "cr":
        return text.replace("\n", "\r").encode(), kind
    data = bytearray(text.encode())
    if kind == "stray":
        for _ in range(rng.randint(1, 20)):
            data.insert(rng.randrange(len(data)), rng.randrange(0x80, 0x100))
    elif kind == "nul":
        for _ in range(rng.randint(1, 20)):
            data.insert(rng.randrange(len(data)), 0)
    else:
        half = len(text) // 2
        data = bytearray(text[:half].encode() + text[half:].encode("cp1251", errors="replace"))
    return bytes(data), kind
