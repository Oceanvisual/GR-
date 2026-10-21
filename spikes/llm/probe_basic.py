"""Spike T074, step 1: does the configured provider answer a strict-schema request?

Prints configuration facts (never the key), then one tiny call through OpenAICompatibleLLM
and one raw call to see usage/cost and how `temperature` is treated.

Usage: uv run python spikes/llm/probe_basic.py
"""

import asyncio
import json
import time
from urllib.parse import urlsplit

from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel

from gr.domain.enums import StageName
from gr.llm.base import LLMError
from gr.llm.cache import strict_json_schema
from gr.llm.openai_compat import OpenAICompatibleLLM
from gr.llm.policy import check_data_policy
from gr.settings import Settings


class Deadline(BaseModel):
    value: int | None
    unit: str
    quote: str


SYSTEM = "Извлеки срок из текста. Ответ — только JSON по схеме. quote — дословная цитата."
USER = (
    "<document>Заявление регистрируется в течение 3 рабочих дней со дня его поступления.</document>"
)


async def raw_call(settings: Settings, *, temperature: float | None) -> None:
    client = AsyncOpenAI(
        base_url=str(settings.llm_base_url),
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "none",
        timeout=settings.llm_timeout_s,
        max_retries=0,
    )
    kwargs: dict[str, object] = {
        "model": settings.llm_model_extraction,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "Deadline",
                "schema": strict_json_schema(Deadline),
                "strict": True,
            },
        },
        "max_tokens": 300,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    started = time.perf_counter()
    try:
        completion = await client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
    except APIStatusError as exc:
        print(f"  raw temperature={temperature}: HTTP {exc.status_code}: {str(exc)[:300]}")
        return
    raw = completion.model_dump(mode="json")
    usage = raw.get("usage") or {}
    print(
        f"  raw temperature={temperature}: {int((time.perf_counter() - started) * 1000)} ms, "
        f"finish={raw['choices'][0]['finish_reason']}, provider={raw.get('provider')}, "
        f"model={raw.get('model')}"
    )
    print(
        f"    usage: {json.dumps({k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'cost')})}"
    )
    print(f"    content: {raw['choices'][0]['message']['content']}")


async def main() -> None:
    settings = Settings()
    print("policy:", settings.llm_data_policy)
    print("host:", urlsplit(str(settings.llm_base_url)).hostname if settings.llm_base_url else None)
    print("models:", settings.llm_model_extraction, "/", settings.llm_model_analysis)
    print(
        "structured mode:",
        settings.llm_structured_mode,
        "| key set:",
        settings.llm_api_key is not None,
    )
    check_data_policy(settings)

    print("\n[1] OpenAICompatibleLLM (contract path, temperature=0):")
    llm = OpenAICompatibleLLM(settings)
    try:
        result = await llm.generate(
            schema=Deadline,
            system=SYSTEM,
            user=USER,
            stage=StageName.DETAIL,
            max_tokens=300,
            run_id="spike",
        )
        print(
            f"  ok: {result.value.model_dump()} | attempts={result.attempts} "
            f"in={result.input_tokens} out={result.output_tokens} estimated={result.tokens_estimated} "
            f"latency={result.latency_ms} ms | mode after call: {llm._mode}"
        )
    except LLMError as exc:
        print(f"  FAILED: {exc.code}: {exc.message_ru}")

    print("\n[2] raw calls:")
    await raw_call(settings, temperature=0)
    await raw_call(settings, temperature=None)


if __name__ == "__main__":
    asyncio.run(main())
