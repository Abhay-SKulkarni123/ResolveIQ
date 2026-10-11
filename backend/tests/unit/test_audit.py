"""The audit trail: the value object, its storage model, and the migration's agreement.

The trail exists to answer one question -- what happened to this case, and who did it --
and both halves of that are asserted here without a database. The domain tests pin the
invariants that make a stored event trustworthy (a timestamp that means an instant, an
actor that is never half-recorded, a payload that cannot be edited after the fact). The
model and migration tests pin the schema that stores it, including the one thing the two
must never disagree about: the set of event types the CHECK constraint accepts.

Whether an engine *accepts* the schema is ``tests/integration/test_audit_persistence.py``
and ``test_migrations.py``'s job. These run on a laptop with nothing installed, which is
exactly why the promises live here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Uuid

from app.adapters.persistence import Base
from app.adapters.persistence.audit_models import _AUDIT_EVENT_TYPES, AuditEventRow
from app.domain.audit import AuditEvent, AuditEventType

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0005_audit_events.py"
)

NOW = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)


def an_event(**overrides: object) -> AuditEvent:
    kwargs: dict[str, object] = {
        "event_type": AuditEventType.CASE_CREATED,
        "dispute_id": uuid4(),
        "occurred_at": NOW,
    }
    kwargs.update(overrides)
    return AuditEvent.record(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The event type vocabulary
# ---------------------------------------------------------------------------


def test_the_event_types_are_a_closed_known_set() -> None:
    """Every member is a lifecycle operation, and nothing names a field-level edit.

    A trail of field diffs is a database changelog; these seven are the coarse facts a
    reviewer reads in order. A failure here is a reminder that adding a member is a
    schema change too -- the CHECK constraint below is derived from this enum.
    """
    assert {member.value for member in AuditEventType} == {
        "CASE_CREATED",
        "EVIDENCE_ATTACHED",
        "INVESTIGATION_COMPLETED",
        "INVESTIGATION_FAILED",
        "REVIEW_RECORDED",
        "DECISION_RECORDED",
        "CASE_REOPENED",
    }


def test_the_orm_check_constraint_lists_every_event_type() -> None:
    """The constraint is generated from the enum, so it cannot fall behind it.

    A value the enum can produce but the CHECK rejects would make the service raise an
    ``IntegrityError`` the moment it recorded a legitimate event -- a failure that only
    appears against a real database, which is the worst place to find it.
    """
    sql = str(
        next(
            c.sqltext
            for c in AuditEventRow.__table__.constraints
            if isinstance(c, CheckConstraint) and "event_type_is_known" in (c.name or "")
        )
    )
    for member in AuditEventType:
        assert f"'{member.value}'" in sql, member
    assert sql == f"event_type IN ({_AUDIT_EVENT_TYPES})"


def test_the_migration_freezes_the_same_event_types() -> None:
    """The migration's literal must match the enum the ORM checks against.

    The migration keeps a frozen copy on purpose -- a historical revision should mean
    the same thing forever -- so the copy is asserted against the live enum here, and
    the frozen literal cannot silently drift from the model.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"_EVENT_TYPES = \((.*?)\)", source, re.S)
    assert match is not None, "the migration no longer declares _EVENT_TYPES"
    # The literals are split across adjacent strings; literal_eval concatenates them.
    frozen = ast.literal_eval(f"({match.group(1)})")
    assert frozen == ",".join(f"'{member.value}'" for member in AuditEventType)


# ---------------------------------------------------------------------------
# Building an event
# ---------------------------------------------------------------------------


def test_record_generates_a_fresh_identity_each_time() -> None:
    """Two records of the same fact are two rows, with different ids.

    The id is generated at record time rather than by the database because the event is
    immutable: a caller that builds the same event twice must not have them collapse
    into one row, and a retried save must not mint a second id for one event.
    """
    first = an_event()
    second = an_event()
    assert first.id != second.id
    assert isinstance(first.id, UUID)


def test_record_defaults_to_an_empty_frozen_payload() -> None:
    """No payload is a real state, not a missing one."""
    event = an_event()
    assert dict(event.payload) == {}
    assert isinstance(event.payload, MappingProxyType)


def test_a_naive_timestamp_is_rejected() -> None:
    """An instant without a zone is not an instant.

    Storing a naive datetime would make the ordering depend on the reading process's
    local zone, and the trail's whole value is that it is one ordered sequence.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        an_event(occurred_at=datetime(2026, 3, 31, 12, 0))


def test_an_actor_without_a_role_is_allowed() -> None:
    """Several service call sites have an id and no role, and the id is worth keeping."""
    event = an_event(actor_id="rev-1")
    assert event.actor_id == "rev-1"
    assert event.actor_role is None


def test_a_role_without_an_actor_is_rejected() -> None:
    """A role with no actor identifies nobody in particular."""
    with pytest.raises(ValueError, match="requires an actor_id"):
        an_event(actor_role="reviewer")


@pytest.mark.parametrize("field", ["actor_id", "actor_role"])
def test_a_blank_actor_field_is_rejected(field: str) -> None:
    """A blank value is a record of nothing that looks like a record."""
    kwargs: dict[str, object] = {"actor_id": "rev-1", "actor_role": "reviewer"}
    kwargs[field] = "   "
    with pytest.raises(ValueError, match="must not be blank"):
        an_event(**kwargs)


def test_a_zero_entity_version_is_rejected() -> None:
    """Versions start at 1; a zero would compare equal to "no version recorded"."""
    with pytest.raises(ValueError, match="entity_version"):
        an_event(entity_version=0)


def test_an_absent_entity_version_is_allowed() -> None:
    assert an_event(entity_version=None).entity_version is None


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_correlation_id_becomes_none(value: str) -> None:
    """A stored blank would look like a correlation id nothing can ever match."""
    assert an_event(correlation_id=value).correlation_id is None


def test_a_real_correlation_id_is_kept_trimmed() -> None:
    assert an_event(correlation_id="  key-1  ").correlation_id == "key-1"


def test_the_payload_is_frozen_depth_first() -> None:
    """A proxy over still-mutable values is immutability in appearance only."""
    event = an_event(payload={"nested": {"items": [1, 2]}})
    assert isinstance(event.payload, MappingProxyType)
    assert isinstance(event.payload["nested"], MappingProxyType)
    assert isinstance(event.payload["nested"]["items"], tuple)
    with pytest.raises(TypeError):
        event.payload["nested"]["items"] = ()  # type: ignore[index]


def test_an_event_cannot_be_modified_after_creation() -> None:
    """The record is the evidence; an editable one is evidence of nothing."""
    event = an_event()
    with pytest.raises(FrozenInstanceError):
        event.actor_id = "someone-else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The storage model
# ---------------------------------------------------------------------------


def test_the_audit_table_is_named_and_keyed_by_uuid() -> None:
    """A char-typed id would sort and compare differently from every other table's."""
    table = Base.metadata.tables["audit_events"]
    assert AuditEventRow.__table__ is table
    assert isinstance(table.columns["id"].type, Uuid)
    assert table.columns["id"].primary_key


@pytest.mark.parametrize(
    ("column", "length"),
    [("event_type", 48), ("actor_id", 128), ("actor_role", 64), ("correlation_id", 64)],
)
def test_the_bounded_text_columns_are_sized(column: str, length: int) -> None:
    assert isinstance(Base.metadata.tables["audit_events"].columns[column].type, String)
    assert Base.metadata.tables["audit_events"].columns[column].type.length == length


def test_the_required_columns_cannot_be_null() -> None:
    """An event without a dispute, a kind or a timestamp is not an event."""
    columns = Base.metadata.tables["audit_events"].columns
    for name in ("dispute_id", "event_type", "payload", "occurred_at"):
        assert columns[name].nullable is False, name


def test_the_optional_context_columns_may_be_null() -> None:
    """Not every event has a run, an actor, a version or a correlation id."""
    columns = Base.metadata.tables["audit_events"].columns
    for name in (
        "investigation_id",
        "actor_id",
        "actor_role",
        "entity_version",
        "correlation_id",
    ):
        assert columns[name].nullable is True, name


def test_occurred_at_carries_a_time_zone() -> None:
    column = Base.metadata.tables["audit_events"].columns["occurred_at"]
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True


def test_entity_version_is_an_integer() -> None:
    assert isinstance(
        Base.metadata.tables["audit_events"].columns["entity_version"].type, Integer
    )


def test_the_dispute_and_investigation_pointers_restrict_deletion() -> None:
    """Nothing may delete the run an event cites out from under the trail."""
    columns = Base.metadata.tables["audit_events"].columns
    dispute_fk = next(iter(columns["dispute_id"].foreign_keys))
    investigation_fk = next(iter(columns["investigation_id"].foreign_keys))
    assert dispute_fk.target_fullname == "disputes.id"
    assert dispute_fk.ondelete == "RESTRICT"
    assert investigation_fk.target_fullname == "investigations.id"
    assert investigation_fk.ondelete == "RESTRICT"


def test_the_payload_column_is_native_json() -> None:
    """Read as a whole document, so the engine's JSON type is the right one.

    JSONB on PostgreSQL, JSON on MySQL, via the same variant the evidence columns use.
    """
    from sqlalchemy.dialects import mysql, postgresql

    column = Base.metadata.tables["audit_events"].columns["payload"]
    assert "JSONB" in str(column.type.compile(dialect=postgresql.dialect()))
    assert "JSON" in str(column.type.compile(dialect=mysql.dialect()))


def test_the_named_check_constraints_exist() -> None:
    named = {
        c.name
        for c in AuditEventRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    # The naming convention prefixes each with ``ck_audit_events_``.
    assert named == {
        "ck_audit_events_event_type_is_known",
        "ck_audit_events_actor_id_not_blank",
        "ck_audit_events_actor_role_not_blank",
        "ck_audit_events_entity_version_is_positive",
    }


def test_the_ordering_index_is_on_dispute_and_time() -> None:
    """The read is "this case's events in order", so the index matches it exactly."""
    indexes = {
        tuple(column.name for column in index.columns)
        for index in AuditEventRow.__table__.indexes
        if isinstance(index, Index)
    }
    assert ("dispute_id", "occurred_at") in indexes
    assert any(
        index.name == "ix_audit_events_dispute_id_occurred_at"
        for index in AuditEventRow.__table__.indexes
    )


def test_the_migration_creates_the_same_table() -> None:
    """The one line the model and the migration must agree on absolutely."""
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'op.create_table(\n        "audit_events"' in source
    assert 'op.create_index(\n        "ix_audit_events_dispute_id_occurred_at"' in source
    assert 'op.drop_table("audit_events")' in source
