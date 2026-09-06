"""The template pair: a manifest nobody may amend, and the install that pins one.

Two tables in the `agent` schema, which 0001 created and 0001's downgrade owns. Nothing
here creates a schema, a function or a trigger, so dropping the two tables in reverse is
the whole reversal.

**`agent.template_version` is granted SELECT and INSERT and nothing else, and that grant is
what "immutable" means.** A published manifest is a promise somebody signed; an amended one
is a different promise wearing the same version number, and every instance pinned to it
would silently start materialising from the new body with no upgrade badge, no diff and
nobody asked. So the privilege is withheld and no policy admits an update either, which is
the arrangement `obs.audit_entry` has for the same reason. There is no trigger here: 0002
adds one to the ledger because a ledger is evidence and has to refuse a superuser too, and
a template manifest is configuration.

**`agent.template_instance` takes UPDATE, deliberately.** Editing an overlay, giving a path
back to the template and accepting an upgrade are all updates to that row, and they are the
operations M13.4 is made of. The difference in privilege between the two tables is the
difference between a published thing and an installed one, written where a person reading
the schema can see it.

**The seal is a check constraint, and there are two of them (M13.2.6).**
`sealed_paths_are_absent` names the five paths an overlay may never change:
`identity.template_id`, `identity.version`, `identity.published_by`,
`guardrails.max_side_effect` and `guardrails.leash`. `overlay_paths_are_settable` refuses
anything that is not one of the twelve settable paths, and that is what closes the
spellings the first cannot see: `guardrails` sets both sealed paths in that section without
naming either, and `guardrails.leash.0.rung` reaches inside one. Neither implies the other,
so both ship. `brain.agents.template` argues the choice of the five at length.

**A CHECK cannot hold a subquery**, so "every key is one of these" is written as
subtraction: after deleting every settable key, nothing is left. `jsonb_exists_any` and
`jsonb_exists_all` are `?|` and `?&` written as the functions they are, which keeps a
question mark out of DDL that travels through a driver and a renderer that each have their
own opinion about one.

**Row-level security is enabled on both and both policies are unconditional**, for the
reason 0014 gives about `agent.agent`. A template has no audience at all: it is a
declaration that travels between installations, and there is nothing in it to filter on. An
instance has the audience of the agent it materialises into, which lives on `agent.agent`
and is applied where a listing is built, against a viewer this table knows nothing about.

**No DELETE grant on either**, as everywhere but 0006. A manifest is referred to by every
instance pinned to it, and an instance is the record of why an agent is configured the way
it is.

**Nothing imports `brain.tables`.** Every predicate below is copied from the model
deliberately, so this migration goes on describing the database it actually built rather
than whatever the models say today. `tests/unit/test_template_tables.py` compares the two on
rendered DDL, which is what turns the copies into a check rather than a duplication.

Task ids: M13.2.1, M13.2.3, M13.2.4, M13.2.5, M13.2.6

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: In creation order: the instance carries a foreign key into the version, so the version
#: has to exist first. `downgrade` walks this in reverse.
TABLES: tuple[str, ...] = ("agent.template_version", "agent.template_instance")

#: `brain.core.department.SLUG_PATTERN`, copied, with its one colon escaped.
#:
#: The escape is not decoration. `sa.CheckConstraint` parses its argument as `text()`, which
#: reads `:name` as a bind parameter, so the unescaped pattern renders as
#: `(?NULL[a-z0-9]+)*` and the constraint PostgreSQL is asked to create is a different
#: regular expression from the one the type enforces. 0015 exists because three tables in
#: 0003 shipped that way and could not take a row at all.
SLUG_GRAMMAR = r"^[a-z][a-z0-9]*(?\:_[a-z0-9]+)*$"

#: `brain.audit.ledger.DIGEST`. A full sha256 hexdigest.
DIGEST_SHAPE = r"^[0-9a-f]{64}$"

#: `brain.agents.template.MANIFEST_PATHS`, copied. The seventeen paths a manifest document
#: has, sorted. A document missing one is a manifest this codebase cannot flatten; a
#: document with an extra one is a field somebody added without a migration.
MANIFEST_PATHS = (
    "'authority.allowed_tools', 'authority.capabilities', 'authority.required_tools', "
    "'authority.scope', 'connectors', 'golden_set', 'guardrails.leash', "
    "'guardrails.max_side_effect', 'identity.display_name', 'identity.published_by', "
    "'identity.summary', 'identity.template_id', 'identity.version', 'persona', "
    "'placeholders', 'skills', 'tier'"
)

#: `brain.agents.template.SETTABLE_PATHS`, copied: the twelve an overlay may mention.
SETTABLE_PATHS = (
    "'authority.allowed_tools', 'authority.capabilities', 'authority.required_tools', "
    "'authority.scope', 'connectors', 'golden_set', 'identity.display_name', "
    "'identity.summary', 'persona', 'placeholders', 'skills', 'tier'"
)

#: `brain.agents.template.SEALED_PATHS`, copied: the five an overlay may never change.
SEALED_PATHS = (
    "'guardrails.leash', 'guardrails.max_side_effect', 'identity.published_by', "
    "'identity.template_id', 'identity.version'"
)

#: The seal (M13.2.6). `jsonb_exists_any` is `?|` written as the function it is.
SEALED_PATHS_ARE_ABSENT = f"NOT jsonb_exists_any(overlay, ARRAY[{SEALED_PATHS}])"

#: And the companion that makes the seal total: an overlay may mention nothing else, so
#: `guardrails` and `guardrails.leash.0.rung` are refused as well as the five by name.
OVERLAY_PATHS_ARE_SETTABLE = f"overlay - ARRAY[{SETTABLE_PATHS}] = '{{}}'::jsonb"

#: Ownership is keyed by every path, not only the settable ones: a sealed path still has an
#: owner and it is whoever published the manifest.
FIELD_OWNER_PATHS_ARE_KNOWN = f"field_owners - ARRAY[{MANIFEST_PATHS}] = '{{}}'::jsonb"

RLS: tuple[str, ...] = (
    "ALTER TABLE agent.template_version ENABLE ROW LEVEL SECURITY",
    # SELECT and INSERT only. PostgreSQL denies what no policy admits, so the absence of an
    # UPDATE policy is a second refusal of amendment sitting underneath the missing grant.
    """
    CREATE POLICY template_version_readable ON agent.template_version
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY template_version_publishable ON agent.template_version
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    "ALTER TABLE agent.template_instance ENABLE ROW LEVEL SECURITY",
    # Unconditional, per the module docstring: the audience lives on `agent.agent` and is
    # applied where a listing is built, against a viewer this table knows nothing about.
    """
    CREATE POLICY template_instance_visible ON agent.template_instance
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

#: No DELETE on either, and no UPDATE on the manifest. The second omission is the immutable
#: half of M13.2.1 and it is the only place that rule is enforced.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT ON agent.template_version TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON agent.template_instance TO brain_app",
)


def _create_template_version() -> None:
    op.create_table(
        "template_version",
        # The key is the pair an instance pins and an upgrade compares. A surrogate would
        # let two rows hold one pair and leave every pinned instance materialising against
        # whichever the resolver found first.
        sa.Column("template_id", sa.String(60), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer, primary_key=True, autoincrement=False, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("signed_by", sa.String(128), nullable=False),
        # No server default. This is when a person published, not when the row arrived.
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # `created_at` alone, and no `updated_at`: a column that can never move is a column
        # that tells a reader something untrue.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Declared in the order the model declares them, so the rendered DDL is
        # character-for-character what `CreateTable` produces from the model.
        sa.CheckConstraint(f"template_id ~ '{SLUG_GRAMMAR}'", name="slug_grammar"),
        sa.CheckConstraint("version >= 1", name="version_is_positive"),
        sa.CheckConstraint(f"content_digest ~ '{DIGEST_SHAPE}'", name="content_digest_shape"),
        sa.CheckConstraint(f"signature ~ '{DIGEST_SHAPE}'", name="signature_shape"),
        sa.CheckConstraint("length(btrim(signed_by)) > 0", name="signed_by_present"),
        sa.CheckConstraint("jsonb_typeof(document) = 'object'", name="document_shape"),
        sa.CheckConstraint(
            f"jsonb_exists_all(document, ARRAY[{MANIFEST_PATHS}])",
            name="document_holds_every_path",
        ),
        sa.CheckConstraint(
            f"document - ARRAY[{MANIFEST_PATHS}] = '{{}}'::jsonb",
            name="document_holds_no_other_path",
        ),
        schema="agent",
    )


def _create_template_instance() -> None:
    op.create_table(
        "template_instance",
        # The agent slug this install materialises into, so there is one instance per agent
        # and the join to `agent.agent` needs no third column.
        sa.Column("id", sa.String(60), primary_key=True, nullable=False),
        # ------------------------------------------------------------------- the pin
        sa.Column("template_id", sa.String(60), nullable=False),
        sa.Column("template_version", sa.Integer, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        # --------------------------------------------------------------- the overlay
        sa.Column(
            "overlay",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "field_owners",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # -------------------------------------------------------- the materialisation
        # No server default. A row with no effective document is an agent nobody can run,
        # and a default would make it look configured.
        sa.Column("effective_document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_hash", sa.String(64), nullable=False),
        # Not a foreign key, for the reason `agent.agent` gives: an agent has to outlive
        # the account that built it.
        sa.Column("created_by", sa.String(128), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["agent.template_version.template_id", "agent.template_version.version"],
            # Named by hand with the convention's own `fk_` prefix: the generated name
            # would be 66 characters against PostgreSQL's 63-byte identifier limit.
            name="fk_template_instance_pinned_version",
        ),
        sa.CheckConstraint(f"id ~ '{SLUG_GRAMMAR}'", name="slug_grammar"),
        sa.CheckConstraint("template_version >= 1", name="version_is_positive"),
        sa.CheckConstraint(f"content_digest ~ '{DIGEST_SHAPE}'", name="content_digest_shape"),
        sa.CheckConstraint(f"effective_hash ~ '{DIGEST_SHAPE}'", name="effective_hash_shape"),
        sa.CheckConstraint("jsonb_typeof(overlay) = 'object'", name="overlay_shape"),
        # The seal, and the rule that makes the seal total.
        sa.CheckConstraint(SEALED_PATHS_ARE_ABSENT, name="sealed_paths_are_absent"),
        sa.CheckConstraint(OVERLAY_PATHS_ARE_SETTABLE, name="overlay_paths_are_settable"),
        sa.CheckConstraint("jsonb_typeof(field_owners) = 'object'", name="field_owners_shape"),
        sa.CheckConstraint(FIELD_OWNER_PATHS_ARE_KNOWN, name="field_owner_paths_are_known"),
        sa.CheckConstraint(
            "jsonb_typeof(effective_document) = 'object'", name="effective_document_shape"
        ),
        sa.CheckConstraint(
            f"jsonb_exists_all(effective_document, ARRAY[{MANIFEST_PATHS}])",
            name="effective_holds_every_path",
        ),
        sa.CheckConstraint(
            f"effective_document - ARRAY[{MANIFEST_PATHS}] = '{{}}'::jsonb",
            name="effective_holds_no_other_path",
        ),
        sa.CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        schema="agent",
    )
    # The upgrade path's index: every instance pinned to one version of one template, which
    # is the query that decides who sees an upgrade badge. Not partial: a disabled agent
    # still needs the badge when somebody brings it back.
    op.create_index(
        "ix_template_instance_pin",
        "template_instance",
        ["template_id", "template_version"],
        schema="agent",
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 through 0015 do; this keeps
    # the constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    # And the immutable half of M13.2.1, asserted rather than left to a comment: a manifest
    # row that can be updated is a promise that can be rewritten under everyone pinned to it.
    assert "UPDATE" not in GRANTS[0]

    _create_template_version()
    _create_template_instance()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policies, the index and the table privileges belong to the tables and go with
    # them, and this migration creates no function and no trigger. Reversed, so the instance
    # goes before the version it points at. `agent` is not dropped: 0001 created it and
    # 0001's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
