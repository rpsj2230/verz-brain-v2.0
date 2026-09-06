"""The Xero connector, tested against the recordings rather than against a Xero key.

Four properties are pinned here, and each one has a wrong version that passes every test
somebody writes without thinking about it.

**The day is a budget, not a pace.** A connector that treats 5,000 a day as 3.5 a minute
looks correct in every unit test and empties the client's allowance by lunchtime, at which
point their payroll export stops too. The tests assert against the platform's own verified
ceiling and against the tenant-wide figure Xero returns in a header, because a connector
keeping a private counter satisfies any test that counts calls on a fake.

**Absent, refused and unreachable stay three answers.** `tests/invariants/test_cassettes.py`
asserts the corpus keeps all three recorded; these assert this connector keeps them apart
all the way to what a person is told and what an auditor reads. The wrong version returns an
empty list for a 429, and an empty ledger is a sentence somebody acts on.

**Money is fetched and never stored.** `amount_due` is not on the platform denylist, so
nothing outside this module refuses it, and declared as a status enum it passes all five
clauses of the projectability test. One test proves exactly that and then proves this
module refuses it anyway. The recorded invoice carries `CANARY-INVOICE-Z9KRT` in that field
for this purpose: a leak is greppable rather than plausible.

**The tenant is the connection.** A fetch addressed to another organisation is refused
before an address is built, and the assertion is on the fetcher never being called, because
a check that runs after the request was assembled is a check that has already spent a call.

Task ids: M11.6.5
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.contract import (
    ConnectorContractError,
    FetchRequest,
    HealthState,
)
from brain.connectors.federation import FailureReason
from brain.connectors.manifest import (
    ChangeSignal,
    FieldShape,
    HotUse,
    ProjectedField,
    failed_clauses,
    projectability,
)
from brain.connectors.projection import assess_staleness
from brain.connectors.throttle import CallOutcome, connector_breaker, record_outcome
from brain.connectors.transports import FieldMapping
from brain.connectors.xero import (
    CONNECTOR_NAME,
    ENTITY_CONTACT,
    ENTITY_INVOICE,
    TENANT_HEADER,
    CallVerdict,
    DayBudget,
    XeroConnection,
    XeroError,
    XeroOutcome,
    XeroReply,
    assert_declarations_agree,
    assert_federated_only,
    connector_fetch,
    day_ceiling,
    day_limit,
    health,
    interpret,
    mapped_targets,
    may_call,
    observe,
    operation_for,
    parse_xero_timestamp,
    projected_record,
    projection_for,
    refresh_promise,
    xero_field_policy,
    xero_manifest,
    xero_retry_delay,
)
from brain.core.entitlement import Capability, EntitlementSet
from brain.core.field_policy import Classification
from brain.core.projection import MAX_PROJECTED_FIELDS, ProjectionRefusedError, is_forbidden
from brain.core.scope import Op
from brain.gate.provenance import Freshness, StalenessHorizon
from brain.ops.limits import MAX_BACKOFF_SECONDS, LimitDecision
from brain.ops.secrets import SecretRef, VaultRole
from brain.tools.fetch import FetchedBytes
from tests.fixtures.cassettes import CASSETTES, Source, for_source, limit_for

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: Xero's day resets at 00:00 New Zealand time, which is a different instant in UTC
#: depending on the season. Written out as an instant here for the same reason the module
#: takes one as a parameter: a test that computed it would be a second implementation of the
#: thing the module deliberately refuses to implement.
RESET = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

TENANT = "tenant_0447"
OTHER_TENANT = "tenant_04471"

REF = SecretRef(path="connectors/xero_ro", role=VaultRole.APPLICATION)

MONEY_CANARY = "CANARY-INVOICE-Z9KRT"

#: A horizon short enough that "just read" and "read yesterday" land in different states.
HORIZON = StalenessHorizon(live_for=timedelta(minutes=15), stale_after=timedelta(hours=24))


class Resolver:
    """Every name answers with one public address. Modelled on the one in test_fetch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return ["93.184.216.34"]


class Fetcher:
    """A fetcher that answers with one body and records every connection it was asked for.

    The recording is the point: the tenant tests assert this list is empty, which is a
    stronger claim than asserting an exception was raised. A refusal after the request was
    assembled has already resolved a name and may already have spent a call.
    """

    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode("utf-8")
        self.connected: list[str] = []

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str:
        del address, max_bytes
        self.connected.append(url)
        return FetchedBytes(body=self.body, final_url=url)


def cassette(cid: str) -> Any:
    """One recording by id, so a test names the recording it is written against."""
    return next(c for c in CASSETTES if c.cid == cid)


INVOICES_200 = cassette("XERO-200-invoices")
RATE_LIMITED = cassette("XERO-429")
TOKEN_EXPIRED = cassette("XERO-401-expired")

#: The same envelope with nothing in it. The corpus records a genuine absence on HubSpot
#: (`HUBSPOT-200-empty`); this is that fact in Xero's own shape, which is what this
#: connector has to tell apart from the two failures above.
EMPTY_LEDGER: dict[str, Any] = {"Invoices": []}


def connection() -> XeroConnection:
    return XeroConnection(tenant_id=TENANT)


def manifest() -> Any:
    return xero_manifest(connection(), ref=REF)


def operation() -> Any:
    return operation_for(ENTITY_INVOICE, resolver=Resolver())


def budget(remaining: int, *, at: datetime = NOW) -> DayBudget:
    return DayBudget(remaining=remaining, resets_at=RESET, observed_at=at)


def allowed_decision() -> LimitDecision:
    return LimitDecision(allowed=True, binding=None, retry_after_seconds=0.0, reason="")


def refused_decision(seconds: float = 30.0) -> LimitDecision:
    return LimitDecision(
        allowed=False, binding=None, retry_after_seconds=seconds, reason="over the minute"
    )


def reply_for(cid: str, **overrides: Any) -> XeroReply:
    """Interpret one recording, so the failures under test are the recorded ones."""
    recorded = cassette(cid)
    arguments: dict[str, Any] = {
        "status": recorded.status,
        "body": recorded.body,
        "fetched_at": NOW.isoformat(),
    }
    arguments.update(overrides)
    return interpret(operation(), **arguments)


# ------------------------------------------------ the day is a budget (M11.3.1, M11.3.3)
def test_the_daily_allowance_is_read_from_the_verified_ceiling_rather_than_declared_here() -> None:
    """A connector that declared its own ceiling could be optimistic about it, and nothing
    would ever contradict the number. `brain.ops.limits` holds the figure with the date it
    was verified and the consequence written beside it.

    Delete this and a hard-coded 5000 in this module passes review, and stays wrong quietly
    the first time the published figure moves."""
    assert day_limit(manifest()) == 5_000
    assert day_limit(manifest()) == limit_for(Source.XERO).calls


def test_the_ceiling_is_reported_as_one_nobody_can_raise() -> None:
    """The limit belongs to the client's tenant rather than to our subscription, so there is
    no plan anybody can buy that moves it. An operator told otherwise spends the outage
    looking for an upgrade button instead of asking for less.

    Delete this and a connector could report a raisable ceiling, which is the one piece of
    information that sends the response in the wrong direction."""
    assert day_ceiling(manifest()).raisable is False


def test_a_spent_day_outlasts_the_platforms_backoff_cap() -> None:
    """**The measured case.** The platform caps a backoff at 300 seconds, which is right for
    a minute window. The recorded refusal asks for 1847 seconds with the day figure at zero,
    so a client obeying the cap returns six times inside the wait it was given, is refused
    six times, and each refusal is a call out of an allowance that does not refill until
    midnight.

    Delete this and delegating the whole wait to `throttle.retry_delay` looks correct."""
    asked = float(RATE_LIMITED.headers["Retry-After"])
    assert asked > MAX_BACKOFF_SECONDS
    waited = xero_retry_delay(
        retry_after_seconds=asked, consecutive_refusals=1, budget=budget(0), now=NOW
    )
    assert waited >= asked
    assert waited >= budget(0).seconds_until_reset(NOW)


def test_a_wait_is_never_shorter_than_the_source_asked_for() -> None:
    """The source knows when its own window reopens and we do not. Coming back early is a
    call spent on a refusal we were told about, which against a daily ceiling is a call
    nobody gets back.

    Delete this and the platform's cap silently shortens every Xero wait."""
    waited = xero_retry_delay(
        retry_after_seconds=900.0, consecutive_refusals=0, budget=None, now=NOW
    )
    assert waited >= 900.0


def test_a_reading_within_one_day_never_raises_the_remaining_figure() -> None:
    """Inside one window the tenant's remaining calls only fall. A reading that says
    otherwise arrived out of order, and believing it hands a burst room it already spent.

    Delete this and taking the latest reading, which reads as obviously correct, silently
    restores an allowance that is gone."""
    spent = budget(3, at=NOW)
    stale_but_later = DayBudget(remaining=900, resets_at=RESET, observed_at=NOW + timedelta(1))
    assert spent.merge(stale_but_later).remaining == 3
    assert stale_but_later.merge(spent).remaining == 3


def test_a_reading_after_the_reset_refills_the_allowance() -> None:
    """The positive half. A budget that only ever fell would pin the connector at zero for
    ever after the first bad day, which is a different way of being permanently wrong.

    Delete this and a merge that always takes the smaller figure passes."""
    spent = budget(0)
    tomorrow = DayBudget(
        remaining=4_900, resets_at=RESET + timedelta(days=1), observed_at=RESET + timedelta(1)
    )
    assert spent.merge(tomorrow).remaining == 4_900
    assert spent.merge(tomorrow).is_exhausted is False


def test_calls_made_between_two_readings_are_counted_against_the_tenants_figure() -> None:
    """The header only arrives with a response, so several calls issued between two readings
    would each see the same remaining figure and each believe there was room.

    Delete this and a fan-out spends the last of a day it had already been told was nearly
    gone."""
    assert budget(2).spend().remaining == 1
    assert budget(2).spend(2).is_exhausted is True
    assert budget(1).spend(5).remaining == 0


def test_the_tenants_own_figure_is_read_from_the_recorded_headers() -> None:
    """The tenant-wide count is the one measurement our own window structurally cannot have:
    it already includes the client's other integrations. The recorded success says 4139 and
    the recorded refusal says 0.

    Delete this and a connector that reads only its own window is optimistic in exactly the
    way that ends the client's day early."""
    live = observe(None, INVOICES_200.headers, at=NOW, resets_at=RESET)
    spent = observe(None, RATE_LIMITED.headers, at=NOW, resets_at=RESET)
    assert live is not None and live.remaining == 4_139
    assert spent is not None and spent.is_exhausted is True


def test_a_header_that_is_absent_is_not_read_as_room() -> None:
    """A missing header means we learned nothing, not that the day is full. The recorded 401
    carries no limit headers at all.

    Delete this and every response without the header resets our belief about the tenant's
    day to whatever a default happened to be."""
    known = budget(12)
    assert observe(known, TOKEN_EXPIRED.headers, at=NOW, resets_at=RESET) == known
    assert observe(None, {}, at=NOW, resets_at=RESET) is None


def test_a_call_is_refused_when_the_tenants_day_is_spent_though_our_window_has_room() -> None:
    """Our window counts our calls and the tenant's day counts everybody's. Both apply and
    neither subsumes the other.

    Delete this and a connector inside its own allowance calls a source that has already
    told it there is nothing left."""
    verdict = may_call(decision=allowed_decision(), budget=budget(0), now=NOW)
    assert verdict.allowed is False
    assert verdict.wait_seconds == pytest.approx(budget(0).seconds_until_reset(NOW))


def test_a_call_is_refused_when_our_window_is_full_though_the_tenant_has_room() -> None:
    """The other direction. The per-principal share exists so one backfill cannot take the
    whole connector, and a tenant with calls left does not make that share bigger.

    Delete this and the platform's own limiter can be ignored by anything holding a
    budget."""
    verdict = may_call(decision=refused_decision(), budget=budget(4_000), now=NOW)
    assert verdict.allowed is False
    assert verdict.wait_seconds == pytest.approx(30.0)


def test_a_call_is_allowed_when_both_allowances_have_room() -> None:
    """The positive sibling. A guard tested only by its refusals is satisfied by a function
    that refuses everything, and a connector that never calls Xero passes every test above.

    Delete this and `may_call` returning False unconditionally is green."""
    assert may_call(decision=allowed_decision(), budget=budget(4_000), now=NOW) == CallVerdict(
        allowed=True
    )


def test_the_first_call_of_the_day_is_not_refused_for_want_of_an_observation() -> None:
    """Nothing has answered yet, so there is no figure to read. Refusing on its absence
    makes the first call of every day impossible and the connector permanently silent.

    Delete this and treating a missing budget as an empty one deadlocks the connector at
    midnight."""
    assert may_call(decision=allowed_decision(), budget=None, now=NOW).allowed is True


def test_a_budget_needs_a_zone_on_both_of_its_instants() -> None:
    """A naive reset instant read in Singapore is eight hours out, which is eight hours of
    believing an allowance has refilled when it has not.

    Delete this and a naive datetime from a configuration file makes the connector call a
    source that is still refusing."""
    with pytest.raises(XeroError, match="timezone"):
        DayBudget(remaining=1, resets_at=RESET.replace(tzinfo=None), observed_at=NOW)
    with pytest.raises(XeroError, match="timezone"):
        DayBudget(remaining=1, resets_at=RESET, observed_at=NOW.replace(tzinfo=None))


def test_a_negative_remaining_count_is_refused_rather_than_read_as_room() -> None:
    """A negative figure is a parse that went wrong, and `is_exhausted` would still be true,
    so the failure would be invisible until somebody printed the number.

    Delete this and a header of "-1" becomes a budget."""
    with pytest.raises(XeroError, match="not a count"):
        DayBudget(remaining=-1, resets_at=RESET, observed_at=NOW)


# ------------------------------------------------------------------ Xero's own dates
def test_a_xero_date_is_read_as_milliseconds_rather_than_seconds() -> None:
    """The recorded due date is 1794700800000. Divided by a thousand it is November 2026;
    read as seconds it is the year 58854, which sorts, filters and renders without
    complaint.

    Delete this and an off-by-1000 error produces due dates fifty-six thousand years out
    that no assertion anywhere notices."""
    recorded = INVOICES_200.body["Invoices"][0]["DueDate"]
    assert parse_xero_timestamp(recorded) == datetime(2026, 11, 15, tzinfo=UTC)


def test_a_parsed_date_carries_a_zone() -> None:
    """A naive timestamp is read in Singapore as eight hours older than it is, which is the
    whole width of the ageing band in `brain.gate.provenance`.

    Delete this and `datetime.fromtimestamp` without a zone passes and every due date is
    silently wrong by the reader's offset."""
    parsed = parse_xero_timestamp("/Date(1794700800000)/")
    assert parsed is not None and parsed.tzinfo is not None


def test_a_date_that_cannot_be_dated_is_dropped_rather_than_guessed() -> None:
    """Xero's ISO renderings carry no zone, and assuming UTC for a ledger keeping New
    Zealand time moves a due date by thirteen hours, which is the difference between an
    invoice that is overdue and one that is not.

    Delete this and a lenient parser attaches a real timestamp to a value nobody sent."""
    assert parse_xero_timestamp("2026-11-15T00:00:00") is None
    assert parse_xero_timestamp("/Date(not-a-number)/") is None
    assert parse_xero_timestamp(None) is None
    row = {"id": "b1f2-0447", "status": "AUTHORISED", "due_date": "2026-11-15T00:00:00"}
    record = projected_record(ENTITY_INVOICE, row, last_seen_at=NOW)
    assert record is not None
    assert "due_date" not in record.fields


# ------------------------------------------------------------ the tenant pin (M11.2.3)
def test_a_connection_admits_its_own_tenant_and_no_other() -> None:
    """A token reaches every organisation it was authorised for, so the pin is the only
    thing narrowing it. Membership is exact rather than by prefix, which is why
    `tenant_0447` does not admit `tenant_04471`: a different company, and the mistake reads
    as correct in every test where the ids do not share a prefix.

    Delete this and a prefix match, or no match at all, passes."""
    pinned = connection()
    assert pinned.admits(TENANT) is True
    assert pinned.admits(OTHER_TENANT) is False
    with pytest.raises(XeroError, match="pinned to one Xero organisation"):
        pinned.assert_admits(OTHER_TENANT)


def test_a_scope_that_narrows_nothing_is_refused_at_connect() -> None:
    """A connector connected to everything the credential reaches has the credential's blast
    radius, and narrowing it later does not un-fetch anything.

    Delete this and a tenant id read from an empty configuration value installs a connector
    scoped to whatever the token can see."""
    for selector in ("", "*", "all"):
        with pytest.raises(ConnectorContractError):
            XeroConnection(tenant_id=selector)


def test_a_fetch_for_another_tenant_never_reaches_the_transport() -> None:
    """**Asserted on the fetcher, not on the exception.** A check that runs after the request
    was assembled has already resolved a name and may already have spent a call out of
    somebody's daily allowance, and the call is what cannot be taken back.

    Delete this and a refusal moved below the address builder still passes a test that only
    looked for the exception."""
    fetcher = Fetcher(INVOICES_200.body)
    fetch = connector_fetch(
        connection(),
        ENTITY_INVOICE,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    with pytest.raises(XeroError):
        fetch(FetchRequest(entity=ENTITY_INVOICE, filters=(("tenant", OTHER_TENANT),)))
    assert fetcher.connected == []


def test_a_fetch_that_names_the_pinned_tenant_reaches_the_source() -> None:
    """The positive sibling, and it also proves the tenant filter is removed rather than
    passed on: the spec declares no `tenant` parameter, so a filter that survived would be
    refused by the address builder and the failure would look like a scope problem.

    Delete this and a connector that refuses every tenant, including its own, is green."""
    fetcher = Fetcher(INVOICES_200.body)
    fetch = connector_fetch(
        connection(),
        ENTITY_INVOICE,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    result = fetch(FetchRequest(entity=ENTITY_INVOICE, filters=(("tenant", TENANT),)))
    assert [r.id for r in result.records] == ["b1f2-0447"]
    assert len(fetcher.connected) == 1


def test_a_fetch_that_names_no_tenant_inherits_the_pin() -> None:
    """The connection already decided which organisation this is. Making every caller repeat
    it gives them somewhere to get it wrong, and the wrong version reads another company's
    ledger under the right company's name.

    Delete this and requiring the filter on every call passes, and every existing caller
    breaks at the same time."""
    fetcher = Fetcher(INVOICES_200.body)
    fetch = connector_fetch(
        connection(),
        ENTITY_INVOICE,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    assert fetch(FetchRequest(entity=ENTITY_INVOICE)).records


def test_the_only_header_this_connector_contributes_is_the_tenant() -> None:
    """A connector borrows a credential for one run and never holds one, so there is no
    authorisation header for it to contribute. The tenant is the whole of what this module
    adds to a call.

    Delete this and an `Authorization` header assembled here would look like part of the
    call rather than like a credential nobody may hold."""
    assert dict(connection().call_headers()) == {TENANT_HEADER: TENANT}


# --------------------------------------------------------------- the contract (M11.1.1)
def test_the_fetch_can_never_be_handed_the_callers_grants() -> None:
    """A connector returns everything it fetched and the redactor removes what is not
    covered. The rule is enforced on what the fetch can be given rather than on what it
    does, because a function never handed an `EntitlementSet` cannot filter by one.

    Delete this and a wrapper that took an entitlement set "just for logging" is
    installable."""
    fetch = connector_fetch(
        connection(),
        ENTITY_INVOICE,
        fetcher=Fetcher(INVOICES_200.body),
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    from brain.connectors.contract import assert_fetches_only

    assert_fetches_only(fetch)
    assert EntitlementSet(principal_id="u_weiling") is not None


def test_a_connection_that_kept_a_token_is_refused_at_construction() -> None:
    """A credential held between calls is a value no rotation can invalidate and no
    revocation can reach. Checked over annotations, so it fails on the first construction
    rather than on the first expiry.

    Delete this and an `api_token` attribute added for convenience survives review."""

    class Leaky(XeroConnection):
        api_token: str = ""

    with pytest.raises(ConnectorContractError, match="api_token"):
        Leaky(tenant_id=TENANT)


# --------------------------------------------------- the projection (M11.4.2, M11.4.4)
def test_the_money_that_makes_this_connector_useful_is_fetched_and_never_stored() -> None:
    """**The canary test.** The recorded invoice carries `CANARY-INVOICE-Z9KRT` as its
    AmountDue. It has to arrive, because it is the answer people ask for, and it must not
    reach the projection, because a stored figure is quoted as current long after the
    invoice was paid.

    The projection is built from the declared fields rather than copied from the row, which
    is the only version that survives somebody adding a mapping target.

    Delete this and a projection that copies the mapped row and deletes what it does not
    want stores the amount the first time the mapping changes."""
    projected_rows = operation().project(INVOICES_200.body)
    assert projected_rows[0]["amount_due"] == MONEY_CANARY
    record = projected_record(ENTITY_INVOICE, projected_rows[0], last_seen_at=NOW)
    assert record is not None
    assert "amount_due" not in record.fields
    assert MONEY_CANARY not in json.dumps(dict(record.fields), default=str)


def test_money_declared_as_a_status_passes_every_platform_clause_and_is_refused_here() -> None:
    """**Why the refusal has to live in this module.** `amount_due` is not on the platform's
    permanent denylist: `contract_value` and `margin` are, and that is a difference in
    vocabulary rather than in kind. Declared as a status enum with a filter use, it passes
    all five clauses of `manifest.projectability` and a reviewer sees nothing wrong.

    Delete this and dropping this module's own guard leaves the field projectable with every
    platform rule still green."""
    disguised = ProjectedField(
        name="amount_due", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.SORT)
    )
    verdicts = projectability(disguised, signal=ChangeSignal.WEBHOOK, label_count=1, field_count=5)
    assert failed_clauses(verdicts) == ()
    assert is_forbidden("amount_due") is False
    with pytest.raises(ProjectionRefusedError, match="amount_due"):
        assert_federated_only(ENTITY_INVOICE, ["amount_due"])


def test_a_tax_number_is_refused_here_because_the_denylist_does_not_spell_it() -> None:
    """The denylist spells `nric`, `passport`, `nin` and `ssn`. It does not spell the tax
    identity number that appears on every invoice, and for a sole trader that number is a
    personal identity number.

    Delete this and the gap closes silently the day somebody maps TaxNumber into the
    projection."""
    assert is_forbidden("tax_number") is False
    with pytest.raises(ProjectionRefusedError, match="tax_number"):
        assert_federated_only(ENTITY_CONTACT, ["tax_number"])
    with pytest.raises(ProjectionRefusedError, match="TaxNumber"):
        assert_federated_only(ENTITY_CONTACT, ["TaxNumber"])


def test_a_contacts_contact_details_are_refused_by_the_platform_denylist() -> None:
    """These are the ones the platform does spell, and this connector relies on that rather
    than restating it. It maps none of them either, so nothing about a contact's personal
    details travels through this process at all.

    Delete this and a mapping added for a "send the invoice by email" feature is
    unopposed."""
    for name in ("email", "phone", "address", "bank_account", "bank_details"):
        assert is_forbidden(name) is True
    assert set(mapped_targets(ENTITY_CONTACT)) == {"name", "status", "updated_at", "tax_number"}


def test_the_projection_keeps_the_pointers_that_make_a_row_findable() -> None:
    """The positive sibling for every refusal above. A projection that stored nothing would
    satisfy all of them and make the fast lane useless, which is the failure that gets the
    twelve-field cap widened rather than respected.

    Delete this and a projection builder returning an empty mapping is green."""
    record = projected_record(
        ENTITY_INVOICE, operation().project(INVOICES_200.body)[0], last_seen_at=NOW
    )
    assert record is not None
    assert record.source_id == "b1f2-0447"
    assert record.field_names == ("contact_id", "due_date", "invoice_number", "status")
    assert record.fields["due_date"] == datetime(2026, 11, 15, tzinfo=UTC)


def test_a_row_with_no_id_is_dropped_rather_than_given_one() -> None:
    """A generated id cannot be cited, cannot be pointed at by a request-access route and
    cannot be matched to the same record on the next fetch, so the row would be reported
    twice and audited never.

    Delete this and a row missing its InvoiceID acquires an invented identity."""
    assert projected_record(ENTITY_INVOICE, {"status": "AUTHORISED"}, last_seen_at=NOW) is None


def test_the_projection_stores_xeros_own_visibility_predicate() -> None:
    """Xero's unit of access is the organisation, so the true predicate is the tenant. An
    unrestricted one would say the same thing while looking as though the source's model had
    been carried across, and `ProjectedEntity` refuses it for that reason.

    Delete this and a projection with no predicate, which is the absence of the source's
    rules rather than a narrower version of them, installs cleanly."""
    projection = projection_for(ENTITY_INVOICE, connection())
    assert projection.visibility.is_unrestricted() is False
    clause = projection.visibility.clauses[0]
    assert (clause.field, clause.op, clause.value) == ("tenant_id", Op.EQ, TENANT)


def test_the_projection_is_a_pointer_and_stays_inside_the_cap() -> None:
    """Twelve fields per entity kind, and one label. This connector is well inside both, and
    the assertion is there so that a future field lands against a number rather than against
    somebody's judgement about whether it is one field too many.

    Delete this and the projection grows a field at a time until it is a mirror."""
    for entity in (ENTITY_INVOICE, ENTITY_CONTACT):
        projection = projection_for(entity, connection())
        assert len(projection.fields) <= MAX_PROJECTED_FIELDS
        assert sum(1 for f in projection.fields if f.shape is FieldShape.LABEL) == 1


def test_the_change_signal_promise_is_the_reconciliation_pass_not_the_webhook() -> None:
    """A lost webhook delivery leaves nothing behind to notice it by, so a freshness promise
    made on the notification is a promise nothing keeps. The interval is the period of the
    pass that would notice a missed change.

    Delete this and an interval of seconds makes every projected row read as live while the
    signal has actually stopped."""
    promise = refresh_promise()
    assert promise.signal is ChangeSignal.WEBHOOK
    assert promise.interval >= timedelta(minutes=30)
    record = projected_record(
        ENTITY_INVOICE, operation().project(INVOICES_200.body)[0], last_seen_at=NOW
    )
    assert record is not None
    reading = assess_staleness(record, now=NOW + timedelta(days=1), promise=promise)
    assert reading.freshness is Freshness.STALE


# ------------------------------------------------- the three declarations (M11.4.5)
def test_every_mapped_field_is_classified_by_the_policy() -> None:
    """A mapped field nothing classifies is withheld from everybody by default-deny, which
    is safe and pointless: it travels through this process and into traces in exchange for
    nothing.

    Delete this and a field added to the mapping to "see if it is useful" ships."""
    policy = xero_field_policy()
    for entity in (ENTITY_INVOICE, ENTITY_CONTACT):
        for name in mapped_targets(entity):
            assert policy.governs(entity, name), f"{entity}.{name} is mapped and unclassified"


def test_every_projected_field_is_also_mapped() -> None:
    """A projected field nothing maps is a column that never arrives, so a fast-lane filter
    on it silently matches nothing and the answer is an empty list nobody questions.

    Delete this and a rename on one side of the pair goes unnoticed until somebody asks a
    question that returns nothing."""
    assert_declarations_agree()
    for entity in (ENTITY_INVOICE, ENTITY_CONTACT):
        mapped = set(mapped_targets(entity))
        projected = {f.name for f in projection_for(entity, connection()).fields}
        assert projected <= mapped


def test_a_mapping_nothing_classifies_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check itself, exercised rather than assumed. Three lists edited by three people at
    three different times disagree quietly, and every disagreement is invisible in review.

    Delete this and `assert_declarations_agree` could return without comparing anything."""
    from brain.connectors import xero as module

    monkeypatch.setattr(
        module,
        "INVOICE_FIELDS",
        (*module.INVOICE_FIELDS, FieldMapping(target="reference", source_path="Reference")),
    )
    with pytest.raises(XeroError, match="reference"):
        assert_declarations_agree()


# ------------------------------------------------------- the classifications (M4.2.1)
def test_the_amount_owing_is_restricted_and_reachable_only_by_its_own_capability() -> None:
    """**Said plainly, because the brief asks for it.** `amount_due` is money, it is the
    field the permission canaries protect, and it is RESTRICTED: returnable to somebody
    holding `read:invoice.amount_due` and never stored anywhere.

    Delete this and a classification of INTERNAL, which reads as harmless, puts an invoice
    total in front of everybody with any invoice grant."""
    rule = xero_field_policy().rule_for(ENTITY_INVOICE, "amount_due")
    assert rule is not None
    assert rule.classification is Classification.RESTRICTED
    assert rule.required_capability == Capability(value="read:invoice.amount_due")
    assert rule.required_capability.verb == "read"


def test_a_contacts_email_is_classified_by_nothing_and_therefore_withheld() -> None:
    """Default-deny is the answer rather than a gap somebody should fill in. Adding a rule
    for a contact's personal details is a deliberate act by whoever owns the field policy,
    not a side effect of somebody adding a connector.

    Delete this and a rule added here quietly makes contact details returnable estate-wide."""
    policy = xero_field_policy()
    for name in ("email", "phone", "address", "bank_account_details"):
        assert policy.rule_for(ENTITY_CONTACT, name) is None


def test_a_tax_number_is_returnable_and_still_unstorable() -> None:
    """The combination `brain.core.field_policy` names as the ordinary case: a field can be
    returnable and unstorable at the same time, and most of the interesting ones are.

    Delete this and the two rules collapse into one, which ends either with the number
    stored or with a compliance question nobody can answer."""
    rule = xero_field_policy().rule_for(ENTITY_CONTACT, "tax_number")
    assert rule is not None and rule.classification is Classification.RESTRICTED
    with pytest.raises(ProjectionRefusedError):
        assert_federated_only(ENTITY_CONTACT, ["tax_number"])


# ------------------------------------------------- three answers, not one (M11.5.5)
def test_an_empty_ledger_and_an_unreachable_one_are_different_answers() -> None:
    """`tests/invariants/test_cassettes.py` keeps the three recorded; this keeps them apart
    in the connector. An empty result for a 429 produces "nothing is owing" out of "I could
    not read the ledger", and somebody acts on it.

    Delete this and collapsing every failure into an empty list is green."""
    present = interpret(operation(), status=200, body=INVOICES_200.body, fetched_at=NOW.isoformat())
    absent = interpret(operation(), status=200, body=EMPTY_LEDGER, fetched_at=NOW.isoformat())
    unreachable = reply_for("XERO-429")
    refused = reply_for("XERO-401-expired")
    assert present.outcome is XeroOutcome.PRESENT
    assert absent.outcome is XeroOutcome.ABSENT
    assert unreachable.outcome is XeroOutcome.UNREACHABLE
    assert refused.outcome is XeroOutcome.REFUSED
    assert len({r.outcome for r in (present, absent, unreachable, refused)}) == 4


def test_an_absence_is_answered_and_a_failure_is_not() -> None:
    """The distinction that matters downstream: an absence is a result with a read time on
    it, and a failure has neither rows nor a time. Both are "no records" to anything that
    only counts.

    Delete this and `answered` could return True for everything, which makes the four
    outcomes decorative."""
    absent = interpret(operation(), status=200, body=EMPTY_LEDGER, fetched_at=NOW.isoformat())
    assert absent.outcome.answered is True
    assert absent.rows is not None and absent.rows.records == ()
    assert reply_for("XERO-429").outcome.answered is False


def test_a_rate_limited_reply_carries_no_rows_and_no_read_time() -> None:
    """**The structural half of "never answer from memory".** A failed reply has nowhere to
    put rows and nowhere to put a read time, so substituting the last good response is not
    something a caller can express. A read time would be worse than the rows: freshness is
    computed from it, and the answer would be reported as current.

    Delete this and a well-meaning cache layer fills the failure branch in."""
    refusal = reply_for("XERO-429")
    assert refusal.rows is None
    assert refusal.fetched_at == ""
    with pytest.raises(XeroError, match="rows or a read time"):
        XeroReply(
            outcome=XeroOutcome.UNREACHABLE,
            call=CallOutcome.QUOTA,
            reason=FailureReason.QUOTA,
            fetched_at=NOW.isoformat(),
        )


def test_a_reply_cannot_claim_records_it_does_not_hold() -> None:
    """PRESENT and ABSENT are decided by what came back, and a reply free to disagree with
    its own rows is a reply that can report an empty ledger as a full one. It is the same
    mistake as the failure branch above, arrived at from the other side.

    Delete this and the outcome becomes a label a caller chooses rather than a fact."""
    empty = interpret(operation(), status=200, body=EMPTY_LEDGER, fetched_at=NOW.isoformat()).rows
    with pytest.raises(XeroError, match="present and absent are decided"):
        XeroReply(
            outcome=XeroOutcome.PRESENT,
            call=CallOutcome.OK,
            rows=empty,
            fetched_at=NOW.isoformat(),
        )
    with pytest.raises(XeroError, match="carries no rows"):
        XeroReply(outcome=XeroOutcome.ABSENT, call=CallOutcome.OK, fetched_at=NOW.isoformat())


def test_a_failure_is_never_reported_as_current() -> None:
    """It follows from the rule above rather than from a second one: with no read time,
    `brain.gate.provenance.assess_freshness` returns UNSTATED by its own argument about a
    time it cannot date.

    Delete this and a freshness branch of this module's own could quietly call a failure
    live."""
    assert reply_for("XERO-429").freshness(horizon=HORIZON, now=NOW) is Freshness.UNSTATED
    assert reply_for("XERO-401-expired").freshness(horizon=HORIZON, now=NOW) is Freshness.UNSTATED


def test_a_reply_read_a_moment_ago_is_live_and_one_read_yesterday_is_not() -> None:
    """The positive sibling. A connector that reported UNSTATED for everything would satisfy
    the test above and make every answer in the building carry a caveat, which trains people
    to skip the line that matters.

    Delete this and freshness could be hard-coded to UNSTATED."""
    fresh = interpret(operation(), status=200, body=INVOICES_200.body, fetched_at=NOW.isoformat())
    assert fresh.freshness(horizon=HORIZON, now=NOW) is Freshness.LIVE
    assert fresh.freshness(horizon=HORIZON, now=NOW + timedelta(days=2)) is Freshness.STALE


def test_the_error_body_of_a_failure_is_never_projected() -> None:
    """The recorded 429 body holds a `Title` and no `Invoices`, so running the field mapping
    over it would raise a specification error in the middle of somebody's question. The
    projection happens on the success branch only.

    Delete this and projecting before classifying turns every rate limit into a crash."""
    assert "Invoices" not in RATE_LIMITED.body
    assert reply_for("XERO-429").outcome is XeroOutcome.UNREACHABLE


def test_a_person_is_told_the_same_thing_whether_we_were_refused_or_unreachable() -> None:
    """The distinction is ours to act on rather than theirs. "Xero declined our credentials"
    tells somebody that we hold Xero credentials, which is a disclosure, and there is
    nothing they can do with it either way.

    Delete this and a helpfully specific message enumerates the company's systems for
    anybody who can type a question."""
    disclosable: frozenset[str] = frozenset()
    spoken = reply_for("XERO-429").notice(disclosable=disclosable)
    assert spoken == reply_for("XERO-401-expired").notice(disclosable=disclosable)
    assert spoken != ""
    # And it does not name the source to somebody whose catalogue never did. A sentence of
    # this module's own would be the generous copy, because it is the one somebody edits
    # while debugging an outage.
    assert CONNECTOR_NAME not in spoken.casefold()


def test_a_notice_names_xero_only_when_the_askers_catalogue_already_did() -> None:
    """Naming a source is a disclosure, and the rule is `federation.PartialAnswer.notice`'s
    rather than a second one written here.

    Delete this and this module could grow its own sentence, which would be the generous
    copy because it is the one somebody edits while debugging."""
    named = reply_for("XERO-429").notice(disclosable=frozenset({CONNECTOR_NAME}))
    assert CONNECTOR_NAME in named


def test_an_answered_reply_says_nothing_about_itself() -> None:
    """A reassurance attached to every successful answer is a claim offered where nobody
    asked for one, and it trains a reader to skip the line that matters when it eventually
    says something else.

    Delete this and every answer carries a sentence about Xero."""
    present = interpret(operation(), status=200, body=INVOICES_200.body, fetched_at=NOW.isoformat())
    assert present.notice(disclosable=frozenset({CONNECTOR_NAME})) == ""
    assert present.failure() is None


def test_the_trace_keeps_the_distinction_the_notice_drops() -> None:
    """An auditor is already entitled to know what this system connects to, and the two
    failures go to different people: a declined authorisation is somebody re-authorising a
    connection, and a rate limit is somebody asking for less.

    Delete this and the two become indistinguishable everywhere, including in the console
    row that decides who gets called."""
    assert reply_for("XERO-429").failure() is not None
    assert reply_for("XERO-429").failure().reason is FailureReason.QUOTA  # type: ignore[union-attr]
    assert reply_for("XERO-401-expired").failure().reason is FailureReason.NOT_SERVING  # type: ignore[union-attr]
    assert reply_for("XERO-429").trace_line() != reply_for("XERO-401-expired").trace_line()


def test_nothing_a_reply_renders_carries_a_value_from_the_response_body() -> None:
    """A detail assembled from a response would put a filter value, and therefore a client's
    name, into a trace and a health row that have a different audience and a different
    retention from the answer they describe.

    Delete this and echoing the vendor's error text into the detail looks helpful."""
    body = dict(RATE_LIMITED.body) | {"Detail": MONEY_CANARY, "Contact": "SNM Construction"}
    reply = interpret(operation(), status=429, body=body, fetched_at=NOW.isoformat())
    rendered = f"{reply.trace_line()} {reply.detail} {reply.notice(disclosable=frozenset())}"
    assert MONEY_CANARY not in rendered
    assert "SNM" not in rendered


def test_a_rate_limit_is_never_counted_against_the_breaker() -> None:
    """A 429 is the rate limiter working and the source saying so. Counting it as ill health
    opens the circuit whenever a connector is popular, so the busiest connector in the
    company is the intermittently unavailable one.

    Delete this and mapping a quota refusal onto a breaker failure passes, and the fix
    somebody reaches for is a longer cooldown, which makes it worse."""
    breaker = connector_breaker(CONNECTOR_NAME)
    after = record_outcome(breaker, reply_for("XERO-429").call, now=NOW)
    assert after.consecutive_failures == 0


def test_a_source_that_did_not_answer_is_counted_against_the_breaker() -> None:
    """The positive sibling. A classification that never counted anything would satisfy the
    test above and leave a dead source in rotation for ever.

    Delete this and returning QUOTA for every failure is green."""
    breaker = connector_breaker(CONNECTOR_NAME)
    reply = interpret(operation(), status=500, body={"message": "Server Error"}, fetched_at="")
    assert reply.call is CallOutcome.UNAVAILABLE
    assert record_outcome(breaker, reply.call, now=NOW).consecutive_failures == 1


def test_a_timeout_is_unreachable_and_says_so_in_the_trace() -> None:
    """A source that never answered has no status to classify, and a caller holding a stale
    status variable would otherwise have it read.

    Delete this and a timeout with a leftover 200 in scope reports rows that never
    arrived."""
    reply = interpret(operation(), status=None, body=None, fetched_at="", timed_out=True)
    assert reply.outcome is XeroOutcome.UNREACHABLE
    assert reply.detail.endswith("in time")


# ---------------------------------------------------------------------- health (M11.1.1)
def test_a_spent_day_is_degraded_rather_than_down() -> None:
    """The source is healthy and we are out of allowance. DOWN sends somebody to check
    whether Xero is up, which it is, and the only action available is to ask for less until
    the reset.

    Delete this and a spent allowance reads as an outage every single afternoon."""
    state = health(reply_for("XERO-429"), budget=budget(0), checked_at=NOW)
    assert state.state is HealthState.DEGRADED
    assert state.is_usable is True
    assert state.checked_at == NOW
    # And a call that succeeded does not restore the row to OK while the allowance is gone:
    # a connector that answered one question and has nothing left for the next is not OK,
    # whatever that one call did. This is why the budget is read before the outcome.
    answered = interpret(operation(), status=200, body=EMPTY_LEDGER, fetched_at=NOW.isoformat())
    assert health(answered, budget=budget(0), checked_at=NOW).state is HealthState.DEGRADED


def test_a_declined_authorisation_is_down_rather_than_unconfigured() -> None:
    """It was working this morning, so it is an incident for whoever owns the connection.
    UNCONFIGURED would file it as an installation task and it would sit there.

    Delete this and an expired token joins the permanently amber rollout rows nobody
    reads."""
    assert health(reply_for("XERO-401-expired"), budget=None, checked_at=NOW).state is (
        HealthState.DOWN
    )


def test_a_connector_nobody_has_probed_is_unconfigured_rather_than_down() -> None:
    """A connector nobody has called yet is a job for whoever installed it, and reporting
    DOWN would page somebody about a system that may be perfectly healthy.

    Delete this and every connector is DOWN between installation and its first call."""
    unprobed = health(None, budget=None, checked_at=NOW)
    assert unprobed.state is HealthState.UNCONFIGURED
    assert unprobed.is_usable is False


def test_a_healthy_call_is_reported_as_healthy() -> None:
    """The positive sibling for the three above. A health function that never returns OK is
    a dashboard nobody looks at twice.

    Delete this and returning DEGRADED unconditionally is green."""
    present = interpret(operation(), status=200, body=INVOICES_200.body, fetched_at=NOW.isoformat())
    assert health(present, budget=budget(4_139), checked_at=NOW).state is HealthState.OK


def test_every_call_outcome_has_a_health_state() -> None:
    """The mapping is total on purpose. A `dict.get` with a default would let a sixth outcome
    be classified as whatever the default said, and for a health state the convenient
    default is OK.

    Delete this and a new outcome raises a KeyError inside a probe, which reads as the
    connector being broken."""
    for outcome in CallOutcome:
        reply = XeroReply(
            outcome=XeroOutcome.ABSENT
            if outcome in (CallOutcome.OK, CallOutcome.TRUNCATED)
            else (
                XeroOutcome.REFUSED if outcome is CallOutcome.REJECTED else XeroOutcome.UNREACHABLE
            ),
            call=outcome,
            rows=None
            if outcome not in (CallOutcome.OK, CallOutcome.TRUNCATED)
            else interpret(
                operation(), status=200, body=EMPTY_LEDGER, fetched_at=NOW.isoformat()
            ).rows,
            fetched_at=""
            if outcome not in (CallOutcome.OK, CallOutcome.TRUNCATED)
            else NOW.isoformat(),
            reason=None
            if outcome in (CallOutcome.OK, CallOutcome.TRUNCATED)
            else FailureReason.TRANSPORT,
        )
        assert health(reply, budget=None, checked_at=NOW).state in HealthState


# ------------------------------------------------------------ the manifest (M11.1.7)
def test_the_manifest_is_read_only_by_declaring_nothing_else() -> None:
    """Read-only is the default value of the field, so a connector installed by somebody in
    a hurry is read-only. Write is a separate deliberate grant that records who made it.

    Delete this and a write binding added while testing a "mark as paid" feature ships."""
    installed = manifest()
    assert installed.credential.mode.value == "read_only"
    assert installed.credential.write_granted_by == ""
    assert installed.ceiling == "xero"


def test_the_manifest_is_scoped_to_the_connections_tenant() -> None:
    """The scope on the manifest is the same pin the connection carries, so the console row
    an operator reads is the restriction that is actually applied.

    Delete this and a manifest scoped to something else than the connection installs, and
    the two disagree with nothing reporting it."""
    assert manifest().scope.selectors == (TENANT,)
    assert manifest().scope.admits(OTHER_TENANT) is False


def test_the_recordings_this_connector_was_written_against_are_still_there() -> None:
    """This whole file is written against three recordings. If the corpus loses one, these
    tests would keep passing against whatever the fixture became, which is the failure mode
    a cassette exists to prevent.

    Delete this and a silently edited cassette changes what is under test."""
    recorded = {c.cid for c in for_source(Source.XERO)}
    assert {"XERO-200-invoices", "XERO-429", "XERO-401-expired"} <= recorded
    assert RATE_LIMITED.headers["Retry-After"] == "1847"
