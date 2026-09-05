"""The job queue: one file, so that replacing it is one file.

Durable execution is the part of the stack most likely to be swapped, because it is the
part whose limits are only discovered under real load. So the shape here is deliberately a
seam: a protocol the rest of the system talks to, a driver module named as a constant, and
a recorded alternative with the measurement that would make us take it. A sweep asserts
that no module outside this one names either implementation, which is what turns "the swap
changes one file" from an intention into a fact that stops being true loudly.

**The queue does not go behind the transaction pooler, and this is the third time that
sentence has cost something.** `brain.migrate` moved from `pg_advisory_lock` to
`pg_advisory_xact_lock`; `brain.session` sets `prepare_threshold=None`; and a job queue
adds the worst of the three, because `LISTEN` behind a transaction pooler raises nothing.
The listener is simply moved to another backend and stops receiving notifications, so the
worker polls on its fallback timer, throughput drops to whatever the poll interval allows,
and every metric says the queue is empty. `queue_url_refusals` refuses the configurations
that produce it - most importantly the one that is easy to write and impossible to see,
which is a worker handed the application's own `DATABASE_URL`.

**A job carries identifiers, never records.** A queue table is a copy of business data with
a different retention, no row-level security, and a habit of being read in psql during an
incident. `Job` refuses arguments that look like content rather than references, which is
the same rule `brain.ops.tracing` applies to spans and `brain.core.redaction` applies to
traces: the places data gets copied to are the places its permissions stop travelling with
it.

**Concurrency is per traffic class, not per worker.** One number for the whole worker means
a backfill's thousand queued jobs occupy every slot and the reply somebody is waiting for
sits behind them. `brain.gate.context.TrafficClass` already draws the line that matters -
whether a person is waiting - so the slots are allocated along it. `HUMAN_INTERACTIVE` gets
zero, which is not an oversight: a person watching a cursor is answered in the request, and
a queue in that path adds a hop that can only make the wait longer.

Rejected: sizing concurrency from the host's core count. The constraint on this box is
memory, not CPU, and the box is shared - `brain.ops.wiring` holds the budget. Eight workers
that fit the cores and not the memory limit are eight workers that get OOM-killed together.

**Crash recovery is a heartbeat in a row, not a lock in a session, and that is the fourth
time transaction pooling has decided a design here.** The elegant construction is a session
advisory lock per running job: the backend dies with the worker, the lock goes, and a job
holding no lock is orphaned. Behind PgBouncer in transaction mode that construction reports
every running job as orphaned within milliseconds, because the lock is held by whichever
backend served that one transaction and is released when it ends. So `verdict_for` reads a
timestamp the worker wrote in its own transaction, which is state in a row and survives
being handed a different backend.

**Re-drive is not retry, and the two are counted separately.** A retry is the job asking
for another go after failing. A re-drive is the machine dying underneath a job that never
reached a verdict. Sharing one counter means a job on a host that OOM-kills twice a night
exhausts its retry budget and is dead-lettered as though it were a bad job - on the one
host where OOM kills are the expected failure, because it is shared with somebody else's
production.

**A job that is not safe to repeat is never re-driven, and not declaring is not declaring
it safe.** `Redrive.UNSAFE` is the default. Re-driving a job that has already sent an email
sends it twice, and nothing downstream can tell that from two people asking. Those go to
`QUARANTINE`, where a person decides, because the honest state of an interrupted side
effect is "nobody knows whether it happened".

What is not claimed here: Procrastinate is not a dependency of this repository and no
worker process exists, so M32.4.1.1 and M32.4.1.4 are policy in this file and nothing
running. The connection rule and the concurrency budget are what a worker will have to
satisfy, and are tested as such.

Task ids: M32.4.1.3, M32.4.2.1, M32.4.2.2, M32.4.2.3
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol
from urllib.parse import urlsplit

from brain.gate.context import TrafficClass
from brain.ops.wiring import component

#: The one module a swap changes. Named as a constant so the sweep that enforces it and
#: the docstring that claims it cannot drift apart.
DRIVER_MODULE: Final = "brain.ops.queue"

#: Implementation names that may appear only in `DRIVER_MODULE`. Both are checked, not only
#: the one in use: a half-finished migration that leaves Hatchet imports in three modules is
#: exactly the state this constant exists to catch.
DRIVER_IMPLEMENTATIONS: Final[tuple[str, ...]] = ("procrastinate", "hatchet")

#: Hostnames that are a transaction pooler on this stack. From the compose service name,
#: which is what the container network resolves.
POOLER_HOSTNAMES: Final[frozenset[str]] = frozenset({"pgbouncer", "pgbouncer-transaction"})

#: Query parameters that only ever appear because somebody knew they were behind a pooler.
#: Their presence on a queue URL is a confession, and it is the most useful signal available
#: because the alternative failure is silent.
_POOLER_MARKERS: Final[tuple[str, ...]] = ("pgbouncer", "prepare_threshold", "prepared_statements")

#: Roughly what one in-flight job costs: a database connection, a task's own working set,
#: and the interpreter's share. Deliberately generous. Under-counting produces a worker
#: that fits on paper and is killed by the cgroup under the first burst.
MIB_PER_SLOT: Final = 48

#: How many jobs may run at once, per traffic class.
CONCURRENCY: Final[Mapping[TrafficClass, int]] = {
    #: Zero, and not because it is small. A person waiting is answered inside the request;
    #: putting that path through a queue adds a hop whose only possible effect is delay.
    TrafficClass.HUMAN_INTERACTIVE: 0,
    #: The largest share. Somebody asked and will read the reply, so throughput here is
    #: what the queue is for.
    TrafficClass.HUMAN_ASYNC: 4,
    #: Deliberately less than async. Nobody is waiting, and automation is the class that
    #: arrives in bursts of a thousand.
    TrafficClass.AUTOMATION: 2,
    #: One. Housekeeping must never hold a slot that interactive or async work needs, which
    #: is the same rule `TrafficClass.SYSTEM` already states about pooler slots.
    TrafficClass.SYSTEM: 1,
}

#: An argument value longer than this is content, not an identifier. A UUID is 36
#: characters; a scope path or a tool name is shorter than this; a sentence is not.
MAX_ARGUMENT_CHARS: Final = 128


class QueueError(Exception):
    """Raised when a job, a connection or a concurrency allocation cannot be deployed."""


class Redrive(enum.StrEnum):
    """Whether a job interrupted mid-flight may be run again without asking anybody.

    Two values and no third. A "probably safe" rung would be selected by whoever is in a
    hurry, and the failure it produces - one duplicated side effect, weeks later, in a
    client's inbox - arrives too far from the decision for anybody to connect the two.
    """

    #: Running it twice changes nothing that the first run already changed.
    SAFE = "safe"
    #: It does something the world can see. A second run is a second thing done.
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class Job:
    """One unit of deferred work: what to run, for whom, and which identifiers it needs.

    `args` holds references. The record itself is fetched by the task, through the same
    gate as everything else, with the caller's entitlements resolved at the time it runs
    rather than at the time it was queued. That ordering matters on its own: entitlements
    granted an hour ago and revoked since must not still be in a queue row.
    """

    task: str
    traffic_class: TrafficClass
    args: Mapping[str, str | int] = field(default_factory=dict)
    #: Whether this job may simply be run again after its worker died mid-flight. Defaults
    #: to unsafe, so a task author who has not thought about it gets the answer that cannot
    #: send a second email.
    redrive: Redrive = Redrive.UNSAFE

    def __post_init__(self) -> None:
        if not self.task.strip():
            msg = "job has no task name"
            raise QueueError(msg)
        for key, value in self.args.items():
            if isinstance(value, str) and len(value) > MAX_ARGUMENT_CHARS:
                msg = (
                    f"job {self.task!r} argument {key!r} is {len(value)} characters; "
                    f"over {MAX_ARGUMENT_CHARS} it is content rather than a reference, and a "
                    "queue table has no row-level security and its own retention"
                )
                raise QueueError(msg)


class QueueDriver(Protocol):
    """What the rest of the system may ask of a job queue.

    Narrow on purpose, and narrow in a specific direction: nothing here exposes a
    transaction, a connection or a session. A driver that handed one out would let a caller
    take a session-level lock through it, which is the failure this module exists to
    prevent, and it would make the swap in `SWAP_CANDIDATES` impossible because Hatchet has
    no Postgres session to hand out.
    """

    def enqueue(self, job: Job) -> str: ...

    def fetch(self, traffic_class: TrafficClass, limit: int) -> Iterator[tuple[str, Job]]: ...

    def complete(self, job_id: str) -> None: ...

    def fail(self, job_id: str, retry_after_seconds: int) -> None: ...


def total_slots() -> int:
    return sum(CONCURRENCY.values())


def concurrency_gaps(
    concurrency: Mapping[TrafficClass, int] | None = None,
    *,
    worker_component: str = "brain-worker",
) -> tuple[str, ...]:
    """Every reason a concurrency allocation will not run inside the worker's memory limit.

    Two checks. A traffic class with no allocation is a class whose jobs are never fetched,
    which presents as a queue that fills and never drains and takes a day to find. And the
    total has to fit the limit `brain.ops.wiring` gives the worker, because on this host the
    limit is real: exceeding it is an OOM kill, not a slowdown.

    The allocation is a parameter defaulting to the declared one. A check that could only
    ever be run against the constant it lives beside cannot be shown to fail, and a check
    nobody has seen fail is a check nobody knows works.
    """
    allocation = CONCURRENCY if concurrency is None else concurrency
    findings: list[str] = []
    for traffic_class in TrafficClass:
        if traffic_class not in allocation:
            findings.append(
                f"{traffic_class.value}: no concurrency allocated, so its jobs are never fetched"
            )
    wanted = sum(allocation.values()) * MIB_PER_SLOT
    limit = component(worker_component).memory_mib
    if wanted > limit:
        findings.append(
            f"{sum(allocation.values())} slots at {MIB_PER_SLOT} MiB is {wanted} MiB, over "
            f"the {limit} MiB limit on {worker_component!r}"
        )
    return tuple(findings)


def queue_url_refusals(queue_url: str, *, app_url: str = "") -> tuple[str, ...]:
    """Every reason this connection string is wrong for a queue, in words that name the fix.

    Returns all of them. A worker pointed at the pooler and sharing the application's URL
    has two problems, and fixing one of them produces a configuration that is still wrong
    in a way that raises nothing.
    """
    findings: list[str] = []
    if app_url and queue_url == app_url:
        findings.append(
            "the queue is using the application's own connection string, which goes through "
            "the transaction pooler; LISTEN/NOTIFY binds to a backend connection and "
            "transaction pooling moves it, so the worker stops being notified and reports "
            "nothing. Give the worker a session-mode or direct URL."
        )
    split = urlsplit(queue_url)
    host = (split.hostname or "").lower()
    if host in POOLER_HOSTNAMES:
        findings.append(
            f"host {host!r} is a transaction pooler. Session-level advisory locks taken "
            "through it are released by an unrelated transaction, and notifications are "
            "delivered to whichever backend happens to hold the connection."
        )
    # The query string only. The hostname is checked above, and matching markers against
    # the whole URL would report the pooler twice for one mistake - which trains whoever
    # reads the output to skim it.
    query = split.query.lower()
    for marker in _POOLER_MARKERS:
        if marker in query:
            findings.append(
                f"the URL carries {marker!r}, which is only ever set to work around a "
                "transaction pooler. A queue does not work around one; it does not use one."
            )
    return tuple(findings)


# ----------------------------------------------------------------- crash recovery
#: How often a running worker writes its heartbeat. Written in the worker's own
#: transaction, because a transaction pooler hands the next statement to a different
#: backend and anything held in a session does not survive that.
HEARTBEAT_SECONDS: Final = 15

#: How many missed heartbeats before a job is treated as orphaned. Four rather than one,
#: and the multiple is the whole point: a worker paused by memory pressure on a shared
#: host, or waiting on a slow connector, misses heartbeats while being perfectly alive.
#: Re-driving a job that is still running produces two of it, which for a job with a side
#: effect is worse than the job never finishing.
STALE_AFTER_HEARTBEATS: Final = 4

#: How many times a job may be re-driven before a person looks at it. A job that kills its
#: worker will kill the next one too, and an uncapped re-drive turns one poison pill into a
#: worker that never processes anything else.
MAX_REDRIVES: Final = 3

#: How far ahead of us a heartbeat may be before we call it clock skew rather than a
#: heartbeat. Two hosts, two clocks, and NTP not yet settled after a reboot.
CLOCK_SKEW_TOLERANCE_SECONDS: Final = 30


def stale_after() -> timedelta:
    return timedelta(seconds=HEARTBEAT_SECONDS * STALE_AFTER_HEARTBEATS)


@dataclass(frozen=True)
class InFlight:
    """A job the queue believes is running, as the row says.

    `redrives` is separate from any retry count the queue keeps, and that separation is the
    reason this dataclass exists rather than a flag on `Job`. A retry is the job asking for
    another go; a re-drive is the machine dying underneath one. Counting them together
    dead-letters healthy jobs on a host whose expected failure is an OOM kill.
    """

    job_id: str
    task: str
    worker_id: str
    heartbeat_at: datetime
    redrives: int = 0
    redrive: Redrive = Redrive.UNSAFE

    def __post_init__(self) -> None:
        if self.heartbeat_at.tzinfo is None:
            # Same rule as every other timestamp in this package. Two workers on two hosts
            # produce an ordering that cannot be compared, and the comparison is the only
            # thing this record is for.
            msg = f"heartbeat for {self.job_id!r} has no timezone"
            raise QueueError(msg)
        if self.redrives < 0:
            msg = f"job {self.job_id!r} reports {self.redrives} re-drives"
            raise QueueError(msg)


class Verdict(enum.StrEnum):
    """What to do with one in-flight job. Closed, because each value is a different action.

    There is deliberately no `UNKNOWN`. Every row gets a decision, and a sweep that can
    return "not sure" returns it for the rows nobody has thought about, which are exactly
    the rows that then sit in `running` for ever.
    """

    #: Its heartbeat is fresh. Leave it alone.
    RUNNING = "running"
    #: Orphaned, and safe to simply run again.
    REDRIVE = "redrive"
    #: Orphaned, and nobody can say whether its side effect happened. A person decides.
    QUARANTINE = "quarantine"
    #: Re-driven too often. It is doing this to workers, not having it done to it.
    DEAD_LETTER = "dead_letter"


def verdict_for(entry: InFlight, now: datetime) -> Verdict:
    """What becomes of this job. The order of the three checks is the design.

    Freshness first, unconditionally. A job that is running must never be dead-lettered
    because it happens to carry a high re-drive count from last week, and it must never be
    quarantined for being unsafe: it has not been interrupted, so there is nothing to
    decide.

    Then the cap, before the safety question. A job that has been through the cap is a
    poison pill whichever answer it gives about idempotency, and quarantining it instead
    would put the same row in front of a person over and over.

    A heartbeat in the future counts as fresh rather than as stale. Clock skew between two
    hosts is real and its safe reading is "still running": treating a future timestamp as
    old would re-drive live jobs during the exact window in which nobody trusts the clocks.
    `clock_skew` reports it separately, so the condition is visible rather than silently
    absorbed.

    That case is carried by the subtraction being signed rather than by a branch. There was
    an explicit `now < entry.heartbeat_at` clause here and mutation testing showed it was
    dead: a negative elapsed time is already under any positive threshold, so removing the
    clause changed nothing and no test could tell. A clause that reads as a guard and guards
    nothing is worse than its absence, because the next person to touch this trusts it. What
    must not appear is an `abs()` around the subtraction, which is the natural-looking edit
    that turns a future heartbeat back into an ancient one.
    """
    if (now - entry.heartbeat_at) <= stale_after():
        return Verdict.RUNNING
    if entry.redrives >= MAX_REDRIVES:
        return Verdict.DEAD_LETTER
    if entry.redrive is Redrive.SAFE:
        return Verdict.REDRIVE
    return Verdict.QUARANTINE


def redrive_plan(entries: Sequence[InFlight], now: datetime) -> dict[Verdict, tuple[str, ...]]:
    """Every in-flight job sorted into what happens to it, keyed by verdict.

    Every verdict is present as a key, including the empty ones. A caller writing
    `plan.get(Verdict.QUARANTINE, ())` reads as though quarantine were optional, and a
    recovery sweep whose most serious outcome can be missing by accident is a sweep that
    reports a clean run while jobs wait for somebody who was never told.
    """
    grouped: dict[Verdict, list[str]] = {verdict: [] for verdict in Verdict}
    for entry in entries:
        grouped[verdict_for(entry, now)].append(entry.job_id)
    return {verdict: tuple(job_ids) for verdict, job_ids in grouped.items()}


def clock_skew(entries: Sequence[InFlight], now: datetime) -> tuple[str, ...]:
    """Jobs whose heartbeat is far enough ahead of us to be a clock, not a heartbeat.

    Separate from the verdict on purpose. Skew is a host problem and re-driving is a queue
    problem, and folding one into the other would either re-drive live work or hide a
    misconfigured clock behind a sweep that says everything is running.
    """
    tolerance = timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    return tuple(
        f"{e.job_id!r} on worker {e.worker_id!r} heartbeat is "
        f"{(e.heartbeat_at - now).total_seconds():.0f}s in the future"
        for e in entries
        if e.heartbeat_at - now > tolerance
    )


# ----------------------------------------------------------------- the seam
@dataclass(frozen=True)
class SwapCandidate:
    """An alternative we have looked at, with the measurement that would make us take it.

    `trigger` and `measured_by` are both required and both prose. A recorded alternative
    with no trigger is a note that somebody once read a comparison page; the trigger is
    what makes it a decision that can be revisited by evidence rather than by mood, and
    `measured_by` is what stops the trigger being a feeling about how things seem.
    """

    name: str
    gives: str
    costs: str
    trigger: str
    measured_by: str

    def __post_init__(self) -> None:
        for name in ("name", "gives", "costs", "trigger", "measured_by"):
            if not str(getattr(self, name)).strip():
                msg = f"swap candidate is missing {name}; an alternative with no {name} is a rumour"
                raise QueueError(msg)


SWAP_CANDIDATES: Final[tuple[SwapCandidate, ...]] = (
    SwapCandidate(
        name="hatchet",
        gives=(
            "a queue that does not live in the application's own Postgres, so queue depth "
            "stops competing with query load for the same connections and the same disk"
        ),
        costs=(
            "another service with its own store on a host that is already over budget for "
            "the full profile, and a second place a job can be lost between"
        ),
        trigger=(
            "queue tables account for more than a fifth of database write throughput, or "
            "queue depth is still rising after the worker has been given its budgeted slots"
        ),
        measured_by=(
            "pg_stat_statements write share attributed to the queue tables, and queue depth "
            "sampled over an hour at full concurrency"
        ),
    ),
)
