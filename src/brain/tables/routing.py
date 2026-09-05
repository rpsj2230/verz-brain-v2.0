"""The routing matrix, and one row per attempt so a trace can explain a fallback.

`brain.models.routing` is the policy: which tier a request lands in, which rungs a tier
offers, when the chain may move on, and when it must refuse. All of it is correct and none
of it is editable. `RoutingChain`'s own docstring says where it is meant to live: "In
Postgres this is `routing_rung`, editable from the console at runtime. Tier assignment
changes roughly monthly as providers ship models, and a change that needs an engineer and a
release is a change that stops happening, after which the pools rot."

**What breaks without this module.** The matrix is a function, so changing a timeout is a
deploy; and `seed_chain` is described in its own docstring as "not a runtime source of
truth", which today it nonetheless is, because there is nowhere else for one to be.

**The attempt table is the one that earns its place immediately.** Everything else here is
configuration that could live in a file for a while. `ops.model_attempt` cannot: a chain
that fell back leaves no evidence of having done so, so a trace *asserts* the fallback
rather than showing it, and `ChainOutcome.depth` - the number M5.4.8 alerts on - is
computed from something nobody can re-read afterwards. One row per try, joined back through
the rung to the tier, is what makes the executed chain reconstructable instead of narrated.

Three decisions worth reading before changing this file.

**`ops`, not `agent` or `gate`.** `brain.db.SCHEMAS` calls `ops` "scheduled jobs, budgets,
deployment records", and a routing matrix is a deployment record. Note the consequence:
`brain.ops.sweeps.sweep_rls` checks `auth`, `gate`, `obs`, `proj`, `know`, `agent`, `mem`
and `er`, and not `ops`, so nothing in CI would notice row-level security missing from these
three tables. It is enabled in `0003` and asserted in `tests/unit/test_tables.py`; the
sweep's schema list is the thing that should change.

**There is no `deployment` table, and `deployment_id` is therefore a plain column.** The
provider registry is M5.1 and has not been built. A foreign key to a table that does not
exist is not an option, and inventing the table here would mean two people designing it.
`provider` and `model` are carried on the rung beside the id, which `RoutingRung` already
does for `model` with a stated reason - it is the field the console edits, the field an
attempt row records, and half of the price-book key. What the domain type gets and this
table cannot is `RoutingRung.__post_init__`'s check that the rung names the model its
deployment actually serves; with no deployment row there is nothing to check against, and a
constraint that is right for a reason it cannot state is worse than none.

**The rung's `role` is a column and a trigger fills it.** M5.3.1 lists `role` among the
columns and M5.3.2 - which is not built here - says it is derived rather than typed.
`RungRole`'s docstring gives the reason: "a label a human types drifts from the position and
provider it is supposed to describe, and then the console shows a primary sitting third in
the chain." `0003` derives it on write, so the column is never a claim somebody made.

Task ids: M5.2.2, M5.3.1, M5.3.4
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.audit.ledger import TRACE_ID
from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.models.routing import FallbackTrigger, RungRole, Tier
from brain.tables.gate import SCOPE_SHAPE
from brain.tables.identity import one_of

#: `Deployment.id`, `provider` and `model` are free strings in the dataclass. Bounded here
#: at the widths a provider's own identifiers actually take, so a mistyped column cannot
#: hold a paragraph.
DEPLOYMENT_ID_CHARS = 120
PROVIDER_CHARS = 60
MODEL_CHARS = 120

#: `brain.audit.ledger.TRACE_ID` sizes this at 64 and `db.py` agrees. An attempt row is
#: joined to a trace, so the width has to match the thing it is joined to.
TRACE_ID_CHARS = 64

#: What an attempt ended as. `ok` plus every member of the closed fallback set, plus
#: `stopped` for the case `trigger_for` returns None - a 400, our request being wrong, where
#: the next rung would receive the same request and produce the same error at full cost.
#:
#: Built from `FallbackTrigger` rather than typed out, for the reason `one_of` exists: a
#: hand-written copy of an enum stops matching it the first time somebody adds a member, and
#: the failure is a row the database refuses in production after passing every test that
#: only exercised the Python side.
#:
#: There is deliberately no `weak_answer` member. `QUALITY_FALLBACK_REJECTED` explains why
#: at length; the short version is that a quality trigger has no falsifiable off condition,
#: so the retry loop it drives terminates on luck. A column that could record one would be
#: somewhere to put the number afterwards, which is how the argument gets reopened.
ATTEMPT_OUTCOMES: tuple[str, ...] = ("ok", "stopped", *sorted(t.value for t in FallbackTrigger))


class RoutingTierRow(TimestampMixin, SoftDeleteMixin, Base):
    """`ops.routing_tier`. A tier and the rule set that decides what lands in it (M5.2.2).

    **The rules are jsonb and the classifier is still a pure function.** That is not a
    contradiction and it is worth being exact about, because the obvious misreading is that
    a rule set in a table means routing is now data-driven and a document could influence
    it. `classify_tier` reads five scalars and makes no model call; what it consults from
    here is the *numbers* - the context window, the headroom - not a program. A rule set that
    could contain a predicate over the question text would reintroduce exactly the failure
    `RoutingRequest` was shaped to prevent, so the column holds an object and the classifier
    holds the logic.

    `context_window` is a column of its own rather than a key inside `rules`, because it is
    the one value with a hard invariant attached: `RoutingChain.narrowest_window` says a
    tier's window must be the narrowest of its rungs, never the widest, or a request sized
    to fit the primary overflows the fallback and the chain fails precisely when it is
    needed. A number under an invariant belongs where a constraint can reach it.

    `Tier.NONE` is a legitimate row. It is the fast lane's tier, it takes no model, and its
    window is zero; giving it a row rather than treating it as an absence is the same
    decision the enum makes, and for the same reason - a caller cannot forget to handle a
    value that is there.
    """

    __tablename__ = "routing_tier"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tier: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Tokens. Zero for `Tier.NONE`, which uses no model at all.
    context_window: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The rule set the console edits. An object, always: an array or a bare scalar here
    #: would deserialise into something the classifier reads as absent, and absent means
    #: "use the compiled default", which is a silent revert of whatever was configured.
    rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(one_of("tier", Tier), name="tier"),
        CheckConstraint("context_window >= 0", name="context_window_non_negative"),
        CheckConstraint("jsonb_typeof(rules) = 'object'", name="rules_object"),
        # A tier is a pool, and two live rows for one pool would make "what is main's
        # window" depend on which came back first.
        Index(
            "uq_routing_tier_tier_live",
            "tier",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "ops"},
    )


class RoutingRungRow(TimestampMixin, SoftDeleteMixin, Base):
    """`ops.routing_rung`. One position in one tier's chain (M5.3.1).

    Every column the leaf names: tier, scope, position, role, deployment, attempts, timeout,
    concurrency.

    **`scope` is `Scope.model_dump()`, the same shape the grant tables hold**, and it is
    checked by the same `SCOPE_SHAPE` predicate imported from `brain.tables.gate` rather
    than a second copy of it. What it means here is narrower than on a grant: it is the
    predicate a request's own scope must be compatible with for this rung to be eligible,
    which is how M5.5.1's residency constraint reaches the matrix. It is not a permission.

    **No foreign key to `ops.routing_tier`.** The tier is a closed vocabulary pinned by a
    check constraint against `Tier`, exactly as `kind` and `employment` are on
    `auth.principal`, so a rung naming a tier that does not exist is already impossible. A
    foreign key would additionally mean a tier row has to be created before any rung can be,
    and that a tier cannot be retired while a rung still names it - neither of which is a
    property anybody asked for.

    **One rung per (tier, position), among live rows.** `RoutingChain.__post_init__` refuses
    two rungs sharing a position and states the cost: the chain order would depend on
    insertion order, "so the executed chain stops being reconstructable from the attempt
    rows, which is the whole point of recording them." The constraint is the same rule where
    the rows actually live.
    """

    __tablename__ = "routing_rung"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tier: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    #: The predicate a request must satisfy for this rung to be eligible.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: Zero-based, ascending. Position 0 is the primary.
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: Derived on write by `ops.routing_rung_role()`, never typed. See the module docstring.
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    deployment_id: Mapped[str] = mapped_column(String(DEPLOYMENT_ID_CHARS), nullable=False)
    provider: Mapped[str] = mapped_column(String(PROVIDER_CHARS), nullable=False)
    model: Mapped[str] = mapped_column(String(MODEL_CHARS), nullable=False)

    #: Tries against this rung before moving on. One is the right answer wherever a person
    #: is waiting: a retry against the deployment that has just timed out spends the same
    #: wall clock for the same outcome, and the next rung is a better use of it.
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)

    #: A ceiling per rung, so a slow provider becomes queueing rather than unbounded memory.
    #: The first symptom of having no ceiling is memory, not latency.
    max_concurrency: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint(one_of("tier", Tier), name="tier"),
        CheckConstraint(one_of("role", RungRole), name="role"),
        CheckConstraint(SCOPE_SHAPE, name="scope_shape"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        # `RoutingRung.__post_init__`, one layer down. The way to remove a rung is to remove
        # it: a rung with zero attempts silently never runs and reads in the console as
        # configured.
        CheckConstraint("attempts >= 1", name="at_least_one_attempt"),
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        CheckConstraint("max_concurrency >= 1", name="concurrency_at_least_one"),
        CheckConstraint("length(btrim(deployment_id)) > 0", name="deployment_present"),
        Index(
            "uq_routing_rung_tier_position_live",
            "tier",
            "position",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_routing_rung_scope",
            "scope",
            postgresql_using="gin",
            postgresql_ops={"scope": "jsonb_path_ops"},
        ),
        {"schema": "ops"},
    )


class ModelAttemptRow(Base):
    """`ops.model_attempt`. One row per try, so the executed chain reconstructs from a join
    (M5.3.4).

    The join is `model_attempt -> routing_rung -> routing_tier`, ordered by `sequence`. That
    is the whole leaf: with it, a trace can *show* that the primary timed out and the second
    rung answered; without it, the trace asserts a fallback happened and nothing can
    contradict it.

    Carries neither `TimestampMixin` nor `SoftDeleteMixin`, and both omissions are
    deliberate. `created_at` beside `started_at` would be two readings of one clock for one
    event, which `obs.audit_entry` rejects for the same reason; `updated_at` would move when
    the outcome is written, which is not a fact anybody asks about. `deleted_at` would let an
    attempt be hidden, and an attempt that can be hidden makes the reconstructed chain a
    claim rather than a record - the chain would come back shorter with nothing to say so.

    It is not append-only in the `obs.audit_entry` sense, and that is a real difference
    rather than an oversight: a row is written when the attempt starts and updated once when
    it finishes, because an attempt that never finishes has to be visible as an attempt that
    never finished. Writing one row at the end instead would lose exactly the in-flight case,
    which is the one an operator is looking at during an incident.
    """

    __tablename__ = "model_attempt"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    #: The trace this attempt belongs to. Every row for one request shares it, which is what
    #: makes the join a chain rather than a list of unrelated tries.
    trace_id: Mapped[str] = mapped_column(String(TRACE_ID_CHARS), nullable=False, index=True)

    rung_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # RESTRICT, so retiring a rung cannot take the evidence of what it did with it. A
        # rung is retired by `deleted_at` anyway; this refuses the hard delete.
        ForeignKey("ops.routing_rung.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    #: Zero-based, within this trace. Not the rung's position: a rung with `attempts = 2`
    #: contributes two rows, and the depth M5.4.8 alerts on counts tries rather than rungs.
    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Null while the attempt is in flight. See the class docstring on why that state exists.
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)

    #: The provider's HTTP status, where there was one. Null for a connection error, a
    #: timeout, or a rung that was never called because its breaker was open.
    status_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(one_of("outcome", ATTEMPT_OUTCOMES), name="outcome"),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_it_started",
        ),
        # Both or neither. A finished attempt with no outcome is a row that cannot say what
        # happened, and an outcome with no finish time is a claim about an event with no
        # moment - the same rule `auth.session` applies to `ended_at` and `end_reason`.
        CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)",
            name="finished_with_an_outcome",
        ),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="status_code_is_a_status_code",
        ),
        # One row per try. Two rows claiming one position in a trace would make the
        # reconstructed chain depend on which came back first, which is the same failure
        # `uq_routing_rung_tier_position_live` refuses one level up.
        Index("uq_model_attempt_trace_id_sequence", "trace_id", "sequence", unique=True),
        CheckConstraint(f"trace_id ~ '{TRACE_ID}'", name="trace_id_shape"),
        {"schema": "ops"},
    )


#: Every table this module declares, in the order a migration must create them: a table
#: appears after everything it points at. `migrations/versions/0003_resolver_and_tables.py`
#: keeps the same order and its downgrade reverses it.
#:
#: This tuple exists here rather than in `brain.tables.__init__` because that file was
#: outside the remit of the change that added these tables. Note the consequence, because it
#: is real and not hypothetical: `brain.tables.__init__` does not import this module, so
#: importing `brain.tables` alone leaves these three tables off `Base.metadata`. Nothing is
#: broken today - the migrations are written by hand and `migrations/env.py` imports neither
#: package - but the import belongs in `__init__` alongside the other three, and
#: `TABLES_IN_DEPENDENCY_ORDER` should grow to cover every table again.
ROUTING_TABLES_IN_DEPENDENCY_ORDER: tuple[str, ...] = (
    "ops.routing_tier",
    "ops.routing_rung",
    "ops.model_attempt",
)
