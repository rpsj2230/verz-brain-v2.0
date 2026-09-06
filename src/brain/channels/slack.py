"""Slack: a signed request, a workspace that is not the directory, and a room that outlives
the answer.

Slack looks like Lark and is not. Both are chat behind a company login, both have rooms and
direct messages, and the three things that differ are the three things this module is about.

**A captured request stays correctly signed for ever, so the clock is an independent check
and not an optimisation in front of the signature.** Slack signs `v0:{timestamp}:{body}` with
the app's signing secret and sends the digest in `X-Slack-Signature`. A signature that
verifies proves the bytes came from Slack at *some* point; it says nothing whatever about
when, so a request pulled out of a proxy log a month later verifies exactly as well as a live
one. `verify` therefore refuses a timestamp outside the replay window before it has anything
to say about the signature, and the two checks are tested while the other passes, because a
suite where each is only exercised with the other already failing cannot tell which one is
doing the work. The order is safe rather than merely cheap: the timestamp is inside the
signed material, so a replay with a freshened timestamp changes the digest and fails anyway.
See `THE_TIMESTAMP_IS_INSIDE_THE_SIGNED_MATERIAL` and `A_CAPTURED_SIGNATURE_IS_A_VALID_ONE`.

The comparison is `hmac.compare_digest`. `==` on a hex digest returns as soon as two
characters differ, so how long it takes says how much of a guess was right, and a few
thousand requests turn that into a signature. This is `channels.webhook`'s argument and it
does not become less true for having been made once already. What is *not* reused is
`webhook.verify` itself: Lark's basestring is `{timestamp}.{body}` and Slack's is
`v0:{timestamp}:{body}`, and a shared verifier taking a format string would be one function
with two vendors' security properties in it. `assert_raw_bytes` is reused, because the reason
for it is the same on any vendor: the signature covers the bytes that arrived, so verifying a
parsed object verifies a re-serialisation nobody signed.

**The sender is the Slack user id and never the name beside it.** A message event carries
`user` (`U0AB…`), issued by Slack, and older payloads carry `user_name`; a profile carries
`display_name`. Both of the latter are set by the person, so somebody can rename themselves
after a colleague, which is precisely why `channels.whatsapp` refuses the WhatsApp profile
name and `channels.lark.Mention` has no display-name field at all. `normalise_message` reads
`user` and there is deliberately no code here that reads either of the others.

**A message in a channel has an audience that is not the asker, and the audience is not
even bounded by who is in the room now.** This is the part that has no Lark equivalent.
`channels.room.floor` computes the intersection of everybody present, and that is the right
check for a private channel, a group DM and a Slack Connect room. In a *public* channel it is
an under-approximation: anybody in the workspace may join afterwards and Slack hands a joiner
the entire history, so the audience for a message posted today includes people who were not
present when the floor was computed and whom nothing invalidates. `channels.room.revalidate`
closes the gap between rendering and sending; nothing closes the gap between sending and next
year. That is why the ceiling here is `INTERNAL`. Refusing public channels outright was
rejected: it makes the channel useless for the ordinary case, and the answer people would
reach for instead is to ask in a DM and paste the reply into the channel by hand, which is
the same disclosure with no floor computed at all. See
`A_PUBLIC_CHANNELS_AUDIENCE_IS_NOT_BOUNDED_BY_WHO_IS_IN_IT`.

**`chat.postEphemeral` is what lets the asker see more than the room without the room seeing
it, and it is the only thing that does.** `Feature.EPHEMERAL` is genuinely true here, unlike
WhatsApp, so a `Posting` is either read by everybody in the conversation or read by one named
person, and there is no third state. The rule the type enforces is that anything the
conversation reads was computed at the floor, and anything computed at one person's reach has
exactly one reader. Ephemeral is a way to avoid over-sharing and never a way around the
floor: the aside is built from the asker's own reach by `channels.room.plan`, so it can only
ever be narrower than what they hold.

The prompt for an unrecognised sender goes out ephemerally too, and that is a decision this
surface makes and no other one has had to. `gate.ingress.UNRECOGNISED_PROMPT` is written so
that an unknown identity, a known but unbound one and one revoked this morning all get the
same words, because the fact being withheld is whether a handset belongs to somebody here.
None of that argument is about announcing to a room that a named colleague's account is not
set up, which is what a public reply to their message does. So in anything with more than one
reader the prompt is ephemeral, and an installation that cannot post ephemerally is refused
rather than answered in public. The honest alternative, opening a DM, is a write this adapter
does not do.

**No `Feature.CARDS`, and Block Kit buttons are the reason to say so out loud rather than a
reason to declare it.** `gate.admission.CHANNEL_VERBS` gives Slack `read` alone, so an
approval pressed here could never be honoured, and `channels.cards.build_approval_card`
refuses a card on a surface that does not declare the feature rather than building one that
fails at the press. Two Slack facts make that the right ceiling rather than a limitation to
lift later. A workspace is not the tenant identity provider: membership is maintained beside
the directory and includes single-channel guests and Slack Connect members from other
companies, so being in the workspace is not evidence of being staff. And a button on a
message in a channel is pressable by everybody who can see the message, not only by the
person it was addressed to, so a card in a channel is an approval control handed to the room.
An ephemeral card is pressable only by its viewer and cannot be reliably patched afterwards,
because an ephemeral message can only be replaced through a `response_url` that expires,
which is exactly the close `channels.cards.EVERY_CARD_OPENED_MUST_BE_CLOSABLE` requires.
See `A_BUTTON_IN_A_CHANNEL_IS_PRESSABLE_BY_THE_CHANNEL`.

Nothing here opens a connection, imports the SDK, or holds a credential. The signing secret
is an argument to a pure function and the transport lives on the other side of `sent`, for
the reason `channels.whatsapp` gives about itself: the cases worth testing are the permission
ones, and a module that owned an HTTP client could only be tested for them against a live
workspace.

Task ids: M10.5.1
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, assert_never

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.channels.cards import assert_label_survives, render_body
from brain.channels.room import Degradation, Member, plan
from brain.channels.webhook import assert_raw_bytes
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

# ------------------------------------------------------------------ written-down reasons

#: Why the replay window is a check in its own right rather than a cheap pre-filter.
A_CAPTURED_SIGNATURE_IS_A_VALID_ONE: Final = (
    "a signature proves the bytes came from Slack and never says when; a request lifted out "
    "of a proxy log a month later verifies exactly as well as a live one, so freshness is a "
    "second check and not something the first one implies"
)

#: Why checking the clock before the digest gives nothing away.
THE_TIMESTAMP_IS_INSIDE_THE_SIGNED_MATERIAL: Final = (
    "the basestring is v0:{timestamp}:{body}, so a captured request replayed with a freshened "
    "timestamp has a different digest; the window can therefore be checked first without "
    "having trusted an unsigned value, and signing only the body would make it meaningless"
)

#: Why the sender is keyed on the id and never on the name in the same payload.
THE_USER_ID_IS_ISSUED_AND_THE_NAME_IS_TYPED: Final = (
    "user is Slack's own identifier for the account; user_name and display_name are set by "
    "the person, so somebody can rename themselves after a colleague and be keyed on as them"
)

#: Why anything the conversation can read is built at the floor of the conversation.
A_CHANNEL_POSTING_IS_READ_BY_THE_CHANNEL: Final = (
    "a message everybody in the conversation can read is built at the intersection of what "
    "everybody in it holds; the asker's reach decides what the asker is shown privately and "
    "never what a colleague reads over their shoulder"
)

#: Why a per-viewer body exists at all, and the thing it is not.
EPHEMERAL_IS_HOW_THE_ASKER_SEES_MORE_WITHOUT_THE_ROOM_SEEING_IT: Final = (
    "chat.postEphemeral is the only way to say more to one person inside a conversation other "
    "people are in; it is a way to avoid over-sharing and never a way around the floor, "
    "because the aside is built from the asker's own reach and can only be narrower"
)

#: Why this surface declares no cards even though Block Kit is exactly the thing for them.
A_BUTTON_IN_A_CHANNEL_IS_PRESSABLE_BY_THE_CHANNEL: Final = (
    "gate.admission.CHANNEL_VERBS gives Slack read alone, so a press could never be honoured "
    "as an approval; and a button on a message in a channel is pressable by everybody who can "
    "see the message, so a card posted into a room hands the room the control"
)

#: Why the classification ceiling does not rise to what Lark carries.
A_PUBLIC_CHANNELS_AUDIENCE_IS_NOT_BOUNDED_BY_WHO_IS_IN_IT: Final = (
    "joining a public channel hands the joiner its whole history, so a message posted today "
    "is read by people who were not present when the floor was computed; the floor is exact "
    "for a private conversation and an under-approximation for a public one"
)

#: Why the wire address is checked against the plan rather than simply passed alongside it.
A_PLAN_IS_BOUND_TO_ONE_VIEWER: Final = (
    "a posting names its reader by salted digest and the user id is checked against it at the "
    "wire; without that the id is a second argument, and the mistake that shows one person's "
    "answer to another is a variable name"
)

# --------------------------------------------------------------------- the signed request

#: The header carrying the seconds-since-epoch Slack signed with.
TIMESTAMP_HEADER: Final = "X-Slack-Request-Timestamp"

#: The header carrying the digest itself.
SIGNATURE_HEADER: Final = "X-Slack-Signature"

#: The vendor's scheme version. Part of the basestring *and* of the rendered signature, which
#: is why it is one constant used twice rather than two literals that can drift apart.
SIGNATURE_VERSION: Final = "v0"

#: How far out of step a request may be. The vendor's own number, from their guidance that a
#: request more than five minutes from local time should be treated as a replay. Small,
#: because it bounds how long a captured request stays useful; not zero, because clocks differ
#: and strict equality would refuse every request from a machine a second ahead.
SLACK_REPLAY_WINDOW: Final = timedelta(minutes=5)

#: What every refusal on the verification path says, whichever check failed.
#:
#: One message for every reason, for the same argument `channels.webhook.WebhookRefusedError`
#: makes: "stale timestamp" and "bad signature" tell somebody probing which part to change
#: next, and the difference between the two is the whole map of what to try.
NOT_ACCEPTED: Final = "this request was not accepted"

# ------------------------------------------------------------------------- the surface

#: What this surface can do. `EPHEMERAL` is real here, which is the one capability Slack has
#: that WhatsApp and email do not, and the thing the whole audience argument rests on.
#:
#: `CARDS` is absent on purpose; see `A_BUTTON_IN_A_CHANNEL_IS_PRESSABLE_BY_THE_CHANNEL`.
#: `EDIT_IN_PLACE` is absent although `chat.update` exists, because its only consumer is the
#: card path this surface refuses at the build, and for the one per-viewer message kind that
#: is sent here `chat.update` does not work at all: an ephemeral message can only be replaced
#: through a `response_url` that expires. `STREAMING` is absent for the reason
#: `channels.lark` gives about its own: a token-by-token update is one API call each against a
#: rate limit nobody has sized a budget for, and declaring it would spend that budget on
#: cosmetics.
SLACK_FEATURES: Final[frozenset[Feature]] = frozenset({Feature.EPHEMERAL, Feature.ATTACHMENTS})

#: The envelope type the Events API posts around a real event. A `url_verification` challenge
#: arrives in the same place with the same signature and is not a message.
EVENT_CALLBACK: Final = "event_callback"

#: The one inbound event type this normaliser reads.
MESSAGE_EVENT: Final = "message"

#: A sha256 hexdigest, the shape `gate.ingress.identity_hash` produces.
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class SlackRefusedError(Exception):
    """Raised when a request cannot be trusted, an event cannot be read, or a message must
    not be posted where it was aimed.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself: a
    forged request and a private answer aimed at a channel are wiring faults or policy
    boundaries rather than outcomes of somebody's question, and degrading them into an answer
    hides a bug behind a shrug.
    """


def signing_material(timestamp: str, body: bytes) -> bytes:
    """The bytes Slack put through HMAC: `v0:{timestamp}:{body}`.

    Exported so a test can produce a real signature rather than assert against a literal, and
    so the one definition of what is signed lives in one place. The timestamp is inside it,
    which is what makes the replay window mean anything; see
    `THE_TIMESTAMP_IS_INSIDE_THE_SIGNED_MATERIAL`.

    Concatenated as bytes rather than decoded and formatted, because the body is not
    necessarily valid UTF-8 and decoding it to build a string is a re-encoding: what would be
    signed is then whatever survived the round trip rather than what arrived.
    """
    return f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body


def sign(signing_secret: str, timestamp: str, body: bytes) -> str:
    """The signature Slack would have sent for this request, prefix and all.

    The `v0=` prefix is part of the value compared, so the version is not something a sender
    can downgrade by presenting a bare digest: a request claiming an older scheme has a
    signature that does not match the one this produces.
    """
    digest = hmac.new(
        signing_secret.encode("utf-8"), signing_material(timestamp, body), hashlib.sha256
    )
    return f"{SIGNATURE_VERSION}={digest.hexdigest()}"


def verify(
    *,
    signing_secret: str,
    signature: str,
    timestamp: str,
    body: bytes,
    now: datetime,
    window: timedelta = SLACK_REPLAY_WINDOW,
) -> None:
    """Refuse anything that is not a live request from Slack (M10.5.1).

    Raises rather than returning a bool. A function returning False is one whose result can
    be ignored by writing `verify(...)` on a line by itself, and that line reads as a check.

    The order is the argument. The clock is consulted before the digest, and that is sound
    rather than merely cheap because the timestamp is inside the signed material: a captured
    request replayed with a freshened timestamp fails the signature regardless. Reversing the
    two would work equally well and invites the reading that a verified signature means a
    live request, which is exactly what it does not mean. See
    `A_CAPTURED_SIGNATURE_IS_A_VALID_ONE`.

    The window is two-sided. A request from the future is as wrong as one from the past, and
    checking only the past is the mistake that reads as thorough: a sender with a fast clock,
    or an attacker choosing a timestamp, would be accepted for as long as they liked.

    `body` is bytes and there is deliberately no overload taking a parsed object;
    `assert_raw_bytes` is `channels.webhook`'s and is reused rather than restated, because
    the reason holds for any vendor. JSON has no canonical form, so an attacker who can make
    the parse and the re-serialisation differ has a body that verifies as one thing and is
    read as another.
    """
    assert_raw_bytes(body)

    if now.tzinfo is None:
        # Not `NOT_ACCEPTED`, because this is not something a sender can cause. A naive clock
        # here is a caller passing `datetime.utcnow()`, and the failure it produces is every
        # request refused on any machine that is not set to UTC, which presents as Slack
        # being broken rather than as a bug in one argument.
        msg = (
            "the replay window needs an aware clock; a naive one is compared against an epoch "
            "read in the server's own zone, so the window silently becomes that offset wide"
        )
        raise SlackRefusedError(msg)

    try:
        sent_at = datetime.fromtimestamp(int(timestamp), tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise SlackRefusedError(NOT_ACCEPTED) from exc

    if abs(sent_at - now) > window:
        raise SlackRefusedError(NOT_ACCEPTED)

    if not signature.isascii():
        # `hmac.compare_digest` raises TypeError on a non-ASCII string rather than returning
        # False, and the header is whatever the sender typed. Without this a single accented
        # character in `X-Slack-Signature` turns a refusal into an unhandled exception, which
        # is a refusal that arrives as a 500 and reads to an operator as a crash.
        raise SlackRefusedError(NOT_ACCEPTED)

    expected = sign(signing_secret, timestamp, bytes(body))
    if not hmac.compare_digest(expected, signature):
        # Constant time. `==` returns as soon as two characters differ, so how long it takes
        # says how much of a guess was right.
        raise SlackRefusedError(NOT_ACCEPTED)


# ------------------------------------------------------------------ who is in the room


class Surface(enum.StrEnum):
    """Slack's own four words for who can read a conversation. Closed, and checked closed.

    The values are the vendor's (`channel`, `group`, `im`, `mpim`) rather than ours, so the
    wire shape needs no translation table that could drift. `audience_is_one_person` is where
    a fifth kind would have to be given an answer, and `assert_never` makes adding one without
    that answer a type error rather than a default.
    """

    #: A public channel. Anybody in the workspace may join it and read the history.
    PUBLIC = "channel"
    #: A private channel. Membership is closed, and the history still outlives the message.
    PRIVATE = "group"
    #: A one-to-one conversation. The only surface where the asker is the whole audience.
    DIRECT = "im"
    #: A group direct message. More than one reader, so it is a room whatever it is called.
    GROUP_DIRECT = "mpim"


def audience_is_one_person(surface: Surface) -> bool:
    """Whether the only reader is the person who asked.

    The declaration every conversation kind has to make, in the shape
    `gate.context.traffic_class_for` makes it. A dictionary with a default would accept a new
    kind silently, and neither default is safe: `True` answers a room at one person's reach,
    and `False` sends a private conversation through a floor computed over one member.

    `mpim` answers False and that is the member most likely to be got wrong. Slack calls it a
    direct message and it has three to nine people in it.
    """
    match surface:
        case Surface.DIRECT:
            return True
        case Surface.PUBLIC | Surface.PRIVATE | Surface.GROUP_DIRECT:
            return False
        case _:
            assert_never(surface)


# ------------------------------------------------------------- reading what arrived


def _mapping(node: object, what: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        msg = f"{what} is {type(node).__name__} and not an object; this is not a Slack event"
        raise SlackRefusedError(msg)
    return node


def _text(node: Mapping[str, Any], key: str, what: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}; a Slack event without one cannot be read"
        raise SlackRefusedError(msg)
    return value


def _received_at(event: Mapping[str, Any]) -> datetime:
    """Slack's `ts`, which is seconds since the epoch with a microsecond suffix, as a string.

    Converted here rather than passed through, because `ChannelEvent.received_at` is a
    datetime for every channel and a channel handing over a string would make every
    downstream comparison a per-channel special case. WhatsApp's is whole seconds and Lark's
    is milliseconds; the difference living in each normaliser is the point of one internal
    shape.
    """
    raw = _text(event, "ts", "the message")
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        msg = f"message ts {raw!r} is not a Slack timestamp"
        raise SlackRefusedError(msg) from exc


@dataclass(frozen=True)
class SlackMessage:
    """One inbound message, normalised, with the Slack-shaped extras the gate does not carry.

    `event` is the shape every other channel produces, so everything downstream reads one
    type. The extras beside it are the ones a chat has and an email does not: which
    conversation this is, and how many people can read it.
    """

    event: ChannelEvent
    conversation: str
    surface: Surface

    @property
    def sender_identity(self) -> str:
        """The salted digest of the sender's Slack user id.

        Derived rather than stored, so it cannot be set to somebody else's while the event
        beside it says otherwise.
        """
        return identity_hash(self.event.channel, self.event.channel_identity)


def normalise_message(raw: object) -> SlackMessage:
    """The mapping the Events API posts, as one internal shape (M10.5.1).

    The shape accepted is stated rather than inferred: an `event_callback` envelope carrying
    a `message` event with a user, a conversation, a conversation kind, text and a `ts`.
    Anything missing is refused, because every available default is worse than a refusal. An
    absent conversation kind answered as a direct message posts a private answer into a
    channel, and answered as a channel computes a floor over a conversation that has one
    person in it.

    **The sender is `user` and the name beside it is never read.** See
    `THE_USER_ID_IS_ISSUED_AND_THE_NAME_IS_TYPED`. Older payloads and slash commands carry
    `user_name`, and a profile carries `display_name`; both are chosen by the person, so
    keying on either lets somebody rename themselves after a colleague and be answered as
    them.

    **A message carrying `bot_id` is refused.** An app's message is not a question from a
    person, and answering one invites a reply from the app that sent it: two bots answering
    each other in a channel is the loop `channels.email.is_automatic` refuses for the same
    reason, and Slack is where it actually happens because integrations post constantly.

    **A message carrying a `subtype` is refused.** An edit puts the new text under `message`,
    a join carries no question at all, and a file share carries a caption where the question
    would be. Reading any of them as a plain message puts something that is not a question
    through the gate.

    `external_id` is the conversation and the `ts` together. Slack's `ts` is unique within a
    conversation and Slack promises nothing about it across conversations, so keying the
    dedupe on `ts` alone would let one message suppress another that happened to share it.
    The pair is also what a Slack permalink is built from, so it is the vendor's own name for
    one message rather than an identifier invented here.
    """
    envelope = _mapping(raw, "the event")
    envelope_type = _text(envelope, "type", "the event")
    if envelope_type != EVENT_CALLBACK:
        msg = (
            f"this normaliser reads {EVENT_CALLBACK!r} envelopes and this one is "
            f"{envelope_type!r}; a challenge or a rate-limit notice is not a message"
        )
        raise SlackRefusedError(msg)

    event = _mapping(envelope.get("event"), "the event body")
    event_type = _text(event, "type", "the event body")
    if event_type != MESSAGE_EVENT:
        msg = (
            f"this normaliser reads {MESSAGE_EVENT!r} events and this one is {event_type!r}; "
            "a different shape read as a message is a blank question"
        )
        raise SlackRefusedError(msg)

    subtype = event.get("subtype")
    if subtype:
        msg = (
            f"this message has subtype {subtype!r}; an edit carries its new text somewhere "
            "else and a join carries no question at all, so reading one as a plain message "
            "puts something that is not a question through the gate"
        )
        raise SlackRefusedError(msg)

    if event.get("bot_id"):
        msg = (
            "this message was posted by an app, and answering it invites a reply from the app "
            "that sent it; two bots answering each other in a channel is a loop nobody is in"
        )
        raise SlackRefusedError(msg)

    conversation = _text(event, "channel", "the message")
    kind = _text(event, "channel_type", "the message")
    try:
        surface = Surface(kind)
    except ValueError as exc:
        msg = (
            f"conversation kind {kind!r} is none of "
            f"{tuple(s.value for s in Surface)}; how many people read this decides what may "
            "be said in it, and there is no safe guess"
        )
        raise SlackRefusedError(msg) from exc

    ts = _text(event, "ts", "the message")
    return SlackMessage(
        event=ChannelEvent(
            channel=Channel.SLACK,
            external_id=f"{conversation}:{ts}",
            channel_identity=_text(event, "user", "the message"),
            text=_text(event, "text", "the message"),
            received_at=_received_at(event),
        ),
        conversation=conversation,
        surface=surface,
    )


# ------------------------------------------------------------------ planning a reply


class Visibility(enum.StrEnum):
    """Who will read one posting. Closed, because each member is a different guarantee."""

    #: Everybody in the conversation. Built at the floor, always.
    CHANNEL = "channel"
    #: One named person, in a conversation other people are in. `chat.postEphemeral`.
    EPHEMERAL = "ephemeral"
    #: A one-to-one conversation, where the only reader is the person who asked.
    DIRECT = "direct"


@dataclass(frozen=True)
class Rendered:
    """A payload and the reach it was computed at.

    The pair travels together for the reason `gate.context.GateContext` keeps its own pair
    together: a payload separated from the reach it was computed at is one that can be posted
    anywhere, and the mistake looks like a variable name.

    This is `channels.lark.Rendered` restated rather than imported. A channel importing
    another channel's types makes them siblings with a dependency between them, so the day
    Lark's grows a chat-specific third field Slack inherits it; two fields and a claim is
    less cost than that coupling.
    """

    payload: ChannelPayload
    #: `EntitlementSet.ent_hash` of the reach the gate used. A claim by the caller, checked
    #: against the floor this module computes for itself.
    ent_hash: str


@dataclass(frozen=True)
class Posting:
    """One message, to one conversation, with one audience.

    The invariants are on the type rather than in the planner, because a `Posting` is also
    built by hand in a test and by whatever wires this to the SDK later, and an invariant only
    the planner applies is one the second caller does not have.

    **`Visibility.DIRECT` requires `Surface.DIRECT`.** That single line is the module's
    subject in one place: a body computed at one person's reach may be posted plainly only
    where that person is the whole audience. Everywhere else it is ephemeral or it is not
    sent.
    """

    conversation: str
    surface: Surface
    visibility: Visibility
    payload: ChannelPayload
    #: The reach `payload` was computed at.
    ent_hash: str
    degradation: Degradation
    #: The reader, as a salted digest. Empty for a posting the whole conversation reads.
    to_identity: str = ""
    #: Text to send instead of rendering the payload. Empty normally; the unrecognised prompt
    #: is the one thing here that is not built from a payload.
    body: str = ""

    def __post_init__(self) -> None:
        if not self.conversation:
            msg = "a posting with no conversation has nowhere to go"
            raise SlackRefusedError(msg)
        if self.visibility is Visibility.CHANNEL:
            if self.to_identity:
                # A conversation-wide posting addressed to one person is a contradiction that
                # resolves the wrong way on every surface: the address is advisory and the
                # posting is public, so it reads as private and is read by everybody.
                msg = (
                    "a posting the whole conversation reads names a viewer; it would read as "
                    "private and be read by the room. A per-viewer body is EPHEMERAL or it "
                    "is not sent"
                )
                raise SlackRefusedError(msg)
            return
        if self.visibility is Visibility.DIRECT and self.surface is not Surface.DIRECT:
            msg = (
                f"a direct posting was aimed at a {self.surface.value!r} conversation, where "
                f"it is read by everybody in it. {A_CHANNEL_POSTING_IS_READ_BY_THE_CHANNEL}"
            )
            raise SlackRefusedError(msg)
        if not _DIGEST_RE.match(self.to_identity):
            msg = (
                f"a {self.visibility.value} posting names {self.to_identity!r} as its reader. "
                f"{A_PLAN_IS_BOUND_TO_ONE_VIEWER}"
            )
            raise SlackRefusedError(msg)


@dataclass(frozen=True)
class ReplyPlan:
    """Everything that will be sent for one question, and how far it had to fall back.

    `degradation` is carried up from `channels.room.plan` rather than recomputed, so a trace
    can say why somebody got a link instead of an answer without this module having its own
    opinion about which of the four happened.
    """

    postings: tuple[Posting, ...]
    degradation: Degradation
    #: Where the gate can run again for whoever follows it. Only ever set when nothing may be
    #: said here, and required in that case: silence is not one of the outcomes.
    link: str = ""


def assert_the_conversation_only_carries_the_floor(
    postings: Sequence[Posting], floor_hash: str
) -> None:
    """The single enforcement point for `A_CHANNEL_POSTING_IS_READ_BY_THE_CHANNEL` (M10.5.1).

    Asked of what is about to be sent rather than of what arrived. Checking the caller's
    conversation body on the way in reads as the same check and is weaker: it passes while the
    asker's payload is assigned to the conversation posting one branch later, which is the
    mistake that actually happens, because the two postings are built a few lines apart and
    look identical in a diff.

    Public rather than private, unlike `channels.lark`'s equivalent, because whatever wires
    this to the SDK builds postings without going through `plan_reply` and needs the same
    check. A guard reachable only from the one caller that already gets it right is a guard
    the second caller does not have.

    It cannot catch a caller who hands over the asker's payload while claiming the floor's
    hash. Nothing can: the hash is a claim about work this module did not do. What it catches
    is every case where the claim and the destination disagree, which is what a channel is in
    a position to know.
    """
    for posting in postings:
        if posting.visibility is Visibility.CHANNEL and posting.ent_hash != floor_hash:
            msg = (
                f"a posting the conversation reads was computed at {posting.ent_hash!r} and "
                f"this conversation's floor is {floor_hash!r}. "
                f"{A_CHANNEL_POSTING_IS_READ_BY_THE_CHANNEL}"
            )
            raise SlackRefusedError(msg)


def plan_reply(
    message: SlackMessage,
    *,
    members: Sequence[Member],
    asker_id: str,
    capabilities: ChannelCapabilities,
    conversation_body: Rendered,
    asker_body: Rendered,
    now: datetime,
    link: str = "",
) -> ReplyPlan:
    """What to post for one question, where, and at whose reach (M10.5.1).

    Two paths, and they are separate for the reason `channels.lark.plan_delivery` keeps its
    own two apart: a one-to-one conversation has exactly one reader and the answer is theirs,
    while anything else has readers nobody asked the gate about. Sharing a path means one edit
    changes both, and the edit that widens the room is the one nobody sees.

    The room path asks `channels.room.plan` for the floor and the degradation rather than
    computing either here. Nothing in this module redacts or intersects: that is the gate's
    work, and a channel doing it again would be a second opinion whose permissive half wins
    the day the two disagree.

    The reader of an ephemeral posting is derived from the message rather than passed in.
    `message.sender_identity` is the person who asked, so the private body cannot be addressed
    to somebody the message did not come from.
    """
    if audience_is_one_person(message.surface):
        present = frozenset(member.principal_id for member in members)
        if present != {asker_id}:
            # A one-to-one conversation carrying anybody but the asker is a room that arrived
            # mislabelled, and answering it here answers it at one person's reach in front of
            # the others. Refused rather than promoted to the room path: the two descriptions
            # of the same conversation disagree, and picking one is a guess.
            msg = (
                f"this {Surface.DIRECT.value!r} conversation lists {len(present)} members and "
                f"the asker is {asker_id}; a direct conversation with anybody else in it is a "
                "room"
            )
            raise SlackRefusedError(msg)
        # No floor sweep on this path, and that is not an omission: there is no posting the
        # conversation reads to sweep. Running one over postings none of which are CHANNEL
        # would read as an enforcement point while enforcing nothing.
        return ReplyPlan(
            postings=(
                Posting(
                    conversation=message.conversation,
                    surface=message.surface,
                    visibility=Visibility.DIRECT,
                    payload=asker_body.payload,
                    ent_hash=asker_body.ent_hash,
                    degradation=Degradation.FULL,
                    to_identity=message.sender_identity,
                ),
            ),
            degradation=Degradation.FULL,
        )

    render = plan(members, asker_id, capabilities, now=now)
    floor_hash = render.envelope.ent_hash()

    if render.degradation is Degradation.LINK_ONLY:
        if not link:
            # Nothing may be said and there is nowhere to send them. Saying nothing leaves
            # somebody waiting on an answer that is never coming, and the honest alternative
            # is a place the gate runs again for whoever follows it.
            msg = (
                "nothing may be said in this conversation and no link was offered; a question "
                "with no answer and no route is silence, which is not one of the outcomes"
            )
            raise SlackRefusedError(msg)
        return ReplyPlan(postings=(), degradation=render.degradation, link=link)

    built = [
        Posting(
            conversation=message.conversation,
            surface=message.surface,
            visibility=Visibility.CHANNEL,
            payload=conversation_body.payload,
            ent_hash=conversation_body.ent_hash,
            degradation=render.degradation,
        )
    ]

    # `render.aside_for` rather than asking again whether this surface can do ephemeral
    # messages. `channels.room.plan` sets it only for a surface that supports the feature, so
    # repeating the question here would be a branch nothing can reach: two enforcement points
    # that are really one, which is worse than one because the next person to edit this
    # deletes whichever they find first. See
    # `EPHEMERAL_IS_HOW_THE_ASKER_SEES_MORE_WITHOUT_THE_ROOM_SEEING_IT`.
    if render.aside_for:
        built.append(
            Posting(
                conversation=message.conversation,
                surface=message.surface,
                visibility=Visibility.EPHEMERAL,
                payload=asker_body.payload,
                ent_hash=asker_body.ent_hash,
                degradation=render.degradation,
                to_identity=message.sender_identity,
            )
        )

    planned = tuple(built)
    assert_the_conversation_only_carries_the_floor(planned, floor_hash)
    return ReplyPlan(postings=planned, degradation=render.degradation)


def unrecognised_reply(
    reach: Unrecognised, message: SlackMessage, *, capabilities: ChannelCapabilities
) -> Posting:
    """What a sender with no binding is told, in the words `gate.ingress` already wrote.

    **This module defines no prompt of its own**, for the reason `channels.whatsapp` gives:
    `UNRECOGNISED_PROMPT` answers an unknown identity, a known but unbound one, and one whose
    binding was revoked this morning with the same words, and a second prompt written here is
    a second thing to get wrong in the direction that confirms an account belongs to somebody.

    **It goes out ephemerally wherever more than one person reads, and that decision is this
    surface's alone.** The prompt is careful about the fact `gate.ingress` is protecting, and
    says nothing about a colleague's account state being announced to a room; a public reply
    to a named person's message says exactly that to everybody in the channel. So a
    conversation with other people in it gets an ephemeral prompt, and an installation without
    the ephemeral scope is refused rather than answered in public. The alternative, opening a
    direct conversation to say it privately, is a write this adapter does not do, and doing it
    would make the answer to an unrecognised sender a side effect.

    `ent_hash` is empty and that is the honest value rather than a placeholder: there is no
    principal here to have reach, which is the same fact `gate.ingress.Unrecognised` states by
    carrying no `EntitlementSet` at all. Neither posting this builds is one the conversation
    reads, so the empty hash never reaches
    `assert_the_conversation_only_carries_the_floor`; if a later edit made one of them a
    conversation posting, the sweep would refuse it, which is the safe direction.
    """
    if reach.channel is not Channel.SLACK:
        msg = (
            f"this reach was built for {reach.channel} and would be posted to "
            f"{Channel.SLACK}; the prompt a person is given is per channel"
        )
        raise SlackRefusedError(msg)

    if audience_is_one_person(message.surface):
        return Posting(
            conversation=message.conversation,
            surface=message.surface,
            visibility=Visibility.DIRECT,
            payload=ChannelPayload(),
            ent_hash="",
            degradation=Degradation.FULL,
            to_identity=message.sender_identity,
            body=reach.prompt,
        )

    if not capabilities.supports(Feature.EPHEMERAL):
        msg = (
            "this workspace cannot post ephemerally, so the prompt would be posted where "
            "everybody in the conversation reads it; telling a room that a named colleague's "
            "account is not set up here is a disclosure they did not ask for"
        )
        raise SlackRefusedError(msg)

    return Posting(
        conversation=message.conversation,
        surface=message.surface,
        visibility=Visibility.EPHEMERAL,
        payload=ChannelPayload(),
        ent_hash="",
        degradation=Degradation.FULL,
        to_identity=message.sender_identity,
        body=reach.prompt,
    )


# ------------------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class SentMessage:
    """One message this adapter delivered. What a test reads instead of a Slack workspace.

    `viewer` is the digest and not the user id it was addressed with. The id is needed once,
    to reach the wire, and a list of them kept beside the answers they received is the staff
    directory joined to what each person asked, which is what `gate.ingress.Binding` declines
    to keep.
    """

    conversation: str
    body: str
    #: The reader of an ephemeral message, as a digest. Empty when the conversation reads it.
    viewer: str = ""


@dataclass
class SlackAdapter:
    """The Slack surface, with the transport left out on purpose.

    No client, no credentials, no SDK, and the signing secret is an argument to `verify`
    rather than a field here. The vendor's HTTP calls belong on the other side of `sent`, and
    keeping them there is what makes the cases that matter testable: a per-viewer body posted
    where the room reads it is a bug in the planning, and a module that opened a socket could
    only be tested for it against a live workspace.

    `features` is a field rather than a constant because it is genuinely per installation.
    What a Slack app may do depends on the scopes granted when somebody installed it, and an
    installation without `chat:write` for ephemeral posting is a real configuration rather
    than a hypothetical one. An adapter that could not express it would answer `EPHEMERAL`
    and then fail on the wire, which is a private body posted where everybody reads it.

    `reachable` is what `healthy` answers, for the reason `adapter.ChannelAdapter.healthy`
    gives: configured-and-unreachable and never-set-up send a person to different places.
    """

    sent: list[SentMessage] = field(default_factory=list)
    reachable: bool = True
    features: frozenset[Feature] = SLACK_FEATURES

    def capabilities(self) -> ChannelCapabilities:
        """What this installation may carry, declared rather than inferred.

        `INTERNAL` and not `CONFIDENTIAL`, which is where Lark sits. Lark is the tenant
        identity provider's own client, so a Lark account is a directory account. A Slack
        workspace keeps its own membership beside the directory and carries single-channel
        guests and Slack Connect members from other companies, and joining a public channel
        hands the joiner every message ever posted in it, so the audience for a message grows
        after it is sent and no floor computed now bounds it. See
        `A_PUBLIC_CHANNELS_AUDIENCE_IS_NOT_BOUNDED_BY_WHO_IS_IN_IT`. Raising this is a
        decision somebody makes deliberately.

        `can_carry_label` is true: a Slack message is plain text with no template to fall
        out of, so the label renders wherever the body does.
        """
        return ChannelCapabilities(
            channel=Channel.SLACK,
            features=self.features,
            max_classification=Classification.INTERNAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """One inbound message, as the shape the gate reads.

        The conversation and its kind are dropped here on purpose: this is the protocol's
        method and the protocol's return type, and a caller that needs to know how many
        people are in the room calls `normalise_message` and gets the whole thing.
        """
        return normalise_message(raw).event

    def send(
        self,
        payload: ChannelPayload,
        *,
        to: str,
        body: str = "",
        viewer: str = "",
        ephemeral: bool = False,
    ) -> None:
        """Put one message into one conversation (M10.5.1).

        `viewer` empty means everybody in `to` reads it. A viewer named means only they do,
        and that is refused unless this installation actually supports it: an ephemeral
        message sent through a workspace that cannot do them is a private answer posted into a
        room, which is the failure `adapter.Feature.EPHEMERAL` exists to name.

        `ephemeral` is stated separately from `viewer` being set, so that asking for a
        per-viewer message and forgetting the viewer is a refusal rather than a public post.

        `viewer` is the raw Slack user id and what is recorded is its digest. The id reaches
        the wire once and is not kept, for the reason `SentMessage` gives.

        `assert_can_send` runs first and is not restated, so this adapter cannot disagree with
        any other about labels and classifications. The produced string is then checked
        against the payload's label, so a caller cannot hand over a body that dropped it.
        """
        assert_can_send(self.capabilities(), payload)
        if ephemeral != bool(viewer):
            msg = (
                "an ephemeral send names its reader and a conversation-wide one names none; "
                "the two disagree here, and the resolution that reads as safe is the public "
                "one"
            )
            raise SlackRefusedError(msg)
        if viewer and not self.capabilities().supports(Feature.EPHEMERAL):
            msg = (
                f"this {Channel.SLACK} installation cannot post a per-viewer message, so this "
                "body would be posted where everybody in the conversation reads it"
            )
            raise SlackRefusedError(msg)
        rendered = body or render_body(payload)
        assert_label_survives(rendered, payload)
        self.sent.append(
            SentMessage(
                conversation=to,
                body=rendered,
                viewer=identity_hash(Channel.SLACK, viewer) if viewer else "",
            )
        )

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: SlackAdapter, posting: Posting, *, to_user: str = "") -> None:
    """Send one planned posting, to the reader it was planned for (M10.5.1).

    The user id arrives here and nowhere else. A `Posting` holds a digest, so whoever resolved
    the binding supplies the id at the wire and this checks the two agree. See
    `A_PLAN_IS_BOUND_TO_ONE_VIEWER`: without the check the id is simply a second argument, and
    handing this function the wrong one is a mistake that looks like a variable name.

    The check is required on a direct posting as well as an ephemeral one, although the
    conversation id is the address in both. It does not prove that a given `D…` conversation
    belongs to that person, which is Slack's mapping and not something this module can see; it
    proves that the caller resolving the binding and the planner agree about who is being
    answered, which is where the mistake is actually made.

    A posting the whole conversation reads is refused a user id. A caller passing one has
    confused the two paths, and the resolution that reads as harmless is to post it publicly.

    The mapping from a plan to a send is here rather than on the adapter, so `send` keeps the
    signature `redaction.assert_channel_adapter` can check: a parameter typed `Posting` names
    no `ChannelPayload`, and an adapter taking one could not be shown safe by reading it.
    """
    if posting.visibility is Visibility.CHANNEL:
        if to_user:
            msg = (
                "a posting the whole conversation reads was handed a user id; it names nobody "
                "because everybody in the conversation reads it, and a caller supplying one "
                "has confused a public posting with a private one"
            )
            raise SlackRefusedError(msg)
        adapter.send(posting.payload, to=posting.conversation, body=posting.body)
        return

    if identity_hash(Channel.SLACK, to_user) != posting.to_identity:
        # Names neither the user id nor the digest it was expected to match. Both reach a log
        # from here, and the pair of them is the directory this module declines to keep.
        msg = f"this posting was planned for somebody else. {A_PLAN_IS_BOUND_TO_ONE_VIEWER}"
        raise SlackRefusedError(msg)

    ephemeral = posting.visibility is Visibility.EPHEMERAL
    adapter.send(
        posting.payload,
        to=posting.conversation,
        body=posting.body,
        viewer=to_user if ephemeral else "",
        ephemeral=ephemeral,
    )
