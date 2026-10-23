"""Intermediate representation: one block list for every format, and the full text built from
it with a map from offsets back to blocks and pages (FR-017, R-22)."""

from bisect import bisect_right
from dataclasses import dataclass
from functools import cached_property

from gr.domain.document import Block, RawDocument

BLOCK_SEPARATOR = "\n"
CELL_SEPARATOR = " | "


@dataclass(frozen=True)
class BlockSpan:
    block: int  # index into the block list
    start: int
    end: int


@dataclass(frozen=True)
class IRText:
    """Full document text plus where each block and page starts in it."""

    text: str
    spans: tuple[BlockSpan, ...]
    page_starts: tuple[int, ...]  # page_starts[p - 1] = offset where page p begins

    def block_at(self, offset: int) -> int:
        """Index of the block containing `offset`; a separator belongs to the block before it."""
        if not self.spans or not 0 <= offset < len(self.text):
            raise IndexError(f"offset {offset} outside the text")
        position = bisect_right(self._starts, offset) - 1
        return self.spans[max(position, 0)].block

    def page_at(self, offset: int) -> int | None:
        if not self.page_starts:
            return None
        if not 0 <= offset < max(len(self.text), 1):
            raise IndexError(f"offset {offset} outside the text")
        return bisect_right(self.page_starts, offset)

    def pages_between(self, start: int, end: int) -> list[int]:
        """1-based pages that the half-open range [start, end) touches."""
        first, last = self.page_at(start), self.page_at(max(start, end - 1))
        if first is None or last is None:
            return []
        return list(range(first, last + 1))

    @cached_property
    def _starts(self) -> list[int]:
        return [span.start for span in self.spans]


def _same_row(a: Block, b: Block) -> bool:
    return (
        a.table is not None
        and b.table is not None
        and (a.table.table, a.table.row) == (b.table.table, b.table.row)
    )


def full_text(blocks: list[Block] | RawDocument) -> IRText:
    """Join blocks into one text. Deterministic: the same blocks give the same text and spans.

    Blocks are separated by a newline; cells of one table row stay on one line, separated by
    ` | `. Blank blocks are skipped. Pages without text start where the next text starts.
    """
    items = blocks.blocks if isinstance(blocks, RawDocument) else blocks
    parts: list[str] = []
    spans: list[BlockSpan] = []
    page_first_offset: dict[int, int] = {}
    length = 0
    previous: Block | None = None
    for index, block in enumerate(items):
        if not block.text.strip():
            continue
        if previous is not None:
            separator = CELL_SEPARATOR if _same_row(previous, block) else BLOCK_SEPARATOR
            parts.append(separator)
            length += len(separator)
        spans.append(BlockSpan(index, length, length + len(block.text)))
        if block.page is not None:
            page_first_offset.setdefault(block.page, length)
        parts.append(block.text)
        length += len(block.text)
        previous = block

    page_starts: list[int] = []
    if page_first_offset:
        last_page = max(page_first_offset)
        next_start = length
        for page in range(last_page, 0, -1):
            next_start = page_first_offset.get(page, next_start)
            page_starts.append(next_start)
        page_starts.reverse()
        page_starts[0] = 0  # anything before the first text block belongs to page 1
    return IRText(text="".join(parts), spans=tuple(spans), page_starts=tuple(page_starts))
