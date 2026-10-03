"""Vocabulary for contracts and their price terms.

``ContractStatus`` answers "what does this contract mean right now?" and
``BillingMode`` answers "how is this metric charged?". Both are domain concepts,
not database details, so they live here rather than beside the ORM.

Why they are not defined in ``app/adapters/persistence/models.py``: the services
that open an investigation and the calculators in ``app.pricing`` must be able to
compare a contract status without importing the ORM adapter. The dependency in
this architecture points inward (docs/SOLID.md), so the vocabulary belongs in the
innermost layer that needs it and the adapter maps onto it.

The ``*_VALUES`` tuples are derived from the enums so the ``CHECK`` constraints
in the database and the Python types can never drift apart: adding a member to an
enum changes both, and a mismatch fails the metadata test in
``tests/unit/test_persistence_models.py``.

Scope note
----------
The design documents name ``status`` and ``billing_mode`` as columns but do not
enumerate their legal values. The members below are the minimum set the rest of
the system can act on; see docs/SYSTEM_DESIGN.md §3.2 "Open schema questions".
They are plain ``CHECK``-constrained strings rather than native PostgreSQL enums,
because relaxing a check is a cheap migration and changing an enum type is not.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "BILLING_MODE_VALUES",
    "CONTRACT_STATUS_VALUES",
    "BillingMode",
    "ContractStatus",
]


class ContractStatus(str, Enum):
    """Lifecycle state of a contract.

    A contract's commercial terms are effective-dated rather than mutated: a
    change of terms produces a new row with the new ``effective_from`` and leaves
    the previous row ``SUPERSEDED``. That is what lets a dispute raised today be
    priced against the terms that actually applied when the usage occurred.
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    TERMINATED = "TERMINATED"


class BillingMode(str, Enum):
    """How a metered quantity is charged.

    The calculators that implement these modes are named in docs/SYSTEM_DESIGN.md
    §6.3 (``unit_price``, ``tiered``, ``commitment``) but are themselves Phase 2
    work. Only the persisted vocabulary is defined here.
    """

    #: Flat price multiplied by the quantity.
    PER_UNIT = "PER_UNIT"
    #: Progressive ladder of thresholds, each with its own unit price.
    TIERED = "TIERED"
    #: Floor that must be paid regardless of usage, above which normal rates apply.
    COMMITMENT = "COMMITMENT"


CONTRACT_STATUS_VALUES: tuple[str, ...] = tuple(status.value for status in ContractStatus)
BILLING_MODE_VALUES: tuple[str, ...] = tuple(mode.value for mode in BillingMode)
