"""Per-request correlation and lifecycle logging.

One middleware, doing two things that belong together: it gives every request a
correlation id, and it writes one line when the request finishes.

The correlation id is accepted from the caller when it is safe (an ``X-Request-ID``
header that is short and contains only URL-safe characters), otherwise generated.
Either way it is echoed back on the response, bound to a context variable for the
duration of the request, and included in every log line emitted while the request
is handled -- so a client can take the id off the response and find the exact
server-side lines that produced it. A client-supplied value is never trusted
blindly: an over-long or non-alphanumeric one is discarded rather than reflected,
because echoing arbitrary caller text into a header is an injection vector.

The lifecycle line records the method, path, status and duration. The query string
is deliberately not logged: it can carry a dispute identifier or free text, and the
log is not the place for request payloads. An unhandled exception is logged here
with its category before being re-raised, so a bug produces a correlated,
severity-``ERROR`` line rather than a traceback with no request id next to it.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging_config import (
    REQUEST_ID_HEADER,
    error_category,
    log_event,
    new_request_id,
    normalise_request_id,
    reset_request_id,
    set_request_id,
)

__all__ = ["RequestContextMiddleware"]

LOGGER = logging.getLogger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a correlation id and log the outcome of every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = normalise_request_id(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
        token = set_request_id(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        method = request.method
        path = request.url.path
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Log, then re-raise: this middleware observes, it does not swallow.
                # The application's error handlers run inside ``call_next`` and have
                # already produced a response for the failures they know about, so
                # only a genuine, unhandled bug reaches here.
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "http.request.failed",
                    method=method,
                    path=path,
                    duration_ms=_elapsed_ms(started),
                    error_category=error_category(exc),
                )
                raise
            log_event(
                LOGGER,
                logging.INFO,
                "http.request.completed",
                method=method,
                path=path,
                status_code=response.status_code,
                duration_ms=_elapsed_ms(started),
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            reset_request_id(token)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
