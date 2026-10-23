"""Structured LLM protocol and error codes (contracts/llm.md, «Общий протокол»)."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from gr.domain.enums import StageName


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    value: T  # validated Pydantic object
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int
    cached: bool
    tokens_estimated: bool = False


class StructuredLLM(Protocol):
    async def generate[T: BaseModel](
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        stage: StageName,
        max_tokens: int,
        run_id: str,
    ) -> LLMResult[T]: ...


class LLMError(Exception):
    """Base for failures that end a stage; `code` goes to `StageState.error.code`."""

    code = "llm_error"
    default_message = "Ошибка LLM."

    def __init__(self, message_ru: str | None = None) -> None:
        self.message_ru = message_ru or self.default_message
        super().__init__(f"{self.code}: {self.message_ru}")


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    default_message = "LLM не ответила вовремя. Повторите позже или уменьшите документ."


class LLMUnavailableError(LLMError):
    code = "llm_unavailable"
    default_message = "Сервис LLM недоступен."


class LLMAuthError(LLMError):
    code = "llm_auth_failed"
    default_message = "Ошибка доступа к LLM: проверьте ключ API."


class LLMInvalidOutputError(LLMError):
    code = "llm_invalid_output"
    default_message = "Ответ LLM не соответствует схеме."


class LLMOutputTruncatedError(LLMError):
    code = "llm_output_truncated"
    default_message = "Ответ LLM обрезан: увеличьте лимит или уменьшите фрагмент."


class LLMPolicyError(LLMError):
    """Raised before any data leaves the service (NFR-009); the API answers 503."""


class LLMNotConfiguredError(LLMPolicyError):
    code = "llm_not_configured"
    default_message = "LLM не настроена: задайте LLM_* в конфигурации."


class ExternalLLMNotAllowedError(LLMPolicyError):
    code = "external_llm_not_allowed"
    default_message = "Политика данных запрещает внешний LLM-сервис."
