"""The client behind the two caches, and the version that makes one of them safe.

`brain.gate.resolve` and `brain.gate.answer_cache` are written against protocols and have
only ever run against fakes. That is the right shape: neither module should know what a
socket is, and every rule about what may be served lives there rather than here. It leaves
two gaps, and this module is both of them.

The first is that nothing connects a cache at all, so `valkey_url` is a setting that
configures nothing. The second is M1.4.5: `VersionSource` had no implementation, so the
version that `resolve.cache_key` puts in the key was never read, and the triggers `0003`
installs bumped a counter nobody looked at. A cache key carrying a version that nothing
reads is not invalidation; it is a comment.

**This module holds no policy.** `resolve` decides whether a cached set may be served,
`answer_cache` decides whether a cached answer is fresh, and `cache_key` decides what makes
two questions the same question. Nothing here re-decides any of that, and the reason is that
a rule implemented twice is a rule that will be changed once.

**A miss and an outage are different, and neither may become a wrong answer.** The protocols
say `get` returns `EntitlementSet | None`, and None means "not in hand". A cache that is
down also has nothing in hand, so both return None and `resolve` falls through to the
database, which is the behaviour that matters: an unreachable cache must slow the system
down, never change what it says. The two are still distinguished, because an operator needs
to tell a cold cache from a dead one: `CacheHealth` counts them separately, and a value that
came back and was refused is counted separately again, because that one is a security signal
rather than a capacity one.

There is no path here that returns a default. The tempting default is an empty entitlement
set, which looks safe and is not: an empty set is a legitimate value that flows onward, gets
hashed, and produces a confident "I could not find that" for somebody who should have seen
the record. Every failure in this module either returns None, meaning "ask the authority", or
raises.

**Nothing is deleted to invalidate, so there is no delete method.** The version is in the
entitlement key and the epochs are in the answer key; a bump orphans every key built from the
old value in the same instant, and an orphaned key cannot be read by accident because nobody
can construct it. A delete is the alternative and it fails in the way that matters: a delete
that does not arrive leaves a stale entry serving a revoked permission, and nothing anywhere
reports it. Adding one here would also add a second invalidation path, which is how the two
come to disagree. Expiry is set on every write because a key with no expiry outlives the
deployment that wrote it, but a TTL is the store reclaiming space and never the correctness
mechanism.

**Never pickle.** Both payloads are the pydantic models' own JSON. A pickle in a shared cache
is remote code execution the moment anything else can write to that cache, and something else
always can: a second service, a misconfigured `redis-cli`, an operator with the URL. JSON
cannot execute, so the worst a hostile writer achieves is a value that fails validation, which
is handled below as a miss.

**Values are checked as though they arrived from outside, because they did.** The bytes are
parsed under `try`, never trusted to be well formed, and a stored answer whose own key
disagrees with the key it was found under is refused.

**Every call has a timeout, and redis-py's defaults are not one.** Two facts, both measured
against redis 8.1 rather than read off the signature. It retries ten times with exponential
jitter backoff: against a closed port on this machine one `get` took 2.07 seconds to fail
with the defaults and 0.50 with the settings below. And `Redis.from_url` leaves both socket
timeouts at None, which is no timeout at all, where the `Redis(...)` constructor defaults
them to five seconds. The constructor's five seconds is what a reader expects and is not what
this code path gets, so a `from_url` client that is not given timeouts explicitly waits for
as long as the far end keeps the connection open. The closed-port measurement is therefore
the optimistic case: it is fast because the connection was refused, and a cache that accepts
and then stops answering is the one that hangs. A cache that hangs is worse than a cache that
is down, because the request waits instead of falling through.

**What the application should do when Valkey is unreachable.** Requests keep working and get
slower, because resolution falls through to `EntitlementStore`. Readiness is a separate
question and this module deliberately answers only half of it: `check_reachable` reports the
fact, and whoever wires `app.py` decides what it means. The lines are:

    app.state.valkey = make_client(settings.valkey_url)
    app.state.ready["cache"] = await check_reachable_async(app.state.valkey)

Marking the instance unready on a cache outage is the conservative reading of `app.py`'s own
rule that a half-connected instance must not serve. It is also how a cache that is optional by
design takes the whole fleet out of rotation at once, since every replica shares one Valkey.
That trade-off belongs to whoever owns the deployment, not to the cache client, so it is
stated here and left as a choice rather than made silently.

Task ids: M1.4.5
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import structlog
from pydantic import TypeAdapter, ValidationError
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from brain.core.entitlement import EntitlementSet
from brain.gate.cache_key import CachedAnswer
from brain.gate.resolve import ResolutionFailedError

log = structlog.get_logger()

#: How long to wait for a connection. Longer than the operation timeout because a connect
#: happens once per pooled connection and an operation happens on every request, so the
#: expensive one is the one to keep tight.
CONNECT_TIMEOUT_SECONDS: Final = 0.5

#: How long to wait for a reply. Valkey on the same host answers a `GET` in well under a
#: millisecond, so this is roughly three hundred times the expected cost. It is not sized to
#: the cache; it is sized to what falling through costs, which is one database query.
OPERATION_TIMEOUT_SECONDS: Final = 0.25

#: Zero retries, against a library default of ten with exponential jitter backoff. A retry
#: multiplies the wall-clock ceiling by the retry count while the fall-through path costs one
#: query, so retrying is paying seconds to avoid milliseconds. Valkey being down is not a
#: transient this module needs to paper over; it is a condition it is designed to survive.
RETRIES: Final = 0

#: Ping an idle pooled connection older than this before using it. A connection dropped by a
#: firewall or a NAT table on the shared host fails on first use, and without this the failure
#: surfaces as a cache miss on somebody's request rather than as a reconnect.
HEALTH_CHECK_INTERVAL_SECONDS: Final = 30

#: The ceiling on the version read. The query is a primary key lookup returning one small
#: integer; anything slower than this is a database in trouble, and waiting on it holds up a
#: request that has not started doing its real work yet.
STATEMENT_TIMEOUT_MS: Final = 250

#: Read `gate.grants_version`. A missing row is zero by design, so this is a plain select and
#: the coalesce happens in Python, where the difference between "no row" and "no answer" is
#: still visible. `COALESCE` in SQL would collapse them into one integer.
VERSION_SQL: Final = "SELECT version FROM gate.grants_version WHERE principal_id = %s"

#: `SET LOCAL statement_timeout` cannot take a bound parameter, and building the statement by
#: interpolation puts a value into SQL text for no reason. `set_config(..., is_local => true)`
#: is the same thing as a parameterised function call.
STATEMENT_TIMEOUT_SQL: Final = "SELECT set_config('statement_timeout', %s, true)"


class ValkeyClient(Protocol):
    """The three commands this module uses, and deliberately not one more.

    Narrow because it is also the seam the tests run against: a fake implementing three
    methods is a fake nobody is tempted to make clever. `redis.Redis` satisfies it
    structurally, and so does a `valkey.Valkey`, which is the point of the wire compatibility.

    There is no `delete` and no `flushdb` on purpose. See the module docstring: the absence is
    the invalidation design, and a method that exists is a method that gets called.
    """

    def get(self, name: str) -> bytes | None: ...
    def setex(self, name: str, time: int, value: bytes) -> object: ...
    def ping(self) -> object: ...


def make_client(
    url: str,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    operation_timeout: float = OPERATION_TIMEOUT_SECONDS,
) -> ValkeyClient:
    """A bounded client for `settings.valkey_url`. TLS is the URL's job (`rediss://`).

    Every timeout and the retry policy are passed explicitly rather than left at the library
    default, and that is the whole content of this function. See the module docstring for the
    measurements: ten retries with backoff, over a `from_url` client whose socket timeouts
    default to None. Unset is not a large timeout, it is no timeout, which is a hang dressed
    as resilience.

    `decode_responses` stays false. The payload is a JSON document that pydantic parses from
    bytes directly, so decoding it to `str` first is a wasted copy and moves a possible
    `UnicodeDecodeError` into the client, where this module cannot catch it and turn it into
    a miss.
    """
    return cast(
        ValkeyClient,
        Redis.from_url(
            url,
            socket_timeout=operation_timeout,
            socket_connect_timeout=connect_timeout,
            # Both halves are needed. `retry` bounds the attempts; `retry_on_error=[]` stops
            # command-level errors from being retried on top of that.
            retry=Retry(NoBackoff(), RETRIES),
            retry_on_error=[],
            health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
            decode_responses=False,
        ),
    )


@dataclass
class CacheHealth:
    """What happened, in numbers an operator can read, and no key or value anywhere.

    Three failure counters rather than one, because they mean different things. `outages` is
    Valkey being unreachable, which is capacity. `rejections` is Valkey answering with
    something this module refused, which is either corruption or another writer in the same
    keyspace, and is the one worth waking up for. `misses` is the cache working normally.

    `last_failure` holds an exception *class name* and never its message. A redis-py
    connection error can carry the URL it was built from, and a URL carries a password.
    """

    hits: int = 0
    misses: int = 0
    writes: int = 0
    outages: int = 0
    rejections: int = 0
    #: Reset by any success, so this answers "is it down now" rather than "was it ever".
    consecutive_outages: int = 0
    last_failure: str = ""

    @property
    def degraded(self) -> bool:
        """True while the last attempt failed. Answers are still correct, just slower."""
        return self.consecutive_outages > 0

    def record_ok(self) -> None:
        self.consecutive_outages = 0

    def record_outage(self, exc: BaseException) -> None:
        self.outages += 1
        self.consecutive_outages += 1
        self.last_failure = type(exc).__name__


class _ValkeyCache:
    """The byte-level half, shared by both caches because both failure modes are identical.

    Subclasses add serialisation and nothing else. Splitting it this way keeps exactly one
    place where an exception from the client is turned into "not in hand", which is the
    behaviour the whole module exists to guarantee.
    """

    #: Named in log lines so a reader can tell which cache degraded.
    name: str = "cache"

    def __init__(self, client: ValkeyClient, *, health: CacheHealth | None = None) -> None:
        self._client = client
        #: Injectable so a deployment can share one counter across both caches, or keep them
        #: apart. Default is per instance, which is the safer of the two to get wrong.
        self.health = health if health is not None else CacheHealth()

    def _read(self, key: str) -> bytes | None:
        """Bytes, or None for both "not there" and "could not ask".

        Broad on the exception, like `resolve` is around its store, and for the same reason:
        whatever redis-py raises must not leave this module. `OSError` is caught beside
        `RedisError` because a socket can fail before the client wraps it.
        """
        try:
            found = self._client.get(key)
        except (RedisError, OSError) as exc:
            self.health.record_outage(exc)
            # The key is not logged. `ent:<principal>:<version>` names a person, and a log
            # line is read by more people, for longer, than the cache entry ever was.
            log.warning("cache unreachable", cache=self.name, op="get", error=type(exc).__name__)
            return None
        self.health.record_ok()
        if found is None:
            self.health.misses += 1
        return found

    def _write(self, key: str, payload: bytes, ttl_seconds: int) -> None:
        """Store with an expiry, and never fail the request for it.

        A cache write failing after a successful load means the answer is already in hand;
        raising here would turn a slow request into a broken one. The TTL check is the
        exception, and it is not an outage: a non-positive TTL is a caller bug that Valkey
        would report as a command error, which this module would then swallow, leaving a cache
        that silently stores nothing.
        """
        if ttl_seconds <= 0:
            msg = f"{self.name} refuses a non-positive ttl ({ttl_seconds}); nothing would store"
            raise ValueError(msg)
        try:
            # SETEX rather than SET with an expiry argument, so there is no spelling of this
            # call that writes a key with no expiry at all.
            self._client.setex(key, ttl_seconds, payload)
        except (RedisError, OSError) as exc:
            self.health.record_outage(exc)
            log.warning("cache unreachable", cache=self.name, op="set", error=type(exc).__name__)
            return
        self.health.record_ok()
        self.health.writes += 1

    def _reject(self, reason: str) -> None:
        """A value came back and was refused. Counted apart from a miss, and logged.

        Neither the key nor the value is logged. The reason is a fixed string from this
        module, so the log line carries no data from the store.
        """
        self.health.rejections += 1
        log.warning("cache value refused", cache=self.name, reason=reason)


class ValkeyEntitlementCache(_ValkeyCache):
    """`brain.gate.resolve.EntitlementCache`, over Valkey.

    Keys arrive built by `resolve.cache_key` and are used verbatim. Nothing here prefixes or
    namespaces them, which was the obvious alternative and was rejected: the key *is* the
    invalidation token, and a client that stores under something other than the key the gate
    computed makes "was this entry orphaned by the version bump" a question about two pieces
    of code instead of one. A deployment that needs to share one Valkey with something else
    uses a separate logical database in the URL, which is configuration rather than code.

    There is no delete method. See the module docstring.
    """

    name = "entitlements"

    def get(self, key: str) -> EntitlementSet | None:
        """The set, or None. None on a miss, on an outage, and on anything unparseable.

        The stored bytes are validated rather than trusted. `resolve._usable` then checks the
        principal and the expiry on whatever comes back, and those checks are deliberately not
        repeated here: they are its job, they are tested there, and a second copy is a copy
        that gets to disagree. What is done here is the check `_usable` cannot do, because it
        needs an object to look at: refusing bytes that are not an `EntitlementSet` at all.
        """
        raw = self._read(key)
        if raw is None:
            return None
        try:
            found = EntitlementSet.model_validate_json(raw)
        except ValidationError:
            # Something else is writing to this keyspace, or a write was truncated. Either
            # way this is a miss, and the load below is the correct answer.
            self._reject("not an entitlement set")
            return None
        self.health.hits += 1
        return found

    def set(self, key: str, value: EntitlementSet, ttl_seconds: int) -> None:
        """Store the set as its own JSON. Expired sets included, as `resolve` requires."""
        self._write(key, value.model_dump_json().encode("utf-8"), ttl_seconds)


#: pydantic's serialiser for a frozen stdlib dataclass. `CachedAnswer` is not a `BaseModel`,
#: and the alternative was `json.dumps` over `dataclasses.asdict` with a hand-written
#: `datetime` round trip. That hand-written half is where a naive `stored_at` would come back
#: from a store that had a timezone going in, and `CachedAnswer.age` subtracts one datetime
#: from another, which raises on a naive/aware pair. A `TypeAdapter` is the same validation
#: the rest of the system already relies on.
_ANSWER: Final = TypeAdapter(CachedAnswer)


class ValkeyAnswerStore(_ValkeyCache):
    """`brain.gate.answer_cache.AnswerStore`, over Valkey.

    Keys arrive from `cache_key.key_for`, which refuses volatile questions outright, and are
    used verbatim for the reason given on the entitlement cache. A key here is a sha256
    digest with no structure, so it cannot collide with an `ent:` key even in a shared
    keyspace.

    There is no delete method, for the same reason as next door.
    """

    name = "answers"

    def get(self, key: str) -> CachedAnswer | None:
        """The answer, or None.

        The key check is not a duplicate of anything. `answer_cache.lookup` checks how old an
        answer is; this checks that the answer found under a key is the answer that was stored
        under it. They are different questions, and only the second one catches a store
        handing back the wrong entry, which in this cache means one person's answer reaching
        another person. Nothing else in the path would notice, because the answer is
        internally consistent; it just belongs to somebody else.
        """
        raw = self._read(key)
        if raw is None:
            return None
        try:
            found = _ANSWER.validate_json(raw)
        except ValidationError:
            self._reject("not a cached answer")
            return None
        if found.key != key:
            self._reject("answer stored under a different key")
            return None
        self.health.hits += 1
        return found

    def set(self, key: str, value: CachedAnswer, ttl_seconds: int) -> None:
        """Store the answer, refusing outright if it disagrees with its own key.

        Raising rather than dropping the write. A mismatch cannot be caused by data: only by
        code that built a `CachedAnswer` with one key and stored it under another, and
        `store_answer` cannot do that because it builds both from the same value. Dropping the
        write silently was the alternative, and it hides at write time the only bug this check
        exists to find, leaving it to be discovered on a read where it is unrecoverable.
        """
        if value.key != key:
            msg = f"{self.name} refuses an answer whose key field is not the key it is stored under"
            raise ValueError(msg)
        self._write(key, _ANSWER.dump_json(value), ttl_seconds)


class Cursor(Protocol):
    """The two calls a version read needs. A psycopg 3 cursor satisfies it."""

    def execute(self, query: str, params: Sequence[object] = ..., /) -> object: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...


class Connection(Protocol):
    """A connection that hands out cursors. A psycopg 3 connection satisfies it."""

    def cursor(self) -> AbstractContextManager[Cursor]: ...


class PostgresVersionSource:
    """`brain.gate.resolve.VersionSource`, over `gate.grants_version` (M1.4.5).

    Here rather than in `brain.gate.resolve`, which holds the protocol and nothing that opens
    a socket, and rather than in `brain.tables.gate`, which describes the schema and runs no
    queries. It reads on the same path and in the same breath as the cache it invalidates, so
    it lives with the cache client.

    **A failed read is never a version.** The one thing this class must not do is return a
    number when it does not know the number, and specifically not zero: zero is a real version,
    held by everybody who has never been granted anything, so a failure that returned zero
    would mint a key that was already used and may still have an entry under it. A missing row
    *is* zero, which `GrantsVersionRow` states and `0003` relies on to avoid seeding data, and
    that is a successful read of a row that does not exist. The two are one `if` apart and the
    consequences are not comparable.

    It raises `ResolutionFailedError`, which is `resolve`'s own failure type, rather than
    letting a psycopg exception out. `resolve` wraps `store.load` but not
    `versions.grants_version`, so anything raised here reaches the caller as it is, and a
    driver error carries a connection string in its message.

    The connection is supplied per call, so this is safe to hold across threads and works
    directly with `psycopg_pool.ConnectionPool.connection`.
    """

    def __init__(
        self,
        connect: Callable[[], AbstractContextManager[Connection]],
        *,
        statement_timeout_ms: int | None = STATEMENT_TIMEOUT_MS,
    ) -> None:
        self._connect = connect
        self._statement_timeout_ms = statement_timeout_ms

    def grants_version(self, principal_id: str) -> int:
        """The principal's current version. Zero when they have never held a grant.

        The id is a bound parameter, never interpolated, so it reaches the database as data
        and reaches no log line at all.
        """
        try:
            with self._connect() as connection, connection.cursor() as cur:
                if self._statement_timeout_ms is not None:
                    # `SET LOCAL` through `set_config`, so the bound is scoped to this
                    # transaction and cannot leak onto the next borrower of a pooled
                    # connection. It requires a transaction: on an autocommit connection
                    # PostgreSQL downgrades this to a warning and the read is unbounded, which
                    # is why psycopg's default of autocommit off is the supported shape and
                    # why a caller who sets `options=-c statement_timeout=...` on the
                    # connection string should pass None here rather than have both.
                    cur.execute(STATEMENT_TIMEOUT_SQL, (f"{self._statement_timeout_ms}ms",))
                cur.execute(VERSION_SQL, (principal_id,))
                row = cur.fetchone()
        except Exception as exc:
            # Broad, like `resolve` is around its store. The detail names the principal and
            # the exception class, never its message: `app.py` logs `exc.detail`, and a
            # psycopg message can carry the host, the user and the password it dialled with.
            msg = f"reading grants_version for {principal_id} failed: {type(exc).__name__}"
            raise ResolutionFailedError(msg) from exc

        if row is None:
            # A principal who has never held a grant has no row, and `0003` writes none. This
            # is the successful-read branch; the failure branch above cannot reach it.
            return 0
        return int(row[0])


def check_reachable(client: ValkeyClient) -> bool:
    """Readiness for the cache. A `PING`, bounded by the client's own socket timeout.

    A command rather than a connection test, for the reason `session.check_reachable` gives
    about PgBouncer: opening a socket proves something is listening, not that it can answer.

    Returns a bool rather than raising, because a readiness probe that raises turns a
    degraded dependency into a crashed process, and the whole point of this cache is that the
    system works without it.
    """
    try:
        client.ping()
    except (RedisError, OSError) as exc:
        # Class name only. `str(exc)` on a connection error can contain the URL, and the URL
        # contains the password. `session.check_reachable` logs the message and predates this
        # module; it is not a precedent worth copying into a line about a credentialed cache.
        log.warning("cache unreachable", cache="valkey", op="ping", error=type(exc).__name__)
        return False
    return True


async def check_reachable_async(client: ValkeyClient) -> bool:
    """`check_reachable` off the event loop, for `app.py`'s async lifespan.

    The client is synchronous because `resolve` and `answer_cache` are, and a synchronous
    socket call awaited directly in a coroutine blocks every other request on that worker for
    the duration of the timeout. `app.py` already runs migrations through `asyncio.to_thread`
    for the same reason.
    """
    return await asyncio.to_thread(check_reachable, client)
