"""The evening digest: what moved, what is late, and whether the wave lands on its date.

Two properties carry most of this file and both are about a forecast refusing to overclaim
in one direction without going quiet in the other.

**Refusing to forecast is for thin evidence, never for bad news.** A wave whose window has
closed with work still open is late as an observation, and a wave that closed nothing across
its whole history is late as a measurement. Neither is a shortage of evidence, and reporting
either as "not forecastable" would hide the single most important thing a digest can say
behind a word that reads like a technicality.

**A projection that was never computed is never rendered.** `Forecast.NOT_FORECASTABLE`
zeroes the rate and carries no date, `BurnDown.__post_init__` refuses the combination
outright, and `render` asks `forecast_made` rather than checking the date for None. Three
layers for one rule because the number outlives the caveat printed beside it.

Task ids: M38.3.3.2, M38.3.3.3
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from brain.core.field_policy import Classification
from brain.ops.digest import (
    DIGEST_CLASSIFICATION,
    FORECAST_TOLERANCE,
    MINIMUM_DAYS_OF_HISTORY,
    NO_RATE,
    NOTHING_CLOSED,
    NOTHING_TO_COMPARE,
    BurnDown,
    DayClosed,
    DigestError,
    Forecast,
    Plan,
    WaveWindow,
    burn_down,
    daily_digest,
    movement_since,
    overdue_leaves,
    plan_from_wave_reports,
    render,
)
from brain.wave_report import ModuleLine, WaveReport

TODAY = date(2026, 9, 6)


def _plan(
    leaf_waves: dict[str, int], closed: set[str], names: dict[int, str] | None = None
) -> Plan:
    return Plan(leaf_waves=leaf_waves, closed=frozenset(closed), wave_names=names or {2: "Data"})


def _window(wave: int, start: date, end: date) -> WaveWindow:
    return WaveWindow(wave=wave, start=start, end=end)


# --------------------------------------------------------------- what moved (M38.3.3.2)
def test_the_first_run_says_it_cannot_tell_rather_than_that_everything_closed_today() -> None:
    """The failure that would happen on the evening the job is switched on. With no snapshot
    to compare against, treating the closed set as today's movement announces several hundred
    tasks as the day's work.

    Delete this and `measured` can default to True, which makes the first digest the least
    accurate one anybody ever reads and the one that sets expectations for the rest."""
    movement = movement_since(None, _plan({"M1.1": 2, "M1.2": 2}, {"M1.1", "M1.2"}))

    assert movement.measured is False
    assert movement.closed == ()
    assert movement.reopened == ()


def test_a_leaf_that_closed_since_the_snapshot_is_reported_as_closed() -> None:
    """The positive case, which a guard tested only by its refusals would not have. Delete
    this and `movement_since` could return an empty Movement for everything and every
    refusal test in this file would stay green."""
    movement = movement_since({"M1.1"}, _plan({"M1.1": 2, "M1.2": 2}, {"M1.1", "M1.2"}))

    assert movement.closed == ("M1.2",)
    assert movement.reopened == ()
    assert movement.measured is True


def test_a_leaf_that_left_the_closed_set_is_reported_as_reopened() -> None:
    """No task is created during a wave, so the only way a leaf becomes open again is a
    `Reopens:` trailer taking a claim back. Delete this and a withdrawn claim is silent, which
    is the one movement a reader most needs to see because it means a previous digest was
    wrong."""
    movement = movement_since({"M1.1", "M1.2"}, _plan({"M1.1": 2, "M1.2": 2}, {"M1.1"}))

    assert movement.reopened == ("M1.2",)
    assert movement.closed == ()


def test_an_id_outside_the_plan_cannot_have_moved_within_it() -> None:
    """A commit can name an id that is not a leaf, through a typo or by claiming an ancestor.
    Delete this and a typo in a commit trailer appears in the digest as a closed task, which
    is a number in a report that corresponds to nothing."""
    movement = movement_since({"M9.9.9"}, _plan({"M1.1": 2}, {"M1.1"}))

    assert movement.closed == ("M1.1",)
    assert movement.reopened == (), "an id that is not in the plan did not reopen within it"


def test_a_quiet_day_is_measured_and_empty_rather_than_unmeasured() -> None:
    """ "Nothing moved" and "we could not tell" are different facts and only one is fixed by
    running again tomorrow. Delete this and the two collapse, and a stalled week reads as a
    reporting problem."""
    movement = movement_since({"M1.1"}, _plan({"M1.1": 2}, {"M1.1"}))

    assert movement.measured is True
    assert movement.is_quiet is True


# --------------------------------------------------------------- what is late (M38.3.3.2)
def test_an_open_leaf_past_its_waves_window_is_overdue_oldest_first() -> None:
    """The ordering is the point as much as the selection: what has been late longest is what
    a reader should see first.

    **The plan is deliberately built in the wrong order.** An earlier version of this test
    listed the leaves already sorted, so replacing the sort with `tuple(late)` returned the
    same tuple and the mutation survived: dict order and due order agreed on that fixture, and
    a test whose input is already sorted cannot tell a sort from a passthrough. `M2.1` is
    inserted first and due last, so the two orders now disagree.

    Delete this and the list comes out in whatever order the WBS happened to be parsed in."""
    plan = _plan({"M2.1": 2, "M1.1": 1}, set())
    windows = {
        1: _window(1, date(2026, 8, 1), date(2026, 8, 20)),
        2: _window(2, date(2026, 8, 21), date(2026, 9, 1)),
    }

    late = overdue_leaves(plan, windows, today=TODAY)

    assert tuple(plan.leaf_waves) == ("M2.1", "M1.1"), "the fixture must not be pre-sorted"
    assert [item.leaf for item in late] == ["M1.1", "M2.1"]
    assert late[0].days_late == (TODAY - date(2026, 8, 20)).days
    assert late[0].due == date(2026, 8, 20)


def test_a_closed_leaf_is_never_overdue_however_late_its_window() -> None:
    """It is done. Reporting it as late puts permanent noise at the top of a report whose top
    has to stay worth reading, and a report people stop opening reports nothing."""
    plan = _plan({"M1.1": 1}, {"M1.1"})
    windows = {1: _window(1, date(2026, 8, 1), date(2026, 8, 20))}

    assert overdue_leaves(plan, windows, today=TODAY) == ()


def test_a_leaf_is_not_overdue_on_the_last_day_of_its_own_window() -> None:
    """The boundary, which is where an off-by-one in a date comparison lives. A wave is due at
    the end of its window, so the window's last day is not late. Delete this and `>=` can
    become `>`, and every wave reports its whole contents as overdue one day early."""
    plan = _plan({"M1.1": 1}, set())
    windows = {1: _window(1, date(2026, 8, 1), TODAY)}

    assert overdue_leaves(plan, windows, today=TODAY) == ()


def test_a_wave_with_no_window_contributes_no_overdue_leaves() -> None:
    """Deliberately a gap rather than a fallback to some other date. Inventing a target here
    would report leaves as late against a date the schedule never set, which is worse than
    saying nothing because it looks authoritative."""
    plan = _plan({"M1.1": 1, "M2.1": 2}, set())
    windows = {2: _window(2, date(2026, 8, 1), date(2026, 8, 20))}

    assert [item.leaf for item in overdue_leaves(plan, windows, today=TODAY)] == ["M2.1"]


def test_a_window_that_closes_before_it_opens_is_refused() -> None:
    """Every leaf in such a window is overdue on its first day, which reads as a catastrophic
    schedule rather than as a typo in a date."""
    with pytest.raises(DigestError, match="closes before it opens"):
        _window(1, date(2026, 9, 1), date(2026, 8, 1))


# --------------------------------------------------------------- the burn-down (M38.3.3.3)
def _history(days: int, closed_per_day: tuple[str, ...] = ()) -> list[DayClosed]:
    return [
        DayClosed(day=date(2026, 9, 6 - offset), closed=frozenset(closed_per_day))
        for offset in range(1, days + 1)
    ]


def test_a_window_that_has_closed_with_work_open_is_behind_on_the_thinnest_history() -> None:
    """**The property this module is arranged around.** Refusing to forecast is for thin
    evidence, never for bad news. A wave whose window has already passed is late as an
    observation, and no amount of missing history makes that less true.

    Delete this and the minimum-history check moves in front of this one, and the single most
    important sentence a digest can say gets replaced by "not forecastable" on exactly the
    days somebody most needs to read it."""
    plan = _plan({"M2.1": 2, "M2.2": 2}, {"M2.1"})
    window = _window(2, date(2026, 8, 1), date(2026, 8, 30))

    result = burn_down(plan, window, [], wave=2, today=TODAY)

    assert result.verdict is Forecast.BEHIND
    assert result.days_to_target == (date(2026, 8, 30) - TODAY).days < 0
    assert "observation rather than a forecast" in result.because


def test_a_measured_rate_of_nought_is_behind_and_not_unmeasurable() -> None:
    """The other half of the same rule. The rate was measured and it is nought, which means
    the wave does not land on its date or on any other date. Calling that a shortage of
    evidence hides a stall behind a word that reads like a technicality.

    Delete this and a wave that has closed nothing for a fortnight reports as
    `not_forecastable`, which is the sentence a reader skips."""
    plan = _plan({"M2.1": 2, "M2.2": 2}, set())
    window = _window(2, date(2026, 9, 1), date(2026, 9, 30))

    result = burn_down(plan, window, _history(MINIMUM_DAYS_OF_HISTORY), wave=2, today=TODAY)

    assert result.verdict is Forecast.BEHIND
    assert result.rate_per_day == NO_RATE
    assert result.projected_finish is None
    assert "measured rate" in result.because


def test_too_little_history_yields_no_rate_and_no_date() -> None:
    """The refusal that is genuinely about evidence. The numbers are zeroed rather than
    computed and labelled, because the label is what gets cropped out of the screenshot and
    the number is what gets quoted in a meeting."""
    plan = _plan({"M2.1": 2, "M2.2": 2}, set())
    window = _window(2, date(2026, 9, 1), date(2026, 9, 30))

    result = burn_down(plan, window, _history(MINIMUM_DAYS_OF_HISTORY - 1), wave=2, today=TODAY)

    assert result.verdict is Forecast.NOT_FORECASTABLE
    assert result.rate_per_day == NO_RATE
    assert result.projected_finish is None


def test_a_wave_closing_fast_enough_is_on_track_and_carries_its_projection() -> None:
    """The positive case, and the only path that produces a date. A module tested only by its
    refusals is satisfied by one that never forecasts at all, which would make the whole
    burn-down decorative."""
    plan = _plan({f"M2.{n}": 2 for n in range(1, 11)}, {"M2.1", "M2.2"})
    window = _window(2, date(2026, 9, 1), date(2026, 12, 31))
    history = [
        DayClosed(day=date(2026, 9, 6 - offset), closed=frozenset({f"M2.{offset + 2}"}))
        for offset in range(1, 5)
    ]

    result = burn_down(plan, window, history, wave=2, today=TODAY)

    assert result.verdict is Forecast.ON_TRACK
    assert result.rate_per_day > NO_RATE
    assert result.projected_finish is not None
    assert result.projected_finish <= window.end


def test_a_wave_closing_too_slowly_is_behind_and_still_carries_its_projection() -> None:
    """Behind with a date and behind without one are different messages: this one says when it
    would land, which is what turns "we are late" into a scope conversation. Delete this and
    the on-track path can be written as `projected <= target or NOT_FORECASTABLE`, which loses
    the date exactly when it is most useful."""
    plan = _plan({f"M2.{n}": 2 for n in range(1, 41)}, {"M2.1"})
    window = _window(2, date(2026, 9, 1), date(2026, 9, 10))
    history = [
        DayClosed(day=date(2026, 9, 6 - offset), closed=frozenset({"M2.1"} if offset == 1 else ()))
        for offset in range(1, 5)
    ]

    result = burn_down(plan, window, history, wave=2, today=TODAY)

    assert result.verdict is Forecast.BEHIND
    assert result.projected_finish is not None
    assert result.projected_finish > window.end


def test_a_finished_wave_is_on_track_with_nothing_left_to_project() -> None:
    """Nothing remains, so there is no rate to apply and no date to produce. Delete this and a
    completed wave divides by a rate it no longer has."""
    plan = _plan({"M2.1": 2}, {"M2.1"})

    result = burn_down(
        plan, _window(2, date(2026, 9, 1), date(2026, 9, 30)), [], wave=2, today=TODAY
    )

    assert result.verdict is Forecast.ON_TRACK
    assert result.remaining == 0
    assert result.projected_finish is None


def test_a_wave_with_no_target_date_does_not_invent_one() -> None:
    """The target is the schedule's and is never computed here. Delete this and the digest
    grows a second opinion about when a wave is due, which is the disagreement between two
    pages this repository has already had once."""
    result = burn_down(_plan({"M2.1": 2}, set()), None, [], wave=2, today=TODAY)

    assert result.verdict is Forecast.NOT_FORECASTABLE
    assert result.target is None
    assert result.days_to_target is None


def test_a_wave_with_no_leaves_has_no_burn_down_to_draw() -> None:
    """Distinct from a finished wave: nought of nought is not completion. Delete this and an
    empty wave reports as on track, which is a green tick for work nobody has planned."""
    result = burn_down(_plan({"M1.1": 1}, set()), None, [], wave=2, today=TODAY)

    assert result.verdict is Forecast.NOT_FORECASTABLE
    assert result.total == 0


def test_a_day_counted_twice_in_the_history_is_refused() -> None:
    """A day weighted twice makes the rate read high, and a rate that reads high produces a
    projected date that is too early. That is the direction of error nobody catches, because
    it agrees with what everyone hopes."""
    day = DayClosed(day=date(2026, 9, 5), closed=frozenset({"M2.1"}))

    with pytest.raises(DigestError, match="appears twice"):
        burn_down(_plan({"M2.1": 2}, set()), None, [day, day], wave=2, today=TODAY)


def test_a_day_in_the_future_is_not_a_completed_day_of_evidence() -> None:
    """History is completed days. A partial day drags the rate down by however early the job
    happens to run, and a future one makes the rate look settled when it is not."""
    ahead = DayClosed(day=date(2026, 9, 7), closed=frozenset())

    with pytest.raises(DigestError, match="not a completed day"):
        burn_down(_plan({"M2.1": 2}, set()), None, [ahead], wave=2, today=TODAY)


def test_a_projection_cannot_exist_under_a_verdict_that_made_none() -> None:
    """The invariant stated at the type rather than at the call site. A date produced where
    none was computed is exactly the promise this module exists not to make, and it would be
    rendered as confidently as a real one."""
    with pytest.raises(DigestError, match="promise this module exists to not make"):
        BurnDown(
            wave=2,
            name="Data",
            total=4,
            closed=1,
            remaining=3,
            target=date(2026, 9, 30),
            days_to_target=24,
            days_of_history=0,
            rate_per_day=NO_RATE,
            projected_finish=date(2026, 9, 20),
            verdict=Forecast.NOT_FORECASTABLE,
            because="thin evidence",
        )


def test_a_burn_down_states_the_reason_its_verdict_came_from() -> None:
    """A verdict nobody can explain is reversed the first time somebody wants the other
    answer, and the reversal is permanent because nobody knows what the original protected."""
    with pytest.raises(DigestError, match="a date with no argument"):
        BurnDown(
            wave=2,
            name="Data",
            total=1,
            closed=0,
            remaining=1,
            target=None,
            days_to_target=None,
            days_of_history=0,
            rate_per_day=NO_RATE,
            projected_finish=None,
            verdict=Forecast.NOT_FORECASTABLE,
            because="   ",
        )


def test_the_minimum_history_is_derived_from_the_tolerance_rather_than_chosen() -> None:
    """A minimum picked by taste drifts to whatever makes today's digest produce a number.
    Deriving it means changing the tolerance changes the evidence bar with it, which is the
    relationship somebody would otherwise have to remember."""
    assert math.ceil(1 / FORECAST_TOLERANCE) == MINIMUM_DAYS_OF_HISTORY
    assert MINIMUM_DAYS_OF_HISTORY > 1


# --------------------------------------------------------------- the plan
def test_a_leaf_in_two_waves_is_refused_rather_than_counted_twice() -> None:
    """It would be counted in both totals and reported overdue against whichever window the
    loop read last, which makes the answer depend on iteration order. This repository has
    already shipped two pages disagreeing about wave totals once."""
    reports = [
        WaveReport(
            wave=1,
            name="Foundation",
            generated_at=None,  # type: ignore[arg-type]
            modules=[ModuleLine(module="M1", name="One", closed=(), open=("M1.1",))],
        ),
        WaveReport(
            wave=2,
            name="Data",
            generated_at=None,  # type: ignore[arg-type]
            modules=[ModuleLine(module="M1", name="One", closed=(), open=("M1.1",))],
        ),
    ]

    with pytest.raises(DigestError, match="appears in wave"):
        plan_from_wave_reports(reports)


def test_the_plan_takes_its_waves_from_the_reports_that_already_resolved_them() -> None:
    """Reading the WBS here would be a third implementation of the per-leaf wave lookup, and
    the one that disagrees is discovered as two pages showing different totals. Delete this
    and the digest can start resolving waves itself."""
    reports = [
        WaveReport(
            wave=2,
            name="Data",
            generated_at=None,  # type: ignore[arg-type]
            modules=[ModuleLine(module="M2", name="Two", closed=("M2.1",), open=("M2.2",))],
        )
    ]

    plan = plan_from_wave_reports(reports)

    assert plan.leaf_waves == {"M2.1": 2, "M2.2": 2}
    assert plan.closed == frozenset({"M2.1"})
    assert plan.name_of(2) == "Data"
    assert plan.name_of(9) == "Wave 9", "an unnamed wave falls back to its number"


# --------------------------------------------------------------- the message
def test_the_first_run_renders_that_it_cannot_compare_rather_than_a_quiet_day() -> None:
    """Two different sentences for two different facts, and only one of them is fixed by
    waiting a day. Delete this and the first digest claims a quiet day, which is a false
    statement about the build on the one evening nobody can check it against yesterday."""
    digest = daily_digest(
        plan=_plan({"M2.1": 2}, {"M2.1"}),
        windows={2: _window(2, date(2026, 9, 1), date(2026, 9, 30))},
        history=[],
        wave=2,
        today=TODAY,
        previously_closed=None,
    )

    text = render(digest)

    assert NOTHING_TO_COMPARE in text
    assert NOTHING_CLOSED not in text


def test_a_quiet_day_produces_a_message_that_says_nothing_closed() -> None:
    """A digest that only ever reports movement cannot report a stall, and the stall is the
    thing worth reading. Delete this and a quiet day can become an absent message, which is
    indistinguishable from the job having failed."""
    digest = daily_digest(
        plan=_plan({"M2.1": 2, "M2.2": 2}, {"M2.1"}),
        windows={2: _window(2, date(2026, 9, 1), date(2026, 9, 30))},
        history=[],
        wave=2,
        today=TODAY,
        previously_closed={"M2.1"},
    )

    text = render(digest)

    assert NOTHING_CLOSED in text
    assert "Nothing reopened today." in text
    assert text.strip(), "a quiet day is still a message"


def test_no_date_is_rendered_when_no_projection_was_made() -> None:
    """The last of the three layers holding one rule. The type refuses the combination, the
    verdict carries `forecast_made`, and the renderer asks it rather than testing the date for
    None. Delete this and a renderer that checks `is not None` prints whatever a future field
    happens to hold."""
    digest = daily_digest(
        plan=_plan({"M2.1": 2, "M2.2": 2}, set()),
        windows={2: _window(2, date(2026, 9, 1), date(2026, 9, 30))},
        history=[],
        wave=2,
        today=TODAY,
        previously_closed={"M2.1"},
    )

    text = render(digest)

    assert digest.burn_down is not None
    assert digest.burn_down.verdict is Forecast.NOT_FORECASTABLE
    assert "reaching nought on" not in text


def test_the_digest_names_tasks_and_waves_and_never_a_person() -> None:
    """The absences are the design. A digest is a message about a plan, and the moment it
    carries a commit subject it carries whatever somebody typed, attributed to them by the
    fact that they closed the task beside it.

    Delete this and an author field can be added to `DailyDigest` and rendered, which turns a
    progress report into a performance report."""
    reports = [
        WaveReport(
            wave=2,
            name="Data",
            generated_at=None,  # type: ignore[arg-type]
            modules=[ModuleLine(module="M2", name="Two", closed=("M2.1",), open=("M2.2",))],
            commits=[{"subject": "Rupash fixed the thing", "author": "rupash@verzdesign.com"}],
        )
    ]

    text = render(
        daily_digest(
            plan=plan_from_wave_reports(reports),
            windows={2: _window(2, date(2026, 9, 1), date(2026, 9, 30))},
            history=[],
            wave=2,
            today=TODAY,
            previously_closed=set(),
        )
    )

    assert "rupash" not in text.lower()
    assert "fixed the thing" not in text
    assert "M2.1" in text, "the task id is what the digest does report"


def test_the_digest_is_classified_internal_so_a_delivery_leaf_cannot_widen_it() -> None:
    """The digest names every open task in the plan, which is the shape of what is not built
    yet. That is an internal fact, and the classification travels with the content so the
    channel adapter in M38.3.3.4 has to carry it rather than decide it."""
    assert DIGEST_CLASSIFICATION is Classification.INTERNAL
