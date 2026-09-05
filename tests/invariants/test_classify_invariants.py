"""What may reach the fast lane. A failure here blocks deploy.

The fast lane answers with no model in the loop, so nothing downstream can notice that the
question was slightly different from the one that got answered. Every test here is about
keeping near-misses out of it.

Task ids: M3.6.1, M3.6.3
"""

from __future__ import annotations

import pytest

from brain.core.lane import Lane
from brain.gate.classify import (
    INTENTS,
    TASK_WORD_COUNT,
    classify_lane,
    match_intent,
)

pytestmark = pytest.mark.invariant


# ------------------------------------------------------------- no model in classification
def test_classification_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The architecture gives two reasons and both matter: a round trip on every request,
    and letting text inside a retrieved document influence which jurisdiction handles the
    question. A classifier that can be argued with is not a classifier."""
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("classification tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    for question in ("hours left on Acme", "draft a report for each client", "who is Wei Ling"):
        assert classify_lane(question).lane in Lane


def test_classification_is_deterministic() -> None:
    """The same question must land in the same lane every time, or a trace explains a
    decision the system would not make again."""
    q = "when does SNM Construction's hosting expire"
    first = classify_lane(q)
    for _ in range(20):
        assert classify_lane(q) == first


# ------------------------------------------------------------- fast lane admission
@pytest.mark.parametrize(
    "question",
    [
        "hours left on Acme",
        "how many hours are remaining for SNM Construction",
        "when does Acme's hosting expire",
        "status of ticket 4471",
        "who manages Acme",
    ],
)
def test_an_exact_intent_reaches_the_fast_lane(question: str) -> None:
    """The lane has to be reachable or it is dead code, and its whole value is answering
    the common shapes in milliseconds."""
    decision = classify_lane(question)
    assert decision.lane is Lane.FAST
    assert decision.intent is not None


@pytest.mark.parametrize(
    "question",
    [
        # The qualifier changes the answer, and no model would be present to notice.
        "how many hours are left on Acme after the November work",
        "hours left on Acme, excluding the retainer",
        "when does Acme's hosting expire and what does renewal cost",
        "status of ticket 4471 and who is working on it",
        # A slot that is not there.
        "how many hours are left on",
        "status of ticket",
    ],
)
def test_a_near_miss_falls_through_to_a_model(question: str) -> None:
    """The single most important property in this module. Falling through is cheap; being
    confidently wrong with nothing able to catch it is not."""
    assert classify_lane(question).lane is not Lane.FAST


def test_a_qualifier_cannot_hide_inside_a_slot() -> None:
    """Anchoring is necessary and not sufficient, which a test found rather than review.
    The client slot accepts spaces, so it happily absorbed "after the November work" into
    the name and the whole pattern still matched end to end."""
    match = match_intent("how many hours are left on Acme after the November work")
    assert match is None


def test_the_cost_of_that_rule_is_a_fall_through_not_a_wrong_answer() -> None:
    """A real client name containing "and" is refused the fast lane. That is the correct
    trade and it is written down here so it reads as a decision rather than a bug: a
    fall-through costs one model call, and a wrong answer in a lane with no model has
    nothing downstream able to catch it."""
    assert match_intent("hours left on Smith and Jones Pte Ltd") is None
    assert classify_lane("hours left on Smith and Jones Pte Ltd").lane is Lane.ANSWER


def test_intent_patterns_are_anchored_at_both_ends() -> None:
    """Structural, so a new intent cannot be added unanchored. An unanchored pattern matches
    its shape inside a longer question, and the longer question almost always carries the
    qualifier that changes the answer."""
    for intent in INTENTS:
        assert intent.pattern.pattern.startswith(("^", "\\A")), intent.name
        assert intent.pattern.pattern.rstrip().endswith("$"), intent.name


def test_every_intent_declares_the_slots_it_needs() -> None:
    """An intent with no required slots would match its shape and answer about nothing."""
    for intent in INTENTS:
        assert intent.required_slots, intent.name
        for slot in intent.required_slots:
            assert f"?P<{slot}>" in intent.pattern.pattern, f"{intent.name} lacks slot {slot}"


def test_a_matched_intent_carries_its_slots() -> None:
    """The fast lane answers from the projection, which needs the identifier, not the
    sentence it was wrapped in."""
    match = match_intent("status of ticket 4471")
    assert match is not None
    assert match.slots["ticket"] == "4471"


# --------------------------------------------------------------- what a request can ask for
def test_asking_for_the_task_lane_is_honoured() -> None:
    """Someone asking for deep work knows more about what they want than a word count."""
    assert classify_lane("summarise this", requested=Lane.TASK).lane is Lane.TASK


def test_asking_for_the_fast_lane_is_not_enough_on_its_own() -> None:
    """The fast lane is not a speed preference. It is a claim that the question is one of a
    small closed set, and the claim is checked rather than accepted."""
    decision = classify_lane("what is going on with the Acme account", requested=Lane.FAST)
    assert decision.lane is Lane.ANSWER


def test_a_refused_fast_lane_request_says_so_in_the_reason() -> None:
    """A silently ignored preference looks like a bug to whoever asked for it."""
    decision = classify_lane("what is going on with the Acme account", requested=Lane.FAST)
    assert "fast lane requested" in decision.reason


def test_an_exact_intent_still_reaches_the_fast_lane_when_asked_for() -> None:
    assert classify_lane("hours left on Acme", requested=Lane.FAST).lane is Lane.FAST


# ------------------------------------------------------------------- the task lane
@pytest.mark.parametrize(
    "question",
    [
        "go through every client and tell me which have hosting expiring",
        "draft a summary of the maintenance work for November",
        "reconcile the Xero invoices against the Freshdesk tickets",
        "check the hours and then tell me who is over budget",
    ],
)
def test_multi_step_work_reaches_the_task_lane(question: str) -> None:
    """Nobody is watching a spinner for these, and answering them in the answer lane means
    a timeout rather than an answer."""
    assert classify_lane(question).lane is Lane.TASK


def test_a_very_long_question_is_work_whatever_it_says() -> None:
    assert classify_lane("client " * (TASK_WORD_COUNT + 1)).lane is Lane.TASK


# ------------------------------------------------------------------- the decision
def test_every_decision_records_a_reason() -> None:
    """M3.6.3. Written at the moment of the decision, because reconstructing it later means
    re-running a classifier that may have changed, and the trace would then explain a
    decision that was never made."""
    for question in (
        "hours left on Acme",
        "what is going on with Acme",
        "go through every client",
    ):
        assert classify_lane(question).reason.strip()


def test_the_default_is_the_answer_lane() -> None:
    """Where a person is waiting and a model reads the actual words. Roughly 95% of
    traffic, and the right place for anything the rules did not confidently place."""
    assert classify_lane("what is going on with the Acme account").lane is Lane.ANSWER
