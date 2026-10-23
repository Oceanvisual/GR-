"""Request options per stage and per model (config/llm.toml)."""

from typing import Any

import pytest

from gr.domain.enums import StageName
from gr.llm import options
from gr.llm.options import request_options

_REAL_CONFIG = options._config
CONFIG: dict[str, Any] = {
    "reasoning": {
        "hosts": ["openrouter.ai"],
        "stages": {"discover": {"enabled": False}, "opportunities": {"effort": "low"}},
        "models": {"big": {"discover": {"effort": "low"}, "detail": {}}},
    }
}
LOW = {"reasoning": {"effort": "low"}}
OFF = {"reasoning": {"enabled": False}}


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options, "_config", lambda: CONFIG)


def test_a_per_model_entry_overrides_the_stage_default() -> None:
    assert request_options(StageName.DISCOVER, "openrouter.ai", "big") == LOW
    assert request_options(StageName.DISCOVER, "openrouter.ai", "small") == OFF


def test_a_model_without_an_entry_for_the_stage_falls_back() -> None:
    assert request_options(StageName.OPPORTUNITIES, "openrouter.ai", "big") == LOW


def test_an_empty_entry_sends_nothing() -> None:
    assert request_options(StageName.DETAIL, "openrouter.ai", "big") == {}


def test_a_model_variant_uses_the_entry_of_its_base_id() -> None:
    assert request_options(StageName.DISCOVER, "openrouter.ai", "big:nitro") == LOW


def test_other_hosts_get_no_options() -> None:
    assert request_options(StageName.DISCOVER, "localhost", "big") == {}


def test_sonnet_reasons_at_low_effort_on_every_extraction_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(options, "_config", _REAL_CONFIG)
    sonnet, haiku = "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5"
    for stage in (StageName.DISCOVER, StageName.DETAIL, StageName.VALIDATE):
        assert request_options(stage, "openrouter.ai", sonnet) == LOW
    assert request_options(StageName.DETAIL, "openrouter.ai", haiku) == OFF
