"""T039: common operations/findings, alternative components and durable expert decisions."""

import pytest
from initiative_builders import initiative

from gr.analysis.relations import ExpertRelation, alternative_components, infer_relations
from gr.domain.enums import InitiativeRelation, SolutionCategory
from gr.domain.initiative import Initiative


def item(key: str, findings: list[str], operations: list[str] | None = None) -> Initiative:
    return initiative(
        SolutionCategory.CLASSIC_AUTOMATION,
        id=key,
        finding_ids=findings,
        operation_ids=operations or ["op1"],
        reduction_potential=[
            {"operation_id": op, "level": "medium", "justification": "x"}
            for op in (operations or ["op1"])
        ],
    )


def test_common_finding_on_a_common_operation_means_alternatives() -> None:
    first, second = infer_relations([item("a", ["f"]), item("b", ["f"])])
    assert first.relations[0].relation == InitiativeRelation.ALTERNATIVE
    assert second.relations[0].initiative_id == first.id
    assert first.relations[0].initiative_id == second.id


def test_different_findings_on_a_common_operation_are_complementary() -> None:
    first, _ = infer_relations([item("a", ["f1"]), item("b", ["f2"])])
    assert first.relations[0].relation == InitiativeRelation.COMPLEMENTARY


def test_shared_finding_without_shared_operation_creates_no_relation() -> None:
    assert all(
        not i.relations
        for i in infer_relations(
            [
                item("a", ["f"], ["op1"]),
                item("b", ["f"], ["op2"]),
            ]
        )
    )


def test_transitive_alternatives_form_one_component_and_output_is_stable() -> None:
    items = [item("c", ["f2"]), item("a", ["f1"]), item("b", ["f1", "f2"]), item("d", ["f3"])]
    before = [i.model_dump_json() for i in items]
    related = infer_relations(items)
    assert alternative_components(related) == [["a", "b", "c"], ["d"]]
    reverse = infer_relations(items[::-1])
    assert {i.id: i.relations for i in related} == {i.id: i.relations for i in reverse}
    assert [i.model_dump_json() for i in items] == before


def test_expert_override_is_symmetric_and_survives_recomputation() -> None:
    items = [item("a", ["f"]), item("b", ["f"])]
    related = infer_relations(
        items,
        overrides=[
            ExpertRelation(
                left_id="b",
                right_id="a",
                relation=InitiativeRelation.COMPLEMENTARY,
            )
        ],
    )
    assert all(i.relations[0].source == "expert" for i in related)
    assert alternative_components(related) == [["a"], ["b"]]
    assert infer_relations(related) == related


def test_new_explicit_expert_decision_can_replace_the_old_one() -> None:
    items = infer_relations(
        [item("a", ["f"]), item("b", ["f"])],
        overrides=[
            ExpertRelation(
                left_id="a",
                right_id="b",
                relation=InitiativeRelation.COMPLEMENTARY,
            )
        ],
    )
    updated = infer_relations(
        items,
        overrides=[
            ExpertRelation(
                left_id="a",
                right_id="b",
                relation=InitiativeRelation.ALTERNATIVE,
            )
        ],
    )
    assert alternative_components(updated) == [["a", "b"]]


def test_unknown_or_conflicting_override_is_rejected() -> None:
    items = [item("a", ["f"]), item("b", ["f"])]
    with pytest.raises(ValueError, match="unknown"):
        infer_relations(
            items,
            overrides=[
                ExpertRelation(
                    left_id="a",
                    right_id="c",
                    relation=InitiativeRelation.ALTERNATIVE,
                )
            ],
        )
    with pytest.raises(ValueError, match="conflicting"):
        infer_relations(
            items,
            overrides=[
                ExpertRelation(left_id="a", right_id="b", relation=InitiativeRelation.ALTERNATIVE),
                ExpertRelation(
                    left_id="b", right_id="a", relation=InitiativeRelation.COMPLEMENTARY
                ),
            ],
        )
