"""S5 validation of LLM answers into the process model (FR-024…FR-028, T031)."""

from validation_builders import detail, discovered, fact, fid, fragments, op, run

from gr.domain.enums import Confidence, DeadlineUnit, KnowledgeLayer, OperationType
from gr.domain.process import ValidationIssue
from gr.extraction.outline import build_outline
from gr.extraction.render import FragmentAliases

REGISTER = "принимает заявление и регистрирует его в системе электронного документооборота"
CHECK = "проверяет комплектность документов, указанных в пункте 2.2"
RETURN = "документы возвращаются заявителю в течение двух рабочих дней"


def codes(issues: list[ValidationIssue]) -> list[str]:
    return [issue.code for issue in issues]


def test_verified_answer_becomes_the_model_with_stable_ids() -> None:
    answer = detail(
        [
            op("o2", "п. 3.2.2", CHECK, depends_on=["o1"], operation_type="completeness_check"),
            op(
                "o1",
                "п. 3.2.1",
                REGISTER,
                performer=[fact("п. 3.2.1", "Специалист отдела делопроизводства",
                                "специалист отдела делопроизводства")],
                deadline=[{"raw": fact("п. 3.2.1", "в течение одного рабочего дня"),
                           "value": 1, "unit": "working_days"}],
                operation_type="registration",
            ),
        ],
        result=[fact("п. 3.2.1", "регистрирует его в системе электронного документооборота")],
    )  # fmt: skip
    model = run(answer)
    (process,) = model.processes
    assert process.id == "proc_1" and process.name.layer == KnowledgeLayer.FACT
    # Execution order follows dependencies, not the order in the answer.
    assert [o.id for o in process.operations] == ["proc_1.op_1", "proc_1.op_2"]
    first, second = process.operations
    assert first.action.value == REGISTER and second.depends_on == ["proc_1.op_1"]
    assert first.performer is not None and first.performer.citations[0].fragment_id == fid(
        "п. 3.2.1"
    )
    assert first.deadline is not None
    assert (first.deadline.value, first.deadline.unit) == (1, DeadlineUnit.WORKING_DAYS)
    assert first.deadline.parsed_from_quote
    assert first.confidence == Confidence.HIGH and process.confidence == Confidence.HIGH
    assert process.validation.issues == [] and model.unverified == []


def test_hallucinated_operation_goes_to_unverified_not_to_facts() -> None:
    answer = detail(
        [
            op("o1", "п. 3.2.1", REGISTER),
            op(
                "o2",
                "п. 3.2.1",
                "специалист выплачивает премию за каждый пропуск",
                depends_on=["o1"],
            ),
            op("o3", "п. 3.2.2", CHECK, depends_on=["o2"]),
        ]
    )
    model = run(answer)
    (process,) = model.processes
    assert [o.action.value for o in process.operations] == [REGISTER, CHECK]
    # The dependency on the dropped operation passes to its own predecessor.
    assert process.operations[1].depends_on == ["proc_1.op_1"]
    (item,) = model.unverified
    assert (item.element_type, item.element_id) == ("operation", "proc_1.o2")
    assert item.reason and item.citation is not None
    assert "unverified_quote" in codes(process.validation.issues)


def test_close_paraphrase_of_the_action_is_not_a_fact() -> None:
    paraphrase = "принимает заявление и заносит его в реестр электронного документооборота"
    model = run(detail([op("o1", "п. 3.2.1", paraphrase)]))
    (process,) = model.processes
    assert process.operations == []
    assert model.unverified[0].element_type == "operation"
    assert "близкое совпадение" in model.unverified[0].reason
    expected = {"unverified_quote", "no_operations", "missing_result"}
    assert set(codes(process.validation.issues)) == expected


def test_deadline_not_in_quote() -> None:
    answer = detail(
        [
            op(
                "o1",
                "п. 3.3.1",
                "в течение трёх рабочих дней проверяет сведения, указанные в заявлении",
                deadline=[{"raw": fact("п. 3.3.1", "в течение трёх рабочих дней"),
                           "value": 5, "unit": "working_days"}],
            )
        ]
    )  # fmt: skip
    model = run(answer)
    operation = model.processes[0].operations[0]
    assert operation.deadline is None and operation.confidence == Confidence.LOW
    assert "deadline_not_in_quote" in codes(model.processes[0].validation.issues)
    assert model.unverified[0].element_type == "deadline"


def test_performer_must_be_named_in_its_quote() -> None:
    answer = detail(
        [op("o1", "п. 3.2.1", REGISTER,
            performer=[fact("п. 3.2.1", "принимает заявление", "руководитель управления")])]
    )  # fmt: skip
    model = run(answer)
    operation = model.processes[0].operations[0]
    assert operation.performer is None and operation.confidence == Confidence.LOW
    assert "performer_not_in_quote" in codes(model.processes[0].validation.issues)


def test_decision_and_signing_are_legally_significant() -> None:
    answer = detail(
        [
            op("o1", "п. 3.3.2", "принимает решение о выдаче пропуска или об отказе в его выдаче",
               operation_type="decision", legally_significant=False),
            op("o2", "п. 3.2.1", REGISTER, operation_type="registration", depends_on=["o1"]),
        ]
    )  # fmt: skip
    first, second = run(answer).processes[0].operations
    assert first.operation_type == OperationType.DECISION and first.legally_significant
    assert not second.legally_significant


def test_cycle_without_loop_back_is_an_error_and_with_it_is_allowed() -> None:
    looped = [
        op("o1", "п. 3.2.1", REGISTER, depends_on=["o2"]),
        op("o2", "п. 3.2.2", CHECK, depends_on=["o1"]),
    ]
    issues = run(detail(looped)).processes[0].validation
    assert "cycle" in codes(issues.issues) and issues.has_errors

    rework = [
        op("o1", "п. 3.2.1", REGISTER),
        op("o2", "п. 3.2.2", CHECK, depends_on=["o1", "o3"]),
        op("o3", "п. 3.2.2", RETURN, depends_on=["o2"], is_loop_back=True),
    ]
    process = run(detail(rework)).processes[0]
    assert "cycle" not in codes(process.validation.issues)
    assert [o.action.value for o in process.operations] == [REGISTER, CHECK, RETURN]


def test_duplicate_operations_are_merged_and_references_redirected() -> None:
    answer = detail(
        [
            op("o1", "п. 3.2.1", REGISTER),
            op("o2", "п. 3.2.1", REGISTER),
            op("o3", "п. 3.2.2", CHECK, depends_on=["o2"]),
        ]
    )
    process = run(answer).processes[0]
    assert len(process.operations) == 2
    assert process.operations[1].depends_on == [process.operations[0].id]
    assert "duplicate_merged" in codes(process.validation.issues)


def test_duplicate_processes_are_merged() -> None:
    first = detail([op("o1", "п. 3.2.1", REGISTER)])
    second = detail([op("o1", "п. 3.2.2", CHECK)])
    found = [discovered("proc_1", "Приём заявления"), discovered("proc_2", "приём  заявления")]
    model = run(first, second, found=found)
    (process,) = model.processes
    assert [o.action.value for o in process.operations] == [REGISTER, CHECK]
    assert "duplicate_merged" in codes(process.validation.issues)


def test_dangling_references_and_invalid_decision_points() -> None:
    answer = detail(
        [op("o1", "п. 3.2.1", REGISTER, depends_on=["o9"]), op("o2", "п. 3.2.2", CHECK)],
        decision_points=[
            {"key": "d1", "after_operation": "o2",
             "condition": fact("п. 3.2.2", "При неполном комплекте документы возвращаются"),
             "outcomes": [{"label": "неполный", "next_operation": "o7"},
                          {"label": "полный", "ends_process": True}],
             "criteria": [fact("п. 2.3", "представление неполного комплекта документов")]},
            {"key": "d2", "after_operation": "o5",
             "condition": fact("п. 3.2.2", "проверяет комплектность"),
             "outcomes": [{"label": "a"}, {"label": "b"}]},
            {"key": "d3", "after_operation": "o1",
             "condition": fact("п. 3.2.2", "проверяет комплектность"),
             "outcomes": [{"label": "один исход"}]},
        ],
    )  # fmt: skip
    process = run(answer).processes[0]
    issues = codes(process.validation.issues)
    assert issues.count("dangling_dependency") == 2  # o9 and outcome to o7
    assert issues.count("invalid_decision_point") == 2
    (point,) = process.decision_points
    assert point.id == "proc_1.dp_1" and point.after_operation_id == process.operations[1].id
    assert point.outcomes[0].next_operation_id is None and point.outcomes[1].ends_process
    # Found inside the cited point (in its subpoint): the citation stays on п. 2.3.
    assert point.criteria[0].citations[0].fragment_id == fid("п. 2.3")


def test_relocated_citation_passes_with_a_warning() -> None:
    model = run(detail([op("o1", "п. 4.1", REGISTER)]))
    operation = model.processes[0].operations[0]
    assert operation.action.citations[0].fragment_id == fid("п. 3.2.1")
    assert operation.confidence == Confidence.MEDIUM
    assert "relocated_citation" in codes(model.processes[0].validation.issues)


def test_ocr_text_caps_confidence_at_medium() -> None:
    frags = fragments(**{"п. 3.2.1": {"text_source": "ocr"}})
    model = run(detail([op("o1", "п. 3.2.1", REGISTER)]), frags=frags)
    operation = model.processes[0].operations[0]
    assert operation.action.confidence == Confidence.MEDIUM
    assert operation.confidence == Confidence.MEDIUM


def test_repealed_fragments_stay_out_of_outline_and_processes() -> None:
    frags = fragments(**{"п. 3.2.2": {"repealed": True}})
    listed = {i for c in build_outline(frags, FragmentAliases(frags)) for i in c.fragment_ids}
    assert fid("п. 3.2.2") not in listed and fid("п. 3.2.1") in listed
    model = run(detail([op("o1", "п. 3.2.1", REGISTER), op("o2", "п. 3.2.2", CHECK)]), frags=frags)
    assert [o.action.value for o in model.processes[0].operations] == [REGISTER]
    assert model.unverified[0].reason == "фрагмент утратил силу"


def test_interpretations_carry_no_citations() -> None:
    answer = detail([op("o1", "п. 3.2.1", REGISTER, inputs=["заявление"], outputs=["номер"])])
    operation = run(answer).processes[0].operations[0]
    assert operation.inputs[0].layer == KnowledgeLayer.INTERPRETATION
    assert operation.inputs[0].citations == [] and operation.outputs[0].value == "номер"


def test_process_name_without_proof_stays_an_interpretation() -> None:
    answer = detail(
        [op("o1", "п. 3.2.1", REGISTER)], name=fact("п. 3.2", "Выдача премий сотрудникам")
    )
    model = run(answer, found=[discovered(name="Приём заявления")])
    process = model.processes[0]
    assert process.name.layer == KnowledgeLayer.INTERPRETATION
    assert process.name.value == "Приём заявления"
    assert model.unverified[0].element_type == "process_name"
