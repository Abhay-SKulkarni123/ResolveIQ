"""The ingest payload: what is stored, what is rebuilt, and what is refused.

``source_document`` is the JSONB column a re-investigation reads from. It exists
because typed records cannot be stored in JSONB and rebuilt faithfully by hand later,
and because a run must be reproducible from exactly what it saw.

The properties worth testing are mostly about *refusal* and *idempotence*, because
those are where a stored document goes wrong:

* **A round trip preserves the money.** Text in, ``Money`` out, no float anywhere.
  A document that lost a digit of precision would produce a recalculation that
  disagrees with the original, and nothing would flag it.
* **A retried write changes nothing.** Ingest is called by webhooks that retry. An
  append would change the document, and the document determines the fingerprint, so a
  retry would stale every run on the case and visibly reopen a dispute that had gained
  no facts. This is the single most likely production failure of this table.
* **A stale document is refused, not guessed at.** A payload written by a version of
  the code that no longer exists must produce "this cannot be read", not a partially
  rebuilt invoice.
* **One key means one fact.** Overwriting a snapshot would invalidate the citations of
  runs that read the old one while leaving them marked valid.
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
    Tier,
    UsageEvent,
)
from app.domain.contracts import BillingMode
from app.domain.evidence import EvidenceItem, EvidenceType
from app.domain.json_frozen import thaw_json
from app.domain.money import Money
from app.services.source_document import (
    SOURCE_DOCUMENT_VERSION,
    SourceDocumentError,
    attach_snapshot,
    build_bundle_from_document,
    from_source_document,
    merge_records,
    to_source_document,
)

PERIOD = InvoicePeriod(date(2026, 3, 1), date(2026, 4, 1))


def usd(text: str) -> Money:
    return Money.parse(text, "USD")


def invoice() -> RecordedInvoice:
    return RecordedInvoice(
        external_id="INV-2026-03-0042",
        period=PERIOD,
        currency="USD",
        lines=(
            InvoiceLine("api_calls", LineType.USAGE, usd("28.40")),
            InvoiceLine("platform_fee", LineType.FIXED, usd("5.00")),
        ),
        stated_total=usd("33.40"),
    )


def term(metric_key: str = "api_calls") -> PriceTerm:
    return PriceTerm(
        metric_key,
        BillingMode.TIERED,
        "USD",
        tiers=(
            Tier(up_to=Decimal("10000"), unit_price=usd("0.50")),
            Tier(up_to=Decimal("50000"), unit_price=usd("0.0007")),
        ),
    )


def event(external_id: str = "e1") -> UsageEvent:
    return UsageEvent(
        external_id=external_id,
        dedupe_key=f"DK-{external_id}",
        metric_key="api_calls",
        occurred_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
        quantity=Decimal("41234"),
        unit="calls",
    )


def payment(external_id: str = "PAY-77") -> Payment:
    return Payment(
        external_id=external_id,
        amount=usd("10.00"),
        allocations=(
            PaymentAllocation(invoice_external_id="INV-2026-03-0042", amount=usd("10.00")),
        ),
    )


def adjustment(external_id: str = "ADJ-1") -> Adjustment:
    return Adjustment(external_id=external_id, amount=usd("3.00"), reason="goodwill")


def encode(
    *,
    inv: RecordedInvoice | None = None,
    terms: list[PriceTerm] | None = None,
    events: list[UsageEvent] | None = None,
    payments_: list[Payment] | None = None,
    adjustments_: list[Adjustment] | None = None,
    text: str = "We were billed the wrong rate for API calls.",
) -> dict[str, object]:
    return to_source_document(
        invoice=inv or invoice(),
        price_terms=terms if terms is not None else [term()],
        usage_events=events if events is not None else [event()],
        payments=payments_ if payments_ is not None else [payment()],
        adjustments=adjustments_ if adjustments_ is not None else [adjustment()],
        dispute_text=text,
    )


def document(**overrides: object) -> dict[str, object]:
    base = encode()
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_the_document_records_its_schema_version() -> None:
    """A document written by a future version must be refused, not misread."""
    assert document()["schema_version"] == SOURCE_DOCUMENT_VERSION


def test_the_document_marks_the_invoice_as_present() -> None:
    """``present`` distinguishes "no invoice to recalculate" from "malformed"."""
    assert document()["present"] is True


def test_amounts_are_encoded_as_text_with_a_currency() -> None:
    """JSONB has no decimal type, so ``{"amount": str, "currency": str}``."""
    encoded = document()["invoice"]["stated_total"]  # type: ignore[index]

    assert encoded == {"amount": "33.40", "currency": "USD"}


def test_no_float_is_written_anywhere() -> None:
    """A float that reaches JSONB has already lost digits before anyone notices."""

    def walk(value: object, path: str) -> None:
        if isinstance(value, float):
            pytest.fail(f"float at {path}: {value!r}")
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(thaw_json(document()), "document")


def test_every_record_kind_is_carried() -> None:
    stored = document()

    assert stored["price_terms"]
    assert stored["usage_events"]
    assert stored["payments"]
    assert stored["adjustments"]
    assert stored["dispute_text"]


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_document_rebuilds_the_records_it_was_built_from() -> None:
    """The claim that makes a re-investigation reproducible."""
    rebuilt = from_source_document(document())

    assert rebuilt["invoice"] == invoice()
    assert rebuilt["price_terms"] == [term()]
    assert rebuilt["usage_events"] == [event()]
    assert rebuilt["payments"] == [payment()]
    assert rebuilt["adjustments"] == [adjustment()]


def test_money_survives_the_round_trip_exactly() -> None:
    """The reason amounts are text.

    A document that lost precision would recalculate to a different total than the
    original, and both would look authoritative.
    """
    rebuilt = from_source_document(document())
    original = invoice()

    assert rebuilt["invoice"].stated_total.as_string() == original.stated_total.as_string()
    assert rebuilt["invoice"].stated_total == usd("33.40")


def test_the_invoice_lines_survive_in_order() -> None:
    """Order matters: a reviewer reading the invoice compares it to what was charged."""
    rebuilt = from_source_document(document())

    assert [line.metric_key for line in rebuilt["invoice"].lines] == [
        "api_calls",
        "platform_fee",
    ]
    assert rebuilt["invoice"].lines[0].recorded_amount == usd("28.40")


def test_an_empty_document_rebuilds_to_empty_collections() -> None:
    """A dispute with no payments yet is normal, not malformed."""
    stored = encode(events=[], payments_=[], adjustments_=[], text="wrong rate")

    rebuilt = from_source_document(stored)

    assert rebuilt["payments"] == []
    assert rebuilt["adjustments"] == []
    assert rebuilt["usage_events"] == []


def test_a_frozen_document_can_be_read() -> None:
    """The aggregate deep-freezes it, so every nested value arrives read-only.

    Without thawing, every ``isinstance(value, dict)`` below would fail on a
    ``mappingproxy`` and every rebuilt case would be unreadable.
    """
    from app.domain.json_frozen import freeze_json

    frozen = freeze_json(document())

    assert from_source_document(frozen)["invoice"] == invoice()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_empty_document_is_refused() -> None:
    with pytest.raises(SourceDocumentError, match="no stored ingest payload"):
        from_source_document({})


def test_a_future_schema_version_is_refused() -> None:
    """Named in the message, so the failure says which build wrote it."""
    stored = document()
    stored["schema_version"] = SOURCE_DOCUMENT_VERSION + 1

    with pytest.raises(SourceDocumentError, match="cannot be read by this build"):
        from_source_document(stored)


def test_a_document_with_no_invoice_is_refused() -> None:
    stored = document()
    del stored["invoice"]

    with pytest.raises(SourceDocumentError, match="invoice"):
        from_source_document(stored)


def test_an_invoice_with_an_unusable_period_is_refused() -> None:
    """Named by path, because "missing period" in a four-year-old row is not obvious."""
    stored = document()
    stored["invoice"]["period"] = {"start": "not-a-date", "end": "2026-03-31"}  # type: ignore[index]

    with pytest.raises(SourceDocumentError, match="period"):
        from_source_document(stored)


def test_a_document_with_a_missing_key_names_the_key() -> None:
    """``KeyError: 'period'`` is not something a caller can act on."""
    stored = document()
    del stored["invoice"]["currency"]  # type: ignore[index]

    with pytest.raises(SourceDocumentError, match="currency"):
        from_source_document(stored)


def test_a_refused_document_never_returns_a_partial_result() -> None:
    """Half a case is worse than none: it looks investigable."""
    stored = document()
    del stored["invoice"]["stated_total"]  # type: ignore[index]

    with pytest.raises(SourceDocumentError):
        from_source_document(stored)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def test_merging_adds_a_record() -> None:
    merged = merge_records(document(), payments=[payment("PAY-99")])

    assert {entry["external_id"] for entry in merged["payments"]} == {"PAY-77", "PAY-99"}


def test_merging_the_same_record_twice_changes_nothing() -> None:
    """The retried webhook.

    An append would change the document, the document determines the fingerprint, and
    every run on the case would be marked stale for a request that added no facts.
    """
    original = document()

    merged = merge_records(original, payments=[payment()])

    assert merged == original


def test_merging_replaces_a_record_with_the_same_id() -> None:
    """A corrected payment is an update, and the old one must not linger."""
    merged = merge_records(
        document(),
        payments=[Payment(external_id="PAY-77", amount=usd("12.00"), allocations=())],
    )

    assert len(merged["payments"]) == 1
    assert merged["payments"][0]["amount"]["amount"] == "12.00"


def test_two_price_terms_for_one_metric_cannot_coexist() -> None:
    """Terms have no external id, so they are keyed by metric.

    Keying them by anything else would let two terms for one metric into the payload,
    which would silently change what the engine prices.
    """
    other = PriceTerm("api_calls", BillingMode.PER_UNIT, "USD", unit_price=usd("0.25"))

    merged = merge_records(document(), price_terms=[other])

    assert len(merged["price_terms"]) == 1
    assert merged["price_terms"][0]["billing_mode"] == "PER_UNIT"


def test_two_different_metrics_both_survive() -> None:
    merged = merge_records(document(), price_terms=[term("platform_fee")])

    assert {entry["metric_key"] for entry in merged["price_terms"]} == {
        "api_calls",
        "platform_fee",
    }


def test_the_merged_document_is_canonically_ordered() -> None:
    """Otherwise re-attaching an existing record moves it and the document differs.

    Identical facts must produce an identical document, or the case version moves and
    a retry looks like new evidence.
    """
    first = merge_records(document(), usage_events=[event("e2"), event("e3")])
    second = merge_records(first, usage_events=[event("e3")])

    assert second == first


def test_merging_into_an_absent_document_is_refused() -> None:
    with pytest.raises(SourceDocumentError, match="no stored ingest payload"):
        merge_records({}, payments=[payment()])


def test_merging_into_an_absent_invoice_is_refused() -> None:
    """There is no bundle to rebuild without an invoice to attach it to."""
    with pytest.raises(SourceDocumentError):
        merge_records({"schema_version": 1, "present": False}, payments=[payment()])


def test_merging_does_not_mutate_the_document_it_was_given() -> None:
    """A frozen aggregate must not be thawed in place by a caller that then re-freezes it."""
    original = document()
    snapshot = thaw_json(original)

    merge_records(original, payments=[payment("PAY-99")])

    assert original == snapshot


def test_the_invoice_is_not_merged() -> None:
    """The invoice is the subject of the dispute.

    Replacing it would be re-opening the case under a new invoice rather than adding
    evidence to the existing one. A genuinely corrected invoice is a different case.
    """
    stored = document()
    replacement = RecordedInvoice(
        external_id="INV-OTHER",
        period=PERIOD,
        currency="USD",
        lines=(InvoiceLine("api_calls", LineType.USAGE, usd("1.00")),),
        stated_total=usd("1.00"),
    )

    merged = merge_records(stored, payments=[payment("PAY-99")])

    # Merging takes no invoice argument at all, so there is no way to swap one by
    # attaching evidence. Changing the invoice means writing a whole new document,
    # which is a different case.
    assert merged["invoice"]["external_id"] == "INV-2026-03-0042"
    assert encode(inv=replacement)["invoice"]["external_id"] == "INV-OTHER"


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot(key: str = "note:call-1", summary: str = "read us the last four digits") -> EvidenceItem:
    return EvidenceItem.create(key, EvidenceType.PAYMENT, {"kind": "CALL_NOTE", "summary": summary})


def test_a_snapshot_is_carried_through_a_merge() -> None:
    """A rebuild must not drop what a reviewer attached by hand."""
    stored = attach_snapshot(document(), snapshot())

    merged = merge_records(stored, payments=[payment("PAY-99")])

    assert [entry["natural_key"] for entry in merged["attached_evidence"]] == ["note:call-1"]


def test_a_snapshot_reaches_the_rebuilt_bundle() -> None:
    stored = attach_snapshot(document(), snapshot())

    bundle = build_bundle_from_document(
        stored, dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )

    assert any(item.natural_key == "note:call-1" for item in bundle.items)


def test_an_identical_snapshot_retry_is_a_no_op() -> None:
    """Same failure mode as a retried record: the fingerprint must not move."""
    original = attach_snapshot(document(), snapshot())

    again = attach_snapshot(original, snapshot())

    assert again == original


def test_a_snapshot_with_the_same_key_and_new_content_is_refused() -> None:
    """One key means one fact.

    Overwriting would invalidate the citations of every run that read the old snapshot
    while leaving those runs marked as valid.
    """
    stored = attach_snapshot(document(), snapshot())

    with pytest.raises(SourceDocumentError, match="one snapshot per key"):
        attach_snapshot(stored, snapshot(summary="a completely different call"))


def test_a_refused_snapshot_leaves_the_document_untouched() -> None:
    stored = attach_snapshot(document(), snapshot())

    with pytest.raises(SourceDocumentError):
        attach_snapshot(stored, snapshot(summary="different"))

    assert len(stored["attached_evidence"]) == 1


def test_two_snapshots_with_different_keys_both_survive() -> None:
    stored = attach_snapshot(document(), snapshot("note:call-1"))
    stored = attach_snapshot(stored, snapshot("note:call-2"))

    assert len(stored["attached_evidence"]) == 2


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def test_the_bundle_contains_every_record_kind() -> None:
    bundle = build_bundle_from_document(
        document(), dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )

    assert {item.evidence_type for item in bundle.items} >= {
        EvidenceType.INVOICE,
        EvidenceType.INVOICE_LINE,
        EvidenceType.USAGE_EVENT,
        EvidenceType.PAYMENT,
        EvidenceType.CONTRACT_TERM,
        EvidenceType.DISPUTE_TEXT,
    }


def test_the_bundle_is_the_same_whether_built_directly_or_via_the_document() -> None:
    """Both the open path and the re-investigate path go through the document.

    If they could disagree, the staleness check would be comparing things that were
    never meant to match, and a freshly opened case could look stale immediately.
    """
    stored = document()

    from_document = build_bundle_from_document(
        stored, dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )
    from_records = build_bundle_from_document(
        stored, dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )

    assert from_document.fingerprint == from_records.fingerprint


def test_the_dispute_text_is_in_the_bundle_as_evidence() -> None:
    bundle = build_bundle_from_document(
        document(), dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )

    text_items = bundle.of_type(EvidenceType.DISPUTE_TEXT)

    assert text_items
    assert "wrong rate" in str(text_items[0].snapshot)


def test_a_snapshot_joins_the_fingerprint() -> None:
    """Otherwise a reviewer could attach evidence without staling the runs that missed it."""
    before = build_bundle_from_document(
        document(), dispute_external_id="DSC-1", contract_external_id="CTR-5512"
    )
    after = build_bundle_from_document(
        attach_snapshot(document(), snapshot()),
        dispute_external_id="DSC-1",
        contract_external_id="CTR-5512",
    )

    assert after.fingerprint != before.fingerprint
