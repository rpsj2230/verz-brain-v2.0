"""Which agent answers, and what a caller learns about the ones they cannot see.

Selection decides which tools reach the question, so it is a permission-adjacent decision
even though it grants nothing itself.

Task ids: M3.6.2
"""

from __future__ import annotations

import re

import pytest

from brain.gate.context import Channel
from brain.gate.select import (
    AgentBinding,
    SelectionRule,
    SelectionStage,
    select_agent,
)

pytestmark = pytest.mark.invariant

ALL = frozenset({"support", "finance", "delivery", "special", "general"})


def _select(question: str = "hello", **over: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "channel": Channel.LARK,
        "visible_agents": ALL,
        "default_agent": "general",
    }
    return select_agent(question, **{**base, **over})  # type: ignore[arg-type]


# -------------------------------------------------------------------- the order
def test_a_binding_beats_a_rule() -> None:
    """A binding is a person deciding about this exact conversation. A rule is a person
    deciding about many. The more specific decision has to win, or the person who set the
    binding watches it stop working with no way to see why."""
    chosen = _select(
        "an invoice question",
        bindings=[AgentBinding(channel=Channel.LARK, agent_id="support")],
        rules=[SelectionRule(pattern=re.compile("invoice"), agent_id="finance")],
    )
    assert chosen.agent_id == "support"
    assert chosen.stage is SelectionStage.BINDING


def test_a_rule_beats_the_classifier() -> None:
    """A rule is somebody's decision; the classifier is the system guessing."""
    chosen = _select(
        "an invoice question",
        rules=[SelectionRule(pattern=re.compile("invoice"), agent_id="support")],
    )
    assert chosen.agent_id == "support"
    assert chosen.stage is SelectionStage.RULE


def test_a_conversation_binding_beats_a_channel_binding() -> None:
    """Both are bindings, and the one naming this conversation is the more specific."""
    chosen = _select(
        bindings=[
            AgentBinding(channel=Channel.LARK, agent_id="general"),
            AgentBinding(channel=Channel.LARK, agent_id="special", conversation_id="oc_1"),
        ],
        conversation_id="oc_1",
    )
    assert chosen.agent_id == "special"


def test_a_binding_for_another_conversation_does_not_apply() -> None:
    chosen = _select(
        bindings=[AgentBinding(channel=Channel.LARK, agent_id="special", conversation_id="oc_1")],
        conversation_id="oc_2",
    )
    assert chosen.stage is SelectionStage.DEFAULT


def test_a_binding_for_another_channel_does_not_apply() -> None:
    chosen = _select(
        bindings=[AgentBinding(channel=Channel.WHATSAPP, agent_id="support")],
    )
    assert chosen.stage is SelectionStage.DEFAULT


# ------------------------------------------------------------------- visibility
def test_an_agent_the_caller_cannot_see_is_skipped_in_silence() -> None:
    """Audience is not authority: visibility decides who can find an agent. Saying "that
    agent is not available to you" would confirm it exists, which is the same mistake as a
    refusal that explains itself."""
    chosen = _select(
        bindings=[AgentBinding(channel=Channel.LARK, agent_id="special")],
        visible_agents=frozenset({"general"}),
    )
    assert chosen.agent_id == "general"
    assert chosen.stage is SelectionStage.DEFAULT
    assert "special" not in chosen.reason


def test_an_invisible_rule_target_falls_through_rather_than_failing() -> None:
    """A person whose rule points at an agent they cannot see gets an answer from the
    default, not an error naming the agent."""
    chosen = _select(
        "an invoice question",
        rules=[SelectionRule(pattern=re.compile("invoice"), agent_id="finance")],
        visible_agents=frozenset({"general"}),
    )
    assert chosen.agent_id == "general"
    assert "finance" not in chosen.reason


def test_an_invisible_classifier_target_falls_through() -> None:
    chosen = _select("an invoice question", visible_agents=frozenset({"general"}))
    assert chosen.agent_id == "general"


def test_the_reason_never_names_an_agent_the_caller_cannot_see() -> None:
    """The reason is written into the trace and may also be shown. Neither should carry the
    name of an agent whose existence the caller is not entitled to."""
    chosen = _select(
        "an invoice question",
        bindings=[AgentBinding(channel=Channel.LARK, agent_id="special")],
        rules=[SelectionRule(pattern=re.compile("invoice"), agent_id="finance")],
        visible_agents=frozenset({"general"}),
    )
    for hidden in ("special", "finance", "support", "delivery"):
        assert hidden not in chosen.reason


# ---------------------------------------------------------------- determinism
def test_selection_is_deterministic() -> None:
    """Same inputs, same agent, every time. Otherwise a trace explains a decision the
    system would not make again."""
    args = {
        "rules": [
            SelectionRule(pattern=re.compile("invoice"), agent_id="finance", priority=1),
            SelectionRule(pattern=re.compile("question"), agent_id="support", priority=1),
        ]
    }
    first = _select("an invoice question", **args)
    for _ in range(20):
        assert _select("an invoice question", **args) == first


def test_equal_priority_rules_resolve_the_same_way_every_run() -> None:
    """Two rules of equal priority must not resolve by list order, or reordering the list
    for readability changes behaviour without anyone meaning to."""
    a = SelectionRule(pattern=re.compile("invoice"), agent_id="finance")
    b = SelectionRule(pattern=re.compile("question"), agent_id="support")
    assert _select("an invoice question", rules=[a, b]) == _select(
        "an invoice question", rules=[b, a]
    )


def test_priority_is_explicit_rather_than_positional() -> None:
    """Relying on order works until someone reorders the list."""
    low = SelectionRule(pattern=re.compile("invoice"), agent_id="finance", priority=0)
    high = SelectionRule(pattern=re.compile("invoice"), agent_id="support", priority=9)
    assert _select("an invoice question", rules=[low, high]).agent_id == "support"


def test_selection_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reasoning as lane classification: a round trip on every request, and text inside
    a retrieved document could otherwise choose which agent, and therefore which tools,
    handle the question."""
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("selection tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert _select("an invoice question").agent_id


# ---------------------------------------------------------------- the classifier
def test_the_classifier_scores_rather_than_taking_the_first_match() -> None:
    """A question mentioning one word from one area and three from another belongs to the
    second, and first-match would give it to whichever came first in a dictionary."""
    chosen = _select("the invoice mentions hosting, domain renewal and expiry")
    assert chosen.agent_id == "delivery"


def test_the_classifier_result_does_not_depend_on_mapping_order() -> None:
    one = _select("ticket about an invoice", keywords={"a": ("ticket",), "b": ("invoice",)})
    two = _select("ticket about an invoice", keywords={"b": ("invoice",), "a": ("ticket",)})
    assert one.agent_id == two.agent_id


def test_a_question_matching_nothing_gets_the_default() -> None:
    """The default is a perfectly good answer for a question the rules did not cover, and a
    classifier trying to cover everything becomes a thing nobody can predict."""
    chosen = _select("what is the weather like")
    assert chosen.stage is SelectionStage.DEFAULT


# ------------------------------------------------------------------- the record
def test_every_selection_records_which_stage_decided_and_why() -> None:
    """A trace saying which agent ran is much less useful than one saying who decided it
    would: a binding somebody set, a rule somebody wrote, or the system guessing."""
    for chosen in (
        _select(bindings=[AgentBinding(channel=Channel.LARK, agent_id="support")]),
        _select(
            "invoice", rules=[SelectionRule(pattern=re.compile("invoice"), agent_id="finance")]
        ),
        _select("a ticket question"),
        _select("what is the weather like"),
    ):
        assert chosen.stage in SelectionStage
        assert chosen.reason.strip()


def test_there_is_always_an_agent() -> None:
    """A selector that can return nothing pushes the empty case onto every caller, and the
    caller that forgets it produces a request with no agent and no error."""
    assert _select("anything at all").agent_id
