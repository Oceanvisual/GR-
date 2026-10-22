"""Versions of the initiatives of a process (FR-083, data-model §8, T052)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from initiative_builders import initiative
from pydantic import ValidationError

from gr.analysis.versions import Retired, current, merge, retired, successors
from gr.domain.enums import ProblemPattern, SolutionCategory
from gr.domain.initiative import (
    Finding,
    Initiative,
    InitiativeSet,
    InitiativeVersion,
    NoChangeDecision,
)

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
CLASSIC = SolutionCategory.CLASSIC_AUTOMATION


def proposal(key: str, process: str, ops: list[str], **updates: Any) -> Initiative:
    found = f"f_{key}"
    return initiative(
        updates.pop("category", CLASSIC), id=key, process_id=process, operation_ids=ops,
        finding_ids=[found], title=f"Инициатива {key}",
        reduction_potential=[{"operation_id": o, "level": "medium", "justification": "x"}
                             for o in ops],
        **updates,
    )  # fmt: skip


def problem(key: str, process: str) -> Finding:
    pattern = ProblemPattern.DUPLICATE_CHECKS
    return Finding(id=f"f_{key}", process_id=process, operation_ids=["op"], pattern=pattern,
                   description=f"Проблема {key}")  # fmt: skip


def of(process: str, *keys: str) -> InitiativeSet:
    return InitiativeSet(
        findings=[problem(k, process) for k in keys],
        initiatives=[proposal(k, process, [f"{process}.op_1"]) for k in keys],
    )


def version(process: str, number: int, *keys: str, **fields: Any) -> InitiativeVersion:
    return InitiativeVersion(process_id=process, version=number, based_on=T0,
                             result=of(process, *keys), **fields)  # fmt: skip


BASE = InitiativeSet(
    findings=[*of("proc_1", "a", "b").findings, *of("proc_2", "c").findings],
    initiatives=[*of("proc_1", "a", "b").initiatives, *of("proc_2", "c").initiatives],
    no_change=[NoChangeDecision(process_id="proc_3", justification="нечего менять")],
)
ORDER = ["proc_1", "proc_2", "proc_3"]


def test_without_regenerations_the_analysis_is_the_effective_version() -> None:
    assert merge(BASE, [], ORDER) == BASE
    assert retired(BASE, []) == {} and current([]) == {}


def test_the_latest_version_of_a_process_replaces_its_initiatives_only() -> None:
    second, third = version("proc_1", 2, "a2"), version("proc_1", 3, "a3", "b3")
    found = merge(BASE, [third, second], ORDER)  # order of the list does not matter
    assert [i.id for i in found.initiatives] == ["a3", "b3", "c"]
    assert [f.id for f in found.findings] == ["f_a3", "f_b3", "f_c"]
    assert found.no_change == BASE.no_change
    assert current([second, third]) == {"proc_1": third}
    assert retired(BASE, [second, third]) == {
        "a": Retired("proc_1", 1, "Инициатива a"),
        "b": Retired("proc_1", 1, "Инициатива b"),
        "f_a": Retired("proc_1", 1, "Проблема a"),
        "f_b": Retired("proc_1", 1, "Проблема b"),
        "a2": Retired("proc_1", 2, "Инициатива a2"),
        "f_a2": Retired("proc_1", 2, "Проблема a2"),
    }


def test_a_version_can_end_with_no_change() -> None:
    nothing = InitiativeVersion(
        process_id="proc_2", version=2, based_on=T0,
        result=InitiativeSet(no_change=[NoChangeDecision(process_id="proc_2",
                                                         justification="уже оптимально")]),
    )  # fmt: skip
    found = merge(BASE, [nothing], ORDER)
    assert [i.id for i in found.initiatives] == ["a", "b"]
    assert [n.process_id for n in found.no_change] == ["proc_2", "proc_3"]  # by the map order


def test_successors_share_operations_and_category_one_to_one() -> None:
    previous = [
        proposal("old_1", "p", ["op_1", "op_2"]),
        proposal("old_2", "p", ["op_2", "op_1"]),
        proposal("old_3", "p", ["op_3"], category=SolutionCategory.AI_NLP),
    ]
    new = [
        proposal("new_1", "p", ["op_2", "op_1"]),  # the operations as a set
        proposal("new_2", "p", ["op_1", "op_2"]),
        proposal("new_3", "p", ["op_1", "op_2"]),  # nothing left to take
        proposal("new_4", "p", ["op_3"]),  # another category
    ]
    assert successors(previous, new) == {"new_1": "old_1", "new_2": "old_2"}


def test_a_version_holds_its_own_process_only() -> None:
    with pytest.raises(ValidationError, match="own process"):
        InitiativeVersion(process_id="proc_1", version=2, based_on=T0, result=of("proc_2", "x"))
    with pytest.raises(ValidationError, match="predecessors"):
        version("proc_1", 2, "a2", predecessors={"ghost": "a"})
    with pytest.raises(ValidationError):
        version("proc_1", 1, "a2")  # version 1 is the analysis itself
