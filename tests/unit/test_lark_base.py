"""The Lark Base connector, driven by the recordings rather than by a tenant.

Five properties are pinned here, and each is a way this connector's subject goes wrong with
nothing failing.

**A hundred calls a minute belongs to the company, not to the asker.** The ceiling cannot be
raised, so one question that walks a large table to the end refuses every colleague for the
rest of the minute with nothing in their answer explaining why. Every test below that hands a
walk a budget is asserting that the budget is spent, is checked before each page, and produces
a partial answer that says so rather than a short one that reads as complete.

**A Base is a Base and not a sheet of cells.** Tables, typed fields, its own record ids. The
scope is one Base *and* one table because `ConnectorScope.admits` is exact membership, and a
record is named by its `record_id` because a row number is reused the moment somebody sorts a
view.

**A cell's type decides what it can become, and four types can become nothing honestly.** A
link, a lookup, an attachment and a person are refused at declaration; a formula is readable
live and never projected. The interesting half is that the refusal is by the source's type
rather than by our name for the field, because `brain.core.projection.is_forbidden` matches a
name and the name in a projected record is ours: `is_forbidden("contact_line")` is False, and
a phone column bound to that target would walk straight past the denylist.

**The visibility predicate is the deployment's and a Base's own sharing is not consulted.**
There is deliberately no default in the module to fall back to.

**Absent, refused and unreachable stay three different answers.** Lark makes this harder than
any other source here: it answers `code: 0` inside a 200 for success and a non-zero code inside
a 200 for failure, so a connector reading the HTTP status alone records a permission refusal as
an empty table, and an empty table reads as a fact about the company.

The fixture that matters is `tests/fixtures/cassettes.py`. `LARK-200-records` carries the real
page envelope, the millisecond timestamp and a canary in a column no binding names;
`LARK-200-code-permission` is the 200 carrying 91403, which is the exchange that cannot be
arranged on demand against a real tenant and is the one this connector is built around.

Task ids: M11.6.3
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.change_signal import DeletionCheck
from brain.connectors.contract import (
    CREDENTIAL_ATTRIBUTE_RE,
    AccessMode,
    ConnectorContractError,
    CredentialBinding,
    FetchRequest,
    HealthState,
    TransportKind,
    assert_fetches_only,
    assert_holds_no_credential,
)
from brain.connectors.federation import FailureReason
from brain.connectors.lark_base import (
    A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST,
    A_FORMULA_CHANGES_EVERY_ROW_WITH_NO_EVENT,
    CHANGE_SIGNAL,
    HEALTH_BY_OUTCOME,
    KIND_FACTS,
    LARK_BASE,
    PAGE_SIZE,
    PERMISSION_DENIED_CODE,
    RESERVED_TARGETS,
    TOTAL_UNSTATED,
    VIEW_FILTER,
    WAIT_WHEN_UNSTATED,
    Endpoint,
    FieldBinding,
    FieldKind,
    LarkBaseBudgetError,
    LarkBaseRefusedError,
    LarkBaseTable,
    LarkBaseUnreachableError,
    LarkReply,
    MinuteBudget,
    PageCursor,
    Representation,
    TableReading,
    arguments_for,
    assert_lark_answered,
    assert_reconciliation_is_affordable,
    business_code,
    ceiling,
    decode_row,
    decode_value,
    envelope_of,
    fair_share_budget,
    fair_share_per_minute,
    first_cursor,
    health,
    kind_facts,
    manifest,
    read_page,
    read_record,
    read_records,
    records_fetch,
    spec_for,
    subscription,
    sweep_cost,
    wait_seconds,
)
from brain.connectors.manifest import (
    ChangeSignal,
    FieldShape,
    HotUse,
    ManifestError,
    ProjectedEntity,
    failed_clauses,
)
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
from brain.ops.limits import MINUTE_SECONDS, connector_ceiling, principal_share_of
from brain.ops.secrets import SecretRef, VaultRole
from tests.fixtures.cassettes import CASSETTES, Cassette, Source, for_source, limit_for

#: The Base and the table one deployment is connected to. The Base identifier is the string
#: that appears in the address bar of anybody who opens the document, which is the whole of
#: `A_BASE_ID_IS_NOT_A_CREDENTIAL`.
BASE_ID = "bascnCMII2ORej2RItqpZZUNMIe"
TABLE_ID = "tblsRc9GRRXKqhvW"
SIBLING_TABLE_ID = "tblQ4xZ7Kp2mNvBc"
VIEW_ID = "vewTpR1urY"
HOST = "open.larksuite.com"
ENTITY = "maintenance"

FETCHED_AT = "2026-09-06T09:00:00+00:00"
NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: The predicate a deployment supplies. Maintenance rows follow the department that owns the
#: client, which enumerates teams rather than people, so it is re-evaluated against the live
#: entitlement set on every question rather than frozen into the projection.
VISIBILITY = Scope(clauses=(Clause(field="department", op=Op.IN, value=("web", "creative")),))

READ_REF = SecretRef(path="connectors/lark_base/bot", role=VaultRole.APPLICATION)

#: What this deployment reads out of the table. Four projected columns and one read live, and
#: `Contract Value` is deliberately bound by nothing: it is the column the recording carries a
#: canary in, so the canary is what proves a column no binding names arrives nowhere.
BINDINGS = (
    FieldBinding(
        target="client",
        base_field="Client",
        kind=FieldKind.TEXT,
        uses=(HotUse.JOIN,),
        shape=FieldShape.JOIN_KEY,
    ),
    FieldBinding(
        target="title",
        base_field="Title",
        kind=FieldKind.TEXT,
        uses=(HotUse.IDENTIFY,),
        shape=FieldShape.LABEL,
    ),
    FieldBinding(
        target="status",
        base_field="Status",
        kind=FieldKind.SINGLE_SELECT,
        uses=(HotUse.FILTER,),
        shape=FieldShape.STATUS,
    ),
    FieldBinding(
        target="renewal",
        base_field="Renewal",
        kind=FieldKind.DATE,
        uses=(HotUse.FILTER, HotUse.SORT),
        shape=FieldShape.TIMESTAMP,
    ),
    FieldBinding(
        target="hours_remaining",
        base_field="Hours Remaining",
        kind=FieldKind.NUMBER,
    ),
)

#: The Base types that hold something which is not a value, and the multi-select that is
#: several values wearing one name. Named here so a kind added to the enum without a decision
#: shows up as a totality failure rather than as a silently readable column.
UNREADABLE_KINDS = (
    FieldKind.LINK,
    FieldKind.LOOKUP,
    FieldKind.ATTACHMENT,
    FieldKind.PERSON,
    FieldKind.LOCATION,
    FieldKind.URL,
    FieldKind.MULTI_SELECT,
)


def a_table(**overrides: Any) -> LarkBaseTable:
    settings: dict[str, Any] = {
        "base_id": BASE_ID,
        "table_id": TABLE_ID,
        "entity": ENTITY,
        "bindings": BINDINGS,
    }
    settings.update(overrides)
    return LarkBaseTable(**settings)


def a_manifest(**overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "table": a_table(),
        "host": HOST,
        "credential": CredentialBinding(ref=READ_REF),
        "visibility": VISIBILITY,
    }
    settings.update(overrides)
    table = settings.pop("table")
    return manifest(table, **settings)


def list_operation(table: LarkBaseTable | None = None) -> Any:
    return (table or a_table()).operation(Endpoint.LIST_RECORDS, host=HOST)


def single_operation(table: LarkBaseTable | None = None) -> Any:
    return (table or a_table()).operation(Endpoint.GET_RECORD, host=HOST)


def cassette(cid: str) -> Cassette:
    """One recording by id, so a test names what it is driven by."""
    return next(c for c in CASSETTES if c.cid == cid)


def reply_of(cid: str) -> LarkReply:
    """A recording as the value a reader hands back.

    Field for field with no translation: `LarkReply` carries the same three things a
    `Cassette` records precisely so this function cannot quietly disagree with the recording.
    """
    recorded = cassette(cid)
    return LarkReply(status=recorded.status, headers=recorded.headers, body=recorded.body)


def a_record(number: int, **cells: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "Client": "SNM Construction Pte Ltd",
        "Title": f"Maintenance {number}",
        "Status": "Active",
        "Renewal": 1_794_700_800_000,
        "Hours Remaining": 12,
        "Contract Value": "CANARY-CONTRACT-7Q4XZ",
    }
    fields.update(cells)
    return {"record_id": f"recSNM{number:05d}", "fields": fields}


def a_page(
    *records: dict[str, Any],
    has_more: bool = False,
    page_token: str = "",
    total: int = TOTAL_UNSTATED,
) -> dict[str, Any]:
    """One page body, in the envelope the recording verifies."""
    data: dict[str, Any] = {"has_more": has_more, "items": list(records)}
    if page_token:
        data["page_token"] = page_token
    if total >= 0:
        data["total"] = total
    return {"code": 0, "data": data}


def a_reply(*records: dict[str, Any], **envelope: Any) -> LarkReply:
    return LarkReply(status=200, body=a_page(*records, **envelope))


@dataclass
class Reader:
    """A reader scripted with one reply per call, recording the cursor it was handed.

    A fake rather than a mock: the interesting assertions are about which pages were asked for
    and which were not, and only a reader that records can show the page that was never asked
    for.

    Strict about an unscripted call, deliberately. A reader that answered every call with the
    last reply would turn a walk that failed to stop into an endless loop, and a test that
    hangs is a test somebody deletes.
    """

    replies: list[LarkReply]
    seen: list[PageCursor]

    def __init__(self, *replies: LarkReply) -> None:
        self.replies = list(replies)
        self.seen = []

    def read(self, cursor: PageCursor) -> LarkReply:
        self.seen.append(cursor)
        if len(self.seen) > len(self.replies):
            raise AssertionError(
                f"call {len(self.seen)} was made and this reader holds {len(self.replies)} "
                "replies; the walk did not stop where it should have"
            )
        return self.replies[len(self.seen) - 1]


def endless_pages(count: int) -> Reader:
    """A reader whose every page claims there is another, for as many calls as asked."""
    return Reader(
        *[
            a_reply(a_record(n), has_more=True, page_token=f"tok-{n}", total=412)
            for n in range(count)
        ]
    )


# ------------------------------------------------------- the minute belongs to the company
def test_one_question_is_budgeted_a_share_of_the_minute_rather_than_the_minute() -> None:
    """The property the whole module is arranged around. Delete this and a budget sized at the
    ceiling installs cleanly, one person's question spends all hundred calls, and every
    colleague asking anything in the following sixty seconds is refused with nothing in their
    answer explaining why."""
    share = fair_share_per_minute()

    assert share == principal_share_of(ceiling().per_minute)
    assert 0 < share < ceiling().per_minute
    assert fair_share_budget() == MinuteBudget(allowance=share)


def test_the_ceiling_is_looked_up_rather_than_restated_and_cannot_be_raised() -> None:
    """A module that stated 100 itself would be a second figure to keep true, and the copy
    that drifts is the one a budget is sized from. The raisability matters as much as the
    number: an operator reading a console row must not go looking for an upgrade button that
    does not exist."""
    assert ceiling() == connector_ceiling(LARK_BASE)
    assert ceiling().per_minute == 100
    assert ceiling().raisable is False
    assert limit_for(Source.LARK_BASE).calls == 100
    assert limit_for(Source.LARK_BASE).raisable is False


def test_a_question_may_not_be_budgeted_the_whole_tenants_minute() -> None:
    """The bound that makes the constant beside it true rather than aspirational. A budget
    equal to the ceiling is exactly one question allowed to spend the company's minute, and
    without this refusal the only thing standing between that and production is whoever
    remembered to call `fair_share_budget`."""
    with pytest.raises(ConnectorContractError) as caught:
        MinuteBudget(allowance=ceiling().per_minute)

    assert str(ceiling().per_minute) in str(caught.value)
    assert MinuteBudget(allowance=ceiling().per_minute - 1).allowance == 99


def test_a_budget_that_cannot_fetch_a_first_page_is_refused() -> None:
    """A zero allowance is not a cautious budget, it is a question that reports an empty table
    it never read. That answer is indistinguishable from a table which is genuinely empty,
    which is the one failure nobody files a bug about."""
    with pytest.raises(ConnectorContractError):
        MinuteBudget(allowance=0)

    with pytest.raises(ConnectorContractError):
        MinuteBudget(allowance=5, spent=-1)


def test_a_call_past_an_exhausted_budget_raises_rather_than_returning_a_flag() -> None:
    """A flag is checked by the caller who remembered to, and a call made past an exhausted
    budget has already taken somebody else's allowance by the time anybody reads it. The spend
    also has to return a new budget rather than mutate this one, or a branch that spent three
    calls is invisible to the branch beside it."""
    budget = MinuteBudget(allowance=2)

    once = budget.spend()
    twice = once.spend()

    assert (budget.spent, once.spent, twice.spent) == (0, 1, 2)
    assert twice.is_exhausted
    assert twice.remaining == 0
    with pytest.raises(LarkBaseBudgetError):
        twice.spend()


def test_a_walk_stops_when_its_share_runs_out_and_says_the_table_had_more() -> None:
    """The behaviour the ceiling forces. Without it a walk pages until Lark says there is no
    more, which on a large table is the whole tenant's minute spent on one question. What is
    returned has to be the records actually read plus the statement that there are more, never
    a short answer that reads as complete."""
    reader = endless_pages(3)

    reading = read_records(
        a_table(),
        list_operation(),
        reader,
        fetched_at=FETCHED_AT,
        budget=MinuteBudget(allowance=3),
    )

    assert reading.pages_read == 3
    assert len(reader.seen) == 3
    assert reading.budget.is_exhausted
    assert reading.stopped_for_budget
    assert reading.more_at_source
    assert not reading.is_all_of_them
    assert reading.result.truncated
    assert reading.resume_from == "tok-2"


def test_a_budget_stop_is_reported_as_a_quota_failure_rather_than_a_truncation() -> None:
    """Truncation is a source refusing to return more, which has a different remedy: waiting
    does not help and nobody should be told to ask Lark for anything. We stopped to protect a
    shared allowance, so the reason recorded is ours. Delete this and a partial answer sends
    whoever reads the trace to the vendor."""
    reading = read_records(
        a_table(),
        list_operation(),
        endless_pages(2),
        fetched_at=FETCHED_AT,
        budget=MinuteBudget(allowance=2),
    )

    partial = reading.partial()

    assert not partial.is_complete
    assert [f.reason for f in partial.failed] == [FailureReason.QUOTA]
    assert [f.connector for f in partial.failed] == [LARK_BASE]
    assert LARK_BASE in reading.trace_line()


def test_a_walk_that_read_the_whole_table_reports_no_failure_at_all() -> None:
    """The positive case, and it is not decoration. A connector that marked every reading
    partial would pass the two tests above and make every answer hedge, which trains a reader
    to ignore the sentence that eventually matters."""
    reader = Reader(
        a_reply(a_record(1), has_more=True, page_token="tok-1", total=2),
        a_reply(a_record(2), has_more=False, total=2),
    )

    reading = read_records(
        a_table(), list_operation(), reader, fetched_at=FETCHED_AT, budget=fair_share_budget()
    )

    assert reading.pages_read == 2
    assert len(reading.result.records) == 2
    assert reading.is_all_of_them
    assert not reading.result.truncated
    assert not reading.stopped_for_budget
    assert reading.resume_from == ""
    assert reading.partial().is_complete
    assert reading.partial().notice(disclosable=frozenset({LARK_BASE})) == ""


def test_a_budget_already_spent_before_the_first_page_is_refused_not_answered_empty() -> None:
    """The case the module is arranged around. Returning an empty result here would report our
    own arithmetic as a fact about the company's data, and an empty table is the one answer
    nobody can tell apart from the truth.

    The reader is asserted to have been left alone as well as the error being raised, because
    the two halves fail differently: a walk that raised after reading a page would have spent
    somebody else's call to produce an exception."""
    reader = Reader()

    with pytest.raises(LarkBaseBudgetError):
        read_records(
            a_table(),
            list_operation(),
            reader,
            fetched_at=FETCHED_AT,
            budget=MinuteBudget(allowance=1, spent=1),
        )

    assert reader.seen == []


def test_each_page_is_asked_for_with_the_token_the_page_before_it_named() -> None:
    """Mutation testing found this one too, and it is the failure `envelope_of` refuses a
    tokenless page to prevent, arriving from the other side: a walk that does not advance its
    own cursor re-reads page one until the budget is gone, returns the same records several
    times, and looks entirely plausible doing it, because the records are real and the count is
    a number nobody can check. Asserted on the cursors the reader was handed rather than on the
    result, because the result of reading page one three times and of reading three pages is
    the same shape."""
    reader = Reader(
        a_reply(a_record(1), has_more=True, page_token="tok-1"),
        a_reply(a_record(2), has_more=True, page_token="tok-2"),
        a_reply(a_record(3), has_more=False),
    )

    reading = read_records(
        a_table(), list_operation(), reader, fetched_at=FETCHED_AT, budget=fair_share_budget()
    )

    assert [cursor.continuation for cursor in reader.seen] == ["", "tok-1", "tok-2"]
    assert len(reading.result.records) == 3
    assert reading.is_all_of_them


def test_a_walk_resumed_from_a_token_continues_rather_than_reading_page_one_again() -> None:
    """The only way a large table is ever read at all under this ceiling: a question that ran
    out of its share is continued by the next one. Without this the resume token is decoration
    and every attempt re-reads the first page, spending the budget to return records the caller
    already has."""
    reader = Reader(a_reply(a_record(7), has_more=False))

    reading = read_records(
        a_table(),
        list_operation(),
        reader,
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
        resume_from="tok-2",
    )

    assert reader.seen[0].continuation == "tok-2"
    assert reading.is_all_of_them


def test_a_reconciliation_sweep_is_costed_at_a_share_of_the_ceiling_and_never_all_of_it() -> None:
    """A sweep is the largest single consumer this connector has. Costed at the whole ceiling
    it looks four times faster than it is, and every question asked while it runs is refused.
    Deleting this lets somebody agree an interval against arithmetic that assumed the company
    stopped asking questions."""
    cost = sweep_cost(412)

    assert cost.calls == 5
    assert cost.calls_per_minute == fair_share_per_minute()
    assert cost.duration == timedelta(minutes=5 / fair_share_per_minute())
    assert sweep_cost(0).calls == 1

    with pytest.raises(ValueError, match="negative"):
        sweep_cost(-1)


def test_a_reconciliation_interval_the_table_cannot_be_swept_inside_is_refused() -> None:
    """A pass that cannot finish inside its own interval never finishes, and the symptom is a
    projection that reads as reconciled while deletions accumulate in it for ever. The floor in
    `brain.connectors.change_signal` cannot catch this: it knows how often a pass is owed and
    not how long one takes at a hundred calls a minute."""
    hourly = subscription(
        a_table(), notify_within=timedelta(minutes=15), reconcile_every=timedelta(hours=1)
    )

    assert_reconciliation_is_affordable(hourly, record_count=100_000)

    with pytest.raises(ConnectorContractError) as caught:
        assert_reconciliation_is_affordable(hourly, record_count=1_000_000)

    assert "1000000" in str(caught.value)


# ----------------------------------------------------- a Base is a Base, not a spreadsheet
def test_a_connection_is_scoped_to_one_table_and_not_to_the_base_that_holds_it() -> None:
    """A scope naming only the Base reaches every table in it, and `ConnectorScope.admits` is
    exact membership rather than a prefix, so the joined selector cannot be satisfied by a
    sibling table. Delete this and a connector installed against the maintenance table answers
    out of the salary table in the same Base."""
    table = a_table()
    scope = table.scope()

    assert table.selector == f"{BASE_ID}/{TABLE_ID}"
    assert scope.resource_kind == "base_table"
    assert scope.admits(table.selector)
    assert not scope.admits(BASE_ID)
    assert not scope.admits(f"{BASE_ID}/{SIBLING_TABLE_ID}")


def test_an_identifier_the_source_would_not_recognise_is_refused_at_connect() -> None:
    """The copied-configuration mistake, which is invisible afterwards: a Base id pasted where
    a table id belongs builds an address that names something real and returns somebody else's
    records, or nothing, and neither reads as a misconfiguration."""
    with pytest.raises(ConnectorContractError):
        a_table(table_id=BASE_ID)

    with pytest.raises(ConnectorContractError):
        a_table(base_id="tbl-not-a-base")

    with pytest.raises(ConnectorContractError):
        a_table(entity="Maintenance Hours")


def test_a_record_is_named_by_its_record_id_and_never_by_its_position() -> None:
    """A row number is reused the moment somebody sorts a view, so a record identified by
    position cannot be refreshed, cited, or matched to itself on the next pass. A row the
    source sent without an id is dropped rather than given a generated one, which is the same
    refusal `normalise` makes one layer up."""
    table = a_table()
    row = list_operation(table).project(a_page(a_record(1)))[0]

    assert row["id"] == "recSNM00001"
    assert table.projected_record(row, last_seen_at=NOW).source_id == "recSNM00001"

    with pytest.raises(ConnectorContractError):
        table.projected_record({"fields": {"Client": "SNM"}}, last_seen_at=NOW)


def test_the_base_identifier_is_never_held_under_the_vendors_word_for_it() -> None:
    """Lark calls it an app token and it is a document id in an address bar, not a credential.
    `contract.CREDENTIAL_ATTRIBUTE_RE` matches an attribute ending that way and would refuse
    the class outright, and it is right to match by name. Delete this and somebody renames
    `base_id` back to the vendor's word, which fails at import rather than in review."""

    @dataclass(frozen=True)
    class VendorSpelling:
        app_token: str

    with pytest.raises(ConnectorContractError):
        assert_holds_no_credential(VendorSpelling)

    assert_holds_no_credential(LarkBaseTable)
    assert a_table().path_arguments() == {"app_token": BASE_ID, "table_id": TABLE_ID}


def test_nothing_this_module_declares_holds_a_credential() -> None:
    """Checked over every declaration rather than the one somebody remembered, so a field
    named `api_key` added to any of them is refused. A connector holding a credential has a
    value no rotation can invalidate and no revocation can reach."""
    from brain.connectors import lark_base

    declarations = [
        member
        for _, member in inspect.getmembers(lark_base, inspect.isclass)
        if member.__module__ == lark_base.__name__ and is_dataclass(member)
    ]

    assert len(declarations) >= 6
    for declared in declarations:
        assert_holds_no_credential(declared)


def test_no_identifier_in_this_module_is_spelled_the_way_lark_spells_it() -> None:
    """**The rule above collides with Lark's vocabulary, and this is where the collision is
    settled.** `contract.CREDENTIAL_ATTRIBUTE_RE` refuses any attribute whose name ends in
    `_token`, crudely and on purpose, because a stored credential is nearly always a plain
    string. Lark calls a Base's identifier an app token and a listing's cursor a page token,
    and neither is a credential: one is in an address bar and the other is an opaque marker.
    On a declaration, though, both are indistinguishable from the thing the guard exists to
    catch.

    The two ways out are exempting this module from the guard or not naming an identifier with
    a credential word, and only the second leaves the guard working everywhere. Delete this and
    the next person to align an attribute with the vendor's payload reopens the exemption,
    which is how a real credential attribute eventually gets through. The vendor's words stay
    where they belong, which is the URL and the decoded body."""
    from brain.connectors import lark_base

    offending = sorted(
        f"{member.__name__}.{attribute}"
        for _, member in inspect.getmembers(lark_base, inspect.isclass)
        if member.__module__ == lark_base.__name__ and is_dataclass(member)
        for attribute in getattr(member, "__annotations__", {})
        if CREDENTIAL_ATTRIBUTE_RE.search(attribute.casefold())
    )

    assert offending == []
    assert CREDENTIAL_ATTRIBUTE_RE.search("page_token")
    assert CREDENTIAL_ATTRIBUTE_RE.search("app_token")
    assert "page_token" in first_cursor(continuation="tok-1").query_arguments()
    assert "app_token" in spec_for(Endpoint.LIST_RECORDS).path


def test_a_column_no_binding_names_arrives_nowhere() -> None:
    """A permission canary rather than an ordinary assertion: it does not check that the right
    cells arrive, it checks that a cell nobody declared cannot. `decode_row` builds a fresh
    dictionary from the declared bindings, so a column the client adds tomorrow is invisible
    until somebody classifies it, which is the correct direction for a field nobody has thought
    about. A copy of the cell container with unwanted keys removed reads identically on the day
    it is written and carries the new column the day after."""
    recorded = cassette("LARK-200-records")
    rows = list_operation().project(recorded.body)
    decoded = decode_row(BINDINGS, rows[0])

    assert "CANARY-CONTRACT-7Q4XZ" in str(recorded.body)
    assert "CANARY-CONTRACT-7Q4XZ" not in str(decoded)
    assert "Contract Value" not in decoded
    assert decoded["client"] == "SNM Construction Pte Ltd"


def test_a_base_column_is_matched_by_its_human_label_rather_than_renamed_to_fit_a_path() -> None:
    """The reason a binding is not a `transports.FieldMapping`. A Base column is authored by a
    person and is called `Hours Remaining`; `transports._SOURCE_PATH_RE` admits identifiers
    only, so a mapping that renamed it would read a different column from the one the author
    named. Delete this and somebody folds the bindings into the REST mapping and the connector
    silently reads nothing."""
    from brain.connectors.transports import FieldMapping, TransportError

    with pytest.raises(TransportError):
        FieldMapping(target="hours_remaining", source_path="Hours Remaining")

    decoded = decode_row(BINDINGS, a_record(1))
    assert decoded["hours_remaining"] == 12

    mapped = {mapping.target for mapping in a_table().transport().fields}
    assert mapped == {"id", "fields"}


def test_a_target_the_record_envelope_already_owns_is_refused() -> None:
    """A binding writing to `id` or `entity` is discarded by `normalise` rather than stored, so
    the field reads in a manifest as one being kept and is silently absent from every record.
    Refused at declaration, which is the only place anybody would see it."""
    for reserved in sorted(RESERVED_TARGETS):
        with pytest.raises(ConnectorContractError):
            FieldBinding(target=reserved, base_field="Client", kind=FieldKind.TEXT)


def test_one_column_read_twice_or_one_target_written_twice_is_refused() -> None:
    """Refused rather than deduplicated, because deduplicating picks one silently and the one
    it picks decides whether the field is a label, which decides whether the projection is
    legal at all."""
    twice_written = (
        FieldBinding(target="client", base_field="Client", kind=FieldKind.TEXT),
        FieldBinding(target="client", base_field="Customer", kind=FieldKind.TEXT),
    )
    twice_read = (
        FieldBinding(target="client", base_field="Client", kind=FieldKind.TEXT),
        FieldBinding(target="customer", base_field="Client", kind=FieldKind.TEXT),
    )

    with pytest.raises(ConnectorContractError):
        a_table(bindings=twice_written)

    with pytest.raises(ConnectorContractError):
        a_table(bindings=twice_read)

    with pytest.raises(ConnectorContractError):
        a_table(bindings=())


# ------------------------------------------- what a typed Base cell may and may not become
def test_a_column_holding_something_that_is_not_a_value_may_not_be_bound_at_all() -> None:
    """The refusal happens at declaration, where the author can name a different column and
    somebody reviews the choice. At ingest a `str()` of a list of record ids becomes a string
    that reads like data, sorts wrongly, joins to nothing, and cannot be told apart from a real
    value by anybody reading the answer."""
    for kind in UNREADABLE_KINDS:
        assert not kind_facts(kind).may_be_read, f"{kind} would be read as one value"
        with pytest.raises(ConnectorContractError) as caught:
            FieldBinding(target="whatever", base_field="Whatever", kind=kind)
        assert str(kind) in str(caught.value)


def test_a_link_and_a_lookup_point_at_a_table_this_connector_is_not_scoped_to() -> None:
    """The reason those two in particular cannot be flattened, stated as a property rather
    than left in a comment. Their value is a list of record ids in a table nobody connected, so
    the string it renders to joins to nothing; a lookup is worse, because its type is the far
    field's type and can be an attachment without this table saying so."""
    for kind in (FieldKind.LINK, FieldKind.LOOKUP, FieldKind.ATTACHMENT):
        assert kind_facts(kind).representation is Representation.ELSEWHERE
        assert not kind_facts(kind).may_be_projected


def test_a_formula_may_be_read_live_and_may_never_be_projected() -> None:
    """The interesting middle case. A formula is a scalar on the wire, so nothing about the
    value says it must not be stored; what says so is that the Base recomputes it on every
    read, so an editor rewriting the formula changes every row at once with no per-record
    change event anywhere. A projected formula is then quoted as current for ever with nothing
    reporting that it stopped being true."""
    live = FieldBinding(
        target="renewal_window", base_field="Renewal Window", kind=FieldKind.FORMULA
    )

    assert kind_facts(FieldKind.FORMULA).representation is Representation.RECOMPUTED
    assert not live.is_projected
    assert decode_value(live, 42) == 42

    with pytest.raises(ConnectorContractError) as caught:
        FieldBinding(
            target="renewal_window",
            base_field="Renewal Window",
            kind=FieldKind.FORMULA,
            uses=(HotUse.FILTER,),
            shape=FieldShape.STATUS,
        )

    assert A_FORMULA_CHANGES_EVERY_ROW_WITH_NO_EVENT in str(caught.value)


def test_a_phone_column_projects_nothing_however_the_binding_is_named() -> None:
    """The gap this connector closes, and it is the sharpest one in the file.
    `brain.core.projection.is_forbidden` matches a field *name*, and the name in a projected
    record is ours rather than Lark's: `contact_line` is not on the denylist and never will be.
    So a Base column called `Mobile` bound to that target walks straight past the permanent
    denylist. The defence is to refuse by the source's own type, which the author does not
    choose."""
    assert is_forbidden("mobile")
    assert not is_forbidden("contact_line")

    with pytest.raises(ConnectorContractError) as caught:
        FieldBinding(
            target="contact_line",
            base_field="Mobile",
            kind=FieldKind.PHONE,
            uses=(HotUse.FILTER,),
            shape=FieldShape.STATUS,
        )

    assert A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST in str(caught.value)

    live = FieldBinding(target="contact_line", base_field="Mobile", kind=FieldKind.PHONE)
    assert decode_value(live, "+65 6100 0000") == "+65 6100 0000"


def test_a_binding_that_asks_to_be_projected_says_which_pointer_shape_it_is() -> None:
    """A text column is a label in one table and a client code in another, and which it is
    here is the author's declaration rather than something a type table can know. Both
    directions are refused: asking to project without saying what the field is, and saying what
    it is without asking to project."""
    with pytest.raises(ConnectorContractError):
        FieldBinding(target="client", base_field="Client", kind=FieldKind.TEXT, uses=(HotUse.JOIN,))

    with pytest.raises(ConnectorContractError):
        FieldBinding(
            target="client", base_field="Client", kind=FieldKind.TEXT, shape=FieldShape.LABEL
        )


def test_a_shape_the_kind_cannot_hold_is_refused() -> None:
    """A pointer that points at the wrong kind of thing. A date declared as a status enum is
    filtered and grouped on as though it had a closed set of values, and every answer built on
    it is wrong in a way that reads as a data problem rather than a declaration problem."""
    with pytest.raises(ConnectorContractError) as caught:
        FieldBinding(
            target="renewal",
            base_field="Renewal",
            kind=FieldKind.DATE,
            uses=(HotUse.FILTER,),
            shape=FieldShape.STATUS,
        )

    assert FieldShape.TIMESTAMP.value in str(caught.value)


def test_a_number_is_read_live_because_a_measure_is_not_a_pointer() -> None:
    """The deliberate omission somebody will try to fix. None of the five pointer shapes
    describes a measure, so a projected number is the value the question is about sitting in a
    local table with a change signal that will eventually stop. The remedy is a single-select
    or a local field, not a wider projection."""
    assert kind_facts(FieldKind.NUMBER).shapes == ()
    assert kind_facts(FieldKind.NUMBER).may_be_read

    with pytest.raises(ConnectorContractError):
        FieldBinding(
            target="hours_remaining",
            base_field="Hours Remaining",
            kind=FieldKind.NUMBER,
            uses=(HotUse.COUNT,),
            shape=FieldShape.STATUS,
        )


def test_every_field_kind_the_enum_names_is_classified_exactly_once() -> None:
    """The table is total on purpose. A `dict.get` with a default would let a type be added and
    silently classified as whatever the default said, and for a question like 'can this be
    stored' the convenient default is the one that stores an attachment token. The api types
    are checked for collisions too: two kinds sharing Lark's number is a copy-paste that makes
    an operator's cross-check against the Base's field list wrong."""
    assert set(KIND_FACTS) == set(FieldKind)

    for kind, facts in KIND_FACTS.items():
        assert facts.kind is kind
        assert facts.note.strip(), f"{kind} is classified with no reason given"
        assert facts.may_be_projected is bool(facts.shapes)
        if facts.shapes:
            assert facts.may_be_read, f"{kind} may be stored and may not be read"

    api_types = [facts.api_type for facts in KIND_FACTS.values()]
    assert len(set(api_types)) == len(api_types)


def test_a_cell_that_is_not_what_the_binding_declared_is_refused_rather_than_coerced() -> None:
    """Refused in both directions, because both produce a value that looks right after a
    `str()`. A rich-text column returns a list of segments where a string was declared and
    joining it invents a value; a number arriving as text is the same problem wearing quotes,
    and parsing it here would be this module deciding what a number written as text means,
    which is the source's decision."""
    text = BINDINGS[0]
    number = BINDINGS[4]

    with pytest.raises(ConnectorContractError):
        decode_value(text, [{"text": "SNM", "type": "text"}])

    with pytest.raises(ConnectorContractError):
        decode_value(number, "12")

    with pytest.raises(ConnectorContractError):
        decode_value(number, True)


def test_a_checkbox_is_a_boolean_and_never_a_truthiness_test() -> None:
    """The string 'false' is true to Python, so a checkbox read by truthiness reports every
    unchecked row as checked the moment the vendor changes how it serialises one."""
    checkbox = FieldBinding(target="renewed", base_field="Renewed", kind=FieldKind.CHECKBOX)

    assert decode_value(checkbox, False) is False

    with pytest.raises(ConnectorContractError):
        decode_value(checkbox, "false")


def test_a_lark_instant_is_milliseconds_and_arrives_with_a_timezone_on_it() -> None:
    """Lark states an instant as milliseconds since the epoch and never as ISO, so a connector
    assuming ISO parses garbage without erroring. The zone matters as much: `ProjectedRecord`
    refuses a naive timestamp because Singapore reads a naive UTC instant as eight hours old,
    which is the whole width of the ageing band."""
    renewal = BINDINGS[3]

    decoded = decode_value(renewal, 1_794_700_800_000)

    assert isinstance(decoded, datetime)
    assert decoded.tzinfo is not None
    assert decoded == datetime(2026, 11, 15, tzinfo=UTC)

    with pytest.raises(ConnectorContractError):
        decode_value(renewal, "2026-11-15T00:00:00Z")


def test_an_empty_cell_stays_empty_and_a_missing_one_contributes_nothing() -> None:
    """Two different facts that a helpful connector collapses. A Base cell holding null has
    said something different from a Base cell holding a zero, and a Base omitting a cell has
    said something different again from one sending an empty value. Inventing either puts a
    figure nobody sent in front of a reader."""
    assert decode_value(BINDINGS[4], None) is None

    decoded = decode_row(BINDINGS, {"fields": {"Client": "SNM", "Hours Remaining": None}})

    assert decoded == {"client": "SNM", "hours_remaining": None}
    assert "renewal" not in decoded


def test_a_record_carrying_no_cell_container_is_not_a_Base_record() -> None:  # noqa: N802
    """A Base record is a record id and a container of typed cells. A reply whose records are
    bare objects is a different shape from the one the specification describes, and reading it
    as a record with every cell absent turns a shape change into a table of empty rows."""
    with pytest.raises(ConnectorContractError):
        decode_row(BINDINGS, {"record_id": "recSNM00001"})


# --------------------------------------------------------------------------- the projection
def test_every_field_this_connector_projects_passes_all_five_clauses() -> None:
    """The positive case, and the one that catches a field added later without an argument.
    Each of the five refuses for a different reason and names a different remedy, so a field
    that passes all five has been thought about five times."""
    projected = tuple(b.as_projected_field() for b in a_table().projected_bindings())
    labels = sum(1 for f in projected if f.shape is FieldShape.LABEL)

    assert labels == 1
    for declared in projected:
        verdicts = clauses_for(
            declared,
            signal=CHANGE_SIGNAL,
            label_count=labels,
            field_count=len(projected),
        )
        assert failed_clauses(verdicts) == (), f"{declared.name} does not survive review"


def test_the_projection_stays_inside_the_twelve_field_cap_with_room_to_spare() -> None:
    """The cap is per entity kind and is what keeps the projection a pointer rather than a
    mirror. Asserted with headroom on purpose: a connector sitting exactly on the limit means
    the next field anybody needs is an argument about which one to drop."""
    names = [f.name for f in a_table().projection(visibility=VISIBILITY).fields]

    assert len(names) < MAX_PROJECTED_FIELDS
    assert check_projection(ENTITY, dict.fromkeys(names, 1)) == []
    assert "id" not in names
    assert "hours_remaining" not in names


def test_a_long_label_is_cut_to_the_limit_rather_than_losing_the_record() -> None:
    """A label over 120 characters is refused by `check_projection`, so an uncut title would
    drop the whole record at ingest and the projection would be missing precisely the noisiest
    rows. A marker would be worse: it makes a title that genuinely ends in an ellipsis
    indistinguishable from one that was cut, and the record's identity is its id."""
    long_title = "x" * (MAX_LABEL_CHARS + 40)
    assert check_projection(ENTITY, {"title": long_title}) != []

    fields = a_table().projected_fields(a_record(1, Title=long_title))

    assert fields["title"] == "x" * MAX_LABEL_CHARS
    assert check_projection(ENTITY, dict(fields)) == []


def test_a_fetched_record_becomes_a_projected_record() -> None:
    """The end-to-end positive case: what the mapping produces is what the projection accepts.
    Without it the two halves drift, and the symptom is every record being refused at ingest
    with the projection quietly staying empty."""
    table = a_table()
    row = list_operation(table).project(a_page(a_record(1)))[0]

    record = table.projected_record(row, last_seen_at=NOW)

    assert record.source == LARK_BASE
    assert record.entity == ENTITY
    assert record.field_names == ("client", "renewal", "status", "title")
    assert record.fields["renewal"] == datetime(2026, 11, 15, tzinfo=UTC)
    assert "hours_remaining" not in record.fields
    assert "contract_value" not in record.fields


def test_a_column_read_live_can_never_reach_the_projection_by_the_wrong_function() -> None:
    """`projected_fields` reads the projected bindings only. A caller that used `decode_row`
    and handed the result to `ProjectedRecord` would be storing a measure with no pointer shape
    and no change signal argument behind it, which is the whole of what the tier table
    forbids."""
    table = a_table()
    everything = decode_row(BINDINGS, a_record(1))

    assert "hours_remaining" in everything
    assert "hours_remaining" not in table.projected_fields(a_record(1))

    with pytest.raises(ProjectionRefusedError):
        ProjectedRecord(
            source=LARK_BASE,
            entity=ENTITY,
            source_id="recSNM00001",
            last_seen_at=NOW,
            fields={"contact_line": "+65 6100 0000", "mobile": "+65 6100 0000"},
        )


# ------------------------------------------------- the predicate, never a resolved ACL
def test_the_projection_carries_a_visibility_predicate_the_deployment_chose() -> None:
    """A Base's own sharing is a resolved list held by Lark, unreachable through
    `base:record:read`, and stale for us the moment somebody moves department. A projection
    stored with no predicate has discarded the source's permission model rather than narrowed
    it, and every row is then visible to anybody holding the entity's capability. There is
    deliberately no default in this module to fall back to."""
    table = a_table()

    assert table.projection(visibility=VISIBILITY).visibility == VISIBILITY

    with pytest.raises(ManifestError):
        table.projection(visibility=Scope.unrestricted())

    with pytest.raises(TypeError):
        table.projection()  # type: ignore[call-arg]


def test_a_predicate_that_enumerates_principals_is_refused_as_a_resolved_acl() -> None:
    """The same list wearing a predicate's clothes. An `IN` over principal ids does not
    re-evaluate against the live entitlement set, because there is nothing in it that depends
    on the caller, so it goes stale on the next joiner, mover or leaver with nothing reporting
    it."""
    enumerated = Scope(clauses=(Clause(field="user_id", op=Op.IN, value=("u_wei", "u_ravi")),))

    with pytest.raises(ManifestError):
        a_table().projection(visibility=enumerated)


# ----------------------------------------- absent, refused and unreachable stay three answers
def test_a_non_zero_code_inside_a_two_hundred_is_a_refusal_and_never_an_empty_table() -> None:
    """The recording this connector exists to survive. Lark answers 200 for a permission
    failure and puts the failure in the body's code, so a connector that checks the status and
    projects the body finds no items, returns no records, and has just recorded 'this table is
    empty' as a fact about the company. Delete this and that is what ships."""
    refusal = reply_of("LARK-200-code-permission")

    assert refusal.status == 200

    with pytest.raises(LarkBaseRefusedError) as caught:
        assert_lark_answered(refusal)

    assert caught.value.code == PERMISSION_DENIED_CODE
    assert caught.value.call_outcome is CallOutcome.REJECTED
    assert not is_retryable(caught.value.call_outcome)


def test_an_unrecognised_business_code_is_a_refusal_rather_than_a_retry() -> None:
    """A table of Lark's business codes needs a default for the code nobody has met, and the
    default that reads as safe is 'the source is unwell, try again'. Against a ceiling that
    cannot be raised that turns one permanent refusal into a retry loop which spends the whole
    tenant's minute achieving nothing."""
    with pytest.raises(LarkBaseRefusedError) as caught:
        assert_lark_answered(LarkReply(status=200, body={"code": 1254043, "msg": "NotFound"}))

    assert caught.value.code == 1254043
    assert not is_retryable(caught.value.call_outcome)


def test_a_reply_with_no_code_in_it_is_refused_rather_than_read_as_success() -> None:
    """Every Lark reply carries a code, so something that does not is an error page, a proxy,
    or a login redirect. Reading one as an empty table is how an outage is recorded as a fact
    about the company. A boolean is refused too, because `True` is an `int` in Python and would
    otherwise pass the type test and fail the equality one, which reads as an unknown code."""
    assert business_code(LarkReply(status=200, body={"code": 0})) == 0

    unreadable: tuple[Any, ...] = (
        {"data": {}},
        {"code": "0"},
        {"code": True},
        "<html>Login</html>",
        None,
    )

    for body in unreadable:
        with pytest.raises(LarkBaseRefusedError):
            business_code(LarkReply(status=200, body=body))


def test_a_quota_refusal_is_unreachable_and_carries_the_wait_the_source_asked_for() -> None:
    """A 429 is the source telling us to stop, and the correct answer is that we could not
    reach it rather than a figure from anywhere else. Guessing the wait instead of reading it
    means backing off wrongly in whichever direction the guess went, and guessing low burns
    what is left of an unraisable budget while looking like a well-behaved client."""
    with pytest.raises(LarkBaseUnreachableError) as caught:
        assert_lark_answered(LarkReply(status=429, headers={"Retry-After": "45"}, body={"code": 0}))

    assert caught.value.call_outcome is CallOutcome.QUOTA
    assert caught.value.wait_for == 45.0
    assert wait_seconds(LarkReply(status=429, headers={"retry-after": "45"})) == 45.0


def test_a_refusal_that_states_no_wait_falls_back_to_the_windows_own_length() -> None:
    """A rate limit with nothing to read is a deviation from every recording in the corpus, and
    the safe direction is unambiguous. Against a fixed per-minute ceiling the earliest the
    window can have room is the next minute, so the fallback is measured rather than guessed; a
    zero-second wait is a retry loop against a source that has just asked us to stop."""
    assert WAIT_WHEN_UNSTATED == MINUTE_SECONDS
    assert wait_seconds(LarkReply(status=429)) == WAIT_WHEN_UNSTATED
    assert wait_seconds(LarkReply(status=429, headers={"Retry-After": "0"})) == WAIT_WHEN_UNSTATED
    dated = LarkReply(status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert wait_seconds(dated) == WAIT_WHEN_UNSTATED


def test_a_server_failure_and_a_refused_request_are_not_the_same_answer() -> None:
    """The remedies are opposite: one is waiting, the other is somebody changing a
    configuration. Collapsed together, a Base the bot was never added to is retried until the
    ceiling is exhausted and never succeeds, and every colleague pays for it."""
    with pytest.raises(LarkBaseUnreachableError) as unwell:
        assert_lark_answered(LarkReply(status=502, body={"message": "Bad gateway"}))

    with pytest.raises(LarkBaseRefusedError) as refused:
        assert_lark_answered(LarkReply(status=403, body={"msg": "Forbidden"}))

    assert unwell.value.call_outcome is CallOutcome.UNAVAILABLE
    assert is_retryable(unwell.value.call_outcome)
    assert refused.value.call_outcome is CallOutcome.REJECTED
    assert not is_retryable(refused.value.call_outcome)


def test_an_empty_table_is_an_answer_rather_than_a_failure() -> None:
    """The third of the three, and the positive case for the two above. A guard that raised on
    everything would pass every refusal test in this file and make a genuinely empty table an
    incident. `code: 0` with no items is the one thing that may travel as an empty result."""
    reading = read_records(
        a_table(),
        list_operation(),
        Reader(a_reply(has_more=False, total=0)),
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
    )

    assert reading.result.records == ()
    assert reading.is_all_of_them
    assert not reading.result.truncated
    assert reading.total_at_source == 0


def test_absent_refused_and_unreachable_stay_three_different_answers() -> None:
    """`tests/invariants/test_cassettes.py` asserts the recordings keep the three
    distinguishable; this asserts the connector does. A naive one collapses all three into an
    empty list, and the reading a person takes from an empty list is 'there are none'."""
    absent = read_records(
        a_table(),
        list_operation(),
        Reader(a_reply(has_more=False)),
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
    )
    assert absent.result.records == ()

    with pytest.raises(LarkBaseRefusedError):
        read_page(list_operation(), Reader(reply_of("LARK-200-code-permission")), first_cursor())

    with pytest.raises(LarkBaseUnreachableError):
        read_page(
            list_operation(),
            Reader(LarkReply(status=503, body={"msg": "unavailable"})),
            first_cursor(),
        )


def test_the_sentence_a_person_is_shown_does_not_name_the_system_that_failed() -> None:
    """Naming it says a Base exists and that we are connected to it, which is a fact obtainable
    by anybody who can type a question. The detail belongs in the trace, which is read by
    somebody already entitled to know what the system connects to."""
    unreachable = LarkBaseUnreachableError("lark answered 429", call_outcome=CallOutcome.QUOTA)
    refused = LarkBaseRefusedError("lark answered 91403", code=PERMISSION_DENIED_CODE)

    assert unreachable.public_message == Degraded.public_message
    assert refused.public_message == unreachable.public_message
    assert LARK_BASE not in unreachable.public_message
    assert unreachable.outcome is Outcome.DEGRADED
    assert refused.outcome is Outcome.DEGRADED
    assert LARK_BASE in unreachable.trace_line()


def test_a_failure_is_recognised_before_its_body_is_projected() -> None:
    """A refusal carries a body of its own. Projecting first turns a permission failure into a
    complaint about the response shape, which sends whoever reads the error to the wrong module
    and hides the fact that a credential needs changing."""
    reader = Reader(reply_of("LARK-200-code-permission"))

    with pytest.raises(LarkBaseRefusedError):
        read_page(list_operation(), reader, first_cursor())

    assert len(reader.seen) == 1


def test_a_body_the_specification_does_not_describe_is_a_failure_and_not_an_empty_table() -> None:
    """The direction of the failure is what matters. A source answering with a shape its own
    specification does not describe has failed, and reporting that as 'no records' summarises
    an outage as an absence, which nobody files a bug about."""
    with pytest.raises(RestSpecError):
        list_operation().project({"code": 0, "data": {"has_more": False, "items": {}}})


# -------------------------------------------------------------------------- the paging shape
def test_a_page_that_does_not_say_whether_there_is_more_is_refused() -> None:
    """'There is no more' and 'the source did not say' are two different facts, and defaulting
    to the first is how an incomplete answer comes to read as a complete one, silently, on
    exactly the table that grew."""
    assert envelope_of(a_page(has_more=False)).has_more is False

    silent: tuple[Any, ...] = (
        {"code": 0, "data": {"items": []}},
        {"code": 0},
        {"code": 0, "data": []},
        [],
    )

    for body in silent:
        with pytest.raises(ConnectorContractError):
            envelope_of(body)


def test_a_page_claiming_more_and_naming_no_token_is_refused() -> None:
    """The sharp refusal in the module. A walk that re-sent an empty token would read page one
    for ever, which against a hundred calls a minute spends the whole tenant's budget in under
    a minute and returns the same records every time. Nothing in the reply says it happened:
    the records look real because they are."""
    with pytest.raises(ConnectorContractError):
        envelope_of({"code": 0, "data": {"has_more": True, "items": [], "page_token": "  "}})

    with pytest.raises(ConnectorContractError):
        envelope_of({"code": 0, "data": {"has_more": True, "items": [], "page_token": 17}})

    assert envelope_of(a_page(has_more=True, page_token="tok-1")).continuation == "tok-1"


def test_the_walk_stops_on_has_more_and_never_on_the_total() -> None:
    """A page carries both and only one of them is an end signal. The total is a count taken
    while the table is being edited, so a walk sized from it reads a page that no longer exists
    or stops one short and reports the remainder as absent. Here the total disagrees with the
    number of records by a factor of a hundred and the walk still stops where the source said
    to."""
    reader = Reader(a_reply(a_record(1), has_more=False, total=412))

    reading = read_records(
        a_table(), list_operation(), reader, fetched_at=FETCHED_AT, budget=fair_share_budget()
    )

    assert reading.total_at_source == 412
    assert len(reading.result.records) == 1
    assert reading.is_all_of_them
    assert envelope_of(a_page(has_more=False)).total == TOTAL_UNSTATED
    assert not envelope_of(a_page(has_more=False)).states_a_total


def test_a_page_size_the_endpoint_would_clamp_is_refused_before_it_is_sent() -> None:
    """After the reply arrives a clamped page and a genuinely short one are identical, so the
    refusal has to happen here or nowhere. The recorded exchange uses one hundred, which is the
    only size anybody has verified, and a size the endpoint silently clamps comes back looking
    like a last page."""
    with pytest.raises(ConnectorContractError) as caught:
        PageCursor(endpoint=Endpoint.LIST_RECORDS, page_size=PAGE_SIZE + 1)

    assert str(PAGE_SIZE) in str(caught.value)

    with pytest.raises(ConnectorContractError):
        PageCursor(endpoint=Endpoint.LIST_RECORDS, page_size=0)

    assert first_cursor().page_size == PAGE_SIZE


def test_only_the_arguments_that_are_set_reach_the_address() -> None:
    """`url_for` refuses an argument the operation does not declare, and this is the other half
    of the same rule: an unset page token sent as an empty string is a cursor the source has to
    interpret, and a view argument nobody asked for reads at the call site as a filter being
    applied."""
    table = a_table()
    operation = list_operation(table)

    first = operation.url_for(arguments_for(table, first_cursor()))
    resumed = operation.url_for(arguments_for(table, first_cursor(continuation="tok-1")))
    viewed = operation.url_for(arguments_for(table, first_cursor(view_id=VIEW_ID)))

    assert first.startswith(f"https://{HOST}/open-apis/bitable/v1/apps/{BASE_ID}/tables/")
    assert f"page_size={PAGE_SIZE}" in first
    assert "page_token" not in first
    assert "view_id" not in first
    assert "page_token=tok-1" in resumed
    assert f"view_id={VIEW_ID}" in viewed


def test_a_view_argument_the_source_would_not_recognise_is_refused() -> None:
    """A view id the Base does not know is accepted by the endpoint and ignored, so the call
    site reads as a filter being applied while every record in the table comes back."""
    with pytest.raises(ConnectorContractError):
        first_cursor(view_id="not-a-view")

    assert first_cursor(view_id=VIEW_ID).view_id == VIEW_ID


def test_a_cursor_carries_the_arguments_its_own_endpoint_declares_and_no_others() -> None:
    """Both directions are invisible in the reply. A page cursor carrying a record id sends an
    argument the list operation does not declare; a single read carrying a page token asks for
    a page of one record. Neither is distinguishable afterwards from the thing it pretends to
    be."""
    with pytest.raises(ConnectorContractError):
        PageCursor(endpoint=Endpoint.LIST_RECORDS, record_id="recSNM00001")

    with pytest.raises(ConnectorContractError):
        PageCursor(endpoint=Endpoint.GET_RECORD, record_id="recSNM00001", continuation="tok-1")

    with pytest.raises(ConnectorContractError):
        PageCursor(endpoint=Endpoint.GET_RECORD)

    single = PageCursor(endpoint=Endpoint.GET_RECORD, record_id="recSNM00001")
    assert single.query_arguments() == {"record_id": "recSNM00001"}


# ------------------------------------------------------------------ reading a single record
def test_a_single_read_and_a_page_are_not_read_by_the_same_function() -> None:
    """A page arrives under `data.items` with `has_more` beside it and one record arrives under
    `data.record` with no continuation at all. Without this, a single read handed to the page
    reader fails on the missing `has_more`, which reports a perfectly good reply as malformed
    and sends whoever reads the error looking for a source that is not broken."""
    single = PageCursor(endpoint=Endpoint.GET_RECORD, record_id="recSNM00001")

    with pytest.raises(ConnectorContractError):
        read_page(single_operation(), Reader(), single)

    with pytest.raises(ConnectorContractError):
        read_record(list_operation(), Reader(), first_cursor(), budget=fair_share_budget())

    assert spec_for(Endpoint.GET_RECORD).records_at == "data.record"
    assert not spec_for(Endpoint.GET_RECORD).returns_list
    assert spec_for(Endpoint.LIST_RECORDS).records_at == "data.items"
    assert spec_for(Endpoint.LIST_RECORDS).returns_list


def test_a_single_read_spends_the_same_minute_a_page_does() -> None:
    """A page is read inside a walk and the walk owns the budget. A single read has no loop
    above it, so a call that spent nothing would be a call against the tenant's hundred a
    minute that nothing counted, and enough of them starve everybody else while every budget in
    the process still reads as untouched."""
    body = {"code": 0, "data": {"record": a_record(1)}}
    cursor = PageCursor(endpoint=Endpoint.GET_RECORD, record_id="recSNM00001")

    row, left = read_record(
        single_operation(),
        Reader(LarkReply(status=200, body=body)),
        cursor,
        budget=MinuteBudget(allowance=2),
    )

    assert row["id"] == "recSNM00001"
    assert decode_row(BINDINGS, row)["client"] == "SNM Construction Pte Ltd"
    assert left.spent == 1

    with pytest.raises(LarkBaseBudgetError):
        read_record(
            single_operation(),
            Reader(LarkReply(status=200, body=body)),
            cursor,
            budget=MinuteBudget(allowance=1, spent=1),
        )


def test_a_single_read_that_answers_with_nothing_nameable_is_a_failure() -> None:
    """`normalise` drops a row with no id, so handing one back would turn a reply nobody can
    read into a record that is simply absent. Absence is the one answer that must never be
    manufactured, because nothing downstream can tell it from the truth."""
    cursor = PageCursor(endpoint=Endpoint.GET_RECORD, record_id="recSNM00001")
    empty = LarkReply(status=200, body={"code": 0, "data": {"record": {}}})

    with pytest.raises(ConnectorContractError):
        read_record(single_operation(), Reader(empty), cursor, budget=fair_share_budget())

    refusal = reply_of("LARK-200-code-permission")
    with pytest.raises(LarkBaseRefusedError):
        read_record(single_operation(), Reader(refusal), cursor, budget=fair_share_budget())


# -------------------------------------------------------------------------- the fetch contract
def test_the_fetch_this_connector_returns_can_never_be_handed_the_callers_grants() -> None:
    """The structural half of 'a connector fetches and does not decide'. The check runs on the
    closure rather than on the factory because the closure is the object a registry would call.
    A check that only ever refuses is satisfied by a module with no fetch in it, so this is the
    positive case as well."""
    fetch = records_fetch(
        a_table(),
        list_operation(),
        Reader(a_reply(a_record(1), has_more=False)),
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
    )

    assert_fetches_only(fetch)
    assert len(fetch(FetchRequest(entity=ENTITY)).records) == 1


def test_the_fetch_is_refused_an_entity_this_table_does_not_hold() -> None:
    """A fetch answering for the wrong entity returns records tagged as something they are not,
    and the redactor then looks up the wrong field policy for every one of them. The entity is
    the deployment's here, because one table holds clients and the next holds hours."""
    fetch = records_fetch(
        a_table(),
        list_operation(),
        Reader(a_reply(has_more=False)),
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
    )

    with pytest.raises(ConnectorContractError):
        fetch(FetchRequest(entity="client"))


def test_a_filter_this_connector_cannot_apply_is_refused_rather_than_dropped() -> None:
    """A Base filter is Lark's own FilterInfo expression and nothing here builds one, so a
    filter accepted and discarded reads at the call site as a narrowing that was applied. The
    view is the one narrowing this connector can actually pass to the source, and it reaches
    the cursor rather than being swallowed."""
    reader = Reader(a_reply(has_more=False))
    fetch = records_fetch(
        a_table(), list_operation(), reader, fetched_at=FETCHED_AT, budget=fair_share_budget()
    )

    with pytest.raises(ConnectorContractError):
        fetch(FetchRequest(entity=ENTITY, filters=(("status", "Active"),)))

    fetch(FetchRequest(entity=ENTITY, filters=((VIEW_FILTER, VIEW_ID),)))

    assert reader.seen[-1].view_id == VIEW_ID


def test_a_cursor_is_carried_because_this_source_genuinely_pages_by_one() -> None:
    """Unlike a source that pages by number, `FetchRequest.cursor` means exactly what it says
    here. Ignoring it would answer with the first page, which is the page a caller resuming
    after a budget stop is least likely to want and most likely to accept."""
    reader = Reader(a_reply(has_more=False))
    fetch = records_fetch(
        a_table(), list_operation(), reader, fetched_at=FETCHED_AT, budget=fair_share_budget()
    )

    fetch(FetchRequest(entity=ENTITY, cursor="eyJvZmZzZXQiOjEwMH0"))

    assert reader.seen[0].continuation == "eyJvZmZzZXQiOjEwMH0"


def test_a_callers_own_limit_stops_the_walk_and_is_not_reported_as_the_source_failing() -> None:
    """A limit is a request rather than a guarantee, and the caller already knows they made it.
    Recording it as a truncation says Lark refused to return more, which is a different fact
    with a different remedy, and it puts 'I could not reach lark_base' in front of somebody
    whose question was answered in full. The reading still says the answer is not all of them,
    which is where a caller learns what their own limit did."""
    reader = Reader(*[a_reply(a_record(n), has_more=True, page_token=f"tok-{n}") for n in range(3)])

    reading = read_records(
        a_table(),
        list_operation(),
        reader,
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
        limit=2,
    )

    assert len(reading.result.records) == 2
    assert reading.stopped_at_caller_limit
    assert not reading.stopped_for_budget
    assert reading.result.truncated
    assert not reading.is_all_of_them
    assert reading.partial().is_complete
    assert reading.partial().notice(disclosable=frozenset({LARK_BASE})) == ""


def test_a_limit_reached_on_the_last_page_still_says_the_answer_is_not_all_of_them() -> None:
    """The discriminating case for the flag above, and mutation testing is what found that the
    test beside it was not one: every page there says there is more, so `truncated` is already
    true from the source's own claim and a connector that ignored the caller's limit entirely
    would pass. Here the page says there is no more and the trim still removed a record, so the
    only thing that can set the flag is the limit. Without this a caller who asked for two of
    three is handed two records marked complete, and two of three is not all of them."""
    reader = Reader(a_reply(a_record(1), a_record(2), a_record(3), has_more=False))

    reading = read_records(
        a_table(),
        list_operation(),
        reader,
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
        limit=2,
    )

    assert len(reading.result.records) == 2
    assert not reading.more_at_source
    assert reading.stopped_at_caller_limit
    assert reading.result.truncated
    assert not reading.is_all_of_them


def test_a_limit_met_exactly_by_one_page_does_not_spend_a_call_to_confirm_it() -> None:
    """Mutation testing found this one: `>=` and `>` are indistinguishable everywhere except
    on the page that returns exactly the number asked for, and there the difference is a call
    made against a hundred a minute the whole company shares, to fetch records that are then
    thrown away by the trim. Nothing in the answer would say it happened; the only symptom is
    somebody else being refused a minute later."""
    reader = Reader(a_reply(a_record(1), a_record(2), has_more=True, page_token="tok-1"))

    reading = read_records(
        a_table(),
        list_operation(),
        reader,
        fetched_at=FETCHED_AT,
        budget=fair_share_budget(),
        limit=2,
    )

    assert len(reader.seen) == 1
    assert reading.budget.spent == 1
    assert reading.pages_read == 1
    assert len(reading.result.records) == 2
    assert reading.stopped_at_caller_limit


def test_a_source_that_had_more_and_was_not_stopped_by_us_is_still_a_truncation() -> None:
    """The default the reason table falls back to. Nothing in the walk produces this today,
    which is precisely why it needs a test: a future stop reason added without a thought lands
    here, and TRUNCATED is the honest answer for one that is the source's rather than ours."""
    reading = TableReading(
        result=read_records(
            a_table(),
            list_operation(),
            Reader(a_reply(has_more=False)),
            fetched_at=FETCHED_AT,
            budget=fair_share_budget(),
        ).result,
        pages_read=1,
        budget=fair_share_budget(),
        more_at_source=True,
    )

    assert [f.reason for f in reading.partial().failed] == [FailureReason.TRUNCATED]


def test_a_negative_limit_is_refused_rather_than_quietly_trimming_the_answer() -> None:
    """The failure it prevents is silent, which is why it is worth a test of its own: a
    negative limit is truthy, so the walk would stop after one page and the trim would remove
    records from the end of it. The caller gets a short answer to a question they asked wrongly
    and nothing says either thing happened."""
    with pytest.raises(ValueError, match="limit"):
        read_records(
            a_table(),
            list_operation(),
            Reader(a_reply(has_more=False)),
            fetched_at=FETCHED_AT,
            budget=fair_share_budget(),
            limit=-1,
        )


# ------------------------------------------------------------------- the manifest and health
def test_the_manifest_names_the_ceiling_the_limits_module_actually_verified() -> None:
    """`throttle.limits_for` looks the numbers up by `ceiling` and not by `name`, so a
    deployment installed under a client's own name with no ceiling named runs against no
    measured limit at all. That is the one mistake in this connector that produces no error and
    costs the whole tenant its minute."""
    declared = a_manifest()

    assert declared.name == LARK_BASE
    assert declared.ceiling == LARK_BASE
    assert declared.transport is TransportKind.REST
    assert ceiling_for(declared).per_minute == 100
    assert ceiling_for(declared).raisable is False


def test_a_write_binding_is_refused_under_a_connector_that_only_reads() -> None:
    """The reverse of the check the platform already makes. A grant covering nothing is a
    permission somebody approved, audited and never used, and it reads in a console as a
    connector that writes into the company's Bases. The bot holds `base:record:read`, so the
    grant is also a claim the token cannot honour."""
    declared = a_manifest()

    assert declared.credential.mode is AccessMode.READ_ONLY
    assert declared.credential.write_granted_by == ""

    with pytest.raises(ConnectorContractError):
        a_manifest(
            credential=CredentialBinding(
                ref=READ_REF, mode=AccessMode.WRITE, write_granted_by="rupash"
            )
        )


def test_every_tool_declares_the_identity_it_actually_runs_under() -> None:
    """A bot token means the source enforces nobody's permissions on our behalf, so ours are
    the only ones there are. Declaring DELEGATED would claim a second independent check that
    does not exist, and `brain.tools.registry` would stop insisting on the scope predicate that
    is the only thing standing in for it."""
    declared = a_manifest()

    assert declared.tool_names() == (
        f"{LARK_BASE}.list_{ENTITY}",
        f"{LARK_BASE}.read_{ENTITY}",
    )
    for tool in declared.tools:
        assert tool.identity_mode is IdentityMode.SERVICE
        assert tool.entity == ENTITY


def test_the_list_tool_tells_the_model_that_a_full_page_is_not_all_of_them() -> None:
    """A description is what the model chooses on and is inside the pinned digest. A tool
    described as returning the table's records, with no mention of the shared allowance, is one
    the model will use to answer 'how many' and cite."""
    listed = next(t for t in a_manifest().tools if t.name.endswith(f"list_{ENTITY}"))
    read = next(t for t in a_manifest().tools if t.name.endswith(f"read_{ENTITY}"))

    assert "incomplete" in listed.description
    assert "allowance" in listed.description
    assert "not returned" in read.description


def test_a_host_that_is_not_larks_is_refused_at_the_manifest_as_well_as_at_the_call() -> None:
    """The copied-configuration mistake, and it is invisible to an address checker because the
    address is perfectly reachable. `brain.tools.fetch.assert_fetchable` refuses a private
    address and a credential in a URL; only this refuses a public address that is simply not
    Lark. Built at manifest time as well so it fails in front of whoever is installing it."""
    with pytest.raises(ConnectorContractError):
        a_table().operation(Endpoint.LIST_RECORDS, host="lark.example.com")

    with pytest.raises(ConnectorContractError):
        a_manifest(host="lark.example.com")

    assert a_manifest(host="open.feishu.cn").scope.admits(a_table().selector)


def test_a_projection_is_declared_only_when_something_is_projected() -> None:
    """A `ProjectedEntity` with no fields is a promise to keep nothing fresh, and it reads in a
    console exactly like a projection that is working. A table read entirely live declares
    none, and the tools still work."""
    live_only = a_table(
        bindings=(
            FieldBinding(
                target="hours_remaining", base_field="Hours Remaining", kind=FieldKind.NUMBER
            ),
        )
    )

    assert a_manifest().projection_for(ENTITY) is not None
    assert (
        manifest(
            live_only, host=HOST, credential=CredentialBinding(ref=READ_REF), visibility=VISIBILITY
        ).projections
        == ()
    )


def test_every_call_outcome_has_a_health_state_and_a_quota_refusal_is_not_an_outage() -> None:
    """A connector refused on volume is a working connector being asked too much, and taking it
    out of service for that is the mistake `throttle.A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH`
    describes. A refusal is UNCONFIGURED rather than DOWN for a different reason: it is almost
    always a Base the bot was never added to, which is a task for whoever installed it rather
    than an incident for whoever is on call."""
    assert set(HEALTH_BY_OUTCOME) == set(CallOutcome)

    probes = {outcome: health(outcome=outcome, checked_at=NOW) for outcome in CallOutcome}

    assert probes[CallOutcome.OK].state is HealthState.OK
    assert probes[CallOutcome.QUOTA].state is HealthState.DEGRADED
    assert probes[CallOutcome.QUOTA].is_usable
    assert probes[CallOutcome.UNAVAILABLE].state is HealthState.DOWN
    assert not probes[CallOutcome.UNAVAILABLE].is_usable
    assert probes[CallOutcome.REJECTED].state is HealthState.UNCONFIGURED
    assert probes[CallOutcome.OK].checked_at == NOW
    assert probes[CallOutcome.OK].connector == LARK_BASE


def test_the_subscription_declares_an_id_sweep_because_a_cursor_cannot_see_a_deletion() -> None:
    """A record removed from a Base is not 'updated': it is one the cursor never mentions
    again. Without an absence check it stays in the projection for good, is counted on, and
    reads as current. A webhook cannot be declared instead because the event scope was never
    granted, and declaring one would be putting somebody else's configuration down as our
    guarantee."""
    subscribed = subscription(
        a_table(), notify_within=timedelta(minutes=15), reconcile_every=timedelta(hours=6)
    )

    assert subscribed.source == LARK_BASE
    assert subscribed.entity == ENTITY
    assert subscribed.kind is ChangeSignal.UPDATED_SINCE
    assert subscribed.kind is CHANGE_SIGNAL
    assert subscribed.deletion_check is DeletionCheck.ID_SWEEP
    assert subscribed.needs_an_absence_check
    assert not subscribed.sees_deletions_by_itself
    assert subscribed.promise().interval == timedelta(hours=6)


def test_the_projected_entity_declares_the_signal_the_subscription_promises() -> None:
    """Two declarations of the same fact, and nothing else compares them. A projection claiming
    a change signal the subscription does not deliver is a set of fields that will be quoted as
    current with nothing anywhere refreshing them."""
    projected = a_table().projection(visibility=VISIBILITY)
    subscribed = subscription(
        a_table(), notify_within=timedelta(minutes=15), reconcile_every=timedelta(hours=6)
    )

    assert projected.change_signal is subscribed.kind
    assert isinstance(projected, ProjectedEntity)


def test_the_recordings_this_connector_is_built_against_still_exist() -> None:
    """Every test above is only as good as the corpus. If the Lark recordings are renamed or
    dropped, the tests that name them fail with a lookup error that reads as a bug here rather
    than as the fixture having moved. The page envelope and the code inside a 200 are the two
    shapes that cannot be arranged against a real tenant on demand."""
    recorded = {c.cid for c in for_source(Source.LARK_BASE)}

    assert {"LARK-200-records", "LARK-200-code-permission"} <= recorded
    assert cassette("LARK-200-code-permission").body["code"] == PERMISSION_DENIED_CODE
    assert cassette("LARK-200-records").body["data"]["has_more"] is True
    assert cassette("LARK-200-records").body["data"]["items"][0]["record_id"].startswith("rec")
