"""How much machinery a request is allowed to use.

Lane started in `brain.models.routing`, whose docstring said it would move here when a
second subsystem needed to branch on it. The gate is that second subsystem: it classifies
the lane at ingress, long before anything decides which model answers, and the gate must
not import from the routing layer to learn what a lane is.

The three lanes are a budget, not a quality setting. Each one names what a request may
spend and therefore what it may promise.

Task ids: M3.1.1, M3.6.1
"""

from __future__ import annotations

import enum


class Lane(enum.StrEnum):
    """The budget a request is admitted under."""

    #: No model at all. Admission is exact intent match with every required slot present;
    #: a fuzzy near-match in a lane with no model in the loop produces a confidently wrong
    #: answer with nothing downstream able to catch it.
    FAST = "fast"
    #: A person is waiting. Roughly 95% of traffic.
    ANSWER = "answer"
    #: Autonomous multi-step work with tool use. Nobody is watching the spinner.
    TASK = "task"
