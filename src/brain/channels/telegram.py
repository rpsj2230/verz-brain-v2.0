"""Telegram: a webhook authenticated by a header, and a group with no floor to answer at.

The Bot API posts updates to an HTTPS endpoint of our choosing. A URL is not a secret: it
sits in a deployment log, in a reverse proxy's access log, in whatever registered the
webhook and in the browser history of whoever tested it. So everything in this file rests
on one check, and the rest of the module is about what that check does not buy.

**The secret token is compared in constant time, and nothing here parses anything until it
passes.** Telegram sets `X-Telegram-Bot-Api-Secret-Token` on every delivery to the value
given when the webhook was registered. Without checking it, anybody who learns the URL
posts an update naming any user id they choose, and this system answers as that person, at
that person's reach, having done nothing wrong at any later step. `normalise_update`
therefore takes a `VerifiedUpdate`, a type only `verified_update` can construct, so
skipping the check is not a thing a caller can do one new call site at a time.
`channels.webhook.verified_handler` uses this shape for the same reason and
`gate.catalogue.ProjectedCatalogue` is where the constructor token comes from.

**The header authenticates Telegram and never the person.** It is a bearer credential
posted beside the body rather than a signature over it, so unlike
`channels.webhook.verify` there is nothing binding the credential to these bytes, no
timestamp to age and no nonce to remember. Anyone who has ever observed the header can
post anything for as long as it stays unchanged. Two consequences are written down rather
than glossed: TLS is what keeps it unobserved and this module cannot enforce that, and a
replay is caught downstream by `ChannelEvent.external_id` dedupe and by nothing here. See
`THE_HEADER_AUTHENTICATES_TELEGRAM_AND_NOT_THE_PERSON`.

**The identity is the numeric `from.id` and never the username.** A Telegram username is
lent rather than owned: the holder can change it, and a released handle can be registered
by somebody else, who would then arrive holding a binding made for the person who had it
before. The numeric id is issued by Telegram and does not move. `channels.whatsapp`
refuses the WhatsApp profile name and `channels.email.sender_address` discards the display
name for the same class of reason, and this is the sharper case of the three: the other
two are attacker-chosen text, and this one is attacker-*acquirable*. There is no code path
in this file that reads `username`. See `A_USERNAME_IS_LENT_AND_THE_NUMERIC_ID_IS_NOT`.

**An answer computed at one person's reach may only go where one person reads.** An update
arrives from a private chat, a group, a supergroup or a broadcast channel. Lark solves the
group case by computing `channels.room.floor` over everybody present and posting at that
floor; that is not available here, because `floor` needs the members and the Bot API will
not enumerate them. There is `getChatMember` for an id you already hold and
`getChatAdministrators` for the admins, and nothing that lists a group. A floor over an
unknown set is not a floor. Telegram also has no per-viewer message in a group, which is a
real difference from Slack's ephemeral post rather than a feature nobody wired up: there is
no mechanism to hand one member of a group something the others do not get.

So a group gets no answer at all. It gets `GROUP_DEFLECTION`, fixed words asking the person
to continue in a direct message, and the reason the instruction points that way is that a
bot cannot open a private chat first: Telegram permits a bot to message a user only after
that user has started a conversation with it. The rule is carried by the types rather than
by a check, because a check is a thing a later branch goes around. `Answer` carries a
payload and is only ever addressed to a person; `Notice` addresses a chat of any size and
**has no payload field at all**, so there is nowhere to put an answer in the thing that can
reach a room. `channels.whatsapp.SlotSource` is the same trick.

**A bot in a group sees a fraction of the group.** With privacy mode on, which is the
default, a bot receives commands, replies to its own messages, messages that mention it and
service messages, and not the rest of the conversation. Nothing here may therefore treat
what it has seen as the conversation: there is no history buffer in this module, no thread
assembled from prior messages and no wording anywhere that claims not to have understood
something, because the messages it did not receive are indistinguishable from messages that
were never sent. Turning privacy mode off changes what arrives and changes nothing about
what this module does with it, which is the property worth having: a setting in somebody
else's console cannot quietly widen this. See `A_BOT_IN_A_GROUP_SEES_A_FRACTION_OF_IT`.

**A callback query is refused rather than handled.** `gate.admission.CHANNEL_VERBS` gives
Telegram `read` alone, so a press could never be honoured as an approval, which is the
argument `channels.whatsapp` makes about reply buttons. There is a second one here and it
is stronger: this adapter declares no `Feature.CARDS` and sends no inline keyboard, so a
callback query addressed to this bot is a press on a button this system did not send. That
is evidence of a fault, not an input. And an inline keyboard on a group message is
pressable by every member of the group, so the press names whoever got there first rather
than the person a decision belonged to.

**Nothing is edited in place, and that is not a claim about Telegram.** `editMessageText`
works. `adapter.Feature.EDIT_IN_PLACE` says in its own docstring what the feature is for,
which is stopping a card being actionable once somebody else has taken the decision it
offers, and this surface has no cards to close. A capability is declared so callers can
branch on it, so declaring one nothing here can serve is a false statement about this
adapter rather than a harmless truth about the vendor. See
`EDITING_A_MESSAGE_HERE_HAS_NOTHING_TO_CLOSE`.

Nothing in this file opens a socket, imports an SDK or holds a bot token. The transport
lives on the other side of `sent`, for the reason `channels.whatsapp` gives: the case worth
testing is an answer reaching a chat it was not computed for, and a module that owned an
HTTP client could only be tested for it against a live bot.

Task ids: M10.5.4
"""

from __future__ import annotations

import enum
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, assert_never

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.channels.cards import assert_label_survives, render_body
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

# ------------------------------------------------------------------ written-down reasons

#: What the header proves, and the much smaller thing it is often read as proving.
THE_HEADER_AUTHENTICATES_TELEGRAM_AND_NOT_THE_PERSON: Final = (
    "the secret token says this update came from something holding the secret; it says "
    "nothing at all about who sent the message, so it is evidence about the transport and "
    "never evidence about a sender, and no assurance may be derived from it"
)

#: Why the numeric id is the identity and the handle beside it is not.
A_USERNAME_IS_LENT_AND_THE_NUMERIC_ID_IS_NOT: Final = (
    "a Telegram username can be changed by its holder and re-registered by somebody else "
    "once released, so a binding made against one hands itself to whoever claims the handle "
    "next; the numeric id is issued by Telegram and does not move"
)

#: Why nothing the gate computed goes into a chat with more than one reader.
AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS: Final = (
    "a group's membership cannot be enumerated over the Bot API, so channels.room.floor has "
    "no members to intersect and there is no reach the room could be answered at; Telegram "
    "has no per-viewer message in a group either, so there is nowhere private to put one"
)

#: What privacy mode means for anything built on top of this adapter.
A_BOT_IN_A_GROUP_SEES_A_FRACTION_OF_IT: Final = (
    "with privacy mode on a bot receives commands, replies to its own messages and mentions "
    "of itself, and not the rest of the conversation; a message that never arrived and a "
    "message that was never sent look identical from here, so neither may be reasoned from"
)

#: Why a press is refused instead of read.
A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL: Final = (
    "this channel carries read alone, so no press could be honoured as an approval; and "
    "this adapter sends no inline keyboard, so a callback query addressed to it is a press "
    "on a button this system did not send rather than an input to be handled"
)

#: Why a genuine vendor capability is still not declared.
EDITING_A_MESSAGE_HERE_HAS_NOTHING_TO_CLOSE: Final = (
    "Telegram does support editing a sent message; the feature exists in this codebase to "
    "disarm a card once the decision it offers has been taken, and this surface has no "
    "cards, so declaring it would tell a caller about a path that is not here"
)

#: Why the chat id has to agree with the plan before anything reaches the wire.
A_PLAN_IS_BOUND_TO_ONE_CHAT: Final = (
    "a plan names its destination by salted digest and the chat id is checked against it; "
    "without that the chat id is a second argument, and the mistake that puts one person's "
    "answer into a group is a variable name"
)

# --------------------------------------------------------- authenticating the delivery

#: The header Telegram sets on every delivery, to the value given when the webhook was
#: registered. Named here so a caller reads one spelling of it rather than typing its own.
AUTHENTICATING_HEADER: Final = "X-Telegram-Bot-Api-Secret-Token"

#: The shortest configured secret this module will accept. Telegram permits one character,
#: and the endpoint it protects is a public HTTPS URL with no rate limit in front of it, so
#: the vendor's floor is not a floor. 32 characters of the alphabet Telegram allows is
#: comfortably past guessing.
MINIMUM_SECRET_LENGTH: Final = 32

#: The one sentence every per-request refusal in `assert_from_telegram` gives. It says
#: neither which check failed nor how close the presentation was, for the reason
#: `channels.webhook.WebhookRefusedError` gives: telling somebody probing which part to fix
#: next is how they fix it.
UPDATE_NOT_ACCEPTED: Final = "this update was not accepted"

#: Not data. The constructor guard, in the shape `gate.catalogue.ProjectedCatalogue` uses.
_VERIFIED_TOKEN: Final = object()


class TelegramRefusedError(Exception):
    """Raised when an update must not be read, or an answer must not be sent.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself: a
    forged delivery and an answer planned for the wrong chat are wiring faults or attacks
    rather than outcomes of somebody's question, and degrading either into an answer hides
    them behind a shrug.
    """


def _mapping(node: object, what: str) -> Mapping[str, Any]:
    """One JSON object out of the parsed body, or a refusal.

    Defined up here rather than beside the other readers because `verified_update` is the
    first thing that needs it: the type it returns promises a mapping, so the check that it
    is one belongs on the way in rather than at each later `.get`.
    """
    if not isinstance(node, Mapping):
        msg = f"{what} is {type(node).__name__} and not an object; this is not a Telegram update"
        raise TelegramRefusedError(msg)
    return node


@dataclass(frozen=True)
class VerifiedUpdate:
    """One update body whose secret header has been checked, and nothing else.

    **This type cannot be constructed outside `verified_update`**, and that is the whole
    point of it. The alternative is a `verify` a caller has to remember to call before
    parsing, which is a check that goes missing from the one call site somebody adds later,
    and the missing one is reachable by anybody who knows the URL. `channels.webhook`
    argues the same about `verified_handler` and `gate.catalogue.ProjectedCatalogue` is
    where the token pattern comes from.

    It carries the body and deliberately not the header, nor the secret, nor whether the
    presented value was close. Keeping any of those on the object is how one of them ends
    up in a log line describing the update.
    """

    body: Mapping[str, Any]
    #: Not data. See the class docstring.
    token: object = None

    def __post_init__(self) -> None:
        if self.token is not _VERIFIED_TOKEN:
            msg = (
                "an update may only be marked verified by brain.channels.telegram."
                "verified_update; a body constructed as verified elsewhere is a body nobody "
                "checked the secret token on"
            )
            raise TelegramRefusedError(msg)


def assert_from_telegram(*, configured: str, presented: str) -> None:
    """Refuse anything that did not arrive with the registered secret (M10.5.4).

    Raises rather than returning a bool, for the reason `channels.webhook.verify` gives: a
    function returning False can be ignored by writing it on a line of its own, and that
    line reads as a check.

    **The configured secret is checked for length first, and that refusal says what is
    wrong.** It fires on every delivery including the honest ones, so it is not a
    distinguisher anybody can use to learn something about the value; it takes the whole
    channel down loudly, which is what an unconfigured authenticator should do. That is the
    opposite of the per-request refusal below, which differs between requests and so says
    one fixed sentence.

    **Both sides are hashed before the comparison.** `hmac.compare_digest` is constant time
    in the contents and leaks the length of the shorter argument, and the length of a secret
    is a small real fact about it. Hashing first makes the comparison fixed width. It also
    removes a sharp edge: `compare_digest` on two `str` values raises on non-ASCII input,
    and the presented value is whatever an attacker put in a header, so the string form
    would turn a hostile header into an exception rather than a refusal.

    There is deliberately no early exit for an empty or obviously wrong presentation. A fast
    path is a timing distinguisher, which is precisely what the constant-time comparison is
    here to remove.
    """
    if len(configured) < MINIMUM_SECRET_LENGTH:
        msg = (
            f"this deployment's Telegram webhook secret is shorter than "
            f"{MINIMUM_SECRET_LENGTH} characters, so the endpoint is effectively open; "
            "register the webhook with a long random secret and configure the same value here"
        )
        raise TelegramRefusedError(msg)
    if not hmac.compare_digest(
        hashlib.sha256(configured.encode("utf-8")).digest(),
        hashlib.sha256(presented.encode("utf-8")).digest(),
    ):
        raise TelegramRefusedError(UPDATE_NOT_ACCEPTED)


def verified_update(raw: object, *, configured: str, presented: str) -> VerifiedUpdate:
    """The only way to get an update body into the rest of this module (M10.5.4).

    The secret is checked before the body is so much as type-checked. A parser reached
    before the authenticator is a parser an anonymous caller can run, and a parser is code;
    the order here means the only thing an unauthenticated request reaches is a hash
    comparison.
    """
    assert_from_telegram(configured=configured, presented=presented)
    return VerifiedUpdate(body=_mapping(raw, "the update"), token=_VERIFIED_TOKEN)


# --------------------------------------------------------------- who is in the chat


class ChatKind(enum.StrEnum):
    """Telegram's own four words for a conversation. Closed, and checked closed.

    The values are the vendor's, so the wire shape needs no translation table that could
    drift, exactly as `channels.lark.ChatType` argues. The member *names* are ours, and
    `BROADCAST` is deliberately not `CHANNEL`: `ChatKind.CHANNEL` sitting beside
    `gate.context.Channel` in the same file is a confusion waiting for a tired reader.
    """

    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    BROADCAST = "channel"


def audience_is_one_person(chat: ChatKind) -> bool:
    """Whether the only reader is the person who asked.

    The declaration every chat kind has to make, in the shape `gate.context.traffic_class_for`
    makes it. A dictionary with a default would accept a new kind silently, and neither
    default is safe: `True` answers a room at one person's reach, and `False` sends a person
    fixed words instead of their answer.

    A supergroup answers the same as a group and is listed separately rather than folded in,
    because Telegram migrates a group to a supergroup on its own and the two arrive with
    different `type` strings for what a reader would call one chat.
    """
    match chat:
        case ChatKind.PRIVATE:
            return True
        case ChatKind.GROUP | ChatKind.SUPERGROUP | ChatKind.BROADCAST:
            return False
        case _:
            assert_never(chat)


# ------------------------------------------------------------- reading what arrived

#: The one update key this normaliser reads. Everything else is refused rather than mapped
#: onto this one; see `normalise_update`.
MESSAGE_UPDATE: Final = "message"

#: The key a press arrives under. Named so the refusal can be specific about it.
CALLBACK_UPDATE: Final = "callback_query"


def _whole_number(node: object, what: str) -> int:
    """One integer out of the parsed JSON, refusing everything that only looks like one.

    `bool` is excluded explicitly because it is a subclass of `int` in Python, so a JSON
    `true` satisfies `isinstance(value, int)` and would go on to render as the identity
    "True". A float is refused rather than truncated: an id that arrived as `12345.0` is an
    id something reformatted on the way here, and truncating it would silently agree with
    whatever did.
    """
    if isinstance(node, bool) or not isinstance(node, int):
        msg = f"{what} has no whole-number id; a Telegram update identifies everything by one"
        raise TelegramRefusedError(msg)
    return node


def _text(node: Mapping[str, Any], key: str, what: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}; a Telegram update without one cannot be read"
        raise TelegramRefusedError(msg)
    return value


def _received_at(seconds: int) -> datetime:
    """Telegram's `date`, which is whole seconds since the epoch as an integer.

    Converted here rather than passed through, because `ChannelEvent.received_at` is a
    datetime for every channel and a channel handing over a number would make every
    downstream comparison a per-channel special case. WhatsApp sends the same quantity as a
    string and Lark sends milliseconds; the difference living in each normaliser is the
    point of having one internal shape.
    """
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        msg = f"message date {seconds!r} is not a whole-second epoch timestamp"
        raise TelegramRefusedError(msg) from exc


def _chat_kind(value: str) -> ChatKind:
    try:
        return ChatKind(value)
    except ValueError as exc:
        msg = (
            f"chat type {value!r} is not one Telegram documents; guessing would decide "
            "whether this conversation has one reader or a hundred"
        )
        raise TelegramRefusedError(msg) from exc


@dataclass(frozen=True)
class TelegramMessage:
    """One inbound message, normalised, with the two Telegram-shaped facts the gate lacks.

    `event` is the shape every other channel produces, so everything downstream reads one
    type. Beside it sit the chat this arrived in and how many people can read it, which is
    what decides where an answer may go.

    `chat_id` is held raw, as `channels.lark.LarkMessage` holds one and as
    `gate.ingress.ChannelEvent` holds `channel_identity`. The rule this module keeps is
    about the way out: a plan names its destination by digest, so no table of chat ids
    beside the answers they received is ever built. On the way in the raw value is what
    arrived and hashing it would only hide it from the reader of a traceback.
    """

    event: ChannelEvent
    chat_id: int
    chat_kind: ChatKind


def normalise_update(update: VerifiedUpdate) -> TelegramMessage:
    """One verified update as the shape the gate reads, or a refusal (M10.5.4).

    Takes a `VerifiedUpdate` and there is deliberately no overload taking a mapping. The
    secret check is the whole security of this channel and a normaliser that could be
    handed a raw body is one that will be.

    **The identity is `from.id` and this function never looks at `from.username`.** See
    `A_USERNAME_IS_LENT_AND_THE_NUMERIC_ID_IS_NOT`. A handle is not merely attacker-chosen
    text like a WhatsApp profile name, it is attacker-*acquirable*: release one and the next
    registrant inherits every binding made against it.

    `update_id` is the external id rather than `message_id`. Telegram repeats `update_id` on
    a redelivery, which is exactly what `ChannelEvent.external_id` is for, while `message_id`
    is unique only inside one chat, so two chats produce the same one and a dedupe key built
    from it discards a real question as a repeat.

    Only `message` is read. An `edited_message` arrives as its own key carrying a
    `message_id` already answered, so reading it as a new question answers the same message
    twice and reading it as the original re-answers a question whose text changed after the
    answer was computed. A `channel_post` may carry no `from` at all, because a broadcast
    post is attributable to the channel rather than to a person, and there is nobody to
    compute a reach for. A `callback_query` is refused for its own reasons; see
    `A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL`.

    A sender marked `is_bot` is refused, for the reason `channels.email.is_automatic`
    refuses machine-generated mail: two systems answering each other end at a rate limit,
    and the field is documented as always present, so requiring it fails closed.
    """
    body = update.body
    if CALLBACK_UPDATE in body:
        msg = (
            f"this update carries a {CALLBACK_UPDATE!r} and this adapter sends no buttons. "
            f"{A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL}"
        )
        raise TelegramRefusedError(msg)
    if MESSAGE_UPDATE not in body:
        msg = (
            f"this normaliser reads {MESSAGE_UPDATE!r} updates and this one has none; an "
            "edited message re-answers a question that has changed, and a broadcast post "
            "may have no sender to compute a reach for"
        )
        raise TelegramRefusedError(msg)

    update_id = _whole_number(body.get("update_id"), "the update")
    message = _mapping(body[MESSAGE_UPDATE], "the message")
    sender = _mapping(message.get("from"), "the sender")
    if sender.get("is_bot") is not False:
        msg = (
            "this message is from a bot, or does not say it is not; answering software "
            "invites an answer back, and two systems answering each other end at a rate limit"
        )
        raise TelegramRefusedError(msg)
    sender_id = _whole_number(sender.get("id"), "the sender")
    chat = _mapping(message.get("chat"), "the chat")
    chat_id = _whole_number(chat.get("id"), "the chat")
    kind = _chat_kind(_text(chat, "type", "the chat"))

    return TelegramMessage(
        event=ChannelEvent(
            channel=Channel.TELEGRAM,
            external_id=str(update_id),
            channel_identity=str(sender_id),
            text=_text(message, "text", "the message"),
            received_at=_received_at(_whole_number(message.get("date"), "the message")),
        ),
        chat_id=chat_id,
        chat_kind=kind,
    )


# ---------------------------------------------------------------- planning a reply

#: What a group is told instead of an answer. Fixed words, with nothing interpolated into
#: them: no name, no echo of the question and no hint that there was an answer to give.
#:
#: It asks the person to write first because that is the only thing that works. A bot cannot
#: open a private chat with somebody who has not started one, so "I will message you
#: privately" would be an instruction this system cannot carry out.
GROUP_DEFLECTION: Final = (
    "I answer in a direct message rather than in a group chat. Send me the same question "
    "directly and I will pick it up there."
)

#: Everything a `Notice` is allowed to say. An allowlist rather than a free string, because
#: a free string field aimed at a room is one somebody eventually interpolates the asker's
#: name into, and then a value out of the answer.
ALLOWED_NOTICES: Final[frozenset[str]] = frozenset({GROUP_DEFLECTION})

#: The most a Telegram binding is ever worth, whatever the header said. Equal to
#: `Assurance.BOUND` and stated here so the ceiling is visible in this file rather than
#: inferred from nothing raising it. See
#: `THE_HEADER_AUTHENTICATES_TELEGRAM_AND_NOT_THE_PERSON`.
TELEGRAM_ASSURANCE_CEILING: Final = Assurance.BOUND

#: What this surface can do, which is send text to one person. Five absences with five
#: separate reasons, and none of them is "nobody got round to it":
#:
#: `EPHEMERAL`, because Telegram has no per-viewer message in a group. Slack has one and
#: that difference is the reason this is a decision rather than an oversight.
#: `CARDS`, because `gate.admission.CHANNEL_VERBS` gives this channel read alone, so a press
#: could never be honoured; `channels.whatsapp` declines reply buttons on the same ground.
#: `EDIT_IN_PLACE`, which the vendor genuinely supports; see
#: `EDITING_A_MESSAGE_HERE_HAS_NOTHING_TO_CLOSE`.
#: `STREAMING`, because streaming here is one edit per token against a rate limit, which is
#: the argument `channels.lark.LARK_FEATURES` makes about spending a budget on cosmetics.
#: `ATTACHMENTS`, because this adapter has no path for a file in either direction, and a
#: declared capability is read by callers deciding what to do rather than as trivia.
TELEGRAM_FEATURES: Final[frozenset[Feature]] = frozenset()


@dataclass(frozen=True)
class Answer:
    """One reply, carrying what the gate computed, addressed to one person.

    There is no chat field on it. An `Answer` is by construction for a private chat, because
    the only function that builds one refuses a message that arrived anywhere else, and the
    digest it carries is of the *sender's* id. In a private chat Telegram's chat id and the
    user's id are the same number, so `deliver` comparing the digest against the chat id at
    the wire is simultaneously the check that this reaches the right person and the check
    that it does not reach a room: a group's chat id is a different number and cannot match.

    `to_identity` is the salted digest and never the id, for the reason
    `gate.ingress.Binding` stores one: a list of Telegram ids beside the answers they
    received is a directory of the company joined to what each person asked.
    """

    to_identity: str
    payload: ChannelPayload
    body: str

    def __post_init__(self) -> None:
        if not self.to_identity:
            msg = "an answer with no recipient has nowhere to go"
            raise TelegramRefusedError(msg)
        if not self.body:
            msg = "an empty message reads as the system being broken rather than as an answer"
            raise TelegramRefusedError(msg)


@dataclass(frozen=True)
class Notice:
    """Fixed words into a chat that may have any number of readers.

    **There is deliberately no payload field and no free body.** This is the only type in
    this module that can address a group, so it is the only one where a value computed at
    one person's reach could reach people whose reach nobody asked about. A field to put one
    in does not exist, and `body` is checked against `ALLOWED_NOTICES`, so what a room can
    be told is the fixed set of things this module wrote. `channels.whatsapp.SlotSource`
    leaves out a `value` field for the same reason: a constraint carried by the shape of a
    type cannot be gone around by a later branch.

    See `AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS`.
    """

    to_identity: str
    body: str

    def __post_init__(self) -> None:
        if not self.to_identity:
            msg = "a notice with no destination has nowhere to go"
            raise TelegramRefusedError(msg)
        if self.body not in ALLOWED_NOTICES:
            msg = (
                "a notice may only say one of the fixed things this module wrote. "
                f"{AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS}"
            )
            raise TelegramRefusedError(msg)


def _assert_telegram(message: TelegramMessage) -> None:
    if message.event.channel is not Channel.TELEGRAM:
        msg = (
            f"this message arrived over {message.event.channel} and would be answered over "
            f"{Channel.TELEGRAM}; the reply belongs on the surface the question came from"
        )
        raise TelegramRefusedError(msg)


def reply_privately(message: TelegramMessage, payload: ChannelPayload) -> Answer:
    """Plan a reply to one person, in the private chat they asked from (M10.5.4).

    Refuses a message that arrived in a group, a supergroup or a broadcast channel. There is
    no fallback that quietly answers a smaller version of the question, because the smaller
    version would have to be computed at a floor and there is no floor to compute: see
    `AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS`. `group_deflection` is what a
    group gets, and it carries nothing.

    Takes the message rather than an id, so the recipient is the person who asked and cannot
    be a third party a caller passed in. The only way to address somebody else is to answer
    a different message, which is the shape `channels.email.reply_to` has for the same reason.

    The body comes from `cards.render_body`, which every channel shares, so a Telegram
    message and a Lark message cannot disagree about what a payload says or about carrying
    its label.
    """
    _assert_telegram(message)
    if not audience_is_one_person(message.chat_kind):
        msg = (
            f"this message arrived in a {message.chat_kind} chat and the answer was computed "
            f"at one person's reach. {AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS}"
        )
        raise TelegramRefusedError(msg)
    return Answer(
        to_identity=identity_hash(Channel.TELEGRAM, message.event.channel_identity),
        payload=payload,
        body=render_body(payload),
    )


def group_deflection(message: TelegramMessage) -> Notice:
    """What a chat with more than one reader is told (M10.5.4).

    **This function takes the message and nothing else, and the signature is the property.**
    There is no reach, no binding, no entitlement set and no payload to pass, so the words a
    group sees cannot depend on who asked or on whether they are bound to anybody. That is
    the DENIED-and-ABSENT rule applied where it is easiest to break: a group whose bound
    members got a different sentence from its unbound ones would publish each member's
    binding status to everybody else in the room, one question at a time.

    Refuses a private chat. A person who asked privately gets their answer, and a deflection
    built for them would be fixed words in place of one.
    """
    _assert_telegram(message)
    if audience_is_one_person(message.chat_kind):
        msg = (
            "this message arrived in a private chat, which is where an answer belongs; a "
            "deflection here would send fixed words in place of the answer"
        )
        raise TelegramRefusedError(msg)
    return Notice(
        to_identity=identity_hash(Channel.TELEGRAM, str(message.chat_id)),
        body=GROUP_DEFLECTION,
    )


def unrecognised_reply(reach: Unrecognised, message: TelegramMessage) -> Answer:
    """What a sender with no binding is told, in the words `gate.ingress` already wrote.

    **This module defines no prompt of its own**, for the reason `channels.whatsapp` and
    `channels.email` both give: `UNRECOGNISED_PROMPT` answers an unknown id, a known but
    unbound one, and one whose binding was revoked this morning with the same words, and a
    second prompt written here is a second thing to get wrong in the direction that confirms
    an account belongs to somebody.

    **Refused outside a private chat, and that refusal is the interesting one.** The prompt
    is careful not to confirm to the person holding the handset whether their account is
    bound. Posting it into a group announces exactly that to every member, about somebody
    who did nothing but ask a question in front of their colleagues. `group_deflection` is
    what a group gets, and it says the same thing to everybody.
    """
    _assert_telegram(message)
    if reach.channel is not Channel.TELEGRAM:
        msg = (
            f"this reach was built for {reach.channel} and would be sent over "
            f"{Channel.TELEGRAM}; the prompt a person is given is per channel"
        )
        raise TelegramRefusedError(msg)
    if not audience_is_one_person(message.chat_kind):
        msg = (
            f"this message arrived in a {message.chat_kind} chat, and the prompt for an "
            "unbound sender posted there tells every member whether this person is bound"
        )
        raise TelegramRefusedError(msg)
    return Answer(
        to_identity=identity_hash(Channel.TELEGRAM, message.event.channel_identity),
        payload=ChannelPayload(),
        body=reach.prompt,
    )


# ------------------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class SentMessage:
    """One message this adapter delivered. What a test reads instead of a Telegram chat.

    `to_identity` is the digest and not the chat id it was addressed with. The id is needed
    once, to reach the wire, and a list of them kept beside the messages they received is
    the directory `gate.ingress.Binding` declines to be.
    """

    to_identity: str
    body: str


@dataclass
class TelegramAdapter:
    """The Telegram surface, with the transport left out on purpose.

    No HTTP client, no SDK and no bot token. The vendor's `sendMessage` belongs on the other
    side of `sent`, and keeping it there is what makes the case that matters testable: an
    answer reaching a chat it was not computed for is a bug in the planning, and a module
    that opened a socket could only be tested for it against a live bot.

    `reachable` is what `healthy` answers, for the reason `adapter.ChannelAdapter.healthy`
    gives: configured-and-unreachable and never-set-up send a person to different places.
    """

    sent: list[SentMessage] = field(default_factory=list)
    reachable: bool = True

    def capabilities(self) -> ChannelCapabilities:
        """What this surface may carry, declared rather than inferred.

        `INTERNAL` and not `CONFIDENTIAL`, and the argument is WhatsApp's with two additions.
        This is a consumer application on a personal handset, with a screen anybody standing
        nearby can read, and `channels.adapter` names that class of surface as the one a
        `restricted` field must not reach. Beyond that: an ordinary Telegram chat is not
        end-to-end encrypted, so the history sits in the vendor's cloud and is restored in
        full onto any device the account signs in on, which is a materially weaker position
        than a messenger keeping history on the handset. And account recovery runs through a
        code sent to a phone number, so a swapped SIM yields everything already said rather
        than only what is said afterwards.

        `can_carry_label` is true. This surface sends plain text, which has room for the
        label `cards.render_body` puts at the top.
        """
        return ChannelCapabilities(
            channel=Channel.TELEGRAM,
            features=TELEGRAM_FEATURES,
            max_classification=Classification.INTERNAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """One inbound message, as the shape the gate reads.

        Takes a `VerifiedUpdate` and refuses anything else, rather than accepting the
        mapping Telegram posts. A mapping would let a caller hand over a body nobody checked
        the secret token on, which is the forgery this module exists to refuse, and it is
        the same reason `channels.email.EmailAdapter.normalise` insists on an `InboundEmail`
        instead of reading a verdict out of a dict.

        Returns the event alone, which is what the protocol promises. Anything that needs to
        know where the message came from calls `normalise_update` and reads the chat.
        """
        if not isinstance(raw, VerifiedUpdate):
            msg = (
                f"this adapter normalises a VerifiedUpdate and was handed a "
                f"{type(raw).__name__}; the secret token has to have been checked first. "
                f"{THE_HEADER_AUTHENTICATES_TELEGRAM_AND_NOT_THE_PERSON}"
            )
            raise TelegramRefusedError(msg)
        return normalise_update(raw).event

    def send(self, payload: ChannelPayload, *, to: str, body: str = "") -> None:
        """Put one message on the wire (M10.5.4).

        `body` empty means render the payload. Whichever it is, the produced string is
        checked against the payload's label, so a caller cannot hand over a body that
        dropped it.

        `assert_can_send` runs first and is not restated here, so this adapter cannot
        disagree with any other about labels and classifications. Nothing here re-checks the
        audience: that is decided when a plan is built, by which of `Answer` and `Notice` it
        is, and a second enforcement point at the wire is one that the next person to edit
        this deletes whichever of the two they find first.
        """
        assert_can_send(self.capabilities(), payload)
        rendered = body or render_body(payload)
        assert_label_survives(rendered, payload)
        self.sent.append(
            SentMessage(to_identity=identity_hash(Channel.TELEGRAM, to), body=rendered)
        )

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: TelegramAdapter, plan: Answer | Notice, *, to_chat_id: int) -> None:
    """Send one planned message, to the chat it was planned for (M10.5.4).

    The chat id arrives here and nowhere else. A plan holds a digest, so whoever resolved the
    binding supplies the destination at the wire and this checks that the two agree. See
    `A_PLAN_IS_BOUND_TO_ONE_CHAT`.

    **For an `Answer` this one check does two jobs.** The digest is of the sender's user id,
    and in a private chat Telegram's chat id is that same number, so an answer can only be
    delivered to the private chat of the person it was computed for. A group's chat id is a
    different number and cannot match, which means the group-disclosure failure is refused by
    the same comparison that refuses sending one person's answer to another.

    **The second of those jobs rests on a vendor fact and is a bonus rather than the guard.**
    It holds because Telegram's id space gives a private chat the user's own id and gives a
    group, a supergroup and a broadcast channel a negative one, so the two cannot collide. If
    that ever stopped being true, what would still stand is the rule that actually carries
    this: `reply_privately` refuses to build an `Answer` from a message that arrived anywhere
    but a private chat, and `Notice` is the only plan addressed to a room and has no payload
    field. Both hold whatever the numbers look like, and neither is checked here.

    The mapping from a plan to a send lives here rather than on the adapter, so `send` keeps
    the signature `redaction.assert_channel_adapter` can check: a parameter typed `Answer`
    names no `ChannelPayload`, and an adapter taking one could not be shown safe by reading
    its signature.
    """
    if identity_hash(Channel.TELEGRAM, str(to_chat_id)) != plan.to_identity:
        # Names neither the chat id nor the digest it was expected to match. Both reach a log
        # from here, and the pair of them is the directory this module declines to keep.
        msg = f"this message was planned for somewhere else. {A_PLAN_IS_BOUND_TO_ONE_CHAT}"
        raise TelegramRefusedError(msg)
    match plan:
        case Answer():
            adapter.send(plan.payload, to=str(to_chat_id), body=plan.body)
        case Notice():
            # An empty payload rather than the plan's, because a `Notice` has none. Passed
            # explicitly so the adapter's label check runs over the fixed words too.
            adapter.send(ChannelPayload(), to=str(to_chat_id), body=plan.body)
        case _:
            assert_never(plan)
