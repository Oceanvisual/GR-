"""Scripted LLM for tests and offline runs: no network, deterministic, counts calls."""

import json
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from gr.domain.enums import StageName
from gr.llm.base import LLMInvalidOutputError, LLMResult

# A scripted reply: a ready object, a dict or JSON text to validate, an exception to raise,
# or a function of the request returning any of those.
Reply = BaseModel | dict[str, Any] | str | Exception | Callable[["FakeCall"], Any]


@dataclass(frozen=True)
class FakeCall:
    stage: StageName
    schema: type[BaseModel]
    system: str
    user: str
    max_tokens: int
    run_id: str


class FakeScenarioExhaustedError(AssertionError):
    pass


class FakeLLM:
    def __init__(
        self,
        replies: dict[StageName, list[Reply]] | None = None,
        *,
        model: str = "fake-llm",
        default: Reply | None = None,
    ) -> None:
        self.model = model
        self._queues: dict[StageName, deque[Reply]] = defaultdict(deque)
        for stage, items in (replies or {}).items():
            self._queues[stage].extend(items)
        self._default = default
        self.calls: list[FakeCall] = []

    def add(self, stage: StageName, *replies: Reply) -> None:
        self._queues[stage].extend(replies)

    def calls_for(self, stage: StageName) -> list[FakeCall]:
        return [call for call in self.calls if call.stage == stage]

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
        call = FakeCall(stage, schema, system, user, max_tokens, run_id)
        self.calls.append(call)
        if self._queues[stage]:
            reply = self._queues[stage].popleft()
        elif self._default is not None:
            reply = self._default
        else:
            raise FakeScenarioExhaustedError(f"no scripted reply for stage {stage}")
        if callable(reply) and not isinstance(reply, BaseModel | Exception):
            reply = reply(call)
        if isinstance(reply, Exception):
            raise reply
        value = self._validate(schema, reply)
        text = value.model_dump_json()
        return LLMResult(
            value=value,
            model=self.model,
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(text) // 4,
            latency_ms=0,
            attempts=1,
            cached=False,
            tokens_estimated=True,
        )

    @staticmethod
    def _validate[T: BaseModel](schema: type[T], reply: Any) -> T:
        try:
            if isinstance(reply, schema):
                return reply
            if isinstance(reply, BaseModel):
                return schema.model_validate(reply.model_dump())
            if isinstance(reply, str):
                return schema.model_validate_json(reply)
            return schema.model_validate(json.loads(json.dumps(reply)))
        except ValidationError as exc:
            raise LLMInvalidOutputError() from exc
