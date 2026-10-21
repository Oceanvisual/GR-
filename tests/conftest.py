"""Shared fixtures: tests never see the developer's environment or `.env`."""

from collections.abc import Callable
from typing import Any

import pytest

from gr.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Settings without `.env`; local and open by default, override per test."""

    def factory(**overrides: Any) -> Settings:
        fields: dict[str, Any] = {"app_env": "local", "auth_enabled": False}
        return Settings(_env_file=None, **(fields | overrides))

    return factory
