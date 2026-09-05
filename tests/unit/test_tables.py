"""The persistence layer: the models, and the migration that has to agree with them.

There is no PostgreSQL on a development machine and there is one in CI, so this file draws
the line deliberately. Everything here is a property of the metadata or of the DDL the
migration emits: names, widths, nullability, which constraints exist, which tables enable
row-level security, and whether the migration builds exactly what the models declare and
drops exactly what it builds. None of it needs a connection - the migration is run against
Alembic's own offline mode, which renders the statements into a buffer instead of a server.

What it cannot check, and what CI therefore has to: that PostgreSQL 18 accepts the DDL, that
the check constraints reject what they are meant to reject, that the row-level security
policies admit and refuse the right rows for `brain_app`, and that the append-only trigger
actually fires. Those are behaviours of a running server, and asserting them here would only
assert that this file's idea of PostgreSQL matches PostgreSQL.

The same line is drawn again for 0003. Its resolver, its counters and its triggers are the
subject of `tests/unit/test_resolver.py`, which says which of its properties need a server;
what is here is the shape of the nine tables it adds.

Task ids: M1.2.1, M1.2.2, M1.2.3, M1.4.1, M1.4.3, M1.4.6, M1.4.7, M1.5.1, M2.1.1, M2.2.1,
M4.2.1, M5.2.2, M5.3.1, M5.3.4, M24.1.1
"""

from __future__ import annotations

import functools
import importlib.util
import io
import re
import types
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

import brain.tables as tables

# Imported for its side effect as well as its constants: `brain.tables.__init__` does not
# import this module, so without the line below `Base.metadata` would be missing three
# tables and every exhaustive assertion here would quietly check a smaller set. That gap is
# recorded on `ROUTING_TABLES_IN_DEPENDENCY_ORDER`; the import belongs in `__init__`.
import brain.tables.routing as routing_tables
from brain.audit.ledger import SUBJECT_KINDS, AuditAction
from brain.core.entitlement import VERBS
from brain.core.field_policy import Classification
from brain.core.principal import Employment, PrincipalKind
from brain.db import metadata
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import identity_hash
from brain.models.routing import FallbackTrigger, RungRole, Tier
from brain.ops.migration_policy import check_file
from brain.tables.identity import SessionEndReason
from brain.tables.routing import ATTEMPT_OUTCOMES

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0002_core_tables.py"
MIGRATION_RESOLVER = VERSIONS / "0003_resolver_and_tables.py"

#: The seven tables 0002 built. `brain.tables.TABLES_IN_DEPENDENCY_ORDER` still names exactly
#: these, because `brain/tables/__init__.py` was outside the remit of the change that added
#: the nine below.
CORE_TABLES = tables.TABLES_IN_DEPENDENCY_ORDER

#: The nine 0003 adds, in the order a migration must create them. Written here rather than
#: read from the migration, so that the migration's own tuple is compared against something
#: rather than against itself.
RESOLVER_TABLES: tuple[str, ...] = (
    "gate.scope",
    "gate.department",
    "gate.team",
    "auth.session",
    "gate.grants_version",
    "gate.policy_epoch",
    *routing_tables.ROUTING_TABLES_IN_DEPENDENCY_ORDER,
)

ALL_TABLES = CORE_TABLES + RESOLVER_TABLES


def _soft_deleted(qualified: tuple[str, ...]) -> tuple[str, ...]:
    """The subset carrying `deleted_at`, read from the metadata rather than listed.

    A hand-written list is a second copy of a fact the models already carry, and the copy is
    wrong the first time a table gains or loses the mixin - in the direction of not checking
    the policy on the table that just acquired one.
    """
    return tuple(q for q in qualified if "deleted_at" in metadata.tables[q].columns)


#: Every 0002 table carrying `deleted_at`. The ledger is the one that must not.
SOFT_DELETED = _soft_deleted(CORE_TABLES)

#: And the same for 0003. Four of its nine deliberately have no `deleted_at`: two counters
#: that must not be able to go backwards, a session whose end is not a retirement, and an
#: attempt row that would make the reconstructed chain a claim if it could be hidden.
SOFT_DELETED_RESOLVER = _soft_deleted(RESOLVER_TABLES)

#: A PostgreSQL dialect to render DDL against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


@functools.cache
def migration_module(path: Path = MIGRATION) -> types.ModuleType:
    """Import a migration as a module so its constants and its two functions can be reached.

    Importing it runs nothing: `upgrade` and `downgrade` are functions, and `alembic.op` is
    a proxy that reaches a database only when one of them is called under a context that
    has one.
    """
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def rendered(direction: str, path: Path = MIGRATION) -> str:
    """The SQL the migration emits, rendered without a database.

    This is Alembic's `--sql` mode driven in-process. It matters that the tests below read
    *this* rather than the file's text: a statement sitting in a constant that `upgrade`
    never executes would pass a source-text search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    module = migration_module(path)
    with Operations.context(context):
        getattr(module, direction)()
    return buffer.getvalue()


def squash(text: str) -> str:
    """Collapse whitespace, so a statement wrapped across lines still compares."""
    return " ".join(text.split())


def table(qualified: str) -> Table:
    return metadata.tables[qualified]


def checks(qualified: str) -> dict[str, str]:
    """Every check constraint on a table, as name to rendered SQL."""
    return {
        str(c.name): str(c.sqltext)
        for c in table(qualified).constraints
        if isinstance(c, CheckConstraint)
    }


def indexes(qualified: str) -> dict[str, Index]:
    """Every index on a table, keyed by name."""
    return {str(ix.name): ix for ix in table(qualified).indexes}


def width(qualified: str, column: str) -> int | None:
    """The declared character width of a string column."""
    kind = table(qualified).columns[column].type
    assert isinstance(kind, String)
    return kind.length


def quoted_values(sql: str) -> list[str]:
    return sorted(re.findall(r"'([a-z_]+)'", sql))


# ----------------------------------------------------------------- the tables exist
def test_every_table_the_domain_needs_is_registered_on_the_metadata() -> None:
    """Without this the models exist and nothing can reach them: `Base.metadata` is what a
    migration, a seed and a query all go through."""
    for qualified in ALL_TABLES:
        assert qualified in metadata.tables, f"{qualified} is not on the metadata"
    assert set(metadata.tables) == set(ALL_TABLES)


def test_no_table_lands_in_the_public_schema() -> None:
    """A table in `public` is a table nobody decided the classification of, and row-level
    security and the grant sweeps are both written against named schemas."""
    for qualified, mapped in metadata.tables.items():
        assert mapped.schema, f"{qualified} has no schema"
        assert mapped.schema != "public"


def test_the_tables_are_ordered_so_a_table_follows_what_it_points_at() -> None:
    """The migration creates them in this order and drops them in reverse. Wrong order and
    `upgrade` fails on a foreign key to a table that does not exist yet, while `downgrade`
    fails on a table something still references."""
    seen: set[str] = set()
    for qualified in ALL_TABLES:
        for constraint in table(qualified).constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            for element in constraint.elements:
                target = element.column.table.fullname
                assert target in seen or target == qualified, (
                    f"{qualified} points at {target}, which is created later"
                )
        seen.add(qualified)


# --------------------------------------------------------------- M1.2.1 principal
def test_the_principal_table_carries_every_field_the_principal_type_does() -> None:
    """The table is storage for `brain.core.principal.Principal`. A field the type carries
    and the table does not is a field that cannot be reloaded, so the object read back is
    not the object that was written."""
    columns = set(table("auth.principal").columns.keys())
    for name in ("id", "kind", "employment", "display_name", "primary_department", "not_after"):
        assert name in columns, f"Principal.{name} has nowhere to go"
    # M1.2.1 names one column the pydantic type does not carry.
    assert "disabled_at" in columns


def test_disabling_a_principal_is_a_different_column_from_retiring_one() -> None:
    """Collapsing the two would mean either that re-enabling is impossible or that
    offboarded staff keep appearing in every list."""
    columns = table("auth.principal").columns
    assert columns["disabled_at"].nullable
    assert columns["deleted_at"].nullable


def test_the_principal_id_is_the_identity_providers_id_and_not_a_surrogate() -> None:
    """The audit ledger refers to a principal by this string in `actor_id`. A surrogate key
    would leave every ledger row pointing at nothing joinable."""
    primary = list(table("auth.principal").primary_key.columns)
    assert [c.name for c in primary] == ["id"]
    assert width("auth.principal", "id") == 128


def test_a_contractor_without_an_expiry_is_refused_by_the_table() -> None:
    """`Principal.model_post_init` calls an unbounded contractor the most common way a
    permission model rots. Without this the rule holds only for rows that came through the
    type, which is not the rows that come from a backfill or an incident."""
    sql = checks("auth.principal")["ck_principal_bounded_engagement_expires"]
    assert "not_after IS NOT NULL" in sql
    assert Employment.CONTRACTOR.value in sql
    assert Employment.PARTNER.value in sql


def test_the_closed_vocabularies_are_written_from_the_enums_themselves() -> None:
    """A hand-typed list is a second copy of an enum, and the copy stops matching the first
    time somebody adds a member. The failure is a row the database refuses in production
    after passing every test that only exercised the Python side."""
    assert quoted_values(checks("auth.principal")["ck_principal_kind"]) == sorted(
        k.value for k in PrincipalKind
    )
    assert quoted_values(checks("auth.principal")["ck_principal_employment"]) == sorted(
        e.value for e in Employment
    )
    assert quoted_values(
        checks("auth.principal_identity")["ck_principal_identity_channel"]
    ) == sorted(c.value for c in Channel)
    assert quoted_values(checks("gate.field_policy")["ck_field_policy_classification"]) == sorted(
        c.value for c in Classification
    )
    assert quoted_values(checks("obs.audit_entry")["ck_audit_entry_action"]) == sorted(
        a.value for a in AuditAction
    )
    verbs = checks("gate.capability_grant")["ck_capability_grant_capability_verb"]
    # `split_part(capability, ':', 1)` contributes a quoted colon that is not a verb.
    assert [v for v in quoted_values(verbs) if v] == sorted(VERBS)


# ------------------------------------------------------- M1.2.2 principal identity
def test_the_identity_column_holds_a_digest_and_is_shaped_so_a_raw_number_cannot_fit() -> None:
    """A binding table full of phone numbers is a company phone book joined to a permission
    model. The width and the constraint together stop a raw identity being written there by
    a hand-typed statement, rather than a convention asking nicely."""
    columns = table("auth.principal_identity").columns
    assert "identity" not in columns, "the raw channel identity must never have a column"
    digest = identity_hash(Channel.LARK, "+6598765432")
    assert width("auth.principal_identity", "identity_hash") == len(digest)
    assert (
        checks("auth.principal_identity")["ck_principal_identity_hash_shape"]
        == r"identity_hash ~ '^[0-9a-f]{64}$'"
    )


def test_one_live_binding_per_channel_identity() -> None:
    """`ingress.resolve` looks a sender up by digest and expects at most one answer. Two
    live rows would make "who is this?" depend on which came back first, which is principal
    confusion and not a duplicate-row problem.

    Partial rather than total: a total constraint would mean a number belonging to somebody
    who left could never be bound to whoever holds it next, because nothing here has the
    DELETE privilege to clear the retired row."""
    index = indexes("auth.principal_identity")["uq_principal_identity_channel_identity_hash_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["channel", "identity_hash"]
    assert "deleted_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


def test_a_stored_binding_is_never_stronger_evidence_than_bound() -> None:
    """`Binding.__post_init__`: a binding is evidence about the day it was made, not about
    this request. Without the constraint a row could claim STRONG and every later message
    from that channel would inherit an authentication that never happened."""
    sql = checks("auth.principal_identity")["ck_principal_identity_assurance_at_most_bound"]
    assert sql == f"assurance BETWEEN {int(Assurance.UNVERIFIED)} AND {int(Assurance.BOUND)}"


# ------------------------------------------------------------------- M1.4.1 grants
def test_no_grant_table_points_at_a_connector() -> None:
    """`sweep_grant_isolation` fails the build on one, and the reason runs both ways: a
    connector that cascades into grants can remove them, and a connector that can be added
    becomes a way to touch the permission graph."""
    for qualified in ALL_TABLES:
        for constraint in table(qualified).constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            for element in constraint.elements:
                target = element.column.table.fullname
                assert "connector" not in target, f"{qualified} points at {target}"


def test_a_principal_cannot_hold_one_capability_twice() -> None:
    """`EntitlementSet.scope_for` intersects the scopes of every grant covering a
    capability, so a second grant narrows. Without this the administrator who adds one
    believes they widened access and has in fact removed it, silently."""
    index = indexes("gate.capability_grant")["uq_capability_grant_principal_id_capability_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["principal_id", "capability"]


def test_a_scope_column_refuses_the_consoles_document_form() -> None:
    """`Scope.model_dump()` is `{"clauses": [...]}` and `scope_sql.parse_predicate` reads
    `{"department": "web"}`. Both are json objects, so a check on the outer type alone
    admits the wrong one - and the wrong one deserialises to a scope with no clauses, which
    is unrestricted. Deleting this removes what stands between a mis-shaped predicate and a
    company-wide grant."""
    for qualified, name in (
        ("gate.capability_grant", "ck_capability_grant_scope_shape"),
        ("gate.capability_pack_assignment", "ck_capability_pack_assignment_scope_shape"),
    ):
        sql = checks(qualified)[name]
        assert "jsonb_typeof(scope) = 'object'" in sql
        assert "jsonb_typeof(scope -> 'clauses') = 'array'" in sql


def test_a_grant_records_who_made_it_and_why_and_may_still_be_unbounded() -> None:
    """M1.4.1 names all three. A grant with no reason is a grant nobody can review and one
    with no granter is a grant nobody can ask about, so both are required; `not_after` is
    nullable because a permanent grant to a member of staff is legitimate and the expiry
    that is not is enforced on the principal instead."""
    for qualified in ("gate.capability_grant", "gate.capability_pack_assignment"):
        columns = table(qualified).columns
        assert not columns["granted_by"].nullable
        assert not columns["reason"].nullable
        assert columns["not_after"].nullable


def test_the_scope_columns_carry_a_gin_index() -> None:
    """Not for `compile_where`, which compiles a scope into a predicate over some other
    table's rows. For the reverse question - which grants mention this department - which
    is how an access review and a revocation sweep both work, and a sequential scan over
    every grant in the company without it."""
    for qualified, name in (
        ("gate.capability_grant", "ix_capability_grant_scope"),
        ("gate.capability_pack_assignment", "ix_capability_pack_assignment_scope"),
    ):
        index = indexes(qualified)[name]
        assert index.dialect_options["postgresql"]["using"] == "gin"
        assert index.dialect_options["postgresql"]["ops"] == {"scope": "jsonb_path_ops"}


# -------------------------------------------------------------------- M1.4.3 packs
def test_a_pack_cannot_be_empty() -> None:
    """An empty pack is assignable, reviewable and grants nothing. It looks like access
    having been given and is not, so the person who assigned it stops looking."""
    assert (
        checks("gate.capability_pack")["ck_capability_pack_not_empty"]
        == "cardinality(capabilities) > 0"
    )


def test_the_scope_is_bound_to_the_assignment_and_not_to_the_pack() -> None:
    """That is what makes a pack worth having: one bundle held by eleven people in eleven
    scopes. A scope on the pack would mean a pack per scope, which is the eleven
    near-identical grant sets that packs exist to remove."""
    assert "scope" not in table("gate.capability_pack").columns
    assert "scope" in table("gate.capability_pack_assignment").columns


def test_one_live_assignment_of_a_pack_per_principal() -> None:
    """Same reasoning as the grant table: assigning one pack twice intersects the two
    scopes and leaves the holder with less than they started with."""
    index = indexes("gate.capability_pack_assignment")[
        "uq_capability_pack_assignment_principal_id_pack_id_live"
    ]
    assert index.unique
    assert [c.name for c in index.columns] == ["principal_id", "pack_id"]


# ------------------------------------------------------------- M4.2.1 field policy
def test_a_field_rule_may_only_ever_require_a_read() -> None:
    """`FieldRule._must_be_a_read`: a field policy gates returning a value, and returning is
    reading. Without this a rule could be satisfied by a write capability, so permission to
    change a number would confer permission to see it."""
    assert (
        checks("gate.field_policy")["ck_field_policy_capability_is_a_read"]
        == "split_part(required_capability, ':', 1) = 'read'"
    )


def test_two_live_rules_cannot_govern_one_field() -> None:
    """`PolicyConflictError` exists because two rules for one field turn "may this person
    see this field" from a lookup into an evaluation-order problem. This is that error one
    layer down: the conflicting row cannot be written at all."""
    index = indexes("gate.field_policy")["uq_field_policy_entity_field_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["entity", "field"]


def test_the_field_policy_table_holds_all_four_things_the_leaf_names() -> None:
    """Entity type, field, required capability, classification. A rule missing any one of
    them cannot be evaluated by `compute_mask`."""
    columns = table("gate.field_policy").columns
    for name in ("entity", "field", "required_capability", "classification"):
        assert name in columns
        assert not columns[name].nullable


# ------------------------------------------------------------------ M24.1.1 ledger
def test_the_ledger_carries_every_field_that_goes_into_the_digest() -> None:
    """`compute_entry_hash` covers all of these. A field that is hashed and not stored
    cannot be recomputed on read, so `verify` would report the whole chain broken."""
    columns = set(table("obs.audit_entry").columns.keys())
    hashed = {
        "seq",
        "at",
        "actor_id",
        "action",
        "subject",
        "ent_hash",
        "trace_id",
        "details",
        "prev_hash",
    }
    assert hashed <= columns
    assert "entry_hash" in columns


def test_the_ledger_has_no_deleted_at_and_no_updated_at() -> None:
    """A ledger entry that can be marked deleted is a ledger entry that can be hidden, and
    an `updated_at` on a table that refuses updates is a column that can only ever lie."""
    columns = set(table("obs.audit_entry").columns.keys())
    assert "deleted_at" not in columns
    assert "updated_at" not in columns
    assert "created_at" not in columns


def test_the_ledger_timestamp_has_no_server_default() -> None:
    """`at` is inside the digest, so the caller passes one authoritative clock's reading. A
    database-filled second reading would disagree with the hashed one by a few milliseconds
    and give any reader two answers to "when"."""
    assert table("obs.audit_entry").columns["at"].server_default is None


def test_the_sequence_number_is_not_generated_by_the_database() -> None:
    """`AuditChain.append` computes `seq` from the previous entry, because a caller who can
    choose it can forge a link. An identity column is exactly such a caller, and it would
    also renumber whatever `prune_before` retained."""
    seq = table("obs.audit_entry").columns["seq"]
    assert seq.autoincrement is False
    assert seq.primary_key


def test_two_entries_cannot_name_the_same_parent() -> None:
    """A fork is the one tamper that survives `AuditChain.verify`: it walks the sequence it
    was handed and cannot see a branch outside the window, so both halves of a forked
    history verify cleanly. Uniqueness on `prev_hash` makes the chain linear by
    construction rather than by inspection."""
    entry_indexes = indexes("obs.audit_entry")
    assert entry_indexes["uq_audit_entry_prev_hash"].unique
    assert entry_indexes["uq_audit_entry_entry_hash"].unique


def test_the_ledger_digest_columns_are_pinned_to_the_shapes_the_ledger_produces() -> None:
    """`ledger.py` warns that if `ent_hash` ever changes width this must fail loudly on the
    next entry written rather than silently storing a short hash. This is that failure."""
    entry_checks = checks("obs.audit_entry")
    assert entry_checks["ck_audit_entry_ent_hash_shape"] == r"ent_hash ~ '^[0-9a-f]{32}$'"
    assert entry_checks["ck_audit_entry_prev_hash_shape"] == r"prev_hash ~ '^[0-9a-f]{64}$'"
    assert entry_checks["ck_audit_entry_entry_hash_shape"] == r"entry_hash ~ '^[0-9a-f]{64}$'"


def test_the_subject_grammar_admits_only_the_closed_set_of_kinds() -> None:
    """`SUBJECT_KINDS` is closed because the client-visible audit view filters on it, and a
    free-text kind makes "everything that ever happened to this principal" unanswerable
    without a full scan and a guess."""
    sql = checks("obs.audit_entry")["ck_audit_entry_subject_grammar"]
    alternation = sql.split("^(", 1)[1].split(")", 1)[0]
    assert sorted(alternation.split("|")) == sorted(SUBJECT_KINDS)


# ------------------------------------------------------- the migration matches them
def test_the_migration_creates_exactly_the_tables_the_models_declare() -> None:
    """A model with no table is a query that fails at runtime; a table with no model is a
    table nothing maintains. Autogenerate cannot catch either, because `migrations/env.py`
    imports `brain.db` and not `brain.tables`, so its metadata is empty."""
    module = migration_module()
    assert module.TABLES == tables.TABLES_IN_DEPENDENCY_ORDER
    resolver = migration_module(MIGRATION_RESOLVER)
    assert resolver.TABLES == RESOLVER_TABLES
    # Every table has a migration and every migration has a model. The union is the check
    # that matters: either half on its own would let a table be created twice or not at all.
    assert set(module.TABLES) | set(resolver.TABLES) == set(metadata.tables)
    assert not set(module.TABLES) & set(resolver.TABLES)


@pytest.mark.parametrize("qualified", tables.TABLES_IN_DEPENDENCY_ORDER)
def test_the_migration_builds_each_table_exactly_as_the_model_declares_it(
    qualified: str,
) -> None:
    """The migration copies the models' predicates rather than importing them, because a
    migration that reads live model code stops describing the database it actually built the
    moment the model changes. A copy needs something comparing it, or it rots without saying
    so - and the comparison is on rendered DDL rather than on source text, so a difference
    in column type, width, nullability, default or constraint is caught rather than a
    difference in how either file is written."""
    expected = squash(str(CreateTable(table(qualified)).compile(dialect=DIALECT)))
    assert expected in squash(rendered("upgrade"))


@pytest.mark.parametrize("qualified", tables.TABLES_IN_DEPENDENCY_ORDER)
def test_the_migration_builds_every_index_the_model_declares(qualified: str) -> None:
    """An index that exists only in the model is an index that is never built, and the
    unique ones are constraints: without them the duplicate grant and the forked ledger
    both become possible in the database that actually runs."""
    emitted = squash(rendered("upgrade"))
    for index in table(qualified).indexes:
        expected = squash(str(CreateIndex(index).compile(dialect=DIALECT)))
        assert expected in emitted, f"{index.name} is never created"


def test_the_downgrade_drops_everything_the_upgrade_creates() -> None:
    """A migration that cannot be reversed is a deploy with no way home. The tables are
    dropped by walking the creation order backwards, so the two cannot drift; the trigger
    function is the one object that does not belong to a table and has to be named."""
    down = squash(rendered("downgrade"))
    up = squash(rendered("upgrade"))
    for qualified in tables.TABLES_IN_DEPENDENCY_ORDER:
        assert f"CREATE TABLE {qualified}" in up
        assert f"DROP TABLE {qualified}" in down
    assert "CREATE FUNCTION obs.audit_entry_is_append_only()" in up
    assert "DROP FUNCTION IF EXISTS obs.audit_entry_is_append_only()" in down


def test_the_downgrade_drops_in_the_reverse_of_the_creation_order() -> None:
    """Dropping `auth.principal` before the grant tables that reference it fails on the
    foreign key, so a downgrade in the wrong order is a downgrade that cannot run - which
    is discovered during a rollback, at the worst possible moment."""
    down = rendered("downgrade")
    positions = [down.index(f"DROP TABLE {q}") for q in tables.TABLES_IN_DEPENDENCY_ORDER]
    assert positions == sorted(positions, reverse=True)


def test_the_downgrade_is_not_a_pass() -> None:
    """`migration_policy` refuses an empty downgrade because it cannot be told apart from
    having forgotten one. This says the same thing about this migration specifically."""
    assert rendered("downgrade").strip()


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The policy is what stops a rename written as a drop plus an add, a not-null column
    with no default, and schema mixed with data - the three ways autogenerate is wrong that
    a diff does not show."""
    assert check_file(MIGRATION) == []


def test_the_migration_changes_no_data() -> None:
    """Schema and data in one migration cannot be rolled back independently: the schema half
    reverses and the data half usually cannot, so combining them makes the whole thing
    one-way. This migration creates tables and nothing else."""
    emitted = squash(rendered("upgrade")).upper()
    for statement in ("INSERT INTO", "DELETE FROM", " SET "):
        assert statement not in emitted, f"the migration emits {statement.strip()}"


# --------------------------------------------------------------- row-level security
@pytest.mark.parametrize("qualified", tables.TABLES_IN_DEPENDENCY_ORDER)
def test_row_level_security_is_enabled_on_every_table(qualified: str) -> None:
    """A table without it is one forgotten WHERE clause away from returning every row to
    every caller, and it looks correct in every test that happens to use a wide principal.
    `sweep_rls` checks only `proj`, `know`, `agent`, `mem` and `er`, so nothing in CI covers
    the tables this migration creates; this is what does."""
    assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" in squash(rendered("upgrade"))


@pytest.mark.parametrize("qualified", SOFT_DELETED)
def test_the_policy_on_a_soft_deleted_table_still_permits_retiring_a_row(
    qualified: str,
) -> None:
    """Without an explicit `WITH CHECK`, PostgreSQL reuses the USING expression to check the
    new row, so setting `deleted_at` would be refused by the very policy meant to hide the
    row afterwards. Soft delete would be impossible and the failure would read as a
    permissions bug."""
    _schema, _, name = qualified.partition(".")
    policy = (
        f"CREATE POLICY {name}_live ON {qualified} FOR ALL TO brain_app "
        "USING (deleted_at IS NULL) WITH CHECK (true)"
    )
    assert policy in squash(rendered("upgrade"))


def test_the_ledger_has_no_policy_for_amendment_or_removal() -> None:
    """PostgreSQL denies what no policy admits, so the absence is the enforcement for every
    role that cannot bypass row-level security - which, per 0001, is every role this system
    owns. Adding an UPDATE policy here would quietly remove that layer."""
    emitted = squash(rendered("upgrade"))
    assert "CREATE POLICY audit_entry_readable ON obs.audit_entry FOR SELECT" in emitted
    assert "CREATE POLICY audit_entry_appendable ON obs.audit_entry FOR INSERT" in emitted
    assert "ON obs.audit_entry FOR UPDATE" not in emitted
    assert "ON obs.audit_entry FOR DELETE" not in emitted
    assert "ON obs.audit_entry FOR ALL" not in emitted


# ------------------------------------------------------------------- privileges
def test_nothing_is_granted_the_privilege_to_hard_delete() -> None:
    """A hard delete destroys the audit trail for the thing deleted, and the audit trail is
    the whole product. Retirement is `deleted_at`, which leaves the row."""
    for line in rendered("upgrade").splitlines():
        if line.strip().startswith("GRANT"):
            assert "DELETE" not in line.upper(), line.strip()


@pytest.mark.parametrize("qualified", tables.TABLES_IN_DEPENDENCY_ORDER)
def test_every_table_grants_the_application_role_what_it_needs(qualified: str) -> None:
    """0001 granted no default privileges on purpose, so a table that forgets to grant is a
    table the application cannot read at all - and the failure arrives at the first request
    rather than at deploy."""
    assert f"ON {qualified} TO brain_app" in squash(rendered("upgrade"))


def test_the_audit_table_refuses_an_update() -> None:
    """A table that is append-only by convention says whatever the person holding the
    database password wants it to say, and nothing about the table afterwards reveals that
    it happened. The privilege is withheld, no policy admits it, and a trigger raises for
    whoever neither of those reaches."""
    emitted = squash(rendered("upgrade"))
    assert "GRANT SELECT, INSERT ON obs.audit_entry TO brain_app" in emitted
    assert "GRANT SELECT, INSERT, UPDATE ON obs.audit_entry" not in emitted
    assert "BEFORE UPDATE ON obs.audit_entry FOR EACH STATEMENT" in emitted
    assert "EXECUTE FUNCTION obs.audit_entry_is_append_only()" in emitted


def test_the_audit_table_refuses_a_removal_and_a_truncation() -> None:
    """TRUNCATE is the obvious way around a delete trigger, and PostgreSQL will not let a
    row-level trigger cover it at all - which is one of the two reasons these triggers are
    statement-level."""
    emitted = squash(rendered("upgrade"))
    assert "BEFORE DELETE ON obs.audit_entry FOR EACH STATEMENT" in emitted
    assert "BEFORE TRUNCATE ON obs.audit_entry FOR EACH STATEMENT" in emitted


def test_the_ledger_refusal_raises_rather_than_discarding_the_write() -> None:
    """A rule doing `DO INSTEAD NOTHING` is the older way to make a table append-only and it
    fails silently: the statement succeeds, no row changes, and whoever ran it believes the
    ledger now says something it does not."""
    emitted = rendered("upgrade")
    assert "RAISE EXCEPTION" in emitted
    assert "DO INSTEAD NOTHING" not in emitted


def test_nothing_the_migration_runs_needs_a_superuser() -> None:
    """0001 creates the application role `NOBYPASSRLS` and that is the whole reason it
    exists. A migration that quietly required more would make every policy written here
    decoration, and the tests would still pass because they would run as the same role."""
    emitted = rendered("upgrade").upper() + rendered("downgrade").upper()
    for forbidden in ("SUPERUSER", "BYPASSRLS", "SET ROLE", "SECURITY DEFINER"):
        assert forbidden not in emitted, f"the migration runs {forbidden}"


# ============================================================ 0003: the remaining tables
# The resolver, the counters and the triggers 0003 also creates are the subject of
# `tests/unit/test_resolver.py`. What is below is the shape of the nine tables.


def resolver_sql(direction: str) -> str:
    return squash(rendered(direction, MIGRATION_RESOLVER))


# ------------------------------------------------------------------- M1.2.3 sessions
def test_a_session_records_when_it_actually_ended_and_why() -> None:
    """M1.2.3 cascades a disable to sessions, and the cascade is only auditable if the row
    says what closed it. Without `end_reason` a session ended by the cascade is
    indistinguishable from one that simply expired, so "did disabling her actually stop
    anything" has no answer."""
    columns = table("auth.session").columns
    assert columns["ended_at"].nullable
    assert columns["end_reason"].nullable
    assert quoted_values(checks("auth.session")["ck_session_end_reason"]) == sorted(
        r.value for r in SessionEndReason
    )


def test_a_session_cannot_end_without_saying_why() -> None:
    """Both columns or neither. An `ended_at` with no reason is a session that stopped for
    reasons nobody recorded, which is exactly the row a disable audit needs; a reason with no
    time is a claim about an event with no moment."""
    assert (
        checks("auth.session")["ck_session_ended_with_a_reason"]
        == "(ended_at IS NULL) = (end_reason IS NULL)"
    )


def test_when_a_session_was_always_going_to_stop_is_not_when_it_did() -> None:
    """`expires_at` and `ended_at` are different facts. Collapsing them would lose the only
    evidence that the cascade ran early, because a session closed by a disable would look
    exactly like one that ran its full length."""
    columns = table("auth.session").columns
    assert not columns["expires_at"].nullable
    assert columns["ended_at"].nullable
    assert checks("auth.session")["ck_session_expires_after_it_starts"] == "expires_at > started_at"


def test_a_session_is_never_hidden_when_it_ends() -> None:
    """No `deleted_at`, so no policy filters ended rows away. Which sessions were open when
    somebody was disabled is precisely the question asked afterwards, and a row-level
    security policy that hid them would make the cascade unverifiable from outside the
    database."""
    assert "deleted_at" not in table("auth.session").columns
    assert "auth.session" not in SOFT_DELETED_RESOLVER


def test_a_session_may_carry_stronger_evidence_than_a_binding() -> None:
    """`principal_identity.assurance` is capped at BOUND because a binding is evidence about
    the day it was made. A session is evidence about now, so AUTHENTICATED and STRONG are
    legitimate here - and capping this column the same way would make a second factor
    unrecordable."""
    sql = checks("auth.session")["ck_session_assurance_in_range"]
    assert sql == f"assurance BETWEEN {int(Assurance.UNVERIFIED)} AND {int(Assurance.STRONG)}"


# ------------------------------------------------------------------ M2.1.1 the scope table
def test_the_scope_table_refuses_the_shape_the_grant_tables_hold() -> None:
    """Two json shapes now live in one schema. `capability_grant.scope` is
    `Scope.model_dump()`; `scope.predicate` is the document form `parse_predicate` reads.
    Read as each other they mean opposite things: a document form parsed as a `Scope` has no
    clauses, which is unrestricted. Deleting this removes what stands between a
    copy-and-pasted predicate and a company-wide scope."""
    assert "scope" not in table("gate.scope").columns, "the column must be called predicate"
    sql = checks("gate.scope")["ck_scope_predicate_shape"]
    assert "jsonb_typeof(predicate) = 'object'" in sql
    assert "NOT (predicate ? 'clauses')" in sql


def test_a_department_scope_has_to_restrict_something() -> None:
    """`ScopeRecord.model_post_init` refuses a flagged scope that is unrestricted, because an
    unbounded department scope is the whole company wearing a department's name. The empty
    object is the one unrestricted predicate a check constraint can recognise without
    iterating a json array, so it is the half the database holds."""
    assert (
        checks("gate.scope")["ck_scope_a_department_scope_restricts_something"]
        == "NOT is_department OR predicate <> '{}'::jsonb"
    )


def test_a_scope_slug_may_be_used_again_after_the_scope_is_retired() -> None:
    """Partial rather than total, for the reason `principal_identity` gives. A department
    wound up and later restarted would otherwise never get its own name back, because the
    retired row still occupies it and nothing here holds DELETE."""
    index = indexes("gate.scope")["uq_scope_slug_live"]
    assert index.unique
    assert "deleted_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


# -------------------------------------------------------------- M2.2.1 the department table
def test_a_department_names_the_scope_that_defines_it() -> None:
    """`Department.scope_slug` is not optional in the type because a department with no
    predicate is a label, and a label cannot decide who sees what. A nullable column here
    would let one be written."""
    columns = table("gate.department").columns
    assert not columns["scope_slug"].nullable
    assert not columns["company_id"].nullable


def test_a_department_has_no_parent_department() -> None:
    """`Department` has no parent field because nesting makes the entitlement lookup
    recursive. A column here would be the place somebody adds one, and every later feature
    would be built on the walk."""
    columns = set(table("gate.department").columns.keys())
    assert "parent_id" not in columns
    assert "parent_slug" not in columns


def test_one_live_department_per_slug_per_company() -> None:
    """Two live rows for one name make "which department is web" depend on which came back
    first, and every grant scoped to `department = web` then reaches whichever the resolver
    happened to pick."""
    index = indexes("gate.department")["uq_department_company_id_slug_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["company_id", "slug"]


# ------------------------------------------------------------------- M1.5.1 the team table
def test_a_team_cannot_exist_outside_a_department() -> None:
    """`Team.department_slug` is non-optional because a team floating outside a department
    has no scope to be narrower than, so a grant naming it would be bounded by nothing. The
    foreign key is that rule where the rows are, rather than only where the type is."""
    keys = [
        c
        for c in table("gate.team").constraints
        if isinstance(c, ForeignKeyConstraint)
        for element in c.elements
        if element.column.table.fullname == "gate.department"
    ]
    assert keys, "team does not point at a department"
    assert not table("gate.team").columns["department_id"].nullable


def test_a_team_does_not_repeat_its_departments_name() -> None:
    """A stored department slug is a second copy of the parent's name, and it goes stale on
    the first rename - silently, in the direction of matching rows that belong to somebody
    else. `Team.path` is a join."""
    columns = set(table("gate.team").columns.keys())
    assert "department_slug" not in columns
    assert "path" not in columns
    assert "company_id" not in columns


# ------------------------------------------------------ M1.4.6 and M1.4.7 the two counters
def test_a_grants_version_cannot_be_retired_or_go_backwards() -> None:
    """The version is what `brain.gate.resolve.cache_key` puts in the key. A hidden row sends
    the reader to its default, so a bumped counter silently returns to zero - which is a key
    colliding with one minted before the revocation, and whatever is cached under it is still
    readable."""
    assert "deleted_at" not in table("gate.grants_version").columns
    assert checks("gate.grants_version")["ck_grants_version_version_non_negative"] == "version >= 0"


def test_the_version_is_per_principal_and_the_epoch_is_not() -> None:
    """They invalidate different things: one answers whether this person's reach changed, the
    other whether the shape of the model did. One table doing both would make a single
    revocation invalidate every cached answer in the company."""
    assert [c.name for c in table("gate.grants_version").primary_key.columns] == ["principal_id"]
    assert [c.name for c in table("gate.policy_epoch").primary_key.columns] == ["id"]
    assert "principal_id" not in table("gate.policy_epoch").columns


def test_there_is_exactly_one_policy_epoch_row() -> None:
    """Two rows would make the epoch an aggregate, and an aggregate over a table anybody can
    write to is a number that moves backwards when the wrong row goes."""
    assert checks("gate.policy_epoch")["ck_policy_epoch_exactly_one_row"] == "id = 1"
    assert table("gate.policy_epoch").columns["id"].autoincrement is False


# ------------------------------------------------------------------- M5.2.2 routing tiers
def test_a_routing_tier_carries_a_jsonb_rule_set() -> None:
    """M5.2.2 names it. The console edits tier assignment roughly monthly as providers ship
    models, and a change that needs an engineer and a release is a change that stops
    happening, after which the pools rot."""
    columns = table("ops.routing_tier").columns
    assert not columns["rules"].nullable
    assert checks("ops.routing_tier")["ck_routing_tier_rules_object"] == (
        "jsonb_typeof(rules) = 'object'"
    )
    assert quoted_values(checks("ops.routing_tier")["ck_routing_tier_tier"]) == sorted(
        t.value for t in Tier
    )


def test_a_tiers_context_window_is_a_column_and_not_a_rule() -> None:
    """`RoutingChain.narrowest_window`: a tier's window must be the narrowest of its rungs,
    never the widest, or a request sized to fit the primary overflows the fallback and the
    chain fails precisely when it is reached. A number under an invariant belongs where a
    constraint can reach it, not inside a json blob."""
    assert "context_window" in table("ops.routing_tier").columns
    assert (
        checks("ops.routing_tier")["ck_routing_tier_context_window_non_negative"]
        == "context_window >= 0"
    )


# -------------------------------------------------------------------- M5.3.1 routing rungs
def test_a_routing_rung_carries_every_column_the_leaf_names() -> None:
    """Tier, scope, position, role, deployment, attempts, timeout, concurrency. A rung
    missing any one of them cannot be executed from the table, so the matrix stays a
    function and editing a timeout stays a deploy."""
    columns = set(table("ops.routing_rung").columns.keys())
    for name in (
        "tier",
        "scope",
        "position",
        "role",
        "deployment_id",
        "attempts",
        "timeout_seconds",
        "max_concurrency",
    ):
        assert name in columns, f"the rung has nowhere to record {name}"


def test_a_rung_with_no_attempts_cannot_be_written() -> None:
    """`RoutingRung.__post_init__`: the way to remove a rung is to remove it. A rung with zero
    attempts silently never runs and reads in the console as configured, so the chain is
    shorter than the person looking at it believes."""
    rung = checks("ops.routing_rung")
    assert rung["ck_routing_rung_at_least_one_attempt"] == "attempts >= 1"
    assert rung["ck_routing_rung_timeout_positive"] == "timeout_seconds > 0"
    assert rung["ck_routing_rung_concurrency_at_least_one"] == "max_concurrency >= 1"


def test_one_rung_per_position_in_a_tier() -> None:
    """`RoutingChain.__post_init__` refuses two rungs sharing a position, and says the cost:
    the chain order would depend on insertion order, so the executed chain stops being
    reconstructable from the attempt rows - which is the whole point of recording them."""
    index = indexes("ops.routing_rung")["uq_routing_rung_tier_position_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["tier", "position"]


def test_a_rungs_role_is_drawn_from_the_closed_set() -> None:
    """`RungRole` has three members and M5.3.2 will derive the column from position and
    provider rather than letting it be typed. Until then the vocabulary is at least closed,
    so a rung cannot claim to be something the chain has no concept of."""
    assert quoted_values(checks("ops.routing_rung")["ck_routing_rung_role"]) == sorted(
        r.value for r in RungRole
    )


def test_no_rung_points_at_a_deployment_table_that_does_not_exist() -> None:
    """The provider registry is M5.1 and has not been built. A foreign key to a table nobody
    has designed is not an option, and inventing it here would mean two people designing it.
    The column is a plain identifier and the constraint says only that it is present."""
    for constraint in table("ops.routing_rung").constraints:
        assert not isinstance(constraint, ForeignKeyConstraint), constraint
    assert (
        checks("ops.routing_rung")["ck_routing_rung_deployment_present"]
        == "length(btrim(deployment_id)) > 0"
    )


# ------------------------------------------------------------------ M5.3.4 attempt rows
def test_one_attempt_row_per_try() -> None:
    """The leaf, as a constraint. Two rows claiming one position in a trace make the
    reconstructed chain depend on which came back first, so a trace would assert a fallback
    rather than show it."""
    index = indexes("ops.model_attempt")["uq_model_attempt_trace_id_sequence"]
    assert index.unique
    assert [c.name for c in index.columns] == ["trace_id", "sequence"]


def test_the_executed_chain_reconstructs_from_a_join() -> None:
    """Attempt to rung to tier, ordered by sequence. Without the foreign key the join has
    nothing to run through and the attempt row records a model name that nothing ties back to
    the chain that chose it."""
    targets = {
        element.column.table.fullname
        for constraint in table("ops.model_attempt").constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert targets == {"ops.routing_rung"}


def test_an_attempt_cannot_be_hidden_from_the_chain_it_belongs_to() -> None:
    """No `deleted_at`. An attempt that can be retired makes the reconstructed chain a claim
    rather than a record: it would come back shorter with nothing anywhere to say so."""
    assert "deleted_at" not in table("ops.model_attempt").columns
    assert "ops.model_attempt" not in SOFT_DELETED_RESOLVER


def test_an_attempt_in_flight_is_visible_as_an_attempt_in_flight() -> None:
    """The row is written when the try starts and finished once. Writing one row at the end
    instead would lose the case where an attempt never returns, which is the one an operator
    is looking at during an incident."""
    columns = table("ops.model_attempt").columns
    assert not columns["started_at"].nullable
    assert columns["finished_at"].nullable
    assert columns["outcome"].nullable
    assert (
        checks("ops.model_attempt")["ck_model_attempt_finished_with_an_outcome"]
        == "(finished_at IS NULL) = (outcome IS NULL)"
    )


def test_the_attempt_outcomes_are_the_closed_fallback_set_and_nothing_else() -> None:
    """`QUALITY_FALLBACK_REJECTED` at the storage layer. A column that could record "the
    answer looked weak" is somewhere to put the number afterwards, which is how an argument
    that was settled gets reopened - and a quality trigger has no falsifiable off condition,
    so the retry loop it drives terminates on luck."""
    recorded = quoted_values(checks("ops.model_attempt")["ck_model_attempt_outcome"])
    assert recorded == sorted(ATTEMPT_OUTCOMES)
    assert {t.value for t in FallbackTrigger} <= set(recorded)
    for invented in ("weak", "quality", "poor_answer", "content_policy"):
        assert invented not in recorded


# ------------------------------------------------- 0003 matches its models and reverses
def test_the_resolver_migration_satisfies_the_migration_policy() -> None:
    """The policy is what stops a rename written as a drop plus an add, a not-null column
    with no default, and schema mixed with data. 0003 creates nine tables and six functions
    and writes not one row, which is why it can be rolled back at all."""
    assert check_file(MIGRATION_RESOLVER) == []


@pytest.mark.parametrize("qualified", RESOLVER_TABLES)
def test_the_resolver_migration_builds_each_table_exactly_as_the_model_declares_it(
    qualified: str,
) -> None:
    """Same reasoning as for 0002: the migration copies the models' predicates rather than
    importing them, so the copy needs something comparing it or it rots without saying so.
    The comparison is on rendered DDL, so a difference in type, width, nullability, default
    or constraint is caught rather than a difference in how either file is written."""
    expected = squash(str(CreateTable(table(qualified)).compile(dialect=DIALECT)))
    assert expected in resolver_sql("upgrade")


@pytest.mark.parametrize("qualified", RESOLVER_TABLES)
def test_the_resolver_migration_builds_every_index_the_model_declares(qualified: str) -> None:
    """An index that exists only in the model is never built, and the unique ones are
    constraints: without them two rungs can share a position and two attempt rows can claim
    one try, which is the reconstructable chain gone."""
    emitted = resolver_sql("upgrade")
    for index in table(qualified).indexes:
        expected = squash(str(CreateIndex(index).compile(dialect=DIALECT)))
        assert expected in emitted, f"{index.name} is never created"


def test_the_resolver_migrations_downgrade_drops_everything_it_creates() -> None:
    """A migration that cannot be reversed is a deploy with no way home, and this one adds
    objects that do not belong to any table it drops - six functions and six triggers on
    0002's tables - so dropping the tables is not enough."""
    down = resolver_sql("downgrade")
    up = resolver_sql("upgrade")
    for qualified in RESOLVER_TABLES:
        assert f"CREATE TABLE {qualified}" in up
        assert f"DROP TABLE {qualified}" in down


def test_the_resolver_migration_drops_in_the_reverse_of_the_creation_order() -> None:
    """Dropping `gate.department` before `gate.team` fails on the foreign key, and the same
    for `ops.routing_rung` before `ops.model_attempt`. A downgrade in the wrong order cannot
    run, which is discovered during a rollback at the worst possible moment."""
    down = resolver_sql("downgrade")
    positions = [down.index(f"DROP TABLE {q}") for q in RESOLVER_TABLES]
    assert positions == sorted(positions, reverse=True)


def test_the_resolver_migration_changes_no_data() -> None:
    """Both counters start empty and their readers coalesce to zero, precisely so this
    migration stays a schema change. Schema and data in one migration cannot be rolled back
    independently: the schema half reverses and the data half usually cannot."""
    emitted = resolver_sql("upgrade").upper()
    for statement in ("INSERT INTO", "DELETE FROM"):
        assert statement not in emitted, f"the migration emits {statement}"


@pytest.mark.parametrize("qualified", RESOLVER_TABLES)
def test_row_level_security_is_enabled_on_every_table_0003_adds(qualified: str) -> None:
    """A table without it is one forgotten WHERE clause away from returning every row to
    every caller. `sweep_rls` covers `auth`, `gate`, `obs`, `proj`, `know`, `agent`, `mem` and
    `er` and not `ops`, so nothing in CI would notice the three routing tables missing it.
    This is what does."""
    assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" in resolver_sql("upgrade")


@pytest.mark.parametrize("qualified", SOFT_DELETED_RESOLVER)
def test_the_policy_on_a_soft_deleted_table_0003_adds_still_permits_retiring_a_row(
    qualified: str,
) -> None:
    """Without an explicit `WITH CHECK`, PostgreSQL reuses the USING expression to check the
    new row, so setting `deleted_at` would be refused by the very policy meant to hide the
    row afterwards. Soft delete would be impossible and the failure would read as a
    permissions bug."""
    _schema, _, name = qualified.partition(".")
    policy = (
        f"CREATE POLICY {name}_live ON {qualified} FOR ALL TO brain_app "
        "USING (deleted_at IS NULL) WITH CHECK (true)"
    )
    assert policy in resolver_sql("upgrade")


@pytest.mark.parametrize("qualified", RESOLVER_TABLES)
def test_every_table_0003_adds_grants_the_application_role_what_it_needs(
    qualified: str,
) -> None:
    """0001 granted no default privileges on purpose, so a table that forgets to grant is a
    table the application cannot read at all - and the failure arrives at the first request
    rather than at deploy. The triggers run as the same role, so this is also what they
    hold."""
    assert f"ON {qualified} TO brain_app" in resolver_sql("upgrade")


def test_nothing_0003_grants_can_hard_delete() -> None:
    """A hard delete destroys the audit trail for the thing deleted. It would also let a
    counter go backwards, which hands out a cache key that was already used under a wider
    entitlement."""
    for line in rendered("upgrade", MIGRATION_RESOLVER).splitlines():
        if line.strip().startswith("GRANT"):
            assert "DELETE" not in line.upper(), line.strip()


def test_nothing_0003_runs_needs_a_superuser() -> None:
    """0001 creates the application role `NOBYPASSRLS`, and a SECURITY DEFINER function would
    undo that from the inside: the triggers would do more than the application can, which is
    a privilege escalation living inside the permission system."""
    emitted = resolver_sql("upgrade").upper() + resolver_sql("downgrade").upper()
    for forbidden in ("SUPERUSER", "BYPASSRLS", "SET ROLE", "SECURITY DEFINER"):
        assert forbidden not in emitted, f"the migration runs {forbidden}"
