"""Eval metrics on toy data (T036): matching, recall and precision, deadlines, gate."""

from typing import Any

import pytest

from evals.golden import Golden
from evals.metrics import (
    FragmentNode,
    PredDeadline,
    Prediction,
    PredOperation,
    PredProcess,
    UnknownLabelError,
    aggregate,
    gate,
    score_document,
    subtree_labels,
)

#  0 документ
#  ├ 1 п. 1 ─ 2 абз. 1 п. 1, 3 абз. 2 п. 1
#  ├ 4 п. 2 ─ 5 абз. 1 п. 2
#  ├ 6 п. 3
#  └ 7 п. 4
FRAGMENTS = [
    FragmentNode(label="документ", parent=None),
    FragmentNode(label="п. 1", parent=0),
    FragmentNode(label="абз. 1 п. 1", parent=1),
    FragmentNode(label="абз. 2 п. 1", parent=1),
    FragmentNode(label="п. 2", parent=0),
    FragmentNode(label="абз. 1 п. 2", parent=4),
    FragmentNode(label="п. 3", parent=0),
    FragmentNode(label="п. 4", parent=0),
]

GOLDEN = Golden.model_validate(
    {
        "document": "игрушечный регламент",
        "sha256": "0" * 64,
        "definition": "процедуры пп. 1–2",
        "annotated_by": "test",
        "processes": [
            {
                "key": "a",
                "name": "Приём и регистрация заявления",
                "roots": ["п. 1"],
                "operations": [
                    {"name": "принимает заявление", "labels": ["абз. 1 п. 1"]},
                    {"name": "регистрирует заявление", "labels": ["абз. 2 п. 1"]},
                ],
                "deadlines": [{"value": 1, "unit": "working_days", "label": "абз. 2 п. 1"}],
            },
            {
                "key": "b",
                "name": "Выдача пропуска",
                "roots": ["п. 2"],
                "operations": [{"name": "выдаёт пропуск", "labels": ["п. 2"]}],
            },
            {"key": "c", "name": "Текущий контроль", "required": False, "roots": ["п. 3"]},
        ],
    }
)


def proc(
    name: str, *ops: tuple[str, str], deadlines: list[PredDeadline] | None = None
) -> PredProcess:
    operations = [PredOperation(name=n, labels=[label]) for n, label in ops]
    return PredProcess(
        name=name,
        labels=sorted({label for _, label in ops}),
        operations=operations,
    )


A = proc(
    "Приём заявления",
    ("принимает заявление", "абз. 1 п. 1"),
    ("регистрирует заявление", "абз. 2 п. 1"),
)
B = proc("Выдача пропуска", ("выдаёт пропуск", "абз. 1 п. 2"))


def predict(*processes: PredProcess, **counts: Any) -> Prediction:
    return Prediction(fragments=FRAGMENTS, processes=list(processes), **counts)


def metric(*processes: PredProcess, name: str, **counts: Any) -> float | None:
    return score_document(GOLDEN, predict(*processes, **counts)).metrics[name]


# --- subtrees ------------------------------------------------------------------------------


def test_root_brings_its_subtree() -> None:
    assert subtree_labels(FRAGMENTS, ["п. 1"]) == {"п. 1", "абз. 1 п. 1", "абз. 2 п. 1"}
    assert subtree_labels(FRAGMENTS, ["абз. 1 п. 2", "п. 4"]) == {"абз. 1 п. 2", "п. 4"}


def test_unknown_label_in_golden_is_an_error() -> None:
    with pytest.raises(UnknownLabelError, match=r"п\. 9"):
        subtree_labels(FRAGMENTS, ["п. 9"])


# --- processes -----------------------------------------------------------------------------


def test_perfect_answer() -> None:
    score = score_document(GOLDEN, predict(A, B))
    assert score.metrics["process_recall"] == 1.0
    assert score.metrics["process_precision"] == 1.0
    assert score.metrics["operation_recall"] == 1.0
    assert score.missed == [] and score.extra == []
    assert {m.golden for m in score.matches} == {"a", "b"}


def test_missed_process_lowers_recall_only() -> None:
    score = score_document(GOLDEN, predict(A))
    assert score.metrics["process_recall"] == 0.5
    assert score.metrics["process_precision"] == 1.0
    assert score.missed == ["Выдача пропуска"]


def test_optional_process_is_accepted_but_not_required() -> None:
    control = proc("Контроль", ("осуществляет контроль", "п. 3"))
    score = score_document(GOLDEN, predict(A, control))
    assert score.metrics["process_precision"] == 1.0  # not a false positive
    assert score.metrics["process_recall"] == 0.5  # and not a hit either


def test_process_outside_the_golden_is_a_false_positive() -> None:
    form = proc("Форма заявления", ("заполняет форму", "п. 4"))
    score = score_document(GOLDEN, predict(A, B, form))
    assert score.metrics["process_precision"] == pytest.approx(2 / 3)
    assert score.extra == ["Форма заявления"]


def test_second_process_on_the_same_procedure_is_a_false_positive() -> None:
    part = proc("Регистрация заявления", ("регистрирует заявление", "абз. 2 п. 1"))
    score = score_document(GOLDEN, predict(A, part, B))
    assert score.metrics["process_recall"] == 1.0
    assert score.metrics["process_precision"] == pytest.approx(2 / 3)
    # The fuller process wins the pair: its share is the same, its overlap larger.
    assert next(m for m in score.matches if m.golden == "a").predicted == "Приём заявления"


def test_merged_procedures_match_nothing() -> None:
    whole = proc(
        "Предоставление услуги",
        ("принимает заявление", "абз. 1 п. 1"),
        ("выдаёт пропуск", "абз. 1 п. 2"),
        ("заполняет форму", "п. 4"),
    )
    score = score_document(GOLDEN, predict(whole))
    assert score.metrics["process_recall"] == 0.0
    assert score.metrics["process_precision"] == 0.0


def test_close_name_rescues_a_low_share() -> None:
    cites = [
        ("принимает заявление", "абз. 1 п. 1"),
        ("ведёт журнал", "п. 4"),
        ("архивирует", "п. 3"),
    ]
    assert metric(proc("Учёт обращений", *cites), name="process_recall") == 0.0
    assert metric(proc("Приём и регистрация заявления", *cites), name="process_recall") == 0.5


def test_no_answer_is_zero_precision() -> None:
    score = score_document(GOLDEN, predict())
    assert score.metrics["process_precision"] == 0.0
    assert score.metrics["process_recall"] == 0.0


# --- operations ----------------------------------------------------------------------------


def test_operation_recall_counts_only_found_processes() -> None:
    half = proc("Приём заявления", ("принимает заявление", "абз. 1 п. 1"))
    assert metric(half, B, name="operation_recall") == pytest.approx(2 / 3)
    assert metric(A, name="operation_recall") == 1.0  # B is missed, its operations are not


def test_operation_recall_is_zero_when_nothing_is_found() -> None:
    assert metric(name="operation_recall") == 0.0


def test_operation_cited_in_a_child_of_the_golden_fragment_counts() -> None:
    assert metric(A, B, name="operation_recall") == 1.0  # B: «абз. 1 п. 2» under «п. 2»


def test_operations_on_one_fragment_are_told_apart_by_name() -> None:
    golden = GOLDEN.model_copy(
        update={
            "processes": [
                GOLDEN.processes[0].model_copy(
                    update={
                        "operations": [
                            GOLDEN.processes[0].operations[0],
                            GOLDEN.processes[0]
                            .operations[1]
                            .model_copy(update={"labels": ["абз. 1 п. 1"]}),
                        ]
                    }
                )
            ]
        }
    )
    one = proc("Приём заявления", ("регистрирует заявление", "абз. 1 п. 1"))
    score = score_document(golden, predict(one))
    assert score.matches[0].missed_operations == ["принимает заявление"]


# --- deadlines -----------------------------------------------------------------------------


def deadline(value: int | None, unit: str, span: str) -> PredDeadline:
    return PredDeadline(value=value, unit=unit, spans=[span])


def with_deadlines(*deadlines: PredDeadline) -> PredProcess:
    return A.model_copy(update={"deadlines": list(deadlines)})


def test_deadline_recall() -> None:
    one_day = deadline(1, "working_days", "в течение 1 рабочего дня")
    assert metric(with_deadlines(one_day), B, name="deadline_recall") == 1.0
    two_days = deadline(2, "working_days", "в течение 2 рабочих дней")
    assert metric(with_deadlines(two_days), B, name="deadline_recall") == 0.0
    assert metric(B, name="deadline_recall") is None  # A not found: nothing to look for


def test_next_working_day_equals_one_working_day() -> None:
    next_day = deadline(None, "next_working_day", "не позднее следующего рабочего дня")
    assert metric(with_deadlines(next_day), name="deadline_recall") == 1.0


def test_deadline_not_in_its_text_is_counted() -> None:
    good = deadline(1, "working_days", "в течение одного рабочего дня")
    bad = deadline(5, "working_days", "в течение одного рабочего дня")
    score = score_document(GOLDEN, predict(with_deadlines(good, bad)))
    assert score.metrics["deadlines_not_in_text"] == 1.0
    assert score.bad_deadlines == ["Приём заявления: 5 working_days"]


def test_unqualified_days_read_as_calendar_are_in_the_text() -> None:
    calendar = deadline(20, "calendar_days", "не должен превышать двадцати дней")
    assert metric(with_deadlines(calendar), name="deadlines_not_in_text") == 0.0


# --- verification and coverage -------------------------------------------------------------


def test_verified_share_counts_rejected_claims() -> None:
    counts = {"citations_passed": 18, "citations_total": 18, "unverified": 2}
    assert metric(A, B, name="verified_share", **counts) == pytest.approx(0.9)
    assert metric(A, B, name="verified_share") is None


def test_coverage() -> None:
    assert metric(A, name="coverage", fragments_used=3, fragments_total=4) == 0.75


# --- aggregation and gate ------------------------------------------------------------------


def test_aggregate_mean_and_min_over_documents() -> None:
    summary = aggregate(
        {
            "x": {"process_recall": 1.0, "deadlines_not_in_text": 0.0, "out_tokens": 100.0},
            "y": {"process_recall": 0.5, "deadlines_not_in_text": 2.0, "out_tokens": 50.0},
            "z": {"process_recall": None},
        }
    )
    assert summary["process_recall.mean"] == 0.75
    assert summary["process_recall.min"] == 0.5
    assert summary["deadlines_not_in_text.sum"] == 2.0
    assert summary["out_tokens.sum"] == 150.0
    assert summary["operation_recall.mean"] is None


CONFIG: dict[str, Any] = {
    "gate": {
        "tolerance": 0.05,
        "tolerances": {"out_tokens.sum": 0.5},
        "max": {"deadlines_not_in_text.sum": 0},
    },
    "targets": {"process_recall": 0.85},
}


def test_gate_regression_beyond_tolerance() -> None:
    problems, _ = gate({"process_recall.min": 0.7}, {"process_recall.min": 0.8}, CONFIG)
    assert problems and "process_recall.min" in problems[0]
    problems, _ = gate({"process_recall.min": 0.76}, {"process_recall.min": 0.8}, CONFIG)
    assert problems == []


def test_gate_ceiling_applies_without_baseline() -> None:
    problems, rows = gate({"deadlines_not_in_text.sum": 1.0}, None, CONFIG)
    assert problems == ["deadlines_not_in_text.sum: 1.0 > max 0"]
    assert rows[0][4].startswith("❌")


def test_gate_usage_only_with_its_own_tolerance() -> None:
    baseline = {"out_tokens.sum": 100.0, "in_tokens.sum": 100.0}
    problems, _ = gate({"out_tokens.sum": 160.0, "in_tokens.sum": 900.0}, baseline, CONFIG)
    assert problems == ["out_tokens.sum: 100.0 → 160.0 (допуск 0.5)"]


def test_gate_targets_only_when_strict() -> None:
    current = {"process_recall.mean": 0.9, "process_recall.min": 0.6}
    assert gate(current, None, CONFIG)[0] == []
    problems, _ = gate(current, None, CONFIG, strict=True)
    assert problems == ["process_recall.min: 0.6 не достигает цели 0.85"]
