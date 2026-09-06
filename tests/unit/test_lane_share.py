"""The fast-lane share, and the three ways a figure about a system becomes one about a person.

The number this module produces is the fast lane's whole justification, which is why it gets
measured at all. It is also the kind of number that quietly acquires a breakdown, and a
breakdown of this one by person says what somebody asks about and how routine their work is.

So the tests come in three groups, one per way that happens. **A dimension nobody should
group on** is tested structurally, because a rule about grouping holds until somebody has a
reason. **A denominator small enough to be one person's morning** is tested at the boundary
from both sides, with the two counts written out rather than derived from the constant, so
the pair pins the floor rather than moving with it. **Machine traffic counted into one half
of the fraction** is tested with a machine population whose lane mix differs from the human
one, because a fixture where both populations look the same is green whether the filter runs
once, twice or never.

Task ids: M6.1.6
"""

from __future__ import annotations

import dataclasses

from brain.core.lane import Lane
from brain.gate.context import TrafficClass
from brain.ops.lane_share import (
    MINIMUM_QUESTIONS_BEFORE_A_SHARE,
    NOT_ENOUGH_TRAFFIC,
    LaneObservation,
    Share,
    is_machine,
    measure,
    share_lines,
)

#: Thirty of fifty answered instantly, which is sixty per cent. Written as two runs rather
#: than as a ratio so the arithmetic under test is not also the arithmetic in the fixture.
HUMAN_FAST = [LaneObservation(Lane.FAST, TrafficClass.HUMAN_INTERACTIVE)] * 30
HUMAN_SLOW = [LaneObservation(Lane.ANSWER, TrafficClass.HUMAN_INTERACTIVE)] * 20

#: A machine population that is entirely fast lane, which is roughly what automations look
#: like: they ask the same few questions on a schedule. Deliberately a different mix from the
#: human traffic, so counting it into either half of the fraction moves the figure. A machine
#: run with the same mix as the human one would be a fixture that reads well and proves
#: nothing.
MACHINE_FAST = [LaneObservation(Lane.FAST, TrafficClass.AUTOMATION)] * 50


def test_machine_traffic_leaves_both_halves_of_the_fraction() -> None:
    """**The property the module is arranged around.** Automations hit the fast lane far more
    often than people do, so counting them into the numerator inflates the figure and into
    the denominator alone deflates it. Either way the number stops meaning "how often
    somebody waiting got an instant answer".

    The three wrong answers are written out because they are all different: 80 per cent for
    both halves, 30 for the denominator alone, and something over 100 for the numerator
    alone. A test asserting only that the figure is 60 would pass if the filter ran twice.

    Delete this and the figure drifts upward as automations are added, and reads as the fast
    lane getting better."""
    people = HUMAN_FAST + HUMAN_SLOW

    assert measure(people).percent == 60
    assert measure(people + MACHINE_FAST).percent == 60
    # And the wrong answers the mutations would produce, stated so the fixture is known to
    # discriminate between them rather than assumed to.
    assert round(100 * (30 + 50) / (50 + 50)) == 80
    assert round(100 * 30 / (50 + 50)) == 30


def test_a_run_of_nothing_but_machines_produces_no_figure_at_all() -> None:
    """The sibling of the test above, and the case that catches a filter applied to the
    numerator only: with the humans removed there is no denominator left, and a module that
    filtered late would divide by the machines.

    Delete this and a nightly automation run reports the fast lane answering everything."""
    assert measure(MACHINE_FAST) == Share()
    assert measure([]) == Share()


def test_a_share_is_not_reported_until_enough_people_have_asked_something() -> None:
    """Five questions and a share of eighty per cent says one person asked five things and
    four were routine. The floor is what stops the figure being a description of somebody's
    morning.

    The two counts are written out rather than built from `MINIMUM_QUESTIONS_BEFORE_A_SHARE`,
    which pins it from both sides: raise the constant and the fifty-observation run stops
    reporting, lower it and the forty-nine-observation one starts. A test that built its
    fixtures from the constant would be green for every value the constant could hold.

    Delete this and a quiet Sunday produces a published figure about whoever was working."""
    interactive = LaneObservation(Lane.FAST, TrafficClass.HUMAN_INTERACTIVE)

    assert measure([interactive] * 49).percent is None
    assert measure([interactive] * 50).percent == 100
    assert MINIMUM_QUESTIONS_BEFORE_A_SHARE == 50


def test_the_floor_counts_people_rather_than_traffic() -> None:
    """The floor is there because a small number of questions is a small number of askers, so
    it has to be applied after machine traffic is removed and not before. Forty-nine people's
    questions beside a thousand scheduled ones is still forty-nine people's questions.

    Delete this and a busy automation lifts the denominator over the floor, and the figure
    that gets published is a figure about the handful of humans underneath it."""
    interactive = LaneObservation(Lane.FAST, TrafficClass.HUMAN_INTERACTIVE)

    assert measure([interactive] * 49 + MACHINE_FAST * 20).percent is None


def test_nothing_in_an_observation_names_anybody() -> None:
    """**The structural half.** "The fast lane answered 62% of questions" is a fact about the
    system and "62% of Priya's questions" is a report about Priya. A rule saying nobody may
    group by a person holds until the first person with a reason, and a report is exactly
    where somebody has one.

    Asserted on the fields rather than on the docstring, because the docstring is where the
    argument lives and a text search would be satisfied by it.

    Delete this and the first useful breakdown adds one field."""
    names = {f.name for f in dataclasses.fields(LaneObservation)}

    assert names == {"lane", "traffic_class"}
    annotations = " ".join(f"{f.name}:{f.type}" for f in dataclasses.fields(LaneObservation))
    for forbidden in ("principal", "person", "user", "channel", "question", "department"):
        assert forbidden not in annotations.lower()


def test_a_share_carries_a_percentage_and_no_counts() -> None:
    """A count is subtractable and a percentage is not. Two counts a month apart, or one
    beside a filtered figure, reconstruct what was not shown, and each looked publishable on
    its own. The console refuses to render a total for the same reason.

    Delete this and the obvious next request, "how many questions was that out of", is one
    field away."""
    names = {f.name for f in dataclasses.fields(Share)}

    assert names == {"percent"}
    assert isinstance(measure(HUMAN_FAST + HUMAN_SLOW).percent, int)


def test_the_line_for_a_thin_window_says_so_without_saying_how_thin() -> None:
    """Reporting "not enough traffic: seven questions" hands back exactly the number the
    floor exists to withhold. The refusal carries no figure at all, which is why the check is
    that the sentence holds no digits rather than that it holds the right ones.

    Delete this and the withheld count comes back in the sentence explaining why it is
    withheld."""
    lines = share_lines(Share())

    assert lines == (NOT_ENOUGH_TRAFFIC,)
    assert not any(character.isdigit() for character in "".join(lines))


def test_the_line_for_a_real_window_states_the_share() -> None:
    """The sibling. A module that refused to report anything would satisfy every test above
    and would be a measurement nobody can read, which is the same as no measurement.

    Delete this and the reporting half is untested and can be emptied without failing."""
    lines = share_lines(measure(HUMAN_FAST + HUMAN_SLOW))

    assert lines == ("The fast lane answered 60% of the questions people asked.",)


def test_every_kind_of_traffic_is_decided_and_none_of_it_by_default() -> None:
    """`is_machine` matches on every member of `TrafficClass` and ends in `assert_never`, so
    a new member is a type error until somebody decides which side it falls on. The decision
    it forces is the one that matters: a mapping with a default would count a new kind of
    robot as a person and the figure would drift with nothing saying it had.

    The expectation is written here as a set rather than imported, so this compares the
    module's `match` statement against an independent statement of the same rule rather than
    against itself.

    Delete this and a traffic class added next year is silently counted as a person."""
    machines = {TrafficClass.AUTOMATION, TrafficClass.SYSTEM}

    for traffic in TrafficClass:
        assert is_machine(traffic) is (traffic in machines), traffic
