"""Email, where the sender writes their own name on the envelope.

Every other channel here is handed an identity by something that checked it. Lark sits
behind the tenant and its identity provider, WhatsApp's `from` is the vendor's own account
identifier, the console has a session. Email has none of that: `From:` is a header the
sender composes, and anybody can compose one that says anything.

**So the authentication verdict is a parameter and is never read out of the message.**
This is the whole shape of the module and it is worth being blunt about, because the
convenient version is a two-line function that greps `Authentication-Results` out of the
headers and believes it. That header is inside the message. A sender who writes their own
`From` writes their own `Authentication-Results` in the same breath, and the only copy worth
anything is the one the *receiving* infrastructure prepended after checking, which cannot be
told apart from the forgeries by looking at the text. `normalise` therefore takes an
`Authentication` the caller obtained from the MTA, and there is deliberately no code path in
this file that reads that header. See `THE_VERDICT_COMES_FROM_THE_MTA_AND_NEVER_FROM_THE_MESSAGE`.

**An unauthenticated message has no sender, and that is not the same as an unknown sender.**
`gate.ingress` already draws the line between an identity nobody has bound and one that does
not exist; this draws an earlier one. A message that failed DMARC is not a message from
somebody we cannot place, it is a message from nobody, and deriving a `channel_identity` from
it would let anybody address the system as anybody. `identity_hash` is salted per channel, so
a forged binding here could not reach a WhatsApp window, but it would reach this person's own
email reach, which is the entire point of forging it.

**DMARC is about the domain and never about the person.** A pass proves the message came
from something authorised to send for that domain. On a shared tenant that is every other
person on it, and on a mailing list it is the list. What it buys is that the domain is real
and the address was not invented; what it does not buy is that this human sent it. That is
why `Channel.EMAIL` carries `read` and nothing else in `gate.admission.CHANNEL_VERBS`, and
why nothing here raises the assurance a binding is worth.

**A reply goes to one person and never to the thread.** The other participants on a thread
have their own reach and none of it was consulted; an answer computed for the sender and
delivered to `Cc` is a disclosure to everybody who happened to be on a mail somebody wrote.
`Reply` has one recipient field and no `cc` at all, so reply-all is not something a caller
can ask for by mistake. See `A_REPLY_IS_ADDRESSED_TO_THE_PERSON_THE_ANSWER_WAS_COMPUTED_FOR`.

**The subject carries no answer.** A subject line is the one part of a message that appears
in a lock-screen notification, in a mail server's logs, in a backup index and in the
recipient's mailbox list, all of which outlive the message and none of which the gate
decided. So a reply's subject is the sender's own subject with a prefix, and there is no
argument that lets a caller put anything else there.

**Automatic mail is never answered.** Two systems replying to each other is a loop that ends
in a full mailbox or a rate limit, and it is the classic way an autoresponder takes down a
support address. RFC 3834's `Auto-Submitted` and the older `Precedence: bulk` are both read,
and this module's own replies declare `Auto-Submitted: auto-replied` so that a correct
counterpart does the same for us.

Nothing here opens a socket, imports an SMTP library or holds a credential. The transport
belongs on the other side of `sent`, for the reason `channels.whatsapp` gives: the case worth
testing is a reply addressed to the wrong participant, and a module that connected to a mail
server could only be tested for it against a live mailbox.

Task ids: M10.5.6
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr
from typing import Final

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.channels.cards import assert_label_survives, render_body
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

# ------------------------------------------------------------------ written-down reasons

#: Why the verdict is an argument rather than something parsed out of the headers.
THE_VERDICT_COMES_FROM_THE_MTA_AND_NEVER_FROM_THE_MESSAGE: Final = (
    "Authentication-Results lives inside the message, so a sender who forges From forges it "
    "too, and the honest copy prepended by the receiving MTA is textually identical to the "
    "forged ones above it. The verdict is therefore supplied by whatever ran the check, and "
    "this module has no code that reads that header"
)

#: Why a message that did not authenticate has no sender at all.
AN_UNAUTHENTICATED_MESSAGE_HAS_NO_SENDER: Final = (
    "a message that failed authentication is not from somebody we cannot place, it is from "
    "nobody; deriving an identity from its From header would let anyone address this system "
    "as anyone, and the binding they would reach is that person's own"
)

#: Why a pass is about the domain and stops there.
DMARC_AUTHENTICATES_A_DOMAIN_AND_NOT_A_PERSON: Final = (
    "a pass proves the message came from something authorised to send for that domain, "
    "which on a shared tenant is every colleague and on a mailing list is the list; it is "
    "evidence the address is real and none at all that this human sent it"
)

#: Why there is no Cc and no reply-all.
A_REPLY_IS_ADDRESSED_TO_THE_PERSON_THE_ANSWER_WAS_COMPUTED_FOR: Final = (
    "everybody else on a thread has their own reach and none of it was consulted; an answer "
    "computed for the sender and copied to the thread is a disclosure to whoever happened to "
    "be on a mail somebody else wrote"
)

#: Why the subject is never built from data.
A_SUBJECT_OUTLIVES_THE_MESSAGE: Final = (
    "the subject is the part that appears on a lock screen, in a mail server's logs, in a "
    "backup index and in a mailbox list, none of which the gate decided and all of which "
    "outlive the message the redaction was applied to"
)

# ------------------------------------------------------------------------------- the rule


class Authentication(enum.StrEnum):
    """What the receiving infrastructure concluded about this message's origin.

    Three values, and the middle one is why this is not a boolean. `NOT_CHECKED` is a
    deployment that has not been wired to an MTA that checks, and it must be distinguishable
    from a message that was checked and failed: the first is our configuration problem and
    the second is somebody attacking us. Collapsing them makes an unconfigured install look
    like it is under attack, and a real attack look like a configuration gap.

    All three are treated identically at the gate, which is the correct outcome and not a
    reason to have fewer of them. What differs is what an operator is told.
    """

    #: SPF, DKIM and DMARC were evaluated and the message is aligned with its From domain.
    PASSED = "pass"
    #: Evaluated, and it is not.
    FAILED = "fail"
    #: Nobody evaluated it. Not a pass. See the class docstring.
    NOT_CHECKED = "not_checked"

    @property
    def establishes_a_sender(self) -> bool:
        """Whether a `From` address on this message may be treated as an address at all.

        A property rather than `is Authentication.PASSED` written at each call site, so a
        fourth verdict is one edit here rather than a search. `NOT_CHECKED` answers False,
        which means an install that is not wired to a checking MTA accepts nothing, and
        that is the safe direction: it fails as "email does not work yet" rather than as
        "anybody may be anybody".
        """
        return self is Authentication.PASSED


#: What email can carry. No `CARDS`, for the reason WhatsApp has none and one email does not:
#: `gate.admission.CHANNEL_VERBS` gives email `read` alone, so a button could never be
#: honoured as an approval. No `EPHEMERAL`, because a mail has exactly one copy per
#: recipient and a per-viewer body has nowhere to live. No `EDIT_IN_PLACE`, because a message
#: that has been delivered cannot be recalled, whatever a mail client's button says.
EMAIL_FEATURES: Final[frozenset[Feature]] = frozenset({Feature.ATTACHMENTS})

#: Headers that mark a message as machine-generated. Both are read: RFC 3834's is the
#: correct one and `Precedence` is what a great deal of software still sends.
AUTO_SUBMITTED = "auto-submitted"
PRECEDENCE = "precedence"

#: `Auto-Submitted` values that mean a human did not send this. The RFC's "no" means a human
#: did, and is the only value that is not automatic.
HUMAN_AUTO_SUBMITTED: Final = "no"

#: `Precedence` values that mean the same thing in older software.
BULK_PRECEDENCE: Final = frozenset({"bulk", "junk", "list", "auto_reply"})

#: What this module puts on its own replies so a correct counterpart does not answer them.
#: Without it two systems answer each other until a mailbox fills, which is the classic way
#: an autoresponder takes down a support address.
OUR_AUTO_SUBMITTED: Final = "auto-replied"

#: The prefix a reply's subject carries. The rest of the subject is the sender's own words,
#: which they already know. See `A_SUBJECT_OUTLIVES_THE_MESSAGE`.
REPLY_PREFIX: Final = "Re: "

#: What a reply's subject falls back to when the original had none. Fixed text, deliberately
#: saying nothing about the question or the answer.
NO_SUBJECT: Final = "Your question"

#: The highest assurance an email binding is ever worth, whatever the authentication said.
#: Equal to `Assurance.BOUND` and stated here so the ceiling is visible in this file rather
#: than inferred from the absence of anything raising it.
EMAIL_ASSURANCE_CEILING: Final = Assurance.BOUND


class EmailRefusedError(Exception):
    """Raised when a message cannot be read, or a reply must not be sent.

    An operations and programming error rather than a user-facing one. What a sender sees is
    either the ordinary unrecognised prompt or nothing at all, by design: a bounce that
    explained why would tell whoever forged the message which part of the forgery failed.
    """


def _header(headers: dict[str, str], name: str) -> str:
    """One header, case-insensitively, or an empty string.

    Header names are case-insensitive per RFC 5322 and arrive from a dozen different mail
    stacks with a dozen different capitalisations. A plain `headers.get("Auto-Submitted")`
    reads as correct and misses `auto-submitted:`, which is the spelling that matters
    because it is the one an attacker picks after reading this file.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def is_automatic(headers: dict[str, str]) -> bool:
    """Whether this message was generated by software rather than written by a person.

    Answered on the headers alone. A heuristic over the body ("looks like an out of office")
    would refuse real questions from people who happen to be away, and the cost of a missed
    detection here is a loop rather than a disclosure, so the cheap and exact check is the
    right one.
    """
    auto = _header(headers, AUTO_SUBMITTED).strip().lower()
    if auto and auto != HUMAN_AUTO_SUBMITTED:
        return True
    return _header(headers, PRECEDENCE).strip().lower() in BULK_PRECEDENCE


def sender_address(from_header: str) -> str:
    """The addr-spec out of a `From` header, lowercased, with the display name discarded.

    **The display name is attacker-controlled and is never read.** `"Rupash Jha"
    <attacker@example.invalid>` renders in most clients as the name alone, which is the whole
    trick; `channels.lark.Mention` refuses a display name for the same reason and
    `channels.whatsapp` refuses the WhatsApp profile name. Only the address is keyed on.

    Lowercased because the domain is case-insensitive and mailbox names are case-insensitive
    in every mail system anybody actually runs. Two digests for one person would be two
    bindings, and the one they did not use looks unbound.
    """
    _display, addr = parseaddr(from_header)
    if not addr or "@" not in addr:
        msg = "this message has no usable From address, so there is nobody it could be from"
        raise EmailRefusedError(msg)
    return addr.strip().lower()


@dataclass(frozen=True)
class InboundEmail:
    """One message, already authenticated by something else, as this module reads it.

    `authentication` is required and has no default. A default of `PASSED` would be a
    catastrophe waiting for one forgetful caller, and a default of `FAIL` would be a
    plausible-looking constructor that silently refuses every real message. Making it
    required means the caller has to have asked.
    """

    message_id: str
    from_header: str
    subject: str
    body: str
    received_at: datetime
    authentication: Authentication
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message_id:
            # Without one there is no dedupe key, and mail is retried by design: a
            # temporary failure anywhere on the path produces the same message again.
            msg = (
                "this message has no Message-ID, so a redelivery could not be told from a "
                "new question"
            )
            raise EmailRefusedError(msg)


def normalise(message: InboundEmail) -> ChannelEvent:
    """One inbound message as the shape the gate reads, or a refusal (M10.5.6).

    Refuses before it reads anything else when the message did not authenticate. The order
    matters: parsing a `From` header on an unauthenticated message and then discarding it is
    one edit away from parsing it and keeping it, and the edit looks like a tidy-up.

    Automatic mail is refused here rather than filtered by the caller, so that every path
    into the gate gets the same answer. A caller that filters is a caller that can forget.
    """
    if not message.authentication.establishes_a_sender:
        msg = (
            f"this message's authentication is {message.authentication.value!r}. "
            f"{AN_UNAUTHENTICATED_MESSAGE_HAS_NO_SENDER}"
        )
        raise EmailRefusedError(msg)
    if is_automatic(message.headers):
        msg = (
            "this message is machine-generated, and answering it invites a reply from the "
            "machine that sent it; two systems answering each other end at a full mailbox"
        )
        raise EmailRefusedError(msg)
    return ChannelEvent(
        channel=Channel.EMAIL,
        external_id=message.message_id,
        channel_identity=sender_address(message.from_header),
        text=message.body,
        received_at=message.received_at,
    )


def reply_subject(original: str) -> str:
    """A reply's subject: the sender's own words, prefixed once.

    Their own subject is the one string we can return without disclosing anything, because
    they wrote it. Nothing else goes here. See `A_SUBJECT_OUTLIVES_THE_MESSAGE`.

    Prefixed once rather than each time. A thread answered twice would otherwise read
    `Re: Re: Re:`, which is cosmetic, and the reason to fix it is not cosmetic: a subject
    that grows without bound is a subject that gets truncated, and what survives truncation
    is the prefixes rather than the words.
    """
    subject = original.strip()
    if not subject:
        return NO_SUBJECT
    if subject.lower().startswith(REPLY_PREFIX.strip().lower()):
        return subject
    return f"{REPLY_PREFIX}{subject}"


@dataclass(frozen=True)
class Reply:
    """One outbound message, planned and not yet sent.

    **There is no `cc` field and no `bcc` field.** Reply-all is not a feature that was left
    out, it is a thing a caller must not be able to ask for: the other participants have
    their own reach and none of it was consulted. See
    `A_REPLY_IS_ADDRESSED_TO_THE_PERSON_THE_ANSWER_WAS_COMPUTED_FOR`.

    `to_identity` is the salted digest, never the address. The address is needed once, at the
    wire, and is supplied there by whoever resolved the binding, so a reply built for one
    person cannot be delivered to another. `channels.whatsapp.Send` is the same shape for the
    same reason.
    """

    to_identity: str
    subject: str
    body: str
    payload: ChannelPayload
    #: The message being answered, so a client threads the reply. Carries no content.
    in_reply_to: str = ""

    def __post_init__(self) -> None:
        if not self.subject.strip():
            msg = "a reply with no subject is filed by mail clients as if it were spam"
            raise EmailRefusedError(msg)


def reply_to(event: ChannelEvent, payload: ChannelPayload, *, subject: str) -> Reply:
    """Plan a reply to one message, addressed to the person who sent it (M10.5.6).

    Takes the event rather than an address, so the recipient is the sender of the message
    being answered and cannot be a third party a caller passed in. The only way to address
    somebody else is to answer a different message.

    The body is rendered from the payload by `cards.render_body`, which every channel shares,
    so an email and a Lark message cannot disagree about what a payload says or about
    carrying its label.
    """
    if event.channel is not Channel.EMAIL:
        msg = (
            f"this event arrived over {event.channel} and would be answered by email; the "
            "reply belongs on the surface the question came from"
        )
        raise EmailRefusedError(msg)
    return Reply(
        to_identity=identity_hash(Channel.EMAIL, event.channel_identity),
        subject=reply_subject(subject),
        body=render_body(payload),
        payload=payload,
        in_reply_to=event.external_id,
    )


def unrecognised_reply(reach: Unrecognised, event: ChannelEvent, *, subject: str) -> Reply:
    """What a sender with no binding is told, in the words `gate.ingress` already wrote.

    **This module defines no prompt of its own**, for the reason `channels.whatsapp` gives:
    `UNRECOGNISED_PROMPT` answers an unknown address, a known but unbound one, and one whose
    binding was revoked this morning with the same words, and a second prompt written here is
    a second thing to get wrong in the direction that confirms an address belongs to somebody.

    Sent at all, rather than silently dropped, because the sender authenticated: they are a
    real address at a real domain, and the honest majority of them are staff who have not
    bound the channel. A message that failed authentication never reaches here, because
    `normalise` refused it, so this cannot become a way to make the system send mail to an
    address somebody else chose.
    """
    if reach.channel is not Channel.EMAIL:
        msg = (
            f"this reach was built for {reach.channel} and would be sent over email; the "
            "prompt a person is given is per channel"
        )
        raise EmailRefusedError(msg)
    return Reply(
        to_identity=identity_hash(Channel.EMAIL, event.channel_identity),
        subject=reply_subject(subject),
        body=reach.prompt,
        payload=ChannelPayload(),
        in_reply_to=event.external_id,
    )


@dataclass(frozen=True)
class SentEmail:
    """One message this adapter delivered. What a test reads instead of a mailbox.

    `to_identity` is the digest and not the address it was sent to. A list of addresses
    beside the answers they received is the staff directory joined to what each person asked,
    which is what `gate.ingress.Binding` declines to keep.
    """

    to_identity: str
    subject: str
    body: str
    headers: dict[str, str]


@dataclass
class EmailAdapter:
    """The email surface, with the transport left out on purpose.

    No SMTP client, no credentials, no mailbox. The case worth testing is a reply addressed
    to a participant the answer was not computed for, and a module that connected to a mail
    server could only be tested for it against a live account.
    """

    sent: list[SentEmail] = field(default_factory=list)
    reachable: bool = True

    def capabilities(self) -> ChannelCapabilities:
        """What this surface may carry, declared rather than inferred.

        `INTERNAL` and not `CONFIDENTIAL`. A mail leaves the tenant the moment it is sent,
        is retained by servers on both sides, is indexed by whatever the recipient uses, and
        is forwarded to anybody in one click with no second thought and no record here.
        `channels.adapter` names email as a surface a `restricted` field must not reach.
        """
        return ChannelCapabilities(
            channel=Channel.EMAIL,
            features=EMAIL_FEATURES,
            max_classification=Classification.INTERNAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """One inbound message, as the shape the gate reads.

        Takes an `InboundEmail` and refuses anything else rather than accepting a mapping and
        reading an authentication verdict out of it. A dict would let a caller hand over
        `{"authentication": "pass"}` assembled from the message itself, which is precisely
        the forgery this module exists to refuse. See
        `THE_VERDICT_COMES_FROM_THE_MTA_AND_NEVER_FROM_THE_MESSAGE`.
        """
        if not isinstance(raw, InboundEmail):
            msg = (
                f"this adapter normalises an InboundEmail and was handed a "
                f"{type(raw).__name__}; the authentication verdict has to come from whatever "
                f"checked it. {THE_VERDICT_COMES_FROM_THE_MTA_AND_NEVER_FROM_THE_MESSAGE}"
            )
            raise EmailRefusedError(msg)
        return normalise(raw)

    def send(self, payload: ChannelPayload, *, to: str, body: str = "", subject: str = "") -> None:
        """Put one message on the wire (M10.5.6).

        `assert_can_send` runs first and is not restated, so this adapter cannot disagree
        with any other about labels and classifications. The produced string is checked
        against the payload's label, so a caller cannot hand over a body that dropped it.

        Every message this adapter sends declares `Auto-Submitted: auto-replied`, so a
        counterpart that reads the header the way `is_automatic` does will not answer us.
        Written here rather than by the caller, because a caller that has to remember is a
        caller who forgets on the one path that loops.
        """
        assert_can_send(self.capabilities(), payload)
        rendered = body or render_body(payload)
        assert_label_survives(rendered, payload)
        self.sent.append(
            SentEmail(
                to_identity=identity_hash(Channel.EMAIL, to),
                subject=subject or NO_SUBJECT,
                body=rendered,
                headers={AUTO_SUBMITTED: OUR_AUTO_SUBMITTED},
            )
        )

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: EmailAdapter, reply: Reply, *, to_address: str) -> None:
    """Send one planned reply, to the address it was planned for (M10.5.6).

    The address arrives here and nowhere else. `Reply` holds a digest, so whoever resolved
    the binding supplies the address at the wire and this checks the two agree. Without it
    the address is simply a second argument, and the mistake that sends one person's answer
    to another is a variable name.

    The refusal names neither the address nor the digest. Both reach a log from here, and the
    pair of them is the directory this module declines to keep.
    """
    if identity_hash(Channel.EMAIL, to_address) != reply.to_identity:
        msg = (
            "this reply was planned for somebody else. "
            f"{A_REPLY_IS_ADDRESSED_TO_THE_PERSON_THE_ANSWER_WAS_COMPUTED_FOR}"
        )
        raise EmailRefusedError(msg)
    adapter.send(reply.payload, to=to_address, body=reply.body, subject=reply.subject)
