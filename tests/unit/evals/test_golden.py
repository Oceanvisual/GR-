"""The golden annotation fits the documents as the parser reads them now (T036).

Golden processes point to fragments by label. A parser change that renames or drops a fragment
would make the eval silently score against nothing, so every label is checked here, without
the LLM.
"""

import hashlib
from functools import cache
from pathlib import Path

import pytest

from evals.golden import Case, Golden, load_cases
from evals.metrics import FragmentNode, subtree_labels
from gr.services.parsing import ParsedDocument, ParseLimits, parse_file

ROOT = Path(__file__).resolve().parents[3]
LIMITS = ParseLimits(max_pages=500, max_chars=2_000_000, max_unzipped_bytes=200 << 20)
CASES = load_cases()


@cache
def nodes(case_id: str) -> list[FragmentNode]:
    case = next(c for c in CASES if c.id == case_id)
    parsed = parse_file(str(ROOT / case.path), "doc_eval", LIMITS)
    assert isinstance(parsed, ParsedDocument), parsed
    index = {f.id: n for n, f in enumerate(parsed.fragments)}
    return [
        FragmentNode(label=f.label, parent=index[f.parent_id] if f.parent_id else None)
        for f in parsed.fragments
    ]


def test_dataset_lists_the_eval_set() -> None:
    assert [c.id for c in CASES] == ["reg261", "fz59"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_golden_is_for_this_edition_of_the_file(case: Case) -> None:
    golden = case.load_golden()
    assert hashlib.sha256((ROOT / case.path).read_bytes()).hexdigest() == golden.sha256


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_label_exists(case: Case) -> None:
    golden = case.load_golden()
    fragments = nodes(case.id)
    for process in golden.processes:
        subtree_labels(fragments, process.roots)
        for operation in process.operations:
            subtree_labels(fragments, operation.labels)
        subtree_labels(fragments, [d.label for d in process.deadlines])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_processes_do_not_share_fragments(case: Case) -> None:
    golden = case.load_golden()
    fragments = nodes(case.id)
    seen: dict[str, str] = {}
    for process in golden.processes:
        for label in subtree_labels(fragments, process.roots):
            assert label not in seen, f"{label}: {seen[label]} и {process.key}"
            seen[label] = process.key


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_operations_and_deadlines_lie_in_their_process(case: Case) -> None:
    golden = case.load_golden()
    fragments = nodes(case.id)
    for process in golden.processes:
        inside = subtree_labels(fragments, process.roots)
        for operation in process.operations:
            assert set(operation.labels) <= inside, (process.key, operation.name)
        for deadline in process.deadlines:
            assert deadline.label in inside, (process.key, deadline.what)


def test_golden_keys_are_unique() -> None:
    for case in CASES:
        golden: Golden = case.load_golden()
        keys = [p.key for p in golden.processes]
        assert len(keys) == len(set(keys)), case.id
        assert golden.required(), case.id
