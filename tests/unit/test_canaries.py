"""The canary harness, held to the property it exists to check on other things.

`brain.ops.canaries` is the module that catches a permission failure by looking at an answer.
So the risk it carries is the one every checker carries: a checker that quietly stops
checking is worse than none, because the green run is read as evidence.

Two shapes of that risk run through everything below. **A check that reports nothing must be
distinguishable from a check that found nothing**, which is why every absence test here has a
positive sibling proving the same function does fire. And **the report must not repeat the
leak**, which is checked against the real canary tokens from `tests.fixtures.company` rather
than against a token invented here, because a token invented here would be one this module
was never at risk of emitting.

The fixtures are the real ones throughout. A canary suite tested with a made-up canary and a
made-up question is a suite that proves its own arrangement works.

Task ids: M28.2.1, M28.2.2, M28.2.3, M28.2.4
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.ops.canaries import (
    CANARY_INTERVAL_SECONDS,
    PROJECTION_CHECK,
    CanaryFinding,
    Finding,
    alert_lines,
    compare_askers,
    due,
    leaked_fields,
    locked_findings,
    projection_findings,
    scan,
    scan_stores,
)
from tests.fixtures.company import CANARIES, canary_tokens
from tests.fixtures.golden import GOLDEN, REFUSAL_TEXT, Expect

CONTRACT = "client.contract_value"
SALARY = "hr.salary"


def test_a_canary_in_an_answer_is_reported_as_the_field_and_never_as_the_value() -> None:
    """**The property the whole module is arranged around.**

    A finding travels: into a ticket, into CI output, into an alert, into whatever the person
    on call pastes it into. Every one of those has weaker access control than the system that
    leaked, so a token inside a finding is the leak happening a second time through the report
    about it.

    Asserted against every real canary rather than the one that was planted, because the
    failure this guards against is a future edit that includes the value "for debugging", and
    that edit would include whichever value it had.

    Delete this and the report becomes a second copy of the leak, distributed further."""
    answer = f"The contract value is {CANARIES[CONTRACT]} and the term is three years."

    found = scan(asker="p_ben", question_id="g1", text=answer, canaries=CANARIES)

    assert [one.subject for one in found] == [CONTRACT]
    assert found[0].kind is Finding.LEAKED
    rendered = " ".join(one.line() for one in found)
    for token in canary_tokens():
        assert token not in rendered, "a finding carries a canary value"


def test_a_finding_has_nowhere_to_put_a_value_even_if_somebody_wanted_to() -> None:
    """The structural half of the rule above. The prose says a finding must not carry the
    token; this says there is no attribute to carry it in.

    A rule stated in a docstring holds until somebody has a bad afternoon. A frozen dataclass
    with four named fields and no room for a fifth means including the value is an edit to the
    model, in a module whose docstring argues against it, which is a decision somebody makes
    rather than a line they add.

    Delete this and `token: str = ""` appears on the model and every other test here still
    passes, because none of them construct one with it set."""
    names = {f.name for f in dataclass_fields(CanaryFinding)}

    assert names == {"kind", "asker", "question_id", "subject"}
    for forbidden in ("token", "value", "canary", "text", "excerpt", "snippet"):
        assert forbidden not in names, f"a finding can carry a {forbidden}"


def test_a_canary_the_asker_is_entitled_to_is_not_a_finding() -> None:
    """The positive sibling, and without it every other assertion here is satisfied by a
    function that reports everything.

    A caller who may read the contract value should see it, and the canary appearing in their
    answer is the system working. A harness that flagged that would be a harness whose output
    an operator learns to ignore, which is the same outcome as no harness at all.

    `permitted` is what carries the entitlement in, because this module holds no entitlements
    and must not: deciding who may read what is `EntitlementSet.intersect`'s job and a second
    opinion here would be a second implementation of the central rule. So the same answer is
    a finding for one asker and not for another, and the only difference is what the caller
    passed.

    Delete this and `scan` can be made to report unconditionally, and every absence test in
    this file goes on passing."""
    answer = f"The contract value is {CANARIES[CONTRACT]}."

    entitled = scan(
        asker="p_ada",
        question_id="g1",
        text=answer,
        canaries=CANARIES,
        permitted=(CONTRACT,),
    )
    not_entitled = scan(asker="p_ben", question_id="g1", text=answer, canaries=CANARIES)

    assert entitled == ()
    assert [one.subject for one in not_entitled] == [CONTRACT]


def test_a_clean_answer_produces_no_finding_at_all() -> None:
    """The other half of the positive case: nothing leaked means nothing reported.

    Trivial to satisfy and worth stating, because the two failures either side of it are a
    harness that reports everything and one that reports nothing, and only one of those is
    caught by the tests above.

    Delete this and a `scan` returning a finding per field regardless of the text still
    passes the entitled-asker test, since that one passes `permitted`."""
    assert scan(asker="p_ben", question_id="g1", text="Three years.", canaries=CANARIES) == ()
    assert leaked_fields("nothing improbable here", CANARIES) == frozenset()


def test_every_place_a_run_leaves_text_behind_is_searched_and_named() -> None:
    """M28.2.2. The answer is the least likely of these to leak, which is the reason the
    others are checked.

    The answer is what the redactor is pointed at, so it is the one surface with a guard in
    front of it. A trace span and a stored payload are written by code that was thinking about
    debugging; `brain.ops.tracing` masks `payload_in` and `payload_out` because somebody
    noticed that once, and this is what checks the masking happened.

    The store's name is in the subject, so a leak into the payload store and a leak into the
    answer are two findings rather than one reported twice, and an operator can tell which
    guard failed.

    Delete this and the harness checks the one surface that was already guarded."""
    found = scan_stores(
        asker="p_ben",
        question_id="g1",
        stores={
            "answer": ["Three years."],
            "trace": [f"tool result: {CANARIES[CONTRACT]}"],
            "payload": [f"{CANARIES[SALARY]} was in the prompt"],
        },
        canaries=CANARIES,
    )

    assert {one.subject for one in found} == {f"trace:{CONTRACT}", f"payload:{SALARY}"}
    rendered = " ".join(one.line() for one in found)
    for token in canary_tokens():
        assert token not in rendered


def test_a_refusal_that_is_not_the_absence_sentence_is_a_finding() -> None:
    """M28.2.1, and the reason the comparison is equality rather than resemblance.

    DENIED and ABSENT are indistinguishable or they are not. A refusal differing by a full
    stop is one bit about whether the thing exists, and a caller who can ask twice can spend
    that bit on every field in the system.

    The variants below are deliberately trivial: a trailing space, a missing full stop, a
    capital. Each would pass any similarity check somebody reached for to make this less
    brittle, and each is a working oracle.

    Delete this and the check becomes "looks like a refusal", which is satisfied by a refusal
    that says which department the record is in."""
    variants = [REFUSAL_TEXT + " ", REFUSAL_TEXT.rstrip("."), REFUSAL_TEXT.upper()]

    for variant in variants:
        found = compare_askers(
            question_id="g1",
            answers={"p_ben": variant},
            refused=["p_ben"],
            absence_text=REFUSAL_TEXT,
        )
        assert [one.kind for one in found] == [Finding.REFUSAL_DISTINGUISHABLE], variant


def test_a_refusal_identical_to_an_absence_is_not_a_finding() -> None:
    """The positive sibling. Without it, `compare_askers` returning a finding unconditionally
    passes the test above.

    Delete this and the harness reports every refusal as broken, which reads as the system
    being broken and ends with somebody turning the harness off."""
    assert (
        compare_askers(
            question_id="g1",
            answers={"p_ben": REFUSAL_TEXT, "p_ada": "The contract runs three years."},
            refused=["p_ben"],
            absence_text=REFUSAL_TEXT,
        )
        == ()
    )


def test_an_asker_who_was_refused_and_answered_nothing_at_all_is_a_finding() -> None:
    """A missing answer is not a passing refusal.

    The tempting reading of an absent entry is "nothing came back, which is what a refusal
    looks like", and it is wrong in the direction that matters: it means the run did not
    observe what it thinks it observed, so every other assertion about that asker is vacuous.

    **The subject is asserted as well as the kind, and a mutation is why.** Skipping the
    `is None` branch still produces a finding, because `None` is not equal to the absence
    sentence either, so a test reading only the kind passes on a version that cannot tell the
    two apart. They are different defects: one says the harness failed to observe an answer,
    the other says the system's refusal text is wrong, and they send somebody to different
    places.

    Delete this and a canary run that failed to record half its answers reports success, or
    reports it as the wrong defect, which costs the same afternoon."""
    missing = compare_askers(
        question_id="g1", answers={}, refused=["p_ben"], absence_text=REFUSAL_TEXT
    )
    wrong_text = compare_askers(
        question_id="g1",
        answers={"p_ben": "No such client."},
        refused=["p_ben"],
        absence_text=REFUSAL_TEXT,
    )

    assert [one.kind for one in missing] == [Finding.REFUSAL_DISTINGUISHABLE]
    assert missing[0].subject == "no answer was recorded"
    assert wrong_text[0].subject != missing[0].subject, (
        "an unobserved answer and a wrong refusal report the same thing, so the report "
        "cannot tell an operator which of the two happened"
    )


def test_nothing_is_reported_about_the_answers_of_people_who_were_not_refused() -> None:
    """Two permitted answers differ for every legitimate reason there is, and a finding
    saying they differed would be a fact about the difference between two people's
    entitlements, in a report that travels.

    Delete this and the harness starts diffing answers it has no business comparing, and the
    output becomes a map of who can see more than whom."""
    found = compare_askers(
        question_id="g1",
        answers={"p_ada": "Three years.", "p_cara": "Three years and renews in May."},
        refused=[],
        absence_text=REFUSAL_TEXT,
    )

    assert found == ()


def test_a_field_that_should_have_been_locked_and_came_back_open_is_a_finding() -> None:
    """A withheld field arrives as a lock rather than as an absence, and the lock is the
    product: it tells the reader the field exists and that they may not see it, which is a
    deliberate disclosure the system makes on purpose.

    A field that should have been locked and was not is either a value that reached somebody,
    or a lock the console cannot render. Both are worth a finding and only one of them shows
    up as a canary.

    Delete this and the harness only catches leaks that happen to carry a planted value."""
    found = locked_findings(
        asker="p_ben",
        question_id="g1",
        expected_locked=(CONTRACT, "client.margin"),
        reported_locked=("client.margin",),
    )

    assert [one.subject for one in found] == [CONTRACT]
    assert found[0].kind is Finding.UNLOCKED


def test_a_field_locked_that_nobody_expected_is_not_a_finding() -> None:
    """Only one direction is reported here, and the asymmetry is deliberate.

    A field locked that the corpus did not predict is the system withholding more than
    expected. That is worth knowing and it is not a permission failure, and reporting it in a
    travelling report would put "this was available to you and now is not" into it.

    Delete this and the harness reports tightening as a defect, which teaches whoever reads it
    that tightening is a defect."""
    assert (
        locked_findings(
            asker="p_ben",
            question_id="g1",
            expected_locked=(),
            reported_locked=(CONTRACT, "client.margin"),
        )
        == ()
    )


def test_a_catalogue_offering_a_tool_the_caller_cannot_invoke_is_a_finding() -> None:
    """M28.2.3. A tool name is a fact about what the installation does: `read_invoice_ledger`
    in a list tells its reader there is an invoice ledger, before they invoke anything.

    So a projection wider than the entitlements is a disclosure and not merely an untidiness,
    and it is the direction that will not announce itself: the extra tool refuses when called,
    which reads as the gate working.

    Delete this and the catalogue can drift wider while every invocation is still refused,
    which is exactly the state that looks correct from inside."""
    found = projection_findings(
        asker="p_ben",
        offered=("search_knowledge", "read_invoice_ledger"),
        admissible=("search_knowledge",),
    )

    assert [(one.kind, one.subject) for one in found] == [
        (Finding.PROJECTION_TOO_WIDE, "read_invoice_ledger")
    ]
    assert found[0].question_id == PROJECTION_CHECK


def test_a_catalogue_withholding_a_tool_the_caller_may_invoke_is_also_a_finding() -> None:
    """Both directions are findings here, unlike `locked_findings` one function up, and the
    difference is what each one costs.

    Too wide is a disclosure. Too narrow is not, and it is a broken product: somebody
    entitled to a tool cannot reach it, and the symptom is an agent that cannot do its job for
    a reason nobody can see. Both are the projection failing to be a function of the
    entitlements, which is the property under test.

    Delete this and the harness only notices the projection drifting in one direction."""
    found = projection_findings(
        asker="p_ada",
        offered=("search_knowledge",),
        admissible=("search_knowledge", "read_client_record"),
    )

    assert [(one.kind, one.subject) for one in found] == [
        (Finding.PROJECTION_TOO_NARROW, "read_client_record")
    ]


def test_a_projection_that_matches_exactly_produces_nothing() -> None:
    """The positive sibling for both directions at once.

    Delete this and `projection_findings` can report on every tool it is given, which passes
    both tests above."""
    tools = ("search_knowledge", "read_client_record")

    assert projection_findings(asker="p_ada", offered=tools, admissible=tools) == ()
    assert projection_findings(asker="p_ada", offered=(), admissible=()) == ()


def test_a_finding_that_names_nothing_cannot_be_constructed() -> None:
    """A finding with a blank subject is a line in a report that tells nobody anything, and
    it is what an empty string produces when a caller passes a value it did not have.

    Refused at construction rather than filtered later, so the useless finding never exists
    rather than being dropped somewhere a future reader has to notice.

    Delete this and a run with a missing field id reports findings nobody can act on, which
    is indistinguishable from noise and gets the harness ignored."""
    for blank in ("asker", "question_id", "subject"):
        kwargs = {
            "kind": Finding.LEAKED,
            "asker": "p_ben",
            "question_id": "g1",
            "subject": CONTRACT,
        }
        kwargs[blank] = ""
        with pytest.raises(ValueError, match="names nothing"):
            CanaryFinding(**kwargs)  # type: ignore[arg-type]


def test_a_run_that_has_never_happened_is_owed_immediately() -> None:
    """M28.2.4. A fresh install checks whether its gate works now rather than in twelve hours.

    The alternative reading, that a never-run canary is not yet due, means the window in which
    an installation is least proven is the window in which it is least checked.

    Delete this and a new deployment goes half a day before anything asks whether its
    permissions hold."""
    assert due(last_run=None, now=datetime(2026, 9, 7, tzinfo=UTC)) is True


def test_a_run_is_owed_once_the_interval_has_passed_and_not_before() -> None:
    """The boundary, from both sides, because an interval check that is always true is a
    scheduled run on every tick and one that is never true is a run that never happens.

    Asserted at the interval exactly as well as either side of it: `>=` and `>` differ only
    there, and the difference is a run that drifts later by one tick every day.

    Delete this and the schedule can become either of those without a test failing."""
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    interval = timedelta(seconds=CANARY_INTERVAL_SECONDS)

    assert due(last_run=now - interval, now=now) is True
    assert due(last_run=now - interval + timedelta(seconds=1), now=now) is False
    assert due(last_run=now, now=now) is False


def test_the_interval_is_not_so_short_that_production_is_asked_constantly() -> None:
    """The figure is a judgement and the property behind it is not: a canary run sends
    synthetic requests to production, so an interval measured in minutes is a load somebody
    pays for to re-measure a system that has not changed.

    Asserted as a floor rather than as the exact number, because the number is arguable and
    "more than once an hour" is the thing that would be wrong. What it detects arrives with a
    deployment, and a caller running it on every deployment as well is doing the right thing.

    Delete this and the interval can be tuned down to a minute by somebody who wants faster
    feedback, and production carries it."""
    assert CANARY_INTERVAL_SECONDS >= 3600


def test_an_alert_carries_no_count_and_repeats_no_finding() -> None:
    """A run that asked one question as several askers finds one defect several times, and an
    alert listing it several times reads as several defects.

    No count and no summary line: "seven findings" is a number about hidden things whenever
    the recipient is not entitled to all seven subjects, and the recipient of an operational
    alert generally is not. Each line stands alone, which is also what makes them safe to
    route separately.

    Delete this and the alert grows a total, which is the most natural thing in the world to
    put at the top of one."""
    twice = [
        CanaryFinding(kind=Finding.LEAKED, asker="p_ben", question_id="g1", subject=CONTRACT),
        CanaryFinding(kind=Finding.LEAKED, asker="p_ben", question_id="g1", subject=CONTRACT),
        CanaryFinding(kind=Finding.UNLOCKED, asker="p_cara", question_id="g2", subject=SALARY),
    ]

    lines = alert_lines(twice)

    assert len(lines) == 2
    assert lines == tuple(sorted(lines)), "the alert's order moves between runs"
    for line in lines:
        assert not any(
            character.isdigit() for character in line.replace("g1", "").replace("g2", "")
        ), f"the alert line carries a number: {line}"


def test_the_corpus_this_harness_is_pointed_at_has_refusals_in_it() -> None:
    """**The check on the fixtures rather than on the code, and without it everything above
    is a demonstration.**

    Every test here builds its own answers, which proves the functions behave and proves
    nothing about whether there is anything to point them at. A corpus with no refusal case
    would make the whole refusal half of this module dead on arrival, and a corpus with no
    canary in a `must_not_contain` would make the leak half dead too.

    Read out of `tests.fixtures.golden` rather than counted here, so adding a question keeps
    this true and removing the last refusal case fails loudly.

    Delete this and the harness can be pointed at an empty corpus and report success."""
    refusals = [one for one in GOLDEN if one.expect is Expect.REFUSE]
    guarded = [one for one in GOLDEN if one.must_not_contain]

    assert refusals, "the corpus has no refusal case, so the refusal check runs on nothing"
    assert guarded, "no question forbids a substring, so the leak check runs on nothing"
    assert any(token in one.must_not_contain for one in guarded for token in canary_tokens()), (
        "no question forbids a canary token, so the tokens are checked against nothing"
    )
