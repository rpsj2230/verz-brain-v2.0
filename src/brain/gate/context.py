"""What a request carries through the gate, and the order the gate does things in.

Every question, from every channel, becomes one of these and travels through the same
ordered steps. The value of that is not tidiness: it is that there is exactly one place
where entitlements are resolved, one place where the catalogue is projected, and one place
where redaction happens. A second path to a channel is a second chance to skip one of them.

Three properties are load-bearing here.

**The recorder is built before we know who is asking.** If the trace were constructed after
identification, a request that failed to identify would leave nothing behind, and the
failures most worth investigating are exactly the ones where identification went wrong.

**Traffic class has no default.** A new channel cannot compile until it declares what kind
of traffic it produces, because the honest answer for a channel nobody has thought about is
not "interactive" but "unknown", and a default would quietly pick one.

**The steps are ordered and the order is checked.** Skipping a step is not a performance
optimisation, it is a permission bypass, and it does not look like one in a diff.

Task ids: M3.1.1, M3.1.2, M3.1.3, M3.1.4
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import assert_never

from brain.core.entitlement import EntitlementSet
from brain.core.lane import Lane
from brain.core.principal import Principal


class Channel(enum.StrEnum):
    """Where a request came from. Every member must appear in `traffic_class_for`."""

    CONSOLE = "console"
    LARK = "lark"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    #: A consumer chat application reached over a bot webhook. Separate from WhatsApp even
    #: though both are chat on a personal handset, because the two differ on what a message
    #: can arrive from: a WhatsApp message reaches us from one person's account, while a
    #: Telegram bot also receives messages from groups it was added to, whose membership it
    #: cannot enumerate. See `brain.channels.telegram`.
    TELEGRAM = "telegram"
    API = "api"
    SCHEDULER = "scheduler"
    WEBHOOK = "webhook"
    #: A chat widget embedded on a client's public website. Separate from API even though
    #: both arrive over HTTP, because the two differ on the only question this enum exists to
    #: answer: whether a person is waiting. `Channel.API` is AUTOMATION, and describing a
    #: visitor watching a cursor blink as automation makes every degradation decision about
    #: them wrong in the direction of queueing an answer nobody will come back for.
    WIDGET = "widget"
    #: A Slack workspace. Separate from LARK rather than folded into a "chat" member,
    #: because the two answer `gate.admission.CHANNEL_VERBS` differently: Lark is the tenant
    #: identity provider's own client and Slack is a workspace whose membership is
    #: maintained beside the directory. See `brain.channels.slack`.
    SLACK = "slack"


class TrafficClass(enum.StrEnum):
    """What kind of traffic this is, which decides what the system may do when it struggles.

    The distinction that matters is not the transport but whether a person is waiting. A
    person waiting should be told the truth quickly, including "I could not reach Xero".
    Nobody waiting means the work can be retried, queued or run slowly, and degrading
    silently is the wrong answer because no one is there to notice.
    """

    #: A person is watching a cursor blink. Degrade visibly, never queue.
    HUMAN_INTERACTIVE = "human_interactive"
    #: A person asked and will read the reply later. Queue rather than degrade.
    HUMAN_ASYNC = "human_async"
    #: No person in the loop. Retry, back off, and alert rather than answer badly.
    AUTOMATION = "automation"
    #: Our own housekeeping. Never user-visible, and never allowed to hold a pooler slot
    #: that interactive traffic needs.
    SYSTEM = "system"


def traffic_class_for(channel: Channel) -> TrafficClass:
    """The declaration required of every channel.

    `assert_never` is the whole point of this function. Adding a member to `Channel`
    without adding it here is a type error, so a new channel cannot reach production
    without someone deciding what happens to it under load. A dictionary with a `.get`
    default would accept the new channel silently and treat it as whatever the default
    happened to be.
    """
    match channel:
        case (
            Channel.CONSOLE
            | Channel.LARK
            | Channel.WHATSAPP
            | Channel.WIDGET
            | Channel.SLACK
            # Somebody is holding the handset and watching for the reply, exactly as they
            # are on WhatsApp. Queueing a Telegram answer would leave a person waiting on a
            # notification that arrives after they have put the phone down.
            | Channel.TELEGRAM
        ):
            return TrafficClass.HUMAN_INTERACTIVE
        case Channel.EMAIL:
            # A reply that arrives in four minutes is fine; a wrong one is not. Queue.
            return TrafficClass.HUMAN_ASYNC
        case Channel.API | Channel.WEBHOOK:
            return TrafficClass.AUTOMATION
        case Channel.SCHEDULER:
            return TrafficClass.SYSTEM
        case _:
            assert_never(channel)


class GateStep(enum.IntEnum):
    """The ordered steps of the gate. The numbers leave room to insert without renumbering.

    Ordering is meaning, not convention. ENTITLE before SELECT means an agent is chosen from
    tools the caller already holds, rather than being chosen first and then having its tools
    trimmed, which is the mistake that turns an agent into a principal. REDACT after INVOKE
    and before COMPOSE means nothing reaches an answer without passing the walker.
    """

    RECORD = 10
    INGEST = 20
    IDENTIFY = 30
    ENTITLE = 40
    CLASSIFY = 50
    CACHE = 60
    SELECT = 70
    INVOKE = 80
    REDACT = 90
    COMPOSE = 100


class StepOutOfOrderError(Exception):
    """Raised when the gate runs a step before one it depends on.

    Deliberately not part of the user-facing taxonomy. Nobody asking a question sees this;
    it is a programming error, and it should stop the request rather than degrade it,
    because a gate that ran SELECT before ENTITLE has already built the wrong catalogue.
    """


@dataclass
class Recorder:
    """The trace, constructed at ingress before anything else is known.

    Fields are filled in as the request learns about itself. That is why this is mutable
    while `GateContext` is frozen: the context is a fact once assembled, and the recorder is
    the running account of how it got there.
    """

    trace_id: str
    received_at: datetime
    channel: Channel
    traffic_class: TrafficClass
    #: Steps completed, in the order they completed. Not a set: order is the evidence.
    steps: list[GateStep] = field(default_factory=list)
    #: Filled in at IDENTIFY. None until then, and None afterwards for an unrecognised
    #: caller, which is a legitimate outcome rather than an error.
    principal_id: str | None = None
    ent_hash: str | None = None
    notes: list[str] = field(default_factory=list)

    def enter(self, step: GateStep) -> None:
        """Record a step, refusing to run one out of order.

        Checked rather than assumed because skipping a step is a permission bypass that
        reads like a refactor in a diff.
        """
        if self.steps and step <= self.steps[-1]:
            raise StepOutOfOrderError(
                f"{step.name} cannot run after {self.steps[-1].name}; "
                "the gate's order is what makes each step's guarantee hold"
            )
        self.steps.append(step)

    def note(self, message: str) -> None:
        """A line for the trace. Never a field value; see the redaction rules."""
        self.notes.append(message)

    def reached(self, step: GateStep) -> bool:
        return step in self.steps


def open_trace(trace_id: str, received_at: datetime, channel: Channel) -> Recorder:
    """Construct the recorder. Called at ingress, before identification.

    This function exists so there is one obvious place to look for the answer to "was this
    request recorded", and so the RECORD step is entered by construction rather than by a
    caller remembering to.
    """
    recorder = Recorder(
        trace_id=trace_id,
        received_at=received_at,
        channel=channel,
        traffic_class=traffic_class_for(channel),
    )
    recorder.enter(GateStep.RECORD)
    return recorder


@dataclass(frozen=True)
class GateContext:
    """Everything the gate knows about one request, once identity and reach are settled.

    Frozen on purpose. A mutable context invites a later step to widen `entitlements`, and
    the invariant that an agent's reach is the caller's reach intersected with its ceiling
    only holds if nothing downstream can edit the caller's reach.
    """

    trace_id: str
    principal: Principal
    entitlements: EntitlementSet
    ent_hash: str
    lane: Lane
    channel: Channel
    traffic_class: TrafficClass
    received_at: datetime

    def __post_init__(self) -> None:
        # The hash is carried rather than recomputed on every use, so it has to be the hash
        # of the set beside it. A mismatched pair would key the cache on one reach while
        # answering with another.
        if self.ent_hash != self.entitlements.ent_hash():
            raise ValueError(
                "ent_hash does not match the entitlements it travels with; "
                "the cache would be keyed on a different reach than the answer uses"
            )
        if self.traffic_class is not traffic_class_for(self.channel):
            raise ValueError(
                f"{self.channel} declares {traffic_class_for(self.channel)}, "
                f"not {self.traffic_class}"
            )

    @property
    def person_is_waiting(self) -> bool:
        """Whether to degrade visibly or queue. Asked often enough to be worth naming."""
        return self.traffic_class is TrafficClass.HUMAN_INTERACTIVE
