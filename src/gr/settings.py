"""Service configuration from environment variables (plan.md, «Конфигурация»)."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["local", "server"]
DataPolicy = Literal["disabled", "local_only", "external_allowed"]
StructuredMode = Literal["json_schema", "json_object", "auto"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["console", "json"]

_MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", frozen=True
    )

    app_env: AppEnv = "local"
    app_port: int = Field(default=8000, gt=0, le=65535)
    app_public_url: AnyHttpUrl | None = None
    data_dir: Path = Path("./data")
    database_url: str | None = None

    auth_enabled: bool = True
    auth_users: SecretStr | None = None

    max_upload_mb: int = Field(default=20, gt=0)
    max_pages: int = Field(default=300, gt=0)
    max_chars: int = Field(default=1_000_000, gt=0)
    max_unzipped_mb: int = Field(default=100, gt=0)

    llm_data_policy: DataPolicy = "disabled"
    llm_base_url: AnyHttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_local_hosts: Annotated[list[str], NoDecode] = ["localhost", "127.0.0.1", "ollama", "vllm"]
    llm_model_extraction: str | None = None
    llm_model_analysis: str | None = None
    llm_model_discovery: str | None = None  # S3 only; the extraction model when not set
    llm_structured_mode: StructuredMode = "auto"
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    llm_concurrency: int = Field(default=4, ge=1)
    llm_cache_enabled: bool = True
    llm_max_output_tokens: int = Field(default=16000, gt=0)
    llm_price_input_per_mtok: Decimal | None = Field(default=None, ge=0)
    llm_price_output_per_mtok: Decimal | None = Field(default=None, ge=0)

    max_concurrent_analyses: int = Field(default=2, ge=1)
    parse_workers: int = Field(default=2, ge=1)
    parse_timeout_s: float = Field(default=120.0, gt=0)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=0)

    ocr_enabled: bool = True
    ocr_lang: str = "rus+eng"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_max_pages: int = Field(default=150, gt=0)
    ocr_page_timeout_s: float = Field(default=60.0, gt=0)
    ocr_min_confidence: float = Field(default=60.0, ge=0, le=100)
    ocr_concurrency: int = Field(default=1, ge=1)

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    @field_validator("llm_local_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if not self.auth_enabled and self.app_env != "local":
            raise ValueError("AUTH_ENABLED=false допустим только при APP_ENV=local")
        if self.auth_enabled and not self.auth_users:
            raise ValueError("AUTH_ENABLED=true требует AUTH_USERS (см. `gr hash-password`)")
        if self.app_env == "server" and self.app_public_url is None:
            raise ValueError("APP_ENV=server требует APP_PUBLIC_URL (внешний адрес сервиса)")
        if self.llm_data_policy != "disabled":
            missing = [
                name
                for name, value in (
                    ("LLM_BASE_URL", self.llm_base_url),
                    ("LLM_MODEL_EXTRACTION", self.llm_model_extraction),
                    ("LLM_MODEL_ANALYSIS", self.llm_model_analysis),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"LLM_DATA_POLICY={self.llm_data_policy} требует {', '.join(missing)}"
                )
        return self

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'gr.db'}"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * _MB

    @property
    def max_unzipped_bytes(self) -> int:
        return self.max_unzipped_mb * _MB


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
