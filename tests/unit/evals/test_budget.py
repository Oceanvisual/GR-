"""Budget includes repair/fallback requests and fails before the next paid request."""

from decimal import Decimal

import pytest

from evals.budget import Budget, BudgetExceeded


def budget(limit: str = "3") -> Budget:
    return Budget(Decimal(limit), Decimal("1.1"), Decimal("2.75"), "m")


def test_conservative_reservation_and_actual_settlement() -> None:
    b = budget()
    first = b.reserve("m", 10000, 16000)
    assert b.committed_usd == first > Decimal("0.05")
    b.settle(first, "0.003")
    assert b.committed_usd == b.actual_usd == Decimal("0.003")
    second = b.reserve("m", 10000, 16000)
    assert b.requests == 2
    assert b.committed_usd == second + Decimal("0.003")


def test_next_request_blocked_before_reservation() -> None:
    b = budget("0.001")
    with pytest.raises(BudgetExceeded):
        b.reserve("m", 10000, 16000)
    assert b.requests == 0 and b.committed_usd == 0


def test_missing_bill_keeps_full_reservation() -> None:
    b = budget()
    bound = b.reserve("m", 1000, 1000)
    b.settle(bound, None)
    assert b.committed_usd == bound and b.actual_usd == 0


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1", "not a number"])
def test_invalid_or_unexpected_bill_stops_eval(cost: str) -> None:
    b = budget()
    bound = b.reserve("m", 1000, 1000)
    with pytest.raises(BudgetExceeded):
        b.settle(bound, cost)
    assert b.committed_usd == bound


def test_unexpected_charge_is_reported_before_stopping() -> None:
    b = budget()
    bound = b.reserve("m", 1000, 1000)
    with pytest.raises(BudgetExceeded):
        b.settle(bound, "5")
    assert b.actual_usd == b.committed_usd == Decimal("5")


def test_different_model_is_not_sent_at_the_old_price() -> None:
    with pytest.raises(BudgetExceeded):
        budget().reserve("expensive", 1000, 1000)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0"])
def test_bad_limits_fail_early(value: str) -> None:
    with pytest.raises(ValueError):
        budget(value)


async def test_http_request_is_blocked_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import BaseModel

    from evals.budget import BudgetedLLM
    from gr.llm.openai_compat import OpenAICompatibleLLM
    from gr.settings import Settings

    class Answer(BaseModel):
        text: str

    async def unexpected(*args: object, **kwargs: object) -> object:
        pytest.fail("paid transport called")

    monkeypatch.setattr(OpenAICompatibleLLM, "_request", unexpected)
    settings = Settings(_env_file=None, auth_enabled=False, llm_max_retries=0)
    client = BudgetedLLM(settings)
    client.budget = budget("0.001")
    try:
        with pytest.raises(BudgetExceeded):
            await client._request(
                "m", Answer, [{"role": "user", "content": "text"}], 16000, "json_schema"
            )
    finally:
        await client.client.close()


async def test_a_provider_failure_is_tried_again_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import BaseModel

    from evals.budget import BudgetedLLM
    from gr.llm.base import LLMUnavailableError
    from gr.llm.openai_compat import OpenAICompatibleLLM, _Response
    from gr.settings import Settings

    class Answer(BaseModel):
        text: str

    outcomes: list[object] = [LLMUnavailableError(), LLMUnavailableError()]

    async def flaky(*args: object, **kwargs: object) -> object:
        if outcomes:
            failure = outcomes.pop(0)
            assert isinstance(failure, LLMUnavailableError)
            raise failure
        bill = {"usage": {"cost": 0.0001}}
        return _Response(content='{"text": "ok"}', finish_reason="stop", input_tokens=1,
                         output_tokens=1, estimated=False, raw=bill)  # fmt: skip

    async def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(OpenAICompatibleLLM, "_request", flaky)
    monkeypatch.setattr("evals.budget.asyncio.sleep", no_pause)
    client = BudgetedLLM(Settings(_env_file=None, auth_enabled=False, llm_max_retries=0))
    client.budget = budget("1")
    try:
        response = await client._request(
            "m", Answer, [{"role": "user", "content": "text"}], 100, "json_schema"
        )
    finally:
        await client.client.close()
    assert response.content == '{"text": "ok"}'
    assert client.budget.requests == 3  # every try reserved, as any request


def test_a_second_model_is_reserved_at_its_own_price() -> None:
    big = {"big": (Decimal("4"), Decimal("10"))}
    b = Budget(Decimal("3"), Decimal("1"), Decimal("1"), "m", big)
    cheap, dear = b.reserve("m", 1000, 1000), b.reserve("big", 1000, 1000)
    assert dear == (Decimal(5096) * 4 + Decimal(1000) * 10) / 1_000_000 > cheap
    with pytest.raises(BudgetExceeded):
        b.reserve("other", 1000, 1000)


def test_a_budget_keeps_the_discovery_model() -> None:
    # T059 follow-up: the budget's overrides once replaced --discovery-model, S3 ran on Haiku.
    from evals.target import llm_overrides

    overrides = llm_overrides(budget(), s6=True, discovery_model="big")
    assert overrides["llm_model_discovery"] == "big"
    assert overrides["llm_model_extraction"] == overrides["llm_model_analysis"] == "m"
    assert "llm_model_discovery" not in llm_overrides(budget(), s6=False, discovery_model=None)
