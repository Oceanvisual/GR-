"""Intermediate representation: blocks, full text and offset maps (FR-017, T077)."""

import random

import pytest
from pydantic import ValidationError

from gr.domain.document import Block, IngestionDiagnostics, RawDocument, TableCell
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.ingestion.base import IngestionError
from gr.ingestion.ir import full_text


def test_block_validation() -> None:
    with pytest.raises(ValidationError):
        Block(text="a", page=0)
    with pytest.raises(ValidationError, match="bbox"):
        Block(text="a", bbox=(5, 5, 1, 10))
    with pytest.raises(ValidationError):
        Block(text="a", align="middle")  # type: ignore[arg-type]


def test_full_text_joins_blocks_and_maps_offsets() -> None:
    blocks = [Block(text="1. Первый пункт."), Block(text="  "), Block(text="2. Второй.")]
    ir = full_text(blocks)
    assert ir.text == "1. Первый пункт.\n2. Второй."
    assert [(s.block, s.start, s.end) for s in ir.spans] == [(0, 0, 16), (2, 17, 27)]
    for span in ir.spans:
        assert ir.text[span.start : span.end] == blocks[span.block].text
    assert ir.block_at(0) == 0
    assert ir.block_at(16) == 0  # the separator belongs to the block before it
    assert ir.block_at(17) == 2
    assert ir.page_starts == ()
    assert ir.page_at(3) is None


def test_table_row_cells_share_a_line() -> None:
    def cell(text: str, row: int, col: int) -> Block:
        return Block(text=text, table=TableCell(table=0, row=row, col=col))

    ir = full_text([Block(text="Перед"), cell("A", 0, 0), cell("B", 0, 1), cell("C", 1, 0)])
    assert ir.text == "Перед\nA | B\nC"


def test_pages_map() -> None:
    blocks = [
        Block(text="стр1", page=1),
        Block(text="стр1б", page=1),
        Block(text="стр3", page=3),  # page 2 has no text
    ]
    ir = full_text(blocks)
    assert ir.page_starts == (0, 11, 11)
    assert [ir.page_at(o) for o in (0, 10, 11, 14)] == [1, 1, 3, 3]
    assert ir.pages_between(0, len(ir.text)) == [1, 2, 3]
    assert ir.pages_between(11, 13) == [3]


def test_raw_document_accepted() -> None:
    raw = RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text="текст")],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    assert full_text(raw).text == "текст"


def test_full_text_deterministic_and_every_offset_has_a_block() -> None:
    rng = random.Random(7)
    words = ["пункт", "срок", "3.5.1", "заявление", "а)", "—", "№ 15"]
    for _ in range(200):
        blocks = [
            Block(
                text=" ".join(rng.choices(words, k=rng.randint(0, 6))),
                page=rng.randint(1, 4) if rng.random() < 0.5 else None,
            )
            for _ in range(rng.randint(1, 12))
        ]
        first, second = full_text(blocks), full_text(list(blocks))
        assert first == second
        for offset in range(len(first.text)):
            index = first.block_at(offset)
            assert 0 <= index < len(blocks) and blocks[index].text.strip()
        for span in first.spans:
            assert first.text[span.start : span.end] == blocks[span.block].text


def test_offset_outside_text() -> None:
    ir = full_text([Block(text="abc")])
    with pytest.raises(IndexError):
        ir.block_at(3)
    with pytest.raises(IndexError):
        full_text([]).block_at(0)


def test_ingestion_error_codes_and_pickling() -> None:
    import pickle

    error = IngestionError("encrypted_file", "Файл защищён паролем.", {"pages": 3})
    restored = pickle.loads(pickle.dumps(error))
    assert (restored.code, restored.details) == ("encrypted_file", {"pages": 3})
    with pytest.raises(ValueError, match="unknown"):
        IngestionError("teapot", "?")


def test_ingestion_codes_exist_in_api_table() -> None:
    from gr.errors import ERRORS
    from gr.ingestion.base import INGESTION_CODES

    assert ERRORS.keys() >= INGESTION_CODES
