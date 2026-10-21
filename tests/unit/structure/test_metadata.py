"""Document requisites with exact citations (FR-006, R-07, T021)."""

from pathlib import Path

import pytest
from structure_helpers import FIXTURES, extract

from gr.domain.document import Block, DocumentMetadata, IngestionDiagnostics, RawDocument
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.structure.builder import build_structure
from gr.structure.metadata import extract_metadata, normalize_date

FIELDS = list(DocumentMetadata.model_fields)


def metadata_of(path: Path) -> tuple[DocumentMetadata, str, dict[str, tuple[int, int]]]:
    result = build_structure(extract(path), "d")
    spans = {f.id: (f.start, f.end) for f in result.fragments}
    return extract_metadata(result.fragments, result.text.text), result.text.text, spans


def values(metadata: DocumentMetadata) -> dict[str, str | None]:
    return {name: getattr(metadata, name).value for name in FIELDS}


@pytest.mark.parametrize("name", ["reg261.docx", "reg261.pdf"])
def test_reg261_requisites(name: str) -> None:
    metadata, _, _ = metadata_of(FIXTURES / "npa" / name)
    found = values(metadata)
    assert found["act_type"] == "Приказ"
    assert found["issuer"] == "ФЕДЕРАЛЬНОЕ АГЕНТСТВО ПО УПРАВЛЕНИЮ ГОСУДАРСТВЕННЫМ ИМУЩЕСТВОМ"
    assert (found["date"], found["number"]) == ("11.09.2020", "261")
    assert found["edition_date"] == "30.05.2023"
    assert found["minjust_registration"] == "14.09.2020 № 59820"
    assert (found["title"] or "").startswith("ОБ УТВЕРЖДЕНИИ АДМИНИСТРАТИВНОГО РЕГЛАМЕНТА")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "corpus/fz_59_2006.docx",
            {"act_type": "Федеральный закон", "date": "02.05.2006", "number": "59-ФЗ",
             "edition_date": "28.12.2024", "minjust_registration": None},
        ),
        (
            "corpus/lenoblast_93oz_2003.pdf",
            {"act_type": "Областной закон", "issuer": "ЛЕНИНГРАДСКАЯ ОБЛАСТЬ",
             "date": "13.11.2003", "number": "93-оз", "edition_date": "27.11.2024"},
        ),
        (
            "synthetic/regulation.txt",
            {"act_type": "Приказ", "issuer": "УПРАВЛЕНИЕ ОХРАНЫ ОБЪЕКТОВ СЕВЕРНОЙ ОБЛАСТИ",
             "date": "15.01.2024", "number": "15", "edition_date": None},
        ),
        (
            "synthetic/law_like.txt",
            {"act_type": "Закон", "issuer": "СЕВЕРНОЙ ОБЛАСТИ",
             "title": "О порядке доступа на охраняемые территории"},
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)  # fmt: skip
def test_other_documents(path: str, expected: dict[str, str | None]) -> None:
    found = values(metadata_of(FIXTURES / path)[0])
    assert {key: found[key] for key in expected} == expected


def test_document_without_requisites_gives_nothing() -> None:
    metadata, _, _ = metadata_of(FIXTURES / "synthetic" / "no_numbering.txt")
    assert all(getattr(metadata, name).value is None for name in FIELDS)
    assert all(getattr(metadata, name).citation is None for name in FIELDS)


def test_dates_of_referenced_acts_are_not_taken() -> None:
    blocks = [
        "ОБЛАСТНОЙ ЗАКОН",
        "О защите территорий",
        "Статья 1. Правовое регулирование",
        "1. Законодательство состоит из Федерального закона от 21.12.1994 N 68-ФЗ.",
    ]
    document = RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text=t) for t in blocks],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    result = build_structure(document, "d")
    metadata = extract_metadata(result.fragments, result.text.text)
    assert metadata.date.value is None and metadata.number.value is None


@pytest.mark.parametrize(
    "path",
    [
        "npa/reg261.docx",
        "npa/reg261.pdf",
        "corpus/fz_59_2006.docx",
        "corpus/lenoblast_93oz_2003.pdf",
    ],
)
def test_citations_are_exact_and_inside_their_fragment(path: str) -> None:
    metadata, text, spans = metadata_of(FIXTURES / path)
    for name in FIELDS:
        field = getattr(metadata, name)
        if field.value is None:
            continue
        citation = field.citation
        assert citation is not None and field.source == "document"
        grounding = citation.grounding
        assert grounding.matched_start is not None and grounding.matched_end is not None
        assert text[grounding.matched_start : grounding.matched_end] == citation.quote
        start, end = spans[citation.fragment_id]
        assert start <= grounding.matched_start < grounding.matched_end <= end
        assert grounding.status.passes


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("11.09.2020", "11.09.2020"), ("1.3.2024", "01.03.2024"), ("11 сентября 2020", "11.09.2020")],
)
def test_normalize_date(raw: str, normalized: str) -> None:
    assert normalize_date(raw) == normalized
