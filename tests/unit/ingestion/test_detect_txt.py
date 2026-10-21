"""Format detection by signature and the TXT extractor (FR-001, FR-002, FR-004, T015)."""

import io
import zipfile
from pathlib import Path

import pytest

from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError
from gr.ingestion.detect import detect_format
from gr.ingestion.registry import ExtractorRegistry
from gr.ingestion.txt import TxtExtractor, decode_text, split_paragraphs

SYNTHETIC = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"
NPA = Path(__file__).resolve().parents[2] / "fixtures" / "npa"
SAMPLE = "1. Заявление подаётся в течение 10 рабочих дней.\n2. Срок рассмотрения - 5 дней.\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _code(data: bytes) -> str:
    with pytest.raises(IngestionError) as exc:
        detect_format(data)
    return exc.value.code


# --- detection -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (SYNTHETIC / "regulation.pdf", DocFormat.PDF),
        (SYNTHETIC / "scan.pdf", DocFormat.PDF),
        (SYNTHETIC / "regulation_manual.docx", DocFormat.DOCX),
        (SYNTHETIC / "regulation.txt", DocFormat.TXT),
        (SYNTHETIC / "regulation_cp1251.txt", DocFormat.TXT),
        (SYNTHETIC / "regulation.html", DocFormat.HTML),
        (NPA / "reg261.docx", DocFormat.DOCX),
        (NPA / "reg261.pdf", DocFormat.PDF),
    ],
    ids=lambda v: v.name if isinstance(v, Path) else v.value,
)
def test_fixtures_detected(path: Path, expected: DocFormat) -> None:
    assert detect_format(path.read_bytes()) == expected


def test_signature_wins_over_extension() -> None:
    xlsx = _zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<w/>"})
    with pytest.raises(IngestionError) as exc:
        detect_format(xlsx)  # e.g. uploaded as report.txt
    assert exc.value.code == "unsupported_format"
    assert exc.value.details["detected"] == "XLSX"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + "WordDocument".encode("utf-16-le"),
            DocFormat.DOC,
        ),
        (b"{\\rtf1\\ansi text}", DocFormat.RTF),
        (
            _zip({"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": b"<x/>"}),
            DocFormat.ODT,
        ),
        (b"\xef\xbb\xbf<!DOCTYPE html><html><body>1.</body></html>", DocFormat.HTML),
        (b"<!-- saved --> <html lang=ru>", DocFormat.HTML),
        ("Текст про <html> в середине".encode(), DocFormat.TXT),
        ("Текст".encode("utf-16"), DocFormat.TXT),
        (b"garbage before %PDF-1.7 within the first kilobyte", DocFormat.PDF),
    ],
)
def test_signatures(data: bytes, expected: DocFormat) -> None:
    assert detect_format(data) == expected


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_document"),
        (b"  \n\t ", "empty_document"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "unsupported_format"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "unsupported_format"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "unsupported_format"),
        (_zip({"a.txt": b"x"}), "unsupported_format"),
        (b"PK\x03\x04 broken zip", "corrupted_file"),
        (bytes(range(256)) * 4, "unsupported_format"),
    ],
)
def test_rejected(data: bytes, code: str) -> None:
    assert _code(data) == code


# --- registry ----------------------------------------------------------------------------


def test_registry_reports_supported_formats() -> None:
    registry = ExtractorRegistry([TxtExtractor()])
    assert isinstance(registry.get(DocFormat.TXT), TxtExtractor)
    with pytest.raises(IngestionError) as exc:
        registry.get(DocFormat.RTF)
    assert exc.value.code == "unsupported_format"
    assert exc.value.details["supported"] == ["txt"]
    with pytest.raises(ValueError, match="already"):
        registry.register(TxtExtractor())


# --- TXT ---------------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1251", "koi8-r", "cp866", "utf-16"])
def test_encodings_decoded(encoding: str) -> None:
    text, _detected = decode_text(SAMPLE.encode(encoding))
    assert text.replace("\r\n", "\n") == SAMPLE


def test_short_cp1251_is_not_read_as_cjk() -> None:
    text, encoding = decode_text("Срок 5 дней".encode("cp1251"))
    assert text == "Срок 5 дней" and encoding == "cp1251"


def test_cp1251_fixture_matches_utf8() -> None:
    utf8 = TxtExtractor().extract((SYNTHETIC / "regulation.txt").read_bytes())
    cp1251 = TxtExtractor().extract((SYNTHETIC / "regulation_cp1251.txt").read_bytes())
    assert [b.text for b in cp1251.blocks] == [b.text for b in utf8.blocks]
    assert cp1251.diagnostics.encoding == "cp1251"
    assert cp1251.diagnostics.analyzable


def test_undetectable_encoding() -> None:
    noise = bytes(b for b in range(0x80, 0x100) if b not in (0x98,)) * 20
    with pytest.raises(IngestionError) as exc:
        TxtExtractor().extract(noise)
    assert exc.value.code == "encoding_undetected"


def test_empty_text_document() -> None:
    with pytest.raises(IngestionError) as exc:
        TxtExtractor().extract("﻿ \n\n".encode())
    assert exc.value.code == "empty_document"


def test_line_per_paragraph() -> None:
    raw = TxtExtractor().extract("Заголовок\r\n\r\n1. Пункт.\r\n2. Пункт.\r\n".encode())
    assert [b.text for b in raw.blocks] == ["Заголовок", "1. Пункт.", "2. Пункт."]
    assert raw.format == DocFormat.TXT and all(b.page is None for b in raw.blocks)


def test_hard_wrapped_paragraphs_are_joined() -> None:
    text = (
        "1. Заявление подаётся в течение десяти рабочих\n"
        "дней со дня возникновения права на получе-\n"
        "ние услуги.\n\n"
        "2. Срок рассмотрения заявления не может превышать\n"
        "пять рабочих дней.\n\n"
        "3. Решение направляется заявителю по почте\n"
        "или в электронной форме.\n"
    )
    assert split_paragraphs(text) == [
        "1. Заявление подаётся в течение десяти рабочих дней со дня возникновения права на "
        "получение услуги.",
        "2. Срок рассмотрения заявления не может превышать пять рабочих дней.",
        "3. Решение направляется заявителю по почте или в электронной форме.",
    ]


def test_long_lines_with_blank_separators_are_not_joined() -> None:
    text = "Раздел I\n\n" + "\n".join(f"{i}. " + "очень длинный пункт " * 8 for i in range(1, 4))
    assert len(split_paragraphs(text)) == 4
