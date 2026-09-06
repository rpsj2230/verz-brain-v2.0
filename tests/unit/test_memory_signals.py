"""Signal capture, held to what it refuses to keep.

A learning system's evidence is a log of the moments it got something wrong, and two things
follow that this file is mostly about. That log must not become a second copy of what people
asked, under different permissions from the conversation. And it must not become a count of
how often the system failed each person, which is the same rows read one join differently.

The detectors are deliberately crude and every test here is written against that: what is
asserted is that they are wrong in the direction that costs nothing and right on the cases
they were written for, not that they are clever.

Task ids: M16.2.1, M16.2.2, M16.2.3, M16.2.4, M16.2.5, M16.2.6, M16.2.7, M16.2.8
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.gate.injection import AutonomyTier
from brain.memory.signals import (
    POSITIVE_SIGNALS,
    REASK_SHARED_TERMS,
    REASK_WINDOW,
    REOPEN_WINDOW,
    Observation,
    Signal,
    counts_by,
    is_contradiction,
    is_reask,
    is_reopen,
    is_takeover,
    signal_gaps,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def observed(signal: Signal, *, principal: str = "p_ada", message: str = "m1") -> Observation:
    return Observation(
        signal=signal,
        conversation_id="c1",
        message_id=message,
        principal_id=principal,
        at=NOW,
    )


def test_an_observation_has_nowhere_to_put_what_was_said() -> None:
    """**The property the whole module is arranged around.**

    The obvious shape carries the question text, because learning wants to read it later, and
    that shape makes this a second transcript of everything anybody asked, sitting under this
    table's permissions rather than the conversation's. `chat.conversation` restricts a
    conversation to the person who had it; a signal row holding the same words would be a way
    to read what somebody asked without holding what it takes to read their conversation.

    Asserted on the model's fields rather than on behaviour, because a field added today is
    unused and load-bearing tomorrow, and by then the docstring arguing against it reads as
    history.

    Delete this and `question: str` appears on the model, and every other test here still
    passes because none of them construct one with it set."""
    names = {f.name for f in dataclass_fields(Observation)}

    assert names == {"signal", "conversation_id", "message_id", "principal_id", "at"}
    for forbidden in ("question", "answer", "text", "body", "reason", "excerpt", "content"):
        assert forbidden not in names, f"an observation can carry a {forbidden}"


def test_signals_may_not_be_counted_per_person() -> None:
    """Every signal is a small statement that an answer was wrong. Grouped by principal that
    is a ranking of who the system fails, which reads as a ranking of who asks badly, and it
    is one join from existing at any moment.

    The principal stays on the row, because a learning system that could not tell one person's
    correction from another's would learn the average of everybody. What is refused is the
    aggregation.

    Delete this and the most obvious query anybody would write against this table is the one
    that turns it into a performance review."""
    rows = [observed(Signal.REASKED), observed(Signal.REASKED, principal="p_ben")]

    with pytest.raises(ValueError, match="ranking of who the system fails"):
        counts_by(rows, "principal_id")


def test_signals_may_be_counted_by_the_things_that_are_about_answers() -> None:
    """The positive sibling. Without it, a `counts_by` that refused everything passes the test
    above and the module has no way to report anything at all.

    Counting by signal kind is what tells somebody which failure is common; counting by
    message is what finds the answer that went wrong repeatedly. Neither is about a person.

    Delete this and the refusal can be widened to everything, which reads as caution."""
    rows = [
        observed(Signal.REASKED),
        observed(Signal.REASKED, message="m2"),
        observed(Signal.ESCALATED),
    ]

    assert counts_by(rows, "signal") == {"reasked": 2, "escalated": 1}
    assert counts_by(rows, "message_id") == {"m1": 2, "m2": 1}


def test_a_field_that_is_not_a_field_is_refused_rather_than_counted_as_nothing() -> None:
    """A typo that silently counted nothing would be a report saying there is no evidence,
    which is the most misleading thing this module could produce: it looks like the system is
    doing well.

    Delete this and `counts_by(rows, "principal")` returns an empty map, which is both wrong
    and reassuring."""
    with pytest.raises(ValueError, match="not a field"):
        counts_by([observed(Signal.REASKED)], "kind")


def test_the_same_question_in_different_words_inside_the_window_is_a_reask() -> None:
    """M16.2.1, and the case the whole detector exists for: somebody read an answer, decided
    it missed, and asked again differently.

    **What this actually detects is a question about the same subject asked again soon**, which
    is a weaker claim than paraphrase detection and is what the available words support. The
    pair below shares two subject terms out of the shorter question's four, and reads to a
    person as obviously the same question. A symmetric overlap scores it 0.29 and would refuse
    it, which is how the first draft of this module was wrong: a question re-asked in
    different words shares few words by definition, so the measure was pulling against the
    leaf.

    Delete this and the most common evidence of a poor answer stops being recorded."""
    assert is_reask(
        "how many hours are left on the Acme retainer",
        "what is the remaining hours balance for Acme",
        apart=timedelta(minutes=2),
    )
    assert is_reask(
        "when does the Acme retainer renew",
        "what is the renewal date for the Acme retainer",
        apart=timedelta(minutes=2),
    )


def test_the_same_question_twice_is_not_a_reask() -> None:
    """Somebody pressing send twice, or a client retrying, is not evidence the answer was
    poor. Counting it would make every network hiccup look like a failed answer, and the
    signal that is easiest to generate would become the most common one in the table.

    Delete this and the retry path quietly manufactures evidence."""
    question = "how many hours are left on the Acme retainer"

    assert not is_reask(question, question, apart=timedelta(minutes=1))
    assert not is_reask(question, question.upper(), apart=timedelta(minutes=1))


def test_a_different_question_is_not_a_reask_however_soon_it_arrives() -> None:
    """The other direction. A follow-up about something else is a new question, and treating
    it as a complaint about the previous answer would make every conversation look like a
    sequence of failures.

    Delete this and the overlap threshold can go to zero, at which point every second question
    in every conversation is evidence the first answer was wrong."""
    assert not is_reask(
        "how many hours are left on the Acme retainer",
        "who is the project manager for Meridian",
        apart=timedelta(minutes=1),
    )


def test_two_questions_that_merely_mention_the_same_things_are_not_a_reask() -> None:
    """**Written because a mutation survived.** Dropping the containment threshold to zero
    passed every other test here, because the only "different question" case shared no words
    at all and so was refused by the shared-term guard instead.

    The pair below shares exactly two subject terms, Acme and hours, and is plainly two
    different questions: what is left on a retainer, and who logged time against a rebuild.
    Containment is 0.4 against a floor of 0.5, so the ratio is what refuses it and nothing
    else can.

    That is the case the threshold exists for. Questions in one conversation are about the
    same things by definition, so sharing subject terms is the normal state and cannot be
    what makes a re-ask.

    Delete this and the threshold is accountable to nothing, and every second question about
    a client becomes evidence that the first answer failed."""
    assert not is_reask(
        "how many hours are left on the Acme retainer",
        "which staff logged hours against the Acme website rebuild last quarter",
        apart=timedelta(minutes=1),
    )


def test_a_reask_outside_the_window_is_not_a_reask() -> None:
    """The window is what separates rephrasing from coming back later with a related question.

    Both sides are asserted, because a window applied only at the top lets a negative gap
    through, and a negative gap is a clock problem being recorded as a signal.

    Delete this and a question asked the next morning is evidence about last night's
    answer."""
    earlier = "how many hours are left on the Acme retainer"
    later = "what is the remaining hours balance for Acme"

    assert is_reask(earlier, later, apart=REASK_WINDOW)
    assert not is_reask(earlier, later, apart=REASK_WINDOW + timedelta(seconds=1))
    assert not is_reask(earlier, later, apart=timedelta(seconds=-1))


def test_a_question_with_no_meaningful_words_is_a_reask_of_nothing() -> None:
    """ "ok thanks" and "ok" overlap completely once the noise words are removed, which is to
    say they overlap on nothing. A set comparison on two empty sets is one, and one is above
    every threshold.

    That is the shape that turns every acknowledgement in every conversation into evidence
    that the answer before it failed.

    Delete this and the most common two messages in any chat become the most common signal in
    the table."""
    assert not is_reask("ok thanks", "ok", apart=timedelta(seconds=30))
    assert not is_reask("please", "the", apart=timedelta(seconds=30))

    # And the guard the ratio cannot give: one shared term contains completely, so a
    # single-word question would otherwise be a re-ask of everything mentioning that word.
    assert REASK_SHARED_TERMS >= 2
    assert not is_reask(
        "Acme?", "how many hours are left on the Acme retainer", apart=timedelta(seconds=30)
    )


def test_a_follow_up_that_says_the_answer_was_wrong_is_a_contradiction() -> None:
    """M16.2.3. The markers are the phrases people use to correct an assistant, which is a
    different list from the phrases that mean disagreement in general.

    Delete this and the signal that most directly says "this answer was wrong" stops being
    captured."""
    for phrase in ("That is not right, the retainer ends in June", "no, it renews annually"):
        assert is_contradiction(phrase), phrase


def test_an_ordinary_follow_up_is_not_a_contradiction() -> None:
    """The positive sibling, and the direction that matters more: a detector that fired on
    every follow-up would make the contradiction signal meaningless, and meaningless is worse
    than absent because somebody would act on it.

    Delete this and the markers can be widened until every conversation contains one."""
    for phrase in ("thanks, and what about Meridian", "can you also check the hours"):
        assert not is_contradiction(phrase), phrase


def test_a_ticket_reopened_inside_the_window_is_evidence_and_outside_it_is_not() -> None:
    """M16.2.6. A ticket closed wrongly is discovered when the person it was closed on comes
    back, which is a working day or two rather than a conversation.

    Bounded on both sides: a reopen before the close is a clock problem, and one beyond the
    window is usually a new problem arriving on an old thread, which would make every
    long-lived ticket a permanent complaint about one answer.

    Delete this and either every reopen for ever counts, or none do."""
    closed = NOW

    assert is_reopen(closed, closed + timedelta(hours=6))
    assert is_reopen(closed, closed + REOPEN_WINDOW)
    assert not is_reopen(closed, closed + REOPEN_WINDOW + timedelta(seconds=1))
    assert not is_reopen(closed, closed - timedelta(seconds=1))


def test_only_a_takeover_at_assisted_is_a_takeover() -> None:
    """M16.2.5, and the two exclusions are the whole of it.

    At SHADOW the agent proposes and a person acts every single time, so a person acting is
    the design rather than a signal: counting it would make a new install, where everything
    is shadow-pinned by default, look like a broken one. At AUTONOMOUS a person stepping in is
    an intervention worth a much louder record than a learning signal, and filing it here
    files an incident in a table nobody reads for incidents.

    Delete this and the catalogue's own rule that every template starts at SHADOW guarantees
    a flood of false evidence on day one."""
    assert is_takeover(AutonomyTier.ASSISTED, by_a_person=True)
    assert not is_takeover(AutonomyTier.SHADOW, by_a_person=True)
    assert not is_takeover(AutonomyTier.AUTONOMOUS, by_a_person=True)
    assert not is_takeover(AutonomyTier.ASSISTED, by_a_person=False)


def test_the_vocabulary_is_exactly_the_leaves_m16_2_asks_for() -> None:
    """A closed vocabulary is only closed if something checks it. A kind nobody declared is
    evidence the tier machinery has no weight for, and it will be weighed as whatever the
    default turns out to be.

    Checked in both directions, because a member added and a member missing are different
    mistakes: the first is evidence nothing knows how to use, and the second is a leaf that
    looks built and captures nothing.

    Delete this and the enum drifts from the work breakdown in either direction, silently."""
    assert signal_gaps() == ()
    assert len(Signal) == 7


def test_the_one_signal_that_says_something_went_right_is_named() -> None:
    """A learning system fed only failures learns only what to avoid.

    `COPIED` is the single positive signal here and it is named rather than left for a reader
    to notice, because somebody counting the members will otherwise assume the positive case
    was forgotten and add one.

    Delete this and either the positive case disappears into the list, or somebody adds a
    second one without noticing there was already exactly one."""
    assert frozenset({Signal.COPIED}) == POSITIVE_SIGNALS
    assert Signal.COPIED in Signal
    assert all(one not in POSITIVE_SIGNALS for one in Signal if one is not Signal.COPIED)


def test_an_observation_pointing_at_nothing_cannot_be_constructed() -> None:
    """A signal whose conversation or message is blank is a row nobody can follow back to what
    happened, which is the whole value of a signal that carries no text.

    Refused at construction rather than filtered later, so the useless row never exists.

    Delete this and the table fills with evidence nobody can act on, which is
    indistinguishable from evidence nobody has looked at."""
    for blank in ("conversation_id", "message_id", "principal_id"):
        kwargs = {
            "signal": Signal.REASKED,
            "conversation_id": "c1",
            "message_id": "m1",
            "principal_id": "p_ada",
            "at": NOW,
        }
        kwargs[blank] = ""
        with pytest.raises(ValueError, match="points at nothing"):
            Observation(**kwargs)  # type: ignore[arg-type]


def test_an_observation_with_a_naive_time_cannot_be_constructed() -> None:
    """Every window in this module is arithmetic on two times, and a naive one compares
    wrongly against an aware one.

    Delete this and a writer in one timezone produces signals whose windows are hours out,
    which presents as detectors that fire too often or not at all and never as a timezone
    bug."""
    with pytest.raises(ValueError, match="naive"):
        Observation(
            signal=Signal.REASKED,
            conversation_id="c1",
            message_id="m1",
            principal_id="p_ada",
            at=datetime(2026, 9, 7, 12, 0),
        )
