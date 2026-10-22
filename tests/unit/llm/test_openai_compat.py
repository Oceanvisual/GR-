"""OpenAI-compatible client over a mock transport, no network (R-08, T013)."""

import io
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from gr.domain.enums import StageName
from gr.llm.base import (
    ExternalLLMNotAllowedError,
    LLMAuthError,
    LLMInvalidOutputError,
    LLMOutputTruncatedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from gr.llm.cache import request_hash, strict_json_schema
from gr.llm.openai_compat import OpenAICompatibleLLM
from gr.logging import configure_logging
from gr.settings import Settings
from gr.storage.db import Database
from gr.storage.repositories import LlmCallRepository

SECRET_TEXT = "Заявитель Иванов И.И. паспорт 4510 123456"


class Step(BaseModel):
    name: str
    default: str | None  # a field literally named `default` must survive strictification


class Answer(BaseModel):
    title: str
    steps: list[Step]


VALID = {"title": "ok", "steps": [{"name": "a", "default": None}]}

Handler = Callable[[httpx2.Request], httpx2.Response]


def completion(content: str | None, *, finish: str = "stop", usage: bool = True) -> httpx2.Response:
    body: dict[str, Any] = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m-extract",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return httpx2.Response(200, json=body)


def error(status: int, message: str = "error") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": message, "type": "x"}})


class Recorder:
    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
async def calls(tmp_path: Path) -> AsyncIterator[LlmCallRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'gr.db'}")
    await database.init()
    yield LlmCallRepository(database)
    await database.dispose()


@pytest.fixture
def llm_settings(make_settings: Callable[..., Settings]) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        fields: dict[str, Any] = {
            "llm_data_policy": "local_only",
            "llm_base_url": "http://localhost:9999/v1",
            "llm_model_extraction": "m-extract",
            "llm_model_analysis": "m-analyze",
            "llm_max_retries": 0,
            "llm_structured_mode": "auto",
        }
        return make_settings(**(fields | overrides))

    return factory


def make_llm(
    settings: Settings, recorder: Recorder, calls: LlmCallRepository | None = None
) -> OpenAICompatibleLLM:
    client = AsyncOpenAI(
        base_url=str(settings.llm_base_url),
        api_key="test",
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_s,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
    )
    return OpenAICompatibleLLM(settings, calls, client=client)


async def ask(llm: OpenAICompatibleLLM, stage: StageName = StageName.DETAIL) -> Any:
    return await llm.generate(
        schema=Answer,
        system="system rules",
        user=f"<document>{SECRET_TEXT}</document>",
        stage=stage,
        max_tokens=500,
        run_id="run_1",
    )


async def test_success_with_json_schema(
    llm_settings: Callable[..., Settings], calls: LlmCallRepository
) -> None:
    recorder = Recorder(completion(json.dumps(VALID)))
    result = await ask(make_llm(llm_settings(), recorder, calls))

    assert result.value == Answer.model_validate(VALID)
    assert (result.input_tokens, result.output_tokens, result.attempts) == (10, 5, 1)
    assert not result.cached and not result.tokens_estimated
    sent = recorder.requests[0]
    assert sent["model"] == "m-extract" and sent["temperature"] == 0
    assert sent["max_tokens"] == 500
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True

    stored = await calls.list_for_run("run_1")
    assert [(c.status, c.attempt) for c in stored] == [("ok", 1)]
    assert stored[0].response is not None and stored[0].response["id"] == "cmpl-1"


async def test_analysis_stage_uses_analysis_model(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(completion(json.dumps(VALID)))
    await ask(make_llm(llm_settings(), recorder), StageName.OPPORTUNITIES)
    assert recorder.requests[0]["model"] == "m-analyze"


async def test_auto_falls_back_to_json_object_and_remembers(
    llm_settings: Callable[..., Settings],
) -> None:
    recorder = Recorder(
        error(400, "response_format json_schema is not supported"),
        completion(json.dumps(VALID)),
        completion(json.dumps(VALID)),
    )
    llm = make_llm(llm_settings(), recorder)
    assert (await ask(llm)).value.title == "ok"
    assert (await ask(llm)).value.title == "ok"
    formats = [r["response_format"]["type"] for r in recorder.requests]
    assert formats == ["json_schema", "json_object", "json_object"]
    system = recorder.requests[1]["messages"][0]["content"]
    assert system.startswith("system rules") and '"additionalProperties": false' in system


class Card(BaseModel):
    title: str


async def test_a_refused_schema_falls_back_alone(llm_settings: Callable[..., Settings]) -> None:
    # T059: the provider took the S3 schema in json_schema mode and refused the larger S4 one
    # every time; only that schema goes as json_object, without a refused request each call.
    recorder = Recorder(
        error(400, "schema too complex"),  # Answer: refused…
        completion(json.dumps(VALID)),  # …and asked again as json_object
        completion(json.dumps(VALID)),  # Answer again: straight to json_object
        completion(json.dumps({"title": "card"})),  # Card keeps json_schema
    )
    llm = make_llm(llm_settings(), recorder)
    assert (await ask(llm)).value.title == "ok"
    assert (await ask(llm)).value.title == "ok"
    card = await llm.generate(schema=Card, system="system rules", user="<document>x</document>",
                              stage=StageName.DISCOVER, max_tokens=100, run_id="run_1")  # fmt: skip
    assert card.value.title == "card"
    formats = [r["response_format"]["type"] for r in recorder.requests]
    assert formats == ["json_schema", "json_object", "json_object", "json_schema"]


async def test_strict_json_schema_mode_does_not_fall_back(
    llm_settings: Callable[..., Settings],
) -> None:
    recorder = Recorder(error(400))
    with pytest.raises(LLMUnavailableError, match="400"):
        await ask(make_llm(llm_settings(llm_structured_mode="json_schema"), recorder))
    assert len(recorder.requests) == 1


async def test_invalid_then_repair_succeeds(
    llm_settings: Callable[..., Settings], calls: LlmCallRepository
) -> None:
    recorder = Recorder(completion('{"title": "x"}'), completion(json.dumps(VALID)))
    result = await ask(make_llm(llm_settings(), recorder, calls))

    assert result.attempts == 2
    assert (result.input_tokens, result.output_tokens) == (20, 10)
    repair = recorder.requests[1]["messages"]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == '{"title": "x"}'
    assert "steps" in repair[3]["content"] and "JSON" in repair[3]["content"]
    assert [c.status for c in await calls.list_for_run("run_1")] == ["invalid", "ok"]


async def test_broken_json_is_asked_again_from_scratch(
    llm_settings: Callable[..., Settings],
) -> None:
    # T059: shown its broken answer, the model copied it byte for byte; asked again from
    # scratch and slightly warmer, it answers anew.
    recorder = Recorder(completion('{"title": "ok", "steps": [}'), completion(json.dumps(VALID)))
    result = await ask(make_llm(llm_settings(), recorder))
    assert result.attempts == 2
    first, second = recorder.requests
    assert second["messages"] == first["messages"] and len(second["messages"]) == 2
    assert (first["temperature"], second["temperature"]) == (0, 0.3)


async def test_invalid_twice_fails(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(completion("not json"), completion('{"title": 1}'))
    with pytest.raises(LLMInvalidOutputError) as exc:
        await ask(make_llm(llm_settings(), recorder))
    assert exc.value.code == "llm_invalid_output"
    assert len(recorder.requests) == 2


async def test_truncated_output_is_not_repaired(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(completion('{"title": "x", "st', finish="length"))
    with pytest.raises(LLMOutputTruncatedError):
        await ask(make_llm(llm_settings(), recorder))
    assert len(recorder.requests) == 1


async def test_timeout(llm_settings: Callable[..., Settings], calls: LlmCallRepository) -> None:
    recorder = Recorder(httpx2.ReadTimeout("slow"))
    with pytest.raises(LLMTimeoutError) as exc:
        await ask(make_llm(llm_settings(llm_timeout_s=7), recorder, calls))
    assert exc.value.code == "llm_timeout" and "7 с" in exc.value.message_ru
    assert [c.status for c in await calls.list_for_run("run_1")] == ["llm_timeout"]


@pytest.mark.parametrize(("status", "expected"), [(401, LLMAuthError), (403, LLMAuthError)])
async def test_auth_errors(
    llm_settings: Callable[..., Settings], status: int, expected: type[Exception]
) -> None:
    with pytest.raises(expected) as exc:
        await ask(make_llm(llm_settings(), Recorder(error(status))))
    assert getattr(exc.value, "code", None) == "llm_auth_failed"


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_unavailable(llm_settings: Callable[..., Settings], status: int) -> None:
    with pytest.raises(LLMUnavailableError, match=str(status)):
        await ask(make_llm(llm_settings(), Recorder(error(status))))


async def test_connection_error(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(httpx2.ConnectError("refused"))
    with pytest.raises(LLMUnavailableError, match="соединения"):
        await ask(make_llm(llm_settings(), recorder))


async def test_missing_usage_is_estimated(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(completion(json.dumps(VALID), usage=False))
    result = await ask(make_llm(llm_settings(), recorder))
    assert result.tokens_estimated
    assert result.input_tokens > 0 and result.output_tokens > 0


async def test_policy_checked_before_sending(llm_settings: Callable[..., Settings]) -> None:
    recorder = Recorder(completion(json.dumps(VALID)))
    settings = llm_settings(llm_base_url="https://api.example.com/v1")
    with pytest.raises(ExternalLLMNotAllowedError):
        await ask(make_llm(settings, recorder))
    assert recorder.requests == []


async def test_logs_carry_no_text(llm_settings: Callable[..., Settings]) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream)
    recorder = Recorder(completion('{"title": "Иванов"}'), completion(json.dumps(VALID)))
    await ask(make_llm(llm_settings(), recorder))
    output = stream.getvalue()
    assert output.count('"event": "llm.call"') == 2
    assert "Иванов" not in output and "system rules" not in output
    assert SECRET_TEXT not in output


def test_strict_schema_closes_objects() -> None:
    schema = strict_json_schema(Answer)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["title", "steps"]
    step = schema["$defs"]["Step"]
    assert step["required"] == ["name", "default"]
    assert "default" in step["properties"]
    assert "title" not in step


async def test_bad_request_unrelated_to_mode_keeps_json_schema(
    llm_settings: Callable[..., Settings],
) -> None:
    recorder = Recorder(
        error(400, "maximum context length exceeded"),
        error(400, "maximum context length exceeded"),
        completion(json.dumps(VALID)),
    )
    llm = make_llm(llm_settings(), recorder)
    with pytest.raises(LLMUnavailableError, match="400"):
        await ask(llm)
    assert (await ask(llm)).value.title == "ok"
    formats = [r["response_format"]["type"] for r in recorder.requests]
    assert formats == ["json_schema", "json_object", "json_schema"]


def test_strict_schema_refuses_dict_fields() -> None:
    class Scores(BaseModel):
        by_criterion: dict[str, int]

    with pytest.raises(ValueError, match="dict fields"):
        strict_json_schema(Scores)


@pytest.mark.parametrize(
    ("base_url", "sent"),
    [
        ("https://openrouter.ai/api/v1", {"enabled": False}),
        ("http://localhost:9999/v1", None),
    ],
)
async def test_reasoning_options_only_for_openrouter(
    llm_settings: Callable[..., Settings], base_url: str, sent: dict[str, Any] | None
) -> None:
    recorder = Recorder(completion(json.dumps(VALID)))
    settings = llm_settings(llm_base_url=base_url, llm_data_policy="external_allowed",
                            llm_api_key="k")  # fmt: skip
    await ask(make_llm(settings, recorder), stage=StageName.DISCOVER)
    assert recorder.requests[0].get("reasoning") == sent


def test_request_options_change_the_cache_key() -> None:
    plain = request_hash("m", Answer, "s", "u")
    assert plain == request_hash("m", Answer, "s", "u", {})
    assert plain != request_hash("m", Answer, "s", "u", {"reasoning": {"enabled": False}})


@pytest.mark.parametrize("opening", ["```json", "```", "```JSON"])
async def test_complete_json_fence_needs_no_repair(
    llm_settings: Callable[..., Settings], calls: LlmCallRepository, opening: str
) -> None:
    content = f"  \n{opening}\n{json.dumps(VALID)}\n```\n "
    recorder = Recorder(completion(content))
    result = await ask(make_llm(llm_settings(), recorder, calls))
    assert result.value == Answer.model_validate(VALID)
    assert result.attempts == 1
    assert len(recorder.requests) == 1
    stored = await calls.list_for_run("run_1")
    assert stored[0].status == "ok"
    assert stored[0].response is not None
    assert stored[0].response["choices"][0]["message"]["content"] == content


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"title":"x"}\n```',
        '```json\n{"title":\n```',
        f"Пояснение\n```json\n{json.dumps(VALID)}\n```",
        f"```json\n{json.dumps(VALID)}\n```\nПояснение",
        f"```json\n{json.dumps(VALID)}",
        f"```json\n{json.dumps(VALID)}\n```\n```json\n{{}}\n```",
    ],
)
async def test_fence_does_not_hide_schema_errors_or_extra_content(
    llm_settings: Callable[..., Settings], content: str
) -> None:
    recorder = Recorder(completion(content), completion(content))
    with pytest.raises(LLMInvalidOutputError):
        await ask(make_llm(llm_settings(), recorder))
    assert len(recorder.requests) == 2
