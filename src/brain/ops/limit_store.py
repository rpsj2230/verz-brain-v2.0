"""Where a sliding window lives between two requests.

`brain.ops.limits` is the algorithm and holds no connection, deliberately: a limiter that
owned a client could not be tested for the boundary between two windows, which is the only
part of a sliding window that is ever wrong. That leaves the gap this module fills. Nothing
here re-decides whether a request may proceed. It loads the windows, hands them to
`limits.check`, and writes back what that decision admitted.

**The algorithm is not reimplemented in Lua, and that is the central choice here.** The
obvious way to make a distributed rate limiter atomic is a server-side script that prunes,
counts, compares and appends in one round trip. It is faster, and it is a second
implementation of the rule, in a second language, that no type checker and no test in this
repository reads. The rule is already subtle: a limit lowered while requests are in flight,
a window that is exactly full, the difference between a refusal and an admitted request.
Two copies of that drift, and the copy that drifts is the one running in production.

So the atomicity comes from optimistic concurrency instead. The keys are watched, the state
is read, `limits.check` decides in Python, and the write is a transaction that fails if any
watched key moved. Under contention a request retries; without contention it costs one
extra round trip over the scripted version. That is the trade, stated plainly: a little
tail latency in exchange for the decision existing exactly once.

**A window is a sorted set whose members are opaque and whose scores are the time.** The
tempting encoding is the timestamp as the member, which is smaller and needs no uuid. It is
also wrong: `ZADD` replaces a member that already exists, so two requests bearing the same
instant collapse into one recorded hit and the limit admits one more than it says. Callers
pass `now` in from outside, so identical instants are not hypothetical: a batch, a test
fixture, or any clock with coarse resolution produces them. `_member` is therefore unique
per call and carries no meaning at all.

**Two different keys can never render to the same string.** A key is a scope, a subject and
a period, and subjects are principal ids, connector names and widget origins, which is to
say strings from outside. Joining them with a colon lets `("a:b", "c")` and `("a", "b:c")`
address the same window, which is one caller spending another caller's allowance. Every
segment is percent-encoded so the separator cannot occur inside one.

**What happens when the store is unreachable is decided per scope, and every scope must
say.** A fairness limit between colleagues fails open: Valkey being down should not take the
product down for a rule whose worst case is that one person is briefly unfair. A limit
guarding a connector fails closed, because the resource it protects is outside this system.
Xero is 5,000 calls a day per tenant, shared with every other integration the client runs;
overrunning it does not degrade us, it breaks their finance team's other tools until
midnight, and nothing we operate can give the calls back. `UNREACHABLE_POLICY` is exhaustive
over `LimitScope` and tested to be, so a new scope cannot be added without somebody choosing.

**A refusal caused by an outage is not a quota refusal.** `RefusalKind.DEPENDENCY` exists
for exactly this: an operator told "quota" goes looking for a limit to raise, and no limit
was reached. The distinction is the whole reason those kinds are separate strings.

Task ids: M23.1.1
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, Protocol, cast
from urllib.parse import quote

import structlog
from redis.exceptions import RedisError, WatchError

from brain.ops.admission import RefusalKind, refusal_record
from brain.ops.limits import (
    Limit,
    LimitDecision,
    LimiterState,
    LimitKey,
    LimitScope,
    WindowState,
    check,
)

log = structlog.get_logger()

#: Namespaces every key this module writes. The entitlement cache and the answer store live
#: in the same Valkey, and a limiter key colliding with a cache key would be read back as a
#: cached entitlement set, which fails as a parse error rather than as a wrong answer only
#: because that store validates what it reads.
KEY_PREFIX: Final = "lim"

#: How many times a request retries after losing the optimistic race. Four rather than
#: unbounded: a caller that keeps losing is a caller under real contention, and spinning
#: there converts a rate limit into a source of load. The fifth loss is reported as a
#: dependency refusal, which is honest, because the store could not answer in time.
MAX_ATTEMPTS: Final = 4

#: Added to every key's expiry on top of its window. A key that expires early loses the tail
#: of its window, and a window missing its oldest hits admits more than the limit says,
#: which is the failure that looks like the limiter working. Slack covers clock skew between
#: this process and the server holding the key.
TTL_SLACK_SECONDS: Final = 5

#: What a caller is told to wait after a dependency refusal. Not derived from a window,
#: because there is no window in hand to derive it from; saying "retry immediately" would
#: turn an outage into a retry storm against the thing that is down.
OUTAGE_RETRY_SECONDS: Final = 5.0


class Availability(enum.StrEnum):
    """What to do with a limit whose window could not be read."""

    #: Admit. The allowance protects something inside this system.
    FAIL_OPEN = "fail_open"
    #: Refuse. The allowance protects something outside it, that we cannot give back.
    FAIL_CLOSED = "fail_closed"


#: Exhaustive over `LimitScope`, and there is a test whose only job is to keep it that way.
#: A missing entry would default to one behaviour or the other by accident, and the accident
#: nobody notices is the open one.
UNREACHABLE_POLICY: Mapping[LimitScope, Availability] = MappingProxyType(
    {
        LimitScope.PRINCIPAL: Availability.FAIL_OPEN,
        LimitScope.CHANNEL: Availability.FAIL_OPEN,
        LimitScope.AGENT: Availability.FAIL_OPEN,
        LimitScope.WIDGET_ORIGIN: Availability.FAIL_OPEN,
        # Both connector scopes protect the same external ceiling. The per-principal share
        # exists to stop one caller taking all of it, and with the windows unreadable there
        # is no way to tell whether they already have.
        LimitScope.CONNECTOR: Availability.FAIL_CLOSED,
        LimitScope.PRINCIPAL_CONNECTOR: Availability.FAIL_CLOSED,
    }
)


def _segment(value: str) -> str:
    """One key segment, with the separator made impossible rather than discouraged.

    The property is injectivity, and it comes from escaping the colon and the percent sign,
    which `quote` does at any `safe` setting that does not name them. `safe=""` is therefore
    conservatism rather than the load-bearing part: it costs nothing and leaves no character
    to reason about later.

    That is a correction. This docstring first claimed `safe=""` was what kept a widget
    origin's slashes out of the key, and mutating it to the default proved the claim false:
    keys split on the colon, so a surviving slash forges nothing. Only the colon matters,
    and it is escaped either way.
    """
    return quote(value, safe="")


def render_key(key: LimitKey) -> str:
    """The Valkey key for one window. Injective: distinct keys give distinct strings."""
    scope, subject, period = key
    return ":".join((KEY_PREFIX, _segment(str(scope)), _segment(subject), _segment(period)))


def _member() -> str:
    """A unique, meaningless member so two hits at one instant stay two hits.

    Meaningless on purpose. A member carrying a principal id or a trace id would put an
    identifier in a store with its own retention, reachable by anything that can range over
    the key.
    """
    return uuid.uuid4().hex


class WindowPipeline(Protocol):
    """The commands one optimistic transaction needs, and not one more.

    `redis.client.Pipeline` satisfies this structurally. Narrow because it is also the seam
    the tests run against, and a fake with six methods is a fake nobody makes clever.
    """

    def __enter__(self) -> WindowPipeline: ...
    def __exit__(self, *exc: object) -> None: ...
    def watch(self, *names: str) -> object: ...
    def multi(self) -> None: ...
    def execute(self) -> list[Any]: ...
    def zrange(self, name: str, start: int, end: int, *, withscores: bool = False) -> Any: ...
    # `min` and `max` shadow builtins, and are spelled that way because redis-py spells
    # them that way. A protocol that renamed them would stop describing the object it
    # exists to describe, and the rename would be invisible until a caller passed one by
    # keyword against the real client.
    def zremrangebyscore(self, name: str, min: Any, max: Any) -> object: ...  # noqa: A002
    def zadd(self, name: str, mapping: Mapping[str, float]) -> object: ...
    def expire(self, name: str, time: int) -> object: ...


class WindowClient(Protocol):
    """Whatever hands out pipelines. `redis.Redis` and `valkey.Valkey` both do."""

    def pipeline(self) -> WindowPipeline: ...


@dataclass
class StoreHealth:
    """Numbers an operator reads, carrying no key and no subject.

    Counted apart because they mean different things. `outages` is the store being
    unreachable, which is somebody's pager. `contention` is the optimistic transaction
    losing and retrying, which is normal under load and only interesting as a rate. `spins`
    is a request that exhausted its retries, which is contention that stopped being normal.
    """

    checks: int = 0
    outages: int = 0
    contention: int = 0
    spins: int = 0
    fail_open: int = 0
    fail_closed: int = 0


@dataclass(frozen=True)
class StoreVerdict:
    """A decision, plus whether it was made with the windows actually in hand.

    The flag is not cosmetic. A refusal reached because a connector's window was unreadable
    must not arrive at an operator as a quota incident: there is no limit to raise, and the
    person who goes looking for one has been sent to the wrong system. `log_record` is the
    only place that difference is expressed, so it is expressed once.
    """

    decision: LimitDecision
    degraded: bool = False
    #: Which scopes could not be read. Scopes, never subjects: an outage line naming
    #: principals is a list of who was active during the outage.
    unreadable: tuple[LimitScope, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    def log_record(self) -> Mapping[str, str]:
        if not self.degraded:
            return self.decision.log_record()
        if self.decision.allowed:
            return MappingProxyType(
                {"verdict": "allowed", "degraded": "true", "detail": "windows unreadable"}
            )
        return refusal_record(
            RefusalKind.DEPENDENCY,
            subject="limit_store",
            detail="the sliding windows could not be read; connector ceilings fail closed",
        )


def _state_from(rows: Mapping[LimitKey, Sequence[tuple[Any, float]]]) -> LimiterState:
    """Rebuild `LimiterState` from what the store returned.

    The score is the time and the member is discarded. Scores come back as floats, so the
    reconstructed instants are equal to about the microsecond rather than identical; that is
    enough, because what the algorithm reads is the count, and the count is the number of
    members rather than the number of distinct instants.
    """
    windows: dict[LimitKey, WindowState] = {}
    for key, pairs in rows.items():
        hits = tuple(
            sorted(datetime.fromtimestamp(float(score), tz=UTC) for _unused, score in pairs)
        )
        windows[key] = WindowState(hits=hits)
    return LimiterState(windows=MappingProxyType(windows))


@dataclass
class ValkeyWindowStore:
    """The sliding windows of `brain.ops.limits`, over Valkey.

    Holds no policy. Which limits apply is `limits.request_limits`; whether they admit is
    `limits.check`; what a caller is told is `LimitDecision`. This object loads, calls and
    writes.
    """

    client: WindowClient
    health: StoreHealth = field(default_factory=StoreHealth)

    def check_and_record(self, *, now: datetime, limits: Sequence[Limit]) -> StoreVerdict:
        """Decide, and record the hit if it was admitted. One transaction, or none.

        The read and the write are in one watched transaction because they are one decision.
        Reading, deciding and writing without watching is the classic double admit: two
        requests both see a window with room, both write, and the limit admits one more than
        it says exactly when it matters, which is under load.
        """
        self.health.checks += 1
        if not limits:
            # No limits govern this, so there is nothing to read and nothing to record. Not
            # an outage and not a degraded answer: an empty allowance list means unlimited
            # by configuration, and `check` says so on an empty state.
            return StoreVerdict(check(now=now, limits=(), state=LimiterState()))

        keys = [render_key(limit.key) for limit in limits]
        for attempt in range(MAX_ATTEMPTS + 1):
            try:
                with self.client.pipeline() as pipe:
                    pipe.watch(*keys)
                    rows = self._read(pipe, now, limits)
                    decision = check(now=now, limits=limits, state=_state_from(rows))
                    pipe.multi()
                    if decision.allowed:
                        self._record(pipe, now, limits)
                    # Executed even when nothing was queued. An empty transaction still
                    # releases the watch, and leaving keys watched on a pooled connection
                    # leaks the watch into whatever that connection does next.
                    pipe.execute()
                    return StoreVerdict(decision)
            except WatchError:
                self.health.contention += 1
                if attempt >= MAX_ATTEMPTS:
                    self.health.spins += 1
                    log.warning("limit store contended out", attempts=attempt + 1)
                    return self._degraded(now, limits, reason="contention")
                continue
            except (RedisError, OSError) as exc:
                self.health.outages += 1
                # No key is logged. A limiter key is a principal id with a prefix on it.
                log.warning("limit store unreachable", error=type(exc).__name__)
                return self._degraded(now, limits, reason="unreachable")
        # Not reachable: every path in the loop returns or continues, and the last iteration
        # returns. Present so a future edit to the bounds cannot fall through to None.
        msg = "the retry loop fell through"
        raise AssertionError(msg)

    def _read(
        self, pipe: WindowPipeline, now: datetime, limits: Sequence[Limit]
    ) -> dict[LimitKey, list[tuple[Any, float]]]:
        """Every watched window, pruned by the server as it is read.

        Pruned here rather than left to the expiry: a key's TTL covers the whole key, so a
        long window holds hits that fell out of a short one over the same subject.
        `limits.WindowState.pruned` would drop them anyway; doing it here keeps the set from
        growing without bound while a subject stays busy.
        """
        rows: dict[LimitKey, list[tuple[Any, float]]] = {}
        for limit in limits:
            name = render_key(limit.key)
            cutoff = now.timestamp() - limit.window_seconds
            pipe.zremrangebyscore(name, "-inf", cutoff)
            rows[limit.key] = list(pipe.zrange(name, 0, -1, withscores=True))
        return rows

    def _record(self, pipe: WindowPipeline, now: datetime, limits: Sequence[Limit]) -> None:
        """Record the admitted request against every limit that governed it.

        All of them, mirroring `LimiterState.record`: a request that consumed a Xero call
        consumed it from the connector's minute, the connector's day and the caller's share,
        and recording only one of those makes the other two drift until they mean nothing.
        """
        for limit in limits:
            name = render_key(limit.key)
            pipe.zadd(name, {_member(): now.timestamp()})
            pipe.expire(name, int(limit.window_seconds) + TTL_SLACK_SECONDS)

    def _degraded(self, now: datetime, limits: Sequence[Limit], *, reason: str) -> StoreVerdict:
        """The answer when the windows are not in hand, decided per scope.

        Fail closed wins over fail open when both are present, and there is no arithmetic to
        it: the caller is about to spend somebody else's external ceiling and nothing here
        can tell how much of it is left.
        """
        closed = tuple(
            limit for limit in limits if UNREACHABLE_POLICY[limit.scope] is Availability.FAIL_CLOSED
        )
        scopes = tuple(dict.fromkeys(limit.scope for limit in limits))
        if not closed:
            self.health.fail_open += 1
            return StoreVerdict(
                check(now=now, limits=(), state=LimiterState()),
                degraded=True,
                unreadable=scopes,
            )
        self.health.fail_closed += 1
        return StoreVerdict(
            LimitDecision(
                allowed=False,
                binding=closed[0],
                retry_after_seconds=OUTAGE_RETRY_SECONDS,
                reason=(
                    f"the limit store is unreachable ({reason}); "
                    f"{closed[0].scope} allowances fail closed"
                ),
                over=closed,
            ),
            degraded=True,
            unreadable=scopes,
        )


def make_store(client: object) -> ValkeyWindowStore:
    """Wrap a `redis.Redis` (or a `valkey.Valkey`) as a window store.

    A cast for the same reason `cache.make_client` casts: the client satisfies the protocol
    structurally, and asking mypy to prove that about a library's overloaded signatures buys
    nothing the protocol does not already state.
    """
    return ValkeyWindowStore(client=cast(WindowClient, client))
