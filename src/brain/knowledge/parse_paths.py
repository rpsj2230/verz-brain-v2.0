"""More than one way to read a document, and the rule that says which one read this one.

`brain.knowledge.scanning` already holds the single-parser story: the scan happens first
because the parameter type says so, the memory bound is checked before the parser is called
because `parse_scanned` is the only place it is called, and a failure is a value carrying a
cause and a stage and nothing else. What none of that answers is the question a second parser
creates the moment it exists, which is **which one produced the passage in front of you**.

**A fallback that silently produces worse output is worse than a refusal, and the reason is
the citation.** A layout-aware parser returns a table as a table; a text extractor returns the
same table as a column of numbers with the header somewhere above it. Both produce blocks,
both chunk, both index, and both are cited as the same document at the same page. The answer's
reliability changed and every artefact a reader can see says it did not. So the path travels
with the parse, in a field that cannot be omitted, and `AttributedParse` cannot be constructed
without one. See `A_FALLBACK_NOBODY_CAN_SEE_IS_A_QUALITY_CHANGE_NOBODY_CAN_AUDIT`.

**An outage is not a reason to fall back**, and this is the rule that does the most work here.
`ParseCause.PARSER_UNAVAILABLE` and `TIMED_OUT` are `is_retryable` precisely because they are
facts about this system rather than about the file, and the queue re-drives them. Treating
either as a reason to try the weaker parser converts an hour of inference downtime into every
document ingested in that hour being permanently on the fallback, with nothing recording it,
nothing re-parsing them, and the only symptom an answer that is quietly worse. The same is
true of `OUT_OF_MEMORY`, where `ParseStage.ADMIT` means no parser ran at all and a second one
gets the same budget. So the set that may fall back is the set of causes that are about a
*parser's* limits, and it has two members. See `AN_OUTAGE_IS_NOT_A_REASON_TO_FALL_BACK`.

**The order of a route is checked rather than documented**, because the two ways to get it
wrong are silent. A route that put a text extractor before the layout parser would index the
worse reading of every document that both can read, and nothing would fail. A route that put
OCR anywhere but last would replace a text layer that was read with glyphs that were guessed,
on a file that had text in it all along. `PATH_RANK` is the ordering and `route_refusals`
applies it, so a route is refused before a byte is parsed rather than producing a worse corpus.

**Each attempt goes through `parse_scanned` again, which is deliberate and costs something.**
The memory bound is therefore re-asked per attempt rather than assumed to still hold, and two
attempts on one file spend one parse budget twice. That is affordable only because
`brain.knowledge.parse_budget.PARSES_AT_ONCE` is one and the container runs them in sequence;
on a worker that ran parses concurrently this loop would be the thing that oversubscribed it.
The rejected alternative was to check the bound once here and call the parsers directly, which
would move the bound out of the one function that may call `Parser.parse` and make it a
convention again, which is the argument `scanning` opens with.

**What is not built, stated plainly.** Nothing stores a path. `know.chunk` has columns for the
chunk, the document, the span, the body and the embedding model, and none for the parser, so a
`DocumentCitation` assembled from a retrieved row has nothing to read a path off. Closing that
is one of two edits and neither is made here: a `path` column on `know.chunk`, written by
whatever writes chunks, or a `know.parse` row per document that a citation joins on
`document_id`. The second is the smaller change and the one this module is shaped for, because
`AttributedParse` is per document rather than per block. Which brings the honest limit of that
shape: **a document read by two paths cannot be described by this type.** A parse where OCR
filled in three pages of an otherwise layout-read PDF has one provenance here and it is a lie
for those three pages. Making it per block needs `brain.knowledge.chunking.Block` to carry the
field and `chunk_blocks` to copy it onto every `Passage`, which is a change to a closed leaf
for a case no parser in this repository can produce, so it is named rather than pre-built.

**Nothing calls anything in this module.** There is no layout parser, no fallback extractor and
no OCR engine: `brain.knowledge.parse_layout` and `brain.knowledge.parse_ocr` are seams that
refuse, for the reason `docs/needs-rupash.md` item 31 gives, and `brain.ops.queue` has no
driver to enqueue a parse job with. What is here is the rule a route has to satisfy on the day
one of those three arrives, written where the argument is rather than left to the call site.

Scope: domain logic. Nothing here opens a connection, reads a clock or holds a parser.

Task ids: M7.2.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from brain.knowledge.chunking import Block
from brain.knowledge.ingest import IngestRefused, ParseCause, ParseFailure
from brain.knowledge.scanning import ParsedDocument, Parser, ScannedContent, parse_scanned

# ------------------------------------------------------------------ written-down reasons

#: Why the path is carried on the parse rather than left to whoever reads the corpus.
A_FALLBACK_NOBODY_CAN_SEE_IS_A_QUALITY_CHANGE_NOBODY_CAN_AUDIT: Final = (
    "Two parsers produce the same shape from the same file and not the same quality. A "
    "layout-aware parser returns a table as one atomic block with its header attached; a text "
    "extractor returns the cells in reading order and the header three lines above them. Both "
    "chunk, both index, and both are cited as page 4 of the same contract, so a reader "
    "checking the citation sees a real document at a real page and has no way to learn that "
    "the figure they were shown came out of a column that was reassembled by guesswork. The "
    "path is therefore a required field on AttributedParse rather than a note somewhere: a "
    "parse whose path nobody recorded is a parse whose quality nobody can ever establish "
    "afterwards, because the file has been read and the reading is what was kept."
)

#: Why the causes that are about this system may not move a document to a weaker parser.
AN_OUTAGE_IS_NOT_A_REASON_TO_FALL_BACK: Final = (
    "PARSER_UNAVAILABLE and TIMED_OUT are the two causes ParseFailure.is_retryable answers yes "
    "for, and it answers yes because both are facts about this system rather than about the "
    "file. A fallback on either is an hour of inference downtime turned into a permanent "
    "quality change: every document uploaded in that hour is read by the weaker parser, "
    "written to the corpus, never re-parsed, and nothing anywhere says which hour it was. The "
    "queue is the retry, and a retry gets the good parser. OUT_OF_MEMORY is refused for a "
    "different reason with the same shape: its stage is ADMIT, so no parser ran, and the "
    "second parser is handed the identical file and the identical budget. What is left is the "
    "set of causes that are about a parser's own limits rather than about the machine, and a "
    "document that one parser cannot open and another can is the only case a fallback improves."
)

#: Why the order of a route is enforced instead of being left to whoever assembles one.
A_ROUTE_IN_THE_WRONG_ORDER_FAILS_WITHOUT_FAILING: Final = (
    "Both ways to misorder a route succeed. Put the text extractor before the layout parser "
    "and every document either could read is indexed as the worse reading, with no error, no "
    "empty result and no difference visible in a chunk. Put OCR anywhere but last and a file "
    "that had a text layer all along is indexed as glyphs somebody guessed, which is the one "
    "substitution in this package that turns a read fact into an invention. Neither is "
    "detectable from the corpus afterwards, so the order is a check that runs before a byte "
    "is parsed rather than a sentence in a docstring somebody assembling a route may not read."
)


# ------------------------------------------------------------------ the paths


class ParsePath(enum.StrEnum):
    """How a document was read. Closed, because the whole point is that the set is reviewable.

    Three, and each is a different kind of claim about the text. A layout read and a plain
    text extraction are both readings of characters the file already contained; OCR is not,
    and that difference is the reason this is an enum rather than an engine name on its own.
    An engine name says which program ran, which changes when somebody upgrades it. A path
    says what kind of evidence the text is, which does not.
    """

    #: The primary. A model that finds the regions, the reading order and the tables.
    LAYOUT = "layout"
    #: Characters pulled out of the container by a general extractor, with no layout model.
    #: Tables arrive as prose, and that is the quality change this module exists to record.
    FALLBACK = "fallback"
    #: Glyphs recognised from an image of a page. Every character is a guess.
    OCR = "ocr"


#: Where each path may appear in a route, lowest first. A separate table rather than an
#: `IntEnum`, because the value of a path is stored and compared and read in an operator line,
#: and `brain.gate.injection.AutonomyTier` gets to be an `IntEnum` precisely because nothing
#: writes one down. Totality is asserted by test: a path with no rank would be silently
#: unorderable, and `route_refusals` would then pass a route it cannot actually order.
PATH_RANK: Final[Mapping[ParsePath, int]] = {
    ParsePath.LAYOUT: 0,
    ParsePath.FALLBACK: 1,
    #: Last, always. See `A_ROUTE_IN_THE_WRONG_ORDER_FAILS_WITHOUT_FAILING`.
    ParsePath.OCR: 2,
}

#: The causes that permit another path to be tried. Two, and the argument for the shortness of
#: this set is `AN_OUTAGE_IS_NOT_A_REASON_TO_FALL_BACK`.
#:
#: UNSUPPORTED is the case a fallback was invented for: the container opened and held something
#: this parser does not handle, which is a statement about the parser and not about the file.
#: NO_TEXT_LAYER is the same shape one step further on, and `CAUSE_TEXT` already words it as
#: needing the scanned-document path rather than a re-upload, which is a fallback in prose.
MAY_TRY_ANOTHER_PATH: Final[frozenset[ParseCause]] = frozenset(
    {ParseCause.UNSUPPORTED, ParseCause.NO_TEXT_LAYER}
)

#: What an engine may be called. Bounded and lowercase, because the name travels into an
#: operator's line and would travel into a column the day one exists, and an unbounded string
#: on a parse record is where a vendor's version banner ends up.
_ENGINE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def may_try_another_path(cause: ParseCause) -> bool:
    """Whether this cause permits a second parser to be handed the same file.

    Positive sense and a membership test rather than a chain of comparisons, so that adding a
    cause to `ParseCause` is a decision about one set rather than an edit somebody makes to a
    condition. A cause added and forgotten falls out as "no fallback", which is the safe
    direction: the document is refused with its cause named rather than quietly downgraded.
    """
    return cause in MAY_TRY_ANOTHER_PATH


@dataclass(frozen=True)
class ParseProvenance:
    """Which path read a document, and which program did the reading.

    Two fields and both are required, which is the enforcement. The path is what a reader
    needs, because it says what kind of evidence the text is; the engine is what an operator
    needs, because "the fallback is producing scrambled tables" is a question about a version
    of a program. Neither is derivable from the other: two engines can share a path, and one
    engine that also does OCR sits on two.

    There is no confidence field, and that absence is argued in
    `brain.knowledge.parse_ocr.A_CONFIDENCE_HAS_NO_SCALE_ACROSS_ENGINES`. In short: a float
    here would sit beside `page` and `section`, which mean the same thing whoever produced
    them, and it would not.
    """

    path: ParsePath
    engine: str

    def __post_init__(self) -> None:
        if not _ENGINE_RE.match(self.engine):
            msg = (
                f"parse engine {self.engine!r} is not a name; an engine is recorded beside "
                "every passage it read, and an unbounded string there is where a vendor's "
                "banner and eventually a line of the document end up"
            )
            raise IngestRefused(msg)

    def describe(self) -> str:
        """The operator's half: "layout by docling-layout-and-tableformer".

        Names our own components and nothing from the file, which is what makes it safe to
        print. It is not shown to an uploader: a person who sent a PDF has no use for the name
        of the program that read it, and `ParseFailure.message` is what they see.
        """
        return f"{self.path.value} by {self.engine}"


@dataclass(frozen=True)
class PathAttempt:
    """One path that was tried and would not read this file.

    Kept rather than discarded, because "this document is on the fallback" and "this document
    is on the fallback because the layout parser could not open it" are different facts to an
    operator, and only the second one leads anywhere. It carries a cause and no detail for the
    reason `ParseRefusal` carries a cause and a stage: there is nowhere for a sentence of the
    document to travel.
    """

    provenance: ParseProvenance
    cause: ParseCause


@dataclass(frozen=True)
class AttributedParse:
    """A parsed document and the path that produced it. The only result a route returns.

    `document` is the `ParsedDocument` the gate built, held rather than unpacked: the digest
    binding text to bytes is the whole of why that type exists, and a result that copied the
    blocks out of it and left the digest behind would be the translation step
    `ParsedDocument` itself warns about.

    `refused` is in route order and holds only paths that actually ran and answered. A path
    that was never reached because an earlier one succeeded is not an attempt and does not
    appear, which is what stops this reading as a list of things that went wrong.
    """

    document: ParsedDocument
    provenance: ParseProvenance
    refused: tuple[PathAttempt, ...] = ()

    @property
    def blocks(self) -> tuple[Block, ...]:
        """The blocks, for a caller that is about to chunk them.

        The same objects the parser returned, never rebuilt. A rebuild is where a page number
        goes missing, and a page number is half of what a citation resolves against.
        """
        return self.document.blocks

    @property
    def fell_back(self) -> bool:
        """Whether the path that answered was not the first one asked.

        The property a console row or an operator alert would branch on, and the reason
        `refused` is a tuple rather than a boolean: the count is not the interesting part, the
        cause is.
        """
        return bool(self.refused)

    def describe(self) -> str:
        """One operator line saying which path read this and what it displaced.

        Deliberately not an uploader's sentence. It names our own engines, and an engine name
        in a message to somebody who uploaded a file is an implementation detail they cannot
        act on, which is the distinction `ParseStage` already makes about itself.
        """
        if not self.refused:
            return f"read by {self.provenance.describe()}"
        displaced = ", ".join(
            f"{attempt.provenance.describe()} ({attempt.cause.value})" for attempt in self.refused
        )
        return f"read by {self.provenance.describe()} after {displaced}"


@dataclass(frozen=True)
class RoutedParser:
    """One parser and the provenance a document it reads will carry.

    The provenance is declared beside the parser rather than reported by it, and that is the
    load-bearing choice in this type. A parser that named its own path could name a different
    one on a bad day, and the failure would be a corpus where some fallback readings are
    recorded as layout readings, which is worse than not recording the path at all: it is a
    field that looks authoritative and is wrong for an unknown subset of rows. Declared here,
    the path is a property of the route somebody assembled and reviewed.

    `parser` is `brain.knowledge.scanning.Parser`, unchanged and unwidened. The protocol takes
    `ScannedContent` and there is no overload taking bytes, which is the ordering property this
    whole area is built on, and a route made of anything else would have been a second way to
    reach a parser.
    """

    provenance: ParseProvenance
    parser: Parser


def route_refusals(route: Sequence[RoutedParser]) -> tuple[str, ...]:
    """Every reason this route must not be run, in words naming the fix.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight` and
    `parse_worker_gaps`: a route wrong in two ways is one where fixing one leaves a route that
    is still wrong and still silent.

    Three checks. An empty route, because a route with no parsers returns a failure that reads
    as a file nothing could make sense of, when what happened is that nobody configured a
    parser. A repeated path, because the path is what a reader is told and two entries sharing
    one would make that answer ambiguous while both engines were being recorded as the same
    kind of evidence. And the order, which is
    `A_ROUTE_IN_THE_WRONG_ORDER_FAILS_WITHOUT_FAILING` and is the only one of the three whose
    failure produces a corpus rather than an error.
    """
    findings: list[str] = []
    if not route:
        findings.append(
            "a parse route with no parsers in it reads every document as a failure whose "
            "cause is about the file; the cause is that nobody configured a parser, and no "
            "wording in ParseCause says that to an uploader because it is not their problem"
        )
        return tuple(findings)

    seen: list[ParsePath] = [entry.provenance.path for entry in route]
    repeated = sorted({path.value for path in seen if seen.count(path) > 1})
    if repeated:
        findings.append(
            f"the route uses {repeated} more than once, so a passage recorded under that path "
            "does not say which engine read it; the path is what a reader is told and two "
            "engines behind one path make that answer ambiguous for every document"
        )

    ranks = [PATH_RANK[path] for path in seen]
    if ranks != sorted(ranks):
        findings.append(
            f"the route runs {[path.value for path in seen]} in that order, and the order is "
            f"{sorted(PATH_RANK, key=lambda path: PATH_RANK[path])}; a weaker parser before a "
            "stronger one indexes the worse reading of every document both can read, and OCR "
            "before either replaces text that was read with glyphs that were guessed"
        )
    return tuple(findings)


def parse_by_route(
    content: ScannedContent,
    *,
    route: Sequence[RoutedParser],
    budget_bytes: int | None = None,
) -> AttributedParse | ParseFailure:
    """Read a document by the first path that can, recording which one did (M7.2.3).

    Each attempt is a call to `brain.knowledge.scanning.parse_scanned`, never a call to a
    parser, so the memory bound and the re-rendering of a refusal stay in the one function
    that owns them. What this adds is the two decisions that only exist once there is more
    than one parser: whether a failure permits another to be tried, and what the result says
    about which one answered.

    **The failure returned when every path refuses is the last one, not the first.** The
    uploader gets one sentence and one remedy, and the remedy that is true is the one from the
    engine that got furthest: a file the layout parser called unsupported and OCR called
    illegible needs a clearer scan, and telling them to convert it to PDF would send them to
    do something they have already effectively done. Every earlier refusal is still recorded
    for an operator, but not in the message, because a person who uploaded a document does not
    have three parsers to reason about.

    `budget_bytes` is passed through unchanged rather than divided among the attempts. The
    attempts are sequential and each one has finished and released its memory before the next
    starts, so the bound that matters is per parse; dividing it would refuse a file that fits
    on the grounds that a second parser might also be asked to read it.
    """
    refusals = route_refusals(route)
    if refusals:
        msg = "; ".join(refusals)
        raise IngestRefused(msg)

    attempts: list[PathAttempt] = []
    # `route` is non-empty, which `route_refusals` has already established, so this name is
    # bound before the return below. Kept as an explicit assignment rather than relying on the
    # loop variable surviving, because a loop that cannot end without binding it is exactly the
    # shape a later edit breaks quietly.
    last: ParseFailure | None = None
    for entry in route:
        outcome = parse_scanned(content, parser=entry.parser, budget_bytes=budget_bytes)
        if isinstance(outcome, ParsedDocument):
            return AttributedParse(
                document=outcome,
                provenance=entry.provenance,
                refused=tuple(attempts),
            )
        last = outcome
        if not may_try_another_path(outcome.cause):
            return outcome
        attempts.append(PathAttempt(provenance=entry.provenance, cause=outcome.cause))
    if last is None:  # pragma: no cover - `route_refusals` refuses an empty route above
        msg = "a route with no entries reached the loop; route_refusals should have refused it"
        raise IngestRefused(msg)
    return last
