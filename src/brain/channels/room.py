"""Answering into a room, where the audience is not the asker.

Every other path in this system answers one person. A room does not: the question comes from
one person and the answer is visible to everyone in it, most of whom the gate was never
asked about. That inversion is what makes this the hardest permission problem here, and it
has exactly one safe answer.

**The envelope is computed at the floor of everyone present (M10.4.1).** Not the asker's
reach, not the most senior person's, not the union: the intersection. If one person in the
room cannot see contract values, the room's envelope has no contract values in it, whoever
asked. Anything else means the asker's permissions decide what a colleague reads, which is
a permission model where the wrong answer is invisible - the message looks like every other
message.

**The per-viewer body is a strictly narrower thing, not a wider one (M10.4.2).** Where a
channel can send a message only one person sees, the asker may get more than the room does
- but still only what *they* hold, never what somebody else in the room holds. Ephemeral is
a way to avoid over-sharing, never a way to route around the floor.

**Degradation ends at a link where the gate runs again (M10.4.3).** A channel that cannot do
ephemeral messages cannot deliver a per-viewer body at all, and the honest response is a
link to somewhere it can be answered properly rather than a quieter version in the room. The
link carries no answer, so following it re-runs the whole gate for whoever actually clicks -
which is the property that makes a forwarded link harmless.

**Membership is read at send time, not at render time (M10.4.4, M10.4.5).** Somebody joins
a room between the question and the answer and the envelope they were not counted in is
already computed. So a render is bound to the membership it was computed for, and a change
invalidates it rather than being merged into it: merging means deciding which of two
truths to keep, and every way of deciding that is a guess.

Task ids: M10.4.1, M10.4.2, M10.4.3, M10.4.4, M10.4.5
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from brain.channels.adapter import ChannelCapabilities, Feature
from brain.core.entitlement import EntitlementSet


class RoomRefusedError(Exception):
    """Raised when nothing may be sent into this room, for any of the reasons below."""


class Degradation(enum.StrEnum):
    """How far an answer had to fall back. Ordered from best to worst, and closed.

    Recorded so a trace can say *why* somebody got a link instead of an answer. Without it
    the two indistinguishable outcomes are "the room's floor removed everything" and "this
    channel cannot do ephemeral messages", which need different fixes from different people.
    """

    #: Everyone present may see the whole answer. Nothing was withheld for the room.
    FULL = "full"
    #: The room sees the floor; the asker separately sees their own, larger view.
    EPHEMERAL_ASIDE = "ephemeral_aside"
    #: The room sees the floor and there is no way to give the asker more here.
    FLOOR_ONLY = "floor_only"
    #: Nothing may be said in the room. A link is offered instead, and the gate re-runs for
    #: whoever follows it.
    LINK_ONLY = "link_only"


@dataclass(frozen=True)
class Member:
    """One person present. Identity and reach, and nothing else.

    There is no `is_admin`, no `role` and no `weight`. Every one of those would be a way for
    one person's presence to count for more than another's, and the floor is a floor
    precisely because it does not.
    """

    principal_id: str
    entitlement: EntitlementSet


@dataclass(frozen=True)
class RoomRender:
    """One answer, bound to the membership it was computed for.

    `membership` is the set of principal ids present when the envelope was computed. It is
    kept so a change can invalidate this render rather than being merged into it - see
    `still_valid_for`.
    """

    envelope: EntitlementSet
    degradation: Degradation
    membership: frozenset[str]
    #: The asker's own reach, when the channel can deliver a private aside. Never wider than
    #: the asker holds, and never used for what goes into the room itself.
    aside_for: str = ""

    def still_valid_for(self, present: frozenset[str]) -> bool:
        """Whether this render may still be sent (M10.4.4).

        Anybody arriving invalidates it, because the envelope was computed at a floor they
        were not part of and their reach may be lower. Anybody *leaving* also invalidates
        it, and that is the half worth stating: the envelope is then narrower than it needs
        to be, which is not a leak - but re-computing costs nothing and a render that
        silently outlives its membership is one nobody can reason about later.
        """
        return present == self.membership


def floor(members: Sequence[Member]) -> EntitlementSet:
    """The intersection of everyone's reach (M10.4.1).

    Intersection, not union and not the asker's. If one person present cannot see contract
    values then the room's answer has none in it, whoever asked - because the alternative is
    that the asker's permissions decide what a colleague reads, and the wrong answer there
    is invisible: it looks like every other message.

    Refuses an empty room rather than returning an empty entitlement. Those two are
    different: an empty set means "present, and entitled to nothing", which is a real and
    answerable state, while an empty room means nobody asked. Returning the same value for
    both would let a bug that lost the membership list read as a room where everybody
    happens to hold nothing.
    """
    if not members:
        msg = "a room with no members has no floor; nobody is there to answer"
        raise RoomRefusedError(msg)

    envelope = members[0].entitlement
    for member in members[1:]:
        # `intersect` is the same function the agent ceiling uses, and using it here rather
        # than a bespoke comparison is the point: one definition of "narrower", so a room
        # and an agent cannot disagree about what an intersection means.
        envelope = envelope.intersect(member.entitlement)
    return envelope


def plan(
    members: Sequence[Member],
    asker_id: str,
    capabilities: ChannelCapabilities,
    *,
    now: datetime,
) -> RoomRender:
    """What may be said in this room, and how far it had to fall back.

    The order is deliberate. The floor first, because it constrains everything; then whether
    the floor leaves anything to say; then whether the asker can be told more privately.
    Computing the aside first would mean deciding what the asker gets before knowing what
    the room gets, and the aside is defined relative to the room.
    """
    present = frozenset(m.principal_id for m in members)
    if asker_id not in present:
        # A question from somebody not in the room is not a room question. Answering it into
        # the room would put an answer in front of an audience the asker is not part of and
        # cannot see the effect of.
        msg = f"{asker_id} is not in this room, so there is no room answer to give them"
        raise RoomRefusedError(msg)

    envelope = floor(members)
    asker = next(m for m in members if m.principal_id == asker_id)

    # An expired member expires the room. `is_expired` is on the set because a grant can
    # carry a time bound; a contractor whose access ended is present in the room and holds
    # nothing, so the floor is empty - which is the correct answer rather than an error.
    room_has_nothing = not envelope.grants or envelope.is_expired(now)

    asker_has_more = bool(asker.entitlement.grants) and len(asker.entitlement.grants) > len(
        envelope.grants
    )

    if room_has_nothing:
        if not capabilities.supports(Feature.EPHEMERAL) or not asker_has_more:
            # Nothing may be said here and there is no private way to say it. A link, and
            # the gate runs again for whoever follows it - which is what makes forwarding
            # the link harmless.
            return RoomRender(
                envelope=envelope, degradation=Degradation.LINK_ONLY, membership=present
            )
        return RoomRender(
            envelope=envelope,
            degradation=Degradation.EPHEMERAL_ASIDE,
            membership=present,
            aside_for=asker_id,
        )

    if asker_has_more and capabilities.supports(Feature.EPHEMERAL):
        return RoomRender(
            envelope=envelope,
            degradation=Degradation.EPHEMERAL_ASIDE,
            membership=present,
            aside_for=asker_id,
        )

    if asker_has_more:
        # The channel cannot do a private aside, so the asker gets what the room gets. Not a
        # leak, and worth recording: somebody will ask why the same question answered
        # differently in two places.
        return RoomRender(envelope=envelope, degradation=Degradation.FLOOR_ONLY, membership=present)

    return RoomRender(envelope=envelope, degradation=Degradation.FULL, membership=present)


def revalidate(render: RoomRender, present: frozenset[str], members: Sequence[Member]) -> None:
    """Refuse a render whose room has changed since it was computed (M10.4.4).

    Raises rather than returning a new render. Recomputing here would hide the change from
    the caller, and the caller is the one holding a half-written message: it needs to know
    the answer it is about to send was computed for a different room, not to be quietly
    handed a different one.
    """
    if render.still_valid_for(present):
        return
    joined = sorted(present - render.membership)
    left = sorted(render.membership - present)
    # Names the counts and not the people. A message naming who joined would tell whoever
    # reads the log something about room membership they may not be entitled to, and the
    # caller already has the list.
    msg = (
        f"the room changed after this answer was computed: {len(joined)} joined, "
        f"{len(left)} left. The envelope was the floor of a different set of people."
    )
    del members  # Present for the caller's convenience; deliberately unused here.
    raise RoomRefusedError(msg)
