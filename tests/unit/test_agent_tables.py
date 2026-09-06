"""The agent table, and the migration that has to agree with it.

Two independent hand-written descriptions of one table: the model, which says what the code
expects, and the migration, which says what was built. Nothing but this file compares them,
which is the arrangement every other migration in this repository has and the reason each
one has a file like this.

The properties here are not only shape. Three of them are the schema half of the rule
`brain.agents.model` exists to keep: no constraint reads the audience off the ceiling or the
ceiling off the audience, the two vocabularies are the ones other modules already own rather
than copies of them, and the column that widens by omission is the one with no default.

Task ids: M13.1.1, M13.1.2, M13.1.4
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from brain.core.department import SLUG_PATTERN
from brain.core.envelope import SideEffect
from brain.db import metadata
from brain.knowledge.visibility import Visibility
from brain.models.routing import TIER_LADDER, Tier
from brain.ops.migration_policy import check_file
from brain.tables.agent import AgentRow

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "versions" / "0014_agent.py"

_DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

#: The columns that decide who may see an agent, and the columns that decide what a run
#: through it may reach. Written out because the whole point is that they are two sets: a
#: derivation from one list would be a test that could not notice them merging.
AUDIENCE_COLUMNS = frozenset({"visibility", "owner_id", "department"})
AUTHORITY_COLUMNS = frozenset({"scope", "capabilities", "allowed_tools", "required_tools"})


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0014", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that these tests read this rather
    than the file's text: a statement sitting in a constant that `upgrade` never executes
    would pass a source-text search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        # `target_metadata` carries `brain.db.NAMING_CONVENTION`, which is what makes
        # `op.create_table` render `ck_agent_tier` for a constraint the migration names
        # `tier`. Without it the migration emits bare names, the comparison below fails on
        # every constraint at once, and the obvious fix is to loosen the comparison.
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    with Operations.context(context):
        getattr(_migration(), direction)()
    return buffer.getvalue()


def _squash(text: str) -> str:
    """Collapse whitespace, so a statement wrapped across lines still compares."""
    return " ".join(text.split())


def _table() -> Table:
    """The mapped table, narrowed to `Table`.

    `__table__` on a declarative class is annotated `FromClause`, which has no `indexes`
    worth reading and cannot be handed to `CreateTable`.
    """
    mapped = AgentRow.__table__
    assert isinstance(mapped, Table)
    return mapped


def _checks() -> dict[str, str]:
    """Every check constraint the model declares, keyed by the name this file writes.

    `brain.db`'s naming convention prefixes every one, so the declared `tier` is
    `ck_agent_tier` by the time it reaches PostgreSQL. Keyed on the suffix here, as
    `tests/unit/test_chat_tables.py` does, so these tests read as the rule they assert
    rather than as the convention that renamed it.
    """
    return {
        str(c.name).removeprefix("ck_agent_"): _squash(str(c.sqltext))
        for c in _table().constraints
        if isinstance(c, CheckConstraint)
    }


def _quoted(sql: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sql))


# ------------------------------------------------------- the model and the migration agree
def test_the_migration_builds_the_table_the_model_declares() -> None:
    """The model and the migration are written separately on purpose, and drift between them
    is invisible until a deploy: the code queries a column PostgreSQL does not have, or a
    check constraint refuses a row the type admits.

    Compared on rendered DDL rather than on the file's text, so a constant nobody executes
    cannot satisfy it. Delete this and the next column added to the model exists only in
    Python."""
    assert _migration().TABLES == ("agent.agent",)
    upgrade = _squash(_rendered("upgrade"))
    assert _squash(str(CreateTable(_table()).compile(dialect=_DIALECT))) in upgrade


def test_the_migration_creates_both_indexes_the_model_declares() -> None:
    """An index the model believes exists is a query plan nobody measured, and the two here
    are deliberately different shapes: one partial for the selection path, one whole for the
    offboarding path. Leaving the second out would make finding a leaver's agents a
    sequential scan of every agent in the company, which works until it is the thing being
    run against a real estate under time pressure."""
    upgrade = _squash(_rendered("upgrade"))
    indexes = sorted(_table().indexes, key=lambda i: i.name or "")
    assert [i.name for i in indexes] == ["ix_agent_owner_id", "ix_agent_selectable"]
    for index in indexes:
        assert _squash(str(CreateIndex(index).compile(dialect=_DIALECT))) in upgrade


def test_the_downgrade_drops_what_the_upgrade_built() -> None:
    """A migration with no way back is a deploy with no way back. The `agent` schema is not
    dropped: 0001 created all nine and 0001's downgrade owns them, so dropping it here would
    take four other modules' future tables with it."""
    down = _squash(_rendered("downgrade"))
    assert "DROP TABLE agent.agent" in down
    assert "DROP SCHEMA" not in down


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists and does something, no unreviewed
    autogeneration markers, no rename written as a drop plus an add."""
    assert check_file(MIGRATION) == []


# ------------------------------------------------------------------ row-level security
def test_row_level_security_is_enabled_on_the_agent_table() -> None:
    """`sweep_rls` fails the build on a table in a named schema without it, and a policy on a
    table where row-level security is not enabled is a policy PostgreSQL never consults. The
    two statements are separate and forgetting the first is silent."""
    sql = _squash("\n".join(_migration().RLS))
    assert "ALTER TABLE agent.agent ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY agent_visible ON agent.agent" in sql


def test_the_policy_does_not_hide_an_agent_from_the_offboarding_path() -> None:
    """The audience is deliberately not enforced by the policy, and this is the reason that
    is a decision rather than an omission.

    A policy restricting personal agents to their owner would hide exactly the rows
    `brain.agents.lifecycle.agents_needing_transfer` exists to find: a personal agent whose
    steward has left is reachable by no audience at all, and behind such a policy the
    application could not list it either. The agents that most need a new owner would be the
    ones nobody could see.

    Asserted as the absence of a per-caller predicate rather than as the presence of `true`,
    so a policy that grew one is caught whatever it compares against."""
    policies = _squash("\n".join(_migration().RLS))
    assert "current_setting" not in policies
    assert "USING (true)" in policies


def test_the_agent_table_is_never_granted_delete() -> None:
    """Retirement is `archived_at`, and the ledger refers to agents by id: a run recorded
    against a row that no longer exists is a trace nobody can read. The one DELETE grant in
    this system belongs to `auth.directory_role_grant`."""
    assert all("DELETE" not in statement for statement in _migration().GRANTS)


# ------------------------------------------------- audience and authority, at schema level
def test_no_check_constraint_ties_the_audience_to_the_ceiling() -> None:
    """The schema half of `AUDIENCE_IS_NOT_AUTHORITY`.

    A constraint naming a visibility column and a ceiling column together would be the
    database asserting a relation between the two axes, and a relation asserted here is one
    the application would then have to satisfy: "a company-visible agent must hold at least
    these capabilities" reads as tidy configuration and is a grant written in DDL.

    Delete this and the first constraint that couples them passes every other test in this
    file, because each one still builds what the model declares."""
    for name, predicate in _checks().items():
        mentions_audience = any(column in predicate for column in AUDIENCE_COLUMNS)
        mentions_authority = any(column in predicate for column in AUTHORITY_COLUMNS)
        assert not (mentions_audience and mentions_authority), (
            f"{name} constrains the audience and the ceiling together: {predicate}"
        )


def test_the_scope_column_has_no_default_and_the_capability_list_does() -> None:
    """The two defaults fail in opposite directions and only one of them is safe.

    A missing `scope` means the ceiling narrows no rows, so a forgotten column widens what a
    run may reach. A missing `capabilities` means the intersection keeps nothing, so a
    forgotten column produces an agent that refuses to start. Swap them and an insert that
    omits a column becomes the widest agent in the estate, silently.

    Delete this and a well-meaning `server_default` on `scope` looks like tidying up."""
    columns = _table().columns
    assert columns["scope"].server_default is None
    assert columns["capabilities"].server_default is not None
    assert columns["allowed_tools"].server_default is not None


def test_the_agent_row_carries_no_foreign_key() -> None:
    """An agent has to outlive the account that built it, so neither the steward nor the
    creator is a foreign key. A foreign key on `owner_id` would refuse to retire a principal
    until every agent had been handed on, which is a workflow rule enforced by a deadlock,
    and the workflow it blocks is the one that runs under time pressure on somebody's last
    day."""
    assert _table().foreign_keys == set()


# --------------------------------------------------------------- one vocabulary, not two
def test_the_visibility_column_holds_the_knowledge_layer_s_three_levels() -> None:
    """One visibility vocabulary in this system, not one per table.

    Asserted against `brain.knowledge.visibility.Visibility` itself, which is a different
    module's constant rather than this constraint's own text, so a fourth level added to the
    enum without a migration fails here instead of being refused by the database at three in
    the morning.

    Delete this and an agent-only spelling of these levels, `global` beside `company`, gets
    into the schema and the two never converge again."""
    assert _quoted(_checks()["visibility"]) == {level.value for level in Visibility}


def test_the_tier_column_admits_the_routing_ladder_and_not_the_fast_lane_s_tier() -> None:
    """`Tier.NONE` is the absence of the ladder, not a rung on it: `RoutingChain.select`
    returns an empty selection for it, so an agent pinned there is one that is chosen, starts
    and answers nothing.

    Compared with `TIER_LADDER`, which is where that distinction is already written down, so
    this cannot pass by agreeing with itself.

    Delete this and the constraint generated from the whole enum lets an agent be configured
    into a state where every one of its runs produces no answer and no error."""
    admitted = _quoted(_checks()["tier"])
    assert admitted == {tier.value for tier in TIER_LADDER}
    assert Tier.NONE.value not in admitted


def test_the_side_effect_ceiling_admits_the_envelope_s_whole_vocabulary() -> None:
    """The ceiling is compared against a tool's declared side effect by
    `brain.gate.catalogue.project`, so a value the envelope can produce and this column
    cannot hold would be an agent nobody can configure for a tool that exists."""
    assert _quoted(_checks()["max_side_effect"]) == {effect.value for effect in SideEffect}


def test_the_department_column_is_present_exactly_when_the_level_needs_it() -> None:
    """`AgentAudience`'s two refusals, in the database, for the row that arrived some other
    way: a department audience with no department resolves to the unrestricted scope, which
    is the widest audience wearing the middle one's name, and a department on a personal or
    company row is a field that reads as an audience and applies to nothing.

    The model's generated predicate is compared against the migration's hand-copied one, so
    the two independent descriptions have to agree. Delete this and they drift the first time
    somebody edits either."""
    assert _checks()["department_matches_level"] == _squash(_migration().DEPARTMENT_MATCHES_LEVEL)


def test_the_scope_column_is_pinned_to_the_clause_form() -> None:
    """`Scope.model_dump()` is `{"clauses": [...]}` and the console's document form is
    `{"department": "web"}`. The second read as a `Scope` is a scope with no clauses, which is
    unrestricted, so the failure mode of a missing shape check is a ceiling that narrows
    nothing at all.

    Compared against the migration's copy rather than against `brain.tables.gate.SCOPE_SHAPE`,
    which the model imports: comparing the model's constraint to the constant it was built
    from would be comparing a value with itself."""
    assert _checks()["scope_shape"] == _squash(_migration().SCOPE_SHAPE)


def test_the_slug_pattern_reaches_postgresql_as_the_pattern_python_enforces() -> None:
    """A colon in a check constraint is a bind parameter unless it is escaped, and nothing
    reports it.

    `CheckConstraint` parses its argument as `text()`. `SLUG_PATTERN` contains `(?:`, so the
    unescaped form renders as `(?NULL[a-z0-9]+)*`: a null bind where the non-capturing group
    was, and a regular expression that is not the one the type applies. The DDL compiles, the
    migration runs, and the column is constrained by something nobody wrote.

    Asserted on the **compiled** DDL and on both sides of it, which this test did not do
    when it was written: `str(constraint.sqltext)` shows `:_` whether or not the colon was
    escaped, because `text()` normalises the escape at construction and prints the parameter
    marker back. So the first version of this test passed for the escaped form and for the
    unescaped one, which a mutation found. What separates them is the compilation, where an
    unbound parameter becomes NULL.

    The pattern is compared against `brain.core.department.SLUG_PATTERN`, another module's
    constant, so this cannot pass by agreeing with itself either. Delete it and the escape
    looks like a stray backslash somebody would tidy away."""
    ddl = _squash(str(CreateTable(_table()).compile(dialect=_DIALECT)))
    assert f"id ~ '{SLUG_PATTERN}'" in ddl, ddl
    assert "NULL[a-z0-9]" not in ddl


def test_a_required_tool_outside_the_allowed_set_is_refused_by_the_database_too() -> None:
    """`AgentCeiling` refuses it at construction because such an agent can never resolve its
    catalogue and is permanently broken. This is the same rule for the row that arrived by a
    seed script or a hand-written insert during an incident, which is the class of row every
    other table here carries a duplicate check for."""
    assert _checks()["required_within_allowed"] == _squash(_migration().REQUIRED_WITHIN_ALLOWED)
