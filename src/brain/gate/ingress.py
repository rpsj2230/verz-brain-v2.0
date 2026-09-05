"""Turning whatever a channel sent into one internal shape, and deciding who sent it.

Every channel has its own event format, its own idea of an identifier, and its own
redelivery behaviour. If those differences reach the gate, every downstream step has to
know about every channel, and the step that forgets one is the bug.

Two things here are security decisions rather than plumbing.

**Binding a channel identity to a person.** The rule is that a nonce minted inside an
authenticated session is presented on the new channel, never a code sent to a number that
asked for one. The difference is who proves what. Sending a code to a claimed number proves
that whoever holds that number can read it, which is exactly what a SIM swap gives an
attacker; requiring a nonce from an authenticated session proves the person was already
signed in, and the new channel only has to demonstrate it can receive what they were shown.

**An unrecognised identity gets nothing, and is told how to fix it.** Not a guess, not a
partial answer, and not an error that reveals whether the number is known to us. The reply
is the same whether the number has never been seen or belongs to someone who has not
completed a binding, because the two must not be distinguishable from outside.

Task ids: M3.2.1, M3.2.2, M3.2.3, M3.2.4
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.gate.admission import Assurance
from brain.gate.context import Channel

#: How long a binding nonce is good for. Short, because it exists only to be carried from
#: one screen to another device that is already in the person's hand.
NONCE_TTL = timedelta(minutes=10)

#: Bytes of entropy in a binding nonce. 128 bits, so guessing is not a strategy even with
#: no rate limiting in front of it.
NONCE_BYTES = 16


@dataclass(frozen=True)
class ChannelEvent:
    """One internal shape, whatever the channel sent.

    `external_id` is the channel's own identifier for this message, kept because it is the
    only thing that makes redelivery detectable. `channel_identity` is the channel's
    identifier for the *sender*, which is a phone number, a Lark open id or an email
    address, and is emphatically not a principal.
    """

    channel: Channel
    external_id: str
    channel_identity: str
    text: str
    received_at: datetime

    def __post_init__(self) -> None:
        if not self.external_id:
            # Without one there is no dedupe key, and a channel that redelivers would
            # answer the same question twice. For a read that is waste; for a side effect
            # it is the thing being done twice.
            raise ValueError(f"{self.channel} event has no external id, so it cannot be deduped")
        if not self.channel_identity:
            raise ValueError(f"{self.channel} event has no sender identity")

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """M3.2.2. Unique per channel, because two channels may well use the same counter.

        Returned as a tuple rather than a joined string so the database index is on two
        columns and neither can be forged into the other by an identifier containing the
        separator.
        """
        return (self.channel.value, self.external_id)


@dataclass(frozen=True)
class Binding:
    """A channel identity that has been proven to belong to a principal.

    `identity_hash` rather than the identity itself. A phone number is on the projection
    denylist, and a binding table full of them is a phone book of everyone at the company
    joined to their role. The hash is enough to look up an inbound message.
    """

    channel: Channel
    identity_hash: str
    principal_id: str
    bound_at: datetime
    #: What the binding itself is worth as evidence about a later request. Never more than
    #: BOUND: proving the binding once does not authenticate every message after it.
    assurance: Assurance = Assurance.BOUND

    def __post_init__(self) -> None:
        if self.assurance > Assurance.BOUND:
            raise ValueError(
                "a binding is evidence about the day it was made, not about this request; "
                "assurance above BOUND has to come from a live session"
            )


def identity_hash(channel: Channel, identity: str) -> str:
    """Stable digest of a channel identity, for storage and lookup.

    Salted by the channel so the same phone number on two channels does not produce one
    digest, which would let a binding on a weak channel be used to find one on a strong
    channel. Length-prefixed for the usual reason.
    """
    parts = (channel.value, identity.strip().casefold())
    blob = "".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BindingNonce:
    """A one-time value minted inside an authenticated session, to be presented elsewhere.

    Note the direction. This is created where the person already is, and carried to the
    channel they want to add. Nothing is ever sent to an address or number that asked to be
    bound, because doing so proves only that whoever holds it can read it.
    """

    value: str
    principal_id: str
    minted_at: datetime
    #: The channel this nonce may bind. A nonce minted for WhatsApp must not bind an email
    #: address, or the weakest channel becomes the way in to every other one.
    channel: Channel

    def is_valid(self, now: datetime) -> bool:
        return now - self.minted_at <= NONCE_TTL


def mint_nonce(principal_id: str, channel: Channel, now: datetime) -> BindingNonce:
    """Mint inside an authenticated session. The caller is responsible for that being true."""
    return BindingNonce(
        value=secrets.token_urlsafe(NONCE_BYTES),
        principal_id=principal_id,
        minted_at=now,
        channel=channel,
    )


class BindingRefusedError(Exception):
    """Raised when a binding attempt does not meet the rule.

    A programming and operations error, not a user-facing one. What the sender sees is the
    same unhelpful prompt either way, by design.
    """


def bind(
    nonce: BindingNonce,
    presented: str,
    event: ChannelEvent,
    now: datetime,
) -> Binding:
    """Complete a binding, or refuse.

    Every check is a separate refusal with its own reason, for the operator reading logs.
    None of those reasons reaches the sender.
    """
    if not secrets.compare_digest(nonce.value, presented):
        # Constant time, because a nonce compared with == leaks its prefix to anyone
        # willing to measure, and 128 bits guessed one byte at a time is not 128 bits.
        raise BindingRefusedError("nonce does not match")
    if not nonce.is_valid(now):
        raise BindingRefusedError("nonce has expired")
    if nonce.channel is not event.channel:
        raise BindingRefusedError(
            f"nonce was minted for {nonce.channel} and presented on {event.channel}; "
            "otherwise the weakest channel becomes the way in to every other one"
        )
    return Binding(
        channel=event.channel,
        identity_hash=identity_hash(event.channel, event.channel_identity),
        principal_id=nonce.principal_id,
        bound_at=now,
    )


def resolve(event: ChannelEvent, bindings: dict[str, Binding]) -> Binding | None:
    """M3.2.3. The binding for this sender, or None. Keyed on the hash, never the identity."""
    return bindings.get(identity_hash(event.channel, event.channel_identity))


#: What an unrecognised sender is told. The same words whether the identity is unknown, or
#: known and unbound, or belongs to someone whose binding was revoked this morning.
#:
#: Naming the person would confirm the number belongs to them, which is the question an
#: attacker with a stolen phone is actually asking. Saying "unknown number" would confirm
#: the opposite for every number that is known. So it says neither.
UNRECOGNISED_PROMPT = (
    "I cannot answer from this channel yet. Sign in to the console and add this channel "
    "from your profile to get started."
)


@dataclass(frozen=True)
class Unrecognised:
    """The outcome for a sender with no binding: no entitlement, and one instruction.

    Carries no `EntitlementSet` at all rather than an empty one. An empty set would be a
    thing that could be intersected, cached and passed along, and the point is that there
    is no principal here to have reach.
    """

    channel: Channel
    prompt: str = UNRECOGNISED_PROMPT
