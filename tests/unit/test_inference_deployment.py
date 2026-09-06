"""The inference server as deployed, held to what `brain.ops.inference` declares.

Two numbers and one network, and all three are only safe while something compares them to
the Python that reasons about them.

**The second cap is the part worth reading twice.** A cgroup limit is enforced by the kernel
killing the container, so it protects the neighbours on a shared host and does nothing for
the work in flight. For a model server the usual pair to it does not exist: CPython has no
heap ceiling, and the dominant term is the weights, which are resident rather than allocated.
So the second cap is a request ceiling, and its whole meaning is that it sits strictly below
what the cgroup counts. A test that read it out of the compose file and compared it to itself
would be green for every value it could hold, so it is compared against the arithmetic in
`brain.ops.inference`, which descends from the component's limit and the declared weights.

**The network is the permission boundary.** Text that has already passed the permission layer
crosses it, and `internal: true` is what says that text has no route off this host. A model
server that could reach out is a model server that can be pointed at a hosted API by one
environment variable, and the answers would keep arriving.

Task ids: none
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.knowledge.embed_queue import MIB
from brain.ops.inference import (
    INFERENCE_COMPONENT,
    REQUESTS_AT_ONCE,
    SERVED_MODELS,
    request_ceiling_bytes,
)
from brain.ops.wiring import COMPONENTS, components_for

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.inference.yml"


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _service() -> dict[str, Any]:
    service: dict[str, Any] = _compose()["services"][INFERENCE_COMPONENT]
    return service


def _environment() -> dict[str, str]:
    return {str(k): str(v) for k, v in (_service().get("environment") or {}).items()}


# ------------------------------------------------------------------ the two caps
def test_the_container_is_sized_to_the_component_it_is_budgeted_as() -> None:
    """`brain.ops.wiring` decides what this may take and every profile's arithmetic is
    computed from it. More here is memory no budget accounted for, and this is the largest
    single component in the system, so the gap would be the largest too.

    Delete this and the compose file can be edited to whatever makes a deployment start while
    `budget_breaches` keeps reporting the old figure."""
    declared = {c.name: c.memory_mib for c in COMPONENTS}
    limit = _service()["deploy"]["resources"]["limits"]["memory"]

    assert int(str(limit).rstrip("M")) == declared[INFERENCE_COMPONENT]


def test_the_request_ceiling_in_the_file_is_the_one_the_arithmetic_produces() -> None:
    """**The second cap, and the only thing that makes it a second cap.**

    The number in the compose file is what the server enforces; the number in
    `brain.ops.inference` is what the container can actually spare once three sets of weights
    and a Python runtime are resident. They are the same quantity written in two places, and
    the deployment is only capped twice while they agree.

    Compared against the arithmetic rather than against a copy of the figure, so the
    comparison is with the component's limit and the declared weights rather than with
    itself.

    Delete this and the ceiling can be raised to whatever makes a large batch go through, the
    container is then promised memory the weights already hold, and the enforcement left is
    the kernel's."""
    assert int(_environment()["INFERENCE_MAX_REQUEST_BYTES"]) == request_ceiling_bytes()


def test_the_request_ceiling_sits_strictly_below_the_cgroup_limit_it_lives_under() -> None:
    """The mistake that makes a second cap useless, and it is the tempting one: set the
    process-side ceiling to the container limit so that nothing is wasted.

    Here the gap is not slack at all, it is the weights and the runtime. A request ceiling
    equal to the cgroup limit would be a server told it may spend on one request everything
    the container also has to hold three models in.

    Delete this and the gap can be closed to reclaim what looks like waste, and the container
    dies on its first request."""
    ceiling = int(_environment()["INFERENCE_MAX_REQUEST_BYTES"])
    limit_mib = int(str(_service()["deploy"]["resources"]["limits"]["memory"]).rstrip("M"))

    assert ceiling < limit_mib * MIB


def test_the_file_serves_one_request_at_a_time_because_the_ceiling_is_the_whole_remainder():
    """Two copies of a small integer, which is safe only while something compares them. The
    ceiling above is what is left after the weights and the runtime, so a server configured
    to serve two requests at once would be allowed that remainder twice."""
    assert int(_environment()["INFERENCE_REQUESTS_AT_ONCE"]) == REQUESTS_AT_ONCE


def test_the_only_ceiling_in_the_runtime_s_own_units_is_set() -> None:
    """torch and onnxruntime size their intra-op thread pools from the host's core count
    rather than from the cgroup, which is the same mistake ClickHouse makes reading
    `/proc/meminfo`. Every one of those threads carries a workspace, so an unbounded pool on
    an eight-core host is eight workspaces inside a budget written for one request.

    `MALLOC_ARENA_MAX` is the allocator's half of the same problem, and it matters more here
    than on the parse worker because inference allocates and frees in large blocks, which is
    exactly the pattern glibc's arenas retain.

    Delete this and the container looks correctly capped in review and grows to a plateau set
    by whichever host it lands on."""
    environment = _environment()

    assert int(environment["OMP_NUM_THREADS"]) >= 1
    assert int(environment["MALLOC_ARENA_MAX"]) >= 1


# ------------------------------------------------------------------ the boundary
def test_the_server_sits_on_an_internal_network_with_no_route_off_the_host() -> None:
    """**The line that makes the permission boundary a fact rather than a promise.** The text
    of a client's documents crosses this network. If the container could reach the internet,
    one environment variable would be enough to point a model at a hosted API, the answers
    would keep arriving, and nothing about the system would look different.

    Delete this and `internal: true` can be dropped to fix some unrelated connectivity
    problem, and every other test here still passes."""
    compose = _compose()

    assert compose["networks"]["inference"]["internal"] is True
    assert _service()["networks"] == ["inference"]


def test_the_server_is_on_no_network_that_reaches_the_application_s_database() -> None:
    """`brain.ops.wiring` records this component as `Wiring.NONE`, meaning it holds no
    connection string to our database. The network is the other half of the same statement
    and is the half that still holds when somebody adds an environment variable in a hurry:
    on the default network `db:5432` resolves by hostname, and the absence of a password
    would be the only thing left."""
    for name, service in _compose()["services"].items():
        networks = service.get("networks") or []
        assert "default" not in networks, f"{name} joins the application's network"
        assert set(networks) == {"inference"}, f"{name} reaches {networks}"


def test_nothing_here_is_published_to_the_host() -> None:
    """`expose` publishes to the compose network; `ports` publishes to the world. A model
    server reachable from the host is one anybody who reaches the host can hand text to and
    read answers from, and this host has neighbours."""
    for name, service in _compose()["services"].items():
        assert "ports" not in service, f"{name} publishes a port to the host"


def test_the_server_is_handed_no_credential_belonging_to_the_application() -> None:
    """It has its own weights and needs nothing else. Checked against the application's own
    variable names rather than for the word "password", matching
    `tests/unit/test_automation_deployment.py`: what is being refused is a credential of
    ours reaching a process that only ever needs text.

    Delete this and `DATABASE_URL` gets interpolated here by somebody wiring the two
    together, which is one line."""
    text = COMPOSE.read_text(encoding="utf-8")

    for forbidden in (
        "${POSTGRES_PASSWORD}",
        "${APP_ROLE_PASSWORD}",
        "${BRAIN_DATABASE_URL}",
        "${DATABASE_URL}",
        "${VALKEY_URL}",
    ):
        assert forbidden not in text, f"the inference server is handed {forbidden}"


def test_the_weights_are_mounted_read_only() -> None:
    """A server that could write to its own model directory would let a set of weights be
    replaced without a deploy, and a vector space that changes behind a stable name is the
    model change `brain.knowledge.embedding` says has no symptom: retrieval does not break,
    it degrades, and nobody files a bug about an answer that is slightly off."""
    mounts = [str(v) for v in (_service().get("volumes") or [])]

    assert mounts, "the weights have to come from somewhere"
    assert all(mount.endswith(":ro") for mount in mounts), mounts


def test_the_server_does_not_fetch_its_own_weights_at_start() -> None:
    """The other half of the internal network. A server that pulls from a model hub on boot
    needs the route out this file exists to deny, and it makes a restart depend on somebody
    else's uptime; the same restart would also be the moment a different revision arrives
    under the same name."""
    assert _environment()["HF_HUB_OFFLINE"] == "1"


# ------------------------------------------------------------------ the file and the module
def test_the_image_is_required_rather_than_defaulted_to_one_that_does_not_serve_these() -> None:
    """**There is no published image serving Qwen3 embeddings, Docling and GLiNER together.**
    A `:-` default would be a container that starts, serves something nobody chose, and is
    discovered by the answers getting quietly worse; `:?` is a deployment that refuses.
    `docker-compose.keycloak.yml` requires its admin password the same way and for the same
    reason.

    Delete this and a placeholder image becomes the deployed one."""
    image = str(_service()["image"])

    assert image.startswith("${INFERENCE_IMAGE:?"), image


def test_every_model_the_file_names_is_one_the_budget_accounted_for() -> None:
    """The weights total in `brain.ops.inference` is what the container's limit was computed
    from, so a fourth model named here and declared nowhere is resident memory no budget
    knows about, in the largest container in the system.

    Delete this and a model can be added to the deployment without appearing in the sum that
    sized the container it runs in."""
    declared = {model.name for model in SERVED_MODELS}
    named = {
        value for key, value in _environment().items() if key.endswith("_MODEL") and value.strip()
    }

    assert named == declared, f"compose names {sorted(named)}, module declares {sorted(declared)}"


def test_the_profiles_in_the_file_match_the_profiles_the_component_is_budgeted_in() -> None:
    """Both directions. A compose profile the component does not have deploys a container no
    profile's arithmetic includes; a component profile the file does not have budgets memory
    for a container that never starts, which makes every other profile look tighter than it
    is."""
    from_file = set(_service()["profiles"])
    from_module = {
        profile
        for profile in ("lite", "standard", "full")
        if INFERENCE_COMPONENT in {c.name for c in components_for(profile)}
    }

    assert from_file == from_module


@pytest.mark.parametrize("service", ["inference-server"])
def test_every_service_in_the_file_carries_a_memory_limit(service: str) -> None:
    """The rule the whole host depends on, applied at the point a service is added rather
    than only to the ones that exist today. An unlimited container is a neighbour's outage,
    and this file's container is the largest thing this system would deploy."""
    assert "memory" in _compose()["services"][service]["deploy"]["resources"]["limits"]
