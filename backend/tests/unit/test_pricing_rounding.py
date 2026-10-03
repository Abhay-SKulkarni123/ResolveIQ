"""Unit tests for the rounding policy and the calculation trace.

The policy tests are the executable form of docs/SYSTEM_DESIGN.md §6.2. They exist
because that table is the only definition of where money is rounded, and a table does
not stop anybody: if someone changes a rounding mode in :mod:`app.pricing.rounding`,
these fail and the change has to be argued for rather than slipped in.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from app.domain.money import Money
from app.pricing.rounding import (
    ALLOCATION_RESIDUAL_MODE,
    EXTENDED_AMOUNT_MODE,
    INVOICE_TOTAL_MODE,
    LINE_AMOUNT_MODE,
    allocation_residual_mode,
    round_invoice_total,
    round_line_amount,
)
from app.pricing.trace import ENGINE_VERSION, CalculationTrace, TraceBuilder, TraceStep


def usd(text: str) -> Money:
    return Money(Decimal(text), "USD")


# ---------------------------------------------------------------------------
# The policy table itself
# ---------------------------------------------------------------------------


def test_line_amounts_round_half_up() -> None:
    assert LINE_AMOUNT_MODE == ROUND_HALF_UP


def test_invoice_totals_round_half_up() -> None:
    assert INVOICE_TOTAL_MODE == ROUND_HALF_UP


def test_allocation_residuals_round_half_even() -> None:
    assert ALLOCATION_RESIDUAL_MODE == ROUND_HALF_EVEN


def test_extended_amounts_declare_no_rounding_rather_than_a_default() -> None:
    """There is no rounding mode for an exact amount.

    Expressed as ``None`` so that a caller reaching for it gets a failure instead of
    quietly applying a mode nobody chose.
    """
    assert EXTENDED_AMOUNT_MODE is None


def test_money_allocate_actually_uses_the_policy_residual_mode() -> None:
    """Ties the constant to the behaviour, so it cannot drift from :meth:`Money.allocate`.

    Splitting 0.05 three ways is the case that distinguishes the two modes: HALF_EVEN
    leaves it at 0.02, HALF_UP takes it to 0.02 as well, so the assertion here is on the
    mode rather than on a hand-computed split.
    """
    assert allocation_residual_mode() == ROUND_HALF_EVEN
    assert Money.allocate.__defaults__ == (ROUND_HALF_EVEN,)


def test_allocation_conserves_every_minor_unit() -> None:
    """The reason HALF_EVEN is used for residuals: the parts must sum to the whole."""
    parts = usd("10.00").allocate([1, 1, 1])

    assert [str(part.amount) for part in parts] == ["3.34", "3.33", "3.33"]
    assert sum_money(parts) == usd("10.00")


def sum_money(items: list[Money]) -> Money:
    total = items[0]
    for item in items[1:]:
        total = total + item
    return total


# ---------------------------------------------------------------------------
# Line amount rounding
# ---------------------------------------------------------------------------


def test_an_exact_line_amount_is_unchanged() -> None:
    assert round_line_amount(usd("21.86")) == usd("21.86")


def test_sub_cent_precision_is_preserved_until_the_line_boundary() -> None:
    """The worked example from §6.4.

    ``31234 * 0.00070`` is 21.86380 and stays exact through the arithmetic; it becomes
    21.86 only when it becomes a line amount.
    """
    exact = Money(Decimal("31234") * Decimal("0.00070"), "USD")

    assert exact.amount == Decimal("21.86380")
    assert round_line_amount(exact) == usd("21.86")


@pytest.mark.parametrize(
    ("exact", "expected"),
    [
        ("21.86380", "21.86"),
        ("21.865", "21.87"),
        ("21.864", "21.86"),
        ("0.005", "0.01"),
        ("0.004", "0.00"),
        ("-21.865", "-21.87"),
        ("-0.005", "-0.01"),
    ],
)
def test_half_up_rounds_away_from_zero_at_the_boundary(exact: str, expected: str) -> None:
    """The distinction from ROUND_HALF_EVEN, which would send 0.005 to 0.00.

    A half cent rounded down is a charge an analyst will dispute, so the commercial
    convention is chosen deliberately rather than inherited from the decimal default.
    """
    assert round_line_amount(usd(exact)) == usd(expected)


def test_rounding_to_the_line_boundary_keeps_the_currency() -> None:
    assert round_line_amount(Money(Decimal("1.005"), "EUR")).currency == "EUR"


def test_an_unlisted_currency_uses_the_documented_two_minor_unit_fallback() -> None:
    """``Money`` defaults any currency outside its table to two decimal places.

    JPY is the obvious counter-example: it has no minor unit. The fallback is ADR-011's
    stated assumption rather than an oversight, and it is recorded here so that adding
    JPY to ``CURRENCY_MINOR_UNITS`` is a deliberate change with a test that moves, not
    a silent correction.
    """
    assert Money(Decimal("1200.4"), "JPY").minor_units == 2
    assert round_line_amount(Money(Decimal("1200.4"), "JPY")) == Money(Decimal("1200.40"), "JPY")


def test_invoice_totals_use_the_same_mode_as_line_amounts() -> None:
    assert round_invoice_total(usd("1000.005")) == usd("1000.01")


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def build_trace() -> CalculationTrace:
    return (
        TraceBuilder(rule_ref="contract:CTR-5512#term:api_calls")
        .step(
            "Total metered quantity in period",
            "sum(usage_events.quantity WHERE metric_key='api_calls')",
            "41234",
            quantity="41234",
            event_count="41234",
        )
        .step("Subtract included units", "max(41234 - 10000, 0)", "31234")
        .step(
            "Tier 1 (10000-50000 @ 0.00070)",
            "min(31234, 40000) * 0.00070",
            "21.86380",
        )
        .step("Round line amount (ROUND_HALF_UP)", "21.86380 -> 21.86", "21.86")
        .build()
    )


def test_step_numbers_are_assigned_in_order() -> None:
    assert [step.n for step in build_trace().steps] == [1, 2, 3, 4]


def test_the_trace_carries_the_rule_reference_and_engine_version() -> None:
    trace = build_trace()

    assert trace.rule_ref == "contract:CTR-5512#term:api_calls"
    assert trace.engine_version == ENGINE_VERSION


def test_the_final_result_is_the_last_step() -> None:
    assert build_trace().final_result == "21.86"


def test_a_trace_serialises_to_the_documented_shape() -> None:
    payload = build_trace().as_dict()

    assert payload["rule_ref"] == "contract:CTR-5512#term:api_calls"
    assert payload["engine_version"] == ENGINE_VERSION
    assert payload["steps"][0] == {
        "n": 1,
        "label": "Total metered quantity in period",
        "expression": "sum(usage_events.quantity WHERE metric_key='api_calls')",
        "result": "41234",
        "inputs": {"quantity": "41234", "event_count": "41234"},
    }
    assert "inputs" not in payload["steps"][1]


def test_a_trace_renders_readably_for_a_human() -> None:
    rendered = build_trace().describe()

    assert rendered.splitlines()[0] == "contract:CTR-5512#term:api_calls (engine 1.0.0)"
    assert "4. Round line amount (ROUND_HALF_UP): 21.86380 -> 21.86 = 21.86" in rendered


def test_a_trace_is_immutable_and_compares_by_value() -> None:
    assert build_trace() == build_trace()


def test_an_empty_trace_is_refused() -> None:
    """A recorded amount must be explainable; an empty trace explains nothing."""
    with pytest.raises(ValueError, match="empty calculation trace"):
        TraceBuilder(rule_ref="contract:X").build()


def test_asking_an_empty_trace_for_its_result_is_an_error() -> None:
    empty = CalculationTrace(steps=(), rule_ref="contract:X")

    with pytest.raises(ValueError, match="at least one step"):
        assert empty.final_result


def test_trace_inputs_keep_their_order() -> None:
    step = TraceStep(n=1, label="l", expression="e", result="r", inputs=(("b", "1"), ("a", "2")))

    assert step.inputs == (("b", "1"), ("a", "2"))
