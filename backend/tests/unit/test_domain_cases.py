"""The dispute-case aggregate: its invariants and its transitions.

The aggregate is immutable, and that is what makes the history trustworthy. Every
``with_*`` method returns a new instance and leaves the old one readable, so a run
that concluded something keeps showing what it concluded after new evidence arrives.

These tests are about *rules*, not storage. Whether the rules survive a round trip
through PostgreSQL is ``tests/integration``'s job, and it skips without a server.

The three properties most of this file is about:

* **Staleness is derived, never stored.** ``is_stale`` compares the fingerprint a run
  read against the case's current fingerprint. There is no flag to forget to set, and
  no way for the two to disagree.
* **New evidence reopens rather than overwrites.** Adding evidence marks the runs
  that did not read it stale and reopens the case, but never deletes or edits them.
* **The aggregate cannot be built in an inconsistent state.** An ``AMEND`` with no
  text, an impact with an amount and no basis: each is refused at construction rather
  than discovered at read time.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

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
from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType

NOW = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)


def invoice_item() -> EvidenceItem:
    return EvidenceItem.create("invoice:INV-1", EvidenceType.INVOICE, {"total": "100.0000"})


def make_case(
    evidence: list[EvidenceItem] | None = None,
    *,
    status: CaseStatus = CaseStatus.OPEN,
) -> DisputeCase:
    """A case with a real evidence bundle, so the fingerprint is derived not faked."""
    items = evidence if evidence is not None else [invoice_item()]
    return DisputeCase(
        id=uuid4(),
        external_id="DSP-1",
        invoice_external_id="INV-1",
        contract_external_id="CTR-1",
        status=status,
        severity=CaseSeverity.MEDIUM,
        description="charged for usage we do not recognise",
        evidence=EvidenceBundle(items=tuple(items)),
        created_at=NOW,
        source_document={"schema_version": 1, "present": True},
    )


def finding(code: str = "USAGE_OVERSTATEMENT") -> StoredFinding:
    return StoredFinding(
        id=uuid4(),
        code=code,
        severity="HIGH",
        category="usage",
        narrative="charge exceeds the events",
        confidence=Decimal("0.9"),
        supporting_evidence=("invoice:INV-1",),
    )


def make_run(
    case: DisputeCase,
    *,
    version: int = 1,
    fingerprint: str | None = None,
    created_at: datetime | None = None,
    findings: tuple[StoredFinding, ...] = (),
) -> InvestigationRecord:
    return InvestigationRecord(
        id=uuid4(),
        dispute_id=case.id,
        version=version,
        status=InvestigationRunStatus.COMPLETE,
        evidence_fingerprint=fingerprint or case.evidence_fingerprint,
        created_at=created_at or NOW,
        summary="the invoice overstates usage",
        provider_name="mock",
        model="mock-deterministic-v1",
        prompt_version="prompt-v1",
        engine_version="engine-v1",
        evidence=case.evidence,
        findings=findings,
        stage_status={"overall": "COMPLETE"},
    )


def add_payment(case: DisputeCase, key: str = "payment:PAY-1") -> DisputeCase:
    return case.with_evidence([*case.evidence.items, EvidenceItem.create(key, EvidenceType.PAYMENT, {"amount": "50.0000"})])


# ---------------------------------------------------------------------------
# Fingerprint and staleness
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_derived_from_the_evidence() -> None:
    """Not a stored column that could disagree with the evidence.

    ``evidence_fingerprint`` is a property over the bundle. Making it a field would
    mean two things that must always match, and a mismatch would be invisible until
    a staleness comparison silently succeeded.
    """
    case = make_case()

    assert case.evidence_fingerprint == case.evidence.fingerprint
    assert case.evidence_fingerprint.startswith("sha256:")


def test_two_cases_with_identical_evidence_share_a_fingerprint() -> None:
    """Otherwise the fingerprint would not identify anything."""
    assert make_case().evidence_fingerprint == make_case().evidence_fingerprint


def test_different_evidence_produces_a_different_fingerprint() -> None:
    case = make_case()
    other = make_case([EvidenceItem.create("invoice:INV-2", EvidenceType.INVOICE, {"total": "9"})])

    assert case.evidence_fingerprint != other.evidence_fingerprint


def test_a_fresh_run_is_not_stale() -> None:
    case = make_case()

    assert case.with_investigation(make_run(case)).is_stale is False


def test_new_evidence_marks_a_run_stale_without_deleting_it() -> None:
    """The whole point of keeping history: version 1 stays readable.

    A reviewer asking "what did the system conclude before we sent the payment
    receipt?" can only answer that if the earlier run survives, with its own findings
    and the evidence it actually read.
    """
    original = finding()
    case = make_case()
    first = case.with_investigation(make_run(case, findings=(original,)))

    updated = add_payment(first)

    assert updated.is_stale is True
    assert len(updated.investigations) == 1
    assert updated.investigations[0].version == 1
    assert updated.investigations[0].is_stale is True
    assert updated.investigations[0].findings[0].code == "USAGE_OVERSTATEMENT"
    # The evidence it read is untouched: still exactly what it saw.
    assert updated.investigations[0].evidence.fingerprint == first.evidence_fingerprint


def test_a_run_over_other_evidence_is_refused_outright() -> None:
    """Stronger than staleness: the run is not accepted at all.

    A run that read evidence which is no longer attached would arrive stale, and there
    would be nothing to review. Refusing it keeps ``AWAITING_REVIEW`` meaningful --
    every run waiting on a reviewer describes the evidence that is currently on the
    case. Staleness is still a derived property of *attached* runs; this guard only
    stops a mismatched one from being attached.
    """
    case = make_case()
    divergent = make_run(case, fingerprint="sha256:" + "0" * 64)

    with pytest.raises(CaseTransitionError, match="no longer attached"):
        case.with_investigation(divergent)

    assert case.investigations == ()


def test_an_attach_of_identical_evidence_is_a_no_op_for_the_fingerprint() -> None:
    """Re-attaching what is already there must not stale the run that read it."""
    case = make_case()
    case = case.with_investigation(make_run(case))

    again = case.with_evidence(list(case.evidence.items))

    assert again.evidence_fingerprint == case.evidence_fingerprint
    assert again.is_stale is False


def test_attaching_identical_evidence_does_not_change_the_case() -> None:
    """Idempotent, and it must not even bump the version.

    A retried attach that bumped the version would invalidate every reviewer's
    expected version for no reason, and -- if the bundle were rebuilt in a different
    order -- could mark runs stale over a set of identical facts.
    """
    case = make_case()
    first = case.with_investigation(make_run(case))

    again = first.with_evidence(list(first.evidence.items))

    assert again.version == first.version
    assert again.evidence_fingerprint == first.evidence_fingerprint
    assert again.status == first.status


def test_attaching_new_evidence_reopens_an_awaiting_review_case() -> None:
    """``AWAITING_REVIEW`` means "a human owes this a decision".

    Evidence arriving while a decision is pending invalidates the premise of that
    decision, so the case goes back to ``REOPENED`` rather than waiting for a review
    of evidence that has since changed.
    """
    case = make_case()
    case = case.with_investigation(make_run(case))
    case = replace(case, status=CaseStatus.AWAITING_REVIEW)

    updated = add_payment(case)

    assert updated.status == CaseStatus.REOPENED


def test_evidence_arriving_on_a_closed_case_reopens_it() -> None:
    """A resolved dispute is not settled by the arrival of new evidence.

    ``RESOLVED`` records that somebody decided. New evidence does not silently undo
    that decision -- it moves the case to ``REOPENED``, where the decision stays in
    the history and a fresh run is legal again.
    """
    case = make_case()
    case = case.with_investigation(make_run(case))
    closed = replace(case, status=CaseStatus.RESOLVED)

    updated = add_payment(closed, key="payment:PAY-9")

    assert updated.status == CaseStatus.REOPENED
    assert updated.is_stale is True


def test_an_open_case_with_no_history_is_never_stale() -> None:
    """Staleness compares a run against the evidence; no run means nothing to compare.

    Returning ``True`` here would make every freshly opened case look like it needed
    re-investigation.
    """
    assert make_case().is_stale is False


# ---------------------------------------------------------------------------
# Investigation versions
# ---------------------------------------------------------------------------


def test_the_first_run_is_version_one() -> None:
    assert make_case().next_investigation_version == 1


def test_versions_are_dense_and_gapless() -> None:
    """No gaps, because "the previous run" must be unambiguous."""
    case = make_case()
    case = case.with_investigation(make_run(case, version=1))
    case = add_payment(case)
    case = case.with_investigation(make_run(case, version=2))

    assert [run.version for run in case.investigations] == [1, 2]
    assert case.next_investigation_version == 3


def test_runs_are_ordered_oldest_first() -> None:
    """Chronological, so the UI can render a history without re-sorting."""
    case = make_case()
    case = case.with_investigation(make_run(case, version=1, created_at=NOW))
    case = add_payment(case)
    case = case.with_investigation(make_run(case, version=2, created_at=NOW + timedelta(hours=1)))

    timestamps = [run.created_at for run in case.investigations]
    assert timestamps == sorted(timestamps)


def test_adding_a_run_moves_the_case_to_awaiting_review() -> None:
    case = make_case()

    assert case.with_investigation(make_run(case)).status is CaseStatus.AWAITING_REVIEW


def test_the_original_case_is_untouched_by_every_transition() -> None:
    """Immutability, which is what makes stored history trustworthy.

    A ``with_*`` method that mutated in place would leave the caller's reference
    showing a state already written to storage, and a version conflict would then be
    undetectable.
    """
    case = make_case()
    before_version = case.version
    before_fingerprint = case.evidence_fingerprint

    case.with_investigation(make_run(case))
    add_payment(case)

    assert case.version == before_version
    assert case.evidence_fingerprint == before_fingerprint
    assert case.investigations == ()
    assert case.status is CaseStatus.OPEN


def test_current_investigation_is_the_newest_run() -> None:
    case = make_case()
    case = case.with_investigation(make_run(case, version=1))
    case = add_payment(case)
    case = case.with_investigation(make_run(case, version=2))

    assert case.current_investigation is not None
    assert case.current_investigation.version == 2


# ---------------------------------------------------------------------------
# Source document
# ---------------------------------------------------------------------------


def test_replacing_the_document_with_identical_content_is_a_no_op() -> None:
    """Compared by canonical JSON, not by object identity.

    A document read out of the database is a fresh object every time; identity
    comparison would make every attach look like a change and reopen cases that had
    gained nothing.
    """
    case = make_case().with_source_document(
        {"schema_version": 1, "present": True, "payments": []}
    )
    before = case.version

    assert case.with_source_document({"schema_version": 1, "present": True, "payments": []}).version == before


def test_replacing_the_document_with_new_content_bumps_the_version() -> None:
    case = make_case()

    changed = case.with_source_document(
        {"schema_version": 1, "present": True, "payments": [{"external_id": "PAY-1"}]}
    )

    assert changed.version == case.version + 1


def test_key_order_in_the_document_does_not_count_as_a_change() -> None:
    case = make_case()
    before = {"schema_version": 1, "present": True, "a": 1, "b": 2}
    case = case.with_source_document(before)

    reordered = case.with_source_document({"b": 2, "a": 1, "present": True, "schema_version": 1})

    assert reordered.version == case.version


def test_the_stored_document_cannot_be_mutated_through_the_aggregate() -> None:
    """Deep-frozen on construction.

    ``source_document`` is the input a re-investigation is computed from. If a caller
    could mutate it in place, a stored run and a later re-run could be computed from
    different records while the fingerprint attested to the original.
    """
    case = make_case()

    with pytest.raises(TypeError):
        case.source_document["injected"] = True  # type: ignore[index]


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def review_for(
    case: DisputeCase,
    run: InvestigationRecord,
    target_id: object,
    *,
    action: ReviewActionKind = ReviewActionKind.ACCEPT,
    **overrides: object,
) -> ReviewAction:
    fields: dict[str, object] = {
        "id": uuid4(),
        "dispute_id": case.id,
        "investigation_id": run.id,
        "target_type": ReviewTargetType.FINDING,
        "target_id": target_id,
        "action": action,
        "actor_id": "rev-1",
        "actor_role": "reviewer",
        "evidence_fingerprint_seen": case.evidence_fingerprint,
        "created_at": NOW,
        "rationale": "checked against the dashboard",
    }
    fields.update(overrides)
    return ReviewAction(**fields)  # type: ignore[arg-type]


def test_a_review_is_refused_when_its_investigation_is_stale() -> None:
    """The annotation would record a judgement about superseded evidence.

    This is the check that makes ``evidence_fingerprint_seen`` on a review worth
    storing. The alternative -- accept the review and mark it stale afterwards --
    leaves a reviewer having relied on findings that have since changed, which is
    the outcome the whole staleness mechanism exists to prevent.
    """
    original = finding()
    case = make_case()
    case = case.with_investigation(make_run(case, findings=(original,)))
    run = case.current_investigation
    assert run is not None
    stale_case = add_payment(case)
    assert stale_case.is_stale is True

    with pytest.raises(CaseTransitionError, match="stale"):
        stale_case.with_review(review_for(stale_case, run, original.id))


def test_amend_requires_replacement_text_and_other_verbs_forbid_it() -> None:
    """A stored review must be able to say what the reviewer actually decided.

    ``AMEND`` with no text records nothing; ``ACCEPT`` carrying text records a
    decision that says something the action does not.
    """
    case = make_case()
    run = make_run(case, findings=(finding(),))
    case = case.with_investigation(run)
    target = run.findings[0].id

    with pytest.raises(ValueError):
        review_for(case, run, target, action=ReviewActionKind.AMEND, amended_narrative=None)
    with pytest.raises(ValueError):
        review_for(case, run, target, action=ReviewActionKind.ACCEPT, amended_narrative="but reworded")


def test_a_review_must_name_its_actor() -> None:
    """An annotation with no actor is not an annotation."""
    with pytest.raises(ValueError):
        ReviewAction(
            id=uuid4(),
            dispute_id=uuid4(),
            investigation_id=uuid4(),
            target_type=ReviewTargetType.FINDING,
            target_id=uuid4(),
            action=ReviewActionKind.ACCEPT,
            actor_id="   ",
            actor_role="reviewer",
            evidence_fingerprint_seen="sha256:" + "a" * 64,
            created_at=NOW,
            rationale="agreed",
        )


def test_an_approve_verb_does_not_exist() -> None:
    """Approval moves money and arrives with §9.3's guards, which do not exist yet.

    A verb in the enum exists in every layer above it, so adding it to a CHECK
    constraint or a response schema before the idempotency and separation-of-duties
    work is how a button gets shipped without its guard.
    """
    values = {action.value for action in ReviewActionKind}

    assert "APPROVE" not in values
    assert values == {"ACCEPT", "REJECT", "REQUEST_MORE_INFO", "AMEND"}


def test_reviews_are_appended_and_never_replace_one_another() -> None:
    case = make_case()
    original = finding()
    run = make_run(case, findings=(original,))
    case = case.with_investigation(run)

    first = case.with_review(
        review_for(case, run, original.id, action=ReviewActionKind.REQUEST_MORE_INFO)
    )
    second = first.with_review(
        review_for(
            first, run, original.id, action=ReviewActionKind.AMEND,
            actor_id="rev-2", amended_narrative="the charge exceeds the events",
        )
    )

    assert len(second.reviews) == 2
    assert [r.action for r in second.reviews] == [
        ReviewActionKind.REQUEST_MORE_INFO,
        ReviewActionKind.AMEND,
    ]
    # The finding itself is untouched, so a reader can still see what the model said.
    assert second.current_investigation.findings[0].narrative == original.narrative


def test_a_review_of_a_nonexistent_target_is_refused() -> None:
    """A well-formed request must not be able to annotate nothing."""
    case = make_case()
    run = make_run(case)
    case = case.with_investigation(run)

    with pytest.raises(CaseTransitionError, match="not found"):
        case.with_review(review_for(case, run, uuid4()))


def test_a_review_of_a_run_on_another_case_is_refused() -> None:
    case = make_case()
    other = make_case()
    foreign = make_run(other)

    with pytest.raises(CaseTransitionError, match="not part of this case"):
        case.with_review(review_for(case, foreign, uuid4()))


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


def test_reopen_requires_a_reason() -> None:
    """A reopen without a reason teaches a later reader nothing."""
    closed = replace(make_case(), status=CaseStatus.RESOLVED)

    with pytest.raises(ValueError, match="requires a reason"):
        closed.reopened(reason="   ", at=NOW)


def test_reopen_only_applies_to_a_closed_case() -> None:
    case = make_case()

    with pytest.raises(CaseTransitionError, match="only a closed case reopens"):
        case.reopened(reason="changed my mind", at=NOW)


def test_reopen_does_not_start_an_investigation() -> None:
    """Reopening and re-running are two separate decisions.

    Conflating them would hide which one a reviewer actually made, and would create a
    run whose provenance nobody chose.
    """
    closed = replace(make_case(), status=CaseStatus.REJECTED)

    reopened = closed.reopened(reason="customer produced a usage export", at=NOW)

    assert reopened.status is CaseStatus.REOPENED
    assert reopened.investigations == ()
    assert "usage export" in reopened.description


# ---------------------------------------------------------------------------
# can_investigate / is_open_for_investigation
# ---------------------------------------------------------------------------


def test_a_case_with_an_invoice_can_be_investigated() -> None:
    assert make_case().can_investigate is True


def test_a_case_marked_absent_cannot_be_investigated() -> None:
    """``{"present": false}`` rather than ``{}``, because the two mean different things.

    An empty object is ambiguous with a malformed document; an explicit marker says
    "this case has no invoice to recalculate", which is a fact about the case.
    """
    absent = make_case().with_source_document({"present": False})

    assert absent.can_investigate is False


@pytest.mark.parametrize("status", [CaseStatus.RESOLVED, CaseStatus.REJECTED])
def test_a_closed_case_cannot_be_investigated(status: CaseStatus) -> None:
    assert make_case(status=status).is_open_for_investigation is False


@pytest.mark.parametrize(
    "status",
    [CaseStatus.OPEN, CaseStatus.INVESTIGATING, CaseStatus.AWAITING_REVIEW, CaseStatus.REOPENED],
)
def test_an_unclosed_case_can_be_investigated(status: CaseStatus) -> None:
    """``AWAITING_REVIEW`` included: re-running a case awaiting review is legitimate.

    The evidence may be unchanged and the model may differ across a version bump, and
    a reviewer comparing two runs is exactly what this system is for.
    """
    assert make_case(status=status).is_open_for_investigation is True


# ---------------------------------------------------------------------------
# Stored records: the money boundary
# ---------------------------------------------------------------------------


def test_a_hypothesis_amount_comes_in_as_text_and_comes_back_as_decimal() -> None:
    """Strings on the way in, ``Decimal`` on the way out.

    The text is the stored form, because a float would lose the last digits before
    anyone noticed; the ``Decimal`` is what arithmetic should use.
    """
    hypothesis = StoredHypothesis(
        id=uuid4(),
        hypothesis_code="USAGE_OVERSTATEMENT",
        title="Usage overstated",
        narrative="the events do not support the charge",
        status="SUPPORTED",
        likelihood=Decimal("0.8"),
        metric_key="api_calls",
        impact_amount="480.0000",
        impact_currency="USD",
        impact_basis="line difference",
        impact_trace={"rule": "line_difference"},
    )

    assert hypothesis.impact_amount == "480.0000"
    assert hypothesis.impact_decimal == Decimal("480.0000")


def test_an_amount_without_a_basis_is_refused() -> None:
    """A figure with no explanation of how it was reached is not reviewable."""
    with pytest.raises(ValueError):
        StoredHypothesis(
            id=uuid4(),
            hypothesis_code="USAGE_OVERSTATEMENT",
            title="t",
            narrative="n",
            status="SUPPORTED",
            likelihood=None,
            metric_key="api_calls",
            impact_amount="480.0000",
            impact_currency="USD",
            impact_basis="   ",
            impact_trace={},
        )


def test_an_amount_and_a_not_assessable_reason_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        StoredHypothesis(
            id=uuid4(),
            hypothesis_code="USAGE_OVERSTATEMENT",
            title="t",
            narrative="n",
            status="SUPPORTED",
            likelihood=None,
            metric_key="api_calls",
            impact_amount="480.0000",
            impact_currency="USD",
            impact_basis="line difference",
            not_assessable_reason="cannot price it",
            impact_trace={},
        )


def test_a_hypothesis_with_no_amount_must_say_something_about_why() -> None:
    """``basis`` or ``reason``, not silence.

    A row with neither reads as "no impact", which is a different claim from "not
    assessable". Phase 3's ``ImpactAssessment`` always carries a ``basis`` and only
    sometimes a reason -- which is why the database constraint is an ``OR`` across
    both rather than an implication to the reason alone.
    """
    with pytest.raises(ValueError):
        StoredHypothesis(
            id=uuid4(),
            hypothesis_code="UNEXPLAINED",
            title="t",
            narrative="n",
            status="SUPPORTED",
            likelihood=None,
            impact_trace={},
        )

    directional = StoredHypothesis(
        id=uuid4(),
        hypothesis_code="UNEXPLAINED",
        title="t",
        narrative="n",
        status="SUPPORTED",
        likelihood=None,
        impact_basis="the charge is directional, not priced",
        impact_trace={},
    )
    assert directional.impact_decimal is None


def test_a_likelihood_outside_zero_to_one_is_refused() -> None:
    # Constructed without an impact, so only the likelihood can be at fault. The
    # aggregate validates a record as a whole, so match the field name rather than
    # the position of its message in the combined error.
    with pytest.raises(ValueError, match="likelihood"):
        StoredHypothesis(
            id=uuid4(),
            hypothesis_code="UNEXPLAINED",
            title="t",
            narrative="n",
            status="SUPPORTED",
            likelihood=Decimal("1.4"),
            impact_basis="directional only",
            impact_trace={},
        )


def test_a_remedy_is_marked_as_needing_human_approval() -> None:
    """Carried explicitly rather than implied by the absence of a button.

    A response that simply omitted the flag would let a UI present a credit note as
    something it can do.
    """
    option = StoredResolutionOption(
        id=uuid4(),
        option_type="ISSUE_CREDIT",
        title="Credit the difference",
        rationale="the recalculated total is lower",
        requires_human_approval=True,
        supporting_evidence=("invoice:INV-1",),
    )

    assert option.requires_human_approval is True


def test_a_calculation_must_carry_its_currency() -> None:
    """Amounts without a currency are not interpretable."""
    with pytest.raises(ValueError):
        CalculationRecord(
            currency="",
            engine_version="engine-v1",
            recalculated_total="100.0000",
            is_complete=True,
            is_provisional=False,
            trace={},
            invoice={},
            balance={},
        )


def test_a_calculation_marks_itself_provisional_when_metrics_are_unresolved() -> None:
    """A balance built on an incomplete recalculation is a lower bound.

    The flag is on the record rather than inferred by the UI, because a reviewer
    treating a provisional balance as final is the failure this prevents.
    """
    calculation = CalculationRecord(
        currency="USD",
        engine_version="engine-v1",
        recalculated_total="100.0000",
        is_complete=False,
        is_provisional=True,
        unresolved_metrics=("api_calls",),
        trace={},
        invoice={},
        balance={},
    )

    assert calculation.has_unresolved is True
    assert calculation.is_provisional is True


# ---------------------------------------------------------------------------
# Cross-case safety
# ---------------------------------------------------------------------------


def test_a_run_carrying_another_cases_evidence_is_refused() -> None:
    """Guards against a repository mixing up two cases' bundles.

    The failure this prevents is quiet: the run would store, its fingerprint would be
    compared against the wrong evidence, and the case would look current when it is
    not. Refused rather than flagged stale, because a run that read foreign evidence
    is not a judgement about this case at all.
    """
    case = make_case()
    other = make_case(
        [EvidenceItem.create("invoice:OTHER", EvidenceType.INVOICE, {"total": "9"})]
    )
    foreign = replace(
        make_run(other),
        dispute_id=case.id,
        evidence=other.evidence,
        evidence_fingerprint=other.evidence_fingerprint,
    )

    with pytest.raises(CaseTransitionError, match="no longer attached"):
        case.with_investigation(foreign)

    assert case.investigations == ()


def test_a_run_claimed_by_another_case_is_refused_even_with_matching_evidence() -> None:
    """Identity is checked independently of the fingerprint.

    Two cases can legitimately hold identical evidence and therefore share a
    fingerprint. The ``dispute_id`` check is what stops case B's run from being
    appended to case A's history, which a fingerprint comparison cannot detect.
    """
    case = make_case()
    impostor = replace(make_run(case), dispute_id=uuid4())

    with pytest.raises(CaseTransitionError, match="different dispute"):
        case.with_investigation(impostor)


def test_a_run_with_a_skipped_version_is_refused() -> None:
    """Versions must be dense, or "the previous run" stops being unambiguous."""
    case = make_case()

    with pytest.raises(CaseTransitionError, match="expected investigation version 1"):
        case.with_investigation(make_run(case, version=7))