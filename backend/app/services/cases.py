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

``reopen``
    The only way out of ``RESOLVED`` or ``REJECTED``, and it needs a reason.

Identity
--------
``actor_id`` is recorded, never checked. Phase 4 has no authentication and must not
pretend to: the development identity arrives in a request header and is stored as a
label on the annotation so that a reviewer can see who said what. Separation of
duties -- refusing to let the analyst who ran the investigation approve it -- is a
later phase, and faking it with a column that is never enforced would be worse than
not having it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.domain.billing import Adjustment, Payment, PriceTerm, RecordedInvoice, UsageEvent
from app.domain.cases import (
    CalculationRecord,
    CaseSeverity,
    CaseStatus,
    CaseTransitionError,
    DisputeCase,
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

    def __init__(self, repository: CaseRepository, provider: LlmProvider) -> None:
        self._repository = repository
        self._provider = provider

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

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def open_case(self, command: OpenCaseCommand) -> DisputeCase:
        """Open a case and store its evidence.

        The bundle is assembled first so the case is never persisted without the
        evidence it was opened from, and so the fingerprint written to the row is
        the fingerprint of real snapshots.

        A duplicate ``dispute_external_id`` raises rather than reopening the
        existing case: ingesting the same ticket twice is a bug in the caller, and
        silently merging two complaints into one case would destroy one of them.
        """
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
        )
        self._repository.save(case, expected_version=0)
        return case

    def investigate(
        self,
        dispute_id: UUID,
        *,
        prompt_version: str,
        validator: EvidenceCitationValidator | None = None,
    ) -> InvestigationRecord:
        """Run an investigation and store its calculation and findings separately.

        Phase 3 is called with the records rebuilt from the case's stored ingest
        payload, not with anything the caller supplies. That is deliberate: the
        evidence was hashed from those records, so re-supplying them would let a
        caller run an investigation over inputs that do not match the evidence the
        case is showing, and the fingerprint would then attest to nothing.
        """
        case = self._repository.get(dispute_id)
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

        records = from_source_document(dict(case.source_document))
        invoice = records["invoice"]
        terms: list[PriceTerm] = records["price_terms"]
        events: list[UsageEvent] = records["usage_events"]

        inputs = InvestigationInputs(
            dispute_external_id=case.external_id,
            invoice=invoice,
            price_terms=terms,
            usage_events=events,
            payments=records["payments"],
            adjustments=records["adjustments"],
            contract_external_id=case.contract_external_id or "",
            dispute_text=_dispute_text_of(case),
            prompt_version=prompt_version,
        )
        # ``investigate`` derives its own usage summaries from the events and the
        # invoice period. Passing them in would create two sources of truth for the
        # same figures, and a mismatch between them would change the recalculated
        # total without changing the evidence.
        result: InvestigationResult = investigate(
            inputs=inputs, provider=self._provider, validator=validator
        )

        if result.evidence.fingerprint != case.evidence_fingerprint:
            # Should be impossible: the summaries are derived from the same records
            # the stored evidence was built from. Refusing is better than storing a
            # run whose fingerprint does not match the case, because every later
            # staleness comparison would then be comparing the wrong things.
            raise CaseTransitionError(
                "re-collected evidence does not match the evidence stored on the case "
                f"({result.evidence.fingerprint} vs {case.evidence_fingerprint}); the ingest "
                "payload and the stored snapshots disagree"
            )

        record = self._to_record(case, result)
        updated = case.with_investigation(record)
        self._repository.save(updated, expected_version=case.version)
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
            return case
        self._repository.save(updated, expected_version=case.version)
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
    ) -> ReviewAction:
        """Record a reviewer's annotation on one finding, hypothesis or option."""
        case = self._repository.get(dispute_id)
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
        )
        updated = case.with_review(review)
        self._repository.save(updated, expected_version=case.version)
        return review

    def reopen(self, dispute_id: UUID, *, reason: str, actor_id: str) -> DisputeCase:
        """Reopen a closed case.

        A reopen does not itself start an investigation. It moves the case to
        ``REOPENED``, which is the state that makes a fresh run legal; the run is a
        separate, explicit action so that a reviewer reopening a case and a reviewer
        re-running it are two decisions that can be recorded separately.
        """
        case = self._repository.get(dispute_id)
        updated = case.reopened(reason=reason, at=_now())
        self._repository.save(updated, expected_version=case.version)
        return updated

    # ------------------------------------------------------------------
    # mapping Phase 3 output onto stored records
    # ------------------------------------------------------------------

    def _to_record(self, case: DisputeCase, result: InvestigationResult) -> InvestigationRecord:
        """Split one Phase 3 result into a calculation record and a run record.

        The split is the point. ``CalculationRecord`` comes entirely from the
        deterministic engine; ``InvestigationRecord`` carries what the model said
        and a pointer to the calculation via its own foreign key. A reviewer can
        therefore trust the arithmetic without trusting the analysis, which is the
        only way the interface can show both without implying the second vouches
        for the first.
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
            id=uuid4(),
            dispute_id=case.id,
            version=case.next_investigation_version,
            status=InvestigationRunStatus.from_investigation_status(result.status),
            evidence_fingerprint=result.evidence.fingerprint,
            created_at=_now(),
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
            stage_status={"overall": result.status.value},
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
