"""Verified bearer identity: HMAC-SHA256 signed tokens (JWT, HS256).

This is the adapter that makes the reviewer-role gate real. A token is a signed
statement of ``sub`` (who) and ``role`` (what they may do); the signature is checked
with a shared secret before either claim is believed, so a caller cannot promote
themselves to ``reviewer`` by editing a header or a token body.

Why HS256 and not an OIDC client: this keeps the deployment self-contained and needs
no vendor dependency, and the token shape is the same compact JWT an identity provider
would issue, so swapping in RS256/JWKS later is an adapter change rather than a
protocol change. The secret is read from configuration and never logged.

The verification order matters and is deliberate: parse structurally, verify the
signature, *then* read any claim. Reading ``exp`` or ``role`` before the signature is
checked would mean acting on attacker-controlled data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.ports.auth import (
    Credentials,
    ExpiredCredentialsError,
    InvalidCredentialsError,
    MissingCredentialsError,
    Principal,
)

__all__ = ["HmacTokenIdentityProvider", "issue_token"]

#: A signing secret shorter than this is refused at construction. HS256's security is
#: the secret's entropy; a short one is brute-forceable offline from a single token.
MIN_SECRET_LENGTH = 16


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    # Restore the padding base64url encoding strips, so both padded and unpadded
    # tokens decode. A segment that is not valid base64 raises binascii.Error, which
    # the caller turns into InvalidCredentialsError rather than letting it escape.
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded)


def issue_token(
    *,
    secret: str,
    subject: str,
    role: str,
    issuer: str = "resolveiq",
    ttl_seconds: int = 3600,
    now: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a signed HS256 token. Used by the operator CLI and by tests.

    Deliberately lives next to the verifier rather than behind an HTTP endpoint: a
    token-minting route in the application would be an authentication bypass with
    extra steps. Issuance belongs to an operator or an identity provider.
    """
    if not secret:
        raise ValueError("a signing secret is required")
    if not subject.strip():
        raise ValueError("token subject must not be blank")
    if not role.strip():
        raise ValueError("token role must not be blank")

    issued_at = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims: dict[str, Any] = {
        "sub": subject.strip(),
        "role": role.strip(),
        "iss": issuer,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    if extra_claims:
        claims.update(extra_claims)

    header_segment = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_segment = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"


class HmacTokenIdentityProvider:
    """Verifies ``Authorization: Bearer <jwt>`` and returns the verified principal."""

    name = "bearer-hs256"

    def __init__(
        self,
        *,
        secret: str,
        issuer: str = "resolveiq",
        leeway_seconds: int = 30,
    ) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"AUTH_SECRET must be at least {MIN_SECRET_LENGTH} characters; "
                "HS256's strength is the secret's entropy"
            )
        self._secret = secret.encode("utf-8")
        self._issuer = issuer
        self._leeway = max(0, int(leeway_seconds))

    def authenticate(self, credentials: Credentials) -> Principal:
        token = self._extract_bearer(credentials.authorization)
        claims, signing_input, signature = self._split(token)
        # Verify over the exact received segments, which is what the issuer signed.
        # (Re-encoding from the decoded JSON would verify only tokens this codebase
        # produced, and would reject a valid token from any other issuer.)
        self._verify_signature(signing_input, signature)
        return self._principal(claims)

    # -- internals -------------------------------------------------------

    @staticmethod
    def _extract_bearer(authorization: str | None) -> str:
        if authorization is None or not authorization.strip():
            raise MissingCredentialsError("an Authorization: Bearer token is required")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise InvalidCredentialsError(
                "the Authorization header must be of the form 'Bearer <token>'"
            )
        return value.strip()

    @staticmethod
    def _split(token: str) -> tuple[dict[str, Any], bytes, bytes]:
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidCredentialsError("the token is not a well-formed JWT")
        header_segment, payload_segment, signature_segment = parts
        try:
            header = json.loads(_b64url_decode(header_segment))
            claims = json.loads(_b64url_decode(payload_segment))
            signature = _b64url_decode(signature_segment)
        except (ValueError, TypeError) as exc:
            raise InvalidCredentialsError("the token could not be decoded") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise InvalidCredentialsError("the token header and claims must be objects")
        if header.get("alg") != "HS256":
            # Refuse "none" and any asymmetric algorithm: pinning the algorithm is
            # what stops the classic alg-confusion attack.
            raise InvalidCredentialsError("the token algorithm is not supported")
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        return claims, signing_input, signature

    def _verify_signature(self, signing_input: bytes, signature: bytes) -> None:
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise InvalidCredentialsError("the token signature did not verify")

    def _principal(self, claims: dict[str, Any]) -> Principal:
        now = int(time.time())
        exp = claims.get("exp")
        if exp is not None:
            try:
                expires_at = int(exp)
            except (TypeError, ValueError) as exc:
                raise InvalidCredentialsError("the token exp claim is not a number") from exc
            if now > expires_at + self._leeway:
                raise ExpiredCredentialsError("the token has expired")

        issuer = claims.get("iss")
        if issuer != self._issuer:
            raise InvalidCredentialsError("the token issuer is not recognised")

        subject = claims.get("sub")
        role = claims.get("role")
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidCredentialsError("the token has no subject")
        if not isinstance(role, str) or not role.strip():
            raise InvalidCredentialsError("the token has no role")

        return Principal(
            subject=subject.strip()[:128],
            role=role.strip()[:64],
            authenticated=True,
            source=self.name,
            issuer=str(issuer),
        )
