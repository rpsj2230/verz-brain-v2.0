"""The entitlement resolver, and the tables the rest of the model was waiting on.

0002 built somewhere to put a grant. This is the migration that makes a grant *mean*
something without the meaning being recomputed, differently, by whoever needed it next.

**One resolver, in the database, called by both the application and a trigger.** That is the
entire point of `gate.resolve_entitlements`. Two implementations of "what does this person
hold" drift, and the drift is invisible until somebody sees a record they should not: the
Python side gets a new rule, the SQL side does not, and every test passes because each is
self-consistent. The function here is the authority, the application loads through it, and
the audit trigger consults it on every entitlement write - so a resolver that has stopped
working fails loudly at write time rather than quietly at read time.

**Three counters, none of them written by the application.** `gate.grants_version` bumps per
principal on any write to any grant-bearing table; `gate.policy_epoch` increments globally on
every entitlement mutation; and both move from AFTER triggers rather than from code, because
the path that forgets is the one written in a hurry during an incident. The version is what
`brain.gate.resolve.cache_key` puts in the key, so a bump orphans every cached entry for that
person in the same instant - which is what makes a revocation take effect now rather than in
sixty seconds.

**The audit row is written by the trigger, and the digest is computed in the database.**
`brain.audit.ledger.append` states the fork in the road: "Either the caller reads the clock
from the database and passes it in, or the digest has to be computed in the database." This
takes the second branch, because M1.4.8's whole content is that forgetting is impossible, and
a ledger write a caller can omit is a ledger with holes exactly where somebody was in a
hurry. `obs.audit_entry_hash` is `compute_entry_hash` in SQL: same domain separator, same
length-prefixed concatenation, same sorted detail keys, same sha256. It is a second
implementation of one function and it is the one place in this file where that was
unavoidable, so `tests/unit/test_resolver.py` pins the pieces it shares with the Python and
CI checks that a trigger-written entry verifies under `AuditChain.verify`.

**Disabling cascades to sessions.** `auth.principal.disabled_at` is a column, and a column
disables nobody who is already holding a token. The trigger ends every live session and bumps
the person's grants version, so the console tab and the cached entitlement both stop working
at the same moment the checkbox is ticked. Re-enabling restores the ability to sign in and
never the sessions that were ended, which is deliberate: a session is evidence about the
moment it opened.

Four things to read before changing this file.

**The SQL is written out, not assembled**, as in 0001 and 0002. Nothing here interpolates a
value into a statement.

**Nothing imports `brain.tables`.** The check constraints below are the same predicates the
models declare, copied deliberately, so this migration keeps describing the database it
actually built. `tests/unit/test_tables.py` compares the two.

**The writes inside the trigger bodies are `MERGE`, not `INSERT`, and that is not a
preference.** `brain.ops.migration_policy.DML` searches a migration's *text* for the two
keywords that open a row insert and refuses them alongside `op.create_table`, because schema
and data in one migration cannot be rolled back independently. The rule is right and the check
cannot tell the difference between a data migration and a statement inside a `$$ ... $$`
function body that runs months later at write time. This migration genuinely writes no data -
even
`gate.policy_epoch` starts empty, with readers coalescing to zero, rather than being seeded -
so rather than defeat the check quietly, the statements are written in the standard SQL
upsert form, which for two of the three is the natural spelling anyway. The check should
learn to skip function bodies; until it does, the ledger append reads its own `ROW_COUNT` so
that a skipped write raises instead of vanishing, which is the failure that got
`CREATE RULE ... DO INSTEAD NOTHING` rejected in 0002. `GET DIAGNOSTICS` rather than `FOUND`,
because whether `MERGE` sets `FOUND` is a question about a PostgreSQL version and `ROW_COUNT`
is not.

**`gate.resolve_entitlements` is stricter than `EntitlementSet.scope_for` in exactly one
way, and it is on purpose.** A disabled or retired principal resolves to no grants at all.
The pydantic type has no notion of `disabled_at` - the column does not exist on `Principal` -
so this is not a disagreement the two could have; it is the database refusing to hand out
grants the type was never told about. Everything else agrees, and
`tests/unit/test_resolver.py` asserts that against the fixture personas.

Task ids: M1.2.3, M1.4.4, M1.4.5, M1.4.6, M1.4.7, M1.4.8, M1.5.1, M2.1.1, M2.2.1, M5.2.2,
M5.3.1, M5.3.4

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: Every table this migration creates, ordered so a table appears after everything it points
#: at. `downgrade` walks this in reverse, which is what makes the two provably inverse
#: without anybody maintaining a second list.
TABLES: tuple[str, ...] = (
    "gate.scope",
    "gate.department",
    "gate.team",
    "auth.session",
    "gate.grants_version",
    "gate.policy_epoch",
    "ops.routing_tier",
    "ops.routing_rung",
    "ops.model_attempt",
)

#: The tables that carry `deleted_at` and therefore get the `_live` policy. The rest get a
#: policy admitting every row, because retiring one of them would be a second way of saying
#: something the row already says.
SOFT_DELETED: tuple[str, ...] = (
    "gate.scope",
    "gate.department",
    "gate.team",
    "ops.routing_tier",
    "ops.routing_rung",
)

# ------------------------------------------------------------------ grammars
# Copied from the domain types rather than imported from them; see the module docstring.
# `brain.core.department.SLUG_PATTERN`
SLUG_GRAMMAR = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
# `brain.audit.ledger.TRACE_ID`
TRACE_ID = r"^[A-Za-z0-9_.-]{1,64}$"

# --------------------------------------------------------------- vocabularies
CHANNEL_IN = "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'webhook', 'whatsapp')"
END_REASON_IN = "end_reason IN ('expired', 'principal_disabled', 'principal_retired', 'signed_out')"
TIER_IN = "tier IN ('heavy', 'main', 'none', 'small')"
RUNG_ROLE_IN = "role IN ('cross_provider_failover', 'primary', 'same_provider_failover')"
OUTCOME_IN = (
    "outcome IN ('circuit_open', 'connection_error', 'context_exceeded', 'ok', "
    "'provider_error', 'rate_limited', 'stopped', 'timeout')"
)

#: `Scope.model_dump()` is `{"clauses": [...]}`; the same predicate 0002 applies to a grant's
#: scope, applied here to a rung's.
SCOPE_SHAPE = "jsonb_typeof(scope) = 'object' AND jsonb_typeof(scope -> 'clauses') = 'array'"

#: The mirror image, for the document form `parse_predicate` reads. There is no positive
#: shape to check, so this refuses the one object that is definitely the other shape.
PREDICATE_SHAPE = "jsonb_typeof(predicate) = 'object' AND NOT (predicate ? 'clauses')"

#: Live rows only, for every table carrying `deleted_at`.
LIVE = "deleted_at IS NULL"

#: `brain.audit.ledger.GENESIS_HASH` and `brain.tables.audit.UNSUPPLIED_ENT_HASH`, as SQL.
GENESIS_HASH_SQL = "repeat('0', 64)"
UNSUPPLIED_ENT_HASH_SQL = "repeat('0', 32)"

#: Any constant works; it only has to be the same in every backend. The ledger's `seq` and
#: `prev_hash` are read and written as one step, and two concurrent appends that both read
#: the same head would produce a fork - which `uq_audit_entry_prev_hash` would refuse, but as
#: a unique violation on an unrelated-looking statement rather than as a wait.
LEDGER_LOCK_ID = 8_274_419_004


# ----------------------------------------------------------------- the resolver (M1.4.4)
# `LANGUAGE sql` rather than plpgsql, and `STABLE` rather than VOLATILE. Both matter. A plain
# SQL body can be inlined by the planner, so calling this in a join does not become a
# per-row function call; and STABLE tells the planner the answer cannot change inside one
# statement, which is what makes it usable from a trigger without re-reading the world.
#
# `p_now` is a parameter and `now()` is deliberately not a default. `brain.models.routing`'s
# circuit breaker makes the same argument at length: a function that reads the clock itself
# cannot be tested at a boundary, and every boundary in this system - a contractor's expiry,
# a grant's `not_after` - is exactly where the interesting behaviour is.
#
# The shape of the returned document is `EntitlementSet.model_dump()`, so the application
# does `EntitlementSet.model_validate(...)` and gets the type's own validators for free
# rather than reassembling grants from rows. A row-returning function was the first draft and
# was rejected for that reason: it made the caller responsible for building the set, and a
# caller building a set is a caller who can forget `not_after`.
#
# `not_after` is the *principal's*, carried onto the set exactly as `standing_entitlement`
# carries it, because `EntitlementSet.scope_for` is where expiry is enforced. Dropping the
# grants of an expired principal here instead would give the same answers today and a
# different `ent_hash`, and an `ent_hash` that does not know about the expiry is a cache key
# reachable from both sides of it.
RESOLVER = """
CREATE FUNCTION gate.resolve_entitlements(p_principal_id text, p_now timestamptz)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    SELECT jsonb_build_object(
        'principal_id', p_principal_id,
        'not_after', (
            SELECT to_jsonb(pr.not_after)
            FROM auth.principal pr
            WHERE pr.id = p_principal_id
              AND pr.deleted_at IS NULL
        ),
        'grants', COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'capability', jsonb_build_object('value', held.capability),
                    'scope', held.scope
                )
                ORDER BY held.capability, held.scope::text
            )
            FROM (
                SELECT g.capability, g.scope
                FROM gate.capability_grant g
                JOIN auth.principal pr ON pr.id = g.principal_id
                WHERE g.principal_id = p_principal_id
                  AND g.deleted_at IS NULL
                  AND (g.not_after IS NULL OR g.not_after > p_now)
                  AND pr.deleted_at IS NULL
                  AND pr.disabled_at IS NULL
                UNION ALL
                SELECT member.capability, a.scope
                FROM gate.capability_pack_assignment a
                JOIN gate.capability_pack k
                  ON k.id = a.pack_id
                 AND k.deleted_at IS NULL
                JOIN auth.principal pr ON pr.id = a.principal_id
                CROSS JOIN LATERAL unnest(k.capabilities) AS member(capability)
                WHERE a.principal_id = p_principal_id
                  AND a.deleted_at IS NULL
                  AND (a.not_after IS NULL OR a.not_after > p_now)
                  AND pr.deleted_at IS NULL
                  AND pr.disabled_at IS NULL
            ) AS held
        ), '[]'::jsonb)
    )
$$
"""

# ------------------------------------------------------- the ledger's digest, in SQL
# `brain.audit.ledger.compute_entry_hash`, statement for statement.
#
# Three details are load-bearing and each of them is a way the two could disagree silently:
#
# `length()` counts characters in PostgreSQL and `len()` counts characters in Python, so the
# length prefix agrees. `octet_length` would not, for anything outside ASCII.
#
# The timestamp is rendered the way `datetime.isoformat()` renders an aware UTC value:
# microseconds are omitted entirely when they are zero and printed to six digits otherwise.
# Getting this wrong produces a digest that differs only for entries written on a whole
# second, which is roughly one in a million and impossible to reproduce on demand.
#
# `ORDER BY ... COLLATE "C"` sorts detail keys by code point, which is what Python's
# `sorted()` does. The database's own collation may sort punctuation differently, and a
# details mapping with both `a_b` and `ab` in it would then hash differently on the two
# sides.
#
# STABLE rather than IMMUTABLE, which was the first draft and is a lie PostgreSQL does not
# check. `to_char(timestamp, text)` is itself declared STABLE because its output can depend
# on `lc_time`, and a function marked stricter than its own body lets the planner fold calls
# it should not. It costs nothing here: every call is made with fresh arguments.
AUDIT_HASH_FUNCTION = """
CREATE FUNCTION obs.audit_entry_hash(
    p_seq bigint,
    p_at timestamptz,
    p_actor_id text,
    p_action text,
    p_subject text,
    p_ent_hash text,
    p_trace_id text,
    p_details jsonb,
    p_prev_hash text
) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_stamp text;
    v_joined text := '';
    v_part text;
    v_key text;
BEGIN
    v_stamp := to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US');
    IF right(v_stamp, 7) = '.000000' THEN
        v_stamp := left(v_stamp, length(v_stamp) - 7);
    END IF;
    v_stamp := v_stamp || '+00:00';

    FOREACH v_part IN ARRAY ARRAY[
        'brain.audit.v1',
        p_prev_hash,
        p_seq::text,
        v_stamp,
        p_actor_id,
        p_action,
        p_subject,
        p_ent_hash,
        p_trace_id
    ] LOOP
        v_joined := v_joined || length(v_part)::text || ':' || v_part;
    END LOOP;

    FOR v_key IN
        SELECT k FROM jsonb_object_keys(p_details) AS k ORDER BY k COLLATE "C"
    LOOP
        v_joined := v_joined || length(v_key)::text || ':' || v_key;
        v_part := p_details ->> v_key;
        v_joined := v_joined || length(v_part)::text || ':' || v_part;
    END LOOP;

    RETURN encode(sha256(convert_to(v_joined, 'UTF8')), 'hex');
END;
$$
"""

# -------------------------------------------------- the audit trigger (M1.4.8)
# Written by the trigger and never by the caller. `tg_table_name` is the only thing that
# differs between a grant and a pack assignment, so one function serves both: two functions
# would be two places to fix when the ledger's shape changes, and the one that gets fixed is
# whichever the person was looking at.
#
# A pack assignment is recorded as `grant`, because `AuditAction` has no member for one and
# adding one was rejected - the enum's own docstring explains that a new member is meant to
# be a deliberate edit in two places, and this is not a new kind of event. `details` carries
# the pack name, so the two remain distinguishable.
#
# `holds` is what makes this trigger call the resolver, and the reason is not decoration: a
# revocation that leaves somebody with nothing and one that leaves them with plenty are
# different events, and an entry naming only the capability that went cannot tell them apart.
# It also means `gate.resolve_entitlements` runs on every entitlement write, so a resolver
# that has stopped working is discovered by the person making the change rather than by the
# person who later sees too much.
#
# Considered and rejected as the resolver's trigger-side caller: a BEFORE INSERT guard
# refusing a grant wider than the granter's own reach. It is a genuinely valuable property
# and it needs `Capability.covers` - trailing-star expansion and all - evaluated in SQL,
# which is a second implementation of the one rule this whole design exists to have only
# once. The guard belongs where `covers` already lives.
AUDIT_TRIGGER_FUNCTION = """
CREATE FUNCTION gate.record_entitlement_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_row jsonb := to_jsonb(NEW);
    v_action text;
    v_actor text;
    v_supplied text;
    v_subject text;
    v_details jsonb;
    v_seq bigint;
    v_prev text;
    v_entry text;
    v_at timestamptz := now();
    v_ent_hash text;
    v_trace text;
    v_written integer;
BEGIN
    IF tg_op = 'INSERT' THEN
        IF NEW.deleted_at IS NOT NULL THEN
            RETURN NULL;
        END IF;
        v_action := 'grant';
    ELSE
        IF OLD.deleted_at IS NOT NULL OR NEW.deleted_at IS NULL THEN
            RETURN NULL;
        END IF;
        v_action := 'revoke';
    END IF;

    v_supplied := NULLIF(current_setting('brain.actor_id', true), '');
    v_actor := COALESCE(v_supplied, v_row ->> 'granted_by');
    v_subject := 'grant:' || (v_row ->> 'id');

    IF tg_table_name = 'capability_grant' THEN
        v_details := jsonb_build_object('capability', v_row ->> 'capability');
    ELSE
        v_details := jsonb_build_object(
            'pack',
            COALESCE(
                (SELECT k.name FROM gate.capability_pack k
                  WHERE k.id = (v_row ->> 'pack_id')::uuid),
                'unknown'
            )
        );
    END IF;
    v_details := v_details || jsonb_build_object('source', tg_table_name);

    IF v_supplied IS NULL THEN
        v_details := v_details || jsonb_build_object('actor', 'inferred');
    END IF;

    v_details := v_details || jsonb_build_object(
        'holds',
        CASE
            WHEN jsonb_array_length(
                gate.resolve_entitlements(v_row ->> 'principal_id', v_at) -> 'grants'
            ) = 0 THEN 'none'
            ELSE 'some'
        END
    );

    PERFORM pg_advisory_xact_lock(8274419004);
    SELECT COALESCE(max(e.seq) + 1, 0) INTO v_seq FROM obs.audit_entry e;
    SELECT COALESCE(
        (SELECT e.entry_hash FROM obs.audit_entry e ORDER BY e.seq DESC LIMIT 1),
        repeat('0', 64)
    ) INTO v_prev;
    v_ent_hash := COALESCE(NULLIF(current_setting('brain.ent_hash', true), ''), repeat('0', 32));
    v_trace := COALESCE(
        NULLIF(current_setting('brain.trace_id', true), ''),
        'tx.' || pg_current_xact_id()::text
    );
    v_entry := obs.audit_entry_hash(
        v_seq, v_at, v_actor, v_action, v_subject, v_ent_hash, v_trace, v_details, v_prev
    );

    MERGE INTO obs.audit_entry AS t
    USING (SELECT v_seq AS seq) AS s
       ON t.seq = s.seq
    WHEN NOT MATCHED THEN
        INSERT (seq, at, actor_id, action, subject, ent_hash, trace_id,
                details, prev_hash, entry_hash)
        VALUES (v_seq, v_at, v_actor, v_action, v_subject, v_ent_hash, v_trace,
                v_details, v_prev, v_entry);

    GET DIAGNOSTICS v_written = ROW_COUNT;
    IF v_written <> 1 THEN
        RAISE EXCEPTION USING
            MESSAGE = 'the ledger already holds seq ' || v_seq
                      || '; the audit entry was not appended',
            ERRCODE = 'restrict_violation',
            HINT = 'an append that is discarded silently is the failure this refuses';
    END IF;

    RETURN NULL;
END;
$$
"""

# --------------------------------------------- grants_version and policy_epoch (M1.4.6, M1.4.7)
# One function for the tables that carry a principal, one for the pack, because a pack has no
# principal of its own: editing the bundle changes what everybody holding it holds, so the
# bump fans out across the live assignments.
#
# Both counters are created on first use rather than seeded. A reader coalesces a missing row
# to zero, so a principal who has never held a grant needs no row and this migration writes no
# data - which is the difference between a schema change that reverses cleanly and one that
# does not.
BUMP_FUNCTION = """
CREATE FUNCTION gate.bump_grants_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_principal text := to_jsonb(NEW) ->> 'principal_id';
BEGIN
    MERGE INTO gate.grants_version AS gv
    USING (SELECT v_principal AS principal_id) AS s
       ON gv.principal_id = s.principal_id
    WHEN MATCHED THEN
        UPDATE SET version = gv.version + 1, updated_at = now()
    WHEN NOT MATCHED THEN
        INSERT (principal_id, version) VALUES (s.principal_id, 1);

    MERGE INTO gate.policy_epoch AS pe
    USING (SELECT 1 AS id) AS s
       ON pe.id = s.id
    WHEN MATCHED THEN
        UPDATE SET epoch = pe.epoch + 1, updated_at = now()
    WHEN NOT MATCHED THEN
        INSERT (id, epoch) VALUES (1, 1);

    RETURN NULL;
END;
$$
"""

BUMP_FOR_PACK_FUNCTION = """
CREATE FUNCTION gate.bump_grants_version_for_pack() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    MERGE INTO gate.grants_version AS gv
    USING (
        SELECT DISTINCT a.principal_id
        FROM gate.capability_pack_assignment a
        WHERE a.pack_id = NEW.id
          AND a.deleted_at IS NULL
    ) AS s
       ON gv.principal_id = s.principal_id
    WHEN MATCHED THEN
        UPDATE SET version = gv.version + 1, updated_at = now()
    WHEN NOT MATCHED THEN
        INSERT (principal_id, version) VALUES (s.principal_id, 1);

    MERGE INTO gate.policy_epoch AS pe
    USING (SELECT 1 AS id) AS s
       ON pe.id = s.id
    WHEN MATCHED THEN
        UPDATE SET epoch = pe.epoch + 1, updated_at = now()
    WHEN NOT MATCHED THEN
        INSERT (id, epoch) VALUES (1, 1);

    RETURN NULL;
END;
$$
"""

# ------------------------------------------------- the disable cascade (M1.2.3)
# Retirement is checked before disablement, because `deleted_at` is the stronger fact and a
# row that acquires both in one statement should record the stronger reason. Re-enabling
# reaches nothing: there is no branch here that clears `ended_at`, and that is the design -
# a session is evidence about the moment it opened, so restoring one would be asserting an
# authentication that never happened.
CASCADE_FUNCTION = """
CREATE FUNCTION auth.cascade_principal_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        UPDATE auth.session
           SET ended_at = now(), end_reason = 'principal_retired', updated_at = now()
         WHERE principal_id = NEW.id
           AND ended_at IS NULL;
    ELSIF NEW.disabled_at IS NOT NULL AND OLD.disabled_at IS NULL THEN
        UPDATE auth.session
           SET ended_at = now(), end_reason = 'principal_disabled', updated_at = now()
         WHERE principal_id = NEW.id
           AND ended_at IS NULL;
    ELSE
        RETURN NULL;
    END IF;

    MERGE INTO gate.grants_version AS gv
    USING (SELECT NEW.id AS principal_id) AS s
       ON gv.principal_id = s.principal_id
    WHEN MATCHED THEN
        UPDATE SET version = gv.version + 1, updated_at = now()
    WHEN NOT MATCHED THEN
        INSERT (principal_id, version) VALUES (s.principal_id, 1);

    RETURN NULL;
END;
$$
"""

#: Created after the functions they call. plpgsql resolves a function name at run time rather
#: than at creation time, so this ordering buys nothing from PostgreSQL - it is here so that
#: reading the file top to bottom is reading the dependency order, and so a test can say the
#: resolver exists before the thing that calls it.
TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER capability_grant_bumps_version
        AFTER INSERT OR UPDATE ON gate.capability_grant
        FOR EACH ROW EXECUTE FUNCTION gate.bump_grants_version()
    """,
    """
    CREATE TRIGGER capability_grant_is_audited
        AFTER INSERT OR UPDATE ON gate.capability_grant
        FOR EACH ROW EXECUTE FUNCTION gate.record_entitlement_change()
    """,
    """
    CREATE TRIGGER capability_pack_assignment_bumps_version
        AFTER INSERT OR UPDATE ON gate.capability_pack_assignment
        FOR EACH ROW EXECUTE FUNCTION gate.bump_grants_version()
    """,
    """
    CREATE TRIGGER capability_pack_assignment_is_audited
        AFTER INSERT OR UPDATE ON gate.capability_pack_assignment
        FOR EACH ROW EXECUTE FUNCTION gate.record_entitlement_change()
    """,
    """
    CREATE TRIGGER capability_pack_bumps_versions
        AFTER INSERT OR UPDATE ON gate.capability_pack
        FOR EACH ROW EXECUTE FUNCTION gate.bump_grants_version_for_pack()
    """,
    """
    CREATE TRIGGER principal_state_cascades
        AFTER UPDATE OF disabled_at, deleted_at ON auth.principal
        FOR EACH ROW EXECUTE FUNCTION auth.cascade_principal_state()
    """,
)

#: Named so `downgrade` drops exactly what `upgrade` created, and so a test can compare the
#: two sets rather than trusting that somebody remembered.
FUNCTIONS: tuple[str, ...] = (
    "gate.resolve_entitlements(text, timestamptz)",
    "obs.audit_entry_hash(bigint, timestamptz, text, text, text, text, text, jsonb, text)",
    "gate.record_entitlement_change()",
    "gate.bump_grants_version()",
    "gate.bump_grants_version_for_pack()",
    "auth.cascade_principal_state()",
)

#: `<trigger name>` and the table it lives on, for the same reason.
TRIGGER_TARGETS: tuple[tuple[str, str], ...] = (
    ("capability_grant_bumps_version", "gate.capability_grant"),
    ("capability_grant_is_audited", "gate.capability_grant"),
    ("capability_pack_assignment_bumps_version", "gate.capability_pack_assignment"),
    ("capability_pack_assignment_is_audited", "gate.capability_pack_assignment"),
    ("capability_pack_bumps_versions", "gate.capability_pack"),
    ("principal_state_cascades", "auth.principal"),
)

# ------------------------------------------------------------ row-level security
# Same pair per table as 0002, and the same `WITH CHECK (true)` beside a `USING` that hides
# retired rows: without it PostgreSQL reuses the USING expression to check the new row, so
# setting `deleted_at` would be refused by the very policy meant to hide the row afterwards.
#
# The four tables with no `deleted_at` get `USING (true)`. That is not a weaker policy by
# accident: row-level security on these tables is a floor, so that a table added later cannot
# be the one nobody enabled it on, and `sweep_rls` fails the build on a table without it.
# `auth.session` in particular must not filter ended rows - which sessions were open when
# somebody was disabled is exactly the question the cascade has to be auditable against.
RLS: tuple[str, ...] = (
    "ALTER TABLE gate.scope ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY scope_live ON gate.scope
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.department ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY department_live ON gate.department
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.team ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY team_live ON gate.team
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE auth.session ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY session_visible ON auth.session
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.grants_version ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY grants_version_visible ON gate.grants_version
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.policy_epoch ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY policy_epoch_visible ON gate.policy_epoch
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
    "ALTER TABLE ops.routing_tier ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY routing_tier_live ON ops.routing_tier
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE ops.routing_rung ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY routing_rung_live ON ops.routing_rung
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE ops.model_attempt ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY model_attempt_visible ON ops.model_attempt
        FOR ALL TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

# ------------------------------------------------------------------- privileges
# Per table, as 0001 said. No table grants DELETE: retirement is `deleted_at`, and the two
# tables here that cannot be retired - a version and an epoch - are counters that must not go
# backwards, so removing a row is exactly the operation to withhold.
#
# The triggers run as the invoking role, which is `brain_app`, and nothing here is SECURITY
# DEFINER. That means the privileges below are what the triggers hold as well: the audit
# trigger appends to `obs.audit_entry` under 0002's SELECT and INSERT grant, and the bump
# triggers need INSERT and UPDATE on the counters. A SECURITY DEFINER function would let the
# triggers do more than the application can, which is a privilege escalation sitting inside
# the permission system.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE ON gate.scope TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.department TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.team TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON auth.session TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.grants_version TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.policy_epoch TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON ops.routing_tier TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON ops.routing_rung TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON ops.model_attempt TO brain_app",
    "GRANT EXECUTE ON FUNCTION gate.resolve_entitlements(text, timestamptz) TO brain_app",
)


def _create_scope() -> None:
    op.create_table(
        "scope",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(60), nullable=False),
        # The document form `parse_predicate` reads, never `Scope.model_dump()`. The two are
        # both json objects and mean opposite things when read as the other.
        sa.Column("predicate", postgresql.JSONB(), nullable=False),
        sa.Column("is_department", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("label", sa.String(120), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"slug ~ '{SLUG_GRAMMAR}'", name="slug_grammar"),
        sa.CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        sa.CheckConstraint(PREDICATE_SHAPE, name="predicate_shape"),
        sa.CheckConstraint(
            "NOT is_department OR predicate <> '{}'::jsonb",
            name="a_department_scope_restricts_something",
        ),
        schema="gate",
    )
    op.create_index("ix_gate_scope_deleted_at", "scope", ["deleted_at"], schema="gate")
    op.create_index(
        "uq_scope_slug_live",
        "scope",
        ["slug"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "ix_scope_predicate",
        "scope",
        ["predicate"],
        schema="gate",
        postgresql_using="gin",
        postgresql_ops={"predicate": "jsonb_path_ops"},
    )


def _create_department() -> None:
    op.create_table(
        "department",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("company_id", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        # Not a foreign key: `gate.scope.slug` is unique only among live rows, and PostgreSQL
        # cannot back a foreign key with a partial unique index. A total unique constraint
        # would make retiring a scope a one-way door on its name.
        sa.Column("scope_slug", sa.String(60), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"slug ~ '{SLUG_GRAMMAR}'", name="slug_grammar"),
        sa.CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        sa.CheckConstraint(f"scope_slug ~ '{SLUG_GRAMMAR}'", name="scope_slug_grammar"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_present"),
        schema="gate",
    )
    op.create_index("ix_gate_department_deleted_at", "department", ["deleted_at"], schema="gate")
    op.create_index("ix_gate_department_company_id", "department", ["company_id"], schema="gate")
    op.create_index("ix_gate_department_scope_slug", "department", ["scope_slug"], schema="gate")
    op.create_index(
        "uq_department_company_id_slug_live",
        "department",
        ["company_id", "slug"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )


def _create_team() -> None:
    op.create_table(
        "team",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"slug ~ '{SLUG_GRAMMAR}'", name="slug_grammar"),
        sa.CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_present"),
        # "Within a department" as a foreign key rather than a convention. A team floating
        # outside one has no scope to be narrower than.
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["gate.department.id"],
            name="fk_team_department_id_department",
            ondelete="RESTRICT",
        ),
        schema="gate",
    )
    op.create_index("ix_gate_team_deleted_at", "team", ["deleted_at"], schema="gate")
    op.create_index("ix_gate_team_department_id", "team", ["department_id"], schema="gate")
    op.create_index(
        "uq_team_department_id_slug_live",
        "team",
        ["department_id", "slug"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )


def _create_session() -> None:
    op.create_table(
        "session",
        # The identity provider's session id, not a surrogate: the ledger refers to a session
        # as `session:<id>` and has to be joinable to this row.
        sa.Column("id", sa.String(120), primary_key=True, nullable=False),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("assurance", sa.SmallInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # When it actually stopped, which is not the same fact as when it was always going
        # to. Losing the difference would lose the only evidence the cascade ran.
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(CHANNEL_IN, name="channel"),
        sa.CheckConstraint("assurance BETWEEN 0 AND 3", name="assurance_in_range"),
        sa.CheckConstraint("expires_at > started_at", name="expires_after_it_starts"),
        sa.CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended_with_a_reason"),
        sa.CheckConstraint(END_REASON_IN, name="end_reason"),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_session_principal_id_principal",
            ondelete="RESTRICT",
        ),
        schema="auth",
    )
    op.create_index("ix_auth_session_principal_id", "session", ["principal_id"], schema="auth")
    # The cascade's working set, and the question the gate asks on every request. Partial,
    # because the ended rows are kept forever and are never the answer to it.
    op.create_index(
        "ix_session_principal_id_live",
        "session",
        ["principal_id"],
        schema="auth",
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def _create_grants_version() -> None:
    op.create_table(
        "grants_version",
        sa.Column("principal_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Monotonic. A counter that goes backwards hands out a cache key that was already
        # used under a wider entitlement, and whatever sits under it is still readable.
        sa.CheckConstraint("version >= 0", name="version_non_negative"),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_grants_version_principal_id_principal",
            ondelete="RESTRICT",
        ),
        schema="gate",
    )


def _create_policy_epoch() -> None:
    op.create_table(
        "policy_epoch",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False, nullable=False),
        sa.Column("epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # One row. Two would make the epoch an aggregate, and an aggregate over a table
        # anybody can write to is a number that moves when the wrong row goes.
        sa.CheckConstraint("id = 1", name="exactly_one_row"),
        sa.CheckConstraint("epoch >= 0", name="epoch_non_negative"),
        schema="gate",
    )


def _create_routing_tier() -> None:
    op.create_table(
        "routing_tier",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column(
            "rules", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(TIER_IN, name="tier"),
        sa.CheckConstraint("context_window >= 0", name="context_window_non_negative"),
        sa.CheckConstraint("jsonb_typeof(rules) = 'object'", name="rules_object"),
        schema="ops",
    )
    op.create_index("ix_ops_routing_tier_deleted_at", "routing_tier", ["deleted_at"], schema="ops")
    op.create_index(
        "uq_routing_tier_tier_live",
        "routing_tier",
        ["tier"],
        unique=True,
        schema="ops",
        postgresql_where=sa.text(LIVE),
    )


def _create_routing_rung() -> None:
    op.create_table(
        "routing_rung",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        # M5.3.2 replaces this with a trigger-derived value. Until then it is typed, which is
        # exactly why that leaf exists: a label a human writes drifts from the position and
        # provider it describes, and the console then shows a primary sitting third.
        sa.Column("role", sa.String(32), nullable=False),
        # No foreign key: the deployment registry is M5.1 and does not exist yet, and a key
        # pointing at a table nobody has designed is not an option.
        sa.Column("deployment_id", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("timeout_seconds", sa.Numeric(6, 2), nullable=False),
        sa.Column("max_concurrency", sa.SmallInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(TIER_IN, name="tier"),
        sa.CheckConstraint(RUNG_ROLE_IN, name="role"),
        sa.CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        sa.CheckConstraint("position >= 0", name="position_non_negative"),
        # The way to remove a rung is to remove it. A rung with zero attempts silently never
        # runs and reads in the console as configured.
        sa.CheckConstraint("attempts >= 1", name="at_least_one_attempt"),
        sa.CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        sa.CheckConstraint("max_concurrency >= 1", name="concurrency_at_least_one"),
        sa.CheckConstraint("length(btrim(deployment_id)) > 0", name="deployment_present"),
        schema="ops",
    )
    op.create_index("ix_ops_routing_rung_deleted_at", "routing_rung", ["deleted_at"], schema="ops")
    op.create_index("ix_ops_routing_rung_tier", "routing_rung", ["tier"], schema="ops")
    # `RoutingChain.__post_init__` refuses two rungs sharing a position, because the chain
    # order would then depend on insertion order and the executed chain would stop being
    # reconstructable from the attempt rows.
    op.create_index(
        "uq_routing_rung_tier_position_live",
        "routing_rung",
        ["tier", "position"],
        unique=True,
        schema="ops",
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "ix_routing_rung_scope",
        "routing_rung",
        ["scope"],
        schema="ops",
        postgresql_using="gin",
        postgresql_ops={"scope": "jsonb_path_ops"},
    )


def _create_model_attempt() -> None:
    op.create_table(
        "model_attempt",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("rung_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.CheckConstraint(OUTCOME_IN, name="outcome"),
        sa.CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_it_started",
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)",
            name="finished_with_an_outcome",
        ),
        sa.CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="status_code_is_a_status_code",
        ),
        sa.CheckConstraint(f"trace_id ~ '{TRACE_ID}'", name="trace_id_shape"),
        sa.ForeignKeyConstraint(
            ["rung_id"],
            ["ops.routing_rung.id"],
            name="fk_model_attempt_rung_id_routing_rung",
            ondelete="RESTRICT",
        ),
        schema="ops",
    )
    op.create_index("ix_ops_model_attempt_trace_id", "model_attempt", ["trace_id"], schema="ops")
    op.create_index("ix_ops_model_attempt_rung_id", "model_attempt", ["rung_id"], schema="ops")
    # One row per try. Two rows claiming one position in a trace would make the reconstructed
    # chain depend on which came back first, which is what recording it was for.
    op.create_index(
        "uq_model_attempt_trace_id_sequence",
        "model_attempt",
        ["trace_id", "sequence"],
        unique=True,
        schema="ops",
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 and 0002 do; this keeps the
    # constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    # And the advisory lock constant is written into the trigger body rather than formatted
    # in, so this is what keeps the two from drifting. The same for the two sentinels, which
    # have to be the widths `brain.audit.ledger` produces or every entry fails its check
    # constraint on the way in.
    assert str(LEDGER_LOCK_ID) in AUDIT_TRIGGER_FUNCTION
    assert GENESIS_HASH_SQL in AUDIT_TRIGGER_FUNCTION
    assert UNSUPPLIED_ENT_HASH_SQL in AUDIT_TRIGGER_FUNCTION

    # Creation order matches TABLES, which the downgrade reverses.
    _create_scope()
    _create_department()
    _create_team()
    _create_session()
    _create_grants_version()
    _create_policy_epoch()
    _create_routing_tier()
    _create_routing_rung()
    _create_model_attempt()

    for statement in RLS:
        op.execute(statement)

    # The resolver first, then the digest, then everything that calls them, then the triggers.
    # Reading order is dependency order.
    op.execute(RESOLVER)
    op.execute(AUDIT_HASH_FUNCTION)
    op.execute(AUDIT_TRIGGER_FUNCTION)
    op.execute(BUMP_FUNCTION)
    op.execute(BUMP_FOR_PACK_FUNCTION)
    op.execute(CASCADE_FUNCTION)

    for statement in TRIGGERS:
        op.execute(statement)

    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The triggers on 0002's tables have to go by name: they belong to tables this migration
    # does not drop, so nothing takes them with it. The triggers on this migration's own
    # tables would go with the table, and there are none.
    for name, qualified in reversed(TRIGGER_TARGETS):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {qualified}")

    # Functions live in a schema rather than on a table, so they are named too. Dropped after
    # the triggers that call them, which is the reverse of the order they were created in.
    for signature in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    # Policies, indexes and table privileges belong to their table and go with it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
