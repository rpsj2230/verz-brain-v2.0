"""Choosing which agent answers, in a fixed order, without asking a model.

Three stages, tried in order, and the order encodes who decided.

1. **A channel binding.** Somebody configured this chat to talk to this agent. That is an
   explicit human decision about this exact conversation and it outranks everything below.
2. **A rule.** A pattern an administrator wrote once, covering many conversations.
3. **A cheap classifier.** Nobody decided; the system is guessing, deterministically.

Each stage is more specific than the one below it, so a more specific decision always wins.
Reversing that would mean a broad rule silently overriding the binding somebody set for one
team, and the person who set it would have no way to see why it stopped working.

**Visibility is checked at every stage, and a failure to see is silent.** An agent the
caller cannot see is skipped, and selection falls through as though the binding or rule were
not there. Saying "that agent is not available to you" would confirm it exists, which is the
same mistake as a refusal that explains itself. The real reason goes in the trace.

**No model is called here.** Same reasoning as lane classification: a round trip on every
request, and text inside a retrieved document could otherwise choose which agent, and
therefore which tools, handle the question.

Task ids: M3.6.2
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from brain.gate.context import Channel


class SelectionStage(StrEnum):
    """Which stage chose, recorded so a trace can say who decided rather than what ran."""

    BINDING = "binding"
    RULE = "rule"
    CLASSIFIER = "classifier"
    DEFAULT = "default"


@dataclass(frozen=True)
class AgentBinding:
    """A conversation wired to an agent by a person.

    `conversation_id` is optional so a binding can cover a whole channel or one group chat.
    A binding naming a conversation is more specific than one naming only a channel, and
    beats it.
    """

    channel: Channel
    agent_id: str
    conversation_id: str | None = None

    @property
    def specificity(self) -> int:
        return 1 if self.conversation_id is None else 2


@dataclass(frozen=True)
class SelectionRule:
    """A pattern an administrator wrote, mapping a question shape to an agent.

    `priority` breaks ties explicitly. Relying on list order works until someone reorders
    the list for readability and changes behaviour without meaning to.
    """

    pattern: re.Pattern[str]
    agent_id: str
    priority: int = 0


@dataclass(frozen=True)
class AgentSelection:
    """Which agent, which stage chose it, and why, in words.

    The reason is written here rather than reconstructed later, for the same reason the lane
    decision is: re-deriving it means re-running a selector that may have changed since, and
    the trace would then explain a decision that was never made.
    """

    agent_id: str
    stage: SelectionStage
    reason: str


#: Entity keywords the cheap classifier knows. Deliberately small: a classifier that tries
#: to cover everything becomes a thing nobody can predict, and the default is a perfectly
#: good answer for a question that matches nothing.
CLASSIFIER_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "support": ("ticket", "tickets", "support", "sla", "complaint"),
    "finance": ("invoice", "invoiced", "billing", "payment", "overdue", "quote"),
    "delivery": ("hosting", "domain", "renewal", "expiry", "deploy", "launch"),
}


def _visible(agent_id: str, visible_agents: frozenset[str]) -> bool:
    return agent_id in visible_agents


def select_agent(
    question: str,
    channel: Channel,
    *,
    visible_agents: frozenset[str],
    default_agent: str,
    bindings: Iterable[AgentBinding] = (),
    rules: Iterable[SelectionRule] = (),
    conversation_id: str | None = None,
    keywords: Mapping[str, tuple[str, ...]] = CLASSIFIER_KEYWORDS,
) -> AgentSelection:
    """Pick an agent. Deterministic, and never a model call.

    `default_agent` is required rather than defaulted, because a selector that can return
    "no agent" pushes the empty case onto every caller, and the caller that forgets it
    produces a request with no agent and no error.
    """
    # 1. Bindings, most specific first. A conversation binding beats a channel binding, and
    # ties break on the agent id so the result cannot depend on iteration order.
    for binding in sorted(
        (b for b in bindings if b.channel is channel),
        key=lambda b: (-b.specificity, b.agent_id),
    ):
        if binding.conversation_id is not None and binding.conversation_id != conversation_id:
            continue
        if not _visible(binding.agent_id, visible_agents):
            # Skipped in silence. Saying so would confirm the agent exists.
            continue
        where = "this conversation" if binding.conversation_id else f"the {channel} channel"
        return AgentSelection(
            agent_id=binding.agent_id,
            stage=SelectionStage.BINDING,
            reason=f"{where} is bound to {binding.agent_id}",
        )

    # 2. Rules, by explicit priority then by pattern text, so two rules of equal priority
    # resolve the same way on every run.
    for rule in sorted(rules, key=lambda r: (-r.priority, r.pattern.pattern)):
        if not rule.pattern.search(question):
            continue
        if not _visible(rule.agent_id, visible_agents):
            continue
        return AgentSelection(
            agent_id=rule.agent_id,
            stage=SelectionStage.RULE,
            reason=f"matched the rule {rule.pattern.pattern!r}",
        )

    # 3. The cheap classifier. Sorted so the result does not depend on mapping order, and
    # scored rather than first-match so a question mentioning one word from one area and
    # three from another lands in the second.
    words = set(re.findall(r"[a-z]+", question.lower()))
    scored = sorted(
        ((sum(1 for k in terms if k in words), agent) for agent, terms in keywords.items()),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if scored and scored[0][0] > 0:
        score, agent_id = scored[0]
        if _visible(agent_id, visible_agents):
            return AgentSelection(
                agent_id=agent_id,
                stage=SelectionStage.CLASSIFIER,
                reason=f"{score} keyword match(es) for {agent_id}",
            )

    return AgentSelection(
        agent_id=default_agent,
        stage=SelectionStage.DEFAULT,
        reason="nothing more specific applied",
    )
