"""Recalculating an invoice: the deterministic comparison a dispute turns on.

The flow, in four steps:

1. **Select.** For each line, gather the usage evidence for that metric inside the
   invoice period (:mod:`app.pricing.usage`).
2. **Price.** Apply the contract's price term for that metric (:mod:`app.pricing.rules`).
3. **Round once.** At the line boundary, and nowhere else (:mod:`app.pricing.rounding`).
4. **Compare.** Put the recalculated amount beside the recorded one.

What this module deliberately does not do
-----------------------------------------
It does not decide *why* two figures differ, and it does not decide what the customer still
owes. The first belongs to the investigation; the second belongs to
:mod:`app.pricing.reconciliation`. Both are separate functions in separate modules, because
a recalculation is a statement about arithmetic and a balance is a statement about account
state. Merging them would make the first impossible to trust without the second.

Determinism
-----------
The engine is a pure function of its arguments. It reads no clock, opens no connection,
consults no random source and iterates nothing in an unordered way: usage selection sorts
its events, lines are processed in invoice order, and price terms are looked up by key.
The same evidence therefore yields the same figures, in the same order, with the same
trace, on every run. That is what makes a dispute result defensible rather than merely
reproducible.
"""

from __future__ import annotations

from app.domain.billing import (
    InvoiceLine,
    InvoicePeriod,
    LineType,
    PriceTerm,
    RecordedInvoice,
    UsageEvent,
)
from app.domain.money import CurrencyMismatchError, Money
from app.pricing.results import (
    InvoiceRecalculation,
    LineRecalculation,
    LineStatus,
    UnresolvedReason,
    UsageQuantityBreakdown,
)
from app.pricing.rounding import LINE_AMOUNT_MODE, round_invoice_total, round_line_amount
from app.pricing.rules import LadderShortfall, billable_units, charge_for_term
from app.pricing.usage import UsageSummary, summarise_usage

__all__ = ["recalculate_invoice"]


def recalculate_invoice(
    invoice: RecordedInvoice,
    price_terms: list[PriceTerm],
    usage_events: list[UsageEvent],
    *,
    contract_external_id: str,
) -> InvoiceRecalculation:
    """Recalculate every line of ``invoice`` and compare against what was recorded.

    ``price_terms`` are the terms of the contract version in force during the invoice
    period. Supplying the wrong contract version is the most likely caller error, and it
    produces a confident wrong answer rather than a failure, so ``contract_external_id`` is
    recorded in every trace: the rule reference on a result is how a reviewer checks that
    the terms applied were the ones in force.

    Raises:
        CurrencyMismatchError: if any supplied term prices a metric in a currency other
            than the invoice's. ResolveIQ performs no FX conversion (OQ-04), so this is
            refused rather than converted.
    """
    terms_by_metric = _index_terms(price_terms, invoice.currency)

    lines = tuple(
        _recalculate_line(line, terms_by_metric, usage_events, invoice.period, contract_external_id)
        for line in invoice.lines
    )
    return _summarise(invoice, lines)


def _index_terms(price_terms: list[PriceTerm], currency: str) -> dict[str, PriceTerm]:
    """Price terms by metric, after checking they belong to this invoice's currency.

    A term in another currency is a data defect rather than a missing rule, and it is
    caught here so that every line fails the same way instead of one line raising from
    inside a multiplication while the rest quietly succeed.
    """
    indexed: dict[str, PriceTerm] = {}
    for term in price_terms:
        if term.currency != currency:
            raise CurrencyMismatchError(
                f"price term for {term.metric_key!r} is in {term.currency} but the invoice "
                f"is in {currency}; ResolveIQ performs no implicit currency conversion "
                "(OQ-04)"
            )
        indexed[term.metric_key] = term
    return indexed


def _recalculate_line(
    line: InvoiceLine,
    terms_by_metric: dict[str, PriceTerm],
    usage_events: list[UsageEvent],
    period: InvoicePeriod,
    contract_external_id: str,
) -> LineRecalculation:
    """Recalculate one line, or record precisely why it could not be.

    A ``FIXED`` line is settled before any usage is gathered: no evidence could confirm a
    flat fee, so looking for some would waste work and invite a fabricated basis for it.
    """
    if line.line_type is LineType.FIXED:
        return LineRecalculation(
            metric_key=line.metric_key,
            line_type=line.line_type,
            status=LineStatus.ACCEPTED_AS_RECORDED,
            recorded_amount=line.recorded_amount,
        )

    term = terms_by_metric.get(line.metric_key)
    if term is None:
        return _unresolved(
            line,
            UnresolvedReason.NO_PRICE_TERM,
            usage=None,
            notes=(
                f"no price term on contract {contract_external_id} prices "
                f"{line.metric_key!r}, so the recorded amount cannot be checked",
            ),
        )

    summary = summarise_usage(line.metric_key, usage_events, period)
    breakdown = _usage_breakdown(term, summary)

    try:
        charge = charge_for_term(term, summary, contract_external_id)
    except LadderShortfall as shortfall:
        # The ladder priced part of the usage and then ran out. The evidence is incomplete
        # rather than wrong, so this is reported on the line instead of raised: the caller
        # still gets every other line back, plus the units the ladder did cover.
        return _unresolved(
            line,
            UnresolvedReason.USAGE_EXCEEDS_TIER_LADDER,
            usage=breakdown,
            notes=(
                f"the tier ladder prices {shortfall.covered_units} units but "
                f"{shortfall.uncovered_units} more are billable with no agreed rate, "
                "so no amount was calculated",
            ),
        )

    calculated = round_line_amount(charge.exact_amount)
    return LineRecalculation(
        metric_key=line.metric_key,
        line_type=line.line_type,
        status=LineStatus.RECALCULATED,
        recorded_amount=line.recorded_amount,
        rule_ref=charge.trace.rule_ref,
        rate=charge.rate,
        usage=breakdown,
        exact_amount=charge.exact_amount,
        rounding=LINE_AMOUNT_MODE,
        calculated_amount=calculated,
        difference=calculated - line.recorded_amount,
        trace=charge.trace,
    )


def _usage_breakdown(term: PriceTerm, summary: UsageSummary) -> UsageQuantityBreakdown:
    """The quantity behind a charge, split into consumed, absorbed and chargeable.

    Derived from the term and the summary rather than from the resulting charge, so that
    it is available on the ladder-shortfall path too, where no charge exists to read it
    from.
    """
    billable, absorbed = billable_units(term, summary)
    return UsageQuantityBreakdown(
        total_quantity=summary.total_quantity,
        included_units=absorbed,
        billable_units=billable,
        contributing_event_count=summary.contributing_event_count,
        duplicate_dedupe_keys=tuple(d.dedupe_key for d in summary.duplicates),
        out_of_period_event_ids=tuple(o.event_id for o in summary.out_of_period),
    )


def _unresolved(
    line: InvoiceLine,
    reason: UnresolvedReason,
    *,
    usage: UsageQuantityBreakdown | None,
    notes: tuple[str, ...],
) -> LineRecalculation:
    """A line that could not be recalculated, with the reason recorded.

    No amount is attached. A zero here would be indistinguishable from a genuinely free
    charge, and a trace of invented steps would be worse than no trace at all.
    """
    return LineRecalculation(
        metric_key=line.metric_key,
        line_type=line.line_type,
        status=LineStatus.UNRESOLVED,
        recorded_amount=line.recorded_amount,
        usage=usage,
        unresolved_reason=reason,
        notes=notes,
    )


def _summarise(
    invoice: RecordedInvoice, lines: tuple[LineRecalculation, ...]
) -> InvoiceRecalculation:
    """Total the lines and state plainly how complete the result is.

    Conservation: ``calculated_total`` is built by summing the same per-line amounts that
    appear on the lines, and ``difference`` follows from those totals rather than being
    recomputed per line. A test asserts the identity holds, which is what stops a later
    change to the totalling from quietly disagreeing with the lines it summarises.
    """
    currency = invoice.currency
    calculated = Money.zero(currency)
    recorded_for_resolved = Money.zero(currency)

    for line in lines:
        if line.calculated_amount is not None:
            calculated = calculated + line.calculated_amount
            recorded_for_resolved = recorded_for_resolved + line.recorded_amount

    calculated_total = round_invoice_total(calculated)
    return InvoiceRecalculation(
        invoice_external_id=invoice.external_id,
        period=invoice.period,
        currency=currency,
        lines=lines,
        recorded_total=recorded_for_resolved,
        calculated_total=calculated_total,
        difference=calculated_total - recorded_for_resolved,
        unresolved_metrics=tuple(
            line.metric_key for line in lines if line.status is LineStatus.UNRESOLVED
        ),
    )
