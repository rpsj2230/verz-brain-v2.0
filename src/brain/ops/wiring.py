"""What runs beside the application on a host that is not ours alone, and how large it may be.

Wave 2 adds components that do not exist as containers yet: a trace ledger, a PII
analyser, an object store, a job queue, an optional automation canvas. This module is the
one place that says how much memory each may take, which profile runs it, how it reaches
Postgres, and what "ready" means for it. It is data plus the checks over that data.
Nothing here opens a socket or reads a compose file at runtime.

**Every component declares a memory limit, as a number here as well as a line in a compose
file.** The host already runs a second production system belonging to the same owner. An
unlimited container is not a sizing mistake on a box like that, it is somebody else's
outage, and it is invisible until the night it happens. Two copies of the figure looks
like duplication and is the only way the duplication becomes checkable: the test suite
reads the deployed compose file and asserts its total against `PRODUCTION_BASELINE_MIB`,
so a service added without a limit fails, and so does a limit raised without anybody
deciding where the memory came from.

Rejected: deriving the budget from the compose files alone. The check then compares a
number with itself and passes for every possible value, including one that fills the host.
A budget has to be asserted somewhere that is not the thing being budgeted.

**A component that needs session state does not go behind the transaction pooler.** This
has cost twice. `brain.migrate` moved from `pg_advisory_lock` to `pg_advisory_xact_lock`
because a session-level lock taken through a transaction pooler is released by whichever
transaction happens to end first. `brain.session` sets `prepare_threshold=None` because a
statement prepared on one backend and executed on another is a production-only failure.
The third case is worse than both, because it has no error at all: a `LISTEN` behind a
transaction pooler simply stops receiving notifications, and a queue that never wakes up
looks exactly like a queue with nothing in it. `Wiring` is therefore a declaration every
component must make, and `pooler_misuse` refuses the combination rather than trusting that
whoever writes the compose entry will remember.

**A profile is a set of components, not a flag on each one.** Ask a per-component boolean
what lite runs and the answer is assembled by the caller, which means two callers assemble
it differently and one of them ships. `components_for` is the only answer to that question.

**Readiness is a sentence, and a component without one is not wired.** Liveness is free:
the process is up. Readiness is the claim that the component can answer correctly, and a
component whose readiness nobody wrote down gets checked for liveness by default, which is
how a half-connected instance stays in rotation answering from whatever it can still
reach. `Component` refuses to be constructed without it.

**What this module concludes, and it is not comfortable.** The full profile does not fit on
this host. Langfuse needs ClickHouse, ClickHouse's practical floor is a gigabyte, and the
existing stack has already committed 3712 MiB of roughly 6400. `budget_breaches("full")`
reports the overrun rather than the numbers being quietly rounded until they agree. The
answer is a second host or a hosted trace ledger, and that is a decision for Rupash, not
something to resolve by editing a constant here.

No leaf ids are claimed by this module. The compose services for these components are not
written, and a memory limit that exists only as a Python constant is not a resource limit
on anything. What is here is the budget those services will have to satisfy, and the
arithmetic that says two of them cannot both be run.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

#: What this system's declared limits may add up to on the shared host, in mebibytes.
#: Approximate on purpose: it is headroom above a neighbour whose own usage moves, so
#: treating it as exact would be false precision. Sizing against it is still the point,
#: because the alternative is sizing against nothing.
HOST_HEADROOM_MIB: Final = 6400

#: The sum of `deploy.resources.limits.memory` across the four services in
#: `docker-compose.yml`, which is what is deployed today. Asserted against that file by
#: the test suite rather than trusted, because the whole value of the number is that it
#: stops matching when somebody changes the file.
PRODUCTION_BASELINE_MIB: Final = 3712

#: Left for the host: the kernel, the page cache, the Docker daemon, and the SSH session
#: an operator needs in order to fix whatever went wrong. Spending the last megabyte of a
#: budget means the first thing to fail is the ability to log in and see why.
HOST_RESERVE_MIB: Final = 256

PROFILES: Final = ("lite", "standard", "full")


class Wiring(enum.StrEnum):
    """How a component reaches this system's Postgres, if it reaches it at all.

    Three values rather than a boolean, because "does not touch our database" and "touches
    it through the pooler" are different facts with different failure modes, and collapsing
    them loses the one that matters: a component wired `NONE` cannot be misconfigured onto
    the pooler, because it has no connection string to get wrong.
    """

    #: Transaction mode. No session state survives between statements.
    POOLER = "pooler"
    #: Session mode or straight to Postgres. Required for LISTEN/NOTIFY, session-level
    #: advisory locks, and server-side prepared statements.
    DIRECT = "direct"
    #: Does not reach this system's database. Its own storage is its own problem.
    NONE = "none"


class WiringError(Exception):
    """Raised when a component or a profile is described in a way that cannot be deployed."""


@dataclass(frozen=True)
class Component:
    """One container, with everything about it that a neighbour's outage depends on.

    `ready_when` is prose and is still required. It is the sentence somebody writes a
    health check from, and a component that arrives without one gets whatever check the
    person wiring it up invents, which is almost always a TCP connect. A TCP connect
    proves a socket is listening, which is liveness wearing readiness' clothes.
    """

    name: str
    memory_mib: int
    profiles: frozenset[str]
    wiring: Wiring
    ready_when: str
    #: True when the component uses LISTEN/NOTIFY, a session-level advisory lock, or
    #: server-side prepared statements. Declared rather than inferred from `wiring`,
    #: so that the two can disagree and be caught.
    needs_session_state: bool = False

    def __post_init__(self) -> None:
        if self.memory_mib < 1:
            # Zero is how "unlimited" is spelled in a dataclass, and it must not be
            # constructible. A component with no ceiling is the failure this file exists
            # for, and it is not one an operator can see in `docker stats` until it bites.
            msg = f"component {self.name!r} declares no memory limit; that is a neighbour's outage"
            raise WiringError(msg)
        if not self.ready_when.strip():
            msg = (
                f"component {self.name!r} does not say what ready means; liveness is not readiness"
            )
            raise WiringError(msg)
        unknown = sorted(self.profiles - set(PROFILES))
        if unknown:
            msg = (
                f"component {self.name!r} names unknown profile(s) {unknown}; "
                f"known: {list(PROFILES)}"
            )
            raise WiringError(msg)


#: Wave 2, sized to each project's own practical floor rather than to what is left over.
#:
#: Sizing to the remainder is the tempting mistake: it produces numbers that add up and
#: containers that are OOM-killed under the first real load, which on a shared host is the
#: neighbour's problem as much as ours. The figures below are what these components need to
#: work. Whether they fit is a separate question, asked by `budget_breaches`, and the
#: answer for `full` is no.
COMPONENTS: Final[tuple[Component, ...]] = (
    Component(
        name="brain-worker",
        memory_mib=384,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.DIRECT,
        needs_session_state=True,
        ready_when="the queue driver has fetched at least once and the database is reachable",
    ),
    Component(
        name="seaweedfs",
        memory_mib=256,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="the S3 gateway lists a known bucket",
    ),
    Component(
        name="presidio-analyzer",
        memory_mib=512,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="the analyser returns a detection for a known-positive probe string",
    ),
    Component(
        name="langfuse-web",
        memory_mib=512,
        profiles=frozenset({"full"}),
        wiring=Wiring.DIRECT,
        # Its migrations take a session-level advisory lock and its ORM prepares
        # statements. Both are the failures `brain.session` documents, arriving in
        # somebody else's code where our connect_args cannot reach them.
        needs_session_state=True,
        ready_when="the ingest endpoint accepts a trace and it is readable back",
    ),
    Component(
        name="langfuse-worker",
        memory_mib=384,
        profiles=frozenset({"full"}),
        wiring=Wiring.DIRECT,
        needs_session_state=True,
        ready_when="the ingest queue depth is falling or empty",
    ),
    Component(
        name="langfuse-clickhouse",
        memory_mib=1024,
        profiles=frozenset({"full"}),
        wiring=Wiring.NONE,
        ready_when="a SELECT against the observations table returns",
    ),
    Component(
        name="langfuse-cache",
        memory_mib=128,
        profiles=frozenset({"full"}),
        wiring=Wiring.NONE,
        ready_when="PING answers",
    ),
    Component(
        name="activepieces",
        memory_mib=512,
        profiles=frozenset({"full"}),
        # Deliberately NONE. It runs its own store; handing it a connection string to
        # ours would put a credential inside a sandbox whose whole premise is that its
        # contents are written by somebody outside this repository.
        wiring=Wiring.NONE,
        ready_when="the flow runner reports a worker and the egress proxy answers",
    ),
)


def component(name: str) -> Component:
    """The component by that name, or a refusal naming the ones that exist.

    Refuses rather than returning None. A caller that gets None writes `if c is None:
    return` and the component silently stops being budgeted, which is the state this file
    exists to make impossible.
    """
    for c in COMPONENTS:
        if c.name == name:
            return c
    msg = f"unknown component {name!r}; known: {[c.name for c in COMPONENTS]}"
    raise WiringError(msg)


def assert_known_profile(profile: str) -> None:
    """Refuse a profile nobody defined, rather than treating it as the empty set.

    A typo in a deployment variable would otherwise select no components, report no
    breaches, and deploy nothing, all of which look like success.
    """
    if profile not in PROFILES:
        msg = f"unknown profile {profile!r}; known: {list(PROFILES)}"
        raise WiringError(msg)


def components_for(profile: str) -> tuple[Component, ...]:
    """Everything wave 2 runs in this profile, in declaration order.

    Lite is empty, and that is the profile flag: a lite install runs no trace ledger, no
    object store and no worker, and writes what it needs to know into its own audit ledger.
    A cut-down Langfuse is not on offer, because Langfuse without ClickHouse is a different
    product with the same name.
    """
    assert_known_profile(profile)
    return tuple(c for c in COMPONENTS if profile in c.profiles)


def wave_two_mib(profile: str) -> int:
    """What wave 2 adds in this profile."""
    return sum(c.memory_mib for c in components_for(profile))


def spendable_mib(*, headroom_mib: int = HOST_HEADROOM_MIB, baseline_mib: int | None = None) -> int:
    """What is left for wave 2 after the deployed stack and the host's own reserve.

    Both figures are parameters with defaults rather than constants read inside, so the
    same arithmetic answers "what if we moved staging off this box" without anybody
    editing a module to find out.
    """
    baseline = PRODUCTION_BASELINE_MIB if baseline_mib is None else baseline_mib
    return headroom_mib - baseline - HOST_RESERVE_MIB


def budget_breaches(
    profile: str,
    *,
    headroom_mib: int = HOST_HEADROOM_MIB,
    baseline_mib: int | None = None,
) -> tuple[str, ...]:
    """Every reason this profile does not fit, in words an operator can act on.

    Returns rather than raises, and returns all of them. A budget check that raises on the
    first problem is run once, fixed once, and run again, which is three deploys to learn
    something one message could have said.
    """
    assert_known_profile(profile)
    available = spendable_mib(headroom_mib=headroom_mib, baseline_mib=baseline_mib)
    wanted = wave_two_mib(profile)
    if wanted <= available:
        return ()
    largest = max(components_for(profile), key=lambda c: c.memory_mib)
    return (
        f"profile {profile!r} wants {wanted} MiB and has {available} MiB; "
        f"over by {wanted - available} MiB. The largest single component is "
        f"{largest.name!r} at {largest.memory_mib} MiB.",
    )


def pooler_misuse() -> tuple[str, ...]:
    """Components that need session state and have been wired through the transaction pooler.

    Empty is the only acceptable answer. This is checked over the declaration rather than
    over a running system because the running system does not complain: a listener behind a
    transaction pooler receives nothing and reports nothing, and a session advisory lock
    taken through one is released by a transaction that knows nothing about it.
    """
    return tuple(
        f"{c.name!r} needs session state and is wired {c.wiring.value!r}; "
        "LISTEN/NOTIFY and session-level advisory locks do not survive transaction pooling"
        for c in COMPONENTS
        if c.needs_session_state and c.wiring is Wiring.POOLER
    )
