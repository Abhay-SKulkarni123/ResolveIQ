"""Identity adapters.

One module per mechanism; :mod:`app.api.deps` chooses between them from settings.
Deliberately no shared base class: the port is a ``Protocol``, so an adapter needs no
inheritance and no import from the port's package.
"""

from __future__ import annotations

from app.adapters.auth.demo import DemoIdentityProvider
from app.adapters.auth.hmac_tokens import HmacTokenIdentityProvider, issue_token

__all__ = ["DemoIdentityProvider", "HmacTokenIdentityProvider", "issue_token"]
