"""Nothing withheld may be cited, and the trace goes one place. A failure here blocks deploy.

Composition is the last step before an answer leaves, which makes it the last chance to
undo everything the gate did. Citations are the sharp edge: a citation names a record and a
field, and a citation naming something the person never received hands them the fact that it
exists.

Task ids: M3.9.1, M3.9.2, M3.9.3, M3.9.4
"""

from __future__ import annotations

import pytest

from brain.core.redaction import (
    ChannelPayload,
    LockedField,
    RedactedAnswer,
    RedactionTrace,
)
from brain.gate.compose import ComposedAnswer, compose, new_trace_ref

pytestmark = pytest.mark.invariant


class RecordingSink:
    """A sink that remembers, so a test can assert what left and how often."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ChannelPayload, RedactionTrace]] = []

    def emit(self, reference: str, payload: ChannelPayload, trace: RedactionTrace) -> None:
        self.calls.append((reference, payload, trace))


TRACE = RedactionTrace(policy_epoch="epoch-a", ent_hash="ent-a")


def _payload(**over: object) -> ChannelPayload:
    base: dict[str, object] = {
        "records": (
            {"@entity": "client", "@id": "c_0447", "name": "SNM Construction", "status": "active"},
        ),
        "source": "laravel",
        "fetched_at": "14:31",
    }
    return ChannelPayload(**{**base, **over})  # type: ignore[arg-type]


def _answer(**over: object) -> RedactedAnswer:
    return RedactedAnswer(payload=_payload(**over), trace=TRACE)


# ----------------------------------------------------- citations cannot name the hidden
def test_a_citation_can_only_name_a_field_the_person_received() -> None:
    """The property the whole module turns on. Citations are derived from the payload, so
    citing something withheld is impossible rather than merely discouraged."""
    answer = compose("SNM is active.", _answer(), sink=RecordingSink())
    cited = {(c.entity, c.record_id, c.field) for c in answer.citations}
    assert cited == {
        ("client", "c_0447", "name"),
        ("client", "c_0447", "status"),
    }


def test_a_locked_field_is_never_cited() -> None:
    """A lock says a field exists on a record the asker may see. A citation would say the
    same thing again while implying the answer rests on it."""
    locked = (LockedField(entity="client", record_id="c_0447", field="contract_value"),)
    answer = compose("SNM is active.", _answer(locked=locked), sink=RecordingSink())
    assert all(c.field != "contract_value" for c in answer.citations)


def test_a_citation_carries_a_field_name_and_never_its_value() -> None:
    """A citation carrying the value would be a second copy of the answer travelling under
    a different name, surviving into places the payload does not."""
    answer = compose("SNM is active.", _answer(), sink=RecordingSink())
    rendered = " ".join(answer.render_citations())
    assert "SNM Construction" not in rendered
    assert "name" in rendered


def test_an_empty_payload_produces_no_citations_and_says_it_is_not_grounded() -> None:
    """An answer with no citations that still asserts a fact is the failure mode. The
    channel has to be able to tell the difference."""
    answer = compose(
        "I could not find that.",
        RedactedAnswer(payload=ChannelPayload(), trace=TRACE),
        sink=RecordingSink(),
    )
    assert answer.citations == ()
    assert answer.grounded is False


def test_a_grounded_answer_says_so() -> None:
    assert compose("SNM is active.", _answer(), sink=RecordingSink()).grounded is True


def test_the_record_identity_is_not_cited_as_though_it_were_a_claim() -> None:
    """ "client 447: id" is noise in every answer and crowds out the citations that matter."""
    answer = compose("x", _answer(), sink=RecordingSink())
    assert all(c.field not in ("id", "@id", "entity", "@entity") for c in answer.citations)


# --------------------------------------------------------------- freshness (M3.9.3)
def test_every_citation_carries_the_freshness_the_payload_recorded() -> None:
    """An answer with no freshness is read as current, and a stale number that looks live
    gets acted on."""
    answer = compose("SNM is active.", _answer(), sink=RecordingSink())
    assert all(c.fetched_at == "14:31" for c in answer.citations)
    assert "as of 14:31" in answer.render_citations()[0]


def test_freshness_comes_from_the_payload_rather_than_from_the_clock() -> None:
    """Composing at 3pm does not make a number fetched at 14:31 a 3pm number. The fetch
    time is a fact recorded when somebody fetched it."""
    answer = compose("x", _answer(fetched_at="09:02"), sink=RecordingSink())
    assert all(c.fetched_at == "09:02" for c in answer.citations)


def test_a_citation_reads_as_a_sentence_a_person_can_check() -> None:
    """A citation nobody can act on is decoration. It has to name the record well enough
    that someone can go and look."""
    rendered = compose("x", _answer(), sink=RecordingSink()).render_citations()[0]
    assert "client" in rendered
    assert "c_0447" in rendered
    assert "laravel" in rendered


# ------------------------------------------------------------ the trace sink (M3.9.2)
def test_the_trace_is_emitted_exactly_once() -> None:
    """Twice is two records of one event, and an auditor counting requests would be wrong."""
    sink = RecordingSink()
    compose("x", _answer(), sink=sink)
    assert len(sink.calls) == 1


def test_the_trace_is_emitted_even_though_the_answer_does_not_carry_it() -> None:
    """The two halves go to different places. The composed answer is what a person gets and
    it has no field that could hold a trace."""
    sink = RecordingSink()
    answer = compose("x", _answer(), sink=sink)
    assert sink.calls[0][2] is TRACE
    assert not hasattr(answer, "trace")
    assert "trace" not in {f for f in ComposedAnswer.__dataclass_fields__ if f != "trace_ref"}


def test_the_emitted_payload_is_the_redacted_one() -> None:
    """M3.9.2 says post-redaction, and that is the whole safety of allowing the payload into
    a trace at all. Emitting the pre-redaction result would put every withheld value into
    the one store that is kept longest."""
    sink = RecordingSink()
    redacted = _answer()
    compose("x", redacted, sink=sink)
    assert sink.calls[0][1] is redacted.payload


def test_the_reference_emitted_matches_the_one_returned() -> None:
    """A person quoting the reference on their answer has to reach the trace it belongs to,
    or the reference is decoration."""
    sink = RecordingSink()
    answer = compose("x", _answer(), sink=sink)
    assert sink.calls[0][0] == answer.trace_ref


# ----------------------------------------------------------- the reference (M3.9.4)
def test_two_identical_answers_get_different_references() -> None:
    """A reference derived from the content would let two people compare references and
    learn they received the same thing. Small, and it works across people, across time,
    and without either of them seeing the other's answer."""
    first = compose("x", _answer(), sink=RecordingSink()).trace_ref
    second = compose("x", _answer(), sink=RecordingSink()).trace_ref
    assert first != second


def test_references_are_not_guessable() -> None:
    """A reference that carries no authority still should not be enumerable, or the trace
    store becomes a list someone can walk."""
    refs = {new_trace_ref() for _ in range(500)}
    assert len(refs) == 500
    assert all(len(r) >= 16 for r in refs)


def test_the_reference_carries_no_data_about_the_answer() -> None:
    """It identifies which trace. Entitlement decides who may open it. A reference that
    granted access would be a capability handed to whoever the answer was forwarded to."""
    answer = compose("SNM is active.", _answer(), sink=RecordingSink())
    for leak in ("SNM", "c_0447", "client", "laravel"):
        assert leak not in answer.trace_ref


# --------------------------------------------------- the gate's redact step (M3.9.1)
def test_the_redact_step_runs_against_the_reach_the_gate_resolved() -> None:
    """It takes the frozen GateContext rather than a bare entitlement. A signature
    accepting an EntitlementSet would let a caller assemble one at this point, which is
    exactly the step the pipeline exists to make impossible."""
    import inspect

    from brain.gate.compose import redact_for_gate

    params = inspect.signature(redact_for_gate).parameters
    assert "context" in params
    assert "entitlement" not in params


def test_redacting_after_composing_is_refused_by_the_recorder() -> None:
    """Entering the step is not bookkeeping. Composing first and redacting afterwards would
    build an answer from fields nobody checked, and the recorder refuses the order."""
    from datetime import UTC, datetime

    from brain.gate.context import Channel, GateStep, StepOutOfOrderError, open_trace

    recorder = open_trace("t", datetime(2026, 9, 5, tzinfo=UTC), Channel.CONSOLE)
    recorder.enter(GateStep.COMPOSE)
    with pytest.raises(StepOutOfOrderError):
        recorder.enter(GateStep.REDACT)
