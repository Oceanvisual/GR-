"""Import-layer rules from plan.md: pure layers must not depend on I/O, web or LLM code."""

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "gr"

# Pure layers and the gr subpackages each of them may import.
ALLOWED_INTERNAL: dict[str, set[str]] = {
    "domain": {"domain"},
    "grounding": {"domain", "grounding"},
    "structure": {"domain", "grounding", "structure"},
    "analysis": {"domain", "grounding", "analysis"},
}

FORBIDDEN_THIRD_PARTY = {
    "fastapi",
    "starlette",
    "uvicorn",
    "openai",
    "httpx",
    "sqlalchemy",
    "aiosqlite",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _violations(layer: str, path: Path) -> list[str]:
    found: list[str] = []
    for module in _imported_modules(path):
        top, _, rest = module.partition(".")
        if top in FORBIDDEN_THIRD_PARTY:
            found.append(module)
        elif top == "gr" and rest:
            subpackage = rest.split(".")[0]
            if subpackage not in ALLOWED_INTERNAL[layer]:
                found.append(module)
    return found


@pytest.mark.parametrize("layer", sorted(ALLOWED_INTERNAL))
def test_pure_layer_imports(layer: str) -> None:
    layer_dir = PACKAGE_ROOT / layer
    assert layer_dir.is_dir(), f"missing package gr.{layer}"
    problems = {
        str(path.relative_to(PACKAGE_ROOT)): bad
        for path in sorted(layer_dir.rglob("*.py"))
        if (bad := _violations(layer, path))
    }
    assert not problems, f"forbidden imports in gr.{layer}: {problems}"


def test_violation_is_detected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import fastapi\nfrom gr.storage import db\nfrom gr.domain import enums\n")
    assert sorted(_violations("analysis", bad)) == ["fastapi", "gr.storage"]
