"""The error envelope from docs/SYSTEM_DESIGN.md §11, and how each failure maps to it.

    {"error": {"code", "message", "details", "request_id"}}

One shape for every error, with a stable machine-readable ``code`` the frontend
branches on. This matters more than it looks: the reviewer UI has to distinguish
"this investigation is stale" from "this case does not exist" from "the database is
down", and parsing prose to do it would put English into the control flow.

``request_id`` is a per-request UUID, not a trace of anything upstream, because
there is no tracing infrastructure to correlate with. It is honest as a correlation
token for the request itself and the audit trail, which is what the design asks it
for.

Mapping rules, and the two that took thought
--------------------------------------------
``CaseTransitionError`` -> **409**. The request was well-formed and the case exists,
but its state forbids the operation. This is distinct from ``EvidenceError`` or
``SourceDocumentError``, which are **422**: those are about the *content* of what was
sent. Collapsing them would make the frontend disable the right action for the wrong
reason -- showing "cannot investigate, the case is stale" when the real problem was a
malformed amount.

``sqlalchemy.exc.OperationalError`` -> **503** with a retry hint, never a 500. §10
lists "DB unavailable" as a named failure (F15) and requires that no partial writes
survive. One transaction per save gives that; the mapping gives the caller a reason to
retry rather than a stack trace.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.cases import CaseTransitionError
from app.domain.evidence import EvidenceError
from app.logging_config import current_request_id, error_category, log_event
from app.ports.cases import CaseConflictError, CaseNotFoundError
from app.services.cases import CaseNotInvestigableError
from app.services.source_document import SourceDocumentError

__all__ = ["ApiError", "error_response", "install_error_handlers"]

LOGGER = logging.getLogger(__name__)


def _log_error(status_code: int, code: str, exc: BaseException | None = None) -> None:
    """Record one handled failure, without ever copying the exception message.

    The exception's *category* is logged and its message is not: a message can carry
    request-supplied text or a model's raw output, and a log is the wrong place for
    either. ``str(exc)`` is what the caller already received; the log only needs to
    say what class of failure this was and what code the caller was given.
    """
    level = logging.ERROR if status_code >= 500 else logging.WARNING
    fields: dict[str, Any] = {"status_code": status_code, "code": code}
    if exc is not None:
        fields["error_category"] = error_category(exc)
    log_event(LOGGER, level, "api.error.responded", **fields)


class ApiError(Exception):
    """An error with a code the frontend can branch on.

    Raised by routes for failures that have no better-typed exception behind them.
    Domain and service exceptions are mapped in ``install_error_handlers`` instead,
    so a rule violation surfaces as the exception that states the rule.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                # The correlation id of the request being handled, so the envelope,
                # the response header and the server log all name the same request.
                # A fresh uuid is the fallback for a response built outside a request
                # (a direct call in a test); there is no request to correlate then.
                "request_id": current_request_id() or str(uuid4()),
            }
        },
    )


def install_error_handlers(application: FastAPI) -> None:
    """Map every known failure onto the envelope.

    Registered per exception type rather than in one catch-all, because the catch-all
    is where a genuine bug gets reported to a user as a validation problem.
    """

    @application.exception_handler(ApiError)
    def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        # Registered before the domain handlers so a route can report a
        # request-shape problem in the same envelope as everything else. Left
        # unregistered it would escape as an unhandled exception, and FastAPI's
        # default 500 body does not match the documented error shape -- so a
        # malformed id would look like a server fault to every client.
        _log_error(exc.status_code, exc.code, exc)
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @application.exception_handler(CaseNotFoundError)
    def _not_found(_: Request, exc: CaseNotFoundError) -> JSONResponse:
        _log_error(404, "DISPUTE_NOT_FOUND", exc)
        return error_response(404, "DISPUTE_NOT_FOUND", str(exc))

    @application.exception_handler(CaseConflictError)
    def _conflict(_: Request, exc: CaseConflictError) -> JSONResponse:
        # 409 with a code of its own rather than the generic TRANSITION one: this is a
        # write-write race, and the frontend's response is to re-read, not to explain
        # that the case is in the wrong state.
        _log_error(409, "VERSION_CONFLICT", exc)
        return error_response(409, "VERSION_CONFLICT", str(exc))

    @application.exception_handler(CaseTransitionError)
    def _transition(_: Request, exc: CaseTransitionError) -> JSONResponse:
        _log_error(409, "INVALID_TRANSITION", exc)
        return error_response(409, "INVALID_TRANSITION", str(exc))

    @application.exception_handler(CaseNotInvestigableError)
    def _not_investigable(_: Request, exc: CaseNotInvestigableError) -> JSONResponse:
        _log_error(422, "NOT_INVESTIGABLE", exc)
        return error_response(422, "NOT_INVESTIGABLE", str(exc))

    @application.exception_handler(SourceDocumentError)
    def _source_document(_: Request, exc: SourceDocumentError) -> JSONResponse:
        _log_error(422, "INVALID_INGEST_PAYLOAD", exc)
        return error_response(422, "INVALID_INGEST_PAYLOAD", str(exc))

    @application.exception_handler(EvidenceError)
    def _evidence(_: Request, exc: EvidenceError) -> JSONResponse:
        _log_error(422, "INVALID_EVIDENCE", exc)
        return error_response(422, "INVALID_EVIDENCE", str(exc))

    @application.exception_handler(OperationalError)
    def _database_down(_: Request, exc: OperationalError) -> JSONResponse:
        # Logged with the driver detail and a traceback, returned without either. The
        # connection string can contain a password, and an error body is the last
        # place that should start leaking one (NEP-009). The category is a field; the
        # traceback is kept because a 503 from the database is exactly the failure a
        # stack is useful for.
        log_event(
            LOGGER,
            logging.ERROR,
            "api.error.responded",
            exc_info=True,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            error_category=error_category(exc),
        )
        return error_response(
            503,
            "DATABASE_UNAVAILABLE",
            "The database is not reachable. Nothing was written; retry once it is back.",
            {"retryable": True},
        )

    @application.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        _log_error(422, "VALIDATION_FAILED", exc)
        return error_response(
            422,
            "VALIDATION_FAILED",
            "The request body did not validate.",
            {"errors": _sanitise_validation(exc.errors())},
        )

    @application.exception_handler(StarletteHTTPException)
    def _http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Routing failures bypass every handler above, because they are raised by
        # Starlette before a route is ever entered. Left alone they answer with
        # ``{"detail": ...}``, which is the second error shape a client has to
        # understand -- and the one a frontend is least likely to have been written
        # against, because nothing in the happy path produces it. A 405 is separated
        # from a 404 because they need different responses: one means fix the URL, the
        # other means fix the method.
        code = {
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, "HTTP_ERROR")
        _log_error(exc.status_code, code, exc)
        return error_response(
            exc.status_code,
            code,
            str(exc.detail) if exc.detail else "The request could not be handled.",
        )

    @application.exception_handler(Exception)
    def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # The last line of defence for a genuine bug. Without it Starlette answers with
        # a bare ``Internal Server Error``, which is a second error shape the frontend
        # would have to understand and which tells a caller nothing it can act on. The
        # exception is logged with its traceback and its category, and the response
        # carries neither: an unhandled exception's message can contain request data or
        # a file path, and neither belongs in a body a browser will render.
        log_event(
            LOGGER,
            logging.ERROR,
            "api.error.responded",
            exc_info=True,
            status_code=500,
            code="INTERNAL_ERROR",
            error_category=error_category(exc),
        )
        return error_response(
            500,
            "INTERNAL_ERROR",
            "An unexpected error occurred and the request was not completed.",
            {"retryable": True},
        )


def _sanitise_validation(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip values out of pydantic's error list.

    pydantic includes the offending input, which for this API is the whole ingest
    payload: an invoice, a customer's complaint, contract terms. Echoing that back
    into a 422 body a client will log is a data leak with no benefit, since the client
    already has what it sent.
    """
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        cleaned.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "type": error.get("type", ""),
                "msg": error.get("msg", ""),
            }
        )
    return cleaned