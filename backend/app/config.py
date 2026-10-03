"""Typed application settings, loaded from the environment.

Every setting is read by the backend only. Nothing here is exposed to the
frontend except through ``/api/v1/capabilities``, which deliberately returns a
mode string rather than a value (NEP-09).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Database used by the migration integration tests. It is disposable:
#: `alembic downgrade base` drops every table in it.
TEST_DATABASE_NAME = "resolveiq_test"


def _sibling_database_url(url: str, database_name: str) -> str:
    """Return ``url`` pointing at a different database on the same server.

    Keeps the scheme, credentials, host and port of ``url`` and replaces only the
    database name, so the development and test databases cannot drift apart in
    host or password.
    """
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database_name}"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The repository keeps `.env` at its root while every command runs from
        # `backend/` (see the Makefile), so a bare ".env" would never be found.
        # Later entries win in pydantic-settings, so a backend-local file still
        # overrides the shared one.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- application ---------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # NoDecode stops pydantic-settings from running json.loads() on the raw value.
    # Without it, the comma-separated form documented in .env.example is a hard
    # startup error, because a bare `http://localhost:5173` is not valid JSON.
    # With it, _split_origins below decides how to read the value.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # -- database ------------------------------------------------------------
    database_url: str = "postgresql+psycopg://resolveiq:resolveiq@localhost:5432/resolveiq"

    # Disposable database for the migration integration tests. Empty means
    # "derive it from database_url"; set it explicitly only to override the host.
    test_database_url: str = ""

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
        """Accept a comma-separated string as well as a list.

        The comma-separated form is what .env.example documents. NoDecode above is
        what lets it arrive as a plain string: without it pydantic-settings would
        try to json.loads() the value first and fail on anything that is not a
        JSON array.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _derive_test_database_url(self) -> Settings:
        """Default the test database URL to a sibling of the development one.

        The migration tests run ``alembic downgrade base``, which drops every
        table. Pointing them at a separate database is what keeps that from
        destroying development data, and deriving the URL rather than repeating
        it means a change of host or password cannot leave the two pointing
        somewhere different.
        """
        if not self.test_database_url:
            self.test_database_url = _sibling_database_url(self.database_url, TEST_DATABASE_NAME)
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    ``lru_cache`` means the environment is parsed once per process and every
    component sees the same object. Tests can call ``get_settings.cache_clear()``.
    """
    return Settings()
