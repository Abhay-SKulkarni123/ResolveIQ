"""The closed vocabulary of causes, severities and remedies.

Everything the model is allowed to *say* about a dispute is named here, and nothing
else is. That is deliberate: a free-text cause is unreviewable, because a reviewer can
only check a claim against something they can look up. A ``hypothesis_code`` is a
pointer into a registry, so every possible cause is visible before the model runs.

This module is vocabulary only. It deliberately contains no calculators and imports
nothing from :mod:`app.pricing`, because ``domain`` is the innermost layer and must not
know how an impact is eventually worked out (ADR-004). The mapping from a code to a
deterministic calculator lives in :mod:`app.pricing.impact`, which is allowed to import
both layers.

The codes themselves are fixed by docs/SYSTEM_DESIGN.md §6.3. Adding one is a schema
change, not a prompt change, and it must arrive with a calculator and a test.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "HYPOTHESIS_CODES",
    "MONEY_MOVING_OPTIONS",
    "FindingSeverity",
    "HypothesisCode",
    "HypothesisStatus",
    "ResolutionOptionType",
    "is_supported_hypothesis_code",
    "requires_approval",
]


class HypothesisCode(str, Enum):
    """A candidate cause, named rather than described.

    The model selects one of these and nothing else. It never supplies a rate, an
    amount or a formula for one — it says *which* rule is relevant and the deterministic
    engine decides what that rule implies (NEP-02, FR-004).
    """

    #: Usage exceeded an allowance and was billed outside the contracted tier ladder.
    OVERAGE_TIER_MISMATCH = "OVERAGE_TIER_MISMATCH"
    #: The per-unit rate applied is not the rate the contract states for that metric.
    UNIT_PRICE_MISMATCH = "UNIT_PRICE_MISMATCH"
    #: The same usage was recorded more than once; the source system's own uniqueness
    #: token collided.
    DUPLICATED_USAGE = "DUPLICATED_USAGE"
    #: Usage fell inside the invoiced period's dates but outside the period itself, or
    #: the reverse.
    OUT_OF_PERIOD_USAGE = "OUT_OF_PERIOD_USAGE"
    #: A payment exists but is not allocated to this invoice, so the balance ignores it.
    UNAPPLIED_PAYMENT = "UNAPPLIED_PAYMENT"
    #: A payment is allocated to this invoice, but not in the amount that would settle it.
    INCORRECT_ALLOCATION = "INCORRECT_ALLOCATION"
    #: The invoice's stated total is not the sum of its own lines.
    STATEMENT_TOTAL_MISMATCH = "STATEMENT_TOTAL_MISMATCH"
    #: Billed usage fell short of a contracted minimum commitment.
    COMMITMENT_SHORTFALL = "COMMITMENT_SHORTFALL"
    #: No cause can be established from the supplied evidence. Carries no impact, and is
    #: a legitimate answer rather than a failure to find one.
    UNEXPLAINED = "UNEXPLAINED"


#: Every code, as a set for membership tests. Present so a caller can validate a string
#: from an untrusted source without catching an exception, and so a test can assert the
#: enum and this set cannot drift apart.
HYPOTHESIS_CODES: frozenset[str] = frozenset(code.value for code in HypothesisCode)


def is_supported_hypothesis_code(value: str) -> bool:
    """Whether ``value`` names a cause in the closed vocabulary.

    Takes a plain ``str`` because the value being checked usually arrives as one — from
    parsed JSON, or from a provider's raw output — and the caller needs a yes/no rather
    than a ``HypothesisCode`` back.
    """
    return value in HYPOTHESIS_CODES


class FindingSeverity(str, Enum):
    """How much a finding matters, for triage and sorting.

    Advisory only. Severity never authorises anything: a ``CRITICAL`` finding does not
    create an adjustment, and ``INFO`` is not suppressed. docs/SYSTEM_DESIGN.md §8.1 is
    explicit that a reviewer's decision, not a severity label, is what gates a remedy.
    """

    #: Context worth recording; no disagreement implied.
    INFO = "INFO"
    #: Something does not reconcile and may need attention.
    WARN = "WARN"
    #: The invoice is materially wrong on the evidence supplied.
    CRITICAL = "CRITICAL"


class HypothesisStatus(str, Enum):
    """Where a hypothesis stands.

    ``OPEN`` is the only status the model may assign. ``RULED_OUT`` and ``CONFIRMED``
    exist because the lifecycle needs them and a reviewer will set them, but they are
    not the model's to claim: a model that could mark its own hypothesis confirmed could
    bypass the review step entirely, and evidence that refutes a hypothesis is the
    engine's and the reviewer's to weigh, not the model's own summary of it.
    """

    #: Proposed, not yet weighed against anything.
    OPEN = "OPEN"
    #: Evidence contradicts it.
    RULED_OUT = "RULED_OUT"
    #: Evidence and review support it.
    CONFIRMED = "CONFIRMED"


class ResolutionOptionType(str, Enum):
    """A remedy the system may *propose*.

    The model chooses which options to surface. It does not choose between them — a
    reviewer selects at most one, or none (FR-006, docs/SYSTEM_DESIGN.md §8.3).
    """

    #: Credit the whole disputed amount.
    FULL_CREDIT = "FULL_CREDIT"
    #: Credit part of the disputed amount.
    PARTIAL_CREDIT = "PARTIAL_CREDIT"
    #: Reissue the invoice at the recalculated amount.
    REBILL_CORRECT_AMOUNT = "REBILL_CORRECT_AMOUNT"
    #: Apply a payment that already exists but is unallocated.
    APPLY_UNAPPLIED_PAYMENT = "APPLY_UNAPPLIED_PAYMENT"
    #: No money moves; the invoice stands and the reasoning is explained.
    NO_ADJUSTMENT = "NO_ADJUSTMENT"
    #: Not enough evidence to proceed; more is needed.
    REQUEST_MORE_INFO = "REQUEST_MORE_INFO"


#: Options that move money. Kept as data rather than encoded in each member so that the
#: test which guards NEP-04 can assert the full set at once, and so adding a member is a
#: deliberate act that forces the question "does this one move money?" to be answered.
MONEY_MOVING_OPTIONS: frozenset[ResolutionOptionType] = frozenset(
    {
        ResolutionOptionType.FULL_CREDIT,
        ResolutionOptionType.PARTIAL_CREDIT,
        ResolutionOptionType.REBILL_CORRECT_AMOUNT,
        ResolutionOptionType.APPLY_UNAPPLIED_PAYMENT,
    }
)


def requires_approval(option: ResolutionOptionType) -> bool:
    """Whether an option of this type may only take effect on a human decision.

    True for every money-moving option, unconditionally. There is no code path in
    ResolveIQ that applies one of these without a recorded ``APPROVE`` decision (NEP-04,
    ADR-006). ``NO_ADJUSTMENT`` and ``REQUEST_MORE_INFO`` move no money, so they are
    false — which is not a weaker approval rule but the absence of one, since there is
    nothing to authorise.
    """
    return option in MONEY_MOVING_OPTIONS
