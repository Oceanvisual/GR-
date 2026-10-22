"""Parse invariants on any input (R-22, SC-014): fixtures, the dev corpus and seeded mutations.

For every input the parser either refuses with a known error code or returns a structure in
which own texts rebuild the cleaned text, nothing is lost, offsets nest, ids are unique and
stable, pages exist and requisite citations point at their quotes.
"""

import io
import struct
import unicodedata
import zipfile
from functools import cache
from pathlib import Path

import pytest
from mutations import FIXTURES, mutate_blocks, mutate_bytes, mutate_text_bytes

from gr.domain.document import DocumentMetadata, RawDocument
from gr.errors import ERRORS
from gr.ingestion.base import Extractor, IngestionError
from gr.ingestion.docx import DocxExtractor
from gr.ingestion.pdf import PdfExtractor
from gr.ingestion.txt import TxtExtractor
from gr.services.parsing import ParsedDocument, ParseFailure, ParseLimits, parse_file, parse_raw
from gr.structure.cleanup import clean
from gr.structure.invariants import check_invariants
from gr.structure.tree import normalized_blocks

LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)
EXTRACTORS: dict[str, Extractor] = {
    ".txt": TxtExtractor(),
    ".docx": DocxExtractor(),
    ".pdf": PdfExtractor(),
}
# Synthetic files, the federal regulation and the dev corpus; the holdout set is not read here.
SOURCES = sorted(
    path
    for folder in ("synthetic", "npa", "corpus")
    for path in (FIXTURES / folder).iterdir()
    if path.suffix in EXTRACTORS
)
MUTATIONS = 300
REFUSALS_AFTER_MUTATION = {"empty_document", "document_too_long"}


@cache
def base(name: str) -> RawDocument | None:
    """The extracted fixture, or None for a file the extractor refuses (corrupted.pdf)."""
    path = next(p for p in SOURCES if p.name == name)
    try:
        return EXTRACTORS[path.suffix].extract(path.read_bytes())
    except IngestionError:
        return None


def readable() -> list[str]:
    return [path.name for path in SOURCES if base(path.name) is not None]


def letters_and_digits(text: str) -> list[str]:
    # Decomposed, without combining marks: normalisation may drop an invisible character
    # between a letter and its mark, and NFC then composes them into another letter (г + ◌́ → ѓ).
    return [char for char in unicodedata.normalize("NFD", text) if char.isalnum()]


def in_order(part: list[str], whole: list[str]) -> bool:
    """`part` is `whole` with some characters left out, nothing moved."""
    rest = iter(whole)
    return all(char in rest for char in part)


def assert_sound(raw: RawDocument | None, parsed: ParsedDocument, context: str) -> None:
    fragments, text = parsed.fragments, parsed.text
    if not fragments:
        assert not text, context  # a scan without OCR: nothing parsed, nothing claimed
        return
    problems = check_invariants(fragments, text)
    assert not problems, f"{context}: {problems[:5]}"
    if raw is not None:
        # Nothing lost: normalisation drops only invisible marks, control characters and extra
        # whitespace; cleaning takes out service lines and editorial notes (T067), and every
        # note lands on a fragment. Every other letter and digit is in the text, in order.
        cleaned = clean(normalized_blocks(raw))
        expected = [c for block in cleaned.blocks for c in letters_and_digits(block.text)]
        assert letters_and_digits(text) == expected, f"{context}: text lost or altered"
        placed = sorted(note for fragment in fragments for note in fragment.notes)
        assert placed == sorted(n.text for n in cleaned.notes), f"{context}: a note is lost"
        source = [c for block in raw.blocks for c in letters_and_digits(block.text)]
        taken = [c for part in [*placed, *cleaned.dropped] for c in letters_and_digits(part)]
        assert sorted(source) == sorted(expected + taken), f"{context}: text lost or invented"
        assert in_order(expected, source), f"{context}: text reordered"
    for fragment in fragments:
        assert fragment.label.strip(), f"{context}: {fragment.id} has no label"
        assert fragment.id.strip() == fragment.id and "//" not in fragment.id, context
        if parsed.page_count:
            assert all(1 <= page <= parsed.page_count for page in fragment.pages), context
    ids = {fragment.id for fragment in fragments}
    for name in DocumentMetadata.model_fields:
        citation = getattr(parsed.metadata, name).citation
        if citation is None:
            continue
        assert citation.fragment_id in ids, f"{context}: {name} cites a missing fragment"
        grounding = citation.grounding
        if grounding.matched_start is not None:
            quoted = text[grounding.matched_start : grounding.matched_end]
            assert quoted == citation.quote, f"{context}: {name} quote is not at its offsets"


def parse_or_refuse(raw: RawDocument, context: str) -> ParsedDocument | None:
    try:
        return parse_raw(raw, "doc_TEST", LIMITS)
    except IngestionError as error:
        assert error.code in REFUSALS_AFTER_MUTATION, f"{context}: {error.code}"
        return None


# --- fixtures and corpus as they are ---------------------------------------------------------


@pytest.mark.parametrize("name", [path.name for path in SOURCES])
def test_fixture_satisfies_invariants(name: str) -> None:
    raw = base(name)
    if raw is None:
        pytest.skip("the extractor refuses this file by design")
    parsed = parse_or_refuse(raw, name)
    assert parsed is not None
    assert_sound(raw, parsed, name)


# --- seeded block mutations: every format through one representation -------------------------


@pytest.mark.parametrize("seed", range(MUTATIONS))
def test_mutated_structure_satisfies_invariants(seed: int) -> None:
    names = readable()
    name = names[seed % len(names)]
    raw, applied = mutate_blocks(base(name), seed)  # type: ignore[arg-type]
    context = f"seed={seed} base={name} mutations={applied}"
    parsed = parse_or_refuse(raw, context)
    if parsed is None:
        return
    assert_sound(raw, parsed, context)
    if seed % 10 == 0:
        # Stable ids and labels: the same input gives the same tree.
        again = parse_or_refuse(raw, context)
        assert again is not None and again.fragments == parsed.fragments, context


# --- damaged files: refused with a known code, or parsed soundly ------------------------------


def _check_file(tmp_path: Path, data: bytes, context: str) -> None:
    path = tmp_path / "upload"
    path.write_bytes(data)
    result = parse_file(str(path), "doc_TEST", LIMITS)
    if isinstance(result, ParseFailure):
        assert result.code in ERRORS and result.code != "internal_error", context
        assert any("а" <= char <= "я" for char in result.message.lower()), context
        return
    assert_sound(None, result, context)


@pytest.mark.parametrize("seed", range(60))
def test_damaged_text_files(tmp_path: Path, seed: int) -> None:
    sources = [p for p in SOURCES if p.suffix == ".txt"]
    source = sources[seed % len(sources)]
    raw = source.read_bytes()
    text = raw.decode("cp1251") if "cp1251" in source.name else raw.decode()
    data, kind = mutate_text_bytes(text, seed)
    _check_file(tmp_path, data, f"seed={seed} base={source.name} kind={kind}")


# The seeds include the ones that once broke the extractors: DOCX 85, 90, 97, 108 (corrupt
# deflate stream), PDF 50 (unloadable page) and 51 (font size below zero).
DAMAGED_SEEDS = {".docx": range(120), ".pdf": range(60)}


DAMAGED_CASES = [(suffix, seed) for suffix, seeds in DAMAGED_SEEDS.items() for seed in seeds]


@pytest.mark.parametrize(("suffix", "seed"), DAMAGED_CASES)
def test_damaged_binary_files(tmp_path: Path, suffix: str, seed: int) -> None:
    sources = [p for p in SOURCES if p.suffix == suffix]
    source = sources[seed % len(sources)]
    data, kind = mutate_bytes(source.read_bytes(), seed)
    _check_file(tmp_path, data, f"seed={seed} base={source.name} kind={kind}")


# --- regressions found by the mutations -------------------------------------------------------


def test_invisible_characters_only_are_an_empty_document() -> None:
    raw = TxtExtractor().extract("​﻿⁠\n­".encode())
    with pytest.raises(IngestionError) as error:
        parse_raw(raw, "doc_TEST", LIMITS)
    assert error.value.code == "empty_document"


def test_docx_with_corrupt_deflate_stream_is_a_corrupted_file() -> None:
    body = "".join(f"<w:p><w:r><w:t>Пункт {i}. Текст пункта.</w:t></w:r></w:p>" for i in range(500))
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    data = bytearray(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("word/document.xml")
    header = info.header_offset
    name_length, extra_length = struct.unpack("<HH", data[header + 26 : header + 30])
    start = header + 30 + name_length + extra_length
    data[start] = 0xFF  # deflate block type 11 is reserved: zlib.error, not a CRC mismatch
    with pytest.raises(IngestionError) as error:
        DocxExtractor().extract(bytes(data))
    assert error.value.code == "corrupted_file"
