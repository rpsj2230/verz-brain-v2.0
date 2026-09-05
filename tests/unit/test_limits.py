"""Rate limits, the verified source ceilings, widget minting and abuse scoring.

`brain.ops.limits` answers "may this caller ask again yet", which is a different question
from `brain.ops.admission`'s "is the machine full" and has a different remedy. Both refuse
before work starts; only this one refuses on something the caller can act on.

The rules that must never break (one principal cannot exhaust a connector, a refusal does
not extend a window, abuse detection has nowhere to refuse) live in
`tests/invariants/test_capacity_invariants.py`. What is here is the arithmetic and the
branches.

Task ids: M23.1.1, M23.1.2, M23.1.3, M23.1.4, M23.1.5, M23.2.1, M23.2.2, M23.2.3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.errors import Outcome
from brain.core.principal import PrincipalKind
from brain.gate.context import TrafficClass
from brain.ops.admission import OPERATOR_ACTION, RefusalKind
from brain.ops.limits import (
    BACKOFF_AFTER_REFUSALS,
    DAY_SECONDS,
    DENIALS_WORTH_NOTICING,
    FRESHDESK_SEARCH_MAX_RECORDS,
    MAX_BACKOFF_SECONDS,
    MINUTE_SECONDS,
    PRINCIPAL_FAIR_SHARE,
    SOURCE_CEILINGS,
    VOLUME_MIN_OBSERVATIONS,
    DenialShape,
    Limit,
    LimiterState,
    LimitScope,
    QuotaExceeded,
    VolumeBand,
    WindowState,
    assess_denials,
    assess_volume,
    backoff_seconds,
    ceilings,
    check,
    connector_ceiling,
    counts_towards_metrics,
    effective_per_day,
    is_automated,
    mint_widget_session,
    principal_share_of,
    request_limits,
    search_completeness,
    source_limits,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _limit(limit: int = 3, *, window: float = MINUTE_SECONDS, subject: str = "p_alice") -> Limit:
    return Limit(
        scope=LimitScope.PRINCIPAL,
        subject=subject,
        period="minute",
        limit=limit,
        window_seconds=window,
    )


def _fill(
    state: LimiterState,
    limit: Limit,
    count: int,
    *,
    start: datetime = NOW,
    step_seconds: float = 1.0,
) -> LimiterState:
    """Record `count` admitted hits, `step_seconds` apart. The step matters: spreading hits
    over more than the window means the early ones have already expired by the last."""
    for offset in range(count):
        state = state.record(start + timedelta(seconds=offset * step_seconds), (limit,))
    return state


# ---------------------------------------------------------------------- the sliding window
def test_a_window_admits_up_to_its_limit_and_not_beyond() -> None:
    """M23.1.1. The ordinary case, and the one a change that refuses everything would still
    pass every other test in this file without."""
    limit = _limit(3)
    state = _fill(LimiterState(), limit, 2)
    assert check(now=NOW + timedelta(seconds=3), limits=(limit,), state=state).allowed
    state = _fill(state, limit, 1, start=NOW + timedelta(seconds=3))
    assert not check(now=NOW + timedelta(seconds=4), limits=(limit,), state=state).allowed


def test_the_window_slides_rather_than_resetting_at_a_boundary() -> None:
    """The whole reason this is not a fixed-window counter. A fixed window with a limit of
    three admits six across two adjacent seconds either side of the boundary, and the thing
    that notices is the connector returning 429 while our own counter says we were fine."""
    limit = _limit(3)
    state = _fill(LimiterState(), limit, 3, start=NOW + timedelta(seconds=55))
    # A fixed minute window would have reset here; a sliding one has not.
    assert not check(now=NOW + timedelta(seconds=61), limits=(limit,), state=state).allowed
    assert check(now=NOW + timedelta(seconds=116), limits=(limit,), state=state).allowed


def test_the_retry_hint_is_the_moment_the_oldest_hit_falls_out() -> None:
    """M23.1.5. The window knows exactly when room appears, so the hint is a fact. A guess
    that is short brings the caller back into a second refusal, and after two of those the
    hint stops being read."""
    limit = _limit(2)
    state = _fill(LimiterState(), limit, 2)
    decision = check(now=NOW + timedelta(seconds=10), limits=(limit,), state=state)
    assert not decision.allowed
    assert decision.retry_after_seconds == pytest.approx(50.0)


def test_a_lowered_limit_still_produces_a_usable_hint() -> None:
    """An operator tightening a limit leaves more hits in the window than the new limit
    allows. Indexing from the wrong end reports a hint from the newest hit, which is a whole
    window too long."""
    state = _fill(LimiterState(), _limit(5), 5)
    tightened = _limit(2)
    decision = check(now=NOW + timedelta(seconds=10), limits=(tightened,), state=state)
    assert not decision.allowed
    # Five hits at t+0..t+4 against a limit of two: four have to expire before a new one
    # fits, so room appears when the hit at t+3 falls out at t+63, ten seconds after now.
    assert decision.retry_after_seconds == pytest.approx(53.0)


def test_a_window_prunes_what_has_fallen_out_of_it() -> None:
    """Without pruning the log grows for ever and the count never drops, so a limit becomes
    a lifetime quota."""
    window = WindowState().record(NOW, MINUTE_SECONDS)
    assert window.count(NOW + timedelta(seconds=30), MINUTE_SECONDS) == 1
    assert window.count(NOW + timedelta(seconds=61), MINUTE_SECONDS) == 0


def test_a_window_with_room_reports_no_wait() -> None:
    """A non-zero hint on a window that would admit is a client told to wait for nothing."""
    assert WindowState().retry_after(NOW, _limit(3)) == 0.0


# ------------------------------------------------------------------- several limits at once
def test_the_retry_hint_is_the_longest_of_every_binding_limit() -> None:
    """A one-second per-principal hint handed out while a fifty-second connector limit is
    also over is worse than no hint: the caller comes back, is refused again, and learns to
    ignore it."""
    short = Limit(
        scope=LimitScope.PRINCIPAL, subject="p", period="10s", limit=1, window_seconds=10.0
    )
    long = Limit(
        scope=LimitScope.CONNECTOR, subject="xero", period="minute", limit=1, window_seconds=60.0
    )
    state = LimiterState().record(NOW, (short, long))
    decision = check(now=NOW + timedelta(seconds=5), limits=(short, long), state=state)
    assert not decision.allowed
    assert decision.binding is long
    assert decision.retry_after_seconds == pytest.approx(55.0)
    assert len(decision.over) == 2


def test_every_applicable_limit_is_recorded_not_only_the_binding_one() -> None:
    """A request that consumed a Xero call consumed it from the connector's minute, the
    connector's day and the caller's share. Recording one of the three lets the other two
    drift until they mean nothing."""
    limits = source_limits("xero", principal_id="p_alice")
    state = LimiterState().record(NOW, limits)
    assert set(state.windows) == {limit.key for limit in limits}


def test_nothing_is_recorded_by_checking() -> None:
    """`check` must be free to call. If checking recorded a hit, a console previewing a
    caller's headroom would consume it."""
    limit = _limit(3)
    state = LimiterState()
    check(now=NOW, limits=(limit,), state=state)
    assert state.windows == {}


# ------------------------------------------------------------------------- limit validation
def test_a_limit_of_zero_is_refused() -> None:
    """Zero is how somebody suspends a caller by editing a number, and the row still reads
    as a configured limit. Suspension is a different change and belongs where it is seen."""
    with pytest.raises(ValueError, match="minimum is 1"):
        _limit(0)


def test_a_limit_needs_a_subject() -> None:
    """An unkeyed window counts everybody together, so one busy person refuses the company
    and the console shows a per-principal limit that is nothing of the kind."""
    with pytest.raises(ValueError, match="needs a subject"):
        _limit(subject="")


def test_a_limit_needs_a_window() -> None:
    """A limit over no time is a lifetime quota wearing the wrong name."""
    with pytest.raises(ValueError, match="non-positive window"):
        _limit(window=0.0)


# ---------------------------------------------------------------- per principal, per connector
def test_one_principal_cannot_take_a_whole_connectors_minute() -> None:
    """The first half of the pair. Without the fair share, a single backfill takes the whole
    of Xero's minute and everybody else's questions fail while that person's succeed."""
    xero = connector_ceiling("xero")
    assert xero is not None
    assert principal_share_of(xero.per_minute) < xero.per_minute


def test_a_connector_is_exhausted_by_the_sum_of_individually_reasonable_callers() -> None:
    """The second half. Five people each inside their quarter share add up to more than the
    connector allows, and only the connector-wide window catches it."""
    alice = source_limits("lark_base", principal_id="p_alice")
    connector = next(limit for limit in alice if limit.scope is LimitScope.CONNECTOR)
    share = next(limit for limit in alice if limit.scope is LimitScope.PRINCIPAL_CONNECTOR)
    # Everybody else has filled the connector's minute. Alice herself has asked for nothing,
    # so nothing about her individually is unreasonable.
    state = _fill(LimiterState(), connector, connector.limit, step_seconds=0.5)
    assert state.window_for(share.key).count(NOW, MINUTE_SECONDS) == 0

    decision = check(now=NOW + timedelta(seconds=50), limits=alice, state=state)
    assert not decision.allowed
    assert decision.binding is not None
    assert decision.binding.scope is LimitScope.CONNECTOR


def test_both_the_connector_minute_and_the_connector_day_apply() -> None:
    """Xero is 60 a minute and 5,000 a day, and a backfill that respects the minute still
    reaches the day. Keeping only the minute makes the day invisible until Xero refuses."""
    limits = source_limits("xero", principal_id="p_alice")
    periods = {(limit.scope, limit.period) for limit in limits}
    assert (LimitScope.CONNECTOR, "minute") in periods
    assert (LimitScope.CONNECTOR, "day") in periods
    assert (LimitScope.PRINCIPAL_CONNECTOR, "minute") in periods


def test_an_unknown_connector_gets_no_invented_ceiling() -> None:
    """A default here would look verified and would not be. The connector budget in
    admission is the conservative fallback; a made-up rate limit would be a number somebody
    later quotes."""
    assert source_limits("hubspot", principal_id="p_alice") == ()
    assert connector_ceiling("hubspot") is None


def test_a_connector_ceiling_of_one_serialises_everybody_rather_than_rounding_to_zero() -> None:
    """A share that rounds to nothing takes every individual caller out of service while the
    connector reads as idle, which is a worse failure than admitting the connector is a
    bottleneck."""
    assert principal_share_of(1) == 1
    assert principal_share_of(2) == 1


def test_the_fair_share_is_a_share_and_not_the_whole() -> None:
    """At 1.0 the per-principal limit is the connector limit, one person can take all of it,
    and half of the two-limits rule stops being true while both limits still exist."""
    assert 0 < PRINCIPAL_FAIR_SHARE < 1


# --------------------------------------------------------------- per channel and per agent
def test_request_limits_adds_a_window_per_dimension_and_removes_none() -> None:
    """M23.1.2, M23.1.3. 'Which limits apply' is exactly the decision a refactor drops a line
    from, and a dropped line is a limit that stops existing while the console still lists
    it."""
    bare = request_limits(principal_id="p_alice", channel="console")
    with_agent = request_limits(principal_id="p_alice", channel="console", agent_id="a_finance")
    with_both = request_limits(
        principal_id="p_alice", channel="console", agent_id="a_finance", connector="xero"
    )
    assert {limit.scope for limit in bare} == {LimitScope.PRINCIPAL, LimitScope.CHANNEL}
    assert len(with_agent) == len(bare) + 1
    assert len(with_both) == len(with_agent) + 3


def test_the_per_principal_limit_is_far_above_any_human_rate() -> None:
    """The estate runs at about six questions a minute across 126 people. A limit tuned near
    real behaviour catches a busy Monday; this one catches a loop."""
    bare = request_limits(principal_id="p_alice", channel="console")
    principal = next(limit for limit in bare if limit.scope is LimitScope.PRINCIPAL)
    assert principal.limit >= 30


# ------------------------------------------------------------------------ backoff and hints
def test_the_backoff_is_exact_until_a_client_stops_reading_it() -> None:
    """M23.1.5. Doubling from the first refusal punishes a client that obeyed the hint. A
    client refused four times in a row has had the exact answer three times already."""
    for refusals in range(BACKOFF_AFTER_REFUSALS + 1):
        assert backoff_seconds(10.0, consecutive_refusals=refusals) == 10.0
    assert backoff_seconds(10.0, consecutive_refusals=BACKOFF_AFTER_REFUSALS + 1) == 20.0


def test_the_backoff_is_capped() -> None:
    """Uncapped doubling leaves a client that has since been fixed still waiting hours after
    the fix shipped."""
    assert backoff_seconds(10.0, consecutive_refusals=40) == MAX_BACKOFF_SECONDS


def test_jitter_only_ever_lengthens_a_hint() -> None:
    """Negative jitter shortens a hint below the time room can possibly appear, which
    guarantees the retry it produces is refused."""
    assert backoff_seconds(10.0, consecutive_refusals=0, jitter=0.5) == 15.0
    assert backoff_seconds(10.0, consecutive_refusals=0, jitter=-5.0) == 10.0


def test_a_negative_refusal_count_is_refused() -> None:
    """A negative count would divide the hint rather than multiply it, so a bookkeeping bug
    would turn the backoff into an accelerator."""
    with pytest.raises(ValueError, match="cannot be negative"):
        backoff_seconds(10.0, consecutive_refusals=-1)


def test_a_quota_refusal_is_distinguishable_from_a_capacity_refusal() -> None:
    """To the person they read alike. To an operator they must not: one says raise this
    caller's allowance, the other says add capacity."""
    limit = _limit(1)
    state = _fill(LimiterState(), limit, 1)
    decision = check(now=NOW + timedelta(seconds=1), limits=(limit,), state=state)
    record = decision.log_record()
    assert record["refusal_kind"] == RefusalKind.QUOTA
    assert record["operator_action"] == OPERATOR_ACTION[RefusalKind.QUOTA]
    error = decision.as_error()
    assert isinstance(error, QuotaExceeded)
    assert error.outcome is Outcome.FAILED


def test_an_allowed_decision_has_no_refusal_to_raise() -> None:
    """Raising a plausible-looking error for an allowed request lets a mistaken branch
    refuse somebody who was within every limit."""
    decision = check(now=NOW, limits=(_limit(3),), state=LimiterState())
    with pytest.raises(ValueError, match="no refusal to raise"):
        decision.as_error()


# ------------------------------------------------------------------ the verified ceilings
def test_the_source_ceilings_match_the_verified_table() -> None:
    """These are constraints, not guidance. Xero's 5,000 a day is the one a backfill reaches
    first; Lark Base's 100 a minute cannot be raised at any price. A number rounded up here
    is a number that produces 429s in production and looks right in review."""
    by_name = {c.name: c for c in SOURCE_CEILINGS}
    assert by_name["xero"].per_minute == 60
    assert by_name["xero"].per_day == 5_000
    assert by_name["freshdesk"].per_minute == 100
    assert by_name["lark_base"].per_minute == 100
    assert by_name["lark_base"].raisable is False


def test_a_derived_daily_ceiling_is_marked_as_derived() -> None:
    """A daily figure calculated from a per-minute one assumes even arrival across 1,440
    minutes, which office traffic is not. Unmarked, it sits beside a published figure looking
    equally solid."""
    lark = connector_ceiling("lark_base")
    assert lark is not None
    per_day, derived = effective_per_day(lark)
    assert per_day == 144_000
    assert derived is True
    xero = connector_ceiling("xero")
    assert xero is not None
    assert effective_per_day(xero) == (5_000, False)


def test_every_verified_source_reaches_the_bottleneck_ladder() -> None:
    """Omitting the sources with no published daily figure was the first version, and the
    ladder then showed one source, which reads as 'only Xero is a constraint' rather than
    'only Xero's constraint is expressible in this unit'."""
    assert {c.name for c in ceilings()} == {c.name for c in SOURCE_CEILINGS}


def test_a_day_window_is_a_day() -> None:
    """Xero's daily ceiling counted over the wrong window is not a daily ceiling."""
    day = next(limit for limit in source_limits("xero", principal_id="p") if limit.period == "day")
    assert day.window_seconds == DAY_SECONDS


# --------------------------------------------------------------- the search-result ceiling
def test_a_freshdesk_search_at_the_cap_is_not_complete() -> None:
    """Freshdesk search returns at most 300 records ever. It is not a page size and cannot be
    paged past, so 'all tickets matching' is silently wrong beyond 300 and looks correct in
    every test anybody writes, because no test has 301 matching tickets."""
    result = search_completeness("freshdesk", FRESHDESK_SEARCH_MAX_RECORDS)
    assert result.complete is False
    assert "must not be summarised" in result.reason


def test_a_freshdesk_search_short_of_the_cap_is_complete() -> None:
    """Marking every search incomplete would make the signal useless, and the abstention it
    triggers would fire on every helpdesk question."""
    assert search_completeness("freshdesk", FRESHDESK_SEARCH_MAX_RECORDS - 1).complete is True


def test_a_connector_with_no_declared_cap_is_treated_as_complete() -> None:
    """Inventing a cap for a source that has not published one would abstain on answers that
    are actually whole."""
    result = search_completeness("xero", 5_000)
    assert result.complete is True
    assert result.cap is None


def test_a_negative_result_count_is_refused() -> None:
    """A negative count would compare below any cap and report a truncated result as
    complete."""
    with pytest.raises(ValueError, match="negative number of records"):
        search_completeness("freshdesk", -1)


# ------------------------------------------------------------------- widget session minting
def test_minting_stops_at_the_mint_rate() -> None:
    """M23.1.4. A script minting sessions in a loop is the fast version of widget abuse, and
    every session it opens is a door to the answer path."""
    limit = Limit(
        scope=LimitScope.WIDGET_ORIGIN,
        subject="verzdesign.com",
        period="minute",
        limit=10,
        window_seconds=MINUTE_SECONDS,
    )
    state = _fill(LimiterState(), limit, 10)
    decision = mint_widget_session(
        now=NOW + timedelta(seconds=1), origin="verzdesign.com", state=state, live_sessions=0
    )
    assert decision.minted is False
    assert decision.retry_after_seconds > 0


def test_minting_stops_at_the_live_session_ceiling_without_touching_existing_sessions() -> None:
    """The slow version: one mint a minute all day never trips a rate limit. Refusing a new
    mint is not refusing a question, and every session already open keeps working."""
    decision = mint_widget_session(
        now=NOW, origin="verzdesign.com", state=LimiterState(), live_sessions=20
    )
    assert decision.minted is False
    assert "existing sessions are unaffected" in decision.reason


def test_an_ordinary_mint_succeeds() -> None:
    """Without this, a change that refused every mint would pass both guards above."""
    decision = mint_widget_session(
        now=NOW, origin="verzdesign.com", state=LimiterState(), live_sessions=3
    )
    assert decision.minted is True


def test_an_unkeyed_mint_is_refused() -> None:
    """A mint with no origin counts every site together, so one busy customer's widget
    refuses everybody else's."""
    with pytest.raises(ValueError, match="unkeyed mint"):
        mint_widget_session(now=NOW, origin="", state=LimiterState(), live_sessions=0)


# ------------------------------------------------------------------------ abuse detection
def test_volume_below_the_observation_floor_is_never_notable() -> None:
    """M23.2.1. Two questions against a baseline of 0.2 is a 10x spike and is also somebody
    asking two questions. Without the floor, every quiet account produces an alert on its
    first busy hour."""
    assessment = assess_volume(observed=2, baseline=0.2)
    assert assessment.band is VolumeBand.ORDINARY
    assert not assessment.is_notable


def test_a_principal_with_no_history_is_ordinary_rather_than_infinite() -> None:
    """A zero baseline divides to infinity, so the first week after a rollout would be
    nothing but alerts and the detector would be switched off before it ever caught
    anything."""
    assessment = assess_volume(observed=1_000, baseline=0.0)
    assert assessment.band is VolumeBand.ORDINARY
    assert assessment.ratio == 0.0


def test_volume_bands_are_ordered_and_reachable() -> None:
    """Bands nobody can reach are a scale with one value on it, and a spike would read the
    same as a quiet afternoon."""
    baseline = 4.0
    ordinary = assess_volume(observed=VOLUME_MIN_OBSERVATIONS, baseline=baseline)
    notable = assess_volume(observed=20, baseline=4.0)
    extreme = assess_volume(observed=60, baseline=4.0)
    assert ordinary.band <= notable.band < extreme.band
    assert extreme.is_extreme


def test_a_negative_volume_is_refused() -> None:
    """A negative count would produce a negative ratio, which sorts below every band and
    would hide the principal from the report entirely."""
    with pytest.raises(ValueError, match="cannot be negative"):
        assess_volume(observed=-1, baseline=1.0)


def test_a_handful_of_denials_says_nothing() -> None:
    """M23.2.2. People mistype client names. An alert on three denials is an alert nobody
    reads by the end of the first week."""
    assert assess_denials(denials=3, distinct_targets=3).shape is DenialShape.ORDINARY
    assert not assess_denials(denials=3, distinct_targets=3).is_worth_alerting


def test_denials_against_one_target_route_to_whoever_can_grant_access() -> None:
    """Twenty denials against one record is somebody who needs that record. Reporting it as
    an attack means the access request never reaches the admin who could grant it."""
    assessment = assess_denials(denials=DENIALS_WORTH_NOTICING + 12, distinct_targets=1)
    assert assessment.shape is DenialShape.ACCESS_NEEDED


def test_denials_across_many_targets_are_enumeration() -> None:
    """Breadth rather than persistence: somebody finding out what exists. It goes to a
    different person from an access request, and merging the two means one of them is
    always ignored."""
    assessment = assess_denials(denials=20, distinct_targets=18)
    assert assessment.shape is DenialShape.ENUMERATION
    assert assessment.is_worth_alerting


def test_more_distinct_targets_than_denials_is_refused() -> None:
    """Impossible arithmetic upstream would otherwise be reported as extreme enumeration,
    and somebody would spend an afternoon on a counting bug."""
    with pytest.raises(ValueError, match="not possible"):
        assess_denials(denials=3, distinct_targets=9)


# --------------------------------------------------------------------- automated traffic
def test_a_service_principal_is_machine_traffic_on_any_channel() -> None:
    """M23.2.3. A service account arriving on an interactive channel is still a machine, and
    counting it as a person inflates every per-person number in the console."""
    assert is_automated(PrincipalKind.SERVICE, TrafficClass.HUMAN_INTERACTIVE)


def test_a_person_on_the_scheduler_is_still_machine_traffic() -> None:
    """A report that runs nightly under somebody's name is not that person asking a
    question. Counting it as one is how 'fast lane share' quietly stops meaning anything."""
    assert is_automated(PrincipalKind.HUMAN, TrafficClass.SYSTEM)
    assert is_automated(PrincipalKind.HUMAN, TrafficClass.AUTOMATION)


def test_a_person_on_an_interactive_channel_counts_towards_the_metrics() -> None:
    """If nothing counted, the exclusion would be total and the numbers §22 asks for would
    be empty rather than clean."""
    assert counts_towards_metrics(PrincipalKind.HUMAN, TrafficClass.HUMAN_INTERACTIVE)
    assert counts_towards_metrics(PrincipalKind.HUMAN, TrafficClass.HUMAN_ASYNC)
    assert not counts_towards_metrics(PrincipalKind.SERVICE, TrafficClass.AUTOMATION)
