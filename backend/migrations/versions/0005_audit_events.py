"""audit events: one ordered trail per case

Revision ID: 0005_audit_events
Revises: 0004_idempotency_keys
Create Date: 2026-10-09

Adds ``audit_events``, the append-only record of what happened to a dispute and who
did it. It is part of the completion pass that gives the Phase 4 surface the one
thing it was missing: a single place a reviewer or an auditor can read the history
in order, instead of reconstructing it from ``investigations``, ``finding_reviews``
and ``dispute_decisions`` by hand.

What this table is, and is not
------------------------------
Each row is one :class:`app.domain.audit.AuditEvent`: a known ``event_type``, a
pointer to the dispute, and the context that is cheap to record now and expensive
to reconstruct later -- the run involved, the actor, the entity version at the time,
and a correlation id. It does **not** duplicate the entity it describes. The run,
review or decision it names remains the source of truth; the audit row is a signpost
to it, so the two can never disagree about the underlying fact.

The table is insert-only. There is no ``updated_at`` and no code path that updates a
row, because an editable audit entry is evidence of nothing. ``occurred_at`` is the
event's own time (the moment the change happened), not a row-insertion default,
which is why it is a plain non-null column rather than a ``server_default now()``.

``payload`` is the engine's native JSON type, matching the evidence snapshots: it is
read as a whole document and never queried per field.

Portability
-----------
Everything here is ``CREATE TABLE`` plus one index, and the downgrade is a single
``DROP TABLE``. Dropping the table removes its foreign keys and index together on
both engines, so there is no index a MySQL foreign key still depends on (error 1553)
to trip over: the explicit ``drop_index`` is deliberately absent for the same reason
``0003`` omits it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.adapters.persistence.ddl import not_blank, nullable

# revision identifiers, used by Alembic.
revision: str = "0005_audit_events"
down_revision: str | None = "0004_idempotency_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The event types this revision accepts, frozen as a literal. The ORM derives the
#: same list from :class:`app.domain.audit.AuditEventType`; keeping the migration's
#: copy literal keeps this revision's meaning fixed even if the enum later grows,
#: which is what a historical migration should do. A unit test asserts the two agree
#: today, so the frozen literal cannot silently fall behind the enum.
_EVENT_TYPES = (
    "'CASE_CREATED','EVIDENCE_ATTACHED','INVESTIGATION_COMPLETED','INVESTIGATION_FAILED',"
    "'REVIEW_RECORDED','DECISION_RECORDED','CASE_REOPENED'"
)

#: Native JSON with a PostgreSQL JSONB variant, exactly as the evidence columns are
#: declared, so both engines store the payload in their own JSON type.
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("actor_role", sa.String(length=64), nullable=True),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"event_type IN ({_EVENT_TYPES})", name="event_type_is_known"),
        sa.CheckConstraint(
            nullable("actor_id", not_blank("actor_id")), name="actor_id_not_blank"
        ),
        sa.CheckConstraint(
            nullable("actor_role", not_blank("actor_role")), name="actor_role_not_blank"
        ),
        sa.CheckConstraint(
            "entity_version IS NULL OR entity_version >= 1",
            name="entity_version_is_positive",
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_audit_events_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_audit_events_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_dispute_id_occurred_at",
        "audit_events",
        ["dispute_id", "occurred_at"],
    )


def downgrade() -> None:
    # ``DROP TABLE`` removes the index and both foreign keys with it. Dropping the
    # index first would trip MySQL's error 1553, because the composite index is the
    # one MySQL uses to support fk_audit_events_dispute_id_disputes.
    op.drop_table("audit_events")
