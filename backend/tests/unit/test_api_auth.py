"""End-to-end identity over HTTP, in both modes.

The demo-mode behaviour is covered by ``test_api_cases.py`` (a header grants a role,
labelled as not-authentication). This file covers ``AUTH_MODE=token``, where the role is
verified rather than declared -- in particular that a caller cannot reach the decision
gate by editing a header or by presenting a token signed with the wrong key.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.auth import issue_token
from app.config import get_settings
from app.main import create_app

SECRET = "0123456789abcdef0123456789abcdef"
ANALYST = issue_token(secret=SECRET, subject="analyst-1", role="analyst")
REVIEWER = issue_token(secret=SECRET, subject="reviewer-1", role="reviewer")

_MANAGED = ("CASE_STORE", "AUTH_MODE", "AUTH_SECRET", "APP_ENV", "DEMO_IDENTITY_ENABLED")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An app in token mode on the in-memory store."""
    previous = {key: os.environ.get(key) for key in _MANAGED}
    os.environ.update(
        {
            "CASE_STORE": "memory",
            "AUTH_MODE": "token",
            "AUTH_SECRET": SECRET,
            "APP_ENV": "development",
        }
    )
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as test_client:
            yield test_client
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Unauthenticated and invalid credentials
# ---------------------------------------------------------------------------


def test_a_request_without_a_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/capabilities", headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_a_malformed_token_is_401_invalid_credentials(client: TestClient) -> None:
    response = client.get("/api/v1/capabilities", headers=_auth("not-a-jwt"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_a_token_signed_with_another_secret_is_401(client: TestClient) -> None:
    forged = issue_token(
        secret="another-secret-entirely-000000000000", subject="x", role="reviewer"
    )

    response = client.get("/api/v1/capabilities", headers=_auth(forged))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_an_expired_token_is_401_credentials_expired(client: TestClient) -> None:
    expired = issue_token(secret=SECRET, subject="x", role="reviewer", ttl_seconds=-60)

    response = client.get("/api/v1/capabilities", headers=_auth(expired))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CREDENTIALS_EXPIRED"


def test_the_health_probe_needs_no_token(client: TestClient) -> None:
    assert client.get("/api/v1/health", headers={}).status_code == 200


# ---------------------------------------------------------------------------
# The reviewer gate, with a verified role
# ---------------------------------------------------------------------------


def test_an_analyst_token_is_refused_the_decision_gate(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/disputes/{uuid4()}/decision",
        json={"decision": "RESOLVED", "rationale": "looks right"},
        headers=_auth(ANALYST),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REVIEWER_REQUIRED"


def test_a_reviewer_token_passes_the_decision_gate(client: TestClient) -> None:
    """No case exists, so the next failure is a 404 -- proof the role gate was passed."""
    response = client.post(
        f"/api/v1/disputes/{uuid4()}/decision",
        json={"decision": "RESOLVED", "rationale": "looks right"},
        headers=_auth(REVIEWER),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DISPUTE_NOT_FOUND"


def test_spoofed_actor_headers_cannot_escalate_an_analyst_token(client: TestClient) -> None:
    """The header path is not consulted at all when a token is present."""
    response = client.post(
        f"/api/v1/disputes/{uuid4()}/decision",
        json={"decision": "RESOLVED", "rationale": "looks right"},
        headers={**_auth(ANALYST), "X-Actor-Id": "ceo", "X-Actor-Role": "reviewer"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REVIEWER_REQUIRED"


# ---------------------------------------------------------------------------
# Capabilities reports the verified identity
# ---------------------------------------------------------------------------


def test_capabilities_reports_a_verified_identity(client: TestClient) -> None:
    body = client.get("/api/v1/capabilities", headers=_auth(REVIEWER)).json()

    assert body["identity"]["mode"] == "bearer-hs256"
    assert body["identity"]["is_authentication"] is True
    assert body["identity"]["actor_id"] == "reviewer-1"
    assert body["identity"]["actor_role"] == "reviewer"


def test_capabilities_does_not_echo_the_secret(client: TestClient) -> None:
    raw = client.get("/api/v1/capabilities", headers=_auth(REVIEWER)).text

    assert SECRET not in raw
