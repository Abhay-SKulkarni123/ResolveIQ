"""The ORM model for a case's audit trail.

One table, ``audit_events``, holding the stored form of
:class:`app.domain.audit.AuditEvent`. It lives in its own module rather than being
folded into :mod:`app.adapters.persistence.case_models` for two reasons:

* the case models file is the subject of a strict, count-based agreement test
  between its CHECK constraints and the Phase 4 migrations. Adding this table's
  constraint there would silently break that test's arithmetic, and, worse, would
  blur the boundary the test draws around the Phase 4 schema;
* an audit trail is a cross-cutting concern, not one of the case aggregate's own
  tables. Keeping it separate keeps that visible.

The table is append-only. There is no ``updated_at`` and no code path updates a
row: an audit entry is the record that something happened, and an editable one is
the record of nothing. ``occurred_at`` is the event's own timestamp -- the moment
the change happened -- rather than a ``created_at`` default, because those two
differ whenever an event is written after the fact, and the interesting time is when
the thing happened, not when the row was inserted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base, UuidPrimaryKeyMixin
from app.adapters.persistence.case_models import JSONB
from app.adapters.persistence.ddl import not_blank, nullable
from app.domain.audit import AuditEventType

__all__ = ["AuditEventRow"]

#: The known event types as SQL string literals, derived from the domain enum so the
#: CHECK constraint cannot silently fall behind it. Written as a constant rather than
#: an f-string inline because the migration builds the exact same literal, and the
#: two are compared as text by ``test_case_persistence_models``.
_AUDIT_EVENT_TYPES: Final[str] = ",".join(f"'{member.value}'" for member in AuditEventType)


class AuditEventRow(UuidPrimaryKeyMixin, Base):
    """One immutable entry in a case's audit trail.

    ``dispute_id`` and ``investigation_id`` both ``RESTRICT`` on delete. A case's
    history is part of the case; nothing may delete the run an event points at while
    the event still cites it, or "what happened, and against which run?" stops being
    answerable.

    ``payload`` is stored in the engine's native JSON type -- JSONB on PostgreSQL,
    JSON on MySQL -- for the same reason the evidence snapshots are: it is read as a
    whole document and never queried per field, so a relational form would add
    structure no query uses.
    """

    __tablename__ = "audit_events"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=True
    )
    actor_id: Mapped[str | None] = mapped_column(String(128))
    actor_role: Mapped[str | None] = mapped_column(String(64))
    entity_version: Mapped[int | None] = mapped_column(Integer)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(f"event_type IN ({_AUDIT_EVENT_TYPES})", name="event_type_is_known"),
        CheckConstraint(
            nullable("actor_id", not_blank("actor_id")), name="actor_id_not_blank"
        ),
        CheckConstraint(
            nullable("actor_role", not_blank("actor_role")), name="actor_role_not_blank"
        ),
        CheckConstraint(
            "entity_version IS NULL OR entity_version >= 1",
            name="entity_version_is_positive",
        ),
        Index("ix_audit_events_dispute_id_occurred_at", "dispute_id", "occurred_at"),
    )
