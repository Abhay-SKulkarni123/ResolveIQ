"""Freezing and thawing JSON-shaped data.

The aggregate is immutable, and the evidence snapshots and ingest payloads inside it
must be immutable too. ``freeze_json`` does that by replacing every ``dict`` with a
``MappingProxyType`` and every ``list`` with a ``tuple``, recursively; ``thaw_json``
undoes it.

**Why this is one module and not a helper in each layer.**

There were four copies of the thaw operation -- one in ``domain.evidence``, one in
``domain.cases``, one in ``services.source_document`` and one in the SQL repository --
each correct in isolation and each shallow at some call site. The cost was a real
defect: the API serialised an evidence snapshot with ``dict(item.snapshot)``, which
thaws only the outermost level, so a nested object survived as a ``mappingproxy`` and
pydantic raised ``Unable to serialize unknown type: <class 'mappingproxy'>`` on the
first investigation response. Every caller now uses the same function, and
``tests/unit/test_json_frozen.py`` fails if a second copy appears.

The freeze is depth-first for the reason given in ``domain.evidence``: a
``MappingProxyType`` over a dict whose values are still mutable dicts has the
*appearance* of immutability while leaving the interesting part writable.

Thawing is also depth-first, and that is what makes a document read out of the
database -- which arrives as ordinary JSON -- compare equal to one taken straight from
a frozen aggregate. ``DisputeCase.with_source_document`` depends on that equality to
decide whether an attach actually changed anything, so a shallow thaw would make every
re-attach look like a change and reopen cases that gained no facts.

Nothing here imports from the rest of the application: this is the innermost layer,
below ``domain``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["freeze_json", "thaw_json"]


def freeze_json(value: Any) -> Any:
    """Recursively replace dicts with read-only mappings and lists with tuples.

    Anything that is not a mapping or a sequence is returned unchanged, so scalars,
    ``Decimal`` and ``datetime`` pass through intact -- freezing must never alter a
    value, only the containers around it.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Recursively restore plain ``dict`` and ``list`` containers.

    Safe to call on data that was never frozen; it is the identity in that case, so
    callers do not need to know where a value came from. That matters because the
    three sources of a snapshot -- a request body, a frozen aggregate, and a JSONB
    column read back -- are all valid inputs to the same code path.

    ``tuple`` becomes ``list`` so that the result is JSON-shaped rather than merely
    iterable. ``json.dumps`` would serialise a tuple as an array anyway, but the
    in-memory form is what gets compared and hashed, and a tuple would compare unequal
    to the list it came from.
    """
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value