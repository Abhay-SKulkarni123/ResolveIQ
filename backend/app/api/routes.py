"""The dispute-case endpoints.

Thin controllers, per docs/SYSTEM_DESIGN.md §11: parse, authorise, delegate,
serialise. No arithmetic, no SQL, no rules. Every ``if`` in this module is about the
*shape* of a request or a response -- whether an id parses, whether a required
collection was left empty -- and not about what a case is allowed to do. That
distinction is the reason a route cannot quietly become a second implementation of a
domain rule.

Implemented here
----------------
===================================== ==========================================
``POST   /disputes``                  open a case from its source records
``GET    /disputes``                  list, filter by status
``GET    /disputes/{id}``             detail: status, staleness, current run
``POST   /disputes/{id}/evidence``    attach records or snapshots (idempotent)
``POST   /disputes/{id}/investigations`` start a run
``POST   /disputes/{id}/review``      record a reviewer annotation
``POST   /disputes/{id}/reopen``      reopen a closed case
``GET    /capabilities``              provider mode and which store is live
===================================== ==========================================

Deliberately absent: ``/adjustments`` and anything that approves. Approval moves money
and needs the idempotency key, the unique constraint and the separation-of-duties rules
of §9.3 as one unit. Shipping the annotation endpoint now and the decision endpoint
with its guards later is the split; shipping the button without the guards is not a
split, it is an incident.

Where the money boundary is enforced
------------------------------------
No route accepts an amount for a remedy, and none returns one that the deterministic
engine did not compute. ``HypothesisResponse.impact`` comes from
``StoredHypothesis.impact_amount``, which only :mod:`app.pricing.impact` writes. A
reviewer can act on an amount here; nothing here acts on one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import ActorDep, CaseServiceDep, SettingsDep
from app.api.errors import ApiError
from app.api.schemas import (
    AttachEvidenceRequest,
    CalculationResponse,
    CaseListResponse,
    CaseSummaryResponse,
    DisputeDetailResponse,
    EvidenceResponse,
    FindingResponse,
    HypothesisResponse,
    InvestigationResponse,
    MoneyResponse,
    OpenDisputeRequest,
    ReopenRequest,
    ResolutionOptionResponse,
    ReviewRequest,
    ReviewResponse,
)
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
from app.domain.cases import (
    CaseSeverity,
    CaseStatus,
    DisputeCase,
    InvestigationRecord,
    ReviewActionKind,
    ReviewTargetType,
)
from app.domain.contracts import BillingMode
from app.domain.evidence import EvidenceItem, EvidenceType
from app.domain.json_frozen import thaw_json
from app.domain.money import Money
from app.services.cases import OpenCaseCommand
from app.services.source_document import SourceDocumentError

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["disputes"])


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _uuid(raw: str, field: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(
            422, "INVALID_IDENTIFIER", f"{field} must be a UUID", {"field": field, "value": raw}
        ) from exc


def _decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ApiError(
            422, "INVALID_AMOUNT", f"{field} is not a decimal amount", {"field": field}
        ) from exc


def _money(payload: Any, field: str) -> Money:
    """Build a domain ``Money`` from a request object.

    The domain validates the scale and the currency and raises if they are wrong; that
    error is a ``BillingDataError``/``ValueError``, translated to a 422 here rather
    than escaping as a 500. Letting a domain invariant raise through unhandled would
    report a client mistake as a server fault.
    """
    try:
        return Money(_decimal(payload.amount, f"{field}.amount"), payload.currency)
    except ValueError as exc:
        raise ApiError(
            422, "INVALID_AMOUNT", f"{field} is not a valid amount: {exc}", {"field": field}
        ) from exc


def _iso_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            422, "INVALID_DATE", f"{field} must be an ISO date (YYYY-MM-DD)", {"field": field}
        ) from exc


def _iso_datetime(raw: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            422, "INVALID_TIMESTAMP", f"{field} must be an ISO 8601 timestamp", {"field": field}
        ) from exc


# ----------------------------------------------------------------------
# request -> domain
# ----------------------------------------------------------------------


def _invoice(payload: Any) -> RecordedInvoice:
    try:
        return RecordedInvoice(
            external_id=payload.invoice.external_id,
            period=InvoicePeriod(
                start=_iso_date(payload.invoice.period_start, "invoice.period_start"),
                end=_iso_date(payload.invoice.period_end, "invoice.period_end"),
            ),
            currency=payload.invoice.currency,
            lines=tuple(
                InvoiceLine(
                    metric_key=line.metric_key,
                    line_type=LineType(line.line_type),
                    recorded_amount=_money(line.recorded_amount, "invoice.lines.recorded_amount"),
                )
                for line in payload.invoice.lines
            ),
            stated_total=_money(payload.invoice.stated_total, "invoice.stated_total"),
        )
    except ValueError as exc:
        raise ApiError(422, "INVALID_INVOICE", str(exc)) from exc


def _price_terms(payloads: Any) -> list[PriceTerm]:
    terms: list[PriceTerm] = []
    for entry in payloads:
        try:
            terms.append(
                PriceTerm(
                    metric_key=entry.metric_key,
                    billing_mode=BillingMode(entry.billing_mode),
                    currency=entry.currency,
                    included_units=_decimal(entry.included_units, "price_terms.included_units"),
                    unit_price=(
                        _money(entry.unit_price, "price_terms.unit_price")
                        if entry.unit_price
                        else None
                    ),
                    overage_price=(
                        _money(entry.overage_price, "price_terms.overage_price")
                        if entry.overage_price
                        else None
                    ),
                    minimum_commitment=(
                        _money(entry.minimum_commitment, "price_terms.minimum_commitment")
                        if entry.minimum_commitment
                        else None
                    ),
                    tiers=(
                        tuple(
                            Tier(
                                up_to=(
                                    _decimal(tier.up_to, "price_terms.tiers.up_to")
                                    if tier.up_to is not None
                                    else None
                                ),
                                unit_price=_money(tier.unit_price, "price_terms.tiers.unit_price"),
                            )
                            for tier in entry.tiers
                        )
                        if entry.tiers is not None
                        else None
                    ),
                )
            )
        except ValueError as exc:
            raise ApiError(422, "INVALID_CONTRACT_TERM", str(exc), {"metric_key": entry.metric_key}) from exc
    return terms


def _usage_events(payloads: Any) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    for entry in payloads:
        try:
            events.append(
                UsageEvent(
                    external_id=entry.external_id,
                    dedupe_key=entry.dedupe_key,
                    metric_key=entry.metric_key,
                    occurred_at=_iso_datetime(entry.occurred_at, "usage_events.occurred_at"),
                    quantity=_decimal(entry.quantity, "usage_events.quantity"),
                    unit=entry.unit,
                )
            )
        except ValueError as exc:
            raise ApiError(
                422, "INVALID_USAGE_EVENT", str(exc), {"external_id": entry.external_id}
            ) from exc
    return events


def _payments(payloads: Any) -> list[Payment]:
    payments: list[Payment] = []
    for entry in payloads:
        try:
            payments.append(
                Payment(
                    external_id=entry.external_id,
                    amount=_money(entry.amount, "payments.amount"),
                    allocations=tuple(
                        PaymentAllocation(
                            invoice_external_id=allocation.invoice_external_id,
                            amount=_money(allocation.amount, "payments.allocations.amount"),
                        )
                        for allocation in entry.allocations
                    ),
                )
            )
        except ValueError as exc:
            raise ApiError(
                422, "INVALID_PAYMENT", str(exc), {"external_id": entry.external_id}
            ) from exc
    return payments


def _adjustments(payloads: Any) -> list[Adjustment]:
    adjustments: list[Adjustment] = []
    for entry in payloads:
        try:
            adjustments.append(
                Adjustment(
                    external_id=entry.external_id,
                    amount=_money(entry.amount, "adjustments.amount"),
                    reason=entry.reason,
                )
            )
        except ValueError as exc:
            raise ApiError(
                422, "INVALID_ADJUSTMENT", str(exc), {"external_id": entry.external_id}
            ) from exc
    return adjustments


def _snapshots(payloads: Any) -> list[EvidenceItem]:
    """Build snapshots from the request.

    ``EvidenceItem.create`` derives the content hash. The request has no field for it,
    so a client cannot supply a hash that disagrees with the content -- which would
    let a tampered snapshot keep a trusted fingerprint and defeat staleness detection.
    """
    try:
        return [
            EvidenceItem.create(
                entry.natural_key, EvidenceType(entry.evidence_type), entry.payload
            )
            for entry in payloads
        ]
    except ValueError as exc:
        raise ApiError(422, "INVALID_SNAPSHOT", str(exc)) from exc


# ----------------------------------------------------------------------
# domain -> response
# ----------------------------------------------------------------------


def _evidence_response(items: Any) -> list[EvidenceResponse]:
    return [
        EvidenceResponse(
            natural_key=item.natural_key,
            evidence_type=item.evidence_type.value,
            content_hash=item.content_hash,
            snapshot=thaw_json(item.snapshot),
        )
        for item in items
    ]


def _money_response(amount: str | None, currency: str | None, reason: str | None = None) -> MoneyResponse:
    return MoneyResponse(amount=amount, currency=currency, not_assessable_reason=reason)


def _calculation_response(calculation: Any) -> CalculationResponse | None:
    if calculation is None:
        return None
    return CalculationResponse(
        currency=calculation.currency,
        engine_version=calculation.engine_version,
        recalculated_total=calculation.recalculated_total,
        recorded_total=calculation.recorded_total,
        difference=calculation.difference,
        outstanding=calculation.outstanding,
        allocated_payments=calculation.allocated_payments,
        net_adjustments=calculation.net_adjustments,
        is_complete=calculation.is_complete,
        is_provisional=calculation.is_provisional,
        unresolved_metrics=list(calculation.unresolved_metrics),
        invoice=thaw_json(calculation.invoice),
        balance=thaw_json(calculation.balance),
        usage_summaries=[thaw_json(u) for u in calculation.usage_summaries],
        trace=thaw_json(calculation.trace),
    )


def _investigation_response(
    investigation: InvestigationRecord, case: DisputeCase
) -> InvestigationResponse:
    """Serialise one run.

    Reviews are attached to the run they were made against, and each is marked with
    whether the evidence it saw is still the evidence on the case. A reviewer looking
    at a stale run needs to see that their own earlier annotation was made against a
    superseded view, and the server is the only place that knows.
    """
    by_target: dict[tuple[str, str], list[Any]] = {}
    for review in case.reviews:
        if review.investigation_id != investigation.id:
            continue
        by_target.setdefault((review.target_type.value, str(review.target_id)), []).append(review)

    def reviews_for(target_type: str, target_id: UUID) -> list[ReviewResponse]:
        return [
            _review_response(review, case) for review in by_target.get((target_type, str(target_id)), [])
        ]

    return InvestigationResponse(
        id=str(investigation.id),
        version=investigation.version,
        status=investigation.status.value,
        summary=investigation.summary,
        provider_name=investigation.provider_name,
        model=investigation.model,
        prompt_version=investigation.prompt_version,
        engine_version=investigation.engine_version,
        created_at=investigation.created_at,
        evidence_fingerprint=investigation.evidence_fingerprint,
        is_stale=investigation.is_stale,
        stale_at=investigation.stale_at,
        degradations=list(investigation.degradations),
        calculation=_calculation_response(investigation.calculation),
        findings=[
            FindingResponse(
                id=str(finding.id),
                code=finding.code,
                severity=finding.severity,
                category=finding.category,
                narrative=finding.narrative,
                confidence=str(finding.confidence),
                supporting_evidence=list(finding.supporting_evidence),
                reviews=reviews_for("FINDING", finding.id),
            )
            for finding in investigation.findings
        ],
        hypotheses=[
            HypothesisResponse(
                id=str(hypothesis.id),
                hypothesis_code=hypothesis.hypothesis_code,
                title=hypothesis.title,
                narrative=hypothesis.narrative,
                status=hypothesis.status,
                likelihood=None if hypothesis.likelihood is None else str(hypothesis.likelihood),
                metric_key=hypothesis.metric_key,
                supporting_evidence=list(hypothesis.supporting_evidence),
                refuting_evidence=list(hypothesis.refuting_evidence),
                impact=_money_response(
                    hypothesis.impact_amount,
                    hypothesis.impact_currency,
                    hypothesis.not_assessable_reason,
                ),
                impact_basis=hypothesis.impact_basis,
                impact_trace=thaw_json(hypothesis.impact_trace),
                reviews=reviews_for("HYPOTHESIS", hypothesis.id),
            )
            for hypothesis in investigation.hypotheses
        ],
        resolution_options=[
            ResolutionOptionResponse(
                id=str(option.id),
                option_type=option.option_type,
                title=option.title,
                rationale=option.rationale,
                requires_human_approval=option.requires_human_approval,
                hypothesis_code=option.hypothesis_code,
                supporting_evidence=list(option.supporting_evidence),
                reviews=reviews_for("RESOLUTION_OPTION", option.id),
            )
            for option in investigation.resolution_options
        ],
        evidence=_evidence_response(investigation.evidence.items),
    )


def _review_response(review: Any, case: DisputeCase) -> ReviewResponse:
    return ReviewResponse(
        id=str(review.id),
        investigation_id=str(review.investigation_id),
        target_type=review.target_type.value,
        target_id=str(review.target_id),
        action=review.action.value,
        actor_id=review.actor_id,
        actor_role=review.actor_role,
        rationale=review.rationale,
        amended_narrative=review.amended_narrative,
        evidence_fingerprint_seen=review.evidence_fingerprint_seen,
        created_at=review.created_at,
        is_against_current_evidence=(
            review.evidence_fingerprint_seen == case.evidence_fingerprint
        ),
    )


def _summary_response(case: DisputeCase) -> CaseSummaryResponse:
    current = case.current_investigation
    calculation = current.calculation if current else None
    return CaseSummaryResponse(
        id=str(case.id),
        external_id=case.external_id,
        invoice_external_id=case.invoice_external_id,
        contract_external_id=case.contract_external_id,
        status=case.status.value,
        severity=case.severity.value,
        is_stale=case.is_stale,
        evidence_fingerprint=case.evidence_fingerprint,
        version=case.version,
        created_at=case.created_at,
        investigation_count=len(case.investigations),
        current_investigation_version=None if current is None else current.version,
        outstanding=calculation.outstanding if calculation else None,
        currency=calculation.currency if calculation else None,
        finding_count=len(current.findings) if current else 0,
    )


def _detail_response(case: DisputeCase) -> DisputeDetailResponse:
    current = case.current_investigation
    summary = _summary_response(case)
    return DisputeDetailResponse(
        **summary.model_dump(),
        description=case.description,
        evidence=_evidence_response(case.evidence.items),
        current_investigation=(
            None if current is None else _investigation_response(current, case)
        ),
        investigations=[_investigation_response(run, case) for run in case.investigations],
        reviews=[_review_response(review, case) for review in case.reviews],
    )


# ----------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------


@router.post(
    "/disputes",
    status_code=status.HTTP_201_CREATED,
    response_model=DisputeDetailResponse,
    summary="Open a dispute case",
)
def create_dispute(payload: OpenDisputeRequest, service_dep: CaseServiceDep) -> DisputeDetailResponse:
    """Open a case from its invoice, contract terms, usage, payments and adjustments.

    The whole payload arrives here because this is the only point at which these
    records exist. Everything afterwards reads back what is stored, so the evidence a
    reviewer sees and the records an investigation is computed from cannot diverge.

    Returns 201 with the full detail, including the evidence fingerprint, so a client
    does not need a second request to learn what it created.
    """
    service, _ = service_dep
    command = OpenCaseCommand(
        dispute_external_id=payload.dispute_external_id,
        invoice=_invoice(payload),
        contract_external_id=payload.contract_external_id,
        dispute_text=payload.dispute_text,
        severity=CaseSeverity(payload.severity),
        description=payload.description,
        price_terms=_price_terms(payload.price_terms),
        usage_events=_usage_events(payload.usage_events),
        payments=_payments(payload.payments),
        adjustments=_adjustments(payload.adjustments),
    )
    return _detail_response(service.open_case(command))


@router.get("/disputes", response_model=CaseListResponse, summary="List dispute cases")
def list_disputes(
    service_dep: CaseServiceDep,
    status_filter: Annotated[
        CaseStatus | None, Query(alias="status", description="Filter by case status")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseListResponse:
    """Newest first. Staleness and headline figures come from persisted state."""
    service, _ = service_dep
    cases = service.list_cases(status=status_filter, limit=limit, offset=offset)
    return CaseListResponse(
        items=[_summary_response(case) for case in cases],
        total=service.count(status=status_filter),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/disputes/{dispute_id}",
    response_model=DisputeDetailResponse,
    summary="Dispute detail, including staleness and every investigation",
)
def get_dispute(dispute_id: str, service_dep: CaseServiceDep) -> DisputeDetailResponse:
    """The reviewer's main read.

    Includes *every* investigation, not just the newest, because the reason a reopened
    case exists is to compare what the system concluded then with what it concludes
    now. Each run carries the evidence it read, so the older runs remain readable in
    full after newer evidence arrives.
    """
    service, _ = service_dep
    return _detail_response(service.get(_uuid(dispute_id, "dispute_id")))


@router.post(
    "/disputes/{dispute_id}/evidence",
    response_model=DisputeDetailResponse,
    summary="Attach records or snapshots to a case",
)
def attach_evidence(
    dispute_id: str, payload: AttachEvidenceRequest, service_dep: CaseServiceDep
) -> DisputeDetailResponse:
    """Add evidence. Idempotent: re-sending the same records changes nothing.

    Attaching something new changes the case fingerprint, which marks every
    investigation that did not read it stale, and reopens the case if it was closed or
    awaiting review. The response carries the new fingerprint so a client can tell
    whether anything actually changed.
    """
    if payload.is_empty():
        raise ApiError(
            422,
            "EMPTY_ATTACHMENT",
            "Supply at least one of price_terms, usage_events, payments, adjustments "
            "or snapshots.",
        )
    service, _ = service_dep
    try:
        case = service.attach_records(
            _uuid(dispute_id, "dispute_id"),
            price_terms=_price_terms(payload.price_terms),
            usage_events=_usage_events(payload.usage_events),
            payments=_payments(payload.payments),
            adjustments=_adjustments(payload.adjustments),
            snapshots=_snapshots(payload.snapshots),
        )
    except SourceDocumentError as exc:
        raise ApiError(422, "INVALID_EVIDENCE", str(exc)) from exc
    return _detail_response(case)


@router.post(
    "/disputes/{dispute_id}/investigations",
    status_code=status.HTTP_201_CREATED,
    response_model=InvestigationResponse,
    summary="Run an investigation over the case's current evidence",
)
def create_investigation(
    dispute_id: str, service_dep: CaseServiceDep, settings_dep: SettingsDep
) -> InvestigationResponse:
    """Start a run.

    Always a new row, never a repeat of the last one: a case that already has
    investigations gets version ``n+1``, and every earlier run keeps its findings and
    its evidence. That is what makes a reopened case reviewable rather than merely
    overwritten.

    ``202`` is not used and no asynchronous job exists. The provider is deterministic
    and in-process, so the run completes before the response. Adding a real provider
    later means returning ``202`` with a location -- and doing so then, rather than
    designing for it now with a status code that would be wrong today.
    """
    service, _ = service_dep
    record = service.investigate(
        _uuid(dispute_id, "dispute_id"), prompt_version=settings_dep.prompt_version
    )
    return _investigation_response(record, service.get(_uuid(dispute_id, "dispute_id")))


@router.post(
    "/disputes/{dispute_id}/review",
    status_code=status.HTTP_201_CREATED,
    response_model=ReviewResponse,
    summary="Record a reviewer annotation on a finding, hypothesis or option",
)
def record_review(
    dispute_id: str, payload: ReviewRequest, service_dep: CaseServiceDep, actor_dep: ActorDep
) -> ReviewResponse:
    """Accept, reject, request more information, or amend.

    The annotation is refused with 409 when the investigation it targets is stale: the
    reviewer would be recording a judgement about evidence the case no longer holds.
    An ``AMEND`` stores the reviewer's replacement wording alongside the finding; it
    never overwrites it, because a finding that has been edited no longer shows what
    the model said and the reader can no longer tell a model error from a preference.

    There is no ``approve`` action. Approving is a money decision and belongs with the
    idempotency and separation-of-duties guards of §9.3.
    """
    service, _ = service_dep
    actor = actor_dep
    dispute_uuid = _uuid(dispute_id, "dispute_id")
    try:
        review = service.record_review(
            dispute_uuid,
            investigation_id=_uuid(payload.investigation_id, "investigation_id"),
            target_type=ReviewTargetType(payload.target_type),
            target_id=_uuid(payload.target_id, "target_id"),
            action=ReviewActionKind(payload.action),
            actor_id=actor.id,
            actor_role=actor.role,
            rationale=payload.rationale,
            amended_narrative=payload.amended_narrative,
        )
    except ValueError as exc:
        # ReviewAction's own invariants: AMEND without text, non-AMEND with it.
        raise ApiError(422, "INVALID_REVIEW", str(exc)) from exc
    return _review_response(review, service.get(dispute_uuid))


@router.post(
    "/disputes/{dispute_id}/reopen",
    response_model=DisputeDetailResponse,
    summary="Reopen a closed case",
)
def reopen_dispute(
    dispute_id: str, payload: ReopenRequest, service_dep: CaseServiceDep, actor_dep: ActorDep
) -> DisputeDetailResponse:
    """Move a ``RESOLVED`` or ``REJECTED`` case back to ``REOPENED``.

    A reason is mandatory and is appended to the case description, because "reopened"
    without a reason is not something a later reader can learn anything from. No
    investigation is started: reopening and re-running are separate decisions, and
    conflating them would hide which one actually happened.
    """
    service, _ = service_dep
    actor = actor_dep
    dispute_uuid = _uuid(dispute_id, "dispute_id")
    case = service.reopen(dispute_uuid, reason=payload.reason, actor_id=actor.id)
    return _detail_response(case)


@router.get("/capabilities", summary="Provider mode, store kind and feature flags")
def capabilities(
    service_dep: CaseServiceDep, settings_dep: SettingsDep, actor_dep: ActorDep
) -> dict[str, Any]:
    """What this build can actually do. Never any configuration *values* (NEP-009).

    ``store`` is here so the reviewer UI can say which backend answered instead of
    leaving the user to infer it, and ``persistence_is_durable`` says plainly whether
    a case would survive a restart. A developer running the memory store should not
    have to read the settings file to know their demo data is ephemeral.
    """
    _, kind = service_dep
    return {
        "llm_provider": settings_dep.llm_provider,
        "llm_model": settings_dep.llm_model,
        "prompt_version": settings_dep.prompt_version,
        "store": kind,
        "persistence_is_durable": kind == "postgres",
        "identity": {
            "mode": "demo-header",
            "is_authentication": False,
            "actor_id": actor_dep.id,
            "actor_role": actor_dep.role,
        },
        "features": {
            "adjustments": False,
            "approval": False,
            "reopen": True,
            "evidence_attachment": True,
            "review_annotations": True,
        },
    }