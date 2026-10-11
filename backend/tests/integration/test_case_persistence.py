"""The full write path of an investigation, against a real database.

The schema tests in this directory prove the migration built the tables and
constraints; the unit suite proves the service rules against an in-memory store.
Neither of those exercises the repository's ``save`` against a database that
enforces foreign keys, and two latent defects only appeared there:

* ``save`` ran a core ``UPDATE`` on ``disputes`` that set
  ``current_investigation_id`` before the investigation row existed, which engines
  that enforce foreign keys reject (1452 on MySQL, 23503 on PostgreSQL);
* the write path thawed frozen snapshots with a shallow ``dict()``, so a nested
  object in ``trace`` survived as a ``mappingproxy`` and failed JSON serialisation.

Both fail on any engine that enforces foreign keys, so neither could show up in
the unit suite (which mocks the repository) or the schema suite (which never
writes rows). Running the real service against the migrated schema is what pins
them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from app.adapters.llm.mock import MockLlmProvider
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.adapters.persistence.database import get_sessionmaker
from app.domain.billing import (
    InvoiceLine,
    InvoicePeriod,
    LineType,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.cases import (
    CaseStatus,
    DecisionKind,
    InvestigationRecord,
    InvestigationRunStatus,
    ReviewActionKind,
    ReviewTargetType,
)
from app.domain.contracts import BillingMode
from app.domain.money import Money
from app.ports.cases import CaseConflictError
from app.services.cases import CaseService, OpenCaseCommand

PERIOD = InvoicePeriod(date(2026, 3, 1), date(2026, 4, 1))


def usd(text: str) -> Money:
    return Money.parse(text, "USD")


def test_investigation_persists_against_a_real_database(db_session) -> None:
    """open -> investigate must survive flush with foreign keys and JSON columns.

    The regression is the write itself: before the fixes, ``save`` raised either an
    integrity error on ``disputes.current_investigation_id`` or a mappingproxy
    serialisation error on ``calculations.trace``. Getting a hydrated case back with
    status ``AWAITING_REVIEW`` and a complete run is the assertion.
    """
    service = CaseService(SqlAlchemyCaseRepository(db_session), MockLlmProvider())

    opened = service.open_case(
        OpenCaseCommand(
            dispute_external_id="DSC-DB-0001",
            invoice=RecordedInvoice(
                external_id="INV-DB-003",
                period=PERIOD,
                currency="USD",
                lines=(InvoiceLine("api_calls", LineType.USAGE, usd("28.40")),),
                stated_total=usd("28.40"),
            ),
            contract_external_id="CTR-DB-1",
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
                    external_id="USE-DB-1",
                    dedupe_key="DK-USE-DB-1",
                    metric_key="api_calls",
                    occurred_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
                    quantity=Decimal("12300"),
                    unit="calls",
                )
            ],
        )
    )
    assert opened.status is CaseStatus.OPEN

    run = service.investigate(opened.id, prompt_version="prompt-v1")
    assert run.status is not None  # a real run was created and stored

    reloaded = service.get(opened.id)
    assert reloaded.status is CaseStatus.AWAITING_REVIEW
    assert reloaded.current_investigation_id == run.id
    assert len(reloaded.investigations) == 1
    investigation = reloaded.investigations[0]
    assert investigation.calculation is not None
    assert len(investigation.findings) > 0
    assert len(reloaded.evidence.items) > 0


def test_a_decision_persists_against_a_real_database(db_session) -> None:
    """The full lifecycle -- open, investigate, review, decide -- round-trips.

    ``dispute_decisions`` is a Phase 4 table with its own foreign keys to
    ``disputes`` and ``investigations`` and its own CHECK constraints. The defects
    this pins are the same class as the write path above: a decision row that
    flushes after the review it depends on, with a rationale the database also
    insists on.
    """
    service = CaseService(SqlAlchemyCaseRepository(db_session), MockLlmProvider())
    opened = service.open_case(
        OpenCaseCommand(
            dispute_external_id="DSC-DB-DECIDE",
            invoice=RecordedInvoice(
                external_id="INV-DB-DECIDE",
                period=PERIOD,
                currency="USD",
                lines=(InvoiceLine("api_calls", LineType.USAGE, usd("28.40")),),
                stated_total=usd("28.40"),
            ),
            contract_external_id="CTR-DB-1",
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
                    external_id="USE-DB-DECIDE",
                    dedupe_key="DK-USE-DB-DECIDE",
                    metric_key="api_calls",
                    occurred_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
                    quantity=Decimal("12300"),
                    unit="calls",
                )
            ],
        )
    )
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

    decided = service.decide(
        opened.id,
        decision=DecisionKind.RESOLVED,
        actor_id="rev-1",
        actor_role="reviewer",
        rationale="the recalculated total matches what was chargeable",
    )

    assert decided.status is CaseStatus.RESOLVED
    reloaded = service.get(opened.id)
    assert reloaded.status is CaseStatus.RESOLVED
    assert len(reloaded.decisions) == 1
    stored = reloaded.decisions[0]
    assert stored.decision is DecisionKind.RESOLVED
    assert stored.rationale == "the recalculated total matches what was chargeable"
    assert stored.investigation_id == run.id
    assert stored.evidence_fingerprint_seen == reloaded.evidence_fingerprint


def _open_command(dispute_external_id: str = "DSC-DB-IDEM") -> OpenCaseCommand:
    return OpenCaseCommand(
        dispute_external_id=dispute_external_id,
        invoice=RecordedInvoice(
            external_id="INV-DB-IDEM",
            period=PERIOD,
            currency="USD",
            lines=(InvoiceLine("api_calls", LineType.USAGE, usd("28.40")),),
            stated_total=usd("28.40"),
        ),
        contract_external_id="CTR-DB-1",
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
                external_id="USE-DB-IDEM",
                dedupe_key="DK-USE-DB-IDEM",
                metric_key="api_calls",
                occurred_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
                quantity=Decimal("12300"),
                unit="calls",
            )
        ],
    )


def test_an_idempotency_key_round_trips_against_a_real_database(db_session) -> None:
    """The key is a real column the repository reads back, not a request-local value.

    The replay lookup that makes a retried open safe goes through
    ``find_by_idempotency_key``; if the key were not persisted, that lookup would
    always miss and the retry would insert a second case.
    """
    repository = SqlAlchemyCaseRepository(db_session)
    service = CaseService(repository, MockLlmProvider())

    opened = service.open_case(_open_command(), idempotency_key="open-db-1")

    reloaded = service.get(opened.id)
    assert reloaded.idempotency_key == "open-db-1"
    found = repository.find_by_idempotency_key("open-db-1")
    assert found is not None
    assert found.id == opened.id
    assert repository.find_by_idempotency_key("never-used") is None


def test_a_second_case_with_the_same_key_is_refused_by_the_database(db_session) -> None:
    """The unique constraint is the backstop the service lookup cannot be.

    Two deliveries can both pass ``find_by_idempotency_key`` before either commits;
    the constraint is what makes the loser fail. The service would replay, so the
    second case is forced through the repository with only the key changed.
    """
    repository = SqlAlchemyCaseRepository(db_session)
    service = CaseService(repository, MockLlmProvider())
    service.open_case(_open_command("DSC-DB-IDEM-A"), idempotency_key="open-db-dup")

    twin = replace(
        service.open_case(_open_command("DSC-DB-IDEM-B")),
        id=uuid4(),
        external_id="DSC-DB-IDEM-B-twinned",
        idempotency_key="open-db-dup",
    )

    with pytest.raises(CaseConflictError):
        repository.save(twin, expected_version=0)


# ---------------------------------------------------------------------------
# Write-ahead investigation recovery (Part 7)
# ---------------------------------------------------------------------------


def _pending_record(case) -> InvestigationRecord:  # type: ignore[no-untyped-def]
    return InvestigationRecord(
        id=uuid4(),
        dispute_id=case.id,
        version=1,
        status=InvestigationRunStatus.PENDING,
        evidence_fingerprint=case.evidence_fingerprint,
        created_at=datetime.now(timezone.utc),
        summary="",
        provider_name="mock",
        model="mock-deterministic-v1",
        prompt_version="",
        engine_version="",
        evidence=case.evidence,
        stage_status={"overall": "PENDING"},
    )


def test_a_write_ahead_marker_is_durable_across_sessions(db_session) -> None:
    """The whole point of ADR-008: the marker is committed before the work starts.

    A second session can see the pending run, which is what a recovery process is.
    If the marker only lived in the request's transaction, a crash would leave nothing
    to find and the case would be stuck in ``INVESTIGATING`` with no run at all.
    """
    repository = SqlAlchemyCaseRepository(db_session)
    service = CaseService(repository, MockLlmProvider())
    opened = service.open_case(_open_command("DSC-DB-PENDING"))
    pending = _pending_record(opened)
    repository.begin_investigation(
        opened.with_pending_investigation(pending), expected_version=opened.version
    )

    engine = cast(Engine, db_session.get_bind())
    fresh = get_sessionmaker(engine)()
    try:
        fresh_repository = SqlAlchemyCaseRepository(fresh)
        found = fresh_repository.list_pending_investigations(
            started_before=pending.created_at + timedelta(hours=1)
        )
        assert found == ((opened.id, pending.id),)
        stored = fresh_repository.get(opened.id)
        assert stored.status is CaseStatus.INVESTIGATING
        assert stored.investigations[0].status is InvestigationRunStatus.PENDING
    finally:
        fresh.close()


def test_resuming_completes_the_pending_row_against_a_real_database(db_session) -> None:
    """A pending row is updated in place, not replaced by a second run."""
    repository = SqlAlchemyCaseRepository(db_session)
    service = CaseService(repository, MockLlmProvider())
    opened = service.open_case(_open_command("DSC-DB-RESUME"))
    pending = _pending_record(opened)
    repository.begin_investigation(
        opened.with_pending_investigation(pending), expected_version=opened.version
    )

    run = service.resume_investigation(opened.id, prompt_version="prompt-v1")
    db_session.commit()

    engine = cast(Engine, db_session.get_bind())
    fresh = get_sessionmaker(engine)()
    try:
        fresh_repository = SqlAlchemyCaseRepository(fresh)
        reloaded = fresh_repository.get(opened.id)
        assert fresh_repository.list_pending_investigations(
            started_before=datetime.now(timezone.utc) + timedelta(hours=1)
        ) == ()
    finally:
        fresh.close()

    assert run.id == pending.id
    assert run.version == pending.version
    assert reloaded.status is CaseStatus.AWAITING_REVIEW
    assert len(reloaded.investigations) == 1
    assert reloaded.investigations[0].id == pending.id
    assert reloaded.investigations[0].status is InvestigationRunStatus.COMPLETE
    assert reloaded.investigations[0].calculation is not None
