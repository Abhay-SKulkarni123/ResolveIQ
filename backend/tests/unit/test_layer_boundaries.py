"""The dependency rule, enforced mechanically.

docs/SOLID.md states the rule as a table: the innermost layers must not import
outward. A diagram in a document does not stop anyone, so this walks the AST of
every module in the inner layers and fails on a forbidden import.

The rule is checked against ``app/`` rather than against ``tests/``. That is
deliberate and is the one place this file refines SOLID.md's wording: a test for
the ORM adapter has to import the ORM adapter, so "tests/unit must import nothing
from adapters" cannot be enforced against the whole test tree without banning the
tests that verify the adapter. The invariant worth protecting is that *production*
inner code stays free of outer dependencies, and that is what is asserted here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Third-party modules the inner layers must never reach for. SQLAlchemy and
#: FastAPI are the two that matter: importing either would tie domain logic to a
#: persistence or transport framework.
FORBIDDEN_THIRD_PARTY = frozenset({"sqlalchemy", "alembic", "fastapi", "pydantic", "psycopg"})

#: app modules each inner layer may not import, from docs/SYSTEM_DESIGN.md §4.
#:
#: ``domain`` imports nothing but the standard library. ``pricing`` may import
#: ``domain`` and nothing else. Neither may reach ``adapters``, ``api``,
#: ``services``, ``config`` or ``main``.
FORBIDDEN_INTERNAL = {
    "domain": frozenset(
        {
            "app.adapters",
            "app.api",
            "app.config",
            "app.main",
            "app.ports",
            "app.pricing",
            "app.services",
        }
    ),
    "pricing": frozenset(
        {
            "app.adapters",
            "app.api",
            "app.config",
            "app.main",
            "app.ports",
            "app.services",
        }
    ),
}

LAYERS = ("domain", "pricing")


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import statement in ``path``, absolute or relative."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import inside app.<layer> can only name a sibling or
                # a subpackage of that same layer, so it is not a violation.
                continue
            if node.module:
                names.add(node.module)
    return names


def layer_modules(layer: str) -> list[Path]:
    return sorted(path for path in (APP_ROOT / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", LAYERS)
def test_the_layer_actually_exists(layer: str) -> None:
    """Guards the rule above: an empty glob would make every check vacuously pass."""
    assert layer_modules(layer), f"app/{layer} contains no Python modules"


@pytest.mark.parametrize("layer", LAYERS)
def test_inner_layers_do_not_import_outer_ones(layer: str) -> None:
    violations: list[str] = []
    forbidden_internal = FORBIDDEN_INTERNAL[layer]
    for path in layer_modules(layer):
        for name in sorted(imported_modules(path)):
            root = name.split(".")[0]
            forbidden = (
                root in FORBIDDEN_THIRD_PARTY
                or name in forbidden_internal
                or any(name.startswith(f"{blocked}.") for blocked in forbidden_internal)
            )
            if forbidden:
                violations.append(f"{path.relative_to(APP_ROOT)} imports {name}")
    assert violations == [], "dependency rule broken:\n" + "\n".join(violations)


def test_domain_does_not_import_pricing() -> None:
    """The innermost layer knows nothing about how money is eventually charged."""
    for path in layer_modules("domain"):
        for name in imported_modules(path):
            assert not name.startswith("app.pricing"), f"{path.name} imports {name}"


def test_the_persistence_adapter_is_only_reached_from_outside() -> None:
    """app.adapters must not be imported by domain or pricing, directly or not.

    This is the DIP assertion stated on its own so that a failure names the rule
    that was broken rather than a list of forbidden module names.
    """
    for layer in LAYERS:
        for path in layer_modules(layer):
            for name in imported_modules(path):
                assert not name.startswith("app.adapters"), f"{path.name} imports {name}"
