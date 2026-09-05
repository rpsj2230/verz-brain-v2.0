"""The capability registry, and the settings an operator tunes without a deploy.

Two tables, no functions, no triggers and no rows. That is worth stating first, because
0003 was the opposite of all four and this file is much shorter for it.

**`gate.capability_registry` is M0.2.3's third piece.** The grammar and the validator have
lived in `brain.core.entitlement` since M0.2; what was missing is somewhere to say which
capability strings are meant to exist. Without it a typo in a grant and a permission
somebody deliberately created are the same row, and the access review reading the grant
table has no way to tell them apart.

**`ops.setting` is M31.3.1.4.** Configuration as data in Postgres rather than code, so
tuning is not a deploy. The rule the table is shaped around is that nothing in it may widen
anybody's reach: no capability, no scope, no grant, no leash rung - not as a column, not as
a value, and not as a key. `src/brain/tables/config.py` makes the argument at length and
lists what is enforced and what is not.

Four things to read before changing this file.

**The SQL is written out, not assembled**, as in 0001, 0002 and 0003. Nothing here
interpolates a value into a statement.

**Nothing imports `brain.tables`.** The check constraints below are the same predicates the
models declare, copied deliberately, so this migration keeps describing the database it
actually built rather than whatever the models say today.
`tests/unit/test_tables.py` compares the two on rendered DDL.

**This migration needed no `MERGE` workaround, and the reason is worth recording.**
`brain.ops.migration_policy.DML` searches a migration's text for the keywords that open a
row write and refuses them beside `op.create_table`, because schema and data in one
migration cannot be rolled back independently. 0003 had to write its trigger bodies as
`MERGE` to get past that, since the check cannot tell a data migration from a statement
inside a `$$ ... $$` body that runs months later at write time. Nothing here writes a row at
all - the registry ships empty because a seeded vocabulary is a vocabulary nobody chose, and
`ops.setting` ships empty because an absent row means the compiled default - so there is
nothing to work around. The check should still learn to skip function bodies.

**There is deliberately no audit trigger on `ops.setting`, and that is a gap rather than a
decision that closes.** Once tuning is not a deploy, the deploy log stops recording who
changed what, and `updated_by` plus `updated_at` only ever answer for the most recent
change. A trigger appending to `obs.audit_entry`, the way 0003 does for every grant, is the
right fix and cannot be written here: `brain.audit.ledger.SUBJECT_KINDS` has no kind for a
setting and `AuditAction` has no member for tuning one. Both are closed on purpose, and
`brain.tables.audit` is explicit that widening one to make a trigger read better is how a
closed set stops being closed. Widening them needs its own migration and its own argument.

Task ids: M0.2.3, M31.3.1.4

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: Every table this migration creates, ordered so a table appears after everything it points
#: at. `downgrade` walks this in reverse, which is what makes the two provably inverse
#: without anybody maintaining a second list. Neither table carries a foreign key, so the
#: order here is only the order the file reads in.
TABLES: tuple[str, ...] = (
    "gate.capability_registry",
    "ops.setting",
)

# ------------------------------------------------------------------ grammars
# Copied from the domain types rather than imported from them; see the module docstring.
# `brain.core.entitlement.CAPABILITY_RE`
CAPABILITY_GRAMMAR = r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*|\.\*)*$"
# `brain.core.envelope.TOOL_NAME_PATTERN`, with its three named groups turned into ordinary
# ones because PostgreSQL has no `(?P<name>` syntax. That module owns the grammar and
# `brain.tools.registry.assert_tool_name` is what refuses a tool on it. There used to be
# three copies that disagreed - two of them `name.name`, admitting `client.read` - and this
# mirrors the strict one, which is now the only one.
TOOL_NAME_GRAMMAR = r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)$"
# `brain.tables.config.SETTING_KEY_PATTERN`. Two segments at least: a bare `timeout` is a
# setting nobody can tell the owner of.
SETTING_KEY_GRAMMAR = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

# --------------------------------------------------------------- vocabularies
VERB_IN = "split_part(capability, ':', 1) IN ('admin', 'approve', 'invoke', 'read', 'write')"
VALUE_TYPE_IN = "value_type IN ('boolean', 'integer', 'json', 'number', 'string')"

# ------------------------------------------------ nothing tunable may be a permission
# `brain.tables.config.RESERVED_KEY_PREFIXES`. A key in one of these namespaces is refused
# by the column rather than by whoever happens to review the row, because an operator
# reaching for a permission knob reaches for its name first.
KEY_IS_NOT_A_PERMISSION = (
    "key !~ '^(auth|capability|entitlement|gate|grant|leash|pack|policy|principal|scope)\\.'"
)

#: A capability string may not be stored as a setting value.
VALUE_IS_NOT_A_CAPABILITY = (
    f"NOT (value_type = 'string' AND (value #>> '{{}}') ~ '{CAPABILITY_GRAMMAR}')"
)

#: Nor may a `Scope.model_dump()`. `IS DISTINCT FROM` rather than `<>`, because the plain
#: comparison yields null for every value that is not an object carrying `clauses`, and a
#: check constraint evaluating to null passes by accident rather than on purpose.
VALUE_IS_NOT_A_SCOPE = "jsonb_typeof(value -> 'clauses') IS DISTINCT FROM 'array'"

#: `value_type` and `value` have to agree, or the declared type is a label. `ELSE false`
#: rather than an open CASE: a CASE with no ELSE yields null for an unlisted type and a
#: check constraint passes on null, so a sixth value type added to the vocabulary above and
#: forgotten here would accept anything at all. Refusing it is the failure that gets fixed.
VALUE_MATCHES_ITS_TYPE = (
    "CASE value_type"
    " WHEN 'string' THEN jsonb_typeof(value) = 'string'"
    " WHEN 'integer' THEN jsonb_typeof(value) = 'number'"
    " AND (value #>> '{}') ~ '^-?[0-9]+$'"
    " WHEN 'number' THEN jsonb_typeof(value) = 'number'"
    " WHEN 'boolean' THEN jsonb_typeof(value) = 'boolean'"
    " WHEN 'json' THEN jsonb_typeof(value) IN ('object', 'array')"
    " ELSE false END"
)

#: Live rows only, for both tables carrying `deleted_at`.
LIVE = "deleted_at IS NULL"

# ------------------------------------------------------------ row-level security
# The same pair per table as 0002 and 0003, and the same `WITH CHECK (true)` beside a
# `USING` that hides retired rows: without it PostgreSQL reuses the USING expression to
# check the new row, so setting `deleted_at` would be refused by the very policy meant to
# hide the row afterwards, and a soft delete would be impossible.
#
# `ops.setting` is the first table added since `brain.ops.sweeps.sweep_rls` learned about
# the `ops` schema, so this is also the first policy in that schema whose absence CI would
# actually notice.
RLS: tuple[str, ...] = (
    "ALTER TABLE gate.capability_registry ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY capability_registry_live ON gate.capability_registry
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE ops.setting ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY setting_live ON ops.setting
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
)

# ------------------------------------------------------------------- privileges
# Per table, as 0001 said. No table grants DELETE: retirement is `deleted_at`, and on
# `ops.setting` that is what returns the system to its compiled default while leaving a row
# saying the override was once there.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE ON gate.capability_registry TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON ops.setting TO brain_app",
)


def _create_capability_registry() -> None:
    op.create_table(
        "capability_registry",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("capability", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        # Nullable: a field policy's required capability gates a field rather than a call,
        # and the opaque capability is demanded by the redactor. Where it is set, it answers
        # the one review question nothing else can: which tool goes dark if this is retired.
        sa.Column("required_by_tool", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # The same predicate the grant table carries. A vocabulary admitting a shape the
        # grant table refuses would be a list of capabilities that cannot be granted.
        sa.CheckConstraint(f"capability ~ '{CAPABILITY_GRAMMAR}'", name="capability_grammar"),
        sa.CheckConstraint(VERB_IN, name="capability_verb"),
        sa.CheckConstraint("length(btrim(description)) > 0", name="described"),
        sa.CheckConstraint(
            f"required_by_tool IS NULL OR required_by_tool ~ '{TOOL_NAME_GRAMMAR}'",
            name="tool_name_grammar",
        ),
        # No principal, no scope, no expiry and no granted_by. This table records that a
        # capability exists, never that anybody holds it; a scope column here would read as
        # a safety rail and behave as a grant nobody granted.
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_registry_deleted_at",
        "capability_registry",
        ["deleted_at"],
        schema="gate",
    )
    # One live row per capability, and partial for the reason `principal_identity` gives: a
    # total constraint would make a retired capability's name unusable forever.
    op.create_index(
        "uq_capability_registry_capability_live",
        "capability_registry",
        ["capability"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )


def _create_setting() -> None:
    op.create_table(
        "setting",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        # No server default: a setting with no value is a row that exists and configures
        # nothing, which reads in the console as having been set.
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"key ~ '{SETTING_KEY_GRAMMAR}'", name="key_grammar"),
        sa.CheckConstraint(KEY_IS_NOT_A_PERMISSION, name="key_is_not_a_permission"),
        sa.CheckConstraint(VALUE_TYPE_IN, name="value_type"),
        sa.CheckConstraint(VALUE_MATCHES_ITS_TYPE, name="value_matches_its_type"),
        sa.CheckConstraint(VALUE_IS_NOT_A_CAPABILITY, name="value_is_not_a_capability"),
        sa.CheckConstraint(VALUE_IS_NOT_A_SCOPE, name="value_is_not_a_scope"),
        sa.CheckConstraint("length(btrim(description)) > 0", name="described"),
        sa.CheckConstraint("length(btrim(updated_by)) > 0", name="updated_by_present"),
        # `updated_by` is a plain column and not a foreign key to `auth.principal`, for the
        # reason `capability_grant.granted_by` is one: a setting can be changed by something
        # that is not a principal row, and the record has to outlive whoever changed it.
        schema="ops",
    )
    op.create_index("ix_ops_setting_deleted_at", "setting", ["deleted_at"], schema="ops")
    # Every reader looks a setting up by key. Two live rows for one key would make the
    # effective configuration depend on which came back first, which is an incident that
    # reads as intermittent rather than as a duplicate row.
    op.create_index(
        "uq_setting_key_live",
        "setting",
        ["key"],
        unique=True,
        schema="ops",
        postgresql_where=sa.text(LIVE),
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001, 0002 and 0003 do; this
    # keeps the constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)

    # Creation order matches TABLES, which the downgrade reverses.
    _create_capability_registry()
    _create_setting()

    for statement in RLS:
        op.execute(statement)

    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # Policies, indexes and table privileges all belong to their table and go with it, and
    # this migration creates no function and no trigger, so dropping the two tables is the
    # whole reversal.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
