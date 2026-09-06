"""What is allowed in, in what order, and what a person is told when it will not parse.

Four decisions, and each one has a failure that is quiet rather than loud.

**The bytes decide the type, never the name.** A file called `quotation.pdf` that begins
`PK\\x03\\x04` is a zip, and handing it to a PDF parser is the cheapest way to spend a parse
worker's whole memory budget on something that was never a PDF. Sniffing settles the
container; it deliberately does not settle what is inside a zip, because that needs the zip
opened, and the place that opens it is the parser.

**The allowlist is an allowlist.** An unrecognised type is refused rather than attempted.
This matches the rule the rest of the platform runs on: there is no deny list anywhere, so
safety comes from what is named rather than from what somebody remembered to exclude.

**Scanning happens before parsing, and "could not scan" is not "clean".** A parser is a large
amount of untrusted-input-handling code, and reaching it before the scanner has finished is
the whole risk. `assert_clean` binds a verdict to the bytes it was reached about, by digest,
so a clean verdict cannot be carried over to a different upload.

**Backpressure refuses at the door.** The alternative, accepting everything and dropping work
when the queue is full, is the one failure nobody notices: the uploader is told the document
is in the knowledge layer, retrieval never finds it, and the answer is merely thin. A refusal
with a retry hint is worse to receive and far better to have.

**A parse failure names its cause.** Not because the cause is interesting, but because
"could not process this file" produces a support ticket, a re-upload of the identical file
and a second ticket. This is not the DENIED-collapsed-to-ABSENT case: the person reading the
message uploaded the file and already knows it exists, so naming the cause discloses nothing
they did not bring with them. The message deliberately has nowhere to put an excerpt of the
document, because an error message travels into logs, alerts and chat clients that the
document's own scope does not reach.

Nothing here opens a socket, spawns a scanner or reads a clock. These are the decisions; the
worker that acts on them is not written in this package.

Task ids: M7.1.1, M7.1.3, M7.1.5, M7.2.5
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from typing import Final

#: The largest anything may be, whatever its type. A second ceiling above the per-type ones,
#: because the per-type table is the thing somebody edits and a mistake there should not be
#: able to admit a file the storage tier cannot hold.
ABSOLUTE_MAX_BYTES: Final = 100 * 1024 * 1024

#: How many leading bytes a sniff needs. Every signature below fits inside this, and reading
#: more would mean holding more of an unscanned file in memory than the decision requires.
SNIFF_BYTES: Final = 16


class IngestRefused(Exception):  # noqa: N818 - the taxonomy in core.errors has no suffixes
    """An upload that will not be accepted, with a reason the uploader may read.

    Outside the `brain.core.errors` taxonomy, like the other refusals in this package. The
    distinction those five outcomes exist for does not apply here: the person being refused
    is the person who chose the file.
    """


class Container(enum.StrEnum):
    """What the leading bytes prove, which is less than what the extension claims."""

    PDF = "pdf"
    #: Any Office Open XML file, and any other zip. The bytes cannot tell a `.docx` from an
    #: `.xlsx` or from a zip of holiday photographs; only opening it can.
    ZIP = "zip"
    PNG = "png"
    JPEG = "jpeg"
    #: No signature, and no NUL bytes in the head. Weak evidence, and the only evidence a
    #: text format leaves.
    TEXT = "text"
    UNKNOWN = "unknown"


class MediaType(enum.StrEnum):
    """The types the knowledge layer accepts. An allowlist, and the whole allowlist."""

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    CSV = "text/csv"
    MARKDOWN = "text/markdown"
    PLAIN = "text/plain"
    PNG = "image/png"
    JPEG = "image/jpeg"


@dataclass(frozen=True)
class TypeLimit:
    """The container a type must actually be, and the largest it may be.

    The ceilings differ by type because parse cost does not follow file size across formats.
    Fifty megabytes of PDF is a scanned book and is expected; fifty megabytes of CSV is a
    database export somebody dragged in by mistake, and admitting it means discovering the
    mistake in the parse worker's memory limit rather than at the door.
    """

    container: Container
    max_bytes: int


#: Every accepted type, its container and its ceiling. A table rather than branches, so that
#: adding a format is one row somebody can review, and so that `MediaType` and this cannot
#: drift apart without the test that compares them failing.
TYPE_LIMITS: Final[dict[MediaType, TypeLimit]] = {
    MediaType.PDF: TypeLimit(Container.PDF, 50 * 1024 * 1024),
    MediaType.DOCX: TypeLimit(Container.ZIP, 25 * 1024 * 1024),
    MediaType.XLSX: TypeLimit(Container.ZIP, 25 * 1024 * 1024),
    MediaType.PPTX: TypeLimit(Container.ZIP, 50 * 1024 * 1024),
    MediaType.CSV: TypeLimit(Container.TEXT, 10 * 1024 * 1024),
    MediaType.MARKDOWN: TypeLimit(Container.TEXT, 5 * 1024 * 1024),
    MediaType.PLAIN: TypeLimit(Container.TEXT, 5 * 1024 * 1024),
    MediaType.PNG: TypeLimit(Container.PNG, 10 * 1024 * 1024),
    MediaType.JPEG: TypeLimit(Container.JPEG, 10 * 1024 * 1024),
}


def ceiling_for(media_type: MediaType) -> int:
    """The most this type may weigh, taking whichever of the two ceilings is lower.

    Both, because they answer different questions and the per-type table is the one somebody
    edits. `ABSOLUTE_MAX_BYTES` exists so that a mistake in a row cannot admit a file the
    storage tier cannot hold, and taking the minimum is what makes that true rather than
    aspirational.

    It lives beside the two constants it reads rather than in `brain.knowledge.uploads`,
    where it was written. The door is not the only thing that needs the door's answer:
    `brain.knowledge.parse_budget` asks what the largest admissible file is in order to size
    the container that parses it, and reaching `uploads` from there would close a cycle,
    because `uploads` imports `scanning` and `scanning` imports the budget. A second copy of
    a two-line rule was the alternative and it is the copy that would have drifted.
    """
    return min(TYPE_LIMITS[media_type].max_bytes, ABSOLUTE_MAX_BYTES)


#: Leading-byte signatures, longest first so that a longer signature is never shadowed by a
#: shorter one that happens to be a prefix of it.
_SIGNATURES: Final[tuple[tuple[bytes, Container], ...]] = (
    (b"\x89PNG\r\n\x1a\n", Container.PNG),
    (b"%PDF-", Container.PDF),
    (b"PK\x03\x04", Container.ZIP),
    (b"\xff\xd8\xff", Container.JPEG),
)


def sniff(head: bytes) -> Container:
    """What the leading bytes say the file is (M7.1.1).

    Text is the fallback rather than a signature, because text has none. The test is that the
    head decodes as UTF-8 and carries no NUL byte: a NUL is the one thing no text format
    contains and every binary format does, so it separates the two cases without pretending
    to identify either.
    """
    for signature, container in _SIGNATURES:
        if head.startswith(signature):
            return container
    if b"\x00" in head:
        return Container.UNKNOWN
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A truncated multi-byte character at the sniff boundary is not evidence of binary,
        # so a decode failure only decides the question when the head is short enough to be
        # the whole file.
        return Container.TEXT if len(head) >= SNIFF_BYTES else Container.UNKNOWN
    return Container.TEXT


@dataclass(frozen=True)
class AdmittedUpload:
    """An upload that passed the door, and the digest everything downstream is bound to.

    The digest is computed here rather than by the scanner or the parser, so that the thing
    scanned and the thing parsed are provably the thing admitted. Without it, a scan verdict
    is a statement about bytes nobody can name.
    """

    filename: str
    media_type: MediaType
    size_bytes: int
    digest: str


def admit_upload(*, filename: str, declared_type: str, content: bytes) -> AdmittedUpload:
    """Accept or refuse one upload on its type and its size (M7.1.1).

    The declared type is used only to choose which ceiling applies, and it is checked against
    the bytes before it is believed. A declared type outside the allowlist is refused without
    looking at the content at all, which is the point of an allowlist: nothing unrecognised
    reaches code that handles it.

    Note what is *not* checked: whether a zip is really a `.docx`. That question needs the
    archive opened, which is parsing, and parsing happens after scanning. Answering it here
    would mean opening an unscanned archive to decide whether to scan it.
    """
    try:
        media_type = MediaType(declared_type)
    except ValueError as exc:
        msg = (
            f"{declared_type!r} is not a type the knowledge layer accepts; "
            f"accepted types are {sorted(t.value for t in MediaType)}"
        )
        raise IngestRefused(msg) from exc

    if not content:
        msg = f"{filename!r} is empty; there is nothing in it to index"
        raise IngestRefused(msg)

    limit = TYPE_LIMITS[media_type]
    size = len(content)
    if size > ABSOLUTE_MAX_BYTES:
        msg = f"{filename!r} is {size} bytes, past the absolute ceiling of {ABSOLUTE_MAX_BYTES}"
        raise IngestRefused(msg)
    if size > limit.max_bytes:
        msg = (
            f"{filename!r} is {size} bytes and the ceiling for {media_type.value} is "
            f"{limit.max_bytes}"
        )
        raise IngestRefused(msg)

    found = sniff(content[:SNIFF_BYTES])
    if found is not limit.container:
        msg = (
            f"{filename!r} is declared as {media_type.value} but its contents are "
            f"{found.value}; the extension is not the type"
        )
        raise IngestRefused(msg)

    return AdmittedUpload(
        filename=filename,
        media_type=media_type,
        size_bytes=size,
        digest=hashlib.sha256(content).hexdigest(),
    )


# ------------------------------------------------- scanning before parsing (M7.1.3)


class ScanVerdict(enum.StrEnum):
    """What a scanner concluded, including the case where it concluded nothing.

    UNSCANNABLE is the member that earns this being an enum rather than a boolean. An
    encrypted archive, a scanner that timed out and a scanner that was not running all produce
    "we do not know", and a boolean forces that into either "clean" or "infected". Recording
    it as "clean" is the failure this whole ordering exists to prevent.
    """

    CLEAN = "clean"
    INFECTED = "infected"
    UNSCANNABLE = "unscannable"


@dataclass(frozen=True)
class ScanResult:
    """A verdict bound to the bytes it was reached about.

    The digest is what makes the binding real. A verdict recorded against a filename or an
    upload id can be reused after the content behind it changes, which turns a clean scan of
    version one into a clean scan of version two. A verdict recorded against a digest cannot.
    """

    digest: str
    verdict: ScanVerdict
    scanner: str
    detail: str = ""


def assert_clean(upload: AdmittedUpload, scan: ScanResult) -> None:
    """The gate between the object store and the parser (M7.1.3).

    Raises unless this upload's own bytes were scanned and found clean. Three refusals, and
    the third is the one that matters:

    A verdict about different bytes is not a verdict about these.

    Infected is refused, which needs no explanation.

    Unscannable is refused, which does. Treating "the scanner could not read it" as permission
    to parse means every file crafted to defeat a scanner is also a file that skips it, and
    the parser is exactly what such a file is aimed at.
    """
    if scan.digest != upload.digest:
        msg = (
            f"the scan on file is for {scan.digest[:12]} and this upload is "
            f"{upload.digest[:12]}; a verdict about other bytes is not a verdict about these"
        )
        raise IngestRefused(msg)
    if scan.verdict is ScanVerdict.INFECTED:
        msg = f"{upload.filename!r} was refused by {scan.scanner}"
        raise IngestRefused(msg)
    if scan.verdict is not ScanVerdict.CLEAN:
        msg = (
            f"{upload.filename!r} could not be scanned by {scan.scanner}, and unscanned is "
            "not clean; the parser is what an unscannable file is aimed at"
        )
        raise IngestRefused(msg)


# ---------------------------------------------------------- backpressure (M7.1.5)


@dataclass(frozen=True)
class QueueLimits:
    """How much ingestion work may be outstanding at once.

    Two numbers rather than one. Depth bounds what is waiting, which is a memory and a latency
    question; concurrency bounds what is running, which is a CPU and a parse-memory question.
    A single number would have to be set for whichever of the two binds first, and the other
    would then be either wasted or exceeded.
    """

    max_depth: int = 500
    max_in_flight: int = 4
    retry_after_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_depth <= 0 or self.max_in_flight <= 0:
            msg = "a queue with no depth or no workers accepts nothing and says it is full"
            raise IngestRefused(msg)
        if self.retry_after_seconds <= 0:
            msg = "a retry hint of zero seconds asks the client to retry immediately, forever"
            raise IngestRefused(msg)


@dataclass(frozen=True)
class QueueDecision:
    """Whether this upload joins the queue, and what to tell whoever sent it.

    There is no field for the queue depth or for a position in it, and that is the design. A
    position is a count of other people's work, it moves backwards as often as forwards, and
    a person watching it learns nothing they can act on. A duration is something they can.
    """

    admitted: bool
    retry_after_seconds: int = 0
    reason: str = ""


def admit_to_queue(*, depth: int, in_flight: int, limits: QueueLimits) -> QueueDecision:
    """Shed at the door when ingestion is saturated (M7.1.5).

    Refusing to accept is deliberately the only lever here. Accepting and dropping later is
    the failure that cannot be seen from outside: the uploader is told the document is in the
    knowledge layer, nothing is ever indexed from it, and the only symptom is an answer that
    is thinner than it should be, months later, with nothing to trace it to.
    """
    if depth >= limits.max_depth:
        return QueueDecision(
            admitted=False,
            retry_after_seconds=limits.retry_after_seconds,
            reason="ingestion is busy; this upload was not accepted and needs sending again",
        )
    if in_flight >= limits.max_in_flight:
        return QueueDecision(
            admitted=False,
            retry_after_seconds=limits.retry_after_seconds,
            reason="ingestion is busy; this upload was not accepted and needs sending again",
        )
    return QueueDecision(admitted=True)


# ------------------------------------------------------- parse failure (M7.2.5)


class ParseCause(enum.StrEnum):
    """Why a document could not be turned into text.

    Each member is a different thing for the uploader to do, which is the test for whether a
    cause belongs here. Two causes with the same remedy are one cause with two names, and the
    person reading the message cannot act on the difference.
    """

    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    #: The container opened and held something the parser does not handle, for instance a zip
    #: that is not an Office document after all. This is where the question `admit_upload`
    #: deliberately left unanswered comes back.
    UNSUPPORTED = "unsupported"
    #: A PDF with no text layer. It is not broken; it is a photograph of a document, and the
    #: remedy is the OCR path rather than a re-upload.
    NO_TEXT_LAYER = "no_text_layer"
    #: The OCR path ran and what came back is too unsure to index. Separate from
    #: `NO_TEXT_LAYER` because the remedy is the one thing that member's wording rules out: it
    #: tells the uploader the scanned-document path is what this file needs, and by the time
    #: this cause is produced that path has already run and answered badly. Added with
    #: `brain.knowledge.parse_ocr`, which is the only place that reaches it.
    ILLEGIBLE = "illegible"
    TIMED_OUT = "timed_out"
    #: The parse worker hit its memory ceiling. Named separately from a timeout because the
    #: remedy differs: a smaller document, not a second attempt.
    OUT_OF_MEMORY = "out_of_memory"
    PARSER_UNAVAILABLE = "parser_unavailable"


#: What each cause means and what to do about it. Every member of `ParseCause` has an entry,
#: which is asserted by a test rather than trusted: a cause added without wording renders as a
#: blank message, which is the generic failure this leaf exists to remove.
CAUSE_TEXT: Final[dict[ParseCause, str]] = {
    ParseCause.ENCRYPTED: (
        "the file is password-protected, so nothing could be read from it. "
        "Upload a copy with the password removed."
    ),
    ParseCause.CORRUPT: (
        "the file is damaged and could not be opened. "
        "Re-export it from the application it came from and upload it again."
    ),
    ParseCause.UNSUPPORTED: (
        "the file opened but is not a format the parser handles. "
        "Save it as PDF or Word and upload it again."
    ),
    ParseCause.NO_TEXT_LAYER: (
        "the file is a scan with no text in it. "
        "It needs the scanned-document path rather than a re-upload."
    ),
    ParseCause.ILLEGIBLE: (
        "the file is a scan and too little of it could be read to be worth searching. "
        "Upload a clearer scan, or a copy that has text in it rather than a photograph."
    ),
    ParseCause.TIMED_OUT: (
        "the file took longer to read than the parser is allowed. Upload it again, and if it "
        "fails a second time it needs splitting into smaller documents."
    ),
    ParseCause.OUT_OF_MEMORY: (
        "the file is too large for the parser to hold. "
        "Split it into smaller documents and upload those."
    ),
    ParseCause.PARSER_UNAVAILABLE: (
        "the parser was not reachable, so this file has not been read yet. "
        "Nothing is wrong with the file and it can be uploaded again shortly."
    ),
}

#: A parse failure's detail line. No newlines, and short: it is a machine's note about which
#: stage failed, and anything long enough to hold a sentence of the document is long enough to
#: leak one into a log.
_DETAIL_RE: Final = re.compile(r"^[A-Za-z0-9 _.:@/-]{0,120}$")


@dataclass(frozen=True)
class ParseFailure:
    """A parse that did not produce text, and the cause it is allowed to name (M7.2.5).

    What is absent from this class is the design. There is no field for a fragment of the
    document, no field for the raw parser exception and no field for a stack trace. A parse
    failure is rendered into a notification, a console row and a log line, and those three
    travel further than the document's own scope does. The cause is a fixed word from
    `ParseCause` and the detail is bounded to a machine's note about which stage failed.
    """

    cause: ParseCause
    media_type: MediaType
    filename: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not _DETAIL_RE.match(self.detail):
            msg = (
                f"parse detail {self.detail!r} is not a short machine note; a detail long "
                "enough to hold a sentence of the document leaks one into every log"
            )
            raise IngestRefused(msg)

    def message(self) -> str:
        """What the uploader is told. Names the cause and what to do about it.

        A message that says only "could not process this file" produces a support ticket, a
        re-upload of the identical file, and a second support ticket. The uploader chose this
        file, so naming why it failed tells them nothing they did not already have.
        """
        named = self.filename or "the file"
        return f"{named} could not be read: {CAUSE_TEXT[self.cause]}"

    @property
    def is_retryable(self) -> bool:
        """Whether sending the same bytes again could succeed.

        Only the two causes that are about this system rather than about the document. Marking
        a corrupt file retryable produces a queue full of work that cannot ever succeed, and
        the retries crowd out the uploads that would.
        """
        return self.cause in (ParseCause.TIMED_OUT, ParseCause.PARSER_UNAVAILABLE)
