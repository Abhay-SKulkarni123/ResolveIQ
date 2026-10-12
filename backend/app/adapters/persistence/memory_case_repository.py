"""In-process implementation of :class:`~app.ports.cases.CaseRepository`.

**This is not durability.** It exists for two reasons and they are the only two:

1. ``backend/app/api`` and ``frontend/`` need something to talk to when no
   PostgreSQL is reachable, so the reviewer workflow can be exercised end to end.
2. The case rules -- fingerprint/staleness, reopen, append-only history, optimistic
   concurrency -- are business rules, and they are worth testing against a real
   implementation of the protocol rather than against a mock that agrees with
   whatever the test expects.

It obeys every rule in the port. Where it is allowed to differ from the SQL
adapter, it says so:

* ``save`` copies the aggregate before storing it. The SQL adapter's rows are
  already a snapshot; here the object the caller holds would otherwise be mutable
  state shared with the "database", and mutating it in a test would look like a
  successful write.
* ``list`` returns full aggregates. The SQL adapter may thin them out; this one
  cannot meaningfully do so without inventing a second read path.

What it must never be: presented as evidence that PostgreSQL works. The capability
endpoint reports the store in use (:mod:`app.api.deps`), and no test may assert
persistence through this class.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from app.domain.audit import AuditEvent
from app.domain.cases import CaseStatus, DisputeCase
from app.ports.cases import CaseConflictError, CaseNotFoundError

__all__ = ["InMemoryCaseRepository"]


class InMemoryCaseRepository:
    """A dictionary per case, with the same contract as the SQL adapter."""

    def __init__(self) -> None:
        self._cases: dict[UUID, DisputeCase] = {}
        self._by_external_id: dict[str, UUID] = {}
        # One append-only list per dispute. The events are immutable, so the list
        # holds the values themselves rather than copies.
        self._audit: dict[UUID, list[AuditEvent]] = {}
        # Insertion order, for tie-breaking `list`. See the comment there.
        self._sequence: dict[UUID, int] = {}
        self._next_sequence = 0

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def exists(self, dispute_id: UUID) -> bool:
        return dispute_id in self._cases

    def get(self, dispute_id: UUID) -> DisputeCase:
        try:
            return self._cases[dispute_id]
        except KeyError:
            raise CaseNotFoundError(f"no dispute with id {dispute_id}") from None

    def get_by_external_id(self, external_id: str) -> DisputeCase:
        case = self.find_by_external_id(external_id)
        if case is None:
            raise CaseNotFoundError(f"no dispute with external_id {external_id!r}")
        return case

    def find_by_external_id(self, external_id: str) -> DisputeCase | None:
        dispute_id = self._by_external_id.get(external_id)
        return None if dispute_id is None else self._cases[dispute_id]

    def find_by_idempotency_key(self, key: str) -> DisputeCase | None:
        """The case opened with ``key``, if any.

        Only the case-level key is indexed here. Keys on runs, reviews and decisions
        are searched inside a known aggregate by the service, which already holds the
        dispute id, so there is nothing to index for them.
        """
        for candidate in self._cases.values():
            if candidate.idempotency_key == key:
                return candidate
        return None

    def list(
        self, *, status: CaseStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[DisputeCase, ...]:
        matching = [
            case
            for case in self._cases.values()
            if status is None or case.status is status
        ]
        # Newest first, tie-broken by insertion order rather than by id.
        #
        # The tiebreak is not cosmetic. `datetime.now()` is coarse on Windows -- two
        # cases opened in the same clock tick get identical timestamps -- and ids are
        # random UUIDs, so an id tiebreak would order same-tick cases arbitrarily.
        # That is not only a flaky test: it makes a reviewer's queue order genuinely
        # nondeterministic and lets pagination show one row twice while skipping
        # another. Insertion order is stable across reads, so equal timestamps fall
        # back to "the one that arrived later", which is what "newest first" means.
        matching.sort(key=lambda c: (c.created_at, self._sequence[c.id]), reverse=True)
        return tuple(matching[offset : offset + limit])

    def count(self, *, status: CaseStatus | None = None) -> int:
        return sum(1 for case in self._cases.values() if status is None or case.status is status)

    def list_audit(
        self, dispute_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[AuditEvent, ...]:
        """Oldest-first, ordered by ``(occurred_at, entity_version, id)``.

        The SQL adapter sorts on the same three keys. ``entity_version`` recovers the
        order of operations that share one clock tick (the service always sets it to
        the case version, which rises by one per change); ``id`` is the final tiebreak
        and is reproduced by ``str(uuid)``, which orders the same way as the raw 16
        bytes both engines store.
        """
        events = sorted(
            self._audit.get(dispute_id, []),
            key=lambda event: (
                event.occurred_at,
                event.entity_version or 0,
                str(event.id),
            ),
        )
        return tuple(events[offset : offset + limit])

    def count_audit(self, dispute_id: UUID) -> int:
        return len(self._audit.get(dispute_id, []))

    def __iter__(self) -> Iterator[DisputeCase]:
        """Iterate stored cases. Test convenience; not part of the port."""
        return iter(self._cases.values())

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def save(self, case: DisputeCase, *, expected_version: int) -> None:
        """Store the aggregate, enforcing the same optimistic concurrency.

        The stored copy is replaced wholesale rather than merged. The aggregate is
        immutable and carries its own history, so a replacement cannot lose an
        investigation or a review that the caller still has -- and rejecting the
        save on a version mismatch is what stops it losing one that it does not.
        """
        stored = self._cases.get(case.id)
        if expected_version == 0:
            if stored is not None:
                raise CaseConflictError(f"dispute {case.id} already exists; expected a create")
            existing_external = self._by_external_id.get(case.external_id)
            if existing_external is not None:
                raise CaseConflictError(
                    f"dispute {case.external_id!r} already exists "
                    f"(id {existing_external}); external_id is unique"
                )
        else:
            if stored is None:
                raise CaseNotFoundError(f"no dispute with id {case.id}")
            if stored.version != expected_version:
                raise CaseConflictError(
                    f"dispute {case.id} changed since it was read "
                    f"(expected version {expected_version}, stored is {stored.version})"
                )

        previous_external = stored.external_id if stored is not None else None
        if case.id not in self._sequence:
            self._sequence[case.id] = self._next_sequence
            self._next_sequence += 1
        self._cases[case.id] = replace(case)
        self._by_external_id[case.external_id] = case.id
        if previous_external is not None and previous_external != case.external_id:
            del self._by_external_id[previous_external]

    def begin_investigation(self, case: DisputeCase, *, expected_version: int) -> None:
        """Write the pending marker.

        In memory there is no transaction to commit, so this is :meth:`save`. The
        durability the SQL adapter buys with an immediate commit has no analogue here,
        and nothing may rely on it: the in-memory store is not durability and never was.
        """
        self.save(case, expected_version=expected_version)

    def commit(self) -> None:
        """No-op: the in-memory store has no transaction to settle.

        Present so the service can commit a failed run's tombstone on its own without
        knowing which adapter it holds. Nothing may read this as durability.
        """
        return None

    def list_pending_investigations(
        self, *, started_before: datetime
    ) -> tuple[tuple[UUID, UUID], ...]:
        """Pending runs started before ``started_before``, oldest-first."""
        pending: list[tuple[UUID, UUID, datetime]] = []
        for case in self._cases.values():
            for run in case.investigations:
                if run.is_pending and run.created_at < started_before:
                    pending.append((case.id, run.id, run.created_at))
        pending.sort(key=lambda row: (row[2], str(row[1])))
        return tuple((dispute_id, investigation_id) for dispute_id, investigation_id, _ in pending)

    def append_audit(self, event: AuditEvent) -> None:
        """Append one event. No update path exists, which is the point."""
        self._audit.setdefault(event.dispute_id, []).append(event)