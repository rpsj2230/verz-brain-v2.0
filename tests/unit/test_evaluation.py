"""The evaluation harness, and the ways a threshold quietly stops being one.

Every test here is about the same failure in a different costume: a suite that reports green
while something is wrong. That is worse than no suite, because the green is read as evidence
and the reading is wrong.

The three shapes it takes are a permission failure averaged into a percentage until it
disappears, a decline that stays above a fixed floor for ever, and a baseline the harness
moves itself. Each has its own test and each has a positive sibling, because a harness that
fails everything is as useless as one that passes everything and is caught by none of the
same assertions.

Task ids: M28.1.3
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dataclass_fields

import pytest

from brain.ops.evaluation import (
    PERMISSION_FAILURES_ALLOWED,
    QUALITY_FLOOR,
    Baseline,
    CaseResult,
    Severity,
    accept_baseline,
    cases_from,
    report_lines,
    score,
)


def quality(qid: str, *, passed: bool) -> CaseResult:
    return CaseResult(
        question_id=qid,
        severity=Severity.QUALITY,
        passed=passed,
        reason="" if passed else "the answer omitted the fact",
    )


def permission(qid: str, *, passed: bool) -> CaseResult:
    return CaseResult(
        question_id=qid,
        severity=Severity.PERMISSION,
        passed=passed,
        reason="" if passed else "the answer carried a restricted value",
    )


def test_one_permission_failure_fails_a_run_that_is_otherwise_perfect() -> None:
    """**The property the whole module is built around.**

    An evaluation suite produces a percentage and a percentage acquires a floor. For answer
    quality that is right. For a permission it means five per cent of questions showed
    somebody something they may not see, reported as a rounding error.

    The run below is ninety-nine cases passing and one permission case failing, which is a
    share of 0.99 against a floor of 0.9. Every averaging arrangement passes it. This does
    not, because permission cases are counted and never averaged.

    Delete this and the one thing the system exists to guarantee becomes a percentage."""
    results = [quality(f"q{n}", passed=True) for n in range(99)]
    results.append(permission("p1", passed=False))

    verdict = score(results)

    assert verdict.may_merge is False
    assert verdict.permission_failures == ("p1",)
    assert verdict.quality_share == 1.0, "the quality cases all passed and still it fails"


def test_a_clean_run_merges() -> None:
    """The positive sibling, and without it every assertion here is satisfied by a `score`
    that never lets anything merge.

    A harness that fails everything is switched off in a week, and switched off is the same
    outcome as absent, reached more expensively.

    Delete this and `score` can return False unconditionally with the whole file green."""
    verdict = score([quality("q1", passed=True), permission("p1", passed=True)])

    assert verdict.may_merge is True
    assert verdict.reasons == ()
    assert verdict.permission_failures == ()


def test_there_is_no_number_of_permission_failures_anybody_may_allow() -> None:
    """The threshold for a permission failure is zero and there is no parameter for it.

    A keyword argument here would be a dial somebody turns to make a build pass on the
    afternoon they need to ship, and the one thing that must not have a dial is the one thing
    the system guarantees. The quality floor is a parameter, deliberately, because an answer
    being less good is a matter of degree.

    Asserted on the signature rather than on behaviour, because behaviour today says nothing
    about whether the dial can be added tomorrow.

    Delete this and `permission_failures_allowed=1` appears in a signature and every other
    test in this file still passes."""
    parameters = set(inspect.signature(score).parameters)

    assert PERMISSION_FAILURES_ALLOWED == 0
    assert "quality_floor" in parameters, "the quality floor should be adjustable"
    for forbidden in ("permission_floor", "permission_failures_allowed", "allow_permission"):
        assert forbidden not in parameters, (
            f"a caller can raise the permission threshold via {forbidden}"
        )


def test_a_quality_share_below_the_floor_does_not_merge() -> None:
    """The ordinary case the floor exists for: a system that was never good enough.

    Delete this and the floor is decorative."""
    results = [quality(f"q{n}", passed=n < 5) for n in range(10)]

    verdict = score(results)

    assert verdict.may_merge is False
    assert verdict.quality_share == 0.5
    assert any("floor" in reason for reason in verdict.reasons)


def test_a_fall_against_the_baseline_fails_even_while_above_the_floor() -> None:
    """**The decline a floor never notices.**

    A floor at 0.9 is satisfied for ever by a system that was at 0.98 and is now at 0.91.
    Every run is above the floor, every step is small, and nothing anywhere says the
    direction. That is the shape of every quality decline anybody has shipped.

    The run below scores 0.95 against a baseline of 1.0. It is comfortably above the floor
    and it is worse than it was, and only the comparison catches it.

    Delete this and the suite reports green while the system gets steadily worse, which is
    exactly the state a suite is bought to prevent."""
    results = [quality(f"q{n}", passed=n < 19) for n in range(20)]

    verdict = score(results, baseline=Baseline(quality_share=1.0))

    assert verdict.quality_share == 0.95
    assert verdict.quality_share > QUALITY_FLOOR
    assert verdict.may_merge is False
    assert any("fell" in reason for reason in verdict.reasons)


def test_a_run_that_matches_or_beats_its_baseline_merges() -> None:
    """The positive sibling for the comparison. Without it, a `score` treating any baseline
    as a failure passes the test above.

    Equal is deliberately allowed. A suite that demanded improvement on every run would fail
    every run that changed nothing, which is most of them.

    Delete this and no run with a baseline can ever merge."""
    steady = [quality(f"q{n}", passed=n < 19) for n in range(20)]

    assert score(steady, baseline=Baseline(quality_share=0.95)).may_merge is True
    assert score(steady, baseline=Baseline(quality_share=0.90)).may_merge is True


def test_a_run_that_scored_nothing_does_not_merge() -> None:
    """A suite that ran nothing produces no failures, and "nothing failed" from a harness
    that asked nothing is the most dangerous green available: it is what a broken runner, a
    filter matching nothing and an import error all look like.

    Delete this and the way to make the evaluation pass is to break it."""
    verdict = score([])

    assert verdict.may_merge is False
    assert any("no cases" in reason for reason in verdict.reasons)


def test_a_corpus_with_no_quality_cases_is_not_scored_as_perfect() -> None:
    """The narrower version of the same trap. A share computed over an empty set has to be
    something, and 1.0 is the convenient answer and the wrong one: a perfect score from a
    corpus with nothing in it is the most convincing wrong answer available.

    The run below has a passing permission case and no quality cases at all, so it is not
    empty and it has no quality to report. It merges, because nothing is wrong, and the share
    it reports is not a claim that everything passed.

    Delete this and deleting the quality corpus raises the score."""
    verdict = score([permission("p1", passed=True)])

    assert verdict.may_merge is True
    assert verdict.quality_share == 0.0, "an absent corpus reports nothing, not perfection"


def test_every_reason_a_run_fails_is_reported_and_not_only_the_first() -> None:
    """A run told about one problem, fixed and re-run to be told the next is three round
    trips to learn what one message could have said.

    The run below is wrong three ways at once: a permission case failed, quality is under the
    floor, and it fell against its baseline.

    Delete this and `score` can return on the first problem, and every single-fault test in
    this file still passes."""
    results = [quality(f"q{n}", passed=n < 5) for n in range(10)]
    results.append(permission("p1", passed=False))

    verdict = score(results, baseline=Baseline(quality_share=1.0))

    assert len(verdict.reasons) == 3
    assert any("permission" in one for one in verdict.reasons)
    assert any("floor" in one for one in verdict.reasons)
    assert any("fell" in one for one in verdict.reasons)


def test_the_baseline_cannot_record_that_permission_cases_failed() -> None:
    """A baseline is a thing a run is allowed to match. Recording that some permission cases
    failed would make failing them the standard, which is the ratchet in its purest form.

    Asserted on the model's fields rather than on behaviour, because a field added today is
    unused and load-bearing tomorrow.

    Delete this and `permission_failures: int = 0` appears on the baseline, and the next
    person to have a bad afternoon sets it to 1."""
    names = {f.name for f in dataclass_fields(Baseline)}

    assert names == {"quality_share"}
    for forbidden in ("permission_failures", "permission_share", "allowed"):
        assert forbidden not in names


def test_a_failing_run_cannot_become_the_baseline() -> None:
    """The ratchet performed in one step rather than twenty.

    `accept_baseline` is the only way the bar moves, and it refuses a run that does not
    merge, so the bar cannot be lowered to whatever today happened to produce.

    Delete this and a bad run is recorded as the new normal by the same command that reports
    it as bad."""
    bad = score([quality("q1", passed=False)])

    with pytest.raises(ValueError, match="does not merge"):
        accept_baseline(bad)


def test_a_passing_run_becomes_a_baseline_at_the_share_it_scored() -> None:
    """The positive sibling. Without it, `accept_baseline` raising unconditionally passes the
    test above and the baseline can never be moved at all.

    Delete this and there is no way to record an improvement, so the baseline stays at
    whatever it was first set to for ever."""
    good = score([quality(f"q{n}", passed=True) for n in range(10)])

    assert accept_baseline(good) == Baseline(quality_share=1.0)


def test_nothing_in_this_module_writes_the_baseline_it_compares_against() -> None:
    """A harness that recorded its own baseline accepts whatever it just measured, so every
    acceptable loss becomes the new normal and the next loss is measured from there. Each
    step looks fine and the sum does not.

    Checked by reading the module's source for a write, because the property is "nothing here
    touches a file" and that is a claim about the whole module rather than about one function.

    Delete this and `accept_baseline` grows a `path` argument, which is one line and reads
    like a convenience."""
    from brain.ops import evaluation

    source = inspect.getsource(evaluation)

    for forbidden in ("write_text(", "open(", "Path(", "json.dump"):
        assert forbidden not in source, (
            f"the module contains {forbidden}, so it can record its own baseline"
        )


def test_a_failing_case_that_says_why_nowhere_cannot_be_constructed() -> None:
    """A failure with no reason is a red build somebody has to reproduce locally to
    understand, which is how a suite becomes something people rerun until it passes.

    Refused at construction rather than filtered later, so the useless result never exists.

    Delete this and a runner that loses its error text still reports failures, and each one
    costs somebody an afternoon."""
    with pytest.raises(ValueError, match="says why nowhere"):
        CaseResult(question_id="q1", severity=Severity.QUALITY, passed=False)

    with pytest.raises(ValueError, match="names nothing"):
        CaseResult(question_id="", severity=Severity.QUALITY, passed=True)


def test_severity_is_taken_from_the_corpus_and_never_inferred_from_the_wording() -> None:
    """`tests.fixtures.golden` says it in its own docstring: "how many hours are left" and
    "how many clients are worth over 50k" read alike and are entirely different questions
    about permission.

    So `cases_from` takes the permission ids and classifies by membership. A function that
    inferred severity from the question text would put the most important classification in
    the system into a substring match over English.

    **The ids below are chosen so a wording guess gets both of them wrong**, and an earlier
    version of this test was not. It used `g_money` for the permission case, so a mutation
    replacing the membership check with a substring match on "money" produced the right answer
    and survived. The test data was picked to read well rather than to discriminate, which is
    the same failure as comparing a constant against itself.

    So the permission case is about hours and says nothing about money, and the quality case
    says money and is not a permission question at all. Any classifier reading the text fails
    in both directions.

    Delete this and severity becomes a guess, and the guess decides whether a failure is a
    percentage or a blocker."""
    cases = cases_from(
        {"g_hours_left_by_department": False, "g_money_owed_total": False},
        permission_ids=("g_hours_left_by_department",),
        reasons={
            "g_hours_left_by_department": "showed another department's hours",
            "g_money_owed_total": "missed the figure",
        },
    )

    by_id = {one.question_id: one for one in cases}
    assert by_id["g_hours_left_by_department"].severity is Severity.PERMISSION
    assert by_id["g_money_owed_total"].severity is Severity.QUALITY


def test_the_report_names_cases_and_carries_no_figure_about_a_person() -> None:
    """A suite report is exactly the kind of document that gets forwarded, and "Priya's
    persona failed four cases" is a statement about what Priya cannot see.

    So the lines name question ids and the quality share, and nothing joins a count to a
    person. The corpus is what knows who asked.

    Delete this and the most readable improvement anybody could make to this report is the
    one that turns it into a disclosure."""
    results = [quality(f"q{n}", passed=n < 5) for n in range(10)]
    results.append(permission("p1", passed=False))

    lines = report_lines(score(results))

    assert any("p1" in line for line in lines)
    joined = " ".join(lines)
    for forbidden in ("persona", "principal", "asked by", "for p_"):
        assert forbidden not in joined, f"the report joins a figure to {forbidden}"


def test_a_passing_report_is_one_line_that_says_it_passed() -> None:
    """The positive sibling for the report, and the thing CI prints on a good day.

    Delete this and the passing case can print nothing at all, which reads in a CI log
    exactly like a step that did not run."""
    lines = report_lines(score([quality("q1", passed=True)]))

    assert len(lines) == 1
    assert "passing" in lines[0]
