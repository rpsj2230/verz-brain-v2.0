"""The core tables: principals, channel identities, grants, packs, field policy, ledger.

0001 built the ground - extensions, nine schemas, and an application role that cannot
bypass row-level security. This is the first migration that creates something holding data,
and everything in it stands on that role being `NOBYPASSRLS`. Nothing below assumes
superuser: the policies name `brain_app`, the privileges are granted table by table as 0001
said they would be, and the one guarantee that has to survive an administrator is a trigger
rather than a privilege, because a privilege is exactly what an administrator has.

Three things are worth reading before changing this file.

**The SQL is written out, not assembled.** Same reason as 0001: nothing here interpolates a
value into a statement, so what runs against the database is what is on the page. The
policy and trigger blocks are literal strings in a tuple rather than a loop over table
names.

**Nothing imports `brain.tables`.** A migration that reads live model code stops describing
the database it actually built the moment the model changes, and then the history is a
history of the present. The check constraints below are the same predicates the models
declare, copied deliberately; `tests/unit/test_tables.py` compares the two so the copy
cannot rot silently.

**The ledger's append-only guarantee is three layers, and only one of them is real against
an administrator.** `brain_app` holds SELECT and INSERT on `obs.audit_entry` and no more;
row-level security admits SELECT and INSERT and nothing else, so every non-bypassing role is
refused; and a trigger raises for anybody at all, table owner and superuser included. A
`CREATE RULE ... DO INSTEAD NOTHING` was rejected because it discards the write silently -
the statement succeeds, no row changes, and whoever ran it believes the ledger now says
something it does not.

Task ids: M1.2.1, M1.2.2, M1.4.1, M1.4.3, M4.2.1, M24.1.1

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: Every table this migration creates, ordered so a table appears after everything it points
#: at. `downgrade` walks this in reverse, which is what makes the two provably inverse
#: without anybody maintaining a second list. `brain.tables.TABLES_IN_DEPENDENCY_ORDER`
#: carries the same tuple and the test suite compares them.
TABLES: tuple[str, ...] = (
    "auth.principal",
    "auth.principal_identity",
    "gate.capability_grant",
    "gate.capability_pack",
    "gate.capability_pack_assignment",
    "gate.field_policy",
    "obs.audit_entry",
)

# ------------------------------------------------------------------ grammars
# Copied from the domain types rather than imported from them; see the module docstring.
# `brain.core.entitlement.CAPABILITY_RE`
CAPABILITY_GRAMMAR = r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*|\.\*)*$"
# `brain.core.field_policy.NAME_PATTERN`
NAME_GRAMMAR = r"^[a-z][a-z0-9_]*$"
# `brain.audit.ledger.IDENTIFIER`, split so the subject grammar can re-anchor its tail.
IDENTIFIER_BODY = r"[A-Za-z0-9_.@-]{1,128}"
IDENTIFIER = f"^{IDENTIFIER_BODY}$"
# `brain.audit.ledger.TRACE_ID`
TRACE_ID = r"^[A-Za-z0-9_.-]{1,64}$"
# `brain.audit.ledger.ENT_HASH` and `DIGEST`
ENT_HASH_HEX = r"^[0-9a-f]{32}$"
SHA256_HEX = r"^[0-9a-f]{64}$"
# `brain.gate.ingress.identity_hash` returns a sha256 hexdigest, so the same shape.
IDENTITY_HASH_HEX = SHA256_HEX
# `brain.audit.ledger.SUBJECT_KINDS`, sorted.
SUBJECT_KINDS = "agent|artifact|connector|entity|grant|leash|principal|session"
SUBJECT_GRAMMAR = f"^({SUBJECT_KINDS}):{IDENTIFIER_BODY}$"

# --------------------------------------------------------------- vocabularies
KIND_IN = "kind IN ('human', 'service')"
EMPLOYMENT_IN = "employment IN ('contractor', 'partner', 'service', 'staff')"
BOUNDED_IN = "employment IN ('contractor', 'partner')"
CHANNEL_IN = "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'webhook', 'whatsapp')"
VERB_IN = "split_part(capability, ':', 1) IN ('admin', 'approve', 'invoke', 'read', 'write')"
CLASSIFICATION_IN = "classification IN ('confidential', 'internal', 'public', 'restricted')"
ACTION_IN = (
    "action IN ('break_glass', 'deny', 'entity_merge', 'grant', "
    "'leash_change', 'publish', 'revoke')"
)

#: `Scope.model_dump()` is `{"clauses": [...]}`. Checking the outer type alone would admit
#: the console's document form, `{"department": "web"}`, which deserialises to a scope with
#: no clauses - and a scope with no clauses is unrestricted. The weak version of this check
#: fails towards the whole company.
SCOPE_SHAPE = "jsonb_typeof(scope) = 'object' AND jsonb_typeof(scope -> 'clauses') = 'array'"

#: Live rows only, for every table carrying `deleted_at`.
LIVE = "deleted_at IS NULL"

# ------------------------------------------------------------ row-level security
# One pair per soft-deletable table: enable, then a policy naming the application role.
#
# `WITH CHECK (true)` beside `USING (deleted_at IS NULL)` is not sloppiness. Without an
# explicit WITH CHECK, PostgreSQL reuses the USING expression as the check on the new row,
# so retiring a row would be refused by the very policy that is meant to hide it afterwards:
# the update sets `deleted_at`, the resulting row fails `deleted_at IS NULL`, and a soft
# delete becomes impossible. USING decides which rows are visible and touchable; WITH CHECK
# decides what a row may become, and a row may become retired.
RLS: tuple[str, ...] = (
    "ALTER TABLE auth.principal ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY principal_live ON auth.principal
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE auth.principal_identity ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY principal_identity_live ON auth.principal_identity
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.capability_grant ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY capability_grant_live ON gate.capability_grant
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.capability_pack ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY capability_pack_live ON gate.capability_pack
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.capability_pack_assignment ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY capability_pack_assignment_live ON gate.capability_pack_assignment
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
    "ALTER TABLE gate.field_policy ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY field_policy_live ON gate.field_policy
        FOR ALL TO brain_app
        USING (deleted_at IS NULL)
        WITH CHECK (true)
    """,
)

# The ledger gets read and append, and no policy for anything else. PostgreSQL denies what
# no policy admits, so this is a second refusal of amendment sitting underneath the trigger.
LEDGER_RLS: tuple[str, ...] = (
    "ALTER TABLE obs.audit_entry ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY audit_entry_readable ON obs.audit_entry
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY audit_entry_appendable ON obs.audit_entry
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
)

# ------------------------------------------------------------ the append-only trigger
# `RAISE EXCEPTION USING MESSAGE = ...` rather than `RAISE EXCEPTION '%', tg_op`. The format
# form is the idiomatic one and it puts a percent sign into a statement that travels through
# a driver whose parameter style is pyformat; that is safe today, and it is the kind of safe
# that depends on nobody ever passing this statement a parameter. Building the message by
# concatenation costs nothing and removes the question.
APPEND_ONLY_FUNCTION = """
CREATE FUNCTION obs.audit_entry_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = 'obs.audit_entry is append-only; ' || tg_op || ' is refused',
        ERRCODE = 'restrict_violation',
        HINT = 'a wrong entry is corrected by appending another, never by editing it';
END;
$$
"""

# Statement-level rather than row-level, on purpose. A row trigger fires once per row, so a
# sweeping removal of the whole table is refused only after the server has walked it; a
# statement trigger refuses the statement before any row is touched. It also fires on a
# statement that would have matched nothing, which is the difference between "this was
# refused" and "this quietly did nothing" in the log.
#
# TRUNCATE is listed because it is the obvious way around the other two, and because
# PostgreSQL will not let a row-level trigger cover it at all.
APPEND_ONLY_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER audit_entry_refuses_amendment
        BEFORE UPDATE ON obs.audit_entry
        FOR EACH STATEMENT EXECUTE FUNCTION obs.audit_entry_is_append_only()
    """,
    """
    CREATE TRIGGER audit_entry_refuses_removal
        BEFORE DELETE ON obs.audit_entry
        FOR EACH STATEMENT EXECUTE FUNCTION obs.audit_entry_is_append_only()
    """,
    """
    CREATE TRIGGER audit_entry_refuses_truncation
        BEFORE TRUNCATE ON obs.audit_entry
        FOR EACH STATEMENT EXECUTE FUNCTION obs.audit_entry_is_append_only()
    """,
)

# ------------------------------------------------------------------- privileges
# 0001 deliberately granted no default privileges, so each table says what it allows. No
# table grants DELETE: retirement is `deleted_at`, and a hard delete destroys the audit
# trail for the thing deleted.
GRANTS: tuple[str, ...] = (
    "GRANT SELECT, INSERT, UPDATE ON auth.principal TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON auth.principal_identity TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.capability_grant TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.capability_pack TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.capability_pack_assignment TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON gate.field_policy TO brain_app",
    # Read and append. No UPDATE, and the trigger above means even holding it would not help.
    "GRANT SELECT, INSERT ON obs.audit_entry TO brain_app",
)


def _create_principal() -> None:
    op.create_table(
        "principal",
        # The principal id from the identity provider, not a surrogate: the ledger's
        # `actor_id` refers to a principal by this string and has to be joinable to it.
        sa.Column("id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("employment", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("primary_department", sa.String(120), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        # Reversible, and not the same fact as `deleted_at`: a disabled principal stays
        # visible because somebody has to be able to re-enable them.
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(KIND_IN, name="kind"),
        sa.CheckConstraint(EMPLOYMENT_IN, name="employment"),
        sa.CheckConstraint("length(btrim(display_name)) > 0", name="display_name_present"),
        # `Principal.model_post_init` calls an unbounded contractor the most common way a
        # permission model rots. The type refuses it on the way in; this refuses the row
        # that arrived some other way.
        sa.CheckConstraint(
            f"NOT ({BOUNDED_IN}) OR not_after IS NOT NULL",
            name="bounded_engagement_expires",
        ),
        schema="auth",
    )
    op.create_index("ix_auth_principal_deleted_at", "principal", ["deleted_at"], schema="auth")
    op.create_index(
        "ix_auth_principal_primary_department", "principal", ["primary_department"], schema="auth"
    )


def _create_principal_identity() -> None:
    op.create_table(
        "principal_identity",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        # The digest, never the identity. The check constraint below is what makes a raw
        # phone number impossible to store here even by a hand-typed statement.
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assurance", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(CHANNEL_IN, name="channel"),
        sa.CheckConstraint(f"identity_hash ~ '{IDENTITY_HASH_HEX}'", name="hash_shape"),
        sa.CheckConstraint("assurance BETWEEN 0 AND 1", name="assurance_at_most_bound"),
        # Constraints are listed in the order SQLAlchemy renders them from the models -
        # checks, then foreign keys - so the two produce byte-identical DDL and the test
        # comparing them can be an equality rather than a set of substring searches.
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_principal_identity_principal_id_principal",
            ondelete="RESTRICT",
        ),
        schema="auth",
    )
    op.create_index(
        "ix_auth_principal_identity_deleted_at", "principal_identity", ["deleted_at"], schema="auth"
    )
    op.create_index(
        "ix_auth_principal_identity_principal_id",
        "principal_identity",
        ["principal_id"],
        schema="auth",
    )
    # Unique among live rows only. A plain unique constraint would mean a phone number
    # belonging to somebody who left can never be bound to the new holder, because the
    # retired row still occupies the pair and nothing here holds DELETE to clear it.
    op.create_index(
        "uq_principal_identity_channel_identity_hash_live",
        "principal_identity",
        ["channel", "identity_hash"],
        unique=True,
        schema="auth",
        postgresql_where=sa.text(LIVE),
    )


def _create_capability_grant() -> None:
    op.create_table(
        "capability_grant",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("capability", sa.String(200), nullable=False),
        # No server default: an unrestricted default turns a forgotten field into a
        # company-wide grant, and neither failure announces itself.
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("granted_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"capability ~ '{CAPABILITY_GRAMMAR}'", name="capability_grammar"),
        sa.CheckConstraint(VERB_IN, name="capability_verb"),
        sa.CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="reason_present"),
        # The only foreign key a grant table carries runs to a principal. There is
        # deliberately none to any connector table: `sweep_grant_isolation` fails the build
        # on one, because a connector that could cascade into grants is a connector that can
        # touch the permission graph.
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_capability_grant_principal_id_principal",
            ondelete="RESTRICT",
        ),
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_grant_deleted_at", "capability_grant", ["deleted_at"], schema="gate"
    )
    op.create_index(
        "ix_gate_capability_grant_principal_id",
        "capability_grant",
        ["principal_id"],
        schema="gate",
    )
    # `EntitlementSet.scope_for` intersects the scopes of every grant covering a capability,
    # so a second grant narrows rather than widens: granting `read:client.name` in the sales
    # scope to somebody who holds it in the web scope leaves them able to read nothing. The
    # constraint turns that silent inversion into a refusal.
    op.create_index(
        "uq_capability_grant_principal_id_capability_live",
        "capability_grant",
        ["principal_id", "capability"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )
    # `jsonb_path_ops` rather than the default class: about a third the size, and the only
    # operator it gives up is key-existence, which no query against a stored predicate uses.
    # This index does nothing for `scope_sql.compile_where`, which compiles a scope into a
    # predicate over some other table; it serves the reverse question, "which grants mention
    # this department", which is how an access review and a revocation sweep both work.
    op.create_index(
        "ix_capability_grant_scope",
        "capability_grant",
        ["scope"],
        schema="gate",
        postgresql_using="gin",
        postgresql_ops={"scope": "jsonb_path_ops"},
    )


def _create_capability_pack() -> None:
    op.create_table(
        "capability_pack",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        # An array rather than a child table: a pack is read whole, assigned whole and
        # revoked whole, and the one query that looks inside it is a containment test the
        # GIN index below answers.
        sa.Column("capabilities", postgresql.ARRAY(sa.String(200)), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"name ~ '{NAME_GRAMMAR}'", name="name_grammar"),
        sa.CheckConstraint("length(btrim(description)) > 0", name="described"),
        # An empty pack is assignable, reviewable and grants nothing, so it looks like
        # access having been given and is not.
        sa.CheckConstraint("cardinality(capabilities) > 0", name="not_empty"),
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_pack_deleted_at", "capability_pack", ["deleted_at"], schema="gate"
    )
    op.create_index(
        "uq_capability_pack_name_live",
        "capability_pack",
        ["name"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "ix_capability_pack_capabilities",
        "capability_pack",
        ["capabilities"],
        schema="gate",
        postgresql_using="gin",
    )


def _create_capability_pack_assignment() -> None:
    op.create_table(
        "capability_pack_assignment",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        # The scope lives on the assignment, not on the pack. That is what makes packs worth
        # having: one `account_manager` pack held by eleven people in eleven scopes, rather
        # than eleven near-identical grant sets that drift.
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("granted_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="reason_present"),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["auth.principal.id"],
            name="fk_capability_pack_assignment_principal_id_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pack_id"],
            ["gate.capability_pack.id"],
            name="fk_capability_pack_assignment_pack_id_capability_pack",
            ondelete="RESTRICT",
        ),
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_pack_assignment_deleted_at",
        "capability_pack_assignment",
        ["deleted_at"],
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_pack_assignment_principal_id",
        "capability_pack_assignment",
        ["principal_id"],
        schema="gate",
    )
    op.create_index(
        "ix_gate_capability_pack_assignment_pack_id",
        "capability_pack_assignment",
        ["pack_id"],
        schema="gate",
    )
    op.create_index(
        "uq_capability_pack_assignment_principal_id_pack_id_live",
        "capability_pack_assignment",
        ["principal_id", "pack_id"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "ix_capability_pack_assignment_scope",
        "capability_pack_assignment",
        ["scope"],
        schema="gate",
        postgresql_using="gin",
        postgresql_ops={"scope": "jsonb_path_ops"},
    )


def _create_field_policy() -> None:
    op.create_table(
        "field_policy",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("entity", sa.String(60), nullable=False),
        sa.Column("field", sa.String(120), nullable=False),
        sa.Column("required_capability", sa.String(200), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"entity ~ '{NAME_GRAMMAR}'", name="entity_grammar"),
        sa.CheckConstraint(f"field ~ '{NAME_GRAMMAR}'", name="field_grammar"),
        sa.CheckConstraint(
            f"required_capability ~ '{CAPABILITY_GRAMMAR}'",
            name="capability_grammar",
        ),
        # A field policy gates returning a value, and returning is reading. Without this a
        # rule could be satisfied by a write capability, so permission to change a number
        # would confer permission to see it.
        sa.CheckConstraint(
            "split_part(required_capability, ':', 1) = 'read'",
            name="capability_is_a_read",
        ),
        sa.CheckConstraint(CLASSIFICATION_IN, name="classification"),
        schema="gate",
    )
    op.create_index(
        "ix_gate_field_policy_deleted_at", "field_policy", ["deleted_at"], schema="gate"
    )
    op.create_index("ix_gate_field_policy_entity", "field_policy", ["entity"], schema="gate")
    # `PolicyConflictError` one layer down: two live rules for one field would make "may
    # this person see this field" an evaluation-order problem, so the pair cannot be written.
    op.create_index(
        "uq_field_policy_entity_field_live",
        "field_policy",
        ["entity", "field"],
        unique=True,
        schema="gate",
        postgresql_where=sa.text(LIVE),
    )


def _create_audit_entry() -> None:
    op.create_table(
        "audit_entry",
        # Not an identity column. `AuditChain.append` computes `seq` from the previous entry
        # because a caller who can choose it can forge a link, and a database-generated
        # sequence is exactly such a caller.
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False, nullable=False),
        # No server default, unlike every other table here: `at` is inside the digest, so the
        # caller has to pass one authoritative clock's reading. A second timestamp filled in
        # by the database would disagree with the hashed one.
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(160), nullable=False),
        # The actor's reach as a digest, never the capabilities themselves.
        sa.Column("ent_hash", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("seq >= 0", name="seq_non_negative"),
        sa.CheckConstraint(f"actor_id ~ '{IDENTIFIER}'", name="actor_id_shape"),
        sa.CheckConstraint(ACTION_IN, name="action"),
        sa.CheckConstraint(f"subject ~ '{SUBJECT_GRAMMAR}'", name="subject_grammar"),
        sa.CheckConstraint(f"ent_hash ~ '{ENT_HASH_HEX}'", name="ent_hash_shape"),
        sa.CheckConstraint(f"trace_id ~ '{TRACE_ID}'", name="trace_id_shape"),
        sa.CheckConstraint(f"prev_hash ~ '{SHA256_HEX}'", name="prev_hash_shape"),
        sa.CheckConstraint(f"entry_hash ~ '{SHA256_HEX}'", name="entry_hash_shape"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        sa.CheckConstraint("entry_hash <> prev_hash", name="not_its_own_parent"),
        schema="obs",
    )
    for column in ("action", "actor_id", "at", "subject", "trace_id"):
        op.create_index(f"ix_obs_audit_entry_{column}", "audit_entry", [column], schema="obs")
    # A repeated digest is a repeated entry.
    op.create_index(
        "uq_audit_entry_entry_hash", "audit_entry", ["entry_hash"], unique=True, schema="obs"
    )
    # Two entries naming the same parent is a fork, and a fork is the one tamper that
    # survives `AuditChain.verify`: it walks a sequence it was handed and cannot see a branch
    # outside the window, so both halves of a forked history verify cleanly. Refusing two
    # children of one parent makes the chain linear by construction.
    op.create_index(
        "uq_audit_entry_prev_hash", "audit_entry", ["prev_hash"], unique=True, schema="obs"
    )


def upgrade() -> None:
    # The statements below name the role literally, the way 0001 does; this keeps the
    # constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)

    # Creation order matches TABLES, which the downgrade reverses.
    _create_principal()
    _create_principal_identity()
    _create_capability_grant()
    _create_capability_pack()
    _create_capability_pack_assignment()
    _create_field_policy()
    _create_audit_entry()

    for statement in RLS:
        op.execute(statement)
    for statement in LEDGER_RLS:
        op.execute(statement)

    op.execute(APPEND_ONLY_FUNCTION)
    for statement in APPEND_ONLY_TRIGGERS:
        op.execute(statement)

    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # Policies, triggers, indexes and table privileges all belong to their table and go with
    # it, so dropping the tables is enough for everything except the trigger function, which
    # lives in the schema rather than on the table.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
    op.execute("DROP FUNCTION IF EXISTS obs.audit_entry_is_append_only()")
