"""The two memory tables, held to what makes them two.

`tests/unit/test_tables.py` already proves every table has a migration and every migration has
a model. These are the properties specific to memory: that a row cannot carry a permission
nobody holds, that the persistent table has no confidence to decay, and that the widths come
from the domain rather than from a literal somebody typed twice.

**Constraints are read off the compiled DDL and never off `str(constraint.sqltext)`.** That is
not a style preference. `CheckConstraint` parses its argument as `text()`, which reads `:name`
as a bind parameter, and three tables shipped a mangled regular expression for the life of the
schema because the test compared the source string rather than what PostgreSQL was sent.
Migration 0015 exists because of it.

Task ids: M16.1.2, M16.1.3
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest
from sqlalchemy import Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from brain.core.entitlement import Capability
from brain.db import Base
from brain.memory.formation import RECALL_FLOOR
from brain.tables.memory import (
    CAPABILITY_TAG_CHARS,
    ENT_HASH_CHARS,
    MAX_CAPABILITY_TAGS,
)

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0018_memory_stores.py"
)


def migration() -> types.ModuleType:
    """The migration as a module, so its statements can be read as data rather than grepped.

    Importing runs nothing: `upgrade` and `downgrade` are functions and `alembic.op` reaches a
    database only when one is called under a context that has one.

    Read as data rather than as text, and an earlier draft of this file is why. Searching the
    file for "FOR ALL" matched the docstring paragraph explaining that there is no FOR ALL
    policy, so the test passed on its own prose. That is the failure CLAUDE.md names as
    asserting on text that also appears nearby, and it has now caught two tests in this
    repository.
    """
    spec = importlib.util.spec_from_file_location("migration_0018", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The two tables, as `Table` objects rather than as model classes.
#:
#: Taken from the metadata rather than through `Model.__table__`, which is the shape
#: `tests/unit/test_tables.py` uses: a `Table` is what the DDL is compiled from, and passing
#: the model class around means every helper has to reach through an attribute mypy cannot
#: see on a declarative base.
PERSISTENT: Table = Base.metadata.tables["mem.persistent"]
ADAPTIVE: Table = Base.metadata.tables["mem.adaptive"]

#: The dialect the DDL is rendered for, built the way `tests/unit/test_tables.py` builds it.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def ddl(table: Table) -> str:
    """The CREATE TABLE PostgreSQL is actually sent, not the Python that built it."""
    return str(CreateTable(table).compile(dialect=DIALECT))


@pytest.mark.parametrize("table", [PERSISTENT, ADAPTIVE])
def test_a_memory_naming_no_capability_cannot_be_stored(table: Table) -> None:
    """**The most dangerous row either table can hold.**

    A memory whose capability array is empty is recalled by everybody, because a reader
    trivially covers an empty requirement. `Formation` refuses to construct one, and that is
    not enough: a row can arrive from a backfill, from a migration or from a psql session,
    none of which construct a `Formation`.

    Read off the compiled DDL rather than the constraint's source string, because the source
    string is what hid a mangled regular expression in three tables until 0015.

    Delete this and the domain guard becomes the only one, and every path that does not go
    through the domain writes a memory anybody can recall."""
    compiled = ddl(table)

    assert "array_length(capability_tags, 1) IS NOT NULL" in compiled
    assert f"BETWEEN 1 AND {MAX_CAPABILITY_TAGS}" in compiled


@pytest.mark.parametrize("table", [PERSISTENT, ADAPTIVE])
def test_the_capability_column_is_as_wide_as_a_capability_may_be(table: Table) -> None:
    """The column has to hold whatever `Capability` admits, and a literal here would be a
    second copy of a bound: right until somebody widens the model, and then a silent
    truncation on the way into the array.

    A truncated capability tag is worse than a rejected one. It produces a requirement nobody
    holds, so the memory is never recalled and nothing anywhere says why.

    Asserted against the model's own constraint rather than against the number, which is the
    difference between a check and a restatement.

    Delete this and the two can drift apart, in the direction that fails silently."""
    declared = next(
        constraint.max_length
        for constraint in Capability.model_fields["value"].metadata
        if getattr(constraint, "max_length", None) is not None
    )

    assert declared == CAPABILITY_TAG_CHARS
    assert f"VARCHAR({declared})[]" in ddl(table)


def test_the_persistent_table_has_no_confidence_to_decay() -> None:
    """**What makes these two tables rather than one.**

    A persistent memory is something a person said. It does not become less true in thirty
    days; it becomes wrong when something contradicts it, which is supersession and is a
    different mechanism entirely.

    The tempting shape is one table with a nullable confidence defaulting to 1.0 so both can
    share a query, and the first loop written over both would then decay everything a person
    ever stated on the adaptive curve.

    Delete this and the column arrives, and it arrives looking like a simplification."""
    persistent = set(PERSISTENT.columns.keys())
    adaptive = set(ADAPTIVE.columns.keys())

    assert "formed_confidence" not in persistent
    assert "formed_confidence" in adaptive
    assert adaptive - persistent == {"formed_confidence"}, (
        "the two tables differ by something other than confidence, so a memory promoted "
        "between them would lose or gain a field nobody decided about"
    )


def test_what_is_stored_is_the_confidence_at_formation_and_never_the_decayed_value() -> None:
    """A stored decayed value is wrong the moment after it is written and needs a job to keep
    it approximately right, and a job that fails silently leaves memories more confident than
    they should be.

    So the column is named for when it was true. The name is the guard: `confidence` alone
    invites a writer to put today's figure in it, and there is no way to tell one from the
    other by looking at a number.

    Delete this and the column can be renamed to `confidence`, after which storing the
    decayed value is the obvious reading of it."""
    columns = set(ADAPTIVE.columns.keys())

    assert "formed_confidence" in columns
    assert "confidence" not in columns
    assert "formed_at" in columns, "a confidence with no formation time cannot be decayed"


def test_the_stored_confidence_is_a_share_and_not_the_retrieval_floor() -> None:
    """Two bounds that look alike and are not. The column admits anything between zero and
    one; the floor at `RECALL_FLOOR` decides what is retrieved.

    A constraint at the floor would refuse to store the evidence that something was inferred
    weakly, which is a thing worth recording precisely because it explains why nothing was
    recalled.

    Delete this and somebody tidies the two numbers into one, and the store stops being able
    to hold a weak inference at all."""
    compiled = ddl(ADAPTIVE)

    assert "formed_confidence >= 0.0 AND formed_confidence <= 1.0" in compiled
    assert str(RECALL_FLOOR) not in compiled, (
        "the retrieval floor has become a storage constraint, so a weakly inferred memory "
        "cannot be recorded at all"
    )


@pytest.mark.parametrize("table", [PERSISTENT, ADAPTIVE])
def test_neither_table_carries_an_updated_at(table: Table) -> None:
    """A row nothing may update, carrying a column saying when it was last updated, is a
    column that tells a reader something untrue.

    The grants are SELECT and INSERT, so there is no update for such a column to record. The
    same argument `obs.audit_entry` and `agent.template_version` make.

    Delete this and the timestamp mixin gets applied for consistency, and every memory row
    grows a field that will never change."""
    columns = set(table.columns.keys())

    assert "updated_at" not in columns
    assert "created_at" in columns
    assert "formed_at" in columns, "when it was written and when it was formed are not the same"


def test_the_migration_grants_no_update_and_no_delete_on_either_table() -> None:
    """No UPDATE means a memory cannot become more confident or wider in scope than it was
    written. No DELETE means the record of what the system believed survives it changing its
    mind, which is what makes a person's question about why it changed answerable.

    Read out of the migration's own statements rather than described, because the grants are
    the enforcement and a docstring is not.

    Delete this and an UPDATE grant is added to make supersession easy, which is the exact
    decision M16.4.2 is supposed to take deliberately."""
    grants = migration().GRANTS

    assert len(grants) == 2, "there are not exactly two grants, one per table"
    for grant in grants:
        assert "SELECT, INSERT" in grant
        assert "UPDATE" not in grant
        assert "DELETE" not in grant
        assert "ALL" not in grant


def test_the_migration_enables_row_level_security_on_both_tables() -> None:
    """New tables shipping with row-level security off is a live failure mode in the other
    project on this account, where it left data reachable through an anonymous key.

    The sweep checks this against a real schema in CI. This checks it against the migration,
    which is where somebody would leave it out, and runs without a database.

    Delete this and a table can be added to `mem` with no policy at all, which reads as
    working because the owner role is unaffected."""
    statements = migration().RLS
    joined = chr(10).join(statements)

    for table in ("mem.persistent", "mem.adaptive"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements

    assert "FOR ALL" not in joined, "a FOR ALL policy admits what the grants deliberately omit"
    assert joined.count("CREATE POLICY") == 4, "two policies per table, matching the grants"
    assert joined.count("FOR SELECT") == 2
    assert joined.count("FOR INSERT") == 2


def test_the_hash_column_holds_exactly_what_the_domain_produces() -> None:
    """`EntitlementSet.ent_hash` is a truncated sha256 and is 32 characters. A narrower column
    truncates it into something that matches nothing, and a wider one admits a value the
    domain never produces, which is the shape a hand-written backfill arrives in.

    Delete this and the audit question the hash exists to answer stops being answerable from
    the row."""
    from brain.core.entitlement import EntitlementSet

    produced = EntitlementSet(principal_id="p_any", grants=()).ent_hash()

    assert len(produced) == ENT_HASH_CHARS
    for one in (PERSISTENT, ADAPTIVE):
        assert f"ent_hash VARCHAR({ENT_HASH_CHARS})" in ddl(one)


def test_the_tag_bound_is_wide_enough_for_the_agents_this_system_ships() -> None:
    """**Written because a mutation survived, and the survivor was mine.**

    The test above asserts the compiled DDL says `BETWEEN 1 AND MAX_CAPABILITY_TAGS` while
    importing that constant from the module it is checking, so raising the bound to a hundred
    thousand moved both sides together and passed. That is the constant compared against
    itself, which CLAUDE.md names as this repository's most repeated defect, and it has now
    been written by every author here including me.

    So the bound is checked against something outside itself: the widest capability list any
    template in the shipped catalogue declares. A memory formed while somebody was running
    that agent carries at least that many tags, so a bound below it would make the system
    unable to remember anything learnt through its own widest template. That is a real
    constraint with a real reason, and it moves when the catalogue does.

    The upper end is bounded too, because the array is a requirement every reader is checked
    against: a row with a thousand tags is a row nobody can ever recall and a query that walks
    a thousand entries to establish it.

    Delete this and the bound is accountable to nothing again, in either direction."""
    from brain.agents.catalogue import CATALOGUE

    widest = max(len(manifest.authority.capabilities) for manifest in CATALOGUE)

    assert widest >= 1, "the catalogue declares no capabilities, so this checks nothing"
    assert widest <= MAX_CAPABILITY_TAGS, (
        f"a memory formed while running the widest template would carry {widest} tags and "
        f"the column admits {MAX_CAPABILITY_TAGS}"
    )
    assert MAX_CAPABILITY_TAGS <= 256, (
        "the tag array is a requirement checked on every recall, so a bound this wide makes "
        "a row nobody can recall and a check nobody wants to run"
    )
