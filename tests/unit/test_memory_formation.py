"""Recall, held to the one failure memory exists to avoid.

That failure is a sentence learnt while acting for somebody with broad access, recalled later
while acting for somebody without it. Nothing about the sentence says where it came from, so
the disclosure arrives looking like the system being helpful, which is why every test here is
about the reader rather than about the text.

Real `EntitlementSet`s throughout, never a stand-in. The rule under test is composed from
`intersect`, `scope_for` and `Scope.matches`, and a fake implementing those would be a test of
the fake. Where a case turns on wildcard coverage or on expiry, it is built from the real
`Capability` and the real `not_after`, because those are the two places the composition is
easy to get subtly wrong.

Task ids: M16.1.1, M16.1.4, M16.1.5, M16.4.1, M16.4.4
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.memory import formation as formation_module
from brain.memory.formation import (
    HALF_LIFE_DAYS,
    RECALL_FLOOR,
    Formation,
    MemoryKind,
    Recollection,
    clause_place,
    confidence_now,
    may_recall,
    recallable,
    requirement,
    session_expiry,
    session_key,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
WEB = clause_place(department="web")
FINANCE = clause_place(department="finance")


def reader(
    *capabilities: str, scope: Scope = WEB, not_after: datetime | None = None
) -> EntitlementSet:
    return EntitlementSet(
        principal_id="p_reader",
        grants=tuple(Grant(capability=Capability(value=one), scope=scope) for one in capabilities),
        not_after=not_after,
    )


def formed(*capabilities: str, scope: Scope = WEB, at: datetime = NOW) -> Formation:
    return Formation(
        principal_id="p_writer",
        capabilities=tuple(Capability(value=one) for one in capabilities),
        scope=scope,
        ent_hash="0" * 32,
        formed_at=at,
    )


def test_a_reader_who_still_reaches_what_it_was_formed_from_is_told_the_memory() -> None:
    """The positive case, and without it every refusal below is satisfied by a `may_recall`
    that refuses everybody.

    A memory nobody can ever have is the same product as no memory, reached more expensively,
    and it is the failure a permission-conscious author writes by accident.

    Delete this and recall can be made to return None unconditionally with the rest of this
    file green."""
    recollection = may_recall(formed("read:client.name"), reader("read:client.name"), now=NOW)

    assert recollection is not None
    assert recollection.confidence == 1.0


def test_a_reader_who_no_longer_reaches_it_is_told_nothing() -> None:
    """**The failure this module exists to prevent.**

    Something learnt while acting for somebody with `read:client.contract_value`, recalled
    for somebody without it. The memory's text may say nothing sensitive at all and it does
    not matter: what it was formed from is the disclosure, because the system knowing it is
    evidence of what somebody could see.

    Delete this and the check becomes a check on the text, which is a classifier deciding
    what may be recalled and is exactly the thing this architecture refuses to have."""
    memory = formed("read:client.contract_value")

    assert may_recall(memory, reader("read:client.name"), now=NOW) is None


def test_a_reader_holding_a_wildcard_reaches_a_memory_formed_under_a_specific_grant() -> None:
    """**The reason the intersection runs requirement-first, and the bug the other direction
    produces.**

    `intersect` keeps a grant of the receiver's only where the ceiling covers it, and
    `Capability.covers` expands only a trailing `.*`. Narrowing the reader by the memory's
    specific capability would drop the wildcard grant of somebody who plainly holds it, so a
    person with `read:client.*` would lose every memory formed under `read:client.name`, and
    the symptom is a senior person seeing less than a junior one.

    Delete this and swapping the two arguments looks like a tidy-up and passes everything
    else here, because every other case uses matching capabilities."""
    memory = formed("read:client.name")

    assert may_recall(memory, reader("read:client.*"), now=NOW) is not None


def test_a_memory_formed_under_several_capabilities_needs_all_of_them() -> None:
    """A memory formed while somebody could see the hours and the rate is evidence of both,
    and a reader holding one of the two has not earned it.

    Asserted with the reader holding a strict subset, because holding none is the easy case
    and holding some is the one an "any of" implementation passes.

    Delete this and `all` becomes `any` in one word, and a memory formed under the widest
    capability somebody held is recalled to anybody holding the narrowest."""
    memory = formed("read:client.hours", "read:client.rate")

    assert may_recall(memory, reader("read:client.hours"), now=NOW) is None
    assert may_recall(memory, reader("read:client.hours", "read:client.rate"), now=NOW) is not None


def test_a_memory_formed_in_one_department_is_not_recalled_in_another() -> None:
    """Scope is half of what a memory was formed from and it is the half a capability check
    alone would miss. Somebody with `read:client.name` in finance holds the capability and
    has never been anywhere near the web department's conversations.

    Delete this and the scope becomes decoration, and every memory in the estate is recalled
    to anybody holding the right verb anywhere."""
    memory = formed("read:client.name", scope=WEB)

    assert may_recall(memory, reader("read:client.name", scope=FINANCE), now=NOW) is None
    assert may_recall(memory, reader("read:client.name", scope=WEB), now=NOW) is not None


def test_a_reader_whose_access_has_expired_is_told_nothing() -> None:
    """`scope_for` refuses an expired principal, and this is what checks that recall goes
    through it rather than round it.

    A contractor's memories are the sharpest version of the problem: they were formed
    legitimately, the person is still in the directory, and the only thing that changed is a
    date. M16.1.4 calls this entitlement expiry and it is the case a hash comparison would
    also catch, which is not a reason to compare hashes: see the module docstring.

    Delete this and expiry stops applying to memory while still applying everywhere else,
    which is the shape of gap that survives review because every other surface is correct."""
    memory = formed("read:client.name")
    expired = reader("read:client.name", not_after=NOW - timedelta(days=1))

    assert may_recall(memory, expired, now=NOW) is None
    assert (
        may_recall(memory, reader("read:client.name", not_after=NOW + timedelta(days=1)), now=NOW)
        is not None
    )


def test_the_recorded_entitlement_hash_is_not_what_recall_compares() -> None:
    """Recording the hash and comparing it are different decisions and only the first is
    made here.

    A reader whose grants have changed since formation, in the widening direction, still
    reaches everything the memory was formed from. A hash comparison would refuse them, and
    the symptom is somebody promoted on Monday quietly losing everything the system learnt
    with them, with nothing anywhere saying why.

    The formation below records a hash that matches nobody, and the recall succeeds, which is
    the whole assertion.

    Delete this and an equality check on `ent_hash` looks like a tightening and passes every
    other test here, because every other test uses one reader and one formation."""
    memory = Formation(
        principal_id="p_writer",
        capabilities=(Capability(value="read:client.name"),),
        scope=WEB,
        ent_hash="a hash no reader will ever have",
        formed_at=NOW,
    )

    assert may_recall(memory, reader("read:client.name", "read:client.rate"), now=NOW) is not None


def test_a_recollection_has_nowhere_to_carry_an_answer() -> None:
    """The structural half of memory never being authoritative over the database.

    A recollection carries the formation and the confidence. It has no value, no row, no
    record id and no citation, so something wanting to answer from memory alone would have to
    add a field to this model rather than read one that is already there.

    Asserted on the model's fields rather than on behaviour, because a field added today is
    unused and load-bearing tomorrow, and by then the docstring arguing against it reads as
    history.

    Delete this and `value: str` appears on the model, and the first thing that uses it
    answers a question about the company from a cache of conversations."""
    names = {f.name for f in dataclass_fields(Recollection)}

    assert names == {"formation", "scope", "confidence"}
    for forbidden in ("value", "text", "answer", "row", "record_id", "citation", "content"):
        assert forbidden not in names, f"a recollection can carry a {forbidden}"


def test_a_formation_naming_no_capability_cannot_be_constructed() -> None:
    """A memory formed under nothing is recalled by everybody, because the reader trivially
    covers an empty requirement.

    That is the most dangerous shape this model can take and it is what a caller produces
    when it has an empty list and does not check. Refused at construction, so the object
    never exists rather than being filtered somewhere a future reader has to notice.

    Delete this and a formation built from an empty capability list becomes a memory with no
    permission attached, which every reader passes."""
    with pytest.raises(ValueError, match="recalled by everybody"):
        Formation(
            principal_id="p_writer",
            capabilities=(),
            scope=WEB,
            ent_hash="0" * 32,
            formed_at=NOW,
        )


def test_a_formation_with_a_naive_time_cannot_be_constructed() -> None:
    """A naive formation time compares wrongly against an aware one, and the comparison is
    what decides decay.

    Delete this and a writer in one timezone forms memories that decay from the wrong
    moment, which presents as memories that are too confident or not confident enough and
    never as a timezone bug."""
    with pytest.raises(ValueError, match="naive"):
        Formation(
            principal_id="p_writer",
            capabilities=(Capability(value="read:client.name"),),
            scope=WEB,
            ent_hash="0" * 32,
            formed_at=datetime(2026, 9, 7, 12, 0),
        )


def test_confidence_halves_over_the_half_life_and_keeps_halving() -> None:
    """The decay curve, checked at two points rather than one, because a single point is
    satisfied by any curve through it.

    Exponential rather than linear is the decision being tested: a linear decay reaches zero
    on a date, so everything formed that day stops mattering at once, which is a cliff nobody
    chose.

    Delete this and the half-life becomes a number nothing reads."""
    later = NOW + timedelta(days=HALF_LIFE_DAYS)
    much_later = NOW + timedelta(days=HALF_LIFE_DAYS * 2)

    assert confidence_now(1.0, formed_at=NOW, now=later) == pytest.approx(0.5)
    assert confidence_now(1.0, formed_at=NOW, now=much_later) == pytest.approx(0.25)
    assert confidence_now(1.0, formed_at=NOW, now=NOW) == pytest.approx(1.0)


def test_a_memory_from_the_future_does_not_gain_confidence() -> None:
    """Clock skew between a writer and a reader is ordinary. A memory that grew more certain
    because two machines disagreed would be the strangest possible bug to diagnose, because
    the value would be above the one it was formed with and nothing would explain it.

    Delete this and skew becomes a confidence bonus."""
    assert confidence_now(1.0, formed_at=NOW + timedelta(days=10), now=NOW) == pytest.approx(1.0)


def test_a_memory_below_the_floor_is_not_recalled_and_one_at_it_is() -> None:
    """The retrieval threshold, from both sides, because a floor that is never reached is
    decoration and one that is always reached is a system that has stopped forgetting.

    The boundary is asserted at the floor exactly as well as either side of it: a memory
    exactly at the threshold is recalled, which is the `>=` the module writes, and the
    difference between that and `>` is one memory nobody can explain the loss of.

    Delete this and decay stops deciding anything."""
    memory = formed("read:client.name")
    who = reader("read:client.name")

    assert may_recall(memory, who, now=NOW, formed_confidence=RECALL_FLOOR) is not None
    assert may_recall(memory, who, now=NOW, formed_confidence=RECALL_FLOOR - 0.01) is None


def test_reach_is_decided_before_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory the reader may not have is refused for that reason whatever its confidence,
    and a decayed memory is never the reason a permission question goes unanswered.

    The order matters for what the refusal means rather than for what it returns: both are
    None. It is asserted because a future author adding a reason to the refusal would put the
    confidence figure into it for the case where reach was the real problem, and a confidence
    figure attached to a memory somebody may not have is a statement that the memory exists.

    **Asserted by observing that confidence is never computed, because asserting on the
    return value cannot discriminate.** Both orderings return None, so a mutation moving the
    confidence check above the reach check survives every assertion about what comes back.
    That was a real survivor. Watching whether `confidence_now` is called is the only way the
    order is visible from outside without adding a reason to the refusal, and adding one is
    the thing this test exists to prevent.

    Delete this and the two refusals can be reordered, which is invisible until the day one
    of them grows an explanation."""
    memory = formed("read:client.contract_value")
    wrong_reader = reader("read:client.name")

    assert may_recall(memory, wrong_reader, now=NOW, formed_confidence=1.0) is None
    assert may_recall(memory, wrong_reader, now=NOW, formed_confidence=0.0) is None

    asked: list[float] = []
    original = formation_module.confidence_now

    def watched(formed_confidence: float, **kwargs: Any) -> float:
        asked.append(formed_confidence)
        return original(formed_confidence, **kwargs)

    monkeypatch.setattr(formation_module, "confidence_now", watched)
    try:
        assert may_recall(memory, wrong_reader, now=NOW, formed_confidence=1.0) is None
        assert asked == [], (
            "confidence was computed for a memory the reader may not have, so the refusal "
            "order has been swapped and a future reason would carry a figure about a memory "
            "whose existence is not this reader's to know"
        )
        assert may_recall(memory, reader("read:client.contract_value"), now=NOW) is not None
        assert asked, "confidence was never computed even for a memory the reader may have"
    finally:
        monkeypatch.setattr(formation_module, "confidence_now", original)


def test_a_set_of_memories_comes_back_filtered_with_no_count_of_what_was_dropped() -> None:
    """Three memories, one reachable, and the answer is one memory and nothing else.

    Not a count, not a total, not "and two others". A number attached to a filtered list is
    the difference between what somebody may see and what exists, which is the subtraction
    this system refuses everywhere and which is easiest to add here, because a recall surface
    naturally wants to say how much it looked at.

    Delete this and `recallable` grows a second return value, which is the most natural
    improvement anybody could make to it."""
    memories = [
        (formed("read:client.name"), 1.0),
        (formed("read:client.contract_value"), 1.0),
        (formed("read:client.name", scope=FINANCE), 1.0),
    ]

    found = recallable(memories, reader("read:client.name"), now=NOW)

    assert len(found) == 1
    assert isinstance(found, tuple)
    assert all(isinstance(one, Recollection) for one in found)


def test_recalled_memories_come_back_most_confident_first() -> None:
    """A caller taking the first few gets the ones most worth having, and the order is stable
    across runs rather than following whatever order the store returned.

    Ties break on formation time, so two equally confident memories come back in a fixed
    order rather than shuffling between runs, which is what makes a recall surface's output
    comparable between two days.

    Delete this and the ordering becomes the store's, which is not an ordering."""
    older = formed("read:client.name", at=NOW - timedelta(days=10))
    newer = formed("read:client.name", at=NOW - timedelta(days=1))
    found = recallable([(older, 1.0), (newer, 0.9)], reader("read:client.name"), now=NOW)

    assert [one.formation.formed_at for one in found] == [newer.formed_at, older.formed_at]


def test_a_session_key_separates_two_principals_on_one_thread() -> None:
    """Session memory lives in a cache with no row-level security in front of it, so the key
    is where the separation has to be.

    A thread id is guessable and reusable, and a key built from it alone would let a second
    principal read the first one's session by arriving on the same thread. The principal is
    in the key and not only in the value.

    Delete this and the least protected of the three memory kinds becomes the one with no
    protection at all."""
    assert session_key("t1", "p_ada") != session_key("t1", "p_ben")
    assert session_key("t1", "p_ada") == session_key("t1", "p_ada")

    for missing in (("", "p_ada"), ("t1", "")):
        with pytest.raises(ValueError, match="separates nobody"):
            session_key(*missing)


def test_a_session_expires_on_idle_and_every_word_moves_it() -> None:
    """A thread has no end anybody declares, so its lifetime is an idle bound, and every
    write to it moves the expiry. That is what makes it a lifetime rather than a timeout.

    Delete this and a long conversation is cut off in the middle, or a finished one is kept
    for ever, depending on which way somebody simplifies it."""
    first = session_expiry(NOW)
    after_speaking = session_expiry(NOW + timedelta(hours=1))

    assert first > NOW
    assert after_speaking > first


def test_the_requirement_puts_the_memorys_scope_on_every_grant() -> None:
    """Two places to put a scope is two things that can disagree, and the disagreement is
    silent in whichever direction the caller happened to write.

    Delete this and a memory formed under two capabilities can carry one scope for the first
    and none for the second, which admits a reader who reaches the second anywhere."""
    memory = formed("read:client.hours", "read:client.rate", scope=WEB)

    built = requirement(memory)

    assert len(built.grants) == 2
    assert all(grant.scope == WEB for grant in built.grants)


def test_the_kind_is_recorded_rather_than_inferred_from_where_a_row_was_found() -> None:
    """A session memory promoted to persistent is a real operation and the promotion has to
    be visible. Inferring the kind from the table a row came out of would make a promotion
    invisible, which is the one thing a learning system must not do quietly.

    Delete this and `kind` becomes derivable, then derived, then wrong the first time
    something is copied between stores."""
    assert formed("read:client.name").kind is MemoryKind.ADAPTIVE
    assert {one.value for one in MemoryKind} == {"session", "persistent", "adaptive"}
