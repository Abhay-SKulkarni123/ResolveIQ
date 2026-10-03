"""The rounding policy, in one place.

docs/SYSTEM_DESIGN.md §6.2 fixes four rounding boundaries and this module is the only
place any of them is applied. That concentration is the point: a rounding mode chosen
at a call site is a rounding mode nobody can audit, and two call sites choosing
differently is a cent-level discrepancy that looks like a billing bug.

The policy
----------

===============================  =================  ===================================
Boundary                         Mode               Rationale
===============================  =================  ===================================
Extended / unit-price amounts    exact, no rounding Preserve precision
Line item amount                 ``ROUND_HALF_UP``  Commercial billing convention
Invoice total                    ``ROUND_HALF_UP``  Matches the system being reconciled
Allocation / proration residual  ``ROUND_HALF_EVEN`` Minimises bias across many rows
===============================  =================  ===================================

The single non-obvious choice is ``ROUND_HALF_EVEN`` for residual distribution, and it
is deliberate: splitting one amount across many rows with ``ROUND_HALF_UP`` biases the
total upward every time. It applies inside :meth:`Money.allocate`, not in this engine,
and :func:`allocation_residual_mode` exists so a test can hold both to the same value.

Why extended amounts are left exact
-----------------------------------
A unit price is stored at four decimal places precisely so that a sub-cent rate such as
0.0007 per API call stays representable. Rounding a product such as
``31234 * 0.00070 = 21.86380`` to 21.86 before the tier ladder has finished walking
would change the answer whenever the ladder has more than one step, because the error
from each step would accumulate. So the exact value is carried to the line boundary and
rounded exactly once, there.

``ROUND_HALF_UP`` at the line boundary is the commercial convention: 0.005 rounds away
from zero, where Python's default ``ROUND_HALF_EVEN`` would round it to 0.00. A billing
system that rounds a half cent down is one an analyst will dispute.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP

from app.domain.money import Money

__all__ = [
    "ALLOCATION_RESIDUAL_MODE",
    "EXTENDED_AMOUNT_MODE",
    "INVOICE_TOTAL_MODE",
    "LINE_AMOUNT_MODE",
    "allocation_residual_mode",
    "round_invoice_total",
    "round_line_amount",
]

#: Line item amounts, per §6.2. See the module docstring for why this is not the
#: :mod:`decimal` default.
LINE_AMOUNT_MODE = ROUND_HALF_UP

#: Invoice totals, per §6.2. The same mode as line amounts because the invoice total is
#: reconciled against the statement the customer received.
INVOICE_TOTAL_MODE = ROUND_HALF_UP

#: Residual distribution when one amount is split across rows. Owned by
#: :meth:`Money.allocate`; exposed here so the policy has one home.
ALLOCATION_RESIDUAL_MODE = ROUND_HALF_EVEN

#: Extended amounts are not rounded at all. ``None`` rather than a rounding mode,
#: because there is no mode to apply and naming one would invite somebody to use it.
EXTENDED_AMOUNT_MODE: str | None = None


def round_line_amount(amount: Money) -> Money:
    """Round a computed line amount to the currency's minor unit.

    >>> round_line_amount(Money(Decimal("21.86380"), "USD"))
    Money(Decimal('21.86'), 'USD')
    >>> round_line_amount(Money(Decimal("21.865"), "USD"))
    Money(Decimal('21.87'), 'USD')
    """
    return amount.quantise(rounding=LINE_AMOUNT_MODE)


def round_invoice_total(amount: Money) -> Money:
    """Round an invoice total to the currency's minor unit.

    Applied even though summing already-rounded line amounts is exact, because the
    policy names this boundary explicitly and a reader should not have to prove to
    themselves that the call is redundant. The engine also asserts the resulting
    conservation property in its tests.
    """
    return amount.quantise(rounding=INVOICE_TOTAL_MODE)


def allocation_residual_mode() -> str:
    """The rounding mode :meth:`Money.allocate` must use.

    Returned as a function rather than only a constant so that the test pinning the
    policy reads as a statement about behaviour: it compares this value against the
    default that :meth:`Money.allocate` actually applies.
    """
    return ALLOCATION_RESIDUAL_MODE
