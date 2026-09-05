"""An agent can never reach past its caller. A failure here blocks deploy.

The catalogue is where the platform's central claim is actually enforced. Everything else
about agents is configuration; this is the mechanism.

Task ids: M3.7.1, M3.7.2, M3.7.3, M3.7.4
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import SideEffect, ToolDefinition
from brain.core.scope import Clause, Op, Scope
from brain.gate.catalogue import (
    AgentCeiling,
    EmptyCatalogueError,
    ProjectedCatalogue,
    project,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _tool(name: str, cap: str, effect: SideEffect = SideEffect.NONE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"does {name}",
        entity=name.split(".", 1)[0],
        required_capability=cap,
        side_effect=effect,
    )


REGISTRY: tuple[ToolDefinition, ...] = (
    _tool("client.read_summary", "read:client.name"),
    _tool("client.read_money", "read:client.contract_value"),
    _tool("ticket.read_status", "read:ticket.status"),
    _tool("ticket.set_status", "write:ticket.status", SideEffect.WRITE),
    _tool("invoice.send_invoice", "write:invoice.status", SideEffect.SEND),
    _tool("payment.release_payment", "approve:payment.release", SideEffect.MONEY),
)

ALL_TOOLS = frozenset(t.name for t in REGISTRY)


def _ents(*caps: str, principal: str = "p_wei_ling") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
    )


WIDE = AgentCeiling(agent_id="a_wide", allowed_tools=ALL_TOOLS, max_side_effect=SideEffect.MONEY)


# ------------------------------------------------- an agent never exceeds its caller
def test_an_agent_cannot_reach_a_tool_its_caller_does_not_hold() -> None:
    """The central claim of the whole platform. If a widely-allowed agent could hand a
    narrow caller a tool they do not hold, the agent would be a principal."""
    caller = _ents("read:client.name")
    shown = project(REGISTRY, caller, WIDE, now=NOW).names
    assert shown == ("client.read_summary",)


def test_widening_the_agent_grants_the_caller_nothing() -> None:
    """An operator who widens an agent's ceiling by mistake hands out no permissions. That
    asymmetry is what makes agent configuration a safe thing to delegate."""
    caller = _ents("read:client.name")
    narrow = AgentCeiling(agent_id="a", allowed_tools=frozenset({"client.read_summary"}))
    assert project(REGISTRY, caller, narrow, now=NOW).names == (
        project(REGISTRY, caller, WIDE, now=NOW).names
    )


def test_the_agent_ceiling_narrows_a_wide_caller() -> None:
    """The other direction. A Super Admin using a reporting agent gets the reporting
    agent's tools, not everything they personally hold."""
    admin = _ents(*[t.required_capability for t in REGISTRY], principal="p_admin")
    reporting = AgentCeiling(agent_id="a_report", allowed_tools=frozenset({"ticket.read_status"}))
    assert project(REGISTRY, admin, reporting, now=NOW).names == ("ticket.read_status",)


def test_a_caller_holding_everything_still_gets_a_catalogue() -> None:
    """A Super Admin's projection equals the registry, which is correct and must not be
    mistaken for a failure to project. An earlier version of this module refused exactly
    this case, on the theory that a full catalogue meant filtering had been skipped."""
    admin = _ents(*[t.required_capability for t in REGISTRY], principal="p_admin")
    assert set(project(REGISTRY, admin, WIDE, now=NOW).names) == ALL_TOOLS


def test_an_expired_principal_is_shown_nothing() -> None:
    """Expiry lives on the entitlement set, so it has to be honoured wherever the set is
    consulted, not only at login."""
    expired = EntitlementSet(
        principal_id="p_contractor",
        grants=(Grant(capability=Capability(value="read:client.name"), scope=Scope()),),
        not_after=NOW - timedelta(days=1),
    )
    assert project(REGISTRY, expired, WIDE, now=NOW).names == ()


# ------------------------------------------------------------------ side effects
def test_a_read_only_agent_cannot_be_given_a_writing_tool() -> None:
    """The side-effect ceiling is separate from the leash: this decides reach, the leash
    decides supervision. A read-only agent should not have a write tool to be supervised
    about in the first place."""
    caller = _ents("read:ticket.status", "write:ticket.status")
    read_only = AgentCeiling(
        agent_id="a_ro", allowed_tools=ALL_TOOLS, max_side_effect=SideEffect.NONE
    )
    shown = project(REGISTRY, caller, read_only, now=NOW).names
    assert "ticket.set_status" not in shown
    assert "ticket.read_status" in shown


def test_the_money_boundary_needs_an_explicit_ceiling() -> None:
    """Nothing reaches a money tool by inheriting a default. The ceiling has to name it."""
    caller = _ents("approve:payment.release")
    for effect in (SideEffect.NONE, SideEffect.DRAFT, SideEffect.WRITE, SideEffect.SEND):
        ceiling = AgentCeiling(agent_id="a", allowed_tools=ALL_TOOLS, max_side_effect=effect)
        assert "payment.release_payment" not in project(REGISTRY, caller, ceiling, now=NOW).names


# -------------------------------------------------------------- unreachable is absent
def test_an_unreachable_tool_is_absent_rather_than_described_and_refused() -> None:
    """A tool described to a model and then refused teaches the model the capability
    exists, and the model says so in its own words. That sentence is the leak, and it
    arrives through the one channel nobody audits."""
    caller = _ents("read:client.name")
    shown = project(REGISTRY, caller, WIDE, now=NOW)
    assert all("contract" not in t.description for t in shown.tools)
    assert "client.read_money" not in shown.names


def test_a_malformed_capability_makes_a_tool_unreachable_not_unrestricted() -> None:
    """A typo in a manifest must not become an open door. Treating an unparseable
    requirement as "no requirement" is the failure mode."""
    broken = _tool("client.oops_typo", "reed:client.name")
    caller = _ents("read:client.name")
    ceiling = AgentCeiling(agent_id="a", allowed_tools=frozenset({"client.oops_typo"}))
    assert project((broken,), caller, ceiling, now=NOW).names == ()


# ------------------------------------------------------------- the empty catalogue
def test_a_required_tool_that_does_not_resolve_is_an_error_not_an_empty_list() -> None:
    """M3.7.4. An agent handed no tools does not stop: it answers from what it already
    believes, confidently and without citations, and the trace shows a clean run."""
    caller = _ents("read:ticket.status")
    ceiling = AgentCeiling(
        agent_id="a_report",
        allowed_tools=ALL_TOOLS,
        required_tools=frozenset({"client.read_summary"}),
    )
    with pytest.raises(EmptyCatalogueError, match=re.escape("client.read_summary")):
        project(REGISTRY, caller, ceiling, now=NOW)


def test_an_agent_requiring_a_tool_it_is_not_allowed_is_refused_at_configuration() -> None:
    """Permanently broken rather than broken per request. Better to refuse the manifest
    than to fail every call and call it a permission problem."""
    with pytest.raises(ValueError, match="requires tools it is not allowed"):
        AgentCeiling(
            agent_id="a",
            allowed_tools=frozenset({"ticket.read_status"}),
            required_tools=frozenset({"payment.release_payment"}),
        )


def test_an_agent_with_no_required_tools_may_legitimately_project_nothing() -> None:
    """Not every empty catalogue is an error. An agent that declared no requirements and
    resolves nothing is a caller who holds nothing, which is a permission outcome."""
    assert project(REGISTRY, _ents(), WIDE, now=NOW).names == ()


# ------------------------------------------------------ projection happens here only
def test_a_catalogue_cannot_be_built_outside_the_projector() -> None:
    """M3.7.3. The shortcut this prevents is handing a raw tool list to a connector or an
    SDK and relying on that to filter. A remote system's filtering is a filter we cannot
    prove ran."""
    with pytest.raises(EmptyCatalogueError, match="may only be built"):
        ProjectedCatalogue(static=REGISTRY, caller_specific=())


def test_the_projector_can_build_one() -> None:
    """The guard has to admit the legitimate path, or the module is unusable."""
    assert isinstance(project(REGISTRY, _ents(), WIDE, now=NOW), ProjectedCatalogue)


# ------------------------------------------------------------------ prompt caching
def test_two_identical_requests_produce_identical_catalogues() -> None:
    """An unstable ordering defeats prompt caching as thoroughly as varying membership,
    and does it invisibly, because the list still looks right."""
    caller = _ents("read:client.name", "read:ticket.status")
    first = project(REGISTRY, caller, WIDE, now=NOW).names
    for _ in range(10):
        assert project(REGISTRY, caller, WIDE, now=NOW).names == first


def test_the_shared_prefix_is_identical_across_callers() -> None:
    """The whole point of the split. If the prefix varied by person, no request would ever
    hit the provider cache and every one would pay full price for the same preamble."""
    universal = frozenset({"ticket.read_status"})
    a = _ents("read:ticket.status", "read:client.name", principal="p_a")
    b = _ents("read:ticket.status", "approve:payment.release", principal="p_b")
    left = project(REGISTRY, a, WIDE, now=NOW, universal=universal)
    right = project(REGISTRY, b, WIDE, now=NOW, universal=universal)
    assert left.static == right.static
    assert left.cache_prefix_length() == 1


def test_naming_a_tool_universal_does_not_grant_it() -> None:
    """An optimisation hint, never an authority. A mistake in the list costs cache hits
    rather than permissions."""
    caller = _ents("read:client.name")
    shown = project(REGISTRY, caller, WIDE, now=NOW, universal=ALL_TOOLS)
    assert shown.names == ("client.read_summary",)


def test_the_split_does_not_change_what_the_model_sees() -> None:
    """The division is about where the cache boundary falls and nothing else. If it
    changed membership it would be a second permission rule."""
    caller = _ents("read:client.name", "read:ticket.status")
    plain = project(REGISTRY, caller, WIDE, now=NOW)
    split = project(REGISTRY, caller, WIDE, now=NOW, universal=frozenset({"ticket.read_status"}))
    assert set(plain.names) == set(split.names)


# ----------------------------------------------------------------------- scope
def test_a_tool_is_absent_when_the_grant_does_not_reach_this_scope() -> None:
    """Holding a capability somewhere is not holding it here. `scope_for` returns None when
    no grant covers the capability at all, which is the case that must hide the tool."""
    scoped = EntitlementSet(
        principal_id="p_wei_ling",
        grants=(
            Grant(
                capability=Capability(value="read:client.name"),
                scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="maintenance"),)),
            ),
        ),
    )
    shown = project(REGISTRY, scoped, WIDE, now=NOW)
    assert shown.names == ("client.read_summary",)
