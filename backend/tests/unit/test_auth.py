"""Tests for the identity providers behind the auth port.

Two providers: the demo header path (which is a label, not a credential) and the HS256
bearer verifier (which is the one that makes the reviewer gate real). The verifier's
negative cases are the point -- a signature that is not checked, an algorithm that is
trusted from the token, or an expiry that is ignored are the ways a token check is
present but useless.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from app.adapters.auth import DemoIdentityProvider, HmacTokenIdentityProvider, issue_token
from app.ports.auth import (
    Credentials,
    ExpiredCredentialsError,
    InvalidCredentialsError,
    InvalidIdentityError,
    MissingCredentialsError,
)

SECRET = "0123456789abcdef0123456789abcdef"  # 32 chars


def bearer(token: str) -> Credentials:
    return Credentials(authorization=f"Bearer {token}")


# ---------------------------------------------------------------------------
# Issuing and verifying
# ---------------------------------------------------------------------------


def test_a_token_round_trips_to_a_verified_principal() -> None:
    token = issue_token(secret=SECRET, subject="alice", role="reviewer")

    principal = HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token))

    assert principal.subject == "alice"
    assert principal.role == "reviewer"
    assert principal.authenticated is True
    assert principal.source == "bearer-hs256"
    assert principal.issuer == "resolveiq"


def test_a_token_with_a_different_role_carries_that_role() -> None:
    token = issue_token(secret=SECRET, subject="bob", role="analyst")
    assert HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token)).role == "analyst"


def test_a_short_secret_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="at least 16"):
        HmacTokenIdentityProvider(secret="short")


def test_issuing_refuses_a_blank_subject() -> None:
    with pytest.raises(ValueError, match="subject"):
        issue_token(secret=SECRET, subject="   ", role="analyst")


# ---------------------------------------------------------------------------
# What must be rejected
# ---------------------------------------------------------------------------


def test_no_authorization_header_is_a_missing_credential() -> None:
    with pytest.raises(MissingCredentialsError):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(Credentials())


def test_a_non_bearer_scheme_is_invalid() -> None:
    provider = HmacTokenIdentityProvider(secret=SECRET)
    with pytest.raises(InvalidCredentialsError):
        provider.authenticate(Credentials(authorization="Basic Zm9v"))


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    token = issue_token(
        secret="a-completely-different-secret-value", subject="mallory", role="reviewer"
    )

    with pytest.raises(InvalidCredentialsError, match="signature"):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token))


def test_a_tampered_token_body_is_rejected() -> None:
    """Re-signing is impossible without the secret, so editing the role fails."""
    token = issue_token(secret=SECRET, subject="bob", role="analyst")
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["role"] = "reviewer"  # escalate
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    forged = f"{header}.{forged_payload}.{signature}"

    with pytest.raises(InvalidCredentialsError):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(forged))


def test_the_alg_none_token_is_rejected() -> None:
    """The classic algorithm-confusion attack: pin the algorithm, do not trust the header."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(
        b"="
    )
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "mallory", "role": "reviewer", "iss": "resolveiq"}).encode()
    ).rstrip(b"=")
    token = f"{header.decode()}.{payload.decode()}."

    with pytest.raises(InvalidCredentialsError):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token))


def test_an_expired_token_is_named_as_expired() -> None:
    token = issue_token(secret=SECRET, subject="alice", role="reviewer", ttl_seconds=-10)
    # No leeway, so the 10-second-over expiry is unambiguously expired rather than
    # absorbed by the clock-skew grace the next test exercises.
    with pytest.raises(ExpiredCredentialsError):
        HmacTokenIdentityProvider(secret=SECRET, leeway_seconds=0).authenticate(bearer(token))


def test_an_expiry_inside_the_leeway_is_accepted() -> None:
    """Clock skew between issuer and verifier is normal; a small grace absorbs it."""
    now = int(time.time())
    token = issue_token(secret=SECRET, subject="alice", role="reviewer", now=now - 5, ttl_seconds=1)
    # exp is now-4, which is in the past, but within the 30s default leeway.
    assert HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token)).subject == "alice"


def test_a_token_from_another_issuer_is_rejected() -> None:
    token = issue_token(secret=SECRET, subject="alice", role="reviewer", issuer="somewhere-else")
    with pytest.raises(InvalidCredentialsError, match="issuer"):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token))


def test_a_token_without_a_role_is_rejected() -> None:
    token = issue_token(secret=SECRET, subject="alice", role="analyst", extra_claims={"role": ""})
    with pytest.raises(InvalidCredentialsError, match="role"):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer(token))


def test_a_well_formed_but_malformed_token_is_rejected() -> None:
    with pytest.raises(InvalidCredentialsError):
        HmacTokenIdentityProvider(secret=SECRET).authenticate(bearer("not-a-jwt"))


# ---------------------------------------------------------------------------
# The demo provider
# ---------------------------------------------------------------------------


def test_the_demo_provider_returns_an_unauthenticated_principal() -> None:
    principal = DemoIdentityProvider().authenticate(Credentials(actor_id="ceo", actor_role="admin"))

    assert principal.subject == "ceo"
    assert principal.role == "admin"
    # The load-bearing assertion: a header grants an identity but never authentication.
    assert principal.authenticated is False
    assert principal.source == "demo-header"


def test_the_demo_provider_falls_back_to_the_configured_default() -> None:
    principal = DemoIdentityProvider(
        default_actor_id="dev-analyst", default_actor_role="analyst"
    ).authenticate(Credentials())

    assert principal.subject == "dev-analyst"
    assert principal.role == "analyst"


def test_the_demo_provider_rejects_a_blank_header() -> None:
    with pytest.raises(InvalidIdentityError):
        DemoIdentityProvider().authenticate(Credentials(actor_id="   ", actor_role="analyst"))


def test_a_disabled_demo_provider_demands_a_token() -> None:
    with pytest.raises(MissingCredentialsError):
        DemoIdentityProvider(enabled=False).authenticate(
            Credentials(actor_id="x", actor_role="analyst")
        )
