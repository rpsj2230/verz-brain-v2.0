"""The Projected tier gets somewhere to live, with the twelve-field cap written into it.

One table, no functions, no triggers and no rows. Everything interesting about it is what it
may hold, and the two constraints that keep it holding no more than that.

**The cap is enforced by the database, not only by the code that writes to it.**
`brain.core.projection.check_projection` counts fields at the boundary and
`brain.connectors.projection.ProjectedRecord` refuses a thirteenth at construction. Both are
our code, and the rows that get a table into trouble are the ones that did not come through
it: a seed script, a hand-written INSERT during an incident, a backfill somebody writes next
year against the table directly. `auth.principal` already carries the same rule twice for the
same reason, and its comment says it plainly: the type refuses it on the way in, this refuses
the row that arrived some other way.

Counting the keys of a jsonb object inside a check constraint has one workable spelling.
`jsonb_object_keys` is set-returning, so it needs a subquery, and a subquery is not allowed in
a check constraint at all. `jsonb_array_length(jsonb_path_query_array(fields, '$.keyvalue()'))`
is neither: it is one immutable function over another, which is exactly what a check
constraint may contain. The `_tz` variants of the jsonpath functions are only stable and
would be refused, which is worth knowing before somebody 'fixes' this by adding a suffix.

**The object check is not made redundant by the cap.** jsonpath runs in lax mode, where
`$.keyvalue()` applied to something that is not an object suppresses the structural error and
yields nothing. A scalar or an array in `fields` therefore counts as zero keys and passes a
cap that only counts. `fields_is_an_object` is what refuses it, in the same spirit as
`chat.message`'s `refs_is_an_array`.

**The fast lane is granted SELECT and nothing else.** 0001 created `brain_fastlane` with no
privileges beyond `USAGE ON SCHEMA proj` and said why: the fast lane answers without a model,
from the local projection only, and must be unable to reach anything else. This is the first
migration to put a table in `proj`, so it is the first that can hand that role a table, and
it hands it exactly one verb. The policy is separate from the grant on purpose: without a
policy naming the role, row-level security returns an empty table to it, and a fast lane that
silently answers from nothing looks identical to a fast lane answering from an empty database.

**No DELETE, and `deleted_at` instead.** A record that disappears from the source has to stop
being counted, and it also has to leave evidence that it once existed and when it stopped:
"was that ticket real, and when did it go" is asked after a wrong answer, not before one. The
one DELETE grant in this system belongs to `auth.directory_role_grant` and 0006 argues for it
there; nothing here needs it.

**The `proj` schema is not created here and not dropped here.** 0001 created all nine and its
downgrade owns them, the same split `auth` gets in 0006.

**Nothing imports `brain.tables`.** The check constraints below are the same predicates the
model declares, copied deliberately, so this migration goes on describing the database it
actually built rather than whatever the models say today. `tests/unit/test_projection.py`
compares the two on rendered DDL.

Task ids: M11.4.1

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"
FAST_ROLE = "brain_fastlane"

#: One table. Listed as a tuple anyway, so `downgrade` walks the same list in reverse and
#: `tests/unit/test_projection.py` can compare it against the package tuple's last slice,
#: which is the shape every migration here uses.
TABLES: tuple[str, ...] = ("proj.record",)

#: Twelve, copied from `brain.core.projection.MAX_PROJECTED_FIELDS` rather than imported, for
#: the reason 0004 and 0006 give: a migration describes the database it built. The model
#: generates the same text from the constant, and the two are compared on rendered DDL, so a
#: cap raised in Python without a migration fails a test rather than being quietly untrue in
#: the database.
FIELDS_WITHIN_THE_CAP = "jsonb_array_length(jsonb_path_query_array(fields, '$.keyvalue()')) <= 12"

#: `brain.core.envelope.OBJECT_NAME_PATTERN`, copied for the same reason.
SOURCE_IS_A_NAME = "source ~ '^[a-z][a-z0-9_]*$'"
ENTITY_IS_A_NAME = "entity ~ '^[a-z][a-z0-9_]*$'"

#: `deleted_at IS NULL` rather than anything per-caller. A projected row is company-wide data
#: and the narrowing that matters is the source's own visibility predicate, which lives in the
#: manifest and is evaluated against the live entitlement set at query time. A resolved copy
#: of it on the row is the failure the whole tier exists to avoid.
#:
#: Two policies, because two roles read this table for different reasons and a single policy
#: `TO brain_app` would leave `brain_fastlane` seeing nothing at all. Permissive policies are
#: OR-ed, and these name disjoint roles, so neither widens the other.
RLS: tuple[str, ...] = (
    "ALTER TABLE proj.record ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY record_live ON proj.record
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    """
    CREATE POLICY record_fastlane_read ON proj.record
        FOR SELECT TO brain_fastlane
        USING (deleted_at IS NULL)
    """,
)

#: No DELETE, as everywhere but 0006. The fast lane reads and can do nothing else.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE ON proj.record TO brain_app",
    "GRANT SELECT ON proj.record TO brain_fastlane",
)


def _create_record() -> None:
    op.create_table(
        "record",
        # The natural key, in three parts. A surrogate would let one source record be
        # projected twice, and the second row goes on serving the value it was written with
        # while the first is refreshed. See the model docstring for the full argument.
        sa.Column("source", sa.String(60), primary_key=True, nullable=False),
        sa.Column("entity", sa.String(60), primary_key=True, nullable=False),
        sa.Column("source_id", sa.String(200), primary_key=True, nullable=False),
        # The entity registry's id, null until resolution produces one. Deliberately outside
        # the key: a merge is a pointer move, and a merge that rewrote a primary key would be
        # a delete plus an insert, which reads in the ledger as the record being removed.
        sa.Column("local_id", sa.String(128), nullable=True),
        sa.Column(
            "fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # When the source last confirmed the record still says this, which is not when our
        # row last changed. An unchanged record that is re-confirmed moves this and leaves
        # `updated_at` alone, and staleness is computed from this one.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # Declared in the order the model declares them, so the rendered DDL is
        # character-for-character what `CreateTable` produces from the model. A comparison on
        # rendered SQL is sensitive to constraint order.
        sa.CheckConstraint(SOURCE_IS_A_NAME, name="source_is_a_name"),
        sa.CheckConstraint(ENTITY_IS_A_NAME, name="entity_is_a_name"),
        sa.CheckConstraint("length(btrim(source_id)) > 0", name="source_id_present"),
        sa.CheckConstraint(
            "local_id IS NULL OR length(btrim(local_id)) > 0", name="local_id_present_if_set"
        ),
        sa.CheckConstraint("jsonb_typeof(fields) = 'object'", name="fields_is_an_object"),
        sa.CheckConstraint(FIELDS_WITHIN_THE_CAP, name="fields_within_the_cap"),
        schema="proj",
    )
    # From `SoftDeleteMixin`, which declares `index=True`. Named by the metadata's `ix`
    # convention, which renders the schema and table into the name.
    op.create_index("ix_proj_record_deleted_at", "record", ["deleted_at"], schema="proj")
    # The join key. Federation resolves one company's records across sources by local id, and
    # this is the one access path the primary key does not already serve. Partial, because a
    # retired row is never the answer to "which records are this company's".
    op.create_index(
        "ix_record_local_id_live",
        "record",
        ["local_id"],
        schema="proj",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def upgrade() -> None:
    # The statements below name both roles literally, the way 0001 through 0006 do; this
    # keeps the constants honest rather than decorative.
    assert all(APP_ROLE in statement or FAST_ROLE in statement for statement in GRANTS)
    assert any(FAST_ROLE in statement for statement in RLS)

    _create_record()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policies, the indexes and the table privileges belong to the table and go with it,
    # and this migration creates no function and no trigger, so dropping the table is the
    # whole reversal. `proj` is not dropped: 0001 created it and 0001's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
