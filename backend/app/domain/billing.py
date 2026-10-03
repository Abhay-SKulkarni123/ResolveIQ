"""Calculation inputs for the billing engine: the evidence a charge is derived from.

What this module is
-------------------
These are the *domain* shapes of the records a recalculation reads: metered usage,
a contract's pricing rule, an invoice as the customer was actually charged it, and
the payments and adjustments already applied to it. The pricing layer
(:mod:`app.pricing`) turns them into computed amounts.

Why they are not the ORM models
-------------------------------
``app/adapters/persistence/models.py`` mirrors the source system's tables, where a
monetary column is a ``Decimal`` and a quantity is a ``Decimal``. The engine needs
something stricter: amounts that are guaranteed to be :class:`~app.domain.money.Money`
and therefore carry a currency, and quantities that are guaranteed non-negative and
finite. Converting at the boundary is what lets the calculators omit validation that
has already happened, so a rule reads as arithmetic rather than as defensive code.

The same reasoning keeps this module free of SQLAlchemy. ``docs/SOLID.md`` states the
dependency rule and ``tests/unit/test_layer_boundaries.py`` enforces it by walking
imports, so importing the ORM here would be a build failure rather than a style note.

Persistence is deliberately not part of this phase. The design snapshots evidence as
immutable JSONB per dispute (docs/SYSTEM_DESIGN.md §5.2), so an investigation
recalculates from a frozen bundle rather than from live tables. See ADR-022.

Assumptions
-----------
Six shapes are not fixed by any document in the repository and are recorded as
assumptions rather than presented as requirements:

1. **The period is half-open, ``[period_start, period_end)``.** An invoice for March
   2025 therefore runs ``2025-03-01`` to ``2025-04-01``. Usage on 31 March is inside
   the period and usage on 1 April is outside it. The alternative, an inclusive end
   date, makes "the last day of the period" ambiguous by a whole day and is the
   usual source of off-by-one-period disputes.
2. **``line_type`` has two members.** ``USAGE`` is derived from metered evidence and
   can be recalculated. ``FIXED`` is a flat charge that no usage evidence can confirm
   or refute. Both are needed: an invoice total that ignored ``FIXED`` lines would not
   reconcile against the invoice, which is the comparison this engine exists to make.
3. **Adjustment sign.** A positive :class:`Adjustment` is a credit that *reduces* the
   outstanding balance; a negative one is a surcharge that increases it. One signed
   field covers both without a second "direction" enum that could contradict the sign.
4. **The tier ladder is measured on billable units**, that is, on usage after
   ``included_units`` have been subtracted. This is the reading that reproduces the
   worked example in docs/SYSTEM_DESIGN.md §6.4, where 41,234 calls less 10,000
   included leaves 31,234 units and tier 1 of 10,000-50,000 is charged
   ``min(31234, 40000) * 0.00070``.
5. **Usage timestamps are timezone-aware and are normalised to UTC.** No document states
   the billing timezone, so UTC is used and stated rather than assumed silently. A naive
   timestamp is two different instants depending on who produced it, and an event on a
   period boundary would then be billed into a different month by two systems that both
   believe they are right. Period selection compares the UTC calendar date, so an event
   at ``2025-04-01T00:00+05:30`` bills into March. Where an account bills in its own
   local timezone, the conversion belongs at the boundary that reads the event.
6. **Payment allocations are per invoice.** One remittance routinely settles several
   invoices, so :class:`PaymentAllocation` names an invoice rather than holding a single
   total. A total cannot express a $300 payment split across two invoices, and inferring
   the split would either ignore the payment or apply all of it to the wrong invoice.

Two decisions that are *not* assumptions, because §6.4 and the ``BillingMode`` values
already fix them: rounding happens once at the line boundary
(:mod:`app.pricing.rounding`), and each billing mode has exactly one rule
(:mod:`app.pricing.rules`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from enum import Enum

from app.domain.contracts import BillingMode
from app.domain.money import Money

__all__ = [
    "Adjustment",
    "BillingDataError",
    "InvoiceLine",
    "InvoicePeriod",
    "LineType",
    "Payment",
    "PaymentAllocation",
    "PriceTerm",
    "RecordedInvoice",
    "Tier",
    "UsageEvent",
]


class BillingDataError(ValueError):
    """Raised when calculation input is structurally invalid.

    Raised at construction time so that a malformed record cannot reach a calculator
    half-way through a dispute. Distinct from an *insufficient evidence* outcome:
    a price term that exists but has no rate is a data defect, whereas a metric with
    no price term at all is a gap in the evidence, and the engine reports the latter
    as an unresolved line rather than raising.
    """


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BillingDataError(f"{field_name} must be a non-blank string, got {value!r}")
    return value.strip()


def _to_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Convert a quantity to an exact ``Decimal``, rejecting floats.

    Quantities are metered values such as "0.5 GB of egress", so they are exact
    decimals for the same reason money is. Rejecting ``float`` here keeps it out of the
    calculation path: INV-01 requires that no monetary or metered value is ever a
    binary float.
    """
    if isinstance(value, (bool, float)):
        raise BillingDataError(
            f"{field_name} must be an exact Decimal, int or str; "
            f"got {type(value).__name__}. Binary floating point cannot represent "
            "metered quantities exactly."
        )
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(value)
    except DecimalException as exc:
        raise BillingDataError(f"{field_name} is not a valid decimal: {value!r}") from exc
    if not candidate.is_finite():
        raise BillingDataError(f"{field_name} must be finite, got {candidate}")
    return candidate


def _require_non_negative(value: Decimal, field_name: str) -> Decimal:
    if value < 0:
        raise BillingDataError(f"{field_name} must not be negative, got {value}")
    return value


class LineType(str, Enum):
    """What kind of charge an invoice line represents."""

    #: Priced from metered usage evidence, so it can be recalculated.
    USAGE = "USAGE"
    #: A flat charge. No usage evidence can confirm or refute it, so the engine
    #: carries it at its recorded amount rather than inventing a basis for it.
    FIXED = "FIXED"


@dataclass(frozen=True)
class UsageEvent:
    """One metered usage record.

    ``dedupe_key`` is the source system's own uniqueness token for the event
    (docs/SYSTEM_DESIGN.md §3.2). Two events sharing one are the same usage counted
    twice, which is a documented cause of a billing dispute, so the key is part of the
    domain shape rather than an implementation detail.
    """

    external_id: str
    dedupe_key: str
    metric_key: str
    occurred_at: datetime
    quantity: Decimal
    unit: str

    def __post_init__(self) -> None:
        _require_non_blank(self.external_id, "UsageEvent.external_id")
        _require_non_blank(self.dedupe_key, "UsageEvent.dedupe_key")
        _require_non_blank(self.metric_key, "UsageEvent.metric_key")
        _require_non_blank(self.unit, "UsageEvent.unit")
        if not isinstance(self.occurred_at, datetime):
            raise BillingDataError(
                f"UsageEvent.occurred_at must be a datetime, got {type(self.occurred_at).__name__}"
            )
        if self.occurred_at.utcoffset() is None:
            # Refused rather than assumed to be UTC. A naive timestamp is two different
            # instants depending on who produced it, and an event on a period boundary would
            # then be billed in a different month by two systems that both believed they
            # were right.
            raise BillingDataError(
                f"UsageEvent.occurred_at is {self.occurred_at.isoformat()} with no timezone; "
                "an instant that could be read as UTC or as local time cannot be placed in "
                "an invoice period unambiguously"
            )
        # Normalised to UTC on the way in, so period selection, sorting and the rendered
        # trace all speak one language and no caller has to remember to convert.
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))
        object.__setattr__(
            self,
            "quantity",
            _require_non_negative(_to_decimal(self.quantity, "quantity"), "quantity"),
        )


@dataclass(frozen=True)
class Tier:
    """One step of a tiered price ladder.

    ``up_to`` is the cumulative upper bound of the tier measured in billable units,
    so a ladder of ``(10000, 50000, None)`` charges the first 10,000 billable units
    at the first price, the next 40,000 at the second, and everything beyond at the
    third. ``None`` means unbounded; a ladder whose last tier is bounded is legal but
    cannot price usage past its end, and the engine reports that as unresolved rather
    than extrapolating a rate nobody agreed to.
    """

    up_to: Decimal | None
    unit_price: Money

    def __post_init__(self) -> None:
        if self.up_to is None:
            object.__setattr__(self, "up_to", None)
        else:
            bound = _require_non_negative(_to_decimal(self.up_to, "Tier.up_to"), "Tier.up_to")
            object.__setattr__(self, "up_to", bound)
        if not isinstance(self.unit_price, Money):
            raise BillingDataError("Tier.unit_price must be a Money amount")
        if self.unit_price.is_negative():
            raise BillingDataError(f"tier unit price must not be negative, got {self.unit_price}")


@dataclass(frozen=True)
class PriceTerm:
    """The pricing rule for one metered metric on one contract.

    A domain counterpart to the ``contract_price_terms`` row, with ``Money`` amounts
    so that no rate can exist without a currency.

    Validity is enforced here rather than in each calculator. ``billing_mode``
    determines which of the optional amounts are required, and stating that once means
    the three rules in :mod:`app.pricing.rules` contain arithmetic and no
    precondition checks.
    """

    metric_key: str
    billing_mode: BillingMode
    currency: str
    unit_price: Money | None = None
    included_units: Decimal = Decimal(0)
    overage_price: Money | None = None
    minimum_commitment: Money | None = None
    tiers: tuple[Tier, ...] | None = None

    def __post_init__(self) -> None:
        _require_non_blank(self.metric_key, "PriceTerm.metric_key")
        if not isinstance(self.billing_mode, BillingMode):
            raise BillingDataError(
                f"PriceTerm.billing_mode must be a BillingMode, got {self.billing_mode!r}"
            )
        # Money validates and normalises the currency, so this also rejects a
        # non-string currency and lower-cases nothing silently.
        currency = Money.zero(self.currency).currency

        object.__setattr__(
            self,
            "included_units",
            _require_non_negative(
                _to_decimal(self.included_units, "PriceTerm.included_units"), "included_units"
            ),
        )
        if self.tiers is not None:
            object.__setattr__(self, "tiers", tuple(self.tiers))

        self._require_rates_in_currency(currency)
        self._validate_against_mode()

    def _require_rates_in_currency(self, currency: str) -> None:
        """Every amount on the term must be denominated in the term's currency.

        A term whose rates mix currencies cannot produce a charge, and catching it
        here gives a precise message instead of a ``CurrencyMismatchError`` raised
        from inside a multiplication.
        """
        amounts = {
            "unit_price": self.unit_price,
            "overage_price": self.overage_price,
            "minimum_commitment": self.minimum_commitment,
        }
        for name, amount in amounts.items():
            if amount is None:
                continue
            if amount.currency != currency:
                raise BillingDataError(
                    f"{name} is in {amount.currency} but the term is in {currency}; "
                    "a price term must state all its rates in one currency"
                )
            if amount.is_negative():
                # The database also checks this (``unit_price_not_negative`` and
                # friends), but a negative rate is meaningless rather than merely
                # unusual: it would silently subtract from the customer's bill.
                raise BillingDataError(
                    f"{name} must not be negative, got {amount}; a negative rate would "
                    "reduce the charge instead of pricing it"
                )
        for tier in self.tiers or ():
            if tier.unit_price.currency != currency:
                raise BillingDataError(
                    f"tier price is in {tier.unit_price.currency} but the term is in "
                    f"{currency}; a price term must state all its rates in one currency"
                )

    def _validate_against_mode(self) -> None:
        if self.billing_mode is BillingMode.TIERED:
            if not self.tiers:
                raise BillingDataError(
                    "a TIERED price term requires a tier ladder; tiered pricing with no "
                    "ladder cannot be calculated (docs/SYSTEM_DESIGN.md §3.2, SQ-03)"
                )
            self._validate_ladder()
            return

        if self.tiers:
            raise BillingDataError(
                f"a {self.billing_mode.value} price term must not carry a tier ladder; "
                "the ladder would be a rule nothing reads"
            )

        if self.billing_mode is BillingMode.COMMITMENT:
            if self.minimum_commitment is None:
                raise BillingDataError(
                    "a COMMITMENT price term requires a minimum_commitment; the floor is "
                    "the whole point of the mode"
                )
        elif self.unit_price is None and self.overage_price is None:
            raise BillingDataError(
                f"a {self.billing_mode.value} price term requires unit_price or overage_price"
            )

    def _validate_ladder(self) -> None:
        """Bounds must strictly increase, so every tier has a positive width.

        A repeated or decreasing bound would create a zero-width or reversed tier. That
        is not a rounding curiosity: it makes the amount depend on how the ladder is
        walked, which would break the determinism the engine promises.
        """
        assert self.tiers is not None  # guaranteed by _validate_against_mode
        previous = Decimal(0)
        unbounded_seen = False
        for tier in self.tiers:
            if unbounded_seen:
                raise BillingDataError(
                    "only the final tier of a ladder may be unbounded; a bounded tier "
                    "after an unbounded one can never be reached"
                )
            if tier.up_to is None:
                unbounded_seen = True
                continue
            if tier.up_to <= previous:
                raise BillingDataError(
                    f"tier bounds must strictly increase; got {tier.up_to} after {previous}"
                )
            previous = tier.up_to

    @property
    def rate_for_billable_units(self) -> Money:
        """The price that applies to a unit beyond ``included_units``.

        ``included_units`` are a free allowance: usage up to that threshold is covered
        by the price and is not charged. Units beyond it are charged at
        ``overage_price`` where one is configured, and at ``unit_price`` otherwise.

        This is the reading that gives both columns a single unambiguous job. A term
        is configured either with one rate that applies to every billable unit
        (``unit_price`` alone) or with an allowance and a separate excess rate
        (``included_units`` plus ``overage_price``). The alternative, treating
        included units as chargeable at ``unit_price`` and only the excess at
        ``overage_price``, leaves ``unit_price`` unread whenever ``overage_price`` is
        set, which is configuration nobody can reason about. Recorded as assumption 3.
        """
        rate = self.overage_price if self.overage_price is not None else self.unit_price
        if rate is None:
            # Unreachable for a validated PER_UNIT or COMMITMENT term, but the return
            # type promises a rate and a bare None here would surface much later.
            raise BillingDataError(
                f"price term for {self.metric_key!r} has no rate to charge billable units"
            )
        return rate


@dataclass(frozen=True)
class InvoicePeriod:
    """A half-open date range ``[start, end)`` that usage is matched against."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise BillingDataError(
                f"invoice period must satisfy start < end, got {self.start} to {self.end}"
            )

    def contains(self, moment: datetime) -> bool:
        """Whether ``moment`` falls inside the period.

        Judged by the UTC calendar day, after converting the instant. Using the timestamp's
        own local date instead would bill a ``2025-04-01T00:00+05:30`` event into March,
        because its local date is 1 April while the instant it names is still 31 March UTC.
        Half-open, so consecutive periods tile the timeline without overlapping.
        """
        utc_date = moment.astimezone(timezone.utc).date()
        return self.start <= utc_date < self.end


@dataclass(frozen=True)
class InvoiceLine:
    """One line of an invoice: what the customer was actually charged.

    ``recorded_amount`` is a fact about the source system, not a computed value. It is kept
    separate from the engine's output throughout so that "what they billed" and "what the
    contract says they owed" are never the same number by accident.

    Only the amount is modelled. An invoice also states a quantity and a rate, and it would
    be reasonable to record them, but nothing here compares them and the comparison is not
    well defined yet: the quantity on an invoice is the metered total, whereas the engine's
    ``billable_units`` is what remains after the included allowance, so setting both fields
    would invite a comparison that silently compares different things. They are added when
    the quantity-versus-rate distinction is actually being drawn.
    """

    metric_key: str
    line_type: LineType
    recorded_amount: Money

    def __post_init__(self) -> None:
        _require_non_blank(self.metric_key, "InvoiceLine.metric_key")
        if not isinstance(self.line_type, LineType):
            raise BillingDataError(
                f"InvoiceLine.line_type must be a LineType, got {self.line_type!r}"
            )
        if not isinstance(self.recorded_amount, Money):
            raise BillingDataError("InvoiceLine.recorded_amount must be a Money amount")


@dataclass(frozen=True)
class RecordedInvoice:
    """An invoice as issued: its period, its lines, and the total it stated."""

    external_id: str
    period: InvoicePeriod
    currency: str
    lines: tuple[InvoiceLine, ...]
    stated_total: Money

    def __post_init__(self) -> None:
        _require_non_blank(self.external_id, "RecordedInvoice.external_id")
        if not isinstance(self.period, InvoicePeriod):
            raise BillingDataError("RecordedInvoice.period must be an InvoicePeriod")
        object.__setattr__(self, "lines", tuple(self.lines))
        currency = Money.zero(self.currency).currency
        object.__setattr__(self, "currency", currency)
        if self.stated_total.currency != currency:
            raise BillingDataError(
                f"stated_total is in {self.stated_total.currency} but the invoice is in {currency}"
            )
        if not self.lines:
            # An invoice with no lines is legitimate only if it charges nothing. One with a
            # total and no lines to support it is the INV-04 defect, and admitting it would
            # let a real discrepancy pass as an unusual shape.
            if not self.stated_total.is_zero():
                raise BillingDataError(
                    f"invoice {self.external_id} states {self.stated_total} but has no "
                    "lines; a total with nothing behind it cannot be reconciled"
                )
            return
        mismatched = sorted(
            {line.metric_key for line in self.lines if line.recorded_amount.currency != currency}
        )
        if mismatched:
            raise BillingDataError(
                f"invoice {self.external_id} is in {currency} but these lines are not: "
                + ", ".join(mismatched)
            )

    @property
    def line_total(self) -> Money:
        """Sum of the recorded line amounts.

        Deliberately distinct from ``stated_total``. When the two disagree the
        difference is the reconciliation finding INV-04 describes, not an arithmetic
        error to be corrected in passing.
        """
        total = Money.zero(self.currency)
        for line in self.lines:
            total = total + line.recorded_amount
        return total


@dataclass(frozen=True)
class PaymentAllocation:
    """The part of one payment applied to one invoice.

    A separate object rather than a field on :class:`Payment` because one payment routinely
    settles several invoices: a single ``$300`` remittance covering two invoices cannot be
    expressed as a total, and a total would leave reconciliation unable to tell how much of
    the payment belongs to the invoice in front of it. It would then have to either ignore
    the payment or apply all of it, and both are wrong.
    """

    invoice_external_id: str
    amount: Money

    def __post_init__(self) -> None:
        _require_non_blank(self.invoice_external_id, "PaymentAllocation.invoice_external_id")
        if not isinstance(self.amount, Money):
            raise BillingDataError("PaymentAllocation.amount must be a Money amount")
        if self.amount.is_negative():
            raise BillingDataError(
                f"allocation to {self.invoice_external_id} is {self.amount}; an allocation "
                "reduces the balance and cannot be negative"
            )


@dataclass(frozen=True)
class Payment:
    """Money received, and the part of it applied to invoices.

    INV-05 requires that the allocations can never exceed the payment, and it is checked
    here rather than at the point of display so that the invariant holds for every caller.

    An empty ``allocations`` tuple means no allocation has been recorded, which is a real
    state rather than an error: an unapplied payment is a documented cause of a dispute
    (§6.3, ``UNAPPLIED_PAYMENT``). It is kept distinct from an explicit allocation of zero,
    because "nobody has applied this yet" and "somebody applied nothing to it" lead to
    different investigations.
    """

    external_id: str
    amount: Money
    allocations: tuple[PaymentAllocation, ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank(self.external_id, "Payment.external_id")
        if not isinstance(self.amount, Money):
            raise BillingDataError("Payment.amount must be a Money amount")
        if self.amount.is_negative():
            raise BillingDataError(
                f"payment {self.external_id} is {self.amount}; money received cannot be "
                "negative. A refund is a negative movement, not a negative payment"
            )
        object.__setattr__(self, "allocations", tuple(self.allocations))

        seen: set[str] = set()
        for allocation in self.allocations:
            if not isinstance(allocation, PaymentAllocation):
                raise BillingDataError(
                    "Payment.allocations must contain PaymentAllocation objects, got "
                    f"{type(allocation).__name__}"
                )
            if allocation.amount.currency != self.amount.currency:
                raise BillingDataError(
                    f"allocation to {allocation.invoice_external_id} is in "
                    f"{allocation.amount.currency} but the payment is in "
                    f"{self.amount.currency}"
                )
            if allocation.invoice_external_id in seen:
                raise BillingDataError(
                    f"payment {self.external_id} allocates to "
                    f"{allocation.invoice_external_id} more than once; merge them so the "
                    "allocation can be checked against the payment"
                )
            seen.add(allocation.invoice_external_id)

        if self.allocated_total is not None and self.allocated_total > self.amount:
            raise BillingDataError(
                f"payment {self.external_id} allocates {self.allocated_total} of a "
                f"{self.amount} payment, which would create money (INV-05)"
            )

    @property
    def allocated_total(self) -> Money | None:
        """The sum applied across every invoice, or ``None`` if none has been recorded."""
        if not self.allocations:
            return None
        total = Money.zero(self.amount.currency)
        for allocation in self.allocations:
            total = total + allocation.amount
        return total

    @property
    def unallocated(self) -> Money:
        """The part of the payment not applied to any invoice."""
        allocated = self.allocated_total
        if allocated is None:
            return self.amount
        return self.amount - allocated

    def allocated_to(self, invoice_external_id: str) -> Money | None:
        """What this payment contributed to one invoice, or ``None`` if never allocated."""
        for allocation in self.allocations:
            if allocation.invoice_external_id == invoice_external_id:
                return allocation.amount
        return None


@dataclass(frozen=True)
class Adjustment:
    """A credit or surcharge already applied to an invoice.

    A positive amount is a credit that reduces the outstanding balance; a negative amount
    is a surcharge that increases it. See the module docstring, assumption 3.

    The sign carries the direction, so ``Money`` is asked for a signed amount here. That
    money can be negative is unusual enough to look like a mistake, which is exactly why
    :mod:`app.pricing.reconciliation` states the direction in its own formula and names it
    in the result, rather than leaving a reader to infer it from the sign of one number.
    """

    external_id: str
    amount: Money
    reason: str = ""

    def __post_init__(self) -> None:
        _require_non_blank(self.external_id, "Adjustment.external_id")
        if not isinstance(self.amount, Money):
            raise BillingDataError("Adjustment.amount must be a Money amount")
        if self.amount.is_zero():
            raise BillingDataError(
                f"adjustment {self.external_id} is zero; record no adjustment rather than "
                "one that does nothing, so the audit trail stays meaningful"
            )
