"""Scanning before parsing, made a type rather than an order somebody has to remember.

`brain.knowledge.ingest` already holds the decisions. `assert_clean` refuses a verdict about
other bytes, refuses an infected file and refuses one the scanner could not read; `ParseCause`
and `CAUSE_TEXT` hold what an uploader may be told. What none of that can do is make the scan
*happen first*. "Call the scanner, then call the parser" is a convention, and a convention is
enforced by whoever writes the fourth caller in a hurry.

**A parser is the most attacker-exposed code in this system, and it is the one component that
cannot refuse malformed input.** Every other module rejects what it does not recognise at its
edge. A parser is handed a hostile file by design, because a hostile file is indistinguishable
from an ordinary one until it has been opened, and opening it is the job. So the ordering
around it is the defence, and an ordering that depends on being remembered is not one.

**Three doors, and each closes a different hole.**

*The parameter type closes the ordinary mistake.* `Parser.parse` takes `ScannedContent`, and
there is no overload taking `bytes`. Handing a parser an unscanned buffer is not an expression
that type-checks, and mypy runs strict over `src`, so that is a gate rather than a suggestion.
This is the shape `brain.tools.registry.assert_object_not_reserved` uses for the skill-script
object and `brain.channels.binding.NonceLedger` uses by having no read method: the unsafe
spelling is not available rather than discouraged.

*The seal closes the deliberate one.* A caller who imports `ScannedContent` and builds one
around raw bytes is refused at runtime, because the constructor demands a module-private
token that only `scan_for_parsing` holds. The class is `@final` as well, so the subclass that
overrides `__post_init__` and skips the check is a type error too.

*The digest closes the subtle one, which is scanning file A and parsing file B.* The gate
hashes the bytes it was handed and compares them with what the door admitted, so a clean
verdict cannot be carried across two uploads even by a caller who is trying to.

**A parser reports failure as a value and has no field for prose.** `ParseRefusal` carries a
cause and a stage, both enum members, and nothing else. There is nowhere for a sentence of the
document to travel, which matters because a parse failure is rendered into a notification, a
console row and a log line, and those three go further than the document's own scope does.
`ingest._DETAIL_RE` bounds a detail to 120 characters of ordinary punctuation, and a line of a
quotation fits inside that comfortably; a closed vocabulary of stage names does not.

**A refusal is not a parse failure, and they arrive by different routes on purpose.** Refused,
corrupt and unsupported have three different remedies. A refusal raises, a parse failure is
returned, and the wording an uploader reads comes from `CAUSE_TEXT` rather than from whatever
the parser felt like saying.

Rejected: catching whatever a parser raises and reporting it as `PARSER_UNAVAILABLE`. Its
wording tells the uploader "nothing is wrong with the file", which is a claim nobody can make
about a file that has just crashed a parser, and `is_retryable` would put it back on the queue
for ever. So an exception out of a parser is out of contract and reaches the worker, where a
bug belongs. The contract is a value, and a value in a union return type cannot be dropped by
an `except Exception` in a worker loop without the drop being visible in review.

Nothing here spawns a scanner or opens a file. `Scanner` is a protocol because there is no
scanner on this machine and acquiring a dependency to get one is not this module's decision to
make; and because the two cases worth testing, an infected verdict and a scanner that reaches
no conclusion, are not reachable against a real one.

Task ids: M7.1.3, M7.2.5
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, final

from brain.knowledge.chunking import Block
from brain.knowledge.ingest import (
    AdmittedUpload,
    IngestRefused,
    ParseCause,
    ParseFailure,
    ScanResult,
    ScanVerdict,
    assert_clean,
)

# ------------------------------------------------------------------ written-down reasons
#: Why the ordering is a type rather than a rule about which function to call first.
SCANNING_BEFORE_PARSING_IS_A_TYPE = (
    "A parser is handed a hostile file by design, so the one thing that must never happen is "
    "a parser reached with bytes nobody scanned. Enforced by calling the scanner first, that "
    "holds until the fourth caller. Enforced by the parser's parameter type it cannot be "
    "spelled at all: there is no overload taking bytes, the only type that carries them "
    "refuses to be built outside the gate, and the gate hashes what it was given and compares "
    "it with what the door admitted."
)

#: Why a parser reports a cause and a stage and nothing else.
A_PARSER_HAS_NO_FIELD_FOR_PROSE = (
    "The message an uploader reads is assembled from CAUSE_TEXT and from the upload's own "
    "filename, and a parser contributes one enum member to it. ParseRefusal has no string "
    "field, so there is nowhere for a line of the document to travel. A detail bounded by "
    "length and character set is not enough on its own: ingest._DETAIL_RE admits 120 "
    "characters of letters, digits and spaces, which is a whole sentence of a quotation, and "
    "a parse failure is read in a chat client the document's own scope does not reach."
)

#: Why a refused file never comes back as a parse failure.
A_REFUSAL_IS_NOT_A_PARSE_FAILURE = (
    "Refused, corrupt and unsupported have three different remedies, so they must not arrive "
    "by the same route. A refusal raises IngestRefused and a parse failure is returned as a "
    "value. Reporting an infected or unscannable file as a corrupt one would tell the person "
    "to re-export it and send it again, which is the worst advice available and produces a "
    "second copy of the same file arriving at the same scanner."
)


# --------------------------------------------------------------------- the scanner seam
@dataclass(frozen=True)
class ScanReport:
    """What a scanner concluded, without the digest it concluded it about.

    **The absent field is the design.** `ingest.ScanResult` binds a verdict to a digest, and
    that binding is only worth anything if the digest is not chosen by the same component that
    chose the verdict. A scanner that echoed back the digest it was asked about could report
    clean for bytes it never read, and the binding would be a comparison of a value with
    itself. So a scanner says what it found and who found it, and the gate attaches the digest
    it computed for the bytes in its own hand.

    No detail field either. A scanner's detail is vendor prose naming a signature, and prose
    from an external tool travelling into a user-facing message is the same hole
    `A_PARSER_HAS_NO_FIELD_FOR_PROSE` describes. The verdict and the scanner's name are what
    anybody acts on.
    """

    verdict: ScanVerdict
    scanner: str

    def __post_init__(self) -> None:
        if not self.scanner.strip():
            msg = (
                "a scan verdict with no scanner named cannot be argued with later; when the "
                "question is why this file was let through, the answer is a name"
            )
            raise IngestRefused(msg)


class Scanner(Protocol):
    """Reaches a verdict about bytes.

    A protocol rather than a client, for the reason `brain.tools.fetch.Fetcher` gives about
    HTTP: the cases that decide whether this is right are an infected verdict and a scanner
    that reaches no conclusion, and neither is reachable against a real scanner in a test.

    It takes the content and returns a report. It is deliberately not told the digest, the
    filename or the declared type: a scanner that can see what it is expected to say is a
    scanner that can be induced to say it.
    """

    def scan(self, content: bytes) -> ScanReport: ...


#: The token `ScannedContent` demands. Module-private, so building one anywhere else means
#: reaching for a name whose leading underscore says what it is for, and being refused anyway.
_ISSUED_BY_THE_GATE: Final = object()


@final
@dataclass(frozen=True)
class ScannedContent:
    """Bytes a scanner has cleared, and the only shape a parser can be handed.

    `@final` is load-bearing rather than decoration. Without it the bypass is four lines: a
    subclass with its own `__post_init__` that does not check the seal, which then satisfies
    `Parser.parse` because it is a `ScannedContent`.

    **That bypass is closed statically and not at runtime, which is worth saying because
    `@final` is widely assumed to be the other way round.** The interpreter will subclass this
    happily; I checked rather than assumed, and the forged subclass constructs and passes
    `isinstance`. What refuses it is mypy, which runs strict over this repository as a
    pre-push hook and again in CI, so the subclass cannot be committed. That is a real
    defence here and it would not be in a project without those gates, which is the condition
    to notice if this module is ever lifted somewhere else.

    The body is carried here rather than fetched again by the parser, and that is the point of
    the type. If a parser re-read the object store by key, the bytes it parsed would be
    whatever was at that key at that moment, and the scan would be a statement about an
    earlier version of them.
    """

    #: `_ISSUED_BY_THE_GATE`, and nothing else will do.
    issued_by: object
    upload: AdmittedUpload
    body: bytes
    scan: ScanResult

    def __post_init__(self) -> None:
        if self.issued_by is not _ISSUED_BY_THE_GATE:
            msg = (
                "scanned content is issued by scan_for_parsing and by nothing else. Building "
                "one directly is asserting that a scanner cleared these bytes, which is the "
                "one claim in this module nobody may make on a scanner's behalf"
            )
            raise IngestRefused(msg)


def scan_for_parsing(upload: AdmittedUpload, content: bytes, *, scanner: Scanner) -> ScannedContent:
    """Scan these bytes and, only if they are clean, make them parseable (M7.1.3).

    The three refusals stay in `ingest.assert_clean` and are not restated here. The digest
    comparison inside it cannot fail on this path, because the digest handed to it is the one
    computed four lines above; it is still called rather than inlined, because a second copy
    of "infected is refused, and unscanned is not clean" is the copy that goes stale.

    The recomputation is the check that `assert_clean` cannot make. `AdmittedUpload` carries a
    digest and no bytes, so nothing else in the system can tell whether the buffer in hand is
    the buffer the door measured, sniffed and admitted. A caller that admitted one file and
    passed another is refused here, and the message says which of the two disagrees.
    """
    digest = hashlib.sha256(content).hexdigest()
    if digest != upload.digest:
        msg = (
            f"{upload.filename!r} was admitted as {upload.digest[:12]} and the bytes offered "
            f"for scanning are {digest[:12]}; the file checked at the door is not the file "
            "that would be parsed"
        )
        raise IngestRefused(msg)

    report = scanner.scan(content)
    result = ScanResult(digest=digest, verdict=report.verdict, scanner=report.scanner)
    assert_clean(upload, result)
    return ScannedContent(issued_by=_ISSUED_BY_THE_GATE, upload=upload, body=content, scan=result)


# ----------------------------------------------------------------- parsing (M7.2.5)
class ParseStage(enum.StrEnum):
    """Where in a parse it went wrong. A closed vocabulary, which is the whole point.

    This is the only thing a parser may say beyond the cause, and it is an enum rather than a
    string so that the set of things it can say is fixed at review time. A free-text note here
    would be the field a document's contents end up in, whatever the docstring asked for.

    Each member is a stage an operator can act on: which part of the pipeline to look at when
    the same cause keeps arriving. The uploader never sees it, because it is a machine's note
    about our own code rather than anything about their file.
    """

    #: Opening the container at all. A password, a truncated header, not the format claimed.
    OPEN = "open"
    #: Working out the reading order and where the regions are.
    LAYOUT = "layout"
    #: Pulling the text out of the regions.
    TEXT = "text"
    #: Reading a table as a table rather than as scrambled prose.
    TABLES = "tables"
    #: The scanned-document path.
    OCR = "ocr"


@dataclass(frozen=True)
class ParseRefusal:
    """A parser saying it could not produce text, in the two words it is allowed.

    What is absent is the design, in the same way and for the same reason as `ParseFailure`.
    There is no field for the exception, no field for a fragment of the document and no field
    for a note. A parser that wants to explain itself has `ParseStage`, and if the stage it
    needs is not there, adding one is a reviewed change to a closed set rather than a sentence
    nobody reads until it is in a chat client.
    """

    cause: ParseCause
    stage: ParseStage


@dataclass(frozen=True)
class ParsedDocument:
    """Blocks, bound to the bytes they were read from.

    `digest` is attached by the gate rather than reported by the parser, for the same reason
    `ScanReport` carries no digest: a field a component fills in is a field it can fill in
    wrongly, and the failure here is text from one document being indexed under another
    document's permissions.

    `blocks` is `brain.knowledge.chunking.Block` rather than a shape of this module's own, so
    the parse output goes to `chunk_document` without a translation step. A translation step
    between two representations of the same thing is where the page numbers get lost, and a
    page number is half of what a citation resolves against.
    """

    digest: str
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if not self.blocks:
            msg = (
                "a parsed document with no blocks is a document that will answer no question "
                "while reading as indexed; that is a parse failure and has to be reported as "
                "one"
            )
            raise IngestRefused(msg)


class Parser(Protocol):
    """Turns cleared bytes into blocks, or says which stage refused to.

    **The parameter is `ScannedContent` and there is no overload taking bytes.** That is the
    ordering property, and it is the reason this protocol exists at all rather than a callable
    alias: the type in the signature is what makes "parse this unscanned buffer" unspellable.

    Failure is a return value, not an exception. An exception can be swallowed by a worker's
    `except Exception: continue`, and the symptom of that is a document somebody believes is
    searchable and is not, discovered months later as an answer that was merely thin. A union
    return type under mypy strict cannot be ignored without the ignore being visible.
    """

    def parse(self, content: ScannedContent) -> Sequence[Block] | ParseRefusal: ...


def _failure(content: ScannedContent, *, cause: ParseCause, stage: ParseStage) -> ParseFailure:
    """One place where a `ParseFailure` is built, so the fields cannot vary by call site.

    The filename and the media type come off the upload rather than from the parser, and the
    wording comes from `CAUSE_TEXT`. What the parser contributes to the sentence an uploader
    reads is one enum member, which is the property `A_PARSER_HAS_NO_FIELD_FOR_PROSE` names.
    """
    return ParseFailure(
        cause=cause,
        media_type=content.upload.media_type,
        filename=content.upload.filename,
        # A colon rather than an equals sign: `ingest._DETAIL_RE` does not admit one, and a
        # detail it refuses would raise out of the failure path, which is the failure path
        # failing to report a failure.
        detail=f"stage:{stage.value}",
    )


def parse_scanned(content: ScannedContent, *, parser: Parser) -> ParsedDocument | ParseFailure:
    """Parse cleared bytes, naming the cause when nothing comes out (M7.2.5).

    Two things happen here that a parser cannot be trusted to do for itself.

    A refusal is re-rendered, so the cause is the parser's and the wording is not. A parser
    that reported its own message would eventually report one containing the document.

    **An empty result is a failure, not an empty document.** This is the silent case the leaf
    exists to remove: nothing raised, nothing found, an item in the knowledge layer that
    retrieval never returns and nobody ever traces back to the day it was uploaded. Every
    accepted type that can legitimately produce no text produces it for the same reason, which
    is that there is no text layer to read, and that cause names the only remedy there is.
    """
    outcome = parser.parse(content)
    if isinstance(outcome, ParseRefusal):
        return _failure(content, cause=outcome.cause, stage=outcome.stage)
    blocks = tuple(outcome)
    if not blocks:
        return _failure(content, cause=ParseCause.NO_TEXT_LAYER, stage=ParseStage.TEXT)
    return ParsedDocument(digest=content.upload.digest, blocks=blocks)
