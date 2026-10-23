"""Versions of the initiatives of a process (FR-083, data-model §8, T052).

The analysis made version 1 of every process; a regeneration makes the next version of one
process. The effective initiatives of a run are those of the latest version of each process. A
new initiative inherits the user's assumptions of the one it replaces when both act on the same
operations with the same solution category; decisions on replaced initiatives stay in the log
and are shown as decisions on the previous version.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from gr.domain.initiative import Initiative, InitiativeSet, InitiativeVersion


class _OfProcess(Protocol):
    @property
    def process_id(self) -> str: ...


@dataclass(frozen=True)
class Retired:
    """An initiative or a finding of a replaced version."""

    process_id: str
    version: int
    title: str  # the initiative's title, the finding's description


def current(versions: Sequence[InitiativeVersion]) -> dict[str, InitiativeVersion]:
    """The latest version of every regenerated process."""
    latest: dict[str, InitiativeVersion] = {}
    for item in versions:
        if item.process_id not in latest or item.version > latest[item.process_id].version:
            latest[item.process_id] = item
    return latest


def merge(
    base: InitiativeSet, versions: Sequence[InitiativeVersion], order: Sequence[str]
) -> InitiativeSet:
    """The analysis's initiatives with every regenerated process taken from its latest version;
    processes follow `order` (the process map), items of a process keep their order."""
    latest = current(versions)
    rank = {key: n for n, key in enumerate(order)}
    parts = [base, *(v.result for v in latest.values())]

    def pick[T: _OfProcess](items: list[list[T]]) -> list[T]:
        own = [x for n, part in enumerate(items) for x in part if n or x.process_id not in latest]
        return sorted(own, key=lambda x: rank.get(x.process_id, len(rank)))

    return InitiativeSet(
        findings=pick([p.findings for p in parts]),
        initiatives=pick([p.initiatives for p in parts]),
        no_change=pick([p.no_change for p in parts]),
        issues=pick([p.issues for p in parts]),
    )


def retired(base: InitiativeSet, versions: Sequence[InitiativeVersion]) -> dict[str, Retired]:
    """Initiatives and findings of the replaced versions, by id."""
    latest = current(versions)
    found: dict[str, Retired] = {}
    olds = [(base, 1, set(latest))]
    olds += [
        (v.result, v.version, {v.process_id}) for v in versions if latest[v.process_id] is not v
    ]
    for result, version, processes in olds:
        for initiative in result.initiatives:
            if initiative.process_id in processes:
                found[initiative.id] = Retired(initiative.process_id, version, initiative.title)
        for finding in result.findings:
            if finding.process_id in processes:
                found[finding.id] = Retired(finding.process_id, version, finding.description)
    return found


def successors(previous: Sequence[Initiative], new: Sequence[Initiative]) -> dict[str, str]:
    """New initiative → the previous one acting on the same operations with the same solution
    category; one to one, in id order."""
    free = sorted(previous, key=lambda i: i.id)
    found: dict[str, str] = {}
    for item in sorted(new, key=lambda i: i.id):
        match = next(
            (
                old
                for old in free
                if set(old.operation_ids) == set(item.operation_ids)
                and old.solution_category == item.solution_category
            ),
            None,
        )
        if match is not None:
            free.remove(match)
            found[item.id] = match.id
    return found
