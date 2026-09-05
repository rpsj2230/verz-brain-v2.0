"""Binding a chat account to a person, exactly once, and taking it back.

The outward direction, the constant-time comparison and the channel pin are tested against
`brain.gate.ingress` and are not re-tested here. Every test in this file is a way the same
nonce binds twice, or a way a binding outlives the person's use of it.

The ledger fake is a set with an atomic-looking API, because the real point of
`NonceLedger.consume` is that a caller cannot write the racy version. A fake with a
`was_consumed` read would let this file prove something the protocol forbids.

Task ids: M10.3.2, M10.3.4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.binding import (
    NONCE_TTL_SECONDS,
    BindingOutcome,
    apply_unbind,
    bind_once,
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
