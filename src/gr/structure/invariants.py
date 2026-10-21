"""Invariants every structure must satisfy on any input (R-22); used by tests and fuzzing."""

from gr.domain.document import Fragment
from gr.domain.ir import BLOCK_SEPARATOR


def check_invariants(fragments: list[Fragment], text: str) -> list[str]:
    """Problems found; an empty list means the tree is sound.

    - ids are unique, the root comes first, parents precede their children;
    - every child's [start, end) lies inside its parent's, and order follows start;
    - own texts, joined in order, rebuild the full text: nothing lost, nothing doubled.
    """
    problems: list[str] = []
    by_id: dict[str, Fragment] = {}
    for fragment in fragments:
        if fragment.id in by_id:
            problems.append(f"duplicate id {fragment.id}")
        by_id[fragment.id] = fragment
    if not fragments or fragments[0].parent_id is not None:
        problems.append("the first fragment must be the root")
    previous_start = -1
    for index, fragment in enumerate(fragments):
        if fragment.order != index:
            problems.append(f"{fragment.id}: order {fragment.order} at position {index}")
        if fragment.start < previous_start:
            problems.append(f"{fragment.id}: starts before the previous fragment")
        previous_start = fragment.start
        if not 0 <= fragment.start < fragment.end <= len(text):
            problems.append(f"{fragment.id}: span {fragment.start}..{fragment.end} outside text")
        if fragment.parent_id is None:
            continue
        parent = by_id.get(fragment.parent_id)
        if parent is None:
            problems.append(f"{fragment.id}: parent {fragment.parent_id} missing or after it")
        elif not parent.start <= fragment.start < fragment.end <= parent.end:
            problems.append(f"{fragment.id}: span outside parent {parent.id}")
        if (
            fragment.text
            and text[fragment.start : fragment.start + len(fragment.text)] != fragment.text
        ):
            problems.append(f"{fragment.id}: own text does not start at its offset")
    rebuilt = BLOCK_SEPARATOR.join(f.text for f in fragments if f.text)
    if rebuilt != text:
        problems.append("own texts do not rebuild the full text")
    return problems
