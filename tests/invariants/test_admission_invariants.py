"""Channel and assurance can only ever take reach away. A failure here blocks deploy.

Two ceilings apply before an agent is chosen. Both are intersections, which means the
interesting tests are not "does WhatsApp allow this" but "can any combination of channel
and assurance produce a capability the caller did not already hold". The answer must be no,
for every combination, which is small enough to check exhaustively.

Task ids: M3.3.3, M3.3.4
"""

from __future__ import annotations

import itertools

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.gate.admission import (
    ASSURANCE_VERBS,
    Assurance,
    admit,
    verbs_for_channel,
    would_lose,
)
from brain.gate.context import Channel

pytestmark = pytest.mark.invariant


def _grant(value: str) -> Grant:
    return Grant(capability=Capability(value=value), scope=Scope())


#: A finance director: holds everything, including the two verbs that move money.
DIRECTOR = EntitlementSet(
    principal_id="p_director",
    grants=(
        _grant("read:client.name"),
        _grant("write:client.status"),
        _grant("invoke:report.generate"),
        _grant("approve:payment.release"),
        _grant("admin:grant.assign"),
    ),
)

ALL_COMBINATIONS = list(itertools.product(list(Channel), list(Assurance)))


# ------------------------------------------------------------ only ever subtracts
@pytest.mark.parametrize(("channel", "assurance"), ALL_COMBINATIONS)
def test_no_combination_can_grant_what_the_caller_does_not_hold(
    channel: Channel, assurance: Assurance
) -> None:
    """The property the whole module exists for, checked exhaustively because the space is
    small. If any combination could add, the channel would become a place to escalate from,
    and an operator widening a ceiling by mistake would be handing out permissions."""
    admitted = admit(DIRECTOR, channel, assurance)
    held = {g.capability.value for g in DIRECTOR.grants}
    assert {g.capability.value for g in admitted.grants} <= held


@pytest.mark.parametrize(("channel", "assurance"), ALL_COMBINATIONS)
def test_admission_never_extends_the_time_bound(channel: Channel, assurance: Assurance) -> None:
    """A contractor's expiry has to survive being narrowed. Rebuilding the set is exactly
    where a `not_after` gets dropped, and a dropped expiry is an unbounded contractor."""
    from datetime import UTC, datetime

    bounded = DIRECTOR.model_copy(update={"not_after": datetime(2026, 10, 1, tzinfo=UTC)})
    assert admit(bounded, channel, assurance).not_after == bounded.not_after


def test_a_caller_holding_nothing_still_holds_nothing_at_the_strongest_assurance() -> None:
    """Assurance is evidence about identity, not a grant. Signing in harder cannot conjure
    reach that was never given."""
    nobody = EntitlementSet(principal_id="p_new")
    assert admit(nobody, Channel.CONSOLE, Assurance.STRONG).grants == ()


# ---------------------------------------------------------------- the channel ceiling
def test_a_message_is_not_a_signature() -> None:
    """The concrete case the channel ceiling exists for. The finance director really does
    hold approve:payment.release, and a WhatsApp message is a phone number that claimed to
    be them."""
    admitted = admit(DIRECTOR, Channel.WHATSAPP, Assurance.STRONG)
    values = {g.capability.value for g in admitted.grants}
    assert "approve:payment.release" not in values
    assert "admin:grant.assign" not in values
    assert "read:client.name" in values


def test_reading_is_allowed_from_every_channel() -> None:
    """Withholding reads by channel would only teach people to go and look somewhere with
    worse logging. What a person may read is already decided, and decided once."""
    for channel in Channel:
        assert "read" in verbs_for_channel(channel)


def test_every_channel_declares_its_verbs() -> None:
    """`assert_never` again: a new channel is a type error rather than an inherited default."""
    for channel in Channel:
        assert verbs_for_channel(channel) <= {"read", "write", "invoke", "approve", "admin"}


# -------------------------------------------------------------- the assurance ceiling
def test_an_unverified_identity_holds_nothing_at_all() -> None:
    """Not "read only". We do not know who this is, so there is no reach to narrow, and
    read-only would still be reading a real person's records on a claim."""
    assert ASSURANCE_VERBS[Assurance.UNVERIFIED] == frozenset()
    assert admit(DIRECTOR, Channel.CONSOLE, Assurance.UNVERIFIED).grants == ()


def test_a_binding_alone_does_not_authorise_an_effect() -> None:
    """BOUND is evidence about the day the binding was made, not about this request."""
    admitted = admit(DIRECTOR, Channel.LARK, Assurance.BOUND)
    assert {g.capability.verb for g in admitted.grants} == {"read"}


def test_approving_needs_a_second_factor_in_this_session() -> None:
    """The one place assurance is stricter than the channel. Lark permits approve; a Lark
    session without a second factor does not."""
    without = admit(DIRECTOR, Channel.LARK, Assurance.AUTHENTICATED)
    with_mfa = admit(DIRECTOR, Channel.LARK, Assurance.STRONG)
    assert "approve:payment.release" not in {g.capability.value for g in without.grants}
    assert "approve:payment.release" in {g.capability.value for g in with_mfa.grants}


def test_assurance_is_monotonic() -> None:
    """Rising assurance must never remove a verb. A non-monotonic ladder would mean signing
    in more strongly could lose you something, which nobody would believe was deliberate."""
    for lower, higher in itertools.pairwise(sorted(Assurance)):
        assert ASSURANCE_VERBS[lower] <= ASSURANCE_VERBS[higher]


@pytest.mark.parametrize("channel", list(Channel))
def test_the_stricter_of_the_two_ceilings_wins(channel: Channel) -> None:
    """Intersection, not precedence. Neither ceiling is a superior authority: whichever
    withholds a verb, it is withheld."""
    for assurance in Assurance:
        admitted = {g.capability.verb for g in admit(DIRECTOR, channel, assurance).grants}
        assert admitted <= verbs_for_channel(channel)
        assert admitted <= ASSURANCE_VERBS[assurance]


# ------------------------------------------------------------------- explaining
def test_a_person_can_be_told_what_their_own_channel_cost_them() -> None:
    """Genuinely useful: "you can approve this from the console". It is about the asker's
    own permissions, which they already know they have."""
    lost = would_lose(DIRECTOR, Channel.WHATSAPP, Assurance.STRONG)
    assert "approve:payment.release" in lost
    assert "read:client.name" not in lost


def test_nothing_is_lost_when_the_channel_and_assurance_admit_everything() -> None:
    assert would_lose(DIRECTOR, Channel.CONSOLE, Assurance.STRONG) == ()
