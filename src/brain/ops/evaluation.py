"""Scoring a run of golden questions, and deciding whether it may merge.

`brain.ops.canaries` asks whether a specific thing leaked. This asks the broader question a
suite of golden questions answers: did the system behave the way the corpus says it should,
and is it behaving worse than it was yesterday.

**A threshold that lets a permission failure through is not a threshold.** This is the whole
design and everything else follows from it. An evaluation suite naturally produces a
percentage, and a percentage naturally acquires a floor: ninety-five per cent passing, ship
it. That is a reasonable way to manage answer quality and a catastrophic way to manage
permissions, because the five per cent is not noise, it is five questions where somebody saw
something they should not have. So cases are scored in two classes and only one of them has a
percentage.

`Severity.PERMISSION` cases have a threshold of zero failures and no arithmetic anywhere near
them. A single one failing fails the run, whatever the rest did. `Severity.QUALITY` cases have
a floor, because an answer being less good is a matter of degree and a suite with no tolerance
at all is a suite somebody switches off the first week. See
`A_PERCENTAGE_IS_THE_WRONG_INSTRUMENT_FOR_A_PERMISSION`.

**A regression is measured against a recorded baseline rather than against a fixed number.**
A fixed floor at ninety per cent is satisfied for ever by a system that was at ninety-eight
and is now at ninety-one, which is the shape of every quality decline anybody has ever
shipped. So the run compares against what was recorded last, and a fall is a failure even
when the absolute figure is still above the floor. Both checks apply: the floor catches a
system that was never good enough, and the comparison catches one that stopped being.

**The baseline may only move down by somebody saying so.** Nothing here writes it. A harness
that recorded its own baseline on every run would ratchet quietly downwards, one acceptable
loss at a time, and each individual step would look fine. `accept_baseline` produces the new
record and a caller writes it, which means the change appears in a commit with a person's name
on it.

**No count attached to a person leaves this module.** `report_lines` names the failing cases
by question id and gives the quality share, which is what a maintainer needs. What it never
produces is a per-persona figure: "Priya's persona failed four cases" is a statement about
what Priya cannot see, and a suite report is exactly the kind of document that gets forwarded.
Cases are identified by question id, and the corpus is what knows who asked.

**Nothing here runs anything.** It takes observations and returns verdicts, so a scored run is
testable without a model, a socket or a clock, which is the same split `brain.ops.limits` and
`brain.ops.canaries` make.

**M28.1.4 is not claimed and this is the reason.** That leaf is CI blocking a merge on a
regression, and blocking needs observations: something has to ask the golden questions and
record what came back. Nothing does, because there is no route behind the gate to ask one
through. A CI step added today would score an empty run, `score` would correctly refuse it,
and the build would be red for ever, so the wiring would be reverted within the hour by
somebody who was right to revert it. The scoring and the thresholds are what can be built
before the runner exists, and they are what is here.

Task ids: M28.1.3
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: Why permission cases are not scored as a percentage with everything else.
A_PERCENTAGE_IS_THE_WRONG_INSTRUMENT_FOR_A_PERMISSION = (
    "An evaluation suite produces a percentage and a percentage acquires a floor, and for "
    "answer quality that is right: an answer being less good is a matter of degree. For a "
    "permission it is not. Ninety-five per cent passing means five per cent of the questions "
    "showed somebody something they may not see, and there is no floor at which that is "
    "acceptable, so permission cases are counted rather than averaged and the threshold is "
    "zero. Mixing them into one figure is how a permission failure becomes a rounding error."
)

#: Why the run compares against a recorded baseline as well as a floor.
A_FLOOR_ALONE_NEVER_NOTICES_A_DECLINE = (
    "A floor at ninety per cent is satisfied for ever by a system that was at ninety-eight "
    "and is now at ninety-one. That is the shape of every quality decline anybody has "
    "shipped: each step is small, each run is above the floor, and nothing says the "
    "direction. So the run also compares against what was recorded last, and a fall fails "
    "even while the absolute figure is still fine."
)

#: Why nothing in this module writes the baseline it compares against.
A_HARNESS_THAT_RECORDS_ITS_OWN_BASELINE_RATCHETS_DOWNWARDS = (
    "A suite that wrote its baseline on every run would accept whatever it just measured, "
    "so every acceptable loss becomes the new normal and the next loss is measured from "
    "there. Each step looks fine and the sum does not. `accept_baseline` returns the record "
    "and a caller writes it, so lowering the bar is an edit somebody makes, in a commit with "
    "their name on it, rather than something the harness does on a Tuesday."
)

#: The share of quality cases that must pass. A floor, not a target.
#:
#: Set from what the corpus is for rather than from what feels safe. The quality cases ask
#: whether an answer carried the fact it should have, and a suite tolerating more than one in
#: ten missing is one nobody trusts to catch anything. It is deliberately not 1.0: an answer
#: is a model's wording, and a corpus with no tolerance is a corpus that fails on a synonym
#: and gets switched off in a fortnight.
QUALITY_FLOOR = 0.9

#: How many permission cases may fail. Zero, and it is not configurable.
#:
#: A parameter here would be a number somebody could raise to make a build pass, and the one
#: thing that must not have a dial on it is the one thing the system exists to guarantee.
PERMISSION_FAILURES_ALLOWED = 0


class Severity(enum.StrEnum):
    """What class of thing a case checks, which decides how it is scored.

    Two members and no third. A middle category would be where every awkward case ends up,
    and its threshold would be argued about once and then never looked at again.
    """

    #: The answer showed something the asker may not see, or refused something they may.
    PERMISSION = "permission"
    #: The answer was correct or not. Wrong is bad and is not a disclosure.
    QUALITY = "quality"


@dataclass(frozen=True)
class CaseResult:
    """What one golden question did on one run."""

    question_id: str
    severity: Severity
    passed: bool
    #: Why it failed, in a sentence. Empty when it passed.
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.question_id:
            msg = "a case result with no question id names nothing anybody can look up"
            raise ValueError(msg)
        if not self.passed and not self.reason:
            msg = f"{self.question_id} failed and says why nowhere, so nobody can act on it"
            raise ValueError(msg)


@dataclass(frozen=True)
class Baseline:
    """What the suite scored last time, as recorded in the repository.

    Only the quality share is carried. There is deliberately no `permission_failures` field:
    a baseline is a thing a run is allowed to match, and recording that some permission cases
    failed would make failing them the standard. The permission threshold is zero for every
    run and there is nothing to compare it against.
    """

    quality_share: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.quality_share <= 1.0:
            msg = f"a quality share of {self.quality_share} is not a share of anything"
            raise ValueError(msg)


@dataclass(frozen=True)
class Verdict:
    """Whether a run may merge, and everything a maintainer needs to see why not."""

    #: Nothing wrong: no permission failure, above the floor, no fall against the baseline.
    may_merge: bool
    #: Question ids of permission cases that failed, sorted. Any entry means `may_merge` is
    #: False, whatever the shares say.
    permission_failures: tuple[str, ...]
    #: The share of quality cases that passed this run.
    quality_share: float
    #: What the quality share was last time, or None on a run with no baseline.
    previous_quality_share: float | None
    #: One line per reason it may not merge, in the order a reader should act on them.
    reasons: tuple[str, ...]


def _share(results: Sequence[CaseResult], severity: Severity) -> float | None:
    """The passing share for one severity, or None when the corpus has no such case.

    None rather than 1.0, and the distinction is the point: a suite with no quality cases at
    all scores a perfect share under any average-of-nothing convention, and a perfect score
    from an empty corpus is the most convincing wrong answer available.
    """
    of_this_kind = [one for one in results if one.severity is severity]
    if not of_this_kind:
        return None
    return sum(1 for one in of_this_kind if one.passed) / len(of_this_kind)


def score(
    results: Sequence[CaseResult],
    *,
    baseline: Baseline | None = None,
    quality_floor: float = QUALITY_FLOOR,
) -> Verdict:
    """Whether this run may merge.

    Three independent reasons it may not, checked in the order they matter and all reported
    rather than the first one raising. A run told about one problem, fixed, and re-run to be
    told about the next is three round trips to learn what one message could have said, which
    is the argument `brain.ops.wiring.budget_breaches` makes for returning every breach.

    An empty run does not merge. A suite that ran nothing produces no failures, and a verdict
    of "nothing failed" from a harness that asked nothing is the most dangerous green there
    is: it is what a broken runner, a bad filter or an import error all look like.
    """
    reasons: list[str] = []

    failed_permissions = tuple(
        sorted(
            one.question_id
            for one in results
            if one.severity is Severity.PERMISSION and not one.passed
        )
    )
    if len(failed_permissions) > PERMISSION_FAILURES_ALLOWED:
        reasons.append(
            f"permission cases failed: {', '.join(failed_permissions)}. There is no share of "
            "these that may fail, so this does not merge whatever the quality figures say."
        )

    quality = _share(results, Severity.QUALITY)
    permission = _share(results, Severity.PERMISSION)
    if quality is None and permission is None:
        reasons.append(
            "the run scored no cases at all, which is what a broken runner, a filter that "
            "matched nothing and an import error all look like"
        )

    share = 0.0 if quality is None else quality
    if quality is not None and quality < quality_floor:
        reasons.append(f"quality is {quality:.3f} against a floor of {quality_floor:.3f}")

    previous = None if baseline is None else baseline.quality_share
    if previous is not None and quality is not None and quality < previous:
        reasons.append(
            f"quality fell from {previous:.3f} to {quality:.3f}. A fall fails even above the "
            "floor: a floor alone never notices a decline."
        )

    return Verdict(
        may_merge=not reasons,
        permission_failures=failed_permissions,
        quality_share=share,
        previous_quality_share=previous,
        reasons=tuple(reasons),
    )


def accept_baseline(verdict: Verdict) -> Baseline:
    """The record a caller writes when it decides this run is the new normal.

    Returns rather than writes, which is the whole of
    `A_HARNESS_THAT_RECORDS_ITS_OWN_BASELINE_RATCHETS_DOWNWARDS`. Nothing in this module
    touches a file, so moving the bar is a commit somebody makes.

    Refuses a run that may not merge. Accepting a baseline from a failing run is exactly the
    ratchet the constant describes, performed in one step instead of twenty.
    """
    if not verdict.may_merge:
        msg = "this run does not merge, so it cannot become the baseline: " + "; ".join(
            verdict.reasons
        )
        raise ValueError(msg)
    return Baseline(quality_share=verdict.quality_share)


def report_lines(verdict: Verdict) -> tuple[str, ...]:
    """What CI prints. One line per reason, and a single line when there is nothing wrong.

    No count of failures and no per-person figure. A suite report is exactly the kind of
    document that gets forwarded, and "four cases failed for Priya's persona" is a statement
    about what Priya cannot see. Cases are named by question id; the corpus knows who asked.
    """
    if verdict.may_merge:
        return (f"evaluation: passing, quality {verdict.quality_share:.3f}",)
    return tuple(f"evaluation: {reason}" for reason in verdict.reasons)


def cases_from(
    observations: Mapping[str, bool],
    *,
    permission_ids: Iterable[str],
    reasons: Mapping[str, str] | None = None,
) -> tuple[CaseResult, ...]:
    """Turn a run's raw pass and fail marks into scored cases.

    A convenience for callers, and the reason it takes the permission ids rather than
    inferring them is that inferring severity from a question's wording is exactly the
    mistake `tests.fixtures.golden` warns about in its own docstring: "how many hours are
    left" and "how many clients are worth over 50k" read alike and are entirely different
    questions about permission. The corpus tags them; nothing here guesses.
    """
    said = dict(reasons or {})
    permission = set(permission_ids)
    return tuple(
        CaseResult(
            question_id=qid,
            severity=Severity.PERMISSION if qid in permission else Severity.QUALITY,
            passed=passed,
            reason="" if passed else said.get(qid, "the case did not meet its expectation"),
        )
        for qid, passed in sorted(observations.items())
    )
