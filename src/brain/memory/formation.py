"""What was true when a memory was formed, and whether it may still be recalled.

A memory is something the system learnt while acting for somebody. The failure it invites is
specific and is not a bug anybody writes on purpose: a thing learnt while acting for a person
with broad access, recalled months later while acting for a person without it. Nothing about
the recalled sentence says where it came from, so the disclosure arrives looking like the
system being helpful.

**So a memory carries what it was formed from, and the reader is checked against that rather
than against the memory's text.** `Formation` records the capabilities, the scope and the
entitlement hash of whoever was asking when it was written. `may_recall` asks whether the
reader still reaches all of it, now, and returns the scope they reach it in rather than a
bool: a verdict with no name is a verdict nobody can show anybody.

**Three existing pieces and no fourth rule.** `EntitlementSet.intersect` decides what narrower
means, `scope_for` decides what holding a capability means and refuses an expired principal,
and `Scope.matches` decides whether a grant admits a place. `brain.ops.denial_alerts.reach`
composes exactly these three for exactly this shape of question, and this module composes them
the same way on purpose. A fourth implementation of the central rule is a fourth place for it
to be subtly wrong, and the permissive copy is the one that wins the day two disagree.

**The intersection runs requirement-first and that is load-bearing rather than stylistic.**
`intersect` keeps a grant of the receiver's only where the ceiling covers it, and
`Capability.covers` expands only a trailing `.*`. Narrowing the *reader* by the memory's
specific capability would drop the wildcard grant of somebody who plainly holds it, so a
person with `read:client.*` would lose a memory formed under `read:client.name`. Narrowing the
requirement by the reader asks the question that was meant: does what this person holds cover
what this memory was formed from.

**The entitlement hash is recorded and is deliberately not the check.** Comparing hashes would
be a stricter rule and a worse one: it invalidates a memory when the reader's grants change at
all, including when they widen, so somebody promoted on Monday loses everything the system
learnt with them. What the hash is for is the audit question, "what could this person see when
this was written", which a set of capabilities answers less exactly than the digest the cache
key is built from. Recorded because it is cheap and answers a question the tags cannot; not
compared, because comparing it would answer a different question from the one being asked. See
`A_HASH_ANSWERS_A_DIFFERENT_QUESTION_FROM_THE_ONE_RECALL_ASKS`.

**Memory is never authoritative over the database.** A recalled memory is a hint about a
conversation and never a fact about the company, and this module gives it nowhere to become
the second: a `Recollection` carries no value, no row, no record id and no citation, only the
formation it came from and the confidence it now has. Something that wanted to answer from
memory alone would have to add a field. See `MEMORY_IS_A_HINT_AND_THE_SOURCE_IS_THE_ANSWER`.

**Confidence decays and the floor is a retrieval threshold rather than a deletion.** A memory
below the floor stops being recalled and stays on the record, because the reason it stopped
mattering is worth being able to look up, and because a system that deleted what it stopped
trusting would have no way to show somebody why it changed its mind.

**Two of M16.4's leaves are here rather than in `correction.py`, and that is not an accident
of where they were written.** Time decay reducing confidence below the retrieval threshold is
`confidence_now` and `RECALL_FLOOR`, and entitlement expiry is `may_recall` going through
`scope_for`, which refuses a principal past their bound. Both are properties of recall itself:
they decide whether a memory is reached at all, on every read, with nothing recorded anywhere.
`correction.py` handles the other two, where something positively contradicts a memory and a
record is written about it. Putting decay beside supersession would suggest they work the same
way, and they do not: one is arithmetic on the clock and the other is a row.

Task ids: M16.1.1, M16.1.4, M16.1.5, M16.4.1, M16.4.4
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Clause, Op, Scope

#: Why the recorded entitlement hash is kept and never used as the recall check.
A_HASH_ANSWERS_A_DIFFERENT_QUESTION_FROM_THE_ONE_RECALL_ASKS = (
    "Comparing the reader's entitlement hash against the one recorded at formation is a "
    "stricter rule and a worse one. It fails whenever the reader's grants have changed at "
    "all, including when they have widened, so somebody promoted on Monday loses everything "
    "the system learnt with them and there is no signal anywhere saying why. What recall "
    "asks is whether this reader still reaches what the memory was formed from, which is a "
    "question about coverage rather than about equality. The hash is recorded because it "
    "answers the audit question exactly, being the same digest the cache key is built from, "
    "and a set of capability tags answers that one only approximately."
)

#: Why a recollection has nowhere to put an answer.
MEMORY_IS_A_HINT_AND_THE_SOURCE_IS_THE_ANSWER = (
    "A recollection carries the formation it came from and the confidence it now has, and "
    "no value, no row, no record id and no citation. That is the structural half of memory "
    "never being authoritative over the database: something that wanted to answer from "
    "memory alone would have to add a field to this model, in a module whose docstring "
    "argues against it, rather than reading one that is already there. What memory is for is "
    "deciding what to look up and how to phrase it, and the looking up is what carries the "
    "permissions of the thing it found."
)

#: Why a memory below the floor is left on the record rather than deleted.
A_MEMORY_THE_SYSTEM_STOPPED_TRUSTING_IS_STILL_A_RECORD = (
    "Confidence decays and the floor is a retrieval threshold, not a deletion. A memory "
    "under it stops being recalled and stays where it is, because a person asking why the "
    "system changed its mind deserves an answer, and because deletion is the one operation "
    "that makes the previous behaviour unexplainable. It also means an undo has something "
    "to restore."
)

#: The confidence below which a memory is not recalled.
#:
#: A floor rather than a target, and set from what the decay curve does rather than from
#: what sounds careful: with `HALF_LIFE_DAYS` at 30, a memory formed at 1.0 and never
#: corroborated crosses this in a little under two months, which is about the point at which
#: a preference somebody expressed once has stopped being evidence of anything.
RECALL_FLOOR = 0.35

#: How long a memory takes to lose half its confidence with nothing reinforcing it.
#:
#: Thirty days rather than a figure per kind. A shorter half-life makes the system forget a
#: real preference between projects; a longer one keeps a one-off correction alive past the
#: circumstances that produced it. This is the parameter most likely to be wrong and it is
#: one number in one place so that changing it is one edit with one argument.
HALF_LIFE_DAYS = 30.0

#: How long session memory lives once the thread stops being spoken to.
#:
#: The thread's lifetime, which is what M16.1.1 asks for, expressed as an idle bound because
#: a thread has no end anybody declares. A conversation nobody has added to in this long is
#: over, whatever the client did with its window.
SESSION_IDLE_SECONDS = 12 * 60 * 60


class MemoryKind(enum.StrEnum):
    """Where a memory lives, which decides what may be done to it.

    Three, matching `brain.db.SCHEMAS`'s description of the `mem` schema. The kind is
    recorded rather than inferred from where a row was found, because a session memory
    promoted to persistent is a real operation and the promotion has to be visible.
    """

    #: Valkey, keyed by thread, gone when the conversation is.
    SESSION = "session"
    #: A table. Something somebody stated, which stays until it is contradicted.
    PERSISTENT = "persistent"
    #: A table. Something the system inferred, which carries confidence and decays.
    ADAPTIVE = "adaptive"


@dataclass(frozen=True)
class Formation:
    """What was true about the writer at the moment a memory was written.

    Every field is about the writer and none is about the memory's content, which is the
    point: this is what recall is checked against, and a check that read the content would be
    a classifier deciding what may be recalled.
    """

    #: Who was asking. Recorded for the audit question, never used to permit a recall: a
    #: memory is not recalled because the same person is back, it is recalled because
    #: whoever is here now still reaches what it was formed from.
    principal_id: str
    #: The capabilities in play when it was formed. What recall is checked against.
    capabilities: tuple[Capability, ...]
    #: Where it was formed. A memory about one department is not a memory about another.
    scope: Scope
    #: `EntitlementSet.ent_hash` at formation. Recorded, never compared. See the constant.
    ent_hash: str
    formed_at: datetime
    kind: MemoryKind = MemoryKind.ADAPTIVE

    def __post_init__(self) -> None:
        if not self.principal_id:
            msg = "a formation with no principal cannot be audited, which is what it is for"
            raise ValueError(msg)
        if not self.capabilities:
            msg = (
                "a formation naming no capability would be recalled by everybody, because "
                "the reader trivially covers an empty requirement"
            )
            raise ValueError(msg)
        if self.formed_at.tzinfo is None:
            msg = "a naive formation time compares wrongly against an aware one"
            raise ValueError(msg)


@dataclass(frozen=True)
class Recollection:
    """A memory that may be recalled, and the terms on which it may.

    No value, no row, no citation. See `MEMORY_IS_A_HINT_AND_THE_SOURCE_IS_THE_ANSWER`.
    """

    formation: Formation
    #: Where the reader reaches it, which is at most where it was formed.
    scope: Scope
    #: What it is worth now, after decay.
    confidence: float


def requirement(formation: Formation) -> EntitlementSet:
    """What a reader must hold before this memory may be recalled to them.

    One grant per capability the memory was formed under, each in the memory's own scope.
    Expressed as an `EntitlementSet` rather than as a list of pairs so that `may_recall` can
    narrow it with the ordinary intersection instead of comparing scopes by hand, which is
    the move `brain.ops.denial_alerts.requirement` makes for the same reason.

    The scope is attached to every grant rather than carried alongside them. Two places to
    put a scope is two things that can disagree, and the disagreement is silent in whichever
    direction the caller happened to write.
    """
    return EntitlementSet(
        principal_id=MEMORY_REQUIREMENT,
        grants=tuple(
            Grant(capability=capability, scope=formation.scope)
            for capability in formation.capabilities
        ),
    )


#: The principal id a memory's requirement is expressed under. Not a real principal: an
#: `EntitlementSet` needs one, and using the writer's would make the requirement look like a
#: grant somebody holds.
MEMORY_REQUIREMENT = "requirement:memory_recall"


def confidence_now(
    formed_confidence: float,
    *,
    formed_at: datetime,
    now: datetime,
    half_life_days: float = HALF_LIFE_DAYS,
) -> float:
    """What a memory is worth after decay, given what it was worth when it was formed.

    Exponential rather than linear, because a linear decay reaches zero on a date and
    everything formed that day stops mattering at once, which is a cliff nobody chose. The
    exponential has no such date and the floor is what decides retrieval.

    A memory from the future is treated as formed now rather than gaining confidence. Clock
    skew between a writer and a reader is ordinary, and a memory that grew more certain
    because two machines disagreed would be the strangest possible bug to diagnose.
    """
    elapsed_days = max(0.0, (now - formed_at).total_seconds() / 86400.0)
    return formed_confidence * math.pow(0.5, elapsed_days / half_life_days)


def may_recall(
    formation: Formation,
    reader: EntitlementSet,
    *,
    now: datetime,
    where: Mapping[str, object] | None = None,
    formed_confidence: float = 1.0,
    floor: float = RECALL_FLOOR,
) -> Recollection | None:
    """Whether this reader may be told this memory, and on what terms.

    None means no, and no is what they are told: nothing anywhere reports that a memory was
    withheld, because "there is something I know and will not say" is a fact about what
    exists, and DENIED and ABSENT are indistinguishable here as everywhere.

    The order is deliberate. Reach is decided before confidence, so a memory the reader may
    not have is refused for that reason whatever its confidence, and a decayed memory is
    never the reason a permission question goes unanswered.

    `where` is the place the recall is happening, checked against the scope the reader
    reaches. Absent, it is the memory's own scope, which is the case where somebody is asking
    about exactly what the memory is about.
    """
    shared = requirement(formation).intersect(reader)

    reached: Scope | None = None
    for capability in formation.capabilities:
        scope = shared.scope_for(capability, now)
        if scope is None:
            return None
        reached = scope if reached is None else reached.intersect(scope)

    if reached is None:
        return None

    place = dict(where) if where is not None else _place_of(formation.scope)
    if place and not reached.matches(place):
        return None

    confidence = confidence_now(formed_confidence, formed_at=formation.formed_at, now=now)
    if confidence < floor:
        return None

    return Recollection(formation=formation, scope=reached, confidence=confidence)


def _place_of(scope: Scope) -> dict[str, object]:
    """The row a scope describes, for scopes that describe one.

    Only equality clauses produce a place. A scope saying "department in (a, b)" describes
    two places and not one, so it yields nothing here and the caller's `where` decides, which
    is the honest reading: this function exists to save a caller repeating themselves, not to
    invent a location.
    """
    return {
        clause.field: clause.value
        for clause in scope.clauses
        if clause.op is Op.EQ and isinstance(clause.value, str)
    }


def session_key(thread_id: str, principal_id: str) -> str:
    """Where one thread's session memory lives.

    The principal is in the key and not only in the value, so a thread id guessed or reused
    reaches nothing: two principals on one thread id get two keys rather than one shared one.
    A session memory is the least protected of the three kinds, living in a cache with no
    row-level security in front of it, so the key is where the separation has to be.
    """
    if not thread_id or not principal_id:
        msg = "a session key needs both a thread and a principal, or it separates nobody"
        raise ValueError(msg)
    return f"mem:session:{principal_id}:{thread_id}"


def session_expiry(
    last_spoken_at: datetime, *, idle_seconds: int = SESSION_IDLE_SECONDS
) -> datetime:
    """When a thread's session memory stops existing.

    Idle rather than absolute, because a thread has no end anybody declares and a
    conversation nobody has added to for this long is over whatever the client did with its
    window. Every write to the thread moves it, which is what makes it a lifetime rather than
    a timeout.
    """
    return last_spoken_at + timedelta(seconds=idle_seconds)


def recallable(
    formations: Sequence[tuple[Formation, float]],
    reader: EntitlementSet,
    *,
    now: datetime,
    where: Mapping[str, object] | None = None,
) -> tuple[Recollection, ...]:
    """Every memory in a set that this reader may have, most confident first.

    Returns what may be recalled and says nothing at all about the rest. Not a count, not a
    total, not "and three others": a number attached to a filtered list is the difference
    between what somebody may see and what exists, which is the subtraction this system
    refuses everywhere.
    """
    found = [
        recollection
        for formation, formed_confidence in formations
        if (
            recollection := may_recall(
                formation, reader, now=now, where=where, formed_confidence=formed_confidence
            )
        )
        is not None
    ]
    return tuple(sorted(found, key=lambda one: (-one.confidence, one.formation.formed_at)))


def clause_place(**fields: str) -> Scope:
    """A scope naming one place, for callers building a formation.

    Here so that a scope cannot be spelled two ways in two call sites, which is how two
    memories come to be about the same department and not match each other.
    """
    return Scope(
        clauses=tuple(
            Clause(field=name, op=Op.EQ, value=value) for name, value in sorted(fields.items())
        )
    )
