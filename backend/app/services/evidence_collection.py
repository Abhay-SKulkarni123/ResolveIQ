"""Turning source records into the immutable evidence bundle.

docs/SYSTEM_DESIGN.md §3.5 has a ``COLLECTING_EVIDENCE`` stage, and this is it. Its
output is an :class:`~app.domain.evidence.EvidenceBundle`: hashed, ordered, and
detached from the live records it was copied from.

Two rules shape the snapshots.

**Amounts are strings.** ``Money.as_string()`` rather than a JSON number. A JSON number
for 21.86 is parsed as a binary float by most readers, which is the same inexactness the
rest of the system refuses (§4a.1 of the interview guide). The model reads these
snapshots as text; it never parses them into an arithmetic path.

**The natural keys match the engine's own rule references.** Phase 2 stamps every
calculation with ``contract:{contract_id}#term:{metric}`` (§6.4), and §5.1 gives
``contract:CTR-5512#term:api_calls`` as the contract-term evidence key. They are the
same string, so a citation in a finding points at exactly the rule reference in the
trace beside it. That correspondence is the reason a reviewer can go from "the model
cited this term" to "this is the step that used it" without translating anything.

Nothing here reads the dispute text for meaning, and nothing filters it. It is copied
whole (subject to the configured length cap) so that the model's view of what the
customer said is the customer's actual words rather than a summary chosen by the code.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.billing import (
    InvoiceLine,
    Payment,
    PriceTerm,
    RecordedInvoice,
    UsageEvent,
)
from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType
from app.domain.money import Money
from app.pricing.usage import UsageSummary

__all__ = ["build_evidence_bundle", "contract_term_key", "dispute_text_key", "invoice_key"]


def invoice_key(invoice_external_id: str) -> str:
    return f"invoice:{invoice_external_id}"


def invoice_line_key(invoice_external_id: str, metric_key: str) -> str:
    return f"{invoice_key(invoice_external_id)}#line:{metric_key}"


def contract_term_key(contract_external_id: str, metric_key: str) -> str:
    """The evidence key for a contract term.

    Deliberately identical to the ``rule_ref`` the pricing engine writes into its traces
    (§6.4). Same string, two jobs: it names the rule that was applied and it names the
    evidence a finding may cite.
    """
    return f"contract:{contract_external_id}#term:{metric_key}"


def usage_summary_key(metric_key: str, summary: UsageSummary) -> str:
    return f"usage:{metric_key}:{summary.period.start}..{summary.period.end}"


def usage_event_key(event: UsageEvent) -> str:
    return f"usage_event:{event.external_id}"


def payment_key(payment_external_id: str) -> str:
    return f"payment:{payment_external_id}"


def payment_allocation_key(payment_external_id: str, invoice_external_id: str) -> str:
    return f"allocation:{payment_external_id}→{invoice_external_id}"


def dispute_text_key(dispute_external_id: str) -> str:
    return f"dispute_text:{dispute_external_id}"


def _decimal_text(value: Decimal) -> str:
    return str(value)


def _invoice_snapshot(invoice: RecordedInvoice) -> dict[str, object]:
    return {
        "invoice_external_id": invoice.external_id,
        "period_start": str(invoice.period.start),
        "period_end": str(invoice.period.end),
        "currency": invoice.currency,
        "stated_total": invoice.stated_total.as_string(),
        "line_total": invoice.line_total.as_string(),
        "line_count": len(invoice.lines),
    }


def _invoice_line_snapshot(invoice: RecordedInvoice, line: InvoiceLine) -> dict[str, object]:
    return {
        "invoice_external_id": invoice.external_id,
        "metric_key": line.metric_key,
        "line_type": line.line_type.value,
        "recorded_amount": line.recorded_amount.as_string(),
    }


def _usage_summary_snapshot(summary: UsageSummary) -> dict[str, object]:
    return {
        "metric_key": summary.metric_key,
        "period_start": str(summary.period.start),
        "period_end": str(summary.period.end),
        "total_quantity": _decimal_text(summary.total_quantity),
        "contributing_event_count": summary.contributing_event_count,
        "duplicate_dedupe_keys": [d.dedupe_key for d in summary.duplicates],
        "out_of_period_event_ids": [e.event_id for e in summary.out_of_period],
    }


def _usage_event_snapshot(event: UsageEvent) -> dict[str, object]:
    return {
        "event_external_id": event.external_id,
        "metric_key": event.metric_key,
        "occurred_at": event.occurred_at.isoformat(),
        "quantity": _decimal_text(event.quantity),
        "unit": event.unit,
        "dedupe_key": event.dedupe_key,
    }


def _price_term_snapshot(term: PriceTerm) -> dict[str, object]:
    """A contract term, as the rule the model is allowed to point at.

    Rates are rendered as strings and tiers are rendered as plain lists, so the snapshot
    is comparable by eye with the calculation trace that used it. The model may read a
    rate; it may not do arithmetic on one, because the schema it answers has nowhere to
    put a result.
    """
    snapshot: dict[str, object] = {
        "metric_key": term.metric_key,
        "billing_mode": term.billing_mode.value,
        "currency": term.currency,
        "included_units": _decimal_text(term.included_units),
        "unit_price": term.unit_price.as_string() if term.unit_price else None,
        "overage_price": term.overage_price.as_string() if term.overage_price else None,
        "minimum_commitment": (
            term.minimum_commitment.as_string() if term.minimum_commitment else None
        ),
    }
    if term.tiers:
        snapshot["tiers"] = [
            {
                "up_to": _decimal_text(tier.up_to) if tier.up_to is not None else None,
                "unit_price": tier.unit_price.as_string(),
            }
            for tier in term.tiers
        ]
    return snapshot


def _payment_snapshot(payment: Payment) -> dict[str, object]:
    return {
        "payment_external_id": payment.external_id,
        "amount": payment.amount.as_string(),
        "allocation_count": len(payment.allocations),
    }


def _allocation_snapshot(
    payment: Payment, invoice_external_id: str, amount: Money
) -> dict[str, object]:
    return {
        "payment_external_id": payment.external_id,
        "invoice_external_id": invoice_external_id,
        "amount": amount.as_string(),
    }


def build_evidence_bundle(
    *,
    dispute_external_id: str,
    invoice: RecordedInvoice,
    price_terms: list[PriceTerm],
    usage_events: list[UsageEvent],
    usage_summaries: list[UsageSummary],
    payments: list[Payment],
    contract_external_id: str,
    dispute_text: str,
    dispute_text_max_chars: int = 10_000,
) -> EvidenceBundle:
    """Collect every record the investigation may cite into one immutable bundle.

    The dispute text is truncated to ``dispute_text_max_chars`` and the truncation is
    recorded in the snapshot, because a silently shortened complaint would leave a
    reviewer reading a summary of the customer's words while believing they had the
    words. Truncation here is a length guard, not an interpretation of the text.

    Adjustments are deliberately absent: §5.1's closed vocabulary has no adjustment
    type, so no finding may cite a credit. They still affect the outcome, through the
    balance the caller reconciles, which is where an adjustment belongs.
    """
    items: list[EvidenceItem] = [
        EvidenceItem.create(
            invoice_key(invoice.external_id), EvidenceType.INVOICE, _invoice_snapshot(invoice)
        )
    ]

    for line in invoice.lines:
        items.append(
            EvidenceItem.create(
                invoice_line_key(invoice.external_id, line.metric_key),
                EvidenceType.INVOICE_LINE,
                _invoice_line_snapshot(invoice, line),
            )
        )

    for summary in usage_summaries:
        items.append(
            EvidenceItem.create(
                usage_summary_key(summary.metric_key, summary),
                EvidenceType.USAGE_SUMMARY,
                _usage_summary_snapshot(summary),
            )
        )

    for event in usage_events:
        items.append(
            EvidenceItem.create(
                usage_event_key(event), EvidenceType.USAGE_EVENT, _usage_event_snapshot(event)
            )
        )

    for term in price_terms:
        items.append(
            EvidenceItem.create(
                contract_term_key(contract_external_id, term.metric_key),
                EvidenceType.CONTRACT_TERM,
                _price_term_snapshot(term),
            )
        )

    for payment in payments:
        items.append(
            EvidenceItem.create(
                payment_key(payment.external_id), EvidenceType.PAYMENT, _payment_snapshot(payment)
            )
        )
        for allocation in payment.allocations:
            if allocation.invoice_external_id != invoice.external_id:
                continue
            items.append(
                EvidenceItem.create(
                    payment_allocation_key(payment.external_id, invoice.external_id),
                    EvidenceType.PAYMENT_ALLOCATION,
                    _allocation_snapshot(payment, invoice.external_id, allocation.amount),
                )
            )

    capped = dispute_text[:dispute_text_max_chars]
    items.append(
        EvidenceItem.create(
            dispute_text_key(dispute_external_id),
            EvidenceType.DISPUTE_TEXT,
            {
                "dispute_external_id": dispute_external_id,
                "invoice_external_id": invoice.external_id,
                "text": capped,
                "character_count": len(dispute_text),
                "truncated": len(dispute_text) > dispute_text_max_chars,
            },
        )
    )

    return EvidenceBundle.of(items)
