"""Channel adapters and room answers.

Every test in the room half is a way one person's permissions decide what a colleague reads.
Every test in the adapter half is a way a warning gets dropped on the way out.

Task ids: M10.1.1, M10.1.2, M10.1.3, M10.1.4, M10.1.5,
M10.4.1, M10.4.2, M10.4.3, M10.4.4, M10.4.5
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.adapter import (
    ChannelCapabilities,
    DeliveryRefusedError,
    Feature,
    assert_can_send,
    registered,
)
from brain.channels.room import (
    Degradation,
    Member,
    RoomRefusedError,
    RoomRender,
    floor,
    plan,
    revalidate,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload
from brain.core.scope import Scope
from brain.gate.context import Channel

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

READ_NAME = "read:client.name"
READ_MARGIN = "read:client.margin"


def _ents(
    *capabilities: str, principal_id: str = "u_a", not_after: datetime | None = None
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
        not_after=not_after,
    )


def _member(principal_id: str, *capabilities: str, not_after: datetime | None = None) -> Member:
    return Member(
        principal_id=principal_id,
        entitlement=_ents(*capabilities, principal_id=principal_id, not_after=not_after),
    )


def _caps(*features: Feature, **overrides: object) -> ChannelCapabilities:
    base: dict[str, object] = {
        "channel": Channel.LARK,
        "features": frozenset(features),
        "max_classification": Classification.INTERNAL,
        "can_carry_label": True,
    }
    base.update(overrides)
    return ChannelCapabilities(**base)  # type: ignore[arg-type]


# ============================================================ the adapter contract


def test_an_adapter_that_cannot_render_a_label_refuses_the_opaque_payload() -> None:
    """M10.1.5, and it is the reason the escape hatch is survivable at all.

    The opaque path exists so a tool returning something the redactor cannot walk is not
    simply unusable. The price is a label saying nobody checked this, carried to the person.
    An adapter that dropped it - because SMS has no formatting, because a card template had
    nowhere to put it - turns "here is something nobody checked" into "here is an answer".

    Deleting this test makes the escape hatch a silent one, which is worse than not having
    it: an unusable tool is visible, and an unlabelled answer is not."""
    payload = ChannelPayload(records=({"@entity": "x", "@id": "1"},), label=OPAQUE_LABEL)
    with pytest.raises(DeliveryRefusedError, match="cannot render a payload label"):
        assert_can_send(_caps(can_carry_label=False), payload)


def test_an_adapter_that_can_render_a_label_carries_the_opaque_payload() -> None:
    """The other half. Refusing everywhere would make the escape hatch unusable rather than
    labelled, which is a different decision and not this one."""
    payload = ChannelPayload(records=({"@entity": "x", "@id": "1"},), label=OPAQUE_LABEL)
    assert_can_send(_caps(can_carry_label=True), payload)


def test_a_channel_refuses_a_classification_above_its_ceiling() -> None:
    """M10.1.3. WhatsApp is a consumer app on somebody's personal phone; the console is
    behind the identity provider. A field classified `restricted` should not reach the first
    because the question happened to be asked there."""
    with pytest.raises(DeliveryRefusedError, match="may carry at most"):
        assert_can_send(
            _caps(max_classification=Classification.INTERNAL),
            ChannelPayload(),
            highest=Classification.RESTRICTED,
        )


def test_the_refusal_names_no_field_and_no_value() -> None:
    """A refusal that quoted either would put the sensitive thing into whatever log records
    the refusal, which is the log kept longest and read most widely."""
    with pytest.raises(DeliveryRefusedError) as caught:
        assert_can_send(
            _caps(max_classification=Classification.PUBLIC),
            ChannelPayload(records=({"@entity": "client", "@id": "c1", "margin": "0.42"},)),
            highest=Classification.CONFIDENTIAL,
        )
    assert "0.42" not in str(caught.value)
    assert "margin" not in str(caught.value)


def test_the_sensitivity_ceiling_is_a_level_and_not_a_list() -> None:
    """Sensitivity is ordered. A list of allowed classes would let somebody permit
    `restricted` while forbidding `confidential`, which is a configuration nobody means and
    which reads in review as deliberate."""
    caps = _caps(max_classification=Classification.CONFIDENTIAL)
    assert caps.may_carry(Classification.PUBLIC)
    assert caps.may_carry(Classification.CONFIDENTIAL)
    assert not caps.may_carry(Classification.RESTRICTED)


def test_an_adapter_whose_health_check_raises_reads_as_unhealthy() -> None:
    """M10.1.4. One broken adapter must not make the health of every other one
    unanswerable, which is what an exception escaping the registry would do."""

    class Exploding:
        def capabilities(self) -> ChannelCapabilities:
            return _caps()

        def normalise(self, raw: object) -> object:
            raise NotImplementedError

        def send(self, payload: object, *, to: str) -> None:
            raise NotImplementedError

        def healthy(self, now: datetime) -> bool:
            msg = "the provider is unreachable"
            raise RuntimeError(msg)

    health = registered({Channel.LARK: Exploding()}, NOW)  # type: ignore[dict-item]
    assert health == {Channel.LARK: False}


def test_registration_and_health_are_reported_apart() -> None:
    """ "Not registered" and "registered and unhealthy" are different problems that send a
    person to different places. A filtered list of the healthy ones makes them identical."""

    class Fine:
        def capabilities(self) -> ChannelCapabilities:
            return _caps()

        def normalise(self, raw: object) -> object:
            raise NotImplementedError

        def send(self, payload: object, *, to: str) -> None:
            raise NotImplementedError

        def healthy(self, now: datetime) -> bool:
            return True

    health = registered({Channel.LARK: Fine()}, NOW)  # type: ignore[dict-item]
    assert Channel.LARK in health
    assert Channel.EMAIL not in health, "an unregistered channel must not appear at all"


# =================================================================== the room floor


def test_the_room_envelope_is_the_intersection_of_everyone_present() -> None:
    """M10.4.1, and the property the whole module exists for. If one person present cannot
    see margins, the room's answer has no margins in it, whoever asked.

    Deleting this lets the asker's permissions decide what a colleague reads, and the wrong
    answer there is invisible: it looks like every other message in the room."""
    members = [
        _member("u_asker", READ_NAME, READ_MARGIN),
        _member("u_junior", READ_NAME),
    ]
    envelope = floor(members)
    held = {g.capability.value for g in envelope.grants}
    assert held == {READ_NAME}


def test_a_senior_person_present_does_not_raise_the_floor() -> None:
    """The other direction, and the one somebody will argue for. "The manager is in the room
    so it is fine" makes the floor a ceiling, and the room stops being safe the moment they
    step out."""
    members = [
        _member("u_junior", READ_NAME),
        _member("u_manager", READ_NAME, READ_MARGIN),
    ]
    held = {g.capability.value for g in floor(members).grants}
    assert READ_MARGIN not in held


def test_an_empty_room_is_refused_rather_than_answered_with_nothing() -> None:
    """An empty entitlement means "present, and entitled to nothing", which is a real and
    answerable state. An empty room means nobody asked. Returning the same value for both
    lets a bug that lost the membership list read as a room where everybody holds nothing."""
    with pytest.raises(RoomRefusedError, match="no members"):
        floor([])


def test_a_question_from_somebody_not_in_the_room_is_refused() -> None:
    """Answering it would put an answer in front of an audience the asker is not part of and
    cannot see the effect of."""
    with pytest.raises(RoomRefusedError, match="not in this room"):
        plan([_member("u_other", READ_NAME)], "u_ghost", _caps(), now=NOW)


def test_a_member_has_no_field_that_could_make_them_count_for_more() -> None:
    """Structural. `is_admin`, `role` or `weight` would each be a way for one person's
    presence to count for more than another's, and a floor is a floor precisely because it
    does not."""
    names = {f.name for f in dataclasses.fields(Member)}
    for forbidden in ("is_admin", "role", "weight", "seniority", "rank", "priority"):
        assert forbidden not in names


# ============================================================ the degradation ladder


def test_everyone_seeing_everything_needs_no_degradation() -> None:
    members = [_member("u_a", READ_NAME), _member("u_b", READ_NAME)]
    assert plan(members, "u_a", _caps(), now=NOW).degradation is Degradation.FULL


def test_the_asker_may_be_told_more_privately_where_the_channel_allows_it() -> None:
    """M10.4.2. Ephemeral is a way to avoid over-sharing, never a way around the floor: the
    aside is the asker's own reach, and what goes into the room is still the floor."""
    members = [_member("u_a", READ_NAME, READ_MARGIN), _member("u_b", READ_NAME)]
    render = plan(members, "u_a", _caps(Feature.EPHEMERAL), now=NOW)
    assert render.degradation is Degradation.EPHEMERAL_ASIDE
    assert render.aside_for == "u_a"
    # And the room's envelope is still the floor, not the asker's.
    assert {g.capability.value for g in render.envelope.grants} == {READ_NAME}


def test_without_ephemeral_the_asker_gets_what_the_room_gets() -> None:
    """Not a leak, and worth recording rather than silent: somebody will ask why the same
    question answered differently in two places, and `FLOOR_ONLY` is the answer."""
    members = [_member("u_a", READ_NAME, READ_MARGIN), _member("u_b", READ_NAME)]
    render = plan(members, "u_a", _caps(), now=NOW)
    assert render.degradation is Degradation.FLOOR_ONLY
    assert render.aside_for == ""


def test_a_floor_of_nothing_and_no_ephemeral_ends_at_a_link() -> None:
    """M10.4.3. The link carries no answer, so following it re-runs the whole gate for
    whoever actually clicks - which is what makes a forwarded link harmless. A quieter
    version in the room would not be."""
    members = [_member("u_a", READ_NAME), _member("u_b")]
    assert plan(members, "u_a", _caps(), now=NOW).degradation is Degradation.LINK_ONLY


def test_a_floor_of_nothing_with_ephemeral_becomes_a_private_answer() -> None:
    """The room gets nothing and the asker gets their own view, privately. Better than a
    link where the channel can do it."""
    members = [_member("u_a", READ_NAME), _member("u_b")]
    render = plan(members, "u_a", _caps(Feature.EPHEMERAL), now=NOW)
    assert render.degradation is Degradation.EPHEMERAL_ASIDE


def test_an_expired_member_empties_the_room() -> None:
    """A contractor whose access ended is present and holds nothing. The floor is then
    empty, which is the correct answer rather than an error - and the room degrades."""
    members = [
        _member("u_a", READ_NAME),
        _member("u_gone", READ_NAME, not_after=NOW - timedelta(days=1)),
    ]
    render = plan(members, "u_a", _caps(), now=NOW)
    assert render.degradation is Degradation.LINK_ONLY


# ==================================================== membership changing under it


def test_somebody_joining_invalidates_a_computed_answer() -> None:
    """M10.4.4. The envelope was computed at a floor they were not part of, and their reach
    may be lower. Merging the two would mean deciding which of two truths to keep, and every
    way of deciding that is a guess."""
    members = [_member("u_a", READ_NAME), _member("u_b", READ_NAME)]
    render = plan(members, "u_a", _caps(), now=NOW)
    with pytest.raises(RoomRefusedError, match="the room changed"):
        revalidate(render, frozenset({"u_a", "u_b", "u_c"}), members)


def test_somebody_leaving_also_invalidates_it() -> None:
    """The half worth stating. The envelope is then narrower than it needs to be, which is
    not a leak - but recomputing costs nothing, and a render that silently outlives its
    membership is one nobody can reason about afterwards."""
    members = [_member("u_a", READ_NAME), _member("u_b", READ_NAME)]
    render = plan(members, "u_a", _caps(), now=NOW)
    with pytest.raises(RoomRefusedError):
        revalidate(render, frozenset({"u_a"}), members)


def test_an_unchanged_room_passes_revalidation() -> None:
    members = [_member("u_a", READ_NAME), _member("u_b", READ_NAME)]
    render = plan(members, "u_a", _caps(), now=NOW)
    revalidate(render, frozenset({"u_a", "u_b"}), members)


def test_the_refusal_counts_the_change_and_does_not_name_who() -> None:
    """A message naming who joined tells whoever reads the log something about room
    membership they may not be entitled to, and the caller already has the list."""
    members = [_member("u_a", READ_NAME)]
    render = RoomRender(
        envelope=_ents(READ_NAME),
        degradation=Degradation.FULL,
        membership=frozenset({"u_a"}),
    )
    with pytest.raises(RoomRefusedError) as caught:
        revalidate(render, frozenset({"u_a", "u_secret_person"}), members)
    assert "u_secret_person" not in str(caught.value)
    assert "1 joined" in str(caught.value)
