"""Unit tests for the Money value object.

These tests are the executable form of NEP-01. They need no database, no
network, and no mocking framework.
"""

from __future__ import annotations

from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal

import pytest

from app.domain.money import (
    STORAGE_SCALE,
    CurrencyMismatchError,
    Money,
    MoneyError,
    NonFiniteAmountError,
    UnsupportedAmountTypeError,
    sum_money,
)

# ---------------------------------------------------------------------------
# NEP-01: floats are rejected, not coerced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.1, 1.5, -2.25, 1e-9, float("1e308")])
def test_float_is_rejected(bad: float) -> None:
    with pytest.raises(UnsupportedAmountTypeError, match="float"):
        Money(bad, "USD")


def test_float_rejection_is_also_a_type_error() -> None:
    # Programming errors should surface as TypeError for callers that check it.
    with pytest.raises(TypeError):
        Money(0.1, "USD")  # type: ignore[arg-type]


def test_bool_is_rejected() -> None:
    # bool is a subclass of int; True -> Decimal(1) is correct but always a bug.
    with pytest.raises(UnsupportedAmountTypeError, match="bool"):
        Money(True, "USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [None, [], {}, object(), b"1.00"])
def test_unsupported_types_are_rejected(bad: object) -> None:
    with pytest.raises(UnsupportedAmountTypeError):
        Money(bad, "USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_amounts_are_rejected(bad: str) -> None:
    with pytest.raises(NonFiniteAmountError):
        Money(Decimal(bad), "USD")


# ---------------------------------------------------------------------------
# Accepted inputs
# ---------------------------------------------------------------------------


def test_accepts_decimal_int_and_str() -> None:
    assert Money(Decimal("21.86"), "USD").amount == Decimal("21.86")
    assert Money(5, "USD").amount == Decimal(5)
    assert Money("21.86", "USD").amount == Decimal("21.86")
    assert Money("  21.86  ", "USD").amount == Decimal("21.86")


def test_zero_and_negative_are_valid() -> None:
    assert Money.zero("USD").is_zero()
    assert Money(Decimal("-10.00"), "USD").is_negative()


def test_rejects_malformed_string() -> None:
    with pytest.raises(MoneyError):
        Money("twelve dollars", "USD")


def test_rejects_empty_string() -> None:
    with pytest.raises(MoneyError, match="empty string"):
        Money("", "USD")


# ---------------------------------------------------------------------------
# Currency handling
# ---------------------------------------------------------------------------


def test_currency_is_normalised_to_uppercase() -> None:
    assert Money(1, "usd").currency == "USD"


@pytest.mark.parametrize("bad", ["US", "USDD", "US1", "", "  ", 840])
def test_invalid_currency_codes_are_rejected(bad: object) -> None:
    with pytest.raises((MoneyError, UnsupportedAmountTypeError)):
        Money(1, bad)  # type: ignore[arg-type]


def test_add_and_subtract_same_currency() -> None:
    total = Money(Decimal("21.86"), "USD") + Money(Decimal("0.14"), "USD")
    assert total.amount == Decimal("22.00")
    assert total.currency == "USD"

    difference = Money(Decimal("100.00"), "USD") - Money(Decimal("99.99"), "USD")
    assert difference.amount == Decimal("0.01")


def test_cross_currency_arithmetic_raises() -> None:
    usd = Money(Decimal("10.00"), "USD")
    eur = Money(Decimal("10.00"), "EUR")

    with pytest.raises(CurrencyMismatchError):
        usd + eur  # type: ignore[operator]
    with pytest.raises(CurrencyMismatchError):
        usd - eur  # type: ignore[operator]
    # Cross-currency ordering must raise rather than return a meaningless answer.
    # The comparisons are assigned so linters do not read them as dead code:
    # the assertion is that the *operation* raises.
    with pytest.raises(CurrencyMismatchError):
        _ = usd < eur
    with pytest.raises(CurrencyMismatchError):
        _ = usd >= eur


def test_equality_requires_same_currency() -> None:
    # Decimal equality holds numerically, but these are different currencies.
    assert Money(Decimal("10.00"), "USD") != Money(Decimal("10.00"), "EUR")


def test_comparison_within_currency() -> None:
    small = Money(Decimal("10.00"), "USD")
    large = Money(Decimal("10.01"), "USD")

    assert small < large
    assert large > small
    assert small <= Money(Decimal("10.00"), "USD")
    assert small >= Money(Decimal("10.00"), "USD")


def test_sorting_a_list_of_money() -> None:
    amounts = [
        Money(Decimal("3.00"), "USD"),
        Money(Decimal("1.00"), "USD"),
        Money(Decimal("2.00"), "USD"),
    ]
    assert [m.amount for m in sorted(amounts)] == [
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
    ]


# ---------------------------------------------------------------------------
# Multiplication never introduces a float
# ---------------------------------------------------------------------------


def test_multiply_by_int_and_decimal() -> None:
    unit_price = Money(Decimal("0.0007"), "USD")
    assert (unit_price * 40000).amount == Decimal("28.0000")
    assert (unit_price * Decimal("0.5")).amount == Decimal("0.00035")


@pytest.mark.parametrize("bad", [0.1, 1.5, True, "2", None])
def test_multiply_rejects_non_exact_scalars(bad: object) -> None:
    with pytest.raises(UnsupportedAmountTypeError):
        Money(Decimal("1.00"), "USD") * bad  # type: ignore[operator]


def test_rmul_supports_quantity_on_the_left() -> None:
    assert (3 * Money(Decimal("1.50"), "USD")).amount == Decimal("4.50")


# ---------------------------------------------------------------------------
# Rounding is always explicit
# ---------------------------------------------------------------------------


def test_quantise_defaults_to_currency_minor_units() -> None:
    assert Money(Decimal("21.8649"), "USD").quantise().amount == Decimal("21.86")
    assert Money(Decimal("21.8655"), "USD").quantise().amount == Decimal("21.87")


def test_quantise_half_up_versus_half_even() -> None:
    # The two modes disagree exactly at the half.
    half = Money(Decimal("2.125"), "USD")
    assert half.quantise(rounding=ROUND_HALF_UP).amount == Decimal("2.13")
    assert half.quantise(rounding="ROUND_HALF_EVEN").amount == Decimal("2.12")
    assert half.quantise(rounding=ROUND_HALF_DOWN).amount == Decimal("2.12")


def test_quantise_to_storage_scale_keeps_sub_cent_prices() -> None:
    # ADR-011: sub-cent unit prices must survive to storage.
    unit_price = Money(Decimal("0.0007"), "USD")
    assert unit_price.quantise_to_storage_scale().amount == Decimal("0.0007")
    assert STORAGE_SCALE == 4


def test_quantise_ignores_ambient_decimal_context() -> None:
    import decimal

    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 2
        # A hostile ambient context must not corrupt the result.
        assert Money(Decimal("123456.789"), "USD").quantise().amount == Decimal("123456.79")
    finally:
        decimal.getcontext().prec = original


# ---------------------------------------------------------------------------
# Allocation: parts must sum exactly to the whole
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "weights"),
    [
        ("100.00", [1, 1, 1]),
        ("10.00", [1, 1, 2]),
        ("0.05", [1, 1, 1]),
        ("-10.00", [1, 1, 1]),
        ("1.00", [5, 3, 2]),
        ("7.77", [1, 1, 1, 1, 1, 1, 1]),
    ],
)
def test_allocation_conserves_money(total: str, weights: list[int]) -> None:
    whole = Money(Decimal(total), "USD")
    parts = whole.allocate(weights)

    assert len(parts) == len(weights)
    assert sum((p.amount for p in parts), Decimal(0)) == whole.amount
    assert all(p.currency == "USD" for p in parts)


def test_allocation_gives_remainder_to_largest_fractional_parts() -> None:
    parts = Money(Decimal("10.00"), "USD").allocate([1, 1, 1])
    assert sorted(p.amount for p in parts) == [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]


def test_allocation_rejects_bad_weights() -> None:
    with pytest.raises(MoneyError, match="at least one weight"):
        Money(Decimal("1.00"), "USD").allocate([])
    with pytest.raises(MoneyError, match="non-negative"):
        Money(Decimal("1.00"), "USD").allocate([1, -1])
    with pytest.raises(MoneyError, match="must not sum to zero"):
        Money(Decimal("1.00"), "USD").allocate([0, 0])


# ---------------------------------------------------------------------------
# Immutability, serialisation, summation
# ---------------------------------------------------------------------------


def test_money_is_immutable_and_hashable() -> None:
    amount = Money(Decimal("1.00"), "USD")
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is a subclass
        amount.amount = Decimal("2.00")  # type: ignore[misc]
    assert {amount, Money(Decimal("1.00"), "USD")} == {amount}


def test_string_formats_are_exact_and_parseable() -> None:
    amount = Money(Decimal("1234567.8900"), "USD")
    assert amount.as_string() == "1234567.8900"
    assert str(amount) == "1234567.8900 USD"
    assert Money.parse("1234567.89", "USD").amount == Decimal("1234567.89")
    # No scientific notation: important for JSON and for SQL parameters.
    assert "E" not in Money(Decimal("1E+3"), "USD").as_string()
    assert Money(Decimal("1E+3"), "USD").as_string() == "1000"


def test_sum_money_requires_a_declared_currency() -> None:
    items = [Money(Decimal("1.10"), "USD"), Money(Decimal("2.20"), "USD")]
    assert sum_money(items, "USD").amount == Decimal("3.30")
    assert sum_money([], "USD").is_zero()

    with pytest.raises(CurrencyMismatchError):
        sum_money([*items, Money(Decimal("1.00"), "EUR")], "USD")


def test_money_rejects_amounts_out_of_supported_range() -> None:
    with pytest.raises(MoneyError, match="out of supported range"):
        Money(Decimal("1e16"), "USD")
