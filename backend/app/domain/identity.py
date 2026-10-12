"""The role vocabulary and the one permission this build enforces server-side.

Roles are strings, not an enum, because the demo path accepts an arbitrary role
string and the audit trail records it verbatim. What must not be arbitrary is the
*decision* the application makes from a role, so that lives here as a single
predicate rather than as an ``if role == "reviewer"`` scattered across routes.

Only one permission exists today: closing a case. Everything else that a reviewer
might be allowed to do (approve an adjustment, execute a credit) is deliberately not
implemented, so there is no permission to grant for it and no way to grant one.
"""

from __future__ import annotations

__all__ = ["ANALYST", "REVIEWER", "can_decide", "known_roles"]

ANALYST = "analyst"
REVIEWER = "reviewer"

#: Roles the UI offers. The API accepts any non-blank role string for attribution,
#: but only these two carry a meaning, and only :data:`REVIEWER` unlocks anything.
known_roles = (ANALYST, REVIEWER)


def can_decide(role: str) -> bool:
    """Whether ``role`` may close a case (``RESOLVED`` / ``REJECTED``).

    The single authorisation predicate in the application. It is consulted on the
    decision route only, and it is applied to the *verified* role in ``AUTH_MODE=token``
    and to the declared role in ``AUTH_MODE=demo`` -- which is exactly why the demo
    mode is not a security control.
    """
    return role == REVIEWER
