"""A real provider: any OpenAI-compatible chat-completions endpoint.

This is the adapter docs/SYSTEM_DESIGN.md §7.1 said a real provider would be: the port
plus this file plus a conformance run, with no change to the domain or the pricing
engine. It talks HTTP via ``httpx`` and holds no vendor SDK, so the test suite can
drive it with an ``httpx.MockTransport`` and needs no network.

Three properties are the reason it is written this carefully.

**The response is validated, not trusted.** The endpoint is asked for JSON and its
output is fed straight into :class:`~app.ports.interpretation.InterpretationResponse`,
which forbids unknown fields and has nowhere to put a monetary amount. A model that
returns ``{"amount": "9999.99"}`` gets that field *rejected*, not ignored, so the
no-money rule is enforced by the parser rather than by the prompt.

**Failures are typed.** A missing key is :class:`LlmNotConfiguredError` (a deployment
fault; retrying cannot fix it), a timeout is :class:`LlmTimeoutError`, and an
unparseable or schema-invalid answer is :class:`LlmResponseFormatError` carrying the
offending text. None of them may be expressed as an empty response, because an empty
response is indistinguishable from a clean invoice.

**The engine stays the authority.** The prompt asks for a ``hypothesis_code`` and a
narrative; the amount attached to that code is computed afterwards by
:mod:`app.pricing.impact`. Nothing here can produce a figure.

Privacy note: the request contains customer dispute text and evidence. Point
``LLM_BASE_URL`` at an endpoint whose data-handling terms you accept; a gateway in
front of a hosted model is a supported deployment, not an afterthought.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.ports.interpretation import InterpretationResponse
from app.ports.llm import (
    InterpretationRequest,
    LlmNotConfiguredError,
    LlmProviderError,
    LlmResponseFormatError,
    LlmTimeoutError,
)

__all__ = ["OpenAiCompatibleProvider"]

#: Status codes worth another attempt: rate limiting and server-side faults are
#: transient. Everything else 4xx is a request we built wrong or a credential problem,
#: and repeating it changes nothing.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class OpenAiCompatibleProvider:
    """Chat-completions with JSON output, validated against the shipped schema."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        name: str = "openai",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            # Raised at construction, not on first call: a deployment that cannot
            # authenticate should fail when it is configured, not silently degrade a
            # live investigation into a "model unavailable" run.
            raise LlmNotConfiguredError(
                "LLM_API_KEY is required when LLM_PROVIDER=openai; refusing to start "
                "without it rather than falling back to the mock"
            )
        if not model or not model.strip():
            raise LlmNotConfiguredError("LLM_MODEL is required when LLM_PROVIDER=openai")
        self.name = name
        self.model = model.strip()
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = max(1.0, float(timeout_seconds))
        self._max_retries = max(0, int(max_retries))
        self._temperature = float(temperature)
        self._transport = transport

    # -- the port ---------------------------------------------------------

    def structured_infer(self, request: InterpretationRequest) -> InterpretationResponse:
        payload = self._build_payload(request)
        body = self._post_with_retries(payload)
        content = self._extract_content(body)
        return self._parse(content)

    # -- request building -------------------------------------------------

    def _build_payload(self, request: InterpretationRequest) -> dict[str, Any]:
        evidence = [
            {
                "natural_key": item.natural_key,
                "evidence_type": item.evidence_type.value,
                "snapshot": dict(item.snapshot),
            }
            for item in request.evidence.items
        ]
        user_content = json.dumps(
            {
                "dispute_external_id": request.dispute_external_id,
                "dispute_text": request.dispute_text,
                "allowed_evidence_keys": sorted(request.allowed_evidence_keys),
                "evidence": evidence,
                "response_schema": request.response_schema,
            },
            separators=(",", ":"),
            sort_keys=False,
        )
        return {
            "model": self.model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": request.system_instructions},
                {"role": "user", "content": user_content},
            ],
            # Broad compatibility: ask for a JSON object rather than a strict
            # json_schema, then validate the result against our own schema. A gateway
            # that does not support structured outputs still works, and the guarantee
            # comes from the validator either way.
            "response_format": {"type": "json_object"},
        }

    # -- transport --------------------------------------------------------

    def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: LlmProviderError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._post(payload)
            except (LlmTimeoutError, LlmProviderError) as exc:
                # A response-format error is permanent and must not be retried; only
                # transient transport/server faults are. LlmResponseFormatError is a
                # subclass of LlmProviderError, so it is excluded explicitly.
                if isinstance(exc, LlmResponseFormatError) or not self._is_retryable(exc):
                    raise
                last_error = exc
                if attempt < self._max_retries:
                    # Bounded exponential backoff; the sleep is short because the
                    # caller already holds the request open for it.
                    time.sleep(min(0.5 * (2**attempt), 2.0))
        # The loop returns on success and only leaves by exhausting retries, which
        # means ``last_error`` is always set here; the guard is for the type checker.
        raise last_error or LlmProviderError(  # pragma: no cover - unreachable
            "the provider failed without producing an error"
        )

    def _is_retryable(self, exc: LlmProviderError) -> bool:
        return isinstance(exc, (LlmTimeoutError, _RetryableProviderError))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError(f"the provider did not answer within {self._timeout}s") from exc
        except httpx.TransportError as exc:
            # Connection refused, DNS failure, TLS failure: transient and retryable.
            raise _RetryableProviderError("the provider could not be reached") from exc

        if response.status_code in (401, 403):
            # A credential/deployment fault; never a "clean invoice" and never retried.
            raise LlmNotConfiguredError(
                f"the provider rejected the credentials (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            message = f"the provider returned HTTP {response.status_code}"
            if response.status_code in _RETRYABLE_STATUS:
                raise _RetryableProviderError(message)
            raise LlmProviderError(message)

        try:
            parsed = response.json()
        except ValueError as exc:
            raise LlmResponseFormatError(
                "the provider response was not JSON", raw_response=response.text
            ) from exc
        if not isinstance(parsed, dict):
            raise LlmResponseFormatError(
                "the provider response was not a JSON object", raw_response=response.text
            )
        return parsed

    # -- response parsing -------------------------------------------------

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmResponseFormatError(
                "the provider response contained no choices", raw_response=json.dumps(body)[:500]
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LlmResponseFormatError(
                "the provider choice had no message", raw_response=json.dumps(body)[:500]
            )
        if message.get("refusal"):
            raise LlmResponseFormatError(
                "the provider refused to answer", raw_response=str(message["refusal"])[:500]
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlmResponseFormatError(
                "the provider returned no content", raw_response=json.dumps(body)[:500]
            )
        return content

    @staticmethod
    def _parse(content: str) -> InterpretationResponse:
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmResponseFormatError(
                "the provider content was not valid JSON", raw_response=content
            ) from exc
        try:
            # extra="forbid" on every model means a monetary field is rejected here,
            # at the boundary, rather than silently dropped.
            return InterpretationResponse.model_validate(decoded)
        except ValidationError as exc:
            raise LlmResponseFormatError(
                f"the provider response did not match the schema: {exc.error_count()} error(s)",
                raw_response=content,
            ) from exc


class _RetryableProviderError(LlmProviderError):
    """A transient provider failure (transport or a retryable status).

    Private because it is an implementation detail of retry classification: callers
    still catch it as :class:`LlmProviderError`, which maps to a ``DEGRADED`` run.
    """
