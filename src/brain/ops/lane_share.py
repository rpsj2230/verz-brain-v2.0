"""How much of the traffic the fast lane answered, as a figure that is about the system.

The fast lane's whole justification is a number: it either answers a worthwhile share of what
people ask, in which case it earns the rule table and the separate database role, or it does
not, in which case it is machinery nobody is using. So the share has to be measured. This is
the module that measures it, and almost everything in it is about the ways that figure turns
into a report about a person.

**"The fast lane answered 62% of questions" is a fact about the system. "The fast lane
answered 62% of Priya's questions" is a report about Priya**, and it is a good one: it says
roughly what she asks about, how routine her work is, and by comparison with last month
whether that changed. `LaneObservation` therefore has no field for a principal, a channel, a
department or a question. Not a rule saying nobody may group by one; nowhere to put one. That
is the same construction `brain.ops.automation_piece` makes about addresses and
`brain.gate.fast_lane` makes about tools, and it is here for the same reason: a rule holds
until the first person who has a reason, and a report is exactly the place somebody has one.
See `THIS_MODULE_HAS_NOWHERE_TO_PUT_A_PERSON`.

**A share is a ratio and never a tally.** `Share` carries a percentage and no counts, which
is `A_PAGE_NEVER_CARRIES_A_COUNT` in `console/src/components/paging.ts` applied one level
back. A count is subtractable and a percentage is not: two counts a month apart, or beside a
filtered figure, reconstruct the thing that was not shown, and nobody notices because each
figure on its own was fine to publish. Rounding to a whole percent is part of it rather than
presentation. An exact ratio over a small population names the population.

**A share over a handful of questions is a report about whoever asked them.** Five questions
and a share of 80% says one person asked five things and four were routine. So there is a
floor on the denominator, and below it the answer is that there is not enough traffic, said
without saying how much there was. Reporting "not enough traffic: 7 questions" would hand
back the number the floor exists to withhold.

**Machine traffic leaves both halves of the fraction or neither.** This is the arithmetic
half of M6.1.6 and it is the easy thing to get half right. Automations ask the same narrow
questions repeatedly, so they hit the fast lane far more often than people do; counting them
in the numerator alone inflates the figure, and in the denominator alone deflates it. Either
way the number stops meaning "how often somebody waiting got an instant answer", which is the
only question it is being asked. `measure` filters once, before either half is counted, and
there is no second place a caller can filter differently.

**Which traffic is machine traffic is decided exhaustively.** `is_machine` matches on every
member of `TrafficClass` with `assert_never` at the end, the same construction
`brain.gate.context.traffic_class_for` uses. A mapping with a default would classify a
traffic class nobody has thought about, and the default nobody notices is the one that counts
a robot as a person.

Rejected: a per-channel share. It reads as an operational breakdown and it is not: on a small
estate a channel is a person, and "the widget answered 90% instantly" is a statement about the
one member of staff who uses the widget. The share that is safe is the one over everything.

Rejected: reporting the share alongside the number of questions, so a reader can tell a
confident figure from a thin one. The floor already answers that: above it the figure is worth
reading and below it there is no figure. Publishing the denominator to say how much to trust
the numerator is how the count gets out.

**Nothing here reads a clock, opens a connection or stores anything**, which is the split
`brain.ops.limits` and `brain.ops.evaluation` make. Observations arrive as a sequence somebody
else kept, so a window boundary and an empty run are both testable, and those are the two
cases a measurement of this kind is ever wrong at.

**Nothing produces a `LaneObservation` yet.** The gate is not assembled end to end anywhere in
this repository, so there is no request path to record one from: `brain.gate.classify` decides
a lane and has no caller in `src`, and `brain.gate.fast_lane` is in the same position. What is
here is the shape the figure has to have before anything starts emitting one, which is the
half worth writing first, because a measurement is easy to start recording and very hard to
stop once the wrong thing is in the table.

Task ids: M6.1.6
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, assert_never

from brain.core.lane import Lane
from brain.gate.context import TrafficClass

# ------------------------------------------------------------------ written-down reasons

#: Why an observation carries a lane and a traffic class and nothing that names anybody.
THIS_MODULE_HAS_NOWHERE_TO_PUT_A_PERSON = (
    "A fast-lane share broken down by person is a report about that person: what they ask "
    "about, how routine their work is, and whether that changed. The same is true of a "
    "channel on a small estate, where a channel is one member of staff. So LaneObservation "
    "has two fields and neither identifies anybody, and grouping by a person is not "
    "forbidden here, it is impossible: there is no field to group on."
)

#: Why the figure is a percentage with no counts beside it.
A_SHARE_IS_A_RATIO_AND_NEVER_A_TALLY = (
    "A count is subtractable and a percentage is not. Two counts a month apart, or a count "
    "beside a filtered one, reconstruct what was not shown, and each figure looked "
    "publishable on its own. The console refuses to render a total for the same reason and "
    "says so in A_PAGE_NEVER_CARRIES_A_COUNT. Rounding to a whole percent is part of the "
    "same rule rather than presentation: an exact ratio over a small population names it."
)

#: Why there is a floor under the denominator, and why the floor never reports itself.
A_SHARE_OVER_A_HANDFUL_OF_QUESTIONS_IS_A_REPORT_ABOUT_A_PERSON = (
    "Five questions and a share of eighty per cent says one person asked five things and "
    "four were routine. Below the floor there is no figure, and the sentence that says so "
    "does not say how far below: reporting 'not enough traffic: seven questions' hands back "
    "exactly the number the floor exists to withhold."
)

#: Why machine traffic is removed once, before either half of the fraction is counted.
MACHINE_TRAFFIC_LEAVES_BOTH_HALVES_OF_THE_FRACTION = (
    "Automations ask the same few questions on a schedule, so they reach the fast lane far "
    "more often than people do. Dropping them from the numerator alone deflates the figure "
    "and from the denominator alone inflates it, and in both cases it stops meaning 'how "
    "often somebody waiting got an instant answer', which is the only thing it is being "
    "asked. One filter, before anything is counted, so there is no second place to get it "
    "half right."
)

# ---------------------------------------------------------------------- the bounds

#: How many questions people have to have asked before a share is worth stating. Fifty
#: rather than ten, and the figure follows from what it is protecting rather than from
#: statistics: below about this, a run of traffic on a small estate is one person's morning,
#: and the share is a description of their morning. It is a floor and not a target; a caller
#: measuring a longer window is doing the right thing.
MINIMUM_QUESTIONS_BEFORE_A_SHARE: Final = 50

#: What a reader is told when the floor was not reached. One sentence, with no number in it.
NOT_ENOUGH_TRAFFIC = "Not enough questions in this window to report a fast-lane share."


@dataclass(frozen=True)
class LaneObservation:
    """One answered question: which lane took it, and whether a person was waiting.

    Two fields, and the absence of a third is the design. See
    `THIS_MODULE_HAS_NOWHERE_TO_PUT_A_PERSON`. There is deliberately no timestamp either:
    the window is decided by whoever selects the observations, and a clock on the record
    would invite this module to start deciding which ones are in it.
    """

    lane: Lane
    traffic_class: TrafficClass


def is_machine(traffic: TrafficClass) -> bool:
    """Whether nobody was waiting for this answer.

    `assert_never` is the point of the shape. A new member of `TrafficClass` is a type error
    here until somebody decides which side it falls on, and the decision it forces is the one
    that matters: a mapping with a default would quietly count a new kind of robot as a
    person, and the figure would drift with nothing reporting that it had.
    """
    match traffic:
        case TrafficClass.AUTOMATION | TrafficClass.SYSTEM:
            return True
        case TrafficClass.HUMAN_INTERACTIVE | TrafficClass.HUMAN_ASYNC:
            return False
        case _:
            assert_never(traffic)


@dataclass(frozen=True)
class Share:
    """The figure, or the fact that there is not one.

    One field rather than a percentage and a flag beside it. A pair can be inconsistent, and
    the inconsistent pair that matters is `measured=True` with nothing to report, which a
    caller renders as "0%" and a reader reads as "the fast lane answered nothing". None is
    the whole of "there is no figure" and it cannot be paired with anything.

    It carries no counts. See `A_SHARE_IS_A_RATIO_AND_NEVER_A_TALLY`.
    """

    percent: int | None = None

    @property
    def measured(self) -> bool:
        return self.percent is not None


def measure(observations: Iterable[LaneObservation]) -> Share:
    """The fast lane's share of what people asked (M6.1.6).

    Machine traffic is removed first and once, so both halves of the fraction are drawn from
    the same population. See `MACHINE_TRAFFIC_LEAVES_BOTH_HALVES_OF_THE_FRACTION`.

    Rounded to a whole percent, and returning no figure at all below the floor. Neither is
    presentation: an exact ratio over a small population names the population, and a
    denominator under the floor is one person's morning.
    """
    human = [one for one in observations if not is_machine(one.traffic_class)]
    if len(human) < MINIMUM_QUESTIONS_BEFORE_A_SHARE:
        return Share()
    fast = sum(1 for one in human if one.lane is Lane.FAST)
    return Share(percent=round(100 * fast / len(human)))


def share_lines(share: Share) -> tuple[str, ...]:
    """What an operator is shown. A percentage or a refusal, and never a number of questions.

    Returned as lines rather than printed, for the reason `brain.ops.canaries.alert_lines`
    gives about its own: whoever calls this decides where it goes, and a module that printed
    would be one that had decided.
    """
    if share.percent is None:
        return (NOT_ENOUGH_TRAFFIC,)
    return (f"The fast lane answered {share.percent}% of the questions people asked.",)
