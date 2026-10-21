"""Structure of the test regulation (FR-010, T020). Skipped when the fixture is absent."""

import pytest
from structure_helpers import FIXTURES, extract

from gr.domain.enums import FragmentKind
from gr.structure.builder import StructureResult, build_structure

NPA = FIXTURES / "npa"
pytestmark = pytest.mark.skipif(not (NPA / "reg261.docx").exists(), reason="no reg261 fixture")
STRUCTURAL = {
    FragmentKind.PREAMBLE,
    FragmentKind.APPROVED_DOCUMENT,
    FragmentKind.SECTION,
    FragmentKind.POINT,
    FragmentKind.SUBPOINT,
    FragmentKind.APPENDIX,
}


@pytest.fixture(scope="module")
def docx() -> StructureResult:
    return build_structure(extract(NPA / "reg261.docx"), "doc_x")


def _labels(result: StructureResult, kind: FragmentKind) -> list[str]:
    return [f.label for f in result.fragments if f.kind == kind]


def test_order_is_separate_from_the_regulation(docx: StructureResult) -> None:
    top = [f for f in docx.fragments if f.parent_id == "root"]
    assert [f.kind for f in top] == [FragmentKind.PREAMBLE, FragmentKind.APPROVED_DOCUMENT]
    preamble_points = [
        f.label
        for f in docx.fragments
        if f.parent_id == "preamble" and f.kind == FragmentKind.POINT
    ]
    assert preamble_points == ["п. 1 вводной части", "п. 2 вводной части", "п. 3 вводной части"]
    assert "АДМИНИСТРАТИВНЫЙ РЕГЛАМЕНТ" in (top[1].heading or "")


def test_sections(docx: StructureResult) -> None:
    assert _labels(docx, FragmentKind.SECTION) == [
        f"разд. {n}" for n in ("I", "II", "III", "IV", "V", "VI")
    ]


def test_key_points(docx: StructureResult) -> None:
    points = set(_labels(docx, FragmentKind.POINT))
    expected = {"п. 2.6", "п. 2.16", "п. 3.2", "п. 3.5", "п. 3.5.1", "п. 3.8.3"}
    assert expected <= points
    for major in range(3, 9):
        assert f"п. 3.{major}" in points


def test_point_216_has_all_its_subpoints(docx: StructureResult) -> None:
    point = next(f for f in docx.fragments if f.label == "п. 2.16")
    subpoints = [
        f for f in docx.fragments if f.parent_id == point.id and f.kind == FragmentKind.SUBPOINT
    ]
    assert len(subpoints) >= 26
    assert subpoints[0].label == "пп. 1 п. 2.16"


def test_pdf_tree_matches_docx(docx: StructureResult) -> None:
    pdf = build_structure(extract(NPA / "reg261.pdf"), "doc_x")

    def skeleton(result: StructureResult) -> list[str]:
        return [f"{f.kind.value} {f.label}" for f in result.fragments if f.kind in STRUCTURAL]

    assert skeleton(pdf) == skeleton(docx)
    pages = next(f.pages for f in pdf.fragments if f.label == "п. 3.5.1")
    assert pages and all(1 <= p <= 47 for p in pages)
