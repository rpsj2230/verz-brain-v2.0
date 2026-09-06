"""The routing matrix over HTTP, so the console can read it and change four numbers of it.

`brain.models.routing.RoutingChain` says where this belongs: "In Postgres this is
`routing_rung`, editable from the console at runtime. Tier assignment changes roughly
monthly as providers ship models, and a change that needs an engineer and a release is a
change that stops happening, after which the pools rot." `brain.tables.routing` built the
table. Nothing could reach it, so the matrix was still a function in a module and every
timeout was still a deploy.

**The matrix is one yes or no, never a filtered list.** `ops.routing_rung`'s row-level
policy admits every live row to the application role and this route applies no per-caller
scope on top, so a caller either reads the whole live matrix or reads none of it. That is
the shape the disclosure rule wants here rather than an exception to it: with no row-level
filtering there is no difference between "these are the rungs" and "these are the rungs you
may see", so there is nothing for a count on the page to disclose by subtraction. It is also
why the refusal below is the collection's, not a row's. See
`THE_MATRIX_IS_NOT_FILTERED_PER_CALLER`.

**The capability is checked before the database is looked at, and the order is the
property.** A caller holding no grant is refused identically on an instance with a database
and on one without; only somebody who may read the matrix can find out that this process
has no pool attached. Checking wiring first would answer 500 to everybody on a wireless
instance and 404 to the unentitled elsewhere, which makes the deployment's state readable
by anybody who can reach the port. `test_a_caller_with_no_grant_cannot_tell_whether_this_
process_has_a_database` is the test, and moving the two lines past each other fails it.

**`role` is readable and not writable, because a label somebody types drifts.** `RungRole`
gives the reason: "a label a human types drifts from the position and provider it is
supposed to describe, and then the console shows a primary sitting third in the chain."
M5.3.2 puts the derivation in a trigger. `RungEdit` therefore has no `role` field and
forbids extra keys, so a console that sent one is refused rather than obeyed. See
`ROLE_IS_DERIVED_AND_NEVER_TYPED`, and read it with the note below about what is actually
in the database today.

**What is not built, stated rather than implied.** `migrations/versions/0003` creates the
column, the check constraint and the grants, and it creates no `ops.routing_rung_role()`
trigger; the comment on `RoutingRungRow.role` says one derives the value on write and no
such function exists in the migration. So today the column is exactly the claim the design
says it must not be, and the only thing standing between the estate and a hand-typed label
is that nothing has ever written to this table. This module does not fix that, because a
trigger is M5.3.2 and belongs in a migration with its own tests. What it does is refuse to
be the thing that makes the gap matter: no write path here accepts a role, so when the
trigger lands nothing has to be taken back.

**Four numbers are editable and eight columns are not.** `attempts`, `timeout_seconds`,
`max_concurrency` and `enabled` are the operational dials: they change with load and with a
provider's behaviour, they are what an incident is answered with, and none of them changes
what the chain is. The rest are refused and each for its own reason. `tier` and `position`
are the rung's place in the chain and are covered by `uq_routing_rung_tier_position_live`,
so moving a rung is a reordering of a whole tier rather than an edit of one row, and doing
it one PATCH at a time collides with the constraint halfway through. `scope` is a predicate
rather than a number and its editor is a scope editor. `deployment_id`, `provider` and
`model` name a deployment, and M5.1's provider registry does not exist: `RoutingRung.__post_
init__` checks that a rung names the model its deployment serves and there is nothing here
to check against, so accepting these three would let the console point a rung at a model
nobody holds a key for and meter it against the wrong price. `id` is the row.

Rejected: a PUT taking the whole row. It reads as the safer verb and is the more dangerous
one here, because a whole-row write has to carry `role`, `tier`, `position` and the three
deployment fields, and every one of them then arrives from a browser as a value the route
must either honour or silently drop. A body that cannot express those fields cannot
overwrite them.

Rejected: optional fields on the edit, so a caller may send one. A partial body means the
field the console left out is the field a reader assumed it sent, and the form on the screen
always holds all four. All four are required, so what is sent is what is displayed.

Rejected: publishing what the caller may do through `/me`. `CallerView`'s docstring refuses
a capability list there and gives the reason: it would be cached in a browser and used to
decide what to render, which is a permission model in the copy an attacker edits. `editable`
below is the narrow version of the same fact and is a different thing: one boolean about one
collection, recomputed on every request, naming no capability and nothing withheld. What it
must never become is a gate: the console renders or hides an editor with it and the PATCH is
refused by this module either way. See `AN_EDITABLE_FLAG_IS_PRESENTATION`.

Rejected: mounting these on `brain.api_routes`. That module's rules are about entities and
enumeration, and a matrix is neither: there is no name for a caller to guess and no
installation shape to map. The dependency is imported from there rather than re-declared, so
there is still one spelling of `asking` and a route here cannot acquire a subtly different
one.

**What has never run.** This repository has no PostgreSQL, so neither the SELECT nor the
UPDATE below has ever been executed. What is tested is the statement each one compiles to,
every refusal either can produce, and the order the checks happen in. The write's success
path is unverified and is the first thing to exercise against a real database.

Task ids: M5.3.3
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Numeric, Select, Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.api import API_PREFIX, COMMON_RESPONSES, Page
from brain.api_routes import Asked
from brain.core.entitlement import Capability
from brain.core.errors import Absent, Failed
from brain.tables.routing import RoutingRungRow

log = structlog.get_logger()


# ------------------------------------------------------------ written-down reasons

#: Why there is no per-row refusal here, and why a page of rungs may say it was cut short.
THE_MATRIX_IS_NOT_FILTERED_PER_CALLER: Final = (
    "Every live rung is visible to every caller who may read the matrix at all, because the "
    "row-level policy on ops.routing_rung admits them and this route adds no scope. So the "
    "collection is one decision rather than a filtered list, and the leak a count normally "
    "carries is not available: there is no difference between the rungs that exist and the "
    "rungs this caller was shown, so no arithmetic over the page discloses anything. That is "
    "a fact about this table and not a general licence; the console still renders no count, "
    "because the rule it keeps is about what a screen may do rather than about what one "
    "endpoint happens to make safe."
)

#: Why the role column is answered and never accepted.
ROLE_IS_DERIVED_AND_NEVER_TYPED: Final = (
    "A rung's role is what its position and its provider make it, so a label a person types "
    "is a second answer to a question the chain has already answered, and the two disagree "
    "the moment a rung moves. The console shows a primary sitting third and believes it. "
    "M5.3.2 derives the column in a trigger; until then nothing writes it, and this module "
    "makes sure that stays true by having nowhere to put a role that arrives."
)

#: Why a boolean about the caller is on the response, and what it must never be used for.
AN_EDITABLE_FLAG_IS_PRESENTATION: Final = (
    "editable says whether this caller holds the write capability, for one collection, on "
    "this request. It exists so a console can leave out a control nobody can use, which is "
    "presentation. It is not a permission: the PATCH checks the same capability again and "
    "refuses whatever the flag said, and a console that skipped the request because the flag "
    "was false would be enforcing a rule in the copy an attacker edits. The request a console "
    "makes must be identical either way."
)


# ----------------------------------------------------------------- the capabilities

#: Reading the matrix. Not the same as reading a record: what it discloses is which
#: providers this company holds keys for, in which regions, and in what order they are
#: tried, which is an operational fact about the estate rather than a business one.
MATRIX_READ: Final = Capability(value="read:routing_matrix")

#: Changing it. An `admin` verb, which `gate.admission.CHANNEL_VERBS` grants to CONSOLE and
#: withholds from API, so a client-credentials token cannot retune the estate. That is the
#: right ceiling for the same reason approval has it: a service account is a secret in a
#: configuration file, and a change attributable to a file is a change nobody made.
MATRIX_WRITE: Final = Capability(value="admin:routing_matrix")


# ------------------------------------------------------------------------ the bounds

#: The widest value a PostgreSQL `smallint` holds. `attempts`, `position` and
#: `max_concurrency` are all `SmallInteger`, and a bound declared here is the difference
#: between a 422 naming the field and a driver-level overflow arriving as a 500.
SMALLINT_MAX: Final = 2**15 - 1


def _numeric_ceiling(precision: int, scale: int) -> float:
    """The largest value a `NUMERIC(precision, scale)` column can hold.

    Derived rather than typed, and read off the column itself below, because the figure is
    a consequence of the column's declaration: writing 9999.99 here would be a fourth copy
    of `Numeric(6, 2)` and the one nobody updates when the column widens.
    """
    return float(10 ** (precision - scale)) - float(10**-scale)


def _timeout_ceiling() -> float:
    """The timeout bound, from `ops.routing_rung.timeout_seconds` itself.

    A `cast` is avoided by asking rather than asserting: a column that stopped being
    `Numeric` would make this return a bound nobody chose, so an unexpected type raises
    here, at import, rather than at the first request that exceeded a limit that was never
    applied.
    """
    column_type = RoutingRungRow.__table__.c.timeout_seconds.type
    if not isinstance(column_type, Numeric):
        msg = "ops.routing_rung.timeout_seconds is no longer NUMERIC; its bound is unknown"
        raise TypeError(msg)
    precision, scale = column_type.precision, column_type.scale
    if precision is None or scale is None:
        msg = "ops.routing_rung.timeout_seconds declares no precision; its bound is unknown"
        raise TypeError(msg)
    return _numeric_ceiling(precision, scale)


#: The largest timeout the column can hold, so an over-large one is refused with a message
#: naming the field rather than accepted and then rejected by the database.
MAX_TIMEOUT_SECONDS: Final = _timeout_ceiling()

#: `CheckConstraint("attempts >= 1", name="at_least_one_attempt")` on the table, and
#: `RoutingRung.__post_init__` one layer down. Both give the same reason: the way to remove
#: a rung is to remove it, and a rung with zero attempts silently never runs while reading
#: in the console as configured.
MIN_ATTEMPTS: Final = 1

#: `CheckConstraint("max_concurrency >= 1", name="concurrency_at_least_one")`. A ceiling of
#: zero is a rung that queues for ever, which is indistinguishable from a hung provider.
MIN_CONCURRENCY: Final = 1

#: How many rungs one page carries at most. A resource bound rather than a permission one:
#: the matrix is tiers times rungs per tier and both are operator-controlled, so this is far
#: above anything a real estate holds and exists so one request cannot ask for an unbounded
#: statement.
MAX_RUNGS_PER_PAGE: Final = 200

#: What a caller gets when they do not say. Above any plausible matrix, so `truncated` is
#: false in practice and the console's lack of a pager is not hiding anything.
DEFAULT_RUNGS_PER_PAGE: Final = 100


# ------------------------------------------------------------------------ the shapes


class RungView(BaseModel):
    """One rung of the matrix, as a console reads it.

    Every column `ops.routing_rung` carries except the two timestamps and `deleted_at`. The
    timestamps are omitted because they are the row's history rather than the chain's shape
    and nothing on the screen would be answered by them; `deleted_at` is omitted because the
    statement below only ever selects live rows, so a column that is always null would be a
    field inviting somebody to build a retired-rung view out of a page that cannot contain
    one.

    `role` is here and is not on `RungEdit`. See `ROLE_IS_DERIVED_AND_NEVER_TYPED`.

    `scope` is the predicate a request must satisfy for this rung to be eligible, in the
    same `Scope.model_dump()` shape the grant tables hold. It is passed through unchanged
    rather than summarised: a rendered sentence would be this module inventing a vocabulary
    for a structure that already has one, and M5.5.1's residency constraint reaches the
    matrix through exactly these clauses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tier: str
    position: int
    role: str
    scope: dict[str, Any]
    deployment_id: str
    provider: str
    model: str
    attempts: int
    timeout_seconds: float
    max_concurrency: int
    enabled: bool


class RungPage(Page[RungView]):
    """The matrix, as one page.

    `total` is inherited and never populated, for the reason every list endpoint here gives.
    It is inherited rather than removed because the leak it would carry elsewhere is not
    available on this collection and the rule is still the rule: see
    `THE_MATRIX_IS_NOT_FILTERED_PER_CALLER`.

    `next_cursor` is always null. `ops.routing_rung` has a stable order in
    (tier, position) and a keyset cursor over it is expressible, so this is a gap rather
    than an impossibility; what makes it a small one is that a matrix above
    `DEFAULT_RUNGS_PER_PAGE` rungs would mean twenty-five rungs in a tier.
    """

    #: There is more. Never how much more.
    truncated: bool = False
    #: Whether this caller may change what is on this page. Presentation only. See
    #: `AN_EDITABLE_FLAG_IS_PRESENTATION`.
    editable: bool = False


class RungEdit(BaseModel):
    """The four numbers a console may change about one rung.

    `extra="forbid"`, which is what turns "the console does not send a role" from a habit
    into an answer: a body carrying `role`, `tier`, `position` or a deployment field is
    refused with a 422 naming the key rather than accepted and quietly ignored. A model that
    ignored unknown keys would let a console believe it had moved a rung.

    Every field is required. See the module docstring on why a partial body is the shape
    that makes a form lie about what it sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts: Annotated[int, Field(ge=MIN_ATTEMPTS, le=SMALLINT_MAX)]
    timeout_seconds: Annotated[float, Field(gt=0, le=MAX_TIMEOUT_SECONDS)]
    max_concurrency: Annotated[int, Field(ge=MIN_CONCURRENCY, le=SMALLINT_MAX)]
    enabled: bool


def view_of(row: RoutingRungRow) -> RungView:
    """One row, copied field by field.

    Written out rather than built from `__dict__` or a column loop, for the reason
    `page_from` in `brain.api_routes` gives about its own: a column added to the table would
    otherwise arrive in a response because a copy loop was generous, and the first such
    column will be `deleted_at`.
    """
    return RungView(
        id=str(row.id),
        tier=row.tier,
        position=row.position,
        role=row.role,
        scope=row.scope,
        deployment_id=row.deployment_id,
        provider=row.provider,
        model=row.model,
        attempts=row.attempts,
        timeout_seconds=float(row.timeout_seconds),
        max_concurrency=row.max_concurrency,
        enabled=row.enabled,
    )


# ---------------------------------------------------------------------- the statements


def live_rungs(limit: int) -> Select[tuple[RoutingRungRow]]:
    """Every live rung, in chain order, at most `limit` of them.

    `deleted_at IS NULL` is written here as well as in the row-level policy, and that is
    belt and braces on purpose rather than duplication: the policy is what stops a retired
    rung reaching a caller, and this is what stops the ordering being decided by rows the
    policy happens to be filtering. A statement whose correctness depends on a policy being
    installed is a statement that is wrong on a database restored without one.

    Ordered by (tier, position) because that is the chain, and an unordered page of a chain
    is a list whose meaning depends on what the planner felt like. `position` ascending is
    the order attempts are made in; `tier` is alphabetical rather than by capability, which
    is a display order and not a claim, and the console does not reorder it.

    `limit + 1` is deliberately not what this asks for. Fetching one more than the page to
    discover whether there is another is the usual trick, and it would make `truncated` mean
    "there is at least one more", which is a count with a value of one. What the caller is
    told instead is that the page came back full, which is the same fact with no arithmetic
    in it.
    """
    return (
        select(RoutingRungRow)
        .where(RoutingRungRow.deleted_at.is_(None))
        .order_by(RoutingRungRow.tier, RoutingRungRow.position)
        .limit(limit)
    )


def apply_edit(rung_id: uuid.UUID, edit: RungEdit) -> Update:
    """The UPDATE for one rung, naming only the four columns the edit carries.

    `deleted_at IS NULL` in the WHERE clause as well as the id, so a retired rung cannot be
    edited back into service by anybody holding its id. Retiring is `deleted_at` and
    un-retiring is not an operation this route offers, because a rung that came back would
    take a position the live matrix may have given to something else and the unique index
    would refuse it at some later moment nobody could connect to this request.

    `returning` the row, so the response is what the database now holds rather than what the
    request asked for. Echoing the request back would report a successful write that a
    trigger had changed, which is exactly the case M5.3.2 introduces.
    """
    return (
        update(RoutingRungRow)
        .where(RoutingRungRow.id == rung_id, RoutingRungRow.deleted_at.is_(None))
        .values(
            attempts=edit.attempts,
            timeout_seconds=edit.timeout_seconds,
            max_concurrency=edit.max_concurrency,
            enabled=edit.enabled,
        )
        .returning(RoutingRungRow)
    )


# ------------------------------------------------------------------------- the wiring


def sessions_of(request: Request) -> async_sessionmaker[AsyncSession] | None:
    """The session factory this process was built with, or None.

    `getattr` and an `isinstance`, in the shape `brain.api_routes.wiring_of` uses and for
    its reason: a test may construct a bare application to exercise one route, and an
    `AttributeError` there would reach a caller as a 500 that reads like a bug in the gate
    rather than like an application built without a database.
    """
    found = getattr(request.app.state, "db_sessions", None)
    return found if isinstance(found, async_sessionmaker) else None


def _require_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """The factory, or a process-level fault.

    A `Failed` rather than an `Absent`, and the difference matters: an instance with no pool
    is broken rather than empty, and answering 404 would tell an operator the matrix is
    unconfigured when what is unconfigured is the database. Only a caller who already holds
    the read capability ever reaches this line.
    """
    factory = sessions_of(request)
    if factory is None:
        raise Failed("no database on this process")
    return factory


def _no_matrix_here() -> Absent:
    """The one refusal this router makes about the matrix.

    Named rather than raised inline in two places, so the two refusals are the same refusal.
    A reader without the capability and a writer without it get one answer, and the detail
    stays out of the body: `brain.app.handle_brain_error` sends `Absent.public_message`, and
    the string below reaches a log.
    """
    return Absent("the routing matrix is not answerable for this caller")


# -------------------------------------------------------------------------- the routes

router = APIRouter(prefix=API_PREFIX, tags=["routing"])


@router.get("/routing/rungs", response_model=RungPage, responses=COMMON_RESPONSES)
async def rungs(
    request: Request,
    asked: Asked,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNGS_PER_PAGE)] = DEFAULT_RUNGS_PER_PAGE,
) -> RungPage:
    """The live matrix, in chain order.

    The capability first and the database second. See the module docstring: the order is
    what stops an unentitled caller reading this deployment's state off the difference
    between a 404 and a 500.

    `truncated` is the page being full rather than a count of what is beyond it. It is
    always false on any matrix a person would recognise, and it is carried anyway because
    the alternative is a screen that silently shows the first hundred of something.
    """
    if not asked.reach.holds(MATRIX_READ, asked.now):
        log.info("routing matrix not answerable", principal=asked.caller.principal.id)
        raise _no_matrix_here()

    factory = _require_sessions(request)
    async with factory() as session:
        found = (await session.execute(live_rungs(limit))).scalars().all()

    return RungPage(
        items=[view_of(row) for row in found],
        next_cursor=None,
        truncated=len(found) >= limit,
        editable=asked.reach.holds(MATRIX_WRITE, asked.now),
    )


@router.patch("/routing/rungs/{rung_id}", response_model=RungView, responses=COMMON_RESPONSES)
async def edit_rung(
    request: Request,
    rung_id: uuid.UUID,
    edit: RungEdit,
    asked: Asked,
) -> RungView:
    """Change the four operational numbers on one rung.

    The write capability, not the read one, and the same refusal either way: a caller who
    may read the matrix and not change it gets the answer a caller who may not read it gets,
    so the reply says nothing about which half they are missing.

    A rung that is not there, or is retired, is the same refusal again. The id came from a
    page this caller was already shown, so an id that matches nothing means the matrix moved
    underneath them, and reporting that as a different outcome would be this route
    explaining a race it did not observe.

    The transaction is committed here rather than by a dependency, because there is one
    statement in it and nothing to compose it with. When a second write lands, the unit of
    work belongs one level up.
    """
    if not asked.reach.holds(MATRIX_WRITE, asked.now):
        log.info("routing matrix not editable", principal=asked.caller.principal.id)
        raise _no_matrix_here()

    factory = _require_sessions(request)
    async with factory() as session:
        row = (await session.execute(apply_edit(rung_id, edit))).scalar_one_or_none()
        if row is None:
            # Rolled back rather than committed, so a refused edit leaves no transaction
            # open on the pool. Nothing was written; the rollback is about the connection.
            await session.rollback()
            log.info("routing rung not editable", rung=str(rung_id))
            raise _no_matrix_here()
        await session.commit()
        return view_of(row)
