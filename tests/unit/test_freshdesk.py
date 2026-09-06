"""The Freshdesk connector, driven by the recordings rather than by a helpdesk.

Four properties are being pinned here, and each is a way this connector's subject goes wrong
without anything failing.

**A result set that stopped at the ceiling says so.** Freshdesk's search returns at most 300
records ever, and the 300th page is reported exactly as a genuinely final one. Every test
below that walks to 300 is asserting the one thing the API cannot tell us.

**A page size the endpoint would clamp is refused before it is sent.** The failure it
prevents is a full page read as a short one, which is "we only ever saw the first 100"
arriving as a correct-looking answer.

**A source that could not answer is not an empty helpdesk.** The 429 recording is the whole
test: what comes back has to be a refusal, and it has to stay distinguishable from a search
that genuinely matched nothing and from a credential the source rejected.

**A requester is projected as an id and never as an address.** The denylist half is checked
against `brain.core.projection`, and so is the more interesting half, which is that a ticket
body is not on that list and is refused by a different clause entirely.

The fixture that matters is the cassettes. `FRESH-200-search` carries a canary in a custom
field, and a test here asserts it never survives the projection; `FRESH-429` carries the
`Retry-After` the connector must read rather than guess. Neither could be arranged against a
real helpdesk on demand, which is the point of recording them.

Task ids: M11.6.2
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.change_signal import DeletionCheck
from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    CredentialBinding,
    FetchRequest,
    assert_fetches_only,
    assert_holds_no_credential,
)
from brain.connectors.freshdesk import (
    CONTACT,
    FETCHED_LIVE_INSTEAD,
    FRESHDESK,
    LIST_MAX_PAGE_SIZE,
    RETRY_AFTER_WHEN_UNSTATED,
    SEARCH_MAX_PAGES,
    SEARCH_PAGE_SIZE,
    TICKET,
    TICKET_FIELDS,
    Endpoint,
    FreshdeskRefusedError,
    FreshdeskUnreachableError,
    PageRequest,
    Reply,
    assert_answered,
    assert_no_substitute_source,
    first_page,
    manifest,
    next_page,
    operation_for,
    projected_field_names,
    projected_fields,
    read_page,
    retry_hint,
    search_tickets,
    shape_for,
    subscription,
    ticket_projection,
    ticket_search_fetch,
)
from brain.connectors.manifest import ChangeSignal, ManifestError, ProjectedEntity, failed_clauses
from brain.connectors.manifest import projectability as clauses_for
from brain.connectors.projection import ProjectedRecord
from brain.connectors.rest import RestSpecError
from brain.connectors.throttle import CallOutcome, ceiling_for, is_retryable
from brain.core.envelope import IdentityMode
from brain.core.errors import Degraded, Outcome
from brain.core.projection import (
    MAX_LABEL_CHARS,
    MAX_PROJECTED_FIELDS,
    ProjectionRefusedError,
    check_projection,
    is_forbidden,
)
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import Freshness
from brain.ops.limits import FRESHDESK_SEARCH_MAX_RECORDS
from brain.ops.secrets import SecretRef, VaultRole
from tests.fixtures.cassettes import CASSETTES, Cassette, Source, for_source

DOMAIN = "verz.freshdesk.com"
FETCHED_AT = "2026-09-06T09:00:00+00:00"
NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: The predicate a deployment supplies. Freshdesk's own visibility follows the group that
#: owns the ticket, which is why `group_id` is projected; it enumerates teams rather than
#: people, so it is re-evaluated against the live entitlement set on every question.
VISIBILITY = Scope(clauses=(Clause(field="group_id", op=Op.IN, value=("g_support", "g_web")),))

READ_REF = SecretRef(path="connectors/freshdesk/api", role=VaultRole.APPLICATION)


def cassette(cid: str) -> Cassette:
    """One recording by id, so a test names what it is driven by."""
    return next(c for c in CASSETTES if c.cid == cid)


def reply_of(cid: str) -> Reply:
    """A recording as the value a page reader hands back.

    Field for field, with no translation: `Reply` carries the same three things a `Cassette`
    records precisely so this function cannot quietly disagree with the recording.
    """
    recorded = cassette(cid)
    return Reply(status=recorded.status, headers=recorded.headers, body=recorded.body)


def a_ticket(number: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 88_000 + number,
        "subject": f"SSL renewal {number}",
        "status": 2,
        "priority": 3,
        "company_id": 447,
        "requester_id": 9_001,
        "group_id": 12,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-05T10:00:00Z",
        "due_by": "2026-09-08T10:00:00Z",
        "requester_email": "someone@snm.example",
        "description": "x" * 400,
        "custom_fields": {"internal_note": "CANARY-TICKET-B6YHF"},
    }
    row.update(overrides)
    return row


def a_search_body(count: int) -> dict[str, Any]:
    return {"total": count, "results": [a_ticket(n) for n in range(count)]}


@dataclass
class Reader:
    """A page reader scripted with one reply per page, recording what it was asked for.

    A fake rather than a mock: the interesting assertions are about which pages were
    requested and which were not, and a reader that records is the only way to see the page
    that was never asked for.

    Strict about an unscripted page, deliberately. A reader that answered page eleven with
    page ten's reply would turn a walk that failed to stop at the ceiling into an endless
    loop, and a test that hangs is a test nobody keeps.
    """

    replies: list[Reply]
    seen: list[PageRequest]

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.seen = []

    def read(self, request: PageRequest) -> Reply:
        self.seen.append(request)
        if request.page > len(self.replies):
            raise AssertionError(
                f"page {request.page} was asked for and this reader holds "
                f"{len(self.replies)}; the walk did not stop where it should have"
            )
        return self.replies[request.page - 1]


def full_pages(count: int) -> Reader:
    """A reader answering every page with a full one, for as many pages as asked."""
    return Reader(*[Reply(status=200, body=a_search_body(SEARCH_PAGE_SIZE)) for _ in range(count)])


def a_manifest(**overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "domain": DOMAIN,
        "credential": CredentialBinding(ref=READ_REF),
        "visibility": VISIBILITY,
    }
    settings.update(overrides)
    return manifest(**settings)


def search_operation() -> Any:
    return operation_for(Endpoint.SEARCH_TICKETS, domain=DOMAIN)


# ------------------------------------------------------------------ the result ceiling
def test_a_search_that_reaches_the_ceiling_is_reported_as_incomplete() -> None:
    """The whole reason this connector is written carefully. Delete this and a search that
    stopped at Freshdesk's 300-record ceiling comes back looking like every other complete
    result, and 'all the open tickets for this client' is silently wrong for any client with
    more than 300, with nothing anywhere reporting it."""
    reading = search_tickets(
        search_operation(),
        full_pages(SEARCH_MAX_PAGES),
        query='"company_id:447"',
        fetched_at=FETCHED_AT,
    )

    assert len(reading.result.records) == FRESHDESK_SEARCH_MAX_RECORDS
    assert reading.hit_the_ceiling
    assert not reading.is_all_of_them
    assert reading.result.truncated
    assert str(FRESHDESK_SEARCH_MAX_RECORDS) in reading.completeness.reason


def test_a_search_short_of_the_ceiling_is_reported_as_everything_matching() -> None:
    """The positive case, and it is not decoration. A connector that marked every result
    truncated would pass the test above and would make every answer hedge, which trains a
    reader to ignore the sentence that eventually matters."""
    reader = Reader(Reply(status=200, body=a_search_body(4)))

    reading = search_tickets(
        search_operation(), reader, query='"company_id:447"', fetched_at=FETCHED_AT
    )

    assert len(reading.result.records) == 4
    assert reading.is_all_of_them
    assert not reading.hit_the_ceiling
    assert not reading.result.truncated


def test_the_walk_stops_at_the_ceiling_rather_than_asking_for_a_page_past_it() -> None:
    """Deleting this leaves a walk that pages until the source says no, which on the search
    endpoint means spending a call against a hundred-a-minute ceiling to be told nothing, on
    every capped search. The recorded ceiling is a fact we already hold; asking anyway is
    paying to rediscover it."""
    reader = full_pages(SEARCH_MAX_PAGES)

    reading = search_tickets(
        search_operation(), reader, query='"company_id:447"', fetched_at=FETCHED_AT
    )

    assert [request.page for request in reader.seen] == list(range(1, SEARCH_MAX_PAGES + 1))
    assert reading.pages_read == SEARCH_MAX_PAGES


def test_a_capped_search_is_not_retried_into_completeness() -> None:
    """The reading a caller reaches for next: retry it and see the rest. There is no rest.
    `throttle.is_retryable` says so for TRUNCATED and this connector produces exactly that
    situation, so the two have to agree or somebody writes the retry loop."""
    assert not is_retryable(CallOutcome.TRUNCATED)


def test_the_page_arithmetic_is_derived_from_the_recorded_ceiling() -> None:
    """If the page count were declared beside the ceiling rather than derived from it,
    somebody raising the page size to make the walk faster would leave the two disagreeing
    about what the ceiling is, and the walk would stop early or ask for a page that does not
    exist."""
    assert SEARCH_MAX_PAGES * SEARCH_PAGE_SIZE >= FRESHDESK_SEARCH_MAX_RECORDS
    assert (SEARCH_MAX_PAGES - 1) * SEARCH_PAGE_SIZE < FRESHDESK_SEARCH_MAX_RECORDS


# ---------------------------------------------------------------------- the paging shape
def test_a_page_shorter_than_the_one_asked_for_ends_the_walk() -> None:
    """A Freshdesk page carries no has_more and no cursor, so this arithmetic is the only
    end-of-data signal there is. Delete it and the walk either never stops or stops on
    something the body does not actually say."""
    request = first_page(Endpoint.SEARCH_TICKETS)

    assert next_page(request, rows_on_page=SEARCH_PAGE_SIZE - 1, rows_so_far=29) is None


def test_a_full_page_is_never_the_end_of_the_walk() -> None:
    """The sibling of the test above, and the one that catches the failure people actually
    ship: a walk that stops after the first full page reports the first hundred records as
    the whole answer."""
    request = first_page(Endpoint.LIST_TICKETS)

    following = next_page(request, rows_on_page=LIST_MAX_PAGE_SIZE, rows_so_far=LIST_MAX_PAGE_SIZE)

    assert following is not None
    assert following.page == 2
    assert following.page_size == request.page_size


def test_a_page_size_the_endpoint_would_clamp_is_refused_before_it_is_sent() -> None:
    """This is where 'we only ever saw the first 100' comes from. Freshdesk clamps rather
    than refuses, so a request for 500 comes back as 100 rows, 100 is short of the 500 asked
    for, and the walk stops believing it saw everything. After the reply arrives the two
    cases are identical, so the refusal has to happen here or nowhere."""
    with pytest.raises(ConnectorContractError) as caught:
        PageRequest(endpoint=Endpoint.LIST_TICKETS, page=1, page_size=LIST_MAX_PAGE_SIZE + 1)

    assert str(LIST_MAX_PAGE_SIZE) in str(caught.value)


def test_the_search_endpoint_pages_at_a_size_nobody_may_choose() -> None:
    """Search ignores `per_page` entirely, so a connector asking for 100 gets 30 and reads a
    full page as a short one after the first call: thirty tickets presented as all of them.
    The endpoint's size is not negotiable and a request naming another one is describing a
    page it will not be sent.

    Both directions, and the smaller one is the reason this test is not covered by the clamp
    test above. A size over the endpoint's maximum is refused by that rule wherever it
    appears; a size *under* a fixed one is refused only by this one, and it is the case that
    quietly breaks the short-page arithmetic, because every page then comes back larger than
    the walk believes it asked for."""
    with pytest.raises(ConnectorContractError):
        PageRequest(endpoint=Endpoint.SEARCH_TICKETS, page=1, page_size=LIST_MAX_PAGE_SIZE)

    with pytest.raises(ConnectorContractError) as caught:
        PageRequest(endpoint=Endpoint.SEARCH_TICKETS, page=1, page_size=SEARCH_PAGE_SIZE - 20)

    assert str(SEARCH_PAGE_SIZE) in str(caught.value)
    assert first_page(Endpoint.SEARCH_TICKETS).page_size == SEARCH_PAGE_SIZE
    assert not shape_for(Endpoint.SEARCH_TICKETS).page_size_parameter


def test_pages_are_numbered_from_one() -> None:
    """A zeroth page is answered with the first page rather than with an error, so a walk
    starting at zero reads page one twice and calls it two pages. Nothing in the reply says
    so, and the record count is simply wrong."""
    with pytest.raises(ConnectorContractError):
        PageRequest(endpoint=Endpoint.SEARCH_TICKETS, page=0, page_size=SEARCH_PAGE_SIZE)


def test_a_by_id_read_never_has_another_page() -> None:
    """The by-id reads return one record and page by nothing. Without this, a caller looping
    on `next_page` over a single ticket is handed a second page that `PageRequest` then
    refuses, so a read that should end quietly ends in a contract error instead."""
    single = first_page(Endpoint.GET_TICKET, arguments=(("id", "88213"),))

    assert next_page(single, rows_on_page=1, rows_so_far=1) is None


def test_a_page_number_is_refused_on_an_endpoint_that_does_not_page() -> None:
    """An argument that is accepted and silently dropped reads at the call site as a filter
    being applied. `brain.connectors.rest` refuses the same shape in a path template, and a
    by-id read paged like a list is the same mistake one layer up."""
    with pytest.raises(ConnectorContractError):
        PageRequest(endpoint=Endpoint.GET_TICKET, page=2, page_size=1)


def test_the_page_number_reaches_the_address_and_the_ignored_size_does_not() -> None:
    """Asserted on the built address rather than on the request, because the request is where
    a page number is easy to hold and forget to send: a walk that requests page four and
    fetches page one loops on the first page for ever and reports thirty records."""
    operation = search_operation()
    request = first_page(Endpoint.SEARCH_TICKETS, arguments=(("query", '"company_id:447"'),))
    second = next_page(request, rows_on_page=SEARCH_PAGE_SIZE, rows_so_far=SEARCH_PAGE_SIZE)

    assert second is not None
    address = operation.url_for(second.as_arguments())
    assert "page=2" in address
    assert "per_page" not in address


def test_the_list_endpoint_reads_its_records_from_a_different_place() -> None:
    """Freshdesk's list endpoints return a bare array and its search returns an envelope. A
    connector using one shape for both finds nothing at `results` and reports an empty
    helpdesk, which is the failure that reads as an answer. Deleting this lets the two
    endpoints share a specification and one of them silently return nothing."""
    assert shape_for(Endpoint.LIST_TICKETS).spec.records_at == ""
    assert shape_for(Endpoint.SEARCH_TICKETS).spec.records_at == "results"

    listed = operation_for(Endpoint.LIST_TICKETS, domain=DOMAIN)
    assert len(listed.project([a_ticket(1), a_ticket(2)])) == 2


def test_a_body_read_at_the_wrong_place_is_a_refusal_and_never_an_empty_helpdesk() -> None:
    """The direction of the failure is what matters. A source answering with a shape its own
    specification does not describe has failed, and reporting that as 'no tickets' summarises
    an outage as an absence, which nobody files a bug about."""
    searching = search_operation()

    with pytest.raises(RestSpecError):
        searching.project([a_ticket(1)])


# ------------------------------------------------------- unreachable, refused, absent
def test_a_rate_limited_search_is_reported_as_unreachable_and_not_as_no_tickets() -> None:
    """Driven by the 429 recording, and it is the reason the recording exists. A connector
    that has only ever been compiled against success turns this into an empty result, and an
    empty result from a permission-aware system reads as a permission decision or as a quiet
    helpdesk. Neither is true and both are actionable-looking."""
    reader = Reader(reply_of("FRESH-429"))

    with pytest.raises(FreshdeskUnreachableError) as caught:
        search_tickets(search_operation(), reader, query='"company_id:447"', fetched_at=FETCHED_AT)

    assert caught.value.call_outcome is CallOutcome.QUOTA
    assert caught.value.outcome is Outcome.DEGRADED


def test_a_server_failure_is_unreachable_for_the_same_reason_a_quota_refusal_is() -> None:
    """Two very different operational problems with the same answer for the asker: we could
    not reach it. Without this, a 5xx takes the success path, the body is projected, and
    whatever an error page happens to contain becomes records."""
    with pytest.raises(FreshdeskUnreachableError) as caught:
        assert_answered(Reply(status=502, body={"message": "Bad gateway"}))

    assert caught.value.call_outcome is CallOutcome.UNAVAILABLE


def test_a_refused_credential_is_not_reported_as_an_unreachable_source() -> None:
    """The remedies are opposite: one is somebody changing a key, the other is waiting.
    Collapsed together, an expired token is retried until the rate limit is exhausted and
    never succeeds, which is exactly what `XERO-401-expired` was recorded to prevent."""
    with pytest.raises(FreshdeskRefusedError) as caught:
        assert_answered(Reply(status=403, body={"description": "Access denied"}))

    assert caught.value.call_outcome is CallOutcome.REJECTED
    assert not is_retryable(caught.value.call_outcome)


def test_an_empty_search_is_an_answer_rather_than_a_failure() -> None:
    """The third of the three, and the positive case for the two above. A guard that raised
    on everything would pass both refusal tests and make a genuinely quiet helpdesk an
    incident."""
    reader = Reader(Reply(status=200, body={"total": 0, "results": []}))

    reading = search_tickets(
        search_operation(), reader, query='"company_id:99999"', fetched_at=FETCHED_AT
    )

    assert reading.result.records == ()
    assert reading.is_all_of_them
    assert not reading.result.truncated


def test_absent_refused_and_unreachable_stay_three_different_answers() -> None:
    """`tests/invariants/test_cassettes.py` asserts the recordings keep the three
    distinguishable; this asserts the connector does. A naive one collapses all three into an
    empty list, and the reading a person takes from an empty list is 'there are none'."""
    absent = search_tickets(
        search_operation(),
        Reader(Reply(status=200, body={"total": 0, "results": []})),
        query='"company_id:1"',
        fetched_at=FETCHED_AT,
    )
    assert absent.result.records == ()

    with pytest.raises(FreshdeskRefusedError):
        assert_answered(Reply(status=401, body={"description": "Invalid credentials"}))

    with pytest.raises(FreshdeskUnreachableError):
        assert_answered(reply_of("FRESH-429"))


def test_the_sentence_a_person_is_shown_does_not_name_the_system_that_failed() -> None:
    """Naming it says a helpdesk exists and that we are connected to it, which is a fact
    obtainable by anybody who can type a question. The detail is for the trace, which is read
    by somebody already entitled to know what the system connects to."""
    unreachable = FreshdeskUnreachableError(
        "freshdesk answered 429", call_outcome=CallOutcome.QUOTA
    )

    assert unreachable.public_message == Degraded.public_message
    assert FRESHDESK not in unreachable.public_message
    assert FreshdeskRefusedError().public_message == unreachable.public_message
    assert FRESHDESK in unreachable.trace_line()


def test_an_unreachable_source_has_no_read_time_to_state() -> None:
    """UNSTATED rather than STALE, and the difference is `brain.gate.provenance`'s: we did
    not read anything, so there is no age. A caller able to treat this as merely dated is a
    caller who will substitute a previous figure and describe it as out of date rather than
    as unknown."""
    assert FreshdeskUnreachableError().freshness is Freshness.UNSTATED


def test_the_retry_hint_is_read_from_the_source_rather_than_guessed() -> None:
    """The recorded 429 states sixty seconds. Guessing instead means backing off wrongly in
    whichever direction the guess was, and guessing low burns what is left of the budget
    faster while looking like a well-behaved client."""
    assert retry_hint(reply_of("FRESH-429")) == 60.0


def test_a_retry_hint_is_read_whatever_case_the_header_arrives_in() -> None:
    """HTTP header names are case-insensitive and vendors change their casing between
    releases. Matching exactly means a connector that reads the hint today falls back to a
    guess after a vendor release, while believing it read the source's own number."""
    assert retry_hint(Reply(status=429, headers={"retry-after": "45"})) == 45.0


def test_a_rate_limit_with_no_hint_backs_off_at_the_long_end() -> None:
    """A 429 with nothing to read is a deviation from every recording in the corpus, and the
    safe direction is unambiguous: guessing low spends the remaining budget faster and
    produces another 429. Deleting this lets an absent header become a zero-second wait,
    which is a retry loop against a source that has just asked us to stop."""
    assert retry_hint(Reply(status=429)) == RETRY_AFTER_WHEN_UNSTATED
    assert retry_hint(
        Reply(status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    ) == (RETRY_AFTER_WHEN_UNSTATED)


def test_a_failure_is_recognised_before_its_body_is_projected() -> None:
    """A 429 carries a body of its own. Projecting first turns a rate limit into a complaint
    about the response shape, which sends whoever reads the error to the wrong module and
    hides the fact that the source asked us to wait."""
    operation = search_operation()

    with pytest.raises(FreshdeskUnreachableError):
        read_page(operation, Reader(reply_of("FRESH-429")), first_page(Endpoint.SEARCH_TICKETS))


# ------------------------------------------------------------------- never from memory
def test_no_fetch_here_can_be_handed_an_answer_it_did_not_fetch() -> None:
    """The structural half of 'never answer from memory'. A cache parameter added later reads
    as an optimisation in review, and the resulting answer is a ticket count from before the
    incident, quoted during the incident, with nothing marking it."""

    def fetch_with_a_cache(request: FetchRequest, cached: dict[str, str]) -> None: ...

    with pytest.raises(ConnectorContractError) as caught:
        assert_no_substitute_source(fetch_with_a_cache)

    assert "cached" in str(caught.value)


def test_the_search_fetch_satisfies_both_checks_it_is_built_under() -> None:
    """The positive case for the guard above and for the contract's own. A check that only
    ever refuses is satisfied by a module with no fetch in it, and the fetch this connector
    actually returns is the object a registry would call."""
    fetch = ticket_search_fetch(search_operation(), full_pages(1), fetched_at=FETCHED_AT)

    assert_fetches_only(fetch)
    assert_no_substitute_source(fetch)


def test_the_fetch_is_refused_an_entity_this_operation_does_not_map() -> None:
    """A fetch answering for the wrong entity returns records tagged as something they are
    not, and the redactor then looks up the wrong field policy for every one of them."""
    fetch = ticket_search_fetch(search_operation(), full_pages(1), fetched_at=FETCHED_AT)

    with pytest.raises(ConnectorContractError):
        fetch(FetchRequest(entity=CONTACT))


def test_a_cursor_is_refused_because_this_source_pages_by_number() -> None:
    """Ignoring it would answer with the first page, which is the page a caller resuming from
    a cursor is least likely to want and most likely to accept."""
    fetch = ticket_search_fetch(search_operation(), full_pages(1), fetched_at=FETCHED_AT)

    with pytest.raises(ConnectorContractError):
        fetch(FetchRequest(entity=TICKET, cursor="eyJvZmZzZXQiOjEwMH0"))


def test_a_callers_limit_stops_the_walk_and_says_the_answer_is_not_all_of_them() -> None:
    """A limit is a request rather than a guarantee, and stopping early is a different claim
    from hitting the ceiling: the caller knows they asked for fifty, and nobody knows whether
    the source had more. Both set `truncated`, and the two stay separable for whoever has to
    say which happened."""
    reading = search_tickets(
        search_operation(),
        full_pages(2),
        query='"company_id:447"',
        fetched_at=FETCHED_AT,
        limit=50,
    )

    assert len(reading.result.records) == 50
    assert reading.stopped_at_caller_limit
    assert reading.result.truncated
    assert not reading.hit_the_ceiling
    assert not reading.is_all_of_them


def test_a_negative_limit_is_refused_rather_than_quietly_trimming_the_answer() -> None:
    """The failure it prevents is silent, which is why it is worth a test of its own: a
    negative limit is truthy, so the walk would stop after one page and the trim would remove
    records from the end of it. The caller gets a short answer to a question they asked
    wrongly, and nothing says either thing happened."""
    with pytest.raises(ValueError, match="limit"):
        search_tickets(
            search_operation(),
            full_pages(1),
            query='"company_id:447"',
            fetched_at=FETCHED_AT,
            limit=-1,
        )


# --------------------------------------------------------------------- the projection
def test_a_ticket_projects_a_requester_id_and_never_a_requester_address() -> None:
    """The field somebody reaches for first. An address in the projection turns a
    permission bug from a leak of one record into a leak of every address we kept, which is
    the argument `brain.core.projection` makes for the denylist existing at all."""
    projected = projected_field_names()

    assert "requester_id" in projected
    assert "requester_email" not in projected
    assert is_forbidden("requester_email")


def test_every_field_this_connector_projects_passes_all_five_clauses() -> None:
    """The positive case, and the one that catches a field added later without an argument.
    Each of the five refuses for a different reason and names a different remedy, so a field
    that passes all five has been thought about five times."""
    labels = sum(1 for f in TICKET_FIELDS if f.shape.value == "label")
    for declared in TICKET_FIELDS:
        verdicts = clauses_for(
            declared,
            signal=ChangeSignal.UPDATED_SINCE,
            label_count=labels,
            field_count=len(TICKET_FIELDS),
        )
        assert failed_clauses(verdicts) == (), f"{declared.name} does not survive review"


def test_a_ticket_body_is_refused_by_a_different_rule_from_the_denylist() -> None:
    """The nuance a reader gets wrong. `description` is not on the permanent denylist, so
    somebody checking only that list concludes a ticket body may be projected. What refuses
    it is the pointer clause, because `subject` is already the entity's one label, and the
    hot clause, because nothing in the fast lane filters on a body."""
    assert not is_forbidden("description")

    with pytest.raises(ManifestError) as caught:
        ProjectedEntity(
            entity=TICKET,
            fields=(
                *TICKET_FIELDS,
                type(TICKET_FIELDS[-1])(name="description", shape=TICKET_FIELDS[-1].shape),
            ),
            change_signal=ChangeSignal.UPDATED_SINCE,
            visibility=VISIBILITY,
        )

    assert "description" in str(caught.value)


def test_a_contact_projects_nothing_at_all() -> None:
    """Not an oversight and not a to-do. The two fields anybody wants from a contact are its
    email and its phone, both permanently denylisted, so a contact projection would be a name
    and an id kept fresh for nobody. Deleting this invites somebody to add one 'for
    completeness'."""
    declared = a_manifest()

    assert declared.projection_for(CONTACT) is None
    assert declared.projection_for(TICKET) is not None


def test_a_contact_address_is_fetched_live_and_may_never_be_stored() -> None:
    """The tier distinction in one test. The contact mapping names an address because a
    federated read returns it for the question that needs it and never keeps it; a projection
    holding the same field is refused twice, at review and again at ingest."""
    contact = operation_for(Endpoint.GET_CONTACT, domain=DOMAIN)
    fetched = contact.project({"id": 9001, "name": "Wei Ling", "email": "wl@snm.example"})

    assert fetched[0]["email"] == "wl@snm.example"

    with pytest.raises(ProjectionRefusedError):
        ProjectedRecord(
            source=FRESHDESK,
            entity=CONTACT,
            source_id="9001",
            last_seen_at=NOW,
            fields={"email": "wl@snm.example"},
        )


def test_the_canary_in_the_recorded_ticket_never_survives_the_projection() -> None:
    """A permission canary rather than an ordinary assertion: it does not check that the
    right fields arrive, it checks that a field nobody mapped cannot. The mapping is an
    allowlist, so a custom field the client adds tomorrow is invisible until somebody
    classifies it, which is the correct direction for a field nobody has thought about."""
    recorded = cassette("FRESH-200-search")
    projected = search_operation().project(recorded.body)

    assert projected
    assert "CANARY-TICKET-B6YHF" not in str(projected)
    assert "custom_fields" not in projected[0]


def test_the_projection_stays_inside_the_twelve_field_cap_with_room_to_spare() -> None:
    """The cap is per entity kind and is what keeps the projection a pointer rather than a
    mirror. Asserted with headroom on purpose: a connector sitting exactly on the limit means
    the next field anybody needs is an argument about which one to drop."""
    assert len(TICKET_FIELDS) < MAX_PROJECTED_FIELDS
    assert check_projection(TICKET, dict.fromkeys(projected_field_names(), 1)) == []


def test_the_record_id_is_not_one_of_the_twelve() -> None:
    """`ProjectedRecord` already carries the source id, and `proj.record` is keyed by it.
    Declaring it as a projected field would spend one of twelve on a value the row has
    twice."""
    assert "id" not in projected_field_names()
    assert any(mapping.target == "id" for mapping in search_operation().transport.fields)


def test_a_fetched_ticket_becomes_a_projected_record() -> None:
    """The end-to-end positive case: what the mapping produces is what the projection
    accepts. Without it the two halves can drift, and the symptom is every record being
    refused at ingest with the projection quietly staying empty."""
    row = search_operation().project(a_search_body(1))[0]

    record = ProjectedRecord(
        source=FRESHDESK,
        entity=TICKET,
        source_id=str(row["id"]),
        last_seen_at=NOW,
        fields=projected_fields(row),
    )

    assert record.field_names == tuple(sorted(projected_field_names()))
    assert "requester_email" not in record.fields


def test_a_long_subject_is_cut_to_the_label_limit_rather_than_losing_the_record() -> None:
    """A label over 120 characters is refused by `check_projection`, so an uncut subject
    would drop the whole ticket at ingest and the projection would be missing precisely the
    noisiest ones. Deleting this makes that failure silent and selective."""
    long_subject = "x" * (MAX_LABEL_CHARS + 40)
    assert check_projection(TICKET, {"subject": long_subject}) != []

    fields = projected_fields(a_ticket(1, subject=long_subject))

    assert fields["subject"] == "x" * MAX_LABEL_CHARS
    assert check_projection(TICKET, dict(fields)) == []


def test_a_field_the_source_omitted_contributes_nothing_rather_than_a_null() -> None:
    """A vendor omitting an optional field has said something different from one sending an
    empty value, and a projection that invented a null would put a value nobody sent in front
    of a reader."""
    fields = projected_fields({"subject": "SSL renewal", "status": 2})

    assert set(fields) == {"subject", "status"}


def test_every_field_deliberately_not_projected_is_named_with_its_reason() -> None:
    """The list is documentation that fails. Without it, the next person to want a ticket
    body finds no record of the decision and reads the denylist, which does not mention
    one."""
    assert set(FETCHED_LIVE_INSTEAD) >= {"requester_email", "phone", "description"}
    for name, reason in FETCHED_LIVE_INSTEAD.items():
        assert reason.strip(), f"{name} is refused with no reason given"
        assert name not in projected_field_names()


# ----------------------------------------------------------------------- the manifest
def test_the_manifest_names_the_ceiling_the_limits_module_actually_verified() -> None:
    """`throttle.limits_for` looks the numbers up by `ceiling` and not by `name`, so a
    deployment installed under a client's own name with no ceiling named runs against no
    measured limit at all. That is the one mistake in this file that produces no error and
    costs somebody else's API budget."""
    declared = a_manifest()

    assert declared.ceiling == FRESHDESK
    assert ceiling_for(declared).per_minute == 100


def test_the_connector_is_read_only_and_names_nobody_who_granted_write() -> None:
    """Read-only is the default value of the field rather than a convention, so a connector
    installed by somebody in a hurry cannot write to a client's helpdesk."""
    declared = a_manifest()

    assert declared.credential.mode is AccessMode.READ_ONLY
    assert declared.credential.write_granted_by == ""


def test_the_scope_at_connect_names_one_helpdesk() -> None:
    """A scope naming nothing reaches everything the credential reaches, and narrowing it
    later does not un-fetch what was already read. This is the check that a copied
    configuration pointed at another client's account is refused."""
    declared = a_manifest()

    assert declared.scope.admits(DOMAIN)
    assert not declared.scope.admits("someone-else.freshdesk.com")


def test_a_scope_that_narrows_nothing_is_refused() -> None:
    """The refusal the test above depends on. Delete it and 'helpdesk: *' installs cleanly."""
    with pytest.raises(ConnectorContractError):
        a_manifest(domain="*")


def test_every_tool_declares_the_identity_it_actually_runs_under() -> None:
    """A shared API key means the source enforces nobody's permissions for us, so ours are
    the only ones there are. Declaring DELEGATED would claim a second independent check that
    does not exist, and the registry would stop insisting on a scope predicate."""
    declared = a_manifest()

    assert declared.tool_names() == (
        "freshdesk.read_contact",
        "freshdesk.read_ticket",
        "freshdesk.search_tickets",
    )
    for tool in declared.tools:
        assert tool.identity_mode is IdentityMode.SERVICE


def test_the_search_tool_tells_the_model_the_result_set_has_a_ceiling() -> None:
    """A description is what the model chooses on and is inside the pinned digest. A search
    tool described as returning matching tickets, with no mention of the ceiling, is one the
    model will use to answer 'how many' and cite."""
    described = next(t for t in a_manifest().tools if t.name == "freshdesk.search_tickets")

    assert str(FRESHDESK_SEARCH_MAX_RECORDS) in described.description
    assert "incomplete" in described.description


def test_the_projection_carries_a_visibility_predicate_the_deployment_chose() -> None:
    """A projection stored with no predicate has discarded the source's permission model
    rather than narrowed it, and every row is then visible to anybody holding the entity's
    capability. There is deliberately no default here to fall back to."""
    with pytest.raises(ManifestError):
        ticket_projection(visibility=Scope.unrestricted())

    assert ticket_projection(visibility=VISIBILITY).visibility == VISIBILITY


def test_the_subscription_declares_an_id_sweep_because_a_cursor_cannot_see_a_deletion() -> None:
    """A removed ticket is not 'updated': it is one the cursor never mentions again. Without
    an absence check it stays in the projection for good, is counted on, and reads as
    current."""
    subscribed = subscription(
        notify_within=timedelta(minutes=15), reconcile_every=timedelta(hours=6)
    )

    assert subscribed.kind is ChangeSignal.UPDATED_SINCE
    assert subscribed.deletion_check is DeletionCheck.ID_SWEEP
    assert subscribed.needs_an_absence_check
    assert subscribed.promise().interval == timedelta(hours=6)


def test_nothing_this_module_declares_holds_a_credential() -> None:
    """Checked over every declaration rather than the one somebody remembered, so a field
    named `api_key` added to any of them is refused. A connector holding a credential has a
    value no rotation can invalidate and no revocation can reach."""
    from brain.connectors import freshdesk

    declarations = [
        member
        for _, member in inspect.getmembers(freshdesk, inspect.isclass)
        if member.__module__ == freshdesk.__name__ and is_dataclass(member)
    ]

    assert len(declarations) >= 4
    for declared in declarations:
        assert_holds_no_credential(declared)


def test_every_endpoint_has_a_shape_and_a_mapping() -> None:
    """The table is total on purpose. A `dict.get` with a default would let a fifth endpoint
    be classified as whatever the default said, and the default for 'does the ceiling apply'
    is the answer that under-reports."""
    for endpoint in Endpoint:
        shape = shape_for(endpoint)
        assert shape.endpoint is endpoint
        assert operation_for(endpoint, domain=DOMAIN).transport.entity in (TICKET, CONTACT)

    assert shape_for(Endpoint.SEARCH_TICKETS).capped
    assert not shape_for(Endpoint.LIST_TICKETS).capped


def test_the_recordings_this_connector_is_built_against_still_exist() -> None:
    """Every test above is only as good as the corpus. If the Freshdesk recordings are
    renamed or dropped, the tests that name them fail with a lookup error that reads as a bug
    here rather than as the fixture having moved."""
    recorded = {c.cid for c in for_source(Source.FRESHDESK)}

    assert {"FRESH-200-search", "FRESH-429"} <= recorded
    assert cassette("FRESH-429").headers["Retry-After"] == "60"
