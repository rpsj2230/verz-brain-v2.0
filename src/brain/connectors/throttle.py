"""What sits in front of every external system: the bucket, the breaker, the retry, the numbers.

The architecture puts a token bucket, a circuit breaker and a retry policy in front of *every*
external system, owned by the connector layer rather than by callers, because twenty agent
runs must not independently decide to call the same API twenty times.

Two of those three already exist, and this module deliberately does not rewrite them.

**The bucket is `brain.ops.limits`.** It holds the sliding window, the verified source
ceilings and the per-principal fair share, and it was written with the boundary case that
matters already tested. A second bucket here would be a second answer to "is this caller
entitled to ask again yet", and the day the two disagree the permissive one wins. `limits_for`
is a thin adapter that refuses an unknown source rather than inventing a ceiling for it.

**The breaker is `brain.models.routing.CircuitBreaker`.** It is a pure state machine over an
identifier, with the half-open probe claim, the exponential cooldown and the caller-supplied
jitter already right. Its field is called `deployment_id`, which reads oddly here, and that is
a smaller cost than two breakers that drift.

What is genuinely new is the third thing, and it is the one everybody gets wrong.

**A 429 is not a breaker failure.** It is the rate limiter working, and the source saying so.
Counting it as a health failure opens the breaker every time a connector is popular, taking a
perfectly healthy system out of service for the crime of being asked. The remedies are
opposite, too: a breaker opens to protect *us* from a dead source, and a quota refusal exists
to protect *the source* from us. `classify` separates them, and `is_breaker_failure` is where
the distinction is written down.

**And a write with no read-back is never retried.** The protocol's own recent revision removed
message redelivery, so connector-side idempotency is mandatory rather than optional. A retried
write either repeats the action or does not, and the only way to tell is to ask the source what
happened; a connector that cannot answer that is restricted to read-only tools, which
`manifest.ToolDeclaration` refuses at review. This is the request-time half of the same rule.

Scope: domain logic. Nothing here opens a connection or reads a clock; `now` is a parameter
and jitter is supplied by the caller, for the reason `CircuitBreaker` gives about both.

Task ids: M11.3.1, M11.3.2, M11.3.3, M11.3.4, M11.3.5
"""

from __future__ import annotations

import enum
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from brain.connectors.manifest import ConnectorManifest
from brain.core.envelope import SideEffect
from brain.models.routing import CircuitBreaker
from brain.ops.limits import (
    MAX_BACKOFF_SECONDS,
    ConnectorLimit,
    Limit,
    backoff_seconds,
    connector_ceiling,
    search_completeness,
    source_limits,
)

# ------------------------------------------------------------------ written-down reasons
#: Why the bucket and the breaker are imported rather than written again.
ONE_BUCKET_AND_ONE_BREAKER = (
    "The sliding window lives in brain.ops.limits and the breaker state machine lives in "
    "brain.models.routing. Both were written with the case that actually goes wrong already "
    "tested: the window boundary for one, the half-open probe for the other. A second copy "
    "here would be a second answer to the same question, and a rule implemented twice is a "
    "rule that will be changed once. The cost is that a breaker's identifier field is called "
    "deployment_id while holding a connector name, which is a smaller problem than two "
    "breakers that disagree about whether a connector is up."
)

#: Why a quota refusal is kept away from the breaker.
A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH = (
    "A 429 is the source telling us the rate limiter should have stopped us, which is a "
    "statement about our volume and not about the source's health. Feeding it to the breaker "
    "opens the circuit whenever a connector is popular, so the busiest connector in the "
    "company is the one that is intermittently unavailable, and the fix somebody reaches for "
    "is a longer cooldown, which makes it worse. The two failures also have opposite "
    "remedies: a breaker protects us from a dead source, and a quota refusal protects the "
    "source from us."
)

#: Why a refusal that named no wait is given the longest one rather than none.
A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO = (
    "brain.ops.limits.backoff_seconds multiplies the figure it is handed, so a refusal "
    "carrying no Retry-After header multiplies zero and returns zero however many refusals "
    "came before it. The doubling looks like a backoff and produces none, and the client that "
    "comes back immediately is the one that turns a burst into a rate limit and then keeps it "
    "there, which is the failure this module exists to prevent. backoff_seconds is not the "
    "bug: it is handed a measured time until the window has room, and zero is a truthful "
    "answer to that question. The bug is the conversion at the boundary, where a header "
    "nobody sent becomes a float that means the opposite of what happened. So the parameter "
    "here is float | None and the distinction is carried by the type: a float is a figure "
    "somebody measured, None is the absence of one, and there is no call site left to write "
    "retry_after(headers) or 0.0 in. A stated zero is floored too, because a 429 is a refusal "
    "by definition and no refusal is ever honestly answered by returning at once. The "
    "substituted figure is the platform's own MAX_BACKOFF_SECONDS rather than one chosen "
    "here, and it is deliberately the long end: guessing low spends what is left of a daily "
    "allowance faster, while a wait that is too long costs one question its freshness."
)

#: Why a percentile is nearest-rank rather than interpolated.
A_PERCENTILE_IS_A_MEASUREMENT_NOT_AN_ESTIMATE = (
    "Nearest-rank returns a latency that actually happened. Interpolating between two samples "
    "returns one that did not, which at these volumes is most of the time: a window with "
    "eleven calls in it has no ninety-fifth percentile to interpolate towards, and the "
    "invented number is then compared against a target and acted on."
)


# ------------------------------------------------------------------- classifying an outcome
class CallOutcome(enum.StrEnum):
    """What one call to a source did. Closed, because everything below branches on it.

    `TRUNCATED` is a success that must not be summarised as one. Freshdesk's search returns at
    most 300 records ever, which is not a page size and cannot be paged past, so "all tickets
    matching" is silently wrong beyond 300 and looks correct in every test anybody writes.
    It is an outcome rather than a flag on OK because the abstention path has to be able to
    branch on it.
    """

    OK = "ok"
    #: Answered, and not with everything. See `brain.ops.limits.SEARCH_CAP_IS_NOT_A_PAGE_SIZE`.
    TRUNCATED = "truncated"
    #: The source refused on volume. Not ill health: see `A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH`.
    QUOTA = "quota"
    #: The source is unwell: a timeout, a connection failure, a 5xx.
    UNAVAILABLE = "unavailable"
    #: The request was wrong: a 4xx that is not a 429. Retrying reproduces it exactly.
    REJECTED = "rejected"


#: The status a source returns when it is rate limiting us.
HTTP_TOO_MANY_REQUESTS: Final = 429

#: Below this a status is a success; at or above it and below 500 the request was wrong.
HTTP_CLIENT_ERROR: Final = 400
HTTP_SERVER_ERROR: Final = 500


def classify(
    *,
    status: int | None = None,
    timed_out: bool = False,
    connection_failed: bool = False,
    returned: int | None = None,
    connector: str = "",
) -> CallOutcome:
    """One call's facts, as an outcome. The order of the branches is the rule.

    Timeouts and connection failures are checked first, because a source that never answered
    has no status to classify and a caller with a stale status variable would otherwise have
    it read. Then 429, before the generic client-error branch, because 429 is a 4xx and
    lumping it in with the rest is exactly the mistake `A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH`
    describes, in the opposite direction: it would make a quota refusal permanent instead of
    retryable.

    Truncation is checked last, on a successful call, and only for a source that declares a
    search ceiling. `brain.ops.limits.search_completeness` owns which sources those are, so
    this cannot come to a different conclusion from the module the ceiling is recorded in.
    """
    if timed_out or connection_failed:
        return CallOutcome.UNAVAILABLE
    if status == HTTP_TOO_MANY_REQUESTS:
        return CallOutcome.QUOTA
    if status is not None and status >= HTTP_SERVER_ERROR:
        return CallOutcome.UNAVAILABLE
    if status is not None and status >= HTTP_CLIENT_ERROR:
        return CallOutcome.REJECTED
    if returned is not None and connector and not search_completeness(connector, returned).complete:
        return CallOutcome.TRUNCATED
    return CallOutcome.OK


def is_breaker_failure(outcome: CallOutcome) -> bool:
    """Whether this outcome should count against the source's health.

    Only `UNAVAILABLE`. A quota refusal is our volume, a rejection is our request, and a
    truncation is a complete answer to a question the source could not fully answer. Feeding
    any of the three to a breaker takes a working source out of rotation on evidence that it
    is working.
    """
    return outcome is CallOutcome.UNAVAILABLE


def is_retryable(
    outcome: CallOutcome, *, side_effect: SideEffect = SideEffect.NONE, verifies_write: bool = False
) -> bool:
    """Whether this call may be made again, given what it does to the world.

    Two independent conditions, and the second is the one that matters.

    The outcome has to be one a retry could change: `UNAVAILABLE` and `QUOTA` might, and
    `REJECTED` never will, because the request was wrong and will be wrong again at full cost.
    `TRUNCATED` is not retryable either, and that is the important one: retrying a search that
    hit a hard 300-record ceiling returns the same 300 records, and a caller who reads a retry
    as a way to see more is reading a cap as a page size.

    And a call with a side effect may only be retried when the connector can read back whether
    the first attempt landed. Without that, a retry either repeats the action or loses it, and
    nothing anywhere can tell which happened. `manifest.ToolDeclaration` refuses to declare
    such a tool at all; this refuses to retry one if it exists anyway.
    """
    if outcome not in (CallOutcome.UNAVAILABLE, CallOutcome.QUOTA):
        return False
    if side_effect is SideEffect.NONE:
        return True
    return verifies_write


#: What a refusal that stated no wait, or stated a zero, is treated as having asked for. The
#: platform's own ceiling rather than a figure invented here. See
#: `A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO`.
RETRY_AFTER_WHEN_UNSTATED: Final = MAX_BACKOFF_SECONDS


def retry_delay(
    *, retry_after_seconds: float | None, consecutive_refusals: int, jitter: float = 0.0
) -> float:
    """How long to wait before trying again. The window's own arithmetic, not a guess.

    Delegated whole to `brain.ops.limits.backoff_seconds`, which already holds the rule: the
    measured time until the window has room, until a client has demonstrated three times over
    that it is not reading the hint, and then doubling to a ceiling. Restating it here would
    give a connector a different backoff from the rest of the platform for no reason anybody
    could name later.

    **What is not delegated is the absence of a figure.** `backoff_seconds` multiplies what it
    is given, so a refusal carrying no `Retry-After` header arrives as zero, is doubled four
    times, and comes back as zero: a backoff that looks like one and is a hot retry loop. The
    parameter is therefore `float | None`, so a source that said nothing is a different value
    from a source that said zero, and the substitution happens once here rather than in each
    connector that remembers to. Three connectors reached this rule independently and a fourth
    did not, which is the argument for it living at the shared root. See
    `A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO`.

    Jitter is the caller's, and only ever lengthens, matching both modules it sits between.
    """
    stated = (
        RETRY_AFTER_WHEN_UNSTATED
        if retry_after_seconds is None or retry_after_seconds <= 0.0
        else retry_after_seconds
    )
    return backoff_seconds(stated, consecutive_refusals=consecutive_refusals, jitter=jitter)


# ------------------------------------------------------- the bucket, adapted (M11.3.1, M11.3.5)
class UnmeasuredSourceError(Exception):
    """A connector declared a ceiling nobody has verified.

    Refusing is the whole point. `source_limits` returns nothing for an unknown connector
    rather than a default, and a caller that read that as "no limits apply" would run a source
    with no ceiling at all. Inventing one is worse again: it produces a number that looks
    verified, sits in a console beside three that are, and is wrong in whichever direction
    somebody guessed.
    """


def limits_for(manifest: ConnectorManifest, *, principal_id: str) -> tuple[Limit, ...]:
    """Every allowance governing one caller making one call to this connector (M11.3.5).

    The manifest names a ceiling and `brain.ops.limits` owns the numbers, which is the split
    that keeps "configured ceilings matching documented limits" true: a connector cannot
    declare its own ceiling, so no connector can be optimistic about one. The verified figures
    are stated where they were verified, dated, with the consequence written beside them.
    """
    if not manifest.ceiling:
        msg = (
            f"connector {manifest.name!r} declares no ceiling; the verified source limits live "
            "in brain.ops.limits and a connector with none would run against no limit at all"
        )
        raise UnmeasuredSourceError(msg)
    limits = source_limits(manifest.ceiling, principal_id=principal_id)
    if not limits:
        msg = (
            f"connector {manifest.name!r} names ceiling {manifest.ceiling!r}, which is not one "
            "of the verified sources; inventing a ceiling produces a number that looks "
            "measured and is not"
        )
        raise UnmeasuredSourceError(msg)
    return limits


def ceiling_for(manifest: ConnectorManifest) -> ConnectorLimit:
    """The verified ceiling this connector runs against, for a console row.

    Separate from `limits_for` because they answer different questions. That one produces the
    windows a request is checked against; this one produces the published numbers an operator
    reads, including whether anything we can do moves them. Xero's cannot be moved because it
    belongs to the client's tenant, and an operator must not go looking for an upgrade button.
    """
    ceiling = connector_ceiling(manifest.ceiling)
    if ceiling is None:
        msg = f"connector {manifest.name!r} names no verified ceiling"
        raise UnmeasuredSourceError(msg)
    return ceiling


# ------------------------------------------------------- the breaker, adapted (M11.3.2)
def connector_breaker(connector: str) -> CircuitBreaker:
    """A fresh breaker for one connector.

    A named constructor rather than a subclass, so there is exactly one state machine and this
    module cannot accumulate an opinion of its own about when a circuit opens. See
    `ONE_BUCKET_AND_ONE_BREAKER`.
    """
    return CircuitBreaker(deployment_id=connector)


def record_outcome(
    breaker: CircuitBreaker, outcome: CallOutcome, *, now: datetime, jitter: float = 0.0
) -> CircuitBreaker:
    """Feed one call's outcome to the breaker, or deliberately do not.

    A quota refusal, a rejection and a truncation return the breaker untouched, and untouched
    is the correct word: they do not count as successes either. Recording one as a success
    would let a stream of 429s hold a genuinely failing connector's breaker closed, which is
    the same mistake as counting them as failures, arrived at from the other side.
    """
    if outcome is CallOutcome.OK:
        return breaker.record_success(now)
    if is_breaker_failure(outcome):
        return breaker.record_failure(now, jitter=jitter)
    return breaker


# --------------------------------------------------------------------- metrics (M11.3.4)
@dataclass(frozen=True)
class CallRecord:
    """One completed call. What a metric window is computed over.

    Deliberately without the request, the response or any identifier from either. A metrics
    record that carried a filter value would put a client's name in whatever stores the
    metrics, which has a different retention and a different audience from the answer it
    described. `brain.models.adapter._trace` makes the same argument about its own line.
    """

    at: datetime
    connector: str
    outcome: CallOutcome
    latency_ms: float


@dataclass(frozen=True)
class ConnectorMetrics:
    """Everything the architecture asks to be tracked per connector, over one window.

    Requests per second and per minute, concurrency, the 429 rate, latency and the error rate.
    Concurrency is passed in rather than derived: a completed call's record says nothing about
    how many were in flight at once, and computing it from overlapping intervals would produce
    a number that is right on average and wrong at the peak, which is the only value anybody
    wants concurrency for.
    """

    connector: str
    window_seconds: float
    requests: int
    per_second: float
    per_minute: float
    concurrency: int
    quota_ratio: float
    error_ratio: float
    latency_p50_ms: float
    latency_p95_ms: float

    @property
    def is_quiet(self) -> bool:
        """Whether there were too few calls for the ratios to mean anything.

        The same guard `brain.ops.limits.assess_volume` applies to its own ratio: one failure
        in two is a 50% error rate and is also two calls. A dashboard that pages on it teaches
        people to ignore the dashboard.
        """
        return self.requests < QUIET_WINDOW_REQUESTS


#: Below this many calls in a window, a ratio is noise. Ten, matching the spirit of
#: `VOLUME_MIN_OBSERVATIONS` at a shorter window: the estate runs at roughly 0.1 requests a
#: second overall, so a one-minute window on a single connector is routinely this small.
QUIET_WINDOW_REQUESTS: Final = 10

#: The window metrics default to. One minute, matching the shorter of the two published
#: ceilings, so a rate computed here is directly comparable with the limit it is measured
#: against rather than needing a conversion nobody would do consistently.
DEFAULT_METRIC_WINDOW = timedelta(minutes=1)


def percentile_ms(latencies: Sequence[float], fraction: float) -> float:
    """Nearest-rank, never interpolated. See `A_PERCENTILE_IS_A_MEASUREMENT_NOT_AN_ESTIMATE`.

    An empty sample returns 0.0 rather than raising: a window with no calls in it is the
    ordinary state of most connectors most of the time, and a metrics function that raised on
    it would put a `try` around every call site.
    """
    if not latencies:
        return 0.0
    if not 0.0 < fraction <= 1.0:
        msg = f"a percentile fraction is in (0, 1], not {fraction}"
        raise ValueError(msg)
    ordered = sorted(latencies)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def measure(
    connector: str,
    records: Sequence[CallRecord],
    *,
    now: datetime,
    window: timedelta = DEFAULT_METRIC_WINDOW,
    concurrency: int = 0,
) -> ConnectorMetrics:
    """Everything about one connector over the last window.

    Records outside the window and records belonging to other connectors are dropped here
    rather than expected to have been filtered by the caller. Expecting it means the filter is
    applied twice in some call sites and not at all in others, and the metric that reads as a
    spike is the one where somebody forgot.
    """
    window_seconds = window.total_seconds()
    if window_seconds <= 0:
        msg = "a metric window is positive; a zero window has no rate"
        raise ValueError(msg)
    cutoff = now - window
    live = [r for r in records if r.connector == connector and cutoff < r.at <= now]
    count = len(live)
    quota = sum(1 for r in live if r.outcome is CallOutcome.QUOTA)
    errors = sum(1 for r in live if r.outcome in (CallOutcome.UNAVAILABLE, CallOutcome.REJECTED))
    latencies = [r.latency_ms for r in live]
    return ConnectorMetrics(
        connector=connector,
        window_seconds=window_seconds,
        requests=count,
        per_second=count / window_seconds,
        per_minute=count / window_seconds * 60.0,
        concurrency=concurrency,
        quota_ratio=(quota / count) if count else 0.0,
        error_ratio=(errors / count) if count else 0.0,
        latency_p50_ms=percentile_ms(latencies, 0.5),
        latency_p95_ms=percentile_ms(latencies, 0.95),
    )
