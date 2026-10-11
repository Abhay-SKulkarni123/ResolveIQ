"""idempotency keys: replay a write instead of repeating it

Revision ID: 0004_idempotency_keys
Revises: 0003_dispute_decisions
Create Date: 2026-10-09

Adds an optional ``idempotency_key`` to the four tables a client writes through the
case API -- ``disputes`` (open), ``investigations`` (run), ``finding_reviews``
(annotate) and ``dispute_decisions`` (decide) -- each unique when present.

What this is for
----------------
A network retry, a double-clicked button or an at-least-once queue delivers the same
request twice. Without a key the second delivery is indistinguishable from a new
one: it opens a second case, runs a second investigation, records a second identical
review, or decides a case twice. The key makes the retry *addressable*. The service
looks the key up first and returns the row the first delivery produced; the unique
constraint is the backstop for the race where both deliveries pass the lookup before
either commits, and the loser reads the winner's row instead of inserting a twin.

This is layer 2 of §9.3's idempotency story, applied to the records that already
exist. Layer 1 (a stored response per key) and the money-movement tables
(``adjustments``, ``review_decisions``) are the resolution phase and are deliberately
not here: this migration adds a column to tables that exist, not a new concept.

Why the column is nullable and the constraint is per-table, not per-dispute
-----------------------------------------------------------------------------
A caller may omit the header, so the column is nullable and old rows read as "no
key". ``NULL`` never compares equal under a SQL unique constraint, so any number of
keyless rows coexist; only real keys collide. The constraint is on the bare key
rather than on ``(dispute_id, key)`` because a key is a client-supplied opaque token
that must not repeat anywhere -- scoping it to a dispute would let the same token
mean two different requests in two disputes.

Portability
-----------
``ADD COLUMN`` and ``ADD CONSTRAINT ... UNIQUE`` are supported by both PostgreSQL
and MySQL, and neither new constraint is an index a foreign key depends on, so the
downgrade can drop them explicitly without tripping MySQL's error 1553.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_idempotency_keys"
down_revision: str | None = "0003_dispute_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(table, unique-constraint-name)``. The column is called ``idempotency_key``
#: everywhere so a repository query is the same shape for each table. Listed as data
#: so the up/down pair cannot drift from each other; the operations below are spelled
#: out one table at a time because Alembic migrations are read and diffed as DDL, and a
#: loop hides which tables the revision touches.
_IDEMPOTENT_TABLES: tuple[tuple[str, str], ...] = (
    ("disputes", "uq_disputes_idempotency_key"),
    ("investigations", "uq_investigations_idempotency_key"),
    ("finding_reviews", "uq_finding_reviews_idempotency_key"),
    ("dispute_decisions", "uq_dispute_decisions_idempotency_key"),
)


def upgrade() -> None:
    op.add_column(
        "disputes", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.create_unique_constraint("uq_disputes_idempotency_key", "disputes", ["idempotency_key"])

    op.add_column(
        "investigations", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.create_unique_constraint(
        "uq_investigations_idempotency_key", "investigations", ["idempotency_key"]
    )

    op.add_column(
        "finding_reviews", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.create_unique_constraint(
        "uq_finding_reviews_idempotency_key", "finding_reviews", ["idempotency_key"]
    )

    op.add_column(
        "dispute_decisions", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.create_unique_constraint(
        "uq_dispute_decisions_idempotency_key", "dispute_decisions", ["idempotency_key"]
    )


def downgrade() -> None:
    # Drop each constraint before the column it is built on. The reverse order works
    # on PostgreSQL (dropping the column removes the constraint with it) but MySQL
    # refuses to drop a column that a constraint still references, so the explicit
    # pair is the one that runs on both engines. Tables are unwound in reverse.
    op.drop_constraint(
        "uq_dispute_decisions_idempotency_key", "dispute_decisions", type_="unique"
    )
    op.drop_column("dispute_decisions", "idempotency_key")

    op.drop_constraint(
        "uq_finding_reviews_idempotency_key", "finding_reviews", type_="unique"
    )
    op.drop_column("finding_reviews", "idempotency_key")

    op.drop_constraint(
        "uq_investigations_idempotency_key", "investigations", type_="unique"
    )
    op.drop_column("investigations", "idempotency_key")

    op.drop_constraint("uq_disputes_idempotency_key", "disputes", type_="unique")
    op.drop_column("disputes", "idempotency_key")
