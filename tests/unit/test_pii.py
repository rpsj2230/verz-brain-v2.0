"""The scrubber: what it finds, what it refuses to decide, and what it must never split.

Every test here is about text on its way out of the building, or about a detector being
mistaken for a permission check.

Task ids: M32.2.1.1, M32.2.1.3, M32.2.1.4, M32.2.2.1, M32.2.2.2, M32.2.2.3
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import pytest

from brain.ops import pii
from brain.ops.pii import (
    NEVER_AN_AUTHORISATION_BOUNDARY,
    PRESIDIO_BUILT_INS,
    PRESIDIO_DECLINED,
    BuiltIn,
    Detection,
    EntityKind,
    budget_breach,
    configuration_gaps,
    detect,
    nric_check_letter,
    nric_checksum_holds,
    scrub,
)


def _kinds(text: str) -> list[str]:
    return [d.kind.value for d in detect(text)]


def _spans(text: str) -> list[str]:
    return [text[d.start : d.end] for d in detect(text)]


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


# --------------------------------------------------- cost
def test_a_slow_scrub_reports_a_breach_and_still_returns_the_text() -> None:
    """Turning a budget overrun into an exception means text that could not be scrubbed in
    time is sent unscrubbed or not at all, and both are worse than being slow. Delete this
    and `budget_breach` can start raising."""
    assert budget_breach(1.0, 1024) is None
    breach = budget_breach(50.0, 1024)
    assert breach is not None and "budget" in breach
    assert budget_breach(50.0, 0) is None
