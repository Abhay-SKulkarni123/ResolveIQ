"""Tests for settings loading.

Two of these are regression tests for bugs that were present in the Phase 0
scaffold and only became reachable once the repository's own ``.env`` was actually
loaded, so they are worth pinning explicitly.
"""

from __future__ import annotations

import pytest

from app.config import DOTENV_PATH, REPO_ROOT, TEST_DATABASE_NAME, Settings

BASE_URL = "postgresql+psycopg://resolveiq:hunter2@db.internal:6543/resolveiq"


def settings(**overrides: str) -> Settings:
    """Build Settings from explicit values only, ignoring any .env on disk."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# The test database URL
# ---------------------------------------------------------------------------


def test_test_database_url_defaults_to_a_sibling_of_the_development_one() -> None:
    """Keeps the role, password, host and port; changes only the database name."""
    assert settings(database_url=BASE_URL).test_database_url == (
        f"postgresql+psycopg://resolveiq:hunter2@db.internal:6543/{TEST_DATABASE_NAME}"
    )


def test_an_explicit_test_database_url_is_left_alone() -> None:
    explicit = "postgresql+psycopg://other:pw@elsewhere:5432/scratch"
    assert settings(database_url=BASE_URL, test_database_url=explicit).test_database_url == explicit


def test_the_test_database_never_defaults_to_the_development_one() -> None:
    """`alembic downgrade base` drops every table. It must not aim at real data."""
    resolved = settings(database_url=BASE_URL).test_database_url
    assert not resolved.endswith("/resolveiq")
    assert resolved.endswith(f"/{TEST_DATABASE_NAME}")


def test_a_url_with_no_database_name_still_gets_one() -> None:
    resolved = settings(
        database_url="postgresql+psycopg://resolveiq@localhost:5432"
    ).test_database_url
    assert resolved.endswith(f"/{TEST_DATABASE_NAME}")


def test_a_url_carrying_query_parameters_keeps_them() -> None:
    """sslmode and friends belong to the connection, not to the database name."""
    resolved = settings(
        database_url=f"{BASE_URL}?sslmode=require&application_name=resolveiq"
    ).test_database_url
    assert resolved.endswith(f"/{TEST_DATABASE_NAME}?sslmode=require&application_name=resolveiq")


# ---------------------------------------------------------------------------
# cors_origins
# ---------------------------------------------------------------------------


def test_cors_origins_accepts_a_comma_separated_string() -> None:
    """The form documented in .env.example.

    Regression test: pydantic-settings JSON-decodes any field it considers complex,
    so without NoDecode this raised SettingsError on `http://localhost:5173` and the
    application could not start from the shipped example file.
    """
    parsed = settings(cors_origins="http://localhost:5173,http://127.0.0.1:5173").cors_origins
    assert parsed == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_cors_origins_trims_whitespace_and_drops_empty_entries() -> None:
    parsed = settings(cors_origins=" http://a.test , , http://b.test ").cors_origins
    assert parsed == ["http://a.test", "http://b.test"]


def test_cors_origins_defaults_to_the_frontend_dev_server() -> None:
    assert settings().cors_origins == ["http://localhost:5173"]


# ---------------------------------------------------------------------------
# root .env resolution
# ---------------------------------------------------------------------------


def test_repo_root_is_the_workspace_with_its_env_beside_backend() -> None:
    """The repository keeps one canonical ``.env`` at the root, next to ``backend/``."""
    assert (REPO_ROOT / "backend").is_dir()
    assert (REPO_ROOT / ".env.example").is_file()
    assert DOTENV_PATH == REPO_ROOT / ".env"


def test_settings_are_configured_to_read_the_root_env_file() -> None:
    """The path pydantic-settings was told about, not a CWD-relative guess."""
    assert Settings.model_config["env_file"] == str(DOTENV_PATH)


def test_database_url_resolves_identically_from_any_working_directory(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``env_file`` used to be CWD-relative, so runs started from the
    repository root, from ``backend/``, through uvicorn or through Alembic could
    resolve a different (or no) ``.env``. The value must be the same everywhere."""
    from_repo_root = Settings().database_url
    monkeypatch.chdir(tmp_path)
    from_elsewhere = Settings().database_url
    assert from_elsewhere == from_repo_root
