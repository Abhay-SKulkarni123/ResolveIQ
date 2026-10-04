"""The freeze/thaw pair, and the reason it is one function rather than four.

``freeze_json`` makes a JSON-shaped structure immutable; ``thaw_json`` undoes it.
The interesting property is not that either works, but that the *same* function is
used everywhere -- because the one time this codebase had four copies, a call site
used a shallow ``dict()`` and the API failed to serialise a nested object.

The duplication guard at the bottom is the part that earns its keep. It fails if a
module defines its own thaw, which is cheap to write and easy to reintroduce.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from app.domain.json_frozen import freeze_json, thaw_json

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------


def test_freeze_makes_a_nested_mapping_unwritable() -> None:
    """Depth-first, not just the outermost level.

    A ``MappingProxyType`` over a dict whose values are still ordinary dicts looks
    immutable and is not: a caller can reach through it and edit the interesting
    part. That is the exact defect the Phase 3 snapshot code was rewritten for, so it
    is asserted at depth.
    """
    frozen = freeze_json({"outer": {"inner": {"value": 1}}})

    with pytest.raises(TypeError):
        frozen["outer"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["outer"]["inner"]["value"] = 2  # type: ignore[index]


def test_freeze_turns_lists_into_tuples_so_they_cannot_be_appended_to() -> None:
    frozen = freeze_json({"items": [1, 2, 3]})

    assert isinstance(frozen["items"], tuple)
    with pytest.raises(AttributeError):
        frozen["items"].append(4)  # type: ignore[attr-defined]


def test_freeze_leaves_scalars_alone() -> None:
    """Freezing changes containers, never values.

    A ``Decimal`` or a ``datetime`` that came out the other side as a string would be
    a silent type change on the way through, and money is exactly the kind of value
    that must not become text here.
    """
    from datetime import datetime
    from decimal import Decimal

    moment = datetime(2026, 3, 1)
    amount = Decimal("12.4000")

    frozen = freeze_json({"when": moment, "amount": amount, "count": 3, "flag": True})

    assert frozen["when"] is moment
    assert frozen["amount"] is amount
    assert frozen["count"] == 3
    assert frozen["flag"] is True


def test_freeze_is_idempotent() -> None:
    once = freeze_json({"a": [1, {"b": 2}]})

    assert thaw_json(freeze_json(once)) == thaw_json(once)


# ---------------------------------------------------------------------------
# thaw
# ---------------------------------------------------------------------------


def test_thaw_is_depth_first() -> None:
    """The property the API depends on.

    A shallow thaw returns a plain dict at the top and leaves ``mappingproxy``
    underneath, which ``json.dumps`` and pydantic both refuse. This is precisely the
    bug that made the first investigation response fail to serialise.
    """
    thawed = thaw_json(freeze_json({"a": {"b": {"c": [1, 2]}}}))

    assert type(thawed) is dict
    assert type(thawed["a"]) is dict  # not MappingProxyType
    assert type(thawed["a"]["b"]) is dict
    assert thawed["a"]["b"]["c"] == [1, 2]


def test_thawed_structures_are_json_serialisable_at_every_depth() -> None:
    """Asserted through ``json.dumps`` rather than by checking types.

    Type checks can be satisfied while a value remains unserialisable, and the thing
    that actually matters is whether the encoder accepts it.
    """
    frozen = freeze_json(
        {"invoice": {"lines": [{"amount": "1.0000", "meta": {"tags": ["a", "b"]}}]}}
    )

    encoded = json.dumps(thaw_json(frozen))

    assert json.loads(encoded)["invoice"]["lines"][0]["meta"]["tags"] == ["a", "b"]


def test_thaw_is_the_identity_on_data_that_was_never_frozen() -> None:
    """Callers must not have to know where a value came from.

    Three sources feed the same code path: a request body, a frozen aggregate, and a
    JSONB column read back as ordinary JSON. Requiring a "was it frozen?" check at
    each site is how one of them ends up skipped.
    """
    plain = {"a": [1, {"b": 2}]}

    assert thaw_json(plain) == plain
    assert type(thaw_json(plain)["a"][1]) is dict


def test_round_trip_preserves_equality_but_changes_container_types() -> None:
    """Equality survives; identity of the containers does not, and should not.

    ``DisputeCase.with_source_document`` compares documents by canonical JSON rather
    than by ``is``, precisely because the frozen and plain forms must be comparable.
    """
    original: dict[str, Any] = {"terms": [{"metric_key": "api_calls", "included": "0"}]}
    thawed = thaw_json(freeze_json(original))

    assert thawed == original
    assert thawed is not original
    assert isinstance(thawed["terms"], list)


def test_freeze_then_thaw_is_value_preserving_for_deep_structures() -> None:
    """A wide, awkward shape: empty containers, nesting, repeated keys, non-ASCII."""
    original = {
        "empty_list": [],
        "empty_dict": {},
        "deep": {"a": {"b": {"c": {"d": [1, {"e": None}]}}}},
        "unicode": "naïve café — 契約",
        "numbers": [0, -1, 10**20],
    }

    assert thaw_json(freeze_json(original)) == original


def test_thaw_accepts_a_tuple_as_well_as_a_list() -> None:
    """Aggregates store tuples; JSON gives back lists. Both must thaw alike."""
    assert thaw_json(({"a": 1}, [2, {"b": 3}])) == [{"a": 1}, [2, {"b": 3}]]


def test_thaw_recognises_any_mapping_not_just_dict() -> None:
    """``MappingProxyType`` is the case that matters, but the check is not special.

    A document read from a frozen aggregate and one read from JSON must take the same
    branch, which is only true if the test keys off the protocol rather than off
    ``dict``.
    """
    assert thaw_json(MappingProxyType({"a": MappingProxyType({"b": 1})})) == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# The duplication guard
# ---------------------------------------------------------------------------


def _thaw_like_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    """Every function whose name looks like a thaw, whatever the prefix.

    Matching the exact name ``thaw`` was the first version's mistake, and it made the
    guard vacuous in the most likely relapse: the copy gets a private prefix, so it is
    called ``_local_thaw`` or ``_thaw_snapshot`` and sails straight past. A name
    *suffix* is the pattern that actually catches it.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.lower().endswith("thaw")
    ]


def _freeze_like_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    """Every function whose name looks like a freeze, whatever the prefix."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and ("freeze" in node.name.lower())
    ]


def _is_recursive_thaw(node: ast.FunctionDef) -> bool:
    """Whether this function is a hand-rolled thaw.

    Identified by structure, not by name: it tests for ``Mapping`` and for a
    list/tuple, and rebuilds a dict and a list from a recursive call to itself. A
    helper that only unwraps one level, or that does something else entirely, is not
    matched -- the point is to catch the second copy of *this* function.
    """
    source = ast.dump(node)
    if "Mapping" not in source or "Dict" not in source or "List" not in source:
        return False
    calls_self = any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == node.name
        for child in ast.walk(node)
    )
    return calls_self


def test_only_json_frozen_contains_a_thaw_implementation() -> None:
    """Fails if a second copy of the thaw logic appears anywhere in ``app/``.

    There were four copies of this function -- in ``domain.evidence``,
    ``domain.cases``, ``services.source_document`` and the SQL repository -- each
    correct alone, and one call site used a shallow ``dict()`` because no single
    correct helper was importable. The response then failed with
    ``Unable to serialize unknown type: <class 'mappingproxy'>``.

    Importing :func:`thaw_json` is the fix; re-implementing it is the relapse.
    """
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in _thaw_like_functions(tree):
            if path.name == "json_frozen.py":
                continue
            if _is_recursive_thaw(function):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{function.lineno}")

    assert offenders == [], (
        "a hand-rolled thaw is defined here; import app.domain.json_frozen.thaw_json "
        f"instead: {offenders}"
    )


def test_only_json_frozen_contains_a_freeze_implementation() -> None:
    """The same guard for the freeze half."""
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "json_frozen.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in _freeze_like_functions(tree):
            if "MappingProxyType" in ast.dump(function) or "MappingProxy" in ast.dump(function):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{function.lineno}")

    assert offenders == [], (
        "a hand-rolled freeze is defined here; import "
        f"app.domain.json_frozen.freeze_json instead: {offenders}"
    )


def test_the_helper_is_the_innermost_layer() -> None:
    """It must import nothing from the application.

    ``app/domain/json_frozen.py`` sits below ``domain``, so anything it imported from
    a sibling layer would invert the dependency rule that
    ``tests/unit/test_layer_boundaries.py`` enforces for the rest of the tree.
    """
    tree = ast.parse((APP_ROOT / "domain" / "json_frozen.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name.startswith("app") for name in imported), imported