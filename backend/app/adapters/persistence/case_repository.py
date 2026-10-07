"""The real PostgreSQL adapter for :class:`~app.ports.cases.CaseRepository`.

Append-only, in one transaction, with the aggregate as the unit of storage.

What ``save`` does, and why it is shaped that way
-------------------------------------------------
A case is a tree: one dispute row, its evidence snapshots, and for each run a row
in ``investigations``, one in ``calculations``, one per finding/hypothesis/option,
and the join rows naming the snapshots that run read. Saving means reconciling
that tree, which comes down to three questions:

1. **Has this run been stored before?** Keyed on ``investigations.id``. Runs,
   calculations, findings, hypotheses and options are inserted and never updated,
   so "already there" means the whole subtree is already there. This is what makes
   version 1 still say what it said after version 2 lands.
2. **Is this snapshot new?** Keyed on
   ``(dispute_id, natural_key, content_hash)`` with ``ON CONFLICT DO NOTHING``,
   which is the database-level half of the idempotency guarantee on the attach
   endpoint.
3. **Has the case itself moved?** A ``WHERE id = :id AND version = :expected``
   update, so a caller that read version 3 cannot silently overwrite version 4.

Optimistic concurrency, not last-write-wins
------------------------------------------
``expected_version`` is checked in the ``WHERE`` clause rather than with a
``SELECT`` first. Two reviewers annotating the same case is the normal case, not
the exception, and a read-then-write check has a window between the two statements
that a concurrent writer walks straight through. The check belongs in the one
statement that does the writing.

Transaction scope
-----------------
One transaction per ``save``. A partially written case -- dispute updated,
findings missing -- is worse than a failed save, because the aggregate's
invariants (fingerprint agreeing with its evidence) would be violated in the
database while the in-memory copy looked fine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import Insert as MySQLInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.case_models import (
    CalculationRow,
    DisputeEvidenceRow,
    DisputeRow,
    FindingReviewRow,
    InvestigationEvidenceRow,
    InvestigationFindingRow,
    InvestigationHypothesisRow,
    InvestigationResolutionOptionRow,
    InvestigationRow,
)
from app.domain.cases import (
    CalculationRecord,
    CaseSeverity,
    CaseStatus,
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
from app.domain.json_frozen import thaw_json
from app.ports.cases import CaseConflictError, CaseNotFoundError

__all__ = ["SqlAlchemyCaseRepository"]

#: Amounts go in and come out as text. Keeping the conversion in one place means a
#: ``NUMERIC`` column never has to guess the scale, and a ``Decimal`` never has to
#: survive a round trip through ``float`` because a caller formatted it for JSON.
_EXPECTED_SCALE = "0.0001"


def _amount(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(_EXPECTED_SCALE))


def _amount_text(value: Decimal | None) -> str | None:
    """Render for storage, or ``None`` for "does not exist".

    ``None`` is preserved rather than becoming zero because an unreconciled invoice
    has no outstanding balance and an unassessable hypothesis has no impact. Zero
    would answer "nothing is owed", which is a claim, not a gap.
    """
    return None if value is None else str(_amount(value))


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(Decimal(_EXPECTED_SCALE)))


def _probability_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.0001")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyCaseRepository:
    """PostgreSQL implementation of the case repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def exists(self, dispute_id: UUID) -> bool:
        return (
            self._session.execute(
                sa.select(sa.literal(True)).select_from(DisputeRow).where(DisputeRow.id == dispute_id)
            ).first()
            is not None
        )

    def get(self, dispute_id: UUID) -> DisputeCase:
        row = self._session.get(DisputeRow, dispute_id)
        if row is None:
            raise CaseNotFoundError(f"no dispute with id {dispute_id}")
        return self._hydrate(row)

    def get_by_external_id(self, external_id: str) -> DisputeCase:
        case = self.find_by_external_id(external_id)
        if case is None:
            raise CaseNotFoundError(f"no dispute with external_id {external_id!r}")
        return case

    def find_by_external_id(self, external_id: str) -> DisputeCase | None:
        row = self._session.execute(
            sa.select(DisputeRow).where(DisputeRow.external_id == external_id)
        ).scalar_one_or_none()
        return None if row is None else self._hydrate(row)

    def list(
        self, *, status: CaseStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[DisputeCase, ...]:
        # Newest first. The id is descending so this agrees with the in-memory
        # adapter: both stores must return the same order for the same data, or a
        # reviewer would see the queue reshuffle when someone points the app at
        # PostgreSQL. Equal timestamps are only possible when the clock is coarse, and
        # then any stable order will do -- what matters is that it is the same one.
        query = sa.select(DisputeRow).order_by(DisputeRow.created_at.desc(), DisputeRow.id.desc())
        if status is not None:
            query = query.where(DisputeRow.status == status.value)
        rows = self._session.execute(query.limit(limit).offset(offset)).scalars().all()
        return tuple(self._hydrate(row) for row in rows)

    def count(self, *, status: CaseStatus | None = None) -> int:
        query = sa.select(sa.func.count()).select_from(DisputeRow)
        if status is not None:
            query = query.where(DisputeRow.status == status.value)
        return int(self._session.execute(query).scalar_one())

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def save(self, case: DisputeCase, *, expected_version: int) -> None:
        """Persist the aggregate, inserting what is new and never rewriting what is not.

        Child rows are written before the conditional update of ``disputes``. The
        update is a core statement, so it executes the moment it is issued and
        never joins the unit of work's dependency ordering; if it ran first it
        would set ``current_investigation_id`` to an id whose row has not been
        inserted yet, which the foreign key rejects.
        """
        existing = self._session.get(DisputeRow, case.id)
        if expected_version == 0:
            if existing is not None:
                raise CaseConflictError(
                    f"dispute {case.id} already exists; expected a create"
                )
            self._insert_case(case)
            self._insert_new_rows(case)
        else:
            if existing is None:
                raise CaseNotFoundError(f"no dispute with id {case.id}")
            self._insert_new_rows(case)
            self._update_case(existing, case, expected_version)
            self._session.flush()

    def _insert_new_rows(self, case: DisputeCase) -> None:
        """Write evidence, investigations and reviews, then settle the new rows.

        ``_insert_investigation`` flushes as it goes so that run and subtree are
        written parent-first; this flush then settles them in the transaction and is
        what lets ``_update_case`` (which runs afterwards) point at a row that
        exists.
        """
        self._insert_new_evidence(case)
        evidence_ids = self._evidence_id_map(case)
        self._insert_new_investigations(case, evidence_ids)
        self._insert_new_reviews(case)
        self._session.flush()

    def _insert_case(self, case: DisputeCase) -> None:
        row = DisputeRow(
            id=case.id,
            external_id=case.external_id,
            invoice_external_id=case.invoice_external_id,
            contract_external_id=case.contract_external_id,
            account_id=case.account_id,
            contract_id=case.contract_id,
            status=case.status.value,
            severity=case.severity.value,
            description=case.description,
            source_document=_source_document(case),
            evidence_fingerprint=case.evidence_fingerprint,
            current_investigation_id=case.current_investigation_id,
            version=case.version,
            created_at=case.created_at,
            updated_at=_now(),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise CaseConflictError(
                f"dispute {case.external_id!r} already exists (external_id is unique)"
            ) from exc

    def _update_case(
        self, row: DisputeRow, case: DisputeCase, expected_version: int
    ) -> None:
        """Conditional update.

        The ``version`` predicate is the concurrency check. If it fails, something
        else wrote this case since the caller read it, and the caller's aggregate
        is based on state that no longer exists.
        """
        result = self._session.execute(
            sa.update(DisputeRow)
            .where(DisputeRow.id == case.id, DisputeRow.version == expected_version)
            .values(
                status=case.status.value,
                severity=case.severity.value,
                description=case.description,
                source_document=_source_document(case),
                evidence_fingerprint=case.evidence_fingerprint,
                current_investigation_id=case.current_investigation_id,
                version=case.version,
                updated_at=_now(),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            raise CaseConflictError(
                f"dispute {case.id} changed since it was read "
                f"(expected version {expected_version}, stored is something else)"
            )

    def _insert_new_evidence(self, case: DisputeCase) -> None:
        """Insert snapshots not already stored for this case.

        ``ON CONFLICT DO NOTHING`` rather than a read-then-insert: two concurrent
        attach requests carrying the same snapshot both find it absent and both
        try to write, and the second must become a no-op rather than a 500. The
        unique index is the arbiter, which is the point of having named it.
        """
        existing = {
            (natural_key, content_hash)
            for natural_key, content_hash in self._session.execute(
                sa.select(DisputeEvidenceRow.natural_key, DisputeEvidenceRow.content_hash).where(
                    DisputeEvidenceRow.dispute_id == case.id
                )
            ).all()
        }
        pending = [
            {
                "id": item_id,
                "dispute_id": case.id,
                "evidence_type": item.evidence_type.value,
                "natural_key": item.natural_key,
                "content_hash": item.content_hash,
                "snapshot": thaw_json(item.snapshot),
                "captured_at": case.created_at,
            }
            for item_id, item in _evidence_with_ids(case)
            if (item.natural_key, item.content_hash) not in existing
        ]
        if not pending:
            return
        arbiter = [
            DisputeEvidenceRow.dispute_id,
            DisputeEvidenceRow.natural_key,
            DisputeEvidenceRow.content_hash,
        ]
        # Two spellings of the same "insert unless the unique index already has it".
        statement: Any
        if self._session.get_bind().dialect.name == "postgresql":
            statement = pg_insert(DisputeEvidenceRow).values(pending).on_conflict_do_nothing(
                index_elements=arbiter
            )
        else:
            # MySQL has no ON CONFLICT. ON DUPLICATE KEY UPDATE with every column set
            # to itself is the equivalent no-op: the unique index is still the arbiter,
            # and unlike INSERT IGNORE it does not also silence unrelated errors.
            # Assigning each arbiter column to itself is the no-op form; it renders
            # as ``col = table.col`` and needs no ``inserted`` alias.
            upsert: MySQLInsert = mysql_insert(DisputeEvidenceRow).values(pending)
            statement = upsert.on_duplicate_key_update(
                **{column.name: column for column in arbiter}
            )
        self._session.execute(statement)

    def _evidence_id_map(self, case: DisputeCase) -> dict[tuple[str, str], UUID]:
        rows = self._session.execute(
            sa.select(
                DisputeEvidenceRow.id,
                DisputeEvidenceRow.natural_key,
                DisputeEvidenceRow.content_hash,
            ).where(DisputeEvidenceRow.dispute_id == case.id)
        ).all()
        return {(natural_key, content_hash): row_id for row_id, natural_key, content_hash in rows}

    def _insert_new_investigations(
        self, case: DisputeCase, evidence_ids: dict[tuple[str, str], UUID]
    ) -> None:
        """Insert runs whose id is not stored yet, with their whole subtree."""
        stored = set(
            self._session.execute(
                sa.select(InvestigationRow.id).where(InvestigationRow.dispute_id == case.id)
            ).scalars()
        )
        for investigation in case.investigations:
            if investigation.id in stored:
                continue
            self._insert_investigation(case, investigation, evidence_ids)

    def _insert_investigation(
        self,
        case: DisputeCase,
        investigation: InvestigationRecord,
        evidence_ids: dict[tuple[str, str], UUID],
    ) -> None:
        self._session.add(
            InvestigationRow(
                id=investigation.id,
                dispute_id=case.id,
                version=investigation.version,
                status=investigation.status.value,
                evidence_fingerprint=investigation.evidence_fingerprint,
                summary=investigation.summary,
                provider_name=investigation.provider_name,
                model=investigation.model,
                prompt_version=investigation.prompt_version,
                engine_version=investigation.engine_version,
                stage_status=thaw_json(investigation.stage_status),
                degradations=list(investigation.degradations),
                stale_at=investigation.stale_at,
                expires_at=investigation.expires_at,
                created_at=investigation.created_at,
            )
        )
        # Every row below points at this one. The mappers carry no relationship(),
        # so the unit of work has no dependency to sort on and would emit the
        # children first; the foreign keys reject that. One flush here buys the
        # ordering explicitly, inside the same transaction.
        self._session.flush()

        if investigation.calculation is not None:
            calculation = investigation.calculation
            self._session.add(
                CalculationRow(
                    dispute_id=case.id,
                    investigation_id=investigation.id,
                    currency=calculation.currency,
                    engine_version=calculation.engine_version,
                    recalculated_total=_amount(Decimal(calculation.recalculated_total)),
                    recorded_total=_amount(Decimal(calculation.recorded_total))
                    if calculation.recorded_total is not None
                    else None,
                    difference=_amount(Decimal(calculation.difference))
                    if calculation.difference is not None
                    else None,
                    outstanding=_amount(Decimal(calculation.outstanding))
                    if calculation.outstanding is not None
                    else None,
                    allocated_payments=_amount(Decimal(calculation.allocated_payments))
                    if calculation.allocated_payments is not None
                    else None,
                    net_adjustments=_amount(Decimal(calculation.net_adjustments))
                    if calculation.net_adjustments is not None
                    else None,
                    is_complete=calculation.is_complete,
                    is_provisional=calculation.is_provisional,
                    unresolved_metrics=list(calculation.unresolved_metrics),
                    trace=thaw_json(calculation.trace),
                    invoice=thaw_json(calculation.invoice),
                    balance=thaw_json(calculation.balance),
                    usage_summaries=[thaw_json(u) for u in calculation.usage_summaries],
                    created_at=investigation.created_at,
                )
            )

        # The join rows are what make "the evidence this run read" answerable
        # after newer snapshots arrive.
        for item in investigation.evidence.items:
            evidence_id = evidence_ids.get((item.natural_key, item.content_hash))
            if evidence_id is None:  # pragma: no cover - guarded by _insert_new_evidence
                raise CaseConflictError(
                    f"evidence {item.natural_key!r} was not stored, so run "
                    f"{investigation.id} cannot reference it"
                )
            self._session.add(
                InvestigationEvidenceRow(
                    investigation_id=investigation.id, evidence_item_id=evidence_id
                )
            )

        for finding in investigation.findings:
            self._session.add(
                InvestigationFindingRow(
                    id=finding.id,
                    investigation_id=investigation.id,
                    code=finding.code,
                    severity=finding.severity,
                    category=finding.category,
                    narrative=finding.narrative,
                    confidence=Decimal(_probability_text(finding.confidence) or "0"),
                    supporting_evidence=list(finding.supporting_evidence),
                    created_at=investigation.created_at,
                )
            )

        for hypothesis in investigation.hypotheses:
            self._session.add(
                InvestigationHypothesisRow(
                    id=hypothesis.id,
                    investigation_id=investigation.id,
                    hypothesis_code=hypothesis.hypothesis_code,
                    title=hypothesis.title,
                    narrative=hypothesis.narrative,
                    likelihood=Decimal(_probability_text(hypothesis.likelihood) or "0")
                    if hypothesis.likelihood is not None
                    else None,
                    status=hypothesis.status,
                    supporting_evidence=list(hypothesis.supporting_evidence),
                    refuting_evidence=list(hypothesis.refuting_evidence),
                    metric_key=hypothesis.metric_key,
                    impact_amount=_amount(hypothesis.impact_decimal),
                    impact_currency=hypothesis.impact_currency,
                    impact_basis=hypothesis.impact_basis,
                    not_assessable_reason=hypothesis.not_assessable_reason,
                    impact_trace=thaw_json(hypothesis.impact_trace),
                    created_at=investigation.created_at,
                )
            )

        for option in investigation.resolution_options:
            self._session.add(
                InvestigationResolutionOptionRow(
                    id=option.id,
                    investigation_id=investigation.id,
                    option_type=option.option_type,
                    title=option.title,
                    rationale=option.rationale,
                    requires_human_approval=option.requires_human_approval,
                    hypothesis_code=option.hypothesis_code,
                    supporting_evidence=list(option.supporting_evidence),
                    created_at=investigation.created_at,
                )
            )

    def _insert_new_reviews(self, case: DisputeCase) -> None:
        """Append annotations not already stored.

        Keyed on id, and inserted only. A review is a judgement made against a
        particular view of the evidence; rewriting one would be rewriting the fact
        that the judgement was made.
        """
        if not case.reviews:
            return
        stored = set(
            self._session.execute(
                sa.select(FindingReviewRow.id).where(FindingReviewRow.dispute_id == case.id)
            ).scalars()
        )
        for action in case.reviews:
            if action.id in stored:
                continue
            self._session.add(
                FindingReviewRow(
                    id=action.id,
                    dispute_id=case.id,
                    investigation_id=action.investigation_id,
                    target_type=action.target_type.value,
                    target_id=action.target_id,
                    action=action.action.value,
                    actor_id=action.actor_id,
                    actor_role=action.actor_role,
                    evidence_fingerprint_seen=action.evidence_fingerprint_seen,
                    rationale=action.rationale,
                    amended_narrative=action.amended_narrative,
                    created_at=action.created_at,
                )
            )

    # ------------------------------------------------------------------
    # hydration
    # ------------------------------------------------------------------

    def _hydrate(self, row: DisputeRow) -> DisputeCase:
        evidence_by_id = self._load_evidence(row.id)
        run_rows = self._session.execute(
            sa.select(InvestigationRow)
            .where(InvestigationRow.dispute_id == row.id)
            .order_by(InvestigationRow.version)
        ).scalars().all()
        investigations = tuple(
            self._hydrate_investigation(run, evidence_by_id) for run in run_rows
        )
        reviews = self._hydrate_reviews(row.id)
        return DisputeCase(
            id=row.id,
            external_id=row.external_id,
            invoice_external_id=row.invoice_external_id,
            contract_external_id=row.contract_external_id,
            account_id=row.account_id,
            contract_id=row.contract_id,
            status=CaseStatus(row.status),
            severity=CaseSeverity(row.severity),
            description=row.description,
            evidence=EvidenceBundle.of(list(evidence_by_id.values())),
            created_at=row.created_at,
            current_investigation_id=row.current_investigation_id,
            investigations=investigations,
            reviews=reviews,
            source_document=row.source_document or {},
            version=row.version,
        )

    def _load_evidence(self, dispute_id: UUID) -> dict[UUID, EvidenceItem]:
        """All snapshots for a case, keyed by row id.

        Order comes from ``EvidenceBundle`` itself (it sorts by natural key), so
        the load order does not matter and the fingerprint is reproducible
        regardless of how the rows came back.
        """
        rows = self._session.execute(
            sa.select(DisputeEvidenceRow).where(DisputeEvidenceRow.dispute_id == dispute_id)
        ).scalars().all()
        return {
            row.id: EvidenceItem.create(
                row.natural_key, EvidenceType(row.evidence_type), row.snapshot
            )
            for row in rows
        }

    def _hydrate_investigation(
        self, row: InvestigationRow, evidence_by_id: dict[UUID, EvidenceItem]
    ) -> InvestigationRecord:
        item_ids = self._session.execute(
            sa.select(InvestigationEvidenceRow.evidence_item_id).where(
                InvestigationEvidenceRow.investigation_id == row.id
            )
        ).scalars().all()
        run_evidence = EvidenceBundle.of(
            [evidence_by_id[i] for i in item_ids if i in evidence_by_id]
        )
        findings = self._hydrate_findings(row.id)
        hypotheses = self._hydrate_hypotheses(row.id)
        options = self._hydrate_options(row.id)
        return InvestigationRecord(
            id=row.id,
            dispute_id=row.dispute_id,
            version=row.version,
            status=InvestigationRunStatus(row.status),
            evidence_fingerprint=row.evidence_fingerprint,
            created_at=row.created_at,
            summary=row.summary,
            provider_name=row.provider_name,
            model=row.model,
            prompt_version=row.prompt_version,
            engine_version=row.engine_version,
            evidence=run_evidence,
            calculation=self._hydrate_calculation(row),
            findings=findings,
            hypotheses=hypotheses,
            resolution_options=options,
            degradations=tuple(row.degradations),
            stage_status=row.stage_status,
            stale_at=row.stale_at,
            expires_at=row.expires_at,
        )

    def _hydrate_calculation(self, row: InvestigationRow) -> CalculationRecord | None:
        calc = self._session.execute(
            sa.select(CalculationRow).where(CalculationRow.investigation_id == row.id)
        ).scalar_one_or_none()
        if calc is None:
            return None
        return CalculationRecord(
            currency=calc.currency,
            engine_version=calc.engine_version,
            recalculated_total=_amount_text(calc.recalculated_total) or "0.0000",
            is_complete=calc.is_complete,
            is_provisional=calc.is_provisional,
            recorded_total=_amount_text(calc.recorded_total),
            difference=_amount_text(calc.difference),
            outstanding=_amount_text(calc.outstanding),
            allocated_payments=_amount_text(calc.allocated_payments),
            net_adjustments=_amount_text(calc.net_adjustments),
            unresolved_metrics=tuple(calc.unresolved_metrics),
            trace=calc.trace,
            invoice=calc.invoice,
            balance=calc.balance,
            usage_summaries=tuple(dict(u) for u in calc.usage_summaries),
        )

    def _hydrate_findings(self, investigation_id: UUID) -> tuple[StoredFinding, ...]:
        rows = self._session.execute(
            sa.select(InvestigationFindingRow)
            .where(InvestigationFindingRow.investigation_id == investigation_id)
            .order_by(InvestigationFindingRow.code, InvestigationFindingRow.id)
        ).scalars().all()
        return tuple(
            StoredFinding(
                id=row.id,
                code=row.code,
                severity=row.severity,
                category=row.category,
                narrative=row.narrative,
                confidence=row.confidence,
                supporting_evidence=tuple(row.supporting_evidence),
            )
            for row in rows
        )

    def _hydrate_hypotheses(self, investigation_id: UUID) -> tuple[StoredHypothesis, ...]:
        rows = self._session.execute(
            sa.select(InvestigationHypothesisRow)
            .where(InvestigationHypothesisRow.investigation_id == investigation_id)
            .order_by(InvestigationHypothesisRow.hypothesis_code, InvestigationHypothesisRow.id)
        ).scalars().all()
        return tuple(
            StoredHypothesis(
                id=row.id,
                hypothesis_code=row.hypothesis_code,
                title=row.title,
                narrative=row.narrative,
                likelihood=row.likelihood,
                status=row.status,
                supporting_evidence=tuple(row.supporting_evidence),
                refuting_evidence=tuple(row.refuting_evidence),
                metric_key=row.metric_key,
                impact_amount=_amount_text(row.impact_amount),
                impact_currency=row.impact_currency,
                impact_basis=row.impact_basis,
                not_assessable_reason=row.not_assessable_reason,
                impact_trace=row.impact_trace,
            )
            for row in rows
        )

    def _hydrate_options(self, investigation_id: UUID) -> tuple[StoredResolutionOption, ...]:
        rows = self._session.execute(
            sa.select(InvestigationResolutionOptionRow)
            .where(InvestigationResolutionOptionRow.investigation_id == investigation_id)
            .order_by(InvestigationResolutionOptionRow.option_type, InvestigationResolutionOptionRow.id)
        ).scalars().all()
        return tuple(
            StoredResolutionOption(
                id=row.id,
                option_type=row.option_type,
                title=row.title,
                rationale=row.rationale,
                requires_human_approval=row.requires_human_approval,
                hypothesis_code=row.hypothesis_code,
                supporting_evidence=tuple(row.supporting_evidence),
            )
            for row in rows
        )

    def _hydrate_reviews(self, dispute_id: UUID) -> tuple[ReviewAction, ...]:
        rows = self._session.execute(
            sa.select(FindingReviewRow)
            .where(FindingReviewRow.dispute_id == dispute_id)
            .order_by(FindingReviewRow.created_at, FindingReviewRow.id)
        ).scalars().all()
        return tuple(
            ReviewAction(
                id=row.id,
                dispute_id=row.dispute_id,
                investigation_id=row.investigation_id,
                target_type=ReviewTargetType(row.target_type),
                target_id=row.target_id,
                action=ReviewActionKind(row.action),
                actor_id=row.actor_id,
                actor_role=row.actor_role,
                evidence_fingerprint_seen=row.evidence_fingerprint_seen,
                created_at=row.created_at,
                rationale=row.rationale,
                amended_narrative=row.amended_narrative,
            )
            for row in rows
        )


def _evidence_with_ids(case: DisputeCase) -> list[tuple[UUID, EvidenceItem]]:
    """Evidence items paired with a deterministic id.

    The id has to be derived, not random: ``save`` may be called twice for the same
    case (a retry after a conflict), and a fresh random id each time would insert a
    duplicate snapshot row under a new primary key -- which the unique index does not
    catch, because it covers the business key and not ``id``. Deriving the id from the
    dispute id and the content hash makes the insert idempotent at the primary key as
    well as at the business key.

    Module level rather than a method because ``list`` is a method on this class, and
    a bare ``list[...]`` annotation inside the class body resolves to that method
    rather than to the builtin.
    """
    return [
        (uuid5(case.id, f"{item.natural_key}\x00{item.content_hash}"), item)
        for item in case.evidence.items
    ]


def _source_document(case: DisputeCase) -> dict[str, Any]:
    """The ingest payload, as a plain dict ready for JSONB.

    The aggregate holds it deep-frozen (``MappingProxyType`` all the way down) and
    the driver needs ordinary containers, so this is the one place the frozen form
    is converted back. Doing it here rather than in the aggregate keeps the domain
    type honest about immutability.

    A case with no payload is stored as ``{"present": False}`` rather than ``{}``
    because ``DisputeCase.can_investigate`` distinguishes the two, and an empty
    object would be ambiguous with a malformed one.
    """
    document = thaw_json(case.source_document)
    return document or {"present": False}