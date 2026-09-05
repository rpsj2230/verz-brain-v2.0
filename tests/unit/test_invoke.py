"""Assembling one agent run. Every test is a way a run could be more trusted than it should
be, or could start when it should not.

Task ids: M3.8.1
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.scope import Clause, Op, Scope
from brain.gate.catalogue import AgentCeiling
from brain.gate.injection import ELEVATED, HIGH, AutonomyTier, RiskAssessment
from brain.gate.invoke import Invocation, InvocationRefusedError, invoke, run
from brain.gate.leash import Leash, LeashEntry
from brain.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
AGENT = "ag_support"
CLEAN = RiskAssessment(score=0, matched=())


class ClientRow(Entity):
    name: str = ""


def a_handler() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow]()


def _definition(name: str, capability: str, effect: SideEffect = SideEffect.NONE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="does a thing",
        entity=name.split(".")[0],
        required_capability=capability,
        side_effect=effect,
        identity_mode=IdentityMode.DELEGATED,
    )


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(_definition("client.read_summary", "read:client.name"), a_handler)
    r.register(_definition("ticket.read_status", "read:ticket.status"), a_handler)
    return r


def _entitlement(*capabilities: str, principal_id: str = "u_weiling") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _ceiling(*tools: str) -> AgentCeiling:
    return AgentCeiling(agent_id=AGENT, allowed_tools=frozenset(tools))


def _leash(rung: AutonomyTier, *targets: str) -> Leash:
    """A leash covering every tool in the default registry unless told otherwise.

    Covering both by default matters: an unleashed tool drags the whole run to shadow, which
    is correct behaviour and would otherwise mask every other property being tested here.
    """
    names = targets or ("client.read_summary", "ticket.read_status")
    return Leash(
        entries=tuple(
            LeashEntry(agent_id=AGENT, target=t, scope=Scope.unrestricted(), rung=rung)
            for t in names
        )
    )


def _invoke(**overrides: object) -> Invocation:
    kwargs: dict[str, object] = {
        "principal_id": "u_weiling",
        "agent_id": AGENT,
        "registry": _registry(),
        "entitlement": _entitlement("read:client.name", "read:ticket.status"),
        "ceiling": _ceiling("client.read_summary", "ticket.read_status"),
        "leash": _leash(AutonomyTier.AUTONOMOUS),
        "assessment": CLEAN,
        "now": NOW,
    }
    kwargs.update(overrides)
    return invoke(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- what it assembles
def test_an_invocation_carries_only_what_the_caller_reaches() -> None:
    """The happy path, and the property the whole module exists for: the tool list is the
    intersection of what the caller holds and what the agent is allowed, never the
    registry."""
    inv = _invoke(ceiling=_ceiling("client.read_summary"))
    assert inv.reachable == ("client.read_summary",)
    assert inv.tool_count == 1


def test_a_tool_the_caller_cannot_reach_is_absent_rather_than_refused() -> None:
    """Absent, not listed-and-refused. A catalogue entry for a tool the caller cannot use
    teaches the model the tool exists, and the model will say so - which is the leak
    `catalogue.py` exists to close, restated here because this is where the catalogue is
    handed onward."""
    inv = _invoke(entitlement=_entitlement("read:client.name"))
    assert inv.reachable == ("client.read_summary",)


def test_the_catalogue_carried_is_the_projected_one_and_cannot_be_forged() -> None:
    """`ProjectedCatalogue` cannot be constructed outside `project`, so an `Invocation`
    holding one is proof the projection ran rather than a promise that it did. Deleting this
    invites a signature taking a plain tuple, which any caller can build."""
    from brain.gate.catalogue import EmptyCatalogueError, ProjectedCatalogue

    inv = _invoke()
    assert isinstance(inv.catalogue, ProjectedCatalogue)
    with pytest.raises(EmptyCatalogueError):
        ProjectedCatalogue(static=(), caller_specific=())


def test_the_entitlement_hash_is_recorded_and_not_the_entitlement() -> None:
    """The audit entry written afterwards has to describe the reach this run had. A hash
    answers "was this the same reach as that one" without the record becoming a copy of the
    permission map."""
    import dataclasses

    inv = _invoke()
    assert len(inv.ent_hash) == 32
    # There is no field that could hold the caller's grants. A tool's *declared* requirement
    # does appear, and correctly so: it is public, and the model is shown it in the
    # catalogue. What must not be here is the set of things this particular person holds.
    names = {f.name for f in dataclasses.fields(inv)}
    assert "entitlement" not in names
    assert "grants" not in names
    assert not any(isinstance(getattr(inv, n), EntitlementSet) for n in names)


# ------------------------------------------------------------------ refusals
def test_a_caller_reaching_nothing_is_refused_rather_than_given_an_empty_run() -> None:
    """The refusal that matters most. A model handed no tools does not stop: it answers from
    context and from training, and that answer looks exactly like a researched one.

    Raised rather than returned empty, because a caller that got an empty `Invocation` would
    have to remember to check, and the check that must be remembered is the one skipped in
    the path nobody tested."""
    with pytest.raises(InvocationRefusedError, match="reaches no tool"):
        _invoke(entitlement=_entitlement("read:invoice.amount"))


def test_an_agent_allowed_nothing_is_refused() -> None:
    """The other direction: the caller holds plenty and the agent's ceiling admits none of
    it."""
    with pytest.raises(InvocationRefusedError, match="reaches no tool"):
        _invoke(ceiling=_ceiling())


# --------------------------------------------------------------- the rung
def test_the_run_takes_the_strictest_rung_across_everything_it_can_reach() -> None:
    """Strictest-wins across the catalogue, for the same reason it is strictest-wins across
    overlapping entries. A run that took the highest rung any of its tools allowed would let
    one loosely-leashed tool raise the trust of every other tool in the same run."""
    leash = Leash(
        entries=(
            LeashEntry(
                agent_id=AGENT,
                target="client.read_summary",
                scope=Scope.unrestricted(),
                rung=AutonomyTier.AUTONOMOUS,
            ),
            LeashEntry(
                agent_id=AGENT,
                target="ticket.read_status",
                scope=Scope.unrestricted(),
                rung=AutonomyTier.ASSISTED,
            ),
        )
    )
    assert _invoke(leash=leash).ceiling_rung is AutonomyTier.ASSISTED


def test_a_tool_with_no_leash_entry_drags_the_whole_run_to_shadow() -> None:
    """Fail-closed, and it composes. An unconfigured target is the target nobody has thought
    about, and a run containing one is a run that has not been thought about either."""
    only_one = _leash(AutonomyTier.AUTONOMOUS, "client.read_summary")
    assert _invoke(leash=only_one).ceiling_rung is AutonomyTier.SHADOW


def test_a_tool_the_caller_cannot_reach_does_not_affect_the_rung() -> None:
    """The order in `invoke` is what makes this true: project first, then compute the rung
    over the intersection. Computing it over the registry would let a tool this caller can
    never use decide how much this run is trusted."""
    leash = Leash(
        entries=(
            LeashEntry(
                agent_id=AGENT,
                target="client.read_summary",
                scope=Scope.unrestricted(),
                rung=AutonomyTier.AUTONOMOUS,
            ),
            LeashEntry(
                agent_id=AGENT,
                target="ticket.read_status",
                scope=Scope.unrestricted(),
                rung=AutonomyTier.SHADOW,
            ),
        )
    )
    inv = _invoke(leash=leash, ceiling=_ceiling("client.read_summary"))
    assert inv.ceiling_rung is AutonomyTier.AUTONOMOUS


def test_a_leash_scope_is_evaluated_against_the_row() -> None:
    """ "Trusted in maintenance, not in finance" has to survive being assembled here, or the
    scope stops applying the moment a run is built through this path rather than by hand."""
    leash = Leash(
        entries=(
            LeashEntry(
                agent_id=AGENT,
                target="client.read_summary",
                scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="maintenance"),)),
                rung=AutonomyTier.AUTONOMOUS,
            ),
        )
    )
    common = {"ceiling": _ceiling("client.read_summary"), "leash": leash}
    assert (
        _invoke(**common, row={"department": "maintenance"}).ceiling_rung is AutonomyTier.AUTONOMOUS
    )
    assert _invoke(**common, row={"department": "finance"}).ceiling_rung is AutonomyTier.SHADOW


# ------------------------------------------------------- risk only ever tightens
def test_elevated_risk_lowers_the_rung_and_says_so() -> None:
    """Recorded rather than silent. A run that quietly dropped a rung is a run whose
    refusals afterwards look arbitrary to whoever is reading the trace."""
    inv = _invoke(assessment=RiskAssessment(score=ELEVATED, matched=("ignore_prior",)))
    assert inv.ceiling_rung is AutonomyTier.ASSISTED
    assert inv.notes and "tightened" in inv.notes[0]


def test_high_risk_drops_the_run_to_shadow() -> None:
    inv = _invoke(assessment=RiskAssessment(score=HIGH, matched=("exfiltrate",)))
    assert inv.ceiling_rung is AutonomyTier.SHADOW


def test_risk_can_never_raise_a_rung() -> None:
    """The invariant the injection module is built around, asserted at the point where the
    two are composed. A clean assessment must not promote a run the leash held at shadow;
    injected text that could raise autonomy would be an escalation written in a ticket."""
    inv = _invoke(leash=_leash(AutonomyTier.SHADOW), assessment=CLEAN)
    assert inv.ceiling_rung is AutonomyTier.SHADOW
    assert inv.notes == ()


# ------------------------------------------------------------------- running
def test_the_body_receives_the_decided_invocation() -> None:
    """The seam exists so an audit entry, a timing record and a trace span attach in one
    place rather than in every caller - and so the thing being wrapped is already decided
    rather than a set of arguments still to be checked."""
    seen: list[str] = []

    def body(inv: Invocation) -> str:
        seen.append(inv.agent_id)
        return "done"

    result = run(_invoke(), body)
    assert seen == [AGENT]
    assert result == "done"
