"""INV-01 enforced mechanically: no binary float ever touches a monetary value.

docs/SYSTEM_DESIGN.md §3.2 requires that all monetary amounts use ``NUMERIC(19,4)`` and that
money is ``Decimal``, "never float". A requirement written in a document does not survive
contact with a codebase, so this walks the AST of the calculation path and fails on the
constructs that reintroduce a float.

What counts as a violation, and why each one is here
----------------------------------------------------
=========================  =========================================================
``0.1``                    A float literal. ``0.1`` is already inexact before it is
                           stored, so the damage is done at the point of writing.
``float(...)``             An explicit conversion, which is how floats usually arrive.
``round(x, 2)``            The builtin silently returns a float for a float argument.
                           ``Decimal.quantize`` is the only rounding INV-01 permits.
``2 / 3``                  A true division with an integer literal yields a float.
                           Division in the calculation path is always Decimal, which is
                           why bare int arithmetic is reported here.
=========================  =========================================================

Detection is necessarily incomplete for division: ``a / b`` cannot be classified without type
information, so only the literal form is reported. The second test below covers the
arithmetic side, and the ``Decimal`` discipline in the domain types covers the rest.

Why naming ``float`` is allowed in exactly one place
----------------------------------------------------
Two modules have to name ``float`` in order to reject it: ``isinstance(value, float)`` is how
a float is caught at the boundary, in both ``Money`` and the billing inputs. Forbidding the
identifier outright would forbid the guards that enforce the rule.

So the exemption is structural rather than a list of filenames: the ``float`` argument of an
``isinstance`` call is forgiven, and nothing else is. A ``float(...)`` conversion, a float
literal or an annotation still fails, in any module, including the two that reject floats.
A filename list would have needed editing every time a new boundary check was added, and
would have silently stopped applying to a renamed file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
CALCULATION_PATH = ("domain", "pricing")


def float_violations(tree: ast.AST, module_name: str) -> list[str]:
    """Every construct in ``tree`` that would put a binary float into a calculation."""
    findings: list[str] = []
    #: Identity of the ``float`` Name nodes that are an ``isinstance`` argument, and so are
    #: the guard rejecting floats rather than a use of them.
    guarded = _isinstance_float_guards(tree)
    del module_name  # Kept in the signature so callers name the module they are checking.

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            findings.append(f"line {node.lineno}: float literal {node.value!r}")

        elif isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            if called == "float":
                findings.append(f"line {node.lineno}: float() conversion")
            elif called == "round":
                findings.append(
                    f"line {node.lineno}: round() returns a float; use Decimal.quantize"
                )

        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # Decimal division is exact and must stay; a true division of ints is a float.
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, int):
                findings.append(
                    f"line {node.lineno}: int / int produces a float; divide Decimals instead"
                )

        elif isinstance(node, ast.Name) and node.id == "float" and id(node) not in guarded:
            findings.append(f"line {node.lineno}: references float")

    return findings


def _isinstance_float_guards(tree: ast.AST) -> set[int]:
    """Identity of every ``float`` name used inside an ``isinstance`` argument.

    Located structurally rather than by string or line matching so the exemption cannot
    quietly widen: only a ``float`` named within an ``isinstance`` call is forgiven, so a
    genuine ``float(value)`` or a float literal in the same function is still reported.

    The argument is walked recursively because the idiomatic form is a tuple,
    ``isinstance(value, (bool, float))``, and stopping at the top level would miss the
    ``float`` inside it the moment a formatter tidied the call.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "isinstance"):
            continue
        for argument in node.args:
            guarded.update(
                id(inner)
                for inner in ast.walk(argument)
                if isinstance(inner, ast.Name) and inner.id == "float"
            )
    return guarded


def calculation_modules() -> list[Path]:
    return sorted(path for layer in CALCULATION_PATH for path in (APP_ROOT / layer).rglob("*.py"))


def test_the_calculation_path_actually_exists() -> None:
    """Guards the rule: an empty glob would make every check below vacuously pass."""
    assert calculation_modules()


@pytest.mark.parametrize(
    "path", calculation_modules(), ids=lambda p: p.relative_to(APP_ROOT).as_posix()
)
def test_no_float_reaches_the_calculation_path(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    violations = float_violations(tree, path.name)

    assert violations == [], (
        f"{path.relative_to(APP_ROOT)} reintroduces binary floating point (INV-01):\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "path", calculation_modules(), ids=lambda p: p.relative_to(APP_ROOT).as_posix()
)
def test_every_monetary_arithmetic_result_is_decimal(path: Path) -> None:
    """Multiplication and division in the calculation path must be Decimal operations.

    Complements the checks above: it does not ask whether a float *type* appears, but whether
    the arithmetic could produce one. A module that only ever multiplies ``Decimal`` values is
    exact by construction, whatever the types of its inputs were.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    suspicious = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.Mult, ast.Div))
        and any(
            isinstance(side, ast.Constant)
            and isinstance(side.value, (int, float))
            and not isinstance(side.value, bool)
            for side in (node.left, node.right)
        )
    ]

    assert suspicious == [], (
        f"{path.relative_to(APP_ROOT)} multiplies a bare numeric literal, which reintroduces "
        "binary floating point (INV-01):\n" + "\n".join(suspicious)
    )


class TestTheGuardItself:
    """The check has to be able to fail, or enforcing it means nothing.

    A guard test that passes for the wrong reason is worse than no guard: it reports
    INV-01 as verified while being incapable of noticing a violation. Each case below feeds
    the checker a snippet it should reject, so a future rewrite that quietly stops detecting
    something fails here first.
    """

    @pytest.mark.parametrize(
        ("label", "source"),
        [
            ("a float literal", "amount = 0.1"),
            ("an explicit float conversion", "amount = float(value)"),
            ("the float builtin used for rounding", "amount = round(value, 2)"),
            ("a true division of integer literals", "amount = 2 / 3"),
            ("a bare float name", "amount = value.__float__() or 0.0"),
        ],
    )
    def test_it_detects_the_constructs_it_claims_to(self, label: str, source: str) -> None:
        assert float_violations(ast.parse(source), "probe.py"), f"not detected: {label}"

    @pytest.mark.parametrize(
        "source",
        [
            "amount = quantity * Decimal('0.00070')",
            "amount = Decimal(value) / Decimal(4)",
            "amount = rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)",
        ],
    )
    def test_it_leaves_exact_decimal_arithmetic_alone(self, source: str) -> None:
        """A guard that flagged Decimal arithmetic would be disabled within a week."""
        assert float_violations(ast.parse(source), "probe.py") == []

    def test_the_isinstance_exemption_does_not_excuse_an_actual_conversion(self) -> None:
        """The guard that *rejects* floats is forgiven; using one is still reported.

        Scoped by identity rather than by line so that adding a real conversion to the same
        function as the guard cannot slip through on a technicality.
        """
        source = "if isinstance(value, float):\n    raise ValueError\namount = float(value)"

        assert float_violations(ast.parse(source), "billing.py")

    def test_the_exemption_is_not_a_blanket_allowance(self) -> None:
        """A float used for anything other than an isinstance guard is reported everywhere.

        Including in the modules that reject floats, which is exactly the case a filename
        allowlist would have got wrong by omission.
        """
        source = "if isinstance(value, float):\n    raise ValueError\nother: float = 0.0"

        assert sorted(float_violations(ast.parse(source), "money.py")) == sorted(
            ["line 3: float literal 0.0", "line 3: references float"]
        )

    def test_the_exemption_survives_the_tuple_form_of_the_guard(self) -> None:
        """``isinstance(value, (bool, float))`` is the form a formatter leaves behind.

        Checking only the top level of the argument would report this as a violation, and the
        response would be to add a noqa rather than to fix the check.
        """
        source = "if isinstance(value, (bool, float)):\n    raise ValueError"

        assert float_violations(ast.parse(source), "billing.py") == []
