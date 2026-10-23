"""NFR-001 / principle VII: no code, config or prompt is tailored to a known evaluation document.

Markers of eval and dev-corpus documents live in evals/markers.toml (R-21); holdout markers live
in evals/holdout/markers.toml, which is git-ignored and never read by the developer. Both use:

    [documents.<slug>]
    markers = ["...", "..."]

A holdout leak is reported without the marker itself, so the failure does not reveal it.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = [ROOT / "src" / "gr"]
SCANNED_SUFFIXES = {".py", ".toml", ".md", ".html", ".txt", ".json", ".j2"}
MARKERS_FILE = ROOT / "evals" / "markers.toml"
HOLDOUT_MARKERS_FILE = ROOT / "evals" / "holdout" / "markers.toml"
HIDDEN_PREFIX = "holdout:"

# The primary test regulation is known from day one; others come from evals/markers.toml.
BUILTIN_MARKERS = {"reg261": ["Росимущ", "земельн", "59820"]}


def _read(path: Path, prefix: str = "") -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        prefix + slug: list(entry.get("markers", []))
        for slug, entry in data.get("documents", {}).items()
    }


def load_markers() -> dict[str, list[str]]:
    markers = {slug: list(values) for slug, values in BUILTIN_MARKERS.items()}
    for slug, values in (_read(MARKERS_FILE) | _read(HOLDOUT_MARKERS_FILE, HIDDEN_PREFIX)).items():
        markers.setdefault(slug, []).extend(values)
    return markers


def scanned_files() -> list[Path]:
    return sorted(
        path
        for directory in SCANNED_DIRS
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in SCANNED_SUFFIXES
    )


def find_leaks(files: list[Path], markers: dict[str, list[str]]) -> list[str]:
    leaks: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        for slug, values in markers.items():
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            for value in values:
                if value.casefold() in text:
                    hidden = slug.startswith(HIDDEN_PREFIX)
                    leaks.append(f"{shown}: «{'***' if hidden else value}» ({slug})")
    return leaks


def test_no_document_specific_markers() -> None:
    leaks = find_leaks(scanned_files(), load_markers())
    assert not leaks, "код/конфиг/промпты содержат маркеры тестовых документов:\n" + "\n".join(
        leaks
    )


def test_leak_is_detected(tmp_path: Path) -> None:
    prompt = tmp_path / "detail.md"
    prompt.write_text("Пример: Территориальное управление РОСИМУЩЕСТВА …", encoding="utf-8")
    leaks = find_leaks([prompt], BUILTIN_MARKERS)
    assert leaks == [f"{prompt}: «Росимущ» (reg261)"]


def test_holdout_marker_is_not_revealed(tmp_path: Path) -> None:
    prompt = tmp_path / "detail.md"
    prompt.write_text("секретный маркер", encoding="utf-8")
    leaks = find_leaks([prompt], {HIDDEN_PREFIX + "doc": ["секретный"]})
    assert leaks == [f"{prompt}: «***» (holdout:doc)"]
