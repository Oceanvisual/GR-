"""One normalization for structure offsets and quote matching (R-11).

Both functions keep a map from every output character to its source offset, so a match found
in normalized text points back into the original document.
"""

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from typing import Literal

Chars = list[tuple[str, int]]  # (character, offset in the source text)

# Line-break hyphens of typeset PDFs (T072): soft hyphen, U+FFFE, STX; plus zero-width marks.
_INVISIBLE = frozenset("­￾\x02​‌‍⁠﻿")
_SPACES = frozenset("\t               　\x0b\x0c")
_QUOTES = frozenset("«»„“”‟\"‘’‚‛'`‹›")
_DASHES = frozenset("‐‑‒–—―−⁃")


@dataclass(frozen=True)
class Normalized:
    text: str
    source: tuple[int, ...]  # source[i] = offset in the original text of output char i

    def to_source(self, offset: int) -> int:
        """Original offset for an output offset; `len(text)` maps past the last source char."""
        if offset == len(self.text):
            return self.source[-1] + 1 if self.source else 0
        return self.source[offset]

    def from_source(self, offset: int) -> int:
        """First output offset at or after an original offset (sources are non-decreasing)."""
        return bisect_left(self.source, offset)

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Original [start, end) covering output [start, end)."""
        if start >= end:
            raise ValueError("empty span")
        return self.source[start], self.source[end - 1] + 1


def _clusters(text: str) -> Chars:
    """Base character with its combining marks, keyed by the base's offset."""
    out: Chars = []
    for index, char in enumerate(text):
        if out and unicodedata.combining(char) and out[-1][1] + len(out[-1][0]) == index:
            out[-1] = (out[-1][0] + char, out[-1][1])
        else:
            out.append((char, index))
    return out


def _expand(clusters: Chars, form: Literal["NFC", "NFKC"]) -> Chars:
    out: Chars = []
    for cluster, index in clusters:
        out.extend((char, index) for char in unicodedata.normalize(form, cluster))
    return out


def _collapse_spaces(chars: Chars, *, keep_newlines: bool) -> Chars:
    """Runs of whitespace → one space; with `keep_newlines`, line breaks survive (at most one
    blank line in a row) and spaces at line edges are dropped."""
    out: Chars = []
    pending_space: int | None = None
    newlines: list[int] = []
    for char, index in chars:
        if char == "\n" and keep_newlines:
            newlines.append(index)
            pending_space = None
            continue
        if char.isspace():
            if pending_space is None:
                pending_space = index
            continue
        if newlines:
            if out:
                out.extend(("\n", i) for i in newlines[:2])
            newlines = []
        elif pending_space is not None and out:
            out.append((" ", pending_space))
        pending_space = None
        out.append((char, index))
    return out


def _done(chars: Chars) -> Normalized:
    return Normalized(text="".join(c for c, _ in chars), source=tuple(i for _, i in chars))


def normalize_document_text(text: str) -> Normalized:
    """Text for structure offsets: NFC, unified spaces and line breaks, no invisible marks.

    Keeps letter case, quotes and dashes as they are; paragraph breaks survive.
    """
    chars: Chars = []
    for char, index in _expand(_clusters(text), "NFC"):
        if char == "\r":
            # \r\n → \n; a lone \r is a line break too.
            nxt = text[index + 1 : index + 2]
            if nxt != "\n":
                chars.append(("\n", index))
            continue
        if char in _INVISIBLE:
            continue
        if char in _SPACES:
            chars.append((" ", index))
            continue
        if unicodedata.category(char) == "Cc" and char != "\n":
            continue
        chars.append((char, index))
    return _done(_collapse_spaces(chars, keep_newlines=True))


_WORD_BREAK_RE = re.compile(r"(?<=\w)[-\u00ad\ufffe\x02][ \t]*\n\s*(?=\w)")


def normalize_for_match_mapped(text: str) -> Normalized:
    """Comparison form (R-11): NFKC, lower case, ё→е, one kind of quote and dash, hyphenated
    line breaks joined, whitespace collapsed. `№` becomes `n` so it equals `N` (NFKC alone
    would turn it into `no`)."""
    # Words split at a line break: "сло-\nво" → "слово", "210-\nФЗ" → "210-ФЗ"; a typeset
    # hyphen marker (soft hyphen, U+FFFE, STX) always joins without a hyphen.
    chars: Chars = []
    position = 0
    for match in _WORD_BREAK_RE.finditer(text):
        chars.extend((char, i) for i, char in enumerate(text[position : match.start()], position))
        before = text[match.start() - 1]
        after = text[match.end()]
        is_dash = text[match.start()] == "-"
        if is_dash and not (before.isalpha() and after.islower()):
            chars.append(("-", match.start()))  # compound word or number: keep the hyphen
        position = match.end()
    chars.extend((char, i) for i, char in enumerate(text[position:], position))

    prepared: Chars = []
    for char, index in chars:
        if char in _INVISIBLE:
            continue
        prepared.append(("N" if char == "№" else char, index))

    out: Chars = []
    for cluster, index in _regroup(prepared):
        for char in unicodedata.normalize("NFKC", cluster).lower():
            if char == "ё":
                char = "е"
            elif char in _QUOTES:
                char = '"'
            elif char in _DASHES:
                char = "-"
            elif char in _INVISIBLE:
                continue
            out.append((char, index))
    return _done(_collapse_spaces(out, keep_newlines=False))


def _regroup(chars: Chars) -> Chars:
    """Re-attach combining marks to their base after filtering."""
    out: Chars = []
    for char, index in chars:
        if out and unicodedata.combining(char):
            out[-1] = (out[-1][0] + char, out[-1][1])
        else:
            out.append((char, index))
    return out


def normalize_for_match(text: str) -> str:
    return normalize_for_match_mapped(text).text
