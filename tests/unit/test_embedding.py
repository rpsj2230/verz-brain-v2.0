"""The model recorded beside a vector, the refusal to mix two, and the resumable rebuild.

Task ids: M7.3.5
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from brain.knowledge.embedding import (
    DEFAULT_BATCH_SIZE,
    EMBEDDING_FIELD,
    EXIT_INCOMPLETE,
    MODEL_IDENTITY_CHARS,
    REBUILD_COMMAND,
    EmbeddedVector,
    EmbeddingError,
    EmbeddingModel,
    MixedEmbeddingError,
    RebuildAction,
    RebuildCursor,
    RebuildPlan,
    _bounded,
    assert_comparable,
    completion_gaps,
    corpus_identity,
    main,
    next_batch,
    progress_note,
    vector_leg_is_available,
)
from brain.knowledge.search import (
    CHUNK,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_FIELD,
    INDEXABLE_DIMENSION_CEILING,
)

MODEL = EmbeddingModel(name="qwen3-embedding", revision="a1b2c3d", dimensions=EMBEDDING_DIMENSIONS)
NEWER = EmbeddingModel(name="qwen3-embedding", revision="e4f5g6h", dimensions=EMBEDDING_DIMENSIONS)


def _row(identity: str | None = MODEL.identity, *, embedded: bool = True) -> dict[str, object]:
    return {
        "chunk_id": "c_0001",
        EMBEDDING_FIELD: [0.0] * 4 if embedded else None,
        EMBEDDING_MODEL_FIELD: identity,
    }


# --------------------------------------------------------------- the model identity
def test_a_model_that_names_no_revision_is_refused() -> None:
    """A provider that updates weights behind a stable name produces a different vector space
    under an identical label, and that is the model change with no symptom at all. Deleting
    this lets a corpus be stamped with an identity that cannot tell the two apart."""
    with pytest.raises(EmbeddingError, match="names no revision"):
        EmbeddingModel(name="qwen3-embedding", revision="", dimensions=EMBEDDING_DIMENSIONS)


def test_two_revisions_of_one_name_are_two_identities() -> None:
    """The whole point of carrying the revision. If they compared equal, a rebuild onto new
    weights would report the corpus as already converted and quietly leave every vector in
    the old space."""
    assert MODEL.name == NEWER.name
    assert MODEL.identity != NEWER.identity


def test_the_same_model_at_two_widths_are_two_identities() -> None:
    """A truncated Matryoshka embedding is a different space from the full one, and the same
    weights at two widths would otherwise share an identity and be compared against each
    other."""
    narrow = EmbeddingModel(name="qwen3-embedding", revision="a1b2c3d", dimensions=1024)
    assert narrow.identity != MODEL.identity
    assert not narrow.fits_the_column


def test_a_model_wider_than_pgvector_will_index_is_refused_before_anything_runs() -> None:
    """The asymmetry that has a corpus to re-embed by the time it is discovered: every row
    inserts and `CREATE INDEX` is what fails. Deleting this moves the failure to the end of a
    rebuild instead of the start of one."""
    with pytest.raises(EmbeddingError, match="indexes at most"):
        EmbeddingModel(name="huge", revision="v1", dimensions=INDEXABLE_DIMENSION_CEILING + 1)


def test_a_name_outside_the_grammar_is_refused() -> None:
    """The identity ends up in a bounded column and in an operator's command line. A name
    with a space or a newline in it would be stored truncated or quoted differently by two
    callers, and an identity that compares unequal to itself makes every vector unusable."""
    with pytest.raises(EmbeddingError, match="not an embedding model name"):
        EmbeddingModel(name="Qwen 3 Embedding", revision="v1", dimensions=EMBEDDING_DIMENSIONS)


def test_the_grammar_cannot_produce_an_identity_the_column_cannot_hold() -> None:
    """Checked at import from the patterns' own behaviour rather than from a comment, because
    a regex mangled on its way into the file matches nothing or everything and neither
    announces itself. Deleting this lets a widened name pattern truncate identities in the
    database, where they compare unequal to themselves."""
    widest = EmbeddingModel(
        name="q" * 64, revision="r" * 40, dimensions=INDEXABLE_DIMENSION_CEILING
    )
    assert len(widest.identity) <= MODEL_IDENTITY_CHARS


def test_a_pattern_that_does_not_admit_the_length_it_claims_cannot_be_imported() -> None:
    """The bound above is computed from the patterns' own behaviour, so a regex mangled on
    its way into the file is an import failure rather than a silent change of grammar. Delete
    this and a pattern matching nothing, or matching far more than the column holds, ships
    with the arithmetic that was true of the pattern it used to be."""
    with pytest.raises(EmbeddingError, match="does not admit exactly"):
        _bounded(re.compile(r"^a{0,3}$"), 64)
    assert _bounded(re.compile(r"^a{0,3}$"), 3) == 3


def test_a_vector_of_the_wrong_width_is_not_from_the_model_it_claims() -> None:
    """A provider response that came back short is a broken call rather than a new model, and
    storing it would put numbers of one length under an identity meaning another."""
    with pytest.raises(EmbeddingError, match="dimensions rather than"):
        EmbeddedVector(model=MODEL, values=(0.0, 1.0))


def test_a_vector_carries_the_model_that_produced_it() -> None:
    """The structural half of the mixing rule, and the positive case beside the refusal. A
    bare list of floats can be compared with anything and nothing in the expression says
    which spaces they came from."""
    vector = EmbeddedVector(model=MODEL, values=tuple(0.0 for _ in range(MODEL.dimensions)))
    assert vector.model.identity == MODEL.identity


# ------------------------------------------------------------- not mixing two models
def test_a_question_and_a_corpus_from_different_models_are_never_compared() -> None:
    """A distance between two vector spaces is a number rather than a distance, and it ranks
    passages in an order that means nothing. Nothing downstream can detect it, because the
    query succeeds and returns the right number of rows."""
    with pytest.raises(MixedEmbeddingError, match="two vector spaces"):
        assert_comparable(NEWER, MODEL.identity)
    assert_comparable(MODEL, MODEL.identity)


def test_a_corpus_holding_two_models_is_refused_rather_than_filtered() -> None:
    """Filtering to one model returns half the candidates, and a short vector leg is exactly
    what iterative scan produces at its scan bound, so the two are indistinguishable to
    everything above. Deleting this trades a visible loss of recall for a silent one."""
    rows = [_row(MODEL.identity), _row(NEWER.identity)]
    with pytest.raises(MixedEmbeddingError, match="the corpus holds vectors from"):
        corpus_identity(rows)


def test_a_vector_with_no_model_recorded_beside_it_is_refused() -> None:
    """This is the corpus as it stands before the identity column exists: vectors produced by
    something, with nothing recording what. Treating them as belonging to the current model
    would be an assertion nobody is in a position to make."""
    with pytest.raises(MixedEmbeddingError, match="no model recorded"):
        corpus_identity([_row(None)])


def test_a_corpus_with_no_vectors_at_all_is_not_a_mixed_corpus() -> None:
    """Embedding is asynchronous, so unembedded chunks are the ordinary state during
    ingestion. Refusing them would turn every fresh upload into a retrieval outage."""
    assert corpus_identity([_row(None, embedded=False)]) == ""
    assert corpus_identity([]) == ""
    assert_comparable(MODEL, "")


def test_a_row_whose_vector_sits_under_another_key_is_read_as_unembedded() -> None:
    """Why the column name is taken from the table rather than typed out here. A key spelled
    differently reads every row as unembedded, which fails closed into "the vector leg
    returned nothing", and that silent degradation is the failure this module exists for."""
    assert corpus_identity([{"vector": [0.0], EMBEDDING_MODEL_FIELD: MODEL.identity}]) == ""
    assert corpus_identity([_row()]) == MODEL.identity
    assert EMBEDDING_FIELD in CHUNK.c


def test_the_vector_leg_is_off_from_the_moment_a_rebuild_is_planned() -> None:
    """In between, the corpus holds two models in one index and no query can tell them apart.
    The lexical leg is unaffected and fusion takes a missing list in its stride, so retrieval
    degrades to text search rather than to nonsense."""
    plan = RebuildPlan(to_model=NEWER, from_identity=MODEL.identity)
    assert not vector_leg_is_available(cursor=plan.start())
    assert vector_leg_is_available(cursor=None)


def test_the_vector_leg_returns_when_the_rebuild_finishes() -> None:
    """A leg switched off and never switched back on is an outage nobody declared. The
    positive case beside the refusal, and the reason the cursor carries `exhausted` rather
    than the caller remembering."""
    finished = RebuildCursor(model=NEWER).advance(last_chunk_id="c_9", written=1, exhausted=True)
    assert vector_leg_is_available(cursor=finished)


# ----------------------------------------------------------------- the rebuild
def test_a_rebuild_onto_a_width_the_column_cannot_hold_is_refused_before_the_first_batch() -> None:
    """The width is part of the column type, so this is a migration before it is a rebuild.
    Found at the first write instead, an afternoon of embedding has already been spent."""
    narrow = EmbeddingModel(name="qwen3-embedding", revision="a1b2c3d", dimensions=1024)
    with pytest.raises(EmbeddingError, match="migration before it is a rebuild"):
        RebuildPlan(to_model=narrow)


def test_a_rebuild_onto_the_model_the_corpus_already_holds_is_refused() -> None:
    """It re-embeds everything and changes nothing, which is the shape a mistyped revision
    takes and the shape of a rebuild run twice by two people during an incident."""
    with pytest.raises(EmbeddingError, match="already on"):
        RebuildPlan(to_model=MODEL, from_identity=MODEL.identity)


def test_a_corpus_with_no_recorded_model_is_a_legitimate_first_rebuild() -> None:
    """Vectors written before anything recorded a model have to be redone, because nothing
    can say what produced them. Refusing an empty starting identity would make the corpus
    that most needs a rebuild the one that cannot have one."""
    plan = RebuildPlan(to_model=MODEL, from_identity="")
    assert next_batch(plan=plan, cursor=plan.start()).action is RebuildAction.EMBED


def test_a_rebuild_resumed_against_a_different_model_is_refused() -> None:
    """The way a third model gets into the corpus: interrupted while rewriting to B, resumed
    towards C, and rows are then left on all three with nothing reporting it. Deleting this
    makes a typo on resumption permanent and invisible."""
    plan = RebuildPlan(to_model=NEWER, from_identity=MODEL.identity)
    with pytest.raises(EmbeddingError, match="carrying on would leave the corpus holding"):
        next_batch(plan=plan, cursor=RebuildCursor(model=MODEL))


def test_a_position_that_does_not_move_forward_is_refused() -> None:
    """A cursor that went backwards or stood still re-embeds the same prefix on every batch
    and never reaches the end, spending GPU hours and reporting progress the whole time. Our
    key is ordered, so this is checkable in a way a connector's opaque token is not."""
    cursor = RebuildCursor(model=MODEL, after_chunk_id="c_0500")
    with pytest.raises(EmbeddingError, match="not forward"):
        cursor.advance(last_chunk_id="c_0400", written=10, exhausted=False)
    with pytest.raises(EmbeddingError, match="not forward"):
        cursor.advance(last_chunk_id="c_0500", written=10, exhausted=False)


def test_a_batch_that_wrote_chunks_must_name_the_last_of_them() -> None:
    """Otherwise the position stands still while the counters move, which reads in a progress
    log as work being done and leaves the rebuild looping over one window."""
    with pytest.raises(EmbeddingError, match="named none of them"):
        RebuildCursor(model=MODEL).advance(last_chunk_id="", written=10, exhausted=False)


def test_a_finished_rebuild_cannot_be_advanced_again() -> None:
    """Advancing past the end starts the job from the beginning, and the second run embeds
    the whole corpus a second time. The same refusal `BackfillCursor.advance` makes, for the
    same reason and against a larger bill."""
    finished = RebuildCursor(model=MODEL, exhausted=True)
    with pytest.raises(EmbeddingError, match="already finished"):
        finished.advance(last_chunk_id="c_9999", written=1, exhausted=True)


def test_a_resumed_rebuild_is_indistinguishable_from_one_that_never_stopped() -> None:
    """The property the whole cursor exists for. A rebuild that restarted after failing three
    quarters of the way through would re-embed everything it had already done, and this is a
    job over every document the company has."""
    plan = RebuildPlan(to_model=NEWER, from_identity=MODEL.identity)
    running = plan.start().advance(last_chunk_id="c_0200", written=200, exhausted=False)
    persisted = RebuildCursor(model=NEWER, after_chunk_id="c_0200", batches=1, chunks=200)
    assert persisted == running
    assert next_batch(plan=plan, cursor=persisted) == next_batch(plan=plan, cursor=running)


def test_the_next_batch_covers_only_what_comes_after_the_position() -> None:
    """The keyset window, which is what makes the job resumable at all. An offset would
    re-read everything before it and skip rows whenever the set shifted underneath, which
    during a rebuild it does on every batch."""
    plan = RebuildPlan(to_model=NEWER, from_identity=MODEL.identity, batch_size=50)
    cursor = plan.start().advance(last_chunk_id="c_0200", written=50, exhausted=False)
    batch = next_batch(plan=plan, cursor=cursor)
    assert batch.after_chunk_id == "c_0200"
    assert batch.size == 50
    assert not batch.is_finished


def test_an_exhausted_cursor_reports_done_and_says_what_was_rebuilt() -> None:
    """The positive end of the job. Without it a caller has no way to tell a finished rebuild
    from one waiting for its next batch, and the vector leg would stay switched off."""
    plan = RebuildPlan(to_model=NEWER, from_identity=MODEL.identity)
    done = next_batch(
        plan=plan,
        cursor=plan.start().advance(last_chunk_id="c_9", written=7, exhausted=True),
    )
    assert done.is_finished
    assert "7 chunk(s)" in done.reason


def test_the_resume_hint_carries_the_model_as_well_as_the_position() -> None:
    """The interesting way to resume a rebuild wrongly is to remember where it got to and
    forget what it was going to. A hint naming only the position invites exactly that."""
    cursor = RebuildCursor(model=NEWER).advance(last_chunk_id="c_0200", written=1, exhausted=False)
    hint = cursor.resume_hint()
    assert REBUILD_COMMAND in hint
    assert NEWER.revision in hint
    assert "--after c_0200" in hint


def test_a_rebuild_that_has_not_started_offers_no_position_to_resume_from() -> None:
    """An empty position pasted back as `--after ''` is a command that either fails or, worse,
    is silently accepted as a chunk id no row has, which restarts the job while looking like
    a resumption."""
    assert "--after" not in RebuildCursor(model=MODEL).resume_hint()


# ------------------------------------------------- progress and completion (M7.3.5)
def test_the_resume_hint_carries_the_counters_as_well_as_the_position() -> None:
    """The position alone is enough to finish the work and not enough to say the work was
    finished. A hint without the counters resumes correctly and starts the totals at zero, so
    every interrupted rebuild reports the size of its last segment as its total and passes a
    completion check it should fail.

    Delete this and the counters can be dropped from the hint as noise, which quietly turns
    `completion_gaps` into a check that only ever sees runs that were never interrupted."""
    cursor = RebuildCursor(model=NEWER, after_chunk_id="c_0200", batches=1, chunks=200)
    hint = cursor.resume_hint()

    assert "--chunks 200" in hint
    assert "--batches 1" in hint
    assert main(hint.removeprefix(REBUILD_COMMAND).split()) == 0


def test_a_finished_rebuild_that_wrote_fewer_chunks_than_the_corpus_is_refused() -> None:
    """The only evidence a rebuild that skipped rows ever leaves. A scan run through a narrowed
    reach, an offset cursor, or a resumed run given a retyped position all report success and
    all leave chunks on the old model indexed beside the new ones. None of the three raises.

    Delete this and a rebuild can report finished having covered a fraction of the corpus, and
    the vector leg is switched back on over a corpus holding two models."""
    cursor = RebuildCursor(model=NEWER, after_chunk_id="c_0400", batches=2, chunks=400)
    findings = completion_gaps(cursor=replace(cursor, exhausted=True), corpus_chunks=12_000)

    assert any("400 of 12000" in finding for finding in findings), findings


def test_a_rebuild_that_wrote_more_chunks_than_the_corpus_holds_is_refused() -> None:
    """The count is the whole of the evidence, so a count that cannot be right has to be said
    out loud rather than rounded into a pass. Something embedded twice means the total no
    longer says anything about coverage, and the run may still have missed rows.

    Delete this and the check becomes a one-sided comparison, which is satisfied by any run
    that overshoots for any reason."""
    cursor = RebuildCursor(model=NEWER, after_chunk_id="c_9", batches=9, chunks=90, exhausted=True)
    findings = completion_gaps(cursor=cursor, corpus_chunks=50)

    assert any("more than once" in finding for finding in findings), findings


def test_a_cursor_exhausted_before_it_ran_a_batch_is_refused() -> None:
    """A cursor that says finished having run no batches claims a corpus nobody embedded, and
    an empty corpus and a rebuild that never started are the two states this tells apart.

    Delete this and a cursor persisted with the wrong flag reports a completed rebuild that did
    not happen, which is worse than a failed one because nobody runs it again."""
    findings = completion_gaps(cursor=RebuildCursor(model=NEWER, exhausted=True), corpus_chunks=0)

    assert any("having run no batches" in finding for finding in findings), findings


def test_a_rebuild_still_in_progress_makes_no_claim_to_check() -> None:
    """An unfinished rebuild is an ordinary state and not a fault, so it produces no findings.
    Reporting one would train whoever reads the output to skim it, which is exactly the habit
    the finished-and-incomplete case depends on them not having.

    Delete this and the two states collapse, and the message that matters arrives beside one
    that arrives on every batch."""
    cursor = RebuildCursor(model=NEWER, after_chunk_id="c_0400", batches=2, chunks=400)

    assert completion_gaps(cursor=cursor, corpus_chunks=12_000) == ()


def test_a_rebuild_that_reached_every_chunk_is_complete() -> None:
    """The positive sibling. A completion check tested only by its refusals is satisfied by one
    that refuses every rebuild, and a rebuild that can never be declared finished is a vector
    leg that is never switched back on."""
    cursor = RebuildCursor(
        model=NEWER, after_chunk_id="c_9999", batches=60, chunks=12_000, exhausted=True
    )

    assert completion_gaps(cursor=cursor, corpus_chunks=12_000) == ()


def test_the_progress_note_reports_the_position_the_counters_and_the_vector_leg() -> None:
    """Partial progress has to be readable, because this job runs over everything the company
    has uploaded and will be interrupted. The whole of the state is one value, so what an
    operator needs during an interruption is one line rather than three things reassembled out
    of a log at the point they are least able to.

    Delete this and the note can lose the position, which is the one field a resumption needs
    and the only one nothing else prints."""
    cursor = RebuildCursor(model=NEWER, after_chunk_id="c_0400", batches=2, chunks=400)
    note = progress_note(cursor, corpus_chunks=12_000)

    assert "400 of 12000" in note
    assert "c_0400" in note
    assert "in progress" in note
    assert "vector leg available: False" in note


def test_a_short_rebuild_claiming_to_be_finished_does_not_reopen_the_vector_leg() -> None:
    """The failure this module is written against, arriving through the line that reports it. A
    cursor that says finished having covered a fraction of the corpus would otherwise be read
    as sound, and the leg would be switched back on over a corpus holding two models, where
    every distance is a number rather than a distance.

    Delete this and the note contradicts the refusal printed beside it, and a reader has to
    decide which half to believe."""
    short = RebuildCursor(
        model=NEWER, after_chunk_id="c_0400", batches=2, chunks=400, exhausted=True
    )

    assert "vector leg available: False" in progress_note(short, corpus_chunks=12_000)
    assert "vector leg available: True" in progress_note(short, corpus_chunks=400)


def test_an_unstated_corpus_count_is_not_read_as_an_empty_corpus() -> None:
    """Zero is "nobody said", and a denominator we do not have must not be invented. Passing it
    through as a real count would report every finished run as having embedded more chunks than
    the corpus holds, which is a refusal that fires for every correct rebuild.

    Delete this and the default turns the completion check into noise, and noise is switched
    off rather than fixed."""
    finished = RebuildCursor(
        model=NEWER, after_chunk_id="c_9", batches=1, chunks=400, exhausted=True
    )

    assert "400 chunk(s)" in progress_note(finished)
    assert "of" not in progress_note(finished).split("chunk(s)")[0].split("batch(es),")[1]


# ------------------------------------------------------------------ the command
def test_the_command_plans_a_rebuild_and_says_how_to_resume_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model swapped underneath the corpus with nothing run at all is the failure this leaf
    is about. The command is what turns that from a configuration change into something
    somebody has to state, and the resumption line is what makes the long job survivable."""
    code = main(
        [
            "--to-model",
            NEWER.name,
            "--revision",
            NEWER.revision,
            "--dimensions",
            str(EMBEDDING_DIMENSIONS),
            "--from-identity",
            MODEL.identity,
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert NEWER.identity in printed
    assert REBUILD_COMMAND in printed
    assert str(DEFAULT_BATCH_SIZE) in printed
    assert "vector leg available: False" in printed


def test_the_command_refuses_a_model_the_column_cannot_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refusing loudly at the top of the afternoon is worth more than failing at the first
    write four hours in, which is where an unchecked width is otherwise discovered."""
    code = main(["--to-model", "qwen3-embedding", "--revision", "v9", "--dimensions", "1024"])
    assert code == 2
    assert "migration before it is a rebuild" in capsys.readouterr().err


def test_the_command_refuses_a_model_with_no_revision(capsys: pytest.CaptureFixture[str]) -> None:
    """The command line is where a revision is easiest to leave out, and a corpus stamped
    with a name alone cannot tell two sets of weights apart afterwards."""
    code = main(["--to-model", "qwen3-embedding", "--revision", "", "--dimensions", "1536"])
    assert code == 2
    assert "names no revision" in capsys.readouterr().err


def _finished(*extra: str) -> list[str]:
    return [
        "--to-model",
        NEWER.name,
        "--revision",
        NEWER.revision,
        "--dimensions",
        str(EMBEDDING_DIMENSIONS),
        "--after",
        "c_0400",
        "--chunks",
        "400",
        "--batches",
        "2",
        "--finished",
        *extra,
    ]


def test_the_command_refuses_a_finished_rebuild_that_did_not_reach_every_chunk(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`completion_gaps` is a mechanism and this is its call site. The most common defect in
    this repository is a correct, tested check that nothing invokes, and it arrives exactly this
    way: the check is written with the values it reasons about and the wiring is left for later.

    The exit code is its own rather than the refusal's, because the two need different actions:
    a refusal is a command retyped, and this is a corpus that has to be rebuilt from the start.

    Delete this and the call can be removed from `main` with every check above still green."""
    code = main(_finished("--corpus-chunks", "12000"))
    captured = capsys.readouterr()

    assert code == EXIT_INCOMPLETE
    assert "11600 are still on whatever model produced them" in captured.err
    assert "the vector leg is sound again" not in captured.out


def test_the_command_says_when_a_claim_to_have_finished_was_compared_with_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unchecked claim, said out loud. Without a count nothing was compared, and the
    difference between that and a checked completion is the whole of what stands between a
    rebuild that skipped rows and a corpus permanently holding two models.

    Delete this and a finished run with no count reads exactly like a verified one, which is
    the reading that leads to the vector leg being switched back on."""
    code = main(_finished())

    assert code == 0
    assert "unchecked" in capsys.readouterr().out


def test_the_command_accepts_a_rebuild_that_reached_every_chunk(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The positive sibling of the refusal above. A command tested only by what it rejects is
    satisfied by one that rejects every rebuild, and a rebuild that can never be declared
    finished leaves the vector leg off for ever."""
    code = main(_finished("--corpus-chunks", "400"))
    captured = capsys.readouterr()

    assert code == 0
    assert "the vector leg is sound again" in captured.out
    assert captured.err == ""
