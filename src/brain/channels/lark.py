"""Lark: how a message is read, and who is allowed to read the answer.

Lark is the channel most of the company will actually use, and it is the one where the
audience is not the asker. Everything difficult here follows from that.

**A group chat has more than one reader and the answer was computed for one caller.**
`channels.room.floor` already computes the intersection of everyone present, and
`channels.room.plan` decides how far an answer had to fall back. What was missing is the
step after: turning that decision into concrete messages without the asker's answer being
one of them. So a room posting carries the payload computed at the floor, a per-viewer body
is delivered ephemerally to one person, and `_assert_room_only_carries_the_floor` refuses a
plan where anything going to the room was computed at any other reach. Posting the asker's
full answer into a room and hoping nobody else reads it is the failure this module exists to
prevent, and it is invisible in a diff: the message looks like every other message.

**The floor check is on the way out, not on the way in.** The rejected design checked the
caller's `room_body` against the floor as it arrived. That misses the bug that actually
happens, which is not a mislabelled input but the asker's payload reaching the room posting
one branch later. Checking every delivery that is about to go to the room catches both, so
there is one enforcement point rather than two that look like two and are really one.

**A mention is attacker-influenced text and the stable id is not.** Lark puts display names
in the message body: a person can rename themselves after a colleague, or simply type
`@Brain` and `@_user_1` into their own text. A parser that read the rendered text would let
either of those address the bot on somebody else's behalf. `Mention` therefore carries the
placeholder key and the salted hash of the open id, and deliberately **has no display-name
field at all**: a name held on the object is a name somebody eventually compares on.
`gate.ingress.identity_hash` is the hash, reused rather than reimplemented, so a mention and
a binding agree about what an identity is.

**Nobody is addressed by their raw channel identity.** A `Delivery` names a viewer by
`identity_hash` and refuses anything that is not one. An open id in a delivery is an open id
one interpolation away from a message body, and `gate.ingress.Binding` makes the same choice
for the same reason: a table of them is a phone book of the company.

**A direct message and a group message take different paths on purpose.** They could share
one, with a one-member room, and `room.plan` would return FULL and the right answer. It was
rejected because the two differ in what a mistake costs. A p2p chat has exactly one reader
and the answer is theirs; a group has readers nobody asked the gate about. Sharing a path
means one edit changes both, and the edit that widens the group is the one nobody sees. The
direct path here also refuses a p2p chat carrying more than the asker, which is the wiring
fault that would otherwise turn a group into a one-member room.

Nothing here opens a connection and there is no SDK. The event shape is data: `normalise`
accepts the mapping Lark posts, refuses anything it cannot read, and produces the same
`gate.ingress.ChannelEvent` every other channel produces. A module that owned an HTTP client
could not be tested for the case that matters, which is a group message rendered at the
wrong reach.

Task ids: M10.2.2, M10.2.5, M10.2.6
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, assert_never

from brain.channels.adapter import (
    ChannelCapabilities,
    DeliveryRefusedError,
    Feature,
    assert_can_send,
)
from brain.channels.cards import render_body
from brain.channels.room import Degradation, Member, plan
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, identity_hash

# ------------------------------------------------------------------ written-down reasons

#: Why a mention is resolved by open id and never by the name beside it.
#:
#: The display name is chosen by the person and rendered into the message text, so it is
#: attacker-influenced twice over: somebody can rename themselves after a colleague, and
#: somebody can type another person's name into their own message. The open id is issued by
#: the tenant and appears in a structured field the sender does not author.
MENTIONS_ARE_KEYED_ON_THE_STABLE_ID: Final = (
    "a mention is resolved by the platform's own identifier, never by the rendered name; a "
    "parser that trusted display names lets somebody name themselves after a colleague"
)

#: Why everything posted where more than one person reads it is built at the floor.
A_ROOM_IS_ANSWERED_AT_ITS_FLOOR: Final = (
    "a message everybody in the room can read is built at the intersection of what everybody "
    "in the room holds; the asker's reach decides what the asker sees privately and never "
    "what a colleague reads"
)

#: Why a viewer is named by hash.
A_VIEWER_IS_NAMED_BY_HASH: Final = (
    "a delivery addresses a person by the salted digest of their channel identity; a raw "
    "open id in a delivery is one interpolation away from being in a message body"
)

#: A sha256 hexdigest, the shape `gate.ingress.identity_hash` produces.
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: What a fully installed Lark app can do. The default for `LarkAdapter.features`, and not
#: a floor: an installation granted fewer scopes declares fewer, which is why the adapter
#: carries this as a field rather than returning it from a constant method.
#:
#: `STREAMING` is absent deliberately. Lark can update a card as tokens arrive, and doing so
#: costs one patch per update against the ceiling `channels.cards` budgets; declaring the
#: feature before that budget has been sized for it would spend the close reserve on
#: cosmetics.
LARK_FEATURES: Final[frozenset[Feature]] = frozenset(
    {Feature.EPHEMERAL, Feature.CARDS, Feature.EDIT_IN_PLACE, Feature.ATTACHMENTS}
)

#: The one message type this normaliser reads. Others are refused rather than guessed at:
#: an image, a file or a card action is a different event with a different shape, and
#: pretending an empty string is its text would put a blank question through the gate.
TEXT_MESSAGE: Final = "text"


class LarkRefusedError(Exception):
    """Raised when a Lark event cannot be read, or an answer cannot be planned for one.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself: a
    malformed event and a mis-built plan are wiring faults rather than outcomes of somebody's
    question, and degrading them into an answer hides a bug behind a shrug.
    """


# ---------------------------------------------------------------- chat shape (M10.2.6)


class ChatType(enum.StrEnum):
    """Lark's own two words for who is in a conversation. Closed, and checked closed.

    The values are the vendor's (`p2p`, `group`) rather than ours, so the wire shape needs no
    translation table that could drift. `audience_is_one_person` is where a third member
    would have to be given an answer, and `assert_never` makes adding one without that
    answer a type error rather than a default.
    """

    DIRECT = "p2p"
    GROUP = "group"


def audience_is_one_person(chat: ChatType) -> bool:
    """Whether the only reader is the person who asked.

    The declaration every chat type has to make, in the shape `gate.context.traffic_class_for`
    makes it. A dictionary with a default would accept a new chat type silently, and the
    default that reads as safe (`False`, treat it as a group) is the one that would answer a
    private question at a floor nobody is standing on.
    """
    match chat:
        case ChatType.DIRECT:
            return True
        case ChatType.GROUP:
            return False
        case _:
            assert_never(chat)


# ------------------------------------------------------------------ mentions (M10.2.2)


@dataclass(frozen=True)
class Mention:
    """One person named in a message. A placeholder key and a stable identity, and no name.

    **There is deliberately no `display_name` field.** See
    `MENTIONS_ARE_KEYED_ON_THE_STABLE_ID`. Keeping the name "for rendering" is how it ends up
    in a comparison: the first time somebody wants to reply "@Wei Ling, here is that figure",
    the name is on the object and the shortest route to it is the one that gets written.
    Rendering a mention back is the vendor's job through the placeholder key, which is what
    the key is for.

    `identity` is `gate.ingress.identity_hash`, salted per channel. Reused rather than
    recomputed here so a mention and a binding cannot disagree about who somebody is.
    """

    #: The token that stands in for this person inside the message text (`@_user_1`).
    key: str
    #: The salted digest of the platform's own identifier for them.
    identity: str


@dataclass(frozen=True)
class LarkMessage:
    """One inbound message, normalised, with the Lark-shaped extras the gate does not carry.

    `event` is the shape every other channel produces, so everything downstream reads one
    type. The extras beside it are the ones a chat has and an email does not: which
    conversation this is, whether more than one person is in it, and who was named.
    """

    event: ChannelEvent
    chat_id: str
    chat_type: ChatType
    mentions: tuple[Mention, ...] = ()

    @property
    def sender_identity(self) -> str:
        """The salted digest of the sender's channel identity.

        Derived rather than stored, so it cannot be set to somebody else's while the event
        beside it says otherwise.
        """
        return identity_hash(self.event.channel, self.event.channel_identity)


def _mapping(node: object, what: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        msg = f"{what} is {type(node).__name__} and not an object; this is not a Lark event"
        raise LarkRefusedError(msg)
    return node


def _text(node: Mapping[str, Any], key: str, what: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}; a Lark event without one cannot be read"
        raise LarkRefusedError(msg)
    return value


def _received_at(header: Mapping[str, Any]) -> datetime:
    """Lark's `create_time`, which is milliseconds since the epoch as a string.

    Converted here rather than passed through, because `ChannelEvent.received_at` is a
    datetime for every channel and a channel that handed over a string would make every
    downstream comparison a per-channel special case.
    """
    raw = _text(header, "create_time", "the event header")
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        msg = f"event create_time {raw!r} is not a millisecond timestamp"
        raise LarkRefusedError(msg) from exc


def _mention(raw: object, index: int) -> Mention:
    """One mention, keyed on the open id and refusing one without it.

    Refused rather than skipped. A mention we cannot key on a stable id is one that could
    only be resolved by the name beside it, and silently dropping it makes a message that
    *did* address the bot read as one that did not, which is a denial somebody can cause on
    purpose by sending a malformed mention.
    """
    node = _mapping(raw, f"mention {index}")
    identifiers = _mapping(node.get("id"), f"mention {index} id")
    open_id = _text(identifiers, "open_id", f"mention {index}")
    return Mention(
        key=_text(node, "key", f"mention {index}"),
        identity=identity_hash(Channel.LARK, open_id),
    )


def normalise_message(raw: object) -> LarkMessage:
    """The mapping Lark posts, as one internal shape (M10.2.2).

    The shape accepted is stated here rather than inferred from whatever arrives: header,
    event, sender open id, message id, chat id, chat type, message type and content. Anything
    missing is refused, because the alternative is a default, and the defaults available are
    all worse than a refusal: an empty text is a blank question through the gate, an absent
    chat type is a group answered as a direct message or the reverse.

    **The text is taken from `content` and the mentions from `mentions`, and the two are
    never crossed.** The content is what the sender typed, placeholders and all; a sender can
    put `@_user_1` or a colleague's name in it. Only the structured `mentions` array carries
    identifiers the tenant issued, so it is the only thing `addressed_to` reads.
    """
    envelope = _mapping(raw, "the event")
    header = _mapping(envelope.get("header"), "the event header")
    body = _mapping(envelope.get("event"), "the event body")
    sender = _mapping(body.get("sender"), "the sender")
    sender_id = _mapping(sender.get("sender_id"), "the sender id")
    message = _mapping(body.get("message"), "the message")

    message_type = _text(message, "message_type", "the message")
    if message_type != TEXT_MESSAGE:
        msg = (
            f"this normaliser reads {TEXT_MESSAGE!r} messages and this one is "
            f"{message_type!r}; a different shape read as text is a blank question"
        )
        raise LarkRefusedError(msg)

    raw_content = _text(message, "content", "the message")
    try:
        content = _mapping(json.loads(raw_content), "the message content")
    except json.JSONDecodeError as exc:
        msg = "the message content is not JSON; Lark encodes it as a JSON string"
        raise LarkRefusedError(msg) from exc

    chat_type = _text(message, "chat_type", "the message")
    try:
        chat = ChatType(chat_type)
    except ValueError as exc:
        msg = (
            f"chat type {chat_type!r} is neither {ChatType.DIRECT.value!r} nor "
            f"{ChatType.GROUP.value!r}; whether more than one person reads this decides "
            "what may be said in it"
        )
        raise LarkRefusedError(msg) from exc

    mentions = message.get("mentions") or ()
    if not isinstance(mentions, Sequence) or isinstance(mentions, str | bytes):
        msg = "the message mentions is not a list; a mention cannot be read from a string"
        raise LarkRefusedError(msg)

    return LarkMessage(
        event=ChannelEvent(
            channel=Channel.LARK,
            external_id=_text(message, "message_id", "the message"),
            channel_identity=_text(sender_id, "open_id", "the sender"),
            text=_text(content, "text", "the message content"),
            received_at=_received_at(header),
        ),
        chat_id=_text(message, "chat_id", "the message"),
        chat_type=chat,
        mentions=tuple(_mention(item, index) for index, item in enumerate(mentions)),
    )


def addressed_to(message: LarkMessage, *, identity: str) -> bool:
    """Whether this message named that identity (M10.2.2).

    Reads `mentions` and never `event.text`. See `MENTIONS_ARE_KEYED_ON_THE_STABLE_ID`: the
    text contains whatever the sender typed, so a message whose words say `@Brain` has not
    addressed the bot, and a message whose words say nothing of the sort has, if the tenant
    put the bot's open id in the structured field.
    """
    return any(mention.identity == identity for mention in message.mentions)


def should_answer(message: LarkMessage, *, bot_identity: str) -> bool:
    """Whether the bot has been spoken to (M10.2.6).

    A direct message is addressed by arriving: there is nobody else in the conversation to
    have meant it for. In a group it has to be a mention, because answering everything in a
    room is a bot that reads a colleague's question about a client and answers it to the
    room at a floor nobody asked it to compute.
    """
    return audience_is_one_person(message.chat_type) or addressed_to(message, identity=bot_identity)


# ------------------------------------------------------- delivery (M10.2.5, M10.2.6)


class Visibility(enum.StrEnum):
    """Who will read one message. Closed, because each member is a different guarantee."""

    #: Everybody in the chat. Built at the floor, always.
    ROOM = "room"
    #: One named person, in a chat other people are in. The mechanism that lets somebody see
    #: more than the floor without the floor moving.
    EPHEMERAL = "ephemeral"
    #: A one-to-one chat, where the only reader is the person who asked.
    DIRECT = "direct"


@dataclass(frozen=True)
class Rendered:
    """A payload and the reach it was computed at.

    The pair travels together for the reason `gate.context.GateContext` keeps its own pair
    together: a payload separated from the reach it was computed at is one that can be
    posted anywhere, and the mistake looks like a variable name.
    """

    payload: ChannelPayload
    #: `EntitlementSet.ent_hash` of the reach the gate used. A claim by the caller, checked
    #: against the floor this module computes for itself.
    ent_hash: str


@dataclass(frozen=True)
class Delivery:
    """One message, to one place, with one audience.

    The invariants are on the type rather than in the planner, because a `Delivery` is also
    built by hand in a test and by whatever wires this to the SDK later, and an invariant
    that only the planner applies is one the second caller does not have.
    """

    chat_id: str
    visibility: Visibility
    payload: ChannelPayload
    #: The reach `payload` was computed at.
    ent_hash: str
    degradation: Degradation
    #: The viewer, as a salted digest. Empty for a room posting.
    to_identity: str = ""

    def __post_init__(self) -> None:
        if not self.chat_id:
            msg = "a delivery with no chat has nowhere to go"
            raise LarkRefusedError(msg)
        if self.visibility is Visibility.ROOM:
            if self.to_identity:
                # A room posting addressed to one person is a contradiction that resolves
                # the wrong way on every surface: the address is advisory and the posting is
                # public, so it reads as private and is read by everybody.
                msg = (
                    "a room posting names a viewer; it would read as private and be posted "
                    "publicly. A per-viewer body is EPHEMERAL or it is not sent"
                )
                raise LarkRefusedError(msg)
            return
        if not _DIGEST_RE.match(self.to_identity):
            msg = (
                f"{self.visibility} delivery names {self.to_identity!r} as its viewer. "
                f"{A_VIEWER_IS_NAMED_BY_HASH}"
            )
            raise LarkRefusedError(msg)


@dataclass(frozen=True)
class DeliveryPlan:
    """Everything that will be sent for one question, and how far it had to fall back.

    `degradation` is carried up from `room.plan` rather than recomputed, so a trace can say
    why somebody got a link instead of an answer without this module having its own opinion
    about which of the four happened.
    """

    deliveries: tuple[Delivery, ...]
    degradation: Degradation
    #: Where the gate can run again for whoever follows it. Only ever set when nothing may
    #: be said here, and required in that case: silence is not one of the outcomes.
    link: str = ""


def _assert_room_only_carries_the_floor(deliveries: Sequence[Delivery], floor_hash: str) -> None:
    """The single enforcement point for `A_ROOM_IS_ANSWERED_AT_ITS_FLOOR` (M10.4.1, M10.2.6).

    Asked of what is about to be sent rather than of what arrived. Checking the caller's
    `room_body` on the way in reads as the same check and is weaker: it passes while the
    asker's payload is assigned to the room posting one branch later, which is the mistake
    that actually happens. Everything with `Visibility.ROOM` is checked here, so both the
    mislabelled input and the misrouted body are refused by one condition.

    It cannot catch a caller who hands over the asker's payload while claiming the floor's
    hash. Nothing can: the hash is a claim about work this module did not do. What it does
    catch is every case where the claim and the destination disagree, which is what a
    channel is in a position to know.
    """
    for delivery in deliveries:
        if delivery.visibility is Visibility.ROOM and delivery.ent_hash != floor_hash:
            msg = (
                f"a room posting was computed at {delivery.ent_hash!r} and this room's floor "
                f"is {floor_hash!r}. {A_ROOM_IS_ANSWERED_AT_ITS_FLOOR}"
            )
            raise LarkRefusedError(msg)


def plan_delivery(
    message: LarkMessage,
    *,
    members: Sequence[Member],
    asker_id: str,
    capabilities: ChannelCapabilities,
    room_body: Rendered,
    asker_body: Rendered,
    now: datetime,
    link: str = "",
) -> DeliveryPlan:
    """What to send for one question, to whom, and at whose reach (M10.2.5, M10.2.6).

    Two paths, and they are separate on purpose; see the module docstring. The direct path
    answers the one person in the conversation at their own reach. The group path asks
    `room.plan` for the floor and the degradation, posts the floor to the room, and delivers
    the asker's own body ephemerally when there is more of it and the surface can do that.

    The viewer of an ephemeral delivery is derived from the event rather than passed in.
    `message.sender_identity` is the person who asked, and taking it from the message means
    the private body cannot be addressed to somebody the message did not come from.

    `room_body` and `asker_body` are computed by the gate and handed over. Nothing here
    redacts or intersects: that is the gate's work, and a channel doing it again would be a
    second opinion whose permissive half wins the day the two disagree.
    """
    if audience_is_one_person(message.chat_type):
        present = frozenset(member.principal_id for member in members)
        if present != {asker_id}:
            # A p2p chat carrying anybody but the asker is a group that arrived mislabelled,
            # and answering it on this path would answer it at one person's reach in front
            # of the others. Refused rather than promoted to the group path: the two
            # descriptions of the same chat disagree, and picking one is a guess.
            msg = (
                f"this {ChatType.DIRECT.value} chat lists {len(present)} members and the "
                f"asker is {asker_id}; a direct chat with anybody else in it is a group"
            )
            raise LarkRefusedError(msg)
        deliveries = (
            Delivery(
                chat_id=message.chat_id,
                visibility=Visibility.DIRECT,
                payload=asker_body.payload,
                ent_hash=asker_body.ent_hash,
                degradation=Degradation.FULL,
                to_identity=message.sender_identity,
            ),
        )
        # No floor sweep on this path, and that is not an omission. There is no room
        # posting to sweep: a p2p chat has one reader, the check above has proved it is the
        # asker, and running a sweep over deliveries none of which are `ROOM` would read as
        # an enforcement point while enforcing nothing.
        return DeliveryPlan(deliveries=deliveries, degradation=Degradation.FULL)

    render = plan(members, asker_id, capabilities, now=now)
    floor_hash = render.envelope.ent_hash()

    if render.degradation is Degradation.LINK_ONLY:
        if not link:
            # Nothing may be said and there is nowhere to send them. Saying nothing at all
            # leaves somebody waiting on an answer that is never coming, and the honest
            # alternative to an answer is a place the gate runs again for whoever follows it.
            msg = (
                "nothing may be said in this room and no link was offered; a question with "
                "no answer and no route is silence, which is not one of the outcomes"
            )
            raise LarkRefusedError(msg)
        return DeliveryPlan(deliveries=(), degradation=render.degradation, link=link)

    built = [
        Delivery(
            chat_id=message.chat_id,
            visibility=Visibility.ROOM,
            payload=room_body.payload,
            ent_hash=room_body.ent_hash,
            degradation=render.degradation,
        )
    ]

    # `render.aside_for` rather than a comparison of the two reaches, and rather than a
    # second check that this surface can do ephemeral messages. `room.plan` sets it only for
    # a surface that supports the feature, so repeating the question here would be a branch
    # nothing can reach: two enforcement points that are really one, which is worse than one
    # because the next person to edit this deletes whichever they find first. The check that
    # a per-viewer send is honourable belongs to the adapter, which is where it is.
    if render.aside_for:
        built.append(
            Delivery(
                chat_id=message.chat_id,
                visibility=Visibility.EPHEMERAL,
                payload=asker_body.payload,
                ent_hash=asker_body.ent_hash,
                degradation=render.degradation,
                to_identity=message.sender_identity,
            )
        )

    planned = tuple(built)
    _assert_room_only_carries_the_floor(planned, floor_hash)
    return DeliveryPlan(deliveries=planned, degradation=render.degradation)


# ------------------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class SentMessage:
    """One message this adapter delivered. What a test reads instead of a Lark tenant."""

    chat_id: str
    body: str
    #: The viewer of an ephemeral message, as a digest. Empty when everybody in the chat
    #: reads it.
    viewer: str = ""


@dataclass
class LarkAdapter:
    """The Lark surface, with the transport left out on purpose.

    There is no client here and no credentials. The vendor's SDK belongs on the other side
    of `sent`, and keeping it there is what makes the case that matters testable: a group
    message rendered at the wrong reach is a bug in the planning, and a module that opened a
    socket could only be tested for it with a Lark tenant standing by.

    `reachable` is what `healthy` answers. A flag rather than a probe, for the reason
    `adapter.ChannelAdapter.healthy` gives: configured-and-unreachable and never-set-up send
    a person to different places, and this type has to be able to express the first.

    `features` is a field rather than a constant because it is genuinely per tenant. What a
    Lark app may do depends on the scopes it was granted when somebody installed it, and an
    installation without the ephemeral scope is a real configuration rather than a
    hypothetical one. An adapter that could not express it would answer `EPHEMERAL` and then
    fail on the wire, which is a private body posted where everybody reads it.
    """

    sent: list[SentMessage] = field(default_factory=list)
    reachable: bool = True
    features: frozenset[Feature] = LARK_FEATURES

    def capabilities(self) -> ChannelCapabilities:
        """What this installation can do, declared rather than inferred.

        `CONFIDENTIAL` and not `RESTRICTED`, and that half is not configurable. Lark is
        behind the tenant and the identity provider, which is why it carries more than
        WhatsApp does; it is also a chat client installed on personal phones with history,
        search and export, which is why the most sensitive class stays in the console.
        Raising that line is a decision somebody makes deliberately, not something an
        adapter infers from having a card renderer.
        """
        return ChannelCapabilities(
            channel=Channel.LARK,
            features=self.features,
            max_classification=Classification.CONFIDENTIAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """Whatever arrived, as the one shape the gate reads."""
        return normalise_message(raw).event

    def send(
        self, payload: ChannelPayload, *, to: str, viewer: str = "", ephemeral: bool = False
    ) -> None:
        """Deliver one payload into one chat (M10.2.5).

        `viewer` empty means everybody in `to` reads it. A viewer named means only they do,
        and that is refused unless this surface actually supports it: an ephemeral message
        sent to a channel that cannot do ephemeral messages is a private answer posted into
        a room, which is the failure `adapter.Feature.EPHEMERAL` exists to name.

        `ephemeral` is stated separately from `viewer` being set, so that asking for a
        per-viewer message and forgetting the viewer is a refusal rather than a public post.

        `assert_can_send` runs first and is not restated here, so this adapter cannot
        disagree with any other about labels and classifications.
        """
        assert_can_send(self.capabilities(), payload)
        if ephemeral != bool(viewer):
            msg = (
                "an ephemeral send names its viewer and a public one names none; the two "
                "disagree here, and the resolution that reads as safe is the public one"
            )
            raise DeliveryRefusedError(msg)
        if viewer and not self.capabilities().supports(Feature.EPHEMERAL):
            msg = (
                f"{Channel.LARK} cannot send a per-viewer message, so this body would be "
                "posted where everybody in the chat reads it"
            )
            raise DeliveryRefusedError(msg)
        self.sent.append(SentMessage(chat_id=to, body=render_body(payload), viewer=viewer))

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: LarkAdapter, delivery: Delivery) -> None:
    """Send one planned delivery.

    The mapping from a `Visibility` to a send is here rather than on the adapter, so that
    `LarkAdapter.send` keeps the signature `redaction.assert_channel_adapter` can check: a
    parameter typed `Delivery` names no `ChannelPayload`, and an adapter that took one could
    not be shown safe by reading it.
    """
    ephemeral = delivery.visibility is Visibility.EPHEMERAL
    adapter.send(
        delivery.payload,
        to=delivery.chat_id,
        viewer=delivery.to_identity if ephemeral else "",
        ephemeral=ephemeral,
    )
