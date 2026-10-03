"""Tests for recalculating a whole invoice.

The unit tests below the engine cover each rule in isolation; these cover the parts that
only exist once lines, contracts and evidence are combined:

* a discrepancy, and that it is reported as a *difference* rather than a diagnosis;
* lines that cannot be recalculated, and the effect they have on the invoice total;
* the identity that keeps the total and the lines from disagreeing;
* determinism, which is what makes a dispute result defensible.

The §6.4 worked example is used as the fixture wherever possible. A test that reproduces the
figure printed in the design document is a test that fails when the document and the code
disagree, which is the point of having written the document.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.billing import (
    InvoiceLine,
    InvoicePeriod,
    LineType,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.contracts import BillingMode
from app.domain.money import CurrencyMismatchError, Money
from app.pricing.engine import recalculate_invoice
from app.pricing.results import LineStatus, UnresolvedReason

CONTRACT = "CTR-5512"
MARCH = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))
CALLS = "api_calls"


def usd(value: str) -> Money:
    return Money(Decimal(value), "USD")


def tiered_term(**overrides: object) -> PriceTerm:
    """The §6.4 contract: 10,000 calls included, then $0.00070 up to 50,000."""
    defaults: dict[str, object] = {
        "metric_key": CALLS,
        "billing_mode": BillingMode.TIERED,
        "currency": "USD",
        "included_units": Decimal("10000"),
        "tiers": (Tier(Decimal("50000"), usd("0.00070")), Tier(None, usd("0.00050"))),
    }
    defaults.update(overrides)
    return PriceTerm(**defaults)  # type: ignore[arg-type]


def usage_event(quantity: str, *, day: int = 15, event_id: str = "e1") -> UsageEvent:
    return UsageEvent(
        external_id=event_id,
        dedupe_key=f"dedupe-{event_id}",
        metric_key=CALLS,
        occurred_at=datetime(2025, 3, day, 12, tzinfo=timezone.utc),
        quantity=Decimal(quantity),
        unit="calls",
    )


def invoice(*lines: InvoiceLine, stated_total: str = "0.00") -> RecordedInvoice:
    return RecordedInvoice(
        external_id="INV-2025-03-001",
        period=MARCH,
        currency="USD",
        lines=lines,
        stated_total=usd(stated_total),
    )


def usage_line(amount: str, metric: str = CALLS) -> InvoiceLine:
    return InvoiceLine(metric_key=metric, line_type=LineType.USAGE, recorded_amount=usd(amount))


def fixed_line(amount: str, metric: str = "platform_fee") -> InvoiceLine:
    return InvoiceLine(metric_key=metric, line_type=LineType.FIXED, recorded_amount=usd(amount))


# ---------------------------------------------------------------------------
# The worked example, and a clean recalculation
# ---------------------------------------------------------------------------


class TestRecalculatingTheWorkedExample:
    """§6.4: 10,000 included calls, $0.00070 thereafter, 41,234 calls used."""

    def test_the_recalculated_amount_is_the_documented_figure(self) -> None:
        result = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert result.calculated_total == usd("21.86")
        assert result.is_complete
        assert result.difference == usd("-6.54")

    def test_a_negative_difference_means_the_invoice_overstated(self) -> None:
        """The direction of the difference is the whole point of computing it.

        A bare "difference of 6.54" leaves the reader to work out who owes whom. Here the
        recalculated figure is lower, so the invoice charged too much.
        """
        result = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert result.difference.is_negative()
        assert result.lines_with_discrepancies[0].difference == usd("-6.54")

    def test_the_exact_amount_is_kept_alongside_the_rounded_one(self) -> None:
        """Both figures are reported: the charged amount, and what it was derived from.

        Keeping the unrounded product is what lets a reviewer check the multiplication
        without redoing the rounding, and it is the figure the trace's steps add up to.
        """
        line = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        ).line_for(CALLS)

        assert line.exact_amount == usd("21.86380")
        assert line.calculated_amount == usd("21.86")

    def test_the_trace_carries_the_contract_term_that_was_applied(self) -> None:
        """The rule reference is how a reviewer checks the right contract version was used.

        Supplying a superseded contract is the most likely way to produce a confident wrong
        answer, and it produces no error at all. Naming the term makes the mistake visible in
        the result rather than only in a reviewer's memory.
        """
        line = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        ).line_for(CALLS)

        assert line.rule_ref == f"contract:{CONTRACT}#term:{CALLS}"
        assert line.trace is not None
        assert line.trace.rule_ref == f"contract:{CONTRACT}#term:{CALLS}"

    def test_a_matching_invoice_reports_no_discrepancy(self) -> None:
        result = recalculate_invoice(
            invoice(usage_line("21.86")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert result.difference.is_zero()
        assert result.lines_with_discrepancies == ()


# ---------------------------------------------------------------------------
# Lines that cannot be recalculated
# ---------------------------------------------------------------------------


class TestUnresolvableLines:
    """Evidence that is insufficient is reported, never substituted."""

    def test_a_fixed_line_is_carried_at_its_recorded_amount(self) -> None:
        """A flat fee cannot be checked against usage, so it is neither verified nor dropped.

        It is excluded from the recalculated total, and saying so is what stops a report from
        implying the whole invoice was verified.
        """
        result = recalculate_invoice(
            invoice(usage_line("21.86"), fixed_line("50.00")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        platform_fee = result.line_for("platform_fee")
        assert platform_fee.status is LineStatus.ACCEPTED_AS_RECORDED
        assert platform_fee.calculated_amount is None
        assert platform_fee.has_discrepancy is False
        assert result.calculated_total == usd("21.86")

    def test_a_metric_with_no_price_term_is_unresolved(self) -> None:
        """No contract term means no agreed rate, so no amount can be produced."""
        result = recalculate_invoice(
            invoice(usage_line("21.86"), usage_line("9.99", metric="egress_gb")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        egress = result.line_for("egress_gb")
        assert egress.status is LineStatus.UNRESOLVED
        assert egress.unresolved_reason is UnresolvedReason.NO_PRICE_TERM
        assert egress.calculated_amount is None
        assert "egress_gb" in result.unresolved_metrics

    def test_an_unresolved_line_makes_the_total_a_lower_bound(self) -> None:
        """The reported difference is the smallest it could be, not its size.

        The unresolved line might have been overstated or understated, so the recalculated
        total covers only what could be checked. ``is_complete`` is what a caller must read
        before quoting the difference as the discrepancy.
        """
        result = recalculate_invoice(
            invoice(usage_line("21.86"), usage_line("9.99", metric="egress_gb")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert result.is_complete is False
        assert result.difference.is_zero()

    def test_a_tier_ladder_that_stops_short_is_unresolved(self) -> None:
        """Usage past a bounded ladder has no agreed rate, so the line is not priced.

        Extrapolating the last tier's rate would produce a confident number that no contract
        agreed to, which is the failure mode this engine exists to avoid.
        """
        bounded = PriceTerm(
            metric_key=CALLS,
            billing_mode=BillingMode.TIERED,
            currency="USD",
            tiers=(Tier(Decimal("1000"), usd("0.05")),),
        )
        result = recalculate_invoice(
            invoice(usage_line("500.00")),
            [bounded],
            [usage_event("1500")],
            contract_external_id=CONTRACT,
        )

        line = result.line_for(CALLS)
        assert line.status is LineStatus.UNRESOLVED
        assert line.unresolved_reason is UnresolvedReason.USAGE_EXCEEDS_TIER_LADDER
        assert line.usage is not None
        assert line.usage.total_quantity == Decimal("1500")
        assert "no agreed rate" in " ".join(line.notes)

    def test_an_unresolved_line_still_reports_the_usage_it_found(self) -> None:
        """The evidence that *was* gathered is reported even though it could not be priced.

        This is what separates "we found nothing" from "we found something we cannot price",
        which are different investigations and different remedies.
        """
        result = recalculate_invoice(
            invoice(usage_line("500.00")),
            [
                PriceTerm(
                    metric_key=CALLS,
                    billing_mode=BillingMode.TIERED,
                    currency="USD",
                    tiers=(Tier(Decimal("1000"), usd("0.05")),),
                )
            ],
            [usage_event("1500")],
            contract_external_id=CONTRACT,
        )

        line = result.line_for(CALLS)
        assert line.usage is not None
        assert line.usage.contributing_event_count == 1
        assert line.trace is None


# ---------------------------------------------------------------------------
# Multi-line invoices
# ---------------------------------------------------------------------------


class TestSeveralLines:
    """Each metric priced on its own term."""

    def test_each_line_uses_its_own_contract_term(self) -> None:
        """Terms are matched by metric, not applied in order.

        Applying terms positionally would silently price storage at the API-call rate
        whenever a contract listed its metrics in a different order.
        """
        events = [
            usage_event("41234"),
            UsageEvent(
                external_id="e2",
                dedupe_key="dedupe-e2",
                metric_key="storage_gb",
                occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
                quantity=Decimal("250"),
                unit="GB",
            ),
        ]
        result = recalculate_invoice(
            invoice(usage_line("21.86"), usage_line("5.75", metric="storage_gb")),
            two_terms(),
            events,
            contract_external_id=CONTRACT,
        )

        # storage: max(250 - 100, 0) = 150 billable * 0.023 = 3.45
        assert result.line_for("storage_gb").calculated_amount == usd("3.45")
        assert result.line_for(CALLS).calculated_amount == usd("21.86")

    def test_several_events_for_one_metric_are_summed_before_pricing(self) -> None:
        """Daily usage accumulates into one billable quantity, then crosses the tiers once.

        Pricing each event separately and adding the amounts would give a different answer
        whenever a tier boundary fell between two days, because the second day's units would
        start again at the top tier.
        """
        events = [
            usage_event("6000", day=1, event_id="e1"),
            usage_event("6000", day=2, event_id="e2"),
        ]
        result = recalculate_invoice(
            invoice(usage_line("8.40")),
            two_terms(),
            events,
            contract_external_id=CONTRACT,
        )

        line = result.line_for(CALLS)
        assert line.usage is not None
        assert line.usage.contributing_event_count == 2
        assert line.usage.total_quantity == Decimal("12000")
        # 12,000 - 10,000 included = 2,000 billable * 0.00070 = 1.40
        assert line.calculated_amount == usd("1.40")

    def test_usage_outside_the_period_is_excluded_from_the_charge(self) -> None:
        """An event from April must not inflate a March invoice."""
        events = [
            usage_event("41234", day=15),
            UsageEvent(
                external_id="e-apr",
                dedupe_key="dedupe-apr",
                metric_key=CALLS,
                occurred_at=datetime(2025, 4, 2, tzinfo=timezone.utc),
                quantity=Decimal("100000"),
                unit="calls",
            ),
        ]
        result = recalculate_invoice(
            invoice(usage_line("21.86")),
            two_terms(),
            events,
            contract_external_id=CONTRACT,
        )

        line = result.line_for(CALLS)
        assert line.usage is not None
        assert line.usage.total_quantity == Decimal("41234")
        assert line.usage.out_of_period_event_ids == ("e-apr",)

    def test_a_duplicated_event_is_counted_once(self) -> None:
        """The same usage delivered twice is charged once.

        This is a documented cause of dispute (§6.3), and it is the reason ``dedupe_key`` is
        part of the domain shape rather than an implementation detail.
        """
        duplicate = UsageEvent(
            external_id="e2",
            dedupe_key="dedupe-e1",
            metric_key=CALLS,
            occurred_at=datetime(2025, 3, 16, tzinfo=timezone.utc),
            quantity=Decimal("41234"),
            unit="calls",
        )
        result = recalculate_invoice(
            invoice(usage_line("43.72")),
            two_terms(),
            [usage_event("41234"), duplicate],
            contract_external_id=CONTRACT,
        )

        line = result.line_for(CALLS)
        assert line.usage is not None
        assert line.usage.total_quantity == Decimal("41234")
        assert line.usage.duplicate_dedupe_keys == ("dedupe-e1",)
        # The invoice charged for both copies; the recalculation did not.
        assert line.calculated_amount == usd("21.86")
        assert line.difference == usd("-21.86")


def two_terms() -> list[PriceTerm]:
    """The two-term contract used by :class:`TestSeveralLines`."""
    return [
        tiered_term(),
        PriceTerm(
            metric_key="storage_gb",
            billing_mode=BillingMode.PER_UNIT,
            currency="USD",
            included_units=Decimal("100"),
            unit_price=usd("0.023"),
        ),
    ]


# ---------------------------------------------------------------------------
# Conservation and determinism
# ---------------------------------------------------------------------------


class TestConservation:
    """The total must be the sum of the lines it summarises."""

    def test_the_calculated_total_equals_the_sum_of_its_lines(self) -> None:
        result = recalculate_invoice(
            invoice(
                usage_line("28.40"), usage_line("5.75", metric="storage_gb"), fixed_line("50.00")
            ),
            two_terms(),
            [
                usage_event("41234"),
                UsageEvent(
                    external_id="e2",
                    dedupe_key="dedupe-e2",
                    metric_key="storage_gb",
                    occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
                    quantity=Decimal("250"),
                    unit="GB",
                ),
            ],
            contract_external_id=CONTRACT,
        )

        summed = Money.zero("USD")
        for line in result.lines:
            if line.calculated_amount is not None:
                summed = summed + line.calculated_amount

        assert result.calculated_total == summed

    def test_the_difference_equals_calculated_minus_recorded(self) -> None:
        result = recalculate_invoice(
            invoice(usage_line("28.40"), usage_line("5.75", metric="storage_gb")),
            two_terms(),
            [
                usage_event("41234"),
                UsageEvent(
                    external_id="e2",
                    dedupe_key="dedupe-e2",
                    metric_key="storage_gb",
                    occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
                    quantity=Decimal("250"),
                    unit="GB",
                ),
            ],
            contract_external_id=CONTRACT,
        )

        assert result.difference == result.calculated_total - result.recorded_total

    def test_the_recorded_total_covers_only_the_lines_that_were_checked(self) -> None:
        """Otherwise a fixed charge would silently enlarge the apparent discrepancy.

        ``recorded_total`` is the recorded side of the same comparison as
        ``calculated_total``, so the two must be drawn over the same set of lines.
        """
        result = recalculate_invoice(
            invoice(usage_line("21.86"), fixed_line("50.00")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert result.recorded_total == usd("21.86")


class TestDeterminism:
    """The same evidence must produce the same result, every time."""

    def test_repeated_calls_return_an_equal_result(self) -> None:
        events = [usage_event("41234")]

        first = recalculate_invoice(
            invoice(usage_line("28.40")), [tiered_term()], events, contract_external_id=CONTRACT
        )
        second = recalculate_invoice(
            invoice(usage_line("28.40")), [tiered_term()], events, contract_external_id=CONTRACT
        )

        assert first == second

    def test_input_order_does_not_change_the_result(self) -> None:
        """Evidence arrives in whatever order the source system produced it.

        A result that depended on arrival order would make a dispute irreproducible, since
        the investigator's query would return the rows in a different order from the
        recalculation's.
        """
        forward = [
            usage_event("6000", day=1, event_id="e1"),
            usage_event("6000", day=2, event_id="e2"),
        ]
        backward = list(reversed(forward))

        first = recalculate_invoice(
            invoice(usage_line("1.40")), [tiered_term()], forward, contract_external_id=CONTRACT
        )
        second = recalculate_invoice(
            invoice(usage_line("1.40")), [tiered_term()], backward, contract_external_id=CONTRACT
        )

        assert first == second

    def test_supplying_terms_for_other_metrics_changes_nothing(self) -> None:
        """An extra term must not disturb a line that does not use it."""
        without = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )
        with_extra = recalculate_invoice(
            invoice(usage_line("28.40")),
            [
                tiered_term(),
                PriceTerm(
                    metric_key="storage_gb",
                    billing_mode=BillingMode.PER_UNIT,
                    currency="USD",
                    unit_price=usd("9.99"),
                ),
            ],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        assert without == with_extra


class TestRefusals:
    """Conditions the engine declines rather than guesses at."""

    def test_a_term_in_another_currency_is_refused(self) -> None:
        """The engine does not convert currency, and must not add unlike amounts (OQ-04)."""
        eur = PriceTerm(
            metric_key=CALLS,
            billing_mode=BillingMode.PER_UNIT,
            currency="EUR",
            unit_price=Money(Decimal("0.05"), "EUR"),
        )
        with pytest.raises(CurrencyMismatchError, match="no implicit currency conversion"):
            recalculate_invoice(
                invoice(usage_line("21.86")),
                [eur],
                [usage_event("41234")],
                contract_external_id=CONTRACT,
            )

    def test_asking_for_a_line_that_is_not_on_the_invoice_raises(self) -> None:
        """Silently returning ``None`` would push the mistake to the caller."""
        result = recalculate_invoice(
            invoice(usage_line("28.40")),
            [tiered_term()],
            [usage_event("41234")],
            contract_external_id=CONTRACT,
        )

        with pytest.raises(KeyError, match="no line for"):
            result.line_for("egress_gb")
