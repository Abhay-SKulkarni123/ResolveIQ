"""The dispute case workflow: open, investigate, annotate, reopen.

Everything a reviewer does goes through here, and nothing here touches SQL. The
service is the only place that knows the order of operations; the aggregate in
:mod:`app.domain.cases` knows the rules; the repository knows the storage.

The operations, and the reasoning behind each
-----------------------------------------------

``open_case``
    Builds the evidence bundle from the ingest payload and stores the case. The
    bundle is built *before* the case is persisted, so a case never exists without
    the evidence it was opened from. That ordering also means the fingerprint on
    the stored row is the fingerprint of real snapshots rather than of an empty
    bundle filled in later.

``investigate``
    Loads the case, rebuilds the typed records from its stored ingest payload, and
    hands them to Phase 3's :func:`app.services.investigation.investigate`. It then
    stores two things: the deterministic :class:`CalculationRecord` and the
    investigation that referenced it. They are separate rows because they have
    different authority -- one came from the engine, the other from a model -- and
    a reviewer asking "was the number right?" must not have to trust the run.

    The case's status becomes ``AWAITING_REVIEW``. The case is *not* resolved:
    nothing here decides anything.

``attach_evidence``
    Idempotent. Attaching a snapshot that is already attached leaves the case
    untouched, and therefore leaves its fingerprint, status and version untouched.
    Attaching a new one stamps every run that did not read it as stale and reopens
    the case if it was closed or awaiting review.

``record_review``
    Records a reviewer's accept / reject / request-more-info / amend against a
    finding, hypothesis or option. It refuses when the investigation is stale,
    because the annotation would then be a judgement about evidence the reviewer
    can no longer see. It never edits the target.

``decide``
    Records the case-level closing verdict (``RESOLVED`` / ``REJECTED``) against
    the run currently under review, after that run has been reviewed at least once
    and over evidence that is still attached. It moves no money; executing an
    amount is the idempotent resolution phase.

``reopen``
    The only way out of ``RESOLVED`` or ``REJECTED``, and it needs a reason.

Identity
--------
``actor_id`` is recorded, never checked *by the domain*. Phase 4 has no
authentication and must not pretend to: the development identity arrives in a
request header and is stored as a label on each annotation so that a reviewer can
see who said what. Separation of duties starts at the API boundary -- the closing
verdict (``decide``) is gated to the reviewer role by the route, not by the domain
-- while the analyst-who-approves restriction and idempotent execution are a later
phase; faking them with a column that is never enforced would be worse than not
having them. The domain does hold one judgement about the actor: opening,
annotating and deciding all refuse a blank ``actor_id``, so a stored record always
names the person who made it.

The audit trail
---------------
Every operation above appends one :class:`app.domain.audit.AuditEvent` once it has
saved, so the case carries a single ordered answer to "what happened to this, and
who did it?". The event is written in the same transaction as the change, which is
what makes the trail trustworthy: it can never outlive a rolled-back change or miss a
committed one. Operations that change nothing -- a replayed idempotent request, a
no-op evidence attach -- record nothing, because the trail is a record of what
happened, not of what was asked for.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from app.domain.audit import AuditEvent, AuditEventType
from app.domain.billing import Adjustment, Payment, PriceTerm, RecordedInvoice, UsageEvent
from app.domain.cases import (
    CalculationRecord,
    CaseSeverity,
    CaseStatus,
    CaseTransitionError,
    DecisionKind,
    DisputeCase,
    DisputeDecision,
    InvestigationRecord,
    InvestigationRunStatus,
    ReviewAction,
    ReviewActionKind,
    ReviewTargetType,
    StoredFinding,
    StoredHypothesis,
    StoredResolutionOption,
)
from app.domain.evidence import EvidenceItem
from app.domain.hypotheses import HypothesisCode
from app.domain.json_frozen import thaw_json
from app.logging_config import log_event
from app.ports.cases import CaseConflictError, CaseRepository
from app.ports.llm import LlmProvider
from app.services.citations import EvidenceCitationValidator
from app.services.investigation import InvestigationInputs, InvestigationResult, investigate
from app.services.source_document import (
    attach_snapshot,
    build_bundle_from_document,
    from_source_document,
    merge_records,
    to_source_document,
)

__all__ = ["CaseNotInvestigableError", "CaseService", "OpenCaseCommand"]

LOGGER = logging.getLogger(__name__)


class CaseNotInvestigableError(ValueError):
    """The case cannot be investigated, and the reason is about the case.

    Distinct from a ``ValueError`` about a field so the API can answer 422 with a
    message naming the case, rather than a generic validation error the frontend
    would render against the wrong input.
    """


class OpenCaseCommand:
    """Everything needed to open a case.

    A plain container rather than nine keyword arguments on the method, because the
    argument list is already long enough to be reordered by accident and a swapped
    ``payments``/``adjustments`` pair would be silent.
    """

    __slots__ = (
        "adjustments",
        "contract_external_id",
        "description",
        "dispute_external_id",
        "dispute_text",
        "invoice",
        "payments",
        "price_terms",
        "severity",
        "usage_events",
    )

    def __init__(
        self,
        *,
        dispute_external_id: str,
        invoice: RecordedInvoice,
        contract_external_id: str,
        dispute_text: str,
        severity: CaseSeverity = CaseSeverity.MEDIUM,
        price_terms: Sequence[PriceTerm] = (),
        usage_events: Sequence[UsageEvent] = (),
        payments: Sequence[Payment] = (),
        adjustments: Sequence[Adjustment] = (),
        description: str = "",
    ) -> None:
        self.dispute_external_id = dispute_external_id
        self.invoice = invoice
        self.contract_external_id = contract_external_id
        self.dispute_text = dispute_text
        self.severity = severity
        self.price_terms = list(price_terms)
        self.usage_events = list(usage_events)
        self.payments = list(payments)
        self.adjustments = list(adjustments)
        self.description = description or dispute_text


class CaseService:
    """Application service for dispute cases."""

    def __init__(
        self,
        repository: CaseRepository,
        provider: LlmProvider,
        *,
        max_attempts: int = 1,
        investigation_ttl_hours: int | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._repository = repository
        self._provider = provider
        #: How many times a transient provider failure is retried before the run is
        #: recorded as its deterministic result. The default of 1 keeps a directly
        #: constructed service honest (no invisible retries); the API wires the
        #: configured value through, so production retries and a unit test does not.
        self._max_attempts = max_attempts
        #: How long a pending run may sit unfinished before recovery reaps it. ``None``
        #: means never expire, which is a legitimate unbounded configuration.
        self._investigation_ttl_hours = investigation_ttl_hours

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get(self, dispute_id: UUID) -> DisputeCase:
        return self._repository.get(dispute_id)

    def get_by_external_id(self, external_id: str) -> DisputeCase:
        return self._repository.get_by_external_id(external_id)

    def list_cases(
        self, *, status: CaseStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[DisputeCase, ...]:
        return self._repository.list(status=status, limit=limit, offset=offset)

    def count(self, *, status: CaseStatus | None = None) -> int:
        return self._repository.count(status=status)

    def list_audit(
        self, dispute_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[AuditEvent, ...]:
        """A case's audit trail, oldest-first.

        The case is loaded first, but only to turn a missing dispute into
        :class:`CaseNotFoundError` rather than an empty page. An empty list for a
        case that does not exist would read as "nothing happened to it", which is a
        different and wrong answer.
        """
        self._repository.get(dispute_id)
        return self._repository.list_audit(dispute_id, limit=limit, offset=offset)

    def count_audit(self, dispute_id: UUID) -> int:
        self._repository.get(dispute_id)
        return self._repository.count_audit(dispute_id)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def open_case(
        self,
        command: OpenCaseCommand,
        *,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> DisputeCase:
        """Open a case and store its evidence.

        The bundle is assembled first so the case is never persisted without the
        evidence it was opened from, and so the fingerprint written to the row is
        the fingerprint of real snapshots.

        A duplicate ``dispute_external_id`` raises rather than reopening the
        existing case: ingesting the same ticket twice is a bug in the caller, and
        silently merging two complaints into one case would destroy one of them.

        An ``idempotency_key`` already seen returns the stored case instead: the
        check runs *before* the external-id guard because a retry of a successful
        open carries the same external id, and raising on it would turn the replay
        into a spurious conflict.
        """
        if idempotency_key is not None:
            replayed = self._repository.find_by_idempotency_key(idempotency_key)
            if replayed is not None:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "case.open.replayed",
                    dispute_id=str(replayed.id),
                    external_id=replayed.external_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
                return replayed
        if self._repository.find_by_external_id(command.dispute_external_id) is not None:
            raise CaseConflictError(
                f"dispute {command.dispute_external_id!r} already exists"
            )

        source_document = to_source_document(
            invoice=command.invoice,
            price_terms=command.price_terms,
            usage_events=command.usage_events,
            payments=command.payments,
            adjustments=command.adjustments,
            dispute_text=command.dispute_text,
        )
        # Built from the document rather than from the command, so that the evidence
        # on a freshly opened case and the evidence a re-run would collect are
        # produced by the same function. If they were built separately, the two could
        # disagree and the staleness check would be comparing things that were never
        # meant to match.
        bundle = build_bundle_from_document(
            source_document,
            dispute_external_id=command.dispute_external_id,
            contract_external_id=command.contract_external_id,
        )
        case = DisputeCase(
            id=uuid4(),
            external_id=command.dispute_external_id,
            invoice_external_id=command.invoice.external_id,
            contract_external_id=command.contract_external_id,
            status=CaseStatus.OPEN,
            severity=command.severity,
            description=command.description,
            evidence=bundle,
            created_at=_now(),
            source_document=source_document,
            idempotency_key=idempotency_key,
        )
        self._repository.save(case, expected_version=0)
        self._append_audit(
            event_type=AuditEventType.CASE_CREATED,
            dispute_id=case.id,
            occurred_at=case.created_at,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=case.version,
            correlation_id=idempotency_key,
            payload={
                "external_id": case.external_id,
                "evidence_fingerprint": case.evidence_fingerprint,
            },
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.opened",
            dispute_id=str(case.id),
            external_id=case.external_id,
            actor_id=actor_id,
            actor_role=actor_role,
            severity=case.severity.value,
            evidence_count=len(case.evidence.items),
        )
        return case

    def investigate(
        self,
        dispute_id: UUID,
        *,
        prompt_version: str,
        validator: EvidenceCitationValidator | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> InvestigationRecord:
        """Run an investigation and store its calculation and findings separately.

        Phase 3 is called with the records rebuilt from the case's stored ingest
        payload, not with anything the caller supplies. That is deliberate: the
        evidence was hashed from those records, so re-supplying them would let a
        caller run an investigation over inputs that do not match the evidence the
        case is showing, and the fingerprint would then attest to nothing.

        A seen ``idempotency_key`` returns the run already produced for it. The lookup
        happens before the state guard because a successful run leaves the case in
        ``AWAITING_REVIEW``, where a fresh run is correctly refused -- the replay must
        be recognised as the same request, not a second one.
        """
        case = self._repository.get(dispute_id)
        if idempotency_key is not None:
            existing = next(
                (run for run in case.investigations if run.idempotency_key == idempotency_key),
                None,
            )
            if existing is not None:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "case.investigate.replayed",
                    dispute_id=str(case.id),
                    investigation_id=str(existing.id),
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
                return existing
        if not case.is_open_for_investigation:
            raise CaseTransitionError(
                f"case {case.external_id} is {case.status.value}; only an open case can be "
                "investigated"
            )
        if not case.can_investigate:
            raise CaseNotInvestigableError(
                f"case {case.external_id} has no stored invoice, so there is nothing to "
                "recalculate"
            )

        inputs = self._inputs_for(case, prompt_version)
        pending = self._pending_record(case, idempotency_key=idempotency_key)
        pending_case = case.with_pending_investigation(pending)
        self._repository.begin_investigation(pending_case, expected_version=case.version)
        return self._run_and_complete(
            pending_case,
            pending,
            inputs,
            validator=validator,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            actor_role=actor_role,
        )

    def _inputs_for(self, case: DisputeCase, prompt_version: str) -> InvestigationInputs:
        """Rebuild the Phase 3 inputs from the case's stored ingest payload.

        ``investigate`` derives its own usage summaries from these records and the
        invoice period. Passing the summaries in would create a second source of truth
        for the same figures, free to drift from the evidence's own hash.
        """
        records = from_source_document(dict(case.source_document))
        return InvestigationInputs(
            dispute_external_id=case.external_id,
            invoice=records["invoice"],
            price_terms=records["price_terms"],
            usage_events=records["usage_events"],
            payments=records["payments"],
            adjustments=records["adjustments"],
            contract_external_id=case.contract_external_id or "",
            dispute_text=_dispute_text_of(case),
            prompt_version=prompt_version,
        )

    def _pending_record(
        self, case: DisputeCase, *, idempotency_key: str | None
    ) -> InvestigationRecord:
        """Build the durable envelope written before the workflow runs (ADR-008).

        It carries the evidence it is about to read and its fingerprint, so a recovery
        can tell whether the evidence has since moved. Its stage map is empty; the
        terminal write records where the run actually stopped.
        """
        created_at = _now()
        expires_at = (
            None
            if self._investigation_ttl_hours is None
            else created_at + timedelta(hours=self._investigation_ttl_hours)
        )
        return InvestigationRecord(
            id=uuid4(),
            dispute_id=case.id,
            version=case.next_investigation_version,
            status=InvestigationRunStatus.PENDING,
            evidence_fingerprint=case.evidence_fingerprint,
            created_at=created_at,
            summary="",
            provider_name=self._provider.name,
            model=self._provider.model,
            prompt_version="",
            engine_version="",
            evidence=case.evidence,
            stage_status={"overall": InvestigationRunStatus.PENDING.value},
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

    def _run_with_retries(
        self,
        inputs: InvestigationInputs,
        *,
        validator: EvidenceCitationValidator | None,
        actor_id: str | None,
        actor_role: str | None,
    ) -> tuple[InvestigationResult, int]:
        """Run the workflow, retrying only transient failures, counting the attempts.

        ``investigate`` never raises for a provider failure; it returns ``retryable`` on
        the result. A timeout or a provider error is retried up to the budget; a
        misconfiguration or an unparseable answer is not, because it would fail
        identically every time. When the budget is spent the last result stands, so the
        deterministic figures are never thrown away over a flaky model.
        """
        attempts = 1
        result = investigate(inputs, self._provider, validator=validator)
        while result.retryable and attempts < self._max_attempts:
            log_event(
                LOGGER,
                logging.WARNING,
                "case.investigation.retrying",
                dispute_id=inputs.dispute_external_id,
                attempt=attempts,
                max_attempts=self._max_attempts,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            attempts += 1
            result = investigate(inputs, self._provider, validator=validator)
        if result.retryable:
            log_event(
                LOGGER,
                logging.WARNING,
                "case.investigation.retries_exhausted",
                dispute_id=inputs.dispute_external_id,
                attempts=attempts,
                max_attempts=self._max_attempts,
                actor_id=actor_id,
                actor_role=actor_role,
            )
        return result, attempts

    def _run_and_complete(
        self,
        case: DisputeCase,
        pending: InvestigationRecord,
        inputs: InvestigationInputs,
        *,
        validator: EvidenceCitationValidator | None,
        idempotency_key: str | None,
        actor_id: str | None,
        actor_role: str | None,
    ) -> InvestigationRecord:
        """Run the workflow over ``pending`` and replace it with the terminal run."""
        result, attempts = self._run_with_retries(
            inputs, validator=validator, actor_id=actor_id, actor_role=actor_role
        )
        if result.evidence.fingerprint != case.evidence_fingerprint:
            # Should be impossible: the evidence is derived from the same records the
            # stored bundle was built from. Failing the pending run is better than
            # leaving it dangling or storing a run whose fingerprint does not match.
            self._fail_pending(
                case,
                pending,
                reason="the evidence changed while the investigation was running",
                actor_id=actor_id,
                actor_role=actor_role,
            )
            raise CaseTransitionError(
                f"investigation produced evidence {result.evidence.fingerprint} that does not "
                f"match the case's {case.evidence_fingerprint}; the run was failed, not stored"
            )
        record = self._to_record(
            case, result, pending=pending, attempts=attempts, idempotency_key=idempotency_key
        )
        updated = case.complete_investigation(record)
        self._repository.save(updated, expected_version=case.version)
        self._append_audit(
            event_type=AuditEventType.INVESTIGATION_COMPLETED,
            dispute_id=case.id,
            occurred_at=record.created_at,
            investigation_id=record.id,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=updated.version,
            correlation_id=idempotency_key,
            payload={
                "investigation_version": record.version,
                "evidence_fingerprint": record.evidence_fingerprint,
                "status": record.status.value,
                "attempts": attempts,
            },
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.investigated",
            dispute_id=str(case.id),
            investigation_id=str(record.id),
            investigation_version=record.version,
            actor_id=actor_id,
            actor_role=actor_role,
            provider=record.provider_name,
            model=record.model,
            prompt_version=record.prompt_version,
            outcome=record.status.value,
            finding_count=len(record.findings),
        )
        return record

    def _fail_pending(
        self,
        case: DisputeCase,
        pending: InvestigationRecord,
        *,
        reason: str,
        actor_id: str | None,
        actor_role: str | None,
    ) -> InvestigationRecord:
        """Bring a pending run to the ``FAILED`` tombstone and record why.

        Committed on its own before this returns. The marker this replaces was written
        ahead and committed in :meth:`CaseRepository.begin_investigation` (ADR-008), so
        the terminal write has to be committed too; when the caller then raises -- the
        fingerprint-mismatch paths -- the request-level rollback would otherwise undo
        the tombstone and strand the case in ``INVESTIGATING`` behind a pending run no
        later request can get past.
        """
        failed = case.fail_investigation(pending.id, reason=reason)
        self._repository.save(failed, expected_version=case.version)
        record = next(run for run in failed.investigations if run.id == pending.id)
        self._append_audit(
            event_type=AuditEventType.INVESTIGATION_FAILED,
            dispute_id=case.id,
            occurred_at=_now(),
            investigation_id=pending.id,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=failed.version,
            payload={"investigation_version": record.version, "reason": reason},
        )
        self._repository.commit()
        log_event(
            LOGGER,
            logging.WARNING,
            "case.investigation.failed",
            dispute_id=str(case.id),
            investigation_id=str(pending.id),
            investigation_version=record.version,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
            entity_version=failed.version,
        )
        return record

    def attach_records(
        self,
        dispute_id: UUID,
        *,
        price_terms: Sequence[PriceTerm] = (),
        usage_events: Sequence[UsageEvent] = (),
        payments: Sequence[Payment] = (),
        adjustments: Sequence[Adjustment] = (),
        snapshots: Sequence[EvidenceItem] = (),
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> DisputeCase:
        """Add records or snapshots to a case, idempotently.

        Two kinds of thing arrive here and they are handled differently on purpose.

        *Typed records* (a payment, a usage event, a contract term) are merged into
        the case's ingest payload **and** the bundle is rebuilt from the result. That
        is what keeps the guarantee in :meth:`investigate` true: a re-run collects
        from the document, so a document that was not updated would produce an
        investigation over evidence the case is not showing -- and the staleness check
        would correctly refuse it, leaving the case permanently unable to be
        re-investigated.

        *Free-form snapshots* (a scanned receipt, a call note) are appended to the
        payload as the hashed snapshots they are, because there is no typed record to
        rebuild them from. They join the fingerprint, so they still stale earlier runs
        exactly as a record would.

        Returns the case unchanged, without writing, when nothing was new.
        """
        case = self._repository.get(dispute_id)
        document = thaw_json(case.source_document)
        if not document or not document.get("present"):
            raise CaseNotInvestigableError(
                f"case {case.external_id} has no stored invoice, so evidence cannot be attached"
            )
        if price_terms or usage_events or payments or adjustments:
            document = merge_records(
                document,
                price_terms=list(price_terms),
                usage_events=list(usage_events),
                payments=list(payments),
                adjustments=list(adjustments),
            )
        for snapshot in snapshots:
            document = attach_snapshot(document, snapshot)

        bundle = build_bundle_from_document(
            document,
            dispute_external_id=case.external_id,
            contract_external_id=case.contract_external_id or "",
        )
        # Merge into the existing bundle rather than replacing it. ``with_evidence``
        # de-duplicates, and the rebuild may legitimately omit a snapshot that was
        # attached directly, so unioning keeps every citable snapshot reachable.
        updated = case.with_evidence(
            [*bundle.items, *case.evidence.items]
        ).with_source_document(document)
        if updated.version == case.version:
            log_event(
                LOGGER,
                logging.INFO,
                "case.evidence.unchanged",
                dispute_id=str(case.id),
                actor_id=actor_id,
                actor_role=actor_role,
            )
            return case
        self._repository.save(updated, expected_version=case.version)
        self._append_audit(
            event_type=AuditEventType.EVIDENCE_ATTACHED,
            dispute_id=case.id,
            occurred_at=_now(),
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=updated.version,
            payload={
                "evidence_fingerprint": updated.evidence_fingerprint,
                "evidence_count": len(updated.evidence.items),
            },
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.evidence.attached",
            dispute_id=str(case.id),
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=updated.version,
            evidence_count=len(updated.evidence.items),
            evidence_fingerprint=updated.evidence_fingerprint,
        )
        return updated

    def record_review(
        self,
        dispute_id: UUID,
        *,
        investigation_id: UUID,
        target_type: ReviewTargetType,
        target_id: UUID,
        action: ReviewActionKind,
        actor_id: str,
        actor_role: str,
        rationale: str,
        amended_narrative: str | None = None,
        idempotency_key: str | None = None,
    ) -> ReviewAction:
        """Record a reviewer's annotation on one finding, hypothesis or option.

        A seen ``idempotency_key`` returns the annotation already recorded for it, so
        a retried request does not store the same judgement twice.
        """
        case = self._repository.get(dispute_id)
        if idempotency_key is not None:
            existing = next(
                (review for review in case.reviews if review.idempotency_key == idempotency_key),
                None,
            )
            if existing is not None:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "case.review.replayed",
                    dispute_id=str(case.id),
                    review_id=str(existing.id),
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
                return existing
        review = ReviewAction(
            id=uuid4(),
            dispute_id=case.id,
            investigation_id=investigation_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor_id=actor_id,
            actor_role=actor_role,
            evidence_fingerprint_seen=case.evidence_fingerprint,
            created_at=_now(),
            rationale=rationale,
            amended_narrative=amended_narrative,
            idempotency_key=idempotency_key,
        )
        updated = case.with_review(review)
        self._repository.save(updated, expected_version=case.version)
        self._append_audit(
            event_type=AuditEventType.REVIEW_RECORDED,
            dispute_id=case.id,
            occurred_at=review.created_at,
            investigation_id=review.investigation_id,
            actor_id=review.actor_id,
            actor_role=review.actor_role,
            entity_version=updated.version,
            correlation_id=idempotency_key,
            payload={
                "action": review.action.value,
                "target_type": review.target_type.value,
                "target_id": str(review.target_id),
            },
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.review.recorded",
            dispute_id=str(case.id),
            review_id=str(review.id),
            investigation_id=str(review.investigation_id),
            actor_id=review.actor_id,
            actor_role=review.actor_role,
            action=review.action.value,
            target_type=review.target_type.value,
        )
        return review

    def decide(
        self,
        dispute_id: UUID,
        *,
        decision: DecisionKind,
        actor_id: str,
        actor_role: str,
        rationale: str,
        idempotency_key: str | None = None,
    ) -> DisputeCase:
        """Close the case with a reviewer's verdict: resolved or rejected.

        The verdict targets the run currently under review, in one optimistic
        transaction. No money moves here: ``DisputeDecision`` is the recorded
        judgement that closes the lifecycle, and executing an amount is the
        idempotent resolution phase with its own guards.

        A seen ``idempotency_key`` returns the case carrying the verdict already
        recorded for it, before the ``AWAITING_REVIEW`` guard is consulted -- a
        decided case is no longer awaiting review, so the retry has to be matched
        here or it would be refused as an illegal second decision.
        """
        case = self._repository.get(dispute_id)
        if idempotency_key is not None and any(
            d.idempotency_key == idempotency_key for d in case.decisions
        ):
            log_event(
                LOGGER,
                logging.INFO,
                "case.decision.replayed",
                dispute_id=str(case.id),
                actor_id=actor_id,
                actor_role=actor_role,
            )
            return case
        current_investigation_id = case.current_investigation_id
        if current_investigation_id is None:
            raise CaseTransitionError(
                f"case {case.external_id} has no investigation under review to decide"
            )
        decision_record = DisputeDecision(
            id=uuid4(),
            dispute_id=case.id,
            investigation_id=current_investigation_id,
            decision=decision,
            actor_id=actor_id,
            actor_role=actor_role,
            evidence_fingerprint_seen=case.evidence_fingerprint,
            created_at=_now(),
            rationale=rationale,
            idempotency_key=idempotency_key,
        )
        updated = case.decided(decision_record)
        self._repository.save(updated, expected_version=case.version)
        self._append_audit(
            event_type=AuditEventType.DECISION_RECORDED,
            dispute_id=case.id,
            occurred_at=decision_record.created_at,
            investigation_id=decision_record.investigation_id,
            actor_id=decision_record.actor_id,
            actor_role=decision_record.actor_role,
            entity_version=updated.version,
            correlation_id=idempotency_key,
            payload={"decision": decision_record.decision.value},
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.decided",
            dispute_id=str(case.id),
            investigation_id=str(decision_record.investigation_id),
            actor_id=decision_record.actor_id,
            actor_role=decision_record.actor_role,
            decision=decision_record.decision.value,
        )
        return updated

    def reopen(
        self,
        dispute_id: UUID,
        *,
        reason: str,
        actor_id: str,
        actor_role: str | None = None,
    ) -> DisputeCase:
        """Reopen a closed case.

        A reopen does not itself start an investigation. It moves the case to
        ``REOPENED``, which is the state that makes a fresh run legal; the run is a
        separate, explicit action so that a reviewer reopening a case and a reviewer
        re-running it are two decisions that can be recorded separately.
        """
        case = self._repository.get(dispute_id)
        reopened_at = _now()
        updated = case.reopened(reason=reason, at=reopened_at)
        self._repository.save(updated, expected_version=case.version)
        self._append_audit(
            event_type=AuditEventType.CASE_REOPENED,
            dispute_id=case.id,
            occurred_at=reopened_at,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=updated.version,
            payload={"reason": reason},
        )
        log_event(
            LOGGER,
            logging.INFO,
            "case.reopened",
            dispute_id=str(case.id),
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=updated.version,
        )
        return updated

    # ------------------------------------------------------------------
    # recovery (Part 7)
    # ------------------------------------------------------------------

    def list_recoverable_investigations(
        self, *, older_than: datetime
    ) -> tuple[tuple[UUID, UUID], ...]:
        """``(dispute_id, investigation_id)`` for runs pending beyond the window."""
        return self._repository.list_pending_investigations(started_before=older_than)

    def fail_interrupted_investigation(
        self,
        dispute_id: UUID,
        investigation_id: UUID,
        *,
        reason: str = "the investigation was interrupted and never finished",
    ) -> InvestigationRecord:
        """Move one abandoned pending run to ``FAILED`` and record it.

        This is the recovery sweep's terminal action: a pending row that outlived its
        window belongs to a process that is not coming back, and a reviewer must not be
        left with a case stuck in ``INVESTIGATING``. The case returns to ``OPEN`` so it
        can be investigated again.
        """
        case = self._repository.get(dispute_id)
        return self._fail_pending(
            case,
            next(run for run in case.investigations if run.id == investigation_id),
            reason=reason,
            actor_id=None,
            actor_role=None,
        )

    def resume_investigation(
        self,
        dispute_id: UUID,
        *,
        prompt_version: str,
        investigation_id: UUID | None = None,
        validator: EvidenceCitationValidator | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> InvestigationRecord:
        """Re-run a pending investigation and complete it in place.

        Used by the recovery command for a pending run that is still within its window
        -- a process restart, say. No second marker is written; the existing pending row
        is completed, so the case never accumulates duplicate runs and the run keeps its
        original version and creation time. The evidence is re-read from the case, so a
        resume over evidence that has since moved fails the run rather than storing a
        stale one.
        """
        case = self._repository.get(dispute_id)
        pending = next(
            (
                run
                for run in case.investigations
                if run.is_pending and (investigation_id is None or run.id == investigation_id)
            ),
            None,
        )
        if pending is None:
            raise CaseTransitionError(
                f"case {case.external_id} has no pending investigation to resume"
            )
        if pending.evidence_fingerprint != case.evidence_fingerprint:
            # The run started over evidence that has since moved. Completing it now would
            # store a result under a fingerprint the pending row never agreed to, so the
            # run is failed and the case reopened for a fresh investigation instead.
            self._fail_pending(
                case,
                pending,
                reason="the evidence changed before the investigation was resumed",
                actor_id=actor_id,
                actor_role=actor_role,
            )
            raise CaseTransitionError(
                f"the evidence changed before investigation {pending.id} was resumed; "
                "the run was failed, not stored"
            )
        if not case.can_investigate:
            raise CaseNotInvestigableError(
                f"case {case.external_id} has no stored invoice, so there is nothing to "
                "recalculate"
            )
        inputs = self._inputs_for(case, prompt_version)
        log_event(
            LOGGER,
            logging.INFO,
            "case.investigation.resumed",
            dispute_id=str(case.id),
            investigation_id=str(pending.id),
            resume_from=pending.resume_from,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        return self._run_and_complete(
            case,
            pending,
            inputs,
            validator=validator,
            idempotency_key=pending.idempotency_key,
            actor_id=actor_id,
            actor_role=actor_role,
        )

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _append_audit(
        self,
        *,
        event_type: AuditEventType,
        dispute_id: UUID,
        occurred_at: datetime,
        investigation_id: UUID | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
        entity_version: int | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one event, after the change that produced it has been saved.

        Ordering is deliberate: the audit row is written only once the operation
        itself has succeeded, so a trail never claims a change that a later failure
        rolled back. Every write path that reaches here has already persisted, and
        none of them catches a save error to reach it anyway.

        The caller decides whether an event is worth recording at all -- an
        idempotent replay and a no-op attach both return before this is called, so
        the trail describes what happened, not what was requested.
        """
        self._repository.append_audit(
            AuditEvent.record(
                event_type=event_type,
                dispute_id=dispute_id,
                occurred_at=occurred_at,
                investigation_id=investigation_id,
                actor_id=actor_id,
                actor_role=actor_role,
                entity_version=entity_version,
                correlation_id=correlation_id,
                payload=payload,
            )
        )

    # ------------------------------------------------------------------
    # mapping Phase 3 output onto stored records
    # ------------------------------------------------------------------

    def _to_record(
        self,
        case: DisputeCase,
        result: InvestigationResult,
        *,
        pending: InvestigationRecord,
        attempts: int = 1,
        idempotency_key: str | None = None,
    ) -> InvestigationRecord:
        """Split one Phase 3 result into a calculation record and a run record.

        The split is the point. ``CalculationRecord`` comes entirely from the
        deterministic engine; ``InvestigationRecord`` carries what the model said
        and a pointer to the calculation via its own foreign key. A reviewer can
        therefore trust the arithmetic without trusting the analysis, which is the
        only way the interface can show both without implying the second vouches
        for the first.

        The run's identity (id, version, created_at, expires_at) comes from the
        pending marker it replaces, not from a fresh ``uuid4`` or ``_now``: the row
        already exists, and the terminal write must land on exactly that row.
        """
        recalculation = result.recalculation
        balance = result.balance
        calculation = CalculationRecord(
            currency=recalculation.currency,
            engine_version=result.engine_version,
            recalculated_total=recalculation.calculated_total.as_string(),
            is_complete=recalculation.is_complete,
            # Provisionality is the *balance's* flag, not the recalculation's. The
            # recalculation is only ever partial (``is_complete``); the balance built
            # on top of it is the figure a reviewer might act on, and it is that one
            # which has to be labelled as a lower bound.
            is_provisional=balance.is_provisional,
            recorded_total=_maybe(recalculation.recorded_total),
            # ``InvoiceRecalculation`` already carries the difference. Computing it
            # again here would be a second definition of the same number, free to
            # drift from the engine's.
            difference=_maybe(recalculation.difference),
            outstanding=_maybe(balance.outstanding),
            allocated_payments=_maybe(balance.allocated_payments),
            net_adjustments=_maybe(balance.net_adjustments),
            unresolved_metrics=tuple(balance.unresolved_metrics or recalculation.unresolved_metrics),
            trace=_trace_document(recalculation),
            invoice=_recalculation_document(recalculation),
            balance=_money_document(balance),
            usage_summaries=tuple(_usage_summary_document(s) for s in result.usage_summaries),
        )

        interpretation = result.interpretation
        findings = (
            tuple(
                StoredFinding(
                    id=uuid4(),
                    code=finding.code,
                    severity=finding.severity.value,
                    category=finding.category,
                    narrative=finding.narrative,
                    confidence=finding.confidence,
                    supporting_evidence=tuple(finding.supporting_evidence),
                )
                for finding in interpretation.response.findings
            )
            if interpretation is not None
            else ()
        )
        impacts = {impact.hypothesis_code: impact for impact in result.impacts}
        hypotheses = (
            tuple(
                _to_hypothesis(hypothesis, impacts.get(_code(hypothesis.hypothesis_code)))
                for hypothesis in interpretation.response.hypotheses
            )
            if interpretation is not None
            else ()
        )
        options = (
            tuple(
                StoredResolutionOption(
                    id=uuid4(),
                    option_type=option.option_type.value,
                    title=option.title,
                    rationale=option.rationale,
                    requires_human_approval=requires_approval(option.option_type),
                    hypothesis_code=option.hypothesis_code,
                    supporting_evidence=tuple(option.supporting_evidence),
                )
                for option in interpretation.response.resolution_options
            )
            if interpretation is not None
            else ()
        )

        return InvestigationRecord(
            id=pending.id,
            dispute_id=case.id,
            version=pending.version,
            status=InvestigationRunStatus.from_investigation_status(result.status),
            evidence_fingerprint=result.evidence.fingerprint,
            created_at=_as_utc(pending.created_at),
            summary=interpretation.response.summary if interpretation is not None else "",
            provider_name=result.provenance.provider_name,
            model=result.provenance.model,
            prompt_version=result.provenance.prompt_version,
            engine_version=result.engine_version,
            evidence=result.evidence,
            calculation=calculation,
            findings=findings,
            hypotheses=hypotheses,
            resolution_options=options,
            degradations=result.degradations,
            stage_status={**dict(result.stage_status), "attempts": attempts},
            expires_at=pending.expires_at,
            idempotency_key=idempotency_key,
        )


def _to_hypothesis(hypothesis: Any, impact: Any) -> StoredHypothesis:
    """Pair a model hypothesis with the engine's assessment of it.

    The impact fields come from ``impact``, never from ``hypothesis``. The model's
    hypothesis has no amount field to take them from, which is the schema-level
    guarantee from Phase 3 that it cannot invent money -- and this is where that
    guarantee is cashed out.
    """
    if impact is None:
        # The model proposed a cause the engine cannot price. That is not an error;
        # it is recorded as unassessable with the reason, which is more honest than
        # omitting the hypothesis or showing a zero.
        return StoredHypothesis(
            id=uuid4(),
            hypothesis_code=str(hypothesis.hypothesis_code.value),
            title=hypothesis.title,
            narrative=hypothesis.narrative,
            likelihood=hypothesis.likelihood,
            status=str(hypothesis.status.value),
            supporting_evidence=tuple(hypothesis.supporting_evidence),
            refuting_evidence=tuple(hypothesis.refuting_evidence),
            metric_key=hypothesis.metric_key,
            not_assessable_reason="the deterministic engine cannot price this hypothesis",
        )
    return StoredHypothesis(
        id=uuid4(),
        hypothesis_code=str(impact.hypothesis_code.value),
        title=hypothesis.title,
        narrative=hypothesis.narrative,
        likelihood=hypothesis.likelihood,
        status=str(hypothesis.status.value),
        supporting_evidence=tuple(hypothesis.supporting_evidence),
        refuting_evidence=tuple(hypothesis.refuting_evidence),
        metric_key=hypothesis.metric_key,
        impact_amount=None if impact.impact is None else impact.impact.as_string(),
        impact_currency=None if impact.impact is None else impact.impact.currency,
        impact_basis=impact.basis,
        not_assessable_reason=impact.not_assessable_reason,
        impact_trace=_trace_document(impact.trace),
    )


def _code(value: Any) -> HypothesisCode:
    return value if isinstance(value, HypothesisCode) else HypothesisCode(value)


def requires_approval(option_type: Any) -> bool:
    """Whether a remedy needs a human before anything happens to money."""
    from app.domain.hypotheses import requires_approval as _requires

    return _requires(option_type)


def _dispute_text_of(case: DisputeCase) -> str:
    """The customer's own words, from the evidence snapshot.

    Read back out of the bundle rather than from a field on the case, because the
    snapshot is what the evidence fingerprint covers. If they could disagree, the
    investigation would be reasoning over a copy the fingerprint does not describe.
    """
    for item in case.evidence.items:
        if item.natural_key.startswith("dispute_text:"):
            return str(item.snapshot.get("text", ""))
    return case.description


def _maybe(money: Any) -> str | None:
    return None if money is None else money.as_string()




def _trace_document(trace: Any) -> dict[str, Any]:
    if trace is None:
        return {}
    document = getattr(trace, "as_document", None)
    if callable(document):
        return dict(document())
    if isinstance(trace, dict):
        return dict(trace)
    return {"repr": repr(trace)}


def _recalculation_document(recalculation: Any) -> dict[str, Any]:
    """The engine's per-line view, for the reviewer's trace panel.

    Serialised defensively rather than through a single ``as_document``: the fields
    that matter to a reviewer (per-line recorded vs calculated, and why a line is
    unresolved) are read individually, and anything the dataclass does not have is
    simply absent rather than raising.
    """
    document: dict[str, Any] = {
        "invoice_external_id": recalculation.invoice_external_id,
        "currency": recalculation.currency,
        "recorded_total": recalculation.recorded_total.as_string(),
        "calculated_total": recalculation.calculated_total.as_string(),
        "difference": recalculation.difference.as_string(),
        "is_complete": recalculation.is_complete,
        "recalculated_line_count": recalculation.recalculated_line_count,
        "unresolved_metrics": list(recalculation.unresolved_metrics),
        "lines": [],
    }
    for line in recalculation.lines:
        document["lines"].append(
            {
                "metric_key": line.metric_key,
                "line_type": line.line_type.value,
                "status": getattr(line.status, "value", str(line.status)),
                "recorded_amount": _maybe(line.recorded_amount),
                "calculated_amount": _maybe(line.calculated_amount),
                "difference": _maybe(line.difference),
                "rule_ref": line.rule_ref,
                "notes": line.notes,
                "unresolved_reason": getattr(line.unresolved_reason, "value", None),
            }
        )
    return document


def _money_document(balance: Any) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name in (
        "outstanding",
        "allocated_payments",
        "net_adjustments",
        "unapplied_payment_total",
        "currency",
        "is_provisional",
        "is_reconciled",
        "invoice_external_id",
        "unresolved_metrics",
    ):
        value = getattr(balance, name, None)
        if value is None:
            continue
        document[name] = value.as_string() if hasattr(value, "as_string") else value
    return document


def _usage_summary_document(summary: Any) -> dict[str, Any]:
    return {
        "metric_key": summary.metric_key,
        "period_start": str(summary.period.start),
        "period_end": str(summary.period.end),
        "total_quantity": str(summary.total_quantity),
        "contributing_event_count": len(summary.contributing_event_ids),
        "duplicate_dedupe_keys": [d.dedupe_key for d in summary.duplicates],
        "out_of_period_event_ids": [e.event_id for e in summary.out_of_period],
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime.

    A pending run's ``created_at`` is read back from the database, where MySQL's
    ``DATETIME`` has no zone and returns a naive value. The run is always created in
    UTC, so a naive value is that instant without its label; reattaching UTC keeps the
    terminal record (and the audit event built from it) timezone-aware.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
