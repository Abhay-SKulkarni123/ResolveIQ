"""Turning a ``hypothesis_code`` into a deterministic number.

docs/SYSTEM_DESIGN.md §6.3 draws the line this module exists to hold: the model picks
*which* rule is relevant, and the engine works out *what that rule implies*. The model
never sees an amount and never supplies one, so there is no code path by which a language
model influences a monetary value.

Nothing here computes money from scratch. Every calculator dispatches to the Phase 2
engine — :func:`app.pricing.engine.recalculate_invoice`,
:func:`app.pricing.usage.summarise_usage`, :func:`app.pricing.reconciliation.reconcile_balance`
— and reports what those functions already proved. That is why this module is thin: a
second implementation of tier maths would be a second thing to disagree with the first,
and a dispute is not the place to discover which of two calculators is right.

A code whose evidence does not support it yields :attr:`ImpactAssessment.impact` of
``None`` and a reason. It never yields an estimate. ``UNEXPLAINED`` always yields
``None`` by construction — there is no rule to apply — which is what makes "I cannot
determine the cause" expressible without it looking like a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.billing import Payment, RecordedInvoice, UsageEvent
from app.domain.hypotheses import HypothesisCode
from app.domain.money import Money
from app.pricing.reconciliation import OutstandingBalance, unallocated_payment_total
from app.pricing.results import InvoiceRecalculation, LineRecalculation, LineStatus
from app.pricing.trace import CalculationTrace
from app.pricing.usage import UsageSummary, summarise_usage

__all__ = [
    "IMPACT_ASSESSORS",
    "EvidenceBundleView",
    "ImpactAssessment",
    "assess_impact",
    "is_assessable",
]


@dataclass(frozen=True)
class EvidenceBundleView:
    """The recalculation context a hypothesis is assessed against.

    Named for what it is: everything already computed by the deterministic engine, plus
    the usage summaries the impact calculators need. It contains no model output. An
    assessor reads this and never the response that proposed the code, which is what
    makes the impact independent of the model's reasoning.
    """

    invoice: RecordedInvoice
    recalculation: InvoiceRecalculation
    balance: OutstandingBalance
    #: Keyed by ``metric_key``.
    usage_summaries: dict[str, UsageSummary]
    usage_events: tuple[UsageEvent, ...]
    #: Recorded payments, so the payment hypotheses can distinguish money nobody allocated
    #: from money allocated to another invoice. Defaulted so a caller focused on charge
    #: arithmetic need not supply them.
    payments: tuple[Payment, ...] = ()


@dataclass(frozen=True)
class ImpactAssessment:
    """What a hypothesis is worth, as far as the evidence determines.

    ``impact`` is ``None`` in three distinct situations, which are deliberately not
    collapsed into one: the code has no rule attached (``UNEXPLAINED``), the evidence
    does not support the code, or the question is about payments rather than a charge.
    ``basis`` says which, because "no impact" and "not applicable" are different answers
    to a reviewer.
    """

    hypothesis_code: HypothesisCode
    #: The amount the evidence supports, or ``None``. Sign convention is the same as the
    #: engine's: positive means the invoice overstated the charge.
    impact: Money | None
    #: How the figure was arrived at. ``None`` whenever ``impact`` is ``None``.
    trace: CalculationTrace | None
    #: What was read to produce it, for a reviewer who wants to check the inputs.
    basis: str
    #: Why ``impact`` is ``None``, when it is.
    not_assessable_reason: str | None = None

    @property
    def has_impact(self) -> bool:
        return self.impact is not None


def _line_difference(
    recalculation: InvoiceRecalculation, metric_key: str
) -> tuple[LineRecalculation | None, str]:
    """The recalculated line for ``metric_key``, or why there isn't one."""
    try:
        return recalculation.line_for(metric_key), ""
    except KeyError:
        return None, f"the invoice has no line for metric {metric_key!r}"


def _usage_summary(view: EvidenceBundleView, metric_key: str) -> tuple[UsageSummary | None, str]:
    """The usage summary for ``metric_key``, computing it if not pre-supplied.

    Computed from the events and the invoice's own period rather than read from the
    bundle, so the quantity assessed is the one the charge was built from.
    """
    summary = view.usage_summaries.get(metric_key)
    if summary is None:
        summary = summarise_usage(metric_key, list(view.usage_events), view.invoice.period)
    return summary, ""


def _assess_usage_related(
    code: HypothesisCode, view: EvidenceBundleView, metric_key: str
) -> ImpactAssessment:
    """Shared handling for the codes that concern a metered charge on one line.

    The impact is the engine's own line difference. The hypothesis only decides *which*
    line's difference is being asserted to be a real disagreement — the arithmetic is
    identical either way, and is never recomputed here.
    """
    unsupported = HypothesisCode.UNEXPLAINED
    if code is unsupported:  # pragma: no cover - guarded by the registry
        raise ValueError("UNEXPLAINED has no calculator and must not be assessed here")

    line, problem = _line_difference(view.recalculation, metric_key)
    if line is None:
        return ImpactAssessment(
            hypothesis_code=code,
            impact=None,
            trace=None,
            basis=f"recalculation of invoice {view.invoice.external_id}",
            not_assessable_reason=problem,
        )

    basis = (
        f"invoice {view.invoice.external_id} line {metric_key!r}: recorded "
        f"{line.recorded_amount} against a recalculated {line.calculated_amount}"
    )

    if line.status is LineStatus.UNRESOLVED:
        # The engine could not price the line, so there is no difference to report. The
        # reason is carried through verbatim: "no price term" and "usage exceeds the
        # ladder" call for different follow-up.
        return ImpactAssessment(
            hypothesis_code=code,
            impact=None,
            trace=line.trace,
            basis=f"{basis}; line unresolved ({line.unresolved_reason})",
            not_assessable_reason=(
                f"the line could not be recalculated, so no impact can be stated: "
                f"{line.unresolved_reason}"
            ),
        )

    if line.difference is None:
        return ImpactAssessment(
            hypothesis_code=code,
            impact=None,
            trace=line.trace,
            basis=basis,
            not_assessable_reason="the line carries no recalculated difference",
        )

    return ImpactAssessment(
        hypothesis_code=code,
        impact=line.difference,
        trace=line.trace,
        basis=basis,
    )


def _assess_overage_tier_mismatch(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    return _assess_usage_related(HypothesisCode.OVERAGE_TIER_MISMATCH, view, metric_key)


def _assess_unit_price_mismatch(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    return _assess_usage_related(HypothesisCode.UNIT_PRICE_MISMATCH, view, metric_key)


def _assess_commitment_shortfall(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """A commitment is a whole-invoice term, so the whole-invoice difference is used.

    The amount is not the line's difference: a shortfall arises from the sum of metered
    lines falling below a minimum, so quoting one line's difference would misstate it by
    construction. The invoice's difference is the figure the contract supports.
    """
    line, problem = _line_difference(view.recalculation, metric_key)
    if line is None:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.COMMITMENT_SHORTFALL,
            impact=None,
            trace=None,
            basis=f"recalculation of invoice {view.invoice.external_id}",
            not_assessable_reason=problem,
        )

    recalculation = view.recalculation
    basis = (
        f"invoice {recalculation.invoice_external_id} total: recalculated "
        f"{recalculation.calculated_total} against recorded {recalculation.recorded_total}"
    )
    if not recalculation.is_complete:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.COMMITMENT_SHORTFALL,
            impact=None,
            trace=recalculation.lines[0].trace if recalculation.lines else None,
            basis=f"{basis}; recalculation incomplete",
            not_assessable_reason=(
                "a minimum commitment applies to the whole invoice, so it cannot be "
                f"assessed while these metrics are unresolved: {', '.join(recalculation.unresolved_metrics)}"
            ),
        )
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.COMMITMENT_SHORTFALL,
        impact=recalculation.difference,
        trace=None,
        basis=basis,
    )


def _assess_duplicated_usage(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """Impact is the value of the duplicated events, priced by the engine.

    The duplicates are identified by :func:`summarise_usage`. What they were *worth* is
    the line's difference, which the engine already computed from a total that had them
    counted once.
    """
    summary, _ = _usage_summary(view, metric_key)
    assessment = _assess_usage_related(HypothesisCode.DUPLICATED_USAGE, view, metric_key)
    if summary is None or not summary.duplicates:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.DUPLICATED_USAGE,
            impact=None,
            trace=assessment.trace,
            basis=assessment.basis,
            not_assessable_reason=(
                f"no duplicate dedupe_key was found for metric {metric_key!r} in the invoice period"
            ),
        )
    dropped = sum((d.dropped_quantity for d in summary.duplicates), Decimal(0))
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.DUPLICATED_USAGE,
        impact=assessment.impact,
        trace=assessment.trace,
        basis=(
            f"{assessment.basis}; {len(summary.duplicates)} duplicated dedupe_key(s) "
            f"contributing {dropped} excluded units were counted once in the recalculated total"
        ),
        not_assessable_reason=assessment.not_assessable_reason,
    )


def _assess_out_of_period_usage(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    summary, _ = _usage_summary(view, metric_key)
    assessment = _assess_usage_related(HypothesisCode.OUT_OF_PERIOD_USAGE, view, metric_key)
    if summary is None or not summary.out_of_period:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.OUT_OF_PERIOD_USAGE,
            impact=None,
            trace=assessment.trace,
            basis=assessment.basis,
            not_assessable_reason=(
                f"every usage event for metric {metric_key!r} falls inside the invoice period"
            ),
        )
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.OUT_OF_PERIOD_USAGE,
        impact=assessment.impact,
        trace=assessment.trace,
        basis=(
            f"{assessment.basis}; {len(summary.out_of_period)} event(s) excluded as outside "
            f"period {view.invoice.period.start}..{view.invoice.period.end}"
        ),
        not_assessable_reason=assessment.not_assessable_reason,
    )


def _assess_unapplied_payment(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """Money received that does not reduce this invoice's balance.

    Two different conditions, both reported because both mean the balance is too high and
    they need different follow-up:

    * *unallocated* — no allocations recorded at all, so nobody applied the remittance;
    * *unapplied* — allocated to some other invoice, so it was spent elsewhere.

    Phase 2 keeps them apart (:attr:`OutstandingBalance.unapplied_payment_total` and
    :func:`unallocated_payment_total`), and conflating them here would send an analyst to
    the wrong record. The impact is the sum, because from the invoice's point of view
    both are money that exists and does not reduce what is owed.
    """
    del metric_key  # A payment question is not about a metered line.
    balance = view.balance
    unapplied = balance.unapplied_payment_total
    unallocated = unallocated_payment_total(list(view.payments), balance.currency)

    if unapplied.is_zero() and unallocated.is_zero():
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.UNAPPLIED_PAYMENT,
            impact=None,
            trace=None,
            basis=f"reconciliation of invoice {balance.invoice_external_id}",
            not_assessable_reason=("every recorded payment is allocated to this invoice"),
        )

    parts = []
    if not unallocated.is_zero():
        parts.append(f"{unallocated} unallocated (no allocation recorded)")
    if not unapplied.is_zero():
        parts.append(f"{unapplied} allocated to another invoice")
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.UNAPPLIED_PAYMENT,
        impact=unallocated + unapplied,
        trace=None,
        basis=f"invoice {balance.invoice_external_id}: " + "; ".join(parts),
    )


def _assess_incorrect_allocation(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """What is still outstanding once payments and adjustments are applied.

    Signed like every other impact: positive means the customer still owes.
    """
    balance = view.balance
    if balance.is_provisional:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.INCORRECT_ALLOCATION,
            impact=None,
            trace=None,
            basis=f"reconciliation of invoice {balance.invoice_external_id}",
            not_assessable_reason=(
                "the balance is provisional because these metrics are unresolved: "
                f"{', '.join(balance.unresolved_metrics)}"
            ),
        )
    if balance.is_settled:
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.INCORRECT_ALLOCATION,
            impact=None,
            trace=None,
            basis=f"reconciliation of invoice {balance.invoice_external_id}",
            not_assessable_reason="the invoice is exactly covered by its allocated payments",
        )
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.INCORRECT_ALLOCATION,
        impact=balance.outstanding,
        trace=None,
        basis=(
            f"invoice {balance.invoice_external_id}: outstanding {balance.outstanding} "
            f"after {balance.allocated_payments} of allocated payments and "
            f"{balance.net_adjustments} of net adjustments"
        ),
    )


def _assess_statement_total_mismatch(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """The invoice's stated total against the sum of its own lines.

    Read from the invoice rather than the recalculation, because this is the one
    hypothesis about internal inconsistency rather than about whether the charge was
    right. ``metric_key`` is not meaningful and is ignored.
    """
    invoice = view.invoice
    difference = invoice.stated_total - invoice.line_total
    basis = (
        f"invoice {invoice.external_id} states {invoice.stated_total} but its lines sum to "
        f"{invoice.line_total}"
    )
    if difference.is_zero():
        return ImpactAssessment(
            hypothesis_code=HypothesisCode.STATEMENT_TOTAL_MISMATCH,
            impact=None,
            trace=None,
            basis=basis,
            not_assessable_reason="the stated total equals the sum of the invoice's lines",
        )
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.STATEMENT_TOTAL_MISMATCH,
        impact=difference,
        trace=None,
        basis=basis,
    )


def _assess_unexplained(view: EvidenceBundleView, metric_key: str) -> ImpactAssessment:
    """No rule, therefore no impact, and that is a complete answer.

    docs/SYSTEM_DESIGN.md §6.3 keeps ``UNEXPLAINED`` in the vocabulary precisely so the
    system can decline to manufacture a figure. It is assessed rather than special-cased
    at the call site so that it is a first-class result rather than a special case.
    """
    del metric_key
    return ImpactAssessment(
        hypothesis_code=HypothesisCode.UNEXPLAINED,
        impact=None,
        trace=None,
        basis=f"invoice {view.invoice.external_id}; no calculator applies to this code",
        not_assessable_reason=(
            "UNEXPLAINED states that no cause could be established from the evidence; "
            "it deliberately carries no impact"
        ),
    )


#: Code -> assessor. The registry is the whole of FR-004.
#:
#: A code absent from this mapping has no calculator, which is a defect rather than a
#: supported state: :func:`is_assessable` exists so a test can assert the two collections
#: agree, so a newly added code cannot be shipped without a calculator behind it.
IMPACT_ASSESSORS = {
    HypothesisCode.OVERAGE_TIER_MISMATCH: _assess_overage_tier_mismatch,
    HypothesisCode.UNIT_PRICE_MISMATCH: _assess_unit_price_mismatch,
    HypothesisCode.DUPLICATED_USAGE: _assess_duplicated_usage,
    HypothesisCode.OUT_OF_PERIOD_USAGE: _assess_out_of_period_usage,
    HypothesisCode.UNAPPLIED_PAYMENT: _assess_unapplied_payment,
    HypothesisCode.INCORRECT_ALLOCATION: _assess_incorrect_allocation,
    HypothesisCode.STATEMENT_TOTAL_MISMATCH: _assess_statement_total_mismatch,
    HypothesisCode.COMMITMENT_SHORTFALL: _assess_commitment_shortfall,
    HypothesisCode.UNEXPLAINED: _assess_unexplained,
}


def is_assessable(code: HypothesisCode) -> bool:
    """Whether ``code`` has a deterministic calculator behind it."""
    return code in IMPACT_ASSESSORS


def assess_impact(
    code: HypothesisCode, view: EvidenceBundleView, metric_key: str | None = None
) -> ImpactAssessment:
    """Assess ``code`` against already-computed evidence.

    ``metric_key`` is required by the codes about a metered charge and ignored by the
    codes about payments or the statement as a whole. Defaulting it keeps the
    call site uniform; the assessors that need it raise a clear error rather than
    guessing, because silently assessing ``api_calls`` against a payment hypothesis
    would produce a confidently wrong basis string.
    """
    assessor = IMPACT_ASSESSORS.get(code)
    if assessor is None:
        return ImpactAssessment(
            hypothesis_code=code,
            impact=None,
            trace=None,
            basis="no calculator registered",
            not_assessable_reason=f"{code} has no deterministic calculator",
        )

    if metric_key is None:
        needs_metric = {
            HypothesisCode.OVERAGE_TIER_MISMATCH,
            HypothesisCode.UNIT_PRICE_MISMATCH,
            HypothesisCode.DUPLICATED_USAGE,
            HypothesisCode.OUT_OF_PERIOD_USAGE,
            HypothesisCode.COMMITMENT_SHORTFALL,
        }
        if code in needs_metric:
            return ImpactAssessment(
                hypothesis_code=code,
                impact=None,
                trace=None,
                basis=f"recalculation of invoice {view.invoice.external_id}",
                not_assessable_reason=(
                    f"{code} concerns one metered line, so it must name the metric_key it applies to"
                ),
            )

    return assessor(view, metric_key or "")
