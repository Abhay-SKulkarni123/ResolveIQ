"""Transport-level hardening: security headers and a fixed-window rate limiter.

These sit below the routes and above the store, and neither one knows anything about a
dispute. They exist because the failure modes they close -- a missing ``nosniff`` that
lets a browser sniff a JSON error as HTML, a leaked ``Host`` header, an endpoint that
answers a million requests a minute -- are not defects in any one route and have no
natural home inside one.

Both are :class:`~starlette.middleware.base.BaseHTTPMiddleware`, which is enough here:
they either add headers to a response or short-circuit a request, and neither needs to
observe the response body.

The rate limiter is deliberately in-process. A deployment behind several replicas gets
a per-replica budget rather than a global one; that is a known limit, stated rather
than implied, and a shared counter (Redis) is the upgrade when it matters. It exists to
blunt an accidental loop or a trivial flood, not to be a WAF.
"""

from __future__ import annotations

import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.errors import error_response

__all__ = ["RateLimitMiddleware", "SecurityHeadersMiddleware"]

#: Conservative, correct-for-a-JSON-API headers. ``frame-options: DENY`` and
#: ``nosniff`` are the two that matter for an API that is never meant to be framed or
#: content-sniffed; the rest are cheap defence in depth.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers to every response.

    ``setdefault`` rather than assignment so a route that deliberately sets one of
    these does not have its choice overwritten.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A fixed-window per-client limiter.

    The window is wall-clock-aligned and shared: when the window index ticks over every
    bucket is dropped at once, which is the simple, slightly bursty behaviour a fixed
    window implies. Keys are client hosts, which is the only identity available before
    a route resolves one; a proxy deployment must therefore be configured to present
    the real client (``FORWARDED_ALLOW_IPS``) or every caller shares one bucket.
    """

    def __init__(
        self,
        app: object,
        *,
        requests: int = 120,
        window_seconds: int = 60,
        exempt_paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limit = max(1, requests)
        self._window = max(1, window_seconds)
        self._exempt = frozenset(exempt_paths)
        self._hits: dict[str, int] = {}
        self._window_index = -1
        # A middleware instance is shared across threads when a sync server runs the
        # app, so the counter is mutated under a lock.
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Preflights and the liveness probe are never throttled: a CORS preflight is
        # cheap and refusing it breaks the browser before the real request is made, and
        # health is what a supervisor polls.
        if request.method == "OPTIONS" or request.url.path in self._exempt:
            return await call_next(request)

        now = time.monotonic()
        window_index = int(now // self._window)
        key = self._client_key(request)
        with self._lock:
            if window_index != self._window_index:
                self._hits.clear()
                self._window_index = window_index
            count = self._hits.get(key, 0) + 1
            self._hits[key] = count

        if count > self._limit:
            retry_after = max(1, self._window - int(now % self._window))
            response = error_response(
                429,
                "RATE_LIMITED",
                "Too many requests; slow down and retry.",
                {"retry_after_seconds": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        return await call_next(request)

    @staticmethod
    def _client_key(request: Request) -> str:
        client = request.client
        return client.host if client and client.host else "unknown"
