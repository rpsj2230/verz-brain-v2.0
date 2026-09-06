"""Four more caches, the reach every key is built from, and the one caching that is banned.

`brain.gate.cache_key` decides what makes two questions the same question and
`brain.gate.answer_cache` decides whether a stored answer may be served. Both are about the
answer. This is the rest of M6.2: the plan cache, the retrieval cache, the embedding cache,
the projection freshness cache, and the prohibition on semantic answer caching. It holds no
client, for the reason `brain.ops.limits` holds none: the cases worth testing in a cache are
the ones where a key is missing a dimension, and those are not reachable through a module
that opens a socket. `brain.cache` is the other half and stores what this module keys.

**A key missing any part of the caller's reach is a disclosure with a performance benefit.**
That sentence is the whole design. Every failure available here is silent: the wrong key does
not raise, it returns somebody else's work, and it does so most often for the questions people
ask most, which are exactly the ones two people of different seniority ask in the same words.
So the reach is not five arguments a caller assembles per cache. It is one value, `CallerKey`,
which every key function below requires, and which **cannot be constructed outside `CallerKey.of`**
in the way `brain.gate.catalogue.ProjectedCatalogue` cannot be constructed outside `project`.
Forgetting a dimension therefore means deleting a field from a frozen type rather than leaving
an argument off a call.

**`of` refuses two things and both are M6.2.1.**

The first is a caller whose time bound has passed. `EntitlementSet.ent_hash` puts `not_after`
in the digest, and its docstring says that is what stops an answer cached before a contractor's
expiry being served afterwards. That is true of two *sets*, one bounded and one not, and it is
not true across the boundary in time: the same contractor's set is byte-identical the minute
before the bound and the minute after, so the hash is identical and so is the key. Nothing
about a digest of a set knows what time it is. So the clock is asked here instead, once, and an
expired caller has no `CallerKey`, which means no plan key, no retrieval key and no answer key.
It is raised as `NotCacheableError`, the exception `cache_key` already uses for a question that
must not be cached, so `answer_cache.store_answer`'s existing decline path catches it without
being edited: an expired caller and a volatile question are the same category of "do not store
this", and giving the new one its own type would have left that path silently storing.

The second is an entitlement hash that does not belong to the set it claims to describe.
`resolve` computes the two together and cannot get them out of step; a `Resolved` assembled
anywhere else can. It is one comparison and it rules out the catastrophic case, in the shape
`resolve` compares a loaded set's principal against the one it asked for.

**Which of the four reach fields each key uses is a decision per cache, not a default.**

- The **answer** key omits `principal_id` and `grants_version`, deliberately, and that is
  `cache_key`'s decision rather than this module's: `ent_hash` exists so that two callers of
  identical reach share an answer, and a per-principal component would collapse the cache to
  one entry per person and delete the whole point of it. `grants_version` is left out for the
  same reason and loses nothing, because it is per principal while the hash is computed from
  the grants themselves and moves whenever they do. Where the version earns its place is one
  layer down, in `resolve.cache_key`, which is what stops a stale entitlement set producing a
  stale hash up here.

- The **retrieval** key carries `principal_id`, and the asymmetry with the answer key is not
  an inconsistency. `brain.knowledge.search.Reach` has a personal branch that matches on
  `owner_id`, so two callers holding byte-identical grants do not have identical retrieval
  reach: one of them owns drafts the other does not. That is a fact about the document plane
  rather than about the grant set, and no digest of a grant set can express it. It also carries
  the department list, because that list is the grants intersected with the department
  registry, and a department added to the registry changes it while the grants stand still.

- The **embedding** key carries nothing about the caller at all, which is the exception and
  needs its argument rather than an exemption. See `AN_EMBEDDING_KEY_IS_A_PROOF_OF_POSSESSION`.

- The **freshness** key carries nothing about the caller either, because how stale a source's
  projection is has nothing to do with who is asking. What it must never become is a number
  reported per principal: see `A_HIT_RATE_PER_PRINCIPAL_IS_A_REPORT_ABOUT_THAT_PRINCIPAL`.

**A cached plan is re-projected for whoever reuses it (M6.2.2).** A plan is a list of tool
names, and a tool name is the one thing about a caller's catalogue that must never travel
between callers: `brain.gate.catalogue` exists because a tool a caller may not reach has to be
*absent* rather than refused, and handing person B a plan naming person A's tools is that rule
broken by a cache. So `CachedPlan` holds ids and nothing else, and `replay` takes a
`ProjectedCatalogue`, which only `catalogue.project` can build. There is no spelling of a
replay that skips the projection, because there is no other way to obtain the argument.

An id that does not survive the projection empties the whole replay rather than trimming it.
Trimming is the tempting half-measure and it is worse than either alternative: it hands the
caller a plan that has quietly lost the step that was going to answer the question, and the
answer that comes back is confidently incomplete. Refusing costs one fresh planning call,
which is the same asymmetry `brain.gate.fast_lane` accepts when two rules match one question.
Nothing about the refusal names the tool, or counts what was dropped.

**Nothing here computes a similarity, and M6.2.6 is a shape rather than a rule.** Semantic
answer caching means answering a new question from a similar old one, which under a permission
model means answering person B out of an entry computed for person A because the questions
looked alike. The named constant states it, `assert_no_similarity_search` refuses a module on
the answer path that could do it, and `assert_answer_types_hold_no_vector` refuses an answer
type that grows somewhere to keep a question's embedding. The embedding cache is the thing
that looks like the prohibited one and is not, and the difference is structural rather than a
matter of intent: it is addressed only by an exact digest of the content, so there is no
operation on it that means "close enough".

**How this differs from `brain.memory.formation`, which decided the opposite about the same
digest.** That module records the entitlement hash and deliberately never compares it, because
recall asks whether the reader still *covers* what a memory was formed from, and equality
fails that question in the wrong direction: it invalidates when the reader's grants widen, so
somebody promoted on Monday loses everything the system learnt with them. See
`A_HASH_ANSWERS_A_DIFFERENT_QUESTION_FROM_THE_ONE_RECALL_ASKS`. For a cache key equality is
exactly the right question, and the reason is that the two are asking about different things.
Recall asks about a reader and a memory, where "wider" is a legitimate answer. A cache key
asks whether two requests are the *same request*, and there is no such thing as a request
being usefully wider than another: an answer computed under one reach is a different answer
from the one that reach would produce today, whichever direction it moved. So this module
compares, `formation` records, and neither is the other's bug. See
`EQUALITY_IS_RIGHT_FOR_A_KEY_AND_WRONG_FOR_A_RECALL`.

Rejected: importing `brain.knowledge.search` so the retrieval key could take a `Reach` object
rather than a principal and a list of departments. It reads better and it drags the module
that builds vector queries into the one file that must be able to say it cannot embed
anything, and `assert_no_similarity_search` would then have to make an exception for the very
import it exists to refuse. The scope arrives as data instead, which is also what
`Reach.departments_setting` reduces it to for the second wall.

Rejected: one `delete` per cache, to invalidate on a grant change. There is none here for the
reason `brain.cache` gives at length: everything that must invalidate is in the key, a bump
orphans every key built from the old value in the same instant, and an orphaned key cannot be
read by accident because nobody can construct it. A delete that does not arrive leaves a stale
entry serving a revoked permission with nothing anywhere reporting it.

Scope: nothing here opens a connection or reads a clock. `now` is a parameter, and the store
is `brain.cache`'s.

Task ids: M6.2.1, M6.2.2, M6.2.3, M6.2.4, M6.2.5, M6.2.6
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType, ModuleType
from typing import Final

from brain.core.department import SLUG_RE
from brain.core.envelope import OBJECT_NAME_PATTERN, TOOL_NAME_PATTERN
from brain.gate.cache_key import DEFAULT_MAX_AGE, NotCacheableError, key_for, normalise_question
from brain.gate.catalogue import ProjectedCatalogue
from brain.gate.resolve import CACHE_TTL_SECONDS, Resolved
from brain.knowledge.item import ITEM_ID_PATTERN

# ------------------------------------------------------------------ written-down reasons

#: The claim every key function here is arranged around.
A_KEY_MISSING_A_DIMENSION_OF_REACH_IS_A_DISCLOSURE: Final = (
    "A cache key that leaves out part of who is asking does not fail loudly. It serves one "
    "person's work to another, silently, and most often for the question two people of "
    "different seniority ask in the same words. That is a disclosure with a performance "
    "benefit attached, and the benefit is what makes it survive review. So the reach is one "
    "value that every key function requires rather than a handful of arguments each of them "
    "assembles, and leaving a dimension out means deleting a field from a frozen type."
)

#: Why semantic answer caching is refused outright rather than bounded by a threshold.
NO_SEMANTIC_ANSWER_CACHING: Final = (
    "Semantic caching answers a new question out of a stored answer to a similar old one. "
    "Under a permission model that sentence is: answer person B from an entry computed for "
    "person A, because the two questions looked alike. No similarity threshold makes that "
    "safe, because the thing being compared is the wording and the thing that has to match "
    "is the reach, and those are unrelated quantities. It also breaks the weaker guarantee "
    "on its own terms, since 'did we invoice Acme' and 'did we invoice Acme in March' are "
    "close in every embedding space and have different answers. The exact question text, "
    "normalised only for whitespace and case, is what makes two questions the same question."
)

#: Why an embedding cache with no caller in its key is not a hole in the rule above.
AN_EMBEDDING_KEY_IS_A_PROOF_OF_POSSESSION: Final = (
    "An embedding cache is keyed on a digest of the exact content and the exact model, and "
    "carries no component about who asked. That is safe for a reason stronger than a policy: "
    "the only way to read an entry is to compute its key, and the only way to compute its "
    "key is to already hold the content byte for byte. A lookup is therefore a proof of "
    "possession rather than a disclosure. It is also the cache people mistake for the "
    "prohibited one, and the difference is that there is no operation on it meaning 'close "
    "enough': an entry is reachable by an exact digest or it is not reachable at all."
)

#: Why a cached plan is emptied rather than trimmed when a tool does not survive projection.
A_PLAN_THAT_DOES_NOT_SURVIVE_PROJECTION_IS_NOT_THIS_CALLER_S_PLAN: Final = (
    "A cached plan names tools, and a tool name is precisely what brain.gate.catalogue keeps "
    "from travelling between callers: an unreachable tool is absent rather than refused, "
    "because a model told a capability exists will say so in its own explanation of what it "
    "just did. Replaying a plan through the projection can therefore drop a step. Trimming "
    "to what survives is the tempting half-measure and is worse than either alternative, "
    "because the caller then runs a plan that has quietly lost the step that was going to "
    "answer the question and the answer comes back confidently incomplete. So the replay is "
    "empty, the caller plans afresh at the cost of one call, and nothing names the tool or "
    "counts what was dropped."
)

#: Why cache counters are per cache and never per person.
A_HIT_RATE_PER_PRINCIPAL_IS_A_REPORT_ABOUT_THAT_PRINCIPAL: Final = (
    "Counters here are per cache. A hit rate broken down by principal is a report about that "
    "principal: how much they ask, how much of it is unusual enough to miss, and, by "
    "subtraction against a colleague on the same questions, how differently the system "
    "treats the two of them. That is the same leak brain.gate.compose refuses when it will "
    "not say 'showing 3 of 47', arriving through an operations dashboard instead of an "
    "answer. CacheHealth has no field for a subject and adding one is the regression."
)

#: Why this module compares the entitlement hash where `brain.memory.formation` does not.
EQUALITY_IS_RIGHT_FOR_A_KEY_AND_WRONG_FOR_A_RECALL: Final = (
    "brain.memory.formation records the entitlement hash and never compares it, because "
    "recall asks whether a reader still covers what a memory was formed from, and equality "
    "answers that in the wrong direction: it invalidates when the reader's grants widen, so "
    "somebody promoted loses everything the system learnt with them. A cache key asks a "
    "different question, which is whether two requests are the same request. There is no "
    "such thing as one request being usefully wider than another, and an answer computed "
    "under a reach that has since moved is the wrong answer whichever way it moved. So the "
    "hash is compared here and recorded there, and neither is the other's bug."
)


class CacheLayerError(Exception):
    """A cache that would be keyed wrongly rather than an answer that would be served wrongly.

    Outside the taxonomy in `brain.core.errors`, like `brain.knowledge.search.SearchError` and
    for the same reason: those five outcomes describe an answer a person is shown, and this
    describes a refusal to build a key. Nobody asking a question ever sees one.

    Note what does *not* raise it. An expired caller raises `NotCacheableError` instead, so
    that `answer_cache.store_answer`'s existing decline path catches it; and a plan that does
    not survive projection is an ordinary outcome that raises nothing at all.
    """


# ---------------------------------------------------------------- the shared digest

#: Bumping this orphans every key this module builds, which is what you want the moment the
#: *meaning* of a key changes rather than its inputs. `brain.gate.cache_key.KEY_VERSION` is
#: the answer key's own and is deliberately separate: the two are versioned by different
#: changes, and one number covering both would invalidate answers for a plan-cache edit.
KEY_VERSION: Final = 1

#: How long a question may be before a key is not built from it.
#:
#: Bounded because `plan_key` and `retrieval_key` digest a caller's own text and nothing else
#: in the gate bounds it, so a document pasted into the question box is hashed again on every
#: request for a key nothing will ever hit twice.
#:
#: **The number is a band rather than a preference, and the band is what a test can hold it
#: to.** Above, it must admit every question the fast lane can match, which is a template
#: plus its slot value, with a wide margin for the ordinary question that is nothing like a
#: rule. Below, it must be small enough that a document is refused rather than digested. A
#: bare figure with neither end stated is the constant `CLAUDE.md` describes: a test that
#: builds its input from it moves with it, and every value it could hold passes.
MAX_QUESTION_CHARS: Final = 4000


def digest_of(fields_in_order: Sequence[str]) -> str:
    """A sha256 over a length-prefixed field list.

    Length-prefixed because joining on a separator lets `("ab", "c")` and `("a", "bc")`
    produce one digest, which here means one caller's work standing in for another's. The
    audit ledger and `brain.gate.cache_key.cache_key` take the same precaution.

    **The rule is stated twice in this repository and that is deliberate.** The alternative
    was to reach into `cache_key` for its private half so the answer key and these four share
    one function. That would put the answer key's digest, which carries the most careful
    argument in this package, under a module that also builds four other keys and would be
    edited for their reasons. The duplication is instead pinned by a test that builds one
    field list both ways and asserts the two digests are equal, so the copies cannot drift
    without something going red.
    """
    if not fields_in_order:
        msg = "a digest over no fields is one constant standing in for every key"
        raise CacheLayerError(msg)
    blob = "".join(f"{len(field)}:{field}" for field in fields_in_order)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- the caller (M6.2.1)

#: Only `CallerKey.of` holds this, so only `of` can build a `CallerKey`. See the class
#: docstring for why an unconstructable type is the enforcement rather than a convention.
_CALLER_TOKEN: Final = object()


@dataclass(frozen=True)
class CallerKey:
    """Everything about who is asking that a cache key here is allowed to depend on.

    **This type cannot be constructed outside `of`**, which is the same guard
    `brain.gate.catalogue.ProjectedCatalogue` carries and is here for a closely related
    reason. The failure being prevented is not malice, it is a call site assembling a key out
    of four loose values and getting one of them from the wrong place, or from a caller who
    is no longer entitled to anything. A single argument that can only be produced by a
    checked constructor turns that from a review question into a type error.

    Four fields of data, and each key function below names which of them it uses and why.
    None of the four has a default, for the reason `CacheKeyParts` has none: a default is a
    silent widening, and the widening is invisible. `token` is the fifth and is not data; it
    defaults to None precisely so that a caller who does not know about it fails the check.
    """

    #: Who is asking. Used by the retrieval key, deliberately not by the answer key. See the
    #: module docstring on why that asymmetry is a fact about the document plane.
    principal_id: str
    #: `EntitlementSet.ent_hash`, checked in `of` against the set it claims to describe.
    #:
    #: There is deliberately no "the hash must not be empty" check below, and the absence was
    #: found by a mutation rather than reasoned to. `of` is the only constructor, and it
    #: refuses unless this equals `EntitlementSet.ent_hash()`, which is a 32-character slice
    #: of a sha256 and is never empty for any set at all. A check for empty could therefore
    #: never fire, and `brain.knowledge.embedding` states the rule this follows: a guard that
    #: can never fire is worse than none, because it reads as protection and is not. What the
    #: removal rests on is that the digest is never empty, which is a property of another
    #: module and is asserted as one rather than assumed.
    ent_hash: str
    #: The version the entitlement cache was keyed on. Carried so a caller building a key has
    #: the whole of what `resolve` learnt, and used by no key here: it is per principal, and a
    #: per-principal component in the answer key would collapse the cache to one entry each.
    grants_version: int
    #: The global invalidation lever. In every key, because a policy change means every
    #: cached decision was reached under rules that no longer apply.
    policy_epoch: int
    #: Not data. The constructor guard, and the reason a caller key has one origin.
    token: object = None

    def __post_init__(self) -> None:
        if self.token is not _CALLER_TOKEN:
            msg = (
                "a caller key may only be built by CallerKey.of, which is what checks the "
                f"time bound and the hash. {A_KEY_MISSING_A_DIMENSION_OF_REACH_IS_A_DISCLOSURE}"
            )
            raise CacheLayerError(msg)
        if not self.principal_id.strip():
            # Reachable, unlike the check for an empty hash that used to sit below it:
            # `EntitlementSet.principal_id` carries no minimum length, so an unnamed
            # principal reaches `of` and would key the retrieval cache for nobody.
            msg = "a caller key needs the principal it belongs to"
            raise CacheLayerError(msg)
        if self.grants_version < 0 or self.policy_epoch < 0:
            msg = "a grants version and a policy epoch are counters and cannot be negative"
            raise CacheLayerError(msg)

    @classmethod
    def of(cls, resolved: Resolved, *, policy_epoch: int, now: datetime) -> CallerKey:
        """The only way to build one. Refuses an expired caller and a hash that is not theirs.

        **The expiry check is the half of M6.2.1 that a digest cannot do.** `ent_hash` puts
        `not_after` in the identity, which distinguishes a bounded set from an unbounded one
        and says nothing at all about what time it is: the same contractor's set hashes
        identically either side of their bound, so an answer stored the minute before would
        be served the minute after under a key that matches exactly. The clock is therefore
        asked once, here, and an expired caller ends up with no key to any cache in this
        module rather than with a check each of them has to remember.

        It raises `NotCacheableError` rather than a type of this module's own, so that
        `answer_cache.store_answer`, which already declines on that exception, declines on
        this too without being edited. An expired caller and a volatile question are the same
        category of thing.

        **The hash is checked against the set it claims to describe.** `resolve` computes the
        two in one breath and cannot get them out of step, and a `Resolved` built anywhere
        else can. One comparison rules out a key carrying one caller's reach and another
        caller's answer, which is the catastrophic failure and is otherwise undetectable
        downstream, because everything about such a key is internally consistent.
        """
        if resolved.entitlements.is_expired(now):
            msg = (
                f"{resolved.entitlements.principal_id} is past their time bound, so nothing "
                "may be stored for them or read back under their reach; the hash is "
                "identical either side of the bound and would match exactly"
            )
            raise NotCacheableError(msg)
        if resolved.ent_hash != resolved.entitlements.ent_hash():
            msg = (
                "the entitlement hash does not belong to the set beside it; a key built from "
                "it would carry one caller's reach and another caller's work"
            )
            raise CacheLayerError(msg)
        return cls(
            principal_id=resolved.entitlements.principal_id,
            ent_hash=resolved.ent_hash,
            grants_version=resolved.grants_version,
            policy_epoch=policy_epoch,
            token=_CALLER_TOKEN,
        )

    @property
    def reach_fields(self) -> tuple[str, ...]:
        """The reach as key fields: the hash and the policy epoch, in that order.

        Named once rather than spelled out in each key function, so a fifth cache cannot be
        written that quietly leaves one of them out. `principal_id` is not in here, because
        the caches that need it need it for a reason of their own and adding it to every key
        would delete the sharing the answer cache exists for.
        """
        return (self.ent_hash, str(self.policy_epoch))


# ----------------------------------------------------------------- the answer (M6.2.1)


def answer_key(
    question: str,
    caller: CallerKey,
    *,
    agent_config_hash: str,
    source_epochs: Mapping[str, int],
    sources: frozenset[str] | None = None,
) -> str:
    """The answer key, built by `cache_key.key_for` with the caller checked first (M6.2.1).

    It calls the existing builder rather than restating it. That module refuses a volatile
    question, normalises the text, length-prefixes the fields and argues at length about why
    the question is normalised so little; a second answer-key builder would be a second place
    for all of that to be subtly different, and the wrong one is whichever the gate happens
    to import. What this adds is the argument the builder cannot make, which is that a caller
    holding a `CallerKey` has already been found to be within their time bound and to own the
    hash they are keyed on.

    It passes `caller.ent_hash` and `caller.policy_epoch`, and neither `principal_id` nor
    `grants_version`. That is `cache_key`'s decision rather than this module's and is argued
    in the module docstring above: the hash exists so two callers of identical reach may
    share an answer, and a per-principal component would collapse the cache to one entry per
    person and delete the whole point of it.
    """
    return key_for(
        question,
        caller.ent_hash,
        agent_config_hash,
        caller.policy_epoch,
        source_epochs,
        sources,
    )


# ------------------------------------------------------------------- the plan (M6.2.2)

#: How long a cached plan may sit in the store. The catalogue a plan was built against changes
#: for two reasons: the caller's grants move, which moves `ent_hash` and orphans the key in the
#: same instant, and the tool registry changes, which is in no key at all. This TTL is
#: therefore the *whole* invalidation for the second case rather than a backstop for the first,
#: which is why it is short and why it is pinned to the answer cache's own freshness bound: two
#: numbers meaning "recently enough" that disagree is one of them being wrong.
PLAN_TTL_SECONDS: Final = int(DEFAULT_MAX_AGE.total_seconds())

#: How many tools one cached plan may name. A plan longer than this is not a plan, it is a
#: model looping, and caching one would make the loop cheap enough to repeat.
MAX_PLAN_TOOLS: Final = 24

_TOOL_NAME_RE: Final = re.compile(TOOL_NAME_PATTERN)


@dataclass(frozen=True)
class CachedPlan:
    """A plan as tool ids and nothing else (M6.2.2).

    **There is no field here for a tool definition, a description, a schema or an argument**,
    and that is the leaf rather than an omission. A definition carries a description written
    for a model, which is the sentence that teaches a model a capability exists; an argument
    carries record ids, which are somebody's data. Ids alone are the least that can be stored
    and still be a plan, and they are worthless to a caller who cannot project them.

    Frozen and validated on the way in, because this arrives from a shared store that other
    processes write to and is therefore checked as though it came from outside, which it did.
    """

    key: str
    tool_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key:
            msg = "a cached plan carries the key it is stored under"
            raise CacheLayerError(msg)
        if not self.tool_ids:
            msg = (
                "a plan naming no tool is not a plan; an empty replay already means 'this "
                "plan is not yours', so an empty plan would be indistinguishable from one"
            )
            raise CacheLayerError(msg)
        if len(self.tool_ids) > MAX_PLAN_TOOLS:
            msg = f"{len(self.tool_ids)} tools is past the {MAX_PLAN_TOOLS} a plan may name"
            raise CacheLayerError(msg)
        if len(set(self.tool_ids)) != len(self.tool_ids):
            # A repeated id is a plan that visits one tool twice, which is either a loop or a
            # merge of two plans. Neither is worth replaying, and both read as longer than
            # they are.
            msg = "a cached plan names a tool twice"
            raise CacheLayerError(msg)
        for tool_id in self.tool_ids:
            if not _TOOL_NAME_RE.match(tool_id):
                # The grammar is `brain.core.envelope`'s, imported rather than restated:
                # `sweep_tool_registry` refuses a second copy of it anywhere in this tree.
                msg = "a cached plan names something that is not a tool id"
                raise CacheLayerError(msg)


@dataclass(frozen=True)
class ReplayedPlan:
    """What is left of a cached plan after the caller's own projection has been applied.

    `tool_ids` is the intersection with the caller's catalogue and never the stored list, so
    a caller that ignores `usable` still gets their own tools rather than somebody else's.
    An id that did not survive empties the whole thing: see
    `A_PLAN_THAT_DOES_NOT_SURVIVE_PROJECTION_IS_NOT_THIS_CALLER_S_PLAN`.

    There is deliberately no field counting what was dropped and no field naming it. The
    count is the number of tools this caller may not reach, which is a fact about the
    catalogue they were never shown, and the name is worse.
    """

    tool_ids: tuple[str, ...]

    @property
    def usable(self) -> bool:
        """Whether this plan may be run. False means plan afresh, not 'run what is left'."""
        return bool(self.tool_ids)


def plan_key(question: str, caller: CallerKey, *, agent_config_hash: str) -> str:
    """The key a plan is stored under (M6.2.2).

    The reach is in it as well as the question, even though `replay` re-projects on the way
    out. Keying on the question alone would be defensible on the argument that the projection
    is the real guard, and it is the wrong shape twice over: every caller would then share one
    entry and every reuse would be a near-certain empty replay, so the cache would cost a
    lookup and return nothing; and the guard would be a single call site rather than the key,
    which is the arrangement `brain.gate.resolve` argues against at length. Both walls, as
    `brain.knowledge.search` puts it about the predicate and the policy.

    No source epochs. A plan is a decision about which tools to reach for and does not read
    any data, so a source moving does not make a plan wrong; it makes the answer built from
    the plan wrong, and that is what the answer key's own source epochs are for.
    """
    if not agent_config_hash:
        msg = (
            "a plan key needs an agent configuration hash; the same question through a "
            "changed agent reaches for different tools and is a different plan"
        )
        raise CacheLayerError(msg)
    return "plan:" + digest_of(
        (
            f"plan/{KEY_VERSION}",
            _question_field(question),
            *caller.reach_fields,
            agent_config_hash,
        )
    )


def replay(plan: CachedPlan, catalogue: ProjectedCatalogue) -> ReplayedPlan:
    """Filter a cached plan through the projection of whoever is reusing it (M6.2.2).

    **The argument is a `ProjectedCatalogue`, which only `brain.gate.catalogue.project` can
    build.** That is the whole of the enforcement and it is why this takes an object rather
    than a set of names: a caller cannot reach this function holding a list of tool names they
    assembled, because the type they would need cannot be constructed. Projection is not
    delegated and is not optional, in the same way `brain.gate.invoke` will not accept a
    catalogue it did not get from the projector.

    Order is the plan's rather than the catalogue's. A plan is a sequence and the catalogue is
    sorted by name for prompt-cache stability, so taking the catalogue's order would silently
    reorder the steps and produce a plan that runs the same tools in the wrong sequence.
    """
    reachable = frozenset(catalogue.names)
    if not all(tool_id in reachable for tool_id in plan.tool_ids):
        return ReplayedPlan(tool_ids=())
    return ReplayedPlan(tool_ids=plan.tool_ids)


# -------------------------------------------------------------- the retrieval (M6.2.3)

#: How long a retrieval result may sit in the store. Shorter than a plan, because the corpus
#: is the thing that changes without anybody deciding to change it: somebody uploads a
#: document and expects to find it. The corpus epoch is in the key and does the real
#: invalidating; this bounds the case where the epoch has not been re-read yet, so it is held
#: at the freshness cache's own lifetime rather than at a number of its own.
RETRIEVAL_TTL_SECONDS: Final = 300

#: How many references one cached retrieval may hold.
#:
#: Not a number of its own: `brain.knowledge.search` caps each leg at `MAX_CANDIDATE_DEPTH`,
#: so the widest list retrieval can produce is one leg's ceiling, and at most both legs'
#: before fusion truncates. Anything longer than that came from somewhere other than a query
#: this system ran. The relation is written down here and checked in the tests against
#: `MAX_CANDIDATE_DEPTH` itself, because that module cannot be imported into this one: it
#: builds vector queries, and `SIMILARITY_BEARING_MODULES` refuses it on the answer path.
MAX_RETRIEVAL_REFERENCES: Final = 500

_ITEM_ID_RE: Final = re.compile(ITEM_ID_PATTERN)


@dataclass(frozen=True)
class CachedRetrieval:
    """The references one retrieval returned, in rank order (M6.2.3).

    Ids and no passages. A chunk's body is the document's content, and a cache holding it is
    a second copy of the corpus outside the table row-level security is written on: the
    policy in `0009` protects `know.chunk`, and nothing protects a Valkey value. Ids are
    re-read through the query that carries `reach_predicate`, so the second wall still stands
    behind every reuse of this.
    """

    key: str
    chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key:
            msg = "a cached retrieval carries the key it is stored under"
            raise CacheLayerError(msg)
        if len(self.chunk_ids) > MAX_RETRIEVAL_REFERENCES:
            msg = (
                f"{len(self.chunk_ids)} references is past the "
                f"{MAX_RETRIEVAL_REFERENCES} a retrieval can have produced"
            )
            raise CacheLayerError(msg)
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            msg = "a cached retrieval names one chunk twice"
            raise CacheLayerError(msg)
        for chunk_id in self.chunk_ids:
            if not _ITEM_ID_RE.match(chunk_id):
                msg = "a cached retrieval names something that is not a reference"
                raise CacheLayerError(msg)


def retrieval_key(
    question: str,
    caller: CallerKey,
    *,
    departments: Sequence[str],
    corpus_epoch: int,
) -> str:
    """The key a retrieval result is stored under, with the scope in it (M6.2.3).

    Four things about the caller, and each closes a hole the others do not.

    `principal_id`, because `brain.knowledge.search.Reach` has a personal branch matching on
    `owner_id`: two callers with byte-identical grants still do not see the same documents,
    because one of them owns drafts. No digest of a grant set can express that, and leaving
    it out would hand one person another person's unfinished notes.

    The department list, sorted, because it is the grants intersected with the department
    registry: a department added to the registry widens the list while the grant stands
    still, so `ent_hash` does not move and the reach does. Sorted so that a list gathered in
    a different order is the same key, which is the same reason `cache_key` sorts its source
    epochs and `ent_hash` sorts its grants.

    `ent_hash` and the policy epoch, because everything else about the caller's reach is in
    them.

    And `corpus_epoch`, which is what a re-index or an upload moves. Without it a cached list
    of references outlives the chunks it names, and a citation nothing can resolve is a
    citation nobody checks.
    """
    if corpus_epoch < 0:
        msg = "a corpus epoch is a counter and cannot be negative"
        raise CacheLayerError(msg)
    ordered = tuple(sorted(departments))
    if len(set(ordered)) != len(ordered):
        # A repeat would not change the reach and would change the key, which is a miss
        # nobody can explain. `Reach` refuses the same thing for the same reason.
        msg = "a retrieval key names one department twice"
        raise CacheLayerError(msg)
    for name in ordered:
        if not SLUG_RE.match(name):
            msg = "a retrieval key names something that is not a department"
            raise CacheLayerError(msg)
    return "retr:" + digest_of(
        (
            f"retr/{KEY_VERSION}",
            _question_field(question),
            caller.principal_id,
            *caller.reach_fields,
            ",".join(ordered),
            str(corpus_epoch),
        )
    )


# -------------------------------------------------------------- the embedding (M6.2.4)

#: How long an embedding may sit in the store. Long, and the length is not a risk appetite:
#: the same text through the same model is the same vector for ever, both inputs are in the
#: key, and there is no event anywhere that could make an entry wrong. A TTL here is purely
#: the store reclaiming space, which is what `brain.cache` says a TTL is always for and is
#: only true of this one cache.
EMBEDDING_TTL_SECONDS: Final = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class CachedEmbedding:
    """A vector, the model that produced it, and no text and no caller (M6.2.4).

    **There is no field here for the content, the question or an answer**, and that is the
    structural half of M6.2.6 seen from the other side. An entry cannot be walked back to the
    words it came from, and there is no index over the values, so the one operation this
    cache supports is "fetch the vector for content I already hold". "Which stored question
    is closest to this one" is not expressible against it, which is what the prohibition
    means. See `AN_EMBEDDING_KEY_IS_A_PROOF_OF_POSSESSION`.

    `model` is the identity `brain.knowledge.embedding.EmbeddingModel.identity` produces,
    carried as a string rather than reconstructed: a vector whose model nobody recorded
    cannot be compared with anything, and a second way of judging two identities equal is
    the one thing that module's `assert_comparable` exists to prevent.
    """

    key: str
    model: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.key:
            msg = "a cached embedding carries the key it is stored under"
            raise CacheLayerError(msg)
        if not self.model:
            msg = (
                "a cached embedding names no model; nothing could say which space it belongs "
                "to, and a distance to it would be a number rather than a distance"
            )
            raise CacheLayerError(msg)
        if not self.values:
            msg = "a cached embedding with no values is a miss that costs a round trip"
            raise CacheLayerError(msg)


def content_hash(content: str) -> str:
    """The digest of exactly these bytes. No normalisation of any kind, and that is the point.

    `cache_key.normalise_question` collapses whitespace and case for a *question*, because
    two people typing the same question differently meant the same question. Content is not
    a question: two documents differing only in whitespace are two documents, they embed
    differently, and folding them together here would hand back one document's vector for
    another's text. This is also what makes the key a proof of possession, since a caller
    must hold the exact bytes rather than something that normalises to them.
    """
    if not content:
        msg = (
            "there is no embedding of nothing, and an empty content hash would be one "
            "constant every empty input collided under"
        )
        raise CacheLayerError(msg)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embedding_key(content: str, *, model: str) -> str:
    """The key an embedding is stored under (M6.2.4).

    The content digest and the model identity, and nothing about the caller. See
    `AN_EMBEDDING_KEY_IS_A_PROOF_OF_POSSESSION` for why that is not the hole it looks like.

    The model is in the key rather than checked after the read, which is the same choice
    `resolve` makes about the grants version and for the same reason: a check after the read
    means the wrong vector was already in hand, and a later refactor that drops it reads as a
    simplification. `brain.knowledge.embedding.assert_comparable` is the check that exists
    for values that arrive with a model attached from somewhere else; this is the one that
    makes a mismatched entry unreachable.
    """
    if not model:
        msg = (
            "an embedding key needs a model identity; without one a rebuild reads the old "
            "vectors back under the new model's keys and the corpus is silently mixed"
        )
        raise CacheLayerError(msg)
    return "emb:" + digest_of((f"emb/{KEY_VERSION}", content_hash(content), model))


# ------------------------------------------------------ the projection freshness (M6.2.5)

#: How long a freshness reading may be reused. Short, because this is the reading that
#: invalidates the others: the epoch it yields is in every answer key, so a reading held past
#: its usefulness holds every answer built on it fresh alongside. Half of
#: `resolve.CACHE_TTL_SECONDS`, which is the shortest lifetime anything else in the request
#: path runs on, because this one gates more than that one does.
FRESHNESS_TTL_SECONDS: Final = CACHE_TTL_SECONDS // 2

#: Microseconds per second. Named because the epoch below multiplies by it, and a bare
#: 1_000_000 in that expression reads like a unit conversion nobody chose.
MICROSECONDS_PER_SECOND: Final = 1_000_000

_OBJECT_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)


@dataclass(frozen=True)
class CachedFreshness:
    """When a source last confirmed one entity's rows, and the epoch that follows from it.

    **`epoch` is a property rather than a field**, and that is the leaf's substance rather
    than tidiness. An epoch supplied alongside a timestamp is two records of one fact, and
    the way they disagree is the way that cannot be noticed: a refresh moves `last_seen_at`
    and a caller forgets to bump the integer, so every answer key built from it is unchanged
    and every cached answer built on data that has since moved stays servable. Derived from
    the timestamp, an epoch that has not moved is a projection that has not moved, which is
    a true statement rather than an assumption.

    Microsecond resolution rather than seconds, because a reconciliation pass confirms many
    rows inside one second and a second-resolution epoch would be stuck across all of them.

    **The reading is cached and the verdict is not**, which is the distinction that makes
    this cache safe at all. `brain.connectors.projection.assess_staleness` turns a
    `last_seen_at`, a promise and a clock into LIVE, AGEING or STALE. Two of those three are
    facts that sit still and the third is the clock, so a cached verdict is a statement about
    how fresh something was when somebody last asked, served to somebody asking now. It would
    be wrong in the direction that never gets noticed: a row that went stale during the
    cache's own lifetime keeps reporting LIVE, and the notice that would have told the reader
    is the thing that goes missing. So what is stored here is the timestamp the database had,
    and the verdict is recomputed against the current clock by the module that owns it. There
    is no field on this type that could hold a `Freshness`.

    It carries no field about who asked and there is nowhere to put one either. How stale a
    projection is has nothing to do with who is reading it, and a freshness reading recorded
    per principal would be a record of who asks about which source. See
    `A_HIT_RATE_PER_PRINCIPAL_IS_A_REPORT_ABOUT_THAT_PRINCIPAL`.
    """

    key: str
    source: str
    entity: str
    #: When the source last confirmed the record, which is `proj.record.last_seen_at` and is
    #: deliberately not `updated_at`. `brain.tables.projection` argues the difference: a row
    #: rewritten by a backfill has a new `updated_at` and the same `last_seen_at`, and
    #: staleness derived from the first would report a record confirmed that nobody confirmed.
    last_seen_at: datetime

    def __post_init__(self) -> None:
        if not self.key:
            msg = "a cached freshness reading carries the key it is stored under"
            raise CacheLayerError(msg)
        for name, value in (("source", self.source), ("entity", self.entity)):
            if not _OBJECT_NAME_RE.match(value):
                msg = f"a freshness reading's {name} is not an object name"
                raise CacheLayerError(msg)
        if self.last_seen_at.tzinfo is None:
            # The same refusal `ProjectedRecord` makes about its own. A naive timestamp
            # compares wrongly against an aware one and produces an epoch off by the
            # deployment's offset from UTC, which is a number that looks entirely ordinary.
            msg = "a naive last_seen_at yields an epoch off by the deployment's UTC offset"
            raise CacheLayerError(msg)

    @property
    def name(self) -> str:
        """`source.entity`, which is how a source epoch is named in an answer key.

        Both halves, because `proj.record` is keyed by both: a Freshdesk company and a Xero
        contact are different companies, and one epoch covering a whole connector would make
        a refresh of either invalidate answers that drew on neither.
        """
        return f"{self.source}.{self.entity}"

    @property
    def epoch(self) -> int:
        """The integer an answer key carries for this source. Moves when the projection does."""
        return int(self.last_seen_at.timestamp() * MICROSECONDS_PER_SECOND)


def freshness_key(source: str, entity: str) -> str:
    """The key a freshness reading is stored under (M6.2.5).

    Nothing about the caller, because freshness is a property of the pipeline. That also
    means one reading serves every caller, which is the point: the alternative is a read of
    `proj.record` on every request to learn a number that is the same for everybody.
    """
    for name, value in (("source", source), ("entity", entity)):
        if not _OBJECT_NAME_RE.match(value):
            msg = f"a freshness key's {name} is not an object name"
            raise CacheLayerError(msg)
    return f"fresh:{source}.{entity}"


def source_epochs(readings: Iterable[CachedFreshness]) -> Mapping[str, int]:
    """The `source_epochs` mapping an answer key takes, from a set of freshness readings.

    One entry per source and entity rather than one per source. Folding a connector's
    entities into a single epoch would need a rule for combining them, and only the newest
    is safe: the oldest does not move when a newer entity refreshes, so an answer drawing on
    that entity would stay cached across a change to the data it was built from. Having to
    choose is the sign the fold is wrong, so nothing is folded.

    Refuses two readings of one pair rather than taking either. Silently keeping the last is
    how a stale reading wins by arriving second, and there is no order here that would make
    the choice meaningful.
    """
    out: dict[str, int] = {}
    for reading in readings:
        if reading.name in out:
            msg = f"two freshness readings for {reading.name}; one of them would be dropped"
            raise CacheLayerError(msg)
        out[reading.name] = reading.epoch
    return MappingProxyType(out)


# ------------------------------------------------- the prohibition, structurally (M6.2.6)

#: Every spelling of "find me something close to this" that could turn a cache into a
#: semantic one. Names rather than semantics, which is sound for the ones listed and complete
#: for nothing: what closes the rest is that these modules import almost nothing, and
#: `assert_no_similarity_search` refuses the libraries a new one would arrive through.
SIMILARITY_CALLS: Final[frozenset[str]] = frozenset(
    {
        "cosine",
        "cosine_similarity",
        "dot",
        "euclidean",
        "knn",
        "most_similar",
        "nearest",
        "nearest_neighbours",
        "nearest_neighbors",
        "similarity",
        "topk",
    }
)

#: Modules that cannot appear on the answer path without the prohibition stopping being one.
#: The three `brain.knowledge` entries are where a vector is produced, compared and ranked;
#: the rest is every numerical library a hand-rolled cosine would be written with.
SIMILARITY_BEARING_MODULES: Final[tuple[str, ...]] = (
    "brain.knowledge.embedding",
    "brain.knowledge.fusion",
    "brain.knowledge.search",
    "numpy",
    "scipy",
    "sklearn",
    "faiss",
    "annoy",
    "hnswlib",
)

#: Field names that would let a type on the answer path hold a question's embedding. Checked
#: alongside the annotation, because the two failures look different: a field called
#: `question_vector` typed as a string is somebody serialising one, and a field called
#: `extra` typed `tuple[float, ...]` is somebody hiding one.
VECTOR_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "centroid",
        "distance",
        "embedding",
        "embeddings",
        "neighbours",
        "neighbors",
        "similarity",
        "vector",
        "vectors",
    }
)


def _call_name(node: ast.Call) -> str:
    """`np.dot` for an attribute call, `cosine` for a bare one, empty for anything else.

    The same reader `brain.gate.fast_lane._call_name` is, restated rather than imported
    because importing it would make this check depend on the fast lane, which is a module
    with an entirely different reason to exist and its own list of forbidden calls.
    """
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every module name the source imports, however it spells the import."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def assert_no_similarity_search(module: ModuleType) -> None:
    """Refuse a module on the answer path that could match one question against another.

    Checked on the imports and the calls rather than on the words, because the words are in
    the docstring above and a search for them would be satisfied by the sentence that argues
    against them. That is the construction `brain.gate.fast_lane` makes in
    `assert_reaches_no_tool_and_no_model`, and it is the same trap it was written for: a test
    that greps for a prohibition passes against the file that states it.

    **Sound and incomplete, in that order.** What it catches is every import of a vector
    library or of the three modules that produce, compare and rank embeddings, and every call
    spelled like a distance. What it cannot catch is a similarity written out by hand in a
    loop over two lists of floats. What closes that is not this function: it is that the
    modules it is run against hold no floats at all, which
    `assert_answer_types_hold_no_vector` checks from the other direction, so there would be
    nothing in scope to compare.

    Note what it does not claim. `CachedEmbedding` holds a vector and lives in this module,
    which is why the check is over calls and imports rather than over the presence of a
    float: storing a vector under an exact digest is not a similarity search, and a check
    that refused the type would refuse M6.2.4 in the name of M6.2.6.
    """
    tree = ast.parse(inspect.getsource(module))
    for name in _imported_modules(tree):
        for forbidden in SIMILARITY_BEARING_MODULES:
            if name == forbidden or name.startswith(f"{forbidden}."):
                msg = f"{module.__name__} imports {name}. {NO_SEMANTIC_ANSWER_CACHING}"
                raise CacheLayerError(msg)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _call_name(node)
            if called in SIMILARITY_CALLS or called.split(".")[-1] in SIMILARITY_CALLS:
                msg = (
                    f"{module.__name__} calls {called} on line {node.lineno}. "
                    f"{NO_SEMANTIC_ANSWER_CACHING}"
                )
                raise CacheLayerError(msg)


def assert_answer_types_hold_no_vector(*types: type) -> None:
    """Refuse an answer-path type that has somewhere to keep a question's embedding (M6.2.6).

    The type is the enforcement, in the shape `brain.gate.fast_lane.FastLaneAnswer` has no
    field for a tool: a rule saying "we do not cache semantically" holds until the first
    person who wants a better hit rate, and a type with nowhere to put a vector cannot be
    talked into one without an edit somebody has to defend.

    **Annotations are read as the source strings PEP 563 left them as, and not resolved.**
    Resolving them would import whatever they name, which on a type that had grown a vector
    field is the machinery this check exists to keep out; a check that has to load the thing
    it is looking for in order to look for it is a check with a hole in exactly one case. The
    string form is also what catches `tuple[float, ...]` without needing to reason about
    generic aliases at all.
    """
    for cls in types:
        for field in fields(cls):
            annotation = field.type if isinstance(field.type, str) else str(field.type)
            if field.name in VECTOR_FIELD_NAMES:
                msg = (
                    f"{cls.__name__}.{field.name} is a place to keep a question's embedding. "
                    f"{NO_SEMANTIC_ANSWER_CACHING}"
                )
                raise CacheLayerError(msg)
            if "float" in annotation:
                msg = (
                    f"{cls.__name__}.{field.name} is typed {annotation}, which is a vector "
                    f"however it is named. {NO_SEMANTIC_ANSWER_CACHING}"
                )
                raise CacheLayerError(msg)


def assert_counters_name_no_principal(*types: type) -> None:
    """Refuse a counter type with somewhere to record whose requests it counted.

    See `A_HIT_RATE_PER_PRINCIPAL_IS_A_REPORT_ABOUT_THAT_PRINCIPAL`. The field names are the
    check because a counter broken down by person arrives as one field on an existing type
    rather than as a new dashboard, and the field is always called something reasonable.
    """
    subjects = frozenset(
        {"principal", "principal_id", "subject", "caller", "user", "user_id", "asker"}
    )
    for cls in types:
        for field in fields(cls):
            if field.name in subjects:
                msg = (
                    f"{cls.__name__}.{field.name} makes these counters a report about one "
                    f"person. {A_HIT_RATE_PER_PRINCIPAL_IS_A_REPORT_ABOUT_THAT_PRINCIPAL}"
                )
                raise CacheLayerError(msg)


# --------------------------------------------------------------------------- helpers


def _question_field(question: str) -> str:
    """The question as a key field: `cache_key`'s own normalisation, and a length bound.

    `normalise_question` is called rather than restated. It collapses whitespace and case and
    deliberately does nothing else, and the argument for how little it does is the reason
    M6.2.6 is possible at all: stemming merges "billed" and "billing", dropping stop words
    drops "not", and every one of those transformations answers a question nobody asked. A
    second normaliser here would be a second, weaker version of that argument.

    The length bound is this module's own. `brain.gate.ingress` bounds a request; nothing
    bounds what a *key* is built from, and every key function below digests a question, so a
    caller sending a megabyte of text would hash it again on every request.
    """
    if not question.strip():
        msg = "a cache key needs a question"
        raise CacheLayerError(msg)
    if len(question) > MAX_QUESTION_CHARS:
        msg = (
            f"a question of {len(question)} characters is past the {MAX_QUESTION_CHARS} a "
            "key is built from; digesting one on every request costs more than the lookup saves"
        )
        raise CacheLayerError(msg)
    return normalise_question(question)


def ttl_invariants() -> tuple[str, ...]:
    """Every relation the four lifetimes above must hold, as sentences, checked by a test.

    Written as a function rather than as bare asserts at import, so that a run of the suite
    reports which relation broke rather than failing to import the module. The relations
    matter because each of these numbers is meaningless on its own: what makes 900 seconds
    right for a plan is that it is the answer cache's own idea of recent, and what makes
    seven days right for an embedding is that nothing can invalidate one. A mutation that
    moved a constant would otherwise pass every test that imports it, which is the defect
    `CLAUDE.md` records against `throttle.RETRY_AFTER_WHEN_UNSTATED`.
    """
    findings: list[str] = []
    if int(DEFAULT_MAX_AGE.total_seconds()) != PLAN_TTL_SECONDS:
        findings.append(
            "a plan may outlive the answer cache's own idea of recent, so two numbers "
            "meaning 'recently enough' disagree and neither is the rule"
        )
    if FRESHNESS_TTL_SECONDS >= CACHE_TTL_SECONDS:
        findings.append(
            "a freshness reading outlives a resolved entitlement set, so the epoch that "
            "invalidates every answer key is held longer than the reach those keys carry"
        )
    if RETRIEVAL_TTL_SECONDS <= FRESHNESS_TTL_SECONDS:
        findings.append(
            "a retrieval result expires no later than the freshness reading that would "
            "have invalidated it, so the epoch in its key can never do any work"
        )
    if EMBEDDING_TTL_SECONDS <= PLAN_TTL_SECONDS:
        findings.append(
            "an embedding expires as fast as a plan, though both its inputs are in its key "
            "and nothing that happens can make one wrong"
        )
    if PLAN_TTL_SECONDS <= RETRIEVAL_TTL_SECONDS:
        findings.append(
            "a plan expires no later than a retrieval, though the tool registry changes far "
            "less often than the corpus and is in no key at all"
        )
    return tuple(findings)


#: The lifetimes, so a caller wiring the stores reads them from one place rather than from
#: five constants it has to know the names of. A mapping rather than five arguments, because
#: a store wired with another cache's TTL is a bug with no symptom until something is served
#: stale, and the day that happens the number is in a call site rather than here.
TTL_SECONDS: Mapping[str, int] = MappingProxyType(
    {
        "plans": PLAN_TTL_SECONDS,
        "retrievals": RETRIEVAL_TTL_SECONDS,
        "embeddings": EMBEDDING_TTL_SECONDS,
        "freshness": FRESHNESS_TTL_SECONDS,
    }
)
