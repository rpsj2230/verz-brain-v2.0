"""Declining to answer, and handing the question to a person.

"I do not know" and "I will not" are answers. This module is where they are produced, and
the first design decision in it is that they are not errors. `brain.core.errors` has five
outcomes and none of them fits: an abstention is not DENIED, not ABSENT, not UNRESOLVED,
not DEGRADED and certainly not FAILED. Modelling it as one of those puts every honest
refusal on the same chart as an outage, which is how a metric gets a target, and the target
is met by answering questions the system should have declined.

**What breaks without it.** The system answers everything. Given a question it cannot
ground, a language model produces a fluent paragraph rather than a refusal, because that is
what the objective rewards; and the paragraph is about a company whose data the reader
believes it read. There is no way to bolt this on afterwards, because by then every
downstream surface has been built to expect prose.

Six properties hold this together, and each exists because the alternative fails quietly.

**An abstention never says the record exists.** DENIED and ABSENT are one event to a person
(`brain.core.errors`), and the redactor makes them one event in the data by dropping a
record rather than returning an empty husk of it. Abstention is the third place that
distinction could leak and it is the easiest one to leak from, because a helpful system
wants to explain itself. So the reason lives in the trace, `AbstentionNotice` has nowhere to
put it, and `NOT_ENTITLED` and `NOTHING_RETRIEVED` share one string by identity rather than
by two literals that agree today.

**Nothing in the classifier can produce `NOT_ENTITLED`.** `abstention_for_search` reads the
post-redaction payload, where a record the asker may not see is simply not present, so a
refusal arrives at the nothing-found branch by construction. The enum member exists for the
audit ledger, raised by the layer that actually saw a `Denied`. That absence is the
mechanism; a branch there would be the leak.

**A claim without a citation is not an answer.** `brain.gate.provenance` says what stands
behind an answer, and where nothing does, the honest output is an abstention rather than a
sentence. Configurable per agent (M8.2.4), defaulting to required, on the same default-deny
principle as an unclassified field.

**Degraded is not abstention.** A source being unreachable is a partial answer that says
what is missing, and it stays in the error taxonomy where `Degraded` already promises we
never substitute a stale value. There is deliberately no constructor here for it. Conflating
the two makes an outage read as a refusal, and then nobody fixes the outage.

**A content-policy refusal lands here rather than on the next model.** Decided 5 September;
the rule itself is `brain.models.routing.CONTENT_POLICY_REFUSAL_IS_NOT_A_TRIGGER`. A refusal
is a property of the request, so the next rung reproduces it at full cost, and where a
different provider does answer, the chain has shopped until something said yes. The company
then has an answer that depends on which model happened to be up. So the system says "I will
not answer that" once, honestly, and records it.

**Escalation names a route, never a person's availability.** "Ask Wei Ling, she is online"
leaks a presence signal and a reporting line to whoever forwards the message. "This needs a
person from maintenance" does not, and it is the sentence that actually gets the work done.

Scope: domain logic. Nothing here sends a message, writes a row, calls a model or reads a
clock; `now` is always a parameter.

Task ids: M8.2.1, M8.2.2, M8.2.3, M8.2.4, M8.3.1, M8.3.2, M8.3.3, M8.3.4, M8.3.5
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Self

from brain.core.redaction import ChannelPayload
from brain.gate.context import Channel
from brain.gate.injection import AutonomyTier
from brain.gate.provenance import Provenance, payload_is_empty

# --------------------------------------------------------------------- grammars

#: A queue, a step name, or an identifier. The same shape as `brain.gate.leash.IDENTIFIER`,
#: because a route that can be escalated to has to be a route that can be audited.
_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")

#: What "what was tried" may contain: a step or tool name, never a sentence. See `Handoff`.
_STEP_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$")


# --------------------------------------------------------- the states (M8.2.1)


class AbstentionReason(enum.StrEnum):
    """Why the system did not answer. Recorded; never rendered.

    The first four are the architecture's four distinct "I don't know" states. The fifth is
    "I will not", which is a different sentence and belongs here rather than in the fallback
    chain (see the module docstring, and the 5 September decision).

    None of these values collides with a `brain.core.errors.Outcome`, deliberately and
    checked in the invariant suite. Two vocabularies sharing a value is how an abstention
    ends up counted as an outage on a dashboard that groups by string.
    """

    #: The search ran and returned nothing this asker may see. Covers the case where a
    #: record existed and was withheld, and it is meant to: that is the whole point.
    NOTHING_RETRIEVED = "nothing retrieved"
    #: Records came back and none of them bears on the question.
    RETRIEVED_BUT_NOT_ANSWERING = "retrieved but not answering"
    #: No connector is configured that could reach the answer. A fact about this company's
    #: setup, identical for everybody, which is why it is safe to say and useful to hear.
    NOTHING_CONNECTED = "nothing connected"
    #: A refusal was seen at a layer that knew it was a refusal. Its public half is byte
    #: identical to NOTHING_RETRIEVED, and there is no branch below that produces it.
    NOT_ENTITLED = "not entitled"
    #: The model declined on content grounds, or an authored policy did. "I will not."
    REFUSED = "refused"


#: The one sentence shared by the two states that must not be told apart (M8.2.3).
#:
#: One constant referenced twice rather than two literals that happen to match, so the
#: property is checked by identity in the invariant suite. Two literals agree until somebody
#: improves the wording of one of them, and the improvement is a permission leak in a diff
#: that reads as copy editing.
#:
#: The wording matches `brain.core.errors.Denied.public_message` for the same reason.
NOT_FOUND_TEXT: Final = "I could not find that."

NOTHING_CONNECTED_TEXT: Final = (
    "Nothing I can reach is connected to that yet, so I have not guessed."
)
NOT_ANSWERING_TEXT: Final = "I found related records, and none of them answers that."
REFUSED_TEXT: Final = "I will not answer that."


#: Every reason's public half. A mapping rather than a method with a match, because the
#: property that matters is that two keys share one value, and that is visible here and
#: would be buried in a function body.
PUBLIC_TEXT: Mapping[AbstentionReason, str] = MappingProxyType(
    {
        AbstentionReason.NOTHING_RETRIEVED: NOT_FOUND_TEXT,
        AbstentionReason.NOT_ENTITLED: NOT_FOUND_TEXT,
        AbstentionReason.RETRIEVED_BUT_NOT_ANSWERING: NOT_ANSWERING_TEXT,
        AbstentionReason.NOTHING_CONNECTED: NOTHING_CONNECTED_TEXT,
        AbstentionReason.REFUSED: REFUSED_TEXT,
    }
)


# ------------------------------------------------- the scope statement (M8.2.2)


@dataclass(frozen=True)
class SearchScope:
    """What an answer covered, in terms the asker was already entitled to know.

    **Derived from the asker's reach, never from what the search actually did**, and that
    inversion is the whole design. A statement assembled from the sources that ran would
    vary with whether a source was healthy, whether a record existed and whether this
    particular person was refused, and every one of those variations is readable by asking
    the same question twice. Derived from reach, it is the same sentence every time for the
    same person, which is what makes it safe to show beside a refusal at all.

    That resolves what looks like a contradiction between M8.2.2, which wants a scope
    statement naming what was searched, and `brain.core.redaction.ChannelPayload`, which
    suppresses the source name when nothing survives precisely so that the set of sources a
    person cannot reach is not enumerable. Both hold, because this names the sources they
    can reach and says nothing about which of them held anything.

    So the wording is "this covers", not "I searched". Where a source in reach was
    unreachable, that is a `Degraded` answer saying what is missing, and it is not this.
    """

    covered: tuple[str, ...] = ()

    def render(self) -> str:
        """The statement, or nothing at all when there is nothing to say.

        An asker with no reachable source gets no sentence rather than "this covers
        nothing". The empty string is honest and the alternative is a sentence about their
        own account that reads, in the moment they were refused, as an explanation of the
        refusal.
        """
        if not self.covered:
            return ""
        if len(self.covered) == 1:
            return f"This covers {self.covered[0]}."
        listed = ", ".join(self.covered[:-1])
        return f"This covers {listed} and {self.covered[-1]}."


def scope_of_reach(admissible: Iterable[str]) -> SearchScope:
    """Build a scope statement from the sources this asker may be told about.

    It takes what the asker can reach and nothing else. There is deliberately no second
    parameter for "what actually ran": a signature that accepted one would let a caller
    intersect the two and reintroduce, in one line at a call site, the outcome-dependence
    this type exists to remove.

    Sorted and deduplicated, so the order sources happen to be configured in cannot be read
    back out of the sentence.
    """
    return SearchScope(covered=tuple(sorted({name for name in admissible if name})))


# ------------------------------------------------------- the outcome (M8.2.3)


@dataclass(frozen=True)
class AbstentionNotice:
    """What a person receives when the system declines.

    **There is no reason field on this type**, and that is the mechanism rather than an
    omission, in the same way as `brain.core.access_route.AskerAcknowledgement` having no
    fields at all. A notice that carried why could be rendered by a channel adapter trying
    to be helpful, and the day it is, DENIED and ABSENT stop being one event. Two notices
    built from the two indistinguishable reasons are equal objects, which a test can assert
    in one line.
    """

    text: str
    scope: SearchScope = SearchScope()

    def render(self) -> str:
        statement = self.scope.render()
        return f"{self.text} {statement}" if statement else self.text


@dataclass(frozen=True)
class Abstention:
    """The internal half: the notice, plus why, for the trace and the ledger.

    Not an exception, and not a subclass of `brain.core.errors.BrainError`. An abstention is
    an outcome the system is supposed to produce, so raising it would put it on the same
    path as a failure, get it caught by the same handlers, and land it in the same counters.

    `detail` follows the redaction rule the rest of the system follows: names and reasons,
    never values. It is read by an auditor and never rendered to the asker.
    """

    reason: AbstentionReason
    scope: SearchScope = SearchScope()
    detail: str = ""

    def for_asker(self) -> AbstentionNotice:
        """The only thing that may be said to the person who asked."""
        return AbstentionNotice(text=PUBLIC_TEXT[self.reason], scope=self.scope)


def nothing_retrieved(scope: SearchScope, *, detail: str = "") -> Abstention:
    return Abstention(reason=AbstentionReason.NOTHING_RETRIEVED, scope=scope, detail=detail)


def not_entitled(scope: SearchScope, *, detail: str = "") -> Abstention:
    """The audit-side reason for a refusal, raised by the layer that saw the `Denied`.

    Its public half is `NOT_FOUND_TEXT`, the same object `nothing_retrieved` produces. This
    constructor exists so the ledger can record what actually happened, which is the same
    division of labour `brain.core.errors` describes: DENIED exists only for the audit log.
    """
    return Abstention(reason=AbstentionReason.NOT_ENTITLED, scope=scope, detail=detail)


def nothing_connected(scope: SearchScope, *, detail: str = "") -> Abstention:
    return Abstention(reason=AbstentionReason.NOTHING_CONNECTED, scope=scope, detail=detail)


def retrieved_but_not_answering(scope: SearchScope, *, detail: str = "") -> Abstention:
    return Abstention(
        reason=AbstentionReason.RETRIEVED_BUT_NOT_ANSWERING, scope=scope, detail=detail
    )


def refused(scope: SearchScope, *, detail: str = "") -> Abstention:
    """ "I will not." The landing place for a content-policy refusal (5 September).

    Reached from two shapes, both of which the model layer already distinguishes: a
    completion whose finish reason is a refusal (`brain.models.adapter.is_refusal`), and a
    `ContentPolicyRefusedError`, which `failure_from` deliberately strips of its status so
    it cannot borrow a fallback trigger from it.

    That mapping is done by the caller rather than imported here, on purpose. The gate does
    not depend on the provider layer anywhere else, and a refusal is a fact about the
    request by the time it reaches this module, not a fact about a provider.
    """
    return Abstention(reason=AbstentionReason.REFUSED, scope=scope, detail=detail)


def abstention_for_search(
    payload: ChannelPayload,
    *,
    scope: SearchScope,
    sources_connected: bool,
    answers_the_question: bool,
) -> Abstention | None:
    """Classify one search's outcome, or None when there is an answer to give.

    **It takes the post-redaction payload, and that is the enforcement of M8.2.3.** A record
    the asker may not see is not in this payload, so it reaches the nothing-retrieved branch
    by the same route as a record that never existed. There is no branch here that returns
    `NOT_ENTITLED`, and there is nowhere in this signature to put a pre-redaction count that
    would let one be written; the difference between the two counts is exactly the fact that
    must never be observable.

    Ordering is meaning. Nothing-connected is checked first because it is a fact about the
    company's configuration rather than about this asker or this record: reporting it as
    nothing-found would send somebody hunting for a record in a system nobody has connected.

    `answers_the_question` is a judgement, and it is admitted here because it can only ever
    cause an abstention. It has no path to a retry: quality is not a fallback trigger
    (`brain.models.routing.QUALITY_FALLBACK_REJECTED`), and the difference between the two
    is that this one terminates.
    """
    if not sources_connected:
        return nothing_connected(scope)
    if payload_is_empty(payload):
        return nothing_retrieved(scope)
    if not answers_the_question:
        return retrieved_but_not_answering(scope)
    return None


# ------------------------------------------------ the citation rule (M8.2.4)


@dataclass(frozen=True)
class CitationPolicy:
    """Whether this agent may state a claim nothing stands behind. Per agent (M8.2.4).

    Defaults to requiring a citation, on the same default-deny principle as an unclassified
    field: an agent nobody has configured is an agent nobody has thought about, and the
    honest behaviour there is to decline rather than to assert.

    A boolean rather than a threshold. "At least two citations" sounds stronger and is a
    number somebody tunes downward the first week it blocks a good answer, whereas "none at
    all" is a fact about the answer that cannot be argued with.
    """

    require_citation: bool = True


#: The default an agent gets by saying nothing.
REQUIRE_CITATION: Final = CitationPolicy()


def abstain_if_uncited(
    provenance: Provenance, *, scope: SearchScope, policy: CitationPolicy = REQUIRE_CITATION
) -> Abstention | None:
    """Refuse to state a claim with nothing behind it (M8.2.4).

    The reason is `NOTHING_RETRIEVED`, which is the same public sentence as a refusal, and
    that is correct rather than convenient: an answer with no citation is an answer with no
    record behind it, and whether the record was absent or withheld is precisely the
    question this system does not answer.
    """
    if policy.require_citation and provenance.is_empty:
        return nothing_retrieved(scope, detail="no citation survived to stand behind a claim")
    return None


# ------------------------------------------------------- escalation (M8.3.x)


class EscalationTrigger(enum.StrEnum):
    """What may raise an escalation. A closed set, in the shape of `FallbackTrigger`.

    Note what has no member: the model deciding a person should be involved. Escalation is
    an authored step (M8.3.1), which means a skill author wrote "if this, ask a person" and
    an operator can read the procedure and predict when it fires. A model-judged escalation
    is unpredictable in both directions at once: it fires on a question somebody could have
    answered, and it stays quiet on the one that mattered, and there is no configuration
    anywhere that would have changed either.
    """

    #: The skill's procedure reached a step that says "hand this to a person".
    AUTHORED_STEP = "authored step"
    #: The system abstained, and the skill declares an escalation for that outcome.
    ABSTENTION = "abstention"
    #: The leash put this action on ASSISTED, so a person renders the verdict.
    APPROVAL_REQUIRED = "approval required"


#: Why there is no MODEL_JUDGEMENT member, kept as a constant so the argument survives the
#: next person who finds the enum restrictive. Same device as
#: `brain.models.routing.QUALITY_FALLBACK_REJECTED`.
MODEL_JUDGEMENT_IS_NOT_A_TRIGGER: Final = (
    "A model deciding to involve a person is not an escalation trigger. Escalation is an "
    "authored step, so that an operator can read a procedure and say when it fires. A "
    "model-judged one fires on the easy question and stays quiet on the hard one, and no "
    "configuration anywhere would have changed either."
)


class EscalationState(enum.StrEnum):
    """Where an escalation is, as a function of the clock alone.

    Two members, not three. Whether somebody answered is recorded by the layer that receives
    the reply; it is not derivable from a timestamp, and a member here that only that layer
    could ever set would make this look like a state machine it is not.
    """

    OPEN = "open"
    EXPIRED = "expired"


@dataclass(frozen=True)
class EscalationRoute:
    """Where an escalation goes: a named queue, and the channel that queue reads (M8.3.3).

    A queue rather than a person. Routing to an individual means routing to whoever is on
    leave, and it puts a reporting line into a message that gets forwarded. A queue is also
    the only shape that can be answered by whoever is actually free without anybody having
    to reveal who that is.

    `address` is the delivery handle for the channel and is never shown to the asker; the
    two halves are kept apart here in the same shape as
    `brain.core.access_route.RoutedRequest`, where the owner's id travels beside the notice
    rather than inside it.

    The limit worth stating: nothing here can stop somebody configuring a queue named after
    a person. A queue name and a principal id are the same grammar, so the type cannot tell
    them apart. What it can do is make that configuration read as wrong wherever it surfaces,
    which is why the notice template is "a person from {queue}".
    """

    queue: str
    channel: Channel
    address: str = ""

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.match(self.queue):
            msg = f"queue {self.queue!r} is not a route name"
            raise ValueError(msg)


@dataclass(frozen=True)
class Handoff:
    """What the person picking this up is given: who asked, what, what was tried, what is
    needed (M8.3.2).

    The asker's own question, in their own words, for the reason
    `brain.core.redaction.OwnerNotice` gives about its own: a request stripped to a
    capability name is a request nobody can judge. The person is deciding whether to spend
    twenty minutes on this, and the question is the only thing that tells them.

    `tried` is step and tool names, enforced rather than trusted. Free text there becomes
    "fetched SNM's contract value = 240000" within a month, and a handoff crosses an
    entitlement boundary: the person picking it up may hold less than the asker, and a
    payload that carried values would hand them data the gate refused them.
    """

    asker_id: str
    question: str
    #: What was attempted, as names. Ordered, because the order is the evidence.
    tried: tuple[str, ...] = ()
    #: What would unblock this, in a sentence an operator wrote.
    needed: str = ""
    #: The trace, quotable and carrying no authority. See `compose.ComposedAnswer`.
    trace_ref: str = ""

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.match(self.asker_id):
            msg = f"asker id {self.asker_id!r} is not an identifier"
            raise ValueError(msg)
        if not self.question.strip():
            msg = "a handoff with no question is one nobody can act on"
            raise ValueError(msg)
        bad = [step for step in self.tried if not _STEP_RE.match(step)]
        if bad:
            msg = (
                f"what was tried must be step names, not prose: {bad}; free text here "
                "carries values across an entitlement boundary the gate just enforced"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class EscalationNotice:
    """What the asker is told. A route, and nothing about a person (M8.3.3).

    One field, and no way to name who, whether they are online, when they last replied or
    how senior they are. Presence is the leak worth being explicit about: it is not
    protected by any capability in the system, it changes minute to minute, and "she is
    online" in a forwarded message is a fact about somebody's working day that they never
    published.
    """

    text: str


#: The two sentences an asker ever sees about an escalation. Templates over one field, so
#: there is no path from an escalation's internals into what a person reads.
ESCALATION_TEXT: Final = "This needs a person from {queue}."
ESCALATION_EXPIRED_TEXT: Final = "Nobody from {queue} has picked this up, so it is unanswered."

#: How long an escalation stays open by default. Within one working day, because the asker
#: is waiting on an answer they were told was coming; anything longer is a promise the
#: system cannot keep and does not admit to breaking.
DEFAULT_ESCALATION_TTL: Final = timedelta(hours=4)


@dataclass(frozen=True)
class Escalation:
    """One question handed to a queue, with an expiry on it (M8.3.1 to M8.3.4).

    Expiry is mandatory and there is no value meaning "never". An escalation with no expiry
    is a question that silently stops existing: the asker was told a person would look, the
    queue never picked it up, and nothing anywhere turns that into an event. The timeout is
    what makes the failure visible, which is the only reason to have one.
    """

    trigger: EscalationTrigger
    route: EscalationRoute
    handoff: Handoff
    raised_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name, value in (("raised_at", self.raised_at), ("expires_at", self.expires_at)):
            if value.tzinfo is None:
                # The same rule as `brain.gate.leash.ActionRecord`: a naive timestamp is a
                # silent bug, and here it would expire an escalation eight hours early or
                # late depending on which machine wrote it.
                msg = f"{name} must be timezone-aware; a naive timestamp expires at the wrong time"
                raise ValueError(msg)
        if self.expires_at <= self.raised_at:
            msg = (
                f"expires_at {self.expires_at.isoformat()} is not after raised_at "
                f"{self.raised_at.isoformat()}; an escalation that is born expired is never seen"
            )
            raise ValueError(msg)

    def state_at(self, now: datetime) -> EscalationState:
        if now.tzinfo is None:
            msg = "now must be timezone-aware; an escalation would expire at the wrong time"
            raise ValueError(msg)
        return EscalationState.EXPIRED if now >= self.expires_at else EscalationState.OPEN

    def for_asker(self) -> EscalationNotice:
        return EscalationNotice(text=ESCALATION_TEXT.format(queue=self.route.queue))

    def expiry_notice(self, now: datetime) -> EscalationNotice | None:
        """What the asker is told once the deadline passes, or None while it has not.

        A notice rather than an answer, and deliberately not a new abstention reason. The
        abstention that caused the escalation already stands and the asker already has it;
        what expiry adds is that the route did not deliver, which names the queue and
        nothing else. Manufacturing an answer here, or quietly downgrading to a different
        reason, would turn "nobody replied" into "there was nothing to find".
        """
        if self.state_at(now) is EscalationState.OPEN:
            return None
        return EscalationNotice(text=ESCALATION_EXPIRED_TEXT.format(queue=self.route.queue))


def raise_escalation(
    *,
    trigger: EscalationTrigger,
    route: EscalationRoute,
    handoff: Handoff,
    now: datetime,
    ttl: timedelta = DEFAULT_ESCALATION_TTL,
) -> Escalation:
    """Open an escalation with an expiry already on it.

    A helper so the expiry cannot be forgotten at a call site, in the same spirit as
    `brain.gate.context.open_trace` entering the RECORD step by construction. Passing a
    non-positive ttl is refused by `Escalation` rather than clamped here: a caller who meant
    "no timeout" should be told this system does not have one.
    """
    return Escalation(
        trigger=trigger,
        route=route,
        handoff=handoff,
        raised_at=now,
        expires_at=now + ttl,
    )


# ------------------------------------------- the takeover signal (M8.3.5)

#: Consecutive-in-window takeovers before the rung comes down. Three, matching
#: `BREAKER_CONSECUTIVE_FAILURES`: one takeover is a person preferring their own wording,
#: two is a coincidence, three on the same target is the leash being wrong.
TAKEOVER_DEMOTION_THRESHOLD: Final = 3

#: How far back a takeover counts. A week rather than a rolling count, because an agent
#: taken over three times in March and never since is an agent that was fixed, and a
#: counter with no window keeps punishing it forever.
TAKEOVER_WINDOW: Final = timedelta(days=7)

#: Where this signal must not go, kept as a constant because the leaf's wording invites it.
#:
#: A human takeover is a judgement about an answer, which is exactly what the provider
#: breaker's trigger set is closed against
#: (`brain.models.routing.QUALITY_FALLBACK_REJECTED`). Feeding it there would take a healthy
#: model out of rotation for every request in the company because one agent's leash was set
#: too long on one target, and the trace would then say "the providers were struggling" when
#: what happened is that somebody rewrote a draft.
TAKEOVER_IS_NOT_A_PROVIDER_FAULT: Final = (
    "A human takeover is a fact about one agent on one target, not about a provider. It "
    "feeds the autonomy breaker here, which lowers that agent's rung. It must never reach "
    "brain.models.routing.CircuitBreaker: that set is closed against judgements about "
    "content, and opening it would remove a healthy model from rotation for everybody."
)


@dataclass(frozen=True)
class TakeoverSignal:
    """A person took over an action the agent had been trusted to do (M8.3.5).

    Carries no principal id. Who took over is in the ledger, which is where an accountable
    record belongs; here it would be a per-person performance counter growing inside a
    safety mechanism, and the first use of it would not be safety.
    """

    agent_id: str
    target: str
    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            msg = "at must be timezone-aware; a naive timestamp lands in the wrong window"
            raise ValueError(msg)


@dataclass(frozen=True)
class AutonomyBreaker:
    """One agent's standing on one target, as a pure state machine (M8.3.5).

    Per (agent, target), like the leash itself, because trust earned updating a ticket is
    not trust to send an invoice. Frozen, with every transition returning a new instance,
    and `now` always a parameter: the same discipline `brain.models.routing.CircuitBreaker`
    is written with, and for the same reason, since the transition worth testing is the one
    where the window empties.

    It only ever tightens. There is no `record_success` here that would raise the rung,
    because a run nobody took over is not evidence that a person would have approved it,
    only that nobody was watching. Rungs go up when an operator raises them.
    """

    agent_id: str
    target: str
    #: When each takeover in the window happened, oldest first.
    takeovers: tuple[datetime, ...] = ()

    def record(self, signal: TakeoverSignal, *, window: timedelta = TAKEOVER_WINDOW) -> Self:
        """Add one takeover and drop everything that has aged out.

        Refuses a signal for another agent or target rather than absorbing it. A breaker
        that silently accepted a foreign signal would demote the wrong agent, and the
        demotion looks in the console exactly like a correct one.
        """
        if signal.agent_id != self.agent_id or signal.target != self.target:
            msg = (
                f"signal for {signal.agent_id}/{signal.target} given to the breaker for "
                f"{self.agent_id}/{self.target}; it would demote the wrong agent"
            )
            raise ValueError(msg)
        cutoff = signal.at - window
        kept = tuple(sorted([*(t for t in self.takeovers if t > cutoff), signal.at]))
        return type(self)(agent_id=self.agent_id, target=self.target, takeovers=kept)

    def recent(self, now: datetime, *, window: timedelta = TAKEOVER_WINDOW) -> int:
        if now.tzinfo is None:
            msg = "now must be timezone-aware; the window would be measured from nowhere"
            raise ValueError(msg)
        cutoff = now - window
        return sum(1 for t in self.takeovers if t > cutoff)

    def is_open(
        self,
        now: datetime,
        *,
        window: timedelta = TAKEOVER_WINDOW,
        threshold: int = TAKEOVER_DEMOTION_THRESHOLD,
    ) -> bool:
        return self.recent(now, window=window) >= threshold

    def rung(
        self,
        ceiling: AutonomyTier,
        now: datetime,
        *,
        window: timedelta = TAKEOVER_WINDOW,
        threshold: int = TAKEOVER_DEMOTION_THRESHOLD,
    ) -> AutonomyTier:
        """The rung this agent should run at on this target: the ceiling, or one below it.

        One step rather than straight to SHADOW. Three takeovers of an autonomous action
        mean a person should see it before it happens, which is ASSISTED; jumping to SHADOW
        would stop the work entirely, and a control that stops the work is a control an
        operator switches off. It never goes below SHADOW, which is already the fail-closed
        rung `brain.gate.leash` returns for an agent nobody has configured.
        """
        if not self.is_open(now, window=window, threshold=threshold):
            return ceiling
        return AutonomyTier(max(AutonomyTier.SHADOW, ceiling - 1))
