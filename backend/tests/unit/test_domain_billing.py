"""Tests for the billing input types: what they refuse, and why.

These are the tests that matter most for a dispute, though they look the least interesting.
Every one of them describes a record that reached the calculator half-formed in a real
system at some point, and the alternative to raising at construction time is a recalculation
that quietly treats a missing rate as zero or a negative quantity as a credit.

The error messages are asserted on, not just the exception type, because the message is what
a support engineer sees. "Invalid input" tells them nothing; "minimum_commitment must not
be negative, got -10.00 USD" tells them which field to fix.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.billing import (
    Adjustment,
    BillingDataError,
    InvoiceLine,
    InvoicePeriod,
    LineType,
    Payment,
    PaymentAllocation,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.contracts import BillingMode
from app.domain.money import Money


def usd(value: str) -> Money:
    return Money(Decimal(value), "USD")


def period() -> InvoicePeriod:
    return InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))


def event(**overrides: object) -> UsageEvent:
    defaults: dict[str, object] = {
        "external_id": "evt-1",
        "dedupe_key": "dedupe-1",
        "metric_key": "api_calls",
        "occurred_at": datetime(2025, 3, 15, tzinfo=timezone.utc),
        "quantity": Decimal("1000"),
        "unit": "calls",
    }
    defaults.update(overrides)
    return UsageEvent(**defaults)  # type: ignore[arg-type]


def per_unit_term(**overrides: object) -> PriceTerm:
    defaults: dict[str, object] = {
        "metric_key": "api_calls",
        "billing_mode": BillingMode.PER_UNIT,
        "currency": "USD",
        "unit_price": usd("0.05"),
    }
    defaults.update(overrides)
    return PriceTerm(**defaults)  # type: ignore[arg-type]


def usage_line(**overrides: object) -> InvoiceLine:
    defaults: dict[str, object] = {
        "metric_key": "api_calls",
        "line_type": LineType.USAGE,
        "recorded_amount": usd("50.00"),
    }
    defaults.update(overrides)
    return InvoiceLine(**defaults)  # type: ignore[arg-type]


class TestUsageEvent:
    """Metered usage as it arrives from a source system."""

    def test_it_keeps_an_exact_fractional_quantity(self) -> None:
        """Usage is frequently fractional: bytes, seconds, sub-call durations.

        Preserving the exact value matters because the amount is quantity times rate, and a
        rate is per single unit. Truncating 0.1 of a call to nothing would misprice it by a
        whole unit's worth of rate.
        """
        assert event(quantity=Decimal("1000.5")).quantity == Decimal("1000.5")

    def test_a_float_quantity_is_refused(self) -> None:
        """A float is refused at the boundary rather than converted.

        ``Decimal(0.1)`` is not ``0.1``; it is the binary approximation nearest to it. Once
        a float reaches a Decimal it has already lost the value, so the conversion would
        launder the error rather than reveal it.
        """
        with pytest.raises(BillingDataError, match="Decimal"):
            event(quantity=1000.5)  # type: ignore[arg-type]

    def test_a_boolean_quantity_is_refused(self) -> None:
        """``True`` is an ``int`` in Python, so it would silently become a quantity of 1."""
        with pytest.raises(BillingDataError, match="Decimal"):
            event(quantity=True)  # type: ignore[arg-type]

    def test_a_numeric_string_quantity_is_accepted_exactly(self) -> None:
        """Numeric strings are converted losslessly.

        External systems routinely deliver quantities as JSON strings, and refusing them
        would mean rejecting correct evidence at the door. The conversion is exact, so
        accepting a string costs nothing in precision.
        """
        assert event(quantity="1000.50").quantity == Decimal("1000.50")

    def test_a_non_numeric_quantity_is_refused(self) -> None:
        """A quantity carrying its unit, such as ``"1000 calls"``, cannot be priced."""
        with pytest.raises(BillingDataError, match="not a valid decimal"):
            event(quantity="1000 calls")  # type: ignore[arg-type]

    def test_a_negative_quantity_is_refused(self) -> None:
        """Negative usage has no meaning here and would reduce a charge.

        A correction or a reversal is a separate event with its own identifier, not a
        negative metered quantity, and accepting both would let the two be double-counted.
        """
        with pytest.raises(BillingDataError, match="must not be negative"):
            event(quantity=Decimal("-5"))

    def test_a_non_finite_quantity_is_refused(self) -> None:
        """NaN and infinity cannot be priced and would poison every total they touched."""
        with pytest.raises(BillingDataError, match="finite"):
            event(quantity=Decimal("NaN"))

    def test_a_blank_metric_key_is_refused(self) -> None:
        with pytest.raises(BillingDataError, match="metric_key"):
            event(metric_key="  ")

    def test_a_naive_timestamp_is_refused(self) -> None:
        """A timestamp without a zone is ambiguous, and the period boundary depends on it.

        ``2025-04-01T00:00:00`` is two different instants in April and would move an event
        across the invoice boundary depending on which system produced it.
        """
        with pytest.raises(BillingDataError, match="timezone"):
            event(occurred_at=datetime(2025, 3, 15))


class TestInvoicePeriod:
    """The window usage is selected from."""

    def test_it_is_half_open_so_periods_can_be_chained(self) -> None:
        """March and April must not both contain 1 April at midnight.

        The boundary event belongs to exactly one period, or a chained set of monthly
        invoices double-counts it.
        """
        boundary = datetime(2025, 4, 1, tzinfo=timezone.utc)

        assert not period().contains(boundary)
        assert InvoicePeriod(date(2025, 4, 1), date(2025, 5, 1)).contains(boundary)

    def test_a_reversed_period_is_refused(self) -> None:
        """An end before the start would select no usage and silently price everything free."""
        with pytest.raises(BillingDataError, match="end"):
            InvoicePeriod(date(2025, 4, 1), date(2025, 3, 1))

    def test_an_empty_period_is_refused(self) -> None:
        with pytest.raises(BillingDataError, match="start < end"):
            InvoicePeriod(date(2025, 3, 1), date(2025, 3, 1))


class TestTier:
    """One rung of a tier ladder."""

    def test_an_unbounded_final_tier_is_allowed(self) -> None:
        assert Tier(None, usd("0.0005")).up_to is None

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(BillingDataError, match="must not be negative"):
            Tier(Decimal("1000"), usd("-0.01"))

    def test_it_needs_a_price(self) -> None:
        with pytest.raises(BillingDataError, match="unit_price"):
            Tier(Decimal("1000"), None)  # type: ignore[arg-type]


class TestPriceTerm:
    """A contract's pricing for one metric."""

    def test_a_per_unit_rate_must_be_present(self) -> None:
        """A per-unit term with no rate could only ever produce a zero charge."""
        with pytest.raises(BillingDataError, match="unit_price"):
            per_unit_term(unit_price=None)

    def test_a_tiered_term_must_carry_a_ladder(self) -> None:
        """A tiered term with no tiers has nothing to walk."""
        with pytest.raises(BillingDataError, match="tier"):
            PriceTerm(
                metric_key="api_calls",
                billing_mode=BillingMode.TIERED,
                currency="USD",
            )

    def test_a_tier_bounds_must_strictly_increase(self) -> None:
        """Equal or decreasing bounds give a tier of zero or negative width.

        A zero-width tier would be skipped silently and a negative one would corrupt the
        remaining-units arithmetic, so both are refused where they can be named.
        """
        with pytest.raises(BillingDataError, match="strictly increase"):
            PriceTerm(
                metric_key="api_calls",
                billing_mode=BillingMode.TIERED,
                currency="USD",
                tiers=(Tier(Decimal("1000"), usd("0.05")), Tier(Decimal("1000"), usd("0.01"))),
            )

    def test_a_commitment_term_must_state_its_minimum(self) -> None:
        """Without a minimum there is no commitment, and the mode would be a PER_UNIT term."""
        with pytest.raises(BillingDataError, match="minimum_commitment"):
            PriceTerm(
                metric_key="api_calls",
                billing_mode=BillingMode.COMMITMENT,
                currency="USD",
                unit_price=usd("0.05"),
            )

    def test_a_negative_rate_is_refused(self) -> None:
        """A negative rate would reduce the customer's bill instead of charging for usage.

        This is refused rather than passed through, because it is not a plausible reading of
        a contract and no customer would thank us for honouring it.
        """
        with pytest.raises(BillingDataError, match="must not be negative"):
            per_unit_term(unit_price=usd("-0.01"))

    def test_a_negative_overage_rate_is_refused(self) -> None:
        with pytest.raises(BillingDataError, match="must not be negative"):
            per_unit_term(overage_price=usd("-0.01"))

    def test_a_negative_committed_minimum_is_refused(self) -> None:
        """A negative floor would silently reduce the charge on every line it applies to."""
        with pytest.raises(BillingDataError, match="must not be negative"):
            PriceTerm(
                metric_key="api_calls",
                billing_mode=BillingMode.COMMITMENT,
                currency="USD",
                unit_price=usd("0.05"),
                minimum_commitment=usd("-100.00"),
            )

    def test_rates_in_another_currency_are_refused(self) -> None:
        """The result would have to add a dollar amount to a euro total.

        Named at construction so the message identifies the offending field, instead of a
        currency error surfacing from inside a multiplication several layers down.
        """
        with pytest.raises(BillingDataError, match="one currency"):
            PriceTerm(
                metric_key="api_calls",
                billing_mode=BillingMode.PER_UNIT,
                currency="EUR",
                unit_price=usd("0.05"),
            )

    def test_a_negative_allowance_is_refused(self) -> None:
        with pytest.raises(BillingDataError, match="must not be negative"):
            per_unit_term(included_units=Decimal("-1000"))

    def test_a_non_ladder_mode_may_not_carry_tiers(self) -> None:
        """Tiers on a PER_UNIT term are ambiguous: there would be no rule saying which wins."""
        with pytest.raises(BillingDataError, match="tier"):
            per_unit_term(tiers=(Tier(None, usd("0.01")),))

    def test_an_overage_rate_replaces_the_base_rate_entirely(self) -> None:
        """Where ``overage_price`` is set it prices every billable unit, not just the excess.

        Asserted because it is a simplification rather than an obvious rule: a blended
        reading, where the first units keep ``unit_price`` and only the excess is billed at
        ``overage_price``, is a different contract and is not implemented. A tiered term is
        how a contract expresses a blended rate.
        """
        term = per_unit_term(
            unit_price=usd("0.05"), included_units=Decimal("1000"), overage_price=usd("0.03")
        )

        assert term.rate_for_billable_units == usd("0.03")

    def test_the_base_rate_applies_when_no_overage_rate_is_set(self) -> None:
        assert per_unit_term(unit_price=usd("0.05")).rate_for_billable_units == usd("0.05")


class TestRecordedInvoice:
    """An invoice as the source system issued it."""

    def test_its_stated_total_is_kept_apart_from_its_line_sum(self) -> None:
        """When the two disagree, that gap is the INV-04 finding, not an error to tidy up.

        Overwriting the stated total with the line sum would destroy the only evidence that
        the two ever differed.
        """
        invoice = RecordedInvoice(
            external_id="INV-1",
            period=period(),
            currency="USD",
            lines=(usage_line(recorded_amount=usd("50.00")),),
            stated_total=usd("60.00"),
        )

        assert invoice.line_total == usd("50.00")
        assert invoice.stated_total == usd("60.00")

    def test_a_line_in_another_currency_is_refused(self) -> None:
        """Adding them would be meaningless, so the invoice is refused as malformed."""
        with pytest.raises(BillingDataError, match="these lines are not"):
            RecordedInvoice(
                external_id="INV-1",
                period=period(),
                currency="USD",
                lines=(usage_line(recorded_amount=Money(Decimal("50.00"), "EUR")),),
                stated_total=usd("50.00"),
            )

    def test_a_nil_invoice_may_have_no_lines(self) -> None:
        """A zero invoice is legitimate, and forcing a line onto it would invent a charge."""
        assert (
            RecordedInvoice(
                external_id="INV-1",
                period=period(),
                currency="USD",
                lines=(),
                stated_total=usd("0.00"),
            ).lines
            == ()
        )

    def test_a_total_with_no_lines_behind_it_is_refused(self) -> None:
        """This is the INV-04 defect in its clearest form.

        Admitting it would let a genuine discrepancy pass as an unusual invoice shape, and
        the whole purpose of the total-versus-lines comparison is to catch exactly this.
        """
        with pytest.raises(BillingDataError, match="no lines"):
            RecordedInvoice(
                external_id="INV-1",
                period=period(),
                currency="USD",
                lines=(),
                stated_total=usd("50.00"),
            )


class TestPayment:
    """Money received, and how much of it reached this invoice."""

    def test_an_unallocated_payment_is_not_the_same_as_a_zero_allocation(self) -> None:
        """The two mean different things and lead to different investigations.

        "Nobody applied this payment yet" is a known cause of dispute (§6.3). "Somebody
        applied nothing to it" is a deliberate decision. Collapsing them would hide the
        first behind the second.
        """
        unallocated = Payment(external_id="PAY-1", amount=usd("100.00"))
        explicitly_zero = Payment(
            external_id="PAY-2",
            amount=usd("100.00"),
            allocations=(PaymentAllocation("INV-1", usd("0.00")),),
        )

        assert unallocated.allocated_to("INV-1") is None
        assert unallocated.unallocated == usd("100.00")
        assert explicitly_zero.allocated_to("INV-1") == usd("0.00")
        assert explicitly_zero.unallocated == usd("100.00")

    def test_it_reports_only_the_share_allocated_to_one_invoice(self) -> None:
        """A single remittance routinely settles several invoices.

        Returning the whole payment for any of them would credit one invoice with money that
        belongs to another, which is the mirror image of the error the allocation exists to
        prevent.
        """
        payment = Payment(
            external_id="PAY-1",
            amount=usd("300.00"),
            allocations=(
                PaymentAllocation("INV-1", usd("120.00")),
                PaymentAllocation("INV-2", usd("180.00")),
            ),
        )

        assert payment.allocated_to("INV-1") == usd("120.00")
        assert payment.allocated_to("INV-2") == usd("180.00")
        assert payment.allocated_to("INV-3") is None
        assert payment.allocated_total == usd("300.00")
        assert payment.unallocated == usd("0.00")

    def test_allocating_more_than_was_received_is_refused(self) -> None:
        """This is INV-05: allocations can never exceed the payment.

        Checked at construction so the invariant holds for every caller rather than only for
        whoever happens to display the payment.
        """
        with pytest.raises(BillingDataError, match="would create money"):
            Payment(
                external_id="PAY-1",
                amount=usd("100.00"),
                allocations=(
                    PaymentAllocation("INV-1", usd("60.00")),
                    PaymentAllocation("INV-2", usd("60.00")),
                ),
            )

    def test_two_allocations_to_one_invoice_are_refused(self) -> None:
        """Split rows are merged upstream so the total can be checked against the payment."""
        with pytest.raises(BillingDataError, match="more than once"):
            Payment(
                external_id="PAY-1",
                amount=usd("100.00"),
                allocations=(
                    PaymentAllocation("INV-1", usd("40.00")),
                    PaymentAllocation("INV-1", usd("30.00")),
                ),
            )

    def test_a_negative_payment_is_refused(self) -> None:
        """A refund is a negative movement against an account, not money received."""
        with pytest.raises(BillingDataError, match="cannot be negative"):
            Payment(external_id="PAY-1", amount=usd("-100.00"))

    def test_a_negative_allocation_is_refused(self) -> None:
        """An allocation reduces a balance; a negative one would increase it."""
        with pytest.raises(BillingDataError, match="cannot be negative"):
            PaymentAllocation("INV-1", usd("-10.00"))


class TestAdjustment:
    """A credit or surcharge already applied."""

    def test_a_credit_is_positive_and_a_surcharge_negative(self) -> None:
        """The sign carries the direction, which the balance formula subtracts.

        Asserted explicitly because the convention is otherwise only discoverable by reading
        the reconciliation formula, and getting it backwards would turn a goodwill credit into
        money the customer still owes.
        """
        credit = Adjustment(external_id="ADJ-1", amount=usd("50.00"))
        surcharge = Adjustment(external_id="ADJ-2", amount=usd("-30.00"))

        assert not credit.amount.is_negative()
        assert surcharge.amount.is_negative()

    def test_a_zero_adjustment_is_refused(self) -> None:
        """An adjustment that does nothing is noise in the audit trail."""
        with pytest.raises(BillingDataError, match="zero"):
            Adjustment(external_id="ADJ-1", amount=usd("0.00"))
