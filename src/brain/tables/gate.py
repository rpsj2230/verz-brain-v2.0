"""Grants, packs of grants, and the rule saying which capability reaches which field.

`brain.core.entitlement` computes what a principal may do from an `EntitlementSet` handed
to it. This module is where that set comes from. Without it the gate is a function with no
inputs: correct about intersection, correct about expiry, and unable to say that anybody
holds anything.

Four things here are decisions rather than transcription.

**Grants never reference a connector.** `brain.ops.sweeps.sweep_grant_isolation` fails the
build on a foreign key from a grant table to a connector table, and the tables below hold
none: every foreign key runs to `auth.principal` or to `gate.capability_pack`. The reason
is stated in the sweep and is worth repeating, because it is not obvious in either
direction. If grants referenced connectors, removing a connector could cascade into
removing grants; worse, adding one would become a way to touch the permission graph. The
two graphs must stay unjoinable at the schema level rather than by anybody remembering.

**A principal may hold a capability once, not twice.** `EntitlementSet.scope_for`
intersects the scopes of every grant covering a capability, because holding something twice
must never be wider than holding it once. That is right, and it makes a second grant do
the opposite of what whoever wrote it intended: granting `read:client.name` in the sales
scope to somebody who already holds it in the web scope leaves them able to read nothing at
all. So the pair is unique among live rows, and an administrator gets a constraint
violation instead of a silent revocation.

**Revocation is deletion, never a negative row.** M1.4.2 and the additive-only rule in
`brain.core.entitlement` both say so. `deleted_at` is that deletion: the row-level security
policy hides retired grants from the application entirely, so nothing has to remember to
filter them, while the row survives for the audit question of who held what and when.

**Scope is `Scope.model_dump()`, not the console's document form.** These are two different
json shapes and the difference is not visible in a column type. `Scope.model_dump()` is
`{"clauses": [...]}`; `brain.core.scope_sql.parse_predicate` reads `{"department": "web"}`.
`brain.seed.py` writes the first. The check constraint below pins the column to the first,
so a document-form predicate written here is refused rather than parsed as an empty scope -
which is to say, rather than being read as unrestricted.

Task ids: M1.4.1, M1.4.3, M4.2.1
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.entitlement import CAPABILITY_RE, VERBS
from brain.core.field_policy import NAME_PATTERN, Classification
from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.tables.identity import PRINCIPAL_ID_CHARS, one_of

#: `Capability.value` is `Field(max_length=200)`.
CAPABILITY_CHARS = 200

#: `FieldRule.entity` and `FieldRule.field` are `Field(max_length=60)` and `max_length=120`.
ENTITY_CHARS = 60
FIELD_CHARS = 120

#: The grammar, taken from the compiled pattern rather than retyped. POSIX and Python agree
#: on every construct this pattern uses; there is no lookaround, no non-greedy quantifier
#: and no named group in it, which is the reason it can be shared at all.
CAPABILITY_PATTERN = CAPABILITY_RE.pattern

#: `NAME_PATTERN` from `brain.core.field_policy`, which is deliberately the field half of
#: the capability grammar: the names a policy can be written about are exactly the names a
#: grant can be written about.
NAME_SQL_PATTERN = NAME_PATTERN

#: `Scope.model_dump()` is an object with a `clauses` array and nothing else. Checking both
#: halves matters: `jsonb_typeof(scope) = 'object'` alone admits `{"department": "web"}`,
#: which is the console's document form and would deserialise to a scope with no clauses.
#: A scope with no clauses is unrestricted, so the failure mode of the weaker check is a
#: predicate that widens to the whole company.
SCOPE_SHAPE = "jsonb_typeof(scope) = 'object' AND jsonb_typeof(scope -> 'clauses') = 'array'"


def _reason_present(column: str = "reason") -> str:
    """A grant with a blank reason is a grant nobody can review.

    Length only, not content. A minimum of eight characters was tried and dropped: it
    refuses "cover" and admits "aaaaaaaa", so it buys nothing and teaches people to pad.
    """
    return f"length(btrim({column})) > 0"


class CapabilityGrantRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.capability_grant`. Mirrors `brain.core.entitlement.Grant` (M1.4.1).

    `granted_by` is a plain column and not a foreign key to `auth.principal`. Two reasons,
    and only the second is about the database: a grant can be made by something that is not
    a principal row at all (a provisioning job, an import), and the record of who granted it
    has to outlive the granter's own row, which a foreign key with any `ondelete` other than
    `SET NULL` would prevent and `SET NULL` would destroy.

    `brain.db.AuditMixin` was the obvious way to get `created_by` here and was rejected:
    `created_by` and `granted_by` would be the same fact under two names, and two names for
    one fact is how they come to disagree. The entitlement under which a grant was made is
    recorded where it belongs, in `obs.audit_entry`.
    """

    __tablename__ = "capability_grant"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    capability: Mapped[str] = mapped_column(String(CAPABILITY_CHARS), nullable=False)

    #: No server default, deliberately. An unrestricted default turns a forgotten field into
    #: a company-wide grant, and a "matches nothing" default turns it into a grant that
    #: silently does nothing. Neither failure announces itself, so the column insists.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    granted_by: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"capability ~ '{CAPABILITY_PATTERN}'",
            name="capability_grammar",
        ),
        # The grammar alone admits `delete:client`, which parses and means nothing. The verb
        # set is closed in `brain.core.entitlement` and is closed here for the same reason.
        CheckConstraint(
            one_of("split_part(capability, ':', 1)", VERBS),
            name="capability_verb",
        ),
        CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        CheckConstraint(_reason_present(), name="reason_present"),
        # See the module docstring: a second grant of the same capability narrows rather
        # than widens, so it is refused rather than accepted and quietly inverted.
        Index(
            "uq_capability_grant_principal_id_capability_live",
            "principal_id",
            "capability",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # `jsonb_path_ops` rather than the default operator class: it is roughly a third the
        # size and faster, and the only operator it drops support for is key-existence,
        # which no query against a stored predicate uses.
        #
        # Stated plainly because it would be easy to claim otherwise: this index does
        # nothing for `brain.core.scope_sql.compile_where`. That function compiles a scope
        # into a predicate over some *other* table's `row_data`; the scope itself is the
        # input, never the thing being searched. What the index is for is the reverse
        # question - "which grants mention this department" - which is how an access review
        # and a revocation sweep both work, and which is a sequential scan without it.
        Index(
            "ix_capability_grant_scope",
            "scope",
            postgresql_using="gin",
            postgresql_ops={"scope": "jsonb_path_ops"},
        ),
        {"schema": "gate"},
    )


class CapabilityPackRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.capability_pack`. A named bundle of capabilities (M1.4.3).

    The capabilities are an array rather than a `capability_pack_item` child table. The
    child table is the orthodox answer and it was rejected because nothing in the design
    ever wants a row per capability: a pack is read whole, assigned whole and revoked whole,
    and the one query that looks inside it ("which packs would grant this?") is
    `capabilities @> ARRAY[...]` against the GIN index below. A third table would buy a
    per-capability foreign key to nothing - capabilities are strings from a grammar, not
    rows - at the cost of a join on the entitlement resolver's hot path.

    The array's members are not regex-checked per element. A check constraint cannot contain
    a subquery, so the only way to do it is
    `array_to_string(capabilities, ' ') ~ '^cap( cap)*$'`, which works only because the
    grammar forbids spaces. That makes the constraint silently depend on a second fact about
    a different module, and a constraint that is right for a reason it does not state is
    worse than no constraint. The grammar is enforced by `Capability` on the way in.
    """

    __tablename__ = "capability_pack"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(CAPABILITY_CHARS)), nullable=False)

    __table_args__ = (
        CheckConstraint(f"name ~ '{NAME_SQL_PATTERN}'", name="name_grammar"),
        CheckConstraint(_reason_present("description"), name="described"),
        # An empty pack is assignable, reviewable, and grants nothing. It looks like access
        # having been given and is not, which is the worst shape a permission object can
        # take: the person who assigned it believes the job is done.
        CheckConstraint("cardinality(capabilities) > 0", name="not_empty"),
        Index(
            "uq_capability_pack_name_live",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_capability_pack_capabilities", "capabilities", postgresql_using="gin"),
        {"schema": "gate"},
    )


class CapabilityPackAssignmentRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.capability_pack_assignment`. A pack held by a principal, in a scope (M1.4.3).

    The scope lives here rather than on the pack, which is what "scope-bound assignment"
    means and is the whole reason packs are worth having: one `account_manager` pack,
    assigned to eleven people in eleven different scopes, instead of eleven near-identical
    sets of grants that drift apart.

    Note for whoever writes `brain.ops.sweeps`' next revision: this is a grant table, and
    `sweep_grant_isolation` will not see it. That sweep matches `src.relname LIKE '%grant%'`
    and this table is called an assignment, so a foreign key from here to a connector table
    would pass the build. There is none, and the sweep's pattern is the thing that should
    change - the name follows M1.4.3.
    """

    __tablename__ = "capability_pack_assignment"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    pack_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("gate.capability_pack.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        CheckConstraint(_reason_present(), name="reason_present"),
        # Same reasoning as the grant table: assigning one pack twice intersects the two
        # scopes and leaves the holder with less than they started with.
        Index(
            "uq_capability_pack_assignment_principal_id_pack_id_live",
            "principal_id",
            "pack_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_capability_pack_assignment_scope",
            "scope",
            postgresql_using="gin",
            postgresql_ops={"scope": "jsonb_path_ops"},
        ),
        {"schema": "gate"},
    )


class FieldPolicyRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.field_policy`. Mirrors `brain.core.field_policy.FieldRule` (M4.2.1).

    In `gate` rather than in a schema of its own, because a field policy is a statement
    about reach and it is read on the same path as a grant. It is not in `proj`: it governs
    what may be *returned*, while `brain.core.projection` governs what may be *stored*, and
    putting them in one place is how the two get confused.

    The default-deny rule (M4.2.2) is not expressible here and deliberately is not
    attempted. A field is withheld because no live row classifies it, which is the absence
    of a row rather than a property of one; `FieldPolicy.rule_for` returning None is where
    that is decided, and `compute_mask` is where it is enforced.
    """

    __tablename__ = "field_policy"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    entity: Mapped[str] = mapped_column(String(ENTITY_CHARS), nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(FIELD_CHARS), nullable=False)
    required_capability: Mapped[str] = mapped_column(String(CAPABILITY_CHARS), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(f"entity ~ '{NAME_SQL_PATTERN}'", name="entity_grammar"),
        CheckConstraint(f"field ~ '{NAME_SQL_PATTERN}'", name="field_grammar"),
        CheckConstraint(
            f"required_capability ~ '{CAPABILITY_PATTERN}'",
            name="capability_grammar",
        ),
        # `FieldRule._must_be_a_read`: a field policy gates returning a value, and returning
        # is reading. Without this a rule could be satisfied by `write:client.margin`, so
        # permission to change a number would confer permission to see it. The noun is left
        # free on purpose - a margin column on a client record may answer to a finance
        # capability - so only the verb is pinned.
        CheckConstraint(
            "split_part(required_capability, ':', 1) = 'read'",
            name="capability_is_a_read",
        ),
        CheckConstraint(
            one_of("classification", Classification),
            name="classification",
        ),
        # `PolicyConflictError` exists because two rules for one field make "may this person
        # see this field" an evaluation-order problem. This is that error, one layer down:
        # the conflicting row cannot be written, so a policy cannot be loaded into a state
        # the loader would have to refuse.
        Index(
            "uq_field_policy_entity_field_live",
            "entity",
            "field",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "gate"},
    )
