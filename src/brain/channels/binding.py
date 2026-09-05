"""Making a binding single-use, and taking one away again.

`brain.gate.ingress` already has the hard parts of binding a chat identity to a person: the
nonce is minted inside an authenticated session and carried outward to the channel, never
sent to an address that asked to be bound; it is compared in constant time; and it is pinned
to the channel it was minted for, so the weakest channel cannot become the way in to every
other one.

Two things were missing, and both are in the leaf's own words: **single use**, and
**unbinding and rebinding**.

**A nonce that is merely valid is not single-use, and the difference is the whole attack.**
`bind` checks that the value matches, has not expired and is on the right channel, and every
one of those is still true the second time it is presented. The nonce travels *through the
chat channel* by design, so it is visible to anything that can read the message: a second
device signed into the same account, a workspace administrator, a backup, a bot with history
access. Whoever reads it inside the ten-minute window can present it from their own account
and bind themselves to somebody else's principal, and the real person's binding still works,
so nothing looks wrong from either side. Ten minutes of exposure is the cost of the outward
direction, which is right; single use is what bounds it to one.

Consumption is therefore a **test-and-set that must be atomic**, not a read followed by a
write. Two presentations racing on a check-then-mark both see an unconsumed nonce and both
bind. `NonceLedger.consume` returns whether *this* caller was the one that consumed it, so
the atomicity lives in the implementation where the storage engine can provide it, and this
module cannot express the racy version: there is no `was_consumed` to read.

**One live binding per principal per channel.** A person has one Lark account. Allowing two
means an old account, which is the one plausibly compromised or belonging to a replaced
device, keeps working forever while the new one also works, so nobody notices. Rebinding
therefore revokes the previous binding on that channel and says so in the result. The cost is
real and accepted: somebody with two legitimate accounts on one channel can bind only the
latest.

**One channel identity never binds to two principals.** This is refused rather than resolved,
because the two readings are "somebody is taking over an account" and "somebody made a
mistake", and there is no evidence here that tells them apart. Resolving it either way
silently picks one.

**Unbinding is deletion, not a flag.** Entitlements in this system are additive only and
nothing subtracts at read time; a revoked-flag on a binding row would be exactly the
subtractive state that `subtractive_state` refuses across the identity package. The record
that a binding existed lives in the audit ledger, which is where a record that must survive
belongs.

Nothing here decides whether the caller is authenticated. `mint_nonce` says the caller is
responsible for that being true, and so is this.

Task ids: M10.3.2, M10.3.4
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from brain.gate.context import Channel
from brain.gate.ingress import (
    NONCE_TTL,
    Binding,
    BindingNonce,
    BindingRefusedError,
    ChannelEvent,
    bind,
)

#: Derived from `ingress.NONCE_TTL` rather than restated, so the two cannot drift. A
#: consumption record shorter than the nonce's own life would let a replay through in the gap
#: between the record being pruned and the nonce expiring.
NONCE_TTL_SECONDS: Final = NONCE_TTL.total_seconds()

#: How long a consumption record must outlive the nonce it records. Zero would be enough in
#: principle, because an expired nonce is refused by `bind` anyway, but the two clocks are
#: not the same clock: the ledger's pruning runs on the storage side. Slack means a record is
#: never pruned while the nonce it guards could still pass its own expiry check.
CONSUMPTION_SLACK: Final = 60


class NonceLedger(Protocol):
    """Records that a nonce has been used, and answers whether this caller was first.

    **There is deliberately no `was_consumed` here.** A protocol with a read and a separate
    write is a protocol whose only correct use is a transaction the caller has to remember,
    and whose racy use type-checks perfectly. `consume` returning a bool means the storage
    engine performs the test and the set together, and a caller cannot spell the version with
    a gap in the middle.

    Implementations: a unique insert whose duplicate-key failure means false, or `SET NX` on
    a key with a TTL. Both are one round trip and both are atomic without a transaction.
    """

    def consume(self, nonce_digest: str, *, now: datetime, ttl_seconds: int) -> bool: ...


def nonce_digest(nonce: BindingNonce) -> str:
    """What the ledger stores instead of the nonce.

    A digest rather than the value, because the ledger is a table of live credentials
    otherwise: anything that can read it during the ten-minute window can present what it
    finds. Salted by principal and channel so the same nonce value, if two mints ever
    collided, is two records rather than one that consumes both.
    """
    parts = (nonce.principal_id, nonce.channel.value, nonce.value)
    blob = "".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BindingOutcome:
    """A completed binding, and what it displaced.

    `revoked` is the binding this one replaced on the same channel, or None. Returned rather
    than quietly dropped because somebody has to write it to the audit ledger, and a
    revocation nobody recorded is a revocation nobody can explain later.
    """

    binding: Binding
    revoked: Binding | None = None


def bind_once(
    nonce: BindingNonce,
    presented: str,
    event: ChannelEvent,
    *,
    now: datetime,
    ledger: NonceLedger,
    existing: Iterable[Binding] = (),
) -> BindingOutcome:
    """Bind, exactly once, and refuse every way this could bind the wrong person.

    **The nonce is consumed after `bind` has validated it, not before.** Consuming first
    would let anybody who can send a message on that channel burn a nonce they cannot use, by
    presenting a wrong value: the real person's next attempt would then fail for a reason
    nobody can see, and the only remedy is minting another, which is the same denial one
    message later. Validate, then consume, then take effect.

    The order after that matters too: the identity check comes before the channel-conflict
    check, because binding one identity to two people is an account takeover and binding a
    second identity for one person is ordinary.
    """
    # Raises BindingRefusedError on a bad value, an expired nonce, or the wrong channel.
    fresh = bind(nonce, presented, event, now)

    ttl_seconds = int(NONCE_TTL_SECONDS) + CONSUMPTION_SLACK
    if not ledger.consume(nonce_digest(nonce), now=now, ttl_seconds=ttl_seconds):
        msg = (
            "this nonce has already been used. It travels through the channel, so anything "
            "that can read the message can present it; one use is what bounds that to the "
            "person it was minted for."
        )
        raise BindingRefusedError(msg)

    live = tuple(existing)
    taken = _bound_to_somebody_else(fresh, live)
    if taken is not None:
        msg = (
            f"this identity is already bound to {taken.principal_id}. Refused rather than "
            "resolved: an identity moving between people is either a takeover or a mistake, "
            "and nothing here can tell those apart."
        )
        raise BindingRefusedError(msg)

    return BindingOutcome(binding=fresh, revoked=_same_channel_binding(fresh, live))


def _bound_to_somebody_else(fresh: Binding, live: Iterable[Binding]) -> Binding | None:
    """An existing binding for this identity that belongs to a different principal.

    **The channel comparison here is redundant and `_same_channel_binding`'s is not**, which
    is worth stating because the two predicates look identical. `ingress.identity_hash` is
    salted by channel, so two hashes are equal only when the channels already are; matching on
    the hash has therefore matched the channel. Removing it changes nothing, and a mutation
    proved that rather than an argument.

    It is load-bearing three lines further down because that predicate matches on the
    principal and on hashes being *different*, which is true across channels all day: without
    it, adding Lark revokes the person's email binding.

    Kept because the redundancy rests on a property of another module. If `identity_hash` ever
    stopped salting by channel, this would silently become the only thing standing between a
    binding on a weak channel and one on a strong channel, which is the exact failure that
    docstring says the salt exists to prevent. There is a test pinning that premise.
    """
    for other in live:
        if (
            other.channel is fresh.channel
            and other.identity_hash == fresh.identity_hash
            and other.principal_id != fresh.principal_id
        ):
            return other
    return None


def _same_channel_binding(fresh: Binding, live: Iterable[Binding]) -> Binding | None:
    """This principal's previous binding on this channel, which the new one replaces."""
    for other in live:
        if (
            other.channel is fresh.channel
            and other.principal_id == fresh.principal_id
            and other.identity_hash != fresh.identity_hash
        ):
            return other
    return None


def unbind(
    principal_id: str,
    channel: Channel,
    live: Iterable[Binding],
) -> tuple[Binding, ...]:
    """The bindings to delete so this person no longer reaches us on this channel.

    Returns what to delete rather than a flag to set. A revoked binding that stays in the
    table is subtractive state, and every read afterwards has to remember to exclude it;
    forget once and a revoked channel is answering again. The audit ledger is where the fact
    that it existed survives.

    Returns every match rather than the first, so a table that has somehow accumulated two
    for one person is cleaned rather than half-cleaned.
    """
    return tuple(b for b in live if b.channel is channel and b.principal_id == principal_id)


def apply_unbind(
    principal_id: str,
    channel: Channel,
    live: Mapping[str, Binding],
) -> dict[str, Binding]:
    """What the binding table looks like afterwards. Keyed by identity hash, as `resolve` is.

    A pure function over the map so the caller can diff it, and so this is testable without a
    database. Nothing here writes.
    """
    doomed = {b.identity_hash for b in unbind(principal_id, channel, live.values())}
    return {k: v for k, v in live.items() if k not in doomed}


def would_replay(nonce: BindingNonce, consumed: Iterable[str]) -> bool:
    """Whether this nonce has already been recorded as used, for a console to show.

    Read-only and advisory. `bind_once` does not call it: the decision has to be the atomic
    consume, and a check here followed by a consume there is the race this module exists to
    remove. It is here so a console can say "that code has been used" without inventing its
    own idea of what used means.
    """
    return nonce_digest(nonce) in set(consumed)
