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

Task ids: M1.2.1, M1.2.2, M1.4.1, M1.4.3, M4.2.1, M24.1.1
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
from brain.audit.ledger import SUBJECT_KINDS, AuditAction
from brain.core.entitlement import VERBS
from brain.core.field_policy import Classification
from brain.core.principal import Employment, PrincipalKind
from brain.db import metadata
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import identity_hash
from brain.ops.migration_policy import check_file

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "versions" / "0002_core_tables.py"

#: Every table carrying `deleted_at`. The ledger is the one that must not.
SOFT_DELETED = tuple(t for t in tables.TABLES_IN_DEPENDENCY_ORDER if t != "obs.audit_entry")

#: A PostgreSQL dialect to render DDL against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


@functools.cache
def migration_module() -> types.ModuleType:
    """Import 0002 as a module so its constants and its two functions can be reached.

    Importing it runs nothing: `upgrade` and `downgrade` are functions, and `alembic.op` is
    a proxy that reaches a database only when one of them is called under a context that
    has one.
    """
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def rendered(direction: str) -> str:
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
    module = migration_module()
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
    for qualified in tables.TABLES_IN_DEPENDENCY_ORDER:
        assert qualified in metadata.tables, f"{qualified} is not on the metadata"
    assert set(metadata.tables) == set(tables.TABLES_IN_DEPENDENCY_ORDER)


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
    for qualified in tables.TABLES_IN_DEPENDENCY_ORDER:
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
    for qualified in tables.TABLES_IN_DEPENDENCY_ORDER:
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
    assert set(module.TABLES) == set(metadata.tables)


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
