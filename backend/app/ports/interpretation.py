"""The wire schema the model must conform to — and the boundary it cannot cross.

docs/SYSTEM_DESIGN.md §7.3 sets out why this module looks the way it does. The
alternatives were "ask the model not to return amounts" (a prompt is advice, not a
type) and "accept amounts and overwrite them with the engine's" (the model may still
narrate an invented figure in prose, and the reviewer's eye goes to the number). So the
schema simply has no field that could hold an amount. A model that tries gets an
unknown-field error, because ``extra="forbid"`` is what turns "please don't" into "you
cannot".

That is the whole of NEP-02 at the type level, and
``tests/unit/test_interpretation_schema.py`` asserts it by walking the generated JSON
schema rather than by trusting this docstring.

Three further decisions, each of which would otherwise have to be re-argued:

**Every finding must cite at least one evidence key.** ``min_length=1`` on
``supporting_evidence`` makes "never treat an unsupported claim as verified" a
constraint on the shape of the response rather than a reviewing instruction. A finding
with nothing to point at is not a weak finding, it is an assertion.

**Inference is stored separately from observation.** ``unverified_claims`` is a
top-level field, not a property of a finding, so it cannot be rendered in the same
place as a fact. A caller that displays findings without also displaying
unverified claims has a bug the type system cannot catch, which is why the separation is
made once here and documented in §5.3 rather than left to each consumer.

**Confidence is a string on the wire.** ``confidence`` and ``likelihood`` are the
model's own estimates and are not monetary, but they are decimals, and this codebase
does not accept a binary float for a decimal for the same reason it rejects one for
money: ``Decimal(0.82)`` recovers the nearest binary approximation to ``0.82``, not
``0.82``. Requiring a string keeps the value exact across the JSON round trip and turns
a lossy conversion into a schema error the provider can see and repair.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.hypotheses import (
    FindingSeverity,
    HypothesisCode,
    HypothesisStatus,
    ResolutionOptionType,
    requires_approval,
)

__all__ = [
    "Finding",
    "Hypothesis",
    "InterpretationResponse",
    "ResolutionOption",
    "confidence_value",
]

#: Bounded prose. Upper limits exist so a runaway generation cannot inflate a row in the
#: database or blow out a prompt on the way back in, not because a long narrative is
#: necessarily a bad one.
_Narrative = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
]
_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
_Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
_EvidenceKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
_Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
_Claim = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]


def confidence_value(raw: Any) -> Decimal:
    """Parse a decimal estimate from a string or integer, refusing a float.

    Rejecting ``float`` is the point rather than an incidental strictness: the caller
    asked for exact decimals everywhere else in this system, and a float sneaking in
    through the provider's JSON is the softest boundary in the whole design. The error
    names the field so the provider can repair it rather than guess.
    """
    if isinstance(raw, bool):
        raise ValueError("a confidence must be a decimal string, not a boolean")
    if isinstance(raw, float):
        raise ValueError(
            'a confidence must be sent as a decimal string such as "0.82"; '
            "a JSON number arrives as a binary float, which is not exact"
        )
    if isinstance(raw, int):
        candidate = Decimal(raw)
    elif isinstance(raw, str):
        try:
            candidate = Decimal(raw.strip())
        except InvalidOperation as exc:
            raise ValueError(f"{raw!r} is not a decimal") from exc
    else:
        raise ValueError("a confidence must be sent as a decimal string")

    if not candidate.is_finite():
        raise ValueError("a confidence must be a finite decimal")
    if candidate < Decimal(0) or candidate > Decimal(1):
        raise ValueError(f"a confidence must be between 0 and 1, got {candidate}")
    return candidate


#: A confidence on the wire is a string; in Python it is a ``Decimal`` bounded to [0, 1].
Confidence = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]


class _Strict(BaseModel):
    """Base for every model in the response.

    ``extra="forbid"`` is the mechanism §7.3 relies on: an unknown field is an error, so
    a model attempting to return an amount is stopped by the parser. ``frozen`` because
    a validated response is evidence about what the model said, and a finding edited
    after validation would no longer be the thing that was checked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Finding(_Strict):
    """One thing that does not add up, with the evidence that shows it."""

    #: Stable, machine-comparable label within a category, e.g. ``LINE_TOTAL_MISMATCH``.
    code: _Code
    severity: FindingSeverity
    category: _Category
    narrative: _Narrative
    #: The model's own estimate, and labelled as such wherever it is displayed (§8.1).
    #: Never used to gate anything: a 0.99 finding and a 0.2 finding both need a human.
    confidence: Confidence
    #: Keys from the case bundle. At least one, enforced by the schema.
    supporting_evidence: tuple[_EvidenceKey, ...] = Field(min_length=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _parse_confidence(cls, raw: Any) -> Decimal:
        return confidence_value(raw)


class Hypothesis(_Strict):
    """A candidate cause, with what supports it and what argues against it.

    ``refuting_evidence`` is required to be present but may be empty: a model with
    nothing against a hypothesis should say so explicitly rather than omit the field and
    let absence read as an accident.
    """

    hypothesis_code: HypothesisCode
    title: _Title
    narrative: _Narrative
    supporting_evidence: tuple[_EvidenceKey, ...] = Field(min_length=1)
    refuting_evidence: tuple[_EvidenceKey, ...] = ()
    #: Which metered line this concerns, for the codes that are about one line.
    metric_key: _Code | None = None
    likelihood: Confidence
    #: Constrained to ``OPEN`` here; ``RULED_OUT`` and ``CONFIRMED`` are a reviewer's to
    #: assign, so a model cannot mark its own guess as settled.
    status: HypothesisStatus = HypothesisStatus.OPEN

    @field_validator("likelihood", mode="before")
    @classmethod
    def _parse_likelihood(cls, raw: Any) -> Decimal:
        return confidence_value(raw)

    @field_validator("status")
    @classmethod
    def _only_open_is_acceptable(cls, value: HypothesisStatus) -> HypothesisStatus:
        if value is not HypothesisStatus.OPEN:
            raise ValueError(
                "a model may only propose OPEN hypotheses; RULED_OUT and CONFIRMED are "
                "assigned by a reviewer against the evidence"
            )
        return value


class ResolutionOption(_Strict):
    """A remedy the system proposes. A reviewer picks at most one, or none (§8.3)."""

    option_type: ResolutionOptionType
    title: _Title
    rationale: _Narrative
    #: The hypothesis this option would act on, by code, so the option and the cause it
    #: addresses stay linked.
    hypothesis_code: HypothesisCode | None = None
    supporting_evidence: tuple[_EvidenceKey, ...] = ()

    @property
    def requires_approval(self) -> bool:
        """Whether this option may only take effect on a human decision.

        Derived from ``option_type`` rather than accepted from the model, so there is no
        value for it to get wrong and no field for a provider to set to ``False``. Every
        money-moving option returns ``True`` unconditionally (NEP-04).
        """
        return requires_approval(self.option_type)


class InterpretationResponse(_Strict):
    """Everything one provider call is allowed to say about one dispute."""

    #: One-paragraph account of what was found, for a reviewer skimming before reading.
    summary: _Summary
    findings: tuple[Finding, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    resolution_options: tuple[ResolutionOption, ...] = ()
    #: Statements the model marks as inference rather than observation. Returned in a
    #: field of their own so a consumer cannot render them as fact (§5.3 rule 3).
    unverified_claims: tuple[_Claim, ...] = ()

    @model_validator(mode="after")
    def _at_least_one(self) -> InterpretationResponse:
        """A response with neither findings nor hypotheses says nothing actionable.

        Refused rather than passed through as an empty result, because an empty
        investigation and a failed one look identical from outside, and only one of them
        is a finding about the invoice.

        Written as a model validator rather than one per field, because the rule is about
        the pair: a response carrying only findings is perfectly valid, and a per-field
        check would reject it. A field validator would also only fire when the field was
        passed explicitly, so ``InterpretationResponse(summary=...)`` — which relies on
        both defaults — would slip through unchecked.
        """
        if not self.findings and not self.hypotheses:
            raise ValueError(
                "a response must contain at least one finding or one hypothesis; an empty "
                "response is indistinguishable from a failed run"
            )
        return self
