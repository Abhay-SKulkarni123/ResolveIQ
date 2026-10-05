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
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
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


def commit_expecting_failure(session: Session) -> IntegrityError:
    """Commit the pending rows and return the error the constraint violation must raise.

    Rollback first so the session is usable again: PostgreSQL aborts the whole
    transaction on a constraint violation, and every later statement in it would
    fail with "current transaction is aborted".

    MySQL classifies a CHECK violation (3819) as ``OperationalError`` rather than
    ``IntegrityError``, so both are accepted there. That is an engine difference in
    how the error is labelled, not a difference in enforcement: the statement still
    fails and the row is still rejected, which is what these tests assert.
    """
    expected: tuple[type[Exception], ...] = (IntegrityError,)
    if session.get_bind().dialect.name == "mysql":
        expected = (IntegrityError, OperationalError)
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


def test_created_at_comes_back_timezone_aware(db_session: Session, account: Account) -> None:
    account_id = account.id
    db_session.expunge_all()
    stored = db_session.get(Account, account_id)
    assert stored is not None
    assert stored.created_at.tzinfo is not None


def test_generated_identifiers_are_distinct(db_session: Session, account: Account) -> None:
    """The Python-side uuid4 default reaches the database as a real primary key."""
    ids = {
        Account(external_id=f"ACC-GEN-{n}", name="Generated", currency="USD").id for n in range(5)
    }
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
    for _ in range(2):
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
    db_session: Session, account: Account
) -> None:
    """RESTRICT, not CASCADE: financial history outlives convenience."""
    db_session.delete(account)
    error = commit_expecting_failure(db_session)
    assert "fk_contracts_account_id_accounts" in str(error.orig)


def test_deleting_a_contract_with_price_terms_is_refused(
    db_session: Session, contract: Contract
) -> None:
    db_session.delete(contract)
    error = commit_expecting_failure(db_session)
    assert "fk_contract_price_terms_contract_id_contracts" in str(error.orig)


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "US1", "DOLLAR"])
def test_a_malformed_currency_is_rejected(db_session: Session, currency: str) -> None:
    db_session.add(Account(external_id="ACC-BAD", name="Bad currency", currency=currency))
    error = commit_expecting_failure(db_session)
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
def test_a_missing_required_column_is_rejected_by_the_server(engine, column: str) -> None:
    """Raw SQL, so what is under test is the server's NOT NULL and not the ORM's.

    MySQL reports a NOT NULL violation as ``IntegrityError`` too, so the same
    assertion holds on both engines.
    """
    with pytest.raises(IntegrityError) as caught:
        _insert_account_with_null(column, engine)
    assert "null value" in str(caught.value.orig).lower()
