"""Explicit S6 no-change reply for existing tests focused on ingestion and S3–S5."""

from typing import Any

from gr.domain.enums import StageName
from gr.llm.fake import FakeCall, FakeScenarioExhaustedError


def no_change_for_s6(call: FakeCall) -> dict[str, Any]:
    if call.stage != StageName.OPPORTUNITIES:
        raise FakeScenarioExhaustedError(f"no scripted reply for {call.stage}")
    return {"no_change": {"applies": True, "justification": "Тест проверяет карту процессов."}}
