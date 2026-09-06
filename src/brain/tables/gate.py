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

Both shapes now live in this schema, which makes that paragraph load-bearing rather than
explanatory. `capability_grant.scope` and `capability_pack_assignment.scope` hold the first;
`scope.predicate` holds the second, and is named `predicate` rather than `scope` for exactly
that reason. `PREDICATE_SHAPE` refuses a `clauses` key the way `SCOPE_SHAPE` requires one,
so each column rejects the other's contents instead of accepting them and meaning something
else. A `{"clauses": [...]}` document read by `parse_predicate` is a scope whose only field
is called `clauses`, which matches no row; the reverse - a document form read as a
`Scope` - is a scope with no clauses, which is unrestricted. One failure is invisible and
the other is company-wide, so neither is left to a naming convention.

**Where the version and the epoch live, and why they are two tables.** `grants_version` is
per principal and is what makes `brain.gate.resolve.cache_key` change the instant somebody
is revoked; `policy_epoch` is global and is what invalidates everything at once. They are
separate because they answer different questions and are read on different paths: the
version is read on every single request for one principal, and a global counter read that
often would be the hottest row in the database.

**The registry is a vocabulary, never a holding.** `gate.capability_registry` says which
capability strings are meant to exist; the grant tables say who holds them. Keeping the two
apart is the whole reason the registry can be reviewed at all - a row that declared both
would be a grant nobody granted, made by whoever was tidying the vocabulary. So the registry
carries no principal, no scope and no expiry, and nothing below gives it one.

Task ids: M0.2.3, M1.4.1, M1.4.3, M1.4.5, M1.4.6, M1.4.7, M1.5.1, M2.1.1, M2.2.1, M4.2.1
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.department import SLUG_PATTERN
from brain.core.entitlement import CAPABILITY_RE, VERBS
from brain.core.envelope import TOOL_NAME_PATTERN as PYTHON_TOOL_NAME_PATTERN
from brain.core.field_policy import NAME_PATTERN, Classification
from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.tables.identity import PRINCIPAL_ID_CHARS, one_of

#: `Capability.value` is `Field(max_length=200)`.
CAPABILITY_CHARS = 200

#: `ToolDefinition.name` is `Field(max_length=80)`.
TOOL_NAME_CHARS = 80

#: `FieldRule.entity` and `FieldRule.field` are `Field(max_length=60)` and `max_length=120`.
ENTITY_CHARS = 60
FIELD_CHARS = 120

#: The grammar, taken from the compiled pattern rather than retyped. POSIX and Python agree
#: on every construct this pattern uses; there is no lookaround, no non-greedy quantifier
#: and no named group in it, which is the reason it can be shared at all.
CAPABILITY_PATTERN = CAPABILITY_RE.pattern

#: Python's named-group syntax, which PostgreSQL's regex engine does not have.
_NAMED_GROUP = re.compile(r"\(\?P<[a-z_]+>")


def _posix(pattern: str) -> str:
    """The same regex with Python's named groups turned into ordinary ones.

    `CAPABILITY_PATTERN` above can be shared verbatim because it uses nothing Python-only.
    `brain.core.envelope.TOOL_NAME_PATTERN` cannot: it names its three groups so that a
    caller splitting a tool name reads them off one match, and `(?P<source>` is a syntax
    error to PostgreSQL. Stripping the names mechanically keeps the constraint derived from
    the pattern rather than retyped beside it - a hand-written second copy of a grammar is a
    grammar with two versions, and the one that gets fixed is whichever the person was
    looking at.

    Only named groups are handled, and deliberately nothing else. A lookaround or a
    non-greedy quantifier appearing in one of these patterns would survive this function and
    be refused by PostgreSQL when the migration runs, which is a loud failure in CI rather
    than a constraint that quietly admits the wrong thing. `tests/unit/test_tables.py`
    checks the rendered pattern carries no Python-only construct at all, so the failure
    arrives before the migration does.
    """
    return _NAMED_GROUP.sub("(", pattern)


#: `brain.core.envelope.TOOL_NAME_PATTERN`, as something PostgreSQL can read.
#:
#: Imported rather than restated, and the module it comes from is named here because that is
#: the only thing keeping the two in step. There were three copies of this grammar and they
#: disagreed: the model field and `brain.ops.sweeps` both said `name.name`, which admits
#: `client.read`, while `brain.tools.registry.assert_tool_name` - the function that actually
#: refuses a tool - required `source.verb_noun`. A check constraint mirroring either of the
#: looser two would let this table record a name no tool could ever be registered under, so
#: it mirrors the strictest, which is now the only one.
TOOL_NAME_PATTERN = _posix(PYTHON_TOOL_NAME_PATTERN)

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

#: The mirror image, for `gate.scope.predicate`, which holds the *document* form that
#: `parse_predicate` reads. There is no positive shape to check - a document is an object
#: mapping arbitrary field names to matchers, so any object is structurally plausible - so
#: this refuses the one object that is definitely the wrong shape. `? 'clauses'` is the
#: jsonb key-existence operator; a document whose only key is `clauses` would parse into a
#: scope testing a field called `clauses`, which matches nothing, and dead configuration
#: reads from the far end of a query exactly like a permission bug.
PREDICATE_SHAPE = "jsonb_typeof(predicate) = 'object' AND NOT (predicate ? 'clauses')"

#: `Department.slug`, `Team.slug` and `ScopeRecord.slug` are all
#: `Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)`.
SLUG_CHARS = 60

#: Why the grammar is built here rather than interpolated at each constraint.
#:
#: **This shipped wrong in four constraints and nothing noticed for the life of the table.**
#: `CheckConstraint` parses its argument as `text()`, and `text()` reads `:name` as a bind
#: parameter. `SLUG_PATTERN` contains one colon, in `(?:`, so `f"slug ~ '{SLUG_PATTERN}'"`
#: compiled to `slug ~ '^[a-z][a-z0-9]*(?NULL[a-z0-9]+)*$'`: the non-capturing group became a
#: null bind, and what reached PostgreSQL was a different regular expression from the one
#: Python enforces. There is no warning at any point. The DDL simply says something else.
#:
#: **It is not a quieter grammar, it is a broken one.** Measured against PostgreSQL 18.6:
#: `(?N` is not a valid ARE construct, a CHECK is not evaluated when the table is created, so
#: `0003` applied cleanly and the first `INSERT` into `gate.scope`, `gate.department` or
#: `gate.team` failed with `ERROR: invalid regular expression: quantifier operand invalid`.
#: Those three tables could not take a row.
#:
#: `0003` copied the same unescaped text, so the model and the migration agreed with each
#: other and both disagreed with `SLUG_PATTERN`, which is why the model-versus-migration
#: comparison in `tests/unit/test_tables.py` passed: it was comparing two copies of one
#: mistake. `0015` corrects the deployed constraints and `test_the_slug_grammar_reaches_
#: postgresql_as_the_pattern_python_enforces` asserts on the **compiled** DDL, because
#: `text()` normalises the escape at construction and prints the marker back either way.
#:
#: Written once here rather than escaped at four call sites, so a fifth constraint cannot be
#: added in the broken form by somebody following the pattern of its neighbours.
_ESCAPED_COLON = "\\:"

#: `SLUG_PATTERN` in the form a check constraint can carry. See `_ESCAPED_COLON`.
SLUG_SQL_PATTERN = SLUG_PATTERN.replace(":", _ESCAPED_COLON)


def _slug_grammar(column: str = "slug") -> str:
    """The grammar clause for one column, escaped so it survives `text()`."""
    return f"{column} ~ '{SLUG_SQL_PATTERN}'"


#: `Department.name` and `Team.name` are `Field(max_length=120)`; `ScopeRecord.label` too.
LABEL_CHARS = 120

#: `Department.company_id` and `Team.company_id` are `Field(max_length=128)`. The same width
#: as a principal id, and for the same reason: it is an identifier from somewhere else.
COMPANY_ID_CHARS = 128


def _reason_present(column: str = "reason") -> str:
    """A grant with a blank reason is a grant nobody can review.

    Length only, not content. A minimum of eight characters was tried and dropped: it
    refuses "cover" and admits "aaaaaaaa", so it buys nothing and teaches people to pad.
    """
    return f"length(btrim({column})) > 0"


class CapabilityRegistryRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.capability_registry`. Every capability that is meant to exist (M0.2.3).

    The grammar and the validator have been in `brain.core.entitlement` since M0.2; what was
    missing is the third thing the leaf names. A grammar says `read:clientt.name` is
    well-formed. Nothing said whether anybody meant it to exist - so a typo in a grant and a
    permission somebody deliberately created were the same row, and an access review reading
    the grant table could not tell them apart. The registry is what makes the difference
    visible: a capability nothing declares is a capability nobody reviewed.

    **This table records that a capability exists, never that anybody holds it.** There is
    no principal, no scope, no `not_after` and no `granted_by`, and none of them is an
    oversight. A scope column here would be the widest reach the capability may be granted
    in, which reads as a safety rail and behaves as a grant: whoever tidied the vocabulary
    would have decided something about everybody's reach. Reach is decided in
    `capability_grant` and `capability_pack_assignment`, one row per person, with a reason
    attached.

    **There is deliberately no foreign key from `capability_grant.capability` to here**, and
    the reasons are worth stating because the missing key is the first thing a reader looks
    for.

    The mechanical one first: uniqueness on `capability` is partial - live rows only, for
    the reason `principal_identity` gives - and PostgreSQL cannot back a foreign key with a
    partial unique index. `gate.department.scope_slug` records the same trade. A total
    unique constraint would buy the key at the price of making a retired capability's name
    unusable forever.

    The second reason survives even if the first were fixed. A grant may name
    `read:client.*`, and `Capability.covers` expands that over field names the registry
    lists one at a time. A foreign key would then either refuse every wildcard grant or
    force the registry to carry a row for each wildcard anybody might write, which is a
    vocabulary of patterns rather than of capabilities. So "every granted capability is a
    registered one" is a query somebody runs, not a constraint the database holds, and this
    docstring says so rather than leaving a reader to assume the key is there.

    `required_by_tool` is nullable because not every capability is asked for by a tool.
    `brain.core.redaction.OPAQUE_CAPABILITY` is demanded by the redactor, and a
    `gate.field_policy` rule's `required_capability` gates a field rather than a call. What
    the column buys where it is set is the review question that has no other answer: which
    tool goes dark if this capability is retired.
    """

    __tablename__ = "capability_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    capability: Mapped[str] = mapped_column(String(CAPABILITY_CHARS), nullable=False)

    #: What holding this reaches, in words. Required for the reason a grant's `reason` is:
    #: an undescribed permission is one nobody can review, and the review is the product.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: The tool that asks for it, where one does. See the class docstring.
    required_by_tool: Mapped[str | None] = mapped_column(String(TOOL_NAME_CHARS), nullable=True)

    __table_args__ = (
        # The same predicate the grant table carries, from the same constant. This is the
        # copy that matters: a grant is written by a person against a vocabulary, and if the
        # vocabulary admits a shape the grant table refuses, the registry becomes a list of
        # capabilities that cannot be granted.
        CheckConstraint(
            f"capability ~ '{CAPABILITY_PATTERN}'",
            name="capability_grammar",
        ),
        CheckConstraint(
            one_of("split_part(capability, ':', 1)", VERBS),
            name="capability_verb",
        ),
        CheckConstraint(_reason_present("description"), name="described"),
        # Mirrors `brain.core.envelope.TOOL_NAME_PATTERN`, which is the grammar
        # `brain.tools.registry.assert_tool_name` refuses a tool on. See `TOOL_NAME_PATTERN`
        # above. Null is admitted because most capabilities are asked for by no tool.
        CheckConstraint(
            f"required_by_tool IS NULL OR required_by_tool ~ '{TOOL_NAME_PATTERN}'",
            name="tool_name_grammar",
        ),
        # One live row per capability. Two would make "what does this mean, and who owns it"
        # depend on which came back first, which is the failure the registry exists to end
        # rather than to reproduce. Partial, so a retired capability's name can be used
        # again - a department wound up and restarted is an ordinary event and so is a
        # capability renamed back.
        Index(
            "uq_capability_registry_capability_live",
            "capability",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "gate"},
    )


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

    This is a grant table, and `sweep_grant_isolation` does now see it: the sweep matches
    `src.relname LIKE '%grant%' OR src.relname LIKE '%pack%'`, and the second clause is what
    catches a table whose name says assignment. The note that used to sit here said the
    opposite and was true when it was written; it is left recorded rather than deleted
    because the gap it describes - a grant-bearing table the isolation sweep cannot see by
    name - is one a differently-named table would reopen.
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


class ScopeRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.scope`. Mirrors `brain.core.department.ScopeRecord` (M2.1.1).

    The column is `predicate` and not `scope`, which is the single most important thing in
    this class. See the module docstring: two json shapes exist in this schema, and a column
    called `scope` on a table called `scope` would be the obvious place for somebody to put
    the wrong one. `PREDICATE_SHAPE` refuses the shape the grant tables hold.

    `is_department` is a boolean rather than a separate `department_scope` table. A second
    table would let a department point at a scope that no longer carries the flag, which is
    the state `assign_department_admin` refuses at authoring time; a column cannot get out
    of step with itself.

    Two rules `ScopeRecord` enforces are deliberately absent here, because a check
    constraint cannot express either. `assert_conjunctive` walks the clause list and reports
    every violation at once; `is_unsatisfiable` compares clauses on the same field against
    each other. Both need iteration over a json array, which means a subquery, which a check
    constraint may not contain. They stay in the type, and the type is the only supported
    way to write one of these rows.
    """

    __tablename__ = "scope"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    slug: Mapped[str] = mapped_column(String(SLUG_CHARS), nullable=False)

    #: The document form that `parse_predicate` reads, never `Scope.model_dump()`.
    predicate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: True for the one scope that *is* a department, as opposed to a scope written inside
    #: one. Only a flagged record may back a department or bound its admin.
    is_department: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    label: Mapped[str] = mapped_column(
        String(LABEL_CHARS), nullable=False, server_default=text("''")
    )

    __table_args__ = (
        CheckConstraint(_slug_grammar(), name="slug_grammar"),
        CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        CheckConstraint(PREDICATE_SHAPE, name="predicate_shape"),
        # `ScopeRecord.model_post_init` refuses a department scope that restricts nothing,
        # because an unbounded department scope is the whole company wearing a department's
        # name. The empty object is the one unrestricted predicate a check constraint can
        # recognise without iterating, so it is the half that is enforced here.
        CheckConstraint(
            "NOT is_department OR predicate <> '{}'::jsonb",
            name="a_department_scope_restricts_something",
        ),
        # Live rows only, for the reason `principal_identity` gives: a total unique
        # constraint would mean a retired slug could never be used again, and a department
        # that was wound up and later restarted is an ordinary event.
        Index(
            "uq_scope_slug_live",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The reverse question again, as on the grant tables: which scopes mention this
        # department. An access review reads it, and so does the console.
        Index(
            "ix_scope_predicate",
            "predicate",
            postgresql_using="gin",
            postgresql_ops={"predicate": "jsonb_path_ops"},
        ),
        {"schema": "gate"},
    )


class DepartmentRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.department`. Mirrors `brain.core.department.Department` (M2.2.1).

    In `gate` rather than in a schema of its own, and not in `auth`. `department.py` is
    explicit that a department is not a container: it is a predicate over rows, and the
    record exists only to give that predicate a name, an owner and somewhere to hang an
    admin. A predicate belongs with the scopes.

    There is no parent department column, for the reason the type has no parent field:
    nesting makes the entitlement lookup recursive. Depth that is genuinely wanted is a
    longer `scope_path` and a prefix clause, or a team.

    **`scope_slug` is not a foreign key, and that is a trade-off rather than an oversight.**
    `gate.scope.slug` is unique only among live rows, and PostgreSQL cannot back a foreign
    key with a partial unique index. The alternative is a total unique constraint on the
    slug, which would make retiring a scope a one-way door on its name - the same failure
    the partial index on `principal_identity` exists to avoid. So the reference is a plain
    column, and the join carries `AND deleted_at IS NULL`, which every query against a
    soft-deleted table needs anyway.
    """

    __tablename__ = "department"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    company_id: Mapped[str] = mapped_column(String(COMPANY_ID_CHARS), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(SLUG_CHARS), nullable=False)
    name: Mapped[str] = mapped_column(String(LABEL_CHARS), nullable=False)

    #: The `gate.scope` row that defines this department. Never nullable: a department with
    #: no predicate is a label, and a label cannot decide who sees what.
    scope_slug: Mapped[str] = mapped_column(String(SLUG_CHARS), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(_slug_grammar(), name="slug_grammar"),
        CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        CheckConstraint(_slug_grammar("scope_slug"), name="scope_slug_grammar"),
        CheckConstraint("length(btrim(name)) > 0", name="name_present"),
        Index(
            "uq_department_company_id_slug_live",
            "company_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "gate"},
    )


class TeamRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.team`. Mirrors `brain.identity.teams.Team` (M1.5.1).

    **Within a department is a foreign key, not a convention.** `Team.department_slug` is
    non-optional in the type because a team floating outside a department has no scope to be
    narrower than. The column here points at `gate.department.id` rather than repeating the
    department's slug, so the containment is enforced by the database and a rename does not
    leave orphaned strings behind. `company_id` is likewise not repeated: the department
    already carries it, and a second copy is a second thing to keep in step.

    That does mean `Team.path` - `<department>.<team>`, the value a `scope_path` prefix
    clause matches against - is a join rather than a column. Denormalising it was considered
    and rejected: a stored path is a cached answer to a question the join answers exactly,
    and it goes stale on the first rename, silently, in the direction of reaching rows that
    belong to somebody else.

    `Team._check` refuses a team whose slug equals its department's. That one cannot be a
    check constraint - it needs the parent row, and a check constraint may not contain a
    subquery - so it stays in the type. It is worth saying which rule is where rather than
    letting a reader assume the database holds all of them.
    """

    __tablename__ = "team"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("gate.department.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(SLUG_CHARS), nullable=False)
    name: Mapped[str] = mapped_column(String(LABEL_CHARS), nullable=False)

    __table_args__ = (
        CheckConstraint(_slug_grammar(), name="slug_grammar"),
        CheckConstraint("length(slug) >= 2", name="slug_long_enough"),
        CheckConstraint("length(btrim(name)) > 0", name="name_present"),
        Index(
            "uq_team_department_id_slug_live",
            "department_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "gate"},
    )


class GrantsVersionRow(TimestampMixin, Base):
    """`gate.grants_version`. One counter per principal, moved only by triggers (M1.4.6).

    `brain.gate.resolve` builds its cache key as `ent:<principal>:<version>` and says why
    the version is in the key rather than checked after a read: a write bumps
    `grants_version`, and every key built from the old version is orphaned in the same
    instant. This is the column that bumps. Without it `VersionSource` has no
    implementation, so the cache either goes unused or serves a revoked permission for as
    long as the sixty-second TTL lasts (M1.4.5).

    **Nothing in the application writes this row.** `0003` puts the bump in an AFTER trigger
    on every grant-bearing table, because the code path that forgets is the one written in a
    hurry during an incident, and a revocation that does not bump is a revocation that does
    not take effect until the TTL expires. A trigger cannot be forgotten by a caller who
    does not know it is there.

    No `SoftDeleteMixin`, and the absence is the design: a version cannot be retired. A
    hidden row would send the reader to its default, so a bumped counter would silently
    return to zero - which is a cache key colliding with one minted before the revocation,
    the exact failure the counter exists to prevent.

    The row is created on the first bump rather than seeded beside the principal. A reader
    coalesces a missing row to zero, so a principal who has never held a grant needs no row
    at all, and `0003` therefore writes no data.
    """

    __tablename__ = "grants_version"

    principal_id: Mapped[str] = mapped_column(
        String(PRINCIPAL_ID_CHARS),
        ForeignKey("auth.principal.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    __table_args__ = (
        # Monotonic. A counter that can go backwards hands out a key that was already used
        # under a wider entitlement, and whatever is cached under it is still readable.
        CheckConstraint("version >= 0", name="version_non_negative"),
        {"schema": "gate"},
    )


class PolicyEpochRow(TimestampMixin, Base):
    """`gate.policy_epoch`. One global counter, moved by every entitlement mutation (M1.4.7).

    Separate from `grants_version` because the two invalidate different things.
    `grants_version` is per principal and answers whether *this person's* reach has changed.
    The epoch is global and answers whether the shape of the permission model has changed at
    all, which is what a cached *answer* has to be keyed on, because an answer drew on rows
    that other people's grants also govern.

    **A name collision worth stating plainly, because it is already in the code.**
    `brain.core.field_policy.FieldPolicy.epoch()` is also called a policy epoch and is a
    different object: a 32-character digest over the field-policy rule set, which is what
    `brain.gate.cache_key.CacheKeyParts.policy_epoch` receives - typed `int` there and `str`
    on `brain.core.redaction.RedactionTrace`, which is the same disagreement one layer up.
    This column is M1.4.7's counter and nothing else. Whoever wires the answer cache has to
    decide which of the two the key carries, or carry both; what must not happen is one
    quietly standing in for the other, because a digest that does not move when a grant
    changes leaves every cached answer reachable.

    One row, pinned by a check constraint rather than by convention. Two rows would make the
    epoch an aggregate, and an aggregate over a table anybody can insert into is a number
    that goes backwards when somebody removes the wrong row.
    """

    __tablename__ = "policy_epoch"

    #: Always 1. Not a boolean and not a partial unique index on a constant, because both
    #: read as though a second row were imaginable.
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("id = 1", name="exactly_one_row"),
        CheckConstraint("epoch >= 0", name="epoch_non_negative"),
        {"schema": "gate"},
    )
