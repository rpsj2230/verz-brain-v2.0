"""Rules the provider health layer must never break.

Four properties, and each one is here rather than in the unit file because breaking it
produces no error and no wrong answer. It produces a system that looks like it is
monitoring providers and is not.

1. **The prober never votes on its own verdict.** A probe outcome that reached the live
   ring would move the fail ratio, and the fail ratio decides whether to open. The result
   is a breaker that opens on synthetic traffic while real requests were succeeding.
2. **A probe that vanishes does not fence a deployment off forever.** The half-open claim
   has a TTL and it has to actually reclaim, or one lost probe removes a provider from
   rotation permanently with nothing raised anywhere.
3. **The ratio rule keeps its minimum sample.** One failure out of one request is a 100%
   failure ratio and means nothing, so without the guard the first failure against a cold
   deployment shortens the fallback chain on no evidence.
4. **Alerting fires on depth, not on the chain running out.** A chain that reached rung
   three and then answered is the event that arrives in time to act on. Alerting only on
   total failure means the first notification is a report of an outage rather than a
   warning about one.

Plus the structural guards: nothing here opens a socket, imports a provider SDK, or reads
a clock, because every one of those turns a pure state machine into something that cannot
be tested for the case that goes wrong.

Task ids: M5.1.1, M5.1.4, M5.4.3, M5.4.4, M5.4.7, M5.4.8
"""

from __future__ import annotations

import ast
import inspect
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.core.lane import Lane
from brain.models import driver as driver_module
from brain.models import health as health_module
from brain.models.driver import (
    DriverFailure,
    PoolRouter,
    ProviderClient,
    routers_for,
)
from brain.models.health import (
    DEPTH_NOT_FINAL_FAILURE,
    PROBE_FAILURES_TO_OPEN_IDLE,
    PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING,
    PROBE_WINDOW,
    AlertLevel,
    ChainAttempt,
    ChainOutcome,
    OpenReason,
    ProviderHealth,
    assess_chain_depth,
    next_probes,
    opens_now,
    probe_verdict,
)
from brain.models.routing import (
    BREAKER_CONSECUTIVE_FAILURES,
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

pytestmark = pytest.mark.invariant

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


def rung(dep: Deployment, *, position: int = 0, tier: Tier = Tier.MAIN) -> RoutingRung:
    return RoutingRung(
        tier=tier,
        position=position,
        model=dep.model,
        deployment=dep,
        attempts=1,
        timeout_seconds=12.0,
        max_concurrency=40,
    )


def opened(name: str = "a", *, at: datetime = T0) -> ProviderHealth:
    health = ProviderHealth.for_deployment(name)
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        health = health.record_live_failure(at)
    return health


def half_open(name: str = "a", *, at: datetime = T0) -> tuple[ProviderHealth, datetime]:
    health = opened(name, at=at)
    when = at + timedelta(seconds=health.breaker.cooldown_seconds + 1)
    return health.advance(when), when


# ---------------------------------------------- 1. the prober does not drive its own verdict
def test_a_probe_outcome_never_moves_the_live_ratio() -> None:
    """The single most important rule in this module. If a probe reached the live ring it
    would move the fail ratio, the fail ratio decides whether to open, and the prober would
    therefore be deciding its own verdict: a run of probes against a merely slow provider
    would open a breaker that live traffic was passing through perfectly well."""
    health = ProviderHealth.for_deployment("a")
    for index in range(BREAKER_RATIO_MIN_SAMPLES):
        health = health.record_live_success(T0 + timedelta(seconds=index))

    live_before = health.live
    ratio_before = health.breaker.fail_ratio()

    # Inside the staleness window on purpose. This test is about probes never moving the
    # live ratio; running past the window would also make the live evidence stale, and the
    # state check below would then measure the staleness rule rather than ring separation.
    for index in range(PROBE_WINDOW):
        health = health.record_probe(ok=False, now=T0 + timedelta(seconds=10 * (index + 1)))

    assert health.live == live_before, "a probe outcome reached the live ring"
    assert health.breaker.fail_ratio() == ratio_before
    assert health.state is BreakerState.CLOSED
    assert health.probe == (False,) * PROBE_WINDOW


def test_a_half_open_probe_does_not_touch_the_live_ring() -> None:
    """The half-open branch is the only path that hands a probe outcome to the breaker, and
    it is safe only because `record_success` and `record_failure` return without touching
    `live` in that state. That is a property of `routing.py` this module depends on, so it
    is pinned here rather than assumed."""
    health, when = half_open("a")
    live_before = health.live

    reopened = health.record_probe(ok=False, now=when)
    assert reopened.state is BreakerState.OPEN
    assert reopened.live == live_before, "a failed half-open probe lengthened the live ring"

    recovered = health.record_probe(ok=True, now=when)
    assert recovered.state is BreakerState.CLOSED
    assert recovered.live == (), "a clean probe left an outcome behind instead of resetting"


def test_a_breaker_opened_by_probes_alone_has_recorded_no_live_failures() -> None:
    """The idle case end to end. If the two probe failures that opened this breaker had
    been recorded as live outcomes, the live ring would say two real requests failed, and
    the console would send somebody to look for traffic that never existed."""
    health = ProviderHealth.for_deployment("idle")
    health = health.record_probe(ok=False, now=T0)
    health = health.record_probe(ok=False, now=T0 + timedelta(seconds=60))

    assert health.state is BreakerState.OPEN
    assert health.live == (), "probe failures were recorded as live failures"
    # The evidence was spent on the transition. Carrying it across the state change would
    # mean one failure after the next cooldown looked like the second of two.
    assert health.probe == ()
    assert health.would_open() is None


def test_probe_evidence_cannot_open_a_breaker_that_current_live_traffic_contradicts() -> None:
    """Where *current* live evidence exists it is strictly better informed. Without this
    bound, a prober on a broken network path takes the whole estate out of rotation while
    every real request is succeeding.

    "Current" is the qualifier, and it was added deliberately. The rule used to be that any
    live evidence at all silenced the probe rule, which made that rule unreachable for the
    deployment it exists for: the live ring is bounded by count and never by age, so once a
    deployment has served anything it never empties again.
    """
    health = ProviderHealth.for_deployment("busy")
    for index in range(BREAKER_RATIO_MIN_SAMPLES):
        health = health.record_live_success(T0 + timedelta(seconds=index))
    # Probes inside the staleness window, so the live successes still speak for now.
    for index in range(PROBE_WINDOW):
        health = health.record_probe(ok=False, now=T0 + timedelta(seconds=10 * (index + 1)))
    assert health.state is BreakerState.CLOSED


def test_probes_do_open_a_breaker_whose_live_evidence_has_gone_stale() -> None:
    """The case the old rule could not reach, and the one the prober exists for. A rung
    that served twenty requests this morning and died at six carries a full ring of
    successes; every probe fails and nothing opened, so it stayed in the chain until a real
    request hit it. That is precisely the outcome probing is meant to pre-empt."""
    health = ProviderHealth.for_deployment("idle")
    for index in range(BREAKER_RATIO_MIN_SAMPLES):
        health = health.record_live_success(T0 + timedelta(seconds=index))

    # Well past LIVE_EVIDENCE_STALE_SECONDS: this morning's successes are not evidence
    # about this minute.
    later = T0 + timedelta(hours=3)
    for index in range(PROBE_FAILURES_TO_OPEN_IDLE):
        health = health.record_probe(ok=False, now=later + timedelta(seconds=60 * index))
    assert health.state is BreakerState.OPEN


def test_stale_live_evidence_does_not_let_a_single_probe_failure_open_anything() -> None:
    """Staleness widens which evidence counts, never how much of it is needed. One failed
    probe is still one observation."""
    health = ProviderHealth.for_deployment("idle")
    health = health.record_live_success(T0)
    health = health.record_probe(ok=False, now=T0 + timedelta(hours=3))
    assert health.state is BreakerState.CLOSED


def test_the_separation_of_the_two_rings_is_written_down_with_its_reason() -> None:
    """A rule with no argument attached is a rule the next person deletes, and merging the
    two rings is a genuinely tempting simplification until the consequence is stated."""
    assert "voting on its own verdict" in PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING


# --------------------------------------------------- 2. a lost probe does not wedge anything
def test_a_probe_that_never_reports_back_does_not_wedge_the_breaker_half_open() -> None:
    """Without the TTL reclaim, one probe that dies in flight holds the single half-open
    admission forever. The deployment then admits nothing, is out of rotation permanently,
    and no error is raised anywhere: it simply never comes back."""
    health, when = half_open("a")
    claimed, admitted = health.claim_probe(when)
    assert admitted is True
    assert claimed.claim_probe(when)[1] is False

    expired = when + timedelta(seconds=BREAKER_PROBE_CLAIM_TTL_SECONDS + 1)
    reclaimed, readmitted = claimed.claim_probe(expired)
    assert readmitted is True
    assert reclaimed.state is BreakerState.HALF_OPEN
    assert probe_verdict(claimed, expired).due is True


def test_deciding_what_to_probe_does_not_consume_the_half_open_admission() -> None:
    """`next_probes` inspects; `claim_probe` claims. If planning claimed, one scheduler tick
    would burn the single admission of every half-open deployment it looked at, and the one
    it actually probed would be the only one that could recover."""
    health, when = half_open("a")
    due = next_probes([health], when)
    assert [v.deployment_id for v in due] == ["a"]

    _, admitted = health.claim_probe(when)
    assert admitted is True, "planning the probe had already spent the admission"


# ------------------------------------------------------- 3. the ratio rule keeps its floor
def test_one_failure_out_of_one_request_is_not_a_failing_provider() -> None:
    """A 100% failure ratio over a single sample is arithmetic, not evidence. Without the
    minimum-sample guard the very first failure against a cold deployment opens its breaker,
    and the fallback chain shortens itself on no information at all."""
    assert opens_now(live=(False,), consecutive_failures=1, probe=()) is None
    assert opens_now(live=(False, False), consecutive_failures=2, probe=()) is None
    below_floor = (False,) * (BREAKER_RATIO_MIN_SAMPLES - 1)
    assert opens_now(live=below_floor, consecutive_failures=0, probe=()) is None


def test_the_ratio_rule_fires_once_the_sample_is_large_enough() -> None:
    """The guard has to be a floor and not a mute button. If the minimum were never
    reached in practice, the ratio rule would be dead code and a provider failing 60% of
    requests without three in a row would stay in rotation indefinitely."""
    live = (False,) * 5 + (True,) * 3
    assert len(live) >= BREAKER_RATIO_MIN_SAMPLES
    assert opens_now(live=live, consecutive_failures=0, probe=()) is OpenReason.LIVE_FAIL_RATIO


# ------------------------------------------------------------- 4. depth, not final failure
def test_a_chain_that_succeeded_at_depth_still_raises_an_alert() -> None:
    """The whole of M5.4.8. This request produced an answer, so no error was logged, no
    failure counter moved and nobody complained, and the primary provider is dead. Alerting
    on final failure instead would leave this silent until every rung was down."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(
            ChainAttempt(
                deployment_id="primary",
                position=0,
                succeeded=False,
                trigger=FallbackTrigger.PROVIDER_ERROR,
            ),
            ChainAttempt(deployment_id="fallback", position=1, succeeded=True),
        ),
    )
    assert outcome.exhausted is False, "this chain answered; it is not a failure"
    alert = assess_chain_depth(outcome)
    assert alert is not None, "a chain that fell back and answered raised nothing"
    assert alert.level is AlertLevel.WARNING
    assert alert.served_by == "fallback"


def test_a_deep_chain_that_answered_is_critical_even_though_nothing_failed_overall() -> None:
    """Depth three means two rungs failed inside one request. On a two-rung chain that is
    everything, and it still produced an answer, so the level has to come from the depth
    rather than from the outcome."""
    outcome = ChainOutcome(
        tier=Tier.HEAVY,
        attempts=(
            ChainAttempt(
                deployment_id="a", position=0, succeeded=False, trigger=FallbackTrigger.TIMEOUT
            ),
            ChainAttempt(
                deployment_id="b", position=1, succeeded=False, trigger=FallbackTrigger.TIMEOUT
            ),
            ChainAttempt(deployment_id="c", position=2, succeeded=True),
        ),
    )
    assert outcome.exhausted is False
    alert = assess_chain_depth(outcome)
    assert alert is not None
    assert alert.level is AlertLevel.CRITICAL


def test_a_rung_the_breaker_removed_counts_towards_depth() -> None:
    """A chain whose primary is circuit-open makes exactly one attempt and answers.
    Counting attempts rather than positions would report depth one and stay silent, and
    that is precisely the state where the outage is a day old and nothing has said so."""
    outcome = ChainOutcome(
        tier=Tier.MAIN,
        attempts=(ChainAttempt(deployment_id="fallback", position=1, succeeded=True),),
        skipped=(SkippedRung(rung=rung(deployment("primary")), reason=SkipReason.CIRCUIT_OPEN),),
    )
    assert len(outcome.attempts) == 1
    assert outcome.depth == 2
    alert = assess_chain_depth(outcome)
    assert alert is not None


def test_alerting_on_depth_rather_than_failure_is_written_down_with_its_reason() -> None:
    """Alerting on final failure is the obvious thing to build and it is what most systems
    ship, so the argument against it has to survive next to the code."""
    assert "arrives when every rung is down" in DEPTH_NOT_FINAL_FAILURE


# -------------------------------------------------------------------- structural guards
def test_the_health_layer_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is policy, not transport. A helpful lookup added later (asking a provider
    whether it is up, resolving a hostname to decide something) would make every routing
    decision depend on the network being healthy, which is the thing being measured."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        msg = "the health layer opened a socket"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    health, when = half_open("a")
    health = health.record_probe(ok=False, now=when)
    health = health.advance(when + timedelta(seconds=600))
    next_probes([health, ProviderHealth.for_deployment("b")], when)
    assess_chain_depth(
        ChainOutcome(
            tier=Tier.MAIN,
            attempts=(ChainAttempt(deployment_id="a", position=1, succeeded=True),),
        )
    )
    routers_for(seed_chain())
    ProviderClient(provider="anthropic").policy_for(rung(deployment("a")), Lane.ANSWER)


def test_no_provider_sdk_reaches_the_policy_layer() -> None:
    """M5.1.1 is about the seam. The moment a provider SDK's types appear in the policy
    layer, swapping the SDK becomes a change to the policy layer, and the policy layer is
    the part holding the residency guarantee."""
    forbidden = {
        "litellm",
        "openai",
        "anthropic",
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "urllib3",
        "socket",
        "sqlalchemy",
    }
    for module in (health_module, driver_module):
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module.split(".")[0])
        leaked = imported & forbidden
        assert not leaked, f"{module.__name__} imports {leaked}"


def test_nothing_in_the_health_layer_reads_the_clock() -> None:
    """`now` is a parameter everywhere. A health record that read the clock itself could
    not be tested for the two cases that actually go wrong: the half-open transition, and a
    probe that never reports back."""
    tree = ast.parse(inspect.getsource(health_module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint({"now", "utcnow", "today", "time", "monotonic", "sleep"})


def test_nothing_in_the_health_layer_sleeps_or_spawns_a_thread() -> None:
    """The prober is a function that says what to probe next, and something else owns the
    schedule. A prober that owned a timer could not be advanced by a test, so the case it
    exists for (an idle provider that died hours ago) would never be exercised."""
    for module in (health_module, driver_module):
        source = inspect.getsource(module)
        tree = ast.parse(source)
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
        assert names.isdisjoint({"threading", "asyncio", "time", "sched", "concurrent"})


def test_a_driver_failure_has_nowhere_to_record_an_opinion_of_the_reply() -> None:
    """`trigger_for` closes the fallback set by its signature. This type holds the same
    closure one layer lower, where an adapter author is actually writing code, so "the
    answer looked weak so I set a trigger" cannot be expressed at all."""
    fields = set(DriverFailure.__dataclass_fields__)
    assert fields == {
        "deployment_id",
        "status",
        "timed_out",
        "connection_failed",
        "context_exceeded",
        "detail",
    }
    forbidden = {"quality", "score", "confidence", "text", "reply", "content", "length"}
    assert fields.isdisjoint(forbidden)


def test_a_pool_router_cannot_be_built_across_two_pools() -> None:
    """M5.1.4. Tag filtering is a proxy-mode feature, so an SDK Router selects from
    whatever deployment list it was constructed with. One Router holding every tier would
    be free to serve a HEAVY request from the SMALL pool, and it would do it silently."""
    main = rung(deployment("m"), tier=Tier.MAIN, position=0)
    heavy = rung(deployment("h"), tier=Tier.HEAVY, position=1)
    with pytest.raises(ValueError, match="a router that spans pools"):
        PoolRouter(tier=Tier.MAIN, rungs=(main, heavy))


def test_no_router_offers_a_tag_filter_to_be_tempted_by() -> None:
    """The property is structural, not a convention: cross-pool selection has to be
    unrepresentable rather than discouraged. A `filter(tag=...)` here would be the exact
    thing that does not work in SDK mode, wearing a name that suggests it does."""
    router = routers_for(seed_chain())[Tier.MAIN]
    surface = {name for name in dir(router) if not name.startswith("_")}
    assert surface == {"tier", "rungs", "deployment_ids", "serves", "rung_for"}


def test_every_pool_router_holds_exactly_its_own_tier() -> None:
    """Built from the real seed chain rather than a fixture, so a rung added to the console
    matrix in the wrong tier shows up here rather than in a request that gets the wrong
    model."""
    chain = seed_chain()
    for tier, router in routers_for(chain).items():
        assert router.tier is tier
        assert router.deployment_ids == tuple(r.deployment.id for r in chain.rungs_for(tier))
        for other in Tier:
            if other is tier:
                continue
            for foreign in chain.rungs_for(other):
                assert not router.serves(foreign.deployment.id)


def test_the_health_record_and_its_breaker_can_never_name_different_deployments() -> None:
    """A health record showing one deployment's cooldown under another's name sends an
    operator to restart a provider that was fine. Deriving the id makes it unrepresentable
    rather than validated."""
    health = ProviderHealth(breaker=CircuitBreaker(deployment_id="only-one"))
    assert health.deployment_id == "only-one"
    assert "deployment_id" not in ProviderHealth.__dataclass_fields__
