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

What is not claimed here: Procrastinate is not a dependency of this repository and no
worker process exists, so M32.4.1.1 and M32.4.1.4 are policy in this file and nothing
running. The connection rule and the concurrency budget are what a worker will have to
satisfy, and are tested as such.

Task ids: M32.4.2.1, M32.4.2.2, M32.4.2.3
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
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
