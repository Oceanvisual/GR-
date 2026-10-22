"""T037: simple rules stay simple; mixed findings and catalogue overrides remain explicit."""

from pathlib import Path

import pytest
from initiative_builders import finding, initiative
from pydantic import ValidationError

from gr.analysis.tech_guard import ProblemCatalog, evaluate, load_catalog
from gr.domain.enums import KnowledgeLayer, OperationType, ProblemPattern, SolutionCategory
from gr.domain.process import Grounded, Operation


def test_catalog_covers_the_closed_problem_enum() -> None:
    assert set(load_catalog().patterns) == set(ProblemPattern)


@pytest.mark.parametrize(
    "pattern", [ProblemPattern.DUPLICATE_CHECKS, ProblemPattern.LOOKUP_AND_RECONCILIATION]
)
@pytest.mark.parametrize(
    "category", [SolutionCategory.LLM_RAG, SolutionCategory.AI_AGENT, SolutionCategory.HYBRID]
)
def test_complex_solution_for_simple_checks_requires_justification(
    pattern: ProblemPattern,
    category: SolutionCategory,
) -> None:
    result = evaluate(initiative(category), [finding(pattern)])
    assert result.min_category == SolutionCategory.CLASSIC_AUTOMATION
    assert result.requires_justification


@pytest.mark.parametrize("pattern", list(ProblemPattern))
def test_classic_automation_is_not_flagged_as_too_complex(pattern: ProblemPattern) -> None:
    assert not evaluate(
        initiative(SolutionCategory.CLASSIC_AUTOMATION), [finding(pattern)]
    ).requires_justification


def test_unrelated_finding_does_not_raise_the_minimum() -> None:
    findings = [
        finding(ProblemPattern.DUPLICATE_CHECKS),
        finding(ProblemPattern.LARGE_TEXT_ANALYSIS, "other"),
    ]
    assert evaluate(initiative(SolutionCategory.AI_NLP), findings).requires_justification


def test_mixed_findings_take_the_highest_required_class_without_mutation() -> None:
    item = initiative(SolutionCategory.AI_NLP, finding_ids=["f1", "f2"])
    before = item.model_dump_json()
    findings = [
        finding(ProblemPattern.DUPLICATE_CHECKS),
        finding(ProblemPattern.LARGE_TEXT_ANALYSIS, "f2"),
    ]
    guard = evaluate(item, findings)
    assert guard.min_category == SolutionCategory.AI_NLP and not guard.requires_justification
    assert item.model_dump_json() == before
    assert evaluate(item, findings[::-1]) == guard


@pytest.mark.parametrize("minimum", ["classic_automation", "ai_nlp"])
def test_completeness_check_uses_operation_type_rule(minimum: str) -> None:
    catalog = load_catalog().model_dump(mode="json")
    catalog["operation_minima"]["completeness_check"] = minimum
    operation = Operation(
        id="op1",
        seq=1,
        name="Проверка",
        operation_type=OperationType.COMPLETENESS_CHECK,
        action=Grounded[str](value="Проверка", layer=KnowledgeLayer.INTERPRETATION),
    )
    result = evaluate(
        initiative(SolutionCategory.LLM_RAG),
        [finding(ProblemPattern.DUPLICATE_CHECKS)],
        operations=[operation],
        catalog=ProblemCatalog.model_validate(catalog),
    )
    assert result.min_category.value == minimum and result.requires_justification


def test_no_change_needs_no_technology() -> None:
    result = evaluate(initiative(SolutionCategory.NO_CHANGE, finding_ids=[]), [])
    assert result.min_category == SolutionCategory.NO_CHANGE and not result.requires_justification


@pytest.mark.parametrize("mode", ["missing", "duplicate", "other_process"])
def test_invalid_finding_references_are_not_silently_evaluated(mode: str) -> None:
    f = finding(ProblemPattern.DUPLICATE_CHECKS)
    findings = {
        "missing": [],
        "duplicate": [f, f],
        "other_process": [finding(f.pattern, process="other")],
    }[mode]
    with pytest.raises(ValueError):
        evaluate(initiative(SolutionCategory.LLM_RAG), findings)


def test_configurable_minimum_is_used_and_missing_pattern_rejected(tmp_path: Path) -> None:
    text = Path("src/gr/config/problem_catalog.toml").read_text()
    path = tmp_path / "catalog.toml"
    path.write_text(text.replace('min_category = "classic_automation"', 'min_category = "ai_nlp"'))
    result = evaluate(
        initiative(SolutionCategory.AI_NLP),
        [finding(ProblemPattern.DUPLICATE_CHECKS)],
        catalog=load_catalog(path),
    )
    assert not result.requires_justification
    data = load_catalog().model_dump(mode="json")
    del data["patterns"]["other"]
    with pytest.raises(ValidationError, match="missing problem patterns"):
        ProblemCatalog.model_validate(data)
