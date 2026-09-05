"""Provider health, the background prober, depth alerting, and the driver seam.

Two modules are covered here rather than one. `brain.models.driver` has no test file of
its own because the seam and the health layer are one piece of work: the driver is what a
probe would actually call, and the failure it returns is what moves a breaker. Splitting
them would put the fake driver in one file and the breaker it feeds in another.

The properties that must never break (probe outcomes staying out of the live ring, the
minimum-sample guard, alerting on depth rather than on final failure) live next door in
`tests/invariants/test_health_invariants.py`. What is here is ordinary behaviour: the
arithmetic, the branches, and the refusals.

Task ids: M5.1.1, M5.1.3, M5.1.4, M5.4.3, M5.4.4, M5.4.7, M5.4.8
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.errors import Outcome
from brain.core.lane import Lane
from brain.models.driver import (
    CONCRETE_ADAPTER_NOT_BUILT,
    LITELLM_IS_A_DRIVER_NOT_A_PROXY,
    TAG_FILTERING_IS_INOPERATIVE_IN_SDK_MODE,
    CallPolicy,
    DriverFailure,
    DriverMessage,
    DriverRegistry,
    DriverRequest,
    DriverResponse,
    LaneOverride,
    ModelDriver,
    ProviderClient,
    ProviderUnavailable,
    Role,
    TokenUsage,
    answer_lane_worst_case_seconds,
    check_answer_lane_budget,
    routers_for,
)
from brain.models.health import (
    CHAIN_DEPTH_CRITICAL,
    CHAIN_DEPTH_WARNING,
    LIVE_EVIDENCE_STALE_SECONDS,
    PROBE_FAILURES_TO_OPEN_IDLE,
    PROBE_INTERVAL_SECONDS,
    PROBE_WINDOW,
    AlertLevel,
    ChainAttempt,
    ChainOutcome,
    OpenReason,
    ProbeReason,
    ProviderHealth,
    assess_chain_depth,
    next_probes,
    opens_now,
    probe_verdict,
)
from brain.models.routing import (
    BREAKER_CONSECUTIVE_FAILURES,
    BREAKER_LIVE_WINDOW,
    BREAKER_PROBE_CLAIM_TTL_SECONDS,
    BREAKER_RATIO_MIN_SAMPLES,
    BreakerState,
    CircuitBreaker,
    Deployment,
    FallbackTrigger,
    ResidencyClass,
    RoutingRung,
    SkippedRung,
    SkipReason,
    Tier,
    seed_chain,
)

T0 = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def deployment(name: str, *, provider: str = "anthropic") -> Deployment:
    return Deployment(
        id=name,
        provider=provider,
        model=f"{provider}-model",
        region="global",
        residency_class=ResidencyClass.GLOBAL,
        context_window=200_000,
    )


def rung(
    dep: Deployment,
    *,
    position: int = 0,
    tier: Tier = Tier.MAIN,
    attempts: int = 1,
    timeout_seconds: float = 12.0,
    max_concurrency: int = 40,
) -> RoutingRung:
    return RoutingRung(
        tier=tier,
        position=position,
        model=dep.model,
        deployment=dep,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )


def opened(name: str = "a", *, at: datetime = T0) -> ProviderHealth:
    """A health record whose breaker has just opened on live failures."""
    health = ProviderHealth.for_deployment(name)
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        health = health.record_live_failure(at)
    assert health.state is BreakerState.OPEN
    return health


def half_open(name: str = "a", *, at: datetime = T0) -> tuple[ProviderHealth, datetime]:
    """A health record sitting half-open, and the moment it got there."""
    health = opened(name, at=at)
    when = at + timedelta(seconds=health.breaker.cooldown_seconds + 1)
    health = health.advance(when)
    assert health.state is BreakerState.HALF_OPEN
    return health, when


# ------------------------------------------------------------------ the opening rules
def test_three_consecutive_live_failures_open_the_breaker() -> None:
    """The first of the three M5.4.4 rules. Without it a provider returning 500s to every
    request stays in the chain until the ratio window fills, which at this traffic volume
    is a different hour."""
    assert opens_now(live=(False, False, False), consecutive_failures=3, probe=()) is (
        OpenReason.CONSECUTIVE_LIVE_FAILURES
    )
    assert opens_now(live=(False, False), consecutive_failures=2, probe=()) is None


def test_more_than_half_a_full_window_failing_opens_the_breaker() -> None:
    """The second rule, and the reason it is a ratio rather than failures per minute: at
    0.1 requests per second a per-minute counter never fills, so a dead provider stays in
    rotation for an hour while the threshold is never reached."""
    live = (False,) * 5 + (True,) * 3  # 5/8 = 0.625, over the half
    assert opens_now(live=live, consecutive_failures=0, probe=()) is OpenReason.LIVE_FAIL_RATIO
    even = (False,) * 4 + (True,) * 4  # exactly half is not "above half"
    assert opens_now(live=even, consecutive_failures=0, probe=()) is None


def test_two_probe_failures_open_a_breaker_that_has_seen_no_live_traffic() -> None:
    """The third rule, and the only one this module owns outright. Without it an idle
    deployment can never open at all: both live rules need requests, and the whole point of
    the prober is the deployment nobody is sending requests to."""
    assert (
        opens_now(live=(), consecutive_failures=0, probe=(False, False))
        is OpenReason.PROBE_FAILURES_WHILE_IDLE
    )
    assert opens_now(live=(), consecutive_failures=0, probe=(False,)) is None


def test_probe_failures_do_not_open_a_breaker_that_live_traffic_is_passing_through() -> None:
    """Where live evidence exists it is strictly better informed. A probe failing while
    real requests succeed is a fact about the prober's path, and opening on it would take a
    working deployment out of rotation."""
    live = (True,) * 8
    assert opens_now(live=live, consecutive_failures=0, probe=(False,) * PROBE_WINDOW) is None


def test_a_recovered_probe_run_does_not_count_towards_the_next_open() -> None:
    """The probe rule is a count, so something has to reset it. Without the reset a
    deployment that failed twice, recovered, then failed once would open on that single
    failure, because the ring still held the old pair."""
    assert opens_now(live=(), consecutive_failures=0, probe=(False, False, True, False)) is None


# ------------------------------------------------------------------- the rings themselves
def test_the_probe_ring_forgets_the_oldest_outcome_once_it_is_full() -> None:
    """An unbounded ring is a memory leak with a health dashboard attached, and it also
    makes the count rule permanent: a failure from an hour ago would still be evidence."""
    health = ProviderHealth.for_deployment("a")
    for index in range(PROBE_WINDOW + 5):
        health = health.record_probe(
            ok=True, now=T0 + timedelta(seconds=PROBE_INTERVAL_SECONDS * index)
        )
    assert len(health.probe) == PROBE_WINDOW


def test_the_live_ring_stays_bounded_at_the_window_the_breaker_declares() -> None:
    """The ratio is over a window. A live ring that grew without bound would make the
    ratio an average over all time, so a provider that failed badly last week could never
    look healthy again."""
    health = ProviderHealth.for_deployment("a")
    for index in range(BREAKER_LIVE_WINDOW + 5):
        health = health.record_live_success(T0 + timedelta(seconds=index))
    assert len(health.live) == BREAKER_LIVE_WINDOW


def test_health_reads_its_deployment_id_off_its_breaker() -> None:
    """Two fields holding one value is two fields that can disagree, and the one a reader
    trusts would be whichever the console rendered. Storing it once makes the mismatch
    unrepresentable rather than merely checked."""
    health = ProviderHealth.for_deployment("anthropic-sonnet-global")
    assert health.deployment_id == "anthropic-sonnet-global"
    assert health.breaker.deployment_id == health.deployment_id


def test_a_fresh_deployment_is_healthy_rather_than_unknown() -> None:
    """`RoutingChain.select` treats a deployment with no breaker on file as admitting.
    Starting blocked-until-probed would mean a fresh install serves nothing until the
    prober has been round every rung."""
    health = ProviderHealth.for_deployment("new")
    assert health.state is BreakerState.CLOSED
    assert health.live == ()
    assert health.probe == ()
    assert health.would_open() is None


# ------------------------------------------------------------------- probe bookkeeping
def test_two_probe_failures_take_an_idle_deployment_out_of_rotation() -> None:
    """The end-to-end of the third opening rule. What breaks without it is the case the
    prober exists for: a provider that died while nobody was asking stays listed healthy
    until a person needs the fallback and finds it dead too."""
    health = ProviderHealth.for_deployment("idle")
    health = health.record_probe(ok=False, now=T0)
    assert health.state is BreakerState.CLOSED

    health = health.record_probe(ok=False, now=T0 + timedelta(seconds=PROBE_INTERVAL_SECONDS))
    assert health.state is BreakerState.OPEN
    assert health.breaker.open_streak == 1
    assert health.breaker.cooldown_until is not None


def test_a_probe_result_arriving_while_the_breaker_is_still_cooling_changes_nothing() -> None:
    """A result can outlive its cooldown. Letting a straggler close or reopen the gate
    would override an evaluation somebody has since redone, which is how one stale retry
    quietly cancels a breaker for everybody."""
    health = opened("a")
    still_cooling = T0 + timedelta(seconds=1)
    after = health.record_probe(ok=True, now=still_cooling)
    assert after.state is BreakerState.OPEN
    assert after.breaker.opened_at == health.breaker.opened_at
    assert after.probe == (True,)


def test_a_clean_half_open_probe_closes_the_breaker_and_resets_the_backoff() -> None:
    """The recovery path. If the streak survived a recovery, the next unrelated incident
    would start its cooldown wherever the last one finished, so a provider that had a bad
    morning would be fenced off for ten minutes over one afternoon blip."""
    health, when = half_open("a")
    recovered = health.record_probe(ok=True, now=when)
    assert recovered.state is BreakerState.CLOSED
    assert recovered.breaker.open_streak == 0
    assert recovered.probe == ()


def test_a_failed_half_open_probe_reopens_with_a_longer_cooldown() -> None:
    """Exponential backoff is what stops a genuinely dead provider being probed every
    thirty seconds for an hour. Without the streak increment the cooldown never grows."""
    health, when = half_open("a")
    first_cooldown = health.breaker.cooldown_seconds
    reopened = health.record_probe(ok=False, now=when)
    assert reopened.state is BreakerState.OPEN
    assert reopened.breaker.open_streak == 2
    assert reopened.breaker.cooldown_seconds > first_cooldown


def test_claiming_a_probe_stamps_that_we_asked_rather_than_that_we_heard_back() -> None:
    """Measuring the interval from the answer would let a provider that never replies
    suppress its own probing, which is the moment it most needs probing."""
    health, when = half_open("a")
    claimed, admitted = health.claim_probe(when)
    assert admitted is True
    assert claimed.last_probe_at == when


def test_a_second_worker_on_the_same_tick_does_not_get_the_half_open_admission() -> None:
    """Half-open admits exactly one request. Two workers probing at once would put two
    failures from a single incident into the ring, and the breaker would treat one event as
    two."""
    health, when = half_open("a")
    first, admitted_first = health.claim_probe(when)
    _, admitted_second = first.claim_probe(when)
    assert (admitted_first, admitted_second) == (True, False)


# ---------------------------------------------------------------------- the prober
def test_a_half_open_deployment_is_due_for_a_probe() -> None:
    """Half-open is the one state where a probe restores capacity rather than only
    updating a record. If the prober skipped it, the single admission would be spent by
    whichever real request arrived first, and a person would wait for that answer."""
    health, when = half_open("a")
    verdict = probe_verdict(health, when)
    assert verdict.due is True
    assert verdict.reason is ProbeReason.HALF_OPEN_ADMISSION


def test_a_cooling_deployment_is_not_probed_and_the_verdict_says_until_when() -> None:
    """Probing during the cooldown spends requests on a provider we have already decided
    to leave alone. The console needs the reason, or 'nothing has probed this for ten
    minutes' has no answer."""
    verdict = probe_verdict(opened("a"), T0 + timedelta(seconds=1))
    assert verdict.due is False
    assert "cooling until" in verdict.detail


def test_a_deployment_with_no_live_outcome_on_record_is_due() -> None:
    """This is the whole reason the prober exists. Both live rules need traffic, so a
    deployment nobody is sending requests to is healthy purely by assumption."""
    verdict = probe_verdict(ProviderHealth.for_deployment("idle"), T0)
    assert verdict.due is True
    assert verdict.reason is ProbeReason.IDLE_NO_LIVE_EVIDENCE


def test_a_full_but_stale_live_ring_is_history_rather_than_evidence() -> None:
    """A deployment that served twenty requests this morning and none since has a full
    live ring and would never be probed. Below roughly one request per five minutes the
    ring describes the past, not now."""
    health = ProviderHealth.for_deployment("quiet").record_live_success(T0)
    much_later = T0 + timedelta(seconds=LIVE_EVIDENCE_STALE_SECONDS + 1)
    verdict = probe_verdict(health, much_later)
    assert verdict.due is True
    assert verdict.reason is ProbeReason.STALE_LIVE_EVIDENCE


def test_a_deployment_carrying_live_traffic_is_not_probed() -> None:
    """Probing a busy healthy provider is pure cost: live traffic already answers the
    question, and better."""
    health = ProviderHealth.for_deployment("busy").record_live_success(T0)
    verdict = probe_verdict(health, T0 + timedelta(seconds=30))
    assert verdict.due is False
    assert "already answering" in verdict.detail


def test_the_interval_stops_a_burst_of_ticks_becoming_a_burst_of_probes() -> None:
    """A scheduler that fires twice, or two workers on one tick, must not produce two
    probes: two failures from one incident would open the breaker on a single event."""
    health = ProviderHealth.for_deployment("idle")
    claimed, _ = health.claim_probe(T0)
    soon = T0 + timedelta(seconds=PROBE_INTERVAL_SECONDS - 1)
    assert probe_verdict(claimed, soon).due is False
    due_again = T0 + timedelta(seconds=PROBE_INTERVAL_SECONDS + 1)
    assert probe_verdict(claimed, due_again).due is True


def test_the_prober_puts_recovery_ahead_of_curiosity() -> None:
    """Probing a half-open deployment restores capacity; probing an idle closed one only
    updates a record. Without the ordering a `limit` would spend the tick on bookkeeping
    while a recovered provider stayed fenced off."""
    recovering, when = half_open("recovering")
    never_probed = ProviderHealth.for_deployment("never-probed")
    long_ago = replace(
        ProviderHealth.for_deployment("probed-long-ago"),
        last_probe_at=when - timedelta(seconds=600),
    )
    recently = replace(
        ProviderHealth.for_deployment("probed-recently"), last_probe_at=when - timedelta(seconds=5)
    )
    busy = ProviderHealth.for_deployment("busy").record_live_success(when - timedelta(seconds=5))

    due = next_probes([recently, busy, long_ago, never_probed, recovering], when)
    assert [v.deployment_id for v in due] == ["recovering", "never-probed", "probed-long-ago"]


def test_the_prober_bounds_one_tick_so_an_outage_is_not_a_retry_storm() -> None:
    """When everything is down everything is due at once. Fanning out to the whole estate
    in one tick aims a burst at a provider that is already struggling."""
    healths = [ProviderHealth.for_deployment(f"d{index}") for index in range(6)]
    assert len(next_probes(healths, T0, limit=2)) == 2
    assert len(next_probes(healths, T0)) == 6


def test_the_prober_reads_a_generator_once_and_still_orders_it() -> None:
    """`healths` is an Iterable, and the ordering reads it twice. A generator would be
    silently empty on the second pass and the result would come back unsorted, which is the
    kind of bug that only appears once a caller stops passing a list."""
    recovering, when = half_open("recovering")
    idle = ProviderHealth.for_deployment("idle")
    due = next_probes((h for h in (idle, recovering)), when)
    assert [v.deployment_id for v in due] == ["recovering", "idle"]


# ------------------------------------------------------------------- depth alerting
def attempt(position: int, *, succeeded: bool, name: str | None = None) -> ChainAttempt:
    return ChainAttempt(
        deployment_id=name or f"rung-{position}",
        position=position,
        succeeded=succeeded,
        trigger=None if succeeded else FallbackTrigger.PROVIDER_ERROR,
    )


def open_skip(position: int) -> SkippedRung:
    return SkippedRung(
        rung=rung(deployment(f"rung-{position}"), position=position),
        reason=SkipReason.CIRCUIT_OPEN,
    )


def test_a_chain_answered_on_its_primary_raises_nothing() -> None:
    """Alerting on a healthy request would bury the real signal in noise, and an alert
    stream nobody reads is the same as no alerting at all."""
    outcome = ChainOutcome(tier=Tier.MAIN, attempts=(attempt(0, succeeded=True),))
    assert outcome.depth == 1
    assert assess_chain_depth(outcome) is None


def test_a_chain_that_fell_back_once_and_answered_is_a_warning() -> None:
    """The headline M5.4.8 case. The person got their answer so nothing else will ever
    report this, and the primary is dead."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(attempt(0, succeeded=False), attempt(1, succeeded=True)),
    )
    assert outcome.depth == CHAIN_DEPTH_WARNING
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.WARNING
    assert alert.served_by == "rung-1"


def test_a_chain_that_reached_the_third_rung_and_answered_is_critical() -> None:
    """Two rungs failed inside one request. On the two-rung seed chain that is everything,
    and it still produced an answer, so no failure counter anywhere moved."""
    outcome = ChainOutcome(
        tier=Tier.HEAVY,
        attempts=(
            attempt(0, succeeded=False),
            attempt(1, succeeded=False),
            attempt(2, succeeded=True),
        ),
    )
    assert outcome.depth == CHAIN_DEPTH_CRITICAL
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.CRITICAL


def test_depth_counts_a_rung_the_breaker_removed_before_the_request_started() -> None:
    """A chain whose primary is circuit-open makes one attempt and answers, so counting
    attempts would report depth one and never alert. That is the exact case where the
    outage is already a day old and nothing has said so."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(attempt(1, succeeded=True),),
        skipped=(open_skip(0),),
    )
    assert outcome.depth == 2
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.WARNING


def test_answering_on_the_primary_with_a_dead_rung_underneath_still_warns() -> None:
    """Nothing about the request was slow or wrong, and the chain has no fallback left.
    Without this the next primary failure is the first anyone hears, and by then there is
    nowhere to go."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(attempt(0, succeeded=True),),
        skipped=(open_skip(1),),
    )
    assert outcome.depth == 1
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.WARNING
    assert "no fallback left" in alert.reason


def test_an_exhausted_chain_is_critical() -> None:
    """Total failure still alerts. It is simply not the thing that decides there is an
    alert, and the level says which of the two happened."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(attempt(0, succeeded=False), attempt(1, succeeded=False)),
    )
    assert outcome.exhausted is True
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.CRITICAL
    assert alert.served_by is None


def test_a_residency_refusal_is_not_a_provider_health_alert() -> None:
    """A residency skip is a policy fact that holds for every request from that scope.
    Alerting on it would fire on every regulated question and send an operator to a
    provider dashboard for something no provider can fix."""
    skipped = SkippedRung(rung=rung(deployment("eu-only"), position=0), reason=SkipReason.RESIDENCY)
    outcome = ChainOutcome(tier=Tier.MAIN, skipped=(skipped,))
    assert assess_chain_depth(outcome) is None


def test_an_attempt_cannot_both_succeed_and_carry_a_fallback_trigger() -> None:
    """A trigger is why the chain moved on. One recorded against a success makes the
    executed chain unreconstructable from the attempt rows, which is the entire reason the
    rows are written."""
    with pytest.raises(ValueError, match="succeeded but carries trigger"):
        ChainAttempt(
            deployment_id="a",
            position=0,
            succeeded=True,
            trigger=FallbackTrigger.TIMEOUT,
        )


# ------------------------------------------------------------------ the driver seam
class FakeDriver:
    """A driver that records what it was asked and returns what it was told to.

    Deliberately not a subclass of anything. `ModelDriver` is a Protocol precisely so an
    adapter does not have to import our base class, and a test that inherited from
    something would stop proving that.
    """

    provider = "anthropic"

    def __init__(self, *, failure: DriverFailure | None = None) -> None:
        self.failure = failure
        self.seen: list[DriverRequest] = []

    def complete(self, request: DriverRequest) -> DriverResponse:
        self.seen.append(request)
        if self.failure is not None:
            raise ProviderUnavailable(self.failure)
        return DriverResponse(
            deployment_id=request.deployment_id,
            model=request.model,
            text="ok",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
        )


def request_for(dep: Deployment) -> DriverRequest:
    return DriverRequest(
        deployment_id=dep.id,
        model=dep.model,
        messages=(DriverMessage(role=Role.USER, content="hello"),),
        timeout_seconds=12.0,
    )


def test_a_plain_object_with_the_right_shape_satisfies_the_driver_protocol() -> None:
    """The seam only pays off if an adapter can be written without importing us. A
    protocol that quietly required a base class would make swapping SDKs a change to
    everything that touches one."""
    driver = FakeDriver()
    assert isinstance(driver, ModelDriver)


def test_a_provider_outage_is_degraded_rather_than_failed() -> None:
    """The taxonomy's promise for Degraded is that we say a source was unreachable and
    never substitute something. Raising Failed would put a provider outage in the same
    bucket as a bug in our own code, and those go to different people."""
    driver = FakeDriver(failure=DriverFailure(deployment_id="a", status=503))
    with pytest.raises(ProviderUnavailable) as caught:
        driver.complete(request_for(deployment("a")))
    assert caught.value.outcome is Outcome.DEGRADED
    assert caught.value.failure.status == 503


def test_a_driver_failure_classifies_itself_through_the_shared_trigger_rule() -> None:
    """One implementation of the fallback rule. A driver that classified its own failures
    would be a second copy of the rule, and the two would disagree on the first 4xx."""
    assert DriverFailure(deployment_id="a", status=429).trigger is FallbackTrigger.RATE_LIMITED
    assert DriverFailure(deployment_id="a", status=503).trigger is FallbackTrigger.PROVIDER_ERROR
    assert DriverFailure(deployment_id="a", timed_out=True).trigger is FallbackTrigger.TIMEOUT
    # A 400 is our request being wrong, and the next rung receives the same request.
    assert DriverFailure(deployment_id="a", status=400).trigger is None


def test_a_request_with_no_messages_or_no_timeout_is_refused_at_construction() -> None:
    """An unbounded call is how a slow provider becomes unbounded memory rather than
    latency, and an empty message list is a bug that reads as an empty answer."""
    dep = deployment("a")
    with pytest.raises(ValueError, match="non-positive timeout"):
        replace(request_for(dep), timeout_seconds=0.0)
    with pytest.raises(ValueError, match="carries no messages"):
        replace(request_for(dep), messages=())


def test_a_lane_may_override_the_timeout_without_touching_the_attempt_count() -> None:
    """M5.1.3. Overrides are per lane because a person waiting and an overnight task want
    opposite things from the same endpoint. Coupling the two would mean lengthening a task
    timeout also lengthened the answer lane's."""
    client = ProviderClient(
        provider="anthropic", lanes={Lane.TASK: LaneOverride(timeout_seconds=90.0)}
    )
    target = rung(deployment("a"), attempts=2, timeout_seconds=12.0)
    assert client.policy_for(target, Lane.TASK) == CallPolicy(
        timeout_seconds=90.0, attempts=2, max_concurrency=40
    )
    assert client.policy_for(target, Lane.ANSWER) == CallPolicy(
        timeout_seconds=12.0, attempts=2, max_concurrency=40
    )


def test_asking_for_a_fast_lane_call_policy_is_refused() -> None:
    """The fast lane's guarantee is that no model saw the question, and everything
    downstream of it was built on that. Returning a plausible policy would let the bug
    run."""
    client = ProviderClient(provider="anthropic")
    with pytest.raises(ValueError, match="fast lane takes no model"):
        client.policy_for(rung(deployment("a")), Lane.FAST)


def test_a_client_refuses_a_rung_served_by_another_provider() -> None:
    """A client holds one provider's timeouts and retries. Applying them to somebody
    else's endpoint would silently give an untested provider a tested provider's budget."""
    client = ProviderClient(provider="anthropic")
    with pytest.raises(ValueError, match="which is served by"):
        client.policy_for(rung(deployment("a", provider="openai")), Lane.ANSWER)


def test_a_zero_attempt_override_is_refused_the_way_a_zero_attempt_rung_is() -> None:
    """The way to remove a rung is to remove it. A zero-attempt override is a rung that
    silently never runs while reading in the console as configured."""
    client = ProviderClient(provider="anthropic", lanes={Lane.ANSWER: LaneOverride(attempts=0)})
    with pytest.raises(ValueError, match="sets 0 attempts"):
        client.policy_for(rung(deployment("a")), Lane.ANSWER)


def test_the_seed_chain_still_fits_the_answer_lane_budget_with_no_overrides() -> None:
    """The compounding (rungs times attempts times timeout) is invisible in any single row
    of the console editor, so nobody notices a three-minute chain until somebody complains
    the bot is slow."""
    chain = seed_chain()
    check_answer_lane_budget(chain, Tier.MAIN, {})
    assert answer_lane_worst_case_seconds(chain, Tier.MAIN, {}) == 24.0


def test_a_lane_override_that_blows_the_answer_lane_budget_is_refused() -> None:
    """`RoutingChain.worst_case_seconds` stops being the truth the moment an override can
    lengthen a timeout. Without this check an operator helping one slow provider takes the
    whole chain past the point where the person has opened another tab."""
    clients = {
        "anthropic": ProviderClient(
            provider="anthropic", lanes={Lane.ANSWER: LaneOverride(timeout_seconds=20.0)}
        )
    }
    with pytest.raises(ValueError, match="over the"):
        check_answer_lane_budget(seed_chain(), Tier.MAIN, clients)


def test_one_router_is_built_per_pool_and_none_for_an_empty_pool() -> None:
    """M5.1.4. An empty Router is an object that accepts calls and fails all of them,
    which surfaces as a provider outage; a missing key is the honest surface for a tier
    nobody has configured."""
    routers = routers_for(seed_chain())
    assert set(routers) == {Tier.MAIN, Tier.HEAVY}
    assert routers[Tier.MAIN].deployment_ids == (
        "anthropic-sonnet-global",
        "anthropic-sonnet-us-east-1",
    )


def test_a_router_refuses_a_deployment_from_another_pool() -> None:
    """Returning None here would be checked at the first call site and coalesced at the
    second, and the second is where a HEAVY request gets served by whatever the SMALL pool
    had lying around."""
    router = routers_for(seed_chain())[Tier.MAIN]
    assert router.serves("anthropic-sonnet-global") is True
    with pytest.raises(ValueError, match="does not serve"):
        router.rung_for("anthropic-opus-global")


def test_a_registry_with_no_adapter_for_a_provider_reports_it_as_unreachable() -> None:
    """We hold no client for this provider is the same class of event as the provider
    being down: the request cannot be served here, and the chain should move on rather than
    crash with a KeyError halfway through a person's question."""
    registry = DriverRegistry(routers=routers_for(seed_chain()))
    with pytest.raises(ProviderUnavailable, match="no driver registered"):
        registry.driver_for(rung(deployment("a")))


def test_a_registry_finds_an_adapter_by_provider_rather_than_by_deployment() -> None:
    """One adapter per SDK, serving every region of that provider. Keying by deployment
    would mean adding a region needed a new registration, and it would fail at the exact
    moment the chain reached it."""
    driver = FakeDriver()
    registry = DriverRegistry(drivers={"anthropic": driver}, routers=routers_for(seed_chain()))
    chain_rung = routers_for(seed_chain())[Tier.MAIN].rung_for("anthropic-sonnet-us-east-1")
    assert registry.driver_for(chain_rung) is driver


def test_the_seam_says_out_loud_that_no_concrete_adapter_exists() -> None:
    """Nothing here calls a provider. Without this written down, the next reader sees a
    protocol, a registry and a router and reasonably concludes the layer is finished."""
    assert "No concrete provider adapter exists yet" in CONCRETE_ADAPTER_NOT_BUILT
    assert "proxy server" in LITELLM_IS_A_DRIVER_NOT_A_PROXY
    assert "proxy-mode feature" in TAG_FILTERING_IS_INOPERATIVE_IN_SDK_MODE


# ------------------------------------------------------ the written rule against the machine
def test_the_written_rule_and_the_breaker_agree_about_live_failures() -> None:
    """`opens_now` restates two rules that `CircuitBreaker.record_failure` also implements,
    so that the policy is readable in one place. Without this the two copies drift and a
    threshold gets changed in one of them.

    The agreement is asserted on failure events only, which is where the breaker evaluates
    its rules. A success never opens a breaker, so the two can briefly disagree after one:
    that gap is deliberate and is documented in the module docstring.
    """
    for pattern in itertools.product([True, False], repeat=9):
        breaker = CircuitBreaker(deployment_id="a")
        live: tuple[bool, ...] = ()
        consecutive = 0
        for ok in pattern:
            if breaker.state is not BreakerState.CLOSED:
                break
            live = (*live, ok)[-BREAKER_LIVE_WINDOW:]
            if ok:
                consecutive = 0
                breaker = breaker.record_success(T0)
                continue
            consecutive += 1
            breaker = breaker.record_failure(T0)
            written = opens_now(live=live, consecutive_failures=consecutive, probe=())
            assert (breaker.state is BreakerState.OPEN) == (written is not None), (
                f"pattern {pattern}: breaker says {breaker.state}, written rule says {written}"
            )


def test_the_ratio_rule_needs_a_full_minimum_sample_and_not_one_less() -> None:
    """The boundary is the part that gets moved by whoever is next annoyed by it. One
    below the minimum with every sample failing must still be closed, or the guard is
    decorative."""
    below = (False,) * (BREAKER_RATIO_MIN_SAMPLES - 1)
    assert opens_now(live=below, consecutive_failures=0, probe=()) is None
    at = (False,) * BREAKER_RATIO_MIN_SAMPLES
    assert opens_now(live=at, consecutive_failures=0, probe=()) is OpenReason.LIVE_FAIL_RATIO


def test_the_probe_rule_needs_its_full_count_and_not_one_less() -> None:
    """Opening an idle deployment on one synthetic failure would fence it off every time a
    DNS lookup hiccuped, and the deployment would then be probed on a backoff instead of on
    the interval."""
    one_short = (False,) * (PROBE_FAILURES_TO_OPEN_IDLE - 1)
    assert opens_now(live=(), consecutive_failures=0, probe=one_short) is None


def test_a_half_open_claim_that_never_reports_back_is_reclaimed_not_wedged() -> None:
    """Without the TTL the deployment sits half-open with the admission held forever,
    admitting nothing, and it is out of rotation permanently with no error raised
    anywhere."""
    health, when = half_open("a")
    claimed, _ = health.claim_probe(when)
    assert claimed.claim_probe(when)[1] is False

    expired = when + timedelta(seconds=BREAKER_PROBE_CLAIM_TTL_SECONDS + 1)
    reclaimed, admitted = claimed.claim_probe(expired)
    assert admitted is True
    assert reclaimed.state is BreakerState.HALF_OPEN
