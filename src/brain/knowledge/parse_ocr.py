"""The scanned-document path, where every character is a guess and has to stay one.

M7.2.4 is "OCR path for scanned documents", and the two other paths in this package read
characters a file already contains. This one does not. A photographed invoice has no text in
it at all; what comes back is a recogniser's opinion about shapes, and the difference between
`1,000` and `l,OOO` is a confidence score rather than an error. **A number read wrongly here
becomes a figure in an answer, with a citation pointing at a real page of a real document that
really does contain that number as an image.** Nothing downstream can catch it: the lexical leg
indexes the wrong string happily, the vector leg embeds it happily, and
`brain.gate.provenance.assert_derived` only checks that a citation came from the trace, not
that the passage says what the answer says.

**So the guard is a floor, and below it a reading is refused rather than indexed.** That is
the trade this module makes and it is a real one in both directions. Refusing means a document
somebody uploaded is not searchable, which is the silent failure the rest of this package
fights. Indexing means a garbled page that answers questions confidently, which is worse,
because "not found" is visible to the person asking and a wrong figure is not. See
`A_GUESS_BELOW_THE_FLOOR_IS_WORSE_THAN_NO_TEXT`.

**The confidence does not travel any further than this module, and that is a decision rather
than an omission.** `ParseProvenance` has no confidence field. A float from Tesseract, a float
from a neural recogniser and a float from a cloud API are three different quantities with one
name, and putting them in one column beside `page` and `section`, which mean the same thing
whoever produced them, would be a number an operator compares across documents that cannot be
compared. What does travel is `ParsePath.OCR`, and that is the fact a reader actually needs:
this passage was guessed. See `A_CONFIDENCE_HAS_NO_SCALE_ACROSS_ENGINES`.

**Whose job is it to stop a low-confidence read looking like a high-confidence one?** Split,
and the split is the answer to the question rather than a dodge. Refusing a reading too poor to
index is this module's, because it is the only place that ever sees the number. Telling a
reader that the passage they are looking at was recognised rather than read is the citation's,
because that is where a person meets the evidence, and it is **not built**: `know.chunk` has no
column for a path and `brain.gate.provenance.DocumentCitation` has no field to render one, so
today an OCR passage and a layout passage cite identically.
`brain.knowledge.parse_paths` names the two edits that would close it. Until one is made, the
floor is the whole of the protection, which is worth saying plainly because a floor alone
protects against a garbled document and not against a plausible misreading of one digit.

**OCR must never report that a file needs OCR.** `ParseCause.NO_TEXT_LAYER` is worded as
needing the scanned-document path rather than a re-upload, and this is that path. An engine
returning it, or returning nothing at all so that `parse_scanned` names it, would send somebody
to do the thing that has just been done. `ParseCause.ILLEGIBLE` was added to
`brain.knowledge.ingest` for this, and it is the only cause in that taxonomy whose remedy is a
better scan. See `OCR_MUST_NEVER_SAY_A_FILE_NEEDS_OCR`.

**What the floor does not cover.** It is one number for a whole document, so a report that is
clean for nine pages and noise on the tenth passes it, and the tenth page is indexed. Making
that per passage needs `brain.knowledge.chunking.Block` to carry the figure and `chunk_blocks`
to copy it onto every `Passage`, which is a change to a closed leaf for a case no engine in
this repository can produce. Named rather than pre-built, and it is the more likely real
failure of the two.

**Nothing calls anything in this module, and no OCR engine exists.** There is no inference
server, and `brain.ops.inference.SERVED_MODELS` declares one parsing model whose weights figure
is a judgement written for layout and table models; whether an OCR engine fits inside it is
unknown and `ocr_gaps` says so rather than assuming. `OcrParser` with no engine refuses with
`PARSER_UNAVAILABLE`, which is the honest state of this leaf.

Scope: domain logic. Nothing here opens a connection, loads a model or reads a clock.

Task ids: M7.2.4
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from brain.knowledge.chunking import Block
from brain.knowledge.ingest import IngestRefused, ParseCause
from brain.knowledge.parse_paths import ParsePath, ParseProvenance
from brain.knowledge.scanning import ParseRefusal, ParseStage, ScannedContent
from brain.ops.inference import SERVED_MODELS, InferenceTask, ServedModel

# ------------------------------------------------------------------ written-down reasons

#: Why a reading below the floor is refused instead of indexed with a caveat.
A_GUESS_BELOW_THE_FLOOR_IS_WORSE_THAN_NO_TEXT: Final = (
    "The two failures are not symmetric. A document that was refused is a document somebody "
    "can see is missing: they search for it, nothing comes back, and the absence is the "
    "symptom. A document that was recognised badly and indexed is a corpus of plausible "
    "strings that answer questions, with a citation pointing at a real page, and the only way "
    "to find out is to open the file and read it, which is the thing a citation exists to save "
    "somebody doing. Neither the lexical leg nor the vector leg can tell the difference: a "
    "tsvector over noise is a valid tsvector and an embedding of noise is a valid vector. So "
    "the refusal is the safe direction, and the floor is where the trade is made rather than "
    "left to whoever reads the answer."
)

#: Why the number is used here and never recorded beside the passage.
A_CONFIDENCE_HAS_NO_SCALE_ACROSS_ENGINES: Final = (
    "Every OCR engine reports a confidence and no two of them mean the same thing by it. One "
    "is a per-character posterior averaged over a word, another is a heuristic over the "
    "recogniser's own alternatives, another is whatever a vendor decided looked reassuring. "
    "Stored on a parse record it would sit beside page and section, which mean the same thing "
    "whoever produced them, and an operator would compare 0.7 from one engine against 0.7 from "
    "the next and act on the difference. ServedModel.sizing_basis exists because a number in a "
    "budget with no stated origin cannot be checked by anybody, and this is that same problem "
    "with no room for a sizing_basis beside it. What is comparable across every engine is that "
    "the characters were guessed, and ParsePath.OCR says exactly that and nothing more."
)

#: Why this path may not produce the cause that names this path as the remedy.
OCR_MUST_NEVER_SAY_A_FILE_NEEDS_OCR: Final = (
    "CAUSE_TEXT[NO_TEXT_LAYER] reads 'the file is a scan with no text in it. It needs the "
    "scanned-document path rather than a re-upload.' That sentence is correct from a layout "
    "parser and is a loop from this one: the scanned-document path is what just ran. Two "
    "routes reach it and both are closed here. An engine can return the cause itself, and an "
    "engine can return no blocks, which parse_scanned turns into that cause because for every "
    "other parser it is the right one. Both become ILLEGIBLE, whose remedy is a clearer scan "
    "or a copy with text in it, and which is the only cause in the taxonomy that asks for a "
    "different file rather than a different action on the same one."
)


# ------------------------------------------------------------------ what serves this path

#: The engine name recorded against a passage this path read. Distinct from
#: `brain.knowledge.parse_layout.LAYOUT_ENGINE` even though the same model serves both tasks,
#: because provenance answers "what kind of evidence is this" and a layout read and a
#: recognised page are not the same kind. One model, two engines, and the route holds both.
OCR_ENGINE: Final = "docling-ocr"

#: Which task on the inference server would answer this. The parsing task, because item 31
#: sized one parsing model and Docling's OCR is part of that stack rather than a fourth model.
#: `ocr_gaps` is what stops that being an assumption nobody checks.
OCR_TASK: Final = InferenceTask.PARSING

#: The least confident reading that may still be indexed.
#:
#: **A judgement with nothing measured behind it**, in the same register as
#: `brain.knowledge.parse_budget.PARSE_EXPANSION` and for the same reason: there is no OCR
#: engine in this project and nothing to measure. The argument for the region it sits in is
#: that below roughly one character in two, neither retrieval leg can work at all. The lexical
#: leg needs a word to survive intact to match it, and a word of eight characters with a coin
#: flip on each has a one in two hundred and fifty six chance of doing so; the vector leg
#: embeds whatever it is given, so noise gets a position in the space and competes for it.
#: Above the floor a reading is indexed and is still a guess, which is what `ParsePath.OCR`
#: carries. Nothing about this figure is safe to describe as calibrated.
OCR_FLOOR_CONFIDENCE: Final = 0.55


def ocr_provenance() -> ParseProvenance:
    """What a document read by this path carries, built in one place.

    A function rather than a constant, matching `parse_layout.layout_provenance`: the path and
    the engine are joined once so a route cannot pair `ParsePath.OCR` with a layout engine's
    name, which would record a guess as a reading.
    """
    return ParseProvenance(path=ParsePath.OCR, engine=OCR_ENGINE)


# ------------------------------------------------------------------ the reading


@dataclass(frozen=True)
class OcrReading:
    """What an engine recognised, and how sure it says it is.

    The confidence is required and has no default. A default would be a number somebody did not
    choose deciding whether a document is indexed, and the only defensible default is the one
    that refuses everything, which would make every engine look broken until somebody found
    this line.

    One figure for the whole reading rather than one per block, which is a limit rather than a
    design: `Block` has no field for it and adding one is a change to a closed leaf. The
    consequence is stated in the module docstring and it is the more likely real failure.
    """

    blocks: tuple[Block, ...]
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            msg = (
                f"an OCR reading reports confidence {self.confidence}, which is not one; a "
                "figure outside the unit interval is an engine reporting in units nobody here "
                "agreed, and it would be compared against a floor that is in the other units"
            )
            raise IngestRefused(msg)

    @property
    def is_legible(self) -> bool:
        """Whether this reading is sure enough to be worth indexing.

        On the type rather than in the parser so the question can be asked of a reading without
        constructing a parser around it, and so the comparison against the floor is spelled
        once. See `A_GUESS_BELOW_THE_FLOOR_IS_WORSE_THAN_NO_TEXT` for the trade it makes.
        """
        return self.confidence >= OCR_FLOOR_CONFIDENCE


class OcrEngine(Protocol):
    """Recognises text on the pages of a cleared document, and says how sure it is.

    Takes `ScannedContent` and there is no overload taking bytes, which is
    `brain.knowledge.scanning.Parser`'s ordering property and applies here for a sharper reason
    than usual: an OCR engine is handed an image decoder, which is the single most
    attacker-exposed piece of code in any parsing stack.

    Returns a reading or a refusal, never raises, matching `Parser`. **An engine reached over a
    network has to hold its refusal to a closed vocabulary**, the way
    `brain.knowledge.parse_layout._declared_refusal` does, and no such decoder is written
    because no such engine exists. An in-process engine is trusted code and needs none.
    """

    def read(self, content: ScannedContent) -> OcrReading | ParseRefusal: ...


@dataclass(frozen=True)
class OcrParser:
    """`brain.knowledge.scanning.Parser` over an OCR engine, refusing when there is none.

    **With `engine` unset this refuses everything, which is the honest state of M7.2.4 today.**
    No OCR engine is a dependency of this project and none is served: `PARSER_UNAVAILABLE` says
    the file has not been read yet and that nothing is wrong with it, and `is_retryable`
    answers yes, so a scanned document parked on this is re-driven when an engine exists.

    Frozen and stateless, matching `parse_layout.LayoutParser`: a parser that remembered
    anything would make one document's result depend on another's, and nothing orders the
    documents a route is handed.
    """

    engine: OcrEngine | None = None

    def parse(self, content: ScannedContent) -> Sequence[Block] | ParseRefusal:
        """Recognise one cleared document, or say why the reading may not be indexed (M7.2.4).

        Three outcomes and two of them are refusals. An engine that is not there is an outage.
        A reading below the floor, or with nothing in it, is `ILLEGIBLE`: see
        `OCR_MUST_NEVER_SAY_A_FILE_NEEDS_OCR` for why an empty reading is converted here rather
        than left for `parse_scanned` to name, which is the one place this path has to disagree
        with the shared failure handling instead of reusing it.

        An engine's own refusal passes through unchanged except for that one substitution. The
        engine knows things this side does not, such as that the image is encrypted or damaged,
        and rewording those would be this module inventing a diagnosis.
        """
        if self.engine is None:
            return ParseRefusal(cause=ParseCause.PARSER_UNAVAILABLE, stage=ParseStage.OCR)
        outcome = self.engine.read(content)
        if isinstance(outcome, ParseRefusal):
            if outcome.cause is ParseCause.NO_TEXT_LAYER:
                return ParseRefusal(cause=ParseCause.ILLEGIBLE, stage=ParseStage.OCR)
            return outcome
        if not outcome.blocks or not outcome.is_legible:
            return ParseRefusal(cause=ParseCause.ILLEGIBLE, stage=ParseStage.OCR)
        return outcome.blocks


def ocr_gaps(models: Sequence[ServedModel] = SERVED_MODELS) -> tuple[str, ...]:
    """Every reason this deployment could not run the OCR path, in words naming the fix.

    Two checks, and neither is about a file.

    Whether anything serves the task at all, which is `brain.ops.pii.configuration_gaps`'s
    question about a detection kind with no recogniser: a task with no model is a request that
    can never be answered, and the symptom is a timeout rather than an error, so a deployment
    missing a model looks exactly like one that is merely down.

    And whether the figure that sized the container was written with an OCR engine in mind. It
    was not: `ServedModel.measured` is False for the parsing model and its `sizing_basis` is a
    judgement about layout and table models. Reported rather than raised, because there is no
    server to measure and inventing a second weights figure would put a number that looks like
    a result next to two that are arithmetic.

    `models` is a parameter defaulting to what is deployed, for the reason
    `brain.ops.inference.weights_mib` gives about its own: a check that can only ever be run
    against the constant beside it cannot be shown to fail, and a check nobody has seen fail is
    a check nobody knows works. Both branches here are unreachable against `SERVED_MODELS`
    today, the first because the parsing task is served and the second because it is not
    measured.

    **Nothing calls this.** `brain.ops.worker.preflight` is where it belongs, beside
    `inference_gaps`, and that file is not edited here.
    """
    for model in models:
        if model.task is not OCR_TASK:
            continue
        if model.measured:
            return ()
        return (
            f"the OCR path is served by {model.name!r}, whose {model.weights_mib} MiB is a "
            "judgement rather than a measurement and was written for layout and table models; "
            "whether a recogniser fits inside it is unknown, and the container is sized from "
            "that figure, so an engine that does not fit is an OOM kill during startup",
        )
    return (
        f"no model serves {OCR_TASK.value!r}, so a scanned document can never be read; the "
        "caller waits, the call times out, and a deployment missing a model looks exactly "
        "like one that is merely down",
    )
