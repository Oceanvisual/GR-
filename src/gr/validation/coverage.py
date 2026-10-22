"""Coverage report (FR-028): which fragments the processes use and which describe actions but
belong to no process.

A fragment is used when it lies in the text of a process (action or related fragments with
their subtrees) or is cited by any verified fact. Fragments marked by discovery as not
describing processes, with their subtrees, are not counted as gaps.
"""

import re
import tomllib
from collections.abc import Iterable, Sequence
from functools import cache
from importlib import resources

from gr.domain.document import Fragment
from gr.domain.process import CoverageReport
from gr.extraction.render import in_force, with_subtrees

_WORD_RE = re.compile(r"[а-яё]+")


@cache
def action_stems() -> tuple[str, ...]:
    text = resources.files("gr.config").joinpath("coverage.toml").read_text("utf-8")
    stems: list[str] = tomllib.loads(text)["action_stems"]
    return tuple(sorted(set(stems)))


def describes_action(text: str) -> bool:
    stems = action_stems()
    return any(word.startswith(stems) for word in _WORD_RE.findall(text.lower()))


def coverage_report(
    fragments: Sequence[Fragment],
    process_fragment_ids: Iterable[str],
    cited_fragment_ids: Iterable[str],
    non_process_fragment_ids: Iterable[str],
) -> CoverageReport:
    alive = in_force(fragments)
    counted = [f for f in fragments if f.parent_id is not None and f.text and f.id in alive]
    used = {f.id for f in with_subtrees(fragments, process_fragment_ids)} | set(cited_fragment_ids)
    non_process = {f.id for f in with_subtrees(fragments, non_process_fragment_ids)}
    uncovered = [
        f.id
        for f in counted
        if f.id not in used and f.id not in non_process and describes_action(f.text)
    ]
    return CoverageReport(
        fragments_total=len(counted),
        fragments_used=sum(1 for f in counted if f.id in used),
        uncovered_action_fragments=uncovered,
        non_process_fragments=[f.id for f in counted if f.id in non_process],
    )
