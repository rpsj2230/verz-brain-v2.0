"""Email: the channel where the sender writes their own name on the envelope.

Every other adapter here is handed an identity by something that checked it. Email is handed
a `From:` header the sender composed, so the tests below are mostly about the two places that
fact leaks into the rest of the system.

**The verdict is a parameter, never a header.** The convenient implementation reads
`Authentication-Results` out of the message and believes it, and that header is written by
whoever wrote the `From` beside it. The tests assert the absence of that path as well as the
refusal, because a refusal can be added and a parse can be added back beside it.

**A reply goes to one person.** `Reply` has no `cc` and no `bcc`, asserted against the type's
fields rather than only against behaviour, because reply-all is not a bug somebody writes on
purpose: it is a field somebody adds because a thread looked untidy.

Task ids: M10.5.6
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.channels.adapter import Feature
from brain.channels.email import (
    AUTO_SUBMITTED,
    EMAIL_ASSURANCE_CEILING,
    EMAIL_FEATURES,
    NO_SUBJECT,
    OUR_AUTO_SUBMITTED,
    Authentication,
    EmailAdapter,
    EmailRefusedError,
    InboundEmail,
    Reply,
    deliver,
    is_automatic,
    normalise,
    reply_subject,
    reply_to,
    sender_address,
    unrecognised_reply,
)
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
ADDRESS = "rupash@verzdesign.com"
DIGEST = identity_hash(Channel.EMAIL, ADDRESS)


def _inbound(
    *,
    authentication: Authentication = Authentication.PASSED,
    from_header: str = ADDRESS,
    subject: str = "Invoice question",
    headers: dict[str, str] | None = None,
) -> InboundEmail:
    return InboundEmail(
        message_id="<abc@mail.example>",
        from_header=from_header,
        subject=subject,
        body="what is outstanding",
        received_at=NOW,
        authentication=authentication,
        headers=headers or {},
    )


def _payload() -> ChannelPayload:
    return ChannelPayload(records=({"invoice": "INV-1"},))


# ------------------------------------------------- the verdict is not in the message
@pytest.mark.parametrize("verdict", [Authentication.FAILED, Authentication.NOT_CHECKED])
def test_a_message_that_did_not_authenticate_has_no_sender(verdict: Authentication) -> None:
    """A message that failed DMARC is not from somebody we cannot place, it is from nobody.
    Deriving an identity from its From header lets anybody address this system as anybody,
    and the binding they reach is that person's own.

    `NOT_CHECKED` is refused alongside `FAILED` deliberately: an install not yet wired to a
    checking MTA must accept nothing rather than accept everything. Delete this and the safe
    direction inverts, so a deployment gap becomes an open door."""
    with pytest.raises(EmailRefusedError, match="from nobody"):
        normalise(_inbound(authentication=verdict))


def test_an_authenticated_message_becomes_an_ordinary_channel_event() -> None:
    """The positive case. A normaliser that refused everything would satisfy every refusal in
    this file and mean no email ever reaches the gate, which is a channel that looks built and
    answers nothing."""
    event = normalise(_inbound())

    assert event.channel is Channel.EMAIL
    assert event.channel_identity == ADDRESS
    assert event.external_id == "<abc@mail.example>"


def test_nothing_in_this_module_reads_the_authentication_results_header() -> None:
    """**The absence is the design, so the absence is what is asserted.**

    A refusal can be kept while a parse is added beside it, and the parse would look like an
    improvement: it removes an argument from the caller. But that header sits inside the
    message, so a sender who forges `From` forges it in the same breath, and the honest copy
    the receiving MTA prepended is textually identical to the forgeries stacked above it.

    **Asserted by behaviour, after two attempts to assert it over the source failed for the
    same reason.** Searching the text matched the module's own docstring, which explains the
    attack and therefore names the header. Parsing it and dropping docstrings then matched the
    named reason constant, which is also prose. Both failures are the trap `CLAUDE.md` records
    twice: a test satisfiable by writing near it passes when the prose is right and the code is
    wrong.

    So the header is put in the message and shown to change nothing, in both directions. A
    forged pass does not rescue a failed verdict, and a forged fail does not sink a real one.
    Only the argument decides, which is the property; a parse added later would break one of
    these two whichever way it leaned.

    Delete this and the convenient two-line version can come back with every other test in
    this file still green."""
    forged_pass = {"Authentication-Results": "mx.example; dmarc=pass header.from=verzdesign.com"}

    with pytest.raises(EmailRefusedError, match="from nobody"):
        normalise(_inbound(authentication=Authentication.FAILED, headers=forged_pass))

    forged_fail = {"Authentication-Results": "mx.example; dmarc=fail header.from=verzdesign.com"}
    event = normalise(_inbound(authentication=Authentication.PASSED, headers=forged_fail))

    assert event.channel_identity == ADDRESS, "the MTA's verdict decides, not the message's"


def test_the_adapter_refuses_a_mapping_and_takes_only_a_checked_message() -> None:
    """A dict would let a caller hand over `{"authentication": "pass"}` assembled from the
    message itself, which is exactly the forgery the module exists to refuse. Requiring the
    type means the verdict had to be constructed by somebody who had one."""
    raw = {"message_id": "<a@b>", "from_header": ADDRESS, "authentication": "pass"}

    with pytest.raises(EmailRefusedError, match="InboundEmail"):
        EmailAdapter().normalise(raw)


def test_the_authentication_verdict_has_no_default() -> None:
    """A default of `PASSED` is a catastrophe waiting for one forgetful caller, and a default
    of `FAILED` is a constructor that plausibly refuses every real message. Requiring it means
    the caller had to have asked somebody.

    Delete this and a default can be added for the convenience of the test that constructs
    these, which is the caller least affected by getting it wrong."""
    with pytest.raises(TypeError):
        InboundEmail(  # type: ignore[call-arg]
            message_id="<a@b>",
            from_header=ADDRESS,
            subject="s",
            body="b",
            received_at=NOW,
        )


def test_an_email_binding_is_never_worth_more_than_bound() -> None:
    """DMARC authenticates a domain and not a person: on a shared tenant a pass covers every
    colleague, and on a mailing list it covers the list. Delete this and a passing DMARC can
    be read as evidence about who is asking, which it has never been."""
    assert EMAIL_ASSURANCE_CEILING is Assurance.BOUND
    assert EMAIL_ASSURANCE_CEILING < Assurance.AUTHENTICATED


# ------------------------------------------------- the display name is the sender's
def test_the_display_name_is_discarded_and_only_the_address_is_keyed_on() -> None:
    """`"Rupash Jha" <attacker@example.invalid>` renders in most clients as the name alone,
    which is the whole trick. `channels.lark.Mention` refuses a display name for this reason
    and `channels.whatsapp` refuses the WhatsApp profile name.

    Delete this and a friendly-looking sender becomes an identity."""
    spoofed = sender_address('"Rupash Jha" <attacker@example.invalid>')

    assert spoofed == "attacker@example.invalid"
    assert identity_hash(Channel.EMAIL, spoofed) != DIGEST


def test_an_address_is_lowercased_so_one_person_has_one_digest() -> None:
    """Mailbox and domain are case-insensitive in every mail system anybody runs. Two digests
    for one person are two bindings, and the one they did not use looks unbound."""
    assert sender_address("Rupash@VerzDesign.COM") == ADDRESS


def test_a_message_with_no_usable_address_has_nobody_it_could_be_from() -> None:
    """An empty or malformed From is not an anonymous sender to be handled gently; there is
    no address to key on at all."""
    with pytest.raises(EmailRefusedError, match="nobody it could be from"):
        sender_address("undisclosed-recipients:;")


def test_a_message_with_no_id_cannot_be_told_apart_from_its_own_redelivery() -> None:
    """Mail is retried by design: a temporary failure anywhere on the path produces the same
    message again. Without an id the second copy is a new question, and for anything with a
    side effect that is the thing being done twice."""
    with pytest.raises(EmailRefusedError, match="Message-ID"):
        InboundEmail(
            message_id="",
            from_header=ADDRESS,
            subject="s",
            body="b",
            received_at=NOW,
            authentication=Authentication.PASSED,
        )


# ------------------------------------------------- automatic mail
@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"auto-submitted": "auto-generated"},
        {"Precedence": "bulk"},
        {"PRECEDENCE": "list"},
    ],
)
def test_machine_generated_mail_is_not_answered(headers: dict[str, str]) -> None:
    """Two systems answering each other end at a full mailbox, which is the classic way an
    autoresponder takes down a support address.

    The casing varies on purpose. Header names are case-insensitive per RFC 5322 and arrive
    from a dozen mail stacks with a dozen capitalisations, so a plain `headers.get(...)` reads
    as correct and misses the spelling an attacker picks after reading the source."""
    assert is_automatic(headers) is True

    with pytest.raises(EmailRefusedError, match="machine-generated"):
        normalise(_inbound(headers=headers))


def test_a_person_who_says_they_are_a_person_is_answered() -> None:
    """RFC 3834's `Auto-Submitted: no` is what a human's mail says when it says anything. The
    positive case matters here because the cheap implementation treats the header's presence
    as the signal, which refuses every message from a client that sets it correctly."""
    assert is_automatic({"Auto-Submitted": "no"}) is False
    assert normalise(_inbound(headers={"Auto-Submitted": "no"})).channel_identity == ADDRESS


def test_our_own_replies_say_they_are_automatic_so_nobody_answers_them() -> None:
    """The other half of the loop. A counterpart that reads the header the way `is_automatic`
    does will not answer us, and it is written by the adapter rather than by the caller
    because a caller who has to remember forgets on exactly the path that loops."""
    adapter = EmailAdapter()
    adapter.send(_payload(), to=ADDRESS, subject="Re: hello")

    assert adapter.sent[0].headers[AUTO_SUBMITTED] == OUR_AUTO_SUBMITTED
    assert is_automatic(adapter.sent[0].headers) is True


# ------------------------------------------------- a reply goes to one person
def test_a_reply_has_nowhere_to_put_a_second_recipient() -> None:
    """**Reply-all is not a bug somebody writes on purpose.** It is a `cc` field somebody adds
    because a thread looked untidy, and the disclosure is to everybody who happened to be on a
    mail written by somebody else, none of whose reach was consulted.

    Asserted against the type's fields rather than against behaviour, because behaviour with
    no `cc` is indistinguishable from behaviour with an empty one."""
    assert set(Reply.__dataclass_fields__) == {
        "to_identity",
        "subject",
        "body",
        "payload",
        "in_reply_to",
    }


def test_a_reply_is_addressed_to_the_sender_of_the_message_being_answered() -> None:
    """`reply_to` takes the event rather than an address, so the recipient is the person who
    asked and cannot be a third party a caller passed in. The only way to address somebody
    else is to answer a different message."""
    event = normalise(_inbound())

    reply = reply_to(event, _payload(), subject="Invoice question")

    assert reply.to_identity == DIGEST
    assert reply.in_reply_to == "<abc@mail.example>"


def test_a_reply_cannot_be_delivered_to_an_address_it_was_not_planned_for() -> None:
    """Without the check the address is simply a second argument, and the mistake that sends
    one person's answer to another is a variable name."""
    adapter = EmailAdapter()
    reply = reply_to(normalise(_inbound()), _payload(), subject="s")

    with pytest.raises(EmailRefusedError, match="planned for somebody else"):
        deliver(adapter, reply, to_address="someone.else@verzdesign.com")

    assert adapter.sent == [], "nothing reaches the wire when the recipient disagrees"


def test_the_refusal_names_neither_the_address_nor_the_digest_it_expected() -> None:
    """Both reach a log from here, and the pair of them is the staff directory joined to what
    each person asked. Delete this and a diagnostic improvement puts the address in it."""
    reply = reply_to(normalise(_inbound()), _payload(), subject="s")

    with pytest.raises(EmailRefusedError) as caught:
        deliver(EmailAdapter(), reply, to_address="someone.else@verzdesign.com")

    assert "someone.else@verzdesign.com" not in str(caught.value)
    assert DIGEST not in str(caught.value)


def test_a_delivered_reply_records_the_digest_and_never_the_address() -> None:
    """The positive case, and the one showing the address is used once and not kept."""
    adapter = EmailAdapter()
    reply = reply_to(normalise(_inbound()), _payload(), subject="Invoice question")

    deliver(adapter, reply, to_address=ADDRESS)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].to_identity == DIGEST
    assert ADDRESS not in adapter.sent[0].to_identity


def test_an_event_from_another_channel_is_not_answered_by_email() -> None:
    """The reply belongs on the surface the question came from. Delete this and a Lark
    question can be answered to an address, which is a different audience."""
    event = ChannelEvent(
        channel=Channel.LARK,
        external_id="om_1",
        channel_identity="ou_abc",
        text="hello",
        received_at=NOW,
    )

    with pytest.raises(EmailRefusedError, match="surface the question came from"):
        reply_to(event, _payload(), subject="s")


# ------------------------------------------------- the subject
def test_a_reply_subject_is_the_senders_own_words_and_nothing_else() -> None:
    """A subject appears on a lock screen, in a mail server's log, in a backup index and in a
    mailbox list, none of which the gate decided and all of which outlive the message. Their
    own subject is the one string we can return without disclosing anything, because they
    wrote it."""
    assert reply_subject("Invoice question") == "Re: Invoice question"


def test_a_subject_is_not_prefixed_twice_however_often_a_thread_is_answered() -> None:
    """Cosmetic on its face and not cosmetic underneath: a subject that grows without bound
    gets truncated, and what survives truncation is the prefixes rather than the words."""
    assert reply_subject("Re: Invoice question") == "Re: Invoice question"
    assert reply_subject("re: invoice question") == "re: invoice question"


def test_a_message_with_no_subject_gets_fixed_text_that_says_nothing() -> None:
    """The fallback cannot be built from the question or the answer, which is the tempting
    way to make an empty subject useful."""
    assert reply_subject("   ") == NO_SUBJECT
    assert "invoice" not in NO_SUBJECT.lower()


def test_a_reply_with_no_subject_at_all_is_refused() -> None:
    """Mail clients file a subjectless message as spam, so an answer somebody is waiting for
    arrives where they will not look."""
    with pytest.raises(EmailRefusedError, match="spam"):
        Reply(to_identity=DIGEST, subject="  ", body="hello", payload=ChannelPayload())


# ------------------------------------------------- the surface
def test_this_surface_declares_no_cards_and_no_ephemeral_and_no_edit() -> None:
    """Three absences with three different reasons. `gate.admission.CHANNEL_VERBS` gives email
    `read` alone, so a button could never be honoured as an approval. A mail has one copy per
    recipient, so a per-viewer body has nowhere to live. And a delivered message cannot be
    recalled, whatever a mail client's button claims."""
    features = EmailAdapter().capabilities().features

    assert features == EMAIL_FEATURES
    assert Feature.CARDS not in features
    assert Feature.EPHEMERAL not in features
    assert Feature.EDIT_IN_PLACE not in features


def test_the_classification_ceiling_is_internal_and_not_confidential() -> None:
    """A mail leaves the tenant when it is sent, is retained by servers on both sides, is
    indexed by whatever the recipient uses, and is forwarded to anybody in one click with no
    record here. Raising this is a decision somebody makes deliberately."""
    assert EmailAdapter().capabilities().max_classification is Classification.INTERNAL


# ------------------------------------------------- the unrecognised sender
def test_an_unrecognised_sender_is_told_the_words_the_gate_already_wrote() -> None:
    """This module defines no prompt of its own. `gate.ingress.UNRECOGNISED_PROMPT` answers an
    unknown address, a known but unbound one, and one whose binding was revoked this morning
    with the same words; a second prompt here is a second thing to get wrong in the direction
    that confirms an address belongs to somebody."""
    event = normalise(_inbound())
    reach = Unrecognised(channel=Channel.EMAIL)

    reply = unrecognised_reply(reach, event, subject="Invoice question")

    assert reply.body == reach.prompt
    assert reply.to_identity == DIGEST


def test_an_unrecognised_reply_can_only_be_built_for_a_sender_who_authenticated() -> None:
    """The guard that stops this becoming a way to make the system send mail to an address
    somebody else chose. A message that failed authentication never produces an event, so it
    never reaches here, and there is no other way in.

    Delete this and the reasoning stops being checked: the refusal lives in `normalise`, and
    a later caller constructing an event by hand would bypass it."""
    with pytest.raises(EmailRefusedError, match="from nobody"):
        normalise(_inbound(authentication=Authentication.FAILED))


def test_a_reach_built_for_another_channel_is_not_sent_by_email() -> None:
    """The prompt a person is given is per channel: the widget's differs and argues at length
    why, and none of that argument transfers to an email address."""
    event = normalise(_inbound())

    with pytest.raises(EmailRefusedError, match="per channel"):
        unrecognised_reply(Unrecognised(channel=Channel.WIDGET), event, subject="s")
