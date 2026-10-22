"""The S3 outline (R-10): one line per fragment, split into chunks within a character budget.

A chunk holds whole subtrees. A subtree larger than the budget is split among its children,
and every part repeats the line of the node it belongs to as context. Every fragment appears
in some chunk; the result depends only on the fragments.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from gr.domain.document import Fragment
from gr.domain.enums import FragmentKind
from gr.extraction.render import ChunkingConfig, FragmentAliases, escape, load_chunking

# Continuations of a point and lines of form templates: their start is enough to place them.
_MINOR = frozenset({FragmentKind.PARAGRAPH, FragmentKind.LIST_ITEM})


@dataclass(frozen=True)
class OutlineChunk:
    text: str
    fragment_ids: list[str] = field(default_factory=list)  # fragments listed, context included


@dataclass(frozen=True)
class _Line:
    fragment_id: str
    text: str


class _Outline:
    def __init__(
        self, fragments: Sequence[Fragment], aliases: FragmentAliases, cfg: ChunkingConfig
    ) -> None:
        self.cfg = cfg
        self.aliases = aliases
        self.children: dict[str | None, list[Fragment]] = {}
        for fragment in sorted(fragments, key=lambda f: f.order):
            self.children.setdefault(fragment.parent_id, []).append(fragment)

    def line(self, fragment: Fragment, depth: int) -> _Line:
        limit = (
            self.cfg.outline_minor_snippet_chars
            if fragment.kind in _MINOR
            else self.cfg.outline_snippet_chars
        )
        snippet = " ".join(fragment.text.split())[:limit]
        parts = [self.aliases.alias(fragment.id), fragment.label]
        if fragment.heading:
            parts.append(fragment.heading)
        head = " · ".join(escape(" ".join(p.split())) for p in parts)
        text = f"{'  ' * depth}{head}" + (f" — {escape(snippet)}" if snippet else "")
        return _Line(fragment.id, text)

    def subtree(self, fragment: Fragment, depth: int) -> list[_Line]:
        lines = [self.line(fragment, depth)]
        for child in self.children.get(fragment.id, []):
            lines += self.subtree(child, depth + 1)
        return lines

    def pack(self, nodes: list[Fragment], depth: int, budget: int) -> list[list[_Line]]:
        chunks: list[list[_Line]] = []
        current: list[_Line] = []
        size = 0
        for node in nodes:
            lines = self.subtree(node, depth)
            length = _size(lines)
            if length > budget and self.children.get(node.id):
                if current:
                    chunks.append(current)
                    current, size = [], 0
                head = self.line(node, depth)
                inner = self.pack(self.children[node.id], depth + 1, budget - len(head.text) - 1)
                chunks += [[head, *part] for part in inner]
                continue
            if current and size + length > budget:
                chunks.append(current)
                current, size = [], 0
            current += lines
            size += length
        if current:
            chunks.append(current)
        return chunks


def _size(lines: list[_Line]) -> int:
    return sum(len(line.text) + 1 for line in lines)


def _merge(parts: list[list[_Line]], budget: int) -> list[list[_Line]]:
    """Neighbouring parts that fit together: no call for a lone short preamble."""
    merged: list[list[_Line]] = []
    for part in parts:
        if merged and _size(merged[-1]) + _size(part) <= budget:
            merged[-1] = merged[-1] + part
        else:
            merged.append(part)
    return merged


def build_outline(
    fragments: Sequence[Fragment],
    aliases: FragmentAliases,
    config: ChunkingConfig | None = None,
) -> list[OutlineChunk]:
    cfg = config or load_chunking()
    outline = _Outline(fragments, aliases, cfg)
    roots = outline.children.get(None, [])
    top = [child for root in roots for child in outline.children.get(root.id, [])]
    parts = _merge(outline.pack(top, 0, cfg.outline_max_chars), cfg.outline_max_chars)
    return [
        OutlineChunk(
            text="\n".join(line.text for line in part),
            fragment_ids=[line.fragment_id for line in part],
        )
        for part in parts
    ]
