"""Response cache keyed by request content (NFR-012) and the strict schema it relies on."""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from gr.domain.enums import StageName
from gr.llm.base import LLMResult, StructuredLLM
from gr.logging import get_logger
from gr.storage.repositories import LlmCacheRepository

log = get_logger(__name__)


def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema adjusted for strict structured outputs: every object closed and
    every property required (optional ones are nullable in the LLM schemas, R-08)."""

    def visit(node: dict[str, Any]) -> dict[str, Any]:
        node = {key: value for key, value in node.items() if key not in ("default", "title")}
        for key in ("properties", "$defs"):
            if key in node:
                node[key] = {name: visit(sub) for name, sub in node[key].items()}
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            if key in node:
                node[key] = [visit(sub) for sub in node[key]]
        if isinstance(node.get("items"), dict):
            node["items"] = visit(node["items"])
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        return node

    return visit(schema.model_json_schema())


def schema_hash(schema: type[BaseModel]) -> str:
    canonical = json.dumps(strict_json_schema(schema), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_hash(model: str, schema: type[BaseModel], system: str, user: str) -> str:
    """sha256 over model, schema and messages; the prompt version lives in the system text."""
    payload = json.dumps(
        {"model": model, "schema": schema_hash(schema), "system": system, "user": user},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class CachedLLM:
    """Decorator over a StructuredLLM: identical requests are answered from `llm_cache`."""

    def __init__(
        self,
        inner: StructuredLLM,
        cache: LlmCacheRepository,
        model_for: Callable[[StageName], str],
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.model_for = model_for

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
        key = request_hash(self.model_for(stage), schema, system, user)
        stored = await self.cache.get(key)
        if stored is not None:
            value = schema.model_validate(stored["value"])
            log.info("llm.cache_hit", stage=stage.value, request_hash=key[:16])
            return LLMResult(
                value=value,
                model=stored["model"],
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                attempts=0,
                cached=True,
            )
        result = await self.inner.generate(
            schema=schema,
            system=system,
            user=user,
            stage=stage,
            max_tokens=max_tokens,
            run_id=run_id,
        )
        await self.cache.put(
            key,
            result.model,
            {"value": result.value.model_dump(mode="json"), "model": result.model},
        )
        return result
