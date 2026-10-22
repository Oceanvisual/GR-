"""Deterministic Mermaid flowchart of a process (api.md § Mermaid, T033)."""

import re
from pathlib import Path

import pytest

from gr.analysis.mermaid import MAX_LABEL, label, process_to_mermaid
from gr.domain.enums import KnowledgeLayer, OperationType, ReviewStatus
from gr.domain.process import DecisionOutcome, DecisionPoint, Grounded, Operation, Process
from gr.domain.review import ReviewState

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "mermaid_process.mmd"


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def operation(
    n: int,
    name: str,
    performer: str | None = None,
    depends_on: tuple[int, ...] = (),
    *,
    loop_back: bool = False,
    status: ReviewStatus = ReviewStatus.PENDING,
) -> Operation:
    return Operation(
        id=f"proc_1.op_{n}",
        seq=n - 1,
        name=name,
        action=text(name),
        performer=text(performer) if performer else None,
        depends_on=[f"proc_1.op_{d}" for d in depends_on],
        operation_type=OperationType.OTHER,
        is_loop_back=loop_back,
        review=ReviewState(status=status),
    )


def process() -> Process:
    return Process(
        id="proc_1",
        name=text("Выдача пропуска"),
        operations=[
            operation(1, "Регистрация заявления", "специалист отдела"),
            operation(2, "Проверка комплектности", "специалист отдела", (1, 4)),
            operation(3, "Решение о выдаче", "начальник отдела", (2,)),
            operation(4, "Возврат на доработку", None, (2,), loop_back=True),
            operation(5, "Лишняя операция", "кто-то", (3,), status=ReviewStatus.REJECTED),
            operation(6, "Выдача пропуска", "специалист", (5,)),
        ],
        decision_points=[
            DecisionPoint(
                id="proc_1.dp_1",
                after_operation_id="proc_1.op_3",
                condition=text("Есть основания для отказа?"),
                outcomes=[
                    DecisionOutcome(label="нет", next_operation_id="proc_1.op_6"),
                    DecisionOutcome(label="да", ends_process=True),
                ],
            )
        ],
    )


def test_golden() -> None:
    assert process_to_mermaid(process()) == GOLDEN.read_text("utf-8")


def test_deterministic() -> None:
    assert process_to_mermaid(process()) == process_to_mermaid(process())


def test_rejected_operation_is_left_out_and_bridged() -> None:
    chart = process_to_mermaid(process())
    assert "op_5" not in chart and "Лишняя" not in chart
    chain = Process(
        id="proc_1",
        name=text("x"),
        operations=[
            operation(1, "a"),
            operation(2, "b", depends_on=(1,), status=ReviewStatus.REJECTED),
            operation(3, "c", depends_on=(2,)),
        ],
    )
    assert "op_1 --> op_3" in process_to_mermaid(chain)  # op_3 depended on the rejected op_2


def test_no_direct_edge_beside_a_decision() -> None:
    chart = process_to_mermaid(process())
    # op_6 follows op_3 through the decision; no second arrow op_3 → op_6.
    assert "op_3 --> op_6" not in chart and 'dp_1 -- "нет" --> op_6' in chart


def test_loop_back_is_dotted() -> None:
    assert "op_4 -.-> op_2" in process_to_mermaid(process())


@pytest.mark.parametrize(
    "hostile",
    [
        'Решение "по заявлению"]; click op_1 call alert()',
        "<script>alert(1)</script>",
        "строка\nс переносом\r\nи ещё",
        "скобки {a} (b) [c] | d ; e # f ` g & h",
    ],
)
def test_labels_cannot_break_out(hostile: str) -> None:
    escaped = label(hostile)
    # Entity codes end with «;»; outside them no character may reach the Mermaid syntax.
    bare = re.sub(r"#(?:\d+|[a-z]+);", "", escaped)
    assert not set(bare) & set('"<>[]{}()|;`#&\n\r')
    chart = process_to_mermaid(
        Process(id="proc_1", name=text("x"), operations=[operation(1, hostile, hostile)])
    )
    node_line = chart.splitlines()[1]
    assert node_line.count('"') == 2 and node_line.endswith('"]')
    assert "<script" not in chart


def test_labels_are_cut_to_the_limit() -> None:
    assert len(label("а" * 200)) == MAX_LABEL and label("а" * 200).endswith("…")
    assert label("a#b") == "a#35;b"  # the entity marker itself is escaped first
