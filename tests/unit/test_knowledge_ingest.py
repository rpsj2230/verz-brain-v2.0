"""The door, the order scanning and parsing happen in, and what a failure is allowed to say.

Task ids: M7.1.1, M7.1.3, M7.1.5, M7.2.5
"""

from __future__ import annotations

import hashlib

import pytest

from brain.knowledge.ingest import (
    ABSOLUTE_MAX_BYTES,
    CAUSE_TEXT,
    TYPE_LIMITS,
    AdmittedUpload,
    Container,
    IngestRefused,
    MediaType,
    ParseCause,
    ParseFailure,
    QueueLimits,
    ScanResult,
    ScanVerdict,
    admit_to_queue,
    admit_upload,
    assert_clean,
    sniff,
)

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"
ZIP = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
CSV = b"sku,sell_price\nPKG-CARE-1,1200\n"


def _admitted(content: bytes = PDF) -> AdmittedUpload:
    return admit_upload(filename="sop.pdf", declared_type=MediaType.PDF.value, content=content)


# ------------------------------------------------------------- the door (M7.1.1)
def test_a_type_outside_the_allowlist_is_refused() -> None:
    """An allowlist is the whole safety property: nothing unrecognised reaches code that
    handles it. A denylist would need somebody to have thought of the format in advance, which
    is the assumption every archive-format bug is built on."""
    with pytest.raises(IngestRefused, match="not a type the knowledge layer accepts"):
        admit_upload(filename="x.exe", declared_type="application/x-dosexec", content=b"MZ")


def test_a_file_whose_bytes_disagree_with_its_declared_type_is_refused() -> None:
    """The extension is not the type. A zip renamed to `.pdf` and handed to a PDF parser is
    the cheapest way to spend a parse worker's whole memory budget on something that was never
    a PDF."""
    with pytest.raises(IngestRefused, match="the extension is not the type"):
        admit_upload(filename="quotation.pdf", declared_type=MediaType.PDF.value, content=ZIP)


def test_an_empty_file_is_refused() -> None:
    """There is nothing in it to index, so it would upload cleanly, parse to nothing and sit
    in the knowledge layer as a document that answers no question. Nobody would look for the
    reason, because nothing failed."""
    with pytest.raises(IngestRefused, match="is empty"):
        admit_upload(filename="empty.pdf", declared_type=MediaType.PDF.value, content=b"")


def test_a_file_past_its_type_s_ceiling_is_refused() -> None:
    """Parse cost does not follow file size across formats. Five megabytes of markdown is a
    database export somebody dragged in, and admitting it means finding out in the parse
    worker's memory limit rather than at the door."""
    oversized = b"#" * (TYPE_LIMITS[MediaType.MARKDOWN].max_bytes + 1)
    with pytest.raises(IngestRefused, match="the ceiling for text/markdown"):
        admit_upload(
            filename="export.md", declared_type=MediaType.MARKDOWN.value, content=oversized
        )


def test_every_accepted_type_has_a_ceiling_and_a_container() -> None:
    """A type in the enum with no row in the table raises a KeyError at the door, which fails
    every upload of that type with a stack trace rather than a refusal. The two must not drift
    apart."""
    assert set(TYPE_LIMITS) == set(MediaType)
    assert all(limit.max_bytes <= ABSOLUTE_MAX_BYTES for limit in TYPE_LIMITS.values())


def test_the_sniffer_reads_the_container_and_not_the_subtype() -> None:
    """A `.docx` and an `.xlsx` are both zips and the leading bytes cannot tell them apart.
    Pretending otherwise would mean opening an archive to decide whether to scan it, which is
    the ordering this module exists to keep."""
    assert sniff(PDF) is Container.PDF
    assert sniff(ZIP) is Container.ZIP
    assert sniff(PNG) is Container.PNG
    assert sniff(CSV) is Container.TEXT


def test_a_file_with_nul_bytes_is_never_text() -> None:
    """A NUL is the one thing no text format contains and most binary formats do. Without this
    check, any unrecognised binary would pass as `text/plain` and be handed to a text parser
    as though it were a CSV."""
    assert sniff(b"sku,sell\x00price") is Container.UNKNOWN


def test_an_admitted_upload_carries_the_digest_of_its_own_bytes() -> None:
    """Everything downstream is bound to this. Without it a scan verdict is a statement about
    bytes nobody can name, and a clean verdict can be carried from one upload to another."""
    assert _admitted().digest == hashlib.sha256(PDF).hexdigest()


# -------------------------------------------- scanning before parsing (M7.1.3)
def test_an_infected_file_never_reaches_the_parser() -> None:
    """The obvious half, and the one that is easy to get right. It is here so that the two
    below are read as the same rule rather than as exceptions to it."""
    scan = ScanResult(digest=_admitted().digest, verdict=ScanVerdict.INFECTED, scanner="clamd")
    with pytest.raises(IngestRefused, match="refused by clamd"):
        assert_clean(_admitted(), scan)


def test_an_unscannable_file_is_not_treated_as_clean() -> None:
    """The half that gets lost. An encrypted archive, a scanner timeout and a scanner that was
    not running all mean "we do not know", and recording that as clean means every file
    crafted to defeat a scanner is also a file that skips it."""
    scan = ScanResult(digest=_admitted().digest, verdict=ScanVerdict.UNSCANNABLE, scanner="clamd")
    with pytest.raises(IngestRefused, match="unscanned is not clean"):
        assert_clean(_admitted(), scan)


def test_a_verdict_about_other_bytes_is_not_a_verdict_about_these() -> None:
    """A verdict recorded against a filename or an upload id can be reused after the content
    behind it changes, which turns a clean scan of version one into a clean scan of version
    two. The digest is what makes the binding real."""
    scan = ScanResult(
        digest=hashlib.sha256(b"something else").hexdigest(),
        verdict=ScanVerdict.CLEAN,
        scanner="clamd",
    )
    with pytest.raises(IngestRefused, match="not a verdict about these"):
        assert_clean(_admitted(), scan)


def test_a_clean_scan_of_these_bytes_opens_the_gate() -> None:
    """The happy path. If it failed, nothing would ever be parsed and the workaround would be
    somebody calling the parser directly, which removes the ordering altogether."""
    scan = ScanResult(digest=_admitted().digest, verdict=ScanVerdict.CLEAN, scanner="clamd")
    assert_clean(_admitted(), scan)  # the assertion is that this does not raise


# --------------------------------------------------------- backpressure (M7.1.5)
def test_a_full_queue_refuses_rather_than_accepting_and_dropping() -> None:
    """Accepting and dropping later is the failure that cannot be seen from outside: the
    uploader is told the document is in the knowledge layer, nothing is indexed from it, and
    the only symptom is an answer that is thinner than it should be, months later."""
    limits = QueueLimits(max_depth=10, max_in_flight=4)
    decision = admit_to_queue(depth=10, in_flight=0, limits=limits)
    assert not decision.admitted
    assert "not accepted" in decision.reason


def test_saturated_workers_shed_even_when_the_queue_is_short() -> None:
    """Depth and concurrency bind at different times: depth is a memory and latency question,
    concurrency is a parse-memory one. A single number would be set for whichever binds first
    and the other would be exceeded."""
    limits = QueueLimits(max_depth=100, max_in_flight=2)
    assert not admit_to_queue(depth=0, in_flight=2, limits=limits).admitted


def test_a_refusal_hands_back_a_duration_and_never_a_position() -> None:
    """A queue position is a count of other people's work, it moves backwards as often as
    forwards, and a person watching it learns nothing they can act on. `QueueDecision` has
    nowhere to put one."""
    limits = QueueLimits(max_depth=1, max_in_flight=1, retry_after_seconds=30)
    decision = admit_to_queue(depth=5, in_flight=0, limits=limits)
    assert decision.retry_after_seconds == 30
    assert "5" not in decision.reason


def test_a_queue_with_no_workers_is_refused_at_configuration_time() -> None:
    """It accepts nothing and reports that it is full, so the symptom is a knowledge layer
    that has stopped ingesting with every component reporting healthy."""
    with pytest.raises(IngestRefused, match="accepts nothing"):
        QueueLimits(max_depth=10, max_in_flight=0)


def test_a_zero_second_retry_hint_is_refused() -> None:
    """It asks the client to retry immediately, forever, which turns a busy queue into a
    denial of service the client performs on itself."""
    with pytest.raises(IngestRefused, match="retry immediately"):
        QueueLimits(retry_after_seconds=0)


def test_an_upload_joins_the_queue_when_there_is_room() -> None:
    """The happy path, and the one that stops a tightened limit from silently closing
    ingestion for everybody."""
    decision = admit_to_queue(depth=0, in_flight=0, limits=QueueLimits())
    assert decision.admitted
    assert decision.retry_after_seconds == 0


# --------------------------------------------------- the parse failure (M7.2.5)
def test_a_parse_failure_names_its_cause_and_what_to_do() -> None:
    """ "Could not process this file" produces a support ticket, a re-upload of the identical
    file, and a second support ticket. The uploader chose the file, so naming why it failed
    tells them nothing they did not already have."""
    failure = ParseFailure(
        cause=ParseCause.ENCRYPTED, media_type=MediaType.PDF, filename="contract.pdf"
    )
    message = failure.message()
    assert "contract.pdf" in message
    assert "password" in message


def test_every_parse_cause_has_wording() -> None:
    """A cause added without a sentence renders as a blank message or a KeyError, which is the
    generic failure this leaf exists to remove, reintroduced by somebody extending the enum."""
    assert set(CAUSE_TEXT) == set(ParseCause)
    assert all(text.strip() for text in CAUSE_TEXT.values())


def test_a_parse_failure_cannot_carry_a_fragment_of_the_document() -> None:
    """A failure is rendered into a notification, a console row and a log line, and those
    travel further than the document's own scope does. The detail is bounded so it cannot hold
    a sentence of the content."""
    with pytest.raises(IngestRefused, match="not a short machine note"):
        ParseFailure(
            cause=ParseCause.CORRUPT,
            media_type=MediaType.PDF,
            detail="failed at: The contract value for SNM is 240,000 and the term is 24 months",
        )


def test_only_failures_about_this_system_are_retryable() -> None:
    """Marking a corrupt file retryable fills the queue with work that can never succeed, and
    those retries crowd out the uploads that would."""
    unavailable = ParseFailure(cause=ParseCause.PARSER_UNAVAILABLE, media_type=MediaType.PDF)
    corrupt = ParseFailure(cause=ParseCause.CORRUPT, media_type=MediaType.PDF)
    assert unavailable.is_retryable
    assert not corrupt.is_retryable


def test_a_scan_with_no_text_layer_is_not_reported_as_broken() -> None:
    """It is a photograph of a document, not a damaged file, and the remedy is the scanned
    document path rather than a re-upload. Conflating them sends the uploader round a loop
    that cannot succeed."""
    failure = ParseFailure(cause=ParseCause.NO_TEXT_LAYER, media_type=MediaType.PDF)
    assert "scan" in failure.message()
    assert not failure.is_retryable
