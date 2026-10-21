"""NFR-001 / principle VII: no code, config or prompt is tailored to a known evaluation document.

Markers of every eval and holdout document live in evals/markers.toml (R-21):

    [documents.<slug>]
    markers = ["...", "..."]
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = [ROOT / "src" / "gr"]
SCANNED_SUFFIXES = {".py", ".toml", ".md", ".html", ".txt", ".json", ".j2"}
MARKERS_FILE = ROOT / "evals" / "markers.toml"

# The primary test regulation is known from day one; others come from evals/markers.toml.
BUILTIN_MARKERS = {"reg261": ["Росимущ", "земельн", "59820"]}


def load_markers() -> dict[str, list[str]]:
    markers = {slug: list(values) for slug, values in BUILTIN_MARKERS.items()}
    if MARKERS_FILE.exists():
        data = tomllib.loads(MARKERS_FILE.read_text(encoding="utf-8"))
        for slug, entry in data.get("documents", {}).items():
            markers.setdefault(slug, []).extend(entry.get("markers", []))
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
            leaks.extend(
                f"{shown}: «{value}» ({slug})" for value in values if value.casefold() in text
            )
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
