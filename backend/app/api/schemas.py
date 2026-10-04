"""Request and response bodies for the case API.

Thin on purpose. These models translate between JSON and domain objects; they do
not validate business rules, and nothing here recomputes or derives a figure. A
field that would need a calculation to fill belongs in the response built from
persisted state, not in a schema that could disagree with it.

Three choices are load-bearing.

**Amounts are strings throughout.** ``12.40``, never ``12.4``. A JSON number is
parsed as a binary float by ``JSON.parse``, so a numeric field would quietly lose
precision on the way to the browser and then be formatted back with a different
value than the one stored (STK-01). The frontend formats them for display and never
does arithmetic on them.

**Every collection of money is accompanied by its currency.** ``recalculated_total``
is meaningless without ``currency``, so the two are separate required fields
rather than a nested object that a caller could populate with one and not the
other.

**Staleness is reported, not recomputed by the client.** ``is_stale`` and
``evidence_fingerprint`` come from the server. The browser comparing two hashes
itself would be a second implementation of the rule, free to drift from
:mod:`app.domain.cases`, and the drift would show up as a reviewer acting on
findings that no longer match the evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AttachEvidenceRequest",
    "CalculationResponse",
    "CaseListResponse",
    "CaseSummaryResponse",
    "DisputeDetailResponse",
    "EvidenceResponse",
    "FindingResponse",
    "HypothesisResponse",
    "InvestigationResponse",
    "MoneyResponse",
    "OpenDisputeRequest",
    "ReopenRequest",
    "ResolutionOptionResponse",
    "ReviewRequest",
    "ReviewResponse",
]

Money = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$", examples=["12.4000"])]


class _Model(BaseModel):
    """Base with the project's conventions: reject unknown fields, validate on assign."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ----------------------------------------------------------------------
# requests
# ----------------------------------------------------------------------


class MoneyInput(_Model):
    """An amount and its currency, as submitted.

    Currency is required even though every amount in a case shares one. A single
    ambiguous amount in a payload is exactly how a figure ends up labelled in the
    wrong currency, and making it optional would only move the guess later.
    """

    amount: Money
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["USD"])]


class InvoiceLineInput(_Model):
    metric_key: Annotated[str, Field(min_length=1, max_length=128)]
    line_type: Literal["USAGE", "FIXED"]
    recorded_amount: MoneyInput


class InvoiceInput(_Model):
    external_id: Annotated[str, Field(min_length=1, max_length=128)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    period_start: Annotated[str, Field(examples=["2026-03-01"])]
    period_end: Annotated[str, Field(examples=["2026-03-31"])]
    stated_total: MoneyInput
    lines: Annotated[list[InvoiceLineInput], Field(min_length=1)]


class TierInput(_Model):
    up_to: str | None = None
    unit_price: MoneyInput


class PriceTermInput(_Model):
    metric_key: Annotated[str, Field(min_length=1, max_length=128)]
    billing_mode: Literal["PER_UNIT", "TIERED", "COMMITMENT"]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    included_units: str = "0"
    unit_price: MoneyInput | None = None
    overage_price: MoneyInput | None = None
    minimum_commitment: MoneyInput | None = None
    tiers: list[TierInput] | None = None


class UsageEventInput(_Model):
    external_id: Annotated[str, Field(min_length=1, max_length=128)]
    dedupe_key: Annotated[str, Field(min_length=1, max_length=255)]
    metric_key: Annotated[str, Field(min_length=1, max_length=128)]
    occurred_at: Annotated[str, Field(examples=["2026-03-05T00:00:00+00:00"])]
    quantity: Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
    unit: Annotated[str, Field(min_length=1, max_length=64)]


class PaymentAllocationInput(_Model):
    invoice_external_id: Annotated[str, Field(min_length=1, max_length=128)]
    amount: MoneyInput


class PaymentInput(_Model):
    external_id: Annotated[str, Field(min_length=1, max_length=128)]
    amount: MoneyInput
    allocations: list[PaymentAllocationInput] = Field(default_factory=list)


class AdjustmentInput(_Model):
    """A credit or surcharge already applied to the invoice.

    Accepted here because adjustments change the outstanding balance, and a reviewer
    who cannot see that a credit was already applied will read the balance as money
    still owed. They are *not* citable evidence: Phase 3's evidence vocabulary is
    closed and has no adjustment type, so no finding may cite one. Storing them in
    the ingest payload rather than widening the vocabulary keeps both properties.
    """

    external_id: Annotated[str, Field(min_length=1, max_length=128)]
    amount: MoneyInput
    reason: str = ""


class OpenDisputeRequest(_Model):
    """Open a case from its source records.

    The whole ingest payload arrives here rather than only an invoice id, because
    this is the only point at which the records exist. Everything after this --
    investigating, reopening, re-running -- reads back what is stored, so the
    evidence a reviewer sees and the records a run is computed from cannot diverge.
    """

    dispute_external_id: Annotated[str, Field(min_length=3, max_length=64)]
    invoice: InvoiceInput
    contract_external_id: Annotated[str, Field(min_length=1, max_length=128)]
    dispute_text: Annotated[str, Field(min_length=1)]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    description: str = ""
    price_terms: list[PriceTermInput] = Field(default_factory=list)
    usage_events: list[UsageEventInput] = Field(default_factory=list)
    payments: list[PaymentInput] = Field(default_factory=list)
    adjustments: list[AdjustmentInput] = Field(default_factory=list)


class AttachEvidenceRequest(_Model):
    """Add records or free-form snapshots to an existing case.

    Every field is optional, and at least one must be present. An empty attach is
    refused rather than accepted as a no-op, because a client sending it has a bug
    and returning 200 would hide it.
    """

    price_terms: list[PriceTermInput] = Field(default_factory=list)
    usage_events: list[UsageEventInput] = Field(default_factory=list)
    payments: list[PaymentInput] = Field(default_factory=list)
    adjustments: list[AdjustmentInput] = Field(default_factory=list)
    snapshots: list[SnapshotInput] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.price_terms or self.usage_events or self.payments or self.adjustments
            or self.snapshots
        )


class SnapshotInput(_Model):
    """A free-form evidence snapshot: a receipt image reference, a call note.

    The snapshot's content hash is **not** supplied. It is derived, because a caller
    that could assert its own hash could make a tampered snapshot keep a trusted
    fingerprint, and the whole staleness mechanism rests on that fingerprint meaning
    what it says.
    """

    natural_key: Annotated[str, Field(min_length=1, max_length=255)]
    evidence_type: Literal[
        "INVOICE",
        "INVOICE_LINE",
        "USAGE_SUMMARY",
        "USAGE_EVENT",
        "PAYMENT",
        "PAYMENT_ALLOCATION",
        "CONTRACT_TERM",
        "DISPUTE_TEXT",
    ]
    payload: dict[str, Any]


class ReopenRequest(_Model):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class ReviewRequest(_Model):
    """A reviewer's annotation on one finding, hypothesis or option.

    There is no ``approve``. Approval moves money and arrives with the idempotency
    and separation-of-duties rules of docs/SYSTEM_DESIGN.md §9.3; offering the verb
    before those exist is how a button gets wired up without its guard.
    """

    investigation_id: str
    target_type: Literal["FINDING", "HYPOTHESIS", "RESOLUTION_OPTION"]
    target_id: str
    action: Literal["ACCEPT", "REJECT", "REQUEST_MORE_INFO", "AMEND"]
    rationale: Annotated[str, Field(max_length=4000)] = ""
    amended_narrative: Annotated[str, Field(max_length=4000)] | None = None


AttachEvidenceRequest.model_rebuild()


# ----------------------------------------------------------------------
# responses
# ----------------------------------------------------------------------


class MoneyResponse(_Model):
    amount: Money | None
    currency: str | None
    #: Why an amount is absent. Never inferred by the frontend: "not assessable" and
    #: "zero" are different answers and the UI must be able to tell them apart.
    not_assessable_reason: str | None = None


class EvidenceResponse(_Model):
    natural_key: str
    evidence_type: str
    content_hash: str
    captured_at: datetime | None = None
    snapshot: dict[str, Any]


class FindingResponse(_Model):
    id: str
    code: str
    severity: str
    category: str
    narrative: str
    confidence: Annotated[str, Field(pattern=r"^\d(\.\d+)?$")]
    supporting_evidence: list[str]
    #: What reviewers have done to this finding. Present so the UI can show a review
    #: against a specific finding without a second round trip.
    reviews: list[ReviewResponse] = Field(default_factory=list)


class HypothesisResponse(_Model):
    id: str
    hypothesis_code: str
    title: str
    narrative: str
    status: str
    likelihood: str | None = None
    metric_key: str | None = None
    supporting_evidence: list[str]
    refuting_evidence: list[str]
    #: Computed by the deterministic engine, never by the model.
    impact: MoneyResponse
    impact_basis: str | None = None
    impact_trace: dict[str, Any] = Field(default_factory=dict)
    reviews: list[ReviewResponse] = Field(default_factory=list)


class ResolutionOptionResponse(_Model):
    id: str
    option_type: str
    title: str
    rationale: str
    #: Always ``true`` for any remedy that would move money. Present as an explicit
    #: field rather than implied by the absence of a button, so the UI cannot
    #: accidentally present a money movement as something it can do.
    requires_human_approval: bool
    hypothesis_code: str | None = None
    supporting_evidence: list[str]
    reviews: list[ReviewResponse] = Field(default_factory=list)


class CalculationResponse(_Model):
    """The engine's figures, stored apart from the run that reported them."""

    currency: str
    engine_version: str
    recalculated_total: Money
    recorded_total: Money | None = None
    difference: Money | None = None
    outstanding: Money | None = None
    allocated_payments: Money | None = None
    net_adjustments: Money | None = None
    is_complete: bool
    is_provisional: bool
    unresolved_metrics: list[str]
    invoice: dict[str, Any] = Field(default_factory=dict)
    balance: dict[str, Any] = Field(default_factory=dict)
    usage_summaries: list[dict[str, Any]] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class InvestigationResponse(_Model):
    id: str
    version: int
    status: str
    summary: str
    provider_name: str
    model: str
    prompt_version: str
    engine_version: str
    created_at: datetime
    evidence_fingerprint: str
    is_stale: bool
    stale_at: datetime | None = None
    degradations: list[str]
    calculation: CalculationResponse | None = None
    findings: list[FindingResponse] = Field(default_factory=list)
    hypotheses: list[HypothesisResponse] = Field(default_factory=list)
    resolution_options: list[ResolutionOptionResponse] = Field(default_factory=list)
    #: The snapshots this run read, which is a subset of the case's evidence after
    #: new evidence arrives. Sending the case's current evidence here would be a lie
    #: for any run but the newest.
    evidence: list[EvidenceResponse] = Field(default_factory=list)


class ReviewResponse(_Model):
    id: str
    investigation_id: str
    target_type: str
    target_id: str
    action: str
    actor_id: str
    actor_role: str
    rationale: str
    amended_narrative: str | None = None
    evidence_fingerprint_seen: str
    created_at: datetime
    #: Whether the evidence this reviewer saw is still the evidence on the case. A
    #: review marked false was made against a superseded view and the UI says so.
    is_against_current_evidence: bool


class CaseSummaryResponse(_Model):
    """One row of the dispute list.

    Carries staleness and the current investigation's headline figures so the list
    can be triaged without loading every finding. It deliberately omits narratives.
    """

    id: str
    external_id: str
    invoice_external_id: str
    contract_external_id: str | None = None
    status: str
    severity: str
    is_stale: bool
    evidence_fingerprint: str
    version: int
    created_at: datetime
    investigation_count: int
    current_investigation_version: int | None = None
    outstanding: Money | None = None
    currency: str | None = None
    finding_count: int = 0


class DisputeDetailResponse(CaseSummaryResponse):
    description: str
    evidence: list[EvidenceResponse]
    current_investigation: InvestigationResponse | None = None
    #: Every run, newest last. Present because "what did the system believe last
    #: week, and why has it changed?" is the question a reopened case is opened to
    #: answer.
    investigations: list[InvestigationResponse] = Field(default_factory=list)
    reviews: list[ReviewResponse] = Field(default_factory=list)


class CaseListResponse(_Model):
    items: list[CaseSummaryResponse]
    total: int
    limit: int
    offset: int


FindingResponse.model_rebuild()
HypothesisResponse.model_rebuild()
ResolutionOptionResponse.model_rebuild()