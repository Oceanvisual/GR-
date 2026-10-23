"""Spike T074, step 4: which schema features push the strict grammar over the provider limit?

Usage: uv run python spikes/llm/probe_schema_variants.py
"""

import asyncio
import copy
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import APIStatusError, AsyncOpenAI

sys.path.insert(0, str(Path(__file__).parent))
from probe_detail import DetailOut

from gr.llm.cache import strict_json_schema
from gr.settings import Settings

Schema = dict[str, Any]


def walk(node: Any, fn: Callable[[Schema], Schema]) -> Any:
    if isinstance(node, dict):
        node = fn({k: walk(v, fn) for k, v in node.items()})
    elif isinstance(node, list):
        node = [walk(v, fn) for v in node]
    return node


def no_enums(node: Schema) -> Schema:
    if "enum" in node:
        return {k: v for k, v in node.items() if k != "enum"} | {"type": "string"}
    return node


def no_nullable(node: Schema) -> Schema:
    options = node.get("anyOf")
    if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        other = next(o for o in options if o != {"type": "null"})
        return {k: v for k, v in node.items() if k != "anyOf"} | other
    return node


def inline_refs(schema: Schema) -> Schema:
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


async def accepted(client: AsyncOpenAI, model: str, schema: Schema) -> str:
    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Верни пустой процесс."}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "DetailOut", "schema": schema, "strict": True},
            },  # type: ignore[typeddict-item]
            max_tokens=16,
            extra_body={"provider": {"only": ["anthropic"]}},
        )
        return "accepted"
    except APIStatusError as exc:
        text = exc.response.text if exc.response is not None else ""
        return (
            "grammar too large"
            if "grammar is too large" in text
            else f"HTTP {exc.status_code} {text[:200]}"
        )


async def main() -> None:
    settings = Settings()
    client = AsyncOpenAI(
        base_url=str(settings.llm_base_url),
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "none",
        max_retries=0,
        timeout=60,
    )
    base = strict_json_schema(DetailOut)
    variants = {
        "as is": base,
        "no enums": walk(copy.deepcopy(base), no_enums),
        "no nullable": walk(copy.deepcopy(base), no_nullable),
        "no enums, no nullable": walk(walk(copy.deepcopy(base), no_enums), no_nullable),
        "inlined refs (size check)": inline_refs(base),
    }
    for name, schema in variants.items():
        size = len(json.dumps(inline_refs(schema), ensure_ascii=False))
        print(
            f"{name:28} inlined size {size:6} → {await accepted(client, str(settings.llm_model_extraction), schema)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
