"""The provider port: the only place the application meets a language model.

docs/SYSTEM_DESIGN.md §7.1 defines this boundary, and the shape here follows it. The
application depends on :class:`LlmProvider`, never on a vendor SDK, so a real provider
arrives as an adapter plus a conformance test rather than as a change to the domain
(``tests/contract/test_llm_provider_conformance.py`` holds every provider to the same
contract).

Two things are worth stating about what is *not* here.

There is no retry logic. A retry policy would have to be justified against a specific
failure mode, and "the network was unhappy" is not one: an LLM call that timed out and
one that returned a malformed response fail differently, and retrying the second just
costs another timeout. ``llm_max_retries`` exists in settings and is deliberately
unread by this module. If a retry is ever added it belongs here, with a test that shows
what it recovers from and what it costs.

There is no streaming, no token accounting, and no prompt assembly. The port takes a
finished :class:`InterpretationRequest` and returns a validated response, which keeps the
prompt-building decision in the service layer where it can be versioned and stored
(FR-016) instead of being scattered across adapters.

Every failure mode a provider can have is one of the exceptions below. The distinction
matters to the caller: :class:`LlmNotConfiguredError` means the deployment is wrong and
should not retry, :class:`LlmTimeoutError` means the call was abandoned, and
:class:`LlmResponseFormatError` means the provider answered but the answer was unusable.
None of them is allowed to look like a successful investigation returning no findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.domain.evidence import EvidenceBundle
from app.ports.interpretation import InterpretationResponse

__all__ = [
    "InterpretationRequest",
    "LlmNotConfiguredError",
    "LlmProvider",
    "LlmProviderError",
    "LlmResponseFormatError",
    "LlmTimeoutError",
]


class LlmProviderError(Exception):
    """Base class for every way a provider call can fail.

    Exists so a caller can catch "the model did not produce a usable answer" as one
    thing. That catch is the important one: it is the boundary at which a run must stop
    being allowed to describe itself as an investigation that found nothing.
    """


class LlmNotConfiguredError(LlmProviderError):
    """The provider cannot run because configuration is missing or wrong.

    Distinct from a failure because it is a deployment fault rather than a runtime one,
    and retrying cannot fix it. Raised rather than falling back to a mock, because a
    system that silently answers from a stub when credentials are absent is reporting
    results it never computed.
    """


class LlmTimeoutError(LlmProviderError):
    """The provider did not answer within the configured timeout."""


class LlmResponseFormatError(LlmProviderError):
    """The provider answered, but the answer could not be parsed into the schema.

    Carries the offending text, truncated, because the reason a schema failed is
    usually visible in the first few hundred characters and diagnosing it without them
    means guessing.
    """

    def __init__(self, message: str, *, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response

    def __str__(self) -> str:
        base = super().__str__()
        if self.raw_response is None:
            return base
        excerpt = self.raw_response[:500]
        suffix = "..." if len(self.raw_response) > len(excerpt) else ""
        return f"{base} (response began: {excerpt!r}{suffix})"


@dataclass(frozen=True)
class InterpretationRequest:
    """One request for an interpretation, fully specified.

    Everything the provider needs is here and nothing it does not: the provider receives
    evidence, the dispute text, and the schema, and has no database access, no pricing
    engine and no ability to ask a follow-up question. A provider that could reach the
    database could put unvetted source data into an arithmetic path, which is the one
    thing this design refuses to allow (NEP-02).
    """

    #: Stable identifier for the dispute, so a provider can correlate without a database.
    dispute_external_id: str
    #: The evidence, already hashed. The provider reads snapshots; it cannot alter them.
    evidence: EvidenceBundle
    #: What the customer said. Untrusted text — the prompt instructs that it is data.
    dispute_text: str
    #: The schema the response must conform to.
    response_schema: dict[str, object]
    #: Recorded on the investigation so a behaviour change is attributable (FR-016).
    prompt_version: str
    #: Instructions accompanying the schema, including the untrusted-data framing (NFR-007).
    system_instructions: str
    #: The keys a citation may name, as a snapshot at request time. Redundant with the
    #: bundle, and carried separately on purpose: the provider states what it was told it
    #: could cite, and validation checks against the bundle independently, so a provider
    #: that invents a wider allowlist cannot widen its own permissions.
    allowed_evidence_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.dispute_external_id.strip():
            raise ValueError("InterpretationRequest.dispute_external_id must not be empty")
        if not self.prompt_version.strip():
            raise ValueError("InterpretationRequest.prompt_version must not be empty")
        if not self.allowed_evidence_keys:
            # An empty allowlist means no citation could ever be valid, so every finding
            # would be rejected and the run could never succeed. Better to say so now.
            raise ValueError(
                "InterpretationRequest.allowed_evidence_keys must not be empty; without it no "
                "citation could be valid"
            )
        unknown = self.allowed_evidence_keys - self.evidence.allowed_keys
        if unknown:
            raise ValueError(
                f"allowed_evidence_keys names keys absent from the evidence bundle: "
                f"{sorted(unknown)}"
            )


@runtime_checkable
class LlmProvider(Protocol):
    """What the application requires of any provider.

    A ``Protocol`` rather than an abstract base class so an adapter needs no
    inheritance and no import from here, which is the point of depending on a port.
    ``name`` and ``model`` are recorded on every investigation (§7.2): two runs made with
    different models are not comparable, and the record is what stops that being
    forgotten.
    """

    #: Stable provider identifier, e.g. ``"mock"`` or ``"openai"``.
    name: str
    #: Model identifier, recorded for the same reason.
    model: str

    def structured_infer(self, request: InterpretationRequest) -> InterpretationResponse:
        """Interpret ``request`` and return a schema-valid response.

        Implementations must:

        * return an :class:`InterpretationResponse` that validates, with no monetary
          field set, since the schema has nowhere to put one;
        * cite only keys in ``request.allowed_evidence_keys``;
        * raise :class:`LlmNotConfiguredError`, :class:`LlmTimeoutError` or
          :class:`LlmResponseFormatError` rather than returning an empty or partial
          response to signal a problem.

        Returning a response with no findings to mean "the model failed" is specifically
        forbidden: it is indistinguishable from a real finding of nothing, and it would
        let a broken deployment look like a clean invoice.
        """
        ...
