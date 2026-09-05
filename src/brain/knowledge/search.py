"""Retrieval over the document plane, with the permission predicate inside the query.

**The scope predicate goes in the WHERE clause, and the top-k is taken from what the caller
may already see.** That is the central claim of this module and everything else here is
arranged to make it true.

The cheaper design is to fetch the fifty most relevant chunks and then drop the ones the
caller may not read. It is one line shorter, it uses the index the same way, and it is
wrong twice over. Somebody with narrow permissions gets almost nothing back, because their
results were crowded out of the candidate set by documents they may not see: the fifty best
matches belong to the whole company, four of them are theirs, and the answer is built from
four passages instead of fifty. That is bad retrieval. What makes it a disclosure is the
second half. **A person who asks a reasonable question and receives nothing has learnt that
plenty exists and none of it is theirs.** The platform's central rule is that DENIED and
ABSENT are indistinguishable to a person, and post-filtering breaks it in both available
ways: it leaks by emptiness, and it leaks by the count, because the difference between what
was asked for and what came back is the number of things the asker is not allowed to see.

So `reach_predicate` is conjoined into the query before `LIMIT`, and the two candidate
queries below have no post-filtering step to add one to. `top_within_reach` states the same
ordering in Python, filter before take, so the property can be tested on a machine with no
PostgreSQL on it.

**The reach is a disjunction over three visibility levels, and that does not weaken the
conjunction-only scope grammar.** `brain.core.scope` refuses disjunction because two narrow
grants must never combine into a wider one, and that argument is about *grants*. The OR
here is over `brain.knowledge.visibility.VISIBILITY_ORDER`, a closed three-member
enumeration that nothing an administrator writes can extend, and each branch is separately
narrowed by an ordinary conjunctive scope. `_level_branch` matches on the enum with
`assert_never`, so a fourth level is a type error rather than a branch nobody wrote.

**The caller's own scope bounds the department branch and nothing else.** Company visibility
means everyone, so applying a departmental grant scope to it would hide the staff handbook
from every person whose grant is departmental, which is everybody: the document is readable
by design, is never found, and the answer is merely thin with nothing saying why. That is
the silent retrieval hole `brain.knowledge.chunking` is written against, arriving through
the query instead of through the chunker. Personal visibility means the owner, which the
grant scope has no business widening or narrowing either.

**A grant whose scope is not a department membership is refused, never trimmed.** The
document plane's reach has to be enumerable, because the second wall below can only carry a
list of names in a session setting. Reducing an arbitrary scope to "the departments it
admits" is where that goes wrong, and it goes wrong silently: a grant reading
`department = web AND owner_id = p_bob` admits Web, so the reduced query returns every Web
document rather than Bob's, and the dropped clause was the one doing the narrowing. A query
missing the clause that was narrowing it returns more rows and reads as better recall.
`reach_for` refuses such a grant rather than reducing it, for the reason
`brain.core.redaction.redact` refuses an opaque request it cannot honour.

---

**The vector index and the filter fight each other, and this is the decision (M15.2.3).**
An approximate index returns its own candidate set and a restrictive filter applied to that
set gives back very few rows: the filtered-ANN problem, and the same shape of failure as
post-filtering one layer down. Two answers exist and the leaf asks for one of them.

*Per-scope partial indexes* are exact. `CREATE INDEX ... WHERE department = 'web'` gives the
Web department a graph containing only rows it may see, so its top-k is full and no filter
runs at all. The cost is that the number of indexes is the number of distinct scopes. Nine
departments is nine HNSW indexes over the same column, each maintained on every insert; a
personal scope per member of staff would be another 126; onboarding a person or opening a
department becomes a DDL change; and rebuilding the knowledge base is that many index builds
rather than one. Worse, it does not compose: a person in two departments needs a union across
two graphs, and the planner will not do that for a single `ORDER BY ... LIMIT`, so the
scope that most needs the mechanism is the one it cannot serve. Partial indexes are the
right answer for a small, fixed, closed set of scopes, and this is not one.

*Iterative scan* is what is used here. pgvector keeps scanning the graph until enough rows
pass the filter, so one index serves every scope. It costs two things and both are stated
rather than assumed. Latency stops being bounded by `ef_search`: a very narrow caller walks
further into the graph, and in the limit that approaches a sequential scan, which is why
`hnsw.max_scan_tuples` is set. And when that bound is reached the leg returns *fewer than
asked for* rather than nothing and rather than something wrong, which is the near-empty set
arriving through the back door. `A_SHORT_LEG_IS_NOT_AN_EMPTY_KNOWLEDGE_BASE` says what must
never be done about it: a short vector leg is not evidence that nothing exists, the lexical
leg is unaffected by it, and reciprocal rank fusion consumes short lists natively.

`relaxed_order` rather than `strict_order`, because the exact distance order inside one leg
is not the final order: `brain.knowledge.fusion` reads positions and the two legs are
combined afterwards, so paying for a guarantee that is then discarded buys nothing.

---

**Row-level security is the second wall (M15.2.7), and both walls are needed for different
reasons.** The predicate in the query is right today and is written by whoever wrote the
query. The policy in the database is what holds when a future query is not: a maintenance
script, a backfill, a console feature, an aggregate somebody adds during an incident. This
repository already fails the build on a table without row-level security, and 0009 gives
`know.chunk` a policy that repeats the reach from the session settings `session_settings`
sets. The policy cannot read the caller's `EntitlementSet`, so it reads the two facts that
reach reduces to: who is asking, and which departments they are in.

The worker that writes chunks is subject to the same wall, and that is deliberate rather
than an oversight to fix with a second role. `E_run(caller, agent) = E(caller) ∩ ceiling`
is the invariant of the platform: an indexing worker that could see past the wall would be a
principal with more reach than any caller, and there is no such principal here. A worker
re-chunking a document runs as that document's owner and sets the same settings a request
does, through the same function.

---

**What is proved without a server and what is not.** Everything below compiles: the SQL is
rendered against the PostgreSQL dialect and asserted on, and the reach arithmetic is
ordinary Python. What needs a real PostgreSQL 18 with pgvector is that the DDL is accepted,
that the generated column is immutable enough to be stored, that the planner actually
reaches the GIN and HNSW indexes, that iterative scan behaves as described under a narrow
filter, and that the policy admits and refuses the rows it is written to. Those are
behaviours of a running server, and asserting them here would only assert that this file's
idea of PostgreSQL matches PostgreSQL.

**Where the table lives, and why it is not in `brain.tables`.** A mapped model belongs
there, registered in that package's `__init__` and in `TABLES_IN_DEPENDENCY_ORDER`, and
that registration is not this change's to make: `brain.tables` asserts that its tuple names
every table on `Base.metadata` and only those, so declaring one there without editing the
package would turn a real guard into a failing build. The declaration below is therefore on
a private `MetaData` carrying the same naming convention, which is what the queries compile
against and what `tests/unit/test_search.py` holds the migration to. Moving it into
`brain/tables/knowledge.py` is a move, not a rewrite.

Nothing here opens a connection, reads a clock or embeds anything. It builds statements.

Task ids: M15.2.1, M15.2.2, M15.2.3, M15.2.4, M15.2.6, M15.2.7
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, assert_never, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.sql import ColumnElement, Select
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.types import UserDefinedType

from brain.core.department import (
    DEPARTMENT_FIELD,
    SLUG_PATTERN,
    SLUG_RE,
    admits_department,
    membership_scope,
)
from brain.core.entitlement import Capability, EntitlementSet
from brain.core.scope import Op, Scope
from brain.core.scope_sql import ColumnLayout, compile_where
from brain.db import Base
from brain.knowledge.chunking import BlockKind
from brain.knowledge.fusion import RRF_K, Fused, Ranking, fuse
from brain.knowledge.item import ITEM_ID_PATTERN, RETRIEVABLE_STATES, KnowledgeState
from brain.knowledge.visibility import OWNER_FIELD, VISIBILITY_ORDER, Visibility


class SearchError(Exception):
    """A retrieval that would be built wrongly rather than answered wrongly.

    Outside the `brain.core.errors` taxonomy, like every other refusal in this package:
    those five outcomes describe an answer given to a person, and this describes a refusal
    to compile a query.
    """


# ------------------------------------------------------------- named reasons


#: The claim the module is arranged around. A constant rather than a comment, for the reason
#: `BOTH_LIMITS_APPLY` is one: the sentence is what survives the person who wrote it.
THE_PREDICATE_IS_INSIDE_THE_QUERY: Final = (
    "the scope predicate is conjoined before LIMIT, so the top-k is drawn from what the "
    "caller may already see; ranking first and filtering afterwards leaks by emptiness and "
    "leaks again by the difference between what was asked for and what came back"
)

#: What must never be inferred from a short vector leg.
A_SHORT_LEG_IS_NOT_AN_EMPTY_KNOWLEDGE_BASE: Final = (
    "iterative scan stops at hnsw.max_scan_tuples and returns fewer rows than asked for; "
    "that is a bound on the scan and never evidence that nothing exists, so it must not be "
    "reported to anybody as an absence"
)

#: The refusal that keeps a clause from being dropped on the way to a department list.
A_SCOPE_THAT_CANNOT_BE_REDUCED_IS_REFUSED_NOT_TRIMMED: Final = (
    "a knowledge grant whose scope tests anything but department membership is refused; "
    "reducing it to the departments it admits would drop every other clause, and a query "
    "missing the clause that was narrowing it returns more rows and reads as better recall"
)


# ------------------------------------------------------- the text side (M15.2.1)


#: The text search configuration, used by the stored column *and* by every query. One
#: constant because the two must agree: a GIN index built with `english` is never used by a
#: query asking with `simple`, and the symptom is a sequential scan that returns the right
#: answer slowly, which nothing fails on.
SEARCH_CONFIG: Final = "english"

if not re.fullmatch(r"[a-z][a-z_]*", SEARCH_CONFIG):  # pragma: no cover - a constant
    _msg = f"{SEARCH_CONFIG!r} is not a text search configuration name"
    raise SearchError(_msg)

#: The configuration as it appears in SQL. `to_tsvector('english', ...)` is immutable and
#: may therefore back a stored generated column; the one-argument `to_tsvector(...)` reads
#: `default_text_search_config` and is only stable, so PostgreSQL refuses it there. The same
#: trap has a sibling worth knowing about: `unaccent()` is stable too, so the obvious
#: `to_tsvector('english', unaccent(body))` is refused, and folding accents in an index
#: needs an immutable wrapper of the kind M14's normalisation leaf calls for.
REGCONFIG_SQL: Final = f"'{SEARCH_CONFIG}'"


def _weighted_tsvector(config_sql: str) -> str:
    """The stored search column: the title, the section heading and the body, weighted.

    Three weights rather than one, because with everything at `A` `ts_rank_cd` cannot tell a
    chunk that is *about* a term from one that mentions it once in passing, and a title is
    the closest thing to a statement of aboutness that a chunk carries. `coalesce` on every
    input because concatenating a tsvector with NULL yields NULL, which would leave the
    whole column empty for any chunk with no section heading, and an empty tsvector matches
    nothing while looking exactly like an indexed row.
    """
    parts = (("title", "A"), ("section", "B"), ("body", "C"))
    return " || ".join(
        f"setweight(to_tsvector({config_sql}, coalesce({column}, '')), '{weight}')"
        for column, weight in parts
    )


WEIGHTED_TSVECTOR: Final = _weighted_tsvector(REGCONFIG_SQL)


# ----------------------------------------------------- the vector side (M15.2.2)


#: pgvector will store a `vector` of up to 16,000 dimensions and will index one of at most
#: 2,000. That asymmetry is the trap: the column is created, the rows are inserted, and
#: `CREATE INDEX` is what fails, by which time there is a corpus to re-embed.
INDEXABLE_DIMENSION_CEILING: Final = 2000

#: What is embedded and stored. 1536 is `text-embedding-3-small`, and it is also
#: `text-embedding-3-large` asked for 1536 dimensions, which is a supported truncation of a
#: Matryoshka-trained model rather than a trick. The larger model's native 3072 was rejected
#: for one reason: it is above the ceiling above, so it cannot be indexed at all.
#:
#: The dimension is part of the column type, so changing the embedding model is a migration
#: and a full re-embed rather than a configuration change. That is the honest cost and it is
#: better paid loudly: PostgreSQL refuses a vector of the wrong width on insert, so a model
#: swapped underneath this fails at the first write instead of returning nonsense distances.
EMBEDDING_DIMENSIONS: Final = 1536

if EMBEDDING_DIMENSIONS > INDEXABLE_DIMENSION_CEILING:  # pragma: no cover - a constant
    _msg = (
        f"{EMBEDDING_DIMENSIONS} dimensions cannot carry an HNSW or IVFFlat index; "
        f"pgvector indexes at most {INDEXABLE_DIMENSION_CEILING}"
    )
    raise SearchError(_msg)


class Vector(UserDefinedType[Any]):
    """pgvector's `vector` type, declared here because the driver package is not installed.

    `pgvector` ships a SQLAlchemy type and is not a dependency of this project, and adding
    one to render six characters of DDL would put an extension's release cadence on the
    critical path of every migration. What is needed is the column specification and a cast
    target; both are below.
    """

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        if dimensions < 1:
            msg = f"{dimensions} is not a vector width"
            raise SearchError(msg)
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: Any) -> str:
        return f"VECTOR({self.dimensions})"


def to_vector_literal(embedding: Sequence[float]) -> str:
    """pgvector's text form, `[0.1,0.2,...]`, which every driver can already send.

    Bound as text and cast in the statement rather than adapted by the driver, so that a
    deployment without pgvector's Python package registered on the connection still sends a
    value the server understands. The width is checked here because the alternative is a
    server-side error naming two integers and no column.
    """
    if len(embedding) != EMBEDDING_DIMENSIONS:
        msg = (
            f"an embedding of {len(embedding)} dimensions cannot be compared against a "
            f"column of {EMBEDDING_DIMENSIONS}; the column's width is the model's"
        )
        raise SearchError(msg)
    return "[" + ",".join(repr(float(value)) for value in embedding) + "]"


# --------------------------------------------------------------- the table


#: Python's non-capturing group. PostgreSQL understands it; SQLAlchemy does not survive it.
_NON_CAPTURING_GROUP: Final = re.compile(r"\(\?:")


def _posix_pattern(pattern: str) -> str:
    """A Python regex as something that can safely be put inside a check constraint.

    **This is a silent failure and it was found by rendering the DDL rather than by reading
    the code.** `CheckConstraint("department ~ '...'")` wraps its argument in
    `sqlalchemy.text`, which reads `:name` as a bind parameter, and
    `brain.core.department.SLUG_PATTERN` contains `(?:_[a-z0-9]+)`. The `:_` was taken for a
    parameter called `_`, bound to nothing, and rendered as the word NULL, so the constraint
    that shipped read `(?NULL[a-z0-9]+)`. It looks like a regex, it is a different regex,
    and nothing about the model or the migration says so.

    Turning the non-capturing group into an ordinary one removes the colon and matches
    identically, which is the same mechanical treatment `brain.tables.gate._posix` gives
    Python's named groups and for a related reason. The two refusals below are the guard on
    the guard: a colon surviving in the pattern would be eaten again, and any other `(?`
    construct is Python-only and would be refused by PostgreSQL when the migration runs.
    Both are raised at import, so neither can reach a database.
    """
    posix = _NON_CAPTURING_GROUP.sub("(", pattern)
    if ":" in posix:
        msg = (
            f"{pattern!r} still carries a colon after conversion; SQLAlchemy reads it as a "
            "bind parameter and renders NULL into the constraint"
        )
        raise SearchError(msg)
    if "(?" in posix:
        msg = f"{pattern!r} carries a construct PostgreSQL's regex engine does not have"
        raise SearchError(msg)
    return posix


#: The two grammars this table writes into check constraints, as PostgreSQL reads them.
REFERENCE_SQL_PATTERN: Final = _posix_pattern(ITEM_ID_PATTERN)
SLUG_SQL_PATTERN: Final = _posix_pattern(SLUG_PATTERN)


def _one_of(column: str, values: Iterable[str]) -> str:
    """An `IN` predicate generated from a closed vocabulary.

    The same helper `brain.tables.identity.one_of` is, restated rather than imported so that
    a knowledge module does not pull the whole table package, and every model in it, onto
    the metadata for four lines of string joining. `brain.knowledge.chunking` restates the
    reference grammar for the same kind of reason.
    """
    listed = ", ".join(f"'{value}'" for value in sorted(values))
    return f"{column} IN ({listed})"


#: The states retrieval may reach, as SQL. Generated from `RETRIEVABLE_STATES` so that a
#: fifth knowledge state cannot become retrievable by being added to an enum: it has to be
#: added to that frozenset, and then this constraint and the policy in 0009 disagree with
#: the database until a migration says otherwise.
RETRIEVABLE_STATE_VALUES: Final[tuple[str, ...]] = tuple(
    sorted(state.value for state in RETRIEVABLE_STATES)
)

CHUNK_ID_CHARS: Final = 128
DOCUMENT_ID_CHARS: Final = 128
OWNER_ID_CHARS: Final = 128
DEPARTMENT_CHARS: Final = 60
TITLE_CHARS: Final = 300
SECTION_CHARS: Final = 300
#: `name@revision:dimensions`, so wide enough for a provider's longest model name plus a
#: revision string. Bounded rather than `Text` because it is compared in a WHERE clause on
#: every vector query and an unbounded column there invites somebody to store a description.
EMBEDDING_MODEL_CHARS: Final = 200

#: The column recording which model produced a row's vector.
#:
#: Defined here rather than in `brain.knowledge.embedding`, which is where it started, because
#: that module imports this one: the table owns its own column names and the module reasoning
#: about models reads them. Putting it the other way round is a circular import, which is the
#: compiler telling you the layering is upside down.
EMBEDDING_MODEL_FIELD: Final = "embedding_model"

#: The shared registry, so `know.chunk` is a table this project knows it has.
#:
#: It was built on a private `MetaData` first, deliberately, because registering it without
#: also listing it in `brain.tables` turns two real guards into failing builds: one asserts
#: the metadata and the declared inventory are the same set, and the other that every table
#: module is imported by the package. Both are worth keeping, so the table waited here until
#: the inventory was updated to match rather than being hidden from the check.
#:
#: The cost of leaving it private was concrete: `alembic revision --autogenerate` compares
#: against `Base.metadata`, so a table absent from it reads as a table the code no longer
#: wants, and the proposal would have been to drop it.
#:
#: The declaration stays in this module rather than moving to `brain/tables/` with the other
#: models, and that is a deviation worth naming. The column list, the generated tsvector and
#: the index strategy are the substance of this module's argument, and splitting the
#: reasoning from the thing it reasons about is how the two drift.
SEARCH_METADATA: Final = Base.metadata

CHUNK: Final = sa.Table(
    "chunk",
    SEARCH_METADATA,
    # The reference grammar bounds this, because a chunk id ends up inside a citation and a
    # citation nothing can resolve is a citation nobody checks.
    sa.Column("chunk_id", sa.String(CHUNK_ID_CHARS), primary_key=True, nullable=False),
    sa.Column("document_id", sa.String(DOCUMENT_ID_CHARS), nullable=False),
    # The chunk's position in its document, which survives a re-parse that moves every
    # character offset and is what makes "the passage after this one" answerable.
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("kind", sa.String(8), nullable=False),
    sa.Column("title", sa.String(TITLE_CHARS), nullable=False, server_default=""),
    sa.Column("section", sa.String(SECTION_CHARS), nullable=False, server_default=""),
    sa.Column("page", sa.Integer(), nullable=True),
    # `start` and `end` are the names on `brain.knowledge.chunking.Chunk`; `END` is a
    # reserved word in SQL and both would be rendered quoted, so the columns carry the
    # prefix and the mapping is stated here rather than discovered in a query.
    sa.Column("span_start", sa.Integer(), nullable=False),
    sa.Column("span_end", sa.Integer(), nullable=False),
    sa.Column("body", sa.Text(), nullable=False),
    # Stored rather than computed per query, and generated rather than written by the
    # application: a trigger or an application-side write is a second place the column can
    # be forgotten, and a chunk whose tsvector was never filled is invisible to the lexical
    # leg while looking indexed. Left nullable because the expression cannot produce NULL
    # anyway; a NOT NULL here would be a constraint that can never fire.
    sa.Column("tsv", TSVECTOR(), sa.Computed(WEIGHTED_TSVECTOR, persisted=True)),
    # Nullable, because embedding is asynchronous. A NOT NULL would make writing a chunk
    # depend on the embedding provider being reachable, so an outage there would stop
    # ingestion rather than delay the vector leg.
    sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
    # Which model produced the vector beside it, as `name@revision:dimensions`. Nullable for
    # the same reason the vector is, and always written with it: a vector whose model nobody
    # recorded cannot be compared with anything, because the distance between two models'
    # embeddings is a number with no meaning rather than an error.
    #
    # **This column is what makes the mixed-model guarantee the database's rather than a
    # caller's.** `brain.knowledge.embedding.corpus_identity` already refuses a corpus holding
    # two models, and it can only refuse rows somebody hands it. Changing the embedding model
    # is the failure with no symptom: old and new vectors sit in one column under one index,
    # every distance between them is meaningless, and retrieval degrades quietly rather than
    # breaking. Conjoining the identity in `vector_query` means a query cannot reach across a
    # model change even if a caller never consults the corpus check.
    sa.Column(EMBEDDING_MODEL_FIELD, sa.String(EMBEDDING_MODEL_CHARS), nullable=True),
    # The document's permissions, copied onto every chunk of it by `chunk_document` and
    # never recomputed here. These three columns are what the reach predicate tests, and the
    # first two take their names from the constants the scope builders use, which is what
    # `OWNER_FIELD` and `DEPARTMENT_FIELD` exist for: a predicate testing `owner` against a
    # column called `owner_id` matches nothing and reads as a permission problem.
    sa.Column(OWNER_FIELD, sa.String(OWNER_ID_CHARS), nullable=False),
    sa.Column(DEPARTMENT_FIELD, sa.String(DEPARTMENT_CHARS), nullable=True),
    sa.Column("visibility", sa.String(16), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    # The times come from the database's clock rather than the application's, for the reason
    # `brain.db.TimestampMixin` gives: a box running thirty containers has thirty clocks and
    # they drift, which makes anything ordered by application time subtly wrong.
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
    sa.CheckConstraint(f"chunk_id ~ '{REFERENCE_SQL_PATTERN}'", name="chunk_id_is_a_reference"),
    sa.CheckConstraint(
        f"document_id ~ '{REFERENCE_SQL_PATTERN}'", name="document_id_is_a_reference"
    ),
    sa.CheckConstraint("ordinal >= 0", name="ordinal_is_a_position"),
    sa.CheckConstraint("span_end > span_start", name="span_is_not_empty"),
    sa.CheckConstraint("length(btrim(owner_id)) > 0", name="owned"),
    sa.CheckConstraint(_one_of("kind", (kind.value for kind in BlockKind)), name="kind"),
    sa.CheckConstraint(
        _one_of("visibility", (level.value for level in Visibility)), name="visibility"
    ),
    sa.CheckConstraint(_one_of("state", (state.value for state in KnowledgeState)), name="state"),
    # This is what makes the row-level security policy's `string_to_array(..., ',')` sound.
    # A department slug cannot contain a comma, so a comma-separated session setting cannot
    # be split into a department that was never granted.
    sa.CheckConstraint(
        f"department IS NULL OR department ~ '{SLUG_SQL_PATTERN}'", name="department_is_a_slug"
    ),
    # A department-visibility chunk with no department matches no branch of the reach
    # predicate, so it is invisible to everybody including its own team. That fails closed,
    # which is the safe direction and exactly why nobody would ever notice it.
    sa.CheckConstraint(
        "visibility <> 'department' OR department IS NOT NULL",
        name="a_department_chunk_names_its_department",
    ),
    schema="know",
)

#: The lexical index. Partial, because a retired chunk in a GIN index is postings nobody may
#: read, and re-chunking a document retires a whole document's worth at once.
LEXICAL_INDEX: Final = sa.Index(
    "ix_chunk_tsv",
    CHUNK.c.tsv,
    postgresql_using="gin",
    postgresql_where=sa.text("deleted_at IS NULL"),
)

#: The vector index (M15.2.2). HNSW rather than IVFFlat: IVFFlat has to be built after there
#: are rows to cluster and has to be rebuilt as the corpus grows, which makes recall a
#: function of when the index was last touched. HNSW is built on an empty table and stays
#: correct as rows arrive, which is what a knowledge base that grows daily needs.
#:
#: `vector_cosine_ops` because the embeddings are normalised, so cosine and inner product
#: rank identically today and cosine stays right if a future model is not normalised. `m`
#: and `ef_construction` are left at pgvector's defaults deliberately: there is nothing on
#: this machine to measure a better pair against, and the knob that matters at query time is
#: `hnsw.ef_search`, which can be changed without rebuilding anything.
#:
#: Partial for the same reason as the lexical index, and it matters more here: a retired
#: chunk in the graph is a node iterative scan has to walk past on every narrow query.
VECTOR_INDEX: Final = sa.Index(
    "ix_chunk_embedding",
    CHUNK.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=sa.text("deleted_at IS NULL"),
)

#: The reach predicate's own access path. `visibility` leads because every branch of the
#: disjunction constrains it, so each branch is a range scan rather than a filter over the
#: whole table.
REACH_INDEX: Final = sa.Index(
    "ix_chunk_reach",
    CHUNK.c.visibility,
    CHUNK.c.department,
    CHUNK.c.owner_id,
    postgresql_where=sa.text("deleted_at IS NULL"),
)

#: A document's chunks in order. Needed twice: to assemble a passage's neighbours for an
#: answer, and to retire a whole document's chunks when it is re-chunked.
#:
#: There is deliberately no index on `deleted_at` alone. It appears in the partial predicate
#: of every index here, and a standalone one would serve only "what has been retired", which
#: is an operator's occasional question paid for on every insert of a job that inserts in a
#: loop. `brain.tables.projection` declines an index on `last_seen_at` for the same reason.
DOCUMENT_INDEX: Final = sa.Index(
    "ix_chunk_document",
    CHUNK.c.document_id,
    CHUNK.c.ordinal,
    postgresql_where=sa.text("deleted_at IS NULL"),
)

INDEXES: Final[tuple[sa.Index, ...]] = (
    LEXICAL_INDEX,
    VECTOR_INDEX,
    REACH_INDEX,
    DOCUMENT_INDEX,
)


# ---------------------------------------------------------------- the reach


#: What a caller must hold to reach the document plane at all. The same noun
#: `brain.knowledge.visibility.PROMOTION_CAPABILITY` uses, so one word covers the knowledge
#: layer rather than one per module.
KNOWLEDGE_READ: Final = Capability(value="read:knowledge")

#: The one column a knowledge scope tests. `reach_for` refuses a grant that tests anything
#: else, so this is the whole vocabulary rather than the part that happens to be handled.
SCOPE_COLUMNS: Final[frozenset[str]] = frozenset({DEPARTMENT_FIELD})

#: Where a scope's fields live on this table. The one admitted field is a real column, which
#: is what lets the department branch use `ix_chunk_reach`; the jsonb fallback in
#: `ColumnLayout` is unreachable here, because the only scope ever compiled is one
#: `membership_scope` built over `DEPARTMENT_FIELD`.
#:
#: The alias is the table's unqualified name, which is what PostgreSQL calls `know.chunk`
#: inside a query that did not rename it. Without it the fragment renders bare column names,
#: which is correct with one FROM and ambiguous the day somebody adds a join.
CHUNK_LAYOUT: Final = ColumnLayout(alias="chunk", promoted=frozenset(SCOPE_COLUMNS))

#: Bound parameter names. Fixed rather than generated so a test can assert on the compiled
#: parameter dictionary, which is how "the value is bound and never interpolated" is stated
#: as a property rather than as a habit.
PRINCIPAL_PARAM: Final = "principal_id"
QUESTION_PARAM: Final = "question"
EMBEDDING_PARAM: Final = "embedding"

#: The bound parameter carrying the model identity. Bound rather than interpolated for the
#: same reason every other value here is: it arrives from a corpus check and a value that
#: reaches SQL as text is a value somebody eventually builds with a format string.
MODEL_PARAM: Final = "embedding_model"
DEPARTMENT_PARAM_PREFIX: Final = "reach"

#: The session settings the row-level security policy in 0009 reads. `app.principal_id` is
#: already used by `chat.conversation`'s policy, so it is reused rather than duplicated
#: under a second name; the department list is new.
PRINCIPAL_SETTING: Final = "app.principal_id"
DEPARTMENTS_SETTING: Final = "app.departments"

#: The separator in `app.departments`, matching `string_to_array(..., ',')` in the policy.
#: Safe because `SLUG_PATTERN` admits no comma, which the table's own check constraint
#: enforces for the column being compared against it.
DEPARTMENT_SEPARATOR: Final = ","

#: pgvector's iterative scan (M15.2.3), and the bound that keeps it from becoming a
#: sequential scan for a narrow caller. See the module docstring for the decision and its
#: cost. Values are constants here rather than settings, because a scan bound tuned per
#: deployment is a recall cliff that moves between environments.
ITERATIVE_SCAN: Final[tuple[tuple[str, str], ...]] = (
    ("hnsw.iterative_scan", "relaxed_order"),
    ("hnsw.max_scan_tuples", "20000"),
)

#: How many candidates each leg returns before fusion. Fifty is the number the leaf's own
#: failure is described in terms of, and it is per leg rather than overall: the two legs
#: disagree by design, so halving each to keep a total would throw away the disagreement
#: that fusion exists to resolve.
CANDIDATE_DEPTH: Final = 50

#: A ceiling on the depth a caller may ask for. Without one the depth is a request parameter
#: and a large enough value turns each leg into a scan with a sort on top. The fused list is
#: truncated to a page anyway, so a depth beyond this buys nothing that is ever shown.
MAX_CANDIDATE_DEPTH: Final = 500

LEXICAL_RETRIEVER: Final = "lexical"
VECTOR_RETRIEVER: Final = "vector"


@dataclass(frozen=True)
class Reach:
    """What one caller may already see in the document plane.

    Two fields, and the second one is a tuple of department names rather than a `Scope` on
    purpose. The department branch has to be expressed twice, once as SQL for the query and
    once as a comma-separated session setting for the policy that is the second wall, and
    two representations of one fact are two things to keep in step. A list of names renders
    to both without either being derived from the other's SQL.

    The scope machinery is still what compiles it: `membership_scope` builds the same
    predicate `brain.core.department` builds for a person in several departments, and
    `compile_where` turns that into SQL with the LIKE escaping and the IN-versus-string trap
    already handled. Nothing here writes a comparison by hand.

    An empty `departments` means the caller reaches no department at all, and it compiles to
    `false` rather than to a missing branch. That is the distinction
    `CrossDepartmentPlan.combined` draws by being `None` rather than unrestricted: a filter
    list that reduced to "no WHERE clause" is the most expensive bug available in this
    design.
    """

    principal_id: str
    departments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            msg = (
                "a reach needs the principal it belongs to; without one the personal branch "
                "compares owner_id against an empty string, which no chunk carries and every "
                "draft would then be invisible to its own author"
            )
            raise SearchError(msg)
        if len(set(self.departments)) != len(self.departments):
            msg = f"{self.principal_id!r} reaches a department twice: {list(self.departments)}"
            raise SearchError(msg)
        for name in self.departments:
            if not SLUG_RE.match(name):
                msg = (
                    f"{name!r} is not a department slug; the second wall reads this list "
                    f"split on {DEPARTMENT_SEPARATOR!r}, so a name outside the grammar "
                    "would split into departments nobody granted"
                )
                raise SearchError(msg)

    @property
    def department_scope(self) -> Scope | None:
        """The predicate for the department branch, or None when there is no branch.

        None rather than `Scope()`, because `Scope()` is the *unrestricted* scope and would
        turn a caller who reaches no department into one who reaches every department. That
        is the same mistake `brain.knowledge.visibility.scope_for` refuses for a personal
        scope built without an owner.
        """
        if not self.departments:
            return None
        return membership_scope(self.departments, DEPARTMENT_FIELD)

    @property
    def departments_setting(self) -> str:
        """The value `app.departments` carries. Empty when the caller reaches none.

        There is no wildcard, deliberately. A caller who reaches every department is given
        every department's name, because a wildcard is one string that switches the second
        wall off and the string is written by application code that a bug can reach.
        """
        return DEPARTMENT_SEPARATOR.join(self.departments)

    def admits(self, row: Mapping[str, object]) -> bool:
        """Whether this caller may see one chunk row: the Python side of `reach_predicate`.

        Two evaluators of one rule, exactly as `brain.core.scope.Clause` has `matches` and
        `to_sql`, and for the same reason: the SQL runs against the whole table and the
        Python one is the only thing that can be tested at its own boundary on a machine
        with no PostgreSQL. Both are built from the same `Reach`, and the department branch
        below is `Scope.matches`, so the audited clause evaluator is not reimplemented here.
        """
        if row.get("deleted_at") is not None:
            return False
        state = str(row.get("state", ""))
        if state not in RETRIEVABLE_STATE_VALUES:
            return False
        owner = str(row.get("owner_id", ""))
        caller_owns = bool(owner) and owner == self.principal_id
        if state == KnowledgeState.DRAFT.value and not caller_owns:
            # A draft is retrievable only by its owner, whatever its visibility says. A
            # department draft reachable by the department is somebody's unfinished note
            # answering a question in their colleague's name.
            return False
        try:
            level = Visibility(str(row.get("visibility", "")))
        except ValueError:
            return False
        scope = self.department_scope
        match level:
            case Visibility.COMPANY:
                return True
            case Visibility.DEPARTMENT:
                return scope is not None and scope.matches(dict(row))
            case Visibility.PERSONAL:
                return caller_owns


def reach_for(
    entitlement: EntitlementSet,
    *,
    departments: Sequence[str],
    now: datetime | None = None,
) -> Reach | None:
    """The caller's reach, or None when they hold no read of the knowledge plane at all.

    None is not the unrestricted reach and there is no way to confuse the two: `Reach` has
    no constructor that means everything, and a caller with no grant produces no `Reach` to
    build a query from. `EntitlementSet.scope_for` already returns None for an expired
    principal, so expiry arrives here as absence rather than as a separate check somebody
    could forget.

    `departments` is the registry, and passing it here is correct in a way it is not in
    `plan_cross_department`, which warns against exactly that. That function reports a gap
    per department the asker cannot reach, so the registry would turn a refusal into an
    inventory of the org chart. Nothing is reported here: the intersection is computed and
    the departments outside it are simply not in the query, which is the whole point.

    A grant whose scope is not a department membership is refused rather than reduced. See
    `_assert_reducible_to_departments`, which is the guard that argument lives in.
    """
    scope = entitlement.scope_for(KNOWLEDGE_READ, now)
    if scope is None:
        return None
    _assert_reducible_to_departments(scope)
    # `admits_department` asks it as a satisfiability question rather than by looking for a
    # department clause, which is what makes an unrestricted grant reach every department
    # instead of none: an unrestricted scope never mentions the field, and a test that
    # looked for the clause would report the opposite of the truth for it.
    reachable = tuple(name for name in dict.fromkeys(departments) if admits_department(scope, name))
    return Reach(principal_id=entitlement.principal_id, departments=reachable)


def _assert_reducible_to_departments(scope: Scope) -> None:
    """Refuse a grant scope that cannot become a list of department names without loss.

    See `A_SCOPE_THAT_CANNOT_BE_REDUCED_IS_REFUSED_NOT_TRIMMED` for what it costs to get
    this wrong. The alternative that has to be named to be rejected is compiling the whole
    scope into the query and leaving the second wall to check liveness only: that keeps
    every grant usable and turns row-level security back into the thing it is on every other
    table, which is not a second wall for reach at all.

    An `ANY` clause is allowed through. It declares a field without testing it, so it admits
    every row and dropping it cannot widen anything.

    A `PREFIX` on the department field is refused with the rest, and it is the one that
    looks harmless. `department LIKE 'web%'` is a real predicate that a list of names could
    represent only by enumerating the registry, so the reduction would be a function of
    which departments existed on the day the query ran.
    """
    offending = sorted(
        f"{clause.field} {clause.op}"
        for clause in scope.clauses
        if clause.op is not Op.ANY
        and (clause.field != DEPARTMENT_FIELD or clause.op not in (Op.EQ, Op.IN))
    )
    if offending:
        msg = (
            f"this grant's scope tests {offending}, and the document plane's reach is a "
            f"membership of {DEPARTMENT_FIELD}; "
            f"{A_SCOPE_THAT_CANNOT_BE_REDUCED_IS_REFUSED_NOT_TRIMMED}"
        )
        raise SearchError(msg)


def _caller_owns(reach: Reach) -> ColumnElement[bool]:
    """`owner_id = :principal_id`, bound rather than rendered into the statement."""
    return CHUNK.c.owner_id == sa.bindparam(
        PRINCIPAL_PARAM, reach.principal_id, type_=sa.String(OWNER_ID_CHARS)
    )


def _department_branch(reach: Reach) -> ColumnElement[bool]:
    """The department membership test, compiled by `brain.core.scope_sql`.

    Not written as a SQLAlchemy `IN` by hand, and the reason is the module `compile_where`
    lives in: it is the audited place a scope becomes SQL, where the LIKE escaping, the
    bare-string-in-an-IN trap and the unsatisfiable case are already decided. A second
    compiler would be a second answer to "who can see this", and the wrong one is whichever
    the query happens to use.
    """
    scope = reach.department_scope
    if scope is None:
        return sa.false()
    compiled = compile_where(scope, CHUNK_LAYOUT, param_prefix=DEPARTMENT_PARAM_PREFIX)
    fragment = sa.text(compiled.where)
    if compiled.params:
        fragment = fragment.bindparams(**compiled.params)
    # SQLAlchemy types a `TextClause` as opaque SQL rather than as a boolean expression.
    # `compile_where` emits nothing but boolean fragments, and proving that structurally
    # would mean parsing the fragment, which buys nothing over the type it already has.
    return cast("ColumnElement[bool]", fragment)


def _level_branch(level: Visibility, reach: Reach) -> ColumnElement[bool]:
    """One branch of the visibility disjunction.

    `assert_never` on the tail is the guard: a fourth level added to `Visibility` without a
    branch here is a type error, rather than a level that silently matches nothing and hides
    every document at it from everybody.
    """
    at_level = CHUNK.c.visibility == level.value
    match level:
        case Visibility.COMPANY:
            return at_level
        case Visibility.DEPARTMENT:
            return sa.and_(at_level, _department_branch(reach))
        case Visibility.PERSONAL:
            return sa.and_(at_level, _caller_owns(reach))
    assert_never(level)


def reach_predicate(reach: Reach) -> ColumnElement[bool]:
    """Everything the caller may see, as one boolean expression (M15.2.6, M15.2.7).

    Four conjuncts and each one closes a different hole:

    Retired chunks are excluded, which is how a re-chunked document stops answering with the
    spans it had before the re-parse.

    Only `RETRIEVABLE_STATES` are reachable, which is how a superseded document stops
    answering beside the version that replaced it. `chunk_document` refuses to *build* a
    chunk for one, and this refuses to *read* the chunks of a document superseded after they
    were built, which is the ordinary case.

    A draft is reachable only by its owner, whatever its visibility says.

    And the visibility disjunction itself, one branch per level.
    """
    return sa.and_(
        CHUNK.c.deleted_at.is_(None),
        CHUNK.c.state.in_(RETRIEVABLE_STATE_VALUES),
        sa.or_(CHUNK.c.state != KnowledgeState.DRAFT.value, _caller_owns(reach)),
        sa.or_(*(_level_branch(level, reach) for level in VISIBILITY_ORDER)),
    )


# ---------------------------------------------------------------- the queries


def _depth(depth: int) -> int:
    if depth < 1:
        msg = f"a candidate depth of {depth} asks for no candidates"
        raise SearchError(msg)
    if depth > MAX_CANDIDATE_DEPTH:
        msg = (
            f"a candidate depth of {depth} is past the ceiling of {MAX_CANDIDATE_DEPTH}; "
            "beyond it each leg is a scan with a sort on top, for rows nothing ever shows"
        )
        raise SearchError(msg)
    return depth


def _tsquery(question: str) -> ColumnElement[Any]:
    """The query side of the lexical leg.

    `websearch_to_tsquery` rather than `to_tsquery`, which is the one that has to be argued
    for. `to_tsquery` raises a syntax error on ordinary punctuation, so a person asking
    "what is the client's SLA?" gets a 500 from an apostrophe. `websearch_to_tsquery` parses
    what a person types, including quoted phrases and a leading minus, and never raises.
    `plainto_tsquery` also never raises and drops the phrase and negation syntax on the
    floor, which is a worse answer to the same question.
    """
    return sa.func.websearch_to_tsquery(
        sa.literal_column(REGCONFIG_SQL),
        sa.bindparam(QUESTION_PARAM, question, type_=sa.Text),
    )


def lexical_query(question: str, *, reach: Reach, depth: int = CANDIDATE_DEPTH) -> Select[Any]:
    """The full-text leg (M15.2.1, M15.2.6).

    The reach predicate is conjoined into the same `WHERE` the `LIMIT` applies to, so the
    rows this returns are `depth` of the caller's own rows rather than the caller's share of
    `depth` of everybody's. There is nowhere in this function to add a filter afterwards,
    which is the point: see `THE_PREDICATE_IS_INSIDE_THE_QUERY`.

    Ordered by rank and then by id, so two identical requests return the same list. Without
    the second key, chunks of equal rank come back in whatever order the index reached them,
    and a fused ranking built on an unstable input is unstable in ways that look like a
    relevance problem.
    """
    query = _tsquery(question)
    relevance = sa.func.ts_rank_cd(CHUNK.c.tsv, query)
    return (
        sa.select(CHUNK.c.chunk_id, relevance.label("relevance"))
        .where(sa.and_(reach_predicate(reach), CHUNK.c.tsv.op("@@")(query)))
        .order_by(relevance.desc(), CHUNK.c.chunk_id.asc())
        .limit(_depth(depth))
    )


def vector_query(
    embedding: Sequence[float],
    *,
    reach: Reach,
    model: str,
    depth: int = CANDIDATE_DEPTH,
) -> Select[Any]:
    """The nearest-neighbour leg (M15.2.2, M15.2.3, M15.2.6).

    The same shape as the lexical leg and the same reason for it. `embedding IS NOT NULL` is
    conjoined rather than left to the sort: a NULL distance orders last in PostgreSQL, so
    without it a corpus with fewer embedded chunks than `depth` would return unembedded
    chunks at the tail with no distance at all, and they would be fused as though they had
    been ranked.

    This is the query iterative scan applies to. Run `iterative_scan_statements` in the same
    transaction, or a narrow caller gets whatever fraction of one `ef_search` window happens
    to pass the filter, which is the near-empty set this whole module is written against.
    """
    target = sa.cast(
        sa.bindparam(EMBEDDING_PARAM, to_vector_literal(embedding), type_=sa.Text),
        Vector(EMBEDDING_DIMENSIONS),
    )
    distance = CHUNK.c.embedding.op("<=>", return_type=sa.Float)(target)
    return (
        sa.select(CHUNK.c.chunk_id, distance.label("distance"))
        .where(
            sa.and_(
                reach_predicate(reach),
                CHUNK.c.embedding.is_not(None),
                # The model is a conjunct rather than a caller's responsibility. Two models'
                # embeddings occupy one column under one index and the distance between them
                # is a number with no meaning, so a query spanning a model change returns
                # confident nonsense rather than an error. `corpus_identity` refuses a mixed
                # corpus and can only refuse rows somebody hands it; this refuses them in the
                # statement, which is the same argument the scope predicate makes one line up.
                CHUNK.c[EMBEDDING_MODEL_FIELD] == sa.bindparam(MODEL_PARAM, model),
            )
        )
        .order_by(distance.asc(), CHUNK.c.chunk_id.asc())
        .limit(_depth(depth))
    )


# ------------------------------------------------------- the session's two walls


def _set_config(name: str, value: str) -> TextClause:
    """One session setting, with the value bound rather than pasted into the statement.

    `SET LOCAL x = :v` is a syntax error: `SET` takes no bind parameters at all, which is
    why so much code ends up interpolating into it. `set_config(name, value, true)` is the
    same operation as an ordinary function call and does take them, so nothing here builds
    SQL out of a value.

    `true` is `is_local`, so the setting lasts for the transaction. That is not a detail:
    PgBouncer runs in transaction mode here, so a session-lifetime `SET` can land on a
    connection that is then handed to somebody else, which 0005 records as the same class of
    trap that made `pg_advisory_lock` unusable in `brain.migrate`.
    """
    return sa.text("SELECT set_config(:name, :value, true)").bindparams(name=name, value=value)


def session_settings(reach: Reach) -> tuple[TextClause, ...]:
    """What the row-level security policy reads, for this caller (M15.2.7).

    Run in the same transaction as the query. A connection that does not run these sees
    nothing but company-visibility chunks, because `current_setting(..., true)` returns NULL
    when nobody set it and every comparison against NULL is NULL rather than true. That is
    the correct direction and it is the *default* direction rather than something written.
    """
    return (
        _set_config(PRINCIPAL_SETTING, reach.principal_id),
        _set_config(DEPARTMENTS_SETTING, reach.departments_setting),
    )


def iterative_scan_statements() -> tuple[TextClause, ...]:
    """Turn on iterative scan for this transaction (M15.2.3).

    Separate from `session_settings` because they answer to different things: those two are
    a permission fact about the caller and these two are a performance decision about the
    index. A deployment that had to change one must not have to think about the other.
    """
    return tuple(_set_config(name, value) for name, value in ITERATIVE_SCAN)


# --------------------------------------------------- the two orders (M15.2.4, M15.2.5)


def top_within_reach(
    rows: Sequence[Mapping[str, object]], *, reach: Reach, depth: int = CANDIDATE_DEPTH
) -> tuple[str, ...]:
    """The chunk ids one leg returns, given rows already in relevance order (M15.2.4).

    **Filter, then take.** This is what the SQL above means, written in Python because the
    property M15.2.4 names is a property of the *order* of the filter and the limit, and
    that is exactly the part that can be checked without a server. Reversing the two lines
    is the whole bug: `rows[:depth]` filtered afterwards gives a narrow caller a handful of
    results and gives everybody else fifty, and the difference between the two counts is
    the thing the asker is not allowed to learn.

    `itertools.islice` rather than a slice of a list, so a caller passing a long corpus
    stops admitting rows once it has enough rather than admitting all of them and throwing
    most away.
    """
    admitted = (row for row in rows if reach.admits(row))
    return tuple(str(row["chunk_id"]) for row in itertools.islice(admitted, _depth(depth)))


def hybrid(
    *,
    lexical: Sequence[str],
    vector: Sequence[str],
    limit: int,
    k: int = RRF_K,
) -> tuple[Fused, ...]:
    """Fuse the two legs and take the page (M15.2.5).

    Both inputs came from queries carrying `reach_predicate`, so every reference in either
    list is one the caller may already see and the fused top-k is in reach by construction.
    That is the sentence this function exists to make true: there is no filtering step here
    and nowhere to put one, because filtering at this point would be the post-filter the
    module is written against, moved one layer later.

    A short list from either leg is ordinary rather than exceptional. The lexical leg
    returns nothing when the question shares no term with the corpus; the vector leg returns
    fewer than asked for when iterative scan reaches its bound. Reciprocal rank fusion takes
    both in its stride, and `A_SHORT_LEG_IS_NOT_AN_EMPTY_KNOWLEDGE_BASE` is what must not be
    concluded from either.
    """
    if limit < 1:
        msg = f"a limit of {limit} asks for no results"
        raise SearchError(msg)
    fused = fuse(
        (
            Ranking.of(LEXICAL_RETRIEVER, lexical),
            Ranking.of(VECTOR_RETRIEVER, vector),
        ),
        k=k,
    )
    return fused[:limit]
