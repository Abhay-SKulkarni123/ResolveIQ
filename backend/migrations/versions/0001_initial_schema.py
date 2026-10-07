"""initial schema: accounts, contracts, contract_price_terms

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-03

Creates the three source-system tables described in docs/SYSTEM_DESIGN.md §3.2.
The dispute-side tables (invoices, line items, usage events, investigations)
are deliberately absent: they arrive with the features that need them.

Constraint and index names
-------------------------
``CheckConstraint`` names below are given in their *bare* form, for example
``external_id_not_blank``, because ``NAMING_CONVENTION`` in
``app/adapters/persistence/base.py`` expands them to
``ck_accounts_external_id_not_blank`` when the table is created. Writing the
already-prefixed name here would produce
``ck_accounts_ck_accounts_external_id_not_blank``, and the longest ones would be
truncated by PostgreSQL's 63-character identifier limit into an unreadable name
with a hash suffix. Bare names keep one derivation of the final name and keep the
migration and the models in step.

Primary key, unique, foreign key and index names are written out in full: the
convention cannot produce them from a single column reference in every case, and
they are short enough to read.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.adapters.persistence.ddl import (
    is_iso4217_currency,
)

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(external_id) <> ''", name="external_id_not_blank"),
        sa.CheckConstraint("TRIM(name) <> ''", name="name_not_blank"),
        sa.CheckConstraint(is_iso4217_currency("currency"), name="currency_is_iso4217"),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("external_id", name="uq_accounts_external_id"),
    )

    op.create_table(
        "contracts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'DRAFT'"), nullable=False
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(external_id) <> ''", name="external_id_not_blank"),
        sa.CheckConstraint("TRIM(name) <> ''", name="name_not_blank"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_period_is_ordered",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'SUPERSEDED', 'TERMINATED')",
            name="status_is_known",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_contracts_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_contracts"),
        sa.UniqueConstraint("external_id", name="uq_contracts_external_id"),
    )
    op.create_index("ix_contracts_account_id", "contracts", ["account_id"], unique=False)

    op.create_table(
        "contract_price_terms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("metric_key", sa.String(length=64), nullable=False),
        sa.Column("billing_mode", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=19, scale=4), nullable=True),
        # Written at the column's own scale rather than as bare "0". Both servers
        # normalise a numeric default to the column's scale when they store it --
        # MySQL turns "0" into "0.0000" -- and `alembic check` compares the
        # spelling in the model against the spelling the server reports, so the
        # two have to be equal rather than merely numerically the same.
        sa.Column(
            "included_units",
            sa.Numeric(precision=19, scale=4),
            server_default=sa.text("0.0000"),
            nullable=False,
        ),
        sa.Column("overage_price", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("minimum_commitment", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("tier_schedule", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("TRIM(metric_key) <> ''", name="metric_key_not_blank"),
        sa.CheckConstraint(is_iso4217_currency("currency"), name="currency_is_iso4217"),
        sa.CheckConstraint(
            "billing_mode IN ('PER_UNIT', 'TIERED', 'COMMITMENT')",
            name="billing_mode_is_known",
        ),
        sa.CheckConstraint("included_units >= 0", name="included_units_not_negative"),
        sa.CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="unit_price_not_negative",
        ),
        sa.CheckConstraint(
            "overage_price IS NULL OR overage_price >= 0",
            name="overage_price_not_negative",
        ),
        sa.CheckConstraint(
            "minimum_commitment IS NULL OR minimum_commitment >= 0",
            name="minimum_commitment_not_negative",
        ),
        sa.CheckConstraint(
            "(billing_mode = 'TIERED') = (tier_schedule IS NOT NULL)",
            name="tier_schedule_matches_billing_mode",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.id"],
            name="fk_contract_price_terms_contract_id_contracts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_contract_price_terms"),
        # "One row per metered metric". This constraint's index also serves every
        # lookup by contract_id, because contract_id is its leading column, so no
        # separate index on contract_id is created.
        sa.UniqueConstraint(
            "contract_id", "metric_key", name="uq_contract_price_terms_contract_id_metric_key"
        ),
    )


def downgrade() -> None:
    # Reverse creation order so that foreign keys never dangle.
    #
    # The indexes are dropped implicitly by DROP TABLE rather than by an explicit
    # op.drop_index(). MySQL refuses to drop an index that a foreign key on the
    # same table depends on (error 1553), and ix_contracts_account_id is exactly
    # that index for fk_contracts_account_id_accounts. PostgreSQL permits the drop,
    # so writing it explicitly made the downgrade work on one engine and fail on
    # the other. DROP TABLE removes the index and the constraint together on both.
    op.drop_table("contract_price_terms")
    op.drop_table("contracts")
    op.drop_table("accounts")
