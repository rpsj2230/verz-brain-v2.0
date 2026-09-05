"""A credential borrowed for one run, with an end it cannot argue its way past.

`brain.ops.secrets` says what a lease is and `brain.ops.openbao` mints one against a real
server. Neither answers the question this module exists for, which is **what stops a run
holding a credential for ever.** A connector credential read once at startup and kept in
memory is a long-lived secret with extra steps: rotating it needs a redeploy, revoking it
does nothing until one, and any dump of the process has it. `borrow` already revokes in a
`finally`, so it covers the run that ends. This covers the run that does not.

**The expiry is required, and the ceiling is not a field.** `expires_at` has no default, so
the language refuses a lease with no end before any validator runs. The total life ceiling,
`not_after`, is *derived* from `borrowed_at` rather than stored. That was the design
decision worth arguing: a stored ceiling is a ceiling that a renewal recomputes, and a
renewal that recomputes the ceiling from its own clock is a lease that renews for ever while
every individual step looks correct. Deriving it means `renew` cannot move it without moving
`borrowed_at`, which is the one field renewal deliberately carries forward untouched.

**Renewal is bounded by wall-clock life, not by a count.** A maximum renewal count was the
first shape and it does not hold: a run refused its fourth renewal simply asks for a longer
term next time, and a count says nothing about how long the credential has actually been
live. `LEASE_ABSOLUTE_MAX` is the bound that means something, and it is independent of how
often, or how little, a holder renews. This is the same pair `brain.identity.sessions` uses
(`SESSION_IDLE` renewable, `SESSION_ABSOLUTE_MAX` not) and the same reason.

**The secret is sealed in the field rather than hidden behind a repr.** `Lease` overrides
`__repr__` and `__str__`, which is right for `Lease` and is not enough one level down: an
override is defeated by `dataclasses.asdict`, by an f-string interpolating the field itself,
by a logging call passing the field as an argument, and by an exception constructed with it.
`SealedSecret` moves the guarantee onto the value, so every rendering path anywhere reaches
`SealedSecret.__repr__` and there is no path left that renders the characters. `RunLease`
therefore keeps the generated dataclass repr on purpose: if the sealing stopped working the
default repr would print the secret, so the test that formats a lease is testing the seal
rather than an override.

**Nothing that persists ever carries a value.** `LeaseRecord` is what may be written down
and has no field that could hold a credential; `held_lease_ids` returns ids the way
`provider_keys.load_into_environment` returns names. A lease id is loggable, and is the
thing an operator needs in order to revoke; the secret is not, and is never returned by
anything whose result is meant to be stored.

**Giving a lease back is deleting it.** `RunHoldings.release` returns holdings without that
lease. A returned flag would be subtractive state, which `brain.identity.packs`
`subtractive_state` refuses across the identity package for a reason that applies exactly
here: it turns "does this run still hold that credential" into a question about evaluation
order, and the read that forgets to exclude the flag is the one somebody writes during an
incident.

**A browser runner may not renew, and that is not a new rule.**
`ops/openbao/policies/browser-runner.hcl` already says so, with the argument: a runner stuck
in a loop on a hostile page is the thing that should lose its credential rather than keep
asking for more time. Encoding it here means the refusal happens before the request instead
of arriving as a 403 that reads as a misconfigured policy.

Scope: domain logic and one lifecycle helper. Nothing here opens a connection or reads a
clock; `now` is a parameter, and `hold` reaches the vault only through the `Vault` protocol.

Task ids: M11.2.2
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from brain.ops.secrets import (
    DEFAULT_LEASE,
    MAX_LEASE,
    Lease,
    SecretRef,
    SecretsUnavailableError,
    Vault,
    VaultRole,
    borrow,
)

# ------------------------------------------------------------------ written-down reasons
#: Why the total life is a wall-clock bound rather than a cap on how often a lease renews.
RENEWAL_IS_BOUNDED_BY_TIME_NOT_BY_COUNT = (
    "A cap on renewals is defeated by asking for a longer term, and it measures the wrong "
    "thing anyway: what matters is how long the credential has been live, not how many "
    "times somebody asked. Three renewals of an hour and one renewal of three hours are the "
    "same exposure and only one of them trips a count. So the bound is LEASE_ABSOLUTE_MAX, "
    "measured from the first borrow, and it holds however often the holder renews."
)

#: Why the ceiling is derived from `borrowed_at` instead of being stored beside it.
THE_CEILING_IS_DERIVED_SO_A_RENEWAL_CANNOT_MOVE_IT = (
    "A stored not_after is a value some later renewal recomputes, and a renewal that "
    "recomputes the ceiling from its own clock renews for ever while every individual step "
    "reads as correct. Deriving it from borrowed_at means the only way to lengthen the "
    "total life is to move the moment the run first borrowed, which is the one field renew "
    "carries forward untouched and the one a reviewer would question."
)

#: Why the secret is a sealed value rather than a plain string behind an overridden repr.
THE_SEAL_IS_ON_THE_VALUE_NOT_ON_THE_CONTAINER = (
    "An overridden __repr__ protects one object. It does nothing for dataclasses.asdict, "
    "for an f-string interpolating the field itself, for a logging call passing the field "
    "as an argument, or for an exception constructed with it, and those are the ordinary "
    "ways a credential reaches a log. Sealing the value means every rendering path in the "
    "process reaches the same __repr__, and there is no remaining path that renders the "
    "characters. The cost is that reading the secret has to be written down deliberately, "
    "which is the point."
)

#: Why returning a lease early removes the row rather than marking it.
GIVING_A_LEASE_BACK_IS_A_DELETION = (
    "A returned flag is subtractive state: every later read has to remember to exclude it, "
    "and the read that forgets is the one somebody writes during an incident. It also makes "
    "'what does this run still hold' a question about evaluation order rather than about a "
    "list. The record that a lease existed belongs in the audit ledger, which is append-only "
    "and which a delete here cannot reach. This is brain.identity.packs.subtractive_state's "
    "rule applied to a credential instead of to a grant."
)

#: Why a browser runner is refused a renewal here as well as in its vault policy.
A_BROWSER_RUNNER_LOSES_ITS_CREDENTIAL_RATHER_THAN_EXTENDS_IT = (
    "ops/openbao/policies/browser-runner.hcl grants revoke and deliberately not renew: a "
    "runner stuck in a loop on a hostile page is exactly the thing that should lose its "
    "credential rather than keep asking for more time. Refusing here as well turns that into "
    "a refusal the caller can read, instead of a 403 from the vault that reads as a policy "
    "somebody forgot to load."
)


# ---------------------------------------------------------------------------- the ceiling
#: The longest a borrowed credential may live from the first borrow, however often it is
#: renewed. Four hours, and the number is a judgement so it is worth saying what it judges.
#:
#: `MAX_LEASE` is one hour and caps a single term. Four hours is four terms, so renewal is a
#: real mechanism (a sync that legitimately runs past an hour can extend three times) and a
#: bounded one (the fourth extension is refused). The ratio is the same four that
#: `brain.channels.widget.WIDGET_SESSION_ABSOLUTE_MAX` chooses over its idle window, and for
#: the same reason: a holder kept alive by a loop is gone within the ceiling rather than at
#: closing time.
#:
#: It is deliberately shorter than `SESSION_ABSOLUTE_MAX`, which is ten hours. A person's
#: session is ten hours because a person is present and three independent mechanisms can end
#: it early: the logout floor, deactivating the principal, and the identity provider
#: declining the next refresh. A lease is held by a process nobody is watching, which
#: `ops/openbao/policies/worker.hcl` says in as many words, so the clock is the only
#: mechanism there is and it has to run out sooner.
#:
#: Four hours matches `brain.identity.roles.BREAK_GLASS_MAX` and
#: `brain.gate.abstain.DEFAULT_ESCALATION_TTL`, which is deliberate: this repository already
#: uses four hours as "longer than any single piece of work somebody is actually present
#: for", and a reader should not have to work out whether a different number means something.
#:
#: What it costs is nothing a backfill cannot absorb. `brain.connectors.backfill` is a
#: resumable value by design, so a long backfill that reaches this ceiling persists its
#: cursor, stops, and resumes on a fresh lease. See `RESUMING_IS_NOT_RESTARTING`.
LEASE_ABSOLUTE_MAX: Final = timedelta(hours=4)

#: The roles whose vault policy grants `sys/leases/renew`. Read from
#: `ops/openbao/policies/` rather than invented here, and pinned against those files by a
#: test: a set that drifts from the policies produces a renewal this refuses and the vault
#: would have allowed, or the reverse, and the reverse is the one that fails at three in the
#: morning. See `A_BROWSER_RUNNER_LOSES_ITS_CREDENTIAL_RATHER_THAN_EXTENDS_IT`.
ROLES_THAT_MAY_RENEW: Final[frozenset[VaultRole]] = frozenset(
    {VaultRole.APPLICATION, VaultRole.WORKER}
)

#: What a sealed secret renders as, everywhere. A fixed string rather than a truncated
#: prefix: a prefix is a head start for anybody who obtains the log, and it is the shape that
#: gets widened to "just a few more characters, for debugging".
SEALED_RENDERING: Final = "<sealed>"


class LeaseLifecycleError(SecretsUnavailableError):
    """A lease was asked for something a lease cannot do.

    One type for every refusal in this module, including the shape ones, and a subclass of
    `SecretsUnavailableError` rather than a sibling. Callers already wrap a borrow in
    `except SecretsUnavailableError` because that is what `brain.ops.secrets` raises; a
    separate hierarchy here would mean a badly shaped lease escapes that handler and crashes
    a request that the existing one would have turned into an honest failure.
    """


# ------------------------------------------------------------------------ the sealed value
class SealedSecret:
    """A credential that has no rendering.

    Not a dataclass, deliberately: a dataclass generates a `__repr__` from its fields, and
    the whole point here is that there is no generated anything. `__slots__` keeps the value
    off an instance dictionary, so it does not appear in `vars()` either.

    Equality is identity, also deliberately. Two secrets are not compared anywhere in this
    system, and a value-based `__eq__` on a credential is a timing oracle unless somebody
    remembers to make it constant time, which is a thing to remember rather than a property.
    Anything that genuinely needs the characters calls `RunLease.reveal`, which checks the
    clock first and is impossible to write by accident.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            msg = (
                "an empty credential is not a credential; a vault that answered with nothing "
                "has failed, and sealing the emptiness would send it to the source instead"
            )
            raise LeaseLifecycleError(msg)
        self._value = value

    def __repr__(self) -> str:
        return SEALED_RENDERING

    def __str__(self) -> str:
        return SEALED_RENDERING

    def __format__(self, spec: str) -> str:
        """Every format spec renders the same.

        Overridden rather than left to `object.__format__`, which delegates an empty spec to
        `__str__` and raises on anything else. Raising would be safe and is worse: an f-string
        with a width on it would crash the request that logged it, and the fix somebody
        reaches for under time pressure is to interpolate the underlying string instead.
        """
        return SEALED_RENDERING

    def reveal(self) -> str:
        """The characters. The only way to them, and it is spelled out at every call site."""
        return self._value


# -------------------------------------------------------------------------- what persists
@dataclass(frozen=True)
class LeaseRecord:
    """What may be written down about a lease: enough to revoke it, and nothing to leak.

    **There is no secret field and there must never be one**, which is the rule
    `brain.channels.api_keys.ApiKeyRecord` states about its own digest and the reason it has
    no plaintext beside it: a field that exists gets populated by whoever is debugging on the
    day it would be convenient.

    The path and the role are flattened out of `SecretRef` rather than held as one, so that
    the absence is visible by reading the field list. A nested value is a place a reviewer
    has to go and look.
    """

    run_id: str
    lease_id: str
    path: str
    role: VaultRole
    borrowed_at: datetime
    expires_at: datetime
    not_after: datetime
    renewals: int = 0


# -------------------------------------------------------------------------- the lease
@dataclass(frozen=True)
class RunLease:
    """One credential, borrowed by one run, with an end it cannot be renewed past.

    Frozen, like every declaration this system persists a shape of. A lease whose expiry
    could be assigned after construction is a lease whose expiry is whatever the last writer
    thought, and the last writer is usually the retry path.

    `borrowed_at` is when this run *first* borrowed, not when the current term was minted.
    That distinction is the whole of the renewal bound: `not_after` is derived from it, and
    `renew` carries it forward unchanged. The current term's start is not stored because
    nothing needs it and storing it would invite somebody to compute the ceiling from it.
    """

    run_id: str
    lease_id: str
    ref: SecretRef
    secret: SealedSecret
    borrowed_at: datetime
    expires_at: datetime
    renewals: int = 0

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            msg = (
                "a lease that names no run cannot be attributed or cleaned up; the run id is "
                "what says whose credential this is when one is found still live"
            )
            raise LeaseLifecycleError(msg)
        if not self.lease_id.strip():
            msg = (
                "a lease with no id cannot be revoked, which is what brain.ops.openbao "
                "refuses a static engine for: there would be nothing to hand back"
            )
            raise LeaseLifecycleError(msg)
        for label, moment in (("borrowed_at", self.borrowed_at), ("expires_at", self.expires_at)):
            if moment.tzinfo is None:
                msg = (
                    f"{label} has no time zone, and this machine is eight hours from UTC; a "
                    "naive expiry is read here as eight hours out, in whichever direction is "
                    "worse for the run that reads it"
                )
                raise LeaseLifecycleError(msg)
        if self.renewals < 0:
            msg = "renewals is a count of extensions granted and cannot be negative"
            raise LeaseLifecycleError(msg)
        if self.expires_at <= self.borrowed_at:
            msg = "a lease that expires before it was borrowed is not a lease"
            raise LeaseLifecycleError(msg)
        if self.expires_at > self.not_after:
            msg = (
                f"this lease would run to {self.expires_at.isoformat()}, past the "
                f"{LEASE_ABSOLUTE_MAX} a borrowed credential may live from the first borrow "
                f"at {self.borrowed_at.isoformat()}. {RENEWAL_IS_BOUNDED_BY_TIME_NOT_BY_COUNT}"
            )
            raise LeaseLifecycleError(msg)

    @property
    def not_after(self) -> datetime:
        """The end no renewal reaches past. Derived, never stored: see
        `THE_CEILING_IS_DERIVED_SO_A_RENEWAL_CANNOT_MOVE_IT`."""
        return self.borrowed_at + LEASE_ABSOLUTE_MAX

    @property
    def may_renew(self) -> bool:
        """Whether this role's vault policy grants `sys/leases/renew` at all."""
        return self.ref.role in ROLES_THAT_MAY_RENEW

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at

    def remaining(self, now: datetime) -> timedelta:
        """Time left on the current term, floored at zero so a caller cannot get a negative
        sleep out of an expired lease and wait backwards."""
        return max(self.expires_at - now, timedelta())

    def headroom(self, now: datetime) -> timedelta:
        """Time left before the ceiling, whatever the current term says. What a renewal has
        to fit inside, and floored at zero for the same reason as `remaining`."""
        return max(self.not_after - now, timedelta())

    def reveal(self, now: datetime) -> str:
        """The credential, and the clock is checked first.

        The same rule and the same reason as `brain.ops.secrets.Lease.reveal`: the source
        might accept an expired credential if its clock differs or the revocation has not
        propagated, and a credential working after we believed it withdrawn is the failure
        the whole leasing design exists to prevent. Taking `now` rather than reading a clock
        is what makes the boundary testable.
        """
        if not self.is_live(now):
            msg = (
                f"lease {self.lease_id} expired at {self.expires_at.isoformat()}; borrow "
                "another rather than using this one, which the source may still accept"
            )
            raise LeaseLifecycleError(msg)
        return self.secret.reveal()

    def record(self) -> LeaseRecord:
        """What may be stored and logged about this lease. No value, by construction."""
        return LeaseRecord(
            run_id=self.run_id,
            lease_id=self.lease_id,
            path=self.ref.path,
            role=self.ref.role,
            borrowed_at=self.borrowed_at,
            expires_at=self.expires_at,
            not_after=self.not_after,
            renewals=self.renewals,
        )


# ------------------------------------------------------------------------ issue and renew
def for_run(lease: Lease, *, run_id: str) -> RunLease:
    """Take what the vault minted and bind it to one run.

    The vault's own `expires_at` is kept rather than recomputed, which is
    `brain.ops.openbao`'s two-clocks rule: the server's duration wins, because believing our
    own number means holding a credential the vault has already withdrawn. The ceiling is
    ours and is applied on top, and at first issue it cannot bind, since `MAX_LEASE` is an
    hour and `LEASE_ABSOLUTE_MAX` is four. It is written as a `min` anyway rather than
    asserted, because the day somebody raises `MAX_LEASE` this is the line that has to hold.
    """
    return RunLease(
        run_id=run_id,
        lease_id=lease.lease_id,
        ref=lease.ref,
        secret=SealedSecret(lease.secret),
        borrowed_at=lease.issued_at,
        expires_at=min(lease.expires_at, lease.issued_at + LEASE_ABSOLUTE_MAX),
    )


def renewable_for(
    lease: RunLease, *, now: datetime, wanted: timedelta = DEFAULT_LEASE
) -> timedelta:
    """The longest term it is worth asking the vault for, or a refusal saying why not.

    Three bounds and the smallest wins: what the caller asked for, `MAX_LEASE`, and the
    headroom left under the ceiling. Returning the bounded number rather than letting the
    caller ask for more and be trimmed is what keeps the vault's answer and ours agreeing:
    a request for two hours against forty minutes of headroom would come back as a lease
    this module then has to shorten, and a shortened lease is one the vault believes is
    longer than we do.
    """
    _assert_renewable(lease, now=now)
    if wanted <= timedelta():
        msg = "a renewal of zero is not a renewal; give the lease back instead"
        raise LeaseLifecycleError(msg)
    return min(wanted, MAX_LEASE, lease.headroom(now))


def renew(lease: RunLease, *, now: datetime, granted: timedelta) -> RunLease:
    """Extend a live lease by what the vault actually granted.

    `granted` is the server's duration and not the one we asked for, matching
    `OpenBaoVault.issue`. It is still clamped to `not_after`, because a vault mount whose
    maximum is longer than ours would otherwise hand back a term that outruns the ceiling,
    and the ceiling is this system's rule rather than the vault's.

    `borrowed_at` is carried forward untouched. That single line is what makes the bound a
    bound: move it to `now` here and every renewal resets the ceiling, each step reads as
    correct, and the lease lives for ever.
    """
    _assert_renewable(lease, now=now)
    if granted <= timedelta():
        msg = (
            f"the vault granted {granted} on a renewal, which is not an extension; treat it "
            "as a refusal and borrow again rather than holding a lease that did not move"
        )
        raise LeaseLifecycleError(msg)
    return replace(
        lease,
        expires_at=min(now + granted, lease.not_after),
        renewals=lease.renewals + 1,
    )


def _assert_renewable(lease: RunLease, *, now: datetime) -> None:
    """Every reason a lease may not be extended, in the order they are worth being told.

    The role first, because that refusal is permanent and no amount of waiting changes it.
    Then liveness: an expired lease is re-borrowed and never renewed, since renewing one is
    how a run keeps using a credential it has already lost the right to. Then the ceiling.

    **The ceiling is tested against the current term, not against `now`, and that is what
    makes it a rule rather than dead code.** A live lease always has time left before the
    ceiling, because `expires_at` can never be past it, so a check on remaining headroom
    could not fire on any lease that got past the liveness check above. What can happen, and
    what a long run does hit, is a term that has already been clamped to the ceiling: a
    renewal then cannot move the expiry by a single second. Refusing it while the lease is
    still live is the useful moment to say so, because the holder is told to borrow again
    before it loses the credential in the middle of an operation rather than after.
    """
    if not lease.may_renew:
        msg = (
            f"{lease.ref.role} may not renew a lease. "
            f"{A_BROWSER_RUNNER_LOSES_ITS_CREDENTIAL_RATHER_THAN_EXTENDS_IT}"
        )
        raise LeaseLifecycleError(msg)
    if not lease.is_live(now):
        msg = (
            f"lease {lease.lease_id} expired at {lease.expires_at.isoformat()} and cannot be "
            "renewed; borrow another. Renewing an expired lease is how a run carries on with "
            "a credential it has already lost"
        )
        raise LeaseLifecycleError(msg)
    if lease.expires_at >= lease.not_after:
        msg = (
            f"lease {lease.lease_id} already runs to the {LEASE_ABSOLUTE_MAX} a borrowed "
            f"credential may live from the first borrow at {lease.borrowed_at.isoformat()}, "
            f"after {lease.renewals} renewal(s), so a renewal cannot move it. Borrow another. "
            f"{RENEWAL_IS_BOUNDED_BY_TIME_NOT_BY_COUNT}"
        )
        raise LeaseLifecycleError(msg)


# -------------------------------------------------------------------- what a run is holding
@dataclass(frozen=True)
class RunHoldings:
    """Every lease one run currently holds. A value, so releasing is constructing.

    Frozen and rebuilt on every change for the reason `brain.connectors.backfill.
    BackfillCursor` gives about its own position: a holder that mutates has a state a crash
    can lose, and what a crash loses here is the list of credentials somebody has to revoke.
    """

    run_id: str
    leases: tuple[RunLease, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            msg = "holdings that name no run cannot be cleaned up by anybody"
            raise LeaseLifecycleError(msg)
        foreign = sorted({lease.run_id for lease in self.leases if lease.run_id != self.run_id})
        if foreign:
            msg = (
                f"these holdings are {self.run_id!r} and carry leases borrowed by {foreign}; "
                "one run releasing another's credentials is a run cleaning up work that is "
                "still going on"
            )
            raise LeaseLifecycleError(msg)
        seen = [lease.lease_id for lease in self.leases]
        doubled = sorted({lease_id for lease_id in seen if seen.count(lease_id) > 1})
        if doubled:
            msg = (
                f"{doubled} appears twice in one run's holdings; two entries for one lease "
                "means releasing it once leaves the other, and the other is the one nobody "
                "looks for"
            )
            raise LeaseLifecycleError(msg)

    @property
    def lease_ids(self) -> tuple[str, ...]:
        """The ids, which are safe to log.

        The shape `provider_keys.load_into_environment` returns for the same reason: a
        function that handed back credentials would be a function whose result must not be
        printed, and every caller would have to know that. Returning ids is what makes the
        line "run r_18 holds 3 leases: ..." possible at all.
        """
        return tuple(lease.lease_id for lease in self.leases)

    def holds(self, lease_id: str) -> bool:
        return lease_id in self.lease_ids

    def ledger(self) -> tuple[LeaseRecord, ...]:
        """What may be persisted about everything this run holds. Values, never secrets."""
        return tuple(lease.record() for lease in self.leases)

    def add(self, lease: RunLease) -> RunHoldings:
        """Take one more lease. Refuses a second entry for the same id via `__post_init__`."""
        return replace(self, leases=(*self.leases, lease))

    def release(self, lease_id: str) -> RunHoldings:
        """Give one lease back: the holdings without it. See
        `GIVING_A_LEASE_BACK_IS_A_DELETION`.

        Silent about an id that is not held, deliberately. Release is the cleanup path and is
        run twice routinely, once by the code that finished and once by whatever sweeps up
        after a crash; raising on the second one turns an idempotent operation into an error
        during shutdown, which is where `revoke_all` already declines to raise.
        """
        return replace(self, leases=tuple(x for x in self.leases if x.lease_id != lease_id))

    def expired(self, now: datetime) -> tuple[str, ...]:
        """Ids of leases whose term has run out while the run still lists them.

        Ids rather than leases, so that the natural thing to do with the result is log it or
        revoke by id, and neither can reach a secret.
        """
        return tuple(lease.lease_id for lease in self.leases if not lease.is_live(now))


# ------------------------------------------------------------------------- the lifecycle
@contextmanager
def hold(
    vault: Vault,
    ref: SecretRef,
    *,
    run_id: str,
    now: datetime,
    ttl: timedelta = DEFAULT_LEASE,
) -> Iterator[RunLease]:
    """Borrow for the length of a block, sealed and bound to a run.

    A thin wrapper over `brain.ops.secrets.borrow` rather than a second implementation of
    issue-and-revoke. The `finally` that gives the lease back, the rule that a revocation
    failure never replaces the run's real exception, and the mapping of a backend's own
    exception onto one typed failure are all arguments already made and tested there, and a
    copy of them here would be a second place for them to be subtly different.

    What this adds is the sealing and the run binding, which is why it yields a `RunLease`
    and not a `Lease`: a caller handed the raw `Lease` has a plain string on `.secret`, and
    the seal is only a guarantee if there is no path that hands one out.
    """
    with borrow(vault, ref, now=now, ttl=ttl) as lease:
        yield for_run(lease, run_id=run_id)
