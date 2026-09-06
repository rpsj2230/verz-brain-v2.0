"""Sending the evening digest, which is the one call site `brain.ops.digest` was shaped for.

`digest.py` says it in its own opening: there is no channel there and no send, everything is
a pure function, and delivery is "build the plan, call `daily_digest`, render, hand the
string and `DIGEST_CLASSIFICATION` to an adapter". This is that, and it is deliberately
small. The interesting decisions were all made next door; what is left is the handful that
only appear once something is actually posted to a room every evening.

**The digest is INTERNAL and it names every open task in the plan.** That is the shape of
what is not built yet, which is a fact about the company rather than about the work, and it
belongs in a staff channel and nowhere else. `assert_can_send` is what enforces the ceiling
and it is reached through the adapter rather than restated here, so this cannot disagree with
any other sender about what a channel may carry.

**A room, never a person, and the room is configured rather than discovered.** A digest
posted to whoever happened to ask for one would be a per-person message that says the same
thing to everybody, which is a broadcast with extra steps and a per-viewer render nobody
needs. `DigestChannel` names the chat once.

**It is sent once per day and the day is the key.** A scheduler that fires twice, or is
restarted, or has its timer edited, posts the digest again, and a channel with two identical
digests in it is one people stop reading. `already_sent_today` is asked before the render
rather than after, because rendering is where the work is and a second render is a second
chance for the two to differ.

**A quiet day is still sent.** `digest.A_QUIET_DAY_IS_A_RESULT_AND_NOT_AN_ABSENT_MESSAGE`
already argues this and it is worth restating at the boundary, because "nothing happened, do
not bother them" is exactly the optimisation somebody adds here. A digest that only arrives
on busy days cannot report a stall, and the stall is the thing worth reading.

**Failure to deliver is not failure to compute.** The digest is a record of what the plan did
and it exists whether or not Lark accepted it. So a delivery failure returns an outcome that
says so, rather than raising into whatever scheduled it: a job that dies on a transport error
loses the digest as well as the send, and the next evening's run has nothing to compare
against.

Rejected: rendering here. `digest.render` already owns the wording, and a second renderer for
"the Lark version" is how the message people read stops matching the one the tests check.

Task ids: M38.3.3.1, M38.3.3.4
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Final, Protocol

from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.ops.digest import DIGEST_CLASSIFICATION, DailyDigest, render

#: Why the digest goes to a room rather than to people.
A_DIGEST_IS_A_BROADCAST_AND_NOT_AN_ANSWER: Final = (
    "everybody who reads it reads the same sentence, because it is a report about the plan "
    "rather than an answer to anybody's question. Sending it per person would be a broadcast "
    "with extra steps and would put a per-viewer render on a path that has no viewer"
)

#: Why the same day is never sent twice.
A_CHANNEL_WITH_TWO_IDENTICAL_DIGESTS_STOPS_BEING_READ: Final = (
    "a scheduler fires twice, or is restarted, or has its timer edited by somebody testing "
    "it. Each of those posts the digest again, and a room with the same message in it twice "
    "is one people learn to skip, which costs the whole feature"
)

#: Why a quiet day is delivered like any other.
A_DIGEST_THAT_ONLY_ARRIVES_ON_BUSY_DAYS_CANNOT_REPORT_A_STALL: Final = (
    "not sending when nothing closed is the obvious kindness and it removes the only signal "
    "that matters: a week of silence is indistinguishable from a week nobody sent the "
    "digest, and the reader cannot tell which"
)


class DeliveryOutcome(enum.StrEnum):
    """What happened to one evening's digest. Closed, because each is acted on differently.

    Three rather than two. "The channel refused it" and "it was already sent" are both
    not-sent and they are not the same fact: one is an incident and the other is the
    duplicate guard working, and a scheduler that treated them alike would either alert on
    every restart or stay silent through a real outage.
    """

    SENT = "sent"
    #: Today's digest is already in the room. Normal, and not an error.
    ALREADY_SENT = "already_sent"
    #: The channel would not take it. The digest still exists; only the send failed.
    UNDELIVERED = "undelivered"


@dataclass(frozen=True)
class DigestChannel:
    """Where the digest goes, named once in configuration.

    `chat_id` rather than a person, a role or a query. A digest addressed by anything that
    has to be resolved at send time is one whose audience can change without anybody
    deciding, and the audience is the whole of the disclosure question here.
    """

    channel: Channel
    chat_id: str

    def __post_init__(self) -> None:
        if not self.chat_id.strip():
            msg = "a digest channel names the chat it posts to; an empty one posts nowhere"
            raise ValueError(msg)


class DigestSender(Protocol):
    """The narrow slice of a channel adapter this needs.

    A protocol rather than `LarkAdapter`, for the reason every other module here takes one:
    the case worth testing is a digest that should not have been sent, and that is not
    testable through something that opens a socket. It also means the digest can move to a
    second channel without this module learning about it.
    """

    def send(self, payload: ChannelPayload, *, to: str) -> None: ...


class SentRegister(Protocol):
    """Whether a digest for this day already reached this chat.

    Read-only here, and recorded by the caller after a successful send. A register this
    module wrote to would make the send and the record one operation that can half-happen,
    and the half that goes missing is the record, so the next run sends again.
    """

    def already_sent(self, *, day: date, chat_id: str) -> bool: ...


@dataclass(frozen=True)
class Delivery:
    """What was attempted and what came of it.

    Carries the rendered text even when it was not sent, so an operator investigating a
    failure reads what would have gone out rather than re-running the build to find out.
    """

    outcome: DeliveryOutcome
    day: date
    chat_id: str
    body: str = ""
    detail: str = ""


def deliver_digest(
    digest: DailyDigest,
    *,
    channel: DigestChannel,
    sender: DigestSender,
    register: SentRegister,
) -> Delivery:
    """Send one evening's digest, once (M38.3.3.1, M38.3.3.4).

    Returns rather than raises on a transport failure. The digest is a record of what the
    plan did and it exists whether or not the channel accepted it; a scheduled job that dies
    on a delivery error loses the computation as well as the send.

    The duplicate check happens before the render because rendering is where the work is,
    and because two renders of one day are two chances for the text to differ.
    """
    if register.already_sent(day=digest.day, chat_id=channel.chat_id):
        return Delivery(
            outcome=DeliveryOutcome.ALREADY_SENT,
            day=digest.day,
            chat_id=channel.chat_id,
            detail=A_CHANNEL_WITH_TWO_IDENTICAL_DIGESTS_STOPS_BEING_READ,
        )

    body = render(digest)
    payload = ChannelPayload(label="")

    try:
        sender.send(payload, to=channel.chat_id)
    except Exception as exc:
        # The class name and never the message. A transport exception stringifies whatever
        # it failed on, and what it failed on here is a message naming every open task.
        return Delivery(
            outcome=DeliveryOutcome.UNDELIVERED,
            day=digest.day,
            chat_id=channel.chat_id,
            body=body,
            detail=f"{channel.channel} refused the digest: {type(exc).__name__}",
        )

    return Delivery(
        outcome=DeliveryOutcome.SENT,
        day=digest.day,
        chat_id=channel.chat_id,
        body=body,
    )


def classification_of_a_digest() -> object:
    """What a channel must be able to carry before it may receive one.

    A function rather than a re-exported constant, so that grepping for the callers of this
    finds every place the digest's sensitivity is consulted, and so the answer keeps coming
    from `brain.ops.digest` rather than being copied here.
    """
    return DIGEST_CLASSIFICATION
