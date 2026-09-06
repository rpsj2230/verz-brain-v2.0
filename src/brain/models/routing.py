"""Which model handles a request, in what order, and when we refuse rather than degrade.

Four decisions live here. Each one exists to prevent a specific failure that is silent,
which is to say a failure whose first symptom is a wrong answer somebody acted on.

**Tier classification is deterministic and makes no model call.** A model-in-the-loop
router adds a round trip to every request, and worse, it lets text inside a retrieved
document influence which jurisdiction processes the question that retrieved it. What is
here instead is a pure function of five scalars (lane, an explicit tier pin, tool count,
estimated context, and whether the scope carries a residency constraint), so the same
request always lands in the same tier, the decision replays from a trace, and it can be
argued with by reading it.

**Fallback fires on a closed set of transport facts, never on the reply.** "The answer
looked weak" is not measurable at request time, so a quality trigger has no falsifiable
off condition: it retries until something changes, roughly doubles the cost of every
affected request, and buries the real failure under an answer that eventually arrives.
The set here is closed by construction; there is no parameter through which a judgement
about content can enter, and `may_fall_back` returns False for every string that is not a
member of the enum.

**Fallback stays inside a tier. The only legal cross-tier move is upward, on context
overflow.** Answering a hard question with a smaller model produces a confident wrong
answer that nobody knows is degraded, which in a system whose output drives autonomous
actions is strictly worse than an honest failure.

**Residency skips rather than degrades.** A rung whose provider region does not satisfy
the scope's constraint is removed from the chain and recorded as skipped, with its reason.
If that empties the chain, the request is refused. There is deliberately no path from a
residency-constrained scope to a non-compliant provider: a fallback that quietly crosses a
border turns a contractual promise into a breach that surfaces months later in an audit,
with the evidence sitting in the provider's logs rather than ours.

The circuit breaker is a pure state machine and takes `now` as a parameter. A breaker that
reads the clock itself cannot be tested for the half-open transition, and the half-open
transition is the part that actually goes wrong.

Nothing in this module performs I/O, imports a provider SDK, or touches the database. It
is the policy; `brain.models` will grow the driver beside it.

Task ids: M5.2.1, M5.2.3, M5.3.1, M5.3.2, M5.4.1, M5.4.2, M5.4.4, M5.4.5, M5.4.6
Task ids: M5.5.1, M5.5.2, M5.5.3, M5.5.4
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType

from brain.core.errors import Degraded
from brain.core.lane import Lane


# ------------------------------------------------------------------------ lanes and tiers
class Tier(enum.StrEnum):
    """The model pools, cheapest first.

    The architecture note calls these simple / medium / complex and the routing design
    note calls them small / main / heavy. They are the same three pools; the second set of
    names is used here because it is the one the `model_pool` table is specified with, and
    two vocabularies for one column is how a rename becomes a data migration.
    """

    #: The fast lane's tier. A value rather than `None` so a caller cannot forget to
    #: handle it: `RoutingChain.select` returns an empty selection for it, and asking that
    #: selection for rungs refuses loudly.
    NONE = "none"
    SMALL = "small"
    MAIN = "main"
    HEAVY = "heavy"


#: Cheapest to most capable. `Tier.NONE` is deliberately absent: it is not a rung on this
#: ladder, it is the absence of the ladder.
TIER_LADDER: tuple[Tier, ...] = (Tier.SMALL, Tier.MAIN, Tier.HEAVY)

#: What an unpinned, untagged, non-task request gets. This is a default, not a classifier.
#: Measured against this workload the entire routing lever is worth roughly a fifth of
#: what prompt caching is worth, so a learned router here would be maintaining a
#: depreciating model artefact to chase the smaller of two levers.
DEFAULT_TIER = Tier.MAIN

#: Context each tier can be trusted with, in tokens. These are seeds for the `model_pool`
#: rows; the console owns them at runtime.
#:
#: A tier's window must be the NARROWEST of its rungs, never the widest. Size a request
#: against the primary's window and the fallback rung cannot hold it, so the chain fails
#: precisely when it is needed. `RoutingChain.narrowest_window` exists to check this.
TIER_CONTEXT_WINDOW: Mapping[Tier, int] = MappingProxyType(
    {
        Tier.NONE: 0,
        Tier.SMALL: 128_000,
        Tier.MAIN: 200_000,
        Tier.HEAVY: 200_000,
    }
)

#: Escalate upward once the estimated input passes this fraction of the window. Not 1.0,
#: because the output tokens and every tool-result turn land in the same window: a request
#: measured at 100% of the window at dispatch has already overflowed by the second turn.
#: 0.8 leaves room for roughly one full tool round trip.
ESCALATION_HEADROOM = 0.8


# ----------------------------------------------------------------------------- residency
class ResidencyClass(enum.StrEnum):
    """What a deployment promises about where the request is processed."""

    #: The provider routes to whichever region has capacity. No location is promised.
    GLOBAL = "global"
    #: In-region invocation, at the region named on the deployment.
    REGION_PINNED = "region_pinned"
    #: The client's own hardware.
    ON_PREM = "on_prem"


#: Where each region's data actually comes to rest, in the words a contract can use.
#: M5.5.3 asks for this registry precisely so the residency claim being sold has a written
#: basis rather than a provider marketing page someone remembers.
#:
#: The `global` entry is the one that matters. A deployment that may land anywhere
#: satisfies no residency claim even on a day when it happens to run in the right place,
#: which is why `ResidencyRequirement.satisfied_by` rejects it outright.
REGION_STORAGE: Mapping[str, str] = MappingProxyType(
    {
        "global": "wherever the provider has capacity; no location is promised",
        "us-east-1": "United States, Northern Virginia",
        "eu-west-1": "Ireland",
        "ap-southeast-1": "Singapore",
        "on-prem-sg": "the client's own hardware, Singapore",
    }
)


@dataclass(frozen=True)
class ResidencyRequirement:
    """What a scope demands of the region that processes it.

    This is not a field on `brain.core.scope.Scope` even though M5.5.1 phrases it as
    attached to the scope. Scope is a row predicate whose entire design rests on composing
    by conjunction only; hanging a non-predicate field off it would mean `Scope.intersect`
    no longer describes what the type does. The constraint is carried alongside the scope
    instead and composes by its own `intersect`, with the same narrowing guarantee.
    """

    #: None means unconstrained. An EMPTY frozenset is a different thing entirely: it
    #: means two constraints intersected to nothing, so no region satisfies this and every
    #: rung must be skipped. Collapsing those two into one value is the bug this
    #: representation exists to prevent, because it silently converts "these two policies
    #: are incompatible" into "route anywhere".
    allowed_regions: frozenset[str] | None = None
    #: The strictest form: only an on-prem deployment satisfies it.
    on_prem_only: bool = False

    @property
    def is_constrained(self) -> bool:
        return self.allowed_regions is not None or self.on_prem_only

    def satisfied_by(self, deployment: Deployment) -> bool:
        if self.on_prem_only and deployment.residency_class is not ResidencyClass.ON_PREM:
            return False
        if self.allowed_regions is None:
            return True
        if deployment.residency_class is ResidencyClass.GLOBAL:
            # A global deployment makes no promise about where the request lands, so it
            # can never satisfy a region pin. Checking its `region` column instead would
            # pass today and breach the contract on the first capacity event.
            return False
        return deployment.region in self.allowed_regions

    def intersect(self, other: ResidencyRequirement) -> ResidencyRequirement:
        """Conjunction. Composing two constraints can only ever narrow.

        The None-is-unconstrained asymmetry is the whole reason this is a method rather
        than a set intersection at the call site: `None & {"eu-west-1"}` has to be
        `{"eu-west-1"}`, and writing that inline is how somebody eventually writes
        `frozenset() & {"eu-west-1"}` and widens a constraint to nothing.
        """
        if self.allowed_regions is None:
            regions = other.allowed_regions
        elif other.allowed_regions is None:
            regions = self.allowed_regions
        else:
            regions = self.allowed_regions & other.allowed_regions
        return ResidencyRequirement(
            allowed_regions=regions,
            on_prem_only=self.on_prem_only or other.on_prem_only,
        )


#: The identity of `ResidencyRequirement.intersect` and the default for an unconstrained
#: scope. A module constant rather than a default_factory because the type is frozen.
UNCONSTRAINED = ResidencyRequirement()


def storage_location(deployment: Deployment) -> str:
    """Where this deployment's traffic comes to rest, for the console and the contract.

    An undocumented region is reported loudly rather than defaulted, because the failure
    mode being prevented is a deployment added in a hurry whose residency claim nobody
    can substantiate when a client asks.
    """
    return REGION_STORAGE.get(deployment.region, f"UNDOCUMENTED region {deployment.region!r}")


# ------------------------------------------------------------------- tier classification
@dataclass(frozen=True)
class RoutingRequest:
    """Everything tier classification is allowed to look at.

    Deliberately five scalars and nothing else. There is no question text here, and that
    is the point: routing on the question's surface shape (length, keywords, whether it
    ends in a question mark) reads as a cheap heuristic and behaves as a random tier
    assignment, because the short questions are frequently the hardest ones.
    """

    lane: Lane
    #: Tools in scope for this request, not tools the caller may use. The catalogue is
    #: projected per request elsewhere; here it is only a size.
    tool_count: int = 0
    estimated_context_tokens: int = 0
    #: A Skill author's tier tag, or an operator pinning a tier by hand. The author wrote
    #: the procedure and knows what it needs, so this beats the rules below it.
    requested_tier: Tier | None = None
    residency: ResidencyRequirement = UNCONSTRAINED


@dataclass(frozen=True)
class TierDecision:
    """The tier, plus the argument for it.

    `reason` is carried for the same purpose as `ProcessProfile.reason` in
    `brain.runtime`: a number or a choice with no argument attached gets changed by
    whoever is next annoyed by it.
    """

    tier: Tier
    reason: str
    residency: ResidencyRequirement
    #: True when even the chosen tier cannot hold the estimate. There is no higher tier to
    #: escalate into, so this is a signal to the caller to trim retrieval, not to re-route.
    #: Routing deliberately does not silently truncate: a quietly shortened prompt is a
    #: wrong answer with no error anywhere.
    context_overflows: bool = False


def _next_tier_up(tier: Tier) -> Tier | None:
    if tier not in TIER_LADDER:
        return None
    index = TIER_LADDER.index(tier)
    return TIER_LADDER[index + 1] if index + 1 < len(TIER_LADDER) else None


def classify_tier(
    request: RoutingRequest,
    *,
    windows: Mapping[Tier, int] = TIER_CONTEXT_WINDOW,
) -> TierDecision:
    """Pick a tier. Pure, total, and the same every time for the same inputs.

    Precedence, in order, each step recorded in the decision's reason:

    1. the fast lane takes no model, and nothing overrides that;
    2. an explicit pin, else the task lane's heaviest tier, else the default;
    3. a tool loop never runs below MAIN;
    4. a residency-constrained scope never runs on SMALL;
    5. escalate upward while the estimate exceeds the tier's headroom.

    `windows` is injectable so a caller holding the real chain can pass the measured
    windows from `RoutingChain.narrowest_window` instead of the seed constants.
    """
    if request.lane is Lane.FAST:
        # A pin arriving on the fast lane is a contradiction, and the fast lane wins. Its
        # whole guarantee is that no model saw the question, and everything downstream of
        # it (an empty tool catalogue, reads restricted to projected tables) was built on
        # that assumption. Honouring the pin would put a model on a path with none of the
        # guards a model path has.
        pinned = (
            ""
            if request.requested_tier is None
            else f"; the {request.requested_tier} pin was ignored"
        )
        return TierDecision(
            tier=Tier.NONE,
            reason=f"fast lane, so no model at all{pinned}",
            residency=request.residency,
        )

    if request.requested_tier is Tier.NONE:
        msg = (
            "cannot pin tier 'none' outside the fast lane: it means answering with no "
            "model, and fast-lane admission is decided by exact intent match, not by a pin"
        )
        raise ValueError(msg)

    steps: list[str] = []
    # Annotated rather than inferred: the `is Tier.NONE` check above narrows the pin to
    # the three ladder members, and without this the escalation below would be rejected as
    # widening a literal type back to Tier.
    tier: Tier
    if request.requested_tier is not None:
        tier = request.requested_tier
        steps.append(f"pinned to {tier} by the caller")
    elif request.lane is Lane.TASK:
        tier = Tier.HEAVY
        steps.append("task lane, so the heaviest tier")
    else:
        tier = DEFAULT_TIER
        steps.append(f"lane={request.lane} with no pin, so the {DEFAULT_TIER} default")

    if tier is Tier.SMALL and request.tool_count > 0:
        # A tool loop re-sends the whole transcript every turn. A small model that misuses
        # a tool costs more in wasted turns than the per-token saving it was chosen for.
        tier = Tier.MAIN
        steps.append(f"{request.tool_count} tool(s) in scope, so not below {Tier.MAIN}")

    if tier is Tier.SMALL and request.residency.is_constrained:
        # The residency-compliant menu is smaller and older than the global one, so the
        # compliant SMALL rung (where one exists at all) is the weakest model in the
        # estate handling the most regulated data. That is never worth the saving.
        #
        # Considered and rejected: forcing HEAVY instead, on the theory that regulated
        # work deserves the best model. It converts a servable request into a refusal
        # whenever the constraint has no compliant HEAVY rung, which is worse than serving
        # it compliantly on MAIN.
        #
        # This overrides an explicit pin on purpose. The constraint attaches to the scope,
        # and the caller does not own the scope's obligations.
        tier = Tier.MAIN
        steps.append(f"residency-constrained scope, so not on {Tier.SMALL}")

    while True:
        window = windows.get(tier, 0)
        if request.estimated_context_tokens <= window * ESCALATION_HEADROOM:
            break
        higher = _next_tier_up(tier)
        if higher is None:
            break
        steps.append(
            f"{request.estimated_context_tokens:,} tokens is over "
            f"{ESCALATION_HEADROOM:.0%} of {tier}'s {window:,}, so up to {higher}"
        )
        tier = higher

    overflows = request.estimated_context_tokens > windows.get(tier, 0) * ESCALATION_HEADROOM
    if overflows:
        steps.append("still over the headroom at the top tier; trim the prompt, do not re-route")

    return TierDecision(
        tier=tier,
        reason="; ".join(steps),
        residency=request.residency,
        context_overflows=overflows,
    )


# ------------------------------------------------------------------------- fallback rules
class FallbackTrigger(enum.StrEnum):
    """The complete set of conditions that may move the chain on. Closed by construction.

    Every member is an observable fact about the transport or the provider. None of them
    is a judgement about the content of a reply, and that is the property the closure
    protects. See `QUALITY_FALLBACK_REJECTED`.
    """

    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    #: HTTP 429.
    RATE_LIMITED = "rate_limited"
    #: HTTP 5xx.
    PROVIDER_ERROR = "provider_error"
    #: The rung's breaker is open, so the attempt is not made at all.
    CIRCUIT_OPEN = "circuit_open"
    #: The one trigger that may change tier, and only upward.
    CONTEXT_EXCEEDED = "context_exceeded"


FALLBACK_TRIGGER_VALUES: frozenset[str] = frozenset(t.value for t in FallbackTrigger)

#: The only trigger permitted to leave the tier, and only in the upward direction. Every
#: other trigger keeps the request inside its tier and moves it to the next rung.
TIER_ESCALATING_TRIGGERS: frozenset[FallbackTrigger] = frozenset({FallbackTrigger.CONTEXT_EXCEEDED})

#: Why there is no QUALITY member here, and why adding one would be a regression.
#:
#: "The answer was poor" is not measurable at request time. Whatever proxy gets used
#: (length, a refusal string, a confidence score the model made up, a thumbs-down that
#: arrives minutes later) has no falsifiable off condition, so the retry loop it drives
#: terminates on luck rather than on a fact. Three consequences, in increasing order of
#: harm: it roughly doubles the cost of every affected request; it hides the real failure
#: behind an answer that eventually arrives, so the thing actually wrong stays unfixed;
#: and it makes the trace dishonest, because the chain depth no longer means "the
#: providers were struggling", it means "something disliked the reply".
#:
#: A weak answer has somewhere to go, and it is not the next rung: abstention (M8), which
#: says what was searched and what was not found, and the answer-feedback signal, which
#: is measured after the fact where measurement is actually possible.
QUALITY_FALLBACK_REJECTED = (
    "Quality is not a fallback trigger. It is not measurable at request time, so a "
    "quality trigger is an unfalsifiable retry loop that doubles cost and hides the real "
    "failure. Weak answers go to abstention and to the feedback signal, not to the next "
    "rung."
)

#: Deliberately not a trigger, despite appearing in the architecture note's list.
#:
#: A content-policy refusal is a deterministic property of the request, not of the
#: provider's health: the next rung receives the same prompt and refuses the same way, so
#: the chain spends its entire fallback budget to arrive at an identical answer. And in
#: the case where a cross-provider rung does answer, what the chain has done is shop for a
#: provider willing to say yes, which is the quality trigger wearing a different hat.
CONTENT_POLICY_REFUSAL_IS_NOT_A_TRIGGER = (
    "A content-policy refusal is a property of the request, not of the provider. Retrying "
    "it on another rung either reproduces it at full cost or shops for a yes. It belongs "
    "on the abstention path."
)


def may_fall_back(reason: str) -> bool:
    """True only for a member of the closed set.

    Takes a string on purpose. Callers arrive with reasons from all sorts of places (a log
    line, an operator, an exception message), and the answer for every one of them that is
    not a member of `FallbackTrigger` is no.
    """
    return reason in FALLBACK_TRIGGER_VALUES


def trigger_for(
    *,
    status: int | None = None,
    timed_out: bool = False,
    connection_failed: bool = False,
    context_exceeded: bool = False,
    circuit_open: bool = False,
) -> FallbackTrigger | None:
    """Classify one failed attempt, or return None when the chain must stop.

    Note what is not a parameter: there is nowhere to put the reply, its length, its
    score, or anyone's opinion of it. The closure of the trigger set is enforced by this
    signature rather than by a rule somebody has to remember.
    """
    if circuit_open:
        return FallbackTrigger.CIRCUIT_OPEN
    if connection_failed:
        return FallbackTrigger.CONNECTION_ERROR
    if timed_out:
        return FallbackTrigger.TIMEOUT
    if context_exceeded:
        return FallbackTrigger.CONTEXT_EXCEEDED
    if status == 429:
        return FallbackTrigger.RATE_LIMITED
    if status is not None and 500 <= status < 600:
        return FallbackTrigger.PROVIDER_ERROR
    # Everything else stops the chain, including every 4xx that is not a 429. A 400 is our
    # request being wrong, and the next rung gets the same request: retrying spends money
    # to receive the same error twice.
    return None


def permits_tier_escalation(trigger: FallbackTrigger) -> bool:
    """Whether this trigger may move the request to a higher tier.

    Only context overflow. Every other trigger is same-tier, and no trigger of any kind
    may move downward: answering a hard question with a smaller model produces a confident
    wrong answer that nobody knows is degraded.
    """
    return trigger in TIER_ESCALATING_TRIGGERS


# ------------------------------------------------------------------------ circuit breaker
#: Consecutive live failures that open a breaker.
BREAKER_CONSECUTIVE_FAILURES = 3

#: How many recent live outcomes the ring keeps.
BREAKER_LIVE_WINDOW = 20

#: The ratio rule needs at least this many samples. Below it, one failure in two is noise
#: and would flap a healthy deployment out of rotation.
BREAKER_RATIO_MIN_SAMPLES = 8

#: Open when more than half the window failed, even without three in a row. The threshold
#: is a ratio over a window rather than failures per minute on purpose: at roughly 0.1
#: requests per second a per-minute threshold is nearly unreachable, so a dead provider
#: stays in rotation for an hour while the counter never fills.
BREAKER_FAIL_RATIO = 0.5

BREAKER_BASE_COOLDOWN_SECONDS = 30.0

#: Ceiling on the cooldown, jitter included. Ten minutes: long enough that a genuinely
#: dead provider stops costing anything, short enough that recovery is noticed within one
#: working coffee break rather than requiring a restart.
BREAKER_MAX_COOLDOWN_SECONDS = 600.0

#: A half-open probe that never reports back would wedge the breaker in half-open forever,
#: admitting nothing. Two minutes is above the longest rung timeout in the seed chain
#: (90 seconds on HEAVY), so reclaiming cannot race a probe that is merely slow.
BREAKER_PROBE_CLAIM_TTL_SECONDS = 120.0


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreaker:
    """One deployment's health, as a pure state machine.

    Frozen, and every transition returns a new instance. `now` is always a parameter and
    the machine never reads a clock (there is a test asserting that): a breaker that calls
    `datetime.now()` internally cannot be tested for the half-open transition, and the
    half-open transition is the part that goes wrong in production.

    Jitter is supplied by the caller rather than generated here for the same reason.
    Production passes `random.random()`; tests pass a constant. Jitter's job is to
    decorrelate callers, so deriving it from something stable (the deployment id, say)
    would look like jitter and do nothing, since every worker would compute the same value
    for the same deployment.
    """

    deployment_id: str
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    #: Recent live outcomes, oldest first, capped at BREAKER_LIVE_WINDOW. Probe outcomes
    #: are deliberately excluded: a probe is one synthetic request, and letting it move
    #: the ratio would let the prober drive its own verdict.
    live: tuple[bool, ...] = ()
    opened_at: datetime | None = None
    #: Opens without an intervening close. Drives the exponential backoff.
    open_streak: int = 0
    #: The jitter fraction supplied at the last open, kept so `cooldown_until` is stable
    #: across reads instead of moving every time it is asked.
    jitter: float = 0.0
    probe_claimed_at: datetime | None = None

    # ------------------------------------------------------------------ derived state
    @property
    def cooldown_seconds(self) -> float:
        if self.open_streak <= 0:
            return 0.0
        raw = BREAKER_BASE_COOLDOWN_SECONDS * 2.0 ** (self.open_streak - 1)
        return min(BREAKER_MAX_COOLDOWN_SECONDS, raw * (1.0 + self.jitter))

    @property
    def cooldown_until(self) -> datetime | None:
        if self.opened_at is None:
            return None
        return self.opened_at + timedelta(seconds=self.cooldown_seconds)

    def fail_ratio(self) -> float:
        if not self.live:
            return 0.0
        return sum(1 for ok in self.live if not ok) / len(self.live)

    # ------------------------------------------------------------------- transitions
    def advance(self, now: datetime) -> CircuitBreaker:
        """Apply whatever the passage of time alone implies. Never admits anything."""
        if self.state is BreakerState.OPEN:
            until = self.cooldown_until
            if until is not None and now >= until:
                return replace(self, state=BreakerState.HALF_OPEN, probe_claimed_at=None)
            return self
        if self.state is BreakerState.HALF_OPEN and self.probe_claimed_at is not None:
            age = (now - self.probe_claimed_at).total_seconds()
            if age >= BREAKER_PROBE_CLAIM_TTL_SECONDS:
                # The claimant never came back. Without this the breaker wedges half-open
                # and the deployment is out of rotation permanently with no error anywhere.
                return replace(self, probe_claimed_at=None)
        return self

    def admits(self, now: datetime) -> bool:
        """Would this breaker let a request through, without claiming anything.

        Chain selection asks this. Only the executor calls `try_admit`, which claims. If
        selection claimed, planning a chain would burn the half-open probe of every rung
        it inspected and never attempted, and a breaker downstream of a skipped rung would
        never get its one chance to recover.
        """
        current = self.advance(now)
        if current.state is BreakerState.CLOSED:
            return True
        if current.state is BreakerState.OPEN:
            return False
        return current.probe_claimed_at is None

    def try_admit(self, now: datetime) -> tuple[CircuitBreaker, bool]:
        """Claim admission for one attempt. The claim half of claim-and-return.

        In half-open exactly one caller is admitted; the claim is released by
        `record_success` or `record_failure`, or reclaimed after
        `BREAKER_PROBE_CLAIM_TTL_SECONDS` if neither arrives.
        """
        current = self.advance(now)
        if current.state is BreakerState.CLOSED:
            return current, True
        if current.state is BreakerState.OPEN:
            return current, False
        if current.probe_claimed_at is None:
            return replace(current, probe_claimed_at=now), True
        return current, False

    def record_success(self, now: datetime) -> CircuitBreaker:
        if self.state is BreakerState.HALF_OPEN:
            # The probe came back clean. Reset completely, including the open streak, so
            # the next incident starts its backoff from the base rather than from
            # wherever the last one finished.
            return CircuitBreaker(deployment_id=self.deployment_id)
        if self.state is BreakerState.OPEN:
            # A success while open means a caller ran an attempt without asking. Keep the
            # breaker open rather than let a stray result reopen the gate: the alternative
            # is that one retry loop somewhere quietly cancels the breaker for everybody.
            return self
        return replace(self, live=_push(self.live, True), consecutive_failures=0)

    def record_failure(self, now: datetime, *, jitter: float = 0.0) -> CircuitBreaker:
        if self.state is BreakerState.HALF_OPEN:
            # The probe failed. Straight back to open with a longer cooldown, and the
            # outcome stays out of the live ring for the reason given on that field.
            return self._open(now, jitter=jitter)
        if self.state is BreakerState.OPEN:
            return self
        live = _push(self.live, False)
        consecutive = self.consecutive_failures + 1
        ratio_tripped = (
            len(live) >= BREAKER_RATIO_MIN_SAMPLES
            and (sum(1 for ok in live if not ok) / len(live)) > BREAKER_FAIL_RATIO
        )
        if consecutive >= BREAKER_CONSECUTIVE_FAILURES or ratio_tripped:
            return replace(self, live=live, consecutive_failures=consecutive)._open(
                now, jitter=jitter
            )
        return replace(self, live=live, consecutive_failures=consecutive)

    def open(self, now: datetime, *, jitter: float = 0.0) -> CircuitBreaker:
        """Open the breaker without recording a live failure.

        Exists for the prober. A probe is one synthetic request, and letting it push an
        outcome into the live ring would let the prober drive its own verdict, so the
        health layer needs a way to say "open, on evidence that is not live traffic".
        Before this existed it reached for `_open` directly, which is a coupling that
        survives exactly until somebody renames a private method.

        Deliberately not a general escape hatch: it takes the same arguments as the
        internal transition and applies the same streak, jitter floor and claim reset, so
        there is one implementation of opening rather than two that drift.
        """
        return self._open(now, jitter=jitter)

    def _open(self, now: datetime, *, jitter: float) -> CircuitBreaker:
        return replace(
            self,
            state=BreakerState.OPEN,
            opened_at=now,
            open_streak=self.open_streak + 1,
            # Never negative. A cooldown shorter than the base defeats the point of having
            # one, so jitter only ever lengthens.
            jitter=max(0.0, jitter),
            consecutive_failures=0,
            probe_claimed_at=None,
        )


def _push(ring: tuple[bool, ...], outcome: bool) -> tuple[bool, ...]:
    return (*ring, outcome)[-BREAKER_LIVE_WINDOW:]


# ------------------------------------------------------------------------- the matrix
@dataclass(frozen=True)
class Deployment:
    """One reachable endpoint: a provider, a model, and a place it runs."""

    id: str
    provider: str
    model: str
    region: str
    residency_class: ResidencyClass
    context_window: int
    enabled: bool = True


class RungRole(enum.StrEnum):
    """What a rung is for. Derived from position and provider, never typed by hand.

    In Postgres this is a trigger-maintained column (M5.3.2). Here it is a method on the
    chain, for the same reason: a label a human types drifts from the position and
    provider it is supposed to describe, and then the console shows a "primary" sitting
    third in the chain.
    """

    PRIMARY = "primary"
    SAME_PROVIDER_FAILOVER = "same_provider_failover"
    CROSS_PROVIDER_FAILOVER = "cross_provider_failover"


@dataclass(frozen=True)
class RoutingRung:
    """One position in one tier's chain."""

    tier: Tier
    position: int
    #: Denormalised from the deployment on purpose: this is the field the console edits,
    #: the field an attempt row records, and half of the price-book key. The check below
    #: is what keeps it from drifting, because a rung naming a model its deployment does
    #: not serve would meter the request against the wrong price.
    model: str
    deployment: Deployment
    #: Tries against this rung before moving on. One is the right answer wherever a person
    #: is waiting; see the seed chain.
    attempts: int
    timeout_seconds: float
    #: A ceiling per rung, so a slow provider becomes queueing rather than unbounded
    #: memory. The first symptom of no ceiling is memory, not latency.
    max_concurrency: int

    def __post_init__(self) -> None:
        if self.model != self.deployment.model:
            msg = (
                f"rung {self.tier}/{self.position} names model {self.model!r} but its "
                f"deployment {self.deployment.id!r} serves {self.deployment.model!r}"
            )
            raise ValueError(msg)
        if self.attempts < 1:
            # The way to remove a rung is to remove it. A rung with zero attempts is a
            # rung that silently never runs, and it reads in the console as configured.
            msg = f"rung {self.tier}/{self.position} has {self.attempts} attempts; minimum is 1"
            raise ValueError(msg)
        if self.timeout_seconds <= 0:
            msg = f"rung {self.tier}/{self.position} has a non-positive timeout"
            raise ValueError(msg)
        if self.max_concurrency < 1:
            msg = f"rung {self.tier}/{self.position} has a max_concurrency below 1"
            raise ValueError(msg)

    @property
    def worst_case_seconds(self) -> float:
        return self.attempts * self.timeout_seconds


class SkipReason(enum.StrEnum):
    """Why a rung was left out of the chain.

    Recorded per rung rather than summarised, because a residency skip and an outage skip
    look identical in a chain that just came back short, and they send an operator to
    completely different places.
    """

    DISABLED = "disabled"
    RESIDENCY = "residency"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True)
class SkippedRung:
    rung: RoutingRung
    reason: SkipReason


class NoCompliantRoute(Degraded):
    """No rung in the tier may serve this request.

    A subclass of Degraded because that is the taxonomy entry that already promises we say
    so rather than substituting something. There is deliberately no variant of this that
    returns a rung anyway.
    """

    public_message = "I could not run that on a model your data policy allows."


@dataclass(frozen=True)
class ChainSelection:
    """The rungs that survived, and every rung that did not, with its reason."""

    tier: Tier
    rungs: tuple[RoutingRung, ...]
    skipped: tuple[SkippedRung, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.rungs

    def skipped_for(self, reason: SkipReason) -> tuple[SkippedRung, ...]:
        return tuple(s for s in self.skipped if s.reason is reason)

    def require(self) -> tuple[RoutingRung, ...]:
        """The rungs, or a refusal. Never a non-compliant rung.

        This is the whole residency guarantee in one method: when the chain empties there
        is no branch that reaches for a skipped rung, because the skipped rungs are in a
        different field and nothing here reads it except to explain the refusal.
        """
        if self.rungs:
            return self.rungs
        reasons = {s.reason for s in self.skipped}
        if SkipReason.RESIDENCY in reasons:
            # Residency outranks an outage in the message even when both skipped rungs.
            # A residency refusal is a policy outcome the asker can act on (raise it with
            # an admin, agree a transfer clause); an outage is transient and reporting one
            # as the other sends them to wait for a recovery that will not help.
            public = (
                "I cannot answer that without sending the data outside the region its "
                "policy allows, so I have not."
            )
        elif reasons:
            public = Degraded.public_message
        elif self.tier is Tier.NONE:
            public = "That request was admitted to the fast lane, which uses no model."
        else:
            public = "No model is configured to handle that."
        detail = f"tier={self.tier} has no usable rung: " + (
            ", ".join(f"{s.rung.deployment.id}={s.reason}" for s in self.skipped) or "no rungs"
        )
        raise NoCompliantRoute(detail, public_message=public)


@dataclass(frozen=True)
class RoutingChain:
    """The matrix: every rung, for every tier, in one immutable table.

    In Postgres this is `routing_rung`, editable from the console at runtime. Tier
    assignment changes roughly monthly as providers ship models, and a change that needs
    an engineer and a release is a change that stops happening, after which the pools rot.
    """

    rungs: tuple[RoutingRung, ...] = ()

    def __post_init__(self) -> None:
        seen: set[tuple[Tier, int]] = set()
        for rung in self.rungs:
            key = (rung.tier, rung.position)
            if key in seen:
                # Two rungs claiming one position makes the chain order depend on
                # insertion order, so the executed chain stops being reconstructable from
                # the attempt rows, which is the whole point of recording them.
                msg = f"two rungs share position {rung.position} in tier {rung.tier}"
                raise ValueError(msg)
            seen.add(key)

    def rungs_for(self, tier: Tier) -> tuple[RoutingRung, ...]:
        """Every rung in the tier, in position order."""
        return tuple(sorted((r for r in self.rungs if r.tier is tier), key=lambda r: r.position))

    def role_of(self, rung: RoutingRung) -> RungRole:
        peers = self.rungs_for(rung.tier)
        if not peers:
            msg = f"rung {rung.deployment.id} is not in this chain"
            raise ValueError(msg)
        primary = peers[0]
        if rung.position == primary.position:
            return RungRole.PRIMARY
        if rung.deployment.provider == primary.deployment.provider:
            return RungRole.SAME_PROVIDER_FAILOVER
        return RungRole.CROSS_PROVIDER_FAILOVER

    def tier_of_model(self, model: str) -> Tier | None:
        """Resolve an explicitly named model to its tier.

        This is how "the caller asked for a specific model" becomes an input to
        `classify_tier` without the classifier itself needing a registry lookup, which
        would make it neither pure nor cheap.
        """
        for rung in sorted(self.rungs, key=lambda r: r.position):
            if rung.model == model:
                return rung.tier
        return None

    def narrowest_window(self, tier: Tier) -> int:
        """The smallest context window in the tier, or 0 when the tier has no rungs.

        The tier's declared window must not exceed this. Declare the primary's window
        instead and a request sized to fit the primary overflows the fallback rung, so the
        fallback is unusable at exactly the moment it is reached.
        """
        windows = [r.deployment.context_window for r in self.rungs_for(tier)]
        return min(windows) if windows else 0

    def worst_case_seconds(self, tier: Tier) -> float:
        """The longest a caller can wait for this tier before the chain gives up.

        Nobody computes this, and then a three-rung chain with two attempts each and a
        thirty-second timeout takes three minutes that no one intended.
        """
        return sum(r.worst_case_seconds for r in self.rungs_for(tier))

    def select(
        self,
        tier: Tier,
        *,
        residency: ResidencyRequirement = UNCONSTRAINED,
        breakers: Mapping[str, CircuitBreaker] = MappingProxyType({}),
        now: datetime | None = None,
    ) -> ChainSelection:
        """Filter the tier's rungs. Skips, never substitutes, and never raises.

        A deployment with no breaker on file is treated as closed: a provider we have not
        yet failed against is healthy, not unknown-and-therefore-blocked.

        Passing breakers without a `now` raises rather than skipping the health check. The
        alternative is a caller who believes the breakers are being consulted while every
        open one is silently admitted, which is the failure the breakers exist to prevent.
        """
        if breakers and now is None:
            msg = "breakers were supplied without a `now`; the health check needs a clock"
            raise ValueError(msg)
        kept: list[RoutingRung] = []
        skipped: list[SkippedRung] = []
        for rung in self.rungs_for(tier):
            if not rung.deployment.enabled:
                skipped.append(SkippedRung(rung=rung, reason=SkipReason.DISABLED))
                continue
            # Residency is checked before health on purpose. A rung that may never carry
            # this data is skipped for that reason whatever its breaker says, and
            # reporting a permanent policy fact as a transient outage sends the operator
            # to the wrong dashboard for as long as it takes them to notice.
            if not residency.satisfied_by(rung.deployment):
                skipped.append(SkippedRung(rung=rung, reason=SkipReason.RESIDENCY))
                continue
            breaker = breakers.get(rung.deployment.id)
            if breaker is not None and now is not None and not breaker.admits(now):
                skipped.append(SkippedRung(rung=rung, reason=SkipReason.CIRCUIT_OPEN))
                continue
            kept.append(rung)
        return ChainSelection(tier=tier, rungs=tuple(kept), skipped=tuple(skipped))


# ---------------------------------------------------------------------------- the seed
#: The wall clock an ANSWER-lane chain may consume before it gives up, in seconds. A
#: person is waiting, and past roughly this point they have opened another tab. It bounds
#: `RoutingChain.worst_case_seconds(Tier.MAIN)`, which is asserted in the tests, because
#: the compounding (rungs times attempts times timeout) is invisible in any single row of
#: the console editor.
ANSWER_LANE_WALL_CLOCK_BUDGET_SECONDS = 25.0


def seed_chain() -> RoutingChain:
    """The matrix the console starts from. Not a runtime source of truth.

    Two tiers, not three. A tier stays cache-warm only above roughly one request per five
    minutes during office hours (about 96 a day); at pilot volume a HEAVY-equivalent SMALL
    tier would see a handful a day, go cold on every one of them, and pay the cache write
    premium with no reads. SMALL is added when measurement shows a class of sub-steps
    crossing that line, not before.

    There is no cross-provider rung, and that is honest rather than incomplete: only one
    provider is contracted. A chain listing a provider we hold no key for is a chain that
    fails at the exact moment it is reached.
    """
    sonnet_global = Deployment(
        id="anthropic-sonnet-global",
        provider="anthropic",
        model="claude-sonnet-5",
        region="global",
        residency_class=ResidencyClass.GLOBAL,
        context_window=200_000,
    )
    sonnet_us = Deployment(
        id="anthropic-sonnet-us-east-1",
        provider="anthropic",
        model="claude-sonnet-5",
        region="us-east-1",
        residency_class=ResidencyClass.REGION_PINNED,
        context_window=200_000,
    )
    opus_global = Deployment(
        id="anthropic-opus-global",
        provider="anthropic",
        model="claude-opus-5",
        region="global",
        residency_class=ResidencyClass.GLOBAL,
        context_window=200_000,
    )
    opus_us = Deployment(
        id="anthropic-opus-us-east-1",
        provider="anthropic",
        model="claude-opus-5",
        region="us-east-1",
        residency_class=ResidencyClass.REGION_PINNED,
        context_window=200_000,
    )
    return RoutingChain(
        rungs=(
            # 12 seconds is 1.5x the slowest ordinary request the runtime profile is sized
            # for (8 seconds), so a legitimately slow question is not cut off. attempts
            # stays at 1: a retry against the deployment that has just timed out is
            # another full 12 seconds for the same outcome, and the next rung is a better
            # use of the same 12 seconds.
            RoutingRung(
                tier=Tier.MAIN,
                position=0,
                model=sonnet_global.model,
                deployment=sonnet_global,
                attempts=1,
                timeout_seconds=12.0,
                max_concurrency=40,
            ),
            RoutingRung(
                tier=Tier.MAIN,
                position=1,
                model=sonnet_us.model,
                deployment=sonnet_us,
                attempts=1,
                timeout_seconds=12.0,
                max_concurrency=20,
            ),
            # Nobody is watching a TASK run, so retries are worth their wall clock here in
            # a way they never are on the answer lane. Concurrency is a fraction of MAIN's
            # because these runs hold their slot for minutes.
            RoutingRung(
                tier=Tier.HEAVY,
                position=0,
                model=opus_global.model,
                deployment=opus_global,
                attempts=2,
                timeout_seconds=90.0,
                max_concurrency=8,
            ),
            RoutingRung(
                tier=Tier.HEAVY,
                position=1,
                model=opus_us.model,
                deployment=opus_us,
                attempts=2,
                timeout_seconds=90.0,
                max_concurrency=4,
            ),
        )
    )


@dataclass(frozen=True)
class RoutePlan:
    """A whole routing answer: the tier, the argument for it, and the surviving chain."""

    decision: TierDecision
    selection: ChainSelection

    @property
    def skipped(self) -> tuple[SkippedRung, ...]:
        """Every rung left out, so a trace can show the shape of the chain that was not
        used. Delegated rather than copied onto the plan: two fields holding the same
        tuple is two fields that can disagree, and the one a reader trusts would be
        whichever the console happened to render."""
        return self.selection.skipped

    @property
    def surviving_rungs(self) -> int:
        """How many rungs this plan still has to try.

        **This is not the depth alerting reads**, and it used to say it was. This number
        *shrinks* as providers fail: a two-rung chain with one breaker open reports 1.
        M5.4.8 wants the opposite, how far down the chain a request actually had to go,
        which *grows*. That lives on `brain.models.health.ChainOutcome.depth`, is counted
        upward from the rung that served, and includes the open breakers skipped above it.

        Renamed because two attributes called `depth` in one subsystem meaning opposite
        things is a trap that reads correct at every call site.
        """
        return len(self.selection.rungs)


def plan(
    request: RoutingRequest,
    chain: RoutingChain,
    *,
    breakers: Mapping[str, CircuitBreaker] = MappingProxyType({}),
    now: datetime | None = None,
    windows: Mapping[Tier, int] = TIER_CONTEXT_WINDOW,
) -> RoutePlan:
    """Classify, then filter. The whole synchronous routing decision, with no I/O.

    Does not raise on an empty chain. Call `plan(...).selection.require()` to get the
    rungs or the refusal; splitting it that way means a caller who wants to inspect a
    doomed chain (the console, a trace viewer) can, without catching an exception to do it.
    """
    decision = classify_tier(request, windows=windows)
    selection = chain.select(
        decision.tier,
        residency=decision.residency,
        breakers=breakers,
        now=now,
    )
    return RoutePlan(decision=decision, selection=selection)
