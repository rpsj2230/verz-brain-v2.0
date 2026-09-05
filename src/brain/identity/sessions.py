"""How long a sign-in lasts, how it ends everywhere at once, and the caller that is not a person.

A session is the only thing that turns a principal into `Assurance.AUTHENTICATED`, and
`brain.gate.admission` narrows every request by that value. So the questions this module
answers are not administrative: they decide what somebody may do right now.

Three things break without it.

**Logout becomes a screen that clears a cookie.** A bearer token is valid because of what is
inside it, not because of anything we hold, so deleting a row on our side changes nothing
about a token already in somebody's hand. If that is where logout stops, then "sign out" on
a shared machine, or after a laptop is stolen, or when somebody leaves on a Friday, means
nothing until the token expires on its own. Logout here raises a **not-before floor** on the
principal, and every token issued at or before it is refused from that moment, whether or not
this process ever saw the session it belonged to. A session that cannot be revoked is a
standing grant with a friendlier name.

**A session refreshes itself forever.** Sliding an idle window on each refresh is right and
is not enough: refreshed often enough, the window never closes, and a sign-in from March is
still authenticating requests in September. Every session carries an absolute expiry it
cannot be refreshed past, set when it opens and never moved.

**A service account acquires authority of its own.** The architecture is explicit that a
service account is "a standing delegation resolving the owner's live entitlements", because
"a service principal with its own grants is union authority, which is the classic
escalation": grant the robot one capability its owner lacks and the pair of them can do more
than either. `reach_for` therefore only ever intersects. There is no argument to it that adds
a grant, and its declared `ceiling` can narrow the owner's reach and never widen it.

No network call is made here. No SQLAlchemy model and no migration is written here; where a
leaf implies a table (`session`, `service_account`), this is the type and the rules only.

Task ids: M1.1.6, M1.1.7
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Principal
from brain.core.scope import Scope
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.identity.oidc import TokenRefusal, TokenRefusedError, VerifiedClaims
from brain.identity.roles import SESSION_ID_PATTERN, IdentityError, NoStandingEntitlement

#: How long a session survives with nothing happening on it. Thirty minutes is the console's
#: window: long enough to read a long answer and come back to it, short enough that a machine
#: left unlocked over lunch is not still signed in.
SESSION_IDLE: Final = timedelta(minutes=30)

#: The longest a session may live however often it is refreshed. Ten hours is one working
#: day with an early start and a late finish; a session that survives the night is one nobody
#: was present for. This is a ceiling and not a policy dial, for the reason `BREAK_GLASS_MAX`
#: is not one: a maximum an operator can raise gets raised on the day it is inconvenient.
SESSION_ABSOLUTE_MAX: Final = timedelta(hours=10)

#: How long a logout floor is kept. It has to outlive the longest-lived token that could have
#: been issued before it, or a token minted just before logout outlives the record saying to
#: refuse it. Anything older than this is provably harmless, because every token issued
#: before it has expired on its own.
FLOOR_RETENTION: Final = SESSION_ABSOLUTE_MAX


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        msg = f"{field} must be timezone-aware; a naive timestamp is a silent bug"
        raise ValueError(msg)
    return value


class Session(BaseModel):
    """One live sign-in.

    `absolute_expiry` is a separate field from `expires_at` rather than something computed at
    refresh time, because the two answer different questions: `expires_at` is "when does this
    go idle", `absolute_expiry` is "when does this end regardless". Computing the second from
    the first at each refresh is how it quietly becomes the first.

    `second_factor` is a fact about *this session*, recorded when it opened. It is not a
    property of the account: somebody with an authenticator app configured who signed in with
    a password alone has not presented a second factor, and treating them as though they had
    is how `Assurance.STRONG` stops meaning anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Keycloak's `sid`. Constrained to the audit ledger's identifier grammar so an
    #: unloggable session cannot be opened, rather than failing later at the audit write.
    session_id: str = Field(min_length=1, max_length=120, pattern=SESSION_ID_PATTERN)
    principal_id: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=500)
    #: The IdP subject this session belongs to. Kept so a back-channel logout naming a
    #: subject rather than a session id can still find it.
    subject: str = Field(min_length=1, max_length=200)
    opened_at: datetime
    expires_at: datetime
    absolute_expiry: datetime
    second_factor: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.opened_at, "opened_at")
        _require_aware(self.expires_at, "expires_at")
        _require_aware(self.absolute_expiry, "absolute_expiry")
        if self.expires_at <= self.opened_at:
            msg = "a session must expire after it opens"
            raise ValueError(msg)
        if self.absolute_expiry < self.expires_at:
            msg = (
                "the absolute expiry is before the idle expiry, so the idle window would "
                "outlive the bound that exists to cap it"
            )
            raise ValueError(msg)
        if self.absolute_expiry - self.opened_at > SESSION_ABSOLUTE_MAX:
            msg = (
                f"a session may run at most {SESSION_ABSOLUTE_MAX}; this one is bounded at "
                f"{self.absolute_expiry - self.opened_at}"
            )
            raise ValueError(msg)
        return self

    def is_live(self, now: datetime) -> bool:
        """True when this session is still both within its idle window and its hard bound."""
        return self.opened_at <= now < min(self.expires_at, self.absolute_expiry)

    def assurance(self, now: datetime) -> Assurance:
        """What this session is worth as evidence about a request arriving now (M1.1.6).

        A dead session returns UNVERIFIED and deliberately not BOUND. BOUND is what a channel
        binding is worth, and a binding is a separate artefact that was proven separately;
        a session that has expired is not weaker evidence about this request, it is no
        evidence at all. Returning BOUND here would hand a read to somebody whose session
        ended, on the strength of the fact that it once existed.
        """
        if not self.is_live(now):
            return Assurance.UNVERIFIED
        return Assurance.STRONG if self.second_factor else Assurance.AUTHENTICATED


def open_session(
    *,
    claims: VerifiedClaims,
    principal: Principal,
    now: datetime,
    second_factor: bool = False,
    idle: timedelta = SESSION_IDLE,
    absolute: timedelta = SESSION_ABSOLUTE_MAX,
) -> Session:
    """Open a session for a verified token (M1.1.6).

    Takes `VerifiedClaims` rather than a token or a subject string, so a session cannot be
    opened from something unchecked. The `sid` is required: a token with no session id is a
    service-account token, and opening an interactive session for one would give a robot an
    `Assurance.AUTHENTICATED` ceiling that the API channel is not supposed to reach.
    """
    if claims.session_id is None:
        raise TokenRefusedError(
            TokenRefusal.SESSION_UNKNOWN,
            "token carries no sid, so it belongs to no interactive session",
        )
    if not principal.is_active(now):
        raise TokenRefusedError(TokenRefusal.PRINCIPAL_INACTIVE, principal.id)
    if absolute > SESSION_ABSOLUTE_MAX:
        msg = f"a session may run at most {SESSION_ABSOLUTE_MAX}, not {absolute}"
        raise IdentityError(msg)

    hard_stop = now + absolute
    # A contractor's own expiry ends their session too. Without this the session outlives the
    # principal, and `EntitlementSet` refusing everything afterwards would show up as an
    # unexplained empty answer rather than as "your access ended".
    if principal.not_after is not None:
        hard_stop = min(hard_stop, principal.not_after)
    if hard_stop <= now:
        raise TokenRefusedError(TokenRefusal.PRINCIPAL_INACTIVE, principal.id)

    return Session(
        session_id=claims.session_id,
        principal_id=principal.id,
        issuer=claims.issuer,
        subject=claims.subject,
        opened_at=now,
        expires_at=min(now + idle, hard_stop),
        absolute_expiry=hard_stop,
        second_factor=second_factor,
    )


class SessionRegistry:
    """Live sessions, and the not-before floor that makes logout mean something (M1.1.6).

    Two structures, and the second is the load-bearing one.

    `_live` is the sessions we know about. Deleting from it is how a session ends, and it is
    deletion rather than a flag for the reason `brain.identity.packs.revoke` deletes: a
    tombstone is a negative row, and a model with negative rows has an evaluation order.

    `_floors` is one timestamp per principal, and it is what survives everything the first
    structure does not. A bearer token is valid because of what is inside it, so a process
    restart, a second replica, or a back-channel logout that arrives before this process ever
    saw the session all leave `_live` unable to refuse anything. The floor refuses on the
    token's own `iat`, which every token carries, so it works in all three cases.

    Rejected: keeping a list of ended session ids. It only refuses tokens that carry a `sid`,
    it grows without bound, and it says nothing about the token minted one second before
    logout that names a session nobody recorded.
    """

    def __init__(self) -> None:
        self._live: dict[str, Session] = {}
        self._floors: dict[str, datetime] = {}

    # -- opening and reading ------------------------------------------------------------
    def register(self, session: Session) -> None:
        """Record a session as live."""
        self._live[session.session_id] = session

    def get(self, session_id: str) -> Session | None:
        return self._live.get(session_id)

    def live_for(self, principal_id: str, now: datetime) -> tuple[Session, ...]:
        return tuple(
            s for s in self._live.values() if s.principal_id == principal_id and s.is_live(now)
        )

    def not_before_for(self, principal_id: str) -> datetime | None:
        """The logout floor for this principal, if there is one."""
        return self._floors.get(principal_id)

    # -- the check ----------------------------------------------------------------------
    def admit(self, claims: VerifiedClaims, principal: Principal, now: datetime) -> Session:
        """The session this token belongs to, or a refusal (M1.1.6).

        The floor is checked first, before the session is even looked up, because it is the
        check that still works when the session is not there. A token that predates a logout
        must be refused identically whether we still hold its session, never held it, or
        restarted since.

        Deliberately does not slide the idle window. A read is a read; the window moves on
        `refresh`, which is the point at which the identity provider has also been asked
        whether this person still exists. Sliding on every request would mean an automated
        poller keeps a session alive with nobody at the keyboard, which is the failure the
        idle window is for.
        """
        floor = self._floors.get(principal.id)
        if floor is not None and claims.issued_at <= floor:
            # `<=` and not `<`. Issue times are whole seconds, so a token minted in the same
            # second as the logout is indistinguishable from one minted just before it, and
            # the safe reading of an ambiguous case is to refuse. The cost is signing in
            # again a second later; the cost of the other choice is a token that survives
            # its own logout.
            raise TokenRefusedError(
                TokenRefusal.LOGGED_OUT,
                f"issued {claims.issued_at.isoformat()}, floor {floor.isoformat()}",
            )
        if not principal.is_active(now):
            raise TokenRefusedError(TokenRefusal.PRINCIPAL_INACTIVE, principal.id)
        if claims.session_id is None:
            raise TokenRefusedError(TokenRefusal.SESSION_UNKNOWN, "token carries no sid")

        session = self._live.get(claims.session_id)
        if session is None:
            raise TokenRefusedError(TokenRefusal.SESSION_UNKNOWN, claims.session_id)
        if session.principal_id != principal.id:
            # A token naming somebody else's session. One comparison to rule out the
            # catastrophic case, the same reasoning as the principal check in `gate.resolve`.
            raise TokenRefusedError(TokenRefusal.SESSION_MISMATCH, claims.session_id)
        if not session.is_live(now):
            del self._live[session.session_id]
            raise TokenRefusedError(TokenRefusal.SESSION_EXPIRED, session.session_id)
        return session

    # -- refresh ------------------------------------------------------------------------
    def refresh(
        self,
        session_id: str,
        principal: Principal,
        now: datetime,
        *,
        idle: timedelta = SESSION_IDLE,
    ) -> Session:
        """Slide the idle window, never past the absolute expiry (M1.1.6).

        The principal is required rather than optional. Refresh is the moment a sign-in gets
        extended, and extending one for somebody who has left is how a leaver keeps working
        for another ten hours; an optional argument here is an argument somebody omits.
        """
        session = self._live.get(session_id)
        if session is None:
            raise TokenRefusedError(TokenRefusal.SESSION_UNKNOWN, session_id)
        if session.principal_id != principal.id:
            raise TokenRefusedError(TokenRefusal.SESSION_MISMATCH, session_id)
        floor = self._floors.get(principal.id)
        if floor is not None and session.opened_at <= floor:
            raise TokenRefusedError(TokenRefusal.LOGGED_OUT, session_id)
        if not principal.is_active(now):
            raise TokenRefusedError(TokenRefusal.PRINCIPAL_INACTIVE, principal.id)
        if not session.is_live(now):
            del self._live[session_id]
            raise TokenRefusedError(TokenRefusal.SESSION_EXPIRED, session_id)

        extended = session.model_copy(
            update={"expires_at": min(now + idle, session.absolute_expiry)}
        )
        self._live[session_id] = extended
        return extended

    # -- ending -------------------------------------------------------------------------
    def end_session(self, session_id: str, now: datetime) -> Session | None:
        """End one session and raise its principal's floor (M1.1.6).

        Returns the session that ended, or None when there was none to end. The floor is
        raised only when we knew the session, because otherwise there is no principal to
        raise it for; `end_all_for` is the call that works without one, and it is what a
        back-channel logout carrying a subject should use.
        """
        session = self._live.pop(session_id, None)
        if session is None:
            return None
        self._raise_floor(session.principal_id, now)
        return session

    def end_all_for(self, principal_id: str, now: datetime) -> tuple[Session, ...]:
        """End every session this principal has, and raise the floor (M1.1.6, M1.2.3).

        **This raises the floor even when there is nothing to end**, and that is the case it
        exists for. A back-channel logout, a disabled account or a stolen laptop reaches a
        replica that never held the session, and the only thing that refuses the token
        already in the attacker's hand is the floor. An implementation that returned early
        on an empty list would work in every test and fail in production behind a load
        balancer.
        """
        ended = tuple(s for s in self._live.values() if s.principal_id == principal_id)
        for session in ended:
            del self._live[session.session_id]
        self._raise_floor(principal_id, now)
        return ended

    def _raise_floor(self, principal_id: str, now: datetime) -> None:
        current = self._floors.get(principal_id)
        # Monotonic. A floor that could be lowered is one a replayed or out-of-order logout
        # event can undo, which would re-admit tokens that were already being refused.
        self._floors[principal_id] = max(current, now) if current is not None else now

    def prune_floors(self, now: datetime, retention: timedelta = FLOOR_RETENTION) -> int:
        """Drop floors old enough that every token they refuse has expired anyway.

        Without this the map grows once per logout forever. The bound is the longest possible
        session, so dropping a floor cannot re-admit anything: a token issued before it
        expired on its own before this ran.
        """
        cutoff = now - retention
        stale = [pid for pid, floor in self._floors.items() if floor < cutoff]
        for pid in stale:
            del self._floors[pid]
        return len(stale)


# ------------------------------------------------------------- service accounts
class ServiceAccount(BaseModel):
    """An API caller that is not a person (M1.1.7).

    A standing delegation of its owner's entitlements, narrowed by a declared ceiling. It has
    no grants of its own and there is no field here that could hold one: the architecture
    calls a service principal with its own grants "union authority, which is the classic
    escalation", and the escalation is not hypothetical. Grant the integration
    `read:client.margin` because it needs it for one report, and the owner who could not see
    margins can now read them through the integration they administer.

    `not_after` is required, unlike on `Principal` where only contractors and partners carry
    one. A service account is the credential most likely to be created for one integration in
    2026 and still working in 2031 with nobody able to say what uses it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(min_length=1, max_length=128)
    #: The IdP subject of the service-account user Keycloak creates for the client.
    subject: str = Field(min_length=1, max_length=200)
    #: Whose reach this borrows. A person, always: a service account owned by nobody is one
    #: nobody reviews, and the review is the only thing that ever removes it.
    owner_principal_id: str = Field(min_length=1, max_length=128)
    #: The capabilities this account may exercise *if its owner holds them*. Enumerated
    #: rather than wildcard, so what it could reach is a list an auditor reads rather than a
    #: claim about it.
    ceiling: tuple[Capability, ...]
    not_after: datetime
    #: Which channel ceiling applies. API, so `brain.gate.admission` refuses `approve` and
    #: `admin` from it whatever the owner holds.
    channel: Channel = Channel.API

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.not_after, "not_after")
        if not self.ceiling:
            msg = (
                f"service account {self.client_id!r} declares no ceiling; an empty one would "
                "either confer nothing or, if somebody later reads it as 'unset', everything"
            )
            raise ValueError(msg)
        if self.owner_principal_id == self.client_id:
            msg = "a service account cannot own itself; the owner is the person who reviews it"
            raise ValueError(msg)
        return self

    def is_active(self, now: datetime) -> bool:
        return now < self.not_after

    def ceiling_set(self) -> EntitlementSet:
        """The ceiling as an entitlement set, for intersection.

        Each capability is carried at unrestricted scope on purpose. The ceiling answers
        *which capabilities*, and the scope stays whatever the owner's own grant says; a
        scope invented here would either widen the owner's (impossible, since `intersect`
        conjoins) or silently narrow it in a place nobody would think to look.
        """
        return EntitlementSet(
            principal_id=self.client_id,
            grants=tuple(
                Grant(capability=capability, scope=Scope.unrestricted())
                for capability in self.ceiling
            ),
            not_after=self.not_after,
        )


def authenticate_service_account(
    claims: VerifiedClaims,
    accounts: Mapping[str, ServiceAccount],
    now: datetime,
) -> ServiceAccount:
    """The service account behind a verified token, or a refusal (M1.1.7).

    `accounts` is keyed by IdP subject, and a human's subject is not in it, which is how the
    two authentication paths stay disjoint without a flag in the token deciding which one
    runs. A token carrying a `sid` is refused outright: that is an interactive session, and
    letting a person's browser token act as a service account would hand it the service
    account's ceiling in addition to their own.
    """
    if claims.session_id is not None:
        raise TokenRefusedError(
            TokenRefusal.NOT_A_SERVICE_ACCOUNT,
            "token belongs to an interactive session",
        )
    account = accounts.get(claims.subject)
    if account is None:
        raise TokenRefusedError(TokenRefusal.UNKNOWN_SERVICE_ACCOUNT, claims.subject)
    azp = claims.claim("azp")
    if isinstance(azp, str) and azp != account.client_id:
        # The authorised party is the client that asked for the token. If it disagrees with
        # the client we matched on `sub`, one of the two mappings is wrong and neither is
        # safe to guess at.
        raise TokenRefusedError(
            TokenRefusal.NOT_A_SERVICE_ACCOUNT,
            f"azp {azp!r} is not {account.client_id!r}",
        )
    if not account.is_active(now):
        raise TokenRefusedError(TokenRefusal.SERVICE_ACCOUNT_EXPIRED, account.client_id)
    return account


def reach_for(
    account: ServiceAccount,
    owner: Principal,
    owner_entitlement: EntitlementSet | NoStandingEntitlement,
    now: datetime,
) -> EntitlementSet | NoStandingEntitlement:
    """What a service account may exercise: its owner's live reach, narrowed (M1.1.7).

    Intersection in one direction only. There is no argument here that could add a grant, and
    the ceiling is intersected rather than unioned, so the account holds a capability only
    when the owner holds it *and* the ceiling admits it. An operator who adds a capability to
    the ceiling that the owner does not hold has granted nothing, which is the same safety
    property the channel and assurance ceilings have in `brain.gate.admission`.

    An owner who holds nothing yields `NoStandingEntitlement` rather than an empty set, so a
    service account owned by a partner cannot be intersected, hashed or cached as though it
    had a reach of zero. That is `brain.identity.roles.NoStandingEntitlement`'s whole point,
    and it would be undone here by returning an empty `EntitlementSet` for convenience.
    """
    if account.owner_principal_id != owner.id:
        msg = (
            f"service account {account.client_id!r} is owned by "
            f"{account.owner_principal_id!r}, not {owner.id!r}"
        )
        raise IdentityError(msg)
    if not owner.is_active(now):
        raise TokenRefusedError(TokenRefusal.OWNER_INACTIVE, owner.id)
    if isinstance(owner_entitlement, NoStandingEntitlement):
        return NoStandingEntitlement(principal_id=account.client_id)
    if owner_entitlement.principal_id != owner.id:
        # The store returned somebody else's row. One comparison to rule out the
        # catastrophic failure, exactly as `brain.gate.resolve` does.
        msg = f"entitlement set belongs to {owner_entitlement.principal_id!r}, not {owner.id!r}"
        raise IdentityError(msg)

    narrowed = owner_entitlement.intersect(account.ceiling_set())
    # Rebuilt under the account's own id rather than returned as the owner's. The audit
    # trail and the answer cache both key on `principal_id`, and an action taken by an
    # integration must not be recorded as an action taken by the person who owns it.
    return EntitlementSet(
        principal_id=account.client_id,
        grants=narrowed.grants,
        not_after=narrowed.not_after,
    )


def assurance_for_service_account(account: ServiceAccount, now: datetime) -> Assurance:
    """What a service-account credential is worth as evidence (M1.1.7).

    AUTHENTICATED and never STRONG. A client secret held by a process is one factor, and it
    is the only one a machine can ever present; treating it as strong would let an
    integration reach `approve` and `admin`, which is the pair of verbs
    `brain.gate.admission` reserves for a live session with a second factor in it.
    """
    return Assurance.AUTHENTICATED if account.is_active(now) else Assurance.UNVERIFIED


def utc_now() -> datetime:
    """The clock, in one place, so a test can see every call site that reads it."""
    return datetime.now(UTC)
