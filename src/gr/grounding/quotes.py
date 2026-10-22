"""Deterministic quote verification (R-11, FR-025): is the quote in the cited fragment?

The cited fragment is checked with its subtree. A quote not found there but found elsewhere
with the same threshold is `relocated`, and the citation moves to the smallest fragment that
contains the match. Only `verified` and `relocated` pass.

The full text is rebuilt from the fragments' own texts (R-22 invariant), so offsets in the
results are offsets in the stored document text.
"""

import tomllib
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from rapidfuzz import fuzz

from gr.domain.document import Fragment, GroundingResult
from gr.domain.enums import GroundingStatus
from gr.domain.ir import BLOCK_SEPARATOR
from gr.grounding.normalize import Normalized, normalize_for_match, normalize_for_match_mapped

_ELLIPSIS = "..."  # NFKC turns "…" into three dots


@dataclass(frozen=True)
class QuoteConfig:
    min_chars: int = 12
    max_chars: int = 600
    fuzzy_ok: float = 92
    fuzzy_partial: float = 80
    min_part_chars: int = 4
    ngram: int = 3
    rare_ngrams: int = 32
    bucket_chars: int = 256
    candidates: int = 6
    direct_max_chars: int = 6000


@cache
def load_config() -> QuoteConfig:
    raw = tomllib.loads(resources.files("gr.config").joinpath("grounding.toml").read_text("utf-8"))
    return QuoteConfig(**raw["quote"], **raw["index"])


@dataclass(frozen=True)
class QuoteMatch:
    """Where the quote really is: `fragment_id` differs from the cited one when relocated."""

    fragment_id: str
    grounding: GroundingResult


@dataclass(frozen=True)
class _Hit:
    score: float
    start: int  # normalised offsets
    end: int


class QuoteIndex:
    """One document prepared for many quotes: normalised text, fragment spans, n-gram index."""

    def __init__(self, fragments: Sequence[Fragment], config: QuoteConfig | None = None) -> None:
        self.config = config or load_config()
        self.fragments = {f.id: f for f in fragments}
        self._by_size = sorted(fragments, key=lambda f: (f.end - f.start, -f.order))
        text = BLOCK_SEPARATOR.join(
            f.text for f in sorted(fragments, key=lambda f: f.order) if f.text
        )
        self.text = text
        self.norm: Normalized = normalize_for_match_mapped(text)
        self._ngrams: dict[str, list[int]] = {}
        n = self.config.ngram
        for i in range(len(self.norm.text) - n + 1):
            self._ngrams.setdefault(self.norm.text[i : i + n], []).append(i)

    # --- public --------------------------------------------------------------------------

    def verify(self, quote: str, fragment_id: str) -> QuoteMatch:
        cfg = self.config
        normalized = normalize_for_match(quote)
        parts = self._parts(normalized)
        length = sum(len(p) for p in parts)
        if not parts or length < cfg.min_chars or len(normalized) > cfg.max_chars:
            too_long = len(normalized) > cfg.max_chars
            reason = "цитата слишком длинная" if too_long else "цитата слишком короткая"
            return self._result(fragment_id, GroundingStatus.INVALID_QUOTE, 0, None, reason=reason)

        cited = self.fragments.get(fragment_id)
        scope = self._norm_span(cited) if cited is not None else None
        in_scope: _Hit | None = None
        if scope is not None:
            in_scope = self._search(parts, *scope)
            if in_scope.score >= cfg.fuzzy_ok:
                return self._result(fragment_id, GroundingStatus.VERIFIED, in_scope.score, in_scope)

        anywhere = self._search(parts, 0, len(self.norm.text))
        if anywhere.score >= cfg.fuzzy_ok:
            found = self._smallest_containing(anywhere)
            return self._result(
                found.id,
                GroundingStatus.RELOCATED,
                anywhere.score,
                anywhere,
                original=fragment_id,
                reason="ссылка исправлена",
            )
        if in_scope is not None and in_scope.score >= cfg.fuzzy_partial:
            return self._result(
                fragment_id,
                GroundingStatus.PARTIAL,
                in_scope.score,
                in_scope,
                reason="близкое совпадение, но не дословное",
            )
        reason = "фрагмент не найден" if cited is None else "цитата не найдена в тексте"
        score = in_scope.score if in_scope is not None else anywhere.score
        return self._result(fragment_id, GroundingStatus.UNVERIFIED, score, None, reason=reason)

    # --- search ----------------------------------------------------------------------------

    def _parts(self, normalized: str) -> list[str]:
        """An elided quote is several parts that must appear in order."""
        pieces = [p.strip(" .,;:") for p in normalized.split(_ELLIPSIS)]
        return [p for p in pieces if len(p) >= self.config.min_part_chars]

    def _search(self, parts: list[str], lo: int, hi: int) -> _Hit:
        exact = self._exact(parts, lo, hi)
        if exact is not None:
            return exact
        hits: list[_Hit] = []
        cursor = lo
        for part in parts:
            hit = self._fuzzy(part, cursor, hi)
            if hit is None:
                return _Hit(0, lo, lo)
            hits.append(hit)
            cursor = hit.end
        return _Hit(min(h.score for h in hits), hits[0].start, hits[-1].end)

    def _exact(self, parts: list[str], lo: int, hi: int) -> _Hit | None:
        text = self.norm.text
        start = text.find(parts[0], lo, hi)
        while start != -1:
            cursor = start + len(parts[0])
            for part in parts[1:]:
                found = text.find(part, cursor, hi)
                if found == -1:
                    break
                cursor = found + len(part)
            else:
                return _Hit(100.0, start, cursor)
            start = text.find(parts[0], start + 1, hi)
        return None

    def _fuzzy(self, part: str, lo: int, hi: int) -> _Hit | None:
        if hi - lo <= self.config.direct_max_chars:
            return self._align(part, lo, hi)
        best: _Hit | None = None
        for start, end in self._candidates(part, lo, hi):
            hit = self._align(part, start, end)
            if hit is not None and (best is None or hit.score > best.score):
                best = hit
        return best

    def _align(self, part: str, lo: int, hi: int) -> _Hit | None:
        if lo >= hi:
            return None
        alignment = fuzz.partial_ratio_alignment(part, self.norm.text[lo:hi])
        if alignment is None or alignment.dest_end <= alignment.dest_start:
            return None
        return _Hit(alignment.score, lo + alignment.dest_start, lo + alignment.dest_end)

    def _candidates(self, part: str, lo: int, hi: int) -> list[tuple[int, int]]:
        """Windows where the quote's rarest n-grams cluster, inside [lo, hi)."""
        cfg = self.config
        grams = {part[i : i + cfg.ngram] for i in range(len(part) - cfg.ngram + 1)}
        known = sorted((g for g in grams if g in self._ngrams), key=lambda g: len(self._ngrams[g]))
        votes: Counter[int] = Counter()
        for gram in known[: cfg.rare_ngrams]:
            positions = self._ngrams[gram]
            for position in positions[bisect_left(positions, lo) : bisect_right(positions, hi)]:
                votes[position // cfg.bucket_chars] += 1
        windows: list[tuple[int, int]] = []
        for bucket, _ in votes.most_common(cfg.candidates):
            start = max(lo, bucket * cfg.bucket_chars - len(part))
            end = min(hi, (bucket + 1) * cfg.bucket_chars + len(part))
            windows.append((start, end))
        return windows

    # --- offsets and fragments -------------------------------------------------------------

    def _norm_span(self, fragment: Fragment) -> tuple[int, int]:
        return self.norm.from_source(fragment.start), self.norm.from_source(fragment.end)

    def _source_span(self, hit: _Hit) -> tuple[int, int]:
        return self.norm.source_span(hit.start, hit.end)

    def _smallest_containing(self, hit: _Hit) -> Fragment:
        start, end = self._source_span(hit)
        return next(f for f in self._by_size if f.start <= start and end <= f.end)

    def _result(
        self,
        fragment_id: str,
        status: GroundingStatus,
        score: float,
        hit: _Hit | None,
        *,
        original: str | None = None,
        reason: str | None = None,
    ) -> QuoteMatch:
        span = self._source_span(hit) if hit is not None and hit.end > hit.start else (None, None)
        return QuoteMatch(
            fragment_id=fragment_id,
            grounding=GroundingResult(
                status=status,
                score=round(score, 1),
                matched_start=span[0],
                matched_end=span[1],
                original_fragment_id=original,
                reason=reason,
            ),
        )


def verify_quote(quote: str, fragment_id: str, fragments: Sequence[Fragment]) -> QuoteMatch:
    """One-off check; for many quotes of one document build a `QuoteIndex` once."""
    return QuoteIndex(fragments).verify(quote, fragment_id)
