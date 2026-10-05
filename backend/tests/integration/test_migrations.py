"""The migration is applied, reversed and applied again against real PostgreSQL.

These tests are the reason the migration exists in a form that can be trusted. A
hand-written migration that has only ever been read is not a migration; this file
executes it, inspects what PostgreSQL actually built, reverses it, and executes it
again.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

from app.adapters.persistence import Base
from tests.integration.conftest import (
    MANAGED_TABLES,
    MYSQL_PRIMARY_KEY_NAME,
    constraint_names,
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
    """After a downgrade the schema is genuinely empty, not merely unversioned."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# what PostgreSQL actually built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", sorted(EXPECTED_COLUMNS))
def test_columns_match_the_models(alembic_config, engine, table_name: str) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        actual = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :name
                    """
                ),
                {"name": table_name},
            )
        }
    assert actual == EXPECTED_COLUMNS[table_name]
    command.downgrade(alembic_config, "base")


@pytest.mark.parametrize("table_name", sorted(EXPECTED_CONSTRAINTS))
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


def test_money_columns_are_numeric_19_4_in_the_database(alembic_config, engine) -> None:
    """NEP-01 as PostgreSQL stores it, not merely as the model declares it."""
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT column_name, numeric_precision, numeric_scale, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'contract_price_terms'
                  AND data_type = 'numeric'
                """
            )
        ).all()
    assert rows, "expected numeric columns on contract_price_terms"
    for name, precision, scale, data_type in rows:
        assert data_type == "numeric", name
        assert (precision, scale) == (19, 4), name
    command.downgrade(alembic_config, "base")


def test_no_column_is_a_floating_point_type(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        floating = connection.execute(
            text(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type IN ('double precision', 'real')
                """
            )
        ).all()
    assert floating == []
    command.downgrade(alembic_config, "base")


def test_timestamps_are_stored_with_a_time_zone(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name = 'created_at'
                  AND data_type = 'timestamp with time zone'
                """
                )
            )
            .scalars()
            .all()
        )
    assert sorted(rows) == sorted(MANAGED_TABLES)
    command.downgrade(alembic_config, "base")


def test_the_tier_ladder_is_jsonb(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        data_type = connection.execute(
            text(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'contract_price_terms'
                  AND column_name = 'tier_schedule'
                """
            )
        ).scalar_one()
    assert data_type == "jsonb"
    command.downgrade(alembic_config, "base")


def test_identifiers_use_the_native_uuid_type(alembic_config, engine) -> None:
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT table_name, data_type FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = 'id'
                  AND table_name <> 'alembic_version'
                """
            )
        ).all()
    assert rows and all(data_type == "uuid" for _, data_type in rows)
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
