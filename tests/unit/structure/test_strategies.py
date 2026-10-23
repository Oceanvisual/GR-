"""Competing structure strategies and the choice between them (FR-018, R-22, T078)."""

from pathlib import Path

import pytest
from structure_helpers import FIXTURES, extract

from gr.domain.document import Block, IngestionDiagnostics, RawDocument
from gr.domain.enums import DocFormat, FragmentKind, StructureMode, TextLayerStatus
from gr.domain.ir import full_text
from gr.structure.builder import build_structure
from gr.structure.invariants import check_invariants
from gr.structure.scoring import MIN_CONFIDENCE, PARAGRAPHS_SCORE, choose
from gr.structure.strategies import STRATEGIES, Strategy, StrategyOutcome
from gr.structure.tree import Unit, build_units, flatten, normalized_blocks

SYNTHETIC = FIXTURES / "synthetic"
FIXTURE_FILES = sorted(
    p
    for p in FIXTURES.glob("*/*")
    if p.suffix in (".txt", ".docx", ".pdf") and "scan" not in p.name and "corrupted" not in p.name
)


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_strategy_keeps_invariants(path: Path, name: str) -> None:
    blocks = normalized_blocks(extract(path))
    ir = full_text(blocks)
    outcome = STRATEGIES[name](build_units(blocks, ir))
    fragments = flatten(outcome.root, "d", ir)
    assert check_invariants(fragments, ir.text) == []


def test_numbered_regulation_wins_confidently() -> None:
    result = build_structure(extract(SYNTHETIC / "regulation.txt"), "d")
    assert result.choice.strategy == "regulation"
    assert result.choice.confidence >= 0.8
    assert set(result.choice.scores) == set(STRATEGIES)
    assert result.choice.failed_strategies == {}


def test_styled_headings_win_with_layout() -> None:
    result = build_structure(extract(SYNTHETIC / "styled_headings.docx"), "d")
    assert result.choice.strategy == "layout"
    assert result.choice.confidence >= MIN_CONFIDENCE
    headings = [f.heading for f in result.fragments if f.kind == FragmentKind.SECTION]
    assert headings == [
        "ПОЛОЖЕНИЕ О ДОПУСКЕ ПОСЕТИТЕЛЕЙ",
        "Общие положения",
        "Допуск посетителей",
        "Ответственность",
    ]
    subsections = [f.label for f in result.fragments if f.kind == FragmentKind.SUBSECTION]
    assert subsections == [
        "подразд. «Оформление разового пропуска»",
        "подразд. «Сопровождение посетителя»",
    ]
    assert result.mode == StructureMode.NUMBERED


def test_unnumbered_text_falls_back_with_low_confidence() -> None:
    result = build_structure(extract(SYNTHETIC / "no_numbering.txt"), "d")
    assert result.choice.strategy == "paragraphs"
    assert result.choice.confidence == PARAGRAPHS_SCORE < MIN_CONFIDENCE
    assert result.mode == StructureMode.PARAGRAPHS and result.warnings


def test_real_documents_choose_numbering() -> None:
    for name in ("npa/reg261.docx", "npa/reg261.pdf", "corpus/fz_59_2006.docx"):
        assert build_structure(extract(FIXTURES / name), "d").choice.strategy == "regulation"


def _broken(_units: list[Unit]) -> StrategyOutcome:
    raise RuntimeError("strategy bug")


def test_failing_strategy_drops_out() -> None:
    strategies: dict[str, Strategy] = {**STRATEGIES, "regulation": _broken}
    result = build_structure(extract(SYNTHETIC / "regulation.txt"), "d", strategies)
    assert result.choice.failed_strategies == {"regulation": "RuntimeError: strategy bug"}
    assert result.choice.strategy in ("layout", "paragraphs")
    assert check_invariants(result.fragments, result.text.text) == []


def test_all_failing_strategies_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="every structure strategy failed"):
        choose([], {"a": _broken})


def test_low_confidence_structure_is_flagged() -> None:
    texts = [
        "1. Первый.",
        "2. Второй.",
        "3. Третий.",
        "9. Мимо.",
        "15. Мимо.",
        "40. Мимо.",
        "77. Мимо.",
    ]
    document = RawDocument(
        format=DocFormat.TXT,
        blocks=[Block(text=t) for t in texts],
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
    )
    result = build_structure(document, "d")
    assert result.choice.strategy == "regulation"
    assert PARAGRAPHS_SCORE < result.choice.confidence < MIN_CONFIDENCE
    assert any("неуверенно" in w for w in result.warnings)
