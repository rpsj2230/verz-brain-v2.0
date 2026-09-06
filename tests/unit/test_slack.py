"""Slack: a captured request, a name somebody chose for themselves, and a room that reads.

Four claims carry this file.

**Freshness and authenticity are two checks, and each is held while the other passes.** A
correctly signed request outside the replay window is refused, and a live request with a
wrong signature is refused. A suite that only ever fails both at once cannot say which one is
doing the work, and the cheap implementation that verifies the signature and calls it a day
passes every test where the timestamp is wrong too.

**The signature covers the timestamp, which is what makes the window mean anything.** Signing
only the body would leave a captured signature replayable for ever with a freshened
timestamp, and the window would be a check on a value nobody signed.

**The sender is the Slack user id.** The payload here carries a `user_name` naming somebody
else, because that is the shape of the attack: a person renames themselves after a colleague
and the convenient parser keys on the name.

**A body computed at one person's reach has one reader.** Slack is the first surface with a
per-viewer mechanism that genuinely exists, so the tests below hold the two halves apart: the
conversation reads the floor, and the asker sees more only through `chat.postEphemeral`.

Task ids: M10.5.1
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels import slack as slack_module
from brain.channels.adapter import ChannelCapabilities, Feature
from brain.channels.room import Degradation, Member, floor
from brain.channels.slack import (
    NOT_ACCEPTED,
    SLACK_FEATURES,
    SLACK_REPLAY_WINDOW,
    Posting,
    Rendered,
    SlackAdapter,
    SlackRefusedError,
    Surface,
    Visibility,
    assert_the_conversation_only_carries_the_floor,
    audience_is_one_person,
    deliver,
    normalise_message,
    plan_reply,
    sign,
    unrecognised_reply,
    verify,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload, assert_channel_adapter
from brain.core.scope import Scope
from brain.gate.admission import CHANNEL_VERBS
from brain.gate.context import Channel
from brain.gate.ingress import Unrecognised, identity_hash

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
STAMP = str(int(NOW.timestamp()))
SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BODY = b'{"type":"event_callback","event":{"type":"message"}}'

USER = "U0ABCDEFG"
DIGEST = identity_hash(Channel.SLACK, USER)
OTHER_USER = "U0ZZZZZZZ"
ROOM = "C0FINANCE"
DM = "D0PRIVATE"

READ_NAME = "read:client.name"
READ_MARGIN = "read:client.margin"


def _ents(*capabilities: str, principal_id: str = "u_asker") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _member(principal_id: str, *capabilities: str) -> Member:
    return Member(
        principal_id=principal_id, entitlement=_ents(*capabilities, principal_id=principal_id)
    )


def _caps(*features: Feature) -> ChannelCapabilities:
    return ChannelCapabilities(
        channel=Channel.SLACK,
        features=frozenset(features),
        max_classification=Classification.INTERNAL,
        can_carry_label=True,
    )


def _payload(**fields: object) -> ChannelPayload:
    return ChannelPayload(records=({"client": "Acme", **fields},))


def _rendered(entitlement: EntitlementSet, **fields: object) -> Rendered:
    return Rendered(payload=_payload(**fields), ent_hash=entitlement.ent_hash())


def _raw(**overrides: object) -> dict[str, object]:
    """The envelope the Events API posts, with one message in it.

    `user_name` is present in every one of these on purpose. It is what an older payload
    carries beside `user`, and every test that reads a sender is therefore also a test that
    the name was not the thing read.
    """
    event: dict[str, object] = {
        "type": "message",
        "channel": ROOM,
        "channel_type": "channel",
        "user": USER,
        "user_name": "wei.ling",
        "text": "what is the margin",
        "ts": "1757160000.000100",
    }
    event.update(overrides)
    return {"type": "event_callback", "team_id": "T0WORKSPACE", "event": event}


def _function(name: str) -> ast.FunctionDef:
    """One function of `brain.channels.slack`, parsed.

    Read as a syntax tree rather than as text, for the reason `tests/unit/test_email.py`
    records at length: a source search is satisfied by the module's own prose, and this
    module's docstring names `hmac.compare_digest` while explaining why it is there.
    """
    tree = ast.parse(inspect.getsource(slack_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"brain.channels.slack has no function named {name}")


# ------------------------------------------------- the request is signed and it is live
def test_a_signed_request_inside_the_window_is_accepted() -> None:
    """The positive case. A verifier that refused everything would satisfy every refusal in
    this file and mean no Slack event ever reaches the gate, which is a channel that looks
    built and answers nothing."""
    verify(
        signing_secret=SECRET,
        signature=sign(SECRET, STAMP, BODY),
        timestamp=STAMP,
        body=BODY,
        now=NOW,
    )


def test_a_correctly_signed_request_from_last_month_is_still_refused() -> None:
    """**The freshness half, held while the authenticity half passes.** The signature here is
    genuine: it is what Slack sent, and it verifies. A signature says the bytes came from
    Slack and never says when, so a request lifted out of a proxy log verifies exactly as well
    as a live one.

    Delete this and the window can be dropped as redundant, because every other signature test
    still passes, and a captured approval stays replayable for ever."""
    stale = str(int((NOW - timedelta(days=30)).timestamp()))

    with pytest.raises(SlackRefusedError):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, stale, BODY),
            timestamp=stale,
            body=BODY,
            now=NOW,
        )


def test_a_request_from_the_future_is_refused_exactly_as_one_from_the_past() -> None:
    """Checking only the past is the mistake that reads as thorough. A sender with a fast
    clock, or an attacker choosing the timestamp they sign, would be accepted for as long as
    they liked, and the window would bound nothing at all in the direction that matters."""
    ahead = str(int((NOW + SLACK_REPLAY_WINDOW + timedelta(seconds=1)).timestamp()))

    with pytest.raises(SlackRefusedError):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, ahead, BODY),
            timestamp=ahead,
            body=BODY,
            now=NOW,
        )


def test_the_replay_window_is_the_vendors_five_minutes_and_not_a_moment_more() -> None:
    """The boundary, where an off-by-one is either a live request refused or a captured one
    admitted. Delete this and the comparison can drift to `>=` or the window to an hour, and
    nothing anywhere would say the bound had moved."""
    assert SLACK_REPLAY_WINDOW.total_seconds() == 5 * 60, "the vendor's own five minutes"
    edge = str(int((NOW - SLACK_REPLAY_WINDOW).timestamp()))
    verify(
        signing_secret=SECRET,
        signature=sign(SECRET, edge, BODY),
        timestamp=edge,
        body=BODY,
        now=NOW,
    )

    past = str(int((NOW - SLACK_REPLAY_WINDOW - timedelta(seconds=1)).timestamp()))
    with pytest.raises(SlackRefusedError):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, past, BODY),
            timestamp=past,
            body=BODY,
            now=NOW,
        )


def test_a_body_nobody_signed_is_refused_while_the_clock_is_perfectly_good() -> None:
    """**The authenticity half, held while the freshness half passes.** The timestamp is now,
    so the window is satisfied and the only thing left standing between a forged body and the
    gate is the digest.

    Delete this and the signature comparison can be dropped while every timestamp test stays
    green, and a forged body is a forged identity: everything downstream starts from the
    sender in it."""
    forged = b'{"type":"event_callback","event":{"type":"message","user":"U0SOMEBODYELSE"}}'

    with pytest.raises(SlackRefusedError):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, STAMP, BODY),
            timestamp=STAMP,
            body=forged,
            now=NOW,
        )


def test_the_signature_covers_the_timestamp_so_a_captured_one_cannot_be_freshened() -> None:
    """Without the timestamp inside the signed material the window checks a value nobody
    signed, and the replay is: take yesterday's captured body and signature, put today's
    timestamp on it, and the request is live and authentic at once.

    Asserted in both directions, because one alone is weak. The two signatures differ, and the
    old signature does not verify against the new timestamp. Delete this and `signing_material`
    can be reduced to the body, which reads as a simplification and removes the window's
    entire meaning."""
    yesterday = str(int((NOW - timedelta(days=1)).timestamp()))
    captured = sign(SECRET, yesterday, BODY)

    assert captured != sign(SECRET, STAMP, BODY)

    with pytest.raises(SlackRefusedError):
        verify(signing_secret=SECRET, signature=captured, timestamp=STAMP, body=BODY, now=NOW)


def test_the_signature_is_compared_in_constant_time() -> None:
    """`==` on a hex digest returns as soon as two characters differ, so how long it takes
    says how much of a guess was right, and a few thousand requests turn that into a
    signature.

    Asserted over the syntax tree rather than over the source text, because this module's own
    docstring names the function while explaining why it is there, and a text search would be
    satisfied by the prose with the code wrong.

    Delete this and `hmac.compare_digest(expected, signature)` can become
    `expected == signature`, which is shorter, reads identically, and passes every behavioural
    test in this file."""
    verifier = _function("verify")

    constant_time = [
        node
        for node in ast.walk(verifier)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
        and any(isinstance(arg, ast.Name) and arg.id == "signature" for arg in node.args)
    ]
    assert constant_time, "the signature has to go through hmac.compare_digest"

    ordinary = [
        node
        for node in ast.walk(verifier)
        if isinstance(node, ast.Compare)
        and any(isinstance(inner, ast.Name) and inner.id == "signature" for inner in ast.walk(node))
    ]
    assert ordinary == [], "the signature is never compared with an ordinary operator"


def test_the_parsed_object_is_not_the_thing_that_was_signed() -> None:
    """JSON has no canonical form, so verifying a re-serialisation verifies something the
    sender never signed: an attacker who can make the parse and the round trip differ has a
    body that verifies as one thing and is read as another.

    Delete this and an overload taking a mapping can be added for the convenience of a caller
    that has already parsed the body, which is every web framework."""
    with pytest.raises(TypeError, match="raw bytes"):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, STAMP, BODY),
            timestamp=STAMP,
            body=BODY.decode(),  # type: ignore[arg-type]
            now=NOW,
        )


def test_a_signature_header_with_non_ascii_is_refused_rather_than_raising() -> None:
    """`hmac.compare_digest` raises `TypeError` on a non-ASCII string rather than returning
    False, and the header is whatever the sender typed. Without the guard one accented
    character turns a refusal into an unhandled exception, which arrives as a 500 and reads to
    an operator as the integration being broken rather than as somebody probing it."""
    with pytest.raises(SlackRefusedError) as caught:
        verify(
            signing_secret=SECRET,
            signature="v0=deadbeefé",
            timestamp=STAMP,
            body=BODY,
            now=NOW,
        )

    assert str(caught.value) == NOT_ACCEPTED


def test_every_refusal_a_sender_can_cause_says_the_same_thing() -> None:
    """ "stale timestamp" and "bad signature" tell somebody probing which part to change next,
    and the difference between the two answers is the whole map of what to try. This is
    `channels.webhook`'s rule and it does not become optional for being made twice.

    Delete this and a diagnostic improvement gives each check its own message, which reads as
    a kindness to an operator and is a kindness to an attacker."""
    unparseable = "not-a-timestamp"
    long_ago = str(int((NOW - timedelta(days=30)).timestamp()))
    messages = set()

    for signature, timestamp in (
        (sign(SECRET, STAMP, BODY) + "0", STAMP),
        (sign(SECRET, long_ago, BODY), long_ago),
        (sign(SECRET, unparseable, BODY), unparseable),
        ("v0=é", STAMP),
    ):
        with pytest.raises(SlackRefusedError) as caught:
            verify(
                signing_secret=SECRET,
                signature=signature,
                timestamp=timestamp,
                body=BODY,
                now=NOW,
            )
        messages.add(str(caught.value))

    assert messages == {NOT_ACCEPTED}


def test_a_naive_clock_is_refused_rather_than_shifting_the_window_by_the_servers_offset() -> None:
    """Not a sender's doing, so it gets its own message. A caller passing `datetime.utcnow()`
    on a machine that is not set to UTC gets a window silently offset by hours, which presents
    as every Slack request being refused and looks like the integration being down.

    Delete this and the comparison raises or lies depending on the server's time zone, and the
    symptom points at Slack."""
    with pytest.raises(SlackRefusedError, match="aware clock"):
        verify(
            signing_secret=SECRET,
            signature=sign(SECRET, STAMP, BODY),
            timestamp=STAMP,
            body=BODY,
            now=NOW.replace(tzinfo=None),
        )


# ------------------------------------------------- the sender is an id and not a name
def test_the_sender_is_the_slack_user_id_and_never_the_name_beside_it() -> None:
    """`user_name` and `display_name` are set by the person, so somebody can rename themselves
    after a colleague and be keyed on as them. `channels.whatsapp` refuses the WhatsApp profile
    name for this reason and `channels.lark.Mention` carries no display-name field at all.

    Delete this and the friendlier field becomes the identity, and a binding lookup answers
    with somebody else's principal."""
    message = normalise_message(_raw())

    assert message.event.channel_identity == USER
    assert message.sender_identity == DIGEST
    assert message.event.channel is Channel.SLACK


def test_a_sender_with_a_name_and_no_id_has_nobody_it_could_be_from() -> None:
    """The half the test above cannot show. A payload carrying only the name must be refused
    rather than fall back to it, and a fallback is what somebody writes the first time a
    payload shape surprises them."""
    payload = _raw()
    event = payload["event"]
    assert isinstance(event, dict)
    del event["user"]

    with pytest.raises(SlackRefusedError, match="has no user"):
        normalise_message(payload)


def test_a_message_posted_by_an_app_is_not_answered() -> None:
    """Two bots answering each other in a channel is the loop `channels.email.is_automatic`
    refuses, and Slack is where it actually happens: a workspace is full of integrations
    posting constantly, and one of them quoting our answer is a conversation with no person
    in it.

    Delete this and a build-status app can start a thread that never ends."""
    with pytest.raises(SlackRefusedError, match="posted by an app"):
        normalise_message(_raw(bot_id="B0DEPLOYBOT"))


@pytest.mark.parametrize("subtype", ["message_changed", "channel_join", "file_share"])
def test_a_message_with_a_subtype_is_not_read_as_a_question(subtype: str) -> None:
    """An edit carries its new text under `message`, a join carries no question at all, and a
    file share carries a caption where the question would be. Reading any of them as a plain
    message puts something that is not a question through the gate, which then answers it from
    whatever the wrong field retrieves."""
    with pytest.raises(SlackRefusedError, match="subtype"):
        normalise_message(_raw(subtype=subtype))


def test_a_message_is_identified_by_its_conversation_and_its_timestamp_together() -> None:
    """Slack's `ts` is unique within a conversation and Slack promises nothing about it across
    conversations, so a dedupe key built from `ts` alone lets one message suppress another
    that happened to share it, in a different channel, from a different person.

    Delete this and the dedupe silently drops questions, which is the failure nobody traces
    because the evidence is a message that was never processed."""
    here = normalise_message(_raw())
    there = normalise_message(_raw(channel="C0OTHER"))

    assert here.event.external_id == f"{ROOM}:1757160000.000100"
    assert here.event.dedupe_key != there.event.dedupe_key


def test_an_unknown_conversation_kind_is_refused_rather_than_guessed() -> None:
    """How many people read this decides what may be said in it, and neither default is safe:
    treating an unknown kind as direct answers a room at one person's reach, and treating it as
    a channel computes a floor over a conversation with one person in it."""
    with pytest.raises(SlackRefusedError, match="conversation kind"):
        normalise_message(_raw(channel_type="huddle"))


def test_a_challenge_is_not_a_message() -> None:
    """`url_verification` arrives at the same endpoint with a valid signature and no event in
    it. Reading it as a message would put an empty question through the gate, and refusing it
    here means the caller has to answer the challenge deliberately."""
    with pytest.raises(SlackRefusedError, match="event_callback"):
        normalise_message({"type": "url_verification", "challenge": "abc"})


def test_a_group_direct_message_is_a_room_however_slack_names_it() -> None:
    """The member most likely to be got wrong. Slack calls `mpim` a direct message and it has
    three to nine people in it, so answering it at the asker's reach puts one person's answer
    in front of the others.

    Delete this and `mpim` can be folded in with `im` because both are called direct
    messages, which is true of the name and false of the audience."""
    assert audience_is_one_person(Surface.DIRECT) is True
    assert audience_is_one_person(Surface.GROUP_DIRECT) is False
    assert audience_is_one_person(Surface.PUBLIC) is False
    assert audience_is_one_person(Surface.PRIVATE) is False


# ------------------------------------------------- a body has one audience
def test_a_body_computed_for_one_person_cannot_be_posted_plainly_into_a_channel() -> None:
    """**The module's subject in one construction.** A direct posting is a body computed at
    one person's reach, and aiming it at a conversation other people are in is the disclosure:
    the message looks like every other message and is read by everybody.

    Delete this and the surface becomes an argument nobody checks, and the mistake is a
    variable holding the wrong conversation id."""
    with pytest.raises(SlackRefusedError, match="read by everybody in it"):
        Posting(
            conversation=ROOM,
            surface=Surface.PUBLIC,
            visibility=Visibility.DIRECT,
            payload=_payload(),
            ent_hash=_ents(READ_MARGIN).ent_hash(),
            degradation=Degradation.FULL,
            to_identity=DIGEST,
        )


def test_a_posting_the_whole_conversation_reads_cannot_name_a_viewer() -> None:
    """A contradiction that resolves the wrong way on every surface: the address is advisory
    and the posting is public, so it reads as private and is read by the room.

    Delete this and a caller can set a viewer on a channel posting and believe it means
    something."""
    with pytest.raises(SlackRefusedError, match="names a viewer"):
        Posting(
            conversation=ROOM,
            surface=Surface.PUBLIC,
            visibility=Visibility.CHANNEL,
            payload=_payload(),
            ent_hash=_ents(READ_NAME).ent_hash(),
            degradation=Degradation.FULL,
            to_identity=DIGEST,
        )


def test_a_per_viewer_posting_names_its_reader_by_digest_and_never_by_user_id() -> None:
    """A raw `U0…` on a posting is one interpolation away from being in a message body and one
    copy away from a table of them, which is the staff directory `gate.ingress.Binding`
    declines to keep.

    Delete this and the planner can pass the id straight through, and the digest becomes
    decoration."""
    with pytest.raises(SlackRefusedError, match="as its reader"):
        Posting(
            conversation=ROOM,
            surface=Surface.PUBLIC,
            visibility=Visibility.EPHEMERAL,
            payload=_payload(),
            ent_hash=_ents(READ_MARGIN).ent_hash(),
            degradation=Degradation.FULL,
            to_identity=USER,
        )


def test_everything_the_conversation_reads_is_computed_at_the_floor() -> None:
    """The asker's reach decides what the asker is shown privately and never what a colleague
    reads over their shoulder. Asserted against the sweep directly as well as through the
    planner, because whatever wires this to the SDK builds postings without going through the
    planner and needs the same check.

    Delete this and a room posting built from the asker's payload goes out looking exactly
    like one built from the floor."""
    asker_reach = _ents(READ_NAME, READ_MARGIN).ent_hash()
    posting = Posting(
        conversation=ROOM,
        surface=Surface.PUBLIC,
        visibility=Visibility.CHANNEL,
        payload=_payload(margin="41%"),
        ent_hash=asker_reach,
        degradation=Degradation.FLOOR_ONLY,
    )

    with pytest.raises(SlackRefusedError, match="floor"):
        assert_the_conversation_only_carries_the_floor((posting,), _ents(READ_NAME).ent_hash())

    assert_the_conversation_only_carries_the_floor((posting,), asker_reach)


def test_the_planner_refuses_a_conversation_body_computed_at_the_askers_reach() -> None:
    """The same rule where the mistake is actually made. The two postings are built a few
    lines apart and the wrong one is a variable name, so the check is on what is about to be
    sent rather than on what the caller passed in.

    Delete this and the sweep is only exercised by hand-built postings, which is not the path
    that ships."""
    members = [_member("u_asker", READ_NAME, READ_MARGIN), _member("u_other", READ_NAME)]
    asker = _ents(READ_NAME, READ_MARGIN, principal_id="u_asker")

    with pytest.raises(SlackRefusedError, match="floor"):
        plan_reply(
            normalise_message(_raw()),
            members=members,
            asker_id="u_asker",
            capabilities=_caps(Feature.EPHEMERAL),
            conversation_body=_rendered(asker, margin="41%"),
            asker_body=_rendered(asker, margin="41%"),
            now=NOW,
        )


def test_the_asker_sees_more_than_the_room_only_through_the_ephemeral_mechanism() -> None:
    """**The positive case, and the one the whole audience argument is for.** The conversation
    gets the floor and the asker gets their own view privately, in the same conversation, with
    nobody else able to read it.

    Delete this and every remaining audience test is a refusal, which is satisfied by a planner
    that posts nothing at all."""
    members = [_member("u_asker", READ_NAME, READ_MARGIN), _member("u_other", READ_NAME)]
    asker = _ents(READ_NAME, READ_MARGIN, principal_id="u_asker")
    room_reach = floor(members)

    plan = plan_reply(
        normalise_message(_raw()),
        members=members,
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        conversation_body=Rendered(payload=_payload(), ent_hash=room_reach.ent_hash()),
        asker_body=_rendered(asker, margin="41%"),
        now=NOW,
    )

    assert plan.degradation is Degradation.EPHEMERAL_ASIDE
    assert [p.visibility for p in plan.postings] == [Visibility.CHANNEL, Visibility.EPHEMERAL]
    assert plan.postings[0].to_identity == ""
    assert plan.postings[0].ent_hash == room_reach.ent_hash()
    assert plan.postings[1].to_identity == DIGEST
    assert plan.postings[1].ent_hash == asker.ent_hash()


def test_an_installation_without_ephemeral_gives_the_asker_what_the_room_gets() -> None:
    """A Slack app is installed with a set of scopes, so an installation that cannot post
    ephemerally is a real configuration rather than a hypothetical one. The honest outcome is
    that the asker sees the floor, recorded as a degradation so somebody can answer why the
    same question was answered differently in two places.

    Delete this and the aside can be planned regardless and refused at the wire, which is a
    private body one retry away from being posted publicly."""
    members = [_member("u_asker", READ_NAME, READ_MARGIN), _member("u_other", READ_NAME)]
    asker = _ents(READ_NAME, READ_MARGIN, principal_id="u_asker")
    room_reach = floor(members)

    plan = plan_reply(
        normalise_message(_raw()),
        members=members,
        asker_id="u_asker",
        capabilities=_caps(),
        conversation_body=Rendered(payload=_payload(), ent_hash=room_reach.ent_hash()),
        asker_body=_rendered(asker, margin="41%"),
        now=NOW,
    )

    assert plan.degradation is Degradation.FLOOR_ONLY
    assert [p.visibility for p in plan.postings] == [Visibility.CHANNEL]


def test_a_direct_conversation_answers_the_one_person_in_it_at_their_own_reach() -> None:
    """The positive case on the other path. A one-to-one conversation has exactly one reader
    and the answer is theirs, so there is no floor to fall to and nothing to hold back."""
    asker = _ents(READ_NAME, READ_MARGIN, principal_id="u_asker")

    plan = plan_reply(
        normalise_message(_raw(channel=DM, channel_type="im")),
        members=[_member("u_asker", READ_NAME, READ_MARGIN)],
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        conversation_body=_rendered(asker),
        asker_body=_rendered(asker, margin="41%"),
        now=NOW,
    )

    assert plan.degradation is Degradation.FULL
    assert [p.visibility for p in plan.postings] == [Visibility.DIRECT]
    assert plan.postings[0].to_identity == DIGEST
    assert plan.postings[0].ent_hash == asker.ent_hash()


def test_a_direct_conversation_carrying_anybody_but_the_asker_is_a_room() -> None:
    """Two descriptions of the same conversation disagreeing, and picking one is a guess. The
    guess that reads as harmless answers at one person's reach in front of the others.

    Delete this and a mislabelled `im` becomes a way to get a private answer posted to a
    group."""
    with pytest.raises(SlackRefusedError, match="is a room"):
        plan_reply(
            normalise_message(_raw(channel=DM, channel_type="im")),
            members=[_member("u_asker", READ_NAME), _member("u_other", READ_NAME)],
            asker_id="u_asker",
            capabilities=_caps(Feature.EPHEMERAL),
            conversation_body=_rendered(_ents(READ_NAME, principal_id="u_asker")),
            asker_body=_rendered(_ents(READ_NAME, principal_id="u_asker")),
            now=NOW,
        )


def test_nothing_may_be_said_and_nowhere_to_send_them_is_refused_rather_than_silent() -> None:
    """A question with no answer and no route is silence, which is not one of the outcomes:
    somebody is left waiting on a reply that is never coming and nothing anywhere says why.

    Delete this and the planner returns an empty plan that reads as success."""
    members = [_member("u_asker", READ_NAME), _member("u_other", READ_MARGIN)]

    with pytest.raises(SlackRefusedError, match="silence"):
        plan_reply(
            normalise_message(_raw()),
            members=members,
            asker_id="u_asker",
            capabilities=_caps(),
            conversation_body=Rendered(
                payload=ChannelPayload(), ent_hash=floor(members).ent_hash()
            ),
            asker_body=_rendered(_ents(READ_NAME, principal_id="u_asker")),
            now=NOW,
        )


# ------------------------------------------------- the unrecognised sender
def test_an_unrecognised_sender_in_a_channel_is_told_privately() -> None:
    """**A decision this surface makes and no other one has had to.**
    `gate.ingress.UNRECOGNISED_PROMPT` is careful never to confirm whether an identity belongs
    to somebody, because the attacker it is written against is holding a stolen handset. None
    of that argument covers announcing to a room that a named colleague's account is not set up
    here, which is exactly what a public reply to their message does.

    Delete this and the prompt goes out where the channel reads it, which is a fact about a
    colleague published to everybody who happens to be in the room."""
    message = normalise_message(_raw())

    posting = unrecognised_reply(
        Unrecognised(channel=Channel.SLACK), message, capabilities=_caps(Feature.EPHEMERAL)
    )

    assert posting.visibility is Visibility.EPHEMERAL
    assert posting.to_identity == DIGEST
    assert posting.body == Unrecognised(channel=Channel.SLACK).prompt


def test_a_workspace_that_cannot_post_privately_is_refused_rather_than_told_in_public() -> None:
    """The fallback that is not available. Opening a direct conversation to say it privately is
    a write this adapter does not do, and doing it would make the answer to an unrecognised
    sender a side effect.

    Delete this and an installation without the scope degrades to a public reply, which is the
    disclosure the ephemeral path exists to avoid."""
    message = normalise_message(_raw())

    with pytest.raises(SlackRefusedError, match="cannot post ephemerally"):
        unrecognised_reply(Unrecognised(channel=Channel.SLACK), message, capabilities=_caps())


def test_an_unrecognised_sender_in_a_direct_conversation_gets_an_ordinary_reply() -> None:
    """The positive case, and the one showing the rule above is about audience rather than
    about the prompt. There is nobody else in a one-to-one conversation to learn anything, so
    an ephemeral message there would be a mechanism with nothing to hide from."""
    message = normalise_message(_raw(channel=DM, channel_type="im"))

    posting = unrecognised_reply(Unrecognised(channel=Channel.SLACK), message, capabilities=_caps())

    assert posting.visibility is Visibility.DIRECT
    assert posting.body == Unrecognised(channel=Channel.SLACK).prompt


def test_this_module_writes_no_prompt_of_its_own() -> None:
    """`UNRECOGNISED_PROMPT` answers an unknown identity, a known but unbound one, and one
    revoked this morning with the same words. A second prompt written here is a second thing to
    get wrong in the direction that confirms an account belongs to somebody.

    Delete this and a friendlier Slack-flavoured sentence appears, and `LEAKING_PATTERNS` only
    checks the ones that go through `Unrecognised`."""
    reach = Unrecognised(channel=Channel.SLACK)
    posting = unrecognised_reply(
        reach, normalise_message(_raw(channel=DM, channel_type="im")), capabilities=_caps()
    )

    assert posting.body == reach.prompt
    assert posting.payload == ChannelPayload()


def test_a_reach_built_for_another_channel_is_not_posted_to_slack() -> None:
    """The prompt a person is given is per channel: the widget's differs and argues at length
    why, and none of that argument transfers to a workspace account."""
    with pytest.raises(SlackRefusedError, match="per channel"):
        unrecognised_reply(
            Unrecognised(channel=Channel.WIDGET),
            normalise_message(_raw()),
            capabilities=_caps(Feature.EPHEMERAL),
        )


# ------------------------------------------------- the surface and its ceiling
def test_this_surface_declares_no_cards_because_a_button_in_a_channel_is_the_channels() -> None:
    """`gate.admission.CHANNEL_VERBS` gives Slack `read` alone, so a press could never be
    honoured as an approval, and the person who pressed it would reasonably believe they had
    approved something. Block Kit makes this worth saying rather than assuming: a button on a
    message in a channel is pressable by everybody who can see the message, so a card posted
    into a room hands the room the control.

    Delete this and CARDS gets added because Slack obviously supports buttons, which is true
    and is not the question."""
    assert CHANNEL_VERBS[Channel.SLACK] == frozenset({"read"})
    assert Feature.CARDS not in SLACK_FEATURES
    assert SlackAdapter().capabilities().features == SLACK_FEATURES


def test_the_ephemeral_feature_is_declared_because_slack_genuinely_has_one() -> None:
    """The other half, and the one the audience design depends on. WhatsApp declares no
    ephemeral because a thread there has one reader; email declares none because a message has
    one copy per recipient. Slack has `chat.postEphemeral`, so the asker can be told more than
    the room without the floor moving.

    Delete this and the feature can be dropped as unused, and `channels.room.plan` silently
    stops offering an aside on the one surface that can deliver one."""
    assert Feature.EPHEMERAL in SLACK_FEATURES
    assert SlackAdapter().capabilities().supports(Feature.EPHEMERAL)


def test_the_classification_ceiling_is_internal_and_not_confidential() -> None:
    """Lark carries `CONFIDENTIAL` because it is the tenant identity provider's own client. A
    Slack workspace keeps its membership beside the directory and carries guests and Slack
    Connect members from other companies, and joining a public channel hands the joiner every
    message ever posted in it, so the audience for a message grows after it is sent and no
    floor computed now bounds it.

    Delete this and the ceiling gets raised to match Lark on the strength of both being chat
    behind a company login, which is the one thing they have in common."""
    assert SlackAdapter().capabilities().max_classification is Classification.INTERNAL


def test_the_adapter_cannot_be_handed_anything_but_a_payload_and_some_scalars() -> None:
    """`redaction.assert_channel_adapter` reads the signature: an adapter whose parameters are
    a `ChannelPayload` and scalars cannot serialise unredacted data, because it was never
    handed any. `deliver` exists to keep `send` that shape, so a `Posting` never appears in
    it."""
    assert_channel_adapter(SlackAdapter().send)


# ------------------------------------------------- the wire
def test_a_posting_cannot_be_delivered_to_a_reader_it_was_not_planned_for() -> None:
    """Without the check the user id is simply a second argument, and the mistake that shows
    one person's answer to another is a variable name.

    Delete this and `deliver` becomes a two-argument function whose arguments are not required
    to agree."""
    adapter = SlackAdapter()
    posting = Posting(
        conversation=ROOM,
        surface=Surface.PUBLIC,
        visibility=Visibility.EPHEMERAL,
        payload=_payload(),
        ent_hash=_ents(READ_NAME).ent_hash(),
        degradation=Degradation.EPHEMERAL_ASIDE,
        to_identity=DIGEST,
    )

    with pytest.raises(SlackRefusedError, match="planned for somebody else"):
        deliver(adapter, posting, to_user=OTHER_USER)

    assert adapter.sent == [], "nothing reaches the wire when the reader disagrees"


def test_the_refusal_names_neither_the_user_id_nor_the_digest_it_expected() -> None:
    """Both reach a log from here, and the pair of them is the directory joined to what each
    person was shown. Delete this and a diagnostic improvement puts the id in the message."""
    posting = Posting(
        conversation=ROOM,
        surface=Surface.PUBLIC,
        visibility=Visibility.EPHEMERAL,
        payload=_payload(),
        ent_hash=_ents(READ_NAME).ent_hash(),
        degradation=Degradation.EPHEMERAL_ASIDE,
        to_identity=DIGEST,
    )

    with pytest.raises(SlackRefusedError) as caught:
        deliver(SlackAdapter(), posting, to_user=OTHER_USER)

    assert OTHER_USER not in str(caught.value)
    assert DIGEST not in str(caught.value)


def test_a_delivered_per_viewer_message_records_the_digest_and_never_the_user_id() -> None:
    """The positive case, and the one showing the id is used once and not kept. A list of
    workspace ids beside the answers they received is the directory joined to what each person
    asked."""
    adapter = SlackAdapter()
    posting = Posting(
        conversation=ROOM,
        surface=Surface.PUBLIC,
        visibility=Visibility.EPHEMERAL,
        payload=_payload(margin="41%"),
        ent_hash=_ents(READ_NAME, READ_MARGIN).ent_hash(),
        degradation=Degradation.EPHEMERAL_ASIDE,
        to_identity=DIGEST,
    )

    deliver(adapter, posting, to_user=USER)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].viewer == DIGEST
    assert adapter.sent[0].conversation == ROOM
    assert USER not in adapter.sent[0].viewer


def test_a_posting_the_conversation_reads_is_refused_a_user_id() -> None:
    """A caller passing one has confused a public posting with a private one, and the
    resolution that reads as harmless is to post it publicly. Delete this and the confusion is
    silent, which is the mistake that only shows up when somebody reads the channel."""
    adapter = SlackAdapter()
    posting = Posting(
        conversation=ROOM,
        surface=Surface.PUBLIC,
        visibility=Visibility.CHANNEL,
        payload=_payload(),
        ent_hash=_ents(READ_NAME).ent_hash(),
        degradation=Degradation.FLOOR_ONLY,
    )

    with pytest.raises(SlackRefusedError, match="confused"):
        deliver(adapter, posting, to_user=USER)

    assert adapter.sent == []


def test_an_ephemeral_send_is_refused_where_the_installation_cannot_do_one() -> None:
    """An ephemeral message sent through a workspace that cannot do them is a private answer
    posted into a room, which is the failure `adapter.Feature.EPHEMERAL` exists to name. The
    planner already avoids it; this is the guard for whatever calls `send` directly.

    Delete this and the only thing standing between a private body and the room is a planner a
    second caller does not have to use."""
    adapter = SlackAdapter(features=frozenset({Feature.ATTACHMENTS}))

    with pytest.raises(SlackRefusedError, match="per-viewer message"):
        adapter.send(_payload(), to=ROOM, viewer=USER, ephemeral=True)

    assert adapter.sent == []


def test_asking_for_a_per_viewer_message_without_a_reader_is_refused_not_posted() -> None:
    """The two arguments disagreeing, where the resolution that reads as safe is the public
    one and is the disclosure. A caller that meant to send privately and forgot the reader
    must not have the message posted to the room instead."""
    adapter = SlackAdapter()

    with pytest.raises(SlackRefusedError, match="disagree"):
        adapter.send(_payload(), to=ROOM, ephemeral=True)

    with pytest.raises(SlackRefusedError, match="disagree"):
        adapter.send(_payload(), to=ROOM, viewer=USER, ephemeral=False)

    assert adapter.sent == []


def test_a_body_that_dropped_the_payload_label_does_not_reach_the_wire() -> None:
    """The opaque escape hatch exists so a tool returning something the redactor cannot walk is
    not simply unusable, and the price is a label carried to the person. A caller supplying its
    own body is the way that label goes missing, which turns "nobody checked this" into "here
    is an answer"."""
    adapter = SlackAdapter()
    labelled = ChannelPayload(records=({"client": "Acme"},), label="unredacted opaque payload")

    with pytest.raises(Exception, match="drops the payload label"):
        adapter.send(labelled, to=ROOM, body="Acme")

    assert adapter.sent == []
