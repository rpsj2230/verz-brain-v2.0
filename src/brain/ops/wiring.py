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

**Two of these components run the same image and the same command, and the second one is
here because of arithmetic.** `brain-worker` is 384 MiB across seven slots, so a job gets
about 48 MiB, and the knowledge door admits a 50 MiB PDF. A parse in that container cannot
hold its own input, never mind read it, and parsing is the one workload whose size is chosen
by somebody outside the company. `brain-parse-worker` is sized for the largest file the door
will admit; `brain.knowledge.parse_budget.parse_worker_gaps` is what compares the two ends
and refuses when they disagree, and asked about `brain-worker` it refuses today.

**A profile is a set of components, not a flag on each one.** Ask a per-component boolean
what lite runs and the answer is assembled by the caller, which means two callers assemble
it differently and one of them ships. `components_for` is the only answer to that question.

**Readiness is a sentence, and a component without one is not wired.** Liveness is free:
the process is up. Readiness is the claim that the component can answer correctly, and a
component whose readiness nobody wrote down gets checked for liveness by default, which is
how a half-connected instance stays in rotation answering from whatever it can still
reach. `Component` refuses to be constructed without it.

**What this module concludes, and it is not comfortable.** Neither `standard` nor `full`
fits on this host. Langfuse needs ClickHouse, ClickHouse's practical floor is a gigabyte,
the identity provider was measured at 768, the inference server has to hold three models
resident, and the existing stack has already committed 3712 MiB of roughly 6400.
`budget_breaches` reports each overrun rather than the numbers being quietly rounded until
they agree. The answer is a second host, a hosted trace ledger, or smaller weights on the
inference server, and all three are decisions for Rupash rather than something to resolve
by editing a constant here.

**The profile is a flag that refuses, not a word in a settings file.** `components_for`
answers what a profile deploys, and that was the whole of it until a lite install could
still carry a `LANGFUSE_HOST` copied from a standard one. Nothing deployed the ledger and
nothing stopped the client library posting spans at it, which fails silently in both
directions: a refused span is retried and dropped inside the client, and an accepted one
means a client's traces are sitting on a host chosen by whoever edited an environment
file. `trace_config_conflicts` is what makes the flag load-bearing.

This module previously claimed no leaves, on the grounds that a memory limit existing only
as a Python constant is not a resource limit on anything. That is still the right test, and
it is now satisfied from the other side: `docker-compose.langfuse.yml` carries the limits
and `tests/unit/test_wiring.py` holds the two copies equal, so neither can move alone.

Still not claimed: M32.1.1.1. The service set is written and has never been started, because
there is no host it fits on. A compose file that has never run is a design.

Task ids: M32.1.1.4
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Literal

#: The machine, in mebibytes, as `free -m` reports it. Measured 2026-09-06 on the live host.
HOST_TOTAL_MIB: Final = 11960

#: What everything that is not this system already reserves on that machine, measured the
#: same day with `docker inspect -f {{.HostConfig.Memory}}` over every running container.
#:
#: Three groups, and every one of them belongs to the owner's other project rather than to
#: this one: the Dify stack at 3,712 MiB across ten containers, an older Langfuse at
#: 1,280 MiB across two, and a v1 worker at 1,024 MiB. `docs/needs-rupash.md` item 25 lists
#: them by name, because the whole value of this figure to the person who can act on it is
#: knowing which containers to remove.
#:
#: **Reservations rather than usage, and the distinction is the reason this constant exists
#: rather than a reading of free memory.** Seven containers were deleted from this host on
#: 2026-09-06 and available memory went from 6,319 MiB to 8,469 MiB, and this number did not
#: move by a single mebibyte, because every container removed was one that reserved nothing.
#: Free memory is what is spare at an instant. A reservation is memory the kernel will hand
#: to a neighbour the moment it asks. Sizing against the first is how a stack runs all week
#: and kills something on the day the neighbour gets busy.
NEIGHBOUR_MIB: Final = 6016

#: What this system's declared limits may add up to on the shared host, in mebibytes.
#: Approximate on purpose: it is headroom above a neighbour whose own usage moves, so
#: treating it as exact would be false precision. Sizing against it is still the point,
#: because the alternative is sizing against nothing.
#:
#: **It is a self-imposed cap, not the size of the machine, and it was documented as the
#: latter.** Measured on the live host 2026-09-06: 11,960 MiB total, 5,641 MiB in use, of
#: which this system accounts for 394 MiB. `docs/needs-rupash.md` item 25 said "your server
#: has about 6.4 GB usable" and that sentence was describing this constant. Corrected there.
#:
#: The cap does not rise to meet the measurement, and the reason is the rest of the box:
#: the containers that reserve memory are already allowed 9,600 MiB of the 11,960. The host
#: is overcommitted before this system asks for anything, so headroom measured as "free
#: right now" is headroom that belongs to whichever neighbour grows first. Sizing to it
#: would make this system the reason somebody else's production is killed, which is the one
#: outcome the whole module exists to prevent.
#:
#: **It was 6400 and 6400 did not fit either.** Re-measured 2026-09-06 after seven
#: containers were removed from the host: the neighbours still reserve `NEIGHBOUR_MIB`, and
#: 6400 on top of that came to 12,416 MiB on an 11,960 MiB machine. The cap was 712 MiB
#: past what the box could honour and nothing said so, because the only test on it re-derived
#: it from itself. It is now `safe_headroom_mib()`, computed from the measurement below, so
#: the number cannot drift from the machine without the recorded machine drifting too.
HOST_HEADROOM_MIB: Final = 5688

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

#: What an install that never says runs. Declared once, here, because it was briefly
#: written in both `brain.app.Settings` and `brain.config.check`, and a default in two
#: places is a default that disagrees with itself the first time one of them is edited.
#:
#: Lite is the only safe value. It deploys nothing beyond the four base services, so
#: forgetting the variable leaves an install under-featured; any other default makes
#: forgetting it the expensive mistake, and on this host `full` does not fit at all.
DEFAULT_PROFILE: Final[Literal["lite", "standard", "full"]] = "lite"


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
        # Separate from `brain-worker` because of arithmetic rather than tidiness. The
        # general worker is 384 MiB across seven slots, which is 48 MiB of slot for a job,
        # and the knowledge door admits a 50 MiB PDF: a parse there cannot hold its own
        # input. The size below is what the door's largest admissible file costs once
        # expanded, plus the reserve the cgroup counts and a parse budget does not.
        # `brain.knowledge.parse_budget.parse_worker_gaps` holds the two ends together and
        # says no when asked about `brain-worker`.
        name="brain-parse-worker",
        memory_mib=512,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.DIRECT,
        needs_session_state=True,
        ready_when=(
            "the queue driver has fetched at least once and the object store returns an "
            "original by key"
        ),
    ),
    Component(
        name="seaweedfs",
        memory_mib=256,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="the S3 gateway lists a known bucket",
    ),
    Component(
        # The identity provider. Sized from measurement rather than from a guess: a
        # throwaway Keycloak 26.0.8 on this host was OOM-killed at a 512 MiB cgroup
        # (exit 137) and ran at 477 MiB steady, 62%, under 768.
        #
        # Not in `lite`, and that is a statement about today rather than about importance.
        # `lite` is what production actually runs and production has no Keycloak, so
        # putting it there would make the profile describe something that is not deployed.
        # Deploying it means production moves to `standard`, which is a decision with a
        # memory cost rather than a word in a frozenset.
        #
        # `Wiring.NONE` is the load-bearing part. Keycloak has its own Postgres on its own
        # internal network and no connection string to this system's database, so it cannot
        # be pointed at one by a misconfiguration. Sharing would put the credential store
        # and the company records it protects in one blast radius.
        name="keycloak",
        memory_mib=768,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="/health/ready answers UP, which is true only once the realm is imported",
    ),
    Component(
        # Keycloak's own database. Counted separately because it is a separate container
        # with a separate limit, and a budget that folded it into the figure above would be
        # a budget that does not match `docker stats`.
        name="keycloak-db",
        memory_mib=256,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="pg_isready answers for the keycloak database",
    ),
    Component(
        name="presidio-analyzer",
        memory_mib=512,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when="the analyser returns a detection for a known-positive probe string",
    ),
    Component(
        # The one process that loads a model, and it is not this image. Item 31 of
        # `docs/needs-rupash.md` was decided on 2026-09-06 as Option A: parsing, embedding
        # and entity recognition all go behind one service, because the alternative was
        # 83 further packages and roughly 1.5 GB installed inside a container budgeted 512.
        #
        # **The figure is the largest in this file and it is arithmetic, not a measurement.**
        # `brain.ops.inference.SERVED_MODELS` derives the weights from published parameter
        # counts at a stated precision and adds a runtime reserve that is a judgement; the
        # sum is what this limit has to hold before it holds a single request.
        # `inference_gaps` compares the two ends and `budget_breaches` reports what it does
        # to the profile, which is the point rather than the problem: sizing it to what is
        # left over would produce a container that is OOM-killed the first time three models
        # are resident at once, and on a shared host that is somebody else's outage.
        #
        # `Wiring.NONE` is load-bearing rather than incidental. This service is handed the
        # text of documents that have already passed the permission layer, and it has no
        # connection string to this system's database, so nothing about a response can name
        # a row that was never sent to it. See
        # `brain.ops.inference.THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE`.
        name="inference-server",
        memory_mib=3072,
        profiles=frozenset({"standard", "full"}),
        wiring=Wiring.NONE,
        ready_when=(
            "every model in brain.ops.inference.SERVED_MODELS answers a probe of its own; a "
            "server that has loaded one of three refuses two thirds of what it is asked "
            "while presenting a listening socket"
        ),
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


#: The components that together are the trace ledger. Named as a set rather than found by
#: a `startswith("langfuse")` test, because the prefix is a naming convention and this is a
#: security-relevant boundary: what a lite install must not run, and what a lite install
#: must not be configured to ship to. A convention is a thing somebody renames.
TRACE_LEDGER: Final = frozenset(
    {"langfuse-web", "langfuse-worker", "langfuse-clickhouse", "langfuse-cache"}
)

#: Environment settings that name somewhere to send spans. Any one of them set on an
#: install with no trace ledger is the failure `trace_config_conflicts` exists for.
TRACE_DESTINATION_SETTINGS: Final = ("langfuse_host", "langfuse_public_key", "langfuse_secret_key")

#: Why lite is not "tracing off". The distinction matters to anybody reading a lite
#: install's records during an incident and finding them thinner than they expected.
LITE_KEEPS_THE_AUDIT_LEDGER = (
    "a lite install records to brain.audit.ledger and ships no spans. The audit ledger is "
    "the client-facing record of who read what, it is append-only, and it is not optional "
    "in any profile. What lite gives up is the step-by-step trace an operator reads to "
    "explain how an answer was assembled, which is a diagnostic aid and not an obligation"
)


def runs_trace_ledger(profile: str) -> bool:
    """Whether this profile runs somewhere for spans to go.

    Derived from `COMPONENTS` rather than declared a second time. A profile gains a trace
    ledger by a component naming it, which is the same edit that puts it in the budget, so
    the two cannot disagree.
    """
    assert_known_profile(profile)
    return any(c.name in TRACE_LEDGER for c in components_for(profile))


def trace_config_conflicts(profile: str, values: dict[str, str]) -> tuple[str, ...]:
    """Trace destinations configured on an install that runs no trace ledger.

    **This is the profile flag doing something rather than describing something.** Without
    it, `profile=lite` is a word in a settings file: the components are not deployed, but
    a `LANGFUSE_HOST` left over from a standard install is still read by the client
    library, and the process spends the rest of its life posting spans at a host that
    either refuses them or, worse, belongs to somebody else and accepts them.

    Neither failure is visible from the application. A refused span is retried and dropped
    inside the client; an accepted one looks like success. So this is checked at startup
    against the declaration, where it fails loudly, rather than trusted to whoever copies
    an environment file between two installs.

    Returns every conflict rather than the first, matching `brain.config.check`: a
    misconfiguration found one variable at a time is a sequence of restarts.
    """
    assert_known_profile(profile)
    if runs_trace_ledger(profile):
        return ()
    return tuple(
        f"{setting} is set but profile {profile!r} runs no trace ledger, so these spans "
        f"have nowhere to go that we control. Unset it, or deploy a profile that runs one. "
        f"Note that {LITE_KEEPS_THE_AUDIT_LEDGER}."
        for setting in TRACE_DESTINATION_SETTINGS
        if (values.get(setting) or "").strip()
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


def safe_headroom_mib(
    *,
    host_mib: int = HOST_TOTAL_MIB,
    neighbour_mib: int = NEIGHBOUR_MIB,
    reserve_mib: int = HOST_RESERVE_MIB,
) -> int:
    """The largest cap this system may hold itself to without overcommitting the machine.

    The arithmetic is the whole argument: what is on the box, less what the neighbours are
    already promised, less what the host keeps for itself. Anything above this is a cap that
    the machine cannot honour if every reservation is called at once, which is the only
    moment a limit matters.

    A function rather than a constant, and that is the point of it. `HOST_HEADROOM_MIB` used
    to be a number with a paragraph of justification and nothing checking either, so the
    paragraph stayed true while the number stopped being. Now the number is the output of the
    measurement, the measurement is three constants a reader can compare against `free -m`
    and `docker inspect`, and a test asserts the two agree. Changing the cap without changing
    the recorded machine fails.

    Parameterised for the question that is actually asked of it: "what if the old project
    goes away" is `safe_headroom_mib(neighbour_mib=0)`, which needs no edit to answer.
    """
    return host_mib - neighbour_mib - reserve_mib


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
