""".doc, .rtf and .odt through LibreOffice (FR-001, R-04, T075).

Without LibreOffice the conversion is checked through a stand-in `soffice`; the tests of the real
one carry the `soffice` marker and are skipped where it is not installed (the Docker image and
CI have it).
"""

import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError
from gr.ingestion.convert import ConvertingExtractor
from gr.ingestion.detect import detect_format
from gr.ingestion.docx import DocxExtractor
from gr.services.parsing import ParsedDocument, ParseLimits, parse_file
from gr.structure.tree import normalized_blocks

SYNTHETIC = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"
SAMPLES = {DocFormat.DOC: "sample.doc", DocFormat.RTF: "sample.rtf", DocFormat.ODT: "sample.odt"}
needs_soffice = pytest.mark.skipif(shutil.which("soffice") is None, reason="no LibreOffice")


def stand_in(tmp_path: Path, body: str) -> str:
    """A `soffice` that runs `body` with the arguments it was given in `args`."""
    script = tmp_path / "soffice"
    script.write_text(
        f"#!{sys.executable}\nimport os, shutil, subprocess, sys, time\nargs = sys.argv[1:]\n"
        f"out = args[args.index('--outdir') + 1]\n{body}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def texts(raw_blocks: list[str]) -> list[str]:
    return [text for text in raw_blocks if text]


def test_the_converted_docx_is_read_and_keeps_the_original_format(tmp_path: Path) -> None:
    docx = SYNTHETIC / "regulation_manual.docx"
    body = (
        "assert '--headless' in args and any(a.startswith('-env:UserInstallation=file:') "
        "for a in args)\n"
        "os.makedirs(out, exist_ok=True)\n"
        f"shutil.copy({str(docx)!r}, os.path.join(out, 'source.docx'))"
    )
    extractor = ConvertingExtractor(DocFormat.DOC, DocxExtractor(), binary=stand_in(tmp_path, body))
    raw = extractor.extract(b"any bytes of a .doc")
    expected = DocxExtractor().extract(docx.read_bytes())
    assert raw.format == DocFormat.DOC
    assert [b.text for b in raw.blocks] == [b.text for b in expected.blocks]


def test_a_failed_conversion_is_a_corrupted_file(tmp_path: Path) -> None:
    binary = stand_in(tmp_path, "sys.stderr.write('source file could not be loaded'); sys.exit(1)")
    extractor = ConvertingExtractor(DocFormat.RTF, DocxExtractor(), binary=binary)
    with pytest.raises(IngestionError) as error:
        extractor.extract(b"{\\rtf1 broken")
    assert error.value.code == "corrupted_file" and "RTF" in error.value.message_ru


def test_a_hung_conversion_is_killed_with_its_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    body = (
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)"
    )
    extractor = ConvertingExtractor(
        DocFormat.ODT, DocxExtractor(), binary=stand_in(tmp_path, body), timeout_s=2
    )
    started = time.monotonic()
    with pytest.raises(IngestionError) as error:
        extractor.extract(b"PK odt")
    assert time.monotonic() - started < 20
    assert error.value.code == "corrupted_file" and error.value.details["reason"] == (
        "conversion_timeout"
    )
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)  # soffice.bin, so to speak, did not outlive the timeout


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a dead parent may linger as a zombie until it is reaped.
    status = Path(f"/proc/{pid}/status")
    return not (status.exists() and "zombie" in status.read_text())


def test_without_libreoffice_the_user_is_asked_for_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(IngestionError) as error:
        ConvertingExtractor(DocFormat.DOC, DocxExtractor()).extract(b"doc")
    assert error.value.code == "unsupported_format"
    assert "LibreOffice" in error.value.message_ru and "DOCX" in error.value.message_ru


@pytest.mark.parametrize(("fmt", "name"), SAMPLES.items())
def test_the_samples_are_detected_by_their_signature(fmt: DocFormat, name: str) -> None:
    assert detect_format((SYNTHETIC / name).read_bytes()) == fmt


# --- the real LibreOffice (marker `soffice`) ----------------------------------------------


@pytest.mark.soffice
@needs_soffice
@pytest.mark.parametrize(("fmt", "name"), SAMPLES.items())
def test_a_sample_gives_the_text_of_its_docx(fmt: DocFormat, name: str) -> None:
    raw = ConvertingExtractor(fmt, DocxExtractor()).extract((SYNTHETIC / name).read_bytes())
    source = DocxExtractor().extract((SYNTHETIC / "regulation_manual.docx").read_bytes())
    assert raw.format == fmt
    assert texts([b.text for b in normalized_blocks(raw)]) == texts(
        [b.text for b in normalized_blocks(source)]
    )


@pytest.mark.soffice
@needs_soffice
def test_a_sample_is_parsed_like_any_document() -> None:
    limits = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)
    parsed = parse_file(str(SYNTHETIC / "sample.odt"), "d", limits)
    assert isinstance(parsed, ParsedDocument) and parsed.format == DocFormat.ODT
    assert parsed.diagnostics.structure is not None
    assert parsed.diagnostics.structure.strategy == "regulation"
