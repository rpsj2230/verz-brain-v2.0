"""The identity policy layer: who someone is on the platform, and what that is not.

Four modules re-exported here, split along the line the architecture draws between governing
the platform and governing data.

- `roles`: the six compiled roles, role grants, deputies, the Super Admin floor,
  break-glass sessions, and the partner who holds nothing.
- `packs`: capability packs, grants with a subject, and revocation as deletion.
- `teams`: teams inside a department, membership, and the two kinds of grant subject.
- `directory`: role grants the corporate directory asserts, which live in their own table
  so the sync that removes them cannot reach a grant a person made.

`oidc` and `sessions` are in the package and are not re-exported from here, which is how they
were already: the first turns an attacker-controlled string into a principal and the second
decides what somebody may do right now, and a caller should have to name either module to
reach it rather than pick it up from a package import.

The package exists so that one question stays cheap. "What can this person see" is
answered by reading their grants, and nothing here can turn it into anything else: a role
is never a grant subject, there is no negative grant to reconcile, and a deputy chain
cannot grow past one link.

This package writes no SQLAlchemy models and no migrations. Where a leaf names a table
(`role_grant`, `capability_pack`, `team`), what is here is the type and the rules that
govern it; the tables belong to whoever owns `src/brain/tables`.
"""

from __future__ import annotations

from brain.identity.directory import (
    DirectoryAssertion,
    DirectoryRoles,
    Reconciliation,
    assert_reconciler_cannot_reach_hand_made_grants,
    directory_role_grants,
    reconcile,
    roles_held,
)
from brain.identity.packs import (
    ADDITIVE_ONLY,
    CapabilityPack,
    PackAssignment,
    PackError,
    SubjectGrant,
    assert_no_role_in_resolution,
    expand,
    held_capabilities,
    resolve_entitlement,
    revoke,
    revoke_assignment,
    revoke_capability,
    subtractive_state,
)
from brain.identity.roles import (
    BREAK_GLASS_CHAIN,
    BREAK_GLASS_MAX,
    DEPUTY_MAX,
    NO_ROLE_IMPLIES_A_CAPABILITY,
    ROLE_COUNT,
    ROLE_NAMES,
    ROLE_SPECS,
    SCOPE_REQUIRED,
    SUPER_ADMIN_FLOOR,
    BreakGlassReason,
    BreakGlassSession,
    IdentityError,
    NoStandingEntitlement,
    Notification,
    Role,
    RoleGrant,
    RoleSpec,
    RoleSubjectError,
    appoint_deputy,
    assert_not_a_role,
    check_deputy_depth,
    open_break_glass,
    reach_during,
    revoke_role,
    role_capability_leaks,
    spec_for,
    standing_entitlement,
    standing_super_admins,
)
from brain.identity.teams import (
    TEAM_FIELD,
    GrantSubject,
    PrincipalSubject,
    SubjectKind,
    Team,
    TeamError,
    TeamMembership,
    TeamSubject,
    assert_within_department,
    members_of,
    membership_clause,
    principal_subject,
    references_team,
    split_team_path,
    subject_for,
    subject_reaches,
    subjects_for,
    team_path,
    team_scope,
    team_subject,
    teams_of,
)

__all__ = [
    "ADDITIVE_ONLY",
    "BREAK_GLASS_CHAIN",
    "BREAK_GLASS_MAX",
    "DEPUTY_MAX",
    "NO_ROLE_IMPLIES_A_CAPABILITY",
    "ROLE_COUNT",
    "ROLE_NAMES",
    "ROLE_SPECS",
    "SCOPE_REQUIRED",
    "SUPER_ADMIN_FLOOR",
    "TEAM_FIELD",
    "BreakGlassReason",
    "BreakGlassSession",
    "CapabilityPack",
    "DirectoryAssertion",
    "DirectoryRoles",
    "GrantSubject",
    "IdentityError",
    "NoStandingEntitlement",
    "Notification",
    "PackAssignment",
    "PackError",
    "PrincipalSubject",
    "Reconciliation",
    "Role",
    "RoleGrant",
    "RoleSpec",
    "RoleSubjectError",
    "SubjectGrant",
    "SubjectKind",
    "Team",
    "TeamError",
    "TeamMembership",
    "TeamSubject",
    "appoint_deputy",
    "assert_no_role_in_resolution",
    "assert_not_a_role",
    "assert_reconciler_cannot_reach_hand_made_grants",
    "assert_within_department",
    "check_deputy_depth",
    "directory_role_grants",
    "expand",
    "held_capabilities",
    "members_of",
    "membership_clause",
    "open_break_glass",
    "principal_subject",
    "reach_during",
    "reconcile",
    "references_team",
    "resolve_entitlement",
    "revoke",
    "revoke_assignment",
    "revoke_capability",
    "revoke_role",
    "role_capability_leaks",
    "roles_held",
    "spec_for",
    "split_team_path",
    "standing_entitlement",
    "standing_super_admins",
    "subject_for",
    "subject_reaches",
    "subjects_for",
    "subtractive_state",
    "team_path",
    "team_scope",
    "team_subject",
    "teams_of",
]
