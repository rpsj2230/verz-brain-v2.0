"""Cutting embedding work into batches a slot can hold, and the jobs that carry them.

Embedding a thousand chunks in one request and embedding them in ten is the same work and a
different failure. **A batch is a budget, not a convenience.** It sets the peak memory of the
process that runs it, and it sets how much work is lost when one request times out. So the
bound is stated, it is derived from the slot the job runs in, and it is checked when the batch
is built rather than while the response is arriving. A bound checked during the work has
already been exceeded by the time it fires, which is the argument `brain.knowledge.parse_budget`
makes about a document; this is that argument one step along, with the batch rather than the
file as the quantity, and with the difference that a batch is a quantity we choose.

**No model enters this image, and that is a decision rather than an omission.** M7.3.3 is
"local embedding via Qwen3 through the inference server", and item 31 of `docs/needs-rupash.md`
was decided on 2026-09-06 as Option A: the models live in a separate service and the Brain asks
it over the network, so the machine-learning stack measured there at roughly 1.5 GB never
becomes a dependency of this repository. What is left here is a seam. `EmbeddingService` is the
whole of what this module may ask of that service, declared the way
`brain.knowledge.rows.RowSource` and `brain.tools.run_skill.ScriptRunner` are declared, and
**nothing in this repository implements it**. See `NOTHING_IMPLEMENTS_THE_EMBEDDING_SERVICE`.

**An embedding writes two columns and can never write a third, which is how a re-embed cannot
lose a scope.** The catastrophic version of this leaf is quiet: a rebuild re-embeds the text,
rewrites the row, and drops or defaults the permissions, leaving a corpus everybody can read
with nothing anywhere reporting it. `brain.knowledge.chunking` makes a chunk's permissions
structural by refusing to build a `Chunk` outside `chunk_document`; the equivalent here is that
there is no value in this module that can carry a permission at all. `EmbeddingWrite.columns` is
`WRITTEN_COLUMNS` and the mapping it returns is read-only, so an embedding is an UPDATE of two
columns of a row that already exists rather than an insert of a row somebody has to remember to
fill in. See `AN_EMBEDDING_WRITES_TWO_COLUMNS_AND_NEVER_A_PERMISSION`.

That ordering falls out of a decision `brain.knowledge.search` already took rather than being
chosen here: the vector column is nullable "because embedding is asynchronous", so the chunk row
is written first, unembedded, carrying the permissions `chunk_document` copied onto it, and the
embedding fills two of its columns later. Rejected: embedding first and inserting the whole row
with its vector. It would make ingestion depend on the inference server being reachable, so an
outage there would stop uploads instead of delaying the vector leg, and it would put the
permission columns back into the hands of the writer that has no document in front of it.

**A vector is matched to a chunk by id and never by position.** A provider returns a list, and a
list that is short, reordered or padded pairs the wrong text with the wrong vector. Nothing
downstream can see that: every row has a plausible vector, retrieval returns confident nonsense
for the affected chunks, and the corpus is wrong in a way no query reports. So the response is
checked against the batch as a set of ids, and the three ways it can disagree are three
different refusals. See `A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION`.

**The model is part of the identity of an embedding**, and that half is already built:
`brain.knowledge.embedding.EmbeddingModel` carries name, revision and width, `EmbeddedVector`
cannot exist without one, and `know.chunk.embedding_model` stores it beside the numbers. This
module's contribution is that a write cannot be produced without the identity travelling with
it, because `EmbeddingWrite` holds an `EmbeddedVector` rather than a bare sequence of floats and
its two columns are filled from the same object.

**Re-driving an embedding job is safe, and that is the only `Redrive.SAFE` in this repository,
so it is worth saying what makes it true and what would stop it.** The write is an update of two
columns keyed by chunk id, with values a deterministic function of the text and the weights, so
running it twice leaves the row exactly as running it once did. Two things would end that. An
insert rather than an update would make the second run a duplicate-key failure or a second row.
And a metered provider would make the second run a second invoice, which is a side effect the
world can see; that one is not true of a local inference server and is the reason Option A
matters here beyond the memory it saves.

**What has no caller yet, stated plainly rather than implied.** `embed_batch_gaps` is called by
`brain.ops.worker.preflight`, which is the process that would run these batches, and it is
checked there rather than on the parse worker's condition because a slot is a slot: any
container draining the queue may be handed an embedding batch. Everything else here has no
caller and cannot have one today, and there are three separate reasons rather than one.
`units_for`, `plan_batches`, `embed_batch` and `writes_for` need an `EmbeddingService`, and
nothing implements one. `embed_job` and `rebuild_job` need a queue driver, and
`brain.ops.queue.NO_DRIVER_IS_INSTALLED` is the sentence saying there is not one, so nothing
has ever been enqueued. And `EmbeddingWrite` is an update nobody applies, because there is no
chunk repository in this repository: `know.chunk` is a table and a set of queries, with no
writer. Naming the three is worth more than a mechanism that looks wired.

The driver's own name is deliberately not written here, and that is not squeamishness:
`tests/unit/test_queue.py` fails the build when any module outside `brain.ops.queue` names an
implementation, which is what makes "replacing the queue changes one file" a fact rather than
an intention. This module found that out by being caught by it.

Scope: domain logic. Nothing here opens a connection, loads a model or reads a clock.

Task ids: M7.3.4
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol

from brain.core.department import DEPARTMENT_FIELD
from brain.gate.context import TrafficClass
from brain.knowledge.chunking import Chunk, ChunkBounds
from brain.knowledge.embedding import (
    DEFAULT_BATCH_SIZE,
    EMBEDDING_FIELD,
    MODEL_IDENTITY_CHARS,
    EmbeddedVector,
    EmbeddingError,
    EmbeddingModel,
    MixedEmbeddingError,
    RebuildCursor,
    RebuildPlan,
    next_batch,
)
from brain.knowledge.search import (
    CHUNK,
    CHUNK_ID_CHARS,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_FIELD,
    SCOPE_COLUMNS,
)
from brain.knowledge.visibility import OWNER_FIELD
from brain.ops.queue import (
    MAX_ARGUMENT_CHARS,
    MIB_PER_SLOT,
    Job,
    Redrive,
    queue_name_for,
)

#: Bytes in a mebibyte. Spelled here for the reason `brain.knowledge.parse_budget` spells it:
#: every budget figure below is declared in MiB, which is the unit `brain.ops.wiring` and every
#: compose file use, and compared against a cost, which is bytes.
MIB: Final = 1024 * 1024


# ------------------------------------------------------------------ written-down reasons

#: Why the batch size is a bound rather than a throughput setting.
A_BATCH_IS_A_BUDGET_AND_IS_CHECKED_BEFORE_THE_REQUEST: Final = (
    "A batch decides two things and neither of them is speed. It decides peak memory, because "
    "the whole response is in the process at once as parsed floats and as the text they were "
    "parsed from. And it decides the failure radius, because nothing is written until the "
    "response arrives, so an interrupted batch loses all of itself and no less. Both are "
    "properties of the batch as it is assembled, so both are checked then: a limit enforced by "
    "watching memory climb has already been passed when it fires, and a limit on how much work "
    "an interruption may lose cannot be applied after the interruption."
)

#: Why an embedding is two columns and why that is what protects the corpus.
AN_EMBEDDING_WRITES_TWO_COLUMNS_AND_NEVER_A_PERMISSION: Final = (
    "A chunk carries the permissions of the document it came from, and the way to lose them is "
    "not to delete them, it is to rewrite the row from something that never had them. A rebuild "
    "reads text and produces vectors; it has no document in front of it, so any permission it "
    "wrote would be a default or a guess, and the default scope is the unrestricted one. So an "
    "embedding is an update of the vector and the model identity on a row that already exists, "
    "and there is no value in this module with a field for a scope, an owner or a visibility. "
    "The failure this closes has no symptom: the text is right, the vector is right, the "
    "citations resolve, and the passage is readable by people who were never granted it."
)

#: Why the response is checked as a set of ids rather than zipped with the request.
A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION: Final = (
    "Providers return lists, and a list is matched to a request by position. A response that is "
    "short by one, reordered, or padded then pairs every chunk after the first difference with "
    "another chunk's vector. Nothing downstream can tell: each row holds a well-formed vector "
    "from the right model, the index builds, and retrieval returns the wrong passages "
    "confidently for as long as nobody re-embeds. So the vectors are consumed as ids and the "
    "three ways a response can disagree with its batch, an id that was not asked for, an id "
    "that was asked for and did not come back, and the same id twice, are three refusals."
)

#: What is on the other side of the seam, and what is not.
NOTHING_IMPLEMENTS_THE_EMBEDDING_SERVICE: Final = (
    "Option A, decided 2026-09-06: the embedding model lives in an inference service and the "
    "Brain asks it over the network, so no machine-learning dependency enters this image. That "
    "service is not built, and no class in this repository satisfies EmbeddingService. What is "
    "written here is the shape one has to fit: it is given a batch that has already been "
    "checked against a memory budget, and it returns one vector per chunk id, each carrying the "
    "model that produced it. A fake in the test suite is the only implementation there has ever "
    "been, and it exists to exercise the refusals rather than to stand in for a service."
)


# ------------------------------------------------------------------ what one batch costs

#: What one float in a returned vector costs while it is held as Python objects. Measured on
#: this interpreter rather than reasoned about: `sys.getsizeof(0.5)` is 24 bytes for the object
#: and a sequence holds an 8-byte pointer to it. Distinct floats are distinct objects, so a
#: vector of a thousand is a thousand of them.
FLOAT_BYTES_IN_A_SEQUENCE: Final = 32

#: What one float costs in the response before it is parsed, and it is counted because both
#: copies exist at once: the decoder holds the text while it builds the objects. Measured:
#: `json.dumps` of 1536 full-precision floats is 29,184 characters, which is 19 per float,
#: rounded up because a provider may send more digits than this one did.
JSON_BYTES_PER_FLOAT: Final = 20

#: What one character of a chunk's text costs on the way out. Four because CPython stores a
#: `str` at the width of its widest code point, so one emoji anywhere quadruples the whole
#: string, and doubled because the text is held twice: once as the unit and once inside the
#: serialised request body.
BYTES_PER_CHARACTER: Final = 4 * 2

#: What the slot's own share is, which the batch may not have. `MIB_PER_SLOT` is what one
#: in-flight job costs altogether, and `brain.ops.queue` says what that covers: a database
#: connection, the task's working set and the interpreter's share. The batch is the working set
#: and not the other two, so a budget equal to the slot would be the same mistake
#: `brain.knowledge.parse_budget.PARSE_WORKER_RESERVE_MIB` exists to prevent, which is a process
#: told it may use everything the accounting already counts.
EMBED_SLOT_RESERVE_MIB: Final = 16


def batch_budget_bytes(
    *, slot_mib: int = MIB_PER_SLOT, reserve_mib: int = EMBED_SLOT_RESERVE_MIB
) -> int:
    """The most one batch may be declared to cost, inside one queue slot.

    Derived from the slot rather than set beside it, and from the slot rather than from the
    container, because a batch runs in one slot however large the container is. Both arguments
    are parameters with defaults for the reason `brain.ops.queue.concurrency_gaps` gives about
    itself: a check that can only ever be run against the constant it lives next to cannot be
    shown to fail, and a check nobody has seen fail is a check nobody knows works.
    """
    return (slot_mib - reserve_mib) * MIB


def vector_cost_bytes(dimensions: int = EMBEDDING_DIMENSIONS) -> int:
    """What one returned vector costs while the response is being read.

    The parsed objects and the unparsed text at the same time, because they are held at the
    same time. Counting only the parsed side halves the figure and the half that is missing is
    the one a decoder is holding at the moment memory peaks.
    """
    return dimensions * (FLOAT_BYTES_IN_A_SEQUENCE + JSON_BYTES_PER_FLOAT)


def batch_cost_bytes(units: Sequence[EmbeddingUnit], *, dimensions: int) -> int:
    """What sending these chunks and reading their vectors back is expected to cost.

    Both directions in one number rather than two bounds, so the two cannot be satisfied
    separately and exceeded together. `dimensions` is the model's width and not the column's:
    what arrives is what the model returns, and a model whose width the column cannot hold is
    refused by `RebuildPlan` for a different reason and at a different moment.
    """
    return (
        len(units) * vector_cost_bytes(dimensions)
        + sum(len(unit.text) for unit in units) * BYTES_PER_CHARACTER
    )


# ------------------------------------------------------------------ the units and the batch


@dataclass(frozen=True)
class EmbeddingUnit:
    """One chunk's text on its way to the inference server, and deliberately nothing else.

    There is no permission field and there must not be one. See
    `AN_EMBEDDING_WRITES_TWO_COLUMNS_AND_NEVER_A_PERMISSION`: a value that could carry a scope
    is a value that can carry the wrong one, and the row this work ends at already holds the
    permissions `chunk_document` copied onto it.
    """

    chunk_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.chunk_id.strip():
            msg = "an embedding unit has no chunk id, so its vector could not be written anywhere"
            raise EmbeddingError(msg)
        if len(self.chunk_id) > CHUNK_ID_CHARS:
            msg = (
                f"chunk id {self.chunk_id!r} is {len(self.chunk_id)} characters and the column "
                f"holds {CHUNK_ID_CHARS}; a truncated id names another chunk or none"
            )
            raise EmbeddingError(msg)
        if not self.text:
            # An empty text embeds to whatever the model does with nothing, which is a vector
            # in the corpus that matches every question a little. `chunk_document` cannot
            # produce one, so this is about the other source: rows read back for a rebuild.
            msg = f"chunk {self.chunk_id!r} has no text; an empty embedding matches everything"
            raise EmbeddingError(msg)


def units_for(chunks: Sequence[Chunk]) -> tuple[EmbeddingUnit, ...]:
    """The text of these chunks, with their permissions deliberately left behind.

    Takes `Chunk` rather than strings so the caller cannot assemble embedding work out of text
    that never came through `chunk_document`, which is the only thing in this system that
    copies a document's permissions onto a passage. What travels from here is the id and the
    text, because the row the vector lands on was written with the permissions already.
    """
    return tuple(EmbeddingUnit(chunk_id=chunk.chunk_id, text=chunk.text) for chunk in chunks)


@dataclass(frozen=True)
class EmbeddingBatch:
    """One request's worth of chunks, checked against both of its bounds at construction.

    Two bounds, two different reasons, and neither is throughput. See
    `A_BATCH_IS_A_BUDGET_AND_IS_CHECKED_BEFORE_THE_REQUEST`.

    Both are fields with defaults rather than constants read inside the check, so a test can
    construct a batch against a small budget and see the refusal fire. They are recorded on the
    batch rather than consulted and discarded, because what bounded a request is the first thing
    anybody asks when one of them was too large.
    """

    model: EmbeddingModel
    units: tuple[EmbeddingUnit, ...]
    #: What this batch was checked against. `default_factory` rather than a module constant read
    #: inside `__post_init__`, so the value is on the object that was checked.
    budget_bytes: int = field(default_factory=batch_budget_bytes)
    #: How many chunks one request may carry, whatever they cost. `DEFAULT_BATCH_SIZE` rather
    #: than a second number: `brain.knowledge.embedding` already argues that figure as the unit
    #: of resumption, which is the same quantity as the failure radius seen from the other end.
    max_chunks: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        if not self.units:
            msg = "an empty batch is a round trip that returns nothing"
            raise EmbeddingError(msg)
        seen: set[str] = set()
        for unit in self.units:
            if unit.chunk_id in seen:
                # Two units with one id make the response ambiguous: one vector arrives for
                # that id and there is no saying which of the two texts produced it, so one of
                # them is silently embedded as the other.
                msg = (
                    f"chunk {unit.chunk_id!r} appears twice in one batch; the response is keyed "
                    "by id, so one of the two texts would be embedded as the other"
                )
                raise EmbeddingError(msg)
            seen.add(unit.chunk_id)
        if len(self.units) > self.max_chunks:
            msg = (
                f"a batch of {len(self.units)} chunks is over the {self.max_chunks} a request "
                "may carry; nothing is written until the response arrives, so an interrupted "
                "batch loses all of itself"
            )
            raise EmbeddingError(msg)
        cost = batch_cost_bytes(self.units, dimensions=self.model.dimensions)
        if cost > self.budget_bytes:
            msg = (
                f"a batch of {len(self.units)} chunks costs {cost} bytes against a budget of "
                f"{self.budget_bytes}; the response is held as parsed floats and as the text "
                "they were parsed from at the same time, and the slot is what has to hold both"
            )
            raise EmbeddingError(msg)

    @property
    def chunk_ids(self) -> frozenset[str]:
        """What this batch expects back, as a set. Unique by construction."""
        return frozenset(unit.chunk_id for unit in self.units)

    @property
    def cost_bytes(self) -> int:
        return batch_cost_bytes(self.units, dimensions=self.model.dimensions)


def plan_batches(
    units: Sequence[EmbeddingUnit],
    *,
    model: EmbeddingModel,
    max_chunks: int = DEFAULT_BATCH_SIZE,
    budget_bytes: int | None = None,
) -> tuple[EmbeddingBatch, ...]:
    """Cut this work into batches that each fit, in the order they were given.

    Greedy and in order rather than packed. Packing by size would fill each request better and
    would reorder the work, and the order is what makes an interruption resumable: the caller's
    position is the last chunk written, so a batch that jumped ahead would leave holes behind
    a cursor that says it has passed them.

    A single chunk that cannot fit a batch on its own is refused by name rather than split.
    Splitting would embed text that no chunk id names, so the vector would sit against a passage
    that is not the one it was made from, and the citation would point at the whole chunk while
    the match came from a third of it. It cannot happen to prose, which `ChunkBounds` bounds; it
    can happen to a table, which `chunk_blocks` emits whole however long it is.
    """
    budget = batch_budget_bytes() if budget_bytes is None else budget_bytes
    batches: list[EmbeddingBatch] = []
    current: list[EmbeddingUnit] = []
    for unit in units:
        alone = batch_cost_bytes([unit], dimensions=model.dimensions)
        if alone > budget:
            msg = (
                f"chunk {unit.chunk_id!r} is {len(unit.text)} characters and costs {alone} "
                f"bytes on its own, over the {budget} byte budget for one batch; it is refused "
                "rather than split, because half a chunk embedded under a whole chunk's id is a "
                "vector for a passage nothing cites"
            )
            raise EmbeddingError(msg)
        candidate = [*current, unit]
        over_budget = batch_cost_bytes(candidate, dimensions=model.dimensions) > budget
        if current and (over_budget or len(candidate) > max_chunks):
            batches.append(
                EmbeddingBatch(
                    model=model,
                    units=tuple(current),
                    budget_bytes=budget,
                    max_chunks=max_chunks,
                )
            )
            current = [unit]
            continue
        current = candidate
    if current:
        batches.append(
            EmbeddingBatch(
                model=model, units=tuple(current), budget_bytes=budget, max_chunks=max_chunks
            )
        )
    return tuple(batches)


# ------------------------------------------------------------------ the seam


@dataclass(frozen=True)
class Embedded:
    """One vector the service produced, and the chunk id it says it is for.

    The id is on the value rather than implied by its position in a list, which is the whole of
    `A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION`. `EmbeddedVector` carries the
    model, so a response cannot arrive without one and cannot arrive at a width the model does
    not claim.
    """

    chunk_id: str
    vector: EmbeddedVector


class EmbeddingService(Protocol):
    """Whatever turns text into vectors. The one thing this module does not do.

    A protocol for the reason `brain.knowledge.rows.RowSource` and
    `brain.tools.run_skill.ScriptRunner` are protocols, and for one more that is specific to
    this leaf: the model is not in this image at all, by the decision in
    `NOTHING_IMPLEMENTS_THE_EMBEDDING_SERVICE`, so there is nothing here that could be called
    directly even if the design wanted it.

    Narrow on purpose, and narrow in a particular direction: it is handed a batch that has
    already been checked against a memory budget rather than a list of strings it could be
    given any number of. A service that took the strings would put the bound on the far side of
    a network call, where this process cannot enforce it and cannot see it fail.
    """

    def embed(self, batch: EmbeddingBatch) -> Sequence[Embedded]: ...


# ------------------------------------------------------------------ what an embedding writes

#: The two columns an embedding fills, taken from the table rather than typed out. Spelling one
#: of them differently writes nothing and reads back as a chunk that was never embedded, which
#: fails closed into "the vector leg returned less than it should" and is exactly the silent
#: degradation `brain.knowledge.embedding` is written against.
WRITTEN_COLUMNS: Final[tuple[str, ...]] = (EMBEDDING_FIELD, EMBEDDING_MODEL_FIELD)

#: The columns that say who may read a chunk. Named as a set so `written_column_gaps` can assert
#: the two sets are disjoint, rather than a reviewer being asked to notice it. `state` is here
#: as well as the three permission columns: a superseded chunk that an embedding write flipped
#: back to published would answer questions beside the version that replaced it, which is the
#: refusal `chunk_document` makes at the other end of the same pipeline.
#:
#: `SCOPE_COLUMNS` is unioned in rather than trusted to be a subset. It is the vocabulary a
#: knowledge scope may test, it is one column today, and the day it becomes two the second one
#: is protected here without anybody remembering to add it.
PERMISSION_COLUMNS: Final[frozenset[str]] = (
    frozenset({OWNER_FIELD, DEPARTMENT_FIELD, "visibility", "state"}) | SCOPE_COLUMNS
)


@dataclass(frozen=True)
class EmbeddingWrite:
    """What one embedded chunk changes in the corpus: two columns of a row that already exists.

    Holds an `EmbeddedVector` rather than a bare sequence, so the model identity and the numbers
    are filled from one object and cannot be written apart. A row with a vector and no model
    beside it is the state `corpus_identity` refuses to read, and the way to produce one is to
    have two values where there should be one.
    """

    chunk_id: str
    vector: EmbeddedVector

    @property
    def columns(self) -> Mapping[str, object]:
        """The update, read-only, so a caller cannot add a third column to it.

        `MappingProxyType` rather than a plain dict for the reason
        `brain.knowledge.uploads.IngestionAdmission.log_record` uses one: a mapping handed out
        of a value object is a mapping somebody mutates, and the mutation this one is protected
        against is the one that adds a permission column.
        """
        return MappingProxyType(
            {
                EMBEDDING_FIELD: self.vector.values,
                EMBEDDING_MODEL_FIELD: self.vector.model.identity,
            }
        )


def writes_for(batch: EmbeddingBatch, embedded: Sequence[Embedded]) -> tuple[EmbeddingWrite, ...]:
    """Turn a service's answer into updates, or refuse the four ways it does not fit the batch.

    The model first, because it is the one disagreement that produces a corpus rather than an
    error: vectors from a model nobody asked for, written under an identity that says they came
    from one that was. Every distance computed against them afterwards is a number.

    Then the ids, as three separate refusals rather than one length check. A length check passes
    for a response that is the right size and the wrong order, which is the case
    `A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION` is about, and which no test of
    counts can see.

    Ordered by the batch rather than by the response, so two runs over the same batch produce
    the same updates in the same order and a caller writing them in a transaction takes its row
    locks in a stable order.
    """
    for one in embedded:
        if one.vector.model.identity != batch.model.identity:
            msg = (
                f"the batch asked {batch.model.identity} and chunk {one.chunk_id!r} came back "
                f"from {one.vector.model.identity}; storing it would record a model that did "
                "not produce it, and every distance against it afterwards is meaningless"
            )
            raise MixedEmbeddingError(msg)

    by_id: dict[str, Embedded] = {}
    for one in embedded:
        if one.chunk_id in by_id:
            msg = (
                f"chunk {one.chunk_id!r} came back twice; the two vectors cannot both be the "
                "one that chunk's text produced, and nothing here can say which is which"
            )
            raise EmbeddingError(msg)
        by_id[one.chunk_id] = one

    unexpected = sorted(set(by_id) - batch.chunk_ids)
    if unexpected:
        msg = (
            f"the response names chunk(s) {unexpected} that were not in the batch; a vector "
            "written against a chunk whose text was never sent is a passage indexed as "
            "something else"
        )
        raise EmbeddingError(msg)

    missing = sorted(batch.chunk_ids - set(by_id))
    if missing:
        msg = (
            f"the response is missing chunk(s) {missing}; the batch is refused whole rather "
            "than written in part, because a partial write moves the position past rows nobody "
            "embedded and they are then left on the old model with nothing recording it"
        )
        raise EmbeddingError(msg)

    return tuple(
        EmbeddingWrite(chunk_id=unit.chunk_id, vector=by_id[unit.chunk_id].vector)
        for unit in batch.units
    )


def embed_batch(batch: EmbeddingBatch, service: EmbeddingService) -> tuple[EmbeddingWrite, ...]:
    """Send one batch and return the updates it produced. Nothing calls this yet.

    The budget was enforced when the batch was built, which is the point of enforcing it there:
    by the time a request is being made there is nothing useful left to check, and a bound
    applied here would be applied to a request that is already going out.

    Writing the updates is the caller's, and deliberately. This module has no session, for the
    reason the layout table gives about `brain.ops.limits` and `brain.ops.limit_store`, and the
    cases worth testing here are the refusals in `writes_for`, none of which is reachable
    through a module that opens a socket.
    """
    return writes_for(batch, service.embed(batch))


# ------------------------------------------------------------------ the jobs

#: The class embedding work is queued on. `TrafficClass.SYSTEM` for the reason
#: `brain.knowledge.uploads.INGESTION_CANNOT_BE_PROMOTED_OUT_OF_BATCH` gives about ingestion,
#: which this is the continuation of: SYSTEM is what makes the workload class BATCH, and BATCH
#: is what caps it at half of any budget, so a re-embed of the whole corpus cannot take the
#: slot a person's question needs.
EMBED_TRAFFIC_CLASS: Final = TrafficClass.SYSTEM

#: The queue name, derived rather than typed. `queue_name_for` refuses per-task queues on
#: purpose, so this is the same queue every other piece of housekeeping drains.
EMBED_QUEUE: Final = queue_name_for(EMBED_TRAFFIC_CLASS)

#: Embedding chunks of a document that has just been ingested.
EMBED_TASK: Final = "knowledge.embed"

#: Re-embedding a window of the corpus onto a different model.
REEMBED_TASK: Final = "knowledge.reembed"

#: How the model identity is spelled in a job's arguments. One name, because the enqueuer and
#: the worker are two programs and a key spelled differently is a job that runs against
#: whatever the default would have been.
MODEL_ARGUMENT: Final = "model"


def embed_job(
    *, document_id: str, first_ordinal: int, last_ordinal: int, model: EmbeddingModel
) -> Job:
    """Queue the embedding of one document's chunks, by ordinal window. Nothing calls this yet.

    **Identifiers and never records**, which `brain.ops.queue.Job` enforces and this shape is
    built for. The obvious argument list is the chunk ids, and two hundred of them is a copy of
    part of the corpus in a table with no row-level security and its own retention. An ordinal
    window names exactly the same chunks in four values, because `chunk_document` builds ids as
    the document id and the ordinal, and the worker fetches them through the same gate as
    everything else at the time it runs.

    `Redrive.SAFE`, which is the only one in this repository: see the module docstring for what
    makes that true and the two changes that would end it.
    """
    if first_ordinal < 0:
        msg = f"{first_ordinal} is not an ordinal"
        raise EmbeddingError(msg)
    if last_ordinal < first_ordinal:
        msg = (
            f"the window {first_ordinal}:{last_ordinal} ends before it starts, so it names no "
            "chunks and the job would report success having embedded nothing"
        )
        raise EmbeddingError(msg)
    return Job(
        task=EMBED_TASK,
        traffic_class=EMBED_TRAFFIC_CLASS,
        args={
            "document_id": document_id,
            "first_ordinal": first_ordinal,
            "last_ordinal": last_ordinal,
            MODEL_ARGUMENT: model.identity,
        },
        redrive=Redrive.SAFE,
    )


def rebuild_job(*, plan: RebuildPlan, cursor: RebuildCursor) -> Job | None:
    """The job for the next window of a rebuild, or None when there is no next window.

    None rather than a job with a zero size, because the two states are different and the caller
    has to branch on them anyway: one is "enqueue this", the other is "the rebuild is over and
    the vector leg is sound again". A zero-sized job would be enqueued, fetched and completed,
    and the log would show a rebuild that never ends.

    Built from `next_batch` rather than from the cursor directly, so the refusal that matters on
    resumption is made in one place: a cursor rebuilding towards one model under a plan naming
    another leaves the corpus holding both, plus whatever it started on.

    Nothing calls this yet. There is no queue driver, so no job has ever been enqueued.
    """
    batch = next_batch(plan=plan, cursor=cursor)
    if batch.is_finished:
        return None
    return Job(
        task=REEMBED_TASK,
        traffic_class=EMBED_TRAFFIC_CLASS,
        args={
            "after_chunk_id": batch.after_chunk_id,
            "size": batch.size,
            MODEL_ARGUMENT: plan.to_model.identity,
        },
        redrive=Redrive.SAFE,
    )


# ------------------------------------------------------------------ the deployment checks


def written_column_gaps(columns: Sequence[str] = WRITTEN_COLUMNS) -> tuple[str, ...]:
    """Every reason these columns are the wrong ones for an embedding to write.

    Two checks, and the second is the one that would be catastrophic. A column that is not on
    `know.chunk` writes nothing, which presents as a corpus that never gets embedded. A
    permission column in the list is a rebuild that rewrites who may read a passage, from a
    process that has no document in front of it and would therefore write a default.

    Asked of a parameter rather than of the constant beside it, so the refusal can be shown to
    fire; the constant is what `embed_batch_gaps` passes.
    """
    findings: list[str] = []
    unknown = sorted(set(columns) - set(CHUNK.c.keys()))
    if unknown:
        findings.append(
            f"{unknown} is not a column of know.chunk, so an embedding written to it updates "
            "nothing and every row reads back as one that was never embedded"
        )
    permissions = sorted(set(columns) & PERMISSION_COLUMNS)
    if permissions:
        findings.append(
            f"an embedding would write {permissions}, which is what says who may read the "
            "chunk; the process that embeds has no document in front of it, so whatever it "
            "wrote there would be a default, and the default scope is the unrestricted one"
        )
    return tuple(findings)


def embed_batch_gaps(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    slot_mib: int = MIB_PER_SLOT,
    reserve_mib: int = EMBED_SLOT_RESERVE_MIB,
    chunk_id_chars: int = CHUNK_ID_CHARS,
    identity_chars: int = MODEL_IDENTITY_CHARS,
    dimensions: int = EMBEDDING_DIMENSIONS,
) -> tuple[str, ...]:
    """Every reason this deployment cannot run an embedding batch, in words that name the fix.

    Called by `brain.ops.worker.preflight`, and called there unconditionally rather than on the
    parse worker's condition, because a slot is a slot: the queue name is derived from the
    traffic class, ingestion and rebuilds are both `SYSTEM`, and any container draining that
    queue may be handed an embedding batch.

    Four checks, and each catches a different way the arithmetic stops being arithmetic.

    The reserve first, because it is what makes the budget a second cap rather than a restatement
    of the slot. A budget at or above the slot is a batch told it may use everything the slot
    already accounts for, and the enforcement left is the kill.

    Then the batch against the budget, computed for chunks at the size `ChunkBounds` bounds prose
    to. It is the check that fails when somebody raises `DEFAULT_BATCH_SIZE` or widens the
    vector. What it does not cover is a table chunk, which `chunk_blocks` emits whole however
    long it is; that one is refused per batch by `plan_batches` because its size is not knowable
    from a deployment.

    Then the columns, which is `written_column_gaps` asked about the declared pair.

    Then the two lengths, which are the least obvious and the most annoying to discover late. A
    job argument over `MAX_ARGUMENT_CHARS` is refused by `Job`, so a chunk id or a model identity
    that outgrew it means every embedding job fails at the moment it is enqueued, having already
    been planned.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight`: a
    deployment wrong in two ways is one where fixing either leaves it still wrong.
    """
    findings: list[str] = []
    budget = batch_budget_bytes(slot_mib=slot_mib, reserve_mib=reserve_mib)
    if budget >= slot_mib * MIB:
        findings.append(
            f"a reserve of {reserve_mib} MiB leaves a batch budget of {budget // MIB} MiB "
            f"against a {slot_mib} MiB slot, so the batch is told it may use everything the "
            "slot accounts for; the slot also holds a database connection and the "
            "interpreter's share, and the queue enforces it by running that many jobs at once"
        )

    if batch_size < 1:
        findings.append(
            f"a batch size of {batch_size} is a request that carries nothing; a rebuild made of "
            "empty batches advances its cursor and embeds no rows"
        )
    else:
        full = ChunkBounds().size
        units = tuple(
            EmbeddingUnit(chunk_id=f"c{index:d}", text="x" * full) for index in range(batch_size)
        )
        cost = batch_cost_bytes(units, dimensions=dimensions)
        if cost > budget:
            findings.append(
                f"{batch_size} chunks of {full} characters at {dimensions} dimensions cost "
                f"{cost // MIB} MiB, over the {budget // MIB} MiB budget for one batch; a "
                "request is held as parsed floats and as the text they were parsed from at once"
            )

    findings.extend(written_column_gaps())

    if chunk_id_chars > MAX_ARGUMENT_CHARS:
        findings.append(
            f"a chunk id may be {chunk_id_chars} characters and a job argument may be "
            f"{MAX_ARGUMENT_CHARS}; every embedding job would be refused at enqueue, after the "
            "batch had been planned"
        )
    if identity_chars > MAX_ARGUMENT_CHARS:
        findings.append(
            f"a model identity may be {identity_chars} characters and a job argument may be "
            f"{MAX_ARGUMENT_CHARS}; the job that carries which model to embed with could not be "
            "enqueued at all"
        )
    return tuple(findings)
