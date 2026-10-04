"""SQLAlchemy models for dispute cases, evidence, calculations and investigations.

Schema
------
Nine tables, matching the investigation entity table in docs/SYSTEM_DESIGN.md §3.3
for everything Phase 4 needs and nothing it does not:

``disputes``
    The case a reviewer works on: identifier, linked invoice and contract, status,
    severity, and the ingest payload the evidence was built from.
``dispute_evidence_items``
    Immutable per-case snapshots of source records. Append-only, content-hashed,
    unique on ``(dispute_id, natural_key, content_hash)``.
``calculations``
    The deterministic engine's output, stored apart from the investigation.
``investigations``
    One analysis run: provenance, the evidence fingerprint it read, staleness.
``investigation_evidence``
    Which snapshot rows a given run actually read.
``investigation_findings`` / ``investigation_hypotheses`` / ``investigation_resolution_options``
    What the run produced, with the engine-computed impact on the hypotheses.
``finding_reviews``
    A reviewer's append-only annotation on one of those.

Deliberate omissions
--------------------
``adjustments``, ``review_decisions`` and ``audit_events`` are in §3.3 and are
**not** here. Approving and moving money is a later phase, and the tables would
carry the unique constraints and decision vocabulary that phase is supposed to
design. ``review_decisions`` in particular is not "review annotations with an
approve flag": approval needs the idempotency and separation-of-duties rules of
§9.3, and a half-built version invites someone to wire the button up.

Why evidence rows are not a foreign key on ``investigations``
------------------------------------------------------------
``investigation_evidence`` is a join table rather than a column on
``dispute_evidence_items`` because the relationship is not "this case has this
evidence" but "this run read this evidence". Both are needed: the case needs the
full current set, the run needs the set as it stood. Storing only the run's
fingerprint would not be enough, because after new evidence arrives the older
snapshots must remain citable -- a reviewer comparing version 1 with version 2
needs to read what version 1 saw, and that is a row reference, not a hash.

Rules applied
-------------
**Money is ``NUMERIC(19,4)``** as everywhere else (NEP-01). Amounts are nullable
in exactly the places where "does not exist" is a real answer: an unreconciled
invoice has no outstanding balance, and a hypothesis that could not be assessed
has no impact.

**Amounts and confidences arrive as ``Decimal``.** Never ``float``.
``tests/unit/test_persistence_models.py`` fails if a float type reaches this
module, and this module is the only place a stored amount becomes a
``Decimal``.

**Currency is stored next to every amount that may have one.** A hypothesis whose
impact could not be computed has ``impact_currency IS NULL`` together with
``not_assessable_reason NOT NULL``; a check constraint makes the two agree. An
amount with no currency would render in the UI as a real number in no currency,
which is the specific outcome STK-01 exists to prevent.

**Only ``disputes`` has ``updated_at``.** Cases genuinely change -- status moves,
evidence is attached, a reopen is recorded. Everything else here is append-only,
so a second timestamp would imply an editing workflow that does not exist, the
same reasoning as ``IngestedAtMixin``.

**Deletes are ``RESTRICT`` everywhere.** A dispute, and everything hanging off it,
is the record of a commercial disagreement. Cascading deletes would destroy the
history a later audit is built from. The one exception is
``disputes.current_investigation_id``, which is ``ON DELETE SET NULL``: a pointer
into a table whose rows are never deleted, so the clause is a formality rather
than a licence to lose history.

**The ``findings`` narrative is not editable.** ``finding_reviews`` holds a
reviewer's amendment, with the original left intact. A finding that has been
overwritten no longer shows what the model said, which is the one thing a reader
needs in order to judge it.

Assumptions requiring confirmation
----------------------------------
1. ``disputes.external_id`` is unique and globally assigned. Not stated in any
   document, and it is the only thing standing between two ingests of the same
   ticket creating two cases.
2. ``severity`` and ``status`` are checked against string literals rather than
   native enums. A native enum would make adding a status a schema migration; a
   check constraint makes it a code change plus an ``ALTER``. The domain enums
   remain the source of truth and the literals here are asserted against them by
   ``tests/unit/test_case_models_match_domain.py``.
3. ``investigations.version`` is dense per dispute starting at 1, enforced by the
   application rather than by a trigger. ``DisputeCase.next_investigation_version``
   derives it from the aggregate and :meth:`SqlAlchemyCaseRepository.save` checks
   it, which is the same deliberate choice as the currency rule above: no billing
   or lifecycle logic inside the database.
4. ``finding_reviews.target_id`` has no foreign key. It is polymorphic across three
   tables with no shared id space. Integrity comes from ``ON DELETE RESTRICT`` on
   ``investigation_id``: a target cannot be deleted while an annotation cites it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base, UuidPrimaryKeyMixin

__all__ = [
    "NUMERIC_MONEY",
    "NUMERIC_PROBABILITY",
    "CalculationRow",
    "DisputeEvidenceRow",
    "DisputeRow",
    "FindingReviewRow",
    "InvestigationEvidenceRow",
    "InvestigationFindingRow",
    "InvestigationHypothesisRow",
    "InvestigationResolutionOptionRow",
    "InvestigationRow",
]

#: Money precision and scale, identical to :mod:`app.adapters.persistence.models`
#: so a case amount and a contract amount are stored the same way.
NUMERIC_MONEY: Final[tuple[int, int]] = (19, 4)

#: Confidence and likelihood are probabilities in [0, 1]. Five decimal places is
#: more resolution than a model can honestly claim; a wider column would only make
#: false precision easier to store.
NUMERIC_PROBABILITY: Final[tuple[int, int]] = (5, 4)

_CASE_STATUSES: Final[str] = "'OPEN','INVESTIGATING','AWAITING_REVIEW','RESOLVED','REJECTED','REOPENED'"
_CASE_SEVERITIES: Final[str] = "'LOW','MEDIUM','HIGH','CRITICAL'"
_RUN_STATUSES: Final[str] = "'PENDING','COMPLETE','DEGRADED','PARTIAL_FAILED'"
_REVIEW_ACTIONS: Final[str] = "'ACCEPT','REJECT','REQUEST_MORE_INFO','AMEND'"
_REVIEW_TARGETS: Final[str] = "'FINDING','HYPOTHESIS','RESOLUTION_OPTION'"

#: Stored snapshots are JSONB, not JSON. Snapshot contents are queried --
#: "every case citing this contract term" is a real review question -- and JSONB
#: is indexable and comparable while ``json`` is opaque text. It also round-trips
#: numbers without the trailing-zero loss that makes a content hash computed on
#: read differ from the one computed on write.
JSONB: Final[Any] = postgresql.JSONB(astext_type=Text())


class DisputeRow(UuidPrimaryKeyMixin, Base):
    """A dispute case.

    ``source_document`` is the validated ingest payload the evidence was built
    from: the invoice, contract terms, usage, payments and adjustments as the
    domain types serialise. It is stored as one JSONB document rather than as a
    set of invoice/usage/payment tables because its role is to be *read back* and
    revalidated to drive a later investigation, not to be queried per field --
    the queryable projection is the evidence snapshot table, which is indexed and
    hashed. Storing both is deliberate: the source document is the input of record,
    the snapshots are the citable view of it, and deriving one from the other
    means one of them can be wrong.

    Keeping the input is also what makes a *second* investigation possible. Phase 3
    took typed ``RecordedInvoice``/``PriceTerm``/``Payment`` objects that existed
    only in memory; without them a reopened case could not be re-run at all.

    ``evidence_fingerprint`` duplicates
    :attr:`DisputeCase.evidence_fingerprint`, which is derived. It is stored
    anyway because the repository needs it in a ``WHERE`` clause to answer "which
    cases have new evidence" without loading every evidence row, and because a
    generated column would not be available on the older PostgreSQL this project
    supports. The save path rewrites it from the aggregate, so it cannot drift.
    """

    __tablename__ = "disputes"

    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_external_id: Mapped[str | None] = mapped_column(String(128))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contracts.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'OPEN'")
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    #: Added by migration 0002 after ``investigations`` exists; declared here so the
    #: ORM knows about the dependency. Circular by nature: a case points at its
    #: newest run and a run points back at its case.
    current_investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("external_id", name="uq_disputes_external_id"),
        CheckConstraint("btrim(external_id) <> ''", name="external_id_not_blank"),
        CheckConstraint("btrim(invoice_external_id) <> ''", name="invoice_external_id_not_blank"),
        CheckConstraint(f"status IN ({_CASE_STATUSES})", name="status_is_known"),
        CheckConstraint(f"severity IN ({_CASE_SEVERITIES})", name="severity_is_known"),
        CheckConstraint("version >= 1", name="version_is_positive"),
        CheckConstraint(
            "evidence_fingerprint ~ '^sha256:[0-9a-f]{64}$'", name="fingerprint_is_sha256"
        ),
        Index("ix_disputes_status_created_at", "status", "created_at"),
        Index("ix_disputes_invoice_external_id", "invoice_external_id"),
    )


class DisputeEvidenceRow(UuidPrimaryKeyMixin, Base):
    """One immutable evidence snapshot belonging to a case.

    No ``updated_at``: a snapshot that changed would invalidate every
    ``content_hash`` and ``evidence_fingerprint`` that referenced it. Correction
    means attaching a new snapshot under the same ``natural_key`` with a
    different ``content_hash`` -- and ``EvidenceBundle`` rejects that combination
    within one bundle, so a corrected fact genuinely produces a new case
    fingerprint and visibly reopens the dispute rather than silently editing it.

    The unique constraint is what makes the "attach evidence" endpoint idempotent
    (docs/SYSTEM_DESIGN.md §11). Two concurrent requests carrying the same snapshot
    race at this index; one wins and the other gets a constraint violation the
    repository treats as "already stored".
    """

    __tablename__ = "dispute_evidence_items"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    natural_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "dispute_id",
            "natural_key",
            "content_hash",
            name="uq_dispute_evidence_items_dispute_id_natural_key_content_hash",
        ),
        CheckConstraint("btrim(natural_key) <> ''", name="natural_key_not_blank"),
        CheckConstraint(
            "content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash_is_sha256"
        ),
        Index("ix_dispute_evidence_items_dispute_id", "dispute_id"),
        Index("ix_dispute_evidence_items_natural_key", "natural_key"),
    )


class CalculationRow(UuidPrimaryKeyMixin, Base):
    """The deterministic engine's output for one investigation.

    A separate table from ``investigations`` on purpose. These numbers are produced
    by :mod:`app.pricing` with no model involved, so they have a different
    authority (the engine, not the analyst), a different lifetime (they do not
    change when the prompt changes) and a different failure mode (a bug here is a
    billing bug). Folding them into the investigation row would make "what did we
    bill" depend on "what did we ask the model", and would make an engine bug
    look like a model hallucination.

    ``investigation_id`` is unique: exactly one calculation per run. A second run
    over the same evidence is a different row, so both remain readable.

    ``unresolved_metrics`` records which metrics could not be priced even though
    the invoice had lines for them. That is the honest counterpart to
    ``is_provisional``: the total is correct for what it covers, and the UI must
    say what it does not cover rather than presenting it as the whole answer.
    """

    __tablename__ = "calculations"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    recalculated_total: Mapped[Decimal] = mapped_column(
        Numeric(*NUMERIC_MONEY), nullable=False
    )
    recorded_total: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    outstanding: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    allocated_payments: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    net_adjustments: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unresolved_metrics: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    trace: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    invoice: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    balance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage_summaries: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("investigation_id", name="uq_calculations_investigation_id"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_is_iso4217"),
        CheckConstraint(
            "NOT is_provisional OR is_complete = false",
            name="provisional_totals_are_incomplete",
        ),
        Index("ix_calculations_dispute_id", "dispute_id"),
    )


class InvestigationRow(UuidPrimaryKeyMixin, Base):
    """One analysis run.

    ``evidence_fingerprint`` is the mechanism for FR-009: comparing it with
    ``disputes.evidence_fingerprint`` says whether this run still describes the
    case. ``stale_at`` is when it stopped, recorded so that staleness has a date a
    reviewer can be shown and so that a case's staleness is queryable without
    loading and re-hashing evidence.

    Both are kept even though ``stale_at`` could be derived from the fingerprint
    comparison. A fingerprint comparison answers "is it stale"; it cannot answer
    "since when", and "the reviewer approved this on the 4th and evidence arrived
    on the 6th" is exactly the question an audit asks.
    """

    __tablename__ = "investigations"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    evidence_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    stage_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    degradations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("dispute_id", "version", name="uq_investigations_dispute_id_version"),
        CheckConstraint("version >= 1", name="version_is_positive"),
        CheckConstraint(f"status IN ({_RUN_STATUSES})", name="status_is_known"),
        CheckConstraint(
            "evidence_fingerprint ~ '^sha256:[0-9a-f]{64}$'", name="fingerprint_is_sha256"
        ),
        Index("ix_investigations_dispute_id", "dispute_id"),
        Index("ix_investigations_stale_at", "stale_at"),
    )


class InvestigationEvidenceRow(Base):
    """Which snapshot rows a given run read.

    Composite primary key, no surrogate: the pair *is* the identity, and a
    surrogate would permit the same snapshot to be linked twice.
    """

    __tablename__ = "investigation_evidence"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), primary_key=True
    )
    evidence_item_id: Mapped[uuid.UUID] = mapped_column(
        # Named explicitly: the convention's expansion would be 65 characters and
        # PostgreSQL would silently truncate it to ``...dispute_evid_087e`` with a
        # hash suffix, leaving a name nobody can grep for. A short stable name is
        # worth more here than the uniformity the convention buys, and the
        # convention's own docstring says so.
        ForeignKey(
            "dispute_evidence_items.id",
            ondelete="RESTRICT",
            name="fk_investigation_evidence_evidence_item_id_evidence",
        ),
        primary_key=True,
    )

    __table_args__ = (Index("ix_investigation_evidence_evidence_item_id", "evidence_item_id"),)


class InvestigationFindingRow(UuidPrimaryKeyMixin, Base):
    """One model observation, exactly as produced. Never updated."""

    __tablename__ = "investigation_findings"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(*NUMERIC_PROBABILITY), nullable=False)
    supporting_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("severity IN ('INFO','WARN','CRITICAL')", name="severity_is_known"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_unit_range"),
        CheckConstraint("btrim(narrative) <> ''", name="narrative_not_blank"),
        Index("ix_investigation_findings_investigation_id", "investigation_id"),
    )


class InvestigationHypothesisRow(UuidPrimaryKeyMixin, Base):
    """A candidate cause with the engine-computed impact attached.

    The impact columns are the deterministic engine's, not the model's. That is
    the whole safety story of Phase 3 in three columns: ``impact_amount`` was
    computed by :mod:`app.pricing.impact` from the trace beside it, and the model
    had no way to write them. A hypothesis the engine could not assess carries
    ``not_assessable_reason`` and no amount, which is a different answer from
    "impact zero" and must not collapse into it.
    """

    __tablename__ = "investigation_hypotheses"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=False
    )
    hypothesis_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    likelihood: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_PROBABILITY))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    supporting_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    refuting_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    metric_key: Mapped[str | None] = mapped_column(String(128))
    impact_amount: Mapped[Decimal | None] = mapped_column(Numeric(*NUMERIC_MONEY))
    impact_currency: Mapped[str | None] = mapped_column(String(3))
    impact_basis: Mapped[str | None] = mapped_column(Text)
    not_assessable_reason: Mapped[str | None] = mapped_column(Text)
    impact_trace: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # These four constraints mirror ``StoredHypothesis.__post_init__`` exactly.
        # The database is the last place a bad row can be stopped, and a check that
        # disagrees with the domain is worse than no check: it rejects a legitimate
        # state, or accepts an illegal one that the domain would have refused.
        #
        # The first is deliberately "basis *or* reason", not "reason". Phase 3's
        # ``ImpactAssessment`` always carries a ``basis`` and only sometimes a
        # ``not_assessable_reason``, so a directional finding with no computable
        # amount ("usage is overstated, but the rule cannot price it") is a real state
        # and must be storable. Requiring a reason there would have made the
        # repository reject rows the domain considers valid.
        CheckConstraint(
            "impact_amount IS NOT NULL "
            "OR COALESCE(BTRIM(impact_basis), '') <> '' "
            "OR COALESCE(BTRIM(not_assessable_reason), '') <> ''",
            name="missing_impact_is_explained",
        ),
        CheckConstraint(
            "impact_amount IS NULL OR COALESCE(BTRIM(impact_basis), '') <> ''",
            name="amount_implies_basis",
        ),
        CheckConstraint(
            "impact_amount IS NULL OR not_assessable_reason IS NULL",
            name="amount_excludes_unassessable",
        ),
        CheckConstraint(
            "impact_amount IS NULL OR impact_currency IS NOT NULL",
            name="amount_implies_currency",
        ),
        CheckConstraint(
            "impact_currency IS NULL OR impact_currency ~ '^[A-Z]{3}$'",
            name="currency_is_iso4217",
        ),
        CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="likelihood_in_unit_range",
        ),
        Index("ix_investigation_hypotheses_investigation_id", "investigation_id"),
    )


class InvestigationResolutionOptionRow(UuidPrimaryKeyMixin, Base):
    """A candidate remedy. Not an instruction, and never executed here.

    ``requires_human_approval`` is stored rather than derived because it is a
    fact about the remedy, not about the phase of the product. Deriving it from
    ``option_type`` would mean a new option type silently appears approvable
    without anyone deciding that.
    """

    __tablename__ = "investigation_resolution_options"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        # Named explicitly: the convention's expansion is 67 characters, over
        # PostgreSQL's 63 limit, which would truncate to an unnameable
        # ``...investigatio_9f3c``. This table needs short explicit foreign key names.
        ForeignKey(
            "investigations.id",
            ondelete="RESTRICT",
            name="fk_investigation_resolution_options_investigation_id",
        ),
        nullable=False,
    )
    option_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    hypothesis_code: Mapped[str | None] = mapped_column(String(64))
    supporting_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "btrim(rationale) <> ''", name="rationale_not_blank"
        ),
        Index("ix_investigation_resolution_options_investigation_id", "investigation_id"),
    )


class FindingReviewRow(UuidPrimaryKeyMixin, Base):
    """A reviewer's append-only annotation on one finding, hypothesis or option.

    ``AMEND`` stores the reviewer's replacement text in ``amended_narrative`` and
    leaves the target untouched. Overwriting the target would be the obvious
    implementation and the wrong one: it would leave no record of what the model
    said, which is the only way a reader can tell a model error from a reviewer
    preference.

    ``evidence_fingerprint_seen`` is the same device as ``review_decisions`` in
    §9.2, applied one phase earlier to a non-approval action. It records that this
    annotation refers to a particular view of the facts; if the evidence has moved
    on, :meth:`DisputeCase.with_review` refuses the annotation rather than
    recording a judgement against superseded evidence.

    ``actor_role`` is stored and never trusted for authorisation. Phase 4 has no
    authentication at all (development identity only), and a column that looks like
    an authorisation input is an invitation to build one on top of it before the
    separation-of-duties rules exist.
    """

    __tablename__ = "finding_reviews"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_fingerprint_seen: Mapped[str] = mapped_column(String(71), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    amended_narrative: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"action IN ({_REVIEW_ACTIONS})", name="action_is_known"),
        CheckConstraint(f"target_type IN ({_REVIEW_TARGETS})", name="target_type_is_known"),
        CheckConstraint("btrim(actor_id) <> ''", name="actor_id_not_blank"),
        CheckConstraint(
            "(action = 'AMEND') = (amended_narrative IS NOT NULL)",
            name="amendment_matches_action",
        ),
        CheckConstraint(
            "amended_narrative IS NULL OR btrim(amended_narrative) <> ''",
            name="amendment_not_blank",
        ),
        CheckConstraint(
            "evidence_fingerprint_seen ~ '^sha256:[0-9a-f]{64}$'",
            name="fingerprint_is_sha256",
        ),
        Index("ix_finding_reviews_dispute_id_created_at", "dispute_id", "created_at"),
        Index("ix_finding_reviews_target", "target_type", "target_id"),
    )
