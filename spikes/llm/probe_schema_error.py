"""Spike T074, step 3: why does the provider reject a large strict schema? (no generation cost)

Usage: uv run python spikes/llm/probe_schema_error.py
"""

import asyncio
import json
import sys
from pathlib import Path

from openai import APIStatusError, AsyncOpenAI

sys.path.insert(0, str(Path(__file__).parent))
from probe_detail import DetailOut

from gr.llm.cache import strict_json_schema
from gr.settings import Settings


async def try_schema(client: AsyncOpenAI, model: str, name: str, schema: dict[str, object]) -> None:
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Верни пустой процесс."}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },  # type: ignore[typeddict-item]
            max_tokens=20,
        )
        print(f"{name}: accepted (finish={completion.choices[0].finish_reason})")
    except APIStatusError as exc:
        body = exc.response.text if exc.response is not None else str(exc)
        print(f"{name}: HTTP {exc.status_code}: {body[:900]}")


async def main() -> None:
    settings = Settings()
    client = AsyncOpenAI(
        base_url=str(settings.llm_base_url),
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "none",
        max_retries=0,
        timeout=60,
    )
    schema = strict_json_schema(DetailOut)
    text = json.dumps(schema, ensure_ascii=False)
    print(
        f"schema: {len(text)} chars, $defs: {len(schema.get('$defs', {}))}, "
        f"anyOf: {text.count('anyOf')}, enum values: {text.count(chr(34) + 'enum')}"
    )
    await try_schema(client, str(settings.llm_model_extraction), "DetailOut", schema)


if __name__ == "__main__":
    asyncio.run(main())
