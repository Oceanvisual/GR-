"""Structure regression over the dev corpus and the golden synthetic files (FR-010…FR-012,
FR-016, SC-012, T068).

Every document parses without an error, and its tree — fragment ids, kinds and labels — matches
the snapshot `<name>.structure.json` next to it. A deliberate change of the parser rewrites the
snapshots with `UPDATE_SNAPSHOTS=1 uv run pytest tests/corpus`; the diff is then read by a
person before it is committed (SC-012: snapshots are checked by hand).
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from gr.services.parsing import ParsedDocument, ParseLimits, parse_file

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)
FORMATS = (".docx", ".pdf", ".html", ".doc", ".rtf", ".odt", ".txt")
CONVERTED = (".doc", ".rtf", ".odt")  # through LibreOffice (T075)
DOCUMENTS = sorted(
    [
        *(path for path in (FIXTURES / "corpus").iterdir() if path.suffix in FORMATS),
        FIXTURES / "synthetic" / "law_like.txt",
        FIXTURES / "synthetic" / "multi_approved.docx",
        FIXTURES / "synthetic" / "table.docx",
    ]
)
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"


def snapshot_of(path: Path) -> Path:
    return path.with_name(f"{path.stem}.structure.json")


def tree(parsed: ParsedDocument) -> list[list[str]]:
    return [[f.id, f.kind.value, f.label] for f in parsed.fragments]


def _marks(path: Path) -> list[pytest.MarkDecorator]:
    if path.suffix not in CONVERTED:
        return []
    absent = shutil.which("soffice") is None
    return [pytest.mark.soffice, pytest.mark.skipif(absent, reason="LibreOffice is not installed")]


@pytest.mark.parametrize(
    "path",
    [pytest.param(path, id=path.name, marks=_marks(path)) for path in DOCUMENTS],
)
def test_the_tree_matches_its_snapshot(path: Path) -> None:
    parsed = parse_file(str(path), "doc", LIMITS)
    assert isinstance(parsed, ParsedDocument), f"{path.name}: {parsed}"
    found = tree(parsed)
    snapshot = snapshot_of(path)
    if UPDATE or not snapshot.exists():
        snapshot.write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n", "utf-8")
        if not UPDATE:
            pytest.fail(f"{snapshot.name} written: check it by hand and commit it")
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    assert found == expected, f"{path.name}: the tree differs from {snapshot.name}"


def test_every_snapshot_has_its_document() -> None:
    names = {snapshot_of(path).name for path in DOCUMENTS}
    stale = [p.name for p in (FIXTURES / "corpus").glob("*.structure.json") if p.name not in names]
    assert not stale, f"snapshots of removed documents: {stale}"
