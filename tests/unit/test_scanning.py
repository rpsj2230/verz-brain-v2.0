"""The ordering that has to be a type, and the failure that has to name its cause.

Every test here is either a way to reach a parser without a scanner having run, or a way for
something a parser said to end up in front of a person. The door itself is tested in
`test_knowledge_ingest.py` and is not re-tested here.

Several of these assert on the *shape* of a type rather than on behaviour, which is deliberate.
"ParseRefusal has no string field" is not something a call can demonstrate; it is a property of
the class, and the regression is somebody adding the field, so the test has to read the fields.

Task ids: M7.1.3, M7.2.5
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
from typing import get_type_hints

import pytest

from brain.knowledge.chunking import Block, BlockKind
from brain.knowledge.ingest import (
    AdmittedUpload,
    IngestRefused,
    MediaType,
    ParseCause,
    ParseFailure,
    ScanResult,
    ScanVerdict,
    admit_upload,
)
from brain.knowledge.scanning import (
    ParsedDocument,
    Parser,
    ParseRefusal,
    ParseStage,
    ScannedContent,
    ScanReport,
    parse_scanned,
    scan_for_parsing,
)

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"
OTHER_PDF = b"%PDF-1.7\n2 0 obj\n<< /Type /Catalog >>\nendobj\n"


def _admitted(content: bytes = PDF, filename: str = "sop.pdf") -> AdmittedUpload:
    return admit_upload(filename=filename, declared_type=MediaType.PDF.value, content=content)


class FakeScanner:
    """Whatever verdict the test asks for, and a count of whether it was ever consulted.

    The count is what proves the order of the checks inside the gate: a refusal that spends a
    scan is a refusal that costs what admitting would have.
    """

    def __init__(self, verdict: ScanVerdict = ScanVerdict.CLEAN, name: str = "fake-av") -> None:
        self.verdict = verdict
        self.name = name
        self.calls = 0
        self.seen: list[bytes] = []

    def scan(self, content: bytes) -> ScanReport:
        self.calls += 1
        self.seen.append(content)
        return ScanReport(verdict=self.verdict, scanner=self.name)


class FakeParser:
    """Returns whatever the test scripted, and records what it was handed."""

    def __init__(self, outcome: list[Block] | ParseRefusal) -> None:
        self.outcome = outcome
        self.seen: list[ScannedContent] = []

    def parse(self, content: ScannedContent) -> list[Block] | ParseRefusal:
        self.seen.append(content)
        return self.outcome


def _blocks() -> list[Block]:
    return [Block(kind=BlockKind.PROSE, text="Escalate a P1 within thirty minutes.", start=0)]


def _cleared(content: bytes = PDF) -> ScannedContent:
    return scan_for_parsing(_admitted(content), content, scanner=FakeScanner())


# ------------------------------------------------- scanning before parsing (M7.1.3)
def test_a_clean_file_becomes_parseable() -> None:
    """The positive case for every refusal below. Without it a gate that refused every file
    would satisfy all of them, and a knowledge layer that accepts nothing passes its own
    safety tests perfectly."""
    upload = _admitted()
    scanner = FakeScanner()

    cleared = scan_for_parsing(upload, PDF, scanner=scanner)

    assert cleared.body == PDF
    assert cleared.upload == upload
    assert cleared.scan.verdict is ScanVerdict.CLEAN
    assert cleared.scan.scanner == "fake-av"
    assert scanner.seen == [PDF]


def test_an_infected_file_never_becomes_parseable() -> None:
    """Deleting this leaves the one refusal everybody assumes is there untested. An infected
    file that reaches a parser has reached the code the file was written for."""
    with pytest.raises(IngestRefused, match="was refused by"):
        scan_for_parsing(_admitted(), PDF, scanner=FakeScanner(ScanVerdict.INFECTED))


def test_a_file_the_scanner_could_not_read_never_becomes_parseable() -> None:
    """This is the one that gets relaxed. An encrypted archive, a scanner that timed out and a
    scanner that was not running all say "we do not know", and reading that as clean means
    every file crafted to defeat a scanner also skips it."""
    with pytest.raises(IngestRefused, match="could not be scanned by"):
        scan_for_parsing(_admitted(), PDF, scanner=FakeScanner(ScanVerdict.UNSCANNABLE))


def test_bytes_that_are_not_the_bytes_admitted_are_refused_before_a_scanner_is_asked() -> None:
    """Without this the gate would scan whatever it was handed and bind a clean verdict to a
    file the door never measured, sniffed or sized. It also pins the order: a scan is not
    spent on a buffer that was already known to be the wrong one."""
    scanner = FakeScanner()

    with pytest.raises(IngestRefused, match="checked at the door"):
        scan_for_parsing(_admitted(PDF), OTHER_PDF, scanner=scanner)

    assert scanner.calls == 0


def test_cleared_content_cannot_be_constructed_outside_the_gate() -> None:
    """The type in `Parser.parse` stops the ordinary mistake; this stops the deliberate one,
    which is importing the type and wrapping it round an unscanned buffer. Delete it and the
    ordering is back to being a convention with a type annotation on top."""
    upload = _admitted()
    scan = ScanResult(digest=upload.digest, verdict=ScanVerdict.CLEAN, scanner="fake-av")

    with pytest.raises(IngestRefused, match="issued by scan_for_parsing"):
        ScannedContent(issued_by=object(), upload=upload, body=PDF, scan=scan)


def test_cleared_content_cannot_be_subclassed_past_its_own_check() -> None:
    """A subclass with its own `__post_init__` satisfies `Parser.parse` and skips the seal,
    which is a four-line bypass of the whole module. `@final` is what makes that a type error,
    and nothing else in the file would fail if the decorator were removed."""
    # Read with `getattr` because the attribute is set by the decorator at runtime and is not
    # in the class's declared interface, so naming it directly is an error to mypy.
    assert getattr(ScannedContent, "__final__", False) is True


def test_a_parser_is_declared_to_take_cleared_content_and_not_bytes() -> None:
    """This is the ordering property itself, and it lives in a signature rather than in a
    call. Widening the parameter to `bytes | ScannedContent` would make every other test here
    still pass while making an unscanned parse spellable again."""
    hints = get_type_hints(Parser.parse)

    assert hints["content"] is ScannedContent


def test_a_scan_verdict_names_the_scanner_that_reached_it() -> None:
    """When the question a year from now is why this file was let through, the answer is a
    name. An anonymous verdict cannot be argued with, and cannot be re-run."""
    with pytest.raises(IngestRefused, match="no scanner named"):
        ScanReport(verdict=ScanVerdict.CLEAN, scanner="   ")


def test_a_scanner_cannot_choose_the_digest_its_verdict_is_bound_to() -> None:
    """The binding between a verdict and the bytes is the whole of `assert_clean`, and it is
    worth nothing if the component that picks the verdict also picks the digest it is compared
    against. Adding a digest field to `ScanReport` would turn that comparison into a value
    being compared with itself, and no behavioural test would notice."""
    assert {f.name for f in dataclasses.fields(ScanReport)} == {"verdict", "scanner"}


# ------------------------------------------------------ the parse failure (M7.2.5)
def test_a_parser_has_no_field_through_which_document_text_could_travel() -> None:
    """Bounding a detail by length and character set is not enough: 120 characters of letters
    and spaces is a whole sentence of a quotation, and a parse failure is read in logs and chat
    clients the document's scope does not reach. A closed vocabulary is what makes the leak
    impossible rather than unlikely, and the regression is a `detail: str` field."""
    hints = get_type_hints(ParseRefusal)

    assert hints
    assert all(isinstance(t, type) and issubclass(t, enum.Enum) for t in hints.values())


def test_a_parse_failure_reports_the_cause_the_parser_named() -> None:
    """Unsupported, corrupt and encrypted have three different remedies. Collapsing them into
    one message produces a support ticket, a re-upload of the identical file, and a second
    support ticket."""
    cleared = _cleared()
    parser = FakeParser(ParseRefusal(cause=ParseCause.UNSUPPORTED, stage=ParseStage.OPEN))

    outcome = parse_scanned(cleared, parser=parser)

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.UNSUPPORTED
    assert outcome.filename == "sop.pdf"
    assert outcome.media_type is MediaType.PDF
    assert outcome.is_retryable is False


def test_a_refused_file_is_raised_rather_than_returned_as_a_parse_failure() -> None:
    """The two routes are the distinction. Reporting an infected file as a corrupt one tells
    the person to re-export it and send it again, which is the worst advice available: the
    remedy for a refusal is not a second attempt at the same file."""
    with pytest.raises(IngestRefused):
        scan_for_parsing(_admitted(), PDF, scanner=FakeScanner(ScanVerdict.INFECTED))


def test_a_parse_that_produced_nothing_is_a_failure_rather_than_an_empty_document() -> None:
    """This is the silent case the leaf exists to remove. Nothing raises, nothing is found,
    and an item sits in the knowledge layer that retrieval never returns; the only symptom is
    an answer that is thinner than it should be, months later, with nothing to trace it to."""
    outcome = parse_scanned(_cleared(), parser=FakeParser([]))

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.NO_TEXT_LAYER


def test_a_parsed_document_is_bound_to_the_bytes_it_was_read_from() -> None:
    """The digest is attached by the gate rather than reported by the parser. Without it,
    blocks from one document could be indexed under another document's permissions and the
    result would look exactly like a correct parse."""
    cleared = _cleared()
    parser = FakeParser(_blocks())

    outcome = parse_scanned(cleared, parser=parser)

    assert isinstance(outcome, ParsedDocument)
    assert outcome.digest == hashlib.sha256(PDF).hexdigest()
    assert outcome.blocks == tuple(_blocks())
    assert parser.seen == [cleared]


def test_the_message_an_uploader_reads_carries_nothing_the_parser_wrote() -> None:
    """The parser contributes one enum member to the sentence and the wording comes from
    `CAUSE_TEXT`. Delete this and a future parser can put its own prose, and eventually the
    document's, into a notification."""
    outcome = parse_scanned(
        _cleared(), parser=FakeParser(ParseRefusal(ParseCause.CORRUPT, ParseStage.OPEN))
    )

    assert isinstance(outcome, ParseFailure)
    assert outcome.detail == f"stage:{ParseStage.OPEN.value}"
    assert outcome.message() == f"sop.pdf could not be read: {_corrupt_wording()}"


def _corrupt_wording() -> str:
    from brain.knowledge.ingest import CAUSE_TEXT

    return CAUSE_TEXT[ParseCause.CORRUPT]


def test_every_parse_stage_renders_into_a_detail_the_failure_type_accepts() -> None:
    """`ingest._DETAIL_RE` admits colons and not equals signs, so `stage=open` raises out of
    the failure path, which is the failure path failing to report a failure. This is the test
    that catches a stage added later whose name does not fit, or the separator being changed
    back to the one that reads more naturally."""
    for stage in ParseStage:
        outcome = parse_scanned(
            _cleared(), parser=FakeParser(ParseRefusal(ParseCause.TIMED_OUT, stage))
        )

        assert isinstance(outcome, ParseFailure)
        assert outcome.detail == f"stage:{stage.value}"


def test_a_parsed_document_cannot_be_built_with_no_blocks() -> None:
    """Belt and braces under `parse_scanned`'s own check, and the one that survives a future
    second caller: a document with no blocks reads as indexed and answers nothing."""
    with pytest.raises(IngestRefused, match="no blocks"):
        ParsedDocument(digest="a" * 64, blocks=())
