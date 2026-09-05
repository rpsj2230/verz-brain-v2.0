"""Tier classification, the fallback matrix, the breaker, and residency.

Three of these are ordinary unit tests. The fourth, residency, is closer to an invariant:
the property under test is that no code path exists from a residency-constrained scope to
a provider outside it, and the way that property breaks in real systems is not a wrong
answer but a quiet one, so it is asserted from several directions here.

Task ids: M5.2.1, M5.2.3, M5.3.1, M5.3.2, M5.4.1, M5.4.2, M5.4.4, M5.4.5, M5.4.6,
M5.5.1, M5.5.2, M5.5.3, M5.5.4
"""

from __future__ import annotations

import ast
import inspect
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.core.errors import Degraded, Outcome
from brain.models.routing import (
    ANSWER_LANE_WALL_CLOCK_BUDGET_SECONDS,
    BREAKER_BASE_COOLDOWN_SECONDS,
    BREAKER_CONSECUTIVE_FAILURES,
    BREAKER_MAX_COOLDOWN_SECONDS,
    BREAKER_PROBE_CLAIM_TTL_SECONDS,
    ESCALATION_HEADROOM,
    QUALITY_FALLBACK_REJECTED,
    REGION_STORAGE,
    TIER_CONTEXT_WINDOW,
    TIER_LADDER,
    BreakerState,
    ChainSelection,
    CircuitBreaker,
    Deployment,
    FallbackTrigger,
    Lane,
    NoCompliantRoute,
    ResidencyClass,
    ResidencyRequirement,
    RoutingChain,
    RoutingRequest,
    RoutingRung,
    RungRole,
    SkipReason,
    Tier,
    classify_tier,
    may_fall_back,
    permits_tier_escalation,
    plan,
    seed_chain,
    storage_location,
    trigger_for,
)

T0 = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def deployment(
    name: str,
    *,
    provider: str = "anthropic",
    model: str = "m",
    region: str = "global",
    residency: ResidencyClass = ResidencyClass.GLOBAL,
    window: int = 200_000,
    enabled: bool = True,
) -> Deployment:
    return Deployment(
        id=name,
        provider=provider,
        model=model,
        region=region,
        residency_class=residency,
        context_window=window,
        enabled=enabled,
    )


def rung(
    dep: Deployment,
    *,
    tier: Tier = Tier.MAIN,
    position: int = 0,
    attempts: int = 1,
    timeout: float = 10.0,
    concurrency: int = 10,
) -> RoutingRung:
    return RoutingRung(
        tier=tier,
        position=position,
        model=dep.model,
        deployment=dep,
        attempts=attempts,
        timeout_seconds=timeout,
        max_concurrency=concurrency,
    )


EU_ONLY = ResidencyRequirement(allowed_regions=frozenset({"eu-west-1"}))


# --------------------------------------------------------------- tier classification
def test_the_same_request_always_lands_in_the_same_tier() -> None:
    """Determinism is the property the whole routing design rests on. Without it a trace
    cannot be replayed, two identical questions can be billed at different tiers, and the
    answer to "why did this go to the expensive model" becomes unanswerable."""
    grid = [
        RoutingRequest(lane=lane, tool_count=tools, estimated_context_tokens=tokens)
        for lane in Lane
        for tools in (0, 1, 9)
        for tokens in (0, 50_000, 190_000, 5_000_000)
    ]
    for request in grid:
        results = {classify_tier(request).tier for _ in range(50)}
        assert len(results) == 1, f"{request} classified inconsistently: {results}"


def test_classification_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5.2.3. A model-in-the-loop router would add a round trip to every request and let
    text inside a retrieved document influence which jurisdiction handles the question
    that retrieved it. Blocking the socket is the only assertion that stays true when
    somebody later adds a helpful lookup."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        msg = "tier classification opened a socket"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    for lane in (Lane.ANSWER, Lane.TASK, Lane.FAST):
        classify_tier(RoutingRequest(lane=lane, tool_count=3, estimated_context_tokens=90_000))


def test_the_fast_lane_takes_no_model_at_all() -> None:
    """The fast lane's guarantee is that no model saw the question; everything downstream
    of it was built on that. A tier value rather than None so a caller cannot forget it."""
    decision = classify_tier(RoutingRequest(lane=Lane.FAST))
    assert decision.tier is Tier.NONE


def test_a_tier_pin_on_the_fast_lane_is_ignored_and_said_so() -> None:
    """Honouring the pin would put a model on a path with an empty tool catalogue and
    reads restricted to projected tables, none of which was designed for one."""
    decision = classify_tier(RoutingRequest(lane=Lane.FAST, requested_tier=Tier.HEAVY))
    assert decision.tier is Tier.NONE
    assert "ignored" in decision.reason


def test_pinning_the_no_model_tier_outside_the_fast_lane_is_refused() -> None:
    """Answering with no model is an admission decision made on exact intent match, not
    something a caller can request. Silently downgrading such a pin to MAIN would hide a
    genuinely confused caller."""
    with pytest.raises(ValueError, match="fast lane"):
        classify_tier(RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.NONE))


def test_the_task_lane_gets_the_heaviest_tier() -> None:
    """TASK is autonomous multi-step work with tool use, which is exactly where agentic
    quality is load-bearing, and it is about 5% of traffic so the cost is contained."""
    assert classify_tier(RoutingRequest(lane=Lane.TASK)).tier is Tier.HEAVY


def test_an_unpinned_answer_request_gets_the_default_not_a_guess() -> None:
    """The default is a default. Anything cleverer here would be a classifier chasing the
    smaller of two cost levers."""
    assert classify_tier(RoutingRequest(lane=Lane.ANSWER)).tier is Tier.MAIN


def test_an_author_pin_beats_the_default_rules() -> None:
    """The Skill author wrote the procedure and knows what it needs. Rules exist for the
    untagged traffic, not to overrule someone who declared an intent."""
    decision = classify_tier(RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL))
    assert decision.tier is Tier.SMALL


def test_a_tool_loop_never_runs_below_the_main_tier() -> None:
    """A tool loop re-sends the whole transcript every turn, so a small model that misuses
    a tool costs more in wasted turns than the per-token saving it was chosen for."""
    decision = classify_tier(
        RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL, tool_count=1)
    )
    assert decision.tier is Tier.MAIN


def test_a_residency_constrained_scope_is_never_classified_small() -> None:
    """The compliant menu is smaller and older than the global one, so a compliant SMALL
    rung is the weakest model in the estate handling the most regulated data."""
    decision = classify_tier(
        RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL, residency=EU_ONLY)
    )
    assert decision.tier is Tier.MAIN
    assert "residency" in decision.reason


def test_residency_overrides_a_pin_because_the_scope_owns_the_obligation() -> None:
    """The constraint attaches to the scope, not to the request. A caller who pins a tier
    is not thereby entitled to relax somebody else's data policy."""
    pinned = classify_tier(RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL))
    constrained = classify_tier(
        RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL, residency=EU_ONLY)
    )
    assert pinned.tier is Tier.SMALL
    assert constrained.tier is not Tier.SMALL


def test_context_overflow_escalates_upward() -> None:
    """The only legal cross-tier move. A request that does not fit has one honest
    destination and it is not a smaller model."""
    tokens = int(TIER_CONTEXT_WINDOW[Tier.SMALL] * ESCALATION_HEADROOM) + 1
    decision = classify_tier(
        RoutingRequest(lane=Lane.ANSWER, requested_tier=Tier.SMALL, estimated_context_tokens=tokens)
    )
    assert decision.tier is Tier.MAIN


def test_no_input_combination_ever_moves_a_request_down_a_tier() -> None:
    """Silently answering a hard question with a smaller model produces a confident wrong
    answer that nobody knows is degraded, which in a system whose output drives autonomous
    actions is strictly worse than an honest failure. Asserted over the whole input grid
    rather than one case, because a downward move would arrive as a special case."""
    for pin in TIER_LADDER:
        floor = TIER_LADDER.index(pin)
        for tools in (0, 4):
            for tokens in (0, 100_000, 400_000):
                for residency in (ResidencyRequirement(), EU_ONLY):
                    decision = classify_tier(
                        RoutingRequest(
                            lane=Lane.ANSWER,
                            requested_tier=pin,
                            tool_count=tools,
                            estimated_context_tokens=tokens,
                            residency=residency,
                        )
                    )
                    assert TIER_LADDER.index(decision.tier) >= floor


def test_overflow_at_the_top_tier_is_reported_rather_than_silently_truncated() -> None:
    """There is nowhere above HEAVY, and a quietly shortened prompt is a wrong answer with
    no error anywhere. The caller is told to trim retrieval, which is where the fix is."""
    decision = classify_tier(RoutingRequest(lane=Lane.TASK, estimated_context_tokens=5_000_000))
    assert decision.tier is Tier.HEAVY
    assert decision.context_overflows is True


def test_a_request_that_fits_does_not_report_overflow() -> None:
    decision = classify_tier(RoutingRequest(lane=Lane.ANSWER, estimated_context_tokens=10_000))
    assert decision.context_overflows is False


def test_every_decision_carries_the_argument_for_itself() -> None:
    """A choice with no argument attached gets changed by whoever is next annoyed by it,
    and nobody can tell afterwards whether the change was safe."""
    for lane in (Lane.ANSWER, Lane.TASK, Lane.FAST):
        assert classify_tier(RoutingRequest(lane=lane)).reason


def test_the_decision_carries_the_residency_constraint_forward() -> None:
    """Chain selection needs it. Recomputing it from the scope at the next layer is how
    the two copies drift and one of them stops being applied."""
    decision = classify_tier(RoutingRequest(lane=Lane.ANSWER, residency=EU_ONLY))
    assert decision.residency == EU_ONLY


# ------------------------------------------------------------------------- the matrix
def test_rungs_come_back_in_position_order_whatever_order_they_were_written_in() -> None:
    """The console edits rows and Postgres returns them unordered. A chain that depends on
    insertion order stops being reconstructable from the attempt rows."""
    a = rung(deployment("a"), position=0)
    b = rung(deployment("b"), position=1)
    c = rung(deployment("c"), position=2)
    chain = RoutingChain(rungs=(c, a, b))
    assert [r.position for r in chain.rungs_for(Tier.MAIN)] == [0, 1, 2]


def test_a_rung_naming_a_model_its_deployment_does_not_serve_is_rejected() -> None:
    """The model is denormalised onto the rung because it is half the price-book key. A
    drifted copy meters the request against the wrong price and nothing complains."""
    with pytest.raises(ValueError, match="serves"):
        RoutingRung(
            tier=Tier.MAIN,
            position=0,
            model="claude-opus-5",
            deployment=deployment("d", model="claude-sonnet-5"),
            attempts=1,
            timeout_seconds=10.0,
            max_concurrency=1,
        )


def test_a_rung_with_no_attempts_is_rejected() -> None:
    """A rung with zero attempts reads in the console as configured and never runs. The
    way to remove a rung is to remove it."""
    with pytest.raises(ValueError, match="attempts"):
        rung(deployment("d"), attempts=0)


def test_a_rung_with_a_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout"):
        rung(deployment("d"), timeout=0.0)


def test_two_rungs_cannot_share_a_position_in_one_tier() -> None:
    """Ambiguous order means the executed chain cannot be rebuilt from the attempt rows,
    which is the only reason those rows are written."""
    with pytest.raises(ValueError, match="position"):
        RoutingChain(rungs=(rung(deployment("a"), position=0), rung(deployment("b"), position=0)))


def test_the_same_position_in_two_tiers_is_fine() -> None:
    chain = RoutingChain(
        rungs=(
            rung(deployment("a"), tier=Tier.MAIN, position=0),
            rung(deployment("b"), tier=Tier.HEAVY, position=0),
        )
    )
    assert len(chain.rungs) == 2


def test_the_role_label_is_derived_from_position_and_provider_not_typed() -> None:
    """M5.3.2. A label somebody types drifts from the position it describes, and then the
    console shows a primary sitting third in the chain."""
    first = rung(deployment("a", provider="anthropic"), position=0)
    second = rung(deployment("b", provider="anthropic"), position=1)
    third = rung(deployment("c", provider="other"), position=2)
    chain = RoutingChain(rungs=(first, second, third))
    assert chain.role_of(first) is RungRole.PRIMARY
    assert chain.role_of(second) is RungRole.SAME_PROVIDER_FAILOVER
    assert chain.role_of(third) is RungRole.CROSS_PROVIDER_FAILOVER


def test_an_explicitly_named_model_resolves_to_its_tier() -> None:
    """This is how "the caller asked for a specific model" reaches the classifier without
    the classifier itself doing a registry lookup, which would make it neither pure nor
    cheap."""
    chain = seed_chain()
    assert chain.tier_of_model("claude-sonnet-5") is Tier.MAIN
    assert chain.tier_of_model("claude-opus-5") is Tier.HEAVY
    assert chain.tier_of_model("a-model-we-do-not-run") is None


def test_a_tiers_window_is_the_narrowest_rung_not_the_widest() -> None:
    """Declare the primary's window and a request sized to fit the primary overflows the
    fallback rung, so the fallback is unusable at exactly the moment it is reached."""
    chain = RoutingChain(
        rungs=(
            rung(deployment("wide", window=1_000_000), position=0),
            rung(deployment("narrow", window=128_000), position=1),
        )
    )
    assert chain.narrowest_window(Tier.MAIN) == 128_000


def test_the_seed_windows_do_not_exceed_the_narrowest_rung_they_describe() -> None:
    """The declared per-tier window is what classification escalates against, so it
    exceeding a real rung would route a request into a chain that cannot hold it."""
    chain = seed_chain()
    for tier in (Tier.MAIN, Tier.HEAVY):
        assert TIER_CONTEXT_WINDOW[tier] <= chain.narrowest_window(tier)


def test_the_worst_case_wall_clock_of_a_chain_is_computable() -> None:
    """Rungs times attempts times timeout compounds, and it is invisible in any single row
    of the console editor. Three rungs at two attempts and thirty seconds is three minutes
    nobody intended."""
    chain = RoutingChain(
        rungs=(
            rung(deployment("a"), position=0, attempts=2, timeout=30.0),
            rung(deployment("b"), position=1, attempts=1, timeout=30.0),
        )
    )
    assert chain.worst_case_seconds(Tier.MAIN) == 90.0


def test_the_seed_answer_chain_fits_inside_the_time_a_person_will_wait() -> None:
    """A person is waiting on the ANSWER lane. This is the assertion that stops a
    well-meant extra retry from quietly doubling the wait for every failed request."""
    assert seed_chain().worst_case_seconds(Tier.MAIN) <= ANSWER_LANE_WALL_CLOCK_BUDGET_SECONDS


def test_the_seed_ships_two_tiers_because_a_third_would_go_cache_cold() -> None:
    """A tier stays cache-warm only above roughly one request per five minutes in office
    hours, about 96 a day. Below that it pays the cache write premium with no reads and
    costs more than it saves, so SMALL is added on measurement, not on principle."""
    assert seed_chain().rungs_for(Tier.SMALL) == ()


def test_the_seed_lists_no_provider_we_hold_no_key_for() -> None:
    """A cross-provider rung for an uncontracted provider is a chain that fails at the
    exact moment it is reached, and it looks like resilience until then."""
    chain = seed_chain()
    assert {r.deployment.provider for r in chain.rungs} == {"anthropic"}


# ------------------------------------------------------------------- fallback triggers
def test_a_timeout_triggers_fallback() -> None:
    assert trigger_for(timed_out=True) is FallbackTrigger.TIMEOUT


def test_a_429_triggers_fallback() -> None:
    assert trigger_for(status=429) is FallbackTrigger.RATE_LIMITED


def test_a_provider_5xx_triggers_fallback() -> None:
    for status in (500, 502, 503, 529):
        assert trigger_for(status=status) is FallbackTrigger.PROVIDER_ERROR


def test_an_open_circuit_triggers_fallback() -> None:
    assert trigger_for(circuit_open=True) is FallbackTrigger.CIRCUIT_OPEN


def test_a_connection_error_triggers_fallback() -> None:
    assert trigger_for(connection_failed=True) is FallbackTrigger.CONNECTION_ERROR


def test_a_4xx_that_is_not_429_stops_the_chain() -> None:
    """A 400 is our request being wrong, and the next rung gets the same request.
    Retrying spends money to receive the identical error a second time."""
    for status in (400, 401, 403, 404, 422):
        assert trigger_for(status=status) is None


def test_a_successful_response_never_triggers_fallback() -> None:
    """M5.4.2. However unhelpful, hedged, short or plainly wrong the reply was."""
    assert trigger_for(status=200) is None


def test_a_poor_answer_never_triggers_fallback() -> None:
    """M5.4.2, stated from the caller's side. Every description of a weak answer somebody
    might reach for returns False, because none of them is a member of the closed set."""
    for reason in (
        "quality",
        "the answer looked weak",
        "low confidence",
        "unhelpful",
        "thumbs down",
        "hallucinated",
        "the user asked again",
        "score below threshold",
        "",
    ):
        assert may_fall_back(reason) is False


def test_every_member_of_the_closed_set_is_permitted_and_nothing_else_is() -> None:
    """The set is closed: membership is the whole test for whether a fallback is legal."""
    for trigger in FallbackTrigger:
        assert may_fall_back(trigger) is True
    assert may_fall_back("provider_error_ish") is False
    assert may_fall_back("TIMEOUT") is False


def test_no_trigger_is_a_judgement_about_content() -> None:
    """Guards the closure by name as well as by value, so a member added in a hurry with a
    plausible name fails here rather than in production six weeks later."""
    forbidden = ("quality", "score", "confidence", "weak", "unhelpful", "refus", "grade")
    for trigger in FallbackTrigger:
        haystack = f"{trigger.name.lower()} {trigger.value}"
        assert not any(word in haystack for word in forbidden), trigger


def test_there_is_nowhere_in_the_signature_to_put_an_opinion_of_the_reply() -> None:
    """The closure is enforced structurally. Somebody adding an `answer` or `score`
    parameter has to change this test, which means arguing for it in review rather than
    slipping it in as a one-line improvement."""
    params = set(inspect.signature(trigger_for).parameters)
    assert params == {
        "status",
        "timed_out",
        "connection_failed",
        "context_exceeded",
        "circuit_open",
    }


def test_the_rejection_of_quality_fallback_is_written_down_with_its_reason() -> None:
    """A rule with no reason attached is a rule the next person deletes."""
    assert "not measurable at request time" in QUALITY_FALLBACK_REJECTED
    assert not hasattr(FallbackTrigger, "QUALITY")


def test_context_overflow_is_the_only_trigger_that_may_change_tier() -> None:
    """Every other trigger keeps the request in its tier, and none may move it down."""
    escalating = [t for t in FallbackTrigger if permits_tier_escalation(t)]
    assert escalating == [FallbackTrigger.CONTEXT_EXCEEDED]


def test_a_circuit_open_outranks_a_status_code_in_classification() -> None:
    """The attempt was never made, so reporting the previous status would attribute a
    failure to a provider that was not asked."""
    assert trigger_for(status=500, circuit_open=True) is FallbackTrigger.CIRCUIT_OPEN


# ------------------------------------------------------------------- circuit breaker
def test_three_consecutive_failures_open_the_breaker() -> None:
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    assert breaker.state is BreakerState.OPEN


def test_two_consecutive_failures_do_not() -> None:
    """A provider that drops one request in a hundred is not broken, and a breaker that
    trips on it removes a healthy deployment from rotation for the cooldown."""
    breaker = CircuitBreaker(deployment_id="d").record_failure(T0).record_failure(T0)
    assert breaker.state is BreakerState.CLOSED


def test_a_success_resets_the_consecutive_count() -> None:
    """Consecutive means consecutive. Counting cumulative failures instead opens every
    long-lived breaker eventually, whatever the provider is actually doing."""
    breaker = CircuitBreaker(deployment_id="d")
    breaker = breaker.record_failure(T0).record_failure(T0).record_success(T0)
    breaker = breaker.record_failure(T0).record_failure(T0)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 2


def test_a_fail_ratio_over_the_window_opens_before_three_in_a_row_ever_happen() -> None:
    """The threshold is a ratio over a window rather than failures per minute because at
    roughly 0.1 requests per second a per-minute counter never fills, so a dead provider
    stays in rotation for an hour. Alternating pass and fail never reaches three in a
    row and is still a provider failing more than half the time."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(4):
        breaker = breaker.record_failure(T0).record_success(T0)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures < BREAKER_CONSECUTIVE_FAILURES

    breaker = breaker.record_failure(T0)
    assert breaker.state is BreakerState.OPEN
    assert breaker.consecutive_failures < BREAKER_CONSECUTIVE_FAILURES


def test_an_open_breaker_admits_nothing_during_its_cooldown() -> None:
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    assert breaker.admits(T0) is False
    assert breaker.admits(T0 + timedelta(seconds=BREAKER_BASE_COOLDOWN_SECONDS - 1)) is False


def test_the_cooldown_backs_off_exponentially_across_repeated_opens() -> None:
    """A provider that fails its probe is more broken than one that has just failed for
    the first time, and probing it on the same interval is how a dead provider gets
    hammered for an hour."""
    first = CircuitBreaker(deployment_id="d", open_streak=1)
    second = CircuitBreaker(deployment_id="d", open_streak=2)
    third = CircuitBreaker(deployment_id="d", open_streak=3)
    assert first.cooldown_seconds == BREAKER_BASE_COOLDOWN_SECONDS
    assert second.cooldown_seconds == BREAKER_BASE_COOLDOWN_SECONDS * 2
    assert third.cooldown_seconds == BREAKER_BASE_COOLDOWN_SECONDS * 4


def test_the_cooldown_never_exceeds_its_ceiling_even_with_full_jitter() -> None:
    """Ten minutes: long enough that a dead provider stops costing anything, short enough
    that a recovery is noticed without a restart."""
    breaker = CircuitBreaker(deployment_id="d", open_streak=12, jitter=1.0)
    assert breaker.cooldown_seconds == BREAKER_MAX_COOLDOWN_SECONDS


def test_jitter_is_supplied_by_the_caller_and_only_ever_lengthens() -> None:
    """Jitter decorrelates callers, so it has to come from the caller. Generating it here
    would either need a clock and a random source inside a pure state machine, or would
    produce the same value in every worker and do nothing at all. A cooldown shorter than
    the base would defeat the point, so a negative jitter is clamped rather than obeyed."""
    plain = CircuitBreaker(deployment_id="d").record_failure(T0, jitter=0.5)
    for _ in range(BREAKER_CONSECUTIVE_FAILURES - 1):
        plain = plain.record_failure(T0, jitter=0.5)
    assert plain.cooldown_seconds == BREAKER_BASE_COOLDOWN_SECONDS * 1.5

    negative = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        negative = negative.record_failure(T0, jitter=-5.0)
    assert negative.cooldown_seconds == BREAKER_BASE_COOLDOWN_SECONDS


def test_the_breaker_half_opens_once_the_cooldown_elapses() -> None:
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    later = T0 + timedelta(seconds=BREAKER_BASE_COOLDOWN_SECONDS + 1)
    breaker, admitted = breaker.try_admit(later)
    assert breaker.state is BreakerState.HALF_OPEN
    assert admitted is True


def test_only_one_request_is_admitted_while_half_open() -> None:
    """The half-open probe is one request. Admitting a second means a recovering provider
    is hit by the full load the instant it comes back, which is how a recovery becomes a
    second outage."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    later = T0 + timedelta(seconds=BREAKER_BASE_COOLDOWN_SECONDS + 1)
    breaker, first = breaker.try_admit(later)
    breaker, second = breaker.try_admit(later)
    assert (first, second) == (True, False)


def test_a_successful_probe_closes_the_breaker_and_resets_the_backoff() -> None:
    """The next incident starts its backoff from the base rather than from wherever the
    last one finished, or one bad afternoon leaves a healthy provider on a ten-minute
    cooldown for the rest of the week."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    later = T0 + timedelta(seconds=BREAKER_BASE_COOLDOWN_SECONDS + 1)
    breaker, _ = breaker.try_admit(later)
    breaker = breaker.record_success(later)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.open_streak == 0
    assert breaker.admits(later) is True


def test_a_failed_probe_reopens_with_a_longer_cooldown() -> None:
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    first_cooldown = breaker.cooldown_seconds
    later = T0 + timedelta(seconds=first_cooldown + 1)
    breaker, _ = breaker.try_admit(later)
    breaker = breaker.record_failure(later)
    assert breaker.state is BreakerState.OPEN
    assert breaker.cooldown_seconds > first_cooldown


def test_a_probe_outcome_stays_out_of_the_live_ring() -> None:
    """A probe is one synthetic request. Letting it move the ratio would let the prober
    drive its own verdict, which makes the ratio evidence about the prober."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    before = breaker.live
    later = T0 + timedelta(seconds=breaker.cooldown_seconds + 1)
    breaker, _ = breaker.try_admit(later)
    breaker = breaker.record_failure(later)
    assert breaker.live == before


def test_an_abandoned_probe_is_reclaimed() -> None:
    """A claimant that never reports back would wedge the breaker in half-open forever,
    admitting nothing, with no error anywhere and the deployment permanently out of
    rotation."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    later = T0 + timedelta(seconds=breaker.cooldown_seconds + 1)
    breaker, admitted = breaker.try_admit(later)
    assert admitted is True

    much_later = later + timedelta(seconds=BREAKER_PROBE_CLAIM_TTL_SECONDS + 1)
    breaker, readmitted = breaker.try_admit(much_later)
    assert readmitted is True


def test_a_success_recorded_against_an_open_breaker_does_not_reopen_the_gate() -> None:
    """It means a caller ran an attempt without asking. Letting a stray result close the
    breaker means one retry loop somewhere quietly cancels it for everybody."""
    breaker = CircuitBreaker(deployment_id="d")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    assert breaker.record_success(T0).state is BreakerState.OPEN


def test_the_breaker_never_reads_the_clock_itself() -> None:
    """`now` is a parameter on every transition. A breaker that reads the clock internally
    cannot be tested for the half-open transition, and the half-open transition is the
    part that goes wrong in production.

    Parsed rather than grepped: the class explains the rule in its own docstring, so a
    substring search matches the explanation and passes for the wrong reason. This walks
    the call sites."""
    tree = ast.parse(inspect.getsource(CircuitBreaker))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint({"now", "utcnow", "today", "time", "monotonic"})


def test_chain_selection_does_not_burn_the_half_open_probe() -> None:
    """Selection asks whether a rung would be admitted; only the executor claims. If
    selection claimed, planning a chain would spend the one probe of every rung it
    inspected and never attempted, and a recovering provider behind a skipped rung would
    never get its chance."""
    breaker = CircuitBreaker(deployment_id="a")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    later = T0 + timedelta(seconds=breaker.cooldown_seconds + 1)

    chain = RoutingChain(rungs=(rung(deployment("a"), position=0),))
    selection = chain.select(Tier.MAIN, breakers={"a": breaker}, now=later)
    assert selection.rungs  # half-open still admits

    _, admitted = breaker.try_admit(later)
    assert admitted is True


def test_a_deployment_with_no_breaker_on_file_is_treated_as_healthy() -> None:
    """A provider we have not yet failed against is healthy, not unknown-and-blocked.
    The opposite reading empties every chain on a cold start."""
    chain = seed_chain()
    assert chain.select(Tier.MAIN, breakers={}, now=T0).rungs


def test_supplying_breakers_without_a_clock_is_refused() -> None:
    """Silently skipping the health check would leave a caller believing the breakers were
    consulted while every open one was admitted, which is the failure they exist for."""
    breaker = CircuitBreaker(deployment_id="a")
    chain = RoutingChain(rungs=(rung(deployment("a"), position=0),))
    with pytest.raises(ValueError, match="clock"):
        chain.select(Tier.MAIN, breakers={"a": breaker})


def test_an_open_breaker_removes_its_rung_from_the_chain() -> None:
    breaker = CircuitBreaker(deployment_id="a")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    chain = RoutingChain(
        rungs=(rung(deployment("a"), position=0), rung(deployment("b"), position=1))
    )
    selection = chain.select(Tier.MAIN, breakers={"a": breaker}, now=T0)
    assert [r.deployment.id for r in selection.rungs] == ["b"]
    assert selection.skipped_for(SkipReason.CIRCUIT_OPEN)[0].rung.deployment.id == "a"


# ----------------------------------------------------------------------------- residency
def test_an_unconstrained_scope_admits_every_enabled_rung() -> None:
    chain = seed_chain()
    selection = chain.select(Tier.MAIN)
    assert len(selection.rungs) == len(chain.rungs_for(Tier.MAIN))
    assert selection.skipped == ()


def test_a_global_deployment_never_satisfies_a_region_pin() -> None:
    """It makes no promise about where the request lands. Checking its region column would
    pass today and breach the contract on the first capacity event."""
    anywhere = deployment("g", region="eu-west-1", residency=ResidencyClass.GLOBAL)
    assert EU_ONLY.satisfied_by(anywhere) is False


def test_a_region_pinned_deployment_in_the_allowed_region_satisfies_the_constraint() -> None:
    eu = deployment("e", region="eu-west-1", residency=ResidencyClass.REGION_PINNED)
    assert EU_ONLY.satisfied_by(eu) is True


def test_on_prem_only_admits_nothing_a_provider_hosts() -> None:
    """The strictest form, reached only on a contractual bar to cross-border transfer."""
    requirement = ResidencyRequirement(on_prem_only=True)
    hosted = deployment("h", region="ap-southeast-1", residency=ResidencyClass.REGION_PINNED)
    local = deployment("l", region="on-prem-sg", residency=ResidencyClass.ON_PREM)
    assert requirement.satisfied_by(hosted) is False
    assert requirement.satisfied_by(local) is True


def test_the_chain_skips_a_non_compliant_rung_and_keeps_going() -> None:
    """M5.5.2. The compliant rung sitting third is still used, and the two ahead of it are
    removed rather than the chain stopping at the first one that cannot serve."""
    chain = RoutingChain(
        rungs=(
            rung(deployment("global-1"), position=0),
            rung(
                deployment("us", region="us-east-1", residency=ResidencyClass.REGION_PINNED),
                position=1,
            ),
            rung(
                deployment("eu", region="eu-west-1", residency=ResidencyClass.REGION_PINNED),
                position=2,
            ),
        )
    )
    selection = chain.select(Tier.MAIN, residency=EU_ONLY)
    assert [r.deployment.id for r in selection.rungs] == ["eu"]
    assert {s.rung.deployment.id for s in selection.skipped_for(SkipReason.RESIDENCY)} == {
        "global-1",
        "us",
    }


def test_no_compliant_rung_is_a_refusal_and_never_a_non_compliant_one() -> None:
    """M5.5.4, and the point of the whole module. A fallback that quietly crosses a border
    turns a contractual promise into a breach that surfaces months later in an audit, with
    the evidence in the provider's logs rather than ours."""
    chain = RoutingChain(
        rungs=(
            rung(deployment("global-1"), position=0),
            rung(
                deployment("us", region="us-east-1", residency=ResidencyClass.REGION_PINNED),
                position=1,
            ),
        )
    )
    selection = chain.select(Tier.MAIN, residency=EU_ONLY)
    assert selection.rungs == ()
    assert selection.is_empty is True
    with pytest.raises(NoCompliantRoute):
        selection.require()


def test_the_refusal_names_residency_rather_than_an_outage() -> None:
    """A residency refusal is a policy outcome the asker can act on. Reporting it as an
    outage sends them to wait for a recovery that would not help them."""
    chain = RoutingChain(
        rungs=(rung(deployment("us", region="us-east-1", residency=ResidencyClass.REGION_PINNED)),)
    )
    with pytest.raises(NoCompliantRoute) as caught:
        chain.select(Tier.MAIN, residency=EU_ONLY).require()
    assert "region" in caught.value.public_message
    assert caught.value.outcome is Outcome.DEGRADED


def test_a_residency_skip_outranks_a_breaker_skip_in_the_refusal() -> None:
    """Both emptied the chain, but only one of them is going to resolve itself."""
    breaker = CircuitBreaker(deployment_id="eu")
    for _ in range(BREAKER_CONSECUTIVE_FAILURES):
        breaker = breaker.record_failure(T0)
    chain = RoutingChain(
        rungs=(
            rung(
                deployment("us", region="us-east-1", residency=ResidencyClass.REGION_PINNED),
                position=0,
            ),
            rung(
                deployment("eu", region="eu-west-1", residency=ResidencyClass.REGION_PINNED),
                position=1,
            ),
        )
    )
    selection = chain.select(Tier.MAIN, residency=EU_ONLY, breakers={"eu": breaker}, now=T0)
    with pytest.raises(NoCompliantRoute) as caught:
        selection.require()
    assert "region" in caught.value.public_message


def test_a_disabled_deployment_is_skipped_for_being_disabled_not_for_residency() -> None:
    """An operator who turned a rung off must not be told their data policy blocked it."""
    chain = RoutingChain(rungs=(rung(deployment("off", enabled=False)),))
    selection = chain.select(Tier.MAIN)
    assert selection.skipped_for(SkipReason.DISABLED)
    assert selection.skipped_for(SkipReason.RESIDENCY) == ()


def test_the_refusal_is_a_degraded_outcome_so_the_taxonomy_still_holds() -> None:
    """It joins the five existing outcomes rather than inventing a sixth for one module."""
    assert issubclass(NoCompliantRoute, Degraded)


def test_two_residency_constraints_compose_by_narrowing() -> None:
    """Same guarantee as Scope.intersect: composing can only ever narrow, so the reachable
    set of any pair of constraints is computable by inspection."""
    eu_or_us = ResidencyRequirement(allowed_regions=frozenset({"eu-west-1", "us-east-1"}))
    combined = eu_or_us.intersect(EU_ONLY)
    assert combined.allowed_regions == frozenset({"eu-west-1"})


def test_an_unconstrained_requirement_is_the_identity_of_composition() -> None:
    """None means unconstrained, so it must not narrow anything. Writing the set
    intersection inline at the call site is how this becomes `frozenset() & {...}` and
    silently forbids everything."""
    assert ResidencyRequirement().intersect(EU_ONLY).allowed_regions == frozenset({"eu-west-1"})
    assert EU_ONLY.intersect(ResidencyRequirement()).allowed_regions == frozenset({"eu-west-1"})


def test_two_incompatible_constraints_forbid_everything_rather_than_allowing_everything() -> None:
    """The sharpest edge in the type. An empty allowed set means no region satisfies this;
    None means no constraint. Collapsing them into one value converts "these two policies
    are incompatible" into "route anywhere", which is the exact opposite answer."""
    us_only = ResidencyRequirement(allowed_regions=frozenset({"us-east-1"}))
    impossible = us_only.intersect(EU_ONLY)
    assert impossible.allowed_regions == frozenset()
    assert impossible.is_constrained is True
    for region, residency in (
        ("us-east-1", ResidencyClass.REGION_PINNED),
        ("eu-west-1", ResidencyClass.REGION_PINNED),
        ("global", ResidencyClass.GLOBAL),
    ):
        assert impossible.satisfied_by(deployment("x", region=region, residency=residency)) is False


def test_on_prem_survives_composition_from_either_side() -> None:
    """The strictest of the two wins, in both orders."""
    strict = ResidencyRequirement(on_prem_only=True)
    assert strict.intersect(EU_ONLY).on_prem_only is True
    assert EU_ONLY.intersect(strict).on_prem_only is True


def test_every_region_the_seed_chain_uses_documents_where_data_rests() -> None:
    """M5.5.3. A deployment added in a hurry whose residency claim nobody can substantiate
    when a client asks is a contract problem, and this is where it is cheapest to catch."""
    for r in seed_chain().rungs:
        assert r.deployment.region in REGION_STORAGE
        assert not storage_location(r.deployment).startswith("UNDOCUMENTED")


def test_an_undocumented_region_is_reported_loudly_rather_than_defaulted() -> None:
    assert storage_location(deployment("x", region="mars-1")).startswith("UNDOCUMENTED")


def test_the_global_entry_says_plainly_that_it_promises_nothing() -> None:
    """The entry that matters. Everything else in the registry is a place name."""
    assert "no location is promised" in REGION_STORAGE["global"]


# --------------------------------------------------------------------------- the plan
def test_a_plan_classifies_and_then_filters_in_one_call() -> None:
    result = plan(RoutingRequest(lane=Lane.TASK), seed_chain())
    assert result.decision.tier is Tier.HEAVY
    assert result.depth == 2


def test_a_plan_carries_the_skipped_rungs_so_chain_depth_can_be_alerted_on() -> None:
    """Alerting reads chain depth, not final failure. By the time the last rung fails the
    outage is already obvious; a chain quietly running one rung deeper every day is the
    signal that arrives in time to be useful."""
    result = plan(
        RoutingRequest(lane=Lane.ANSWER, residency=EU_ONLY),
        seed_chain(),
    )
    assert result.depth == 0
    assert len(result.skipped) == 2


def test_a_plan_does_not_raise_so_a_doomed_chain_can_still_be_inspected() -> None:
    """The console and the trace viewer need to show what was skipped and why, and needing
    to catch an exception to read a data structure is how that view stops being built."""
    result = plan(RoutingRequest(lane=Lane.ANSWER, residency=EU_ONLY), seed_chain())
    assert result.selection.is_empty is True
    with pytest.raises(NoCompliantRoute):
        result.selection.require()


def test_the_fast_lane_has_no_chain_to_resolve() -> None:
    """Asking the router for a fast-lane chain is a bug upstream, so it refuses with a
    message that says which layer to look at rather than returning an empty tuple that
    reads like an outage."""
    selection = seed_chain().select(Tier.NONE)
    assert selection.rungs == ()
    with pytest.raises(NoCompliantRoute, match="tier=none"):
        selection.require()


def test_an_empty_selection_with_no_skips_says_nothing_is_configured() -> None:
    """A tier with no rungs at all is a configuration gap, not a policy refusal and not an
    outage, and the three need different fixes."""
    selection = ChainSelection(tier=Tier.SMALL, rungs=())
    with pytest.raises(NoCompliantRoute) as caught:
        selection.require()
    assert "No model is configured" in caught.value.public_message
