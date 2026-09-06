"""What a route may be, which failures let it try again, and what the result records.

Every test here is either a way for a document to end up on a weaker parser without anything
saying so, or a way for a route to run in an order that produces a worse corpus with no error.
Those are the two failures M7.2.3 exists to prevent and neither of them raises anything.

The single-parser story is tested in `test_scanning.py` and the memory bound in
`test_parse_budget.py`; neither is re-tested here. What is tested here is the part that only
exists once there is more than one parser.

Task ids: M7.2.3
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from brain.knowledge.chunking import Block, BlockKind
from brain.knowledge.ingest import (
    AdmittedUpload,
    IngestRefused,
    MediaType,
    ParseCause,
    ParseFailure,
    ScanVerdict,
    admit_upload,
)
from brain.knowledge.parse_paths import (
    MAY_TRY_ANOTHER_PATH,
    PATH_RANK,
    AttributedParse,
    ParsePath,
    ParseProvenance,
    PathAttempt,
    RoutedParser,
    may_try_another_path,
    parse_by_route,
    route_refusals,
)
from brain.knowledge.scanning import (
    ParsedDocument,
    ParseRefusal,
    ParseStage,
    ScannedContent,
    ScanReport,
    scan_for_parsing,
)

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"


def _admitted(content: bytes = PDF, filename: str = "sop.pdf") -> AdmittedUpload:
    return admit_upload(filename=filename, declared_type=MediaType.PDF.value, content=content)


class _CleanScanner:
    def scan(self, content: bytes) -> ScanReport:
        return ScanReport(verdict=ScanVerdict.CLEAN, scanner="fake-av")


def _cleared(content: bytes = PDF) -> ScannedContent:
    return scan_for_parsing(_admitted(content), content, scanner=_CleanScanner())


def _block(text: str = "a paragraph", start: int = 0) -> Block:
    return Block(kind=BlockKind.PROSE, text=text, start=start)


class FakeParser:
    """Returns whatever the test scripted, and counts whether it was ever asked.

    The count is what proves a route stopped rather than carried on: a fallback that did not
    happen and one that happened and produced the same failure are the same value and are not
    the same event.
    """

    def __init__(self, outcome: list[Block] | ParseRefusal) -> None:
        self.outcome = outcome
        self.calls = 0

    def parse(self, content: ScannedContent) -> list[Block] | ParseRefusal:
        self.calls += 1
        return self.outcome


def _entry(path: ParsePath, outcome: list[Block] | ParseRefusal, engine: str = "") -> RoutedParser:
    return RoutedParser(
        provenance=ParseProvenance(path=path, engine=engine or f"fake-{path.value}"),
        parser=FakeParser(outcome),
    )


# ------------------------------------------------------------------ the provenance
def test_a_parse_provenance_will_not_hold_an_engine_name_that_is_really_prose() -> None:
    """The engine is recorded beside every passage it read and would go into a column the day
    one exists. Delete this and the field takes any string at all, which is where a vendor's
    version banner goes first and a line of somebody's contract goes second, exactly as
    `A_PARSER_HAS_NO_FIELD_FOR_PROSE` describes one layer down."""
    with pytest.raises(IngestRefused, match="is not a name"):
        ParseProvenance(path=ParsePath.LAYOUT, engine="Docling 2.1.0: read 'confidential'")


def test_a_parse_provenance_accepts_the_engine_names_this_repository_actually_uses() -> None:
    """The positive case beside the refusal. A validator tested only by what it rejects is
    satisfied by one that rejects everything, and the symptom of that would be that no route
    can be constructed at all."""
    for engine in ("docling-layout-and-tableformer", "docling-ocr", "tika", "tesseract5"):
        assert ParseProvenance(path=ParsePath.LAYOUT, engine=engine).engine == engine


def test_a_provenance_line_names_our_own_components_and_nothing_from_the_file() -> None:
    """`describe` is printed into an operator's log. Delete this and somebody adds the
    filename to it "so we can tell which document", which puts an attacker-chosen string into
    a log line, which is the miss `brain.gate.injection._normalise` was written for."""
    described = ParseProvenance(path=ParsePath.OCR, engine="docling-ocr").describe()

    assert described == "ocr by docling-ocr"


# ------------------------------------------------------------------ which failures may retry
def test_no_failure_a_retry_would_fix_is_ever_allowed_to_reach_a_weaker_parser() -> None:
    """The rule the whole leaf turns on, asserted against `ParseFailure.is_retryable` rather
    than against a second list here. A cause is retryable because it is a fact about this
    system, and falling back on one converts an outage into a permanent quality change for
    every document uploaded during it.

    Delete this and `MAY_TRY_ANOTHER_PATH` can be widened to include PARSER_UNAVAILABLE, which
    is the single most tempting entry in the whole enum and the one with no symptom."""
    retryable = {
        cause
        for cause in ParseCause
        if ParseFailure(cause=cause, media_type=MediaType.PDF).is_retryable
    }

    assert retryable, "is_retryable answered no for every cause; the anchor has gone"
    assert MAY_TRY_ANOTHER_PATH.isdisjoint(retryable)


def test_a_file_refused_before_any_parser_ran_is_not_handed_to_a_second_one() -> None:
    """`OUT_OF_MEMORY` is produced by `parse_scanned` at `ParseStage.ADMIT`, so no parser was
    called and the second one gets the identical file and the identical budget. Delete this and
    a 50 MiB document spends every parser in the route refusing it in turn."""
    assert ParseCause.OUT_OF_MEMORY not in MAY_TRY_ANOTHER_PATH
    assert not may_try_another_path(ParseCause.OUT_OF_MEMORY)


def test_the_set_that_may_fall_back_is_neither_empty_nor_the_whole_taxonomy() -> None:
    """Both degenerate values pass every other test in this file. Empty means no fallback ever
    happens and the leaf is decoration; the whole enum means every failure downgrades the
    document, including the ones that are about this system. Delete this and either is
    reachable by an edit that looks like tidying."""
    every_cause = frozenset(ParseCause)
    outside = every_cause - MAY_TRY_ANOTHER_PATH

    assert MAY_TRY_ANOTHER_PATH, "no cause falls back, so the leaf does nothing"
    assert outside, "every cause falls back, including the ones that are about this system"
    assert MAY_TRY_ANOTHER_PATH.issubset(every_cause)


@pytest.mark.parametrize("cause", sorted(ParseCause))
def test_a_route_tries_a_second_parser_exactly_when_the_cause_permits_it(
    cause: ParseCause,
) -> None:
    """The behaviour behind the set rather than a second copy of the set, run over every member
    of the taxonomy so that adding a cause without deciding about it is a failure here.

    Delete this and `MAY_TRY_ANOTHER_PATH` and `parse_by_route` can disagree: the set says a
    cause falls back, the loop does not, and nothing anywhere compares them."""
    second = _entry(ParsePath.FALLBACK, [_block()])
    route = [_entry(ParsePath.LAYOUT, ParseRefusal(cause=cause, stage=ParseStage.OPEN)), second]

    outcome = parse_by_route(_cleared(), route=route)
    consulted = second.parser.calls  # type: ignore[attr-defined]

    if cause in MAY_TRY_ANOTHER_PATH:
        assert consulted == 1
        assert isinstance(outcome, AttributedParse)
    else:
        assert consulted == 0
        assert isinstance(outcome, ParseFailure)
        assert outcome.cause is cause


# ------------------------------------------------------------------ the order of a route
def test_every_path_has_a_rank_so_a_route_can_always_be_ordered() -> None:
    """`route_refusals` orders a route by `PATH_RANK`. A path missing from that table raises a
    KeyError inside the check, which is a guard that fails rather than a guard that refuses.

    Delete this and a fourth path added to `ParsePath` makes every route check explode at the
    call site instead of reporting anything."""
    assert set(PATH_RANK) == set(ParsePath)
    assert len(set(PATH_RANK.values())) == len(ParsePath), "two paths share a rank"


def test_the_guessing_path_ranks_last_and_the_layout_path_ranks_first() -> None:
    """The property the numbers exist for, asserted rather than the numbers themselves. OCR
    before a text read replaces characters that were in the file with characters somebody
    guessed; a plain extractor before the layout parser indexes the worse reading of every
    document both can read. Neither produces an error.

    Delete this and the ranks can be permuted and the suite stays green."""
    assert PATH_RANK[ParsePath.OCR] == max(PATH_RANK.values())
    assert PATH_RANK[ParsePath.LAYOUT] == min(PATH_RANK.values())


def test_a_route_that_would_guess_before_it_reads_is_refused_before_a_byte_is_parsed() -> None:
    """The behavioural half of the rank. Delete this and a route assembled OCR-first runs, every
    scanned-looking PDF is indexed as recognised glyphs, and there is nothing in the corpus or
    the logs that distinguishes that from a correct parse."""
    findings = route_refusals(
        [_entry(ParsePath.OCR, [_block()]), _entry(ParsePath.LAYOUT, [_block()])]
    )

    assert len(findings) == 1
    assert "before either replaces text that was read with glyphs that were guessed" in findings[0]


def test_a_route_in_the_right_order_is_not_refused() -> None:
    """The positive case. A check tested only by what it rejects is satisfied by one that
    rejects every route, and the symptom would be that parsing never happens at all."""
    assert (
        route_refusals(
            [
                _entry(ParsePath.LAYOUT, [_block()]),
                _entry(ParsePath.FALLBACK, [_block()]),
                _entry(ParsePath.OCR, [_block()]),
            ]
        )
        == ()
    )


def test_a_route_with_no_parsers_is_refused_rather_than_reported_as_a_bad_document() -> None:
    """An empty route would otherwise fall out of the loop as a failure whose cause is about
    the file. Delete this and a deployment with nothing configured tells every uploader their
    document could not be read, which is a support ticket per upload for a configuration
    mistake."""
    findings = route_refusals([])

    assert len(findings) == 1
    assert "no parsers" in findings[0]


def test_two_engines_behind_one_path_are_refused_because_the_record_would_be_ambiguous() -> None:
    """The path is what a reader is told. Delete this and a route can hold two engines on the
    same path, both readings are recorded as the same kind of evidence, and "this passage was
    read by the fallback" stops identifying which program produced it."""
    findings = route_refusals(
        [
            _entry(ParsePath.FALLBACK, [_block()], engine="tika"),
            _entry(ParsePath.FALLBACK, [_block()], engine="tesseract5"),
        ]
    )

    assert any("more than once" in finding for finding in findings)


def test_parse_by_route_refuses_a_bad_route_instead_of_running_it() -> None:
    """`route_refusals` is advice until something applies it. Delete this and the function
    exists, is tested, and is never consulted by the one caller that matters, which is the
    twelfth instance of that in this repository."""
    parser = FakeParser([_block()])
    route = [RoutedParser(ParseProvenance(ParsePath.OCR, "docling-ocr"), parser)]

    with pytest.raises(IngestRefused):
        parse_by_route(_cleared(), route=[*route, _entry(ParsePath.LAYOUT, [_block()])])
    assert parser.calls == 0


# ------------------------------------------------------------------ what the result records
def test_a_document_read_by_the_first_path_records_that_nothing_was_displaced() -> None:
    """The positive case, and the one that stops `fell_back` reading true for every parse.
    Delete this and a route could record a fallback on documents the primary parser read, which
    would make the flag noise and stop anybody looking at it."""
    outcome = parse_by_route(_cleared(), route=[_entry(ParsePath.LAYOUT, [_block()])])

    assert isinstance(outcome, AttributedParse)
    assert outcome.provenance.path is ParsePath.LAYOUT
    assert outcome.refused == ()
    assert outcome.fell_back is False


def test_a_document_that_fell_back_records_which_path_gave_up_and_why() -> None:
    """The whole point of the leaf. A citation says the same document either way and the
    answer's reliability did not stay the same, so the path and the cause it displaced are kept
    on the parse.

    Delete this and a fallback becomes invisible: the corpus holds a scrambled table, the
    citation resolves, and nothing anywhere records that the layout parser refused first."""
    outcome = parse_by_route(
        _cleared(),
        route=[
            _entry(ParsePath.LAYOUT, ParseRefusal(ParseCause.UNSUPPORTED, ParseStage.OPEN)),
            _entry(ParsePath.FALLBACK, [_block()]),
        ],
    )

    assert isinstance(outcome, AttributedParse)
    assert outcome.provenance.path is ParsePath.FALLBACK
    assert outcome.fell_back is True
    assert outcome.refused == (
        PathAttempt(
            provenance=ParseProvenance(ParsePath.LAYOUT, "fake-layout"),
            cause=ParseCause.UNSUPPORTED,
        ),
    )


def test_the_blocks_a_route_returns_are_the_objects_the_parser_produced() -> None:
    """A rebuilt block is where a page number goes missing, and a page number is half of what a
    citation resolves against. Delete this and a later edit that copies the blocks into a shape
    of the route's own passes, and the loss shows up as citations that name a chunk and no page,
    months later."""
    block = _block("the only paragraph")
    outcome = parse_by_route(_cleared(), route=[_entry(ParsePath.LAYOUT, [block])])

    assert isinstance(outcome, AttributedParse)
    assert outcome.blocks[0] is block
    assert outcome.blocks is outcome.document.blocks


def test_a_parse_result_holds_the_digest_the_gate_bound_to_the_bytes() -> None:
    """`AttributedParse` holds the `ParsedDocument` rather than unpacking it, and the digest is
    the binding between the text and the bytes somebody scanned. Delete this and a result that
    carried only blocks would be text nobody can prove came from the file that was admitted."""
    content = _cleared()
    outcome = parse_by_route(content, route=[_entry(ParsePath.LAYOUT, [_block()])])

    assert isinstance(outcome, AttributedParse)
    assert isinstance(outcome.document, ParsedDocument)
    assert outcome.document.digest == hashlib.sha256(PDF).hexdigest()


def test_when_every_path_refuses_the_uploader_is_told_what_the_last_one_found() -> None:
    """The earlier refusals are an operator's information and the last one is the remedy that is
    still true: a file the layout parser called unsupported and the fallback called corrupt
    needs re-exporting, and telling somebody to convert it to PDF sends them to do something
    that has already been tried.

    Delete this and the first refusal is returned, which is the one from the parser that got
    least far."""
    outcome = parse_by_route(
        _cleared(),
        route=[
            _entry(ParsePath.LAYOUT, ParseRefusal(ParseCause.UNSUPPORTED, ParseStage.OPEN)),
            _entry(ParsePath.FALLBACK, ParseRefusal(ParseCause.CORRUPT, ParseStage.OPEN)),
        ],
    )

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.CORRUPT


def test_a_route_that_runs_out_of_paths_reports_the_one_that_ran_out() -> None:
    """The sibling of the test above, and it covers the other exit from the loop. When the last
    path refuses with a cause that *would* permit a fallback and there is nothing left to try,
    the loop ends rather than returning early, and what comes back has to be that path's
    refusal and not the first one's.

    Written after a mutation survived: holding the first failure instead of the last passed the
    test above, because a cause that forbids a fallback leaves the loop by the early return and
    never reaches the end of it. Delete this and that whole exit is unmeasured, and an uploader
    whose scan reached OCR is told the layout parser found an unsupported format."""
    outcome = parse_by_route(
        _cleared(),
        route=[
            _entry(ParsePath.LAYOUT, ParseRefusal(ParseCause.UNSUPPORTED, ParseStage.OPEN)),
            _entry(ParsePath.FALLBACK, ParseRefusal(ParseCause.NO_TEXT_LAYER, ParseStage.TEXT)),
        ],
    )

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.NO_TEXT_LAYER


def test_a_result_has_nowhere_to_put_a_fragment_of_the_document() -> None:
    """Read off the classes rather than demonstrated by a call, because "there is no field for
    it" is a property of the type and the regression is somebody adding one. A parse result is
    rendered into console rows and operator logs that travel further than a document's own
    scope does.

    Delete this and a `detail` or a `sample` field is added to carry "a bit of context", which
    is a line of somebody's contract in a log."""
    attempt_fields = {field.name for field in dataclasses.fields(PathAttempt)}
    parse_fields = {field.name for field in dataclasses.fields(AttributedParse)}

    assert attempt_fields == {"provenance", "cause"}
    assert parse_fields == {"document", "provenance", "refused"}


def test_an_operator_line_names_the_path_that_answered_and_the_one_it_displaced() -> None:
    """`describe` is the only human-readable summary of a fallback that exists. Delete this and
    it can silently start reporting only the winning path, which is the state of affairs the
    leaf exists to end."""
    outcome = parse_by_route(
        _cleared(),
        route=[
            _entry(ParsePath.LAYOUT, ParseRefusal(ParseCause.UNSUPPORTED, ParseStage.OPEN)),
            _entry(ParsePath.FALLBACK, [_block()]),
        ],
    )

    assert isinstance(outcome, AttributedParse)
    assert outcome.describe() == (
        "read by fallback by fake-fallback after layout by fake-layout (unsupported)"
    )


def test_a_route_reaches_a_parser_through_the_gate_that_holds_the_memory_bound() -> None:
    """A route calls `parse_scanned` and never `Parser.parse`, which is what keeps the bound in
    the one function that owns it. Proved by giving the route a budget no file can fit: both
    parsers stay untouched and the failure is the one `parse_scanned` produces at
    `ParseStage.ADMIT`.

    Delete this and a route that called the parsers directly, which is one line shorter and
    reads perfectly well, would hand an unbounded file to every engine in it."""
    first = _entry(ParsePath.LAYOUT, [_block()])
    second = _entry(ParsePath.FALLBACK, [_block()])

    outcome = parse_by_route(_cleared(), route=[first, second], budget_bytes=1)

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.OUT_OF_MEMORY
    assert outcome.detail == f"stage:{ParseStage.ADMIT.value}"
    assert first.parser.calls == 0  # type: ignore[attr-defined]
    assert second.parser.calls == 0  # type: ignore[attr-defined]
