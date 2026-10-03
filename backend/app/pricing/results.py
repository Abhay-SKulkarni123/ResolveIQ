"""What a recalculation returns: an explainable result, not just a number.

FR-005 requires that every computed amount carries a calculation trace, and §5 requires
that the result be consumable by an investigation agent without that agent performing any
financial arithmetic of its own. That second point shapes this module.

A discrepancy is a result, not an explanation
---------------------------------------------
``LineRecalculation`` reports that the recalculated amount is 21.86 and the invoice
recorded 28.40. It does not report *why*. The difference might be a duplicated usage
event, a wrong tier boundary, a stale contract version, or a typo in the source system,
and deciding which is the investigation's job, not the calculator's.

Keeping the two apart matters for a specific reason: an amount that arrives with a cause
attached is harder to challenge. If the engine says "this is a duplicate-charge bug", a
reviewer has to disprove the engine's diagnosis before they can trust the arithmetic. If
it says only "these two figures differ by $6.54, and here is the step that produced
mine", the arithmetic stands on its own and the diagnosis can be argued separately.

Three statuses, because "we could not check this" is not "this was correct"
--------------------------------------------------------------------------------------
======================================  ==============================================
``RECALCULATED``                        Priced from evidence; the figures can be compared.
``ACCEPTED_AS_RECORDED``                Not independently verifiable, carried unchanged.
``UNRESOLVED``                          Evidence insufficient; no amount was produced.
======================================  ==============================================

Collapsing the last two into a single "no discrepancy" outcome would be the most
dangerous simplification available here. A ``FIXED`` line and a ``USAGE`` line with no
price term both fail to produce a recalculated amount, but only one of them is a genuine
gap in the evidence, and an invoice whose total cannot be verified must say so rather than
reporting a clean match.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.domain.billing import InvoicePeriod, LineType
from app.domain.money import Money
from app.pricing.trace import CalculationTrace

__all__ = [
    "InvoiceRecalculation",
    "LineRecalculation",
    "LineStatus",
    "UnresolvedReason",
    "UsageQuantityBreakdown",
]


class LineStatus(str, Enum):
    """How far the engine got with one line."""

    #: Priced from usage evidence and a price term. Both figures exist and are comparable.
    RECALCULATED = "RECALCULATED"
    #: A flat charge. No usage evidence can confirm or refute it, so it is carried at its
    #: recorded amount and excluded from the recalculated total. Reported rather than
    #: dropped, because dropping it would break the reconciliation against the invoice.
    ACCEPTED_AS_RECORDED = "ACCEPTED_AS_RECORDED"
    #: Evidence was insufficient. No amount was produced and none was guessed.
    UNRESOLVED = "UNRESOLVED"


class UnresolvedReason(str, Enum):
    """Why a line could not be recalculated."""

    #: No price term on the applicable contract prices this metric. Either the metric is
    #: not contracted, or the wrong contract version was supplied as evidence.
    NO_PRICE_TERM = "NO_PRICE_TERM"
    #: Billable usage runs past the end of a tier ladder that has no unbounded final tier,
    #: so no agreed rate exists for the excess.
    USAGE_EXCEEDS_TIER_LADDER = "USAGE_EXCEEDS_TIER_LADDER"


@dataclass(frozen=True)
class UsageQuantityBreakdown:
    """Which usage produced a charge, and how much of it was chargeable.

    Carried on the result so that a reviewer can see the quantity without re-deriving it
    from the evidence bundle, and so that "the rate was wrong" can be distinguished from
    "the quantity was wrong" without opening anything else.
    """

    #: Units consumed before the allowance was applied.
    total_quantity: Decimal
    #: Units the included allowance absorbed.
    included_units: Decimal
    #: Units actually charged for.
    billable_units: Decimal
    #: How many usage events contributed, after duplicates were removed.
    contributing_event_count: int
    #: Duplicate usage that was excluded, as ``dedupe_key`` identifiers.
    duplicate_dedupe_keys: tuple[str, ...]
    #: Event identifiers excluded for falling outside the invoice period.
    out_of_period_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class LineRecalculation:
    """One line: what was charged, what it should be, and how that was arrived at.

    ``calculated_amount`` and ``difference`` are ``None`` for any line that is not
    ``RECALCULATED``. Making them optional rather than defaulting them to zero is the point:
    a zero would be indistinguishable from a genuinely free charge, and the difference
    between "this line cost nothing" and "this line could not be checked" is exactly the
    distinction a dispute turns on.
    """

    metric_key: str
    line_type: LineType
    status: LineStatus
    #: What the source system charged. Always present: it is a fact, not a computation.
    recorded_amount: Money
    #: Which contract term was applied, or ``None`` when none applied.
    rule_ref: str | None = None
    #: The rate that applied, where there is a single one.
    rate: Money | None = None
    #: Usage behind the charge, for a ``USAGE`` line that was recalculated.
    usage: UsageQuantityBreakdown | None = None
    #: The unrounded product of quantity and rate(s).
    exact_amount: Money | None = None
    #: The rounding mode applied at the line boundary, recorded so a reader knows which
    #: convention produced the charged figure.
    rounding: str | None = None
    #: The recalculated charge, rounded once at the line boundary.
    calculated_amount: Money | None = None
    #: ``calculated_amount - recorded_amount``. Positive means the invoice overstated.
    difference: Money | None = None
    #: Why the line is unresolved. ``None`` for a resolved line.
    unresolved_reason: UnresolvedReason | None = None
    #: Plain-language detail on the status, for a reviewer and for the investigation agent.
    #: Deliberately not a diagnosis: it describes what was missing, not what went wrong.
    notes: tuple[str, ...] = ()
    #: The full derivation. Present for every resolved line.
    trace: CalculationTrace | None = None

    @property
    def has_discrepancy(self) -> bool:
        """Whether a recalculated line differs from what was recorded.

        Requires a difference to exist. An unresolved line has no opinion, and reporting
        ``False`` for it would let a partial recalculation read as a clean invoice.
        """
        return self.difference is not None and not self.difference.is_zero()

    @property
    def is_resolved(self) -> bool:
        return self.status is LineStatus.RECALCULATED


@dataclass(frozen=True)
class InvoiceRecalculation:
    """An invoice recalculated against its contract and usage evidence.

    ``calculated_total`` covers only the lines that could be resolved. When
    ``unresolved_metrics`` is non-empty the total is a partial figure and
    :attr:`is_complete` is ``False``; the two must be read together, which is why the
    property exists rather than leaving a caller to infer completeness from a count.
    """

    invoice_external_id: str
    period: InvoicePeriod
    currency: str
    lines: tuple[LineRecalculation, ...]
    #: Sum of the recorded line amounts. Distinct from the invoice's ``stated_total``;
    #: where they differ, that is the INV-04 reconciliation finding.
    recorded_total: Money
    #: The total the contract supports, over the lines that could be resolved.
    calculated_total: Money
    #: ``calculated_total - recorded_total``, over the lines that could be resolved. Equal to
    #: the invoice's discrepancy only when :attr:`is_complete` is True.
    difference: Money
    #: Metrics that could not be recalculated, in invoice line order.
    unresolved_metrics: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Whether every line on the invoice was recalculated.

        ``False`` means the difference covers only the lines that could be checked, so it is
        the discrepancy as far as it is known and not the whole of it. A caller must not quote
        it as the size of the disagreement: an unresolved line might have been overstated or
        understated, so the true figure can be larger in magnitude in either direction.
        """
        return not self.unresolved_metrics

    @property
    def lines_with_discrepancies(self) -> tuple[LineRecalculation, ...]:
        return tuple(line for line in self.lines if line.has_discrepancy)

    @property
    def recalculated_line_count(self) -> int:
        return sum(1 for line in self.lines if line.is_resolved)

    def line_for(self, metric_key: str) -> LineRecalculation:
        """The line for one metric.

        Raises rather than returning ``None``, because a caller reaching for a metric that
        is not on the invoice has made a mistake that should surface immediately.
        """
        for line in self.lines:
            if line.metric_key == metric_key:
                return line
        raise KeyError(f"invoice {self.invoice_external_id} has no line for {metric_key!r}")
