"""The identity policy layer, mechanically: roles, deputies, packs, teams, break-glass.

The rules that must never break live in `tests/invariants/test_identity_invariants.py`.
What is here is the machinery: which grants are refused and why, what a deputy
appointment copies, what a pack expands to, what a revocation leaves behind, and what a
break-glass session hands to the audit ledger.

All three identity modules are exercised from this one file rather than from three,
because they are one change: a pack assignment names a subject, a subject may be a team,
and a team is refused if it is really a role. Splitting the tests would mean reading three
files to find out where a behaviour is pinned.

Task ids: M1.2.4, M1.2.5, M1.3.1, M1.3.2, M1.3.3, M1.3.4, M1.3.5, M1.4.2, M1.4.3,
M1.5.1, M1.5.2, M1.5.3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from brain.audit.ledger import AuditAction
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.scope import Clause, Op, Scope
from brain.identity.packs import (
    CapabilityPack,
    PackAssignment,
    PackError,
    SubjectGrant,
    expand,
    held_capabilities,
    resolve_entitlement,
    revoke,
    revoke_assignment,
    revoke_capability,
)
from brain.identity.roles import (
    BREAK_GLASS_CHAIN,
    BREAK_GLASS_MAX,
    DEPUTY_MAX,
    ROLE_COUNT,
    ROLE_SPECS,
    SCOPE_REQUIRED,
    SUPER_ADMIN_FLOOR,
    BreakGlassReason,
    BreakGlassSession,
    IdentityError,
    NoStandingEntitlement,
    Role,
    RoleGrant,
    RoleSpec,
    appoint_deputy,
    check_deputy_depth,
    open_break_glass,
    reach_during,
    revoke_role,
    spec_for,
    standing_entitlement,
    standing_super_admins,
)
from brain.identity.teams import (
    PrincipalSubject,
    Team,
    TeamError,
    TeamMembership,
    TeamSubject,
    assert_within_department,
    members_of,
    principal_subject,
    references_team,
    subject_for,
    subject_reaches,
    subjects_for,
    team_scope,
    team_subject,
    teams_of,
)

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def cap(value: str) -> Capability:
    return Capability(value=value)


def staff(pid: str = "u_priya") -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=Employment.STAFF,
        display_name="Priya",
        primary_department="web",
    )


def partner(pid: str = "u_partner") -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=Employment.PARTNER,
        display_name="Installing partner",
        not_after=datetime(2027, 1, 1, tzinfo=UTC),
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
        reason="test fixture",
        granted_at=granted_at,
        not_after=not_after,
        deputy_of=deputy_of,
    )


# ------------------------------------------------------------- the six roles
def test_there_are_exactly_six_roles_and_every_one_carries_a_spec() -> None:
    """M1.3.1. Without this, a seventh role is added and nothing anywhere notices: the
    spec table, the console and the scope rule all read from `Role`, and a member with no
    spec would raise a KeyError in whichever of them ran first in production."""
    assert len(Role) == ROLE_COUNT == 6
    assert set(ROLE_SPECS) == set(Role)
    assert {s.role for s in ROLE_SPECS.values()} == set(Role)


def test_the_role_table_cannot_be_written_to_at_runtime() -> None:
    """Not editable rows has to mean not editable at runtime as well. Delete this and a
    module-level dict is one import away from being a role table with worse durability
    than the one we refused to build."""
    with pytest.raises(TypeError):
        cast("dict[Role, RoleSpec]", ROLE_SPECS)[Role.MEMBER] = spec_for(Role.AUDITOR)


def test_only_the_department_admin_and_the_approver_are_scoped_roles() -> None:
    """M1.3.2. The scope rule is read from `SCOPE_REQUIRED` by the validator and by the
    console. If this set drifts, a Super Admin grant starts accepting a scope that nothing
    consults, and whoever wrote it believes they narrowed the platform."""
    assert {Role.DEPARTMENT_ADMIN, Role.APPROVER} == SCOPE_REQUIRED
    assert {r for r in Role if spec_for(r).scope_required} == SCOPE_REQUIRED


# ------------------------------------------------------------- role grants
def test_a_department_admin_grant_without_a_scope_is_refused() -> None:
    """M1.3.2. This is the failure `DepartmentAdmin` already refuses, one table earlier:
    an admin with no scope grants company-wide, approves company-wide and sees a console
    filtered to nothing, and not one of those fails loudly."""
    with pytest.raises(ValidationError, match="needs a scope"):
        role_grant(Role.DEPARTMENT_ADMIN, "u_priya")


def test_an_approver_grant_without_a_scope_is_refused() -> None:
    """M1.3.2. An unscoped approver approves anything anyone puts in front of them, which
    makes the Assisted rung decorative."""
    with pytest.raises(ValidationError, match="needs a scope"):
        role_grant(Role.APPROVER, "u_priya")


def test_a_department_admin_scope_that_restricts_nothing_is_refused() -> None:
    """Null and unrestricted are the same mistake here. Without this, the rule above is
    satisfied by `Scope.unrestricted()` and the check becomes a formality."""
    with pytest.raises(ValidationError, match="restricts nothing"):
        role_grant(Role.DEPARTMENT_ADMIN, "u_priya", scope=Scope.unrestricted())


def test_a_department_admin_scope_that_can_never_match_is_refused() -> None:
    """A saved scope matching no row is dead configuration that reads exactly like a
    permission bug from the far end of a query."""
    impossible = Scope.department("web").intersect(Scope.department("sales"))
    with pytest.raises(ValidationError, match="never match"):
        role_grant(Role.DEPARTMENT_ADMIN, "u_priya", scope=impossible)


def test_an_unscoped_role_refuses_a_scope_rather_than_storing_one_nobody_reads() -> None:
    """M1.3.2. Delete this and a Super Admin grant accepts `scope=web`, stores it, and
    consults it nowhere. The person who wrote it thinks the platform is bounded."""
    with pytest.raises(ValidationError, match="carries no scope"):
        role_grant(Role.SUPER_ADMIN, "u_priya", scope=Scope.department("web"))


def test_a_role_grant_that_expires_before_it_starts_is_refused() -> None:
    """An inverted window is a grant that is simultaneously active and expired depending
    on which comparison a caller reaches for."""
    with pytest.raises(ValidationError, match="after granted_at"):
        role_grant(Role.MEMBER, "u_priya", not_after=NOW - timedelta(days=1))


def test_a_naive_timestamp_on_a_role_grant_is_refused() -> None:
    """Follows `Principal.not_after`. A naive expiry compares against whatever the server
    timezone happens to be, which is a silent eight-hour window in Singapore."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        role_grant(Role.MEMBER, "u_priya", granted_at=datetime(2026, 9, 5, 9, 0))


# ---------------------------------------------------------------- deputies
def test_a_deputy_grant_must_carry_an_expiry() -> None:
    """M1.3.3. An unbounded deputy is an appointment nobody made and nobody reviews: the
    whole point of the flag is that it ends."""
    with pytest.raises(ValidationError, match="must carry not_after"):
        role_grant(Role.MEMBER, "u_sam", deputy_of="u_priya")


def test_a_deputy_grant_longer_than_thirty_days_is_refused() -> None:
    """M1.3.3. Thirty days is the bound; without the check the field is documentation."""
    with pytest.raises(ValidationError, match="at most 30 days"):
        role_grant(
            Role.MEMBER,
            "u_sam",
            not_after=NOW + DEPUTY_MAX + timedelta(days=1),
            deputy_of="u_priya",
        )


def test_a_principal_cannot_deputise_for_themselves() -> None:
    """A self-deputy is a way to extend your own grant by thirty days at a time without
    anybody appointing you."""
    with pytest.raises(ValidationError, match="cannot deputise for themselves"):
        role_grant(Role.MEMBER, "u_sam", not_after=NOW + timedelta(days=5), deputy_of="u_sam")


def test_appointing_a_deputy_copies_the_scope_of_the_standing_grant() -> None:
    """A deputy covering a Department Admin must be bounded to the same department. If the
    scope were dropped, the cover would be wider than the job."""
    standing = role_grant(Role.DEPARTMENT_ADMIN, "u_priya", scope=Scope.department("web"))
    deputy = appoint_deputy(standing, "u_sam", granted_by="u_priya", reason="annual leave", now=NOW)

    assert deputy.scope == standing.scope
    assert deputy.is_deputy
    assert deputy.deputy_of == "u_priya"
    assert deputy.not_after == NOW + DEPUTY_MAX


def test_a_deputy_never_outlives_the_grant_it_covers() -> None:
    """A contractor's own Department Admin grant ends in a week. A thirty-day deputy would
    outlive it and keep running a department for somebody who has left."""
    standing = role_grant(
        Role.DEPARTMENT_ADMIN,
        "u_priya",
        scope=Scope.department("web"),
        not_after=NOW + timedelta(days=7),
    )
    deputy = appoint_deputy(standing, "u_sam", granted_by="u_priya", reason="leave", now=NOW)
    assert deputy.not_after == NOW + timedelta(days=7)


def test_appointing_a_deputy_against_an_expired_grant_is_refused() -> None:
    """Otherwise a lapsed admin can appoint a live one, which is a way back in for
    somebody whose own access has already ended."""
    standing = role_grant(
        Role.DEPARTMENT_ADMIN,
        "u_priya",
        scope=Scope.department("web"),
        granted_at=NOW - timedelta(days=30),
        not_after=NOW - timedelta(days=1),
    )
    with pytest.raises(IdentityError, match="does not currently hold"):
        appoint_deputy(standing, "u_sam", granted_by="u_priya", reason="leave", now=NOW)


def test_a_deputy_appointment_longer_than_the_maximum_is_refused_at_the_call() -> None:
    """The constructor already refuses it. This refuses it one step earlier, where the
    number a human typed still exists and can be reported back to them."""
    standing = role_grant(Role.SUPER_ADMIN, "u_priya")
    with pytest.raises(IdentityError, match="runs 1 to 30 days"):
        appoint_deputy(standing, "u_sam", granted_by="u_priya", reason="leave", now=NOW, days=45)


def test_an_expired_deputy_grant_no_longer_confers_the_role() -> None:
    """`is_active` is what makes the thirty-day bound mean anything after the fact. If it
    only checked `not_after is None`, every expired deputy would still resolve."""
    deputy = role_grant(
        Role.MEMBER, "u_sam", not_after=NOW + timedelta(days=1), deputy_of="u_priya"
    )
    assert deputy.is_active(NOW)
    assert not deputy.is_active(NOW + timedelta(days=2))


# ------------------------------------------------------- the super admin floor
def test_a_deputy_super_admin_does_not_count_towards_the_floor() -> None:
    """M1.3.4. Cover for annual leave is not ownership. If a deputy counted, the floor
    would empty itself on a date nobody has in their calendar."""
    grants = [
        role_grant(Role.SUPER_ADMIN, "u_a"),
        role_grant(Role.SUPER_ADMIN, "u_b"),
        role_grant(Role.SUPER_ADMIN, "u_c", not_after=NOW + timedelta(days=10), deputy_of="u_a"),
    ]
    assert {g.principal_id for g in standing_super_admins(grants, NOW)} == {"u_a", "u_b"}


def test_revoking_down_to_the_floor_is_allowed() -> None:
    """The floor is two, not three. A rule that refused any revocation would be switched
    off within a week, and then it would protect nothing."""
    grants = [role_grant(Role.SUPER_ADMIN, f"u_{n}") for n in "abc"]
    remaining = revoke_role(grants, grants[0], now=NOW)
    assert len(standing_super_admins(remaining, NOW)) == SUPER_ADMIN_FLOOR


def test_an_already_broken_floor_does_not_block_an_unrelated_revocation() -> None:
    """Somebody left and the row was deleted in the database. If the floor then blocked
    every unrelated revocation, the first thing an operator would do is switch it off."""
    grants = [role_grant(Role.SUPER_ADMIN, "u_a"), role_grant(Role.AUDITOR, "u_b")]
    remaining = revoke_role(grants, grants[1], now=NOW)
    assert len(remaining) == 1


def test_revoking_a_grant_that_is_not_in_the_set_is_refused() -> None:
    """Revocation removes a row that exists. Silently succeeding on a row that does not
    would report "access removed" for access that is still there."""
    grants = [role_grant(Role.SUPER_ADMIN, "u_a"), role_grant(Role.SUPER_ADMIN, "u_b")]
    with pytest.raises(IdentityError, match="not in this set"):
        revoke_role(grants, role_grant(Role.AUDITOR, "u_z"), now=NOW)


def test_revocation_of_a_role_deletes_the_row_rather_than_marking_it() -> None:
    """M1.4.2 applies to role grants as much as capability grants. A "revoked" flag is a
    negative row, and a negative row gives resolution an order."""
    grants = [role_grant(Role.SUPER_ADMIN, f"u_{n}") for n in "abc"]
    remaining = revoke_role(grants, grants[2], now=NOW)
    assert len(remaining) == len(grants) - 1
    assert grants[2] not in remaining


# ------------------------------------------------------------------- partner
def test_a_partner_holds_no_standing_entitlement() -> None:
    """M1.2.4. Delete this and a partner resolves like anyone else, which means whatever
    the install left in the grant table is live for them permanently."""
    result = standing_entitlement(partner())
    assert isinstance(result, NoStandingEntitlement)
    assert result.principal_id == "u_partner"


def test_a_partner_holds_nothing_even_when_rows_exist_for_them() -> None:
    """Rows for a partner can exist: a migration wrote them, they were staff once, someone
    made a mistake. Holding nothing regardless is the safe reading of that state."""
    grant = Grant(capability=cap("read:client.name"), scope=Scope.department("web"))
    assert isinstance(standing_entitlement(partner(), (grant,)), NoStandingEntitlement)


def test_a_member_of_staff_resolves_to_an_ordinary_entitlement_set() -> None:
    """The partner rule must not swallow everybody. Without this the check above would
    pass just as well if `standing_entitlement` always returned nothing."""
    grant = Grant(capability=cap("read:client.name"), scope=Scope.department("web"))
    result = standing_entitlement(staff(), (grant,))
    assert isinstance(result, EntitlementSet)
    assert result.holds(cap("read:client.name"))


# --------------------------------------------------------------- break-glass
def break_glass_grants() -> tuple[Grant, ...]:
    return (Grant(capability=cap("admin:connector"), scope=Scope.department("web")),)


def test_opening_break_glass_returns_the_notice_alongside_the_session() -> None:
    """M1.2.5. The pair return is the enforcement. If this ever becomes a single return,
    notification moves to the caller, and the caller that forgets is the one handling an
    incident at two in the morning."""
    session, notice = open_break_glass(
        session_id="bg_001",
        principal=partner(),
        reason=BreakGlassReason.INSTALL,
        grants=break_glass_grants(),
        authorised_by="u_sa_a",
        notify=["u_sa_a", "u_auditor"],
        now=NOW,
    )
    assert notice.session_id == session.session_id
    assert notice.recipients == ("u_sa_a", "u_auditor")
    assert "bg_001" in notice.summary


def test_a_break_glass_session_that_notifies_nobody_is_refused() -> None:
    """M1.2.5. A session nobody is told about is an unaudited admin account with an
    expiry date."""
    with pytest.raises(ValidationError, match="notify at least one"):
        open_break_glass(
            session_id="bg_002",
            principal=partner(),
            reason=BreakGlassReason.INSTALL,
            grants=break_glass_grants(),
            authorised_by="u_sa_a",
            notify=[],
            now=NOW,
        )


def test_a_self_authorised_break_glass_session_is_refused() -> None:
    """An elevation you grant yourself is an admin account with a longer name. The
    company has to be the one that decided."""
    with pytest.raises(ValidationError, match="authorised by somebody other"):
        open_break_glass(
            session_id="bg_003",
            principal=partner(),
            reason=BreakGlassReason.INSTALL,
            grants=break_glass_grants(),
            authorised_by="u_partner",
            notify=["u_sa_a"],
            now=NOW,
        )


def test_a_break_glass_session_longer_than_the_maximum_is_refused() -> None:
    """M1.2.5. "Time-boxed" with no number is a phrase, not a bound."""
    with pytest.raises(IdentityError, match="at most"):
        open_break_glass(
            session_id="bg_004",
            principal=partner(),
            reason=BreakGlassReason.INCIDENT_RESPONSE,
            grants=break_glass_grants(),
            authorised_by="u_sa_a",
            notify=["u_sa_a"],
            now=NOW,
            duration=BREAK_GLASS_MAX + timedelta(minutes=1),
        )


def test_a_break_glass_session_with_no_grants_is_refused() -> None:
    """It would be audited and notified as an elevation while conferring nothing, which
    trains everyone who reads the notices to ignore them."""
    with pytest.raises(ValidationError, match="no grants"):
        BreakGlassSession(
            session_id="bg_005",
            principal_id="u_partner",
            reason=BreakGlassReason.INSTALL,
            opened_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            grants=(),
            authorised_by="u_sa_a",
            notified=("u_sa_a",),
        )


def test_a_session_id_that_cannot_be_written_to_the_ledger_is_refused() -> None:
    """The audit subject is `session:<id>` under the ledger's own grammar. Refusing here
    means the session never opens; refusing later means it opened and was not recorded."""
    with pytest.raises(IdentityError, match="cannot be written to the ledger"):
        open_break_glass(
            session_id="bg 006!",
            principal=partner(),
            reason=BreakGlassReason.INSTALL,
            grants=break_glass_grants(),
            authorised_by="u_sa_a",
            notify=["u_sa_a"],
            now=NOW,
        )


def test_a_break_glass_entitlement_carries_the_session_expiry() -> None:
    """M1.2.5. The bound lives on the `EntitlementSet` so expiry is enforced by the same
    machinery that enforces a contractor's, and so the `ent_hash` differs either side of
    it. A bound checked by the caller is a bound one caller can skip."""
    session, _ = open_break_glass(
        session_id="bg_007",
        principal=partner(),
        reason=BreakGlassReason.INSTALL,
        grants=break_glass_grants(),
        authorised_by="u_sa_a",
        notify=["u_sa_a"],
        now=NOW,
    )
    entitlement = session.to_entitlement()
    assert entitlement.not_after == NOW + BREAK_GLASS_MAX
    assert entitlement.holds(cap("admin:connector"), NOW)
    assert not entitlement.holds(cap("admin:connector"), NOW + BREAK_GLASS_MAX)


def test_a_break_glass_session_records_itself_on_its_own_chain() -> None:
    """M1.2.5. "Separately audited" is the chain name. Mixed into the main ledger, a
    handful of break-glass rows are found only by somebody who already knew to look."""
    session, _ = open_break_glass(
        session_id="bg_008",
        principal=partner(),
        reason=BreakGlassReason.INCIDENT_RESPONSE,
        grants=break_glass_grants(),
        authorised_by="u_sa_a",
        notify=["u_sa_a", "u_auditor"],
        now=NOW,
    )
    assert session.audit_action is AuditAction.BREAK_GLASS
    assert session.audit_subject == "session:bg_008"
    assert session.audit_chain == BREAK_GLASS_CHAIN

    details = session.audit_details()
    assert details["reason"] == "incident_response"
    assert details["notified"] == "u_auditor,u_sa_a"
    assert details["chain"] == BREAK_GLASS_CHAIN


def test_a_partner_reaches_only_what_an_open_session_grants() -> None:
    """M1.2.4 and M1.2.5 together. Standing grants are never unioned with a session's,
    because a union outlives the session in every cache it touched."""
    who = partner()
    session, _ = open_break_glass(
        session_id="bg_009",
        principal=who,
        reason=BreakGlassReason.INSTALL,
        grants=break_glass_grants(),
        authorised_by="u_sa_a",
        notify=["u_sa_a"],
        now=NOW,
    )
    during = reach_during(who, session, now=NOW)
    assert isinstance(during, EntitlementSet)
    assert during.holds(cap("admin:connector"), NOW)

    after = reach_during(who, session, now=NOW + BREAK_GLASS_MAX)
    assert isinstance(after, NoStandingEntitlement)
    assert isinstance(reach_during(who, None), NoStandingEntitlement)


def test_a_session_belonging_to_someone_else_is_refused() -> None:
    """Otherwise one open session elevates whoever passes it in, which is worse than no
    session at all because it is audited under the wrong name."""
    session, _ = open_break_glass(
        session_id="bg_010",
        principal=partner(),
        reason=BreakGlassReason.INSTALL,
        grants=break_glass_grants(),
        authorised_by="u_sa_a",
        notify=["u_sa_a"],
        now=NOW,
    )
    with pytest.raises(IdentityError, match="different principal"):
        reach_during(staff(), session, now=NOW)


# ------------------------------------------------------------------- packs
def maintenance_pack() -> CapabilityPack:
    return CapabilityPack(
        slug="maintenance_engineer",
        label="Maintenance engineer",
        capabilities=(cap("read:ticket.subject"), cap("write:ticket.status")),
    )


def assignment(
    *,
    subject: PrincipalSubject | TeamSubject | None = None,
    scope: Scope | None = None,
    not_after: datetime | None = None,
) -> PackAssignment:
    return PackAssignment(
        subject=subject or principal_subject("u_priya"),
        pack_slug="maintenance_engineer",
        scope=scope or Scope.department("maintenance"),
        granted_by="u_dept_admin",
        reason="joined maintenance",
        granted_at=NOW,
        not_after=not_after,
    )


def test_a_pack_expands_to_one_grant_per_capability_bound_to_the_assignment_scope() -> None:
    """M1.4.3. `expand` is the only route from a pack to a grant. A resolver that read
    packs directly would be a second implementation of what a pack means."""
    grants = expand(maintenance_pack(), assignment())
    assert {g.capability.value for g in grants} == {"read:ticket.subject", "write:ticket.status"}
    assert all(g.scope == Scope.department("maintenance") for g in grants)
    assert all(g.from_pack == "maintenance_engineer" for g in grants)


def test_an_assignment_of_a_pack_that_restricts_nothing_is_refused() -> None:
    """M1.4.3. "Scope-bound" means bound. A pack is the largest thing anyone assigns in
    one action, so an unbounded one is the widest row in the system."""
    with pytest.raises(ValidationError, match="restricts nothing"):
        assignment(scope=Scope.unrestricted())


def test_expanding_a_pack_the_assignment_does_not_name_is_refused() -> None:
    """A mismatched pair silently grants a different bundle than the assignment records,
    and the audit row would name the assignment."""
    other = CapabilityPack(slug="finance", label="Finance", capabilities=(cap("read:invoice.id"),))
    with pytest.raises(PackError, match="not 'finance'"):
        expand(other, assignment())


def test_a_pack_normalises_its_capabilities_so_two_spellings_hash_alike() -> None:
    """Two orderings of one bundle must serialise identically, or the same person hashes
    to two entitlements and misses their own cache entry."""
    one = CapabilityPack(
        slug="pk",
        label="Pack",
        capabilities=(cap("write:ticket.status"), cap("read:ticket.subject")),
    )
    two = CapabilityPack(
        slug="pk",
        label="Pack",
        capabilities=(
            cap("read:ticket.subject"),
            cap("write:ticket.status"),
            cap("read:ticket.subject"),
        ),
    )
    assert one.capabilities == two.capabilities


def test_an_empty_pack_is_refused() -> None:
    """An empty pack looks like access in the console and confers none, which is the most
    expensive kind of wrong answer to debug."""
    with pytest.raises(ValidationError, match="no capabilities"):
        CapabilityPack(slug="hollow", label="Hollow", capabilities=())


def test_a_missing_pack_is_a_refusal_rather_than_an_empty_expansion() -> None:
    """A broken catalogue must not look like a correctly narrow person. That difference
    matters at exactly the moment somebody is debugging why an answer came back empty."""
    with pytest.raises(PackError, match="not in the catalogue"):
        resolve_entitlement(staff(), assignments=[assignment()], packs={}, now=NOW)


def test_an_expired_assignment_confers_nothing() -> None:
    """The end date on a pack assignment is the offboarding path for a project pod. If it
    were not checked, the pod would outlive the project."""
    expired = assignment(not_after=NOW + timedelta(days=1))
    result = resolve_entitlement(
        staff(),
        assignments=[expired],
        packs={"maintenance_engineer": maintenance_pack()},
        now=NOW + timedelta(days=2),
    )
    assert isinstance(result, EntitlementSet)
    assert result.grants == ()


# -------------------------------------------------------------- revocation
def subject_grant(
    capability: str,
    *,
    subject: PrincipalSubject | TeamSubject | None = None,
    scope: Scope | None = None,
    from_pack: str | None = None,
) -> SubjectGrant:
    return SubjectGrant(
        subject=subject or principal_subject("u_priya"),
        capability=cap(capability),
        scope=scope or Scope.department("web"),
        granted_by="u_dept_admin",
        reason="test fixture",
        granted_at=NOW,
        from_pack=from_pack,
    )


def test_revocation_removes_the_row_and_leaves_nothing_behind() -> None:
    """M1.4.2. The whole rule in one assertion: after a revocation there is no row about
    the revoked capability at all, negative or otherwise."""
    grants = [subject_grant("read:client.name"), subject_grant("read:client.tier")]
    remaining = revoke_capability(grants, principal_subject("u_priya"), cap("read:client.name"))

    assert len(remaining) == 1
    assert all(g.capability.value != "read:client.name" for g in remaining)


def test_revoking_a_narrow_capability_leaves_a_wider_grant_alone() -> None:
    """Exact match, not `covers`. Deleting `read:client.*` because somebody asked about
    `read:client.name` takes away more than was asked for, from a decision somebody else
    made."""
    grants = [subject_grant("read:client.*"), subject_grant("read:client.name")]
    remaining = revoke_capability(grants, principal_subject("u_priya"), cap("read:client.name"))

    assert [g.capability.value for g in remaining] == ["read:client.*"]


def test_revoking_an_assignment_removes_the_grants_it_produced() -> None:
    """A grant whose `from_pack` points at nothing is access nobody can account for. The
    pair return is what stops the second half being forgotten."""
    assignments = [assignment()]
    grants = [*expand(maintenance_pack(), assignments[0]), subject_grant("read:client.name")]

    remaining_assignments, remaining_grants = revoke_assignment(
        assignments, grants, principal_subject("u_priya"), "maintenance_engineer"
    )
    assert remaining_assignments == ()
    assert [g.capability.value for g in remaining_grants] == ["read:client.name"]


def test_revoke_does_not_mutate_the_set_it_was_given() -> None:
    """The audit row for a revocation is built by comparing before with after. If the
    input were mutated there would be no before."""
    grants = [subject_grant("read:client.name")]
    revoke(grants, where=lambda g: True)
    assert len(grants) == 1


# ---------------------------------------------------------------- teams
def design_team() -> Team:
    return Team(company_id="verz", department_slug="web", slug="design", name="Web design")


def membership(pid: str, *, not_after: datetime | None = None) -> TeamMembership:
    return TeamMembership(principal_id=pid, team_path="web.design", since=NOW, not_after=not_after)


def test_a_team_predicate_names_its_department_as_well_as_itself() -> None:
    """M1.5.1. The team clause alone would admit a row in another department that happens
    to carry a team of the same name. Same collision `starter_scopes` guards against."""
    scope = design_team().scope()
    assert scope.matches({"department": "web", "team": "design"})
    assert not scope.matches({"department": "sales", "team": "design"})
    assert references_team(scope)


def test_a_team_subtree_scope_is_a_prefix_over_the_scope_path() -> None:
    """M1.5.1. A hierarchy under a department is a prefix over `scope_path`, which narrows
    without making the entitlement lookup recursive."""
    scope = design_team().subtree_scope()
    assert scope.matches({"department": "web", "scope_path": "web.design.brand"})
    assert not scope.matches({"department": "web", "scope_path": "web.build.brand"})


def test_a_team_may_not_share_its_department_s_name() -> None:
    """One name, two things: "grant Priya web" would have two meanings, and the safe
    reading is not the one a resolver picks by declaration order."""
    with pytest.raises(ValidationError, match="same name as its department"):
        Team(company_id="verz", department_slug="web", slug="web", name="Web")


def test_a_membership_that_ends_before_it_starts_is_refused() -> None:
    """An inverted window makes `is_active` answer differently depending on which end a
    caller compares first."""
    with pytest.raises(ValidationError, match="after since"):
        TeamMembership(
            principal_id="u_priya", team_path="web.design", since=NOW, not_after=NOW - timedelta(1)
        )


def test_an_expired_membership_reaches_nothing() -> None:
    """M1.5.2. The membership window is checked at resolve time rather than by whoever
    loaded the rows, because a filter in a query is a filter one caller can forget."""
    ended = membership("u_priya", not_after=NOW + timedelta(days=1))
    assert teams_of("u_priya", [ended], NOW) == ("web.design",)
    assert teams_of("u_priya", [ended], NOW + timedelta(days=2)) == ()
    assert members_of("web.design", [ended], NOW + timedelta(days=2)) == ()


def test_a_team_grant_reaches_every_current_member() -> None:
    """M1.5.3. This is the point of a team subject: one row, and membership is the only
    thing that changes when somebody joins."""
    grant = subject_grant("read:ticket.subject", subject=team_subject("web.design"))
    memberships = [membership("u_priya"), membership("u_sam")]

    for pid in ("u_priya", "u_sam"):
        assert subject_reaches(grant.subject, pid, memberships, NOW)
    assert not subject_reaches(grant.subject, "u_outsider", memberships, NOW)


def test_a_grant_that_arrived_through_a_team_is_indistinguishable_downstream() -> None:
    """M1.5.3. `as_grant` drops the subject. If it did not, the answer somebody got would
    depend on how their access happened to be written rather than on what it amounts to."""
    direct = subject_grant("read:ticket.subject", subject=principal_subject("u_priya"))
    via_team = subject_grant("read:ticket.subject", subject=team_subject("web.design"))
    assert direct.as_grant() == via_team.as_grant()


def test_a_team_grant_resolves_for_a_member_and_not_for_anyone_else() -> None:
    """The end-to-end shape of M1.5.3. Without it, `subject_reaches` could be correct and
    the resolver could still be ignoring it."""
    grants = [subject_grant("read:ticket.subject", subject=team_subject("web.design"))]
    memberships = [membership("u_priya")]

    inside = resolve_entitlement(staff("u_priya"), grants=grants, memberships=memberships, now=NOW)
    outside = resolve_entitlement(staff("u_sam"), grants=grants, memberships=memberships, now=NOW)

    assert isinstance(inside, EntitlementSet)
    assert isinstance(outside, EntitlementSet)
    assert inside.holds(cap("read:ticket.subject"), NOW)
    assert not outside.holds(cap("read:ticket.subject"), NOW)


def test_subject_keys_are_sorted_so_one_person_hashes_one_way() -> None:
    """Whatever caches an entitlement compares these. Two orderings of one membership list
    must not look like two different principals in traces."""
    memberships = [
        TeamMembership(principal_id="u_priya", team_path="web.design", since=NOW),
        TeamMembership(principal_id="u_priya", team_path="web.build", since=NOW),
    ]
    assert subjects_for("u_priya", memberships, NOW) == (
        "principal:u_priya",
        "team:web.build",
        "team:web.design",
    )


def test_a_subject_is_parsed_rather_than_guessed() -> None:
    """A guess that gets it wrong turns a person into a team, and a team grant reaches
    everyone in it."""
    assert subject_for("principal:u_priya") == principal_subject("u_priya")
    assert subject_for("team:web.design") == team_subject("web.design")
    with pytest.raises(TeamError, match="not a subject"):
        subject_for("u_priya")


def test_a_team_grant_bounded_to_another_department_is_refused() -> None:
    """The row reads as though it were bounded when it is bounded to somewhere else, so
    it either reaches nothing or reaches the wrong rows."""
    scope = Scope(clauses=(Clause(field="department", op=Op.EQ, value="sales"),))
    with pytest.raises(TeamError, match="the grant would reach nothing"):
        assert_within_department(team_subject("web.design"), scope, ["web", "sales"])


def test_a_team_scope_helper_and_the_team_type_agree() -> None:
    """Two spellings of one predicate hash differently, so the same grant would look like
    two. This is the check that keeps the constructor and the helper together."""
    assert team_scope("web.design") == design_team().scope()


# ------------------------------------------------------------- resolution
def test_resolution_combines_direct_grants_packs_and_team_grants() -> None:
    """The capstone. Each half is tested above; this is what proves they are actually
    wired into one answer rather than three functions nobody calls together."""
    result = resolve_entitlement(
        staff("u_priya"),
        grants=[
            subject_grant("read:client.name"),
            subject_grant("read:ticket.subject", subject=team_subject("web.design")),
        ],
        assignments=[assignment()],
        packs={"maintenance_engineer": maintenance_pack()},
        memberships=[membership("u_priya")],
        now=NOW,
    )
    assert isinstance(result, EntitlementSet)
    assert held_capabilities(result) == {
        "read:client.name",
        "read:ticket.subject",
        "write:ticket.status",
    }


def test_a_partner_resolves_to_nothing_however_many_rows_they_have() -> None:
    """M1.2.4 through the resolver rather than the helper. Delete this and the partner
    rule holds in `standing_entitlement` and leaks in the function everybody calls."""
    result = resolve_entitlement(
        partner(),
        grants=[subject_grant("read:client.name", subject=principal_subject("u_partner"))],
        now=NOW,
    )
    assert isinstance(result, NoStandingEntitlement)


def test_deputy_depth_is_swept_across_a_whole_grant_table() -> None:
    """M1.3.3. `appoint_deputy` cannot be the only enforcement: rows arrive from seed
    files and migrations that call the constructor directly."""
    grants = [
        role_grant(Role.SUPER_ADMIN, "u_a"),
        role_grant(Role.SUPER_ADMIN, "u_b", not_after=NOW + timedelta(days=5), deputy_of="u_a"),
    ]
    assert check_deputy_depth(grants, NOW) == []

    chained = [
        *grants,
        role_grant(Role.SUPER_ADMIN, "u_c", not_after=NOW + timedelta(days=5), deputy_of="u_b"),
    ]
    findings = check_deputy_depth(chained, NOW)
    assert len(findings) == 1
    assert "depth one" in findings[0]
