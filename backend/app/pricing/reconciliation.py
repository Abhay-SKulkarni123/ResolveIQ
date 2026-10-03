"""What the customer still owes, once the recalculation, payments and credits are known.

Separate from :mod:`app.pricing.engine` on purpose
---------------------------------------------------
Recalculation answers "does the arithmetic support this invoice?". Reconciliation answers
"what is left to pay?". They are kept apart because they are checkable in different ways:
a recalculation can be re-derived from the contract and usage alone, whereas a balance also
depends on events that happened *after* the invoice — a payment arriving late, a goodwill
credit, a disputed line being written off. A module that produced both would make it
impossible to ask the first question without also asserting something about the second.

Only the recalculated total is used
-----------------------------------
The balance is built from the figure the contract supports, not the figure the invoice
stated. Using the stated total would make the two agree by construction, and the whole
point of the exercise would be lost. Where the recalculation could not price every line the
result is marked :attr:`OutstandingBalance.is_provisional`, because the total behind it is
then a lower bound rather than an answer.

Three things this module refuses to do
--------------------------------------
* **Clamp an overpayment to zero.** A negative balance is money the customer is owed. Zeroing
  it destroys the fact that a refund is due and turns a payable-into-customer situation into
  a clean-looking settled one.
* **Guess an unallocated payment's invoice.** A payment with no recorded allocation is
  reported as unapplied rather than applied to whichever invoice is open. §6.3 lists
  ``UNAPPLIED_PAYMENT`` as a cause of dispute precisely because the source system held the
  money without attaching it.
* **Convert currency.** Everything must already be in the invoice's currency (OQ-04).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.billing import Adjustment, Payment
from app.domain.money import CurrencyMismatchError, Money
from app.pricing.results import InvoiceRecalculation

__all__ = [
    "OutstandingBalance",
    "reconcile_balance",
    "unallocated_payment_total",
]


def unallocated_payment_total(payments: list[Payment], currency: str) -> Money:
    """Money received with no allocations recorded at all.

    The complement of :attr:`OutstandingBalance.unapplied_payment_total`, and a distinct
    condition from it. ``_unapplied_payments`` counts money that was allocated to some
    *other* invoice; this counts money nobody allocated anywhere. The investigation needs
    both, because they call for different follow-up — one is a misdirection, the other is
    an unapplied remittance — and a report that conflated them would send an analyst to
    the wrong record.

    Public so the ``UNAPPLIED_PAYMENT`` impact calculator can report the figure without
    re-summing the payments itself. The arithmetic stays here, next to the other balance
    arithmetic, so there is one place where it can be wrong.
    """
    total = Money.zero(currency)
    for payment in payments:
        if not payment.allocations:
            total = total + payment.amount
    return total


@dataclass(frozen=True)
class OutstandingBalance:
    """How much is still owed on an invoice, and the figures it was built from.

    Every component is retained rather than only the answer. A customer disputing a balance
    needs to see that the payment was applied and the credit was honoured, and a balance
    presented as a lone number invites the suspicion that something was netted off
    invisibly.
    """

    invoice_external_id: str
    currency: str
    #: What the contract says the invoice was worth.
    recalculated_total: Money
    #: Payments recorded against this invoice, and only this invoice.
    allocated_payments: Money
    #: Signed sum of adjustments: positive for credits, negative for surcharges.
    net_adjustments: Money
    #: ``recalculated_total - allocated_payments - net_adjustments``.
    outstanding: Money
    #: Payments that exist but are not applied to this invoice.
    unapplied_payment_total: Money
    #: ``False`` when at least one line could not be recalculated, making the total a
    #: lower bound and the outstanding figure provisional.
    is_provisional: bool
    #: Metrics that could not be recalculated, for the report to name.
    unresolved_metrics: tuple[str, ...]

    @property
    def is_settled(self) -> bool:
        """Whether the invoice is exactly covered.

        Requires a non-provisional balance: an invoice whose total could not be verified
        is not known to be settled, however small the provisional figure is.
        """
        return not self.is_provisional and self.outstanding.is_zero()

    @property
    def is_credit(self) -> bool:
        """Whether the customer is owed money rather than owing it."""
        return self.outstanding.is_negative()


def reconcile_balance(
    recalculation: InvoiceRecalculation,
    payments: list[Payment],
    adjustments: list[Adjustment],
) -> OutstandingBalance:
    """Work out what is outstanding on a recalculated invoice.

    Payments are matched to the invoice by explicit allocation. Only the portion of a
    payment allocated to this invoice reduces its balance, so a single remittance settling
    several invoices cannot be double-counted against any one of them.

    Raises:
        CurrencyMismatchError: if a payment or adjustment is in another currency. ResolveIQ
            does no FX conversion (OQ-04), so the sum would be meaningless.
    """
    invoice_id = recalculation.invoice_external_id
    currency = recalculation.currency

    allocated = Money.zero(currency)
    for payment in payments:
        _require_currency(payment.amount.currency, currency, f"payment {payment.external_id}")
        applied = payment.allocated_to(invoice_id)
        if applied is not None:
            _require_currency(applied.currency, currency, f"allocation from {payment.external_id}")
            allocated = allocated + applied

    net_adjustments = Money.zero(currency)
    for adjustment in adjustments:
        _require_currency(
            adjustment.amount.currency, currency, f"adjustment {adjustment.external_id}"
        )
        # Signed addition: a credit is positive and reduces the balance, a surcharge is
        # negative and increases it. One formula covers both directions, so neither can be
        # handled correctly while the other is handled backwards.
        net_adjustments = net_adjustments + adjustment.amount

    outstanding = recalculation.calculated_total - allocated - net_adjustments

    return OutstandingBalance(
        invoice_external_id=invoice_id,
        currency=currency,
        recalculated_total=recalculation.calculated_total,
        allocated_payments=allocated,
        net_adjustments=net_adjustments,
        outstanding=outstanding,
        unapplied_payment_total=_unapplied_payments(payments, invoice_id, currency),
        is_provisional=not recalculation.is_complete,
        unresolved_metrics=recalculation.unresolved_metrics,
    )


def _unapplied_payments(payments: list[Payment], invoice_external_id: str, currency: str) -> Money:
    """Money received that is not applied to this invoice.

    Counts a payment only when it has allocations recorded and none of them is to this
    invoice. A payment with no allocations at all is *unallocated*, a different condition
    from one allocated elsewhere, and the investigation distinguishes the two: the first
    means nobody applied the money, the second means it was spent on something else.
    """
    total = Money.zero(currency)
    for payment in payments:
        if payment.allocations and payment.allocated_to(invoice_external_id) is None:
            total = total + payment.amount
    return total


def _require_currency(actual: str, expected: str, subject: str) -> None:
    if actual != expected:
        raise CurrencyMismatchError(
            f"{subject} is in {actual} but the invoice is in {expected}; ResolveIQ performs "
            "no implicit currency conversion (OQ-04)"
        )
