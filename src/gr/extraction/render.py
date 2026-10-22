"""Fragments as the LLM sees them (contracts/llm.md, R-10, R-12).

Fragment ids are replaced by short aliases `F<n>` (document order); answers are mapped back
and an unknown alias is a validation issue for the caller, never an exception. Markers that
delimit the document in the prompt are escaped inside its text with full-width look-alikes,
which quote matching folds back (NFKC), so quotes stay verifiable.
"""

import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from gr.domain.document import Fragment

_ALIAS_RE = re.compile(r"\s*\[?\s*[fFфФ]\s*(\d+)\s*\]?\s*")
# Prompt structure markers: a document cannot open or close blocks of its own (R-12).
# No word boundary after the name: «<document_meta>» must be caught too.
_MARKER_RE = re.compile(r"([\[<])(\s*/?\s*(?:fragment|document|task))", re.IGNORECASE)
_FULL_WIDTH = {"[": "［", "<": "＜"}
_PATH_MAX_CHARS = 200


@dataclass(frozen=True)
class ChunkingConfig:
    outline_snippet_chars: int = 160
    outline_minor_snippet_chars: int = 60
    outline_max_chars: int = 40_000
    detail_max_chars: int = 60_000


@cache
def load_chunking() -> ChunkingConfig:
    raw = tomllib.loads(resources.files("gr.config").joinpath("chunking.toml").read_text("utf-8"))
    return ChunkingConfig(
        outline_snippet_chars=raw["outline"]["snippet_chars"],
        outline_minor_snippet_chars=raw["outline"]["minor_snippet_chars"],
        outline_max_chars=raw["outline"]["max_chars"],
        detail_max_chars=raw["detail"]["max_chars"],
    )


def escape(text: str) -> str:
    """Neutralise prompt markers inside document text and attribute values."""
    return _MARKER_RE.sub(lambda m: _FULL_WIDTH[m.group(1)] + m.group(2), text)


def _attribute(text: str) -> str:
    return escape(" ".join(text.split())).replace('"', "'")


class FragmentAliases:
    """`F<n>` for every fragment, n in document order starting at 1 (the root has none)."""

    def __init__(self, fragments: Sequence[Fragment]) -> None:
        ordered = sorted((f for f in fragments if f.parent_id is not None), key=lambda f: f.order)
        self._alias = {f.id: f"F{n}" for n, f in enumerate(ordered, 1)}
        self._id = {alias: fragment_id for fragment_id, alias in self._alias.items()}

    def alias(self, fragment_id: str) -> str:
        return self._alias[fragment_id]

    def resolve(self, alias: str) -> str | None:
        """The fragment id for an alias from an answer; None when it names nothing.

        Tolerates «f12», «[F12]» and a full fragment id written instead of the alias.
        """
        match = _ALIAS_RE.fullmatch(alias)
        if match is not None:
            return self._id.get(f"F{int(match.group(1))}")
        return alias if alias in self._alias else None


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _paths(fragments: Sequence[Fragment]) -> dict[str, str]:
    """Heading path of every fragment: its ancestors below the root, outermost first."""
    by_id = {f.id: f for f in fragments}
    paths: dict[str, str] = {}
    for fragment in sorted(fragments, key=lambda f: f.order):
        parent = by_id.get(fragment.parent_id or "")
        if parent is None or parent.parent_id is None:
            paths[fragment.id] = ""
            continue
        title = parent.label + (f". {_short(parent.heading)}" if parent.heading else "")
        above = paths.get(parent.id, "")
        paths[fragment.id] = f"{above} > {title}" if above else title
    return paths


def with_subtrees(fragments: Sequence[Fragment], ids: Iterable[str]) -> list[Fragment]:
    """The given fragments and all their descendants, once each, in document order."""
    wanted = set(ids)
    by_id = {f.id: f for f in fragments}
    selected: list[Fragment] = []
    for fragment in sorted(fragments, key=lambda f: f.order):
        node: Fragment | None = fragment
        while node is not None and node.id not in wanted:
            node = by_id.get(node.parent_id or "")
        if node is not None:
            selected.append(fragment)
    return selected


def render_fragments(
    selected: Sequence[Fragment], all_fragments: Sequence[Fragment], aliases: FragmentAliases
) -> str:
    """Full text of the selected fragments, each in its own `[fragment]` block."""
    paths = _paths(all_fragments)
    blocks = []
    for fragment in sorted(selected, key=lambda f: f.order):
        if not fragment.text or fragment.parent_id is None:
            continue
        path = _attribute(paths.get(fragment.id, ""))[:_PATH_MAX_CHARS]
        blocks.append(
            f'[fragment id="{aliases.alias(fragment.id)}" label="{_attribute(fragment.label)}"'
            + (f' path="{path}"' if path else "")
            + f"]\n{escape(fragment.text)}\n[/fragment]"
        )
    return "\n".join(blocks)


def render_document(body: str, meta: dict[str, str]) -> str:
    """The data part of the user message: metadata and the document block."""
    meta_line = "; ".join(f"{key}: {_attribute(value)}" for key, value in meta.items() if value)
    return f"<document_meta>{meta_line}</document_meta>\n<document>\n{body}\n</document>"
