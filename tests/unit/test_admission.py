"""Capacity admission: the budgets, the classes, the queue and the capacity model.

This is `brain.ops.admission`, which decides whether there is room to start. It is not
`brain.gate.admission`, which decides what a channel and a sign-in take away from what a
person holds; those are covered in `tests/invariants/test_admission_invariants.py` and the
two have nothing in common but a word.

The rules that must never break (interactive shed last, a person waiting never queued, a
capacity refusal never looking like a permission one) live in
`tests/invariants/test_capacity_invariants.py`. What is here is the ordinary behaviour:
the arithmetic, the branches, and the refusals.

Task ids: M22.1.1, M22.1.2, M22.1.3, M22.1.4, M22.1.5, M22.2.1, M22.2.2, M22.2.3,
M22.2.4, M22.3.1, M22.3.2, M22.3.4
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.core.errors import Outcome
from brain.core.lane import Lane
from brain.gate.context import TrafficClass
from brain.ops.admission import (
    CLASS_CEILING,
    FIRST_BOTTLENECK_AT_SCALE,
    INGESTION_THROTTLE_IS_THE_CLASS_CEILING,
    LOAD_TEST_TARGET,
    OPERATOR_ACTION,
    POOL_SHARE,
    SHED_ORDER,
    AdmissionRequest,
    Budget,
    BudgetKind,
    CapacityProfile,
    CapacityRefused,
    CapacityState,
    Ceiling,
    Demand,
    RefusalKind,
    Resource,
    Verdict,
    WorkloadClass,
    bottleneck_ladder,
    budget_for,
    decide,
    first_bottleneck,
    headroom,
    kind_of,
    little_law_concurrency,
    narrower_of,
    pools_for,
    seed_budgets,
    seed_profiles,
    shed_plan,
    workload_class_for,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
BUDGETS = seed_budgets()


def _request(
    *,
    resource: Resource = Resource.MODEL_CALLS,
    traffic: TrafficClass = TrafficClass.HUMAN_INTERACTIVE,
    lane: Lane = Lane.ANSWER,
    key: str = "",
    units: int = 1,
) -> AdmissionRequest:
    return AdmissionRequest(
        trace_id="tr_1",
        lane=lane,
        traffic_class=traffic,
        resource=resource,
        key=key,
        units=units,
    )


def _state(resource: Resource, used: int, *, key: str = "", queued: int = 0) -> CapacityState:
    return CapacityState(used={(resource, key): used}, queued={(resource, key): queued})


# ------------------------------------------------------------------------ budgets as rows
def test_every_resource_the_architecture_budgets_has_a_seed_row() -> None:
    """M22.1.1. The seven budgeted resources come from §25 verbatim. A member added to the
    enum with no row would be admitted against nothing, and `decide` would shed every
    request for it with a message nobody expects: the failure would look like an outage."""
    assert {b.resource for b in BUDGETS} == set(Resource)


def test_every_budget_row_carries_the_argument_for_its_number() -> None:
    """M22.1.2. A limit with no reason attached is changed by whoever is next annoyed by it,
    and nobody can tell afterwards what it was answering."""
    for budget in BUDGETS:
        assert budget.reason, f"{budget.resource}/{budget.key} has no reason"


def test_the_lark_base_row_is_sized_by_littles_law_against_a_fixed_ceiling() -> None:
    """M22.1.1, M22.3.2. Lark Base is 100 requests a minute and cannot be raised: 1.67 a
    second, and at the 5s connector timeout that is 8.3 in flight. A larger number here
    would produce only 429s, and nothing in the system would say why."""
    lark = budget_for(BUDGETS, (Resource.SOURCE_CALLS, "lark_base"))
    assert lark is not None
    assert lark.limit == 8
    assert little_law_concurrency(100 / 60, lark.mean_service_seconds) == pytest.approx(8.33, 0.01)


def test_a_rate_budget_without_a_window_is_refused() -> None:
    """A rate limit with no window is not a limit. Without this the tokens-per-minute row
    would count for ever and admit once, then never again."""
    with pytest.raises(ValueError, match="needs a window"):
        Budget(resource=Resource.TOKENS_PER_MINUTE, limit=1000)


def test_a_concurrency_budget_with_a_window_is_refused() -> None:
    """A concurrency budget with a window reads as a rate limit and behaves as neither, and
    the retry hint it produces is wrong in whichever direction the reader did not expect."""
    with pytest.raises(ValueError, match="things in flight"):
        Budget(resource=Resource.MODEL_CALLS, limit=10, window_seconds=60.0)


def test_a_budget_of_zero_is_refused_because_it_reads_as_configured() -> None:
    """Switching a resource off by editing its limit to zero is invisible in a console: the
    row still reads as a budget. Removing the capability is a different change."""
    with pytest.raises(ValueError, match="minimum is 1"):
        Budget(resource=Resource.MODEL_CALLS, limit=0)


def test_a_globally_budgeted_resource_cannot_be_keyed_by_connector() -> None:
    """Model calls are budgeted once. A row keyed by connector would never be found by
    `budget_for`, so the console would show a limit that nothing consults."""
    with pytest.raises(ValueError, match="budgeted once globally"):
        Budget(resource=Resource.MODEL_CALLS, limit=10, key="xero")


def test_only_source_calls_are_budgeted_per_connector() -> None:
    """§25 budgets 'concurrent source calls per connector' and everything else globally.
    Widening this set silently turns a global ceiling into a per-key one, which is a much
    larger total limit wearing the same number."""
    assert kind_of(Resource.SOURCE_CALLS) is BudgetKind.CONCURRENCY
    assert kind_of(Resource.TOKENS_PER_MINUTE) is BudgetKind.RATE


# --------------------------------------------------------------------------- the request
def test_a_source_call_must_name_its_connector() -> None:
    """M22.1.1. An unkeyed source-call counter is one global pool, which lets a Xero
    backfill occupy every slot and starve Freshdesk while the console reports only that the
    system is busy."""
    with pytest.raises(ValueError, match="no connector was named"):
        _request(resource=Resource.SOURCE_CALLS)


def test_a_globally_budgeted_request_cannot_carry_a_connector() -> None:
    """A key on a global resource has nowhere to go, so it would be silently ignored and
    the caller would believe a per-connector limit was being applied."""
    with pytest.raises(ValueError, match="nowhere to go"):
        _request(resource=Resource.BROWSER_SESSIONS, key="xero")


def test_the_fast_lane_cannot_ask_for_a_model_call_slot() -> None:
    """§25 says FAST still runs under pressure, and the reason it can is that it uses no
    model. A fast-lane request asking for a model-call slot means the lane classification
    and this call disagree; admitting it would put a model on a path with none of the guards
    a model path has."""
    with pytest.raises(ValueError, match="fast lane uses no model"):
        _request(lane=Lane.FAST)


def test_a_request_for_no_units_is_refused() -> None:
    """A zero-unit request consumes nothing and is always admitted, which makes it a way to
    pass admission without asking for anything and then do the work anyway."""
    with pytest.raises(ValueError, match="minimum is 1"):
        _request(units=0)


# -------------------------------------------------------------------- classes and lanes
@pytest.mark.parametrize("traffic", list(TrafficClass))
def test_every_traffic_class_maps_to_a_workload_class(traffic: TrafficClass) -> None:
    """M22.2.1. `workload_class_for` uses `assert_never`, so a new traffic class cannot
    compile without somebody deciding which pool it shares. Deleting this stops that being
    exercised at all, and a match statement that never runs proves nothing."""
    assert workload_class_for(traffic, Lane.ANSWER) in set(WorkloadClass)


@pytest.mark.parametrize("traffic", list(TrafficClass))
def test_the_task_lane_can_only_lower_a_requests_class(traffic: TrafficClass) -> None:
    """§25 puts tasks and agents in background, but a scheduler-launched task is already
    batch and promoting it would hand housekeeping a larger share than it had. Composition
    here only ever subtracts, like every other ceiling in the system."""
    plain = workload_class_for(traffic, Lane.ANSWER)
    tasked = workload_class_for(traffic, Lane.TASK)
    assert SHED_ORDER.index(tasked) <= SHED_ORDER.index(plain)


def test_a_console_question_is_interactive_and_a_scheduled_one_is_batch() -> None:
    """The two ends of the ladder. If these collapsed into one class, the whole shedding
    mechanism would have nothing to order."""
    assert workload_class_for(TrafficClass.HUMAN_INTERACTIVE, Lane.ANSWER) is (
        WorkloadClass.INTERACTIVE
    )
    assert workload_class_for(TrafficClass.SYSTEM, Lane.ANSWER) is WorkloadClass.BATCH


def test_narrowing_two_classes_takes_the_one_that_gives_way_first() -> None:
    """`narrower_of` is how the task lane lowers a class. If it took the higher one, a
    console-launched task would keep interactive's whole-budget share."""
    assert narrower_of(WorkloadClass.INTERACTIVE, WorkloadClass.BATCH) is WorkloadClass.BATCH
    assert narrower_of(WorkloadClass.BACKGROUND, WorkloadClass.BACKGROUND) is (
        WorkloadClass.BACKGROUND
    )


# ------------------------------------------------------------------------- the decision
def test_a_request_inside_its_class_share_is_admitted() -> None:
    """The ordinary case. Without it, a change that refuses everything would still pass
    every refusal test in this file."""
    decision = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 10), now=NOW)
    assert decision.verdict is Verdict.ADMITTED
    assert decision.admitted
    assert decision.decided_at == NOW


def test_a_person_waiting_is_shed_rather_than_queued() -> None:
    """The spine of the module. A person watching a cursor is told the truth now; a queue
    position is a promise nobody is there to collect, and it costs them the whole wait
    before they find out."""
    decision = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW)
    assert decision.verdict is Verdict.SHED
    assert decision.queue is None
    assert decision.retry_after_seconds is not None


def test_nobody_waiting_is_queued_with_a_position_and_an_estimate() -> None:
    """M22.1.4. A queue with no visible position is a queue nobody can reason about, and
    the first question during an incident is always how far back the work is."""
    decision = decide(
        _request(traffic=TrafficClass.SYSTEM),
        BUDGETS,
        _state(Resource.MODEL_CALLS, 32, queued=3),
        now=NOW,
    )
    assert decision.verdict is Verdict.QUEUED
    assert decision.queue is not None
    assert decision.queue.position == 4
    assert decision.queue.expected_wait_seconds > 0


def test_a_console_launched_task_is_queued_because_nobody_waits_on_the_run() -> None:
    """M22.1.3. §25 says TASK queues. The person who pressed the button is waiting for the
    sub-second acknowledgement, not for the run, so shedding it throws away work that could
    have been done at three in the morning."""
    decision = decide(
        _request(traffic=TrafficClass.HUMAN_INTERACTIVE, lane=Lane.TASK),
        BUDGETS,
        _state(Resource.MODEL_CALLS, 40),
        now=NOW,
    )
    assert decision.verdict is Verdict.QUEUED
    assert decision.workload_class is WorkloadClass.BACKGROUND


def test_a_resource_with_no_budget_row_is_shed_rather_than_admitted() -> None:
    """An unbudgeted resource is exactly the failure §25 exists to prevent: each subsystem
    consuming until Postgres or memory gives out. Admitting on a missing row would restore
    that, and it would look like a configuration gap rather than an outage."""
    decision = decide(_request(), (), CapacityState(), now=NOW)
    assert decision.verdict is Verdict.SHED
    assert decision.budget is None
    assert "no budget row" in decision.reason


def test_a_connector_without_a_row_of_its_own_falls_back_to_the_default() -> None:
    """A connector nobody has measured still needs a ceiling, and a low default is the
    honest one. Without the fallback every new connector would be shed entirely on its
    first call."""
    decision = decide(
        _request(resource=Resource.SOURCE_CALLS, key="hubspot"),
        BUDGETS,
        CapacityState(),
        now=NOW,
    )
    assert decision.verdict is Verdict.ADMITTED
    assert decision.budget is not None
    assert decision.budget.key == ""
    assert decision.ceiling == 4


def test_a_measured_connector_never_inherits_the_default_row() -> None:
    """Xero's ceiling is derived from a verified 60 calls a minute. Falling back to the
    conservative default for it would halve its throughput with nothing anywhere saying so."""
    xero = budget_for(BUDGETS, (Resource.SOURCE_CALLS, "xero"))
    assert xero is not None
    assert xero.limit == 5


def test_the_queue_estimate_counts_how_far_over_the_ceiling_the_budget_is() -> None:
    """The textbook `position * service / servers` ignores the overshoot and told a batch
    request refused at 32-against-20 to come back in 0.2 seconds, when thirteen things had
    to finish first. An estimate short by an order of magnitude is worse than none: the
    caller comes straight back and is refused again."""
    decision = decide(
        _request(traffic=TrafficClass.SYSTEM),
        BUDGETS,
        _state(Resource.MODEL_CALLS, 32),
        now=NOW,
    )
    assert decision.queue is not None
    # 13 departures needed at 32/4 = 8 a second.
    assert decision.queue.expected_wait_seconds == pytest.approx(13 / 8)


def test_a_deeper_queue_position_waits_longer() -> None:
    """A position that does not affect the estimate is a position that means nothing, and
    everybody in the queue would be told the same time."""
    shallow = decide(
        _request(traffic=TrafficClass.SYSTEM),
        BUDGETS,
        _state(Resource.MODEL_CALLS, 32, queued=0),
        now=NOW,
    )
    deep = decide(
        _request(traffic=TrafficClass.SYSTEM),
        BUDGETS,
        _state(Resource.MODEL_CALLS, 32, queued=20),
        now=NOW,
    )
    assert shallow.queue is not None
    assert deep.queue is not None
    assert deep.queue.expected_wait_seconds > shallow.queue.expected_wait_seconds


def test_a_rate_budget_waits_whole_windows() -> None:
    """Nothing departs from a rate budget until the window rolls, so a hint measured in
    service times would send the caller back inside the same window every time."""
    decision = decide(
        _request(resource=Resource.TOKENS_PER_MINUTE, traffic=TrafficClass.AUTOMATION, units=12000),
        BUDGETS,
        _state(Resource.TOKENS_PER_MINUTE, 199_000),
        now=NOW,
    )
    assert decision.verdict is Verdict.QUEUED
    assert decision.queue is not None
    assert decision.queue.expected_wait_seconds == 60.0


def test_the_shed_hint_lengthens_with_jitter_and_never_shortens() -> None:
    """Jitter decorrelates a hundred clients refused in the same second. Negative jitter
    would shorten a hint below the time a slot can possibly free, which defeats the point
    of giving one."""
    plain = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW)
    jittered = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW, jitter=0.5)
    negative = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW, jitter=-5.0)
    assert plain.retry_after_seconds is not None
    assert jittered.retry_after_seconds is not None
    assert negative.retry_after_seconds is not None
    assert jittered.retry_after_seconds > plain.retry_after_seconds
    assert negative.retry_after_seconds == plain.retry_after_seconds


def test_utilisation_is_measured_against_the_class_share() -> None:
    """Against the whole budget it reads low while a class is already being refused, which
    is the number that makes an operator say the machine is idle while half the estate
    queues."""
    decision = decide(
        _request(traffic=TrafficClass.SYSTEM), BUDGETS, _state(Resource.MODEL_CALLS, 20), now=NOW
    )
    # Batch's share of the 40-call budget is 20, so 20 used is 100% of it, not 50%.
    assert decision.utilisation == pytest.approx(1.0)


# -------------------------------------------------------------------------- the refusal
def test_a_capacity_refusal_names_the_resource_and_the_action() -> None:
    """A refusal for capacity and a refusal for permission read alike to the person asking.
    To an operator they must not: one says add capacity, the other says the permission model
    is working. Without the record, a busy afternoon looks like a burst of access failures."""
    decision = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW)
    record = decision.log_record()
    assert record["refusal_kind"] == RefusalKind.CAPACITY
    assert record["operator_action"] == OPERATOR_ACTION[RefusalKind.CAPACITY]
    assert "model_calls" in record["subject"]


def test_an_admitted_decision_logs_what_it_used_and_names_no_refusal() -> None:
    """An admitted request that logged a refusal kind would poison every count of refusals
    on the dashboard, and the count is what triggers the alert."""
    record = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 1), now=NOW).log_record()
    assert record["verdict"] == "admitted"
    assert "refusal_kind" not in record


def test_a_capacity_refusal_is_not_a_permission_refusal() -> None:
    """`CapacityRefused` carries FAILED, never DENIED. DENIED is the outcome meaning 'this
    exists and you may not see it', and the audit log is the only place that distinction
    survives; collapsing capacity into it corrupts the signal."""
    decision = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 40), now=NOW)
    error = decision.as_error()
    assert isinstance(error, CapacityRefused)
    assert error.outcome is Outcome.FAILED
    assert "busy" in error.public_message


def test_an_admitted_decision_has_no_refusal_to_raise() -> None:
    """Calling `as_error` on an admission is a caller bug, and returning a plausible-looking
    error for it would let a mistaken branch refuse a request that was admitted."""
    decision = decide(_request(), BUDGETS, _state(Resource.MODEL_CALLS, 1), now=NOW)
    with pytest.raises(ValueError, match="no refusal to raise"):
        decision.as_error()


# ------------------------------------------------------------------------- the shed plan
def test_the_shed_plan_names_what_was_deferred() -> None:
    """M22.1.5. A count of refusals names nothing. During an incident the question is which
    class is giving way on which resource, and a number cannot answer it."""
    notices = shed_plan(BUDGETS, _state(Resource.MODEL_CALLS, 32))
    assert notices
    assert all("deferred" in n.deferred for n in notices)
    assert {n.workload_class for n in notices} == {WorkloadClass.BATCH, WorkloadClass.BACKGROUND}


def test_the_shed_plan_lists_the_class_that_gives_way_first_first() -> None:
    """Read top to bottom it is the order the system will fail in. Sorted any other way it
    is a list somebody has to decode before it helps."""
    notices = shed_plan(BUDGETS, _state(Resource.MODEL_CALLS, 40))
    classes = [n.workload_class for n in notices if n.resource is Resource.MODEL_CALLS]
    assert classes == [c for c in SHED_ORDER if c in classes]


def test_an_idle_system_defers_nothing() -> None:
    """A shed plan that always has entries is a dashboard nobody looks at."""
    assert shed_plan(BUDGETS, CapacityState()) == ()


# ------------------------------------------------------------- ingestion and the pools
def test_batch_headroom_is_the_whole_of_ingestion_throttling() -> None:
    """M22.2.4. Ingestion is throttled by BATCH's share of the document-job budget and by
    nothing else. A second knob would drift from this one, and the operator watching a
    stalled parse could not tell which of them stopped it."""
    assert "rejected" in INGESTION_THROTTLE_IS_THE_CLASS_CEILING
    empty = headroom(BUDGETS, CapacityState(), (Resource.DOCUMENT_JOBS, ""), WorkloadClass.BATCH)
    busy = headroom(
        BUDGETS,
        _state(Resource.DOCUMENT_JOBS, 2),
        (Resource.DOCUMENT_JOBS, ""),
        WorkloadClass.BATCH,
    )
    assert empty == 2  # half of the four-job budget
    assert busy == 0


def test_headroom_for_a_resource_with_no_row_is_nothing() -> None:
    """Reporting headroom against a budget that does not exist would let a caller start work
    the admission controller would have refused."""
    assert headroom((), CapacityState(), (Resource.MODEL_CALLS, ""), WorkloadClass.INTERACTIVE) == 0


def test_pools_give_each_class_at_least_one_connection() -> None:
    """M22.2.2. A class with no pool cannot run at all, and the console still shows three
    pools. Separate pools are what stop a long batch transaction holding the connection the
    request path needs."""
    pools = pools_for(20)
    assert pools.slots_for(WorkloadClass.INTERACTIVE) == 12
    assert pools.slots_for(WorkloadClass.BACKGROUND) == 5
    assert pools.slots_for(WorkloadClass.BATCH) == 3
    assert pools.total == 20


@pytest.mark.parametrize("slots", [3, 4, 7, 10, 20, 33, 64])
def test_pools_never_overcommit_the_slots_that_exist(slots: int) -> None:
    """Rounding each share independently overcommits the pooler, and the symptom of an
    overcommitted pooler is a connection error on the request path rather than a queue."""
    pools = pools_for(slots)
    assert pools.total == slots
    assert min(pools.interactive, pools.background, pools.batch) >= 1


def test_a_pool_too_small_to_split_refuses_rather_than_pretending() -> None:
    """Reporting three pools of which two are empty is worse than saying the box is too
    small: the isolation the console claims would not exist."""
    with pytest.raises(ValueError, match="isolates nothing"):
        pools_for(2)


def test_the_pool_shares_are_a_whole() -> None:
    """Shares that do not sum to one leave slots unassigned or overcommit them, and neither
    shows up until the pooler refuses a connection."""
    assert sum(POOL_SHARE.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------- the capacity model
def test_little_law_sizes_against_the_peak_rather_than_the_daily_mean() -> None:
    """M22.3.1. Five thousand questions a day is 0.06 a second and sizes a machine that is
    never busy. What queues is the busiest minute."""
    daily_mean = little_law_concurrency(5000 / 86_400, 4.0)
    peak = little_law_concurrency(2.0, 4.0)
    assert daily_mean < 1
    assert peak == 8.0


def test_little_law_refuses_negative_inputs() -> None:
    """A negative arrival rate produces a negative concurrency, which reads as spare
    capacity and would size a profile downwards."""
    with pytest.raises(ValueError, match="non-negative"):
        little_law_concurrency(-1.0, 4.0)


def test_every_profile_records_the_arithmetic_behind_its_sizing() -> None:
    """M22.3.2. A sizing with no working shown cannot be argued with, so it gets changed by
    whoever is next surprised by it."""
    for profile in seed_profiles():
        assert profile.reason
        assert profile.servers_needed >= 1


def test_a_profile_rounds_a_partial_server_up() -> None:
    """Half a slot serves nobody. Rounding down sizes every profile just under its own
    peak."""
    profile = CapacityProfile(
        name="t", peak_arrivals_per_second=0.3, mean_service_seconds=4.0, reason="test"
    )
    assert profile.concurrency == pytest.approx(1.2)
    assert profile.servers_needed == 2


def test_the_load_test_target_is_a_number_and_a_pass_condition() -> None:
    """M22.3.3. 'Load test the system' is not a specification. Without a stated peak and a
    stated pass condition, the test measures whatever the person running it happened to
    drive."""
    assert "2 arrivals a second" in LOAD_TEST_TARGET
    assert "99.5%" in LOAD_TEST_TARGET


# --------------------------------------------------------------------- bottleneck ladder
def test_nothing_binds_while_every_source_has_headroom() -> None:
    """M22.3.4. Returning the least-headroom source anyway would make every dashboard show a
    permanent red bottleneck, and a permanent alert is not an alert."""
    demands = (Demand(ceiling=Ceiling(name="x", per_day=5_000), calls_per_day=100),)
    assert first_bottleneck(demands, multiplier=10.0) is None


def test_the_first_bottleneck_is_the_one_reached_earliest() -> None:
    """A ladder that reported the largest demand rather than the smallest headroom would
    send an operator to the busiest source instead of the one about to fail."""
    demands = (
        Demand(ceiling=Ceiling(name="roomy", per_day=1_000_000), calls_per_day=10_000),
        Demand(ceiling=Ceiling(name="tight", per_day=5_000), calls_per_day=500),
    )
    bottleneck = first_bottleneck(demands, multiplier=10.0)
    assert bottleneck is not None
    assert bottleneck.ceiling.name == "tight"
    assert bottleneck.binds_at == pytest.approx(10.0)


def test_a_source_nobody_calls_is_not_a_bottleneck_at_any_scale() -> None:
    """Zero demand against any ceiling is infinite headroom. Treating it as a division by
    zero would put an unused connector at the top of the ladder for ever."""
    demands = (Demand(ceiling=Ceiling(name="idle", per_day=10), calls_per_day=0),)
    assert first_bottleneck(demands, multiplier=100.0) is None


def test_an_unraisable_ceiling_says_so_in_its_reason() -> None:
    """An alert about a ceiling no plan moves must not send anybody looking for an upgrade
    button that does not exist."""
    demands = (Demand(ceiling=Ceiling(name="lark", per_day=100, raisable=False), calls_per_day=50),)
    bottleneck = first_bottleneck(demands, multiplier=10.0)
    assert bottleneck is not None
    assert "no plan raises it" in bottleneck.reason


def test_a_derived_daily_ceiling_says_it_was_derived() -> None:
    """A daily figure calculated from a per-minute one assumes even arrival, which office
    traffic is not. Reporting it as though the vendor published it is how a source is
    declared safe at 40x and starts returning 429s at 8x."""
    ceiling = Ceiling(name="minutely", per_day=144_000, derived=True)
    bottleneck = first_bottleneck((Demand(ceiling=ceiling, calls_per_day=20_000),), multiplier=10.0)
    assert bottleneck is not None
    assert "derived" in bottleneck.reason


def test_the_ladder_answers_ten_and_a_hundred_times() -> None:
    """M22.3.4 asks for both, and they are usually different answers: ten is a ceiling
    somebody can buy past, a hundred is usually one nobody can."""
    demands = (Demand(ceiling=Ceiling(name="tight", per_day=5_000), calls_per_day=500),)
    ladder = bottleneck_ladder(demands)
    assert [m for m, _ in ladder] == [10.0, 100.0]
    assert all(b is not None for _, b in ladder)


def test_a_multiplier_of_zero_is_refused() -> None:
    """Zero times today's volume is not a scale question, and it would report every ceiling
    as bound because zero demand meets no ceiling at all."""
    with pytest.raises(ValueError, match="positive"):
        first_bottleneck((), multiplier=0.0)


def test_the_documented_bottleneck_names_xero_and_lark() -> None:
    """M22.3.4 asks for the first bottleneck to be documented, not computed on request. A
    ladder with no written conclusion leaves the reader to work out which of two answers
    matters."""
    assert "Xero" in FIRST_BOTTLENECK_AT_SCALE
    assert "Lark Base" in FIRST_BOTTLENECK_AT_SCALE
    assert "5,000" in FIRST_BOTTLENECK_AT_SCALE


def test_the_class_ceilings_are_ordered_and_interactive_is_the_widest() -> None:
    """The ordering is the whole shedding policy. Reversed, batch would outlive the request
    path under load and nothing in the system would look wrong."""
    assert CLASS_CEILING[WorkloadClass.INTERACTIVE] == 1.0
    assert (
        CLASS_CEILING[WorkloadClass.BATCH]
        < CLASS_CEILING[WorkloadClass.BACKGROUND]
        < CLASS_CEILING[WorkloadClass.INTERACTIVE]
    )
