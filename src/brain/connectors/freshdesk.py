"""Freshdesk: the source whose search will not tell you what it did not return.

Every other connector in this package is a variation on "fetch the records and hand them
back". This one is written first among the vendor connectors because it is the one where
doing that produces a wrong answer that looks right, and keeps looking right in every test
anybody writes.

**Freshdesk's search returns at most 300 records, ever.** Not per page, not per request: a
hard ceiling on the result set, recorded in `brain.ops.limits.FRESHDESK_SEARCH_MAX_RECORDS`
and verified in `tests/fixtures/cassettes.py`. The 300th record and the last record are
reported identically, so "every open P1 older than five days" comes back at 300, reads as
complete, and is silently wrong for any client with more. Nothing in the response says so.
So the walk here stops at the ceiling deliberately and marks the result truncated, and
`SearchReading` carries the completeness verdict beside the records rather than behind a
method somebody may not call. See `A_CEILING_REACHED_IS_AN_ANSWER_THAT_MUST_SAY_SO`.

**The paging trap is a clamped page size, not an off-by-one.** Freshdesk's search pages by
number at a fixed thirty per page and ignores `per_page` entirely; the list endpoints accept
`per_page` up to a hundred and silently clamp anything larger. A connector that asks for 500
gets 100 rows, compares 100 against the 500 it asked for, concludes it has seen the last page
and stops. That is the whole of "we only ever saw the first 100 clients", and it is why
`PageRequest` refuses a page size the endpoint would not honour rather than sending it and
hoping. See `A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE`.

**There is nothing in a page body that says whether there is another one.** No `has_more`,
no cursor, no total that can be trusted (the search's `total` is the ceiling once it is
reached). The only end-of-data signal available is a page shorter than the one that was
asked for, which is why the size a request declares is load-bearing rather than decorative.

**An unreachable Freshdesk is reported as unreachable and never answered from memory.** A
429 with `Retry-After`, a 502 and an expired key are three different operational problems and
one user-facing sentence, which is `brain.core.errors.Degraded`'s. What must never happen is
the fourth thing: a previous answer served as though it were this one. There is deliberately
no parameter anywhere in this module that could carry one, and
`assert_no_substitute_source` refuses a fetch that grows one, structurally, in the shape
`contract.assert_fetches_only` uses. See `AN_UNREACHABLE_SOURCE_HAS_NOTHING_TO_SUBSTITUTE`.

**Absent, refused and unreachable stay three answers.** An empty result set is a value with
no records in it; a refused credential and an unreachable source are raised. Collapsing any
of the three into "no tickets" is the failure `tests/invariants/test_cassettes.py` asserts
the recordings keep distinguishable, and it is the one a naive connector commits by
returning an empty list whatever happened. See `ABSENT_REFUSED_AND_UNREACHABLE_ARE_THREE`.

**A ticket projects a requester id and never a requester address.** The requester's email is
the field somebody reaches for first, and it is on the permanent denylist in
`brain.core.projection`, so the projection keeps the opaque id that joins to the entity
registry and the address is fetched live or not at all. The contact entity goes further and
projects nothing at all, because the two fields anybody wants from a contact are the two that
may never be stored. See `A_REQUESTER_IS_A_JOIN_KEY_AND_NEVER_AN_ADDRESS`.

Rejected, and worth stating because each looks tidier:

*A hand-written HTTP client for the four endpoints.* `brain.connectors.rest` already parses
an operation, builds and checks the address, and projects a body through a declared field
mapping, and it refuses a mapping that names no id. Writing a second one here would put the
redirect rule, the address check and the "fresh dictionary rather than a copied row" rule in
two places, and the copy in the vendor file is the one nobody re-reads.

*Restating the 300 as a page count.* `SEARCH_MAX_PAGES` is derived from the recorded ceiling
rather than declared beside it, so there is one number to keep true. A second copy would let
somebody "speed the walk up" by raising the page size and quietly change what the connector
believes the ceiling to be.

*Paging the whole entity here.* `brain.connectors.backfill` owns the long walk, because that
one has to yield to interactive traffic and survive a restart. This module walks one
question's search, bounded by the vendor's own ceiling and by the caller's limit, and holds
no allowance of its own.

Scope: domain logic. Nothing here opens a socket, resolves a name or reads a clock. The page
reader, the fetched-at stamp and every interval are parameters, for the reason
`brain.models.routing.CircuitBreaker` gives about `now`.

Task ids: M11.6.2
"""

from __future__ import annotations

import enum
import inspect
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol

from brain.connectors.change_signal import ChangeSubscription, DeletionCheck
from brain.connectors.contract import (
    ConnectorContractError,
    ConnectorScope,
    CredentialBinding,
    FetchRequest,
    TransportKind,
    assert_fetches_only,
)
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.projection import ProjectedValue
from brain.connectors.rest import OperationSpec, ParameterSpec, RestOperation
from brain.connectors.throttle import CallOutcome, classify
from brain.connectors.transports import FieldMapping, RestTransport, SourceRecord, normalise
from brain.core.envelope import IdentityMode, TypedResult
from brain.core.errors import Degraded
from brain.core.projection import MAX_LABEL_CHARS
from brain.core.scope import Scope
from brain.gate.provenance import FRESHNESS_TEXT, Freshness
from brain.ops.limits import (
    FRESHDESK_SEARCH_MAX_RECORDS,
    MAX_BACKOFF_SECONDS,
    SearchCompleteness,
    search_completeness,
)

# ------------------------------------------------------------------ written-down reasons
#: Why a page size is refused rather than sent and clamped.
A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE = (
    "Freshdesk clamps a page size above its own maximum instead of refusing it, and the "
    "search endpoint ignores the parameter altogether. So a connector asking for 500 gets "
    "100, compares 100 against the 500 it asked for, and concludes it has seen the last "
    "page. The result is the first page presented as the whole answer, with no error "
    "anywhere and no way to tell from the response that anything was cut. This is where 'we "
    "only ever saw the first 100' comes from, and the only defence is to refuse the request "
    "at the point it is built, because after the reply arrives the two cases are identical."
)

#: Why the absence of a continuation flag is stated rather than worked around.
THE_ONLY_END_SIGNAL_IS_A_SHORT_PAGE = (
    "A Freshdesk page carries no has_more, no cursor and no trustworthy total: the search's "
    "total is the ceiling once the ceiling is reached, so reading it as a count of matches "
    "reports 300 as a fact. What is left is arithmetic on the page that came back, and it is "
    "only sound if the size asked for is the size the endpoint honours. That is why the page "
    "size is part of the request rather than a global, and why a request that names a size "
    "the endpoint would change is refused before it is sent."
)

#: Why a capped result set is marked rather than trimmed, retried or refused.
A_CEILING_REACHED_IS_AN_ANSWER_THAT_MUST_SAY_SO = (
    "Three hundred records that stop because the source stops look exactly like three "
    "hundred records that stop because there were three hundred, and the difference is "
    "invisible in the response, in a log, and in every test written against a fixture with "
    "fewer than three hundred rows. Refusing the answer would be worse than useless, since "
    "the records are real and the question is often answerable from them; retrying returns "
    "the same three hundred, which is why `throttle.is_retryable` says no to TRUNCATED. So "
    "the records are returned, `TypedResult.truncated` is set, and the completeness verdict "
    "travels beside them with the sentence `brain.ops.limits` already wrote. What to do "
    "about it belongs to the abstention path: say what was searched and what could not be "
    "seen."
)

#: Why there is nowhere in this module to put a previously fetched answer.
AN_UNREACHABLE_SOURCE_HAS_NOTHING_TO_SUBSTITUTE = (
    "When Freshdesk refuses on volume or fails to answer, the correct behaviour is to say "
    "the source could not be reached. The tempting alternative is to serve what was fetched "
    "an hour ago, which produces an answer that is confident, plausible, undated and wrong "
    "in exactly the questions this system exists for: a ticket count from before the "
    "incident, quoted during the incident. The refusal is structural rather than a rule "
    "somebody follows, because a cache parameter added later would read as an optimisation "
    "in review. No function here takes one, and `assert_no_substitute_source` refuses a "
    "fetch that grows one."
)

#: Why an empty result, a refused credential and an unreachable source are three values.
ABSENT_REFUSED_AND_UNREACHABLE_ARE_THREE = (
    "A search that matched nothing is a fact about the helpdesk. A 401 or a 403 is a fact "
    "about our own credential, and the remedy is a person changing a key rather than "
    "waiting. A 429 or a 502 is a fact about reaching the source at all, and the remedy is "
    "to wait. Collapsed into one empty list they become the same answer, and the one it "
    "reads as is the reassuring one: no tickets. So an absence is a result with no records "
    "in it, and the other two are raised, with the outcome on the exception so an operator "
    "can tell which happened. The sentence a person is shown stays the same for both, "
    "because which of our systems is unwell is not theirs to learn from a failure."
)

#: Why the requester is kept as an id and the address is not kept at all.
A_REQUESTER_IS_A_JOIN_KEY_AND_NEVER_AN_ADDRESS = (
    "Every question about a ticket eventually wants to know who raised it, and the field "
    "that answers it in the source is an email address. An address is on the permanent "
    "denylist in `brain.core.projection` because storing it turns a permission mistake into "
    "a breach: a bug over a projected field leaks every address we kept, while the same bug "
    "over a federated one leaks the single record that question fetched. The requester id "
    "answers the same questions for the fast lane, joins to the entity registry, and is "
    "useless to anybody who cannot already reach Freshdesk. The address is mapped only on "
    "the live contact read, where it is never stored, and the contact entity projects "
    "nothing at all for the same reason."
)


# ---------------------------------------------------------------------------- the names
#: The connector's name, and the key `brain.ops.limits` records the verified ceiling and the
#: search cap under. The same string in both places on purpose: `throttle.limits_for` looks
#: up `manifest.ceiling` rather than `manifest.name`, so a deployment installed under a
#: client's own name still has to point at this one or it runs against no measured limit at
#: all. `manifest` below sets `ceiling` to exactly this.
FRESHDESK: Final = "freshdesk"

#: The entity kinds this connector returns. Both are names in the sense
#: `brain.core.field_policy` means: a policy is looked up by this string, and a tag nothing
#: matches is withheld from everybody.
TICKET: Final = "ticket"
CONTACT: Final = "contact"

#: This connector's own version, which moves when anything in the manifest moves. An upgrade
#: is recognised by a version change, so editing a field mapping without touching this leaves
#: a pinned digest disagreeing with a connector nobody upgraded.
VERSION: Final = "1.0.0"

#: What the field mapping names its specification. A reference and not a document: a vendor
#: spec is hundreds of kilobytes on the vendor's own release schedule, and embedding it would
#: put every unrelated edit inside the pinned digest.
SPEC_REF: Final = "freshdesk.v2"

#: Freshdesk numbers pages from one. A connector starting at zero either reads the first page
#: twice or is answered with an error, and the first of those is the one that looks like it
#: worked.
FIRST_PAGE: Final = 1

#: The search endpoint's page size. Fixed by the vendor rather than requested: `per_page` is
#: not read on this endpoint, so a request naming any other size is describing a page it will
#: not get. See `A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE`.
SEARCH_PAGE_SIZE: Final = 30

#: The largest page the list endpoints honour. Anything above it is clamped silently, which
#: is why `PageRequest` refuses rather than sends.
LIST_MAX_PAGE_SIZE: Final = 100

#: How many search pages exist before the hard ceiling is reached. Derived from the recorded
#: ceiling rather than declared beside it, so there is exactly one number to keep true: raise
#: the page size and this follows, instead of the two quietly disagreeing about what the
#: ceiling is.
SEARCH_MAX_PAGES: Final = math.ceil(FRESHDESK_SEARCH_MAX_RECORDS / SEARCH_PAGE_SIZE)

#: What an unreachable source's data is worth, in `brain.gate.provenance`'s vocabulary. Not
#: STALE, which is a claim about an age we would have to have measured: nothing was read, so
#: there is no read time to state and nothing may be rendered as current. UNSTATED is the
#: fail-closed state there and it is the honest one here.
UNREACHABLE_FRESHNESS: Final = Freshness.UNSTATED

#: The retry hint used when a source refuses on volume and says nothing about when. The
#: platform's own ceiling rather than a number invented here, and deliberately the long end:
#: guessing low burns what is left of the budget faster, and every recorded 429 in the
#: corpus carries a hint, so an absent one is a deviation rather than the ordinary case.
RETRY_AFTER_WHEN_UNSTATED: Final = MAX_BACKOFF_SECONDS

#: Parameter names that would let a fetch answer from something other than the call it is
#: making. Matched by name for the reason `contract.CREDENTIAL_ATTRIBUTE_RE` gives about
#: credentials: what is being smuggled is an ordinary mapping or sequence, so a rule that
#: only looked at types would pass `cache: dict[str, Any]` and refuse nothing.
SUBSTITUTE_PARAMETER_RE: Final = re.compile(
    r"(^|_)(cache|cached|fallback|previous|prior|memo|memoised|snapshot|last_answer"
    r"|last_known|from_memory)(_|$)"
)


# ------------------------------------------------------------------------ the endpoints
class Endpoint(enum.StrEnum):
    """The four Freshdesk operations this connector reads.

    Closed, and small on purpose. Every member here is an operation somebody has decided we
    need, with a field mapping reviewed beside it; adding a fifth is a manifest edit that
    moves the digest, which is the visibility the architecture asks for. The two paged ones
    behave differently enough that the difference is a table below rather than a comment.
    """

    #: `/api/v2/search/tickets`. Fixed page size, hard 300-record ceiling.
    SEARCH_TICKETS = "search_tickets"
    #: `/api/v2/tickets`. Pages by number with a requested size, bare array in the body.
    LIST_TICKETS = "list_tickets"
    #: One ticket by id.
    GET_TICKET = "get_ticket"
    #: One contact by id. Federated only: nothing from here is ever stored.
    GET_CONTACT = "get_contact"


@dataclass(frozen=True)
class EndpointShape:
    """How one endpoint pages, what it returns, and whether the ceiling applies to it.

    The specification is carried whole rather than summarised, so `records_at` and the
    parameter list have one home: `brain.connectors.rest.RestOperation.project` reads them to
    find the rows, and this module reads the same object to decide how to walk. A second copy
    of "where the records live" is a second thing to keep in step with the vendor, and the
    copy that drifts is the one that silently reads an empty array.
    """

    endpoint: Endpoint
    spec: OperationSpec
    entity: str
    #: The largest page this endpoint will honour.
    max_page_size: int
    #: What the endpoint calls its page-size parameter, or empty when it has none and the
    #: size is fixed at `max_page_size`. Empty is not "unlimited": it means a request naming
    #: any other size is describing a page the source will not send.
    page_size_parameter: str = ""
    #: What the endpoint calls its page-number parameter, or empty when it does not page.
    page_parameter: str = ""
    #: Whether the source's hard result ceiling applies to this endpoint's result set.
    capped: bool = False

    @property
    def paged(self) -> bool:
        return bool(self.page_parameter)

    @property
    def fixed_page_size(self) -> bool:
        """Whether the size is the vendor's rather than the caller's."""
        return not self.page_size_parameter


def _query(name: str, *, required: bool = False) -> ParameterSpec:
    return ParameterSpec(name=name, location="query", required=required)


#: Every endpoint, with the specification it is reached by. Total over `Endpoint`, and
#: `shape_for` is the only way in, so a member added without a row here fails in front of
#: whoever added it rather than being classified by a default. The same argument
#: `brain.connectors.change_signal.SIGNAL_FACTS` makes about its own table, and
#: `MappingProxyType` for the same reason: a module-level dict is one any importer can edit.
ENDPOINTS: Final[MappingProxyType[Endpoint, EndpointShape]] = MappingProxyType(
    {
        Endpoint.SEARCH_TICKETS: EndpointShape(
            endpoint=Endpoint.SEARCH_TICKETS,
            spec=OperationSpec(
                operation_id="searchTickets",
                method="get",
                path="/api/v2/search/tickets",
                parameters=(_query("query", required=True), _query("page")),
                records_at="results",
                returns_list=True,
            ),
            entity=TICKET,
            max_page_size=SEARCH_PAGE_SIZE,
            page_parameter="page",
            capped=True,
        ),
        Endpoint.LIST_TICKETS: EndpointShape(
            endpoint=Endpoint.LIST_TICKETS,
            spec=OperationSpec(
                operation_id="listTickets",
                method="get",
                path="/api/v2/tickets",
                parameters=(_query("page"), _query("per_page"), _query("updated_since")),
                # The body is the array itself. A connector looking for `results` here finds
                # nothing and reports an empty helpdesk, which is why the two endpoints carry
                # their own specifications rather than sharing one.
                records_at="",
                returns_list=True,
            ),
            entity=TICKET,
            max_page_size=LIST_MAX_PAGE_SIZE,
            page_size_parameter="per_page",
            page_parameter="page",
        ),
        Endpoint.GET_TICKET: EndpointShape(
            endpoint=Endpoint.GET_TICKET,
            spec=OperationSpec(
                operation_id="getTicket",
                method="get",
                path="/api/v2/tickets/{id}",
                parameters=(ParameterSpec(name="id", location="path", required=True),),
                records_at="",
                returns_list=False,
            ),
            entity=TICKET,
            max_page_size=1,
        ),
        Endpoint.GET_CONTACT: EndpointShape(
            endpoint=Endpoint.GET_CONTACT,
            spec=OperationSpec(
                operation_id="getContact",
                method="get",
                path="/api/v2/contacts/{id}",
                parameters=(ParameterSpec(name="id", location="path", required=True),),
                records_at="",
                returns_list=False,
            ),
            entity=CONTACT,
            max_page_size=1,
        ),
    }
)


def shape_for(endpoint: Endpoint) -> EndpointShape:
    """How this endpoint pages and where its records live.

    A function rather than a bare subscript, so the totality of `ENDPOINTS` is asserted in one
    place and no caller invents a fallback when a lookup misses. A missing row is a
    contract error rather than a default, because the default for "does the ceiling apply"
    is the answer that under-reports.
    """
    try:
        return ENDPOINTS[endpoint]
    except KeyError as exc:  # pragma: no cover - the totality test keeps this unreached
        msg = (
            f"{endpoint!r} has no endpoint shape, so nothing knows how it pages or whether "
            "the search ceiling applies to it; declare it before anything reads it"
        )
        raise ConnectorContractError(msg) from exc


# --------------------------------------------------------------------- one page, as a value
@dataclass(frozen=True)
class PageRequest:
    """One page of one endpoint, checked at the point it is built.

    Frozen and validated in `__post_init__` rather than checked by whoever sends it, because
    the two failures below are both invisible in the reply: a clamped page size comes back as
    a short page, and a zeroth page comes back as the first one. Neither is distinguishable
    afterwards from the thing it is pretending to be.
    """

    endpoint: Endpoint
    page: int
    page_size: int
    #: The source's own query vocabulary, passed through. `brain.connectors.rest` turns these
    #: into the address; nothing here is a permission decision, for the reason
    #: `contract.A_SCOPE_PREDICATE_IS_NOT_A_GRANT` gives.
    arguments: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        shape = shape_for(self.endpoint)
        if self.page < FIRST_PAGE:
            msg = (
                f"{self.endpoint} was asked for page {self.page}; Freshdesk numbers pages "
                f"from {FIRST_PAGE}, and a lower one is answered with the first page again "
                "rather than with an error, so the walk reads one page twice and calls it two"
            )
            raise ConnectorContractError(msg)
        if not shape.paged and self.page != FIRST_PAGE:
            msg = (
                f"{self.endpoint} returns one record and was asked for page {self.page}; a "
                "page number on an endpoint that does not page is an argument that is "
                "accepted and dropped, which reads at the call site as a filter being applied"
            )
            raise ConnectorContractError(msg)
        if self.page_size < 1:
            msg = (
                f"a page of {self.page_size} records is a call that costs a call and "
                "returns nothing"
            )
            raise ConnectorContractError(msg)
        if shape.fixed_page_size and self.page_size != shape.max_page_size:
            msg = (
                f"{self.endpoint} pages at exactly {shape.max_page_size} and does not read a "
                f"page-size parameter, so a request for {self.page_size} describes a page it "
                f"will not be sent. {A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE}"
            )
            raise ConnectorContractError(msg)
        if self.page_size > shape.max_page_size:
            msg = (
                f"{self.endpoint} honours at most {shape.max_page_size} records a page and "
                f"was asked for {self.page_size}; the source clamps rather than refuses. "
                f"{A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE}"
            )
            raise ConnectorContractError(msg)

    def as_arguments(self) -> dict[str, str]:
        """The arguments `brain.connectors.rest.RestOperation.url_for` builds the address from.

        The page number and the page size are added only where the endpoint declares a
        parameter for them, so an endpoint that ignores `per_page` is not sent one. `url_for`
        refuses an argument the operation does not declare, which is the second half of the
        same rule and the reason this cannot quietly send a parameter nobody reads.
        """
        shape = shape_for(self.endpoint)
        built = dict(self.arguments)
        if shape.page_parameter:
            built[shape.page_parameter] = str(self.page)
        if shape.page_size_parameter:
            built[shape.page_size_parameter] = str(self.page_size)
        return built


def first_page(endpoint: Endpoint, *, arguments: tuple[tuple[str, str], ...] = ()) -> PageRequest:
    """The first page of an endpoint, at the size that endpoint actually honours.

    A named constructor rather than defaults on `PageRequest`, because the size a request
    should carry is a property of the endpoint and looking it up is the step somebody skips.
    """
    return PageRequest(
        endpoint=endpoint,
        page=FIRST_PAGE,
        page_size=shape_for(endpoint).max_page_size,
        arguments=arguments,
    )


@dataclass(frozen=True)
class Reply:
    """What came back, as a value. The same three fields a cassette records.

    Deliberately identical in shape to `tests.fixtures.cassettes.Cassette`, so a recorded
    exchange becomes one of these without a translation step that could disagree with the
    recording. This module never constructs one: it is what a transport hands over, which is
    what keeps every rule here testable without a socket.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None

    def header(self, name: str) -> str:
        """One header, matched without regard to case.

        HTTP header names are case-insensitive and vendors change their casing between
        releases. A connector matching `Retry-After` exactly, handed `retry-after`, finds
        nothing and falls back to a guess while believing it read the source's own hint.
        """
        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return ""


class PageReader(Protocol):
    """Whatever performs one exchange and hands back the reply.

    A protocol rather than a client, so this module holds no connection, and for the reason
    `brain.knowledge.rows.RowSource` gives about its own: the cases that matter here are the
    ceiling, the short page and the refusal, and none of them can be arranged reliably
    against a real helpdesk. In production the implementation is
    `brain.connectors.rest.RestOperation.read`'s neighbourhood, borrowing a lease for the
    duration of the call; in tests it is the recorded cassettes.
    """

    def read(self, request: PageRequest) -> Reply: ...


# ------------------------------------------------------------------ what a reply means
class FreshdeskUnreachableError(Degraded):
    """Freshdesk could not answer: a quota refusal, a timeout, or a server failure.

    A `Degraded` and therefore carrying the platform's one sentence for this, which does not
    name the system. The call outcome and the retry hint are for whoever is on call; the
    person who asked the question is told the same thing whatever failed, because which of the
    company's systems is unwell is not a fact obtainable by typing a question.

    `call_outcome` is spelled out rather than reusing `BrainError.outcome`, which is the
    user-facing taxonomy and is DEGRADED for both of the classes here. Two different questions
    ("what is the person told" and "what did the source actually do") sharing one attribute is
    how the second one ends up rendered to somebody.
    """

    def __init__(
        self,
        detail: str = "",
        *,
        call_outcome: CallOutcome = CallOutcome.UNAVAILABLE,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome
        self.retry_after = retry_after

    @property
    def freshness(self) -> Freshness:
        """What anything a caller might substitute would be worth: nothing datable.

        `UNSTATED` rather than `STALE`, and the distinction is `brain.gate.provenance`'s: we
        did not read anything, so there is no read time, and a caller must not be able to
        treat "we do not know" as merely "we know it is old".
        """
        return UNREACHABLE_FRESHNESS

    def trace_line(self) -> str:
        """The full statement, for an operator rather than for the asker.

        Names the source and the outcome unconditionally, which is safe for the reason
        `brain.connectors.federation.PartialAnswer.trace_lines` is safe: a trace is read by
        somebody already entitled to know what the system connects to, and nothing here can
        put this string into a channel payload.
        """
        return (
            f"{FRESHDESK}: {self.call_outcome}, retry after {self.retry_after:.0f}s, "
            f"data {FRESHNESS_TEXT[self.freshness]}"
        )


class FreshdeskRefusedError(Degraded):
    """Freshdesk understood the request and would not answer it.

    Its own type rather than a flag on the one above, because the two go to different people
    and have opposite remedies: an expired key or a scope the client narrowed is somebody
    changing a credential, and waiting makes it no better. `throttle.is_retryable` says the
    same thing about `REJECTED`, and this is the shape that stops a retry loop being written
    against it in the first place.

    Also a `Degraded`, so the asker is told the same sentence as for an outage. A refusal that
    read differently would say which of our credentials is wrong to somebody who asked about
    a ticket.
    """

    def __init__(
        self, detail: str = "", *, call_outcome: CallOutcome = CallOutcome.REJECTED
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome


def retry_hint(reply: Reply) -> float:
    """How long the source asked us to wait, or the long end when it did not say.

    Seconds only. Freshdesk states this header in seconds, and the other form it may take in
    HTTP is a date, which cannot be turned into a wait without reading a clock this module
    deliberately does not have. So an unparseable value takes the same path as an absent one
    rather than being half-understood, and the path is the long one: see
    `RETRY_AFTER_WHEN_UNSTATED`.
    """
    stated = reply.header("Retry-After").strip()
    if not stated:
        return RETRY_AFTER_WHEN_UNSTATED
    try:
        seconds = float(stated)
    except ValueError:
        return RETRY_AFTER_WHEN_UNSTATED
    if seconds <= 0:
        return RETRY_AFTER_WHEN_UNSTATED
    return seconds


def assert_answered(reply: Reply) -> None:
    """Raise unless this reply is an answer, keeping the three outcomes apart.

    The classification is `brain.connectors.throttle.classify`'s and is not restated here, so
    this cannot come to a different conclusion from the module that owns the rule that a 429
    is a quota refusal rather than ill health. What this adds is which exception each outcome
    becomes, and that mapping is the whole of `ABSENT_REFUSED_AND_UNREACHABLE_ARE_THREE`:

    - `QUOTA` and `UNAVAILABLE` are the source not answering. Raised, with the hint.
    - `REJECTED` is the source refusing us. Raised, with no hint, because a retry reproduces
      it exactly and a hint invites one.
    - `OK` returns, and an empty result set then travels as a result with no records in it.

    Called before the body is read, deliberately. A 429 carries a body of its own
    (`{"description": "Rate limit exceeded"}`), and projecting that first would report a rate
    limit as a malformed response, which sends whoever reads the error to the wrong module.
    """
    outcome = classify(status=reply.status)
    if outcome in (CallOutcome.QUOTA, CallOutcome.UNAVAILABLE):
        raise FreshdeskUnreachableError(
            f"{FRESHDESK} answered {reply.status}; the source could not be reached, and an "
            "answer from anywhere else would be presented as though it had been",
            call_outcome=outcome,
            retry_after=retry_hint(reply),
        )
    if outcome is CallOutcome.REJECTED:
        raise FreshdeskRefusedError(
            f"{FRESHDESK} refused the request with {reply.status}; this is our credential or "
            "our request rather than the helpdesk's health, so waiting does not fix it",
            call_outcome=outcome,
        )


# --------------------------------------------------------------------------- the walk
def next_page(request: PageRequest, *, rows_on_page: int, rows_so_far: int) -> PageRequest | None:
    """The page after this one, or None when there is not one worth asking for.

    Three ways a walk ends, and the order is the rule.

    **A short page.** The only end-of-data signal Freshdesk offers: see
    `THE_ONLY_END_SIGNAL_IS_A_SHORT_PAGE`. Sound only because `PageRequest` refused a size the
    endpoint would have clamped.

    **The hard result ceiling.** Checked through `brain.ops.limits.search_completeness`, which
    owns which sources have one and is deliberately conservative at exactly the cap, so this
    cannot come to a more optimistic conclusion than the module the ceiling is recorded in.
    Asking for the page past the ceiling spends a call to be told nothing.

    **An endpoint that does not page at all**, which is the by-id reads.
    """
    if rows_on_page < 0 or rows_so_far < 0:
        msg = "a page cannot hold a negative number of rows"
        raise ValueError(msg)
    shape = shape_for(request.endpoint)
    if not shape.paged:
        return None
    if rows_on_page < request.page_size:
        return None
    if shape.capped and not search_completeness(FRESHDESK, rows_so_far).complete:
        return None
    return replace(request, page=request.page + 1)


@dataclass(frozen=True)
class SearchReading:
    """A search's records, and everything needed to say what they are not.

    `completeness` is a field rather than something a caller recomputes, so the verdict that
    travels with the records is the one the walk actually stopped on. There is deliberately
    no value here meaning "withhold", which is the shape `brain.connectors.projection` and
    `brain.ops.limits` both use for their own assessments: a future caller cannot start
    refusing on truncation without adding somewhere to express it and being seen in review.
    """

    result: TypedResult[SourceRecord]
    completeness: SearchCompleteness
    pages_read: int
    #: Whether the walk stopped because the caller asked for fewer records, which is a
    #: different claim from the ceiling and has to stay separable from it: the caller knows
    #: they asked, and nobody knows the source stopped.
    stopped_at_caller_limit: bool = False

    @property
    def hit_the_ceiling(self) -> bool:
        return not self.completeness.complete

    @property
    def is_all_of_them(self) -> bool:
        """Whether this may be spoken about as everything matching the search."""
        return self.completeness.complete and not self.stopped_at_caller_limit

    def trace_line(self) -> str:
        """What the search did, for an operator. Names the source, as a trace may."""
        return (
            f"{FRESHDESK}.{TICKET}: {len(self.result.records)} record(s) over "
            f"{self.pages_read} page(s); {self.completeness.reason}"
        )


def search_tickets(
    operation: RestOperation,
    reader: PageReader,
    *,
    query: str,
    fetched_at: str,
    limit: int = 0,
) -> SearchReading:
    """Walk the search endpoint to the ceiling, and say plainly where it stopped (M11.6.2).

    The projection is `brain.connectors.rest.RestOperation.project`'s, so what the mapping
    does not name never arrives: a fresh dictionary per row rather than a copy of the source's
    row, which is what stops a vendor adding a column from adding it to us. The rows are
    counted before that projection, because the ceiling is a fact about what the source
    returned rather than about what survived our mapping, and `normalise` drops a row with no
    usable id.

    `limit` is the caller's and is a request rather than a guarantee, exactly as
    `contract.FetchRequest` says: a limit above the ceiling comes back short and the result
    says so. A limit of zero means "as much as the source will give", which is at most 300.
    """
    if limit < 0:
        msg = "a negative limit is not a limit"
        raise ValueError(msg)
    request = first_page(Endpoint.SEARCH_TICKETS, arguments=(("query", query),))
    rows: list[Mapping[str, Any]] = []
    pages = 0
    stopped_at_caller_limit = False
    while True:
        page = read_page(operation, reader, request)
        pages += 1
        rows.extend(page)
        if limit and len(rows) >= limit:
            del rows[limit:]
            stopped_at_caller_limit = True
            break
        following = next_page(request, rows_on_page=len(page), rows_so_far=len(rows))
        if following is None:
            break
        request = following

    completeness = search_completeness(FRESHDESK, len(rows))
    result = normalise(
        operation.transport.entity,
        tuple(rows),
        source=FRESHDESK,
        fetched_at=fetched_at,
        # Both reasons produce a partial answer and they are not the same claim, so both set
        # the flag and `SearchReading` keeps them apart for anybody who needs to say which.
        truncated=not completeness.complete or stopped_at_caller_limit,
    )
    return SearchReading(
        result=result,
        completeness=completeness,
        pages_read=pages,
        stopped_at_caller_limit=stopped_at_caller_limit,
    )


def read_page(
    operation: RestOperation, reader: PageReader, request: PageRequest
) -> tuple[Mapping[str, Any], ...]:
    """One exchange: refuse the failure, then project what the mapping names.

    The order is load-bearing and is argued for in `assert_answered`. What is not re-wrapped
    is the other failure: a body that is not the shape the operation's own specification
    declares raises `brain.connectors.rest.RestSpecError` from `project`, and that is left to
    propagate rather than being renamed here, for the reason `brain.connectors.rest` gives
    about not giving an operator two names for one refusal. The property that matters holds
    either way, and it is the one a naive connector loses: an unreadable body is a failure and
    never an empty result, because reporting an outage as an absence is how "no tickets"
    becomes the answer during an incident.
    """
    reply = reader.read(request)
    assert_answered(reply)
    return operation.project(reply.body)


# ------------------------------------------------------- the fetch, as the contract wants it
def assert_no_substitute_source(fetch: Callable[..., object]) -> None:
    """Refuse a fetch that could be handed a previous answer (M11.6.2).

    Checked over the signature rather than the body, in the same form and for the same reason
    as `contract.assert_fetches_only`: what a function does is a body somebody edits, and what
    it can be given is a declaration a test can read. A fetch that never receives a cache
    cannot serve from one, so "we never answer from memory" is a shape rather than a promise.

    Note what this does not do. It cannot stop an author reaching a module-level dictionary,
    and nothing structural can; what it removes is the ordinary way this happens, which is a
    caller with a stale result in hand passing it in as a courtesy during an incident. See
    `AN_UNREACHABLE_SOURCE_HAS_NOTHING_TO_SUBSTITUTE`.
    """
    try:
        signature = inspect.signature(fetch)
    except (TypeError, ValueError) as exc:
        msg = f"{getattr(fetch, '__name__', fetch)!r} has no readable signature to check"
        raise ConnectorContractError(msg) from exc

    name = getattr(fetch, "__name__", repr(fetch))
    offenders = sorted(
        parameter.name
        for parameter in signature.parameters.values()
        if SUBSTITUTE_PARAMETER_RE.search(parameter.name.casefold())
    )
    if offenders:
        msg = (
            f"connector fetch {name!r} takes {offenders}, which could carry an answer this "
            f"call did not fetch. {AN_UNREACHABLE_SOURCE_HAS_NOTHING_TO_SUBSTITUTE}"
        )
        raise ConnectorContractError(msg)


def ticket_search_fetch(
    operation: RestOperation, reader: PageReader, *, fetched_at: str
) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
    """The search as a connector fetch, checked against the contract before it is returned.

    The checks run on the closure rather than on this factory, and that is the point of
    building one: the closure is the object a registry would call, so it is the object whose
    signature has to be shown never to receive the caller's grants, a vault, or a previous
    answer. The reader and the stamp are wiring, supplied by whoever builds the connector,
    and a parameter a caller cannot reach is a parameter that cannot carry any of the three.

    A cursor is refused rather than ignored. Freshdesk pages by number, so an opaque cursor
    means the caller believes this source resumes the way another one does, and answering
    with the first page would be a wrong answer that reads as a right one.
    """

    def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
        if request.entity != operation.transport.entity:
            msg = (
                f"this operation maps {operation.transport.entity!r} and was asked for "
                f"{request.entity!r}"
            )
            raise ConnectorContractError(msg)
        if request.cursor:
            msg = (
                "Freshdesk search pages by number and has no cursor to resume from; a cursor "
                "here means the caller expects this source to page like another one, and the "
                "first page returned in answer would read as the page they asked for"
            )
            raise ConnectorContractError(msg)
        return search_tickets(
            operation,
            reader,
            query=_search_query(request.filters),
            fetched_at=fetched_at,
            limit=request.limit,
        ).result

    assert_fetches_only(_fetch)
    assert_no_substitute_source(_fetch)
    return _fetch


#: The two quote characters a search value could close the expression with. Removed rather
#: than escaped: an escape is a second opinion about the source's own parser, and the one
#: that decides what runs is the source's. A value with a quote in it is a value nobody
#: searches for on purpose, and dropping the character fails in the safe direction, because a
#: term that matches nothing returns nothing while a term that matched more would return
#: records nobody asked about.
_QUOTES: Final = str.maketrans({"'": None, '"': None})


def _search_query(filters: tuple[tuple[str, str], ...]) -> str:
    """The source's own search syntax, built from the filters the gate passed through.

    Freshdesk's search takes one quoted expression of `field:value` terms joined by AND, so
    the filters are rendered into that rather than into a parameter each. This is not a
    permission decision and has nowhere to become one: the filters arrive already decided, and
    a connector narrowing by entitlement is the thing `contract.A_CONNECTOR_NEVER_DECIDES`
    refuses by never handing one an entitlement set.
    """
    terms = [f"{name}:'{value.translate(_QUOTES)}'" for name, value in filters]
    return f'"{" AND ".join(terms)}"'


# ------------------------------------------------------------------------- the projection
#: What is kept locally about a ticket, and why each one earns a place under the twelve-field
#: cap. Nine of twelve, and the headroom is deliberate: the fields most often asked for next
#: are a ticket type and a source channel, and leaving room for them means adding one is a
#: review rather than an argument about which existing field to drop.
#:
#: `id` is deliberately absent. `brain.connectors.projection.ProjectedRecord` carries the
#: source id as its own field and the primary key of `proj.record` is built from it, so
#: declaring it here would spend one of the twelve on a value the row already has.
TICKET_FIELDS: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(
        name="company_id",
        shape=FieldShape.JOIN_KEY,
        uses=(HotUse.JOIN, HotUse.FILTER),
    ),
    #: The requester as the source's own opaque id, never as an address. See
    #: `A_REQUESTER_IS_A_JOIN_KEY_AND_NEVER_AN_ADDRESS`.
    ProjectedField(name="requester_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN,)),
    ProjectedField(name="group_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.FILTER,)),
    ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)),
    ProjectedField(
        name="priority",
        shape=FieldShape.STATUS,
        uses=(HotUse.FILTER, HotUse.SORT, HotUse.COUNT),
    ),
    ProjectedField(
        name="created_at",
        shape=FieldShape.TIMESTAMP,
        uses=(HotUse.FILTER, HotUse.SORT),
    ),
    ProjectedField(
        name="updated_at",
        shape=FieldShape.TIMESTAMP,
        uses=(HotUse.FILTER, HotUse.SORT),
    ),
    ProjectedField(name="due_by", shape=FieldShape.TIMESTAMP, uses=(HotUse.FILTER, HotUse.SORT)),
    #: The one label. A second one would be a payload arriving in instalments, which is what
    #: the pointer clause in `manifest.projectability` refuses.
    ProjectedField(name="subject", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,)),
)

#: The name of the projected field that identifies a ticket to a person, and therefore the one
#: value that has to be cut to fit `brain.core.projection.MAX_LABEL_CHARS`.
LABEL_FIELD: Final = "subject"

#: Freshdesk fields this connector deliberately never projects, with the reason each is out.
#: Written down because the interesting half is that they are refused by *different* rules:
#: an address is on the permanent denylist and a ticket body is not, so a reader assuming the
#: denylist covers everything would conclude a description is projectable. It is refused by
#: the pointer and hot clauses instead, and a reviewer needs to know which rule to check.
FETCHED_LIVE_INSTEAD: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "requester_email": "on the permanent denylist; the requester id answers the same "
        "questions and cannot be posted anywhere useful to somebody who stole it",
        "phone": "on the permanent denylist, at any size, under any configuration",
        "description": "not on the denylist and refused anyway: nothing in the fast lane "
        "filters, sorts or counts on a ticket body, and it would be a second label",
        "description_text": "the same body with the markup removed, and the same answer",
        "conversations": "on the denylist as `conversation`, and a container besides",
        "custom_fields": "a nested object, which is how a twelve-field cap is defeated "
        "politely; name the leaves the fast lane filters on or fetch it live",
    }
)


def ticket_projection(*, visibility: Scope) -> ProjectedEntity:
    """What is kept about a ticket, and the predicate that decides who may read a row.

    `visibility` has no default, deliberately, and it is the one thing this module refuses to
    decide. A predicate supplied here would be one helpdesk's ownership rule applied to
    another client's tickets, and `manifest.ProjectedEntity` already refuses an unrestricted
    one because a projection stored with no predicate has discarded the source's permission
    model rather than narrowed it. The shape Freshdesk's own visibility takes is a test over
    the group that owns the ticket, which is why `group_id` is projected; it is stored as a
    predicate and evaluated against the live entitlement set, so somebody moving team gets a
    different row set with no writes and no invalidation.

    The change signal is `UPDATED_SINCE` and not a webhook. Freshdesk can fire one, but only
    through an automation rule inside the client's own account, so a connector declaring
    WEBHOOK would be declaring somebody else's configuration as our guarantee. The cursor is
    what the API itself offers, and `subscription` below carries the consequence: a cursor
    cannot see a deletion.
    """
    return ProjectedEntity(
        entity=TICKET,
        fields=TICKET_FIELDS,
        change_signal=ChangeSignal.UPDATED_SINCE,
        visibility=visibility,
    )


def projected_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in TICKET_FIELDS)


def projected_fields(row: Mapping[str, Any]) -> dict[str, ProjectedValue]:
    """One fetched ticket as the fields a projected record may hold.

    Built from the declared names rather than by removing what is unwanted from the row, for
    the reason `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE` gives:
    the two read the same today and diverge the first time the vendor adds a column, because
    a copy carries it and a build does not.

    The label is cut to `MAX_LABEL_CHARS`, and the cut is silent by design. A marker would
    make a subject that genuinely ends in an ellipsis indistinguishable from one that was cut,
    and the record's identity is its id rather than its label, so a shortened label still
    identifies exactly what it identified before. The alternative is worse in the way that
    matters: `check_projection` refuses an over-long string, so a ticket with a long subject
    would be dropped whole at ingest and the projection would silently be missing precisely
    the noisiest tickets.
    """
    built: dict[str, ProjectedValue] = {}
    for name in projected_field_names():
        if name not in row:
            # An absent field contributes nothing rather than a null. A vendor omitting an
            # optional field has said something different from one sending an empty value.
            continue
        value = row[name]
        if name == LABEL_FIELD and isinstance(value, str):
            value = value[:MAX_LABEL_CHARS]
        built[name] = value
    return built


# ------------------------------------------------------------------- the field mappings
def _mapping(*names: str) -> tuple[FieldMapping, ...]:
    """Identity mappings for names Freshdesk and we happen to spell the same way.

    The identity looks pointless and is not: the mapping is an allowlist, so what it does not
    name cannot arrive whatever the source sends. The recorded ticket in the cassettes carries
    a canary in `custom_fields.internal_note`, and it never appears in a projected row for
    exactly this reason.
    """
    return tuple(FieldMapping(target=name, source_path=name) for name in names)


#: The ticket mapping: the projected fields plus the record id, which `normalise` reads and
#: which `RestOperation` refuses a mapping for lacking.
TICKET_MAPPING: Final[tuple[FieldMapping, ...]] = _mapping("id", *(f.name for f in TICKET_FIELDS))

#: The contact mapping, and the one place in this connector where an address is named at all.
#: This is a federated read: the record is returned to answer one question and is never
#: stored, which is the distinction `brain.core.projection` draws in its own docstring
#: between a restriction on storage and a restriction on access. HR can read a salary; the
#: salary is fetched from the source each time and never lands here. Moving any of these into
#: a projection is refused twice over, at manifest review and again at ingest.
CONTACT_MAPPING: Final[tuple[FieldMapping, ...]] = _mapping(
    "id", "name", "email", "phone", "company_id"
)

FIELDS_BY_ENTITY: Final[MappingProxyType[str, tuple[FieldMapping, ...]]] = MappingProxyType(
    {TICKET: TICKET_MAPPING, CONTACT: CONTACT_MAPPING}
)


def transport_for(endpoint: Endpoint) -> RestTransport:
    """The declaration `brain.connectors.rest` binds to a parsed operation.

    The operation id comes off the endpoint's own specification rather than being written
    again here, so the mapping cannot name an operation the shape does not describe.
    """
    shape = shape_for(endpoint)
    return RestTransport(
        spec_ref=SPEC_REF,
        operation=shape.spec.operation_id,
        entity=shape.entity,
        fields=FIELDS_BY_ENTITY[shape.entity],
    )


def operation_for(endpoint: Endpoint, *, domain: str) -> RestOperation:
    """One endpoint, bound to its mapping and to the one address it is reached at.

    `RestOperation.__post_init__` runs `assert_maps_only` over every declaration this is built
    from, so a mapping that grew a permission clause is refused here rather than at the first
    request. Nothing is fetched: the address is built and checked by `prepare`, which the
    transport calls with a resolver this module does not have.
    """
    return RestOperation(
        base_url=f"https://{domain}",
        operation=shape_for(endpoint).spec,
        transport=transport_for(endpoint),
    )


# ---------------------------------------------------------------- the change subscription
def subscription(*, notify_within: timedelta, reconcile_every: timedelta) -> ChangeSubscription:
    """How Freshdesk tells us a projected ticket moved, and how a deletion is ever learned.

    Two of the four fields are facts about the API and are fixed here. The cursor is what
    Freshdesk offers (`updated_since` on the list endpoint), and `ID_SWEEP` is the only
    deletion check a read-only integration has: a removed ticket is not "updated", it is
    simply one the cursor never mentions again, so absence has to be checked for by
    enumerating the ids the source still returns. See
    `brain.connectors.change_signal.A_CURSOR_CANNOT_SEE_A_DELETION`, which is the same
    mechanism this estate already runs for Lark Base.

    The two intervals have no defaults and are the deployment's, matching `RefreshPromise`'s
    own refusal to hold one: how often a client's helpdesk is polled and fully reconciled is a
    property of that installation, and a module-level number applied on a caller's behalf
    would be an inference presented as a declaration.
    """
    return ChangeSubscription(
        source=FRESHDESK,
        entity=TICKET,
        kind=ChangeSignal.UPDATED_SINCE,
        notify_within=notify_within,
        reconcile_every=reconcile_every,
        deletion_check=DeletionCheck.ID_SWEEP,
    )


# --------------------------------------------------------------------------- the manifest
#: What the model reads when it decides whether this tool answers the question. Inside the
#: pinned digest, and written to say the one thing that is true of this endpoint and of
#: almost no other: the result set has a ceiling and a full one is not evidence of anything.
SEARCH_TOOL_DESCRIPTION: Final = (
    "Search the helpdesk for tickets matching a filter. Returns at most 300 tickets however "
    "many match, and reports the result as incomplete when that ceiling is reached, so a full "
    "result is never evidence that it is all of them."
)


def manifest(
    *,
    domain: str,
    credential: CredentialBinding,
    visibility: Scope,
    version: str = VERSION,
) -> ConnectorManifest:
    """Everything this connector declares, for one deployment (M11.6.2).

    **The scope names one helpdesk, and it is worth saying what that does and does not
    narrow.** It refuses a credential pointed at a different Freshdesk account, which is the
    mistake a copied configuration makes. It does not narrow anything *within* the account,
    because a Freshdesk API key is account-wide and there is no per-group key to ask for; a
    scope claiming otherwise would read in a console as a boundary that had been enforced.
    `brain.connectors.transports.THE_SANDBOX_IS_NOT_IN_THIS_MODULE` makes the same
    distinction about a sandbox profile, and the honest statement is the same: somebody chose
    this, and choosing it is not the same as it having been enforced.

    **`ceiling` is the verified source name and not this deployment's.** `throttle.limits_for`
    looks the numbers up by that field, and a connector installed as `acme_helpdesk` with no
    ceiling named would run against no measured limit at all rather than against Freshdesk's
    hundred a minute.

    **Every tool declares SERVICE identity**, which is the honest reading of a shared API key.
    The source is not enforcing anybody's permissions on our behalf, so ours are the only ones
    there are, and that is exactly the case `brain.tools.registry` refuses to register without
    a scope predicate.
    """
    return ConnectorManifest(
        name=FRESHDESK,
        version=version,
        transport=TransportKind.REST,
        scope=ConnectorScope(resource_kind="helpdesk", selectors=(domain,)),
        credential=credential,
        tools=(
            ToolDeclaration(
                name="freshdesk.search_tickets",
                description=SEARCH_TOOL_DESCRIPTION,
                entity=TICKET,
                identity_mode=IdentityMode.SERVICE,
            ),
            ToolDeclaration(
                name="freshdesk.read_ticket",
                description="Read one helpdesk ticket by its id.",
                entity=TICKET,
                identity_mode=IdentityMode.SERVICE,
            ),
            ToolDeclaration(
                name="freshdesk.read_contact",
                description=(
                    "Read one helpdesk contact by its id, live. Contact details are fetched "
                    "for the question that needs them and are never stored."
                ),
                entity=CONTACT,
                identity_mode=IdentityMode.SERVICE,
            ),
        ),
        # One projection, and the absence of a second is the decision. A contact's useful
        # fields are its email and its phone, both permanently denylisted, so a contact
        # projection would be a name and an id: a mirror of nothing, kept fresh for nobody.
        projections=(ticket_projection(visibility=visibility),),
        ceiling=FRESHDESK,
    )
