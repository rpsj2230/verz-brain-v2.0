"""The four tables entity resolution decides against, and the view that forwards a merge.

Storage for `brain.resolution.canonical`, which is the definition. Every width, grammar and
bound here is written from that layer's constants rather than retyped, so a change to the
domain breaks a test rather than a deploy.

**`er.canonical` carries no field values and that absence is the schema's main feature.** The
obvious design gives it a `name`, a `domain` and a `primary_contact`, refreshed from whichever
source is most trusted. Those columns would be a projection with no permission surface of its
own: readable by anybody who reaches any member, assembled from all of them, so a merge would
hand the readers of an open record the fields of a restricted one. There is nowhere on this
table to put such a value, and adding one is the regression this whole module is shaped
against. `brain.resolution.canonical.A_CANONICAL_ENTITY_HOLDS_NO_FIELDS` is the argument.

**Four tables rather than one wide one, because they have four different keys.** An entity is
keyed by its minted id. An alias is keyed by the observation: this name, on this source record.
An identifier is keyed by the assertion: this kind of key with this digest, on this source
record. A link is keyed by the source record alone, which is what makes one record belong to
exactly one entity. Collapsing any pair would mean one of those keys stops being enforced, and
the one that stops being enforced is always the link's: two links for one record are two
answers to "what is this part of", and whichever a query reads first is the answer.

**Every child row carries its own `(source, entity, source_id)`, and that is what makes a
merged view filterable.** After a merge an entity's aliases come from several records, and
which of them a reader may see is decided per row rather than per entity. A schema that hung
aliases off the entity alone could not express the filter at all.

**Those three columns are named exactly as `proj.record` names them**, including `entity`,
which sits confusingly close to `entity_id` in the same table. The alternative,
`source_entity`, reads better in isolation and was rejected: this triple's whole purpose is to
join to `proj.record`, and a join whose columns are named differently on the two sides is a
join somebody eventually writes wrong. The confusion is visible in one table; a mis-written
join is visible nowhere.

**There is deliberately no foreign key from these tables into `proj.record`.** A link is a
statement about a source record, and `proj.record` is a bounded cache of one: a record that is
federated rather than projected has no row there, and a key would make the resolution graph
depend on a cache having been filled. `gate.fast_path_rule` refuses the same key for the same
reason, and 0019 argues it.

**`merged_into` is a self-referencing foreign key, and that is the structural half of "an
issued id resolves forever".** A pointer into nothing would make an id unresolvable, and the
database is the only place that can refuse one: the constructor cannot see the other rows.
`brain.resolution.canonical.current_id` raises on a dangling pointer and says in as many words
that meeting one means the row arrived some other way.

**No DELETE anywhere.** An entity that stops being current is merged, not removed, because the
whole value of the forwarding pointer is that the old id still resolves; deleting the stub is
the one operation that breaks it. An alias and an identifier are observations, and an
observation made on 3 March was made.

**UPDATE on `er.canonical` and `er.link`, and not on the other two.** The pointer is written by
a merge and a record's membership can be corrected, which are the two things that legitimately
change. An observation cannot. Note what that UPDATE grant costs: correcting a link overwrites
the previous membership and nothing records what it was. That is M14.5.1's pre-image and it is
not built; `brain.resolution.canonical.NOTHING_HERE_RECORDS_WHO_MERGED_OR_ON_WHAT_EVIDENCE`
says the same about the merge itself.

**`er.resolved_alias` is a view rather than a table, and it is declared `security_invoker`.**
See `THE_VIEW_HAS_TO_RUN_AS_THE_CALLER`: a PostgreSQL view runs as its owner by default and
therefore bypasses row-level security on the tables underneath it, which would make a view the
one way to read rows the policies refuse. The declaration is in migration 0020, because a view
is not part of `Base.metadata` and nothing here can carry it.

Task ids: M14.1.1, M14.1.2, M14.1.3, M14.1.4, M14.1.5
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.envelope import OBJECT_NAME_PATTERN
from brain.db import Base
from brain.resolution.canonical import MAX_ALIAS_CHARS, EntityType, IdentifierKind
from brain.tables.identity import one_of

#: Why the resolved-alias view is declared `security_invoker`.
THE_VIEW_HAS_TO_RUN_AS_THE_CALLER = (
    "A PostgreSQL view executes with the privileges of its owner unless it says otherwise, "
    "and row-level security on the tables underneath is evaluated against that owner rather "
    "than against whoever is querying. A view created by a migration is owned by the "
    "migration's role, so an ordinary view over er.alias would be a route that reads every "
    "row the policies on er.alias refuse, and it would look like a convenience rather than "
    "like a hole. security_invoker = true, available since PostgreSQL 15, makes the policies "
    "apply to the caller. The policies on these tables are unconditional today, which is "
    "exactly why this is worth doing now: the day one of them grows a predicate, a view "
    "written without this would go on ignoring it and nothing would report that it had."
)

#: The width of a canonical id. The same as `proj.record.local_id`, which is where a resolved
#: id is written, and matched deliberately: a narrower column here would admit an id that
#: table can hold and this one cannot, and the join would then find nothing for exactly the
#: longest ids.
ENTITY_ID_CHARS = 128

#: The three parts of a source record's key, at `proj.record`'s widths for the same reason.
SOURCE_CHARS = 60
ENTITY_CHARS = 60
SOURCE_ID_CHARS = 200

#: Who or what minted an entity. `AuditMixin.created_by` is this wide, and a minting job's
#: name is the same kind of string as a principal id.
CREATED_BY_CHARS = 128

#: A sha256 hex digest. Sixty-four characters, and the constraint below pins the alphabet as
#: well as the width, which is what makes it impossible to store a raw email in the column.
DIGEST_CHARS = 64

#: The observed name form, at the domain's own bound rather than at a literal typed twice.
ALIAS_CHARS = MAX_ALIAS_CHARS

#: How wide an enum value column is. Sixteen holds every member of both closed vocabularies
#: with room to spare, and `one_of` is what actually restricts them.
ENUM_CHARS = 16

#: Sixty-four hex characters and nothing else. `brain.resolution.canonical.Identifier` refuses
#: anything else at construction and this refuses the row that came in through another door,
#: which is the split `proj.record` uses for its field cap and `auth.principal` for its
#: bounded engagement.
KEY_HASH_IS_A_DIGEST = f"key_hash ~ '^[0-9a-f]{{{DIGEST_CHARS}}}$'"

#: A merge to oneself is a one-hop cycle: following the pointer never reaches a survivor. It
#: is the only cycle a single row can see, and therefore the only one a check constraint can
#: refuse; longer ones are `current_id`'s to detect.
NOT_MERGED_INTO_ITSELF = "merged_into IS NULL OR merged_into <> entity_id"

#: The pointer and its timestamp agree, or the two halves of the system disagree about which
#: entities exist: one merged at no time reads as current to anything filtering on the
#: timestamp, and one merged into nothing at a time reads as merged to anything filtering on
#: the pointer.
MERGE_IS_STATED_ONCE = "(merged_into IS NULL) = (merged_at IS NULL)"

#: A share on both sides. A figure above one is not a probability and a negative one is a
#: belief nothing calibrated produces.
CONFIDENCE_IS_A_SHARE = "confidence >= 0.0 AND confidence <= 1.0"

#: The partial index predicate on the forwarding pointer, written once because the model and
#: the migration both state it and a predicate spelled two ways is two different indexes.
POINTER_IS_SET = "merged_into IS NOT NULL"


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


def _source_ref_constraints() -> tuple[CheckConstraint, ...]:
    """The grammar the three source columns share, matching `proj.record`'s own.

    A helper rather than three copies, and the failure three copies produce is specific: a
    grammar that differs between the tables admits a source name into one and refuses it in
    another, so a record has an alias and no link, and the family walk finds a name attached
    to nothing.
    """
    return (
        CheckConstraint(f"source ~ '{OBJECT_NAME_PATTERN}'", name="source_is_a_name"),
        CheckConstraint(f"entity ~ '{OBJECT_NAME_PATTERN}'", name="entity_is_a_name"),
        CheckConstraint(_present("source_id"), name="source_id_present"),
    )


def _points_at_a_canonical_entity() -> ForeignKeyConstraint:
    """`entity_id` names an entity that exists.

    A child row naming an entity nothing minted is a row no resolution will ever find, and it
    is silently invisible rather than loudly wrong: the family walk starts from entities, so
    an orphan alias is simply never gathered. The key turns that into a refused insert.
    """
    return ForeignKeyConstraint(["entity_id"], ["er.canonical.entity_id"])


class CanonicalEntityRow(Base):
    """`er.canonical` (M14.1.1). A minted id, what kind of thing it is, and where it came from.

    No name, no domain, no contact, no `fields`. See the module docstring.
    """

    __tablename__ = "canonical"

    #: Minted by the writer rather than by the database, for the reason `mem.persistent` gives
    #: about its own id: an entity is decided in the domain and stored afterwards, and an id
    #: assigned on INSERT would mean the object and the row disagree until the commit.
    entity_id: Mapped[str] = mapped_column(String(ENTITY_ID_CHARS), primary_key=True)

    #: Company, person or project. On the entity rather than inferred from the connector it
    #: was built from, because one source's records are not all one kind.
    entity_type: Mapped[str] = mapped_column(String(ENUM_CHARS), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: The principal or the job that minted it. A plain column rather than a key into
    #: `auth.principal`, because a backfill is not a person and an entity has to outlive the
    #: account of whoever ran one. `agent.agent` makes the same choice for the same reason.
    created_by: Mapped[str] = mapped_column(String(CREATED_BY_CHARS), nullable=False)

    #: The source record that first evidenced this entity. Provenance, and never handed to a
    #: reader: it names a record they may not reach. See
    #: `canonical.THE_VIEW_CARRIES_NOTHING_OFF_THE_CANONICAL_ROW_BUT_ITS_ID_AND_ITS_TYPE`.
    created_from_source: Mapped[str] = mapped_column(String(SOURCE_CHARS), nullable=False)
    created_from_entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), nullable=False)
    created_from_source_id: Mapped[str] = mapped_column(String(SOURCE_ID_CHARS), nullable=False)

    #: The forwarding pointer (M14.1.5). Null while this entity is the current one. A
    #: self-referencing key, so a pointer into nothing cannot be stored: an issued id that
    #: forwards to a row that is not there resolves to nothing at all.
    merged_into: Mapped[str | None] = mapped_column(String(ENTITY_ID_CHARS), nullable=True)

    #: When it stopped being current. Never who, and never on what evidence: see
    #: `canonical.NOTHING_HERE_RECORDS_WHO_MERGED_OR_ON_WHAT_EVIDENCE`.
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["merged_into"], ["er.canonical.entity_id"]),
        CheckConstraint(_present("entity_id"), name="entity_id_present"),
        CheckConstraint(one_of("entity_type", EntityType), name="entity_type_known"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        CheckConstraint(
            f"created_from_source ~ '{OBJECT_NAME_PATTERN}'", name="created_from_source_is_a_name"
        ),
        CheckConstraint(
            f"created_from_entity ~ '{OBJECT_NAME_PATTERN}'", name="created_from_entity_is_a_name"
        ),
        CheckConstraint(_present("created_from_source_id"), name="created_from_source_id_present"),
        CheckConstraint(NOT_MERGED_INTO_ITSELF, name="not_merged_into_itself"),
        CheckConstraint(MERGE_IS_STATED_ONCE, name="merge_is_stated_once"),
        # "Which entities were merged into this one" is the unmerge question and half of the
        # family walk, and it is the one access path the primary key does not serve. Partial,
        # because the overwhelming majority of rows carry a null here and an index entry for
        # each of them is a write cost paid on every mint for a query that never wants them.
        Index(
            "ix_canonical_merged_into",
            "merged_into",
            postgresql_where=text(POINTER_IS_SET),
        ),
        {"schema": "er"},
    )


class EntityAliasRow(Base):
    """`er.alias` (M14.1.2). One observed name form, with its source and when it was first seen.

    Keyed by the observation rather than by a surrogate: one source record asserting one name
    is one fact, and a surrogate would let the same fact be recorded twice with two first-seen
    dates, of which the later one is wrong.
    """

    __tablename__ = "alias"

    source: Mapped[str] = mapped_column(String(SOURCE_CHARS), primary_key=True)
    entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(SOURCE_ID_CHARS), primary_key=True)

    #: The observed form, verbatim, and part of the key. Not normalised: the normalisation
    #: that collapses "ACME PTE LTD" and "Acme Pte. Ltd." is M14.2's, and a table holding only
    #: normalised forms cannot be re-normalised when that changes.
    name: Mapped[str] = mapped_column(String(ALIAS_CHARS), primary_key=True)

    #: The entity this name was observed against. Never rewritten by a merge, which is why
    #: `er.resolved_alias` exists to forward it.
    entity_id: Mapped[str] = mapped_column(String(ENTITY_ID_CHARS), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        _points_at_a_canonical_entity(),
        *_source_ref_constraints(),
        CheckConstraint(_present("entity_id"), name="entity_id_present"),
        CheckConstraint(_present("name"), name="name_present"),
        # The family walk gathers every alias whose entity resolves to one survivor, so this
        # is the access path the observation key does not serve.
        Index("ix_alias_entity_id", "entity_id"),
        {"schema": "er"},
    )


class EntityIdentifierRow(Base):
    """`er.identifier` (M14.1.3). A hashed join key, its kind, and the record that asserted it.

    There is no column for the value and there is not going to be one. See
    `canonical.AN_IDENTIFIER_IS_A_HASH_AND_THERE_IS_NOWHERE_TO_PUT_THE_VALUE`.
    """

    __tablename__ = "identifier"

    source: Mapped[str] = mapped_column(String(SOURCE_CHARS), primary_key=True)
    entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(SOURCE_ID_CHARS), primary_key=True)

    #: Which kind of key this is. In the key because one record can assert several: a company
    #: has a UEN and a domain, and both are hashed with their kind in the material.
    kind: Mapped[str] = mapped_column(String(ENUM_CHARS), primary_key=True)

    #: `identifier_hash`'s output, and nothing that is not one. Sixty-four lowercase hex
    #: characters, so a raw address cannot be stored here by any route, including a
    #: hand-written INSERT during an incident.
    key_hash: Mapped[str] = mapped_column(String(DIGEST_CHARS), primary_key=True)

    entity_id: Mapped[str] = mapped_column(String(ENTITY_ID_CHARS), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        _points_at_a_canonical_entity(),
        *_source_ref_constraints(),
        CheckConstraint(_present("entity_id"), name="entity_id_present"),
        CheckConstraint(one_of("kind", IdentifierKind), name="kind_known"),
        CheckConstraint(KEY_HASH_IS_A_DIGEST, name="key_hash_is_a_digest"),
        # Stage one of the cascade asks "which entities carry this digest", per candidate
        # record. Both columns and in this order, because a digest is only meaningful with its
        # kind: the same query without the kind would join a phone number to a tax id the day
        # two peppered digests ever collided.
        Index("ix_identifier_kind_key_hash", "kind", "key_hash"),
        Index("ix_identifier_entity_id", "entity_id"),
        {"schema": "er"},
    )


class EntityLinkRow(Base):
    """`er.link` (M14.1.4). One source record mapped to one canonical entity, with confidence.

    Keyed by the source record alone, which is the whole rule: one record belongs to exactly
    one entity. Two rows would be two answers to "what is this part of" and whichever a query
    read first would win, silently, and differently on each run.
    """

    __tablename__ = "link"

    source: Mapped[str] = mapped_column(String(SOURCE_CHARS), primary_key=True)
    entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(SOURCE_ID_CHARS), primary_key=True)

    entity_id: Mapped[str] = mapped_column(String(ENTITY_ID_CHARS), nullable=False)

    #: What the cascade believed. Evidence about a match and never an input to reach: see
    #: `canonical.A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION`. It is on this row and on nothing
    #: a reader is handed.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        _points_at_a_canonical_entity(),
        *_source_ref_constraints(),
        CheckConstraint(_present("entity_id"), name="entity_id_present"),
        CheckConstraint(CONFIDENCE_IS_A_SHARE, name="confidence_is_a_share"),
        # "Which records are this entity's" is the membership query and the other half of the
        # family walk, and it is what the primary key cannot answer.
        Index("ix_link_entity_id", "entity_id"),
        {"schema": "er"},
    )


#: The name of the forwarding view (M14.1.6), so the migration, the tests and any later reader
#: spell it once. Not a table and therefore not on `Base.metadata`; migration 0020 creates it
#: and `tests/unit/test_resolution_tables.py` holds its SQL to the properties
#: `brain.resolution.canonical.resolved_aliases` has.
RESOLVED_ALIAS_VIEW = "er.resolved_alias"
