"""The persistence boundary for dispute cases.

Hexagonal, and deliberately coarse. The unit of storage is the whole
:class:`~app.domain.cases.DisputeCase` aggregate rather than one row per entity,
because the invariants that matter -- the case fingerprint agreeing with its
evidence, a run's fingerprint agreeing with the evidence it read -- span rows.
A row-level repository would push that reconciliation into every caller and get
it wrong somewhere.

The cost of coarseness is that :meth:`CaseRepository.save` writes the aggregate.
Implementations must still respect append-only-ness: runs, findings, hypotheses,
options and reviews are inserted, never updated, so that "version 1 concluded X"
stays true forever. That rule is stated once here and enforced in both adapters.

The one exception is the recovery lifecycle (ADR-008): a run written ahead as
``PENDING`` is later replaced by its terminal twin through
:meth:`CaseRepository.begin_investigation` and :meth:`CaseRepository.save`. That is a
single one-way edge, ``PENDING`` → terminal, and nothing else may update a stored run.

Two implementations exist and the difference between them is the point:

* :class:`~app.adapters.persistence.case_repository.SqlAlchemyCaseRepository` --
  the real one, PostgreSQL only.
* :class:`~app.adapters.persistence.memory_case_repository.InMemoryCaseRepository`
  -- an in-process store for development and tests when no database is
  reachable. It obeys every rule in this protocol, but it is not durability and
  nothing may claim it is.

Optimistic concurrency is part of the protocol rather than an implementation
detail: :meth:`save` takes the version the caller read and raises
:class:`CaseConflictError` if the stored version has moved on. Two reviewers
annotating the same case is normal, and last-write-wins would silently drop one
of the annotations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.audit import AuditEvent
from app.domain.cases import CaseStatus, DisputeCase

__all__ = ["CaseConflictError", "CaseNotFoundError", "CaseRepository"]


class CaseNotFoundError(LookupError):
    """No case with the requested id or external id.

    Split from ``CaseConflictError`` so a 404 and a 409 cannot be confused by a
    caller that catches ``LookupError`` broadly.
    """


class CaseConflictError(RuntimeError):
    """The stored case changed since the caller read it.

    Raised by :meth:`CaseRepository.save` when the caller's version is behind.
    The caller is expected to re-read and decide, not to retry blindly.
    """


@runtime_checkable
class CaseRepository(Protocol):
    """Load and store dispute cases.

    Implementations are responsible for transactional integrity: a ``save`` that
    writes a dispute and its investigations must not leave half of them behind.
    The SQL implementation gets this from one transaction; the in-memory one from
    building the new state before swapping it in.
    """

    def save(self, case: DisputeCase, *, expected_version: int) -> None:
        """Insert or update ``case``.

        Args:
            case: the aggregate to persist. Its ``version`` is ignored in favour of
                ``expected_version``, which is the version the caller believes it
                is replacing.
            expected_version: the version read by the caller. ``0`` means "this case
                does not exist yet and must not already be present", which is how a
                create avoids silently overwriting a case created concurrently
                under the same external id.

        Raises:
            CaseConflictError: the stored version differs, or an insert was expected
                but the id is already taken.
        """
        ...

    def begin_investigation(self, case: DisputeCase, *, expected_version: int) -> None:
        """Persist a case whose newest run is ``PENDING`` and commit on its own.

        The write-ahead marker of ADR-008. Unlike :meth:`save`, this call must make the
        pending row durable *before* the workflow runs, so a crash mid-investigation
        leaves a recoverable trace rather than nothing. The SQL implementation therefore
        commits immediately instead of joining the request's transaction -- a deliberate
        and documented exception to "one transaction per request", scoped to this marker.

        Append-only-ness is relaxed for exactly one edge: a ``PENDING`` run may later be
        replaced by its terminal twin through :meth:`save`. No other update to a stored
        run is permitted.

        Args:
            case: the aggregate carrying the new ``PENDING`` run.
            expected_version: the version the caller read; the marker does not bump the
                case version, so this is the same version the caller already holds.

        Raises:
            CaseConflictError: the stored version differs, or the case already has a
                pending run.
        """
        ...

    def commit(self) -> None:
        """Make the current unit of work durable.

        The second documented exception to "one transaction per request", alongside
        :meth:`begin_investigation`. The write-ahead marker is committed on its own
        before the workflow runs, so the tombstone that replaces it when the run fails
        must be committed on its own too: otherwise the request-level rollback that
        follows the failure would erase the tombstone and leave the case stuck in
        ``INVESTIGATING`` behind a pending run no later request can get past.

        For implementations without transactions this is a no-op.
        """
        ...

    def list_pending_investigations(
        self, *, started_before: datetime
    ) -> tuple[tuple[UUID, UUID], ...]:
        """Return ``(dispute_id, investigation_id)`` for pending runs started early.

        Backs the recovery sweep: a pending run older than the retention window is one a
        process died in the middle of.         Ordered oldest-first so a recovery works through
        them in the order they were abandoned.
        """
        ...

    def get(self, dispute_id: UUID) -> DisputeCase:
        """Return the case with this id.

        Raises:
            CaseNotFoundError: no such case.
        """
        ...

    def get_by_external_id(self, external_id: str) -> DisputeCase:
        """Return the case with this business identifier.

        Raises:
            CaseNotFoundError: no such case.
        """
        ...

    def find_by_external_id(self, external_id: str) -> DisputeCase | None:
        """Return the case with this business identifier, or ``None``.

        The non-raising twin of :meth:`get_by_external_id`, for create paths that
        must distinguish "already exists" from "does not exist".
        """
        ...

    def find_by_idempotency_key(self, key: str) -> DisputeCase | None:
        """Return the case opened with ``key``, or ``None``.

        Backs the ``Idempotency-Key`` replay for the open-dispute endpoint. Keys on
        investigations, reviews and decisions are not looked up here: those requests
        carry the dispute id, so the service searches the hydrated aggregate instead.
        """
        ...

    def list(
        self, *, status: CaseStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[DisputeCase, ...]:
        """Return cases newest-first, optionally filtered by status.

        Loading a whole aggregate per row is heavier than a list view needs, so
        implementations may populate ``investigations`` and ``reviews`` with only
        what the list requires. The contract is deliberately loose here: a list
        response that omits finding bodies is fine, one that omits the case status
        or fingerprint is not.
        """
        ...

    def count(self, *, status: CaseStatus | None = None) -> int:
        """Total cases matching the filter, for pagination."""
        ...

    def exists(self, dispute_id: UUID) -> bool:
        """Whether a case with this id is stored."""
        ...

    # ------------------------------------------------------------------
    # audit trail
    # ------------------------------------------------------------------
    #
    # The trail is a separate concern from the aggregate and deliberately kept on
    # this port rather than on a repository of its own. The events are meaningless
    # without the case they describe, they are written in the same transaction as
    # the change that produced them, and a caller that can load a case can always
    # answer "what happened to it". A second port would only add a second thing to
    # wire up and a second chance for the two to drift.

    def append_audit(self, event: AuditEvent) -> None:
        """Append one event. Never updates, never deletes.

        Implementations may assume the caller has already decided the event is
        worth recording; this method does not deduplicate. An event has a fresh id
        from :meth:`AuditEvent.record`, so a retried ``save`` that appends the same
        event twice is the caller's bug, not something to paper over here.
        """
        ...

    def list_audit(
        self, dispute_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[AuditEvent, ...]:
        """Return a case's events oldest-first.

        Ordered by ``(occurred_at, entity_version, id)``. Oldest-first, unlike
        ``list``: an audit trail read in reverse reads as a stack of unrelated
        events, while in order it tells a story.

        The ``entity_version`` tiebreak matters because the clock is coarse enough
        that several operations can share an ``occurred_at`` -- on Windows the
        resolution is milliseconds -- and a random ``id`` would then order the story
        arbitrarily. The case version rises by one with every recorded operation, so
        it recovers the order the events actually happened in. ``id`` is the final
        tiebreak, for events that share both, so the read is fully deterministic.
        """
        ...

    def count_audit(self, dispute_id: UUID) -> int:
        """Total events recorded against a case, for pagination."""
        ...