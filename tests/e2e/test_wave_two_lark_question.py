"""The wave-2 milestone: the same question, asked in Lark, answered differently per person.

`docs/wbs` states what must be live after wave two as "the same question answered in Lark,
against connector cassettes". Every part of that sentence is load-bearing and this drives all
of it in one path: a message arrives as a Lark event, the sender is resolved to a principal
through a binding, their reach is computed from the company fixture, a row tool is projected
into a catalogue at that reach, a row is read, the redactor decides what may leave, and the
Lark adapter refuses to send anything the surface may not carry.

**"The same question" is the whole test.** One question object, asked by two people, must
produce two different answers, and neither person may see what the other's grants buy. A
system that answered both identically would pass any test that only checked one of them,
which is why the unit suites cannot substitute for this: each of those modules is correct on
its own and the question is whether they compose.

**Wei Ling is the fixture's own worked example.** Her note reads "sees-record-not-money, the
locked field on screen 3, as a fixture", and her `forbidden` list names the contract value
explicitly. She may see the client and may never see what it is worth, which is exactly the
case that separates a permission-aware system from one that filters rows.

**The canaries are what make a leak unmistakable.** `CANARY-CONTRACT-7Q4XZ` is not a
plausible number; if it appears in an answer it did not come from the model's imagination and
it did not come from a test row that looked like real data. It leaked.

**What this does not do.** No model is called, no HTTP is served, no database is touched. The
row source is driven from a cassette-shaped record rather than from a live Freshdesk, which
is what "against connector cassettes" asks for and is the reason the milestone is reachable
before anybody has credentials. The gap between this and a person actually typing into Lark
is the transport, which every adapter here deliberately leaves on the other side of `sent`.

Task ids: M38.2.2.3
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from brain.channels.lark import LarkAdapter
from brain.core.envelope import Entity, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, policy_from_rows
from brain.core.redaction import LOCK_TEXT, serialise_for_channel
from brain.gate.context import Channel
from brain.gate.ingress import Binding, ChannelEvent, identity_hash, resolve
from tests.fixtures.company import CANARIES, canary_tokens, person

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: The question. One object, asked by two people, which is the point of the milestone.
QUESTION = "how many hours are left on SNM Construction and what is the contract worth?"

#: Wei Ling sees the record and never the money; Aaron holds the money capability too. Both
#: are the fixture's own people rather than principals invented here, so what they may see is
#: decided by the company fixture and not by this test.
SEES_RECORD_NOT_MONEY = "u_weiling"
SEES_THE_MONEY = "u_aaron"


class Client(Entity):
    """One client record, shaped as a projection of the kind a connector produces."""

    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    hosting_expiry: str = "2026-11-14"
    hours_remaining: int = 12
    contract_value: str = CANARIES["client.contract_value"]
    margin: str = CANARIES["client.margin"]


POLICY: FieldPolicy = policy_from_rows(
    [
        ("client", "name", "read:client.name", Classification.INTERNAL),
        ("client", "department", "read:client.name", Classification.INTERNAL),
        ("client", "hosting_expiry", "read:client.hosting_expiry", Classification.INTERNAL),
        ("client", "hours_remaining", "read:client.hours_remaining", Classification.INTERNAL),
        ("client", "contract_value", "read:client.contract_value", Classification.RESTRICTED),
        ("client", "margin", "read:client.margin", Classification.RESTRICTED),
    ]
)


def _lark_event(sender: str) -> ChannelEvent:
    """The message as the Lark adapter hands it to the gate."""
    return ChannelEvent(
        channel=Channel.LARK,
        external_id=f"om_{sender}",
        channel_identity=f"ou_{sender}",
        text=QUESTION,
        received_at=NOW,
    )


def _bindings(*principal_ids: str) -> dict[str, Binding]:
    """A binding per person, keyed the way `resolve` looks them up: by salted digest."""
    return {
        identity_hash(Channel.LARK, f"ou_{pid}"): Binding(
            channel=Channel.LARK,
            identity_hash=identity_hash(Channel.LARK, f"ou_{pid}"),
            principal_id=pid,
            bound_at=NOW,
        )
        for pid in principal_ids
    }


def _answer_for(principal_id: str) -> tuple[Any, list[Any]]:
    """Drive the whole path for one person and return their payload and what Lark received.

    The order here is the system's order rather than a convenient one: resolve the sender,
    take their reach from the fixture, build the result a connector would have produced,
    redact at that reach, and hand the result to the adapter which applies the surface's own
    ceiling.
    """
    event = _lark_event(principal_id)
    binding = resolve(event, _bindings(SEES_RECORD_NOT_MONEY, SEES_THE_MONEY))
    assert binding is not None, (
        "the fixture's own person is not bound, so nothing else means anything"
    )
    assert binding.principal_id == principal_id

    entitlement = person(binding.principal_id).entitlement()
    result: TypedResult[Entity] = TypedResult(
        records=(Client(entity="client", id="c_0447"),),
        source="freshdesk",
        fetched_at=NOW.isoformat(),
    )

    payload = serialise_for_channel(result, entitlement=entitlement, policy=POLICY, now=NOW)

    adapter = LarkAdapter()
    adapter.send(payload, to="oc_maintenance")
    return payload, adapter.sent


# --------------------------------------------------------------- the milestone
def test_the_same_question_gets_two_different_answers() -> None:
    """**The sentence the wave-2 milestone is written as.** One question, two people, two
    answers, and the difference is their grants rather than anything in the question.

    A system that answered both identically would pass every unit test in this repository,
    because each module is correct alone and the failure is in the composition. This is the
    only test that asks whether they compose.

    Delete this and the wave can be declared closed on the strength of parts that have never
    been run together."""
    narrow, _ = _answer_for(SEES_RECORD_NOT_MONEY)
    wide, _ = _answer_for(SEES_THE_MONEY)

    assert narrow.records != wide.records
    assert set(narrow.records[0]) < set(wide.records[0]), (
        "the narrower person must see strictly fewer fields, not merely different ones"
    )


def test_the_person_who_may_not_see_the_money_does_not_see_the_money() -> None:
    """Wei Ling is the fixture's own worked example: her note calls her
    "sees-record-not-money, the locked field on screen 3, as a fixture", and her `forbidden`
    list names the contract value.

    Asserted on the canary rather than on the field's absence, because a field can be absent
    from a record and present in a label, a summary or a rendered body."""
    payload, sent = _answer_for(SEES_RECORD_NOT_MONEY)

    assert CANARIES["client.contract_value"] not in str(payload.model_dump())
    assert CANARIES["client.contract_value"] not in sent[0].body


def test_she_still_gets_the_answer_she_asked_for() -> None:
    """The half that makes the other half worth having. A system that withheld everything
    would satisfy every leak test here and be useless, and "the answer was refused" is the
    failure people actually report."""
    payload, sent = _answer_for(SEES_RECORD_NOT_MONEY)

    record = payload.records[0]
    assert record["name"] == "SNM Construction Pte Ltd"
    assert record["hours_remaining"] == 12
    assert "SNM Construction" in sent[0].body


def test_the_person_entitled_to_the_money_receives_it() -> None:
    """The other positive case. Withholding a restricted field from somebody who holds its
    capability is the same bug as leaking it, arriving from the other direction, and it is
    the one nobody files a security report about."""
    payload, _ = _answer_for(SEES_THE_MONEY)

    assert payload.records[0]["contract_value"] == CANARIES["client.contract_value"]


# --------------------------------------------------------------- nothing leaks anywhere
@pytest.mark.parametrize("principal_id", [SEES_RECORD_NOT_MONEY, SEES_THE_MONEY])
def test_no_canary_the_person_is_forbidden_reaches_them(principal_id: str) -> None:
    """Every canary on the fixture's `forbidden` list, checked against the whole payload and
    the whole rendered body rather than against the record alone.

    Parametrised over both people because the wide caller is forbidden things too, and a leak
    test that only ran against the narrow one would miss a field that leaks to everybody."""
    payload, sent = _answer_for(principal_id)
    rendered = str(payload.model_dump()) + sent[0].body

    for forbidden in person(principal_id).forbidden:
        token = CANARIES.get(forbidden)
        if token is None:
            continue
        assert token not in rendered, f"{principal_id} was shown {forbidden}"


def test_the_answer_carries_no_count_of_what_was_withheld() -> None:
    """DENIED and ABSENT must be indistinguishable, and a count is how that rule is broken by
    accident. "Showing 4 of 6 fields" tells the reader there are two things they may not see,
    which is two facts they did not have.

    Checked on the rendered body, because that is the string a person reads.

    **"Restricted" is deliberately not in this list, and working out why was worth the
    detour.** The body does contain it, because `render_lock` returns `LOCK_TEXT` and every
    withheld field renders as that word. It looks like the classification's name leaking and
    it is not: `render_lock()` takes no arguments at all, so it cannot vary by viewer, by
    field, by classification or by reason, and two people comparing screens learn nothing
    from it. The coincidence with `Classification.RESTRICTED` is cosmetic. The property that
    matters is asserted in the next test rather than by banning the word here."""
    _, sent = _answer_for(SEES_RECORD_NOT_MONEY)
    body = sent[0].body

    for leak in ("2 of", "of 6", "withheld", "redacted", "hidden", "out of scope"):
        assert leak not in body.lower(), f"the answer says {leak!r}"


def test_every_locked_field_renders_as_the_same_words() -> None:
    """The real guarantee behind the lock, driven end to end rather than at the unit.

    A lock that varied by field, classification or reason would be a side channel: two people
    comparing screens could read the difference and learn which of them was refused and why.
    Wei Ling has two fields locked, under the same classification but different capabilities,
    and both must be indistinguishable in what she actually receives.

    Delete this and `render_lock` can grow an argument, which would read as a helpful
    improvement to the message."""
    payload, sent = _answer_for(SEES_RECORD_NOT_MONEY)
    body = sent[0].body

    locked_names = sorted({f"{lock.entity}.{lock.field}" for lock in payload.locked})
    assert len(locked_names) >= 2, "this person should have more than one field locked"

    rendered = [line.split(": ", 1)[1] for line in body.splitlines() if ": " in line]
    lock_lines = [value for value in rendered if value == LOCK_TEXT]

    assert len(lock_lines) == len(locked_names)
    assert len(set(lock_lines)) == 1, f"locks render differently from each other: {set(lock_lines)}"


def test_the_binding_is_looked_up_by_digest_and_never_by_the_lark_id() -> None:
    """A binding table keyed on raw channel identities is a directory of every member of
    staff joined to their role. `resolve` keys on the salted digest, and this asserts the raw
    id is not what finds it."""
    event = _lark_event(SEES_RECORD_NOT_MONEY)
    bindings = _bindings(SEES_RECORD_NOT_MONEY)

    assert f"ou_{SEES_RECORD_NOT_MONEY}" not in bindings
    assert resolve(event, bindings) is not None


def test_an_unbound_sender_resolves_to_nobody() -> None:
    """The path this test drives begins with a binding, and somebody with none gets no
    principal at all rather than a default one. `gate.ingress.Unrecognised` is what they are
    told, and it says the same words whether the identity is unknown, known but unbound, or
    revoked this morning."""
    stranger = ChannelEvent(
        channel=Channel.LARK,
        external_id="om_stranger",
        channel_identity="ou_nobody",
        text=QUESTION,
        received_at=NOW,
    )

    assert resolve(stranger, _bindings(SEES_RECORD_NOT_MONEY)) is None


def test_every_canary_in_the_corpus_is_accounted_for_by_this_shape() -> None:
    """The guard on the leak tests above. They assert particular tokens are absent, and a
    canary that no longer appears in this record at all would make them pass for the wrong
    reason.

    So the two this record actually carries are asserted present in the corpus, which means
    renaming one breaks this rather than silently emptying the checks."""
    carried = {CANARIES["client.contract_value"], CANARIES["client.margin"]}

    assert carried <= canary_tokens()
