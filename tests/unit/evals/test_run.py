"""Baseline must never bless incomplete runs or unreviewed annotations."""

import json
from decimal import Decimal
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
        cases: list[Case],
        *,
        cached: bool,
        budget: Budget | None = None,
        s6: bool = False,
        discovery_model: str | None = None,
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


def test_a_baseline_of_another_eval_set_is_not_compared(
    prepared: Outcome, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A new document lowers `.min` without any regression: such a baseline is replaced.
    run.BASELINE.write_text(
        json.dumps({"metrics": {"verified_share.min": 1.0}, "documents": {"old": {}}})
    )
    monkeypatch.setattr(run, "load_config", lambda: {"gate": {"tolerance": 0.0}})
    assert run.main(["--update-baseline"]) == 0
    assert json.loads(run.BASELINE.read_text())["documents"].keys() == {"toy"}


def _regressing(monkeypatch: pytest.MonkeyPatch, config: dict[str, object]) -> None:
    run.BASELINE.write_text(
        json.dumps({"metrics": {"process_precision.mean": 1.0}, "documents": {"toy": {}}})
    )
    monkeypatch.setattr(run, "load_config", lambda: config)


def test_a_regression_needs_its_reason_to_become_the_baseline(
    prepared: Outcome, monkeypatch: pytest.MonkeyPatch
) -> None:
    _regressing(monkeypatch, {"gate": {"tolerance": 0.05}})
    assert run.main(["--update-baseline"]) == 1
    assert json.loads(run.BASELINE.read_text())["metrics"] == {"process_precision.mean": 1.0}

    reason = "S3 prompt now lists the act's procedures: fewer duplicate processes"
    assert run.main(["--update-baseline", "--accept-regressions", reason]) == 0
    accepted = json.loads(run.BASELINE.read_text())["accepted_regressions"]
    assert accepted["reason"] == reason
    assert [r.split(":")[0] for r in accepted["regressions"]] == ["process_precision.mean"]


def test_a_ceiling_is_never_accepted(prepared: Outcome, monkeypatch: pytest.MonkeyPatch) -> None:
    _regressing(monkeypatch, {"gate": {"max": {"calls.sum": 0}}})
    assert run.main(["--update-baseline", "--accept-regressions", "anything"]) == 1


def test_accepting_regressions_only_updates_a_baseline() -> None:
    assert run.main(["--accept-regressions", "why"]) == 2


def test_a_discovery_model_without_tariff_fails_before_llm(
    prepared: Outcome, monkeypatch: pytest.MonkeyPatch
) -> None:
    rates = {"model": "cheap", "input_per_mtok": "1", "output_per_mtok": "1"}
    monkeypatch.setattr(run, "load_config", lambda: {"live_budget": rates})
    monkeypatch.setattr(run, "run_documents", lambda *a, **kw: pytest.fail("LLM invoked"))
    assert run.main(["--budget-usd", "1", "--discovery-model", "unpriced"]) == 2


def test_the_discovery_model_and_its_tariff_reach_the_run(
    prepared: Outcome, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def documents(cases: list[Case], *, cached: bool, budget: Budget | None = None,
                        s6: bool = False, discovery_model: str | None = None,
                        ) -> dict[str, Outcome | str]:  # fmt: skip
        seen.update(budget=budget, discovery_model=discovery_model)
        return {"toy": prepared}

    rates = {"model": "cheap", "input_per_mtok": "1", "output_per_mtok": "1",
             "models": {"big": {"input_per_mtok": "4", "output_per_mtok": "10"}}}  # fmt: skip
    monkeypatch.setattr(run, "load_config", lambda: {"live_budget": rates})
    monkeypatch.setattr(run, "run_documents", documents)
    run.main(["--budget-usd", "1", "--discovery-model", "big"])
    assert seen["discovery_model"] == "big"
    assert isinstance(seen["budget"], Budget)
    assert seen["budget"].other_models == {"big": (Decimal("4"), Decimal("10"))}
    assert run.main(["--rescore", "missing.json", "--discovery-model", "big"]) == 2
    with pytest.raises(SystemExit):  # argparse: «не число», not a traceback
        run.main(["--budget-usd", "0,5x"])
