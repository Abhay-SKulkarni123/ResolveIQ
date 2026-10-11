"""Request correlation and lifecycle logging, over the real application.

The application is built with the memory store, so the whole middleware stack runs
without a database. What is asserted here is what an operator would rely on during
an incident: every response names a request, the error envelope agrees with the
header, and one structured line records each request's outcome.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import RequestContextMiddleware
from app.config import get_settings
from app.logging_config import REQUEST_ID_HEADER
from app.main import create_app

ACTOR = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client on the memory store, using the environment the app actually reads."""
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


def test_a_response_carries_a_generated_correlation_id(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 32


def test_a_safe_inbound_id_is_echoed_back(client) -> None:
    response = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_an_unsafe_inbound_id_is_replaced(client) -> None:
    response = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "not a safe id"})

    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != "not a safe id"
    assert len(echoed) == 32


def test_each_request_gets_its_own_id(client) -> None:
    first = client.get("/api/v1/health").headers[REQUEST_ID_HEADER]
    second = client.get("/api/v1/health").headers[REQUEST_ID_HEADER]

    assert first != second


def test_the_error_envelope_agrees_with_the_response_header(client) -> None:
    response = client.get("/api/v1/disputes/not-a-uuid")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_IDENTIFIER"
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_a_handled_failure_is_logged_with_its_code(client, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.api.errors")

    client.get("/api/v1/disputes/not-a-uuid")

    records = [r for r in caplog.records if getattr(r, "event", None) == "api.error.responded"]
    assert records
    fields = records[0].fields
    assert fields["status_code"] == 422
    assert fields["code"] == "INVALID_IDENTIFIER"


def test_a_completed_request_is_logged(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.api.middleware")

    client.get("/api/v1/health")

    records = [r for r in caplog.records if getattr(r, "event", None) == "http.request.completed"]
    assert records
    fields = records[0].fields
    assert fields["method"] == "GET"
    assert fields["path"] == "/api/v1/health"
    assert fields["status_code"] == 200
    assert isinstance(fields["duration_ms"], float)


def test_an_unhandled_failure_is_logged_with_a_safe_category(caplog) -> None:
    application = FastAPI()
    application.add_middleware(RequestContextMiddleware)

    @application.get("/boom")
    def boom() -> None:
        raise RuntimeError("a value that should not be logged: hunter2")

    caplog.set_level(logging.ERROR, logger="app.api.middleware")
    with TestClient(application, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")

    assert response.status_code == 500
    records = [r for r in caplog.records if getattr(r, "event", None) == "http.request.failed"]
    assert records
    assert records[0].fields["error_category"] == "RuntimeError"
    assert "hunter2" not in caplog.text
