"""dispute cases, evidence, calculations and investigations

Revision ID: 0002_dispute_cases
Revises: 0001_initial_schema
Create Date: 2026-10-04

Implements the dispute-side entity table in docs/SYSTEM_DESIGN.md §3.3 for
everything Phase 4 needs. The source-system tables of §3.2 are already present
from ``0001_initial_schema``.

Deliberately absent: ``adjustments``, ``review_decisions`` and ``audit_events``.
Approval and money movement are a later phase, and ``review_decisions`` would need
the idempotency and separation-of-duties rules of §9.3 that phase has not designed
yet. What Phase 4 does need -- a reviewer annotating a finding, amending its
wording, rejecting it or asking for more information -- is ``finding_reviews``,
which records no approval and moves no money.

Ordering
--------
``disputes`` and ``investigations`` reference each other: a case points at its
newest run, a run points back at its case. This migration therefore creates
``disputes`` *without* the ``current_investigation_id`` foreign key, creates
``investigations``, and adds the constraint with ``ALTER TABLE``. A single
``CREATE TABLE`` cannot express it, and ``use_alter`` is the one place Alembic's
SQL rendering earns its keep.

Why the source document is one JSONB column
-------------------------------------------
``disputes.source_document`` holds the validated ingest payload -- invoice,
contract terms, usage, payments and adjustments -- as the domain types serialise
it. It exists so that a case can be *re-investigated*: Phase 3 took typed value
objects that only ever lived in memory, so without it a reopened dispute could not
be re-run at all. Seven normalised tables (invoices, line items, usage events,
payments, allocations, adjustments, and their dispute-scoped copies) would answer
per-field queries, and nothing in Phase 4 queries per field -- the queryable,
hashed projection is ``dispute_evidence_items``. Both are stored because the
source document is the input of record and the snapshots are the citable view of
it; deriving one from the other at read time means one of them can be wrong.

Check constraint names
----------------------
Bare, per ``0001_initial_schema``: ``ck`` expands ``%(constraint_name)s`` to
``ck_<table>_<constraint_name>``. The three longest explicit names in this
migration (``ck_investigation_resolution_options_...``) are written out in full
only for *foreign keys*, where the convention cannot be overridden usefully; every
``CheckConstraint`` here is under the 63-character limit once expanded, which
``tests/unit/test_case_migration.py`` asserts rather than trusting.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from app.adapters.persistence.ddl import (
    is_iso4217_currency,
    is_sha256_fingerprint,
    nullable,
)

revision: str = "0002_dispute_cases"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(19, 4)
PROBABILITY = sa.Numeric(5, 4)
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # The case. current_investigation_id is added after investigations exists.
    # ------------------------------------------------------------------
    op.create_table(
        "disputes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("invoice_external_id", sa.String(length=128), nullable=False),
        sa.Column("contract_external_id", sa.String(length=128), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'OPEN'"), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_document", JSONB, nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("current_investigation_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(external_id) <> ''", name="external_id_not_blank"),
        sa.CheckConstraint(
            "TRIM(invoice_external_id) <> ''", name="invoice_external_id_not_blank"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','INVESTIGATING','AWAITING_REVIEW','RESOLVED','REJECTED','REOPENED')",
            name="status_is_known",
        ),
        sa.CheckConstraint("severity IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="severity_is_known"),
        sa.CheckConstraint("version >= 1", name="version_is_positive"),
        sa.CheckConstraint(
            is_sha256_fingerprint("evidence_fingerprint"), name="fingerprint_is_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_disputes_account_id_accounts", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.id"],
            name="fk_disputes_contract_id_contracts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_disputes"),
        sa.UniqueConstraint("external_id", name="uq_disputes_external_id"),
    )
    op.create_index("ix_disputes_invoice_external_id", "disputes", ["invoice_external_id"])
    op.create_index(
        "ix_disputes_status_created_at", "disputes", ["status", "created_at"]
    )

    # ------------------------------------------------------------------
    # Immutable evidence snapshots. No updated_at: a snapshot that changed
    # would invalidate every content hash and fingerprint that referenced it.
    # ------------------------------------------------------------------
    op.create_table(
        "dispute_evidence_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("natural_key", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(natural_key) <> ''", name="natural_key_not_blank"),
        sa.CheckConstraint(
            is_sha256_fingerprint("content_hash"), name="content_hash_is_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_dispute_evidence_items_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dispute_evidence_items"),
        # This is the idempotency guarantee for POST /disputes/{id}/evidence.
        sa.UniqueConstraint(
            "dispute_id",
            "natural_key",
            "content_hash",
            name="uq_dispute_evidence_items_dispute_id_natural_key_content_hash",
        ),
    )
    op.create_index(
        "ix_dispute_evidence_items_dispute_id", "dispute_evidence_items", ["dispute_id"]
    )
    op.create_index(
        "ix_dispute_evidence_items_natural_key", "dispute_evidence_items", ["natural_key"]
    )

    # ------------------------------------------------------------------
    # One analysis run.
    # ------------------------------------------------------------------
    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("evidence_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("stage_status", JSONB, nullable=False),
        sa.Column("degradations", JSONB, nullable=False),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="version_is_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING','COMPLETE','DEGRADED','PARTIAL_FAILED')",
            name="status_is_known",
        ),
        sa.CheckConstraint(
            is_sha256_fingerprint("evidence_fingerprint"), name="fingerprint_is_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_investigations_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigations"),
        sa.UniqueConstraint("dispute_id", "version", name="uq_investigations_dispute_id_version"),
    )
    op.create_index("ix_investigations_dispute_id", "investigations", ["dispute_id"])
    op.create_index("ix_investigations_stale_at", "investigations", ["stale_at"])

    # Close the disputes <-> investigations cycle now that both exist.
    op.create_foreign_key(
        "fk_disputes_current_investigation_id_investigations",
        "disputes",
        "investigations",
        ["current_investigation_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ------------------------------------------------------------------
    # The deterministic engine's output, stored apart from the run.
    # ------------------------------------------------------------------
    op.create_table(
        "calculations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("recalculated_total", MONEY, nullable=False),
        sa.Column("recorded_total", MONEY, nullable=True),
        sa.Column("difference", MONEY, nullable=True),
        sa.Column("outstanding", MONEY, nullable=True),
        sa.Column("allocated_payments", MONEY, nullable=True),
        sa.Column("net_adjustments", MONEY, nullable=True),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), nullable=False),
        sa.Column("unresolved_metrics", JSONB, nullable=False),
        sa.Column("trace", JSONB, nullable=False),
        sa.Column("invoice", JSONB, nullable=False),
        sa.Column("balance", JSONB, nullable=False),
        sa.Column("usage_summaries", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(is_iso4217_currency("currency"), name="currency_is_iso4217"),
        sa.CheckConstraint(
            "NOT is_provisional OR is_complete = false", name="provisional_totals_are_incomplete"
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_calculations_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_calculations_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_calculations"),
        sa.UniqueConstraint("investigation_id", name="uq_calculations_investigation_id"),
    )
    op.create_index("ix_calculations_dispute_id", "calculations", ["dispute_id"])

    # ------------------------------------------------------------------
    # Which snapshot rows a given run read.
    # ------------------------------------------------------------------
    op.create_table(
        "investigation_evidence",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_item_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_item_id"],
            ["dispute_evidence_items.id"],
            name="fk_investigation_evidence_evidence_item_id_evidence",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_investigation_evidence_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "investigation_id", "evidence_item_id", name="pk_investigation_evidence"
        ),
    )
    op.create_index(
        "ix_investigation_evidence_evidence_item_id",
        "investigation_evidence",
        ["evidence_item_id"],
    )

    # ------------------------------------------------------------------
    # What the run produced.
    # ------------------------------------------------------------------
    op.create_table(
        "investigation_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("confidence", PROBABILITY, nullable=False),
        sa.Column("supporting_evidence", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("severity IN ('INFO','WARN','CRITICAL')", name="severity_is_known"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="confidence_in_unit_range"
        ),
        sa.CheckConstraint("TRIM(narrative) <> ''", name="narrative_not_blank"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_investigation_findings_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_findings"),
    )
    op.create_index(
        "ix_investigation_findings_investigation_id",
        "investigation_findings",
        ["investigation_id"],
    )

    op.create_table(
        "investigation_hypotheses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("hypothesis_code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("likelihood", PROBABILITY, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("supporting_evidence", JSONB, nullable=False),
        sa.Column("refuting_evidence", JSONB, nullable=False),
        sa.Column("metric_key", sa.String(length=128), nullable=True),
        # The engine wrote these three, not the model. See case_models.py.
        sa.Column("impact_amount", MONEY, nullable=True),
        sa.Column("impact_currency", sa.String(length=3), nullable=True),
        sa.Column("impact_basis", sa.Text(), nullable=True),
        sa.Column("not_assessable_reason", sa.Text(), nullable=True),
        sa.Column("impact_trace", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # These mirror ``StoredHypothesis.__post_init__`` and must stay identical to
        # the ORM definitions in ``case_models.py`` -- a constraint that disagrees
        # with the domain rejects valid rows or admits invalid ones.
        #
        # "basis *or* reason", not just "reason": ``ImpactAssessment`` in Phase 3
        # always carries a ``basis`` and only sometimes a ``not_assessable_reason``,
        # so a directional hypothesis with no computable amount is a legitimate state
        # that has to be storable.
        sa.CheckConstraint(
            "impact_amount IS NOT NULL "
            "OR COALESCE(TRIM(impact_basis), '') <> '' "
            "OR COALESCE(TRIM(not_assessable_reason), '') <> ''",
            name="missing_impact_is_explained",
        ),
        sa.CheckConstraint(
            "impact_amount IS NULL OR COALESCE(TRIM(impact_basis), '') <> ''",
            name="amount_implies_basis",
        ),
        sa.CheckConstraint(
            "impact_amount IS NULL OR not_assessable_reason IS NULL",
            name="amount_excludes_unassessable",
        ),
        sa.CheckConstraint(
            "impact_amount IS NULL OR impact_currency IS NOT NULL",
            name="amount_implies_currency",
        ),
        sa.CheckConstraint(
            nullable("impact_currency", is_iso4217_currency("impact_currency")),
            name="currency_is_iso4217",
        ),
        sa.CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="likelihood_in_unit_range",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_investigation_hypotheses_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_hypotheses"),
    )
    op.create_index(
        "ix_investigation_hypotheses_investigation_id",
        "investigation_hypotheses",
        ["investigation_id"],
    )

    op.create_table(
        "investigation_resolution_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("option_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False),
        sa.Column("hypothesis_code", sa.String(length=64), nullable=True),
        sa.Column("supporting_evidence", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(rationale) <> ''", name="rationale_not_blank"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_investigation_resolution_options_investigation_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_resolution_options"),
    )
    op.create_index(
        "ix_investigation_resolution_options_investigation_id",
        "investigation_resolution_options",
        ["investigation_id"],
    )

    # ------------------------------------------------------------------
    # Reviewer annotations. Append-only, and no APPROVE: that is Phase 5.
    # ------------------------------------------------------------------
    op.create_table(
        "finding_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_role", sa.String(length=64), nullable=False),
        sa.Column("evidence_fingerprint_seen", sa.String(length=71), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("amended_narrative", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('ACCEPT','REJECT','REQUEST_MORE_INFO','AMEND')", name="action_is_known"
        ),
        sa.CheckConstraint(
            "target_type IN ('FINDING','HYPOTHESIS','RESOLUTION_OPTION')",
            name="target_type_is_known",
        ),
        sa.CheckConstraint("TRIM(actor_id) <> ''", name="actor_id_not_blank"),
        # An amendment without a replacement, or a replacement without an amendment,
        # is a row nobody can interpret.
        sa.CheckConstraint(
            "(action = 'AMEND') = (amended_narrative IS NOT NULL)",
            name="amendment_matches_action",
        ),
        sa.CheckConstraint(
            "amended_narrative IS NULL OR TRIM(amended_narrative) <> ''",
            name="amendment_not_blank",
        ),
        sa.CheckConstraint(
            is_sha256_fingerprint("evidence_fingerprint_seen"),
            name="fingerprint_is_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["disputes.id"],
            name="fk_finding_reviews_dispute_id_disputes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_finding_reviews_investigation_id_investigations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finding_reviews"),
    )
    op.create_index(
        "ix_finding_reviews_dispute_id_created_at",
        "finding_reviews",
        ["dispute_id", "created_at"],
    )
    op.create_index("ix_finding_reviews_target", "finding_reviews", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_finding_reviews_target", table_name="finding_reviews")
    op.drop_index("ix_finding_reviews_dispute_id_created_at", table_name="finding_reviews")
    op.drop_table("finding_reviews")

    op.drop_index(
        "ix_investigation_resolution_options_investigation_id",
        table_name="investigation_resolution_options",
    )
    op.drop_table("investigation_resolution_options")

    op.drop_index(
        "ix_investigation_hypotheses_investigation_id",
        table_name="investigation_hypotheses",
    )
    op.drop_table("investigation_hypotheses")

    op.drop_index("ix_investigation_findings_investigation_id", table_name="investigation_findings")
    op.drop_table("investigation_findings")

    op.drop_index(
        "ix_investigation_evidence_evidence_item_id", table_name="investigation_evidence"
    )
    op.drop_table("investigation_evidence")

    op.drop_index("ix_calculations_dispute_id", table_name="calculations")
    op.drop_table("calculations")

    # Break the disputes <-> investigations cycle before dropping either side.
    op.drop_constraint(
        "fk_disputes_current_investigation_id_investigations",
        "disputes",
        type_="foreignkey",
    )

    op.drop_index("ix_investigations_stale_at", table_name="investigations")
    op.drop_index("ix_investigations_dispute_id", table_name="investigations")
    op.drop_table("investigations")

    op.drop_index("ix_dispute_evidence_items_natural_key", table_name="dispute_evidence_items")
    op.drop_index("ix_dispute_evidence_items_dispute_id", table_name="dispute_evidence_items")
    op.drop_table("dispute_evidence_items")

    op.drop_index("ix_disputes_status_created_at", table_name="disputes")
    op.drop_index("ix_disputes_invoice_external_id", table_name="disputes")
    op.drop_table("disputes")