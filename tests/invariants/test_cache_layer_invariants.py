"""The caching layers. A failure here blocks deploy.

`tests/unit/test_caches.py` checks each guard where it lives. This states the rules those
guards exist to serve, in a form that would still fail if every one of those unit tests were
deleted and the module rewritten from its docstring. That claim was not true when this file
was written: a mutation dropping the entitlement hash out of `CallerKey.reach_fields`
survived the whole of it, because the answer key reads the hash directly and the retrieval
key carries a principal id that separates two callers on its own. The plan key was the only
one that depended on `reach_fields` alone, and it had no invariant. It has one now.

The reach rule is stated over generated entitlement sets rather than over two hand-picked
ones. A pair chosen to read well can be a pair a wrong implementation happens to separate:
the two callers below differ by a capability drawn from a pool, in both directions, and the
property is asserted as an equivalence rather than as an inequality, so an implementation
that made every key unique would fail it just as an implementation that made them all
identical would.

Task ids: M6.2.1, M6.2.2, M6.2.6
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from brain.cache import CacheHealth
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import SideEffect, ToolDefinition
from brain.core.scope import Clause, Op, Scope
from brain.gate import answer_cache as answer_cache_module
from brain.gate import cache_key as cache_key_module
from brain.gate import caches as caches_module
from brain.gate.cache_key import CachedAnswer, CacheKeyParts, NotCacheableError
from brain.gate.caches import (
    CachedPlan,
    CallerKey,
    answer_key,
    assert_answer_types_hold_no_vector,
    assert_counters_name_no_principal,
    assert_no_similarity_search,
    plan_key,
    replay,
    retrieval_key,
)
from brain.gate.catalogue import AgentCeiling, project
from brain.gate.resolve import Resolved

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
Q = "which clients have hosting expiring next month"

#: A pool wide enough that two drawn subsets are usually different and sometimes identical,
#: which is what makes the equivalence below testable in both directions.
POOL = (
    "read:client.name",
    "read:client.hours_remaining",
    "read:invoice.total",
    "read:ticket.status",
    "read:knowledge",
)

CAPABILITY_SETS = st.frozensets(st.sampled_from(POOL), min_size=1, max_size=len(POOL))


def reach(principal_id: str, capabilities: frozenset[str]) -> CallerKey:
    """A caller key built the way the gate builds one, from real grants."""
    entitlements = EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(
                capability=Capability(value=value),
                scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),)),
            )
            for value in sorted(capabilities)
        ),
    )
    return CallerKey.of(
        Resolved(
            entitlements=entitlements,
            ent_hash=entitlements.ent_hash(),
            grants_version=1,
            from_cache=False,
        ),
        policy_epoch=3,
        now=NOW,
    )


# ------------------------------------------------------- reach decides who shares (M6.2.1)


@example(mine=frozenset({"read:client.name"}), theirs=frozenset({"read:client.name"}))
@example(mine=frozenset({"read:client.name"}), theirs=frozenset({"read:invoice.total"}))
@given(mine=CAPABILITY_SETS, theirs=CAPABILITY_SETS)
def test_an_answer_key_is_shared_exactly_when_two_callers_reach_the_same_things(
    mine: frozenset[str], theirs: frozenset[str]
) -> None:
    """Two callers share an answer key if and only if their reach is the same.

    Both directions in one statement. An implementation that dropped the entitlement hash
    fails the inequality; one that mixed in the principal id fails the equality and would
    quietly reduce the cache to one entry per person. Delete this and the two failures have
    to be caught by two separate hand-written pairs, either of which can be chosen to suit
    the implementation rather than the rule.

    The two `@example`s are not decoration. Generation alone would leave the equality
    direction to chance, since two independently drawn sets are usually different, and a
    property that only ever sees one of its two directions is half a property whichever way
    it is written.
    """
    ours = answer_key(Q, reach("u_a", mine), agent_config_hash="cfg", source_epochs={"s.e": 1})
    yours = answer_key(Q, reach("u_b", theirs), agent_config_hash="cfg", source_epochs={"s.e": 1})

    assert (ours == yours) is (mine == theirs)


@example(mine=frozenset({"read:client.name"}), theirs=frozenset({"read:client.name"}))
@example(mine=frozenset({"read:client.name"}), theirs=frozenset({"read:invoice.total"}))
@given(mine=CAPABILITY_SETS, theirs=CAPABILITY_SETS)
def test_a_plan_key_is_shared_exactly_when_two_callers_reach_the_same_things(
    mine: frozenset[str], theirs: frozenset[str]
) -> None:
    """The same equivalence as the answer key, over the reach a plan key is built from.

    A plan is re-projected on the way out, so the key could be argued down to the question
    alone; it is not, because every caller would then share one entry and every reuse would
    be a near-certain empty replay. Delete this and `CallerKey.reach_fields` can be emptied
    of everything but the policy epoch with this whole file still green, which is the
    mutation that survived it once already.
    """
    ours = plan_key(Q, reach("u_a", mine), agent_config_hash="cfg")
    yours = plan_key(Q, reach("u_b", theirs), agent_config_hash="cfg")

    assert (ours == yours) is (mine == theirs)


@given(mine=CAPABILITY_SETS, theirs=CAPABILITY_SETS)
def test_a_retrieval_key_is_never_shared_between_two_people(
    mine: frozenset[str], theirs: frozenset[str]
) -> None:
    """The document plane's reach includes what a person owns, so nobody shares a key.

    Unlike the answer key above, and deliberately: `brain.knowledge.search.Reach` matches
    personal documents on `owner_id`, so equal grants are not equal reach. Delete this and
    the retrieval key can be made to look like the answer key on the argument that the two
    should be consistent, and one person's drafts are retrieved for another.
    """
    ours = retrieval_key(Q, reach("u_a", mine), departments=("web",), corpus_epoch=1)
    yours = retrieval_key(Q, reach("u_b", theirs), departments=("web",), corpus_epoch=1)

    assert ours != yours


def test_nothing_may_be_keyed_for_a_caller_past_their_time_bound() -> None:
    """An answer stored before a contractor's expiry must not be served after it.

    The entitlement hash is identical either side of the bound, because it is a digest of a
    set and a set does not know what time it is. Delete this and the only thing standing
    between an expired contractor and their own cached answers is a check somebody has to
    remember at four call sites.
    """
    entitlements = EntitlementSet(
        principal_id="u_contractor",
        grants=(
            Grant(
                capability=Capability(value="read:client.name"),
                scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),)),
            ),
        ),
        not_after=NOW - timedelta(seconds=1),
    )
    same_set_before_the_bound = EntitlementSet(
        principal_id="u_contractor",
        grants=entitlements.grants,
        not_after=entitlements.not_after,
    )

    # The hash the key would have been built from is the same on both sides of the bound,
    # which is the whole reason the clock has to be consulted separately.
    assert entitlements.ent_hash() == same_set_before_the_bound.ent_hash()

    with pytest.raises(NotCacheableError):
        CallerKey.of(
            Resolved(
                entitlements=entitlements,
                ent_hash=entitlements.ent_hash(),
                grants_version=1,
                from_cache=False,
            ),
            policy_epoch=3,
            now=NOW,
        )


# --------------------------------------------- a plan is re-projected per caller (M6.2.2)

REGISTRY = (
    ToolDefinition(
        name="laravel.read_client",
        description="a tool",
        entity="client",
        required_capability="read:client.name",
        side_effect=SideEffect.NONE,
    ),
    ToolDefinition(
        name="xero.read_invoice",
        description="a tool",
        entity="client",
        required_capability="read:invoice.total",
        side_effect=SideEffect.NONE,
    ),
)

CEILING = AgentCeiling(agent_id="a_reporting", allowed_tools=frozenset(t.name for t in REGISTRY))


@given(capabilities=CAPABILITY_SETS)
def test_a_replayed_plan_never_names_a_tool_outside_the_callers_own_catalogue(
    capabilities: frozenset[str],
) -> None:
    """Whatever the plan named, the replay is a subset of what this caller may reach.

    Stated over generated reach so that it holds for the caller who reaches everything, the
    caller who reaches nothing, and every caller in between. Delete this and `replay` can be
    changed to return the stored list whenever the catalogue is non-empty, which passes any
    single hand-written pair where the second caller reaches nothing at all.
    """
    entitlements = EntitlementSet(
        principal_id="u_b",
        grants=tuple(
            Grant(
                capability=Capability(value=value),
                scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),)),
            )
            for value in sorted(capabilities)
        ),
    )
    catalogue = project(REGISTRY, entitlements, CEILING, now=NOW)
    plan = CachedPlan(key="plan:x", tool_ids=("laravel.read_client", "xero.read_invoice"))

    replayed = replay(plan, catalogue)

    assert set(replayed.tool_ids) <= set(catalogue.names)


def test_each_cache_addresses_its_own_keyspace_by_name_and_by_digest() -> None:
    """One caller asking one question produces three keys that separate two ways over.

    The prefix separates them for anybody reading the keyspace, and the digest separates
    them for the store, which is what matters when several services share one Valkey. Both
    are asserted because either alone is enough for the keys to differ today and neither
    alone survives the other being dropped: the digests carry their own namespace strings,
    and a prefix removed for tidiness would leave three bare digests in one keyspace with
    only the stored value's type to tell them apart.
    """
    who = reach("u_a", frozenset({"read:client.name"}))
    plan = plan_key(Q, who, agent_config_hash="cfg")
    retrieval = retrieval_key(Q, who, departments=("web",), corpus_epoch=0)
    answer = answer_key(Q, who, agent_config_hash="cfg", source_epochs={})

    assert plan.startswith("plan:")
    assert retrieval.startswith("retr:")
    assert len({plan, retrieval, answer}) == 3
    assert len({plan.split(":", 1)[1], retrieval.split(":", 1)[1], answer}) == 3


# ------------------------------------------------- no semantic answer caching (M6.2.6)


def test_no_module_on_the_answer_path_can_match_one_question_against_another() -> None:
    """Semantic caching would answer person B out of an entry computed for person A.

    Delete this and the shortest route from here to that is an import and four lines, in a
    module whose docstring argues against it and whose tests would all still pass.
    """
    assert_no_similarity_search(cache_key_module)
    assert_no_similarity_search(answer_cache_module)
    assert_no_similarity_search(caches_module)
    assert_answer_types_hold_no_vector(CacheKeyParts, CachedAnswer)


def test_nothing_in_the_cache_layer_counts_what_one_person_could_not_see() -> None:
    """Never a count of hidden items, and an operations counter is still a count.

    A hit rate per principal says how much somebody asks and how much of it the system had
    to work for, and against a colleague on the same questions it says how differently the
    two of them are treated. Delete this and the leak arrives through a dashboard rather
    than through an answer, which is the direction nobody is watching.
    """
    assert_counters_name_no_principal(CacheHealth)
    assert not set(caches_module.ReplayedPlan.__dataclass_fields__) - {"tool_ids"}
