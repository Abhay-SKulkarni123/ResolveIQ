"""Declarative base, constraint naming and shared column behaviour.

Everything ORM-specific lives under :mod:`app.adapters.persistence`. No module in
``app.domain`` or ``app.pricing`` may import this package; that rule is checked
mechanically by ``tests/unit/test_layer_boundaries.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["NAMING_CONVENTION", "Base", "IngestedAtMixin", "UuidPrimaryKeyMixin"]

#: Deterministic constraint names.
#:
#: Alembic's autogenerate compares constraint *names* as well as shapes, so an
#: unnamed constraint yields a fresh migration on every run. Pinning the names in
#: the metadata makes generated migrations stable and lets the integration tests
#: assert on exact names rather than on counts.
#:
#: ``ck`` interpolates ``%(constraint_name)s``, which means every
#: ``CheckConstraint`` in this package must pass an explicit ``name=``. That is
#: the point: it is the only way to get a short, readable, stable check name
#: instead of one derived from the first hundred characters of a SQL expression.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata."""

    metadata: ClassVar[MetaData] = MetaData(naming_convention=NAMING_CONVENTION)


class UuidPrimaryKeyMixin:
    """Surrogate UUID primary key, generated in Python.

    These tables mirror records owned by a customer billing system, so the
    primary key is ours rather than theirs: the source's own identifier lives in
    an ``external_id`` column with a uniqueness constraint. A surrogate key keeps
    that separation intact and means an ingest of two accounts that share a source
    identifier still has distinct rows to reconcile rather than silently
    overwriting each other.

    The default is applied by Python, not by a database function, so the same
    value is visible to the ORM without a round trip and the schema carries no
    dependency on a particular PostgreSQL version's UUID generator.
    """

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class IngestedAtMixin:
    """Timezone-aware timestamp recording when ResolveIQ first stored the row.

    There is deliberately no ``updated_at``. Source records are treated as
    append-only: a change of commercial terms produces a new effective-dated
    contract row rather than an edit, and reproducibility of an investigation
    rests on the evidence snapshot (ADR-006), not on a mutable-row audit trail.
    Adding a second timestamp here would imply an editing workflow that does not
    exist.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
