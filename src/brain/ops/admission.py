"""Whether there is room to start this work, decided before any of it starts.

The design had per-run limits and no global one. Architecture §25 puts the arithmetic
plainly: "Twenty users each running eight subtasks is a hundred and sixty concurrent
operations against a machine sized for ten", and its conclusion is that **every resource
gets a global budget, enforced centrally before work starts**, not by each subsystem
independently consuming until Postgres or memory gives out. That is this module.

Four properties carry the weight, and each one prevents a failure that is invisible.

**The decision happens before work starts, and never in the middle.** A request admitted
and then abandoned halfway has already paid for the tokens, the connector call and the
person's wait; killing it recovers nothing and loses the answer as well. So `decide` runs
once, on a snapshot, and there is deliberately no "cancel" or "revoke" here for a caller to
reach for later. See `ADMISSION_DECIDES_BEFORE_WORK_STARTS`.

**A person waiting is shed, never queued; nobody waiting is queued, never shed.** This
looks backwards for about ten seconds. The class that gets turned away with an honest "the
system is full" is the interactive one, and the class handed a queue position is the batch
one. It is the right way round because of the ceilings: interactive may use the whole
budget and batch may use half of it, so by the time an interactive request is refused every
other class has been refused for a long while already. And at that point the useful thing
for a person watching a cursor is the truth now, not a position in a queue they will not
watch. `TrafficClass.HUMAN_INTERACTIVE` says the same thing in one line: degrade visibly,
never queue.

**Shedding is by class, and the interactive class is shed last.** Implemented as a per-class
share of every budget rather than as an ordered list somebody maintains, because a list and
a set of thresholds drift and then the list is the one that looks authoritative in review.
`SHED_ORDER` is *derived* from `CLASS_CEILING`, so the two cannot disagree.

**A refusal for capacity is not a refusal for permission.** To the person they may read
alike. To an operator they must not: one says add capacity, the other says the permission
model is working. `RefusalKind` and `OPERATOR_ACTION` are how that survives into the logs,
and `CapacityRefused` is deliberately not a subclass of `Denied` or of `Degraded`.

Everything here is a pure function of its arguments. `now` is a parameter, nothing sleeps,
nothing spawns a thread and nothing counts anything itself: the caller passes a
`CapacityState` snapshot. A controller that owned its own counters could not be tested for
the case that goes wrong, which is the decision taken while the counters are stale.

The §25 pressure table falls out of the two mechanisms rather than being coded as a table:

    FAST still runs          the fast lane uses no model, so it never asks for the
                             resource that saturates; `AdmissionRequest` refuses to let it
    ANSWER queues briefly    a brief in-request wait is `limit_concurrency` in
                             `brain.runtime`, not a queue position; see A_PERSON_...
    TASK queues              the task lane is never "a person waiting", whatever channel
                             launched it: the person is waiting for the acknowledgement
    browser queues separately  browser sessions are their own budget row
    ingestion throttles      ingestion is BATCH, whose ceiling is half of every budget
    reports deprioritise     reports are BATCH too, by the same one mechanism

This is the capacity half of admission. `brain.gate.admission` is the unrelated other half:
what the channel and the strength of a sign-in take away from what a person holds. Nothing
here touches entitlements, and nothing there knows the machine is busy.

Task ids: M22.1.1, M22.1.2, M22.1.3, M22.1.4, M22.1.5, M22.2.1, M22.2.2, M22.2.3
Task ids: M22.2.4, M22.3.1, M22.3.2, M22.3.4
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import assert_never

from brain.core.errors import BrainError, Outcome
from brain.core.lane import Lane
from brain.gate.context import TrafficClass

# ------------------------------------------------------------------ written-down reasons
#: Why there is no mid-flight admission check, and why adding one would not help.
ADMISSION_DECIDES_BEFORE_WORK_STARTS = (
    "A request admitted and then abandoned has already cost the tokens, the connector call "
    "and the person's wait. Stopping it halfway recovers none of that and loses the answer "
    "as well, so the only decision worth taking is the one taken before the work starts. "
    "Anything that wants to stop work already running is a kill switch and belongs where "
    "kill switches are, not in the function that admits."
)

#: Why the interactive class is refused outright while lower classes are handed a position.
A_PERSON_WAITING_IS_NEVER_QUEUED = (
    "A person watching a cursor is refused rather than queued, and that is not harsher "
    "treatment: interactive may use the whole of every budget, so by the time it is refused "
    "the other classes have been refused for some time. What a queue position buys is a "
    "retry that costs nothing, which is worth having only when nobody is waiting for it. "
    "Architecture §25 says ANSWER 'queues briefly'; the brief wait it means is the "
    "connection-level one that `runtime.profile_for` already provides through "
    "limit_concurrency, not a durable position handed back to a person."
)

#: Why capacity, quota and permission are three refusals rather than one.
CAPACITY_IS_NOT_PERMISSION = (
    "Three refusals that read alike to the person asking and mean three different things to "
    "whoever is on call. Capacity: add capacity, this is our fault. Quota: raise this "
    "principal's allowance or leave it, this is working as configured. Permission: this is "
    "working, do nothing. Collapsing capacity into the DENIED outcome would corrupt the one "
    "signal that says the permission model is doing its job, because a busy afternoon would "
    "look like a burst of access failures."
)

#: Why ingestion has no throttle of its own.
INGESTION_THROTTLE_IS_THE_CLASS_CEILING = (
    "Ingestion is throttled by BATCH's share of the document and embedding budgets, and by "
    "the batch connection pool, and by nothing else. A separate ingestion knob was "
    "considered and rejected: two mechanisms governing one resource drift apart, and the "
    "operator watching a stalled parse cannot tell which of them stopped it."
)


# ------------------------------------------------------------------------------ resources
class Resource(enum.StrEnum):
    """Exactly the seven things architecture §25 says get a global budget.

    Quoted rather than paraphrased: "concurrent model calls · concurrent source calls per
    connector · browser sessions · long-running tasks · document jobs · embedding jobs ·
    tokens per minute". An eighth member added here without a seed row is caught by
    `seed_budgets`, which must cover every member.
    """

    MODEL_CALLS = "model_calls"
    SOURCE_CALLS = "source_calls"
    BROWSER_SESSIONS = "browser_sessions"
    LONG_RUNNING_TASKS = "long_running_tasks"
    DOCUMENT_JOBS = "document_jobs"
    EMBEDDING_JOBS = "embedding_jobs"
    TOKENS_PER_MINUTE = "tokens_per_minute"


class BudgetKind(enum.StrEnum):
    """Whether a budget counts things in flight or things per window.

    The difference is what "free" means. A concurrency slot frees when the work finishes; a
    rate slot frees when the window rolls, whatever anybody does. A single counter for both
    would tell a caller to retry after the mean service time for a limit that will not move
    for another fifty seconds.
    """

    CONCURRENCY = "concurrency"
    RATE = "rate"


def kind_of(resource: Resource) -> BudgetKind:
    """Every resource must declare its kind, checked by the compiler.

    `assert_never` rather than a dictionary with a default, for the reason
    `traffic_class_for` gives: a new resource cannot reach production without somebody
    deciding what it means for it to be full.
    """
    match resource:
        case (
            Resource.MODEL_CALLS
            | Resource.SOURCE_CALLS
            | Resource.BROWSER_SESSIONS
            | Resource.LONG_RUNNING_TASKS
            | Resource.DOCUMENT_JOBS
            | Resource.EMBEDDING_JOBS
        ):
            return BudgetKind.CONCURRENCY
        case Resource.TOKENS_PER_MINUTE:
            return BudgetKind.RATE
        case _:
            assert_never(resource)


#: The resources budgeted per connector rather than once globally. §25 says "concurrent
#: source calls **per connector**", and the per-connector split is the whole point: one
#: global source-call counter lets a Xero backfill occupy every slot and starve Freshdesk,
#: which reads in the console as "the system is busy" rather than as "Xero is busy".
PER_CONNECTOR: frozenset[Resource] = frozenset({Resource.SOURCE_CALLS})


# ------------------------------------------------------------------------ workload classes
class WorkloadClass(enum.StrEnum):
    """The three pools that share the database, from §25.

    Three, not six. "Six classes would be over-engineering at this size; one is what causes
    an ingestion run to make the chat slow."
    """

    #: The request path.
    INTERACTIVE = "interactive"
    #: Tasks and agents.
    BACKGROUND = "background"
    #: Ingestion, re-indexing, reporting.
    BATCH = "batch"


#: The share of any one budget a class may occupy. Interactive is 1.0 and must stay 1.0:
#: it is the definition of "shed last", and an invariant test pins it.
#:
#: Batch at half means a bulk parse can never take more than half of anything, so the chat
#: keeps at least half the machine while a re-index runs. Background at four fifths means
#: tasks and agents can never take the last fifth, which is the slice the request path is
#: refused out of only when the whole budget is gone.
CLASS_CEILING: Mapping[WorkloadClass, float] = MappingProxyType(
    {
        WorkloadClass.BATCH: 0.5,
        WorkloadClass.BACKGROUND: 0.8,
        WorkloadClass.INTERACTIVE: 1.0,
    }
)

#: Who is refused first, derived from the ceilings rather than written beside them. A
#: hand-maintained order and a set of thresholds drift, and the order is the one that looks
#: authoritative in review while the thresholds are the one the code actually obeys.
SHED_ORDER: tuple[WorkloadClass, ...] = tuple(sorted(WorkloadClass, key=lambda c: CLASS_CEILING[c]))


def narrower_of(left: WorkloadClass, right: WorkloadClass) -> WorkloadClass:
    """The lower-priority of two classes. Composition here only ever subtracts."""
    return SHED_ORDER[min(SHED_ORDER.index(left), SHED_ORDER.index(right))]


def workload_class_for(traffic: TrafficClass, lane: Lane) -> WorkloadClass:
    """Which pool this request belongs to, from the traffic class and the lane.

    The traffic class decides the base, because whether a person is waiting is the fact
    that matters and the channel already declared it at ingress with no default.

    The lane can then only lower it. A task launched from the console is task-lane work
    however interactive the channel is: §25 puts "tasks and agents" in background, and the
    person who pressed the button is waiting for the acknowledgement rather than for the
    run. Lowering rather than assigning keeps the house rule that ceilings only subtract,
    so a scheduler-launched task stays in BATCH instead of being promoted out of it.
    """
    match traffic:
        case TrafficClass.HUMAN_INTERACTIVE:
            base = WorkloadClass.INTERACTIVE
        case TrafficClass.HUMAN_ASYNC | TrafficClass.AUTOMATION:
            base = WorkloadClass.BACKGROUND
        case TrafficClass.SYSTEM:
            base = WorkloadClass.BATCH
        case _:
            assert_never(traffic)
    if lane is Lane.TASK:
        return narrower_of(base, WorkloadClass.BACKGROUND)
    return base


def person_is_waiting(traffic: TrafficClass, lane: Lane) -> bool:
    """Whether somebody is watching this particular unit of work finish.

    Not the same question as `GateContext.person_is_waiting`, and deliberately a different
    function rather than a reuse. A task-lane run started from the console has an
    interactive traffic class and nobody waiting on the run itself, because what the person
    is waiting for is the sub-second acknowledgement. Queueing it is free; shedding it
    throws away work that could have been done at three in the morning.
    """
    return traffic is TrafficClass.HUMAN_INTERACTIVE and lane is not Lane.TASK


# ------------------------------------------------------------------------- refusal kinds
class RefusalKind(enum.StrEnum):
    """Why something did not happen, in the words that decide who fixes it."""

    #: The caller does not hold it. Recorded as DENIED, shown as ABSENT.
    PERMISSION = "permission"
    #: The system is full. The caller is entitled and would have been served on a quiet day.
    CAPACITY = "capacity"
    #: The caller is over an allowance that belongs to them, not to the machine.
    QUOTA = "quota"
    #: Something this decision needed could not be reached, and the safe answer was no.
    #: Distinct from CAPACITY because the operator action is different and mutually
    #: exclusive: capacity says buy more, a dependency says repair the thing that is down.
    #: Folding the two together is how an outage gets answered by a bigger server.
    DEPENDENCY = "dependency"


#: One action per kind, and no two the same. This is the whole of the "distinguishable to
#: an operator" property: an alert carrying one of these strings has already said what to do.
OPERATOR_ACTION: Mapping[RefusalKind, str] = MappingProxyType(
    {
        RefusalKind.PERMISSION: "nothing; the permission model is working",
        RefusalKind.CAPACITY: "add capacity or raise the budget row",
        RefusalKind.QUOTA: "raise this principal's limit, or leave it and let them wait",
        RefusalKind.DEPENDENCY: "restore the named dependency; nothing here is over its limit",
    }
)


def refusal_record(kind: RefusalKind, *, subject: str, detail: str) -> Mapping[str, str]:
    """The operator-facing fields for one refusal.

    `subject` is what ran out (a resource, a connector, a capability), never who asked and
    never a field value. The audit layer owns the principal; repeating it here would put a
    second copy of an identity in a second place with its own retention.
    """
    return MappingProxyType(
        {
            "refusal_kind": str(kind),
            "operator_action": OPERATOR_ACTION[kind],
            "subject": subject,
            "detail": detail,
        }
    )


class CapacityRefused(BrainError):  # noqa: N818 - the taxonomy in core.errors has no suffixes
    """There was no room to start, and we said so before starting.

    Deliberately not a `Denied`: the caller holds everything they need, and DENIED is the
    outcome that means "this exists and you may not see it". Deliberately not a `Degraded`
    either: no source was unreachable, we simply declined to start the work, and sending an
    operator to check whether Xero is up when the answer is "the machine is full" wastes
    the first ten minutes of an incident.

    `Outcome.FAILED` is the honest taxonomy entry for it, and `RefusalKind.CAPACITY` on the
    log record is what separates it from every other FAILED.
    """

    outcome = Outcome.FAILED
    public_message = "The system is busy right now, so I have not started that."


# ------------------------------------------------------------------------------- budgets
#: How a budget is addressed: the resource, and the connector it belongs to (empty for the
#: global ones). A plain tuple rather than a class because it is a dictionary key and
#: nothing more.
BudgetKey = tuple[Resource, str]

_NO_COUNTS: Mapping[BudgetKey, int] = MappingProxyType({})


@dataclass(frozen=True)
class Budget:
    """One global ceiling, as a configuration row rather than a constant (M22.1.2).

    `source` records where the row came from. §25 is explicit that "All of these are
    configuration rows, not constants", and the reason to carry the provenance is that the
    first question about a budget is always whether anybody chose it or whether it is still
    the seed nobody revisited.
    """

    resource: Resource
    limit: int
    #: The connector this row governs, or empty for a global row. A row keyed "" for a
    #: per-connector resource is the default applied to a connector with no row of its own.
    key: str = ""
    #: How long one unit of this budget is typically held. Little's law needs it, the queue
    #: estimate needs it, and it is the number an operator changes when the estimate is
    #: consistently wrong.
    mean_service_seconds: float = 1.0
    #: RATE budgets only. The window the limit is counted over.
    window_seconds: float | None = None
    source: str = "seed"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.limit < 1:
            # A budget of zero is how a resource is switched off, and switching a resource
            # off by editing a limit is invisible in the console: the row still reads as
            # configured. Removing the capability is a different change and belongs
            # somewhere it can be seen.
            msg = f"budget {self.resource}/{self.key or '*'} has limit {self.limit}; minimum is 1"
            raise ValueError(msg)
        if self.mean_service_seconds <= 0:
            msg = f"budget {self.resource}/{self.key or '*'} has a non-positive service time"
            raise ValueError(msg)
        expected = kind_of(self.resource)
        if expected is BudgetKind.RATE and self.window_seconds is None:
            msg = f"{self.resource} is a rate budget and needs a window"
            raise ValueError(msg)
        if expected is BudgetKind.CONCURRENCY and self.window_seconds is not None:
            # A concurrency budget with a window reads as a rate limit and behaves as
            # neither, and the retry hint it produces is wrong in whichever direction the
            # reader did not expect.
            msg = f"{self.resource} counts things in flight, so a window makes no sense on it"
            raise ValueError(msg)
        if self.key and self.resource not in PER_CONNECTOR:
            msg = (
                f"{self.resource} is budgeted once globally, so it cannot be keyed by {self.key!r}"
            )
            raise ValueError(msg)

    @property
    def kind(self) -> BudgetKind:
        return kind_of(self.resource)

    @property
    def budget_key(self) -> BudgetKey:
        return (self.resource, self.key)

    def ceiling_for(self, workload: WorkloadClass) -> int:
        """The most of this budget one class may hold.

        Floors, then never below one. A share that rounds to zero would take a class out of
        service with the row still reading as configured, which is the same failure the
        limit check above refuses; on a budget that small there is no isolation to be had
        and the honest fix is a larger budget, not a rounding rule.
        """
        return max(1, math.floor(self.limit * CLASS_CEILING[workload]))


def seed_budgets() -> tuple[Budget, ...]:
    """The rows the console starts from. Not a runtime source of truth.

    Every number here is derived from something measured or contracted, and the derivation
    is in the row's own `reason` so an operator changing it can see what it was answering.

    The per-connector source-call rows are the clearest case. Lark Base is **100 requests
    per minute, permanently**: their documentation states it cannot be raised, so it is
    1.67 calls a second for the whole tenant for ever. At the 5-second connector timeout
    from §25, Little's law gives L = 1.67 x 5 = 8.3 calls in flight to saturate exactly
    that ceiling, so 8 is the concurrency above which the only extra thing we can produce
    is 429s. Xero's 60 a minute gives 1.0 x 5 = 5. Sizing either against a bigger number
    is sizing against a number that does not exist.
    """
    return (
        Budget(
            resource=Resource.MODEL_CALLS,
            limit=40,
            mean_service_seconds=4.0,
            reason=(
                "40 matches the MAIN primary rung's max_concurrency in models.routing."
                "seed_chain, so the global budget binds at the same point the busiest rung "
                "does rather than after it. 4s is the ANSWER p50 target from §25."
            ),
        ),
        Budget(
            resource=Resource.SOURCE_CALLS,
            key="lark_base",
            limit=8,
            mean_service_seconds=5.0,
            reason=(
                "Lark Base is 100 requests per minute and their docs state it cannot be "
                "raised: 1.67 calls/second for the whole tenant. At the 5s connector "
                "timeout, L = 1.67 x 5 = 8.3, so 8 saturates the ceiling exactly."
            ),
        ),
        Budget(
            resource=Resource.SOURCE_CALLS,
            key="xero",
            limit=5,
            mean_service_seconds=5.0,
            reason=(
                "Xero is 60 calls a minute (1.0/second) and 5,000 a day per tenant, shared "
                "with every other integration the client runs. L = 1.0 x 5 = 5."
            ),
        ),
        Budget(
            resource=Resource.SOURCE_CALLS,
            key="freshdesk",
            limit=8,
            mean_service_seconds=5.0,
            reason=(
                "Freshdesk is 100/400/700 a minute by plan. Sized against the lowest, "
                "because sizing against a plan we may not hold produces 429s on the day "
                "somebody downgrades."
            ),
        ),
        Budget(
            resource=Resource.SOURCE_CALLS,
            key="",
            limit=4,
            mean_service_seconds=5.0,
            reason=(
                "The default for a connector with no row of its own. Deliberately low: a "
                "connector nobody has measured is a connector whose ceiling nobody knows."
            ),
        ),
        Budget(
            resource=Resource.BROWSER_SESSIONS,
            limit=2,
            mean_service_seconds=60.0,
            reason=(
                "Browser is the most expensive subsystem on a box running about thirty "
                "containers on twelve gigabytes; each session is hundreds of megabytes."
            ),
        ),
        Budget(
            resource=Resource.LONG_RUNNING_TASKS,
            limit=10,
            mean_service_seconds=300.0,
            reason=(
                "§25: a hundred and sixty concurrent operations 'against a machine sized for ten'."
            ),
        ),
        Budget(
            resource=Resource.DOCUMENT_JOBS,
            limit=4,
            mean_service_seconds=30.0,
            reason="Parsing is CPU-bound and shares the box with the request path.",
        ),
        Budget(
            resource=Resource.EMBEDDING_JOBS,
            limit=4,
            mean_service_seconds=10.0,
            reason="Sized with document jobs; a re-index that outruns parsing only queues earlier.",
        ),
        Budget(
            resource=Resource.TOKENS_PER_MINUTE,
            limit=200_000,
            window_seconds=60.0,
            mean_service_seconds=4.0,
            reason=(
                "Sized against the peak minute, not the daily total. 0.1 requests/second is "
                "the estate mean, about 6 questions a minute; at roughly 12,000 tokens a "
                "question this admits about 16 in the busiest minute, 2.7x the mean."
            ),
        ),
    )


# --------------------------------------------------------------------------- the snapshot
@dataclass(frozen=True)
class CapacityState:
    """What is in flight right now, as somebody else counted it.

    This module counts nothing. A controller that owned its counters would be untestable
    for the case that actually goes wrong, which is a decision taken against stale counts,
    and it would also have to be a singleton, which is how a per-worker limit quietly
    becomes a per-process one on a box running several workers.
    """

    #: Units held per budget. In flight for a concurrency budget; observed in the current
    #: window for a rate budget.
    used: Mapping[BudgetKey, int] = _NO_COUNTS
    #: How many callers are already waiting on each budget, so a queue position is a
    #: position rather than a guess.
    queued: Mapping[BudgetKey, int] = _NO_COUNTS

    def used_for(self, key: BudgetKey) -> int:
        return self.used.get(key, 0)

    def queued_for(self, key: BudgetKey) -> int:
        return self.queued.get(key, 0)


# ------------------------------------------------------------------------- the decision
class Verdict(enum.StrEnum):
    ADMITTED = "admitted"
    #: Nobody is waiting, so the work keeps its place and comes back.
    QUEUED = "queued"
    #: Somebody is waiting, so they are told the truth now.
    SHED = "shed"


@dataclass(frozen=True)
class QueuePlacement:
    """A position and an estimate, which are not the same as a promise.

    The estimate comes from Little's law over the class's own share of the budget. It is
    reported as an estimate because a confident wall-clock promise that the queue cannot
    keep is worse than an honest range: the person stops believing the next one.
    """

    position: int
    expected_wait_seconds: float


@dataclass(frozen=True)
class AdmissionRequest:
    """One unit of work asking for room, before any of it has been done."""

    trace_id: str
    lane: Lane
    traffic_class: TrafficClass
    resource: Resource
    #: The connector, for a per-connector resource. Empty for the global ones.
    key: str = ""
    units: int = 1

    def __post_init__(self) -> None:
        if self.units < 1:
            msg = f"{self.trace_id} asked for {self.units} units; minimum is 1"
            raise ValueError(msg)
        if self.lane is Lane.FAST and self.resource is Resource.MODEL_CALLS:
            # The fast lane's whole guarantee is that no model saw the question, which is
            # also why §25 can say FAST still runs under pressure. A fast-lane request
            # asking for a model-call slot means the lane classification and this call
            # disagree, and the one that is wrong is not knowable from here.
            msg = (
                "the fast lane uses no model, so it cannot ask for a model-call slot; "
                "either the lane is wrong or the caller is not on the fast path"
            )
            raise ValueError(msg)
        if self.resource in PER_CONNECTOR and not self.key:
            msg = (
                f"{self.resource} is budgeted per connector and no connector was named; "
                "an unkeyed source-call counter lets one connector starve every other"
            )
            raise ValueError(msg)
        if self.resource not in PER_CONNECTOR and self.key:
            msg = f"{self.resource} is budgeted once globally, so {self.key!r} has nowhere to go"
            raise ValueError(msg)

    @property
    def budget_key(self) -> BudgetKey:
        return (self.resource, self.key)

    @property
    def workload_class(self) -> WorkloadClass:
        return workload_class_for(self.traffic_class, self.lane)

    @property
    def has_someone_waiting(self) -> bool:
        return person_is_waiting(self.traffic_class, self.lane)


@dataclass(frozen=True)
class AdmissionDecision:
    """The verdict and the whole argument for it.

    `reason` is a sentence for the same purpose `ProcessProfile.reason` is one: a refusal
    with no argument attached gets removed by whoever is next annoyed by it.
    """

    verdict: Verdict
    request: AdmissionRequest
    workload_class: WorkloadClass
    decided_at: datetime
    #: The budget consulted, or None when there was no row at all.
    budget: Budget | None
    used: int
    ceiling: int
    reason: str
    queue: QueuePlacement | None = None
    retry_after_seconds: float | None = None

    @property
    def admitted(self) -> bool:
        return self.verdict is Verdict.ADMITTED

    @property
    def utilisation(self) -> float:
        """Where this class stood against its own share, not against the whole budget.

        Against the whole budget it would read low while batch was already being refused,
        which is the number that makes an operator say the machine is idle while half the
        estate queues.
        """
        return self.used / self.ceiling if self.ceiling else 1.0

    def as_error(self) -> CapacityRefused:
        """The refusal as an exception, for a caller that wants to raise one.

        Returned rather than raised. Admission returning a decision and the caller choosing
        what to do with it is what lets the console inspect a doomed request without
        catching an exception, the same split `RoutePlan` makes.
        """
        if self.verdict is Verdict.ADMITTED:
            msg = f"{self.request.trace_id} was admitted; there is no refusal to raise"
            raise ValueError(msg)
        return CapacityRefused(self.reason)

    def log_record(self) -> Mapping[str, str]:
        """The operator-facing line. Names the resource, never the asker."""
        if self.admitted:
            return MappingProxyType(
                {
                    "verdict": str(self.verdict),
                    "resource": str(self.request.resource),
                    "workload_class": str(self.workload_class),
                    "used": str(self.used),
                    "ceiling": str(self.ceiling),
                }
            )
        subject = f"{self.request.resource}/{self.request.key or '*'}"
        return refusal_record(RefusalKind.CAPACITY, subject=subject, detail=self.reason)


def budget_for(budgets: Sequence[Budget], key: BudgetKey) -> Budget | None:
    """The row governing this key, falling back to the connector-wide default row.

    Exact match first so a connector with a measured ceiling never silently inherits the
    conservative default, and the default only ever applies to a connector nobody has
    measured.
    """
    resource = key[0]
    exact = [b for b in budgets if b.budget_key == key]
    if exact:
        return exact[0]
    if resource in PER_CONNECTOR:
        fallback = [b for b in budgets if b.budget_key == (resource, "")]
        if fallback:
            return fallback[0]
    return None


def headroom(
    budgets: Sequence[Budget], state: CapacityState, key: BudgetKey, workload: WorkloadClass
) -> int:
    """How many more units this class may take of this budget. Never negative.

    This is also the whole of ingestion throttling: `headroom(..., WorkloadClass.BATCH)` on
    the document-job budget is how many parses may start, and there is deliberately no
    second knob. See `INGESTION_THROTTLE_IS_THE_CLASS_CEILING`.
    """
    budget = budget_for(budgets, key)
    if budget is None:
        return 0
    return max(0, budget.ceiling_for(workload) - state.used_for(key))


def _wait_seconds(budget: Budget, *, used: int, units: int, ceiling: int, position: int) -> float:
    """How long until there is room, counting how far over the ceiling the budget already is.

    The first version of this was `position * mean_service / ceiling`, which is the textbook
    M/M/c wait and is wrong here in a way that matters: it ignores the overshoot. A batch
    request refused because the budget is at 32 against a batch share of 20 was told to come
    back in 0.2 seconds, when thirteen things in flight have to finish first. An estimate
    that is short by an order of magnitude is worse than none, because the caller comes
    straight back and is refused again.

    So: count the departures needed (the overshoot, plus everybody already queued ahead),
    and divide by the rate departures actually happen at, which is `used / mean_service` for
    a concurrency budget. For a rate budget nothing departs at all until the window rolls,
    so the answer is a whole number of windows.

    It stays an estimate and is reported as one. It assumes no new arrivals, which is false
    exactly when the system is busy, so it is a floor rather than a promise.
    """
    needed = max(1, used + units - ceiling + max(0, position - 1))
    if budget.kind is BudgetKind.RATE:
        # `window_seconds` is never None on a rate budget — `Budget.__post_init__` refuses
        # one without it — but that is a runtime guarantee and the type is still optional,
        # so the fallback is spelled out rather than asserted away.
        window = budget.window_seconds or budget.mean_service_seconds
        return math.ceil(needed / ceiling) * window
    departures_per_second = max(1, used) / budget.mean_service_seconds
    return needed / departures_per_second


def decide(
    request: AdmissionRequest,
    budgets: Sequence[Budget],
    state: CapacityState,
    *,
    now: datetime,
    jitter: float = 0.0,
) -> AdmissionDecision:
    """Admit, queue or shed. Pure, total, and taken before any work starts.

    The order is the rule:

    1. no budget row at all means shed, because an unbudgeted resource is precisely the
       failure global budgets exist to prevent, and admitting into one is how a subsystem
       consumes until memory gives out;
    2. within the class's share of the budget means admit;
    3. otherwise, somebody waiting is told now and nobody waiting is given a position.

    There is no fourth branch that admits anyway under some condition, and adding one would
    be the whole of the regression: a budget with an exception is a budget that binds on
    the days nothing was going to go wrong.
    """
    workload = request.workload_class
    key = request.budget_key
    budget = budget_for(budgets, key)

    if budget is None:
        return AdmissionDecision(
            verdict=Verdict.SHED,
            request=request,
            workload_class=workload,
            decided_at=now,
            budget=None,
            used=0,
            ceiling=0,
            reason=(
                f"no budget row for {request.resource}/{request.key or '*'}; an unbudgeted "
                "resource is the failure global budgets exist to prevent, so nothing starts "
                "against it"
            ),
        )

    ceiling = budget.ceiling_for(workload)
    used = state.used_for(key)

    if used + request.units <= ceiling:
        return AdmissionDecision(
            verdict=Verdict.ADMITTED,
            request=request,
            workload_class=workload,
            decided_at=now,
            budget=budget,
            used=used,
            ceiling=ceiling,
            reason=(
                f"{used + request.units} of {ceiling} {request.resource} for {workload} "
                f"(budget {budget.limit}, class share {CLASS_CEILING[workload]:.0%})"
            ),
        )

    shortfall = (
        f"{request.resource}/{request.key or '*'} is at {used} of {ceiling} for {workload}, "
        f"out of a budget of {budget.limit}"
    )

    if request.has_someone_waiting:
        return AdmissionDecision(
            verdict=Verdict.SHED,
            request=request,
            workload_class=workload,
            decided_at=now,
            budget=budget,
            used=used,
            ceiling=ceiling,
            reason=f"{shortfall}; a person is waiting, so this is said out loud rather than queued",
            # Jitter only on the shed hint. A shed request is a client we do not control
            # coming back on its own, and a hundred of them refused in the same second
            # would otherwise return together. A queue position is ours to schedule, so
            # jittering it would only make an estimate we own less accurate.
            retry_after_seconds=(
                _wait_seconds(budget, used=used, units=request.units, ceiling=ceiling, position=1)
                * (1.0 + max(0.0, jitter))
            ),
        )

    position = state.queued_for(key) + 1
    wait = _wait_seconds(budget, used=used, units=request.units, ceiling=ceiling, position=position)
    return AdmissionDecision(
        verdict=Verdict.QUEUED,
        request=request,
        workload_class=workload,
        decided_at=now,
        budget=budget,
        used=used,
        ceiling=ceiling,
        reason=f"{shortfall}; nobody is waiting, so it keeps its place at position {position}",
        queue=QueuePlacement(position=position, expected_wait_seconds=wait),
        retry_after_seconds=wait,
    )


# ---------------------------------------------------------------------- the shed policy
@dataclass(frozen=True)
class ShedNotice:
    """One class, on one budget, that is being turned away right now.

    §25 asks for a "shed-load policy naming what was deferred". A count of refusals does
    not name anything; this does, and it is readable without a request in hand, which is
    what makes it usable on a dashboard during an incident rather than afterwards in a log.
    """

    resource: Resource
    key: str
    workload_class: WorkloadClass
    used: int
    ceiling: int
    limit: int
    deferred: str


def shed_plan(budgets: Sequence[Budget], state: CapacityState) -> tuple[ShedNotice, ...]:
    """What is currently being deferred, and for whom. A snapshot, not a history.

    Ordered by resource and then by shed order, so the top of the list is always the class
    that gives way first and the bottom is the request path. An operator reading it top to
    bottom is reading the order the system will fail in.
    """
    notices: list[ShedNotice] = []
    for budget in budgets:
        used = state.used_for(budget.budget_key)
        for workload in SHED_ORDER:
            ceiling = budget.ceiling_for(workload)
            if used < ceiling:
                continue
            notices.append(
                ShedNotice(
                    resource=budget.resource,
                    key=budget.key,
                    workload_class=workload,
                    used=used,
                    ceiling=ceiling,
                    limit=budget.limit,
                    deferred=(
                        f"{workload} work on {budget.resource}"
                        f"{'/' + budget.key if budget.key else ''} is deferred: {used} of a "
                        f"{ceiling} share of {budget.limit}"
                    ),
                )
            )
    return tuple(
        sorted(notices, key=lambda n: (str(n.resource), n.key, SHED_ORDER.index(n.workload_class)))
    )


# ------------------------------------------------------------------- connection pools
#: The share of a worker's database slots each class gets. Interactive holds most of them
#: because it is the only class anybody is waiting on; batch holds enough to make progress
#: and never enough to matter. The three must sum to 1.0, which an invariant test pins.
POOL_SHARE: Mapping[WorkloadClass, float] = MappingProxyType(
    {
        WorkloadClass.INTERACTIVE: 0.60,
        WorkloadClass.BACKGROUND: 0.25,
        WorkloadClass.BATCH: 0.15,
    }
)


@dataclass(frozen=True)
class ClassPools:
    """Three connection pools, one per workload class (§25).

    Separate pools rather than one pool with priorities, because a priority scheme on a
    shared pool still lets a long batch transaction hold a connection the request path
    needs: priority decides who gets the next free slot, not who is holding the ones that
    are not free.
    """

    interactive: int
    background: int
    batch: int

    def slots_for(self, workload: WorkloadClass) -> int:
        match workload:
            case WorkloadClass.INTERACTIVE:
                return self.interactive
            case WorkloadClass.BACKGROUND:
                return self.background
            case WorkloadClass.BATCH:
                return self.batch
            case _:
                assert_never(workload)

    @property
    def total(self) -> int:
        return self.interactive + self.background + self.batch


def pools_for(slots_per_worker: int) -> ClassPools:
    """Split one worker's slots across the three classes.

    `brain.runtime.choose_workers` already decides how many workers there are, and it does
    it against memory, cores and the pooler's client slots rather than `os.cpu_count()`,
    leaving roughly `pool_slots // 20` workers with about twenty slots each. This is the
    split of those twenty, which is the number that actually stops an ingestion run from
    making the chat slow: the class ceilings decide how much *work* a class may start, and
    the pools decide whether the work it did start can hold a connection the request path
    needs.

    Fewer than three slots refuses rather than degrades. A split that cannot give each
    class one connection has not isolated anything, and reporting three pools of which two
    are empty is worse than saying the box is too small.
    """
    if slots_per_worker < 3:
        msg = (
            f"{slots_per_worker} slot(s) per worker cannot be split three ways; "
            "a pool that cannot give each class one connection isolates nothing"
        )
        raise ValueError(msg)
    background = max(1, math.floor(slots_per_worker * POOL_SHARE[WorkloadClass.BACKGROUND]))
    batch = max(1, math.floor(slots_per_worker * POOL_SHARE[WorkloadClass.BATCH]))
    # The remainder goes to interactive rather than being rounded to it, so the three
    # always sum to exactly the slots that exist. Rounding each independently overcommits
    # the pooler, and the symptom of an overcommitted pooler is a connection error on the
    # request path rather than a queue anywhere.
    return ClassPools(
        interactive=slots_per_worker - background - batch,
        background=background,
        batch=batch,
    )


# -------------------------------------------------------------------- the capacity model
@dataclass(frozen=True)
class CapacityProfile:
    """A sizing, stated as peak concurrency rather than as daily volume (M22.3.1).

    Daily volume is the number everybody has and nobody can size against. Five thousand
    questions a day is 0.06 a second, which sizes a machine that is never busy; what
    queues is the busiest minute, and the busiest minute of an office system is
    lunchtime-shaped rather than uniform.
    """

    name: str
    #: Arrivals per second in the peak minute, not the daily mean.
    peak_arrivals_per_second: float
    #: How long one of them occupies a server, end to end.
    mean_service_seconds: float
    reason: str

    @property
    def concurrency(self) -> float:
        """Little's law: L = lambda W. The number of things in flight at the peak."""
        return little_law_concurrency(self.peak_arrivals_per_second, self.mean_service_seconds)

    @property
    def servers_needed(self) -> int:
        """Whole servers, rounded up, because half a slot serves nobody."""
        return max(1, math.ceil(self.concurrency))


def little_law_concurrency(arrivals_per_second: float, mean_service_seconds: float) -> float:
    """L = lambda W, and the only sizing rule this module uses.

    Written as a named function rather than inlined at three call sites because the queue
    estimate, the per-connector concurrency seeds and the profile sizing are all the same
    arithmetic, and three copies is how one of them gets a stray factor of sixty.
    """
    if arrivals_per_second < 0 or mean_service_seconds < 0:
        msg = "arrival rate and service time are both non-negative"
        raise ValueError(msg)
    return arrivals_per_second * mean_service_seconds


def seed_profiles() -> tuple[CapacityProfile, ...]:
    """Sizings recorded per profile (M22.3.2), against the deployment profiles in §23."""
    return (
        CapacityProfile(
            name="lite",
            peak_arrivals_per_second=0.5,
            mean_service_seconds=4.0,
            reason=(
                "0.1 requests/second is the estate mean; the peak minute is taken at 5x "
                "that. L = 0.5 x 4 = 2 model calls in flight at the peak."
            ),
        ),
        CapacityProfile(
            name="full",
            peak_arrivals_per_second=2.0,
            mean_service_seconds=4.0,
            reason=(
                "126 staff, sized for a peak of 2 questions a second. L = 2 x 4 = 8 in "
                "flight, comfortably inside the 40-call model budget."
            ),
        ),
        CapacityProfile(
            name="ha",
            peak_arrivals_per_second=8.0,
            mean_service_seconds=4.0,
            reason=(
                "The 10x trigger. L = 8 x 4 = 32 in flight, which is where the 40-call "
                "model budget starts to be the thing that binds rather than the box."
            ),
        ),
    )


# ------------------------------------------------------------- first bottleneck at scale
#: The documented answer to M22.3.4, and the arithmetic behind it is checked by a test
#: rather than left as prose.
#:
#: At 10x and at 100x the first ceiling reached on daily volume is **Xero's 5,000 calls a
#: day per tenant**, and it is not ours to raise: it belongs to the tenant and is shared
#: with every other integration the client runs. Nothing overtakes it, because the two
#: other verified sources state per-minute ceilings, and a per-minute ceiling implies
#: 144,000 a day sustained, roughly 29 times Xero's.
#:
#: What that arithmetic cannot see is burst shape, and that is where **Lark Base's 100
#: requests a minute** binds: a single minute of 120 calls is over a ceiling that no plan
#: raises, on a day whose total is nowhere near anything. So the 100x answer has two halves
#: and only one of them is on the ladder.
#:
#: Neither is answered by adding servers. Both are answered by projecting more and fetching
#: less, which is the argument §8 already makes for the projection existing at all: a
#: realistic question touches 5-20 records, and federating everything means 15,000-60,000
#: calls a day against a ceiling of 5,000.
FIRST_BOTTLENECK_AT_SCALE = (
    "10x and 100x on daily volume: Xero's 5,000 calls a day per tenant, shared with the "
    "client's other integrations and not ours to raise. On burst shape at any scale: Lark "
    "Base's 100 requests a minute, which no plan raises and which daily arithmetic cannot "
    "see. Neither is answered by adding servers; both are answered by projecting more and "
    "fetching less."
)


@dataclass(frozen=True)
class Ceiling:
    """An external limit we do not control, with whether money can move it.

    `raisable` is the field that changes what an operator does. Lark Base's per-minute
    ceiling is fixed by the vendor, so an alert about it must not be answered by anybody
    going to look for an upgrade button that does not exist.

    `derived` marks a daily figure that was calculated from a per-minute one rather than
    stated by the vendor. It matters because a derived daily ceiling always flatters the
    source: it assumes traffic arrives evenly, and real traffic does not, so the per-minute
    ceiling behind it binds earlier than the ladder can show. Reporting a derived figure as
    though the vendor published it is how a source gets declared safe at 40x and starts
    returning 429s at 8x.
    """

    name: str
    per_day: int
    raisable: bool = True
    derived: bool = False

    def __post_init__(self) -> None:
        if self.per_day < 1:
            msg = f"ceiling {self.name} has a non-positive daily limit"
            raise ValueError(msg)


@dataclass(frozen=True)
class Demand:
    """What we would ask of one ceiling at today's volume."""

    ceiling: Ceiling
    calls_per_day: int

    def __post_init__(self) -> None:
        if self.calls_per_day < 0:
            msg = f"demand on {self.ceiling.name} is negative"
            raise ValueError(msg)

    def binds_at(self) -> float:
        """The multiple of today's volume at which this ceiling is reached.

        Infinity when we ask nothing of it, which is honest: a source nobody calls is not a
        bottleneck at any scale, and reporting it as one at 100x would bury the real answer.
        """
        if self.calls_per_day == 0:
            return math.inf
        return self.ceiling.per_day / self.calls_per_day


@dataclass(frozen=True)
class Bottleneck:
    """The first ceiling reached at a given multiple of today's volume."""

    multiplier: float
    ceiling: Ceiling
    calls_per_day: int
    demand_at_multiplier: float
    binds_at: float
    reason: str


def first_bottleneck(demands: Sequence[Demand], *, multiplier: float) -> Bottleneck | None:
    """Which ceiling is reached first at `multiplier` times today's volume.

    None when nothing binds, which is a real answer and not a missing one: at 1x a system
    whose projection is working should have no bottleneck at all, and returning a
    least-headroom ceiling anyway would make every dashboard show a permanent red source.

    Ties break on the name so the answer is stable across runs rather than showing up as
    churn in the console.
    """
    if multiplier <= 0:
        msg = "a multiplier of today's volume is positive"
        raise ValueError(msg)
    bound = [d for d in demands if d.calls_per_day * multiplier >= d.ceiling.per_day]
    if not bound:
        return None
    worst = min(bound, key=lambda d: (d.binds_at(), d.ceiling.name))
    raisable = (
        "and a plan upgrade can move it"
        if worst.ceiling.raisable
        else "and no plan raises it, so the only answer is to ask for less"
    )
    derived = (
        " The daily figure is derived from a per-minute ceiling, so it assumes even arrival "
        "and the real limit binds earlier under burst."
        if worst.ceiling.derived
        else ""
    )
    return Bottleneck(
        multiplier=multiplier,
        ceiling=worst.ceiling,
        calls_per_day=worst.calls_per_day,
        demand_at_multiplier=worst.calls_per_day * multiplier,
        binds_at=worst.binds_at(),
        reason=(
            f"at {multiplier:g}x, {worst.ceiling.name} would take "
            f"{worst.calls_per_day * multiplier:,.0f} calls a day against a ceiling of "
            f"{worst.ceiling.per_day:,} ({raisable}); it binds at "
            f"{worst.binds_at():.1f}x.{derived}"
        ),
    )


def bottleneck_ladder(
    demands: Sequence[Demand], multipliers: Sequence[float] = (10.0, 100.0)
) -> tuple[tuple[float, Bottleneck | None], ...]:
    """The documented first bottleneck at each multiple (M22.3.4).

    Ten and a hundred by default, because those are the two the architecture asks for and
    because they are far enough apart to be different answers: ten is usually a ceiling
    somebody can buy their way past, and a hundred is usually one nobody can.
    """
    return tuple((m, first_bottleneck(demands, multiplier=m)) for m in multipliers)


#: What a load test has to reproduce (M22.3.3). A number, because "load test the system"
#: is not a specification and the thing being tested is the peak minute rather than the
#: daily total. The test itself is not domain logic and is not here; this is the target it
#: has to hit and the pass condition it has to check.
LOAD_TEST_TARGET = (
    "Drive the full profile's peak: 2 arrivals a second for ten minutes, with the mix of "
    "workload classes seen in production rather than interactive only. Pass conditions are "
    "the §25 service levels (ANSWER p95 under 8s, FAST p95 under 500ms, successful request "
    "rate above 99.5%) held while batch work runs concurrently, and zero interactive "
    "requests shed while any batch work is still being admitted."
)

# A `CapacityReport` aggregating the profiles, the current shedding and the ladder was
# written here and removed. Nothing constructs one: the console will want some such shape,
# and guessing it now means shipping a type whose fields were chosen by whoever wrote this
# rather than by the screen that has to render them.
