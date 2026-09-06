"""API keys for the REST channel.

Every test here is a way a key becomes more than a credential, outlives what it speaks for,
or leaves the secret somewhere it can be read. What an API caller may then *see* is
`reach_for`'s question and is tested against `brain.identity.sessions`; nothing about
entitlements is re-tested here, because nothing here computes one.

Task ids: M10.5.7
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.api_keys import (
    KEY_RE,
    MAX_LIVE_KEYS_PER_ACCOUNT,
    PREFIX,
    ApiKeyError,
    ApiKeyRecord,
    handle_of,
    issue,
    loggable,
    revoke,
    verify,
)
from brain.core.entitlement import Capability
from brain.identity.sessions import ServiceAccount

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
YEAR = NOW + timedelta(days=365)


def account(client_id: str = "sync-xero", *, not_after: datetime = YEAR) -> ServiceAccount:
    return ServiceAccount(
        client_id=client_id,
        subject="svc-" + client_id,
        owner_principal_id="u_priya",
        ceiling=(Capability(value="read:client.name"),),
        not_after=not_after,
    )


# ----------------------------------------------------------------- it issues at all
def test_a_key_is_issued_and_reads_back_against_its_own_record() -> None:
    """If this fails every refusal below passes for the wrong reason: a module that issues
    nothing and verifies nothing satisfies most of this file."""
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=30))

    assert verify(issued.secret, issued.record, account(), now=NOW).client_id == "sync-xero"


def test_the_secret_is_never_kept_on_the_record() -> None:
    """**The point of storing a digest is lost the moment the plaintext sits beside it**, and
    a field that exists gets populated by whoever is debugging on the day it is convenient.

    Asserted over the record's fields rather than by reading one, so adding `secret` back
    fails here rather than passing quietly with the value unset in this test."""
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=30))
    fields = {f.name for f in dataclasses.fields(issued.record)}

    assert "secret" not in fields
    assert "key" not in fields
    assert issued.secret not in str(issued.record)


def test_a_key_record_carries_no_capability_of_its_own() -> None:
    """**A key with its own scope list is union authority wearing a different noun.** The
    architecture names that as the classic escalation: grant the integration one capability
    its owner lacks and the owner reads it through the integration they administer.

    A `ServiceAccount` already answers what may be reached, and it is a delegation of its
    owner narrowed by a ceiling. A second answer here would be the one that drifts wide.

    Delete this and a `scopes` field is added for a caller who wants a read-only key, which
    is a reasonable-sounding request with no safe implementation at this layer."""
    fields = {f.name for f in dataclasses.fields(ApiKeyRecord)}

    for forbidden in ("capabilities", "scopes", "ceiling", "grants", "permissions"):
        assert forbidden not in fields


# --------------------------------------------------------------------- the secret
def test_a_wrong_secret_is_refused() -> None:
    """The base case. Without it every test below could pass against a verifier that accepts
    anything with the right shape."""
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=30))
    # The right shape and the right handle, so this reaches the digest comparison. Written
    # with the wrong separator it was refused as malformed instead, which passes the same
    # `raises` while testing nothing about the secret at all.
    forged = f"{PREFIX}.{issued.record.handle}.{'x' * 43}"

    assert KEY_RE.match(forged) is not None, "the forged key must parse to test the secret"
    with pytest.raises(ApiKeyError, match="secret does not match"):
        verify(forged, issued.record, account(), now=NOW)


def test_the_secret_is_compared_in_constant_time() -> None:
    """A digest compared with `==` returns as soon as two bytes differ, so the time taken
    says how much of the guess was right, and a value guessed one byte at a time is not 256
    bits of anything.

    Asserted on the source, because timing is not observable in a unit test on a machine
    running other work, and asserting it badly would produce a test that fails at random.
    The behaviour is identical either way, so only the text says which comparison is used,
    which is exactly when a source assertion earns its place."""
    import inspect

    from brain.channels import api_keys

    source = inspect.getsource(api_keys.verify)
    assert "secrets.compare_digest(_digest(secret), record.digest)" in source
    assert "_digest(secret) == record.digest" not in source


def test_a_string_that_is_not_a_key_is_refused_before_anything_is_hashed() -> None:
    """A malformed string is refused by shape, so no digest is computed and no record is
    consulted. It also means `loggable` has something to say about it without trying it."""
    for rubbish in ("", "hello", "brn_short_x", "Bearer abc", PREFIX + "__"):
        with pytest.raises(ApiKeyError):
            handle_of(rubbish)


def test_the_pattern_does_not_match_a_key_buried_in_a_longer_string() -> None:
    """Anchored, because an unanchored pattern accepts whatever surrounds the key: a header
    value with trailing junk, or two keys concatenated, would parse as one."""
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=30))

    assert KEY_RE.match(issued.secret) is not None
    assert KEY_RE.match("Bearer " + issued.secret) is None
    assert KEY_RE.match(issued.secret + " trailing") is None


def test_two_keys_are_never_the_same() -> None:
    """Both halves come from `secrets`. A handle collision would make one key's lookup find
    another's record, and a secret collision needs no explanation."""
    made = [issue(account(), now=NOW, not_after=NOW + timedelta(days=30)) for _ in range(50)]

    # The *secret half*, not the whole string. Comparing whole keys hides a fixed secret
    # behind a varying handle, and a mutation setting every secret to one value survived
    # this test until it was written this way.
    secrets_only = {k.secret.rsplit(".", 1)[1] for k in made}

    assert len(secrets_only) == 50
    assert len({k.record.handle for k in made}) == 50
    assert len({k.record.digest for k in made}) == 50


# ------------------------------------------------------------------- what it outlives
def test_a_key_cannot_outlive_the_account_it_speaks_for() -> None:
    """`ServiceAccount.not_after` is required for a stated reason: a service account is the
    credential most likely to be made for one integration in 2026 and still working in 2031
    with nobody able to say what uses it. A key with a longer expiry reintroduces exactly
    that, one indirection along.

    Delete this and a key issued "for two years" against an account expiring next month
    keeps working after the account is gone."""
    short = account(not_after=NOW + timedelta(days=7))

    issued = issue(short, now=NOW, not_after=NOW + timedelta(days=365))

    assert issued.record.not_after == short.not_after


def test_an_expired_key_is_refused() -> None:
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=1))

    with pytest.raises(ApiKeyError):
        verify(issued.secret, issued.record, account(), now=NOW + timedelta(days=2))


def test_a_live_key_for_a_dead_account_is_refused() -> None:
    """The account's expiry is the one that means the integration is over, and a key can sit
    inside its own window while that has passed. Checking only the key would let a
    decommissioned integration keep working until its key happened to lapse."""
    short = account(not_after=NOW + timedelta(days=2))
    issued = issue(short, now=NOW, not_after=NOW + timedelta(days=2))
    later = NOW + timedelta(days=3)

    with pytest.raises(ApiKeyError):
        verify(issued.secret, issued.record, short, now=later)


def test_a_key_that_outlives_its_account_is_still_refused_when_the_account_ends() -> None:
    """The account check in `verify`, isolated.

    `issue` caps a key's expiry at its account's, so in anything this module mints the key
    expires first and the key-expiry check gets there before this one. That made the account
    check look redundant: a mutation removing it survived every other test here.

    It is not redundant, because `verify` takes a record and an account from two separate
    lookups and a record can predate the cap, arrive from a restore, or be written by hand.
    Constructed directly for that reason, which is the only way to reach the branch.

    Delete this and the account's expiry stops meaning anything on the key path, so a
    decommissioned integration keeps working until its key happens to lapse."""
    acct = account(not_after=NOW + timedelta(days=1))
    issued = issue(acct, now=NOW, not_after=NOW + timedelta(days=1))
    overlong = dataclasses.replace(issued.record, not_after=NOW + timedelta(days=365))
    after_the_account_ends = NOW + timedelta(days=2)

    assert overlong.is_live(after_the_account_ends), "the fixture did not outlive the account"
    with pytest.raises(ApiKeyError, match="no longer active"):
        verify(issued.secret, overlong, acct, now=after_the_account_ends)


def test_a_key_is_refused_against_a_different_account() -> None:
    """A record and an account arrive from two lookups, and nothing but this makes them agree.
    Without it, a key for the reporting integration verifies against the finance one if a
    caller pairs them wrongly, and the reach would then be the finance account's."""
    issued = issue(account("reporting"), now=NOW, not_after=NOW + timedelta(days=30))

    with pytest.raises(ApiKeyError):
        verify(issued.secret, issued.record, account("finance"), now=NOW)


def test_a_key_that_has_already_expired_is_not_issued() -> None:
    """Matched on the message, because the account-window check below also raises here and
    the two mean different things: "you asked for a date in the past" is a caller mistake,
    "this account has no window left" is a fact about the integration. A bare `raises` let a
    mutation removing this check pass, caught by the other one."""
    with pytest.raises(ApiKeyError, match="not worth issuing"):
        issue(account(), now=NOW, not_after=NOW - timedelta(days=1))


def test_no_key_is_issued_for_an_account_with_no_window_left() -> None:
    """Refused rather than clamped to zero, because a key valid for no time reads in a
    console as a key that exists."""
    spent = account(not_after=NOW - timedelta(days=1))

    with pytest.raises(ApiKeyError, match="no window left"):
        issue(spent, now=NOW, not_after=NOW + timedelta(days=30))


# --------------------------------------------------------------- rotation and removal
def test_a_second_key_may_be_live_so_rotation_needs_no_downtime() -> None:
    """A rotation that requires downtime is a rotation that does not happen. Both keys work
    during the overlap, which is the whole point of allowing two."""
    acct = account()
    first = issue(acct, now=NOW, not_after=NOW + timedelta(days=30))
    second = issue(acct, now=NOW, not_after=NOW + timedelta(days=30), existing=[first.record])

    assert verify(first.secret, first.record, acct, now=NOW).client_id == "sync-xero"
    assert verify(second.secret, second.record, acct, now=NOW).client_id == "sync-xero"


def test_a_third_live_key_is_refused_rather_than_rotating_one_out() -> None:
    """Choosing which existing key to drop is a decision, and making it here revokes a key
    somebody is using without anybody deciding to.

    Delete this and "we will tidy up the old ones later" produces an account with nine keys,
    of which nobody can say which two are in use."""
    acct = account()
    live = [
        issue(acct, now=NOW, not_after=NOW + timedelta(days=30)).record
        for _ in range(MAX_LIVE_KEYS_PER_ACCOUNT)
    ]

    with pytest.raises(ApiKeyError, match="limit"):
        issue(acct, now=NOW, not_after=NOW + timedelta(days=30), existing=live)


def test_an_expired_key_does_not_count_towards_the_limit() -> None:
    """Otherwise an account reaches its ceiling permanently and can never be rotated again,
    which turns a safety limit into a reason to raise the limit."""
    acct = account()
    dead = issue(acct, now=NOW, not_after=NOW + timedelta(days=1)).record
    other = issue(acct, now=NOW, not_after=NOW + timedelta(days=365)).record
    later = NOW + timedelta(days=2)

    fresh = issue(acct, now=later, not_after=later + timedelta(days=30), existing=[dead, other])

    assert fresh.record.handle not in {dead.handle, other.handle}


def test_another_accounts_keys_do_not_count_towards_this_ones_limit() -> None:
    """The limit is per account. Counting every key in the system would make a busy
    deployment unable to issue any."""
    theirs = [
        issue(account("other"), now=NOW, not_after=NOW + timedelta(days=30)).record
        for _ in range(MAX_LIVE_KEYS_PER_ACCOUNT)
    ]

    mine = issue(account("mine"), now=NOW, not_after=NOW + timedelta(days=30), existing=theirs)

    assert mine.record.client_id == "mine"


def test_revoking_removes_the_row_rather_than_flagging_it() -> None:
    """A revoked flag is subtractive state: every read afterwards has to remember to exclude
    it, and the read that forgets is the one somebody writes during an incident. The record
    that the key existed belongs in the audit ledger, which a delete here cannot reach.

    Delete this and a `revoked_at` column is added because it reads as more auditable, which
    is the failure mode the identity package refuses everywhere else."""
    acct = account()
    doomed = issue(acct, now=NOW, not_after=NOW + timedelta(days=30)).record
    kept = issue(acct, now=NOW, not_after=NOW + timedelta(days=30)).record

    left = revoke(doomed.handle, [doomed, kept])

    assert [r.handle for r in left] == [kept.handle]
    assert not any(hasattr(r, "revoked_at") for r in left)


def test_revoking_a_handle_nobody_holds_changes_nothing() -> None:
    """So removal cannot be written as "return an empty tuple", which would satisfy the test
    above and revoke everything."""
    acct = account()
    kept = issue(acct, now=NOW, not_after=NOW + timedelta(days=30)).record

    assert revoke("not-a-handle", [kept]) == (kept,)


# ------------------------------------------------------------------------- logging
def test_what_is_logged_identifies_the_key_without_being_one() -> None:
    """The handle selects a row; the secret is the credential. A log line is read by more
    people, for longer, than the key ever was."""
    issued = issue(account(), now=NOW, not_after=NOW + timedelta(days=30))

    line = loggable(issued.secret)

    assert issued.record.handle in line
    assert issued.secret not in line


def test_something_that_is_not_a_key_is_not_echoed_into_the_log() -> None:
    """A malformed key is often a real key with a typo, or a real key from another system.
    Logging the input to find out is how a credential lands in a log with that log's
    retention rather than its own.

    Delete this and the obvious debugging change - print what we got - puts working
    credentials into the log line for every fat-fingered request."""
    line = loggable("brn_realhandle_ThisLooksLikeARealSecretValue")

    assert "ThisLooksLikeARealSecret" not in line
    assert line == "<not an api key>"


def test_no_message_from_this_module_distinguishes_the_three_failures() -> None:
    """ "No such key", "wrong secret" and "expired" are the whole search space for somebody
    probing, and telling them apart tells them which of the three they achieved.

    The operator's message may say which, because it goes to a log. What must not happen is
    a distinct *type* per failure, because a type is what a caller branches on and turns into
    three different responses without meaning to."""
    import inspect

    from brain.channels import api_keys

    raised = {
        node.split("(")[0].strip()
        for node in re.findall(r"raise\s+(\w+)", inspect.getsource(api_keys))
    }

    assert raised == {"ApiKeyError"}


# ------------------------------------------- the half of M10.5.7 that does not exist yet
def test_no_rest_api_is_served_and_the_module_says_so() -> None:
    """**M10.5.7 reads "REST API with key management" and only the key management exists.**

    The application serves `/health/live`, `/health/ready` and the build pages. No route
    invokes a tool, answers a question or accepts an API key, so nothing in `api_keys` has
    ever been asked to authenticate anybody.

    That is worth a test rather than a comment because the leaf is marked done on a tracker a
    client reads. The counters that normally catch a gap cannot see this one:
    `sweep_traceability` checks a claim has a test and this one does, and nothing anywhere
    checks that a claim covers the whole of what its leaf says.

    So the absence is asserted directly. **When somebody adds the API this test fails**, and
    failing is the point: it forces the paragraph in `api_keys`'s docstring to be rewritten
    at the moment it stops being true, which is the only mechanism available for keeping a
    claim honest about its own scope.

    Delete this and the tracker keeps saying a REST API was delivered."""
    from brain.app import Settings, create_app

    app = create_app(Settings(env="development", run_migrations=False))
    served = {path for route in app.routes if (path := getattr(route, "path", None)) is not None}

    # Everything the application actually answers today. Documentation, health, and the
    # build pages the client reads; nothing behind the gate.
    expected_prefixes = (
        "/health",
        "/build",
        "/api/status.json",
        "/docs",
        "/redoc",
        "/openapi",
        "/admin",
        "/me",
    )
    unexpected = sorted(p for p in served if not p.startswith(expected_prefixes))

    assert not unexpected, (
        f"a route appeared that is not health, docs or a build page: {unexpected}. If this is "
        "the REST API, M10.5.7 is finally whole and the docstring in brain.channels.api_keys "
        "saying it does not exist must be rewritten"
    )


def test_the_module_discloses_that_the_api_half_is_missing() -> None:
    """The guard on the disclosure itself. The test above asserts the absence; this asserts
    somebody reading the module is told about it.

    An accurate absence that nobody documents is how the next person builds against a claim
    the tracker made and the code never met."""
    import inspect

    from brain.channels import api_keys

    # Whitespace collapsed before matching. The sentence wraps in the source, so a literal
    # search finds "There is no REST\nAPI" and fails on a docstring that says exactly the
    # right thing. Asserting on prose is usually the wrong move; here the prose is the
    # artefact under test, because the disclosure is the deliverable.
    doc = " ".join((inspect.getdoc(api_keys) or "").split())

    assert "There is no REST API" in doc
    assert "HALF OF M10.5.7 DOES NOT EXIST" in doc
