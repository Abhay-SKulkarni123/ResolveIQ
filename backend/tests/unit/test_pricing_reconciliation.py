"""Tests for what is left owing on an invoice.

Reconciliation is where a correct recalculation can still produce a wrong answer, because
the inputs here are events that happened after the invoice: money received, credits issued,
payments never applied. The tests concentrate on the cases where doing the obvious thing is
wrong — applying an unallocated payment because the money exists, clamping an overpayment to
zero, netting a credit off the wrong side.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.billing import (
    Adjustment,
    InvoiceLine,
    InvoicePeriod,
    LineType,
    Payment,
    PaymentAllocation,
    PriceTerm,
    RecordedInvoice,
    UsageEvent,
)
from app.domain.contracts import BillingMode
from app.domain.money import CurrencyMismatchError, Money
from app.pricing.engine import recalculate_invoice
from app.pricing.reconciliation import reconcile_balance

CONTRACT = "CTR-5512"
INVOICE_ID = "INV-2025-03-001"
MARCH = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))


def usd(value: str) -> Money:
    return Money(Decimal(value), "USD")


@pytest.fixture
def recalculation():  # type: ignore[no-untyped-def]
    """An invoice of $120.00 that recalculates exactly, so the balance arithmetic is visible.

    Per-unit at $0.06 over 2,000 calls. Nothing unresolved, so any movement in the outstanding
    figure is attributable to the payment or adjustment under test and nothing else.
    """
    term = PriceTerm(
        metric_key="api_calls",
        billing_mode=BillingMode.PER_UNIT,
        currency="USD",
        unit_price=usd("0.06"),
    )
    invoice = RecordedInvoice(
        external_id=INVOICE_ID,
        period=MARCH,
        currency="USD",
        lines=(
            InvoiceLine(
                metric_key="api_calls",
                line_type=LineType.USAGE,
                recorded_amount=usd("120.00"),
            ),
        ),
        stated_total=usd("120.00"),
    )
    usage = UsageEvent(
        external_id="e1",
        dedupe_key="dedupe-e1",
        metric_key="api_calls",
        occurred_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
        quantity=Decimal("2000"),
        unit="calls",
    )
    return recalculate_invoice(invoice, [term], [usage], contract_external_id=CONTRACT)


class TestTheBasicBalance:
    """Total less payments less credits."""

    def test_an_unpaid_invoice_owes_everything(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        balance = reconcile_balance(recalculation, [], [])

        assert balance.outstanding == usd("120.00")
        assert balance.is_settled is False
        assert balance.is_provisional is False

    def test_a_fully_paid_invoice_is_settled(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        payment = Payment(
            external_id="PAY-1",
            amount=usd("120.00"),
            allocations=(PaymentAllocation(INVOICE_ID, usd("120.00")),),
        )

        balance = reconcile_balance(recalculation, [payment], [])

        assert balance.outstanding.is_zero()
        assert balance.is_settled

    def test_a_part_payment_leaves_the_remainder_owing(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        payment = Payment(
            external_id="PAY-1",
            amount=usd("50.00"),
            allocations=(PaymentAllocation(INVOICE_ID, usd("50.00")),),
        )

        assert reconcile_balance(recalculation, [payment], []).outstanding == usd("70.00")

    def test_every_component_of_the_balance_is_reported(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        """A lone outstanding figure invites the suspicion that something was netted off.

        The customer needs to see that the payment was applied and the credit honoured, so
        the inputs are carried alongside the answer rather than discarded once summed.
        """
        balance = reconcile_balance(
            recalculation,
            [
                Payment(
                    external_id="PAY-1",
                    amount=usd("50.00"),
                    allocations=(PaymentAllocation(INVOICE_ID, usd("50.00")),),
                )
            ],
            [Adjustment(external_id="ADJ-1", amount=usd("10.00"), reason="goodwill")],
        )

        assert balance.recalculated_total == usd("120.00")
        assert balance.allocated_payments == usd("50.00")
        assert balance.net_adjustments == usd("10.00")
        assert balance.outstanding == usd("60.00")


class TestAdjustments:
    """Credits reduce the balance; surcharges increase it."""

    def test_a_credit_reduces_what_is_owed(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        balance = reconcile_balance(
            recalculation, [], [Adjustment(external_id="ADJ-1", amount=usd("20.00"))]
        )

        assert balance.outstanding == usd("100.00")

    def test_a_surcharge_increases_what_is_owed(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        """A negative adjustment is added, not subtracted.

        The single formula ``total - payments - adjustments`` handles both directions
        because the sign carries them. A rule that branched on the sign, or that clamped a
        surcharge away, would move money in the wrong direction for one of the two cases.
        """
        balance = reconcile_balance(
            recalculation, [], [Adjustment(external_id="ADJ-1", amount=usd("-30.00"))]
        )

        assert balance.net_adjustments == usd("-30.00")
        assert balance.outstanding == usd("150.00")

    def test_several_adjustments_are_netted(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        balance = reconcile_balance(
            recalculation,
            [],
            [
                Adjustment(external_id="ADJ-1", amount=usd("20.00")),
                Adjustment(external_id="ADJ-2", amount=usd("-5.00")),
            ],
        )

        assert balance.net_adjustments == usd("15.00")
        assert balance.outstanding == usd("105.00")


class TestPaymentsThatBelongElsewhere:
    """One remittance routinely settles several invoices."""

    def test_only_the_allocated_share_reduces_this_invoice(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        payment = Payment(
            external_id="PAY-1",
            amount=usd("300.00"),
            allocations=(
                PaymentAllocation(INVOICE_ID, usd("120.00")),
                PaymentAllocation("INV-2025-02-001", usd("180.00")),
            ),
        )

        balance = reconcile_balance(recalculation, [payment], [])

        assert balance.allocated_payments == usd("120.00")
        assert balance.outstanding.is_zero()

    def test_an_unallocated_payment_does_not_reduce_the_balance(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        """The money exists, but nobody has said which invoice it pays.

        Applying it because the invoice happens to be open would be a guess, and a guess that
        hides a real balance is worse than an obviously unpaid invoice. §6.3 lists
        ``UNAPPLIED_PAYMENT`` as a cause of dispute for exactly this state.
        """
        payment = Payment(external_id="PAY-1", amount=usd("120.00"))

        balance = reconcile_balance(recalculation, [payment], [])

        assert balance.allocated_payments.is_zero()
        assert balance.outstanding == usd("120.00")

    def test_a_payment_allocated_to_another_invoice_is_reported_as_unapplied(
        self,
        recalculation,  # type: ignore[no-untyped-def]
    ) -> None:
        """Money received but spent elsewhere is still worth surfacing.

        The customer believes they paid; the invoice says otherwise. Reporting the amount
        turns that into a question with an answer rather than an argument.
        """
        payment = Payment(
            external_id="PAY-1",
            amount=usd("300.00"),
            allocations=(PaymentAllocation("INV-2025-02-001", usd("300.00")),),
        )

        balance = reconcile_balance(recalculation, [payment], [])

        assert balance.unapplied_payment_total == usd("300.00")
        assert balance.outstanding == usd("120.00")

    def test_an_unallocated_payment_is_not_counted_as_unapplied(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        """The two conditions mean different things and lead to different investigations.

        "Nobody applied this yet" is a processing gap. "It was applied to February" is a
        fact about another invoice, and counting it here would double-report the same money.
        """
        unallocated = Payment(external_id="PAY-1", amount=usd("50.00"))
        elsewhere = Payment(
            external_id="PAY-2",
            amount=usd("50.00"),
            allocations=(PaymentAllocation("INV-2025-02-001", usd("50.00")),),
        )

        assert reconcile_balance(recalculation, [unallocated], []).unapplied_payment_total.is_zero()
        assert reconcile_balance(recalculation, [elsewhere], []).unapplied_payment_total == usd(
            "50.00"
        )


class TestOverpayment:
    """Paying more than owed leaves a credit, not a zero."""

    def test_an_overpayment_is_reported_as_money_owed_back(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        """Clamping to zero would erase a refund that is genuinely due.

        The negative figure is the finding: the account is in credit and something has to
        happen about it. Reporting zero would make the account look settled and the customer
        would have to raise it again.
        """
        payment = Payment(
            external_id="PAY-1",
            amount=usd("150.00"),
            allocations=(PaymentAllocation(INVOICE_ID, usd("150.00")),),
        )

        balance = reconcile_balance(recalculation, [payment], [])

        assert balance.outstanding == usd("-30.00")
        assert balance.is_credit
        assert balance.is_settled is False

    def test_a_credit_larger_than_the_invoice_leaves_a_credit(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        balance = reconcile_balance(
            recalculation, [], [Adjustment(external_id="ADJ-1", amount=usd("200.00"))]
        )

        assert balance.outstanding == usd("-80.00")
        assert balance.is_credit


class TestIncompleteRecalculations:
    """A balance built on a partial total must say so."""

    def test_a_partially_recalculable_invoice_gives_a_provisional_balance(self) -> None:
        """The total behind the balance is a lower bound, so the balance is not an answer.

        An unresolved line might have been wrong in either direction. Reporting the figure
        without this flag would let a partial check read as a settled account.
        """
        term = PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT,
            currency="USD",
            unit_price=usd("0.06"),
        )
        invoice = RecordedInvoice(
            external_id=INVOICE_ID,
            period=MARCH,
            currency="USD",
            lines=(
                InvoiceLine(
                    metric_key="api_calls", line_type=LineType.USAGE, recorded_amount=usd("120.00")
                ),
                InvoiceLine(
                    metric_key="egress_gb", line_type=LineType.USAGE, recorded_amount=usd("30.00")
                ),
            ),
            stated_total=usd("150.00"),
        )
        usage = UsageEvent(
            external_id="e1",
            dedupe_key="dedupe-e1",
            metric_key="api_calls",
            occurred_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
            quantity=Decimal("2000"),
            unit="calls",
        )
        result = recalculate_invoice(invoice, [term], [usage], contract_external_id=CONTRACT)

        balance = reconcile_balance(result, [], [])

        assert balance.is_provisional
        assert balance.outstanding == usd("120.00")
        assert balance.unresolved_metrics == ("egress_gb",)

    def test_a_provisional_balance_is_never_reported_as_settled(self) -> None:
        """A tiny provisional figure is still not a verified zero.

        ``is_settled`` requiring a complete recalculation is what stops "we could not check
        the other line" from reading as "there is nothing outstanding".
        """
        term = PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT,
            currency="USD",
            unit_price=usd("0.06"),
        )
        invoice = RecordedInvoice(
            external_id=INVOICE_ID,
            period=MARCH,
            currency="USD",
            lines=(
                InvoiceLine(
                    metric_key="api_calls", line_type=LineType.USAGE, recorded_amount=usd("120.00")
                ),
                InvoiceLine(
                    metric_key="egress_gb", line_type=LineType.USAGE, recorded_amount=usd("30.00")
                ),
            ),
            stated_total=usd("150.00"),
        )
        usage = UsageEvent(
            external_id="e1",
            dedupe_key="dedupe-e1",
            metric_key="api_calls",
            occurred_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
            quantity=Decimal("2000"),
            unit="calls",
        )
        result = recalculate_invoice(invoice, [term], [usage], contract_external_id=CONTRACT)
        payment = Payment(
            external_id="PAY-1",
            amount=usd("120.00"),
            allocations=(PaymentAllocation(INVOICE_ID, usd("120.00")),),
        )

        assert reconcile_balance(result, [payment], []).is_settled is False


class TestRefusals:
    """Conditions reconciliation declines rather than approximates."""

    def test_a_payment_in_another_currency_is_refused(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        payment = Payment(
            external_id="PAY-1",
            amount=Money(Decimal("120.00"), "EUR"),
            allocations=(PaymentAllocation(INVOICE_ID, Money(Decimal("120.00"), "EUR")),),
        )

        with pytest.raises(CurrencyMismatchError, match="no implicit currency conversion"):
            reconcile_balance(recalculation, [payment], [])

    def test_an_adjustment_in_another_currency_is_refused(self, recalculation) -> None:  # type: ignore[no-untyped-def]
        adjustment = Adjustment(
            external_id="ADJ-1", amount=Money(Decimal("10.00"), "GBP"), reason="goodwill"
        )

        with pytest.raises(CurrencyMismatchError, match="ADJ-1"):
            reconcile_balance(recalculation, [], [adjustment])
