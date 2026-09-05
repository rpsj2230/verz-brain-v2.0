"""Working out what a caller holds, quickly, without ever being wrong about it.

Entitlement resolution happens on every single request, so it has to be cached, and a cache
in front of a permission decision is the most dangerous cache in any system. Every other
cache serves a stale answer; this one serves somebody else's reach.

Three rules make it safe, and all three are about invalidation rather than speed.

**The key carries the version, so nothing is ever deleted.** A write bumps
`grants_version`, and every key built from the old version is orphaned in the same instant.
Deleting entries on write is the alternative, and it fails in the way that matters: a delete
that does not arrive leaves a stale entry serving a revoked permission, and nothing anywhere
reports it. An orphaned key cannot be read by accident because nobody can construct it.

**A miss loads, and a failure raises.** There is no path here that returns a default. The
tempting default is an empty set, which looks safe and is not: an empty set is a legitimate
value that flows onward, gets cached, gets hashed and produces a confident "I could not find
that" for someone who should have seen the record. A resolution that failed must say so.

**The set is checked against the principal who asked for it.** A store returning the wrong
row is the catastrophic failure, and it is one comparison to rule out.

Task ids: M3.3.1, M3.3.2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from brain.core.entitlement import EntitlementSet
from brain.core.errors import BrainError, Outcome

#: How long a resolved entitlement may live in the cache. Short enough that a missed
#: version bump is measured in seconds, long enough to matter across a burst of requests
#: from one person. It is a backstop: correctness comes from the version in the key.
CACHE_TTL_SECONDS = 60


class ResolutionFailedError(BrainError):
    """Resolution could not complete. Deliberately not an empty entitlement set.

    An empty set is a legitimate value: it flows onward, gets cached, gets hashed, and
    produces a confident "I could not find that" for a person who should have seen the
    record. A failure has to be distinguishable from holding nothing.
    """

    outcome = Outcome.FAILED
    public_message = "I could not work out what you have access to just now."


class VersionSource(Protocol):
    """The current grants version for a principal. Must be cheap; it is read every request.

    Separate from the store because the whole design depends on learning the version
    without loading the grants. If reading the version cost what loading costs, the cache
    would save nothing.
    """

    def grants_version(self, principal_id: str) -> int: ...


class EntitlementStore(Protocol):
    """The authority. Slow, correct, and consulted only on a miss."""

    def load(self, principal_id: str) -> EntitlementSet: ...


class EntitlementCache(Protocol):
    """A cache that may forget at any time and must never invent.

    `get` returning None is always safe. There is no method to delete, on purpose: version
    bumping is the invalidation mechanism, and offering a delete invites a second one.
    """

    def get(self, key: str) -> EntitlementSet | None: ...
    def set(self, key: str, value: EntitlementSet, ttl_seconds: int) -> None: ...


def cache_key(principal_id: str, grants_version: int) -> str:
    """`ent:<principal>:<version>`.

    The version is in the key rather than checked after a read. Checking after means the
    stale value was already in hand, and a later refactor that forgets the check reads as
    a simplification.
    """
    return f"ent:{principal_id}:{grants_version}"


@dataclass(frozen=True)
class Resolved:
    """A caller's reach, its hash, and where it came from.

    `from_cache` exists so a trace can show it. A cache hit rate nobody can see is one
    nobody notices collapsing, and the first symptom would be a latency complaint rather
    than a cache problem.
    """

    entitlements: EntitlementSet
    ent_hash: str
    grants_version: int
    from_cache: bool


def resolve(
    principal_id: str,
    *,
    versions: VersionSource,
    store: EntitlementStore,
    cache: EntitlementCache,
    now: datetime | None = None,
) -> Resolved:
    """M3.3.1 and M3.3.2: resolve the caller's reach and compute its hash.

    The hash is computed here from the set just resolved, never carried alongside it from
    the store. A stored hash is a second copy of a fact, and the two copies disagree the
    first time anyone edits grants without recomputing it, at which point the cache is
    keyed on one reach while the answer uses another.
    """
    try:
        version = versions.grants_version(principal_id)
    except Exception as exc:
        # The same guard as `store.load` below, and it was missing here for the same
        # reason it is easy to miss: this call looks like bookkeeping rather than I/O. It
        # is a database read, and whatever the driver raises would otherwise cross the
        # gate unchanged, connection string and all.
        #
        # Refuses rather than carrying on without a version. Carrying on would mean
        # skipping the cache and loading fresh, which is correct but useless: the version
        # source and the store are the same database, so a failure here means the load is
        # about to fail too, and the only thing the fall-through achieves is a second
        # error and a thundering herd onto a database that is already unwell.
        raise ResolutionFailedError(f"reading grants version for {principal_id}: {exc}") from exc
    key = cache_key(principal_id, version)

    cached = cache.get(key)
    if cached is not None and _usable(cached, principal_id, now):
        return Resolved(
            entitlements=cached,
            ent_hash=cached.ent_hash(),
            grants_version=version,
            from_cache=True,
        )

    try:
        loaded = store.load(principal_id)
    except Exception as exc:
        # Broad on purpose. Whatever the driver raises, a caller of this function must see
        # a failure it can distinguish from holding nothing, not a psycopg error leaking
        # through the gate with a connection string in its message.
        raise ResolutionFailedError(f"loading entitlements for {principal_id}: {exc}") from exc

    if loaded.principal_id != principal_id:
        # The catastrophic failure, and one comparison to rule out. A store returning the
        # wrong row hands one person another person's reach, and every check downstream
        # would pass, because the set is internally consistent.
        raise ResolutionFailedError(
            f"store returned entitlements for {loaded.principal_id} when asked for {principal_id}"
        )

    # Cached even when expired. Expiry is a property of the set, checked wherever the set
    # is used, and refusing to cache an expired set would mean re-loading it on every
    # request from a contractor whose access ended, which is when the load is least useful.
    cache.set(key, loaded, CACHE_TTL_SECONDS)
    return Resolved(
        entitlements=loaded,
        ent_hash=loaded.ent_hash(),
        grants_version=version,
        from_cache=False,
    )


def _usable(cached: EntitlementSet, principal_id: str, now: datetime | None) -> bool:
    """Whether a cache hit may be served.

    A cache is a shared, mutable store that other processes write to, so a value coming out
    of it is checked as though it arrived from outside, which it did. The principal check
    catches a key collision or a mis-set; the expiry check catches a set cached before an
    expiry that has since passed, which the TTL alone would not, because a sixty-second TTL
    happily outlives a contractor whose access ended thirty seconds ago.
    """
    if cached.principal_id != principal_id:
        return False
    return not cached.is_expired(now)
