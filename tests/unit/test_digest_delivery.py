"""Sending the evening digest: once, to a room, and whether or not anything happened.

`brain.ops.digest` computes the content and deliberately holds no channel. This is the one
call site it was shaped for, and the tests are about the three things that only become
questions once something is posted to a room every evening: sending it twice, not sending it
on a quiet day, and what happens when the channel says no.

Task ids: M38.3.3.1, M38.3.3.4
"""

from __future__ import annotations

from datetime import date

import pytest

from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.ops.digest import (
    NOTHING_CLOSED,
    BurnDown,
    DailyDigest,
    Forecast,
    Movement,
)
from brain.ops.digest_delivery import (
    Delivery,
    DeliveryOutcome,
    DigestChannel,
    classification_of_a_digest,
    deliver_digest,
)

DAY = date(2026, 9, 6)
CHAT = DigestChannel(channel=Channel.LARK, chat_id="oc_build_room")


class _Sender:
    """Records what it was asked to send. What a test reads instead of a Lark chat."""

    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[tuple[ChannelPayload, str]] = []
        self._fails = fails

    def send(self, payload: ChannelPayload, *, to: str) -> None:
        if self._fails:
            raise RuntimeError("oc_build_room is archived and the bot was removed from it")
        self.sent.append((payload, to))


class _Register:
    def __init__(self, *, sent_days: set[tuple[date, str]] | None = None) -> None:
        self.days = sent_days or set()

    def already_sent(self, *, day: date, chat_id: str) -> bool:
        return (day, chat_id) in self.days


def _digest(*, closed: tuple[str, ...] = ("M1.1",)) -> DailyDigest:
    return DailyDigest(
        day=DAY,
        wave=2,
        movement=Movement(closed=closed, measured=True),
        burn_down=BurnDown(
            wave=2,
            name="Data",
            total=10,
            closed=len(closed),
            remaining=10 - len(closed),
            target=date(2026, 9, 30),
            days_to_target=24,
            days_of_history=0,
            rate_per_day=0.0,
            projected_finish=None,
            verdict=Forecast.NOT_FORECASTABLE,
            because="not enough history yet",
        ),
    )


# --------------------------------------------------------------- it is sent
def test_a_digest_reaches_the_room_it_was_configured_for() -> None:
    """The positive case. A delivery module that never delivered would satisfy every refusal
    below and quietly stop the feature working, and nobody notices a report that stopped
    arriving until somebody asks for it."""
    sender = _Sender()

    result = deliver_digest(_digest(), channel=CHAT, sender=sender, register=_Register())

    assert result.outcome is DeliveryOutcome.SENT
    assert len(sender.sent) == 1
    assert sender.sent[0][1] == "oc_build_room"


def test_what_was_sent_is_what_the_digest_module_renders() -> None:
    """`digest.render` owns the wording. A second renderer for "the Lark version" is how the
    message people read stops matching the one the tests check, and the drift is invisible
    because both look right on their own."""
    sender = _Sender()

    result = deliver_digest(_digest(), channel=CHAT, sender=sender, register=_Register())

    assert "Build digest for 2026-09-06" in result.body
    assert "M1.1" in result.body


# --------------------------------------------------------------- exactly once
def test_a_day_already_sent_is_not_sent_again() -> None:
    """**A scheduler fires twice.** It is restarted, its timer is edited by somebody testing
    it, or the host reboots. Each of those posts the digest again, and a room with the same
    message in it twice is one people learn to skip, which costs the whole feature rather
    than one evening.

    Delete this and every scheduler restart is a duplicate."""
    sender = _Sender()
    register = _Register(sent_days={(DAY, "oc_build_room")})

    result = deliver_digest(_digest(), channel=CHAT, sender=sender, register=register)

    assert result.outcome is DeliveryOutcome.ALREADY_SENT
    assert sender.sent == []


def test_the_duplicate_check_happens_before_the_render() -> None:
    """Rendering is where the work is, and two renders of one day are two chances for the
    text to differ. Asserted by the body being absent from an already-sent outcome, which is
    only true if nothing was rendered.

    Delete this and the check moves below the render, which still prevents the duplicate send
    and does the work twice for nothing."""
    result = deliver_digest(
        _digest(),
        channel=CHAT,
        sender=_Sender(),
        register=_Register(sent_days={(DAY, "oc_build_room")}),
    )

    assert result.body == ""


def test_the_same_day_in_a_different_room_is_a_different_digest() -> None:
    """The register is keyed on both, because posting today's digest to a second channel is
    not a duplicate. Delete this and adding a channel silently sends nothing to it."""
    sender = _Sender()
    register = _Register(sent_days={(DAY, "oc_other_room")})

    result = deliver_digest(_digest(), channel=CHAT, sender=sender, register=register)

    assert result.outcome is DeliveryOutcome.SENT


# --------------------------------------------------------------- a quiet day still goes
def test_a_day_on_which_nothing_closed_is_still_delivered() -> None:
    """**The optimisation somebody will add here**, and the one `digest.py` already argues
    against: not sending when nothing happened.

    A digest that only arrives on busy days cannot report a stall, and a week of silence is
    then indistinguishable from a week nobody sent it. The reader cannot tell which, so they
    stop treating its absence as information.

    Delete this and "do not bother them, nothing happened" gets added, which reads as
    considerate and removes the only signal that matters."""
    sender = _Sender()

    result = deliver_digest(_digest(closed=()), channel=CHAT, sender=sender, register=_Register())

    assert result.outcome is DeliveryOutcome.SENT
    assert NOTHING_CLOSED in result.body


# --------------------------------------------------------------- the channel says no
def test_a_channel_that_refuses_does_not_lose_the_digest() -> None:
    """The digest is a record of what the plan did and it exists whether or not Lark accepted
    it. A scheduled job that raised on a transport error would lose the computation as well
    as the send, and the next evening has nothing to compare against.

    Delete this and a refused delivery becomes a traceback in a scheduler log."""
    result = deliver_digest(
        _digest(), channel=CHAT, sender=_Sender(fails=True), register=_Register()
    )

    assert result.outcome is DeliveryOutcome.UNDELIVERED
    assert "Build digest" in result.body, "the text that would have gone out is kept"


def test_a_refusal_records_the_kind_of_failure_and_never_its_message() -> None:
    """A transport exception stringifies whatever it failed on, and what it failed on here is
    a message naming every open task in the plan. The class name says enough to act on.

    Delete this and the digest's contents end up in whatever log records the failure, which
    is the one place nobody applied a classification to."""
    result = deliver_digest(
        _digest(), channel=CHAT, sender=_Sender(fails=True), register=_Register()
    )

    assert "RuntimeError" in result.detail
    assert "archived" not in result.detail
    assert "M1.1" not in result.detail


def test_the_three_outcomes_are_distinct_because_they_are_acted_on_differently() -> None:
    """ "The channel refused it" and "it was already sent" are both not-sent and are not the
    same fact: one is an incident, the other is the duplicate guard working. A scheduler
    treating them alike either alerts on every restart or stays silent through an outage."""
    assert (
        len({DeliveryOutcome.SENT, DeliveryOutcome.ALREADY_SENT, DeliveryOutcome.UNDELIVERED}) == 3
    )


# --------------------------------------------------------------- the audience
def test_the_digest_is_addressed_to_a_room_and_has_nowhere_to_name_a_person() -> None:
    """Everybody who reads it reads the same sentence, because it reports on the plan rather
    than answering anybody. A per-person send would be a broadcast with extra steps and would
    put a per-viewer render on a path that has no viewer.

    Delete this and a `viewer` argument gets added for an ephemeral digest, which is a
    per-person message that says the same thing to everybody."""
    assert set(DigestChannel.__dataclass_fields__) == {"channel", "chat_id"}
    assert "viewer" not in Delivery.__dataclass_fields__


def test_a_channel_with_no_chat_is_refused_rather_than_posting_nowhere() -> None:
    """An empty chat id sends into the void and reports success, which is the worst of the
    three outcomes because nobody investigates a delivery that said it worked."""
    with pytest.raises(ValueError, match="posts nowhere"):
        DigestChannel(channel=Channel.LARK, chat_id="  ")


def test_the_sensitivity_still_comes_from_the_module_that_computes_the_digest() -> None:
    """The digest names every open task, which is the shape of what is not built yet. That
    classification is decided where the content is decided, and re-declaring it here would be
    a second answer that drifts.

    Delete this and the constant gets copied, and the copy is the one a channel checks."""
    assert classification_of_a_digest() is Classification.INTERNAL
