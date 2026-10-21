"""Competing structure strategies (FR-018, R-22). Each builds a full tree from the same units and
reports the evidence the scoring needs; none may assume a particular document."""

from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median

from gr.domain.enums import FragmentKind
from gr.structure.patterns import Marker, classify, looks_like_heading, starts_lowercase
from gr.structure.regulation import run_regulation
from gr.structure.tree import Node, Unit, covered_units, paragraph_node, short_title

_K = FragmentKind
LARGE_FONT = 1.2  # a heading by size: this many times the document's median size
MAX_LAYOUT_HEADING_LINES = 6


@dataclass
class StrategyOutcome:
    name: str
    root: Node
    warnings: list[str] = field(default_factory=list)
    covered: int = 0  # units with a structural role
    numbered: int = 0
    rejected: int = 0
    duplicates: int = 0
    headings: int = 0
    largest_group: int = 0  # units under the biggest top-level heading (layout)


Strategy = Callable[[list[Unit]], StrategyOutcome]


def regulation(units: list[Unit]) -> StrategyOutcome:
    outcome = run_regulation(units)
    return StrategyOutcome(
        name="regulation",
        root=outcome.root,
        warnings=outcome.warnings,
        covered=covered_units(outcome.root),
        numbered=outcome.numbered,
        rejected=outcome.rejected,
        duplicates=outcome.duplicates,
    )


def _heading_level(unit: Unit, median_size: float | None) -> int | None:
    """Level of a heading recognised by its formatting alone, or None."""
    if unit.in_table or classify(unit.text).marker == Marker.LIST_ITEM:
        return None
    if unit.style_level is not None:
        return unit.style_level
    if not looks_like_heading(unit.text):
        return None
    large = bool(unit.size and median_size and unit.size >= LARGE_FONT * median_size)
    if large or _capitals(unit):
        return 1
    if unit.bold or unit.centered:
        return 2
    return None


def layout(units: list[Unit]) -> StrategyOutcome:
    """Headings by formatting (style, size, bold, centring, capitals); paragraphs under them."""
    sizes = [u.size for u in units if u.size]
    median_size = median(sizes) if sizes else None
    levels = [_heading_level(u, median_size) for u in units]
    root = Node(_K.ROOT, "", "документ", ref="")
    open_headings: list[tuple[int, Node]] = []
    counters: dict[int, int] = {}
    headings = 0
    i = 0
    while i < len(units):
        level = levels[i]
        if level is None:
            parent = open_headings[-1][1] if open_headings else root
            paragraph_node(parent, units[i])
            i += 1
            continue
        end = i + 1  # a heading carried over to the next lines (lowercase or all in capitals)
        while (
            end < len(units)
            and levels[end] == level
            and end - i < MAX_LAYOUT_HEADING_LINES
            and (
                starts_lowercase(units[end].text) or (_capitals(units[i]) and _capitals(units[end]))
            )
        ):
            end += 1
        while open_headings and open_headings[-1][0] >= level:
            open_headings.pop()
        parent = open_headings[-1][1] if open_headings else root
        counters[id(parent)] = counters.get(id(parent), 0) + 1
        title = " ".join(u.text for u in units[i:end])
        top = parent is root
        kind = _K.SECTION if top else _K.SUBSECTION
        label = f"{'разд.' if top else 'подразд.'} «{short_title(title)}»"
        segment = f"{'s' if top else 'h'}{counters[id(parent)]}"
        node = parent.add(Node(kind, segment, label, heading=title, units=units[i:end]))
        open_headings.append((level, node))
        headings += 1
        i = end
    largest = max(
        (_subtree_units(child) for child in root.children if child.kind == _K.SECTION), default=0
    )
    return StrategyOutcome(
        name="layout",
        root=root,
        covered=covered_units(root),
        headings=headings,
        largest_group=largest,
    )


def _capitals(unit: Unit) -> bool:
    letters = [ch for ch in unit.text if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) >= 0.9 * len(letters)


def _subtree_units(node: Node) -> int:
    return len(node.units) + sum(_subtree_units(child) for child in node.children)


def paragraphs(units: list[Unit]) -> StrategyOutcome:
    """Always succeeds: one fragment per paragraph, labelled with its page when known."""
    root = Node(_K.ROOT, "", "документ")
    for k, unit in enumerate(units, 1):
        label = f"абз. {k}" + (f", стр. {unit.page}" if unit.page else "")
        root.add(Node(_K.PARAGRAPH, f"p{k}", label, units=[unit]))
    warning = "нумерация не распознана: структура построена по абзацам, ссылки менее точны"
    return StrategyOutcome(name="paragraphs", root=root, warnings=[warning])


STRATEGIES: dict[str, Strategy] = {
    "regulation": regulation,
    "layout": layout,
    "paragraphs": paragraphs,
}
