"""The four caches of M6.2, the reach their keys are built from, and the banned one.

No Valkey and no PostgreSQL are contacted anywhere in this file. The store half is exercised
against the same three-method fake `tests/unit/test_cache.py` uses, which is the whole reason
`brain.cache.ValkeyClient` is a narrow protocol.

Every entitlement set below is built from real `Grant`s rather than from a hash somebody
typed, and that is deliberate rather than thorough. A test that hands a key builder the
string "ent-aaa" proves the builder puts a string in a key; it cannot prove that two people
with different permissions get different strings, which is the only property that matters
here. The sets differ by one capability, or by one department, or by one time bound, and each
difference is asserted to move the key on its own.

Task ids: M6.2.1, M6.2.2, M6.2.3, M6.2.4, M6.2.5, M6.2.6
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import TypeAdapter

from brain.cache import (
    CacheHealth,
    ValkeyRecordCache,
    embedding_cache,
    freshness_cache,
    plan_cache,
    retrieval_cache,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import SideEffect, ToolDefinition
from brain.core.fast_path import MAX_TEMPLATE_CHARS
from brain.core.scope import Clause, Op, Scope
from brain.gate import answer_cache as answer_cache_module
from brain.gate import cache_key as cache_key_module
from brain.gate import caches as caches_module
from brain.gate.cache_key import KEY_VERSION as ANSWER_KEY_VERSION
from brain.gate.cache_key import (
    CachedAnswer,
    CacheKeyParts,
    NotCacheableError,
    cache_key,
    normalise_question,
)
from brain.gate.caches import (
    EMBEDDING_TTL_SECONDS,
    FRESHNESS_TTL_SECONDS,
    MAX_PLAN_TOOLS,
    MAX_QUESTION_CHARS,
    MAX_RETRIEVAL_REFERENCES,
    PLAN_TTL_SECONDS,
    RETRIEVAL_TTL_SECONDS,
    TTL_SECONDS,
    CachedEmbedding,
    CachedFreshness,
    CachedPlan,
    CachedRetrieval,
    CacheLayerError,
    CallerKey,
    ReplayedPlan,
    answer_key,
    assert_answer_types_hold_no_vector,
    assert_counters_name_no_principal,
    assert_no_similarity_search,
    content_hash,
    digest_of,
    embedding_key,
    freshness_key,
    plan_key,
    replay,
    retrieval_key,
    source_epochs,
    ttl_invariants,
)
from brain.gate.catalogue import AgentCeiling, ProjectedCatalogue, project
from brain.gate.fast_lane import MAX_SLOT_VALUE_CHARS
from brain.gate.resolve import Resolved
from brain.knowledge.search import MAX_CANDIDATE_DEPTH

NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)

QUESTION = "How many hours are left on Acme"
AGENT_CONFIG = "cfg-e3a1"

#: A length at which the text somebody pasted is a document rather than a question. Held as
#: a literal here so it does not move when `MAX_QUESTION_CHARS` does, which is the whole
#: point of the assertion that uses it.
A_DOCUMENT_NOT_A_QUESTION = 100_000


# ------------------------------------------------------------------ the fakes


class FakeValkey:
    """The three commands the client uses, in memory. Opens no socket."""

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


class _NullStore:
    """An `AnswerStore` that would notice if it were written to, and never is.

    `store_answer` is driven here only to prove it declines before reaching a store. A fake
    that silently accepted a write would let the test pass whether it declined or not.
    """

    def __init__(self) -> None:
        self.writes = 0

    def get(self, key: str) -> CachedAnswer | None:
        return None

    def set(self, key: str, value: CachedAnswer, ttl_seconds: int) -> None:
        self.writes += 1


class DeadValkey:
    """A client whose every command raises, standing in for Valkey being unreachable."""

    def get(self, name: str) -> bytes | None:
        raise OSError("connection refused")

    def setex(self, name: str, time: int, value: bytes) -> object:
        raise OSError("connection refused")

    def ping(self) -> object:
        raise OSError("connection refused")


# ------------------------------------------------------------------ builders


def grant(capability: str, department: str = "web") -> Grant:
    return Grant(
        capability=Capability(value=capability),
        scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value=department),)),
    )


def ents(
    principal_id: str,
    *capabilities: str,
    department: str = "web",
    not_after: datetime | None = None,
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(grant(c, department) for c in capabilities),
        not_after=not_after,
    )


def resolved(entitlements: EntitlementSet, *, grants_version: int = 3) -> Resolved:
    """A `Resolved` the way `resolve` builds one: the hash computed from the set beside it."""
    return Resolved(
        entitlements=entitlements,
        ent_hash=entitlements.ent_hash(),
        grants_version=grants_version,
        from_cache=False,
    )


def caller(
    entitlements: EntitlementSet, *, policy_epoch: int = 4, grants_version: int = 3
) -> CallerKey:
    return CallerKey.of(
        resolved(entitlements, grants_version=grants_version),
        policy_epoch=policy_epoch,
        now=NOW,
    )


def tool(name: str, capability: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="a tool",
        entity="client",
        required_capability=capability,
        side_effect=SideEffect.NONE,
    )


REGISTRY = (
    tool("laravel.read_client", "read:client.name"),
    tool("xero.read_invoice", "read:invoice.total"),
    tool("freshdesk.read_ticket", "read:ticket.status"),
)

CEILING = AgentCeiling(
    agent_id="a_reporting",
    allowed_tools=frozenset(t.name for t in REGISTRY),
)


def catalogue_for(entitlements: EntitlementSet) -> ProjectedCatalogue:
    return project(REGISTRY, entitlements, CEILING, now=NOW)


def module_at(source: str, path: Path) -> ModuleType:
    """A module object over a source file, never executed.

    `assert_no_similarity_search` reads the source through `inspect.getsource`, which needs
    a module with a `__file__` that exists and nothing else. Not executing it is what lets a
    fixture import numpy on a machine where numpy is not installed, which is exactly the
    module the check has to be proved against.
    """
    path.write_text(source, encoding="utf-8", newline="\n")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    return importlib.util.module_from_spec(spec)


# ------------------------------------------------- the caller key and the answer (M6.2.1)


def test_two_callers_with_different_reach_never_share_an_answer_key():
    """The property the whole layer exists for.

    Delete this and a key builder that dropped the entitlement hash passes everything else
    in this file: every other test here varies one thing at a time and would still see its
    own component move the key. This is the one that says two different people asking the
    same words in the same second get different keys.
    """
    broad = caller(ents("u_alice", "read:client.name", "read:invoice.total"))
    narrow = caller(ents("u_bob", "read:client.name"))
    epochs = {"laravel.client": 44}

    mine = answer_key(QUESTION, broad, agent_config_hash=AGENT_CONFIG, source_epochs=epochs)
    theirs = answer_key(QUESTION, narrow, agent_config_hash=AGENT_CONFIG, source_epochs=epochs)

    assert mine != theirs


def test_two_callers_with_identical_reach_share_one_answer_key():
    """The positive half, and it is not a nicety.

    A key builder that mixed the principal id in would pass the test above and fail this
    one, and it would silently reduce the answer cache to one entry per person, which is a
    cache that costs a lookup and never hits. Deleting this leaves nothing asserting that
    the sharing `ent_hash` exists for still happens.
    """
    mine = caller(ents("u_alice", "read:client.name"), grants_version=3)
    theirs = caller(ents("u_bob", "read:client.name"), grants_version=91)
    epochs = {"laravel.client": 44}

    assert answer_key(
        QUESTION, mine, agent_config_hash=AGENT_CONFIG, source_epochs=epochs
    ) == answer_key(QUESTION, theirs, agent_config_hash=AGENT_CONFIG, source_epochs=epochs)


def test_a_caller_past_their_time_bound_can_key_no_cache_at_all():
    """The half of M6.2.1 a digest of the entitlement set cannot do.

    `ent_hash` puts `not_after` in the identity, which is constant across the boundary in
    time: the contractor's set is byte-identical a minute before their bound and a minute
    after, so the key matches exactly and the answer stored before is served after. Delete
    this and the expiry check in `CallerKey.of` can be removed with every other test in this
    file still green, because every one of them uses a caller who has not expired.
    """
    expired = ents("u_contractor", "read:client.name", not_after=NOW - timedelta(seconds=1))

    with pytest.raises(NotCacheableError):
        CallerKey.of(resolved(expired), policy_epoch=4, now=NOW)


def test_a_caller_inside_their_time_bound_is_keyed_normally():
    """The sibling of the refusal above, so the guard is not satisfied by refusing everybody.

    Delete this and `CallerKey.of` could raise for every contractor, expired or not, and the
    only symptom would be that nothing is ever cached for anybody with a time bound.
    """
    live = ents("u_contractor", "read:client.name", not_after=NOW + timedelta(seconds=1))

    assert CallerKey.of(resolved(live), policy_epoch=4, now=NOW).principal_id == "u_contractor"


def test_the_expiry_refusal_is_the_exception_the_answer_store_already_declines_on():
    """`store_answer` declines on `NotCacheableError` and nothing else.

    Delete this and somebody gives the expiry its own exception type, which reads tidier and
    means `store_answer` stops declining: the call raises out of a path whose whole contract
    is that declining to cache is normal and returns None. Asserted as the identity of the
    type rather than as a subclass check, because a subclass of something else that happened
    to be caught would satisfy a looser assertion.
    """
    expired = ents("u_contractor", "read:client.name", not_after=NOW)

    with pytest.raises(NotCacheableError) as raised:
        CallerKey.of(resolved(expired), policy_epoch=4, now=NOW)

    assert type(raised.value) is NotCacheableError

    # And the other half of the claim, driven rather than asserted about: `store_answer`
    # declines on that exception, returns None rather than raising it at the caller, and
    # does not reach the store on the way. The store counts its writes so that a decline
    # and a successful store are told apart by something other than the return value.
    store = _NullStore()
    declined = answer_cache_module.store_answer(
        "how many tickets are open right now",
        "some answer",
        ent_hash="ent-aaa",
        agent_config_hash=AGENT_CONFIG,
        policy_epoch=1,
        source_epochs={},
        store=store,
        now=NOW,
    )

    assert declined is None
    assert store.writes == 0


def test_an_entitlement_hash_that_is_not_the_sets_own_is_refused():
    """The catastrophic case, ruled out by one comparison.

    A `Resolved` carrying one caller's hash beside another caller's set produces a key that
    is internally consistent and belongs to nobody, and everything downstream passes. Delete
    this and the comparison in `CallerKey.of` can go, and the only way the mistake surfaces
    is one person reading another person's answer.
    """
    alice = ents("u_alice", "read:client.name")
    bob = ents("u_bob", "read:invoice.total")

    with pytest.raises(CacheLayerError):
        CallerKey.of(
            Resolved(
                entitlements=alice,
                ent_hash=bob.ent_hash(),
                grants_version=3,
                from_cache=False,
            ),
            policy_epoch=4,
            now=NOW,
        )


def test_a_caller_key_cannot_be_built_outside_of_the_checked_constructor():
    """The structural half: the checks above cannot be walked around by a plain constructor.

    Delete this and `CallerKey(principal_id=..., ent_hash=..., ...)` becomes available at
    every call site, which is a key built from four loose values with neither the time bound
    nor the hash ever looked at. That is the arrangement `CallerKey` exists to replace.
    """
    with pytest.raises(CacheLayerError):
        CallerKey(principal_id="u_alice", ent_hash="abc", grants_version=1, policy_epoch=1)


def test_a_caller_key_refuses_a_reach_it_could_not_tell_two_people_apart_by():
    """The invariants on the value itself, and only the ones that can actually be reached.

    An unnamed principal keys the retrieval cache for nobody, and a negative counter is a
    version or an epoch that came from somewhere other than the database. Both arrive through
    `of`, because `Resolved` is a plain dataclass and `EntitlementSet.principal_id` carries no
    minimum length. Delete this and either can be removed with the suite green.

    There is deliberately nothing here about an empty entitlement hash. That check existed
    and a mutation removing it survived: `of` refuses unless the hash equals
    `EntitlementSet.ent_hash()`, which is never empty, so the branch could not fire and the
    guard was removed rather than given a test it could only pass vacuously. What the removal
    rests on is the test below.
    """
    alice = ents("u_alice", "read:client.name")
    unnamed = EntitlementSet(principal_id="", grants=alice.grants)

    with pytest.raises(CacheLayerError):
        CallerKey.of(resolved(unnamed), policy_epoch=1, now=NOW)
    with pytest.raises(CacheLayerError):
        CallerKey.of(
            Resolved(
                entitlements=alice, ent_hash=alice.ent_hash(), grants_version=-1, from_cache=False
            ),
            policy_epoch=1,
            now=NOW,
        )
    with pytest.raises(CacheLayerError):
        caller(alice, policy_epoch=-1)


def test_the_hash_a_caller_key_is_built_from_is_never_empty():
    """What the removed empty-hash guard was replaced by: the property that made it dead.

    `CallerKey` no longer checks for an empty entitlement hash, because `of` accepts only a
    hash equal to the set's own and `EntitlementSet.ent_hash` is a fixed-width slice of a
    sha256 that no set can make empty, not even one holding no grants at all. Delete this and
    the removal rests on nothing: a change to `ent_hash` that could return an empty string
    would put every caller under one key with no test anywhere objecting.
    """
    assert EntitlementSet(principal_id="u_alice").ent_hash()
    assert len(EntitlementSet(principal_id="u_alice").ent_hash()) == 32
    assert len(ents("u_alice", "read:client.name").ent_hash()) == 32


def test_a_key_needs_a_question_to_be_built_from():
    """An empty question is one key every empty request would collide under.

    Delete this and a caller whose question was stripped to nothing by an upstream step gets
    a key, shares it with every other such caller, and the answer under it is whoever's
    arrived first.
    """
    who = caller(ents("u_alice", "read:client.name"))

    with pytest.raises(CacheLayerError):
        plan_key("   ", who, agent_config_hash=AGENT_CONFIG)
    with pytest.raises(CacheLayerError):
        retrieval_key("", who, departments=("web",), corpus_epoch=1)


def test_a_revoked_grant_moves_the_answer_key():
    """Revocation is the deletion of a grant, and it has to invalidate.

    Delete this and nothing asserts that the *content* of the entitlement set reaches the
    key. The two-callers test above varies the principal as well; this varies only the
    grants, on one principal, which is what a revocation actually is.
    """
    before = caller(ents("u_alice", "read:client.name", "read:invoice.total"))
    after = caller(ents("u_alice", "read:client.name"))
    epochs = {"laravel.client": 44}

    assert answer_key(
        QUESTION, before, agent_config_hash=AGENT_CONFIG, source_epochs=epochs
    ) != answer_key(QUESTION, after, agent_config_hash=AGENT_CONFIG, source_epochs=epochs)


def test_a_moved_policy_epoch_moves_every_key_this_module_builds():
    """The global invalidation lever has to reach all four caches, not just the answer.

    Delete this and a key function that forgot `reach_fields` still passes its own
    reach test, because the entitlement hash alone would move it. The epoch is the lever
    that moves when nobody's grants have changed at all, which is what a policy change is.
    """
    before = caller(ents("u_alice", "read:client.name"), policy_epoch=4)
    after = caller(ents("u_alice", "read:client.name"), policy_epoch=5)

    assert answer_key(
        QUESTION, before, agent_config_hash=AGENT_CONFIG, source_epochs={}
    ) != answer_key(QUESTION, after, agent_config_hash=AGENT_CONFIG, source_epochs={})
    assert plan_key(QUESTION, before, agent_config_hash=AGENT_CONFIG) != plan_key(
        QUESTION, after, agent_config_hash=AGENT_CONFIG
    )
    assert retrieval_key(QUESTION, before, departments=("web",), corpus_epoch=2) != retrieval_key(
        QUESTION, after, departments=("web",), corpus_epoch=2
    )


def test_a_moved_source_epoch_moves_the_answer_key():
    """A source refreshing has to orphan the answers built on it.

    Delete this and the freshness cache below computes an epoch nothing carries anywhere,
    which is the state `brain.cache` describes as a version nobody reads: not invalidation,
    a comment.
    """
    who = caller(ents("u_alice", "read:client.name"))

    assert answer_key(
        QUESTION, who, agent_config_hash=AGENT_CONFIG, source_epochs={"laravel.client": 44}
    ) != answer_key(
        QUESTION, who, agent_config_hash=AGENT_CONFIG, source_epochs={"laravel.client": 45}
    )


def test_the_shared_digest_agrees_with_the_answer_keys_own_builder():
    """The two length-prefixed digests in this package produce the same string.

    `digest_of` restates the rule `cache_key.cache_key` implements, and the duplication is
    argued rather than accidental. Delete this and the copies are free to drift: one of them
    starts joining on a separator, `("ab", "c")` and `("a", "bc")` collide in whichever one
    changed, and nothing anywhere compares them again.
    """
    parts = CacheKeyParts(
        question=QUESTION,
        ent_hash="ent-aaa",
        agent_config_hash=AGENT_CONFIG,
        policy_epoch=7,
        source_epochs={"laravel.client": 44, "xero.invoice": 2},
    )
    same_fields = (
        str(ANSWER_KEY_VERSION),
        normalise_question(QUESTION),
        "ent-aaa",
        AGENT_CONFIG,
        "7",
        "laravel.client=44;xero.invoice=2",
    )

    assert digest_of(same_fields) == cache_key(parts)


def test_a_digest_over_no_fields_is_refused():
    """An empty field list is one constant standing in for every key.

    Delete this and a key function whose arguments all evaporated returns a valid-looking
    digest that every caller shares, which is the widest possible failure returning the
    narrowest possible symptom.
    """
    with pytest.raises(CacheLayerError):
        digest_of(())


def test_the_question_bound_admits_a_question_and_refuses_a_document():
    """The bound is asserted against figures from elsewhere, never against itself.

    This test read `"a" * (MAX_QUESTION_CHARS + 1)` first, and a mutation moving the constant
    to forty million survived it: the input moved with the constant and both sides of the
    comparison stayed in step, which is exactly the defect `CLAUDE.md` records against
    `throttle.RETRY_AFTER_WHEN_UNSTATED`. It now refuses a fixed literal, and holds the
    constant inside the band its own comment describes, using the fast lane's two published
    figures for the lower end.

    Delete this and the bound can be moved to any value at all, because nothing else in this
    file passes a question long enough to reach it.
    """
    who = caller(ents("u_alice", "read:client.name"))

    with pytest.raises(CacheLayerError):
        plan_key("a" * A_DOCUMENT_NOT_A_QUESTION, who, agent_config_hash=AGENT_CONFIG)

    assert MAX_QUESTION_CHARS > MAX_TEMPLATE_CHARS + MAX_SLOT_VALUE_CHARS
    assert MAX_QUESTION_CHARS < A_DOCUMENT_NOT_A_QUESTION
    assert plan_key(QUESTION, who, agent_config_hash=AGENT_CONFIG)


# ------------------------------------------------------------- the plan cache (M6.2.2)


def test_a_cached_plan_holds_tool_ids_and_has_nowhere_to_put_a_definition():
    """M6.2.2 says ids only, and this is what makes that a shape rather than a habit.

    A tool definition carries a description written for a model, which is the sentence that
    teaches a model that a capability exists, and arguments carry record ids. Delete this
    and a field called `steps` or `tools` holding whole definitions can be added with every
    other plan test still green, because none of them looks at what a plan may contain.
    """
    assert {f.name for f in CachedPlan.__dataclass_fields__.values()} == {"key", "tool_ids"}
    assert CachedPlan.__dataclass_fields__["tool_ids"].type == "tuple[str, ...]"


def test_a_plan_naming_a_tool_the_caller_cannot_reach_replays_to_nothing():
    """The disclosure a plan cache invites, refused.

    Alice reaches two tools and Bob one. Replaying Alice's plan for Bob must not hand Bob a
    plan naming a tool that is absent from his catalogue. Delete this and `replay` can
    return the stored list unchanged, which is the cache handing one person another
    person's tool catalogue.
    """
    alice = ents("u_alice", "read:client.name", "read:invoice.total")
    bob = ents("u_bob", "read:client.name")
    plan = CachedPlan(
        key=plan_key(QUESTION, caller(alice), agent_config_hash=AGENT_CONFIG),
        tool_ids=("laravel.read_client", "xero.read_invoice"),
    )

    assert replay(plan, catalogue_for(alice)).tool_ids == (
        "laravel.read_client",
        "xero.read_invoice",
    )
    assert replay(plan, catalogue_for(bob)).tool_ids == ()
    assert replay(plan, catalogue_for(bob)).usable is False


def test_a_plan_is_emptied_rather_than_trimmed_to_what_the_caller_can_reach():
    """Trimming is the tempting half-measure and it is the worse answer.

    Bob reaches one of the plan's two tools. A trimmed replay would hand him a plan that has
    quietly lost the step that was going to answer the question, and the answer comes back
    confidently incomplete. Delete this and a one-line change from `if not all(...)` to a
    comprehension over the survivors passes the test above, because Bob would still not see
    the tool he cannot reach.
    """
    bob = ents("u_bob", "read:client.name")
    plan = CachedPlan(
        key="plan:whatever",
        tool_ids=("laravel.read_client", "xero.read_invoice"),
    )

    replayed = replay(plan, catalogue_for(bob))

    assert replayed.tool_ids == ()
    assert "laravel.read_client" not in replayed.tool_ids


def test_a_replayed_plan_keeps_the_plans_order_and_not_the_catalogues():
    """A plan is a sequence, and the catalogue is sorted by name for prompt caching.

    Delete this and `replay` can be written as a filter over `catalogue.names`, which is
    one line shorter and runs the same tools in alphabetical order. The steps would be the
    right steps in the wrong sequence, which nothing downstream is able to notice.
    """
    alice = ents("u_alice", "read:client.name", "read:invoice.total", "read:ticket.status")
    plan = CachedPlan(
        key="plan:whatever",
        tool_ids=("xero.read_invoice", "freshdesk.read_ticket", "laravel.read_client"),
    )

    assert replay(plan, catalogue_for(alice)).tool_ids == plan.tool_ids
    assert catalogue_for(alice).names != plan.tool_ids


def test_a_replayed_plan_carries_no_count_of_what_it_dropped():
    """Never a count of hidden items, including by subtraction.

    The number of tools that did not survive a replay is the number of tools this caller may
    not reach, which is a fact about a catalogue they were never shown. Delete this and a
    `dropped` or `skipped` field is added for a trace, and the trace becomes a per-person
    inventory of the tool registry.
    """
    names = {f.name for f in ReplayedPlan.__dataclass_fields__.values()}

    assert names == {"tool_ids"}


def test_a_plan_key_moves_with_the_caller_and_with_the_agent():
    """Both walls: the key narrows and `replay` re-projects, rather than one or the other.

    Delete this and the plan key can be reduced to the question alone on the argument that
    the projection is the real guard. Every caller then shares one entry, every reuse is a
    near-certain empty replay, and the cache costs a lookup and returns nothing.
    """
    alice = caller(ents("u_alice", "read:client.name", "read:invoice.total"))
    bob = caller(ents("u_bob", "read:client.name"))

    assert plan_key(QUESTION, alice, agent_config_hash=AGENT_CONFIG) != plan_key(
        QUESTION, bob, agent_config_hash=AGENT_CONFIG
    )
    assert plan_key(QUESTION, alice, agent_config_hash=AGENT_CONFIG) != plan_key(
        QUESTION, alice, agent_config_hash="cfg-other"
    )
    assert plan_key(QUESTION, alice, agent_config_hash=AGENT_CONFIG) == plan_key(
        f"  {QUESTION.upper()}  ", alice, agent_config_hash=AGENT_CONFIG
    )


def test_a_plan_key_needs_an_agent_configuration_hash():
    """The same question through a changed agent reaches for different tools.

    Delete this and an empty configuration hash is accepted, so every version of an agent
    shares one plan and an agent edited to reach fewer tools keeps replaying the old plan
    until the TTL expires.
    """
    with pytest.raises(CacheLayerError):
        plan_key(QUESTION, caller(ents("u_alice", "read:client.name")), agent_config_hash="")


def test_a_cached_plan_refuses_a_name_that_is_not_a_tool_id():
    """A plan arrives from a shared store and is checked as though it came from outside.

    Delete this and anything that can write to the keyspace can put a string into a plan,
    which reaches `replay` as a name to look up and reaches a log line as itself.
    """
    with pytest.raises(CacheLayerError):
        CachedPlan(key="plan:whatever", tool_ids=("not a tool id",))
    with pytest.raises(CacheLayerError):
        CachedPlan(key="plan:whatever", tool_ids=("laravel.read_client", "laravel.read_client"))
    with pytest.raises(CacheLayerError):
        CachedPlan(key="plan:whatever", tool_ids=())
    with pytest.raises(CacheLayerError):
        CachedPlan(key="", tool_ids=("laravel.read_client",))
    with pytest.raises(CacheLayerError):
        CachedPlan(
            key="plan:whatever",
            tool_ids=tuple(f"laravel.read_client{n}" for n in range(MAX_PLAN_TOOLS + 1)),
        )


# --------------------------------------------------------- the retrieval cache (M6.2.3)


def test_two_callers_with_the_same_grants_do_not_share_a_retrieval_key():
    """The document plane's reach includes what a person owns, which no grant digest holds.

    `brain.knowledge.search.Reach` has a personal branch matching on `owner_id`, so two
    people with byte-identical grants still do not see the same documents: one of them owns
    drafts. Delete this and the retrieval key can be built from the entitlement hash alone,
    exactly like the answer key, and one person's unfinished notes are retrieved for
    another.
    """
    alice = caller(ents("u_alice", "read:knowledge"))
    bob = caller(ents("u_bob", "read:knowledge"))

    assert alice.ent_hash == bob.ent_hash
    assert retrieval_key(QUESTION, alice, departments=("web",), corpus_epoch=2) != retrieval_key(
        QUESTION, bob, departments=("web",), corpus_epoch=2
    )


def test_a_retrieval_key_is_the_same_whatever_order_the_departments_arrive_in():
    """The positive half, and it is what makes the cache hit at all.

    A department list gathered in a different order is the same reach. Delete this and the
    sort can be dropped, at which point the same caller misses their own entry depending on
    the order `reach_for` happened to walk the registry in, and the symptom is a hit rate
    that nobody can reproduce.
    """
    who = caller(ents("u_alice", "read:knowledge"))

    assert retrieval_key(
        QUESTION, who, departments=("web", "finance"), corpus_epoch=2
    ) == retrieval_key(QUESTION, who, departments=("finance", "web"), corpus_epoch=2)


def test_a_department_added_to_a_callers_reach_moves_their_retrieval_key():
    """The department list is the grants intersected with a registry that changes on its own.

    A department added to the registry widens the list while the grants stand still, so the
    entitlement hash does not move and the reach does. Delete this and the department list
    can be dropped from the key on the argument that the hash covers it, which it does not.
    """
    who = caller(ents("u_alice", "read:knowledge"))

    assert retrieval_key(QUESTION, who, departments=("web",), corpus_epoch=2) != retrieval_key(
        QUESTION, who, departments=("web", "finance"), corpus_epoch=2
    )


def test_a_moved_corpus_epoch_moves_the_retrieval_key():
    """An upload or a re-index has to orphan the reference lists built before it.

    Delete this and a cached list of chunk ids outlives the chunks it names, so an answer
    is composed from citations that resolve to nothing, and a citation nobody can resolve is
    a citation nobody checks.
    """
    who = caller(ents("u_alice", "read:knowledge"))

    assert retrieval_key(QUESTION, who, departments=("web",), corpus_epoch=2) != retrieval_key(
        QUESTION, who, departments=("web",), corpus_epoch=3
    )


def test_a_retrieval_key_refuses_something_that_is_not_a_department():
    """The department list is split on a comma by the second wall's own policy.

    `know.chunk` constrains a department to a slug so `string_to_array(..., ',')` cannot
    split one name into two. Delete this and a name carrying a comma reaches a key, and the
    same value reaching the session setting would split into departments nobody granted.
    """
    who = caller(ents("u_alice", "read:knowledge"))

    with pytest.raises(CacheLayerError):
        retrieval_key(QUESTION, who, departments=("web,finance",), corpus_epoch=2)
    with pytest.raises(CacheLayerError):
        retrieval_key(QUESTION, who, departments=("web", "web"), corpus_epoch=2)
    with pytest.raises(CacheLayerError):
        retrieval_key(QUESTION, who, departments=("web",), corpus_epoch=-1)

    assert retrieval_key(QUESTION, who, departments=("web", "finance"), corpus_epoch=0)


def test_a_cached_retrieval_holds_references_and_no_passages():
    """A chunk's body is the document's content, and Valkey has no row-level security.

    The policy in 0009 protects `know.chunk`; nothing protects a cache value. Delete this
    and a `bodies` or `passages` field is added to save the re-read, and the corpus now has
    a second copy sitting outside the wall that was written to guard it.
    """
    assert {f.name for f in CachedRetrieval.__dataclass_fields__.values()} == {"key", "chunk_ids"}

    with pytest.raises(CacheLayerError):
        CachedRetrieval(key="retr:whatever", chunk_ids=("a", "a"))
    with pytest.raises(CacheLayerError):
        CachedRetrieval(key="retr:whatever", chunk_ids=("has a space",))
    with pytest.raises(CacheLayerError):
        CachedRetrieval(key="", chunk_ids=("c1",))


def test_the_retrieval_cap_is_what_a_query_could_have_produced():
    """The cap is pinned to `brain.knowledge.search`'s own leg ceiling, not chosen.

    A list longer than both legs together came from somewhere other than a query this system
    ran. Delete this and the number is a bare 500 that a test building its input from it
    would move with, which is the defect the question bound above was caught by.
    """
    assert MAX_CANDIDATE_DEPTH <= MAX_RETRIEVAL_REFERENCES <= 2 * MAX_CANDIDATE_DEPTH

    with pytest.raises(CacheLayerError):
        CachedRetrieval(
            key="retr:whatever",
            chunk_ids=tuple(f"c{n}" for n in range(2 * MAX_CANDIDATE_DEPTH + 1)),
        )


def test_a_retrieval_that_found_nothing_is_still_a_cacheable_result():
    """An empty result is the caller's own empty, and re-running it costs two queries.

    Delete this and an empty `chunk_ids` gets refused as a mistake, so the most expensive
    question in the system, the one that matches nothing under a narrow reach, is the one
    that is never cached.
    """
    assert CachedRetrieval(key="retr:whatever", chunk_ids=()).chunk_ids == ()


# --------------------------------------------------------- the embedding cache (M6.2.4)


def test_two_different_texts_never_share_an_embedding_key():
    """The whole of what an embedding cache has to get right.

    Delete this and a key built from, say, the first hundred characters passes every other
    embedding test here, and two documents with a shared preamble get one another's vectors.
    """
    assert embedding_key("the quick brown fox", model="m@r:1536") != embedding_key(
        "the quick brown cat", model="m@r:1536"
    )


def test_the_same_text_through_a_different_model_is_a_different_key():
    """A distance between two models' vectors is a number and not a distance.

    Delete this and a rebuild reads the old vectors back under the new model's keys, which
    is the corpus silently holding two models: the failure `brain.knowledge.embedding` is
    written against, arriving through the cache instead of through the table.
    """
    assert embedding_key("the quick brown fox", model="m@r1:1536") != embedding_key(
        "the quick brown fox", model="m@r2:1536"
    )


def test_an_embedding_key_is_the_same_for_every_caller():
    """The exception to the reach rule, asserted rather than assumed.

    An entry is reachable only by holding the exact content, so a lookup is a proof of
    possession. Delete this and somebody adds the entitlement hash to the key for
    consistency, at which point every person embeds the same handbook separately and the
    cache stops paying for itself.
    """
    assert "u_alice" not in embedding_key("some text", model="m@r:1536")
    assert embedding_key("some text", model="m@r:1536") == embedding_key(
        "some text", model="m@r:1536"
    )


def test_content_is_hashed_exactly_where_a_question_is_normalised():
    """Two documents differing only in whitespace are two documents.

    `normalise_question` collapses whitespace and case because two people typing a question
    differently meant the same question; content is not a question, and folding it here
    hands back one document's vector for another's text. Delete this and `content_hash`
    can call `normalise_question` for symmetry, which reads tidy and is wrong.
    """
    assert content_hash("Acme  Ltd") != content_hash("Acme Ltd")
    assert content_hash("acme ltd") != content_hash("Acme Ltd")
    assert normalise_question("Acme  Ltd") == normalise_question("acme ltd")


def test_an_embedding_key_refuses_empty_content_and_an_unnamed_model():
    """Both are a key that several unrelated values would collide under.

    Delete this and an empty content hash becomes one constant every empty input shares,
    and an unnamed model makes every model's vectors reachable under one key.
    """
    with pytest.raises(CacheLayerError):
        embedding_key("", model="m@r:1536")
    with pytest.raises(CacheLayerError):
        embedding_key("some text", model="")


def test_a_stored_embedding_refuses_to_exist_without_the_model_that_produced_it():
    """The value's own guard, which is not the key builder's.

    A mutation removing this survived a first pass, because the only test pointed at an
    unnamed model was the one above and it exercises `embedding_key` rather than the type.
    The two are different doors into the same failure: a vector whose model nobody recorded
    cannot be compared with anything, and one arriving from the store never went through a
    key builder at all.
    """
    with pytest.raises(CacheLayerError):
        CachedEmbedding(key="emb:x", model="", values=(0.1, 0.2))
    with pytest.raises(CacheLayerError):
        CachedEmbedding(key="emb:x", model="m@r:1536", values=())
    with pytest.raises(CacheLayerError):
        CachedEmbedding(key="", model="m@r:1536", values=(0.1,))

    assert CachedEmbedding(key="emb:x", model="m@r:1536", values=(0.1,)).values == (0.1,)


def test_the_embedding_cache_has_nowhere_to_put_the_text_it_embedded():
    """The structural half of M6.2.6, seen from the embedding side.

    An entry cannot be walked back to the words it came from and there is no index over its
    values, so "which stored question is closest to this one" is not expressible against it.
    Delete this and a `content` or `question` field is added to make the cache debuggable,
    and the store becomes exactly the corpus of question vectors the prohibition is about.
    """
    names = {f.name for f in CachedEmbedding.__dataclass_fields__.values()}

    assert names == {"key", "model", "values"}
    assert not names & {"content", "question", "text", "payload", "answer"}


# ------------------------------------------------- the projection freshness cache (M6.2.5)


def reading(source: str, entity: str, seen: datetime) -> CachedFreshness:
    return CachedFreshness(
        key=freshness_key(source, entity), source=source, entity=entity, last_seen_at=seen
    )


def test_a_freshness_epoch_moves_when_the_projection_does():
    """The epoch is what invalidates every answer built on a source.

    Delete this and an epoch that is stuck, or that is a constant, passes construction: the
    reading looks fine, the answer key it feeds never changes, and every answer built on
    data that has since moved stays servable until its own TTL.
    """
    before = reading("xero", "invoice", NOW)
    after = reading("xero", "invoice", NOW + timedelta(microseconds=1))

    assert after.epoch > before.epoch


def test_a_freshness_reading_has_no_epoch_field_to_disagree_with_its_timestamp():
    """Two records of one fact, and the way they disagree cannot be noticed.

    A refresh moves `last_seen_at` and a caller forgets to bump a supplied integer, so every
    key built from it is unchanged. Delete this and `epoch` is added as a field with a
    default, which is the same failure with a nicer signature.
    """
    names = {f.name for f in CachedFreshness.__dataclass_fields__.values()}

    assert names == {"key", "source", "entity", "last_seen_at"}
    assert "epoch" not in names


def test_two_freshness_readings_for_one_source_and_entity_are_refused():
    """Silently keeping the last is how a stale reading wins by arriving second.

    Delete this and `source_epochs` takes whichever came last, which is an ordering nobody
    chose, and the loser might be the newer one.
    """
    with pytest.raises(CacheLayerError):
        source_epochs(
            [reading("xero", "invoice", NOW), reading("xero", "invoice", NOW - timedelta(days=1))]
        )


def test_source_epochs_names_the_entity_as_well_as_the_source():
    """`proj.record` is keyed by both, so one epoch per connector is the wrong grain.

    A Freshdesk company and a Xero contact are different companies. Delete this and the two
    entities of one connector fold into one epoch, which needs a rule for combining them,
    and the only safe rule is the newest, which invalidates answers that drew on neither.
    """
    epochs = source_epochs(
        [reading("xero", "invoice", NOW), reading("xero", "contact", NOW - timedelta(hours=1))]
    )

    assert set(epochs) == {"xero.invoice", "xero.contact"}
    assert epochs["xero.invoice"] > epochs["xero.contact"]


def test_a_naive_last_seen_at_is_refused():
    """A naive timestamp yields an epoch off by the deployment's offset from UTC.

    Delete this and a reading read back from somewhere that dropped the timezone produces a
    perfectly ordinary looking integer that is eight hours wrong, so an answer is invalidated
    or held on a boundary nobody can find.
    """
    with pytest.raises(CacheLayerError):
        reading("xero", "invoice", datetime(2026, 9, 7, 9, 0))


def test_a_stored_freshness_reading_checks_its_own_source_and_entity():
    """The value's guard, which is not the key builder's.

    A reading arriving from the store never went through `freshness_key`, so the type has to
    check what it carries. Delete this and a reading naming anything at all is accepted, and
    `source_epochs` puts that name straight into an answer key.
    """
    with pytest.raises(CacheLayerError):
        CachedFreshness(key="fresh:x", source="Xero Ltd", entity="invoice", last_seen_at=NOW)
    with pytest.raises(CacheLayerError):
        CachedFreshness(key="fresh:x", source="xero", entity="Invoice Line", last_seen_at=NOW)
    with pytest.raises(CacheLayerError):
        CachedFreshness(key="", source="xero", entity="invoice", last_seen_at=NOW)


def test_a_freshness_key_names_no_caller_and_refuses_a_name_that_is_not_an_object():
    """One reading serves everybody, which is the point, and its parts are checked.

    Delete this and a caller component creeps into the key, at which point the number the
    whole estate shares is read once per person, and a source name from outside reaches a
    key verbatim.
    """
    assert freshness_key("xero", "invoice") == "fresh:xero.invoice"

    with pytest.raises(CacheLayerError):
        freshness_key("Xero Ltd", "invoice")
    with pytest.raises(CacheLayerError):
        freshness_key("xero", "Invoice Line")


# ------------------------------------------------------------ the prohibition (M6.2.6)


def test_the_answer_types_have_nowhere_to_keep_a_questions_embedding():
    """M6.2.6 as a shape rather than as a rule somebody remembers.

    A rule saying "we do not cache semantically" holds until the first person who wants a
    better hit rate. Delete this and `CacheKeyParts` grows a `question_embedding` and
    `CachedAnswer` grows the neighbours it was matched against, and the answer cache becomes
    the thing this leaf exists to prohibit without anything going red.
    """
    assert_answer_types_hold_no_vector(CacheKeyParts, CachedAnswer, CachedPlan, CachedRetrieval)


def test_a_type_that_grew_a_vector_field_is_refused():
    """The checker above has to be able to fail, or it is a function that returns None.

    Two ways in, checked separately: a field named for a vector however it is typed, and a
    field typed as one however it is named. Delete this and the checker can be reduced to
    `pass` with the test above still green, which is the shape of a guard that guards
    nothing.
    """

    @dataclass(frozen=True)
    class NamedForOne:
        key: str
        embedding: str

    @dataclass(frozen=True)
    class TypedAsOne:
        key: str
        extra: tuple[float, ...]

    with pytest.raises(CacheLayerError):
        assert_answer_types_hold_no_vector(NamedForOne)
    with pytest.raises(CacheLayerError):
        assert_answer_types_hold_no_vector(TypedAsOne)


def test_no_module_on_the_answer_path_can_search_by_similarity():
    """Checked on the imports and the calls, not on the words, which are in a docstring.

    A test that greps for the prohibition passes against the file that states it, which is
    the trap `sweep_tool_registry` fell into. Delete this and any of these three modules can
    grow an import of a vector library, and the shortest path from here to semantic caching
    is one import and four lines.
    """
    assert_no_similarity_search(cache_key_module)
    assert_no_similarity_search(answer_cache_module)
    assert_no_similarity_search(caches_module)


def test_a_module_that_could_search_by_similarity_is_refused(tmp_path):
    """The positive control for the checker above, on both halves of what it reads.

    Delete this and `assert_no_similarity_search` can be an empty function: the three
    modules it is pointed at import nothing of the kind today, so the test above passes
    whatever the checker does.
    """
    importer = module_at("import numpy\n", tmp_path / "importer.py")
    caller_module = module_at(
        "def pick(a, b):\n    return cosine_similarity(a, b)\n", tmp_path / "caller.py"
    )
    innocent = module_at("def pick(a, b):\n    return sorted([a, b])\n", tmp_path / "innocent.py")

    with pytest.raises(CacheLayerError):
        assert_no_similarity_search(importer)
    with pytest.raises(CacheLayerError):
        assert_no_similarity_search(caller_module)
    assert_no_similarity_search(innocent)


# ----------------------------------------------- counters name nobody, and the lifetimes


def test_cache_counters_have_nowhere_to_name_a_principal():
    """A hit rate per person is a report about that person.

    How much somebody asks, how much of it misses, and by subtraction against a colleague
    how differently the system treats the two of them. Delete this and a `principal` field
    is added to `CacheHealth` for a dashboard, and the leak arrives through operations
    rather than through an answer.
    """
    assert_counters_name_no_principal(CacheHealth)


def test_a_counter_type_that_names_a_principal_is_refused():
    """The positive control, for the same reason as the one above it.

    Delete this and `assert_counters_name_no_principal` can be `pass`, because `CacheHealth`
    names nobody today and would keep passing an empty check for ever.
    """

    @dataclass
    class PerPerson:
        hits: int
        principal_id: str

    with pytest.raises(CacheLayerError):
        assert_counters_name_no_principal(PerPerson)


def test_every_cache_lifetime_holds_its_relation_to_the_others():
    """Each of these numbers is meaningless on its own and is pinned to something outside it.

    A test asserting `PLAN_TTL_SECONDS == 900` while importing the constant compares it
    against itself, which is the defect `CLAUDE.md` records. Delete this and any of the four
    can be moved to any value with every other test in this file still green.
    """
    assert ttl_invariants() == ()
    assert TTL_SECONDS == {
        "plans": PLAN_TTL_SECONDS,
        "retrievals": RETRIEVAL_TTL_SECONDS,
        "embeddings": EMBEDDING_TTL_SECONDS,
        "freshness": FRESHNESS_TTL_SECONDS,
    }


def test_a_lifetime_that_broke_its_relation_is_reported(monkeypatch):
    """The checker above has to be able to fail.

    Delete this and `ttl_invariants` can return an empty tuple unconditionally, at which
    point the test above passes for every possible set of lifetimes and the relations are
    prose.
    """
    monkeypatch.setattr(caches_module, "PLAN_TTL_SECONDS", 1)

    findings = ttl_invariants()

    assert findings
    assert any("recently enough" in finding for finding in findings)


# ------------------------------------------------------------------ the store half


def test_a_value_stored_by_one_cache_is_refused_by_another_reading_the_same_key():
    """A shared keyspace hands back whatever is under a key, and the type is the check.

    Delete this and a plan read through the retrieval cache is parsed as far as it will go
    and returned, which in a store several services can write to is a value from somewhere
    else being served as this one.
    """
    client = FakeValkey()
    plans = plan_cache(client)
    retrievals = retrieval_cache(client)
    key = "plan:shared"
    plans.set(key, CachedPlan(key=key, tool_ids=("laravel.read_client",)), 60)

    assert retrievals.get(key) is None
    assert retrievals.health.rejections == 1
    assert plans.get(key) is not None


def test_a_value_that_fails_its_own_validation_is_a_rejection_and_never_a_raise():
    """A read path that raises turns a corrupt cache entry into a failed request.

    The types here refuse themselves with `CacheLayerError`, which is not a `ValueError` and
    is therefore not wrapped by pydantic. Delete this and the `except` narrows to
    `ValidationError` on the next tidy-up, and a single bad value in the store starts
    failing every request that touches it.
    """
    client = FakeValkey()
    plans = plan_cache(client)
    client.data["plan:corrupt"] = b'{"key":"plan:corrupt","tool_ids":["not a tool id"]}'

    assert plans.get("plan:corrupt") is None
    assert plans.health.rejections == 1


def test_a_value_found_under_a_key_that_is_not_its_own_is_refused():
    """The check that catches a store handing back the wrong entry.

    Nothing else would notice: the value is internally consistent and simply belongs to
    somebody else. Delete this and a mis-set or a key collision reaches the caller as a
    perfectly ordinary hit.
    """
    client = FakeValkey()
    embeddings = embedding_cache(client)
    stored = CachedEmbedding(key="emb:one", model="m@r:1536", values=(0.1, 0.2))
    # Serialised through a `TypeAdapter` rather than as hand-written JSON, so a change to
    # how a value is stored does not leave this fixture asserting against a format nothing
    # writes any more.
    client.data["emb:two"] = bytes(TypeAdapter(CachedEmbedding).dump_json(stored))

    assert embeddings.get("emb:two") is None
    assert embeddings.health.rejections == 1


def test_storing_a_value_under_a_key_that_is_not_its_own_is_refused_at_write_time():
    """A mismatch cannot be caused by data, only by code, so it is raised rather than dropped.

    Delete this and the write is dropped silently, which hides at write time the only bug the
    read-side check exists to find and leaves it to be discovered on a read where it is
    unrecoverable.
    """
    plans = plan_cache(FakeValkey())

    with pytest.raises(ValueError, match="key field"):
        plans.set("plan:one", CachedPlan(key="plan:two", tool_ids=("laravel.read_client",)), 60)


def test_a_store_that_is_down_is_a_miss_and_never_an_error():
    """An unreachable cache must slow the system down, never change what it says.

    Delete this and an `OSError` from the client leaves the module through `get`, so Valkey
    being unreachable stops being a fall-through and starts being an outage. The write is
    exercised too, because a cache write failing after a successful load means the answer is
    already in hand and raising there turns a slow request into a broken one.
    """
    freshness = freshness_cache(DeadValkey())
    stored = reading("xero", "invoice", NOW)

    assert freshness.get(stored.key) is None
    assert freshness.health.degraded is True

    freshness.set(stored.key, stored, FRESHNESS_TTL_SECONDS)

    assert freshness.health.outages == 2
    assert freshness.health.writes == 0


def test_a_value_survives_a_round_trip_through_the_store():
    """The positive half: the guards above are not satisfied by refusing everything.

    Delete this and a store that rejected every value would pass every other store test in
    this file, because all of them assert that something is refused.
    """
    client = FakeValkey()
    freshness = freshness_cache(client)
    stored = reading("xero", "invoice", NOW)
    freshness.set(stored.key, stored, FRESHNESS_TTL_SECONDS)

    found = freshness.get(stored.key)

    assert found is not None
    assert found == stored
    assert found.epoch == stored.epoch
    assert client.ttls[stored.key] == FRESHNESS_TTL_SECONDS
    assert freshness.health.hits == 1


def test_a_shared_health_counter_can_be_passed_to_several_caches():
    """A deployment may share one counter across caches or keep them apart.

    Delete this and the injectable health argument loses its only exercise, and a later
    change that made each cache build its own would go unnoticed until an operator asked
    why the numbers on the dashboard had halved.
    """
    shared = CacheHealth()
    client = FakeValkey()
    plans = plan_cache(client, health=shared)
    retrievals = retrieval_cache(client, health=shared)
    plans.set("plan:a", CachedPlan(key="plan:a", tool_ids=("laravel.read_client",)), 60)
    retrievals.set("retr:a", CachedRetrieval(key="retr:a", chunk_ids=()), 60)

    assert shared.writes == 2


def test_the_stores_are_named_apart_so_a_log_line_says_which_one_degraded():
    """Four caches through one class still have to be distinguishable in an outage.

    Delete this and the shared class keeps `_ValkeyCache`'s default name for all four, so
    every log line says "cache" and an operator cannot tell which one is failing.
    """
    client = FakeValkey()
    names = {
        plan_cache(client).name,
        retrieval_cache(client).name,
        embedding_cache(client).name,
        freshness_cache(client).name,
    }

    assert names == {"plans", "retrievals", "embeddings", "freshness"}
    assert names == set(TTL_SECONDS)


def test_a_record_cache_can_be_built_for_a_type_it_was_not_given_a_factory_for():
    """The generic is a generic, and the four factories are conveniences over it.

    Delete this and `ValkeyRecordCache` can be narrowed to the four types by hand, which
    puts the fifth cache back to being a fifth copy of one `get`.
    """
    client = FakeValkey()
    plans: ValkeyRecordCache[CachedPlan] = ValkeyRecordCache(
        client, TypeAdapter(CachedPlan), name="spare"
    )
    stored = CachedPlan(key="plan:spare", tool_ids=("laravel.read_client",))
    plans.set(stored.key, stored, PLAN_TTL_SECONDS)

    assert plans.get(stored.key) == stored
