"""A cache in front of a permission decision. A failure here blocks deploy.

Every other cache in the system serves a stale answer when it is wrong. This one serves
somebody else's reach, so the tests are about invalidation and about failure, not about
speed.

Task ids: M3.3.1, M3.3.2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.gate.resolve import (
    CACHE_TTL_SECONDS,
    ResolutionFailedError,
    cache_key,
    resolve,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _ents(principal: str, *caps: str, not_after: datetime | None = None) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
        not_after=not_after,
    )


class FakeVersions:
    def __init__(self, version: int = 1) -> None:
        self.version = version
        self.reads = 0

    def grants_version(self, principal_id: str) -> int:
        del principal_id
        self.reads += 1
        return self.version


class FakeStore:
    def __init__(self, sets: dict[str, EntitlementSet]) -> None:
        self.sets = sets
        self.loads = 0

    def load(self, principal_id: str) -> EntitlementSet:
        self.loads += 1
        return self.sets[principal_id]


class FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, EntitlementSet] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> EntitlementSet | None:
        return self.data.get(key)

    def set(self, key: str, value: EntitlementSet, ttl_seconds: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl_seconds


class BrokenStore:
    def load(self, principal_id: str) -> EntitlementSet:
        raise RuntimeError(f"database is unreachable, asked for {principal_id}")


class WrongStore:
    def load(self, principal_id: str) -> EntitlementSet:
        del principal_id
        return _ents("p_somebody_else", "read:client.contract_value")


# ---------------------------------------------------------------- invalidation
def test_a_version_bump_orphans_the_old_entry_rather_than_deleting_it() -> None:
    """The whole invalidation design. A delete that does not arrive leaves a stale entry
    serving a revoked permission and nothing reports it. An orphaned key cannot be read by
    accident, because nobody can construct it."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()

    first = resolve("p", versions=versions, store=store, cache=cache)
    assert first.from_cache is False

    store.sets["p"] = _ents("p")  # the grant is revoked
    versions.version = 2

    second = resolve("p", versions=versions, store=store, cache=cache)
    assert second.from_cache is False
    assert second.entitlements.grants == ()
    # The old entry is still sitting there, unreachable and harmless.
    assert cache_key("p", 1) in cache.data


def test_the_same_version_is_served_from_cache() -> None:
    """A cache that never hits is not a cache, and resolution happens on every request."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()

    resolve("p", versions=versions, store=store, cache=cache)
    second = resolve("p", versions=versions, store=store, cache=cache)
    assert second.from_cache is True
    assert store.loads == 1


def test_the_version_is_in_the_key_rather_than_checked_after_the_read() -> None:
    """Checking after means the stale value was already in hand, and a later refactor that
    drops the check reads as a simplification."""
    assert cache_key("p", 1) != cache_key("p", 2)
    assert "1" in cache_key("p", 1)


def test_two_principals_never_share_a_key() -> None:
    assert cache_key("p_a", 1) != cache_key("p_b", 1)


def test_reading_the_version_is_cheaper_than_loading_the_grants() -> None:
    """Structural: the version comes from its own source. If learning the version cost what
    loading costs, the cache would save nothing and would still carry all its risk."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()
    for _ in range(5):
        resolve("p", versions=versions, store=store, cache=cache)
    assert versions.reads == 5
    assert store.loads == 1


# ------------------------------------------------------------------- failure
def test_a_failed_load_raises_rather_than_returning_an_empty_set() -> None:
    """An empty set is a legitimate value: it flows onward, gets cached, gets hashed and
    produces a confident "I could not find that" for a person who should have seen the
    record. A failure has to be distinguishable from holding nothing."""
    with pytest.raises(ResolutionFailedError):
        resolve("p", versions=FakeVersions(), store=BrokenStore(), cache=FakeCache())


def test_a_failed_load_caches_nothing() -> None:
    """Otherwise the failure becomes the answer for the next sixty seconds."""
    cache = FakeCache()
    with pytest.raises(ResolutionFailedError):
        resolve("p", versions=FakeVersions(), store=BrokenStore(), cache=cache)
    assert cache.data == {}


def test_a_store_returning_the_wrong_principal_is_refused() -> None:
    """The catastrophic failure, and one comparison to rule out. Every check downstream
    would pass, because the set handed back is internally consistent; it just belongs to
    somebody else."""
    with pytest.raises(ResolutionFailedError, match="p_somebody_else"):
        resolve("p", versions=FakeVersions(), store=WrongStore(), cache=FakeCache())


def test_a_cache_entry_for_the_wrong_principal_is_ignored_not_served() -> None:
    """A cache is a shared mutable store that other processes write to, so a value coming
    out of it is checked as though it arrived from outside, which it did."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()
    cache.data[cache_key("p", 1)] = _ents("p_other", "read:client.contract_value")

    resolved = resolve("p", versions=versions, store=store, cache=cache)
    assert resolved.from_cache is False
    assert resolved.entitlements.principal_id == "p"


# -------------------------------------------------------------------- expiry
def test_an_expired_cache_entry_is_not_served() -> None:
    """A sixty-second TTL happily outlives a contractor whose access ended thirty seconds
    ago. Expiry is a property of the set, so it is checked when the set is read."""
    versions = FakeVersions(1)
    expired = _ents("p", "read:client.name", not_after=NOW - timedelta(minutes=1))
    store = FakeStore({"p": expired})
    cache = FakeCache()
    cache.data[cache_key("p", 1)] = expired

    resolved = resolve("p", versions=versions, store=store, cache=cache, now=NOW)
    assert resolved.from_cache is False


def test_an_unexpired_entry_is_served() -> None:
    versions = FakeVersions(1)
    live = _ents("p", "read:client.name", not_after=NOW + timedelta(days=30))
    cache = FakeCache()
    cache.data[cache_key("p", 1)] = live
    resolved = resolve("p", versions=versions, store=FakeStore({"p": live}), cache=cache, now=NOW)
    assert resolved.from_cache is True


# ---------------------------------------------------------------------- hash
def test_the_hash_is_computed_from_the_set_just_resolved() -> None:
    """M3.3.2. A stored hash is a second copy of a fact, and the copies disagree the first
    time anyone edits grants without recomputing it, at which point the cache is keyed on
    one reach while the answer uses another."""
    ents = _ents("p", "read:client.name")
    resolved = resolve(
        "p", versions=FakeVersions(1), store=FakeStore({"p": ents}), cache=FakeCache()
    )
    assert resolved.ent_hash == ents.ent_hash()


def test_a_cache_hit_and_a_miss_produce_the_same_hash() -> None:
    """If they differed, the answer cache would miss for the same person depending on
    whether their entitlement happened to be warm."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()
    miss = resolve("p", versions=versions, store=store, cache=cache)
    hit = resolve("p", versions=versions, store=store, cache=cache)
    assert miss.ent_hash == hit.ent_hash
    assert hit.from_cache and not miss.from_cache


def test_two_principals_with_the_same_reach_hash_alike() -> None:
    """The hash is of the reach, not of the person. Two people with identical entitlements
    must share an answer, or the cache is per-person and worth much less."""
    a = resolve(
        "p_a",
        versions=FakeVersions(1),
        store=FakeStore({"p_a": _ents("p_a", "read:client.name")}),
        cache=FakeCache(),
    )
    b = resolve(
        "p_b",
        versions=FakeVersions(1),
        store=FakeStore({"p_b": _ents("p_b", "read:client.name")}),
        cache=FakeCache(),
    )
    assert a.ent_hash == b.ent_hash


# ----------------------------------------------------------------------- ttl
def test_the_ttl_is_a_backstop_and_not_the_correctness_mechanism() -> None:
    """Sixty seconds, and the version in the key is what makes a revocation immediate. A
    TTL alone would leave a revoked permission live for up to a minute."""
    versions, store = FakeVersions(1), FakeStore({"p": _ents("p", "read:client.name")})
    cache = FakeCache()
    resolve("p", versions=versions, store=store, cache=cache)
    assert cache.ttls[cache_key("p", 1)] == CACHE_TTL_SECONDS
    assert CACHE_TTL_SECONDS == 60
