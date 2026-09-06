"""Two tables: what somebody stated, and what the system inferred.

Storage for `brain.memory.formation`, which is the definition. Every width, grammar and
bound here is written from that layer's constants rather than retyped, so a change to the
domain breaks a test rather than a deploy.

**Two tables rather than one with a kind column, and the difference is not cosmetic.** A
persistent memory is something a person said, and it stays until something contradicts it. An
adaptive memory is something the system worked out, and it decays. Those are different
lifetimes and different evidence, and a single table would carry a nullable confidence that is
meaningless on half its rows, which is the column a later reader treats as zero. The kind is
still recorded on both, because `MemoryKind` names three and a session memory promoted into
either of these is a real operation that has to be visible in the row rather than inferred
from which table it turned up in.

**The capability tags are the permission and the array may not be empty.** A memory naming no
capability is recalled by everybody, because a reader trivially covers an empty requirement.
That is the most dangerous row either table can hold and it is refused twice: `Formation`
refuses to construct one, and a check constraint refuses to store one. The second is not
redundant with the first, because a row can arrive from a migration, a backfill or a
psql session, none of which construct a `Formation`.

**No confidence column on the persistent table.** The temptation is to give it one defaulting
to 1.0 so the two tables can share a query, and that would make every persistent memory decay
on the adaptive table's curve the first time somebody wrote one loop over both. What a person
stated does not become less true in thirty days; it becomes wrong when something contradicts
it, which is supersession and is a different mechanism.

**What is stored is the confidence at formation, never the decayed value.** A stored decayed
value is wrong the moment after it is written and needs a job to keep it approximately right,
and that job is a thing that can fail silently and leave memories more confident than they
should be. `confidence_now` computes it from `formed_confidence` and `formed_at` at read time,
so there is no stale number anywhere and nothing to keep up to date.

**SELECT and INSERT only, on both.** That is the same arrangement `agent.upgrade_decline`
makes, and it is deliberately a decision to revisit rather than a permanent one: M16.4.2 asks
for supersession on contradiction, marked rather than deleted, and marking is either an UPDATE
grant or an append-only row that supersedes an earlier one. This repository has reached for
append-only every time the question has come up, and the choice belongs to that leaf rather
than to this one. Until then a memory cannot be edited, which is the conservative direction:
nothing can quietly become more confident or wider in scope than it was written.

**No `updated_at`.** A row nothing may update carrying a column that says when it was last
updated is a column that tells a reader something untrue, which is the argument
`obs.audit_entry` and `agent.template_version` both make.

Task ids: M16.1.2, M16.1.3
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.entitlement import Capability
from brain.db import Base
from brain.memory.formation import RECALL_FLOOR


def _capability_width() -> int:
    """How wide a capability may be, read off `Capability` rather than retyped.

    The column has to hold whatever the model admits, and a literal here would be a second
    copy of a bound: right until somebody widens the model, and then a silent truncation on
    the way into the array. Reading the model's own constraint means the column moves with
    it, and a model that stopped declaring one fails here loudly rather than defaulting to
    something plausible.
    """
    for constraint in Capability.model_fields["value"].metadata:
        width = getattr(constraint, "max_length", None)
        if width is not None:
            return int(width)
    msg = (
        "Capability.value declares no maximum length, so nothing here can say how wide the "
        "column holding one has to be"
    )
    raise RuntimeError(msg)


#: The width of one capability tag, from the model that validates one.
CAPABILITY_TAG_CHARS = _capability_width()

#: How long a principal id may be. The same bound `auth.principal` uses, because these rows
#: name one and a wider column here would admit an id that table cannot hold.
PRINCIPAL_ID_CHARS = 64

#: The width of `EntitlementSet.ent_hash`, which is a truncated sha256 and is 32 characters.
#: Written from the domain rather than as a literal, so a change there fails a test.
ENT_HASH_CHARS = 32

#: How many capability tags one memory may carry.
#:
#: Bounded because the array is a requirement every reader is checked against, so a row with
#: a thousand tags is a row nobody can ever recall and a query that walks a thousand entries
#: to find that out. Generous against what a formation really holds: a memory formed while
#: somebody was reading a client record carries the handful of capabilities that record
#: needed.
MAX_CAPABILITY_TAGS = 32


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


def _tags_are_a_real_requirement(column: str) -> str:
    """The capability array is non-empty and bounded.

    Written as one constraint rather than two so the failure names the whole rule. A row
    failing either half is a row whose permission is not what anybody intended, and telling
    an operator which half is not what they need to know first.
    """
    return (
        f"array_length({column}, 1) IS NOT NULL "
        f"AND array_length({column}, 1) BETWEEN 1 AND {MAX_CAPABILITY_TAGS}"
    )


class PersistentMemoryRow(Base):
    """`mem.persistent`. Something a person stated, which stays until contradicted.

    No confidence and no decay. See the module docstring: what somebody said does not become
    less true with time, it becomes wrong when something contradicts it.
    """

    __tablename__ = "persistent"

    #: A ULID. Generated by the writer rather than by the database, because a memory is
    #: formed in the domain and stored afterwards, and an id assigned on INSERT would mean
    #: the object and the row disagree about what it is until the transaction commits.
    id: Mapped[str] = mapped_column(String(26), primary_key=True)

    #: Who was asking when it was formed. Recorded for the audit question and never used to
    #: permit a recall: `may_recall` asks what the reader reaches, not who wrote it.
    principal_id: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)

    #: What the memory is. Prompt material, stored and never parsed.
    statement: Mapped[str] = mapped_column(String(4000), nullable=False)

    #: The capabilities in play at formation. The requirement every reader is checked
    #: against, and the reason the check constraint below refuses an empty array.
    capability_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(CAPABILITY_TAG_CHARS)), nullable=False
    )

    #: Where it was formed, as the same jsonb predicate every other scope in this system is.
    scope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    #: `EntitlementSet.ent_hash` at formation. Recorded, never compared. See
    #: `brain.memory.formation.A_HASH_ANSWERS_A_DIFFERENT_QUESTION_FROM_THE_ONE_RECALL_ASKS`.
    ent_hash: Mapped[str] = mapped_column(String(ENT_HASH_CHARS), nullable=False)

    #: Which kind this row was written as. Recorded rather than inferred from the table, so
    #: a session memory promoted here says so.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    formed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_present("principal_id"), name="principal_present"),
        CheckConstraint(_present("statement"), name="statement_present"),
        CheckConstraint(_present("ent_hash"), name="ent_hash_present"),
        CheckConstraint(_tags_are_a_real_requirement("capability_tags"), name="tags_bounded"),
        Index("ix_persistent_principal", "principal_id"),
        {"schema": "mem"},
    )


class AdaptiveMemoryRow(Base):
    """`mem.adaptive`. Something the system inferred, which decays.

    Carries the confidence it was formed with and never the decayed value, so there is no
    stale number and no job to keep one right. See the module docstring.
    """

    __tablename__ = "adaptive"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)

    principal_id: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)

    #: What was inferred. Prompt material, stored and never parsed.
    statement: Mapped[str] = mapped_column(String(4000), nullable=False)

    capability_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(CAPABILITY_TAG_CHARS)), nullable=False
    )

    scope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    ent_hash: Mapped[str] = mapped_column(String(ENT_HASH_CHARS), nullable=False)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: What it was worth when it was formed. `confidence_now` decays this at read time.
    #:
    #: Bounded to a share on both sides. Above one is not a confidence, and below zero is a
    #: memory the system is certain is wrong, which is a different thing from one it has
    #: stopped believing and has no meaning on this curve.
    formed_confidence: Mapped[float] = mapped_column(Float, nullable=False)

    formed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_present("principal_id"), name="principal_present"),
        CheckConstraint(_present("statement"), name="statement_present"),
        CheckConstraint(_present("ent_hash"), name="ent_hash_present"),
        CheckConstraint(_tags_are_a_real_requirement("capability_tags"), name="tags_bounded"),
        # A share, on both sides. The floor a memory is retrieved above is
        # `RECALL_FLOOR` and is deliberately not this constraint: a memory may be formed
        # below the retrieval floor and never be recalled, which is a legitimate thing to
        # record, and a constraint at the floor would refuse to store the evidence that
        # something was inferred weakly.
        CheckConstraint(
            "formed_confidence >= 0.0 AND formed_confidence <= 1.0", name="confidence_share"
        ),
        Index("ix_adaptive_principal", "principal_id"),
        {"schema": "mem"},
    )


#: The retrieval floor, re-exported so a reader of this module finds it without going looking,
#: and so the comment above about it not being a constraint has the value beside it.
RETRIEVAL_FLOOR = RECALL_FLOOR
