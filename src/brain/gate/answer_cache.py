"""Storing and fetching a whole answer, and refusing to when that would be wrong.

The key module next door decides *what makes two questions the same question*. This one is
the narrow part that touches the store, and it exists separately because the two have
different failure modes: a bad key serves the wrong person's answer, while a bad lookup
serves the right person a stale one.

The shape mirrors `brain.gate.resolve` deliberately. Both put everything that must
invalidate into the key rather than checking it after the read, so a moved source epoch
produces a miss rather than a hit that something downstream has to notice. Two caches with
two different invalidation philosophies is how one of them ends up wrong.

Task ids: M3.5.2
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from brain.gate.cache_key import DEFAULT_MAX_AGE, CachedAnswer, NotCacheableError, key_for


class AnswerStore(Protocol):
    """A store that may forget at any time and must never invent.

    No delete method, for the same reason the entitlement cache has none: everything that
    must invalidate is in the key, and offering a delete invites a second, weaker
    invalidation path that someone will rely on.
    """

    def get(self, key: str) -> CachedAnswer | None: ...
    def set(self, key: str, value: CachedAnswer, ttl_seconds: int) -> None: ...


#: How long an entry may sit in the store. Longer than `DEFAULT_MAX_AGE`, because the age
#: check is what decides whether an answer is servable and this only decides when the store
#: may reclaim the space. Making the TTL the freshness rule would mean an answer becomes
#: unservable at a moment nothing in the code can name.
STORE_TTL_SECONDS = 3600


def lookup(
    key: str,
    store: AnswerStore,
    now: datetime,
    *,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> CachedAnswer | None:
    """A servable answer, or None. Never a stale one, and never a bare payload.

    Returns the `CachedAnswer` rather than its payload so the caller cannot forget the age.
    A function returning the text alone would make surfacing "answered 4 minutes ago"
    optional, and optional is the same as absent by the third caller.
    """
    found = store.get(key)
    if found is None:
        return None
    if not found.is_fresh(now, max_age):
        # Not deleted. The store reclaims on its own schedule, and a read path that writes
        # is a read path that fails differently under load.
        return None
    return found


def store_answer(
    question: str,
    payload: str,
    *,
    ent_hash: str,
    agent_config_hash: str,
    policy_epoch: int,
    source_epochs: dict[str, int],
    sources: frozenset[str] | None = None,
    store: AnswerStore,
    now: datetime,
) -> str | None:
    """Store an answer, or decline. Returns the key used, or None when it was not cached.

    It builds the key itself rather than taking one, and that is the point: a caller
    holding a key could store an answer under a key built from somebody else's reach. The
    only way to store is to hand over the parts, so the key and the answer cannot disagree
    about who the answer was for.

    Declining is normal and returns None rather than raising. A volatile question is not an
    error; it is a question that gets asked again next time, which is the correct behaviour
    for "how many tickets are open right now".
    """
    try:
        key = key_for(
            question,
            ent_hash,
            agent_config_hash,
            policy_epoch,
            source_epochs,
            sources,
        )
    except NotCacheableError:
        return None

    store.set(
        key,
        CachedAnswer(key=key, payload=payload, stored_at=now, source_epochs=source_epochs),
        STORE_TTL_SECONDS,
    )
    return key
