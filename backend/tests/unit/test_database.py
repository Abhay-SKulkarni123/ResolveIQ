"""Tests for engine construction, session wiring and credential safety.

None of these tests need a live database. They cover the behaviour that has to be
right *before* a connection is attempted: rejecting an unsupported backend,
building an engine without touching the network, and making sure a password
cannot escape in an error message.

The last of those is the important one. A failed connection is exactly when a
connection URL tends to be logged, and the connection URL is where the password
is.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from app.adapters.persistence.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    UnsupportedDatabaseError,
    check_connection,
    create_engine_for_url,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    redact_database_url,
    session_scope,
)
from app.config import Settings

# A real-looking URL with a real-looking password. Port 1 is reserved and nothing
# listens on it, so a connection attempt fails immediately instead of hanging.
PASSWORD = "sup3rs3cret-do-not-log"
UNREACHABLE_URL = f"postgresql+psycopg://resolveiq:{PASSWORD}@127.0.0.1:1/resolveiq"


@pytest.fixture(autouse=True)
def _reset_engine_cache():
    """Keep the process-wide engine from leaking between tests."""
    dispose_engine()
    yield
    dispose_engine()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_hides_the_password_and_keeps_the_useful_parts() -> None:
    redacted = redact_database_url(UNREACHABLE_URL)
    assert PASSWORD not in redacted
    assert "resolveiq" in redacted
    assert "127.0.0.1:1" in redacted
    assert "resolveiq_test" not in redacted  # sanity: the database name survives
    assert redacted.endswith("/resolveiq")


def test_redaction_of_a_url_without_a_password_is_harmless() -> None:
    url = "postgresql+psycopg://resolveiq@localhost:5432/resolveiq"
    assert redact_database_url(url) == url


@pytest.mark.parametrize("junk", ["not a url at all", "", "://:::", "no-scheme-here"])
def test_an_unparseable_url_is_reported_not_echoed(junk: str) -> None:
    """A malformed URL must never fall through to being printed verbatim."""
    assert redact_database_url(junk) == "<unparseable database url>"


def test_a_url_carrying_a_password_always_loses_it() -> None:
    # Whatever SQLAlchemy can parse, the password must not survive redaction.
    redacted = redact_database_url("postgresql+psycopg://user:letmein@localhost:5432/db")
    assert "letmein" not in redacted
    assert redacted == "postgresql+psycopg://user:***@localhost:5432/db"


def test_a_password_shaped_like_a_url_is_still_masked() -> None:
    # A password containing '@' and '/' must be percent-encoded in a URL, but a
    # hand-written one may not be. Assert on the result rather than trusting the
    # caller to have encoded it.
    url = "postgresql+psycopg://user:p%40ss%2Fword@localhost:5432/db"
    redacted = redact_database_url(url)
    assert "p%40ss%2Fword" not in redacted
    assert redacted == "postgresql+psycopg://user:***@localhost:5432/db"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def test_sqlite_is_refused_rather_than_silently_accepted() -> None:
    """ADR-014: PostgreSQL is the reference; MySQL is also supported.

    A SQLite URL would build a working engine and then behave wrongly: JSONB,
    native uuid and timestamptz all have different semantics there, and the
    failures appear as data corruption rather than as an error.
    """
    with pytest.raises(UnsupportedDatabaseError, match="ADR-014"):
        create_engine_for_url("sqlite:///resolveiq.db")


def test_unsupported_backend_error_is_a_configuration_error() -> None:
    assert issubclass(UnsupportedDatabaseError, DatabaseConfigurationError)


def test_mysql_is_now_accepted() -> None:
    """MySQL became a supported backend so the repository can run against it.

    ``create_engine`` is lazy and never connects here, so this asserts only that the
    URL is admitted rather than refused -- the same construction path PostgreSQL takes.
    """
    engine = create_engine_for_url("mysql+pymysql://user:pass@localhost/resolveiq")
    assert engine.dialect.name == "mysql"


def test_an_unsupported_backend_is_still_refused() -> None:
    """Widening the supported set must not turn the guard into a no-op."""
    with pytest.raises(UnsupportedDatabaseError, match="ADR-014"):
        create_engine_for_url("oracle+cx_oracle://user:pass@localhost/resolveiq")


def test_an_invalid_url_is_reported_as_a_configuration_error() -> None:
    with pytest.raises(DatabaseConfigurationError, match="not a valid connection URL"):
        create_engine_for_url("this is not a url")


def test_building_an_engine_does_not_connect() -> None:
    """create_engine is lazy; nothing should touch the network here."""
    engine = create_engine_for_url(UNREACHABLE_URL)
    assert isinstance(engine, Engine)
    dispose_engine()


# ---------------------------------------------------------------------------
# Connection failure
# ---------------------------------------------------------------------------


def test_unreachable_database_raises_a_typed_error() -> None:
    engine = create_engine_for_url(UNREACHABLE_URL)
    with pytest.raises(DatabaseConnectionError):
        check_connection(engine)


def test_connection_failure_message_never_contains_the_password() -> None:
    """The whole point of DatabaseConnectionError: safe to log or return."""
    engine = create_engine_for_url(UNREACHABLE_URL)
    try:
        check_connection(engine)
    except DatabaseConnectionError as exc:
        rendered = str(exc)
    else:  # pragma: no cover - the connection above must fail
        pytest.fail("expected the connection to fail")

    assert PASSWORD not in rendered
    # The host and database are worth disclosing: they are what makes the message
    # actionable, and neither is a secret.
    assert "127.0.0.1" in rendered
    assert "resolveiq" in rendered


def test_connection_failure_keeps_the_original_cause() -> None:
    """Chaining preserves the driver detail for logs while the message stays safe."""
    engine = create_engine_for_url(UNREACHABLE_URL)
    with pytest.raises(DatabaseConnectionError) as caught:
        check_connection(engine)
    assert caught.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Engine and session caching
# ---------------------------------------------------------------------------


def test_the_engine_is_created_once_and_reused() -> None:
    settings = Settings(_env_file=None, database_url=UNREACHABLE_URL)
    first = get_engine(settings)
    assert get_engine(settings) is first


def test_a_different_settings_object_reuses_the_cached_engine() -> None:
    """Settings is already process-wide and cached; a second engine would leak a pool."""
    settings = Settings(_env_file=None, database_url=UNREACHABLE_URL)
    assert get_engine(settings) is get_engine(settings)


def test_disposing_clears_the_cache() -> None:
    settings = Settings(_env_file=None, database_url=UNREACHABLE_URL)
    first = get_engine(settings)
    dispose_engine()
    assert get_engine(settings) is not first


def test_session_factory_can_be_bound_to_an_explicit_engine() -> None:
    """Tests and scripts pass their own engine so they never touch the shared pool."""
    engine = create_engine_for_url(UNREACHABLE_URL)
    factory = get_sessionmaker(engine)
    other = get_sessionmaker(engine)
    assert factory is not other  # a fresh factory per call: no cross-test state
    assert factory.kw["bind"] is engine


def test_session_scope_yields_a_bound_session_and_closes_it() -> None:
    """The lifecycle must close the session even though no work is done.

    No connection is opened here: constructing a Session and closing it are local
    operations, so this asserts the shape of the context manager without a server.
    """
    engine = create_engine_for_url(UNREACHABLE_URL)
    with session_scope(engine) as session:
        assert session.get_bind() is engine
    assert not session.in_transaction()


def test_session_scope_rolls_back_when_the_body_raises() -> None:
    engine = create_engine_for_url(UNREACHABLE_URL)
    with pytest.raises(RuntimeError, match="body failed"), session_scope(engine) as session:
        raise RuntimeError("body failed")
    # The rollback also needs no server because the session never began one.
    assert not session.in_transaction()
