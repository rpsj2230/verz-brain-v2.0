"""A conversation turn. Every test is a way yesterday's permission answers today's question.

Task ids: M9.2.1, M9.2.2, M9.2.3, M9.2.4
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.chat.turns import (
    CONTEXT_DEPTH,
    Correction,
    CorrectionKind,
    RecordRef,
    Turn,
    TurnKind,
    assemble,
    context_for,
    dropped_from_context,
    record_correction,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.redaction import LOCK_TEXT, ChannelPayload, LockedField
from brain.core.scope import Scope

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)

READ_CLIENT = Capability(value="read:client.name")
READ_INVOICE = Capability(value="read:invoice.amount")


def _entitlement(*capabilities: Capability, not_after: datetime | None = None) -> EntitlementSet:
    return EntitlementSet(
        principal_id="u_weiling",
        grants=tuple(Grant(capability=c, scope=Scope.unrestricted()) for c in capabilities),
        not_after=not_after,
    )


def _answer(*refs: RecordRef, at: datetime = NOW, locked: tuple[LockedField, ...] = ()) -> Turn:
    return Turn(
        kind=TurnKind.ANSWER,
        at=at,
        principal_id="u_weiling",
        text="here is what I found",
        refs=refs,
        locked=locked,
    )


CLIENT_REF = RecordRef(entity="client", record_id="c_1", required=READ_CLIENT)
INVOICE_REF = RecordRef(entity="invoice", record_id="i_9", required=READ_INVOICE)


# ------------------------------------------------- what is shown (M9.2.1, M9.2.2)
def test_an_answer_is_assembled_from_the_payload_and_not_from_prose() -> None:
    """A model asked to summarise its own tool results fills gaps with something plausible,
    and a plausible sentence about a record the caller could not see is indistinguishable
    from a real one. The records shown are the records the redactor passed."""
    payload = ChannelPayload(records=({"@entity": "client", "@id": "c_1", "name": "Acme"},))
    shown = assemble(payload)
    assert shown.records == ({"@entity": "client", "@id": "c_1", "name": "Acme"},)
    assert not shown.empty


def test_every_lock_renders_identically() -> None:
    """A lock that varied by field, by reason or by viewer would let two people comparing
    screens work out which of them was refused and why. `render_lock` takes no arguments,
    which is what makes that impossible; rendering locks from a local string here would
    reintroduce it one channel at a time."""
    payload = ChannelPayload(
        records=({"@entity": "client", "@id": "c_1"},),
        locked=(
            LockedField(entity="client", record_id="c_1", field="contract_value"),
            LockedField(entity="client", record_id="c_1", field="margin"),
        ),
    )
    shown = assemble(payload)
    assert shown.locks == (LOCK_TEXT, LOCK_TEXT)
    assert len(set(shown.locks)) == 1, "two locks rendered differently"


def test_a_payload_with_no_records_is_marked_empty() -> None:
    """So the caller reaches the abstention path rather than composing its own sentence.
    Two channels writing their own "nothing found" is two wordings, and a difference between
    them is a difference somebody can read."""
    assert assemble(ChannelPayload()).empty


def test_the_shown_answer_carries_no_reason_for_any_lock() -> None:
    """Structural. "Out of scope" tells the asker the field exists on records elsewhere;
    "unclassified" tells them about the policy. Every lock is the same lock, and a type with
    nowhere to put a reason cannot acquire one in a later refactor."""
    shown = assemble(ChannelPayload())
    names = {f.name for f in dataclasses.fields(shown)}
    for forbidden in ("reason", "reasons", "why", "classification", "policy"):
        assert forbidden not in names


# ------------------------------------------------------ carried context (M9.2.3)
def test_a_follow_up_may_draw_on_what_the_asker_can_still_see() -> None:
    """The happy path. Without it nothing else here is testing a mechanism that works."""
    history = [_answer(CLIENT_REF)]
    assert context_for(history, _entitlement(READ_CLIENT), now=NOW) == (CLIENT_REF,)


def test_a_reference_the_asker_can_no_longer_see_falls_out_of_context() -> None:
    """The rule this module exists for. A transcript is a copy of an answer sitting where
    the next question can reach it, after the grant that justified it may have gone.

    Re-checked on read rather than filtered on write: checking at write time freezes the
    answer at the moment the transcript was written, which is exactly the freezing this
    prevents. Deleting this test makes a revoked grant take effect on new questions and not
    on follow-ups, which is the half of a conversation nobody thinks to test."""
    history = [_answer(CLIENT_REF, INVOICE_REF)]
    # The invoice grant is gone; the client grant remains.
    assert context_for(history, _entitlement(READ_CLIENT), now=NOW) == (CLIENT_REF,)


def test_an_expired_entitlement_carries_nothing_forward() -> None:
    """A contractor whose access ended on Friday must not have Thursday's references survive
    into Monday's follow-up. `holds` takes a time for this reason, and `context_for` requires
    one rather than defaulting, so the one place that could get expiry wrong has to say what
    time it thinks it is."""
    ending = _entitlement(READ_CLIENT, not_after=NOW + timedelta(days=1))
    history = [_answer(CLIENT_REF)]
    assert context_for(history, ending, now=NOW) == (CLIENT_REF,)
    assert context_for(history, ending, now=LATER) == ()


def test_the_asker_is_never_told_what_fell_away() -> None:
    """ "Two things from earlier are no longer available to you" is a statement about what
    they used to be able to see. It is a permission fact they are not owed, and one they
    could probe by watching the number change. The count exists for the operator's log; the
    answer simply stops mentioning those records."""
    history = [_answer(CLIENT_REF, INVOICE_REF)]
    entitlement = _entitlement(READ_CLIENT)
    assert dropped_from_context(history, entitlement, now=NOW) == 1
    # And nothing about the count is reachable from what the asker is shown.
    shown = assemble(ChannelPayload())
    assert not hasattr(shown, "dropped")


def test_context_does_not_reach_back_further_than_the_depth() -> None:
    """Not a memory limit: a bound on how far a question can reach without saying so. "What
    about the other one?" twenty turns later is a question about something the asker has
    forgotten the details of too."""
    history = [
        _answer(RecordRef(entity="client", record_id=f"c_{i}", required=READ_CLIENT))
        for i in range(CONTEXT_DEPTH + 4)
    ]
    kept = context_for(history, _entitlement(READ_CLIENT), now=NOW)
    assert len(kept) == CONTEXT_DEPTH
    assert kept[-1].record_id == f"c_{CONTEXT_DEPTH + 3}", "the most recent turn was dropped"


def test_a_record_mentioned_repeatedly_enters_the_context_once() -> None:
    """Otherwise a record referred to in four turns enters the prompt four times, which
    costs tokens and teaches the model that repetition means importance."""
    history = [_answer(CLIENT_REF), _answer(CLIENT_REF), _answer(CLIENT_REF)]
    assert context_for(history, _entitlement(READ_CLIENT), now=NOW) == (CLIENT_REF,)


def test_a_turn_has_nowhere_to_store_a_record_s_contents() -> None:
    """Structural, and it is the mechanism rather than the rule. A copy of a record still
    reads perfectly after the grant behind it is revoked, and nothing about the transcript
    would look wrong. An identifier yields nothing on the next turn."""
    names = {f.name for f in dataclasses.fields(RecordRef)}
    for forbidden in ("value", "values", "content", "data", "fields", "record", "row", "text"):
        assert forbidden not in names, f"RecordRef has a {forbidden!r} field"


# ---------------------------------------------------------- corrections (M9.2.4)
def test_a_correction_records_the_shape_of_the_disagreement() -> None:
    """Which answer, what kind of wrong, and what that answer drew on - so a reviewer can go
    and look at the same records."""
    history = [_answer(CLIENT_REF)]
    correction = record_correction(
        history, CorrectionKind.WRONG_FACT, principal_id="u_weiling", at=NOW + timedelta(minutes=1)
    )
    assert correction.kind is CorrectionKind.WRONG_FACT
    assert correction.refs == (CLIENT_REF,)
    assert correction.answer_at == NOW


def test_a_correction_has_nowhere_to_put_the_corrected_content() -> None:
    """The design, not an omission. Storing what the person said the right answer is turns
    the chat box into a write path into the knowledge base: no review, no scope, no
    provenance, available to anybody who can talk to the assistant.

    A person correcting an invoice total in chat is asserting something about a record they
    may not be entitled to write, through a channel that checks nothing. Deleting this test
    invites a `corrected_text` field that looks obviously useful."""
    names = {f.name for f in dataclasses.fields(Correction)}
    for forbidden in ("text", "content", "correction", "corrected", "value", "answer", "fact"):
        assert forbidden not in names, f"Correction has a {forbidden!r} field"


def test_a_correction_with_no_answer_behind_it_is_refused() -> None:
    """A complaint, not a correction. Counting it alongside real corrections would make the
    signal say the system is wrong more often than it is, and that signal is used to decide
    where to look."""
    question = Turn(kind=TurnKind.QUESTION, at=NOW, principal_id="u_weiling", text="hello?")
    with pytest.raises(ValueError, match="no answer"):
        record_correction([question], CorrectionKind.MISSING, principal_id="u_weiling", at=NOW)


def test_a_correction_cannot_predate_the_answer_it_corrects() -> None:
    """Ordering is the only thing making a correction attributable to an answer. One that
    arrived first is a record of something that did not happen."""
    with pytest.raises(ValueError, match="cannot predate"):
        Correction(
            answer_at=NOW,
            at=NOW - timedelta(minutes=1),
            principal_id="u_weiling",
            kind=CorrectionKind.STALE,
        )


def test_the_correction_attaches_to_the_most_recent_answer() -> None:
    """A conversation has several answers in it. "That is wrong" means the last one."""
    history = [
        _answer(CLIENT_REF, at=NOW),
        Turn(kind=TurnKind.QUESTION, at=NOW + timedelta(minutes=1), principal_id="u_weiling"),
        _answer(INVOICE_REF, at=NOW + timedelta(minutes=2)),
    ]
    correction = record_correction(
        history, CorrectionKind.STALE, principal_id="u_weiling", at=NOW + timedelta(minutes=3)
    )
    assert correction.refs == (INVOICE_REF,)
