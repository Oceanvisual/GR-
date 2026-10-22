"""OpenAI-compatible chat completions with structured output, repair and accounting (R-08)."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal, cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, ValidationError

from gr.domain.enums import StageName
from gr.domain.ids import new_id
from gr.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMOutputTruncatedError,
    LLMResult,
    LLMTimeoutError,
    LLMUnavailableError,
)
from gr.llm.cache import request_hash, strict_json_schema
from gr.llm.options import request_options
from gr.llm.policy import check_data_policy
from gr.llm.prompts import JSON_OBJECT_INSTRUCTION, REPAIR_INSTRUCTION
from gr.logging import get_logger
from gr.settings import Settings
from gr.storage.repositories import LlmCall, LlmCallRepository

log = get_logger(__name__)

Mode = Literal["json_schema", "json_object"]
EXTRACTION_STAGES = frozenset({StageName.DISCOVER, StageName.DETAIL, StageName.VALIDATE})
_CHARS_PER_TOKEN = 3  # rough for Russian text; used only when the provider omits `usage`
_ERROR_DETAIL_LIMIT = 20
# Broken JSON is asked again from scratch, slightly warmer: shown its own answer, the model
# copied it byte for byte (T059). Schema errors keep the repair with the answer and errors.
RETRY_TEMPERATURE = 0.3
_JSON_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```\Z", re.IGNORECASE)


def unwrap_json_fence(content: str) -> str:
    """Remove one complete Markdown wrapper, never prose or malformed JSON content."""
    match = _JSON_FENCE.fullmatch(content.strip())
    return match["body"] if match else content


class _Response(BaseModel):
    content: str | None
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    estimated: bool
    raw: dict[str, Any]


class OpenAICompatibleLLM:
    def __init__(
        self,
        settings: Settings,
        calls: LlmCallRepository | None = None,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.settings = settings
        self.calls = calls
        self.client = client or AsyncOpenAI(
            base_url=str(settings.llm_base_url) if settings.llm_base_url else None,
            # Local servers (Ollama, vLLM) ignore the key, but the SDK requires one.
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "none",
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
        configured = settings.llm_structured_mode
        self._mode: Mode = "json_object" if configured == "json_object" else "json_schema"
        self._may_fall_back = configured == "auto"
        # Schemas the endpoint refused in json_schema mode: they go as json_object from then on.
        # The refusal is per schema (T059: the S3 schema passed, the larger S4 one never did).
        self._refused: set[str] = set()
        self._semaphore = asyncio.Semaphore(settings.llm_concurrency)

    def options_for(self, stage: StageName) -> dict[str, Any]:
        """Provider-specific fields of this stage's requests (config/llm.toml)."""
        host = self.settings.llm_base_url.host if self.settings.llm_base_url else None
        return request_options(stage, host, self.model_for(stage))

    def model_for(self, stage: StageName) -> str:
        if stage == StageName.DISCOVER and self.settings.llm_model_discovery:
            return self.settings.llm_model_discovery
        model = (
            self.settings.llm_model_extraction
            if stage in EXTRACTION_STAGES
            else self.settings.llm_model_analysis
        )
        if not model:
            raise LLMUnavailableError(f"Не задана модель для этапа {stage.value}.")
        return model

    async def generate[T: BaseModel](
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        stage: StageName,
        max_tokens: int,
        run_id: str,
    ) -> LLMResult[T]:
        check_data_policy(self.settings)  # before any byte of the document leaves
        model = self.model_for(stage)
        options = self.options_for(stage)
        key = request_hash(model, schema, system, user, options)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        totals = {"in": 0, "out": 0, "latency": 0}
        estimated = False
        temperature = 0.0
        for attempt in (1, 2):
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    response = await self._complete(
                        model, schema, messages, max_tokens, stage, options, temperature
                    )
            except LLMError as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                await self._record(run_id, stage, model, attempt, exc.code, None, elapsed, key)
                raise
            latency_ms = int((time.perf_counter() - started) * 1000)
            totals["in"] += response.input_tokens
            totals["out"] += response.output_tokens
            totals["latency"] += latency_ms
            estimated = estimated or response.estimated

            status = "ok"
            error: LLMError | None = None
            value: T | None = None
            if response.finish_reason == "length":
                status, error = "truncated", LLMOutputTruncatedError()
            else:
                try:
                    value = schema.model_validate_json(unwrap_json_fence(response.content or ""))
                except ValidationError as exc:
                    status = "invalid"
                    error = LLMInvalidOutputError()
                    if all(e["type"] == "json_invalid" for e in exc.errors()):
                        temperature = RETRY_TEMPERATURE
                    elif attempt == 1:
                        messages += [
                            {"role": "assistant", "content": response.content or ""},
                            {"role": "user", "content": _repair_message(exc)},
                        ]
            await self._record(run_id, stage, model, attempt, status, response, latency_ms, key)
            if value is not None:
                return LLMResult(
                    value=value,
                    model=model,
                    input_tokens=totals["in"],
                    output_tokens=totals["out"],
                    latency_ms=totals["latency"],
                    attempts=attempt,
                    cached=False,
                    tokens_estimated=estimated,
                )
            if isinstance(error, LLMOutputTruncatedError) or attempt == 2:
                assert error is not None
                raise error
        raise AssertionError("unreachable")

    async def _complete(
        self,
        model: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        max_tokens: int,
        stage: StageName,
        options: dict[str, Any],
        temperature: float = 0.0,
    ) -> _Response:
        mode: Mode = "json_object" if schema.__name__ in self._refused else self._mode
        try:
            return await self._request(
                model, schema, messages, max_tokens, mode, options, temperature
            )
        except openai.BadRequestError as exc:
            log.warning("llm.rejected", stage=stage.value, model=model, mode=mode,
                        reason=str(getattr(exc, "message", exc))[:200])  # fmt: skip
            if not (self._may_fall_back and mode == "json_schema"):
                raise LLMUnavailableError("Сервис LLM отклонил запрос (код 400).") from None
            rejected = getattr(exc, "code", None) or type(exc).__name__
            # `auto`: perhaps the endpoint does not take json_schema. Retry once in json_object.
            try:
                response = await self._request(
                    model, schema, messages, max_tokens, "json_object", options, temperature
                )
            except openai.BadRequestError as again:
                # Rejected either way: the request itself is at fault (context length, model
                # id, max_tokens), not the structured mode, which therefore stays as it was.
                log.warning("llm.rejected", stage=stage.value, model=model, mode="json_object",
                            reason=str(getattr(again, "message", again))[:200])  # fmt: skip
                raise LLMUnavailableError("Сервис LLM отклонил запрос (код 400).") from None
            log.warning("llm.structured_mode_fallback", stage=stage.value, schema=schema.__name__,
                        to="json_object", reason=rejected)  # fmt: skip
            self._refused.add(schema.__name__)
            return response

    async def _request(
        self,
        model: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        max_tokens: int,
        mode: Mode,
        options: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> _Response:
        strict = strict_json_schema(schema)
        if mode == "json_schema":
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": strict, "strict": True},
            }
            sent = messages
        else:
            response_format = {"type": "json_object"}
            instruction = JSON_OBJECT_INSTRUCTION.format(
                schema=json.dumps(strict, ensure_ascii=False)
            )
            sent = [{**messages[0], "content": messages[0]["content"] + instruction}, *messages[1:]]
        try:
            completion = await self.client.chat.completions.create(
                model=model,
                messages=cast("list[ChatCompletionMessageParam]", sent),
                response_format=cast("ResponseFormat", response_format),
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=options or None,
            )
        except openai.APITimeoutError:
            raise LLMTimeoutError(
                f"LLM не ответила за {self.settings.llm_timeout_s:g} с. "
                "Повторите позже или уменьшите документ."
            ) from None
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise LLMAuthError() from None
        except openai.BadRequestError:
            raise
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"Сервис LLM недоступен (код {exc.status_code}).") from None
        except openai.APIConnectionError:
            raise LLMUnavailableError("Сервис LLM недоступен (нет соединения).") from None

        choice = completion.choices[0] if completion.choices else None
        content = choice.message.content if choice else None
        usage = completion.usage
        if usage is not None:
            in_tokens, out_tokens, estimated = usage.prompt_tokens, usage.completion_tokens, False
        else:
            prompt_chars = sum(len(m["content"]) for m in sent)
            in_tokens = prompt_chars // _CHARS_PER_TOKEN
            out_tokens = len(content or "") // _CHARS_PER_TOKEN
            estimated = True
        return _Response(
            content=content,
            finish_reason=choice.finish_reason if choice else None,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            estimated=estimated,
            raw=completion.model_dump(mode="json"),
        )

    async def _record(
        self,
        run_id: str,
        stage: StageName,
        model: str,
        attempt: int,
        status: str,
        response: _Response | None,
        latency_ms: int,
        key: str,
    ) -> None:
        # Texts never reach the log; the raw response is kept only in llm_calls (FR-030).
        in_tokens = response.input_tokens if response else 0
        out_tokens = response.output_tokens if response else 0
        estimated = response.estimated if response else False
        log.info(
            "llm.call",
            run_id=run_id,
            stage=stage.value,
            model=model,
            attempt=attempt,
            status=status,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            tokens_estimated=estimated,
            latency_ms=latency_ms,
            request_hash=key[:16],
        )
        if self.calls is not None:
            await self.calls.add(
                LlmCall(
                    id=new_id("llm"),
                    run_id=run_id,
                    stage=stage.value,
                    model=model,
                    attempt=attempt,
                    status=status,
                    in_tokens=in_tokens,
                    out_tokens=out_tokens,
                    tokens_estimated=estimated,
                    latency_ms=latency_ms,
                    request_hash=key,
                    response=response.raw if response else None,
                    created_at=datetime.now(UTC),
                )
            )


def _repair_message(exc: ValidationError) -> str:
    # Paths and messages only: `input` would echo the model's (document-derived) text back.
    lines = [
        f"- {'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()[:_ERROR_DETAIL_LIMIT]
    ]
    return REPAIR_INSTRUCTION.format(errors="\n".join(lines))
