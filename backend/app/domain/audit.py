"""The audit trail: what happened to a case, in the order it happened.

An investigation already keeps its own history -- runs are append-only, reviews and
decisions are appended, and a case carries every version of itself. What none of
those records answers on its own is the question an auditor actually asks: *what
happened to this case, and who did it?* That is a single ordered sequence spanning
every kind of change, and no individual table owns it.

This module is the value object for one entry in that sequence. It is deliberately
plain: an :class:`AuditEventType`, a pointer back to the dispute, the pieces of
context that are cheap to record now and expensive to reconstruct later (the run
involved, the actor, the entity version, a correlation id), and a free-form
``payload`` for the details specific to each event kind.

Two rules, both of which the rest of the system relies on:

* **Append-only.** An event is created, stored and never edited. The record is the
  evidence that something happened; an editable one is evidence of nothing.
* **Self-describing, but not authoritative.** An event names the run it concerns,
  but it does not duplicate the run: the run remains the source of truth and the
  audit row only points at it. Two copies of the same fact agree until they do not.

The module imports nothing but the standard library and :mod:`app.domain.json_frozen`
(a sibling in the same layer), so it stays usable from every layer without dragging a
framework behind it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.domain.json_frozen import freeze_json

__all__ = ["AuditEvent", "AuditEventType"]


class AuditEventType(str, Enum):
    """The kinds of change a case can record.

    Deliberately closed and coarse. One member per lifecycle operation, not one per
    field that changed: a trail of field-level diffs is a database changelog, and a
    reviewer reading it learns nothing about *why* a case moved. The values are the
    string literals stored in the ``event_type`` column, so this enum is the source
    of truth the CHECK constraint is asserted against.
    """

    CASE_CREATED = "CASE_CREATED"
    EVIDENCE_ATTACHED = "EVIDENCE_ATTACHED"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
    #: A run that was written ahead and never finished: retries exhausted, TTL expired,
    #: or a process died mid-run. Distinct from ``INVESTIGATION_COMPLETED`` because a
    #: case that ends up with an answer and one that ends up with a tombstone are not
    #: the same story, and an auditor needs to tell them apart.
    INVESTIGATION_FAILED = "INVESTIGATION_FAILED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    DECISION_RECORDED = "DECISION_RECORDED"
    CASE_REOPENED = "CASE_REOPENED"


def _normalise_correlation_id(value: str | None) -> str | None:
    """Blank and whitespace-only correlation ids become ``None``.

    A stored ``"   "`` would be a correlation id nothing can ever match, which is
    worse than an absent one: it looks like data and behaves like a hole.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable entry in a case's audit trail.

    ``investigation_id``, ``actor_id``, ``entity_version`` and ``correlation_id`` are
    all optional because not every event has all of them: opening a case has no
    investigation yet, an automated system action may have no actor, and a request
    that carried no ``Idempotency-Key`` has no correlation id. Storing a sentinel
    instead (an empty string, a zero) would make "absent" and "present but empty" the
    same value and lose the distinction.
    """

    id: UUID
    event_type: AuditEventType
    dispute_id: UUID
    occurred_at: datetime
    investigation_id: UUID | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    entity_version: int | None = None
    correlation_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("audit occurred_at must be timezone-aware")
        # A role with no actor is nonsense -- "a reviewer did this, but nobody in
        # particular" identifies no one and would only ever be a bug. An actor whose
        # role was not supplied is, by contrast, a real and common case: several
        # operations reach the service from code that has an actor id but no role,
        # and recording the id is strictly better than recording nothing. The
        # asymmetry is deliberate.
        if self.actor_role is not None and self.actor_id is None:
            raise ValueError("actor_role requires an actor_id")
        if self.actor_id is not None and not self.actor_id.strip():
            raise ValueError("actor_id, when present, must not be blank")
        if self.actor_role is not None and not self.actor_role.strip():
            raise ValueError("actor_role, when present, must not be blank")
        if self.entity_version is not None and self.entity_version < 1:
            raise ValueError(f"entity_version must be >= 1, got {self.entity_version}")
        object.__setattr__(self, "correlation_id", _normalise_correlation_id(self.correlation_id))
        object.__setattr__(self, "payload", freeze_json(dict(self.payload)))

    @classmethod
    def record(
        cls,
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
    ) -> AuditEvent:
        """Build an event with a fresh id.

        The id is generated here rather than by the database because an audit entry
        is immutable and its identity is fixed at the moment the event is recorded:
        a caller that retries a ``save`` must not produce a second row under a new
        primary key. The caller controls everything else, so the same event built
        twice has the same content.
        """
        return cls(
            id=uuid4(),
            event_type=event_type,
            dispute_id=dispute_id,
            occurred_at=occurred_at,
            investigation_id=investigation_id,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_version=entity_version,
            correlation_id=correlation_id,
            payload=dict(payload or {}),
        )
