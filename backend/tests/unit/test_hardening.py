"""Transport hardening: trusted hosts, rate limiting, security headers, docs gating.

These are the controls that are easy to add and easy to leave subtly off, so each one is
asserted by observing the response rather than by inspecting the middleware list.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app

SECRET = "0123456789abcdef0123456789abcdef"


@contextmanager
def app_with(**env: str) -> Iterator[TestClient]:
    """Build an app with exactly these environment values, then restore the process."""
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


def test_responses_carry_baseline_security_headers() -> None:
    with app_with(CASE_STORE="memory") as client:
        response = client.get("/api/v1/health", headers={})

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_security_headers_can_be_turned_off() -> None:
    with app_with(CASE_STORE="memory", SECURITY_HEADERS_ENABLED="false") as client:
        response = client.get("/api/v1/health", headers={})

    assert "X-Frame-Options" not in response.headers


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_the_limiter_returns_429_after_the_budget_is_spent() -> None:
    with app_with(
        CASE_STORE="memory",
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_REQUESTS="2",
        RATE_LIMIT_WINDOW_SECONDS="60",
    ) as client:
        assert client.get("/api/v1/capabilities").status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 200
        third = client.get("/api/v1/capabilities")

    assert third.status_code == 429
    assert third.json()["error"]["code"] == "RATE_LIMITED"
    assert third.headers["Retry-After"]


def test_the_health_probe_is_exempt_from_the_limiter() -> None:
    with app_with(
        CASE_STORE="memory",
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_REQUESTS="1",
        RATE_LIMIT_WINDOW_SECONDS="60",
    ) as client:
        for _ in range(5):
            assert client.get("/api/v1/health", headers={}).status_code == 200


# ---------------------------------------------------------------------------
# Trusted hosts
# ---------------------------------------------------------------------------


def test_an_unlisted_host_is_rejected() -> None:
    with app_with(CASE_STORE="memory", ALLOWED_HOSTS="api.example.com") as client:
        response = client.get("/api/v1/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400


def test_a_listed_host_is_accepted() -> None:
    with app_with(CASE_STORE="memory", ALLOWED_HOSTS="api.example.com") as client:
        response = client.get("/api/v1/health", headers={"Host": "api.example.com"})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Docs exposure
# ---------------------------------------------------------------------------


def test_docs_are_served_in_development_by_default() -> None:
    with app_with(CASE_STORE="memory") as client:
        assert client.get("/openapi.json").status_code == 200


def test_docs_can_be_forced_off() -> None:
    with app_with(CASE_STORE="memory", EXPOSE_DOCS="false") as client:
        assert client.get("/openapi.json").status_code == 404


def test_docs_are_off_in_production_by_default() -> None:
    with app_with(
        CASE_STORE="memory",
        APP_ENV="production",
        AUTH_MODE="token",
        AUTH_SECRET=SECRET,
        ALLOWED_HOSTS="api.example.com",
        CORS_ORIGINS="http://localhost:5173",
    ) as client:
        # The Host header must be the allow-listed one, or TrustedHost rejects before
        # routing and the docs check would be masked by a 400.
        assert client.get("/openapi.json", headers={"Host": "api.example.com"}).status_code == 404
        assert client.get("/api/v1/health", headers={"Host": "api.example.com"}).status_code == 200


# ---------------------------------------------------------------------------
# The production configuration guard
# ---------------------------------------------------------------------------

PRODUCTION = {
    "app_env": "production",
    "auth_mode": "token",
    "auth_secret": SECRET,
    "allowed_hosts": "api.example.com",
    "cors_origins": "http://localhost:5173",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**PRODUCTION, **overrides})


def test_a_valid_production_config_is_accepted() -> None:
    assert _settings().app_env == "production"


def test_production_refuses_the_demo_identity_mode() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=token"):
        _settings(auth_mode="demo")


def test_production_refuses_a_short_signing_secret() -> None:
    with pytest.raises(ValueError, match="AUTH_SECRET"):
        _settings(auth_secret="too-short")


def test_production_refuses_a_wildcard_cors_origin() -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _settings(cors_origins="*")


def test_production_refuses_a_wildcard_host_allow_list() -> None:
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        _settings(allowed_hosts="*")


def test_development_is_never_blocked_by_the_guard() -> None:
    """The demo path must keep running with no configuration at all."""
    assert Settings(_env_file=None).auth_mode == "demo"
