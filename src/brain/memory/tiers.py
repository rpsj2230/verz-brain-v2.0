"""How much oversight a thing the system learnt needs before it takes effect.

`brain.memory.signals` notices that an answer went badly. This decides what the system is
allowed to do about it, and the whole design is one idea: **the tier follows what a change
would reach, never how confident anything is about it.** A learning the system is certain of
that would widen somebody's access needs a person; a learning it is unsure of that reorders
two search results does not. Confidence decides whether to act. Blast radius decides who has
to agree.

That ordering is the thing to protect. The natural implementation is the other way round,
because confidence is a number already sitting there and blast radius has to be worked out,
and the failure it produces is a system that widens access quietly whenever it is sure enough.
See `CONFIDENCE_DECIDES_WHETHER_AND_BLAST_RADIUS_DECIDES_WHO`.

**Four tiers, and the gap between two and three is where the whole thing is decided.**

- Zero is the session. It never leaves the conversation, so nothing outside can be wrong
  because of it, and nobody is told: telling somebody about a change confined to the
  conversation they are having is noise that trains them to ignore the notices that matter.
- One is automatic and is everything that changes what the system *says* without changing
  what anybody may *see*: a preference, a retrieval boost, a demotion, a corroborated link,
  and every negative signal. Wrong, these produce a worse answer, and a worse answer is
  visible to the person who got it.
- Two is proposed and then promoted: fast-path rules and procedural shortcuts. Wrong, these
  produce a confident answer from the wrong place, which is not visible to the person who
  got it, so a person has to have agreed first and agreement is counted over independent
  occurrences rather than repetitions of one.
- Three is gated and is anything that changes who may see what. Nothing here can approve
  one, and that is the load-bearing sentence in the module: `propose` returns a proposal,
  there is no `apply`, and `Gated` carries no field an approval could be written into.

**A change nobody classified is tier three.** `blast_radius` refuses a change it has no rule
for rather than defaulting to something safe-sounding, because "safe-sounding" for an unknown
change is exactly the judgement nobody is in a position to make. Falling to tier three means
an unclassified learning waits for a person, which is inconvenient and is the direction that
does not lose anybody's data.

**Nothing here applies anything.** No memory is written, no rule installed, no grant changed,
no leash moved. This module takes a proposed change and returns what tier it is and what that
implies; the machinery that would act on the answer does not exist. Said plainly because a
module about automatic learning that quietly did some would be the worst kind of surprise.

Task ids: M16.3.1, M16.3.2, M16.3.3, M16.3.4, M16.3.5, M16.3.6
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.memory.signals import Signal

#: Why the tier is decided by reach and never by confidence.
CONFIDENCE_DECIDES_WHETHER_AND_BLAST_RADIUS_DECIDES_WHO = (
    "A learning the system is certain of that would widen somebody's access needs a person. "
    "A learning it is unsure of that reorders two search results does not. So confidence "
    "decides whether to act at all and blast radius decides who has to agree, and the two "
    "are never mixed into one score. The natural implementation is the other way round, "
    "because confidence is a number already sitting there and blast radius has to be worked "
    "out, and what that produces is a system that widens access quietly whenever it happens "
    "to be sure enough. No threshold anywhere in this module reads a confidence."
)

#: Why an unclassified change is gated rather than given a safe default.
A_CHANGE_NOBODY_CLASSIFIED_IS_THE_ONE_NOBODY_THOUGHT_ABOUT = (
    "`blast_radius` refuses a change kind it has no rule for. A default would have to be "
    "chosen by somebody who has not seen the change, and the only defensible choice is the "
    "most cautious one, which is what refusing amounts to: an unclassified learning waits "
    "for a person. That is inconvenient in exactly the case where inconvenience is cheap and "
    "the alternative loses somebody's data."
)

#: Why nothing here can approve a gated change.
THIS_MODULE_HAS_NOWHERE_TO_APPROVE = (
    "`propose` returns a proposal and there is no `apply`. A `Gated` proposal carries no "
    "approver, no decision and no timestamp, so approving one is not a field away: it is a "
    "surface that does not exist yet. That is the same construction "
    "`brain.ops.automation_piece` uses about addresses and `brain.agents.catalogue` uses "
    "about side effects, and it is the only form of this rule that survives the first "
    "afternoon somebody is in a hurry."
)

#: How many independent occurrences a tier-two proposal needs before it may be promoted.
#:
#: Three, and what makes it three is what it is counting rather than the number itself. One
#: occurrence is an anecdote. Two is a coincidence often enough to matter. Three separate
#: conversations, on separate days, agreeing about the same shortcut is the point at which the
#: pattern is more likely than the alternative, which is that one person phrases things
#: unusually.
PROMOTION_AGREEMENT = 3

#: The window occurrences must fall inside to count towards one promotion.
#:
#: Thirty days. Long enough that a shortcut used weekly reaches the threshold, short enough
#: that three agreements spread across a year are not treated as a pattern: an agency's
#: processes change faster than that, and a shortcut promoted on evidence from three seasons
#: is a shortcut about a way of working nobody uses any more.
PROMOTION_WINDOW = timedelta(days=30)


class Tier(enum.IntEnum):
    """How much has to happen before a learning takes effect.

    An `IntEnum` and ordered, because the useful operation is "at least tier two" and a
    comparison is the honest way to write it. Ordered upwards by how much oversight is
    required, so a higher tier is always more careful, and `max` over several changes is the
    tier of the whole set. That last property is why the ordering matters: a batch containing
    one gated change is a gated batch.
    """

    #: The session. Never leaves the conversation, and nobody is told.
    SESSION = 0
    #: Automatic. Changes what the system says, never what anybody may see.
    AUTOMATIC = 1
    #: Proposed, then promoted once enough independent occurrences agree.
    PROMOTED = 2
    #: Gated. Changes who may see what, and a person decides.
    GATED = 3


class Change(enum.StrEnum):
    """What a learning would change. A closed vocabulary, and the reason is the tier map.

    Every member has exactly one entry in `BLAST_RADIUS`, and `tier_gaps` refuses the set if
    that stops being true in either direction. A member with no entry is a change whose tier
    is decided by whatever the lookup does when it misses, and a member of the map with no
    change is a rule about something that cannot happen.
    """

    #: Tier zero. Something true for this conversation and no other.
    SESSION_CONTEXT = "session_context"
    #: Tier one. How somebody likes an answer shaped.
    PREFERENCE = "preference"
    #: Tier one. This source answered this kind of question well, so rank it higher.
    RETRIEVAL_BOOST = "retrieval_boost"
    #: Tier one. And the reverse, which is the one that matters more.
    RETRIEVAL_DEMOTION = "retrieval_demotion"
    #: Tier one. Two records are the same thing, corroborated by more than one source.
    ENTITY_LINK = "entity_link"
    #: Tier one. Every signal that says an answer went badly.
    NEGATIVE_SIGNAL = "negative_signal"
    #: Tier two. A question shape answerable without a model.
    FAST_PATH_RULE = "fast_path_rule"
    #: Tier two. A sequence of steps somebody repeats.
    PROCEDURAL_SHORTCUT = "procedural_shortcut"
    #: Tier three. Somebody may see more than they could.
    SCOPE_WIDENING = "scope_widening"
    #: Tier three. Something becomes a fact about the company rather than a conversation.
    COMPANY_KNOWLEDGE = "company_knowledge"
    #: Tier three. An agent is trusted further than it was.
    LEASH_INCREASE = "leash_increase"
    #: Tier three. Two records either side of a money boundary become one.
    MONEY_BOUNDARY_MERGE = "money_boundary_merge"
    #: Tier three. Somebody holds a capability they did not.
    CAPABILITY_ADDITION = "capability_addition"


#: What tier each kind of change needs. Exhaustive over `Change` by test.
#:
#: Written as data rather than as a function with branches, so the whole policy is one thing
#: a reviewer reads in one place. A branch chain hides the shape: it is easy to see that a
#: condition exists and hard to see that every member is covered exactly once.
BLAST_RADIUS: dict[Change, Tier] = {
    Change.SESSION_CONTEXT: Tier.SESSION,
    Change.PREFERENCE: Tier.AUTOMATIC,
    Change.RETRIEVAL_BOOST: Tier.AUTOMATIC,
    Change.RETRIEVAL_DEMOTION: Tier.AUTOMATIC,
    Change.ENTITY_LINK: Tier.AUTOMATIC,
    Change.NEGATIVE_SIGNAL: Tier.AUTOMATIC,
    Change.FAST_PATH_RULE: Tier.PROMOTED,
    Change.PROCEDURAL_SHORTCUT: Tier.PROMOTED,
    Change.SCOPE_WIDENING: Tier.GATED,
    Change.COMPANY_KNOWLEDGE: Tier.GATED,
    Change.LEASH_INCREASE: Tier.GATED,
    Change.MONEY_BOUNDARY_MERGE: Tier.GATED,
    Change.CAPABILITY_ADDITION: Tier.GATED,
}

#: The changes that alter who may see what. Every one is gated and none may ever not be.
#:
#: A separate set from the map above, and deliberately not derived from it. Derived, the two
#: would agree by construction and the test comparing them would be a constant compared
#: against itself: lowering one of these in `BLAST_RADIUS` would move both sides together.
#: Written out, the map is checked against a list somebody has to edit on purpose.
CHANGES_WHAT_ANYBODY_MAY_SEE: frozenset[Change] = frozenset(
    {
        Change.SCOPE_WIDENING,
        Change.COMPANY_KNOWLEDGE,
        Change.LEASH_INCREASE,
        Change.MONEY_BOUNDARY_MERGE,
        Change.CAPABILITY_ADDITION,
    }
)

#: Which signals are evidence for a tier-one learning of their own.
#:
#: All of them, and that is M16.3.2's "all negative signals" read literally. A signal is
#: already a statement that something went badly and recording that is not a decision about
#: anybody's access, so waiting for a person to agree buys nothing and loses the evidence.
LEARNABLE_SIGNALS: frozenset[Signal] = frozenset(Signal)


class TierError(Exception):
    """A change nobody classified, or a proposal that cannot be what it says it is."""


@dataclass(frozen=True)
class Occurrence:
    """One time something happened, for counting agreement.

    Carries a conversation and a day and nothing else. Not the text, for the reason
    `brain.memory.signals` gives at length, and not the principal: agreement is about a
    pattern recurring, and counting whose conversations produced it turns a promotion record
    into a note about who the system learns from.
    """

    conversation_id: str
    on: datetime

    def __post_init__(self) -> None:
        if not self.conversation_id:
            msg = "an occurrence with no conversation cannot be told apart from another"
            raise TierError(msg)
        if self.on.tzinfo is None:
            msg = "a naive occurrence time compares wrongly against an aware one"
            raise TierError(msg)


@dataclass(frozen=True)
class Proposal:
    """A thing the system would like to change, and the tier that decides who agrees.

    No approver, no decision, no approved_at. See `THIS_MODULE_HAS_NOWHERE_TO_APPROVE`: a
    gated change is not approved by adding a field here, it is approved by a surface that
    does not exist yet.
    """

    change: Change
    tier: Tier
    #: What it is about, as an opaque reference the caller understands. Never a value.
    subject: str

    def __post_init__(self) -> None:
        if not self.subject:
            msg = "a proposal about nothing cannot be reviewed by anybody"
            raise TierError(msg)
        if BLAST_RADIUS.get(self.change) is not self.tier:
            msg = (
                f"a {self.change.value} proposal claims tier {int(self.tier)} and its blast "
                f"radius is {int(BLAST_RADIUS[self.change])}; the tier is not the proposer's "
                "to choose"
            )
            raise TierError(msg)


def blast_radius(change: Change) -> Tier:
    """What tier this kind of change needs, from what it would reach.

    Refuses a change with no entry rather than defaulting. See
    `A_CHANGE_NOBODY_CLASSIFIED_IS_THE_ONE_NOBODY_THOUGHT_ABOUT`.

    Reads no confidence, takes no confidence, and there is no parameter one could be passed
    in. That absence is the enforceable half of the module's central rule.
    """
    tier = BLAST_RADIUS.get(change)
    if tier is None:
        msg = (
            f"no blast radius is declared for {change.value!r}, so nothing can say who has to "
            "agree to it; an unclassified change waits for a person"
        )
        raise TierError(msg)
    return tier


def propose(change: Change, *, subject: str) -> Proposal:
    """The proposal for one change, at the tier its reach requires.

    The only way to build a `Proposal` with a tier that is not the caller's opinion: the tier
    comes from `blast_radius` and `__post_init__` refuses one that disagrees, so a caller
    constructing the dataclass directly cannot lower it either.
    """
    return Proposal(change=change, tier=blast_radius(change), subject=subject)


def tier_of(changes: Iterable[Change]) -> Tier:
    """The tier a set of changes needs, which is the highest any of them needs.

    A batch containing one gated change is a gated batch. The alternative, applying the
    automatic ones now and holding the gated one back, is how a change that only makes sense
    as a whole gets applied in half.

    An empty set is tier zero: nothing is being changed, so nobody needs to agree.
    """
    return max((blast_radius(one) for one in changes), default=Tier.SESSION)


def independent(
    occurrences: Sequence[Occurrence], *, now: datetime, window: timedelta = PROMOTION_WINDOW
) -> int:
    """How many independent occurrences fall inside the window.

    **Independent means separate conversations, and that is the whole of the arithmetic worth
    arguing about.** Somebody asking the same thing four times in one conversation is one
    piece of evidence about a shortcut and four pieces of evidence that an answer was unclear,
    and counting it as four is how a shortcut gets promoted on the strength of one bad
    afternoon.

    Occurrences from the future are ignored rather than counted. Clock skew that could
    manufacture agreement is skew that promotes a rule.
    """
    seen: set[str] = set()
    for one in occurrences:
        age = now - one.on
        if timedelta(0) <= age <= window:
            seen.add(one.conversation_id)
    return len(seen)


def may_promote(
    proposal: Proposal,
    occurrences: Sequence[Occurrence],
    *,
    now: datetime,
    agreement: int = PROMOTION_AGREEMENT,
    window: timedelta = PROMOTION_WINDOW,
) -> bool:
    """Whether a tier-two proposal has enough independent agreement to take effect.

    False for anything that is not tier two, and the two directions are different refusals
    rather than one. A tier-three change can never be promoted by agreement, because
    agreement is not what it is waiting for: three people repeating a pattern is not a person
    deciding somebody may see more. A tier-one change does not need promoting and asking
    whether it may be is a question about the wrong thing, so the honest answer is no rather
    than a vacuous yes.
    """
    if proposal.tier is not Tier.PROMOTED:
        return False
    return independent(occurrences, now=now, window=window) >= agreement


def tier_gaps(changes: Iterable[Change] = tuple(Change)) -> tuple[str, ...]:
    """Every change with no tier, every tier rule for a change that does not exist, and every
    access-changing kind that is not gated.

    The third is the one that matters. The first two keep the map and the vocabulary in step;
    the third checks the map against a separate list, written out rather than derived, so that
    lowering a gated change fails instead of moving both sides of a comparison together.
    """
    declared = set(changes)
    gaps: list[str] = []

    for missing in sorted(one.value for one in declared - set(BLAST_RADIUS)):
        gaps.append(f"{missing} has no blast radius, so nothing can say who agrees to it")
    for extra in sorted(one.value for one in set(BLAST_RADIUS) - declared):
        gaps.append(f"{extra} has a blast radius and is not a change anything can propose")
    for one in sorted(CHANGES_WHAT_ANYBODY_MAY_SEE, key=lambda c: c.value):
        if BLAST_RADIUS.get(one) is not Tier.GATED:
            gaps.append(
                f"{one.value} changes who may see what and is not gated, so the system could "
                "widen somebody's access without anybody agreeing"
            )
    return tuple(gaps)
