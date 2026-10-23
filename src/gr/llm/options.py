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


def request_options(stage: StageName, host: str | None, model: str) -> dict[str, Any]:
    """Extra request body fields for this stage and model on this host; empty for other
    providers. A per-model table overrides the stage defaults; an empty entry sends nothing."""
    reasoning = _config().get("reasoning", {})
    hosts: list[str] = reasoning.get("hosts", [])
    if not host or not any(host == h or host.endswith(f".{h}") for h in hosts):
        return {}
    for_model = reasoning.get("models", {}).get(model.split(":", 1)[0], {})  # «id:nitro» → id
    if stage.value in for_model:
        stage_options = for_model[stage.value]
    else:
        stage_options = reasoning.get("stages", {}).get(stage.value)
    return {"reasoning": dict(stage_options)} if stage_options else {}
