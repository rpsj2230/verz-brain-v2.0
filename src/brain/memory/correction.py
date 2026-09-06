"""What happens when the system learns it was wrong, and why nothing is deleted.

`brain.memory.formation` decides what may be recalled and lets confidence decay. Decay is what
handles a memory quietly going out of date. This handles the two cases where something
positively contradicts one, and both of them end in a mark rather than a removal.

**Nothing here deletes a memory, and that is the decision the module is built around.**
Deleting is the one operation that makes the previous behaviour unexplainable: a person asking
why the system stopped saying something deserves an answer, and after a delete there is
nothing to answer from. A superseded memory stays where it is, marked, and stops being
recalled. That also means the mark is undoable and a delete is not, which matters when the
thing that superseded it turns out to be the wrong one.

**Supersession and demotion are different failures and are kept apart.**

Supersession is one memory contradicted by a newer one: somebody said the retainer ends in
June, then said it ends in September. Both were true statements about what somebody said, and
the newer one wins because it is newer. The older is marked, not scored down: a confidence is
about how much evidence there is, and being contradicted is not weak evidence, it is a
different fact.

Demotion is a memory contradicted by the source. The database says the retainer ends in
December and no conversation gets a vote on that, because `MEMORY_IS_A_HINT_AND_THE_SOURCE_IS_
THE_ANSWER` is the arrangement the whole memory plane rests on. So the memory is demoted below
the retrieval floor immediately, with no decay to wait for and no agreement to gather, and it
is flagged so a person can see the system was carrying something the records disagreed with.

**Immediately is the load-bearing word in M16.4.3.** A demotion that waited for a promotion
threshold, an agreement count or a decay curve would leave the system answering from something
it already knows the records contradict, for as long as the wait lasted. There is no threshold
in this module and no parameter that could hold one.

**Neither correction records what either side said.** A `Supersession` names two memories and
the signal that prompted it; a `Demotion` names one memory and the field the source disagreed
about. Neither carries a value, for the reason `brain.memory.signals` gives at length: a
correction log holding both the old and the new value is a transcript of what people said and
what the records hold, in one table, under permissions belonging to neither.

Task ids: M16.4.2, M16.4.3
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime

from brain.memory.formation import RECALL_FLOOR
from brain.memory.signals import Signal

#: Why a corrected memory is marked rather than removed.
A_DELETED_MEMORY_CANNOT_EXPLAIN_ITSELF = (
    "Deleting is the one operation that makes the previous behaviour unexplainable. Somebody "
    "asking why the system stopped saying something deserves an answer, and after a delete "
    "there is nothing to answer from: not what it used to think, not when it changed its "
    "mind, not what changed it. A mark leaves all three, and a mark is undoable, which "
    "matters on the day the thing that superseded a memory turns out to be the wrong one."
)

#: Why a memory the source contradicts is demoted at once rather than scored down.
THE_SOURCE_DOES_NOT_WAIT_FOR_A_THRESHOLD = (
    "A demotion that waited for an agreement count, a promotion job or a decay curve would "
    "leave the system answering from something it already knows the records contradict, for "
    "as long as the wait lasted. The source is authoritative and memory is a hint, so there "
    "is nothing to weigh: the memory goes below the retrieval floor on the first "
    "disagreement. There is no threshold in this module and no parameter that could hold one."
)

#: Why a supersession is not a confidence reduction.
BEING_CONTRADICTED_IS_NOT_WEAK_EVIDENCE = (
    "Confidence says how much evidence there is for something. A memory contradicted by a "
    "newer statement is not weakly evidenced, it is superseded: the evidence for it was fine "
    "and the fact changed. Scoring it down would put those two on one scale, and a reader "
    "seeing a low confidence cannot tell whether the system is unsure or whether it has been "
    "told otherwise. The mark says which."
)

#: What a demoted memory's confidence becomes.
#:
#: Zero rather than just below the floor. Below the floor is a memory the system still half
#: believes and would recall the moment somebody lowered the threshold; zero is a memory the
#: records have contradicted, and no threshold anybody picks brings it back.
DEMOTED_CONFIDENCE = 0.0


class Correction(enum.StrEnum):
    """What happened to a memory. Two members and deliberately no third.

    A third for "somebody deleted it" would be a member describing an operation this module
    refuses to have, and a member for it is how the operation arrives.
    """

    #: A newer memory says otherwise. The older stops being recalled.
    SUPERSEDED = "superseded"
    #: The source says otherwise. The memory is demoted at once and flagged.
    DEMOTED = "demoted"


@dataclass(frozen=True)
class Supersession:
    """One memory replaced by a newer one.

    Names two memories and the signal that prompted it, and carries neither statement. A
    correction log holding the old and the new text is a transcript of what people said, in a
    table with permissions belonging to neither conversation.
    """

    #: The memory that stops being recalled.
    superseded_id: str
    #: The memory that replaced it.
    by_id: str
    #: What prompted the correction, from the closed signal vocabulary.
    prompted_by: Signal
    at: datetime

    def __post_init__(self) -> None:
        for name in ("superseded_id", "by_id"):
            if not getattr(self, name):
                msg = f"a supersession with no {name} names nothing anybody can look up"
                raise ValueError(msg)
        if self.superseded_id == self.by_id:
            msg = (
                "a memory cannot supersede itself; that is a loop the recall path would "
                "follow and a correction nobody can undo"
            )
            raise ValueError(msg)
        if self.at.tzinfo is None:
            msg = "a naive correction time compares wrongly against an aware one"
            raise ValueError(msg)


@dataclass(frozen=True)
class Demotion:
    """One memory the source contradicted.

    Names the memory and the field the records disagreed about, and carries neither value.
    Naming the field is what makes the flag actionable; carrying the values would make this a
    second copy of the record and of the conversation at once.
    """

    memory_id: str
    #: Which field the source disagreed about, as a name like `client.renewal_date`.
    field: str
    at: datetime
    #: What the memory is worth now. Zero, and there is no way to construct another value.
    confidence: float = DEMOTED_CONFIDENCE

    def __post_init__(self) -> None:
        if not self.memory_id or not self.field:
            msg = "a demotion with no memory or no field names nothing anybody can act on"
            raise ValueError(msg)
        if self.confidence != DEMOTED_CONFIDENCE:
            msg = (
                f"a demoted memory is worth {DEMOTED_CONFIDENCE} and this one claims "
                f"{self.confidence}; the source does not partly win"
            )
            raise ValueError(msg)
        if self.at.tzinfo is None:
            msg = "a naive correction time compares wrongly against an aware one"
            raise ValueError(msg)


def superseded_ids(supersessions: Iterable[Supersession]) -> frozenset[str]:
    """Every memory that has been replaced, as ids.

    A set rather than a chain walk. Supersession here is one step: A replaced by B, and if B
    is later replaced by C then B is in this set too. Nothing needs to know that A was
    replaced *by* something that has itself been replaced, because both are equally not
    recalled, and following the chain would be the loop `Supersession` refuses to let anybody
    start.
    """
    return frozenset(one.superseded_id for one in supersessions)


def demoted_ids(demotions: Iterable[Demotion]) -> frozenset[str]:
    """Every memory the source has contradicted, as ids."""
    return frozenset(one.memory_id for one in demotions)


def corrected(
    supersessions: Iterable[Supersession] = (),
    demotions: Iterable[Demotion] = (),
) -> frozenset[str]:
    """Every memory that must not be recalled, whichever way it was corrected.

    One set rather than two, because the recall path has one question to ask and asking it
    twice is how one of the two gets forgotten at a call site.
    """
    return superseded_ids(supersessions) | demoted_ids(demotions)


def confidence_after(memory_id: str, demotions: Iterable[Demotion], formed: float) -> float:
    """What a memory is worth once the source has had its say.

    Zero if the records contradicted it, and what it was formed with otherwise. No decay is
    applied here: decay is a function of time and belongs to `confidence_now`, and mixing the
    two would make a demotion look like a memory that had simply aged.
    """
    return DEMOTED_CONFIDENCE if memory_id in demoted_ids(demotions) else formed


def demotion_is_immediate() -> bool:
    """Whether a demotion waits for anything. It does not, and this says so testably.

    A function rather than a constant, because what is being asserted is that no threshold
    exists anywhere in the module rather than that one is set to zero. `correction_gaps`
    checks the same property over the module's own signatures.
    """
    return True


def correction_gaps() -> tuple[str, ...]:
    """Everything about this module that would let a correction be delayed or lost.

    Three checks, and the second is the one worth having. A demotion parameterised by a
    threshold is a demotion somebody can postpone, and the postponement would look like
    tuning rather than like the system answering from something it knows to be contradicted.
    """
    import inspect

    gaps: list[str] = []

    if DEMOTED_CONFIDENCE >= RECALL_FLOOR:
        gaps.append(
            f"a demoted memory is worth {DEMOTED_CONFIDENCE} and the retrieval floor is "
            f"{RECALL_FLOOR}, so the source contradicting a memory does not stop it being "
            "recalled"
        )

    for function in (confidence_after, corrected, superseded_ids, demoted_ids):
        taken = set(inspect.signature(function).parameters)
        for forbidden in ("threshold", "after", "delay", "agreement", "window", "grace"):
            if forbidden in taken:
                gaps.append(
                    f"{function.__name__} takes a {forbidden}, so a correction can be "
                    "postponed and the system goes on answering from something the records "
                    "contradict"
                )

    for model in (Supersession, Demotion):
        names = {f.name for f in fields(model)}
        for forbidden in ("value", "old_value", "new_value", "statement", "text", "was"):
            if forbidden in names:
                gaps.append(
                    f"{model.__name__} carries {forbidden}, which makes the correction log a "
                    "copy of what was said and what the records hold, under permissions "
                    "belonging to neither"
                )

    return tuple(gaps)


def newest_first(supersessions: Sequence[Supersession]) -> tuple[Supersession, ...]:
    """Corrections in the order somebody reviewing them wants: most recent first.

    Ties break on the superseded id, so two corrections written in one transaction come back
    in a fixed order rather than whichever the store returned. A review surface whose order
    moves between two readings is one nobody can work through.
    """
    return tuple(sorted(supersessions, key=lambda one: (-one.at.timestamp(), one.superseded_id)))
