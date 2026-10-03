"""The calculation trace: how a number was arrived at, step by step.

docs/SYSTEM_DESIGN.md §6.4 requires that every computed amount carries an ordered,
serialisable trace, and FR-005 says why: an analyst disputing a figure must be able to
find the exact step that produced it, together with a reference to the rule that was
applied.

The shape implemented here is the one in the design document::

    {
      "steps": [
        {"n": 1, "label": "Total metered quantity in period",
         "expression": "sum(...)", "inputs": {"quantity": "41234"}, "result": "41234"},
        {"n": 4, "label": "Round line amount (ROUND_HALF_UP)", "result": "21.86"}
      ],
      "rule_ref": "contract:CTR-5512#term:api_calls",
      "engine_version": "1.0.0"
    }

Why ``inputs`` is a tuple of pairs
---------------------------------
A frozen dataclass holding a ``dict`` is not hashable and not genuinely immutable; a
caller could mutate a trace that a result claims to be fixed. A tuple of pairs is
immutable, ordered, and compares by value, so a :class:`CalculationTrace` can be used
in a set or asserted against a literal in a test without defensive copying. The
``as_dict`` method converts to the JSON shape above at the edge, which is the only
place a mapping is wanted.

Why step numbers are assigned by a builder
------------------------------------------
Numbering steps by hand means every insertion shifts the numbers and every number can be
wrong. :class:`TraceBuilder` assigns them on :meth:`TraceBuilder.step`, so a rule
describes what happened and never where it happened to sit in the list.

This module holds no financial logic. It records arithmetic that has already been
performed by :mod:`app.pricing.rules`; keeping it separate is what stops a formatting
concern from growing into a second place where an amount is computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ENGINE_VERSION",
    "CalculationTrace",
    "TraceBuilder",
    "TraceStep",
]

#: Recorded on every trace so that a figure can be tied to the code that produced it.
#: Bumped when a rule's arithmetic changes in a way that could move an amount, because
#: a trace produced by different arithmetic is not comparable with one produced by this.
ENGINE_VERSION = "1.0.0"

#: A step's inputs, as ordered name/value pairs. Values are strings because a trace is
#: read by a person and serialised to JSON; the exact :class:`~decimal.Decimal` is
#: already preserved by ``result`` and by the amount the caller holds.
Inputs = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TraceStep:
    """One arithmetic step, in the order it was performed."""

    n: int
    label: str
    expression: str
    result: str
    inputs: Inputs = ()

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape from §6.4."""
        payload: dict[str, Any] = {
            "n": self.n,
            "label": self.label,
            "expression": self.expression,
            "result": self.result,
        }
        if self.inputs:
            payload["inputs"] = dict(self.inputs)
        return payload


@dataclass(frozen=True)
class CalculationTrace:
    """The full derivation of one amount."""

    steps: tuple[TraceStep, ...]
    rule_ref: str
    engine_version: str = ENGINE_VERSION

    @property
    def final_result(self) -> str:
        """The result of the last step, which is the amount the trace explains."""
        if not self.steps:
            raise ValueError("a calculation trace must contain at least one step")
        return self.steps[-1].result

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape from §6.4, ready for ``resolution_options.calculation_trace``."""
        return {
            "steps": [step.as_dict() for step in self.steps],
            "rule_ref": self.rule_ref,
            "engine_version": self.engine_version,
        }

    def describe(self) -> str:
        """A readable rendering, for logs and for asserting on a whole trace at once.

        >>> TraceBuilder(rule_ref="contract:CTR-1#term:api_calls") \\
        ...     .step("Charge billable units", "31234 * 0.0007", "21.86380") \\
        ...     .build().describe()
        'contract:CTR-1#term:api_calls (engine 1.0.0)\\n  1. Charge billable units: 31234 * 0.0007 = 21.86380'
        """
        lines = [f"{self.rule_ref} (engine {self.engine_version})"]
        lines += [
            f"  {step.n}. {step.label}: {step.expression} = {step.result}" for step in self.steps
        ]
        return "\n".join(lines)


@dataclass
class TraceBuilder:
    """Accumulates steps and assigns their numbers.

    Mutable by design: it is a local construction aid used while a rule runs, and the
    immutable :class:`CalculationTrace` it produces is what leaves the engine.
    """

    rule_ref: str
    steps: list[TraceStep] = field(default_factory=list)

    def step(
        self,
        label: str,
        expression: str,
        result: str,
        **inputs: str,
    ) -> TraceBuilder:
        """Record one step and return ``self`` so calls can be chained.

        ``inputs`` are keyword-only, which makes each one self-labelling at the call
        site: ``inputs(quantity="41234")`` reads correctly, where a positional
        ``"41234"`` in a trace would not.
        """
        self.steps.append(
            TraceStep(
                n=len(self.steps) + 1,
                label=label,
                expression=expression,
                result=result,
                inputs=tuple(inputs.items()),
            )
        )
        return self

    def build(self) -> CalculationTrace:
        """Freeze the accumulated steps into an immutable trace."""
        if not self.steps:
            raise ValueError(
                "refusing to build an empty calculation trace: a recorded amount must "
                "be explainable, and an empty trace explains nothing"
            )
        return CalculationTrace(steps=tuple(self.steps), rule_ref=self.rule_ref)
