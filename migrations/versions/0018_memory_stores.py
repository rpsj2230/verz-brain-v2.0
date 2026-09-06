"""The two memory tables, and the schema they live in.

`mem` is named in `brain.db.SCHEMAS` and created by 0001, and until now it has held nothing.
These are the first two tables in it: `mem.persistent` for what somebody stated and
`mem.adaptive` for what the system inferred. `brain.tables.memory`'s docstring argues why they
are two tables rather than one with a kind column, and this migration builds what that module
declares.

**The capability array is the permission and the constraint is not redundant with the
model.** `brain.memory.formation.Formation` refuses to construct a memory naming no
capability, because such a memory is recalled by everybody: a reader trivially covers an empty
requirement. A row can arrive here from a backfill, from a psql session or from a later
migration, none of which construct a `Formation`, so the rule is written into the table as
well.

**SELECT and INSERT only, on both tables, and the omissions are the decision.** No UPDATE
means a memory cannot become more confident or wider in scope than it was written. No DELETE
means the record of what the system believed survives the system changing its mind, which is
what makes a person's question about why it changed answerable. M16.4.2 asks for supersession
on contradiction, marked rather than deleted, and whether marking is an UPDATE grant or an
append-only superseding row belongs to that leaf. This repository has chosen append-only every
time the question has come up.

**Row-level security on both, with policies matching the grants exactly.** Two policies per
table and no `FOR ALL`, so PostgreSQL denies what no policy admits, and 0001 leaves no role in
this system able to bypass it.

The `USING (true)` in the read policies is not an absence of a permission check. Recall is
decided in `brain.memory.formation.may_recall`, against the reader's live entitlements, and a
predicate here would be a second and different implementation of the same rule reading columns
this table does not have: the scope is a jsonb predicate rather than a department string, and
evaluating it in SQL would mean a second scope evaluator beside `Scope.matches`.

Task ids: M16.1.2, M16.1.3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

#: The role every grant below names. Asserted against the statements rather than trusted, the
#: way 0017 does, which keeps the constant honest rather than decorative.
APP_ROLE = "brain_app"

TABLES: tuple[str, ...] = ("mem.persistent", "mem.adaptive")

#: How many capability tags one memory may carry. A copy of
#: `brain.tables.memory.MAX_CAPABILITY_TAGS`, following the convention every migration here
#: keeps: a migration describes the schema at a moment, and one that imported a constant would
#: silently change meaning the day the constant changed.
MAX_CAPABILITY_TAGS = 32

#: The width of one capability tag, copied from what `Capability.value` admits.
CAPABILITY_TAG_CHARS = 200

TAGS_CHECK = (
    "array_length(capability_tags, 1) IS NOT NULL "
    f"AND array_length(capability_tags, 1) BETWEEN 1 AND {MAX_CAPABILITY_TAGS}"
)

RLS: tuple[str, ...] = (
    "ALTER TABLE mem.persistent ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mem.adaptive ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY persistent_readable ON mem.persistent
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY persistent_writable ON mem.persistent
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    """
    CREATE POLICY adaptive_readable ON mem.adaptive
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY adaptive_writable ON mem.adaptive
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
)

#: No UPDATE and no DELETE on either. A memory cannot be edited into something wider, and the
#: record of what the system believed survives it changing its mind.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT ON mem.persistent TO brain_app",
    "GRANT SELECT, INSERT ON mem.adaptive TO brain_app",
)


def _shared_columns() -> list[sa.Column[object]]:
    """The columns both tables carry, built once so the two cannot drift apart.

    A helper rather than two copies, and the reason is the specific failure two copies
    produce here: a width that differs between them means a memory promoted from one to the
    other is truncated on the way, and truncating a capability tag produces a requirement
    nobody holds rather than an error.
    """
    return [
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("statement", sa.String(4000), nullable=False),
        sa.Column(
            "capability_tags",
            postgresql.ARRAY(sa.String(CAPABILITY_TAG_CHARS)),
            nullable=False,
        ),
        sa.Column("scope", postgresql.JSONB, nullable=False),
        sa.Column("ent_hash", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("formed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def _shared_constraints() -> list[sa.schema.SchemaItem]:
    return [
        sa.CheckConstraint("length(btrim(principal_id)) > 0", name="principal_present"),
        sa.CheckConstraint("length(btrim(statement)) > 0", name="statement_present"),
        sa.CheckConstraint("length(btrim(ent_hash)) > 0", name="ent_hash_present"),
        sa.CheckConstraint(TAGS_CHECK, name="tags_bounded"),
    ]


def upgrade() -> None:
    assert all(APP_ROLE in statement for statement in GRANTS)

    op.execute("CREATE SCHEMA IF NOT EXISTS mem")

    op.create_table(
        "persistent",
        *_shared_columns(),
        *_shared_constraints(),
        schema="mem",
    )
    op.create_index("ix_persistent_principal", "persistent", ["principal_id"], schema="mem")

    op.create_table(
        "adaptive",
        *_shared_columns(),
        sa.Column("formed_confidence", sa.Float(), nullable=False),
        *_shared_constraints(),
        # A share on both sides, and deliberately not the retrieval floor: a memory inferred
        # weakly may be recorded and never recalled, and a constraint at the floor would
        # refuse to store the evidence that something was inferred weakly.
        sa.CheckConstraint(
            "formed_confidence >= 0.0 AND formed_confidence <= 1.0", name="confidence_share"
        ),
        schema="mem",
    )
    op.create_index("ix_adaptive_principal", "adaptive", ["principal_id"], schema="mem")

    for statement in (*RLS, *GRANTS):
        op.execute(statement)


def downgrade() -> None:
    """Drop both tables and leave the schema.

    The schema is left because 0001 created it and this migration only created it again if
    something had removed it. Dropping it here would take out a namespace an earlier
    migration is responsible for, which is the kind of tidy-up that fails on the one
    installation where somebody added a table to it by hand.
    """
    op.drop_index("ix_adaptive_principal", table_name="adaptive", schema="mem")
    op.drop_table("adaptive", schema="mem")
    op.drop_index("ix_persistent_principal", table_name="persistent", schema="mem")
    op.drop_table("persistent", schema="mem")
