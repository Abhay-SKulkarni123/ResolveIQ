"""Alembic environment.

Two responsibilities, and deliberately nothing else:

1. Tell Alembic where the schema is defined (``Base.metadata``).
2. Tell Alembic where the database is (the environment, not this file).

The application is not started and no settings beyond the database URL are read.
Importing ``app.main`` here would mean a migration depends on FastAPI being
importable and configured, which is exactly the coupling that makes an upgrade
fail on a machine where the API cannot boot.

Comparison settings
-------------------
``compare_type=True`` and ``compare_server_default=True`` are on because this
schema's correctness depends on the difference between ``NUMERIC(19,4)`` and
``NUMERIC`` and between ``TIMESTAMP WITH TIME ZONE`` and ``TIMESTAMP``. Without
them, autogenerate would report "no changes" for a precision that had been
silently narrowed.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.adapters.persistence import Base
from app.config import get_settings

config = context.config


def get_database_url() -> str:
    """Resolve the URL, in order of precedence.

    1. ``ALEMBIC_DATABASE_URL`` in the environment. This is how automation points
       the same migration files at the disposable test database. It is preferred
       over the ini option because Alembic's ConfigParser treats ``%`` as an
       interpolation character, so a password containing one would be mangled.
    2. ``sqlalchemy.url`` in ``alembic.ini``, for a deliberate one-off override.
    3. ``Settings.database_url``, which is the normal path and reads ``.env``.

    ``alembic.ini`` leaves ``sqlalchemy.url`` empty so that no credential is ever
    committed.
    """
    from_environment = os.environ.get("ALEMBIC_DATABASE_URL", "").strip()
    if from_environment:
        return from_environment

    from_ini = config.get_main_option("sqlalchemy.url", "")
    if from_ini:
        return from_ini

    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``).

    Useful for reviewing a migration before it runs and for checking that a
    revision produces valid PostgreSQL without needing a live server.
    """
    context.configure(
        url=get_database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database.

    ``pool.NullPool`` because Alembic is a short-lived process: a connection pool
    would only delay shutdown.
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
