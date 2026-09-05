"""What the channel and the strength of the sign-in take away from what a person holds.

The core invariant of the platform is that a run holds the intersection of what the caller
holds with a ceiling. Agents are the familiar case. Two more ceilings apply before any
agent is chosen, and both exist because holding a permission is not the same as being able
to exercise it from anywhere, on the strength of any sign-in.

**The channel ceiling.** A WhatsApp message is a phone number that claimed to be someone.
The finance director really does hold `approve:payment`, and a message from a swapped SIM
must not be able to use it. The console, behind a live SSO session, is a different
proposition to the same permission.

**The assurance ceiling.** How strongly we know who this is, right now. It is separate from
the channel because the same channel carries both: a Lark account with a live session and
one where the binding was done six months ago are not equally convincing.

Both only ever subtract, and that is the property worth defending. Neither ceiling can hand
anyone a capability they did not already hold, so an operator who widens a channel ceiling
by mistake grants nothing; they only stop taking something away. Getting this backwards, so
that a channel could *add*, would make the channel a place to escalate from.

Task ids: M3.3.3, M3.3.4
"""

from __future__ import annotations

import enum
from typing import assert_never

from brain.core.entitlement import EntitlementSet
from brain.gate.context import Channel


class Assurance(enum.IntEnum):
    """How strongly we know who is asking, at this moment.

    Ordered, so "at least this assurance" is a comparison. Deliberately about *now* rather
    than about the account: a binding made six months ago is evidence about six months ago.
    """

    #: A channel identity nobody has bound to a principal. Not anonymous, worse: it looks
    #: like a person and is not one yet.
    UNVERIFIED = 0
    #: Bound to a principal, but on the strength of that binding alone. The binding itself
    #: is trustworthy (a nonce minted inside an authenticated session, never a code sent to
    #: a number that asked for one), but it is not evidence about this request.
    BOUND = 1
    #: A live authenticated session.
    AUTHENTICATED = 2
    #: A live session with a second factor presented in it.
    STRONG = 3


#: The verbs each channel may carry, whatever the caller holds.
#:
#: Read is everywhere: the permission model already decides what a person may read, and
#: withholding it by channel would only teach people to go and look somewhere with worse
#: logging. Effects are the thing that varies, because an effect from a channel we cannot
#: strongly authenticate is an effect attributable to a phone number.
CHANNEL_VERBS: dict[Channel, frozenset[str]] = {
    Channel.CONSOLE: frozenset({"read", "write", "invoke", "approve", "admin"}),
    Channel.LARK: frozenset({"read", "write", "invoke", "approve"}),
    # No admin, and no approve. A message is not a signature.
    Channel.WHATSAPP: frozenset({"read"}),
    # Email is spoofable and asynchronous; by the time it is read the sender may have
    # changed their mind, and there is nobody present to confirm.
    Channel.EMAIL: frozenset({"read"}),
    Channel.API: frozenset({"read", "write", "invoke"}),
    Channel.WEBHOOK: frozenset({"read", "invoke"}),
    Channel.SCHEDULER: frozenset({"read", "write", "invoke"}),
    # Read and nothing else, and narrower than WhatsApp for a reason WhatsApp does not have:
    # nobody has said who this is. The assurance ceiling already gives an unverified caller
    # nothing at all, so today this changes no outcome. It matters the day a widget visitor
    # identifies themselves, because they are then an ordinary authenticated principal whose
    # verbs would otherwise be decided by the assurance level alone. A person may sign in
    # through a widget; they may not approve a payment through one.
    Channel.WIDGET: frozenset({"read"}),
}

#: The verbs each assurance level may carry.
ASSURANCE_VERBS: dict[Assurance, frozenset[str]] = {
    # An unbound identity holds nothing at all. Not "read only": nothing. We do not know
    # who they are, so there is no reach to narrow.
    Assurance.UNVERIFIED: frozenset(),
    Assurance.BOUND: frozenset({"read"}),
    Assurance.AUTHENTICATED: frozenset({"read", "write", "invoke"}),
    Assurance.STRONG: frozenset({"read", "write", "invoke", "approve", "admin"}),
}


def verbs_for_channel(channel: Channel) -> frozenset[str]:
    """Every channel declares its verbs. `assert_never` makes a new one a type error.

    Same reasoning as the traffic class: a channel nobody has thought about must not
    inherit a permissive default just because a dictionary lookup needed one.
    """
    match channel:
        case (
            Channel.CONSOLE
            | Channel.LARK
            | Channel.WHATSAPP
            | Channel.EMAIL
            | Channel.API
            | Channel.WEBHOOK
            | Channel.SCHEDULER
            | Channel.WIDGET
        ):
            return CHANNEL_VERBS[channel]
        case _:
            assert_never(channel)


def _ceiling_for_verbs(
    held: EntitlementSet, verbs: frozenset[str], principal_id: str
) -> EntitlementSet:
    """A ceiling built from what the caller holds, restricted to these verbs.

    Built from the caller's own grants rather than from a wildcard, so it is structurally
    incapable of adding anything. A ceiling expressed as `read:*` would intersect to
    whatever the caller holds under read, which is the same answer today and a much easier
    thing to get wrong tomorrow.
    """
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(g for g in held.grants if g.capability.verb in verbs),
        not_after=held.not_after,
    )


def admit(
    held: EntitlementSet,
    channel: Channel,
    assurance: Assurance,
) -> EntitlementSet:
    """What this caller may actually exercise, here, now.

    `E_admitted = E(caller) ∩ channel_ceiling ∩ assurance_ceiling`, and every term can only
    narrow. The result is what the rest of the gate treats as the caller's reach, so
    everything downstream, including the agent intersection and the cache key, is computed
    against the narrowed set rather than the nominal one.
    """
    allowed = verbs_for_channel(channel) & ASSURANCE_VERBS[assurance]
    return _ceiling_for_verbs(held, allowed, held.principal_id)


def would_lose(held: EntitlementSet, channel: Channel, assurance: Assurance) -> tuple[str, ...]:
    """Capabilities the caller holds but cannot use here. For explaining, never for hinting.

    This is the raw material for "you can do this from the console", which is a genuinely
    useful thing to tell someone about their *own* permissions. It is emphatically not for
    telling anyone what someone else could do, and not for explaining an absent record: a
    refusal that explains itself confirms the record exists.
    """
    admitted = {g.capability.value for g in admit(held, channel, assurance).grants}
    return tuple(sorted({g.capability.value for g in held.grants} - admitted))
