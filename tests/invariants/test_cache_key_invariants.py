"""Who may share a cached answer. A failure here blocks deploy.

The cache is the one place in the system where an answer computed for one person is handed
to another on purpose. Everything that makes that safe lives in the key, so these tests are
about the key and nothing else.

Task ids: M3.5.1, M3.5.4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.gate.cache_key import (
    DEFAULT_MAX_AGE,
    CachedAnswer,
    CacheKeyParts,
    NotCacheableError,
    cache_key,
    is_volatile,
    key_for,
    normalise_question,
)

pytestmark = pytest.mark.invariant

Q = "which clients have hosting expiring next month"
BASE = {
    "question": Q,
    "ent_hash": "ent-aaa",
    "agent_config_hash": "cfg-1",
    "policy_epoch": 7,
    "source_epochs": {"laravel": 44, "xero": 9},
}


def _key(**over: object) -> str:
    return cache_key(CacheKeyParts(**{**BASE, **over}))  # type: ignore[arg-type]


# ------------------------------------------------------- the property that matters
def test_two_people_with_different_reach_never_share_an_answer() -> None:
    """The whole reason the entitlement hash is in the key. Without this, the first person
    to ask a question decides what everyone after them sees, and seniority stops mattering
    at exactly the moment it should."""
    assert _key(ent_hash="ent-aaa") != _key(ent_hash="ent-bbb")


def test_two_people_with_identical_reach_do_share_an_answer() -> None:
    """The other half. A key that never collides is not a cache, and two people whose reach
    is byte-identical cannot see anything different from one another."""
    assert _key() == _key()


def test_an_empty_entitlement_hash_is_refused_rather_than_hashed() -> None:
    """An empty string hashes perfectly well, and every caller passing one would collide
    into a single shared entry. This has to fail loudly at construction."""
    with pytest.raises(ValueError, match="entitlement hash"):
        _key(ent_hash="")


# --------------------------------------------------------------- the other four parts
@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("question", "which clients have hosting expiring this month"),
        ("agent_config_hash", "cfg-2"),
        ("policy_epoch", 8),
        ("source_epochs", {"laravel": 45, "xero": 9}),
    ],
)
def test_changing_any_single_part_changes_the_key(field: str, other: object) -> None:
    """Each part is in the key because it changes the answer. A part that did not move the
    key would be decoration, and its absence would be a silent widening."""
    assert _key(**{field: other}) != _key()


def test_a_missing_source_is_not_the_same_as_a_source_at_epoch_zero() -> None:
    """An answer that did not consult Xero and one that consulted a freshly reset Xero are
    different answers. Collapsing them would serve the first when the second was asked."""
    assert _key(source_epochs={"laravel": 44}) != _key(source_epochs={"laravel": 44, "xero": 0})


def test_gather_order_does_not_change_the_key() -> None:
    """Sources arrive in whatever order the fan-out finished. If order changed the key, the
    hit rate would collapse to near zero and nobody would notice, because a miss is silent."""
    assert _key(source_epochs={"xero": 9, "laravel": 44}) == _key(
        source_epochs={"laravel": 44, "xero": 9}
    )


# ------------------------------------------------------------------ no collisions
@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Plain concatenation: "ab"+"c" and "a"+"bc" are both "abc".
        (("ab", "c"), ("a", "bc")),
        # A separator does not fix it, it only moves the problem into the values. Any field
        # that can contain the separator can shift the boundary itself, and an entitlement
        # hash is not guaranteed to exclude any particular character.
        (("a|b", "c"), ("a", "b|c")),
        (("a:b", "c"), ("a", "b:c")),
        (("a;b", "c"), ("a", "b;c")),
    ],
)
def test_parts_cannot_be_shuffled_across_the_boundary_between_them(
    left: tuple[str, str], right: tuple[str, str]
) -> None:
    """Length-prefixing exists for this, and only length-prefixing survives all four cases.
    A digest that two different callers can both produce is one caller reading the other's
    answer, which is the failure this whole module exists to prevent."""
    a = _key(ent_hash=left[0], agent_config_hash=left[1])
    b = _key(ent_hash=right[0], agent_config_hash=right[1])
    assert a != b


# -------------------------------------------------------------- question normalisation
@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("Did we invoice Acme", "did we invoice acme"),
        ("did  we   invoice Acme", "did we invoice Acme"),
        ("did we invoice Acme\n", "did we invoice Acme"),
    ],
)
def test_spacing_and_case_do_not_make_a_new_question(one: str, two: str) -> None:
    """Normalising these is safe and buys most of the hit rate available."""
    assert normalise_question(one) == normalise_question(two)


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("did we invoice Acme", "did we not invoice Acme"),
        ("did we invoice Acme", "did we invoice Acme Ltd"),
        ("show me open tickets", "show me closed tickets"),
    ],
)
def test_questions_a_person_meant_to_distinguish_stay_distinct(one: str, two: str) -> None:
    """The reason normalisation stops at whitespace and case. Stemming or stop-word removal
    merges these, and dropping "not" inverts the answer."""
    assert normalise_question(one) != normalise_question(two)


# ------------------------------------------------------------------- never-cache
@pytest.mark.parametrize(
    "question",
    [
        "how many tickets are open right now",
        "who is on leave today",
        "what is the latest ticket from Acme",
        "how many invoices are outstanding",
    ],
)
def test_a_volatile_question_is_refused_a_key(question: str) -> None:
    """These are wrong by the time they are read. Caching one means confidently telling
    someone a number that changed while the page loaded."""
    assert is_volatile(question)
    with pytest.raises(NotCacheableError):
        key_for(question, "ent-aaa", "cfg-1", 7, {"laravel": 44})


def test_a_volatile_source_refuses_however_the_question_was_phrased() -> None:
    """Shape matching is language-dependent and a person who writes "as of this moment"
    defeats it. The source is the reliable signal, so it is checked as well."""
    assert is_volatile("summarise the support position", frozenset({"queue_depth"}))


def test_a_stable_question_is_cacheable() -> None:
    """A never-cache rule that catches everything is a disabled cache."""
    assert not is_volatile(Q)
    assert key_for(Q, "ent-aaa", "cfg-1", 7, {"laravel": 44})


def test_refusing_raises_rather_than_returning_none() -> None:
    """A caller that forgets to check a None walks on with the string "None" as its key and
    caches the volatile thing anyway, under a key every other volatile thing shares."""
    with pytest.raises(NotCacheableError):
        key_for("how many tickets are open right now", "e", "c", 1, {})


# ------------------------------------------------------------------ age on a hit
def test_a_hit_can_always_say_how_old_it_is() -> None:
    """M3.5.3. A cached answer that does not say it is cached is a lie of omission: the
    reader assumes the system just looked."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    answer = CachedAnswer("k", "payload", now - timedelta(minutes=4), {"laravel": 44})
    assert answer.age(now) == timedelta(minutes=4)
    assert answer.age_label(now) == "answered 4 minutes ago"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "answered just now"),
        (timedelta(minutes=1), "answered 1 minute ago"),
        (timedelta(minutes=59), "answered 59 minutes ago"),
        (timedelta(hours=2), "answered 2 hours ago"),
    ],
)
def test_the_age_reads_as_words_not_a_timestamp(delta: timedelta, expected: str) -> None:
    """A timestamp makes the reader do arithmetic to answer the only question they have,
    which is whether this is recent enough for what they are about to do."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert CachedAnswer("k", "p", now - delta, {}).age_label(now) == expected


def test_an_answer_older_than_the_backstop_is_not_fresh() -> None:
    """Epoch invalidation is exact, and this covers the case where a source changed without
    telling us, which the projection rules already assume happens."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    stale = CachedAnswer("k", "p", now - DEFAULT_MAX_AGE - timedelta(seconds=1), {})
    assert not stale.is_fresh(now)
