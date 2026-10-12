"""The development identity provider: ``X-Actor-Id`` / ``X-Actor-Role`` headers.

This is the OQ-01 path, unchanged in behaviour but now confined to ``AUTH_MODE=demo``.
It exists so the analyst/reviewer split can be demonstrated and so the workbench is
usable from a browser with no token, and it is explicitly *not* authentication: it
grants an identity to anyone who asks, which is why the principal it returns carries
``authenticated=False`` and ``AUTH_MODE=token`` is required in production.

Keeping it as a provider rather than as branchy code in a dependency means the switch
between the demo path and a verified one is a single object, and every route keeps
depending on the same :class:`~app.ports.auth.Principal`.
"""

from __future__ import annotations

from app.ports.auth import (
    Credentials,
    InvalidIdentityError,
    MissingCredentialsError,
    Principal,
)

__all__ = ["DemoIdentityProvider"]


class DemoIdentityProvider:
    """Resolves the development identity from the actor headers, with a default."""

    name = "demo-header"

    def __init__(
        self,
        *,
        enabled: bool = True,
        default_actor_id: str = "dev-analyst",
        default_actor_role: str = "analyst",
    ) -> None:
        self._enabled = enabled
        self._default_actor_id = default_actor_id
        self._default_actor_role = default_actor_role

    def authenticate(self, credentials: Credentials) -> Principal:
        if not self._enabled:
            raise MissingCredentialsError(
                "demo identity is disabled and this build has AUTH_MODE=demo; "
                "set AUTH_MODE=token and send a bearer token"
            )
        actor_id = (credentials.actor_id or self._default_actor_id).strip()
        role = (credentials.actor_role or self._default_actor_role).strip()
        if not actor_id:
            raise InvalidIdentityError("X-Actor-Id must not be blank")
        if not role:
            raise InvalidIdentityError("X-Actor-Role must not be blank")
        return Principal(
            subject=actor_id[:128],
            role=role[:64],
            authenticated=False,
            source=self.name,
        )
