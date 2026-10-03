"""The response schema, and the monetary fields it does not have.

NEP-02 says financial calculations stay separate from AI interpretation, and
docs/SYSTEM_DESIGN.md §7.3 says the mechanism is that the schema has no amount field.
A mechanism that is only described in a docstring is not a mechanism, so
:func:`test_no_field_could_hold_a_monetary_amount` walks the *generated* JSON schema and
fails if any property name could carry an amount.

That test is written against generated output rather than against the Python attributes
for a specific reason: a provider is handed ``model_json_schema()``, not the class. A
constraint that held on the class but not in the emitted JSON would protect nothing at
runtime.

The confidence-as-string rule gets its own tests because it is the one place this schema
is stricter than it strictly needs to be, and a future contributor will reasonably ask
why. The answer is in the module docstring and in ``test_a_json_float_confidence_is_refused``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.hypotheses import HypothesisCode, ResolutionOptionType
from app.ports.interpretation import (
    Finding,
    Hypothesis,
    InterpretationResponse,
    ResolutionOption,
)

#: Substrings that would indicate a field able to carry money. Deliberately broad on
#: ``amount``/``total``/``price`` and narrow enough to avoid flagging a field merely
#: because it is numeric: ``confidence`` and ``likelihood`` are numbers and are not money.
MONETARY_NAME_MARKERS = (
    "amount",
    "money",
    "monetary",
    "total",
    "price",
    "cost",
    "charge",
    "charged",
    "balance",
    "credit",
    "debit",
    "currency",
    "impact",
    "outstanding",
    "subtotal",
    "refund",
    "write_off",
)


def every_property(schema: dict[str, Any], path: str = "") -> list[tuple[str, str]]:
    """Every ``(dotted_path, property_name)`` in a JSON schema, descending into $defs."""
    found: list[tuple[str, str]] = []

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name in properties:
            found.append((f"{path}.{name}" if path else name, name))

    for key in ("$defs", "definitions"):
        defs = schema.get(key)
        if isinstance(defs, dict):
            for def_name, def_schema in defs.items():
                found.extend(every_property(def_schema, f"{path}{key}.{def_name}"))

    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(every_property(items, f"{path}[]"))

    for combiner in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combiner)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    found.extend(every_property(branch, path))

    return found


class TestTheSchemaCannotCarryMoney:
    def test_no_field_could_hold_a_monetary_amount(self) -> None:
        """The whole of §7.3, asserted against the schema a provider actually receives."""
        offenders = [
            f"{path} ({name})"
            for path, name in every_property(InterpretationResponse.model_json_schema())
            if any(marker in name.lower() for marker in MONETARY_NAME_MARKERS)
        ]
        assert offenders == [], (
            "the response schema must have no field able to carry a monetary amount "
            "(NEP-02); found: " + ", ".join(offenders)
        )

    def test_returning_an_amount_is_an_unknown_field_error(self) -> None:
        """extra="forbid" is what stops it, rather than a value check.

        A value check would accept an amount under a permitted name; refusing unknown
        fields means a model attempting one is stopped by the parser, before anything
        downstream can act on it.
        """
        payload = {
            "summary": "x",
            "findings": [
                {
                    "code": "C",
                    "severity": "WARN",
                    "category": "CAT",
                    "narrative": "n",
                    "confidence": "0.5",
                    "supporting_evidence": ["k"],
                    "amount": "10.00",
                }
            ],
        }
        with pytest.raises(ValidationError, match="amount"):
            InterpretationResponse.model_validate(payload)

    def test_narrative_prose_may_mention_an_amount_while_setting_no_monetary_field(self) -> None:
        """A stated limitation, recorded so it is not mistaken for a guarantee.

        §7.3 itself notes that a model may narrate an invented amount in prose, and no
        schema can prevent that. What the structure guarantees is that such a figure
        cannot reach a calculation. Detecting a number inside narrative text is a
        different problem, and asserting otherwise here would overstate the design.
        """
        finding = Finding(
            code="C",
            severity="WARN",
            category="CAT",
            narrative="The amount charged appears inconsistent with the contracted rate.",
            confidence="0.5",
            supporting_evidence=("k",),
        )
        assert "amount" in finding.narrative
        assert (
            "amount"
            not in every_property(
                Finding.model_json_schema(),
                path="Finding",
            )[0][1]
        )

    def test_no_field_declares_a_monetary_type(self) -> None:
        """Guards the schema from being widened sideways, e.g. by a bare number field."""
        blob = json.dumps(InterpretationResponse.model_json_schema()).lower()
        assert "money" not in blob
        assert '"currency"' not in blob


class TestConfidenceIsExact:
    def test_a_decimal_string_is_accepted(self) -> None:
        finding = Finding(
            code="C",
            severity="WARN",
            category="CAT",
            narrative="n",
            confidence="0.82",
            supporting_evidence=("k",),
        )
        assert finding.confidence == Decimal("0.82")

    def test_a_json_float_confidence_is_refused(self) -> None:
        """0.82 as a JSON number is not 0.82.

        Refused rather than converted, for the reason the rest of the system refuses
        floats: ``Decimal(0.82)`` recovers the nearest binary approximation and the
        imprecision becomes invisible instead of being reported.
        """
        with pytest.raises(ValidationError, match="decimal string"):
            Finding(
                code="C",
                severity="WARN",
                category="CAT",
                narrative="n",
                confidence=0.82,  # type: ignore[arg-type]
                supporting_evidence=("k",),
            )

    @pytest.mark.parametrize("value", ["-0.1", "1.1", "2", "NaN", "Infinity", "", "abc"])
    def test_out_of_range_or_nonsense_confidence_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            Finding(
                code="C",
                severity="WARN",
                category="CAT",
                narrative="n",
                confidence=value,
                supporting_evidence=("k",),
            )

    def test_confidence_bounds_are_enforced_after_parsing(self) -> None:
        """Even if the before-validator were bypassed, the field constraint holds.

        Two independent mechanisms, deliberately: the validator produces a message naming
        the field and the reason, and the ``ge``/``le`` constraint holds for any other
        route into the model.
        """
        with pytest.raises(ValidationError):
            Finding(
                code="C",
                severity="WARN",
                category="CAT",
                narrative="n",
                confidence="1.5",
                supporting_evidence=("k",),
            )


class TestEvidenceCitationConstraints:
    def test_a_finding_with_no_citation_is_refused(self) -> None:
        """§5.3 rule 2, as a shape constraint rather than a review instruction."""
        with pytest.raises(ValidationError):
            Finding(
                code="C",
                severity="WARN",
                category="CAT",
                narrative="n",
                confidence="0.5",
                supporting_evidence=(),
            )

    def test_a_hypothesis_with_no_citation_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                hypothesis_code=HypothesisCode.UNIT_PRICE_MISMATCH,
                title="t",
                narrative="n",
                supporting_evidence=(),
                likelihood="0.5",
            )

    def test_refuting_evidence_may_be_empty(self) -> None:
        hypothesis = Hypothesis(
            hypothesis_code=HypothesisCode.UNIT_PRICE_MISMATCH,
            title="t",
            narrative="n",
            supporting_evidence=("k",),
            likelihood="0.5",
        )
        assert hypothesis.refuting_evidence == ()


class TestAModelCannotSettleItsOwnHypothesis:
    @pytest.mark.parametrize("status", ["RULED_OUT", "CONFIRMED"])
    def test_only_open_is_acceptable_from_a_provider(self, status: str) -> None:
        """§8.2: ruling a hypothesis in or out is a reviewer's act, not the model's.

        Without this, a model could mark its own guess CONFIRMED and the review step
        would become a formality over a decision already recorded as settled.
        """
        with pytest.raises(ValidationError, match="only propose OPEN"):
            Hypothesis(
                hypothesis_code=HypothesisCode.UNIT_PRICE_MISMATCH,
                title="t",
                narrative="n",
                supporting_evidence=("k",),
                likelihood="0.5",
                status=status,
            )

    def test_open_is_the_default(self) -> None:
        hypothesis = Hypothesis(
            hypothesis_code=HypothesisCode.UNEXPLAINED,
            title="t",
            narrative="n",
            supporting_evidence=("k",),
            likelihood="0.1",
        )
        assert hypothesis.status.value == "OPEN"


class TestResolutionOptions:
    @pytest.mark.parametrize(
        "option_type",
        [
            ResolutionOptionType.FULL_CREDIT,
            ResolutionOptionType.PARTIAL_CREDIT,
            ResolutionOptionType.REBILL_CORRECT_AMOUNT,
            ResolutionOptionType.APPLY_UNAPPLIED_PAYMENT,
        ],
    )
    def test_every_money_moving_option_requires_approval(self, option_type) -> None:
        option = ResolutionOption(option_type=option_type, title="t", rationale="r")
        assert option.requires_approval is True

    @pytest.mark.parametrize(
        "option_type",
        [ResolutionOptionType.NO_ADJUSTMENT, ResolutionOptionType.REQUEST_MORE_INFO],
    )
    def test_non_monetary_options_do_not(self, option_type) -> None:
        option = ResolutionOption(option_type=option_type, title="t", rationale="r")
        assert option.requires_approval is False

    def test_requires_approval_is_not_a_field_the_model_can_set(self) -> None:
        """Derived from the option type, so there is no value for a provider to get wrong.

        A field would also break the §7.3 argument: a model asserting
        ``requires_approval: false`` on a credit option is a claim about money that the
        system would have to check anyway.
        """
        assert "requires_approval" not in ResolutionOption.model_json_schema()["properties"]

    def test_a_provider_cannot_inject_it_as_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="requires_approval"):
            ResolutionOption.model_validate(
                {
                    "option_type": "FULL_CREDIT",
                    "title": "t",
                    "rationale": "r",
                    "requires_approval": False,
                }
            )


class TestResponseLevelConstraints:
    def test_a_response_with_neither_findings_nor_hypotheses_is_refused(self) -> None:
        """An empty response is indistinguishable from a failed run, so it is not accepted."""
        with pytest.raises(ValidationError, match="at least one finding"):
            InterpretationResponse(summary="nothing to say")

    def test_findings_alone_are_enough(self) -> None:
        response = InterpretationResponse(
            summary="s",
            findings=(
                Finding(
                    code="C",
                    severity="INFO",
                    category="CAT",
                    narrative="n",
                    confidence="0.1",
                    supporting_evidence=("k",),
                ),
            ),
        )
        assert response.hypotheses == ()

    def test_unknown_top_level_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            InterpretationResponse.model_validate(
                {"summary": "s", "findings": [], "hypotheses": [], "confidence": "0.9"}
            )

    def test_the_response_is_frozen(self) -> None:
        """A finding edited after validation is no longer the thing that was checked."""
        response = InterpretationResponse(
            summary="s",
            findings=(
                Finding(
                    code="C",
                    severity="INFO",
                    category="CAT",
                    narrative="n",
                    confidence="0.1",
                    supporting_evidence=("k",),
                ),
            ),
        )
        with pytest.raises(ValidationError):
            response.summary = "edited"  # type: ignore[misc]
        assert response.summary == "s"

    def test_a_blank_narrative_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Finding(
                code="C",
                severity="INFO",
                category="CAT",
                narrative="   ",
                confidence="0.1",
                supporting_evidence=("k",),
            )

    def test_unverified_claims_are_a_separate_top_level_field(self) -> None:
        """§5.3 rule 3: inference must not be shaped like fact."""
        response = InterpretationResponse.model_validate(
            {
                "summary": "s",
                "findings": [
                    {
                        "code": "C",
                        "severity": "INFO",
                        "category": "CAT",
                        "narrative": "n",
                        "confidence": "0.1",
                        "supporting_evidence": ["k"],
                    }
                ],
                "unverified_claims": ["the source system may have used another contract"],
            }
        )
        assert response.unverified_claims == ("the source system may have used another contract",)
        assert "unverified_claims" in InterpretationResponse.model_json_schema()["properties"]

    def test_hypothesis_codes_are_restricted_to_the_closed_vocabulary(self) -> None:
        with pytest.raises(ValidationError):
            InterpretationResponse.model_validate(
                {
                    "summary": "s",
                    "findings": [
                        {
                            "code": "C",
                            "severity": "INFO",
                            "category": "CAT",
                            "narrative": "n",
                            "confidence": "0.1",
                            "supporting_evidence": ["k"],
                        }
                    ],
                    "hypotheses": [
                        {
                            "hypothesis_code": "BILLING_SEEMS_WRONG",
                            "title": "t",
                            "narrative": "n",
                            "supporting_evidence": ["k"],
                            "likelihood": "0.5",
                        }
                    ],
                }
            )
