"""The audit ledger's table. Append-only in the database, not in the coding standard.

`brain.audit.ledger` builds a hash chain in memory and says so plainly: "Nothing here
touches a database. The table that eventually persists these entries stores the same fields
and runs `verify` as its check job." This is that table.

**What breaks without it.** Everything the ledger is for. A chain that lives in a process
is a chain that ends when the process does, so M24.1.2's verification job has nothing to
walk, M24.1.5's client-visible view has nothing to filter, and the answer to "who granted
her that, and when" is that nobody recorded it.

**Why the chain is not enough on its own.** The module docstring in `ledger.py` is careful
about what a hash chain proves: editing entry 12 invalidates 12 and everything after it, so
a quiet edit is detectable *by a walk*. It says nothing about whether the edit is possible.
A table that is append-only by convention is one UPDATE away from saying whatever the
person holding the database password wants, and the chain only helps if somebody runs the
walk afterwards and believes the result. So the refusal is in the database:

1. `brain_app` is granted SELECT and INSERT on this table and nothing else. That stops the
   application, which is the only thing that should ever be writing here.
2. Row-level security is enabled with a SELECT policy and an INSERT policy and no others.
   PostgreSQL denies what no policy admits, so UPDATE and DELETE are refused for every role
   that cannot bypass row-level security - which, per `0001_foundation`, is every role this
   system owns.
3. A trigger raises. Triggers fire for the table owner and for superusers too, so this is
   the layer that survives somebody connecting as `postgres` with a good reason.

Only the third of those is proof against an administrator, and it is the reason the trigger
exists rather than the grants being considered sufficient.

**A rule was rejected in favour of a trigger.** `CREATE RULE ... DO INSTEAD NOTHING` is the
older way to make a table append-only and it is worse in exactly one way that matters: it
discards the write silently. The statement reports success, zero rows change, and whoever
ran it believes the ledger now says something it does not. An exception is loud, names the
operation, and carries a hint about the only correct remedy, which is to append a
correcting entry rather than to edit a wrong one.

**Retention is not carved out, and that is a decision with a cost.** `AuditChain.prune_before`
exists, M24.2.1 suspends deletion under legal hold, and neither can run against this table:
the trigger refuses removal from everybody, unconditionally. The alternative was a session
setting the retention job sets and the trigger honours, and it was rejected because anybody
who can set that setting can edit the ledger, which returns the table to being append-only
by convention with an extra step. When retention is built it needs its own migration and
its own argument; until then the ledger only grows, which is the failure that can be fixed
later rather than the one that cannot.

Task ids: M24.1.1
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.audit.ledger import (
    DIGEST,
    DIGEST_CHARS,
    ENT_HASH,
    IDENTIFIER,
    SUBJECT_KINDS,
    TRACE_ID,
    AuditAction,
)
from brain.db import Base
from brain.tables.identity import one_of

#: `AuditEntry.actor_id` is validated against `IDENTIFIER`, which is bounded at 128.
ACTOR_ID_CHARS = 128

#: `AuditEntry.subject` is `Field(max_length=160)`.
SUBJECT_CHARS = 160

#: `AuditEntry.ent_hash` is `EntitlementSet.ent_hash`, truncated to 32 characters.
ENT_HASH_CHARS = 32

#: `db.py` sizes `trace_id` at 64 characters and `ledger.py` says so.
TRACE_ID_CHARS = 64


def _bare(pattern: str) -> str:
    """Strip the anchors off a Python pattern so it can be re-anchored inside a bigger one.

    The alternative was to keep a second, unanchored copy of each pattern next to the first.
    Two copies of a regex is two regexes, and the one that gets fixed is whichever one the
    person was looking at.
    """
    return pattern.removeprefix("^").removesuffix("$")


#: `<kind>:<id>`, with the kind closed and the id an identifier. This is `_subject_grammar`
#: written as one regex: the validator does the same job in two steps, which a check
#: constraint cannot.
SUBJECT_PATTERN = f"^({'|'.join(sorted(SUBJECT_KINDS))}):{_bare(IDENTIFIER)}$"


class AuditEntryRow(Base):
    """`obs.audit_entry`. Mirrors `brain.audit.ledger.AuditEntry` (M24.1.1).

    Carries neither `TimestampMixin` nor `SoftDeleteMixin`, and both omissions are the
    point.

    `TimestampMixin` would add a `created_at` filled by the database clock and an
    `updated_at` maintained on update. The second is meaningless on a table that refuses
    updates. The first is worse than meaningless: `at` is inside the digest, so
    `ledger.append` explains that the caller must pass one authoritative clock's reading and
    the database cannot fill it in with `server_default` the way every other table here
    does. A second timestamp beside it, from a second reading of the same clock, is a
    column that disagrees with the hashed one by a few milliseconds and gives anybody
    reading the table two answers to "when".

    `SoftDeleteMixin` would add a `deleted_at`, and a ledger entry that can be marked
    deleted is a ledger entry that can be hidden. That is the thing the whole table exists
    to make impossible.
    """

    __tablename__ = "audit_entry"

    #: The chain position, and the primary key. Explicitly not an identity column:
    #: `AuditChain.append` computes `seq` from the previous entry because a caller who can
    #: choose it can forge a link, and a database-generated sequence is exactly such a
    #: caller. It also would not survive `prune_before`, which keeps the original numbering
    #: of what it retains.
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    #: No server default. See the class docstring: this timestamp is inside the digest.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    actor_id: Mapped[str] = mapped_column(String(ACTOR_ID_CHARS), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(SUBJECT_CHARS), nullable=False, index=True)

    #: The actor's reach as a digest, never the capabilities themselves. A ledger of
    #: capabilities is a map of who can see what, which is a document nobody should have.
    ent_hash: Mapped[str] = mapped_column(String(ENT_HASH_CHARS), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(TRACE_ID_CHARS), nullable=False, index=True)

    #: Field names and digests only. `redact_details` decides what may be in here and
    #: `AuditEntry._names_only` refuses the rest on the way back out, so a row written by an
    #: older version of the code cannot smuggle a value past the type. The constraint below
    #: pins the shape and not the contents: a per-key regex over a jsonb object needs a
    #: subquery, and a check constraint may not contain one.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    prev_hash: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    __table_args__ = (
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        CheckConstraint(f"actor_id ~ '{IDENTIFIER}'", name="actor_id_shape"),
        CheckConstraint(one_of("action", AuditAction), name="action"),
        CheckConstraint(f"subject ~ '{SUBJECT_PATTERN}'", name="subject_grammar"),
        CheckConstraint(f"ent_hash ~ '{ENT_HASH}'", name="ent_hash_shape"),
        CheckConstraint(f"trace_id ~ '{TRACE_ID}'", name="trace_id_shape"),
        CheckConstraint(f"prev_hash ~ '{DIGEST}'", name="prev_hash_shape"),
        CheckConstraint(f"entry_hash ~ '{DIGEST}'", name="entry_hash_shape"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        # An entry whose digest equals its parent's is either a duplicate row or a link
        # pointed at itself. Both are cheap to refuse and neither has a legitimate reading.
        CheckConstraint("entry_hash <> prev_hash", name="not_its_own_parent"),
        # Unique, because a repeated digest means a repeated entry.
        Index("uq_audit_entry_entry_hash", "entry_hash", unique=True),
        # Unique for a stronger reason, and this one the chain cannot check itself: two
        # entries naming the same parent is a fork. `AuditChain.verify` walks a sequence it
        # was handed and cannot see a branch that is not in the window, so a fork is exactly
        # the tamper that survives verification - write a second history from entry 12 and
        # both halves walk cleanly. The database refuses to hold two children of one parent,
        # which makes the ledger linear by construction rather than by inspection.
        Index("uq_audit_entry_prev_hash", "prev_hash", unique=True),
        {"schema": "obs"},
    )
