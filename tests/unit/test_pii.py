"""The scrubber: what it finds, what it refuses to decide, and what it must never split.

Every test here is about text on its way out of the building, or about a detector being
mistaken for a permission check.

Task ids: M32.2.1.1, M32.2.1.2, M32.2.1.3, M32.2.1.4, M32.2.2.1, M32.2.2.2, M32.2.2.3

M32.2.2.4 is deliberately not in that line. The harness and the recorded absence are tested
below; the leaf asks for a measurement on the client CPU and nobody here has one.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import math
import re
import time
from collections.abc import Iterator, Sequence
from datetime import date

import pytest

from brain.ops import pii
from brain.ops.inference import InferenceTask, served_model
from brain.ops.pii import (
    BUDGET_MS_PER_KIB,
    ENTITY_REQUEST_KEYS,
    ENTITY_SPAN_KEYS,
    ENTITY_SPANS_KEY,
    GLINER_DECLINED,
    GLINER_LABELS,
    GLINER_MODEL_NAME,
    MINIMUM_TIMED_SAMPLES,
    NEVER_AN_AUTHORISATION_BOUNDARY,
    PRESIDIO_BUILT_INS,
    PRESIDIO_DECLINED,
    SCRUB_COST_ON_THE_BUILD_MACHINE,
    SCRUB_PERCENTILE,
    BuiltIn,
    Detection,
    EntityKind,
    GlinerLabel,
    PiiError,
    ScrubCost,
    benchmark_text,
    budget_breach,
    budget_gaps,
    configuration_gaps,
    decode_entity_spans,
    detect,
    detect_with_model,
    entity_request,
    measure_scrub,
    merge_detections,
    nric_check_letter,
    nric_checksum_holds,
    scrub,
)


def _kinds(text: str) -> list[str]:
    return [d.kind.value for d in detect(text)]


def _spans(text: str) -> list[str]:
    return [text[d.start : d.end] for d in detect(text)]


def _characters(detections: Sequence[Detection]) -> set[int]:
    """Every offset these detections cover. The unit the coverage rules are stated in."""
    return {offset for d in detections for offset in range(d.start, d.end)}


def _raw_matches(text: str) -> set[int]:
    """Every offset any recogniser's pattern reached, read off the patterns themselves.

    Built from `RECOGNISERS` rather than from `detect`, so that the coverage rule is checked
    against what went in rather than against the function's own answer. A test that asked
    `detect` what it found and then checked that it covered what it found would pass for a
    resolution that dropped half of them.
    """
    return {
        offset
        for recogniser in pii.RECOGNISERS
        for match in recogniser.pattern.finditer(text)
        for offset in range(match.start(), match.end())
    }


def _span(label: str, start: int, end: int, score: float) -> dict[str, object]:
    return {"label": label, "start": start, "end": end, "score": score}


def _model_person() -> GlinerLabel:
    return next(d for d in GLINER_LABELS if d.kind is EntityKind.UNPATTERNED_NAME)


def _stepping_clock(elapsed_ms: list[float]) -> Iterator[float]:
    """A clock reading out prepared durations, in seconds, two readings per sample."""
    at = 0.0
    for step in elapsed_ms:
        yield at
        yield at + step / 1000.0
        at += step / 1000.0 + 0.5


# --------------------------------------------------- the boundary (M32.2.2.1, .2, .3)
def test_a_detection_has_nowhere_to_express_a_refusal() -> None:
    """The mechanism, not the intention. A caller who wants to block on a detection has to
    add a field for the decision and be seen adding it, exactly as `brain.gate.injection`
    arranges for prompt-injection scores. Delete this and `blocked: bool = False` can be
    added in a diff that reads as a feature."""
    names = {f.name for f in dataclasses.fields(Detection)}
    forbidden = {
        "block",
        "blocked",
        "deny",
        "denied",
        "allow",
        "allowed",
        "permit",
        "permitted",
        "authorised",
        "authorized",
        "refuse",
        "refused",
    }
    assert not names & forbidden, names & forbidden


def test_nothing_in_the_module_answers_whether_a_caller_may_see_something() -> None:
    """The public surface is the boundary. A module that exports `is_permitted(text)` is a
    module somebody will wire into a permission check, whatever its docstring says. Delete
    this and the rule survives only as prose."""
    permission_shaped = re.compile(
        r"^(is_|may_|can_|check_|assert_)?(allow|permit|authoris|authoriz)"
    )
    exported = [
        name
        for name, value in vars(pii).items()
        if not name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value))
    ]
    assert not [n for n in exported if permission_shaped.match(n)], exported
    assert "grants" in NEVER_AN_AUTHORISATION_BOUNDARY


def test_detecting_nothing_is_not_a_verdict_that_the_text_is_safe() -> None:
    """`detect` returns detections and nothing else: there is no second return value, no
    boolean, no confidence for the text as a whole. A caller cannot ask this module whether
    a string is clean, because the honest answer is that it does not know. Delete this and
    a tuple grows a second element."""
    assert detect("nothing identifying here") == ()
    assert scrub("nothing identifying here") == "nothing identifying here"


# --------------------------------------------------- Singapore formats (M32.2.1.3)
def test_the_published_nric_derivation_produces_the_worked_example() -> None:
    """S1234567D is the example the algorithm is always quoted with, and it is the only
    check available on an algorithm the government never published. Delete this and a
    transposed row of the check-letter table becomes invisible."""
    assert nric_check_letter("S", "1234567") == "D"
    assert nric_checksum_holds("S1234567D")
    assert not nric_checksum_holds("S1234567A")


def test_an_nric_with_a_wrong_check_letter_is_still_redacted() -> None:
    """The safety property, and the reason a possibly-wrong checksum is harmless here. A
    mistyped NRIC is still a person's number with a digit changed. Delete this and
    redaction starts depending on an algorithm nobody can verify, which is a leak with a
    plausible explanation attached."""
    assert _kinds("His number is S1234567A.") == ["sg_nric"]
    assert scrub("His number is S1234567A.") == "His number is [sg_nric]."


def test_a_valid_checksum_raises_confidence_and_never_lowers_it() -> None:
    """Confirmation may only make a recogniser surer. A `confirm` that could veto would be
    a checksum deciding whether to redact, by another name. Delete this and
    `max(base, confirmed)` can become a plain assignment."""
    good = detect("S1234567D")[0]
    bad = detect("S1234567A")[0]
    assert good.confidence > bad.confidence
    assert bad.confidence > 0


def test_all_three_uen_forms_are_found_whole() -> None:
    """The nine-digit local-company form begins with something the eight-digit business
    form matches, so a shorter match taken first leaves a digit and a check letter outside
    the redaction. A partly redacted identifier is not redacted. Delete this and the
    overlap rule can be reversed with no visible failure."""
    text = "UEN 201512345K, the older 53112233B, and charity T05SS1234J."
    assert _spans(text) == ["201512345K", "53112233B", "T05SS1234J"]
    assert _kinds(text) == ["sg_uen"] * 3


def test_the_prefix_offset_is_applied_so_two_series_do_not_share_a_check_letter() -> None:
    """Only the S branch has a public worked example, so this is the check available on the
    rest: the T and G series add an offset to the weighted sum before the table is indexed.
    Dropping it makes T agree with S and G agree with F, which is wrong for every number in
    those series and invisible against a single anchored example. Delete this and the offset
    can be removed with one test still passing."""
    assert nric_check_letter("T", "1234567") != nric_check_letter("S", "1234567")
    assert nric_check_letter("G", "1234567") != nric_check_letter("F", "1234567")


def test_an_identifier_is_kept_whole_when_a_shorter_more_confident_pattern_overlaps_it() -> None:
    """`91234567A` is eight digits shaped like a local mobile number and nine characters
    shaped like a business UEN. The phone recogniser is the more confident of the two and the
    UEN is the longer one, and length wins: keeping the confident short span would leave the
    check letter outside the redaction, and a partly redacted identifier is not redacted. The
    label may then be the wrong one of the two, which is cosmetic; the leftover characters are
    not. Delete this and the ordering in `_prefer` can be reversed with nothing failing."""
    assert _spans("Order 91234567A was raised.") == ["91234567A"]
    assert scrub("Order 91234567A was raised.") == "Order [sg_uen] was raised."


def test_a_local_phone_number_is_found_in_both_written_forms() -> None:
    """Both are what people actually type, and a scrubber that only handles one of them
    misses half of them. Delete this and the country-code form passes through."""
    text = "Call 9123 4567 or +65 6123 4567."
    assert _kinds(text) == ["sg_phone", "sg_phone"]
    assert _spans(text) == ["9123 4567", "+65 6123 4567"]


def test_eight_digits_inside_a_longer_number_is_not_a_phone_number() -> None:
    """Without the lookaround the pattern matches the middle of a longer reference and
    redacts eight digits out of the centre of it, which reads as a corrupted string rather
    than as a redaction and destroys the reference for whoever needed it. Delete this and
    the lookarounds can be dropped as noise."""
    assert detect("Reference 1234567890123 is fine.") == ()


# --------------------------------------------------- name forms (M32.2.1.4)
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nur Aisyah binti Abdullah signed off.", "Nur Aisyah binti Abdullah"),
        ("Ravi s/o Muthusamy called.", "Ravi s/o Muthusamy"),
        ("Muhammad Faiz bin Hassan approved.", "Muhammad Faiz bin Hassan"),
        ("Siti d/o Ramasamy replied.", "Siti d/o Ramasamy"),
        ("Kumar a/l Segaran is on leave.", "Kumar a/l Segaran"),
    ],
)
def test_a_patronymic_name_is_redacted_as_one_span(text: str, expected: str) -> None:
    """Presidio's English NER recognises the last token and leaves the rest, and reports
    success. A half-redacted name identifies the person as well as a whole one does and
    looks handled, which is worse than looking broken. Delete this and the connector
    becomes a boundary rather than a join."""
    assert _spans(text) == [expected]
    assert expected not in scrub(text)


def test_an_initialled_name_keeps_the_initial_inside_the_redaction() -> None:
    """The written form of most Tamil names here. Redacting `Kumar` and leaving `R.` leaves
    the initial paired with whatever context the sentence carries. Delete this and the
    initial survives every scrub."""
    assert _spans("R. Kumar approved it.") == ["R. Kumar"]
    assert _spans("A. R. Rahman attended.") == ["A. R. Rahman"]


def test_a_name_written_in_han_characters_is_redacted() -> None:
    """A romanised name and the same person's name in Chinese must not have different
    outcomes, and the Chinese one is the case an English NER model does not see at all.
    Delete this and Chinese-script names pass through untouched."""
    assert _kinds("陈伟明 attended the review.") == ["person_name_cjk"]


def test_a_patronymic_span_does_not_swallow_the_rest_of_the_sentence() -> None:
    """Unbounded repetition eats the capitalised word after the name, and a redaction that
    eats the next sentence's first word is how people conclude the scrubber is broken and
    ask for it to be switched off. Delete this and the bound can be removed for
    'completeness'."""
    scrubbed = scrub("Nur Aisyah binti Abdullah. Monday is fine.")
    assert "Monday is fine." in scrubbed


# --------------------------------------------------- scrubbing
def test_a_scrub_replaces_rather_than_removes() -> None:
    """A removed span joins two clauses into a sentence neither of them said, and nothing
    downstream can tell anything was there. Delete this and the model receives text that
    reads as complete and is not."""
    assert scrub("Email rupash@verzdesign.com now.") == "Email [email] now."


def test_every_detected_span_is_gone_from_the_scrubbed_text() -> None:
    """The end-to-end property. Every rule above is about finding things; this is the one
    that says finding them removes them. Delete this and an off-by-one in the span
    arithmetic leaves a character of every identifier behind."""
    text = (
        "S1234567D called 9123 4567 about Ravi s/o Muthusamy at name@example.com, UEN 201512345K."
    )
    scrubbed = scrub(text)
    for span in _spans(text):
        assert span not in scrubbed, span


# --------------------------------------------------- the Presidio configuration (M32.2.1.1)
def test_every_entity_kind_has_somewhere_to_come_from() -> None:
    """A kind declared with no recogniser is a label that can never be produced, so a caller
    filtering for it waits forever and reports nothing wrong. Delete this and adding a
    member to `EntityKind` becomes a silent no-op."""
    assert configuration_gaps() == ()
    assert set(EntityKind)


def test_an_entity_covered_both_locally_and_by_presidio_is_reported() -> None:
    """Two answers to one question, where the loser is whichever the merge happens to drop.
    Delete this and the closure check only ever looks in one direction."""
    original = pii.PRESIDIO_BUILT_INS
    try:
        pii.PRESIDIO_BUILT_INS = (*original, BuiltIn("EMAIL", 0.5, "duplicate on purpose"))  # type: ignore[misc]
        assert any("EMAIL" in gap for gap in configuration_gaps())
    finally:
        pii.PRESIDIO_BUILT_INS = original  # type: ignore[misc]


def test_every_presidio_recogniser_says_why_it_is_not_a_local_pattern() -> None:
    """Each entry is a network dependency in the path of a scrub, and a scrubber reached
    over the network fails open on a timeout. The prose is what makes somebody weigh that.
    Delete this and the list grows by convenience."""
    for built_in in PRESIDIO_BUILT_INS:
        assert built_in.why_not_local.strip()
    with pytest.raises(ValueError, match="why it is not a local pattern"):
        BuiltIn("PERSON", 0.5, "  ")


def test_a_declined_entity_is_recorded_with_its_reason() -> None:
    """An absence is otherwise indistinguishable from an oversight, and the next reader
    enables it. `DATE_TIME` in particular looks like an obvious omission and is a
    deliberate one. Delete this and the reasoning is lost the first time somebody tidies
    the list."""
    assert set(PRESIDIO_DECLINED) >= {"DATE_TIME", "LOCATION"}
    for reason in PRESIDIO_DECLINED.values():
        assert reason.strip()


# --------------------------------------------------- resolving overlaps without losing any
def test_a_resolution_covers_every_character_any_recogniser_reached() -> None:
    """The rule the old resolution broke. Choosing between two overlapping findings drops
    whatever the loser reached and the winner does not, and a partly redacted identifier is
    not redacted. Checked against the raw pattern matches rather than against `detect`'s own
    answer, because the failure is precisely that the answer is smaller than the input.
    Delete this and overlap resolution can go back to selecting rather than tiling."""
    text = (
        "A. R. Rahman a/l Segaran called about S1234567D, order 91234567A and 9123 4567, "
        "copying R. Kumar bin Hassan and name@example.com."
    )
    assert _raw_matches(text) <= _characters(detect(text))


def test_a_name_overlapped_by_two_recognisers_is_redacted_whole() -> None:
    """The concrete case the rule above is abstract about, and the one that leaked. An
    initialled name starts at 0 and a patronymic starts inside it; the patronymic is longer,
    so under the old rule it won outright and the scrub emitted the initials in clear beside
    one redaction. Delete this and the leak comes back with every other test still green."""
    text = "A. R. Rahman a/l Segaran replied."
    scrubbed = scrub(text)
    for fragment in ("A.", "R.", "Rahman", "Segaran"):
        assert fragment not in scrubbed, scrubbed
    assert scrubbed.endswith(" replied.")


def test_detections_come_back_in_order_and_never_overlapping() -> None:
    """`scrub` walks the spans with a cursor and skips anything starting behind it, so two
    overlapping detections would drop the second one's tail out of the redaction. The
    resolution is what guarantees they do not overlap. Delete this and a resolution that
    emits an overlap turns into a leak two functions away."""
    text = (
        "A. R. Rahman a/l Segaran and Nur Aisyah binti Abdullah, S1234567D, 201512345K, "
        "+65 6123 4567, 陈伟明, name@example.com"
    )
    found = detect(text)
    assert [d.start for d in found] == sorted(d.start for d in found)
    for earlier, later in itertools.pairwise(found):
        assert earlier.end <= later.start, (earlier, later)


def test_the_cost_of_a_scrub_grows_with_the_text_and_not_with_its_square() -> None:
    """The one property here that no assertion about output can express, and the one the
    budget is written in. The resolution this replaced compared each candidate against every
    span already kept, so on identifier-dense text its cost per kibibyte rose with length and
    left the budget somewhere between 26 KiB and 64 KiB. Timed as a ratio between two sizes
    on the same machine rather than against a wall-clock figure, and from the fastest of five
    runs, because a threshold in milliseconds is a test that fails on a busy machine and a
    ratio is not. Measured at 0.99 times linear after the change and 4.78 before it. Delete
    this and the quadratic can be reintroduced with every correctness test still passing."""
    small = benchmark_text(16384)
    large = benchmark_text(131072)

    def fastest(text: str) -> float:
        best = None
        for _ in range(5):
            started = time.perf_counter()
            scrub(text)
            taken = time.perf_counter() - started
            best = taken if best is None else min(best, taken)
        assert best is not None
        return best

    over_linear = (fastest(large) / fastest(small)) / (len(large) / len(small))
    assert over_linear < 2.5, over_linear


# --------------------------------------------------- the entity model (M32.2.1.2)
def test_the_entity_model_named_here_is_the_one_the_inference_server_serves() -> None:
    """Two files name this model: the sizing that decides the container's memory limit, and
    the request built here. A name that drifts between them is a request sent to a server
    holding different weights, which answers, plausibly, with spans computed by something
    nobody sized for. Asserted against the other module rather than against the constant
    beside it, which is how `hubspot.CEILING_NAME` was caught pointing at Freshdesk. Delete
    this and either name can be edited alone."""
    assert served_model(InferenceTask.ENTITY_RECOGNITION).name == GLINER_MODEL_NAME


def test_the_model_and_presidio_agree_on_how_sure_is_sure_enough_about_a_name() -> None:
    """Both legs find people and both feed the same merge. Two thresholds means the same
    name is redacted or not depending on which container happened to see it, and the
    detection rate M32.2.2.3 watches then moves whenever either is redeployed for reasons
    that have nothing to do with the text. Delete this and the two drift apart silently."""
    presidio_person = next(b for b in PRESIDIO_BUILT_INS if b.presidio_name == "PERSON")
    assert _model_person().score_threshold == presidio_person.score_threshold


def test_every_label_says_what_the_default_recognisers_miss_about_it() -> None:
    """The leaf is named for exactly this: entities the default recognisers miss. A label
    that Presidio or a regex already finds is a second network call for an answer somebody
    already has, and the prose is what makes an author check before adding one. Delete this
    and the label list grows by convenience, which is the same failure `why_not_local`
    exists to prevent one layer down."""
    for declared in GLINER_LABELS:
        assert declared.why_the_default_recognisers_miss_it.strip()
    with pytest.raises(ValueError, match="what the default recognisers miss"):
        GlinerLabel(
            label="person",
            kind=EntityKind.UNPATTERNED_NAME,
            score_threshold=0.6,
            why_the_default_recognisers_miss_it="  ",
        )


def test_a_label_that_a_local_pattern_already_produces_is_reported() -> None:
    """The model is for what the patterns cannot see. A label mapping to a kind a regex
    already produces is a container, a network call and a second set of weights buying an
    answer this process had before it asked. Delete this and the closure check only ever
    looks at Presidio."""
    original = pii.GLINER_LABELS
    try:
        pii.GLINER_LABELS = (  # type: ignore[misc]
            *original,
            GlinerLabel(
                label="identity card",
                kind=EntityKind.NRIC,
                score_threshold=0.5,
                why_the_default_recognisers_miss_it="duplicate on purpose",
            ),
        )
        assert any("identity card" in gap for gap in configuration_gaps())
    finally:
        pii.GLINER_LABELS = original  # type: ignore[misc]


def test_a_label_both_asked_for_and_declined_is_reported() -> None:
    """`GLINER_DECLINED` is the record of what was considered and refused, so a label
    appearing in both says the record disagrees with the request and one of the two is a
    stale edit. Delete this and the declined list becomes decoration. Delete the list and
    the next reader cannot tell a decision from an oversight, which is the argument
    `PRESIDIO_DECLINED` already makes."""
    assert set(GLINER_DECLINED) >= {"organisation", "address"}
    for reason in GLINER_DECLINED.values():
        assert reason.strip()
    original = pii.GLINER_LABELS
    try:
        pii.GLINER_LABELS = (  # type: ignore[misc]
            *original,
            GlinerLabel(
                label="organisation",
                kind=EntityKind.UNPATTERNED_NAME,
                score_threshold=0.5,
                why_the_default_recognisers_miss_it="contradiction on purpose",
            ),
        )
        assert any("both asked for and declined" in gap for gap in configuration_gaps())
    finally:
        pii.GLINER_LABELS = original  # type: ignore[misc]


def test_a_request_carries_the_text_and_the_labels_and_nothing_that_names_a_principal() -> None:
    """The structural half of this module never being a permission check. A request that
    cannot grow a key cannot grow one naming a department or a visibility, and a response to
    that request cannot be read as a decision about who may see what. Asserted on the whole
    key set rather than on the absence of one name, because the next field somebody adds
    will not be called `department`. Delete this and the request grows a scope."""
    request = entity_request("Tan Wei Ling called.")
    assert tuple(request) == ENTITY_REQUEST_KEYS
    assert request["text"] == "Tan Wei Ling called."
    assert request["labels"] == tuple(d.label for d in GLINER_LABELS)
    with pytest.raises(TypeError):
        request["department"] = "finance"  # type: ignore[index]


def test_a_span_the_model_was_not_asked_for_is_refused_rather_than_ignored() -> None:
    """`EntityKind` is closed because the label goes into the outgoing text, and a network
    is where a closed vocabulary quietly opens. An unknown label is either a token nobody
    chose appearing in a prompt or a span dropped in silence, and refusing is the only
    outcome that anybody finds out about. Delete this and the far side decides the
    vocabulary."""
    text = "Tan Wei Ling called."
    with pytest.raises(PiiError, match="no declared label"):
        decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("nationality", 0, 12, 0.9)]})


def test_a_span_outside_the_text_is_refused_because_it_answers_about_another_string() -> None:
    """An offset past the end of what was sent means the far side scored a different string
    from the one this process holds, and then every other span in that answer is against
    the wrong text too. A span one character out is the half-redacted name this module is
    mostly about. Delete this and offsets are trusted, which is how a name ends up redacted
    from the middle of the word after it."""
    text = "Tan Wei Ling called."
    with pytest.raises(PiiError, match="computed against a different string"):
        decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("person", 0, len(text) + 1, 0.9)]})
    with pytest.raises(PiiError, match="covers no characters"):
        decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("person", 5, 5, 0.9)]})


def test_a_span_below_its_threshold_is_dropped_and_one_at_it_is_kept() -> None:
    """The threshold is the whole of this module's caution about the model layer, and a
    guard tested only by what it refuses is satisfied by refusing everything. The pair is
    the point: below is silence, at is a detection. Delete either half and the threshold can
    be moved to nought or to one with the other still passing."""
    text = "Tan Wei Ling called."
    threshold = _model_person().score_threshold
    quiet = decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("person", 0, 12, threshold / 2)]})
    assert quiet == ()
    heard = decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("person", 0, 12, threshold)]})
    assert [d.kind for d in heard] == [EntityKind.UNPATTERNED_NAME]
    assert heard[0].start == 0 and heard[0].end == 12


def test_a_response_that_cannot_be_read_is_not_a_response_that_found_nothing() -> None:
    """The two have to be different events in the code and are the same event to whoever is
    waiting, which is the distinction `A_REFUSAL_IS_NOT_AN_EMPTY_RESULT` draws on the
    embedding leg. A missing list recorded as "no names in this text" is a scrub that
    reports it did something it did not. Delete this and a malformed answer becomes a clean
    bill of health."""
    with pytest.raises(PiiError, match="cannot be read is not an answer"):
        decode_entity_spans("Tan Wei Ling called.", {"results": []})
    with pytest.raises(PiiError, match="where one span was due"):
        decode_entity_spans("Tan Wei Ling called.", {ENTITY_SPANS_KEY: ["person"]})
    assert decode_entity_spans("Tan Wei Ling called.", {ENTITY_SPANS_KEY: []}) == ()


def test_a_span_missing_any_of_its_fields_is_refused() -> None:
    """A span with no score is a span with no threshold applied to it, and a span with no
    offsets is one placed by its position in a list, which pairs the wrong text with the
    wrong label and looks well formed everywhere downstream. Delete this and a partial span
    is filled in with defaults by whoever writes the server."""
    for missing in ENTITY_SPAN_KEYS:
        span = _span("person", 0, 12, 0.9)
        del span[missing]
        with pytest.raises(PiiError, match="states"):
            decode_entity_spans("Tan Wei Ling called.", {ENTITY_SPANS_KEY: [span]})


# --------------------------------------------------- the merge (M32.2.1.2)
def test_a_model_span_never_takes_the_label_off_a_deterministic_one() -> None:
    """The overlap rule, and the case is real rather than hypothetical: an NRIC sits beside
    a name in the sentences this runs on, and a model asked for people will sometimes take
    both as one span. The pattern's extent comes from a format with a fixed length; the
    model's moves between revisions. Delete this and a checksummed identifier can be counted
    as a name, and the detection rates M32.2.2.3 watches stop meaning anything."""
    text = "Tan Wei Ling S1234567D called."
    payload = {ENTITY_SPANS_KEY: [_span("person", 0, 22, 0.95)]}
    merged = detect_with_model(text, payload)
    by_kind = {d.kind: (d.start, d.end) for d in merged}
    assert by_kind[EntityKind.NRIC] == (13, 22)
    assert by_kind[EntityKind.UNPATTERNED_NAME] == (0, 13)


def test_a_merge_covers_every_character_either_side_covered() -> None:
    """The property that makes "a detector that finds more redacts more" true rather than
    hoped for. Resolving the overlap by keeping one span would have redacted the identifier
    and left the name beside it in clear, which is the failure that looks handled. Delete
    this and the merge can start choosing between the two legs."""
    text = "Tan Wei Ling S1234567D called."
    model = decode_entity_spans(text, {ENTITY_SPANS_KEY: [_span("person", 0, 22, 0.95)]})
    deterministic = detect(text)
    merged = merge_detections(deterministic, model)
    assert _characters(merged) == _characters(deterministic) | _characters(model)
    assert "Tan Wei Ling" not in scrub(text, merged)

    # Both ends of the same rule, because the subtraction has a branch at each. Above, the
    # model span reaches back before the identifier. Here it reaches past the last thing any
    # pattern covered, which is the case that leaks if the residue after the final block is
    # dropped and which the sentence above cannot show.
    trailing = "S1234567D Tan Wei Ling called."
    after = decode_entity_spans(trailing, {ENTITY_SPANS_KEY: [_span("person", 0, 22, 0.95)]})
    both = merge_detections(detect(trailing), after)
    assert _characters(both) == _characters(detect(trailing)) | _characters(after)
    assert "Tan Wei Ling" not in scrub(trailing, both)


def test_the_model_leg_can_only_ever_add_to_what_is_redacted() -> None:
    """`A_MODEL_MAY_ONLY_EVER_ADD_TO_A_SCRUB`, as an assertion. Everything about the way
    this degrades rests on the deterministic leg being computed first and unconditionally:
    an absent server, a restarting one and a profile that deploys none all arrive as no
    payload, and all three have to leave the patterns' answer untouched. Delete this and a
    model that returns nothing can start returning less than nothing."""
    text = "Nur Aisyah binti Abdullah, S1234567D, on 9123 4567."
    assert detect_with_model(text, None) == detect(text)
    payload = {ENTITY_SPANS_KEY: [_span("person", 0, 25, 0.9)]}
    assert _characters(detect_with_model(text, payload)) >= _characters(detect(text))


def test_a_merge_of_nothing_is_the_deterministic_answer_unchanged() -> None:
    """The positive case for the merge, and the shape a working server produces most of the
    time: a text with no unpatterned names in it. A merge that returned something different
    from `detect` when the model found nothing would mean the model leg changed the scrub by
    being present rather than by finding anything. Delete this and the merge can start
    rewriting spans it was not asked about."""
    text = "S1234567D and 201512345K were quoted."
    assert merge_detections(detect(text), ()) == detect(text)


# --------------------------------------------------- cost
def test_a_slow_scrub_reports_a_breach_and_still_returns_the_text() -> None:
    """Turning a budget overrun into an exception means text that could not be scrubbed in
    time is sent unscrubbed or not at all, and both are worse than being slow. Delete this
    and `budget_breach` can start raising."""
    assert budget_breach(1.0, 1024) is None
    breach = budget_breach(50.0, 1024)
    assert breach is not None and "budget" in breach
    assert budget_breach(50.0, 0) is None


# --------------------------------------------------- the measurement (M32.2.2.4, not claimed)
def test_the_sample_floor_is_where_the_percentile_stops_being_the_maximum() -> None:
    """`MINIMUM_TIMED_SAMPLES` is derived rather than chosen, and this is the property it is
    derived from: at nearest rank, one sample fewer makes the ninety-fifth percentile the
    last element, which is the maximum wearing a percentile's name and is a measurement of
    whatever else the machine was doing. Asserted as the arithmetic rather than as the
    number, so the constant cannot be edited to agree with itself. Delete this and the floor
    becomes a round number somebody liked."""
    assert math.ceil(SCRUB_PERCENTILE * MINIMUM_TIMED_SAMPLES) < MINIMUM_TIMED_SAMPLES
    assert math.ceil(SCRUB_PERCENTILE * (MINIMUM_TIMED_SAMPLES - 1)) == MINIMUM_TIMED_SAMPLES - 1


def test_a_timing_run_reports_the_percentile_and_not_the_mean_or_the_worst() -> None:
    """The harness is arithmetic over a clock, and a harness that read its own clock could
    only be checked by running it and hoping. The prepared readings separate all three
    answers on purpose: the mean of them is 8.4, the maximum is 100 and the ninety-fifth
    percentile is 50. Delete this and `_percentile` can become `max` or `sum(...)/len(...)`
    with nothing failing, and a budget would then be compared against a figure nobody
    waited."""
    readings = [1.0] * 18 + [50.0, 100.0]
    ticks = _stepping_clock(readings)
    cost = measure_scrub(
        "x" * 1024,
        clock=lambda: next(ticks),
        hardware="a fake clock",
        basis="prepared readings",
        excludes="everything real",
        taken_on=date(2026, 9, 7),
        samples=len(readings),
    )
    assert cost.ms_per_kib == pytest.approx(50.0)
    assert cost.samples == len(readings)
    assert cost.chars == 1024


def test_a_timing_run_that_measured_the_clock_rather_than_the_scrubber_is_refused() -> None:
    """Three ways a figure can be arithmetic rather than evidence, and each has to be
    refused where the figure is built rather than argued about where it is read. A cost of
    nought is a clock that did not tick. Too few samples is a maximum called a percentile.
    Prose left empty is a number nobody can check the origin of, which is the failure
    `ServedModel.sizing_basis` exists for. Delete this and any of the three ships."""
    ordinary = ScrubCost(
        taken_on=date(2026, 9, 7),
        hardware="a machine",
        basis="some text",
        excludes="the model leg",
        chars=1024,
        samples=MINIMUM_TIMED_SAMPLES,
        ms_per_kib=0.5,
    )
    assert ordinary.ms_per_kib == 0.5
    with pytest.raises(ValueError, match="clock that did not tick"):
        dataclasses.replace(ordinary, ms_per_kib=0.0)
    with pytest.raises(ValueError, match="maximum by another name"):
        dataclasses.replace(ordinary, samples=MINIMUM_TIMED_SAMPLES - 1)
    with pytest.raises(ValueError, match="states its hardware"):
        dataclasses.replace(ordinary, hardware="  ")
    with pytest.raises(ValueError, match="states its excludes"):
        dataclasses.replace(ordinary, excludes="")
    with pytest.raises(ValueError, match="no characters"):
        dataclasses.replace(ordinary, chars=0)


def test_the_budget_reports_that_it_has_never_been_measured_on_the_client_cpu() -> None:
    """The recorded absence, and the reason M32.2.2.4 is not claimed. This system is
    single-tenant and client-hosted, so the machine the budget is about is one nobody here
    has, and a figure from a laptop is not a measurement of it. The finding is permanent
    until somebody runs the harness where the software is installed, which is why it lives
    in a function of its own rather than in `configuration_gaps`, which must be empty.
    Delete this and the absence becomes a paragraph somebody deletes while tidying."""
    findings = budget_gaps()
    assert len(findings) == 1, findings
    assert "M32.2.2.4" in findings[0]
    assert not SCRUB_COST_ON_THE_BUILD_MACHINE.on_the_client_cpu


def test_a_budget_measured_on_the_target_machine_and_met_reports_nothing() -> None:
    """The positive case, without which the check above is satisfied by a function that
    complains about everything. It is also the shape of the edit that closes the leaf: run
    the harness on the client's hardware, record it with the flag set, and this returns
    empty. Delete this and `budget_gaps` can start refusing every cost it is handed."""
    measured = ScrubCost(
        taken_on=date(2026, 9, 7),
        hardware="the client's own host",
        basis="benchmark_text(65536)",
        excludes="the model leg",
        chars=65772,
        samples=MINIMUM_TIMED_SAMPLES,
        ms_per_kib=0.7,
        on_the_client_cpu=True,
    )
    assert budget_gaps(measured, budget_ms_per_kib=2.0) == ()
    over = dataclasses.replace(measured, ms_per_kib=5.0)
    breaches = budget_gaps(over, budget_ms_per_kib=2.0)
    assert len(breaches) == 1 and "5.00 ms/KiB" in breaches[0]
    assert budget_gaps(None) and "nothing has been timed" in budget_gaps(None)[0]


def test_the_declared_budget_is_above_what_has_actually_been_timed() -> None:
    """A budget below the only measurement anybody has taken is a budget already known to be
    breached, and one that never fails is a paragraph. Asserted between the two constants
    rather than against either alone, which is the relation `RETRY_AFTER_WHEN_UNSTATED >=
    MAX_BACKOFF_SECONDS` is stated as. Delete this and `BUDGET_MS_PER_KIB` can be edited in
    either direction to make something else pass."""
    assert SCRUB_COST_ON_THE_BUILD_MACHINE.ms_per_kib < BUDGET_MS_PER_KIB
    assert budget_breach(SCRUB_COST_ON_THE_BUILD_MACHINE.ms_per_kib, 1024) is None


def test_the_harness_times_the_real_scrubber_on_this_machine() -> None:
    """The harness has to have been run, or it is a design. This is the only test here that
    reads a real clock, and it deliberately asserts nothing about how long anything took: a
    threshold in milliseconds is a test that fails on a busy machine, and what is worth
    pinning is that `measure_scrub` accepts a real clock, times the real `scrub` and
    produces a cost that `ScrubCost` will accept. Delete this and the recorded figure could
    have been typed rather than measured."""
    text = benchmark_text(2048)
    cost = measure_scrub(
        text,
        clock=time.perf_counter,
        hardware="whatever is running the suite",
        basis="benchmark_text(2048)",
        excludes="the model leg, which has no server",
        taken_on=date(2026, 9, 7),
    )
    assert cost.chars == len(text)
    assert cost.samples == MINIMUM_TIMED_SAMPLES
    assert cost.ms_per_kib > 0.0


def test_a_benchmark_is_never_cut_through_an_identifier() -> None:
    """The benchmark is repeated whole rather than truncated, because a cut through an
    identifier changes how many things are found, and the count of findings is what the old
    resolution's cost scaled with. A benchmark whose input shape changes with the size asked
    for cannot be compared across sizes, which is the comparison that found the problem.
    Delete this and `benchmark_text` can start slicing."""
    paragraph = pii.BENCHMARK_PARAGRAPH
    text = benchmark_text(1000)
    assert len(text) >= 1000
    assert len(text) % len(paragraph) == 0
    per_paragraph = len(detect(paragraph))
    assert per_paragraph > 0
    for repeats in (1, 3, 11):
        assert len(detect(paragraph * repeats)) == repeats * per_paragraph
    with pytest.raises(ValueError, match="times nothing"):
        benchmark_text(0)
