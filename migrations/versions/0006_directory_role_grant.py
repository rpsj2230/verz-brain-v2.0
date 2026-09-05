"""Role grants a directory asserted, in a table the directory sync owns outright.

One table, no functions, no triggers and no rows - the same shape as 0004, and for the same
reason: everything interesting about it is in what it may hold and who may write to it.

**Why a second table rather than a column on the first.** This is decision 21 in
`docs/needs-rupash.md`, and the argument is about reach rather than care. A directory sync
has to be able to take a role away, because somebody leaving a Keycloak group has to stop
being an approver; so the sync deletes. If a grant a person made and a grant a group
asserted live in one table, then every run of the sync has to work out which rows are its
own, and there are only two ways that goes. It gets it wrong and deletes a hand-made grant,
which is silent, because the symptom is a person quietly holding less than they should. Or
it gets it right by carrying a `source` column that every delete, every count and every
access review afterwards has to remember to filter on, and the statement that forgets is the
one somebody writes during an incident. A separate table makes "the sync may delete anything
it can see" both true and safe, because what it can see is only ever its own rows.

The other table is not built yet: `role_grant` is M1.3.2, and today only the type exists
(`brain.identity.roles.RoleGrant`). This migration deliberately does not build it and this
table must not be made to serve as it - whoever builds `role_grant` builds a second one, with
`deleted_at` and no DELETE grant, like every other table in this schema.

**This migration grants DELETE, and it is the first in this system that does.** Every table
before it retires rows with `deleted_at` and grants SELECT, INSERT and UPDATE only, on the
grounds that a hard delete destroys the audit trail for the thing deleted. That rule is not
being relaxed - it is being satisfied a different way. A tombstone column here would be a row
that subtracts, which `brain.identity.packs.subtractive_state` refuses across the identity
package and which `revoke_role` already refuses for hand-made role grants: a `revoked` flag
turns "does she hold this role" into a question about evaluation order. The record that the
directory once asserted the role belongs in `obs.audit_entry`, which is append-only and which
a delete here cannot reach.

So the privilege is deliberate, it is scoped to exactly one table, and
`tests/unit/test_directory_role_grant.py` asserts that no other table in any migration grants
it. That test is the thing that keeps this from becoming a precedent.

**The primary key is the natural key.** `(principal_id, role, source_group)` is the entire
content of a row. A surrogate `uuid` would let the same assertion be stored twice, and two
rows saying one thing is not a duplicate-row problem here: reconciliation would delete one
of them, report the role removed, and leave the person holding it. The key refuses the second
row, so the sync's insert is idempotent by construction rather than by whichever ON CONFLICT
clause somebody wrote.

**There is no scope column.** The scope belongs to the rule that maps the group
(`brain.identity.oidc.GroupRoleRule`), which is reviewed in this repository. A copy on the row
would be a second answer that nothing updates, because reconciliation keys on the triple above
and never rewrites a row whose group is still asserted - so a scope narrowed in the rule would
go on being served wide from a row written months earlier.

**The SQL is written out, not assembled**, as in 0001 through 0005. Nothing here interpolates
a value into a statement.

**Nothing imports `brain.tables`.** The check constraints below are the same predicates the
model declares, copied deliberately, so this migration keeps describing the database it
actually built rather than whatever the models say today. `tests/unit/test_tables.py` compares
the two on rendered DDL.

Task ids: M1.1.5

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: One table. Listed as a tuple anyway, so `downgrade` walks the same list in reverse and
#: `tests/unit/test_tables.py` can compare it against the package tuple's last slice - the
#: shape every migration here uses, and worth keeping at one entry rather than special-casing.
TABLES: tuple[str, ...] = ("auth.directory_role_grant",)

#: `brain.identity.roles.Role`, copied rather than imported for the reason 0004 gives: a
#: migration describes the database it built, not whatever the enum says today. Sorted, so
#: the rendered SQL does not depend on the enum's declaration order.
ROLE_IN = (
    "role IN ('approver', 'auditor', 'connector_admin', 'department_admin', "
    "'member', 'super_admin')"
)

#: `USING (true)` rather than a liveness test, for the same reason `auth.session` has one in
#: 0003: there is no `deleted_at` on this table to test. Row-level security is enabled anyway
#: so that this cannot be the table nobody turned it on for, and so `sweep_rls` stays green -
#: a sweep that has an exception in it is a sweep with a place to hide the real one.
#:
#: The policy is `FOR ALL`, which covers the DELETE granted below. That is the point rather
#: than an oversight: the sync's deletes go through the same policy as its reads, so a future
#: policy that narrows what the sync can see narrows what it can remove by the same edit.
RLS: tuple[str, ...] = (
    "ALTER TABLE auth.directory_role_grant ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY directory_role_grant_visible ON auth.directory_role_grant
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

#: The one DELETE grant in this system. See the module docstring for why, and
#: `tests/unit/test_directory_role_grant.py` for the test that stops it spreading.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE, DELETE ON auth.directory_role_grant TO brain_app",
)


def _create_directory_role_grant() -> None:
    op.create_table(
        "directory_role_grant",
        sa.Column("principal_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("role", sa.String(32), primary_key=True, nullable=False),
        sa.Column("source_group", sa.String(300), primary_key=True, nullable=False),
        # When the sync last confirmed the directory still asserts this. A sync that has
        # stopped running fails in the dangerous direction - nothing is removed, so everybody
        # keeps everything - and the symptom is an absence of change. This column makes
        # "these rows are from a sync that last ran a fortnight ago" a query.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        # When the directory first asserted it. `granted_at` on the resulting RoleGrant is
        # built from this, so the grant reads as dating from the membership rather than from
        # whichever sync run last touched the row.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Declared in the order the model declares them - checks, then the foreign key - so
        # the rendered DDL is character-for-character what `CreateTable` produces from the
        # model. `test_the_migration_builds_the_table_the_model_declares` compares the two,
        # and a comparison on rendered SQL is sensitive to constraint order.
        sa.CheckConstraint(ROLE_IN, name="role"),
        sa.CheckConstraint("length(btrim(source_group)) > 0", name="source_group_present"),
        # RESTRICT rather than CASCADE. The sync deleting its own rows is ordinary; a
        # principal disappearing from underneath them is not, and the second must fail loudly
        # rather than tidy itself away.
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_directory_role_grant_principal_id_principal",
            ondelete="RESTRICT",
        ),
        schema="auth",
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 through 0005 do; this keeps
    # the constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)

    _create_directory_role_grant()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policy and the table privileges belong to the table and go with it, and this
    # migration creates no function and no trigger, so dropping the table is the whole
    # reversal. `auth` is not dropped: 0002 created it and 0002's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
