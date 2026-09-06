"""When a guess may be indexed, and the one thing this path is never allowed to say.

Two failures are tested here and they pull in opposite directions. A reading too poor to be
worth anything, indexed, is a corpus of plausible strings that answer questions with a citation
pointing at a real page. A reading refused is a document nobody can find. The floor is where
that trade is made, so most of these tests are about the floor being applied at the value it is
set to rather than at whatever value it holds.

The confidences here are literals written in this file. A test that compared an answer against
`OCR_FLOOR_CONFIDENCE` while importing it from the module under test would be green for every
figure the floor could hold, which CLAUDE.md records happening three times in one afternoon.

Task ids: M7.2.4
"""

from __future__ import annotations

import dataclasses

import pytest

from brain.knowledge.chunking import Block, BlockKind
from brain.knowledge.ingest import (
    CAUSE_TEXT,
    AdmittedUpload,
    IngestRefused,
    MediaType,
    ParseCause,
    ParseFailure,
    ScanVerdict,
    admit_upload,
)
from brain.knowledge.parse_layout import LAYOUT_ENGINE
from brain.knowledge.parse_ocr import (
    OCR_ENGINE,
    OCR_FLOOR_CONFIDENCE,
    OCR_TASK,
    OcrParser,
    OcrReading,
    ocr_gaps,
    ocr_provenance,
)
from brain.knowledge.parse_paths import ParsePath
from brain.knowledge.scanning import (
    ParsedDocument,
    ParseRefusal,
    ParseStage,
    ScannedContent,
    ScanReport,
    parse_scanned,
    scan_for_parsing,
)
from brain.ops.inference import SERVED_MODELS, InferenceTask, ServedModel, served_model

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"


def _admitted(content: bytes = PDF, filename: str = "invoice.pdf") -> AdmittedUpload:
    return admit_upload(filename=filename, declared_type=MediaType.PDF.value, content=content)


class _CleanScanner:
    def scan(self, content: bytes) -> ScanReport:
        return ScanReport(verdict=ScanVerdict.CLEAN, scanner="fake-av")


def _cleared(content: bytes = PDF) -> ScannedContent:
    return scan_for_parsing(_admitted(content), content, scanner=_CleanScanner())


def _block(text: str = "Total due 1,000.00") -> Block:
    return Block(kind=BlockKind.PROSE, text=text, start=0)


class FakeEngine:
    """Hands back whatever the test scripted, and counts whether it was asked."""

    def __init__(self, outcome: OcrReading | ParseRefusal) -> None:
        self.outcome = outcome
        self.calls = 0

    def read(self, content: ScannedContent) -> OcrReading | ParseRefusal:
        self.calls += 1
        return self.outcome


# ------------------------------------------------------------------ the reading itself
@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -1.0])
def test_a_reading_reported_in_units_nobody_agreed_is_refused(confidence: float) -> None:
    """The floor is a fraction, so an engine reporting a percentage would compare 87 against
    0.55 and pass everything it ever produced.

    Delete this and an engine that reports 0 to 100, which several real ones do, silently
    disables the only guard on this path."""
    with pytest.raises(IngestRefused, match="which is not one"):
        OcrReading(blocks=(_block(),), confidence=confidence)


@pytest.mark.parametrize(
    ("confidence", "legible"),
    [(1.0, True), (0.95, True), (0.7, True), (0.56, True), (0.2, False), (0.0, False)],
)
def test_a_reading_is_legible_at_the_confidences_this_project_decided_on(
    confidence: float, legible: bool
) -> None:
    """The floor asserted against numbers written here rather than against the constant. Every
    row is a figure a real engine could report, and the pair either side of the boundary is
    what pins the constant to a value rather than to a relation.

    Delete this and the floor can be moved to 0.0, which indexes every scan however garbled, or
    to 1.0, which refuses every scan and reads as an engine that does not work."""
    assert OcrReading(blocks=(_block(),), confidence=confidence).is_legible is legible


def test_the_floor_sits_inside_the_range_where_it_can_do_anything_at_all() -> None:
    """The relation behind the figure, beside the table above. A floor of zero admits noise and
    a floor of one refuses a perfect read, and both are values the constant could hold while
    every other test in this file that used it would still pass.

    Delete this and the two degenerate settings stop being named anywhere."""
    assert 0.0 < OCR_FLOOR_CONFIDENCE < 1.0


def test_a_reading_carries_no_field_a_line_of_the_document_could_travel_in() -> None:
    """Read off the dataclass, matching `test_scanning.py`'s reading of `ParseRefusal`. An OCR
    engine returns vendor prose about what it thought it saw, and a parse result is rendered
    into logs and console rows that travel further than the document's scope.

    Delete this and a `detail` or `raw_text` field is added to help with debugging, and the
    recognised text of somebody's payslip ends up in an operator log."""
    assert {field.name for field in dataclasses.fields(OcrReading)} == {"blocks", "confidence"}


# ------------------------------------------------------------------ what the parser does
def test_an_ocr_parser_with_no_engine_says_the_file_has_not_been_read_yet() -> None:
    """The honest state of M7.2.4: no OCR engine is a dependency of this project and none is
    served. `PARSER_UNAVAILABLE` is retryable, so a scanned document parked on it is re-driven
    when an engine exists rather than being marked dead.

    Delete this and the seam can start returning nothing instead, which `parse_scanned` turns
    into `NO_TEXT_LAYER`: the cause whose remedy is the path that just refused."""
    outcome = OcrParser().parse(_cleared())

    assert outcome == ParseRefusal(cause=ParseCause.PARSER_UNAVAILABLE, stage=ParseStage.OCR)


def test_a_reading_the_engine_was_sure_of_is_returned_as_blocks() -> None:
    """The positive case. A path tested only by its refusals is satisfied by one that refuses
    everything, which is what this path does today, so without this nothing separates "not
    built" from "built and refusing everything"."""
    block = _block()
    engine = FakeEngine(OcrReading(blocks=(block,), confidence=0.95))

    outcome = OcrParser(engine=engine).parse(_cleared())

    assert not isinstance(outcome, ParseRefusal)
    assert outcome == (block,)
    assert outcome[0] is block, "the block was rebuilt, and a rebuild loses the page number"


def test_a_reading_the_engine_was_unsure_of_is_refused_rather_than_indexed() -> None:
    """The trade this leaf makes. A garbled invoice indexed is a wrong figure in an answer with
    a citation pointing at a real page, and neither retrieval leg can tell: a tsvector over
    noise is a valid tsvector and an embedding of noise is a valid vector.

    Delete this and the confidence is computed, compared against nothing, and every reading is
    indexed however poor."""
    engine = FakeEngine(OcrReading(blocks=(_block(),), confidence=0.2))

    outcome = OcrParser(engine=engine).parse(_cleared())

    assert outcome == ParseRefusal(cause=ParseCause.ILLEGIBLE, stage=ParseStage.OCR)


def test_this_path_never_tells_somebody_their_file_needs_this_path() -> None:
    """`CAUSE_TEXT[NO_TEXT_LAYER]` says the file needs the scanned-document path rather than a
    re-upload, which is a loop coming from the scanned-document path. Both routes to it are
    closed: an engine returning the cause, and an engine returning nothing so that
    `parse_scanned` names it.

    Delete this and a scanned invoice that OCR could not read tells its uploader to send it
    through OCR, for ever."""
    from_engine = OcrParser(
        engine=FakeEngine(ParseRefusal(cause=ParseCause.NO_TEXT_LAYER, stage=ParseStage.OCR))
    ).parse(_cleared())
    from_nothing = OcrParser(engine=FakeEngine(OcrReading(blocks=(), confidence=1.0))).parse(
        _cleared()
    )

    assert from_engine == ParseRefusal(cause=ParseCause.ILLEGIBLE, stage=ParseStage.OCR)
    assert from_nothing == ParseRefusal(cause=ParseCause.ILLEGIBLE, stage=ParseStage.OCR)


def test_an_empty_reading_does_not_reach_the_gate_that_would_name_it_a_missing_text_layer() -> None:
    """The same rule proved through `parse_scanned` rather than against the parser alone,
    because `parse_scanned` is what would otherwise produce the wrong cause and it is shared
    with every other parser.

    Delete this and the substitution can be removed from `OcrParser` and the unit test above
    rewritten to match, with nothing checking the two together."""
    outcome = parse_scanned(
        _cleared(), parser=OcrParser(engine=FakeEngine(OcrReading(blocks=(), confidence=1.0)))
    )

    assert isinstance(outcome, ParseFailure)
    cause = outcome.cause

    assert cause is not ParseCause.NO_TEXT_LAYER, "the gate named the cause this path is"
    assert cause is ParseCause.ILLEGIBLE


def test_a_refusal_only_the_engine_could_have_reached_is_passed_on_unchanged() -> None:
    """An engine that opened the image knows things this side does not: that the file is
    encrypted, that it is damaged. Rewording those would be this module inventing a diagnosis
    from a value it did not compute.

    Delete this and every engine refusal collapses to `ILLEGIBLE`, which tells somebody with a
    password-protected file to send a clearer scan."""
    refusal = ParseRefusal(cause=ParseCause.ENCRYPTED, stage=ParseStage.OPEN)

    assert OcrParser(engine=FakeEngine(refusal)).parse(_cleared()) == refusal


def test_an_ocr_parser_satisfies_the_gate_that_holds_the_scan_and_the_memory_bound() -> None:
    """`parse_scanned` is the only place `Parser.parse` may be called, and a path that did not
    satisfy that protocol would have to be reached some other way, which is the unscanned-buffer
    hole the type exists to close.

    Delete this and the seam can drift out of the contract, and the drift is found by whoever
    wires it up rather than here."""
    block = _block()
    outcome = parse_scanned(
        _cleared(), parser=OcrParser(engine=FakeEngine(OcrReading(blocks=(block,), confidence=0.9)))
    )

    assert isinstance(outcome, ParsedDocument)
    assert outcome.blocks == (block,)


def test_a_seam_that_holds_no_state_gives_two_documents_the_same_answer() -> None:
    """`OcrParser` is frozen and keeps nothing between calls. Delete this and a parser that
    cached the last reading would make one document's result depend on another's, which on a
    queue is a result nobody can reproduce."""
    parser = OcrParser()

    assert parser.parse(_cleared()) == parser.parse(_cleared(PDF + b"different"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        parser.engine = None  # type: ignore[misc]


# ---------------------------------------------------- the cause this path needed and added
def test_the_cause_this_path_produces_asks_for_a_different_file_and_not_a_different_action() -> (
    None
):
    """`ILLEGIBLE` was added to the taxonomy for this leaf, and it earns its place only if its
    remedy differs from every other member's: `ParseCause`'s own docstring says two causes with
    the same remedy are one cause with two names.

    Delete this and the wording can drift into a copy of `NO_TEXT_LAYER`'s, at which point the
    substitution above achieves nothing while every test of the substitution stays green."""
    illegible = CAUSE_TEXT[ParseCause.ILLEGIBLE]

    assert illegible != CAUSE_TEXT[ParseCause.NO_TEXT_LAYER]
    assert "clearer scan" in illegible
    assert "scanned-document path" not in illegible


def test_a_file_nobody_could_read_is_not_put_back_on_the_queue_for_ever() -> None:
    """`is_retryable` is computed in `brain.knowledge.ingest` from a set this module does not
    write, and `ILLEGIBLE` is a fact about the document rather than about this system: sending
    the same bytes again produces the same reading at the same cost.

    Delete this and adding the cause to the retryable set goes unnoticed, and the queue fills
    with scans that cannot ever succeed while crowding out the uploads that would."""
    failure = ParseFailure(cause=ParseCause.ILLEGIBLE, media_type=MediaType.PDF)

    assert failure.is_retryable is False
    assert ParseFailure(cause=ParseCause.PARSER_UNAVAILABLE, media_type=MediaType.PDF).is_retryable


# ------------------------------------------------------------------ what would serve this
def test_a_recognised_passage_is_recorded_as_a_guess_and_not_as_a_reading() -> None:
    """The path is the whole of what travels out of here, so it has to be the guessing one and
    the engine has to be distinguishable from the layout engine even though one model serves
    both tasks.

    Delete this and an OCR passage can be recorded under the layout engine's name, which files a
    guess as a reading and cannot be undone from the corpus afterwards."""
    provenance = ocr_provenance()

    assert provenance.path is ParsePath.OCR
    assert provenance.engine == OCR_ENGINE
    assert len({OCR_ENGINE, LAYOUT_ENGINE}) == 2, "one name serves both paths"


def test_the_task_this_path_would_use_is_one_the_inference_server_actually_serves() -> None:
    """Asked of `brain.ops.inference` rather than asserted about a constant here, so the answer
    comes from another package. Delete this and `OCR_TASK` can name a task with no model behind
    it, which presents as a server that is merely slow and then not there at all."""
    assert OCR_TASK is InferenceTask.PARSING
    assert served_model(OCR_TASK).task is OCR_TASK


def test_the_deployment_is_reported_as_sized_from_a_figure_nobody_measured() -> None:
    """The finding this leaf adds to the deployment record. The parsing model's weights figure
    is a judgement written for layout and table models, with no recogniser in its basis, and the
    container was sized from it.

    Delete this and the OCR path reads as deployable on a container nobody checked could hold
    it."""
    findings = ocr_gaps()

    assert any(model.task is OCR_TASK and not model.measured for model in SERVED_MODELS)
    assert len(findings) == 1
    assert "judgement rather than a measurement" in findings[0]


def test_a_measured_model_reports_no_gap_and_an_absent_one_reports_a_different_gap() -> None:
    """Both other branches, which are unreachable against what is deployed today. Without the
    parameter and this test, `ocr_gaps` could return its one finding unconditionally and read
    exactly the same on this deployment.

    Delete this and the check stops being a check: it becomes a sentence printed whenever it is
    called."""
    measured = ServedModel(
        task=OCR_TASK,
        name="measured-recogniser",
        weights_mib=512,
        sizing_basis="measured on the host",
        measured=True,
    )
    other = ServedModel(
        task=InferenceTask.EMBEDDING,
        name="an-embedding-model",
        weights_mib=8,
        sizing_basis="a figure for a test",
    )

    assert ocr_gaps([measured]) == ()
    assert len(ocr_gaps([other])) == 1
    assert "no model serves" in ocr_gaps([other])[0]
