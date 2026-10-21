"""PDF lines → paragraphs (T072 findings). Pure Python over line geometry, no PDFium here.

Lines come from PDFium's own text reconstruction; this module drops running headers and page
numbers, re-attaches line pieces split by superscripts, and decides where a paragraph starts:
first-line indent, a list marker after a finished sentence, a large vertical gap, a centred
heading, or a short line that ends a sentence.
"""

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import pairwise
from statistics import median

from gr.domain.document import Block

HEADER_BAND = 0.15  # top and bottom share of the page searched for running headers
REPEAT_SHARE = 0.5  # a masked line repeated on this share of same-parity pages is a header
MIN_PAGES_FOR_HEADERS = 3
SYLLABLE_HYPHENS_PER_PAGE = 2.0  # typeset book: ~8.5 per page; Word-made PDF: ~0.02 (T072)

_DIGITS_RE = re.compile(r"\d+")
_PAGE_NUMBER_RE = re.compile(
    r"^(?:[-–—]?\s*#\s*[-–—]?|стр(?:аница)?\.?\s*#(?:\s*из\s*#)?|#\s*/\s*#)$", re.I
)
_LIST_START_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)](?:\s|$)|[а-яa-z]\)\s|[IVXLC]+\.\s|[-–—•]\s|(?:Статья|Глава|Раздел)\s+\d)"
)
_SENTENCE_END_RE = re.compile(r"[.;:!?]['\"»)]*$")
_DASH_ITEM_RE = re.compile(r"^[-–—•]\s")
# Legal abbreviations end with a dot without ending the sentence: "ст." + "5135) ..." is one line.
_ABBREVIATION_END_RE = re.compile(
    r"(?:^|[\s(])(?:ст|стст|п|пп|ч|абз|подп|разд|гл|прил|г|гг|N|№|см|т|тыс|млн|руб|коп|д|ул|рег)\.$",
    re.I,
)
_HYPHEN_MARKS = ("￾", "\x02", "­")


@dataclass(frozen=True)
class Line:
    text: str
    page: int  # 1-based
    x0: float
    x1: float
    top: float  # PDF points, origin at the bottom: larger is higher on the page
    bottom: float
    size: float
    page_width: float
    page_height: float


@dataclass
class LayoutResult:
    blocks: list[Block]
    header_footer_lines: int = 0
    columns_pages: list[int] = field(default_factory=list)


def _mask(text: str) -> str:
    return _DIGITS_RE.sub("#", " ".join(text.split())).strip()


def _in_band(line: Line) -> bool:
    return (
        line.top >= line.page_height * (1 - HEADER_BAND)
        or line.bottom <= line.page_height * HEADER_BAND
    )


def strip_running_headers(pages: list[list[Line]]) -> tuple[list[list[Line]], int]:
    """Drop lines in the top/bottom bands that repeat across pages of the same parity, and
    lone page numbers there."""
    if len(pages) < MIN_PAGES_FOR_HEADERS:
        return pages, 0

    def repeated_in(group: list[list[Line]]) -> set[str]:
        counts: Counter[str] = Counter()
        for lines in group:
            counts.update({_mask(line.text) for line in lines if _in_band(line)})
        threshold = max(2, REPEAT_SHARE * len(group))
        return {text for text, n in counts.items() if n >= threshold}

    # Books alternate headers on even and odd pages; short documents have too few pages of one
    # parity, so a line counts as a header if it repeats across all pages or within its parity.
    everywhere = repeated_in(pages)
    repeated = {
        parity: everywhere
        | repeated_in([lines for i, lines in enumerate(pages) if i % 2 == parity])
        for parity in (0, 1)
    }
    kept_pages: list[list[Line]] = []
    removed = 0
    for i, lines in enumerate(pages):
        kept = []
        for line in lines:
            masked = _mask(line.text)
            if _in_band(line) and (masked in repeated[i % 2] or _PAGE_NUMBER_RE.match(masked)):
                removed += 1
            else:
                kept.append(line)
        kept_pages.append(kept)
    return kept_pages, removed


def _vertical_overlap(a: Line, b: Line) -> float:
    return min(a.top, b.top) - max(a.bottom, b.bottom)


def merge_line_pieces(lines: list[Line]) -> list[Line]:
    """Re-join a visual line split by a superscript or a font change: the piece starts right of
    the previous one's end and overlaps it vertically."""
    merged: list[Line] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.page == line.page
            and line.x0 >= prev.x1 - 1
            and _vertical_overlap(prev, line) > 0.3 * min(prev.size, line.size)
        ):
            gap = line.x0 - prev.x1
            glue = "" if gap < 0.15 * prev.size or line.text[:1] in ".,;:)»" else " "
            merged[-1] = replace(
                prev,
                text=prev.text.rstrip() + glue + line.text.lstrip(),
                x1=line.x1,
                top=max(prev.top, line.top),
                bottom=min(prev.bottom, line.bottom),
            )
        else:
            merged.append(line)
    return merged


def hyphenation_density(pages: list[list[Line]]) -> float:
    """Hyphen markers PDFium left inside joined words, per page (T072).

    Typeset documents hyphenate syllables (several per page); Word-made PDFs have almost none,
    and there the rare marker stands for a real hyphen of a compound word.
    """
    markers = sum(
        line.text.rstrip()[:-1].count(mark)
        for lines in pages
        for line in lines
        for mark in _HYPHEN_MARKS
    )
    return markers / max(len(pages), 1)


def _clean(text: str, *, syllable_hyphens: bool) -> str:
    inner = text.rstrip()
    replacement = "" if syllable_hyphens else "-"
    for mark in _HYPHEN_MARKS:
        if mark in inner[:-1]:
            inner = inner[:-1].replace(mark, replacement) + inner[-1]
    return inner.strip()


def _join(paragraph: str, line: str) -> str:
    if paragraph.endswith(_HYPHEN_MARKS):
        return paragraph[:-1] + line  # syllable hyphen at the line end
    if re.search(r"\w-$", paragraph):
        return paragraph + line  # compound word or "210-" + "ФЗ": keep the hyphen
    return f"{paragraph} {line}"


@dataclass
class _Stats:
    margin: float
    right: float
    spacing: float
    size: float


def _page_stats(lines: list[Line]) -> _Stats:
    sizes = [line.size for line in lines] or [10.0]
    x0s = Counter(round(line.x0) for line in lines)
    margin = float(min((x for x, n in x0s.items() if n >= max(2, 0.1 * len(lines))), default=0))
    if not margin and lines:
        margin = min(line.x0 for line in lines)
    right = max((line.x1 for line in lines), default=0.0)
    gaps = [a.top - b.top for a, b in pairwise(lines) if 0 < a.top - b.top < 3 * median(sizes)]
    return _Stats(
        margin=margin,
        right=right,
        spacing=median(gaps) if gaps else 1.2 * median(sizes),
        size=median(sizes),
    )


def _centered(line: Line, stats: _Stats) -> bool:
    left = line.x0 - stats.margin
    right = stats.right - line.x1
    return left > 2 * stats.size and right > 2 * stats.size and abs(left - right) < 2 * stats.size


def _starts_paragraph(prev: Line, line: Line, stats: _Stats, prev_stats: _Stats) -> bool:
    indent = line.x0 - stats.margin
    # Blank space between the lines, not top-to-top: PDFium returns a hyphen-joined pair of
    # visual lines as one line, which would otherwise look like a paragraph gap.
    if prev.page == line.page and prev.bottom - line.top > 0.8 * stats.spacing:
        return True
    if _centered(line, stats) != _centered(prev, prev_stats):
        return True
    if _centered(line, stats):
        return False  # a multi-line centred heading continues
    if indent > 0.6 * stats.size:
        return True
    ends_sentence = bool(_SENTENCE_END_RE.search(prev.text)) and not _ABBREVIATION_END_RE.search(
        prev.text
    )
    if ends_sentence and (_LIST_START_RE.match(line.text) or line.text.startswith("(")):
        return True  # a list item, or a parenthetical note such as an edition remark
    if _DASH_ITEM_RE.match(line.text) and prev.text.rstrip().endswith((",", ";", ":", ".")):
        return True  # dash list items often end with a comma
    deliberate = prev.page == line.page and _next_word_fits(prev, line, prev_stats)
    starts_new = line.text[:1].isupper() or bool(_LIST_START_RE.match(line.text))
    return deliberate and (ends_sentence or starts_new)


def _next_word_fits(prev: Line, line: Line, stats: _Stats) -> bool:
    """The first word of `line` would have fitted at the end of `prev`: the break was deliberate,
    so `prev` ends a paragraph. Works for justified and ragged-right text alike."""
    words = line.text.split()
    if not words or not prev.text:
        return False
    char_width = (prev.x1 - prev.x0) / max(len(prev.text), 1)
    # Width is estimated from the average glyph, so demand a margin: a missed break is usually
    # caught by indent or list rules, while a false one cuts a point in two.
    needed = (len(words[0]) + 2) * char_width * 1.2
    return stats.right - prev.x1 > needed


def detect_columns(lines: list[Line]) -> bool:
    """Two or more text lines side by side at the same height, repeatedly: a multi-column page."""
    side_by_side = 0
    for a, b in pairwise(lines):
        if _vertical_overlap(a, b) > 0.5 * a.size and b.x0 > a.x1 + 2 * a.size:
            side_by_side += 1
    return side_by_side >= 5


def build_blocks(pages: list[list[Line]]) -> LayoutResult:
    pages, removed = strip_running_headers(pages)
    columns_pages = [i + 1 for i, lines in enumerate(pages) if detect_columns(lines)]
    syllable_hyphens = hyphenation_density(pages) >= SYLLABLE_HYPHENS_PER_PAGE
    pages = [
        merge_line_pieces(
            [
                replace(ln, text=_clean(ln.text, syllable_hyphens=syllable_hyphens))
                for ln in lines
                if ln.text.strip()
            ]
        )
        for lines in pages
    ]
    stats = {lines[0].page: _page_stats(lines) for lines in pages if lines}

    blocks: list[Block] = []
    current: list[Line] = []

    def flush() -> None:
        if not current:
            return
        first = current[0]
        page_stats = stats[first.page]
        text = current[0].text
        for line in current[1:]:
            text = _join(text, line.text)
        same_page = [line for line in current if line.page == first.page]
        blocks.append(
            Block(
                text=text,
                page=first.page,
                bbox=(
                    min(line.x0 for line in same_page),
                    min(line.bottom for line in same_page),
                    max(line.x1 for line in same_page),
                    max(line.top for line in same_page),
                ),
                indent=round(first.x0 - page_stats.margin, 1),
                font_size=round(median(line.size for line in current), 1) or None,
                align="center" if _centered(first, page_stats) else None,
            )
        )
        current.clear()

    prev: Line | None = None
    for lines in pages:
        for line in lines:
            if prev is not None and _starts_paragraph(
                prev, line, stats[line.page], stats[prev.page]
            ):
                flush()
            current.append(line)
            prev = line
    flush()
    return LayoutResult(blocks=blocks, header_footer_lines=removed, columns_pages=columns_pages)
