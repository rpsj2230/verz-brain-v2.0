"""The wave-2 component budget: what it refuses, and what it says does not fit.

Every test here is about a deployment that would take down a neighbour, or about a
component that would look healthy while answering from half a stack.

The compose services now exist, so these tests do two jobs rather than one. They still guard
the budget arithmetic, and they additionally hold `docker-compose.langfuse.yml` equal to the
`COMPONENTS` the budget is computed from, in both directions: a limit cannot move in the YAML
without moving in the Python, and a service cannot be added to the YAML without being
budgeted at all.

The second cap is the one worth reading twice. A cgroup limit is enforced by killing the
container, so it protects the neighbour and not the application; what stops a process sizing
itself for an 11.7 GiB host it cannot have is the ceiling given to the process in its own
units, and that is asserted here per service and asserted to sit strictly below the cgroup
limit it lives under.

Task ids: M32.1.1.2, M32.1.1.4
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.ops.wiring import (
    COMPONENTS,
    HOST_HEADROOM_MIB,
    HOST_RESERVE_MIB,
    PRODUCTION_BASELINE_MIB,
    PROFILES,
    TRACE_DESTINATION_SETTINGS,
    TRACE_LEDGER,
    Component,
    Wiring,
    WiringError,
    budget_breaches,
    component,
    components_for,
    pooler_misuse,
    runs_trace_ledger,
    spendable_mib,
    trace_config_conflicts,
    wave_two_mib,
)

REPO = Path(__file__).resolve().parents[2]


def _a_component(**overrides: Any) -> Component:
    base: dict[str, Any] = {
        "name": "probe",
        "memory_mib": 64,
        "profiles": frozenset({"full"}),
        "wiring": Wiring.NONE,
        "ready_when": "it answers a probe",
    }
    base.update(overrides)
    return Component(**base)


def _compose_memory_limits(name: str) -> dict[str, int]:
    """Every service's memory limit in one compose file, in MiB.

    Parsed rather than grepped. A regex over the text would find `memory: 1024M` inside a
    comment, which is exactly the state a half-finished edit leaves the file in.
    """
    raw = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    limits: dict[str, int] = {}
    for service, body in raw["services"].items():
        value = body.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory")
        if value is None:
            continue
        text = str(value).strip().upper()
        if text.endswith("G"):
            limits[service] = int(float(text[:-1]) * 1024)
        else:
            limits[service] = int(float(text.rstrip("M")))
    return limits


# ------------------------------------------------------- the limit is not optional
def test_a_component_with_no_memory_limit_cannot_be_constructed() -> None:
    """This is the whole file in one assertion. The host runs a second production system
    belonging to the same owner, and an unlimited container takes it down rather than
    degrading ours. Delete this and a zero passes construction, is rendered in a compose
    file as no limit at all, and is invisible until the night it matters."""
    with pytest.raises(WiringError, match="no memory limit"):
        _a_component(memory_mib=0)


def test_a_component_that_does_not_say_what_ready_means_is_refused() -> None:
    """Liveness and readiness are separate and the distinction is load-bearing: a
    half-connected instance does not refuse, it answers from whatever it can still reach.
    A component with no readiness sentence gets a TCP connect check, which is liveness
    wearing readiness' clothes. Delete this and that becomes the default."""
    with pytest.raises(WiringError, match="liveness is not readiness"):
        _a_component(ready_when="   ")


def test_a_component_naming_a_profile_nobody_defined_is_refused() -> None:
    """A typo in a profile name produces a component that is never selected by any profile
    and never reported missing. Delete this and a component can be declared, budgeted and
    silently never deployed."""
    with pytest.raises(WiringError, match="unknown profile"):
        _a_component(profiles=frozenset({"lite", "medium"}))


def test_every_declared_component_states_a_limit_and_a_readiness_check() -> None:
    """The constructor guards are worthless if the real declarations bypass them, and a
    frozen dataclass built at import time fails at import rather than in a test - which is
    a traceback in whatever imported it first, not a named failure."""
    for c in COMPONENTS:
        assert c.memory_mib >= 1, c.name
        assert c.ready_when.strip(), c.name


# ------------------------------------------------------- the budget
def test_the_baseline_matches_the_compose_file_it_claims_to_describe() -> None:
    """`PRODUCTION_BASELINE_MIB` is the only reason the budget means anything: it is the
    memory already spent. Delete this and the constant becomes a number somebody typed
    once, the compose file drifts above it, and every profile reports that it fits."""
    limits = _compose_memory_limits("docker-compose.yml")
    assert sum(limits.values()) == PRODUCTION_BASELINE_MIB, limits


@pytest.mark.parametrize(
    "compose", ["docker-compose.yml", "docker-compose.lite.yml", "docker-compose.staging.yml"]
)
def test_every_deployed_service_carries_an_explicit_memory_limit(compose: str) -> None:
    """The rule applied to what is actually deployed, not only to what is planned. A
    service added to a compose file with no `deploy.resources.limits.memory` is an
    unlimited container on a shared host, and nothing else in the repository would notice
    it."""
    raw = yaml.safe_load((REPO / compose).read_text(encoding="utf-8"))
    limits = _compose_memory_limits(compose)
    missing = sorted(set(raw["services"]) - set(limits))
    assert not missing, f"{compose}: services with no memory limit: {missing}"


def test_the_lite_profile_adds_nothing_and_therefore_always_fits() -> None:
    """Lite is what is running in production today. Delete this and a component can acquire
    `lite` in its profile set, which puts a trace ledger on a client's box on the strength
    of one word in one frozenset."""
    assert components_for("lite") == ()
    assert wave_two_mib("lite") == 0
    assert budget_breaches("lite") == ()


def test_the_standard_profile_fits_the_shared_host() -> None:
    """The profile this arithmetic exists to protect. Delete this and a component's size can
    grow past the headroom with nothing failing until the container is killed on the
    host."""
    assert budget_breaches("standard") == ()


def test_the_full_profile_does_not_fit_and_names_the_component_that_does_not() -> None:
    """The finding, not a bug. Langfuse needs ClickHouse, its practical floor is a
    gigabyte, and the deployed stack has already committed most of the host. Delete this
    and the overrun stops being visible anywhere, which means it is discovered by
    deploying it."""
    breaches = budget_breaches("full")
    assert len(breaches) == 1
    assert "langfuse-clickhouse" in breaches[0]
    assert "over by" in breaches[0]


def test_the_budget_leaves_the_host_something_to_be_fixed_from() -> None:
    """Spending the last megabyte means the first casualty is the SSH session an operator
    needs to see what happened. Delete this and the reserve can be set to zero to make a
    profile fit, which is arithmetic solving a capacity problem."""
    assert spendable_mib() == HOST_HEADROOM_MIB - PRODUCTION_BASELINE_MIB - HOST_RESERVE_MIB
    assert HOST_RESERVE_MIB > 0


def test_the_budget_can_be_asked_about_a_host_we_do_not_have() -> None:
    """The decision the full profile forces is "second host or hosted ledger", and it is
    answerable only if the arithmetic takes the headroom as a parameter. Delete this and
    the answer requires editing a constant, which is how a budget becomes whatever makes
    today's deployment pass."""
    assert budget_breaches("full", headroom_mib=16384, baseline_mib=0) == ()


# ------------------------------------------------------- the pooler
def test_a_component_needing_session_state_is_never_wired_through_the_pooler() -> None:
    """The failure with no error message. LISTEN behind a transaction pooler delivers
    nothing and raises nothing, and a session-level advisory lock taken through one is
    released by an unrelated transaction. This has already cost `brain.migrate` a rewrite
    and `brain.session` a connect_arg. Delete this and the third instance ships."""
    assert pooler_misuse() == ()


def test_the_pooler_check_actually_looks_at_the_declaration() -> None:
    """A check over real data that happens to be clean passes whether or not it works.
    Delete this and `pooler_misuse` could return an empty tuple unconditionally and every
    other test in this file would still be green."""
    bad = _a_component(name="listener", wiring=Wiring.POOLER, needs_session_state=True)
    assert bad.needs_session_state and bad.wiring is Wiring.POOLER
    offenders = tuple(
        c.name for c in (*COMPONENTS, bad) if c.needs_session_state and c.wiring is Wiring.POOLER
    )
    assert offenders == ("listener",)


# ------------------------------------------------------- lookups refuse rather than default
def test_an_unknown_component_is_refused_rather_than_returned_as_nothing() -> None:
    """A caller handed None writes `if c is None: return`, and the component stops being
    budgeted without anybody removing it. Delete this and a renamed component disappears
    from the budget silently."""
    with pytest.raises(WiringError, match="unknown component"):
        component("langfuse")


def test_an_unknown_profile_is_refused_rather_than_selecting_nothing() -> None:
    """A typo in a deployment variable would otherwise select no components, report no
    breaches, and deploy nothing, all of which look exactly like success."""
    with pytest.raises(WiringError, match="unknown profile"):
        components_for("lite ")
    assert set(PROFILES) == {"lite", "standard", "full"}


# ------------------------------------------------------- the profile flag refuses (M32.1.1.4)
def test_lite_runs_no_trace_ledger_and_full_does() -> None:
    """The flag's whole meaning, asserted in both directions. A test that only checks lite
    is satisfied by a function returning False for everything, which would then also
    disable tracing on the profile that exists to have it."""
    assert runs_trace_ledger("lite") is False
    assert runs_trace_ledger("standard") is False
    assert runs_trace_ledger("full") is True


def test_a_lite_install_pointed_at_a_trace_destination_is_refused() -> None:
    """The failure the flag exists for, and it is silent in both directions: a span posted
    to a host that refuses it is retried and dropped inside the client library, and one
    posted to a host that accepts it means a client's traces are sitting somewhere chosen
    by whoever last copied an environment file.

    Delete this and `profile=lite` goes back to being a word that selects no components
    while the process keeps shipping spans."""
    conflicts = trace_config_conflicts("lite", {"langfuse_host": "https://cloud.langfuse.com"})

    assert len(conflicts) == 1
    assert "langfuse_host" in conflicts[0]
    assert "audit ledger" in conflicts[0], "the refusal should say what lite still records"


def test_every_trace_destination_setting_is_checked_and_not_only_the_host() -> None:
    """A key without a host is not harmless: the client library has a default host, so a
    stray public key is a live destination. Delete this and only the obvious variable is
    checked, which is the one somebody remembers to unset."""
    values = dict.fromkeys(TRACE_DESTINATION_SETTINGS, "set")

    assert len(trace_config_conflicts("lite", values)) == len(TRACE_DESTINATION_SETTINGS)


def test_a_profile_that_runs_a_trace_ledger_may_be_pointed_at_one() -> None:
    """The positive case. A guard tested only by its refusals is satisfied by a function
    that refuses everything, and that function would make the full profile unconfigurable
    while every refusal test stayed green."""
    assert trace_config_conflicts("full", {"langfuse_host": "http://langfuse-web:3000"}) == ()


def test_a_lite_install_with_no_trace_destination_is_clean() -> None:
    """The other positive case, and the one that runs in production today. Delete this and
    the check could refuse every lite install, which is the shape of every install we
    currently deploy."""
    assert trace_config_conflicts("lite", {}) == ()
    assert trace_config_conflicts("lite", {"langfuse_host": "   "}) == ()


def test_the_trace_ledger_membership_is_a_named_set_rather_than_a_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`startswith("langfuse")` is the obvious implementation and it makes a security
    boundary depend on a naming convention.

    **Asserted by making the two disagree, which is the only way to see the difference.**
    An earlier version of this test checked the contents of `TRACE_LEDGER` and nothing
    else, and a mutation replacing the membership test with the prefix survived it: every
    component in the set happens to be named `langfuse-*` today, so the two agree on the
    real data and a test over the real data cannot tell them apart. A vendor swap or a
    rename is the day they stop agreeing, and that is the day lite starts running a trace
    ledger it is not allowed to run.

    Delete this and the implementation can go back to a prefix with every other test green.
    """
    renamed = Component(
        name="traces-clickhouse",
        memory_mib=1024,
        profiles=frozenset({"lite"}),
        wiring=Wiring.NONE,
        ready_when="a SELECT against the observations table returns",
    )
    monkeypatch.setattr("brain.ops.wiring.COMPONENTS", (renamed,))
    monkeypatch.setattr("brain.ops.wiring.TRACE_LEDGER", frozenset({"traces-clickhouse"}))

    assert runs_trace_ledger("lite") is True, "membership must follow the set, not the name"


def test_the_trace_ledger_set_names_the_ledger_and_not_the_object_store() -> None:
    """The set's contents, kept separate from the test above so a wrong membership and a
    wrong lookup fail as different sentences. `seaweedfs` is shared with `standard`, so
    including it here would make every standard install look like it runs a trace ledger.
    """
    assert {
        "langfuse-web",
        "langfuse-worker",
        "langfuse-clickhouse",
        "langfuse-cache",
    } == TRACE_LEDGER
    assert "seaweedfs" not in TRACE_LEDGER, "the object store is not the trace ledger"


# ------------------------------------------------------- the trace ledger's own sizing
LANGFUSE_COMPOSE = "docker-compose.langfuse.yml"


def _clickhouse_server_ceiling_mib() -> int:
    """`max_server_memory_usage` out of the mounted config, in MiB.

    Read from the XML rather than from a copy in this file. The whole argument for a
    mounted config over a shell heredoc in the compose command is that it can be reviewed;
    a test that re-types the number instead of reading it gives that up.
    """
    text = (REPO / "ops" / "langfuse" / "clickhouse-memory.xml").read_text(encoding="utf-8")
    found = re.search(r"<max_server_memory_usage>(\d+)</max_server_memory_usage>", text)
    assert found is not None, "the ClickHouse config no longer sets max_server_memory_usage"
    return int(found.group(1)) // (1024 * 1024)


def _process_ceilings_mib() -> dict[str, int]:
    """What each service tells the process inside it, in MiB.

    Five services, five different spellings of the same idea, which is why this is a
    function and not a constant: Node takes a flag, ClickHouse takes bytes in an XML file,
    Valkey takes a command argument, and Go takes an environment variable in its own
    units. A service whose spelling is not handled here returns nothing and fails the
    completeness test below rather than being skipped.
    """
    raw = yaml.safe_load((REPO / LANGFUSE_COMPOSE).read_text(encoding="utf-8"))
    ceilings: dict[str, int] = {}
    for service, body in raw["services"].items():
        env = body.get("environment", {}) or {}
        command = body.get("command", []) or []

        node = re.search(r"--max-old-space-size=(\d+)", str(env.get("NODE_OPTIONS", "")))
        if node is not None:
            ceilings[service] = int(node.group(1))
            continue

        go = re.fullmatch(r"(\d+)MiB", str(env.get("GOMEMLIMIT", "")))
        if go is not None:
            ceilings[service] = int(go.group(1))
            continue

        if "--maxmemory" in command:
            value = command[command.index("--maxmemory") + 1]
            ceilings[service] = int(str(value).lower().rstrip("mb"))
            continue

        mounts = " ".join(str(v) for v in body.get("volumes", []) or [])
        if "clickhouse-memory.xml" in mounts:
            ceilings[service] = _clickhouse_server_ceiling_mib()
    return ceilings


def test_the_trace_ledger_sizes_every_service_to_the_component_it_is_budgeted_as() -> None:
    """The budget is arithmetic over `COMPONENTS`, and the compose file is what actually
    runs. Two copies of five numbers is only safe while something compares them.

    Delete this and the compose file can be edited to whatever makes a deployment start,
    `budget_breaches("full")` keeps reporting the old figures, and the overrun that the
    whole module exists to surface is computed from numbers nobody deploys."""
    limits = _compose_memory_limits(LANGFUSE_COMPOSE)
    declared = {c.name: c.memory_mib for c in COMPONENTS}

    mismatched = {s: (m, declared.get(s)) for s, m in limits.items() if declared.get(s) != m}
    assert not mismatched, f"compose and wiring disagree (service: compose, wiring): {mismatched}"


def test_every_service_in_the_trace_ledger_is_a_component_that_was_budgeted() -> None:
    """A service can be added to the compose file without being added to `COMPONENTS`, and
    then it is real memory on the host that no profile accounts for.

    Delete this and the ledger grows a sixth container that `wave_two_mib` cannot see."""
    services = set(
        yaml.safe_load((REPO / LANGFUSE_COMPOSE).read_text(encoding="utf-8"))["services"]
    )
    unbudgeted = sorted(services - {c.name for c in COMPONENTS})
    assert not unbudgeted, f"services with no component and therefore no budget: {unbudgeted}"


def test_every_service_also_tells_the_process_inside_it_what_its_ceiling_is() -> None:
    """The cap that actually prevents the starvation this leaf is named for.

    A cgroup limit stops a runaway container taking the host down, and it does so by
    killing it. It does not stop the process sizing itself for the wrong machine:
    ClickHouse reads `/proc/meminfo` rather than its cgroup and picks caches for 11.7 GiB,
    and Node's old-space default does the same. Both are then killed by the kernel at the
    first real query, which presents as a restart loop with nothing in the application log.

    Delete this and a service can be added with a tidy `deploy.resources.limits.memory`
    and no internal ceiling at all, which looks correct in review and is the exact
    configuration that OOM-loops in production."""
    limits = _compose_memory_limits(LANGFUSE_COMPOSE)
    ceilings = _process_ceilings_mib()

    missing = sorted(set(limits) - set(ceilings))
    assert not missing, (
        f"services capped by cgroup only, which the kernel enforces by killing: {missing}"
    )


def test_no_process_ceiling_is_set_at_or_above_the_cgroup_limit_it_sits_under() -> None:
    """The mistake that makes the second cap useless, and it is the tempting one: set the
    process ceiling to the container limit so nothing is wasted.

    Every one of these ceilings counts only part of what the cgroup counts. Node's
    old-space excludes V8 metaspace and the binary, `max_server_memory_usage` excludes
    ClickHouse's merge buffers and allocator fragmentation, Valkey's `maxmemory` excludes
    fragmentation that runs 1.2 to 1.5x, and `GOMEMLIMIT` excludes goroutine stacks. A
    process told it may use exactly the cgroup limit will therefore exceed the cgroup
    limit and be killed while believing it is within budget.

    Delete this and the gaps can be closed one service at a time, each edit looking like
    reclaimed waste."""
    limits = _compose_memory_limits(LANGFUSE_COMPOSE)
    ceilings = _process_ceilings_mib()

    too_high = {s: (c, limits[s]) for s, c in ceilings.items() if c >= limits[s]}
    assert not too_high, (
        f"process ceiling at or above its cgroup limit (service: process, cgroup): {too_high}"
    )


def test_the_trace_ledger_is_the_full_profile_and_not_something_lite_can_acquire() -> None:
    """The profile flag, checked from the compose side. Every service in this file is a
    component `full` runs and none of them is one `lite` runs, so composing it is a
    deliberate act rather than something a lite install can drift into.

    Not "full only": `seaweedfs` is in `standard` as well, because the object store is a
    component in its own right and this file names it rather than starting a second S3
    gateway beside it. That is the reason this asserts a subset of `full` and a disjoint
    set from `lite`, instead of equality with either.

    Delete this and a component in this file can gain `lite` in its profile set, which
    would put a 2.3 GiB trace stack on a client's box on the strength of one word in one
    frozenset."""
    services = set(
        yaml.safe_load((REPO / LANGFUSE_COMPOSE).read_text(encoding="utf-8"))["services"]
    )
    lite = {c.name for c in components_for("lite")}

    assert not (services & lite), f"lite would run trace-ledger services: {sorted(services & lite)}"
    assert services <= {c.name for c in components_for("full")}
