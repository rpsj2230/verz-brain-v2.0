"""The automation sandbox as deployed, held to the boundary `brain.ops.automation` declares.

That module opens by saying the canvas runs flows a client's own staff assemble, so the code
inside it is written by somebody outside this repository and is treated as hostile: no
database URL, no vault token, no provider key, and an egress allowlist rather than an open
network. It then says plainly that the container has a memory budget and no compose service.

This is that boundary as a deployment, and these tests are what stop the two drifting. A
boundary that exists only as a Python constant is a boundary nothing enforces, which is the
same gap that left `ToolRegistry` unbuilt until yesterday and the retention policy untold to
any store until this morning.

**The network is the load-bearing part.** The allowlist matters, and an allowlist checked
inside the sandbox is one the sandbox can edit. What makes it hold is that `automation` is an
internal network with no route out, so the proxy is the only way through rather than the
polite way through.

Task ids: M32.6.1.1
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.ops.automation import EGRESS_ALLOWLIST
from brain.ops.wiring import COMPONENTS

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.automation.yml"
EGRESS_CONF = REPO / "ops" / "automation" / "egress.conf"

#: `acl <name> dstdomain <host>` with no leading dot. The absence of the dot is the point and
#: is asserted separately, so this captures the host as written rather than normalising it.
DSTDOMAIN_RE = re.compile(r"^acl\s+\S+\s+dstdomain\s+(\S+)\s*$", re.MULTILINE)


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _service(name: str) -> dict[str, Any]:
    service: dict[str, Any] = _compose()["services"][name]
    return service


# --------------------------------------------------------------- the allowlist is deployed
def test_the_proxy_allows_exactly_the_hosts_the_policy_names() -> None:
    """Two copies of five hostnames, safe only while something compares them.

    Both directions. A host in the config and not in the policy is a route nobody reviewed;
    a host in the policy and not in the config is a source the canvas cannot reach, which
    presents as a flow that mysteriously fails rather than as a missing line.

    Delete this and the proxy config becomes the real allowlist while `automation.py` keeps
    describing a different one."""
    configured = set(DSTDOMAIN_RE.findall(EGRESS_CONF.read_text(encoding="utf-8")))

    assert configured == set(EGRESS_ALLOWLIST)


def test_no_allowlisted_host_is_written_as_a_suffix() -> None:
    """**The bug `brain.ops.automation` spends a paragraph on, in the one place it can
    actually happen.**

    Squid's leading-dot form matches every subdomain. `.xero.com` therefore admits any host
    under it, and the module's own argument is that a suffix check admits
    `notapi.lark.com` because that string genuinely ends with `api.lark.com`. The Python
    side matches exactly; this asserts the deployment does too.

    Delete this and one convenient dot reopens the hole the constant was written to close."""
    configured = DSTDOMAIN_RE.findall(EGRESS_CONF.read_text(encoding="utf-8"))

    suffixed = [host for host in configured if host.startswith(".")]
    assert not suffixed, f"these are written as suffixes and match every subdomain: {suffixed}"


def test_the_proxy_denies_by_default() -> None:
    """An allowlist under a permissive default is decoration. `http_access deny all` has to
    be the last word, because Squid takes the first rule that matches and a later allow would
    never be reached to be noticed as wrong."""
    lines = [
        line.strip()
        for line in EGRESS_CONF.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("http_access")
    ]

    assert lines[-1] == "http_access deny all"


# --------------------------------------------------------------- the network is the boundary
def test_the_canvas_sits_on_an_internal_network_with_no_route_out() -> None:
    """**The line that makes the allowlist enforceable rather than advisory.**

    If the canvas could reach the internet directly, the proxy would be a convention: a flow
    that unsets `HTTP_PROXY` would simply go around it. On an internal network there is
    nowhere to go around to, so the proxy is the only route rather than the preferred one.

    Delete this and `internal: true` can be dropped to fix some unrelated connectivity
    problem, and every remaining test here still passes."""
    compose = _compose()

    assert compose["networks"]["automation"]["internal"] is True
    assert _service("activepieces")["networks"] == ["automation"]


def test_only_the_proxy_touches_a_network_that_reaches_out() -> None:
    """One service on both sides, and it is the one whose whole job is deciding what crosses.
    A second would be a second route, and the allowlist only constrains this one."""
    compose = _compose()
    outward = [
        name
        for name, service in compose["services"].items()
        if "egress" in (service.get("networks") or [])
    ]

    assert outward == ["automation-egress"]


def test_the_canvas_is_not_on_the_application_stack_at_all() -> None:
    """Being on the application's network would let a flow reach `db:5432` and `cache:6379`
    by hostname, and the absence of credentials would then be the only thing between an
    assembled flow and the database.

    Two defences are better than one, and the network is the one that still holds when
    somebody adds an environment variable in a hurry."""
    for name, service in _compose()["services"].items():
        networks = service.get("networks") or []
        assert "default" not in networks, f"{name} joins the default network"
        assert set(networks) <= {"automation", "egress"}, f"{name} reaches {networks}"


# --------------------------------------------------------------- no credentials of ours
def test_no_service_is_handed_a_credential_belonging_to_the_application() -> None:
    """The canvas keeps its own store and is given nothing of ours. This checks the file for
    the application's own variable names rather than for the word "password", because the
    sandbox legitimately has a password: its own.

    Delete this and `POSTGRES_PASSWORD` gets interpolated here by somebody wiring the two
    together, which is one line, and one line is how a sandbox stops being one."""
    text = COMPOSE.read_text(encoding="utf-8")

    for forbidden in (
        "${POSTGRES_PASSWORD}",
        "${APP_ROLE_PASSWORD}",
        "${BRAIN_DATABASE_URL}",
        "${DATABASE_URL}",
        "${VALKEY_URL}",
        "${LANGFUSE_S3_SECRET_ACCESS_KEY}",
    ):
        assert forbidden not in text, f"the sandbox is handed {forbidden}"


def test_the_canvas_points_at_its_own_database_and_not_ours() -> None:
    """`automation-db` is on the internal network and exists only for this. The application's
    `db` service is on another network entirely and is not named here."""
    environment = _service("activepieces")["environment"]

    assert environment["AP_POSTGRES_HOST"] == "automation-db"
    assert environment["AP_POSTGRES_DATABASE"] == "activepieces"


def test_every_outbound_request_is_pointed_at_the_proxy() -> None:
    """Set in the deployment rather than left to whoever writes a flow. The internal network
    means unsetting these reaches nothing rather than reaching everything, so this is the
    belt and the network is the braces."""
    environment = _service("activepieces")["environment"]

    assert environment["HTTP_PROXY"] == "http://automation-egress:3128"
    assert environment["HTTPS_PROXY"] == "http://automation-egress:3128"


# --------------------------------------------------------------- sizing
def test_the_canvas_is_sized_to_the_component_it_is_budgeted_as() -> None:
    """`brain.ops.wiring` decides what `activepieces` may take and every profile's arithmetic
    is computed from it. More here is memory no budget accounted for."""
    declared = {c.name: c.memory_mib for c in COMPONENTS}
    limit = _service("activepieces")["deploy"]["resources"]["limits"]["memory"]

    assert int(str(limit).rstrip("M")) == declared["activepieces"]


@pytest.mark.parametrize("service", ["activepieces", "automation-egress", "automation-db"])
def test_every_service_in_the_sandbox_carries_a_memory_limit(service: str) -> None:
    """The rule the whole host depends on. An unlimited container is a neighbour's outage,
    and this file adds three of them at once."""
    limits = _service(service)["deploy"]["resources"]["limits"]

    assert "memory" in limits


def test_nothing_here_is_published_to_the_host() -> None:
    """`expose` publishes to the compose network; `ports` publishes to the world. A canvas
    reachable from the host is one reachable from anywhere the host is, and this one runs
    code somebody outside this repository wrote."""
    for name, service in _compose()["services"].items():
        assert "ports" not in service, f"{name} publishes a port to the host"
