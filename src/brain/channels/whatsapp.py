"""WhatsApp: a business rule with a security edge, and an approval that is not one.

WhatsApp is the only surface here where the platform, rather than this system, decides what
may be said and when. The vendor's rule is that free text may be sent only inside a
twenty-four hour window, and outside it only a template somebody approved in advance. That
reads like a delivery constraint and it is a permission problem wearing a delivery
constraint's clothes.

**The window opens when the person messages us, and never when we message them
(M10.5.3).** The direction is the entire rule. A window that an outbound message renewed
would renew itself for ever, so the first message would buy an unbounded licence to keep
sending, and the constraint would permit exactly the thing it exists to refuse. So
`SessionWindows` is opened by handing it an inbound `gate.ingress.ChannelEvent` and there is
deliberately no method that takes anything else: not a `note(direction=...)` with an
argument somebody can pass the wrong way round, and nothing on the send path that touches
the window at all. See `ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW`.

**An approved template is approved text with holes in it, and nobody approved the holes.**
The vendor reviews the fixed words. The substitution is filled at send time by this system,
so a slot filled with a value out of somebody's data is a disclosure that arrives wearing
the approval of text that discloses nothing. Two constraints together, because neither is
enough alone:

*The payload must have been computed at the recipient's own reach.* Checked by comparing the
caller's stated `ent_hash` against `EntitlementSet.ent_hash`, which is the comparison
`channels.cards.build_approval_card` makes for the same reason and
`gate.context.GateContext` makes about its own pair. Without it a template sent to a manager
can carry the junior asker's answer.

*And a slot is filled by reference into that payload, never by a string a caller hands
over.* `SlotSource` names a record and a field and has nowhere to put a value, so a caller
holding a figure it fetched itself cannot get it into a slot. With only the first check, a
correctly addressed payload sits beside a slot filled from anywhere at all; with only the
second, the values are real payload values computed for the wrong person. See
`AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION`.

**The channel ceiling stays at `read`, and that is a finding rather than a gap to close.**
`gate.admission.CHANNEL_VERBS` gives WhatsApp `read` alone because a message is not a
signature, and this module does not widen it. What follows is worth stating out loud:
WhatsApp templates support reply buttons, and a template asking somebody to approve
something would produce a press this system may not honour, because `approve` is not a verb
this channel carries. `Feature.CARDS` is therefore not declared, so
`channels.cards.build_approval_card` refuses a WhatsApp card rather than building one that
degrades on the press. An approval belongs on a surface behind the identity provider.

**The number addresses the wire once and is never held.** `Send` names its recipient by
`gate.ingress.identity_hash` and `deliver` refuses to put a plan on a number whose digest
does not match, so a plan built for one person cannot be delivered to another. What the
adapter records is the digest, for the reason `gate.ingress.Binding` stores one: a table of
these is a phone book of the company joined to their roles.

Nothing here opens a connection, imports an SDK or holds a credential. The event shape is
data: `normalise_messages` reads the mapping the Cloud API posts and refuses what it cannot
read. A module that owned an HTTP client could not be tested for the case that matters,
which is a template filled from the wrong person's answer.

Task ids: M10.5.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.channels.cards import assert_label_survives, render_body
from brain.core.entitlement import EntitlementSet
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

# ------------------------------------------------------------------ written-down reasons

#: Why only a message from the person moves the window.
#:
#: The vendor's rule bounds how long we may keep talking freely after somebody talks to us.
#: If our own message counted, sending one would extend the permission to send another, and
#: the bound would be "for ever" for anybody we messaged once. The rule is only a rule
#: because the clock is reset by the other party.
ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW: Final = (
    "the window runs from the last message the person sent us and never from one we sent "
    "them; a window an outbound message renewed would renew itself for ever, and the rule "
    "would permit precisely the unbounded sending it exists to refuse"
)

#: Why an approved template is not evidence about what a template carries.
AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION: Final = (
    "the vendor approved the fixed words; nobody approved the values. A slot is filled by "
    "reference out of the payload the gate computed at the recipient's own reach, so a "
    "caller has no way to put a value in that the recipient could not have been shown"
)

#: Why this surface declares no cards even though the vendor offers reply buttons.
A_MESSAGE_IS_NOT_A_SIGNATURE: Final = (
    "gate.admission.CHANNEL_VERBS gives WhatsApp read alone, so a button press here could "
    "never be honoured as an approval; declaring cards would build one that fails at the "
    "press instead of refusing at the build"
)

#: Why a plan cannot be delivered to a number it was not planned for.
A_PLAN_IS_BOUND_TO_ONE_RECIPIENT: Final = (
    "a send names its recipient by salted digest and the wire address is checked against "
    "it; without that the number is a separate argument, and the mistake that sends one "
    "person's answer to another is a variable name"
)

# ------------------------------------------------------------------------------- the rule

#: How long free text may be sent after the person last wrote to us. The vendor's number.
SESSION_WINDOW: Final = timedelta(hours=24)

#: What this surface can do. `CARDS` is absent on purpose; see `A_MESSAGE_IS_NOT_A_SIGNATURE`.
#: `EPHEMERAL` is absent because there is no room to be ephemeral in: a WhatsApp thread has
#: one reader, and a per-viewer body is a mechanism for a shared room.
WHATSAPP_FEATURES: Final[frozenset[Feature]] = frozenset({Feature.ATTACHMENTS})

#: The one inbound message type this normaliser reads. An image, a location, a sticker or a
#: delivery receipt is a different event with a different shape, and reading one as text
#: would put a blank question through the gate.
TEXT_MESSAGE: Final = "text"

#: A slot in a template body. The vendor writes positional slots as `{{1}}`; named slots use
#: the same braces. Both are one grammar here, because what matters downstream is that a
#: slot has a key `sources` can be looked up by.
SLOT_RE: Final = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

#: Characters a substituted value may not contain. A newline in a slot appends lines the
#: approver never read to text that was approved, which is the whole shape of the risk: the
#: reader trusts the message because the top of it is the approved wording.
FORBIDDEN_IN_A_SLOT: Final = ("\n", "\r", "\t")


class WhatsAppRefusedError(Exception):
    """Raised when a WhatsApp event cannot be read, or a message must not be sent.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself: a
    malformed webhook and a template filled from the wrong person's answer are wiring faults
    rather than outcomes of somebody's question, and degrading them into an answer hides a
    bug behind a shrug.
    """


# ------------------------------------------------------------------- reading what arrived


def _mapping(node: object, what: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        msg = f"{what} is {type(node).__name__} and not an object; this is not a WhatsApp event"
        raise WhatsAppRefusedError(msg)
    return node


def _sequence(node: object, what: str) -> Sequence[Any]:
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        msg = f"{what} is not a list; a WhatsApp webhook batches its entries in one"
        raise WhatsAppRefusedError(msg)
    return node


def _text(node: Mapping[str, Any], key: str, what: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}; a WhatsApp event without one cannot be read"
        raise WhatsAppRefusedError(msg)
    return value


def _received_at(message: Mapping[str, Any]) -> datetime:
    """The vendor's `timestamp`, which is whole seconds since the epoch as a string.

    Converted here rather than passed through, because `ChannelEvent.received_at` is a
    datetime for every channel and a channel handing over a string would make every
    downstream comparison a per-channel special case. Lark's is milliseconds; the difference
    living in each normaliser is the point of having one internal shape.
    """
    raw = _text(message, "timestamp", "the message")
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        msg = f"message timestamp {raw!r} is not a whole-second epoch timestamp"
        raise WhatsAppRefusedError(msg) from exc


def _one_message(raw: object) -> ChannelEvent:
    """One entry from the `messages` array, as the shape every channel produces.

    **The sender's profile name is deliberately not read.** The webhook carries it in
    `contacts[].profile.name`, and it is set by the sender on their own handset, so it is
    attacker-influenced in exactly the way `channels.lark.Mention` refuses a display name
    for: somebody can name themselves after a colleague. The `from` field is the vendor's
    own identifier for the account, and it is the only thing here worth keying on.
    """
    message = _mapping(raw, "the message")
    message_type = _text(message, "type", "the message")
    if message_type != TEXT_MESSAGE:
        msg = (
            f"this normaliser reads {TEXT_MESSAGE!r} messages and this one is "
            f"{message_type!r}; a different shape read as text is a blank question"
        )
        raise WhatsAppRefusedError(msg)
    body = _mapping(message.get("text"), "the message text")
    return ChannelEvent(
        channel=Channel.WHATSAPP,
        external_id=_text(message, "id", "the message"),
        channel_identity=_text(message, "from", "the message"),
        text=_text(body, "body", "the message text"),
        received_at=_received_at(message),
    )


def normalise_messages(raw: object) -> tuple[ChannelEvent, ...]:
    """Every inbound message in one webhook delivery (M10.5.3).

    A batch, because the Cloud API posts one. Returning a tuple rather than the first
    message is the whole reason this function exists: a normaliser that read `messages[0]`
    would answer one person and silently drop everybody else in the same delivery, and the
    symptom is a question that was never answered with nothing anywhere saying so.

    A change carrying `statuses` rather than `messages` is a delivery receipt and
    contributes nothing here. Skipped rather than refused: a receipt is an ordinary thing to
    receive, and refusing the batch it arrives in would refuse the messages beside it.

    **A message this normaliser cannot read is skipped for exactly the same reason, and it
    used to refuse.** `_one_message` raises on anything that is not text, and calling it
    across the batch meant one image refused the whole delivery. The Cloud API batches
    messages from *different senders* into one POST, so a sticker from one person threw away
    a question from another, and the person who asked it got silence with nothing anywhere
    saying why. The module already made this argument about `statuses` and then did the
    opposite one loop down; found by writing the test for it.

    `_one_message` stays strict, because the single-message path wants a refusal: a caller
    asking for one event and handing over an image should be told. What changed is only that
    the batch does not let one unreadable message speak for the rest of it.

    An unanswered image is still an unanswered image. That is a real gap and it belongs to
    the reply path rather than the normaliser: telling somebody "I cannot read pictures"
    requires knowing who they are, which is `gate.ingress`'s decision and not this
    function's.
    """
    envelope = _mapping(raw, "the event")
    events: list[ChannelEvent] = []
    for entry in _sequence(envelope.get("entry"), "the event entry list"):
        node = _mapping(entry, "an entry")
        for change in _sequence(node.get("changes"), "an entry's changes"):
            value = _mapping(_mapping(change, "a change").get("value"), "a change value")
            messages = value.get("messages")
            if messages is None:
                continue
            for item in _sequence(messages, "a change's messages"):
                message = _mapping(item, "the message")
                if _text(message, "type", "the message") != TEXT_MESSAGE:
                    continue
                events.append(_one_message(message))
    return tuple(events)


# ------------------------------------------------------------------- the window (M10.5.3)


@dataclass
class SessionWindows:
    """When each person last wrote to us, keyed by salted digest.

    In memory, and that is a limitation stated rather than hidden, exactly as
    `channels.webhook.SeenNonces` states its own: two replicas each keep their own view, so
    a window one replica has seen open is closed on the other and the message degrades to a
    template. That direction is the safe one, which is why this is a seam rather than a bug.
    The store is a `dict[str, datetime]` precisely so the shared version is a swap.

    **There is deliberately no method that takes an outbound message.** The rule is a
    direction, and a `note(event, direction=...)` would make the direction an argument
    somebody can pass the wrong way round. See `ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW`.
    """

    _opened: dict[str, datetime] = field(default_factory=dict)

    def note_inbound(self, event: ChannelEvent) -> None:
        """Open or renew this person's window from a message they sent (M10.5.3).

        Refuses an event from another channel. The digest is salted per channel by
        `gate.ingress.identity_hash`, so a Lark event could not silently open a WhatsApp
        window even without this; what it would do instead is write a key nothing ever reads,
        leaving a channel that can only send templates and no error anywhere saying why. A
        registry wired to the wrong stream is a fault, and it says so.

        The later of the two timestamps wins. A redelivery of an older message must not pull
        the window backwards: the vendor redelivers on its own schedule, and a window that
        shrank when it did would refuse free text somebody had earned.
        """
        if event.channel is not Channel.WHATSAPP:
            msg = (
                f"a {event.channel} message cannot open a {Channel.WHATSAPP} window; this "
                "registry has been handed the wrong stream, and the window it would write "
                "is one nothing reads"
            )
            raise WhatsAppRefusedError(msg)
        if event.received_at.tzinfo is None:
            msg = "a naive timestamp cannot be compared with the clock; the window would raise"
            raise WhatsAppRefusedError(msg)
        key = identity_hash(event.channel, event.channel_identity)
        seen = self._opened.get(key)
        self._opened[key] = event.received_at if seen is None else max(seen, event.received_at)

    def opened_at(self, identity: str) -> datetime | None:
        """When this person last wrote to us, or None. Read-only; nothing here opens one."""
        return self._opened.get(identity)

    def is_open(self, identity: str, now: datetime) -> bool:
        """Whether free text may be sent to this person right now (M10.5.3).

        Never true for somebody who has not written to us. A person we have no record of is
        not "open by default": the whole permission being modelled is one they granted by
        writing, and a default of open is a default of never having asked.
        """
        opened = self._opened.get(identity)
        if opened is None:
            return False
        return now - opened < SESSION_WINDOW


# ----------------------------------------------------------------- templates (M10.5.3)


@dataclass(frozen=True)
class SlotSource:
    """Where one slot's value comes from: one record in the payload and one field on it.

    **There is deliberately no `value` field.** A caller holding a figure it fetched itself
    has nowhere to put it, which is the half of
    `AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION` that a hash comparison cannot
    provide: the hash says the payload was built for the right person and says nothing at
    all about a string beside it.
    """

    record: int
    field: str


@dataclass(frozen=True)
class Template:
    """One template the vendor has approved, with its slots left in it.

    `body` is the approved wording. It is held here rather than fetched at send time because
    the check that matters is against the words that were approved, and a body read from the
    provider at send time is a body that changed after the review.
    """

    name: str
    language: str
    body: str

    def __post_init__(self) -> None:
        if not self.name or not self.language or not self.body:
            msg = "a template needs a name, a language and the approved wording"
            raise WhatsAppRefusedError(msg)

    @property
    def slots(self) -> tuple[str, ...]:
        """The slot keys in the approved wording, in order of first appearance.

        Deduplicated, because a template may use the same slot twice and a caller should
        supply one source for it rather than two that can disagree.
        """
        seen: list[str] = []
        for match in SLOT_RE.finditer(self.body):
            key = match.group(1)
            if key not in seen:
                seen.append(key)
        return tuple(seen)


def _slot_value(payload: ChannelPayload, source: SlotSource) -> str:
    """One value, out of the payload and nowhere else.

    Every refusal here is the same refusal wearing a different failure: the value the caller
    is pointing at is not in what the gate decided this recipient may see. A field the
    redactor locked is absent from the record, so a slot naming it lands on the missing-field
    branch rather than needing a rule of its own.
    """
    if not 0 <= source.record < len(payload.records):
        msg = (
            f"slot source names record {source.record} and this payload has "
            f"{len(payload.records)}. {AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION}"
        )
        raise WhatsAppRefusedError(msg)
    record = payload.records[source.record]
    if source.field not in record:
        # Names the field and never a value from the record. A refusal quoting one would
        # write the thing being withheld into whatever log records the refusal.
        msg = (
            f"slot source names {source.field!r}, which is not in the payload the gate built "
            f"for this recipient. {AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION}"
        )
        raise WhatsAppRefusedError(msg)
    value = record[source.field]
    if not isinstance(value, str | int | float | bool):
        # A nested object rendered through `str` flattens a whole subtree into one line of an
        # approved message, and every field of it arrives without anybody having named it.
        msg = (
            f"{source.field!r} holds a {type(value).__name__}, which would flatten a whole "
            "subtree into one slot; a slot carries one scalar"
        )
        raise WhatsAppRefusedError(msg)
    rendered = str(value)
    if any(bad in rendered for bad in FORBIDDEN_IN_A_SLOT):
        msg = (
            f"{source.field!r} contains a line break, which would append lines nobody "
            "approved to text somebody did; the reader trusts the message because the top "
            "of it is the approved wording"
        )
        raise WhatsAppRefusedError(msg)
    return rendered


def fill(
    template: Template,
    *,
    payload: ChannelPayload,
    body_ent_hash: str,
    recipient: EntitlementSet,
    sources: Mapping[str, SlotSource],
) -> str:
    """The approved wording with its slots filled, or a refusal (M10.5.3).

    Two independent constraints, in this order, and the order is the argument. The reach
    first, because it decides whether the payload is the right one at all; then the sources,
    which are only meaningful against a payload that is. Checking the sources first would
    validate references into somebody else's answer and then discover it was somebody
    else's.

    A slot with no source is refused rather than left standing. An unfilled `{{1}}` reaching
    the provider is not a rendering blemish: the provider substitutes what it is handed, so
    a slot this function did not check is a slot filled by whatever the caller passes on the
    wire, which is the entire thing being prevented.

    A source for a slot the template does not have is refused too. A caller that believes it
    has filled a slot which does not exist is a caller whose values are going somewhere this
    function is not looking.

    `assert_label_survives` is reused rather than restated, so a template and a plain message
    cannot disagree about labels. Its effect here is worth spelling out: a payload carrying
    `redaction.OPAQUE_LABEL` cannot go out as a template at all, because an approved body has
    nowhere to put a warning nobody approved. That is the correct outcome and it is why the
    check is on the produced string rather than on the surface.
    """
    if body_ent_hash != recipient.ent_hash():
        msg = (
            f"this body was computed at {body_ent_hash!r} and the recipient's reach is "
            f"{recipient.ent_hash()!r}. {AN_APPROVED_TEMPLATE_SAYS_NOTHING_ABOUT_THE_SUBSTITUTION}"
        )
        raise WhatsAppRefusedError(msg)

    slots = template.slots
    missing = tuple(key for key in slots if key not in sources)
    if missing:
        msg = (
            f"template {template.name!r} has slots {missing} with no source; the provider "
            "substitutes whatever it is handed, so a slot nothing here checked is a slot "
            "filled from outside this function"
        )
        raise WhatsAppRefusedError(msg)
    unknown = tuple(sorted(key for key in sources if key not in slots))
    if unknown:
        msg = (
            f"sources were given for {unknown}, which template {template.name!r} does not "
            "have; a caller filling a slot that does not exist is one whose values go "
            "somewhere this function is not looking"
        )
        raise WhatsAppRefusedError(msg)

    values = {key: _slot_value(payload, sources[key]) for key in slots}
    body = SLOT_RE.sub(lambda match: values[match.group(1)], template.body)
    assert_label_survives(body, payload)
    return body


# ------------------------------------------------------------------- planning a send


class SendKind(enum.StrEnum):
    """Which of the vendor's two kinds of message this is. Closed, because they differ in
    the one thing this module exists to decide."""

    #: Anything we wrote. Permitted only inside the window.
    FREE_TEXT = "free_text"
    #: Approved wording with checked substitutions. Permitted whenever.
    TEMPLATE = "template"


@dataclass(frozen=True)
class Send:
    """One outbound message, to one person, of one kind.

    The recipient is a digest and not a number, for the reason `channels.lark.Delivery` names
    a viewer by one: a number on a plan is a number one interpolation away from a message
    body and one copy away from a table of them. `deliver` is where the number appears, once.
    """

    to_identity: str
    kind: SendKind
    payload: ChannelPayload
    body: str
    #: The approved template this was built from. Empty for free text.
    template_name: str = ""

    def __post_init__(self) -> None:
        if not self.to_identity:
            msg = "a send with no recipient has nowhere to go"
            raise WhatsAppRefusedError(msg)
        if not self.body:
            msg = "an empty message reads as the system being broken rather than as an answer"
            raise WhatsAppRefusedError(msg)
        if (self.kind is SendKind.TEMPLATE) != bool(self.template_name):
            msg = (
                "a template send names its template and a free-text send names none; the two "
                "disagree here, and the resolution that reads as harmless is the free-text one"
            )
            raise WhatsAppRefusedError(msg)


def free_text(
    *,
    to_identity: str,
    payload: ChannelPayload,
    now: datetime,
    sessions: SessionWindows,
) -> Send:
    """Anything we wrote, which needs an open window (M10.5.3).

    Takes the window registry because the answer depends on it. `templated` deliberately does
    not take one at all, and that asymmetry is the vendor's rule expressed in two signatures
    rather than in a branch: there is no argument to pass `templated` that would make it
    consult a window, and none to pass this that would make it skip one.

    Nothing here renews the window. The person's own message is the only thing that does.
    """
    if not sessions.is_open(to_identity, now):
        msg = (
            "this session window is closed, so only an approved template may be sent. "
            f"{ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW}"
        )
        raise WhatsAppRefusedError(msg)
    return Send(
        to_identity=to_identity,
        kind=SendKind.FREE_TEXT,
        payload=payload,
        body=render_body(payload),
    )


def templated(
    template: Template,
    *,
    to_identity: str,
    payload: ChannelPayload,
    body_ent_hash: str,
    recipient: EntitlementSet,
    sources: Mapping[str, SlotSource],
) -> Send:
    """Approved wording, filled from the recipient's own payload (M10.5.3).

    Deliberately takes no `SessionWindows` and no clock. A template is what the vendor
    permits outside the window, and it is permitted inside one too, so consulting a window
    here would be a check with no outcome that a later reader would tighten into one.
    """
    body = fill(
        template,
        payload=payload,
        body_ent_hash=body_ent_hash,
        recipient=recipient,
        sources=sources,
    )
    return Send(
        to_identity=to_identity,
        kind=SendKind.TEMPLATE,
        payload=payload,
        body=body,
        template_name=template.name,
    )


def unrecognised_reply(
    reach: Unrecognised,
    *,
    to_identity: str,
    now: datetime,
    sessions: SessionWindows,
) -> Send:
    """What a sender with no binding is told, in the words `gate.ingress` already wrote.

    **This module defines no prompt of its own.** `gate.ingress.UNRECOGNISED_PROMPT` answers
    an unknown number, a known but unbound one, and one whose binding was revoked this
    morning with the same words, and a second prompt written here would be a second thing to
    get wrong in the direction that confirms a number belongs to somebody. `channels.widget`
    does pass its own, and its docstring argues at length why the fact it conceals is
    published by the customer's own website; nothing of that argument transfers to a phone
    number.

    It goes out as free text, which is only possible because their own message opened the
    window. That is worth noticing rather than assuming: a message sat in a queue for more
    than `SESSION_WINDOW` cannot be answered even with this, and the honest response then is
    silence rather than a template, because there is no approved wording that says this and
    inventing one would be inventing a prompt.
    """
    if reach.channel is not Channel.WHATSAPP:
        msg = (
            f"this reach was built for {reach.channel} and would be sent over "
            f"{Channel.WHATSAPP}; the prompt a person is given is per channel"
        )
        raise WhatsAppRefusedError(msg)
    if not sessions.is_open(to_identity, now):
        msg = (
            "this session window is closed, so the prompt cannot be sent as free text and "
            "there is no approved wording that says it. "
            f"{ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW}"
        )
        raise WhatsAppRefusedError(msg)
    return Send(
        to_identity=to_identity,
        kind=SendKind.FREE_TEXT,
        payload=ChannelPayload(),
        body=reach.prompt,
    )


# ------------------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class SentMessage:
    """One message this adapter delivered. What a test reads instead of a WhatsApp number.

    `to_identity` is the digest and not the number it was addressed with. The number is
    needed once, to reach the wire, and a list of them kept beside the messages they
    received is the phone book `gate.ingress.Binding` refuses to be.
    """

    to_identity: str
    body: str


@dataclass
class WhatsAppAdapter:
    """The WhatsApp surface, with the transport left out on purpose.

    No client, no credentials, no SDK. The vendor's HTTP calls belong on the other side of
    `sent`, and keeping them there is what makes the case that matters testable: a template
    filled from the wrong person's payload is a bug in the filling, and a module that opened
    a socket could only be tested for it against a live business account.

    `reachable` is what `healthy` answers, for the reason `adapter.ChannelAdapter.healthy`
    gives: configured-and-unreachable and never-set-up send a person to different places.
    """

    sent: list[SentMessage] = field(default_factory=list)
    reachable: bool = True

    def capabilities(self) -> ChannelCapabilities:
        """What this surface may carry, declared rather than inferred.

        `INTERNAL` and not `CONFIDENTIAL`. Lark carries more because it is behind the tenant
        and the identity provider; WhatsApp is a consumer application on a personal handset,
        with backups, a second device and a screen anybody standing nearby can read, and
        `channels.adapter` names it as the example of a surface a `restricted` field must not
        reach. Raising this is a decision somebody makes deliberately.

        `can_carry_label` is true, because free text can carry one. A template cannot, and
        that is enforced where it belongs, on the produced string, by `fill`.
        """
        return ChannelCapabilities(
            channel=Channel.WHATSAPP,
            features=WHATSAPP_FEATURES,
            max_classification=Classification.INTERNAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """One inbound message, as the shape the gate reads.

        Refuses a batch carrying anything other than exactly one message. The protocol
        returns a single event and there is no honest way to answer it for a batch of three:
        picking the first drops two questions silently. `normalise_messages` is the function
        for a batch, and this one says so rather than guessing.
        """
        events = normalise_messages(raw)
        if len(events) != 1:
            msg = (
                f"this delivery carries {len(events)} messages and this method returns one; "
                "picking the first would drop the rest with nothing anywhere saying so. Use "
                "normalise_messages"
            )
            raise WhatsAppRefusedError(msg)
        return events[0]

    def send(self, payload: ChannelPayload, *, to: str, body: str = "") -> None:
        """Put one message on the wire (M10.5.3).

        `body` empty means render the payload; a template supplies its own filled wording.
        Whichever it is, the produced string is checked against the payload's label, so a
        caller cannot hand over a body that dropped it.

        `assert_can_send` runs first and is not restated here, so this adapter cannot
        disagree with any other about labels and classifications. Nothing here consults the
        session window: the window decides what may be *planned*, and a second check at the
        wire would be a second enforcement point that the next person to edit this deletes
        whichever of the two they find first.
        """
        assert_can_send(self.capabilities(), payload)
        rendered = body or render_body(payload)
        assert_label_survives(rendered, payload)
        self.sent.append(
            SentMessage(to_identity=identity_hash(Channel.WHATSAPP, to), body=rendered)
        )

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: WhatsAppAdapter, send: Send, *, to_number: str) -> None:
    """Send one planned message, to the number it was planned for (M10.5.3).

    The number arrives here and nowhere else. `Send` holds a digest, so whoever resolved the
    binding supplies the address at the wire, and this checks that the two agree. See
    `A_PLAN_IS_BOUND_TO_ONE_RECIPIENT`: without the check the number is simply a second
    argument, and handing this function the wrong one is a mistake that looks like a
    variable name and sends one person's answer to another.

    The mapping from a plan to a send is here rather than on the adapter, so `send` keeps the
    signature `redaction.assert_channel_adapter` can check: a parameter typed `Send` names no
    `ChannelPayload`, and an adapter taking one could not be shown safe by reading it.
    """
    if identity_hash(Channel.WHATSAPP, to_number) != send.to_identity:
        # Names neither the number nor the digest it was expected to match. Both reach a log
        # from here, and the pair of them is the phone book this module declines to keep.
        msg = f"this send was planned for somebody else. {A_PLAN_IS_BOUND_TO_ONE_RECIPIENT}"
        raise WhatsAppRefusedError(msg)
    adapter.send(send.payload, to=to_number, body=send.body)
