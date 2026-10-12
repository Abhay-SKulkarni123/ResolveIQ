"""The identity port: the one place the application learns *who* is calling.

Before this module the application took ``X-Actor-Id`` / ``X-Actor-Role`` headers at
face value (OQ-01). That is fine for a development workbench and unacceptable for a
deployment where the reviewer-role gate protects the one permanent lifecycle change.
This port keeps the demo path available while giving a real deployment somewhere to
plug in verified identity, and it keeps every route depending on the same
:class:`Principal` regardless of how it was obtained.

The distinction that matters is carried on the principal, not left to be inferred:
:attr:`Principal.authenticated` is ``False`` for the development header identity and
``True`` for a verified credential. ``GET /api/v1/capabilities`` and the audit trail
both report it, so a consumer can never mistake one for the other.

A provider returns a principal or raises one of the :class:`AuthenticationError`
subclasses. Returning ``None`` to mean "no identity" is deliberately not the contract:
an absent identity and a rejected one are different answers to a client, and a caller
that conflates them will either leak or over-refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "AuthenticationError",
    "Credentials",
    "ExpiredCredentialsError",
    "IdentityProvider",
    "InvalidCredentialsError",
    "InvalidIdentityError",
    "MissingCredentialsError",
    "Principal",
]


class AuthenticationError(Exception):
    """Base for every way a request can fail to present a usable identity.

    ``code`` is the stable machine-readable value the error envelope carries, so a
    client can branch on it the same way it branches on every other error code.
    ``status_code`` is the HTTP status the envelope should use; keeping it on the
    error means the route layer does not have to re-derive it from the exception type.
    """

    code: str = "UNAUTHENTICATED"
    status_code: int = 401


class MissingCredentialsError(AuthenticationError):
    """No credential was presented for a surface that requires one."""

    code = "UNAUTHENTICATED"
    status_code = 401


class InvalidCredentialsError(AuthenticationError):
    """A credential was presented and could not be verified.

    Deliberately does not say *why* on the wire: "signature did not match" versus
    "unknown issuer" versus "malformed token" tells an attacker which part to fix.
    The reason is available to logs, not to the response.
    """

    code = "INVALID_CREDENTIALS"
    status_code = 401


class ExpiredCredentialsError(InvalidCredentialsError):
    """The credential was valid once and is past its ``exp``.

    Separated from the generic invalid case only so a client can tell "log in again"
    from "this token is not for us"; both are still a 401.
    """

    code = "CREDENTIALS_EXPIRED"
    status_code = 401


class InvalidIdentityError(AuthenticationError):
    """A demo identity header was blank. Kept for the development path only.

    This is a 422 rather than a 401 because the request is malformed, not
    unauthenticated: the demo headers are labels, and a whitespace label is a bug in
    the caller, not a rejected login.
    """

    code = "INVALID_IDENTITY"
    status_code = 422


@dataclass(frozen=True, slots=True)
class Credentials:
    """The raw identity material a request carried.

    One container for both the real (``Authorization``) and the development
    (``X-Actor-Id`` / ``X-Actor-Role``) forms, so :func:`app.api.deps.principal`
    reads the request headers once and hands the provider a plain value.
    """

    authorization: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified-or-declared identity, resolved to a subject and a role.

    ``authenticated`` is the load-bearing field: it is ``True`` only when a provider
    verified a credential. ``source`` names the mechanism for the audit trail.
    """

    subject: str
    role: str
    authenticated: bool
    source: str
    issuer: str | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    """What the application requires of any identity mechanism.

    ``name`` is recorded so ``/capabilities`` and the logs can say which mechanism
    resolved the request.
    """

    name: str

    def authenticate(self, credentials: Credentials) -> Principal:
        """Return the principal ``credentials`` identifies, or raise.

        Implementations must raise an :class:`AuthenticationError` subclass rather
        than returning a default or anonymous principal: an anonymous principal that
        the role gate then treats as an analyst is exactly the spoofable-header bug
        this port exists to close.
        """
        ...
