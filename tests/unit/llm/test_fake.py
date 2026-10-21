"""FakeLLM scenarios (T012)."""

import pytest
from pydantic import BaseModel

from gr.domain.enums import StageName
from gr.llm.base import LLMInvalidOutputError, LLMTimeoutError, StructuredLLM
from gr.llm.fake import FakeCall, FakeLLM, FakeScenarioExhaustedError


class Answer(BaseModel):
    items: list[str]


async def _ask(llm: StructuredLLM, stage: StageName = StageName.DISCOVER) -> Answer:
    result = await llm.generate(
        schema=Answer, system="sys", user="user text", stage=stage, max_tokens=100, run_id="run_1"
    )
    return result.value


async def test_replies_in_order_per_stage() -> None:
    llm = FakeLLM(
        {
            StageName.DISCOVER: [Answer(items=["a"]), {"items": ["b"]}, '{"items": ["c"]}'],
            StageName.DETAIL: [{"items": ["d"]}],
        }
    )
    assert (await _ask(llm, StageName.DETAIL)).items == ["d"]
    assert [(await _ask(llm)).items for _ in range(3)] == [["a"], ["b"], ["c"]]
    assert len(llm.calls) == 4
    assert len(llm.calls_for(StageName.DISCOVER)) == 3
    assert llm.calls[0].user == "user text" and llm.calls[0].run_id == "run_1"


async def test_exhausted_scenario_fails_loudly() -> None:
    with pytest.raises(FakeScenarioExhaustedError):
        await _ask(FakeLLM())


async def test_default_reply_and_callable() -> None:
    def echo(call: FakeCall) -> dict[str, list[str]]:
        return {"items": [call.stage.value, call.system]}

    llm = FakeLLM(default=echo)
    assert (await _ask(llm, StageName.OPPORTUNITIES)).items == ["opportunities", "sys"]


async def test_scripted_errors() -> None:
    llm = FakeLLM({StageName.DISCOVER: [LLMTimeoutError(), {"items": 1}]})
    with pytest.raises(LLMTimeoutError):
        await _ask(llm)
    with pytest.raises(LLMInvalidOutputError):
        await _ask(llm)


async def test_result_metadata() -> None:
    llm = FakeLLM(default={"items": []})
    result = await llm.generate(
        schema=Answer,
        system="s" * 40,
        user="u" * 40,
        stage=StageName.DETAIL,
        max_tokens=1,
        run_id="r",
    )
    assert result.model == "fake-llm"
    assert result.input_tokens == 20 and result.tokens_estimated
    assert not result.cached and result.attempts == 1
