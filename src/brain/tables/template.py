"""Two tables: the manifest nobody may amend, and the install that pins one and edits it.

Mirrors `brain.agents.template`, which is the definition; this is storage for it. Every
width, vocabulary and path list below is generated from that module's own constants rather
than retyped, so a change to the domain breaks a test instead of a deploy.

**The seal is here and not there.** `brain.agents.template.check_overlay` refuses a sealed
path with a sentence somebody can act on, and it runs only for a caller who came through
that module. `sealed_paths_are_absent` runs for every write that reaches PostgreSQL,
including the seed script, the console fix applied by hand and the UPDATE somebody ran at
two in the morning to unblock a release. Those are the writes a seal exists for, so this
file is the enforcement point and the validator is the message. The domain module says the
same thing in `THE_CHECK_CONSTRAINT_IS_THE_SEAL_AND_THE_VALIDATOR_IS_THE_MESSAGE`.

**Two constraints hold the overlay, and they are not two copies of one rule.**
`sealed_paths_are_absent` refuses the five paths by name. `overlay_paths_are_settable`
refuses anything that is not one of the twelve settable paths, which is what closes the
spellings the first one cannot see: `guardrails` sets both sealed paths in that section
without naming either, `guardrails.leash.0.rung` reaches inside one, and a trailing space
makes a third string that equals no sealed path. Neither constraint implies the other. The
settable rule admits a sealed path, because a sealed path is a real path; the sealed rule
admits an unknown one. Shipping only the settable rule would make the seal depend on that
list being right, and the list is the thing that grows every time somebody adds a field.

**Subtraction rather than a negated existence test, because a CHECK cannot hold a
subquery.** `overlay - ARRAY[...] = '{}'::jsonb` says "after removing every settable key,
nothing is left", which is an allow list written as one expression with no scan and no
subselect. The obvious `NOT EXISTS (SELECT 1 FROM jsonb_object_keys(overlay) ...)` is what
this wants to be and PostgreSQL refuses it outright in a check constraint.

**`jsonb_exists_all` rather than `?&`.** They are the same operator; the function form has
no question mark in it. A `?` inside DDL that travels through a driver, a migration
renderer and a test harness is a character three layers each have their own opinion about,
and the function form removes the question rather than escaping it three ways.

**The manifest table is granted SELECT and INSERT and nothing else, and that is what
"immutable" means here.** No UPDATE privilege, and no policy admitting one, so a published
manifest cannot be amended by the application at all. `obs.audit_entry` is the precedent
and the argument is the same shape: a table that is append-only by convention says whatever
the person holding the database password wants it to say. It carries no `TimestampMixin`
for the same reason `obs.audit_entry` does not: `updated_at` on a row that can never be
updated is a column that tells a reader something untrue, and `signed_at` is already the
authoritative time, inside the digest's neighbourhood rather than beside it.

**The instance table is updatable, deliberately.** An overlay is edited, a field is cleared
and given back to the template, and an upgrade rewrites the pin. Those are the operations
M13.4 is made of, so this table takes UPDATE while the manifest table does not, which is
the whole distinction between a published thing and an installed one.

**A composite foreign key from the instance to the manifest version, and none from the
agent.** An instance pinned to a manifest that does not exist is an instance nobody can
materialise, and `agent.template_version` never loses a row, so the key blocks nothing. A
key between `agent.template_instance.id` and `agent.agent.id` would be right in one
direction and impossible in the other: every agent is an install, so it would have to run
from `agent.agent`, and that is an ALTER on a deployed table belonging to M13.3, which is
where both rows first get written together.

**No audience columns.** The visibility level, the steward and the department live on
`agent.agent` and nowhere else. A template that travelled with an audience would publish an
agent into a company it has never seen, and a second copy on the instance would be a copy
that can disagree with the one selection actually reads. `brain.agents.model` argues the
axis and this is what it looks like when a second table is tempted to repeat it.

**No DELETE grant on either**, as everywhere but 0006. A manifest is referred to by every
instance pinned to it, and an instance is the record of why an agent is configured the way
it is.

Task ids: M13.2.1, M13.2.3, M13.2.4, M13.2.5, M13.2.6
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.agents.model import AGENT_ID_CHARS, OWNER_ID_CHARS
from brain.agents.template import MANIFEST_PATHS, SEALED_PATHS, SETTABLE_PATHS
from brain.audit.ledger import DIGEST_CHARS
from brain.core.department import SLUG_PATTERN
from brain.db import Base, TimestampMixin

#: A colon inside a check constraint is a bind parameter unless it is escaped.
#:
#: `CheckConstraint` parses its argument as `text()`, and `text()` reads `:name` as a
#: parameter to bind later. `SLUG_PATTERN` contains one colon, in `(?:`, so the unescaped
#: form renders as `(?NULL[a-z0-9]+)*`: the non-capturing group becomes a null bind, and
#: measured against PostgreSQL 18.6 the first INSERT then fails with "invalid regular
#: expression: quantifier operand invalid". Nothing reports it at DDL time.
#:
#: `brain.tables.agent` carries the same escape and 0015 exists because three tables in
#: `brain.tables.gate` did not. The test for it asserts on the compiled `CreateTable` DDL
#: rather than on `str(constraint.sqltext)`, because `text()` normalises the escape at
#: construction and prints the parameter marker back either way.
_ESCAPED_COLON = "\\:"


def _slug_grammar(column: str) -> str:
    """The shared slug grammar, escaped, applied to one column."""
    return f"{column} ~ '" + SLUG_PATTERN.replace(":", _ESCAPED_COLON) + "'"


def _digest_shape(column: str) -> str:
    """A full sha256 hexdigest, as `brain.audit.ledger.DIGEST` spells it.

    Written from `DIGEST_CHARS` rather than with a literal 64, so a change to the digest
    width is one edit and a failing test rather than a constraint that silently disagrees
    with the values the application produces.
    """
    return f"{column} ~ '^[0-9a-f]{{{DIGEST_CHARS}}}$'"


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


def _array(paths: tuple[str, ...]) -> str:
    """A `text[]` literal from a path tuple.

    The paths are module constants matching `[a-z_.]+`, so there is nothing here to quote
    around: a path that could carry an apostrophe would be a path that could carry a
    semicolon, and it would have failed `MANIFEST_PATHS`'s own test long before this.
    """
    return "ARRAY[" + ", ".join(f"'{path}'" for path in paths) + "]"


def _holds_every_path(column: str) -> str:
    """Nothing missing. `jsonb_exists_all` is `?&` without the question mark."""
    return f"jsonb_exists_all({column}, {_array(MANIFEST_PATHS)})"


def _holds_no_other_path(column: str) -> str:
    """Nothing extra. Subtraction rather than a subquery; see the module docstring."""
    return f"{column} - {_array(MANIFEST_PATHS)} = '{{}}'::jsonb"


#: The five an overlay may not change, however it is written (M13.2.6). The list is
#: generated from `brain.agents.template.SEALED_PATHS` rather than retyped, so a path
#: sealed in the domain and not here fails `tests/unit/test_template_tables.py` instead of
#: being admitted by the database.
SEALED_PATHS_ARE_ABSENT = f"NOT jsonb_exists_any(overlay, {_array(SEALED_PATHS)})"

#: And the companion: an overlay may mention nothing but the twelve settable paths. This is
#: what refuses `guardrails`, `guardrails.leash.0.rung` and every other spelling that
#: reaches a sealed value without equalling one.
OVERLAY_PATHS_ARE_SETTABLE = f"overlay - {_array(SETTABLE_PATHS)} = '{{}}'::jsonb"

#: Ownership is keyed by path too, and by every path rather than only the settable ones:
#: the sealed paths are owned by whoever published the manifest, which is the answer an
#: upgrade review needs for them.
FIELD_OWNER_PATHS_ARE_KNOWN = f"field_owners - {_array(MANIFEST_PATHS)} = '{{}}'::jsonb"


class TemplateVersionRow(Base):
    """`agent.template_version`. One published, signed manifest (M13.2.1).

    The primary key is `(template_id, version)` rather than a surrogate, because that pair
    is what an instance pins and what an upgrade compares. A surrogate would let two rows
    hold one pair and leave every pinned instance materialising against whichever the
    resolver found first, which is the failure the pin exists to prevent.

    Carries no `TimestampMixin`. `updated_at` on a table that is never updated is a column
    that tells a reader something untrue, and `signed_at` is the time anybody asks about:
    when this manifest was published, by the clock of whoever published it, rather than
    when the row landed.
    """

    __tablename__ = "template_version"

    #: Mirrors `ManifestIdentity.template_id`. One namespace with agents, scopes and tool
    #: objects, policed by `brain.core.department.check_slug_collisions`.
    template_id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), primary_key=True)

    #: Mirrors `ManifestIdentity.version`. An integer, not a semantic version: the upgrade
    #: path shows a per-path diff rather than asking whether a change was breaking, so a
    #: number somebody chose while publishing would decide nothing and mislead anyway.
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    #: The digest of `document`, recomputed by `SignedManifest` every time it is loaded.
    content_digest: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    #: HMAC-SHA256 over the digest. Checked by `brain.agents.template.verify`, which
    #: `install` calls before an instance is pinned to this row.
    signature: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    signed_by: Mapped[str] = mapped_column(String(OWNER_ID_CHARS), nullable=False)

    #: No server default. This is when a person published, not when a row arrived, and the
    #: two differ by however long the publish path took. `obs.audit_entry.at` refuses a
    #: default for the same reason.
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: `TemplateManifest.document()`: the flat map from dotted path to JSON value. Flat
    #: rather than nested, because the overlay, the seal, the ownership map and M13.4's
    #: diff are all keyed by path, and one representation for four mechanisms is one place
    #: for them to agree.
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: When the row landed, which is not `signed_at`. Declared here rather than inherited,
    #: because the mixin brings `updated_at` with it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(_slug_grammar("template_id"), name="slug_grammar"),
        CheckConstraint("version >= 1", name="version_is_positive"),
        CheckConstraint(_digest_shape("content_digest"), name="content_digest_shape"),
        CheckConstraint(_digest_shape("signature"), name="signature_shape"),
        CheckConstraint(_present("signed_by"), name="signed_by_present"),
        CheckConstraint("jsonb_typeof(document) = 'object'", name="document_shape"),
        CheckConstraint(_holds_every_path("document"), name="document_holds_every_path"),
        CheckConstraint(_holds_no_other_path("document"), name="document_holds_no_other_path"),
        {"schema": "agent"},
    )


class TemplateInstanceRow(TimestampMixin, Base):
    """`agent.template_instance`. One install: a pin, an overlay, and who set what.

    The primary key is the agent slug this instance materialises into, so there is exactly
    one instance per agent and the join to `agent.agent` needs no third column. Every agent
    is an install of something, per M13.2.7, and a hand-built one is an install of the
    blank template.

    Updatable, unlike the manifest table. Editing an overlay, giving a path back to the
    template and accepting an upgrade are all updates to this row, and they are the
    operations the versioning leaf is made of.
    """

    __tablename__ = "template_instance"

    #: The agent slug. `AgentRecord.agent_id`'s grammar, because it becomes one.
    id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), primary_key=True)

    # ----------------------------------------------------------------------- the pin
    #: Three columns, because the pin is three facts. The pair points at the manifest and
    #: the digest catches the case the pair cannot see: a version republished with a
    #: different body. The immutable grant makes that impossible through the application;
    #: this is the second refusal, for the row that arrived some other way.
    template_id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    # ------------------------------------------------------------------- the overlay
    #: The local edits, keyed by dotted path. Empty is the safe default and it means this
    #: install is exactly what was published.
    overlay: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )

    #: One entry per overlaid path: source, who set it, when (M13.2.4). Keyed by the same
    #: paths, and checked against the whole manifest vocabulary rather than the settable
    #: subset, because a sealed path still has an owner and it is the publisher.
    field_owners: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )

    # --------------------------------------------------------------- the materialisation
    #: What `materialise` produced: the manifest with the overlay applied, flattened
    #: (M13.2.5). A cache of a computation, written together with the hash below or not at
    #: all. No server default, because a row with no effective document is an agent nobody
    #: can run and a default would make it look configured.
    effective_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: The digest of `effective_document`, which is what `brain.gate.cache_key.key_for`
    #: takes as `agent_config_hash`.
    #:
    #: Not checked against `effective_document` by a constraint, and the reason is that the
    #: two canonicalisations are different. The digest is taken over Python's canonical
    #: JSON, and `effective_document::text` is PostgreSQL's own rendering of the same
    #: value; they differ on non-ASCII escaping and on number formatting, so a constraint
    #: comparing them would be comparing two encodings and would refuse the first manifest
    #: with an accent in it. The property that matters is that the hash moves when the
    #: overlay moves, which is a property of `materialise` and is tested there.
    effective_hash: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    #: Who installed it. Not a foreign key, for the reason `agent.agent` gives: an agent
    #: has to outlive the account that built it.
    created_by: Mapped[str] = mapped_column(String(OWNER_ID_CHARS), nullable=False)

    __table_args__ = (
        # Named by hand, and the name carries the `fk_` prefix `brain.db.NAMING_CONVENTION`
        # would have given it. The convention's own template renders
        # `fk_template_instance_template_id_template_version_template_version`, which is 66
        # characters against PostgreSQL's 63-byte identifier limit: the server truncates
        # silently and the constraint ends up called something nobody wrote.
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["agent.template_version.template_id", "agent.template_version.version"],
            name="fk_template_instance_pinned_version",
        ),
        CheckConstraint(_slug_grammar("id"), name="slug_grammar"),
        CheckConstraint("template_version >= 1", name="version_is_positive"),
        CheckConstraint(_digest_shape("content_digest"), name="content_digest_shape"),
        CheckConstraint(_digest_shape("effective_hash"), name="effective_hash_shape"),
        CheckConstraint("jsonb_typeof(overlay) = 'object'", name="overlay_shape"),
        # The seal (M13.2.6), and the rule that makes the seal total. See the module
        # docstring for why both ship and why neither implies the other.
        CheckConstraint(SEALED_PATHS_ARE_ABSENT, name="sealed_paths_are_absent"),
        CheckConstraint(OVERLAY_PATHS_ARE_SETTABLE, name="overlay_paths_are_settable"),
        CheckConstraint("jsonb_typeof(field_owners) = 'object'", name="field_owners_shape"),
        CheckConstraint(FIELD_OWNER_PATHS_ARE_KNOWN, name="field_owner_paths_are_known"),
        CheckConstraint(
            "jsonb_typeof(effective_document) = 'object'", name="effective_document_shape"
        ),
        CheckConstraint(_holds_every_path("effective_document"), name="effective_holds_every_path"),
        CheckConstraint(
            _holds_no_other_path("effective_document"), name="effective_holds_no_other_path"
        ),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        # The upgrade path's index: every instance pinned to one version of one template,
        # which is the query M13.4.2 runs to decide who sees an upgrade badge. Not partial:
        # a disabled agent still needs the badge when somebody brings it back.
        Index("ix_template_instance_pin", "template_id", "template_version"),
        {"schema": "agent"},
    )
