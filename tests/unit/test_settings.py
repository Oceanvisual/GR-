from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gr.settings import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

LOCAL_OPEN: dict[str, Any] = {"app_env": "local", "auth_enabled": False}
LLM_READY: dict[str, Any] = {
    "llm_base_url": "http://localhost:11434/v1",
    "llm_model_extraction": "m-extract",
    "llm_model_analysis": "m-analyze",
}


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**LOCAL_OPEN, **overrides})


def test_defaults_are_safe() -> None:
    settings = make()
    assert settings.llm_data_policy == "disabled"
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.resolved_database_url == "sqlite+aiosqlite:///data/gr.db"


def test_auth_disabled_only_in_local() -> None:
    with pytest.raises(ValidationError, match="AUTH_ENABLED=false"):
        make(app_env="server", app_public_url="https://npa.example.org")


def test_auth_enabled_requires_users() -> None:
    with pytest.raises(ValidationError, match="AUTH_USERS"):
        make(auth_enabled=True)


def test_server_requires_public_url() -> None:
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        make(app_env="server", auth_enabled=True, auth_users="u:scrypt$a$b")


def test_server_config_valid() -> None:
    settings = make(
        app_env="server",
        auth_enabled=True,
        auth_users="u:scrypt$a$b",
        app_public_url="https://npa.example.org",
    )
    assert str(settings.app_public_url) == "https://npa.example.org/"


@pytest.mark.parametrize("policy", ["local_only", "external_allowed"])
def test_llm_policy_requires_endpoint_and_models(policy: str) -> None:
    with pytest.raises(ValidationError, match="LLM_BASE_URL, LLM_MODEL_EXTRACTION"):
        make(llm_data_policy=policy)
    assert make(llm_data_policy=policy, **LLM_READY).llm_data_policy == policy


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_upload_mb", 0),
        ("max_pages", 0),
        ("app_port", 70000),
        ("ocr_dpi", 50),
        ("ocr_min_confidence", 101),
        ("llm_max_retries", 11),
    ],
)
def test_limits_validated(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("LLM_LOCAL_HOSTS", "ollama, 10.0.0.5 ,")
    monkeypatch.setenv("APP_PUBLIC_URL", "")
    settings = Settings(_env_file=None)
    assert settings.llm_local_hosts == ["ollama", "10.0.0.5"]
    assert settings.app_public_url is None


def test_secrets_not_rendered() -> None:
    settings = make(llm_api_key="sk-secret-value", **LLM_READY, llm_data_policy="local_only")
    assert "sk-secret-value" not in repr(settings)


def test_env_example_lists_every_setting() -> None:
    if not ENV_EXAMPLE.exists():
        pytest.skip(".env.example отсутствует: создайте его из шаблона (см. README)")
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = [name.upper() for name in Settings.model_fields if f"{name.upper()}=" not in text]
    assert not missing, f".env.example не содержит: {missing}"
