"""Engine and session lifecycle, and credential-safe handling of failures.

This module owns everything about *how* the process reaches PostgreSQL. It knows
nothing about accounts or contracts.

Two rules shape the design:

**A connection password must never reach a log, an exception message or an API
response.** A failed connection is one of the most likely moments for a URL to be
logged verbatim, and the URL is where the password lives. Every error raised here
is assembled from a redacted URL, and any fragment of the driver message that
happens to contain the password is masked before it leaves this module. The
behaviour is pinned by ``tests/unit/test_database.py``.

**PostgreSQL is the only supported backend** (ADR-014). ``create_engine_for_url``
says so explicitly rather than letting a ``sqlite://`` URL produce a working but
wrong-shaped database, because the schema uses ``JSONB``, native ``uuid`` and
``timestamptz``, none of which behave the same way elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_OVERFLOW",
    "DEFAULT_POOL_SIZE",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "UnsupportedDatabaseError",
    "check_connection",
    "create_engine_for_url",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "redact_database_url",
    "session_scope",
]

logger = logging.getLogger(__name__)

#: A small pool. The backend is an internal tool, not a public API, so a large
#: pool would only hold idle PostgreSQL backends open against the database the
#: user's other tools are using.
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10

#: Seconds to wait for the TCP connection before giving up. Without this a
#: firewalled host leaves a request thread blocked for the OS default.
CONNECT_TIMEOUT_SECONDS = 5

_POSTGRESQL_BACKEND = "postgresql"

# Process-wide caches. The engine holds a connection pool, so creating one per
# request would leak backends; the settings object is already cached, so reading
# the URL again would return the same value anyway.
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class DatabaseConfigurationError(RuntimeError):
    """The configured database URL is missing or unusable."""


class UnsupportedDatabaseError(DatabaseConfigurationError):
    """The URL points at something other than PostgreSQL."""


class DatabaseConnectionError(RuntimeError):
    """The database could not be reached. The message never contains credentials."""


def _redact(message: str, secret: str | None) -> str:
    """Mask a secret if it appears anywhere in ``message``."""
    if not secret:
        return message
    return message.replace(secret, "***")


def redact_database_url(url: str) -> str:
    """Return a form of ``url`` that is safe to log.

    Used for every log line and error message that mentions the database. An
    unparseable URL is reported as such rather than echoed, because a malformed
    URL is exactly the case where a naive fallback might print it whole.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<unparseable database url>"


def _parse_postgresql_url(url: str) -> URL:
    """Parse ``url`` and confirm it addresses PostgreSQL."""
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not a valid connection URL; expected something like "
            "postgresql+psycopg://user:password@host:5432/database"
        ) from exc

    if parsed.get_backend_name() != _POSTGRESQL_BACKEND:
        raise UnsupportedDatabaseError(
            f"ResolveIQ supports PostgreSQL only (ADR-014), but DATABASE_URL uses "
            f"{parsed.get_backend_name()!r}. Point it at a PostgreSQL database."
        )
    return parsed


def create_engine_for_url(url: str, *, echo: bool = False) -> Engine:
    """Build a new engine for ``url``.

    ``pool_pre_ping`` is on because the connection to a developer database is
    long-lived and idle: PostgreSQL or a laptop may have closed it in the
    meantime, and a stale pooled connection otherwise surfaces as an error on the
    first request after a quiet spell. The ping costs one round trip and removes
    an entire class of intermittent failure.
    """
    _parse_postgresql_url(url)
    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=DEFAULT_POOL_SIZE,
        max_overflow=DEFAULT_MAX_OVERFLOW,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        active = settings or get_settings()
        _engine = create_engine_for_url(active.database_url, echo=active.app_env == "development")
        logger.info("database engine created for %s", redact_database_url(active.database_url))
    return _engine


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``, or to the shared engine.

    ``expire_on_commit=False`` because objects are read after a commit to build a
    response. With the default, that read triggers a refresh SELECT per attribute;
    the objects are already in memory and still current.
    """
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Session lifecycle for scripts, data migrations and tests.

    Commits when the block exits normally, rolls back if it raises, and always
    closes. A caller that needs to inspect a failure *and* decide whether to
    commit must use ``get_sessionmaker`` directly rather than this helper.
    """
    session = get_sessionmaker(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(engine: Engine | None = None) -> None:
    """Verify the database is reachable, raising ``DatabaseConnectionError``.

    Used by the integration tests and, later, by the readiness probe. The message
    names the server, the database and the driver error, and nothing else.

    Only ``DBAPIError`` is translated. A programming mistake such as an invalid
    statement raises ``InvalidRequestError``, which is left alone: reporting it as
    "could not reach the database" would send the reader looking at the network
    instead of at the code.
    """
    active = engine if engine is not None else get_engine()
    secret = active.url.password
    try:
        with active.connect() as connection:
            connection.execute(text("SELECT 1"))
    except DBAPIError as exc:
        raise DatabaseConnectionError(
            f"could not reach PostgreSQL at {redact_database_url(str(active.url))}: "
            f"{_redact(str(exc.orig), secret)}"
        ) from exc


def dispose_engine() -> None:
    """Close the pooled connections and clear the caches.

    Tests call this between cases. Nothing else should: disposing the engine
    discards the pool that ``get_engine`` promises to keep.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
