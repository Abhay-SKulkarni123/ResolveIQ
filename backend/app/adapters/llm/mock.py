"""A deterministic, offline provider.

The default implementation of :class:`~app.ports.llm.LlmProvider`, and the reason the
test suite needs no network and no credentials.

Determinism is the requirement, not realism. Given the same request it produces the same
response, byte for byte, so a test can assert on it and a reviewer can reproduce a run.
It reads the evidence bundle and the engine's own findings rather than inventing
anything, which means a mock that passes here could not produce a figure the
deterministic engine would contradict.

It also fails on demand. :meth:`MockLlmProvider.fail_with` and
:meth:`MockLlmProvider.corrupt_response` exist so the paths that matter — a provider
that is unconfigured, times out, or answers with something unparseable — can be tested
without waiting for a real provider to misbehave. A mock that can only succeed proves
nothing about how the application handles the other cases, which is most of what §10 is
about.

Not in mock mode does anything become less strict. Citation validation, the closed
hypothesis vocabulary and the no-money-fields rule apply identically, so a passing test
here is evidence about the real pipeline's shape rather than about a special case.
"""

from __future__ import annotations

import json

from app.domain.hypotheses import (
    FindingSeverity,
    HypothesisCode,
    HypothesisStatus,
    ResolutionOptionType,
)
from app.ports.interpretation import (
    Finding,
    Hypothesis,
    InterpretationResponse,
    ResolutionOption,
)
from app.ports.llm import (
    InterpretationRequest,
    LlmNotConfiguredError,
    LlmProviderError,
    LlmResponseFormatError,
)

__all__ = ["MockLlmProvider"]


class MockLlmProvider:
    """A provider that answers from the evidence it is given.

    Implements the port structurally; ``isinstance(MockLlmProvider(), LlmProvider)``
    holds because :class:`~app.ports.llm.LlmProvider` is a runtime-checkable
    ``Protocol``.
    """

    def __init__(
        self,
        *,
        name: str = "mock",
        model: str = "mock-deterministic-v1",
        fail_with: type[LlmProviderError] | None = None,
        corrupt_response: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        #: When set, every call raises this instead of answering.
        self._fail_with = fail_with
        #: When set, every call returns text that will not parse. Simulates a provider
        #: that ignores the schema, which is the common real-world failure.
        self._corrupt = corrupt_response
        #: Requests received, so a caller or test can assert on what was sent — in
        #: particular that the schema and the allowed keys were included.
        self.requests: list[InterpretationRequest] = []

    def structured_infer(self, request: InterpretationRequest) -> InterpretationResponse:
        """Return a schema-valid interpretation of ``request``."""
        self.requests.append(request)

        if self._fail_with is not None:
            raise self._fail_with(
                f"mock provider configured to fail with {self._fail_with.__name__}"
            )

        if self._corrupt:
            # Deliberately plausible: valid JSON, wrong shape. A provider that returned
            # nonsense would be easier to detect than this one.
            raise LlmResponseFormatError(
                "mock provider configured to return an unparseable response",
                raw_response=json.dumps({"findings": [{"code": "X", "amount": "10.00"}]}),
            )

        return self._interpret(request)

    def _interpret(self, request: InterpretationRequest) -> InterpretationResponse:
        """Derive a response from the evidence bundle alone.

        Picks citations from what was supplied rather than from a fixed list, so the
        mock cannot pass the citation validator by memorising the keys of one particular
        fixture: change the bundle and its citations change with it.
        """
        keys = sorted(request.evidence.allowed_keys)
        if not keys:  # pragma: no cover - the request constructor forbids an empty bundle
            raise LlmNotConfiguredError("mock provider received no evidence to cite")

        invoice_key = next(
            (key for key in keys if key.startswith("invoice:") and "#line:" not in key),
            keys[0],
        )
        line_keys = [key for key in keys if "#line:" in key]
        term_keys = [key for key in keys if key.startswith("contract:")]

        findings: list[Finding] = [
            Finding(
                code="MOCK_RECALCULATION_SUMMARY",
                severity=FindingSeverity.WARN,
                category="RECONCILIATION",
                narrative=(
                    "The deterministic engine recalculated this invoice from its contract "
                    "terms and usage evidence. This mock finding exists so the workflow can "
                    "be exercised end to end; it asserts no cause."
                ),
                confidence="1",
                supporting_evidence=tuple(key for key in [invoice_key, *line_keys[:1]] if key)
                or (invoice_key,),
            )
        ]

        hypotheses: list[Hypothesis] = []
        metric_keys = sorted({key.split("#term:", 1)[1] for key in term_keys if "#term:" in key})
        target_metric = metric_keys[0] if metric_keys else None

        supporting = tuple(key for key in [invoice_key, *term_keys[:1], *line_keys[:1]] if key)
        hypotheses.append(
            Hypothesis(
                hypothesis_code=HypothesisCode.OVERAGE_TIER_MISMATCH
                if target_metric
                else HypothesisCode.UNEXPLAINED,
                title="Overage may have been billed outside the contracted tier ladder",
                narrative=(
                    "Raised as a candidate only. The engine computes what the contract "
                    "supports for this code; whether it applies is a reviewer's judgement, "
                    "not this response's."
                ),
                supporting_evidence=supporting or (invoice_key,),
                refuting_evidence=(),
                metric_key=target_metric,
                likelihood="0.5",
                status=HypothesisStatus.OPEN,
            )
        )

        return InterpretationResponse(
            summary=(
                "Mock interpretation. One reconciling finding and one candidate cause were "
                "derived from the supplied evidence; no monetary amount is asserted here."
            ),
            findings=tuple(findings),
            hypotheses=tuple(hypotheses),
            resolution_options=(
                ResolutionOption(
                    option_type=ResolutionOptionType.NO_ADJUSTMENT,
                    title="Confirm the invoice with supporting explanation",
                    rationale=(
                        "Non-monetary by construction: a reviewer must decide whether any "
                        "credit is warranted, and this option moves no money."
                    ),
                    hypothesis_code=HypothesisCode.UNEXPLAINED,
                    supporting_evidence=(invoice_key,),
                ),
            ),
            unverified_claims=(
                "Whether the source system applied a contract version other than the one "
                "supplied cannot be determined from this evidence.",
            ),
        )
