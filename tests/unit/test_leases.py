"""Per-run leases: every test is a way a borrowed credential outlives its run, or leaks.

The module under test is pure apart from `hold`, which reaches a vault only through the
`Vault` protocol, so the fake below is a few lines rather than a server. Nothing here
contacts OpenBao; `tests/unit/test_openbao.py` is where the wire format is tested.

Task ids: M11.2.2
"""

from __future__ import annotations

import dataclasses
import logging
import operator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain.connectors.contract import CREDENTIAL_ATTRIBUTE_RE
from brain.identity.packs import subtractive_state
from brain.ops import leases as leases_module
from brain.ops.leases import (
    LEASE_ABSOLUTE_MAX,
    ROLES_THAT_MAY_RENEW,
    SEALED_RENDERING,
    LeaseLifecycleError,
    LeaseRecord,
    RunHoldings,
    RunLease,
    SealedSecret,
    for_run,
    hold,
    renew,
    renewable_for,
)
from brain.ops.secrets import (
    MAX_LEASE,
    Lease,
    SecretRef,
    SecretsUnavailableError,
    VaultRole,
)

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
SECRET = "v-xero-9f:this-string-belongs-in-no-log"
REF = SecretRef(path="connectors/creds/xero", role=VaultRole.APPLICATION)
WORKER_REF = SecretRef(path="connectors/creds/lark_base", role=VaultRole.WORKER)
BROWSER_REF = SecretRef(path="browser/creds/portal", role=VaultRole.BROWSER_RUNNER)

REPO = Path(__file__).resolve().parents[2]
POLICY_DIR = REPO / "ops" / "openbao" / "policies"


def a_lease(
    *,
    ref: SecretRef = REF,
    borrowed_at: datetime = NOW,
    term: timedelta = MAX_LEASE,
    renewals: int = 0,
) -> RunLease:
    """A lease as `for_run` would have built one, with the pieces a test wants to vary."""
    return RunLease(
        run_id="r_18",
        lease_id="connectors/creds/xero/abc123",
        ref=ref,
        secret=SealedSecret(SECRET),
        borrowed_at=borrowed_at,
        expires_at=borrowed_at + term,
        renewals=renewals,
    )


def a_vault_lease(*, ref: SecretRef = REF, term: timedelta = MAX_LEASE) -> Lease:
    return Lease(
        lease_id="connectors/creds/xero/abc123",
        ref=ref,
        secret=SECRET,
        issued_at=NOW,
        expires_at=NOW + term,
    )


class FakeVault:
    """A vault that mints one answer and records what it was asked to give back.

    Implements the `Vault` protocol structurally rather than by inheritance, because that is
    the whole point of the protocol: `hold` must work against anything with the two verbs,
    and a test that subclassed `OpenBaoVault` would be testing the subclass.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.issued: list[str] = []
        self.revoked: list[str] = []
        self._fail = fail

    def issue(self, ref: SecretRef, ttl: timedelta) -> Lease:
        if self._fail is not None:
            raise self._fail
        self.issued.append(ref.path)
        return a_vault_lease(ref=ref, term=min(ttl, MAX_LEASE))

    def revoke(self, lease_id: str) -> None:
        self.revoked.append(lease_id)


# --------------------------------------------------------------- the expiry is not optional
def test_a_lease_cannot_be_constructed_without_an_expiry() -> None:
    """The strongest form of "a lease always has an end" is that the language refuses one
    without it. Delete this and `expires_at` can acquire a default in a later edit, at which
    point every lease built by a caller that forgot it runs until the default says so, which
    is the standing credential this whole module exists to prevent."""
    with pytest.raises(TypeError, match="expires_at"):
        RunLease(  # type: ignore[call-arg]
            run_id="r_18",
            lease_id="connectors/creds/xero/abc123",
            ref=REF,
            secret=SealedSecret(SECRET),
            borrowed_at=NOW,
        )


def test_the_expiry_field_carries_no_default_of_any_kind() -> None:
    """The structural half of the test above, and the one that survives a refactor changing
    how the class is built. Deleting it means a `default_factory` returning a far-future
    datetime would satisfy the TypeError test by never raising."""
    expiry = {f.name: f for f in dataclasses.fields(RunLease)}["expires_at"]
    assert expiry.default is dataclasses.MISSING
    assert expiry.default_factory is dataclasses.MISSING


def test_a_lease_that_ends_before_it_was_borrowed_is_refused() -> None:
    """An inverted pair is an expired lease at the moment of issue, and `is_live` would
    answer False for a credential the vault has just minted. Deleting this lets a clock skew
    at the issuing end produce a lease nothing can ever read, reported as a source error."""
    with pytest.raises(LeaseLifecycleError, match="expires before it was borrowed"):
        RunLease(
            run_id="r_18",
            lease_id="lease-1",
            ref=REF,
            secret=SealedSecret(SECRET),
            borrowed_at=NOW,
            expires_at=NOW - timedelta(minutes=1),
        )


def test_a_naive_expiry_is_refused() -> None:
    """This machine is eight hours from UTC. Deleting this lets a naive expiry through, and
    an eight-hour error on a four-hour ceiling is a lease that is either long dead or has
    twice the life it was meant to have, with nothing reporting either."""
    with pytest.raises(LeaseLifecycleError, match="no time zone"):
        RunLease(
            run_id="r_18",
            lease_id="lease-1",
            ref=REF,
            secret=SealedSecret(SECRET),
            borrowed_at=NOW,
            expires_at=datetime(2026, 9, 6, 10, 0),  # a naive timestamp, which is the point
        )


def test_a_lease_longer_than_the_absolute_maximum_cannot_be_constructed_at_all() -> None:
    """The ceiling is checked in the constructor, so there is no moment at which an
    over-long lease is a value in this process. Deleting this leaves the ceiling enforced
    only inside `renew`, and a caller building a `RunLease` directly walks past it."""
    with pytest.raises(LeaseLifecycleError, match="from the first borrow"):
        RunLease(
            run_id="r_18",
            lease_id="lease-1",
            ref=REF,
            secret=SealedSecret(SECRET),
            borrowed_at=NOW,
            expires_at=NOW + LEASE_ABSOLUTE_MAX + timedelta(seconds=1),
        )


def test_a_lease_inside_the_ceiling_is_built_and_can_be_read() -> None:
    """The positive sibling. A guard tested only by its refusals is satisfied by a
    constructor that refuses everything, and deleting this hides exactly that."""
    lease = a_lease()
    assert lease.is_live(NOW)
    assert lease.reveal(NOW) == SECRET
    assert lease.not_after == NOW + LEASE_ABSOLUTE_MAX


# ------------------------------------------------------------------- an expired lease
def test_an_expired_lease_refuses_to_be_read() -> None:
    """The source may accept an expired credential if its clock differs or the revocation
    has not propagated, and a credential working after we believed it withdrawn is the
    failure leasing exists to prevent. Deleting this makes `reveal` an attribute read."""
    lease = a_lease()
    with pytest.raises(LeaseLifecycleError, match="expired at"):
        lease.reveal(NOW + MAX_LEASE + timedelta(seconds=1))


def test_a_lease_is_not_live_at_the_instant_it_expires() -> None:
    """The boundary, which is the case a rewrite gets wrong by choosing `<=`. Deleting this
    leaves a one-instant window in which an expired lease reads as live, and that window is
    exactly where a retry loop lands."""
    lease = a_lease()
    assert lease.is_live(lease.expires_at - timedelta(microseconds=1))
    assert not lease.is_live(lease.expires_at)
    assert lease.remaining(lease.expires_at + timedelta(hours=1)) == timedelta()


def test_a_refused_read_raises_something_a_secrets_handler_already_catches() -> None:
    """Callers wrap borrowing in `except SecretsUnavailableError`. Deleting this lets the
    error hierarchy be split later, after which a badly shaped lease escapes the handler
    that would have turned it into an honest failure and crashes the request instead."""
    with pytest.raises(SecretsUnavailableError):
        a_lease().reveal(NOW + timedelta(days=1))


# ----------------------------------------------------------------------------- renewal
def test_renewal_cannot_push_a_lease_past_the_absolute_maximum() -> None:
    """The whole claim of the module: a lease that can be renewed for ever is not a lease.
    Renewals are granted a full term each time and the total life still stops at the
    ceiling. Deleting this lets the clamp in `renew`, or the ceiling check in the
    constructor, be removed while every individual renewal still looks correct."""
    lease = a_lease()
    now = NOW
    refusal: LeaseLifecycleError | None = None
    for _ in range(20):
        now += timedelta(minutes=50)
        try:
            lease = renew(lease, now=now, granted=MAX_LEASE)
        except LeaseLifecycleError as exc:
            refusal = exc
            break
        assert lease.expires_at <= NOW + LEASE_ABSOLUTE_MAX

    assert refusal is not None, "renewal was never refused, so the ceiling is not a ceiling"
    assert lease.renewals == 4
    assert lease.expires_at == NOW + LEASE_ABSOLUTE_MAX


def test_a_lease_already_at_its_ceiling_is_refused_a_renewal_while_it_is_still_live() -> None:
    """A term already clamped to the ceiling cannot be moved, so the holder is told to
    borrow another before it loses the credential mid-operation rather than after. Deleting
    this leaves a renewal that succeeds, reports success and changes nothing."""
    lease = a_lease(term=LEASE_ABSOLUTE_MAX)
    now = NOW + timedelta(hours=3)
    assert lease.is_live(now)
    with pytest.raises(LeaseLifecycleError, match="cannot move it"):
        renew(lease, now=now, granted=MAX_LEASE)


def test_a_renewal_carries_the_first_borrow_forward_so_the_ceiling_does_not_move() -> None:
    """The one line that makes the bound a bound. Set `borrowed_at=now` in `renew` and every
    renewal resets the ceiling, each step reads as correct, and the lease lives for ever.
    Deleting this test is what lets that edit through."""
    lease = a_lease()
    renewed = renew(lease, now=NOW + timedelta(minutes=50), granted=MAX_LEASE)
    assert renewed.borrowed_at == NOW
    assert renewed.not_after == NOW + LEASE_ABSOLUTE_MAX
    assert renewed.renewals == 1


def test_a_renewal_takes_the_term_the_vault_granted_rather_than_the_one_asked_for() -> None:
    """The positive case, and the two-clocks rule `brain.ops.openbao` states: the server's
    duration wins. Deleting this allows a renewal that records the requested term, after
    which the process believes it holds a credential the vault has already withdrawn."""
    lease = a_lease()
    now = NOW + timedelta(minutes=30)
    renewed = renew(lease, now=now, granted=timedelta(minutes=12))
    assert renewed.expires_at == now + timedelta(minutes=12)
    assert renewed.reveal(now) == SECRET


def test_the_term_offered_for_renewal_never_exceeds_what_is_left_under_the_ceiling() -> None:
    """`renewable_for` is what a caller asks the vault for, so an answer larger than the
    headroom produces a lease this module then has to shorten, which is a lease the vault
    believes is longer than we do. Deleting this lets the headroom bound be dropped."""
    lease = a_lease(term=timedelta(hours=3, minutes=50))
    now = NOW + timedelta(hours=3, minutes=45)
    assert renewable_for(lease, now=now, wanted=MAX_LEASE) == timedelta(minutes=15)
    assert renewable_for(lease, now=now, wanted=timedelta(minutes=5)) == timedelta(minutes=5)
    assert renewable_for(a_lease(), now=NOW, wanted=timedelta(days=1)) == MAX_LEASE


def test_an_expired_lease_cannot_be_renewed() -> None:
    """Renewing an expired lease is how a run carries on with a credential it has already
    lost the right to. Deleting this lets a worker that woke up late extend something the
    vault has finished with, and the source may well accept it."""
    lease = a_lease()
    with pytest.raises(LeaseLifecycleError, match="cannot be renewed"):
        renew(lease, now=NOW + MAX_LEASE, granted=MAX_LEASE)


def test_a_browser_runner_may_not_renew_a_lease() -> None:
    """Its vault policy grants revoke and deliberately not renew, because a runner looping on
    a hostile page should lose its credential rather than keep asking for more time. Deleting
    this turns the refusal into a 403 from the vault, which reads as an unloaded policy."""
    lease = a_lease(ref=BROWSER_REF)
    with pytest.raises(LeaseLifecycleError, match="may not renew"):
        renew(lease, now=NOW + timedelta(minutes=5), granted=MAX_LEASE)
    with pytest.raises(LeaseLifecycleError, match="may not renew"):
        renewable_for(lease, now=NOW + timedelta(minutes=5))


def test_the_worker_role_may_renew_because_its_own_policy_says_so() -> None:
    """The positive sibling to the refusal above. A role check tested only by the role it
    refuses is satisfied by refusing everybody, which would stop every long sync in the
    system and look like a vault outage."""
    lease = a_lease(ref=WORKER_REF)
    renewed = renew(lease, now=NOW + timedelta(minutes=50), granted=MAX_LEASE)
    assert renewed.renewals == 1


def test_the_roles_that_may_renew_are_exactly_those_their_vault_policy_grants_renew_to() -> None:
    """`ROLES_THAT_MAY_RENEW` and `ops/openbao/policies/*.hcl` are two statements of one
    rule, and drift between them fails in only one direction visibly. A role this allows and
    the policy does not gives a 403 at three in the morning; the reverse silently declines a
    renewal a long sync was entitled to. Deleting this lets either happen unnoticed."""
    policies = sorted(POLICY_DIR.glob("*.hcl"))
    assert policies, "no vault policies found; the path this test pins has moved"

    granted: set[VaultRole] = set()
    for path in policies:
        role = VaultRole(path.stem.replace("-", "_"))
        if 'path "sys/leases/renew"' in path.read_text(encoding="utf-8"):
            granted.add(role)

    assert granted == set(ROLES_THAT_MAY_RENEW)
    assert VaultRole.BROWSER_RUNNER not in granted


# -------------------------------------------------------------------------- the seal
def test_formatting_a_lease_never_produces_its_secret() -> None:
    """Every ordinary way an object reaches a log, at once. The seal is on the value rather
    than on an overridden `__repr__`, so `asdict`, an f-string over the field itself and a
    logging call all render the same. Deleting this lets the seal be replaced by a container
    override, which the first of those three walks straight past."""
    lease = a_lease()
    holdings = RunHoldings(run_id="r_18").add(lease)
    record = lease.record()
    log = logging.LogRecord("brain", logging.INFO, __file__, 1, "borrowed %s", (lease,), None)

    renderings = [
        repr(lease),
        str(lease),
        f"{lease}",
        f"{lease!r}",
        f"{lease!s}",
        # Percent interpolation, written through `operator.mod` because that is the call a
        # logging call makes and an f-string is not a substitute for testing it.
        operator.mod("%s", (lease,)),
        format(lease),
        repr(lease.secret),
        str(lease.secret),
        f"{lease.secret}",
        f"{lease.secret:>40}",
        str(dataclasses.asdict(lease)),
        repr(holdings),
        repr(record),
        str(lease.record()),
        repr(holdings.ledger()),
        log.getMessage(),
        repr(LeaseLifecycleError(f"failed while holding {lease}")),
    ]
    for rendering in renderings:
        assert SECRET not in rendering
        assert "this-string-belongs-in-no-log" not in rendering

    assert SEALED_RENDERING in repr(lease)
    assert lease.reveal(NOW) == SECRET


def test_nothing_that_is_meant_to_be_stored_can_hold_a_credential() -> None:
    """`LeaseRecord` is what gets written down, and a field that could hold a value is a
    field somebody populates while debugging. Checked against the same name pattern
    `brain.connectors.contract` refuses a connector attribute by, so the two agree on what a
    credential-shaped name is. Deleting this lets a `secret` field be added to the record."""
    for field in dataclasses.fields(LeaseRecord):
        assert not CREDENTIAL_ATTRIBUTE_RE.search(field.name.casefold()), field.name
        assert field.type != "SealedSecret", field.name

    holdings = RunHoldings(run_id="r_18").add(a_lease())
    assert holdings.lease_ids == ("connectors/creds/xero/abc123",)
    assert [r.lease_id for r in holdings.ledger()] == ["connectors/creds/xero/abc123"]


def test_an_empty_credential_is_refused_rather_than_sealed() -> None:
    """A vault that answered with nothing has failed. Sealing the emptiness would send an
    empty credential to the source, which answers with a permission error, and the incident
    then reads as a scope problem with the connector rather than an outage in the vault."""
    with pytest.raises(LeaseLifecycleError, match="not a credential"):
        SealedSecret("")


# ------------------------------------------------------------- giving a lease back
def test_returning_a_lease_removes_it_rather_than_flagging_it() -> None:
    """A returned flag is subtractive state: every later read has to remember to exclude it,
    and the read that forgets is the one written during an incident. Deleting this lets
    `release` become a marker, after which "what does this run still hold" is a question
    about evaluation order rather than about a list."""
    first = a_lease()
    second = dataclasses.replace(first, lease_id="connectors/creds/xero/def456")
    holdings = RunHoldings(run_id="r_18").add(first).add(second)

    left = holdings.release(first.lease_id)

    assert len(left.leases) == 1
    assert not left.holds(first.lease_id)
    assert first.lease_id not in left.lease_ids
    assert [r.lease_id for r in left.ledger()] == [second.lease_id]
    assert first.lease_id not in repr(left)


def test_releasing_one_lease_leaves_the_others_held() -> None:
    """The positive sibling. A release that emptied the holdings would pass every assertion
    about the lease that was given back, and would revoke credentials a run is still using."""
    first = a_lease()
    second = dataclasses.replace(first, lease_id="connectors/creds/xero/def456")
    left = RunHoldings(run_id="r_18").add(first).add(second).release(first.lease_id)
    assert left.holds(second.lease_id)


def test_no_type_in_this_module_carries_state_that_subtracts() -> None:
    """The structural statement of the rule above, using the identity package's own detector
    so that the two cannot drift. Deleting this lets a field called `revoked` or `withdrawn`
    be added, which reads as tidier than deletion and reintroduces the deny-list shape the
    whole platform refuses."""
    assert subtractive_state(leases_module) == []


def test_releasing_a_lease_that_is_not_held_is_not_an_error() -> None:
    """Release is the cleanup path and runs twice routinely, once by the code that finished
    and once by whatever sweeps up after a crash. Deleting this lets a refusal be added, and
    a refusal during shutdown loses the list of what had not been given back yet."""
    holdings = RunHoldings(run_id="r_18").add(a_lease())
    assert holdings.release("no-such-lease").lease_ids == holdings.lease_ids


def test_one_lease_cannot_be_held_twice_by_one_run() -> None:
    """Two entries for one lease means releasing it once leaves the other, and the other is
    the one nobody looks for. Deleting this lets a retry that re-adds the same lease produce
    a credential that survives its own revocation in the holdings."""
    lease = a_lease()
    with pytest.raises(LeaseLifecycleError, match="appears twice"):
        RunHoldings(run_id="r_18").add(lease).add(lease)


def test_a_run_cannot_hold_a_lease_another_run_borrowed() -> None:
    """One run releasing another's credentials is a run cleaning up work that is still going
    on. Deleting this lets a shared holdings value be assembled from two runs, after which
    the first to finish revokes the second's connector credential mid-fetch."""
    with pytest.raises(LeaseLifecycleError, match="borrowed by"):
        RunHoldings(run_id="r_19").add(a_lease())


def test_holdings_report_the_ids_of_leases_whose_term_has_run_out() -> None:
    """Ids rather than leases, so the natural thing to do with the result is log it or
    revoke by id and neither can reach a secret. Deleting this removes the only way a
    long-running holder notices it is carrying something it can no longer use."""
    holdings = RunHoldings(run_id="r_18").add(a_lease())
    assert holdings.expired(NOW) == ()
    assert holdings.expired(NOW + MAX_LEASE) == ("connectors/creds/xero/abc123",)


# ------------------------------------------------------------------------ the lifecycle
def test_holding_a_lease_seals_it_and_binds_it_to_the_run() -> None:
    """`hold` is the intended entry point, and it must not hand back the vault's own `Lease`,
    whose `.secret` is a plain string. Deleting this lets the wrapper be simplified into a
    passthrough, and the seal then guards a value nobody is given."""
    vault = FakeVault()
    with hold(vault, REF, run_id="r_18", now=NOW) as lease:
        assert isinstance(lease, RunLease)
        assert isinstance(lease.secret, SealedSecret)
        assert lease.run_id == "r_18"
        assert lease.reveal(NOW) == SECRET
    assert vault.revoked == ["connectors/creds/xero/abc123"]


def test_a_lease_is_given_back_when_the_run_raises() -> None:
    """The exception path is the one nobody tests and the one where a leaked lease matters
    most, because a run that crashed halfway is exactly the run somebody re-runs while the
    first credential is still live. Deleting this lets the `finally` be lost in a rewrite."""
    vault = FakeVault()
    with pytest.raises(RuntimeError), hold(vault, REF, run_id="r_18", now=NOW):
        raise RuntimeError("the fetch failed")
    assert vault.revoked == ["connectors/creds/xero/abc123"]


def test_a_vault_term_is_never_lengthened_by_binding_it_to_a_run() -> None:
    """`for_run` applies our ceiling on top of the vault's answer and never the reverse. The
    vault's duration wins because believing our own number means holding a credential the
    server has already withdrawn. Deleting this lets the `min` become a `max` unnoticed."""
    short = for_run(a_vault_lease(term=timedelta(minutes=5)), run_id="r_18")
    assert short.expires_at == NOW + timedelta(minutes=5)
    assert short.not_after == NOW + LEASE_ABSOLUTE_MAX
