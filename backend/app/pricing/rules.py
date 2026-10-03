"""The pricing rules: how a metered quantity becomes a charge.

Three billing modes exist (:class:`~app.domain.contracts.BillingMode`), and each becomes
one small function here. What they share is more interesting than how they differ:
every mode subtracts the same included-unit allowance before charging anything, so that
step lives in :func:`billable_units` and is called once per charge rather than copied
into three places where it could drift.

The arithmetic contract
-----------------------

**One rounding, at the end.** Per §6.2 an extended amount is exact and only the line
amount is rounded. Each rule therefore returns an exact :class:`Charge`, and the caller
(:mod:`app.pricing.engine`) rounds once. This is not a stylistic preference. A tiered
ladder with three tiers would accumulate three separate rounding errors if each tier were
rounded on the way past, and the total would then depend on how many tiers the contract
happened to have: the same usage would cost more under a longer ladder.

**Quantities are decimals and never clamped.** ``max(quantity - included, 0)`` is the
only floor applied anywhere, and it exists because a free allowance cannot produce a
negative charge. Nothing else is clamped: usage below the allowance yields a genuine zero
rather than a fabricated small charge.

**A rate is never invented.** Where a rule cannot price what it was given, it refuses.
:func:`charge_tiered` raises :class:`LadderShortfall` when usage runs past a ladder with
no unbounded final tier, and the engine reports an unresolved line. Extrapolating the last
rate, or charging the excess at zero, would both produce a confident number no contract
agreed to.

Worked example
--------------
The trace in §6.4, recomputed here. A contract includes 10,000 calls and then charges
$0.00070 each; the customer used 41,234 calls::

    billable_units  = max(41234 - 10000, 0)          = 31234
    tier 1 charges  = min(31234, 50000 - 10000)      = 31234 units
    exact amount    = 31234 * Decimal("0.00070")    = 21.86380
    line amount     = round(21.86380, HALF_UP, 2dp)  = 21.86

At 55,000 calls, tier 1 charges its full 40,000-unit width and tier 2 the remaining
5,000. That is why the ladder is walked rather than indexed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.billing import PriceTerm
from app.domain.contracts import BillingMode
from app.domain.money import Money
from app.pricing.trace import CalculationTrace, TraceBuilder
from app.pricing.usage import UsageSummary

__all__ = [
    "Charge",
    "LadderShortfall",
    "billable_units",
    "charge_commitment",
    "charge_for_term",
    "charge_per_unit",
    "charge_tiered",
    "rule_ref",
]


class LadderShortfall(Exception):
    """Raised when billable units run past the end of a tier ladder.

    Carries the units that *were* priceable so the caller can still report the partial
    work. This is not a contract error: a ladder that simply stops is a real pricing
    configuration, and the engine's answer to it is an unresolved line rather than an
    invented rate.
    """

    def __init__(self, covered_units: Decimal, uncovered_units: Decimal) -> None:
        super().__init__(
            f"tier ladder prices {covered_units} units but {uncovered_units} more are "
            "billable and no rate exists for them"
        )
        self.covered_units = covered_units
        self.uncovered_units = uncovered_units


@dataclass(frozen=True)
class Charge:
    """The outcome of applying one price term to one usage summary.

    ``exact_amount`` is deliberately unrounded. It is what the trace's pre-rounding steps
    add up to, and keeping it lets the engine show both the precise figure and the charged
    figure rather than only the latter.

    ``rate`` is the single rate that applied, or ``None`` when no single rate decided the
    amount: a nil charge, a ladder with several tiers, or a commitment floor.
    """

    #: Exact product of quantity and rate(s), before the line boundary.
    exact_amount: Money
    #: Units left after the included allowance was subtracted.
    billable_units: Decimal
    #: Units the allowance actually absorbed, which may be fewer than it allows.
    included_units_applied: Decimal
    #: The rate that applied, where there is exactly one.
    rate: Money | None
    #: How the amount was arrived at. Never empty.
    trace: CalculationTrace


def rule_ref(contract_external_id: str, metric_key: str) -> str:
    """The ``rule_ref`` shape from §6.4: which contract term produced this amount."""
    return f"contract:{contract_external_id}#term:{metric_key}"


def billable_units(term: PriceTerm, usage: UsageSummary) -> tuple[Decimal, Decimal]:
    """Units left to charge once the included allowance is removed.

    Returns ``(billable, allowance_applied)``. The allowance actually consumed is
    reported separately because a contract with a 10,000-call allowance used in a month
    with 4,000 calls should show 4,000 absorbed and 0 charged. Showing 10,000 instead
    would leave the reader to work out that the remainder was not a negative charge.
    """
    allowance = min(usage.total_quantity, term.included_units)
    return usage.total_quantity - allowance, allowance


def _record_selection(
    builder: TraceBuilder, term: PriceTerm, usage: UsageSummary
) -> tuple[Decimal, Decimal]:
    """Trace how the billable quantity was arrived at; return ``(billable, allowance)``.

    Delegates the allowance arithmetic to :func:`billable_units` rather than repeating it,
    so the traced quantity and the charged quantity cannot drift apart: a disagreement
    between the trace and the amount is the one inconsistency that would make the whole
    explanation untrustworthy.

    Duplicates and out-of-period events get their own steps so that a difference between
    the invoice and the recalculation can be traced to evidence *selection* rather than to
    the pricing rule. A reviewer who sees a shortfall wants to know whether the usage was
    wrong or the rate was wrong, and these steps are what separate the two.
    """
    builder.step(
        "Total metered quantity in period",
        f"sum(usage.quantity WHERE metric_key='{usage.metric_key}' "
        f"AND occurred_at IN [{usage.period.start}, {usage.period.end}))",
        str(usage.total_quantity),
        quantity=str(usage.total_quantity),
        event_count=str(usage.contributing_event_count),
    )
    for duplicate in usage.duplicates:
        builder.step(
            f"Excluded duplicate usage (dedupe_key={duplicate.dedupe_key})",
            f"drop {list(duplicate.dropped_event_ids)}, keep {duplicate.kept_event_id}",
            str(duplicate.dropped_quantity),
            dropped_quantity=str(duplicate.dropped_quantity),
        )
    for excluded in usage.out_of_period:
        builder.step(
            f"Excluded out-of-period usage ({excluded.event_id})",
            f"occurred_at={excluded.occurred_at.isoformat()} outside "
            f"[{usage.period.start}, {usage.period.end})",
            str(Decimal(0)),
        )

    billable, allowance = billable_units(term, usage)
    if term.included_units > 0:
        builder.step(
            "Units absorbed by the included allowance",
            f"min({usage.total_quantity}, {term.included_units})",
            str(allowance),
            allowance=str(term.included_units),
        )
    builder.step(
        "Billable units",
        f"max({usage.total_quantity} - {term.included_units}, 0)",
        str(billable),
    )
    return billable, allowance


def _per_unit_amount(
    term: PriceTerm,
    usage: UsageSummary,
    builder: TraceBuilder,
) -> tuple[Decimal, Decimal, Money, Money]:
    """The flat-rate calculation, tracing into ``builder``.

    Returns ``(billable, allowance, rate, exact_amount)``. Shared by
    :func:`charge_per_unit` and :func:`charge_commitment` so that a committed charge is
    built on exactly the same steps as a metered one, and the only difference a reviewer
    has to understand is the floor applied at the end.
    """
    billable, allowance = _record_selection(builder, term, usage)
    rate = term.rate_for_billable_units
    source = "overage_price" if term.overage_price is not None else "unit_price"
    builder.step(f"Rate applied ({source})", str(rate.amount), str(rate.amount))

    if billable == 0:
        # Zero usage is a real, fully explained outcome: the allowance or the absence of
        # usage absorbed everything, so the charge is nil rather than unknown.
        builder.step("Charge is nil", "billable units = 0", Money.zero(rate.currency).as_string())
        return billable, allowance, rate, Money.zero(rate.currency)

    exact = billable * rate.amount
    builder.step("Exact charge (unrounded)", f"{billable} * {rate.amount}", str(exact))
    return billable, allowance, rate, Money(exact, rate.currency)


def charge_per_unit(term: PriceTerm, usage: UsageSummary, ref: str) -> Charge:
    """A flat rate for every billable unit.

    Examples:
        >>> from datetime import date
        >>> from app.domain.billing import InvoicePeriod
        >>> from app.pricing.usage import UsageSummary
        >>> term = PriceTerm(
        ...     metric_key="api_calls",
        ...     billing_mode=BillingMode.PER_UNIT,
        ...     currency="USD",
        ...     unit_price=Money(Decimal("0.05"), "USD"),
        ... )
        >>> period = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))
        >>> usage = UsageSummary("api_calls", period, Decimal("1200"), ("e1",), (), ())
        >>> charge_per_unit(term, usage, "contract:C1#term:api_calls").exact_amount
        Money(Decimal('60.00'), 'USD')
    """
    builder = TraceBuilder(rule_ref=ref)
    billable, allowance, rate, exact = _per_unit_amount(term, usage, builder)
    return Charge(
        exact_amount=exact,
        billable_units=billable,
        included_units_applied=allowance,
        rate=rate,
        trace=builder.build(),
    )


def charge_tiered(term: PriceTerm, usage: UsageSummary, ref: str) -> Charge:
    """A progressive ladder, each tier charging the units that fell into it at its own rate.

    The ladder is walked rather than indexed because the tiers are progressive: tier *n*
    charges only what is left after tiers 1 to *n-1* have been filled, so the amount
    charged by an early tier depends on the usage in total.

    Raises :class:`LadderShortfall` when billable units pass the end of a ladder with no
    unbounded final tier.
    """
    assert term.tiers is not None  # PriceTerm guarantees a ladder for a TIERED term
    builder = TraceBuilder(rule_ref=ref)
    billable, allowance = _record_selection(builder, term, usage)
    currency = term.tiers[0].unit_price.currency

    if billable == 0:
        builder.step("Charge is nil", "billable units = 0", Money.zero(currency).as_string())
        return Charge(
            exact_amount=Money.zero(currency),
            billable_units=billable,
            included_units_applied=allowance,
            rate=None,
            trace=builder.build(),
        )

    exact = Decimal(0)
    remaining = billable
    lower_bound = Decimal(0)
    charged = Decimal(0)

    for position, tier in enumerate(term.tiers):
        if remaining == 0:
            break
        width = remaining if tier.up_to is None else tier.up_to - lower_bound
        units_here = min(remaining, width)
        if units_here == 0:
            # A tier the usage never reached. Skipping it keeps the trace proportional to
            # the usage rather than to the length of the contract's ladder.
            continue

        amount_here = units_here * tier.unit_price.amount
        span = (
            f"{lower_bound} and above" if tier.up_to is None else f"{lower_bound} to {tier.up_to}"
        )
        # The expression shows the units still to place when this tier was reached, not the
        # overall billable total: after the first tier those differ, and a reader checking
        # the arithmetic against the step above needs the number this step actually used.
        builder.step(
            f"Tier {position + 1} ({span} @ {tier.unit_price.amount})",
            f"min({remaining}, {width}) * {tier.unit_price.amount}",
            str(amount_here),
            units=str(units_here),
        )

        exact += amount_here
        remaining -= units_here
        charged += units_here
        if tier.up_to is not None:
            lower_bound = tier.up_to

    if remaining > 0:
        raise LadderShortfall(covered_units=charged, uncovered_units=remaining)

    builder.step("Exact charge (unrounded)", "sum of tier amounts", str(exact))
    return Charge(
        exact_amount=Money(exact, currency),
        billable_units=billable,
        included_units_applied=allowance,
        rate=None,
        trace=builder.build(),
    )


def charge_commitment(term: PriceTerm, usage: UsageSummary, ref: str) -> Charge:
    """Normal rates, floored at the committed minimum.

    The floor is what distinguishes the mode: a customer who committed to $1,000 of usage
    and consumed $400 still owes $1,000. Written as ``max(metered, floor)`` so that both
    figures appear in the trace, which is what a reviewer needs when the floor is what the
    invoice actually charged.

    A committed minimum is priced with the same per-unit rule as :func:`charge_per_unit`
    and then floored, because ``minimum_commitment`` is a single amount rather than a
    ladder. A commitment whose *rates* were tiered would need the floor applied per tier;
    that is not expressible in the current columns and is recorded as an open assumption.
    """
    assert term.minimum_commitment is not None  # PriceTerm guarantees this for COMMITMENT
    builder = TraceBuilder(rule_ref=ref)
    billable, allowance, rate, metered = _per_unit_amount(term, usage, builder)
    floor = term.minimum_commitment

    builder.step("Committed minimum", f"minimum_commitment = {floor.amount}", floor.as_string())

    if metered >= floor:
        builder.step(
            "Metered charge already meets the commitment",
            f"max({metered.amount}, {floor.amount}) = {metered.amount}",
            metered.as_string(),
        )
        return Charge(
            exact_amount=metered,
            billable_units=billable,
            included_units_applied=allowance,
            rate=rate,
            trace=builder.build(),
        )

    shortfall = floor - metered
    builder.step(
        "Shortfall raised to the committed minimum",
        f"max({metered.amount}, {floor.amount})",
        floor.as_string(),
        shortfall=str(shortfall.amount),
    )
    return Charge(
        exact_amount=floor,
        billable_units=billable,
        included_units_applied=allowance,
        rate=rate,
        trace=builder.build(),
    )


def charge_for_term(term: PriceTerm, usage: UsageSummary, contract_external_id: str) -> Charge:
    """Apply whichever rule ``term.billing_mode`` names.

    The dispatch lives here so that it exists once. :mod:`app.pricing.engine` is the only
    caller, which means adding a :class:`BillingMode` member is one ``match`` arm and one
    test rather than a change spread across every caller.
    """
    ref = rule_ref(contract_external_id, term.metric_key)
    match term.billing_mode:
        case BillingMode.PER_UNIT:
            return charge_per_unit(term, usage, ref)
        case BillingMode.TIERED:
            return charge_tiered(term, usage, ref)
        case BillingMode.COMMITMENT:
            return charge_commitment(term, usage, ref)
