"""Departments, the scopes they own, and the boundary between two of them.

A department here is not a container. It is a predicate over rows, and the department
record exists only to give that predicate a name, an owner and somewhere to hang an admin.
Everything else follows from that single decision: the ninth department is configuration
rather than a rebuild, a person can sit in two departments without a second copy of
anything, and someone who transfers reads a different row set on their next request with
no reindex and no cache purge.

Four things break without this module.

**A department admin with no scope is a super admin nobody appointed.** The assignment
path refuses an unrestricted scope for the same reason the column is NOT NULL: in this
model null and unrestricted are the same mistake, and it is the mistake that quietly hands
one team's admin the whole company.

**Composition could widen.** Folding a principal's scopes is the one operation where a
mistake turns two narrow grants into a wide one. `compose` intersects, checks the result
against every input before returning it, and refuses an empty list rather than returning
the identity, because "no scopes" arriving from a failed query must never mean everything.

**Multi-department membership gets confused with composition.** A person in Sales and Web
is one scope with a membership test, written at grant time by an authority that could have
written the wide grant anyway. It is not two scopes combined at request time, and nothing
here offers a way to reach the first from the second.

**A cross-department question leaks through its own refusal.** The plan is built from the
asker's scopes and the departments the question named, and from nothing else. It never
sees a row, so it cannot count what it is hiding, and a department that exists but is out
of reach produces exactly the text a department that does not exist produces.

The table shapes here are the domain records only: no SQLAlchemy model and no migration is
written in this module, and the collision check is the check itself rather than its wiring
into CI. The qualification sits above the line rather than on it, because that line is
parsed for ids and prose on it is read as claims whatever it says about them.

Task ids: M2.1.1, M2.1.4, M2.1.5, M2.2.1, M2.2.2, M2.2.3, M2.2.4
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from brain.core.scope import Clause, Op, Scope
from brain.core.scope_sql import (
    assert_conjunctive,
    is_unsatisfiable,
    parse_predicate,
    scope_narrows,
    to_predicate,
)

#: One namespace, three registries reading from it. Dots are excluded so a slug can never
#: be mistaken for a tool name, which is `source.verb_noun`.
SLUG_PATTERN = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
SLUG_RE = re.compile(SLUG_PATTERN)

#: The field every department predicate tests. Named once so the starter generator, the
#: membership constructor and the reachability test cannot drift apart.
DEPARTMENT_FIELD = "department"

#: Used by the hierarchical starter scope. A team under Web carries `web.projects`, which
#: is the shape the prefix operator exists for.
PATH_FIELD = "scope_path"

#: Set on a row a department is willing to expose outside itself. It is what the
#: request-access route can actually grant without a human deciding case by case.
SHARED_FIELD = "cross_department_visible"

#: One sentence, one placeholder, no count. The wording is a module constant rather than
#: an f-string at the call site so that it cannot acquire "3 items" the week someone
#: decides the message is unhelpful.
GAP_TEMPLATE = "{department} is outside the scopes you hold, so nothing from it is here."

REQUEST_ACCESS_TEMPLATE = "You can request access to {department}."

#: A route, not a URL. Whoever renders it owns the prefix.
REQUEST_ACCESS_ROUTE = "request-access/department/{department}"


class DepartmentError(Exception):
    """Raised when a department, an admin assignment or a composition would be unsafe.

    Like `PredicateRefusedError`, this is an authoring-time failure and never reaches a
    person asking a question. It is separate from the `brain.core.errors` taxonomy on
    purpose: those five outcomes describe an answer, and this describes a refusal to
    write a row.
    """


# ------------------------------------------------------------ the registries
class ScopeRecord(BaseModel):
    """A named scope. The row behind `scope table: slug, predicate jsonb, department flag`.

    Validation runs on construction rather than in a helper, because a helper is something
    a loader can be written around. Two rules, and both of them refuse rather than repair:
    the predicate must satisfy the conjunction-only grammar, and it must be able to match
    something. A saved scope that can never match is dead configuration that looks exactly
    like a permission bug from the far end of a query.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    scope: Scope
    #: True for the one scope that *is* a department, as opposed to a scope written inside
    #: one. Only a flagged record may back a department or bound its admin.
    is_department: bool = False
    label: str = Field(default="", max_length=120)

    def model_post_init(self, _context: object, /) -> None:
        assert_conjunctive(self.scope)
        if is_unsatisfiable(self.scope):
            msg = f"scope {self.slug!r} cannot match any row; it is a permission bug with a name"
            raise ValueError(msg)
        if self.is_department and self.scope.is_unrestricted():
            msg = f"scope {self.slug!r} is flagged as a department and restricts nothing"
            raise ValueError(msg)

    @classmethod
    def from_predicate(
        cls,
        slug: str,
        document: Mapping[str, Any],
        *,
        is_department: bool = False,
        label: str = "",
    ) -> Self:
        """Build from the stored jsonb form."""
        return cls(
            slug=slug,
            scope=parse_predicate(document),
            is_department=is_department,
            label=label,
        )

    def predicate(self) -> dict[str, Any]:
        """The stored jsonb form. Round trips with `from_predicate`."""
        return to_predicate(self.scope)


class Department(BaseModel):
    """A department under a company. Flat by design.

    There is no parent department field. Nesting departments makes an entitlement question
    recursive, and the level people actually mean by a sub-department is a team, which is
    its own table with its own membership. A hierarchy that is wanted anyway is expressed
    as a prefix scope over `scope_path`, which narrows without changing the shape of the
    entitlement lookup.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    #: The `ScopeRecord` that defines this department. Never optional: a department with no
    #: predicate is a label, and a label cannot decide who sees what.
    scope_slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)


class DepartmentAdmin(BaseModel):
    """An admin appointment, bounded by a scope that cannot be absent.

    The scope is what the whole role means. A Department Admin grants within their scope,
    approves within their scope, and sees a console filtered to it; with the scope missing
    every one of those reads as company-wide, and none of them fail loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str = Field(min_length=1, max_length=128)
    department_slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    scope: Scope

    def model_post_init(self, _context: object, /) -> None:
        assert_conjunctive(self.scope)
        if self.scope.is_unrestricted():
            msg = (
                f"admin {self.principal_id!r} of {self.department_slug!r} has an unrestricted "
                "scope; an unbounded department admin is a super admin nobody appointed"
            )
            raise ValueError(msg)
        if is_unsatisfiable(self.scope):
            msg = f"admin {self.principal_id!r} of {self.department_slug!r} has an empty scope"
            raise ValueError(msg)


def assign_department_admin(
    department: Department,
    scope_record: ScopeRecord,
    principal_id: str,
    *,
    within: Scope | None = None,
) -> DepartmentAdmin:
    """Appoint an admin, bounded by the department's own scope.

    The department's scope record is an argument rather than something looked up, so an
    appointment cannot be written without producing the predicate that bounds it. That is
    the "scope not null" rule expressed where it holds instead of in a column constraint
    that a loader can bypass.

    `within` narrows further, for an admin who runs part of a department. It is
    intersected, never substituted, so it can only ever shrink the appointment.
    """
    if scope_record.slug != department.scope_slug:
        msg = (
            f"{scope_record.slug!r} is not the scope of {department.slug!r}; "
            f"that department is defined by {department.scope_slug!r}"
        )
        raise DepartmentError(msg)
    if not scope_record.is_department:
        msg = f"{scope_record.slug!r} is not flagged as a department scope"
        raise DepartmentError(msg)

    scope = scope_record.scope if within is None else compose((scope_record.scope, within))
    return DepartmentAdmin(
        principal_id=principal_id,
        department_slug=department.slug,
        scope=scope,
    )


# --------------------------------------------------------------- composition
def compose(scopes: Sequence[Scope]) -> Scope:
    """Fold a principal's scopes into the one scope a request runs under.

    Conjunction, always. The result is checked against every input before it is returned:
    the check cannot fail today, because intersection keeps both clause sets and every
    clause entails itself, and it is here so that a future operator which reorders,
    deduplicates or simplifies clauses cannot widen without this raising.

    Written as a raise rather than an `assert`, because `assert` disappears under
    `python -O` and a permission invariant that can be compiled out is not an invariant.

    An empty sequence raises. The identity element of conjunction is the unrestricted
    scope, and returning it would be mathematically tidy and operationally catastrophic:
    a principal whose scope list came back empty from a failed query would be handed the
    whole company.
    """
    ordered = tuple(scopes)
    if not ordered:
        msg = "compose() needs at least one scope; an empty list is not the unrestricted scope"
        raise DepartmentError(msg)

    result = ordered[0]
    for scope in ordered[1:]:
        result = result.intersect(scope)

    for scope in ordered:
        if not scope_narrows(result, scope):
            msg = f"composition widened past {to_predicate(scope)}; refusing to return it"
            raise DepartmentError(msg)
    return result


def membership_scope(departments: Sequence[str], field_name: str = DEPARTMENT_FIELD) -> Scope:
    """The single scope for a principal who sits in several departments.

    This is a constructor, not a composition, and the difference is the whole permission
    model. It is written when a grant is written, by someone who could have written the
    wider grant directly; it is not something two narrow grants can be combined into at
    request time. `compose` has no path to this, and that is deliberate rather than
    incidental: intersecting `department = sales` with `department = web` yields a scope
    that matches nothing, which is the correct conservative answer.

    One department renders as an equality rather than a one-member membership test. Both
    admit the same rows, and two spellings of one meaning would hash differently, so the
    same person would miss their own cache entry and appear in traces as two principals.
    """
    unique = tuple(sorted(set(departments)))
    if not unique:
        msg = "a membership scope over no departments would match nothing"
        raise DepartmentError(msg)
    if len(unique) == 1:
        return Scope(clauses=(Clause(field=field_name, op=Op.EQ, value=unique[0]),))
    return Scope(clauses=(Clause(field=field_name, op=Op.IN, value=unique),))


def admits_department(scope: Scope, department: str) -> bool:
    """True when this scope could reach any row in that department.

    Asked as a satisfiability question rather than by looking for a department clause,
    because most scopes do not carry one. A scope that never mentions the field genuinely
    does reach every department, and a test that looked for the clause would report the
    opposite.
    """
    return not is_unsatisfiable(scope.intersect(department_scope(department)))


def department_scope(department: str) -> Scope:
    """The predicate that is a department. One spelling, used everywhere."""
    return Scope(clauses=(Clause(field=DEPARTMENT_FIELD, op=Op.EQ, value=department),))


# ------------------------------------------------------- the creation wizard
@dataclass(frozen=True)
class DepartmentDraft:
    """What the wizard produces: a department and its starter scopes, written or not at all.

    A draft rather than a sequence of writes, because a wizard that saves as it goes leaves
    half a department behind when step three fails, and half a department is one with a
    name and no predicate, which is the shape that reads as company-wide.
    """

    department: Department
    scopes: tuple[ScopeRecord, ...]

    @property
    def defining_scope(self) -> ScopeRecord:
        """The one record carrying the department flag."""
        return self.scopes[0]


def starter_scopes(slug: str) -> tuple[ScopeRecord, ...]:
    """The scopes a new department starts with. The first one is the department itself.

    Three, and each one is strictly narrower than the department scope, which is what makes
    them safe to create unattended. Two candidates were rejected:

    A read-only scope, because read-only is a verb in the capability and not a predicate
    over rows. Encoding it here would put one rule in two places and let them disagree.

    A personal scope, because personal means `owner = this principal` and there is no
    principal at the point a department is created. Generating one with a placeholder owner
    produces a scope that matches nothing or, worse, matches rows with no owner set.
    """
    if not SLUG_RE.match(slug):
        msg = f"{slug!r} is not a usable department slug"
        raise DepartmentError(msg)

    own = ScopeRecord(
        slug=slug,
        scope=department_scope(slug),
        is_department=True,
        label=f"Everything belonging to {slug}",
    )
    # The prefix clause alone would admit a row in another department that happens to
    # carry a matching path, so the department clause stays in. Every starter scope is
    # the department scope plus something, never something else.
    tree = ScopeRecord(
        slug=f"{slug}_tree",
        scope=own.scope.intersect(
            Scope(clauses=(Clause(field=PATH_FIELD, op=Op.PREFIX, value=f"{slug}."),))
        ),
        label=f"Teams under {slug}",
    )
    shared = ScopeRecord(
        slug=f"{slug}_shared",
        scope=own.scope.intersect(
            Scope(clauses=(Clause(field=SHARED_FIELD, op=Op.EQ, value="true"),))
        ),
        label=f"Rows {slug} has marked visible outside itself",
    )
    return (own, tree, shared)


def create_department(company_id: str, slug: str, name: str) -> DepartmentDraft:
    """The wizard, as a function. Produces a department and its starter scopes."""
    scopes = starter_scopes(slug)
    department = Department(
        company_id=company_id,
        slug=slug,
        name=name,
        scope_slug=scopes[0].slug,
    )
    return DepartmentDraft(department=department, scopes=scopes)


# ---------------------------------------------------------- the CI collision
@dataclass(frozen=True)
class Collision:
    """One name claimed by two registries."""

    slug: str
    detail: str

    def __str__(self) -> str:
        return f"{self.slug}: {self.detail}"


def _normalise(slug: str) -> str:
    """Fold the spellings a person would read as the same name."""
    return slug.strip().lower().replace("-", "_")


def check_slug_collisions(
    scope_slugs: Iterable[str],
    agent_slugs: Iterable[str] = (),
    tool_objects: Iterable[str] = (),
) -> list[Collision]:
    """Every name claimed by more than one registry.

    Three registries share one namespace in the places that matter: a grant reads
    "read:client in finance", a request-access route is keyed by slug, and the console
    resolves one typed name against all three. If an agent and a scope are both called
    `finance`, "grant Priya finance" has two meanings, and the safe reading is not the one
    a resolver picks by declaration order.

    Comparison is on the folded form, so `client-ops` and `client_ops` collide. Two names
    that only a machine can tell apart are a collision in the interface even when the
    database is content with them.
    """
    seen: dict[str, str] = {}
    findings: list[Collision] = []

    for registry, slugs in (
        ("scope", scope_slugs),
        ("agent", agent_slugs),
        ("tool object", tool_objects),
    ):
        for raw in slugs:
            key = _normalise(raw)
            previous = seen.get(key)
            if previous is None:
                seen[key] = registry
                continue
            findings.append(
                Collision(
                    slug=raw,
                    detail=(
                        f"claimed by the {previous} registry and the {registry} registry; "
                        "one name must mean one thing"
                    ),
                )
            )
    return findings


# --------------------------------------------------- the cross-department gap
@dataclass(frozen=True)
class Gap:
    """A department the question named and the asker cannot reach.

    Carries the name the asker typed and nothing else. There is no field for how much is
    behind it, no field for whether it exists, and no field a count could be put in later
    without someone noticing.
    """

    department: str

    @property
    def message(self) -> str:
        return GAP_TEMPLATE.format(department=self.department)

    @property
    def request_access(self) -> str:
        return REQUEST_ACCESS_TEMPLATE.format(department=self.department)

    @property
    def route(self) -> str:
        return REQUEST_ACCESS_ROUTE.format(department=self.department)


@dataclass(frozen=True)
class DepartmentFilter:
    """One department the asker does reach, and the scope to read it under."""

    department: str
    scope: Scope


@dataclass(frozen=True)
class CrossDepartmentPlan:
    """What to run, what to say about the rest, and where to send someone who needs more.

    `combined` is None when nothing is reachable, and None is not the unrestricted scope.
    A plan that reduced an empty filter list to "no WHERE clause" is the single most
    expensive bug available in this design, so the empty case has a value of its own type
    rather than a value that happens to mean everything.
    """

    filters: tuple[DepartmentFilter, ...]
    gaps: tuple[Gap, ...]
    combined: Scope | None

    @property
    def reachable(self) -> tuple[str, ...]:
        return tuple(f.department for f in self.filters)

    @property
    def answerable(self) -> bool:
        return bool(self.filters)


def plan_cross_department(asker: Scope, departments: Sequence[str]) -> CrossDepartmentPlan:
    """Intersect what the asker reaches, state the gap for the rest, offer a route.

    `departments` is what the *question* named. It must never be the department registry:
    passing every department in the company would turn the gap list into an inventory of
    departments the asker cannot see, and the asker would learn the org chart from a
    refusal. Nothing in the signature can enforce that, which is why it is said here.

    This function is never given a row, a count or a registry, so it cannot report how much
    it is withholding even by accident. A department that exists and is out of reach
    produces the same output as a department that was never created, because the decision
    is made entirely from the asker's own scopes.
    """
    assert_conjunctive(asker)
    filters: list[DepartmentFilter] = []
    gaps: list[Gap] = []

    for name in dict.fromkeys(departments):
        if admits_department(asker, name):
            filters.append(
                DepartmentFilter(department=name, scope=asker.intersect(department_scope(name)))
            )
        else:
            gaps.append(Gap(department=name))

    reachable = tuple(f.department for f in filters)
    combined = compose((asker, membership_scope(reachable))) if reachable else None
    return CrossDepartmentPlan(
        filters=tuple(filters),
        gaps=tuple(gaps),
        combined=combined,
    )
