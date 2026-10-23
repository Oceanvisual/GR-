"""Data transfer policy table (R-09, NFR-009, T012)."""

from collections.abc import Callable

import pytest

from gr.llm.base import ExternalLLMNotAllowedError, LLMNotConfiguredError
from gr.llm.policy import check_data_policy, is_local_host
from gr.settings import Settings

MODELS = {"llm_model_extraction": "m1", "llm_model_analysis": "m2"}


def test_disabled_policy_refuses(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(LLMNotConfiguredError) as exc:
        check_data_policy(make_settings())
    assert exc.value.code == "llm_not_configured"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8001/v1",
        "http://[::1]:8000/v1",
        "http://10.1.2.3/v1",
        "http://192.168.0.10:11434/v1",
        "http://172.20.0.5/v1",
        "http://ollama:11434/v1",
        "http://VLLM:8000/v1",
    ],
)
def test_local_only_accepts_local_hosts(make_settings: Callable[..., Settings], url: str) -> None:
    check_data_policy(make_settings(llm_data_policy="local_only", llm_base_url=url, **MODELS))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "https://openrouter.ai/api/v1",
        "http://127.0.0.1.attacker.example/v1",
        "http://8.8.8.8/v1",
        "http://localhost.evil.example/v1",
    ],
)
def test_local_only_refuses_external(make_settings: Callable[..., Settings], url: str) -> None:
    settings = make_settings(llm_data_policy="local_only", llm_base_url=url, **MODELS)
    with pytest.raises(ExternalLLMNotAllowedError) as exc:
        check_data_policy(settings)
    assert exc.value.code == "external_llm_not_allowed"


def test_local_hosts_list_is_configurable(make_settings: Callable[..., Settings]) -> None:
    url = "http://llm.corp.internal/v1"
    refused = make_settings(llm_data_policy="local_only", llm_base_url=url, **MODELS)
    with pytest.raises(ExternalLLMNotAllowedError):
        check_data_policy(refused)
    allowed = make_settings(
        llm_data_policy="local_only",
        llm_base_url=url,
        llm_local_hosts="llm.corp.internal",
        **MODELS,
    )
    check_data_policy(allowed)


def test_external_allowed_accepts_any_host(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(
        llm_data_policy="external_allowed", llm_base_url="https://api.example.com/v1", **MODELS
    )
    check_data_policy(settings)


@pytest.mark.parametrize(("host", "local"), [("", False), ("LocalHost.", True), ("0x7f.1", False)])
def test_is_local_host_edge_cases(host: str, local: bool) -> None:
    assert is_local_host(host, ["localhost"]) is local
