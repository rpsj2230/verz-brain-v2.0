"""Storing and fetching a whole answer, and refusing to when that would be wrong.

The key module next door decides *what makes two questions the same question*. This one is
the narrow part that touches the store, and it exists separately because the two have
different failure modes: a bad key serves the wrong person's answer, while a bad lookup
serves the right person a stale one.

The shape mirrors `brain.gate.resolve` deliberately. Both put everything that must
invalidate into the key rather than checking it after the read, so a moved source epoch
produces a miss rather than a hit that something downstream has to notice. Two caches with
two different invalidation philosophies is how one of them ends up wrong.

**A hit reaches a person with its age attached, and that is enforced by the type rather
than by remembering (M3.5.3).** `CachedAnswer.age_label` has existed since the key module
was written, and its docstring says a cached answer that does not say it is cached is a lie
of omission. Nothing in `src/` called it: the label existed, the rule was written down, and
every path that could have served a hit would have served `found.payload` on its own.

That is the third instance of one pattern in this repository, so it is worth fixing in the
shape that cannot recur rather than by adding the call. `ServedAnswer` refuses to be
constructed as a cache hit whose text does not carry the label, so a caller that renders the
payload alone does not produce a quietly wrong answer, it produces an error. `serve_cached`
is the one function that builds one, and `serve_fresh` is its counterpart for an answer that
has no age to declare, so the two cases are distinguishable at the call site instead of by
whether somebody remembered a second argument.

Rejected: putting the label on the channel adapters. There are seven of them and each would
have its own idea of where the age goes, which is six chances to leave it out and one
guarantee that the wording drifts. The age belongs to the answer, not to the surface.

Task ids: M3.5.2, M3.5.3
"""

from __future__ import annotations

from dataclasses import dataclass
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


#: Every string `CachedAnswer.age_label` produces begins with this, whether the answer is
#: seconds or hours old. Held here as the one token `ServedAnswer` checks for, rather than
#: the check restating the label's grammar, so a reworded label is one edit and not a guard
#: that silently stops matching.
AGE_MARKER = "answered "

#: What separates the answer from its provenance. One blank line, because every channel
#: this reaches renders plain text and a reader should see the age as a note under the
#: answer rather than as part of it.
AGE_SEPARATOR = "\n\n"


class AgeNotSurfacedError(Exception):
    """Raised when a cached answer would reach a person without saying how old it is.

    A programming error, and deliberately not a `BrainError`: the taxonomy in
    `brain.core.errors` describes outcomes a person is shown, and this is a bug in the
    caller that must never become one of them.
    """


@dataclass(frozen=True)
class ServedAnswer:
    """An answer on its way to a person, with its provenance already in the text.

    **The constructor is the enforcement.** A cache hit whose text does not carry the age is
    refused here, which is why this type exists at all rather than the caller being trusted
    to concatenate two strings. The failure being prevented is not exotic: it is
    `return found.payload`, which is the obvious thing to write and produces an answer the
    reader assumes was computed just now.

    `age_seconds` is carried beside the text rather than parsed back out of it, because a
    channel that wants to render the age its own way needs the number, and a channel
    reaching into the string for it would be a second parser of a sentence meant for people.
    """

    text: str
    from_cache: bool
    age_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.from_cache:
            if self.age_seconds is not None:
                msg = (
                    "a freshly computed answer has no age to declare; carrying one would "
                    "describe the moment it was computed as though it were a lookup"
                )
                raise AgeNotSurfacedError(msg)
            return
        if self.age_seconds is None:
            msg = (
                "a cache hit has an age; serving one without it is the omission this type "
                "exists to prevent"
            )
            raise AgeNotSurfacedError(msg)
        if AGE_MARKER not in self.text:
            msg = (
                "this answer came from the cache and its text does not say so. A reader "
                "assumes the system just looked, and deciding what to do with an answer "
                "depends on knowing it might be a quarter of an hour old"
            )
            raise AgeNotSurfacedError(msg)


def serve_cached(found: CachedAnswer, now: datetime) -> ServedAnswer:
    """The one way a cached answer becomes text for a person (M3.5.3).

    Takes the `CachedAnswer` rather than its payload, so there is no signature here that
    could be called without the thing that knows the age.
    """
    return ServedAnswer(
        text=f"{found.payload}{AGE_SEPARATOR}{found.age_label(now)}",
        from_cache=True,
        age_seconds=int(found.age(now).total_seconds()),
    )


def serve_fresh(payload: str) -> ServedAnswer:
    """An answer computed for this request, which has no age to declare.

    Its counterpart exists so the two cases are told apart at the call site. A single
    `serve(payload, age=None)` would make the cached case the one somebody forgets, which is
    exactly the omission being prevented.
    """
    return ServedAnswer(text=payload, from_cache=False)


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
