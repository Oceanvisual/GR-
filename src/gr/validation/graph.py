"""Operation graph checks (FR-026): references, cycles and the order of operations.

Works on answer keys before ids are assigned. A dependency on an operation that does not
exist is dropped with an issue. A cycle is allowed only through an operation marked as a
return for rework (`is_loop_back`). Operations are ordered topologically along their
dependencies, ties by their order in the answer. A dependency on a loop-back step is the
back edge of the rework loop: it is not used for ordering, so the step a rework returns to
still comes before the rework itself.
"""

import heapq
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    key: str
    depends_on: tuple[str, ...]
    is_loop_back: bool = False


@dataclass
class GraphResult:
    order: list[str]  # keys in execution order
    depends_on: dict[str, list[str]]  # existing dependencies only
    dangling: list[tuple[str, str]] = field(default_factory=list)  # (operation, missing key)
    cycles: list[list[str]] = field(default_factory=list)  # cycles without a loop-back step


def _strong_components(keys: list[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative; components in no particular order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in keys:
        if root in index:
            continue
        work = [(root, iter(edges.get(root, [])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(edges.get(child, []))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def check_graph(nodes: Sequence[Node]) -> GraphResult:
    position = {node.key: i for i, node in enumerate(nodes)}
    loop_back = {node.key for node in nodes if node.is_loop_back}
    depends_on: dict[str, list[str]] = {}
    dangling: list[tuple[str, str]] = []
    for node in nodes:
        kept = []
        for dependency in node.depends_on:
            if dependency not in position:
                dangling.append((node.key, dependency))
            elif dependency != node.key and dependency not in kept:
                kept.append(dependency)
        depends_on[node.key] = kept

    # Edges point from a prerequisite to the operation that waits for it.
    edges: dict[str, list[str]] = {node.key: [] for node in nodes}
    for key, prerequisites in depends_on.items():
        for prerequisite in prerequisites:
            edges[prerequisite].append(key)
    cycles = [
        sorted(component, key=position.__getitem__)
        for component in _strong_components([n.key for n in nodes], edges)
        if len(component) > 1 and not loop_back & set(component)
    ]

    # Kahn's algorithm without the back edges (dependencies on a loop-back step).
    waiting = {
        key: {p for p in prerequisites if p not in loop_back}
        for key, prerequisites in depends_on.items()
    }
    ready = [(position[key], key) for key, pending in waiting.items() if not pending]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, key = heapq.heappop(ready)
        order.append(key)
        for follower in edges[key]:
            pending = waiting[follower]
            if key in pending:
                pending.discard(key)
                if not pending:
                    heapq.heappush(ready, (position[follower], follower))
    # Members of a forbidden cycle never become ready: they keep their answer order.
    order += sorted(set(position) - set(order), key=position.__getitem__)
    return GraphResult(order=order, depends_on=depends_on, dangling=dangling, cycles=cycles)
