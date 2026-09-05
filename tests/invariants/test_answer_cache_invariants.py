"""Storing and serving whole answers. A failure here blocks deploy.

The key module decides who may share an answer. This decides whether a shareable answer is
still worth serving, and whether it should have been stored at all.

Task ids: M3.5.2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.gate.answer_cache import STORE_TTL_SECONDS, lookup, store_answer
from brain.gate.cache_key import DEFAULT_MAX_AGE, CachedAnswer, key_for

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
Q = "which clients have hosting expiring next month"


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, CachedAnswer] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> CachedAnswer | None:
        return self.data.get(key)

    def set(self, key: str, value: CachedAnswer, ttl_seconds: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl_seconds


def _store(store: FakeStore, question: str = Q, **over: object) -> str | None:
    base: dict[str, object] = {
        "ent_hash": "ent-aaa",
        "agent_config_hash": "cfg-1",
        "policy_epoch": 7,
        "source_epochs": {"laravel": 44},
    }
    return store_answer(
        question,
        "four clients",
        store=store,
        now=NOW,
        **{**base, **over},  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ round trip
def test_an_answer_stored_can_be_found_again() -> None:
    """A cache that never hits is not a cache."""
    store = FakeStore()
    key = _store(store)
    assert key is not None
    found = lookup(key, store, NOW)
    assert found is not None
    assert found.payload == "four clients"


def test_a_lookup_returns_the_answer_with_its_age_not_a_bare_payload() -> None:
    """A function returning the text alone makes surfacing "answered 4 minutes ago"
    optional, and optional is the same as absent by the third caller."""
    store = FakeStore()
    key = _store(store)
    assert key is not None
    found = lookup(key, store, NOW + timedelta(minutes=4))
    assert found is not None
    assert found.age_label(NOW + timedelta(minutes=4)) == "answered 4 minutes ago"


def test_a_miss_is_none_rather_than_an_empty_answer() -> None:
    """An empty answer would be served to a person as a confident "nothing found"."""
    assert lookup("no-such-key", FakeStore(), NOW) is None


# ------------------------------------------------------------------- staleness
def test_an_answer_past_its_age_is_not_served() -> None:
    """The backstop for a source that changed without telling us, which the projection
    rules already assume happens."""
    store = FakeStore()
    key = _store(store)
    assert key is not None
    assert lookup(key, store, NOW + DEFAULT_MAX_AGE + timedelta(seconds=1)) is None


def test_an_answer_inside_its_age_is_served() -> None:
    store = FakeStore()
    key = _store(store)
    assert key is not None
    assert lookup(key, store, NOW + DEFAULT_MAX_AGE - timedelta(seconds=1)) is not None


def test_a_stale_read_does_not_write() -> None:
    """A read path that deletes is a read path that fails differently under load, and the
    store reclaims on its own schedule anyway."""
    store = FakeStore()
    key = _store(store)
    assert key is not None
    lookup(key, store, NOW + timedelta(hours=2))
    assert key in store.data


def test_the_store_ttl_is_longer_than_the_freshness_window() -> None:
    """They are different rules. Making the TTL the freshness rule would mean an answer
    becomes unservable at a moment nothing in the code can name."""
    assert DEFAULT_MAX_AGE.total_seconds() < STORE_TTL_SECONDS


# -------------------------------------------------------------- what is not stored
def test_a_volatile_question_is_not_stored_and_that_is_not_an_error() -> None:
    """It is a question that gets asked again next time, which is correct for "how many
    tickets are open right now"."""
    store = FakeStore()
    assert _store(store, "how many tickets are open right now") is None
    assert store.data == {}


def test_a_volatile_source_is_not_stored_however_the_question_was_phrased() -> None:
    store = FakeStore()
    assert _store(store, "summarise support", sources=frozenset({"queue_depth"})) is None
    assert store.data == {}


# --------------------------------------------------- the key and the answer agree
def test_storing_builds_its_own_key_from_the_parts() -> None:
    """A caller holding a key could store an answer under a key built from somebody else's
    reach. Handing over the parts is what makes that impossible."""
    store = FakeStore()
    key = _store(store)
    assert key == key_for(Q, "ent-aaa", "cfg-1", 7, {"laravel": 44})


def test_two_callers_with_different_reach_do_not_find_each_others_answers() -> None:
    """The property the key module guarantees, asserted again here because this is the
    module that actually performs the read."""
    store = FakeStore()
    mine = _store(store, ent_hash="ent-aaa")
    theirs_key = key_for(Q, "ent-bbb", "cfg-1", 7, {"laravel": 44})
    assert mine != theirs_key
    assert lookup(theirs_key, store, NOW) is None


def test_a_moved_source_epoch_is_a_miss_rather_than_a_stale_hit() -> None:
    """Everything that must invalidate is in the key, so a changed source produces a miss
    rather than a hit something downstream has to notice."""
    store = FakeStore()
    _store(store, source_epochs={"laravel": 44})
    moved = key_for(Q, "ent-aaa", "cfg-1", 7, {"laravel": 45})
    assert lookup(moved, store, NOW) is None


def test_a_policy_change_is_a_miss() -> None:
    """The global lever. A tightened field policy must not be undone by answers cached
    before it."""
    store = FakeStore()
    _store(store, policy_epoch=7)
    assert lookup(key_for(Q, "ent-aaa", "cfg-1", 8, {"laravel": 44}), store, NOW) is None


def test_the_stored_answer_carries_the_epochs_it_rests_on() -> None:
    """So a served answer can name the sources behind it without a second lookup."""
    store = FakeStore()
    key = _store(store)
    assert key is not None
    found = lookup(key, store, NOW)
    assert found is not None
    assert found.source_epochs == {"laravel": 44}
