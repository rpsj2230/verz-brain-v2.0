"""Whether a provider is actually working, including when nobody has asked it lately.

`routing.CircuitBreaker` is the state machine for one deployment's health. It is correct
and it is not sufficient on its own, for two reasons this module exists to fix.

**A breaker only learns from traffic.** Every rule it has (three consecutive failures, more
than half a window failed) needs live requests to fire. At roughly 0.1 requests per second
across the whole estate, a deployment that is second or third in its chain can go an entire
evening without a single live request, so a provider that died at six o'clock is still
listed healthy at nine and the first person to need the fallback discovers it for us.
`ProviderHealth` adds a probe ring beside the live ring, and a prober that says which
deployments are worth asking, so an idle-but-broken provider becomes visible without
waiting for somebody to trip over it.

**Probe evidence and live evidence must not be mixed.** A probe is one synthetic request we
chose to send. If its outcome went into the live ring it would move the fail ratio, and the
fail ratio is what decides whether to open, so the prober would be voting on its own
verdict: three probes against a provider that is merely slow would open a breaker that live
traffic was passing through perfectly well. The rings are separate here, and
`PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING` is the written form of the argument.

The third thing here is alerting, and it is a different shape from what most systems ship.
**We alert on chain depth, not on final failure.** A chain that reached rung three and then
succeeded is the event worth knowing about: the person got their answer, so nothing is
visibly broken, and the primary provider is dead. Alerting on total failure means the first
notification arrives when every rung is down, which is a report rather than a warning. See
`DEPTH_NOT_FINAL_FAILURE`.

Everything here is a pure state machine. `now` is always a parameter, nothing sleeps,
nothing spawns a thread, and the prober is a function that answers "what should be probed
next" for a scheduler to act on. A prober that owned a timer could not be tested for the
case that actually goes wrong, which is a probe that never comes back.

This is the health *type*, not the health *table*. `provider_health` as a Postgres row
lives in `brain.tables`; nothing here imports SQLAlchemy or knows a column name.

Task ids: M5.4.3, M5.4.4, M5.4.7, M5.4.8
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from brain.models.routing import (
    BREAKER_CONSECUTIVE_FAILURES,
    BREAKER_FAIL_RATIO,
    BREAKER_RATIO_MIN_SAMPLES,
    BreakerState,
    CircuitBreaker,
    FallbackTrigger,
    SkippedRung,
    SkipReason,
    Tier,
)

# ------------------------------------------------------------------- written-down reasons
#: Why the prober's outcomes are kept out of the ring that decides the verdict.
PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING = (
    "A probe is one synthetic request we chose to send. Letting its outcome into the live "
    "ring lets it move the fail ratio, and the fail ratio is what decides whether to open, "
    "so the prober would be voting on its own verdict: three probes against a merely slow "
    "provider would open a breaker that live traffic was passing through fine. Probe "
    "evidence opens a breaker only under its own rule, and only when there is no live "
    "evidence at all."
)

#: Why the alert fires on depth rather than on the chain running out.
DEPTH_NOT_FINAL_FAILURE = (
    "A chain that reached rung three and then succeeded is the interesting event: the "
    "person got their answer, so nothing looks broken, and the primary provider is dead. "
    "Alerting only on final failure means the first notification arrives when every rung is "
    "down, which is a report rather than a warning."
)


# --------------------------------------------------------------------- the opening rules
#: How many recent probe outcomes the probe ring keeps. Smaller than the live ring
#: (`BREAKER_LIVE_WINDOW`, 20) because at a sixty-second interval ten probes is ten minutes
#: of history, and evidence older than the cooldown ceiling is not evidence about now.
PROBE_WINDOW = 10

#: Probe failures that open a breaker whose live ring is empty. Two, not one: a single
#: failed synthetic request is as likely to be our own network as the provider's, and
#: opening on it would take a deployment out of rotation every time a DNS lookup hiccuped.
#: Two consecutive failures a minute apart is a provider, not a blip.
PROBE_FAILURES_TO_OPEN_IDLE = 2

#: How often an idle deployment is worth asking about. Sixty seconds is the M5.4.7 figure
#: and it is the right order of magnitude: short enough that an evening outage is caught in
#: a minute rather than at the next person's request, long enough that probing the whole
#: estate costs a rounding error against 0.1 requests per second of real traffic.
PROBE_INTERVAL_SECONDS = 60.0

#: How long live evidence stays evidence. A tier stays cache-warm only above roughly one
#: request per five minutes during office hours, so below that rate a full live ring is a
#: record of the past rather than a statement about now, and the deployment needs probing
#: even though its ring is not empty.
LIVE_EVIDENCE_STALE_SECONDS = 300.0


class OpenReason(enum.StrEnum):
    """Which of the three M5.4.4 rules fired.

    Recorded rather than summarised, for the reason `SkipReason` is: "the breaker opened"
    sends an operator to look at the provider, whereas "the breaker opened on two probe
    failures with no live traffic at all" tells them the outage started before anybody
    noticed, and those are different investigations.
    """

    CONSECUTIVE_LIVE_FAILURES = "consecutive_live_failures"
    LIVE_FAIL_RATIO = "live_fail_ratio"
    PROBE_FAILURES_WHILE_IDLE = "probe_failures_while_idle"


def opens_now(
    *,
    live: Sequence[bool],
    consecutive_failures: int,
    probe: Sequence[bool],
) -> OpenReason | None:
    """The complete M5.4.4 rule, in one readable place, or None to stay closed.

    The first two rules are also implemented inside `CircuitBreaker.record_failure`, which
    owns the transitions. That duplication is deliberate and bounded: this function is the
    written statement of the rule, the one a reviewer reads to check the policy, and
    `test_the_written_rule_and_the_breaker_agree_about_live_failures` fails if the two ever
    disagree. The alternative was to leave the policy existing only as scattered conditions
    inside a transition method, which is how a threshold gets changed in one of two places.

    The third rule is owned here outright, because a probe outcome must never reach
    `record_failure` at all: see `PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING`.
    """
    if consecutive_failures >= BREAKER_CONSECUTIVE_FAILURES:
        return OpenReason.CONSECUTIVE_LIVE_FAILURES
    # The minimum-sample guard is the whole difference between a ratio rule and a hair
    # trigger. One failure out of one request is a 100% failure ratio and means nothing at
    # all; without this the first failure against a cold deployment opens its breaker and
    # the fallback chain shortens itself on no evidence.
    if len(live) >= BREAKER_RATIO_MIN_SAMPLES:
        failures = sum(1 for ok in live if not ok)
        if failures / len(live) > BREAKER_FAIL_RATIO:
            return OpenReason.LIVE_FAIL_RATIO
    # Probe evidence counts only where there is no live evidence to contradict it. With
    # live traffic flowing the live rules are strictly better informed, and a probe that
    # fails while real requests succeed is telling us about the prober's path, not the
    # provider's health.
    if not live:
        recent = tuple(probe)[-PROBE_FAILURES_TO_OPEN_IDLE:]
        if len(recent) >= PROBE_FAILURES_TO_OPEN_IDLE and not any(recent):
            return OpenReason.PROBE_FAILURES_WHILE_IDLE
    return None


# ----------------------------------------------------------------------- provider health
def _push_probe(ring: tuple[bool, ...], outcome: bool) -> tuple[bool, ...]:
    return (*ring, outcome)[-PROBE_WINDOW:]


def _carry_probe(
    probe: tuple[bool, ...], before: BreakerState, after: BreakerState
) -> tuple[bool, ...]:
    """Probe evidence is scoped to one breaker state, and is dropped when the state moves.

    Without this a deployment that recovered would carry its old probe failures forward,
    so the next single failure would look like the second of two and reopen the breaker
    immediately. The live ring needs no equivalent because it is a ratio over a window that
    ages out on its own; the probe rule is a count, and a count has to be reset by
    something.
    """
    return () if before is not after else probe


@dataclass(frozen=True)
class ProviderHealth:
    """One deployment's health: the breaker, plus the evidence a breaker cannot gather.

    Frozen, and every transition returns a new instance, matching `CircuitBreaker`. `now`
    is a parameter everywhere for the same reason it is there: a health record that read
    the clock could not be tested for the case where a probe never reports back, and that
    is the case that wedges a deployment out of rotation permanently.

    The deployment id is read off the breaker rather than stored again. Two fields holding
    one value is two fields that can disagree, and the one a reader would trust is whichever
    the console happened to render.
    """

    breaker: CircuitBreaker
    #: Recent probe outcomes, oldest first, capped at PROBE_WINDOW. Never merged into
    #: `breaker.live`. See PROBE_OUTCOMES_STAY_OUT_OF_THE_LIVE_RING.
    probe: tuple[bool, ...] = ()
    #: When a probe was last dispatched, not when one last answered. The interval measures
    #: how often we ask; measuring from the answer would let a provider that never replies
    #: suppress its own probing exactly when it most needs probing.
    last_probe_at: datetime | None = None
    #: When live traffic last reached this deployment, so a full but stale live ring can be
    #: recognised as history rather than as a statement about now.
    last_live_at: datetime | None = None

    @classmethod
    def for_deployment(cls, deployment_id: str) -> ProviderHealth:
        """A deployment we have never failed against: healthy, with no evidence either way.

        Closed rather than unknown, matching `RoutingChain.select`, which treats a
        deployment with no breaker on file as admitting. Starting unknown-and-blocked would
        mean a fresh install serves nothing until something probes every rung.
        """
        return cls(breaker=CircuitBreaker(deployment_id=deployment_id))

    # -------------------------------------------------------------------- derived state
    @property
    def deployment_id(self) -> str:
        return self.breaker.deployment_id

    @property
    def state(self) -> BreakerState:
        return self.breaker.state

    @property
    def live(self) -> tuple[bool, ...]:
        """The live ring, read-only, so a caller never has to reach through to the breaker
        and is never tempted to build one combined ring on the way past."""
        return self.breaker.live

    def probe_fail_ratio(self) -> float:
        """The probe ring's own ratio. Reported, never fed into the opening rules: the
        probe rule is a count on an empty live ring, and a second ratio would be a second
        way for synthetic traffic to reach the verdict."""
        if not self.probe:
            return 0.0
        return sum(1 for ok in self.probe if not ok) / len(self.probe)

    def would_open(self) -> OpenReason | None:
        """Why this deployment is unhealthy right now, by the written rule. For the
        console, and for the test that keeps the written rule and the breaker in step."""
        return opens_now(
            live=self.breaker.live,
            consecutive_failures=self.breaker.consecutive_failures,
            probe=self.probe,
        )

    # --------------------------------------------------------------------- transitions
    def advance(self, now: datetime) -> ProviderHealth:
        """Apply what the passage of time alone implies. Admits nothing, probes nothing."""
        breaker = self.breaker.advance(now)
        if breaker is self.breaker:
            return self
        return replace(
            self,
            breaker=breaker,
            probe=_carry_probe(self.probe, self.breaker.state, breaker.state),
        )

    def record_live_success(self, now: datetime) -> ProviderHealth:
        """A real request succeeded. Goes to the breaker's live ring and never to the probe
        ring: a live outcome is the better evidence and does not need corroborating."""
        breaker = self.breaker.record_success(now)
        return replace(
            self,
            breaker=breaker,
            probe=_carry_probe(self.probe, self.breaker.state, breaker.state),
            last_live_at=now,
        )

    def record_live_failure(self, now: datetime, *, jitter: float = 0.0) -> ProviderHealth:
        """A real request failed. The breaker owns whether that opens anything."""
        breaker = self.breaker.record_failure(now, jitter=jitter)
        return replace(
            self,
            breaker=breaker,
            probe=_carry_probe(self.probe, self.breaker.state, breaker.state),
            last_live_at=now,
        )

    def claim_probe(self, now: datetime) -> tuple[ProviderHealth, bool]:
        """Claim the right to probe, and stamp that we asked.

        Split from `record_probe` for exactly the reason `CircuitBreaker.admits` is split
        from `try_admit`: deciding which deployments to probe must not consume the half-open
        admission of every deployment it inspects. `probe_verdict` inspects, this claims,
        and only a caller that gets True back should send anything.

        The stamp lands here rather than on the outcome so two workers running the same
        sixty-second tick do not both probe one idle deployment, which would put two
        failures from a single incident into the ring and open the breaker on one event.
        """
        breaker, admitted = self.breaker.try_admit(now)
        claimed = replace(
            self,
            breaker=breaker,
            probe=_carry_probe(self.probe, self.breaker.state, breaker.state),
            last_probe_at=now if admitted else self.last_probe_at,
        )
        return claimed, admitted

    def record_probe(self, *, ok: bool, now: datetime, jitter: float = 0.0) -> ProviderHealth:
        """A probe reported back. Its outcome enters the probe ring and nothing else.

        Three branches, because the breaker's state changes what a probe means:

        - HALF_OPEN: the probe *is* the admission decision, and this is the only path that
          hands an outcome to the breaker. It is safe because `record_success` and
          `record_failure` both return without touching `live` in that state, which is a
          property this module depends on rather than a coincidence it relies on quietly:
          `test_a_half_open_probe_does_not_touch_the_live_ring` pins it.
        - OPEN: still cooling. A result arriving here came from a claim that outlived its
          cooldown; it is filed and changes nothing, because letting a straggler close or
          reopen the gate would override an evaluation somebody has since redone.
        - CLOSED: the outcome is evidence only, and opens the breaker under exactly one
          rule, `OpenReason.PROBE_FAILURES_WHILE_IDLE`.
        """
        current = self.advance(now)
        probe = _push_probe(current.probe, ok)
        breaker = current.breaker

        if breaker.state is BreakerState.HALF_OPEN:
            settled = (
                breaker.record_success(now) if ok else breaker.record_failure(now, jitter=jitter)
            )
            return replace(
                current,
                breaker=settled,
                probe=_carry_probe(probe, breaker.state, settled.state),
                last_probe_at=now,
            )

        if breaker.state is BreakerState.OPEN:
            return replace(current, probe=probe, last_probe_at=now)

        reason = opens_now(
            live=breaker.live,
            consecutive_failures=breaker.consecutive_failures,
            probe=probe,
        )
        if reason is OpenReason.PROBE_FAILURES_WHILE_IDLE:
            # Opening without recording a live failure, which is precisely what must not
            # happen here: a probe is one synthetic request and letting it move the live
            # ratio would let the prober drive its own verdict. `CircuitBreaker.open`
            # exists for this, so there is one implementation of opening rather than a
            # second copy of the streak and backoff arithmetic in a module with no
            # business owning it.
            settled = breaker.open(now, jitter=jitter)
            return replace(
                current,
                breaker=settled,
                probe=_carry_probe(probe, breaker.state, settled.state),
                last_probe_at=now,
            )
        return replace(current, probe=probe, last_probe_at=now)


# ------------------------------------------------------------------- the prober (M5.4.7)
class ProbeReason(enum.StrEnum):
    """Why a deployment is worth probing now."""

    #: The cooldown has elapsed and nobody holds the single admission. The prober is the
    #: right caller for it: a real request arriving here spends a person's wait on finding
    #: out whether a provider is back.
    HALF_OPEN_ADMISSION = "half_open_admission"
    #: Closed, and no live outcome has ever been recorded. The breaker's own rules cannot
    #: fire without traffic, so without a probe this deployment is healthy by assumption.
    IDLE_NO_LIVE_EVIDENCE = "idle_no_live_evidence"
    #: Closed with a live ring, but the last live outcome is older than the window in which
    #: it means anything.
    STALE_LIVE_EVIDENCE = "stale_live_evidence"


@dataclass(frozen=True)
class ProbeVerdict:
    """Whether to probe this deployment, and the argument either way.

    The not-due verdicts carry their reason too, because the console question that matters
    is "why has nothing probed this dead-looking provider for ten minutes", and a function
    that returns only the due ones cannot answer it.
    """

    deployment_id: str
    due: bool
    reason: ProbeReason | None
    detail: str


def _seconds_since(when: datetime | None, now: datetime) -> float | None:
    return None if when is None else (now - when).total_seconds()


def probe_verdict(
    health: ProviderHealth,
    now: datetime,
    *,
    interval_seconds: float = PROBE_INTERVAL_SECONDS,
    stale_after_seconds: float = LIVE_EVIDENCE_STALE_SECONDS,
) -> ProbeVerdict:
    """Should this deployment be probed at `now`, and why or why not. Pure.

    Note the order. Half-open outranks everything, because that is the one state where a
    probe changes the deployment's availability rather than merely its record. The interval
    is checked before the idle rules so a burst of scheduler ticks cannot become a burst of
    probes aimed at one struggling provider.
    """
    current = health.advance(now)
    breaker = current.breaker

    if breaker.state is BreakerState.OPEN:
        until = breaker.cooldown_until
        return ProbeVerdict(
            deployment_id=current.deployment_id,
            due=False,
            reason=None,
            detail=(
                f"open and cooling until {until.isoformat()}"
                if until is not None
                else "open with no cooldown recorded"
            ),
        )

    if breaker.state is BreakerState.HALF_OPEN:
        if breaker.probe_claimed_at is not None:
            return ProbeVerdict(
                deployment_id=current.deployment_id,
                due=False,
                reason=None,
                detail="another caller holds the half-open admission",
            )
        return ProbeVerdict(
            deployment_id=current.deployment_id,
            due=True,
            reason=ProbeReason.HALF_OPEN_ADMISSION,
            detail="cooldown elapsed and the single admission is unclaimed",
        )

    since_probe = _seconds_since(current.last_probe_at, now)
    if since_probe is not None and since_probe < interval_seconds:
        return ProbeVerdict(
            deployment_id=current.deployment_id,
            due=False,
            reason=None,
            detail=f"probed {since_probe:.0f}s ago, inside the {interval_seconds:.0f}s interval",
        )

    if not breaker.live:
        return ProbeVerdict(
            deployment_id=current.deployment_id,
            due=True,
            reason=ProbeReason.IDLE_NO_LIVE_EVIDENCE,
            detail="closed with no live outcome on record, so healthy only by assumption",
        )

    since_live = _seconds_since(current.last_live_at, now)
    if since_live is None or since_live >= stale_after_seconds:
        return ProbeVerdict(
            deployment_id=current.deployment_id,
            due=True,
            reason=ProbeReason.STALE_LIVE_EVIDENCE,
            detail=(
                "live ring has entries but no recent traffic"
                if since_live is None
                else f"last live outcome was {since_live:.0f}s ago"
            ),
        )

    return ProbeVerdict(
        deployment_id=current.deployment_id,
        due=False,
        reason=None,
        detail=f"live traffic {since_live:.0f}s ago is already answering the question",
    )


def next_probes(
    healths: Iterable[ProviderHealth],
    now: datetime,
    *,
    interval_seconds: float = PROBE_INTERVAL_SECONDS,
    stale_after_seconds: float = LIVE_EVIDENCE_STALE_SECONDS,
    limit: int | None = None,
) -> tuple[ProbeVerdict, ...]:
    """What to probe next, most urgent first. The whole of the background prober.

    A function, not a loop and not a thread. Something else owns the schedule and calls this
    every `PROBE_INTERVAL_SECONDS`; that something can be a worker tick, a cron entry, or a
    test advancing a datetime by hand, and the third one is why it is shaped this way.

    Half-open deployments sort first: probing one restores capacity, while probing an idle
    closed one only updates a record. Within a group the least recently probed goes first,
    with a never-probed deployment ahead of all of them, and the deployment id breaks ties
    so the order is stable across runs rather than showing up as churn in the console.

    `limit` bounds one tick's work. Without it an estate-wide outage makes every deployment
    due at once and the tick fans out to all of them together, which is a retry storm aimed
    at a provider that is already struggling.
    """
    # Materialised because it is read twice, and an `Iterable` that is a generator would
    # otherwise be silently empty the second time and produce an unsorted result.
    records = tuple(healths)
    verdicts = [
        probe_verdict(
            health,
            now,
            interval_seconds=interval_seconds,
            stale_after_seconds=stale_after_seconds,
        )
        for health in records
    ]
    ages = {h.deployment_id: _seconds_since(h.last_probe_at, now) for h in records}

    def order(verdict: ProbeVerdict) -> tuple[int, float, str]:
        urgency = 0 if verdict.reason is ProbeReason.HALF_OPEN_ADMISSION else 1
        age = ages.get(verdict.deployment_id)
        # Never probed sorts ahead of everything else in its group: it is the case with no
        # evidence at all, and the one a scheduler must not starve.
        staleness = float("inf") if age is None else age
        return (urgency, -staleness, verdict.deployment_id)

    due = sorted((v for v in verdicts if v.due), key=order)
    return tuple(due if limit is None else due[:limit])


# ---------------------------------------------------------------- depth alerting (M5.4.8)
#: The depth at which a chain is worth a warning. Two, because depth two means the primary
#: failed: the request was answered, so nobody complains, and one rung is now carrying
#: traffic that was meant to have a fallback behind it.
CHAIN_DEPTH_WARNING = 2

#: The depth at which it stops being a warning. Three means two rungs failed inside one
#: request; on the two-rung seed chain that is the whole chain.
CHAIN_DEPTH_CRITICAL = 3


class AlertLevel(enum.StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ChainAttempt:
    """One rung actually tried, and what came back.

    `position` is the rung's position in the full tier chain, not its index in the list of
    attempts. Those two differ exactly when a rung was skipped, and that is the case depth
    alerting exists for: a chain whose primary is circuit-open and whose second rung answers
    has made one attempt and has reached depth two.
    """

    deployment_id: str
    position: int
    succeeded: bool
    trigger: FallbackTrigger | None = None

    def __post_init__(self) -> None:
        if self.succeeded and self.trigger is not None:
            msg = (
                f"attempt on {self.deployment_id!r} succeeded but carries trigger "
                f"{self.trigger}; a trigger is why the chain moved on, so recording one on a "
                f"success makes the executed chain unreconstructable from the rows"
            )
            raise ValueError(msg)
        if self.position < 0:
            msg = f"attempt on {self.deployment_id!r} has a negative position"
            raise ValueError(msg)


@dataclass(frozen=True)
class ChainOutcome:
    """What one request's chain actually did, in the shape the alerting rule needs.

    Carries the skipped rungs as well as the attempts, because a rung the breaker removed
    before the request started is a rung the request still had to get past.
    """

    tier: Tier
    attempts: tuple[ChainAttempt, ...] = ()
    skipped: tuple[SkippedRung, ...] = ()

    @property
    def served(self) -> ChainAttempt | None:
        for attempt in self.attempts:
            if attempt.succeeded:
                return attempt
        return None

    @property
    def exhausted(self) -> bool:
        """The chain ran out without an answer. Not the alerting trigger; see
        `DEPTH_NOT_FINAL_FAILURE`. It raises the level of an alert that depth already
        justified, and it is not the only way to get one."""
        return self.served is None

    @property
    def open_rungs(self) -> tuple[SkippedRung, ...]:
        """Rungs the breaker fenced off. A residency or disabled skip is deliberately not
        counted: residency is a policy fact that holds for every request from that scope, so
        alerting on it would fire on every regulated question, and a disabled rung is an
        operator's own decision rather than a provider's health."""
        return tuple(s for s in self.skipped if s.reason is SkipReason.CIRCUIT_OPEN)

    @property
    def depth(self) -> int:
        """The one-based position of the deepest rung this request had to reach.

        Counts attempts, plus circuit-open skips above the rung that served. A dead rung
        *below* the answering one is a real problem and is reported separately by
        `open_rungs`; folding it into depth would say the request went deeper than it did,
        and depth is the number the alert thresholds are calibrated against.
        """
        served = self.served
        positions = [a.position for a in self.attempts]
        positions += [
            s.rung.position
            for s in self.open_rungs
            if served is None or s.rung.position < served.position
        ]
        return max(positions) + 1 if positions else 0


@dataclass(frozen=True)
class DepthAlert:
    """One alert, with its argument already written.

    `reason` is a sentence rather than a code because this lands in front of whoever is on
    call, and "chain_depth_exceeded tier=main depth=3" makes them go and read the source to
    find out whether it matters.
    """

    level: AlertLevel
    tier: Tier
    depth: int
    served_by: str | None
    reason: str


def assess_chain_depth(
    outcome: ChainOutcome,
    *,
    warn_at: int = CHAIN_DEPTH_WARNING,
    critical_at: int = CHAIN_DEPTH_CRITICAL,
) -> DepthAlert | None:
    """Alert on how deep the chain went, whether or not it eventually answered.

    The ordering of the branches is the whole rule. Depth is evaluated for a chain that
    succeeded, before anything asks whether it failed, because a successful chain at depth
    three is the signal that arrives in time to act on. Exhaustion only raises the level of
    an alert; it is never the thing that decides there is one.

    Suppression and de-duplication are deliberately absent. They belong to whatever delivers
    alerts, because a window inside this function would make it return None for a genuine
    event on account of an earlier one, and every test of the rule would then be a test of
    the suppressor.
    """
    if not outcome.attempts and not outcome.open_rungs:
        # Nothing was tried and nothing was fenced off. That is a residency refusal or an
        # unconfigured tier, which routing already reports as `NoCompliantRoute`; raising a
        # provider-health alert for it would send an operator to the wrong dashboard.
        return None

    depth = outcome.depth
    served = outcome.served
    served_by = None if served is None else served.deployment_id

    if depth >= critical_at:
        tail = (
            f" and was served by {served_by}; every rung above it is failing"
            if served_by is not None
            else " and ran out of rungs without an answer"
        )
        return DepthAlert(
            level=AlertLevel.CRITICAL,
            tier=outcome.tier,
            depth=depth,
            served_by=served_by,
            reason=f"tier {outcome.tier} reached rung {depth}{tail}",
        )

    if outcome.exhausted:
        return DepthAlert(
            level=AlertLevel.CRITICAL,
            tier=outcome.tier,
            depth=depth,
            served_by=None,
            reason=f"tier {outcome.tier} exhausted its chain at depth {depth} without an answer",
        )

    if depth >= warn_at:
        return DepthAlert(
            level=AlertLevel.WARNING,
            tier=outcome.tier,
            depth=depth,
            served_by=served_by,
            reason=(
                f"tier {outcome.tier} answered on rung {depth} rather than its primary; the "
                f"answer arrived, so nothing else will report this"
            ),
        )

    if outcome.open_rungs:
        # Served on the primary, but a rung underneath is fenced off. Nothing about this
        # request was slow or wrong, and the chain is one failure from having nowhere left.
        dead = ", ".join(s.rung.deployment.id for s in outcome.open_rungs)
        return DepthAlert(
            level=AlertLevel.WARNING,
            tier=outcome.tier,
            depth=depth,
            served_by=served_by,
            reason=(
                f"tier {outcome.tier} answered on its primary, but {dead} is circuit-open, so "
                f"the chain has no fallback left"
            ),
        )

    return None
