"""The audit trail against a real database: durable, ordered, and transactional.

The unit suite proves the rules against an in-memory store and the API suite proves
the route against the same store. Neither touches a database that enforces foreign
keys and serialises JSON, so the four claims that only a real engine can settle live
here:

* an event is *durable* -- written, committed, and read back by a different session;
* it is *ordered* -- the shelf's ``ORDER BY`` returns the lifecycle in the order the
  operations happened, not in the arbitrary order a random id would;
* it shares the case's *transaction* -- the case and its event commit together and
  roll back together, so the trail cannot record a change that did not land;
* its payload survives a *JSON round trip* with nesting intact.

Each test writes through the real :class:`SqlAlchemyCaseRepository` against the
migrated schema and reads back through a second session, because a value still in the
first session's identity map proves nothing about what the database stored.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.llm.mock import MockLlmProvider
from app.adapters.persistence import get_sessionmaker
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.domain.audit import AuditEvent, AuditEventType
from app.domain.billing import (
    InvoiceLine,
    InvoicePeriod,
    LineType,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.cases import DecisionKind, ReviewActionKind, ReviewTargetType
from app.domain.contracts import BillingMode
from app.domain.json_frozen import thaw_json
from app.domain.money import Money
from app.services.cases import CaseService, OpenCaseCommand

PERIOD = InvoicePeriod(date(2026, 3, 1), date(2026, 4, 1))


def usd(text: str) -> Money:
    return Money.parse(text, "USD")


def open_command(dispute_external_id: str = "DSC-AUDIT-1") -> OpenCaseCommand:
    return OpenCaseCommand(
        dispute_external_id=dispute_external_id,
        invoice=RecordedInvoice(
            external_id=f"INV-{dispute_external_id}",
            period=PERIOD,
            currency="USD",
            lines=(InvoiceLine("api_calls", LineType.USAGE, usd("28.40")),),
            stated_total=usd("28.40"),
        ),
        contract_external_id="CTR-AUDIT-1",
        dispute_text="We were billed the wrong rate for API calls.",
        price_terms=[
            PriceTerm(
                "api_calls",
                BillingMode.TIERED,
                "USD",
                tiers=(
                    Tier(up_to=Decimal("10000"), unit_price=usd("0.50")),
                    Tier(up_to=Decimal("50000"), unit_price=usd("0.0007")),
                ),
            )
        ],
        usage_events=[
            UsageEvent(
                external_id="USE-AUDIT-1",
                dedupe_key="DK-USE-AUDIT-1",
                metric_key="api_calls",
                occurred_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
                quantity=Decimal("12300"),
                unit="calls",
            )
        ],
    )


def service_on(session: Session) -> CaseService:
    return CaseService(SqlAlchemyCaseRepository(session), MockLlmProvider())


def test_a_creation_event_survives_a_fresh_session(db_session) -> None:
    """Written and committed in one session, read back by another."""
    service = service_on(db_session)
    opened = service.open_case(open_command(), actor_id="rev-1", actor_role="reviewer")
    db_session.commit()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        events = SqlAlchemyCaseRepository(reader).list_audit(opened.id)

    assert [event.event_type for event in events] == [AuditEventType.CASE_CREATED]
    assert events[0].payload["external_id"] == opened.external_id
    assert events[0].actor_id == "rev-1"
    assert events[0].occurred_at.tzinfo is not None


def test_the_lifecycle_reads_back_in_order(db_session) -> None:
    """The whole point, against the database's own ``ORDER BY``.

    Every operation runs inside the same clock tick, so the order can only come from
    the ``entity_version`` tiebreak; a random-id sort would scramble it. This is the
    integration twin of the unit ordering test, and the one that proves the SQL
    ``ORDER BY`` actually asks for the tiebreak.
    """
    service = service_on(db_session)
    opened = service.open_case(open_command(), actor_id="rev-1", actor_role="reviewer")
    run = service.investigate(opened.id, prompt_version="prompt-v1")
    service.record_review(
        opened.id,
        investigation_id=run.id,
        target_type=ReviewTargetType.FINDING,
        target_id=run.findings[0].id,
        action=ReviewActionKind.ACCEPT,
        actor_id="rev-1",
        actor_role="reviewer",
        rationale="matches the dashboard export",
    )
    service.decide(
        opened.id,
        decision=DecisionKind.RESOLVED,
        actor_id="rev-1",
        actor_role="reviewer",
        rationale="the recalculated total matches what was chargeable",
    )
    db_session.commit()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        events = SqlAlchemyCaseRepository(reader).list_audit(opened.id)

    assert [event.event_type for event in events] == [
        AuditEventType.CASE_CREATED,
        AuditEventType.INVESTIGATION_COMPLETED,
        AuditEventType.REVIEW_RECORDED,
        AuditEventType.DECISION_RECORDED,
    ]
    assert [event.entity_version for event in events] == [1, 2, 3, 4]


def test_the_trail_cites_a_real_run_through_the_foreign_key(db_session) -> None:
    """The investigation pointer is a real foreign key, not just an id-shaped value."""
    service = service_on(db_session)
    opened = service.open_case(open_command())
    run = service.investigate(opened.id, prompt_version="prompt-v1")
    db_session.commit()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        events = SqlAlchemyCaseRepository(reader).list_audit(opened.id)

    completed = next(
        event for event in events if event.event_type is AuditEventType.INVESTIGATION_COMPLETED
    )
    assert completed.investigation_id == run.id


def test_the_audit_and_the_case_share_one_transaction(db_session) -> None:
    """If the transaction rolls back, neither the case nor its event survives.

    This is the atomicity the whole design rests on: an event appended outside the
    case's transaction could outlive a change that never committed, and the trail
    would then describe a past that did not happen.
    """
    repository = SqlAlchemyCaseRepository(db_session)
    opened = service_on(db_session).open_case(open_command())
    # The session does not autoflush, so flush to put both the case and its event
    # into the open transaction -- neither has been committed yet.
    db_session.flush()

    assert repository.count_audit(opened.id) == 1
    assert repository.exists(opened.id)

    db_session.rollback()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        fresh = SqlAlchemyCaseRepository(reader)
        assert fresh.count_audit(opened.id) == 0
        assert not fresh.exists(opened.id)


def test_a_replayed_open_writes_one_event(db_session) -> None:
    """The idempotency guard stops the second request before it records anything."""
    service = service_on(db_session)
    first = service.open_case(open_command(), idempotency_key="audit-idem")
    service.open_case(open_command(), idempotency_key="audit-idem")
    db_session.commit()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        assert SqlAlchemyCaseRepository(reader).count_audit(first.id) == 1


def test_a_nested_payload_survives_the_json_round_trip(db_session) -> None:
    """The payload is stored as JSON, so nesting has to come back as it went in."""
    service = service_on(db_session)
    opened = service.open_case(open_command())
    repository = SqlAlchemyCaseRepository(db_session)
    event = AuditEvent.record(
        event_type=AuditEventType.CASE_REOPENED,
        dispute_id=opened.id,
        occurred_at=datetime.now(timezone.utc),
        entity_version=2,
        payload={"reason": "new evidence", "trace": {"steps": [{"key": "value"}]}},
    )
    repository.append_audit(event)
    db_session.commit()

    factory = get_sessionmaker(db_session.get_bind())
    with factory() as reader:
        events = SqlAlchemyCaseRepository(reader).list_audit(opened.id)

    stored = next(
        event for event in events if event.event_type is AuditEventType.CASE_REOPENED
    )
    # The aggregate holds its payload deep-frozen; thaw it back to compare the
    # document that was stored.
    assert thaw_json(stored.payload) == {
        "reason": "new evidence",
        "trace": {"steps": [{"key": "value"}]},
    }
