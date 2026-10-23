"""Sentences that address an AI model instead of describing a procedure (R-12, T035).

Document text is data, and the model is told so, but an obedient model may still quote an
injected instruction verbatim: the quote is in the text and would pass verification. Spans
found here are therefore excluded from evidence. Detection is by sentence: a sentence with
a second-person imperative or a prompt marker is suspicious as a whole.
"""

import re
import tomllib
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache
from importlib import resources

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*", re.MULTILINE)


@dataclass(frozen=True)
class _Patterns:
    imperative: re.Pattern[str]
    marker: re.Pattern[str]


@cache
def _patterns() -> _Patterns:
    raw = tomllib.loads(resources.files("gr.config").joinpath("injection.toml").read_text("utf-8"))
    words = sorted(raw["imperatives"], key=len, reverse=True)
    imperative = r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w-])"
    return _Patterns(
        imperative=re.compile(imperative),
        marker=re.compile("|".join(f"(?:{m})" for m in raw["markers"]), re.MULTILINE),
    )


def instruction_spans(text: str) -> list[tuple[int, int]]:
    """[start, end) of every sentence of `text` that looks like an instruction to a model."""
    patterns = _patterns()
    lowered = text.lower().replace("ё", "е")
    spans = []
    for sentence in _SENTENCE_RE.finditer(lowered):
        chunk = sentence.group(0)
        if patterns.imperative.search(chunk) or patterns.marker.search(chunk):
            spans.append((sentence.start(), sentence.end()))
    return spans


class InstructionIndex:
    """Fast overlap checks against the instruction-like spans of one text."""

    def __init__(self, text: str) -> None:
        self.spans = instruction_spans(text)
        self._starts = [start for start, _ in self.spans]

    def overlaps(self, start: int, end: int) -> bool:
        """Spans are sorted and disjoint: only the last one starting before `end` can reach
        past `start`."""
        i = bisect_right(self._starts, end - 1) - 1
        return i >= 0 and self.spans[i][1] > start
