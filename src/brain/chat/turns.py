"""One turn of a conversation: what is shown, what is carried forward, what a correction says.

**The rule this module exists for: retained context carries no reach.** Everything else in
the system answers fresh against the caller's entitlement at the moment of asking. A
transcript does not - it is a copy of an answer, sitting where the next question can reach
it, after the grant that justified it may have been revoked. A follow-up answered "from
context" is a follow-up answered at last week's permissions.

So a `Turn` keeps record *identifiers* and never record *contents*, and `context_for`
re-checks every one of them against the entitlement of the turn being asked now. What
survives is what the asker may still see; what does not survive simply is not mentioned.

**A correction records that an answer was wrong, never what the right answer is.** The
obvious design stores the user's corrected text as a fact and reuses it, which turns the
chat box into a write path into the knowledge base with no review, no scope and no
provenance - anybody who can talk to the assistant can teach it something. What is stored
is the shape of the disagreement.

Task ids: M9.2.1, M9.2.2, M9.2.3, M9.2.4
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.redaction import ChannelPayload, LockedField, render_lock

#: The most turns a follow-up may look back through. Not a memory limit: a bound on how
#: far a question can reach without saying so. "What about the other one?" twenty turns
#: later is a question about something the asker has forgotten the details of too, and
#: answering it from a record they can no longer see is the failure this module is about.
CONTEXT_DEPTH = 6


class TurnKind(enum.StrEnum):
    """What a turn is. Closed, because each kind is treated differently downstream and a
    new one invented in a code path would be handled by nothing."""

    QUESTION = "question"
    ANSWER = "answer"
    CORRECTION = "correction"


@dataclass(frozen=True)
class RecordRef:
    """A record an answer drew on: what it was, not what it said.

    There is no field here for a value, and that is the mechanism. A reference the asker may
    no longer resolve is a reference that yields nothing on the next turn; a *copy* of the
    record would still read perfectly after the grant behind it was revoked, and nothing
    about the transcript would look wrong.
    """

    entity: str
    record_id: str
    #: What the asker needed in order to see this record at all. Re-checked on every later
    #: turn, so a reference cannot outlive the grant that justified it.
    required: Capability


@dataclass(frozen=True)
class Turn:
    """One question or answer, and the references behind it.

    Frozen. A turn that could be edited after the fact is a transcript that cannot be
    trusted as a record of what was said, which is the only reason to keep one.
    """

    kind: TurnKind
    at: datetime
    principal_id: str
    text: str = ""
    refs: tuple[RecordRef, ...] = ()
    #: Fields withheld from records this turn showed. Carried so a follow-up knows a lock
    #: was already explained and does not re-explain it, never so the lock can be lifted.
    locked: tuple[LockedField, ...] = ()

    @property
    def is_answer(self) -> bool:
        return self.kind is TurnKind.ANSWER


@dataclass(frozen=True)
class ShownAnswer:
    """What a channel renders: records, locks, and nothing that explains a refusal.

    Assembled from a `ChannelPayload` rather than from anything the model wrote (M9.2.1).
    A model asked to summarise its own tool results will fill a gap with something
    plausible, and a plausible sentence about a record the caller could not see is
    indistinguishable from a real one.
    """

    records: tuple[dict[str, object], ...] = ()
    locks: tuple[str, ...] = ()
    #: Set when every record was withheld. The sentence is the abstention path's, not one
    #: composed here, so there is one wording rather than one per channel.
    empty: bool = False

    @property
    def lock_count(self) -> int:
        return len(self.locks)


def assemble(payload: ChannelPayload) -> ShownAnswer:
    """Turn a redacted payload into what a channel shows (M9.2.1, M9.2.2).

    Every lock renders through `render_lock`, which takes no arguments. That signature is
    the guarantee: a lock that varied by field, by reason or by viewer would let two people
    comparing screens work out which of them was refused and why. Rendering them here from
    a local string would reintroduce exactly that, one channel at a time.
    """
    return ShownAnswer(
        records=tuple(dict(r) for r in payload.records),
        locks=tuple(render_lock() for _ in payload.locked),
        empty=not payload.records,
    )


def context_for(
    history: Sequence[Turn],
    entitlement: EntitlementSet,
    *,
    now: datetime,
    depth: int = CONTEXT_DEPTH,
) -> tuple[RecordRef, ...]:
    """The references a follow-up may draw on, re-checked against reach *now* (M9.2.3).

    This is the whole point of the module. The obvious implementation returns the last few
    turns' references and lets the retrieval layer worry about permissions; that is wrong in
    a way nothing downstream can fix, because by then the reference has already been put in
    front of the model as an established fact, and a model told "the client is Acme" does
    not stop to ask whether it still may know that.

    Re-checked rather than filtered on the way in: a grant revoked between two turns has to
    take effect on the second one. Checking at write time would freeze the answer at the
    moment the transcript was written, which is the freezing this module exists to prevent.

    `now` is required rather than defaulted, and that is deliberate. `holds` takes a time
    because a grant can expire, and a contractor whose access ended on Friday must not have
    Thursday's references survive into Monday's follow-up. A default of `datetime.now` would
    work and would also let a caller forget the argument exists; requiring it means the one
    place that could get expiry wrong has to say what time it thinks it is.

    Deduplicated, keeping the most recent mention, so a record referred to in four turns
    does not enter the prompt four times.
    """
    seen: dict[tuple[str, str], RecordRef] = {}
    for turn in reversed(list(history)[-depth:]):
        for ref in turn.refs:
            key = (ref.entity, ref.record_id)
            if key in seen:
                continue
            if entitlement.holds(ref.required, now):
                seen[key] = ref
    # Reversed again so the oldest surviving reference comes first, which is the order the
    # conversation happened in and therefore the order it reads in.
    return tuple(reversed(list(seen.values())))


def dropped_from_context(
    history: Sequence[Turn],
    entitlement: EntitlementSet,
    *,
    now: datetime,
    depth: int = CONTEXT_DEPTH,
) -> int:
    """How many references fell away because the asker may no longer see them.

    For the operator's log and for nobody else. Telling the asker "two things from earlier
    are no longer available to you" is a statement about what they used to be able to see,
    which is a permission fact they are not owed and which changes over time in a way they
    could probe. The answer simply stops mentioning those records.
    """
    total = {(r.entity, r.record_id) for turn in list(history)[-depth:] for r in turn.refs}
    kept = {
        (r.entity, r.record_id) for r in context_for(history, entitlement, now=now, depth=depth)
    }
    return len(total - kept)


class CorrectionKind(enum.StrEnum):
    """What kind of wrong an answer was. Closed, and deliberately coarse.

    Finer categories would be guesses about intent made from one sentence, and the value of
    this signal is in counting it rather than in reading any single one.
    """

    WRONG_FACT = "wrong_fact"
    MISSING = "missing"
    MISREAD_QUESTION = "misread_question"
    STALE = "stale"


@dataclass(frozen=True)
class Correction:
    """A person saying an answer was wrong, recorded as a signal and not as a fact (M9.2.4).

    **There is no field for the corrected content, and there is no version of this that has
    one.** Storing what the person said the right answer is turns the chat box into a write
    path into the knowledge base: no review, no scope, no provenance, and available to
    anybody who can talk to the assistant. A person correcting an invoice total in chat is
    asserting something about a record they may not be entitled to write, through a channel
    that checks nothing.

    What is kept is the shape: which answer, what kind of wrong, and the references that
    answer used - so somebody can go and look at the same records the answer drew on. That
    is the useful half, and it is the half that carries no claim.
    """

    answer_at: datetime
    at: datetime
    principal_id: str
    kind: CorrectionKind
    #: What the answer had drawn on, so a reviewer can look at the same records. Identifiers
    #: only, like everywhere else here.
    refs: tuple[RecordRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.at < self.answer_at:
            msg = "a correction cannot predate the answer it corrects"
            raise ValueError(msg)


def record_correction(
    history: Sequence[Turn], kind: CorrectionKind, *, principal_id: str, at: datetime
) -> Correction:
    """Attach a correction to the most recent answer in the history.

    Raises when there is nothing to correct. A correction with no answer behind it is a
    complaint, and counting it alongside real corrections would make the signal say the
    system is wrong more often than it is.
    """
    last_answer = next((t for t in reversed(list(history)) if t.is_answer), None)
    if last_answer is None:
        msg = "there is no answer in this conversation to correct"
        raise ValueError(msg)
    return Correction(
        answer_at=last_answer.at,
        at=at,
        principal_id=principal_id,
        kind=kind,
        refs=last_answer.refs,
    )
