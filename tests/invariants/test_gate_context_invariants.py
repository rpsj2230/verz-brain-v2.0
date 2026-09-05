"""The order the gate does things in, and what a request is not allowed to skip.

Ordering here is a permission property, not a style preference. Each step's guarantee holds
only because the steps before it already ran, so a skipped step is a bypass that reads like
a refactor.

Task ids: M3.1.1, M3.1.2, M3.1.3, M3.1.4
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.core.entitlement import EntitlementSet
from brain.core.lane import Lane
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.gate.context import (
    Channel,
    GateContext,
    GateStep,
    Recorder,
    StepOutOfOrderError,
    TrafficClass,
    open_trace,
    traffic_class_for,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(
        id="p_wei_ling",
        kind=PrincipalKind.HUMAN,
        employment=Employment.STAFF,
        display_name="Wei Ling",
        primary_department="maintenance",
    )


def _context(**over: object) -> GateContext:
    ents = EntitlementSet(principal_id="p_wei_ling")
    base: dict[str, object] = {
        "trace_id": "t_0001",
        "principal": _principal(),
        "entitlements": ents,
        "ent_hash": ents.ent_hash(),
        "lane": Lane.ANSWER,
        "channel": Channel.CONSOLE,
        "traffic_class": TrafficClass.HUMAN_INTERACTIVE,
        "received_at": NOW,
    }
    return GateContext(**{**base, **over})  # type: ignore[arg-type]


# ------------------------------------------------------- traffic class has no default
def test_every_channel_declares_a_traffic_class() -> None:
    """M3.1.4. The mechanism is `assert_never`: a new Channel member that nobody classified
    is a type error rather than a request that quietly behaves like whatever the default
    happened to be."""
    for channel in Channel:
        assert isinstance(traffic_class_for(channel), TrafficClass)


def test_a_channel_where_a_person_waits_is_not_classified_as_automation() -> None:
    """The distinction is not the transport, it is whether someone is watching. Getting it
    wrong means queueing a request a person is waiting on, or degrading one nobody would
    notice had degraded."""
    for channel in (Channel.CONSOLE, Channel.LARK, Channel.WHATSAPP):
        assert traffic_class_for(channel) is TrafficClass.HUMAN_INTERACTIVE


def test_a_context_cannot_claim_a_traffic_class_its_channel_does_not_have() -> None:
    """Otherwise the declaration is advisory, and a caller can hand-write a context that
    claims to be automation to escape an interactive timeout."""
    with pytest.raises(ValueError, match="declares"):
        _context(channel=Channel.SCHEDULER, traffic_class=TrafficClass.HUMAN_INTERACTIVE)


# --------------------------------------------------- the recorder exists before identity
def test_the_trace_is_open_before_anything_is_known_about_the_caller() -> None:
    """M3.1.3. If the recorder were built after identification, a request that failed to
    identify would leave nothing behind, and those are the failures most worth reading."""
    recorder = open_trace("t_0001", NOW, Channel.LARK)
    assert recorder.reached(GateStep.RECORD)
    assert recorder.principal_id is None


def test_a_request_that_fails_at_identify_still_left_a_trace() -> None:
    """The property the ordering exists to produce. An unrecognised caller is a legitimate
    outcome, not an error, and it has to be visible afterwards."""
    recorder = open_trace("t_0002", NOW, Channel.WHATSAPP)
    recorder.enter(GateStep.INGEST)
    recorder.enter(GateStep.IDENTIFY)
    recorder.note("no principal bound to this WhatsApp number")

    assert recorder.principal_id is None
    assert recorder.reached(GateStep.RECORD)
    assert recorder.steps == [GateStep.RECORD, GateStep.INGEST, GateStep.IDENTIFY]


# --------------------------------------------------------------------- step ordering
def test_the_steps_run_in_order() -> None:
    """A skipped step is not a performance optimisation, it is a bypass."""
    recorder = open_trace("t_0003", NOW, Channel.CONSOLE)
    for step in (GateStep.INGEST, GateStep.IDENTIFY, GateStep.ENTITLE, GateStep.CLASSIFY):
        recorder.enter(step)
    assert recorder.steps[-1] is GateStep.CLASSIFY


def test_selecting_an_agent_before_resolving_entitlements_is_refused() -> None:
    """The specific ordering mistake that turns an agent into a principal. Choose the agent
    first and its tools get trimmed afterwards; resolve reach first and the catalogue is
    built from what the caller already holds."""
    recorder = open_trace("t_0004", NOW, Channel.CONSOLE)
    recorder.enter(GateStep.SELECT)
    with pytest.raises(StepOutOfOrderError, match="ENTITLE"):
        recorder.enter(GateStep.ENTITLE)


def test_composing_an_answer_before_redacting_it_is_refused() -> None:
    """The other ordering that matters. Compose before redact and the payload has already
    been built out of fields the caller may not see."""
    recorder = open_trace("t_0005", NOW, Channel.CONSOLE)
    recorder.enter(GateStep.COMPOSE)
    with pytest.raises(StepOutOfOrderError, match="REDACT"):
        recorder.enter(GateStep.REDACT)


def test_a_step_cannot_run_twice() -> None:
    """Running ENTITLE again mid-request is how a widened entitlement gets in after the
    catalogue was built from the narrow one."""
    recorder = open_trace("t_0006", NOW, Channel.CONSOLE)
    recorder.enter(GateStep.ENTITLE)
    with pytest.raises(StepOutOfOrderError):
        recorder.enter(GateStep.ENTITLE)


def test_redaction_sits_between_invocation_and_composition() -> None:
    """Stated as an ordering fact so that reordering the enum fails a test rather than
    quietly changing what reaches a person."""
    assert GateStep.INVOKE < GateStep.REDACT < GateStep.COMPOSE


def test_entitlement_resolution_precedes_every_step_that_uses_reach() -> None:
    assert GateStep.ENTITLE < GateStep.CACHE
    assert GateStep.ENTITLE < GateStep.SELECT
    assert GateStep.ENTITLE < GateStep.INVOKE


# ---------------------------------------------------------------- the context itself
def test_the_carried_hash_must_match_the_entitlements_beside_it() -> None:
    """A mismatched pair keys the cache on one reach and answers with another, which is the
    exact shape of a cross-person leak."""
    with pytest.raises(ValueError, match="does not match"):
        _context(ent_hash="not-the-real-hash")


def test_the_context_is_frozen() -> None:
    """A later step that could widen `entitlements` would break the one invariant the whole
    permission model rests on."""
    ctx = _context()
    with pytest.raises((AttributeError, TypeError)):
        ctx.entitlements = EntitlementSet(principal_id="someone_else")  # type: ignore[misc]


def test_a_context_knows_whether_a_person_is_waiting() -> None:
    """Asked at every degrade decision. Getting it from the traffic class rather than the
    channel means one rule, checked once."""
    assert _context().person_is_waiting
    assert not _context(
        channel=Channel.SCHEDULER, traffic_class=TrafficClass.SYSTEM
    ).person_is_waiting


def test_the_recorder_never_holds_a_field_value() -> None:
    """Notes are for what happened, not for what was withheld. A trace that records the
    value it refused to show has moved the leak rather than closed it."""
    recorder = Recorder(
        trace_id="t",
        received_at=NOW,
        channel=Channel.CONSOLE,
        traffic_class=TrafficClass.HUMAN_INTERACTIVE,
    )
    recorder.note("withheld 2 fields on client:447")
    assert all("$" not in note for note in recorder.notes)
    assert recorder.notes == ["withheld 2 fields on client:447"]
