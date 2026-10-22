"""Provider-specific request options per stage (`config/llm.toml`)."""

import tomllib
from functools import cache
from importlib import resources
from typing import Any

from gr.domain.enums import StageName


@cache
def _config() -> dict[str, Any]:
    text = resources.files("gr.config").joinpath("llm.toml").read_text("utf-8")
    config: dict[str, Any] = tomllib.loads(text)
    return config


def request_options(stage: StageName, host: str | None) -> dict[str, Any]:
    """Extra request body fields for this stage on this host; empty for other providers."""
    reasoning = _config().get("reasoning", {})
    hosts: list[str] = reasoning.get("hosts", [])
    if not host or not any(host == h or host.endswith(f".{h}") for h in hosts):
        return {}
    stage_options = reasoning.get("stages", {}).get(stage.value)
    return {"reasoning": dict(stage_options)} if stage_options else {}
