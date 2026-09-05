"""The principal, and the channel identities that reach one.

`brain.core.principal.Principal` is the only thing in this system that carries authority,
and until this module existed it carried it for the lifetime of one request. There was
nowhere to record that a contractor's engagement ends on Friday, nowhere to record that an
account was disabled this morning, and nowhere at all to answer the question every inbound
message asks first: whose number is this?

**What breaks without it.** The gate cannot identify anybody. `brain.gate.ingress.resolve`
takes a dict of bindings and has no source for it; a `Binding` produced by `bind` is
returned to the caller and then discarded. Every leaf under M1.2 is blocked on a row.

Two decisions are load-bearing here.

**The raw channel identity is never stored.** `principal_identity` holds the digest that
`brain.gate.ingress.identity_hash` produces and nothing else. A table of phone numbers and
Lark open ids joined to names and departments is a company phone book with a permission
model attached, and it is the single most valuable thing in this database to anybody who
should not have it. The column carries a check constraint pinning it to sixty-four
lowercase hex characters, so a raw number cannot be written there even by a hand-typed
statement: the shape of the column refuses it, rather than a convention asking nicely.

**`disabled_at` and `deleted_at` are different facts.** Disabling is reversible and is what
M1.2.3 cascades to sessions; a disabled principal must stay visible, because somebody has
to be able to re-enable them and because the ledger refers to them. Deleting is offboarding,
and the row-level security policy hides those rows. Collapsing the two would mean either
that re-enabling is impossible or that offboarded staff keep appearing in every list.

**A disabled principal with a live session is not disabled.** `disabled_at` is a column,
and a column on its own changes nothing that is already running: a console tab holding a
token, a Lark thread mid-conversation, a scheduled run that opened its session an hour ago.
`auth.session` below is where those live, and `0003` ends them from a trigger on
`auth.principal` rather than from whichever code path happened to set the column. That is
the whole content of M1.2.3, and the direction is deliberately one-way - re-enabling
restores the ability to sign in, never the sessions that were ended.

**A grant a directory made lives in its own table, not beside a grant a person made.**
`auth.directory_role_grant` is the whole of decision 21 in `docs/needs-rupash.md`, and the
argument for it is about what the sync is able to reach rather than about what it is
careful to avoid. A directory sync has to take a role away when somebody leaves a group, so
it must be able to delete; put both kinds of grant in one table and every run has to decide
which rows are its own, either by getting it wrong and deleting somebody's hand-made grant
or by carrying a `source` column that every query afterwards has to remember to filter on.
A separate table makes "the sync may delete anything it can see" true and safe at the same
time: what it can see is only ever its own rows. See the class docstring for the key, and
`brain.identity.directory` for the reconciliation that reads it.

Task ids: M1.1.5, M1.2.1, M1.2.2, M1.2.3
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.principal import Employment, PrincipalKind
from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.identity.roles import Role

#: `Principal.id` is `Field(max_length=128)`. Every column that holds a principal id is
#: this wide, including the ones that hold it without a foreign key.
PRINCIPAL_ID_CHARS = 128

#: `Principal.display_name` is `Field(max_length=200)`.
DISPLAY_NAME_CHARS = 200

#: `brain.gate.ingress.identity_hash` returns a sha256 hexdigest. Not derived by calling it
#: here - an import-time digest is a surprising thing to find in a table definition - but
#: `tests/unit/test_tables.py` calls it and compares, so the two cannot drift apart quietly.
IDENTITY_HASH_CHARS = 64

#: The shape `identity_hash` produces, as a POSIX regex. Written from the function's own
#: return rather than from a comment about it, so a change to the digest breaks the check
#: constraint's test rather than silently admitting a shorter value.
IDENTITY_HASH_PATTERN = f"^[0-9a-f]{{{IDENTITY_HASH_CHARS}}}$"

#: `Principal.model_post_init` requires these two to carry `not_after`. Named here so the
#: constraint below is generated from the same tuple the model checks against.
BOUNDED_EMPLOYMENTS = (Employment.CONTRACTOR, Employment.PARTNER)

#: `BreakGlassSession.session_id` is `Field(max_length=120)`, and the ledger's subject
#: grammar is what bounds it: a session id has to survive `session:<id>` in an audit row.
SESSION_ID_CHARS = 120

#: Wider than any of the six role values, and wide enough that a seventh does not need a
#: column change - which matters more here than elsewhere, because this column is part of a
#: primary key. The width is headroom and not a check: `one_of("role", Role)` is what
#: actually constrains the value, and `tests/unit/test_directory_role_grant.py` asserts every
#: role fits, so a role name that outgrew the column would fail a test rather than be
#: truncated into one the check constraint then refuses at three in the morning.
ROLE_CHARS = 32

#: `GroupRoleRule.group` is `Field(max_length=300)`. The group path as the identity provider
#: spells it, e.g. `/brain/approver/web`, and it is part of a primary key, so the two widths
#: must agree: a rule the type accepts and the column refuses is a sync that fails on one
#: client's directory and nowhere in CI.
SOURCE_GROUP_CHARS = 300


class SessionEndReason(enum.StrEnum):
    """Why a session stopped being live. Closed, and every member is a field name.

    Closed for the reason `BreakGlassReason` is closed: the value is recordable in the
    ledger only if it matches the field-name grammar, and "how many sessions did the
    disable cascade end last quarter" is a query rather than a reading exercise.

    There is deliberately no `revoked` member, and the omission is not cosmetic.
    `brain.identity.packs.subtractive_state` refuses that word across the identity package
    because a row that subtracts turns resolution into an evaluation-order problem. A
    session is not a grant, so the rule does not formally reach here - but two vocabularies
    where one says `revoked` and the other refuses to is how the word gets back in.
    """

    #: The holder signed out.
    SIGNED_OUT = "signed_out"
    #: `expires_at` passed and a sweep tidied the row up.
    EXPIRED = "expired"
    #: The cascade from `auth.principal.disabled_at` (M1.2.3).
    PRINCIPAL_DISABLED = "principal_disabled"
    #: The cascade from `auth.principal.deleted_at`. Separate from the above because
    #: offboarding and a temporary disable are different events, and the question asked
    #: afterwards ("was she still working here?") has different answers.
    PRINCIPAL_RETIRED = "principal_retired"


def one_of(column: str, values: Iterable[str]) -> str:
    """An `IN` predicate over a closed vocabulary, built from the vocabulary itself.

    Every closed enum in this system is a `StrEnum`, and the corresponding check constraint
    could be written out by hand. It is generated instead, because a hand-written list is a
    second copy of the enum that stops matching it the first time somebody adds a member -
    and the failure is silent in exactly the wrong direction: the new member is refused by
    the database at three in the morning, after passing every test that only exercised the
    Python side.

    Generating it does not make the deployed constraint update itself; that needs a
    migration, and it should. What it does is make the model and the migration disagree the
    moment the enum changes, and `tests/unit/test_tables.py` turns that disagreement into a
    failing build.

    Values are sorted so the rendered SQL does not depend on declaration order. Enum
    declaration order is not part of an enum's contract, and a reordering during a merge
    would otherwise look like a schema change.

    This helper lives in `identity.py` rather than in the package `__init__` because the
    package imports every table module and a helper in `__init__` would have to be bound
    before those imports ran - a partially-initialised module is a fragile place to keep
    something three other modules depend on.
    """
    listed = ", ".join(f"'{value}'" for value in sorted(values))
    return f"{column} IN ({listed})"


class PrincipalRow(TimestampMixin, SoftDeleteMixin, Base):
    """`auth.principal`. Mirrors `brain.core.principal.Principal` (M1.2.1).

    Named `PrincipalRow` rather than `Principal` on purpose. There is already exactly one
    `Principal` in this system and it is the pydantic type; a second class with the same
    name in a sibling package is an import somebody eventually gets wrong, and the way you
    find out is that an entitlement is computed from a database row that skipped every
    validator the real type carries.
    """

    __tablename__ = "principal"

    #: The principal id itself, not a surrogate, and **not the identity provider's**.
    #: It is minted here (`c_0447`), and the IdP subject is a separate fact recorded
    #: against it, resolved by `oidc.PrincipalDirectory.principal_for_subject`. An earlier
    #: version of this comment said the id arrived from the provider, which contradicted
    #: both that lookup and the example beside it: `c_0447` is not a Keycloak subject.
    #:
    #: The indirection costs one join and buys the only migration that matters. Replacing
    #: the identity provider, or moving one client onto their own, rewrites the mapping and
    #: nothing else. Were the id the provider's, that change would rewrite every grant and
    #: every row of the audit ledger, and a ledger whose subject ids were rewritten is a
    #: ledger nobody can attest to.
    #:
    #: A surrogate key here would mean the ledger's `actor_id` pointed at nothing joinable,
    #: which is the one property it needs.
    id: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), primary_key=True)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    employment: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_CHARS), nullable=False)
    primary_department: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    #: Enforced at entitlement time rather than at login time, per `Principal`. The column
    #: is the record of it; `EntitlementSet.is_expired` is the enforcement.
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Reversible. See the module docstring on why this is not `deleted_at`.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Text plus a check constraint rather than a PostgreSQL enum type. A native enum
        # cannot have a member removed at all and needs ALTER TYPE to gain one, which
        # cannot run inside the single transaction `migrations/env.py` wraps a migration
        # in on older servers; a check constraint is dropped and recreated like anything
        # else, so the downgrade is ordinary.
        CheckConstraint(one_of("kind", PrincipalKind), name="kind"),
        CheckConstraint(one_of("employment", Employment), name="employment"),
        CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="display_name_present",
        ),
        # `Principal.model_post_init` refuses an unbounded contractor or partner, calling it
        # the most common way a permission model rots. The rule is worth having twice: the
        # type catches it on the way in, and this catches the row that arrived some other
        # way - a migration, a backfill, a hand-written statement during an incident.
        CheckConstraint(
            f"NOT ({one_of('employment', BOUNDED_EMPLOYMENTS)}) OR not_after IS NOT NULL",
            name="bounded_engagement_expires",
        ),
        {"schema": "auth"},
    )


class PrincipalIdentityRow(TimestampMixin, SoftDeleteMixin, Base):
    """`auth.principal_identity`. Mirrors `brain.gate.ingress.Binding` (M1.2.2).

    One live row per channel identity. `resolve` looks a sender up by digest and expects
    at most one answer; two live rows for one digest would make "who is this?" depend on
    which one came back first, which is a principal-confusion bug and not a duplicate-row
    bug.
    """

    __tablename__ = "principal_identity"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The digest, never the identity. See the module docstring.
    identity_hash: Mapped[str] = mapped_column(String(IDENTITY_HASH_CHARS), nullable=False)

    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        # RESTRICT rather than CASCADE. A binding that vanishes when a principal row goes
        # takes with it the only record that a number was ever bound to anybody, and the
        # question asked after an incident is exactly "whose was this number".
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: `Binding.__post_init__` refuses anything above BOUND: a binding is evidence about
    #: the day it was made, not about this request. Stored as the integer the `IntEnum`
    #: already is, because the ordering is the whole point of the type and a text column
    #: would make "at least this assurance" a lookup rather than a comparison.
    assurance: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(int(Assurance.BOUND)))
    )

    __table_args__ = (
        CheckConstraint(one_of("channel", Channel), name="channel"),
        CheckConstraint(
            f"identity_hash ~ '{IDENTITY_HASH_PATTERN}'",
            name="hash_shape",
        ),
        CheckConstraint(
            f"assurance BETWEEN {int(Assurance.UNVERIFIED)} AND {int(Assurance.BOUND)}",
            name="assurance_at_most_bound",
        ),
        # Unique among live rows only, not a plain unique constraint. The plain version was
        # written first and is wrong for a reason that only shows up after a year: a phone
        # number belonging to someone who left is reissued to a new hire, and with a total
        # unique constraint the new binding can never be made, because the retired one still
        # occupies the pair and nothing here holds the DELETE privilege to clear it.
        # Restricting uniqueness to live rows keeps the property `resolve` actually depends
        # on - at most one *current* binding per channel identity - without making
        # offboarding a one-way door.
        Index(
            "uq_principal_identity_channel_identity_hash_live",
            "channel",
            "identity_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "auth"},
    )


class SessionRow(TimestampMixin, Base):
    """`auth.session`. A live channel session, and the thing a disable has to reach (M1.2.3).

    There is no pydantic type this mirrors, which is worth saying plainly rather than
    leaving a reader to look for one. `brain.identity.roles.BreakGlassSession` is a
    different object: a time-boxed elevation with its own grants, its own chain and its own
    notification. This is the ordinary thing - somebody is signed in on a channel right now
    - and it exists because `disabled_at` on its own disables nobody who is already holding
    a token.

    **`ended_at` rather than `deleted_at`, and no `SoftDeleteMixin`.** Every other table
    here retires rows with `deleted_at` and hides them behind a row-level security policy.
    A session must not be hidden when it ends: "which sessions were open when she was
    disabled, and when did they stop" is exactly the question asked afterwards, and a
    policy that filtered them out would make the cascade unverifiable from the outside.
    Carrying both columns was considered and rejected for the reason `disabled_at` and
    `deleted_at` are two columns and not one: two names for one fact is how they come to
    disagree, and here they would genuinely be one fact.

    **`expires_at` is not the same as `ended_at`.** The first is when the session was
    always going to stop; the second is when it actually did. A session that runs its full
    length ends at its expiry and records `expired`; one the cascade closes ends early and
    records why. Collapsing them would lose the only evidence that the cascade ran.
    """

    __tablename__ = "session"

    #: Not a surrogate uuid: the identity provider mints the session id and the ledger
    #: refers to it as `session:<id>`, so it has to be the string that arrives.
    id: Mapped[str] = mapped_column(String(SESSION_ID_CHARS), primary_key=True)

    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        # RESTRICT for the same reason `principal_identity` uses it: the record that a
        # session existed must outlive the account, because that is what an offboarding
        # review reads.
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)

    #: How strongly the holder was authenticated when this session opened. Unlike a
    #: binding, a session may legitimately carry AUTHENTICATED or STRONG: those levels are
    #: *about* a live session, which is what this row is.
    assurance: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        CheckConstraint(one_of("channel", Channel), name="channel"),
        CheckConstraint(
            f"assurance BETWEEN {int(Assurance.UNVERIFIED)} AND {int(Assurance.STRONG)}",
            name="assurance_in_range",
        ),
        CheckConstraint("expires_at > started_at", name="expires_after_it_starts"),
        # Both or neither. An `ended_at` with no reason is a session that stopped for
        # reasons nobody recorded, which is the row the cascade audit needs most; a reason
        # with no time is a claim about an event with no moment.
        CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)",
            name="ended_with_a_reason",
        ),
        CheckConstraint(one_of("end_reason", SessionEndReason), name="end_reason"),
        # The cascade's working set, and the one query the gate runs on every request:
        # "is this principal's session still live". Partial, because the ended rows are
        # kept forever and are never the answer to that question.
        Index(
            "ix_session_principal_id_live",
            "principal_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        {"schema": "auth"},
    )


class DirectoryRoleGrantRow(TimestampMixin, Base):
    """`auth.directory_role_grant`. A role a directory group asserts, and only that (M1.1.5).

    The sync owns this table outright. It may insert into it and delete from it without
    asking any question about who wrote a row, because there is no other writer. The
    protection is the schema rather than a WHERE clause, and that is the entire difference
    between this design and the one it replaces. A `source` column in a shared table protects
    a hand-made grant only for as long as every delete statement anybody ever writes
    remembers to carry it, and the one that forgets is the one that runs during an incident.

    **The other table does not exist yet, and saying so is the point.** `role_grant` - the
    grants a person makes, which `brain.identity.roles.RoleGrant` describes - is M1.3.2 and is
    still unbuilt. This table is deliberately not it and must not become it: whoever builds
    `role_grant` builds a second table, granted SELECT, INSERT and UPDATE and no DELETE, with
    `deleted_at` for retirement like every other table here. Adding a `granted_by` and a
    `reason` column here instead, so one table could serve both, is the exact merge decision
    21 refused.

    **The primary key is the natural key, not a surrogate.** `(principal_id, role,
    source_group)` is the whole content of a row: "this group says this person has this
    role". A `uuid` id would let the same sentence be stored twice, and two rows saying one
    thing is not a duplicate-row problem here - it is a reconciliation that deletes one of
    them, reports the role removed, and leaves the person holding it. The database refuses
    the second row instead, so the sync's insert is idempotent by construction rather than
    by whichever ON CONFLICT clause was written.

    **There is no scope column, and its absence is load-bearing.** A `department_admin` or
    `approver` grant needs a scope, and the scope is a property of the *rule* that maps the
    group (`brain.identity.oidc.GroupRoleRule`), which is reviewed in this repository. Copied
    onto the row it becomes a second answer that nothing updates: reconciliation keys on the
    triple above, so a row whose group is still asserted is never rewritten, and a scope
    narrowed in the rule would go on being served wide from a row written months earlier.
    `brain.identity.directory.directory_role_grants` reads the scope from the rule every
    time, so the reviewed copy is the only one.

    **`last_seen_at` is the fourth column and it is not decoration.** A sync that stops
    running fails silently and in the dangerous direction: nothing is removed, so everyone
    keeps everything, and the symptom is an absence of change. The column makes "these rows
    are from a sync that last ran a fortnight ago" a query. `created_at` from
    `TimestampMixin` is the other half - when the directory first asserted it - and it is
    what `granted_at` on the resulting `RoleGrant` is built from.

    **No `deleted_at`, and no `SoftDeleteMixin`.** Retirement here would be a tombstone, and
    `brain.identity.packs.subtractive_state` refuses that shape across the identity package
    for the reason `revoke_role` deletes rather than flags: a row that subtracts turns "does
    she hold this role" into an evaluation-order question. What a deletion here costs is the
    record that the directory once asserted the role, and that record belongs in
    `obs.audit_entry`, which is append-only and cannot be edited by removal.
    """

    __tablename__ = "directory_role_grant"

    #: Who the directory says holds the role. `RESTRICT` for the reason
    #: `principal_identity` uses it: the sync deleting its own rows is ordinary, a principal
    #: disappearing underneath them is not, and the second must fail loudly.
    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    #: One of the six. Text plus a check constraint generated from the enum, as everywhere
    #: else here, so adding a seventh role fails a test rather than a 3am INSERT.
    role: Mapped[str] = mapped_column(String(ROLE_CHARS), primary_key=True)

    #: The group as the identity provider spells it, e.g. `/brain/approver/web`. Part of the
    #: key rather than a detail on the row: two groups may both confer `approver`, and if
    #: only one of them is still asserted the person keeps the role. Collapsing the two into
    #: one row would make leaving either group remove it.
    source_group: Mapped[str] = mapped_column(String(SOURCE_GROUP_CHARS), primary_key=True)

    #: When the sync last confirmed the directory still asserts this. See the class
    #: docstring: a sync that has stopped is invisible without it.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(one_of("role", Role), name="role"),
        CheckConstraint("length(btrim(source_group)) > 0", name="source_group_present"),
        # No secondary index. Every read here is "what does the directory currently say
        # about this person", and `principal_id` leads the primary key, so its own index
        # already answers that. A separate index on the same leading column was written
        # first and removed: it would be a second copy of the same b-tree, paid for on every
        # insert the sync makes, serving no query the key does not.
        {"schema": "auth"},
    )
