"""Conservative request budget for explicitly authorized live evals; no service defaults."""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from gr.llm.base import LLMError, LLMUnavailableError
from gr.llm.cache import strict_json_schema
from gr.llm.openai_compat import Mode, OpenAICompatibleLLM, _Response


class BudgetExceeded(LLMError):
    code = "eval_budget_exceeded"
    default_message = "Лимит стоимости eval исчерпан; новый запрос не отправлен."


@dataclass
class Budget:
    limit_usd: Decimal
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    allowed_model: str  # every budgeted stage, unless a stage has its own model below
    # Other models a stage may use (--discovery-model): model → (input, output) per Mtok.
    other_models: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    committed_usd: Decimal = Decimal(0)
    actual_usd: Decimal = Decimal(0)
    requests: int = 0

    def __post_init__(self) -> None:
        prices = [p for pair in self.other_models.values() for p in pair]
        for value in (self.limit_usd, self.input_per_mtok, self.output_per_mtok, *prices):
            if not value.is_finite() or value <= 0:
                raise ValueError("Budget and conservative prices must be finite and positive")

    def prices(self, model: str) -> tuple[Decimal, Decimal]:
        if model == self.allowed_model:
            return self.input_per_mtok, self.output_per_mtok
        if model in self.other_models:
            return self.other_models[model]
        raise BudgetExceeded("Модель отсутствует в тарифном снимке eval.")

    def reserve(self, model: str, input_bytes: int, output_tokens: int) -> Decimal:
        if input_bytes < 0 or output_tokens <= 0:
            raise ValueError("request size must be nonnegative and output limit positive")
        input_per_mtok, output_per_mtok = self.prices(model)
        # One token per UTF-8 byte plus 4096 tokens for protocol overhead. Prices must
        # include the highest applicable endpoint/context/cache-write tariff.
        bound = (
            Decimal(input_bytes + 4096) * input_per_mtok + Decimal(output_tokens) * output_per_mtok
        ) / 1_000_000
        if self.committed_usd + bound > self.limit_usd:
            raise BudgetExceeded()
        self.committed_usd += bound
        self.requests += 1
        return bound

    def settle(self, reserved: Decimal, cost: object) -> None:
        if cost is None:
            return  # no bill: keep the full conservative reservation
        try:
            actual = Decimal(str(cost))
        except InvalidOperation:
            raise BudgetExceeded("Провайдер вернул некорректную стоимость.") from None
        if not actual.is_finite() or actual < 0:
            raise BudgetExceeded(
                "Провайдер вернул стоимость вне тарифного снимка; eval остановлен."
            )
        self.committed_usd += actual - reserved
        self.actual_usd += actual
        if actual > reserved:
            raise BudgetExceeded("Провайдер превысил тарифный снимок; eval остановлен.")

    def report(self) -> dict[str, str | int]:
        return {
            "limit_usd": str(self.limit_usd),
            "model": self.allowed_model,
            "conservative_input_per_mtok": str(self.input_per_mtok),
            "conservative_output_per_mtok": str(self.output_per_mtok),
            **{
                f"{model} (input/output per Mtok)": f"{i}/{o}"
                for model, (i, o) in self.other_models.items()
            },
            "committed_upper_bound_usd": str(self.committed_usd),
            "reported_actual_usd": str(self.actual_usd),
            "requests": self.requests,
        }


TRANSIENT_RETRIES = (2.0, 6.0)  # seconds before the 2nd and 3rd try of a provider failure


class BudgetedLLM(OpenAICompatibleLLM):
    """HTTP retries of the SDK are off (they would bypass the budget): a provider failure
    (5xx, 429, no connection) is tried again here, each try reserved like any request."""

    budget: Budget

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
        for pause in (*TRANSIENT_RETRIES, None):
            try:
                return await self._budgeted(
                    model, schema, messages, max_tokens, mode, options, temperature
                )
            except LLMUnavailableError:
                if pause is None:
                    raise
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _budgeted(
        self,
        model: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        max_tokens: int,
        mode: Mode,
        options: dict[str, Any] | None,
        temperature: float,
    ) -> _Response:
        if self.settings.llm_max_retries:
            raise BudgetExceeded("Для ограниченного eval отключите автоматические HTTP-повторы.")
        encoded = json.dumps(
            {"messages": messages, "schema": strict_json_schema(schema)},
            ensure_ascii=False,
        ).encode("utf-8")
        reserved = self.budget.reserve(model, len(encoded), max_tokens)
        # A failed request retains its reservation: an interrupted response might be billed.
        response = await super()._request(
            model, schema, messages, max_tokens, mode, options, temperature
        )
        self.budget.settle(reserved, response.raw.get("usage", {}).get("cost"))
        return response
