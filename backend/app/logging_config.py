"""Structured logging, in one place.

Every log line this process emits is a single JSON object with a stable ``event``
name and a fixed core of fields, so a log can be filtered by event rather than
parsed out of prose. The shape is deliberately small: a timestamp, a severity, the
event name, and the request id that ties related lines together, with each event's
own fields merged in beside them.

Why JSON and not a formatted string
-----------------------------------
A human-readable line is fine until a field contains a space, a quote or a newline.
Structured logs are read by machines most of the time, and the first time a dispute
description reaches a log line an unescaped formatter turns one record into several.
Serialising to JSON removes the entire class of "the log line broke" problem.

What is never logged
--------------------
Three things, and they are worth stating because each is the obvious debugging aid:
credentials (an API key, a connection password, a token), the full text of a source
document, and the raw prompt sent to or response received from a model. Those leak
customer data and secrets into a channel with different access controls from the
database. :func:`error_category` exists so a provider failure can be logged as
``LlmResponseFormatError`` without ever touching the ``raw_response`` that the
exception happens to carry, which is where the model's untrusted text lives.

Correlation
-----------
The request id lives in a :class:`contextvars.ContextVar`, not on the logger. A
context variable follows the async task that set it and is cleared when the request
ends, so two concurrent requests cannot see each other's id and a worker thread
reused for the next request cannot inherit the previous one's.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import Settings

__all__ = [
    "MAX_REQUEST_ID_LENGTH",
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "configure_logging",
    "current_request_id",
    "error_category",
    "log_event",
    "new_request_id",
    "normalise_request_id",
    "reset_request_id",
    "set_request_id",
]

#: Response/request header carrying the correlation id.
REQUEST_ID_HEADER = "X-Request-ID"

#: Longest correlation id accepted from a client. A caller-supplied id is echoed
#: into the response and every log line for the request, so an unbounded one is an
#: easy way to bloat a log or smuggle control characters into it.
MAX_REQUEST_ID_LENGTH = 128

#: The only characters a correlation id may contain. Anything else -- whitespace, a
#: quote, a control character -- makes the value unsafe to echo into a header or a
#: log line, so it is rejected rather than sanitised.
_REQUEST_ID_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)

#: Core fields the formatter always emits; an event's own fields are merged beside
#: them and may not overwrite them.
_CORE_FIELDS = frozenset({"timestamp", "severity", "event", "request_id", "logger"})

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Generate a fresh, opaque correlation id."""
    return uuid4().hex


def normalise_request_id(raw: str | None) -> str | None:
    """Return ``raw`` if it is safe to adopt as a correlation id, else ``None``.

    Adopting a client's id is what lets a front end correlate its own log with the
    server's. It is only safe if the value cannot be used to inject into a log line
    or a response header, so an over-long value or one containing anything but
    ``[A-Za-z0-9._:-]`` is rejected outright and the caller generates a fresh one.
    Rejecting is deliberate: sanitising a bad id into a plausible one would hide a
    misbehaving client and still produce a collision-prone id.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
        return None
    if not all(char in _REQUEST_ID_ALPHABET for char in candidate):
        return None
    return candidate


def set_request_id(value: str) -> Token[str | None]:
    """Bind ``value`` as the current request's correlation id."""
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the correlation id that was active before :func:`set_request_id`."""
    _request_id.reset(token)


def current_request_id() -> str | None:
    """The correlation id of the request being handled, or ``None`` outside one."""
    return _request_id.get()


def error_category(exc: BaseException) -> str:
    """A safe, stable label for ``exc``: its class name, and nothing else.

    Used instead of ``str(exc)`` whenever a failure is logged. Exception messages
    are attacker-influenced and, for :class:`~app.ports.llm.LlmResponseFormatError`,
    contain the raw model response -- untrusted text this system promises never to
    write to a log. The class name is enough to triage by, and the message stays
    where it belongs: in the error the caller already received.
    """
    return type(exc).__name__


class JsonFormatter(logging.Formatter):
    """Render every record as a single JSON object.

    A record may carry ``event`` (a stable name) and ``fields`` (a mapping of
    structured context). When it does not -- a third-party library's record, say --
    the formatted message becomes the ``event`` and nothing is lost.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": current_request_id(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in _CORE_FIELDS:
                    payload[key] = value
        if record.exc_info:
            # The traceback is kept for a genuine bug. The exception message is not
            # appended separately: it is already inside the traceback, and the pattern
            # this module exists to avoid is ``str(exc)`` being logged on its own.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _resolve_level(name: str) -> int:
    """Turn a settings string into a logging level, defaulting to ``INFO``."""
    resolved = logging.getLevelName(str(name).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(settings: Settings) -> None:
    """Attach the JSON handler to the application logger, once per process.

    Idempotent on purpose: ``create_app`` is called by every test module, and each
    call must not stack another handler on and multiply every line. The level comes
    from ``app_log_level`` so that setting finally has a consumer.
    """
    logger = logging.getLogger("app")
    logger.setLevel(_resolve_level(settings.app_log_level))
    for handler in logger.handlers:
        if getattr(handler, "_resolveiq_json", False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._resolveiq_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit one structured event.

    ``event`` is a stable dotted name (``ai.provider.invoked``) so a log can be
    filtered by it. ``fields`` are the event's structured context and are merged
    into the JSON object beside the core fields. Passing a field named ``event`` or
    ``request_id`` cannot overwrite the core ones: the formatter ignores those keys.
    ``exc_info`` attaches the current traceback, for the rare case (an unhandled
    failure) where the stack, not just the category, is the useful part.
    """
    logger.log(level, event, extra={"event": event, "fields": fields}, exc_info=exc_info)
