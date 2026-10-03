"""The investigation lifecycle: which stages ran, and how the run ended.

docs/SYSTEM_DESIGN.md §3.5 draws the state machine, and the part of it that matters
most is that ``COMPLETE``, ``DEGRADED`` and ``PARTIAL_FAILED`` are three distinct
outcomes rather than one outcome with a warning attached. FR-012 exists because a
degraded investigation presented as a clean one is worse than no investigation: a
reviewer who is told "complete" stops looking.

So the vocabulary lives here, in the innermost layer, as plain enums with no behaviour.
Nothing can quietly promote a degraded run, because promotion would mean writing a
different enum member and there is no code that decides that from a heuristic.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["InvestigationStage", "InvestigationStatus"]


class InvestigationStage(str, Enum):
    """The stages a run passes through, in order."""

    PENDING = "PENDING"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    INTERPRETING = "INTERPRETING"
    COMPUTING_IMPACT = "COMPUTING_IMPACT"
    PROPOSING_RESOLUTIONS = "PROPOSING_RESOLUTIONS"
    COMPLETE = "COMPLETE"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    DEGRADED = "DEGRADED"


class InvestigationStatus(str, Enum):
    """How a run ended.

    The distinction the API must preserve (FR-012):

    ``COMPLETE``
        Every stage finished and the evidence was sufficient. A reviewer can treat the
        output as a whole answer.

    ``DEGRADED``
        The run produced usable output with a stated caveat — evidence was incomplete,
        or the model was unavailable and the deterministic findings stand alone. Still
        an answer, but not the whole one.

    ``PARTIAL_FAILED``
        A stage failed in a way that lost its output. The model answered and the answer
        could not be used. Nothing was silently dropped: the failure is the result.
    """

    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
