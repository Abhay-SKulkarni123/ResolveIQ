"""Turning ingest payloads into domain value objects, and back again.

A dispute has to be investigable more than once. Phase 3 took typed
``RecordedInvoice``/``PriceTerm``/``Payment`` objects that only ever existed in
memory, so storing only the evidence snapshots would leave a reopened case
impossible to re-run. This module is the serialisation boundary that fixes that:
the ingest payload is stored once on the case, and every later investigation
rebuilds the typed objects from it through the *same* domain constructors.

Why the evidence snapshots are not used for this
------------------------------------------------
They would be the obvious choice and they are lossy. ``_invoice_snapshot`` in
:mod:`app.services.evidence_collection` records what the model may read about an
invoice -- a stated total, a line count -- not everything the pricing engine needs.
``_usage_summary_snapshot`` records duplicate dedupe keys but not which events
contributed. A round trip through those snapshots would therefore quietly change
the numbers on the second run, which is the exact failure ADR-006 exists to
prevent: two runs a week apart must agree.

Keeping both is not redundancy. The source document is the input of record; the
snapshots are the citable, hashed view of it. Both are stored, and neither is
derived from the other at read time, so neither can be quietly wrong.

Round-tripping is tested rather than assumed
--------------------------------------------
``tests/unit/test_source_document.py`` asserts that every record type survives
``to_source_document`` / ``from_source_document`` with equal values, and that a
rebuilt invoice recalculates to the same total. A field added to a domain dataclass
without a codec entry fails that test instead of silently becoming ``None`` on the
second investigation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from app.domain.billing import (
    Adjustment,
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
from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType
from app.domain.json_frozen import thaw_json
from app.domain.money import Money
from app.pricing.usage import summarise_usage

__all__ = [
    "SOURCE_DOCUMENT_VERSION",
    "SourceDocumentError",
    "attach_snapshot",
    "build_bundle_from_document",
    "from_source_document",
    "merge_records",
    "to_source_document",
]

#: Bumped when the document's shape changes incompatibly. A stored document with a
#: different version is rejected rather than misread, because a wrong-but-parseable
#: invoice is worse than an error.
SOURCE_DOCUMENT_VERSION: Final[int] = 1

_MISSING = object()


class SourceDocumentError(ValueError):
    """The stored ingest payload is absent, malformed or the wrong version.

    Separate from ``BillingDataError`` so the API can map it to 422 with a message
    about the *case* rather than about a field on an invoice.
    """


# ----------------------------------------------------------------------
# encoding
# ----------------------------------------------------------------------


def _money(value: Money | None) -> dict[str, str] | None:
    return None if value is None else {"amount": value.as_string(), "currency": value.currency}


def _money_from(document: dict[str, Any], key: str) -> Money | None:
    raw = document.get(key)
    if raw is None:
        return None
    if not isinstance(raw, dict) or "amount" not in raw or "currency" not in raw:
        raise SourceDocumentError(f"{key!r} is not a money object: {raw!r}")
    return Money(Decimal(str(raw["amount"])), str(raw["currency"]))


def _period(period: InvoicePeriod) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _period_from(raw: Any, where: str) -> InvoicePeriod:
    if not isinstance(raw, dict) or "start" not in raw or "end" not in raw:
        raise SourceDocumentError(f"{where} is not a period: {raw!r}")
    try:
        return InvoicePeriod(start=date.fromisoformat(str(raw["start"])), end=date.fromisoformat(str(raw["end"])))
    except ValueError as exc:
        raise SourceDocumentError(f"{where} has an unparseable date: {raw!r}") from exc


def to_source_document(
    *,
    invoice: RecordedInvoice,
    price_terms: list[PriceTerm],
    usage_events: list[UsageEvent],
    payments: list[Payment],
    adjustments: list[Adjustment],
    dispute_text: str = "",
    dispute_text_max_chars: int = 10_000,
) -> dict[str, Any]:
    """Serialise the ingest payload.

    Amounts become ``{"amount": "28.40", "currency": "USD"}`` objects rather than
    bare strings so that the currency of every number travels with it. A bare
    amount in a stored document is an amount whose currency has to be guessed from
    a sibling field, and guessing is how the wrong-currency total gets displayed.
    """
    return {
        "schema_version": SOURCE_DOCUMENT_VERSION,
        "present": True,
        # The customer's words are stored, not merely hashed. Without them the bundle
        # could not be rebuilt from the document, and a rebuild is what makes the
        # evidence fingerprint a function of one value rather than of two.
        "dispute_text": dispute_text[:dispute_text_max_chars],
        "invoice": _encoded_invoice(invoice),
        "price_terms": _encoded_terms(price_terms),
        "usage_events": _encoded_usage_events(usage_events),
        "payments": _encoded_payments(payments),
        # Adjustments are stored here even though Phase 3's closed evidence
        # vocabulary has no adjustment type and no finding may cite a credit. They
        # still belong to the case: they change the balance the engine reconciles,
        # and a reviewer who cannot see that a credit was already applied will read
        # an outstanding balance as money still owed. See case_models.py on why
        # evidence snapshots cannot carry them.
        "adjustments": _encoded_adjustments(adjustments),
    }


def _encoded_invoice(invoice: RecordedInvoice) -> dict[str, Any]:
    return {
        "external_id": invoice.external_id,
        "period": _period(invoice.period),
        "currency": invoice.currency,
        "stated_total": _money(invoice.stated_total),
        "lines": [
            {
                "metric_key": line.metric_key,
                "line_type": line.line_type.value,
                "recorded_amount": _money(line.recorded_amount),
            }
            for line in invoice.lines
        ],
    }


def _encoded_terms(price_terms: list[PriceTerm]) -> list[dict[str, Any]]:
    return [
        {
            "metric_key": term.metric_key,
            "billing_mode": term.billing_mode.value,
            "currency": term.currency,
            "unit_price": _money(term.unit_price),
            "included_units": str(term.included_units),
            "overage_price": _money(term.overage_price),
            "minimum_commitment": _money(term.minimum_commitment),
            # ``None`` and ``()`` both mean "no ladder" to a reader, but
            # ``PriceTerm.__eq__`` distinguishes them and a round trip that changed
            # the value would fail the equality test rather than being caught by a
            # recalculation. Encode the distinction rather than normalising it away.
            "tiers": None
            if term.tiers is None
            else [
                {
                    "up_to": None if tier.up_to is None else str(tier.up_to),
                    "unit_price": _money(tier.unit_price),
                }
                for tier in term.tiers
            ],
        }
        for term in price_terms
    ]


def _encoded_usage_events(usage_events: list[UsageEvent]) -> list[dict[str, Any]]:
    return [
        {
            "external_id": event.external_id,
            "dedupe_key": event.dedupe_key,
            "metric_key": event.metric_key,
            "occurred_at": event.occurred_at.isoformat(),
            "quantity": str(event.quantity),
            "unit": event.unit,
        }
        for event in usage_events
    ]


def _encoded_payments(payments: list[Payment]) -> list[dict[str, Any]]:
    return [
        {
            "external_id": payment.external_id,
            "amount": _money(payment.amount),
            "allocations": [
                {
                    "invoice_external_id": allocation.invoice_external_id,
                    "amount": _money(allocation.amount),
                }
                for allocation in payment.allocations
            ],
        }
        for payment in payments
    ]


def _encoded_adjustments(adjustments: list[Adjustment]) -> list[dict[str, Any]]:
    return [
        {
            "external_id": adjustment.external_id,
            "amount": _money(adjustment.amount),
            "reason": adjustment.reason,
        }
        for adjustment in adjustments
    ]


# ----------------------------------------------------------------------
# decoding
# ----------------------------------------------------------------------


def _require(document: dict[str, Any], key: str, kind: type) -> Any:
    value = document.get(key, _MISSING)
    if value is _MISSING or not isinstance(value, kind):
        raise SourceDocumentError(f"source document is missing a usable {key!r}: {value!r}")
    return value


def _raise_missing(what: str) -> Any:
    """Fail the read because a required field is absent.

    A ``or``-chain over ``_money_from`` needs a right-hand side that never returns:
    ``_money_from`` yields ``None`` for an absent *optional* amount, and the caller
    has already decided this one is required. Raising here keeps that decision at the
    call site and produces a message naming the field, which is what
    :meth:`from_source_document` promises its callers.

    The return annotation is ``Any`` because this function never returns; it is used
    in an expression position where the checker needs a type.
    """
    raise SourceDocumentError(f"source document is missing a usable {what}")


def from_source_document(document: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the typed records from a stored document.

    Returns the keyword arguments for
    :meth:`app.services.evidence_collection.build_evidence_bundle` plus the typed
    objects, so the caller cannot accidentally pair an invoice with a document
    belonging to a different one.

    Every failure raises :class:`SourceDocumentError` rather than letting a
    ``KeyError`` or ``TypeError`` escape, because a stored document is a record
    that may be months old and written by a version of the code that no longer
    exists. A clear "this case's ingest payload cannot be read" is what a caller
    can act on; ``KeyError: 'period'`` is not.
    """
    if not document:
        raise SourceDocumentError("this case has no stored ingest payload")
    # The aggregate deep-freezes the document, so every nested dict arrives as a
    # MappingProxyType and every list as a tuple. Thawing once here is clearer than
    # loosening every isinstance check below to accept Mapping, and it guarantees
    # the rebuilt domain objects hold plain containers rather than read-only views
    # that would surprise a caller inspecting them.
    document = thaw_json(document)
    version = document.get("schema_version")
    if version != SOURCE_DOCUMENT_VERSION:
        raise SourceDocumentError(
            f"source document schema_version {version!r} cannot be read by this build "
            f"(expected {SOURCE_DOCUMENT_VERSION})"
        )

    raw_invoice = _require(document, "invoice", dict)
    invoice = RecordedInvoice(
        external_id=str(_require(raw_invoice, "external_id", str)),
        period=_period_from(raw_invoice.get("period"), "invoice.period"),
        currency=str(_require(raw_invoice, "currency", str)),
        lines=tuple(
            InvoiceLine(
                metric_key=str(line["metric_key"]),
                line_type=LineType(str(line["line_type"])),
                recorded_amount=_money_from(line, "recorded_amount")
                or _raise_missing("invoice line recorded_amount"),
            )
            for line in _require(raw_invoice, "lines", list)
        ),
        stated_total=_money_from(raw_invoice, "stated_total")
        or _raise_missing("invoice stated_total"),
    )

    price_terms = [
        PriceTerm(
            metric_key=str(term["metric_key"]),
            billing_mode=BillingMode(str(term["billing_mode"])),
            currency=str(term["currency"]),
            unit_price=_money_from(term, "unit_price"),
            included_units=Decimal(str(term["included_units"])),
            overage_price=_money_from(term, "overage_price"),
            minimum_commitment=_money_from(term, "minimum_commitment"),
            tiers=None
            if term["tiers"] is None
            else tuple(
                Tier(
                    up_to=None if tier["up_to"] is None else Decimal(str(tier["up_to"])),
                    unit_price=_money_from(tier, "unit_price")
                    or _raise_missing("tier unit_price"),
                )
                for tier in term["tiers"]
            ),
        )
        for term in document.get("price_terms", [])
    ]

    usage_events = [
        UsageEvent(
            external_id=str(event["external_id"]),
            dedupe_key=str(event["dedupe_key"]),
            metric_key=str(event["metric_key"]),
            occurred_at=_timestamp(event["occurred_at"], "usage event occurred_at"),
            quantity=Decimal(str(event["quantity"])),
            unit=str(event["unit"]),
        )
        for event in document.get("usage_events", [])
    ]

    payments = [
        Payment(
            external_id=str(payment["external_id"]),
            amount=_money_from(payment, "amount") or _raise_missing("payment amount"),
            allocations=tuple(
                PaymentAllocation(
                    invoice_external_id=str(allocation["invoice_external_id"]),
                    amount=_money_from(allocation, "amount")
                    or _raise_missing("payment allocation amount"),
                )
                for allocation in payment.get("allocations", [])
            ),
        )
        for payment in document.get("payments", [])
    ]

    adjustments = [
        Adjustment(
            external_id=str(adjustment["external_id"]),
            amount=_money_from(adjustment, "amount") or _raise_missing("adjustment amount"),
            reason=str(adjustment.get("reason", "")),
        )
        for adjustment in document.get("adjustments", [])
    ]

    return {
        "invoice": invoice,
        "price_terms": price_terms,
        "usage_events": usage_events,
        "payments": payments,
        "adjustments": adjustments,
        "dispute_text": str(document.get("dispute_text", "")),
    }


def _timestamp(raw: Any, where: str) -> datetime:
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise SourceDocumentError(f"{where} is not an ISO timestamp: {raw!r}") from exc


def _thaw_document(value: Mapping[str, Any]) -> dict[str, Any]:
    """``_thaw`` at document granularity, so the result is typed.

    ``_thaw`` returns ``Any`` because it is recursive over arbitrary JSON. Widening
    that to the top of a function whose contract is ``dict[str, Any]`` pushes the
    ``Any`` into the return statement and out into every caller, so the narrowing is
    asserted once here instead of being rediscovered by annotation at each of the
    dozen places that touch a document.
    """
    thawed = thaw_json(value)
    # A frozen aggregate stores a mapping here, and JSON only has objects, so the
    # dict check cannot fail for a document produced by this module. If it somehow
    # does, the caller gets a clear error rather than an AttributeError later on.
    if not isinstance(thawed, dict):
        raise SourceDocumentError("stored ingest payload is not a JSON object")
    return thawed


def merge_records(
    document: dict[str, Any],
    *,
    price_terms: Sequence[PriceTerm] = (),
    usage_events: Sequence[UsageEvent] = (),
    payments: Sequence[Payment] = (),
    adjustments: Sequence[Adjustment] = (),
) -> dict[str, Any]:
    """Add records to a stored ingest payload, keyed by external id.

    Keyed rather than appended so that attaching the same payment twice is a no-op.
    An append would grow the document on every retry, and the document's contents
    determine the evidence fingerprint -- so a retried request would change the
    fingerprint, mark every run stale and visibly reopen a case that had not
    actually gained any facts. That is the specific failure this idempotence
    prevents.

    The invoice is *not* merged. It is the subject of the dispute, and replacing it
    would be re-opening the case under a new invoice rather than adding evidence to
    the existing one. A genuinely corrected invoice is a different case.

    Free-form snapshots a reviewer attached (a scanned receipt, a call note) are
    carried through untouched in ``attached_evidence``. They are not source records
    and cannot be turned back into typed values, so they are kept as the hashed
    snapshots they are.
    """
    if not document or not document.get("present"):
        raise SourceDocumentError("this case has no stored ingest payload to extend")

    merged = _thaw_document(document)
    # Deliberately no ``setdefault("attached_evidence", [])`` here. Adding an empty
    # list is itself a change to the document, and the document decides the case
    # version: opening a case and then re-sending a record that was already supplied
    # would bump the version and rewrite the row while adding nothing. Every reader
    # treats the key as optional, so leaving it absent costs nothing.

    def _upsert(
        key: str, records: list[Any], id_of: Any, encode: Any, key_of: Any | None = None
    ) -> None:
        """Replace-or-insert by identity.

        ``key_of`` reads the identity out of an *encoded* entry and defaults to
        ``external_id``. It is needed because price terms have no external id: they
        are identified by ``metric_key``, and keying them by anything else would let
        two different terms for one metric coexist in the payload -- which would
        silently change what the engine prices.
        """
        read_key = key_of or (lambda entry: entry["external_id"])
        existing = {str(read_key(entry)): entry for entry in merged.get(key, [])}
        for record in records:
            existing[str(id_of(record))] = encode(record)
        # Sorted by identity so the document is canonical. Without this, re-attaching
        # a record already present would move it to the end of the list, producing a
        # different JSON document for identical facts -- which would bump the case
        # version and mark every run stale, making a retried request look like new
        # evidence.
        merged[key] = [existing[name] for name in sorted(existing)]

    _upsert("price_terms", list(price_terms), lambda t: t.metric_key,
            lambda t: _encoded_terms([t])[0], key_of=lambda entry: entry["metric_key"])
    _upsert("usage_events", list(usage_events), lambda e: e.external_id,
            lambda e: _encoded_usage_events([e])[0])
    _upsert("payments", list(payments), lambda p: p.external_id,
            lambda p: _encoded_payments([p])[0])
    _upsert("adjustments", list(adjustments), lambda a: a.external_id,
            lambda a: _encoded_adjustments([a])[0])
    return merged


def attach_snapshot(document: dict[str, Any], item: EvidenceItem) -> dict[str, Any]:
    """Record a free-form snapshot on the ingest payload so it survives a rebuild.

    Idempotent for an identical retry. Re-sending the same snapshot must not change
    the document, because the document's contents determine the evidence
    fingerprint: a retried request that appended a duplicate would change the
    fingerprint, mark every existing run stale and visibly reopen a case that had not
    gained a single fact. A retry is the *expected* failure mode for a network call
    that may or may not have landed, so it has to be safe.

    Same key with different content is refused rather than overwritten. One key means
    one fact; if the content behind it changes, the old snapshot has to stay citable
    by the runs that read it, and overwriting would silently invalidate their
    citations while leaving them marked as valid.
    """
    merged = _thaw_document(document)
    attached = list(merged.get("attached_evidence", []))
    entry = {
        "natural_key": item.natural_key,
        "evidence_type": item.evidence_type.value,
        "snapshot": thaw_json(item.snapshot),
    }
    for existing in attached:
        if existing["natural_key"] != item.natural_key:
            continue
        if existing == entry:
            # Identical retry. Return the document untouched rather than appending a
            # second copy under the same key, which the bundle builder would then see
            # as a duplicate natural key.
            return merged
        raise SourceDocumentError(
            f"{item.natural_key!r} is already attached with different content; a "
            "dispute may hold one snapshot per key"
        )
    attached.append(entry)
    merged["attached_evidence"] = attached
    return merged


def build_bundle_from_document(
    document: dict[str, Any], *, dispute_external_id: str, contract_external_id: str
) -> EvidenceBundle:
    """Rebuild the whole evidence bundle from a stored ingest payload.

    This is the single definition of "the evidence a case has", and both the attach
    path and the investigate path go through it. That is what makes the staleness
    check in :meth:`CaseService.investigate` meaningful: the fingerprint it compares
    against is produced by the same function, so a mismatch means the document and
    the stored snapshots genuinely disagree rather than that two code paths disagree
    about how to collect evidence.

    Rebuilding rather than appending also gives idempotence for free. The bundle is
    a pure function of the document, so re-attaching a record already present
    produces a byte-identical bundle and therefore an identical fingerprint.
    """
    from app.services.evidence_collection import build_evidence_bundle

    records = from_source_document(document)
    invoice = records["invoice"]
    terms: list[PriceTerm] = records["price_terms"]
    events: list[UsageEvent] = records["usage_events"]
    summaries = [summarise_usage(term.metric_key, events, invoice.period) for term in terms]
    bundle = build_evidence_bundle(
        dispute_external_id=dispute_external_id,
        invoice=invoice,
        price_terms=terms,
        usage_events=events,
        usage_summaries=summaries,
        payments=records["payments"],
        contract_external_id=contract_external_id,
        dispute_text=str(records["dispute_text"]),
    )
    extra = [
        EvidenceItem.create(entry["natural_key"], EvidenceType(entry["evidence_type"]), entry["snapshot"])
        for entry in document.get("attached_evidence", [])
    ]
    return EvidenceBundle.of([*bundle.items, *extra])
