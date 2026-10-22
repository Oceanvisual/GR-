"""Baseline must never bless incomplete runs or unreviewed annotations."""

from pathlib import Path

import pytest

from evals import run
from evals.budget import Budget
from evals.golden import Case, Golden
from evals.metrics import Prediction
from evals.target import Outcome


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Outcome:
    case = Case(id="toy", path="unused", golden="unused")
    golden = Golden(
        document="toy",
        sha256="0" * 64,
        definition="toy",
        annotated_by="test",
        verified_by="expert",
        processes=[],
    )
    outcome = Outcome(
        prediction=Prediction(fragments=[], processes=[]),
        run_id="test",
        status="succeeded",
        complete=True,
        failed_processes=0,
        usage={"calls": 1, "in_tokens": 10, "out_tokens": 10, "latency_s": 1},
        models={"extraction": "fake"},
        prompt_versions={"discover": "test"},
    )

    async def documents(
        cases: list[Case], *, cached: bool, budget: Budget | None = None
    ) -> dict[str, Outcome | str]:
        return {"toy": outcome}

    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(run, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(run, "load_config", lambda: {})
    monkeypatch.setattr(run, "load_cases", lambda: [case])
    monkeypatch.setattr(run, "checked_golden", lambda _: golden)
    monkeypatch.setattr(run, "run_documents", documents)
    return outcome


@pytest.mark.parametrize("option", [["--cached"], ["--rescore", "missing"], ["--only", "toy"]])
def test_invalid_baseline_options_fail_before_running(option: list[str]) -> None:
    assert run.main(["--update-baseline", *option]) == 2


def test_unreviewed_baseline_fails_before_llm(
    prepared: Outcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = run.checked_golden(Case(id="toy", path="unused", golden="unused"))
    monkeypatch.setattr(
        run, "checked_golden", lambda _: golden.model_copy(update={"verified_by": None})
    )
    monkeypatch.setattr(run, "run_documents", lambda *a, **kw: pytest.fail("LLM invoked"))
    assert run.main(["--update-baseline"]) == 2
    assert not run.BASELINE.exists()


@pytest.mark.parametrize("field,value", [("complete", False), ("failed_processes", 1)])
def test_incomplete_success_cannot_update_baseline(
    prepared: Outcome,
    field: str,
    value: bool | int,
) -> None:
    setattr(prepared, field, value)
    assert run.main(["--update-baseline"]) == 1
    assert not run.BASELINE.exists()


def test_failed_gate_preserves_existing_baseline(
    prepared: Outcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = '{"metrics": {}}'
    run.BASELINE.write_text(original)
    monkeypatch.setattr(run, "gate", lambda *a, **kw: (["failed ceiling"], []))
    assert run.main(["--update-baseline"]) == 1
    assert run.BASELINE.read_text() == original


def test_complete_reviewed_run_can_be_baseline(prepared: Outcome) -> None:
    assert run.main(["--update-baseline"]) == 0
    assert run.BASELINE.exists()


def test_incomplete_regular_run_fails(prepared: Outcome) -> None:
    prepared.complete = False
    assert run.main([]) == 1
