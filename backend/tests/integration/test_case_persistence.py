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

from datetime import date, datetime, timezone
from decimal import Decimal

from app.adapters.llm.mock import MockLlmProvider
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.domain.billing import (
    InvoiceLine,
    InvoicePeriod,
    LineType,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.cases import CaseStatus
from app.domain.contracts import BillingMode
from app.domain.money import Money
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