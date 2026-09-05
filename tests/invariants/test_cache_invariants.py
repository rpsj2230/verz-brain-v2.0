"""A cache client in front of a permission decision. A failure here blocks deploy.

`test_resolve_invariants` proves `resolve` is right about a cache. These prove the cache is
the thing `resolve` was promised: it forgets, it never invents, and when it is unreachable it
gets out of the way rather than becoming an answer.

Four properties carry the module, and each is a way the system could be wrong rather than
slow.

- A miss and an outage both fall through to the authority, and neither becomes an empty
  entitlement set. An empty set is a legitimate value that flows onward and produces a
  confident "I could not find that" for somebody who should have seen the record.
- Nothing is deleted to invalidate, so there is no method that deletes.
- Nothing is pickled, because a pickle in a shared cache is remote code execution the moment
  anything else can write to that cache.
- Every call is bounded, because a cache that hangs is worse than one that is down: the
  request waits instead of falling through.

No Valkey and no PostgreSQL are contacted anywhere in this file.

Task ids: M1.4.5
"""

from __future__ import annotations

import ast
import pickle
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from structlog.testing import capture_logs

import brain.cache
from brain.cache import (
    STATEMENT_TIMEOUT_MS,
    STATEMENT_TIMEOUT_SQL,
    VERSION_SQL,
    PostgresVersionSource,
    ValkeyAnswerStore,
    ValkeyClient,
    ValkeyEntitlementCache,
    check_reachable,
    make_client,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.gate.answer_cache import STORE_TTL_SECONDS, lookup, store_answer
from brain.gate.cache_key import CachedAnswer, key_for
from brain.gate.resolve import (
    CACHE_TTL_SECONDS,
    ResolutionFailedError,
    cache_key,
    resolve,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)

#: Shaped like a real connection string, so a log line that leaked one would be caught.
DEAD_URL = "redis://brain:s3cr3t-valkey-password@cache.internal:6379/0"
DB_URL = "postgres://brain:d1fferent-db-password@db.internal:5432/brain"

SOURCE = Path(brain.cache.__file__).read_text(encoding="utf-8")


# ------------------------------------------------------------------ the fakes
class FakeValkey:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def get(self, name: str) -> bytes | None:
        return self.data.get(name)

    def setex(self, name: str, time: int, value: bytes) -> object:
        self.data[name] = bytes(value)
        self.ttls[name] = time
        return True

    def ping(self) -> object:
        return True


class DeadValkey:
    """Unreachable, the way redis-py reports it: a connection error naming the URL."""

    def get(self, name: str) -> bytes | None:
        raise RedisConnectionError(f"Error connecting to {DEAD_URL}")

    def setex(self, name: str, time: int, value: bytes) -> object:
        raise RedisConnectionError(f"Error connecting to {DEAD_URL}")

    def ping(self) -> object:
        raise RedisTimeoutError(f"Timeout connecting to {DEAD_URL}")


class RawSocketValkey:
    """Fails with a bare `OSError`, which a client can leak before it wraps anything."""

    def get(self, name: str) -> bytes | None:
        raise OSError(104, "Connection reset by peer")

    def setex(self, name: str, time: int, value: bytes) -> object:
        raise OSError(104, "Connection reset by peer")

    def ping(self) -> object:
        raise OSError(104, "Connection reset by peer")


class GoodStore:
    def __init__(self, sets: dict[str, EntitlementSet]) -> None:
        self.sets = sets
        self.loads = 0

    def load(self, principal_id: str) -> EntitlementSet:
        self.loads += 1
        return self.sets[principal_id]


class BrokenStore:
    def load(self, principal_id: str) -> EntitlementSet:
        raise RuntimeError(f"database is unreachable, asked for {principal_id}")


class FixedVersions:
    def __init__(self, version: int = 1) -> None:
        self.version = version

    def grants_version(self, principal_id: str) -> int:
        del principal_id
        return self.version


class FakeCursor:
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
    def execute(self, query: str, params: Sequence[object] = ()) -> object:
        del query, params
        raise RuntimeError(f"connection to {DB_URL} failed")

    def fetchone(self) -> tuple[Any, ...] | None:  # pragma: no cover - execute raises first
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor | BrokenCursor) -> None:
        self._cursor = cursor

    @contextmanager
    def cursor(self) -> Iterator[FakeCursor | BrokenCursor]:
        yield self._cursor


def version_source(
    versions: dict[str, int], *, broken: bool = False
) -> tuple[PostgresVersionSource, list[tuple[str, tuple[object, ...]]]]:
    seen: list[tuple[str, tuple[object, ...]]] = []
    connection = FakeConnection(BrokenCursor() if broken else FakeCursor(versions, seen))

    @contextmanager
    def connect() -> Iterator[FakeConnection]:
        yield connection

    return PostgresVersionSource(connect), seen


def ents(principal: str, *caps: str, not_after: datetime | None = None) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
        not_after=not_after,
    )


# ------------------------------------------------- a miss and an outage are different
def test_a_cache_outage_falls_through_to_the_database() -> None:
    """The property the whole module exists for. An unreachable cache must make the system
    slower and never change what it says, so `get` reports "not in hand" rather than raising:
    `resolve` wraps `store.load` and nothing else, so an exception from the cache would leave
    the gate as an unhandled error on every single request."""
    cache = ValkeyEntitlementCache(DeadValkey())
    store = GoodStore({"u_weiling": ents("u_weiling", "read:client.name")})

    resolved = resolve("u_weiling", versions=FixedVersions(1), store=store, cache=cache)

    assert resolved.from_cache is False
    assert resolved.entitlements == ents("u_weiling", "read:client.name")
    assert store.loads == 1
    assert cache.health.degraded is True


def test_a_cache_outage_never_becomes_an_empty_entitlement_set() -> None:
    """The tempting default looks safe and is not. An empty set flows onward, gets hashed, and
    produces a confident "I could not find that" for a person who should have seen the record,
    which is indistinguishable from a correct answer at every point downstream."""
    cache = ValkeyEntitlementCache(DeadValkey())
    resolved = resolve(
        "u_weiling",
        versions=FixedVersions(1),
        store=GoodStore({"u_weiling": ents("u_weiling", "read:client.name")}),
        cache=cache,
    )
    assert resolved.entitlements.grants != ()


def test_a_cache_outage_on_top_of_a_database_outage_raises_rather_than_returning_nothing() -> None:
    """Both halves down is the case where a default would be most tempting and most wrong. A
    resolution that failed has to say so."""
    with pytest.raises(ResolutionFailedError):
        resolve(
            "u_weiling",
            versions=FixedVersions(1),
            store=BrokenStore(),
            cache=ValkeyEntitlementCache(DeadValkey()),
        )


def test_a_failed_cache_write_does_not_fail_the_request() -> None:
    """By the time the write happens the answer is already in hand. Raising here would turn a
    degraded cache into a broken system, which is the failure this module is meant to rule
    out."""
    cache = ValkeyEntitlementCache(DeadValkey())
    cache.set(cache_key("u_weiling", 1), ents("u_weiling", "read:client.name"), CACHE_TTL_SECONDS)
    assert cache.health.outages == 1


def test_a_bare_socket_error_is_handled_like_any_other_outage() -> None:
    """redis-py wraps most failures, and not all of them. Catching only `RedisError` leaves a
    reset connection propagating out of the gate on a path nobody tests."""
    cache = ValkeyEntitlementCache(RawSocketValkey())
    assert cache.get(cache_key("u_weiling", 1)) is None
    cache.set(cache_key("u_weiling", 1), ents("u_weiling"), CACHE_TTL_SECONDS)
    assert cache.health.outages == 2


def test_a_miss_and_an_outage_are_counted_apart() -> None:
    """They are the same instruction to `resolve` and a different fact for an operator. Merged
    into one counter, a dead cache reads as a cache that is merely cold, and the difference is
    the one worth paging somebody about."""
    cold = ValkeyEntitlementCache(FakeValkey())
    cold.get(cache_key("u_weiling", 1))
    dead = ValkeyEntitlementCache(DeadValkey())
    dead.get(cache_key("u_weiling", 1))

    assert (cold.health.misses, cold.health.outages, cold.health.degraded) == (1, 0, False)
    assert (dead.health.misses, dead.health.outages, dead.health.degraded) == (0, 1, True)


# ------------------------------------------------------------- nothing is deleted
def test_neither_cache_offers_a_way_to_delete_an_entry() -> None:
    """The invalidation design in one assertion. The version is in the key and a bump orphans
    the old one; a delete that does not arrive leaves a stale entry serving a revoked
    permission and nothing reports it. A second invalidation path is one that will be relied
    on, so there is not one to reach for."""
    forbidden = ("delete", "remove", "evict", "invalidate", "flush", "purge", "unlink", "expire")
    for cls in (ValkeyEntitlementCache, ValkeyAnswerStore, ValkeyClient):
        named = [n for n in dir(cls) if any(word in n.lower() for word in forbidden)]
        assert named == [], f"{cls.__name__} exposes {named}"


def test_every_write_carries_an_expiry() -> None:
    """A key with no expiry outlives the deployment that wrote it. That is the store reclaiming
    space and not invalidation, which is why the TTL is the caller's constant rather than a
    number this module chooses."""
    client = FakeValkey()
    ValkeyEntitlementCache(client).set(
        cache_key("u_weiling", 1), ents("u_weiling"), CACHE_TTL_SECONDS
    )
    ValkeyAnswerStore(client).set(
        "k", CachedAnswer(key="k", payload="p", stored_at=NOW, source_epochs={}), STORE_TTL_SECONDS
    )
    assert client.ttls == {cache_key("u_weiling", 1): CACHE_TTL_SECONDS, "k": STORE_TTL_SECONDS}


def test_an_expired_entitlement_set_is_still_cached() -> None:
    """`resolve` requires it and says why: refusing to cache an expired set means re-loading it
    on every request from a contractor whose access ended, which is when the load is least
    useful. Expiry is a property of the set, checked where the set is read."""
    client = FakeValkey()
    cache = ValkeyEntitlementCache(client)
    expired = ents("u_temp", "read:client.name", not_after=NOW - timedelta(minutes=1))

    resolve(
        "u_temp",
        versions=FixedVersions(1),
        store=GoodStore({"u_temp": expired}),
        cache=cache,
        now=NOW,
    )
    assert cache_key("u_temp", 1) in client.data


# ----------------------------------------------------------------- never pickle
def test_the_cache_client_imports_no_module_that_can_execute_a_stored_value() -> None:
    """A pickle in a shared cache is remote code execution the moment anything else can write
    to that cache, and something else always can: another service, a `redis-cli`, an operator
    with the URL. Checked on the import list rather than on the prose, which says the word
    several times."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported.isdisjoint({"pickle", "cloudpickle", "dill", "marshal", "shelve"})


def test_a_pickle_sitting_in_the_cache_is_refused_rather_than_executed() -> None:
    """The attack the rule exists for, carried out. A hostile writer puts a payload in the
    keyspace; unpickling it runs its `__reduce__` inside the gate. JSON cannot execute, so the
    worst outcome is a value that fails validation, which is a miss."""
    client = FakeValkey()
    key = cache_key("u_weiling", 1)
    client.data[key] = pickle.dumps(_HostilePayload())

    assert ValkeyEntitlementCache(client).get(key) is None
    assert _EXECUTED == [], "a stored payload ran code inside the gate"


def test_both_payloads_are_json_that_another_tool_can_read() -> None:
    """The positive half. A cache somebody can inspect with `redis-cli` is one they debug
    without running our deserialiser over whatever is in there."""
    client = FakeValkey()
    ValkeyEntitlementCache(client).set(cache_key("p", 1), ents("p", "read:client.name"), 60)
    ValkeyAnswerStore(client).set(
        "k", CachedAnswer(key="k", payload="p", stored_at=NOW, source_epochs={"x": 1}), 60
    )
    for raw in client.data.values():
        assert raw.startswith(b"{") and raw.endswith(b"}")


# ------------------------------------------- a value out of the cache came from outside
def test_a_value_that_is_not_well_formed_is_a_miss_and_not_a_crash() -> None:
    """The bytes are shared, mutable, and writable by anything holding the URL. Trusting them
    to parse turns somebody else's write into an exception on the permission path."""
    client = FakeValkey()
    key = cache_key("u_weiling", 1)
    cache = ValkeyEntitlementCache(client)

    for rubbish in (b"", b"{", b"null", b'{"principal_id": 4}', b"\xff\xfe not utf-8"):
        client.data[key] = rubbish
        assert cache.get(key) is None
    assert cache.health.rejections == 5


def test_a_cache_entry_for_the_wrong_principal_is_left_for_resolve_to_refuse() -> None:
    """The division of labour, and a check that it composes. The cache parses and hands back
    what it found; `resolve._usable` compares the principal. Repeating the comparison here
    would put the same rule in two places, which is how one of them gets relaxed."""
    client = FakeValkey()
    client.data[cache_key("u_weiling", 1)] = (
        ents("u_someone_else", "read:client.contract_value").model_dump_json().encode()
    )
    cache = ValkeyEntitlementCache(client)

    assert cache.get(cache_key("u_weiling", 1)) is not None  # the cache is not the judge
    resolved = resolve(
        "u_weiling",
        versions=FixedVersions(1),
        store=GoodStore({"u_weiling": ents("u_weiling", "read:client.name")}),
        cache=cache,
    )
    assert resolved.entitlements.principal_id == "u_weiling"
    assert resolved.from_cache is False


def test_an_answer_found_under_a_key_that_is_not_its_own_is_refused() -> None:
    """Nothing else in the path would notice. The answer is internally consistent and fresh; it
    just belongs to somebody else, and `lookup` checks age rather than identity."""
    elsewhere = CachedAnswer(
        key="somebody-elses-key", payload="margin is 41%", stored_at=NOW, source_epochs={}
    )
    staging = FakeValkey()
    ValkeyAnswerStore(staging).set(elsewhere.key, elsewhere, STORE_TTL_SECONDS)

    client = FakeValkey()
    client.data["k"] = staging.data[elsewhere.key]  # the same bytes, under the wrong key
    store = ValkeyAnswerStore(client)

    assert store.get("k") is None
    assert lookup("k", store, NOW) is None
    # Refused on both reads. Nothing here removes it, because nothing here deletes; the
    # entry sits there until its expiry, unreachable and harmless.
    assert store.health.rejections == 2
    assert "k" in client.data


# ------------------------------------------------------------------- the key is the key
def test_the_key_reaches_the_store_exactly_as_the_gate_built_it() -> None:
    """The key is the invalidation token. A client that prefixes or rewrites it makes "was this
    orphaned by the version bump" a question about two pieces of code instead of one, and the
    answer stops being obvious at exactly the moment somebody needs it to be."""
    client = FakeValkey()
    ValkeyEntitlementCache(client).set(cache_key("u_weiling", 7), ents("u_weiling"), 60)
    assert list(client.data) == ["ent:u_weiling:7"]

    answers = FakeValkey()
    key = store_answer(
        "did we invoice acme",
        "yes",
        ent_hash="e" * 32,
        agent_config_hash="a" * 32,
        policy_epoch=9,
        source_epochs={"lark_base": 4},
        store=ValkeyAnswerStore(answers),
        now=NOW,
    )
    assert list(answers.data) == [key]
    assert key == key_for("did we invoice acme", "e" * 32, "a" * 32, 9, {"lark_base": 4})


# ---------------------------------------------------------------------- timeouts
def test_every_call_the_client_makes_is_bounded() -> None:
    """A cache that hangs is worse than a cache that is down, because the request waits instead
    of falling through. Asserted on the type and not on a value, because the trap is that
    `Redis.from_url` leaves both timeouts at None: a test comparing against redis-py's
    documented five seconds would pass on a client that waits forever."""
    client = cast(Redis, make_client(DEAD_URL))
    kwargs = client.connection_pool.connection_kwargs

    for name in ("socket_timeout", "socket_connect_timeout"):
        bound = kwargs.get(name)
        assert isinstance(bound, float), f"{name} is not set to a number"
        assert 0 < bound <= 1, f"{name} is {bound}, which is not a bound worth having"
    assert kwargs["retry"].get_retries() == 0


def test_the_version_read_is_bounded_too() -> None:
    """It is a network call like the others. A primary key lookup that hangs holds up a request
    before it has begun its real work, and nothing downstream can tell it is waiting."""
    versions, seen = version_source({"u_weiling": 3})
    versions.grants_version("u_weiling")
    assert seen[0] == (STATEMENT_TIMEOUT_SQL, (f"{STATEMENT_TIMEOUT_MS}ms",))
    assert STATEMENT_TIMEOUT_MS <= 1000


# ------------------------------------------------------- the version is never guessed
def test_a_missing_version_row_is_zero_and_a_failed_read_is_never_a_number() -> None:
    """One `if` apart, and the consequences are not comparable. A principal who has never held
    a grant has no row, which is zero. A read that failed must not also be zero: zero is a real
    version, so returning it would mint a key that was already used, under a wider entitlement,
    and whatever is cached there is still readable."""
    present, _ = version_source({"u_weiling": 4})
    assert present.grants_version("u_new_starter") == 0

    broken, _ = version_source({}, broken=True)
    with pytest.raises(ResolutionFailedError):
        broken.grants_version("u_new_starter")


# ---------------------------------------------------------------- nothing leaks
def test_no_credential_and_no_key_reaches_a_log_line() -> None:
    """A log line is read by more people, for longer, than the cache entry it describes. A
    redis-py connection error carries the URL it dialled and the URL carries the password, and
    `ent:<principal>:<version>` names a person."""
    cache = ValkeyEntitlementCache(DeadValkey())
    with capture_logs() as events:
        cache.get(cache_key("u_weiling", 1))
        cache.set(cache_key("u_weiling", 1), ents("u_weiling"), CACHE_TTL_SECONDS)
        check_reachable(DeadValkey())

    blob = repr(events)
    assert events, "an outage was not reported at all"
    assert "s3cr3t-valkey-password" not in blob
    assert "cache.internal" not in blob
    assert "u_weiling" not in blob
    # Still useful, or somebody puts the message back to debug it.
    assert "ConnectionError" in blob
    assert "TimeoutError" in blob


def test_a_failed_version_read_reports_no_connection_string_either() -> None:
    """`app.py` logs `exc.detail` on every `BrainError`, so the detail is a log line by another
    name, and a psycopg message names the host, the user and the password it dialled with."""
    versions, _ = version_source({}, broken=True)
    with pytest.raises(ResolutionFailedError) as caught:
        versions.grants_version("u_weiling")

    assert "d1fferent-db-password" not in str(caught.value)
    assert "db.internal" not in str(caught.value)
    assert "u_weiling" in str(caught.value)


# --------------------------------------------------- the payload used by the pickle test
_EXECUTED: list[str] = []


def _mark() -> str:
    """Stands in for the arbitrary code a pickle payload runs. Never called."""
    _EXECUTED.append("a pickle in the cache executed")
    return "owned"


class _HostilePayload:
    """Runs `_mark` if anything unpickles it. Nothing should."""

    def __reduce__(self) -> tuple[Callable[[], str], tuple[()]]:
        return (_mark, ())
