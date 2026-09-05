"""What makes two questions the same question, and who is allowed to share an answer.

This is the most dangerous small module in the system. A cache key that omits a dimension
does not fail loudly: it silently serves one person's answer to another, and it does so
most often for the questions people ask most, which are exactly the ones where two people
of different seniority ask the same words and must get different answers.

So the key is built from five parts and every one of them is required:

- **the question**, normalised only for whitespace and case
- **the entitlement hash**, which is what stops two people sharing an answer their reach
  does not agree on
- **the agent configuration hash**, because the same question through a changed agent is a
  different question
- **the policy epoch**, which is the global invalidation lever
- **the source epochs**, one per source the answer drew on, which is per-source freshness

There is no default for any of them. A missing component would widen the set of people a
cached answer can reach, and the widening would be invisible.

**Why the question is normalised so little.** It is tempting to strip punctuation, stem
words, or drop stop words so that near-identical questions share an entry. Every one of
those transformations merges questions that a person meant to distinguish: "did we invoice
Acme" and "did we invoice Acme?" are the same question, but stemming can merge "billed" and
"billing", and dropping "not" is catastrophic. The hit rate we would gain is not worth
answering a question nobody asked.

Task ids: M3.5.1, M3.5.4
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

#: The key format. Bumping this invalidates every cached answer at once, which is what you
#: want the moment the *meaning* of a key changes rather than its inputs. Without it, a
#: change to how keys are built would leave old entries reachable under new keys by
#: coincidence.
KEY_VERSION = 1

#: How old a cached answer may be before it is not worth serving, regardless of epochs.
#: Epoch invalidation is exact and this is a backstop for the case where a source changed
#: without telling us, which the projection freshness rules already assume happens.
DEFAULT_MAX_AGE = timedelta(minutes=15)


class NotCacheableError(Exception):
    """Raised when a key is requested for something that must never be cached.

    An exception rather than a None return: a caller that forgets to check a None walks on
    with a key of "None" and caches the thing anyway.
    """


#: Question shapes whose answer is different by the time it is read.
#:
#: This is a backstop, not the primary mechanism. The reliable signal is the *source*: an
#: answer that touched a live ticket count is volatile whether or not the question said
#: "right now". Shape matching is language-dependent and a person who writes "as of this
#: moment" defeats it. Both run, and either one is enough to refuse.
VOLATILE_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(right now|at the moment|as of (now|today)|currently)\b", re.I),
    re.compile(r"\b(today|tonight|this (morning|afternoon|hour))\b", re.I),
    re.compile(r"\bhow many\b.{0,40}\b(open|unresolved|pending|outstanding|waiting)\b", re.I),
    re.compile(
        r"\b(latest|most recent|newest|last)\b.{0,30}\b(ticket|invoice|message|reply)\b", re.I
    ),
    re.compile(r"\bwho is\b.{0,20}\b(on leave|off|available|online)\b", re.I),
)

#: Sources whose rows change faster than any sensible cache lifetime. An answer that read
#: one of these is not cached at all, however the question was phrased.
VOLATILE_SOURCES: frozenset[str] = frozenset({"freshdesk_live", "lark_presence", "queue_depth"})


def is_volatile(question: str, sources: frozenset[str] | None = None) -> bool:
    """True when this answer must not be cached at all."""
    if sources and (sources & VOLATILE_SOURCES):
        return True
    return any(p.search(question) for p in VOLATILE_QUESTION_PATTERNS)


def normalise_question(question: str) -> str:
    """Collapse whitespace and case, and nothing else. See the module docstring."""
    return " ".join(question.split()).casefold()


@dataclass(frozen=True)
class CacheKeyParts:
    """Every input to a cache key. Frozen, and no field has a default.

    A default here would be a silent widening: `policy_epoch: int = 0` means a caller that
    forgets to pass the epoch gets an answer from before the policy changed.
    """

    question: str
    ent_hash: str
    agent_config_hash: str
    policy_epoch: int
    #: One entry per source the answer drew on. Sorted before hashing, so the same set of
    #: sources gathered in a different order is the same key.
    source_epochs: Mapping[str, int]

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("cache key needs a question")
        if not self.ent_hash:
            # The one that matters most. An empty entitlement hash would make every caller
            # look identical and share each other's answers.
            raise ValueError(
                "cache key needs an entitlement hash; without it every caller collides"
            )
        if not self.agent_config_hash:
            raise ValueError("cache key needs an agent configuration hash")
        if self.policy_epoch < 0:
            raise ValueError("policy epoch cannot be negative")


def cache_key(parts: CacheKeyParts) -> str:
    """The key. Length-prefixed, so no two different part lists can produce one digest.

    Joining on a separator lets ("ab", "c") and ("a", "bc") collide, which here would mean
    one person's answer standing in for another's. The audit ledger takes the same
    precaution for the same reason.
    """
    fields: list[str] = [
        str(KEY_VERSION),
        normalise_question(parts.question),
        parts.ent_hash,
        parts.agent_config_hash,
        str(parts.policy_epoch),
        # Sorted so gather order does not change the key.
        ";".join(f"{name}={epoch}" for name, epoch in sorted(parts.source_epochs.items())),
    ]
    blob = "".join(f"{len(f)}:{f}" for f in fields)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def key_for(
    question: str,
    ent_hash: str,
    agent_config_hash: str,
    policy_epoch: int,
    source_epochs: Mapping[str, int],
    sources: frozenset[str] | None = None,
) -> str:
    """Build a key, refusing outright for anything volatile."""
    if is_volatile(question, sources):
        raise NotCacheableError(f"volatile question shape or source; not cached: {question[:60]!r}")
    return cache_key(
        CacheKeyParts(
            question=question,
            ent_hash=ent_hash,
            agent_config_hash=agent_config_hash,
            policy_epoch=policy_epoch,
            source_epochs=source_epochs,
        )
    )


@dataclass(frozen=True)
class CachedAnswer:
    """A stored answer and when it was computed.

    `stored_at` is not optional and not defaulted to now. An answer that cannot say how old
    it is cannot be shown to a person honestly, and the rule below is that every hit
    surfaces its age.
    """

    key: str
    payload: str
    stored_at: datetime
    #: Carried so a served answer can name the sources it rests on without another lookup.
    source_epochs: Mapping[str, int]

    def age(self, now: datetime) -> timedelta:
        return now - self.stored_at

    def is_fresh(self, now: datetime, max_age: timedelta = DEFAULT_MAX_AGE) -> bool:
        return self.age(now) <= max_age

    def age_label(self, now: datetime) -> str:
        """Plain words for the person reading the answer, not a timestamp.

        A cached answer that does not say it is cached is a lie of omission: the reader
        assumes the system just looked. "answered 4 minutes ago" lets them decide whether
        that is recent enough for what they are about to do with it.
        """
        seconds = int(self.age(now).total_seconds())
        if seconds < 60:
            return "answered just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"answered {minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        return f"answered {hours} hour{'s' if hours != 1 else ''} ago"
