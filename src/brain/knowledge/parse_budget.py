"""What one parse is allowed to cost, and the container that is sized to let it.

Parsing is the only place in this system where **an input decides how much memory is used,
and the input arrives from outside**. Everything else is bounded by something we wrote: a
page size, a retry budget, a slot count. A parse is handed a file somebody else chose, and
the relationship between that file's size and the memory needed to read it is set by whoever
made the file. Four megabytes of PDF renders to gigabytes of pixels. Twenty-five kilobytes
of zip decompresses to whatever the compressor could achieve, and a size check on the
archive fires long after the memory is gone.

What makes that worse than an ordinary resource question is who pays. The kernel does not
kill a parse, it kills a process, and the process is holding every other job in flight. One
hostile document therefore ends the reply somebody was waiting for, in a container that
restarts with nothing in its log to say which document did it.

**Two caps, as everywhere else here, and the second one is not a heap ceiling because
CPython has none.** `docker-compose.langfuse.yml` pairs each cgroup limit with a ceiling in
the runtime's own units, `GOMEMLIMIT` for Go and `--max-old-space-size` for Node, so the
runtime collects harder instead of being killed. Python has no equivalent and inventing one
is worse than saying so: `resource.setrlimit(RLIMIT_AS)` bounds address space rather than
heap, and address space is reserved by every shared library the interpreter maps and by
every allocator arena it never returns, so a process given its container's limit as
`RLIMIT_AS` fails on an ordinary import rather than on a leak. So the process-side ceiling
here is **admission**: an input whose declared cost is over the budget is refused before the
parser is called. `brain.ops.worker` reached the same conclusion about slots and this is the
same shape one level down, with the file rather than the slot count as the quantity.

**A bound checked before the work, because a bound checked during it has already been
exceeded when it fires.** Watching resident memory climb tells you a limit was passed; by
then the pages are allocated, and if the climb was fast enough the kernel has already
decided. So the question asked here is asked of the file: its admitted size times what its
container costs to open. See `A_DECLARED_COST_IS_A_CLAIM_THE_FILE_MAKES_ABOUT_ITSELF` for
what that can and cannot know, because it is not much.

**A parse worker is a container of its own, and that is arithmetic rather than taste.** The
general worker is budgeted 384 MiB and runs seven slots at `MIB_PER_SLOT`, which leaves
48 MiB of slot for a job. The door admits a 50 MiB PDF, so a parse in that container cannot
hold the file it was given, never mind what reading it costs. Two ways to close that: shrink
the door's ceilings, or size a container for the parse. Shrinking the door was rejected,
because `TYPE_LIMITS` is a reviewed decision and 50 MiB of PDF is an ordinary scanned book,
so refusing it would move a memory problem onto a person with a document. `parse_worker_gaps`
is what stops the two drifting apart again: it asks whether the container can hold the
largest file the door will admit, and asked about `brain-worker` it says no.

**One parse at a time, and that falls out of the budget rather than being chosen beside
it.** The budget is the whole of what is left after `PARSE_WORKER_RESERVE_MIB`, so two
concurrent parses would each be allowed all of it. `PARSES_AT_ONCE` is therefore a property
of the sizing, and `parse_worker_gaps` refuses a deployment that allocates the parse worker
more slots than that.

**What is not contained, stated plainly rather than implied.** See
`A_PARSE_THAT_BLOWS_ITS_BOUND_IS_NOT_CONTAINED_IN_PROCESS`. The admission check ends a job
without ending the process, which is the failure that can be contained. A parser that runs
inside the bound and allocates past it anyway cannot be, because the allocation that trips
the cgroup is usually inside a C extension and the answer to it is SIGKILL. Containing that
needs the parse in a subprocess with its own rlimit, and there is no subprocess here.

**A refusal names the file and nothing else.** `ParseCause.OUT_OF_MEMORY` already carries
the wording, and it says the file is too large to hold and to split it up. It does not say
how busy the queue is, how many parses are ahead, or what anybody else uploaded; see
`A_REFUSAL_NAMES_THE_FILE_AND_NEVER_THE_QUEUE`.

**This budget is what a document costs to read, and it says nothing about what a parser
costs to load.** The two are separate and only one of them is decidable here. Item 31 on the
Needs Rupash page measures the layout-aware stack at 83 further packages and roughly 1.5 GB
installed before any model file is fetched, which does not fit this container and does not
fit any other in `brain.ops.wiring`'s budget either. Raising the figure below until it did
would be answering a question the owner has been asked, so the figure is sized for the input
and the dependency question stays open where it was put.

**The placement gap this module named is closed, and it was a live defect.** This paragraph
used to say that `queue_name_for` derived a queue from the traffic class alone, that ingestion
is `TrafficClass.SYSTEM` like every other piece of housekeeping, and that both workers
therefore drained `system` and either could fetch a parse. That was exactly right, and what it
meant is that the general worker, whose slots are 48 MiB inside 384, could fetch the 50 MiB
document the knowledge door admits. Every check passed while it was true: the slot arithmetic
said one 48 MiB slot fits, and `parse_worker_gaps` was only ever asked of the container named
as the parse worker.

`SlotClass` in `brain.ops.queue` is the change to the central rule that closes it. A
whole-container job derives `system.whole_container`, the general worker drains the standard
names and the parse worker drains that one, and the two are now checked against each other
rather than each alone. The priority rule survives intact: a task may declare a cost and still
may not declare a class, because saying a job is expensive only ever routes it to the scarcer
container that runs one at a time.

Nothing runs today either way: there is no queue driver and no parser, so no job has ever been
fetched by either container. The routing exists and the traffic does not.

Task ids: M7.2.6
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from brain.gate.context import TrafficClass
from brain.knowledge.ingest import (
    TYPE_LIMITS,
    AdmittedUpload,
    Container,
    MediaType,
    ceiling_for,
)
from brain.ops.wiring import component

#: Bytes in a mebibyte. Spelled once because every figure below is declared in MiB, which is
#: the unit `brain.ops.wiring` and every compose file use, and compared against a file size,
#: which is bytes.
MIB: Final = 1024 * 1024


# ------------------------------------------------------------------ written-down reasons
#: Why the second cap is an admission check rather than a ceiling inside the interpreter.
THE_SECOND_CAP_IS_ADMISSION_BECAUSE_CPYTHON_HAS_NO_HEAP_CEILING = (
    "A cgroup limit is enforced by killing the process, so it protects the neighbour on a "
    "shared host and not the work in flight. The pair to it everywhere else in this "
    "repository is a ceiling in the runtime's own units, and CPython has none to set: "
    "RLIMIT_AS bounds address space rather than heap, which shared libraries and allocator "
    "arenas reserve without ever touching, so a process given its container's limit fails "
    "on an import. The ceiling that is available is a decision taken before the work: an "
    "input whose declared cost is over the budget is refused before the parser is called. "
    "What it does not cover is an input under the budget whose real cost is not."
)

#: Why the cost is computed from the file's own declaration and what that misses.
A_DECLARED_COST_IS_A_CLAIM_THE_FILE_MAKES_ABOUT_ITSELF = (
    "The declared cost knows two things: the size the door measured, which is a fact about "
    "the bytes rather than a header anybody sent, and the container those bytes proved to "
    "be. It does not know the page count, the number or resolution of embedded images, the "
    "compression ratio of an archive, or how deeply one is nested. So the expansion factors "
    "are a rule of thumb calibrated to refuse the obvious and admit the ordinary, and not "
    "one of them has been measured, because there is no parser in this project to measure. "
    "A file that is under the budget and expands past it anyway is not caught here, and the "
    "only thing standing behind it is the cgroup limit and the kill."
)

#: Why a parse that exceeds its bound is not contained, and what containing it would take.
A_PARSE_THAT_BLOWS_ITS_BOUND_IS_NOT_CONTAINED_IN_PROCESS = (
    "Two failures wear the same name and only one of them can be contained. An input whose "
    "declared cost is over the budget is refused, that job ends as a ParseFailure, and the "
    "process carries on: contained. A parser that starts inside the bound and allocates "
    "past the container's limit is not, and no amount of care in this module changes that, "
    "because the allocation that trips the cgroup is usually inside a C extension and the "
    "kernel's answer to it is SIGKILL, which no handler sees. Containing that one means "
    "running the parse in a subprocess with its own rlimit and reading the result back, and "
    "no subprocess is built here. Saying the bound protects the container would be claiming "
    "an isolation nobody wrote."
)

#: Why the refusal is worded from the file and never from the machine's state.
A_REFUSAL_NAMES_THE_FILE_AND_NEVER_THE_QUEUE = (
    "DENIED and ABSENT are indistinguishable here as everywhere. 'This file is too large to "
    "hold, split it up' is a fact about the uploader's own document, which they brought "
    "with them and already know. 'The queue is full because of twelve other jobs' is a "
    "count of other people's work, and a person who learns it has learned that twelve other "
    "people uploaded something. So the wording is CAUSE_TEXT[OUT_OF_MEMORY] and this module "
    "adds nothing to it; the numbers that would identify the deployment stay in the "
    "operator-facing findings, which are printed into a container log and not returned to "
    "anybody."
)


# ------------------------------------------------------------------ the container's sizing
#: The component that runs parses. Named rather than assumed, because the whole argument
#: above is that it is not the general worker: `brain-worker` is 384 MiB across seven slots
#: and the door admits a 50 MiB PDF, so a parse there cannot hold its own input.
PARSE_WORKER_COMPONENT: Final = "brain-parse-worker"

#: What the cgroup counts and the parse budget does not: the interpreter, the imports, the
#: database connection, the object-store client, and the copy of the file itself that
#: `ScannedContent` is holding while the parser reads from it.
#:
#: It is why the process-side ceiling sits strictly below the cgroup limit rather than equal
#: to it, which is the same relation `tests/unit/test_wiring.py` asserts of every service in
#: the trace ledger, and the mistake it exists to prevent is the tempting one: set the
#: process ceiling to the container limit so that nothing is wasted, and be killed while
#: believing you are within budget.
PARSE_WORKER_RESERVE_MIB: Final = 64

#: How many parses the parse worker runs at once. One, and it is derived rather than chosen:
#: the budget below is the whole of what is left after the reserve, so a second concurrent
#: parse would be allowed the same whole budget and the container has no second copy of it.
PARSES_AT_ONCE: Final = 1

#: How much bigger a parse's working set is than the file, by what the bytes proved to be.
#:
#: **None of these is measured**; see `A_DECLARED_COST_IS_A_CLAIM_THE_FILE_MAKES_ABOUT_ITSELF`.
#: They are stated as judgements, one per container, because the door's ceilings are per
#: type and the cost of opening genuinely differs by container: a compressed archive is not
#: the same problem as a file that is already the bytes it will be read as.
#:
#: TEXT is the only one with a mechanism rather than a feeling behind it. CPython stores a
#: `str` as one, two or four bytes per code point and picks the width from the widest
#: character present, so a single emoji anywhere in an otherwise ASCII document quadruples
#: the decoded copy of the whole of it.
PARSE_EXPANSION: Final[Mapping[Container, int]] = {
    #: The object graph plus whichever page is being rendered. A PDF is compressed and its
    #: working set is not.
    Container.PDF: 6,
    #: An Office document is a zip whose XML parts deflate at roughly ten to one, and the
    #: parse holds the decompressed part and a tree over it at the same time.
    Container.ZIP: 8,
    #: The decoded string, at CPython's widest representation, plus the block list built
    #: from it.
    Container.TEXT: 4,
    #: Pixels. A document-like PNG compresses far past this and the cgroup limit is what
    #: covers the rest.
    Container.PNG: 16,
    #: Pixels again, and JPEG's ratio on a photographed page is higher than PNG's.
    Container.JPEG: 20,
    #: Unreachable through the door, which refuses anything it cannot name, and present so
    #: the table is total: a lookup that can raise KeyError inside a memory guard is a guard
    #: that fails open. The figure is the largest in the table, because the honest cost of
    #: something nobody identified is the worst one here.
    Container.UNKNOWN: 20,
}


def parse_budget_bytes(
    *,
    worker_component: str = PARSE_WORKER_COMPONENT,
    reserve_mib: int = PARSE_WORKER_RESERVE_MIB,
) -> int:
    """The most one parse may be declared to cost, in the container that runs it.

    Derived from the component's own memory limit rather than set beside it, which is the
    rule `brain.knowledge.uploads.queue_limits_for` states about the ingestion queue and the
    reason is the same: two numbers governing one resource drift, and the operator watching
    a refused parse cannot tell which of them refused it. Here the drift would be worse than
    confusing, because the pair of numbers is what makes the second cap mean anything.

    Both arguments are parameters with defaults, so this can be asked about a container we do
    not have. A check that can only ever be run against the constant beside it cannot be
    shown to fail, and a check nobody has seen fail is a check nobody knows works.
    """
    return (component(worker_component).memory_mib - reserve_mib) * MIB


def declared_cost_bytes(upload: AdmittedUpload) -> int:
    """What reading this file is expected to cost, from its size and its container.

    Called before the parser, never during it, which is the whole point: a limit enforced by
    watching memory climb has already been exceeded by the time it fires. See
    `A_DECLARED_COST_IS_A_CLAIM_THE_FILE_MAKES_ABOUT_ITSELF` for what this cannot know, which
    is most of what decides a real parse.

    The size is `AdmittedUpload.size_bytes`, which the door measured off the buffer rather
    than reading out of a `Content-Length`. That distinction is the reason this is worth
    anything at all: a declared length is a claim by whoever is uploading, and the whole of
    `brain.knowledge.uploads.read_within` exists because that claim is sometimes a lie.
    """
    return upload.size_bytes * PARSE_EXPANSION[TYPE_LIMITS[upload.media_type].container]


def fits_parse_budget(upload: AdmittedUpload, *, budget_bytes: int | None = None) -> bool:
    """Whether this file may be handed to a parser at all.

    Positive sense on purpose. The consumer is a guard that returns a `ParseFailure`, and a
    guard reading `if not fits(...)` says what is true of the file it lets through, which is
    the sentence a reader needs when they are asking whether the check is the right way
    round.
    """
    budget = parse_budget_bytes() if budget_bytes is None else budget_bytes
    return declared_cost_bytes(upload) <= budget


def worst_declared_cost() -> tuple[MediaType, int]:
    """The largest parse the door can produce, and the type that produces it.

    Over the door's own table rather than over a list here, so a type added to `TYPE_LIMITS`
    or a ceiling raised in it is answered by this function on the same commit. That is what
    makes `parse_worker_gaps` a check rather than a restatement: the two numbers it compares
    are edited in two different files by two different people for two different reasons.

    `ceiling_for` rather than `TypeLimit.max_bytes`, because the door takes the lower of the
    per-type ceiling and the absolute one and this has to ask the question the door answers.
    **The two agree on every row in the table today**, so a mutation swapping one for the
    other survives the suite, and that is recorded rather than papered over with a test built
    to fit it. They diverge the first time somebody types a per-type ceiling above
    `ABSOLUTE_MAX_BYTES`, and the divergence is safe in the direction that matters: reading
    `max_bytes` would size this container for a file the door would refuse anyway.
    """
    costs = {
        media_type: ceiling_for(media_type) * PARSE_EXPANSION[limit.container]
        for media_type, limit in TYPE_LIMITS.items()
    }
    worst = max(costs, key=lambda media_type: costs[media_type])
    return worst, costs[worst]


def parse_budget_note(*, worker_component: str = PARSE_WORKER_COMPONENT) -> str:
    """The line an operator reads in the container log, saying what this worker may parse.

    Printed at start rather than left in the source for the reason `WorkerPlan.describe`
    prints the fallback poll interval: the plan already says how many slots there are and
    what a slot costs, and on this container both of those numbers understate the job by an
    order of magnitude. An operator who saw only "1 slot, 48 MiB of a 512 MiB limit" would
    reasonably conclude the container is eight times larger than it needs to be, and shrink
    it, and discover why on the first large document.
    """
    budget_mib = parse_budget_bytes(worker_component=worker_component) // MIB
    media_type, cost = worst_declared_cost()
    return (
        f"parse budget: {budget_mib} MiB for {PARSES_AT_ONCE} parse(s) at once, of a "
        f"{component(worker_component).memory_mib} MiB limit. The largest file the door "
        f"admits is {media_type.value} at a declared cost of {cost // MIB} MiB. A parse over "
        "the budget is refused before the file is opened; a parse that exceeds it while "
        "running is an OOM kill, because CPython has no ceiling to set"
    )


def parse_worker_gaps(
    allocation: Mapping[TrafficClass, int],
    *,
    worker_component: str = PARSE_WORKER_COMPONENT,
    reserve_mib: int = PARSE_WORKER_RESERVE_MIB,
) -> tuple[str, ...]:
    """Every reason this container will not parse what the door lets in, in words naming the fix.

    Three checks, and each catches a different way the two caps stop being two.

    The reserve, first, because it is what makes the process-side ceiling a second cap rather
    than a restatement of the first. A budget at or above the container's limit means the
    only enforcement left is the kill.

    Then the concurrency. The budget is the whole of what is left, so more than
    `PARSES_AT_ONCE` of them is that many times the container's memory promised out of one
    container's worth.

    Then the door, which is the check this module exists for and the one that is failing
    today for `brain-worker`: a container whose budget cannot hold the largest file the door
    admits will refuse a file the uploader was told was accepted, and it will do it after the
    file has been fetched, scanned and stored.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight`: a
    container sized wrongly in two ways is one where fixing one leaves a configuration that
    is still wrong and still silent.
    """
    findings: list[str] = []
    limit_mib = component(worker_component).memory_mib
    budget = parse_budget_bytes(worker_component=worker_component, reserve_mib=reserve_mib)

    if budget >= limit_mib * MIB:
        findings.append(
            f"a reserve of {reserve_mib} MiB leaves a parse budget of {budget // MIB} MiB "
            f"against a {limit_mib} MiB limit on {worker_component!r}, so the process is "
            "told it may use everything the cgroup counts and more; the cgroup counts the "
            "interpreter, the imports and the file itself, and enforces by killing"
        )

    slots = sum(allocation.values())
    if slots > PARSES_AT_ONCE:
        findings.append(
            f"{worker_component!r} is allocated {slots} slots and the parse budget is "
            f"{budget // MIB} MiB each, which is {slots} times what the container has; the "
            f"budget is what is left after the reserve, so only {PARSES_AT_ONCE} parse(s) "
            "fit at a time"
        )

    media_type, cost = worst_declared_cost()
    if cost > budget:
        findings.append(
            f"the door admits {media_type.value} up to {ceiling_for(media_type) // MIB} MiB, "
            f"which is a declared cost of {cost // MIB} MiB, over the {budget // MIB} MiB "
            f"budget on {worker_component!r}; a file accepted at the door would be refused "
            "after it had been fetched, scanned and stored"
        )
    return tuple(findings)
