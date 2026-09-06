"""The four `er` tables and the forwarding view, held to what makes them safe.

`tests/unit/test_tables.py` already proves every table has a migration and every migration has
a model. These are the properties specific to entity resolution, and they are almost all about
the same thing: a merge must not widen anybody's reach. The schema's contribution to that is
narrow and checkable. `er.canonical` has no column that could hold a value from a member,
every child row carries the source record it was observed on so the filter has something to
filter by, and the forwarding view runs as the caller rather than as its owner.

**The view's `security_invoker` clause is the one thing in this file that is a live hole if it
is wrong.** A PostgreSQL view runs with its owner's privileges by default, and row-level
security on the tables underneath is evaluated against that owner. The migration's role owns
this view, so without the clause `er.resolved_alias` reads past every policy on `er.alias`.

**Constraints are read off the compiled DDL and never off `str(constraint.sqltext)`.** That is
not a style preference. `CheckConstraint` parses its argument as `text()`, which reads `:name`
as a bind parameter, and three tables shipped a mangled regular expression for the life of the
schema because a test compared the source string rather than what PostgreSQL was sent.
Migration 0015 exists because of it.

Task ids: M14.1.1, M14.1.2, M14.1.3, M14.1.4, M14.1.5, M14.1.6
"""

from __future__ import annotations

import importlib.util
import io
import types
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import ForeignKeyConstraint, Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from brain.db import Base, metadata
from brain.resolution.canonical import (
    MAX_ALIAS_CHARS,
    MAX_FORWARD_DEPTH,
    EntityType,
    IdentifierKind,
    identifier_hash,
)
from brain.tables.resolution import RESOLVED_ALIAS_VIEW

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0020_entity_resolution.py"
)

CANONICAL: Table = Base.metadata.tables["er.canonical"]
ALIAS: Table = Base.metadata.tables["er.alias"]
IDENTIFIER: Table = Base.metadata.tables["er.identifier"]
LINK: Table = Base.metadata.tables["er.link"]
CHILDREN = (ALIAS, IDENTIFIER, LINK)

DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def migration() -> types.ModuleType:
    """The migration as a module, so its statements can be read as data rather than grepped.

    Importing runs nothing: `upgrade` and `downgrade` are functions, and `alembic.op` reaches a
    database only when one is called under a context that has one.

    Read as data rather than as text, for the reason `tests/unit/test_memory_tables.py` gives:
    an earlier draft of that file searched the migration for "FOR ALL" and matched the
    paragraph explaining that there is no FOR ALL policy, so the test passed on its own prose.
    """
    spec = importlib.util.spec_from_file_location("migration_0020", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in process. It matters that the tests read this rather than
    the file's text: a statement sitting in a constant that `upgrade` never executes would pass
    a source search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    module = migration()
    with Operations.context(context):
        getattr(module, direction)()
    return buffer.getvalue()


def squash(text: str) -> str:
    return " ".join(text.split())


def ddl(table: Table) -> str:
    """The CREATE TABLE PostgreSQL is actually sent, not the Python that built it."""
    return str(CreateTable(table).compile(dialect=DIALECT))


# ------------------------------------------------------- the migration builds the models
@pytest.mark.parametrize("table", [CANONICAL, ALIAS, IDENTIFIER, LINK])
def test_the_migration_builds_exactly_the_table_the_model_declares(table: Table) -> None:
    """The migration copies every predicate the model declares, deliberately, so that it goes
    on describing the database it built rather than whatever the models say next year. This is
    what turns the copy into a check rather than a duplication.

    Delete this and the two drift, and the drift is invisible: the models are what every query
    is built from and the migration is what the server actually ran, so a constraint present in
    one and absent in the other is a rule that exists in tests and not in production.
    """
    assert squash(ddl(table)) in squash(rendered("upgrade"))


def test_the_migration_drops_the_view_and_every_table_it_builds() -> None:
    """A deploy with no way back is what the migration policy exists to refuse, and a
    downgrade that forgets the view fails on a dependent object rather than reversing.

    Delete this and the view survives its own tables, and the next upgrade meets an object it
    did not create and cannot describe.
    """
    down = squash(rendered("downgrade"))

    assert "DROP VIEW IF EXISTS er.resolved_alias" in down
    for qualified in migration().TABLES:
        schema, _, name = qualified.partition(".")
        assert f"DROP TABLE {schema}.{name}" in down
    # In reverse: `er.canonical` is dropped last, because the other three point at it.
    assert down.index("DROP TABLE er.link") < down.index("DROP TABLE er.canonical")


# ------------------------------------------------------------------ row-level security
def test_row_level_security_is_enabled_on_every_table_this_migration_builds() -> None:
    """New tables shipping with row-level security off is a live failure mode on this account,
    where it left data reachable through an anonymous key.

    Read from the migration's own statements, which is where somebody would leave it out, and
    checked against `TABLES` rather than against a list written here, so a fifth table cannot
    be added without a policy.

    Delete this and a table can be added to `er` with no policy at all, which reads as working
    because the owner role is unaffected by policies it has none of.
    """
    statements = migration().RLS

    for qualified in migration().TABLES:
        assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" in statements


def test_no_policy_and_no_grant_on_any_of_these_tables_admits_a_delete() -> None:
    """An entity that stops being current is forwarded, never removed: the whole value of the
    pointer is that an id issued before a merge still resolves, and deleting the stub is the
    one operation that breaks it. An alias and an identifier are observations.

    Both halves are asserted, because either alone is insufficient. PostgreSQL denies what no
    policy admits, and a `FOR ALL` policy would admit what the grants deliberately omit.

    Delete this and a DELETE grant is added to make a bad merge easy to clean up, and cleaning
    it up destroys every id that was ever issued for it.
    """
    module = migration()
    joined = chr(10).join(module.RLS)

    for grant in module.GRANTS:
        assert "DELETE" not in grant
        assert "ALL" not in grant
    assert "FOR ALL" not in joined
    assert "FOR DELETE" not in joined
    # And the two tables that may be updated are exactly the two that legitimately change: the
    # forwarding pointer, and a record's membership. An observation cannot be edited.
    updatable = {
        grant.split(" ON ")[1].split(" TO ")[0] for grant in module.GRANTS if "UPDATE" in grant
    }
    assert updatable == {"er.canonical", "er.link"}


def test_nothing_in_this_migration_widens_what_the_fast_lane_reaches() -> None:
    """M6.1.3 is that the fast lane reaches projected tables and nothing else, and `er` is not
    `proj`.

    `brain.ops.migration_policy` applies the rule to every migration, and this asserts it here
    too, because the way the property is lost is a migration written for a good reason granting
    the role one more table.

    Delete this and the assertion inside `upgrade` is the only guard, and an assertion inside a
    function only fires when somebody runs it.
    """
    assert "brain_fastlane" not in squash(rendered("upgrade"))
    assert "brain_fastlane" not in squash(rendered("downgrade"))


# ------------------------------------------------------------------ the forwarding view
def test_the_forwarding_view_runs_as_the_caller_rather_than_as_its_owner() -> None:
    """**The one live hole in this file if it is wrong.**

    A PostgreSQL view executes with its owner's privileges unless it says otherwise, and
    row-level security on the tables underneath is evaluated against that owner rather than
    against whoever is querying. This view is owned by the migration's role, so without
    `security_invoker` it reads past every policy on `er.alias` and `er.canonical`, and it does
    so looking like a convenience rather than like a hole.

    The policies on those tables are unconditional today, which is exactly why this is asserted
    now: the day one of them grows a predicate, a view written without the clause goes on
    ignoring the predicate and nothing reports that it has.

    Read off the rendered SQL rather than the constant, so a constant nothing executes cannot
    satisfy it.

    Delete this and the clause can be dropped as noise by anybody tidying the DDL.
    """
    upgrade = squash(rendered("upgrade"))

    assert "CREATE VIEW er.resolved_alias WITH (security_invoker = true)" in upgrade


def test_the_forwarding_view_anchors_on_unmerged_entities_so_a_cycle_cannot_loop() -> None:
    """The recursion walks backwards from the survivors, not forwards from the aliases.

    Forwards is the obvious direction and it follows `merged_into` into any cycle a corrupt
    pair of rows created, looping until the statement times out and taking every healthy
    entity's query with it. Anchoring on `merged_into IS NULL` means a cycle contains no anchor
    and is unreachable, so the corruption costs those aliases and nothing else.

    Delete this and the direction can be reversed while every other assertion here still
    passes, because a healthy graph gives the same answer either way. The difference only shows
    up on corrupt data, which is when the difference is a hung database.
    """
    upgrade = squash(rendered("upgrade"))

    assert "WITH RECURSIVE survivor" in upgrade
    assert "FROM er.canonical c WHERE c.merged_into IS NULL" in upgrade
    assert "JOIN survivor s ON c.merged_into = s.entity_id" in upgrade


def test_the_views_recursion_bound_is_the_resolvers_own() -> None:
    """The two implementations of forwarding have to agree on how deep a chain may be.

    The migration carries its own copy of the figure, as every migration here carries its own
    copy of every predicate, and this compares the two independent copies rather than either
    against itself. An off-by-one between them would mean the view forwarding a chain
    `current_id` calls corrupt, so an alias would resolve to an entity no caller can reach by
    asking about it.

    Delete this and the two drift apart, and the drift shows up only on chains longer than
    anything a test builds.
    """
    module = migration()

    assert module.MAX_FORWARD_DEPTH == MAX_FORWARD_DEPTH
    assert f"WHERE s.depth < {MAX_FORWARD_DEPTH}" in squash(rendered("upgrade"))


def test_the_view_keeps_the_entity_a_name_was_observed_against_beside_the_surviving_one() -> None:
    """ "Which entity was this name observed against" is the unmerge question, and it is the
    column a view that only forwarded would drop.

    Delete this and the view returns the survivor alone, which is enough to answer a lookup and
    not enough to undo one: the pointer's whole value is that nothing was rewritten, and a view
    that hides what was not rewritten makes that invisible.
    """
    upgrade = squash(rendered("upgrade"))

    assert "s.current_id AS entity_id" in upgrade
    assert "a.entity_id AS observed_entity_id" in upgrade


def test_the_view_is_the_only_object_here_that_is_not_a_table() -> None:
    """A view is not on `Base.metadata`, so nothing in `brain.tables` can carry it and nothing
    in `tests/unit/test_tables.py` can check it.

    That is why this file exists, and it is why the name is a constant in
    `brain.tables.resolution` rather than a string typed in three places.

    Delete this and the view's name drifts from the module that names it, which is discovered
    by a query returning "relation does not exist" in production.
    """
    assert RESOLVED_ALIAS_VIEW == "er.resolved_alias"
    assert RESOLVED_ALIAS_VIEW not in metadata.tables
    assert f"CREATE VIEW {RESOLVED_ALIAS_VIEW}" in squash(rendered("upgrade"))


# -------------------------------------------------- what the schema may and may not hold
def test_the_canonical_table_has_no_column_that_could_hold_a_value_from_a_member() -> None:
    """**The structural half of "merging two records merges two permission surfaces".**

    The expected set is a literal here, not read off the table, so a column added later fails
    rather than being absorbed.

    Delete this and a `name` arrives, sourced from whichever member is most trusted. It has no
    permission surface of its own, so everybody who reaches any member reaches it, and every
    merge widens the estate's reach by one entity.
    """
    assert set(CANONICAL.columns.keys()) == {
        "entity_id",
        "entity_type",
        "created_at",
        "created_by",
        "created_from_source",
        "created_from_entity",
        "created_from_source_id",
        "merged_into",
        "merged_at",
    }


def test_the_identifier_table_has_no_column_for_the_value_it_identifies() -> None:
    """A join key is an email address, a phone number or a registration number, which is to say
    a contact record broken into columns.

    Two halves, and the second is what makes it structural rather than conventional: there is
    no `value` column, and the column that does exist admits sixty-four lowercase hex
    characters and nothing else, so a raw address cannot be stored in it by any route including
    a hand-written INSERT during an incident.

    Read off the compiled DDL, because the source string is what hid a mangled pattern in three
    tables until 0015.

    Delete this and a `value` column arrives for a review queue that wanted to show a human
    what matched, and `er.identifier` becomes the mailing list it was designed not to be.
    """
    columns = set(IDENTIFIER.columns.keys())

    assert "value" not in columns
    assert not any(name in columns for name in ("email", "phone", "raw", "plaintext"))
    assert "key_hash ~ '^[0-9a-f]{64}$'" in ddl(IDENTIFIER)
    # And the column is exactly as wide as what the domain produces: narrower truncates a
    # digest into one that joins nothing, wider admits a value the domain never makes.
    produced = identifier_hash(IdentifierKind.EMAIL, "someone@example.com", pepper="p")
    assert f"key_hash VARCHAR({len(produced)})" in ddl(IDENTIFIER)


def test_the_forwarding_pointer_is_a_foreign_key_into_the_same_table() -> None:
    """The structural half of "an issued id resolves forever".

    A pointer into nothing makes an id unresolvable, and the database is the only place that
    can refuse one: the constructor cannot see the other rows.
    `brain.resolution.canonical.current_id` raises on a dangling pointer and says in as many
    words that meeting one means the row arrived some other way, which is only true while this
    key exists.

    Delete this and the key can be dropped as an awkward self-reference during a schema tidy,
    after which a merge to a mistyped id is storable and every id issued for the source entity
    stops resolving.
    """
    self_references = [
        constraint
        for constraint in CANONICAL.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and [column.name for column in constraint.columns] == ["merged_into"]
    ]

    assert len(self_references) == 1
    assert self_references[0].elements[0].column.table.fullname == "er.canonical"
    assert "FOREIGN KEY(merged_into) REFERENCES er.canonical (entity_id)" in ddl(CANONICAL)


def test_a_source_record_can_belong_to_only_one_entity() -> None:
    """`er.link`'s key is the source record and nothing else.

    Two links for one record are two answers to "what is this part of", and whichever a query
    reads first is the answer, silently and differently on each run. The confidence column
    makes that especially tempting: it looks like a table of candidates, and adding the entity
    to the key would let it be one.

    Delete this and `entity_id` joins the key, which reads as more expressive and turns the
    membership table into a scoreboard nothing resolves.
    """
    assert [column.name for column in LINK.primary_key.columns] == [
        "source",
        "entity",
        "source_id",
    ]


def test_an_alias_is_keyed_by_the_observation_rather_than_by_the_entity() -> None:
    """One source record asserting one name is one fact.

    Keyed by the entity instead, a company could hold one name; keyed by a surrogate, the same
    fact could be recorded twice with two first-seen dates, the later of which is wrong.

    Delete this and the alias table stops being a record of what each source said and becomes a
    list of names with no provenance, which is exactly what the merge filter needs and cannot
    then have.
    """
    assert [column.name for column in ALIAS.primary_key.columns] == [
        "source",
        "entity",
        "source_id",
        "name",
    ]


@pytest.mark.parametrize("table", CHILDREN)
def test_every_child_row_carries_the_source_record_it_came_from(table: Table) -> None:
    """**This is what makes a merged view filterable at all.**

    After a merge an entity's aliases, identifiers and members come from several records, and
    which of them a reader may see is decided per row. A schema that hung these off the entity
    alone could not express the filter, and the only available implementations would be "show
    everything" or "show nothing".

    Delete this and a tidy-up drops the triple from `er.alias` on the grounds that the entity
    is already named, and `resolved_view` loses the thing it filters by.
    """
    columns = set(table.columns.keys())

    assert {"source", "entity", "source_id"} <= columns
    assert "entity_id" in columns


def test_the_migration_and_the_resolver_agree_on_the_closed_vocabularies() -> None:
    """Two independent copies of each enum, compared against each other.

    The migration writes the values out, as every migration here writes out every predicate,
    and the model generates them with `one_of`. Comparing the migration's literal against the
    enum is a check; comparing the model's generated constraint against the enum would be the
    enum compared against itself.

    Delete this and a member added to `EntityType` passes every Python test and is refused by
    the database at three in the morning.
    """
    module = migration()

    for value in EntityType:
        assert f"'{value.value}'" in module.ENTITY_TYPE_IN
    for kind in IdentifierKind:
        assert f"'{kind.value}'" in module.KIND_IN
    # And nothing extra, so a member removed from an enum leaves the constraint behind.
    assert module.ENTITY_TYPE_IN.count("'") == 2 * len(list(EntityType))
    assert module.KIND_IN.count("'") == 2 * len(list(IdentifierKind))


def test_the_alias_column_holds_exactly_the_name_the_domain_admits() -> None:
    """The observed name is part of `er.alias`'s primary key, so its width is an index-tuple
    bound rather than a tidiness one.

    The migration's literal is the independent copy: the model reads
    `brain.resolution.canonical.MAX_ALIAS_CHARS`, so asserting the model against that constant
    would be the constant compared against itself, and both sides would move together.

    Delete this and the domain refuses at 200 while the column admits 500, or the other way
    round, and the row that finds out is one in a backfill at whatever hour the longest name in
    the estate arrives.
    """
    assert f"name VARCHAR({MAX_ALIAS_CHARS})" in ddl(ALIAS)
    assert f"sa.String({MAX_ALIAS_CHARS})" in MIGRATION.read_text(encoding="utf-8")
    assert f'sa.Column("name", sa.String({MAX_ALIAS_CHARS})' in MIGRATION.read_text(
        encoding="utf-8"
    )


def test_neither_observation_table_carries_an_updated_at() -> None:
    """A row nothing may update, carrying a column saying when it was last updated, is a column
    that tells a reader something untrue.

    The grants on `er.alias` and `er.identifier` are SELECT and INSERT, so there is no update
    for such a column to record. The same argument `obs.audit_entry`, `agent.template_version`
    and the two memory tables all make.

    Delete this and `TimestampMixin` is applied for consistency, and every observation grows a
    field that will never change.
    """
    for table in (ALIAS, IDENTIFIER):
        columns = set(table.columns.keys())
        assert "updated_at" not in columns
        assert "created_at" in columns
        assert "first_seen_at" in columns, (
            "when the row was written and when the source first said it are not the same"
        )


def test_no_table_here_keys_into_the_projected_cache() -> None:
    """A link is a statement about a source record; `proj.record` is a bounded cache of one.

    A key into it would make the resolution graph depend on a cache having been filled, so a
    record that is federated rather than projected could not be resolved at all.
    `gate.fast_path_rule` refuses the same key for the same reason and 0019 argues it.

    Delete this and the key is added because the columns line up, and entity resolution
    silently stops working for every source that is not projected.
    """
    for table in (CANONICAL, *CHILDREN):
        targets = {
            element.column.table.fullname
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for element in constraint.elements
        }
        assert "proj.record" not in targets
