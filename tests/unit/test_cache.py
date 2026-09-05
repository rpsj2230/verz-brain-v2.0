"""The Valkey client, the two caches over it, and the version source under them.

No Valkey and no PostgreSQL are contacted anywhere in this file. The client is a fake with
three methods and the connection is a fake with two, which is the whole reason
`brain.cache.ValkeyClient` and `brain.cache.Connection` are narrow protocols rather than
concrete types.

`make_client` is exercised for real, because building a `redis.Redis` opens no socket: the
connection pool is lazy, and the assertions are about the arguments it was configured with.

Task ids: M1.4.5
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from brain.cache import (
    CONNECT_TIMEOUT_SECONDS,
    OPERATION_TIMEOUT_SECONDS,
    RETRIES,
    STATEMENT_TIMEOUT_MS,
    STATEMENT_TIMEOUT_SQL,
    VERSION_SQL,
    CacheHealth,
    PostgresVersionSource,
    ValkeyAnswerStore,
    ValkeyEntitlementCache,
    check_reachable,
    check_reachable_async,
    make_client,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.gate.answer_cache import STORE_TTL_SECONDS, AnswerStore, lookup, store_answer
from brain.gate.cache_key import CachedAnswer, key_for
from brain.gate.resolve import (
    CACHE_TTL_SECONDS,
    EntitlementCache,
    ResolutionFailedError,
    VersionSource,
    cache_key,
    resolve,
)

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)

#: A URL shaped like a real one, so a test can prove the password never reaches a log line.
DEAD_URL = "redis://brain:s3cr3t-valkey-password@cache.internal:6379/0"


# ------------------------------------------------------------------ the fakes
class FakeValkey:
    """An in-memory stand-in for the three commands the client uses. Opens no socket."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.pings = 0

    def get(self, name: str) -> bytes | None:
        return self.data.get(name)

    def setex(self, name: str, time: int, value: bytes) -> object:
        self.data[name] = bytes(value)
        self.ttls[name] = time
        return True

    def ping(self) -> object:
        self.pings += 1
        return True


class DeadValkey:
    """Fails every call the way an unreachable Valkey fails, credentialed message included."""

    def get(self, name: str) -> bytes | None:
        raise RedisConnectionError(f"Error connecting to {DEAD_URL}")

    def setex(self, name: str, time: int, value: bytes) -> object:
        raise RedisConnectionError(f"Error connecting to {DEAD_URL}")

    def ping(self) -> object:
        raise RedisConnectionError(f"Error connecting to {DEAD_URL}")


class FakeCursor:
    """Answers `VERSION_SQL` from a dict and records every statement it was given."""

    def __init__(
        self, versions: dict[str, int], seen: list[tuple[str, tuple[object, ...]]]
    ) -> None:
        self._versions = versions
        self.seen = seen
        self._row: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Sequence[object] = ()) -> object:
        self.seen.append((query, tuple(params)))
        if query == VERSION_SQL:
            found = self._versions.get(str(params[0]))
            self._row = None if found is None else (found,)
        return None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class BrokenCursor:
    """A database that is there and does not answer."""

    def execute(self, query: str, params: Sequence[object] = ()) -> object:
        raise RuntimeError(f"connection to postgres://brain:s3cr3t@db:5432 failed on {query[:12]}")

    def fetchone(self) -> tuple[Any, ...] | None:  # pragma: no cover - execute raises first
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor | BrokenCursor) -> None:
        self._cursor = cursor

    @contextmanager
    def cursor(self) -> Iterator[FakeCursor | BrokenCursor]:
        yield self._cursor


def version_source(
    versions: dict[str, int],
    *,
    broken: bool = False,
    statement_timeout_ms: int | None = STATEMENT_TIMEOUT_MS,
) -> tuple[PostgresVersionSource, list[tuple[str, tuple[object, ...]]]]:
    seen: list[tuple[str, tuple[object, ...]]] = []
    cursor = BrokenCursor() if broken else FakeCursor(versions, seen)
    connection = FakeConnection(cursor)

    @contextmanager
    def connect() -> Iterator[FakeConnection]:
        yield connection

    return PostgresVersionSource(connect, statement_timeout_ms=statement_timeout_ms), seen


def ents(principal: str, *caps: str, not_after: datetime | None = None) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
        not_after=not_after,
    )


def an_answer(key: str = "k", payload: str = "SNM has 12 hours left") -> CachedAnswer:
    return CachedAnswer(key=key, payload=payload, stored_at=NOW, source_epochs={"lark_base": 4})


# ------------------------------------------------------------ the entitlement cache
def test_an_entitlement_set_survives_a_round_trip_through_the_cache() -> None:
    """The base case. Without it the module is a set of failure handlers around a cache that
    was never shown to work."""
    client = FakeValkey()
    cache = ValkeyEntitlementCache(client)
    value = ents("u_weiling", "read:client.name", "read:client.hours_remaining")

    cache.set(cache_key("u_weiling", 3), value, CACHE_TTL_SECONDS)
    assert cache.get(cache_key("u_weiling", 3)) == value


def test_the_stored_bytes_are_the_models_own_json() -> None:
    """A shared cache is read by other tools and other people. A payload that is not JSON is a
    payload nobody can inspect without running our code, which is how a debugging session ends
    up unpickling whatever is in there."""
    client = FakeValkey()
    ValkeyEntitlementCache(client).set(
        cache_key("u_weiling", 1), ents("u_weiling", "read:client.name"), CACHE_TTL_SECONDS
    )
    stored = json.loads(client.data[cache_key("u_weiling", 1)])
    assert stored["principal_id"] == "u_weiling"
    assert stored["grants"][0]["capability"]["value"] == "read:client.name"


def test_a_key_that_was_never_written_is_a_miss() -> None:
    """None is the protocol's word for "not in hand", and the whole design rests on `resolve`
    treating it as an instruction to ask the authority."""
    cache = ValkeyEntitlementCache(FakeValkey())
    assert cache.get(cache_key("u_weiling", 1)) is None
    assert cache.health.misses == 1


def test_the_ttl_reaches_the_store_on_every_write() -> None:
    """A key written with no expiry outlives the deployment that wrote it, and an orphaned
    entitlement key that never expires is a permission decision nobody can find."""
    client = FakeValkey()
    ValkeyEntitlementCache(client).set(
        cache_key("u_weiling", 1), ents("u_weiling"), CACHE_TTL_SECONDS
    )
    assert client.ttls[cache_key("u_weiling", 1)] == CACHE_TTL_SECONDS


def test_a_non_positive_ttl_is_refused_rather_than_silently_storing_nothing() -> None:
    """Valkey reports it as a command error, which this module swallows like any other, so
    without the check the cache would appear to work and hold nothing."""
    cache = ValkeyEntitlementCache(FakeValkey())
    with pytest.raises(ValueError, match="ttl"):
        cache.set(cache_key("u_weiling", 1), ents("u_weiling"), 0)


def test_resolve_works_end_to_end_over_the_real_cache_class() -> None:
    """M1.4.5 is the wiring, not the class. `resolve` had never run against anything but a
    dict, so this is the first thing that shows the two halves fit."""
    client = FakeValkey()
    cache = ValkeyEntitlementCache(client)
    versions, _ = version_source({"u_weiling": 7})
    store = _Store({"u_weiling": ents("u_weiling", "read:client.name")})

    first = resolve("u_weiling", versions=versions, store=store, cache=cache)
    second = resolve("u_weiling", versions=versions, store=store, cache=cache)

    assert first.from_cache is False
    assert second.from_cache is True
    assert store.loads == 1
    assert cache_key("u_weiling", 7) in client.data


class _Store:
    def __init__(self, sets: dict[str, EntitlementSet]) -> None:
        self.sets = sets
        self.loads = 0

    def load(self, principal_id: str) -> EntitlementSet:
        self.loads += 1
        return self.sets[principal_id]


# ----------------------------------------------------------------- the answer store
def test_an_answer_survives_a_round_trip_including_its_timezone() -> None:
    """`CachedAnswer.age` subtracts one datetime from another, which raises on a naive/aware
    pair. A store that dropped the timezone would break every hit at the moment it is read,
    not at the moment it is written."""
    client = FakeValkey()
    store = ValkeyAnswerStore(client)
    store.set("k", an_answer(), STORE_TTL_SECONDS)

    found = store.get("k")
    assert found is not None
    assert found == an_answer()
    assert found.stored_at.tzinfo is not None
    assert found.age(NOW + timedelta(minutes=4)) == timedelta(minutes=4)


def test_an_answer_whose_key_field_disagrees_with_its_key_is_refused_on_write() -> None:
    """The only way this happens is code that built the answer with one key and stored it
    under another. Dropping the write would hide it until a read, where the same mismatch
    means one person's answer reaching somebody else."""
    store = ValkeyAnswerStore(FakeValkey())
    with pytest.raises(ValueError, match="key"):
        store.set("k", an_answer(key="a-different-key"), STORE_TTL_SECONDS)


def test_the_answer_cache_works_end_to_end_over_the_real_store_class() -> None:
    """`store_answer` and `lookup` had only ever run against a dict. This is the pair against
    something that serialises, which is where a store goes wrong."""
    client = FakeValkey()
    store: AnswerStore = ValkeyAnswerStore(client)
    key = store_answer(
        "did we invoice acme",
        "yes, on the third",
        ent_hash="e" * 32,
        agent_config_hash="a" * 32,
        policy_epoch=9,
        source_epochs={"lark_base": 4},
        store=store,
        now=NOW,
    )

    assert key == key_for("did we invoice acme", "e" * 32, "a" * 32, 9, {"lark_base": 4})
    assert key is not None
    found = lookup(key, store, NOW + timedelta(minutes=4))
    assert found is not None
    assert found.payload == "yes, on the third"
    assert found.age_label(NOW + timedelta(minutes=4)) == "answered 4 minutes ago"


def test_a_stale_answer_is_still_the_stores_to_return_and_lookups_to_refuse() -> None:
    """The division of labour. Freshness is `answer_cache`'s rule, so a store that filtered on
    age would be deciding the same thing twice, and the two copies would disagree the first
    time `DEFAULT_MAX_AGE` moves."""
    client = FakeValkey()
    store = ValkeyAnswerStore(client)
    store.set("k", an_answer(), STORE_TTL_SECONDS)

    assert store.get("k") is not None
    assert lookup("k", store, NOW + timedelta(hours=1)) is None


# ------------------------------------------------------------------ the version source
def test_the_version_source_reads_the_counter_the_triggers_bump() -> None:
    """M1.4.5. Until this existed the version in the cache key came from nowhere, so a
    revocation bumped a number nobody read."""
    versions, seen = version_source({"u_weiling": 12})
    assert versions.grants_version("u_weiling") == 12
    assert (VERSION_SQL, ("u_weiling",)) in seen


def test_a_principal_who_has_never_held_a_grant_reads_as_zero() -> None:
    """`GrantsVersionRow` says the row is created on the first bump, which is what lets `0003`
    create nine tables and write no data. A reader that required a row would fail for
    everybody who has never been granted anything."""
    versions, _ = version_source({})
    assert versions.grants_version("u_new_starter") == 0


def test_a_failed_version_read_raises_the_gates_own_error() -> None:
    """`resolve` wraps `store.load` and not `versions.grants_version`, so anything raised here
    reaches the caller unchanged. A psycopg error carries the connection string it dialled."""
    versions, _ = version_source({}, broken=True)
    with pytest.raises(ResolutionFailedError) as caught:
        versions.grants_version("u_weiling")
    assert "s3cr3t" not in str(caught.value)
    assert "u_weiling" in str(caught.value)


def test_the_version_read_carries_a_statement_timeout() -> None:
    """A primary key lookup that hangs holds up a request before it has started doing its real
    work, and the request has no way to know it is waiting on a cache key."""
    versions, seen = version_source({"u_weiling": 1})
    versions.grants_version("u_weiling")
    assert seen[0] == (STATEMENT_TIMEOUT_SQL, (f"{STATEMENT_TIMEOUT_MS}ms",))


def test_the_statement_timeout_can_be_left_to_the_connection_string() -> None:
    """A deployment that sets `options=-c statement_timeout=...` should not get two bounds,
    one of which is silently a warning on an autocommit connection."""
    versions, seen = version_source({"u_weiling": 1}, statement_timeout_ms=None)
    versions.grants_version("u_weiling")
    assert [q for q, _ in seen] == [VERSION_SQL]


# ------------------------------------------------------------------------- health
def test_readiness_pings_rather_than_only_connecting() -> None:
    """`session.check_reachable` gives the reason: something listening is not something that
    can answer. PgBouncer accepts connections it cannot fulfil, and so does a proxy."""
    client = FakeValkey()
    assert check_reachable(client) is True
    assert client.pings == 1


def test_readiness_reports_a_dead_cache_rather_than_raising() -> None:
    """A probe that raises turns a degraded dependency into a crashed process, and this cache
    is one the system is designed to work without."""
    assert check_reachable(DeadValkey()) is False


def test_readiness_can_be_awaited_without_blocking_the_loop() -> None:
    """The client is synchronous because `resolve` is. Awaited directly, a blocking socket call
    stops every other request on that worker for the length of the timeout."""
    assert asyncio.run(check_reachable_async(FakeValkey())) is True
    assert asyncio.run(check_reachable_async(DeadValkey())) is False


def test_health_counts_hits_misses_and_writes_apart() -> None:
    """A hit rate nobody can see is one nobody notices collapsing, and the first symptom would
    be a latency complaint rather than a cache problem."""
    cache = ValkeyEntitlementCache(FakeValkey())
    cache.get(cache_key("u_weiling", 1))
    cache.set(cache_key("u_weiling", 1), ents("u_weiling"), CACHE_TTL_SECONDS)
    cache.get(cache_key("u_weiling", 1))

    assert (cache.health.misses, cache.health.writes, cache.health.hits) == (1, 1, 1)
    assert cache.health.degraded is False


def test_one_health_record_can_be_shared_by_both_caches() -> None:
    """A deployment reporting one cache figure wants one counter. The default is per instance
    because two counters merged by accident is a milder mistake than one split by accident."""
    shared = CacheHealth()
    entitlements = ValkeyEntitlementCache(FakeValkey(), health=shared)
    answers = ValkeyAnswerStore(FakeValkey(), health=shared)
    entitlements.get("ent:u_weiling:1")
    answers.get("k")
    assert shared.misses == 2


# ------------------------------------------------------------------------- client
def test_the_client_is_built_with_every_timeout_named() -> None:
    """`Redis.from_url` leaves both socket timeouts at None, which is not a large timeout but
    no timeout, and retries ten times with backoff on top. Left alone, one `get` against a
    cache that accepts and then stops answering waits as long as the far end holds the
    connection open."""
    client = cast(Redis, make_client("redis://127.0.0.1:6399/0"))
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == OPERATION_TIMEOUT_SECONDS
    assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT_SECONDS
    assert kwargs["retry"].get_retries() == RETRIES


def test_the_client_returns_bytes_rather_than_decoded_strings() -> None:
    """pydantic parses JSON from bytes directly. Decoding first is a wasted copy and moves a
    possible UnicodeDecodeError into the client, where this module cannot turn it into a
    miss."""
    client = cast(Redis, make_client("redis://127.0.0.1:6399/0"))
    assert client.connection_pool.connection_kwargs.get("decode_responses") is False


def test_the_classes_satisfy_the_protocols_they_are_written_against() -> None:
    """Checked by mypy rather than at runtime: the protocols are structural and not
    `runtime_checkable`, so a drifted signature is a type error and never an exception."""
    cache: EntitlementCache = ValkeyEntitlementCache(FakeValkey())
    answers: AnswerStore = ValkeyAnswerStore(FakeValkey())
    versions: VersionSource = version_source({})[0]

    assert cache.get("ent:u_weiling:1") is None
    assert answers.get("k") is None
    assert versions.grants_version("u_weiling") == 0
