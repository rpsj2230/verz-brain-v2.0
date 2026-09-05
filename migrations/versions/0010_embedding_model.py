"""Record which model produced each vector, so a query cannot reach across a model change.

One column. No table, no data, no function.

**The failure this closes has no symptom, which is why it is worth a migration of its own.**
Changing the embedding model invalidates every stored vector, and nothing about that is
visible from a query: old and new vectors sit in the same column under the same index, every
distance between them is a number rather than an error, and the results come back confident
and wrong. Retrieval degrades instead of breaking, which is the failure mode nobody reports
because nobody can see it.

`brain.knowledge.embedding.corpus_identity` already refuses a corpus holding two models, and
it can only refuse rows a caller hands it. With the identity in the column, `vector_query`
conjoins it, so the statement itself cannot span a model change even when nothing consulted
the corpus check first. That is the same argument the scope predicate makes one line above it
in the same WHERE clause: a rule a caller has to remember is a rule that holds until the
second caller.

**Nullable, and left nullable deliberately.** Every vector today was written before this
column existed, so there is no honest value to backfill: the model that produced them is not
recorded anywhere, and inventing one would be asserting a fact nobody knows. A NULL therefore
means "unembedded, or embedded by something unrecorded", and both are handled the same way,
which is that the row cannot serve the vector leg. `corpus_identity` refuses a vector with no
model beside it rather than guessing, and the rebuild is what fills the column in.

**No index on it.** It is conjoined with a reach predicate and an `IS NOT NULL` in a query
whose selectivity comes from the HNSW scan, and one more equality over a low-cardinality
column adds a filter rather than a lookup. An index here would be maintained on every write
to buy nothing.

Task ids: M7.3.5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

#: No table is created. Named for symmetry with the migrations that do, and empty so the
#: model-versus-migration comparison does not look for one.
TABLES: tuple[str, ...] = ()

#: The columns this migration adds to a table an earlier one built. Read by
#: `tests/unit/test_search.py`, which compares 0009's DDL against the current declaration and
#: has to know which columns arrived afterwards. Declared here rather than listed in the test,
#: so adding another column is one file to edit and the test needs no teaching.
ADDS_COLUMNS: tuple[str, ...] = ("embedding_model",)

SCHEMA = "know"
TABLE = "chunk"
COLUMN = "embedding_model"

#: Mirrors `brain.knowledge.search.EMBEDDING_MODEL_CHARS`. There is a test holding the two
#: equal rather than a comment asking somebody to remember.
MODEL_CHARS = 200


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.String(MODEL_CHARS), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the column.

    This loses which model produced each vector, and that is a real loss rather than a tidy
    reversal: after it the corpus cannot be told apart from one embedded by a single unknown
    model, and the next rebuild has nothing to compare against. It is still the right
    downgrade, because the alternative is a one-way migration for a column addition, and a
    schema somebody cannot reverse is a deploy somebody cannot roll back.
    """
    op.drop_column(TABLE, COLUMN, schema=SCHEMA)
