"""The migration is applied, reversed and applied again against a real server.

These tests are the reason the migration exists in a form that can be trusted. A
hand-written migration that has only ever been read is not a migration; this file
executes it, inspects what the server actually built, reverses it, and executes it
again.

The database is inspected through ``information_schema``, which both PostgreSQL
and MySQL implement, with the schema name and the physical spelling of each type
resolved for the engine that is running. PostgreSQL names the default schema
``public`` and has ``uuid``, ``jsonb`` and ``timestamptz``; MySQL has no separate
schema namespace and stores the same three things as ``CHAR(32)``, ``JSON`` and
``DATETIME``. Asserting one engine's spelling on the other would fail a schema
that is doing exactly what was asked of it, so every assertion here is on the
meaning of the column rather than on the spelling of one server.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

from app.adapters.persistence import Base
from tests.integration.conftest import (
    MANAGED_TABLES,
    MYSQL_PRIMARY_KEY_NAME,
    TIMESTAMPED_TABLES,
    constraint_names,
    floating_point_data_types,
    physical_type,
    schema_expression,
    table_names,
)

pytestmark = pytest.mark.integration

EXPECTED_COLUMNS = {
    "accounts": {
        "id",
        "external_id",
        "name",
        "currency",
        "created_at",
    },
    "contracts": {
        "id",
        "account_id",
        "external_id",
        "name",
        "status",
        "effective_from",
        "effective_to",
        "created_at",
    },
    "contract_price_terms": {
        "id",
        "contract_id",
        "metric_key",
        "billing_mode",
        "currency",
        "unit_price",
        "included_units",
        "overage_price",
        "minimum_commitment",
        "tier_schedule",
        "created_at",
    },
}

EXPECTED_CONSTRAINTS = {
    "accounts": {
        "pk_accounts",
        "uq_accounts_external_id",
        "ck_accounts_external_id_not_blank",
        "ck_accounts_name_not_blank",
        "ck_accounts_currency_is_iso4217",
    },
    "contracts": {
        "pk_contracts",
        "uq_contracts_external_id",
        "fk_contracts_account_id_accounts",
        "ck_contracts_external_id_not_blank",
        "ck_contracts_name_not_blank",
        "ck_contracts_status_is_known",
        "ck_contracts_effective_period_is_ordered",
    },
    "contract_price_terms": {
        "pk_contract_price_terms",
        "uq_contract_price_terms_contract_id_metric_key",
        "fk_contract_price_terms_contract_id_contracts",
        "ck_contract_price_terms_metric_key_not_blank",
        "ck_contract_price_terms_currency_is_iso4217",
        "ck_contract_price_terms_billing_mode_is_known",
        "ck_contract_price_terms_included_units_not_negative",
        "ck_contract_price_terms_unit_price_not_negative",
        "ck_contract_price_terms_overage_price_not_negative",
        "ck_contract_price_terms_minimum_commitment_not_negative",
        "ck_contract_price_terms_tier_schedule_matches_billing_mode",
    },
}


# ---------------------------------------------------------------------------
# upgrade / downgrade / upgrade
# ---------------------------------------------------------------------------


def test_upgrade_creates_every_table(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        assert table_names(connection) == set(MANAGED_TABLES)
    command.downgrade(alembic_config, "base")


def test_downgrade_removes_every_table(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    with engine.connect() as connection:
        assert table_names(connection) == set()


def test_the_migration_survives_a_full_round_trip(alembic_config, engine) -> None:
    """upgrade -> downgrade -> upgrade, ending in the same schema.

    A migration that only works once has usually been written against whatever
    state the database happened to be in.
    """
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        first_pass = {table: constraint_names(connection, table) for table in MANAGED_TABLES}

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    with engine.connect() as connection:
        second_pass = {table: constraint_names(connection, table) for table in MANAGED_TABLES}
        assert table_names(connection) == set(MANAGED_TABLES)

    assert first_pass == second_pass
    command.downgrade(alembic_config, "base")


def test_downgrade_leaves_the_database_usable_for_the_next_run(alembic_config, engine) -> None:
    """After a downgrade the next run starts from nothing, not from leftovers.

    A downgrade that fails part-way leaves tables behind; the upgrade that follows
    then meets tables it is not expecting. Asserting the schema is empty is only
    half of it, so the schema is also rebuilt to prove nothing was left in the way.
    """
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    with engine.connect() as connection:
        assert table_names(connection) == set()
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        assert table_names(connection) == set(MANAGED_TABLES)
    command.downgrade(alembic_config, "base")


# ---------------------------------------------------------------------------
# what the server actually built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", sorted(EXPECTED_COLUMNS))
def test_columns_match_the_models(alembic_config, engine, table_name: str) -> None:
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    with engine.connect() as connection:
        actual = {
            row[0]
            for row in connection.execute(
                text(
                    f"""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = {schema_expression(dialect)} AND table_name = :name
                    """
                ),
                {"name": table_name},
            )
        }
    assert actual == EXPECTED_COLUMNS[table_name]
    command.downgrade(alembic_config, "base")


def expected_for(connection: object, table_name: str) -> set[str]:
    """Return the declared constraint names for ``table_name`` on this engine.

    Identical to :data:`EXPECTED_CONSTRAINTS` except on MySQL, which reserves the
    name ``PRIMARY`` for a table's primary key and discards the ``pk_<table>`` name
    the migration supplies. Every other name must still match exactly, so the
    double-prefixing and truncation this test guards against stay covered.
    """
    names = set(EXPECTED_CONSTRAINTS[table_name])
    if connection.engine.dialect.name == "mysql":  # type: ignore[attr-defined]
        names.discard(f"pk_{table_name}")
        names.add(MYSQL_PRIMARY_KEY_NAME)
    return names


@pytest.mark.parametrize("table_name", sorted(EXPECTED_CONSTRAINTS))
def test_constraint_names_are_exactly_as_declared(alembic_config, engine, table_name: str) -> None:
    """Named constraints can be asserted on, which is the point of naming them.

    Also catches the naming-convention trap: a check constraint written into a
    migration with its already-prefixed name comes out double-prefixed, and the
    longest names come back truncated with a hash suffix.
    """
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        assert constraint_names(connection, table_name) == expected_for(connection, table_name)
    command.downgrade(alembic_config, "base")


#: The columns NEP-01 fixes at NUMERIC(19,4): three money columns and the
#: quantity that is compared against them.
EXACT_NUMERIC_COLUMNS = {
    "unit_price",
    "overage_price",
    "minimum_commitment",
    "included_units",
}


def test_money_columns_are_numeric_19_4_in_the_database(alembic_config, engine) -> None:
    """NEP-01 as the server stores it, not merely as the model declares it."""
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT column_name, numeric_precision, numeric_scale, data_type
                FROM information_schema.columns
                WHERE table_schema = {schema_expression(dialect)}
                  AND table_name = 'contract_price_terms'
                  AND data_type = :decimal_type
                """
            ),
            {"decimal_type": physical_type(dialect, "decimal")},
        ).all()
    # Filtering on the engine's decimal type is what proves each of these four is
    # stored exactly; the set proves none of them was missed.
    found = {name for name, *_ in rows}
    assert found == EXACT_NUMERIC_COLUMNS, found
    for name, precision, scale, _data_type in rows:
        assert (precision, scale) == (19, 4), name
    command.downgrade(alembic_config, "base")


def test_no_column_is_a_floating_point_type(alembic_config, engine) -> None:
    """NEP-01: money must not sit in a column that cannot hold it exactly."""
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    forbidden = floating_point_data_types(dialect)
    placeholders = ", ".join(f":t{n}" for n in range(len(forbidden)))
    with engine.connect() as connection:
        floating = connection.execute(
            text(
                f"""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = {schema_expression(dialect)}
                  AND data_type IN ({placeholders})
                """
            ),
            {f"t{n}": value for n, value in enumerate(forbidden)},
        ).all()
    assert floating == []
    command.downgrade(alembic_config, "base")


def test_timestamps_are_stored_with_a_time_zone(alembic_config, engine) -> None:
    """Every timestamp column carries a zone, spelled as this engine spells one.

    PostgreSQL stores ``timestamp with time zone``. MySQL has no such type and
    stores ``datetime``, which is a wall clock; the connection pins the MySQL
    session to UTC so that wall clock is UTC by construction -- see
    ``create_engine_for_url``. What is asserted on both engines is that every
    table which declares a timestamp has one, and that no table which does not
    declare one has quietly gained one.
    """
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    f"""
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = {schema_expression(dialect)}
                  AND column_name = 'created_at'
                  AND data_type = :instant_type
                """
                ),
                {"instant_type": physical_type(dialect, "instant")},
            )
            .scalars()
            .all()
        )
    assert sorted(rows) == sorted(TIMESTAMPED_TABLES)
    command.downgrade(alembic_config, "base")


def test_the_tier_ladder_is_jsonb(alembic_config, engine) -> None:
    """A ladder is a JSON document held in the engine's own JSON type.

    PostgreSQL has ``jsonb``, MySQL has ``json``. Neither is text and neither is a
    child table, which is the property under test: a ladder is read and written as
    a whole document and is never queried across contracts, so the relational form
    would add ordering and integrity machinery for no query it would serve.
    """
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    with engine.connect() as connection:
        data_type = connection.execute(
            text(
                f"""
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = {schema_expression(dialect)}
                  AND table_name = 'contract_price_terms'
                  AND column_name = 'tier_schedule'
                """
            )
        ).scalar_one()
    assert data_type == physical_type(dialect, "json")
    command.downgrade(alembic_config, "base")


def test_identifiers_use_the_native_uuid_type(alembic_config, engine) -> None:
    """Every primary key is the engine's native fixed-width identifier type.

    PostgreSQL has ``uuid``. MySQL does not, and stores the same value as
    ``CHAR(32)``: fixed-width, exactly the encoding of a uuid, and never a
    variable-width or lossy fallback. The width is asserted on MySQL for the same
    reason the type is -- a ``CHAR(1)`` would satisfy a type check and break every
    join.
    """
    command.upgrade(alembic_config, "head")
    dialect = engine.dialect.name
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT table_name, data_type, character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = {schema_expression(dialect)} AND column_name = 'id'
                  AND table_name <> 'alembic_version'
                """
            )
        ).all()
    assert rows, "expected at least one id column"
    expected_type = physical_type(dialect, "uuid")
    for table_name, data_type, length in rows:
        assert data_type == expected_type, table_name
        if dialect == "mysql":
            assert length == 32, table_name
    command.downgrade(alembic_config, "base")


# ---------------------------------------------------------------------------
# the models and the migration have not drifted apart
# ---------------------------------------------------------------------------


def test_the_database_matches_the_models_with_no_pending_changes(alembic_config, engine) -> None:
    """`alembic check`: autogenerate finds nothing to do.

    This is the test that catches a migration edited by hand without the models,
    or a model changed without a migration. It is the single most valuable check in
    this file.
    """
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    command.downgrade(alembic_config, "base")


def test_metadata_declares_exactly_the_tables_that_were_created(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        created = table_names(connection)
    assert created == set(Base.metadata.tables)
    command.downgrade(alembic_config, "base")
