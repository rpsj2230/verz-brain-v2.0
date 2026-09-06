"""The agent table: one row per configured agent, with audience and authority kept apart.

One table in the `agent` schema, which 0001 created and 0001's downgrade owns. Nothing here
creates a schema, a function or a trigger, so dropping the table is the whole reversal.

**Two independent descriptions of reach, in one row, that must never be derived from each
other.** `visibility` decides who may see and start the agent; `scope`, `capabilities` and
`allowed_tools` decide what a run through it may reach. `brain.agents.model` argues that
separation and this migration is where it meets a schema: from a `psql` prompt both are just
configuration, and `UPDATE agent.agent SET visibility = 'company'` has to remain a change to
who sees the agent and nothing else. No constraint below reads one from the other, and there
is no generated column or trigger that could.

**`department_matches_level` is the audience rule the type already refuses, refused again
for the row that arrived some other way.** A department audience with no department resolves
to the unrestricted scope, which is the widest audience under the middle one's name; a
department on a personal or company row is a field that reads as an audience and applies to
nothing. The equality is between two booleans, which PostgreSQL compares directly.

**Row-level security is enabled and the policy is `USING (true)`, deliberately.** Two
reasons, and the second is the decisive one.

The audience cannot be expressed here without becoming a second, partial copy of a rule that
has one implementation. Department membership is multi-valued, so the policy would have to
compare a session setting holding a list, and the day the SQL and
`brain.agents.model.visible_to` disagree the permissive one wins silently. `chat.conversation`
puts its rule in the policy because the rule is single-valued and unambiguous; this one is
neither.

And a policy that hid personal agents from everybody but their owner would hide exactly the
rows the offboarding path exists to find. `brain.agents.lifecycle.agents_needing_transfer`
looks for live agents whose steward has left, which is to say rows whose audience now reaches
nobody at all; behind such a policy the application could not see them, and a leaver's
private agents would sit in the table for ever with no one able to list them. The audience is
applied where a listing is built, against a viewer this table knows nothing about.

**No DELETE grant**, as everywhere but 0006. Retirement is `archived_at`, and the ledger
refers to agents by id: a run recorded against a row that no longer exists is a trace nobody
can read.

**Nothing imports `brain.tables`.** Every predicate below is copied from the model
deliberately, so this migration goes on describing the database it actually built rather than
whatever the models say today. `tests/unit/test_agent_tables.py` compares the two on rendered
DDL, which is what turns the copies into a check rather than a duplication.

Task ids: M13.1.1, M13.1.2, M13.1.4

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: One table. A tuple anyway, so `downgrade` walks the same list in reverse and
#: `tests/unit/test_agent_tables.py` can compare it against the package tuple's last slice,
#: which is the shape every migration here uses.
TABLES: tuple[str, ...] = ("agent.agent",)

#: `brain.core.department.SLUG_PATTERN`, copied, with its one colon escaped.
#:
#: The escape is not decoration. `sa.CheckConstraint` parses its argument as `text()`, which
#: reads `:name` as a bind parameter, so the unescaped pattern renders as
#: `(?NULL[a-z0-9]+)*` and the constraint PostgreSQL is asked to create is a different
#: regular expression from the one the type enforces. 0003 carries the unescaped form for
#: `gate.scope`, `gate.department` and `gate.team`; that needs its own migration to alter,
#: and repeating it here would make four.
SLUG_GRAMMAR = r"id ~ '^[a-z][a-z0-9]*(?\:_[a-z0-9]+)*$'"

#: `brain.models.routing.TIER_LADDER`, not `Tier`. `none` is the fast lane's absence of a
#: ladder rather than a pool, and an agent pinned to it would reach no model and answer
#: nothing.
TIER_IN = "tier IN ('heavy', 'main', 'small')"

#: `brain.knowledge.visibility.Visibility`. The same three levels the knowledge layer uses,
#: because there is one visibility vocabulary in this system and not one per table.
VISIBILITY_IN = "visibility IN ('company', 'department', 'personal')"

#: `brain.core.envelope.SideEffect`.
SIDE_EFFECT_IN = "max_side_effect IN ('draft', 'money', 'none', 'send', 'write')"

#: `brain.tables.gate.SCOPE_SHAPE`. `Scope.model_dump()` is an object with a `clauses` array;
#: the console's document form deserialises to a scope with no clauses, which is unrestricted,
#: so the shape is pinned rather than left to whoever writes the row.
SCOPE_SHAPE = "jsonb_typeof(scope) = 'object' AND jsonb_typeof(scope -> 'clauses') = 'array'"

#: `AgentAudience`'s two refusals as one predicate.
DEPARTMENT_MATCHES_LEVEL = (
    "(visibility = 'department') = (department IS NOT NULL) "
    "AND (department IS NULL OR length(btrim(department)) > 0)"
)

#: `AgentCeiling.__post_init__`: a required tool outside the allowed set can never resolve,
#: so the agent is permanently broken and every request fails rather than the save.
REQUIRED_WITHIN_ALLOWED = "required_tools <@ allowed_tools"

RLS: tuple[str, ...] = (
    "ALTER TABLE agent.agent ENABLE ROW LEVEL SECURITY",
    # See the module docstring. The audience is not enforced here because enforcing it here
    # would be a second copy of a rule with one implementation, and because it would hide the
    # rows the offboarding path exists to find.
    """
    CREATE POLICY agent_visible ON agent.agent
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

#: No DELETE. An archived agent keeps its row.
#:
#: The tuple is named `GRANTS` because the repository-wide guard against a second DELETE grant
#: now reads that attribute off every migration module. It used to read only lines beginning
#: with a quoted GRANT, and `ruff format` puts a single-statement tuple on one line beginning
#: with `GRANTS:`, so this migration and 0009 were both invisible to it. A mutation here found
#: that: adding DELETE below failed this migration's own test and nothing else.
GRANTS: tuple[str, ...] = ("GRANT SELECT, INSERT, UPDATE ON agent.agent TO brain_app",)


def _create_agent() -> None:
    op.create_table(
        "agent",
        # The slug is the key. It is the identity a leash entry, a trace and a channel
        # binding all name, and a surrogate would let two rows hold one slug while the
        # leash pointed at whichever the resolver happened to find first.
        sa.Column("id", sa.String(60), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("persona", sa.String(2000), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        # ------------------------------------------------------------------ audience
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("department", sa.String(60), nullable=True),
        # ----------------------------------------------------------------- authority
        # No server default, and `capabilities` below has one. The directions differ: a
        # missing scope narrows no rows, so a forgotten column widens what the ceiling
        # admits, while a missing capability list keeps nothing and the agent refuses to
        # start. `gate.capability_grant.scope` refuses a default for the same reason.
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.ARRAY(sa.String(200)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "allowed_tools",
            postgresql.ARRAY(sa.String(80)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "required_tools",
            postgresql.ARRAY(sa.String(80)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("max_side_effect", sa.String(16), nullable=False, server_default="none"),
        # ----------------------------------------------------------------- lifecycle
        # Neither of these is a foreign key. An agent has to outlive the account that built
        # it, and a foreign key on the steward would refuse to retire an account until every
        # agent had been handed on, which is a workflow rule enforced by a deadlock.
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        # Declared in the order the model declares them, so the rendered DDL is
        # character-for-character what `CreateTable` produces from the model. A comparison on
        # rendered SQL is sensitive to constraint order.
        sa.CheckConstraint(SLUG_GRAMMAR, name="slug_grammar"),
        sa.CheckConstraint("length(btrim(display_name)) > 0", name="display_name_present"),
        sa.CheckConstraint("length(btrim(persona)) > 0", name="persona_present"),
        sa.CheckConstraint(TIER_IN, name="tier"),
        sa.CheckConstraint(VISIBILITY_IN, name="visibility"),
        sa.CheckConstraint("length(btrim(owner_id)) > 0", name="owner_present"),
        sa.CheckConstraint(DEPARTMENT_MATCHES_LEVEL, name="department_matches_level"),
        sa.CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        sa.CheckConstraint(REQUIRED_WITHIN_ALLOWED, name="required_within_allowed"),
        sa.CheckConstraint(SIDE_EFFECT_IN, name="max_side_effect"),
        sa.CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        schema="agent",
    )
    # The selection path. Partial, because a disabled or archived agent is never in a
    # selection set and indexing it would make the common query read rows it discards.
    op.create_index(
        "ix_agent_selectable",
        "agent",
        ["visibility", "department"],
        schema="agent",
        postgresql_where=sa.text("disabled_at IS NULL AND archived_at IS NULL"),
    )
    # The offboarding path, and not partial: the agents that most need a new steward include
    # the ones somebody disabled on their way out.
    op.create_index("ix_agent_owner_id", "agent", ["owner_id"], schema="agent")


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 through 0013 do; this keeps
    # the constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)

    _create_agent()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policy, the indexes and the table privileges belong to the table and go with it,
    # and this migration creates no function and no trigger, so dropping the table is the
    # whole reversal. `agent` is not dropped: 0001 created it and 0001's downgrade owns it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
