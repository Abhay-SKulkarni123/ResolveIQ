"""Validating that every claim points at evidence that exists.

docs/SYSTEM_DESIGN.md §5.3 specifies this as a pure function over
``(response, allowed_keys)`` with four rules, and the last of them is the one that
matters most: **one invalid citation rejects the entire response.** Silently dropping
the offending key would let a hallucinated citation hide inside an otherwise
well-formed answer, and a reviewer reading a clean-looking finding has no way to know
something was removed. An obvious total failure is the safer outcome, and §10 (F10)'s
repair path exists to handle it.

The function is pure and takes the allowlist as data rather than reaching for a
repository, so it can be tested exhaustively without a database and cannot observe
anything but its arguments.

Every rule is checked, and *all* violations are reported together. Reporting only the
first would mean a repair loop that fixes one problem per round trip, and a provider
being told about one bad key at a time is a provider being walked into a timeout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ports.interpretation import (
    Finding,
    Hypothesis,
    InterpretationResponse,
    ResolutionOption,
)

__all__ = [
    "CitationViolation",
    "EvidenceCitationValidator",
    "InvalidCitationsError",
    "ValidatedInterpretation",
]


@dataclass(frozen=True)
class CitationViolation:
    """One citation that names a key the bundle does not contain.

    ``location`` says where in the response the citation appeared (``findings[0]``,
    ``hypotheses[1].refuting_evidence[2]``) rather than which key was wrong, because a
    provider repairing its output needs the position to edit and the key to delete.
    """

    key: str
    location: str

    def __str__(self) -> str:
        return f"{self.location} cites {self.key!r}, which is not in the evidence bundle"


@dataclass(frozen=True)
class ValidatedInterpretation:
    """A response that passed validation, with the citation set it was checked against.

    The response is returned rather than just a boolean so the caller cannot
    accidentally use an unvalidated object: obtaining this type requires having passed
    the check.
    """

    response: InterpretationResponse
    #: The keys that were allowed, retained so the investigation can record what the
    #: model was permitted to cite.
    allowed_keys: frozenset[str]
    #: Every distinct key cited across findings and hypotheses, sorted. Recorded so a
    #: reviewer can see at a glance which evidence was used and, by omission, which was
    #: collected but never referenced.
    cited_keys: frozenset[str] = field(default_factory=frozenset)


class InvalidCitationsError(ValueError):
    """Raised when a response cites evidence that is not in the bundle.

    Carries every violation rather than only the first, so a repair attempt has
    everything it needs in one pass.
    """

    def __init__(self, violations: tuple[CitationViolation, ...]) -> None:
        self.violations = violations
        detail = "; ".join(str(violation) for violation in violations)
        super().__init__(
            f"{len(violations)} citation(s) reference evidence outside the bundle, so the "
            f"whole response was rejected: {detail}"
        )


def _check_keys(
    keys: tuple[str, ...], location: str, allowed: frozenset[str]
) -> list[CitationViolation]:
    return [CitationViolation(key=key, location=location) for key in keys if key not in allowed]


def _check_finding(
    finding: Finding, index: int, allowed: frozenset[str]
) -> list[CitationViolation]:
    """Rule 2 is structural — ``min_length=1`` in the schema — and restated as a check.

    The schema already refuses a finding with no citations, so reaching this branch
    means a response was constructed rather than parsed. It is still checked, because a
    validator that assumes its input was validated is one refactor away from accepting an
    uncited claim.
    """
    location = f"findings[{index}]"
    violations = _check_keys(
        finding.supporting_evidence, f"{location}.supporting_evidence", allowed
    )
    if not finding.supporting_evidence:
        violations.append(CitationViolation(key="", location=f"{location}.supporting_evidence"))
    return violations


def _check_hypothesis(
    hypothesis: Hypothesis, index: int, allowed: frozenset[str]
) -> list[CitationViolation]:
    """Both citation lists are checked; refuting evidence too.

    A fabricated key in ``refuting_evidence`` is as much a fabrication as one in
    ``supporting_evidence``, and it is the more effective lie: evidence credited with
    ruling out a cause is an argument a reviewer is unlikely to re-derive.
    """
    location = f"hypotheses[{index}]"
    return [
        *_check_keys(
            hypothesis.supporting_evidence,
            f"{location}.supporting_evidence",
            allowed,
        ),
        *_check_keys(hypothesis.refuting_evidence, f"{location}.refuting_evidence", allowed),
    ]


def _check_resolution_option(
    option: ResolutionOption, index: int, allowed: frozenset[str]
) -> list[CitationViolation]:
    """Resolution options are checked too.

    docs/SYSTEM_DESIGN.md §5.3 enumerates the three citation-bearing fields of a
    response, and a resolution option's evidence list is a fourth. It is checked for the
    same reason: a fabricated key supporting a proposed remedy is a fabricated key, and
    the doc's list is a description of what existed when it was written rather than a
    statement that only those three fields may cite anything.
    """
    return _check_keys(
        option.supporting_evidence, f"resolution_options[{index}].supporting_evidence", allowed
    )


class EvidenceCitationValidator:
    """The §5.3 validator. Stateless, so it can be a module-level singleton."""

    def validate(
        self,
        response: InterpretationResponse,
        allowed_keys: frozenset[str],
    ) -> ValidatedInterpretation:
        """Validate ``response`` against ``allowed_keys``.

        Raises:
            InvalidCitationsError: if any citation names a key outside ``allowed_keys``,
                or if any finding cites nothing at all. The whole response is rejected;
                nothing is filtered.
        """
        violations: list[CitationViolation] = []

        for index, finding in enumerate(response.findings):
            violations.extend(_check_finding(finding, index, allowed_keys))

        for index, hypothesis in enumerate(response.hypotheses):
            violations.extend(_check_hypothesis(hypothesis, index, allowed_keys))

        for index, option in enumerate(response.resolution_options):
            violations.extend(_check_resolution_option(option, index, allowed_keys))

        if violations:
            raise InvalidCitationsError(tuple(violations))

        return ValidatedInterpretation(
            response=response,
            allowed_keys=allowed_keys,
            cited_keys=self._cited_keys(response),
        )

    @staticmethod
    def _cited_keys(response: InterpretationResponse) -> frozenset[str]:
        """Every key cited anywhere in the response."""
        keys: set[str] = set()
        for finding in response.findings:
            keys.update(finding.supporting_evidence)
        for hypothesis in response.hypotheses:
            keys.update(hypothesis.supporting_evidence)
            keys.update(hypothesis.refuting_evidence)
        return frozenset(keys)
