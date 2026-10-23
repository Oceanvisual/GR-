"""The eval reads the verified process model as the metrics need it (T036, evals/target.py)."""

from validation_builders import FIXTURES, LIMITS, detail, fact, op, run

from evals.target import to_prediction
from gr.ingestion.txt import TxtExtractor
from gr.services.parsing import parse_raw

REGISTER = "принимает заявление и регистрирует его в системе электронного документооборота"
CHECK = "проверяет комплектность документов, указанных в пункте 2.2"


def test_process_model_to_prediction() -> None:
    answer = detail(
        [
            op(
                "o1",
                "п. 3.2.1",
                REGISTER,
                deadline=[{"raw": fact("п. 3.2.1", "в течение одного рабочего дня"),
                           "value": 1, "unit": "working_days"}],
            ),
            op("o2", "п. 3.2.2", CHECK, depends_on=["o1"]),
            op("o3", "п. 3.2.1", "специалист выплачивает премию за каждый пропуск"),
        ]
    )  # fmt: skip
    model = run(answer)
    raw = TxtExtractor().extract((FIXTURES / "synthetic" / "regulation.txt").read_bytes())
    parsed = parse_raw(raw, "doc_V", LIMITS)

    prediction = to_prediction(model, parsed.fragments, parsed.text)

    (process,) = prediction.processes
    # The name cites «п. 3.2», the actions their own points; the invented operation is gone.
    assert process.labels == ["п. 3.2", "п. 3.2.1", "п. 3.2.2"]
    assert [o.labels for o in process.operations] == [["п. 3.2.1"], ["п. 3.2.2"]]
    (deadline,) = process.deadlines
    assert (deadline.value, deadline.unit) == (1, "working_days")
    assert deadline.spans == ["в течение одного рабочего дня"]
    assert prediction.unverified == len(model.unverified) >= 1
    assert prediction.citations_total == prediction.citations_passed > 0
    assert len(prediction.fragments) == len(parsed.fragments)
    parents = [n.parent for n in prediction.fragments]
    assert parents[0] is None and all(p is not None for p in parents[1:])
