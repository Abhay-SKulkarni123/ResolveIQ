"""The OpenAI-compatible adapter, exercised over a stubbed transport.

No network: ``httpx.MockTransport`` replaces the socket, so every failure mode the
adapter claims to handle -- a timeout, a rejected key, a 5xx, a body that is not JSON, a
body that is JSON but not the schema -- is produced deterministically.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.adapters.llm.openai_compatible import OpenAiCompatibleProvider
from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType
from app.domain.hypotheses import FindingSeverity
from app.ports.interpretation import Finding, InterpretationResponse
from app.ports.llm import (
    InterpretationRequest,
    LlmNotConfiguredError,
    LlmProviderError,
    LlmResponseFormatError,
    LlmTimeoutError,
)

API_KEY = "test-key-not-a-real-secret"
MODEL = "test-model"


def _request() -> InterpretationRequest:
    bundle = EvidenceBundle.of(
        [
            EvidenceItem.create("invoice:INV-1", EvidenceType.INVOICE, {"stated_total": "28.40"}),
            EvidenceItem.create("dispute_text:DSC-1", EvidenceType.DISPUTE_TEXT, {"text": "wrong"}),
        ]
    )
    return InterpretationRequest(
        dispute_external_id="DSC-1",
        evidence=bundle,
        dispute_text="We were billed the wrong rate.",
        response_schema=InterpretationResponse.model_json_schema(),
        prompt_version="prompt-v1",
        system_instructions="Interpret the evidence.",
        allowed_evidence_keys=bundle.allowed_keys,
    )


def _valid_content() -> str:
    response = InterpretationResponse(
        summary="The recorded total does not agree with the contract term.",
        findings=(
            Finding(
                code="RATE_MISMATCH",
                severity=FindingSeverity.WARN,
                category="PRICING",
                narrative="The recorded amount looks wrong against the term.",
                confidence="0.8",
                supporting_evidence=("invoice:INV-1",),
            ),
        ),
    )
    return response.model_dump_json()


def _provider(handler: Callable[[httpx.Request], httpx.Response], **overrides: object):
    options: dict[str, object] = {
        "api_key": API_KEY,
        "model": MODEL,
        "max_retries": 0,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return OpenAiCompatibleProvider(**options)  # type: ignore[arg-type]


def _chat(content: str | None) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        message: dict[str, object] = {"content": content}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    return handler


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_missing_key_refuses_to_construct() -> None:
    with pytest.raises(LlmNotConfiguredError):
        OpenAiCompatibleProvider(api_key="", model=MODEL)


def test_a_blank_key_refuses_to_construct() -> None:
    with pytest.raises(LlmNotConfiguredError):
        OpenAiCompatibleProvider(api_key="   ", model=MODEL)


def test_a_missing_model_refuses_to_construct() -> None:
    with pytest.raises(LlmNotConfiguredError):
        OpenAiCompatibleProvider(api_key=API_KEY, model="")


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_valid_response_parses_to_an_interpretation_response() -> None:
    result = _provider(_chat(_valid_content())).structured_infer(_request())

    assert isinstance(result, InterpretationResponse)
    assert result.findings[0].code == "RATE_MISMATCH"


def test_the_request_is_a_json_mode_chat_completion() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": _valid_content()}}]})

    _provider(handler).structured_infer(_request())

    assert seen["auth"] == f"Bearer {API_KEY}"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    # The allow-list and the schema travel with the request, so the model has what the
    # rules reference.
    assert "invoice:INV-1" in json.dumps(body["messages"][1])


# ---------------------------------------------------------------------------
# Responses that must not read as success
# ---------------------------------------------------------------------------


def test_a_monetary_field_is_rejected_not_ignored() -> None:
    """NEP-02 at the boundary: the schema has no amount field, so one is an error."""
    content = json.dumps(
        {
            "summary": "s",
            "findings": [
                {
                    "code": "X",
                    "severity": "INFO",
                    "category": "C",
                    "narrative": "n",
                    "confidence": "0.5",
                    "supporting_evidence": ["invoice:INV-1"],
                    "amount": "9999.99",
                }
            ],
        }
    )
    with pytest.raises(LlmResponseFormatError):
        _provider(_chat(content)).structured_infer(_request())


def test_content_that_is_not_json_is_a_format_error() -> None:
    with pytest.raises(LlmResponseFormatError):
        _provider(_chat("this is not json")).structured_infer(_request())


def test_content_missing_required_fields_is_a_format_error() -> None:
    with pytest.raises(LlmResponseFormatError):
        _provider(_chat(json.dumps({"findings": []}))).structured_infer(_request())


def test_a_response_with_no_choices_is_a_format_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LlmResponseFormatError):
        _provider(handler).structured_infer(_request())


def test_a_refusal_is_a_format_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": None, "refusal": "I cannot help"}}]}
        )

    with pytest.raises(LlmResponseFormatError):
        _provider(handler).structured_infer(_request())


def test_a_body_that_is_not_a_json_object_is_a_format_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, 2, 3]")

    with pytest.raises(LlmResponseFormatError):
        _provider(handler).structured_infer(_request())


# ---------------------------------------------------------------------------
# Transport and status failures
# ---------------------------------------------------------------------------


def test_a_timeout_is_an_llm_timeout_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(LlmTimeoutError):
        _provider(handler).structured_infer(_request())


def test_a_rejected_key_is_a_configured_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(LlmNotConfiguredError):
        _provider(handler).structured_infer(_request())


def test_a_server_error_is_a_provider_error_not_a_format_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LlmProviderError) as caught:
        _provider(handler).structured_infer(_request())
    assert not isinstance(caught.value, LlmResponseFormatError)


def test_a_transient_failure_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adapters.llm.openai_compatible.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "try later"})
        return httpx.Response(200, json={"choices": [{"message": {"content": _valid_content()}}]})

    result = _provider(handler, max_retries=1).structured_infer(_request())

    assert calls["n"] == 2
    assert isinstance(result, InterpretationResponse)


def test_a_format_error_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adapters.llm.openai_compatible.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(LlmResponseFormatError):
        _provider(handler, max_retries=3).structured_infer(_request())

    assert calls["n"] == 1


def test_an_unreachable_host_is_a_provider_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(LlmProviderError):
        _provider(handler).structured_infer(_request())
