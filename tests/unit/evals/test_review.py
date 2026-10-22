"""Expert packet distinguishes omitted context from an unmatched action in supplied text."""

from evals.golden import Golden
from evals.metrics import FragmentNode, Prediction, PredOperation, PredProcess, score_document
from evals.review import review_markdown
from gr.domain.document import Fragment
from gr.domain.enums import Confidence, FragmentKind
from gr.extraction.discovery import DiscoveredProcess, DiscoveryResult


def test_review_exposes_source_and_context_without_claiming_expert_approval() -> None:
    fragments = [
        Fragment(
            id=f"f{n}",
            doc_id="d",
            label=f"п. {n}",
            kind=FragmentKind.POINT,
            text=f"Текст {n} | <данные>",
            start=n * 30,
            end=n * 30 + 20,
            order=n,
        )
        for n in range(1, 5)
    ]
    golden = Golden.model_validate(
        {
            "document": "toy",
            "sha256": "0" * 64,
            "definition": "definition",
            "annotated_by": "draft",
            "processes": [
                {
                    "key": "p",
                    "name": "Процесс",
                    "roots": ["п. 1", "п. 2", "п. 3"],
                    "operations": [
                        {"name": f"Действие {n}", "labels": [f"п. {n}"]} for n in range(1, 4)
                    ],
                }
            ],
        }
    )
    prediction = Prediction(
        fragments=[FragmentNode(label=f.label, parent=None) for f in fragments],
        processes=[
            PredProcess(
                name="Процесс",
                labels=["п. 1"],
                operations=[
                    PredOperation(name="Действие 1", labels=["п. 1"]),
                ],
            ),
            PredProcess(name="Лишний", labels=["п. 4"]),
        ],
    )
    discovery = DiscoveryResult(
        processes=[
            DiscoveredProcess(
                key="p",
                name="Процесс",
                core_fragment_ids=["f1", "f2"],
                confidence=Confidence.HIGH,
            )
        ],
        chunks=1,
        prompt_version="test",
    )
    result = review_markdown(
        golden,
        fragments,
        discovery,
        prediction,
        score_document(golden, prediction),
    )
    assert "НЕ ПРОВЕРЕНО" in result
    assert "Действие 2 | не сопоставлено; исходный пункт передан в S4" in result
    assert "Действие 3 | не сопоставлено; исходный пункт не передан в S4" in result
    assert "### Лишний" in result
    assert r"Текст 4 \| &lt;данные&gt;" in result
    assert golden.verified_by is None
