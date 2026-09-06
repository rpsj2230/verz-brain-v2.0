"""Correction, held to the two things it refuses to do: delete, and wait.

Decay handles a memory going quietly out of date and lives in `formation.py`. This handles the
two cases where something positively contradicts one, and both end in a mark. Every test here
is about the mark surviving and the correction being immediate, because the two ways this
module could fail are deleting the evidence and postponing the fix.

Task ids: M16.4.2, M16.4.3
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.memory import correction as correction_module
from brain.memory.correction import (
    DEMOTED_CONFIDENCE,
    Correction,
    Demotion,
    Supersession,
    confidence_after,
    corrected,
    correction_gaps,
    demoted_ids,
    demotion_is_immediate,
    newest_first,
    superseded_ids,
)
from brain.memory.formation import RECALL_FLOOR
from brain.memory.signals import Signal

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def superseded(old: str, new: str, *, at: datetime = NOW) -> Supersession:
    return Supersession(superseded_id=old, by_id=new, prompted_by=Signal.CONTRADICTED, at=at)


def demoted(memory: str, *, field: str = "client.renewal_date") -> Demotion:
    return Demotion(memory_id=memory, field=field, at=NOW)


def test_a_memory_the_source_contradicts_is_worth_nothing_immediately() -> None:
    """**M16.4.3, and "immediately" is the load-bearing word.**

    A demotion that waited for an agreement count, a promotion job or a decay curve would
    leave the system answering from something it already knows the records contradict, for as
    long as the wait lasted. The source is authoritative and memory is a hint, so there is
    nothing to weigh.

    Zero rather than just under the floor, because just under the floor is a memory the system
    still half believes and would recall the moment somebody lowered the threshold.

    Delete this and a demotion becomes a confidence adjustment, which is a delay with a number
    attached."""
    assert confidence_after("m1", [demoted("m1")], formed=1.0) == DEMOTED_CONFIDENCE
    assert DEMOTED_CONFIDENCE == 0.0
    assert DEMOTED_CONFIDENCE < RECALL_FLOOR


def test_a_memory_the_source_says_nothing_about_keeps_what_it_was_formed_with() -> None:
    """The positive sibling, and without it every assertion here is satisfied by a function
    that demotes everything.

    A correction path that zeroed every memory would be a system that forgets everything the
    moment anything is corrected, which is the same product as no memory reached expensively.

    Delete this and `confidence_after` can return zero unconditionally with the file green."""
    assert confidence_after("m2", [demoted("m1")], formed=0.8) == 0.8
    assert confidence_after("m1", [], formed=0.8) == 0.8


def test_nothing_in_this_module_can_postpone_a_correction() -> None:
    """The enforceable half of "immediately". A threshold parameter is a correction somebody
    can delay, and the delay would read as tuning rather than as the system answering from
    something it knows to be contradicted.

    Asserted on the signatures rather than on behaviour, because behaviour today says nothing
    about whether a `grace=` can be added tomorrow, and it would arrive with a small default
    that looks harmless.

    Delete this and `confidence_after(..., grace=timedelta(days=1))` appears, and it will read
    as being careful about flapping."""
    for function in (confidence_after, corrected, superseded_ids, demoted_ids):
        taken = set(inspect.signature(function).parameters)
        for forbidden in ("threshold", "after", "delay", "agreement", "window", "grace"):
            assert forbidden not in taken, f"{function.__name__} can postpone a correction"

    assert demotion_is_immediate() is True
    assert correction_gaps() == ()


def test_a_correction_carries_no_value_from_either_side() -> None:
    """A correction log holding the old value and the new one is a transcript of what people
    said and a copy of what the records hold, in one table, under permissions belonging to
    neither.

    A supersession names two memories and the signal that prompted it; a demotion names one
    memory and the field the records disagreed about. Naming the field is what makes the flag
    actionable and is as far as it goes.

    Delete this and `old_value` appears, because it is genuinely the most useful thing anybody
    could add to a correction record."""
    for model in (Supersession, Demotion):
        names = {f.name for f in dataclass_fields(model)}
        for forbidden in ("value", "old_value", "new_value", "statement", "text", "was"):
            assert forbidden not in names, f"{model.__name__} carries {forbidden}"


def test_nothing_in_this_module_deletes_anything() -> None:
    """Deleting is the one operation that makes the previous behaviour unexplainable: after
    it, a person asking why the system stopped saying something has nothing to be answered
    from.

    Checked over the module's own names, because the property is about the whole module, and
    because the first thing anybody writes when a correction table gets large is the thing
    that clears it.

    Delete this and `forget` arrives, and it will be described as housekeeping."""
    public = {name for name in dir(correction_module) if not name.startswith("_")}

    for forbidden in ("delete", "remove", "forget", "purge", "clear", "drop"):
        assert forbidden not in public, f"the module exposes {forbidden}"

    assert {one.value for one in Correction} == {"superseded", "demoted"}


def test_a_superseded_memory_and_a_demoted_one_are_one_question_at_the_recall_path() -> None:
    """Two ways of being corrected and one question to ask, because asking it twice is how one
    of the two gets forgotten at a call site.

    Delete this and the recall path checks supersessions, ships, and the demotions are
    remembered a fortnight later."""
    both = corrected([superseded("m1", "m2")], [demoted("m3")])

    assert both == frozenset({"m1", "m3"})
    assert corrected() == frozenset()


def test_a_memory_cannot_supersede_itself() -> None:
    """A loop the recall path would follow, and a correction nobody can undo: the memory that
    would restore it is the one that replaced it.

    Refused at construction, so the loop never exists rather than being detected somewhere
    later by something that has to know to look.

    Delete this and a correction written from a variable that was not reassigned produces a
    memory that supersedes itself, which reads in a listing as a memory that was corrected and
    behaves as one that vanished."""
    with pytest.raises(ValueError, match="cannot supersede itself"):
        superseded("m1", "m1")


def test_a_correction_naming_nothing_cannot_be_constructed() -> None:
    """A correction with a blank id or a blank field is a row nobody can act on, which is
    indistinguishable in a review queue from one nobody has read.

    Delete this and the queue fills with entries that cannot be followed anywhere."""
    with pytest.raises(ValueError, match="names nothing"):
        Supersession(superseded_id="", by_id="m2", prompted_by=Signal.CONTRADICTED, at=NOW)

    with pytest.raises(ValueError, match="names nothing"):
        Demotion(memory_id="m1", field="", at=NOW)


def test_a_demotion_cannot_claim_the_source_only_partly_won() -> None:
    """The confidence on a demotion is not a value a caller supplies. A demotion at 0.3 is a
    memory the records contradict that the system still half believes, which is the shape a
    threshold would take once somebody wanted one.

    Delete this and the field becomes settable, and the first caller to set it will be doing
    it to stop something flapping."""
    with pytest.raises(ValueError, match="does not partly win"):
        Demotion(memory_id="m1", field="client.renewal_date", at=NOW, confidence=0.3)


def test_a_naive_correction_time_cannot_be_constructed() -> None:
    """Every ordering in this module compares two times, and a naive one compares wrongly
    against an aware one.

    Delete this and a review queue orders corrections by hours that do not mean the same
    thing, which presents as a queue that shuffles rather than as a timezone bug."""
    for build in (
        lambda: Supersession(
            superseded_id="m1",
            by_id="m2",
            prompted_by=Signal.CONTRADICTED,
            at=datetime(2026, 9, 7, 12, 0),
        ),
        lambda: Demotion(memory_id="m1", field="f", at=datetime(2026, 9, 7, 12, 0)),
    ):
        with pytest.raises(ValueError, match="naive"):
            build()


def test_corrections_come_back_newest_first_in_a_fixed_order() -> None:
    """A review surface whose order moves between two readings is one nobody can work
    through: somebody halfway down the list loses their place every time the page reloads.

    Ties break on the superseded id, so two corrections written in one transaction come back
    in a fixed order rather than whichever the store returned.

    Delete this and the order becomes the store's, which is not an order."""
    older = superseded("m1", "m2", at=NOW - timedelta(days=1))
    newer = superseded("m3", "m4", at=NOW)
    tied_a = superseded("a1", "a2", at=NOW)
    tied_b = superseded("b1", "b2", at=NOW)

    assert newest_first([older, newer]) == (newer, older)
    assert newest_first([tied_b, tied_a])[0].superseded_id == "a1"


def test_supersession_is_one_step_rather_than_a_chain_to_walk() -> None:
    """A replaced by B, and B later replaced by C, puts both A and B in the set. Nothing needs
    to know that A was replaced by something itself replaced, because both are equally not
    recalled, and following the chain is the loop the constructor refuses to let anybody
    start.

    Delete this and somebody writes the walk, and the first cycle in the data hangs the recall
    path."""
    chain = [superseded("A", "B"), superseded("B", "C")]

    assert superseded_ids(chain) == frozenset({"A", "B"})


def test_nothing_yet_composes_a_correction_with_the_recall_path() -> None:
    """**The honest gap, asserted so it cannot be forgotten quietly.**

    `formation.may_recall` decides reach and decay and knows nothing about corrections, and
    this module decides corrections and knows nothing about reach. Composing them is a
    caller's job and no caller exists, so a superseded memory would still be recalled by
    anything wiring only the first half.

    This is the twelfth instance in this repository of something correct, tested, documented
    and never invoked, and the difference is that this one says so in a test rather than in a
    docstring nobody runs.

    Delete this and the gap stops being visible, and the day somebody wires recall they will
    wire the half they can see."""
    from brain.memory import formation

    recall_parameters = set(inspect.signature(formation.may_recall).parameters)
    for absent in ("corrected", "supersessions", "demotions", "superseded"):
        assert absent not in recall_parameters, (
            f"may_recall now takes {absent}, so the two halves have been composed and this "
            "test should be replaced by one asserting a superseded memory is not recalled"
        )


def test_correction_gaps_reports_a_demotion_that_would_still_be_recalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Written because a mutation survived, and it is the second time this week the same
    shape has.** `correction_gaps` has three checks and every test called it on a healthy
    module and asserted an empty tuple, which passes whether the first check is there or not.

    A diagnostic nobody has watched report anything is a diagnostic nobody can rely on. The
    check that matters here is the one comparing the demoted confidence against the retrieval
    floor: if somebody raised the first or lowered the second until they crossed, the source
    contradicting a memory would stop preventing its recall, and every other test in this file
    would still pass because each asserts one of the two numbers alone.

    The constant is patched rather than edited, so the module is left as it is and the test
    says what a broken pairing looks like rather than requiring one.

    Delete this and the two numbers can drift past each other, and the only thing that would
    have noticed goes on returning an empty tuple."""
    monkeypatch.setattr("brain.memory.correction.DEMOTED_CONFIDENCE", RECALL_FLOOR + 0.1)

    gaps = correction_gaps()

    assert any("retrieval floor" in one for one in gaps), gaps
    assert any("does not stop it being" in one for one in gaps), gaps
