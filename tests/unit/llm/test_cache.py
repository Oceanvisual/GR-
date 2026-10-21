"""CachedLLM and request hashing (NFR-012, T013)."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from gr.domain.enums import StageName
from gr.llm.cache import CachedLLM, request_hash
from gr.llm.fake import FakeLLM
from gr.storage.db import Database
from gr.storage.repositories import LlmCacheRepository


class Answer(BaseModel):
    items: list[str]


class Other(BaseModel):
    items: list[int]


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[LlmCacheRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'gr.db'}")
    await database.init()
    yield LlmCacheRepository(database)
    await database.dispose()


async def _ask(llm: CachedLLM, user: str = "doc") -> tuple[list[str], bool]:
    result = await llm.generate(
        schema=Answer, system="s", user=user, stage=StageName.DISCOVER, max_tokens=10, run_id="r"
    )
    return result.value.items, result.cached


async def test_identical_request_served_from_cache(cache: LlmCacheRepository) -> None:
    inner = FakeLLM(default={"items": ["a"]})
    llm = CachedLLM(inner, cache, model_for=lambda _stage: "m")
    assert await _ask(llm) == (["a"], False)
    assert await _ask(llm) == (["a"], True)
    assert len(inner.calls) == 1
    assert await _ask(llm, user="other doc") == (["a"], False)
    assert len(inner.calls) == 2


async def test_failures_are_not_cached(cache: LlmCacheRepository) -> None:
    inner = FakeLLM({StageName.DISCOVER: [RuntimeError("down"), {"items": ["b"]}]})
    llm = CachedLLM(inner, cache, model_for=lambda _stage: "m")
    with pytest.raises(RuntimeError):
        await _ask(llm)
    assert await _ask(llm) == (["b"], False)


def test_hash_depends_on_model_schema_and_messages() -> None:
    base = request_hash("m", Answer, "s", "u")
    assert base == request_hash("m", Answer, "s", "u")
    variants = {
        request_hash("m2", Answer, "s", "u"),
        request_hash("m", Other, "s", "u"),
        request_hash("m", Answer, "s2", "u"),
        request_hash("m", Answer, "s", "u2"),
    }
    assert len(variants | {base}) == 5
