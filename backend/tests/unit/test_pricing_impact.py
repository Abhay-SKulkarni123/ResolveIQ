"""The impact registry: FR-004 and the codes of §6.3.

The central claim these tests defend is that the registry *dispatches* rather than
reimplements. Every calculator reports a figure the Phase 2 engine already produced, so
the strongest available check is that the impact equals the engine's own figure for the
same input — computed independently in the test from ``recalculate_invoice`` rather than
read back from the same call the assessor made.

``UNEXPLAINED`` gets particular attention. It is the code that lets the system say "I
cannot determine the cause" without manufacturing a figure, so the tests assert it yields
no impact for every kind of input, including input that would support some other code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.billing import (
    Adjustment,
    InvoiceLine,
    InvoicePeriod,
    LineType,
    Payment,
    PaymentAllocation,
    PriceTerm,
    RecordedInvoice,
    Tier,
    UsageEvent,
)
from app.domain.contracts import BillingMode
from app.domain.hypotheses import (
    HYPOTHESIS_CODES,
    MONEY_MOVING_OPTIONS,
    HypothesisCode,
    ResolutionOptionType,
    requires_approval,
)
from app.domain.money import Money
from app.pricing.engine import recalculate_invoice
from app.pricing.impact import (
    IMPACT_ASSESSORS,
    EvidenceBundleView,
    assess_impact,
    is_assessable,
)
from app.pricing.reconciliation import reconcile_balance
from app.pricing.usage import summarise_usage

CONTRACT = "CTR-5512"
INVOICE_ID = "INV-2026-03-0042"
PERIOD = InvoicePeriod(date(2026, 3, 1), date(2026, 4, 1))


def usd(text: str) -> Money:
    return Money.parse(text, "USD")


def tiered_term() -> PriceTerm:
    """A ladder whose recalculated total is deliberately different from what was billed."""
    return PriceTerm(
        "api_calls",
        BillingMode.TIERED,
        "USD",
        tiers=(
            Tier(up_to=Decimal("10000"), unit_price=usd("0.50")),
            Tier(up_to=Decimal("50000"), unit_price=usd("0.0007")),
        ),
    )


def invoice(
    *, stated: str = "28.40", lines: tuple[tuple[str, LineType, str], ...] | None = None
) -> RecordedInvoice:
    built = lines or (("api_calls", LineType.USAGE, "28.40"),)
    return RecordedInvoice(
        external_id=INVOICE_ID,
        period=PERIOD,
        currency="USD",
        lines=tuple(InvoiceLine(key, kind, usd(amount)) for key, kind, amount in built),
        stated_total=usd(stated),
    )


def events(*specs: tuple[str, str, str, str]) -> list[UsageEvent]:
    """``(external_id, dedupe_key, quantity, day)`` tuples, all inside the period."""
    return [
        UsageEvent(
            external_id=external_id,
            dedupe_key=dedupe_key,
            metric_key="api_calls",
            occurred_at=datetime(2026, 3, int(day), 9, tzinfo=timezone.utc),
            quantity=Decimal(quantity),
            unit="calls",
        )
        for external_id, dedupe_key, quantity, day in specs
    ]


def view_for(
    recorded: RecordedInvoice | None = None,
    *,
    terms: list[PriceTerm] | None = None,
    usage: list[UsageEvent] | None = None,
    payments: list[Payment] | None = None,
    adjustments: list[Adjustment] | None = None,
) -> EvidenceBundleView:
    recorded = recorded or invoice()
    terms = terms if terms is not None else [tiered_term()]
    usage = usage if usage is not None else events(("e1", "DK-1", "41234", "15"))
    recalculation = recalculate_invoice(recorded, terms, usage, contract_external_id=CONTRACT)
    balance = reconcile_balance(recalculation, payments or [], adjustments or [])
    return EvidenceBundleView(
        invoice=recorded,
        recalculation=recalculation,
        balance=balance,
        usage_summaries={"api_calls": summarise_usage("api_calls", usage, recorded.period)},
        usage_events=tuple(usage),
        payments=tuple(payments or []),
    )


class TestTheRegistryCoversTheClosedVocabulary:
    def test_every_hypothesis_code_has_a_calculator(self) -> None:
        """A code with no calculator is a defect, not a supported state.

        Without this, adding a code to the enum would silently produce an assessment
        with no impact and no explanation of why.
        """
        missing = [code for code in HypothesisCode if not is_assessable(code)]
        assert missing == []

    def test_the_registry_has_no_calculator_without_a_code(self) -> None:
        assert set(IMPACT_ASSESSORS) == set(HypothesisCode)

    def test_the_vocabulary_is_exactly_the_nine_documented_codes(self) -> None:
        """§6.3 fixes the list. A test on the exact set makes an accidental addition visible."""
        assert (
            frozenset(
                {
                    "OVERAGE_TIER_MISMATCH",
                    "UNIT_PRICE_MISMATCH",
                    "DUPLICATED_USAGE",
                    "OUT_OF_PERIOD_USAGE",
                    "UNAPPLIED_PAYMENT",
                    "INCORRECT_ALLOCATION",
                    "STATEMENT_TOTAL_MISMATCH",
                    "COMMITMENT_SHORTFALL",
                    "UNEXPLAINED",
                }
            )
            == HYPOTHESIS_CODES
        )

    def test_the_frozenset_and_the_enum_cannot_drift(self) -> None:
        assert frozenset(code.value for code in HypothesisCode) == HYPOTHESIS_CODES


class TestUsageRelatedCodesReportTheEnginesOwnDifference:
    """The registry must not do arithmetic of its own.

    Each assertion recomputes the expected figure straight from ``recalculate_invoice``,
    so a registry that quietly reimplemented tier maths with a different rounding rule
    would fail here.
    """

    @pytest.mark.parametrize(
        "code",
        [
            HypothesisCode.OVERAGE_TIER_MISMATCH,
            HypothesisCode.UNIT_PRICE_MISMATCH,
        ],
    )
    def test_impact_equals_the_line_difference(self, code: HypothesisCode) -> None:
        view = view_for()
        expected = (
            recalculate_invoice(
                view.invoice,
                [tiered_term()],
                events(("e1", "DK-1", "41234", "15")),
                contract_external_id=CONTRACT,
            )
            .line_for("api_calls")
            .difference
        )

        assessment = assess_impact(code, view, "api_calls")

        assert assessment.impact == expected

    def test_a_per_unit_term_is_priced_by_the_same_path(self) -> None:
        term = PriceTerm("api_calls", BillingMode.PER_UNIT, "USD", unit_price=usd("0.0007"))
        view = view_for(terms=[term])
        assessment = assess_impact(HypothesisCode.UNIT_PRICE_MISMATCH, view, "api_calls")
        assert assessment.impact == view.recalculation.line_for("api_calls").difference

    def test_the_trace_is_carried_through_for_audit(self) -> None:
        """§6.4: a number a reviewer cannot trace is a number they must trust blindly."""
        assessment = assess_impact(HypothesisCode.OVERAGE_TIER_MISMATCH, view_for(), "api_calls")
        assert assessment.trace is not None
        assert assessment.trace.rule_ref is not None


class TestDuplicateAndOutOfPeriodNeedTheirAnomaly:
    def test_duplicated_usage_reports_no_impact_when_nothing_was_duplicated(self) -> None:
        assessment = assess_impact(HypothesisCode.DUPLICATED_USAGE, view_for(), "api_calls")
        assert assessment.impact is None
        assert "duplicate" in (assessment.not_assessable_reason or "").lower()

    def test_duplicated_usage_is_reported_when_a_key_collides(self) -> None:
        """Two events on one dedupe_key are the same usage recorded twice."""
        usage = events(
            ("e1", "DK-1", "20000", "10"),
            ("e2", "DK-1", "20000", "11"),
        )
        view = view_for(usage=usage)
        assert view.usage_summaries["api_calls"].duplicates

        assessment = assess_impact(HypothesisCode.DUPLICATED_USAGE, view, "api_calls")
        assert assessment.impact is not None
        assert "duplicated" in assessment.basis.lower()

    def test_out_of_period_usage_reports_no_impact_when_everything_is_in_period(self) -> None:
        assessment = assess_impact(HypothesisCode.OUT_OF_PERIOD_USAGE, view_for(), "api_calls")
        assert assessment.impact is None
        assert "inside the invoice period" in (assessment.not_assessable_reason or "")

    def test_out_of_period_usage_is_reported_when_an_event_falls_outside(self) -> None:
        outside = UsageEvent(
            external_id="e9",
            dedupe_key="DK-9",
            metric_key="api_calls",
            occurred_at=datetime(2026, 4, 5, 9, tzinfo=timezone.utc),
            quantity=Decimal("500"),
            unit="calls",
        )
        view = view_for(usage=[*events(("e1", "DK-1", "41234", "15")), outside])

        assessment = assess_impact(HypothesisCode.OUT_OF_PERIOD_USAGE, view, "api_calls")
        assert assessment.impact is not None
        assert "outside period" in assessment.basis.lower()


class TestPaymentAndStatementCodes:
    def test_unallocated_money_is_reported(self) -> None:
        """Nobody applied the remittance at all — the most common real case."""
        payment = Payment("PAY-1", usd("20.00"), ())
        view = view_for(payments=[payment])

        assessment = assess_impact(HypothesisCode.UNAPPLIED_PAYMENT, view)

        assert assessment.impact == usd("20.00")
        assert "unallocated" in assessment.basis

    def test_money_allocated_to_another_invoice_is_reported(self) -> None:
        """A distinct condition: the money exists but was spent elsewhere."""
        payment = Payment("PAY-1", usd("20.00"), (PaymentAllocation("INV-OTHER", usd("20.00")),))
        view = view_for(payments=[payment])
        assert view.balance.unapplied_payment_total == usd("20.00")

        assessment = assess_impact(HypothesisCode.UNAPPLIED_PAYMENT, view)

        assert assessment.impact == usd("20.00")
        assert "another invoice" in assessment.basis

    def test_both_conditions_are_summed_and_named_separately(self) -> None:
        """Conflating them would send an analyst to the wrong record."""
        payments = [
            Payment("PAY-1", usd("10.00"), ()),
            Payment("PAY-2", usd("5.00"), (PaymentAllocation("INV-OTHER", usd("5.00")),)),
        ]
        view = view_for(payments=payments)

        assessment = assess_impact(HypothesisCode.UNAPPLIED_PAYMENT, view)

        assert assessment.impact == usd("15.00")
        assert "unallocated" in assessment.basis
        assert "another invoice" in assessment.basis

    def test_unallocated_money_does_not_reduce_the_balance(self) -> None:
        """Phase 2's rule: only an explicit allocation reduces a balance."""
        payment = Payment("PAY-1", usd("20.00"), ())
        view = view_for(payments=[payment])
        assert view.balance.allocated_payments.is_zero()

    def test_unapplied_payment_is_silent_when_every_payment_is_allocated_here(self) -> None:
        payment = Payment(
            "PAY-1",
            usd("20.00"),
            (PaymentAllocation(INVOICE_ID, usd("20.00")),),
        )
        assessment = assess_impact(HypothesisCode.UNAPPLIED_PAYMENT, view_for(payments=[payment]))
        assert assessment.impact is None

    def test_incorrect_allocation_reports_the_outstanding_balance(self) -> None:
        view = view_for()
        assessment = assess_impact(HypothesisCode.INCORRECT_ALLOCATION, view)
        assert assessment.impact == view.balance.outstanding

    def test_incorrect_allocation_is_silent_when_the_invoice_is_settled(self) -> None:
        total = view_for().recalculation.calculated_total
        payment = Payment("PAY-1", total, (PaymentAllocation(INVOICE_ID, total),))
        view = view_for(payments=[payment])
        assert view.balance.is_settled

        assessment = assess_impact(HypothesisCode.INCORRECT_ALLOCATION, view)
        assert assessment.impact is None

    def test_statement_total_mismatch_compares_the_stated_total_to_the_lines(self) -> None:
        """§3.2 / INV-04: the stated total is kept distinct from the sum of the lines."""
        recorded = invoice(stated="30.00")  # lines still sum to 28.40
        view = view_for(recorded)
        assessment = assess_impact(HypothesisCode.STATEMENT_TOTAL_MISMATCH, view)
        assert assessment.impact == usd("1.60")

    def test_statement_total_mismatch_is_silent_when_the_statement_adds_up(self) -> None:
        assessment = assess_impact(HypothesisCode.STATEMENT_TOTAL_MISMATCH, view_for())
        assert assessment.impact is None
        assert "equals the sum" in (assessment.not_assessable_reason or "")

    def test_commitment_shortfall_uses_the_whole_invoice_difference(self) -> None:
        """Not the line's difference: a commitment applies to the invoice as a whole."""
        view = view_for()
        assessment = assess_impact(HypothesisCode.COMMITMENT_SHORTFALL, view, "api_calls")
        assert assessment.impact == view.recalculation.difference


class TestUnexplainedNeverCarriesAnImpact:
    def test_it_has_no_impact_for_ordinary_input(self) -> None:
        assessment = assess_impact(HypothesisCode.UNEXPLAINED, view_for(), "api_calls")
        assert assessment.impact is None

    def test_it_has_no_impact_even_when_evidence_would_support_another_code(self) -> None:
        """The most important case: a clear discrepancy must not tempt it into a number."""
        view = view_for()
        assert view.recalculation.difference != Money.zero("USD")

        assessment = assess_impact(HypothesisCode.UNEXPLAINED, view, "api_calls")

        assert assessment.impact is None
        assert assessment.trace is None

    def test_it_explains_itself_rather_than_being_silent(self) -> None:
        """ "No impact" and "not applicable" are different answers to a reviewer."""
        assessment = assess_impact(HypothesisCode.UNEXPLAINED, view_for())
        assert "UNEXPLAINED" in (assessment.not_assessable_reason or "")


class TestUnassessableCasesReportWhyRatherThanGuessing:
    def test_a_missing_metric_key_is_refused_for_a_line_bound_code(self) -> None:
        """Silently defaulting to some metric would produce a confident wrong basis."""
        assessment = assess_impact(HypothesisCode.OVERAGE_TIER_MISMATCH, view_for())
        assert assessment.impact is None
        assert "metric_key" in (assessment.not_assessable_reason or "")

    def test_an_unknown_metric_key_is_refused(self) -> None:
        assessment = assess_impact(HypothesisCode.UNIT_PRICE_MISMATCH, view_for(), "no_such_metric")
        assert assessment.impact is None
        assert "no line for metric" in (assessment.not_assessable_reason or "")

    def test_an_unresolved_line_yields_no_impact_and_says_why(self) -> None:
        """No price term means no rate, so no difference may be stated."""
        view = view_for(terms=[])
        assert not view.recalculation.is_complete

        assessment = assess_impact(HypothesisCode.OVERAGE_TIER_MISMATCH, view, "api_calls")
        assert assessment.impact is None
        assert "could not be recalculated" in (assessment.not_assessable_reason or "")

    def test_a_commitment_is_not_assessed_on_a_partial_recalculation(self) -> None:
        """A minimum applies to the whole invoice, so a partial total cannot settle it."""
        view = view_for(terms=[])
        assessment = assess_impact(HypothesisCode.COMMITMENT_SHORTFALL, view, "api_calls")
        assert assessment.impact is None
        assert "unresolved" in (assessment.not_assessable_reason or "")

    def test_a_provisional_balance_yields_no_allocation_impact(self) -> None:
        """§6.2: a small provisional figure must not read as a verified near-zero."""
        view = view_for(terms=[])
        assessment = assess_impact(HypothesisCode.INCORRECT_ALLOCATION, view)
        assert assessment.impact is None
        assert "provisional" in (assessment.not_assessable_reason or "")


class TestEveryAssessmentNamesItsBasis:
    @pytest.mark.parametrize("code", list(HypothesisCode))
    def test_the_basis_is_never_empty(self, code: HypothesisCode) -> None:
        """A figure or a refusal with no explanation is not reviewable."""
        assessment = assess_impact(code, view_for(), "api_calls")
        assert assessment.basis.strip()

    @pytest.mark.parametrize("code", list(HypothesisCode))
    def test_an_impact_always_has_a_trace_or_says_it_needs_none(self, code: HypothesisCode) -> None:
        """§6.4 traceability, or an explicit statement of why there is nothing to trace.

        Payment and statement hypotheses legitimately have no calculation trace — they
        compare two recorded figures rather than applying a pricing rule — so the
        requirement is that the absence is deliberate, not accidental.
        """
        assessment = assess_impact(code, view_for(), "api_calls")
        if assessment.impact is not None and assessment.trace is None:
            assert code in {
                HypothesisCode.UNAPPLIED_PAYMENT,
                HypothesisCode.INCORRECT_ALLOCATION,
                HypothesisCode.STATEMENT_TOTAL_MISMATCH,
                HypothesisCode.COMMITMENT_SHORTFALL,
            }


class TestApprovalRulesAreUnconditional:
    @pytest.mark.parametrize("option", sorted(MONEY_MOVING_OPTIONS, key=str))
    def test_every_money_moving_option_requires_approval(self, option) -> None:
        assert requires_approval(option) is True

    def test_the_non_monetary_options_are_exactly_the_two_documented(self) -> None:
        assert ResolutionOptionType.NO_ADJUSTMENT not in MONEY_MOVING_OPTIONS
        assert ResolutionOptionType.REQUEST_MORE_INFO not in MONEY_MOVING_OPTIONS

    def test_the_money_moving_set_is_the_four_documented_options(self) -> None:
        assert (
            frozenset(
                {
                    ResolutionOptionType.FULL_CREDIT,
                    ResolutionOptionType.PARTIAL_CREDIT,
                    ResolutionOptionType.REBILL_CORRECT_AMOUNT,
                    ResolutionOptionType.APPLY_UNAPPLIED_PAYMENT,
                }
            )
            == MONEY_MOVING_OPTIONS
        )
