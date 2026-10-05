"""Fixtures for tests that need a real PostgreSQL.

Every test here is marked ``integration`` and every one of them depends on
``live_database``. When PostgreSQL is not reachable that fixture skips, so a
developer without a database still gets a green unit suite instead of a wall of
errors that all mean the same thing.

The database used is always the disposable one from ``Settings.test_database_url``.
The tests run ``alembic downgrade base``, which drops every table, so pointing
them at the development database would destroy real data. ``refuse_destructive_
tests_on_development_database`` makes that mistake impossible rather than merely
discouraged.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.adapters.persistence import (
    DatabaseConnectionError,
    check_connection,
    create_engine_for_url,
    get_sessionmaker,
)
from app.config import TEST_DATABASE_NAME, get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
MIGRATIONS_DIR = BACKEND_ROOT / "migrations"

#: Tables Alembic and the models own. Everything else in the database is not ours
#: and must not be inspected or dropped.
MANAGED_TABLES = ("accounts", "contracts", "contract_price_terms")


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of the disposable test database."""
    return get_settings().test_database_url


@pytest.fixture(scope="session")
def live_database(database_url: str) -> str:
    """Skip the whole integration suite when PostgreSQL is not usable."""
    try:
        engine = create_engine_for_url(database_url)
        check_connection(engine)
        engine.dispose()
    except DatabaseConnectionError as exc:
        pytest.skip(
            f"PostgreSQL is not reachable for the integration tests: {exc}\n"
            f"Create the role and databases with:\n"
            f"  psql -h 127.0.0.1 -p 5432 -U <superuser> -d postgres "
            f"-f backend/scripts/create_dev_database.sql"
        )
    return database_url


@pytest.fixture(scope="session")
def refuse_destructive_tests_on_development_database(database_url: str, live_database: str) -> None:
    """Guard against running a downgrade against a database that holds data.

    The check is on the database *name*, because that is the signal a human
    recognises. It is not a security control.
    """
    if TEST_DATABASE_NAME not in database_url:
        pytest.fail(
            f"Refusing to run destructive migration tests against {database_url!r}: "
            f"the database name is not {TEST_DATABASE_NAME!r}."
        )


@pytest.fixture(scope="session")
def engine(live_database: str) -> Iterator[Engine]:
    """Engine for the test database, disposed when the session ends."""
    active = create_engine_for_url(live_database)
    yield active
    active.dispose()


@pytest.fixture(scope="session")
def alembic_config(live_database: str) -> Config:
    """Alembic configuration aimed at the test database.

    The URL travels in ``ALEMBIC_DATABASE_URL`` rather than in the Config object
    because Alembic's ConfigParser would try to interpolate a ``%`` in the
    password. ``migrations/env.py`` reads it first.
    """
    os.environ["ALEMBIC_DATABASE_URL"] = live_database
    # The Settings cache would otherwise keep serving the development URL.
    get_settings.cache_clear()

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


@pytest.fixture()
def migrated_database(
    alembic_config: Config, engine: Engine, refuse_destructive_tests_on_development_database: None
) -> Iterator[Engine]:
    """A database at head revision, left clean for the next test.

    Migrating up before each test and down after it keeps the cases independent
    and leaves nothing behind for the next run to trip over. The downgrade is also
    what proves the migration is reversible, so it is not wasted work.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    yield engine
    command.downgrade(alembic_config, "base")


@pytest.fixture()
def db_session(migrated_database: Engine) -> Iterator[Session]:
    """Session against a freshly migrated schema."""
    factory = get_sessionmaker(migrated_database)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def connection(engine: Engine) -> Iterator[object]:
    """Raw connection, for asserting on PostgreSQL's own catalog."""
    with engine.connect() as active:
        yield active


def _schema_expression(dialect_name: str) -> str:
    """SQL returning the current schema name as a single string.

    PostgreSQL calls the default schema ``public`` and exposes
    ``current_schema()``; MySQL has no separate schema namespace and exposes the
    current database as ``DATABASE()``. Hard-coding ``'public'`` silently returned
    nothing on MySQL, which would have made every table-presence assertion pass
    vacuously.
    """
    return "current_schema()" if dialect_name == "postgresql" else "DATABASE()"


def table_names(connection: object) -> set[str]:
    """Every table in the current schema, excluding alembic's own bookkeeping."""
    dialect_name = connection.engine.dialect.name  # type: ignore[attr-defined]
    result = connection.execute(  # type: ignore[attr-defined]
        text(
            f"""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = {_schema_expression(dialect_name)}
              AND table_type = 'BASE TABLE'
              AND table_name <> 'alembic_version'
            """
        )
    )
    return set(result.scalars())


#: MySQL always reports a table's primary key as ``PRIMARY`` and ignores whatever
#: ``CONSTRAINT`` name was supplied for it, so it cannot carry the
#: ``pk_<table>`` name that PostgreSQL records. This maps that engine-mandated
#: name back so the same expectation can be asserted on both.
MYSQL_PRIMARY_KEY_NAME = "PRIMARY"


def constraint_names(connection: object, table: str) -> set[str]:
    """Every constraint on ``table``, whatever its type, in either dialect."""
    dialect_name = connection.engine.dialect.name  # type: ignore[attr-defined]
    if dialect_name == "mysql":
        result = connection.execute(  # type: ignore[attr-defined]
            text(
                """
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                """
            ),
            {"table_name": table},
        )
    else:
        result = connection.execute(  # type: ignore[attr-defined]
            text(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = to_regclass(:table_name)
                """
            ),
            {"table_name": f"public.{table}"},
        )
    return set(result.scalars())
