"""The agent table: a persona, a ceiling, a tier and an audience, in one row.

Mirrors `brain.agents.model.AgentRecord`, which is the definition; this is storage for it.
Every width, pattern and vocabulary below is generated from the domain type's own constants
rather than retyped, so a change to the record breaks a test instead of a deploy.

**Two columns describe reach and they are not the same column.** `visibility` says who may
see and start this agent; `scope` and `capabilities` say what a run through it may reach.
`brain.agents.model` argues why at length and this file is the reason that argument has to
survive contact with a schema: in a `psql` session both look like configuration, and the
first person to write `UPDATE agent.agent SET visibility = 'company'` on the reasonable
belief that they are publishing an agent must not thereby be widening anything it reaches.
Nothing here derives one from the other, and there is no third column that could.

**The department column is present exactly when the level needs it.**
`department_iff_department_level` is `AgentAudience`'s two refusals written as one predicate:
a department audience with no department would resolve to the unrestricted scope, which is
the widest audience wearing the middle one's name, and a department recorded on a personal
or company agent is a field that reads as an audience and applies to nothing. The type
refuses both on the way in; this refuses the row that arrived some other way, which is the
same pairing `auth.principal` and `proj.record` both carry and say so.

**`scope` has no server default and `capabilities` does, and the difference is the direction
each one fails in.** A missing scope means "narrow no rows", so a forgotten column would
quietly widen what the ceiling admits, and `gate.capability_grant` refuses a default for
exactly that reason. A missing capability list means the intersection keeps nothing, so a
forgotten column produces an agent that reaches nothing and refuses to start. One default is
safe and the other is not, so only one is written.

**Lifecycle is two nullable timestamps and not a state column.** `AgentRecord.state` derives
the three states from them, archived first. A `state` column beside the timestamps would be
two facts that can disagree, and a `state` column instead of them loses the date, which is
the only part anybody asks for after the fact.

**No foreign key on `owner_id` or `created_by`.** The same reason `capability_grant.granted_by`
and `chat.conversation.principal_id` have none: an agent has to outlive the account that
built it, and a foreign key on the steward would make the order of an offboarding
load-bearing, refusing to retire the account until every agent had been handed on. That is
a workflow constraint enforced by a deadlock, and the workflow it blocks is the one that
happens under time pressure. `brain.agents.lifecycle.agents_needing_transfer` is where that
job is expressed instead.

**`brain.db.AuditMixin` was rejected here**, as it was in `brain.tables.gate`. It would add
`created_by` beside the column already declared below, and two names for one fact is how
they come to disagree. The entitlement a change was made under belongs in `obs.audit_entry`.

Task ids: M13.1.1, M13.1.2, M13.1.4
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.agents.model import (
    AGENT_ID_CHARS,
    DEPARTMENT_CHARS,
    DISPLAY_NAME_CHARS,
    OWNER_ID_CHARS,
    PERSONA_CHARS,
)
from brain.core.department import SLUG_PATTERN
from brain.core.envelope import SideEffect
from brain.db import Base, TimestampMixin
from brain.knowledge.visibility import Visibility
from brain.models.routing import TIER_LADDER
from brain.tables.gate import CAPABILITY_CHARS, SCOPE_SHAPE, TOOL_NAME_CHARS
from brain.tables.identity import one_of

#: The vocabulary a `visibility` column may hold: the same three levels the knowledge layer
#: uses, generated from the enum rather than listed, per `one_of`.
VISIBILITY_IN = one_of("visibility", Visibility)

#: `TIER_LADDER` rather than `Tier`, and the difference is deliberate. `Tier.NONE` is the
#: fast lane's absence of a ladder, not a pool: an agent pinned to it would be routed to no
#: model at all and would answer nothing, so it is refused by `AgentRecord` on the way in and
#: by this constraint for the row that arrived some other way.
TIER_IN = one_of("tier", TIER_LADDER)

#: The largest side effect a run through this agent may have, from the envelope's own enum.
SIDE_EFFECT_IN = one_of("max_side_effect", SideEffect)

#: `AgentAudience`'s two refusals as one predicate. The equality is on booleans, which
#: PostgreSQL compares directly, so this reads as "the level is department exactly when a
#: department is recorded". The second half refuses a blank string, which satisfies
#: `IS NOT NULL` and is not a department.
DEPARTMENT_IFF_DEPARTMENT_LEVEL = (
    f"(visibility = '{Visibility.DEPARTMENT.value}') = (department IS NOT NULL) "
    "AND (department IS NULL OR length(btrim(department)) > 0)"
)

#: `AgentCeiling.__post_init__` refuses a required tool outside the allowed set, because it
#: can never resolve and the agent is permanently broken. `<@` is array containment, which
#: says the same thing in one operator and needs no subquery.
REQUIRED_WITHIN_ALLOWED = "required_tools <@ allowed_tools"

#: A colon inside a check constraint is a bind parameter unless it is escaped.
#:
#: `CheckConstraint` parses its argument as `text()`, and `text()` reads `:name` as a
#: parameter to bind later. `SLUG_PATTERN` contains one colon, in `(?:`, so the unescaped
#: form renders as `(?NULL[a-z0-9]+)*`: the non-capturing group becomes a null bind and what
#: reaches PostgreSQL is a different regular expression from the one Python enforces. There
#: is no error at any point; the DDL simply says something else.
#:
#: `brain.tables.gate` writes this pattern unescaped in three constraints and 0003 copied it
#: verbatim, so the model and the migration agree with each other and both disagree with the
#: pattern. Correcting those means altering three deployed constraints, which is a migration
#: of its own and not this leaf's to write. What is in scope here is not shipping a fourth.
_ESCAPED_COLON = "\\:"

#: The slug grammar, escaped, so the constraint that ships is the pattern `SLUG_PATTERN` is.
SLUG_GRAMMAR = "id ~ '" + SLUG_PATTERN.replace(":", _ESCAPED_COLON) + "'"


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


class AgentRow(TimestampMixin, Base):
    """`agent.agent`. One configured agent (M13.1.1).

    The primary key is the slug rather than a surrogate, because the slug is the identity
    everywhere else: a leash entry names it, a trace names it, a channel binding names it,
    and `brain.core.department.check_slug_collisions` polices it against scopes and tool
    objects in one shared namespace. A surrogate key would let two rows hold one slug and
    leave the leash pointing at whichever the resolver happened to find.

    No soft delete. Retirement here is `archived_at`, which is the same idea with a name
    that matches what a person did, and a second retirement column would make "which of
    these two means gone" a question nobody can answer from the schema.
    """

    __tablename__ = "agent"

    #: Mirrors `AgentRecord.agent_id`. Named `id` because that is what every other primary
    #: key in this schema set is called, and a column called `agent_id` in a table called
    #: `agent` reads as a foreign key to somewhere else.
    id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), primary_key=True)

    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_CHARS), nullable=False)

    #: Prompt material, stored and never parsed. Anything in here that decided a permission
    #: would be a permission decided by whoever last edited a text box.
    persona: Mapped[str] = mapped_column(String(PERSONA_CHARS), nullable=False)

    #: Which model pool answers. `TIER_IN` pins it to the three real rungs.
    tier: Mapped[str] = mapped_column(String(16), nullable=False)

    # ------------------------------------------------------------------ audience
    #: Who may see and start this agent. Not consulted by anything that decides reach.
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The current steward. Moves on transfer; `created_by` does not.
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_CHARS), nullable=False)

    #: Set exactly at the department level, per `DEPARTMENT_IFF_DEPARTMENT_LEVEL`. Null
    #: rather than an empty string, which is what the record's `""` maps to.
    department: Mapped[str | None] = mapped_column(String(DEPARTMENT_CHARS), nullable=True)

    # ----------------------------------------------------------------- authority
    #: `Scope.model_dump()`, the `{"clauses": [...]}` form, checked by `SCOPE_SHAPE`. The
    #: console's document form deserialises to a scope with no clauses, which is
    #: unrestricted, so the shape is pinned rather than left to whoever writes the row.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: What the ceiling admits. Empty is the safe default and means the intersection keeps
    #: nothing. The array's members are not regex-checked per element for the reason
    #: `gate.capability_pack` gives: the only spelling available depends on a second fact
    #: about the grammar, and a constraint that is right for a reason it does not state is
    #: worse than no constraint. `Capability` enforces it on the way in.
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(CAPABILITY_CHARS)), nullable=False, server_default=text("'{}'")
    )

    #: The tools this agent may ask for. `brain.gate.catalogue.project` intersects them with
    #: what the caller can reach, so this is a ceiling and never a grant.
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String(TOOL_NAME_CHARS)), nullable=False, server_default=text("'{}'")
    )

    #: The subset without which the agent cannot function.
    required_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String(TOOL_NAME_CHARS)), nullable=False, server_default=text("'{}'")
    )

    max_side_effect: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=SideEffect.NONE.value
    )

    # ----------------------------------------------------------------- lifecycle
    created_by: Mapped[str] = mapped_column(String(OWNER_ID_CHARS), nullable=False)

    #: Reversible.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Terminal. Nothing in `brain.agents.lifecycle` clears it.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(SLUG_GRAMMAR, name="slug_grammar"),
        CheckConstraint(_present("display_name"), name="display_name_present"),
        CheckConstraint(_present("persona"), name="persona_present"),
        CheckConstraint(TIER_IN, name="tier"),
        CheckConstraint(VISIBILITY_IN, name="visibility"),
        CheckConstraint(_present("owner_id"), name="owner_present"),
        CheckConstraint(DEPARTMENT_IFF_DEPARTMENT_LEVEL, name="department_matches_level"),
        CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        CheckConstraint(REQUIRED_WITHIN_ALLOWED, name="required_within_allowed"),
        CheckConstraint(SIDE_EFFECT_IN, name="max_side_effect"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        # The selection path's index: everything an audience test needs, over the rows that
        # can actually be chosen. Partial, because a disabled or archived agent is never in
        # a selection set and indexing it would make the common query read rows it discards.
        Index(
            "ix_agent_selectable",
            "visibility",
            "department",
            postgresql_where=text("disabled_at IS NULL AND archived_at IS NULL"),
        ),
        # The offboarding path's index, and not partial. `agents_needing_transfer` asks who
        # owns what including agents that are merely disabled, and a partial index matching
        # the selection one would miss exactly the rows that need a new steward most.
        Index("ix_agent_owner_id", "owner_id"),
        {"schema": "agent"},
    )
