"""Default assumptions from the profile (FR-050…FR-052, data-model §5, T043)."""

import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
from initiative_builders import initiative
from pydantic import ValidationError

from gr.analysis.defaults import (
    SYSTEM_USER,
    AssumptionProfile,
    build_assumptions,
    load_profile,
    operations_after_decisions,
)
from gr.domain.assumptions import (
    Assumption,
    AssumptionSet,
    Triple,
    initiative_key,
    initiative_operation_key,
    operation_key,
    process_key,
)
from gr.domain.enums import (
    AssumptionSource,
    KnowledgeLayer,
    Level5,
    OperationType,
    SolutionCategory,
)
from gr.domain.initiative import Initiative
from gr.domain.process import (
    CoverageReport,
    DecisionOutcome,
    DecisionPoint,
    Grounded,
    Operation,
    Process,
    ProcessModel,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)
LATER = datetime(2026, 10, 23, tzinfo=UTC)


def text(value: str) -> Grounded[str]:
    return Grounded[str](value=value, layer=KnowledgeLayer.INTERPRETATION)


def operation(
    process: str, n: int, kind: OperationType, *depends: int, loop_back: bool = False
) -> Operation:
    return Operation(
        id=f"{process}.op_{n}",
        seq=n,
        name=kind.value,
        action=text(kind.value),
        operation_type=kind,
        depends_on=[f"{process}.op_{d}" for d in depends],
        is_loop_back=loop_back,
    )


def point(process: str, after: int, *targets: int | None) -> DecisionPoint:
    return DecisionPoint(
        id=f"{process}.dp_{after}",
        after_operation_id=f"{process}.op_{after}",
        condition=text("есть основания для отказа"),
        outcomes=[
            DecisionOutcome(
                label=f"исход {n}",
                next_operation_id=f"{process}.op_{t}" if t else None,
                ends_process=t is None,
            )
            for n, t in enumerate(targets, 1)
        ],
    )


def application() -> Process:
    """Intake → check → decision → (refusal | issuance → record)."""
    p = "proc_1"
    return Process(
        id=p,
        name=text("Выдача разрешения"),
        operations=[
            operation(p, 1, OperationType.INTAKE),
            operation(p, 2, OperationType.COMPLETENESS_CHECK, 1),
            operation(p, 3, OperationType.DECISION, 2),
            operation(p, 4, OperationType.NOTIFICATION),
            operation(p, 5, OperationType.ISSUANCE, 3),
            operation(p, 6, OperationType.RECORD_KEEPING, 5),
        ],
        decision_points=[point(p, 3, 4, 5)],
    )


def approval() -> Process:
    """Approve → sign; after signing: rework (a loop-back step), back to approval, or done."""
    p = "proc_2"
    return Process(
        id=p,
        name=text("Согласование акта"),
        operations=[
            operation(p, 1, OperationType.APPROVAL, 3),  # approval again after rework
            operation(p, 2, OperationType.SIGNING, 1),
            operation(p, 3, OperationType.DOCUMENT_PREPARATION, 2, loop_back=True),
        ],
        decision_points=[point(p, 2, 3, 1, None)],
    )


def model(*processes: Process) -> ProcessModel:
    return ProcessModel(
        processes=list(processes or (application(), approval())),
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )


def rag(key: str = "ini_proc_1_1", *ops: tuple[str, str], **updates: Any) -> Initiative:
    """An llm_rag initiative of proc_1 on the given (operation, reduction level) pairs."""
    pairs = ops or (("proc_1.op_2", "high"), ("proc_1.op_5", "very_high"))
    ratings = initiative(SolutionCategory.LLM_RAG).ratings.model_dump(mode="json")
    ratings["integration_complexity"]["level"] = "high"
    return initiative(
        SolutionCategory.LLM_RAG,
        id=key,
        process_id="proc_1",
        operation_ids=[op for op, _ in pairs],
        reduction_potential=[
            {"operation_id": op, "level": level, "justification": "x"} for op, level in pairs
        ],
        ratings=ratings,
        **updates,
    )


def build(
    initiatives: list[Initiative] | None = None,
    existing: AssumptionSet | None = None,
    **kwargs: Any,
) -> AssumptionSet:
    return build_assumptions(
        kwargs.pop("model", None) or model(),
        [rag()] if initiatives is None else initiatives,
        existing=existing,
        run_id="run_1",
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def values(found: AssumptionSet, key: str) -> tuple[Decimal, Decimal, Decimal]:
    return found.by_key()[key].values.as_tuple


def triple(conservative: str, base: str, optimistic: str) -> tuple[Decimal, Decimal, Decimal]:
    return (Decimal(conservative), Decimal(base), Decimal(optimistic))


# --- profile -------------------------------------------------------------------------------


def raw_profile() -> dict[str, Any]:
    source = resources.files("gr.config").joinpath("assumption_profile.toml").read_text("utf-8")
    return tomllib.loads(source, parse_float=Decimal)


def test_profile_is_complete_and_exact() -> None:
    profile = load_profile()
    assert set(profile.time_hours) == set(OperationType)
    assert set(profile.reduction_share) == set(Level5)
    assert SolutionCategory.NO_CHANGE not in profile.capex
    # Read as exact decimals: no float noise in money or shares.
    assert str(profile.reduction_share[Level5.MEDIUM]) == "0.35"
    assert profile.capex[SolutionCategory.CLASSIC_AUTOMATION] == Decimal(1_500_000)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda raw: raw["time_hours"].pop("signing"), "time_hours"),
        (lambda raw: raw["capex"].update(no_change=0), "capex"),
        (lambda raw: raw["reduction_share"].update(high=Decimal("1.2")), "shares"),
        (lambda raw: raw["spread"].update(horizon_years=[1, 1]), "horizon_years"),
        (lambda raw: raw["spread"].update(capex=[0, 1]), "positive"),
        (lambda raw: raw.update(horizon_years=4), "horizon_years"),
    ],
)
def test_profile_rejects_gaps_and_out_of_range_values(change: Any, error: str) -> None:
    raw = raw_profile()
    change(raw)
    with pytest.raises(ValidationError, match=error):
        AssumptionProfile.model_validate(raw)


# --- generation ----------------------------------------------------------------------------


def test_every_element_gets_every_key_of_the_data_model() -> None:
    found = build()
    ops = [op for p in model().processes for op in p.operations]
    expected = {"hourly_cost", "overhead_ratio", "fte_hours_per_year", "horizon_years"}
    expected |= {process_key(p, "volume_per_year") for p in ("proc_1", "proc_2")}
    expected |= {operation_key(op.id, k) for op in ops for k in ("time_hours", "case_share")}
    expected |= {
        initiative_key("ini_proc_1_1", k)
        for k in ("capex", "fixed_opex_per_year", "adoption_y1", "adoption_y2", "adoption_y3")
    }
    expected |= {
        initiative_operation_key("ini_proc_1_1", op, k)
        for op in ("proc_1.op_2", "proc_1.op_5")
        for k in ("reduction_share", "review_hours", "variable_cost_per_exec")
    }
    assert set(found.by_key()) == expected
    assert all(item.source == AssumptionSource.DEFAULT_PROFILE for item in found.items)
    assert all(item.updated_by == SYSTEM_USER and item.note for item in found.items)
    assert (found.run_id, found.version, found.created_at) == ("run_1", 1, NOW)
    pair = found.by_key()[initiative_operation_key("ini_proc_1_1", "proc_1.op_2", "review_hours")]
    assert (pair.scope, pair.target_id, pair.parameter) == (
        "initiative_operation",
        "ini_proc_1_1/proc_1.op_2",
        "review_hours",
    )


def test_values_follow_the_profile_tables_and_spreads() -> None:
    found = build()
    io = "ini_proc_1_1"
    assert values(found, "hourly_cost") == triple("1200", "1200", "1200")
    assert values(found, "horizon_years") == triple("3", "3", "3")
    assert values(found, process_key("proc_1", "volume_per_year")) == triple("700", "1000", "1300")
    # completeness_check 0.5 h, ×0.8 / ×1.2.
    assert values(found, operation_key("proc_1.op_2", "time_hours")) == triple("0.4", "0.5", "0.6")
    # Reduction by level, ×0.6 / ×1.2, capped at 1.
    assert values(found, initiative_operation_key(io, "proc_1.op_2", "reduction_share")) == triple(
        "0.36", "0.6", "0.72"
    )
    assert values(found, initiative_operation_key(io, "proc_1.op_5", "reduction_share")) == triple(
        "0.48", "0.8", "0.96"
    )
    # Review: llm_rag 0.25 × operation time; more review in the conservative scenario.
    assert values(found, initiative_operation_key(io, "proc_1.op_2", "review_hours")) == triple(
        "0.1875", "0.125", "0.0875"
    )
    assert values(
        found, initiative_operation_key(io, "proc_1.op_2", "variable_cost_per_exec")
    ) == triple("7.5", "5", "3.5")
    # Capex: llm_rag 4 M × integration «high» 1.5; fixed opex 20 % of capex.
    assert values(found, initiative_key(io, "capex")) == triple("8400000", "6000000", "4800000")
    assert values(found, initiative_key(io, "fixed_opex_per_year")) == triple(
        "1560000", "1200000", "960000"
    )
    assert values(found, initiative_key(io, "adoption_y1")) == triple("0.3", "0.5", "0.6")
    assert values(found, initiative_key(io, "adoption_y3")) == triple("0.6", "1.0", "1")


def test_volumes_and_shares_after_a_decision_require_input() -> None:
    found = build().by_key()
    assert found[process_key("proc_1", "volume_per_year")].requires_input
    shares = {
        n: found[operation_key(f"proc_1.op_{n}", "case_share")] for n in range(1, 7)
    }  # fmt: skip
    assert all(item.values.as_tuple == triple("1", "1", "1") for item in shares.values())
    # Refusal (op 4), issuance (op 5) and what follows issuance (op 6) depend on the outcome.
    assert {n for n, item in shares.items() if item.requires_input} == {4, 5, 6}
    assert not found[operation_key("proc_1.op_2", "time_hours")].requires_input


def test_rework_and_returns_do_not_flag_the_steps_before_the_decision() -> None:
    # The decision after signing sends cases to rework (a loop-back step) or back to approval:
    # approval and signing are done in every case, only rework is not.
    assert operations_after_decisions(approval()) == {"proc_2.op_3"}
    assert operations_after_decisions(application()) == {
        "proc_1.op_4",
        "proc_1.op_5",
        "proc_1.op_6",
    }


def test_no_change_initiative_costs_nothing() -> None:
    nothing = rag(
        "ini_proc_1_2",
        ("proc_1.op_2", "medium"),
        solution_category=SolutionCategory.NO_CHANGE,
        finding_ids=[],
        alternatives_considered=[],
    )
    found = build([nothing])
    io = "ini_proc_1_2"
    zero = triple("0", "0", "0")
    assert values(found, initiative_key(io, "capex")) == zero
    assert values(found, initiative_key(io, "fixed_opex_per_year")) == zero
    for parameter in ("reduction_share", "review_hours", "variable_cost_per_exec"):
        assert values(found, initiative_operation_key(io, "proc_1.op_2", parameter)) == zero


def test_initiative_on_an_unknown_operation_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown operation"):
        build([rag("ini_x", ("proc_9.op_1", "low"))])


def test_generation_is_deterministic() -> None:
    assert build().model_dump_json() == build().model_dump_json()


def test_existing_set_of_another_run_is_refused() -> None:
    other = build().model_copy(update={"run_id": "run_2"})
    with pytest.raises(ValueError, match="another run"):
        build(existing=other)


# --- regeneration --------------------------------------------------------------------------


def user_value(item: Assumption, *numbers: str) -> Assumption:
    return item.model_copy(
        update={
            "values": Triple(
                conservative=Decimal(numbers[0]),
                base=Decimal(numbers[1]),
                optimistic=Decimal(numbers[2]),
            ),
            "source": AssumptionSource.USER,
            "requires_input": False,
            "updated_by": "expert",
            "updated_at": LATER,
        }
    )


def edited(found: AssumptionSet, key: str, *numbers: str) -> AssumptionSet:
    """A user save: the item changed, a new version."""
    items = [user_value(i, *numbers) if i.key == key else i for i in found.items]
    return found.model_copy(update={"items": items, "version": found.version + 1})


def test_nothing_new_keeps_the_same_set_and_version() -> None:
    first = build()
    assert build(existing=first, now=LATER) is first


def test_regeneration_keeps_user_values_and_fills_only_new_keys() -> None:
    time_key = operation_key("proc_1.op_2", "time_hours")
    capex_key = initiative_key("ini_proc_1_1", "capex")
    saved = edited(edited(build(), time_key, "1", "2", "3"), capex_key, "1", "100", "1")
    # A new initiative on the same operation appears (S6 rerun or an expert's addition).
    second = rag("ini_proc_1_2", ("proc_1.op_2", "low"))
    found = build([rag(), second], existing=saved, now=LATER)

    assert found.version == saved.version + 1 and found.created_at == LATER
    before, after = saved.by_key(), found.by_key()
    assert all(after[key] == item for key, item in before.items())  # nothing overwritten
    assert after[time_key].values.base == 2 and after[time_key].source == AssumptionSource.USER
    # The new initiative's review time comes from the user's operation time: 0.25 × 2 h.
    review = initiative_operation_key("ini_proc_1_2", "proc_1.op_2", "review_hours")
    assert after[review].values.base == Decimal("0.5")
    assert after[review].source == AssumptionSource.DEFAULT_PROFILE
    # Opex of the old initiative stays as generated; a fresh one follows the user's capex.
    assert values(found, initiative_key("ini_proc_1_1", "fixed_opex_per_year"))[1] == 1_200_000


def test_derived_default_of_a_new_key_follows_the_users_parent_value() -> None:
    capex_key = initiative_key("ini_proc_1_1", "capex")
    opex_key = initiative_key("ini_proc_1_1", "fixed_opex_per_year")
    saved = edited(build(), capex_key, "1", "100", "1")
    without_opex = saved.model_copy(update={"items": [i for i in saved.items if i.key != opex_key]})
    found = build(existing=without_opex, now=LATER)
    assert values(found, opex_key) == triple("26.0", "20.0", "16.0")  # 0.2 × 100, ×1.3 / ×0.8


def test_gone_elements_lose_defaults_but_keep_user_values() -> None:
    capex_key = initiative_key("ini_proc_1_1", "capex")
    saved = edited(build(), capex_key, "1", "2", "3")
    found = build([], existing=saved, now=LATER)
    keys = set(found.by_key())
    assert capex_key in keys  # the user's number is never lost
    assert initiative_key("ini_proc_1_1", "adoption_y1") not in keys
    assert not any(k.startswith("ini.ini_proc_1_1.proc_1") for k in keys)
    assert found.version == saved.version + 1


def test_key_builders_check_the_scope() -> None:
    with pytest.raises(ValueError, match="not an assumption of scope operation"):
        operation_key("proc_1.op_1", "capex")
    key = initiative_operation_key("ini_1", "proc_1.op_1", "reduction_share")
    assert key == "ini.ini_1.proc_1.op_1.reduction_share"
