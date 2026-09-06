"""Everything about embedding that is decided before a socket is opened, and by whom.

M7.3.3 is "local embedding via Qwen3 through the inference server", and item 31 of
`docs/needs-rupash.md` decided on 2026-09-06 that the model lives in a service of its own.
`brain.ops.inference` is the seam to that service and holds the wire contract;
`brain.knowledge.embed_queue` holds the batch and the protocol nothing implements. What was
missing between them is this: the decisions a client would otherwise take on its own, in the
module that cannot open a connection, so that the case that is always wrong can be tested
without one. That is the split the layout table states about `brain.ops.limits` and
`brain.ops.limit_store`, and the client half is `brain.ops.inference_client`.

**An embedding failure while ingesting and one while answering are different failures, and
only the first is allowed to be invisible.** This is the decision the rest of the module is
arranged around. A chunk that could not be embedded is still in the corpus, still found by
the lexical leg, and its vector arrives when the queue re-drives the job, so nobody needs to
be told anything and the honest response is to write nothing and wait. A *question* that
could not be embedded is different in kind: the nearest-neighbour leg cannot run at all, the
answer in front of a person right now is composed from text search alone, and no later run
fixes the answer they already read. So the query leg's outage is `Outcome.DEGRADED` and never
`OK`. See `AN_INGEST_OUTAGE_IS_INVISIBLE_AND_A_QUERY_OUTAGE_IS_DECLARED`.

**The vector width is read off the column rather than configured, so a model of another width
is a migration and cannot be anything else.** `served_embedding_model` has no `dimensions`
parameter and there is no setting that could supply one: the figure comes from
`know.chunk.embedding`'s own declared type. A server running different weights states its own
width in the response, `EmbeddingModel` is built from that, and `writes_for` refuses the
identity mismatch, so a width change surfaces as a refusal rather than as rows nobody can
compare. See `THE_WIDTH_IS_THE_COLUMNS_SO_A_MODEL_CHANGE_IS_A_MIGRATION`, and `dimension_gaps`
for the disagreement that exists today between Qwen3 and this column.

**A run that stopped in the middle is not a run that finished, and `EmbedRun` cannot be built
saying otherwise.** `writes_for` already refuses a response that covers part of one batch. The
gap this closes is one level up: a rebuild is many batches, and a loop that swallowed the
third failure and carried on would return the writes of batches one, two, four and five, whose
last chunk id is past a hole nobody embedded. `RebuildCursor.advance` would accept it, because
the position moved forward, and the rows in the hole are then left on the old model with
nothing recording it. So the run stops at the first failure and reports how many of how many
batches it completed. See `A_RUN_THAT_STOPPED_IS_NOT_A_RUN_THAT_FINISHED`.

**Normalisation is checked and never applied.** `brain.knowledge.search.VECTOR_INDEX` chooses
`vector_cosine_ops` and the comment beside it says the embeddings are normalised; nothing
anywhere has ever checked that, and Qwen3 does not normalise unless it is asked to. Rejected:
normalising the vector here on arrival, which is one line and always succeeds. It would make
this process the last thing to change the numbers, so the corpus would hold values that are
not the ones the model produced under the identity recorded beside them, and `EmbeddedVector`
exists to say those two cannot be separated. A check turns a comment into a fact and names the
flag that fixes it; a transform hides which side was wrong. See
`THE_INDEX_ASSUMES_NORMALISED_VECTORS_SO_THE_RESPONSE_IS_CHECKED`.

**A question's vector is never a write.** `question_vector` returns an `EmbeddedVector` and
deliberately does not go through `writes_for`, whose output is an update to `know.chunk`. A
question is not a passage, it belongs to nobody, and the only value that could carry it into
the corpus is one this path never constructs. See `A_QUESTIONS_VECTOR_IS_NEVER_A_WRITE`.

**The timeout is what the queue can tolerate and not what the model needs**, because nothing
here knows the second figure. No such server has ever run on this host, so there is no
throughput to divide a batch by; what is knowable is that a request outlasting
`brain.ops.queue.stale_after` is a job the queue treats as orphaned and re-drives while the
first copy is still waiting, which sends the same batch twice to a server already too slow to
answer once. So `EMBED_TIMEOUT_SECONDS` is derived from those figures rather than chosen
beside them, and if the first measurement says a full batch needs longer, the fix is a smaller
batch rather than a longer timeout.

**What has no caller yet, stated plainly rather than implied.** Nothing in this repository
calls anything in this module. `embed_all` needs an `EmbeddingService`, and the only one is
`brain.ops.inference_client`, which needs an address to an inference server that has no image;
`question_batch` and `question_vector` need a retrieval path that embeds a question, and
`brain.knowledge.search.vector_query` is still called with a vector nobody produces;
`policy_gaps` is not called by `brain.ops.worker.preflight`, where it belongs beside
`embed_batch_gaps`, because that file is being edited by somebody else today. So this is a
policy nothing consults, and saying so is worth more than a wire that looks live.

Scope: domain logic. Nothing here opens a connection, loads a model or reads a clock.

Task ids: none
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from brain.core.errors import Outcome
from brain.knowledge.embed_queue import (
    Embedded,
    EmbeddingBatch,
    EmbeddingService,
    EmbeddingUnit,
    EmbeddingWrite,
    embed_batch,
)
from brain.knowledge.embedding import (
    EMBEDDING_FIELD,
    EmbeddedVector,
    EmbeddingError,
    EmbeddingModel,
)
from brain.knowledge.search import CHUNK, Vector
from brain.ops.inference import InferenceRefused, InferenceTask, served_model
from brain.ops.queue import HEARTBEAT_SECONDS, stale_after

# ------------------------------------------------------------------ written-down reasons

#: Why the two legs fail differently, and which of them is allowed to say nothing.
AN_INGEST_OUTAGE_IS_INVISIBLE_AND_A_QUERY_OUTAGE_IS_DECLARED: Final = (
    "A chunk whose embedding failed is still a chunk: it is in the corpus, the lexical leg "
    "still finds it, and the queue re-drives the job, so the only correct response is to "
    "write nothing and let it arrive late. Nobody is told, and nobody needs to be, because "
    "nothing a person can see is wrong. A question whose embedding failed is not the same "
    "event wearing different clothes. There is no vector, so there is no nearest-neighbour "
    "leg, so the answer being composed at that moment comes from text search alone, and no "
    "later run repairs the answer somebody has already read. That is the degradation "
    "brain.knowledge.embedding is written against arriving from the other end of the "
    "pipeline, and the one thing that stops it being invisible is refusing to call it OK. So "
    "the ingest leg fails the job and the query leg returns Outcome.DEGRADED, and neither "
    "writes a row. What is deliberately not done on either leg is a retry inside the call: "
    "the ingest leg already has one, which is the queue, and a loop that slept and tried "
    "again would hold a slot for the length of an outage while the queue thought the job was "
    "running."
)

#: Why the width is not a setting, and what a change to it actually is.
THE_WIDTH_IS_THE_COLUMNS_SO_A_MODEL_CHANGE_IS_A_MIGRATION: Final = (
    "The width is part of the type of know.chunk.embedding, so it is a fact about the "
    "database and not about a deployment. Making it configurable would let an operator point "
    "an install at a model of another width, at which point every insert is refused by "
    "PostgreSQL, or worse, the width happens to match and the vectors are from a space "
    "nothing recorded. So served_embedding_model has no dimensions parameter, and the figure "
    "it uses is read off the column object rather than from a constant that sits beside it: a "
    "column altered without the constant being edited would otherwise leave the two "
    "disagreeing with nothing to notice. What is left is a model whose native width is not "
    "the column's, and that is a migration and a full re-embed, which is what dimension_gaps "
    "says in words rather than leaving somebody to discover at the first insert."
)

#: Why a partial run reports itself as one, and why it stops rather than carrying on.
A_RUN_THAT_STOPPED_IS_NOT_A_RUN_THAT_FINISHED: Final = (
    "writes_for refuses a response covering part of one batch, and that is not enough on its "
    "own, because a rebuild is many batches and the loop over them is where partial success "
    "gets rounded up. A loop that caught the third batch's failure and carried on would hand "
    "back the writes of one, two, four and five; their last chunk id is past a hole, "
    "RebuildCursor.advance accepts it because the position moved forward, and the rows in the "
    "hole stay on the old model with nothing anywhere recording it, which is exactly the "
    "silent state A_REBUILD_RESUMES_BY_KEY_AND_NEVER_BY_OFFSET is about. So the run stops at "
    "the first failure, its position is the last chunk of the last batch that completed "
    "whole, and it carries how many of how many batches it managed. A run holding a failure "
    "cannot also report itself complete, and that is refused at construction rather than left "
    "to a caller reading the right field."
)

#: Why an un-normalised vector is refused rather than normalised on arrival.
THE_INDEX_ASSUMES_NORMALISED_VECTORS_SO_THE_RESPONSE_IS_CHECKED: Final = (
    "brain.knowledge.search.VECTOR_INDEX picks vector_cosine_ops and says in a comment that "
    "the embeddings are normalised, so cosine and inner product rank identically. That is an "
    "assertion about the far side that nothing has ever tested, and it is not free: sentence "
    "encoders return un-normalised vectors unless asked, so the claim is one flag away from "
    "being false and the day it becomes false is the day somebody swaps the operator class "
    "for the faster one and every ranking quietly changes. Normalising here was rejected. It "
    "always succeeds, which is the problem: the numbers written to the corpus would then not "
    "be the numbers the model produced, under an identity that says they are, and "
    "EmbeddedVector exists precisely to say those two cannot be separated. A check makes the "
    "comment a fact and its message names the flag on the far side; a transform makes it true "
    "by rewriting the evidence."
)

#: Why a question's vector never travels as a write.
A_QUESTIONS_VECTOR_IS_NEVER_A_WRITE: Final = (
    "The response to a question and the response to a batch of chunks arrive in the same "
    "shape, and the tempting saving is to read both with writes_for. Its output is an "
    "EmbeddingWrite, which is an update to a row of know.chunk, and a question has no row: it "
    "is nobody's passage, it carries no permissions, and a value that could be written is a "
    "value somebody eventually writes. So the query leg has its own reader that returns the "
    "vector and nothing that could reach the corpus. The batch's id is a constant rather than "
    "an invented chunk id for the same reason from the other side: it cannot be produced by "
    "chunk_document, which builds every id as an item id and a four-digit ordinal, so a "
    "question can never be confused for a passage even by a response that is wrong."
)


class EmbeddingUnavailable(EmbeddingError):  # noqa: N818 - the family in embedding.py has no suffixes
    """The service could not be asked, or did not answer. Distinct from a bad answer.

    Its own name inside `EmbeddingError` for the reason `InferenceRefused` has one: an
    operator has to be able to tell "the server is down" from "the server said something
    nobody expected", because the first is a deployment and the second is a contract. Both
    are caught by anything wrapping the embed leg, and both write nothing.
    """


# ------------------------------------------------------------------ the two legs (M7.3.3)


class EmbeddingLeg(enum.StrEnum):
    """Which side of the corpus an embedding was for. Closed, because the policy is a map.

    Two, and the difference between them is the whole of
    `AN_INGEST_OUTAGE_IS_INVISIBLE_AND_A_QUERY_OUTAGE_IS_DECLARED`. A third would need
    somebody to decide what an outage means for it rather than inheriting one by accident.
    """

    #: Writing vectors for chunks that are already in the corpus.
    INGEST = "ingest"
    #: Embedding a question so the nearest-neighbour leg can run.
    QUERY = "query"


@dataclass(frozen=True)
class OutageResponse:
    """What happens on one leg when the service cannot answer, as a value rather than prose.

    `writes_anything` is on here even though it is False for both legs, and that is
    deliberate: it is the field a future third leg would have to fill in, and a policy where
    the dangerous answer has to be typed out is one nobody reaches by omission.
    """

    leg: EmbeddingLeg
    #: Whether any row is written. False on both legs; see the class docstring.
    writes_anything: bool
    #: Whether something else will try again without a person asking it to.
    retried: bool
    #: What the caller reports. Never `Outcome.DENIED` or `Outcome.ABSENT`: an outage is a
    #: fact about this system and says nothing about what exists or who may see it.
    outcome: Outcome
    reason: str


#: Exhaustive over `EmbeddingLeg`, in the shape `brain.ops.limit_store.UNREACHABLE_POLICY` is
#: exhaustive over `LimitScope`, and tested per member for the same reason: a missing entry
#: would otherwise take whichever behaviour the lookup happened to fall through to.
OUTAGE_POLICY: Final[Mapping[EmbeddingLeg, OutageResponse]] = MappingProxyType(
    {
        EmbeddingLeg.INGEST: OutageResponse(
            leg=EmbeddingLeg.INGEST,
            writes_anything=False,
            retried=True,
            outcome=Outcome.FAILED,
            reason=(
                "the job failed and nothing was written; the chunks stay unembedded and the "
                "queue re-drives it, which is safe because an embedding is an update of two "
                "columns keyed by chunk id"
            ),
        ),
        EmbeddingLeg.QUERY: OutageResponse(
            leg=EmbeddingLeg.QUERY,
            writes_anything=False,
            retried=False,
            outcome=Outcome.DEGRADED,
            reason=(
                "the question has no vector, so the nearest-neighbour leg did not run and "
                "this answer was composed from the lexical leg alone; nothing repairs an "
                "answer that has already been read, so it is reported rather than absorbed"
            ),
        ),
    }
)


def outage_response(leg: EmbeddingLeg) -> OutageResponse:
    """What this leg does when the service cannot answer, or a refusal naming the legs.

    Refuses rather than returning a default, matching `brain.ops.inference.served_model`. A
    default here is the one that gets chosen by accident, and the accident nobody notices is
    the one that reports a degraded answer as an ordinary one.
    """
    response = OUTAGE_POLICY.get(leg)
    if response is None:
        msg = (
            f"no outage response is declared for the {leg.value!r} leg; every leg has to say "
            f"what an unreachable service means for it, and the ones that do are "
            f"{sorted(one.value for one in OUTAGE_POLICY)}"
        )
        raise EmbeddingError(msg)
    return response


# ------------------------------------------------------ the width, from the column (M7.3.3)


def _column_dimensions() -> int:
    """The width of `know.chunk.embedding`, read off the column rather than from a constant.

    `brain.knowledge.search.EMBEDDING_DIMENSIONS` is the number the column was built from and
    importing it would be the obvious thing to do. It is one step too far away: a column
    altered to another width without that constant being edited leaves the two disagreeing,
    and the disagreement presents as every insert being refused by PostgreSQL long after the
    model was chosen. The type object is the fact.
    """
    column_type = CHUNK.c[EMBEDDING_FIELD].type
    if not isinstance(column_type, Vector):
        msg = (
            f"{EMBEDDING_FIELD} is a {type(column_type).__name__} rather than a vector column, "
            "so nothing here can say what width a model has to produce"
        )
        raise EmbeddingError(msg)
    return column_type.dimensions


#: What the corpus can hold, which is the only width this system may ask for.
COLUMN_DIMENSIONS: Final[int] = _column_dimensions()

#: What Qwen3-Embedding-0.6B produces. **Not measured here**, in the register
#: `brain.ops.inference.ServedModel.sizing_basis` requires: it is the hidden size on the
#: published model card, and that card also offers Matryoshka truncation, which shortens a
#: vector and cannot lengthen one. So this figure is a ceiling as well as a default, and no
#: setting on the far side turns 1024 into the 1536 this column holds. There is no such server
#: on this host, so there is nothing to measure it against; `dimension_gaps` is what compares
#: the two ends rather than a sentence here claiming they agree.
QWEN3_EMBEDDING_DIMENSIONS: Final = 1024


def served_embedding_model(*, revision: str) -> EmbeddingModel:
    """The model this system will record against a vector. No width parameter, deliberately.

    The name comes from `brain.ops.inference.SERVED_MODELS`, which is where the weights the
    container was sized for are declared, so a model served without being declared there is
    memory no budget accounted for and cannot be reached through this function at all.

    The revision is the caller's because it is the one part nobody here can know: it is which
    weights are in the volume, and `EmbeddingModel` refuses an empty one rather than defaulting
    to a first version, which would be a promise made on behalf of somebody who did not check.

    The width is neither, and that is the point. See
    `THE_WIDTH_IS_THE_COLUMNS_SO_A_MODEL_CHANGE_IS_A_MIGRATION`.
    """
    return EmbeddingModel(
        name=served_model(InferenceTask.EMBEDDING).name,
        revision=revision,
        dimensions=COLUMN_DIMENSIONS,
    )


def dimension_gaps(
    *,
    model_dimensions: int = QWEN3_EMBEDDING_DIMENSIONS,
    column_dimensions: int = COLUMN_DIMENSIONS,
) -> tuple[str, ...]:
    """Whether the served model can produce what this column holds, in words naming the fix.

    **This is a schema finding and not a configuration one**, which is why it is a sentence
    about a migration rather than about a setting. Both figures are parameters with defaults
    for the reason `brain.ops.inference.weights_mib` takes one: a check that can only ever be
    run against the constants beside it cannot be shown to fail.

    It fires today. The column is the width of a hosted model chosen in
    `brain.knowledge.search` and Qwen3-Embedding-0.6B is narrower, so this deployment cannot
    embed with the model it names until somebody decides which of the two moves. That decision
    is the owner's for the same reason item 31's three ways out are: it trades answer quality
    against a container this host cannot fit, and picking one here would spend a migration on
    a width the next decision changes again.
    """
    if model_dimensions == column_dimensions:
        return ()
    return (
        f"the served embedding model produces {model_dimensions} dimensions and "
        f"know.chunk.embedding holds {column_dimensions}; the width is part of the column "
        "type, so this is a migration that alters the column and rebuilds the vector index, "
        "plus a re-embed of every chunk, and not a setting anybody can change. Nothing is "
        "written in the meantime: an insert of the wrong width is refused by PostgreSQL",
    )


# ------------------------------------------------------ normalisation, checked (M7.3.3)

#: The relative error a half-precision pipeline accumulates before a vector is even returned.
#: A judgement rather than a measurement, in the register `PARSE_EXPANSION` is: fp16 carries
#: about three decimal digits, so a norm computed from fp16 components is right to about this.
FP16_ACCUMULATED_ERROR: Final = 1e-3

#: How far a returned vector's norm may sit from one. Ten times the figure above, so ordinary
#: half-precision arithmetic can never trip it, and far below one, so a vector that was not
#: normalised at all cannot pass however small its scale happens to be.
VECTOR_NORM_TOLERANCE: Final = 1e-2

if VECTOR_NORM_TOLERANCE <= FP16_ACCUMULATED_ERROR:  # pragma: no cover - a constant
    _msg = (
        f"a norm tolerance of {VECTOR_NORM_TOLERANCE} is inside the {FP16_ACCUMULATED_ERROR} "
        "a half-precision pipeline accumulates, so every honest response would be refused"
    )
    raise EmbeddingError(_msg)

if VECTOR_NORM_TOLERANCE >= 1.0:  # pragma: no cover - a constant
    _msg = (
        f"a norm tolerance of {VECTOR_NORM_TOLERANCE} admits a vector of every scale down to "
        "zero, which is the check not being one"
    )
    raise EmbeddingError(_msg)


def vector_norm(values: Sequence[float]) -> float:
    """The Euclidean length of a returned vector.

    `math.fsum` rather than the built-in sum, so the error in the check is not the thing the
    check measures: over a thousand terms an ordinary sum accumulates its own drift, and a
    refusal has to be the far side's fault and never this arithmetic's.
    """
    return math.sqrt(math.fsum(value * value for value in values))


def accept_vectors(embedded: Sequence[Embedded]) -> tuple[Embedded, ...]:
    """The vectors a response carried, or a refusal that names the flag on the far side.

    Checks what `brain.knowledge.search.VECTOR_INDEX` assumes and nothing checks. See
    `THE_INDEX_ASSUMES_NORMALISED_VECTORS_SO_THE_RESPONSE_IS_CHECKED` for why this is a check
    rather than a normalisation, and `InferenceRefused` rather than a name of its own because
    this is the same event that class already describes: the service answered, and the answer
    cannot be written down.

    Returns the vectors rather than None so a caller cannot use the unchecked list by
    forgetting to assign, which is the shape `brain.knowledge.embed_queue.writes_for` uses.
    """
    for one in embedded:
        norm = vector_norm(one.vector.values)
        if abs(norm - 1.0) > VECTOR_NORM_TOLERANCE:
            msg = (
                f"chunk {one.chunk_id!r} came back with a vector of length {norm:.4f} rather "
                "than one. The vector index is built with vector_cosine_ops on the stated "
                "assumption that these are normalised, and nothing else in this system checks "
                "it; ask the server to normalise its embeddings rather than normalising them "
                "here, because a vector rewritten on arrival is no longer the one the recorded "
                "model produced"
            )
            raise InferenceRefused(msg)
    return tuple(embedded)


# ------------------------------------------------------ how long a request may take (M7.3.3)

#: The most a single embedding request may take before it is abandoned. Derived from the
#: queue's own figures rather than chosen beside them: a worker writes its heartbeat in its
#: own transaction, so a worker blocked in a request is a worker writing none, and a request
#: that outlasts `stale_after` is a job the queue treats as orphaned and re-drives while the
#: first copy is still waiting. One heartbeat of margin, which is the smallest unit the queue
#: measures staleness in.
#:
#: **It is not an estimate of how long a batch takes.** Nothing here knows that: no inference
#: server has ever run on this host, so there is no throughput to divide a batch by. If a
#: measurement one day says a full batch needs longer than this, the answer is a smaller
#: batch, because a longer timeout is a job running twice.
EMBED_TIMEOUT_SECONDS: Final[float] = stale_after().total_seconds() - HEARTBEAT_SECONDS


# ------------------------------------------------------ a question, not a passage (M7.3.3)

#: What the wire calls the single input a question is sent as. A constant rather than an
#: invented chunk id, and one no chunk can hold: `chunk_document` builds every id as an item
#: id, a full stop and a four-digit ordinal. See `A_QUESTIONS_VECTOR_IS_NEVER_A_WRITE`.
QUESTION_UNIT_ID: Final = "question"


def question_batch(text: str, *, model: EmbeddingModel) -> EmbeddingBatch:
    """One question, as the same batch a chunk would travel in.

    The same value rather than a second request shape, because the server, the budget and
    every refusal in `decode_embeddings` apply unchanged to one input, and a second shape
    would be a second thing to keep right. What is not shared is what comes back: see
    `question_vector`.
    """
    return EmbeddingBatch(model=model, units=(EmbeddingUnit(chunk_id=QUESTION_UNIT_ID, text=text),))


def question_vector(embedded: Sequence[Embedded]) -> EmbeddedVector:
    """The question's vector, or a refusal. Never an `EmbeddingWrite`.

    Refuses a response that is not exactly one vector for exactly the id that was sent, which
    is `A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION` applied to a batch of one:
    a response carrying two entries, or one naming something else, is a server answering a
    different question, and taking the first entry would embed the wrong text with nothing
    downstream able to see it.

    The width is not checked here and does not need to be: `EmbeddedVector` refuses a vector
    whose length disagrees with the model that claims it, and `vector_query` refuses one whose
    length disagrees with the column.
    """
    if len(embedded) != 1:
        msg = (
            f"a question was sent as one input and {len(embedded)} vector(s) came back; there "
            "is no rule for choosing among them, and choosing the first embeds whichever text "
            "the far side happened to put there"
        )
        raise InferenceRefused(msg)
    only = embedded[0]
    if only.chunk_id != QUESTION_UNIT_ID:
        msg = (
            f"the response names {only.chunk_id!r} and the question was sent as "
            f"{QUESTION_UNIT_ID!r}; a vector matched to an input by its position rather than "
            "its id is how the wrong text gets searched for"
        )
        raise InferenceRefused(msg)
    return only.vector


# ------------------------------------------------------ many batches, one run (M7.3.3)


@dataclass(frozen=True)
class EmbedRun:
    """What a run over several batches actually achieved, and whether it finished.

    The invariants are enforced here rather than left to a caller reading the right field,
    because the failure this closes is a caller reading the wrong one. See
    `A_RUN_THAT_STOPPED_IS_NOT_A_RUN_THAT_FINISHED`.
    """

    planned: int
    completed: int
    writes: tuple[EmbeddingWrite, ...] = ()
    #: Why the run stopped, empty when it did not. A string rather than an exception, because
    #: this value is what a caller logs and what a cursor is advanced from, and an exception
    #: carried in a field is one somebody re-raises far from where it happened.
    failure: str = ""

    def __post_init__(self) -> None:
        if self.planned < 0 or self.completed < 0:
            msg = "a run cannot have planned or completed a negative number of batches"
            raise EmbeddingError(msg)
        if self.completed > self.planned:
            msg = (
                f"a run completed {self.completed} of {self.planned} batch(es), which is more "
                "than it had; the count that would be trusted is the one that is wrong"
            )
            raise EmbeddingError(msg)
        if self.failure and self.completed == self.planned:
            msg = (
                f"a run of {self.planned} batch(es) reports every one complete and also "
                f"carries a failure ({self.failure}); one of the two is false and a reader "
                "cannot tell which"
            )
            raise EmbeddingError(msg)
        if not self.failure and self.completed != self.planned:
            msg = (
                f"a run stopped after {self.completed} of {self.planned} batch(es) and says "
                "why nowhere; a run that stopped for no stated reason is reported as one that "
                "was interrupted by nothing, and its position is past rows nobody embedded"
            )
            raise EmbeddingError(msg)

    @property
    def is_complete(self) -> bool:
        """Whether every planned batch was written. The one question a caller may ask."""
        return not self.failure and self.completed == self.planned

    @property
    def last_chunk_id(self) -> str:
        """The last chunk actually written, which is how far a cursor may be advanced.

        The last write rather than the largest id. `plan_batches` keeps the order it was
        given and the rebuild scan is ordered by key, so the two agree; where they would not,
        `RebuildCursor.advance` refuses a position that is not forward, which is the check
        that should fire rather than this one quietly picking the biggest number it can see.
        """
        return self.writes[-1].chunk_id if self.writes else ""


def embed_all(batches: Sequence[EmbeddingBatch], service: EmbeddingService) -> EmbedRun:
    """Send these batches in order and stop at the first one that fails.

    **Stops rather than continuing**, and that is the decision in this function. Carrying on
    would produce writes whose last chunk id is beyond a hole nobody embedded, and the cursor
    would accept it because the position moved forward. It is also the right thing on the
    evidence: the first failure is almost always the server, and the batches after it fail too
    while costing a round trip each.

    Catches `EmbeddingError` and nothing wider. A `TypeError` here is a bug in this repository
    and has to reach somebody, not be folded into a run report as though a server had done it.

    The writes are returned rather than applied, matching `embed_batch`: this module has no
    session, and the caller is what holds a transaction to take row locks in.
    """
    writes: list[EmbeddingWrite] = []
    for done, batch in enumerate(batches):
        try:
            writes.extend(embed_batch(batch, service))
        except EmbeddingError as exc:
            return EmbedRun(
                planned=len(batches),
                completed=done,
                writes=tuple(writes),
                failure=str(exc),
            )
    return EmbedRun(planned=len(batches), completed=len(batches), writes=tuple(writes))


# ------------------------------------------------------------------ the deployment check


def policy_gaps(
    *,
    model_dimensions: int = QWEN3_EMBEDDING_DIMENSIONS,
    column_dimensions: int = COLUMN_DIMENSIONS,
    timeout_seconds: float = EMBED_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Every reason this deployment cannot embed, whatever the inference server is doing.

    Nothing calls this. It belongs beside `embed_batch_gaps` in `brain.ops.worker.preflight`,
    which is the process that would run these batches, and that file is being edited by
    somebody else today; wiring it there is one line and is not claimed here.

    Two checks, and they fail in opposite directions. The width is a refusal at the first
    insert, which is loud. The timeout is not a refusal at all: a request allowed to outlast
    `stale_after` produces a second copy of the job rather than an error, so the symptom is
    load rather than a message, and it appears only when the server is already slow.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight`.
    """
    findings = list(
        dimension_gaps(model_dimensions=model_dimensions, column_dimensions=column_dimensions)
    )
    stale_seconds = stale_after().total_seconds()
    if timeout_seconds >= stale_seconds:
        findings.append(
            f"an embedding request may take {timeout_seconds}s and the queue treats a job as "
            f"orphaned after {stale_seconds}s; a slow server would have the same batch sent "
            "twice rather than reported once, and nothing in the log would say so"
        )
    if timeout_seconds <= 0:
        findings.append(
            f"an embedding request is allowed {timeout_seconds}s, which is a client that "
            "abandons every request before the server has read it"
        )
    return tuple(findings)
