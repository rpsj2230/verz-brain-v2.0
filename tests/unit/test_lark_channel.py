"""Reading a Lark message, and deciding who may read the answer.

Every test here is one way an answer computed for one person ends up in front of another.
The normalisation half is about the one field a sender does not author; the delivery half is
about the room's floor being the thing a room message is built at.

Task ids: M10.2.2, M10.2.5, M10.2.6
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from brain.channels.adapter import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryRefusedError,
    Feature,
)
from brain.channels.cards import CardRefusedError, assert_label_survives
from brain.channels.lark import (
    ChatType,
    Delivery,
    LarkAdapter,
    LarkRefusedError,
    Rendered,
    Visibility,
    addressed_to,
    audience_is_one_person,
    deliver,
    normalise_message,
    plan_delivery,
    should_answer,
)
from brain.channels.room import Degradation, Member, floor
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload, assert_channel_adapter
from brain.core.scope import Scope
from brain.gate.context import Channel
from brain.gate.ingress import identity_hash

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
CREATE_TIME = str(int(NOW.timestamp() * 1000))

READ_NAME = "read:client.name"
READ_MARGIN = "read:client.margin"

BOT_OPEN_ID = "ou_brain_bot"
BOT_IDENTITY = identity_hash(Channel.LARK, BOT_OPEN_ID)
ASKER_OPEN_ID = "ou_asker"
ASKER_IDENTITY = identity_hash(Channel.LARK, ASKER_OPEN_ID)


# ------------------------------------------------------------------------- builders


def _ents(
    *capabilities: str, principal_id: str = "u_asker", not_after: datetime | None = None
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
        not_after=not_after,
    )


def _member(principal_id: str, *capabilities: str) -> Member:
    return Member(
        principal_id=principal_id, entitlement=_ents(*capabilities, principal_id=principal_id)
    )


def _caps(*features: Feature, **overrides: object) -> ChannelCapabilities:
    base: dict[str, object] = {
        "channel": Channel.LARK,
        "features": frozenset(features),
        "max_classification": Classification.CONFIDENTIAL,
        "can_carry_label": True,
    }
    base.update(overrides)
    return ChannelCapabilities(**base)  # type: ignore[arg-type]


def _mention_json(
    key: str = "@_user_1", open_id: str = BOT_OPEN_ID, name: str = "Brain"
) -> dict[str, Any]:
    """A mention as Lark sends one: a placeholder key, an id block, and a display name."""
    return {"key": key, "id": {"open_id": open_id, "union_id": "on_x"}, "name": name}


def _raw(
    *,
    chat_type: str = "group",
    open_id: str = ASKER_OPEN_ID,
    mentions: Sequence[Any] = (),
    text: str = "@_user_1 what is the margin on SNM",
    message_id: str = "om_1",
    chat_id: str = "oc_1",
    message_type: str = "text",
    create_time: str = CREATE_TIME,
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "ev_1",
            "event_type": "im.message.receive_v1",
            "create_time": create_time,
        },
        "event": {
            "sender": {"sender_id": {"open_id": open_id, "user_id": "u1"}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": json.dumps({"text": text}),
                "mentions": list(mentions),
            },
        },
    }


def _payload(*names: str, label: str = "") -> ChannelPayload:
    return ChannelPayload(
        records=tuple(
            {"@entity": "client", "@id": f"c_{index}", "name": name}
            for index, name in enumerate(names)
        ),
        label=label,
    )


# ============================================================ normalisation (M10.2.2)


def test_a_lark_event_becomes_the_one_shape_the_gate_reads() -> None:
    """The positive case for normalisation, and the sibling every refusal below needs.

    A guard tested only by its refusals is satisfied by a normaliser that refuses
    everything. Deleting this leaves the whole file passing against a function that reads
    no event at all."""
    message = normalise_message(_raw(mentions=[_mention_json()]))

    assert message.event.channel is Channel.LARK
    assert message.event.external_id == "om_1"
    assert message.event.channel_identity == ASKER_OPEN_ID
    assert message.event.received_at == NOW
    assert message.chat_id == "oc_1"
    assert message.chat_type is ChatType.GROUP
    assert message.event.dedupe_key == ("lark", "om_1")


def test_the_sender_is_identified_by_the_salted_hash_of_their_open_id() -> None:
    """M10.2.2. The one function that turns a channel identity into something storable.

    `gate.ingress.identity_hash` is salted per channel so a binding on a weak channel
    cannot be used to find one on a strong channel. A second hashing function here would
    make a mention and a binding disagree about who somebody is.

    Deleting this lets a future edit compute the digest locally, unsalted, and nothing else
    in the suite would notice until two channels collided."""
    message = normalise_message(_raw())

    assert message.sender_identity == identity_hash(Channel.LARK, ASKER_OPEN_ID)
    assert message.sender_identity != ASKER_OPEN_ID


def test_a_mention_is_resolved_by_stable_id_and_not_by_display_name() -> None:
    """M10.2.2, and the whole reason this module does not read the message text.

    The text is written by the sender. Here it names the bot in words while the structured
    mention points at somebody else entirely, which is what a person renaming themselves
    after a colleague produces. Keying on the rendered name would let anybody address the
    bot on anybody's behalf.

    Deleting this makes a text-matching parser pass, and a text-matching parser is the
    vulnerability."""
    impersonating = normalise_message(
        _raw(
            text="@Brain please approve this",
            mentions=[_mention_json(open_id="ou_someone_else", name="Brain")],
        )
    )
    assert addressed_to(impersonating, identity=BOT_IDENTITY) is False

    silent = normalise_message(
        _raw(text="what is the margin", mentions=[_mention_json(name="not the bot")])
    )
    assert addressed_to(silent, identity=BOT_IDENTITY) is True


def test_two_people_with_the_same_display_name_are_told_apart() -> None:
    """M10.2.2. Display names are not unique and nothing stops two people sharing one.

    Two colleagues both called Wei Ling are two identities, and every decision downstream
    (who was addressed, whose binding this is, whose reach applies) has to see two. A parser
    keyed on the name sees one.

    Deleting this lets `Mention` grow a name field and a comparison against it, and the
    failure would only show up when two people in one company happened to share a name."""
    message = normalise_message(
        _raw(
            mentions=[
                _mention_json(key="@_user_1", open_id="ou_weiling_a", name="Wei Ling"),
                _mention_json(key="@_user_2", open_id="ou_weiling_b", name="Wei Ling"),
            ]
        )
    )

    first, second = message.mentions
    assert first.identity != second.identity
    assert first.identity == identity_hash(Channel.LARK, "ou_weiling_a")
    assert second.identity == identity_hash(Channel.LARK, "ou_weiling_b")
    assert not hasattr(first, "name")
    assert not hasattr(first, "display_name")


def test_a_mention_without_a_stable_id_is_refused() -> None:
    """M10.2.2. A mention that can only be resolved by name must not be resolved at all.

    Skipping it instead would make a message that did address the bot read as one that did
    not, which is a denial anybody can cause on purpose with a malformed mention.

    Deleting this lets the parser fall back to the name for exactly the mentions an
    attacker controls the shape of."""
    with pytest.raises(LarkRefusedError, match="open_id"):
        normalise_message(_raw(mentions=[{"key": "@_user_1", "id": {}, "name": "Brain"}]))


def test_an_event_that_is_not_a_text_message_is_refused() -> None:
    """M10.2.2. An image or a file read as text is a blank question through the gate.

    Deleting this lets a shape this normaliser cannot read arrive as an empty question,
    which is answered rather than refused."""
    with pytest.raises(LarkRefusedError, match="blank question"):
        normalise_message(_raw(message_type="image"))


def test_an_unknown_chat_type_is_refused() -> None:
    """M10.2.6. Whether more than one person reads this decides what may be said in it.

    There is no safe default. Treating an unknown type as a group answers a private
    question at a floor nobody is standing on; treating it as direct posts one person's
    answer to a room.

    Deleting this lets a new Lark chat type take whichever branch happens to be first."""
    with pytest.raises(LarkRefusedError, match="whether more than one person"):
        normalise_message(_raw(chat_type="topic_group"))


def test_a_malformed_event_is_refused_rather_than_defaulted() -> None:
    """M10.2.2. Every field this reads is required, because every default is worse.

    Deleting this lets a missing message id through, and a message with no id has no dedupe
    key, so a redelivery is answered twice."""
    broken = _raw()
    del broken["event"]["message"]["message_id"]
    with pytest.raises(LarkRefusedError, match="message_id"):
        normalise_message(broken)

    with pytest.raises(LarkRefusedError, match="not JSON"):
        normalise_message({**_raw(), "event": _content_is_not_json()})


def _content_is_not_json() -> dict[str, Any]:
    body: dict[str, Any] = _raw()["event"]
    body["message"]["content"] = "not json at all"
    return body


# ============================================================ who gets answered (M10.2.6)


def test_every_chat_type_declares_whether_one_person_reads_it() -> None:
    """M10.2.6. The declaration `audience_is_one_person` exists to force.

    `assert_never` makes a new member a type error rather than a default, and this asserts
    the two that exist actually differ: a function returning the same answer for both would
    type-check perfectly and route every group message down the direct path.

    Deleting this lets the two collapse into one answer, which is the collapse the whole
    module is arranged to prevent."""
    assert audience_is_one_person(ChatType.DIRECT) is True
    assert audience_is_one_person(ChatType.GROUP) is False


def test_a_group_message_the_bot_was_not_mentioned_in_is_not_answered() -> None:
    """M10.2.6. A bot that answers everything in a room answers colleagues' questions.

    The direct half is the sibling: a p2p message is addressed by arriving, because there
    is nobody else it could have been meant for.

    Deleting this lets the bot read every message in every group it is installed in and
    answer at a floor nobody asked it to compute."""
    unaddressed = normalise_message(_raw(mentions=[_mention_json(open_id="ou_colleague")]))
    assert should_answer(unaddressed, bot_identity=BOT_IDENTITY) is False

    addressed = normalise_message(_raw(mentions=[_mention_json()]))
    assert should_answer(addressed, bot_identity=BOT_IDENTITY) is True

    private = normalise_message(_raw(chat_type="p2p", mentions=[]))
    assert should_answer(private, bot_identity=BOT_IDENTITY) is True


# ============================================================ the room floor (M10.2.5)


def _group_setup() -> tuple[Member, Member, list[Member], EntitlementSet]:
    """A room where the asker holds a capability a colleague does not."""
    asker = _member("u_asker", READ_NAME, READ_MARGIN)
    colleague = _member("u_colleague", READ_NAME)
    members = [asker, colleague]
    return asker, colleague, members, floor(members)


def test_a_group_message_is_rendered_at_the_room_floor_and_not_at_the_askers_reach() -> None:
    """M10.4.1 through the Lark path, and the reason this module exists (M10.2.6).

    The asker holds margin and the colleague does not, so the room's floor holds name only.
    What goes into the room is the payload computed at that floor, and the assertion is on
    the reach the delivery declares rather than on the text of the body: a body that
    happened to contain no margin today would pass a text assertion tomorrow.

    Deleting this lets the asker's answer be posted into a room where a colleague who may
    not see margin reads it, and nothing about the message would look wrong."""
    asker, _, members, room_floor = _group_setup()
    message = normalise_message(_raw(mentions=[_mention_json()]))
    room_body = Rendered(payload=_payload("SNM"), ent_hash=room_floor.ent_hash())
    asker_body = Rendered(payload=_payload("SNM", "margin"), ent_hash=asker.entitlement.ent_hash())

    plan = plan_delivery(
        message,
        members=members,
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        room_body=room_body,
        asker_body=asker_body,
        now=NOW,
    )

    room = [d for d in plan.deliveries if d.visibility is Visibility.ROOM]
    assert len(room) == 1
    assert room[0].ent_hash == room_floor.ent_hash()
    assert room[0].ent_hash != asker.entitlement.ent_hash()
    assert room[0].payload is room_body.payload


def test_a_room_posting_computed_at_the_askers_reach_is_refused() -> None:
    """M10.4.1. The single enforcement point, exercised from the side that matters.

    Here the caller hands the asker's payload over as the room body and says so honestly in
    the `ent_hash` beside it. The floor this module computes for itself disagrees, and the
    plan is refused rather than sent.

    Deleting this removes the only thing standing between a wiring mistake and one person's
    permissions deciding what a colleague reads."""
    asker, _, members, room_floor = _group_setup()
    message = normalise_message(_raw(mentions=[_mention_json()]))
    asker_body = Rendered(payload=_payload("SNM", "margin"), ent_hash=asker.entitlement.ent_hash())

    with pytest.raises(LarkRefusedError, match="floor"):
        plan_delivery(
            message,
            members=members,
            asker_id="u_asker",
            capabilities=_caps(Feature.EPHEMERAL),
            room_body=asker_body,
            asker_body=asker_body,
            now=NOW,
        )
    assert room_floor.ent_hash() != asker.entitlement.ent_hash()


def test_a_per_viewer_body_is_delivered_ephemerally_rather_than_posted() -> None:
    """M10.2.5, and the direction the whole module turns on.

    The asker holds more than the floor, so they get their own body. It goes out as an
    ephemeral message addressed to them, and the room posting beside it is still the floor.
    Both halves are asserted: an implementation that posted the asker's body and skipped the
    ephemeral one would satisfy either half alone.

    Deleting this lets the aside become a second room posting, which is the private answer
    read by everybody that ephemeral messages exist to avoid."""
    asker, _, members, room_floor = _group_setup()
    message = normalise_message(_raw(mentions=[_mention_json()]))
    room_body = Rendered(payload=_payload("SNM"), ent_hash=room_floor.ent_hash())
    asker_body = Rendered(payload=_payload("SNM", "margin"), ent_hash=asker.entitlement.ent_hash())

    plan = plan_delivery(
        message,
        members=members,
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        room_body=room_body,
        asker_body=asker_body,
        now=NOW,
    )

    assert plan.degradation is Degradation.EPHEMERAL_ASIDE
    by_visibility = {d.visibility: d for d in plan.deliveries}
    assert set(by_visibility) == {Visibility.ROOM, Visibility.EPHEMERAL}
    assert by_visibility[Visibility.EPHEMERAL].payload is asker_body.payload
    assert by_visibility[Visibility.EPHEMERAL].to_identity == ASKER_IDENTITY
    assert by_visibility[Visibility.ROOM].payload is room_body.payload
    assert all(
        d.payload is not asker_body.payload
        for d in plan.deliveries
        if d.visibility is Visibility.ROOM
    )


def test_a_room_where_everyone_holds_the_same_thing_gets_one_posting() -> None:
    """The positive sibling. A floor that withholds nothing must still answer.

    Without it, every assertion above is satisfied by a planner that refuses every group
    message, which is a permission model with no product in it.

    Deleting this lets the module fail closed everywhere and look correct."""
    members = [_member("u_asker", READ_NAME), _member("u_colleague", READ_NAME)]
    room_floor = floor(members)
    message = normalise_message(_raw(mentions=[_mention_json()]))
    body = Rendered(payload=_payload("SNM"), ent_hash=room_floor.ent_hash())

    plan = plan_delivery(
        message,
        members=members,
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        room_body=body,
        asker_body=body,
        now=NOW,
    )

    assert plan.degradation is Degradation.FULL
    assert [d.visibility for d in plan.deliveries] == [Visibility.ROOM]
    assert plan.link == ""


def test_nothing_may_be_said_and_no_link_offered_is_refused() -> None:
    """M10.4.3. Silence is not one of the outcomes.

    When the floor leaves nothing to say and there is no private way to say it, the honest
    answer is a link where the gate runs again for whoever follows it. A plan with neither
    leaves somebody waiting on an answer that is never coming.

    Deleting this lets a room question end in nothing at all, which reads as the bot being
    broken and is indistinguishable from it."""
    members = [_member("u_asker"), _member("u_colleague")]
    message = normalise_message(_raw(mentions=[_mention_json()]))
    empty = Rendered(payload=ChannelPayload(), ent_hash=floor(members).ent_hash())

    with pytest.raises(LarkRefusedError, match="silence"):
        plan_delivery(
            message,
            members=members,
            asker_id="u_asker",
            capabilities=_caps(),
            room_body=empty,
            asker_body=empty,
            now=NOW,
        )


def test_a_link_only_room_is_told_nothing_and_offered_somewhere_the_gate_runs_again() -> None:
    """M10.4.3, the positive half. The link carries no answer, which is what makes it safe.

    Deleting this lets the refusal above be satisfied by refusing the link case outright,
    and a room that can never be answered is a room nobody uses."""
    members = [_member("u_asker"), _member("u_colleague")]
    message = normalise_message(_raw(mentions=[_mention_json()]))
    empty = Rendered(payload=ChannelPayload(), ent_hash=floor(members).ent_hash())

    plan = plan_delivery(
        message,
        members=members,
        asker_id="u_asker",
        capabilities=_caps(),
        room_body=empty,
        asker_body=empty,
        now=NOW,
        link="https://console.example/threads/t1",
    )

    assert plan.deliveries == ()
    assert plan.degradation is Degradation.LINK_ONLY
    assert plan.link == "https://console.example/threads/t1"


# ============================================================ direct messages (M10.2.6)


def test_a_direct_message_and_a_group_message_take_different_paths() -> None:
    """M10.2.6. Two paths, and the group one is never the wider of the two.

    The same person asks the same question in both places. Alone, they get their own reach;
    in the room, the room gets the floor. The last two assertions are the ordering itself:
    the floor is the asker's reach intersected with everybody else's, so it cannot be wider,
    and the two hashes differ so the test is not passing on them being the same thing.

    Deleting this lets the two paths converge on whichever is more convenient, and the
    convenient one is the asker's reach."""
    asker, _, members, room_floor = _group_setup()
    asker_body = Rendered(payload=_payload("SNM", "margin"), ent_hash=asker.entitlement.ent_hash())
    room_body = Rendered(payload=_payload("SNM"), ent_hash=room_floor.ent_hash())

    direct = plan_delivery(
        normalise_message(_raw(chat_type="p2p")),
        members=[asker],
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        room_body=asker_body,
        asker_body=asker_body,
        now=NOW,
    )
    group = plan_delivery(
        normalise_message(_raw(mentions=[_mention_json()])),
        members=members,
        asker_id="u_asker",
        capabilities=_caps(Feature.EPHEMERAL),
        room_body=room_body,
        asker_body=asker_body,
        now=NOW,
    )

    assert [d.visibility for d in direct.deliveries] == [Visibility.DIRECT]
    assert direct.deliveries[0].ent_hash == asker.entitlement.ent_hash()
    assert direct.deliveries[0].to_identity == ASKER_IDENTITY

    room = next(d for d in group.deliveries if d.visibility is Visibility.ROOM)
    assert room.ent_hash == room_floor.ent_hash()
    assert room.ent_hash != direct.deliveries[0].ent_hash
    assert asker.entitlement.intersect(room_floor).ent_hash() == room_floor.ent_hash()


def test_a_direct_chat_carrying_more_than_the_asker_is_refused() -> None:
    """M10.2.6. A p2p chat with anybody else in it is a group that arrived mislabelled.

    Answering it on the direct path would answer at one person's reach in front of the
    others. Promoting it to the group path was rejected: the chat type and the membership
    disagree, and picking one of them is a guess about which is the lie.

    Deleting this makes a mislabelled chat the shortest route to answering a group at the
    asker's reach, which is the exact failure the group path is built to refuse."""
    asker, _, members, _ = _group_setup()
    body = Rendered(payload=_payload("SNM"), ent_hash=asker.entitlement.ent_hash())

    with pytest.raises(LarkRefusedError, match="is a group"):
        plan_delivery(
            normalise_message(_raw(chat_type="p2p")),
            members=members,
            asker_id="u_asker",
            capabilities=_caps(Feature.EPHEMERAL),
            room_body=body,
            asker_body=body,
            now=NOW,
        )


# ============================================================ addressing and sending


def test_a_room_posting_may_not_name_a_viewer() -> None:
    """M10.2.5. A public message addressed to one person reads as private and is not.

    The address is advisory on every surface and the posting is public on all of them, so
    the two resolve the wrong way round: the writer believes one person will read it and
    everybody does.

    Deleting this lets a per-viewer body be built as a room posting with a name on it, which
    is the leak wearing the shape of the fix."""
    with pytest.raises(LarkRefusedError, match="read as private"):
        Delivery(
            chat_id="oc_1",
            visibility=Visibility.ROOM,
            payload=_payload("SNM"),
            ent_hash="x",
            degradation=Degradation.FULL,
            to_identity=ASKER_IDENTITY,
        )


def test_a_viewer_named_by_raw_channel_identity_is_refused() -> None:
    """M10.2.5. Nobody is addressed by their open id, only by the salted digest of it.

    An open id on a delivery is one interpolation away from a message body, and a store of
    them is the phone book `gate.ingress.Binding` refuses to keep.

    Deleting this lets the raw identity travel with every ephemeral message, into every log
    that records one."""
    with pytest.raises(LarkRefusedError, match="viewer"):
        Delivery(
            chat_id="oc_1",
            visibility=Visibility.EPHEMERAL,
            payload=_payload("SNM"),
            ent_hash="x",
            degradation=Degradation.EPHEMERAL_ASIDE,
            to_identity=ASKER_OPEN_ID,
        )


def test_an_ephemeral_send_to_an_installation_without_the_scope_is_refused() -> None:
    """M10.2.5. The check the adapter owes, because the adapter is what knows.

    A Lark app installed without the ephemeral scope is a real configuration. Sending a
    per-viewer body to one is a private answer posted where everybody in the chat reads it,
    so the adapter refuses rather than degrading to a public post.

    Deleting this lets the failure happen on the wire, where the only recovery is that
    somebody notices the message everybody can see."""
    adapter = LarkAdapter(features=frozenset({Feature.CARDS}))
    delivery = Delivery(
        chat_id="oc_1",
        visibility=Visibility.EPHEMERAL,
        payload=_payload("SNM"),
        ent_hash="x",
        degradation=Degradation.EPHEMERAL_ASIDE,
        to_identity=ASKER_IDENTITY,
    )

    with pytest.raises(DeliveryRefusedError, match="per-viewer"):
        deliver(adapter, delivery)
    assert adapter.sent == []


def test_an_ephemeral_send_that_names_no_viewer_is_refused() -> None:
    """M10.2.5. Asking for a private message and forgetting the viewer is not a public one.

    The two ways of saying "only this person sees it" have to agree, because the resolution
    that reads as harmless is the public post.

    Deleting this lets an ephemeral send with an empty viewer go out to the whole chat."""
    adapter = LarkAdapter()
    with pytest.raises(DeliveryRefusedError, match="disagree"):
        adapter.send(_payload("SNM"), to="oc_1", ephemeral=True)
    assert adapter.sent == []


def test_a_planned_delivery_reaches_the_surface_with_its_audience_intact() -> None:
    """The positive sibling for the two refusals above, end to end through the fake.

    An ephemeral delivery arrives carrying its viewer and a room posting arrives carrying
    none. Asserted on the recorded message rather than on the plan, because the plan being
    right and the send dropping the viewer is exactly the gap between them.

    Deleting this lets `deliver` post everything publicly while every planning test
    passes."""
    adapter = LarkAdapter()
    room = Delivery(
        chat_id="oc_1",
        visibility=Visibility.ROOM,
        payload=_payload("SNM"),
        ent_hash="x",
        degradation=Degradation.EPHEMERAL_ASIDE,
    )
    aside = Delivery(
        chat_id="oc_1",
        visibility=Visibility.EPHEMERAL,
        payload=_payload("SNM", "margin"),
        ent_hash="y",
        degradation=Degradation.EPHEMERAL_ASIDE,
        to_identity=ASKER_IDENTITY,
    )

    deliver(adapter, room)
    deliver(adapter, aside)

    assert [m.viewer for m in adapter.sent] == ["", ASKER_IDENTITY]
    assert all(m.chat_id == "oc_1" for m in adapter.sent)
    assert "SNM" in adapter.sent[0].body


def test_the_adapter_sends_no_body_that_dropped_the_payload_label() -> None:
    """M10.1.5 through this adapter, and M10.2.3's half of it.

    The opaque escape hatch survives only if the label reaches the person. Two halves: what
    this adapter actually sends carries the label, and a body that dropped it is refused
    rather than delivered as though it were an answer nobody had to check.

    Deleting this lets a Lark installation drop the label while every capability check in
    M4 goes on passing, which is the failure item 12 in docs/needs-rupash.md describes as
    invisible from either side."""
    adapter = LarkAdapter()
    labelled = ChannelPayload(records=({"@entity": "x", "@id": "1"},), label=OPAQUE_LABEL)

    adapter.send(labelled, to="oc_1")
    assert OPAQUE_LABEL in adapter.sent[0].body

    with pytest.raises(CardRefusedError, match="drops the payload label"):
        assert_label_survives("here is an answer", labelled)


def test_the_lark_adapter_cannot_be_handed_anything_but_a_payload() -> None:
    """M4.4.1. The signature check, applied to this adapter rather than to a fictional one.

    `assert_channel_adapter` reads the signature: an adapter whose parameters are a
    `ChannelPayload` and some scalars cannot serialise the trace, the reasons or the dropped
    records, because it was never handed any. `deliver` is a module function precisely so
    `send` can keep that shape.

    Deleting this lets `send` grow a `RedactedAnswer` parameter "because it needed the
    source name too", which is the path around the serialiser."""
    adapter = LarkAdapter()
    assert_channel_adapter(adapter.send)
    assert isinstance(adapter, ChannelAdapter)
    assert adapter.healthy(NOW) is True
    assert LarkAdapter(reachable=False).healthy(NOW) is False
