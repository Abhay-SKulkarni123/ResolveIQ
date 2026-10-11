"""The structured-logging plumbing: the id, the formatter and the configuration.

These are the pieces every other log site depends on, so they are pinned directly:
a correlation id that cannot be malformed, a JSON object whose core fields an event
cannot overwrite, and a configuration that is safe to call once per test process.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from app.config import Settings
from app.logging_config import (
    MAX_REQUEST_ID_LENGTH,
    JsonFormatter,
    configure_logging,
    current_request_id,
    error_category,
    new_request_id,
    normalise_request_id,
    reset_request_id,
    set_request_id,
)


@pytest.fixture
def app_logger_state() -> Iterator[logging.Logger]:
    """Restore the application logger's handlers and level after a test."""
    logger = logging.getLogger("app")
    handlers = list(logger.handlers)
    level = logger.level
    try:
        yield logger
    finally:
        logger.handlers = handlers
        logger.setLevel(level)


def _record(**fields: object) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "message", None, None)
    record.event = "real.event"
    if fields:
        record.fields = fields
    return record


class TestNormaliseRequestId:
    def test_a_generated_id_is_a_hex_uuid(self) -> None:
        value = new_request_id()
        assert len(value) == 32
        assert all(char in "0123456789abcdef" for char in value)

    def test_two_generated_ids_differ(self) -> None:
        assert new_request_id() != new_request_id()

    def test_a_safe_client_id_is_adopted(self) -> None:
        assert normalise_request_id("abc-123") == "abc-123"

    def test_whitespace_around_a_good_id_is_trimmed(self) -> None:
        assert normalise_request_id("  abc-123  ") == "abc-123"

    def test_a_maximum_length_id_is_accepted(self) -> None:
        at_limit = "a" * MAX_REQUEST_ID_LENGTH
        assert normalise_request_id(at_limit) == at_limit

    @pytest.mark.parametrize(
        "unsafe",
        [
            None,
            "",
            "   ",
            "has a space",
            "line\nbreak",
            '"quoted"',
            "a" * (MAX_REQUEST_ID_LENGTH + 1),
        ],
    )
    def test_an_unsafe_id_is_rejected(self, unsafe: str | None) -> None:
        assert normalise_request_id(unsafe) is None


class TestContextVariable:
    def test_it_binds_and_restores(self) -> None:
        assert current_request_id() is None
        token = set_request_id("abc-123")
        assert current_request_id() == "abc-123"
        reset_request_id(token)
        assert current_request_id() is None


class TestJsonFormatter:
    def test_it_emits_a_json_object_with_the_core_fields(self) -> None:
        payload = json.loads(JsonFormatter().format(_record()))

        assert payload["severity"] == "INFO"
        assert payload["logger"] == "app.test"
        assert payload["event"] == "real.event"
        assert payload["request_id"] is None
        assert "timestamp" in payload

    def test_event_fields_are_merged_in(self) -> None:
        payload = json.loads(JsonFormatter().format(_record(status_code=204, duration_ms=1.5)))

        assert payload["status_code"] == 204
        assert payload["duration_ms"] == 1.5

    def test_an_event_cannot_overwrite_the_core_fields(self) -> None:
        token = set_request_id("abc-123")
        try:
            record = _record()
            record.fields = {"event": "spoofed", "request_id": "spoofed", "severity": "spoofed"}
            payload = json.loads(JsonFormatter().format(record))
        finally:
            reset_request_id(token)

        assert payload["event"] == "real.event"
        assert payload["request_id"] == "abc-123"
        assert payload["severity"] == "INFO"

    def test_the_current_request_id_is_attached(self) -> None:
        token = set_request_id("abc-123")
        try:
            payload = json.loads(JsonFormatter().format(_record()))
        finally:
            reset_request_id(token)

        assert payload["request_id"] == "abc-123"

    def test_a_record_without_an_event_falls_back_to_its_message(self) -> None:
        record = logging.LogRecord("app.other", logging.WARNING, __file__, 1, "plain", None, None)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["event"] == "plain"


class TestErrorCategory:
    def test_it_is_the_class_name_and_not_the_message(self) -> None:
        exc = ValueError("a secret value that must not be logged")
        assert error_category(exc) == "ValueError"


class TestConfigureLogging:
    def test_it_attaches_the_json_handler_once(
        self, app_logger_state: logging.Logger
    ) -> None:
        configure_logging(Settings())
        configure_logging(Settings())

        marked = [
            handler
            for handler in app_logger_state.handlers
            if getattr(handler, "_resolveiq_json", False)
        ]
        assert len(marked) == 1
        assert isinstance(marked[0].formatter, JsonFormatter)

    def test_it_reads_the_level_from_settings(self, app_logger_state: logging.Logger) -> None:
        configure_logging(Settings(app_log_level="DEBUG"))
        assert app_logger_state.level == logging.DEBUG

    def test_an_unknown_level_falls_back_to_info(self, app_logger_state: logging.Logger) -> None:
        configure_logging(Settings(app_log_level="NOT_A_LEVEL"))
        assert app_logger_state.level == logging.INFO
