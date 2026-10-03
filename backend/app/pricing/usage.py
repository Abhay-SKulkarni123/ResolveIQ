"""Selecting and totalling the usage evidence a charge is based on.

A recalculation is only as trustworthy as the usage behind it, so this module is where
the three ways usage evidence goes wrong are handled, each explicitly rather than by
accident:

**Duplicate events.** The source system stamps every event with a ``dedupe_key`` that is
unique by contract (§3.2). Two events carrying one key are the same usage recorded
twice, which is a documented cause of a dispute (§6.3, ``DUPLICATED_USAGE``). Each key is
counted once. Which of the two copies is kept is decided by a stated rule rather than by
input order, so the recalculation does not depend on how a database returned the rows.

**Out-of-period events.** Only events inside the invoice's half-open period contribute. An
event outside it is excluded and reported. Excluding it silently would be the worst
outcome: the total would look authoritative while quietly disagreeing with the invoice's
own dates.

**Missing events.** Nothing here can detect absence. If the usage set is empty the
quantity is zero, and zero is a real answer that means "no metered usage in this period".
It is not evidence that usage was lost, and the engine does not pretend otherwise. An
invoice whose lines imply usage where the evidence has none shows up as a discrepancy,
which is the correct place for that judgement to be made.

Determinism
-----------
:func:`summarise_usage` sorts its events by ``(occurred_at, external_id)`` before doing
anything else. Every downstream result — the dedupe winner, the order of reported
duplicates, the order of reported out-of-period events — derives from that one ordering,
so two runs over the same events in a different input order produce identical output.
That is a requirement of the engine, not a nicety: a dispute result that changed when a
query plan changed would be indefensible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.billing import InvoicePeriod, UsageEvent

__all__ = ["DuplicateUsage", "OutOfPeriodUsage", "UsageSummary", "summarise_usage"]


@dataclass(frozen=True)
class DuplicateUsage:
    """A ``dedupe_key`` that appeared on more than one event, and what was dropped."""

    dedupe_key: str
    kept_event_id: str
    dropped_event_ids: tuple[str, ...]
    dropped_quantity: Decimal


@dataclass(frozen=True)
class OutOfPeriodUsage:
    """An event excluded because it falls outside the invoice period."""

    event_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class UsageSummary:
    """The usage evidence that applies to one metric over one period.

    ``total_quantity`` is the exact sum of the contributing events. It is *not*
    rounded and *not* clamped: a negative total is impossible because
    :class:`~app.domain.billing.UsageEvent` rejects a negative quantity at construction,
    and this module does no arithmetic that could produce one.
    """

    metric_key: str
    period: InvoicePeriod
    total_quantity: Decimal
    contributing_event_ids: tuple[str, ...]
    duplicates: tuple[DuplicateUsage, ...]
    out_of_period: tuple[OutOfPeriodUsage, ...]

    @property
    def contributing_event_count(self) -> int:
        return len(self.contributing_event_ids)

    @property
    def has_anomalies(self) -> bool:
        """Whether anything was excluded, so the trace can say so."""
        return bool(self.duplicates or self.out_of_period)


def _sort_key(event: UsageEvent) -> tuple[datetime, str]:
    return (event.occurred_at, event.external_id)


def summarise_usage(
    metric_key: str,
    events: list[UsageEvent],
    period: InvoicePeriod,
) -> UsageSummary:
    """Total the usage for one metric inside one period.

    ``events`` may contain every metric's events; those for other metrics are ignored, so
    a caller does not have to pre-filter and cannot accidentally filter with the wrong
    key.

    Examples:
        >>> from datetime import date, datetime
        >>> period = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))
        >>> events = [
        ...     UsageEvent("e1", "k1", "api_calls", datetime(2025, 3, 1, 9), Decimal("100"), "calls"),
        ...     UsageEvent("e2", "k2", "api_calls", datetime(2025, 3, 15), Decimal("50"), "calls"),
        ... ]
        >>> summary = summarise_usage("api_calls", events, period)
        >>> summary.total_quantity
        Decimal('150')
    """
    for_metric = sorted((e for e in events if e.metric_key == metric_key), key=_sort_key)

    in_period: list[UsageEvent] = []
    out_of_period: list[OutOfPeriodUsage] = []
    for event in for_metric:
        if period.contains(event.occurred_at):
            in_period.append(event)
        else:
            out_of_period.append(
                OutOfPeriodUsage(event_id=event.external_id, occurred_at=event.occurred_at)
            )

    kept, duplicates = _deduplicate(in_period)

    total = Decimal(0)
    for event in kept:
        total += event.quantity

    return UsageSummary(
        metric_key=metric_key,
        period=period,
        total_quantity=total,
        contributing_event_ids=tuple(event.external_id for event in kept),
        duplicates=tuple(duplicates),
        out_of_period=tuple(out_of_period),
    )


def _deduplicate(
    events: list[UsageEvent],
) -> tuple[list[UsageEvent], list[DuplicateUsage]]:
    """Keep the first event per ``dedupe_key`` and report the rest.

    ``events`` must already be sorted by :func:`_sort_key`, which makes "first" mean
    "earliest occurrence, then lowest identifier" rather than "whichever the database
    returned first". The returned duplicates are ordered by ``dedupe_key`` so the trace
    is byte-identical across runs.
    """
    kept: list[UsageEvent] = []
    duplicates: list[DuplicateUsage] = []
    seen: dict[str, UsageEvent] = {}

    for event in events:
        first = seen.get(event.dedupe_key)
        if first is None:
            seen[event.dedupe_key] = event
            kept.append(event)
            continue
        duplicates.append(
            DuplicateUsage(
                dedupe_key=event.dedupe_key,
                kept_event_id=first.external_id,
                dropped_event_ids=(event.external_id,),
                dropped_quantity=event.quantity,
            )
        )

    # Merge repeats of the same key so one key yields one reported duplicate, however
    # many extra copies arrived. The dropped identifiers stay in arrival order, which is
    # the sorted event order and therefore still deterministic.
    merged: dict[str, DuplicateUsage] = {}
    for duplicate in duplicates:
        existing = merged.get(duplicate.dedupe_key)
        if existing is None:
            merged[duplicate.dedupe_key] = duplicate
            continue
        merged[duplicate.dedupe_key] = DuplicateUsage(
            dedupe_key=duplicate.dedupe_key,
            kept_event_id=existing.kept_event_id,
            dropped_event_ids=existing.dropped_event_ids + duplicate.dropped_event_ids,
            dropped_quantity=existing.dropped_quantity + duplicate.dropped_quantity,
        )

    return kept, [merged[key] for key in sorted(merged)]
