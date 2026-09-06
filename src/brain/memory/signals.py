"""What the system notices about its own answers, and what it refuses to keep about them.

Learning needs evidence, and the evidence is almost entirely negative: a question asked again
in different words, an answer somebody had to correct, a job handed to a person. Each of those
is a small statement that the system got something wrong or incomplete, and together they are
what tells it where to improve.

**A signal names what it is about and never repeats it.** This is the decision the whole
module is arranged around. The obvious shape carries the question text, because learning wants
to read it later, and that shape makes this table a second transcript of everything anybody
asked, sitting under different permissions from the conversation it came from. A signal
carries a conversation and a message id, so reading what was actually said means going back to
`chat.message`, where the conversation's own permissions apply. See
`A_SIGNAL_LOG_MUST_NOT_BECOME_A_SECOND_TRANSCRIPT`.

That is also why the detectors take the text as an argument and hand back a verdict. Deciding
whether one question restates another needs both questions, and the place both already exist
is the conversation. Nothing here stores what it was shown.

**Every signal is about an answer, never about a person.** A count of how often somebody
re-asked is a performance review assembled from a debugging tool, and it is one join away from
existing at every moment. So a signal carries the principal it happened for, because a
learning system that could not tell one person's correction from another's would learn the
average of everybody, and nothing in this module aggregates by that field. `counts_by` exists
and refuses to group by principal, which is the enforceable half.

**The signals are all captured from the first migration, which is M16.2.8 and is a decision
about time rather than about code.** Learning tiers, promotion and decay are not built. If
capture waits for them, the day they arrive there is nothing to learn from and the system
starts from zero at the moment somebody is watching. Recording from the start costs a table
and buys the first month of evidence, and the cost of the other order is a month.

**A signal is evidence and never an instruction.** Nothing here changes a memory, a rule or a
grant. `brain.memory.formation` decides what may be recalled and this decides what is worth
noticing; the tier machinery that connects them is M16.3 and does not exist. Said plainly
because a module named for learning that quietly adjusted something would be the worst kind of
surprise.

Task ids: M16.2.1, M16.2.2, M16.2.3, M16.2.4, M16.2.5, M16.2.6, M16.2.7, M16.2.8
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.gate.injection import AutonomyTier

#: Why a signal names a message rather than quoting one.
A_SIGNAL_LOG_MUST_NOT_BECOME_A_SECOND_TRANSCRIPT = (
    "A signal carries a conversation id and a message id and never the text. Carrying the "
    "text is the obvious shape, because learning wants to read it later, and it makes this "
    "a second copy of everything anybody asked, under this table's permissions rather than "
    "the conversation's. `chat.conversation` restricts a conversation to the person who had "
    "it; a signal table holding the same words would be a way to read what somebody asked "
    "without holding what it takes to read their conversation. So reading what was said "
    "means going back to the message, where that check happens."
)

#: Why nothing here aggregates by principal.
A_COUNT_PER_PERSON_IS_A_PERFORMANCE_REVIEW = (
    "Every signal is a small statement that an answer was wrong. Grouped by principal that "
    "is a ranking of who the system fails, which reads as a ranking of who asks badly, and "
    "it is one join from existing at any moment. The principal is on the row because a "
    "learning system that could not tell one person's correction from another's would learn "
    "the average of everybody. What is refused is the aggregation: `counts_by` takes a field "
    "and refuses that one."
)

#: Why capture starts before the learning that consumes it.
CAPTURE_STARTS_BEFORE_ANYTHING_LEARNS_FROM_IT = (
    "Tiers, promotion and decay are M16.3 and M16.4 and are not built. If capture waits for "
    "them, then on the day they arrive there is nothing to learn from, and the system begins "
    "learning from zero at the moment somebody is finally watching it. Recording from the "
    "first migration costs one table and buys however long it takes to build the rest. The "
    "other order costs that same time and buys nothing."
)

#: How long after an answer a re-ask still counts as the same question.
#:
#: Ten minutes. Long enough for somebody to read an answer, decide it missed, and rephrase;
#: short enough that coming back after lunch with a related question is a new question rather
#: than evidence the first answer failed. The figure is a judgement and the property behind it
#: is not: a window measured in hours turns every follow-up into a complaint about the answer
#: before it.
REASK_WINDOW = timedelta(minutes=10)

#: How long after a ticket is closed a reopen still counts as evidence about the answer.
#:
#: Two days rather than ten minutes, because a ticket closed wrongly is discovered when the
#: person it was closed on comes back, and that is a working day or two rather than a
#: conversation. Beyond it a reopen is usually a new problem on an old thread.
REOPEN_WINDOW = timedelta(days=2)

#: How much of the shorter question's meaning the longer one has to share.
#:
#: **Containment rather than a symmetric overlap, and the first draft of this module got it
#: wrong in a way worth recording.** It used a Jaccard index at 0.6, which cannot detect what
#: M16.2.1 asks for: a question re-asked *in different words* shares few words by definition,
#: so the measure and the leaf were pulling in opposite directions. "How many hours are left
#: on the Acme retainer" against "what is the remaining hours balance for Acme" scores 0.29
#: symmetric and 0.5 contained, and it is plainly the same question.
#:
#: What survives a rephrase is the subject: the proper nouns and the domain terms. What
#: changes is everything around them. So the measure asks how much of the shorter question the
#: two have in common, which is high exactly when one is a restatement of the other.
REASK_CONTAINMENT = 0.5

#: How many meaningful words two questions must actually share.
#:
#: Two, and it is the guard the ratio cannot provide. A question that reduces to one word
#: after the noise is removed is contained completely by anything mentioning that word, so
#: "Acme?" would be a re-ask of every question about Acme ever asked in the window. The ratio
#: says how much they overlap and this says whether there is enough there to be talking about.
REASK_SHARED_TERMS = 2

#: Words that carry no meaning for the comparison above.
#:
#: Short and closed. A long stop list starts deciding which words matter, which is the
#: judgement the crude comparison exists to avoid making.
_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "the",
        "to",
        "us",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
    }
)

_WORD = re.compile(r"[a-z0-9']+")


class Signal(enum.StrEnum):
    """What was noticed. One member per leaf of M16.2, and no member for anything else.

    A closed vocabulary rather than a free string, because a signal kind is read by the tier
    machinery to decide how much a piece of evidence is worth, and a kind nobody declared is
    evidence nothing knows how to weigh. `signal_gaps` refuses a set that has grown one.
    """

    #: M16.2.1. The same question again, in different words, inside the window.
    REASKED = "reasked"
    #: M16.2.2. Somebody copied the answer, which is the only positive signal here.
    COPIED = "copied"
    #: M16.2.3. A follow-up that contradicts what was just said.
    CONTRADICTED = "contradicted"
    #: M16.2.4. The conversation went to a person.
    ESCALATED = "escalated"
    #: M16.2.5. A person took over an action an agent was running at ASSISTED.
    TAKEN_OVER = "taken_over"
    #: M16.2.6. A ticket closed and reopened inside the window.
    REOPENED = "reopened"
    #: M16.2.7. An approval was refused, and the reason travels with it.
    REJECTED = "rejected"


#: The one signal here that says something went right.
#:
#: Named because a learning system fed only failures learns only what to avoid, and because a
#: reader counting the members will otherwise assume the positive case was forgotten.
POSITIVE_SIGNALS: frozenset[Signal] = frozenset({Signal.COPIED})


@dataclass(frozen=True)
class Observation:
    """One thing noticed about one answer.

    No question, no answer, no reason text and no value. `reason` on a rejection is the one
    piece of prose that would be useful here and it is deliberately absent: an approver's
    reason is written about a specific request and travels with the approval, and copying it
    into a learning log is the same second-transcript problem the module docstring describes,
    one field smaller.
    """

    signal: Signal
    #: Which conversation. The permission boundary for reading what was actually said.
    conversation_id: str
    #: Which turn. Together with the conversation, enough to go and look.
    message_id: str
    #: Who it happened for. On the row and never aggregated on. See the named constant.
    principal_id: str
    at: datetime

    def __post_init__(self) -> None:
        for name in ("conversation_id", "message_id", "principal_id"):
            if not getattr(self, name):
                msg = f"an observation with no {name} points at nothing anybody can look up"
                raise ValueError(msg)
        if self.at.tzinfo is None:
            msg = "a naive observation time compares wrongly against an aware one"
            raise ValueError(msg)


def _words(text: str) -> frozenset[str]:
    """The words that carry meaning, lowercased. Deliberately crude; see `REASK_OVERLAP`."""
    return frozenset(_WORD.findall(text.lower())) - _NOISE


def is_reask(
    earlier: str,
    later: str,
    *,
    apart: timedelta,
    window: timedelta = REASK_WINDOW,
    containment: float = REASK_CONTAINMENT,
    shared_terms: int = REASK_SHARED_TERMS,
) -> bool:
    """Whether the second question is the first one asked again in different words.

    **This detects a question about the same subject asked again soon, which is a weaker claim
    than paraphrase detection and is what the words available can support.** Doing better
    means comparing embeddings, which puts a model call on the path that exists to notice a
    model call went badly, and produces a similarity number nobody can explain to the person
    whose question it grouped. Two shared subject terms and a window is wrong sometimes in
    both directions and a person reading the signal can see exactly why, which is the property
    worth having in a learning signal.

    Takes both questions rather than storing either, which is why this is a function and not a
    table: the comparison needs the text, the text already exists in the conversation, and
    nothing has to be copied anywhere to make the decision.

    Identical questions are not a re-ask. Somebody pressing send twice, or a client retrying,
    is not evidence the answer was poor, and counting it would make every network hiccup look
    like a failed answer.

    A question with no meaningful words is a re-ask of nothing. "ok thanks" against "ok"
    contains completely once the noise is removed, which is to say on nothing.
    """
    if apart > window or apart < timedelta(0):
        return False
    first, second = _words(earlier), _words(later)
    if not first or not second:
        return False
    if earlier.strip().lower() == later.strip().lower():
        return False
    shared = first & second
    if len(shared) < shared_terms:
        return False
    return len(shared) / min(len(first), len(second)) >= containment


def is_contradiction(follow_up: str, *, markers: Iterable[str] = ()) -> bool:
    """Whether a follow-up says the answer before it was wrong.

    A marker list rather than a model, and it is wrong in both directions on purpose. What it
    buys is that a person reading a `CONTRADICTED` signal can see exactly why it fired, which
    a similarity score does not give them, and that this path does not call a model to decide
    whether a model's answer was bad.

    The default markers are the phrases people actually use, which is a different list from
    the phrases that mean disagreement in general: "that is not right" and "no, it is" are how
    somebody corrects an assistant, and "I disagree" is how they argue with a person.
    """
    lowered = follow_up.lower()
    return any(marker in lowered for marker in (tuple(markers) or CONTRADICTION_MARKERS))


#: The phrases that mean the answer before this one was wrong.
#:
#: Written out rather than inferred, and short. Every addition is somebody deciding that a
#: form of words counts as a correction, which is a judgement worth making one phrase at a
#: time in a diff rather than by widening a pattern.
CONTRADICTION_MARKERS: tuple[str, ...] = (
    "that is not right",
    "that's not right",
    "that is wrong",
    "that's wrong",
    "not correct",
    "actually it",
    "actually the",
    "no, it",
    "no it is",
)


def is_reopen(
    closed_at: datetime, reopened_at: datetime, *, window: timedelta = REOPEN_WINDOW
) -> bool:
    """Whether a ticket coming back counts as evidence about the answer that closed it.

    Bounded on both sides. A reopen before the close is a clock problem rather than a signal,
    and one beyond the window is usually a new problem arriving on an old thread, which would
    make every long-lived ticket a permanent complaint about one answer.
    """
    gap = reopened_at - closed_at
    return timedelta(0) <= gap <= window


def is_takeover(tier: AutonomyTier, *, by_a_person: bool) -> bool:
    """Whether a person stepping in counts as a takeover.

    Only at ASSISTED, and the two exclusions are the point. At SHADOW the agent proposes and a
    person acts every time, so a person acting is the design rather than a signal. At
    AUTONOMOUS a person stepping in is an intervention worth a much louder record than a
    learning signal, and treating it as one would file an incident in a table nobody reads for
    incidents.

    Delete the tier check and every shadow-mode action becomes evidence that the agent failed,
    which is the shape that would make a new install look like a broken one.
    """
    return by_a_person and tier is AutonomyTier.ASSISTED


def counts_by(observations: Sequence[Observation], field: str) -> Mapping[str, int]:
    """How many observations fall in each value of one field.

    Refuses `principal_id`, and that refusal is the enforceable half of
    `A_COUNT_PER_PERSON_IS_A_PERFORMANCE_REVIEW`. Grouped by person these rows are a ranking
    of who the system fails, which reads as a ranking of who asks badly.

    Refuses anything that is not a field of `Observation` too, rather than returning an empty
    map: a typo that silently counted nothing would be a report saying there is no evidence.
    """
    if field == "principal_id":
        msg = (
            "signals may not be counted per person: every one of them says an answer was "
            "wrong, and grouped by principal that is a ranking of who the system fails"
        )
        raise ValueError(msg)
    allowed = {"signal", "conversation_id", "message_id"}
    if field not in allowed:
        msg = f"{field!r} is not a field observations may be counted by; try {sorted(allowed)}"
        raise ValueError(msg)

    counted: dict[str, int] = {}
    for one in observations:
        key = str(getattr(one, field))
        counted[key] = counted.get(key, 0) + 1
    return counted


def signal_gaps(kinds: Iterable[Signal] = tuple(Signal)) -> tuple[str, ...]:
    """Every signal declared here that the leaves do not ask for, and the reverse.

    A closed vocabulary is only closed if something checks it. This is what makes adding a
    member a decision: a kind nobody declared is evidence the tier machinery has no weight
    for, and it will be weighed as whatever the default turns out to be.
    """
    declared = {one.value for one in kinds}
    expected = {
        "reasked",
        "copied",
        "contradicted",
        "escalated",
        "taken_over",
        "reopened",
        "rejected",
    }
    gaps: list[str] = []
    for extra in sorted(declared - expected):
        gaps.append(f"{extra} is declared and no leaf of M16.2 asks for it")
    for missing in sorted(expected - declared):
        gaps.append(f"{missing} is asked for by M16.2 and is not declared")
    return tuple(gaps)
