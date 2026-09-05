"""Teams inside a department, and the two things a grant may be about.

A team is the level people actually mean when they ask for a sub-department. Departments
are flat on purpose (`brain.core.department` says why: nesting makes the entitlement
question recursive), so the structure below a department lives here instead, as a
predicate over `scope_path` rather than as a parent pointer.

Three things break without this module.

**"The Web design pod" has nowhere to live.** Without a team, the only ways to express it
are a ninth department, which duplicates a department's whole apparatus for six people,
or a per-person grant repeated six times, which drifts the first time someone joins.

**A grant can only ever name a person.** Six identical grants written one at a time are
six chances to write the seventh differently, and when somebody leaves, six rows to find.
`TeamSubject` makes the team the subject, so membership is the only thing that changes.

**A team becomes a second, quieter role.** It does not, and the guard is structural:
`SubjectKind` has exactly two members and never a third, so a role cannot be a subject
even by mistake. `assert_not_a_role` covers the other direction, where a role name arrives
as a string from a loader.

Team membership widens the *set* of capabilities a person holds and can never widen the
*scope* of one they already hold: `EntitlementSet.scope_for` intersects across matching
grants, so a second grant of the same capability narrows. That asymmetry is what makes
"add them to the team" a safe operation to hand a department admin.

M1.5.1 names a `team` table. This is the type and its rules; the table belongs to whoever
owns `src/brain/tables`.

Task ids: M1.5.1, M1.5.2, M1.5.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain.core.department import DEPARTMENT_FIELD, PATH_FIELD, SLUG_PATTERN
from brain.core.scope import Clause, Op, Scope
from brain.identity.roles import IdentityError, assert_not_a_role

#: The field a team predicate tests. Named once, like `DEPARTMENT_FIELD`, so the
#: constructor, the membership test and the "does this scope mention a team" check cannot
#: drift apart.
TEAM_FIELD: Final = "team"

#: `web.design`: the department slug, a dot, the team slug. The same shape
#: `department.starter_scopes` already builds prefix scopes against, so a team path is
#: reachable by the department's `<slug>_tree` scope without anything new being defined.
TEAM_PATH_PATTERN = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"

_TEAM_PATH_RE = re.compile(TEAM_PATH_PATTERN)
_SLUG_RE = re.compile(SLUG_PATTERN)


class TeamError(IdentityError):
    """Raised when a team, a membership or a subject would be unsafe to write."""


# ------------------------------------------------------------------- the team
class Team(BaseModel):
    """A team within a department (M1.5.1).

    `department_slug` is not optional, and that is the whole content of "within a
    department". A team floating outside one has no scope to be narrower than, so a grant
    naming it would be bounded by nothing, which is the department-admin failure again
    wearing a smaller name.

    There is no parent team field, for the reason `Department` has no parent department:
    it would make the entitlement lookup recursive. A deeper structure that is genuinely
    wanted is a longer `scope_path` and a prefix clause, which narrows without changing
    the shape of the lookup.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: str = Field(min_length=1, max_length=128)
    department_slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _check(self) -> Self:
        assert_not_a_role(self.slug)
        if self.slug == self.department_slug:
            # One name, two things: a grant reading "web" would be ambiguous between the
            # department and the team, and the safe reading is not the one a resolver
            # picks by declaration order. Same rule as `check_slug_collisions`.
            msg = f"team {self.slug!r} has the same name as its department"
            raise ValueError(msg)
        return self

    @property
    def path(self) -> str:
        """`department.team`. The value a `scope_path` prefix clause matches against."""
        return f"{self.department_slug}.{self.slug}"

    def scope(self) -> Scope:
        """The predicate that is this team.

        Both clauses, always. The team clause alone would admit a row in another
        department that happens to carry a team of the same name, which is exactly the
        collision `starter_scopes` keeps the department clause in for.
        """
        return Scope(
            clauses=(
                Clause(field=DEPARTMENT_FIELD, op=Op.EQ, value=self.department_slug),
                Clause(field=TEAM_FIELD, op=Op.EQ, value=self.slug),
            )
        )

    def subtree_scope(self) -> Scope:
        """Everything at or under this team's path.

        Kept separate from `scope` rather than folded into it. A prefix over `scope_path`
        and an equality on `team` answer different questions, and a scope that quietly did
        both would reach rows that no clause a reader can see accounts for.
        """
        return Scope(
            clauses=(
                Clause(field=DEPARTMENT_FIELD, op=Op.EQ, value=self.department_slug),
                Clause(field=PATH_FIELD, op=Op.PREFIX, value=f"{self.path}."),
            )
        )


def team_path(department_slug: str, team_slug: str) -> str:
    """Build a team path, refusing anything that would not round trip."""
    for part in (department_slug, team_slug):
        if not _SLUG_RE.match(part):
            msg = f"{part!r} is not a usable slug"
            raise TeamError(msg)
    return f"{department_slug}.{team_slug}"


def split_team_path(path: str) -> tuple[str, str]:
    """`web.design` to `("web", "design")`. Refuses anything else."""
    if not _TEAM_PATH_RE.match(path):
        msg = f"{path!r} is not a team path; a team path is <department>.<team>"
        raise TeamError(msg)
    department, _, team = path.partition(".")
    return department, team


# ------------------------------------------------------------- membership
class TeamMembership(BaseModel):
    """One person in one team (M1.5.2).

    `not_after` exists because the common case for a team is a project pod, and a project
    ends. A membership with no end date outlives the work it was created for, and the row
    that outlives its reason is the row nobody reviews.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str = Field(min_length=1, max_length=128)
    team_path: str = Field(min_length=5, max_length=121, pattern=TEAM_PATH_PATTERN)
    since: datetime
    not_after: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        assert_not_a_role(self.principal_id)
        for value, field in ((self.since, "since"), (self.not_after, "not_after")):
            if value is not None and value.tzinfo is None:
                msg = f"{field} must be timezone-aware; a naive timestamp is a silent bug"
                raise ValueError(msg)
        if self.not_after is not None and self.not_after <= self.since:
            msg = "not_after must be after since; a membership that ends before it starts"
            raise ValueError(msg)
        return self

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if moment < self.since:
            return False
        return self.not_after is None or moment < self.not_after


def teams_of(
    principal_id: str,
    memberships: Sequence[TeamMembership],
    now: datetime | None = None,
) -> tuple[str, ...]:
    """The team paths this principal is currently in. Sorted and deduplicated.

    Sorted for the same reason `Scope` normalises its clauses: whatever is built from this
    ends up inside an `ent_hash`, and two orderings of one membership list must not look
    like two different principals in traces.
    """
    return tuple(
        sorted(
            {
                m.team_path
                for m in memberships
                if m.principal_id == principal_id and m.is_active(now)
            }
        )
    )


def members_of(
    path: str,
    memberships: Sequence[TeamMembership],
    now: datetime | None = None,
) -> tuple[str, ...]:
    """The principals currently in this team."""
    split_team_path(path)
    return tuple(
        sorted({m.principal_id for m in memberships if m.team_path == path and m.is_active(now)})
    )


def membership_clause(path: str) -> Clause:
    """The clause a scope uses to reference a team (M1.5.2).

    A scope may name a team the same way it names a department: as an equality on a field
    the row carries. It is not a membership lookup performed at request time. A predicate
    that had to resolve a person to their teams before it could be evaluated would stop
    being a row predicate, which is what `scope_sql` compiles to SQL and what makes "what
    can this person see" answerable by reading the grant.
    """
    _, team = split_team_path(path)
    return Clause(field=TEAM_FIELD, op=Op.EQ, value=team)


def team_scope(path: str) -> Scope:
    """The predicate for a team named by path. One spelling, used everywhere."""
    department, team = split_team_path(path)
    return Scope(
        clauses=(
            Clause(field=DEPARTMENT_FIELD, op=Op.EQ, value=department),
            Clause(field=TEAM_FIELD, op=Op.EQ, value=team),
        )
    )


def references_team(scope: Scope) -> bool:
    """True when this scope tests the team field.

    Used by the console to explain a grant ("bounded to the design team") and by the sweep
    that checks a team grant is actually bounded by a team. Asked of the clause list rather
    than by evaluating anything, because a scope is data.
    """
    return any(c.field == TEAM_FIELD for c in scope.clauses)


# --------------------------------------------------------------- grant subjects
class SubjectKind(enum.StrEnum):
    """What a grant can be about.

    Two members, and there will never be a third called `role` (M1.3.5). The constraint is
    expressed here, in the type, rather than as a check somewhere that a code path can
    avoid: with no role member, a role-subject grant is not something the resolver has to
    refuse, it is something that cannot be constructed.

    The cost of getting this wrong is not a leak on day one. It is that "what can this
    person see" turns from a lookup over their grants into a walk over their roles, their
    roles' grants and the scopes attached to each, and every later feature is built on the
    walk.
    """

    PRINCIPAL = "principal"
    TEAM = "team"


class PrincipalSubject(BaseModel):
    """A grant about one person."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[SubjectKind.PRINCIPAL] = SubjectKind.PRINCIPAL
    principal_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check(self) -> Self:
        assert_not_a_role(self.principal_id)
        return self

    @property
    def key(self) -> str:
        return f"{SubjectKind.PRINCIPAL.value}:{self.principal_id}"


class TeamSubject(BaseModel):
    """A grant about a team (M1.5.3).

    Alongside the individual, never instead of one. Both forms resolve to the same
    `Grant`, so nothing downstream, including the redactor and the cache key, learns that
    a grant arrived through a team. If it did, the answer a person got would depend on how
    their access was written rather than on what it amounts to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[SubjectKind.TEAM] = SubjectKind.TEAM
    team_path: str = Field(min_length=5, max_length=121, pattern=TEAM_PATH_PATTERN)

    @model_validator(mode="after")
    def _check(self) -> Self:
        department, team = split_team_path(self.team_path)
        assert_not_a_role(department)
        assert_not_a_role(team)
        return self

    @property
    def key(self) -> str:
        return f"{SubjectKind.TEAM.value}:{self.team_path}"


#: Discriminated on `kind`, so a stored subject round trips without a guess about which
#: variant a bare mapping meant.
GrantSubject = Annotated[PrincipalSubject | TeamSubject, Field(discriminator="kind")]


def principal_subject(principal_id: str) -> PrincipalSubject:
    return PrincipalSubject(principal_id=principal_id)


def team_subject(path: str) -> TeamSubject:
    return TeamSubject(team_path=path)


def subject_for(identifier: str) -> PrincipalSubject | TeamSubject:
    """Parse `principal:u_1` or `team:web.design`. Refuses anything else.

    Written as an explicit parse rather than a "looks like a path, must be a team" guess.
    A guess that gets it wrong turns a person into a team, and a team grant reaches
    everyone in it.
    """
    kind, sep, rest = identifier.partition(":")
    if not sep:
        msg = f"{identifier!r} is not a subject; a subject is <kind>:<id>"
        raise TeamError(msg)
    match kind:
        case SubjectKind.PRINCIPAL.value:
            return PrincipalSubject(principal_id=rest)
        case SubjectKind.TEAM.value:
            return TeamSubject(team_path=rest)
        case _:
            # Reached by `role:super_admin`, which is precisely the thing M1.3.5 forbids,
            # and by anything else somebody invents. Both get the same refusal.
            msg = (
                f"{kind!r} is not a grant subject kind; grants are about a principal or a "
                f"team, never a role"
            )
            raise TeamError(msg)


def subject_reaches(
    subject: PrincipalSubject | TeamSubject,
    principal_id: str,
    memberships: Sequence[TeamMembership] = (),
    now: datetime | None = None,
) -> bool:
    """True when a grant with this subject applies to this principal.

    A team subject reaches a principal only through an active membership. An expired
    membership reaches nothing, and it is checked here rather than by whoever loaded the
    rows, because a filter in a query is a filter one caller can forget.
    """
    if isinstance(subject, PrincipalSubject):
        return subject.principal_id == principal_id
    return subject.team_path in teams_of(principal_id, memberships, now)


def subjects_for(
    principal_id: str,
    memberships: Sequence[TeamMembership] = (),
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Every subject key that reaches this principal right now.

    The key set, not the grants. Whatever caches an entitlement can compare this cheaply
    to decide whether a membership change invalidates it, without resolving anything.
    """
    keys = [principal_subject(principal_id).key]
    keys.extend(team_subject(p).key for p in teams_of(principal_id, memberships, now))
    return tuple(sorted(keys))


def assert_within_department(
    subject: PrincipalSubject | TeamSubject,
    scope: Scope,
    departments: Iterable[str],
) -> None:
    """Refuse a team grant whose scope leaves the team's own department.

    A department admin may write grants within their scope. A team grant is the one shape
    where that is easy to get wrong, because the team names a department and the scope
    names another, and the resulting row reads as though it were bounded when it is
    bounded to somewhere else.
    """
    if not isinstance(subject, TeamSubject):
        return
    department, _ = split_team_path(subject.team_path)
    if department not in set(departments):
        msg = (
            f"team {subject.team_path!r} sits in {department!r}, which is not among the "
            "departments this grant may be written in"
        )
        raise TeamError(msg)
    named = {c.value for c in scope.clauses if c.field == DEPARTMENT_FIELD and c.op is Op.EQ}
    if named and department not in named:
        msg = (
            f"team {subject.team_path!r} sits in {department!r} but the scope is bounded to "
            f"{sorted(str(n) for n in named)}; the grant would reach nothing or the wrong rows"
        )
        raise TeamError(msg)
