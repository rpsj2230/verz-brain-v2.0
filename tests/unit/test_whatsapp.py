"""WhatsApp: a delivery constraint that is really a permission, and an approval that is not one.

Two claims carry this file.

**The window opens when the person writes to us, never when we write to them.** The
direction is the whole rule. A window an outbound message renewed would renew itself for
ever, so the first message would buy an unbounded licence to keep sending and the constraint
would permit exactly what it exists to refuse. Tested from both sides: an inbound message
opens one, and there is no method on `SessionWindows` that an outbound message could reach.

**An approved template is approved text with holes in it, and nobody approved the holes.**
Two independent constraints, and the tests below hold each one while the other passes, which
is the only way to show that neither is carrying the other. A payload built at the right
reach with a slot filled from anywhere is refused, and a slot filled by reference out of the
wrong person's payload is refused.

Task ids: M10.5.3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.adapter import Feature
from brain.channels.whatsapp import (
    SESSION_WINDOW,
    WHATSAPP_FEATURES,
    SendKind,
    SessionWindows,
    SlotSource,
    Template,
    WhatsAppAdapter,
    WhatsAppRefusedError,
    deliver,
    fill,
    free_text,
    normalise_messages,
    templated,
    unrecognised_reply,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.core.scope import Scope
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
NUMBER = "+6591234567"
DIGEST = identity_hash(Channel.WHATSAPP, NUMBER)


def _ents(*capabilities: str, principal_id: str = "u_asker") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _event(
    *,
    channel: Channel = Channel.WHATSAPP,
    identity: str = NUMBER,
    received_at: datetime = NOW,
    text: str = "what is the balance",
) -> ChannelEvent:
    return ChannelEvent(
        channel=channel,
        external_id="wamid.1",
        channel_identity=identity,
        text=text,
        received_at=received_at,
    )


def _payload(**fields: object) -> ChannelPayload:
    return ChannelPayload(records=({"invoice": "INV-1", **fields},))


# ------------------------------------------------------- the window's direction (M10.5.3)
def test_a_message_from_the_person_opens_their_window() -> None:
    """The positive case. A registry that never opened a window would satisfy every refusal
    in this file and make the channel template-only for ever, which is a working system that
    has quietly stopped doing the thing it was built for."""
    windows = SessionWindows()
    windows.note_inbound(_event())

    assert windows.is_open(DIGEST, NOW) is True
    assert windows.opened_at(DIGEST) == NOW


def test_there_is_no_method_by_which_an_outbound_message_could_open_a_window() -> None:
    """**The rule is a direction, so it is enforced by the shape of the type rather than by
    an argument.** A `note(event, direction=...)` would make the direction something a caller
    passes, and a caller that passes it wrongly renews a window for ever.

    Delete this and an `note_outbound` can be added for symmetry, which reads as tidy and
    turns a bounded permission into an unbounded one."""
    surface = {name for name in dir(SessionWindows) if not name.startswith("_")}

    assert surface == {"note_inbound", "opened_at", "is_open"}, (
        "only an inbound message may move the window; see ONLY_AN_INBOUND_MESSAGE_OPENS_THE_WINDOW"
    )


def test_somebody_who_has_never_written_to_us_has_no_open_window() -> None:
    """A person we have no record of is not open by default. The permission being modelled is
    one they granted by writing, and a default of open is a default of never having asked."""
    assert SessionWindows().is_open(DIGEST, NOW) is False


def test_the_window_closes_exactly_at_the_vendors_twenty_four_hours() -> None:
    """The boundary, where an off-by-one becomes a message the vendor rejects or a permission
    taken that was not given. Delete this and `<` can become `<=` and the window quietly runs
    a moment past what was granted."""
    windows = SessionWindows()
    windows.note_inbound(_event())

    assert windows.is_open(DIGEST, NOW + SESSION_WINDOW - timedelta(seconds=1)) is True
    assert windows.is_open(DIGEST, NOW + SESSION_WINDOW) is False


def test_a_redelivered_older_message_does_not_pull_the_window_backwards() -> None:
    """The vendor redelivers on its own schedule. A window that shrank when it did would
    refuse free text somebody had earned, and the cause would be invisible because the
    redelivery is not something this system can see happening."""
    windows = SessionWindows()
    windows.note_inbound(_event(received_at=NOW))
    windows.note_inbound(_event(received_at=NOW - timedelta(hours=6)))

    assert windows.opened_at(DIGEST) == NOW


def test_an_event_from_another_channel_cannot_open_a_whatsapp_window() -> None:
    """The digest is salted per channel, so a Lark event could not silently open a WhatsApp
    window anyway. What it would do instead is write a key nothing ever reads, leaving a
    channel that can only send templates and no error saying why. A registry wired to the
    wrong stream is a fault and should say so."""
    with pytest.raises(WhatsAppRefusedError, match="wrong stream"):
        SessionWindows().note_inbound(_event(channel=Channel.LARK))


def test_free_text_is_refused_once_the_window_has_closed() -> None:
    """The rule doing its job. Delete this and anything we wrote can be sent at any time, and
    the vendor's refusal arrives as a delivery failure nobody traces back to a permission."""
    windows = SessionWindows()
    windows.note_inbound(_event())

    with pytest.raises(WhatsAppRefusedError, match="session window is closed"):
        free_text(
            to_identity=DIGEST,
            payload=_payload(),
            now=NOW + SESSION_WINDOW,
            sessions=windows,
        )


def test_free_text_inside_the_window_is_permitted_and_does_not_renew_it() -> None:
    """Both halves in one test because they are one property: sending is allowed, and sending
    is not a reason to keep being allowed. Delete this and `free_text` can note its own send,
    which is the unbounded window written as a convenience."""
    windows = SessionWindows()
    windows.note_inbound(_event())
    later = NOW + timedelta(hours=6)

    send = free_text(to_identity=DIGEST, payload=_payload(), now=later, sessions=windows)

    assert send.kind is SendKind.FREE_TEXT
    assert windows.opened_at(DIGEST) == NOW, "our own message must not move the window"


def test_a_template_needs_no_window_and_has_no_way_to_consult_one() -> None:
    """The asymmetry is the vendor's rule expressed in two signatures rather than in a branch.
    There is no argument to pass `templated` that would make it check a window, and none to
    pass `free_text` that would make it skip one.

    Delete this and a `sessions` parameter can be added to `templated` for symmetry, which is
    a check with no outcome that a later reader tightens into one."""
    template = Template(name="balance", language="en", body="Your invoice {{1}} is ready.")
    recipient = _ents("read:finance")

    send = templated(
        template,
        to_identity=DIGEST,
        payload=_payload(),
        body_ent_hash=recipient.ent_hash(),
        recipient=recipient,
        sources={"1": SlotSource(record=0, field="invoice")},
    )

    assert send.kind is SendKind.TEMPLATE
    assert send.body == "Your invoice INV-1 is ready."


# ------------------------------------------------ nobody approved the substitution (M10.5.3)
def test_a_payload_computed_at_another_persons_reach_cannot_fill_a_template() -> None:
    """**The first of the two constraints, held while the second passes.** Every slot here is
    filled by reference out of the payload, which is correct; the payload is the wrong
    person's. Without this check a template sent to a manager can carry the junior asker's
    answer, wearing the approval of text that discloses nothing.

    Delete this and the reference-only rule alone is left, which guarantees the values are
    real payload values and says nothing about whose."""
    asker = _ents("read:finance", principal_id="u_junior")
    recipient = _ents("read:finance", "read:payroll", principal_id="u_manager")
    template = Template(name="balance", language="en", body="Invoice {{1}}.")

    with pytest.raises(WhatsAppRefusedError, match="computed at"):
        fill(
            template,
            payload=_payload(),
            body_ent_hash=asker.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="invoice")},
        )


def test_a_slot_cannot_be_filled_from_a_field_the_gate_did_not_put_in_the_payload() -> None:
    """**The second constraint, held while the first passes.** The reach matches, so this is
    the right person's payload; the slot names something that is not in it, which is what a
    field the redactor locked looks like from here.

    Delete this and a correctly addressed payload sits beside a slot filled from anywhere at
    all, which is the entire failure the reference-only design prevents."""
    recipient = _ents("read:finance")
    template = Template(name="balance", language="en", body="Salary {{1}}.")

    with pytest.raises(WhatsAppRefusedError, match="not in the payload"):
        fill(
            template,
            payload=_payload(),
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="salary")},
        )


def test_a_slot_source_has_nowhere_to_put_a_value_a_caller_fetched_itself() -> None:
    """The constraint expressed as a type rather than as a check. A caller holding a figure it
    fetched has no field to put it in, which is the half of the rule a hash comparison cannot
    provide: the hash says the payload was built for the right person and says nothing about a
    string beside it.

    Delete this and a `value` field can be added to `SlotSource` as a convenience for the
    caller that already has the number."""
    assert set(SlotSource.__dataclass_fields__) == {"record", "field"}


def test_a_refusal_names_the_field_and_never_a_value_from_the_record() -> None:
    """A refusal quoting the value writes the thing being withheld into whatever log records
    the refusal, which is the leak arriving through the mechanism built to prevent it."""
    recipient = _ents("read:finance")
    payload = ChannelPayload(records=({"invoice": "INV-1", "salary": "92000"},))
    template = Template(name="t", language="en", body="{{1}}")

    with pytest.raises(WhatsAppRefusedError) as caught:
        fill(
            template,
            payload=payload,
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="missing")},
        )

    assert "92000" not in str(caught.value)
    assert "INV-1" not in str(caught.value)


def test_a_slot_with_no_source_is_refused_rather_than_left_standing() -> None:
    """An unfilled slot reaching the provider is not a rendering blemish. The provider
    substitutes what it is handed, so a slot this function did not check is one filled by
    whatever the caller puts on the wire."""
    recipient = _ents("read:finance")
    template = Template(name="t", language="en", body="Invoice {{1}} for {{2}}.")

    with pytest.raises(WhatsAppRefusedError, match="no source"):
        fill(
            template,
            payload=_payload(),
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="invoice")},
        )


def test_a_source_for_a_slot_the_template_does_not_have_is_refused() -> None:
    """A caller that believes it has filled a slot which does not exist is a caller whose
    values are going somewhere this function is not looking."""
    recipient = _ents("read:finance")
    template = Template(name="t", language="en", body="Invoice {{1}}.")

    with pytest.raises(WhatsAppRefusedError, match="does not have"):
        fill(
            template,
            payload=_payload(amount="10"),
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={
                "1": SlotSource(record=0, field="invoice"),
                "2": SlotSource(record=0, field="amount"),
            },
        )


def test_a_newline_in_a_slot_cannot_append_lines_nobody_approved() -> None:
    """The reader trusts the message because the top of it is the approved wording. A value
    carrying a line break writes unapproved lines underneath it, inside a message the vendor
    signed off."""
    recipient = _ents("read:finance")
    payload = ChannelPayload(records=({"invoice": "INV-1\nPay now to bit.ly/x"},))
    template = Template(name="t", language="en", body="Invoice {{1}}.")

    with pytest.raises(WhatsAppRefusedError, match="line break"):
        fill(
            template,
            payload=payload,
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="invoice")},
        )


def test_a_nested_object_cannot_be_flattened_into_one_slot() -> None:
    """A subtree rendered through `str` puts every field of it into an approved message, and
    none of them was named by anybody. A slot carries one scalar."""
    recipient = _ents("read:finance")
    payload = ChannelPayload(records=({"invoice": {"id": "INV-1", "salary": "92000"}},))
    template = Template(name="t", language="en", body="Invoice {{1}}.")

    with pytest.raises(WhatsAppRefusedError, match="whole subtree"):
        fill(
            template,
            payload=payload,
            body_ent_hash=recipient.ent_hash(),
            recipient=recipient,
            sources={"1": SlotSource(record=0, field="invoice")},
        )


# ------------------------------------------------------- the ceiling and the surface
def test_this_surface_declares_no_cards_because_a_message_is_not_a_signature() -> None:
    """`gate.admission.CHANNEL_VERBS` gives WhatsApp `read` alone, so a button press here
    could never be honoured as an approval. Declaring cards would build one that fails at the
    press rather than refusing at the build, and the person who pressed it would reasonably
    believe they had approved something.

    Delete this and CARDS can be added because the vendor supports reply buttons, which is
    true and is not the question."""
    assert Feature.CARDS not in WHATSAPP_FEATURES
    assert WhatsAppAdapter().capabilities().features == WHATSAPP_FEATURES


def test_the_classification_ceiling_is_internal_and_not_confidential() -> None:
    """A consumer application on a personal handset, with backups, a second device and a
    screen anybody standing nearby can read. Raising this is a decision somebody makes
    deliberately, not one that arrives by a constant being edited to make a send work."""
    assert WhatsAppAdapter().capabilities().max_classification is Classification.INTERNAL


# ------------------------------------------------------- the number (M10.5.3)
def test_a_plan_cannot_be_delivered_to_a_number_it_was_not_planned_for() -> None:
    """Without the check the number is simply a second argument, and the mistake that sends
    one person's answer to another is a variable name.

    Delete this and `deliver` becomes a two-argument function where the arguments are not
    required to agree."""
    adapter = WhatsAppAdapter()
    windows = SessionWindows()
    windows.note_inbound(_event())
    send = free_text(to_identity=DIGEST, payload=_payload(), now=NOW, sessions=windows)

    with pytest.raises(WhatsAppRefusedError, match="planned for somebody else"):
        deliver(adapter, send, to_number="+6598765432")

    assert adapter.sent == [], "nothing reaches the wire when the recipient disagrees"


def test_the_refusal_names_neither_the_number_nor_the_digest_it_expected() -> None:
    """Both reach a log from here, and the pair of them is the phone book this module declines
    to keep. Delete this and a diagnostic improvement puts the number in the message."""
    windows = SessionWindows()
    windows.note_inbound(_event())
    send = free_text(to_identity=DIGEST, payload=_payload(), now=NOW, sessions=windows)

    with pytest.raises(WhatsAppRefusedError) as caught:
        deliver(WhatsAppAdapter(), send, to_number="+6598765432")

    assert "+6598765432" not in str(caught.value)
    assert DIGEST not in str(caught.value)


def test_a_delivered_message_records_the_digest_and_never_the_number() -> None:
    """The positive case, and the one that shows the address is used once and not kept. A list
    of numbers beside the messages they received is the phone book `gate.ingress.Binding`
    refuses to be."""
    adapter = WhatsAppAdapter()
    windows = SessionWindows()
    windows.note_inbound(_event())
    send = free_text(to_identity=DIGEST, payload=_payload(), now=NOW, sessions=windows)

    deliver(adapter, send, to_number=NUMBER)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].to_identity == DIGEST
    assert NUMBER not in adapter.sent[0].to_identity


# ------------------------------------------------------- the unrecognised sender
def test_an_unrecognised_sender_is_told_the_words_the_gate_already_wrote() -> None:
    """This module defines no prompt of its own. `gate.ingress.UNRECOGNISED_PROMPT` answers an
    unknown number, a known but unbound one, and one whose binding was revoked this morning
    with the same words, and a second prompt written here would be a second thing to get wrong
    in the direction that confirms a number belongs to somebody."""
    windows = SessionWindows()
    windows.note_inbound(_event())
    reach = Unrecognised(channel=Channel.WHATSAPP)

    send = unrecognised_reply(reach, to_identity=DIGEST, now=NOW, sessions=windows)

    assert send.body == reach.prompt
    assert send.kind is SendKind.FREE_TEXT


def test_an_unrecognised_sender_outside_the_window_gets_silence_not_a_template() -> None:
    """There is no approved wording that says this, and inventing one would be inventing a
    prompt. Silence is the honest answer to a message that sat in a queue too long."""
    with pytest.raises(WhatsAppRefusedError, match="no approved wording"):
        unrecognised_reply(
            Unrecognised(channel=Channel.WHATSAPP),
            to_identity=DIGEST,
            now=NOW,
            sessions=SessionWindows(),
        )


def test_a_reach_built_for_another_channel_is_not_sent_over_whatsapp() -> None:
    """The prompt a person is given is per channel: the widget's differs and argues at length
    why, and none of that argument transfers to a phone number."""
    windows = SessionWindows()
    windows.note_inbound(_event())

    with pytest.raises(WhatsAppRefusedError, match="per channel"):
        unrecognised_reply(
            Unrecognised(channel=Channel.WIDGET),
            to_identity=DIGEST,
            now=NOW,
            sessions=windows,
        )


# ------------------------------------------------------- reading what the vendor posts
def _webhook(*messages: dict[str, object]) -> dict[str, object]:
    """The Cloud API envelope, which batches entries and changes around the messages."""
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


def _text_message(n: int, sender: str = NUMBER) -> dict[str, object]:
    return {
        "id": f"wamid.{n}",
        "from": sender,
        "timestamp": "1757160000",
        "type": "text",
        "text": {"body": "hello"},
    }


def test_a_non_text_message_is_not_read_as_a_blank_question() -> None:
    """An image, a location or a sticker is a different event with a different shape. Reading
    one as text puts an empty question through the gate, which answers it from whatever the
    empty string retrieves."""
    raw = _webhook({"id": "wamid.1", "from": NUMBER, "timestamp": "1757160000", "type": "image"})

    assert normalise_messages(raw) == ()


def test_one_unreadable_message_does_not_throw_away_the_questions_beside_it() -> None:
    """**A defect found by writing this test, and fixed rather than documented.**

    `_one_message` raises on anything that is not text, and the batch loop called it across
    every message, so one image refused the whole delivery. The Cloud API batches messages
    from *different senders* into one POST, which means a sticker from one person discarded a
    question from another, and the person who asked got silence with nothing anywhere saying
    why.

    The module already made exactly this argument about delivery receipts, one loop above,
    and then did the opposite. Delete this and the batch goes back to letting one unreadable
    message speak for the rest of it."""
    raw = _webhook(
        {"id": "wamid.1", "from": "+6590000001", "timestamp": "1757160000", "type": "sticker"},
        _text_message(2, sender="+6590000002"),
    )

    events = normalise_messages(raw)

    assert len(events) == 1
    assert events[0].channel_identity == "+6590000002"


def test_a_delivery_receipt_does_not_refuse_the_messages_beside_it() -> None:
    """A receipt is an ordinary thing to receive. Refusing the batch it arrives in would
    refuse real questions on the strength of a status update."""
    raw = {
        "entry": [
            {
                "changes": [
                    {"value": {"statuses": [{"id": "wamid.0", "status": "delivered"}]}},
                    {"value": {"messages": [_text_message(1)]}},
                ]
            }
        ]
    }

    assert len(normalise_messages(raw)) == 1


def test_the_single_message_path_refuses_a_batch_rather_than_picking_the_first() -> None:
    """Picking the first drops the rest with nothing anywhere saying so, which is two people's
    questions silently unanswered."""
    raw = _webhook(_text_message(1), _text_message(2))

    with pytest.raises(WhatsAppRefusedError, match="normalise_messages"):
        WhatsAppAdapter().normalise(raw)
