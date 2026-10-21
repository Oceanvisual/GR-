"""Structure primitives shared by every strategy: units of text, tree nodes, flattening.

Every paragraph of the document is the own text of exactly one fragment, so the own texts of all
fragments, in order, rebuild the full text. A fragment's [start, end) covers its subtree — the
text a quote of that fragment may come from (R-11).
"""

import re
from dataclasses import dataclass, field

from gr.domain.document import Block, Fragment, RawDocument
from gr.domain.enums import FragmentKind, TextSource
from gr.domain.ir import IRText
from gr.grounding.normalize import normalize_document_text

_K = FragmentKind
_HEADING_STYLE_RE = re.compile(r"(?:heading|заголовок)\s*(\d)", re.I)


@dataclass
class Unit:
    """An atom of structure: one paragraph, or one table row (its cells joined by ` | `)."""

    blocks: list[int]
    text: str
    start: int
    end: int
    page: int | None
    centered: bool
    emphasized: bool
    in_table: bool
    ocr: bool
    bold: bool = False
    size: float | None = None
    style_level: int | None = None  # «Heading 2» → 2; «Title» → 1


@dataclass
class Node:
    kind: FragmentKind
    segment: str
    label: str
    number: str | None = None
    heading: str | None = None
    units: list[Unit] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None
    scope: str = ""  # label suffix of the numbering context: " вводной части", " прил. 1"
    paragraphs: int = 0  # абзацы counted under this node
    last_sub: tuple[int, ...] | None = None
    last_letter: str | None = None
    approved_doc: str | None = None
    ref: str | None = None  # how children refer to this node, if not by its label

    @property
    def reference(self) -> str:
        return self.label if self.ref is None else self.ref

    def add(self, child: "Node") -> "Node":
        child.parent = self
        self.children.append(child)
        return child


def normalized_blocks(raw: RawDocument) -> list[Block]:
    blocks = []
    for block in raw.blocks:
        text = normalize_document_text(block.text).text.replace("\n", " ").strip()
        blocks.append(block.model_copy(update={"text": text}))
    return blocks


def _style_level(style: str | None) -> int | None:
    if not style:
        return None
    if m := _HEADING_STYLE_RE.search(style):
        return int(m.group(1))
    return 1 if style.strip().lower() in ("title", "название") else None


def _same_row(a: Block, b: Block) -> bool:
    return (
        a.table is not None
        and b.table is not None
        and (a.table.table, a.table.row) == (b.table.table, b.table.row)
    )


def build_units(blocks: list[Block], ir: IRText) -> list[Unit]:
    units: list[Unit] = []
    for span in ir.spans:
        block = blocks[span.block]
        previous = units[-1] if units else None
        if previous is not None and _same_row(blocks[previous.blocks[-1]], block):
            previous.blocks.append(span.block)
            previous.end = span.end
            previous.text = ir.text[previous.start : previous.end]
            continue
        style = (block.style or "").lower()
        level = _style_level(block.style)
        units.append(
            Unit(
                blocks=[span.block],
                text=ir.text[span.start : span.end],
                start=span.start,
                end=span.end,
                page=block.page,
                centered=block.align == "center",
                emphasized=bool(block.bold) or level is not None or "заголов" in style,
                in_table=block.table is not None,
                ocr=block.text_source == TextSource.OCR,
                bold=bool(block.bold),
                size=block.font_size,
                style_level=level,
            )
        )
    return units


def paragraph_node(parent: Node, unit: Unit, kind: FragmentKind = _K.PARAGRAPH) -> Node:
    """An «абзац» child: an item's own text is its first paragraph, so numbering starts at 2."""
    if parent.kind in (_K.POINT, _K.PART, _K.SUBPOINT) and parent.paragraphs == 0:
        parent.paragraphs = 1
    parent.paragraphs += 1
    label = f"абз. {parent.paragraphs}"
    if parent.reference:
        label = f"{label} {parent.reference}"
    return parent.add(Node(kind, f"a{parent.paragraphs}", label, units=[unit]))


def short_title(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def covered_units(root: Node) -> int:
    """Units that got a structural role: everything except paragraphs lying directly at the root."""
    count = 0

    def visit(node: Node) -> None:
        nonlocal count
        loose = node.kind in (_K.PARAGRAPH, _K.LIST_ITEM) and node.parent is root
        if not loose:
            count += len(node.units)
        for child in node.children:
            visit(child)

    visit(root)
    return count


def flatten(root: Node, doc_id: str, ir: IRText) -> list[Fragment]:
    fragments: list[Fragment] = []

    def span(node: Node) -> tuple[int, int]:
        starts = [u.start for u in node.units]
        ends = [u.end for u in node.units]
        for child in node.children:
            child_start, child_end = span(child)
            starts.append(child_start)
            ends.append(child_end)
        return (min(starts), max(ends)) if starts else (0, 0)

    def visit(node: Node, path: str, parent_id: str | None) -> None:
        start, end = span(node)
        if node.kind == _K.ROOT:
            start, end = 0, len(ir.text)
        own = ir.text[node.units[0].start : node.units[-1].end] if node.units else ""
        fragment_id = path or "root"
        fragments.append(
            Fragment(
                id=fragment_id,
                doc_id=doc_id,
                kind=node.kind,
                number=node.number,
                label=node.label,
                heading=node.heading,
                text=own,
                start=start,
                end=end,
                pages=ir.pages_between(start, end),
                parent_id=parent_id,
                order=len(fragments),
                text_source=TextSource.OCR if any(u.ocr for u in node.units) else TextSource.NATIVE,
                approved_doc=node.approved_doc,
            )
        )
        seen: dict[str, int] = {}
        for child in node.children:
            seen[child.segment] = seen.get(child.segment, 0) + 1
            segment = (
                child.segment
                if seen[child.segment] == 1
                else f"{child.segment}~{seen[child.segment]}"
            )
            visit(child, f"{path}/{segment}" if path else segment, fragment_id)

    visit(root, "", None)
    return fragments


def stats(fragments: list[Fragment], ir: IRText) -> dict[str, int]:
    def count(*kinds: FragmentKind) -> int:
        return sum(1 for f in fragments if f.kind in kinds)

    return {
        "sections": count(_K.SECTION, _K.CHAPTER),
        "articles": count(_K.ARTICLE),
        "points": count(_K.POINT, _K.PART),
        "subpoints": count(_K.SUBPOINT),
        "paragraphs": count(_K.PARAGRAPH, _K.LIST_ITEM),
        "pages": len(ir.page_starts),
        "chars": len(ir.text),
    }
