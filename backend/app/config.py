"""Typed application settings, loaded from the environment.

Every setting is read by the backend only. Nothing here is exposed to the
frontend except through ``/api/v1/capabilities``, which deliberately returns a
mode string rather than a value (NEP-09).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- application ---------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # -- database ------------------------------------------------------------
    database_url: str = "postgresql+psycopg://resolveiq:resolveiq@localhost:5432/resolveiq"

    # -- llm -----------------------------------------------------------------
    # STK-06: "mock" is the default so the app runs offline and tests are
    # deterministic. A real provider adapter is not implemented yet (OQ-08).
    llm_provider: Literal["mock"] = "mock"
    llm_model: str = "mock-deterministic-v1"
    llm_api_key: str = ""
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2
    llm_temperature: float = 0
    prompt_version: str = "prompt-v1"

    # -- investigation -------------------------------------------------------
    investigation_ttl_hours: int = 24
    dispute_description_max_chars: int = 10_000

    # -- demo identity -------------------------------------------------------
    # OQ-01: THIS IS NOT AUTHENTICATION. Headers are trivially spoofable and are
    # present only so the review/approve flow can be demonstrated. See
    # docs/ENGINEERING_DECISIONS.md and README.md §Security.
    demo_identity_enabled: bool = True
    demo_default_actor_id: str = "dev-analyst"
    demo_default_actor_role: Literal["analyst", "reviewer"] = "analyst"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string as well as a JSON list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    ``lru_cache`` means the environment is parsed once per process and every
    component sees the same object. Tests can call ``get_settings.cache_clear()``.
    """
    return Settings()
