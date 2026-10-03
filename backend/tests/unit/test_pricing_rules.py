"""Unit tests for the three billing rules.

Every expected amount here is hand-computed from the contract and the usage, not copied
from a previous run. That distinction is the point of the file: a test that asserts the
engine agrees with itself proves nothing, so each case states the arithmetic in its name
or docstring and the assertion is the independently derived figure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.billing import BillingDataError, InvoicePeriod, PriceTerm, Tier
from app.domain.contracts import BillingMode
from app.domain.money import Money
from app.pricing.rounding import round_line_amount
from app.pricing.rules import (
    LadderShortfall,
    billable_units,
    charge_commitment,
    charge_per_unit,
    charge_tiered,
    rule_ref,
)
from app.pricing.usage import UsageSummary

PERIOD = InvoicePeriod(date(2025, 3, 1), date(2025, 4, 1))
REF = "contract:CTR-5512#term:api_calls"


def usd(text: str) -> Money:
    return Money(Decimal(text), "USD")


def usage(quantity: str, *, event_count: int = 1) -> UsageSummary:
    """A usage summary with no duplicates and nothing out of period.

    Built directly rather than through :func:`summarise_usage` so that a rule test
    isolates the rule: the selection logic has its own tests in ``test_pricing_usage.py``.
    """
    return UsageSummary(
        metric_key="api_calls",
        period=PERIOD,
        total_quantity=Decimal(quantity),
        contributing_event_ids=tuple(f"e{i}" for i in range(event_count)),
        duplicates=(),
        out_of_period=(),
    )


def per_unit(price: str, *, included: str = "0", overage: str | None = None) -> PriceTerm:
    return PriceTerm(
        metric_key="api_calls",
        billing_mode=BillingMode.PER_UNIT,
        currency="USD",
        unit_price=usd(price),
        included_units=Decimal(included),
        overage_price=usd(overage) if overage is not None else None,
    )


def ladder(*bounds_and_prices: tuple[str | None, str], included: str = "0") -> PriceTerm:
    return PriceTerm(
        metric_key="api_calls",
        billing_mode=BillingMode.TIERED,
        currency="USD",
        included_units=Decimal(included),
        tiers=tuple(
            Tier(None if bound is None else Decimal(bound), usd(price))
            for bound, price in bounds_and_prices
        ),
    )


def commitment(minimum: str, *, price: str, included: str = "0") -> PriceTerm:
    return PriceTerm(
        metric_key="api_calls",
        billing_mode=BillingMode.COMMITMENT,
        currency="USD",
        unit_price=usd(price),
        included_units=Decimal(included),
        minimum_commitment=usd(minimum),
    )


# ---------------------------------------------------------------------------
# Included units, shared by every mode
# ---------------------------------------------------------------------------


def test_billable_units_removes_the_allowance() -> None:
    assert billable_units(per_unit("0.05", included="10000"), usage("41234")) == (
        Decimal("31234"),
        Decimal("10000"),
    )


def test_billable_units_floors_at_zero_rather_than_going_negative() -> None:
    """A free allowance cannot produce a negative charge."""
    assert billable_units(per_unit("0.05", included="10000"), usage("400")) == (
        Decimal(0),
        Decimal("400"),
    )


def test_the_allowance_reports_only_what_it_actually_absorbed() -> None:
    """10,000 included against 400 used means 400 absorbed, not 10,000."""
    _, allowance = billable_units(per_unit("0.05", included="10000"), usage("400"))

    assert allowance == Decimal("400")


def test_usage_exactly_equal_to_the_allowance_is_fully_absorbed() -> None:
    assert billable_units(per_unit("0.05", included="10000"), usage("10000")) == (
        Decimal(0),
        Decimal("10000"),
    )


def test_one_unit_past_the_allowance_is_charged() -> None:
    assert billable_units(per_unit("0.05", included="10000"), usage("10001")) == (
        Decimal(1),
        Decimal("10000"),
    )


# ---------------------------------------------------------------------------
# PER_UNIT
# ---------------------------------------------------------------------------


def test_per_unit_multiplies_quantity_by_rate() -> None:
    """1200 calls at $0.05 is $60.00."""
    charge = charge_per_unit(per_unit("0.05"), usage("1200"), REF)

    assert charge.exact_amount == usd("60.00")


def test_per_unit_subtracts_the_allowance_before_charging() -> None:
    """31,234 billable calls at $0.05 is $1,561.70."""
    charge = charge_per_unit(per_unit("0.05", included="10000"), usage("41234"), REF)

    assert charge.exact_amount == usd("1561.70")


def test_per_unit_uses_overage_price_for_units_beyond_the_allowance() -> None:
    """10,000 included at no charge, then 2,000 at the $0.02 overage rate = $40.00."""
    charge = charge_per_unit(
        per_unit("0.05", included="10000", overage="0.02"), usage("12000"), REF
    )

    assert charge.exact_amount == usd("40.00")
    assert charge.rate == usd("0.02")


def test_per_unit_with_a_zero_rate_is_zero_not_missing() -> None:
    """A free metric is priced at zero. That is an answer, not a gap in the evidence."""
    charge = charge_per_unit(per_unit("0"), usage("100000"), REF)

    assert charge.exact_amount == usd("0")
    assert charge.billable_units == Decimal("100000")


def test_zero_usage_produces_a_nil_charge_with_a_trace() -> None:
    charge = charge_per_unit(per_unit("0.05"), usage("0"), REF)

    assert charge.exact_amount == usd("0")
    assert charge.billable_units == Decimal(0)
    assert "Charge is nil" in charge.trace.describe()


def test_zero_usage_with_a_free_allowance_is_still_nil() -> None:
    charge = charge_per_unit(per_unit("0.05", included="10000"), usage("0"), REF)

    assert charge.exact_amount == usd("0")


def test_a_fractional_rate_and_quantity_multiply_exactly() -> None:
    """0.0007 * 0.5 = 0.00035 exactly; as a float it would be 0.00035000000000000003."""
    charge = charge_per_unit(per_unit("0.0007"), usage("0.5"), REF)

    assert charge.exact_amount.amount == Decimal("0.00035")
    assert charge.exact_amount.amount.as_tuple().exponent == -5


def test_the_exact_amount_is_not_pre_rounded() -> None:
    """The line boundary rounds once; the rule must not round first."""
    charge = charge_per_unit(per_unit("0.0007", included="10000"), usage("41234"), REF)

    assert charge.exact_amount.amount == Decimal("21.86380")
    assert round_line_amount(charge.exact_amount) == usd("21.86")


# ---------------------------------------------------------------------------
# TIERED
# ---------------------------------------------------------------------------


def test_tiered_charges_the_first_tier_when_usage_stays_inside_it() -> None:
    """41,234 units all fall in the single 0-and-above tier at $0.00070.

    41,234 * 0.00070 = 28.86380, which the line boundary rounds to 28.86.
    """
    charge = charge_tiered(ladder((None, "0.00070")), usage("41234"), REF)

    assert charge.exact_amount.amount == Decimal("28.86380")
    assert round_line_amount(charge.exact_amount) == usd("28.86")


def test_tiered_reproduces_the_design_document_example() -> None:
    """10,000 included, then a 0-50,000 tier at $0.00070, on 41,234 calls.

    §6.4 step 3: ``min(31234, 40000) * 0.00070 = 21.86380``, and step 4 rounds that to
    21.86. The second tier at $0.00050 is never reached.
    """
    term = ladder(("50000", "0.00070"), (None, "0.00050"), included="10000")

    charge = charge_tiered(term, usage("41234"), REF)

    assert charge.billable_units == Decimal("31234")
    assert charge.exact_amount.amount == Decimal("21.86380")
    assert round_line_amount(charge.exact_amount) == usd("21.86")
    assert "Tier 1 (0 to 50000 @ 0.00070)" in charge.trace.describe()


def test_tiered_splits_usage_across_tiers_progressively() -> None:
    """60,000 billable units: tier 1 takes 50,000 @ $0.01 = $500, tier 2 takes 10,000 @ $0.005 = $50."""
    charge = charge_tiered(ladder(("50000", "0.01"), (None, "0.005")), usage("60000"), REF)

    assert charge.exact_amount == usd("550.00")


def test_tiered_charges_nothing_for_usage_below_the_first_tier() -> None:
    """Ladder starts at 0, so any usage is inside tier 1. Zero usage is the nil case."""
    charge = charge_tiered(ladder(("50000", "0.01"), (None, "0.005")), usage("0"), REF)

    assert charge.exact_amount == usd("0")


def test_tier_boundary_usage_is_charged_at_the_lower_rate_only() -> None:
    """Exactly 50,000 units fills tier 1 and nothing more: 50,000 * $0.01 = $500.00."""
    charge = charge_tiered(ladder(("50000", "0.01"), (None, "0.005")), usage("50000"), REF)

    assert charge.exact_amount == usd("500.00")


def test_one_unit_past_a_tier_boundary_moves_one_unit_into_the_next_tier() -> None:
    """50,001 units: 50,000 @ $0.01 = $500.00 plus 1 @ $0.005 = $0.005 -> $500.005 -> $500.01."""
    charge = charge_tiered(ladder(("50000", "0.01"), (None, "0.005")), usage("50001"), REF)

    assert charge.exact_amount == usd("500.005")
    assert round_line_amount(charge.exact_amount) == usd("500.01")


def test_each_tier_contributes_its_own_step_to_the_trace() -> None:
    charge = charge_tiered(ladder(("50000", "0.01"), (None, "0.005")), usage("60000"), REF)
    labels = [step.label for step in charge.trace.steps]

    assert any(label.startswith("Tier 1 (0 to 50000") for label in labels)
    assert any(label.startswith("Tier 2 (50000 and above") for label in labels)


def test_tiers_the_usage_never_reaches_are_not_traced() -> None:
    """A long ladder should not produce a trace longer than the usage it explains."""
    charge = charge_tiered(
        ladder(("1000", "0.10"), ("5000", "0.05"), (None, "0.01")), usage("500"), REF
    )
    labels = [step.label for step in charge.trace.steps]

    assert sum(label.startswith("Tier") for label in labels) == 1
    assert charge.exact_amount == usd("50.00")


def test_a_ladder_that_ends_is_a_shortfall_rather_than_an_extrapolated_rate() -> None:
    """Usage past a bounded ladder has no agreed price, so no amount is produced."""
    term = ladder(("50000", "0.01"))

    with pytest.raises(LadderShortfall) as raised:
        charge_tiered(term, usage("60000"), REF)

    assert raised.value.covered_units == Decimal("50000")
    assert raised.value.uncovered_units == Decimal("10000")


def test_a_shortfall_reports_how_much_the_ladder_did_cover() -> None:
    with pytest.raises(LadderShortfall) as raised:
        charge_tiered(ladder(("1000", "0.10")), usage("1500"), REF)

    assert raised.value.covered_units == Decimal("1000")
    assert raised.value.uncovered_units == Decimal("500")


def test_a_tiered_ladder_covers_usage_exactly_at_its_final_bound() -> None:
    """A ladder ending at 1,000 prices exactly 1,000 units without complaint."""
    charge = charge_tiered(ladder(("1000", "0.10")), usage("1000"), REF)

    assert charge.exact_amount == usd("100.00")


def test_a_zero_priced_tier_is_a_valid_free_allowance() -> None:
    """Tier 1 free up to 10,000 units, then $0.01: 20,000 units = 10,000 free + 10,000 paid."""
    charge = charge_tiered(ladder(("10000", "0"), (None, "0.01")), usage("20000"), REF)

    assert charge.exact_amount == usd("100.00")


def test_tiers_are_summed_without_intermediate_rounding() -> None:
    """Two exact tier products of $0.015 each, summed to $0.030 and rounded once.

    Rounding each tier to cents on the way past would give 0.02 + 0.02 = $0.04, so the
    charged amount would depend on how many tiers the contract happened to have. That is
    the specific failure the single-rounding rule in §6.2 exists to prevent.
    """
    charge = charge_tiered(ladder(("3", "0.005"), (None, "0.005")), usage("6"), REF)

    assert charge.exact_amount.amount == Decimal("0.030")
    assert round_line_amount(charge.exact_amount) == usd("0.03")


def test_a_single_tier_product_can_also_land_on_a_half_cent() -> None:
    """3 units at $0.005 is $0.015, which rounds away from zero to $0.02."""
    charge = charge_tiered(ladder((None, "0.005")), usage("3"), REF)

    assert charge.exact_amount.amount == Decimal("0.015")
    assert round_line_amount(charge.exact_amount) == usd("0.02")


# ---------------------------------------------------------------------------
# COMMITMENT
# ---------------------------------------------------------------------------


def test_commitment_charges_the_metered_amount_when_it_clears_the_floor() -> None:
    """$1,200 metered against a $1,000 floor: the metered amount stands."""
    charge = charge_commitment(commitment("1000.00", price="0.10"), usage("12000"), REF)

    assert charge.exact_amount == usd("1200.00")


def test_commitment_raises_a_shortfall_to_the_floor() -> None:
    """$400 metered against a $1,000 floor: $1,000 is owed."""
    charge = charge_commitment(commitment("1000.00", price="0.10"), usage("4000"), REF)

    assert charge.exact_amount == usd("1000.00")


def test_commitment_is_charged_in_full_when_there_is_no_usage_at_all() -> None:
    charge = charge_commitment(commitment("1000.00", price="0.10"), usage("0"), REF)

    assert charge.exact_amount == usd("1000.00")
    assert charge.billable_units == Decimal(0)


def test_commitment_exactly_meeting_the_floor_is_not_raised() -> None:
    """1,000 units at $1.00 is exactly $1,000.00, so max() returns it unchanged."""
    charge = charge_commitment(commitment("1000.00", price="1.00"), usage("1000"), REF)

    assert charge.exact_amount == usd("1000.00")


def test_commitment_one_cent_below_the_floor_is_raised_by_one_cent() -> None:
    """999 units at $1.00 is $999.00, one cent under the $1,000.00 floor."""
    charge = charge_commitment(commitment("1000.00", price="1.00"), usage("999"), REF)

    assert charge.exact_amount == usd("1000.00")


def test_the_commitment_trace_shows_both_the_metered_amount_and_the_floor() -> None:
    charge = charge_commitment(commitment("1000.00", price="0.10"), usage("4000"), REF)
    rendered = charge.trace.describe()

    assert "Exact charge (unrounded): 4000 * 0.10 = 400.00" in rendered
    assert "Committed minimum" in rendered
    assert "Shortfall raised to the committed minimum" in rendered


def test_commitment_applies_the_allowance_before_the_floor() -> None:
    """10,000 units included, 4,000 used, $1.00 each: metered is nil, floor still applies."""
    charge = charge_commitment(
        commitment("1000.00", price="1.00", included="10000"), usage("4000"), REF
    )

    assert charge.billable_units == Decimal(0)
    assert charge.included_units_applied == Decimal("4000")
    assert charge.exact_amount == usd("1000.00")


def test_a_zero_commitment_is_not_a_floor() -> None:
    """A $0 commitment means no floor, so the metered amount stands on its own."""
    charge = charge_commitment(commitment("0.00", price="0.10"), usage("500"), REF)

    assert charge.exact_amount == usd("50.00")


# ---------------------------------------------------------------------------
# Malformed price terms are refused before any arithmetic
# ---------------------------------------------------------------------------


def test_a_tiered_term_without_a_ladder_is_refused() -> None:
    with pytest.raises(BillingDataError, match="requires a tier ladder"):
        PriceTerm(metric_key="api_calls", billing_mode=BillingMode.TIERED, currency="USD")


def test_a_commitment_term_without_a_floor_is_refused() -> None:
    with pytest.raises(BillingDataError, match="requires a minimum_commitment"):
        PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.COMMITMENT,
            currency="USD",
            unit_price=usd("1.00"),
        )


def test_a_per_unit_term_with_no_rate_is_refused() -> None:
    with pytest.raises(BillingDataError, match="requires unit_price or overage_price"):
        PriceTerm(metric_key="api_calls", billing_mode=BillingMode.PER_UNIT, currency="USD")


def test_a_ladder_whose_bounds_do_not_increase_is_refused() -> None:
    """A zero-width tier would make the amount depend on how the ladder is walked."""
    with pytest.raises(BillingDataError, match="strictly increase"):
        ladder(("5000", "0.01"), ("5000", "0.005"))


def test_a_decreasing_ladder_is_refused() -> None:
    with pytest.raises(BillingDataError, match="strictly increase"):
        ladder(("5000", "0.01"), ("1000", "0.005"))


def test_a_bounded_tier_after_an_unbounded_one_is_refused() -> None:
    """The second tier could never be reached, so the ladder contradicts itself."""
    with pytest.raises(BillingDataError, match="only the final tier"):
        ladder((None, "0.01"), ("9000", "0.005"))


def test_a_ladder_attached_to_a_non_tiered_term_is_refused() -> None:
    """A ladder nothing reads is configuration nobody can reason about."""
    with pytest.raises(BillingDataError, match="must not carry a tier ladder"):
        PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT,
            currency="USD",
            unit_price=usd("0.01"),
            tiers=(Tier(Decimal("100"), usd("0.02")),),
        )


def test_a_negative_rate_is_refused() -> None:
    with pytest.raises(BillingDataError, match="must not be negative"):
        per_unit("-0.01")


def test_a_negative_tier_price_is_refused() -> None:
    with pytest.raises(BillingDataError, match="must not be negative"):
        ladder(("100", "-0.01"))


def test_a_negative_allowance_is_refused() -> None:
    with pytest.raises(BillingDataError, match="must not be negative"):
        per_unit("0.01", included="-1")


def test_rates_in_two_currencies_are_refused() -> None:
    """ResolveIQ performs no FX conversion, so a mixed-currency term cannot be charged."""
    with pytest.raises(BillingDataError, match="one currency"):
        PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.PER_UNIT,
            currency="USD",
            unit_price=usd("0.01"),
            overage_price=Money(Decimal("0.02"), "EUR"),
        )


def test_a_tier_price_in_another_currency_is_refused() -> None:
    with pytest.raises(BillingDataError, match="one currency"):
        PriceTerm(
            metric_key="api_calls",
            billing_mode=BillingMode.TIERED,
            currency="USD",
            tiers=(Tier(Decimal("100"), Money(Decimal("0.02"), "GBP")),),
        )


# ---------------------------------------------------------------------------
# Trace completeness
# ---------------------------------------------------------------------------


def test_every_charge_carries_a_trace_naming_its_rule() -> None:
    charge = charge_per_unit(per_unit("0.05"), usage("10"), "contract:CTR-9#term:seats")

    assert charge.trace.rule_ref == "contract:CTR-9#term:seats"
    assert charge.trace.engine_version == "1.0.0"
    assert charge.trace.steps[0].n == 1


def test_the_trace_records_the_billable_quantity_for_both_per_unit_and_tiered() -> None:
    charges = (
        charge_per_unit(per_unit("0.05", included="1000"), usage("1500"), REF),
        charge_tiered(
            ladder(("1000", "0.05"), (None, "0.01"), included="1000"), usage("1500"), REF
        ),
    )

    for charge in charges:
        assert "Billable units: max(1500 - 1000, 0) = 500" in charge.trace.describe()


def test_rule_ref_names_the_contract_and_the_metric() -> None:
    assert rule_ref("CTR-5512", "api_calls") == "contract:CTR-5512#term:api_calls"
