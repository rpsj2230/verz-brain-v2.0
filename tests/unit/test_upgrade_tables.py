"""The decline table, and the migration that has to agree with it.

Two independent hand-written descriptions of one table: the model, which says what the code
expects, and the migration, which says what was built. Nothing but this file compares them,
which is the arrangement every other migration in this repository has.

Two of the properties here are not shape at all.

**Durability is a pair of missing privileges.** A decline is worth recording only if it
survives, and what makes it survive is that `agent.upgrade_decline` is granted SELECT and
INSERT and nothing else: no UPDATE, so a decline cannot be edited into naming a different
version, and no DELETE, so it cannot be quietly withdrawn to make a badge come back. A test
for that has to read the grants and the policies rather than the columns.

**The primary key is an argument, not a shape.** Declining version 3 must say nothing about
version 4, which is why the version is in the key and the template alone is not. Keyed by
template, one decline would silence every future version for ever, and the install would sit
behind with nobody told.

The widths and the grammars below are compared against `brain.agents.model`,
`brain.audit.ledger` and `brain.core.department`, which are modules this file does not
generate anything from, so no assertion here passes by agreeing with itself.

Task ids: M13.4.5
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from brain.agents.model import AGENT_ID_CHARS, OWNER_ID_CHARS
from brain.agents.upgrade import Decline
from brain.audit.ledger import DIGEST_CHARS
from brain.core.department import SLUG_PATTERN
from brain.db import metadata
from brain.ops.migration_policy import check_file
from brain.tables.upgrade import UpgradeDeclineRow

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0017_upgrade_decline.py"

_DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

DECLINE_TABLE = "agent.upgrade_decline"
VERSION_TABLE = "agent.template_version"
INSTANCE_TABLE = "agent.template_instance"

#: The columns `agent.agent` uses to say who may see an agent. Named here so the assertion
#: that this table carries none reads as the rule rather than as a spelling.
AUDIENCE_COLUMNS = frozenset({"visibility", "audience", "owner_id", "department"})


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0017", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that these tests read this rather
    than the file's text: a statement sitting in a constant that `upgrade` never executes
    would pass a source-text search and build nothing, which for a grant is the difference
    between a rule and a comment.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    with Operations.context(context):
        getattr(_migration(), direction)()
    return buffer.getvalue()


def _squash(text: str) -> str:
    return " ".join(text.split())


def _table() -> Table:
    mapped = UpgradeDeclineRow.__table__
    assert isinstance(mapped, Table)
    return mapped


# ------------------------------------------------- the model and the migration agree
def test_the_migration_builds_the_table_the_model_declares() -> None:
    """The model and the migration are written separately on purpose, and drift between them
    is invisible until a deploy: the code queries a column PostgreSQL does not have, or a
    check constraint refuses a row the type admits.

    Compared on rendered DDL rather than on the file's text, so a constant nobody executes
    cannot satisfy it. Delete this and the next column added to the model exists only in
    Python."""
    assert _migration().TABLES == (DECLINE_TABLE,)
    expected = _squash(str(CreateTable(_table()).compile(dialect=_DIALECT)))
    assert expected in _squash(_rendered("upgrade"))


def test_the_downgrade_drops_what_the_upgrade_built() -> None:
    """A migration with no way back is a deploy with no way back.

    The `agent` schema is not dropped: 0001 created all ten and 0001's downgrade owns
    them."""
    down = _squash(_rendered("downgrade"))
    assert f"DROP TABLE {DECLINE_TABLE}" in down
    assert "DROP SCHEMA" not in down


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists and does something, no unreviewed
    autogeneration markers, no rename written as a drop plus an add, no not-null column
    without a server default."""
    assert check_file(MIGRATION) == []


def test_the_migration_follows_the_one_before_it() -> None:
    """A revision that does not chain is a migration Alembic never runs, and the symptom is
    a table that exists in every test and in no database."""
    module = _migration()
    assert module.revision == "0017"
    assert module.down_revision == "0016"


def test_the_migration_changes_no_data() -> None:
    """A schema change reverses and a data change usually cannot, so combining them makes the
    whole thing one way. This migration creates one table and nothing else."""
    emitted = _squash(_rendered("upgrade")).upper()
    for statement in ("INSERT INTO", "DELETE FROM", "UPDATE AGENT."):
        assert statement not in emitted, f"the migration emits {statement}"


# ------------------------------------------------------- M13.4.5 a decline is for ever
def test_a_decline_is_never_granted_an_update_or_a_delete() -> None:
    """This pair of omissions is the whole of what "declined for ever" means once a row
    exists.

    Without UPDATE, a decline cannot be edited into naming a different version. Without
    DELETE, it cannot be quietly withdrawn so that the badge comes back, and a badge that
    comes back after somebody said no is a badge people learn to click through. Changing your
    mind is accepting the upgrade, which moves the pin on `agent.template_instance` and
    leaves this row standing.

    Delete this and `UPDATE agent.upgrade_decline SET version = ...` becomes available to the
    application, which is a decline of whatever the next version turns out to be."""
    grants = _migration().GRANTS
    assert grants == ("GRANT SELECT, INSERT ON agent.upgrade_decline TO brain_app",)
    assert all("UPDATE" not in statement for statement in grants)
    assert all("DELETE" not in statement for statement in grants)
    assert f"GRANT SELECT, INSERT ON {DECLINE_TABLE} TO brain_app" in _squash(_rendered("upgrade"))


def test_no_policy_admits_amending_or_withdrawing_a_decline() -> None:
    """PostgreSQL denies what no policy admits, so the absence is a second refusal sitting
    underneath the withheld privileges, for every role that cannot bypass row-level security,
    which per 0001 is every role this system owns.

    Asserted as the absence of an UPDATE, DELETE or ALL policy rather than as the presence of
    the two that exist, so a `FOR ALL` policy added later as a tidy-up is caught."""
    policies = _squash("\n".join(_migration().RLS))
    assert f"CREATE POLICY upgrade_decline_readable ON {DECLINE_TABLE} FOR SELECT" in policies
    assert f"CREATE POLICY upgrade_decline_recordable ON {DECLINE_TABLE} FOR INSERT" in policies
    assert f"ON {DECLINE_TABLE} FOR UPDATE" not in policies
    assert f"ON {DECLINE_TABLE} FOR DELETE" not in policies
    assert f"ON {DECLINE_TABLE} FOR ALL" not in policies


def test_row_level_security_is_enabled_on_the_table() -> None:
    """`sweep_rls` fails the build on a table in a named schema without it, and a policy on a
    table where row-level security is not enabled is a policy PostgreSQL never consults. The
    two statements are separate and forgetting the first is silent."""
    assert f"ALTER TABLE {DECLINE_TABLE} ENABLE ROW LEVEL SECURITY" in _squash(
        "\n".join(_migration().RLS)
    )


def test_the_key_names_the_version_so_declining_one_says_nothing_about_the_next() -> None:
    """The argument the primary key encodes.

    Keyed by the instance and the template alone, one decline would silence every future
    version of that template for ever: the badge would never come back, and the install would
    sit behind with nobody told. The version is therefore in the key, and the row is one
    decline of one version rather than a mute switch.

    Delete this and the version can be dropped from the key as a simplification, which looks
    identical on the day it is written and is invisible from then on."""
    assert [c.name for c in _table().primary_key.columns] == [
        "instance_id",
        "template_id",
        "version",
    ]


def test_the_declined_body_is_recorded_and_is_not_part_of_the_key() -> None:
    """A decline names a body as well as a number, for the reason the pin carries a digest: a
    version republished with a different body is a body nobody reviewed, and a decline that
    went on hiding it would turn a refusal to read into a refusal to be shown.

    It stays out of the key because the foreign key below points at `agent.template_version`,
    where the pair is unique: two declines of one version would be two bodies the target
    table cannot tell apart.

    Delete this and the column can be removed as redundant, and the decline covers whatever
    that version becomes."""
    columns = _table().columns
    assert "content_digest" in columns
    assert columns["content_digest"].nullable is False
    assert "content_digest" not in [c.name for c in _table().primary_key.columns]
    assert f"content_digest ~ '^[0-9a-f]{{{DIGEST_CHARS}}}$'" in _squash(
        str(CreateTable(_table()).compile(dialect=_DIALECT))
    )


def test_a_decline_belongs_to_an_install_and_names_a_version_that_was_published() -> None:
    """Both keys, and both point at tables 0016 built.

    A decline for an install that does not exist is a row nobody can read, and a decline of a
    version nobody published is a decline of nothing. Neither key blocks anything anybody
    wants to do, because no DELETE grant exists on either target.

    Delete this and a decline can name a version that was never published, which is the row
    that would silence the badge for a version that then arrives."""
    keys = {
        tuple(c.name for c in fk.columns): fk.referred_table.fullname
        for fk in _table().foreign_key_constraints
    }
    assert keys == {
        ("template_id", "version"): VERSION_TABLE,
        ("instance_id",): INSTANCE_TABLE,
    }


def test_the_slug_pattern_reaches_postgresql_as_the_pattern_python_enforces() -> None:
    """A colon in a check constraint is a bind parameter unless it is escaped, and nothing
    reports it at DDL time.

    `CheckConstraint` parses its argument as `text()`. `SLUG_PATTERN` contains `(?:`, so the
    unescaped form renders as `(?NULL[a-z0-9]+)*`: a null bind where the non-capturing group
    was. Measured against PostgreSQL 18.6, the DDL is accepted and the first INSERT fails
    with "invalid regular expression: quantifier operand invalid", which is what 0015 exists
    to repair for three tables in 0003.

    Asserted on the **compiled** DDL, because `str(constraint.sqltext)` prints the parameter
    marker back whether or not the colon was escaped: `text()` normalises the escape at
    construction, and a version of this test written against `sqltext` passes for both forms.

    Compared against `brain.core.department.SLUG_PATTERN`, another module's constant, so it
    cannot pass by agreeing with itself."""
    ddl = _squash(str(CreateTable(_table()).compile(dialect=_DIALECT)))
    for column in ("instance_id", "template_id"):
        assert f"{column} ~ '{SLUG_PATTERN}'" in ddl, ddl
    assert "NULL[a-z0-9]" not in ddl


def test_the_columns_are_as_wide_as_the_types_they_hold() -> None:
    """The migration's hand-typed widths against the domain modules' own constants.

    Read off the DDL the migration emits rather than off the model, which is generated from
    those constants and would be comparing a value with itself. A column narrower than the
    type it stores refuses a legal value, and the first symptom is a decline that will not
    save for one person and saves for everybody else.

    Delete this and the model can widen an id while the database keeps the old bound."""
    upgrade = _squash(_rendered("upgrade"))
    assert f"instance_id VARCHAR({AGENT_ID_CHARS})" in upgrade
    assert f"template_id VARCHAR({AGENT_ID_CHARS})" in upgrade
    assert f"content_digest VARCHAR({DIGEST_CHARS})" in upgrade
    assert f"declined_by VARCHAR({OWNER_ID_CHARS})" in upgrade


def test_the_row_carries_exactly_what_the_domain_type_carries() -> None:
    """The model mirrors `brain.agents.upgrade.Decline`, and this is what says so.

    `created_at` is the one column with no counterpart, and it is deliberately not
    `declined_at`: one is when the row arrived and the other is when a person decided.

    Delete this and a field added to `Decline` has nowhere to be stored, or a column arrives
    here that nothing in the domain ever fills."""
    columns = {c.name for c in _table().columns}
    assert columns - {"created_at"} == set(Decline.model_fields)


def test_the_table_carries_no_updated_at_and_no_deleted_at() -> None:
    """A row that can never be updated with an `updated_at` on it is a column that tells a
    reader something untrue, which is `obs.audit_entry`'s argument and
    `agent.template_version`'s. A `deleted_at` would be worse: a soft delete is a withdrawal
    written as a column, and a decline that can be withdrawn is a badge that comes back."""
    columns = {c.name for c in _table().columns}
    assert "created_at" in columns
    assert "updated_at" not in columns
    assert "deleted_at" not in columns


def test_the_decline_time_has_no_server_default() -> None:
    """This is when a person decided, not when the row arrived, and the two differ by however
    long the path took. `agent.template_version.signed_at` and `obs.audit_entry.at` both
    refuse a default for the same reason.

    Delete this and `declined_at` picks up `now()`, and the answer to "when did we say no to
    this" becomes the time of whatever write happened to carry it."""
    columns = _table().columns
    assert columns["declined_at"].server_default is None
    assert columns["created_at"].server_default is not None


def test_the_table_carries_no_audience_column() -> None:
    """`AUDIENCE_IS_NOT_AUTHORITY` at schema level. Who may see the agent a decline concerns
    lives on `agent.agent` and in one place; a copy here would be a second answer, and the day
    the two disagree the one selection reads is whichever was written last."""
    for column in _table().columns:
        assert column.name not in AUDIENCE_COLUMNS, column.name


def test_there_is_no_index_beyond_the_primary_key() -> None:
    """The only question asked of this table is whether one instance declined one version of
    one template, which is the primary key exactly.

    An index on `template_id` alone would serve a listing across the estate, and there is no
    such listing: `brain.agents.upgrade` argues that one has to apply the agent audience
    first and holds no agent records to apply it with. An index built for a query nobody
    makes is a write cost nobody chose."""
    assert _table().indexes == set()
    assert "CREATE INDEX" not in _rendered("upgrade").upper()
