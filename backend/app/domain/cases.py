"""Dispute cases: the thing a reviewer actually works on.

Phase 3 produced an :class:`~app.domain.evidence.EvidenceBundle` and an
``InvestigationResult`` that were thrown away when the process exited. Phase 4
makes both durable. The domain types here are the durable shapes, and they are
plain standard-library dataclasses so that ``app.domain`` keeps its rule of
importing nothing but the standard library (enforced by
``tests/unit/test_layer_boundaries.py``).

Three decisions worth stating, because each one is a place where the obvious
implementation is the wrong one.

**Money crosses this boundary as a decimal string, not as ``Decimal`` and never
as ``float``.** The persistence layer has to hand the value to a driver, a JSON
document and a ``NUMERIC`` column; a ``Decimal`` survives all three but invites
``float`` coercion at each hop. A string is exact everywhere and is what
``NUMERIC(19, 4)`` stores anyway. :func:`money_from_text` is the only reader.

**Findings are stored as the model produced them and are never edited in place.**
A reviewer who disagrees amends their *own* record
(:class:`ReviewAction`) and the original survives. Overwriting a finding would
destroy the only evidence of what the model actually claimed, which is the thing
FR-014 is about.

**An investigation is identified by the evidence it read.** Every persisted
investigation carries the ``evidence_fingerprint`` of the bundle it consumed, and
the case carries the fingerprint of everything currently attached. Comparing the
two is the entire staleness mechanism (FR-009): no timestamps, no diffing, no
scheduled job. That is why evidence snapshots are content-hashed in Phase 3 --
the hash is the precondition for this file being possible at all.

The case status enum follows docs/SYSTEM_DESIGN.md §3.4, including the guards.
The guards live here rather than in the service so that a transition attempted
from any entry point raises, not just the one the API happens to use.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final
from uuid import UUID

from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType, canonical_json
from app.domain.investigation import InvestigationStage, InvestigationStatus
from app.domain.json_frozen import freeze_json, thaw_json

__all__ = [
    "AMOUNT_PATTERN",
    "CalculationRecord",
    "CaseSeverity",
    "CaseStatus",
    "CaseTransitionError",
    "DecisionKind",
    "DisputeCase",
    "DisputeDecision",
    "InvestigationRecord",
    "InvestigationRunStatus",
    "ReviewAction",
    "ReviewActionKind",
    "ReviewTargetType",
    "StoredFinding",
    "StoredHypothesis",
    "StoredResolutionOption",
    "money_from_text",
]

#: Money is stored as ``"1234.5600"``: a signed decimal, no exponent, no ``NaN``
#: or ``Infinity``. Enforced on the way in *and* on the way out, because
#: ``float('inf')`` round-trips through JSON as a bare ``Infinity`` token that
#: PostgreSQL's ``NUMERIC`` rejects with a driver error rather than a useful one.
AMOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^-?\d+(\.\d+)?$")

#: Severity bands for a case. Deliberately coarser than
#: ``FindingSeverity``: a case is about money owed, a finding is about a
#: discrepancy, and conflating them would let three INFO findings imply a
#: CRITICAL case.
_CASE_SEVERITIES: Final[frozenset[str]] = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})

_EXTERNAL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$")

#: Longest idempotency key the schema stores (``Idempotency-Key`` header). A key is
#: an opaque client token, so it is kept as-is apart from trimming; length is bounded
#: only because the column is ``VARCHAR(255)``.
IDEMPOTENCY_KEY_MAX_LENGTH: Final[int] = 255

#: The workflow stages in the order they must complete, used to answer "where does a
#: recovery resume from". Derived from :class:`~app.domain.investigation.InvestigationStage`
#: so the two vocabularies cannot drift.
_STAGE_ORDER: Final[tuple[str, ...]] = (
    InvestigationStage.COLLECTING_EVIDENCE.value,
    InvestigationStage.INTERPRETING.value,
    InvestigationStage.COMPUTING_IMPACT.value,
    InvestigationStage.PROPOSING_RESOLUTIONS.value,
)

#: The marker a stage carries once it has finished. Anything else means "not done".
_STAGE_COMPLETE: Final[str] = "COMPLETE"


def _normalise_idempotency_key(value: str | None) -> str | None:
    """Trim an idempotency key, treating blank as absent.

    ``None`` and an empty string both mean "the caller did not supply a key", so
    they are collapsed to ``None``: otherwise ``""`` and ``"  "`` would each be
    stored and each be a distinct key that no retry could ever match. A key that
    is too long is rejected here rather than truncated, because truncation would
    silently make two different keys collide.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"idempotency_key must be a string or None, got {type(value).__name__}")
    key = value.strip()
    if not key:
        return None
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValueError(
            f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters, "
            f"got {len(key)}"
        )
    return key


class CaseTransitionError(Exception):
    """A state-machine guard refused a transition.

    Named distinctly from ``ValueError`` so the API layer can map it to 409
    without catching unrelated bugs as if they were business-rule refusals.
    """


def money_from_text(text: str | None) -> Decimal | None:
    """Parse a stored amount back into a ``Decimal``.

    ``None`` means "this amount does not exist", which is different from zero and
    is common: an unreconciled invoice has no outstanding balance, and a
    hypothesis that was not assessable has no impact. Collapsing the two would
    let an unassessable hypothesis read as "impact 0.00, therefore safe".

    Raises ``ValueError`` on non-finite or exponential text. Callers are
    repositories reading their own writes, so a failure here is a bug rather
    than user input -- but it is raised, not swallowed, because silently
    returning ``None`` for a malformed amount would look identical to the
    legitimate absent case above.
    """
    if text is None:
        return None
    candidate = text.strip()
    if not AMOUNT_PATTERN.match(candidate):
        raise ValueError(f"not a storable amount: {text!r}")
    try:
        return Decimal(candidate)
    except InvalidOperation as exc:  # pragma: no cover - unreachable via the regex
        raise ValueError(f"not a decimal: {text!r}") from exc


class CaseStatus(str, Enum):
    """Case lifecycle, docs/SYSTEM_DESIGN.md §3.4."""

    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    REOPENED = "REOPENED"


class CaseSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class InvestigationRunStatus(str, Enum):
    """Lifecycle of a *persisted* investigation row.

    ``PENDING`` is added here because a row is written when an investigation
    starts, not when it finishes; Phase 3's ``InvestigationStatus`` only ever
    described finished runs. The three terminal members that mirror
    ``InvestigationStatus`` (``COMPLETE``/``DEGRADED``/``PARTIAL_FAILED``) are
    reached by the workflow; ``FAILED`` is the fourth terminal member and is
    reached only by recovery.

    ``FAILED`` is deliberately *not* an ``InvestigationStatus``: the workflow
    never produces it. It exists so a PENDING row abandoned by a crashed
    process, an exhausted retry budget or an expired TTL can be brought to a
    terminal state without inventing an interpretation that was never
    produced. A run that ends here has no findings, no calculation and no
    currency; it is a tombstone, not an answer.
    """

    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self is not InvestigationRunStatus.PENDING

    @classmethod
    def from_investigation_status(cls, status: InvestigationStatus) -> InvestigationRunStatus:
        return cls(status.value)


class ReviewTargetType(str, Enum):
    """What a reviewer's action was aimed at.

    A single table with a ``(target_type, target_id)`` pair rather than three
    tables, because the three targets share every property that matters here:
    they are append-only, they are addressed by id, and no two reviewer actions
    ever need to be joined together. The cost is no foreign key on ``target_id``,
    which is stated plainly in the migration rather than glossed over -- the
    referential integrity is provided by ``ON DELETE RESTRICT`` on the parent
    investigation, so a target cannot outlive the investigation that produced it.
    """

    FINDING = "FINDING"
    HYPOTHESIS = "HYPOTHESIS"
    RESOLUTION_OPTION = "RESOLUTION_OPTION"


class ReviewActionKind(str, Enum):
    """Reviewer verbs for Phase 4.

    ``APPROVE`` is deliberately absent *here*. Approving and moving money is the
    idempotency phase (Part 4 of the completion pass); what this vocabulary records
    is a reviewer's annotation, not a verdict. The verdict that closes a case is a
    separate kind (:class:`DecisionKind`), which sets ``RESOLVED``/``REJECTED`` and
    moves no money either.
    """

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REQUEST_MORE_INFO = "REQUEST_MORE_INFO"
    AMEND = "AMEND"


class DecisionKind(str, Enum):
    """The two verdicts that close a case's lifecycle.

    Values deliberately shadow the resulting :class:`CaseStatus` members, so
    ``CaseStatus(decision.value)`` is the status a decided case moves to and the
    mapping cannot drift. A decision is not an instruction to move money: it is
    the recorded judgement that the reviewed run settles the dispute (``RESOLVED``)
    or that it does not (``REJECTED``). Applying an amount or executing an
    adjustment belongs to the idempotent resolution phase.
    """

    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class StoredFinding:
    """One model observation, exactly as produced.

    ``confidence`` is a ``Decimal`` because it is compared and ranked, not
    displayed. ``supporting_evidence`` holds evidence natural keys, which is what
    makes a finding traceable back to the bundle without duplicating the
    snapshot.
    """

    id: UUID
    code: str
    severity: str
    category: str
    narrative: str
    confidence: Decimal
    supporting_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("finding code must not be blank")
        if not self.narrative.strip():
            raise ValueError("finding narrative must not be blank")
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError(f"confidence out of range: {self.confidence}")
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))


@dataclass(frozen=True, slots=True)
class StoredHypothesis:
    """A candidate cause plus the deterministic impact computed for it.

    Impact is stored as decimal text with its currency so the pair can never be
    separated. ``not_assessable_reason`` is populated *instead of* an amount when
    the metric was missing -- the third state that "amount is NULL" would
    otherwise have to mean three different things.
    """

    id: UUID
    hypothesis_code: str
    title: str
    narrative: str
    likelihood: Decimal | None
    status: str
    supporting_evidence: tuple[str, ...] = ()
    refuting_evidence: tuple[str, ...] = ()
    metric_key: str | None = None
    impact_amount: str | None = None
    impact_currency: str | None = None
    impact_basis: str | None = None
    not_assessable_reason: str | None = None
    impact_trace: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "refuting_evidence", tuple(self.refuting_evidence))
        object.__setattr__(self, "impact_trace", freeze_json(self.impact_trace))
        # Three states, not two, and they are kept apart because "impact 0.00" and
        # "impact unknown" are different answers to a reviewer's question:
        #
        #   quantified   amount + currency + basis (basis says how it was computed)
        #   directional  basis only, e.g. "overage likely above contracted tier"
        #   unassessable not_assessable_reason, with no amount at all
        #
        # The rule that matters: an amount and a "not assessable" reason are mutually
        # exclusive, and an amount always carries its currency. A row with an amount
        # but no currency is the failure this prevents -- it renders as a real number
        # in a currency nobody can name.
        quantified = self.impact_amount is not None
        if quantified and self.not_assessable_reason is not None:
            raise ValueError("impact cannot be both quantified and not assessable")
        if quantified:
            if self.impact_currency is None:
                raise ValueError("impact_amount requires impact_currency")
            if len(self.impact_currency) != 3 or not self.impact_currency.isupper():
                raise ValueError(f"impact_currency must be ISO 4217: {self.impact_currency!r}")
            if not (self.impact_basis or "").strip():
                raise ValueError("a quantified impact requires impact_basis")
        elif not ((self.impact_basis or "").strip() or (self.not_assessable_reason or "").strip()):
            raise ValueError(
                "hypothesis impact must be quantified, directional, or explicitly not assessable"
            )
        if self.likelihood is not None and not Decimal(0) <= self.likelihood <= Decimal(1):
            raise ValueError(f"likelihood out of range: {self.likelihood}")

    @property
    def impact_decimal(self) -> Decimal | None:
        return money_from_text(self.impact_amount)


@dataclass(frozen=True, slots=True)
class StoredResolutionOption:
    """A candidate remedy. Never an instruction to move money."""

    id: UUID
    option_type: str
    title: str
    rationale: str
    requires_human_approval: bool
    hypothesis_code: str | None = None
    supporting_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))


@dataclass(frozen=True, slots=True)
class CalculationRecord:
    """The deterministic recalculation, persisted as its own record.

    Separate from the investigation on purpose. The numbers are produced by
    ``app.pricing`` with no model involved, so they have a different lifetime
    (they do not change when the prompt changes), a different authority (the
    engine, not the analyst) and a different audit story. Folding them into the
    investigation row would make "what did we bill" depend on "what did we ask".

    Amounts are decimal text in ``NUMERIC(19, 4)`` (STK-01). ``trace`` and
    ``invoice``/``balance``/``usage_summaries`` are the JSONB projections that let
    a reviewer see the per-line arithmetic instead of a single total.
    """

    currency: str
    engine_version: str
    recalculated_total: str
    is_complete: bool
    is_provisional: bool
    recorded_total: str | None = None
    difference: str | None = None
    outstanding: str | None = None
    allocated_payments: str | None = None
    net_adjustments: str | None = None
    unresolved_metrics: tuple[str, ...] = ()
    trace: Mapping[str, Any] = field(default_factory=dict)
    invoice: Mapping[str, Any] = field(default_factory=dict)
    balance: Mapping[str, Any] = field(default_factory=dict)
    usage_summaries: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"currency must be three uppercase letters: {self.currency!r}")
        for name in (
            "recalculated_total",
            "recorded_total",
            "difference",
            "outstanding",
            "allocated_payments",
            "net_adjustments",
        ):
            value: str | None = getattr(self, name)
            money_from_text(value)
        object.__setattr__(self, "unresolved_metrics", tuple(self.unresolved_metrics))
        object.__setattr__(self, "trace", freeze_json(self.trace))
        object.__setattr__(self, "invoice", freeze_json(self.invoice))
        object.__setattr__(self, "balance", freeze_json(self.balance))
        object.__setattr__(
            self, "usage_summaries", tuple(freeze_json(u) for u in self.usage_summaries)
        )

    @property
    def difference_decimal(self) -> Decimal | None:
        return money_from_text(self.difference)

    @property
    def has_unresolved(self) -> bool:
        return bool(self.unresolved_metrics)


@dataclass(frozen=True, slots=True)
class InvestigationRecord:
    """One analysis run over one evidence bundle.

    ``evidence_fingerprint`` is the join to the evidence that was read, and
    ``stale_at`` is the moment the case moved on without it. Both are stored; the
    first is what makes the second mechanical rather than a policy reminder
    (docs/SYSTEM_DESIGN.md §9.2).
    """

    id: UUID
    dispute_id: UUID
    version: int
    status: InvestigationRunStatus
    evidence_fingerprint: str
    created_at: datetime
    summary: str
    provider_name: str
    model: str
    prompt_version: str
    engine_version: str
    evidence: EvidenceBundle
    calculation: CalculationRecord | None = None
    findings: tuple[StoredFinding, ...] = ()
    hypotheses: tuple[StoredHypothesis, ...] = ()
    resolution_options: tuple[StoredResolutionOption, ...] = ()
    degradations: tuple[str, ...] = ()
    stage_status: Mapping[str, Any] = field(default_factory=dict)
    stale_at: datetime | None = None
    expires_at: datetime | None = None
    #: The ``Idempotency-Key`` that created this run, kept so a retried request can be
    #: answered with this run instead of starting a second one. Unique per key.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"investigation version must be >= 1, got {self.version}")
        if not self.evidence_fingerprint.startswith("sha256:"):
            raise ValueError(f"fingerprint must be a sha256 digest: {self.evidence_fingerprint!r}")
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "hypotheses", tuple(self.hypotheses))
        object.__setattr__(self, "resolution_options", tuple(self.resolution_options))
        object.__setattr__(self, "degradations", tuple(self.degradations))
        object.__setattr__(self, "stage_status", freeze_json(self.stage_status))
        object.__setattr__(
            self, "idempotency_key", _normalise_idempotency_key(self.idempotency_key)
        )

    @property
    def is_stale(self) -> bool:
        return self.stale_at is not None

    @property
    def has_impact(self) -> bool:
        """True when at least one hypothesis carries a quantified impact."""
        return any(h.impact_decimal is not None for h in self.hypotheses)

    # ------------------------------------------------------------------
    # recovery state
    # ------------------------------------------------------------------
    #
    # A run's per-stage status, its attempt count and its failure reason live inside
    # the ``stage_status`` JSON mapping rather than in dedicated columns. The mapping is
    # already persisted and already frozen; dedicated columns would need a migration and
    # a new CHECK constraint for facts that only the recovery path reads. The accessors
    # below are the only readers, so the shape is documented in one place:
    #
    #   {"overall": "PENDING", "stages": {"COLLECTING_EVIDENCE": "COMPLETE", ...},
    #    "attempts": 2, "failure_reason": null}

    @property
    def is_pending(self) -> bool:
        return self.status is InvestigationRunStatus.PENDING

    @property
    def attempts(self) -> int:
        """How many workflow attempts this run has made (always at least 1)."""
        value = self.stage_status.get("attempts", 1)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 1

    @property
    def failure_reason(self) -> str | None:
        """Why the run is terminal-but-failed, or ``None`` for any other state."""
        value = self.stage_status.get("failure_reason")
        return value if isinstance(value, str) and value else None

    @property
    def resume_from(self) -> str | None:
        """The first stage that has not completed, if the run can be resumed.

        ``None`` when every stage is complete, which is the normal case for a run that
        reached a terminal status by finishing. A ``PENDING`` run with no recorded stages
        resumes from the very beginning.
        """
        stages = self.stage_status.get("stages")
        if not isinstance(stages, Mapping):
            return _STAGE_ORDER[0]
        for stage in _STAGE_ORDER:
            if stages.get(stage) != _STAGE_COMPLETE:
                return stage
        return None

    def is_expired(self, now: datetime) -> bool:
        """Whether the run's TTL has elapsed at ``now``.

        A run with no ``expires_at`` never expires: the TTL is opt-in and an unbounded
        run is a legitimate configuration, not a bug.
        """
        return self.expires_at is not None and self.expires_at <= now

    def with_attempt(self, attempts: int) -> InvestigationRecord:
        """Return a copy recording ``attempts`` made so far."""
        if attempts < 1:
            raise ValueError(f"attempts must be >= 1, got {attempts}")
        state = {**dict(self.stage_status), "attempts": attempts}
        return replace(self, stage_status=state)


@dataclass(frozen=True, slots=True)
class ReviewAction:
    """A reviewer's append-only annotation on a finding.

    ``evidence_fingerprint_seen`` records what the reviewer was looking at. If
    evidence is added afterwards the annotation refers to a view of the facts
    that no longer exists, and the UI can say so -- the same reasoning as
    ``review_decisions.evidence_fingerprint_seen`` in §9.2, applied one phase
    earlier and to a non-approval action.
    """

    id: UUID
    dispute_id: UUID
    investigation_id: UUID
    target_type: ReviewTargetType
    target_id: UUID
    action: ReviewActionKind
    actor_id: str
    actor_role: str
    evidence_fingerprint_seen: str
    created_at: datetime
    rationale: str = ""
    amended_narrative: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.action is ReviewActionKind.AMEND and not (self.amended_narrative or "").strip():
            raise ValueError("AMEND requires amended_narrative")
        if self.action is not ReviewActionKind.AMEND and self.amended_narrative is not None:
            raise ValueError(f"{self.action.value} must not carry amended_narrative")
        # Mirrors ``actor_id_not_blank`` on ``finding_reviews``. Checked here as well so
        # an unattributable annotation fails when it is built rather than at flush
        # time, when the caller no longer holds the object that needs fixing.
        if not self.actor_id.strip():
            raise ValueError("review requires a non-blank actor_id")
        object.__setattr__(
            self, "idempotency_key", _normalise_idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class DisputeDecision:
    """A reviewer's closing verdict on the case.

    Append-only, like every other recorded judgement. ``investigation_id`` names
    the run that was under review when the verdict was made and
    ``evidence_fingerprint_seen`` the evidence that run read, so a later reader can
    tell exactly what the verdict was based on.

    Explicitly not an approval that moves money. ``decision`` closes the lifecycle
    (``RESOLVED``/``REJECTED``); executing an amount is the idempotency phase and
    is deliberately a separate record with its own guards.
    """

    id: UUID
    dispute_id: UUID
    investigation_id: UUID
    decision: DecisionKind
    actor_id: str
    actor_role: str
    evidence_fingerprint_seen: str
    rationale: str
    created_at: datetime
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("a decision requires a non-blank actor_id")
        if not self.rationale.strip():
            raise ValueError("a decision requires a non-blank rationale")
        object.__setattr__(
            self, "idempotency_key", _normalise_idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class DisputeCase:
    """The aggregate root: a case, its evidence, and every run on it.

    ``investigations`` grows but never shrinks, and ``reviews`` is append-only.
    That is what makes "reopen" safe: the new run is ``version`` n+1 and the
    previous n is still there to compare against.

    The aggregate is immutable. ``with_evidence`` and friends return new instances;
    the repository is responsible for making that durable. Making it mutable
    instead would mean a partially-applied ``add_evidence`` could leave the
    fingerprint disagreeing with the bundle, which is the one invariant the whole
    staleness story rests on.
    """

    id: UUID
    external_id: str
    invoice_external_id: str
    status: CaseStatus
    severity: CaseSeverity
    description: str
    evidence: EvidenceBundle
    created_at: datetime
    contract_external_id: str | None = None
    account_id: UUID | None = None
    contract_id: UUID | None = None
    current_investigation_id: UUID | None = None
    investigations: tuple[InvestigationRecord, ...] = ()
    reviews: tuple[ReviewAction, ...] = ()
    decisions: tuple[DisputeDecision, ...] = ()
    #: The validated ingest payload the evidence was built from: the invoice,
    #: contract terms, usage, payments and adjustments as the domain types
    #: serialise them.
    #:
    #: This is here, and not supplied as a service argument at investigation time,
    #: because a *second* investigation has to be possible. Phase 3 took typed
    #: ``RecordedInvoice``/``PriceTerm``/``Payment`` objects that existed only in
    #: memory; without keeping the input, reopening a dispute and re-running it
    #: would require the caller to re-supply every record and there would be no
    #: guarantee they supplied the same ones the evidence was hashed from.
    #:
    #: Empty by default, which is honest: a case built without ingest can be stored
    #: and reviewed, and attempting to investigate it fails for want of an invoice
    #: rather than inventing one.
    source_document: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    #: The ``Idempotency-Key`` the case was opened with, if any. Set once at creation
    #: and never rewritten; a retried "open dispute" replays the stored case instead.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not _EXTERNAL_ID_PATTERN.match(self.external_id):
            raise ValueError(f"malformed dispute external_id: {self.external_id!r}")
        if not self.invoice_external_id.strip():
            raise ValueError("invoice_external_id must not be blank")
        if self.severity.value not in _CASE_SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")
        object.__setattr__(self, "investigations", tuple(self.investigations))
        object.__setattr__(self, "reviews", tuple(self.reviews))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "source_document", freeze_json(self.source_document))
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(
            self, "idempotency_key", _normalise_idempotency_key(self.idempotency_key)
        )

    @property
    def can_investigate(self) -> bool:
        """Whether the ingest payload is present.

        Checked before running rather than after, so the caller gets "this case
        has no invoice to analyse" instead of a recalculation failure on a missing
        key.
        """
        return bool(self.source_document) and bool(self.source_document.get("present", True))

    # ------------------------------------------------------------------
    # derived state
    # ------------------------------------------------------------------

    @property
    def evidence_fingerprint(self) -> str:
        """Fingerprint of everything currently attached to the case.

        Recomputed from the bundle rather than stored on the row. The bundle
        already knows how to hash itself and cannot drift from its own contents;
        a stored column would be a second thing to keep in step.
        """
        return self.evidence.fingerprint

    @property
    def current_investigation(self) -> InvestigationRecord | None:
        if self.current_investigation_id is None:
            return None
        for investigation in self.investigations:
            if investigation.id == self.current_investigation_id:
                return investigation
        return None

    @property
    def is_stale(self) -> bool:
        """Whether the newest run no longer describes the attached evidence."""
        current = self.current_investigation
        return current is not None and current.evidence_fingerprint != self.evidence_fingerprint

    @property
    def next_investigation_version(self) -> int:
        return len(self.investigations) + 1

    @property
    def is_open_for_investigation(self) -> bool:
        return self.status in (
            CaseStatus.OPEN,
            CaseStatus.INVESTIGATING,
            CaseStatus.AWAITING_REVIEW,
            CaseStatus.REOPENED,
        )

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------

    def with_source_document(self, document: Mapping[str, Any]) -> DisputeCase:
        """Replace the ingest payload.

        Separate from :meth:`with_evidence` on purpose. Evidence can be attached
        freely, but the payload is what a later investigation is computed *from*, so
        it is written by exactly one operation (:meth:`CaseService.attach_records`)
        rather than by anything that merely wants to add a fact.

        A no-op returns ``self``. Re-attaching a record that is already present must
        not bump ``version``, because the version is a client's optimistic-concurrency
        token: moving it for a request that changed nothing makes a retry look like a
        concurrent edit and manufactures a spurious conflict for the next writer.
        """
        if _same_document(self.source_document, document):
            return self
        return replace(self, source_document=document, version=self.version + 1)

    def with_evidence(self, items: Sequence[EvidenceItem]) -> DisputeCase:
        """Attach evidence and void any run that did not read it.

        Idempotent by construction: ``EvidenceBundle.of`` de-duplicates on
        ``(natural_key, content_hash)``, so attaching the same snapshot twice is
        a no-op and the fingerprint does not move. That matters because the API
        contract for this endpoint is "idempotent", and implementing that here
        rather than in the service means no caller can get it wrong.

        Prior runs keep their own evidence, findings and ``stale_at`` stamp. They
        are not deleted and not rewritten -- a reviewer needs to see what the
        system believed last Tuesday and why.
        """
        combined = EvidenceBundle.of([*self.evidence.items, *items])
        if combined.fingerprint == self.evidence_fingerprint:
            return self
        marked_at = self._latest_investigation_at()
        investigations = tuple(
            replace(inv, stale_at=inv.stale_at or marked_at)
            if inv.evidence_fingerprint != combined.fingerprint
            else inv
            for inv in self.investigations
        )
        return replace(
            self,
            evidence=combined,
            investigations=investigations,
            version=self.version + 1,
            status=self._status_after_evidence(investigations, combined.fingerprint),
        )

    def _status_after_evidence(
        self, investigations: tuple[InvestigationRecord, ...], new_fingerprint: str
    ) -> CaseStatus:
        """New evidence reopens a closed or reviewing case; an untouched one stays put.

        A case awaiting review whose evidence moved must not keep claiming to be
        awaiting review of facts that no longer exist. Dropping it to REOPENED is
        what forces the next run to be a fresh one instead of a silent
        re-interpretation of stale findings.

        ``new_fingerprint`` is passed in rather than read from ``self`` because the
        bundle has already been replaced by the time this runs, and comparing the
        superseded runs against the *old* fingerprint finds nothing -- which would
        quietly leave the case claiming to be under review on evidence that has
        since changed.
        """
        if self.status in (CaseStatus.RESOLVED, CaseStatus.REJECTED):
            return CaseStatus.REOPENED
        if any(inv.evidence_fingerprint != new_fingerprint for inv in investigations):
            return CaseStatus.REOPENED
        return self.status

    def _latest_investigation_at(self) -> datetime:
        if not self.investigations:
            return self.created_at
        return max(i.created_at for i in self.investigations)

    def with_investigation(self, investigation: InvestigationRecord) -> DisputeCase:
        """Attach a completed run and move the case to review.

        Guards from §3.4 are enforced here:

        * a closed case may only be reopened, never re-investigated directly;
        * an investigation must read the evidence that is currently attached,
          because a run over superseded evidence would be stale on arrival and
          there would be nothing to review.
        """
        if self.status in (CaseStatus.RESOLVED, CaseStatus.REJECTED):
            raise CaseTransitionError(
                f"case {self.external_id} is {self.status.value}; reopen it before investigating"
            )
        if investigation.evidence_fingerprint != self.evidence_fingerprint:
            raise CaseTransitionError(
                "investigation was produced from evidence that is no longer attached to the case"
            )
        if investigation.version != self.next_investigation_version:
            raise CaseTransitionError(
                f"expected investigation version {self.next_investigation_version}, "
                f"got {investigation.version}"
            )
        if investigation.dispute_id != self.id:
            raise CaseTransitionError("investigation belongs to a different dispute")
        return replace(
            self,
            investigations=(*self.investigations, investigation),
            current_investigation_id=investigation.id,
            status=CaseStatus.AWAITING_REVIEW,
            version=self.version + 1,
        )

    # ------------------------------------------------------------------
    # write-ahead investigation lifecycle (Part 7)
    # ------------------------------------------------------------------

    def with_pending_investigation(self, investigation: InvestigationRecord) -> DisputeCase:
        """Record that an investigation has *started*, before any work is done.

        This is the write-ahead half of the recovery design (ADR-008). The row is
        persisted as ``PENDING`` and committed before the workflow runs, so a crash
        mid-run leaves a durable trace of an unfinished investigation rather than no
        trace at all. Only the terminal transition (:meth:`complete_investigation`)
        bumps ``version``; a pending marker must not, or the optimistic-concurrency
        token a caller holds from the previous response would be invalidated for a
        request that has not yet changed the case's meaning.

        Guards mirror :meth:`with_investigation`, plus one this path adds: a case may
        hold at most one pending run at a time. Two concurrent starts would otherwise
        both write a PENDING row and neither could be resolved deterministically.
        """
        if self.status in (CaseStatus.RESOLVED, CaseStatus.REJECTED):
            raise CaseTransitionError(
                f"case {self.external_id} is {self.status.value}; reopen it before investigating"
            )
        if investigation.status is not InvestigationRunStatus.PENDING:
            raise CaseTransitionError("a write-ahead investigation must be PENDING")
        if investigation.dispute_id != self.id:
            raise CaseTransitionError("investigation belongs to a different dispute")
        if investigation.evidence_fingerprint != self.evidence_fingerprint:
            raise CaseTransitionError(
                "investigation was produced from evidence that is no longer attached to the case"
            )
        if any(run.is_pending for run in self.investigations):
            raise CaseTransitionError("this case already has a pending investigation")
        if investigation.version != self.next_investigation_version:
            raise CaseTransitionError(
                f"expected investigation version {self.next_investigation_version}, "
                f"got {investigation.version}"
            )
        return replace(
            self,
            investigations=(*self.investigations, investigation),
            status=CaseStatus.INVESTIGATING,
        )

    def complete_investigation(self, investigation: InvestigationRecord) -> DisputeCase:
        """Replace a pending run with its terminal result and move the case to review.

        The one-way PENDING → terminal edge is enforced here: a run that is not pending
        cannot be completed twice, so a replayed completion is a refusal rather than a
        duplicate finding set. ``version`` advances by one, exactly as it did when
        :meth:`with_investigation` attached a finished run in one step.
        """
        if not investigation.status.is_terminal:
            raise CaseTransitionError("a completed investigation must be terminal")
        index = next(
            (i for i, run in enumerate(self.investigations) if run.id == investigation.id),
            None,
        )
        if index is None:
            raise CaseTransitionError(
                f"investigation {investigation.id} is not part of this case"
            )
        existing = self.investigations[index]
        if existing.status is not InvestigationRunStatus.PENDING:
            raise CaseTransitionError(
                "only a pending investigation can be completed; "
                f"{investigation.id} is {existing.status.value}"
            )
        if investigation.version != existing.version:
            raise CaseTransitionError(
                f"completed investigation must keep version {existing.version}, "
                f"got {investigation.version}"
            )
        if investigation.dispute_id != self.id:
            raise CaseTransitionError("investigation belongs to a different dispute")
        if investigation.evidence_fingerprint != self.evidence_fingerprint:
            raise CaseTransitionError(
                "investigation was produced from evidence that is no longer attached to the case"
            )
        investigations = (
            *self.investigations[:index],
            investigation,
            *self.investigations[index + 1 :],
        )
        return replace(
            self,
            investigations=investigations,
            current_investigation_id=investigation.id,
            status=CaseStatus.AWAITING_REVIEW,
            version=self.version + 1,
        )

    def fail_investigation(self, investigation_id: UUID, *, reason: str) -> DisputeCase:
        """Bring an abandoned pending run to the ``FAILED`` tombstone state.

        Used by retry exhaustion and by the recovery sweep for a TTL-expired run. The
        case returns to ``OPEN`` so it can be investigated again; a failure does not
        leave the case stuck in ``INVESTIGATING`` with nothing running. ``version``
        advances because the case's state genuinely changed.
        """
        index = next(
            (i for i, run in enumerate(self.investigations) if run.id == investigation_id),
            None,
        )
        if index is None:
            raise CaseTransitionError(f"investigation {investigation_id} is not part of this case")
        existing = self.investigations[index]
        if not existing.is_pending:
            raise CaseTransitionError(
                f"investigation {investigation_id} is {existing.status.value}, not pending"
            )
        if not reason.strip():
            raise ValueError("failing an investigation requires a reason")
        failed = replace(
            existing,
            status=InvestigationRunStatus.FAILED,
            stage_status={
                **dict(existing.stage_status),
                "overall": InvestigationRunStatus.FAILED.value,
                "failure_reason": reason.strip(),
            },
        )
        investigations = (
            *self.investigations[:index],
            failed,
            *self.investigations[index + 1 :],
        )
        status = (
            CaseStatus.OPEN if self.status is CaseStatus.INVESTIGATING else self.status
        )
        return replace(
            self,
            investigations=investigations,
            status=status,
            version=self.version + 1,
        )

    def reopened(self, reason: str, at: datetime) -> DisputeCase:
        """Reopen a closed case. Reason is mandatory (§3.4)."""
        if self.status not in (CaseStatus.RESOLVED, CaseStatus.REJECTED):
            raise CaseTransitionError(f"case is {self.status.value}, only a closed case reopens")
        if not reason.strip():
            raise ValueError("reopening requires a reason")
        return replace(
            self,
            status=CaseStatus.REOPENED,
            description=f"{self.description}\n\n[reopened] {reason.strip()}",
            version=self.version + 1,
        )

    def with_review(self, action: ReviewAction) -> DisputeCase:
        """Record a reviewer annotation.

        The annotation is refused when the investigation it targets is already
        stale. Letting a reviewer annotate findings they can no longer see the
        evidence behind would record a judgement against a superseded view.
        """
        target = next((i for i in self.investigations if i.id == action.investigation_id), None)
        if target is None:
            raise CaseTransitionError(
                f"investigation {action.investigation_id} is not part of this case"
            )
        if target.evidence_fingerprint != self.evidence_fingerprint:
            raise CaseTransitionError(
                "investigation is stale; reopen or re-investigate before recording a review"
            )
        if not self._target_exists(target, action):
            raise CaseTransitionError(f"{action.target_type.value} {action.target_id} not found")
        return replace(self, reviews=(*self.reviews, action), version=self.version + 1)

    def decided(self, decision: DisputeDecision) -> DisputeCase:
        """Close the case with a reviewer's verdict.

        The safety property this guard provides is the point of the endpoint: a
        case may only be decided by the run that is actually under review, over
        evidence that is still attached, and only after that run has been reviewed
        at least once -- a verdict on a run nobody has looked at is not a review,
        it is an order.

        ``decision.investigation_id`` must be the *current* run, not a superseded
        one: deciding a case against version 1 while version 2 sits on it would
        record a verdict the reviewer could not have compared. New evidence since
        the run (``is_stale``) therefore also refuses the decision, exactly as it
        refuses a review.
        """
        if self.status is not CaseStatus.AWAITING_REVIEW:
            raise CaseTransitionError(
                f"case {self.external_id} is {self.status.value}; "
                "a case is decided only while it awaits review"
            )
        if decision.investigation_id != self.current_investigation_id:
            raise CaseTransitionError(
                "a decision must target the investigation that is currently under review"
            )
        if self.is_stale:
            raise CaseTransitionError(
                "investigation is stale; re-investigate before deciding the case"
            )
        if not any(review.investigation_id == decision.investigation_id for review in self.reviews):
            raise CaseTransitionError(
                "a case cannot be decided before the run under review has been reviewed"
            )
        return replace(
            self,
            decisions=(*self.decisions, decision),
            status=CaseStatus(decision.decision.value),
            version=self.version + 1,
        )

    @staticmethod
    def _target_exists(investigation: InvestigationRecord, action: ReviewAction) -> bool:
        """Addressability check, keyed on the target type.

        Findings, hypotheses and options all have UUIDs but live in separate
        records with no shared id space, so the id cannot be validated by looking
        it up generically. Checking the type-specific collection is what stops a
        well-formed request from annotating a nonexistent finding.
        """
        wanted = action.target_id
        if action.target_type is ReviewTargetType.HYPOTHESIS:
            return any(hypothesis.id == wanted for hypothesis in investigation.hypotheses)
        if action.target_type is ReviewTargetType.RESOLUTION_OPTION:
            return any(option.id == wanted for option in investigation.resolution_options)
        return any(finding.id == wanted for finding in investigation.findings)

    # ------------------------------------------------------------------
    # evidence lookup
    # ------------------------------------------------------------------

    def evidence_for(self, investigation: InvestigationRecord) -> tuple[EvidenceItem, ...]:
        """The exact snapshots an investigation read.

        Stored per investigation rather than joined by dispute, which is the
        whole point: version 1 must keep showing version 1's evidence after
        version 2's arrives, or "compare with what we saw then" is impossible.
        """
        return tuple(investigation.evidence.items)

    def invoice_evidence(self) -> EvidenceItem | None:
        invoices = self.evidence.of_type(EvidenceType.INVOICE)
        return invoices[0] if invoices else None

    def contract_evidence(self) -> tuple[EvidenceItem, ...]:
        return self.evidence.of_type(EvidenceType.CONTRACT_TERM)


def _same_document(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Whether two ingest payloads carry the same facts.

    Compared as canonical JSON rather than with ``==``: one side comes from a frozen
    aggregate (read-only mappings, tuples) and the other from a freshly built dict, so
    ``==`` would report a difference for documents whose contents are identical and
    make every attach look like a change.
    """
    try:
        return canonical_json(thaw_json(left)) == canonical_json(thaw_json(right))
    except (TypeError, ValueError):  # pragma: no cover - only for non-JSON payloads
        return False
