"""Citation validation: §5.3's four rules, including the strict one.

The rule this file exists to pin down is that **one bad citation rejects the whole
response**. That is a deliberate choice with a cost — a single hallucinated key loses an
entire investigation — and a cost that looks like a bug to anyone who finds it first. So
there is a test asserting the rejection is total (nothing is silently filtered), and
another asserting a fabricated key in ``refuting_evidence`` is caught as firmly as one in
``supporting_evidence``, since evidence credited with disproving a cause is the more
effective fabrication of the two.
"""

from __future__ import annotations

import pytest

from app.domain.evidence import EvidenceBundle, EvidenceItem, EvidenceType
from app.domain.hypotheses import HypothesisCode, ResolutionOptionType
from app.ports.interpretation import (
    Finding,
    Hypothesis,
    InterpretationResponse,
    ResolutionOption,
)
from app.services.citations import (
    EvidenceCitationValidator,
    InvalidCitationsError,
    ValidatedInterpretation,
)

INVOICE = "invoice:INV-1"
LINE = "invoice:INV-1#line:api_calls"
TERM = "contract:CTR-1#term:api_calls"
DISPUTE = "dispute_text:DSC-1"
FABRICATED = "invoice:INV-999999"


def bundle() -> EvidenceBundle:
    return EvidenceBundle.of(
        [
            EvidenceItem.create(INVOICE, EvidenceType.INVOICE, {"total": "28.40"}),
            EvidenceItem.create(LINE, EvidenceType.INVOICE_LINE, {"amount": "28.40"}),
            EvidenceItem.create(TERM, EvidenceType.CONTRACT_TERM, {"mode": "TIERED"}),
            EvidenceItem.create(DISPUTE, EvidenceType.DISPUTE_TEXT, {"text": "wrong rate"}),
        ]
    )


def finding(*keys: str) -> Finding:
    return Finding(
        code="C",
        severity="WARN",
        category="CAT",
        narrative="n",
        confidence="0.5",
        supporting_evidence=tuple(keys),
    )


def hypothesis(*supporting: str, refuting: tuple[str, ...] = ()) -> Hypothesis:
    return Hypothesis(
        hypothesis_code=HypothesisCode.UNIT_PRICE_MISMATCH,
        title="t",
        narrative="n",
        supporting_evidence=tuple(supporting),
        refuting_evidence=refuting,
        metric_key="api_calls",
        likelihood="0.5",
    )


def response_with(*, findings=(), hypotheses=(), options=()) -> InterpretationResponse:
    return InterpretationResponse.model_validate(
        {
            "summary": "s",
            "findings": [f.model_dump(mode="json") for f in findings],
            "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
            "resolution_options": [o.model_dump(mode="json") for o in options],
        }
    )


class TestAValidResponsePasses:
    def test_citations_within_the_bundle_are_accepted(self) -> None:
        validated = EvidenceCitationValidator().validate(
            response_with(findings=(finding(INVOICE, LINE),), hypotheses=(hypothesis(TERM),)),
            bundle().allowed_keys,
        )
        assert isinstance(validated, ValidatedInterpretation)
        assert validated.response.findings[0].code == "C"

    def test_the_allowed_keys_used_are_retained(self) -> None:
        """So the investigation can record what the model was permitted to cite."""
        keys = bundle().allowed_keys
        validated = EvidenceCitationValidator().validate(
            response_with(findings=(finding(INVOICE),)), keys
        )
        assert validated.allowed_keys == keys

    def test_cited_keys_are_reported(self) -> None:
        validated = EvidenceCitationValidator().validate(
            response_with(
                findings=(finding(INVOICE),),
                hypotheses=(hypothesis(TERM, refuting=(DISPUTE,)),),
            ),
            bundle().allowed_keys,
        )
        assert validated.cited_keys == frozenset({INVOICE, TERM, DISPUTE})


class TestOneFabricatedCitationRejectsEverything:
    def test_an_unknown_key_in_supporting_evidence_is_rejected(self) -> None:
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding(INVOICE, FABRICATED),)),
                bundle().allowed_keys,
            )

    def test_an_unknown_key_in_refuting_evidence_is_rejected(self) -> None:
        """A fabricated key credited with disproving a cause is still a fabrication."""
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(hypotheses=(hypothesis(INVOICE, refuting=(FABRICATED,)),)),
                bundle().allowed_keys,
            )

    def test_an_unknown_key_in_a_resolution_option_is_rejected(self) -> None:
        option = ResolutionOption(
            option_type=ResolutionOptionType.NO_ADJUSTMENT,
            title="t",
            rationale="r",
            supporting_evidence=(FABRICATED,),
        )
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding(INVOICE),), options=(option,)),
                bundle().allowed_keys,
            )

    def test_nothing_is_silently_filtered(self) -> None:
        """The valid findings in the same response are discarded too, not kept.

        Asserted directly because "drop the bad key and return the rest" is the intuitive
        implementation and would pass every other test here.
        """
        response = response_with(
            findings=(finding(INVOICE), finding(FABRICATED)),
            hypotheses=(hypothesis(TERM),),
        )
        with pytest.raises(InvalidCitationsError) as caught:
            EvidenceCitationValidator().validate(response, bundle().allowed_keys)
        assert len(caught.value.violations) == 1

    def test_a_near_miss_key_is_still_rejected(self) -> None:
        """Citation matching is exact. A close key is a hallucination, not a typo to forgive."""
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding("invoice:INV-1#line:api_call"),)),
                bundle().allowed_keys,
            )

    def test_a_row_id_is_not_a_natural_key(self) -> None:
        """§5.1: findings cite natural keys, never database ids."""
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding("42"),)), bundle().allowed_keys
            )


class TestEveryViolationIsReportedAtOnce:
    def test_multiple_bad_keys_are_all_listed(self) -> None:
        """A repair pass gets everything it needs in one round trip.

        Reporting only the first would mean walking the provider towards a timeout one
        key at a time.
        """
        with pytest.raises(InvalidCitationsError) as caught:
            EvidenceCitationValidator().validate(
                response_with(
                    findings=(finding("bogus:1"),),
                    hypotheses=(hypothesis("bogus:2", refuting=("bogus:3",)),),
                ),
                bundle().allowed_keys,
            )
        assert len(caught.value.violations) == 3

    def test_each_violation_names_its_position(self) -> None:
        """So a provider can edit the right list rather than guess."""
        with pytest.raises(InvalidCitationsError) as caught:
            EvidenceCitationValidator().validate(
                response_with(
                    findings=(finding(INVOICE), finding("bogus:1")),
                    hypotheses=(hypothesis("bogus:2"),),
                ),
                bundle().allowed_keys,
            )
        locations = {violation.location for violation in caught.value.violations}
        assert locations == {"findings[1].supporting_evidence", "hypotheses[0].supporting_evidence"}

    def test_the_message_says_the_whole_response_was_rejected(self) -> None:
        with pytest.raises(InvalidCitationsError, match="whole response was rejected"):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding("bogus:1"),)), bundle().allowed_keys
            )


class TestAnEmptyAllowlistRejectsEverything:
    def test_no_citation_can_be_valid(self) -> None:
        """Defence in depth: the workflow cannot build such a request, and if one arrives
        the validator still refuses it rather than waving everything through."""
        with pytest.raises(InvalidCitationsError):
            EvidenceCitationValidator().validate(
                response_with(findings=(finding(INVOICE),)), frozenset()
            )
