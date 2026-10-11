"""dispute decisions: a reviewer's closing verdict on a case

Revision ID: 0003_dispute_decisions
Revises: 0002_dispute_cases
Create Date: 2026-10-08

Implements the case-level verdict table of docs/SYSTEM_DESIGN.md §3.3. Part 3 of
the completion pass adds, to the Phase 4 surface, the one record that makes the
``RESOLVED``/``REJECTED`` lifecycle reachable: ``dispute_decisions``.

What this table is, and is not
-------------------------------
A ``dispute_decisions`` row records *who* closed the case, *when*, against *which
run and evidence*, and *why* (mandatory rationale). It is append-only and carries
no monetary amount. It is **not** an approval that applies or moves money -- that
is the idempotent resolution phase, which adds its own execution records and
guards. Shipping the closing verdict first is what lets a reviewer actually close
a case safely; shipping the money-movement half before its idempotency and
separation-of-duties rules exist is explicitly deferred (same reasoning as the
``review_decisions`` note in ``0002``).

Concurrency and double-resolution
---------------------------------
A decision is safe without its own uniqueness constraint because the aggregate's
optimistic ``version`` predicate is the arbiter: two reviewers deciding the same
``AWAITING_REVIEW`` case in parallel each build from the same version, and the
first ``save`` to land wins while the second gets ``CaseConflictError`` only in
the transaction that also re-reads state. ``dispute_decisions`` therefore stays
append-only history rather than a "one per dispute" row, which is also what lets a
case that is reopened and re-decided record both verdicts.

``evidence_fingerprint_seen`` uses the same device as ``finding_reviews``: it
records the exact view of the facts the verdict was based on, and the domain
refuses a decision over evidence that has since moved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.adapters.persistence.ddl import is_sha256_fingerprint

# revision identifiers, used by Alembic.
revision: str = "0003_dispute_decisions"
down_revision: str | None = "0002_dispute_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # A reviewer's closing verdict. Append-only, no money, rationale required.
    # ------------------------------------------------------------------
    op.create_table(
        "dispute_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_role", sa.String(length=64), nullable=False),
        sa.Column("evidence_fingerprint_seen", sa.String(length=71), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('RESOLVED','REJECTED')", name="decision_is_known"
        ),
        sa.CheckConstraint("TRIM(actor_id) <> ''", name="actor_id_not_blank"),
        sa.CheckConstraint("TRIM(rationale) <> ''", name="rationale_not_blank"),
        sa.CheckConstraint(
            is_sha256_fingerprint("evidence_fingerprint_seen"),
            name="fingerprint_is_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_dispute_decisions_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_dispute_decisions_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dispute_decisions"),
    )
    op.create_index(
        "ix_dispute_decisions_dispute_id_created_at",
        "dispute_decisions",
        ["dispute_id", "created_at"],
    )


def downgrade() -> None:
    # The index is removed implicitly by DROP TABLE, not by an explicit
    # op.drop_index(). MySQL refuses to drop an index that a foreign key on the
    # same table depends on (error 1553): ix_dispute_decisions_dispute_id_created_at
    # is the supporting index for fk_dispute_decisions_dispute_id_disputes. DROP
    # TABLE removes the index and both constraints together on both engines, and
    # nothing references dispute_decisions from another table, so this is also the
    # complete rollback.
    op.drop_table("dispute_decisions")