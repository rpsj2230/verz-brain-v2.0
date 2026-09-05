"""Identity rules that must never break. A failure here blocks deploy.

Five rules, each one a decision that is cheap to make now and impossible to unmake later:

- grants are additive, and revocation is deletion of the row (M1.4.2);
- a role is never the subject of a capability grant (M1.3.5);
- there are six roles, compiled in (M1.3.1);
- deputies are depth one and bounded at thirty days (M1.3.3);
- the Super Admin floor is two (M1.3.4);

plus the two shapes the identity layer inherits from the gate: a partner holds nothing
rather than an empty set (M1.2.4), and a break-glass session is time-boxed and notifies
(M1.2.5).

Several of these are asserted structurally, over the package's own namespace, rather than
by exercising a code path. That is deliberate. A test that calls a function proves the
function behaves; a test that reads the module proves nobody added a second function that
does not.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from pydantic import ValidationError

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.scope import Scope
from brain.identity import directory, packs, roles, teams
from brain.identity.packs import (
    CapabilityPack,
    PackAssignment,
    SubjectGrant,
    assert_no_role_in_resolution,
    held_capabilities,
    resolve_entitlement,
    revoke_capability,
    subtractive_state,
)
from brain.identity.roles import (
    BREAK_GLASS_MAX,
    DEPUTY_MAX,
    ROLE_COUNT,
    SUPER_ADMIN_FLOOR,
    BreakGlassReason,
    IdentityError,
    NoStandingEntitlement,
    Role,
    RoleGrant,
    RoleSubjectError,
    appoint_deputy,
    check_deputy_depth,
    open_break_glass,
    revoke_role,
    role_capability_leaks,
    standing_entitlement,
)
from brain.identity.teams import (
    PrincipalSubject,
    SubjectKind,
    TeamError,
    TeamSubject,
    principal_subject,
    subject_for,
    team_subject,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)

#: Every module in the package. Named once so a new module is added here, or it is not
#: swept by any of the structural checks below.
IDENTITY_MODULES: tuple[ModuleType, ...] = (roles, packs, teams, directory)


def cap(value: str) -> Capability:
    return Capability(value=value)


def person(pid: str = "u_priya", employment: Employment = Employment.STAFF) -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=employment,
        display_name="Test person",
        not_after=datetime(2027, 1, 1, tzinfo=UTC) if employment is Employment.PARTNER else None,
    )


def role_grant(
    role: Role,
    pid: str,
    *,
    scope: Scope | None = None,
    granted_at: datetime = NOW,
    not_after: datetime | None = None,
    deputy_of: str | None = None,
) -> RoleGrant:
    return RoleGrant(
        principal_id=pid,
        role=role,
        scope=scope,
        granted_by="u_founder",
        reason="invariant fixture",
        granted_at=granted_at,
        not_after=not_after,
        deputy_of=deputy_of,
    )


def subject_grant(
    capability: str,
    subject: PrincipalSubject | TeamSubject | None = None,
    scope: Scope | None = None,
) -> SubjectGrant:
    return SubjectGrant(
        subject=subject or principal_subject("u_priya"),
        capability=cap(capability),
        scope=scope or Scope.department("web"),
        granted_by="u_dept_admin",
        reason="invariant fixture",
        granted_at=NOW,
    )


# ----------------------------------------------------- INV: additive only
def test_there_is_no_negative_grant_anywhere_in_the_identity_package() -> None:
    """M1.4.2. The invariant `brain.core.entitlement` is built on, asserted as an absence.

    Delete this and the first person who needs to take one field away from a grant they
    did not write adds a `suspended` flag, and from that moment resolution has an order,
    two rows can disagree, and no grant can be read on its own again. The check is
    structural because the failure arrives under a friendly name, never as `deny_list`.
    """
    findings = [f for module in IDENTITY_MODULES for f in subtractive_state(module)]
    assert findings == [], findings


def test_revocation_deletes_the_row_and_leaves_nothing_that_subtracts() -> None:
    """M1.4.2. The behavioural half of the rule above.

    Without it, `revoke` could return the same rows with a flag set and every structural
    check would still pass, because the flag would be on a field called something
    innocent.
    """
    grants = (subject_grant("read:client.name"), subject_grant("read:client.tier"))
    remaining = revoke_capability(grants, principal_subject("u_priya"), cap("read:client.name"))

    assert len(remaining) == len(grants) - 1
    assert all(g.capability.value == "read:client.tier" for g in remaining)

    resolved = resolve_entitlement(person(), grants=remaining, now=NOW)
    assert isinstance(resolved, EntitlementSet)
    assert not resolved.holds(cap("read:client.name"), NOW)


def test_adding_a_grant_never_takes_a_capability_away() -> None:
    """M1.4.2, as monotonicity. Additive means adding is safe to reason about.

    This is what makes "put them in the team" something a department admin can be handed
    without a review: a new grant can add a capability and can never remove one. If a
    negative row existed, adding a grant could subtract, and this test is what would go
    red.
    """
    base = (subject_grant("read:client.name"),)
    extra = subject_grant("read:ticket.subject", scope=Scope.department("maintenance"))

    before = resolve_entitlement(person(), grants=base, now=NOW)
    after = resolve_entitlement(person(), grants=(*base, extra), now=NOW)
    assert isinstance(before, EntitlementSet)
    assert isinstance(after, EntitlementSet)
    assert held_capabilities(before) <= held_capabilities(after)


def test_a_second_grant_of_one_capability_narrows_rather_than_widens() -> None:
    """The other half of "additive". Adding grants adds capabilities; it must never widen
    the scope of one already held, because `EntitlementSet.scope_for` intersects.

    Delete this and somebody "fixes" the resolver to union scopes, and two narrow grants
    combine into a wide one, which is the failure `Scope` is conjunction-only to prevent.
    """
    narrow = subject_grant("read:client.name", scope=Scope.department("web"))
    wide = subject_grant("read:client.name", scope=Scope.unrestricted())

    resolved = resolve_entitlement(person(), grants=(narrow, wide), now=NOW)
    assert isinstance(resolved, EntitlementSet)
    scope = resolved.scope_for(cap("read:client.name"), NOW)
    assert scope is not None
    assert scope.matches({"department": "web"})
    assert not scope.matches({"department": "sales"})


# ------------------------------------------- INV: a role is never a subject
def test_a_role_may_never_be_the_subject_of_a_capability_grant() -> None:
    """M1.3.5. The constraint, from all three directions at once.

    If a role could hold a capability, "what can this person see" stops being a lookup
    over their grants and becomes a walk over their roles, those roles' grants and the
    scopes on each, and every feature after it is built on the walk.
    """
    # The type has no role variant, so a role subject cannot be constructed at all.
    assert {k.value for k in SubjectKind} == {"principal", "team"}

    # A role arriving as a subject string is refused rather than resolved.
    with pytest.raises(TeamError, match="never a role"):
        subject_for("role:super_admin")

    # A role name arriving where a principal id or a team slug belongs is refused. The
    # refusal is a `RoleSubjectError` and not a `ValidationError`, even from inside a
    # pydantic validator: this is a programming error rather than bad input, and a caller
    # wrapping model construction in `except ValidationError` must not swallow it.
    with pytest.raises(RoleSubjectError, match="platform role"):
        principal_subject("super_admin")
    with pytest.raises(RoleSubjectError, match="platform role"):
        team_subject("web.approver")


def test_no_role_is_mapped_to_anything_capability_shaped() -> None:
    """M1.3.5, structurally. "No role implies a capability, including Super Admin."

    This is the check that catches the convenience mapping somebody adds in a hurry and
    calls `DEFAULTS`. Delete it and the rule survives only as long as everyone remembers
    it, which for a permission model is not long enough.
    """
    findings = [f for module in IDENTITY_MODULES for f in role_capability_leaks(vars(module))]
    assert findings == [], findings


def test_entitlement_resolution_cannot_even_see_a_role() -> None:
    """M1.3.5, as a signature. `resolve_entitlement` takes no role argument.

    A check inside a function body is removed by whoever adds the feature that needs it. A
    parameter that does not exist has to be added first, which is a diff somebody reviews.
    """
    assert_no_role_in_resolution(resolve_entitlement)
    assert "role" not in " ".join(inspect.signature(resolve_entitlement).parameters)


def test_a_super_admin_resolves_to_whatever_they_were_granted_and_no_more() -> None:
    """M1.3.5, behaviourally. The architecture is explicit: a Super Admin sees no document
    body without a grant. Delete this and the first "surely the owner can see everything"
    patch makes the role a capability after all."""
    admin = person("u_sa")
    grants = (role_grant(Role.SUPER_ADMIN, "u_sa"),)
    assert grants[0].role is Role.SUPER_ADMIN

    resolved = resolve_entitlement(admin, grants=(), now=NOW)
    assert isinstance(resolved, EntitlementSet)
    assert held_capabilities(resolved) == frozenset()


# ----------------------------------------------------- INV: six roles, compiled
def test_the_platform_has_exactly_six_roles_and_they_are_compiled_in() -> None:
    """M1.3.1. An editable role table is a role someone edits at 2am.

    Pinned by name rather than by count alone, so that swapping one role for another fails
    here rather than passing quietly because the total is still six.
    """
    assert len(Role) == ROLE_COUNT
    assert {r.value for r in Role} == {
        "super_admin",
        "department_admin",
        "member",
        "auditor",
        "connector_admin",
        "approver",
    }


# ------------------------------------------------ INV: deputies are depth one
def test_a_deputy_cannot_appoint_a_deputy() -> None:
    """M1.3.3. The rule that keeps a thirty-day delegation from becoming permanent.

    Each link in a chain is individually within thirty days, so nothing about any single
    row looks wrong; the appointment simply renews itself for as long as somebody keeps
    passing it on, and nobody re-approves anything. Delete this test and the bound becomes
    decorative.
    """
    standing = role_grant(Role.DEPARTMENT_ADMIN, "u_priya", scope=Scope.department("web"))
    deputy = appoint_deputy(standing, "u_sam", granted_by="u_priya", reason="leave", now=NOW)

    with pytest.raises(IdentityError, match="depth one"):
        appoint_deputy(deputy, "u_alex", granted_by="u_sam", reason="leave", now=NOW)


def test_a_deputy_chain_written_straight_into_the_table_is_caught_by_the_sweep() -> None:
    """M1.3.3. `appoint_deputy` is not the only way a row appears: seed files, migrations
    and console forms call the constructor directly. Without the sweep, the depth rule
    holds only on the one path that goes through the helper."""
    chain = [
        role_grant(Role.SUPER_ADMIN, "u_a"),
        role_grant(Role.SUPER_ADMIN, "u_b", not_after=NOW + timedelta(days=5), deputy_of="u_a"),
        role_grant(Role.SUPER_ADMIN, "u_c", not_after=NOW + timedelta(days=5), deputy_of="u_b"),
    ]
    findings = check_deputy_depth(chain, NOW)
    assert len(findings) == 1
    assert "u_c" in findings[0]


def test_a_deputy_appointment_is_bounded_at_thirty_days() -> None:
    """M1.3.3. The bound exists so that cover for annual leave expires on its own. A
    maximum somebody can raise at the call site is not a maximum."""
    assert timedelta(days=30) == DEPUTY_MAX
    with pytest.raises(ValidationError, match="at most 30 days"):
        role_grant(
            Role.MEMBER,
            "u_sam",
            not_after=NOW + DEPUTY_MAX + timedelta(seconds=1),
            deputy_of="u_priya",
        )


# -------------------------------------------- INV: the Super Admin floor of two
def test_the_last_super_admin_cannot_be_revoked() -> None:
    """M1.3.4. One Super Admin is a single point of lockout: they leave, and nobody left
    in the company can appoint a replacement, because appointing one is a Super Admin
    action. Delete this and the platform can be locked out by one ordinary revocation."""
    grants = [role_grant(Role.SUPER_ADMIN, "u_a"), role_grant(Role.SUPER_ADMIN, "u_b")]
    with pytest.raises(IdentityError, match="floor is 2"):
        revoke_role(grants, grants[0], now=NOW)

    assert SUPER_ADMIN_FLOOR == 2


def test_a_deputy_cannot_be_used_to_satisfy_the_super_admin_floor() -> None:
    """M1.3.3 and M1.3.4 together, which is where the interesting failure lives: revoke
    one of two Super Admins while a deputy covers them, and the floor looks satisfied
    until the deputy expires on a date nobody has in their calendar."""
    grants = [
        role_grant(Role.SUPER_ADMIN, "u_a"),
        role_grant(Role.SUPER_ADMIN, "u_b"),
        role_grant(Role.SUPER_ADMIN, "u_c", not_after=NOW + timedelta(days=10), deputy_of="u_a"),
    ]
    with pytest.raises(IdentityError, match="floor is 2"):
        revoke_role(grants, grants[1], now=NOW)


# ------------------------------------------------- INV: a partner holds nothing
def test_a_partner_holds_nothing_which_is_not_an_empty_entitlement_set() -> None:
    """M1.2.4. The distinction `brain.gate.ingress.Unrecognised` exists to make.

    An empty `EntitlementSet` is a thing that can be intersected with an agent ceiling,
    hashed into a cache key and passed down a delegation chain. `NoStandingEntitlement`
    cannot be any of those, so the day somebody adds a default grant to the resolver, a
    partner does not silently acquire it along with everyone else.
    """
    result = standing_entitlement(person("u_partner", Employment.PARTNER))

    assert isinstance(result, NoStandingEntitlement)
    assert not isinstance(result, EntitlementSet)
    for attribute in ("intersect", "ent_hash", "holds", "grants"):
        assert not hasattr(result, attribute), attribute


# ------------------------------------------- INV: break-glass is bounded and loud
def test_a_break_glass_session_cannot_be_opened_without_a_notification() -> None:
    """M1.2.5. A break-glass session nobody is told about is an unaudited admin account.

    The pair return is the enforcement: there is no code path that produces a session
    without also producing the notice. Delete this and the notice becomes an optional
    second call that whoever is handling an incident at 2am will not make.
    """
    session, notice = open_break_glass(
        session_id="bg_inv",
        principal=person("u_partner", Employment.PARTNER),
        reason=BreakGlassReason.INCIDENT_RESPONSE,
        grants=(Grant(capability=cap("admin:connector"), scope=Scope.department("web")),),
        authorised_by="u_sa_a",
        notify=["u_sa_a", "u_auditor"],
        now=NOW,
    )
    assert notice.recipients == session.notified
    assert notice.recipients != ()

    signature = inspect.signature(open_break_glass)
    assert "tuple" in str(signature.return_annotation)


def test_a_break_glass_entitlement_is_unreachable_once_the_session_expires() -> None:
    """M1.2.5. Time-boxed has to mean the entitlement stops, not that a flag flips.

    The bound rides on the `EntitlementSet`, so `scope_for` refuses after expiry and the
    `ent_hash` differs either side of it, which means an answer cached inside the window
    cannot be served outside it. Delete this and "time-boxed" is a column nobody reads.
    """
    session, _ = open_break_glass(
        session_id="bg_inv2",
        principal=person("u_partner", Employment.PARTNER),
        reason=BreakGlassReason.INSTALL,
        grants=(Grant(capability=cap("admin:connector"), scope=Scope.department("web")),),
        authorised_by="u_sa_a",
        notify=["u_sa_a"],
        now=NOW,
    )
    entitlement = session.to_entitlement()
    after = NOW + BREAK_GLASS_MAX

    assert entitlement.holds(cap("admin:connector"), NOW)
    assert not entitlement.holds(cap("admin:connector"), after)
    assert entitlement.scope_for(cap("admin:connector"), after) is None
    assert not session.is_open(after)


# ------------------------------------------- INV: a pack cannot escape its scope
def test_a_pack_assignment_is_always_bound_to_a_scope() -> None:
    """M1.4.3. A pack is the largest thing anyone assigns in one action, and it is the row
    least likely to be read carefully because the interesting part is the pack name. An
    unbounded assignment is the widest row in the system wearing an ordinary shape."""
    pack = CapabilityPack(
        slug="engineer", label="Engineer", capabilities=(cap("read:ticket.subject"),)
    )
    with pytest.raises(ValidationError, match="restricts nothing"):
        PackAssignment(
            subject=principal_subject("u_priya"),
            pack_slug=pack.slug,
            scope=Scope.unrestricted(),
            granted_by="u_dept_admin",
            reason="invariant fixture",
            granted_at=NOW,
        )
