"""The sliding windows, over a store that can be slow, contended or absent.

Every test here is a way the window in Valkey stops matching the window the algorithm
believes in. The algorithm itself is tested in `test_limits.py` against an in-memory state;
nothing is re-tested here, because a second copy of those assertions would be a second place
to update when the rule changes.

The fake implements `WindowPipeline` and keeps sorted sets in a dict. Deliberately literal:
it replaces a member on a repeated `zadd` exactly as Valkey does, which is what makes the
"two hits at one instant" test able to fail. A fake that appended blindly would prove the
fake appends.

Task ids: M23.1.1
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import WatchError

from brain.ops.admission import RefusalKind
from brain.ops.limit_store import (
    KEY_PREFIX,
    MAX_ATTEMPTS,
    TTL_SLACK_SECONDS,
    UNREACHABLE_POLICY,
    Availability,
    StoreVerdict,
    ValkeyWindowStore,
    render_key,
)
from brain.ops.limits import Limit, LimitScope

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def per_principal(limit: int = 2, window: float = 60.0) -> Limit:
    return Limit(
        scope=LimitScope.PRINCIPAL,
        subject="p_alice",
        period="minute",
        limit=limit,
        window_seconds=window,
    )


def per_connector(limit: int = 2, window: float = 60.0) -> Limit:
    return Limit(
        scope=LimitScope.CONNECTOR,
        subject="xero",
        period="minute",
        limit=limit,
        window_seconds=window,
    )


class FakePipeline:
    """A sorted set per key, with Valkey's replace-on-duplicate-member behaviour."""

    def __init__(self, store: FakeClient) -> None:
        self.store = store

    def __enter__(self) -> FakePipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self.store.watched = ()

    def watch(self, *names: str) -> object:
        self.store.watched = names
        # Kept after the block exits. `__exit__` releases the watch, as the real pipeline
        # does, so an assertion made afterwards has to read what *was* watched.
        self.store.ever_watched = names
        self.store.watch_calls += 1
        return None

    def multi(self) -> None:
        self.store.multi_calls += 1

    def execute(self) -> list[Any]:
        self.store.execute_calls += 1
        if self.store.conflict_for > 0:
            self.store.conflict_for -= 1
            raise WatchError("a watched key moved")
        return []

    def zrange(self, name: str, start: int, end: int, *, withscores: bool = False) -> Any:
        del start, end, withscores
        return sorted(self.store.sets.get(name, {}).items(), key=lambda pair: pair[1])

    def zremrangebyscore(self, name: str, min: Any, max: Any) -> object:  # noqa: A002
        del min
        members = self.store.sets.get(name, {})
        for member, score in list(members.items()):
            if score <= float(max):
                del members[member]
        return None

    def zadd(self, name: str, mapping: Mapping[str, float]) -> object:
        self.store.sets.setdefault(name, {}).update(mapping)
        return None

    def expire(self, name: str, time: int) -> object:
        self.store.ttls[name] = time
        return None


class FakeClient:
    def __init__(self, *, conflict_for: int = 0, raises: Exception | None = None) -> None:
        self.sets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}
        self.watched: tuple[str, ...] = ()
        self.ever_watched: tuple[str, ...] = ()
        self.conflict_for = conflict_for
        self.raises = raises
        self.watch_calls = 0
        self.multi_calls = 0
        self.execute_calls = 0

    def pipeline(self) -> FakePipeline:
        if self.raises is not None:
            raise self.raises
        return FakePipeline(self)


def store_with(**kwargs: Any) -> tuple[ValkeyWindowStore, FakeClient]:
    client = FakeClient(**kwargs)
    return ValkeyWindowStore(client=client), client


# ----------------------------------------------------------------- it counts at all
def test_a_request_under_the_limit_is_admitted_and_recorded() -> None:
    """If this fails every refusal below passes for the wrong reason: a store that admits
    nothing and records nothing satisfies most of this file."""
    store, client = store_with()
    limits: Sequence[Limit] = (per_principal(limit=2),)

    first = store.check_and_record(now=NOW, limits=limits)

    assert first.allowed
    assert len(client.sets[render_key(limits[0].key)]) == 1


def test_the_request_that_crosses_the_limit_is_refused() -> None:
    """The window has to survive between calls. Delete this and a store that writes to a
    fresh dict every time still passes the test above."""
    store, _ = store_with()
    limits = (per_principal(limit=2),)

    assert store.check_and_record(now=NOW, limits=limits).allowed
    assert store.check_and_record(now=NOW, limits=limits).allowed
    refused = store.check_and_record(now=NOW, limits=limits)

    assert not refused.allowed
    assert refused.decision.retry_after_seconds > 0


def test_a_refused_request_is_not_recorded() -> None:
    """`REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW` is a rule in the algorithm, and this is
    the half of it that lives in the store: the transaction must queue no write when the
    decision was no.

    Without this a client that retries eagerly is locked out permanently, with the retry
    hint receding faster than the client can obey it."""
    store, client = store_with()
    limits = (per_principal(limit=1),)
    key = render_key(limits[0].key)

    store.check_and_record(now=NOW, limits=limits)
    store.check_and_record(now=NOW, limits=limits)
    store.check_and_record(now=NOW, limits=limits)

    assert len(client.sets[key]) == 1, "a refusal was recorded and extended the window"


def test_two_requests_at_the_very_same_instant_count_as_two() -> None:
    """The reason a member is a uuid and not the timestamp.

    Valkey's `ZADD` replaces a member that already exists, so encoding the instant as the
    member makes two simultaneous requests collapse into one recorded hit, and a limit of
    two admits three. `now` comes from the caller, so identical instants are ordinary rather
    than exotic: a batch, or any clock with coarse resolution, produces them.

    Delete this and the smaller encoding looks correct in every other test here, because
    every other test uses distinct instants or does not care."""
    store, client = store_with()
    limits = (per_principal(limit=5),)
    key = render_key(limits[0].key)

    store.check_and_record(now=NOW, limits=limits)
    store.check_and_record(now=NOW, limits=limits)

    assert len(client.sets[key]) == 2, "two hits at one instant collapsed into one"


def test_a_hit_that_has_fallen_out_of_the_window_stops_counting() -> None:
    """The boundary between two windows, which is the only part of a sliding window that is
    ever wrong, exercised through the store rather than the algorithm."""
    store, _ = store_with()
    limits = (per_principal(limit=1, window=60.0),)

    assert store.check_and_record(now=NOW, limits=limits).allowed
    assert not store.check_and_record(now=NOW + timedelta(seconds=30), limits=limits).allowed
    assert store.check_and_record(now=NOW + timedelta(seconds=61), limits=limits).allowed


# -------------------------------------------------------------------- the key itself
def test_two_different_windows_can_never_share_a_key() -> None:
    """A subject is a principal id, a connector name or a widget origin: a string from
    outside. Joined with a colon, `("a:b", "c")` and `("a", "b:c")` address one window, and
    one caller spends another caller's allowance.

    Delete this and the naive join reads correctly, because every other subject in this file
    is a plain identifier."""
    ambiguous = render_key((LimitScope.PRINCIPAL, "a:b", "c"))
    other = render_key((LimitScope.PRINCIPAL, "a", "b:c"))

    assert ambiguous != other


def test_a_widget_origin_with_a_url_in_it_still_makes_one_key_segment() -> None:
    """A widget origin is a URL, and a URL contains the separator. `https://app...` joined
    raw would render as five colon-separated fields where the key format has four, so the
    scope and period a reader parses back out are not the ones that went in.

    The slash is not the point and this test does not claim it is: mutating `safe=""` to
    the default leaves the slash unencoded and changes nothing, because keys split on the
    colon. Delete this and only the artificial `a:b` case above covers the real subject
    shape that carries a separator."""
    key = render_key((LimitScope.WIDGET_ORIGIN, "https://app.example.com/embed", "minute"))

    assert key.count(":") == 3, "a subject leaked separators into the key"
    assert key.startswith(f"{KEY_PREFIX}:")


def test_every_key_is_given_an_expiry_at_least_as_long_as_its_window() -> None:
    """A key that expires early loses the oldest hits in its window, so the limit admits
    more than it says, and it does so silently. This is the failure that looks like the
    limiter working."""
    store, client = store_with()
    limits = (per_principal(window=60.0),)

    store.check_and_record(now=NOW, limits=limits)

    assert client.ttls[render_key(limits[0].key)] >= 60 + TTL_SLACK_SECONDS - 1


# ----------------------------------------------------------- concurrency and outages
def test_the_keys_are_watched_before_they_are_read() -> None:
    """Reading, deciding and writing without a watch is the double admit: two requests both
    see room, both write, and the limit admits one more than it says exactly under load.

    Asserted as a call rather than as an outcome, because a single-threaded test cannot
    observe the race it prevents. This is the one place in this file where the mechanism is
    the assertion, and that is why the fake counts the calls."""
    store, client = store_with()

    store.check_and_record(now=NOW, limits=(per_principal(),))

    assert client.watch_calls == 1
    assert client.ever_watched == (render_key(per_principal().key),)


def test_a_lost_race_is_retried_rather_than_reported() -> None:
    """Losing the optimistic transaction means somebody else wrote first, not that the
    caller is over their limit. Reporting it would refuse a request that was inside its
    allowance, and the caller would have no way to tell the two apart."""
    store, client = store_with(conflict_for=2)

    verdict = store.check_and_record(now=NOW, limits=(per_principal(limit=5),))

    assert verdict.allowed
    assert not verdict.degraded
    assert store.health.contention == 2
    assert client.execute_calls == 3


def test_endless_contention_is_a_dependency_refusal_and_not_a_quota_one() -> None:
    """A caller that never wins the race is not over an allowance. Told "quota", an operator
    goes looking for a limit to raise and finds one that was never reached.

    The retries are bounded for the same reason: spinning turns a rate limit into a source
    of the load it exists to shed."""
    store, _ = store_with(conflict_for=MAX_ATTEMPTS + 1)

    verdict = store.check_and_record(now=NOW, limits=(per_connector(limit=5),))

    assert not verdict.allowed
    assert verdict.degraded
    assert store.health.spins == 1
    assert verdict.log_record()["refusal_kind"] == RefusalKind.DEPENDENCY


def test_a_fairness_limit_admits_when_the_store_is_unreachable() -> None:
    """Valkey being down must not take the product down for a rule whose worst case is that
    one colleague is briefly unfair to another."""
    store, _ = store_with(raises=RedisConnectionError("no route to host"))

    verdict = store.check_and_record(now=NOW, limits=(per_principal(),))

    assert verdict.allowed
    assert verdict.degraded, "an outage was reported as an ordinary admission"
    assert store.health.outages == 1


def test_a_connector_limit_refuses_when_the_store_is_unreachable() -> None:
    """The other half, and the one that costs something. Xero is 5,000 calls a day per
    tenant, shared with every other integration the client runs. Overrunning it does not
    degrade us; it breaks their finance team's other tools until midnight, and nothing we
    operate can give the calls back.

    Delete this and a Valkey outage quietly spends a resource we do not own."""
    store, _ = store_with(raises=RedisConnectionError("no route to host"))

    verdict = store.check_and_record(now=NOW, limits=(per_connector(),))

    assert not verdict.allowed
    assert verdict.degraded
    assert verdict.decision.retry_after_seconds > 0
    assert store.health.fail_closed == 1


def test_one_connector_limit_closes_a_request_that_also_carries_fairness_limits() -> None:
    """A real request carries both: a person's own rate and their share of a connector.
    Fail closed wins, because the caller is about to spend an external ceiling and nothing
    in hand can say how much of it is left.

    Delete this and the policy still passes both single-scope tests above while admitting
    every real mixed request during an outage."""
    store, _ = store_with(raises=RedisConnectionError("no route to host"))

    verdict = store.check_and_record(now=NOW, limits=(per_principal(), per_connector()))

    assert not verdict.allowed


def test_an_outage_line_names_scopes_and_never_subjects() -> None:
    """An outage report listing subjects is a list of who was active during the outage,
    written to whatever reads operator logs and kept for as long as those are."""
    store, _ = store_with(raises=RedisConnectionError("down"))

    verdict = store.check_and_record(now=NOW, limits=(per_principal(), per_connector()))

    rendered = " ".join(f"{k}={v}" for k, v in verdict.log_record().items())
    assert "p_alice" not in rendered
    assert "xero" not in rendered


def test_every_limit_scope_says_what_happens_when_the_store_is_unreachable() -> None:
    """A scope with no entry would take one behaviour or the other by accident, and the
    accident nobody notices is the one that admits.

    This is the test that makes adding a `LimitScope` a decision rather than an edit."""
    assert set(UNREACHABLE_POLICY) == set(LimitScope)
    assert set(UNREACHABLE_POLICY.values()) == set(Availability)


def test_a_scope_that_guards_something_outside_this_system_fails_closed() -> None:
    """Spelled out as the rule rather than as the table, so that changing the table without
    changing the rule fails here.

    The distinction is not "important" versus "unimportant". It is whether the resource can
    be given back: capacity inside this system returns when the outage ends, and a spent
    Xero call does not."""
    for scope, availability in UNREACHABLE_POLICY.items():
        outside = "connector" in str(scope)
        expected = Availability.FAIL_CLOSED if outside else Availability.FAIL_OPEN
        assert availability is expected, f"{scope} has the wrong outage behaviour"


# ------------------------------------------------------------------- nothing to do
def test_a_request_governed_by_no_limits_touches_the_store_at_all_not() -> None:
    """An empty allowance list means unlimited by configuration, not unknown. Reading a
    window for it would put a key in Valkey for every unlimited caller, and reporting it as
    degraded would make an ordinary request look like an outage."""
    store, client = store_with()

    verdict = store.check_and_record(now=NOW, limits=())

    assert verdict.allowed
    assert not verdict.degraded
    assert client.watch_calls == 0


def test_an_admitted_request_is_recorded_against_every_limit_that_governed_it() -> None:
    """A request that consumed a Xero call consumed it from the connector's window and from
    the caller's share. Recording one of those makes the other drift until it means
    nothing, and the drift is silent for as long as nobody is near a ceiling."""
    store, client = store_with()
    limits = (per_principal(limit=5), per_connector(limit=5))

    store.check_and_record(now=NOW, limits=limits)

    for limit in limits:
        assert len(client.sets[render_key(limit.key)]) == 1


@pytest.mark.parametrize("degraded", [True, False])
def test_a_verdict_reports_the_same_answer_as_the_decision_it_wraps(degraded: bool) -> None:
    """`allowed` on the wrapper and `allowed` on the decision must never disagree; a caller
    reading the wrapper and an operator reading the decision would see different events."""
    store, _ = store_with(raises=RedisConnectionError("down") if degraded else None)
    verdict: StoreVerdict = store.check_and_record(now=NOW, limits=(per_principal(),))

    assert verdict.allowed is verdict.decision.allowed
