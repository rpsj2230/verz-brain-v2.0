"""The SQL resolver, the two counters, the cascade, and the audit rows nobody can forget.

`tests/unit/test_tables.py` covers the shape of the tables 0003 adds. This covers the parts
of 0003 that are behaviour: a function, five trigger functions and six triggers.

**Where the line is drawn, and why it is drawn here.** There is no PostgreSQL on a
development machine and there is one in CI, so this file is in two halves. The first half
reads the SQL the migration actually emits and asserts things that are true of the text: that
the resolver is one function, that it is created before the thing that calls it, that upgrade
and downgrade name the same objects, that the digest written in SQL shares every constant
with the digest written in Python, and that no grant-bearing table is missing a trigger. All
of that is checkable without a server and none of it is checkable by reading the file's source
text, because a statement sitting in a constant that `upgrade` never executes would pass a
grep and build nothing.

The second half needs a server and says so. Three properties cannot be established any other
way, and they are the three that matter most:

- that `gate.resolve_entitlements` agrees with `EntitlementSet.scope_for` on every fixture
  persona, which is the whole argument for having one resolver rather than two;
- that an audit entry written by a trigger verifies under `AuditChain.verify`, which is the
  only real proof that `obs.audit_entry_hash` is `compute_entry_hash`;
- that disabling a principal actually ends their live sessions.

Those tests skip when `DATABASE_URL` is unset, exactly as `brain.ops.sweeps` does, and they
run the migrations themselves rather than assuming somebody else did. A skip is announced
rather than silent: `sweep_traceability` spent its entire life green while checking nothing,
and the lesson was that a check which cannot fail is worse than no check at all.

Task ids: M1.2.3, M1.4.4, M1.4.5, M1.4.6, M1.4.7, M1.4.8
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from brain.audit.ledger import DIGEST_CHARS, GENESIS_HASH, HASH_SCHEMA, AuditChain, AuditEntry
from brain.core.entitlement import Capability, EntitlementSet
from brain.db import normalise_database_url
from brain.gate.resolve import CACHE_TTL_SECONDS, cache_key
from brain.tables.audit import (
    ACTOR_SETTING,
    ENT_HASH_SETTING,
    GRANT_SUBJECT_KIND,
    PACK_ASSIGNMENT_ACTION,
    TRACE_ID_SETTING,
    UNSUPPLIED_ENT_HASH,
)
from tests.fixtures.company import NOW, build_company

# The render harness lives with the tests that first needed it. Importing it rather than
# copying twenty lines is the same decision the migration makes about grammars: two copies of
# a fact is two facts, and the one that gets fixed is whichever the person was looking at.
from tests.unit.test_tables import (
    MIGRATION_RESOLVER,
    REPO,
    migration_module,
    rendered,
    squash,
)

#: The grant tables, and what each one must have a trigger for. Written here rather than read
#: from the migration so the migration is compared against something.
GRANT_BEARING = ("gate.capability_grant", "gate.capability_pack_assignment")


def upgrade_sql() -> str:
    return rendered("upgrade", MIGRATION_RESOLVER)


def downgrade_sql() -> str:
    return rendered("downgrade", MIGRATION_RESOLVER)


def function_body(qualified: str) -> str:
    """One function's text, from the emitted SQL rather than from the file.

    Reading the emitted SQL matters: a statement sitting in a constant that `upgrade` never
    executes would pass a search of the source text and build nothing.
    """
    emitted = upgrade_sql()
    start = emitted.index(f"CREATE FUNCTION {qualified}")
    following = [
        emitted.index(marker, start + 1)
        for marker in ("CREATE FUNCTION ", "CREATE TRIGGER ", "GRANT ")
        if marker in emitted[start + 1 :]
    ]
    return emitted[start : min(following)] if following else emitted[start:]


def resolver_body() -> str:
    return function_body("gate.resolve_entitlements")


# =============================================================== the resolver (M1.4.4)
def test_there_is_exactly_one_entitlement_resolver_in_the_database() -> None:
    """The whole argument for M1.4.4. Two implementations of what a person holds drift, and
    the drift is invisible until somebody sees a record they should not: one side gets a new
    rule, the other does not, and every test passes because each is self-consistent. Delete
    this and a second resolver can appear without anybody noticing it is a second one."""
    emitted = upgrade_sql()
    assert emitted.count("CREATE FUNCTION gate.resolve_entitlements") == 1
    assert len(re.findall(r"CREATE FUNCTION \w+\.resolve\w*", emitted)) == 1


def test_the_resolver_is_created_before_the_trigger_that_calls_it() -> None:
    """plpgsql resolves a function name at run time, so PostgreSQL would accept the reverse
    order and fail later, at the first grant, with a message about an unknown function rather
    than about a migration. Ordering the file by dependency is what makes reading it top to
    bottom tell the truth."""
    emitted = upgrade_sql()
    resolver = emitted.index("CREATE FUNCTION gate.resolve_entitlements")
    caller = emitted.index("CREATE FUNCTION gate.record_entitlement_change")
    trigger = emitted.index("CREATE TRIGGER capability_grant_is_audited")
    assert resolver < caller < trigger


def test_the_resolver_is_called_by_the_application_and_by_a_trigger() -> None:
    """M1.4.4 in one line. The trigger call is not decoration: it means the resolver runs on
    every entitlement write, so a resolver that has stopped working is found by the person
    making the change rather than by the person who later sees too much. The application half
    is the grant of EXECUTE, without which `EntitlementStore` cannot reach it at all."""
    emitted = upgrade_sql()
    assert "gate.resolve_entitlements(v_row ->> 'principal_id', v_at)" in squash(emitted)
    assert (
        "GRANT EXECUTE ON FUNCTION gate.resolve_entitlements(text, timestamptz) TO brain_app"
        in squash(emitted)
    )


def test_the_resolver_returns_the_shape_an_entitlement_set_validates() -> None:
    """A row-returning function was the first draft and made the caller responsible for
    assembling the set - and a caller assembling a set is a caller who can forget `not_after`,
    which is where expiry is enforced. Returning the document `EntitlementSet` validates means
    the type's own validators run on the way out of the database."""
    body = squash(resolver_body())
    for field in EntitlementSet.model_fields:
        assert f"'{field}'," in body, f"the resolver never builds {field}"
    # The nested shapes, so a `Grant` and its `Capability` round trip rather than arriving as
    # a bare string the model would reject.
    assert "jsonb_build_object('value', held.capability)" in body


def test_the_resolver_carries_the_principals_own_expiry_rather_than_applying_it() -> None:
    """`EntitlementSet.scope_for` returns None for an expired set, and `ent_hash` includes
    `not_after`. Dropping an expired principal's grants in SQL instead would give the same
    answers today and a different hash, and a hash that does not know about the expiry is a
    cache key reachable from both sides of it."""
    body = squash(resolver_body())
    assert "'not_after', ( SELECT to_jsonb(pr.not_after)" in body


def test_the_resolver_honours_every_way_a_grant_stops_counting() -> None:
    """Four of them, and each is a separate row-level fact: the grant is retired, the grant
    has expired, the pack behind it is retired, or the assignment has expired. Missing any one
    means a revoked or lapsed permission keeps resolving, which is the failure the whole
    module exists to prevent."""
    body = squash(resolver_body())
    assert "g.deleted_at IS NULL" in body
    assert "(g.not_after IS NULL OR g.not_after > p_now)" in body
    assert "k.deleted_at IS NULL" in body
    assert "(a.not_after IS NULL OR a.not_after > p_now)" in body


def test_the_resolver_refuses_to_hand_out_grants_for_a_disabled_principal() -> None:
    """The one place the SQL is deliberately stricter than `EntitlementSet.scope_for`, and it
    is not a disagreement the two could have: `Principal` has no `disabled_at` field, so the
    type was never told about the column. M1.2.3's cascade ends the sessions; this is the
    other half, so a token that outlived the cascade still resolves to nothing."""
    body = squash(resolver_body())
    assert body.count("pr.disabled_at IS NULL") == 2, "both grants and packs must be filtered"
    assert body.count("pr.deleted_at IS NULL") == 3


def test_a_pack_assignment_resolves_through_the_pack_rather_than_beside_it() -> None:
    """`packs.expand` is the only route from a pack to a grant in Python, and the reason is
    that a second route is a second definition of what a pack means. The union here is that
    rule in SQL: the assignment contributes its scope, the pack contributes its capabilities,
    and nothing invents a scope of its own."""
    body = squash(resolver_body())
    assert "unnest(k.capabilities)" in body
    assert "SELECT member.capability, a.scope" in body


def test_the_resolver_cannot_see_a_role() -> None:
    """M1.3.5, expressed the only way SQL allows. `assert_no_role_in_resolution` refuses a
    Python resolver that even takes a role parameter, because a resolver that can see roles is
    one refactor from consulting them - at which point "what can this person see" stops being
    a lookup and becomes a walk over their roles, their roles' grants and the scopes on
    each."""
    body = resolver_body().lower()
    assert "role" not in body.replace("brain_app", ""), "the resolver mentions a role"


def test_the_resolver_takes_the_clock_as_an_argument() -> None:
    """The same rule `CircuitBreaker` follows and states: a function that reads the clock
    itself cannot be tested at a boundary, and every boundary here - a contractor's expiry, a
    grant's `not_after` - is exactly where the interesting behaviour is."""
    body = resolver_body()
    assert "p_now timestamptz" in body
    assert "now()" not in body


# ===================================================== the digest, written twice (M1.4.8)
def test_the_ledger_digest_in_sql_shares_every_constant_with_the_one_in_python() -> None:
    """`obs.audit_entry_hash` is `compute_entry_hash` in SQL, which is a second implementation
    of one function - the one place in 0003 where that was unavoidable, because the trigger
    has to produce the chain link itself. A domain separator, a length prefix and a hash that
    differ by one character produce a ledger that fails verification for every entry, and the
    failure looks like tampering."""
    body = function_body("obs.audit_entry_hash")
    assert f"'{HASH_SCHEMA}'" in body
    # `_digest` prefixes each part with its length and a colon. `length()` counts characters
    # in PostgreSQL and `len()` counts characters in Python, so the two agree; `octet_length`
    # would not, for anything outside ASCII.
    assert "length(v_part)::text || ':' || v_part" in body
    assert "encode(sha256(convert_to(v_joined, 'UTF8')), 'hex')" in body
    assert str(DIGEST_CHARS) not in body, "the digest width is sha256's, never a literal"


def test_the_digest_sorts_detail_keys_the_way_python_sorts_them() -> None:
    """`compute_entry_hash` sorts details before hashing so two entries built in different
    orders digest identically. Python sorts by code point; a database's own collation may
    ignore punctuation, so a details mapping holding both `a_b` and `ab` would hash
    differently on the two sides - and only for entries that happen to carry both."""
    assert 'ORDER BY k COLLATE "C"' in upgrade_sql()


def test_the_timestamp_is_rendered_the_way_python_renders_an_aware_utc_value() -> None:
    """`datetime.isoformat()` omits microseconds entirely when they are zero and prints six
    digits otherwise. Getting this wrong produces a digest that differs only for entries
    written on a whole second, which is roughly one in a million and impossible to reproduce
    on demand."""
    emitted = squash(upgrade_sql())
    assert "'YYYY-MM-DD\"T\"HH24:MI:SS.US'" in emitted
    assert "IF right(v_stamp, 7) = '.000000' THEN" in emitted
    assert "v_stamp || '+00:00'" in emitted


def test_the_ledger_append_raises_rather_than_discarding_the_entry() -> None:
    """0002 rejected `CREATE RULE ... DO INSTEAD NOTHING` because it discards a write
    silently: the statement succeeds, no row changes, and whoever ran it believes the ledger
    now says something it does not. The upsert form used here can skip a row for the same
    reason, so it reads its own `ROW_COUNT` and turns a skip into an exception. `FOUND` was
    the first draft: whether `MERGE` sets it is a question about a PostgreSQL version, and a
    safety check whose behaviour depends on the answer is not one."""
    emitted = squash(upgrade_sql())
    assert (
        "GET DIAGNOSTICS v_written = ROW_COUNT; IF v_written <> 1 THEN RAISE EXCEPTION" in emitted
    )
    assert "the audit entry was not appended" in emitted


def test_the_ledger_append_serialises_rather_than_racing() -> None:
    """`seq` and `prev_hash` are read and written as one step. Two concurrent appends reading
    the same head would write a fork, which `uq_audit_entry_prev_hash` refuses - but as a
    unique violation on a statement that mentions neither, so the person whose grant failed
    has no idea why. The lock turns a collision into a wait."""
    emitted = squash(upgrade_sql())
    assert "PERFORM pg_advisory_xact_lock(" in emitted


# ================================================== the audit trigger (M1.4.8)
@pytest.mark.parametrize("qualified", GRANT_BEARING)
def test_every_grant_bearing_table_writes_its_own_audit_row(qualified: str) -> None:
    """M1.4.8's whole content is that forgetting is impossible. A caller that has to remember
    to audit is a caller that forgets during an incident, which is the one occasion the entry
    is worth having."""
    emitted = squash(upgrade_sql())
    assert (
        f"AFTER INSERT OR UPDATE ON {qualified} "
        "FOR EACH ROW EXECUTE FUNCTION gate.record_entitlement_change()" in emitted
    )


def test_a_grant_and_a_revocation_are_recorded_as_different_actions() -> None:
    """`AuditAction` keeps DENY and REVOKE apart for the same reason, and its docstring gives
    it: collapsing events that differ by orders of magnitude in frequency makes "who removed
    her access, and when" unanswerable without reading everything in between. A soft delete
    is the revocation, so that is what the trigger keys on."""
    emitted = squash(upgrade_sql())
    assert "v_action := 'grant'" in emitted
    assert "v_action := 'revoke'" in emitted
    assert "IF OLD.deleted_at IS NOT NULL OR NEW.deleted_at IS NULL THEN RETURN NULL" in emitted


def test_a_pack_assignment_is_recorded_under_an_action_that_already_exists() -> None:
    """`AuditAction` has no member for assigning a pack and this deliberately does not add
    one: the vocabulary is closed so that a new auditable action is a deliberate edit in two
    places. An assignment is a grant - every capability it expands to is one the holder now
    has - so it is recorded as one, with the pack name in the details."""
    assert PACK_ASSIGNMENT_ACTION.value == "grant"
    emitted = squash(upgrade_sql())
    assert "jsonb_build_object( 'pack'," in emitted


def test_the_audit_row_names_the_grant_it_is_about() -> None:
    """`SUBJECT_KINDS` is closed because the client-visible audit view filters on it. There is
    no `pack` kind and none is added here: widening a closed set so one trigger reads better
    is how it stops being closed."""
    assert f"v_subject := '{GRANT_SUBJECT_KIND}:' || (v_row ->> 'id')" in squash(upgrade_sql())


def test_an_unsupplied_entitlement_hash_is_a_sentinel_and_not_a_plausible_digest() -> None:
    """The trigger cannot compute a real `ent_hash`: reproducing pydantic's json rendering in
    SQL would be a second implementation of the digest, and the two would agree right up until
    somebody added a field. Thirty-two zeros is unmistakably "not supplied"; `md5('')` would
    be a digest of something, and an auditor would have no way to tell."""
    assert UNSUPPLIED_ENT_HASH == "0" * 32
    assert "repeat('0', 32)" in upgrade_sql()
    assert f"repeat('0', {DIGEST_CHARS})" in upgrade_sql()
    assert GENESIS_HASH == "0" * DIGEST_CHARS


def test_the_application_can_attribute_a_write_and_is_told_when_it_did_not() -> None:
    """The actor of a revocation is not on the row: the grant tables record `granted_by` and
    nothing records who removed it. The session settings let the application say; when it does
    not, the entry records that the attribution was inferred, so nobody reads `granted_by` as
    a record of who revoked."""
    emitted = squash(upgrade_sql())
    for setting in (ACTOR_SETTING, ENT_HASH_SETTING, TRACE_ID_SETTING):
        assert f"current_setting('{setting}', true)" in emitted
    assert "jsonb_build_object('actor', 'inferred')" in emitted


# ============================== grants_version and policy_epoch (M1.4.5, M1.4.6, M1.4.7)
@pytest.mark.parametrize("qualified", GRANT_BEARING)
def test_every_grant_bearing_table_bumps_the_version_by_trigger(qualified: str) -> None:
    """M1.4.6 says by trigger and not by application code, and the reason is that the path
    which forgets is the one written in a hurry. A revocation that does not bump is a
    revocation that does not take effect until the TTL expires, and nothing anywhere reports
    it."""
    emitted = squash(upgrade_sql())
    assert (
        f"AFTER INSERT OR UPDATE ON {qualified} "
        "FOR EACH ROW EXECUTE FUNCTION gate.bump_grants_version()" in emitted
    )


def test_editing_a_pack_bumps_everybody_holding_it() -> None:
    """A pack has no principal of its own, so the bump has to fan out over the live
    assignments. Without this, removing a capability from a pack leaves every holder's cached
    entitlement intact and still serving the capability that was just taken away."""
    emitted = squash(upgrade_sql())
    assert (
        "AFTER INSERT OR UPDATE ON gate.capability_pack "
        "FOR EACH ROW EXECUTE FUNCTION gate.bump_grants_version_for_pack()" in emitted
    )
    assert "SELECT DISTINCT a.principal_id FROM gate.capability_pack_assignment a" in emitted


def test_the_version_the_cache_key_uses_is_the_one_the_trigger_bumps() -> None:
    """M1.4.5. `resolve.cache_key` puts the version in the key rather than checking it after a
    read, because checking after means the stale value was already in hand. That only
    invalidates anything if something moves the version, and until 0003 nothing did:
    `VersionSource` was a protocol with no implementation."""
    assert cache_key("u_weiling", 7) == "ent:u_weiling:7"
    assert cache_key("u_weiling", 7) != cache_key("u_weiling", 8)
    assert CACHE_TTL_SECONDS == 60
    emitted = squash(upgrade_sql())
    assert "UPDATE SET version = gv.version + 1" in emitted


def test_every_entitlement_mutation_moves_the_global_epoch() -> None:
    """M1.4.7, and it is separate from the version on purpose: one is per principal and one is
    global. A cached *answer* drew on rows that other people's grants also govern, so a
    per-principal counter cannot invalidate it."""
    emitted = squash(upgrade_sql())
    assert emitted.count("UPDATE SET epoch = pe.epoch + 1") == 2
    assert "MERGE INTO gate.policy_epoch AS pe" in emitted


def test_neither_counter_is_seeded_by_the_migration() -> None:
    """A missing row reads as zero, which is why 0003 can create nine tables and still be a
    pure schema change. Seeding them would put data in a migration, and the schema half
    reverses while the data half usually cannot."""
    emitted = squash(upgrade_sql()).upper()
    assert "INSERT INTO" not in emitted


# ================================================== the disable cascade (M1.2.3)
def test_disabling_a_principal_reaches_the_sessions_already_open() -> None:
    """A disabled person with a live session is not disabled. `disabled_at` is a column, and a
    column changes nothing that is already running - a console tab holding a token, a Lark
    thread mid-conversation. Without the cascade, M1.2.3 is a checkbox that stops nobody."""
    emitted = squash(upgrade_sql())
    assert (
        "AFTER UPDATE OF disabled_at, deleted_at ON auth.principal "
        "FOR EACH ROW EXECUTE FUNCTION auth.cascade_principal_state()" in emitted
    )
    assert "end_reason = 'principal_disabled'" in emitted
    assert "end_reason = 'principal_retired'" in emitted


def test_disabling_a_principal_also_orphans_their_cached_entitlement() -> None:
    """Ending the sessions is half of it. A cached entitlement outlives the session it was
    resolved for, so without the bump a disabled person's reach stays in Valkey under a key
    that is still constructible for up to sixty seconds."""
    cascade = squash(function_body("auth.cascade_principal_state"))
    assert "MERGE INTO gate.grants_version AS gv" in cascade


def test_re_enabling_a_principal_does_not_restore_a_session() -> None:
    """Deliberately one-way. A session is evidence about the moment it opened, so restoring
    one would assert an authentication that never happened. Re-enabling restores the ability
    to sign in, which is a different thing and is what the person actually needs."""
    emitted = squash(upgrade_sql())
    assert "ended_at = NULL" not in emitted
    assert "end_reason = NULL" not in emitted


# ================================================ the migration reverses what it built
def test_the_upgrade_and_the_downgrade_name_the_same_functions() -> None:
    """Six functions live in a schema rather than on a table, so dropping the tables does not
    take them with it. A downgrade that leaves one behind makes the next upgrade fail on a
    name that already exists, which is discovered during a rollback."""
    module = migration_module(MIGRATION_RESOLVER)
    up = squash(upgrade_sql())
    down = squash(downgrade_sql())
    for signature in module.FUNCTIONS:
        name = signature.split("(", 1)[0]
        assert f"CREATE FUNCTION {name}(" in up, f"{name} is never created"
        assert f"DROP FUNCTION IF EXISTS {signature}" in down, f"{name} is never dropped"
    assert up.count("CREATE FUNCTION ") == len(module.FUNCTIONS)
    assert down.count("DROP FUNCTION ") == len(module.FUNCTIONS)


def test_the_upgrade_and_the_downgrade_name_the_same_triggers() -> None:
    """The six triggers sit on 0002's tables, which this migration does not drop, so nothing
    takes them with it either. One left behind would keep firing against a function that is
    gone, and the first grant after the rollback would fail."""
    module = migration_module(MIGRATION_RESOLVER)
    up = squash(upgrade_sql())
    down = squash(downgrade_sql())
    for name, qualified in module.TRIGGER_TARGETS:
        assert f"CREATE TRIGGER {name}" in up
        assert f"DROP TRIGGER IF EXISTS {name} ON {qualified}" in down
    assert up.count("CREATE TRIGGER ") == len(module.TRIGGER_TARGETS)
    assert down.count("DROP TRIGGER ") == len(module.TRIGGER_TARGETS)


# ============================================================ what only a server can say
def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("BRAIN_DATABASE_URL") or None


@pytest.fixture(scope="module")
def migrated_url() -> str:
    """A database at head. Runs the migrations rather than assuming somebody else did.

    CI's `tests` job runs pytest before its own `alembic upgrade head` step, so a test that
    assumed a migrated schema would skip in exactly the place it is meant to run. `upgrade` is
    idempotent, so the later step still does what it says.
    """
    url = _database_url()
    if url is None:
        pytest.skip("DATABASE_URL is unset, so the resolver cannot be run; CI always sets it")
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    config.set_main_option("sqlalchemy.url", normalise_database_url(url).replace("%", "%%"))
    command.upgrade(config, "head")
    return url


@pytest.fixture
def loaded(migrated_url: str) -> Iterator[Any]:
    """The synthetic company, in a real database, rolled back afterwards.

    Every write here fires the triggers 0003 installs, which is the point: the audit rows and
    the version bumps under test are produced by loading the fixture rather than by a test
    reaching for them.
    """
    import psycopg

    connection = psycopg.connect(migrated_url)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM obs.audit_entry")
            row = cur.fetchone()
            assert row is not None
            if row[0]:
                pytest.skip("the ledger already holds entries; this test needs a clean chain")
            for person in build_company().values():
                principal = person.principal
                cur.execute(
                    "INSERT INTO auth.principal"
                    " (id, kind, employment, display_name, primary_department, not_after)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        principal.id,
                        str(principal.kind),
                        str(principal.employment),
                        principal.display_name,
                        principal.primary_department,
                        principal.not_after,
                    ),
                )
                for grant in person.grants:
                    cur.execute(
                        "INSERT INTO gate.capability_grant"
                        " (principal_id, capability, scope, granted_by, reason)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (
                            principal.id,
                            grant.capability.value,
                            json.dumps(grant.scope.model_dump(mode="json")),
                            "u_seed",
                            "loaded from the test fixture",
                        ),
                    )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _resolve(connection: Any, principal_id: str) -> EntitlementSet:
    with connection.cursor() as cur:
        cur.execute("SELECT gate.resolve_entitlements(%s, %s)", (principal_id, NOW))
        row = cur.fetchone()
    assert row is not None
    return EntitlementSet.model_validate(row[0])


@pytest.mark.needs_db
def test_the_sql_resolver_agrees_with_scope_for_on_every_fixture_persona(loaded: Any) -> None:
    """The property M1.4.4 exists for, and the only test that can establish it.

    Every capability any persona holds is probed against every persona, so the assertion
    covers what each person holds *and* what they do not: a resolver that returned one row too
    many would fail on somebody else's capability rather than on their own.

    Note which rule is deliberately not under test here. A partner holds nothing whatever the
    grant table says, and that is `standing_entitlement`'s job one layer above the store;
    `Person.entitlement()` builds the set the same way, so the two agree on the rows.
    """
    people = build_company()
    probes = sorted({g.capability.value for p in people.values() for g in p.grants})
    assert len(probes) > 5, "the probe set would not distinguish anybody"

    for principal_id, person in people.items():
        expected = person.entitlement()
        actual = _resolve(loaded, principal_id)
        assert actual.principal_id == principal_id
        assert actual.not_after == expected.not_after
        for value in probes:
            capability = Capability(value=value)
            assert actual.scope_for(capability, NOW) == expected.scope_for(capability, NOW), (
                f"{principal_id} disagrees about {value}"
            )


@pytest.mark.needs_db
def test_a_trigger_written_audit_entry_verifies_as_a_chain(loaded: Any) -> None:
    """The only real proof that `obs.audit_entry_hash` is `compute_entry_hash`. Every entry
    below was written by a trigger and hashed in SQL; `AuditChain.verify` recomputes each
    digest in Python and walks the links. A single character of disagreement between the two
    implementations breaks every entry, and this is what says so."""
    with loaded.cursor() as cur:
        cur.execute(
            "SELECT seq, at, actor_id, action, subject, ent_hash, trace_id,"
            " details, prev_hash, entry_hash FROM obs.audit_entry ORDER BY seq"
        )
        rows = cur.fetchall()

    expected = sum(len(p.grants) for p in build_company().values())
    assert len(rows) == expected, "a grant was written without an audit entry"

    entries = [
        AuditEntry(
            seq=row[0],
            at=row[1],
            actor_id=row[2],
            action=row[3],
            subject=row[4],
            ent_hash=row[5],
            trace_id=row[6],
            details=row[7],
            prev_hash=row[8],
            entry_hash=row[9],
        )
        for row in rows
    ]
    assert entries[0].prev_hash == GENESIS_HASH
    assert AuditChain(entries).verify() is None
    assert {e.action.value for e in entries} == {"grant"}
    assert all(e.ent_hash == UNSUPPLIED_ENT_HASH for e in entries)


@pytest.mark.needs_db
def test_a_revocation_bumps_the_version_and_leaves_its_own_entry(loaded: Any) -> None:
    """The two halves of M1.4.6 and M1.4.8 on the path that matters. A revocation that does
    not bump keeps serving the permission for as long as the TTL lasts, and one that leaves no
    entry makes "who removed her access, and when" unanswerable."""
    with loaded.cursor() as cur:
        cur.execute(
            "SELECT version FROM gate.grants_version WHERE principal_id = %s", ("u_weiling",)
        )
        row = cur.fetchone()
        assert row is not None
        before = row[0]

        cur.execute(
            "UPDATE gate.capability_grant SET deleted_at = now()"
            " WHERE principal_id = %s AND capability = %s",
            ("u_weiling", "read:client.name"),
        )

        cur.execute(
            "SELECT version FROM gate.grants_version WHERE principal_id = %s", ("u_weiling",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] > before

        cur.execute("SELECT action, details FROM obs.audit_entry ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "revoke"
        assert row[1]["capability"] == "read:client.name"
        assert row[1]["holds"] == "some"

    resolved = _resolve(loaded, "u_weiling")
    assert resolved.scope_for(Capability(value="read:client.name"), NOW) is None


@pytest.mark.needs_db
def test_disabling_a_principal_ends_their_live_sessions_and_leaves_them_ended(
    loaded: Any,
) -> None:
    """M1.2.3, both directions. Disabling has to reach a session that is already open, and
    re-enabling must not restore it: a session is evidence about the moment it opened, so
    bringing one back would assert an authentication that never happened."""
    with loaded.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.session"
            " (id, principal_id, channel, assurance, started_at, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            ("sess_weiling", "u_weiling", "console", 2, NOW, NOW + timedelta(hours=8)),
        )

        cur.execute("UPDATE auth.principal SET disabled_at = now() WHERE id = %s", ("u_weiling",))
        cur.execute(
            "SELECT ended_at, end_reason FROM auth.session WHERE id = %s", ("sess_weiling",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] == "principal_disabled"

        # And the reach goes with it, whatever the grant table still says.
        assert _resolve(loaded, "u_weiling").grants == ()

        cur.execute("UPDATE auth.principal SET disabled_at = NULL WHERE id = %s", ("u_weiling",))
        cur.execute(
            "SELECT ended_at, end_reason FROM auth.session WHERE id = %s", ("sess_weiling",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] == "principal_disabled"

    assert _resolve(loaded, "u_weiling").grants != ()
