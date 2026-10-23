"""Offline replay cannot fall back to a provider or accept a different request."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from evals import replay
from gr.domain.enums import StageName
from gr.llm.base import LLMInvalidOutputError, LLMOutputTruncatedError
from gr.llm.cache import request_hash


class Answer(BaseModel):
    text: str


OPTIONS: dict[str, Any] = {"reasoning": {"enabled": False}}


def call(content: str, *, finish: str = "stop", user: str = "u") -> replay.RecordedCall:
    return replay.RecordedCall(
        model="m",
        request_hash=request_hash("m", Answer, "s", user, OPTIONS),
        response={"choices": [{"finish_reason": finish, "message": {"content": content}}]},
    )


async def ask(llm: replay.ReplayLLM) -> str:
    answer = await llm.generate(
        schema=Answer,
        system="s",
        user="u",
        stage=StageName.DETAIL,
        max_tokens=100,
        run_id="r",
    )
    assert answer.cached and answer.input_tokens == answer.output_tokens == 0
    return answer.value.text


async def test_fenced_answer_replays_without_network() -> None:
    llm = replay.ReplayLLM([call('```json\n{"text":"ok"}\n```')], lambda _m: OPTIONS)
    assert await ask(llm) == "ok"


async def test_repair_used_only_if_first_response_invalid() -> None:
    llm = replay.ReplayLLM([call("invalid"), call('{"text":"repaired"}')], lambda _m: OPTIONS)
    assert await ask(llm) == "repaired"


async def test_prefer_first_valid_response_to_repair() -> None:
    llm = replay.ReplayLLM(
        [call('{"text":"first"}'), call('{"text":"second"}')], lambda _m: OPTIONS
    )
    assert await ask(llm) == "first"


@pytest.mark.parametrize("options", [OPTIONS, {}])
async def test_request_or_options_mismatch_fails_closed(options: dict[str, Any]) -> None:
    llm = replay.ReplayLLM([call('{"text":"ok"}', user="other")], lambda _m: options)
    with pytest.raises(replay.ReplayError, match="request_hash"):
        await ask(llm)


async def test_options_are_part_of_identity() -> None:
    llm = replay.ReplayLLM([call('{"text":"ok"}')], lambda _m: {})
    with pytest.raises(replay.ReplayError, match="request_hash"):
        await ask(llm)


async def test_invalid_twice_is_not_a_success() -> None:
    llm = replay.ReplayLLM(
        [call("invalid"), call("invalid"), call('{"text":"third"}')], lambda _m: OPTIONS
    )
    with pytest.raises(LLMInvalidOutputError):
        await ask(llm)


async def test_truncated_response_keeps_split_signal() -> None:
    llm = replay.ReplayLLM([call('{"text":"ok"}', finish="length")], lambda _m: OPTIONS)
    with pytest.raises(LLMOutputTruncatedError):
        await ask(llm)


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        replay.read_snapshot(path, "r")
    assert not path.exists()


@pytest.mark.parametrize("destination", ["source.json", "gr.db", "baseline.json"])
def test_cannot_overwrite_source_database_or_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    monkeypatch.setattr(replay, "ROOT", tmp_path)
    (tmp_path / "evals").mkdir()
    source, database = tmp_path / "source.json", tmp_path / "gr.db"
    baseline = tmp_path / "evals/baseline.json"
    for path in (source, database, baseline):
        path.write_text("original")
    output = baseline if destination == "baseline.json" else tmp_path / destination
    assert replay.main([str(source), "--database", str(database), "--output", str(output)]) == 2
    assert all(p.read_text() == "original" for p in (source, database, baseline))


def test_replay_writes_a_separate_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def report(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"mode": "offline_replay", "documents": {"toy": {"outcome": {"complete": True}}}}

    monkeypatch.setattr(replay, "replay_report", report)
    output = tmp_path / "replay.json"
    assert replay.main([str(tmp_path / "source.json"), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["mode"] == "offline_replay"
