"""Role grants a directory asserted: the table that holds them and the reconciliation
that keeps it matching the directory.

Every test here is a way a role somebody was given by hand disappears, or a way a role the
directory withdrew keeps working. The two failures pull in opposite directions and one table
cannot avoid both, which is the argument decision 21 in `docs/needs-rupash.md` settles and
which `test_a_hand_made_grant_is_never_in_the_delete_set` is the test for.

Asserted against the model, against the migration's rendered constants, and against the pure
functions, because the three are written separately on purpose - the model describes what the
code expects, the migration describes the database it built, the reconciler decides - and the
only thing comparing them is a test.

Task ids: M1.1.5
"""

from __future__ import annotations

import importlib.util
import inspect
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from brain.core.scope import Scope
from brain.db import metadata
from brain.identity.directory import (
    DirectoryAssertion,
    assert_reconciler_cannot_reach_hand_made_grants,
    directory_role_grants,
    reconcile,
    roles_held,
)
from brain.identity.oidc import GroupRoleRule
from brain.identity.roles import IdentityError, Role, RoleGrant, standing_super_admins
from brain.tables.identity import ROLE_CHARS, SOURCE_GROUP_CHARS, DirectoryRoleGrantRow

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0006_directory_role_grant.py"

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
ISSUER = "https://id.verz.example/realms/brain"

#: The group a client's Keycloak uses to say somebody approves for the web department.
APPROVER_GROUP = "/brain/approver/web"
WEB = Scope.department("web")

APPROVER_RULE = GroupRoleRule(group=APPROVER_GROUP, role=Role.APPROVER, scope=WEB)
SUPER_ADMIN_RULE = GroupRoleRule(group="/brain/super-admin", role=Role.SUPER_ADMIN)


#: A PostgreSQL dialect to render DDL against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
_DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def _migration() -> ModuleType:
    return _module(MIGRATION)


def _module(path: Path) -> ModuleType:
    """Any migration, loaded. Module level only: nothing here calls `upgrade`."""
    spec = importlib.util.spec_from_file_location(f"m{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that the tests read this rather than
    the file's text: a statement sitting in a constant that `upgrade` never executes would
    pass a source-text search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    with Operations.context(context):
        getattr(_migration(), direction)()
    return buffer.getvalue()


def _squash(text: str) -> str:
    """Collapse whitespace, so a statement wrapped across lines still compares."""
    return " ".join(text.split())


def _table() -> Table:
    """The mapped table, narrowed to `Table`.

    `__table__` on a declarative class is annotated `FromClause`, which has no `primary_key`
    worth reading and cannot be handed to `CreateTable`. Narrowed once here rather than with
    an ignore comment at each use, so a genuine type error is still reported.
    """
    mapped = DirectoryRoleGrantRow.__table__
    assert isinstance(mapped, Table)
    return mapped


def assertion(
    pid: str = "u_priya",
    role: Role = Role.APPROVER,
    group: str = APPROVER_GROUP,
) -> DirectoryAssertion:
    return DirectoryAssertion(principal_id=pid, role=role, source_group=group)


def hand_made(
    role: Role = Role.APPROVER,
    pid: str = "u_priya",
    *,
    scope: Scope | None = WEB,
    granted_at: datetime = NOW,
    not_after: datetime | None = None,
    deputy_of: str | None = None,
) -> RoleGrant:
    """A grant a person made, with a named grantor and a reason. The row the whole design
    exists to keep out of the sync's reach."""
    return RoleGrant(
        principal_id=pid,
        role=role,
        scope=scope,
        granted_by="u_founder",
        reason="appointed at the September review",
        granted_at=granted_at,
        not_after=not_after,
        deputy_of=deputy_of,
    )


# ------------------------------------------------------------- the reconciliation
def test_a_grant_the_directory_stopped_asserting_is_proposed_for_deletion() -> None:
    """The reason the sync needs a table it may delete from at all. Somebody leaves the
    approver group and has to stop being an approver; without this the row lives forever and
    the directory becomes a place where access is added and never removed.

    Delete this test and reconciliation quietly becomes an upsert, which is exactly what
    `role_grants_from_groups` warns about: "a login-time upsert only ever adds"."""
    held = {assertion()}
    result = reconcile(asserted=[], held=held)
    assert result.to_delete == frozenset(held)
    assert result.to_insert == frozenset()


def test_a_grant_the_directory_still_asserts_is_left_alone() -> None:
    """A sync that rewrote every row it still agrees with would churn `last_seen_at` and, in
    an audit ledger, read exactly like everybody's role being removed and restored on every
    run. `unchanged` is what a caller touches timestamps on.

    Delete this and a re-sync of an unchanged directory proposes a full delete-and-insert,
    which is indistinguishable from a real change in every review afterwards."""
    still_there = assertion()
    result = reconcile(asserted={still_there}, held={still_there})
    assert result.to_delete == frozenset()
    assert result.to_insert == frozenset()
    assert result.unchanged == frozenset({still_there})
    assert result.is_empty


def test_a_grant_the_directory_has_started_asserting_is_proposed_for_insertion() -> None:
    """The other half. Without it the sync can only ever remove, and joining a group confers
    nothing until somebody notices."""
    fresh = assertion()
    result = reconcile(asserted={fresh}, held=[])
    assert result.to_insert == frozenset({fresh})
    assert result.to_delete == frozenset()


def test_the_reconciler_can_never_propose_deleting_a_row_it_was_not_shown() -> None:
    """`to_delete` is a subset of `held`, always, because it is a set difference.

    The caller executes these deletes. A reconciler able to name a row it was not handed as
    currently-held would be one that deletes a row nobody read first - and the way that
    happens in practice is somebody replacing the difference with a loop that rebuilds the
    delete list from the directory side.

    Delete this test and that refactor passes review."""
    held = {assertion(), assertion(pid="u_sam"), assertion(role=Role.MEMBER, group="/brain/all")}
    for asserted in ([], [assertion()], list(held), [assertion(pid="u_nobody")]):
        result = reconcile(asserted=asserted, held=held)
        assert result.to_delete <= frozenset(held)


def test_a_hand_made_grant_is_never_in_the_delete_set() -> None:
    """**This is the test that justifies the two-table design.** Deleting it removes the only
    mechanical statement of why `auth.directory_role_grant` is a second table rather than a
    `source` column on the first, and the next person to read the schema will merge them.

    Priya was appointed an approver by a person, with a grantor and a reason on the row. She
    is also in the Keycloak group that asserts the same role, and this morning she was taken
    out of that group. The sync must remove what the group conferred and must not touch what
    the appointment conferred - and the two grants name the same principal, the same role and
    the same scope, so nothing about the row itself distinguishes them.

    With one table that distinction lives in a WHERE clause. Here it does not exist as a
    question: `reconcile` is handed directory assertions, has no parameter a `RoleGrant`
    fits into, and so cannot name her appointment in any answer it gives. She keeps the role
    afterwards, from the other table, which is what the last assertion checks."""
    appointment = hand_made(Role.APPROVER)
    from_the_group = assertion(role=Role.APPROVER)
    assert appointment.principal_id == from_the_group.principal_id
    assert appointment.role is from_the_group.role

    result = reconcile(asserted=[], held={from_the_group})

    # The directory row goes, and it is the only thing in the answer.
    assert result.to_delete == frozenset({from_the_group})
    assert all(isinstance(item, DirectoryAssertion) for item in result.to_delete)
    # Nothing in the delete set names the appointment, and nothing could: the appointment is
    # not a `DirectoryAssertion` and there is no member of the set equal to it.
    assert appointment not in result.to_delete  # type: ignore[comparison-overlap]

    # And after the sync has done everything it proposed, she is still an approver.
    directory_after = directory_role_grants(
        held=[],
        rules=[APPROVER_RULE],
        issuer=ISSUER,
        granted_at=NOW,
    )
    still_held = roles_held(hand_made=[appointment], directory=directory_after.grants, now=NOW)
    assert [g.role for g in still_held] == [Role.APPROVER]
    assert still_held[0].granted_by == "u_founder"


def test_the_reconciler_has_nowhere_to_put_a_hand_made_grant() -> None:
    """The same rule as the test above, stated against the signature rather than against a
    run. A check inside the function body is removable by whoever adds the feature that needs
    it; a parameter that does not exist has to be added first, which is a diff with a reviewer
    on it. Same mechanism as `assert_no_role_in_resolution`.

    Delete this and `reconcile` can grow a `hand_made: Sequence[RoleGrant]` argument without
    anything failing until somebody's grant is gone."""
    assert_reconciler_cannot_reach_hand_made_grants(reconcile)

    def leaky_by_parameter(
        asserted: list[DirectoryAssertion],
        hand: list[RoleGrant],
    ) -> frozenset[DirectoryAssertion]:
        return frozenset(asserted) - frozenset()  # pragma: no cover - never called

    def leaky_by_return(asserted: list[DirectoryAssertion]) -> tuple[RoleGrant, ...]:
        return ()  # pragma: no cover - never called

    for leaky in (leaky_by_parameter, leaky_by_return):
        with pytest.raises(IdentityError, match="grant a person made"):
            assert_reconciler_cannot_reach_hand_made_grants(leaky)


def test_reconciliation_is_a_pure_function_of_two_sets() -> None:
    """No database, no clock, no principal lookup. The reconciler is the half of a sync that
    can be wrong in a way nobody notices until rows are gone, so it is the half that has to be
    testable without a server.

    Asserted on the signature: a parameter named for a session, a connection or a moment is
    how the purity is lost, and it is always lost by addition rather than by rewrite."""
    parameters = set(inspect.signature(reconcile).parameters)
    assert parameters == {"asserted", "held"}


# ------------------------------------------------- the key refuses a repeat
def test_the_same_assertion_twice_is_refused_by_the_key() -> None:
    """`(principal_id, role, source_group)` is the primary key, so the database refuses the
    second row rather than storing two sentences that say one thing.

    Two rows for one assertion is not a duplicate-row problem here. Reconciliation would
    delete one of them, report the role removed, and leave the person holding it from the
    other - which is a permission that survives its own removal and shows as removed in the
    ledger.

    Delete this test and a surrogate `uuid` primary key reads in review as a tidy-up."""
    primary = [c.name for c in _table().primary_key.columns]
    assert primary == ["principal_id", "role", "source_group"]
    assert "id" not in _table().columns

    # And the same in the type, which is what the reconciler actually deduplicates on: two
    # assertions that say the same thing are one member of a set.
    assert len({assertion(), assertion()}) == 1
    result = reconcile(asserted=[assertion(), assertion()], held=[])
    assert len(result.to_insert) == 1


def test_two_groups_conferring_one_role_are_two_rows() -> None:
    """`source_group` is part of the key rather than a detail on the row. Two groups may both
    confer `approver`, and if only one stops being asserted the person keeps the role.

    Collapsing them - keying on (principal, role) alone - would make leaving either group
    remove it, which is the failure that looks most like a working sync."""
    from_web = assertion(group=APPROVER_GROUP)
    from_leads = assertion(group="/brain/approver/leads")
    result = reconcile(asserted={from_leads}, held={from_web, from_leads})
    assert result.to_delete == frozenset({from_web})
    assert result.unchanged == frozenset({from_leads})


def test_an_assertion_needs_a_principal_and_a_group() -> None:
    """A blank principal names nobody, and a row with no group cannot be reconciled at all -
    nothing can ever say the group stopped asserting it, so the row is permanent."""
    with pytest.raises(IdentityError):
        DirectoryAssertion(principal_id="  ", role=Role.MEMBER, source_group=APPROVER_GROUP)
    with pytest.raises(IdentityError):
        DirectoryAssertion(principal_id="u_priya", role=Role.MEMBER, source_group="  ")


def test_a_role_name_cannot_arrive_as_a_principal_id() -> None:
    """M1.3.5 through the directory's own door. `principal_id = "super_admin"` creates a row
    that reads like a role grant to every human who looks at the table, and resolves for
    whoever happens to own that id."""
    with pytest.raises(IdentityError):
        DirectoryAssertion(
            principal_id="super_admin", role=Role.MEMBER, source_group=APPROVER_GROUP
        )


# ------------------------------------------------ held rows become role grants
def test_the_scope_comes_from_the_rule_and_never_from_the_row() -> None:
    """The table has no scope column, and this is why. Reconciliation keys on the triple, so a
    row whose group is still asserted is never rewritten - a scope copied onto it at insert
    would go on being served at its original width long after somebody narrowed the reviewed
    rule.

    Delete this test and a `scope` column on the table looks like an obvious convenience."""
    assert "scope" not in _table().columns
    narrowed = GroupRoleRule(group=APPROVER_GROUP, role=Role.APPROVER, scope=Scope.department("cx"))
    result = directory_role_grants(
        held=[assertion()], rules=[narrowed], issuer=ISSUER, granted_at=NOW
    )
    assert [g.scope for g in result.grants] == [Scope.department("cx")]


def test_a_held_row_whose_group_lost_its_rule_confers_nothing_and_is_reported() -> None:
    """A rule retired, or a rule renamed with a typo, look identical from the row's side. The
    first is normal and the second is an outage that presents as "my permissions disappeared
    overnight", so the rows are returned rather than dropped silently.

    They are not deleted here: deleting is `reconcile`'s job and it answers to the directory,
    not to the rule set. A rule coming back must make the row work again."""
    orphan = assertion(group="/brain/retired-group")
    result = directory_role_grants(
        held=[orphan], rules=[APPROVER_RULE], issuer=ISSUER, granted_at=NOW
    )
    assert result.grants == ()
    assert result.unruled == (orphan,)


def test_a_row_whose_rule_now_points_at_a_different_role_is_not_coerced() -> None:
    """Somebody re-points an existing group at another role. The stale row is not silently
    upgraded to the new one: the next reconciliation deletes it and inserts the right triple,
    because the directory asserts the new one and not the old."""
    repointed = GroupRoleRule(group=APPROVER_GROUP, role=Role.AUDITOR)
    result = directory_role_grants(
        held=[assertion(role=Role.APPROVER)],
        rules=[repointed],
        issuer=ISSUER,
        granted_at=NOW,
    )
    assert result.grants == ()
    assert len(result.unruled) == 1


def test_a_directory_grant_says_a_directory_made_it() -> None:
    """`granted_by` names the issuer, so a row that appeared with no human behind it says so.
    That is the difference between "somebody appointed her" and "a directory did", and it is
    the only thing an access review has to go on."""
    result = directory_role_grants(
        held=[assertion()], rules=[APPROVER_RULE], issuer=ISSUER, granted_at=NOW
    )
    grant = result.grants[0]
    assert grant.granted_by == f"idp:{ISSUER}"
    assert APPROVER_GROUP in grant.reason
    assert grant.granted_at == NOW


def test_an_issuer_too_long_to_record_as_a_grantor_is_refused() -> None:
    """`RoleGrant.granted_by` is 128 characters. Without this the failure is an INSERT that
    fails after the decision has been taken, in whatever transaction the sync opened."""
    with pytest.raises(IdentityError, match="too long"):
        directory_role_grants(
            held=[assertion()], rules=[APPROVER_RULE], issuer="x" * 200, granted_at=NOW
        )


def test_the_grants_come_back_in_a_stable_order() -> None:
    """Sets iterate in an order that varies with hash randomisation. Without the sort, a diff
    of two sync runs is noise and a golden test flaps."""
    held = [
        assertion(pid="u_sam", group="/brain/all", role=Role.MEMBER),
        assertion(pid="u_priya", group="/brain/all", role=Role.MEMBER),
    ]
    rules = [GroupRoleRule(group="/brain/all", role=Role.MEMBER)]
    first = directory_role_grants(held=held, rules=rules, issuer=ISSUER, granted_at=NOW)
    second = directory_role_grants(
        held=list(reversed(held)), rules=rules, issuer=ISSUER, granted_at=NOW
    )
    assert [g.principal_id for g in first.grants] == ["u_priya", "u_sam"]
    assert first.grants == second.grants


# ------------------------------------------------------------------ the union
def test_role_resolution_unions_both_sources() -> None:
    """A role granted by hand and a role asserted by a directory are both roles the person
    holds. Reading one table would make whichever source was left out invisible - and the
    invisible one is a permission somebody has and nobody can see.

    Delete this and the console lists half of somebody's roles, which reads as correct."""
    appointment = hand_made(Role.DEPARTMENT_ADMIN, "u_priya", scope=WEB)
    from_directory = directory_role_grants(
        held=[assertion(pid="u_sam", role=Role.SUPER_ADMIN, group="/brain/super-admin")],
        rules=[SUPER_ADMIN_RULE],
        issuer=ISSUER,
        granted_at=NOW,
    )
    united = roles_held(hand_made=[appointment], directory=from_directory.grants, now=NOW)
    assert {(g.principal_id, g.role) for g in united} == {
        ("u_priya", Role.DEPARTMENT_ADMIN),
        ("u_sam", Role.SUPER_ADMIN),
    }


def test_a_second_source_can_only_ever_add() -> None:
    """Entitlements in this system are additive only: no deny list, no negative grant, nothing
    that subtracts at resolve time. So every role held from the hand-made table alone is still
    held once the directory is unioned in, whatever the directory says.

    Delete this and somebody adds a rule that lets a directory row take a role away, which is
    one line and reads as a feature."""
    appointments = [
        hand_made(Role.APPROVER, "u_priya"),
        hand_made(Role.AUDITOR, "u_sam", scope=None),
    ]
    alone = roles_held(hand_made=appointments, directory=[], now=NOW)
    # Asserted before the subset check below, and found by mutation: `alone` is computed by
    # the same function under test, so a `roles_held` that returned nothing at all would make
    # every subset comparison below hold vacuously.
    assert set(alone) == set(appointments)

    everyone = GroupRoleRule(group="/brain/all", role=Role.MEMBER)
    for held in ([], [assertion(pid="u_kim", role=Role.MEMBER, group="/brain/all")]):
        from_directory = directory_role_grants(
            held=held, rules=[everyone], issuer=ISSUER, granted_at=NOW
        )
        united = roles_held(hand_made=appointments, directory=from_directory.grants, now=NOW)
        assert set(alone) <= set(united), "a directory row took a role away"


def test_one_person_holding_a_role_from_both_sources_is_counted_once() -> None:
    """`standing_super_admins` counts what the union returns, and `revoke_role` refuses to take
    that count below the floor of two. Without the collapse, one person appointed *and* in the
    Keycloak group satisfies a floor that exists precisely because one person is a single point
    of lockout.

    The hand-made grant is the one kept, because it is the one a reviewer can act on: it names
    a grantor and a reason, where the directory row names a group in somebody else's
    Keycloak."""
    appointment = hand_made(Role.SUPER_ADMIN, "u_priya", scope=None)
    from_directory = directory_role_grants(
        held=[assertion(pid="u_priya", role=Role.SUPER_ADMIN, group="/brain/super-admin")],
        rules=[SUPER_ADMIN_RULE],
        issuer=ISSUER,
        granted_at=NOW,
    )
    united = roles_held(hand_made=[appointment], directory=from_directory.grants, now=NOW)
    assert len(standing_super_admins(united, NOW)) == 1
    assert united[0].granted_by == "u_founder", "the reviewable grant is the one kept"


def test_an_expired_grant_from_either_source_confers_nothing() -> None:
    """An expired deputy is cover that has ended. Including it would make
    `standing_super_admins` count cover as ownership, which is the exact thing the floor
    exists to refuse."""
    ended = hand_made(
        Role.SUPER_ADMIN,
        "u_sam",
        scope=None,
        not_after=NOW - timedelta(days=1),
        granted_at=NOW - timedelta(days=10),
        deputy_of="u_priya",
    )
    assert roles_held(hand_made=[ended], directory=[], now=NOW) == ()


def test_the_two_sources_are_keyword_only() -> None:
    """Passing one sequence twice would double every grant in it and type-check perfectly.
    Keyword-only arguments make that a name somebody has to write twice."""
    parameters = inspect.signature(roles_held).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())


# -------------------------------------------------------------- the table
def test_the_table_records_what_the_sync_needs_and_nothing_else() -> None:
    """Principal, role, the group that asserted it, and when the sync last saw it. A column
    beyond those is a fact the sync would have to maintain a second copy of."""
    columns = set(_table().columns.keys())
    assert columns == {
        "principal_id",
        "role",
        "source_group",
        "last_seen_at",
        "created_at",
        "updated_at",
    }


def test_the_table_has_no_tombstone_column() -> None:
    """Retirement here would be a row that subtracts, which
    `brain.identity.packs.subtractive_state` refuses across the identity package and which
    `revoke_role` already refuses for hand-made role grants. The record that the directory
    once asserted a role belongs in `obs.audit_entry`, which a delete here cannot reach."""
    columns = _table().columns
    for name in ("deleted_at", "revoked_at", "active", "is_active", "removed_at"):
        assert name not in columns


def test_every_role_value_fits_the_column() -> None:
    """The width is headroom, not a check - `one_of("role", Role)` is what constrains the
    value. Without this a longer role name is truncated into one the check constraint then
    refuses at three in the morning, rather than failing here."""
    assert max(len(r.value) for r in Role) <= ROLE_CHARS


def test_the_group_column_is_as_wide_as_the_rule_that_writes_it() -> None:
    """`source_group` is part of a primary key, so the two widths have to agree. A group a
    `GroupRoleRule` accepts and the column refuses is a sync that fails on one client's
    directory and nowhere in CI."""
    metadata_items = GroupRoleRule.model_fields["group"].metadata
    limits = [m.max_length for m in metadata_items if hasattr(m, "max_length")]
    assert limits == [SOURCE_GROUP_CHARS]


def test_a_principal_cannot_vanish_from_under_a_directory_row() -> None:
    """`RESTRICT`, not `CASCADE`. The sync deleting its own rows is ordinary; a principal
    disappearing underneath them is not, and the second must fail loudly rather than tidy
    itself away."""
    fks = list(_table().foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.fullname == "auth.principal"
    assert fks[0].ondelete == "RESTRICT"


# ---------------------------------------------------------------- the migration
def test_the_migration_enables_row_level_security() -> None:
    """A policy on a table without row-level security enabled is a policy PostgreSQL never
    consults, and `sweep_rls` fails the build on a table in a named schema without it. The two
    statements are separate and forgetting the first is silent."""
    sql = "\n".join(_migration().RLS)
    assert "ALTER TABLE auth.directory_role_grant ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY directory_role_grant_visible ON auth.directory_role_grant" in sql


def test_the_policy_covers_the_delete() -> None:
    """`FOR ALL`, so the sync's deletes go through the same policy as its reads. A policy
    written `FOR SELECT` would leave DELETE ungoverned, which is the one statement here that
    must not be."""
    sql = "\n".join(_migration().RLS)
    assert "FOR ALL TO brain_app" in sql


def test_the_sync_may_delete_from_its_own_table() -> None:
    """The whole point of the second table. Without the privilege the sync cannot remove a
    role when somebody leaves a group, which is the failure that leaves a directory as a place
    where access is added and never taken away."""
    grants = "\n".join(_migration().GRANTS)
    expected = "GRANT SELECT, INSERT, UPDATE, DELETE ON auth.directory_role_grant TO brain_app"
    assert expected in grants


def test_no_other_table_in_any_migration_grants_delete() -> None:
    """This is the one DELETE grant in the system, and this test is what stops it becoming a
    precedent. Every other table retires rows with `deleted_at`, because a hard delete
    destroys the audit trail for the thing deleted.

    Delete this test and the next table that finds `deleted_at` inconvenient grants DELETE
    too, with this migration cited as the reason.

    **Asked twice, of the text and of the module, because the text scan alone had a hole.**
    It reads lines that *begin* with a quoted GRANT, which is how most migrations write their
    tuples, and a migration whose `GRANTS` is short enough to sit on one line begins that line
    with `GRANTS:` instead. `ruff format` produces exactly that for a single-statement tuple,
    so it is not a style anybody chooses. 0009 and 0014 are both in that shape, and a DELETE
    added to either passed this test until the second loop below existed. Found by mutation,
    on 0014.

    The text scan is kept rather than replaced: a statement written inline in `upgrade`, or in
    a constant under another name, is invisible to the module scan and visible to it."""
    offenders: list[str] = []
    for path in sorted(VERSIONS.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith('"GRANT') and not stripped.startswith("'GRANT"):
                continue
            if "DELETE" not in stripped:
                continue
            if "auth.directory_role_grant" in stripped:
                continue
            offenders.append(f"{path.name}: {stripped}")
    checked = 0
    for path in sorted(VERSIONS.glob("*.py")):
        grants: tuple[str, ...] = getattr(_module(path), "GRANTS", ())
        checked += len(grants)
        offenders.extend(
            f"{path.name}: {statement}"
            for statement in grants
            if "DELETE" in statement and "auth.directory_role_grant" not in statement
        )
    assert offenders == [], f"a second DELETE grant: {offenders}"
    # Said out loud, because a scan that finds nothing and a scan that looks at nothing read
    # identically from a green test, and this file's other loop has already been both.
    assert checked > 0, "no migration exposed a GRANTS tuple, so the module scan checked nothing"


def test_the_migration_follows_the_one_before_it() -> None:
    """A revision that does not chain is a migration Alembic never runs, and the symptom is a
    table missing in production while every test passes."""
    module = _migration()
    assert module.revision == "0006"
    assert module.down_revision == "0005"


def test_the_migration_builds_the_table_the_model_declares() -> None:
    """The migration copies the model's predicates rather than importing them, so that it
    keeps describing the database it actually built - which means the copy needs something
    comparing it or it rots without saying so.

    Compared on rendered DDL rather than on source text, so a difference in type, width,
    nullability, default or constraint is caught rather than a difference in wording. Rendered
    through Alembic's offline mode, which writes the statements into a buffer instead of a
    server, because there is no PostgreSQL on a development machine."""
    assert _migration().TABLES == ("auth.directory_role_grant",)
    expected = _squash(str(CreateTable(_table()).compile(dialect=_DIALECT)))
    assert expected in _squash(_rendered("upgrade"))


def test_the_column_widths_match_the_types_that_write_them() -> None:
    """Stated separately from the DDL comparison above, because that one fails with a wall of
    SQL and this one names the constant. `ROLE_CHARS` and `SOURCE_GROUP_CHARS` are part of a
    primary key, so a width that drifts from the migration is a key the model and the database
    disagree about."""
    columns = _table().columns
    assert columns["role"].type.length == ROLE_CHARS  # type: ignore[attr-defined]
    assert columns["source_group"].type.length == SOURCE_GROUP_CHARS  # type: ignore[attr-defined]
    source = inspect.getsource(_migration())
    assert f"sa.String({ROLE_CHARS})" in source
    assert f"sa.String({SOURCE_GROUP_CHARS})" in source


def test_the_downgrade_drops_what_the_upgrade_built() -> None:
    """A migration with no way back is a deploy with no way back. `auth` is not dropped: 0002
    created it and 0002's downgrade owns it."""
    source = inspect.getsource(_migration().downgrade)
    assert "op.drop_table" in source
    assert "DROP SCHEMA" not in source


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists, no unreviewed autogeneration markers, no
    schema and data change in one file."""
    from brain.ops.migration_policy import check_file

    assert check_file(MIGRATION) == []


def test_every_table_any_migration_builds_enables_row_level_security() -> None:
    """`brain.ops.sweeps.rls` asked exactly this, and it can only ask it of a live server -
    so on a development machine it prints "skip" and the answer arrives in CI, after the push.

    This is the same question asked of the migrations' text, which needs no server. It is
    here rather than in `test_tables.py` because `auth.directory_role_grant` is the table that
    would have failed it: it carries no `deleted_at`, so the policy it needs is `USING (true)`
    and there is nothing about the model that would make its absence obvious.

    A statement in a constant no `upgrade` executes would pass a source-text search, so the
    match is against `ALTER TABLE <qualified> ENABLE ROW LEVEL SECURITY` with the schema on
    it - the exact form the sweep's `relrowsecurity` column is set by."""
    from brain.tables import TABLES_IN_DEPENDENCY_ORDER

    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(VERSIONS.glob("*.py")))
    missing = [
        qualified
        for qualified in TABLES_IN_DEPENDENCY_ORDER
        if f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" not in text
    ]
    assert missing == [], f"row-level security is never enabled on {missing}"


def test_the_table_is_in_the_package_tuple() -> None:
    """`brain.tables.TABLES_IN_DEPENDENCY_ORDER` is the one list anything outside the package
    reads, and a partial list is worse than none: the table it omits looks accounted for, and
    autogenerate would propose dropping it."""
    from brain.db import Base
    from brain.tables import TABLES_IN_DEPENDENCY_ORDER

    assert "auth.directory_role_grant" in TABLES_IN_DEPENDENCY_ORDER
    assert set(TABLES_IN_DEPENDENCY_ORDER) == set(Base.metadata.tables)
