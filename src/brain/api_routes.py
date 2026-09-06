"""The first routes under the versioned prefix, and the gate on them.

Until this module the application served liveness, readiness and the build documentation,
and nothing else. There was no route under `API_PREFIX` at all, which is why
`brain.api.COMMON_RESPONSES` described itself as attached to every route while being
attached to none, why `brain.openapi.DOCUMENTED_BEFORE_IT_IS_ENFORCED` had to say the
document was ahead of the code, and why the console's typed client was generated from a
schema describing health checks.

**A route is a lens, never a principal.** Nothing here holds a credential, opens a
connection, or reads a record on its own account. What a request may see is the caller's own
reach, narrowed by the channel it arrived on and by how strongly they are signed in, and the
narrowing is `brain.gate.admission.admit` rather than anything written here. That function
existed, was tested, and was called by nothing, so the ceiling it computes was a description
of what would happen rather than a thing that happened. See
`THE_ROUTE_ADDS_NOTHING_TO_WHAT_THE_CALLER_HOLDS`.

**One dependency decides the order, so no route can get it wrong.** `asking` authenticates,
resolves and narrows, in that order, and hands a route a caller and exactly one entitlement.
A route that assembled those itself would be a route that could resolve before it
authenticated, or answer at the nominal reach because the narrowed one was one variable away.
There is only ever one `EntitlementSet` in scope in this module.

**Every refusal is the same refusal.** An entity nothing classifies, an entity with no tool
registered, an entity whose tool this caller reaches no column of, and a record that simply
is not there all answer 404 with `brain.api.ErrorBody` and the same sentence. That is
`brain.core.redaction`'s record-level rule applied one level up, and it is worth applying
there because an installation's shape is as enumerable by asking as a client list is: a
caller who could tell "there is no price list here" from "you may not read it" could map
what this company runs by trying names. See `AN_ENTITY_IS_AS_ENUMERABLE_AS_A_RECORD`.

**No count of anything withheld leaves here.** `brain.api.Page` carries an optional `total`
and this route never sets it, deliberately and not for want of a cheap count: a total behind
a permission predicate is the "showing 3 of 47" leak with a different label on it. What a
caller gets instead is `truncated`, which says there is more without saying how much more.
See `A_TOTAL_IS_A_HIDDEN_ITEM_COUNT`.

**The redactor is the only path to a response body.** The handler produces a `TypedResult`,
`serialise_for_channel` turns it into a `ChannelPayload`, and `RecordPage` is built from that
payload and from nothing else. There is no branch here that reads a record, a trace or a
redaction reason directly, which is the shape `brain.core.redaction` enforces on channel
adapters by giving them nowhere to put one.

**What this cannot do yet, stated rather than implied.** The application registers no row
tool, because `brain.tools.startup` argues at length that wiring one changes the deployed
connection profile and deserves its own measurement. So on the deployed instance every
entity answers 404, uniformly, for everybody. That is the correct answer for an install with
no data plane and it is indistinguishable from a refusal, which is the property that matters;
what it is not is useful. The route reads `app.state.tools`, so the day somebody passes
`records=` to `build_registry` this answers without a line changing here.

Rejected: a `POST /ask` taking a question in words. There is no model wired into this
process, so such a route would either refuse everything or return whatever a keyword match
happened to find while presenting itself as an answer. A route that reads records is a
smaller claim and it is the one screen 3 is built on.

Rejected: projecting a catalogue through `brain.gate.invoke` for a request with no agent in
it. `invoke` assembles an agent run: it wants a ceiling, a leash and an injection
assessment, and a records read has none of the three. Inventing them so the call could be
made would put three fabricated values into the one place the platform's reach is decided.
The agent term of the invariant enters at `gate.invoke` and `gate.leash.decide`, and it is
deliberately absent here rather than faked.

Task ids: M31.1.4.1, M31.1.4.3, M31.1.4.4
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from brain.api import API_PREFIX, COMMON_RESPONSES, Page
from brain.core.entitlement import EntitlementSet
from brain.core.errors import Absent, BrainError, Failed
from brain.core.redaction import (
    ChannelPayload,
    LockedField,
    require_typed_result,
    serialise_for_channel,
)
from brain.gate.admission import admit
from brain.gate.context import Channel
from brain.gate.resolve import EntitlementCache, EntitlementStore, VersionSource, resolve
from brain.identity.bearer import Caller, TokenAuthority, authenticate
from brain.identity.oidc import VerifiedClaims
from brain.knowledge.rows import DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, RowRequest, row_scope_for
from brain.tools.registry import ToolRegistry
from brain.tools.startup import classification_for

log = structlog.get_logger()


# ------------------------------------------------------------ written-down reasons

#: Why the route computes reach rather than being handed it.
THE_ROUTE_ADDS_NOTHING_TO_WHAT_THE_CALLER_HOLDS: Final = (
    "A route that could see more than its caller defeats the whole system, and the way that "
    "happens is never a deliberate bypass: it is a handler reading a record to decide "
    "whether to show it, or a service credential used because the caller's own reach was "
    "inconvenient. So the entitlement passed to the reader and the entitlement passed to the "
    "redactor are the same object, computed once from the caller's grants and narrowed by "
    "gate.admission, and there is no other entitlement in scope for it to be confused with."
)

#: Why an unknown entity answers exactly as a forbidden one does.
AN_ENTITY_IS_AS_ENUMERABLE_AS_A_RECORD: Final = (
    "Whether this company runs a price list, an HR table or a finance ledger is a fact about "
    "the company, and a caller who can tell 'nothing here is called that' from 'you may not "
    "read it' learns it by trying names. So an unclassified entity, an unregistered one, an "
    "ambiguous one and one whose rows this caller reaches none of are one answer with one "
    "body, and the difference between them is a log line."
)

#: Why `total` is never populated on this route.
A_TOTAL_IS_A_HIDDEN_ITEM_COUNT: Final = (
    "A count computed behind a permission predicate tells the reader how many rows the "
    "predicate removed, because they can see the ones it kept. 'Showing 3 of 47' is 44 facts "
    "they did not have, and it is the same leak whether the number is called total, count or "
    "matches. `truncated` says there is more and says nothing about how much more, which is "
    "the whole of what a person paging through a list actually needs."
)


# ------------------------------------------------------------------- the wiring


@dataclass(frozen=True)
class GateWiring:
    """Everything a request under this prefix needs that the process holds once.

    One object rather than four attributes on `app.state`, because the four are useless
    apart: a token authority with no entitlement store authenticates people and can tell them
    nothing, and a store with no authority is a reach nobody can be identified for. Bundling
    them means "is this application wired" is one question with one answer, and a half-wired
    process is unrepresentable rather than a combination somebody has to reason about.

    Absent on a deployed instance today. `brain.identity.bearer` argues why the absence
    refuses rather than waving requests through, and `brain.app` records what is missing.
    """

    authority: TokenAuthority
    versions: VersionSource
    store: EntitlementStore
    cache: EntitlementCache


def wiring_of(request: Request) -> GateWiring | None:
    """The wiring this process was built with, or None.

    `getattr` rather than attribute access, because a test may construct a bare `FastAPI` to
    exercise one route and an `AttributeError` there would surface as a 500 that reads like a
    bug in the gate rather than like an application built without one.
    """
    found = getattr(request.app.state, "gate", None)
    return found if isinstance(found, GateWiring) else None


def channel_for(claims: VerifiedClaims) -> Channel:
    """Which channel ceiling this token is held to.

    A token carrying Keycloak's `sid` came from an interactive browser session, which is what
    the console is. One with no `sid` belongs to no session, which is what a service-account
    token looks like, and `gate.admission.CHANNEL_VERBS` gives `API` neither `approve` nor
    `admin` for exactly that reason: a client-credentials grant is a secret in a
    configuration file, and an approval from one is an approval attributable to a file.

    Read from the claims rather than from a request header, because a header naming the
    channel is a header a caller can set, and the caller would then be choosing their own
    ceiling.
    """
    return Channel.CONSOLE if claims.session_id else Channel.API


@dataclass(frozen=True)
class Asking:
    """One request's caller, and the single reach it will be answered at.

    `reach` is `E_admitted = E(caller) ∩ channel_ceiling ∩ assurance_ceiling` (M3.3.3,
    M3.3.4), and it is the only `EntitlementSet` a route ever sees. The nominal set the store
    returned is deliberately not carried alongside it: two entitlements in scope, one wider
    than the other, is one autocomplete away from an answer computed at the wrong one.
    """

    caller: Caller
    reach: EntitlementSet
    channel: Channel
    now: datetime


def asking(request: Request) -> Asking:
    """Authenticate, resolve, narrow. One dependency, in that order, for every route.

    Named as a dependency at each route rather than installed as middleware. Middleware reads
    better and fails open in the case that matters: a route mounted on a path the middleware
    did not match is a route with no authentication, and nothing about it looks different in
    review. A missing dependency is visible in the diff that adds the route, and
    `test_every_route_under_the_prefix_authenticates_its_caller` asserts over the mounted set
    rather than over anybody's habit.

    Both ceilings come from `gate.admission.admit`, which builds them out of the caller's own
    grants and is therefore structurally incapable of adding anything. Nothing here composes
    an entitlement by hand; see `THE_ROUTE_ADDS_NOTHING_TO_WHAT_THE_CALLER_HOLDS`.

    The agent term of the platform invariant is absent because there is no agent in this
    path, and it is absent rather than supplied as an identity ceiling. An identity ceiling
    would be a real `EntitlementSet` in scope narrowing nothing, sitting exactly where a real
    one belongs, and the next person to add an agent here would have somewhere plausible not
    to put it.
    """
    now = datetime.now(UTC)
    wiring = wiring_of(request)
    caller = authenticate(
        wiring.authority if wiring is not None else None,
        request.headers.get("authorization"),
        now=now,
    )
    if wiring is None:
        # Unreachable: `authenticate` refuses a request on a process with no authority, and
        # an authority only exists inside a `GateWiring`. Written as a refusal rather than an
        # assertion because the one thing this function must never do is fall through to a
        # caller with no reach computed, which would then be an empty set that looks resolved.
        raise Failed("no gate wiring on this process")

    resolved = resolve(
        caller.principal_id,
        versions=wiring.versions,
        store=wiring.store,
        cache=wiring.cache,
        now=now,
    )
    channel = channel_for(caller.claims)
    return Asking(
        caller=caller,
        reach=admit(resolved.entitlements, channel, caller.assurance),
        channel=channel,
        now=now,
    )


#: The dependency every route under this prefix takes. Spelled once so a new route cannot
#: acquire a subtly different one by copying an older signature.
Asked = Annotated[Asking, Depends(asking)]


# ------------------------------------------------------------------ the shapes


class CallerView(BaseModel):
    """Who the API thinks is asking. The caller's own facts and nobody else's.

    Deliberately carries no list of capabilities. It would be the caller's own list and
    therefore safe in the narrow sense, and it would also be the first thing cached in a
    browser and used to decide what to render, which is a permission model in the copy an
    attacker edits. What the console may show is decided by what the API answers, one request
    at a time.

    `assurance` is here because it is the one fact a person can act on: "sign in again with
    your second factor" is a thing they can do, and it is the difference between holding the
    approve verb and being able to exercise it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str
    display_name: str
    primary_department: str | None = None
    employment: str
    assurance: str
    channel: str
    #: Order-independent digest of the reach this request was computed at. Says nothing about
    #: what the reach contains, and is what a support conversation quotes beside a trace id
    #: when two people disagree about what they saw.
    ent_hash: str


class RecordPage(Page[dict[str, Any]]):
    """One page of records, already through the redactor.

    Built from a `ChannelPayload` and from nothing else, which is what makes "the serialiser
    is the only path to a channel" a shape here rather than a convention: there is no field
    on this model that a trace, a redaction reason or a dropped record could be assigned to.

    `next_cursor` is always null today and that is a fact about the row plane rather than
    about this route. `brain.core.scope.Op` has EQ, IN, PREFIX and ANY and no ordered
    comparison, so a keyset position cannot be expressed as a filter the query compiler will
    accept, and an offset would re-read and re-filter under a permission predicate, which is
    the failure `brain.api.Page` was written to avoid. `truncated` carries the only fact a
    caller needs in the meantime.

    `total` is inherited and never populated. See `A_TOTAL_IS_A_HIDDEN_ITEM_COUNT`.
    """

    #: Fields present on the record and withheld from this caller. The lock is the product,
    #: not an apology: screen 3 shows a record with its restricted columns marked, and the
    #: person holding the capability sees the figure in the same place.
    locked: tuple[LockedField, ...] = ()
    #: Suppressed when nothing survived, by the payload rather than by this route. A page
    #: that named the source it found nothing in would answer a question nobody may ask.
    source: str = ""
    fetched_at: str = ""
    #: There is more. Never how much more.
    truncated: bool = False


def page_from(payload: ChannelPayload) -> RecordPage:
    """The response, copied field by field off the payload.

    Written out rather than spread from a dump, so that a field added to `ChannelPayload`
    does not arrive in a response body because a copy loop was generous. Every field here is
    one somebody chose to publish.
    """
    return RecordPage(
        items=list(payload.records),
        next_cursor=None,
        locked=payload.locked,
        source=payload.source,
        fetched_at=payload.fetched_at,
        truncated=payload.truncated,
    )


# ------------------------------------------------------------------ the routes

router = APIRouter(prefix=API_PREFIX, tags=["gate"])


@router.get("/me", response_model=CallerView, responses=COMMON_RESPONSES)
async def me(asked: Asked) -> CallerView:
    """Who this token belongs to, and what it can be exercised at.

    The one route that answers something useful before a data plane exists, and the one that
    proves the whole authentication path end to end: a signature checked against a published
    key, an issuer and audience compared exactly, an expiry honoured, a subject mapped to a
    principal this company wrote down, and a reach narrowed by the channel and the sign-in.
    """
    return CallerView(
        principal_id=asked.caller.principal.id,
        display_name=asked.caller.principal.display_name,
        primary_department=asked.caller.principal.primary_department,
        employment=str(asked.caller.principal.employment),
        assurance=asked.caller.assurance.name.lower(),
        channel=str(asked.channel),
        ent_hash=asked.reach.ent_hash(),
    )


@router.get("/records/{entity}", response_model=RecordPage, responses=COMMON_RESPONSES)
async def records(
    request: Request,
    entity: str,
    asked: Asked,
    limit: Annotated[int, Query(ge=1, le=MAX_ROW_LIMIT)] = DEFAULT_ROW_LIMIT,
) -> RecordPage:
    """Rows of one entity, at this caller's reach, redacted.

    The reader is handed the reach, which is what puts the scope predicate inside the query
    rather than around the result. The redactor is handed the same object again, which is
    what catches anything the reader let through. Two enforcement points and one entitlement.

    A caller who holds no grant over this entity is refused before either of them, with the
    answer an unknown entity gets. An empty page would be the friendlier response and it is
    the leak: it says the entity exists here, and a caller comparing an empty page against a
    404 maps the installation by trying names.
    """
    registry = getattr(request.app.state, "tools", None)
    if not isinstance(registry, ToolRegistry):
        # A process-level fault, identical for every caller and every entity, so it discloses
        # nothing about what exists. `brain.app.lifespan` builds one before it yields.
        raise Failed("no tool registry on this process")

    classification = classification_for(entity)
    matching = [d for d in registry.definitions() if d.entity == entity]
    # `row_scope_for` and never a check written here. It is the same function `read_rows`
    # consults, so "does this caller reach rows of this kind" has one answer; the difference
    # is only that a route has to turn None into a status while a reader turns it into FALSE.
    reaches = row_scope_for(entity, asked.reach, asked.now) is not None

    if classification is None or len(matching) != 1 or not reaches:
        # One refusal for four causes, on purpose. Answering an unreachable entity with an
        # empty page instead would be the leak: an empty list means "there is nothing here
        # for you to see", and a caller comparing an empty page against a 404 learns which
        # entities this installation carries by trying names. It is the same argument
        # `gate.catalogue` makes about tools, where a tool the caller cannot reach is absent
        # from the list rather than described and then refused.
        #
        # More than one match is a misconfigured install rather than a permission question,
        # and it is logged as one; answering it differently would tell a caller that two
        # systems here carry this entity.
        log.info(
            "entity not answerable",
            entity=entity,
            classified=classification is not None,
            tools=len(matching),
            reaches=reaches,
        )
        raise Absent(f"{entity!r} is not answerable for this caller")

    handler = registry.get(matching[0].name).handler
    try:
        # In a thread because the row plane is synchronous and this process is not.
        # `brain.tools.startup` sets out why that mismatch has not been resolved and what each
        # resolution costs; running the reader off the event loop is the part that is this
        # module's business.
        raw = await asyncio.to_thread(
            handler, RowRequest(limit=limit), entitlement=asked.reach, now=asked.now
        )
        # The boundary check rather than a cast. A handler that returned a dictionary is
        # refused here, where it is a contract violation by a tool, rather than being walked.
        result = require_typed_result(raw)
    except BrainError:
        # Already in the taxonomy, already has a public message, already maps to a status.
        # Re-wrapping would turn a refusal into a server fault.
        raise
    except Exception as exc:
        # Broad for the reason `gate.resolve` gives about its own: whatever a driver raises
        # would otherwise reach the response as FastAPI's default body, which is not
        # `ErrorBody`, or as a message with a connection string in it.
        raise Failed(f"reading {entity}: {type(exc).__name__}") from exc

    return page_from(
        serialise_for_channel(
            result, entitlement=asked.reach, policy=classification.policy(), now=asked.now
        )
    )
