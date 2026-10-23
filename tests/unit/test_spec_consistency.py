"""Consistency of Spec Kit artifacts: task graph and requirement traceability."""

import re
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs" / "001-npa-ai-analyzer"
TASK_HEAD_RE = re.compile(r"^- \[[ xX]\] (T\d{3})\b", re.M)
DEPS_RE = re.compile(r"^  - \*\*Зависит\*\*: ([^\n]*)", re.M)
REQ_RE = re.compile(r"\*\*((?:FR|NFR|SC)-\d{3})\b")


def _task_graph() -> dict[str, set[str]]:
    text = (SPEC_DIR / "tasks.md").read_text(encoding="utf-8")
    heads = list(TASK_HEAD_RE.finditer(text))
    graph: dict[str, set[str]] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[head.start() : end]
        task = head.group(1)
        assert task not in graph, f"duplicate task id {task}"
        deps = DEPS_RE.search(block)
        graph[task] = set(re.findall(r"T\d{3}", deps.group(1))) if deps else set()
    return graph


def test_tasks_present() -> None:
    assert len(_task_graph()) > 0


def test_task_dependencies_exist() -> None:
    graph = _task_graph()
    unknown = {task: deps - graph.keys() for task, deps in graph.items() if deps - graph.keys()}
    assert not unknown, f"unknown dependencies: {unknown}"


def test_task_dependencies_acyclic() -> None:
    graph = _task_graph()
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        assert node not in visiting, f"dependency cycle: {' -> '.join([*path, node])}"
        if node in done:
            return
        visiting.add(node)
        for dep in sorted(graph.get(node, set())):
            visit(dep, [*path, node])
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        visit(node, [])


def test_every_requirement_is_traced() -> None:
    spec = (SPEC_DIR / "spec.md").read_text(encoding="utf-8")
    others = "\n".join(
        p.read_text(encoding="utf-8") for p in SPEC_DIR.rglob("*.md") if p.name != "spec.md"
    )
    defined = set(REQ_RE.findall(spec))
    assert defined, "no requirement ids found in spec.md"
    missing = sorted(req for req in defined if not re.search(rf"\b{req}\b", others))
    assert not missing, f"requirements not referenced outside spec.md: {missing}"
