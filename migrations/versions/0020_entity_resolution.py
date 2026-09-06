"""The `er` schema stops being empty: four tables and the view that forwards a merge.

`er` is named in `brain.db.SCHEMAS` as "entity resolution: candidates, merges, pre-images" and
0001 created it. These are the first objects in it. `brain.tables.resolution` declares the four
tables and argues the shape; this builds what that module declares, and adds the one object it
cannot carry, because a view is not part of `Base.metadata`.

**The permission argument is the reason these are four narrow tables rather than one wide
one.** Merging two records merges two permission surfaces, and the way that goes wrong is a
canonical row carrying the best-known name, domain and contact for an entity: assembled from
every member, readable by anybody who reaches any one of them, so a merge hands the readers of
an open record the fields of a restricted one. `er.canonical` has no such column, every alias
and identifier carries the source record it was observed on, and which of them a reader may see
is therefore decided per row. `brain.resolution.canonical` makes the argument in full.

**`er.resolved_alias` is declared `WITH (security_invoker = true)`, and that is the load-bearing
clause in this file.** A PostgreSQL view runs with its owner's privileges by default, and
row-level security on the tables underneath is evaluated against that owner rather than against
whoever is querying. A view created by a migration is owned by the migration's role, so an
ordinary view over `er.alias` would be a documented route around every policy on `er.alias`,
looking for all the world like a convenience. The policies here are unconditional today, which
is exactly why the clause is worth writing now rather than when it first matters: the day one
of them grows a predicate, a view written without it goes on ignoring the predicate and nothing
reports that it has. `security_barrier` was considered and left off: it forbids pushing
user-supplied quals below the join, which costs plan quality on a view whose whole job is to be
joined to, and the leak it closes is about leaky operators rather than about policies.

**The view anchors on entities that are not merged and walks backwards.** That is not a
stylistic choice about recursive CTEs. Walking forwards from an alias would follow `merged_into`
into any cycle a corrupt pair of rows created and loop until the statement timed out, taking
every healthy entity's query with it. Anchoring on `merged_into IS NULL` means a cycle contains
no anchor, is unreachable, and contributes no rows: the corruption costs those aliases and
nothing else. `brain.resolution.canonical.current_id` raises on the same cycle, deliberately,
because a caller asking about one id asked a question with no true answer, and
`brain.resolution.canonical._survivor` documents the disagreement.

**No DELETE grant on any of the four.** An entity that stops being current is merged, never
removed: the whole value of the forwarding pointer is that an id issued before a merge still
resolves, and deleting the stub is the one operation that breaks it. An alias and an identifier
are observations, and an observation made on 3 March was made. PostgreSQL denies what no policy
admits and 0001 leaves no role able to bypass row-level security, so the missing DELETE policy
sits underneath the missing DELETE grant, as it does in 0018 and 0019.

**UPDATE on `er.canonical` and `er.link` only.** The forwarding pointer is written by a merge
and a record's membership can be corrected. An observation cannot be edited. Note the cost of
that link grant plainly: correcting a link overwrites the previous membership and nothing here
records what it was, exactly as nothing records who performed a merge or on what evidence.
M14.5.1 asks for the pre-image and M14.5.4 for the audit; neither is built, and this migration
does not pretend otherwise by adding columns nothing writes.

**Nothing is granted to `brain_fastlane`.** M6.1.3 is that the fast lane reaches projected
tables and nothing else, and `er` is not `proj`. Asserted below rather than left to this
paragraph, in the shape 0019 uses.

**Nothing imports `brain.tables`.** The predicates below are the same ones the models declare,
copied deliberately, so this file goes on describing the database it actually built rather than
whatever the models say next year. `tests/unit/test_resolution_tables.py` compares the two on
rendered DDL, which is what turns the copy into a check rather than a duplication.

Task ids: M14.1.1, M14.1.2, M14.1.3, M14.1.4, M14.1.5, M14.1.6

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"
FAST_ROLE = "brain_fastlane"

#: The four tables this migration builds, in the order it builds them: `er.canonical` first,
#: because the other three carry a foreign key into it. `downgrade` walks this in reverse.
TABLES: tuple[str, ...] = ("er.canonical", "er.alias", "er.identifier", "er.link")

#: The view, which is not a table and is dropped before them.
VIEW = "er.resolved_alias"

#: `brain.core.envelope.OBJECT_NAME_PATTERN`, copied for the reason 0004, 0006, 0008 and 0019
#: give: a migration describes the database it built.
NAME = "^[a-z][a-z0-9_]*$"

#: `brain.resolution.canonical.EntityType`, sorted the way `one_of` sorts it.
ENTITY_TYPE_IN = "entity_type IN ('company', 'person', 'project')"

#: `brain.resolution.canonical.IdentifierKind`, likewise.
KIND_IN = "kind IN ('domain', 'email', 'phone', 'tax_id', 'uen')"

#: A sha256 hex digest and nothing else. This one constraint is the whole of the privacy rule
#: for join keys: with it, a raw email address cannot be stored in `er.identifier` by any
#: route, including a hand-written INSERT during an incident. See
#: `brain.resolution.canonical.AN_IDENTIFIER_IS_A_HASH_AND_THERE_IS_NOWHERE_TO_PUT_THE_VALUE`.
KEY_HASH_IS_A_DIGEST = "key_hash ~ '^[0-9a-f]{64}$'"

#: A merge to oneself is a one-hop cycle, and the only cycle a single row can see.
NOT_MERGED_INTO_ITSELF = "merged_into IS NULL OR merged_into <> entity_id"

#: The pointer and its timestamp say the same thing, or two halves of the system disagree
#: about which entities are current.
MERGE_IS_STATED_ONCE = "(merged_into IS NULL) = (merged_at IS NULL)"

#: A share on both sides. Above one is not a probability; below zero is a belief nothing
#: calibrated produces.
CONFIDENCE_IS_A_SHARE = "confidence >= 0.0 AND confidence <= 1.0"

#: The partial index predicate on the forwarding pointer.
POINTER_IS_SET = "merged_into IS NOT NULL"

#: `brain.resolution.canonical.MAX_FORWARD_DEPTH`, copied. The two have to agree: the view
#: forwarding a chain the resolver calls corrupt would mean an alias resolving to an entity no
#: caller can reach through `current_id`.
MAX_FORWARD_DEPTH = 16

#: The forwarding view (M14.1.6).
#:
#: `security_invoker` so the policies on `er.alias` and `er.canonical` apply to whoever is
#: querying rather than to the role that created the view. The recursion anchors on entities
#: that are not merged and walks backwards along `merged_into`, so a cycle has no anchor, is
#: unreachable, and silently contributes no rows instead of looping. `observed_entity_id` is
#: kept beside the resolved one because "which entity was this name observed against" is the
#: unmerge question, and a view that dropped it would make the pointer's history unreadable.
RESOLVED_ALIAS_VIEW = f"""
CREATE VIEW er.resolved_alias WITH (security_invoker = true) AS
WITH RECURSIVE survivor(entity_id, current_id, depth) AS (
        SELECT c.entity_id, c.entity_id, 0
        FROM er.canonical c
        WHERE c.merged_into IS NULL
    UNION ALL
        SELECT c.entity_id, s.current_id, s.depth + 1
        FROM er.canonical c
        JOIN survivor s ON c.merged_into = s.entity_id
        WHERE s.depth < {MAX_FORWARD_DEPTH}
)
SELECT
    s.current_id AS entity_id,
    a.entity_id AS observed_entity_id,
    a.name AS name,
    a.source AS source,
    a.entity AS entity,
    a.source_id AS source_id,
    a.first_seen_at AS first_seen_at
FROM er.alias a
JOIN survivor s ON s.entity_id = a.entity_id
"""

RLS: tuple[str, ...] = (
    "ALTER TABLE er.canonical ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE er.alias ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE er.identifier ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE er.link ENABLE ROW LEVEL SECURITY",
    # `USING (true)` is not an absence of a permission check, for the reason 0018 gives about
    # `mem.persistent`. Which members of an entity a reader may see is decided in
    # `brain.resolution.canonical.resolved_view`, against the reader's live entitlements and
    # the field policy, one source record at a time. A predicate here would be a second and
    # different implementation of that rule, reading columns these tables do not have: the
    # reach question is about a projected record's fields, and these rows carry only its key.
    """
    CREATE POLICY canonical_readable ON er.canonical
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY canonical_writable ON er.canonical
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    # The merge. An UPDATE policy and no DELETE policy, because an entity that stops being
    # current is forwarded rather than removed.
    """
    CREATE POLICY canonical_mergeable ON er.canonical
        FOR UPDATE TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
    """
    CREATE POLICY alias_readable ON er.alias
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY alias_writable ON er.alias
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    """
    CREATE POLICY identifier_readable ON er.identifier
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY identifier_writable ON er.identifier
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    """
    CREATE POLICY link_readable ON er.link
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY link_writable ON er.link
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    # A record's membership can be corrected. See the docstring for what that costs.
    """
    CREATE POLICY link_correctable ON er.link
        FOR UPDATE TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

#: No DELETE on anything, as everywhere but 0006. No UPDATE on the two observation tables. And
#: nothing at all for `brain_fastlane`: see the docstring.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE ON er.canonical TO brain_app",
    "GRANT SELECT, INSERT ON er.alias TO brain_app",
    "GRANT SELECT, INSERT ON er.identifier TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON er.link TO brain_app",
    "GRANT SELECT ON er.resolved_alias TO brain_app",
)


def _source_ref_columns() -> list[sa.Column[str]]:
    """The three columns every child table carries, built once so the three cannot drift.

    A helper rather than three copies, and the failure three copies produce is specific: a
    width that differs between them truncates a source id on the way into one table and not
    another, and the two rows then describe different records while looking like one. Named
    exactly as `proj.record` names them, including `entity`, because this triple exists to
    join to that table and a join whose columns differ by name on the two sides is a join
    somebody eventually writes wrong.
    """
    return [
        sa.Column("source", sa.String(60), primary_key=True, nullable=False),
        sa.Column("entity", sa.String(60), primary_key=True, nullable=False),
        sa.Column("source_id", sa.String(200), primary_key=True, nullable=False),
    ]


def _source_ref_constraints() -> list[sa.schema.SchemaItem]:
    return [
        sa.CheckConstraint(f"source ~ '{NAME}'", name="source_is_a_name"),
        sa.CheckConstraint(f"entity ~ '{NAME}'", name="entity_is_a_name"),
        sa.CheckConstraint("length(btrim(source_id)) > 0", name="source_id_present"),
    ]


def _create_canonical() -> None:
    op.create_table(
        "canonical",
        # Minted by the writer, at `proj.record.local_id`'s width, because that column is
        # where a resolved id is written and a narrower one here would admit an id that
        # table can hold and this one cannot.
        sa.Column("entity_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Created provenance. `created_by` is the principal or the job that minted it, and
        # the three `created_from_*` columns are the source record that first evidenced it.
        # Never handed to a reader: the record they name may not be one this reader reaches.
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_from_source", sa.String(60), nullable=False),
        sa.Column("created_from_entity", sa.String(60), nullable=False),
        sa.Column("created_from_source_id", sa.String(200), nullable=False),
        # The forwarding pointer, and the moment it was set. Self-referencing, so a pointer
        # into nothing cannot be stored: an issued id that forwards to a row which is not
        # there resolves to nothing at all, and the constructor cannot see the other rows.
        sa.Column("merged_into", sa.String(128), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        # Declared in the order the model declares them, so the rendered DDL is
        # character-for-character what `CreateTable` produces from the model. A comparison on
        # rendered SQL is sensitive to constraint order.
        sa.ForeignKeyConstraint(["merged_into"], ["er.canonical.entity_id"]),
        sa.CheckConstraint("length(btrim(entity_id)) > 0", name="entity_id_present"),
        sa.CheckConstraint(ENTITY_TYPE_IN, name="entity_type_known"),
        sa.CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        sa.CheckConstraint(f"created_from_source ~ '{NAME}'", name="created_from_source_is_a_name"),
        sa.CheckConstraint(f"created_from_entity ~ '{NAME}'", name="created_from_entity_is_a_name"),
        sa.CheckConstraint(
            "length(btrim(created_from_source_id)) > 0", name="created_from_source_id_present"
        ),
        sa.CheckConstraint(NOT_MERGED_INTO_ITSELF, name="not_merged_into_itself"),
        sa.CheckConstraint(MERGE_IS_STATED_ONCE, name="merge_is_stated_once"),
        schema="er",
    )
    # Partial, because most rows carry a null here and an index entry for each of them is a
    # write cost paid on every mint for a query that never wants them.
    op.create_index(
        "ix_canonical_merged_into",
        "canonical",
        ["merged_into"],
        schema="er",
        postgresql_where=sa.text(POINTER_IS_SET),
    )


def _create_alias() -> None:
    op.create_table(
        "alias",
        *_source_ref_columns(),
        # The observed form, verbatim and part of the key: one source record asserting one
        # name is one fact, and a surrogate would let it be recorded twice with two
        # first-seen dates, the later of which is wrong. Bounded at 200 so the key stays
        # inside PostgreSQL's btree tuple limit; `brain.resolution.canonical.MAX_ALIAS_CHARS`
        # refuses a longer one before it reaches here.
        sa.Column("name", sa.String(200), primary_key=True, nullable=False),
        # Never rewritten by a merge, which is why `er.resolved_alias` exists to forward it.
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["er.canonical.entity_id"]),
        *_source_ref_constraints(),
        sa.CheckConstraint("length(btrim(entity_id)) > 0", name="entity_id_present"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_present"),
        schema="er",
    )
    op.create_index("ix_alias_entity_id", "alias", ["entity_id"], schema="er")


def _create_identifier() -> None:
    op.create_table(
        "identifier",
        *_source_ref_columns(),
        # In the key because one record can assert several: a company has a UEN and a domain,
        # and both are hashed with their kind in the material.
        sa.Column("kind", sa.String(16), primary_key=True, nullable=False),
        # The digest and nothing that is not one. There is no column for the value.
        sa.Column("key_hash", sa.String(64), primary_key=True, nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["er.canonical.entity_id"]),
        *_source_ref_constraints(),
        sa.CheckConstraint("length(btrim(entity_id)) > 0", name="entity_id_present"),
        sa.CheckConstraint(KIND_IN, name="kind_known"),
        sa.CheckConstraint(KEY_HASH_IS_A_DIGEST, name="key_hash_is_a_digest"),
        schema="er",
    )
    # Stage one of the cascade asks "which entities carry this digest", per candidate record.
    # Both columns, and in this order: a digest is only meaningful with its kind.
    op.create_index("ix_identifier_kind_key_hash", "identifier", ["kind", "key_hash"], schema="er")
    op.create_index("ix_identifier_entity_id", "identifier", ["entity_id"], schema="er")


def _create_link() -> None:
    op.create_table(
        "link",
        # Keyed by the source record alone, which is the whole rule: one record belongs to
        # exactly one entity. Two rows would be two answers to "what is this part of", and
        # whichever a query read first would win, silently and differently on each run.
        *_source_ref_columns(),
        sa.Column("entity_id", sa.String(128), nullable=False),
        # Evidence about a match and never an input to reach. It is on this row and on
        # nothing a reader is handed.
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["er.canonical.entity_id"]),
        *_source_ref_constraints(),
        sa.CheckConstraint("length(btrim(entity_id)) > 0", name="entity_id_present"),
        sa.CheckConstraint(CONFIDENCE_IS_A_SHARE, name="confidence_is_a_share"),
        schema="er",
    )
    op.create_index("ix_link_entity_id", "link", ["entity_id"], schema="er")


def upgrade() -> None:
    # The statements name the role literally, the way 0001 through 0019 do; this keeps the
    # constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    assert all("DELETE" not in statement for statement in GRANTS)
    # M6.1.3: the fast lane reaches projected tables and nothing else, and `er` is not `proj`.
    # `brain.ops.migration_policy` applies the same rule to every migration written after it;
    # this is the same assertion 0019 makes about itself.
    assert all(FAST_ROLE not in statement for statement in GRANTS + RLS)
    # The clause the view's whole safety rests on, asserted rather than left to the docstring:
    # without it the view reads past every policy on the tables underneath.
    assert "security_invoker = true" in RESOLVED_ALIAS_VIEW

    _create_canonical()
    _create_alias()
    _create_identifier()
    _create_link()

    op.execute(RESOLVED_ALIAS_VIEW)
    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop the view, then the four tables, and leave the schema.

    The view goes first because it depends on two of the tables. The policies, the indexes and
    the table privileges belong to the tables and go with them, and this migration creates no
    function and no trigger. `er` is not dropped: 0001 created all nine schemas and 0001's
    downgrade owns them.
    """
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
