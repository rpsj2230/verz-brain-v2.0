"""The per-connector token bucket: what it admits, what it refuses, and what it is not.

Two things are being pinned here and they pull in opposite directions. The bucket has to
admit a burst, because a question that touches four clients issues four calls at once and
pacing that is pacing the only traffic pattern this platform has. And it has to be
impossible to configure faster than the vendor documents, because the only observable effect
of that is the client being rate-limited by their own supplier.

`test_a_window_admits_a_burst_that_a_bucket_paces` is the one that stops somebody deleting
either the bucket or the sliding window on the grounds that the other one exists.

Nothing here reads a clock. Every instant is a constant, which is what makes the refill
arithmetic testable at all.

Task ids: M11.3.1
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.ops.limits import (
    LimiterState,
    check,
    connector_ceiling,
    source_limits,
)
from brain.ops.token_bucket import (
    DEFAULT_BURST_SECONDS,
    BucketState,
    TokenBucket,
    TokenBucketError,
    bucket_for,
    sustained_per_second,
)

T0 = datetime(2026, 9, 6, 9, 0, 0, tzinfo=UTC)

#: Xero's verified ceiling is 60 a minute, so its sustained rate is exactly one call a
#: second and its default burst is exactly ten tokens. Used throughout because the
#: arithmetic is legible: a reader can check every number in this file by hand.
XERO_RATE = 1.0
XERO_BURST = 10.0


def _drain(bucket: TokenBucket, now: datetime) -> tuple[int, TokenBucket]:
    """Take one token at a time, at one instant, until the bucket refuses. Returns the count."""
    admitted = 0
    current = bucket
    while True:
        decision = current.take(now)
        if not decision.admitted:
            return admitted, current
        admitted += 1
        current = decision.bucket


# ------------------------------------------------------------------ the burst and the rate
def test_a_bucket_admits_a_burst_up_to_its_size_and_then_throttles_to_the_refill_rate() -> None:
    """The whole point of a bucket beside a window. Delete this and a bucket that admitted
    one call at a time, or one that never refused at all, would both pass every other test
    here: the burst is the behaviour, and the throttle afterwards is what makes it safe."""
    bucket = bucket_for("xero", now=T0)
    assert bucket.capacity == XERO_BURST

    burst, empty = _drain(bucket, T0)
    assert burst == int(XERO_BURST)

    refused = empty.take(T0)
    assert refused.admitted is False
    assert refused.retry_after_seconds == pytest.approx(1.0 / XERO_RATE)

    # And from here it is one call per refill interval, not another burst.
    current = empty
    for second in range(1, 6):
        at = T0 + timedelta(seconds=second)
        first = current.take(at)
        assert first.admitted is True, f"the token earned by second {second} was not admitted"
        second_at_the_same_instant = first.bucket.take(at)
        assert second_at_the_same_instant.admitted is False
        current = first.bucket


def test_the_retry_hint_is_the_measured_time_until_the_bucket_has_room() -> None:
    """A guessed hint sends a client back before there is a token for it, which produces a
    second refusal that looks like the limiter misbehaving. Delete this and the hint could
    become a constant and every other test would still pass."""
    empty = bucket_for("xero", now=T0).take(T0, cost=XERO_BURST).bucket

    refused = empty.take(T0, cost=4.0)

    assert refused.admitted is False
    assert refused.retry_after_seconds == pytest.approx(4.0 / XERO_RATE)


def test_a_refused_take_spends_nothing() -> None:
    """Charging for a refusal makes an eager client's wait grow every time it retries, so the
    hint it was handed is false the moment it acts on it.

    The balance is deliberately *partial* rather than empty, and that is the whole reason
    this test is shaped the way it is. An earlier version drained the bucket to nothing
    first, and a bucket that spent on the refusal path but clamped at zero was
    indistinguishable there: it survived the mutation. Only a refusal against a balance that
    has something in it can see a token being taken.

    Delete this and a bucket that decremented on the refusal path passes every other test in
    this file."""
    at = T0 + timedelta(seconds=3)
    partly = bucket_for("xero", now=T0).take(T0, cost=XERO_BURST).bucket.refilled(at)
    assert partly.tokens == pytest.approx(3.0)

    for _attempt in range(20):
        refused = partly.take(at, cost=4.0)
        assert refused.admitted is False
        assert refused.bucket.tokens == pytest.approx(3.0)
        assert refused.retry_after_seconds == pytest.approx(1.0 / XERO_RATE)
        partly = refused.bucket

    # Twenty refusals later the fourth token arrives exactly when the first hint said it
    # would, rather than a further twenty seconds away.
    assert partly.take(at + timedelta(seconds=1), cost=4.0).admitted is True


def test_a_bucket_does_not_refill_past_its_capacity() -> None:
    """A connector nobody called all night must not answer with a day's worth of burst at
    nine in the morning. Delete this and the clamp could go, turning an idle weekend into a
    burst of 170,000 calls the first time anybody asked a question."""
    bucket = bucket_for("xero", now=T0)
    spent = bucket.take(T0, cost=XERO_BURST).bucket

    rested = spent.refilled(T0 + timedelta(hours=12))

    assert rested.tokens == XERO_BURST


def test_a_clock_that_goes_backwards_adds_no_tokens() -> None:
    """`now` is passed in, so a replayed or skewed instant is not hypothetical. Delete this
    and a caller could refill a bucket by handing it an earlier time, twice."""
    bucket = bucket_for("xero", now=T0)
    spent = bucket.take(T0, cost=XERO_BURST).bucket

    rewound = spent.refilled(T0 - timedelta(minutes=5))

    assert rewound.tokens == pytest.approx(0.0)
    assert rewound.updated_at == T0


# ------------------------------------------------------- the rate comes from the ceiling
def test_the_refill_rate_is_derived_from_the_verified_ceiling() -> None:
    """The positive half of the refusal below: a guard tested only by what it refuses is
    satisfied by a function that refuses everything. Delete this and `bucket_for` could
    return a bucket paced at any rate at all as long as it was slow."""
    for connector in ("xero", "freshdesk", "lark_base"):
        ceiling = connector_ceiling(connector)
        assert ceiling is not None
        bucket = bucket_for(connector, now=T0)

        assert bucket.refill_per_second == pytest.approx(ceiling.per_minute / 60.0)
        assert bucket.capacity == pytest.approx(
            min(float(ceiling.per_minute), bucket.refill_per_second * DEFAULT_BURST_SECONDS)
        )


def test_a_bucket_cannot_be_configured_to_refill_faster_than_the_declared_ceiling() -> None:
    """Lark Base is 100 a minute and their documentation says it cannot be raised, so a
    bucket refilling faster than that gets the client 429ed by their own supplier while
    reading in our console as a deliberate setting.

    Delete this and the check on the constructor is the only thing left, which nothing would
    notice being removed: every bucket the platform builds comes from `bucket_for` and is
    already at the ceiling."""
    ceiling = connector_ceiling("lark_base")
    assert ceiling is not None
    allowed = sustained_per_second(ceiling)

    with pytest.raises(TokenBucketError, match="verified ceiling"):
        TokenBucket(
            connector="lark_base",
            capacity=16.0,
            refill_per_second=allowed * 2,
            tokens=16.0,
            updated_at=T0,
        )

    # Exactly at the ceiling is allowed, which is what makes the refusal about being faster
    # rather than about being configured at all.
    at_the_ceiling = TokenBucket(
        connector="lark_base",
        capacity=16.0,
        refill_per_second=allowed,
        tokens=16.0,
        updated_at=T0,
    )
    assert at_the_ceiling.refill_per_second == allowed


def test_a_refilled_bucket_is_rechecked_against_the_ceiling() -> None:
    """Every bucket after the first is made by `dataclasses.replace`, so a check that ran
    only on the first construction would be a check the running system never applies. Delete
    this and moving the guard out of `__post_init__` would pass the suite."""
    spent = bucket_for("lark_base", now=T0).take(T0).bucket

    with pytest.raises(TokenBucketError, match="verified ceiling"):
        TokenBucket(
            connector=spent.connector,
            capacity=spent.capacity,
            refill_per_second=spent.refill_per_second * 3,
            tokens=spent.tokens,
            updated_at=spent.updated_at,
        )


def test_a_burst_larger_than_the_declared_window_is_refused() -> None:
    """A capacity above the whole minute ceiling breaches it on its own, whatever the refill
    rate is: the bucket would admit 200 calls into a window that permits 100. Delete this and
    `burst_seconds` becomes a way to buy a bigger allowance than the vendor sells."""
    with pytest.raises(TokenBucketError, match="larger than the whole declared window"):
        TokenBucket(
            connector="lark_base",
            capacity=200.0,
            refill_per_second=1.0,
            tokens=200.0,
            updated_at=T0,
        )


def test_an_unmeasured_connector_gets_no_bucket_rather_than_a_default() -> None:
    """Inventing a ceiling produces a number that looks measured, sits in a console beside
    three that are, and is wrong in whichever direction somebody guessed. Delete this and a
    connector nobody has verified would run at whatever default was convenient."""
    with pytest.raises(TokenBucketError, match="not one of the verified sources"):
        bucket_for("hubspot", now=T0)


def test_a_call_that_can_never_fit_is_refused_rather_than_waited_for() -> None:
    """A cost above the capacity has no arrival time, so the honest answer is a refusal
    rather than a hint nobody can act on. Delete this and such a call returns a wait that
    never comes true."""
    bucket = bucket_for("xero", now=T0)

    with pytest.raises(TokenBucketError, match="can never be"):
        bucket.take(T0, cost=XERO_BURST + 1)


# ---------------------------------------------------------------- one bucket per connector
def test_two_connectors_do_not_share_a_bucket() -> None:
    """A shared pool lets a Xero backfill pace every question against Lark Base, whose
    ceiling is a different number from a different vendor, and lets a generous source top up
    a pool a strict one is drawing from.

    Delete this and keying the state by anything else, or by nothing, passes every other test
    in this file."""
    state = BucketState()

    at = T0
    for _call in range(int(XERO_BURST)):
        decision, state = state.take("xero", now=at)
        assert decision.admitted is True

    exhausted, state = state.take("xero", now=at)
    assert exhausted.admitted is False

    untouched, state = state.take("lark_base", now=at)
    assert untouched.admitted is True
    assert state.tokens_in("xero") == pytest.approx(0.0)
    assert state.tokens_in("lark_base") > 0.0

    # And the two are paced by their own ceilings rather than by a shared one.
    assert state.buckets["xero"].refill_per_second != state.buckets["lark_base"].refill_per_second


def test_a_bucket_is_made_full_on_first_use() -> None:
    """A connector nobody has called has spent nothing, so starting it empty makes the first
    question of the morning wait for an allowance that is entirely unused. Delete this and
    starting empty would pass, and would be noticed only as the platform feeling slow after
    every deploy."""
    state = BucketState()

    decision, _state = state.take("xero", now=T0)

    assert decision.admitted is True
    assert decision.bucket.tokens == pytest.approx(XERO_BURST - 1.0)


# ------------------------------------------------ why the window and the bucket both exist
def test_a_window_admits_a_burst_that_a_bucket_paces() -> None:
    """The reason both exist, as a fact rather than as a paragraph.

    Ten Xero calls inside one second are comfortably inside every sliding window that governs
    them, because a window counts a minute and cannot see shape. The bucket refuses the
    eleventh, because it counts shape and cannot see a minute.

    Delete this and the next reader deletes one of the two modules on the grounds that the
    other one already limits the connector, and whichever one goes takes its failure mode
    with it."""
    limits = source_limits("xero", principal_id="p_alice")
    state = LimiterState()
    bucket = bucket_for("xero", now=T0)

    for _call in range(int(XERO_BURST)):
        assert check(now=T0, limits=limits, state=state).allowed is True
        state = state.record(T0, limits)
        bucket = bucket.take(T0).bucket

    # The window still has room: ten calls is a sixth of Xero's minute.
    assert check(now=T0, limits=limits, state=state).allowed is True
    # The bucket does not, because all ten arrived in the same instant.
    assert bucket.take(T0).admitted is False
