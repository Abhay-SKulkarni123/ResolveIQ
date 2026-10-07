"""SQLAlchemy models for the commercial source data.

Schema
------
Three tables, matching the entity table in docs/SYSTEM_DESIGN.md §3.2:

``accounts``
    The billing customer and the currency its invoices are settled in.
``contracts``
    An effective-dated commercial agreement belonging to one account.
``contract_price_terms``
    One row per metered metric per contract: the pricing rule itself.

These are the source-system records. The tables that describe a *dispute*
(invoices, line items, usage events, investigations, approvals) are not part of
this phase and are deliberately absent rather than stubbed.

Design rules applied here
-------------------------
**Money is ``NUMERIC(19,4)`` and never floating point** (NEP-01, ADR-011). Every
monetary column is a ``Numeric`` with explicit precision and scale, and
``tests/unit/test_persistence_models.py`` fails if a float type ever reaches this
module. Quantities use the same type so a fractional unit such as 0.5 GB is
representable without a float.

**Money columns are ``Decimal``, not :class:`~app.domain.money.Money`.** ``Money``
is the domain value object and is what the pricing layer computes with. Mapping a
value object straight onto a column would couple the storage format to the domain
type and would drag domain validation into every load path. The conversion
belongs in the repository, which is where the two representations meet.

**Currency is stored explicitly wherever money is stored.** ``accounts.currency``
is the settlement currency; ``contract_price_terms.currency`` is the currency of
the amounts on that row. Both are required, so no amount is ever interpreted
without one. There is no FX conversion in ResolveIQ (OQ-04), so in practice the
term currency equals the account currency; that equality is a domain rule and is
not enforced by a cross-table constraint, because a database-level check would
require a trigger and would put billing logic in the storage layer.

**Currency codes are ``CHAR(3)``-shaped strings with a format check**, not a
foreign key to a currency table. The set of ISO 4217 codes changes over time and
ResolveIQ must be able to store a code it has not seen before rather than reject
it at the boundary.

**Deletes are ``RESTRICT``.** Every foreign key names its delete behaviour
explicitly, even where it matches the SQL default, because in a billing system a
cascading delete destroys the financial history that a dispute is about. Deleting
an account that still has contracts is an error the caller must handle, not a
silent row loss.

**Check constraints are named.** They are the last line of defence against
invalid data arriving from an ingest script or a manual fix, and an unnamed check
cannot be asserted on by name or altered without recreating it.

Assumptions requiring confirmation
----------------------------------
Two values sets and one shape rule are not fully determined by the design
documents and are called out in docs/SYSTEM_DESIGN.md §3.2:

1. The members of ``ContractStatus`` and ``BillingMode``
   (:mod:`app.domain.contracts`).
2. ``tier_schedule`` is required exactly when ``billing_mode`` is ``TIERED``.
   Tiered pricing with no ladder cannot be computed, so the check exists; the
   remaining shape of the JSON document is validated by the domain, not by the
   database, so that the tier semantics live in one place.
3. ``external_id`` is unique on its own rather than per parent. Source systems
   generally mint contract identifiers globally, but this is not guaranteed by
   any document in the repository.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.adapters.persistence.base import Base, IngestedAtMixin, UuidPrimaryKeyMixin
from app.adapters.persistence.ddl import (
    is_iso4217_currency,
)
from app.domain.contracts import BILLING_MODE_VALUES, CONTRACT_STATUS_VALUES

__all__ = ["Account", "Contract", "ContractPriceTerm"]

#: ADR-011: 19 total digits with 4 decimal places. Enough for 15 integer digits,
#: which is 10^15 in the smallest unit, and it keeps sub-cent unit prices such as
#: 0.0007 per API call representable.
MONEY_PRECISION = 19
MONEY_SCALE = 4

#: Quantities share the money type. They are not money, but they are metered and
#: a fractional unit (0.5 GB of egress) must be storable exactly.
QUANTITY_PRECISION = 19
QUANTITY_SCALE = 4

_MAX_EXTERNAL_ID_LENGTH = 128
_MAX_NAME_LENGTH = 255
_MAX_METRIC_KEY_LENGTH = 64
_MAX_STATE_LENGTH = 32

# SQL fragments shared by the check constraints below.
_CURRENCY_IS_ISO4217 = is_iso4217_currency("currency")


def _in_list(column: str, allowed: tuple[str, ...]) -> str:
    """Render an ``IN`` list for a check constraint.

    The values come from the domain enums rather than from a literal written out
    here, so adding a member to an enum updates the constraint with it. The
    literals are still frozen into the generated migration: an old migration must
    keep describing the schema as it was when it was written, not as the code
    happens to look today.
    """
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class Account(Base, UuidPrimaryKeyMixin, IngestedAtMixin):
    """A billing customer and the currency its invoices are settled in."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("TRIM(external_id) <> ''", name="external_id_not_blank"),
        CheckConstraint("TRIM(name) <> ''", name="name_not_blank"),
        CheckConstraint(_CURRENCY_IS_ISO4217, name="currency_is_iso4217"),
    )

    #: Identifier assigned by the source billing system. Unique across accounts.
    external_id: Mapped[str] = mapped_column(
        String(_MAX_EXTERNAL_ID_LENGTH), nullable=False, unique=True
    )

    #: Display name of the customer.
    name: Mapped[str] = mapped_column(String(_MAX_NAME_LENGTH), nullable=False)

    #: ISO 4217 settlement currency, stored explicitly rather than defaulted.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    contracts: Mapped[list[Contract]] = relationship(
        back_populates="account",
        # Contracts are few per account and always needed together, so a
        # collection load is cheap. A repository that queries contracts directly
        # is unaffected.
        lazy="selectin",
        order_by="Contract.effective_from",
    )


class Contract(Base, UuidPrimaryKeyMixin, IngestedAtMixin):
    """An effective-dated commercial agreement belonging to one account."""

    __tablename__ = "contracts"
    __table_args__ = (
        CheckConstraint("TRIM(external_id) <> ''", name="external_id_not_blank"),
        CheckConstraint("TRIM(name) <> ''", name="name_not_blank"),
        CheckConstraint(_in_list("status", CONTRACT_STATUS_VALUES), name="status_is_known"),
        # An end date before the start date is never a legitimate agreement.
        # NULL end means the contract is open-ended, which the comparison
        # naturally allows because a NULL operand yields UNKNOWN, not false.
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_period_is_ordered",
        ),
        # Needed because no unique constraint starts with account_id, so the
        # foreign key has no index to reuse. PostgreSQL does not create one.
        Index("ix_contracts_account_id", "account_id"),
    )

    #: Owning customer. RESTRICT: an account with contracts is not deletable.
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Identifier assigned by the source billing system. Unique across contracts.
    external_id: Mapped[str] = mapped_column(
        String(_MAX_EXTERNAL_ID_LENGTH), nullable=False, unique=True
    )

    #: Human-readable contract name or reference.
    name: Mapped[str] = mapped_column(String(_MAX_NAME_LENGTH), nullable=False)

    #: Lifecycle state; see :class:`app.domain.contracts.ContractStatus`.
    status: Mapped[str] = mapped_column(
        String(_MAX_STATE_LENGTH),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
    )

    #: First day the terms apply. A date, not an instant: contracts take effect
    #: on calendar days, whereas usage events are point-in-time.
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    #: Last day the terms apply, or NULL while the contract is open-ended.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    account: Mapped[Account] = relationship(back_populates="contracts")
    price_terms: Mapped[list[ContractPriceTerm]] = relationship(
        back_populates="contract",
        lazy="selectin",
        order_by="ContractPriceTerm.metric_key",
    )


class ContractPriceTerm(Base, UuidPrimaryKeyMixin, IngestedAtMixin):
    """The pricing rule for one metered metric on one contract.

    One row per metric per contract. A metric is priced at most once, which the
    ``(contract_id, metric_key)`` unique constraint enforces: without it a second
    row for ``api_calls`` would make the price of every call ambiguous, and the
    ambiguity would surface as a wrong invoice rather than as an error.
    """

    __tablename__ = "contract_price_terms"
    __table_args__ = (
        # "One row per metered metric" (docs/SYSTEM_DESIGN.md §3.2).
        UniqueConstraint("contract_id", "metric_key"),
        CheckConstraint("TRIM(metric_key) <> ''", name="metric_key_not_blank"),
        CheckConstraint(_CURRENCY_IS_ISO4217, name="currency_is_iso4217"),
        CheckConstraint(
            _in_list("billing_mode", BILLING_MODE_VALUES), name="billing_mode_is_known"
        ),
        CheckConstraint("included_units >= 0", name="included_units_not_negative"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="unit_price_not_negative"),
        CheckConstraint(
            "overage_price IS NULL OR overage_price >= 0", name="overage_price_not_negative"
        ),
        CheckConstraint(
            "minimum_commitment IS NULL OR minimum_commitment >= 0",
            name="minimum_commitment_not_negative",
        ),
        # A tier ladder exists exactly when the mode is TIERED. Tiered pricing
        # with no ladder cannot be calculated at all, and a ladder attached to a
        # non-tiered row would be a rule nothing reads. The JSON document's
        # internal shape is left to the domain: putting it in a CHECK would
        # duplicate the tier semantics in SQL and in Python, and the two would
        # eventually disagree.
        CheckConstraint(
            "(billing_mode = 'TIERED') = (tier_schedule IS NOT NULL)",
            name="tier_schedule_matches_billing_mode",
        ),
    )

    #: Owning contract. RESTRICT: a contract with price terms is not deletable.
    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contracts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: The metered quantity this row prices, such as ``api_calls`` or
    #: ``egress_gb``. An opaque key: ResolveIQ does not define a metric catalogue,
    #: and usage events are matched to it by name.
    metric_key: Mapped[str] = mapped_column(String(_MAX_METRIC_KEY_LENGTH), nullable=False)

    #: How the quantity is charged; see :class:`app.domain.contracts.BillingMode`.
    billing_mode: Mapped[str] = mapped_column(String(_MAX_STATE_LENGTH), nullable=False)

    #: ISO 4217 currency of the three amounts below, required so no amount on this
    #: row can be read without knowing what it is denominated in.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    #: Flat price per unit. NULL when the mode does not use one, as a TIERED term
    #: carries its prices inside ``tier_schedule`` instead.
    unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE),
        nullable=True,
    )

    #: Units included in the price before overage applies. Zero by default, which
    #: means every unit is charged.
    included_units: Mapped[Decimal] = mapped_column(
        Numeric(precision=QUANTITY_PRECISION, scale=QUANTITY_SCALE),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0.0000"),
    )

    #: Price applied to units beyond ``included_units``.
    overage_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE),
        nullable=True,
    )

    #: Amount payable whether or not the metric was used.
    minimum_commitment: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE),
        nullable=True,
    )

    #: Progressive ladder of thresholds for TIERED terms. JSONB rather than a
    #: child table because a ladder is always read and written as a whole
    #: document and is never queried across contracts; the relational form would
    #: add ordering and integrity machinery for no query it would serve.
    tier_schedule: Mapped[dict[str, Any] | None] = mapped_column(
        # ``none_as_null``: "no ladder" has to be SQL NULL, because the check
        # constraint above asks exactly that question -- ``tier_schedule IS NOT
        # NULL``. With SQLAlchemy's default, the Python value ``None`` is written
        # as the JSON literal ``null``, which is a value rather than an absence:
        # a term built with ``tier_schedule=None`` would satisfy ``IS NOT NULL``
        # and be rejected as a non-tiered row carrying a ladder. None has to mean
        # the same thing whether it is passed explicitly or left unset.
        JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    )

    contract: Mapped[Contract] = relationship(back_populates="price_terms")
