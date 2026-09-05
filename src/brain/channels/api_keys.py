"""API keys for the REST channel: issuing them, checking them, and taking them away.

**A key is a credential, never a principal, and that is the whole shape of this module.**
`brain.identity.sessions.ServiceAccount` already exists and is a standing delegation of its
owner's entitlements narrowed by a declared ceiling, with no grants of its own and no field
that could hold one. A key authenticates *as* one of those. It carries no capabilities, no
owner and no ceiling, because every one of those would be a second place for the answer to
"what may this reach" to live, and the second place is the one that drifts wide.

That is not a hypothetical. The architecture calls a service principal holding its own grants
"union authority, which is the classic escalation": grant the integration one capability its
owner lacks, and the owner who could not see margins can read them through the integration
they administer. A key with its own scope list is that same mistake wearing a different noun.

So `reach_for` is still the only thing that computes what an API caller may see, and it still
intersects with the owner's live entitlements at request time. Nothing here is consulted for
that, and there is nothing here to consult.

**The secret is shown once and never stored.** What is kept is a digest, and the comparison
is constant time. A key compared with `==` leaks its prefix to anyone willing to measure, and
a secret guessed one byte at a time is not a secret.

**SHA-256 rather than bcrypt or argon2, deliberately, and the reason is the input.** A
password KDF exists to make guessing a low-entropy human choice expensive. These secrets are
256 bits from `secrets.token_urlsafe`, so there is nothing to guess and a slow hash buys
nothing but latency on every request. Reject this reasoning the moment a key becomes anything
a person chooses or types, because then it is a password and it needs a password's hash.

**A key is addressed by a handle that is safe to log.** `brn.<handle>.<secret>`: the handle
selects one row so verification is a lookup rather than a scan over every key in the system,
which is both faster and the difference between comparing one digest and comparing all of
them. The handle is what appears in logs, in an audit entry and in a console list. The secret
appears nowhere after the moment it is issued.

**A key may not outlive the account it speaks for.** `ServiceAccount.not_after` is required
for a stated reason: a service account is the credential most likely to be made for one
integration in 2026 and still working in 2031 with nobody able to say what uses it. A key
with a longer expiry than its account reintroduces exactly that, one indirection along.

**Revocation is deletion.** A revoked flag is subtractive state, which
`brain.identity.packs.subtractive_state` refuses across the identity package: it turns "is
this key valid" into a question about evaluation order, and every read afterwards has to
remember to exclude it. The record that a key existed belongs in the audit ledger, which is
append-only and which a delete here cannot reach.

**Rotation is two live keys, briefly, and bounded.** A rotation that requires downtime is a
rotation that does not happen, so a second key may be issued while the first still works.
`MAX_LIVE_KEYS_PER_ACCOUNT` is two rather than unlimited, because "we will tidy up the old
ones later" is how an account ends up with nine keys, of which nobody can say which two are
in use.

This module lives beside the other channels rather than in `identity` because it is the API
channel's front door, and because what a caller may then do is the identity layer's answer
and stays there. It is M10.5.7, and the leaf sits under "Further channels" for the same
reason.

Task ids: M10.5.7
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from brain.identity.sessions import ServiceAccount

#: Bytes of randomness in the secret half. 32 is 256 bits: not guessable, and the reason the
#: digest below needs no key-derivation function.
SECRET_BYTES: Final = 32

#: Bytes in the handle. Shorter than the secret on purpose: it is an identifier and not a
#: credential, it appears in logs, and it only has to be unique among live keys.
HANDLE_BYTES: Final = 8

#: Marks the string as one of ours, so a key pasted into the wrong field is recognisable in a
#: log or a support ticket without anybody having to try it.
PREFIX: Final = "brn"

#: Two, not unlimited. One live key is a rotation that needs downtime, which is a rotation
#: that does not happen; more than two is an account nobody can reason about.
MAX_LIVE_KEYS_PER_ACCOUNT: Final = 2

#: `brn.<handle>.<secret>`. Anchored, because an unanchored pattern matches a key embedded in
#: a longer string and would accept whatever surrounds it.
#:
#: **A full stop separates the parts, and an underscore does not.** `secrets.token_urlsafe`
#: emits base64url, whose alphabet includes `-` and `_`, so `brn_<handle>_<secret>` has no
#: unambiguous split point: the handle group swallows underscores and the boundary lands
#: wherever the engine backtracks to. Found by the round trip failing on a key whose secret
#: happened to contain one.
#:
#: This is the second time this exact mistake has been made in this repository in one day:
#: `brain.ops.limit_store.render_key` joined key segments with a colon that could occur
#: inside a segment, so two different windows could address one row. A separator has to be a
#: character the parts cannot contain, and checking that is one line of reasoning that was
#: skipped twice.
KEY_RE: Final = re.compile(r"^brn\.([A-Za-z0-9_-]{6,32})\.([A-Za-z0-9_-]{20,128})$")


class ApiKeyError(Exception):
    """A key was refused, or could not be issued.

    Deliberately one type with an operator-facing message. What the caller on the other end
    of the request is told is a flat "unauthorised": distinguishing "no such key" from "wrong
    secret" from "expired" tells somebody probing which of the three they achieved, and the
    three are the whole search space.
    """


def _digest(secret: str) -> str:
    """The stored form of a secret. See the module docstring on why this is not a password
    hash: the input is 256 bits of randomness, so there is nothing for a slow hash to slow
    down except us."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKeyRecord:
    """What is kept about a key once it has been issued.

    **There is no `secret` field and there must never be one.** The whole value of storing a
    digest is lost the moment the plaintext sits beside it, and a field that exists gets
    populated by whoever is debugging on the day it would be convenient.

    There is also no capability, scope or ceiling here. That is not an omission: see the
    module docstring. What this key may reach is its service account's question, and the
    account already answers it.
    """

    handle: str
    client_id: str
    digest: str
    issued_at: datetime
    not_after: datetime
    #: Free text an operator writes so a key in a list is identifiable later. Never the
    #: secret, and never trusted: it is displayed, so it is escaped by whoever displays it.
    label: str = ""

    def is_live(self, now: datetime) -> bool:
        return now < self.not_after


@dataclass(frozen=True)
class IssuedKey:
    """A freshly minted key: the record to store, and the secret to show once.

    Two fields rather than one object with the secret on it, so that storing the wrong half
    is a type error rather than a habit. Whatever persists `record` cannot accidentally
    persist `secret`, because it does not have it.
    """

    record: ApiKeyRecord
    #: The full `brn_..._...` string. Shown to the person once, at issue, and never
    #: recoverable afterwards. If it is lost, the answer is to issue another and revoke this
    #: one, which is also what happens if it leaks.
    secret: str


def issue(
    account: ServiceAccount,
    *,
    now: datetime,
    not_after: datetime,
    label: str = "",
    existing: Iterable[ApiKeyRecord] = (),
) -> IssuedKey:
    """Mint a key for a service account.

    **The expiry is capped by the account's own.** A key that outlives the account it speaks
    for is a credential for something that no longer exists, and `ServiceAccount.not_after`
    is required precisely to stop an integration outliving everybody's memory of it. Capped
    rather than refused, because the caller asking for longer is usually asking for "as long
    as possible" and the answer to that is the account's expiry.

    **A third live key is refused rather than silently rotating one out.** Choosing which
    existing key to drop is a decision, and making it here would revoke a key somebody is
    using without anybody deciding to.
    """
    live = [k for k in existing if k.client_id == account.client_id and k.is_live(now)]
    if len(live) >= MAX_LIVE_KEYS_PER_ACCOUNT:
        msg = (
            f"{account.client_id} already has {len(live)} live keys, which is the limit. "
            "Revoke the one being replaced first: choosing it here would take away a key "
            "somebody is using without anybody deciding to."
        )
        raise ApiKeyError(msg)
    if not_after <= now:
        msg = "a key that has already expired is not worth issuing"
        raise ApiKeyError(msg)

    bounded = min(not_after, account.not_after)
    if bounded <= now:
        msg = (
            f"{account.client_id} expires at {account.not_after.isoformat()}, so there is no "
            "window left to issue a key in. Extend the account, deliberately, first."
        )
        raise ApiKeyError(msg)

    handle = secrets.token_urlsafe(HANDLE_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    record = ApiKeyRecord(
        handle=handle,
        client_id=account.client_id,
        digest=_digest(secret),
        issued_at=now,
        not_after=bounded,
        label=label,
    )
    return IssuedKey(record=record, secret=f"{PREFIX}.{handle}.{secret}")


def handle_of(presented: str) -> str:
    """The handle inside a presented key, for looking one row up.

    Parsing before comparing is what makes verification a lookup rather than a scan over
    every key in the system. It also means a malformed string is refused without any digest
    being computed at all.
    """
    match = KEY_RE.match(presented)
    if match is None:
        msg = "not an api key"
        raise ApiKeyError(msg)
    return match.group(1)


def verify(
    presented: str,
    record: ApiKeyRecord,
    account: ServiceAccount,
    *,
    now: datetime,
) -> ServiceAccount:
    """Check a presented key and return the account it speaks for.

    Returns the account rather than a boolean, and that is not decoration: a function
    returning True invites `if verify(...)` followed by the caller deciding for itself which
    account this was, and deciding it from something the caller already had rather than from
    what was proved. Handing back the account means the only thing the caller can act on is
    the thing that was checked.

    Every refusal raises the same type with an operator-facing message. What reaches the
    other end is "unauthorised" whichever check failed, because distinguishing "no such key"
    from "wrong secret" from "expired" hands somebody probing exactly the three answers they
    are looking for.
    """
    match = KEY_RE.match(presented)
    if match is None:
        msg = "not an api key"
        raise ApiKeyError(msg)
    handle, secret = match.group(1), match.group(2)

    # **Redundant for security and kept for the message.** A record fetched under a
    # different handle belongs to a different key, so its digest would not match either and
    # the next check refuses it anyway. A mutation removing this survives the suite, which is
    # correct rather than a gap.
    #
    # It stays because the two failures mean different things to whoever reads the log. A
    # mismatched handle is a bug in the caller that paired them: it looked one record up and
    # verified against another. A mismatched digest is a wrong or stale secret, which is
    # either an attack or an expired integration. Collapsing them would send somebody
    # hunting for a leaked key when the fault is two lines of our own lookup.
    if not secrets.compare_digest(handle, record.handle):
        msg = "key handle does not match the record it was looked up by"
        raise ApiKeyError(msg)

    # Constant time. `==` on a digest leaks its prefix to anyone willing to measure, and a
    # value guessed one byte at a time is not 256 bits of anything.
    if not secrets.compare_digest(_digest(secret), record.digest):
        msg = f"secret does not match key {record.handle}"
        raise ApiKeyError(msg)

    if not record.is_live(now):
        msg = f"key {record.handle} expired at {record.not_after.isoformat()}"
        raise ApiKeyError(msg)

    # The account is checked as well as the key. A key can be within its own expiry while the
    # account it speaks for has ended, and the account's expiry is the one that means the
    # integration is over.
    if record.client_id != account.client_id:
        msg = f"key {record.handle} is not for {account.client_id}"
        raise ApiKeyError(msg)
    if not account.is_active(now):
        msg = f"{account.client_id} is no longer active"
        raise ApiKeyError(msg)

    return account


def revoke(handle: str, records: Iterable[ApiKeyRecord]) -> tuple[ApiKeyRecord, ...]:
    """What is left after revoking one key. Deletion, not a flag.

    A revoked flag is subtractive state: every read afterwards has to remember to exclude it,
    and the read that forgets is the one somebody writes during an incident. The record that
    this key existed belongs in the audit ledger, which a delete here cannot reach.
    """
    return tuple(r for r in records if r.handle != handle)


def loggable(presented: str) -> str:
    """What may be written down about a presented key.

    The handle identifies which key without being one. Anything that fails to parse renders
    as a fixed string rather than as itself: a malformed key is often a real key with a typo,
    or a real key from a different system, and logging the input to find out is how a
    credential ends up in a log with that log's retention rather than its own.
    """
    try:
        return f"{PREFIX}.{handle_of(presented)}"
    except ApiKeyError:
        return "<not an api key>"
