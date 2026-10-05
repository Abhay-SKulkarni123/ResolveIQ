"""Tests for the ORM models and the schema they declare.

These run without a database. They assert on ``Base.metadata``, which is the
schema *as declared*; whether PostgreSQL accepts it is proven separately by
``tests/integration/test_migrations.py`` and ``test_constraints.py``.

The theme is that the constraints are the specification. A ``NUMERIC(19,4)``
column or a ``RESTRICT`` delete rule is a promise about stored data, and a promise
that nothing checks is a comment.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import CheckConstraint, DateTime, Float, Numeric, UniqueConstraint, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.adapters.persistence import (
    MONEY_PRECISION,
    MONEY_SCALE,
    Account,
    Base,
    Contract,
    ContractPriceTerm,
)
from app.adapters.persistence.ddl import is_iso4217_currency
from app.domain.contracts import BILLING_MODE_VALUES, CONTRACT_STATUS_VALUES

DIALECT = postgresql.dialect()

#: Phase 1-2: the source-of-truth tables imported from the billing system.
SOURCE_TABLES = {"accounts", "contracts", "contract_price_terms"}

#: Phase 4: disputes, their immutable evidence, calculations, investigations and the
#: reviewer's annotations.
DISPUTE_TABLES = {
    "disputes",
    "dispute_evidence_items",
    "calculations",
    "investigations",
    "investigation_evidence",
    "investigation_findings",
    "investigation_hypotheses",
    "investigation_resolution_options",
    "finding_reviews",
}

EXPECTED_TABLES = SOURCE_TABLES | DISPUTE_TABLES

#: The join table between an investigation and the snapshots it read. It has a
#: composite primary key of the two foreign keys and no surrogate ``id``: the pair
#: *is* the identity, and a surrogate would allow the same link twice.
LINK_TABLES = {"investigation_evidence"}

#: Everything except the link tables, for assertions that need a surrogate key.
TABLES_WITH_ID = EXPECTED_TABLES - LINK_TABLES

MONEY_COLUMNS = ("unit_price", "overage_price", "minimum_commitment")
QUANTITY_COLUMNS = ("included_units",)


def render_create_table(table: object) -> str:
    """DDL for one table as PostgreSQL will receive it."""
    return str(CreateTable(table).compile(dialect=DIALECT))  # type: ignore[arg-type]


def unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
    """The column tuples this table declares UNIQUE over."""
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


# ---------------------------------------------------------------------------
# Shape of the schema
# ---------------------------------------------------------------------------


def test_schema_contains_exactly_the_declared_tables() -> None:
    """The whole schema, pinned.

    Kept as an exact set rather than a subset check: an accidental table -- a scratch
    table, a leftover experiment, a second spelling of one that exists -- is
    otherwise invisible until it reaches a migration and a reviewer has to notice it
    there. Phase 4 added nine tables to the three source tables.
    """
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_column_uses_a_floating_point_type() -> None:
    """NEP-01 at the storage layer.

    A float would round a unit price such as 0.0007 into a value that is not the
    one the contract says, and the error would be invisible until an invoice
    disputed it.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


@pytest.mark.parametrize("table_name", sorted(SOURCE_TABLES))
def test_money_columns_are_numeric_19_4(table_name: str) -> None:
    """ADR-011: NUMERIC(19,4) for every monetary column."""
    table = Base.metadata.tables[table_name]
    for column in table.columns:
        if column.name in MONEY_COLUMNS or column.name in QUANTITY_COLUMNS:
            assert isinstance(column.type, Numeric), f"{table_name}.{column.name}"
            assert column.type.precision == MONEY_PRECISION
            assert column.type.scale == MONEY_SCALE


def test_rendered_ddl_names_the_precision_and_no_float_type() -> None:
    ddl = "\n".join(render_create_table(t) for t in Base.metadata.sorted_tables)
    assert "NUMERIC(19, 4)" in ddl
    for forbidden in ("FLOAT", "DOUBLE PRECISION", "REAL"):
        assert forbidden not in ddl


def test_every_timestamp_column_is_timezone_aware() -> None:
    """Any timestamp, not just ``created_at``.

    A naive timestamp in a UTC-only deployment is a latent off-by-hours bug: it
    reads correctly until a daylight-saving boundary or a server in another zone
    touches it, and a dispute's ``occurred_at`` is exactly the kind of field that
    gets compared across sources. The dispute-side tables use ``captured_at`` as
    well as ``created_at``, so this checks by type rather than by name.
    """
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and column.type.timezone is not True
    ]
    assert naive == []


def test_identifiers_are_native_uuid_columns() -> None:
    # A string primary key would make a foreign key a text comparison and would
    # put the encoding choice in every join.
    for table_name in sorted(TABLES_WITH_ID):
        assert isinstance(Base.metadata.tables[table_name].columns["id"].type, Uuid), table_name


def test_link_tables_use_uuid_foreign_keys_rather_than_a_surrogate_key() -> None:
    """``investigation_evidence`` is identified by the pair it links.

    Both halves are checked because a link table with one uuid and one text column
    is the failure this catches, and it would still compare as a text join for half
    of its queries.
    """
    for column in Base.metadata.tables["investigation_evidence"].columns:
        assert isinstance(column.type, Uuid), column.name


# ---------------------------------------------------------------------------
# Required fields and defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_name", "nullable_column"),
    [
        ("accounts", "external_id"),
        ("accounts", "name"),
        ("accounts", "currency"),
        ("contracts", "account_id"),
        ("contracts", "external_id"),
        ("contracts", "name"),
        ("contracts", "status"),
        ("contracts", "effective_from"),
        ("contract_price_terms", "contract_id"),
        ("contract_price_terms", "metric_key"),
        ("contract_price_terms", "billing_mode"),
        ("contract_price_terms", "currency"),
    ],
)
def test_column_is_required(table_name: str, nullable_column: str) -> None:
    assert Base.metadata.tables[table_name].columns[nullable_column].nullable is False


def test_effective_to_is_optional_because_a_contract_may_be_open_ended() -> None:
    assert Base.metadata.tables["contracts"].columns["effective_to"].nullable is True


def test_optional_price_columns_are_nullable() -> None:
    table = Base.metadata.tables["contract_price_terms"]
    for column in MONEY_COLUMNS:
        assert table.columns[column].nullable is True, column


def test_contract_status_defaults_to_draft() -> None:
    """Both the ORM default and the server default agree.

    A column default is applied by the database on INSERT and by the ORM on
    flush; asserting on the column rather than on a freshly constructed object is
    what makes this a test of the schema. That a stored row really comes back as
    DRAFT is asserted against PostgreSQL in tests/integration.
    """
    column = Base.metadata.tables["contracts"].columns["status"]
    assert column.default is not None and column.default.arg == "DRAFT"
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'DRAFT'"


def test_included_units_defaults_to_zero() -> None:
    """No included units means every unit is charged."""
    column = Base.metadata.tables["contract_price_terms"].columns["included_units"]
    assert column.default is not None and column.default.arg == Decimal("0")
    assert column.server_default is not None
    assert str(column.server_default.arg) == "0"


def test_primary_key_is_generated_in_python_without_a_round_trip() -> None:
    """The key comes from uuid4 in the application, not from a database function.

    Asserting the factory rather than a generated value is deliberate: a transient
    object has no id until it is flushed, and a test that constructed and inserted
    a row would be an integration test wearing a unit test's name.

    SQLAlchemy wraps a zero-argument callable so it can also receive an execution
    context, and keeps the original reachable as ``__wrapped__``; that is the
    function to call here.
    """
    for table_name in sorted(TABLES_WITH_ID):
        default = Base.metadata.tables[table_name].columns["id"].default
        assert default is not None
        factory = getattr(default.arg, "__wrapped__", default.arg)
        first, second = factory(), factory()
        assert isinstance(first, uuid.UUID), table_name
        assert first != second, table_name


# ---------------------------------------------------------------------------
# Relationships and delete behaviour
# ---------------------------------------------------------------------------


def test_relationships_are_connected_in_both_directions() -> None:
    assert Account.contracts.property.mapper.class_ is Contract
    assert Contract.account.property.mapper.class_ is Account
    assert Contract.price_terms.property.mapper.class_ is ContractPriceTerm
    assert ContractPriceTerm.contract.property.mapper.class_ is Contract


def test_back_populates_keeps_both_sides_in_step() -> None:
    """Assigning the parent must populate the child, without a database.

    A one-directional relationship is the classic ORM defect: it passes a mapper
    configuration check and then silently fails to save. This needs no server
    because relationship assignment is entirely in memory.
    """
    account = Account(external_id="ACC-1", name="Northstar Cloud", currency="USD")
    contract = Contract(
        account=account, external_id="CTR-1", name="Enterprise", effective_from=date(2026, 1, 1)
    )
    ContractPriceTerm(
        contract=contract, metric_key="api_calls", billing_mode="PER_UNIT", currency="USD"
    )

    assert contract in account.contracts
    assert account in [contract.account]
    assert contract.price_terms[0].contract is contract
    assert contract.price_terms[0].contract_id == contract.id


@pytest.mark.parametrize(
    ("table_name", "column_name", "referred_table"),
    [
        ("contracts", "account_id", "accounts"),
        ("contract_price_terms", "contract_id", "contracts"),
    ],
)
def test_foreign_keys_restrict_deletes(
    table_name: str, column_name: str, referred_table: str
) -> None:
    """A cascade would delete financial history as a side effect.

    Deleting an account that still has contracts has to be a decision somebody
    makes, not something a row removal triggers.
    """
    column = Base.metadata.tables[table_name].columns[column_name]
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.column.table.name == referred_table
    assert foreign_key.ondelete == "RESTRICT"


def test_foreign_key_names_are_deterministic() -> None:
    """Every FK name is short enough to survive PostgreSQL, and stable.

    Two of these are deliberately *not* the naming convention's output. The
    convention would produce
    ``fk_investigation_resolution_options_investigation_id_investigations`` (67
    bytes) and
    ``fk_investigation_evidence_evidence_item_id_dispute_evidence_items`` (65), and
    PostgreSQL truncates anything past 63 bytes with a hash suffix -- producing a
    name that differs from the metadata and appears in no ``grep``. The two
    exceptions are asserted by name here so a future edit cannot quietly replace
    them with the over-long convention form.

    Every other name is the convention's, which is what keeps a later autogenerated
    migration a no-op instead of a spurious drop-and-recreate.
    """
    names = {
        fk.constraint.name  # type: ignore[union-attr]
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
    }
    assert names == {
        # Phase 1-2
        "fk_contracts_account_id_accounts",
        "fk_contract_price_terms_contract_id_contracts",
        # Phase 4
        "fk_disputes_account_id_accounts",
        "fk_disputes_contract_id_contracts",
        "fk_disputes_current_investigation_id_investigations",
        "fk_dispute_evidence_items_dispute_id_disputes",
        "fk_calculations_dispute_id_disputes",
        "fk_calculations_investigation_id_investigations",
        "fk_investigations_dispute_id_disputes",
        "fk_investigation_evidence_investigation_id_investigations",
        "fk_investigation_findings_investigation_id_investigations",
        "fk_investigation_hypotheses_investigation_id_investigations",
        "fk_finding_reviews_dispute_id_disputes",
        "fk_finding_reviews_investigation_id_investigations",
        # The two documented short-name exceptions (over 63 bytes if conventional).
        "fk_investigation_evidence_evidence_item_id_evidence",
        "fk_investigation_resolution_options_investigation_id",
    }


def test_foreign_key_names_fit_postgresql_identifier_limit() -> None:
    """The real reason for the two exceptions above, asserted on the whole schema.

    Without this the short names look like an inconsistency to be tidied away, and
    tidying them away silently reintroduces the truncation.
    """
    too_long = [
        fk.constraint.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if len((fk.constraint.name or "").encode("utf-8")) > 63
    ]
    assert too_long == []


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["accounts", "contracts"])
def test_source_identifier_is_unique(table_name: str) -> None:
    """Two records from the billing system must not claim the same identifier."""
    assert ("external_id",) in unique_column_sets(table_name)


def test_a_metric_is_priced_at_most_once_per_contract() -> None:
    """The (contract_id, metric_key) rule from docs/SYSTEM_DESIGN.md §3.2.

    Without it, two rows for the same metric make every charge on that metric
    ambiguous, and the ambiguity surfaces as a wrong invoice rather than an error.
    """
    assert ("contract_id", "metric_key") in unique_column_sets("contract_price_terms")


def test_contract_id_needs_no_separate_index() -> None:
    """The unique constraint's index already leads with contract_id.

    A second index on the same column would duplicate work on every write to
    buy nothing.
    """
    table = Base.metadata.tables["contract_price_terms"]
    indexed_columns = {tuple(column.name for column in index.columns) for index in table.indexes}
    assert ("contract_id",) not in indexed_columns


def test_account_id_is_indexed() -> None:
    """No unique constraint leads with account_id, so the FK needs its own index."""
    table = Base.metadata.tables["contracts"]
    indexed_columns = {tuple(column.name for column in index.columns) for index in table.indexes}
    assert ("account_id",) in indexed_columns


# ---------------------------------------------------------------------------
# Check constraints, and the domain enums they must not drift from
# ---------------------------------------------------------------------------


def check_sql_for(table_name: str) -> str:
    table = Base.metadata.tables[table_name]
    return "\n".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_contract_status_check_lists_every_enum_member() -> None:
    """The database and app.domain.contracts must agree on the legal states.

    Adding a state to the enum without widening the CHECK would produce a schema
    that accepts nothing new; widening the CHECK alone would accept values no code
    can produce.
    """
    sql = check_sql_for("contracts")
    for status in CONTRACT_STATUS_VALUES:
        assert f"'{status}'" in sql, status


def test_billing_mode_check_lists_every_enum_member() -> None:
    sql = check_sql_for("contract_price_terms")
    for mode in BILLING_MODE_VALUES:
        assert f"'{mode}'" in sql, mode


@pytest.mark.parametrize(
    ("table_name", "expected"),
    [
        ("accounts", is_iso4217_currency("currency")),
        ("contracts", "effective_to IS NULL OR effective_to >= effective_from"),
        ("contract_price_terms", "included_units >= 0"),
        ("contract_price_terms", "unit_price IS NULL OR unit_price >= 0"),
        ("contract_price_terms", "overage_price IS NULL OR overage_price >= 0"),
        ("contract_price_terms", "minimum_commitment IS NULL OR minimum_commitment >= 0"),
    ],
)
def test_check_constraint_is_present(table_name: str, expected: str) -> None:
    assert expected in check_sql_for(table_name)


def test_tier_ladder_exists_exactly_when_the_mode_is_tiered() -> None:
    """Tiered pricing with no ladder cannot be calculated.

    The ladder's internal shape is deliberately not checked here: that would put
    the tier semantics in SQL and in Python and guarantee the two would eventually
    disagree.
    """
    assert "(billing_mode = 'TIERED') = (tier_schedule IS NOT NULL)" in check_sql_for(
        "contract_price_terms"
    )


@pytest.mark.parametrize("table_name", sorted(TABLES_WITH_ID))
def test_every_check_constraint_is_named(table_name: str) -> None:
    """A nameless CHECK cannot be altered without being dropped and recreated."""
    table = Base.metadata.tables[table_name]
    unnamed = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and not constraint.name
    ]
    assert unnamed == []


@pytest.mark.parametrize("table_name", sorted(TABLES_WITH_ID))
def test_check_constraint_names_fit_postgresql_identifier_limit(table_name: str) -> None:
    """PostgreSQL truncates identifiers past 63 bytes, silently and with a hash.

    A truncated name is unique but unreadable, and it differs from the name in
    the model metadata, so it must be caught here rather than in a review.
    """
    for constraint in Base.metadata.tables[table_name].constraints:
        assert len(constraint.name.encode("utf-8")) <= 63, constraint.name
