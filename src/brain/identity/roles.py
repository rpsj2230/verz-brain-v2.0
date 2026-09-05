"""Platform roles, deputies, break-glass sessions, and the partner who holds nothing.

Roles and entitlements answer two different questions, and this module exists to keep them
apart. A role governs the *platform*: publish an agent, appoint an admin, read audit
metadata. An entitlement governs *data*, as a (capability, scope) pair. The architecture
states the boundary in one line, and the whole module is built to make that line true:
"No role implies a capability, including Super Admin."

Four things break without this module.

**A role table someone edits at 2am.** The six roles are a compiled constant, exported
through a `MappingProxyType` so they cannot even be mutated in process. A role stored as
an editable row is a permission model whose shape changes without a deploy, a review or a
diff, and the change is invisible in the repository afterwards.

**"What can this person see" becomes a graph walk.** If a role could be the subject of a
capability grant, answering that question means resolving a person to their roles, those
roles to their grants, and reconciling the scopes. `SubjectKind` in `brain.identity.teams`
has two members and never a third, and `assert_not_a_role` refuses a role name arriving as
a subject id from a loader. `brain.identity.packs.resolve_entitlement` does not take role
grants as an argument at all, which is the same rule expressed as a signature.

**A thirty-day delegation becomes permanent by chaining.** A deputy is a time-boxed role
grant, and a deputy who could appoint a deputy renews the chain forever without anyone
re-approving anything. `appoint_deputy` refuses when the standing grant is itself a
deputy, and `check_deputy_depth` catches a chain written directly into the table.

**The last Super Admin is revoked and nobody can appoint another.** The floor is two, and
a deputy does not count towards it: a time-boxed holder is cover, not ownership.

Break-glass and the partner principal live here rather than beside `Principal` because
both are about the authority a principal carries rather than the record of who they are.
A partner holds nothing at all, which is not the same as holding an empty set: see
`brain.gate.ingress.Unrecognised` for the shape this borrows.

No SQLAlchemy model and no migration is written here. Where a leaf names a table
(`role_grant`), this is the type and the rules only; the table belongs to whoever owns
`src/brain/tables`.

Task ids: M1.2.4, M1.2.5, M1.3.1, M1.3.2, M1.3.3, M1.3.4, M1.3.5
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain.audit.ledger import AuditAction, redact_details
from brain.core.entitlement import CAPABILITY_RE, Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal
from brain.core.scope import Scope
from brain.core.scope_sql import assert_conjunctive, is_unsatisfiable


class IdentityError(Exception):
    """Raised when a role grant, a deputy appointment or a break-glass session is unsafe.

    An authoring-time failure, like `DepartmentError`. It never reaches a person asking a
    question: it is what stops a row being written. It is deliberately outside the
    `brain.core.errors` taxonomy, because those five outcomes describe an answer and this
    describes a refusal to record something.
    """


class RoleSubjectError(IdentityError):
    """Raised when something tries to make a role the subject of a grant.

    A distinct type so the constraint from M1.3.5 shows up by name in a traceback rather
    than as one message among many. Whoever reads the log should not have to infer which
    rule fired.
    """


# --------------------------------------------------------------- the six roles
class Role(enum.StrEnum):
    """The six platform roles. Compiled in, never rows (M1.3.1).

    A seventh role is a code change, a review and a deploy. That is the point: the set of
    things a person can be is part of the design of the system, not part of its data. An
    editable role table looks like flexibility and is actually an unreviewed permission
    model, because the row that granted someone the platform is indistinguishable from the
    row that granted them a department.

    Rejected: a `role` string column with a check constraint. The constraint lives in the
    database, the enum lives in the code, and the two drift the first time somebody writes
    a migration by hand. One definition, in the language the checks are written in.
    """

    SUPER_ADMIN = "super_admin"
    DEPARTMENT_ADMIN = "department_admin"
    MEMBER = "member"
    AUDITOR = "auditor"
    CONNECTOR_ADMIN = "connector_admin"
    APPROVER = "approver"


@dataclass(frozen=True)
class RoleSpec:
    """What a role is for, written down where the code can read it.

    `scope_required` is the only field any logic consults. The prose fields are here so the
    console can render the table from the same constant the validator uses, rather than
    from a second copy in a template that nobody updates.
    """

    role: Role
    scope_required: bool
    exists_to: str
    typical_count: str


#: How many roles there are. Pinned as a number so that adding a member to `Role` fails a
#: test rather than quietly widening the model.
ROLE_COUNT: Final = 6

#: The roles whose grant is meaningless without a scope. A Department Admin with no scope
#: is a Super Admin nobody appointed, which is the same failure `DepartmentAdmin` refuses;
#: an Approver with no scope approves anything anyone asks them to.
SCOPE_REQUIRED: Final[frozenset[Role]] = frozenset({Role.DEPARTMENT_ADMIN, Role.APPROVER})

_SPECS: Final[tuple[RoleSpec, ...]] = (
    RoleSpec(
        role=Role.SUPER_ADMIN,
        scope_required=False,
        exists_to=(
            "Own the platform: publish global agents, change the catalogue, "
            "confirm nominations, disable principals"
        ),
        typical_count="2 to 4",
    ),
    RoleSpec(
        role=Role.DEPARTMENT_ADMIN,
        scope_required=True,
        exists_to=(
            "Run one department: approve its publications, grant within its scope, "
            "adopt orphaned agents, lower leashes"
        ),
        typical_count="1 per department",
    ),
    RoleSpec(
        role=Role.MEMBER,
        scope_required=False,
        exists_to="Ask questions, build personal agents, own their delegations",
        typical_count="everyone",
    ),
    RoleSpec(
        role=Role.AUDITOR,
        scope_required=False,
        exists_to=(
            "Read the metadata plane end to end, including Super Admin activity. Never the content."
        ),
        typical_count="1 to 2",
    ),
    RoleSpec(
        role=Role.CONNECTOR_ADMIN,
        scope_required=False,
        exists_to="Install connectors, bind and rotate credential references",
        typical_count="1 to 2, deliberately not the Super Admins",
    ),
    RoleSpec(
        role=Role.APPROVER,
        scope_required=True,
        exists_to="Approve Assisted-rung actions, within their own entitlement",
        typical_count="per department",
    ),
)

#: The compiled constant. A read-only proxy rather than a dict, because "not editable
#: rows" has to mean "not editable at runtime" as well; a plain dict at module level is a
#: role table with worse durability, and one import away from being written to.
ROLE_SPECS: Final[Mapping[Role, RoleSpec]] = MappingProxyType({s.role: s for s in _SPECS})

#: Every role name as a bare string. Used to refuse a role arriving where a subject id
#: belongs, which is how M1.3.5 is broken by a loader rather than by a type.
ROLE_NAMES: Final[frozenset[str]] = frozenset(r.value for r in Role)

#: There is deliberately no mapping from a role to a capability anywhere in this package.
#: Not an empty one either: an empty mapping is a place to put an entry. The absence is
#: asserted by `role_capability_leaks`, which is run over every module in the package by
#: the invariant suite.
NO_ROLE_IMPLIES_A_CAPABILITY: Final = (
    "Roles govern the platform; entitlements govern data. A Super Admin sees no document "
    "body without a grant of their own."
)


def spec_for(role: Role) -> RoleSpec:
    """The spec for a role. Total over the enum by construction, so it cannot return None."""
    return ROLE_SPECS[role]


def assert_not_a_role(identifier: str) -> None:
    """Refuse an identifier that names a role (M1.3.5).

    The type system already makes a role unusable as a grant subject, because
    `SubjectKind` has no role member. This exists for the other direction: an id arriving
    as a string from a seed file, a migration or a console form, where `principal_id =
    "super_admin"` would create a grant that reads like a role grant to every human who
    later looks at the table, and resolves for whoever happens to own that id.
    """
    if identifier in ROLE_NAMES:
        msg = (
            f"{identifier!r} is a platform role, and a role may never be the subject of a "
            "capability grant; capabilities attach to principals and teams"
        )
        raise RoleSubjectError(msg)


def _looks_like_a_capability(value: object) -> bool:
    """True for anything that would carry a capability if a role were mapped to it."""
    if isinstance(value, Capability | Grant | EntitlementSet):
        return True
    if isinstance(value, str):
        return bool(CAPABILITY_RE.match(value))
    if isinstance(value, Mapping):
        return any(_looks_like_a_capability(v) for v in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_looks_like_a_capability(v) for v in value)
    return False


def role_capability_leaks(namespace: Mapping[str, Any]) -> list[str]:
    """Names in a module namespace that map a role to something capability-shaped.

    Run over the whole package by the invariant suite. The check is on the *shape* of the
    data rather than on a name, because the way this rule actually gets broken is not
    somebody writing `ROLE_CAPABILITIES`; it is somebody adding a convenience mapping in a
    hurry and calling it `DEFAULTS`.

    Rejected: grepping the source for the word "capability" near the word "role". Both
    words appear in every explanatory comment in this file, so the check would either be
    permanently red or would need an exclusion list, and an exclusion list is where the
    real violation eventually hides.
    """
    findings: list[str] = []
    for name, value in namespace.items():
        if name.startswith("_"):
            continue
        keyed_by_role = isinstance(value, Mapping) and any(
            isinstance(k, Role) or (isinstance(k, str) and k in ROLE_NAMES) for k in value
        )
        if keyed_by_role and _looks_like_a_capability(value):
            findings.append(f"{name} maps a role to something capability-shaped")
        role_attr = getattr(value, "role", None)
        if isinstance(role_attr, Role) and _looks_like_a_capability(
            getattr(value, "capability", None)
        ):
            findings.append(f"{name} carries both a role and a capability")
    return findings


# ---------------------------------------------------------------- role grants
def _require_aware(value: datetime | None, field: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        msg = f"{field} must be timezone-aware; a naive timestamp is a silent bug"
        raise ValueError(msg)
    return value


#: The longest a deputy appointment may run (M1.3.3). Thirty days is the architecture's
#: number, and the reason it is a constant rather than a policy row is that a maximum
#: somebody can raise is not a maximum.
DEPUTY_MAX = timedelta(days=30)

#: How many standing Super Admins the company must keep (M1.3.4).
SUPER_ADMIN_FLOOR: Final = 2


class RoleGrant(BaseModel):
    """One person holding one platform role, optionally as a time-boxed deputy.

    The row behind `role_grant`. The table is not written here.

    `deputy_of` is the deputy flag and the depth-one evidence in one field. A bare boolean
    would say that this grant is a delegation without saying whose, and depth cannot be
    checked without knowing whose: the question "is the person being covered for
    themselves only a deputy" has no answer from a flag.

    `granted_by` and `reason` are required on every grant, deputy or not. A role grant with
    no recorded reason is one nobody can review later, and the review is the only thing
    that ever removes a grant that should not have been made.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str = Field(min_length=1, max_length=128)
    role: Role
    #: Required for the roles in `SCOPE_REQUIRED`, refused for the others (M1.3.2).
    scope: Scope | None = None
    granted_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    granted_at: datetime
    not_after: datetime | None = None
    #: The principal this grant covers for. None for a standing grant.
    deputy_of: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.granted_at, "granted_at")
        _require_aware(self.not_after, "not_after")
        assert_not_a_role(self.principal_id)

        required = spec_for(self.role).scope_required
        if required and self.scope is None:
            msg = (
                f"a {self.role} grant needs a scope; without one it reads as company-wide "
                "and nothing about it fails loudly"
            )
            raise ValueError(msg)
        if not required and self.scope is not None:
            # Rejected: storing the scope and ignoring it. A scope that is written, saved
            # and never consulted is worse than no scope at all, because whoever wrote it
            # believes they narrowed the grant.
            msg = (
                f"a {self.role} grant carries no scope; {self.role} is company-wide, and a "
                "scope stored against it would be read by nobody"
            )
            raise ValueError(msg)
        if self.scope is not None:
            assert_conjunctive(self.scope)
            if self.scope.is_unrestricted():
                msg = f"the scope on this {self.role} grant restricts nothing"
                raise ValueError(msg)
            if is_unsatisfiable(self.scope):
                msg = f"the scope on this {self.role} grant can never match a row"
                raise ValueError(msg)

        if self.not_after is not None and self.not_after <= self.granted_at:
            msg = "not_after must be after granted_at; a grant that expires before it starts"
            raise ValueError(msg)

        if self.deputy_of is not None:
            assert_not_a_role(self.deputy_of)
            if self.deputy_of == self.principal_id:
                msg = "a principal cannot deputise for themselves"
                raise ValueError(msg)
            if self.not_after is None:
                msg = (
                    "a deputy grant must carry not_after; an unbounded deputy is an "
                    "appointment nobody made"
                )
                raise ValueError(msg)
            if self.not_after - self.granted_at > DEPUTY_MAX:
                msg = (
                    f"a deputy grant may run at most {DEPUTY_MAX.days} days; "
                    f"this one runs {(self.not_after - self.granted_at).days}"
                )
                raise ValueError(msg)
        return self

    @property
    def is_deputy(self) -> bool:
        """The deputy flag (M1.3.3). Derived, so it cannot disagree with `deputy_of`."""
        return self.deputy_of is not None

    def is_active(self, now: datetime | None = None) -> bool:
        """True when this grant still confers the role."""
        moment = now or datetime.now(UTC)
        if moment < self.granted_at:
            return False
        return self.not_after is None or moment < self.not_after


def appoint_deputy(
    standing: RoleGrant,
    deputy_principal_id: str,
    *,
    granted_by: str,
    reason: str,
    now: datetime,
    days: int = DEPUTY_MAX.days,
) -> RoleGrant:
    """Appoint a deputy against a standing grant. Depth one, thirty days (M1.3.3).

    The standing grant is an argument rather than a role name, because depth cannot be
    checked from a role name. A deputy appointing a deputy is how a bounded delegation
    becomes an unbounded one: each link is individually within thirty days, and the chain
    renews itself for as long as somebody keeps re-appointing.

    A deputy never outlives the grant it covers. If the standing grant is itself
    time-boxed and ends sooner than thirty days from now, the deputy ends with it.
    """
    if standing.is_deputy:
        msg = (
            f"{standing.principal_id!r} holds {standing.role} as a deputy and cannot appoint "
            "one; deputies are depth one, or a thirty-day delegation renews itself forever"
        )
        raise IdentityError(msg)
    if not standing.is_active(now):
        msg = f"{standing.principal_id!r} does not currently hold {standing.role}"
        raise IdentityError(msg)
    if not 0 < days <= DEPUTY_MAX.days:
        msg = f"a deputy appointment runs 1 to {DEPUTY_MAX.days} days, not {days}"
        raise IdentityError(msg)

    expires = now + timedelta(days=days)
    if standing.not_after is not None:
        expires = min(expires, standing.not_after)
    if expires <= now:
        msg = f"{standing.principal_id!r}'s own grant ends at or before {now}"
        raise IdentityError(msg)

    return RoleGrant(
        principal_id=deputy_principal_id,
        role=standing.role,
        scope=standing.scope,
        granted_by=granted_by,
        reason=reason,
        granted_at=now,
        not_after=expires,
        deputy_of=standing.principal_id,
    )


def check_deputy_depth(grants: Sequence[RoleGrant], now: datetime | None = None) -> list[str]:
    """Every deputy grant whose principal is themselves only a deputy.

    `appoint_deputy` cannot be the only enforcement, because rows also arrive from a seed
    file, a migration or a console form that called the constructor directly. This is the
    sweep that reads the table and says so.
    """
    standing: dict[tuple[str, Role], bool] = {}
    for g in grants:
        if g.is_active(now) and not g.is_deputy:
            standing[(g.principal_id, g.role)] = True

    findings: list[str] = []
    for g in grants:
        # Bound to a local rather than narrowed by `is_deputy`, and deliberately not by an
        # `assert`: an assert vanishes under `python -O`, and a depth check that can be
        # compiled out is not a check. Same reasoning as `department.compose`.
        covering = g.deputy_of
        if covering is None or not g.is_active(now):
            continue
        if not standing.get((covering, g.role), False):
            findings.append(
                f"{g.principal_id} deputises {g.role} for {covering}, who does not hold "
                "it in their own right; deputies are depth one"
            )
    return findings


def standing_super_admins(
    grants: Sequence[RoleGrant], now: datetime | None = None
) -> tuple[RoleGrant, ...]:
    """The active, non-deputy Super Admin grants.

    A deputy does not count towards the floor. Cover for annual leave is not ownership of
    the platform, and a floor that a thirty-day appointment can satisfy is a floor that
    empties itself on a date nobody has in their calendar.
    """
    return tuple(
        g for g in grants if g.role is Role.SUPER_ADMIN and not g.is_deputy and g.is_active(now)
    )


def revoke_role(
    grants: Sequence[RoleGrant],
    target: RoleGrant,
    *,
    now: datetime | None = None,
) -> tuple[RoleGrant, ...]:
    """Remove a role grant by deleting the row, and refuse to drop below the floor.

    Revocation is deletion here for exactly the reason it is deletion in
    `brain.identity.packs`: a "revoked" flag is a negative row, and a model with negative
    rows has an evaluation order.

    The floor (M1.3.4) is enforced against the state *after* the removal, and only when
    the removal is what causes the breach. A company already sitting on one Super Admin
    (someone left, the row was deleted in the database) must still be able to revoke an
    unrelated Auditor grant, or the first thing an operator does is switch the check off.
    """
    remaining = tuple(g for g in grants if g != target)
    if len(remaining) == len(grants):
        msg = "that grant is not in this set; revocation removes a row that exists"
        raise IdentityError(msg)

    before = len(standing_super_admins(grants, now))
    after = len(standing_super_admins(remaining, now))
    if after < SUPER_ADMIN_FLOOR and after < before:
        msg = (
            f"revoking this grant would leave {after} standing Super Admin(s); the floor is "
            f"{SUPER_ADMIN_FLOOR}, because one is a single point of lockout and a deputy "
            "cannot appoint a replacement"
        )
        raise IdentityError(msg)
    return remaining


# ------------------------------------------------------------- the partner
#: What is said to a partner asking anything without a break-glass session open. It names
#: no record and confirms nothing about what exists.
PARTNER_PROMPT = (
    "This account holds no standing access. Open an authorised break-glass session to act."
)


@dataclass(frozen=True)
class NoStandingEntitlement:
    """A partner's standing reach: nothing at all (M1.2.4).

    Not an `EntitlementSet` with no grants, and the difference is the whole point. An empty
    set is a thing that can be intersected with an agent ceiling, cached under an
    `ent_hash`, passed down a delegation chain and logged as a principal's reach. This
    cannot be any of those, because there is no standing reach here to compute with. It is
    `brain.gate.ingress.Unrecognised` for a principal we do know: the identity is real, the
    authority is absent.

    Rejected: `EntitlementSet(principal_id=..., grants=())`. It type-checks everywhere a
    real set does, which means the day someone adds a default grant to the resolver, a
    partner silently acquires it along with everybody else.
    """

    principal_id: str
    prompt: str = PARTNER_PROMPT


def standing_entitlement(
    principal: Principal,
    grants: Sequence[Grant] = (),
) -> EntitlementSet | NoStandingEntitlement:
    """What this principal holds when no break-glass session is open (M1.2.4).

    A partner gets `NoStandingEntitlement` whatever the grant table says. The grants are
    still accepted as an argument and still ignored, deliberately: a partner with rows in
    `capability_grant` is a state that can exist (they were staff once, a migration wrote
    them, someone made a mistake), and the safe behaviour is to hold nothing regardless
    rather than to assume the rows are absent.
    """
    if principal.employment is Employment.PARTNER:
        return NoStandingEntitlement(principal_id=principal.id)
    return EntitlementSet(
        principal_id=principal.id,
        grants=tuple(grants),
        not_after=principal.not_after,
    )


# ---------------------------------------------------------------- break-glass
#: The longest a break-glass session may run (M1.2.5), confirmed by Rupash on 5 September.
#: Four hours is one working session: long enough to finish an install or an incident,
#: short enough that a forgotten session expires before the next working day. Rejected:
#: 24 hours, which is an admin account with a slightly awkward name, and no bound at all,
#: which is the thing this exists to prevent.
#:
#: **This is a ceiling, not the duration.** Whoever authorises a session passes the window
#: they mean, and four hours is what they get if they say nothing. That was Rupash's
#: request and it is the right shape: the person authorising knows whether this is a
#: ten-minute password reset or a four-hour migration, and a fixed window teaches everyone
#: to ask for the maximum every time.
#:
#: The ceiling itself is deliberately not configurable. A maximum an operator can raise is
#: a maximum that gets raised during the incident that made it inconvenient.
BREAK_GLASS_MAX = timedelta(hours=4)

#: Break-glass entries go in their own chain, not the main ledger (M1.2.5). A Super Admin's
#: routine activity runs to thousands of entries a day, and a handful of break-glass rows
#: mixed into it are found only by someone who already knew to look. A separate chain has
#: its own head, so "has anything happened here" is one comparison rather than a query.
BREAK_GLASS_CHAIN: Final = "break_glass"

#: Session ids must survive `AuditEntry.subject`, which is `<kind>:<id>` with the id
#: matching the ledger's IDENTIFIER grammar. Pinned here so an unloggable id is refused at
#: the point the session is opened, rather than when the audit write fails afterwards.
SESSION_ID_PATTERN = r"^[A-Za-z0-9_.@-]{1,120}$"

_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


class BreakGlassReason(enum.StrEnum):
    """Why a session was opened. A closed vocabulary, for two reasons.

    It is recordable: every member matches the ledger's field-name grammar, so the reason
    survives `redact_details` intact and the audit row says something. Free text would be
    redacted to nothing, and an audit trail of "REDACTED" is not an audit trail.

    It is countable: "how many incident-response sessions last quarter" is a query rather
    than a reading exercise.
    """

    INSTALL = "install"
    INCIDENT_RESPONSE = "incident_response"
    DATA_RECOVERY = "data_recovery"
    LOCKOUT = "lockout"


@dataclass(frozen=True)
class Notification:
    """The notice that a break-glass session was opened.

    Returned alongside the session by `open_break_glass`, never produced separately. That
    is the enforcement: there is no code path that yields a session without also yielding
    the thing that tells somebody about it, so "we forgot to send the notification" has to
    be a decision made by a caller holding the notice in their hand.
    """

    recipients: tuple[str, ...]
    session_id: str
    principal_id: str
    reason: BreakGlassReason
    opened_at: datetime
    expires_at: datetime

    @property
    def summary(self) -> str:
        return (
            f"Break-glass session {self.session_id} opened for {self.principal_id} "
            f"({self.reason}) at {self.opened_at.isoformat()}, "
            f"expiring {self.expires_at.isoformat()}."
        )


class BreakGlassSession(BaseModel):
    """A time-boxed, separately audited, notified elevation (M1.2.5).

    This is how a partner with no standing entitlement does an install, and how anyone
    else acts outside what they hold. Every field on it is a constraint rather than
    metadata:

    - `expires_at` bounds it, and `to_entitlement` puts that bound on the returned
      `EntitlementSet` so expiry is enforced by the machinery that already enforces a
      contractor's, rather than by a new check somebody has to remember to call;
    - `grants` is enumerated, never wildcard, so what the session could reach is a list
      an auditor can read rather than a claim about it;
    - `authorised_by` is somebody other than the person acting, so the session is a
      decision by the company rather than by the account;
    - `notified` cannot be empty, because a break-glass session nobody is told about is an
      unaudited admin account with an expiry date.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=120, pattern=SESSION_ID_PATTERN)
    principal_id: str = Field(min_length=1, max_length=128)
    reason: BreakGlassReason
    opened_at: datetime
    expires_at: datetime
    #: What the session confers. Explicit, and never derived from a role.
    grants: tuple[Grant, ...]
    #: Who authorised it. Never the principal acting.
    authorised_by: str = Field(min_length=1, max_length=128)
    #: Who was told. Principal ids, at least one.
    notified: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.opened_at, "opened_at")
        _require_aware(self.expires_at, "expires_at")
        assert_not_a_role(self.principal_id)
        if self.expires_at <= self.opened_at:
            msg = "a break-glass session must expire after it opens"
            raise ValueError(msg)
        if self.expires_at - self.opened_at > BREAK_GLASS_MAX:
            msg = (
                f"a break-glass session may run at most {BREAK_GLASS_MAX}; this one runs "
                f"{self.expires_at - self.opened_at}"
            )
            raise ValueError(msg)
        if not self.grants:
            msg = (
                "a break-glass session with no grants confers nothing and would still be "
                "audited and notified as an elevation; write the grants or do not open it"
            )
            raise ValueError(msg)
        if self.authorised_by == self.principal_id:
            msg = (
                "a break-glass session must be authorised by somebody other than the "
                "principal using it; self-authorised elevation is an admin account"
            )
            raise ValueError(msg)
        if not self.notified:
            msg = (
                "a break-glass session must notify at least one principal; one nobody is "
                "told about is an unaudited admin account"
            )
            raise ValueError(msg)
        return self

    def is_open(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return self.opened_at <= moment < self.expires_at

    def to_entitlement(self) -> EntitlementSet:
        """What this session confers, bounded by its own expiry.

        The bound is carried on the set rather than checked by the caller, so an answer
        computed inside the window and served after it is impossible by construction:
        `EntitlementSet.scope_for` returns None once expired, and the `ent_hash` includes
        `not_after`, so a cached answer cannot be reached from the other side of it.
        """
        return EntitlementSet(
            principal_id=self.principal_id,
            grants=self.grants,
            not_after=self.expires_at,
        )

    @property
    def audit_action(self) -> AuditAction:
        """The ledger's own vocabulary. Never a new string invented here."""
        return AuditAction.BREAK_GLASS

    @property
    def audit_subject(self) -> str:
        """`<kind>:<id>` in the ledger's grammar, with `session` as the kind."""
        return f"session:{self.session_id}"

    @property
    def audit_chain(self) -> str:
        return BREAK_GLASS_CHAIN

    def audit_details(self) -> dict[str, str]:
        """The details this session contributes to its audit entry.

        Passed through the ledger's own redactor rather than assembled to satisfy it. The
        two rules about what may appear in a ledger row live in one place, and this is not
        that place; anything here that turns out to be a value gets replaced rather than
        stored.
        """
        return redact_details(
            {
                "reason": self.reason.value,
                "principal": self.principal_id,
                "authorised_by": self.authorised_by,
                "notified": list(self.notified),
                "chain": self.audit_chain,
            }
        )


def open_break_glass(
    *,
    session_id: str,
    principal: Principal,
    reason: BreakGlassReason,
    grants: Sequence[Grant],
    authorised_by: str,
    notify: Iterable[str],
    now: datetime,
    duration: timedelta = BREAK_GLASS_MAX,
) -> tuple[BreakGlassSession, Notification]:
    """Open a session and produce the notice, or refuse. Both, or neither (M1.2.5).

    The pair return is the design. A function that returned only the session would leave
    notification to a caller, and the caller that forgets is the one handling an incident
    at two in the morning. Here the notice exists whether or not anyone sends it, which
    means a session opened and never announced is visible as an unsent `Notification`
    rather than as nothing at all.
    """
    if not _SESSION_ID_RE.match(session_id):
        msg = f"session id {session_id!r} cannot be written to the ledger as a subject"
        raise IdentityError(msg)
    if duration > BREAK_GLASS_MAX:
        msg = f"a break-glass session may run at most {BREAK_GLASS_MAX}, not {duration}"
        raise IdentityError(msg)
    if duration <= timedelta(0):
        msg = "a break-glass session must have a positive duration"
        raise IdentityError(msg)

    recipients = tuple(dict.fromkeys(notify))
    session = BreakGlassSession(
        session_id=session_id,
        principal_id=principal.id,
        reason=reason,
        opened_at=now,
        expires_at=now + duration,
        grants=tuple(grants),
        authorised_by=authorised_by,
        notified=recipients,
    )
    notice = Notification(
        recipients=recipients,
        session_id=session.session_id,
        principal_id=session.principal_id,
        reason=session.reason,
        opened_at=session.opened_at,
        expires_at=session.expires_at,
    )
    return session, notice


def reach_during(
    principal: Principal,
    session: BreakGlassSession | None,
    grants: Sequence[Grant] = (),
    now: datetime | None = None,
) -> EntitlementSet | NoStandingEntitlement:
    """What a principal may exercise right now, with or without a session open.

    A partner with an open session holds the session's grants and nothing else; with no
    session they hold `NoStandingEntitlement`. Standing grants are never unioned with a
    session's, because union is how a temporary elevation becomes a permanent one: the
    session ends, and whatever the union produced has already been cached under an
    `ent_hash` that does not know it.
    """
    if session is not None and session.principal_id != principal.id:
        msg = "that break-glass session belongs to a different principal"
        raise IdentityError(msg)
    if session is not None and session.is_open(now):
        return session.to_entitlement()
    return standing_entitlement(principal, grants)


# ----------------------------------------------------- the Approver consistency check
#: The verb that actually decides whether somebody may approve something.
APPROVE_VERB: Final = "approve"


class RoleMismatchKind(enum.StrEnum):
    """The two ways the Approver role and the approve capability can disagree."""

    #: Holds the role, holds no approve capability. The role does nothing for them.
    ROLE_WITHOUT_CAPABILITY = "role_without_capability"
    #: Holds an approve capability, does not hold the role. They can approve; the console
    #: will not list them among the people who can.
    CAPABILITY_WITHOUT_ROLE = "capability_without_role"


@dataclass(frozen=True)
class RoleMismatch:
    """One person whose role and capability disagree, and which way round."""

    principal_id: str
    kind: RoleMismatchKind

    def __str__(self) -> str:
        if self.kind is RoleMismatchKind.ROLE_WITHOUT_CAPABILITY:
            return f"{self.principal_id} holds the Approver role and no approve capability"
        return f"{self.principal_id} can approve but does not hold the Approver role"


def approver_mismatches(
    role_grants: Iterable[RoleGrant],
    entitlements: Mapping[str, EntitlementSet],
) -> tuple[RoleMismatch, ...]:
    """Where the Approver role and the approve capability disagree (M1.3.5, decided 5 Sep).

    **The capability decides. Always.** The role is a label the console filters on, not an
    authority, which is the same rule the rest of this module follows: no role implies a
    capability, including Super Admin. Nothing here changes what anybody may do.

    So why report at all. Because both disagreements are silent and neither looks wrong on
    a screen. Somebody given the Approver role and no capability cannot approve anything,
    and the person who granted the role believes they made them an approver. Somebody with
    the capability and no role can approve, and the console listing "our approvers" leaves
    them out. Each is a configuration mistake that surfaces as an absence, which is the
    hardest kind to notice.

    Returned rather than raised. This is a report for an administrator to read, not a
    refusal: refusing would mean an incomplete configuration stopped people working, and
    the state is common and temporary during onboarding.
    """
    holds_role = {grant.principal_id for grant in role_grants if grant.role is Role.APPROVER}
    holds_capability = {
        principal_id
        for principal_id, entitlement in entitlements.items()
        if any(grant.capability.verb == APPROVE_VERB for grant in entitlement.grants)
    }

    out = [
        RoleMismatch(principal_id=pid, kind=RoleMismatchKind.ROLE_WITHOUT_CAPABILITY)
        for pid in sorted(holds_role - holds_capability)
    ]
    out += [
        RoleMismatch(principal_id=pid, kind=RoleMismatchKind.CAPABILITY_WITHOUT_ROLE)
        for pid in sorted(holds_capability - holds_role)
    ]
    return tuple(out)
