"""The audit trail over HTTP: one ordered, read-only history of a case.

The trail's value is that it is *the* record -- complete, in order, and impossible to
edit through the API. Each of those is a property of the route, not of the store, so
they are asserted here against the memory store:

* the sequence reads oldest-first and matches the order the operations happened in,
  even though the whole test runs inside one coarse clock tick;
* the response is a plain JSON document a browser can render, not the frozen form the
  aggregate keeps;
* the endpoint only reads -- two calls return the same page, and no event is written
  by the act of reading it.

Running against the memory store is deliberate: it is the API a developer exercises
without a database, and a route that only worked against PostgreSQL would be caught by
``tests/integration/test_audit_persistence.py`` instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ACTOR = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}


@pytest.fixture
def client() -> TestClient:
    """A client on the memory store, chosen through the settings the app reads."""
    import os

    from app.config import get_settings

    previous = os.environ.get("CASE_STORE")
    os.environ["CASE_STORE"] = "memory"
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), headers=ACTOR) as test_client:
            yield test_client
    finally:
        if previous is None:
            os.environ.pop("CASE_STORE", None)
        else:
            os.environ["CASE_STORE"] = previous
        get_settings.cache_clear()


def open_payload(dispute_external_id: str = "DSC-1") -> dict[str, object]:
    return {
        "dispute_external_id": dispute_external_id,
        "description": "We were billed the wrong rate for API calls.",
        "invoice": {
            "external_id": "INV-2026-03-0042",
            "currency": "USD",
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "stated_total": {"amount": "28.40", "currency": "USD"},
            "lines": [
                {
                    "metric_key": "api_calls",
                    "line_type": "USAGE",
                    "recorded_amount": {"amount": "28.40", "currency": "USD"},
                }
            ],
        },
        "contract_external_id": "CTR-5512",
        "dispute_text": "We were billed the wrong rate for API calls.",
        "severity": "MEDIUM",
        "price_terms": [
            {
                "metric_key": "api_calls",
                "billing_mode": "TIERED",
                "currency": "USD",
                "tiers": [
                    {"up_to": "10000", "unit_price": {"amount": "0.50", "currency": "USD"}},
                    {"up_to": "50000", "unit_price": {"amount": "0.0007", "currency": "USD"}},
                ],
            }
        ],
        "usage_events": [
            {
                "external_id": "e1",
                "dedupe_key": "DK-1",
                "metric_key": "api_calls",
                "occurred_at": "2026-03-15T09:00:00+00:00",
                "quantity": "41234",
                "unit": "calls",
            }
        ],
    }


def open_case(client: TestClient, dispute_external_id: str = "DSC-1") -> dict[str, Any]:
    response = client.post("/api/v1/disputes", json=open_payload(dispute_external_id))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def audit_url(dispute_id: str) -> str:
    return f"/api/v1/disputes/{dispute_id}/audit"


def reviewed_case(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """An awaiting-review case and the run its review annotated."""
    created = open_case(client)
    run = client.post(
        f"/api/v1/disputes/{created['id']}/investigations", json={}
    ).json()
    response = client.post(
        f"/api/v1/disputes/{created['id']}/review",
        json={
            "investigation_id": run["id"],
            "target_type": "FINDING",
            "target_id": run["findings"][0]["id"],
            "action": "ACCEPT",
            "rationale": "matches the dashboard export",
        },
    )
    assert response.status_code == 201, response.text
    return created, run


# ---------------------------------------------------------------------------
# What the trail contains
# ---------------------------------------------------------------------------


def test_a_new_case_has_exactly_its_creation_recorded(client: TestClient) -> None:
    created = open_case(client)

    body = client.get(audit_url(created["id"])).json()

    assert body["total"] == 1
    assert [event["event_type"] for event in body["items"]] == ["CASE_CREATED"]


def test_the_creation_event_names_the_actor_from_the_headers(client: TestClient) -> None:
    """The trail's second question is "who", and the header is where the demo identity
    arrives; if it were dropped here the record would answer only half of it."""
    created = open_case(client)

    (event,) = client.get(audit_url(created["id"])).json()["items"]

    assert event["actor_id"] == "rev-1"
    assert event["actor_role"] == "reviewer"


def test_the_payload_is_a_plain_json_document(client: TestClient) -> None:
    """A frozen mapping would not survive serialisation, and the caller wants data."""
    created = open_case(client)

    (event,) = client.get(audit_url(created["id"])).json()["items"]

    assert isinstance(event["payload"], dict)
    assert event["payload"]["external_id"] == created["external_id"]
    assert event["payload"]["evidence_fingerprint"] == created["evidence_fingerprint"]


def test_a_full_lifecycle_reads_oldest_first(client: TestClient) -> None:
    """The ordering promise, exercised end to end inside one clock tick.

    All four operations happen in well under the clock's resolution, so this is
    exactly the case a naive ``(occurred_at, id)`` sort gets wrong.
    """
    created, _ = reviewed_case(client)
    client.post(
        f"/api/v1/disputes/{created['id']}/decision",
        json={"decision": "RESOLVED", "rationale": "recalculated total was chargeable"},
    )

    body = client.get(audit_url(created["id"])).json()

    assert [event["event_type"] for event in body["items"]] == [
        "CASE_CREATED",
        "INVESTIGATION_COMPLETED",
        "REVIEW_RECORDED",
        "DECISION_RECORDED",
    ]
    assert body["total"] == 4


def test_the_investigation_event_cites_the_run(client: TestClient) -> None:
    """The run is referenced by id rather than copied, so the trail cannot disagree
    with the aggregate about what the run said."""
    created, run = reviewed_case(client)

    events = client.get(audit_url(created["id"])).json()["items"]
    completed = next(event for event in events if event["event_type"] == "INVESTIGATION_COMPLETED")

    assert completed["investigation_id"] == run["id"]


def test_an_idempotency_key_becomes_the_correlation_id(client: TestClient) -> None:
    """The key a retry carries is what ties its request to the event it produced."""
    headers = {"Idempotency-Key": "open-abc"}
    created = client.post(
        "/api/v1/disputes", json=open_payload(), headers=headers
    ).json()

    (event,) = client.get(audit_url(created["id"])).json()["items"]

    assert event["correlation_id"] == "open-abc"


def test_an_event_without_a_key_has_no_correlation_id(client: TestClient) -> None:
    """Absent is meaningful: it says the request was not an idempotent one."""
    created = open_case(client)

    (event,) = client.get(audit_url(created["id"])).json()["items"]

    assert event["correlation_id"] is None


def test_each_event_carries_the_case_version_at_the_time(
    client: TestClient,
) -> None:
    """The version is what makes the sequence reconstructable after a coarse clock."""
    created, _ = reviewed_case(client)

    versions = [
        event["entity_version"]
        for event in client.get(audit_url(created["id"])).json()["items"]
    ]

    assert versions == [1, 2, 3]


# ---------------------------------------------------------------------------
# Pagination and refusals
# ---------------------------------------------------------------------------


def test_pagination_returns_a_window_and_a_total(client: TestClient) -> None:
    created, _ = reviewed_case(client)

    body = client.get(audit_url(created["id"]), params={"limit": 2, "offset": 1}).json()

    assert [event["entity_version"] for event in body["items"]] == [2, 3]
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1


def test_an_offset_past_the_end_is_empty_but_still_totals(client: TestClient) -> None:
    """An empty page and "no events" are different answers, and ``total`` tells them
    apart."""
    created = open_case(client)

    body = client.get(audit_url(created["id"]), params={"offset": 50}).json()

    assert body["items"] == []
    assert body["total"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 500}, {"offset": -1}])
def test_out_of_range_paging_is_rejected(
    client: TestClient, params: dict[str, int]
) -> None:
    created = open_case(client)

    response = client.get(audit_url(created["id"]), params=params)

    assert response.status_code == 422


def test_the_trail_of_an_unknown_case_is_a_404(client: TestClient) -> None:
    response = client.get(audit_url("11111111-1111-1111-1111-111111111111"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DISPUTE_NOT_FOUND"


def test_a_malformed_dispute_id_is_a_4xx_not_a_500(client: TestClient) -> None:
    response = client.get(audit_url("not-a-uuid"))

    assert 400 <= response.status_code < 500
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# The endpoint only reads
# ---------------------------------------------------------------------------


def test_reading_the_trail_writes_nothing(client: TestClient) -> None:
    """A read that appended an event would make the record describe its own readers."""
    created = open_case(client)
    first = client.get(audit_url(created["id"])).json()

    second = client.get(audit_url(created["id"])).json()

    assert first == second
    assert second["total"] == 1


def test_capabilities_advertises_the_audit_trail(client: TestClient) -> None:
    features = client.get("/api/v1/capabilities").json()["features"]

    assert features["audit_trail"] is True
