"""Which model produced a vector, why that has to be written down, and how to redo them all.

Changing the embedding model invalidates every vector in the corpus, and nothing about that
is visible from a query. The old and the new vectors sit in the same column and the same HNSW
index; a distance between two of them is a number, it is just not a distance between two
meanings. Retrieval does not break, it degrades, and it degrades silently: the leg returns
fifty rows, the answer is composed from them, the citations resolve, and the passages are
merely worse than they were. Nobody files a bug about an answer that is slightly off.

So three things, and each is one of the three ways this fails.

**A vector without its model is a vector from an unknown space.** The identity is recorded
beside the numbers, and it is not the model's name. A provider that updates weights behind
a stable name produces a different vector space under an identical label, which is the same
failure with nothing at all to notice it by, so the identity carries a revision and a width
as well and refuses to be built without them. See `A_MODEL_NAME_IS_NOT_A_MODEL_IDENTITY`.

**A search must not mix two models, and mixing is refused rather than filtered.** The
tempting answer is to add the model to the WHERE clause and carry on, which is right and is
not enough on its own: during a rebuild that clause silently halves the candidate set, and a
short vector leg is indistinguishable from the one iterative scan produces when it reaches
`hnsw.max_scan_tuples`, which `brain.knowledge.search.A_SHORT_LEG_IS_NOT_AN_EMPTY_KNOWLEDGE_BASE`
says must never be read as an absence. So a corpus holding two models has no vector leg at
all: the lexical leg is unaffected, reciprocal rank fusion consumes a missing list natively,
and retrieval degrades to text search, which is worse in a way somebody can see and reason
about. See `A_MIXED_CORPUS_HAS_NO_VECTOR_LEG`.

**A rebuild resumes by key and never by offset.** It is a job over everything the company has
ever uploaded, it takes hours, and it will be interrupted. `OFFSET` is the obvious cursor and
it is wrong twice: it re-reads every row before the offset on each batch, and, far worse, it
*skips rows* whenever the underlying set shifts, which during a rebuild it does on every
batch. Rows skipped by a rebuild are chunks left on the old model with nothing anywhere
saying so, which is precisely the silent state this module exists to prevent. So the position
is the last chunk id written, the whole position is a value the caller persists, and resuming
is constructing the next batch from it. See `A_REBUILD_RESUMES_BY_KEY_AND_NEVER_BY_OFFSET`.

The cursor is deliberately the same shape as `brain.connectors.backfill.BackfillCursor`,
including the two refusals in `advance`, and deliberately not that class. That one names a
connector and an entity, and its `cursor` is the source's own opaque token which is never
parsed, on purpose. A rebuild's position is our own primary key, which has to be compared to
tell a real advance from a loop, so reusing the value would mean either lying about the
connector field or widening a type that a connector backfill depends on. Sharing the shape is
what was worth having; sharing the class was not.

**A rebuild is interrupted, so its state has to survive being written down.** The cursor is the
whole of that state and it is a value, which is what lets `resume_hint` print a command line
somebody pastes back. The counters ride in it as well as the position, and that is the half
that is easy to leave out: a hint carrying only the position resumes the work correctly and
loses the one number that can be compared against the corpus afterwards. See
`A_RESUMED_REBUILD_CARRIES_ITS_COUNTERS_OR_IT_CANNOT_BE_CHECKED`, and
`A_REBUILD_THAT_REPORTS_DONE_HAS_TO_HAVE_REACHED_EVERY_CHUNK` for what the comparison is for.

**What is not claimed here.** The command below plans a rebuild, refuses one that cannot run,
reports where one has got to, and refuses the claim that one finished when the arithmetic says
otherwise. It does not execute one, because executing means embedding text and rewriting rows:
the embedding service is a seam nothing implements (M7.3.3, and see
`brain.knowledge.embed_queue`), there is no queue driver to fetch a batch with, and no chunk
repository to read from or write to. The column the identity is stored in does now exist;
migration 0010 added `know.chunk.embedding_model` and `vector_query` conjoins it, so a query
cannot span a model change even when a caller never consults `corpus_identity`. What remains
missing is everything that would move a row.

Scope: domain logic. Nothing here opens a connection, loads a model or reads a clock.

Task ids: M7.3.5
"""

from __future__ import annotations

import argparse
import enum
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final

from brain.knowledge.search import (
    CHUNK,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_FIELD,
    INDEXABLE_DIMENSION_CEILING,
)

# ------------------------------------------------------------------ written-down reasons

#: Why the recorded identity is more than the model's name.
A_MODEL_NAME_IS_NOT_A_MODEL_IDENTITY: Final = (
    "Two vectors are comparable when the same weights produced them, and a name is not "
    "evidence of that. A provider updating weights behind a stable name gives a different "
    "vector space under an identical label, and a corpus half rebuilt against it carries no "
    "sign anywhere that anything changed. The width belongs in the identity for the same "
    "reason from the other direction: the same family at two dimensions is two spaces, and "
    "the one time the database catches a mistake for us is when the width is wrong. So a "
    "model that cannot name its revision is refused rather than defaulted to a first "
    "version, because a default revision is a promise nobody made on behalf of somebody who "
    "did not check."
)

#: Why a corpus holding two models loses its vector leg entirely.
A_MIXED_CORPUS_HAS_NO_VECTOR_LEG: Final = (
    "Filtering the vector leg to one model during a rebuild returns half the candidates, and "
    "a short vector leg is exactly what iterative scan produces when it reaches "
    "hnsw.max_scan_tuples, so the two are indistinguishable to everything downstream and "
    "A_SHORT_LEG_IS_NOT_AN_EMPTY_KNOWLEDGE_BASE forbids reading either as an absence. A "
    "quietly halved candidate set is the degradation this whole module is written against, "
    "arriving through the fix. So a mixed corpus has no vector leg: retrieval falls back to "
    "the lexical leg, which is unaffected and which fusion consumes on its own without "
    "special handling, and the loss of recall is a thing an operator can see and reason "
    "about rather than a slow drift in answer quality that nobody attributes to anything."
)

#: Why a rebuild that reports itself finished is checked against the corpus.
A_REBUILD_THAT_REPORTS_DONE_HAS_TO_HAVE_REACHED_EVERY_CHUNK: Final = (
    "A rebuild that skipped rows reports success, and the rows it skipped are chunks left on "
    "the old model beside the new ones with nothing anywhere recording it. Every way that "
    "happens is silent: a scan run through a narrowed reach never saw the rows it could not "
    "read, an offset cursor steps over rows whenever the set shifts underneath it, and a run "
    "resumed from a position somebody retyped starts after the gap. None of the three raises. "
    "What they have in common is arithmetic: a finished rebuild wrote as many chunks as the "
    "corpus holds, and a count that disagrees is the only evidence any of them leaves behind. "
    "The count has to come from the corpus, because a total derived from the run compares the "
    "run against itself and is right whatever the run did."
)

#: Why the counters travel in the resume hint and not only the position.
A_RESUMED_REBUILD_CARRIES_ITS_COUNTERS_OR_IT_CANNOT_BE_CHECKED: Final = (
    "A rebuild is interrupted by definition, so the state that survives an interruption is the "
    "whole of what is ever known about the run. If only the position came back, every resumed "
    "run would start its counters at zero, the final total would be the size of the last "
    "segment rather than of the corpus, and the completion check would refuse a rebuild that "
    "was fine while passing one that had skipped the first half. The position alone is enough "
    "to finish the work and not enough to say the work was finished."
)

#: Why the position is the last key written rather than a row offset.
A_REBUILD_RESUMES_BY_KEY_AND_NEVER_BY_OFFSET: Final = (
    "An OFFSET cursor re-reads every row before it on every batch, which is the cost people "
    "notice, and skips rows whenever the set shifts underneath it, which is the one they do "
    "not. A rebuild shifts the set on every batch by definition. A skipped row is a chunk "
    "left on the old model, indexed beside the new ones, with nothing anywhere reporting it, "
    "so the job reports success and the corpus is permanently mixed. The position is "
    "therefore the last chunk id written and the query is a keyset scan. The order does not "
    "have to mean anything, only to be total and stable, and a primary key is both. It does "
    "have to be collation-independent: PostgreSQL orders text by the database collation, so "
    "the scan is ordered under the C collation or a collation change between two runs can "
    "move the boundary and skip whatever fell across it."
)


class EmbeddingError(Exception):
    """An embedding arrangement that would degrade retrieval rather than fail it.

    Outside the `brain.core.errors` taxonomy, like every refusal in this package: those five
    outcomes describe an answer given to a person, and this describes a refusal to run a job
    or to compare two numbers.
    """


class MixedEmbeddingError(EmbeddingError):
    """Vectors from two models were about to be compared, or served from one index.

    Its own type rather than a message, because this is the one an operator has to be able to
    catch and turn into "the vector leg is off today", which is a different response from
    everything else here.
    """


# ------------------------------------------------------------------- the model identity

#: The column the identity is stored in, beside the vector. It does not exist yet: see the
#: module docstring on what is not claimed. Named here so the migration that adds it, the
#: writer that fills it and the reader below cannot spell it three ways.
# `EMBEDDING_MODEL_FIELD` is imported from `search`, which owns the column it names.

#: The column the vector is stored in, taken from the table rather than typed out. A key
#: spelled differently reads every row as unembedded, which fails closed into "the vector leg
#: returned nothing", and that is the exact silent degradation this module exists to prevent.
EMBEDDING_FIELD: Final[str] = CHUNK.c.embedding.name

#: How long a stored identity may be. The column that will hold it is bounded, and a value
#: that cannot be stored is otherwise discovered at the first write of a job that has already
#: embedded a batch. Nothing checks this per model, because the check below proves the
#: grammar cannot produce a longer one, and a guard that can never fire is worse than none.
MODEL_IDENTITY_CHARS: Final = 128

#: Model names as providers write them: `qwen3-embedding-0.6b`, `text-embedding-3-small`.
_MODEL_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: A revision as a tag or a commit: whatever the operator can point at to say which weights.
_REVISION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def _bounded(pattern: re.Pattern[str], chars: int) -> int:
    """The length a pattern admits, confirmed against the pattern rather than asserted.

    A regex that has been mangled on its way into a file is the failure this is here for. A
    pattern that quietly became one matching nothing refuses every model name, which somebody
    notices; a pattern that quietly became one matching everything admits a name the column
    cannot hold, which nobody notices until a rebuild is four hours in. Neither can survive
    import now, because the bound below is computed from the pattern's own behaviour.
    """
    if not pattern.match("a" * chars) or pattern.match("a" * (chars + 1)):
        msg = f"{pattern.pattern!r} does not admit exactly {chars} characters"
        raise EmbeddingError(msg)
    return chars


_MAX_NAME_CHARS: Final = _bounded(_MODEL_NAME_RE, 64)
_MAX_REVISION_CHARS: Final = _bounded(_REVISION_RE, 40)

#: The longest identity the grammar can produce: a name, a separator, a revision, a separator
#: and a width no wider than pgvector will index. Checked at import in the shape
#: `brain.knowledge.search` checks its own dimension against the indexing ceiling, so a
#: widened name pattern is a failure here rather than a truncated column value later.
_WIDEST_IDENTITY: Final = (
    _MAX_NAME_CHARS + 1 + _MAX_REVISION_CHARS + 1 + len(str(INDEXABLE_DIMENSION_CEILING))
)

if _WIDEST_IDENTITY > MODEL_IDENTITY_CHARS:  # pragma: no cover - a constant
    _msg = (
        f"the grammar admits an identity of {_WIDEST_IDENTITY} characters and the column "
        f"holds {MODEL_IDENTITY_CHARS}; a truncated identity compares unequal to itself"
    )
    raise EmbeddingError(_msg)


@dataclass(frozen=True)
class EmbeddingModel:
    """What produced a vector, in enough detail to say whether two vectors are comparable.

    Three fields, and each one has been the whole failure somewhere. See
    `A_MODEL_NAME_IS_NOT_A_MODEL_IDENTITY`.

    Frozen, because an identity that could be edited after a vector was written under it is
    an identity that can be made to claim a different space.
    """

    name: str
    #: Which weights. Refused when empty rather than defaulted, because a default revision is
    #: an assertion that the weights have not moved, made by code that cannot know.
    revision: str
    dimensions: int

    def __post_init__(self) -> None:
        if not _MODEL_NAME_RE.match(self.name):
            msg = f"{self.name!r} is not an embedding model name"
            raise EmbeddingError(msg)
        if not _REVISION_RE.match(self.revision):
            msg = (
                f"{self.name!r} names no revision ({self.revision!r}); a model without one "
                "cannot be told apart from the same name with different weights behind it, "
                "which is the model change that has no symptom"
            )
            raise EmbeddingError(msg)
        if self.dimensions < 1:
            msg = f"{self.dimensions} is not a vector width"
            raise EmbeddingError(msg)
        if self.dimensions > INDEXABLE_DIMENSION_CEILING:
            # The asymmetry `brain.knowledge.search` names: pgvector stores a vector far
            # wider than it will index, so a model chosen on quality alone is accepted by
            # every insert and refused by `CREATE INDEX`, at which point there is a whole
            # corpus embedded by it and no index to search. Refused here, before the plan.
            msg = (
                f"{self.name!r} is {self.dimensions} dimensions and pgvector indexes at most "
                f"{INDEXABLE_DIMENSION_CEILING}; every row would insert and the index build "
                "would be what fails, after the corpus had been re-embedded"
            )
            raise EmbeddingError(msg)

    @property
    def identity(self) -> str:
        """The string written beside every vector this model produced.

        One field rather than three columns, because the only question ever asked of it is
        whether two of them are equal, and three columns is three chances to compare two of
        them and forget the third.
        """
        return f"{self.name}@{self.revision}:{self.dimensions}"

    @property
    def fits_the_column(self) -> bool:
        """Whether the corpus's vector column is this model's width.

        `brain.knowledge.search.EMBEDDING_DIMENSIONS` is part of the column type, so a model
        of another width cannot be stored at all and swapping to one is a migration before it
        is a rebuild. Asked here so a plan refuses before the first batch rather than after
        it, which is the difference between a wasted minute and a wasted afternoon.
        """
        return self.dimensions == EMBEDDING_DIMENSIONS


@dataclass(frozen=True)
class EmbeddedVector:
    """Numbers that cannot be separated from the model that produced them.

    This is the structural half of the mixing rule. A bare sequence of floats can be compared
    with any other, and nothing about the expression says which spaces they came from; a value
    that carries its model can only be compared through `assert_comparable`, and there is no
    other way to get at the numbers that does not pass the model along with them.

    The width is checked against the model rather than against the column, deliberately. A
    model that returned the wrong number of dimensions is a broken provider response, and it
    is worth separating from a model whose width the column cannot hold, which is a migration
    somebody has not run.
    """

    model: EmbeddingModel
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.values) != self.model.dimensions:
            msg = (
                f"{self.model.identity} returned {len(self.values)} dimensions rather than "
                f"{self.model.dimensions}; the vector is not from the model it claims"
            )
            raise EmbeddingError(msg)


def assert_comparable(question: EmbeddingModel, corpus: str) -> None:
    """Refuse a distance between a question and a corpus embedded by different models.

    `corpus` is the identity as it is stored, a string, rather than an `EmbeddingModel`. That
    is not laziness about parsing: the stored value is the fact, and reconstructing a model
    from it in order to compare identities would introduce a second way for two identities to
    be judged equal, which is the one thing this function is for.

    An empty corpus identity is not an error. It means no row carries a vector at all, which
    is an ordinary state during ingestion, and `corpus_identity` has already refused the case
    that looks the same and is not: a row that has a vector and no model beside it.
    """
    if not corpus:
        return
    if question.identity != corpus:
        msg = (
            f"the question was embedded by {question.identity} and the corpus by {corpus}; a "
            "distance between two vector spaces is a number and not a distance"
        )
        raise MixedEmbeddingError(msg)


def corpus_identity(rows: Iterable[Mapping[str, object]]) -> str:
    """The one model every embedded row came from, or empty when none of them are embedded.

    Refuses two, which is the mixed corpus, and refuses a row holding a vector with no model
    recorded beside it, which is the corpus as it stands before this leaf is finished: those
    vectors were produced by something, and nothing anywhere records what.

    Rejected: returning the majority identity, or the first one seen. Both make the function
    total and both answer the wrong question. The caller is asking whether it may run a vector
    leg, and "mostly this model" is not a yes.
    """
    seen: set[str] = set()
    for row in rows:
        if row.get(EMBEDDING_FIELD) is None:
            continue
        identity = str(row.get(EMBEDDING_MODEL_FIELD) or "")
        if not identity:
            msg = (
                "a chunk carries a vector with no model recorded beside it; nothing can say "
                "which space it belongs to, so it cannot be compared with anything"
            )
            raise MixedEmbeddingError(msg)
        seen.add(identity)
    if len(seen) > 1:
        msg = (
            f"the corpus holds vectors from {sorted(seen)}; distances between two of these "
            "are meaningless and the index cannot tell them apart"
        )
        raise MixedEmbeddingError(msg)
    return seen.pop() if seen else ""


# ---------------------------------------------------------------------- the rebuild

#: How many chunks one batch rewrites. Two hundred, and the number is judging the wrong thing
#: if it is read as throughput: a batch is the unit of resumption, so its real cost is how
#: much work an interruption loses and how long one transaction holds its rows. Two hundred
#: chunks is seconds of work either way, and a bigger batch buys nothing that matters here
#: because the ceiling on a local model is the GPU rather than the round trip.
DEFAULT_BATCH_SIZE: Final = 200

#: What an operator runs. Named here so the message telling somebody how to resume and the
#: module they resume it with cannot drift apart.
REBUILD_COMMAND: Final = "python -m brain.knowledge.embedding"


@dataclass(frozen=True)
class RebuildCursor:
    """Where a rebuild has got to. The whole of its state, and a value on purpose.

    The shape `brain.connectors.backfill.BackfillCursor` has, for the reason
    `RESUMING_IS_NOT_RESTARTING` gives there: a run resumed from a stored cursor is
    indistinguishable from one that never stopped, and there is nothing in this module for a
    crash to lose.

    `model` rides along because resuming is where the third model gets into the corpus. A
    rebuild interrupted while rewriting to B and resumed against C leaves rows on A, rows on
    B and rows on C, and `next_batch` refuses that by comparing this against the plan.
    """

    model: EmbeddingModel
    #: The last chunk id written. The scan resumes strictly after it. Empty means the
    #: beginning, which no chunk id can be, so there is one spelling of "not started".
    after_chunk_id: str = ""
    batches: int = 0
    chunks: int = 0
    #: The scan found no rows after this one. See `RebuildBatch.is_finished`.
    exhausted: bool = False

    def __post_init__(self) -> None:
        if self.batches < 0 or self.chunks < 0:
            msg = "batches and chunks are counts and cannot be negative"
            raise EmbeddingError(msg)

    def advance(self, *, last_chunk_id: str, written: int, exhausted: bool) -> RebuildCursor:
        """Move on by one batch. Refuses the two ways a rebuild fails to terminate.

        **An advance past an exhausted cursor.** The caller has ignored `DONE` and is about to
        start from the beginning, which is the whole corpus embedded a second time.

        **A position that did not move forward.** Our key is ordered, so this is checkable in
        a way it is not for a connector's opaque token: `BackfillCursor` can only refuse a
        cursor identical to the one it was given, while this can refuse one that went
        backwards, which is the resumption bug that would silently re-embed a prefix of the
        corpus on every batch and never finish.

        A batch that wrote rows must name the last of them. Without that the position stands
        still while the counters move, which reads in a progress log as work being done.
        """
        if written < 0:
            msg = "a batch cannot write a negative number of chunks"
            raise EmbeddingError(msg)
        if self.exhausted:
            msg = (
                f"the rebuild to {self.model.identity} is already finished; advancing past "
                "the end starts it again, and the second run embeds the whole corpus twice"
            )
            raise EmbeddingError(msg)
        if written > 0 and not last_chunk_id:
            msg = (
                f"a batch wrote {written} chunk(s) and named none of them; the position "
                "would stand still while the counters moved, which reads as progress"
            )
            raise EmbeddingError(msg)
        if last_chunk_id and last_chunk_id <= self.after_chunk_id:
            msg = (
                f"the rebuild is at {self.after_chunk_id!r} and the batch ended at "
                f"{last_chunk_id!r}, which is not forward; a position that does not advance "
                "re-embeds the same prefix on every batch and never reaches the end"
            )
            raise EmbeddingError(msg)
        return replace(
            self,
            after_chunk_id=last_chunk_id or self.after_chunk_id,
            batches=self.batches + 1,
            chunks=self.chunks + written,
            exhausted=exhausted,
        )

    def resume_hint(self) -> str:
        """What an operator types to carry on. Built from the cursor rather than from prose.

        The whole model is repeated in it rather than just the position, because the
        interesting way to resume a rebuild wrongly is to remember where it got to and forget
        what it was going to.

        **The counters are in it too**, and that is not decoration: see
        `A_RESUMED_REBUILD_CARRIES_ITS_COUNTERS_OR_IT_CANNOT_BE_CHECKED`. A hint carrying only
        the position resumes the work correctly and loses the only number
        `completion_gaps` has to compare against the corpus, so every interrupted rebuild would
        report a total the size of its last segment and pass a check it should fail.
        """
        resume = f" --after {self.after_chunk_id}" if self.after_chunk_id else ""
        counters = f" --chunks {self.chunks} --batches {self.batches}" if self.batches else ""
        return (
            f"{REBUILD_COMMAND} --to-model {self.model.name} --revision {self.model.revision} "
            f"--dimensions {self.model.dimensions}{resume}{counters}"
        )


@dataclass(frozen=True)
class RebuildPlan:
    """What a rebuild is moving the corpus to, and from what.

    Refuses, at construction, the two plans that cannot do what they say. A model the column
    cannot hold is a migration somebody has not run, and finding that out at the first write
    means an afternoon of embedding was spent before the first refusal. A model the corpus is
    already on is a rebuild that costs everything and changes nothing, which is the shape a
    mistyped revision takes.
    """

    to_model: EmbeddingModel
    #: The identity the corpus holds now, from `corpus_identity`. Empty is legitimate and is
    #: the first rebuild: vectors written before anything recorded a model have to be redone,
    #: because nothing can say what produced them.
    from_identity: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        if not self.to_model.fits_the_column:
            msg = (
                f"{self.to_model.identity} is {self.to_model.dimensions} dimensions and the "
                f"column is {EMBEDDING_DIMENSIONS}; the width is part of the column type, so "
                "this is a migration before it is a rebuild"
            )
            raise EmbeddingError(msg)
        if self.from_identity == self.to_model.identity:
            msg = (
                f"the corpus is already on {self.to_model.identity}; a rebuild to the model "
                "it already holds re-embeds everything and changes nothing"
            )
            raise EmbeddingError(msg)
        if self.batch_size < 1:
            msg = "a batch of zero chunks is a round trip that returns nothing"
            raise EmbeddingError(msg)

    def start(self) -> RebuildCursor:
        """A cursor at the beginning. One place the starting state is written."""
        return RebuildCursor(model=self.to_model)


def vector_leg_is_available(*, cursor: RebuildCursor | None) -> bool:
    """Whether retrieval may run its nearest-neighbour leg (M7.3.5).

    False from the moment a rebuild is planned until the moment it finishes, because in
    between the corpus holds two models and the index cannot tell them apart. None means no
    rebuild is in flight, which is the ordinary state and the only one where the leg is sound.

    This is not a refusal of anybody's request, and there is nothing in this module that could
    become one: the lexical leg is untouched, fusion takes a missing list in its stride, and
    the answer a person gets is composed from text search instead. See
    `A_MIXED_CORPUS_HAS_NO_VECTOR_LEG` for why that is better than a leg that quietly returns
    half of what it should.
    """
    return cursor is None or cursor.exhausted


class RebuildAction(enum.StrEnum):
    """What a rebuild may do next. Closed, because everything above it branches on this."""

    EMBED = "embed"
    DONE = "done"


@dataclass(frozen=True)
class RebuildBatch:
    """One decision, and the window the caller reads to act on it.

    Carries the window rather than a query, in the shape `brain.connectors.backfill` hands
    back a `FetchRequest`: what to select is the caller's, and the boundary of what to select
    is the part that has to be right.
    """

    action: RebuildAction
    reason: str
    #: Select chunks strictly after this id, ordered by id, at most `size` of them.
    after_chunk_id: str = ""
    size: int = 0

    @property
    def is_finished(self) -> bool:
        return self.action is RebuildAction.DONE


def next_batch(*, plan: RebuildPlan, cursor: RebuildCursor) -> RebuildBatch:
    """Whether another batch is due, and which chunks it covers.

    The one refusal is the one that matters on resumption. A cursor built for a different
    model than the plan is a rebuild being carried on towards somewhere else, and the corpus
    ends holding three models rather than one. `brain.connectors.backfill.next_step` refuses
    the same mistake between a cursor and a manifest, for the same reason.

    Nothing is recorded here. `RebuildCursor.advance` is a separate call the caller makes once
    the batch has actually been written, which is what keeps a batch that failed halfway from
    moving the position past rows nobody embedded. That is the split
    `brain.ops.limits.check` makes from `LimiterState.record`.
    """
    if cursor.model.identity != plan.to_model.identity:
        msg = (
            f"the cursor is rebuilding to {cursor.model.identity} and the plan says "
            f"{plan.to_model.identity}; carrying on would leave the corpus holding both, plus "
            "whatever it started on"
        )
        raise EmbeddingError(msg)
    if cursor.exhausted:
        return RebuildBatch(
            action=RebuildAction.DONE,
            reason=(
                f"{cursor.chunks} chunk(s) re-embedded to {cursor.model.identity} over "
                f"{cursor.batches} batch(es); the vector leg is sound again"
            ),
        )
    return RebuildBatch(
        action=RebuildAction.EMBED,
        reason=(
            f"re-embedding to {cursor.model.identity} from "
            f"{plan.from_identity or 'vectors with no model recorded'}"
        ),
        after_chunk_id=cursor.after_chunk_id,
        size=plan.batch_size,
    )


# ------------------------------------------------------- progress and completion (M7.3.5)


def completion_gaps(*, cursor: RebuildCursor, corpus_chunks: int) -> tuple[str, ...]:
    """Every reason this rebuild's claim to have finished is not true.

    See `A_REBUILD_THAT_REPORTS_DONE_HAS_TO_HAVE_REACHED_EVERY_CHUNK`. A cursor that has not
    reached the end makes no claim, so it produces nothing here. That is why this is separate
    from `progress_note` rather than folded into it: an unfinished rebuild is an ordinary state
    and a finished one that did not reach every chunk is a corpus permanently holding two
    models, and a single function returning both would report the second in the voice of the
    first.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight`. A run that
    both wrote nothing and claims a corpus is wrong twice, and hearing about one of the two
    invites a second run that is wrong in the way nobody was told about.
    """
    if corpus_chunks < 0:
        msg = f"{corpus_chunks} is not a number of chunks"
        raise EmbeddingError(msg)
    if not cursor.exhausted:
        return ()
    findings: list[str] = []
    if cursor.batches == 0:
        findings.append(
            f"the rebuild to {cursor.model.identity} reports finished having run no batches; a "
            "cursor exhausted before it started claims a corpus that nobody embedded"
        )
    if cursor.chunks < corpus_chunks:
        findings.append(
            f"the rebuild wrote {cursor.chunks} of {corpus_chunks} chunk(s), so "
            f"{corpus_chunks - cursor.chunks} are still on whatever model produced them, "
            "indexed beside the new ones with nothing else recording which is which"
        )
    if cursor.chunks > corpus_chunks:
        findings.append(
            f"the rebuild wrote {cursor.chunks} chunk(s) and the corpus holds {corpus_chunks}; "
            "something has been embedded more than once, so this count cannot be used to say "
            "the corpus was covered"
        )
    return tuple(findings)


def progress_note(cursor: RebuildCursor, *, corpus_chunks: int = 0) -> str:
    """Where a rebuild has got to, in the line an operator reads before deciding what to do.

    **Partial progress has to be readable**, because this job runs over everything the company
    has ever uploaded and will be interrupted. What makes it readable is that the whole of the
    state is one value: the position, the counters and the model are one object rather than
    three things somebody reassembles out of a log at the point they are least able to.

    `corpus_chunks` is optional and zero is "nobody said", not "the corpus is empty". Reporting
    a denominator we do not have would be inventing one, and an operator reading "800 of 800"
    for a run that has covered an eighth of the corpus is worse off than one reading "800".
    `completion_gaps` takes the same number without a default, because a claim to have finished
    is checked against a count or it is not checked.

    The vector leg's availability is on the line rather than left to be worked out, because it
    is the question anybody watching a rebuild is really asking: retrieval is on the lexical leg
    alone until this finishes, and somebody may have to explain to a person why their answers
    are thinner today. It is the cursor's own answer narrowed by the count when there is one: a
    cursor that says it is finished having covered a fraction of the corpus would otherwise turn
    the leg back on over a corpus holding two models, which is the failure this whole module is
    written against arriving through the line that reports it.
    """
    covered = not corpus_chunks or not completion_gaps(cursor=cursor, corpus_chunks=corpus_chunks)
    reached = f"{cursor.chunks} of {corpus_chunks}" if corpus_chunks else str(cursor.chunks)
    return (
        f"rebuild to {cursor.model.identity}: "
        f"{'finished' if cursor.exhausted else 'in progress'}, {cursor.batches} batch(es), "
        f"{reached} chunk(s), position {cursor.after_chunk_id or 'the beginning'}, "
        f"vector leg available: {vector_leg_is_available(cursor=cursor) and covered}"
    )


# ------------------------------------------------------------------ the command

#: The command's refusal: a plan or a cursor that cannot do what it says.
EXIT_REFUSED: Final = 2

#: A rebuild that reports itself finished and did not reach every chunk. A code of its own
#: rather than sharing the refusal's, for the reason `brain.ops.worker` gives about 78 and 69:
#: the two need different actions from different people. A refusal is a command retyped; this
#: one is a corpus that has to be rebuilt again from the beginning.
EXIT_INCOMPLETE: Final = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=REBUILD_COMMAND,
        description="Plan a re-embed of the knowledge corpus onto a different model.",
    )
    parser.add_argument("--to-model", required=True, help="the model name to rebuild onto")
    parser.add_argument("--revision", required=True, help="which weights, as a tag or a commit")
    parser.add_argument("--dimensions", required=True, type=int, help="the model's vector width")
    parser.add_argument(
        "--from-identity",
        default="",
        help="what the corpus holds now, from corpus_identity; empty for an unrecorded corpus",
    )
    parser.add_argument("--batch-size", default=DEFAULT_BATCH_SIZE, type=int)
    parser.add_argument("--after", default="", help="resume strictly after this chunk id")
    # The counters a resumed run carries. Without them a resumed rebuild reports the size of
    # its last segment as its total; see `A_RESUMED_REBUILD_CARRIES_ITS_COUNTERS_OR_IT_CANNOT
    # _BE_CHECKED`. `resume_hint` prints them, so an operator pastes rather than retypes.
    parser.add_argument("--chunks", default=0, type=int, help="chunks written before this run")
    parser.add_argument("--batches", default=0, type=int, help="batches run before this run")
    parser.add_argument(
        "--finished",
        action="store_true",
        help="the run reported that it had reached the end; check that claim",
    )
    parser.add_argument(
        "--corpus-chunks",
        default=0,
        type=int,
        help="how many chunks the corpus holds, from a count over it; 0 for unstated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Plan a rebuild, report where one has got to, and check a claim that one has finished.

    It deliberately does not embed anything: see the module docstring on what is not claimed.
    What it does is everything around the embedding that has to be right for the embedding to
    be worth running. It turns a model swap from a configuration change into something an
    operator has to state and refuses the two forms of it that cannot work. It reads back a
    cursor as a state anybody can act on, which is what makes an interrupted run resumable
    rather than merely restartable. And, given a count over the corpus, it refuses the claim
    that a run finished when the arithmetic says it did not, which is the only evidence a
    rebuild that skipped rows ever leaves.

    Three exit codes rather than two. A refusal is a command retyped; an incomplete rebuild is
    a corpus that has to be done again from the beginning, and sending both to the same code
    sends the wrong person to look.
    """
    args = _parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        model = EmbeddingModel(
            name=args.to_model, revision=args.revision, dimensions=args.dimensions
        )
        plan = RebuildPlan(
            to_model=model, from_identity=args.from_identity, batch_size=args.batch_size
        )
        cursor = replace(
            plan.start(),
            after_chunk_id=args.after,
            chunks=args.chunks,
            batches=args.batches,
            exhausted=args.finished,
        )
        batch = next_batch(plan=plan, cursor=cursor)
        # Only when a count was given. Zero is "nobody said", and passing it through would read
        # as a corpus of no chunks, so every finished run would be reported as having embedded
        # more than the corpus holds.
        incomplete = (
            completion_gaps(cursor=cursor, corpus_chunks=args.corpus_chunks)
            if args.corpus_chunks
            else ()
        )
    except EmbeddingError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    position = cursor.after_chunk_id or "the beginning"
    print(f"rebuild to {model.identity}")
    if not incomplete:
        # `next_batch`'s finishing sentence says the vector leg is sound again, and it is
        # entitled to: it knows the cursor and nothing else. When the corpus count says the
        # run did not reach every chunk, that sentence is false, and printing it beside the
        # findings that contradict it would leave a reader to decide which half to believe.
        print(f"  {batch.reason}")
    if not batch.is_finished:
        # Only when there is one. A finished rebuild printing "0 at once" reads as a window of
        # nothing rather than as an ending, and the difference matters to whoever is deciding
        # whether to run the command again.
        print(f"  next: chunks after {position}, {batch.size} at once")
    print(f"  {progress_note(cursor, corpus_chunks=args.corpus_chunks)}")
    print(f"  resume with: {cursor.resume_hint()}")
    if cursor.exhausted and not args.corpus_chunks:
        # An unchecked claim, said out loud. A run that reports itself finished and offers no
        # count has not been compared with anything, and the difference between that and a
        # checked completion is the whole of what stands between a rebuild that skipped rows
        # and a corpus permanently holding two models.
        print(
            "  unchecked: nothing was compared against the corpus, so 'finished' here is the "
            "run's own word. Pass --corpus-chunks from a count over know.chunk"
        )
    if incomplete:
        print(
            "this rebuild reports finished and did not reach every chunk, so the corpus holds "
            "two models and the vector leg must stay off:",
            file=sys.stderr,
        )
        for finding in incomplete:
            print(f"  - {finding}", file=sys.stderr)
        return EXIT_INCOMPLETE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
