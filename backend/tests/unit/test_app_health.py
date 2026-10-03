"""Smoke tests for the application factory.

Small, but they verify two things that matter early: the app is constructible,
and the only endpoint that reports configuration does not leak secrets (NEP-09).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "resolveiq-api"
    assert body["llm_provider"] == "mock"


def test_health_does_not_leak_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A realistic secret in the environment must not appear in any response."""
    get_settings.cache_clear()
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value-should-never-appear")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:pa55word@db:5432/resolveiq")
    try:
        response = TestClient(create_app()).get("/api/v1/health")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    raw = response.text
    assert "sk-super-secret-value-should-never-appear" not in raw
    assert "pa55word" not in raw
    # The provider *mode* is safe to expose; the key is not.
    assert response.json()["llm_provider"] == "mock"


def test_settings_parse_comma_separated_cors_origins() -> None:
    settings = Settings(cors_origins="http://localhost:5173,http://127.0.0.1:5173")

    assert settings.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_settings_default_to_the_deterministic_mock_provider() -> None:
    # STK-06: the offline mock is the default, so nothing reaches a network by
    # accident and tests stay reproducible.
    assert Settings().llm_provider == "mock"
    assert Settings().llm_api_key == ""
