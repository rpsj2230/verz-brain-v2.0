"""What bounds one embedding batch, what a vector is matched to, and what a write may touch.

Three of these assert a relation rather than a value, on purpose. `EMBED_SLOT_RESERVE_MIB`,
`JSON_BYTES_PER_FLOAT` and `BYTES_PER_CHARACTER` are judgements, and a test that compared one of
them against itself would be green for every value it could hold, which is the failure this
repository's own notes record catching three authors in one afternoon. So the reserve is
asserted through the property it exists for, which is that the batch budget sits strictly below
the slot the batch runs in; the JSON figure through the property that a returned vector costs
more than its parsed floats; and the character figure through the property that two batches of
the same length and different text cost differently.

The reordering test is the one to keep if only one survives. A response that is the right length
and the wrong order passes every count and every set check, and pairs each chunk with another
chunk's vector, and nothing downstream can see it: every row holds a well-formed vector from the
right model. So the vectors carry distinguishable values and each write is checked against the
vector its own chunk produced, not merely against the set of ids.

Task ids: M7.3.4
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import Any

import pytest

from brain.gate.context import TrafficClass
from brain.knowledge.chunking import Block, BlockKind, ChunkBounds, chunk_document
from brain.knowledge.embed_queue import (
    BYTES_PER_CHARACTER,
    EMBED_QUEUE,
    EMBED_TASK,
    EMBED_TRAFFIC_CLASS,
    FLOAT_BYTES_IN_A_SEQUENCE,
    MIB,
    PERMISSION_COLUMNS,
    REEMBED_TASK,
    WRITTEN_COLUMNS,
    Embedded,
    EmbeddingBatch,
    EmbeddingUnit,
    EmbeddingWrite,
    batch_budget_bytes,
    batch_cost_bytes,
    embed_batch,
    embed_batch_gaps,
    embed_job,
    plan_batches,
    rebuild_job,
    units_for,
    vector_cost_bytes,
    writes_for,
    written_column_gaps,
)
from brain.knowledge.embedding import (
    DEFAULT_BATCH_SIZE,
    MODEL_IDENTITY_CHARS,
    EmbeddedVector,
    EmbeddingError,
    EmbeddingModel,
    MixedEmbeddingError,
    RebuildCursor,
    RebuildPlan,
)
from brain.knowledge.item import KnowledgeItem
from brain.knowledge.search import CHUNK, CHUNK_ID_CHARS, EMBEDDING_DIMENSIONS
from brain.knowledge.visibility import KnowledgeVisibility
from brain.ops import worker as worker_module
from brain.ops.queue import (
    CONCURRENCY,
    MAX_ARGUMENT_CHARS,
    MIB_PER_SLOT,
    Job,
    QueueError,
    Redrive,
    queue_name_for,
)
from brain.ops.worker import preflight

MODEL = EmbeddingModel(name="qwen3-embedding", revision="a1b2c3d", dimensions=EMBEDDING_DIMENSIONS)
OTHER = EmbeddingModel(name="qwen3-embedding", revision="e4f5g6h", dimensions=EMBEDDING_DIMENSIONS)

#: The permission columns spelled out rather than imported from the module under test. The
#: module's own set is what the guard reads, so comparing against it would compare the guard
#: with itself; these four are what `know.chunk` calls the fact of who may read a passage.
READERSHIP_COLUMNS = frozenset({"owner_id", "department", "visibility", "state"})


def _vector(model: EmbeddingModel = MODEL, *, mark: float = 0.0) -> EmbeddedVector:
    """A vector whose first value identifies which chunk it was made for.

    The mark is what makes a positional mix-up visible. Without it every vector in a batch is
    identical, and a response consumed by position rather than by id produces exactly the same
    writes as one consumed correctly.
    """
    return EmbeddedVector(model=model, values=(mark, *([0.0] * (model.dimensions - 1))))


def _units(count: int, *, chars: int = 10) -> tuple[EmbeddingUnit, ...]:
    return tuple(
        EmbeddingUnit(chunk_id=f"k_doc.{index:04d}", text="x" * chars) for index in range(count)
    )


def _batch(units: tuple[EmbeddingUnit, ...], **kwargs: Any) -> EmbeddingBatch:
    return EmbeddingBatch(model=MODEL, units=units, **kwargs)


class _Service:
    """A stand-in for the inference server, and the only implementation there has ever been.

    It answers in the order it was asked unless told otherwise, which is what the reordering
    and short-response tests vary.
    """

    def __init__(self, *, reverse: bool = False, drop: int = 0, model: EmbeddingModel = MODEL):
        self.reverse = reverse
        self.drop = drop
        self.model = model
        self.batches: list[EmbeddingBatch] = []

    def embed(self, batch: EmbeddingBatch) -> list[Embedded]:
        self.batches.append(batch)
        answered = [
            Embedded(chunk_id=unit.chunk_id, vector=_vector(self.model, mark=float(index)))
            for index, unit in enumerate(batch.units)
        ]
        if self.drop:
            answered = answered[: -self.drop]
        return list(reversed(answered)) if self.reverse else answered


# ------------------------------------------------------------------ what a batch costs
def test_a_batch_budget_sits_strictly_below_the_slot_it_runs_in() -> None:
    """The reserve is what makes the batch budget a second cap rather than a restatement of the
    slot. A slot holds a database connection and the interpreter's share as well as the batch,
    so a budget equal to the slot is a batch told it may use memory that is already spoken for,
    and the enforcement left is the kernel's.

    Delete this and the reserve can be set to zero, which produces batches that fit the
    arithmetic and a worker whose slots add up to more than its container."""
    assert batch_budget_bytes() < MIB_PER_SLOT * MIB


def test_a_returned_vector_costs_more_than_the_floats_it_becomes() -> None:
    """The response text and the parsed objects exist at the same time: the decoder holds the
    characters while it builds the floats. Counting only the parsed side halves the figure, and
    the half that goes missing is the one being held at the moment memory peaks.

    Delete this and the JSON term can be dropped from the cost, which roughly doubles how many
    chunks a batch is allowed and moves the peak past the slot."""
    parsed_floats_alone = EMBEDDING_DIMENSIONS * FLOAT_BYTES_IN_A_SEQUENCE

    assert vector_cost_bytes(EMBEDDING_DIMENSIONS) > parsed_floats_alone


def test_the_cost_of_a_batch_counts_the_text_it_sends_and_not_only_the_vectors() -> None:
    """A batch of long chunks and a batch of short ones cost different amounts, because the
    request body is in the process too. A cost that counted only the response would let a batch
    of table chunks, which `chunk_blocks` emits whole however long they are, pass a budget it
    cannot fit in.

    Delete this and the text term can be removed, and the only chunks that would then be
    bounded are the ones that were never the problem."""
    short = batch_cost_bytes(_units(4, chars=10), dimensions=EMBEDDING_DIMENSIONS)
    long = batch_cost_bytes(_units(4, chars=100_000), dimensions=EMBEDDING_DIMENSIONS)

    assert long > short
    assert long - short == 4 * (100_000 - 10) * BYTES_PER_CHARACTER


def test_the_declared_batch_size_fits_the_budget_at_the_size_prose_is_cut_to() -> None:
    """The relation between two modules' constants, which is the whole reason this figure can
    be trusted. `DEFAULT_BATCH_SIZE` is argued in `brain.knowledge.embedding` as the unit of
    resumption and knows nothing about memory; the budget is derived from the queue's slot and
    knows nothing about resumption. They are edited in different files for different reasons.

    Delete this and either can move until a batch no longer fits a slot, which presents as a
    worker killed by the kernel during ingestion with nothing in its log."""
    full = ChunkBounds().size
    cost = batch_cost_bytes(_units(DEFAULT_BATCH_SIZE, chars=full), dimensions=EMBEDDING_DIMENSIONS)

    assert cost <= batch_budget_bytes()


# ------------------------------------------------------------------ the batch's own bounds
def test_a_batch_over_its_memory_budget_is_refused_when_it_is_built() -> None:
    """Before the work, not during it. A bound enforced by watching memory climb has already
    been passed when it fires, and if the climb was fast enough the kernel has already decided.

    Delete this and the check can move to the moment the response is read, where there is
    nothing useful left to do about it."""
    with pytest.raises(EmbeddingError, match="costs"):
        _batch(_units(4, chars=1000), budget_bytes=1000)


def test_a_batch_over_its_chunk_count_is_refused_however_little_it_costs() -> None:
    """The second bound, and a different reason from the first. Nothing is written until the
    response arrives, so an interrupted batch loses all of itself; a thousand tiny chunks fit
    the memory budget comfortably and lose a thousand chunks of work to one timeout.

    Delete this and the count cap can be removed, leaving the failure radius bounded only by
    how small the chunks happen to be."""
    with pytest.raises(EmbeddingError, match="over the 3 a request may carry"):
        _batch(_units(4), max_chunks=3)


def test_a_chunk_appearing_twice_in_one_batch_is_refused() -> None:
    """The response is keyed by id, so a duplicate makes it ambiguous: one vector comes back
    for that id and nothing can say which of the two texts produced it. One of the two is then
    silently indexed as the other.

    Delete this and a caller assembling a batch from two overlapping windows produces a corpus
    where some passages are embedded as their neighbours."""
    unit = EmbeddingUnit(chunk_id="k_doc.0001", text="first")
    twin = EmbeddingUnit(chunk_id="k_doc.0001", text="second")

    with pytest.raises(EmbeddingError, match="appears twice"):
        _batch((unit, twin))


def test_an_empty_batch_is_refused() -> None:
    """A request that carries nothing is a round trip that returns nothing, and a rebuild made
    of them advances its cursor over rows nobody embedded.

    Delete this and an off-by-one in a caller's windowing produces a run that reports batches
    and chunks and leaves the corpus exactly as it was."""
    with pytest.raises(EmbeddingError, match="returns nothing"):
        _batch(())


def test_a_batch_within_both_bounds_is_built() -> None:
    """The positive sibling of the three refusals above. A guard tested only by what it refuses
    is satisfied by a constructor that refuses everything, and a batch nobody can build is a
    corpus nobody can embed."""
    batch = _batch(_units(4))

    assert batch.chunk_ids == {unit.chunk_id for unit in _units(4)}
    assert batch.cost_bytes <= batch.budget_bytes


# ------------------------------------------------------------------ planning
def test_planning_never_produces_a_batch_over_either_bound() -> None:
    """The property that makes `plan_batches` worth having rather than a loop at each call
    site. Every batch it returns is one `EmbeddingBatch` would accept, which is checked here
    over a budget small enough that the splitting actually happens.

    Delete this and the planner can emit a final batch that was never checked, which is where
    an off-by-one in the accumulation would land."""
    budget = batch_cost_bytes(_units(3), dimensions=EMBEDDING_DIMENSIONS)
    batches = plan_batches(_units(10), model=MODEL, budget_bytes=budget)

    assert len(batches) > 1
    for batch in batches:
        assert batch.cost_bytes <= budget
        assert len(batch.units) <= DEFAULT_BATCH_SIZE


def test_planning_covers_every_chunk_exactly_once_and_keeps_the_order_it_was_given() -> None:
    """Order is what makes an interruption resumable: the position is the last chunk written,
    so a planner that reordered would leave holes behind a cursor claiming to have passed them.
    The units here are deliberately not in id order, so a planner that sorted would be caught
    rather than looking equivalent.

    Delete this and packing by size becomes a tempting optimisation, and the cost of it is
    unfindable rows rather than a slow rebuild."""
    scrambled = (
        EmbeddingUnit(chunk_id="k_doc.0009", text="nine"),
        EmbeddingUnit(chunk_id="k_doc.0001", text="one"),
        EmbeddingUnit(chunk_id="k_doc.0005", text="five"),
        EmbeddingUnit(chunk_id="k_doc.0003", text="three"),
    )
    batches = plan_batches(scrambled, model=MODEL, max_chunks=2)
    flattened = [unit for batch in batches for unit in batch.units]

    assert flattened == list(scrambled)


def test_a_chunk_too_large_to_batch_alone_is_refused_by_name_rather_than_split() -> None:
    """Splitting would embed text that no chunk id names, so the vector would sit against a
    passage it was not made from and the citation would point at the whole chunk while the
    match came from part of it. It cannot happen to prose, which `ChunkBounds` bounds; it can
    happen to a table, which is emitted whole however long it is.

    Delete this and an oversized table is quietly halved, and the half nobody embedded is
    unfindable while the half that was embedded answers under the whole chunk's citation."""
    huge = EmbeddingUnit(chunk_id="k_price_list.0002", text="x" * 100_000)

    with pytest.raises(EmbeddingError, match=re.escape("k_price_list.0002")):
        plan_batches((huge,), model=MODEL, budget_bytes=1000)


def test_planning_no_chunks_produces_no_batches() -> None:
    """A document whose chunks are all embedded already produces no work, and no work has to
    mean no request rather than one empty one.

    Delete this and the planner can return a single empty batch, which `EmbeddingBatch` then
    refuses at construction, turning "nothing to do" into an error."""
    assert plan_batches((), model=MODEL) == ()


# ------------------------------------------------------------------ units and permissions
def test_a_unit_carries_an_id_and_text_and_has_nowhere_to_put_a_permission() -> None:
    """The structural half of the guarantee. A value that could carry a scope is a value that
    can carry the wrong one, and the row this work lands on already holds the permissions
    `chunk_document` copied onto it.

    Delete this and a scope field can be added for the convenience of some caller, and the
    first thing that fills it in will be a rebuild that has no document in front of it."""
    assert {field.name for field in fields(EmbeddingUnit)} == {"chunk_id", "text"}


def test_units_are_built_from_chunks_so_the_text_came_through_the_permission_copy() -> None:
    """`chunk_document` is the only thing in this system that copies a document's permissions
    onto a passage, and taking `Chunk` rather than strings is what stops embedding work being
    assembled out of text that never went through it.

    Delete this and `units_for` can be widened to take strings, which makes the whole chunking
    guarantee optional for anybody with a text and an id."""
    item = KnowledgeItem(
        item_id="k_sop",
        content="Deployments go out on a Tuesday.",
        title="SOP",
        visibility=KnowledgeVisibility.of_department("web"),
        owner_id="p_wei_ling",
    )
    chunks = chunk_document(
        item,
        [Block(kind=BlockKind.PROSE, text="Deployments go out on a Tuesday.", start=0)],
        bounds=ChunkBounds(),
    )
    units = units_for(chunks)

    assert [unit.chunk_id for unit in units] == [chunk.chunk_id for chunk in chunks]
    assert [unit.text for unit in units] == [chunk.text for chunk in chunks]


def test_a_unit_with_no_text_is_refused() -> None:
    """An empty text embeds to whatever the model does with nothing, which is a vector in the
    corpus that matches every question a little and cites a passage with nothing in it.

    Delete this and a row read back with a null body becomes a chunk that answers everything
    slightly."""
    with pytest.raises(EmbeddingError, match="matches everything"):
        EmbeddingUnit(chunk_id="k_doc.0001", text="")


def test_a_unit_whose_id_is_wider_than_the_column_is_refused() -> None:
    """A truncated id names another chunk or none, so the vector is written against a passage
    it was not made from, or against nothing at all while the job reports success.

    Delete this and the refusal moves to the database, which reports it after the batch has
    been embedded."""
    with pytest.raises(EmbeddingError, match="characters and the column holds"):
        EmbeddingUnit(chunk_id="k" * (CHUNK_ID_CHARS + 1), text="body")


# ------------------------------------------------------------------ what a write may touch
def test_an_embedding_writes_the_vector_and_its_model_and_no_other_column() -> None:
    """The catastrophic failure this leaf can produce is quiet: a rebuild re-embeds text,
    rewrites the row, and drops or defaults the permissions, leaving a corpus everybody can
    read with nothing reporting it. The structural answer is that there is no third column to
    write. The expected pair is read off `know.chunk` rather than from the module under test,
    so the assertion is not the guard compared with itself.

    Delete this and an embedding write can grow a scope, an owner or a state, filled in by a
    process that has no document in front of it and would therefore write a default."""
    write = EmbeddingWrite(chunk_id="k_doc.0001", vector=_vector())

    assert set(write.columns) == {CHUNK.c.embedding.name, CHUNK.c.embedding_model.name}
    assert not set(write.columns) & READERSHIP_COLUMNS
    assert write.columns[CHUNK.c.embedding_model.name] == MODEL.identity


def test_a_permission_column_cannot_be_added_to_a_write_after_it_is_built() -> None:
    """The mapping is handed out of a value object, and a mapping handed out is a mapping
    somebody mutates. The mutation this one is protected against is the one that adds a
    readership column on the way to the database.

    Delete this and a caller can build the update, add `visibility`, and hand it on; every test
    above still passes because the write it inspected was correct when it was made."""
    write = EmbeddingWrite(chunk_id="k_doc.0001", vector=_vector())

    with pytest.raises(TypeError):
        write.columns["visibility"] = "company"  # type: ignore[index]


def test_a_readership_column_in_the_written_pair_is_reported_as_a_gap() -> None:
    """Asked of a parameter rather than of the constant, so the refusal can be shown to fire.
    A check that can only ever be run against the value it lives beside cannot be shown to
    fail, and a check nobody has seen fail is a check nobody knows works.

    Delete this and `WRITTEN_COLUMNS` can be extended with a permission column, and the only
    thing that would notice is a person reading a diff."""
    findings = written_column_gaps((CHUNK.c.embedding.name, "visibility"))

    assert any("who may read" in finding for finding in findings), findings


def test_a_column_that_is_not_on_the_chunk_table_is_reported_as_a_gap() -> None:
    """A misspelled column updates nothing, so every row reads back as one that was never
    embedded, which fails closed into a vector leg that quietly returns less than it should.

    Delete this and a rename in the table can leave this module writing to a column that is no
    longer there, with no error anywhere."""
    findings = written_column_gaps(("embeddings",))

    assert any("not a column of know.chunk" in finding for finding in findings), findings


def test_the_declared_pair_is_two_real_columns_and_neither_says_who_may_read() -> None:
    """The positive sibling. A gap check tested only by what it reports is satisfied by one
    that reports everything, and a pair that can never be written is a corpus that can never
    be embedded."""
    assert written_column_gaps() == ()
    assert set(WRITTEN_COLUMNS) <= set(CHUNK.c.keys())
    assert not set(WRITTEN_COLUMNS) & PERMISSION_COLUMNS


# ------------------------------------------------------------------ matching by id
def test_a_reordered_response_is_matched_by_id_rather_than_by_position() -> None:
    """The failure nothing downstream can see. A response that is the right length and the
    wrong order pairs every chunk with another chunk's vector; each row then holds a
    well-formed vector from the right model, the index builds, and retrieval returns the wrong
    passages confidently until somebody re-embeds. The vectors here carry a mark so a
    positional pairing is visible rather than merely possible.

    Delete this and the response can be consumed with `zip`, which passes every count and every
    set check that remains."""
    batch = _batch(_units(4))
    forwards = embed_batch(batch, _Service())
    backwards = embed_batch(batch, _Service(reverse=True))

    assert [write.chunk_id for write in backwards] == [unit.chunk_id for unit in batch.units]
    assert [write.vector.values[0] for write in backwards] == [
        write.vector.values[0] for write in forwards
    ]


def test_a_response_missing_a_chunk_refuses_the_whole_batch() -> None:
    """Written in part is worse than refused whole. A partial write moves the caller's position
    past rows nobody embedded, and those rows are then left on the old model with nothing
    anywhere recording which they are.

    Delete this and a short response becomes a silent hole in the corpus rather than a failed
    batch that is simply run again."""
    with pytest.raises(EmbeddingError, match="missing chunk"):
        embed_batch(_batch(_units(4)), _Service(drop=1))


def test_a_response_naming_a_chunk_that_was_not_asked_for_is_refused() -> None:
    """A vector written against a chunk whose text was never sent is a passage indexed as
    something else, and it is indistinguishable afterwards from one that was embedded properly.

    Delete this and a service answering from a stale batch can overwrite rows in this one."""
    batch = _batch(_units(2))
    answered = [Embedded(chunk_id=unit.chunk_id, vector=_vector()) for unit in batch.units] + [
        Embedded(chunk_id="k_other.0001", vector=_vector())
    ]

    with pytest.raises(EmbeddingError, match="not in the batch"):
        writes_for(batch, answered)


def test_a_response_returning_one_chunk_twice_is_refused() -> None:
    """Two vectors cannot both be the one that chunk's text produced, and nothing here can say
    which is which; taking either is picking at random.

    Delete this and the later one silently wins, which is a coin toss written into the
    corpus."""
    batch = _batch(_units(2))
    first = batch.units[0]
    answered = [
        Embedded(chunk_id=first.chunk_id, vector=_vector(mark=1.0)),
        Embedded(chunk_id=first.chunk_id, vector=_vector(mark=2.0)),
    ]

    with pytest.raises(EmbeddingError, match="came back twice"):
        writes_for(batch, answered)


def test_a_response_from_another_model_is_refused_rather_than_stored() -> None:
    """The one disagreement that produces a corpus rather than an error: vectors from a model
    nobody asked for, written under the identity of one that was. Every distance computed
    against them afterwards is a number rather than a distance, and no query reports it.

    Delete this and a service that fell back to a different model mid-rebuild leaves a corpus
    that says it holds one model and holds two."""
    with pytest.raises(MixedEmbeddingError, match="came back from"):
        embed_batch(_batch(_units(2)), _Service(model=OTHER))


def test_a_complete_response_produces_one_write_for_each_chunk_in_batch_order() -> None:
    """The positive sibling of the four refusals above. A matcher tested only by what it
    rejects is satisfied by one that rejects everything, and a batch that can never produce
    writes is a corpus that can never be embedded.

    The order is asserted as well as the set, because a caller writing these in a transaction
    takes its row locks in this order, and two runs over one batch have to take them the
    same way."""
    batch = _batch(_units(3))
    writes = embed_batch(batch, _Service())

    assert [write.chunk_id for write in writes] == [unit.chunk_id for unit in batch.units]
    assert all(write.vector.model is MODEL for write in writes)


# ------------------------------------------------------------------ the jobs
def test_an_embedding_job_names_a_window_because_the_chunk_ids_are_records() -> None:
    """`brain.ops.queue.Job` refuses an argument long enough to be content rather than a
    reference, and a batch's worth of chunk ids is content: a queue table has no row-level
    security, its own retention, and a habit of being read in psql during an incident. An
    ordinal window names the same chunks in four values, because a chunk id is its document and
    its ordinal.

    Delete this and the ids can be packed into an argument, which is refused at enqueue for a
    small batch and accepted for a smaller one, so the shape works until it does not."""
    job = embed_job(document_id="k_sop", first_ordinal=0, last_ordinal=199, model=MODEL)

    assert job.task == EMBED_TASK
    assert job.args["document_id"] == "k_sop"
    assert job.args["last_ordinal"] == 199
    with pytest.raises(QueueError, match="content rather than a reference"):
        Job(
            task=EMBED_TASK,
            traffic_class=EMBED_TRAFFIC_CLASS,
            args={"chunk_ids": ",".join(unit.chunk_id for unit in _units(20))},
        )


def test_embedding_work_is_queued_where_it_cannot_take_an_interactive_slot() -> None:
    """`TrafficClass.SYSTEM` is what makes the workload class BATCH, and BATCH is what caps it
    at half of any budget, so a re-embed of the whole corpus cannot take the slot a person's
    question needs. The queue name is derived from the class rather than chosen, so a task
    author cannot pick a priority by picking a name.

    Delete this and embedding can be moved onto the async queue for throughput, where it
    competes with replies somebody is waiting for."""
    job = embed_job(document_id="k_sop", first_ordinal=0, last_ordinal=1, model=MODEL)

    assert job.traffic_class is TrafficClass.SYSTEM
    assert queue_name_for(job.traffic_class) == EMBED_QUEUE
    assert CONCURRENCY[TrafficClass.SYSTEM] > 0


def test_an_embedding_job_may_be_re_driven_because_its_write_is_an_update() -> None:
    """The only `Redrive.SAFE` in this repository, so it is worth a test of its own. Running it
    twice sets the same two columns of the same row to the same values, because the vector is a
    function of the text and the weights. An insert rather than an update would end that, and so
    would a metered provider, for which a second run is a second invoice.

    Delete this and the default takes over, which is `UNSAFE`, and every worker that dies
    mid-batch sends a re-embed to quarantine for a person to decide about by hand."""
    job = embed_job(document_id="k_sop", first_ordinal=0, last_ordinal=1, model=MODEL)

    assert job.redrive is Redrive.SAFE


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    """It names no chunks, so the job would be fetched, run and completed having embedded
    nothing, and the log would show a successful batch.

    Delete this and an off-by-one in a caller's windowing is invisible: the counters move and
    the corpus does not."""
    with pytest.raises(EmbeddingError, match="ends before it starts"):
        embed_job(document_id="k_sop", first_ordinal=5, last_ordinal=4, model=MODEL)


def test_a_rebuild_job_carries_the_position_the_cursor_is_at() -> None:
    """The queue side of a resumable rebuild. The window is the cursor's position and the
    plan's batch size, so a job enqueued after an interruption covers the rows the last one did
    not rather than starting again.

    Delete this and the job can be built from the plan alone, which re-embeds the corpus from
    the beginning every time a worker restarts."""
    plan = RebuildPlan(to_model=OTHER, from_identity=MODEL.identity)
    cursor = RebuildCursor(model=OTHER, after_chunk_id="k_sop.0199", chunks=200, batches=1)
    job = rebuild_job(plan=plan, cursor=cursor)

    assert job is not None
    assert job.task == REEMBED_TASK
    assert job.args["after_chunk_id"] == "k_sop.0199"
    assert job.args["size"] == plan.batch_size


def test_a_rebuild_that_has_reached_the_end_enqueues_nothing() -> None:
    """None rather than a job with a zero-sized window. A zero-sized job is enqueued, fetched
    and completed, and the log shows a rebuild that never ends.

    Delete this and the finished state becomes a job, and the queue holds one for every worker
    poll for as long as the cursor is left where it is."""
    plan = RebuildPlan(to_model=OTHER, from_identity=MODEL.identity)
    finished = RebuildCursor(model=OTHER, after_chunk_id="k_sop.9999", chunks=9, exhausted=True)

    assert rebuild_job(plan=plan, cursor=finished) is None


def test_a_rebuild_job_refuses_a_cursor_that_is_heading_somewhere_else() -> None:
    """The refusal that matters on resumption, made in one place by going through `next_batch`
    rather than reading the cursor here. A rebuild interrupted while rewriting to one model and
    resumed against another leaves the corpus holding both, plus whatever it started on.

    Delete this and the job can be built straight from the cursor, and the check that exists in
    `brain.knowledge.embedding` is bypassed by the caller that most needs it."""
    plan = RebuildPlan(to_model=OTHER, from_identity=MODEL.identity)
    elsewhere = RebuildCursor(model=MODEL)

    with pytest.raises(EmbeddingError, match="carrying on would leave the corpus holding both"):
        rebuild_job(plan=plan, cursor=elsewhere)


def test_a_chunk_id_and_a_model_identity_both_fit_a_job_argument() -> None:
    """Three modules' bounds have to agree for an embedding job to be enqueueable at all: the
    chunk id column, the model identity grammar, and what `Job` will accept as a reference.
    Each is edited in a different file for a different reason.

    Delete this and widening either grammar makes every embedding job fail at the moment it is
    enqueued, after the batch has been planned and the work laid out."""
    assert CHUNK_ID_CHARS <= MAX_ARGUMENT_CHARS
    assert MODEL_IDENTITY_CHARS <= MAX_ARGUMENT_CHARS


# ------------------------------------------------------------------ the deployment check
def test_a_reserve_that_leaves_no_second_cap_is_reported() -> None:
    """A budget at or above the slot is a batch told it may use everything the slot already
    accounts for, which is the mistake `brain.knowledge.parse_budget` names one level up: set
    the process ceiling to the container limit so nothing is wasted, and be killed while
    believing you are within budget.

    Delete this and the reserve can be set to zero, and every batch arithmetic test above still
    passes because they are all computed from the same budget."""
    findings = embed_batch_gaps(reserve_mib=0)

    assert any("everything the slot accounts for" in finding for finding in findings), findings


def test_a_batch_size_that_cannot_fit_a_slot_is_reported() -> None:
    """The check that fails when somebody raises the batch size or widens the vector. Both are
    reasonable-looking edits in files that say nothing about memory, and the container that
    discovers the answer is a worker killed by the kernel mid-ingestion.

    Delete this and the two constants can drift until a batch is larger than the slot it runs
    in, which nothing else in this repository compares."""
    findings = embed_batch_gaps(batch_size=DEFAULT_BATCH_SIZE * 20)

    assert any("budget for one batch" in finding for finding in findings), findings


def test_a_chunk_id_wider_than_a_job_argument_is_reported() -> None:
    """Discovered late otherwise: the batch is planned, the work is laid out, and the job is
    refused at the moment it is handed to the queue.

    Delete this and the relation is only asserted where both constants happen to be imported,
    rather than by the process that would enqueue the job."""
    findings = embed_batch_gaps(chunk_id_chars=MAX_ARGUMENT_CHARS + 1)

    assert any("job argument may be" in finding for finding in findings), findings


def test_a_batch_size_of_nothing_is_reported() -> None:
    """A rebuild made of empty batches advances its cursor and embeds no rows, and reports
    success at the end of it.

    Delete this and a zero reaches `plan_batches`, which produces one batch per chunk rather
    than refusing, so the failure is a rebuild that is a thousand times slower."""
    findings = embed_batch_gaps(batch_size=0)

    assert any("carries nothing" in finding for finding in findings), findings


def test_the_declared_deployment_can_run_an_embedding_batch() -> None:
    """The positive sibling, and the strongest thing this section asserts: the constants as
    they stand describe a batch that fits the slot it would run in, written to columns that
    exist and say nothing about who may read a passage."""
    assert embed_batch_gaps() == ()


def test_the_worker_preflight_asks_whether_a_batch_would_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`embed_batch_gaps` is a mechanism and this is its call site. The most common defect in
    this repository is a correct, tested, documented check that nothing invokes, and it happens
    exactly this way: the check is written beside the module it belongs to and the wiring is
    left for later.

    The finding is substituted rather than provoked through the environment, because this check
    reads constants rather than variables: there is no value an operator can put in a compose
    file that makes a batch too large. So what is asserted is the call itself, which is the part
    a tidy-up removes.

    Delete this and the call can be taken out of `preflight` with every arithmetic test in this
    file still green."""
    environment = {
        "QUEUE_URL": "postgresql+psycopg://brain:pw@db:5432/brain",
        "DATABASE_URL": "postgresql+psycopg://brain:pw@pgbouncer:5432/brain",
        "BRAIN_WORKER_SLOTS_HUMAN_INTERACTIVE": "0",
        "BRAIN_WORKER_SLOTS_HUMAN_ASYNC": "4",
        "BRAIN_WORKER_SLOTS_AUTOMATION": "2",
        "BRAIN_WORKER_SLOTS_SYSTEM": "1",
    }
    assert preflight(environment) == ()

    monkeypatch.setattr(
        worker_module, "embed_batch_gaps", lambda: ("a batch would not fit this slot",)
    )

    assert "a batch would not fit this slot" in preflight(environment)
