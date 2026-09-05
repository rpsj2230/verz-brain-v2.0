"""The document plane gets somewhere to live, and two indexes that answer different questions.

One table, four indexes, one policy. Everything interesting is in the four decisions below;
`brain.knowledge.search` carries the long-form argument for each and this file describes the
database it actually builds.

**The search column is generated, not written.** `tsv` is `GENERATED ALWAYS AS ... STORED`
rather than filled by the application or by a trigger, because either of those is a second
place the column can be forgotten and a chunk whose tsvector was never filled is invisible to
the lexical leg while looking perfectly indexed in every listing.

The expression has one workable spelling and the trap is worth knowing before somebody
tidies it. `to_tsvector('english', ...)` takes a `regconfig` and is IMMUTABLE, which is what
a stored generated column requires; the one-argument `to_tsvector(...)` reads
`default_text_search_config` and is only STABLE, so PostgreSQL refuses it there outright.
`unaccent()` is STABLE for the same kind of reason, so the obvious
`to_tsvector('english', unaccent(body))` is refused as well, and folding accents into an
index needs an immutable wrapper of the sort M14's normalisation leaf calls for. The `_tz`
suffix trap in 0008 is the same lesson about a different family of functions.

**The weights are three, not one.** Title at A, section heading at B, body at C. With
everything at A, `ts_rank_cd` cannot tell a chunk that is *about* a term from one that
mentions it once, and the title is the closest thing to a statement of aboutness a chunk
carries. Every input is wrapped in `coalesce`, because concatenating a tsvector with NULL
yields NULL and one missing section heading would empty the whole column for that row.

**HNSW rather than IVFFlat, and one index rather than one per scope (M15.2.3).** IVFFlat has
to be built after there are rows to cluster and rebuilt as the corpus grows, which makes
recall a function of when somebody last touched the index. HNSW is built on an empty table
and stays correct as rows arrive. A partial index per scope would be exact and would need
one index per department plus one per person, each maintained on every insert, with
onboarding becoming a DDL change; it also cannot serve a person who is in two departments,
because the planner will not union two graphs for one `ORDER BY ... LIMIT`. Iterative scan
serves every scope from this one index instead, and `brain.knowledge.search` states what
that costs.

Both vector and lexical indexes are partial on `deleted_at IS NULL`. Re-chunking a document
retires a document's worth of rows at once, and a retired chunk in an HNSW graph is a node
that every narrow query's iterative scan has to walk past.

**The policy is the second wall, and it applies to the worker too (M15.2.7).** The query
carries the reach predicate and is right today; the policy is what holds when a future query
is not, which is the one written during an incident or by a backfill next year. It cannot
read an `EntitlementSet`, so it reads the two facts a reach reduces to: `app.principal_id`,
which `chat.conversation`'s policy already uses and which is reused here rather than
duplicated under a second name, and `app.departments`, which is new.

There is one policy rather than a read policy beside a maintenance policy, and that is
deliberate. Permissive policies are OR-ed within a command, so a second `FOR ALL` policy
saying `deleted_at IS NULL` would defeat this one for SELECT completely. The consequence is
that an indexing worker sees a document only when it sets the same settings a request does,
which is the platform's own invariant rather than an inconvenience:
`E_run(caller, agent) = E(caller) ∩ ceiling` means there is no principal here with more
reach than a caller, and a worker re-chunking a document runs as that document's owner.

`WITH CHECK (true)` for the same reason 0002 gives on every soft-deleted table. Without an
explicit one, PostgreSQL reuses the USING expression against the new row, so marking a chunk
superseded would be refused by the very policy meant to stop it being read afterwards.

**`string_to_array(..., ',')` is sound because the column cannot hold a comma.**
`department_is_a_slug` pins the column to `brain.core.department.SLUG_PATTERN`, which admits
no comma, so a comma-separated session setting cannot be split into a department nobody
granted. The check constraint is load-bearing for the policy, not decoration beside it.

**No DELETE, and no grant to the fast lane.** A retired chunk is `deleted_at`, as everywhere
but 0006. The fast lane answers from the local projection without a model and has no
business in the document plane at all, so it is granted nothing here.

**The `know` schema is not created here and not dropped here.** 0001 created all nine and its
downgrade owns them, the same split 0008 draws for `proj`.

**Nothing imports `brain.knowledge.search`.** The definitions below are copies of what that
module declares, made deliberately so this migration goes on describing the database it
actually built. `tests/unit/test_search.py` compares the two on rendered DDL, which is what
stops the copy rotting in silence.

Task ids: M15.2.1, M15.2.2, M15.2.3, M15.2.7

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.types import UserDefinedType

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: One table. Listed as a tuple anyway, so `downgrade` walks it in reverse and the package
#: tuple can be compared against these end to end, which is the shape every migration uses.
TABLES: tuple[str, ...] = ("know.chunk",)

#: `brain.knowledge.search.EMBEDDING_DIMENSIONS`, copied. 1536 is what OpenAI's embedding
#: models produce natively or by supported truncation, and it is under pgvector's 2,000
#: dimension ceiling for an indexable vector. The larger model's native 3,072 would store
#: and would refuse to index, which is discovered after there is a corpus to re-embed.
EMBEDDING_DIMENSIONS = 1536

#: `brain.knowledge.search.WEIGHTED_TSVECTOR`, copied. See the module docstring for why the
#: two-argument `to_tsvector` and the `coalesce` on every input are both load-bearing.
WEIGHTED_TSVECTOR = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(section, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'C')"
)

#: `brain.knowledge.item.ITEM_ID_PATTERN`, which a chunk id is held to because it ends up
#: inside a citation.
REFERENCE_PATTERN = "^[A-Za-z0-9_.@-]{1,128}$"

#: `brain.core.department.SLUG_PATTERN` with its non-capturing group made an ordinary one.
#: The colon in `(?:` is read as a bind parameter by SQLAlchemy and rendered as NULL, so the
#: constraint that would ship is a different regex that still looks like the right one.
SLUG_PATTERN = "^[a-z][a-z0-9]*(_[a-z0-9]+)*$"

#: `brain.knowledge.chunking.BlockKind`, `brain.knowledge.visibility.Visibility` and
#: `brain.knowledge.item.KnowledgeState`, copied rather than imported for the reason 0004
#: and 0006 give. Alphabetical, matching how `one_of` renders, so the model and the
#: migration compare equal as text rather than only as meaning.
KIND_IN = "kind IN ('prose', 'table')"
VISIBILITY_IN = "visibility IN ('company', 'department', 'personal')"
STATE_IN = "state IN ('archived', 'draft', 'published', 'superseded')"

#: `brain.knowledge.item.RETRIEVABLE_STATES`, as the policy spells it. A superseded document
#: must stop answering beside the version that replaced it, and a chunk built before the
#: supersession is still on the table when it happens.
RETRIEVABLE_IN_POLICY = "state IN ('draft', 'published')"

#: The session settings the policy reads. `app.principal_id` is 0005's, reused.
PRINCIPAL_SETTING = "app.principal_id"
DEPARTMENTS_SETTING = "app.departments"

#: Written out rather than assembled, as 0001 through 0008 are: nothing here interpolates a
#: value into a statement, and the policy body is the last place a reader should have to
#: resolve a constant to know what the database enforces.
#:
#: The branches are in the order `VISIBILITY_ORDER` declares, narrowest first, so the policy
#: and `brain.knowledge.search.reach_predicate` read alike side by side.
#:
#: `current_setting(..., true)` returns NULL rather than raising when nobody set it, and
#: every comparison against NULL is NULL rather than true, so an unidentified connection
#: sees only company-visibility chunks. That is the default direction rather than something
#: written, which is the property 0005 records about its own policy.
RLS: tuple[str, ...] = (
    "ALTER TABLE know.chunk ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY chunk_within_reach ON know.chunk
        FOR ALL TO brain_app
        USING (
            deleted_at IS NULL
            AND state IN ('draft', 'published')
            AND (state <> 'draft' OR owner_id = current_setting('app.principal_id', true))
            AND (
                visibility = 'personal'
                    AND owner_id = current_setting('app.principal_id', true)
                OR visibility = 'department'
                    AND department = ANY(
                        string_to_array(current_setting('app.departments', true), ',')
                    )
                OR visibility = 'company'
            )
        )
        WITH CHECK (true)
    """,
)

#: No DELETE, as everywhere but 0006, and nothing for the fast lane: it answers from the
#: local projection without a model and has no business in the document plane.
GRANTS: tuple[str, ...] = ("GRANT SELECT, INSERT, UPDATE ON know.chunk TO brain_app",)


class Vector(UserDefinedType[Any]):
    """pgvector's `vector`, declared here because the driver package is not a dependency.

    A copy of `brain.knowledge.search.Vector`, deliberately, for the reason every other
    constant in this file is copied: a migration describes the database it built. It renders
    the column specification and nothing else, which is all a migration needs of it.
    """

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: Any) -> str:
        return f"VECTOR({self.dimensions})"


def _create_chunk() -> None:
    op.create_table(
        "chunk",
        sa.Column("chunk_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("title", sa.String(300), nullable=False, server_default=""),
        sa.Column("section", sa.String(300), nullable=False, server_default=""),
        sa.Column("page", sa.Integer(), nullable=True),
        # `start` and `end` are the names on the domain type; `END` is a reserved word, so
        # both carry the prefix rather than being rendered quoted forever.
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # Left nullable because the expression cannot produce NULL: every input is
        # coalesced, so a NOT NULL here would be a constraint that can never fire.
        sa.Column("tsv", TSVECTOR(), sa.Computed(WEIGHTED_TSVECTOR, persisted=True)),
        # Nullable because embedding is asynchronous. A NOT NULL would make writing a chunk
        # depend on the embedding provider being reachable, so an outage there would stop
        # ingestion outright rather than delaying the vector leg.
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("department", sa.String(60), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
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
        # character-for-character what `CreateTable` produces from it. A comparison on
        # rendered SQL is sensitive to constraint order.
        sa.CheckConstraint(f"chunk_id ~ '{REFERENCE_PATTERN}'", name="chunk_id_is_a_reference"),
        sa.CheckConstraint(
            f"document_id ~ '{REFERENCE_PATTERN}'", name="document_id_is_a_reference"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_is_a_position"),
        sa.CheckConstraint("span_end > span_start", name="span_is_not_empty"),
        sa.CheckConstraint("length(btrim(owner_id)) > 0", name="owned"),
        sa.CheckConstraint(KIND_IN, name="kind"),
        sa.CheckConstraint(VISIBILITY_IN, name="visibility"),
        sa.CheckConstraint(STATE_IN, name="state"),
        # Load-bearing for the policy above, not decoration beside it: no comma can reach
        # this column, so splitting `app.departments` on a comma cannot invent a department.
        sa.CheckConstraint(
            f"department IS NULL OR department ~ '{SLUG_PATTERN}'", name="department_is_a_slug"
        ),
        # A department-visibility chunk with no department matches no branch of the reach
        # predicate, so it is invisible to everybody including its own team. That fails
        # closed, which is exactly why nobody would ever notice it.
        sa.CheckConstraint(
            "visibility <> 'department' OR department IS NOT NULL",
            name="a_department_chunk_names_its_department",
        ),
        schema="know",
    )
    # The lexical leg. Partial, because a retired chunk in a GIN index is postings nobody
    # may read, and re-chunking retires a document's worth at once.
    op.create_index(
        "ix_chunk_tsv",
        "chunk",
        ["tsv"],
        schema="know",
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # The vector leg. `m` and `ef_construction` are pgvector's defaults on purpose: there is
    # nothing here to measure a better pair against, and the knob that matters at query time
    # is `hnsw.ef_search`, which changes without rebuilding anything.
    op.create_index(
        "ix_chunk_embedding",
        "chunk",
        ["embedding"],
        schema="know",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # The reach predicate's own access path. `visibility` leads because every branch of the
    # disjunction constrains it, so each branch is a range scan rather than a filter.
    op.create_index(
        "ix_chunk_reach",
        "chunk",
        ["visibility", "department", "owner_id"],
        schema="know",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # A document's chunks in order: needed to assemble a passage's neighbours, and to retire
    # a whole document's chunks when it is re-chunked. There is deliberately no index on
    # `deleted_at` alone; it is in the partial predicate of all four.
    op.create_index(
        "ix_chunk_document",
        "chunk",
        ["document_id", "ordinal"],
        schema="know",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 through 0008 do; this keeps
    # the constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    assert any(APP_ROLE in statement for statement in RLS)
    # And the policy really does read both settings. A policy that lost one of them would
    # still be a policy, and it would be one that admits every department to everybody.
    assert all(setting in RLS[1] for setting in (PRINCIPAL_SETTING, DEPARTMENTS_SETTING))

    _create_chunk()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policy, the indexes and the table privileges belong to the table and go with it,
    # and this migration creates no function and no trigger, so dropping the table is the
    # whole reversal. `know` is not dropped: 0001 created it and 0001's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
