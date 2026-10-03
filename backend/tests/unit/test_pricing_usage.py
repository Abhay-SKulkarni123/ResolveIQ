"""Unit tests for usage selection: period filtering, deduplication, exact totals.

These are the tests that decide what a charge is based on. If this module selects the
wrong events, every amount downstream is wrong in a way that looks plausible, so the
cases here are about selection rather than about arithmetic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.billing import BillingDataError, InvoicePeriod, UsageEvent
from app.pricing.usage import summarise_usage

MARCH = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))
CALLS = "api_calls"


def utc(*args: object) -> datetime:
    """A timezone-aware timestamp, which is the only kind UsageEvent accepts.

    Written out rather than sprinkling ``tzinfo=timezone.utc`` through every literal so the
    tests read as instants and the zone stays a single deliberate decision.
    """
    return datetime(*args, tzinfo=timezone.utc)  # type: ignore[arg-type]


def event(
    event_id: str,
    dedupe_key: str,
    quantity: str,
    *,
    day: int = 15,
    hour: int = 12,
    metric: str = CALLS,
    unit: str = "calls",
) -> UsageEvent:
    return UsageEvent(
        external_id=event_id,
        dedupe_key=dedupe_key,
        metric_key=metric,
        occurred_at=utc(2025, 3, day, hour),
        quantity=Decimal(quantity),
        unit=unit,
    )


# ---------------------------------------------------------------------------
# Totalling
# ---------------------------------------------------------------------------


def test_a_single_event_is_its_own_total() -> None:
    summary = summarise_usage(CALLS, [event("e1", "k1", "41234")], MARCH)

    assert summary.total_quantity == Decimal("41234")
    assert summary.contributing_event_ids == ("e1",)
    assert summary.contributing_event_count == 1


def test_several_events_are_summed_exactly() -> None:
    events = [
        event("e1", "k1", "0.1", day=1),
        event("e2", "k2", "0.2", day=10),
        event("e3", "k3", "2.25", day=20),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    # 0.1 + 0.2 is exactly 0.3 in decimal arithmetic. Summed as binary floats the same
    # three values give 2.5500000000000003, which is the entire reason NEP-01 exists.
    assert summary.total_quantity == Decimal("0.3") + Decimal("2.25")
    assert summary.total_quantity == Decimal("2.55")
    assert summary.total_quantity + Decimal("0") == Decimal("2.55")


def test_a_fractional_quantity_is_not_truncated() -> None:
    summary = summarise_usage(CALLS, [event("e1", "k1", "0.5")], MARCH)

    assert summary.total_quantity == Decimal("0.5")


def test_events_for_other_metrics_are_ignored() -> None:
    events = [
        event("e1", "k1", "100"),
        event("e2", "k2", "9999", metric="egress_gb"),
        event("e3", "k3", "50", metric="seats"),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("100")
    assert summary.contributing_event_ids == ("e1",)


def test_no_events_means_zero_usage_rather_than_an_error() -> None:
    """Zero is a real answer meaning "nothing metered in this period".

    It is not evidence that usage was lost, and the engine must not invent a quantity
    to fill the gap.
    """
    summary = summarise_usage(CALLS, [], MARCH)

    assert summary.total_quantity == Decimal(0)
    assert summary.contributing_event_ids == ()
    assert summary.has_anomalies is False


# ---------------------------------------------------------------------------
# Period filtering
# ---------------------------------------------------------------------------


def test_the_first_day_of_the_period_is_inside_it() -> None:
    summary = summarise_usage(CALLS, [event("e1", "k1", "10", day=1)], MARCH)

    assert summary.total_quantity == Decimal("10")
    assert summary.out_of_period == ()


def test_the_last_day_of_the_period_is_inside_it() -> None:
    summary = summarise_usage(CALLS, [event("e1", "k1", "10", day=31)], MARCH)

    assert summary.total_quantity == Decimal("10")


def test_the_day_after_the_period_is_outside_it() -> None:
    """The period is half-open, so 1 April is the first day of the next invoice."""
    boundary = UsageEvent(
        external_id="e1",
        dedupe_key="k1",
        metric_key=CALLS,
        occurred_at=utc(2025, 4, 1, 0, 0),
        quantity=Decimal("5000"),
        unit="calls",
    )

    summary = summarise_usage(CALLS, [boundary], MARCH)

    assert summary.total_quantity == Decimal(0)
    assert [o.event_id for o in summary.out_of_period] == ["e1"]


def test_the_day_before_the_period_is_outside_it() -> None:
    before = UsageEvent(
        external_id="e1",
        dedupe_key="k1",
        metric_key=CALLS,
        occurred_at=utc(2025, 2, 28, 23, 59),
        quantity=Decimal("5000"),
        unit="calls",
    )

    summary = summarise_usage(CALLS, [before], MARCH)

    assert summary.total_quantity == Decimal(0)
    assert [o.event_id for o in summary.out_of_period] == ["e1"]


def test_out_of_period_events_are_reported_rather_than_dropped_silently() -> None:
    events = [
        event("in1", "k1", "100", day=10),
        UsageEvent(
            external_id="out1",
            dedupe_key="k2",
            metric_key=CALLS,
            occurred_at=utc(2025, 5, 2),
            quantity=Decimal("700"),
            unit="calls",
        ),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("100")
    assert [o.event_id for o in summary.out_of_period] == ["out1"]
    assert summary.has_anomalies is True


def test_mixed_period_events_produce_one_total_and_one_exclusion() -> None:
    events = [
        event("in1", "k1", "100", day=5),
        event("in2", "k2", "200", day=25),
        UsageEvent(
            external_id="out1",
            dedupe_key="k3",
            metric_key=CALLS,
            occurred_at=utc(2024, 12, 31),
            quantity=Decimal("9999"),
            unit="calls",
        ),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("300")
    assert len(summary.out_of_period) == 1


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_two_events_with_one_dedupe_key_are_counted_once() -> None:
    events = [
        event("e1", "same-key", "100", day=10),
        event("e2", "same-key", "100", day=10),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("100")
    assert summary.contributing_event_ids == ("e1",)


def test_the_earliest_copy_of_a_duplicate_is_the_one_kept() -> None:
    events = [
        event("late", "same-key", "100", day=20),
        event("early", "same-key", "100", day=5),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.contributing_event_ids == ("early",)
    assert len(summary.duplicates) == 1
    assert summary.duplicates[0].kept_event_id == "early"
    assert summary.duplicates[0].dropped_event_ids == ("late",)
    assert summary.duplicates[0].dropped_quantity == Decimal("100")


def test_ties_on_the_timestamp_are_broken_by_identifier() -> None:
    """Two copies stamped at the same instant must still resolve the same way every run."""
    events = [
        event("b-event", "same-key", "10", day=10, hour=9),
        event("a-event", "same-key", "10", day=10, hour=9),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.contributing_event_ids == ("a-event",)


def test_three_copies_of_one_key_are_reported_as_one_duplicate() -> None:
    events = [
        event("e1", "same-key", "10", day=1),
        event("e2", "same-key", "10", day=2),
        event("e3", "same-key", "10", day=3),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("10")
    assert len(summary.duplicates) == 1
    assert summary.duplicates[0].dropped_event_ids == ("e2", "e3")
    assert summary.duplicates[0].dropped_quantity == Decimal("20")


def test_distinct_keys_are_not_treated_as_duplicates() -> None:
    events = [
        event("e1", "key-a", "100", day=1),
        event("e2", "key-b", "200", day=2),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert summary.total_quantity == Decimal("300")
    assert summary.duplicates == ()


def test_duplicates_are_reported_in_key_order() -> None:
    events = [
        event("e1", "key-z", "10", day=1),
        event("e2", "key-z", "10", day=2),
        event("e3", "key-a", "10", day=3),
        event("e4", "key-a", "10", day=4),
    ]

    summary = summarise_usage(CALLS, events, MARCH)

    assert [d.dedupe_key for d in summary.duplicates] == ["key-a", "key-z"]


def test_a_duplicate_outside_the_period_is_excluded_not_deduplicated() -> None:
    """Deduplication applies within the period.

    An event outside the period is excluded first, so it neither contributes to the
    total nor appears as a dropped duplicate.
    """
    inside = event("in1", "shared", "100", day=10)
    outside = UsageEvent(
        external_id="out1",
        dedupe_key="shared",
        metric_key=CALLS,
        occurred_at=utc(2025, 6, 1),
        quantity=Decimal("100"),
        unit="calls",
    )

    summary = summarise_usage(CALLS, [outside, inside], MARCH)

    assert summary.total_quantity == Decimal("100")
    assert summary.duplicates == ()
    assert [o.event_id for o in summary.out_of_period] == ["out1"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_input_order_does_not_change_the_result() -> None:
    events = [
        event("e1", "key-a", "10", day=3),
        event("e2", "key-b", "20", day=1),
        event("e3", "key-a", "10", day=2),
        event("e4", "key-c", "30", day=9),
    ]

    forwards = summarise_usage(CALLS, events, MARCH)
    backwards = summarise_usage(CALLS, list(reversed(events)), MARCH)

    assert forwards == backwards


def test_repeated_calls_return_an_equal_summary() -> None:
    events = [event("e1", "key-a", "10"), event("e2", "key-b", "20")]

    assert summarise_usage(CALLS, events, MARCH) == summarise_usage(CALLS, events, MARCH)


# ---------------------------------------------------------------------------
# Input validation, which protects the arithmetic above
# ---------------------------------------------------------------------------


def test_a_negative_quantity_is_rejected() -> None:
    with pytest.raises(BillingDataError, match="must not be negative"):
        event("e1", "k1", "-5")


def test_a_float_quantity_is_rejected() -> None:
    """INV-01 extends to metered values, not only to money.

    A float cannot represent 0.1 GB exactly, so a usage quantity built from one would
    put a binary approximation into an otherwise exact sum.
    """
    with pytest.raises(BillingDataError, match="exact Decimal"):
        UsageEvent(
            external_id="e1",
            dedupe_key="k1",
            metric_key=CALLS,
            occurred_at=utc(2025, 3, 15),
            quantity=1.5,  # type: ignore[arg-type]
            unit="calls",
        )


def test_a_bool_quantity_is_rejected() -> None:
    with pytest.raises(BillingDataError, match="exact Decimal"):
        UsageEvent(
            external_id="e1",
            dedupe_key="k1",
            metric_key=CALLS,
            occurred_at=utc(2025, 3, 15),
            quantity=True,  # type: ignore[arg-type]
            unit="calls",
        )


def test_a_non_finite_quantity_is_rejected() -> None:
    with pytest.raises(BillingDataError, match="finite"):
        event("e1", "k1", "NaN")


@pytest.mark.parametrize("field_name", ["external_id", "dedupe_key", "metric_key", "unit"])
def test_blank_identifiers_are_rejected(field_name: str) -> None:
    kwargs = {
        "external_id": "e1",
        "dedupe_key": "k1",
        "metric_key": CALLS,
        "unit": "calls",
    }
    kwargs[field_name] = "   "

    with pytest.raises(BillingDataError, match=field_name):
        UsageEvent(
            occurred_at=utc(2025, 3, 15),
            quantity=Decimal("1"),
            **kwargs,  # type: ignore[arg-type]
        )


def test_an_empty_period_is_rejected() -> None:
    with pytest.raises(BillingDataError, match="start < end"):
        InvoicePeriod(date(2025, 3, 1), date(2025, 3, 1))


def test_a_reversed_period_is_rejected() -> None:
    with pytest.raises(BillingDataError, match="start < end"):
        InvoicePeriod(date(2025, 4, 1), date(2025, 3, 1))
