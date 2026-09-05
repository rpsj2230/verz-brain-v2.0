"""`proj.record`: the bounded copy of somebody else's record, and when we last saw it.

The Projected tier is the one deliberate exception to "we do not sync connector data", and
it exists because pure federation is arithmetically impossible: a realistic question touches
five to twenty records, which is 15,000 to 60,000 calls a day against a Xero ceiling of
5,000 shared with every other integration the client runs. `brain.core.projection` says what
may be copied and how much of it; `brain.connectors.manifest` refuses a declaration that
would break those rules at review. This is where the copy actually lands, and it is the last
place the rules can be enforced against a row that arrived some other way.

Four decisions carry this table.

**The twelve-field cap is a column constraint, not a comment.** `check_projection` counts
the fields at the boundary and `ProjectedRecord` refuses a thirteenth at construction, and
both of those are our code. A migration, a seed, a hand-written INSERT during an incident and
a backfill somebody writes next year are not. The rule is worth having twice for exactly the
reason `auth.principal`'s bounded-engagement check exists twice: the constructor catches it
on the way in, and the constraint catches the row that came in through another door. A
projection that grows past the cap is not a fuller projection, it is a second copy of the
source system with its own retention, its own staleness and its own breach surface, and
nobody ever decides to build that. It arrives one useful field at a time.

**The key is `(source, entity, source_id)`, and each of the three is load-bearing.** That
triple is the entire content of "which record in which system", so it is the natural key and
there is no surrogate. A `uuid` id would let one source record be projected twice, and two
rows for one record is not a duplicate-row problem here: a refresh updates one of them, the
other goes on serving the value it was written with, and the fast lane filters, sorts and
counts across both. The count is then wrong with nothing anywhere reporting it, which is the
same argument `auth.directory_role_grant` makes about its own natural key.

`source` is in the key because record ids are the source's own namespace: Freshdesk company
42 and Xero contact 42 are different companies, and a key without the source merges them by
coincidence of integers, which is precisely the question entity resolution exists to answer
on evidence. `entity` is in it because ids are namespaced per entity kind inside one source
too: Freshdesk ticket 42 and Freshdesk company 42 both exist, and without `entity` the second
insert collides with the first and the loser is whichever the backfill reached second.

**`local_id` is deliberately not in the key.** The entity registry's id is the *answer* to
entity resolution rather than an input to it, and it moves: the architecture requires a merge
to be a pointer move rather than a source-record change, and a merge that had to rewrite a
primary key would be a delete plus an insert. That loses `created_at` and reads in the ledger
exactly like the record being removed and re-added. It is also null for a record that has
been fetched and not yet resolved, which is the ordinary state during a backfill, and a null
cannot sit in a primary key at all.

**`last_seen_at` is not `updated_at`, and the difference is what M11.4.9 is computed from.**
`updated_at` is when *our row* changed. `last_seen_at` is when the source last confirmed the
record still says this. A source confirming an unchanged record moves the second and not the
first, so an answer that derived staleness from `updated_at` would report a record confirmed
five minutes ago as a month old, and the fast lane would decline to serve a value that is
perfectly current. Deriving it the other way round is worse: with only `updated_at`, a record
nothing has confirmed for a fortnight reads as fresh for as long as nobody edits it.

**There is no visibility column**, and its absence is the same argument
`auth.directory_role_grant` makes about scope. The source's visibility predicate belongs to
`manifest.ProjectedEntity`, which is reviewed in this repository and evaluated against the
live entitlement set on every query. Copied onto the row it becomes a second answer that
nothing updates, and a predicate narrowed in the manifest would go on being served wide from
a row written months earlier. Storing a *resolved* list instead is the failure the whole tier
is designed against; `manifest._assert_predicate_is_not_an_acl` refuses it at review.

Rejected: one row per projected field, keyed `(source, entity, source_id, ordinal)` with
`ordinal` checked below twelve, which would make the cap true by construction with no
function call in a constraint. It also makes every fast-lane filter a self-join over twelve
rows per record, and the fast lane's entire purpose is filtering, sorting and counting inside
500ms. A cap enforced by a shape that destroys the thing being capped is not a better cap.

Rejected: a GIN index over `fields`. It indexes every key and every value in the column,
which is a second copy of the projection paid for on every insert of a job whose defining
property is that it inserts in a loop. The fast lane's filters are per entity kind, so the
index worth having is a per-entity expression index added when there is a measured query to
add it for, rather than a blanket one added on the day the table is created.

Task ids: M11.4.1
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.envelope import OBJECT_NAME_PATTERN
from brain.core.projection import MAX_PROJECTED_FIELDS
from brain.db import Base, SoftDeleteMixin, TimestampMixin

#: `brain.core.envelope.Entity.entity` is `Field(max_length=60)`, and a connector name is
#: matched by the same `OBJECT_NAME_PATTERN`. Both columns are that wide so a name the types
#: accept cannot be one the column truncates.
SOURCE_CHARS = 60
ENTITY_CHARS = 60

#: The source's own record id. `brain.connectors.contract._SELECTOR_RE` bounds a source's own
#: identifier at 200 characters, and a record id is the same kind of string arriving from the
#: same systems, so the two widths agree deliberately rather than by coincidence.
SOURCE_ID_CHARS = 200

#: The entity registry's id for whatever this record is part of. The same width as every
#: locally minted id in this system, `identity.PRINCIPAL_ID_CHARS`, restated rather than
#: imported because a company is not a principal and one width moving should not move both.
LOCAL_ID_CHARS = 128

#: How many keys the `fields` object holds, as SQL. Written once because two constraints and
#: two tests read it, and a count expressed differently in each is a count that disagrees
#: with itself. `jsonb_path_query_array` is immutable, which is what lets it appear in a
#: check constraint at all; the `_tz` variants are only stable and would be refused.
FIELD_COUNT_SQL = "jsonb_array_length(jsonb_path_query_array(fields, '$.keyvalue()'))"

#: The cap, in the database, generated from the same constant `check_projection` counts
#: against. Generated rather than written out for the reason `identity.one_of` gives: a
#: hand-copied number is a second definition that stops matching the first in silence.
FIELDS_WITHIN_THE_CAP = f"{FIELD_COUNT_SQL} <= {MAX_PROJECTED_FIELDS}"

#: Refuses an array or a scalar in `fields`. Not made redundant by the cap above: jsonpath
#: runs in lax mode, where `$.keyvalue()` applied to a non-object suppresses the structural
#: error and yields nothing, so a scalar would sail through a cap that counts zero keys.
FIELDS_IS_AN_OBJECT = "jsonb_typeof(fields) = 'object'"


class ProjectedRecordRow(TimestampMixin, SoftDeleteMixin, Base):
    """`proj.record`. One projected record: which source, which record, and how old (M11.4.1).

    Named `ProjectedRecordRow` rather than `ProjectedRecord` because
    `brain.connectors.projection.ProjectedRecord` is the constructed value that the cap is
    enforced on, and two classes one letter apart in sibling packages is an import somebody
    eventually gets wrong. The way you find out is that a projection reaches the database
    having skipped the constructor that refuses a thirteenth field.

    **Rows are retired, never removed.** A record that disappears from the source is a fact
    worth keeping: the fast lane must stop counting it, and "when did this stop existing over
    there" is the question asked afterwards. `deleted_at` does both, and the row-level
    security policy hides retired rows from every query that forgets to filter them.
    """

    __tablename__ = "record"

    #: The connector this came from, as `ConnectorManifest.name` spells it.
    source: Mapped[str] = mapped_column(String(SOURCE_CHARS), primary_key=True)

    #: The entity kind, as the manifest's `ProjectedEntity.entity` spells it. The redactor
    #: and the field policy are both looked up by this string.
    entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), primary_key=True)

    #: The source's own identifier, passed through and never parsed. Its shape is the
    #: source's business, and reading one here would make us wrong the day they change it.
    source_id: Mapped[str] = mapped_column(String(SOURCE_ID_CHARS), primary_key=True)

    #: The entity registry's id, once resolution has produced one. Null until then, which is
    #: the ordinary state of a row a backfill has just written. See the module docstring for
    #: why this is a column and not part of the key.
    local_id: Mapped[str | None] = mapped_column(String(LOCAL_ID_CHARS), nullable=True)

    #: The hot fields: ids, join keys, status enums, timestamps and at most one short label.
    #: At most twelve of them, checked below as well as at construction. An empty object is
    #: allowed and is the smallest useful projection: the identity, the join key and the age.
    fields: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    #: When the source last confirmed this record still says what the row says. Not
    #: `updated_at`: see the module docstring. This is the column
    #: `brain.connectors.projection` computes an age from, and the only evidence that the
    #: change signal is still delivering.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Text plus check constraints rather than any narrower column type, matching every
        # other table here: a name that the Python side accepts and the database refuses is
        # a failure at three in the morning rather than in a test.
        CheckConstraint(f"source ~ '{OBJECT_NAME_PATTERN}'", name="source_is_a_name"),
        CheckConstraint(f"entity ~ '{OBJECT_NAME_PATTERN}'", name="entity_is_a_name"),
        CheckConstraint("length(btrim(source_id)) > 0", name="source_id_present"),
        # Null is how "not resolved yet" is said. An empty string is how it gets said by
        # accident, and it would join to nothing while reading as resolved.
        CheckConstraint(
            "local_id IS NULL OR length(btrim(local_id)) > 0", name="local_id_present_if_set"
        ),
        CheckConstraint(FIELDS_IS_AN_OBJECT, name="fields_is_an_object"),
        CheckConstraint(FIELDS_WITHIN_THE_CAP, name="fields_within_the_cap"),
        # The join. Federation resolves one company's records across sources by local id, so
        # this is the one access path the primary key does not already serve. Partial,
        # because a retired row is never the answer to "which records are this company's".
        #
        # There is deliberately no index on `last_seen_at`. "What is stale" is an operator's
        # occasional question, and an index for it is a write cost paid on every insert of a
        # job that inserts in a loop.
        Index(
            "ix_record_local_id_live",
            "local_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "proj"},
    )
