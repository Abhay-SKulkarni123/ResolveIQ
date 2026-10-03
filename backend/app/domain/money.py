"""Exact monetary value object.

This module is the enforcement point for NEP-01: monetary calculations use exact
decimal arithmetic, never binary floating point. It is the *only* type permitted
to hold an amount.

Design notes
------------
* ``float`` is **rejected**, not coerced. ``Decimal(0.1)`` is exactly
  ``0.1000000000000000055511151231257827...``; converting with ``str()`` hides a
  bug that should be impossible rather than fixing it.
* The ambient :mod:`decimal` context is never mutated. Operations that need a
  precision use ``localcontext()``. Global context mutation is a classic source
  of order-dependent bugs that appear only under concurrency.
* Cross-currency arithmetic raises. There is no implicit FX conversion.
* Rounding is always explicit. There is no implicit quantisation.

Rounding *modes* live here (they are part of the value object's vocabulary), but
the decision of which mode applies at which billing boundary belongs to
``app.pricing.rounding``. Keeping the policy out of this module is what stops the
value object from growing billing logic (SRP).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, DecimalException, localcontext
from typing import Any

__all__ = [
    "ROUND_HALF_EVEN",
    "ROUND_HALF_UP",
    "STORAGE_SCALE",
    "CurrencyMismatchError",
    "Money",
    "MoneyError",
    "NonFiniteAmountError",
    "UnsupportedAmountTypeError",
    "sum_money",
]

# Number of decimal places retained in storage (NUMERIC(19,4)).
# Sub-cent unit prices such as 0.0007 per API call must stay representable.
STORAGE_SCALE = 4
_STORAGE_QUANTUM = Decimal(1).scaleb(-STORAGE_SCALE)

#: Minor-unit exponent per currency. Defaults to 2 for anything unlisted.
#: Revisit if a 0- or 3-exponent currency is introduced (see ADR-011).
CURRENCY_MINOR_UNITS: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2}
_DEFAULT_MINOR_UNITS = 2

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Guard against absurd values long before Postgres NUMERIC(19,4) rejects them.
_MAX_ABSOLUTE = Decimal("1e15")


class MoneyError(ValueError):
    """Base class for all monetary errors."""


class UnsupportedAmountTypeError(MoneyError, TypeError):
    """Raised when a value cannot be safely converted to an exact amount.

    Subclasses ``TypeError`` because this is a programming error at a boundary,
    not bad user input.
    """


class NonFiniteAmountError(MoneyError):
    """Raised for NaN or Infinity."""


class CurrencyMismatchError(MoneyError):
    """Raised when an operation mixes currencies.

    ResolveIQ has no FX conversion; see open question OQ-04.
    """


def _to_decimal(value: Any) -> Decimal:
    """Convert an acceptable input to ``Decimal``, rejecting unsafe types."""
    if isinstance(value, Money):
        return value.amount
    if isinstance(value, bool):
        # bool is a subclass of int. True -> Decimal(1) is technically correct
        # and practically always a bug, so it is refused explicitly.
        raise UnsupportedAmountTypeError(
            "bool is not a valid monetary amount (did you mean a flag?)"
        )
    if isinstance(value, float):
        raise UnsupportedAmountTypeError(
            "float is not an acceptable monetary amount; use Decimal or str. "
            "Binary floating point cannot represent decimal currency exactly."
        )
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyError("empty string is not a valid monetary amount")
        try:
            candidate = Decimal(text)
        except DecimalException as exc:
            raise MoneyError(f"not a valid decimal amount: {value!r}") from exc
    else:
        raise UnsupportedAmountTypeError(
            f"unsupported type for monetary amount: {type(value).__name__}"
        )

    if not candidate.is_finite():
        raise NonFiniteAmountError(f"monetary amount must be finite, got {candidate}")
    if abs(candidate) > _MAX_ABSOLUTE:
        raise MoneyError(f"monetary amount out of supported range: {candidate}")
    return candidate


@dataclass(frozen=True, eq=True)
class Money:
    """An exact amount in a single currency.

    Immutable and always valid: there is no such thing as an unvalidated
    ``Money`` instance, which is why no amount can be compared or added without
    having passed through this constructor.

    Examples:
        >>> Money(Decimal("21.86"), "USD") + Money(Decimal("0.14"), "USD")
        Money(Decimal('22.00'), 'USD')
        >>> Money(1.5, "USD")
        Traceback (most recent call last):
        UnsupportedAmountTypeError: float is not an acceptable monetary amount...
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _to_decimal(self.amount))

        currency = self.currency
        if not isinstance(currency, str):
            raise UnsupportedAmountTypeError(
                f"currency must be a string, got {type(currency).__name__}"
            )
        normalised = currency.strip().upper()
        if not _CURRENCY_RE.match(normalised):
            raise MoneyError(f"currency must be a 3-letter ISO 4217 code, got {self.currency!r}")
        object.__setattr__(self, "currency", normalised)

    # -- construction helpers -------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal("0"), currency)

    @classmethod
    def parse(cls, text: str, currency: str) -> Money:
        """Parse a string such as ``"1234.56"`` in the given currency."""
        return cls(Decimal(text.strip()), currency)

    # -- introspection --------------------------------------------------------

    @property
    def minor_units(self) -> int:
        """Number of decimal places for this currency (2 for USD)."""
        return CURRENCY_MINOR_UNITS.get(self.currency, _DEFAULT_MINOR_UNITS)

    def is_zero(self) -> bool:
        return self.amount == 0

    def is_negative(self) -> bool:
        return self.amount < 0

    # -- arithmetic -----------------------------------------------------------

    def _check_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency} with {other.currency}; "
                "ResolveIQ performs no implicit currency conversion"
            )

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        """Multiply by an exact scalar.

        ``float`` is refused here too, so a float cannot sneak in through a
        quantity or a rate.
        """
        if isinstance(factor, bool) or not isinstance(factor, (int, Decimal)):
            raise UnsupportedAmountTypeError(
                f"Money may only be multiplied by an int or Decimal, got {type(factor).__name__}"
            )
        return Money(self.amount * Decimal(factor), self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    # -- comparison -----------------------------------------------------------
    # Ordering compares amount first and is only valid within one currency.
    # Raising on a cross-currency comparison is deliberate: silently ordering
    # USD against EUR would produce a confidently meaningless answer.

    def _comparable(self, other: object) -> bool:
        """Return True if ``other`` may be ordered against ``self``.

        Raises ``CurrencyMismatchError`` when the currencies differ, so a
        cross-currency comparison can never silently produce a meaningless
        ordering.
        """
        if not isinstance(other, Money):
            return False
        self._check_same_currency(other)
        return True

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount >= other.amount

    # -- quantisation ---------------------------------------------------------

    def quantise(self, minor_units: int | None = None, rounding: str = ROUND_HALF_UP) -> Money:
        """Round to a number of decimal places.

        Rounding is never implicit. Presentation and settlement boundaries call
        this explicitly; storage does not (see ADR-011).
        """
        places = self.minor_units if minor_units is None else minor_units
        with localcontext() as ctx:
            # Enough precision that the quantise itself is not distorted by the
            # ambient context of whichever thread is running.
            ctx.prec = max(28, len(self.amount.as_tuple().digits) + abs(places) + 10)
            quantum = Decimal(1).scaleb(-places)
            return Money(self.amount.quantize(quantum, rounding=rounding), self.currency)

    def quantise_to_storage_scale(self, rounding: str = ROUND_HALF_UP) -> Money:
        """Round to ``STORAGE_SCALE`` decimals, for values headed to NUMERIC(19,4)."""
        return self.quantise(minor_units=STORAGE_SCALE, rounding=rounding)

    def allocate(self, weights: list[int], rounding: str = ROUND_HALF_EVEN) -> list[Money]:
        """Split this amount across ``weights``, conserving every minor unit.

        Allocation happens at the currency's minor-unit resolution, so splitting
        ``10.00 USD`` three ways gives ``3.33 / 3.33 / 3.34`` rather than
        ``3 / 3 / 4``. The parts sum exactly to the whole: no money is created or
        lost. Leftover minor units go to the largest fractional parts first.

        ``rounding`` governs only the initial step of bringing ``self`` onto the
        minor-unit grid; the split itself is exact.

        Raises if the weights are empty, negative, or sum to zero.
        """
        if not weights:
            raise MoneyError("allocate requires at least one weight")
        if any(w < 0 for w in weights):
            raise MoneyError("allocate weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise MoneyError("allocate weights must not sum to zero")

        places = self.minor_units
        with localcontext() as ctx:
            ctx.prec = max(28, len(self.amount.as_tuple().digits) + 20)
            # Work in integer minor units: exact, and immune to decimal context.
            total_minor = int(self.quantise(places, rounding).amount.scaleb(places))
            exact = [Decimal(total_minor) * Decimal(w) / Decimal(total_weight) for w in weights]
            floors = [int(x.to_integral_value(rounding="ROUND_FLOOR")) for x in exact]
            residual = total_minor - sum(floors)
            # Hand leftover units to the largest fractional parts first.
            order = sorted(
                range(len(weights)), key=lambda i: (exact[i] - floors[i], -i), reverse=True
            )
            for position in range(residual):
                floors[order[position % len(order)]] += 1
            return [Money(Decimal(f).scaleb(-places), self.currency) for f in floors]

    # -- serialisation --------------------------------------------------------

    def as_string(self) -> str:
        """Plain decimal string. Safe to put in a JSON payload or a SQL parameter."""
        return format(self.amount, "f")

    def __str__(self) -> str:
        return f"{self.as_string()} {self.currency}"


def sum_money(items: list[Money], currency: str) -> Money:
    """Sum a list of ``Money``, requiring every element to share ``currency``.

    A named function rather than ``sum()`` so the currency can be stated once and
    checked, instead of relying on the first element being correct.
    """
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total
