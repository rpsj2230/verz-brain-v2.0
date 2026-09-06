"""The process that would drain the queue, and what it refuses to start without.

`brain.ops.queue` decides the policy and owns no connection. This is the other half: the
thing a container runs, which reads an environment, checks the policy against it, and either
lays out its worker processes or refuses. It is a separate module for the reason the layout
table gives about limits and the limit store - **a module that decides policy does not own a
client** - and the split earns itself here, because everything that makes a worker wrong is
in the environment rather than in the algorithm.

**The checks in this file already existed and nothing called them.** `queue_url_refusals`
and `concurrency_gaps` were written, tested and invoked from nowhere but their own test
file, which is the most common defect in this repository and the one that is hardest to see
in review: a correct, tested, documented mechanism nobody runs. This is their call site. A
worker that starts is a worker whose connection string is not the application's, whose
schema is one the row-level security sweep enumerates, and whose slots fit the memory limit
its container was given.

**A container is capped twice and the second cap here is not a heap ceiling, because CPython
has none.** `docker-compose.langfuse.yml` sets `GOMEMLIMIT` for Go and `--max-old-space-size`
for Node, each strictly below its cgroup limit, so the process collects rather than being
killed. Python has no equivalent, and the honest thing is to say so rather than to invent
one: `resource.setrlimit(RLIMIT_AS)` bounds address space rather than heap and turns a
correct program into a MemoryError. So the second cap is the admission-side one. The worker
is told how many jobs it may run at once, `MIB_PER_SLOT` says what one costs, and
`concurrency_gaps` refuses an allocation whose total is over the container's limit. That
bounds the ordinary failure, which is slots sized for a machine we do not have. It does not
bound a single job that leaks, and that one is still an OOM kill; the compose file says so
where an operator will read it.

**One image and one command run two differently sized containers, and the difference is a
file somebody else chose.** Everything the general worker does is bounded by something we
wrote; a parse is bounded by whoever made the document, and 48 MiB of slot cannot hold the
50 MiB PDF the knowledge door admits. So `BRAIN_WORKER_COMPONENT` says which component this
container is, every figure above is checked against that component's limit rather than
against `brain-worker`'s, and on the parse worker `brain.knowledge.parse_budget` adds the
checks the slot arithmetic cannot make. Reading it from the environment rather than
inferring it from the slot allocation is deliberate: a wrong allocation is the thing being
checked, and a limit inferred from it would make every allocation fit.

**Both containers drained the same queue, and sizing them differently did not stop that.**
`queue_name_for` derived a queue from the traffic class alone, ingestion is
`TrafficClass.SYSTEM` like every other piece of housekeeping, and both workers were allocated
a `system` slot. So the general worker could fetch the parse the parse worker was built for,
into a slot a fifth its size, and every check in this file passed: the slot arithmetic said
one slot at 48 MiB fits 384, and `parse_worker_gaps` was asked only of the container named as
the parse worker. `brain.knowledge.parse_budget` names that gap in its own docstring and says
closing it needs a change to the queue's rule about who may choose a queue.
`BRAIN_WORKER_SLOT_CLASS` is that change arriving here: it says which shape of container this
is, `queue_name_for` takes it, and the two containers stop being able to fetch each other's
work. It is read from the environment for the same reason the component is, and the two are
checked against each other, because a container that says it is the parse worker and serves
standard slots is the original defect written down explicitly.

That is also one of the two imports in this file that run from `brain.ops` into
`brain.knowledge`, and both are the right way round: what a parse may cost is a property of the
knowledge door and of the container's limit, and this module is the only thing that knows which
container it is in. The alternative considered was a second entry point in the knowledge layer
with its own `--ready` and its own heartbeat, which is a copy of this file for one constant's
worth of difference, and the copy is the one that goes stale.

**The embedding batch is checked for every container and the parse budget for one, and the
asymmetry is the point.** A parse is sized against a container, because only one container is
sized for a file somebody outside this company chose. A batch is sized against a slot:
`queue_name_for` derives a queue from the traffic class and refuses per-task queues, embedding
work is `TrafficClass.SYSTEM` like the rest of the housekeeping, and both workers drain
`system`. So either of them can be handed an embedding batch, and `embed_batch_gaps` is asked
unconditionally rather than behind a component name. `brain.ops.inference.inference_gaps` is
asked on the same terms and for the same reason, from the far end of that batch: it compares
the largest batch the planner will build against the largest request the inference server
will accept, and a container that may be handed a batch is a container that may find out.

**It exits rather than loops.** There is no queue driver installed, so the run mode prints
`NO_DRIVER_IS_INSTALLED` and exits. A worker that started anyway would poll an empty queue
and report itself healthy, and an empty queue and an absent queue look identical from every
metric there is - which is the same argument `brain.ops.queue` makes about a listener behind
a transaction pooler. That is now asked rather than stated: it prints the sentence when
`driver_is_installed` says so, having previously printed it unconditionally, which would have
gone on being printed on the first day it was false.

**`--deploy-plan` prints the steps that install the queue, because they existed nowhere.**
`THE_QUEUE_SCHEMA_IS_NOT_ALEMBICS` argues that the driver's DDL is a deploy step rather than
a migration and that the price is paid in two places rather than hidden. The second place was
not written down anywhere: no command, no ordering, and no statement to enable row-level
security on the tables the driver creates without it. `brain.ops.queue.DEPLOY_PLAN` is that
runbook and this is the mode that prints it, so the answer to "what does this leaf still
need" is a command an operator can run rather than a paragraph somebody has to find.

**Readiness is the heartbeat, and the heartbeat is not written yet.** `brain.ops.wiring`
says the worker is ready when "the queue driver has fetched at least once and the database
is reachable", so `--ready` reads the heartbeat file a running worker would write and
compares its age against `brain.ops.queue.stale_after()`, which is the same staleness the
re-drive sweep uses rather than a second copy of it. Nothing writes that file today, so the
check answers "not ready", which is the correct answer for a container that is not draining
a queue.

Not claimed: M32.4.1.4, and the reason has narrowed rather than gone. The placement defect
above is fixed and the two containers are now checked against each other, which is real work
against that leaf. The service is still written, sized, and never started, because the process
it starts has no driver to fetch with. `docker-compose.langfuse.yml` refuses M32.1.1.1 on the
same grounds and in the same words: a compose file that has never run is a design.

What this serves is the leaf named in the paragraph above, and it is deliberately not
claimed. The id is not repeated on the line below, because that line is parsed for ids and
a sentence saying a leaf is not claimed reads to the parser exactly like claiming it.

Task ids: none
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from brain.gate.context import TrafficClass
from brain.knowledge.embed_queue import embed_batch_gaps
from brain.knowledge.parse_budget import (
    PARSE_WORKER_COMPONENT,
    parse_budget_note,
    parse_worker_gaps,
)
from brain.ops.checkpoints import channel_policy_gaps, connection_refusals
from brain.ops.inference import inference_gaps
from brain.ops.queue import (
    DEPLOY_PLAN,
    DRIVER_SCHEMA,
    FALLBACK_POLL_SECONDS,
    MIB_PER_SLOT,
    NO_DRIVER_IS_INSTALLED,
    Shard,
    SlotClass,
    concurrency_gaps,
    deploy_plan_gaps,
    driver_is_installed,
    driver_schema_gaps,
    queue_url_refusals,
    stale_after,
    worker_shards,
)
from brain.ops.wiring import WiringError, component

# ------------------------------------------------------------------------ the environment
#: Where the worker looks for its queue. A name of its own rather than `DATABASE_URL`,
#: because the whole of `queue_url_refusals` is the case where the two are the same string.
QUEUE_URL_ENV: Final = "QUEUE_URL"

#: The application's connection, which the worker also has because it makes ordinary queries
#: through the pooler like everything else. Read here only so the two can be compared.
APP_URL_ENV: Final = "DATABASE_URL"

#: Where the graph's saved state goes. Optional: an install with no durable graph has none,
#: and a missing checkpointer is not a misconfiguration. A wrong one is.
CHECKPOINTER_URL_ENV: Final = "BRAIN_CHECKPOINTER_URL"

#: The file a running worker touches. In the container's own filesystem rather than shared,
#: so it says something about this process rather than about the fleet.
HEARTBEAT_PATH_ENV: Final = "BRAIN_WORKER_HEARTBEAT"

#: Which `brain.ops.wiring` component this container is. One image and one command run two
#: differently sized containers, and every piece of arithmetic below is against a limit that
#: belongs to one of them: slots against the cgroup limit, and a parse against what is left
#: of it. Read from the environment rather than inferred from the slot allocation, because a
#: container that has been given the wrong allocation is exactly the case being checked, and
#: inferring the limit from the mistake would make every allocation fit.
WORKER_COMPONENT_ENV: Final = "BRAIN_WORKER_COMPONENT"

#: What an unset `WORKER_COMPONENT_ENV` means. The general worker, because that is the
#: deployment that existed first and a variable added later must not change what a container
#: already running does.
DEFAULT_WORKER_COMPONENT: Final = "brain-worker"

#: Which shape of container this is: several standard slots, or one job whose size somebody
#: outside this company chose. It decides the queue names this worker drains, which is what
#: stops the general worker fetching the parse the parse worker was sized for.
#:
#: Read from the environment rather than derived from the component, although today the two
#: agree. Deriving it would make the pair unable to disagree, and a container that names
#: itself the parse worker while serving standard slots is exactly the state worth catching:
#: it is the original defect, written out explicitly, and a derived value would silently
#: correct it instead of refusing it.
SLOT_CLASS_ENV: Final = "BRAIN_WORKER_SLOT_CLASS"

#: What an unset `SLOT_CLASS_ENV` means. The standard slot, for the reason
#: `DEFAULT_WORKER_COMPONENT` defaults to the general worker: a variable added later must not
#: change what a container already running does, and it is the safe direction. A container
#: that has not been told keeps draining the cheap queues and cannot fetch the expensive work.
DEFAULT_SLOT_CLASS: Final = SlotClass.STANDARD

#: Which slot class each component serves. One entry per worker container, and it is the
#: pairing rather than either value that is checked: `brain-parse-worker` exists because a
#: parse is bounded by its input, so a parse worker serving standard slots has been sized for
#: one job and pointed at the queue for the other kind.
COMPONENT_SLOT_CLASS: Final[Mapping[str, SlotClass]] = {
    DEFAULT_WORKER_COMPONENT: SlotClass.STANDARD,
    PARSE_WORKER_COMPONENT: SlotClass.WHOLE_CONTAINER,
}

#: The directory the heartbeat lives in, under whatever the platform calls temporary.
HEARTBEAT_DIRECTORY: Final = "brain-worker"

#: How the per-class allocation is spelled in an environment. One variable per class rather
#: than one packed string, so a deployment that gets one of them wrong is wrong in one place
#: and readable in `docker inspect`.
SLOT_ENV_PREFIX: Final = "BRAIN_WORKER_SLOTS_"

#: `EX_CONFIG` from sysexits. A distinct code because the two ways this process refuses need
#: different actions: 78 says an operator wrote something wrong, 69 says the build is missing
#: a dependency, and a single exit 1 for both sends the wrong person to look.
EXIT_MISCONFIGURED: Final = 78
#: `EX_UNAVAILABLE`. There is nothing to fetch with; see `NO_DRIVER_IS_INSTALLED`.
EXIT_NO_DRIVER: Final = 69
#: The healthcheck's failure. Ordinary and expected while the worker is starting.
EXIT_NOT_READY: Final = 1


def slot_env_name(traffic_class: TrafficClass) -> str:
    """The environment variable holding this class's slot count.

    Derived from the enum rather than listed, so a new traffic class arrives with a variable
    name already decided. A hand-written list is the same failure `CONCURRENCY` is checked
    for: a class nobody allocated is a class whose jobs are never fetched.
    """
    return f"{SLOT_ENV_PREFIX}{traffic_class.name}"


def declared_slots(env: Mapping[str, str]) -> tuple[Mapping[TrafficClass, int], tuple[str, ...]]:
    """The allocation this environment describes, and every variable it could not read.

    Returns both rather than raising on the first bad value, matching `brain.config.check`:
    a misconfiguration found one variable at a time is a sequence of restarts.

    A missing variable is left out of the mapping rather than defaulted to the declared
    number. `concurrency_gaps` already reports a class with no allocation as one whose jobs
    are never fetched, and quietly substituting the constant would make the environment
    look authoritative while the code ignored it - so a deployment that forgot a class would
    work, and the same deployment on a version with a different constant would not.
    """
    allocation: dict[TrafficClass, int] = {}
    problems: list[str] = []
    for traffic_class in TrafficClass:
        name = slot_env_name(traffic_class)
        raw = (env.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{name}={raw!r} is not a number of slots")
            continue
        if value < 0:
            problems.append(f"{name}={value} is negative; zero is how a class is not drained")
            continue
        allocation[traffic_class] = value
    return allocation, tuple(problems)


@dataclass(frozen=True)
class WorkerPlan:
    """What this worker would run, and what it would cost.

    `memory_mib` is the limit the component was budgeted at rather than anything measured.
    That is the honest source: the container's cgroup limit is set from the compose file,
    the compose file is held equal to `brain.ops.wiring` by test, and a worker reading its
    own cgroup would be reading the same number one step later.
    """

    shards: tuple[Shard, ...]
    memory_mib: int

    @property
    def slots(self) -> int:
        return sum(shard.concurrency for shard in self.shards)

    @property
    def slot_memory_mib(self) -> int:
        return self.slots * MIB_PER_SLOT

    @property
    def slot_class(self) -> SlotClass:
        """Which shape of container this plan lays out.

        Read off the shards rather than stored, so it cannot disagree with the queues they
        drain. A stored copy is a label, and a label that disagrees with the queue names is
        the one that gets believed in a log.
        """
        return self.shards[0].slot_class if self.shards else SlotClass.STANDARD

    def describe(self) -> str:
        """The plan in the form an operator reads in a container log.

        The fallback poll interval is printed rather than left in the source, and it is the
        least obvious line here. It is the queue's entire latency on the day notifications
        stop being delivered, which behind a transaction pooler is a day with no error in it,
        so the number somebody would need during that incident is in the log from the start
        rather than being looked up while it is happening.
        """
        sizing = (
            f"{self.slot_memory_mib} MiB of a {self.memory_mib} MiB limit"
            if self.slot_class is SlotClass.STANDARD
            # The standard-slot arithmetic understates a whole-container plan by an order of
            # magnitude, and printing it unqualified is what would make an operator shrink
            # the container. What one such job may cost is not this module's to compute; the
            # component that runs them prints it beside this line.
            else f"the whole of a {self.memory_mib} MiB limit for each, one at a time"
        )
        lines = [
            f"worker plan: {len(self.shards)} process(es), {self.slots} "
            f"{self.slot_class.value} slot(s), {sizing}",
            f"queue schema: {DRIVER_SCHEMA}",
            f"fallback poll: {FALLBACK_POLL_SECONDS}s, which is the whole latency if "
            "notifications stop being delivered",
        ]
        lines.extend(
            f"  {shard.queue}: {shard.concurrency} slot(s), {shard.slot_class.value}"
            for shard in sorted(self.shards, key=lambda s: s.queue)
        )
        return "\n".join(lines)


def plan_for(
    allocation: Mapping[TrafficClass, int],
    *,
    worker_component: str = "brain-worker",
    slot_class: SlotClass = SlotClass.STANDARD,
) -> WorkerPlan:
    """The processes this allocation lays out, against the component's own memory limit.

    The slot class reaches `worker_shards` rather than being recorded beside the plan, because
    it is what the queue names are derived from: a plan that carried it as a label while the
    shards drained the standard queues would print the intention and deploy the defect.
    """
    return WorkerPlan(
        shards=worker_shards(allocation, slot_class),
        memory_mib=component(worker_component).memory_mib,
    )


def declared_slot_class(env: Mapping[str, str]) -> tuple[SlotClass, tuple[str, ...]]:
    """Which shape of container this is, and the reason if the environment did not say.

    Returns the default alongside the complaint rather than raising, matching
    `declared_slots`: every check after this one is arithmetic that needs a slot class, and a
    preflight that stopped here would report one problem out of the several a badly
    configured container usually has.

    An unreadable value falls back to the default rather than to nothing, and the default is
    the safe direction: a container that drains the standard queues cannot fetch the job that
    would kill it. The finding is still returned, so the container does not start.
    """
    raw = (env.get(SLOT_CLASS_ENV) or "").strip()
    if not raw:
        return DEFAULT_SLOT_CLASS, ()
    try:
        return SlotClass(raw), ()
    except ValueError:
        return DEFAULT_SLOT_CLASS, (
            f"{SLOT_CLASS_ENV}={raw!r} is not a slot class, so this container's queue names "
            f"would be guessed; known: {[s.value for s in SlotClass]}",
        )


def component_slot_class_gaps(worker_component: str, slot_class: SlotClass) -> tuple[str, ...]:
    """Whether this container is serving the slot class its component was budgeted for.

    The check the two variables exist to make possible. `BRAIN_WORKER_COMPONENT` decides which
    memory limit every figure is checked against and `BRAIN_WORKER_SLOT_CLASS` decides which
    queues are drained, and getting one of the two right is worse than getting both wrong: a
    container sized for a parse and pointed at the standard queues reports a healthy fleet
    while the parse it exists for is fetched by a container a fifth its size.

    A component nobody has paired is left alone rather than refused. `COMPONENT_SLOT_CLASS`
    names the two worker containers, and a third one is a deployment decision this function
    has no basis to make; `preflight` has already refused a component `brain.ops.wiring` does
    not budget, which is the case worth refusing.
    """
    expected = COMPONENT_SLOT_CLASS.get(worker_component)
    if expected is None or expected is slot_class:
        return ()
    return (
        f"{worker_component!r} is budgeted as a {expected.value} container and "
        f"{SLOT_CLASS_ENV} says {slot_class.value}, so it is sized for one kind of job and "
        "draining the queue for the other",
    )


def declared_component(env: Mapping[str, str]) -> str:
    """Which component this container is, as its environment says.

    A string rather than a `Component`, because the name has to be reportable when it is not
    one: `component()` refuses an unknown name, and a preflight that let that exception out
    would print a traceback where every other misconfiguration prints a sentence.
    """
    return (env.get(WORKER_COMPONENT_ENV) or "").strip() or DEFAULT_WORKER_COMPONENT


def preflight(env: Mapping[str, str]) -> tuple[str, ...]:
    """Every reason this worker must not start, in words that name the fix.

    The order is the design: what cannot be read, then where it connects, then how much of
    it there is. A slot allocation checked against a queue URL that does not exist is
    arithmetic about nothing, and reporting it alongside the missing URL trains whoever
    reads the output to skim it.

    All of them, never the first. A worker pointed at the pooler with a slot allocation that
    does not fit has two problems, and fixing one produces a configuration that is still
    wrong in a way that raises nothing.

    The one exception to that is a component name nobody declared, which is returned on its
    own. Every remaining check is arithmetic against a memory limit, and an unknown component
    has none: continuing against the default would report figures for a container this one is
    not, which is worse than reporting nothing, because it looks like an answer.
    """
    queue_url = (env.get(QUEUE_URL_ENV) or "").strip()
    app_url = (env.get(APP_URL_ENV) or "").strip()
    checkpointer_url = (env.get(CHECKPOINTER_URL_ENV) or "").strip()

    findings: list[str] = []
    if not queue_url:
        findings.append(
            f"{QUEUE_URL_ENV} is not set. The worker needs a connection of its own: giving "
            f"it {APP_URL_ENV} points it at the transaction pooler, which is the one "
            "configuration that fails without an error"
        )
    else:
        findings.extend(queue_url_refusals(queue_url, app_url=app_url))
    findings.extend(driver_schema_gaps())
    findings.extend(deploy_plan_gaps())
    if checkpointer_url:
        findings.extend(connection_refusals(checkpointer_url, app_url=app_url))
        # Asked only when a checkpointer is configured, which is the same condition the URL
        # is checked under. An install with no durable graph has no allowlist to have drifted,
        # and reporting a channel policy at a container that saves nothing would be a finding
        # nobody can act on in a list whose whole value is that every line names a fix.
        findings.extend(channel_policy_gaps())

    allocation, unreadable = declared_slots(env)
    findings.extend(unreadable)
    slot_class, unreadable_class = declared_slot_class(env)
    findings.extend(unreadable_class)

    worker_component = declared_component(env)
    try:
        component(worker_component)
    except WiringError as exc:
        findings.append(
            f"{WORKER_COMPONENT_ENV}={worker_component!r} is not a component "
            f"brain.ops.wiring budgets, so nothing says how much memory this container has "
            f"and no slot or parse arithmetic below can be done: {exc}"
        )
        return tuple(findings)

    findings.extend(component_slot_class_gaps(worker_component, slot_class))
    findings.extend(
        concurrency_gaps(allocation, worker_component=worker_component, slot_class=slot_class)
    )
    # Every container, not only one of them, and that is the difference between this check and
    # the parse worker's below. A parse budget is a property of a container, because only one
    # container is sized for a document somebody else chose. A batch budget is a property of a
    # slot: `queue_name_for` derives the queue from the traffic class, embedding work is
    # `SYSTEM` like every other piece of housekeeping, and both workers drain `system`, so
    # either of them may be handed an embedding batch and both have to be able to hold one.
    findings.extend(embed_batch_gaps())
    # The other end of the same batch, and asked of every container for the same reason. A
    # batch that fits the slot and does not fit what the inference server will accept is
    # refused after the planner has built it, and the two figures are edited in two files by
    # two people: `MIB_PER_SLOT` is the queue's and knows nothing about models, and the
    # server's ceiling is what is left of a container after three sets of weights.
    findings.extend(inference_gaps())
    if worker_component == PARSE_WORKER_COMPONENT:
        # Only the parse worker, because only the parse worker is sized for a parse. Asked
        # about `brain-worker` these checks refuse, correctly: 48 MiB of slot cannot hold the
        # 50 MiB PDF the door admits. Running them there would stop the general worker over a
        # job it is not meant to be given, which is the wrong end to fix the routing at.
        findings.extend(parse_worker_gaps(allocation, worker_component=worker_component))
    return tuple(findings)


# ------------------------------------------------------------------------- readiness
def default_heartbeat_path() -> Path:
    """Where the heartbeat goes when nothing says otherwise.

    The temporary directory, asked for rather than spelled. Two reasons, and the second is
    the one that matters. The image runs as a non-root user with no home and no shell, so
    the temporary directory is the one place in it this process can write. And a heartbeat
    must not survive a restart: the file says "a worker is working right now", and a durable
    copy of that sentence left behind by a process that died is exactly the lie the re-drive
    sweep exists to catch, so it belongs somewhere the container forgets.

    `tempfile.gettempdir()` rather than a literal, so the tests can point it somewhere real
    on a machine whose temporary directory is not spelled the same way.
    """
    return Path(tempfile.gettempdir()) / HEARTBEAT_DIRECTORY / "heartbeat"


def heartbeat_age_seconds(path: Path, *, now: datetime) -> float | None:
    """How long ago the worker last said it was working, or None if it never has.

    None rather than a large number for a missing file. A worker that has not started is not
    a worker that is behind, and collapsing the two would let a container that never opened
    a connection report the same condition as one that is merely slow.
    """
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return (now - datetime.fromtimestamp(modified, tz=UTC)).total_seconds()


def is_ready(path: Path, *, now: datetime) -> bool:
    """Whether this container should be in rotation.

    The threshold is `brain.ops.queue.stale_after()` and not a number of its own. The queue
    decides when a worker has stopped answering, and a readiness check that disagreed with
    it would produce the two states that are both wrong: a container reporting ready while
    the recovery sweep re-drives its jobs, or a container taken out of rotation while it is
    still holding work nothing will reclaim.
    """
    age = heartbeat_age_seconds(path, now=now)
    return age is not None and age <= stale_after().total_seconds()


# ----------------------------------------------------------------------------- the process
def deploy_plan_text() -> str:
    """The queue's install steps, in the form an operator reads before a deploy.

    Rendered here rather than in `brain.ops.queue` for the split this module exists for: the
    queue decides what the steps are and this is the process that prints things. The state of
    the first step is asked rather than written, because `driver_is_installed` can answer it
    and a runbook that says a dependency is missing after somebody has added it is a runbook
    that gets ignored on the line that matters.
    """
    lines = ["queue deploy plan, in order:"]
    for step in DEPLOY_PLAN:
        marks = []
        if step.optional:
            marks.append("optional")
        if step.order == 1:
            marks.append("done" if driver_is_installed() else "NOT DONE")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        lines.append(f"  {step.order}. {step.what}{suffix}")
        lines.append(f"     because {step.why}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """`python -m brain.ops.worker [--check | --ready | --deploy-plan]`.

    Four modes, and each one is used by something. `--check` is an operator asking whether a
    deployment would start; `--ready` is the container healthcheck; `--deploy-plan` is the
    steps that install the queue, which existed nowhere before and which nothing else prints;
    no argument is the container's command. The environment is a parameter defaulting to the
    real one so the modes can be tested without one, which is the same reason
    `brain.ops.admission` takes `now` rather than reading a clock.
    """
    import os

    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if env is None else env

    if "--deploy-plan" in arguments:
        # Before the preflight and independent of it. An operator asking what installs the
        # queue is usually asking because the queue is not installed, which is the state in
        # which every other mode here refuses.
        print(deploy_plan_text())
        return 0

    if "--ready" in arguments:
        declared = (environment.get(HEARTBEAT_PATH_ENV) or "").strip()
        path = Path(declared) if declared else default_heartbeat_path()
        if is_ready(path, now=datetime.now(tz=UTC)):
            print(f"ready: heartbeat at {path} is fresh")
            return 0
        print(f"not ready: no fresh heartbeat at {path}", file=sys.stderr)
        return EXIT_NOT_READY

    findings = preflight(environment)
    if findings:
        print("this worker will not start:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    allocation, _ = declared_slots(environment)
    worker_component = declared_component(environment)
    slot_class, _ = declared_slot_class(environment)
    print(plan_for(allocation, worker_component=worker_component, slot_class=slot_class).describe())
    if worker_component == PARSE_WORKER_COMPONENT:
        # The plan's own numbers understate this container by an order of magnitude: it says
        # one slot at `MIB_PER_SLOT` of a 512 MiB limit, and an operator reading that would
        # reasonably shrink the container. What one parse may actually cost is a different
        # figure and it is printed beside it rather than left in a source file.
        print(parse_budget_note(worker_component=worker_component))
    if "--check" in arguments:
        return 0

    # The run mode, which does not run. See the module docstring: a worker that started
    # against no driver would poll an empty queue and report itself healthy, and an empty
    # queue is indistinguishable from an absent one in every metric there is.
    #
    # Asked rather than asserted. The sentence was printed unconditionally, which was true
    # and would have stayed printed on the first day it stopped being. There is still nothing
    # to run when a driver is present, because nothing implements `QueueDriver`, so that path
    # says what is missing instead of saying what is installed.
    if not driver_is_installed():
        print(NO_DRIVER_IS_INSTALLED, file=sys.stderr)
        return EXIT_NO_DRIVER
    print(
        "a queue driver is installed and nothing implements brain.ops.queue.QueueDriver, so "
        "there is still nothing to fetch with. See --deploy-plan for what remains.",
        file=sys.stderr,
    )
    return EXIT_NO_DRIVER


if __name__ == "__main__":
    raise SystemExit(main())
