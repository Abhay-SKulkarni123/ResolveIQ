"""PostgreSQL itself enforces the rules the models declare.

A check constraint written in a model is only a claim until a server has rejected a
row because of it. Each test here stores a row that must fail and asserts on the
``IntegrityError``. Where PostgreSQL names the constraint the assertion is on the
name, so a test keeps working when the rule is expressed differently but still
holds.

Every rejection is rolled back, so a failed insert cannot leave an aborted
transaction behind for the next case.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.adapters.persistence import Account, Contract, ContractPriceTerm
from app.domain.contracts import BillingMode, ContractStatus

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def account(db_session: Session) -> Account:
    record = Account(external_id="ACC-1", name="Northstar Cloud", currency="USD")
    db_session.add(record)
    db_session.commit()
    return record


@pytest.fixture()
def contract(db_session: Session, account: Account) -> Contract:
    record = Contract(
        account_id=account.id,
        external_id="CTR-1",
        name="Enterprise 2026",
        status=ContractStatus.ACTIVE.value,
        effective_from=date(2026, 1, 1),
    )
    db_session.add(record)
    db_session.commit()
    return record


def commit_expecting_failure(session: Session) -> Exception:
    """Commit the pending rows and return the error the constraint violation must raise.

    Rollback first so the session is usable again: PostgreSQL aborts the whole
    transaction on a constraint violation, and every later statement in it would
    fail with "current transaction is aborted".

    Three labels are accepted because three are used for what is the same event,
    the server refusing a row:

    * ``IntegrityError`` for a violated constraint, on either engine;
    * ``OperationalError`` for MySQL's CHECK violations (error 3819), which MySQL
      classifies as an operational problem rather than an integrity one;
    * ``DataError`` for a value that cannot be stored at all, such as one longer
      than the column it is going into (error 1406 on MySQL, 54000 on
      PostgreSQL).

    None of that changes what is asserted: the statement failed, the server
    rejected the row, and no application code was involved in the decision.
    """
    expected: tuple[type[Exception], ...] = (IntegrityError, DataError)
    if session.get_bind().dialect.name == "mysql":
        expected = (IntegrityError, OperationalError, DataError)
    with pytest.raises(expected) as caught:
        session.commit()
    session.rollback()
    return caught.value


# ---------------------------------------------------------------------------
# Round trip: the schema works for ordinary data
# ---------------------------------------------------------------------------


def test_a_full_account_contract_term_graph_survives_a_round_trip(db_session: Session) -> None:
    """The ordinary case, proving the fixture graph is real data and not just valid DDL."""
    account = Account(external_id="ACC-9", name="Helios Systems", currency="EUR")
    contract = Contract(
        account=account,
        external_id="CTR-9",
        name="Platform licence",
        status=ContractStatus.ACTIVE.value,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )
    term = ContractPriceTerm(
        contract=contract,
        metric_key="api_calls",
        billing_mode=BillingMode.PER_UNIT.value,
        currency="EUR",
        unit_price=Decimal("0.0007"),
        included_units=Decimal("10000.0000"),
        overage_price=Decimal("0.0005"),
    )
    db_session.add(term)
    db_session.commit()
    contract_id = contract.id

    db_session.expunge_all()
    stored = db_session.get(Contract, contract_id)
    assert stored is not None
    assert stored.account.name == "Helios Systems"
    assert stored.price_terms[0].unit_price == Decimal("0.0007")
    assert stored.price_terms[0].currency == "EUR"


def test_a_sub_cent_unit_price_is_stored_exactly(db_session: Session, contract: Contract) -> None:
    """The reason for NUMERIC(19,4) rather than NUMERIC(10,2)."""
    term = ContractPriceTerm(
        contract_id=contract.id,
        metric_key="api_calls",
        billing_mode=BillingMode.PER_UNIT.value,
        currency="USD",
        unit_price=Decimal("0.0007"),
    )
    db_session.add(term)
    db_session.commit()
    term_id = term.id

    db_session.expunge_all()
    stored = db_session.get(ContractPriceTerm, term_id)
    assert stored is not None
    assert stored.unit_price == Decimal("0.0007")


def test_defaults_are_applied_when_a_row_is_stored(db_session: Session, account: Account) -> None:
    """status DRAFT and included_units 0, where the model and the server default agree."""
    contract = Contract(
        account_id=account.id,
        external_id="CTR-DEF",
        name="Defaults",
        effective_from=date(2026, 1, 1),
    )
    db_session.add(contract)
    db_session.flush()

    term = ContractPriceTerm(
        contract_id=contract.id,
        metric_key="api_calls",
        billing_mode=BillingMode.PER_UNIT.value,
        currency="USD",
    )
    db_session.add(term)
    db_session.commit()
    contract_id, term_id = contract.id, term.id

    db_session.expunge_all()
    stored_contract = db_session.get(Contract, contract_id)
    stored_term = db_session.get(ContractPriceTerm, term_id)
    assert stored_contract is not None and stored_contract.status == "DRAFT"
    assert stored_term is not None and stored_term.included_units == Decimal("0")


def test_a_tier_ladder_round_trips_as_json(db_session: Session, contract: Contract) -> None:
    ladder = {"tiers": [{"up_to": 10_000, "unit_price": "0.0010"}, {"unit_price": "0.0007"}]}
    term = ContractPriceTerm(
        contract_id=contract.id,
        metric_key="egress_gb",
        billing_mode=BillingMode.TIERED.value,
        currency="USD",
        tier_schedule=ladder,
    )
    db_session.add(term)
    db_session.commit()
    term_id = term.id

    db_session.expunge_all()
    stored = db_session.get(ContractPriceTerm, term_id)
    assert stored is not None and stored.tier_schedule == ladder


def _as_instant(value: datetime, dialect: str) -> datetime:
    """Read a stored timestamp as an instant in UTC, on either engine.

    PostgreSQL hands back a zone-aware datetime. MySQL hands back a wall clock
    with no zone, which is UTC only because the engine pins the session to UTC
    (see ``create_engine_for_url``); the zone is attached here rather than
    assumed, so what the callers compare is a real instant either way.
    """
    if value.tzinfo is None:
        assert dialect == "mysql", value
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_created_at_comes_back_timezone_aware(db_session: Session, account: Account) -> None:
    """``created_at`` reads back as an instant, whichever engine stored it.

    PostgreSQL declares ``timestamptz`` and the driver hands back a zone-aware
    datetime, so on that engine ``tzinfo`` is asserted directly.

    MySQL has no timestamp type that carries a zone: ``DATETIME`` is a wall
    clock and the driver returns it naive. A zone cannot be asked for after the
    fact, so it has to be guaranteed by the schema, and it is -- the engine pins
    every MySQL session to UTC, which makes the wall clock in the column UTC
    whether the row was written by the application in UTC or by ``DEFAULT
    now()``. Before that pin, ``now()`` returned the host's local time while the
    application wrote UTC, and the two disagreed by hours while both looking
    perfectly plausible.

    So MySQL is asserted on the property rather than on PostgreSQL's spelling of
    it: an instant written as UTC is read back as that same instant, and a row
    the schema defaulted itself agrees with the UTC clock. ``tzinfo`` is asserted
    only where the driver can actually supply it.
    """
    dialect = db_session.get_bind().dialect.name
    defaulted_id = account.id

    written = datetime(2026, 3, 15, 12, 34, 56, tzinfo=timezone.utc)
    db_session.add(Account(external_id="ACC-TZ", name="Zoned", currency="USD", created_at=written))
    db_session.commit()
    db_session.expunge_all()

    app_written = next(
        row for row in db_session.scalars(select(Account)).all() if row.external_id == "ACC-TZ"
    )
    assert _as_instant(app_written.created_at, dialect) == written
    if dialect == "postgresql":
        assert app_written.created_at.tzinfo is not None

    schema_written = db_session.get(Account, defaulted_id)
    assert schema_written is not None
    observed = _as_instant(schema_written.created_at, dialect)
    assert abs((datetime.now(timezone.utc) - observed).total_seconds()) < 60


def test_generated_identifiers_are_distinct(db_session: Session, account: Account) -> None:
    """The Python-side uuid4 default reaches the database as a real primary key.

    The rows are flushed rather than merely constructed: a column default is
    applied when the INSERT is built, so objects that have never been flushed
    carry no identifier at all and would all compare equal to ``None``.
    """
    records = [
        Account(external_id=f"ACC-GEN-{n}", name="Generated", currency="USD") for n in range(5)
    ]
    db_session.add_all(records)
    db_session.flush()
    ids = {record.id for record in records}
    assert len(ids) == 5
    assert account.id not in ids


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


def test_two_accounts_cannot_share_an_external_id(db_session: Session, account: Account) -> None:
    db_session.add(Account(external_id="ACC-1", name="Impostor", currency="USD"))
    error = commit_expecting_failure(db_session)
    assert "uq_accounts_external_id" in str(error.orig)


def test_two_contracts_cannot_share_an_external_id(db_session: Session, contract: Contract) -> None:
    db_session.add(
        Contract(
            account_id=contract.account_id,
            external_id="CTR-1",
            name="Clash",
            effective_from=date(2026, 1, 1),
        )
    )
    error = commit_expecting_failure(db_session)
    assert "uq_contracts_external_id" in str(error.orig)


def test_a_metric_cannot_be_priced_twice_on_one_contract(
    db_session: Session, contract: Contract
) -> None:
    """Both rows are pending before either is written, so the index decides.

    Flushing each row as it is added would raise from the flush instead, which
    tests the order of the inserts rather than the uniqueness rule.
    """
    db_session.add_all(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
            unit_price=Decimal("0.001"),
        )
        for _ in range(2)
    )
    error = commit_expecting_failure(db_session)
    assert "uq_contract_price_terms_contract_id_metric_key" in str(error.orig)


def test_the_same_metric_may_be_priced_on_two_different_contracts(
    db_session: Session, contract: Contract
) -> None:
    """Uniqueness is per contract, not global: renegotiated terms must remain possible."""
    other = Contract(
        account_id=contract.account_id,
        external_id="CTR-2",
        name="Successor",
        status=ContractStatus.SUPERSEDED.value,
        effective_from=date(2027, 1, 1),
    )
    db_session.add(other)
    db_session.flush()
    for target in (contract, other):
        db_session.add(
            ContractPriceTerm(
                contract_id=target.id,
                metric_key="api_calls",
                billing_mode=BillingMode.PER_UNIT.value,
                currency="USD",
                unit_price=Decimal("0.001"),
            )
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# Foreign keys and delete behaviour
# ---------------------------------------------------------------------------


def test_a_term_cannot_reference_a_missing_contract(db_session: Session) -> None:
    db_session.add(
        ContractPriceTerm(
            contract_id=uuid.uuid4(),
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
        )
    )
    error = commit_expecting_failure(db_session)
    assert "fk_contract_price_terms_contract_id_contracts" in str(error.orig)


def test_a_contract_cannot_reference_a_missing_account(db_session: Session) -> None:
    db_session.add(
        Contract(
            account_id=uuid.uuid4(),
            external_id="CTR-ORPHAN",
            name="Orphan",
            effective_from=date(2026, 1, 1),
        )
    )
    error = commit_expecting_failure(db_session)
    assert "fk_contracts_account_id_accounts" in str(error.orig)


def test_deleting_an_account_with_contracts_is_refused(
    db_session: Session, account: Account, contract: Contract
) -> None:
    """RESTRICT, not CASCADE: financial history outlives convenience.

    The ``contract`` fixture is what makes this test exist: with no contract
    hanging off the account the delete simply succeeds.

    The delete is issued as SQL rather than through ``session.delete``. On seeing
    a parent go, SQLAlchemy de-associates the children it has loaded by setting
    their foreign key to NULL, so the server is never asked whether the row may
    go and the failure comes from the NOT NULL column instead of from the
    constraint under test. Issuing the DELETE directly makes the answer the
    server's, the same way the NOT NULL cases below are made the server's.
    """
    assert contract.account_id == account.id
    with pytest.raises(IntegrityError) as caught:
        db_session.execute(delete(Account).where(Account.id == account.id))
    db_session.rollback()
    assert "fk_contracts_account_id_accounts" in str(caught.value.orig)


def test_deleting_a_contract_with_price_terms_is_refused(
    db_session: Session, contract: Contract
) -> None:
    """The same rule from the other side, and for the same reason."""
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
            unit_price=Decimal("0.001"),
        )
    )
    db_session.flush()
    with pytest.raises(IntegrityError) as caught:
        db_session.execute(delete(Contract).where(Contract.id == contract.id))
    db_session.rollback()
    assert "fk_contract_price_terms_contract_id_contracts" in str(caught.value.orig)


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "US1", "DOLLAR"])
def test_a_malformed_currency_is_rejected(db_session: Session, currency: str) -> None:
    """The server refuses a currency that is not an ISO-4217 code.

    ``currency`` is declared ``VARCHAR(3)``, so a value longer than three
    characters is stopped by the column's length guard before the ISO-4217 check
    constraint is evaluated at all. That is still the server -- never the ORM --
    rejecting the row, so every case here is asserted as a rejection; only the
    in-width values can be attributed to the check constraint by name, and
    pretending otherwise would be asserting something the database never said.
    """
    db_session.add(Account(external_id="ACC-BAD", name="Bad currency", currency=currency))
    error = commit_expecting_failure(db_session)
    if len(currency) > 3:
        assert isinstance(error, DataError), currency
    else:
        assert "ck_accounts_currency_is_iso4217" in str(error.orig)


@pytest.mark.parametrize("field", ["external_id", "name"])
def test_a_blank_required_string_is_rejected(db_session: Session, field: str) -> None:
    values = {"external_id": "ACC-BLANK", "name": "Blank", "currency": "USD", field: "   "}
    db_session.add(Account(**values))
    error = commit_expecting_failure(db_session)
    assert f"ck_accounts_{field}_not_blank" in str(error.orig)


def test_an_unknown_contract_status_is_rejected(db_session: Session, account: Account) -> None:
    db_session.add(
        Contract(
            account_id=account.id,
            external_id="CTR-BAD",
            name="Bad status",
            status="PENDING_REVIEW",
            effective_from=date(2026, 1, 1),
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contracts_status_is_known" in str(error.orig)


@pytest.mark.parametrize("status", [status.value for status in ContractStatus])
def test_every_declared_status_is_accepted(
    db_session: Session, account: Account, status: str
) -> None:
    """The check must not be narrower than the enum it claims to mirror."""
    db_session.add(
        Contract(
            account_id=account.id,
            external_id=f"CTR-{status}",
            name=status,
            status=status,
            effective_from=date(2026, 1, 1),
        )
    )
    db_session.commit()


def test_a_contract_may_not_end_before_it_starts(db_session: Session, account: Account) -> None:
    db_session.add(
        Contract(
            account_id=account.id,
            external_id="CTR-BACK",
            name="Backwards",
            effective_from=date(2026, 6, 1),
            effective_to=date(2026, 1, 1),
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contracts_effective_period_is_ordered" in str(error.orig)


@pytest.mark.parametrize(
    ("effective_from", "effective_to"),
    [
        (date(2026, 6, 1), date(2026, 6, 1)),
        (date(2026, 6, 1), date(2026, 7, 1)),
        (date(2026, 6, 1), None),
    ],
    ids=["single-day", "a-period", "open-ended"],
)
def test_a_legitimate_effective_period_is_accepted(
    db_session: Session, account: Account, effective_from: date, effective_to: date | None
) -> None:
    """The check is >= , so a one-day contract and an open-ended one both stand."""
    db_session.add(
        Contract(
            account_id=account.id,
            external_id=f"CTR-OK-{effective_from}-{effective_to}",
            name="Fine",
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    db_session.commit()


@pytest.mark.parametrize("field", ["unit_price", "overage_price", "minimum_commitment"])
def test_a_negative_amount_is_rejected(db_session: Session, contract: Contract, field: str) -> None:
    values = {
        "contract_id": contract.id,
        "metric_key": "api_calls",
        "billing_mode": BillingMode.PER_UNIT.value,
        "currency": "USD",
        field: Decimal("-0.0001"),
    }
    db_session.add(ContractPriceTerm(**values))
    error = commit_expecting_failure(db_session)
    assert f"ck_contract_price_terms_{field}_not_negative" in str(error.orig)


def test_a_negative_quantity_is_rejected(db_session: Session, contract: Contract) -> None:
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
            included_units=Decimal("-1"),
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contract_price_terms_included_units_not_negative" in str(error.orig)


def test_a_zero_amount_is_allowed(db_session: Session, contract: Contract) -> None:
    """The checks are >= 0, not > 0: a free tier legitimately prices at zero."""
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="support_calls",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
            unit_price=Decimal("0"),
            overage_price=Decimal("0"),
            minimum_commitment=Decimal("0"),
        )
    )
    db_session.commit()


def test_tiered_pricing_without_a_ladder_is_rejected(
    db_session: Session, contract: Contract
) -> None:
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="egress_gb",
            billing_mode=BillingMode.TIERED.value,
            currency="USD",
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contract_price_terms_tier_schedule_matches_billing_mode" in str(error.orig)


def test_a_ladder_on_a_non_tiered_term_is_rejected(db_session: Session, contract: Contract) -> None:
    """A rule nothing reads is worse than no rule: it looks like configuration."""
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="egress_gb",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
            tier_schedule={"tiers": []},
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contract_price_terms_tier_schedule_matches_billing_mode" in str(error.orig)


@pytest.mark.parametrize("mode", [mode.value for mode in BillingMode])
def test_every_declared_billing_mode_is_accepted(
    db_session: Session, contract: Contract, mode: str
) -> None:
    """TIERED is the one mode that needs a ladder, so it is given one."""
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="api_calls",
            billing_mode=mode,
            currency="USD",
            tier_schedule={"tiers": []} if mode == BillingMode.TIERED.value else None,
        )
    )
    db_session.commit()


def test_an_unknown_billing_mode_is_rejected(db_session: Session, contract: Contract) -> None:
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="api_calls",
            billing_mode="USAGE_BASED",
            currency="USD",
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contract_price_terms_billing_mode_is_known" in str(error.orig)


def test_a_blank_metric_key_is_rejected(db_session: Session, contract: Contract) -> None:
    db_session.add(
        ContractPriceTerm(
            contract_id=contract.id,
            metric_key="  ",
            billing_mode=BillingMode.PER_UNIT.value,
            currency="USD",
        )
    )
    error = commit_expecting_failure(db_session)
    assert "ck_contract_price_terms_metric_key_not_blank" in str(error.orig)


# ---------------------------------------------------------------------------
# NOT NULL, enforced by the server rather than by the ORM
# ---------------------------------------------------------------------------


def _insert_account_with_null(column: str, engine) -> None:
    """Insert an account row whose ``column`` is NULL, bypassing the ORM entirely.

    The column list is built from a literal dict in this file, so interpolating
    the names is safe; the values are always bound parameters.
    """
    values = {"external_id": "ACC-NN", "name": "Not null", "currency": "USD"}
    values[column] = None
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO accounts ({columns}) VALUES ({placeholders})"), values
        )


@pytest.mark.parametrize("column", ["external_id", "name", "currency"])
def test_a_missing_required_column_is_rejected_by_the_server(
    migrated_database, column: str
) -> None:
    """Raw SQL, so what is under test is the server's NOT NULL and not the ORM's.

    The schema has to be migrated first. Against a bare engine there is no
    ``accounts`` table at all, so what is actually exercised is whether some
    earlier test happened to leave one behind -- which is not this test's
    subject, and stops being true the moment the suite is ordered differently.

    Both engines report a NOT NULL violation as ``IntegrityError``, and both
    name the offending column and the word "null" -- PostgreSQL as ``null value
    in column "name" violates not-null constraint``, MySQL as ``column 'name'
    cannot be null`` -- so those are what is asserted, rather than one engine's
    phrasing of the same rejection.
    """
    with pytest.raises(IntegrityError) as caught:
        _insert_account_with_null(column, migrated_database)
    message = str(caught.value.orig).lower()
    assert "null" in message
    assert column in message
