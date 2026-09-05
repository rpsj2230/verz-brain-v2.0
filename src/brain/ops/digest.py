"""The evening digest: what the plan did today, and whether the wave still lands on its date.

`brain.status` computes what is closed and `brain.wave_report` says what one wave delivered.
Neither says what changed *since yesterday*, and neither says whether the wave is going to
make its date. That is this module, and it is the content of the daily message rather than
the message: **there is no channel here and no send.** M38.3.3.1 and M38.3.3.4 ship the
Lark delivery in wave two, and everything below is a pure function so that arrival is one
call site: build the plan, call `daily_digest`, render, hand the string and
`DIGEST_CLASSIFICATION` to an adapter. `brain.ops.denial_alerts` made the same split for the
same reason, and stated it the same way.

**A burn-down against a target date is a forecast, and a forecast stated as a number is read
as a promise.** A rate over two days of history is those two days. `brain.knowledge.quality`
settled the shape of this already: below `MINIMUM_JUDGED_CASES` it returns `NOT_MEASURABLE`
with the metrics reported as nought rather than a mean somebody screenshots, because the
screenshot outlives the caveat printed beside it. The same applies here and harder, because
a projected date is quoted back in a meeting. So `Forecast.NOT_FORECASTABLE` zeroes the rate
and returns no projected date at all, and `MINIMUM_DAYS_OF_HISTORY` is derived from the
tolerance rather than chosen. See `A_FORECAST_FROM_TOO_LITTLE_HISTORY_IS_NOT_A_FORECAST`.

**Two answers are refusals to forecast and two are not, and the difference matters.** A wave
whose window has already closed with work still open is late as an observation, not as a
projection, and it is reported as behind on two days of history exactly as on twenty. A wave
that has closed nothing across the whole history window is also behind: the rate is measured
and it is nought, and calling that unmeasurable would hide the single most important thing a
digest can say. Refusing to forecast is for thin evidence, never for bad news.

**Overdue is measured against the leaf's own wave, never its module's.** A leaf can sit in a
later wave than the module it belongs to, which is why `schedule.js` carries `LEAF_WAVE` and
why `brain.status.build_status` buckets per leaf. Reading the module's wave here would mark
M38's go-live leaves overdue from the first week, because M38 is a wave-0 module. That
resolution is not repeated below: `plan_from_wave_reports` takes it from the wave reports,
which already did it. See `OVERDUE_IS_MEASURED_AGAINST_THE_LEAFS_OWN_WAVE`.

**A digest that only ever reports movement cannot report a stall.** Zero closed is a real and
frequent answer and it is the one worth reading, which is the argument `brain.status` already
makes for keeping `closed_today` on the page. So a quiet day produces a digest that says
nothing closed, and never an empty message or no message. See
`A_QUIET_DAY_IS_A_RESULT_AND_NOT_AN_ABSENT_MESSAGE`.

**The digest is read by whoever can see the channel, so it is a disclosure surface even
though it is only about our own plan.** A task id, a wave and a date are facts about this
build. "Nine tasks closed by Priya" is a performance report nobody agreed to, it is derived
from an authorship field that exists for blame in `git`, and once it is in a channel it is
forwarded. Nothing here carries an author, a committer, an assignee or an owner, and nothing
carries free text from a commit either: a commit subject is the one field in this pipeline
that nobody validates, and it is where a client name or a person's name would arrive. See
`THE_DIGEST_ATTRIBUTES_WORK_TO_THE_PLAN_AND_NEVER_TO_A_PERSON`.

Rejected: computing the wave windows here from `schedule.js`. The windows come from capacity,
track splits and a working-day walk, all of which live in `docs/wbs/render.js`, and a second
implementation would give two target dates that drift, with the one on the digest being the
one nobody reviews. `brain.wave_report` refused the same duplication for the same reason and
took `due_dates` as a parameter; `windows` here is that parameter. A wave with no window
supplied is not overdue and is not forecast, which is a gap rather than a guess. See
`THE_TARGET_DATE_IS_THE_SCHEDULES_AND_IS_NEVER_COMPUTED_HERE`.

Rejected: per-leaf due dates from the tracker. `render.js` distributes a date to every leaf
across its module's window, which makes a leaf late for being fifteenth in a list rather than
for anything about the work; and it assigns from `WIN[m.wave]`, the module's window, so a leaf
moved to a later wave carries a date from a wave it is not in. The wave window is the
commitment and the leaf position is a drawing.

Rejected: a second walk of the history to read `Reopens:` trailers. `brain.status` owns commit
parsing, including the unit-separator trap that silently drops every one-line message, and a
second parser is a second place to get that wrong. Movement is instead one comparison of two
snapshots of the closed set, so what closed and what came back open cannot disagree about what
a close is. The cost is that the caller has to keep yesterday's snapshot, the way
`denial_alerts.AlertLog` is kept by somebody else, and the first ever run has none: that run
reports no movement rather than claiming the entire backlog closed today.

Rejected: suppressing the message when nothing moved. See the quiet-day argument above.

Nothing here reads a clock, opens a connection or runs `git`. `today` is a parameter and the
plan is a snapshot, for the reason `denial_alerts` gives about its window: a forecast that
read the clock could not be tested at the day boundary, which is the only part of a daily
report that is ever wrong.

Task ids: M38.3.3.2, M38.3.3.3
"""

from __future__ import annotations

import enum
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import MappingProxyType
from typing import Final

from brain.core.field_policy import Classification
from brain.wave_report import WaveReport


class DigestError(Exception):
    """A digest that would be assembled wrongly rather than one that reports badly.

    Outside the `brain.core.errors` taxonomy, like `brain.knowledge.quality.QualityError`:
    those five outcomes describe an answer given to a person, and this describes a refusal to
    assemble a report from inputs that contradict each other.
    """


# ------------------------------------------------------------------ written-down reasons
#: Why thin evidence produces no rate and no date rather than a rate with a caveat.
A_FORECAST_FROM_TOO_LITTLE_HISTORY_IS_NOT_A_FORECAST: Final = (
    "a burn-down projects a finish date from a rate, and with n days of history one ordinary "
    "day is a whole nth of that rate, so the date moves by about a nth of itself when one day "
    "changes; below MINIMUM_DAYS_OF_HISTORY the projection is a property of which days "
    "somebody happened to have rather than of the work, and it is stated in a channel where "
    "it is read as a commitment. So the rate is reported as nought and no date is produced at "
    "all, the shape brain.knowledge.quality uses for a metric over too few judged cases, "
    "because a number with a caveat beside it is screenshotted without the caveat"
)

#: Why an empty day still produces a message, and a full one.
A_QUIET_DAY_IS_A_RESULT_AND_NOT_AN_ABSENT_MESSAGE: Final = (
    "nothing closed today is the answer a reader most needs and the one a reporting tool is "
    "most likely to swallow, because a digest assembled from a list of events is empty when "
    "the list is empty and an empty message looks like a broken job rather than a stalled "
    "build; brain.status keeps closed_today on the page for this reason, and the same rule "
    "holds here, so a quiet day renders a sentence saying so and never nothing"
)

#: Why lateness is decided by the leaf's wave rather than by the wave its module sits in.
OVERDUE_IS_MEASURED_AGAINST_THE_LEAFS_OWN_WAVE: Final = (
    "a leaf can sit in a later wave than its module, which is what schedule.js LEAF_WAVE is "
    "for: M38 is a wave-zero module carrying go-live leaves that cannot happen before wave "
    "five, and reading the module's wave would report those as overdue from the first week "
    "while a wave-zero leaf parked in a late module would never be reported at all; the "
    "resolution is not repeated here, it is taken from the wave reports, which already did it"
)

#: What the digest may say about who did the work, which is nothing.
THE_DIGEST_ATTRIBUTES_WORK_TO_THE_PLAN_AND_NEVER_TO_A_PERSON: Final = (
    "a task id, a wave and a date are facts about this build and the status page already "
    "shows them to the client; nine tasks closed by a named colleague is a performance report "
    "nobody agreed to, assembled from an authorship field that exists so a change can be "
    "traced rather than so a person can be ranked, and a channel message is forwarded and "
    "pasted in a way a status page is not. So there is no author, committer, assignee or "
    "owner field anywhere here, and no free text from a commit either: a subject line is the "
    "one field in this pipeline that nothing validates"
)

#: Why the target date is supplied rather than derived.
THE_TARGET_DATE_IS_THE_SCHEDULES_AND_IS_NEVER_COMPUTED_HERE: Final = (
    "a wave window comes from capacity, track splits and a working-day walk in "
    "docs/wbs/render.js, so recomputing it here would put two target dates in the estate and "
    "the one on the evening message is the one nobody reviews; brain.wave_report refused the "
    "same duplication and took its due dates as a parameter, and windows is that parameter. A "
    "wave with no window is reported with no target and no forecast, which is a stated gap "
    "rather than a guessed date"
)


# ------------------------------------------------------------------ the thresholds
#: How far a projected finish may move on one ordinary day's evidence before the projection
#: is not worth stating. A quarter: a date that one day can shift by more than that is a
#: reading of that day.
FORECAST_TOLERANCE: Final = 0.25

#: The shortest history a rate may be computed over, derived rather than chosen.
#:
#: With `n` days of evidence a single ordinary day is one `n`th of the mean rate, and the
#: projected finish moves with it, so `n` below `1 / FORECAST_TOLERANCE` lets one day move the
#: date by more than the tolerance on its own. A test holds the two together, so tightening the
#: tolerance lengthens the required history rather than quietly permitting a shorter one. This
#: is the shape `brain.knowledge.quality.MINIMUM_JUDGED_CASES` uses, for the same reason.
MINIMUM_DAYS_OF_HISTORY: Final = math.ceil(1 / FORECAST_TOLERANCE)

#: How sensitive the digest is, for whoever hands it to a channel adapter.
#:
#: Decided here so it is decided once, as `denial_alerts.ALERT_CLASSIFICATION` is. INTERNAL
#: rather than CONFIDENTIAL: every fact in it is already on the status page, which M38.3.2.5
#: says is visible to the client, so claiming a higher ceiling would make every surface that
#: carries the digest declare one it needs for nothing else. Not PUBLIC, because the delivery
#: plan for an engagement is not something to put where it can be indexed.
DIGEST_CLASSIFICATION: Final[Classification] = Classification.INTERNAL

#: The rate reported when no rate was computed. A named constant rather than a bare nought at
#: four call sites, so the zeroing is visibly the same decision each time.
NO_RATE: Final = 0.0

_NO_LEAVES: Mapping[str, int] = MappingProxyType({})
_NO_NAMES: Mapping[int, str] = MappingProxyType({})


# ------------------------------------------------------------------ the plan
@dataclass(frozen=True)
class WaveWindow:
    """One wave's window, exactly as the schedule computed it, and never recomputed here.

    Dates rather than a length in days: a length would have to be walked forward over working
    days to become a date, which is the calculation `render.js` owns and the one this module
    refuses to hold a second copy of.
    """

    wave: int
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = (
                f"wave {self.wave} ends {self.end} and starts {self.start}; a window that "
                "closes before it opens makes every leaf in it overdue on the first day"
            )
            raise DigestError(msg)


@dataclass(frozen=True)
class Plan:
    """Every leaf, the wave that will actually do it, and which of them are closed now.

    A snapshot rather than something that reads `git`, for the reason `denial_alerts.AlertLog`
    is one: a report that fetched its own facts could not be run twice over the same day, and
    running it twice over the same day is the property the delivery job depends on.
    """

    #: Leaf id to the wave that will do it, already resolved per leaf. Insertion order is the
    #: order the reports were built in; nothing here depends on it.
    leaf_waves: Mapping[str, int] = _NO_LEAVES
    #: Which leaves are closed as of this snapshot.
    closed: frozenset[str] = frozenset()
    #: Wave number to the name the WBS gives it, for rendering only.
    wave_names: Mapping[int, str] = _NO_NAMES

    def leaves_in(self, wave: int) -> tuple[str, ...]:
        return tuple(leaf for leaf, in_wave in self.leaf_waves.items() if in_wave == wave)

    def name_of(self, wave: int) -> str:
        """The wave's name, falling back to its number the way `wave_report` does."""
        return self.wave_names.get(wave, f"Wave {wave}")


def plan_from_wave_reports(reports: Iterable[WaveReport]) -> Plan:
    """The plan, taken from wave reports rather than resolved again from the WBS.

    **This is the whole reason the digest does not read the WBS itself.** `build_wave_report`
    already decides which wave a leaf belongs to, using the per-leaf override, and two pages
    disagreeing about wave totals is a bug this repository has already had. Reading
    `leaf_waves` here would be a third implementation of that lookup; taking the answer from
    the report that already made it cannot disagree with the report.

    Nothing is copied from `WaveReport.commits`. Those carry subject lines, which are free
    text written by whoever made the commit, and the digest carries no free text from a
    commit. See `THE_DIGEST_ATTRIBUTES_WORK_TO_THE_PLAN_AND_NEVER_TO_A_PERSON`.
    """
    leaf_waves: dict[str, int] = {}
    closed: set[str] = set()
    names: dict[int, str] = {}
    for report in reports:
        names[report.wave] = report.name
        for module_line in report.modules:
            for leaf in module_line.closed:
                closed.add(leaf)
            for leaf in (*module_line.closed, *module_line.open):
                seen = leaf_waves.get(leaf)
                if seen is not None and seen != report.wave:
                    msg = (
                        f"{leaf} appears in wave {seen} and in wave {report.wave}; a leaf in "
                        "two waves is counted twice and is overdue against whichever window "
                        "the loop happened to read last"
                    )
                    raise DigestError(msg)
                leaf_waves[leaf] = report.wave
    return Plan(
        leaf_waves=MappingProxyType(leaf_waves),
        closed=frozenset(closed),
        wave_names=MappingProxyType(names),
    )


# ------------------------------------------------------------------ what moved (M38.3.3.2)
@dataclass(frozen=True)
class Movement:
    """What the plan did between two snapshots: what closed, and what came back open.

    Both sides of one comparison rather than two independently gathered lists, so they cannot
    disagree about what being closed means. `reopened` is what "tasks opened" is in a fixed
    plan: no task is created during a wave, so the only way a leaf becomes open again is a
    `Reopens:` trailer taking a claim back, which is exactly a leaf leaving the closed set.
    """

    closed: tuple[str, ...] = ()
    reopened: tuple[str, ...] = ()
    #: False when there was no previous snapshot to compare against, which is the first run.
    #: The alternative reading of a missing snapshot is that everything closed today, which is
    #: a digest announcing several hundred tasks on the evening the job is switched on.
    measured: bool = False

    @property
    def is_quiet(self) -> bool:
        """Nothing moved. A result, not an absence. See
        `A_QUIET_DAY_IS_A_RESULT_AND_NOT_AN_ABSENT_MESSAGE`."""
        return not self.closed and not self.reopened


def movement_since(previously_closed: Collection[str] | None, plan: Plan) -> Movement:
    """What has moved since a snapshot of the closed set.

    `plan.closed` is the other side of the comparison rather than a second argument, because
    two closed sets passed in separately can be two unrelated sets, and the failure is a
    plausible-looking digest rather than an error.

    Ids outside the plan are dropped. A commit can name an id that is not a leaf, through a
    typo or through claiming an ancestor, and `brain.status` already intersects with the
    leaves it matched for the same reason: an id that is not in the plan cannot have moved
    within it.
    """
    if previously_closed is None:
        return Movement(measured=False)
    leaves = set(plan.leaf_waves)
    before = set(previously_closed) & leaves
    after = set(plan.closed) & leaves
    # Sorted for a stable digest rather than for plan order, which two runs must agree on and
    # which set iteration does not give. Sorting ids as strings is not plan order and is not
    # claimed to be: `M12.1.10` sorts before `M12.1.2` and the plan does not mean that.
    return Movement(
        closed=tuple(sorted(after - before)),
        reopened=tuple(sorted(before - after)),
        measured=True,
    )


@dataclass(frozen=True)
class OverdueLeaf:
    """One open leaf whose wave's window has closed.

    `due` is the window's end and not a date computed for this leaf. There is no per-leaf
    commitment in this plan, only a per-wave one, and inventing one would report a leaf as
    late for having been listed fifteenth.
    """

    leaf: str
    wave: int
    due: date
    days_late: int


def overdue_leaves(
    plan: Plan, windows: Mapping[int, WaveWindow], *, today: date
) -> tuple[OverdueLeaf, ...]:
    """Every open leaf whose own wave's window has already closed (M38.3.3.2).

    A closed leaf is never overdue, the same rule `brain.wave_report` keeps: it is done, and
    reporting it as late puts permanent noise at the top of a report whose top has to stay
    worth reading.

    A wave with no window contributes nothing. That is deliberately a gap rather than a
    fallback to some other date. See `THE_TARGET_DATE_IS_THE_SCHEDULES_AND_IS_NEVER_COMPUTED_HERE`.

    Oldest first, so the thing that has been late longest is read first.
    """
    late: list[OverdueLeaf] = []
    for leaf, wave in plan.leaf_waves.items():
        if leaf in plan.closed:
            continue
        window = windows.get(wave)
        if window is None or window.end >= today:
            continue
        late.append(
            OverdueLeaf(leaf=leaf, wave=wave, due=window.end, days_late=(today - window.end).days)
        )
    return tuple(sorted(late, key=lambda item: (item.due, item.leaf)))


# ------------------------------------------------------------------ the burn-down (M38.3.3.3)
@dataclass(frozen=True)
class DayClosed:
    """One completed day of evidence: which leaves closed on it.

    Ids rather than a count, because a burn-down is per wave and a count would have to be
    apportioned by whoever kept it. Intersecting ids with the wave's leaves is exact and
    cannot attribute another wave's day to this one.
    """

    day: date
    closed: frozenset[str] = frozenset()


class Forecast(enum.StrEnum):
    """What the burn-down concluded, and only one of the three is a refusal to conclude.

    Three rather than two, for the reason `brain.knowledge.quality.Verdict` has three: "it
    does not land on the date" and "there is not enough evidence to say" are different facts
    about the world and lead to different next steps, and collapsing them makes a stall look
    like a measurement problem.
    """

    ON_TRACK = "on_track"
    BEHIND = "behind"
    NOT_FORECASTABLE = "not_forecastable"

    @property
    def forecast_made(self) -> bool:
        """Whether a projection exists to render.

        A property rather than a comparison written at each call site, so a fourth verdict is
        one edit here instead of a search for every `is Forecast.NOT_FORECASTABLE`. It is what
        a renderer asks before printing a date, and printing one that was never computed is
        the failure this whole module is arranged against.
        """
        return self is not Forecast.NOT_FORECASTABLE


@dataclass(frozen=True)
class BurnDown:
    """One wave against its target date, with the evidence the verdict came from.

    `because` is required and non-empty, for the reason `quality.Evaluation.because` is: a
    verdict nobody can explain is reversed the first time somebody wants the other answer, and
    the reversal is permanent because nobody knows what the original was protecting.

    `rate_per_day` is nought and `projected_finish` is None whenever no projection was made.
    The numbers are zeroed rather than computed and labelled, because the label is what gets
    cropped out of the screenshot.
    """

    wave: int
    name: str
    total: int
    closed: int
    remaining: int
    target: date | None
    #: Calendar days from today to the target. Negative once the window has closed, which is
    #: a fact a reader needs rather than a number to clamp at nought.
    days_to_target: int | None
    days_of_history: int
    rate_per_day: float
    projected_finish: date | None
    verdict: Forecast
    because: str

    def __post_init__(self) -> None:
        if not self.because.strip():
            msg = "a burn-down states its reason; a bare verdict is a date with no argument"
            raise DigestError(msg)
        if not self.verdict.forecast_made and self.projected_finish is not None:
            msg = (
                f"wave {self.wave} projects {self.projected_finish} under a verdict of "
                f"{self.verdict.value}; a date produced where none was computed is the "
                "promise this module exists to not make"
            )
            raise DigestError(msg)


def burn_down(
    plan: Plan,
    window: WaveWindow | None,
    history: Sequence[DayClosed],
    *,
    wave: int,
    today: date,
    minimum_days: int = MINIMUM_DAYS_OF_HISTORY,
) -> BurnDown:
    """How one wave stands against its target date (M38.3.3.3).

    The order of the answers is the function, and the first three are not forecasts:

    1. Nothing remains, so the wave is finished and there is nothing to project.
    2. No window was supplied, so there is no target to project against.
    3. The window has already closed with work still open. That is an observation and it is
       reported on two days of history exactly as on twenty. Refusing to forecast is for thin
       evidence, never for bad news.
    4. Below `minimum_days` of history there is no rate, so no rate and no date are given.
    5. A measured rate of nought is behind, not unmeasurable: the wave does not land at the
       rate it is actually going, and that is the sentence somebody needs to read.

    Days are calendar days on both sides of the comparison. Working days are the schedule's
    unit and were rejected here for one reason that settles it: this build closes leaves at
    weekends, and a working-day denominator divides a weekend's evidence by nought days. The
    cost is stated rather than hidden - a projection crossing a weekend assumes work happens
    on it, which for a period of paid delivery is optimistic - and the target date itself is
    still the schedule's, computed over working days by the schedule.

    `history` is completed days. A partial today would drag the rate down by however early the
    job runs, so a day after `today` is refused outright and the caller is expected to pass
    days that are over.
    """
    leaves = set(plan.leaves_in(wave))
    total = len(leaves)
    closed_count = len(leaves & plan.closed)
    remaining = total - closed_count
    target = window.end if window is not None else None
    days_to_target = (target - today).days if target is not None else None
    name = plan.name_of(wave)

    seen_days: set[date] = set()
    for entry in history:
        if entry.day in seen_days:
            msg = (
                f"{entry.day} appears twice in the history for wave {wave}; a day counted "
                "twice is weighted twice and the rate reads high"
            )
            raise DigestError(msg)
        if entry.day > today:
            msg = (
                f"{entry.day} is after {today}, so it is not a completed day of evidence; a "
                "future day in the history is a bug that makes the rate look settled"
            )
            raise DigestError(msg)
        seen_days.add(entry.day)

    def answer(
        verdict: Forecast,
        because: str,
        *,
        rate: float = NO_RATE,
        projected: date | None = None,
        days_of_history: int = 0,
    ) -> BurnDown:
        return BurnDown(
            wave=wave,
            name=name,
            total=total,
            closed=closed_count,
            remaining=remaining,
            target=target,
            days_to_target=days_to_target,
            days_of_history=days_of_history,
            rate_per_day=rate,
            projected_finish=projected,
            verdict=verdict,
            because=because,
        )

    if total and not remaining:
        return answer(
            Forecast.ON_TRACK,
            f"every one of the {total} leaves in wave {wave} is closed, so there is nothing "
            "left to project",
        )
    if not total:
        return answer(
            Forecast.NOT_FORECASTABLE,
            f"wave {wave} has no leaves in this plan, so there is no burn-down to draw",
        )
    if target is None:
        return answer(
            Forecast.NOT_FORECASTABLE,
            f"no window was supplied for wave {wave}, so there is no target date to burn "
            f"down against; {THE_TARGET_DATE_IS_THE_SCHEDULES_AND_IS_NEVER_COMPUTED_HERE}",
        )
    if today > target:
        return answer(
            Forecast.BEHIND,
            f"the wave {wave} window closed on {target.isoformat()} and {remaining} of "
            f"{total} leaves are still open, which is an observation rather than a forecast",
        )

    if len(history) < minimum_days:
        return answer(
            Forecast.NOT_FORECASTABLE,
            f"{len(history)} days of history is below the {minimum_days} a rate is computed "
            f"over; {A_FORECAST_FROM_TOO_LITTLE_HISTORY_IS_NOT_A_FORECAST}",
            days_of_history=len(history),
        )

    closed_in_history = sum(len(entry.closed & leaves) for entry in history)
    rate = closed_in_history / len(history)
    if rate == NO_RATE:
        return answer(
            Forecast.BEHIND,
            f"nothing in wave {wave} closed across {len(history)} days, so the measured rate "
            f"is nought and {remaining} open leaves do not land on {target.isoformat()} or "
            "on any other date at this rate",
            days_of_history=len(history),
        )

    projected = today + timedelta(days=math.ceil(remaining / rate))
    return answer(
        Forecast.ON_TRACK if projected <= target else Forecast.BEHIND,
        f"{remaining} of {total} leaves are open and wave {wave} has been closing "
        f"{rate:.2f} a day over {len(history)} days, which reaches nought on "
        f"{projected.isoformat()} against a target of {target.isoformat()}",
        rate=rate,
        projected=projected,
        days_of_history=len(history),
    )


# ------------------------------------------------------------------ the digest
@dataclass(frozen=True)
class DailyDigest:
    """One evening's message, as content. Nothing here knows where it goes.

    Read the absences, they are the design. There is no author, no committer, no assignee, no
    owner and no commit subject, and there is no field any of those could be added to without
    appearing in a diff. See `THE_DIGEST_ATTRIBUTES_WORK_TO_THE_PLAN_AND_NEVER_TO_A_PERSON`.
    """

    day: date
    wave: int
    movement: Movement = field(default_factory=Movement)
    overdue: tuple[OverdueLeaf, ...] = ()
    burn_down: BurnDown | None = None


def daily_digest(
    *,
    plan: Plan,
    windows: Mapping[int, WaveWindow],
    history: Sequence[DayClosed],
    wave: int,
    today: date,
    previously_closed: Collection[str] | None,
    minimum_days: int = MINIMUM_DAYS_OF_HISTORY,
) -> DailyDigest:
    """The evening digest for one day (M38.3.3.2, M38.3.3.3).

    `wave` is passed in rather than worked out here. `brain.status.build_status` already
    decides which wave is current, as the first wave whose closed count is below its total,
    and deciding it a second way would let the digest report on one wave while the status page
    shows another.

    A quiet day is a digest like any other, with the movement empty and the burn-down and the
    overdue list still computed. See `A_QUIET_DAY_IS_A_RESULT_AND_NOT_AN_ABSENT_MESSAGE`.
    """
    return DailyDigest(
        day=today,
        wave=wave,
        movement=movement_since(previously_closed, plan),
        overdue=overdue_leaves(plan, windows, today=today),
        burn_down=burn_down(
            plan,
            windows.get(wave),
            history,
            wave=wave,
            today=today,
            minimum_days=minimum_days,
        ),
    )


#: What a quiet day says, written out rather than assembled from an empty list.
NOTHING_CLOSED = "Nothing closed today."

#: What the first run says. Distinct from a quiet day, because "we cannot tell" and "nothing
#: happened" are different facts and the first one is fixed by running again tomorrow.
NOTHING_TO_COMPARE = "No snapshot from yesterday, so today's movement cannot be stated."


def render(digest: DailyDigest) -> str:
    """The digest as a person reads it, and the only string the delivery leaf will need.

    Movement first, then what is late, then the burn-down. That order is deliberate: what
    moved is why somebody opens the message, what is late is what they act on, and the
    forecast is the part that is read last and quoted most, so it sits under its own reason.

    Every line is built from task ids, wave numbers, wave names, dates and counts. Nothing is
    interpolated from a commit.
    """
    burn = digest.burn_down
    lines: list[str] = [
        f"Build digest for {digest.day.isoformat()}",
        f"Wave {digest.wave}: {burn.name if burn is not None else ''}".rstrip(": "),
        "",
    ]

    if not digest.movement.measured:
        lines += [NOTHING_TO_COMPARE, ""]
    else:
        if digest.movement.closed:
            lines.append(f"Closed today ({len(digest.movement.closed)}):")
            lines += [f"- {leaf}" for leaf in digest.movement.closed]
        else:
            lines.append(NOTHING_CLOSED)
        if digest.movement.reopened:
            lines.append(f"Reopened today ({len(digest.movement.reopened)}):")
            lines += [f"- {leaf}" for leaf in digest.movement.reopened]
        else:
            lines.append("Nothing reopened today.")
        lines.append("")

    if digest.overdue:
        lines.append(f"Overdue ({len(digest.overdue)}), oldest first:")
        lines += [
            f"- {item.leaf} (wave {item.wave}) was due {item.due.isoformat()}, "
            f"{item.days_late} days ago"
            for item in digest.overdue
        ]
    else:
        lines.append("Nothing is past its wave's window.")
    lines.append("")

    if burn is not None:
        target = burn.target.isoformat() if burn.target is not None else "no target date"
        lines.append(
            f"Burn-down: {burn.closed} of {burn.total} closed, {burn.remaining} open, "
            f"target {target}."
        )
        if burn.verdict.forecast_made and burn.projected_finish is not None:
            lines.append(
                f"Verdict: {burn.verdict.value}, reaching nought on "
                f"{burn.projected_finish.isoformat()}."
            )
        else:
            lines.append(f"Verdict: {burn.verdict.value}.")
        lines.append(f"Because: {burn.because}")

    return "\n".join(lines)
