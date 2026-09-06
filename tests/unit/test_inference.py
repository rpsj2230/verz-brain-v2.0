"""The inference contract: what it refuses on the wire, what it costs, and where it may not go.

Three of these are worth reading before the rest, because they are the ones that would be
easy to write in a way that proves nothing.

**The model test is written from the raw payload, not from an `Embedded`.** A producer and a
consumer sit either side of this value: `decode_embeddings` reads a model off the wire and
`writes_for` compares it against the batch. A test that hands `writes_for` an `Embedded` it
built itself has tested the consumer twice and the producer not at all, which is the lesson
`CLAUDE.md` records about `lark_wiki.restriction_of`. So the model tests go through the
decoder from a dictionary, and the one that matters asserts the failure the decoder exists to
prevent: a decoder filling the model in from the batch would make the check downstream
compare the batch with itself and pass for every set of weights the server could be running.

**The sizing constants are asserted against something outside themselves.** `weights_mib` on
the two models with published parameter counts is checked against that arithmetic rather than
against the module's own number, and `INFERENCE_RUNTIME_RESERVE_MIB` through the property it
exists for, which is that the request ceiling sits strictly below what is left after the
weights. Asserting either against itself would be green for every value it could hold, which
is the failure `CLAUDE.md` records catching three authors in one afternoon.

**The request test asserts the whole key set, in both directions.** The permission boundary
here is structural: a request that cannot grow a key cannot grow one that carries a scope. A
test that asserted only that the expected keys are present would pass with a `department`
beside them, and that is the one addition anybody would make in good faith.

Task ids: none
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pytest

from brain.config import check
from brain.connectors.throttle import CallOutcome
from brain.knowledge.embed_queue import (
    MIB,
    EmbeddingBatch,
    EmbeddingUnit,
    batch_budget_bytes,
    writes_for,
)
from brain.knowledge.embedding import EmbeddingModel, MixedEmbeddingError
from brain.knowledge.ingest import CAUSE_TEXT, MediaType, ParseCause, ParseFailure
from brain.knowledge.search import EMBEDDING_DIMENSIONS
from brain.ops import worker as worker_module
from brain.ops.inference import (
    INFERENCE_COMPONENT,
    INFERENCE_DESTINATION_SETTINGS,
    INPUT_KEYS,
    MODEL_KEYS,
    REQUEST_KEYS,
    REQUESTS_AT_ONCE,
    SERVED_MODELS,
    InferenceRefused,
    InferenceTask,
    ServedModel,
    decode_embeddings,
    embedding_request,
    inference_config_conflicts,
    inference_gaps,
    parse_cause_for,
    request_ceiling_bytes,
    runs_inference_server,
    served_model,
    weights_mib,
)
from brain.ops.wiring import Wiring, WiringError, component
from brain.ops.worker import preflight

MODEL = EmbeddingModel(name="qwen3-embedding", revision="a1b2c3d", dimensions=EMBEDDING_DIMENSIONS)
OTHER = EmbeddingModel(name="qwen3-embedding", revision="e4f5g6h", dimensions=EMBEDDING_DIMENSIONS)

#: Published parameter counts and the precision each model's weights are stored at, written
#: here rather than imported from the module under test. These are facts about somebody
#: else's model card, which is what makes them usable to check our arithmetic against: a test
#: reading `weights_mib` back out of `SERVED_MODELS` would compare the figure with itself.
#: Docling is deliberately absent, because it publishes no single parameter count and its
#: entry says so.
PUBLISHED_PARAMETERS: dict[str, tuple[int, int]] = {
    "qwen3-embedding-0.6b": (600_000_000, 2),
    "gliner": (210_000_000, 4),
}


def _units(count: int) -> tuple[EmbeddingUnit, ...]:
    return tuple(
        EmbeddingUnit(chunk_id=f"k_doc.{index:04d}", text="something to embed")
        for index in range(count)
    )


def _batch(count: int = 2, *, model: EmbeddingModel = MODEL) -> EmbeddingBatch:
    return EmbeddingBatch(model=model, units=_units(count))


def _model_block(model: EmbeddingModel) -> dict[str, Any]:
    return {"name": model.name, "revision": model.revision, "dimensions": model.dimensions}


def _values(mark: float = 1.0) -> list[float]:
    return [mark, *([0.0] * (EMBEDDING_DIMENSIONS - 1))]


def _payload(batch: EmbeddingBatch, *, answered_by: EmbeddingModel | None = None) -> dict[str, Any]:
    """A well-formed response for this batch, from whichever model is named.

    `answered_by` defaults to the batch's model so the positive cases read plainly, and is a
    parameter so the disagreement case is a change of one argument rather than a hand-built
    dictionary that could differ in some other way as well.
    """
    model = batch.model if answered_by is None else answered_by
    return {
        "model": _model_block(model),
        "vectors": [
            {"chunk_id": unit.chunk_id, "values": _values(float(index + 1))}
            for index, unit in enumerate(batch.units)
        ],
    }


# ---------------------------------------------------- the model comes off the wire (M7.3.3)
def test_the_model_a_response_names_is_the_one_recorded_and_not_the_one_asked_for() -> None:
    """**The whole reason this decoder exists**, asserted from the raw payload rather than
    from a value the test built.

    `writes_for` refuses a response whose model disagrees with its batch. That refusal is
    worth nothing if the model it compares was copied out of the batch on the way in: the
    comparison is then between a value and itself and passes for every set of weights the
    server could have been running, including a previous one it had not finished unloading.
    The corpus then holds vectors from two spaces under one recorded identity, which is
    exactly the state `brain.knowledge.embedding` exists to prevent, arriving through the
    check written to prevent it.

    Delete this and `decode_embeddings` can be simplified to build `EmbeddedVector(model=
    batch.model, ...)`, every other test in this file stays green, and the guard downstream
    silently stops being a guard."""
    batch = _batch()

    decoded = decode_embeddings(batch, _payload(batch, answered_by=OTHER))

    assert [one.vector.model.identity for one in decoded] == [OTHER.identity] * 2
    with pytest.raises(MixedEmbeddingError, match="did not produce it"):
        writes_for(batch, decoded)


def test_a_response_that_names_the_model_it_was_asked_for_is_written_down() -> None:
    """The positive case, and it is not a formality: a decoder that refused every response
    would pass the test above and every other refusal here. This is also the end-to-end
    path, from a dictionary to the two columns an embedding writes, which is the only shape
    that proves the decoder and `writes_for` agree about what a well-formed answer is.

    Delete this and the contract can refuse everything while looking thoroughly tested."""
    batch = _batch(3)

    writes = writes_for(batch, decode_embeddings(batch, _payload(batch)))

    assert [w.chunk_id for w in writes] == [unit.chunk_id for unit in batch.units]
    assert {w.vector.model.identity for w in writes} == {MODEL.identity}


def test_a_response_stating_no_model_is_refused_rather_than_assumed() -> None:
    """The tempting fallback is the dangerous one. A response with no model block could be
    filled in from the batch and everything downstream would work, which is precisely why it
    must not be: the vectors were produced by something, and a server that has stopped
    saying what is a server that has changed something.

    Delete this and an absent model becomes the requested model, which is the same defect as
    the test above arriving through a missing key rather than through a shortcut."""
    batch = _batch()
    payload = _payload(batch)
    del payload["model"]

    with pytest.raises(InferenceRefused, match="states no model"):
        decode_embeddings(batch, payload)


@pytest.mark.parametrize("missing", MODEL_KEYS)
def test_a_model_missing_any_of_its_three_fields_is_refused(missing: str) -> None:
    """Parametrised over the fields themselves, so a fourth arrives here already checked.

    All three are load-bearing and for different reasons `brain.knowledge.embedding` sets
    out: a name alone cannot tell two sets of weights apart behind one label, a revision is
    what says which weights, and the width is what makes the same family at two dimensions
    two spaces rather than one.

    Delete this and a partial identity is accepted, `EmbeddingModel` is constructed from
    whatever is left, and two vector spaces are recorded under one string."""
    batch = _batch()
    payload = _payload(batch)
    del payload["model"][missing]

    with pytest.raises(InferenceRefused, match="states"):
        decode_embeddings(batch, payload)


def test_a_model_this_system_could_not_store_is_refused_at_the_wire() -> None:
    """`EmbeddingModel` already refuses a name the grammar does not admit and a width pgvector
    will not index. The value of doing it here is when: at the wire, before a batch of
    vectors is held in memory and before anything is written, rather than at the first
    write of a job that has already embedded everything.

    Delete this and a malformed identity raises an `EmbeddingError` somewhere further along,
    where it reads as our arithmetic rather than as the far side's answer."""
    batch = _batch()
    payload = _payload(batch)
    payload["model"]["name"] = "Qwen3 Embedding"

    with pytest.raises(InferenceRefused, match="cannot record"):
        decode_embeddings(batch, payload)


def test_a_width_that_is_not_a_number_is_refused_rather_than_coerced() -> None:
    """A width arrives as JSON and JSON has one number type, so a server sending "1536" as a
    string is an ordinary bug on the far side. Coercing it would work and would also coerce
    `true`, which Python counts as 1, into a one-dimensional model.

    Delete this and a boolean becomes a vector width, `EmbeddedVector` refuses the length,
    and the refusal names the wrong thing entirely."""
    batch = _batch()
    payload = _payload(batch)
    payload["model"]["dimensions"] = str(EMBEDDING_DIMENSIONS)

    with pytest.raises(InferenceRefused, match="not one"):
        decode_embeddings(batch, payload)


# ------------------------------------------------------- a well-formed answer that is wrong
@pytest.mark.parametrize("poison", [math.nan, math.inf, -math.inf])
def test_a_vector_carrying_a_value_that_is_not_finite_is_refused(poison: float) -> None:
    """**The failure with no symptom anywhere.** A NaN in a stored vector makes every distance
    against that row NaN; PostgreSQL orders NaN above every real number, so the row never
    ranks, never appears in an answer and never errors. The passage sits in the corpus,
    indexed, cited by nothing, and no query reports it. An infinity gets there by a different
    route and ends in the same place.

    Nothing else in this system produces a float, so the wire is the only door this can come
    through and this is the only place it can be closed.

    Delete this and a provider bug becomes a corpus with holes in it that nobody can find."""
    batch = _batch(1)
    payload = _payload(batch)
    payload["vectors"][0]["values"][7] = poison

    with pytest.raises(InferenceRefused, match="in its vector"):
        decode_embeddings(batch, payload)


def test_a_vector_of_zeros_is_refused_because_that_is_what_a_failed_encode_returns() -> None:
    """A zero vector is the shape a caught exception takes on the far side: something failed,
    something returned a default, and the response is well formed. Its cosine distance to
    anything is undefined, so the row matches nothing and reports nothing.

    It cannot arrive legitimately. `EmbeddingUnit` refuses empty text, so there is no input
    that could honestly produce one.

    Delete this and the one response shape that means "we could not do this" is written down
    as a vector."""
    batch = _batch(1)
    payload = _payload(batch)
    payload["vectors"][0]["values"] = [0.0] * EMBEDDING_DIMENSIONS

    with pytest.raises(InferenceRefused, match="vector of zeros"):
        decode_embeddings(batch, payload)


def test_an_ordinary_vector_whose_first_value_is_zero_is_still_accepted() -> None:
    """The positive case for the guard above, and the one that would break a lazy
    implementation. A real embedding routinely has zeros in it; only a vector that is
    entirely zero is the failure. A check written as "the first value is zero" or "any value
    is zero" would refuse most of a real corpus and every refusal test here would stay
    green."""
    batch = _batch(1)
    payload = _payload(batch)
    payload["vectors"][0]["values"] = [0.0, 0.0, 0.5, *([0.0] * (EMBEDDING_DIMENSIONS - 3))]

    decoded = decode_embeddings(batch, payload)

    assert decoded[0].vector.values[2] == 0.5


def test_a_vector_naming_no_chunk_is_refused_rather_than_matched_by_position() -> None:
    """`A_VECTOR_IS_MATCHED_TO_A_CHUNK_BY_ID_AND_NEVER_BY_POSITION`, enforced at the point the
    id would be lost. A response whose entries carry no id can only be paired with the
    request by position, which is the pairing that silently associates the wrong text with
    the wrong numbers and leaves every row looking well formed.

    Delete this and the decoder can fall back to the batch's order, which is the whole
    failure `writes_for` was written against, reintroduced upstream of it."""
    batch = _batch(2)
    payload = _payload(batch)
    del payload["vectors"][1]["chunk_id"]

    with pytest.raises(InferenceRefused, match="naming no chunk"):
        decode_embeddings(batch, payload)


def test_a_response_with_no_vectors_list_is_refused_and_is_not_an_empty_batch() -> None:
    """`A_REFUSAL_IS_NOT_AN_EMPTY_RESULT` at the point where the two could be confused. A
    missing list decoded as zero vectors is a batch that reports having written nothing,
    which a caller with a cursor would record as progress.

    Delete this and `payload.get("vectors")` returning None becomes an empty tuple, and a
    server answering `{}` moves a rebuild's position past rows nobody embedded."""
    batch = _batch()

    with pytest.raises(InferenceRefused, match="carries no"):
        decode_embeddings(batch, {"model": _model_block(MODEL)})


def test_an_empty_vectors_list_reaches_writes_for_as_a_batch_that_answered_nothing() -> None:
    """The other half of the same rule, and the reason the decoder does not check ids itself.
    An empty list is well formed, so it decodes; what refuses it is `writes_for`, which sees
    every id missing. The refusal is the same one a partial response gets, which is right:
    both are a batch that must not be written in part.

    Delete this and nothing anywhere proves that an empty answer is refused rather than
    quietly returning no writes."""
    batch = _batch(2)

    decoded = decode_embeddings(batch, {"model": _model_block(MODEL), "vectors": []})

    assert decoded == ()
    with pytest.raises(Exception, match="missing chunk"):
        writes_for(batch, decoded)


def test_a_response_naming_a_chunk_that_was_never_sent_is_refused_by_the_batch() -> None:
    """The far side's only route into the corpus, and it is closed by `writes_for` rather
    than here. A vector written against a chunk whose text was never sent would be a passage
    indexed as something else, chosen by the service rather than by the caller, which is the
    one way a response could widen what somebody sees.

    Asserted through the decoder rather than around it, because that is the path a real
    response takes and the point is that the two halves together refuse it.

    Delete this and the boundary in `THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE` rests on
    a sentence in a docstring."""
    batch = _batch(1)
    payload = _payload(batch)
    payload["vectors"].append({"chunk_id": "k_other.0001", "values": _values(9.0)})

    with pytest.raises(Exception, match="not in the batch"):
        writes_for(batch, decode_embeddings(batch, payload))


# ------------------------------------------------------------------ the request carries no scope
def test_a_request_carries_the_model_and_the_inputs_and_nothing_else() -> None:
    """**The permission boundary, made structural.** Everything sent here has already passed
    the permission layer, and what stops the request widening anything is that there is
    nowhere in it to put a scope.

    Asserted as the whole key set rather than as the presence of the expected keys, in both
    directions. A test checking only that `model` and `inputs` are there passes with a
    `department` sitting beside them, and adding one "so the server can filter" is exactly
    the change somebody would make in good faith.

    Delete this and a permission column reaches a process that has no business holding one,
    and the response becomes something a caller could read a decision out of."""
    request = embedding_request(_batch(2))
    inputs = request["inputs"]
    model_block = request["model"]

    assert set(request) == set(REQUEST_KEYS)
    assert isinstance(inputs, tuple)
    for one in inputs:
        assert set(one) == set(INPUT_KEYS)
    assert isinstance(model_block, Mapping)
    assert set(model_block) == set(MODEL_KEYS)


def test_a_request_cannot_be_edited_after_it_is_built() -> None:
    """`EmbeddingWrite.columns` makes the same argument and this one has the sharper edge: a
    mapping handed out of a value object is a mapping somebody mutates, and the mutation
    guarded against here is the one that adds a permission on the way to the wire.

    Delete this and the key-set test above holds only at the moment of construction."""
    request = embedding_request(_batch(1))

    with pytest.raises(TypeError):
        request["department"] = "finance"  # type: ignore[index]


def test_a_request_carries_the_batch_it_was_built_from_and_not_a_summary() -> None:
    """The positive case. A request that dropped the text would serialise, would be sent, and
    would come back with vectors for nothing; a request that dropped an id would come back
    unmatchable. Both are refused downstream and neither is worth discovering there."""
    batch = _batch(3)

    request = embedding_request(batch)
    inputs = request["inputs"]

    assert isinstance(inputs, tuple)
    assert [one["chunk_id"] for one in inputs] == [unit.chunk_id for unit in batch.units]
    assert [one["text"] for one in inputs] == [unit.text for unit in batch.units]


# ------------------------------------------------------------------ what a person is told
def test_no_inference_outcome_is_ever_reported_as_a_fact_about_the_document() -> None:
    """**The wrong answer this mapping exists to prevent**, and it costs somebody an
    afternoon rather than a query. Most of the wordings available say something about the
    file: re-export it, convert it, split it. None of those is true when the server was
    restarting.

    Asserted as the exact reachable set, with `TIMED_OUT` named separately because it is the
    wording that looks closest and is the one that must not be produced: `classify` folds a
    slow server and an absent one into one outcome, and only one sentence is true of both.

    Delete this and the constant can be changed to any `ParseCause` at all, including one
    that tells somebody to split a document that is perfectly fine."""
    reachable = {parse_cause_for(outcome) for outcome in CallOutcome} - {None}

    assert reachable == {ParseCause.PARSER_UNAVAILABLE}
    assert ParseCause.TIMED_OUT not in reachable


def test_every_cause_an_inference_outage_produces_is_one_the_uploader_may_retry() -> None:
    """The property behind the mapping rather than a second copy of it, and the retryable set
    is computed from `ParseFailure` rather than restated here. An outage is temporary by
    definition, so a cause that says otherwise sends somebody to change a file that was never
    the problem, and the retries a wrong cause suppresses are the ones that would have
    worked.

    Delete this and a cause could be chosen that is uniform, refuses to blame the file, and
    still marks the job dead."""
    retryable = {
        cause
        for cause in ParseCause
        if ParseFailure(cause=cause, media_type=MediaType.PDF).is_retryable
    }
    reachable = {parse_cause_for(outcome) for outcome in CallOutcome} - {None}

    assert reachable <= retryable
    assert reachable < retryable, "a mapping that reached every retryable cause has not chosen"


def test_a_call_that_worked_tells_the_uploader_nothing() -> None:
    """The positive case, and the one that stops the mapping being a function that always
    produces a failure. A parse that succeeded has no cause, and a cause for a success would
    be rendered into a notification about a document that was read correctly."""
    assert parse_cause_for(CallOutcome.OK) is None
    assert CAUSE_TEXT[ParseCause.PARSER_UNAVAILABLE].startswith("the parser was not reachable")


# ------------------------------------------------------------------ the sizing
@pytest.mark.parametrize("name", sorted(PUBLISHED_PARAMETERS))
def test_a_declared_footprint_matches_the_parameter_count_its_basis_claims(name: str) -> None:
    """The constants asserted against something outside themselves, which is the rule
    `CLAUDE.md` states after three authors were caught comparing a constant with itself in
    one afternoon.

    `PUBLISHED_PARAMETERS` holds facts from somebody else's model card. The declared figure
    has to be at least what that arithmetic gives, because a container sized below its
    weights cannot load them, and within 128 MiB of it, because the entries say they are
    rounded up to the next 128 and a figure well above the arithmetic is a different claim
    that would need a different basis.

    Delete this and `weights_mib` becomes whatever makes the budget pass."""
    model = next(m for m in SERVED_MODELS if m.name == name)
    parameters, bytes_each = PUBLISHED_PARAMETERS[name]
    floor_mib = math.ceil(parameters * bytes_each / MIB)

    assert model.weights_mib >= floor_mib, f"{name} cannot hold its own weights"
    assert model.weights_mib - floor_mib < 128, f"{name} claims more than its basis explains"


def test_the_request_ceiling_sits_strictly_below_what_is_left_after_the_weights() -> None:
    """`INFERENCE_RUNTIME_RESERVE_MIB` asserted through the property it exists for rather than
    against itself. The cgroup counts the interpreter, torch, the tokenisers, the HTTP server
    and the allocator's arenas, and none of those is weights; a ceiling equal to the
    remainder is a process told it may use memory the runtime has already taken.

    Delete this and the reserve can be set to zero, which makes every arithmetic check in
    this file pass and the container die on its first request."""
    limit_mib = component(INFERENCE_COMPONENT).memory_mib

    assert request_ceiling_bytes() < (limit_mib - weights_mib()) * MIB
    assert request_ceiling_bytes() > 0


def test_the_ceiling_is_the_whole_remainder_because_one_request_is_served_at_a_time() -> None:
    """`REQUESTS_AT_ONCE` asserted through what it means rather than by reading it back.
    The claim the module makes is that the ceiling is the whole of what is left, which is
    also why only one request fits; a second concurrent request would be allowed all of it
    again and the container has no second copy.

    The reserve is passed explicitly rather than left to its default, so this pins the
    concurrency and nothing else: with the reserve on both sides of the comparison it cannot
    quietly stand in for the thing under test.

    Delete this and the concurrency can be raised to two, every finding stays empty because
    the halved ceiling still just clears the batch budget, and the container is quietly
    promised twice what it has."""
    reserve_on_both_sides = 512
    limit_mib = component(INFERENCE_COMPONENT).memory_mib
    remainder_mib = limit_mib - weights_mib() - reserve_on_both_sides

    assert REQUESTS_AT_ONCE == 1
    assert request_ceiling_bytes(reserve_mib=reserve_on_both_sides) == remainder_mib * MIB


def test_the_largest_batch_the_planner_builds_fits_the_largest_request_the_server_takes() -> None:
    """The two ends of one number, edited in two files for two reasons. `batch_budget_bytes`
    descends from `MIB_PER_SLOT` and knows nothing about models; the ceiling descends from a
    container limit and knows nothing about queues.

    If the ceiling were the smaller, every full batch would be refused by the server after
    the planner had built it, which presents as an embedding leg that works on small
    documents and fails on large ones, weeks after either number moved.

    Delete this and the slot can grow or the container can shrink with nothing comparing
    them."""
    assert batch_budget_bytes() <= request_ceiling_bytes()


def test_every_task_this_service_is_asked_to_do_has_a_model_serving_it() -> None:
    """A task with no model is a request that can never be answered and the failure has no
    error in it: the caller waits, the call times out, and a deployment missing a model is
    indistinguishable from one that is merely down. `brain.ops.pii.configuration_gaps` makes
    the same check about a detection kind with no recogniser.

    Delete this and a task can be declared, budgeted for by nobody, and served by nothing."""
    for task in InferenceTask:
        assert served_model(task).task is task


def test_a_task_nothing_serves_is_refused_rather_than_returned_as_nothing() -> None:
    """A caller handed None writes `if model is None: return` and the task stops being served
    without anybody removing it, which is the argument `brain.ops.wiring.component` makes
    about a renamed component."""

    class _Unserved:
        value = "translation"

    with pytest.raises(WiringError, match="no model serves"):
        served_model(_Unserved())  # type: ignore[arg-type]


def test_no_served_model_footprint_has_been_measured_on_this_host() -> None:
    """A statement of fact today, and the test is the thing that has to be edited when it
    stops being one. Every figure in `SERVED_MODELS` is arithmetic from a published parameter
    count or a judgement; there is no such server here and no weights pulled, so there is
    nothing to measure.

    Delete this and a figure can be marked measured by somebody who did not measure it, which
    is worse than an unmeasured figure because it stops anybody asking."""
    assert [m.name for m in SERVED_MODELS if m.measured] == []


def test_a_model_that_does_not_say_where_its_figure_came_from_cannot_be_declared() -> None:
    """The shape `brain.ops.pii.BuiltIn` uses for `why_not_local`. A number in a memory budget
    is an assertion about a machine, and one whose origin nobody wrote down cannot be checked
    by the next person to read it.

    Delete this and the table becomes a list of integers somebody typed once."""
    with pytest.raises(WiringError, match="does not say where"):
        ServedModel(task=InferenceTask.EMBEDDING, name="probe", weights_mib=64, sizing_basis="   ")


def test_a_model_with_no_declared_weights_cannot_be_declared() -> None:
    """Zero is how "we did not think about it" is spelled in a dataclass, and a model that
    costs nothing to hold is a model the container was not sized for. The same refusal
    `Component` makes about a missing memory limit, one level down."""
    with pytest.raises(WiringError, match="no resident weights"):
        ServedModel(
            task=InferenceTask.PARSING, name="probe", weights_mib=0, sizing_basis="measured"
        )


# ------------------------------------------------------------------ the deployment gaps
def test_the_declared_deployment_has_nothing_wrong_with_it() -> None:
    """The positive case, and it is load-bearing rather than decorative: `inference_gaps` is
    called by `brain.ops.worker.preflight`, so anything it reports stops every worker in the
    estate from starting. A check that refuses the deployment we actually declare is a check
    somebody deletes."""
    assert inference_gaps() == ()


def test_a_container_that_cannot_hold_its_own_weights_is_refused() -> None:
    """The check that fires when a fourth model is added or one moves to fp32. The container
    then dies during startup with three models half loaded rather than under load, which on a
    shared host is a neighbour's outage.

    Asked through a parameter rather than by editing the constant, so the refusal can be seen
    to fire; a check nobody has watched fail is a check nobody knows works."""
    findings = inference_gaps(reserve_mib=4096)

    assert any("cannot load what it is meant to serve" in f for f in findings)


def test_serving_more_than_one_request_at_a_time_is_refused() -> None:
    """The ceiling is the whole remainder, so two concurrent requests is twice the
    container's memory promised out of one container's worth. `PARSES_AT_ONCE` makes the
    identical argument about the parse worker."""
    findings = inference_gaps(requests_at_once=4)

    assert any("would serve 4 requests at once" in f for f in findings)


def test_a_server_that_answers_no_requests_at_all_is_refused() -> None:
    """Zero is not a smaller number of requests, it is a service that presents a listening
    socket and answers nothing, which every metric reads as a queue that is merely slow."""
    findings = inference_gaps(requests_at_once=0)

    assert any("answers nothing" in f for f in findings)


def test_a_task_nothing_is_declared_to_serve_is_reported_as_a_deployment_gap() -> None:
    """The failure with no error in it. A task with no model behind it means the caller waits,
    the call times out, and the outcome is `UNAVAILABLE`, so a deployment that is missing a
    model looks exactly like one that is merely down and the operator goes looking at the
    wrong thing.

    Driven through the `models` parameter, because the declared table is complete and a check
    that can only be run against complete data cannot be shown to fire.

    Delete this and the branch can be removed with every other deployment test here green,
    since none of the others makes it fire."""
    findings = inference_gaps(models=(SERVED_MODELS[0],))

    assert any("no model is declared" in f for f in findings), findings


def test_a_batch_larger_than_the_server_will_accept_is_refused_before_anybody_sends_one() -> None:
    """The two-ended check, seen failing. A ceiling below the batch budget means every full
    batch is refused after the planner has built it.

    Driven by shrinking the container rather than by growing the batch, because the batch
    budget belongs to `brain.ops.queue` and this file has no business reaching into it."""
    findings = inference_gaps(reserve_mib=555)

    assert any("after the planner had built it" in f for f in findings)


def test_the_inference_server_holds_no_connection_string_to_our_database() -> None:
    """**The boundary as a declaration rather than as a sentence.** The service is handed text
    that has already passed the permission layer; if it also held a connection string it
    could read a row nobody sent it, and the permission layer would be describing a boundary
    with a door beside it.

    Both directions, because a check over real data that happens to be clean passes whether
    or not it works: that is the failure
    `test_the_pooler_check_actually_looks_at_the_declaration` exists for. The check is shown
    firing by asking it about a component that really is wired to our database, which
    `brain-worker` is, rather than by patching the declaration into a shape nothing deploys.

    Delete this and `Wiring.NONE` can be changed here to fix some connectivity problem, and
    the only thing left saying the service must not reach the database is a comment."""
    assert component(INFERENCE_COMPONENT).wiring is Wiring.NONE

    findings = inference_gaps(server_component="brain-worker")

    assert any("is wired 'direct'" in f for f in findings), findings


# ------------------------------------------------------------------ the profile flag refuses
def test_lite_deploys_no_inference_server_and_standard_does() -> None:
    """The flag's meaning in both directions. A test that only checks lite is satisfied by a
    function returning False for everything, which would then also refuse the address on the
    profile that exists to have one."""
    assert runs_inference_server("lite") is False
    assert runs_inference_server("standard") is True
    assert runs_inference_server("full") is True


def test_a_lite_install_pointed_at_an_inference_server_is_refused() -> None:
    """**A heavier version of the trace conflict and the reason it is worth checking
    separately.** A stray `LANGFUSE_HOST` ships spans, which are metadata about a request. A
    stray inference address ships the text of the client's own documents to a host nobody
    here chose, and the far side answers, so nothing about it looks broken.

    Delete this and `profile=lite` goes back to being a word that selects no components while
    the process keeps posting documents somewhere."""
    conflicts = inference_config_conflicts("lite", {"inference_url": "http://192.0.2.1:8080"})

    assert len(conflicts) == 1
    assert "inference_url" in conflicts[0]
    assert "text of this client's documents" in conflicts[0]


def test_every_inference_destination_setting_is_checked() -> None:
    """Parametrised from the tuple itself, so a second setting arrives already covered. The
    trace check learned this the hard way: a key without a host is still a live destination,
    because the client library has a default host."""
    values = dict.fromkeys(INFERENCE_DESTINATION_SETTINGS, "set")

    assert len(inference_config_conflicts("lite", values)) == len(INFERENCE_DESTINATION_SETTINGS)


def test_a_profile_that_deploys_an_inference_server_may_be_pointed_at_one() -> None:
    """The positive case. A guard tested only by its refusals is satisfied by a function that
    refuses everything, and that function would make the standard profile unconfigurable
    while every refusal test here stayed green."""
    assert inference_config_conflicts("standard", {"inference_url": "http://inference:8080"}) == ()


def test_startup_refuses_an_install_that_would_ship_documents_to_a_server_it_does_not_run() -> None:
    """**`inference_config_conflicts` is a mechanism and this is its call site**, which is the
    part a tidy-up removes and the part nothing else here would notice the loss of.

    It has to be `brain.config.check` rather than anywhere later, because that runs before the
    port is bound: after that the process is in rotation and every document it ingests has
    already gone somewhere. The problem is reported against `profile` rather than against the
    address, matching the trace conflict beside it, because the address is not wrong in
    itself; it is wrong on this profile.

    Delete this and the loop can be taken out of `check` with every conflict test in this file
    still green."""
    base = {"database_url": "postgresql://u:p@db/brain", "valkey_url": "redis://cache:6379/0"}

    assert check("production", {**base, "profile": "lite"}) == []

    problems = check("production", {**base, "profile": "lite", "inference_url": "http://x:8080"})

    assert [p.setting for p in problems] == ["profile"]
    assert "inference_url" in problems[0].problem


def test_the_worker_preflight_asks_what_the_inference_server_will_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`inference_gaps` is a mechanism and this is its call site.** The most common defect in
    this repository is a correct, tested, documented check that nothing invokes, and it
    happens exactly this way: the check is written beside the module it belongs to and the
    wiring is left for later.

    Asked of every worker rather than of one, for the reason `embed_batch_gaps` is: a slot is
    a slot, embedding work is `SYSTEM`, and either container may be handed a batch to send.

    The finding is substituted rather than provoked through the environment, matching
    `test_the_worker_preflight_asks_whether_a_batch_would_fit`: this check reads constants
    rather than variables, so there is no value an operator could put in a compose file that
    makes it fire. What is asserted is the call.

    Delete this and the call can be taken out of `preflight` with every arithmetic test in
    this file still green."""
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
        worker_module, "inference_gaps", lambda: ("the inference server would refuse this",)
    )

    assert "the inference server would refuse this" in preflight(environment)


def test_a_lite_install_with_no_inference_address_is_clean() -> None:
    """The other positive case, and the one that runs in production today. Whitespace counts
    as unset, matching the trace check, because an environment file with `BRAIN_INFERENCE_URL=`
    in it produces an empty string rather than an absent key."""
    assert inference_config_conflicts("lite", {}) == ()
    assert inference_config_conflicts("lite", {"inference_url": "   "}) == ()
