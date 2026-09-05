"""Capability packs, grants with a subject, and revocation as deletion.

`brain.core.entitlement` states the rule this module has to keep: entitlements are
additive only. A field is invisible because no grant covers it, never because a rule took
it away. Everything here is shaped by that one sentence.

Three things break without this module.

**Revocation becomes a negative row.** The obvious way to take access away from a grant
you did not write is to write another row saying "not this", and the moment that exists,
"can X see Y" stops being a lookup and becomes an evaluation-order problem: which row
wins, what happens when two disagree, what does a cached answer from before the deny row
mean. `revoke` deletes. There is no tombstone, no `revoked` flag and no `suspended` flag,
and `subtractive_state` fails the build if one appears, including under a friendlier name.

**Twelve capabilities get typed out per person.** A pack is the named bundle a department
admin actually thinks in, and `expand` is the only way one becomes grants, so a pack
assignment cannot quietly mean something different from the pack.

**A pack assignment escapes its scope.** An assignment carries a scope and cannot be
written without one, for the same reason `DepartmentAdmin` cannot: an unbounded bundle of
capabilities is the widest single row anybody can write, and it looks like an ordinary
one.

`resolve_entitlement` takes no role grants. Not "ignores them": does not accept them. That
is M1.3.5 written as a signature, and `assert_no_role_in_resolution` pins it, because a
resolver that could see roles is one refactor away from consulting them.

M1.4.3 names a `capability_pack` table and M1.4.1 a `capability_grant` table. These are
the types and their rules; the tables belong to whoever owns `src/brain/tables`.

Task ids: M1.4.2, M1.4.3, and the subject half of M1.5.3
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain.core.department import SLUG_PATTERN
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Principal
from brain.core.scope import Scope
from brain.core.scope_sql import assert_conjunctive, is_unsatisfiable
from brain.identity.roles import (
    IdentityError,
    NoStandingEntitlement,
    assert_not_a_role,
    standing_entitlement,
)
from brain.identity.teams import (
    GrantSubject,
    PrincipalSubject,
    TeamMembership,
    TeamSubject,
    subject_reaches,
)


class PackError(IdentityError):
    """Raised when a pack or an assignment would be unsafe to write."""


# ------------------------------------------------------------------- the pack
class CapabilityPack(BaseModel):
    """A named bundle of capabilities (M1.4.3).

    The bundle carries no scope of its own. A pack is "what a maintenance engineer needs
    to do their job"; where they may do it is a property of the person and the assignment,
    not of the job. A pack that carried a scope would have to be duplicated per department,
    which is how eight nearly-identical packs end up disagreeing about one capability.

    Capabilities are sorted and deduplicated on construction, for the reason `Scope`
    normalises its clauses: two spellings of the same bundle must serialise identically or
    the same person hashes to two different entitlements.

    Rejected: refusing a pack that contains both `read:client.*` and `read:client.name`.
    The narrow one is redundant rather than wrong, and refusing it would mean a pack could
    not be widened without first finding and deleting every member the widening now
    covers, which is a review nobody would enjoy and everybody would shortcut.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=120)
    capabilities: tuple[Capability, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        assert_not_a_role(self.slug)
        if not self.capabilities:
            msg = (
                f"pack {self.slug!r} contains no capabilities; an empty pack is a grant "
                "that looks like access and confers none"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "capabilities" in data:
            caps = data["capabilities"]
            if isinstance(caps, Iterable) and not isinstance(caps, str | bytes):
                seen = {c.value if isinstance(c, Capability) else str(c) for c in caps}
                return {**data, "capabilities": tuple(Capability(value=v) for v in sorted(seen))}
        return data

    def covers(self, capability: Capability) -> bool:
        """True when some member of this pack implies `capability`."""
        return any(c.covers(capability) for c in self.capabilities)


class PackAssignment(BaseModel):
    """A pack given to a subject, bound to a scope (M1.4.3).

    The scope is required and may not be unrestricted. A pack is the largest thing anyone
    assigns in one action, so it is also the largest thing an unbounded assignment hands
    over, and it is the row least likely to be read carefully because the interesting part
    is the pack name.

    Rejected: defaulting the scope to the assigner's own. It would make the widest
    assignment in the system the one that took the least typing, and it would silently
    widen every time the assigner's own scope widened.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: GrantSubject
    pack_slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    scope: Scope
    granted_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    granted_at: datetime
    not_after: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.granted_at, "granted_at")
        _require_aware(self.not_after, "not_after")
        assert_not_a_role(self.pack_slug)
        assert_conjunctive(self.scope)
        if self.scope.is_unrestricted():
            msg = (
                f"assignment of pack {self.pack_slug!r} restricts nothing; a scope-bound "
                "assignment that is bound to everything is the widest row in the system"
            )
            raise ValueError(msg)
        if is_unsatisfiable(self.scope):
            msg = f"assignment of pack {self.pack_slug!r} has a scope that matches no row"
            raise ValueError(msg)
        if self.not_after is not None and self.not_after <= self.granted_at:
            msg = "not_after must be after granted_at"
            raise ValueError(msg)
        return self

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if moment < self.granted_at:
            return False
        return self.not_after is None or moment < self.not_after


def _require_aware(value: datetime | None, field: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        msg = f"{field} must be timezone-aware; a naive timestamp is a silent bug"
        raise ValueError(msg)
    return value


# ---------------------------------------------------------------- the grant
class SubjectGrant(BaseModel):
    """One capability, one scope, one subject: a person or a team (M1.5.3).

    The row behind `capability_grant`, plus the subject that makes a team grant possible.
    The table is not written here.

    `as_grant` throws the subject away on purpose. Downstream, including the redactor, the
    agent intersection and the cache key, must not be able to tell whether access arrived
    through a person or through a team, or the answer somebody gets starts depending on
    how their access happened to be written rather than on what it amounts to.

    There is no `effect` field, no `allow` boolean and no `revoked` flag. Every row here
    grants. Taking access away is `revoke`, which removes the row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: GrantSubject
    capability: Capability
    scope: Scope
    granted_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    granted_at: datetime
    not_after: datetime | None = None
    #: Set when this grant came from expanding a pack. It is provenance, so that revoking
    #: an assignment can find the rows it produced; it is never consulted at resolve time.
    from_pack: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_aware(self.granted_at, "granted_at")
        _require_aware(self.not_after, "not_after")
        assert_conjunctive(self.scope)
        if is_unsatisfiable(self.scope):
            msg = f"grant of {self.capability.value} has a scope that matches no row"
            raise ValueError(msg)
        if self.not_after is not None and self.not_after <= self.granted_at:
            msg = "not_after must be after granted_at"
            raise ValueError(msg)
        return self

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if moment < self.granted_at:
            return False
        return self.not_after is None or moment < self.not_after

    def as_grant(self) -> Grant:
        """The core `Grant` this row amounts to. Subject, reason and provenance dropped."""
        return Grant(capability=self.capability, scope=self.scope)


def expand(pack: CapabilityPack, assignment: PackAssignment) -> tuple[SubjectGrant, ...]:
    """Turn a pack assignment into the grants it means (M1.4.3).

    The only route from a pack to a grant. A resolver that read packs directly would be a
    second implementation of what a pack means, and the two would disagree the first time
    somebody added a rule to one of them.

    Every produced grant carries the assignment's scope, unchanged. Rejected: intersecting
    with something from the pack. A pack has no scope, and inventing one here would make
    the same pack mean different things in different departments while looking identical
    in the console.
    """
    if pack.slug != assignment.pack_slug:
        msg = f"assignment names pack {assignment.pack_slug!r}, not {pack.slug!r}"
        raise PackError(msg)
    return tuple(
        SubjectGrant(
            subject=assignment.subject,
            capability=capability,
            scope=assignment.scope,
            granted_by=assignment.granted_by,
            reason=assignment.reason,
            granted_at=assignment.granted_at,
            not_after=assignment.not_after,
            from_pack=pack.slug,
        )
        for capability in pack.capabilities
    )


# -------------------------------------------------------------- revocation
def revoke[T](rows: Sequence[T], *, where: Callable[[T], bool]) -> tuple[T, ...]:
    """Remove every row matching `where` (M1.4.2). Deletion, never a negative row.

    Generic over the row type because the rule is the same for grants, assignments and
    role grants: the way access goes away is that the row that conferred it is not there
    any more. Nothing anywhere adds a row that subtracts.

    A deny row is attractive for about ten minutes: it lets a department admin take one
    field away from somebody whose grant they did not write. What it costs is that no
    grant can be read on its own ever again, that resolution acquires an order, that two
    denies can conflict, and that a cached answer is now a claim about which rows existed
    at the moment it was computed. `brain.core.entitlement` refuses the mechanism; this
    refuses the workaround.

    Returns a new tuple. The input is not mutated, so a caller comparing before and after
    can log what went, which is what the audit row for a revocation is built from.
    """
    return tuple(row for row in rows if not where(row))


def revoke_capability(
    grants: Sequence[SubjectGrant],
    subject: PrincipalSubject | TeamSubject,
    capability: Capability,
) -> tuple[SubjectGrant, ...]:
    """Delete this subject's grants of exactly this capability.

    Exact match on the capability value, not `covers`. Revoking `read:client.name` must not
    silently delete a `read:client.*` grant that happens to imply it: those are different
    decisions, made by possibly different people, and removing the wider one because
    somebody asked about the narrower one takes away more than was asked for.
    """
    return revoke(
        grants,
        where=lambda g: g.subject == subject and g.capability.value == capability.value,
    )


def revoke_assignment(
    assignments: Sequence[PackAssignment],
    grants: Sequence[SubjectGrant],
    subject: PrincipalSubject | TeamSubject,
    pack_slug: str,
) -> tuple[tuple[PackAssignment, ...], tuple[SubjectGrant, ...]]:
    """Delete an assignment and the grants it produced. Both, or the pack half survives.

    Returned as a pair rather than applied in two calls, because the failure mode of two
    calls is that the second one does not happen and the expanded grants outlive the
    assignment that explains them. A grant with a `from_pack` pointing at nothing is
    access nobody can account for.
    """
    remaining_assignments = revoke(
        assignments,
        where=lambda a: a.subject == subject and a.pack_slug == pack_slug,
    )
    remaining_grants = revoke(
        grants,
        where=lambda g: g.subject == subject and g.from_pack == pack_slug,
    )
    return remaining_assignments, remaining_grants


# --------------------------------------------------- the additive-only guard
#: Names that would mean a row subtracts. Matched against class names and model field
#: names across the identity package, never against prose: every one of these words
#: appears in the comments explaining why the thing it names does not exist.
#:
#: `revoked` is here and `revoke` is not, deliberately. A function that removes a row is
#: the mechanism; a field called `revoked` is the tombstone, which is a negative row with
#: a friendlier name and exactly the resolution-order problem a deny list has.
SUBTRACTIVE_NAME = (
    r"(?i)(deny|denied|denies|negative|suspend|suspended|excluded|blocked|revoked|"
    r"disallow|forbidden|blacklist|blocklist|withdrawn|inactive_flag)"
)

_SUBTRACTIVE_RE = re.compile(SUBTRACTIVE_NAME)


def _field_names(obj: type[Any]) -> list[str]:
    model_fields = getattr(obj, "model_fields", None)
    if isinstance(model_fields, Mapping):
        return [str(k) for k in model_fields]
    if is_dataclass(obj):
        return [f.name for f in dataclass_fields(obj)]
    return []


def subtractive_state(module: ModuleType) -> list[str]:
    """Every name in this module that would let a row subtract (M1.4.2).

    Structural rather than textual. It reads class names and model field names, so an
    explanation of why there is no deny list does not trip it, and a field called
    `suspended` does even if somebody documents it as harmless.

    Rejected: a test that greps the source for "deny". The word appears eleven times in
    this package, every one of them in a comment saying the mechanism does not exist, so
    the check would be permanently red or would need an exclusion list, and the exclusion
    list is where the real one would eventually sit.
    """
    findings: list[str] = []
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(value, type) and getattr(value, "__module__", None) == module.__name__:
            if _SUBTRACTIVE_RE.search(name):
                findings.append(f"{module.__name__}.{name} is a type that subtracts")
            for field in _field_names(value):
                if _SUBTRACTIVE_RE.search(field):
                    findings.append(f"{module.__name__}.{name}.{field} subtracts at resolve time")
    return findings


def assert_no_role_in_resolution(resolver: Callable[..., Any]) -> None:
    """Refuse a resolver that can even see a role (M1.3.5).

    The strongest available statement of the rule. A check inside the function body can be
    removed by whoever adds the feature that needs it; a parameter that does not exist has
    to be added first, which is a diff somebody reviews.
    """
    offending = [name for name in inspect.signature(resolver).parameters if "role" in name.lower()]
    if offending:
        msg = (
            f"{resolver.__name__} takes {offending}; entitlement resolution must not be able "
            "to see a role, or 'what can this person see' becomes a graph walk"
        )
        raise IdentityError(msg)


# ------------------------------------------------------------- resolution
def resolve_entitlement(
    principal: Principal,
    *,
    grants: Sequence[SubjectGrant] = (),
    assignments: Sequence[PackAssignment] = (),
    packs: Mapping[str, CapabilityPack] | None = None,
    memberships: Sequence[TeamMembership] = (),
    now: datetime | None = None,
) -> EntitlementSet | NoStandingEntitlement:
    """Everything this principal holds, from grants and packs and nothing else.

    No role grants, by signature. A Super Admin resolves to whatever they were granted,
    which for a fresh install is nothing, and the console showing them an empty data plane
    is the design working rather than a bug (`architecture.html`: "A Super Admin sees none
    of it without a grant").

    A partner resolves to `NoStandingEntitlement` regardless of what rows exist for them,
    which is `standing_entitlement`'s job and is delegated rather than repeated.

    Resolution is a union of rows, and union is safe here precisely because
    `EntitlementSet.scope_for` intersects the scopes of matching grants. Adding a team
    membership can add capabilities; it can never widen the scope of one the person
    already held. That asymmetry is what makes "put them in the team" a safe thing to hand
    a department admin.
    """
    catalogue = dict(packs or {})
    resolved: list[Grant] = []

    for row in grants:
        if row.is_active(now) and subject_reaches(row.subject, principal.id, memberships, now):
            resolved.append(row.as_grant())

    for assignment in assignments:
        if not assignment.is_active(now):
            continue
        if not subject_reaches(assignment.subject, principal.id, memberships, now):
            continue
        pack = catalogue.get(assignment.pack_slug)
        if pack is None:
            # A missing pack is a refusal, never an empty expansion. Silently resolving to
            # nothing would make a broken catalogue look like a correctly narrow person,
            # and the difference matters at exactly the moment somebody is debugging why
            # an answer came back empty.
            msg = (
                f"assignment references pack {assignment.pack_slug!r}, which is not in the "
                "catalogue; resolving it as empty would look like a person with no access"
            )
            raise PackError(msg)
        resolved.extend(row.as_grant() for row in expand(pack, assignment))

    return standing_entitlement(principal, tuple(resolved))


def held_capabilities(entitlement: EntitlementSet) -> frozenset[str]:
    """The capability values in a set. For monotonicity checks and for the console.

    Deliberately a set of strings rather than of `Grant`s: it answers "which capabilities"
    without answering "in what scope", which is the question that must be asked through
    `scope_for` so the intersection happens.
    """
    return frozenset(g.capability.value for g in entitlement.grants)


#: Named so a reader of the invariant suite finds the sentence rather than only the test.
ADDITIVE_ONLY: Final = (
    "Grants are additive. Revocation is deletion of the row, never a negative grant, and "
    "never a flag that subtracts at resolve time."
)
