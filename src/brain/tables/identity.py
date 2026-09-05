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

Task ids: M1.2.1, M1.2.2
"""

from __future__ import annotations

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

    #: The principal id itself, not a surrogate. It arrives from the identity provider
    #: (`c_0447`, a Keycloak subject) and is what every other table and the entire audit
    #: ledger already refers to a principal by. A surrogate key here would mean the ledger's
    #: `actor_id` pointed at nothing joinable, which is the one property it needs.
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
