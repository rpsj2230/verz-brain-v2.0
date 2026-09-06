"""The row that makes a declined upgrade stay declined.

One table in the `agent` schema, which 0001 created and 0001's downgrade owns. Nothing here
creates a schema, a function or a trigger, so dropping the table is the whole reversal.

**`agent.upgrade_decline` is granted SELECT and INSERT and nothing else, and that grant is
the feature.** A decline is worth recording only if it is durable: one that is forgotten by
the next publish is a nag, and a nag is how somebody accepts an upgrade they did not read.
Without UPDATE, a decline cannot be edited into naming a different version; without DELETE,
it cannot be quietly withdrawn so that a badge comes back. Changing your mind is accepting
the upgrade, which moves the pin on `agent.template_instance` and leaves this row standing
as the record that somebody once said no. `agent.template_version` has the same arrangement
for the neighbouring reason.

**The primary key names the version, not the template.** Declining version 3 must say
nothing at all about version 4, or the first decline silences the template for ever and the
install is one nobody will ever look at again. `brain.agents.upgrade` argues it at length.

**Two foreign keys, both to tables 0016 built.** A decline belongs to an install that
exists and names a version that was published. Neither key blocks anything: no DELETE grant
exists on either target, so neither row ever goes away underneath this one.

**Row-level security is enabled and the two policies are unconditional**, for the reason
0014 and 0016 give. A decline carries no audience of its own; who may see the agent it
concerns lives on `agent.agent` and is applied where a listing is built, against a viewer
this table knows nothing about. The absence of an UPDATE or DELETE policy is a second
refusal sitting underneath the two missing grants, because PostgreSQL denies what no policy
admits.

**No index beyond the primary key.** The only question asked of this table is whether one
instance declined one version of one template, which is that key exactly.

**Nothing imports `brain.tables`.** Every predicate below is copied from the model
deliberately, so this migration goes on describing the database it actually built rather
than whatever the models say later. `tests/unit/test_upgrade_tables.py` compares the two on
rendered DDL, which is what turns the copy into a check rather than a duplication.

Task ids: M13.4.5

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: The one table this migration builds. `downgrade` walks it in reverse, which for one
#: table is the same order and is written that way so adding a second cannot forget it.
TABLES: tuple[str, ...] = ("agent.upgrade_decline",)

#: `brain.core.department.SLUG_PATTERN`, copied, with its one colon escaped.
#:
#: `sa.CheckConstraint` parses its argument as `text()`, which reads `:name` as a bind
#: parameter, so the unescaped pattern renders as `(?NULL[a-z0-9]+)*` and the constraint
#: PostgreSQL is asked to create is a different regular expression from the one the type
#: enforces. Nothing reports it at DDL time; the first INSERT fails. 0015 exists because
#: three tables in 0003 shipped that way and could not take a row at all.
SLUG_GRAMMAR = r"^[a-z][a-z0-9]*(?\:_[a-z0-9]+)*$"

#: `brain.audit.ledger.DIGEST`. A full sha256 hexdigest.
DIGEST_SHAPE = r"^[0-9a-f]{64}$"

RLS: tuple[str, ...] = (
    "ALTER TABLE agent.upgrade_decline ENABLE ROW LEVEL SECURITY",
    # SELECT and INSERT only, matching the grants. A decline is never amended and never
    # withdrawn, so there is no policy admitting either, and PostgreSQL denies what no
    # policy admits for every role in this system: 0001 leaves none able to bypass it.
    """
    CREATE POLICY upgrade_decline_readable ON agent.upgrade_decline
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY upgrade_decline_recordable ON agent.upgrade_decline
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
)

#: No UPDATE and no DELETE. That pair of omissions is the whole of what "declined for ever"
#: means once a row exists, and it is the only place the rule is enforced.
GRANTS: tuple[str, ...] = ("GRANT SELECT, INSERT ON agent.upgrade_decline TO brain_app",)


def upgrade() -> None:
    # The statement names the role literally, the way 0001 through 0016 do; this keeps the
    # constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    # And the durable half of M13.4.5, asserted rather than left to a comment: a decline that
    # can be updated is a version that can be un-declined, and a decline that can be deleted
    # is a badge that comes back.
    assert all("UPDATE" not in statement for statement in GRANTS)
    assert all("DELETE" not in statement for statement in GRANTS)

    op.create_table(
        "upgrade_decline",
        # The instance, the template and the version: what was declined, for whom. Declining
        # version 3 says nothing about version 4, which is why the version is in the key.
        sa.Column("instance_id", sa.String(60), primary_key=True, nullable=False),
        sa.Column("template_id", sa.String(60), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer, primary_key=True, autoincrement=False, nullable=False),
        # The body that was declined. Out of the key, because the foreign key below points at
        # a table where the pair above is unique.
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("declined_by", sa.String(128), nullable=False),
        # No server default. This is when a person decided, not when the row arrived.
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["template_id", "version"],
            ["agent.template_version.template_id", "agent.template_version.version"],
            name="fk_upgrade_decline_declined_version",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["agent.template_instance.id"],
            name="fk_upgrade_decline_instance",
        ),
        sa.CheckConstraint(f"instance_id ~ '{SLUG_GRAMMAR}'", name="instance_slug_grammar"),
        sa.CheckConstraint(f"template_id ~ '{SLUG_GRAMMAR}'", name="template_slug_grammar"),
        sa.CheckConstraint("version >= 1", name="version_is_positive"),
        sa.CheckConstraint(f"content_digest ~ '{DIGEST_SHAPE}'", name="content_digest_shape"),
        sa.CheckConstraint("length(btrim(declined_by)) > 0", name="declined_by_present"),
        schema="agent",
    )

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policies and the table privileges belong to the table and go with it, and this
    # migration creates no function and no trigger. `agent` is not dropped: 0001 created it
    # and 0001's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
