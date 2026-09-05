"""The live fetch path and what sits in front of it: fan-out, budgets, breaker, metrics.

Separate from `test_connectors.py` because these are about a request rather than about an
installation. Everything there is decided once, at review; everything here is decided while
somebody is waiting.

Task ids: M11.3.1, M11.3.2, M11.3.3, M11.3.4, M11.3.5, M11.5.1, M11.5.2, M11.5.3, M11.5.4,
M11.5.5
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.connectors.contract import ConnectorScope, CredentialBinding, TransportKind
from brain.connectors.federation import (
    CONNECTOR_TIMEOUT_MS,
    FEDERATION_TIMEOUT_MS,
    BudgetState,
    CallBudget,
    FailureReason,
    FanOutPlan,
    FederationError,
    FlightRole,
    PartialAnswer,
    SingleFlight,
    SourceCall,
    SourceFailure,
    flight_key,
)
from brain.connectors.manifest import ConnectorManifest
from brain.connectors.throttle import (
    CallOutcome,
    CallRecord,
    UnmeasuredSourceError,
    ceiling_for,
    classify,
    connector_breaker,
    is_breaker_failure,
    is_retryable,
    limits_for,
    measure,
    percentile_ms,
    record_outcome,
    retry_delay,
)
from brain.core.envelope import SideEffect
from brain.core.errors import Degraded
from brain.models.routing import BREAKER_CONSECUTIVE_FAILURES, BreakerState
from brain.ops.limits import FRESHDESK_SEARCH_MAX_RECORDS, LimitScope
from brain.ops.secrets import SecretRef, VaultRole

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
REF = SecretRef(path="database/creds/xero_ro", role=VaultRole.APPLICATION)


def a_manifest(*, name: str = "xero", ceiling: str = "xero") -> ConnectorManifest:
    return ConnectorManifest(
        name=name,
        version="1.0.0",
        transport=TransportKind.REST,
        scope=ConnectorScope(resource_kind="tenant", selectors=("t_verz",)),
        credential=CredentialBinding(ref=REF),
        ceiling=ceiling,
    )


# ------------------------------------------------------------- the live timeout (M11.5.1)
def test_the_live_fetch_timeout_is_eight_hundred_milliseconds() -> None:
    """Pinned as a number because the whole latency argument rests on it. A live fetch that
    quietly grew to two seconds would leave the answer lane nothing for the model, and the
    symptom would be slow answers rather than a changed constant."""
    assert FEDERATION_TIMEOUT_MS == 800


def test_the_live_fetch_timeout_is_shorter_than_the_task_lane_default() -> None:
    """The two exist because somebody is waiting on one and nobody is watching the other.
    If they converged, either the answer lane would inherit a five-second fetch or the task
    lane would start abandoning work that was merely slow."""
    assert FEDERATION_TIMEOUT_MS < CONNECTOR_TIMEOUT_MS


# ---------------------------------------------------------------- the fan-out (M11.5.2)
def test_independent_calls_cost_the_slowest_and_not_the_sum() -> None:
    """This is the whole point of fanning out. Reporting the sum makes every multi-source
    question look impossible and pushes somebody to cut a source that costs nothing in
    parallel."""
    plan = FanOutPlan(
        calls=(
            SourceCall(call_id="crm", connector="hubspot", entity="company"),
            SourceCall(call_id="helpdesk", connector="freshdesk", entity="ticket"),
            SourceCall(call_id="ledger", connector="xero", entity="invoice"),
        )
    )
    assert plan.critical_path_ms() == FEDERATION_TIMEOUT_MS
    assert plan.sequential_ms() == FEDERATION_TIMEOUT_MS * 3
    assert plan.waves() == (("crm", "helpdesk", "ledger"),)


def test_a_dependent_call_extends_the_critical_path() -> None:
    """A call that needs another call's answer cannot start until it has it. Treating the
    two as independent produces a plan whose latency is right on paper and whose second
    call has no argument to make."""
    plan = FanOutPlan(
        calls=(
            SourceCall(call_id="resolve", connector="freshdesk", entity="company"),
            SourceCall(
                call_id="ledger", connector="xero", entity="invoice", depends_on=("resolve",)
            ),
        )
    )
    assert plan.critical_path_ms() == FEDERATION_TIMEOUT_MS * 2
    assert plan.waves() == (("resolve",), ("ledger",))


def test_a_plan_deeper_than_the_budget_is_refused_with_the_chain_named() -> None:
    """'There is a cycle' or 'too slow' sends somebody to read the whole graph. Naming the
    chain makes it a one-line fix, and the refusal happens before any call is made."""
    plan = FanOutPlan(
        calls=(
            SourceCall(call_id="a", connector="x", entity="e"),
            SourceCall(call_id="b", connector="y", entity="e", depends_on=("a",)),
            SourceCall(call_id="c", connector="z", entity="e", depends_on=("b",)),
        )
    )
    with pytest.raises(FederationError, match="a then b then c"):
        plan.assert_within()


def test_a_plan_with_a_cycle_is_refused_at_construction() -> None:
    """A cycle deadlocks the executor, and a deadlock at request time reports as a slow
    source rather than as the bug it is."""
    with pytest.raises(FederationError, match="cycle"):
        FanOutPlan(
            calls=(
                SourceCall(call_id="a", connector="x", entity="e", depends_on=("b",)),
                SourceCall(call_id="b", connector="y", entity="e", depends_on=("a",)),
            )
        )


def test_a_dependency_on_a_call_outside_the_plan_is_refused() -> None:
    """An executor would either wait forever or start the call too early, and both look
    like the source being slow."""
    with pytest.raises(FederationError, match="not in this plan"):
        FanOutPlan(calls=(SourceCall(call_id="a", connector="x", entity="e", depends_on=("z",)),))


def test_two_calls_sharing_an_id_are_refused() -> None:
    """A dependency on a duplicated id is ambiguous, and which call it waits for would be
    decided by iteration order."""
    with pytest.raises(FederationError, match="share the id"):
        FanOutPlan(
            calls=(
                SourceCall(call_id="a", connector="x", entity="e"),
                SourceCall(call_id="a", connector="y", entity="e"),
            )
        )


# ------------------------------------------------------------------ the budgets (M11.5.3)
def test_a_plan_over_the_per_source_budget_is_refused_before_the_first_call() -> None:
    """Enforcing call by call has already spent everything up to the refusal, and against
    Xero's 5,000 a day per tenant those calls do not come back."""
    plan = FanOutPlan(
        calls=tuple(
            SourceCall(call_id=f"c{i}", connector="xero", entity="invoice") for i in range(7)
        )
    )
    with pytest.raises(FederationError, match="per-source budget"):
        CallBudget().assert_affordable(plan)


def test_parallelism_cannot_be_used_to_evade_a_ceiling() -> None:
    """Eight calls at once spend eight of the tenant's allowance. Without the per-source
    budget, fanning out is a way around the rate limits rather than a way around latency."""
    plan = FanOutPlan(
        calls=tuple(
            SourceCall(call_id=f"c{i}", connector="xero", entity="invoice") for i in range(8)
        )
    )
    assert plan.critical_path_ms() == FEDERATION_TIMEOUT_MS
    assert CallBudget().check(plan)


def test_every_way_a_plan_exceeds_its_budget_is_reported_at_once() -> None:
    """One at a time turns assembling a plan into a guessing game where each fix reveals
    the next objection."""
    plan = FanOutPlan(
        calls=tuple(
            SourceCall(call_id=f"c{i}", connector="xero", entity="invoice") for i in range(9)
        )
    )
    problems = CallBudget(total=5).check(plan)
    assert len(problems) == 2


def test_spending_past_the_budget_raises_rather_than_returning_a_flag() -> None:
    """A flag is checked by the caller that remembered to, and a call made past an
    exhausted budget has already spent somebody's tenant allowance."""
    budget = CallBudget(total=2)
    state = BudgetState().spend("xero", budget=budget).spend("xero", budget=budget)
    with pytest.raises(FederationError, match="whole budget"):
        state.spend("xero", budget=budget)


def test_the_per_source_budget_binds_before_the_global_one() -> None:
    """The two limits are different failures. Without the per-source half, one question can
    take a whole tenant's minute from one connector while staying inside a generous global
    figure."""
    budget = CallBudget(total=50, default_per_source=1)
    state = BudgetState().spend("xero", budget=budget)
    with pytest.raises(FederationError, match="budget of 1 for that source"):
        state.spend("xero", budget=budget)


# ------------------------------------------------------- thundering herd (M11.5.4)
def test_the_second_caller_for_one_key_is_a_follower() -> None:
    """Twenty agent runs asking the same question at the same moment produce twenty
    identical fetches, which is how a connector comfortably inside its ceiling produces a
    429 on the morning everybody arrives at once."""
    flight = SingleFlight()
    key = flight_key("xero", "invoice", (("client", "c_0447"),))
    assert flight.claim(key, now=NOW) is FlightRole.LEADER
    assert flight.claim(key, now=NOW) is FlightRole.FOLLOWER


def test_a_claim_that_was_never_released_expires() -> None:
    """A leader that crashed would otherwise make every later question for that key a
    permanent follower waiting on a fetch nobody is performing, and the connector would
    read as healthy while every question about it hung."""
    flight = SingleFlight()
    key = flight_key("xero", "invoice", ())
    flight.claim(key, now=NOW)
    later = NOW + timedelta(seconds=11)
    assert flight.claim(key, now=later) is FlightRole.LEADER


def test_releasing_a_key_nobody_holds_is_not_an_error() -> None:
    """The release belongs in a `finally` beside the fetch, and a `finally` that can raise
    replaces the real exception with its own."""
    SingleFlight().release("never-claimed")


def test_the_flight_key_does_not_depend_on_who_is_asking() -> None:
    """Coalescing is only safe because a connector never decides what a caller may see:
    every follower gets the same unredacted rows and each is redacted separately. Putting
    an entitlement hash in the key would make the coalescing useless in exactly the case it
    is for, which is twenty people asking one thing at nine in the morning."""
    assert flight_key("xero", "invoice", (("b", "2"), ("a", "1"))) == flight_key(
        "xero", "invoice", (("a", "1"), ("b", "2"))
    )


# ------------------------------------------------------- graceful degradation (M11.5.5)
def test_a_notice_names_only_a_source_the_asker_could_already_see() -> None:
    """'I could not reach the finance ledger' says a finance ledger exists and that we are
    connected to it. Asked repeatedly it enumerates the company's systems for somebody
    entitled to none of them."""
    partial = PartialAnswer(
        fetched=("freshdesk",),
        failed=(SourceFailure(connector="xero", reason=FailureReason.TIMEOUT),),
    )
    assert partial.notice(disclosable=frozenset({"xero"})) == (
        "I could not reach xero, so this answer is missing whatever it holds."
    )
    assert partial.notice(disclosable=frozenset({"freshdesk"})) == Degraded.public_message


def test_a_complete_answer_carries_no_notice_at_all() -> None:
    """A reassurance that every source answered is a list of the sources by another route,
    offered on every single request."""
    assert PartialAnswer(fetched=("xero",)).notice(disclosable=frozenset({"xero"})) == ""


def test_the_trace_keeps_every_source_and_reason() -> None:
    """The same split the redactor makes between a payload and a trace. An operator cannot
    diagnose a partial answer from the sentence the asker was given."""
    partial = PartialAnswer(
        failed=(
            SourceFailure(connector="xero", reason=FailureReason.TIMEOUT),
            SourceFailure(connector="hubspot", reason=FailureReason.CIRCUIT_OPEN, detail="open"),
        )
    )
    assert partial.trace_lines() == ("hubspot: circuit_open (open)", "xero: timeout")


# ------------------------------------------------------- classifying an outcome (M11.3.2)
def test_a_429_is_a_quota_refusal_and_not_ill_health() -> None:
    """Feeding it to the breaker opens the circuit whenever a connector is popular, so the
    busiest connector in the company becomes the intermittently unavailable one and the fix
    somebody reaches for is a longer cooldown, which makes it worse."""
    outcome = classify(status=429)
    assert outcome is CallOutcome.QUOTA
    assert not is_breaker_failure(outcome)


def test_a_timeout_and_a_5xx_are_ill_health() -> None:
    """These are the failures a breaker exists for. If neither counted, the breaker would
    never open and a dead source would be retried by every caller for as long as it stayed
    dead."""
    assert is_breaker_failure(classify(timed_out=True))
    assert is_breaker_failure(classify(status=503))
    assert is_breaker_failure(classify(connection_failed=True))


def test_a_4xx_that_is_not_a_429_is_a_rejection_and_is_never_retried() -> None:
    """The request was wrong and will be wrong again at full cost. Retrying it spends the
    ceiling on a call that cannot succeed."""
    outcome = classify(status=404)
    assert outcome is CallOutcome.REJECTED
    assert not is_retryable(outcome)
    assert not is_breaker_failure(outcome)


def test_a_freshdesk_search_at_the_cap_is_truncated_rather_than_ok() -> None:
    """300 records is a hard ceiling, not a page size, so 'all tickets matching' is silently
    wrong beyond it and looks correct in every test anybody writes."""
    assert (
        classify(status=200, returned=FRESHDESK_SEARCH_MAX_RECORDS, connector="freshdesk")
        is CallOutcome.TRUNCATED
    )
    assert classify(status=200, returned=299, connector="freshdesk") is CallOutcome.OK


def test_a_truncated_result_is_not_retryable() -> None:
    """Retrying a search that hit a hard ceiling returns the same records, and a caller who
    reads a retry as a way to see more is reading a cap as a page size."""
    assert not is_retryable(CallOutcome.TRUNCATED)


def test_a_write_without_a_read_back_is_never_retried() -> None:
    """A retried write either repeats the action or loses it, and without a read-back
    nothing anywhere can tell which happened."""
    assert not is_retryable(CallOutcome.UNAVAILABLE, side_effect=SideEffect.WRITE)
    assert is_retryable(CallOutcome.UNAVAILABLE, side_effect=SideEffect.WRITE, verifies_write=True)
    assert is_retryable(CallOutcome.UNAVAILABLE)


# ------------------------------------------------------------------ the breaker (M11.3.2)
def test_three_consecutive_unavailable_calls_open_the_breaker() -> None:
    """Without this the breaker never opens on the ordinary shape of an outage, and every
    caller keeps paying the timeout for as long as the source stays down."""
    breaker = connector_breaker("xero")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = record_outcome(breaker, CallOutcome.UNAVAILABLE, now=NOW)
    assert breaker.state is BreakerState.OPEN


def test_a_run_of_quota_refusals_never_opens_the_breaker() -> None:
    """The rate limiter is doing its job. Opening here takes a healthy connector out of
    service for being asked, which is the opposite of what a breaker is for."""
    breaker = connector_breaker("xero")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES * 3):
        breaker = record_outcome(breaker, CallOutcome.QUOTA, now=NOW)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0


def test_a_quota_refusal_is_not_recorded_as_a_success_either() -> None:
    """Counting it as a success would let a stream of 429s hold a genuinely failing
    connector's breaker closed, which is the same mistake from the other side."""
    breaker = connector_breaker("xero")
    breaker = record_outcome(breaker, CallOutcome.UNAVAILABLE, now=NOW)
    before = breaker.consecutive_failures
    breaker = record_outcome(breaker, CallOutcome.QUOTA, now=NOW)
    assert breaker.consecutive_failures == before


# ----------------------------------------------------------------- retry policy (M11.3.3)
def test_the_retry_hint_is_the_measured_wait_until_a_client_stops_reading_it() -> None:
    """Delegated whole to the platform's own backoff, so a connector cannot end up with a
    different retry policy from everything else for a reason nobody could name later."""
    assert retry_delay(retry_after_seconds=12.0, consecutive_refusals=1) == 12.0
    assert retry_delay(retry_after_seconds=12.0, consecutive_refusals=5) > 12.0


def test_jitter_only_ever_lengthens_a_retry() -> None:
    """A wait shorter than the measured one defeats the point of measuring it, and every
    refused client would come back before there was room."""
    assert retry_delay(retry_after_seconds=10.0, consecutive_refusals=0, jitter=0.5) == 15.0
    assert retry_delay(retry_after_seconds=10.0, consecutive_refusals=0, jitter=-9.0) == 10.0


# ---------------------------------------------------- the configured ceilings (M11.3.5)
def test_a_connector_gets_its_ceiling_from_the_verified_table_and_not_from_itself() -> None:
    """A connector that could declare its own ceiling would be optimistic about it, and the
    number would sit in a console beside three verified ones looking equally solid."""
    limits = limits_for(a_manifest(), principal_id="p_alice")
    subjects = {(limit.scope, limit.period, limit.limit) for limit in limits}
    assert (LimitScope.CONNECTOR, "day", 5_000) in subjects
    assert (LimitScope.CONNECTOR, "minute", 60) in subjects


def test_a_connector_naming_an_unmeasured_source_is_refused() -> None:
    """`source_limits` returns nothing for an unknown connector rather than a default, and
    a caller reading that as 'no limits apply' would run a source with no ceiling at all."""
    with pytest.raises(UnmeasuredSourceError, match="not one of the verified sources"):
        limits_for(a_manifest(name="netsuite", ceiling="netsuite"), principal_id="p_alice")


def test_a_connector_with_no_ceiling_at_all_is_refused() -> None:
    """An empty ceiling reads as 'not configured yet' and behaves as 'unlimited', which is
    the widest possible unconfigured state."""
    with pytest.raises(UnmeasuredSourceError, match="declares no ceiling"):
        limits_for(a_manifest(name="netsuite", ceiling=""), principal_id="p_alice")


def test_the_ceilings_that_no_plan_can_raise_are_marked_as_such() -> None:
    """Xero's ceiling belongs to the client's tenant and Lark Base's is fixed by their own
    documentation. An operator told a ceiling is raisable goes looking for an upgrade button
    that does not exist, during an incident."""
    assert not ceiling_for(a_manifest()).raisable
    assert not ceiling_for(a_manifest(name="lark", ceiling="lark_base")).raisable


# ---------------------------------------------------------------------- metrics (M11.3.4)
def test_metrics_cover_every_number_the_architecture_asks_for() -> None:
    """Requests per second and minute, concurrency, the 429 rate, latency and the error
    rate. A missing one is a number nobody notices is absent until an incident needs it."""
    records = (
        CallRecord(at=NOW, connector="xero", outcome=CallOutcome.OK, latency_ms=100.0),
        CallRecord(at=NOW, connector="xero", outcome=CallOutcome.QUOTA, latency_ms=20.0),
        CallRecord(at=NOW, connector="xero", outcome=CallOutcome.UNAVAILABLE, latency_ms=900.0),
    )
    metrics = measure("xero", records, now=NOW, concurrency=2)
    assert metrics.requests == 3
    assert metrics.per_minute == pytest.approx(3.0)
    assert metrics.per_second == pytest.approx(0.05)
    assert metrics.concurrency == 2
    assert metrics.quota_ratio == pytest.approx(1 / 3)
    assert metrics.error_ratio == pytest.approx(1 / 3)
    assert metrics.latency_p95_ms == 900.0


def test_another_connectors_calls_do_not_reach_this_connectors_metrics() -> None:
    """Expecting the caller to filter means the filter is applied in some call sites and
    not others, and the metric that reads as a spike is the one where somebody forgot."""
    records = (
        CallRecord(at=NOW, connector="xero", outcome=CallOutcome.OK, latency_ms=10.0),
        CallRecord(at=NOW, connector="freshdesk", outcome=CallOutcome.UNAVAILABLE, latency_ms=1.0),
    )
    assert measure("xero", records, now=NOW).error_ratio == 0.0


def test_calls_outside_the_window_are_dropped() -> None:
    """A window that never forgets is a total, and a total compared against a per-minute
    ceiling reads as an emergency on a busy afternoon."""
    old = CallRecord(
        at=NOW - timedelta(minutes=5), connector="xero", outcome=CallOutcome.OK, latency_ms=10.0
    )
    assert measure("xero", (old,), now=NOW).requests == 0


def test_a_quiet_window_says_so_rather_than_reporting_a_ratio() -> None:
    """One failure in two is a 50% error rate and is also two calls. A dashboard that pages
    on it teaches people to ignore the dashboard."""
    records = (
        CallRecord(at=NOW, connector="xero", outcome=CallOutcome.UNAVAILABLE, latency_ms=5.0),
    )
    assert measure("xero", records, now=NOW).is_quiet


def test_a_percentile_is_a_latency_that_actually_happened() -> None:
    """Interpolating returns a number that did not occur, which at these volumes is most of
    the time: eleven calls have no ninety-fifth percentile to interpolate towards, and the
    invented number is then compared against a target and acted on."""
    latencies = [10.0, 20.0, 30.0, 40.0]
    assert percentile_ms(latencies, 0.95) in latencies
    assert percentile_ms(latencies, 0.5) == 20.0
    assert percentile_ms([], 0.95) == 0.0
