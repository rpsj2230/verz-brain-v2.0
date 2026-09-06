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

**A slot has a size as well as a class, and until now it had only one size.** `MIB_PER_SLOT`
is one number for every job in the fleet, and two containers are budgeted from it very
differently: `brain-worker` holds seven slots inside 384 MiB, and `brain-parse-worker` holds
one job inside 512 MiB because the job's size was chosen by whoever made the document. Both
of them drained `system`, because `queue_name_for` derived the queue from the traffic class
alone, so **the 50 MiB PDF the knowledge door admits could be fetched into a 48 MiB slot**.
`brain.knowledge.parse_budget` names that gap and says closing it needs a change to this
module's rule rather than to that one, which is what `SlotClass` is.

The rule that had to survive the change is the one `queue_name_for` exists for: a task author
may not choose a priority. Priority is whether a person is waiting, and the channel declared
that at ingress with no default. **Cost is not priority, and declaring a job expensive can
only ever make it slower**: it routes to the scarcer container that runs one at a time. So a
job may declare its cost and may not declare its class, and the queue name is derived from
both rather than written by anybody.

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

**Per-class concurrency is a process layout, not a setting, and that is the driver's doing.**
The driver takes one concurrency per worker process and a list of queues for it to drain, so
the only way it can enforce a per-class allocation is as separate processes over separate
queues. `worker_shards` is that translation, and `queue_name_for` derives the queue name
from the traffic class so a task author cannot choose a priority by choosing a name. A class
allocated zero gets no shard rather than an idle one: a worker that fetches nothing still
holds a connection and still reports itself up.

**A container has one memory limit, so it has one slot size, and that is why the slot class
belongs to the deployment rather than to the allocation.** The tempting shape is a mapping
keyed by class and size together, so that one worker could hold four cheap jobs or one
expensive one. It cannot: four standard slots and one whole-container slot are 4 x 48 plus
the whole of what is left, which is more than one container twice over. The choice between
those two shapes is a choice between two containers, and `BRAIN_WORKER_SLOT_CLASS` is where
it is made.

**The standard queue keeps its name and only the expensive one is new, and the asymmetry is
deliberate.** Renaming `system` to `system.standard` would leave a worker running the
previous image draining a queue nothing enqueues onto, which is the failure this whole module
is about: an empty queue and an absent queue are indistinguishable from every metric there
is. Leaving the name alone means an old worker keeps taking exactly the work it was always
safe to take and can never fetch the work that would kill it.

**The queue's own tables are not in `migrations/versions`, and `DRIVER_SCHEMA` is what keeps
that from being invisible.** See `THE_QUEUE_SCHEMA_IS_NOT_ALEMBICS`. The short version is
that the driver versions its own DDL, a transcribed copy forks at the driver's next release,
and the price of not transcribing it is tables that arrive with no row-level security. Naming
a schema `brain.db.SCHEMAS` already lists puts them inside the only sweep that would report
that, which converts a silent gap into a failing check.

**The deploy plan is the second half of the sentence above about the schema, and it did not
exist.** `THE_QUEUE_SCHEMA_IS_NOT_ALEMBICS` says the price of not transcribing the driver's
DDL is paid in two places rather than hidden: an operator runs the migration history and then
the driver's own command, and the driver's tables arrive with no row-level security. Nothing
anywhere said what that command was, in what order, or what has to follow it. `DEPLOY_PLAN`
is that runbook as data, `deploy_plan_gaps` checks the one property that makes it safe (the
row-level security step comes after the tables exist and is never optional), and
`driver_rls_statements` builds the statements for the tables that are actually there rather
than for a transcribed list, because a transcribed list is the fork this repository already
refused.

**The row-level security step enables and creates no policy, which is the opposite of every
other table here and is the point.** PostgreSQL denies every row to every role once the flag
is on and nothing admits them, and it exempts the table's owner, which is the role the
driver's own connection uses. So enabling denies `brain_app` and leaves the queue working. A
policy of `USING (true)` would read as thoroughness and would grant everything back, which is
a green sweep over a table nothing protects: the failure this repository keeps finding, in
the one place it would be invisible.

What is not claimed here, and it has not moved: **Procrastinate is not a dependency of this
repository**, so nothing in this file has ever fetched a job. M32.4.1.1 is the driver
running, and what is written is the shape a driver has to fit: the schema it may install
into, the queues it must drain, the connection it may not use, the steps that install it, and
the message it prints when it is absent. `brain.ops.worker` is the process that would run it
and refuses to start without it, so the absence is loud rather than a worker looping on an
empty queue. That absence is now asked rather than asserted: `driver_is_installed` looks for
the module, so the day the dependency lands the run mode stops printing a sentence that has
stopped being true.

Task ids: M32.4.1.3, M32.4.2.1, M32.4.2.2, M32.4.2.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol
from urllib.parse import urlsplit

from brain.db import SCHEMAS
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

#: What a PostgreSQL table name may be before `driver_rls_statements` interpolates it into
#: DDL. Unquoted lower-case identifiers only, which is what the driver creates and what the
#: catalogue returns for them; anything else is refused rather than quoted, because a name
#: needing quotes in this position is a name nobody in this stack chose.
_IDENTIFIER_RE: Final = re.compile(r"^[a-z_][a-z0-9_]*$")

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

#: How many jobs a container serving `SlotClass.WHOLE_CONTAINER` may hold at once.
#:
#: One, and it is arithmetic rather than a choice. A whole-container slot is the whole of
#: what is left after the container's own overhead, so a second one would be the same memory
#: promised twice out of a container that has one copy of it.
#: `brain.knowledge.parse_budget.PARSES_AT_ONCE` reaches the same number from the other end
#: and for the same reason; this constant is the queue's half, which is how many slots a
#: worker of that class may be allocated, and that one is how large the budget is. Neither
#: can compute the other: the size of the budget needs the door's ceilings, and the number of
#: slots needs the process layout.
WHOLE_CONTAINER_SLOTS: Final = 1

#: An argument value longer than this is content, not an identifier. A UUID is 36
#: characters; a scope path or a tool name is shorter than this; a sentence is not.
#:
#: `brain.ops.checkpoints` imports this rather than choosing its own. A checkpoint and a job
#: row are the same question asked twice - what may be written to a store the redactor does
#: not reach - and two numbers for it would drift in the direction of whichever store somebody
#: was debugging that week.
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


class SlotClass(enum.StrEnum):
    """How much of a container one of these jobs holds while it runs.

    Two values, for the reason `Redrive` has two: a middle rung is selected by whoever is in
    a hurry, and here the arithmetic supports exactly two anyway. Either the cost is a figure
    this repository chose, in which case a container holds several of them, or the cost was
    chosen by whoever made the input, in which case the container holds one.

    **This is not a priority and cannot be used as one.** `TrafficClass` decides who is served
    first and a task may not choose it. A task may choose this, because choosing
    `WHOLE_CONTAINER` moves the job to the container that runs one job at a time, which is
    strictly slower. There is no value here that buys anything.
    """

    #: Bounded by something we wrote: a page size, a batch, a retry budget. `MIB_PER_SLOT`
    #: is what one costs, and a container holds as many as its limit divided by that.
    STANDARD = "standard"
    #: Bounded by an input somebody outside this company chose. One at a time, and the slot
    #: is the whole of what is left after the container's own overhead. A parse is the only
    #: one today; see `brain.knowledge.parse_budget` for what that costs and why.
    WHOLE_CONTAINER = "whole_container"


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
    slot_class: SlotClass = SlotClass.STANDARD,
) -> tuple[str, ...]:
    """Every reason a concurrency allocation will not run inside the worker's memory limit.

    A traffic class with no allocation is a class whose jobs are never fetched, whichever
    slot class this container serves. That check is first and is unconditional: it presents
    as a queue that fills and never drains and takes a day to find.

    **The arithmetic after it depends on the slot class, and using one of them for both was
    the defect.** A standard slot is `MIB_PER_SLOT` and a container holds as many as fit, so
    the question is a sum against the limit. A whole-container slot is the whole of what is
    left, so the sum is meaningless and the question is how many there are: more than
    `WHOLE_CONTAINER_SLOTS` promises the same container out that many times, and none at all
    is a container that drains nothing while reporting itself up. Multiplying a
    whole-container allocation by `MIB_PER_SLOT` is what let one parse slot cost 48 MiB on
    paper in a container sized at 512 for it.

    What is deliberately not here is how large a whole-container slot is. That needs the
    knowledge door's ceilings and the container's own reserve, which
    `brain.knowledge.parse_budget` holds and `brain.ops.worker` asks it for. This module
    knows the shape of the allocation and not the size of a document, and an ops module
    importing the knowledge layer to find out would be the cycle that shape avoids.

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
    slots = sum(allocation.values())
    limit = component(worker_component).memory_mib
    if slot_class is SlotClass.WHOLE_CONTAINER:
        if slots != WHOLE_CONTAINER_SLOTS:
            findings.append(
                f"{worker_component!r} serves {slot_class.value} slots and is allocated "
                f"{slots} of them; one such slot is the whole of what is left of "
                f"{limit} MiB, so {WHOLE_CONTAINER_SLOTS} is the only number that is not "
                "either a container promised out twice or a container that drains nothing"
            )
        return tuple(findings)
    wanted = slots * MIB_PER_SLOT
    if wanted > limit:
        findings.append(
            f"{slots} slots at {MIB_PER_SLOT} MiB is {wanted} MiB, over "
            f"the {limit} MiB limit on {worker_component!r}"
        )
    return tuple(findings)


def pooler_url_findings(url: str) -> tuple[str, ...]:
    """Every sign that this connection string goes through a transaction pooler.

    Separate and public because **the queue is not the only thing that must not go behind
    one**. `brain.ops.checkpoints` asks the identical question about the checkpointer's
    connection, and a second copy of this would be the fourth copy of a pooler rule in this
    repository. The house argument against that is in `brain.knowledge.uploads`: the copy
    that drifts is the one nobody is looking at, and every failure in this family is silent,
    so the drift is discovered by the thing it was meant to prevent.

    What is deliberately not here is the consequence. A pooler breaks LISTEN for the queue
    and breaks server-side prepared statements for the checkpointer, and those are different
    sentences to write in a refusal. So this reports the pooler's own behaviour, which is
    true whoever is asking, and each caller adds what it means for them.
    """
    findings: list[str] = []
    split = urlsplit(url)
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


def queue_url_refusals(queue_url: str, *, app_url: str = "") -> tuple[str, ...]:
    """Every reason this connection string is wrong for a queue, in words that name the fix.

    Returns all of them. A worker pointed at the pooler and sharing the application's URL
    has two problems, and fixing one of them produces a configuration that is still wrong
    in a way that raises nothing.

    The sharing check is first and is this function's own, because it is the only one of the
    three that needs to know what the application is connected to. The other two are
    `pooler_url_findings`, asked of the queue URL alone.
    """
    findings: list[str] = []
    if app_url and queue_url == app_url:
        findings.append(
            "the queue is using the application's own connection string, which goes through "
            "the transaction pooler; LISTEN/NOTIFY binds to a backend connection and "
            "transaction pooling moves it, so the worker stops being notified and reports "
            "nothing. Give the worker a session-mode or direct URL."
        )
    return (*findings, *pooler_url_findings(queue_url))


# ----------------------------------------------------------------- the driver's own needs
#: Where the queue's own tables go. `brain.db.SCHEMAS` describes `ops` as "scheduled jobs,
#: budgets, deployment records", which is what a job row is.
#:
#: The driver's default is `public`, and `public` is the one answer this repository has
#: already ruled out. `brain.db` puts it plainly: a table there is "a table nobody decided
#: the classification of". The mechanical half matters more than the tidiness half:
#: `brain.ops.sweeps.sweep_rls` reads `SCHEMAS` and looks nowhere else, so a queue installed
#: into `public` is not merely undeclared, it is outside the reach of the only check that
#: would report its tables having no row-level security.
DRIVER_SCHEMA: Final = "ops"

#: Why the queue's tables are not in `migrations/versions`, and what that costs.
THE_QUEUE_SCHEMA_IS_NOT_ALEMBICS = (
    "The driver ships and versions its own DDL and applies it with its own command. "
    "Transcribing that into an Alembic revision would fork it: our copy would be the one "
    "the deploy runs and the driver's would be the one its code expects, and they diverge "
    "at the driver's next release with no error until a query names a column that is not "
    "there. So the queue schema is a deploy step and not a migration, and the price is "
    "paid in two places rather than hidden. There is one migration history for our tables "
    "and one command for the driver's, which is two things an operator must run; and the "
    "driver's tables arrive with no row-level security on them, which is why "
    "DRIVER_SCHEMA puts them somewhere sweep_rls is already looking. A loud failure in a "
    "sweep beats a silent one in a schema nobody enumerates."
)

#: How long a worker waits on a notification before polling anyway.
#:
#: Not a tuning knob: it is the whole of what is left when the notification path is broken.
#: A worker behind a transaction pooler receives no notifications and raises nothing, so
#: this interval becomes the queue's entire latency, and a value chosen for tidiness (a
#: minute, say) turns that failure into "the system is slow this week" rather than into
#: something anybody investigates. Five seconds is short enough that the degraded mode is
#: survivable and long enough that an idle worker is not a poller against a shared database.
FALLBACK_POLL_SECONDS: Final = 5

#: What a worker says when it has nothing to fetch with.
#:
#: Worded here because this is the one module permitted to name an implementation, and
#: `brain.ops.worker` prints it rather than spelling the name itself. That is the seam doing
#: its job in the least glamorous way available: even the error message about the driver
#: being missing lives in the file the swap would replace.
NO_DRIVER_IS_INSTALLED = (
    "no queue driver is installed. procrastinate is not a dependency of this project, so "
    "there is no queue to fetch from and this process would otherwise sit in a loop "
    "reporting an empty queue, which is indistinguishable from a queue with nothing in it. "
    "It exits instead, and says which of the two it is."
)


def driver_schema_gaps(schema: str = DRIVER_SCHEMA) -> tuple[str, ...]:
    """Every reason the queue may not be installed into this schema.

    A parameter with a default rather than a check over the constant beside it, for the
    reason `concurrency_gaps` gives about itself: a check that can only ever be run against
    the one value it lives next to cannot be shown to fail, and a check nobody has seen fail
    is a check nobody knows works.
    """
    findings: list[str] = []
    if not schema.strip():
        findings.append(
            "the queue names no schema, so its tables land wherever search_path points, "
            "which on a fresh connection is public"
        )
        return tuple(findings)
    if schema not in SCHEMAS:
        findings.append(
            f"schema {schema!r} is not one brain.db.SCHEMAS names, so brain.ops.sweeps."
            "sweep_rls does not look in it; a queue table with no row-level security there "
            f"is never reported. Known: {sorted(SCHEMAS)}"
        )
    return tuple(findings)


# ----------------------------------------------------------------- the deploy step
#: The module a driver would be imported from. Named here because this is the one file
#: permitted to name an implementation, and `driver_is_installed` needs a string to look for.
DRIVER_IMPORT_NAME: Final = "procrastinate"

#: The command that creates the driver's own tables, with the search path that puts them in
#: `DRIVER_SCHEMA`. The driver's DDL is unqualified, so the schema is decided by the
#: connection rather than by an argument, exactly as `brain.ops.checkpoints` decides the
#: saver's. `<app>` is left as a placeholder because there is no application object to name
#: yet, and writing a plausible one would make a runbook that fails on its first line look
#: like a runbook that works.
DRIVER_SCHEMA_COMMAND: Final = (
    'PGOPTIONS="-c search_path=' + DRIVER_SCHEMA + '" procrastinate --app=<app> schema --apply'
)


def driver_is_installed() -> bool:
    """Whether a queue driver can be imported in this process.

    Asked rather than assumed. `brain.ops.worker` printed `NO_DRIVER_IS_INSTALLED` on every
    run, unconditionally, which was true and would have gone on being printed on the first
    day it was false. A sentence that cannot stop being said is not a report.

    `find_spec` rather than an import, because importing a queue driver connects nothing but
    does pull in its dependency tree, and this is called from a preflight whose whole purpose
    is to fail fast and say why.
    """
    import importlib.util

    return importlib.util.find_spec(DRIVER_IMPORT_NAME) is not None


@dataclass(frozen=True)
class DeployStep:
    """One thing an operator runs, and what is not true until they have.

    `why` is required and is not a description of the command. It is what is still broken
    after the previous step and before this one, because a runbook whose steps say what they
    do is a runbook whose steps get skipped when they look redundant.
    """

    order: int
    what: str
    why: str
    #: Whether the deploy may proceed without this step. Only the schema-creation step is
    #: ever skippable, and only because a schema that already exists is the ordinary case.
    optional: bool = False

    def __post_init__(self) -> None:
        for name in ("what", "why"):
            if not str(getattr(self, name)).strip():
                msg = f"deploy step {self.order} has no {name}; a step with no {name} is a habit"
                raise QueueError(msg)


DEPLOY_PLAN: Final[tuple[DeployStep, ...]] = (
    DeployStep(
        order=1,
        what=f"add {DRIVER_IMPORT_NAME} to the project dependencies and lock it",
        why=(
            "there is no driver, so no job has ever been enqueued or fetched and every step "
            "below describes a command that cannot be run yet. This is the whole of what "
            "stands between the rest of this plan and a working queue"
        ),
    ),
    DeployStep(
        order=2,
        what=f"CREATE SCHEMA IF NOT EXISTS {DRIVER_SCHEMA}",
        why=(
            "the driver's DDL is unqualified and lands wherever search_path points, which on "
            "a fresh connection is public. Optional only because the migration history "
            "already creates this schema on any database the application has started against"
        ),
        optional=True,
    ),
    DeployStep(
        order=3,
        what=DRIVER_SCHEMA_COMMAND,
        why=(
            "the driver versions its own tables and applies them with its own command. "
            "Transcribing that into an Alembic revision would fork it at the driver's next "
            "release with no error until a query names a column that is not there; see "
            "THE_QUEUE_SCHEMA_IS_NOT_ALEMBICS. Run on a direct connection, never the pooler"
        ),
    ),
    DeployStep(
        order=4,
        what=(
            "for every table the step above created: ALTER TABLE <table> ENABLE ROW LEVEL "
            "SECURITY. See driver_rls_statements, which builds these from the tables that "
            "are there rather than from a list that would fork the same way the DDL would"
        ),
        why=(
            "the driver's tables arrive with row-level security off. They are in a schema "
            "brain.ops.sweeps.sweep_rls enumerates, so until this has run the sweep is red, "
            "and a red sweep with no remedy written down is a sweep somebody switches off"
        ),
    ),
)


def driver_rls_statements(tables: Sequence[str], *, schema: str = DRIVER_SCHEMA) -> tuple[str, ...]:
    """The row-level security statements for the tables the driver created.

    **One statement per table and no policy, which is the opposite of every migration in this
    repository and is deliberate.** PostgreSQL denies every row to every role once the flag is
    on and no policy admits them, and it exempts the table's owner, which is the role the
    driver's own connection uses. So this leaves the queue working and denies `brain_app`,
    which has no business selecting from a queue table: the application enqueues through the
    driver, not with a SELECT. A policy of `USING (true)` would read as thoroughness, would
    grant everything straight back, and would leave a green sweep over a table nothing
    protects.

    Built from the tables that are there rather than from a list held here. A transcribed list
    forks at the driver's next release exactly as a transcribed CREATE would, and it fails in
    the quietest possible direction: a table the driver added that this list does not name is
    a table with no row-level security, in a schema whose whole reason for being chosen was
    that somebody would notice.

    The identifier is checked rather than quoted because DDL cannot take a parameter, so this
    interpolates. The names come from the catalogue of a database we have just written to,
    which is not a hostile source and is not a reason to skip the check: the cost of being
    wrong here is arbitrary DDL and the cost of the check is a regular expression.
    """
    statements: list[str] = []
    for table in tables:
        if not _IDENTIFIER_RE.match(table):
            msg = (
                f"table name {table!r} is not a bare identifier, and this builds DDL by "
                "interpolation because DDL takes no parameters"
            )
            raise QueueError(msg)
        statements.append(f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY")
    return tuple(statements)


def deploy_plan_gaps(plan: Sequence[DeployStep] | None = None) -> tuple[str, ...]:
    """Every way this runbook could be followed and still leave the queue wrong.

    Three properties, and each one is a way a plan stays green while the thing it describes
    is broken.

    The steps must be consecutively ordered from one, because a plan with a gap in it is a
    plan somebody has edited a step out of, and the step most likely to look removable is the
    last one.

    The row-level security step must come after the step that creates the tables. It cannot
    be run before them and there is no migration that could: the tables do not exist until
    the driver's command has run. Stating it as a check rather than as a comment is what
    stops the plan being reordered into something that reads tidier and does nothing.

    And **only the step that creates the schema may be skipped**, which is what
    `DeployStep.optional` says the field is for and which nothing enforced until a mutation
    marked the driver's own command optional and no test noticed. Two steps fail differently
    when they are skipped and the message says which: skipping the driver's command leaves a
    queue with no tables, and skipping the security step leaves a working queue whose tables
    are readable by any role that can reach the database, with nothing to say so but a sweep
    whose fix is the step that was skipped. One condition and two sentences, rather than two
    checks: two checks report one keyword twice, and neither can be shown to fail on its own
    while the other is there.

    The plan is a parameter defaulting to the declared one, for the reason `concurrency_gaps`
    takes one: a check that can only be run against the constant beside it cannot be shown to
    fail.
    """
    steps = DEPLOY_PLAN if plan is None else tuple(plan)
    findings: list[str] = []
    if [s.order for s in steps] != list(range(1, len(steps) + 1)):
        findings.append(
            f"the deploy steps are numbered {[s.order for s in steps]}, which is not "
            "1..n; a gap is a step somebody removed and the removable-looking one is last"
        )
    rls = [s for s in steps if "ROW LEVEL SECURITY" in s.what]
    ddl = [s for s in steps if DRIVER_IMPORT_NAME in s.what and "schema" in s.what]
    if not rls:
        findings.append(
            "no step enables row-level security, so the driver's tables stay readable by "
            "every role and sweep_rls stays red with no remedy beside it"
        )
    elif ddl and min(s.order for s in rls) < max(s.order for s in ddl):
        findings.append(
            "row-level security is enabled before the command that creates the tables, and "
            "there is nothing to enable it on until that has run"
        )
    # One check for one edit, and one finding for it. There were briefly two here, a rule
    # about the security step and a rule about every other step, and a mutation showed the
    # cost of that: disabling either left the other reporting, so neither could be shown to
    # fail on its own, and an operator who marked one step optional would have read two
    # findings about one keyword. `brain.ops.queue.pooler_url_findings` states the same rule
    # about itself: reporting one mistake twice trains whoever reads the output to skim it.
    #
    # The consequence is what differs between the steps, not the rule, so the message
    # branches and the condition does not.
    findings.extend(
        f"step {s.order} is marked optional; the only step that may be skipped is the one a "
        "database the application has started against has already run, which is the schema. "
        + (
            "Skipping this one leaves a working queue whose tables nothing protects."
            if "ROW LEVEL SECURITY" in s.what
            else "Skipping this one leaves a queue with no tables at all."
        )
        for s in steps
        if s.optional and "CREATE SCHEMA" not in s.what
    )
    return tuple(findings)


@dataclass(frozen=True)
class Shard:
    """One worker process's share of the queue: which queue it drains, and how widely.

    A shard exists because the driver takes one concurrency per worker process and a list
    of queues for it to drain. **Per-class concurrency is therefore not a setting, it is a
    process layout**, and expressing it as anything else means one number for the whole
    worker, which is the failure this module's opening argument names: a backfill's
    thousand automation jobs occupy every slot and the reply somebody is waiting for sits
    behind them.
    """

    queue: str
    traffic_class: TrafficClass
    concurrency: int
    #: Which container shape this shard belongs to. Defaults to the one that existed before
    #: there was a choice, so a caller written against the earlier signature keeps meaning
    #: what it meant.
    slot_class: SlotClass = SlotClass.STANDARD

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            # A shard is a process. One with no slots holds a database connection, drains
            # nothing, and reports itself healthy, which is a connection spent on a queue
            # that was deliberately never to be drained. The classes allocated zero get no
            # shard at all rather than an idle one; see `worker_shards`.
            msg = (
                f"shard {self.queue!r} has {self.concurrency} slots; a worker that fetches "
                "nothing still holds a connection and still reports itself up"
            )
            raise QueueError(msg)
        derived = queue_name_for(self.traffic_class, self.slot_class)
        if self.queue != derived:
            # The guard behind `queue_name_for`. Deriving the name in one function and then
            # letting a caller pass any string beside it leaves the rule enforced only where
            # somebody remembered to use the function, and the shard is the one place a
            # queue name is written down for a process to drain.
            msg = (
                f"shard queue {self.queue!r} is not the name "
                f"{self.traffic_class.value}/{self.slot_class.value} derives, which is "
                f"{derived!r}; a hand-written queue name is how a task chooses its own "
                "priority or lands in a container that cannot hold it"
            )
            raise QueueError(msg)


def queue_name_for(traffic_class: TrafficClass, slot_class: SlotClass = SlotClass.STANDARD) -> str:
    """The queue a job of this class and cost is enqueued onto.

    Derived from both and from nothing else. A queue name chosen per task would let a task
    author choose a priority, and the thing that decides priority here is whether a person is
    waiting, which the channel already declared at ingress with no default. Deriving the name
    means a new traffic class cannot be added without its queue existing, and a task cannot
    smuggle itself into the interactive share by being named well.

    **The standard slot class contributes nothing to the name and the other one does.** That
    reads as an inconsistency and is the safe direction. Renaming `system` to
    `system.standard` would strand a worker running the previous image on a queue nothing
    enqueues onto, which is indistinguishable from an idle queue in every metric there is,
    and it would do that to the container that was always safe to run. Leaving the name alone
    means an older worker keeps taking exactly the work it can hold and cannot fetch the work
    that would kill it, which is the failure that matters of the two.
    """
    if slot_class is SlotClass.STANDARD:
        return traffic_class.value
    return f"{traffic_class.value}.{slot_class.value}"


def worker_shards(
    concurrency: Mapping[TrafficClass, int] | None = None,
    slot_class: SlotClass = SlotClass.STANDARD,
) -> tuple[Shard, ...]:
    """The worker processes this allocation describes, in traffic-class order.

    A class allocated zero is omitted rather than given an idle shard. That is the whole
    reason this returns a tuple of shards instead of the mapping it was built from:
    `HUMAN_INTERACTIVE` is deliberately zero, and the natural loop over a mapping produces a
    worker for it which then waits for ever on a queue nothing is meant to enqueue onto.

    Ordered by the enum rather than by size, so two runs of a deploy produce the same
    process list and a diff of the plan is a diff of the decision.

    One slot class for the whole allocation rather than one per class, because a container has
    one memory limit and therefore one slot size. Two containers is how the fleet expresses
    four cheap jobs or one expensive one, and `BRAIN_WORKER_SLOT_CLASS` is where a container
    says which of the two it is.
    """
    allocation = CONCURRENCY if concurrency is None else concurrency
    return tuple(
        Shard(
            queue=queue_name_for(traffic_class, slot_class),
            traffic_class=traffic_class,
            concurrency=allocation[traffic_class],
            slot_class=slot_class,
        )
        for traffic_class in TrafficClass
        if allocation.get(traffic_class, 0) > 0
    )


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
