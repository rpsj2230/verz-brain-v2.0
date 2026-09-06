"""The two template tables, and the migration that has to agree with them.

Two independent hand-written descriptions of one pair of tables: the models, which say what
the code expects, and the migration, which says what was built. Nothing but this file
compares them, which is the arrangement every other migration in this repository has.

Three of the properties here are not shape at all.

**The seal is a check constraint or it is nothing.** `brain.agents.template.check_overlay`
refuses a sealed path with a sentence somebody can act on and runs only for a caller who
came through that module. The writes a seal exists for are the other ones: a seed script,
an UPDATE run by hand to unblock a release. So the tests below read the seal off the DDL
the migration emits, not off the model's constant and not off the validator.

**Immutability is a privilege, not a frozen class.** `agent.template_version` is granted
SELECT and INSERT and no policy on it admits an update, which is what makes a published
manifest unamendable. A test for that has to look at the grants.

**The path lists are compared between two modules that do not import each other.** The
model generates its constraints from `brain.agents.template`; the migration copies the
paths as literals. Comparing the model's constraint against the constant it was built from
would be comparing a value with itself, so every path assertion here reads the migration's
copy and checks it against the domain module.

Task ids: M13.2.1, M13.2.3, M13.2.4, M13.2.5, M13.2.6
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

from brain.agents.template import MANIFEST_PATHS, SEALED_PATHS, SETTABLE_PATHS
from brain.core.department import SLUG_PATTERN
from brain.db import metadata
from brain.ops.migration_policy import check_file
from brain.tables.template import TemplateInstanceRow, TemplateVersionRow

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0016_template.py"

_DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

VERSION_TABLE = "agent.template_version"
INSTANCE_TABLE = "agent.template_instance"

#: The columns `agent.agent` uses to say who may see an agent. Named here so the assertion
#: that neither template table carries one reads as the rule rather than as a spelling.
AUDIENCE_COLUMNS = frozenset({"visibility", "audience", "owner_id", "department"})


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0016", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that these tests read this rather
    than the file's text: a constraint sitting in a constant that `upgrade` never executes
    would pass a source-text search and build nothing, which for a seal is the difference
    between a fence and a comment.
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


def _table(qualified: str) -> Table:
    mapped = {
        VERSION_TABLE: TemplateVersionRow.__table__,
        INSTANCE_TABLE: TemplateInstanceRow.__table__,
    }[qualified]
    assert isinstance(mapped, Table)
    return mapped


def _checks(qualified: str) -> dict[str, str]:
    """Every check constraint a model declares, keyed by the name the model writes.

    `brain.db`'s naming convention prefixes each one with the table, so `slug_grammar`
    reaches PostgreSQL as `ck_template_version_slug_grammar`. Keyed on the suffix here, as
    `tests/unit/test_agent_tables.py` does, so these read as the rule rather than as the
    convention that renamed it.
    """
    _schema, _, name = qualified.partition(".")
    return {
        str(c.name).removeprefix(f"ck_{name}_"): _squash(str(c.sqltext))
        for c in _table(qualified).constraints
        if isinstance(c, CheckConstraint)
    }


def _quoted(sql: str) -> tuple[str, ...]:
    return tuple(re.findall(r"'([a-z_.]+)'", sql))


# ------------------------------------------------- the model and the migration agree
def test_the_migration_builds_the_tables_the_models_declare() -> None:
    """The model and the migration are written separately on purpose, and drift between
    them is invisible until a deploy: the code queries a column PostgreSQL does not have,
    or a check constraint refuses a row the type admits.

    Compared on rendered DDL rather than on the file's text, so a constant nobody executes
    cannot satisfy it. Delete this and the next column added to either model exists only in
    Python."""
    assert _migration().TABLES == (VERSION_TABLE, INSTANCE_TABLE)
    upgrade = _squash(_rendered("upgrade"))
    for qualified in (VERSION_TABLE, INSTANCE_TABLE):
        expected = _squash(str(CreateTable(_table(qualified)).compile(dialect=_DIALECT)))
        assert expected in upgrade, qualified


def test_the_migration_creates_the_index_the_upgrade_path_reads() -> None:
    """An index that exists only in the model is an index that is never built. This one is
    the query M13.4 runs to decide which installs see an upgrade badge, over a table that
    grows with every agent in the company."""
    upgrade = _squash(_rendered("upgrade"))
    indexes = sorted(_table(INSTANCE_TABLE).indexes, key=lambda i: i.name or "")
    assert [i.name for i in indexes] == ["ix_template_instance_pin"]
    for index in indexes:
        assert _squash(str(CreateIndex(index).compile(dialect=_DIALECT))) in upgrade


def test_the_downgrade_drops_what_the_upgrade_built_and_in_the_reverse_order() -> None:
    """A migration with no way back is a deploy with no way back, and the order matters
    here: the instance holds a foreign key into the version, so dropping the version first
    fails on a table something still references.

    The `agent` schema is not dropped: 0001 created all ten and 0001's downgrade owns
    them."""
    down = _squash(_rendered("downgrade"))
    assert down.index(f"DROP TABLE {INSTANCE_TABLE}") < down.index(f"DROP TABLE {VERSION_TABLE}")
    assert "DROP SCHEMA" not in down


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists and does something, no unreviewed
    autogeneration markers, no rename written as a drop plus an add."""
    assert check_file(MIGRATION) == []


def test_the_migration_follows_the_one_before_it() -> None:
    """A revision that does not chain is a migration Alembic never runs, and the symptom is
    a table that exists in every test and in no database."""
    module = _migration()
    assert module.revision == "0016"
    assert module.down_revision == "0015"


def test_the_migration_changes_no_data() -> None:
    """A schema change reverses and a data change usually cannot, so combining them makes
    the whole thing one way. This migration creates two tables and nothing else."""
    emitted = _squash(_rendered("upgrade")).upper()
    for statement in ("INSERT INTO", "DELETE FROM", "UPDATE AGENT."):
        assert statement not in emitted, f"the migration emits {statement}"


# ------------------------------------------------------- M13.2.1 the manifest is immutable
def test_the_manifest_table_is_never_granted_an_update() -> None:
    """This grant is the whole of what "immutable" means here.

    A published manifest is a promise somebody signed. Amended, it is a different promise
    under the same version number, and every instance pinned to it starts materialising
    from the new body with no upgrade badge, no diff and nobody asked. A frozen pydantic
    model says nothing at all to a psql session; a withheld privilege does.

    Delete this and `UPDATE agent.template_version SET document = ...` becomes available to
    the application, and the pin's third field is the only thing left standing between a
    republished body and every install of it."""
    grants = {
        statement.split(" ON ")[1]: statement
        for statement in _migration().GRANTS
        if " ON " in statement
    }
    assert "UPDATE" not in grants[f"{VERSION_TABLE} TO brain_app"]
    assert "UPDATE" in grants[f"{INSTANCE_TABLE} TO brain_app"]
    assert f"GRANT SELECT, INSERT ON {VERSION_TABLE} TO brain_app" in _squash(_rendered("upgrade"))


def test_no_policy_on_the_manifest_table_admits_an_amendment() -> None:
    """PostgreSQL denies what no policy admits, so the absence is a second refusal sitting
    underneath the withheld privilege, for every role that cannot bypass row-level security
    - which, per 0001, is every role this system owns.

    Asserted as the absence of an UPDATE or ALL policy rather than as the presence of the
    two that exist, so a `FOR ALL` policy added later as a tidy-up is caught."""
    policies = _squash("\n".join(_migration().RLS))
    assert f"CREATE POLICY template_version_readable ON {VERSION_TABLE} FOR SELECT" in policies
    assert f"CREATE POLICY template_version_publishable ON {VERSION_TABLE} FOR INSERT" in policies
    assert f"ON {VERSION_TABLE} FOR UPDATE" not in policies
    assert f"ON {VERSION_TABLE} FOR DELETE" not in policies
    assert f"ON {VERSION_TABLE} FOR ALL" not in policies


def test_the_manifest_table_carries_no_updated_at() -> None:
    """A column that can never move is a column that tells a reader something untrue, and
    the reader here is somebody working out whether a manifest was tampered with. `signed_at`
    is the time anybody actually asks about."""
    columns = _table(VERSION_TABLE).columns
    assert "created_at" in columns
    assert "updated_at" not in columns
    assert "updated_at" in _table(INSTANCE_TABLE).columns


def test_neither_table_is_ever_granted_delete() -> None:
    """A manifest is referred to by every instance pinned to it, and an instance is the
    record of why an agent is configured the way it is. The one DELETE grant in this system
    belongs to `auth.directory_role_grant`."""
    assert all("DELETE" not in statement for statement in _migration().GRANTS)


def test_row_level_security_is_enabled_on_both_tables() -> None:
    """`sweep_rls` fails the build on a table in a named schema without it, and a policy on
    a table where row-level security is not enabled is a policy PostgreSQL never consults.
    The two statements are separate and forgetting the first is silent."""
    sql = _squash("\n".join(_migration().RLS))
    for qualified in (VERSION_TABLE, INSTANCE_TABLE):
        assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" in sql


# --------------------------------------------------- M13.2.6 the five sealed paths
def test_the_seal_is_a_check_constraint_in_the_ddl_the_migration_emits() -> None:
    """The property the whole leaf turns on, asserted where it is enforced.

    `check_overlay` is a message for a caller who came through the module. This is the
    fence, and it has to be in the SQL rather than in a Python constant a test can read: a
    constraint declared and never emitted refuses nothing at all.

    Delete this and the seal can quietly become validator-only, which is the state a direct
    write goes around."""
    upgrade = _squash(_rendered("upgrade"))
    assert "CONSTRAINT ck_template_instance_sealed_paths_are_absent CHECK" in upgrade
    assert "CONSTRAINT ck_template_instance_overlay_paths_are_settable CHECK" in upgrade


def test_the_seal_names_the_five_paths_the_domain_module_seals() -> None:
    """The migration's hand-copied list against `brain.agents.template.SEALED_PATHS`, which
    is another module's constant that this file does not generate anything from. The model
    builds its constraint from that tuple, so comparing the model's own SQL against it
    would be comparing a value with itself.

    Read off the predicate the migration actually builds as well as off the constant it
    builds it from, because those are two separable facts and only the first is enforced. A
    mutation pointing `OVERLAY_PATHS_ARE_SETTABLE` at the wrong list left the constant
    correct and the constraint wrong, and only the DDL comparison noticed, by a test whose
    name says nothing about seals.

    Delete this and a path sealed in Python is settable in the database, which is the
    direction that matters: the seal in Python is the one that can be bypassed."""
    module = _migration()
    assert _quoted(module.SEALED_PATHS) == tuple(SEALED_PATHS)
    assert _quoted(module.SEALED_PATHS_ARE_ABSENT) == tuple(SEALED_PATHS)
    assert _checks(INSTANCE_TABLE)["sealed_paths_are_absent"] == _squash(
        module.SEALED_PATHS_ARE_ABSENT
    )
    assert len(SEALED_PATHS) == 5


def test_the_overlay_constraint_admits_exactly_the_settable_paths() -> None:
    """The companion rule, compared the same way. This is what refuses `guardrails` and
    `guardrails.leash.0.rung`: neither is one of the twelve, so neither survives the
    subtraction, and neither equals a sealed path so the constraint above cannot see them.

    Asserted on the predicate as well as on the constant, for the reason the test above
    gives: pointing the predicate at the full path list leaves the constant right and the
    constraint admitting every sealed path.

    Delete this and the settable list in the database drifts from the one the type enforces,
    and the first symptom is an install refused for a field the console offered."""
    module = _migration()
    assert _quoted(module.SETTABLE_PATHS) == tuple(SETTABLE_PATHS)
    assert _quoted(module.MANIFEST_PATHS) == tuple(MANIFEST_PATHS)
    assert _quoted(module.OVERLAY_PATHS_ARE_SETTABLE) == tuple(SETTABLE_PATHS)
    assert _checks(INSTANCE_TABLE)["overlay_paths_are_settable"] == _squash(
        module.OVERLAY_PATHS_ARE_SETTABLE
    )


def test_the_two_overlay_constraints_are_independent_and_neither_implies_the_other() -> None:
    """They are not two copies of one rule, and this is the property that says so.

    The settable rule admits a sealed path, because a sealed path is a real path in a real
    manifest. The sealed rule admits an unknown one, because it names five strings. Only
    together do they say "a known path that is not sealed".

    Delete this and one of them looks redundant and gets removed as a tidy-up. Removing the
    settable rule reopens every spelling that reaches a sealed value without equalling it;
    removing the sealed rule makes the seal depend on a list that grows every time somebody
    adds a field."""
    sealed_sql = _checks(INSTANCE_TABLE)["sealed_paths_are_absent"]
    settable_sql = _checks(INSTANCE_TABLE)["overlay_paths_are_settable"]
    # The sealed rule says nothing about unknown paths: it names five strings and no others.
    assert set(_quoted(sealed_sql)) == set(SEALED_PATHS)
    # The settable rule says nothing about sealed paths: none of them appears in it, so on
    # its own it would delete every settable key and leave a sealed one behind, refusing the
    # row for the right reason but by a rule that never mentions the seal.
    assert not set(_quoted(settable_sql)) & set(SEALED_PATHS)


def test_no_settable_path_is_an_ancestor_or_a_descendant_of_a_sealed_one() -> None:
    """The property that makes the allow list close the spellings the deny list cannot see.

    If `guardrails` were a settable path it would survive the subtraction, and setting it
    would replace both sealed paths under it without naming either. If a sealed path had a
    settable descendant, the leaf could be written directly. Neither is true today and
    neither is prevented by anything except this test.

    Delete this and adding a section-level path to `MANIFEST_PATHS` reopens the seal, while
    every other test in both files goes on passing."""
    for settable in SETTABLE_PATHS:
        for sealed in SEALED_PATHS:
            assert settable != sealed
            assert not sealed.startswith(f"{settable}."), f"{settable} contains {sealed}"
            assert not settable.startswith(f"{sealed}."), f"{settable} is inside {sealed}"


def test_the_ownership_map_is_checked_against_every_path_and_not_only_the_settable_ones() -> None:
    """A sealed path still has an owner and it is whoever published the manifest, so the
    ownership column's vocabulary is the whole manifest rather than the overlay's subset.

    Checked against the migration's copy of the full path list. Delete this and the
    constraint narrows to the settable twelve, and `ownership`'s answer for the five sealed
    paths becomes a row the database refuses."""
    predicate = _squash(_migration().FIELD_OWNER_PATHS_ARE_KNOWN)
    assert _quoted(predicate) == tuple(MANIFEST_PATHS)
    assert _checks(INSTANCE_TABLE)["field_owner_paths_are_known"] == predicate


def test_a_document_column_is_pinned_to_the_manifests_own_path_list() -> None:
    """Both halves: nothing missing and nothing extra. A document short of a path is a
    manifest this codebase cannot flatten, and one with a path nobody declared is a field
    somebody added without a migration.

    Two constraints rather than one, so a mutation to either is caught by name."""
    version_checks = _checks(VERSION_TABLE)
    instance_checks = _checks(INSTANCE_TABLE)
    assert _quoted(version_checks["document_holds_every_path"]) == tuple(MANIFEST_PATHS)
    assert _quoted(version_checks["document_holds_no_other_path"]) == tuple(MANIFEST_PATHS)
    assert _quoted(instance_checks["effective_holds_every_path"]) == tuple(MANIFEST_PATHS)
    assert _quoted(instance_checks["effective_holds_no_other_path"]) == tuple(MANIFEST_PATHS)


# ------------------------------------------------------------------ shape and the pin
def test_the_slug_pattern_reaches_postgresql_as_the_pattern_python_enforces() -> None:
    """A colon in a check constraint is a bind parameter unless it is escaped, and nothing
    reports it.

    `CheckConstraint` parses its argument as `text()`. `SLUG_PATTERN` contains `(?:`, so the
    unescaped form renders as `(?NULL[a-z0-9]+)*`: a null bind where the non-capturing group
    was. Measured against PostgreSQL 18.6, the DDL is accepted and the first INSERT fails
    with "invalid regular expression: quantifier operand invalid", which is what 0015 exists
    to repair for three tables in 0003.

    Asserted on the **compiled** DDL, because `str(constraint.sqltext)` prints the parameter
    marker back whether or not the colon was escaped: `text()` normalises the escape at
    construction. A version of this test written against `sqltext` passes for both forms,
    which a mutation found on `brain.tables.agent`.

    Compared against `brain.core.department.SLUG_PATTERN`, another module's constant, so it
    cannot pass by agreeing with itself."""
    for qualified, column in ((VERSION_TABLE, "template_id"), (INSTANCE_TABLE, "id")):
        ddl = _squash(str(CreateTable(_table(qualified)).compile(dialect=_DIALECT)))
        assert f"{column} ~ '{SLUG_PATTERN}'" in ddl, ddl
        assert "NULL[a-z0-9]" not in ddl


def test_an_instance_points_at_the_version_it_pins() -> None:
    """An instance pinned to a manifest that does not exist is an agent nobody can
    materialise, and the pair is what the pin names. A single-column key on `template_id`
    would let an instance claim a version that was never published.

    `agent.template_version` never loses a row, so this key blocks nothing anybody wants to
    do."""
    keys = {
        tuple(c.name for c in fk.columns): fk.referred_table.fullname
        for fk in _table(INSTANCE_TABLE).foreign_key_constraints
    }
    assert keys == {("template_id", "template_version"): VERSION_TABLE}
    assert _table(VERSION_TABLE).foreign_keys == set()


def test_neither_template_table_carries_an_audience_column() -> None:
    """`AUDIENCE_IS_NOT_AUTHORITY`, at schema level, for a configuration that travels
    between installations.

    A manifest with a visibility column would publish an agent into a company it has never
    seen. An instance with one would be a second copy of a fact `agent.agent` already holds,
    and the day the two disagree the one selection reads is whichever was written last.

    Delete this and a `visibility` column arrives on the instance as a convenience for a
    listing, and there are two answers to who may see an agent."""
    for qualified in (VERSION_TABLE, INSTANCE_TABLE):
        for column in _table(qualified).columns:
            assert column.name not in AUDIENCE_COLUMNS, f"{qualified}.{column.name}"


def test_the_materialised_columns_have_no_server_default() -> None:
    """A row with no effective document is an agent nobody can run, and a default would
    make it look configured: an empty JSONB object satisfies every other constraint on the
    column except the two that demand the manifest's paths, and a default that is always
    refused is a column that can never be omitted for a confusing reason.

    The overlay and the ownership map do default to an empty object, and that is the safe
    direction: an install with no local edits is exactly what was published."""
    columns = _table(INSTANCE_TABLE).columns
    assert columns["effective_document"].server_default is None
    assert columns["effective_hash"].server_default is None
    assert columns["overlay"].server_default is not None
    assert columns["field_owners"].server_default is not None
