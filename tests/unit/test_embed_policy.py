"""The decisions taken about an embedding before anything is sent, and the ones that are wrong.

Four of these are written in a particular way and it is worth saying why before the rest.

**The width test asks the column, not the module.** `COLUMN_DIMENSIONS` is read off
`know.chunk.embedding`'s own type object, so asserting it against
`brain.knowledge.search.EMBEDDING_DIMENSIONS` would compare two names for one number. The
assertion is against the rendered column specification, which is what PostgreSQL is actually
told, and against the *shape* of `served_embedding_model`: a function with no width parameter
is what makes a model change a migration, and a signature is the only thing that can say so
without being a comment.

**The outage tests assert against `brain.core.errors`.** Which outcome each leg reports is the
whole decision in this module, and asserting `response.outcome is Outcome.DEGRADED` against a
name imported from the module under test would be green for every outcome it could hold. The
value comes from the taxonomy instead, and the two legs are asserted to differ, which is the
property rather than the pair of constants.

**The tolerance is asserted through the property that makes it right**, in both directions: it
has to admit a unit vector carrying half-precision noise, and it has to refuse a vector nobody
normalised. A test comparing it against itself would pass at any value including zero.

**The run tests count the calls the fake received.** A run that stopped at the third batch and
one that ran all five and threw four away produce the same `completed`, and only the call count
separates them. That is the difference between stopping and swallowing.

Task ids: none
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, replace

import pytest

from brain.core.errors import Outcome
from brain.knowledge.chunking import Block, BlockKind, ChunkBounds, chunk_document
from brain.knowledge.embed_policy import (
    COLUMN_DIMENSIONS,
    EMBED_TIMEOUT_SECONDS,
    FP16_ACCUMULATED_ERROR,
    QUESTION_UNIT_ID,
    QWEN3_EMBEDDING_DIMENSIONS,
    VECTOR_NORM_TOLERANCE,
    EmbeddingLeg,
    EmbeddingUnavailable,
    EmbedRun,
    accept_vectors,
    dimension_gaps,
    embed_all,
    outage_response,
    policy_gaps,
    question_batch,
    question_vector,
    served_embedding_model,
    vector_norm,
)
from brain.knowledge.embed_queue import (
    Embedded,
    EmbeddingBatch,
    EmbeddingUnit,
    EmbeddingWrite,
)
from brain.knowledge.embedding import EMBEDDING_FIELD, EmbeddedVector, EmbeddingError
from brain.knowledge.item import KnowledgeItem
from brain.knowledge.search import CHUNK, EMBEDDING_DIMENSIONS, Vector
from brain.knowledge.visibility import KnowledgeVisibility
from brain.ops.inference import SERVED_MODELS, InferenceRefused, InferenceTask
from brain.ops.queue import stale_after

A_REVISION = "v1.0.0"
MODEL = served_embedding_model(revision=A_REVISION)


def _unit_values(dimensions: int = COLUMN_DIMENSIONS) -> tuple[float, ...]:
    """A vector of length one, built from the width rather than from a stored literal."""
    return tuple([1.0 / math.sqrt(dimensions)] * dimensions)


def _vector(values: tuple[float, ...] | None = None) -> EmbeddedVector:
    return EmbeddedVector(model=MODEL, values=values if values is not None else _unit_values())


def _batch(*chunk_ids: str) -> EmbeddingBatch:
    return EmbeddingBatch(
        model=MODEL,
        units=tuple(EmbeddingUnit(chunk_id=one, text=f"text for {one}") for one in chunk_ids),
    )


@dataclass
class FakeService:
    """An `EmbeddingService` that answers, fails or answers badly, and counts being asked.

    The call count is the point of it. Everything else here could be a lambda.
    """

    fail_on_call: int = 0
    answer_partially_on_call: int = 0
    calls: int = 0

    def embed(self, batch: EmbeddingBatch) -> tuple[Embedded, ...]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            msg = "the inference server did not answer"
            raise EmbeddingUnavailable(msg)
        units = batch.units[:1] if self.calls == self.answer_partially_on_call else batch.units
        return tuple(Embedded(chunk_id=unit.chunk_id, vector=_vector()) for unit in units)


# ---------------------------------------------------- the two legs, and which one is silent


@pytest.mark.parametrize("leg", list(EmbeddingLeg))
def test_every_leg_says_what_an_unreachable_service_means_for_it(leg: EmbeddingLeg) -> None:
    """Parametrised over the enum rather than over a list written out here, so a third leg
    added without a decision beside it fails immediately rather than falling through to
    whichever response the lookup happened to reach.

    Delete this and a leg can be added with no outage policy at all, which is the shape
    `brain.ops.limit_store.UNREACHABLE_POLICY` has a test of its own to prevent."""
    response = outage_response(leg)
    assert response.leg is leg
    assert response.reason.strip()


def test_a_question_that_could_not_be_embedded_is_never_reported_as_an_ordinary_answer() -> None:
    """The decision this module is arranged around. An answer composed without the
    nearest-neighbour leg is thinner than one composed with it, and nothing else in the system
    will ever tell the person reading it, so the outcome carries it.

    The outcome is compared against the taxonomy in `brain.core.errors` and against the other
    leg, never against a name from the module under test, which would be green for every value
    it could hold.

    Delete this and the query leg can be quietly given the ingest leg's outcome, at which point
    a degraded retrieval reaches a person as an ordinary answer."""
    query = outage_response(EmbeddingLeg.QUERY)
    ingest = outage_response(EmbeddingLeg.INGEST)

    assert query.outcome is Outcome.DEGRADED
    assert query.outcome is not ingest.outcome


def test_an_outage_on_either_leg_writes_nothing_at_all() -> None:
    """Nothing written is what makes both legs safe to re-drive: an embedding is an update of
    two columns keyed by chunk id, and a partial write moves a rebuild's position past rows
    nobody embedded.

    Delete this and a leg can be given permission to record what it managed before the server
    stopped answering, which is the corpus permanently holding two models."""
    assert not any(outage_response(leg).writes_anything for leg in EmbeddingLeg)


def test_only_the_ingest_leg_is_retried_without_anybody_asking() -> None:
    """The asymmetry in one line. An ingest job is re-driven by the queue and the vector
    arrives late; a question is not re-asked, because the person has already been given an
    answer and no later run repairs it.

    Delete this and the query leg can be marked as retried, which is a promise that something
    will fix the answer somebody has already read."""
    assert outage_response(EmbeddingLeg.INGEST).retried
    assert not outage_response(EmbeddingLeg.QUERY).retried


def test_an_outage_never_reports_an_outcome_that_says_something_about_what_exists() -> None:
    """`DENIED` and `ABSENT` are the two outcomes that describe the world rather than this
    system, and an embedding outage describes neither: it says a service did not answer, which
    is true whatever the corpus holds and whoever is asking.

    Delete this and an outage can be reported as `ABSENT`, which tells a person their document
    is not there because a container restarted."""
    outcomes = {outage_response(leg).outcome for leg in EmbeddingLeg}
    assert not outcomes & {Outcome.DENIED, Outcome.ABSENT}


def test_a_leg_with_no_declared_response_is_refused_rather_than_given_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default is the entry somebody gets by not thinking about it, and the one that would be
    chosen by accident is the quiet one.

    Delete this and `outage_response` can be rewritten to fall back to the ingest leg, so a new
    leg silently inherits "write nothing and say nothing" whether or not that is right for
    it."""
    from brain.knowledge import embed_policy

    monkeypatch.setattr(embed_policy, "OUTAGE_POLICY", {EmbeddingLeg.INGEST: None})
    with pytest.raises(EmbeddingError, match="outage response"):
        embed_policy.outage_response(EmbeddingLeg.QUERY)


# ------------------------------------------------- the width is the column's (M7.3.3)


def test_the_width_this_system_asks_for_is_the_width_the_column_was_created_with() -> None:
    """Asserted against the rendered column specification, which is the string PostgreSQL is
    given, rather than against `EMBEDDING_DIMENSIONS`, which would be one name for the number
    checked against another name for the same number.

    Delete this and `COLUMN_DIMENSIONS` can be replaced by a literal that agrees with the
    column today and drifts from it at the next migration, at which point every insert is
    refused and nothing says why."""
    column_type = CHUNK.c[EMBEDDING_FIELD].type
    assert isinstance(column_type, Vector)
    assert column_type.get_col_spec() == f"VECTOR({COLUMN_DIMENSIONS})"


def test_a_model_identity_cannot_be_asked_for_at_any_width_but_the_column_s() -> None:
    """The structural half of "a model change is a migration". A function with a `dimensions`
    parameter is a function an operator can point at a model of another width, and the failure
    that follows is either every insert being refused or, worse, a width that happens to match
    and vectors from a space nothing recorded.

    The signature is asserted rather than the value, because the value being right today says
    nothing about whether it can be made wrong tomorrow.

    Delete this and a `dimensions` argument can be added for the convenience of one caller, and
    the width stops being a fact about the database."""
    parameters = inspect.signature(served_embedding_model).parameters
    assert set(parameters) == {"revision"}
    assert served_embedding_model(revision=A_REVISION).dimensions == COLUMN_DIMENSIONS


def test_the_model_named_is_one_the_container_was_sized_to_hold() -> None:
    """`SERVED_MODELS` is where the weights the inference container's memory limit was computed
    from are declared, so a model reachable through this function that is not in that list is
    memory no budget accounted for.

    Delete this and the name can be typed here instead, which is how a fourth set of weights
    ends up resident in a container sized for three."""
    declared = {model.name for model in SERVED_MODELS if model.task is InferenceTask.EMBEDDING}
    assert served_embedding_model(revision=A_REVISION).name in declared


def test_a_model_narrower_than_the_column_is_reported_as_a_migration_and_not_a_setting() -> None:
    """The finding has to say which kind of change it is, because the two have completely
    different costs: a setting is an edit and a restart, and this is an `ALTER`, an index
    rebuild and a re-embed of everything the company has ever uploaded.

    Delete this and the disagreement can be reported as a configuration problem, which sends
    somebody looking for an environment variable that does not exist."""
    findings = dimension_gaps(model_dimensions=8, column_dimensions=16)

    assert len(findings) == 1
    assert "8" in findings[0]
    assert "16" in findings[0]
    assert "migration" in findings[0]


def test_two_widths_that_agree_are_reported_as_nothing() -> None:
    """The positive case. A check tested only by its refusals is satisfied by a function that
    refuses everything, and this one runs on every deployment.

    Delete this and `dimension_gaps` can be made to fire unconditionally, which turns a real
    finding into one everybody learns to scroll past."""
    assert dimension_gaps(model_dimensions=16, column_dimensions=16) == ()


def test_this_deployment_cannot_embed_with_the_model_it_names() -> None:
    """The honest state of M7.3.3 as it stands, asserted rather than written in a comment. The
    column is the width of a hosted model chosen in `brain.knowledge.search` and Qwen3 produces
    fewer dimensions than that; Matryoshka truncation shortens a vector and cannot lengthen
    one, so no setting on the far side closes the gap.

    The column figure in the message is checked against `search.EMBEDDING_DIMENSIONS`, which is
    outside the module under test, so this cannot pass by both ends moving together.

    Delete this and the disagreement becomes something a reader has to notice for themselves,
    which is exactly how a leaf gets claimed for a model that cannot store a vector."""
    assert QWEN3_EMBEDDING_DIMENSIONS < EMBEDDING_DIMENSIONS
    findings = dimension_gaps()
    assert len(findings) == 1
    assert str(EMBEDDING_DIMENSIONS) in findings[0]


# ------------------------------------------------------ normalisation, checked not applied


def test_a_vector_nobody_normalised_is_refused_and_the_message_names_the_far_side() -> None:
    """`brain.knowledge.search.VECTOR_INDEX` is built with `vector_cosine_ops` on the stated
    assumption that these vectors are normalised, and nothing else in this system has ever
    checked it. Sentence encoders return un-normalised vectors unless asked, so the assumption
    is one flag away from being false.

    Delete this and the comment beside the index becomes the only thing asserting a property of
    the far side, which is a comment asserting a fact about another process."""
    unnormalised = _vector(tuple([1.0] * COLUMN_DIMENSIONS))
    with pytest.raises(InferenceRefused, match="normalise"):
        accept_vectors([Embedded(chunk_id="k_a.0000", vector=unnormalised)])


def test_a_normalised_vector_carrying_half_precision_noise_is_accepted() -> None:
    """The positive case, and the one that decides whether the tolerance is usable at all. A
    check that refused ordinary fp16 arithmetic would refuse every honest response, and the fix
    somebody would reach for is deleting the check.

    Delete this and the tolerance can be tightened to something no real pipeline satisfies, and
    the embedding leg stops working for a reason nobody would look for on the far side."""
    values = list(_unit_values())
    values[0] += FP16_ACCUMULATED_ERROR / 2
    accepted = accept_vectors([Embedded(chunk_id="k_a.0000", vector=_vector(tuple(values)))])

    assert len(accepted) == 1
    assert accepted[0].chunk_id == "k_a.0000"


def test_the_norm_tolerance_admits_arithmetic_error_and_still_refuses_an_unscaled_vector() -> None:
    """The tolerance asserted through the two properties that make the figure right rather than
    against itself, which would be green at zero and at one.

    Delete this and the tolerance can be set to any value at all: too tight and every response
    is refused, too loose and a vector of any scale passes, and both look like an ordinary
    number in a diff."""
    assert VECTOR_NORM_TOLERANCE > FP16_ACCUMULATED_ERROR
    assert VECTOR_NORM_TOLERANCE < 1.0
    assert vector_norm(tuple([1.0] * COLUMN_DIMENSIONS)) == pytest.approx(
        math.sqrt(COLUMN_DIMENSIONS)
    )
    assert vector_norm(_unit_values()) == pytest.approx(1.0)


def test_the_checked_vectors_are_handed_back_so_an_unchecked_list_cannot_be_used() -> None:
    """Returning the values rather than None is what stops a caller writing
    `accept_vectors(...)` on a line of its own and then using the list it already had, which
    still compiles and skips nothing visible.

    Delete this and the function can be made to return None, at which point forgetting to
    assign it is a silent removal of the check."""
    embedded = [Embedded(chunk_id="k_a.0000", vector=_vector())]
    assert accept_vectors(embedded) == tuple(embedded)


# ------------------------------------------------------ how long a request may take


def test_a_request_is_abandoned_before_the_queue_decides_its_job_is_orphaned() -> None:
    """Compared against the queue's own threshold rather than against the constant beside it. A
    request allowed to outlast `stale_after` produces a second copy of the job while the first
    is still waiting, so a slow server is sent the same batch twice and the log shows one job.

    Delete this and the timeout can be raised to something comfortable, and the symptom is
    doubled load on a server that was already too slow rather than any error at all."""
    assert 0 < EMBED_TIMEOUT_SECONDS < stale_after().total_seconds()


def test_a_timeout_the_queue_cannot_tolerate_is_reported_with_both_numbers() -> None:
    """The check has to name the queue's threshold as well as the timeout, because the fix is
    to choose between them and a finding naming one of the two does not say what to compare it
    against.

    Delete this and the relation between the two figures stops being checkable, and they are
    decided in two files by two people."""
    stale = stale_after().total_seconds()
    findings = policy_gaps(model_dimensions=16, column_dimensions=16, timeout_seconds=stale)

    assert len(findings) == 1
    assert str(stale) in findings[0]


def test_a_timeout_of_nothing_is_reported_rather_than_treated_as_no_limit() -> None:
    """Zero reads as "no timeout" in most libraries and as "give up at once" in others, and
    either way it is a figure nobody chose.

    Delete this and a timeout of zero passes every check, which is a client that abandons every
    request before the server has finished reading it."""
    findings = policy_gaps(model_dimensions=16, column_dimensions=16, timeout_seconds=0.0)
    assert findings and any("abandons" in one for one in findings)


def test_a_deployment_whose_widths_agree_and_whose_timeout_fits_reports_nothing() -> None:
    """The positive case for the whole check. Without it, a `policy_gaps` that returned a
    finding unconditionally would satisfy every test above.

    Delete this and the gaps function can be made to fire always, and preflight becomes a
    banner rather than a check."""
    assert policy_gaps(model_dimensions=16, column_dimensions=16) == ()


# ------------------------------------------------------ a question is not a passage


def test_a_question_travels_under_an_id_no_chunk_could_ever_hold() -> None:
    """Proved from a real chunk rather than from the grammar written in a docstring:
    `chunk_document` is the only thing that makes a chunk id, and its ids carry an item id and
    a four-digit ordinal.

    Delete this and the question's id can be changed to something a document could produce, at
    which point a response about a question and a response about a passage are the same
    message and one can be written where the other was meant."""
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
    batch = question_batch("who deploys on a Tuesday", model=MODEL)

    assert [unit.chunk_id for unit in batch.units] == [QUESTION_UNIT_ID]
    assert chunks and all(chunk.chunk_id != QUESTION_UNIT_ID for chunk in chunks)


def test_a_question_that_was_answered_yields_a_vector_and_never_a_write() -> None:
    """The positive case, and the structural claim beside it: the query leg's reader returns an
    `EmbeddedVector`, so there is no value on this path that could be applied to a row of
    `know.chunk`.

    Delete this and the query leg can be rewritten to go through `writes_for` for the saving,
    at which point a question's vector is one `columns` call away from the corpus."""
    vector = question_vector([Embedded(chunk_id=QUESTION_UNIT_ID, vector=_vector())])

    assert isinstance(vector, EmbeddedVector)
    assert not isinstance(vector, EmbeddingWrite)
    assert vector.values == _unit_values()


def test_a_response_naming_something_other_than_the_question_is_refused() -> None:
    """Matching by position is what this refuses. One input and one vector always line up by
    position, so a response naming a different id would pass every count-based check and embed
    whichever text the far side had in hand.

    Delete this and the query leg searches for a vector made from somebody else's text, and
    every citation it returns looks perfectly well formed."""
    with pytest.raises(InferenceRefused, match="position"):
        question_vector([Embedded(chunk_id="k_a.0000", vector=_vector())])


def test_a_response_carrying_more_than_one_vector_for_one_question_is_refused() -> None:
    """There is no rule for choosing among them and taking the first is a rule invented at the
    call site by whoever is in a hurry.

    Delete this and a server answering twice picks the question's meaning for it."""
    two = [
        Embedded(chunk_id=QUESTION_UNIT_ID, vector=_vector()),
        Embedded(chunk_id=QUESTION_UNIT_ID, vector=_vector()),
    ]
    with pytest.raises(InferenceRefused, match="2 vector"):
        question_vector(two)


# ------------------------------------------------------ many batches, one run (M7.3.3)


def test_a_run_that_completed_every_batch_reports_every_write_in_batch_order() -> None:
    """The positive case. Every refusal below is satisfied by a runner that returns nothing at
    all, and the order matters because a caller writing these in one transaction takes its row
    locks in it.

    Delete this and `embed_all` can be made to drop writes silently while every failure test
    still passes."""
    batches = (_batch("k_a.0000", "k_a.0001"), _batch("k_a.0002"))
    run = embed_all(batches, FakeService())

    assert run.is_complete
    assert [write.chunk_id for write in run.writes] == ["k_a.0000", "k_a.0001", "k_a.0002"]


def test_a_run_that_failed_partway_is_not_reported_as_one_that_finished() -> None:
    """The whole of the third design question. `writes_for` refuses a response covering part of
    one batch; this is the level above, where a loop over several batches is what rounds
    partial up to complete.

    Delete this and a run that embedded two batches of five reports success, and the caller
    advances a cursor past three batches nobody embedded."""
    batches = (_batch("k_a.0000"), _batch("k_a.0001"), _batch("k_a.0002"))
    run = embed_all(batches, FakeService(fail_on_call=2))

    assert not run.is_complete
    assert (run.completed, run.planned) == (1, 3)
    assert run.failure


def test_a_run_stops_at_the_first_failure_rather_than_writing_past_the_hole() -> None:
    """The call count is what separates stopping from swallowing: a run that continued and
    discarded the rest would report the same counts and would have spent a round trip on every
    remaining batch against a server that is down.

    It also protects the position. Writes from batches after the failure carry a larger chunk
    id, `RebuildCursor.advance` accepts any id that moved forward, and the rows in the gap are
    then left on the old model with nothing recording it.

    Delete this and the loop can be given a `continue`, which is one word and turns a resumable
    rebuild into a corpus with holes in it."""
    batches = tuple(_batch(f"k_a.{index:04d}") for index in range(5))
    service = FakeService(fail_on_call=3)
    run = embed_all(batches, service)

    assert service.calls == 3
    assert [write.chunk_id for write in run.writes] == ["k_a.0000", "k_a.0001"]
    assert run.last_chunk_id == "k_a.0001"


def test_a_batch_answered_in_part_is_not_counted_as_a_batch_that_completed() -> None:
    """A response missing one of the ids it was asked about is refused whole by `writes_for`,
    and this is the assertion that the run treats that refusal as a stop rather than as a
    smaller batch.

    Delete this and a server returning half a batch produces a run that looks like a shorter
    one, and the chunks it skipped stay unembedded with the position past them."""
    batches = (_batch("k_a.0000"), _batch("k_a.0001", "k_a.0002"))
    run = embed_all(batches, FakeService(answer_partially_on_call=2))

    assert not run.is_complete
    assert run.completed == 1
    assert [write.chunk_id for write in run.writes] == ["k_a.0000"]


def test_a_run_cannot_be_constructed_claiming_every_batch_while_carrying_a_failure() -> None:
    """The invariant is enforced at construction rather than by asking a caller to read the
    right field, because the failure being closed is a caller reading the wrong one.

    Delete this and a run can report five of five complete and a failure at the same time, and
    which half a reader believes is a coin toss."""
    with pytest.raises(EmbeddingError, match="one of the two is false"):
        EmbedRun(planned=2, completed=2, failure="the server did not answer")


def test_a_run_that_stopped_short_and_says_why_nowhere_is_refused() -> None:
    """The other direction of the same invariant. A run reporting two of five with no reason is
    reported as having been interrupted by nothing, and its position is past rows nobody
    embedded.

    Delete this and a caller can build a silent partial run, which is the state this whole
    value exists to make impossible."""
    with pytest.raises(EmbeddingError, match="says why nowhere"):
        EmbedRun(planned=5, completed=2)


def test_a_run_cannot_report_more_batches_than_it_had() -> None:
    """A count larger than the plan is arithmetic that cannot be true, and the number that
    would be trusted is the wrong one.

    Delete this and a miscounted loop reports a rebuild covering more than it planned, which
    reads as success."""
    with pytest.raises(EmbeddingError, match="more than it had"):
        EmbedRun(planned=1, completed=2)


def test_a_run_that_wrote_nothing_reports_no_position_rather_than_the_beginning() -> None:
    """An empty position and the first chunk id are different states: one is "nothing was
    written" and the other is "one row was". `RebuildCursor` spells the beginning as an empty
    string for the same reason.

    Delete this and a failed first batch can report a position, which advances a cursor over
    rows nobody embedded."""
    run = embed_all((_batch("k_a.0000"),), FakeService(fail_on_call=1))

    assert run.last_chunk_id == ""
    assert run.writes == ()


def test_a_bug_in_this_repository_is_not_folded_into_a_run_report() -> None:
    """`embed_all` catches `EmbeddingError` and nothing wider. A `TypeError` here is ours, and
    reporting it as a batch that failed sends somebody to look at a server that is fine.

    Delete this and the except clause can be widened to `Exception`, at which point every
    programming error in the embed path is reported as an inference outage."""

    @dataclass
    class Broken:
        def embed(self, batch: EmbeddingBatch) -> tuple[Embedded, ...]:
            raise TypeError(batch.model.identity)

    with pytest.raises(TypeError):
        embed_all((_batch("k_a.0000"),), Broken())


def test_a_run_carries_the_writes_of_the_batches_that_did_complete() -> None:
    """A failure partway does not make the work already done worthless: those rows are correct,
    the write is an update keyed by chunk id, and re-driving the job rewrites them identically.
    What the run must not do is claim the ones it did not reach.

    Delete this and a failed run can be made to discard everything, which turns one bad batch
    into an outage's worth of work thrown away on every re-drive."""
    batches = (_batch("k_a.0000"), _batch("k_a.0001"))
    run = embed_all(batches, FakeService(fail_on_call=2))

    assert len(run.writes) == 1
    assert run.writes[0].columns[EMBEDDING_FIELD] == _unit_values()


def test_a_replaced_run_is_checked_again_rather_than_trusted() -> None:
    """`replace` on a frozen dataclass re-runs `__post_init__`, and this asserts that it does:
    a caller editing a completed run's counters cannot produce one that claims what it did not
    do.

    Delete this and the invariant holds only for runs built by `embed_all`, which is the one
    caller that was never going to break it."""
    run = embed_all((_batch("k_a.0000"),), FakeService())
    with pytest.raises(EmbeddingError):
        replace(run, completed=0)
