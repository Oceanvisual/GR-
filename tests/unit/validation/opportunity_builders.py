"""Valid process model and S6 drafts for deterministic US3 tests."""

from pathlib import Path

from gr.domain.document import Fragment
from gr.domain.enums import FragmentKind, KnowledgeLayer, OperationType
from gr.domain.process import CoverageReport, Grounded, Operation, Process, ProcessModel
from gr.extraction.opportunities import OpportunitiesResult, OpportunityDraft
from gr.extraction.schemas import OpportunitiesOut

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/llm"


def model() -> ProcessModel:
    return ProcessModel(
        processes=[
            Process(
                id="proc_1",
                name=Grounded[str](value="Тестовый процесс", layer=KnowledgeLayer.INTERPRETATION),
                operations=[
                    Operation(
                        id=f"proc_1.op_{n}",
                        seq=n,
                        name=f"Действие {n}",
                        action=Grounded[str](
                            value="Проверяет документы", layer=KnowledgeLayer.INTERPRETATION
                        ),
                        operation_type=OperationType.COMPLETENESS_CHECK,
                    )
                    for n in range(1, 3)
                ],
            )
        ],
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )


def fragments() -> list[Fragment]:
    return [
        Fragment(
            id="body/1",
            parent_id="body",
            doc_id="doc_test",
            label="п. 1",
            kind=FragmentKind.POINT,
            text="Проверяет документы",
            start=0,
            end=19,
            order=0,
        )
    ]


def answer(name: str = "opp_correct") -> OpportunitiesOut:
    return OpportunitiesOut.model_validate_json((FIXTURES / f"{name}.json").read_text())


def drafts(value: OpportunitiesOut | None = None) -> OpportunitiesResult:
    return OpportunitiesResult(
        processes=[OpportunityDraft(process_id="proc_1", answer=value or answer())],
        prompt_version="test",
    )
