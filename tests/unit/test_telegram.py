"""Telegram: a header that is the whole authentication, and a room that cannot be answered.

Three claims carry this file.

**Nothing is read until the secret token has been checked, and the check is constant time.**
Both halves are tested, and the second one is tested by behaviour rather than by reading the
source: a header carrying a non-ASCII character has to come back as an ordinary refusal, which
is only true if both sides are hashed before they are compared. The ordering is tested by
presenting a wrong secret together with a body that is not an object at all, and requiring the
secret's refusal rather than the body's.

**The identity is the numeric id and the username is not read anywhere.** Tested in both
directions, in the shape `tests/unit/test_email.py` settled on after two attempts to assert an
absence over the source text passed on the strength of the module's own prose: two updates that
share a numeric id and differ in username are one person, and two that share a username and
differ in numeric id are two.

**An answer computed at one person's reach cannot reach a group.** Held by the types rather
than by a check, so it is asserted against the types: `Notice` is the only plan that can
address a chat with more than one reader and it has no payload field to put an answer in.

Task ids: M10.5.4
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import pytest

from brain.channels.adapter import Feature
from brain.channels.telegram import (
    ALLOWED_NOTICES,
    GROUP_DEFLECTION,
    MINIMUM_SECRET_LENGTH,
    TELEGRAM_ASSURANCE_CEILING,
    TELEGRAM_FEATURES,
    UPDATE_NOT_ACCEPTED,
    Answer,
    ChatKind,
    Notice,
    TelegramAdapter,
    TelegramMessage,
    TelegramRefusedError,
    VerifiedUpdate,
    assert_from_telegram,
    audience_is_one_person,
    deliver,
    group_deflection,
    normalise_update,
    reply_privately,
    unrecognised_reply,
    verified_update,
)
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.admission import Assurance, verbs_for_channel
from brain.gate.context import Channel, TrafficClass, traffic_class_for
from brain.gate.ingress import (
    LEAKING_PATTERNS,
    ChannelEvent,
    Unrecognised,
    identity_hash,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
EPOCH_SECONDS = int(NOW.timestamp())

SENDER_ID = 573194821
OTHER_SENDER_ID = 573194822
#: Telegram gives a group a negative chat id, which is why a person's digest can never match
#: one. The tests lean on that in `deliver` rather than on the sign itself.
GROUP_ID = -1001234567890

SECRET = "z8Qv3Lm1Rt7Yb2Nc5Hj9Kd4Ws6Px0Ae"[:31] + "Q"
DIGEST = identity_hash(Channel.TELEGRAM, str(SENDER_ID))
GROUP_DIGEST = identity_hash(Channel.TELEGRAM, str(GROUP_ID))


def _update(
    *,
    update_id: int = 4001,
    sender_id: int = SENDER_ID,
    chat_id: int | None = None,
    chat_type: str = "private",
    text: str = "what is outstanding",
    username: str = "rupash",
    is_bot: bool = False,
    date: int = EPOCH_SECONDS,
) -> dict[str, object]:
    """The envelope Telegram posts, with the knobs each test needs to turn."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 77,
            "from": {
                "id": sender_id,
                "is_bot": is_bot,
                "first_name": "Somebody",
                "username": username,
            },
            "chat": {"id": sender_id if chat_id is None else chat_id, "type": chat_type},
            "date": date,
            "text": text,
        },
    }


def _verified(raw: object) -> VerifiedUpdate:
    return verified_update(raw, configured=SECRET, presented=SECRET)


def _message(**kwargs: object) -> TelegramMessage:
    return normalise_update(_verified(_update(**kwargs)))  # type: ignore[arg-type]


def _payload() -> ChannelPayload:
    return ChannelPayload(records=({"invoice": "INV-1"},))


# ------------------------------------------------- the header is the whole authentication
def test_an_update_presenting_the_registered_secret_is_accepted() -> None:
    """The positive case. A verifier that refused everything satisfies every refusal below
    and means no Telegram message ever reaches the gate, which is a channel that looks built
    and answers nothing."""
    update = _verified(_update())

    assert update.body["update_id"] == 4001


@pytest.mark.parametrize(
    "presented",
    ["", "wrong", SECRET[:-1], SECRET + "x", SECRET.upper()],
)
def test_an_update_presenting_the_wrong_secret_is_refused(presented: str) -> None:
    """**The check the whole channel rests on.** The webhook URL is not a secret: it is in a
    deployment log, a proxy access log and the browser history of whoever tested it. Without
    this, anybody who learns the URL posts an update naming any user id and the system answers
    as that person, at that person's reach, with every later step behaving correctly.

    A prefix, a suffix and a case change are all present deliberately: each is what a
    comparison written slightly wrong would accept.

    Delete this and the header becomes decoration."""
    with pytest.raises(TelegramRefusedError, match=UPDATE_NOT_ACCEPTED):
        assert_from_telegram(configured=SECRET, presented=presented)


def test_a_refusal_says_one_fixed_sentence_and_never_how_close_the_guess_was() -> None:
    """Telling somebody probing which part to fix next is how they fix it, which is the
    argument `channels.webhook.WebhookRefusedError` makes about itself. A refusal that
    distinguished "empty" from "wrong length" from "wrong value" is a guided search.

    Delete this and a diagnostic improvement puts the presented value, or its length, into the
    message and from there into whatever logs refusals."""
    with pytest.raises(TelegramRefusedError) as caught:
        assert_from_telegram(configured=SECRET, presented="zzzz")

    assert str(caught.value) == UPDATE_NOT_ACCEPTED
    assert SECRET not in str(caught.value)
    assert "zzzz" not in str(caught.value)


def test_a_header_that_is_not_ascii_is_refused_rather_than_raising() -> None:
    """**This is the constant-time comparison asserted by behaviour rather than by reading
    the source.** `hmac.compare_digest` on two `str` values raises `TypeError` on non-ASCII
    input, and the presented value is whatever an attacker put in a header. Hashing both sides
    first is what makes this a refusal, and hashing is also what removes the length leak
    `compare_digest` otherwise has.

    Delete this and the comparison can be rewritten over the raw strings, which passes every
    other test here and turns one hostile header into a traceback out of the webhook
    endpoint."""
    with pytest.raises(TelegramRefusedError, match=UPDATE_NOT_ACCEPTED):
        assert_from_telegram(configured=SECRET, presented="éèê" * 20)


def test_the_comparison_is_not_written_as_an_equality() -> None:
    """A timing side channel cannot be observed from a unit test, so the property is asserted
    over the parsed function instead: no `==` and no `!=` anywhere in it, and exactly one call
    to `hmac.compare_digest`.

    `==` on a digest returns as soon as two bytes differ, so the time it takes says how much of
    a guess was right, and a few thousand requests turn that into the secret. `channels.webhook`
    says the same thing about its signature check and has nothing asserting it.

    Parsed rather than searched, because a substring test for "compare_digest" is satisfied by
    the docstring above it, which is the trap `CLAUDE.md` records twice."""
    tree = ast.parse(inspect.getsource(assert_from_telegram))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    constant_time = [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
    ]
    equalities = [
        operator
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        for operator in node.ops
        if isinstance(operator, ast.Eq | ast.NotEq)
    ]

    assert len(constant_time) == 1, "the secret is compared exactly once, in constant time"
    assert equalities == [], "an equality here leaks the secret one byte at a time"


def test_a_deployment_with_a_short_secret_refuses_everything_loudly() -> None:
    """Telegram permits a one-character secret and the endpoint it protects is a public HTTPS
    URL with no rate limit in front of it, so the vendor's floor is not a floor.

    This refusal names the problem, unlike the one above, and the difference is deliberate: it
    fires on every delivery including the honest ones, so it tells an attacker nothing they
    could not learn by sending anything at all, and a misconfigured authenticator should fail
    loudly rather than quietly.

    Delete this and an install configured with `test` accepts `test`."""
    short = "x" * (MINIMUM_SECRET_LENGTH - 1)

    with pytest.raises(TelegramRefusedError, match="shorter than"):
        assert_from_telegram(configured=short, presented=short)


def test_the_secret_is_checked_before_the_body_is_even_looked_at() -> None:
    """**The order is the property.** A parser reached before the authenticator is a parser an
    anonymous caller can run, and a parser is code. Asserted by presenting a wrong secret
    together with a body that is not an object at all: the refusal that comes back has to be
    the secret's, because the body's refusal would prove the parse ran first.

    Delete this and the two lines can be swapped during a tidy-up, which changes no test and
    exposes the reader to the internet."""
    with pytest.raises(TelegramRefusedError, match=UPDATE_NOT_ACCEPTED):
        verified_update("not an object at all", configured=SECRET, presented="wrong")

    with pytest.raises(TelegramRefusedError, match="not a Telegram update"):
        verified_update("not an object at all", configured=SECRET, presented=SECRET)


def test_an_update_cannot_be_marked_verified_by_anything_but_the_verifier() -> None:
    """The check is unskippable because the type carrying a checked body cannot be built
    without it, which is `gate.catalogue.ProjectedCatalogue`'s constructor token and the shape
    `channels.webhook.verified_handler` argues for.

    Delete this and the token guard can be removed as ceremony, leaving a `verify` a caller
    has to remember to call, which is a check that goes missing from the call site somebody
    adds later."""
    with pytest.raises(TelegramRefusedError, match="only be marked verified"):
        VerifiedUpdate(body={"update_id": 1})


def test_the_adapter_refuses_a_raw_mapping_and_takes_only_a_verified_update() -> None:
    """A mapping would let a caller hand over a body nobody checked the header on, which is
    exactly the forgery this module exists to refuse. `channels.email.EmailAdapter.normalise`
    insists on an `InboundEmail` for the same reason."""
    with pytest.raises(TelegramRefusedError, match="VerifiedUpdate"):
        TelegramAdapter().normalise(_update())


# ------------------------------------------------- the identity is the number
def test_the_identity_is_the_numeric_id_and_the_username_beside_it_is_not_read() -> None:
    """**Asserted in both directions, because an absence asserted over the source text passes
    on the strength of the module's own prose.** `tests/unit/test_email.py` records two
    attempts at that and why both were wrong.

    A Telegram username is lent rather than owned: its holder can change it, and a released
    handle can be registered by somebody else, who would then arrive holding a binding made
    for whoever had it before. `channels.whatsapp` refuses the profile name and
    `channels.email.sender_address` discards the display name for the same class of reason,
    and this is the sharper case: those two are attacker-chosen text and this one is
    attacker-acquirable.

    Delete this and the friendly-looking handle becomes the identity, which is a binding
    handed to whoever claims it next."""
    renamed = _message(username="somebody-else-entirely")
    impersonator = _message(sender_id=OTHER_SENDER_ID, username="rupash")

    assert renamed.event.channel_identity == str(SENDER_ID)
    assert identity_hash(Channel.TELEGRAM, renamed.event.channel_identity) == DIGEST
    assert impersonator.event.channel_identity == str(OTHER_SENDER_ID)
    assert identity_hash(Channel.TELEGRAM, impersonator.event.channel_identity) != DIGEST


def test_a_verified_private_message_becomes_an_ordinary_channel_event() -> None:
    """The positive case, and the one that pins every field the gate reads. A normaliser that
    refused everything would satisfy every refusal in this file and mean no Telegram message
    ever reaches the gate."""
    message = _message()

    assert message.event.channel is Channel.TELEGRAM
    assert message.event.external_id == "4001"
    assert message.event.channel_identity == str(SENDER_ID)
    assert message.event.text == "what is outstanding"
    assert message.event.received_at == NOW
    assert message.chat_kind is ChatKind.PRIVATE
    assert message.chat_id == SENDER_ID


def test_the_external_id_is_the_update_id_and_not_the_message_id() -> None:
    """`message_id` is unique inside one chat and not across chats, so two people's questions
    collide on it and the second is discarded as a redelivery of the first. `update_id` is the
    vendor's identifier for the delivery and is repeated when Telegram retries, which is
    exactly what `ChannelEvent.external_id` is for.

    Delete this and `message_id` reads as the more natural choice, because it is the id of the
    thing being answered."""
    one = _message(update_id=9100)
    two = _message(update_id=9101, chat_id=GROUP_ID, chat_type="group")

    assert one.event.external_id == "9100"
    assert one.event.dedupe_key == (Channel.TELEGRAM.value, "9100")
    assert one.event.external_id != two.event.external_id


@pytest.mark.parametrize("bad", [True, False, "573194821", 573194821.0, None])
def test_a_sender_id_that_is_not_a_whole_number_is_refused(bad: object) -> None:
    """`bool` is in this list on purpose: it is a subclass of `int` in Python, so a JSON `true`
    satisfies `isinstance(value, int)` and would go on to be the identity "True", which is one
    identity shared by everybody who sends one. A float is refused rather than truncated,
    because an id that arrived as `12345.0` was reformatted by something on the way here and
    truncating it agrees with whatever did."""
    raw: dict[str, object] = {
        "update_id": 1,
        "message": {
            "from": {"id": bad, "is_bot": False},
            "chat": {"id": 1, "type": "private"},
            "date": EPOCH_SECONDS,
            "text": "hello",
        },
    }

    with pytest.raises(TelegramRefusedError, match="whole-number id"):
        normalise_update(_verified(raw))


def test_a_message_from_a_bot_is_not_answered() -> None:
    """Two systems answering each other end at a rate limit, which is what
    `channels.email.is_automatic` refuses for the same reason. `is_bot` is documented as always
    present, so requiring it fails closed: a sender that does not say it is a person is not
    treated as one."""
    with pytest.raises(TelegramRefusedError, match="from a bot"):
        _message(is_bot=True)

    without: dict[str, object] = {
        "update_id": 1,
        "message": {
            "from": {"id": SENDER_ID},
            "chat": {"id": SENDER_ID, "type": "private"},
            "date": EPOCH_SECONDS,
            "text": "hello",
        },
    }
    with pytest.raises(TelegramRefusedError, match="from a bot"):
        normalise_update(_verified(without))


def test_a_message_with_no_text_is_not_read_as_a_blank_question() -> None:
    """A photo, a sticker, a location or a service message is a different shape with no `text`
    at all. Reading one as text puts an empty question through the gate, which answers it from
    whatever the empty string retrieves."""
    photo: dict[str, object] = {
        "update_id": 1,
        "message": {
            "from": {"id": SENDER_ID, "is_bot": False},
            "chat": {"id": SENDER_ID, "type": "private"},
            "date": EPOCH_SECONDS,
            "photo": [{"file_id": "abc"}],
        },
    }

    with pytest.raises(TelegramRefusedError, match="has no text"):
        normalise_update(_verified(photo))


def test_a_chat_type_telegram_does_not_document_is_refused_rather_than_guessed_at() -> None:
    """Guessing decides whether this conversation has one reader or a hundred, and both
    defaults are wrong: treating an unknown type as private answers a room at one person's
    reach, and treating it as a group sends a person fixed words instead of their answer."""
    with pytest.raises(TelegramRefusedError, match="not one Telegram documents"):
        _message(chat_type="forum_topic")


# ------------------------------------------------- what is not a question
def test_a_callback_query_is_refused_rather_than_read_as_a_decision() -> None:
    """**Two reasons, and the second is the stronger one.**
    `gate.admission.CHANNEL_VERBS` gives this channel `read` alone, so a press could never be
    honoured as an approval; that is the argument `channels.whatsapp` makes about reply
    buttons. On top of it, this adapter declares no `Feature.CARDS` and sends no inline
    keyboard, so a callback query addressed to this bot is a press on a button this system did
    not send. That is evidence of a fault, not an input to be handled.

    Delete this and a handler can be added for it, and the person pressing would reasonably
    believe they had approved something."""
    press: dict[str, object] = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": SENDER_ID, "is_bot": False},
            "data": "approve",
        },
    }

    with pytest.raises(TelegramRefusedError, match="sends no buttons"):
        normalise_update(_verified(press))

    assert Feature.CARDS not in TELEGRAM_FEATURES
    assert "approve" not in verbs_for_channel(Channel.TELEGRAM)


@pytest.mark.parametrize("key", ["edited_message", "channel_post", "my_chat_member"])
def test_only_a_message_update_is_read(key: str) -> None:
    """An `edited_message` carries a `message_id` already answered, so reading it as a new
    question answers the same message twice and reading it as the original re-answers a
    question whose text changed after the answer was computed. A `channel_post` may carry no
    `from` at all, because a broadcast post is attributable to the channel rather than to a
    person, and there is nobody to compute a reach for.

    Delete this and any of these can be mapped onto the message path, which reads as support
    for a feature and is a question answered on somebody else's behalf."""
    raw: dict[str, object] = {
        "update_id": 1,
        key: {
            "message_id": 77,
            "from": {"id": SENDER_ID, "is_bot": False},
            "chat": {"id": SENDER_ID, "type": "private"},
            "date": EPOCH_SECONDS,
            "text": "hello",
        },
    }

    with pytest.raises(TelegramRefusedError, match="reads 'message' updates"):
        normalise_update(_verified(raw))


# ------------------------------------------------- an answer goes where one person reads
@pytest.mark.parametrize("kind", [ChatKind.GROUP, ChatKind.SUPERGROUP, ChatKind.BROADCAST])
def test_an_answer_is_refused_for_a_chat_with_more_than_one_reader(kind: ChatKind) -> None:
    """**The failure this module exists to prevent, and it is invisible in a diff.** The answer
    was computed at the asker's reach; everybody else in the group has their own and none of it
    was consulted.

    There is deliberately no fallback that answers a smaller version of the question. Lark
    computes `channels.room.floor` over everybody present and posts at that, and that is not
    available here: `floor` needs the members, and the Bot API has `getChatMember` for an id you
    already hold and nothing that lists a group. A floor over an unknown set is not a floor.

    Delete this and a group question gets a private answer posted in front of everybody, and
    the message looks like every other message."""
    message = _message(chat_id=GROUP_ID, chat_type=kind.value)

    with pytest.raises(TelegramRefusedError, match="was computed at one person's reach"):
        reply_privately(message, _payload())


def test_an_answer_to_a_private_chat_carries_the_payload_the_gate_built() -> None:
    """The positive case. A planner that refused every chat would satisfy the refusal above and
    make the channel answer nobody at all."""
    answer = reply_privately(_message(), _payload())

    assert answer.to_identity == DIGEST
    assert "INV-1" in answer.body
    assert answer.payload == _payload()


def test_the_only_plan_that_can_address_a_group_has_nowhere_to_put_an_answer() -> None:
    """**The rule is carried by the types, so it is asserted against the types.** A check is a
    thing a later branch goes around; a field that does not exist is not.

    `Notice` is the only plan that can be addressed to a chat with more than one reader, and it
    has no payload field, so there is nothing for a value computed at one person's reach to
    travel in. `channels.whatsapp.SlotSource` leaves out a `value` field for the same reason,
    and `channels.email.Reply` leaves out `cc`.

    Delete this and a payload field can be added to `Notice` because a group deflection looked
    unhelpfully bare."""
    assert set(Notice.__dataclass_fields__) == {"to_identity", "body"}
    assert set(Answer.__dataclass_fields__) == {"to_identity", "payload", "body"}


def test_a_notice_may_only_say_one_of_the_fixed_things_this_module_wrote() -> None:
    """The other half of the same rule. Without it `body` is a free string aimed at a room, and
    the first thing somebody interpolates into it is the asker's name, then a value out of the
    answer."""
    with pytest.raises(TelegramRefusedError, match="fixed things this module wrote"):
        Notice(to_identity=GROUP_DIGEST, body="Wei Ling, your invoice INV-1 is ready.")

    assert set(ALLOWED_NOTICES) == {GROUP_DEFLECTION}


def test_a_group_is_told_the_same_thing_whoever_asked() -> None:
    """**The signature is the property.** `group_deflection` takes the message and nothing
    else: no reach, no binding, no entitlement set and no payload, so the words a group sees
    cannot depend on who asked or on whether they are bound to anybody.

    That is the DENIED-and-ABSENT rule applied where it is easiest to break. A group whose
    bound members got a different sentence from its unbound ones would publish each member's
    binding status to everybody else in the room, one question at a time.

    Delete this and a `reach` parameter can be added so the deflection can be more helpful to
    people who are not set up yet."""
    parameters = inspect.signature(group_deflection).parameters

    assert list(parameters) == ["message"]

    notice = group_deflection(_message(chat_id=GROUP_ID, chat_type="group"))

    assert notice.body == GROUP_DEFLECTION
    assert notice.to_identity == GROUP_DIGEST


def test_the_group_deflection_confirms_nothing_about_anybody() -> None:
    """Checked against `gate.ingress.LEAKING_PATTERNS`, the same rule an unrecognised prompt is
    held to, because this sentence is read by everybody in the room rather than by one person
    holding a handset. It also says nothing about the question, which the asker already put in
    front of the group, and nothing about there having been an answer to give.

    Delete this and the wording can be improved into "I have no record of you here"."""
    for pattern in LEAKING_PATTERNS:
        assert pattern.search(GROUP_DEFLECTION) is None, GROUP_DEFLECTION
    assert "invoice" not in GROUP_DEFLECTION.lower()


def test_a_deflection_is_refused_for_a_private_chat() -> None:
    """The sibling of the group refusal. A person who asked privately gets their answer, and a
    deflection built for them is fixed words in place of one."""
    with pytest.raises(TelegramRefusedError, match="where an answer belongs"):
        group_deflection(_message())


def test_a_supergroup_and_a_broadcast_are_not_one_person() -> None:
    """Telegram migrates a group to a supergroup on its own, so the two arrive with different
    `type` strings for what a reader would call one chat. Listing them separately is what stops
    a migration quietly turning a room into a private chat."""
    assert audience_is_one_person(ChatKind.PRIVATE) is True
    assert audience_is_one_person(ChatKind.GROUP) is False
    assert audience_is_one_person(ChatKind.SUPERGROUP) is False
    assert audience_is_one_person(ChatKind.BROADCAST) is False


def test_a_message_from_another_channel_is_not_answered_over_telegram() -> None:
    """The reply belongs on the surface the question came from. Delete this and a Lark question
    can be answered into a Telegram chat, which is a different audience."""
    foreign = TelegramMessage(
        event=ChannelEvent(
            channel=Channel.LARK,
            external_id="om_1",
            channel_identity="ou_abc",
            text="hello",
            received_at=NOW,
        ),
        chat_id=SENDER_ID,
        chat_kind=ChatKind.PRIVATE,
    )

    with pytest.raises(TelegramRefusedError, match="surface the question came from"):
        reply_privately(foreign, _payload())


# ------------------------------------------------- the chat id, used once
def test_an_answer_cannot_be_delivered_to_a_chat_it_was_not_planned_for() -> None:
    """Without the check the chat id is simply a second argument, and the mistake that puts one
    person's answer somewhere else is a variable name.

    **The same comparison does two jobs here.** The digest is of the sender's user id, and in a
    private chat Telegram's chat id is that same number, so an answer can only be delivered to
    the private chat of the person it was computed for. A group's chat id is a different number
    and cannot match, which is why the group-disclosure failure is refused by the check that
    refuses sending one person's answer to another.

    Delete this and `deliver` becomes a two-argument function whose arguments are not required
    to agree."""
    adapter = TelegramAdapter()
    answer = reply_privately(_message(), _payload())

    with pytest.raises(TelegramRefusedError, match="planned for somewhere else"):
        deliver(adapter, answer, to_chat_id=OTHER_SENDER_ID)

    with pytest.raises(TelegramRefusedError, match="planned for somewhere else"):
        deliver(adapter, answer, to_chat_id=GROUP_ID)

    assert adapter.sent == [], "nothing reaches the wire when the destination disagrees"


def test_the_refusal_names_neither_the_chat_id_nor_the_digest_it_expected() -> None:
    """Both reach a log from here, and the pair of them is the directory of Telegram accounts
    joined to what each person asked. Delete this and a diagnostic improvement puts the chat id
    in the message."""
    answer = reply_privately(_message(), _payload())

    with pytest.raises(TelegramRefusedError) as caught:
        deliver(TelegramAdapter(), answer, to_chat_id=OTHER_SENDER_ID)

    assert str(OTHER_SENDER_ID) not in str(caught.value)
    assert DIGEST not in str(caught.value)


def test_a_delivered_message_records_the_digest_and_never_the_chat_id() -> None:
    """The positive case, and the one showing the chat id is used once and not kept. A list of
    Telegram ids beside the answers they received is the directory `gate.ingress.Binding`
    refuses to be."""
    adapter = TelegramAdapter()
    answer = reply_privately(_message(), _payload())

    deliver(adapter, answer, to_chat_id=SENDER_ID)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].to_identity == DIGEST
    assert str(SENDER_ID) not in adapter.sent[0].to_identity
    assert "INV-1" in adapter.sent[0].body


def test_a_deflection_reaches_the_group_it_was_planned_for_and_carries_nothing() -> None:
    """The positive case for the other plan. The group gets fixed words and no payload at all,
    which is the whole of what a chat with unknown membership may be told."""
    adapter = TelegramAdapter()
    notice = group_deflection(_message(chat_id=GROUP_ID, chat_type="supergroup"))

    deliver(adapter, notice, to_chat_id=GROUP_ID)

    assert adapter.sent == [type(adapter.sent[0])(to_identity=GROUP_DIGEST, body=GROUP_DEFLECTION)]


# ------------------------------------------------- the unrecognised sender
def test_an_unrecognised_sender_is_told_the_words_the_gate_already_wrote() -> None:
    """This module defines no prompt of its own. `gate.ingress.UNRECOGNISED_PROMPT` answers an
    unknown account, a known but unbound one, and one whose binding was revoked this morning
    with the same words, and a second prompt written here would be a second thing to get wrong
    in the direction that confirms an account belongs to somebody."""
    reach = Unrecognised(channel=Channel.TELEGRAM)

    answer = unrecognised_reply(reach, _message())

    assert answer.body == reach.prompt
    assert answer.to_identity == DIGEST
    assert answer.payload == ChannelPayload()


def test_the_unrecognised_prompt_is_never_posted_into_a_group() -> None:
    """**The most interesting refusal in the file.** The prompt is carefully written not to
    confirm to the person holding the handset whether their account is bound. Posting it into a
    group announces exactly that to every member, about somebody who did nothing but ask a
    question in front of their colleagues.

    Delete this and the unbound case in a group gets the helpful reply, which is the leak the
    whole prompt exists to avoid, delivered to an audience instead of to one person."""
    message = _message(chat_id=GROUP_ID, chat_type="group")

    with pytest.raises(TelegramRefusedError, match="whether this person is bound"):
        unrecognised_reply(Unrecognised(channel=Channel.TELEGRAM), message)


def test_a_reach_built_for_another_channel_is_not_sent_over_telegram() -> None:
    """The prompt a person is given is per channel: the widget's differs and argues at length
    why, and none of that argument transfers to a Telegram account."""
    with pytest.raises(TelegramRefusedError, match="per channel"):
        unrecognised_reply(Unrecognised(channel=Channel.WIDGET), _message())


# ------------------------------------------------- the surface and the ceilings
def test_this_surface_declares_no_features_at_all_and_each_absence_has_a_reason() -> None:
    """Five absences with five separate reasons, and none of them is "nobody got round to it".

    `EPHEMERAL`, because Telegram has no per-viewer message in a group; Slack has one, and that
    difference is why this is a decision rather than an oversight. `CARDS`, because this channel
    carries `read` alone, so a press could never be honoured. `STREAMING`, because streaming
    here is one edit per token against a rate limit. `ATTACHMENTS`, because this adapter has no
    path for a file in either direction. `EDIT_IN_PLACE`, which the vendor genuinely supports:
    the feature exists in this codebase to disarm a card once somebody has taken the decision it
    offers, and this surface has no cards, so declaring it would tell a caller about a path that
    is not here.

    Delete this and `EDIT_IN_PLACE` gets declared because `editMessageText` works, which is true
    and is not the question."""
    features = TelegramAdapter().capabilities().features

    assert features == TELEGRAM_FEATURES
    assert set(TELEGRAM_FEATURES) == set()
    for feature in Feature:
        assert feature not in features


def test_the_classification_ceiling_is_internal_and_not_confidential() -> None:
    """A consumer application on a personal handset, which `channels.adapter` already names as
    the class of surface a `restricted` field must not reach, and two arguments beyond that. An
    ordinary Telegram chat is not end-to-end encrypted, so the history sits in the vendor's
    cloud and is restored in full onto any device the account signs in on. And recovery runs
    through a code sent to a phone number, so a swapped SIM yields everything already said
    rather than only what is said afterwards.

    Raising this is a decision somebody makes deliberately, not one that arrives by a constant
    being edited to make a send work."""
    ceiling = TelegramAdapter().capabilities().max_classification

    assert ceiling is Classification.INTERNAL
    assert ceiling.rank < Classification.CONFIDENTIAL.rank


def test_a_telegram_binding_is_never_worth_more_than_bound() -> None:
    """The header proves the update came from something holding the secret. It says nothing at
    all about who sent the message, so no assurance may be derived from it, and a binding is
    evidence about the day it was made rather than about this request.

    Delete this and a verified webhook can be read as evidence about who is asking, which it has
    never been."""
    assert TELEGRAM_ASSURANCE_CEILING is Assurance.BOUND
    assert TELEGRAM_ASSURANCE_CEILING < Assurance.AUTHENTICATED


def test_this_channel_carries_read_and_nothing_else() -> None:
    """The declaration `gate.admission.verbs_for_channel` requires of every channel, asserted
    here as well because this is the file where the reasons for it live. A Telegram message is
    a numeric account id vouched for by a header, and an effect authorised by that is an effect
    attributable to a header.

    Delete this and the verb set can be widened in `admission.py` with nothing in the channel's
    own tests noticing."""
    assert verbs_for_channel(Channel.TELEGRAM) == frozenset({"read"})
    assert traffic_class_for(Channel.TELEGRAM) is TrafficClass.HUMAN_INTERACTIVE


def test_a_body_that_dropped_the_payload_label_does_not_reach_the_wire() -> None:
    """`cards.assert_label_survives` is reused rather than restated, so this adapter and every
    other cannot disagree about labels. The check is on the produced string, which is the
    question `adapter.assert_can_send` cannot ask.

    Delete this and a caller can hand over a body it composed itself that quietly dropped
    "here is something nobody checked"."""
    from brain.channels.cards import CardRefusedError
    from brain.core.redaction import OPAQUE_LABEL

    adapter = TelegramAdapter()
    payload = ChannelPayload(records=({"invoice": "INV-1"},), label=OPAQUE_LABEL)

    with pytest.raises(CardRefusedError):
        adapter.send(payload, to=str(SENDER_ID), body="Your invoice is ready.")

    assert adapter.sent == []


def test_an_adapter_that_cannot_be_reached_reads_as_unhealthy_rather_than_absent() -> None:
    """Configured-and-unreachable and never-set-up send a person to different places, which is
    what `adapter.registered` reports apart."""
    assert TelegramAdapter().healthy(NOW) is True
    assert TelegramAdapter(reachable=False).healthy(NOW) is False
