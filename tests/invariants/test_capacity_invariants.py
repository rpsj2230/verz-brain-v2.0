"""Rules the capacity and limiting layers must never break.

Six properties. Each one is here rather than in the unit files because breaking it produces
no error and no wrong answer. It produces a system that looks like it is protecting itself
and is not.

1. **The interactive class is shed last.** Reverse the class ceilings and a bulk re-index
   keeps running while people watching a cursor are turned away, and every dashboard still
   reads "the system is busy". Nothing else in the code would look wrong.
2. **A person waiting is never handed a queue position, and nobody waiting is never shed.**
   The first wastes a person's whole wait before telling them anything; the second throws
   away work that could have run at three in the morning for free.
3. **One principal cannot exhaust a connector for everybody, and one connector cannot be
   exhausted by the sum of everybody being individually reasonable.** Two different
   failures, and neither limit catches the other's.
4. **Abuse detection has nowhere to express a refusal.** Same rule as the injection
   classifier: a heuristic that refuses teaches legitimate users to work around it while
   anybody adapting deliberately walks through it.
5. **A refusal for capacity is not a refusal for permission.** To an operator one says add
   capacity and the other says the permission model is working. Collapsing them corrupts
   the one signal that says access control is doing its job.
6. **The verified source ceilings are not rounded.** Xero's 5,000 a day, Lark Base's 100 a
   minute that no plan raises, and Freshdesk's 300-record search cap are facts. A number
   rounded up here produces 429s in production and looks fine in review.

Plus the structural guards: nothing here reads a clock, sleeps, spawns a thread or imports
`random`, because every one of those turns a pure state machine into something that cannot
be tested for the case that goes wrong.

Task ids: M22.1.1, M22.1.2, M22.1.3, M22.1.4, M22.1.5, M22.2.1, M22.2.2, M22.2.3,
M22.3.1, M22.3.2, M22.3.4, M23.1.1, M23.1.2, M23.1.3, M23.1.4, M23.2.1, M23.2.2, M23.2.3
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.errors import Denied, Outcome
from brain.core.lane import Lane
from brain.gate.context import TrafficClass
from brain.ops import admission as admission_module
from brain.ops import limits as limits_module
from brain.ops.admission import (
    A_PERSON_WAITING_IS_NEVER_QUEUED,
    ADMISSION_DECIDES_BEFORE_WORK_STARTS,
    CAPACITY_IS_NOT_PERMISSION,
    CLASS_CEILING,
    INGESTION_THROTTLE_IS_THE_CLASS_CEILING,
    OPERATOR_ACTION,
    SHED_ORDER,
    AdmissionRequest,
    Budget,
    CapacityRefused,
    CapacityState,
    Demand,
    RefusalKind,
    Resource,
    Verdict,
    WorkloadClass,
    bottleneck_ladder,
    decide,
    pools_for,
    seed_budgets,
    workload_class_for,
)
from brain.ops.limits import (
    ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE,
    AUTOMATED_TRAFFIC_IS_DECLARED_NOT_SNIFFED,
    BOTH_LIMITS_APPLY,
    FRESHDESK_SEARCH_MAX_RECORDS,
    LIMITS_ARE_CHECKED_BEFORE_CAPACITY,
    MINUTE_SECONDS,
    REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW,
    SOURCE_CEILINGS,
    THE_HINT_IS_MEASURED_NOT_GUESSED,
    DenialAssessment,
    LimiterState,
    LimitScope,
    QuotaExceeded,
    VolumeAssessment,
    assess_denials,
    assess_volume,
    ceilings,
    check,
    principal_share_of,
    search_completeness,
    source_limits,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
BUDGETS = seed_budgets()

#: Everything that is not the request path, for the "shed last" comparisons.
LOWER_CLASSES = tuple(c for c in WorkloadClass if c is not WorkloadClass.INTERACTIVE)


def _request(
    resource: Resource,
    traffic: TrafficClass,
    *,
    lane: Lane = Lane.ANSWER,
    key: str = "",
) -> AdmissionRequest:
    return AdmissionRequest(
        trace_id="tr", lane=lane, traffic_class=traffic, resource=resource, key=key
    )


def _traffic_for(workload: WorkloadClass) -> TrafficClass:
    """A traffic class that lands in this workload class on the answer lane."""
    match workload:
        case WorkloadClass.INTERACTIVE:
            return TrafficClass.HUMAN_INTERACTIVE
        case WorkloadClass.BACKGROUND:
            return TrafficClass.AUTOMATION
        case WorkloadClass.BATCH:
            return TrafficClass.SYSTEM
    raise AssertionError(workload)


# ------------------------------------------------------------------ 1. shed last of all
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 8, 10, 16, 40, 64, 200_000])
def test_an_interactive_request_is_shed_last(limit: int) -> None:
    """The headline rule, stated behaviourally rather than as a comparison of constants.

    At every level of usage, an interactive request that is refused implies every other
    class is refused too. Reverse the ceilings and a bulk re-index outlives the request
    path under load, with nothing in the code looking wrong and every dashboard still
    reporting only that the system is busy.
    """
    budget = Budget(resource=Resource.MODEL_CALLS, limit=limit, mean_service_seconds=4.0)
    for used in range(0, limit + 3):
        state = CapacityState(used={(Resource.MODEL_CALLS, ""): used})
        interactive = decide(
            _request(Resource.MODEL_CALLS, TrafficClass.HUMAN_INTERACTIVE),
            (budget,),
            state,
            now=NOW,
        )
        if interactive.admitted:
            continue
        for workload in LOWER_CLASSES:
            other = decide(
                _request(Resource.MODEL_CALLS, _traffic_for(workload)), (budget,), state, now=NOW
            )
            assert not other.admitted, (
                f"{workload} was still admitted at {used}/{limit} while the request path "
                "was being turned away"
            )


@pytest.mark.parametrize("limit", [1, 2, 4, 8, 40, 5_000])
def test_interactive_may_use_the_whole_of_every_budget(limit: int) -> None:
    """A reserve held back from the request path is capacity bought and never used by the
    only class anybody is waiting on. Interactive's share is 1.0 and must stay 1.0."""
    budget = Budget(resource=Resource.MODEL_CALLS, limit=limit, mean_service_seconds=4.0)
    assert budget.ceiling_for(WorkloadClass.INTERACTIVE) == limit


def test_every_seed_budget_gives_the_request_path_its_whole_limit() -> None:
    """The same rule across the real rows, so a row added with an odd limit cannot quietly
    reserve part of itself away from interactive traffic."""
    for budget in BUDGETS:
        assert budget.ceiling_for(WorkloadClass.INTERACTIVE) == budget.limit


def test_the_shed_order_is_derived_from_the_ceilings_and_cannot_disagree() -> None:
    """A hand-written order beside a set of thresholds drifts, and the order is the one that
    looks authoritative in review while the thresholds are the one the code obeys."""
    assert tuple(sorted(WorkloadClass, key=lambda c: CLASS_CEILING[c])) == SHED_ORDER
    assert set(SHED_ORDER) == set(WorkloadClass)
    assert SHED_ORDER[-1] is WorkloadClass.INTERACTIVE


@pytest.mark.parametrize(("traffic", "lane"), list(itertools.product(TrafficClass, Lane)))
def test_no_traffic_class_and_lane_combination_escapes_a_workload_class(
    traffic: TrafficClass, lane: Lane
) -> None:
    """Every combination lands in exactly one pool. A combination with no class would have
    no ceiling, which is an unbudgeted request wearing a budget's name."""
    assert workload_class_for(traffic, lane) in set(WorkloadClass)


# ----------------------------------------------------- 2. queue or shed, never both ways
@pytest.mark.parametrize("resource", [r for r in Resource if r is not Resource.SOURCE_CALLS])
def test_a_person_waiting_is_never_handed_a_queue_position(resource: Resource) -> None:
    """`TrafficClass.HUMAN_INTERACTIVE` says degrade visibly and never queue. A position
    handed to somebody watching a cursor costs them the whole wait before they learn
    anything, and they will not come back to collect it."""
    budget = next(b for b in BUDGETS if b.resource is resource and not b.key)
    for used in (0, budget.limit // 2, budget.limit, budget.limit * 2):
        state = CapacityState(used={(resource, ""): used}, queued={(resource, ""): 5})
        decision = decide(
            _request(resource, TrafficClass.HUMAN_INTERACTIVE), BUDGETS, state, now=NOW
        )
        assert decision.queue is None
        assert decision.verdict in (Verdict.ADMITTED, Verdict.SHED)


@pytest.mark.parametrize("traffic", [TrafficClass.HUMAN_ASYNC, TrafficClass.AUTOMATION])
def test_nobody_waiting_is_never_shed_while_a_budget_row_exists(traffic: TrafficClass) -> None:
    """Work nobody is waiting for can be retried for free. Shedding it throws away an answer
    that would have cost nothing to produce later, and the person who asked by email finds
    out only when nothing arrives."""
    for used in (0, 20, 40, 400):
        state = CapacityState(used={(Resource.MODEL_CALLS, ""): used})
        decision = decide(_request(Resource.MODEL_CALLS, traffic), BUDGETS, state, now=NOW)
        assert decision.verdict is not Verdict.SHED


@pytest.mark.parametrize("traffic", list(TrafficClass))
def test_a_queued_decision_always_carries_a_position_and_an_estimate(
    traffic: TrafficClass,
) -> None:
    """M22.1.4. A queue with no visible position is a queue nobody can reason about, and
    "how far back is it" is the first question asked during an incident."""
    state = CapacityState(
        used={(Resource.MODEL_CALLS, ""): 60}, queued={(Resource.MODEL_CALLS, ""): 7}
    )
    decision = decide(_request(Resource.MODEL_CALLS, traffic), BUDGETS, state, now=NOW)
    if decision.verdict is not Verdict.QUEUED:
        return
    assert decision.queue is not None
    assert decision.queue.position >= 1
    assert decision.queue.expected_wait_seconds > 0


@pytest.mark.parametrize("limit", [1, 4, 40])
@pytest.mark.parametrize("workload", list(WorkloadClass))
def test_admission_never_admits_beyond_a_class_share(limit: int, workload: WorkloadClass) -> None:
    """The budget is the whole mechanism. A single branch that admits anyway under some
    condition restores the failure §25 exists to prevent: subsystems consuming until
    Postgres or memory gives out."""
    budget = Budget(resource=Resource.MODEL_CALLS, limit=limit, mean_service_seconds=4.0)
    ceiling = budget.ceiling_for(workload)
    for used in range(0, limit + 3):
        state = CapacityState(used={(Resource.MODEL_CALLS, ""): used})
        decision = decide(
            _request(Resource.MODEL_CALLS, _traffic_for(workload)), (budget,), state, now=NOW
        )
        if decision.admitted:
            assert used + 1 <= ceiling


def test_an_unbudgeted_resource_is_never_admitted() -> None:
    """A resource with no row admitted by default is every subsystem consuming freely, which
    is the exact arithmetic §25 opens with: a hundred and sixty concurrent operations against
    a machine sized for ten."""
    for resource in Resource:
        key = "xero" if resource is Resource.SOURCE_CALLS else ""
        decision = decide(
            _request(resource, TrafficClass.SYSTEM, key=key), (), CapacityState(), now=NOW
        )
        assert decision.verdict is Verdict.SHED


# --------------------------------------------------- 3. per principal and per connector
def test_a_connector_stays_usable_by_several_people_and_not_merely_by_one_more() -> None:
    """The stronger half, and the reason it is a separate test.

    `principal_share_of` clamps to `connector_limit - 1`, so "one caller cannot exhaust the
    connector" stays literally true however large the fair share is set: at 100% the hog
    takes everything but a single slot. The test above then passes, because it asks whether
    one other caller gets through, and exactly one does.

    One slot a minute for the rest of the company is exhaustion by any reading that matters.
    So this asks the question the property is actually about: after a hog has used its whole
    share, can several distinct people still be served? At the real share of a quarter, yes.
    At 100% with the clamp, no - and that is the mutation the pair above did not catch, found
    by setting the constant to 1.0 and watching only the constant's own test fail.

    Three rather than a proportion, because it must hold for the smallest verified ceiling
    too, and a proportion would silently become "one" there and stop testing anything.
    """
    for source in SOURCE_CEILINGS:
        hog = source_limits(source.name, principal_id="p_hog")
        share = next(limit for limit in hog if limit.scope is LimitScope.PRINCIPAL_CONNECTOR)
        connector = next(limit for limit in hog if limit.scope is LimitScope.CONNECTOR)
        if connector.limit < 4:
            # A ceiling this small serialises the company whatever the share is. That is a
            # fact about the connector, not something a fair-share rule can fix, and
            # asserting otherwise here would only pin the arithmetic of a degenerate case.
            continue

        state = LimiterState()
        for offset in range(share.limit):
            state = state.record(NOW + timedelta(seconds=offset * 0.5), hog)

        at = NOW + timedelta(seconds=share.limit * 0.5)
        served = 0
        for i in range(3):
            other = source_limits(source.name, principal_id=f"p_other_{i}")
            if check(now=at, limits=other, state=state).allowed:
                served += 1
                state = state.record(at, other)
        assert served == 3, (
            f"{source.name}: one caller's share left room for only {served} other people"
        )


def test_one_principal_cannot_exhaust_a_connector_for_everybody() -> None:
    """The first half of the pair, over every verified source.

    One caller uses their whole share of a connector's minute and is refused. Everybody else
    must still get through, which is only true because the per-principal share is strictly
    below the connector's own ceiling. Raise the share to the whole ceiling and one backfill
    takes the connector for the rest of the minute while nothing looks misconfigured.
    """
    for source in SOURCE_CEILINGS:
        hog = source_limits(source.name, principal_id="p_hog")
        share = next(limit for limit in hog if limit.scope is LimitScope.PRINCIPAL_CONNECTOR)
        state = LimiterState()
        for offset in range(share.limit):
            state = state.record(NOW + timedelta(seconds=offset * 0.5), hog)

        at = NOW + timedelta(seconds=share.limit * 0.5)
        assert not check(now=at, limits=hog, state=state).allowed, source.name

        other = source_limits(source.name, principal_id="p_other")
        assert check(now=at, limits=other, state=state).allowed, (
            f"{source.name} was exhausted for everybody by one principal"
        )


def test_the_connector_ceiling_binds_even_when_every_caller_is_within_their_share() -> None:
    """The second half. Enough individually reasonable callers add up to a ceiling nobody
    individually crossed, and only the connector-wide window sees it. Remove that window and
    the first sign is a 429 from the vendor."""
    for source in SOURCE_CEILINGS:
        connector = next(
            limit
            for limit in source_limits(source.name, principal_id="p0")
            if limit.scope is LimitScope.CONNECTOR and limit.period == "minute"
        )
        state = LimiterState()
        calls = 0
        for index in itertools.count():
            caller = source_limits(source.name, principal_id=f"p{index}")
            share = next(limit for limit in caller if limit.scope is LimitScope.PRINCIPAL_CONNECTOR)
            for _ in range(share.limit):
                state = state.record(NOW + timedelta(seconds=calls * 0.01), caller)
                calls += 1
            if calls > connector.limit:
                break

        at = NOW + timedelta(seconds=calls * 0.01)
        fresh = source_limits(source.name, principal_id="p_new")
        decision = check(now=at, limits=fresh, state=state)
        assert not decision.allowed, source.name
        assert decision.binding is not None
        assert decision.binding.scope is LimitScope.CONNECTOR


@pytest.mark.parametrize("connector_limit", [2, 3, 5, 60, 100, 5_000])
def test_a_principals_share_is_always_strictly_below_the_connectors_ceiling(
    connector_limit: int,
) -> None:
    """The arithmetic behind the behavioural test above, so a fair-share constant edited to
    1.0 fails here as well as there."""
    assert 1 <= principal_share_of(connector_limit) < connector_limit


def test_a_refused_request_never_extends_its_window() -> None:
    """Counting refusals means a client that retries too eagerly pushes its own retry time
    further away every time it tries, so a thirty-second limit becomes a permanent lockout
    and the hint it was given becomes a lie. Nothing about that is visible to the client."""
    limits = source_limits("xero", principal_id="p_alice")
    state = LimiterState()
    for offset in range(60):
        state = state.record(NOW + timedelta(seconds=offset * 0.1), limits)

    at = NOW + timedelta(seconds=10)
    first = check(now=at, limits=limits, state=state)
    assert not first.allowed
    for _ in range(50):
        again = check(now=at, limits=limits, state=state)
        assert again.retry_after_seconds == first.retry_after_seconds


# ---------------------------------------------------------- 4. detection cannot refuse
def test_abuse_detection_has_nowhere_to_express_a_refusal() -> None:
    """M23.2.1, M23.2.2, enforced structurally rather than by review. If there is no value
    meaning "block", a future caller cannot start blocking without adding one, and adding one
    is visible in a diff. A boolean nobody currently reads would not be."""
    forbidden = {
        "block",
        "blocked",
        "deny",
        "denied",
        "refuse",
        "refused",
        "reject",
        "rejected",
        "allowed",
        "permitted",
        "banned",
        "suspend",
        "suspended",
    }
    for kind in (VolumeAssessment, DenialAssessment):
        names = {f.name.lower() for f in dataclasses.fields(kind)}
        names |= {n.lower() for n in dir(kind) if not n.startswith("_")}
        assert not (names & forbidden), f"{kind.__name__} has somewhere to refuse"


def test_the_abuse_detectors_return_an_assessment_and_never_a_verdict() -> None:
    """A function that returned a bool would be a refusal with no name. The return type is
    the surface a caller sees, so pinning it is what stops one appearing."""
    assert inspect.signature(assess_volume).return_annotation == "VolumeAssessment"
    assert inspect.signature(assess_denials).return_annotation == "DenialAssessment"


def test_the_limiter_cannot_consult_abuse_detection() -> None:
    """The declared limit is a published number with a retry hint. If a score could reach
    `check`, a caller would be refused on a heuristic and told it was a rate limit, which is
    the worst of both: unfalsifiable and dressed as policy."""
    annotations = {str(p.annotation) for p in inspect.signature(check).parameters.values()}
    assert not any("Assessment" in a or "Band" in a or "Shape" in a for a in annotations)


@pytest.mark.parametrize("observed", [0, 1, 50, 5_000, 10_000_000])
def test_no_volume_at_all_makes_the_next_question_fail(observed: int) -> None:
    """Whatever the volume, the assessment is a band and a note. There is no input that
    turns it into something the request path can act on."""
    assessment = assess_volume(observed=observed, baseline=1.0)
    assert isinstance(assessment, VolumeAssessment)
    assert assessment.note


# ------------------------------------------------- 5. capacity is not permission
def test_a_capacity_refusal_never_carries_the_denied_outcome() -> None:
    """DENIED means "this exists and you may not see it", and the audit log is the only place
    that distinction survives, because the person is always told ABSENT. A busy afternoon
    landing in that count would make the permission model look like it was failing."""
    state = CapacityState(used={(Resource.MODEL_CALLS, ""): 1_000})
    decision = decide(
        _request(Resource.MODEL_CALLS, TrafficClass.HUMAN_INTERACTIVE), BUDGETS, state, now=NOW
    )
    error = decision.as_error()
    assert error.outcome is not Outcome.DENIED
    assert not isinstance(error, Denied)
    assert isinstance(error, CapacityRefused)


def test_a_quota_refusal_is_a_different_type_from_a_capacity_refusal() -> None:
    """One says raise this caller's allowance, the other says add capacity. A shared type
    would let a caller catch one and silently handle both."""
    assert not issubclass(QuotaExceeded, CapacityRefused)
    assert not issubclass(CapacityRefused, QuotaExceeded)
    assert not issubclass(CapacityRefused, Denied)
    assert not issubclass(QuotaExceeded, Denied)


def test_the_three_refusal_kinds_have_three_different_actions() -> None:
    """The whole of "distinguishable to an operator". Two kinds sharing an action means one
    of them sends somebody to the wrong dashboard, and they find out how long it takes to
    check a machine that is fine."""
    assert set(OPERATOR_ACTION) == set(RefusalKind)
    assert len(set(OPERATOR_ACTION.values())) == len(RefusalKind)


def test_every_capacity_refusal_names_the_resource_that_ran_out() -> None:
    """An alert saying only that something was refused sends an operator looking. The
    resource and the connector are what turn it into an action."""
    for resource in Resource:
        key = "xero" if resource is Resource.SOURCE_CALLS else ""
        state = CapacityState(used={(resource, key): 10_000_000})
        decision = decide(
            _request(resource, TrafficClass.HUMAN_INTERACTIVE, key=key), BUDGETS, state, now=NOW
        )
        record = decision.log_record()
        assert record["refusal_kind"] == RefusalKind.CAPACITY
        assert str(resource) in record["subject"]


# --------------------------------------------------- 6. the verified ceilings are facts
def test_the_verified_source_limits_are_not_rounded() -> None:
    """Constraints, not guidance. Xero's 5,000 a day is the ceiling a backfill reaches first.
    Lark Base's 100 a minute is stated by the vendor as unraisable, so sizing against a
    higher number is sizing against a number that does not exist."""
    by_name = {c.name: c for c in SOURCE_CEILINGS}
    assert by_name["xero"].per_day == 5_000
    assert by_name["xero"].per_minute == 60
    assert by_name["lark_base"].per_minute == 100
    assert by_name["lark_base"].raisable is False
    assert FRESHDESK_SEARCH_MAX_RECORDS == 300


@pytest.mark.parametrize("returned", [300, 301, 500, 1_000])
def test_a_freshdesk_search_at_or_above_the_cap_is_never_reported_complete(
    returned: int,
) -> None:
    """A capped result set looks exactly like a full page of results, and no test anybody
    writes has 301 matching tickets. Beyond the cap the answer is silently wrong with nothing
    reporting it, which is the one failure mode here that reaches a person as a fact."""
    result = search_completeness("freshdesk", returned)
    assert result.complete is False


def test_the_first_bottleneck_at_ten_and_a_hundred_times_is_xeros_daily_ceiling() -> None:
    """M22.3.4, computed rather than asserted in prose.

    The estate runs at about 0.1 requests a second, which is 8,640 a day; roughly six per
    cent of questions touching any one source puts about 500 calls a day on each. At that
    demand Xero's 5,000 is reached at 10x while the other two, whose daily figures are
    derived from per-minute ceilings, still have room. If this ever reports a different
    source, the documented answer in `FIRST_BOTTLENECK_AT_SCALE` is out of date.
    """
    demands = tuple(Demand(ceiling=c, calls_per_day=500) for c in ceilings())
    ladder = dict(bottleneck_ladder(demands))
    for multiplier in (10.0, 100.0):
        bottleneck = ladder[multiplier]
        assert bottleneck is not None
        assert bottleneck.ceiling.name == "xero"
    assert "Xero" in admission_module.FIRST_BOTTLENECK_AT_SCALE


def test_every_rejected_alternative_is_still_written_down() -> None:
    """These constants are the arguments for the mechanisms above, and a mechanism whose
    argument has been deleted is one somebody removes next quarter because nothing explains
    why it is there. Importing them is most of the guard; the length check is what stops one
    being emptied to a stub to make a lint pass.

    The ordering one earns its place differently. Nothing composes the two modules, so
    `LIMITS_ARE_CHECKED_BEFORE_CAPACITY` is a contract for the caller rather than something
    the code enforces, and saying so in the constant is the only place a reader will see it.
    """
    reasons = (
        ADMISSION_DECIDES_BEFORE_WORK_STARTS,
        A_PERSON_WAITING_IS_NEVER_QUEUED,
        CAPACITY_IS_NOT_PERMISSION,
        INGESTION_THROTTLE_IS_THE_CLASS_CEILING,
        BOTH_LIMITS_APPLY,
        LIMITS_ARE_CHECKED_BEFORE_CAPACITY,
        REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW,
        ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE,
        AUTOMATED_TRAFFIC_IS_DECLARED_NOT_SNIFFED,
        THE_HINT_IS_MEASURED_NOT_GUESSED,
    )
    for reason in reasons:
        assert len(reason) > 120, reason
    assert "nothing here that can enforce it" in LIMITS_ARE_CHECKED_BEFORE_CAPACITY


# ----------------------------------------------------------------- structural guards
def test_nothing_in_the_capacity_layer_reads_a_clock() -> None:
    """`now` is a parameter everywhere. A controller that read the clock could not be tested
    for the case that actually goes wrong, which is a decision taken against counters that
    were true a moment ago."""
    for module in (admission_module, limits_module):
        tree = ast.parse(inspect.getsource(module))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called.isdisjoint({"now", "utcnow", "today", "time", "monotonic", "sleep"})


def test_nothing_here_sleeps_spawns_a_thread_or_generates_its_own_jitter() -> None:
    """A limiter that owned a timer could not be advanced by a test, so the window boundary
    would never be exercised. And jitter generated here rather than passed in would be
    identical across workers, which looks like jitter and decorrelates nothing."""
    for module in (admission_module, limits_module):
        tree = ast.parse(inspect.getsource(module))
        names = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert names.isdisjoint({"threading", "asyncio", "time", "sched", "concurrent", "random"})


def test_nothing_in_the_capacity_layer_touches_the_database_or_the_network() -> None:
    """These are policy modules. An import of the driver, the session or a client here would
    make the admission decision depend on the thing it is protecting."""
    for module in (admission_module, limits_module):
        tree = ast.parse(inspect.getsource(module))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(
            m.startswith(("sqlalchemy", "psycopg", "redis", "httpx", "brain.db", "brain.tables"))
            for m in modules
        )


@pytest.mark.parametrize("slots", list(range(3, 65)))
def test_the_pools_never_overcommit_and_never_starve_a_class(slots: int) -> None:
    """M22.2.2. Overcommitting the pooler shows up as a connection error on the request path
    rather than as a queue anywhere, and a class with no connections cannot run at all while
    the console still shows three pools."""
    pools = pools_for(slots)
    assert pools.total == slots
    assert min(pools.interactive, pools.background, pools.batch) >= 1
    assert pools.interactive >= pools.background >= pools.batch


def test_a_window_is_bounded_by_its_own_limit() -> None:
    """M23.1.1. Only admitted requests are recorded, and an admitted request was under the
    limit, so the log cannot grow without bound. A window that kept every attempt would be a
    memory leak per principal in Valkey with no ceiling anywhere."""
    limits = source_limits("lark_base", principal_id="p_alice")
    share = next(limit for limit in limits if limit.scope is LimitScope.PRINCIPAL_CONNECTOR)
    state = LimiterState()
    at = NOW
    recorded = 0
    for _ in range(500):
        at = at + timedelta(seconds=0.4)
        if check(now=at, limits=limits, state=state).allowed:
            state = state.record(at, limits)
            recorded += 1
    assert recorded > 0
    assert len(state.window_for(share.key).hits) <= share.limit
    assert state.window_for(share.key).count(at, MINUTE_SECONDS) <= share.limit
