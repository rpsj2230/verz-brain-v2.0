"""Whether a connector may burst now, and at what rate it is allowed to keep going.

`brain.ops.limits` already holds a sliding window, and the obvious reading of this module is
that it is a second one. It is not, and the difference is the reason both exist.

**A window answers "how many in the last minute". A bucket answers "may I burst now, and at
what sustained rate".** Those are different questions with different failure modes, and
neither answer contains the other:

- The window is a *total*. It is what keeps us under Xero's 5,000 a day, and it is the only
  one of the two that can count a day at all: see `THE_BUCKET_IS_NOT_THE_DAILY_BUDGET`.
  What it does not constrain is shape. A limit of 100 a minute admits all 100 inside one
  second and then nothing for fifty-nine, which is within our own accounting and is exactly
  the traffic that trips a vendor's own per-second smoothing.
- The bucket is a *shape*. It paces a fan-out: sixteen parallel calls go straight through
  and the seventeenth waits, at a rate derived from the vendor's published ceiling. What it
  does not constrain is the total, because over a long enough interval a bucket admits
  `capacity + rate x seconds`, which is the whole point of a burst allowance.

So they compose and neither replaces the other. Nothing here calls the window and nothing
there calls this, deliberately, for the reason `limits.LIMITS_ARE_CHECKED_BEFORE_CAPACITY`
gives about admission: composing them inside one of the two would make that one depend on
the other's stored state, and the gate already holds both.

**The refill rate is derived from the declared ceiling and cannot be configured beside it.**
A bucket that refills faster than the vendor documents is a bucket whose only observable
effect is getting the client rate-limited by their own supplier, and the number would look
deliberate because somebody typed it. `bucket_for` computes the rate from
`brain.ops.limits.connector_ceiling`, and `TokenBucket.__post_init__` refuses a rate above
it however the object was built, so a hand-constructed bucket cannot be more generous than
the derived one. An unmeasured connector is refused rather than given a default: see
`limits.source_limits`, which returns nothing for the same reason.

**A refused take spends nothing.** The same rule as
`limits.REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW`, and it is wrong in the same way when it
is broken: a client that retries eagerly would push its own recovery further away every
time it tried, so a one-second wait becomes permanent and the hint the client was given
becomes a lie.

Rejected: making this the daily budget as well, by refilling at the lower of the per-minute
and per-day rates. Xero's 5,000 a day is 0.058 calls a second smeared across 86,400, so a
bucket derived that way would refill one token every seventeen seconds and every interactive
question would wait. `limits.effective_per_day` already says why the smearing is false:
office traffic does not arrive evenly across 1,440 minutes. The daily ceiling is the
window's, which can count a day; this paces seconds.

Nothing here opens a connection or reads a clock. `now` is a parameter, for the reason
`brain.models.routing.CircuitBreaker` gives, and there is no store half of this module yet:
when there is one it belongs beside `brain.ops.limit_store`, holding the connection and no
policy, and it will find the arithmetic here already tested for the case that is always
wrong, which is a bucket refilled across a gap it did not observe.

Task ids: M11.3.1
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Final

from brain.ops.limits import MINUTE_SECONDS, ConnectorLimit, connector_ceiling

# ------------------------------------------------------------------ written-down reasons
#: Why a token bucket exists beside a sliding window rather than instead of it.
A_WINDOW_IS_A_TOTAL_AND_A_BUCKET_IS_A_SHAPE = (
    "A sliding window answers how many calls happened in the last minute or the last day, "
    "which is the question a published quota asks. A token bucket answers whether a burst "
    "may go now and how fast the caller may keep going, which is the question a vendor's "
    "unpublished per-second smoothing asks. A window admits its whole minute inside one "
    "second and is still correct by its own definition; a bucket admits capacity plus rate "
    "times elapsed over any interval and so cannot bound a day. Replacing either with the "
    "other loses the failure the other one catches, and the loss is invisible until a "
    "vendor starts refusing us."
)

#: Why the daily ceiling is not what this refills from.
THE_BUCKET_IS_NOT_THE_DAILY_BUDGET = (
    "Deriving the refill from the lower of the minute and day ceilings looks stricter and is "
    "wrong. Xero's 5,000 a day is 0.058 calls a second only if traffic arrives evenly across "
    "1,440 minutes, and brain.ops.limits.effective_per_day already records that it does not: "
    "office traffic is a working day with a backfill in it. A bucket derived that way would "
    "make every interactive question wait seventeen seconds for a token while the day's "
    "budget sat unspent. The day is the window's to count; this paces seconds."
)

#: Why the rate is computed rather than accepted.
THE_REFILL_IS_DERIVED_NOT_CONFIGURED = (
    "A bucket refilling faster than the vendor documents has one observable effect, which is "
    "that the client gets rate-limited by their own supplier, and the number looks "
    "deliberate because somebody typed it. So the rate is computed from the verified ceiling "
    "in brain.ops.limits, and a bucket built by hand with a faster rate is refused rather "
    "than accepted and warned about. Lark Base's 100 a minute is documented as unraisable, "
    "so sizing above it is sizing against a number that does not exist."
)

#: Why a refusal costs nothing.
A_REFUSED_TAKE_SPENDS_NOTHING = (
    "Only an admitted call consumes a token. Charging for a refusal means a client that "
    "retries too eagerly drains a bucket it was already waiting on, so the wait grows with "
    "every attempt and the retry hint the client was handed becomes false the moment it acts "
    "on it. This is limits.REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW, said about tokens."
)


# ------------------------------------------------------------------------------ the error
class TokenBucketError(Exception):
    """A bucket was configured in a shape that would breach the source's own ceiling.

    Outside the user-facing taxonomy in `brain.core.errors`, and one type rather than two.
    An unmeasured connector and an over-generous refill are the same mistake, by the same
    person, at the same moment: declaring how hard a connector may be driven. Splitting them
    would invent a distinction nobody would act on differently, and both should stop a
    connector being installed rather than reach anybody who asked a question.
    """


# --------------------------------------------------------------- deriving from the ceiling
#: How long a quiet connector may bank tokens for, in seconds of its own sustained rate.
#: Ten, which makes Xero's burst ten calls and Lark Base's sixteen. Chosen against what a
#: fan-out actually looks like here: a question that touches several clients issues a
#: handful of calls at once and then stops, and a burst smaller than that would pace the
#: only traffic pattern the platform has. It is deliberately far below a whole window, so a
#: single fan-out cannot spend a minute's allowance in a second.
DEFAULT_BURST_SECONDS: Final = 10.0

#: The smallest a bucket may be. A capacity below one token is a connector that can never
#: admit a single call while reading in a console as configured, which is worse than a
#: connector that is visibly switched off.
MIN_CAPACITY: Final = 1.0


def sustained_per_second(ceiling: ConnectorLimit) -> float:
    """The fastest a connector may be driven indefinitely, from its verified minute ceiling.

    The per-minute figure and not the per-day one: see `THE_BUCKET_IS_NOT_THE_DAILY_BUDGET`.
    One function so the division exists once; two call sites computing `per_minute / 60`
    independently is how a bucket ends up a rounding away from its own limit.
    """
    if ceiling.per_minute < 1:
        msg = f"{ceiling.name} declares a per-minute ceiling of {ceiling.per_minute}"
        raise TokenBucketError(msg)
    return ceiling.per_minute / MINUTE_SECONDS


def _verified(connector: str) -> ConnectorLimit:
    """The verified ceiling for a connector, or a refusal naming why there is no default.

    Mirrors `brain.connectors.throttle.limits_for`, and does not import it: `throttle` is in
    `brain.connectors`, which imports this package, and reaching back the other way would
    make the layering circular for the sake of one exception class.
    """
    ceiling = connector_ceiling(connector)
    if ceiling is None:
        msg = (
            f"{connector!r} is not one of the verified sources, so there is no ceiling to "
            "derive a refill rate from. Inventing one produces a number that looks measured, "
            f"sits in a console beside numbers that are, and paces nothing. "
            f"{THE_REFILL_IS_DERIVED_NOT_CONFIGURED}"
        )
        raise TokenBucketError(msg)
    return ceiling


# -------------------------------------------------------------------------- the bucket
@dataclass(frozen=True)
class TokenBucket:
    """One connector's burst allowance, and how fast it comes back.

    Frozen and returned rather than mutated, matching `limits.WindowState`: the state is
    somebody else's to store, and an object that mutated in place could not be handed to a
    store that has to write it back conditionally.

    `__post_init__` re-derives the ceiling on every construction, `dataclasses.replace`
    included, so a bucket that has been refilled, spent or rebuilt is checked against the
    published limit every time rather than only when it was first made.
    """

    connector: str
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: datetime

    def __post_init__(self) -> None:
        ceiling = _verified(self.connector)
        allowed = sustained_per_second(ceiling)
        if self.refill_per_second <= 0:
            msg = f"{self.connector} refills at {self.refill_per_second}/s, which never refills"
            raise TokenBucketError(msg)
        if self.refill_per_second > allowed:
            msg = (
                f"{self.connector} would refill at {self.refill_per_second:.4f}/s against a "
                f"verified ceiling of {ceiling.per_minute} a minute, which is "
                f"{allowed:.4f}/s. {THE_REFILL_IS_DERIVED_NOT_CONFIGURED}"
            )
            raise TokenBucketError(msg)
        if self.capacity < MIN_CAPACITY:
            msg = (
                f"{self.connector} has a capacity of {self.capacity}, below one token, so no "
                "call could ever be admitted while the connector reads as configured"
            )
            raise TokenBucketError(msg)
        if self.capacity > ceiling.per_minute:
            msg = (
                f"{self.connector} would hold a burst of {self.capacity} against a ceiling of "
                f"{ceiling.per_minute} a minute; a burst larger than the whole declared "
                "window breaches it on its own, whatever the refill rate is"
            )
            raise TokenBucketError(msg)
        if not 0.0 <= self.tokens <= self.capacity:
            msg = f"{self.connector} holds {self.tokens} of a capacity of {self.capacity}"
            raise TokenBucketError(msg)

    def refilled(self, now: datetime) -> TokenBucket:
        """The bucket as of `now`, with elapsed time turned into tokens. Idempotent.

        A `now` behind `updated_at` adds nothing and does not move the clock back. Callers
        pass `now` in from outside, so a replayed or skewed instant is not hypothetical, and
        letting it rewind `updated_at` would hand out the same seconds twice on the next
        call. This is the same refusal `limits.WindowState.record` makes by sorting.
        """
        elapsed = max(0.0, (now - self.updated_at).total_seconds())
        return replace(
            self,
            tokens=min(self.capacity, self.tokens + elapsed * self.refill_per_second),
            updated_at=self.updated_at if now < self.updated_at else now,
        )

    def take(self, now: datetime, *, cost: float = 1.0) -> BucketDecision:
        """Spend one call's worth, or say how long until there is room. See `BucketDecision`.

        The wait is measured rather than guessed, matching the rule
        `limits.THE_HINT_IS_MEASURED_NOT_GUESSED` states: the bucket knows its own rate, so
        it knows exactly when the missing tokens arrive. A caller that has been refused
        repeatedly hands that number to `limits.backoff_seconds`, which owns the spacing rule
        for the whole platform rather than letting each limiter invent one.
        """
        if cost <= 0:
            msg = "a call costs at least one token; a free call is not rate limited at all"
            raise TokenBucketError(msg)
        if cost > self.capacity:
            msg = (
                f"a call costing {cost} against a capacity of {self.capacity} can never be "
                f"admitted by {self.connector}, so waiting for it would never end"
            )
            raise TokenBucketError(msg)

        filled = self.refilled(now)
        if filled.tokens >= cost:
            return BucketDecision(
                admitted=True,
                bucket=replace(filled, tokens=filled.tokens - cost),
                retry_after_seconds=0.0,
                reason=(
                    f"{self.connector} had {filled.tokens:.2f} of {self.capacity:.2f} tokens "
                    f"and spent {cost:.2f}"
                ),
            )
        wait = (cost - filled.tokens) / filled.refill_per_second
        return BucketDecision(
            # See `A_REFUSED_TAKE_SPENDS_NOTHING`. The refilled bucket is returned because
            # refilling is not spending, and dropping it would lose the elapsed time.
            admitted=False,
            bucket=filled,
            retry_after_seconds=wait,
            reason=(
                f"{self.connector} holds {filled.tokens:.2f} tokens and refills at "
                f"{self.refill_per_second:.2f}/s; room for {cost:.2f} in {wait:.2f}s"
            ),
        )


@dataclass(frozen=True)
class BucketDecision:
    """Whether this call may go, the bucket afterwards, and when to come back.

    The bucket travels with the decision rather than being fetched again by the caller,
    because the caller would otherwise have to know that a refusal still advances the clock.
    There is no field here meaning "how many callers were refused" and there must not be
    one: a count of refusals per connector is fine, and a count keyed by anything about the
    person is a different artifact with a different audience.
    """

    admitted: bool
    bucket: TokenBucket
    retry_after_seconds: float
    reason: str


def bucket_for(
    connector: str, *, now: datetime, burst_seconds: float = DEFAULT_BURST_SECONDS
) -> TokenBucket:
    """A full bucket for one connector, paced by that connector's verified ceiling.

    Full rather than empty, and that is a choice. A connector nobody has called has spent
    nothing, so starting it empty would make the first question of the morning wait for a
    quota that is entirely unused. The cost is that a process restart hands back a burst
    that may already have been spent by the process before it, which is an argument for the
    state living in a store rather than for starting empty.

    `burst_seconds` is the one dial, and it can only make the bucket smaller than the
    ceiling: the capacity is clamped to a whole minute's allowance and the *rate* is not a
    parameter at all. See `THE_REFILL_IS_DERIVED_NOT_CONFIGURED`.
    """
    if burst_seconds <= 0:
        msg = f"a burst of {burst_seconds}s holds no tokens"
        raise TokenBucketError(msg)
    ceiling = _verified(connector)
    rate = sustained_per_second(ceiling)
    capacity = max(MIN_CAPACITY, min(float(ceiling.per_minute), rate * burst_seconds))
    return TokenBucket(
        connector=connector,
        capacity=capacity,
        refill_per_second=rate,
        tokens=capacity,
        updated_at=now,
    )


# ------------------------------------------------------------------ one bucket per connector
_NO_BUCKETS: Mapping[str, TokenBucket] = MappingProxyType({})


@dataclass(frozen=True)
class BucketState:
    """Every connector's bucket, keyed by connector. Never one bucket for several sources.

    A shared bucket is the failure this keying exists to prevent, and it is attractive
    because it looks like fairness: one pool, everybody draws from it. What it actually does
    is let a backfill against Xero pace every question against Lark Base, whose ceiling is a
    different number, published by a different vendor, and unraisable. It also does the
    reverse, which is worse: a source with a generous ceiling would top up a pool that a
    strict source is drawing from, so the strict one is driven past its limit by traffic that
    never touched it.

    Modelled on `limits.LimiterState`, and for the same reason: in production these rows
    live in a store, and a state object holding its own client could not be tested for the
    case that is always wrong, which here is a bucket refilled across a gap nobody observed.
    """

    buckets: Mapping[str, TokenBucket] = _NO_BUCKETS

    def bucket_for(
        self, connector: str, *, now: datetime, burst_seconds: float = DEFAULT_BURST_SECONDS
    ) -> TokenBucket:
        """This connector's bucket, made on first use and paced by its own ceiling."""
        held = self.buckets.get(connector)
        if held is not None:
            return held
        return bucket_for(connector, now=now, burst_seconds=burst_seconds)

    def take(
        self,
        connector: str,
        *,
        now: datetime,
        cost: float = 1.0,
        burst_seconds: float = DEFAULT_BURST_SECONDS,
    ) -> tuple[BucketDecision, BucketState]:
        """Spend from one connector's bucket, leaving every other connector's untouched.

        Returns the new state rather than mutating, so a caller that decides not to proceed
        for some other reason simply drops it. The decision and the state are returned
        together because a caller that had to remember to write one back would eventually
        not, and the bucket that silently stops refilling reads as a connector that has
        become slow.
        """
        decision = self.bucket_for(connector, now=now, burst_seconds=burst_seconds).take(
            now, cost=cost
        )
        updated = dict(self.buckets)
        updated[connector] = decision.bucket
        return decision, BucketState(buckets=MappingProxyType(updated))

    def tokens_in(self, connector: str) -> float:
        """What a console row shows. Zero for a connector with no bucket yet, which is
        honest: a bucket that has not been made has not been measured, and reporting its
        theoretical capacity would show an allowance that nothing is holding."""
        held = self.buckets.get(connector)
        return 0.0 if held is None else held.tokens
