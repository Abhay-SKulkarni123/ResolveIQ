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

from typing import Protocol, runtime_checkable
from uuid import UUID

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