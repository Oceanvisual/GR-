"""Duplicates in an answer (FR-026): merged, never silently dropped.

Two operations are the same when their actions cite the same fragment with the same
normalised wording; the first one stays and takes the other's dependencies and conditions,
and every reference to the duplicate is redirected. Two processes with the same normalised
name are merged like windows of one process.
"""

from collections.abc import Callable

from gr.extraction.detailing import DetailedProcess, merge_windows
from gr.extraction.schemas import DecisionPointOut, DetailOut, OperationOut
from gr.grounding.normalize import normalize_for_match


def _unique[T](items: list[T]) -> list[T]:
    return list(dict.fromkeys(items))


def dedupe_operations(
    detail: DetailOut, resolve: Callable[[str], str | None]
) -> tuple[DetailOut, list[tuple[str, str]]]:
    """The answer without duplicate operations, and the (duplicate, kept) pairs."""
    kept: dict[tuple[str, str], OperationOut] = {}
    redirect: dict[str, str] = {}
    for operation in detail.operations:
        identity = (
            resolve(operation.action.ref) or operation.action.ref,
            normalize_for_match(operation.action.text),
        )
        first = kept.get(identity)
        if first is None:
            kept[identity] = operation
            continue
        redirect[operation.key] = first.key
        conditions = {c.model_dump_json(): c for c in [*first.conditions, *operation.conditions]}
        kept[identity] = first.model_copy(
            update={
                "depends_on": _unique([*first.depends_on, *operation.depends_on]),
                "conditions": list(conditions.values()),
            }
        )

    def target(key: str) -> str:
        return redirect.get(key, key)

    operations = [
        op.model_copy(
            update={
                "depends_on": [
                    d for d in _unique([target(d) for d in op.depends_on]) if d != op.key
                ]
            }
        )
        for op in kept.values()
    ]
    points: list[DecisionPointOut] = [
        dp.model_copy(
            update={
                "after_operation": target(dp.after_operation),
                "outcomes": [
                    o.model_copy(update={"next_operation": target(o.next_operation)})
                    for o in dp.outcomes
                ],
            }
        )
        for dp in detail.decision_points
    ]
    merged = detail.model_copy(update={"operations": operations, "decision_points": points})
    return merged, list(redirect.items())


def dedupe_processes(
    processes: list[DetailedProcess], names: dict[str, str]
) -> tuple[list[DetailedProcess], list[tuple[str, str]]]:
    """Processes with the same normalised name merged into the first; (merged, kept) pairs."""
    groups: dict[str, list[DetailedProcess]] = {}
    for process in processes:
        name = normalize_for_match(names.get(process.process_key, "")) or process.process_key
        groups.setdefault(name, []).append(process)
    result: list[DetailedProcess] = []
    pairs: list[tuple[str, str]] = []
    for group in groups.values():
        first = group[0]
        if len(group) > 1:
            first = first.model_copy(
                update={
                    "detail": merge_windows([p.detail for p in group]),
                    "windows": sum(p.windows for p in group),
                }
            )
            pairs += [(p.process_key, first.process_key) for p in group[1:]]
        result.append(first)
    return result, pairs
