"""Deciding how much machinery a question gets, without asking a model first.

Classification runs before anything expensive, so it cannot itself be expensive. It also
cannot call a model, for two reasons the architecture states plainly: it would add a round
trip to every single request, and it would let text inside a retrieved document influence
which jurisdiction handles the question. A classifier that can be argued with is not a
classifier.

So every feature here is computable from the question and the request metadata alone.

**Admission to the fast lane is exact, and deliberately hard.** The fast lane answers with
no model in the loop at all, which means nothing downstream can notice that the question
was slightly different from the one that got answered. A fuzzy near-match produces a
confidently wrong answer with no mechanism able to catch it. So an intent matches only when
the shape matches and every required slot is present; anything less falls through to the
answer lane, where a model reads the actual words.

Falling through is cheap and being wrong is not. That asymmetry is the whole design.

Task ids: M3.6.1, M3.6.3
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from brain.core.lane import Lane


@dataclass(frozen=True)
class Intent:
    """One question shape the fast lane knows how to answer exactly.

    `pattern` is anchored at both ends on purpose. An unanchored pattern would match its
    shape *inside* a longer question, and the longer question almost always carries a
    qualifier that changes the answer: "how many hours are left on Acme" and "how many
    hours are left on Acme after the November work" are not the same question.
    """

    name: str
    pattern: re.Pattern[str]
    required_slots: tuple[str, ...]


#: The fast-lane vocabulary. Small on purpose: every entry is a promise that this exact
#: shape can be answered from the projection with no model reading it.
INTENTS: tuple[Intent, ...] = (
    Intent(
        "client_hours_remaining",
        re.compile(
            r"^\s*(?:how many\s+)?hours?\s+(?:are\s+)?(?:left|remaining)\s+(?:on|for)\s+"
            r"(?P<client>[\w &.'-]{2,60})\s*\??\s*$",
            re.I,
        ),
        ("client",),
    ),
    Intent(
        "client_hosting_expiry",
        re.compile(
            r"^\s*when\s+does\s+(?P<client>[\w &.'-]{2,60})(?:'s)?\s+hosting\s+expire\s*\??\s*$",
            re.I,
        ),
        ("client",),
    ),
    Intent(
        "ticket_status",
        re.compile(
            r"^\s*(?:what(?:'s| is)\s+the\s+)?status\s+of\s+ticket\s+"
            r"(?P<ticket>\d{1,10})\s*\??\s*$",
            re.I,
        ),
        ("ticket",),
    ),
    Intent(
        "client_account_manager",
        re.compile(
            r"^\s*who\s+(?:is|manages|handles)\s+(?:the\s+account\s+manager\s+for\s+)?"
            r"(?P<client>[\w &.'-]{2,60})\s*\??\s*$",
            re.I,
        ),
        ("client",),
    ),
)


@dataclass(frozen=True)
class IntentMatch:
    intent: str
    slots: dict[str, str]


#: Words that turn a name into a condition. A slot containing one of these has swallowed
#: the qualifier rather than matched a name.
#:
#: Found by a test, not by inspection. Anchoring the pattern at both ends is necessary and
#: not sufficient: "how many hours are left on Acme after the November work" matched
#: `client_hours_remaining` perfectly well, because the client slot accepts spaces and so
#: absorbed "after the November work" into the name. The fast lane would then have looked
#: up a client by that string, and a lookup that fuzzy-matches would have answered the
#: narrower question with the broader answer.
#:
#: This costs real names. "Smith and Jones Pte Ltd" contains "and" and will fall through to
#: the answer lane. That is the correct trade under the asymmetry this module is built on:
#: a fall-through is one model call, and a wrong answer in a lane with no model has nothing
#: downstream able to catch it.
QUALIFIER_WORDS: frozenset[str] = frozenset(
    {
        "after",
        "before",
        "since",
        "until",
        "between",
        "during",
        "excluding",
        "except",
        "including",
        "besides",
        "apart",
        "aside",
        "and",
        "or",
        "plus",
        "minus",
        "with",
        "without",
        "unless",
        "if",
        "when",
        "where",
        "than",
        "but",
        "only",
        "also",
    }
)

#: A name longer than this is a phrase. Verz's longest client names run to four words plus
#: a suffix; anything beyond is carrying something that is not a name.
MAX_SLOT_WORDS = 6


def is_a_name_not_a_phrase(value: str) -> bool:
    """Whether a slot value is a name rather than a name with a condition stuck to it.

    Public because `brain.gate.fast_lane` matches data-driven rules against the same shapes
    and has to apply the same rule. A second copy would be a second answer to "is this a
    name", and the copy that drifts is the one that lets a qualifier through in the lane
    where no model is reading.
    """
    words = value.split()
    if not words or len(words) > MAX_SLOT_WORDS:
        return False
    return not any(w.strip(".,'").lower() in QUALIFIER_WORDS for w in words)


def match_intent(question: str) -> IntentMatch | None:
    """The exact match, or nothing. There is no partial credit here by design."""
    for intent in INTENTS:
        found = intent.pattern.match(question)
        if found is None:
            continue
        slots = {k: v.strip() for k, v in found.groupdict().items() if v is not None}
        # A shape that matched but left a required slot empty is precisely the near-miss
        # the fast lane must not answer.
        if not all(slots.get(slot) for slot in intent.required_slots):
            continue
        # And a slot that matched a whole phrase is the other near-miss, which anchoring
        # does not catch because the phrase is inside the slot rather than outside it.
        if not all(is_a_name_not_a_phrase(slots[slot]) for slot in intent.required_slots):
            continue
        return IntentMatch(intent=intent.name, slots=slots)
    return None


#: Phrases where the asker has said, in words, that this is a piece of work rather than a
#: question. Cheap to detect and worth honouring: someone who writes "go through every
#: client" has told us the shape of what they want.
TASK_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(go through|work through|for (?:each|every))\b", re.I),
    re.compile(
        r"\b(draft|write|prepare|compile|generate)\b.{0,30}\b(report|summary|list|deck)\b", re.I
    ),
    re.compile(r"\b(and then|after that|once (?:you|that))\b", re.I),
    re.compile(r"\b(cross[- ]?reference|reconcile|compare)\b", re.I),
)

#: Above this many words, a question is doing more than one thing whatever it says.
TASK_WORD_COUNT = 60


@dataclass(frozen=True)
class LaneDecision:
    """The lane, and the reason, recorded rather than inferred afterwards.

    M3.6.3. The reason is written at the moment of the decision because reconstructing it
    later means re-running a classifier that may have changed since, and the trace would
    then explain a decision that was never made.
    """

    lane: Lane
    reason: str
    intent: IntentMatch | None = None


def classify_lane(question: str, *, requested: Lane | None = None) -> LaneDecision:
    """Pick the lane from features available without a model.

    Order matters. An explicit request for the task lane is honoured, because someone
    asking for deep work knows more about what they want than a word count does. An
    explicit request for the *fast* lane is not honoured on its own: the fast lane is not a
    speed preference, it is a claim that the question is one of a small closed set, and
    that claim is checked rather than accepted.
    """
    if requested is Lane.TASK:
        return LaneDecision(Lane.TASK, "the asker requested the task lane")

    intent = match_intent(question)
    if intent is not None:
        return LaneDecision(
            Lane.FAST, f"exact intent match on {intent.intent} with every slot present", intent
        )

    if requested is Lane.FAST:
        # Refused, and the reason is recorded so the trace shows the request was seen. A
        # silently ignored preference looks like a bug to whoever asked for it.
        return LaneDecision(
            Lane.ANSWER,
            "fast lane requested but the question matches no exact intent, so a model reads it",
        )

    if len(question.split()) > TASK_WORD_COUNT:
        return LaneDecision(Lane.TASK, f"more than {TASK_WORD_COUNT} words")

    for phrase in TASK_PHRASES:
        if phrase.search(question):
            return LaneDecision(Lane.TASK, "the question describes multi-step work")

    return LaneDecision(Lane.ANSWER, "a person is waiting and a model reads the question")
