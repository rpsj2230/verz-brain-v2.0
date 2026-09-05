"""Binding a chat account to a person, exactly once, and taking it back.

The outward direction, the constant-time comparison and the channel pin are tested against
`brain.gate.ingress` and are not re-tested here. Every test in this file is a way the same
nonce binds twice, or a way a binding outlives the person's use of it.

The ledger fake is a set with an atomic-looking API, because the real point of
`NonceLedger.consume` is that a caller cannot write the racy version. A fake with a
`was_consumed` read would let this file prove something the protocol forbids.

Task ids: M10.3.1, M10.3.2, M10.3.4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.binding import (
    NONCE_TTL_SECONDS,
    BindingOutcome,
    SessionNonce,
    apply_unbind,
    assert_session_still_authorises,
    bind_once,
    mint_for_session,
    nonce_digest,
    unbind,
    would_replay,
)
from brain.gate.context import Channel
from brain.gate.ingress import (
    Binding,
    BindingNonce,
    BindingRefusedError,
    ChannelEvent,
    identity_hash,
    mint_nonce,
)
from brain.identity.sessions import Session, SessionRegistry

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
ALICE = "u_alice"
MALLORY = "u_mallory"


class Ledger:
    """A set, behind the one method the protocol exposes."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.ttls: list[int] = []

    def consume(self, nonce_digest: str, *, now: datetime, ttl_seconds: int) -> bool:
        del now
        self.ttls.append(ttl_seconds)
        if nonce_digest in self.seen:
            return False
        self.seen.add(nonce_digest)
        return True


def event(identity: str = "ou_alice_lark", channel: Channel = Channel.LARK) -> ChannelEvent:
    return ChannelEvent(
        channel=channel,
        channel_identity=identity,
        external_id=f"msg-{identity}",
        text="bind me",
        received_at=NOW,
    )


def nonce(principal: str = ALICE, channel: Channel = Channel.LARK) -> BindingNonce:
    return mint_nonce(principal, channel, NOW)


def existing(principal: str, identity: str, channel: Channel = Channel.LARK) -> Binding:
    return Binding(
        channel=channel,
        identity_hash=identity_hash(channel, identity),
        principal_id=principal,
        bound_at=NOW - timedelta(days=30),
    )


# ------------------------------------------------------------------ it binds at all
def test_a_correct_nonce_binds_the_sender_to_the_person_who_minted_it() -> None:
    """If this fails every refusal below passes for the wrong reason: a binder that refuses
    everything satisfies all of them."""
    n = nonce()
    out = bind_once(n, n.value, event(), now=NOW, ledger=Ledger())

    assert isinstance(out, BindingOutcome)
    assert out.binding.principal_id == ALICE
    assert out.revoked is None


# ----------------------------------------------------------------- the replay itself
def test_the_same_nonce_cannot_bind_a_second_account() -> None:
    """**The reason this module exists.** `ingress.bind` checks that the value matches, has
    not expired and is on the right channel, and all three are still true the second time.

    The nonce travels through the chat channel by design, so it is readable by a second
    device on the account, a workspace administrator, a backup, or a bot with history access.
    Whoever reads it inside the window can present it from their own account, and the real
    person's binding still works, so nothing looks wrong from either side.

    Delete this and the outward direction still holds and the ten-minute window becomes
    unlimited uses."""
    ledger = Ledger()
    n = nonce()
    bind_once(n, n.value, event(), now=NOW, ledger=ledger)

    with pytest.raises(BindingRefusedError, match="already been used"):
        bind_once(n, n.value, event("ou_mallory_lark"), now=NOW, ledger=ledger)


def test_a_wrong_value_does_not_burn_the_nonce() -> None:
    """The ordering, and it is the difference between a denial and a nuisance.

    Consuming before validating lets anybody who can send a message on that channel destroy
    a nonce they cannot use, by presenting anything at all. The real person's next attempt
    then fails for a reason nobody can see, and minting another is the same denial one
    message later.

    Delete this and somebody reorders these two lines for tidiness and turns the binding
    flow into a thing a stranger can switch off."""
    ledger = Ledger()
    n = nonce()

    with pytest.raises(BindingRefusedError):
        bind_once(n, "not-the-nonce", event(), now=NOW, ledger=ledger)

    assert ledger.seen == set(), "a failed attempt consumed the nonce"
    assert bind_once(n, n.value, event(), now=NOW, ledger=ledger).binding.principal_id == ALICE


def test_an_expired_nonce_does_not_burn_itself_either() -> None:
    """Same rule for the same reason. An expired nonce is refused by `bind`, and reaching the
    ledger at all would record a consumption for a value nobody successfully used."""
    ledger = Ledger()
    n = nonce()
    late = NOW + timedelta(seconds=NONCE_TTL_SECONDS + 1)

    with pytest.raises(BindingRefusedError, match="expired"):
        bind_once(n, n.value, event(), now=late, ledger=ledger)

    assert ledger.seen == set()


def test_the_consumption_record_outlives_the_nonce() -> None:
    """A record pruned before the nonce expires is a window in which a replay works again,
    and it is a window nobody would find by testing the happy path.

    Delete this and the TTL can be set to anything, including something shorter than the
    nonce, and every other test in this file still passes."""
    ledger = Ledger()
    n = nonce()
    bind_once(n, n.value, event(), now=NOW, ledger=ledger)

    assert ledger.ttls, "the ledger was never asked to keep a record"
    assert ledger.ttls[0] > NONCE_TTL_SECONDS


def test_the_ledger_stores_a_digest_and_never_the_nonce() -> None:
    """The ledger is a table of live credentials otherwise: anything that can read it inside
    the window can present what it finds. The nonce is a bearer value for ten minutes and the
    record of it must not be."""
    ledger = Ledger()
    n = nonce()
    bind_once(n, n.value, event(), now=NOW, ledger=ledger)

    assert n.value not in ledger.seen
    assert nonce_digest(n) in ledger.seen


# --------------------------------------------------------------- who ends up bound
def test_an_identity_already_bound_to_somebody_else_is_refused() -> None:
    """Binding one chat account to two people is an account takeover, and the two readings
    of it are "somebody is taking over" and "somebody made a mistake". Nothing here can tell
    those apart, so it refuses rather than resolving, because resolving either way silently
    picks one.

    Delete this and a nonce minted by Mallory, presented from Alice's Lark account, quietly
    points Alice's messages at Mallory's principal."""
    n = nonce(MALLORY)
    live = (existing(ALICE, "ou_alice_lark"),)

    with pytest.raises(BindingRefusedError, match="already bound"):
        bind_once(n, n.value, event("ou_alice_lark"), now=NOW, ledger=Ledger(), existing=live)


def test_rebinding_on_a_new_account_revokes_the_previous_one() -> None:
    """A person has one Lark account. Two live bindings mean the old one, which is the one
    plausibly compromised or on a replaced device, keeps working forever while the new one
    also works, so nobody notices.

    The revoked binding is returned rather than dropped because somebody has to write it to
    the ledger, and a revocation nobody recorded is one nobody can explain later."""
    n = nonce(ALICE)
    live = (existing(ALICE, "ou_alice_old_device"),)

    out = bind_once(n, n.value, event("ou_alice_new"), now=NOW, ledger=Ledger(), existing=live)

    assert out.revoked is not None
    assert out.revoked.identity_hash == identity_hash(Channel.LARK, "ou_alice_old_device")


def test_a_binding_on_another_channel_is_left_alone_by_a_rebind() -> None:
    """So the revocation cannot be widened into revoking everything, which would satisfy the
    test above. Adding Lark must not silently remove the person's email binding."""
    n = nonce(ALICE, Channel.LARK)
    live = (
        existing(ALICE, "alice@example.com", Channel.EMAIL),
        existing(ALICE, "ou_alice_old", Channel.LARK),
    )

    out = bind_once(n, n.value, event("ou_alice_new"), now=NOW, ledger=Ledger(), existing=live)

    assert out.revoked is not None
    assert out.revoked.channel is Channel.LARK


def test_rebinding_the_same_account_revokes_nothing() -> None:
    """Presenting a fresh nonce from the account already bound is a person redoing a step,
    not a device change. Reporting a revocation would write an audit entry for something
    that did not happen."""
    n = nonce(ALICE)
    live = (existing(ALICE, "ou_alice_lark"),)

    out = bind_once(n, n.value, event("ou_alice_lark"), now=NOW, ledger=Ledger(), existing=live)

    assert out.revoked is None


def test_the_same_identity_string_on_two_channels_is_two_different_hashes() -> None:
    """This pins the premise a redundancy rests on, which is why it lives here rather than
    beside `identity_hash` itself.

    The channel comparison in the takeover check is redundant *because* the hash is salted by
    channel: two hashes are equal only when the channels already are. A mutation removing that
    comparison survives, correctly. But the redundancy is a property of another module, and if
    the salt ever went away, that comparison would silently become the only thing standing
    between a binding on a weak channel and one on a strong channel.

    Delete this and unsalting `identity_hash` looks like a simplification, passes every test
    in both files, and turns a documented redundancy into the load-bearing check nobody knows
    is load-bearing."""
    assert identity_hash(Channel.LARK, "alice@example.com") != identity_hash(
        Channel.EMAIL, "alice@example.com"
    )


# ------------------------------------------------------------------------ unbinding
def test_unbinding_names_every_binding_for_that_person_on_that_channel() -> None:
    """Every match rather than the first, so a table that has somehow accumulated two for one
    person is cleaned rather than half-cleaned, leaving the one nobody looked at answering."""
    live = (
        existing(ALICE, "ou_alice_one"),
        existing(ALICE, "ou_alice_two"),
        existing(ALICE, "alice@example.com", Channel.EMAIL),
        existing(MALLORY, "ou_mallory"),
    )

    doomed = unbind(ALICE, Channel.LARK, live)

    assert len(doomed) == 2
    assert all(b.principal_id == ALICE and b.channel is Channel.LARK for b in doomed)


def test_unbinding_removes_the_row_rather_than_flagging_it() -> None:
    """A revoked binding left in the table is subtractive state, and every read afterwards
    has to remember to exclude it. Forget once and a revoked channel is answering again.

    Delete this and somebody adds a `revoked_at` column, which reads as more auditable and
    is the failure mode the whole identity package refuses elsewhere."""
    live = {b.identity_hash: b for b in (existing(ALICE, "ou_alice"), existing(MALLORY, "ou_m"))}

    after = apply_unbind(ALICE, Channel.LARK, live)

    assert identity_hash(Channel.LARK, "ou_alice") not in after
    assert len(after) == 1


def test_unbinding_somebody_with_no_binding_changes_nothing() -> None:
    """So the removal cannot be written as "return an empty map", which would satisfy the
    test above and sign everybody out."""
    live = {b.identity_hash: b for b in (existing(MALLORY, "ou_m"),)}

    assert apply_unbind(ALICE, Channel.LARK, live) == live


def test_the_advisory_check_reports_a_used_nonce_without_deciding_anything() -> None:
    """`would_replay` exists so a console can say "that code has been used" in the same words
    the binder would, rather than inventing its own idea of what used means. Read-only.

    Delete this and the function is unexercised, and an unexercised public function is one
    that drifts from the digest the binder actually writes."""
    ledger = Ledger()
    n = nonce()
    assert not would_replay(n, ledger.seen)

    bind_once(n, n.value, event(), now=NOW, ledger=ledger)

    assert would_replay(n, ledger.seen)


def test_the_advisory_replay_check_is_not_what_decides() -> None:
    """`would_replay` exists so a console can say "that code has been used" without inventing
    its own idea of what used means. It must never be the decision: a check here followed by
    a consume there is exactly the race the atomic `consume` removes.

    Asserted on the source, because the behaviour is identical either way while nothing
    races, which is every test in this file."""
    import inspect

    from brain.channels import binding

    body = inspect.getsource(binding.bind_once)
    assert "would_replay" not in body, "the decision moved to the advisory read"
    assert "ledger.consume(" in body


def test_the_ledger_protocol_offers_no_way_to_read_before_writing() -> None:
    """The racy version must not be spellable. A protocol with a separate read is one whose
    only correct use is a transaction the caller has to remember, and whose incorrect use
    type-checks perfectly.

    Delete this and somebody adds `was_consumed` for a console, and the next caller uses it
    to decide."""
    from brain.channels.binding import NonceLedger

    methods = {name for name in vars(NonceLedger) if not name.startswith("_")}
    assert methods == {"consume"}


# ------------------------------------------- minting requires somebody to be signed in
#
# `ingress.mint_nonce` takes a principal id as a string and documents that the caller must
# have authenticated them. That is a true sentence and not a guard.


def _session(principal: str = ALICE, *, opened: datetime = NOW) -> Session:
    return Session(
        session_id="sid-" + principal,
        principal_id=principal,
        issuer="https://id.example/realms/brain",
        subject="1f2e3d4c-0000-4000-8000-000000000001",
        opened_at=opened,
        expires_at=opened + timedelta(minutes=30),
        absolute_expiry=opened + timedelta(hours=10),
    )


def test_a_code_is_minted_for_the_person_who_is_signed_in() -> None:
    """The happy path, and it also pins where the principal comes from: the session, not an
    argument. If this fails, the refusals below pass for the wrong reason."""
    minted = mint_for_session(_session(), Channel.LARK, now=NOW)

    assert isinstance(minted, SessionNonce)
    assert minted.nonce.principal_id == ALICE
    assert minted.session_id == "sid-" + ALICE


def test_minting_takes_no_principal_argument_at_all() -> None:
    """**The actual guard, and it is a shape rather than a check.** A function taking a
    principal id lets a caller mint a nonce naming a colleague, present it from their own
    chat account, and receive that colleague's messages. That is the exact attack the outward
    direction exists to prevent, and it rested on the caller being careful.

    Asserted on the signature because there is no behaviour to observe: the wrong call does
    not exist. Delete this and a principal parameter can be added back for a caller that
    "already knows who it is", which is how it was written the first time."""
    import inspect

    from brain.channels import binding

    parameters = set(inspect.signature(binding.mint_for_session).parameters)
    assert "principal_id" not in parameters
    assert parameters == {"session", "channel", "now"}


def test_an_expired_sign_in_mints_nothing() -> None:
    """Minting outside a live session is minting on behalf of somebody who is not there.

    Uses `Session.is_live` rather than a second opinion, so idle expiry and the absolute
    ceiling stay the ones `brain.identity.sessions` decided."""
    stale = _session(opened=NOW - timedelta(hours=11))

    with pytest.raises(BindingRefusedError, match="expired"):
        mint_for_session(stale, Channel.LARK, now=NOW)


def test_a_code_stops_working_when_the_person_signs_out() -> None:
    """A nonce lives ten minutes, which is long enough to request a code and then sign out.
    Signing out has to end the code at that moment, or "log me out everywhere" leaves a
    credential outstanding that binds a chat account afterwards.

    Delete this and logout is honoured for tokens and ignored for binding codes."""
    registry = SessionRegistry()
    session = _session()
    registry.register(session)
    minted = mint_for_session(session, Channel.LARK, now=NOW)

    assert_session_still_authorises(minted, now=NOW, registry=registry)

    registry.end_session(session.session_id, NOW + timedelta(minutes=1))

    with pytest.raises(BindingRefusedError):
        assert_session_still_authorises(minted, now=NOW + timedelta(minutes=2), registry=registry)


def test_a_code_naming_a_sign_in_nobody_has_heard_of_is_refused() -> None:
    """Found by mutation: the sign-out test above is satisfied by the not-before floor alone,
    so the `registry.get` branch was never exercised by anything.

    It is load-bearing on its own. A `SessionNonce` is an ordinary object and can be built
    naming any session id; the floor for a principal who has never signed out is None, so the
    floor check passes it. Only looking the session up refuses a code that came from a
    sign-in that never existed.

    Delete this and a fabricated pair binds, and the sign-out test still passes."""
    registry = SessionRegistry()
    forged = SessionNonce(
        nonce=mint_nonce(ALICE, Channel.LARK, NOW),
        session_id="sid-that-never-existed",
    )

    with pytest.raises(BindingRefusedError, match="has ended"):
        assert_session_still_authorises(forged, now=NOW, registry=registry)


def test_a_code_from_a_sign_in_that_simply_timed_out_is_refused() -> None:
    """The other branch the floor does not cover, and also found by mutation.

    Nobody signed out here, so no floor was ever raised. The session is still in the registry
    and has merely run past its idle window. Without the liveness half of the check, a code
    minted in a session that expired hours ago still binds, and expiry would mean nothing on
    this path while meaning everything on the token path.

    Delete this and `is_live` can be dropped from the lookup with every other test green."""
    registry = SessionRegistry()
    session = _session(opened=NOW - timedelta(hours=9))
    registry.register(session)
    minted = mint_for_session(session, Channel.LARK, now=NOW - timedelta(hours=9))

    # Well past the thirty-minute idle window, and no sign-out anywhere.
    later = NOW - timedelta(hours=9) + timedelta(hours=2)

    assert registry.not_before_for(ALICE) is None, "a floor was raised; this is the wrong test"
    with pytest.raises(BindingRefusedError, match="has ended"):
        assert_session_still_authorises(minted, now=later, registry=registry)


def test_a_code_is_refused_after_a_sign_out_this_process_never_saw() -> None:
    """The half that survives a restart or a second replica.

    `registry.get` only answers for sessions this process knows about, so a logout handled
    elsewhere leaves it unable to refuse anything. The not-before floor is one timestamp per
    principal and every sign-out raises it, so it answers in all three cases.

    Delete this and the guard works on whichever process happened to serve the sign-out and
    silently does nothing on the others, which is the failure that only appears once there is
    more than one replica."""
    registry = SessionRegistry()
    session = _session()
    registry.register(session)
    minted = mint_for_session(session, Channel.LARK, now=NOW)

    # A sign-out that raises the floor. The session row is then re-registered, standing in
    # for a replica that never learned the session had ended.
    registry.end_all_for(ALICE, NOW + timedelta(minutes=1))
    registry.register(session)

    with pytest.raises(BindingRefusedError, match="revoked"):
        assert_session_still_authorises(minted, now=NOW + timedelta(minutes=2), registry=registry)
