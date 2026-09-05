"""Binding a channel to a person, and what an unrecognised sender learns. Blocks deploy.

The binding rule is the security decision here, and it is about direction. Sending a code
to a number that asked to be bound proves only that whoever holds that number can read it,
which is exactly what a SIM swap gives an attacker. A nonce minted inside an authenticated
session proves the person was already signed in.

Task ids: M3.2.1, M3.2.2, M3.2.3, M3.2.4, M10.3.3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import (
    NONCE_TTL,
    UNRECOGNISED_PROMPT,
    Binding,
    BindingRefusedError,
    ChannelEvent,
    Unrecognised,
    bind,
    identity_hash,
    mint_nonce,
    resolve,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _event(
    channel: Channel = Channel.WHATSAPP,
    external_id: str = "wamid-001",
    identity: str = "+6591234567",
) -> ChannelEvent:
    return ChannelEvent(
        channel=channel,
        external_id=external_id,
        channel_identity=identity,
        text="how many hours are left on Acme",
        received_at=NOW,
    )


# ------------------------------------------------------------------ one shape in
def test_an_event_without_an_external_id_is_refused() -> None:
    """Without one there is no dedupe key. A channel that redelivers would answer twice,
    and for a side effect answering twice is doing the thing twice."""
    with pytest.raises(ValueError, match="deduped"):
        ChannelEvent(
            channel=Channel.LARK,
            external_id="",
            channel_identity="ou_abc",
            text="hello",
            received_at=NOW,
        )


def test_an_event_without_a_sender_is_refused() -> None:
    with pytest.raises(ValueError, match="sender identity"):
        ChannelEvent(
            channel=Channel.LARK,
            external_id="m1",
            channel_identity="",
            text="hello",
            received_at=NOW,
        )


def test_the_dedupe_key_is_two_columns_not_a_joined_string() -> None:
    """M3.2.2. Two channels may well use the same counter, and a joined string can be
    forged by an identifier that contains the separator."""
    key = _event().dedupe_key
    assert key == ("whatsapp", "wamid-001")
    assert isinstance(key, tuple)


def test_the_same_identifier_on_two_channels_is_two_events() -> None:
    assert _event(Channel.LARK, "m1").dedupe_key != _event(Channel.WHATSAPP, "m1").dedupe_key


# ------------------------------------------------------------------- the identity
def test_an_identity_is_stored_as_a_hash_not_a_number() -> None:
    """A phone number is on the projection denylist, and a binding table full of them is a
    phone book of everyone at the company joined to their role."""
    digest = identity_hash(Channel.WHATSAPP, "+6591234567")
    assert "+6591234567" not in digest
    assert len(digest) == 64


def test_the_same_number_on_two_channels_hashes_differently() -> None:
    """Salted by channel, so a binding proved on a weak channel cannot be used to look up
    the same person's binding on a strong one."""
    assert identity_hash(Channel.WHATSAPP, "+6591234567") != identity_hash(
        Channel.LARK, "+6591234567"
    )


def test_identity_hashing_ignores_case_and_surrounding_space() -> None:
    """Email arrives capitalised differently every time; a binding that depends on the
    casing is a binding that silently stops working."""
    assert identity_hash(Channel.EMAIL, " Wei.Ling@verz.sg ") == identity_hash(
        Channel.EMAIL, "wei.ling@verz.sg"
    )


# ---------------------------------------------------------------------- binding
def test_a_nonce_minted_for_one_channel_cannot_bind_another() -> None:
    """Otherwise the weakest channel becomes the way in to every other one: mint for
    WhatsApp, present on email, and the email binding inherits the trust."""
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    with pytest.raises(BindingRefusedError, match="minted for"):
        bind(nonce, nonce.value, _event(Channel.EMAIL, "e1", "wei@verz.sg"), NOW)


def test_an_expired_nonce_is_refused() -> None:
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    later = NOW + NONCE_TTL + timedelta(seconds=1)
    with pytest.raises(BindingRefusedError, match="expired"):
        bind(nonce, nonce.value, _event(), later)


def test_a_wrong_nonce_is_refused() -> None:
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    with pytest.raises(BindingRefusedError, match="does not match"):
        bind(nonce, "not-the-nonce", _event(), NOW)


def test_two_nonces_are_never_the_same() -> None:
    """Guessing is not a strategy at 128 bits, but only if they are actually random."""
    values = {mint_nonce("p", Channel.LARK, NOW).value for _ in range(200)}
    assert len(values) == 200


def test_a_completed_binding_names_the_principal_and_hashes_the_identity() -> None:
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    binding = bind(nonce, nonce.value, _event(), NOW)
    assert binding.principal_id == "p_wei_ling"
    assert binding.identity_hash == identity_hash(Channel.WHATSAPP, "+6591234567")


def test_a_binding_is_never_worth_more_than_bound() -> None:
    """The whole point of the assurance ladder. Proving a binding once is evidence about
    that day, not authentication of every message after it."""
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    assert bind(nonce, nonce.value, _event(), NOW).assurance is Assurance.BOUND

    with pytest.raises(ValueError, match="live session"):
        Binding(
            channel=Channel.WHATSAPP,
            identity_hash="x" * 64,
            principal_id="p",
            bound_at=NOW,
            assurance=Assurance.AUTHENTICATED,
        )


# ------------------------------------------------------------------ resolution
def test_a_bound_sender_resolves_to_their_principal() -> None:
    nonce = mint_nonce("p_wei_ling", Channel.WHATSAPP, NOW)
    binding = bind(nonce, nonce.value, _event(), NOW)
    assert resolve(_event(), {binding.identity_hash: binding}) is binding


def test_an_unknown_sender_resolves_to_nothing() -> None:
    assert resolve(_event(identity="+6599999999"), {}) is None


def test_a_binding_on_another_channel_does_not_resolve_here() -> None:
    """The channel salt again, from the other direction."""
    nonce = mint_nonce("p_wei_ling", Channel.LARK, NOW)
    lark_event = _event(Channel.LARK, "m1", "+6591234567")
    binding = bind(nonce, nonce.value, lark_event, NOW)
    assert (
        resolve(_event(Channel.WHATSAPP, "w1", "+6591234567"), {binding.identity_hash: binding})
        is None
    )


# --------------------------------------------------------------- the unrecognised
def test_an_unrecognised_sender_carries_no_entitlement_field_at_all() -> None:
    """M3.2.4. Not an empty set, which is a thing that can be intersected, cached and
    passed along. There is no principal here to have reach."""
    assert not hasattr(Unrecognised(Channel.WHATSAPP), "entitlements")


def test_the_prompt_names_nobody_and_confirms_nothing() -> None:
    """The same words whether the number is unknown, known but unbound, or belonged to
    someone whose binding was revoked this morning. Naming the person would confirm the
    number is theirs, which is the question an attacker with a stolen phone is asking."""
    text = UNRECOGNISED_PROMPT.lower()
    for leak in ("unknown", "not found", "no account", "unregistered", "wei", "already"):
        assert leak not in text


def test_the_prompt_tells_the_person_what_to_do() -> None:
    """A refusal with no route forward is indistinguishable from a broken system, and the
    honest majority of people hitting this are staff who have not bound the channel yet."""
    assert "console" in UNRECOGNISED_PROMPT.lower()


def test_the_prompt_is_the_same_object_for_every_channel() -> None:
    """A per-channel variation would let someone compare replies across channels to learn
    which one a number is bound on."""
    assert Unrecognised(Channel.WHATSAPP).prompt == Unrecognised(Channel.EMAIL).prompt


# ------------------------------------------ the prompt rule, now actually enforced
#
# `test_the_prompt_is_the_same_object_for_every_channel` above compares two *default*
# constructions, which are equal by definition. `prompt` is an ordinary field, so a channel
# could always pass its own, and one legitimately does: a widget visitor has no binding to
# speak of and cannot be told to add a channel from a profile they do not have.
#
# So the invariant its name claims was never enforced. These check the property instead.


@pytest.mark.parametrize(
    "leaking",
    [
        "No Lark account is bound to this handle.",
        "That number is unknown to us.",
        "This handle is not registered.",
        "We have no record of this number.",
        "That user does not exist.",
        "You have never signed in here.",
        "That handle is unrecognised.",
    ],
)
def test_a_prompt_that_confirms_whether_the_sender_is_bound_is_refused(leaking: str) -> None:
    """Each of these answers the question an attacker holding a stolen handset came to ask.

    The first one is the case that matters most, and it is why this is a pattern and not a
    substring check: "No Lark account" does not contain "no account", so a plain phrase list
    accepts the sentence somebody would actually write. It was accepted, until this.

    Delete this and the rule goes back to living in a test that compares two defaults, which
    is a test of nothing."""
    with pytest.raises(ValueError, match="confirms whether the sender is bound"):
        Unrecognised(channel=Channel.LARK, prompt=leaking)


def test_the_prompts_this_system_actually_uses_are_accepted() -> None:
    """The other direction, and it is not a formality: a rule tuned until it refuses
    everything would satisfy every test above while making the feature unbuildable.

    The widget's prompt is the one that proves a per-channel prompt stays possible. It has to
    be, because "sign in and add this channel from your profile" is an instruction an
    anonymous visitor cannot follow, and an unfollowable instruction reads as the product
    being broken."""
    from brain.channels.widget import WIDGET_PROMPT

    assert Unrecognised(Channel.WHATSAPP).prompt == UNRECOGNISED_PROMPT
    assert Unrecognised(channel=Channel.WIDGET, prompt=WIDGET_PROMPT).prompt == WIDGET_PROMPT


def test_an_unrecognised_sender_is_never_told_nothing_at_all() -> None:
    """Silence is indistinguishable from the system being broken, and the honest majority of
    people reaching this path are staff who have not bound the channel yet."""
    with pytest.raises(ValueError, match="told something"):
        Unrecognised(channel=Channel.LARK, prompt="   ")
