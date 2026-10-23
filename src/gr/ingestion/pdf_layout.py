"""PDF lines → paragraphs (T072 findings). Pure Python over line geometry, no PDFium here.

Lines come from PDFium's own text reconstruction; this module drops running headers and page
numbers, re-attaches line pieces split by superscripts, and decides where a paragraph starts:
first-line indent, a list marker after a finished sentence, a large vertical gap, a centred
heading, or a short line that ends a sentence. A page set in columns is read column by column
(FR-019, T079): columns are found by the left edges of the lines, and each column has its own
margin and right edge for the paragraph rules.
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
MIN_COLUMN_LINES = 5  # lines starting at one left edge, side by side with lines left of it
COLUMN_GUTTER = 1.5  # blank band between columns, in font sizes
COLUMN_LINE_WIDTH = 12  # median width of a column's lines, in font sizes: prose, not table cells

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
    column: int | None = None  # on a page set in columns; None: the whole width


@dataclass
class LayoutResult:
    blocks: list[Block]
    header_footer_lines: int = 0
    columns_pages: list[int] = field(default_factory=list)


def _positive(value: float) -> float | None:
    return value if value > 0 else None


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
    next_column = None not in (prev.column, line.column) and prev.column != line.column
    if next_column and _LIST_START_RE.match(line.text):
        return True  # a numbered item tops the column: the heading that ended the last one ended
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


def column_starts(lines: list[Line]) -> list[float]:
    """Where each column after the first starts; [] for a page in one column.

    A column start is a left edge shared by several lines that stand side by side with lines
    left of it, with a blank band between them, and that are as wide as prose: indented first
    lines have nothing beside them, and the cells of a two-column table are short.
    """
    if len(lines) < 2 * MIN_COLUMN_LINES:
        return []
    size = median(line.size for line in lines)
    clusters: list[list[Line]] = []
    for line in sorted(lines, key=lambda ln: ln.x0):
        if clusters and line.x0 - clusters[-1][0].x0 <= 0.5 * size:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    starts = []
    for members in clusters[1:]:
        if len(members) < MIN_COLUMN_LINES:
            continue
        start = members[0].x0
        left = [line for line in lines if line.x1 <= start - COLUMN_GUTTER * size]
        beside = [
            line
            for line in members
            if any(_vertical_overlap(line, other) > 0.5 * line.size for other in left)
        ]
        wide = median(line.x1 - line.x0 for line in members) >= COLUMN_LINE_WIDTH * size
        if len(beside) >= MIN_COLUMN_LINES and wide:
            starts.append(start)
    return starts


def detect_columns(lines: list[Line]) -> bool:
    return bool(column_starts(lines))


def in_reading_order(lines: list[Line], starts: list[float]) -> list[Line]:
    """Column by column; a line across the columns (a heading) ends the columns above it.

    Within a column the order of the page is kept: it is top to bottom whether the PDF was
    written column by column or row by row, and a line split by a superscript stays together.
    """
    size = median(line.size for line in lines)
    ordered: list[Line] = []
    columns: list[list[Line]] = [[] for _ in range(len(starts) + 1)]

    def close() -> None:
        for column in columns:
            ordered.extend(column)
            column.clear()

    for line in lines:
        index = sum(1 for start in starts if line.x0 >= start - 0.5 * size)
        across = index < len(starts) and line.x1 > starts[index] - size
        if across:
            close()
            ordered.append(line)
        else:
            columns[index].append(replace(line, column=index))
    close()
    return ordered


def build_blocks(pages: list[list[Line]]) -> LayoutResult:
    pages, removed = strip_running_headers(pages)
    pages = list(pages)  # the caller's list stays as it was
    columns_pages = []
    for i, lines in enumerate(pages):
        if starts := column_starts(lines):
            columns_pages.append(i + 1)
            pages[i] = in_reading_order(lines, starts)
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
    # A line across the page is measured against the whole page; a column line, its column.
    stats: dict[tuple[int, int | None], _Stats] = {}
    for lines in pages:
        if lines:
            stats[(lines[0].page, None)] = _page_stats(lines)
        for column in {line.column for line in lines} - {None}:
            in_column = [line for line in lines if line.column == column]
            stats[(in_column[0].page, column)] = _page_stats(in_column)

    blocks: list[Block] = []
    current: list[Line] = []

    def flush() -> None:
        if not current:
            return
        first = current[0]
        page_stats = stats[(first.page, first.column)]
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
                font_size=_positive(round(median(line.size for line in current), 1)),
                align="center" if _centered(first, page_stats) else None,
            )
        )
        current.clear()

    prev: Line | None = None
    for lines in pages:
        for line in lines:
            if prev is not None and _starts_paragraph(
                prev, line, stats[(line.page, line.column)], stats[(prev.page, prev.column)]
            ):
                flush()
            current.append(line)
            prev = line
    flush()
    return LayoutResult(blocks=blocks, header_footer_lines=removed, columns_pages=columns_pages)
