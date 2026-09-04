"""The error taxonomy.

Five outcomes, and the distinction between the first two is the one that matters:

- DENIED   — it exists and you may not see it
- ABSENT   — it does not exist, or no grant covers it, and we will not say which

Every user-facing path must collapse DENIED into ABSENT before it reaches a person.
Otherwise "you are not allowed to see the contract value for SNM" confirms that SNM has
a contract value, and the permission model leaks by its own error messages. DENIED exists
only so the audit log can record what actually happened.

Task ids: M0.2.7
"""

from __future__ import annotations

import enum


class Outcome(enum.StrEnum):
    DENIED = "denied"
    ABSENT = "absent"
    UNRESOLVED = "unresolved"
    DEGRADED = "degraded"
    FAILED = "failed"


class BrainError(Exception):
    """Base for everything the gate raises. Carries an outcome, never a bare string."""

    outcome: Outcome = Outcome.FAILED
    #: what a person is allowed to be told
    public_message: str = "Something went wrong."

    def __init__(self, detail: str = "", *, public_message: str | None = None) -> None:
        super().__init__(detail or self.public_message)
        self.detail = detail
        if public_message is not None:
            self.public_message = public_message


class Denied(BrainError):
    """The thing exists and the caller may not see it. Never surfaced verbatim."""

    outcome = Outcome.DENIED
    public_message = "I could not find that."


class Absent(BrainError):
    """It does not exist, or nothing the caller holds reaches it."""

    outcome = Outcome.ABSENT
    public_message = "I could not find that."


class Unresolved(BrainError):
    """The name did not resolve to exactly one entity."""

    outcome = Outcome.UNRESOLVED
    public_message = "I found more than one match and could not tell which you meant."


class Degraded(BrainError):
    """A source was unreachable. We say so; we never substitute a stale value."""

    outcome = Outcome.DEGRADED
    public_message = "I could not reach one of the systems needed to answer that."


class Failed(BrainError):
    outcome = Outcome.FAILED


def to_public(error: BrainError) -> str:
    """Collapse DENIED into ABSENT on the way out.

    This is the only function that should ever produce a message for a person, and it is
    why `Denied` and `Absent` share a public message.
    """
    return error.public_message
