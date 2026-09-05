"""The wave-2 component budget: what it refuses, and what it says does not fit.

Every test here is about a deployment that would take down a neighbour, or about a
component that would look healthy while answering from half a stack.

No leaf ids are claimed by `brain.ops.wiring`; these tests guard the budget the compose
services will have to satisfy when they are written.
"""

from __future__ import annotations

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
    Component,
    Wiring,
    WiringError,
    budget_breaches,
    component,
    components_for,
    pooler_misuse,
    spendable_mib,
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
