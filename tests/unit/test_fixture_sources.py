"""Every real fixture listed in a SOURCES.md is present and matches its recorded sha256."""

import hashlib
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROW_RE = re.compile(r"^\| ([^|\s]+\.\w+) \|.*\| ([0-9a-f]{64}) \|$", re.M)


def _rows() -> list[tuple[Path, str]]:
    rows: list[tuple[Path, str]] = []
    for sources in sorted(FIXTURES.glob("*/SOURCES.md")):
        text = sources.read_text(encoding="utf-8")
        rows += [(sources.parent / name, digest) for name, digest in ROW_RE.findall(text)]
    return rows


def test_reg261_is_recorded() -> None:
    names = {path.name for path, _ in _rows()}
    assert {"reg261.docx", "reg261.pdf"} <= names


@pytest.mark.parametrize(("path", "digest"), _rows(), ids=lambda v: getattr(v, "name", "sha"))
def test_fixture_matches_sha256(path: Path, digest: str) -> None:
    assert path.exists(), f"{path} is listed in SOURCES.md but missing"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
