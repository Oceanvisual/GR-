"""T041: malformed hypotheses cannot become valid initiatives or silently complete a run."""

from opportunity_builders import answer, drafts, fragments, model

from gr.domain.enums import AutomationLevel, LegalGate, Level5
from gr.domain.initiative import InitiativeSet
from gr.extraction.schemas import OpportunitiesOut
from gr.validation.postprocess import postprocess


def test_valid_draft_obeys_all_domain_invariants_and_does_not_mutate_inputs() -> None:
    source = drafts()
    before = source.model_dump_json()
    result = postprocess(model(), source, fragments())
    (item,) = result.initiatives
    assert item.id == "ini_proc_1_1" and item.finding_ids == ["fnd_proc_1_1"]
    assert item.tech_guard and not item.tech_guard.requires_justification
    assert item.gate and item.gate.status == LegalGate.ALLOWED
    assert not result.issues and not result.no_change
    assert InitiativeSet.model_validate_json(result.model_dump_json()) == result
    assert before == source.model_dump_json()


def test_a_regeneration_gives_ids_unlike_those_it_replaces() -> None:
    result = postprocess(model(), drafts(), fragments(), version=3)
    (item,) = result.initiatives
    assert item.id == "ini_proc_1_v3_1" and item.finding_ids == ["fnd_proc_1_v3_1"]
    assert [f.id for f in result.findings] == ["fnd_proc_1_v3_1"]


def test_unknown_operations_are_not_exposed_as_valid_initiatives() -> None:
    result = postprocess(model(), drafts(answer("opp_unknown_ids")), fragments())
    assert not result.initiatives and not result.findings
    assert {i.code for i in result.issues} >= {"unknown_operation", "empty_initiative", "no_result"}


def test_no_change_is_explicit_and_requires_a_justification() -> None:
    result = postprocess(model(), drafts(answer("opp_no_change")), fragments())
    assert not result.initiatives and len(result.no_change) == 1 and not result.issues
    result = postprocess(model(), drafts(OpportunitiesOut()), fragments())
    assert any(i.code == "no_result" for i in result.issues)
    assert not result.no_change


def test_missing_reduction_gets_conservative_level_and_an_issue() -> None:
    raw = answer().model_dump(mode="json")
    raw["initiatives"][0]["reduction_potential"] = []
    result = postprocess(model(), drafts(OpportunitiesOut.model_validate(raw)), fragments())
    assert result.initiatives[0].reduction_potential[0].level == Level5.VERY_LOW
    assert any(i.code == "missing_reduction" for i in result.issues)


def test_unknown_provision_is_excluded_without_inventing_a_legal_basis() -> None:
    raw = answer().model_dump(mode="json")
    raw["initiatives"][0].update(
        {
            "regulatory_mode": "requires_change",
            "provisions_to_revise": [
                {
                    "ref": "F999",
                    "change_nature": "Изменение порядка",
                }
            ],
        }
    )
    result = postprocess(model(), drafts(OpportunitiesOut.model_validate(raw)), fragments())
    assert not result.initiatives
    assert {i.code for i in result.issues} >= {"unknown_fragment", "missing_provision"}


def test_regulatory_change_is_linked_and_legal_autonomy_capped() -> None:
    raw = answer().model_dump(mode="json")
    raw["initiatives"][0].update(
        {
            "regulatory_mode": "requires_change",
            "automation_level": "autonomous",
            "provisions_to_revise": [{"ref": "F1", "change_nature": "Дополнить порядок"}],
        }
    )
    process_model = model()
    process_model.processes[0].operations[1] = (
        process_model.processes[0].operations[1].model_copy(update={"legally_significant": True})
    )
    result = postprocess(process_model, drafts(OpportunitiesOut.model_validate(raw)), fragments())
    item = result.initiatives[0]
    assert item.requires_legal_review and item.provisions_to_revise[0].fragment_id == "body/1"
    assert item.automation_level == AutomationLevel.HUMAN_APPROVAL and item.autonomy_capped
    assert item.gate and item.gate.status == LegalGate.REQUIRES_REGULATORY_CHANGE
