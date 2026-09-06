"""The agent record: who sees it, what it reaches, and the wall between those two questions.

Every test here is a way one of the two axes could come to move the other, or a way a
listing could tell somebody about an agent they were not meant to know exists.

**Two directions, two tests, on purpose.** `test_visibility_does_not_change_what_an_agent_
reaches` and `test_a_wider_ceiling_does_not_change_who_can_see_an_agent` say the same thing
from opposite ends, and neither is redundant: code that derived the ceiling from the level
passes the second, and code that derived the level from the ceiling passes the first. Both
are written over a cross product rather than over one example, so a coupling that only
appears at the widest level is caught as well.

The reach in these tests is computed by `EntitlementSet.intersect`, which is the platform's
one implementation of `E_run(caller, agent) = E(caller) ∩ agent_ceiling`, and the catalogue
by the real `brain.gate.invoke.invoke`. Building a stand-in for either would leave these
tests asserting that this file's idea of the invariant is self-consistent.

Task ids: M13.1.1, M13.1.2, M13.1.3
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.agents.model import (
    AgentAudience,
    AgentAuthority,
    AgentRecord,
    AgentState,
    AgentViewer,
    audience_scope,
    entitlement_ceiling,
    runnable_agent_ids,
    tool_ceiling,
    visible_agent_ids,
    visible_to,
)
from brain.core.department import department_scope
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.scope import Clause, Op, Scope
from brain.gate.context import Channel
from brain.gate.injection import AutonomyTier, RiskAssessment
from brain.gate.invoke import InvocationRefusedError, invoke
from brain.gate.leash import Leash, LeashEntry
from brain.gate.select import AgentBinding, SelectionStage, select_agent
from brain.knowledge.visibility import Visibility
from brain.models.routing import TIER_LADDER, Tier
from brain.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

STEWARD = "u_wei_ling"
SOMEBODY_ELSE = "u_priya"

WEB = "web"
FINANCE = "finance"

#: Distinct from every scope slug and tool object in the tree, so the estate these tests
#: describe cannot trip `brain.ops.sweeps.sweep_slug_collisions`.
TRIAGE = "support_triage"
CHASER = "renewal_chaser"


class ClientRow(Entity):
    name: str = ""


def a_handler() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow]()


def _definition(name: str, capability: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="does a thing",
        entity=name.split(".")[0],
        required_capability=capability,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.DELEGATED,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_definition("client.read_summary", "read:client.name"), a_handler)
    registry.register(_definition("ticket.read_status", "read:ticket.status"), a_handler)
    return registry


def _caller(
    *capabilities: str, principal_id: str = STEWARD, scope: Scope | None = None
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=scope or Scope.unrestricted())
            for v in capabilities
        ),
    )


def _authority(
    *capabilities: str,
    scope: Scope | None = None,
    tools: frozenset[str] = frozenset({"client.read_summary"}),
    required: frozenset[str] = frozenset(),
    effect: SideEffect = SideEffect.NONE,
) -> AgentAuthority:
    return AgentAuthority(
        scope=scope if scope is not None else Scope.unrestricted(),
        capabilities=tuple(Capability(value=v) for v in capabilities),
        allowed_tools=tools,
        required_tools=required,
        max_side_effect=effect,
    )


def _personal(owner: str = STEWARD) -> AgentAudience:
    return AgentAudience(level=Visibility.PERSONAL, owner_id=owner)


def _of_department(department: str = WEB, owner: str = STEWARD) -> AgentAudience:
    return AgentAudience(level=Visibility.DEPARTMENT, owner_id=owner, department=department)


def _company(owner: str = STEWARD) -> AgentAudience:
    return AgentAudience(level=Visibility.COMPANY, owner_id=owner)


def _record(
    *,
    agent_id: str = TRIAGE,
    audience: AgentAudience | None = None,
    authority: AgentAuthority | None = None,
    **overrides: object,
) -> AgentRecord:
    fields: dict[str, object] = {
        "agent_id": agent_id,
        "display_name": "Support triage",
        "persona": "Answers support questions in the house voice.",
        "audience": audience or _personal(),
        "authority": authority or _authority("read:client.name"),
        "created_by": STEWARD,
    }
    fields.update(overrides)
    return AgentRecord.model_validate(fields)


def _viewer(principal_id: str, *departments: str) -> AgentViewer:
    return AgentViewer(principal_id=principal_id, departments=frozenset(departments))


#: The three audiences, so the cross-product tests iterate the real levels rather than a
#: list somebody would have to remember to extend.
AUDIENCES = (_personal(), _of_department(), _company())

#: Ceilings that differ in every way one can: nothing, one capability in one department, and
#: everything the caller holds with no row predicate at all. If audience were derived from
#: authority anywhere, these three would not produce one answer.
CEILINGS = (
    _authority(),
    _authority("read:client.name", scope=department_scope(WEB), tools=frozenset()),
    _authority(
        "read:client.name",
        "read:ticket.status",
        scope=Scope.unrestricted(),
        tools=frozenset({"client.read_summary", "ticket.read_status"}),
        effect=SideEffect.SEND,
    ),
)


# ------------------------------------------------------------------ the levels (M13.1.2)
def test_a_personal_agent_is_visible_to_its_steward_and_to_nobody_else() -> None:
    """The narrowest level has to actually discriminate. An audience predicate that matched
    everybody would pass every test about its shape while publishing every draft agent in the
    company, and a personal agent is where somebody's half-finished prompt lives."""
    audience = _personal()
    assert visible_to(audience, _viewer(STEWARD))
    assert not visible_to(audience, _viewer(SOMEBODY_ELSE))
    assert not visible_to(audience, _viewer(SOMEBODY_ELSE, WEB, FINANCE))


def test_a_department_agent_is_visible_to_everybody_in_that_department() -> None:
    """The level almost every agent actually wants. Without this an agent built for a team
    is either one person's or the whole company's, which is the failure
    `brain.knowledge.visibility` rejects `is_public` for."""
    audience = _of_department(WEB)
    assert visible_to(audience, _viewer(SOMEBODY_ELSE, WEB))
    assert visible_to(audience, _viewer(STEWARD, WEB))


def test_a_department_agent_is_invisible_outside_its_department() -> None:
    """The refusal half. A department audience that admitted everybody would make the middle
    level indistinguishable from the widest one, and nothing would fail: the agent would
    simply appear in every picker in the company."""
    assert not visible_to(_of_department(WEB), _viewer(SOMEBODY_ELSE, FINANCE))
    assert not visible_to(_of_department(WEB), _viewer(SOMEBODY_ELSE))


def test_somebody_in_two_departments_sees_the_agents_of_both() -> None:
    """Membership is a set, so the test is `any` and not `all`. Requiring every department to
    match would hide a Web agent from somebody who is also in Sales, which reads as the agent
    being broken rather than as a permission decision, and gets fixed by widening something."""
    both = _viewer(SOMEBODY_ELSE, WEB, FINANCE)
    assert visible_to(_of_department(WEB), both)
    assert visible_to(_of_department(FINANCE), both)


def test_a_company_agent_is_visible_to_somebody_with_no_department_at_all() -> None:
    """A contractor, a service principal, anybody the directory has not filed yet. If the
    audience test only ever looked at department rows, the widest level would be invisible to
    exactly the people who have no other agent available to them."""
    assert visible_to(_company(), _viewer(SOMEBODY_ELSE))
    assert visible_to(_personal(SOMEBODY_ELSE), _viewer(SOMEBODY_ELSE))


def test_the_three_levels_resolve_to_the_predicates_the_rest_of_the_system_uses() -> None:
    """One visibility vocabulary, not one per module. The department level is
    `brain.core.department.department_scope` and the company level is the unrestricted scope,
    which is what makes an agent audience comparable with a knowledge item's.

    Delete this and an agent-only reading of these three levels grows quietly, and the two
    stop agreeing about what department visibility means."""
    assert audience_scope(_company()).is_unrestricted()
    assert audience_scope(_of_department(WEB)) == department_scope(WEB)
    personal = audience_scope(_personal())
    assert personal.matches({"owner_id": STEWARD})
    assert not personal.matches({"owner_id": SOMEBODY_ELSE})


def test_a_department_audience_without_a_department_is_refused() -> None:
    """The blank-field case, and the reason `scope_for` is reused rather than reimplemented:
    a department predicate with no department is `Scope()`, which is unrestricted, so the
    middle level would silently become the widest one through an empty form field."""
    with pytest.raises(ValueError, match="needs a department"):
        AgentAudience(level=Visibility.DEPARTMENT, owner_id=STEWARD)


def test_a_department_recorded_on_a_personal_agent_is_refused() -> None:
    """The other direction, which is quieter. A department on a personal row is a field that
    reads as an audience and applies to nothing, and the first query to filter agents by
    department would return one nobody had published."""
    with pytest.raises(ValueError, match="carries no department"):
        AgentAudience(level=Visibility.PERSONAL, owner_id=STEWARD, department=WEB)


# --------------------------------------------------------- denied and absent are one answer
def test_an_agent_outside_the_audience_and_an_agent_that_was_never_made_are_one_answer() -> None:
    """The rule the whole platform keeps, at the agent listing.

    Two estates, identical except that one contains an agent this viewer may not see. The
    listings are equal, which is the only way a person cannot tell a refusal from an absence.
    A listing that returned "1 of 2", or a set with a sentinel in it, would fail here and
    nowhere else in this suite."""
    hidden = _record(agent_id=CHASER, audience=_personal(SOMEBODY_ELSE))
    mine = _record(agent_id=TRIAGE, audience=_personal(STEWARD))
    viewer = _viewer(STEWARD)
    assert visible_agent_ids([mine, hidden], viewer) == visible_agent_ids([mine], viewer)
    assert visible_agent_ids([mine, hidden], viewer) == frozenset({TRIAGE})


def test_a_listing_is_a_set_of_ids_with_nowhere_to_put_a_count() -> None:
    """A count of hidden agents is a count of teams, projects and clients the reader was not
    told about. The type is the enforcement: a frozenset of ids cannot carry a total, and a
    return type that could would be the first step to "showing 3 of 47".

    Delete this and the signature is free to become a tuple of the set and a number."""
    listing = visible_agent_ids([_record(audience=_company())], _viewer(SOMEBODY_ELSE))
    assert isinstance(listing, frozenset)
    assert all(isinstance(item, str) for item in listing)


# ------------------------------------------------- audience is not authority (M13.1.3, one way)
def test_visibility_does_not_change_what_an_agent_reaches() -> None:
    """Direction one, over the cross product of levels and ceilings.

    Publishing an agent to the whole company must not widen the ceiling it runs under. The
    obvious way to break this is a well-meant `if level is COMPANY: scope = unrestricted` in
    the ceiling producer, which reads as consistency and is a grant written by a visibility
    control.

    The reach is computed with `EntitlementSet.intersect`, the one implementation of the
    invariant, so this asserts the real narrowing rather than a copy of it. Compared on
    `ent_hash`, which is order-independent and covers the capabilities, their scopes and the
    time bound together.

    Delete this and a console that widens an agent's audience widens its reach, silently, and
    the audit entry records only that somebody changed the visibility."""
    caller = _caller("read:client.name", "read:ticket.status")
    for authority in CEILINGS:
        reaches = {
            audience_scope(a).model_dump_json(): caller.intersect(
                entitlement_ceiling(_record(audience=a, authority=authority))
            ).ent_hash()
            for a in AUDIENCES
        }
        assert len(set(reaches.values())) == 1, reaches
        ceilings = {tool_ceiling(_record(audience=a, authority=authority)) for a in AUDIENCES}
        assert len(ceilings) == 1


def test_a_globally_visible_agent_reaches_no_more_than_a_personal_one() -> None:
    """The readable instance of the rule above, written out because the cross-product test
    passes for a reason a reader has to reconstruct.

    One ceiling, narrowed to the Web department, on two agents that differ only in who can
    see them. The company-visible one reaches Web, exactly as the private one does."""
    authority = _authority("read:client.name", scope=department_scope(WEB))
    caller = _caller("read:client.name")
    published = caller.intersect(
        entitlement_ceiling(_record(audience=_company(), authority=authority))
    )
    private = caller.intersect(
        entitlement_ceiling(_record(audience=_personal(), authority=authority))
    )
    assert published.ent_hash() == private.ent_hash()
    assert published.scope_for(Capability(value="read:client.name")) == department_scope(WEB)


# ------------------------------------------------- audience is not authority (M13.1.3, the other)
def test_a_wider_ceiling_does_not_change_who_can_see_an_agent() -> None:
    """Direction two, over the same cross product, and the one a single test would miss.

    An agent given a wide ceiling so that it can do its job must not thereby appear in
    everybody's picker. The natural way to break it is a listing that lets an unrestricted
    ceiling stand in for a company audience, which is attractive because such an agent
    usually is for everybody.

    Delete this and the two rules can be tied together in the direction the other test does
    not look at, which is precisely how a coupling survives review."""
    viewers = (_viewer(STEWARD), _viewer(SOMEBODY_ELSE), _viewer(SOMEBODY_ELSE, WEB))
    for audience in AUDIENCES:
        for viewer in viewers:
            seen = {
                visible_agent_ids([_record(audience=audience, authority=c)], viewer)
                for c in CEILINGS
            }
            assert len(seen) == 1, (audience.level, viewer.principal_id, seen)


def test_an_agent_whose_ceiling_admits_everything_is_visible_only_to_its_steward() -> None:
    """The readable instance of the rule above. The widest ceiling in this file, on a personal
    agent: it reaches whatever its caller reaches and is seen by one person.

    This is the shape that gets built in practice, an administrator's own agent with nothing
    taken off it, and the failure it guards is that agent turning up in 126 pickers."""
    everything = _authority(
        "read:client.name",
        "read:ticket.status",
        scope=Scope.unrestricted(),
        tools=frozenset({"client.read_summary", "ticket.read_status"}),
    )
    record = _record(audience=_personal(STEWARD), authority=everything)
    assert visible_agent_ids([record], _viewer(STEWARD)) == frozenset({TRIAGE})
    assert visible_agent_ids([record], _viewer(SOMEBODY_ELSE, WEB)) == frozenset()


# ------------------------------------------------------------------- the ceiling (M13.1.1)
def test_an_agent_can_only_narrow_the_caller_and_never_widen_them() -> None:
    """The platform invariant, asserted through this record rather than restated by it.

    The ceiling names a capability the caller does not hold. The intersection does not
    acquire it, because an agent is a lens: if a ceiling could add, installing an agent would
    be a way to grant yourself something."""
    caller = _caller("read:client.name")
    generous = _authority("read:client.name", "read:ticket.status")
    reach = caller.intersect(entitlement_ceiling(_record(authority=generous)))
    assert reach.holds(Capability(value="read:client.name"))
    assert not reach.holds(Capability(value="read:ticket.status"))


def test_a_run_through_an_agent_is_still_the_caller_s_run() -> None:
    """An agent is a lens and never a principal. The intersection keeps the caller's id, so
    nothing an agent does is attributed to the agent, and the ceiling's own id is prefixed so
    that a set which is a ceiling cannot be read in a trace as a person.

    Asserted against the agent id rather than against the prefix constant: comparing the
    rendered id with the constant it was built from would pass for any prefix at all,
    including an empty one."""
    caller = _caller("read:client.name")
    record = _record()
    ceiling = entitlement_ceiling(record)
    assert caller.intersect(ceiling).principal_id == STEWARD
    assert ceiling.principal_id != record.agent_id
    assert ceiling.principal_id.endswith(record.agent_id)
    assert len(ceiling.principal_id) > len(record.agent_id)


def test_an_agent_that_declares_no_capabilities_reaches_nothing() -> None:
    """The fail-closed direction of an empty ceiling, and the reason the capability list has
    a server default while the scope column does not.

    A new agent does nothing until somebody says what it may reach. `invoke` refuses the run
    outright rather than starting it with an empty catalogue, because a model handed no tools
    answers from context and training and the answer reads like a researched one."""
    record = _record(authority=_authority())
    caller = _caller("read:client.name")
    reach = caller.intersect(entitlement_ceiling(record))
    assert reach.grants == ()
    with pytest.raises(InvocationRefusedError):
        invoke(
            principal_id=STEWARD,
            agent_id=record.agent_id,
            registry=_registry(),
            entitlement=reach,
            ceiling=tool_ceiling(record),
            leash=_leash(record.agent_id),
            assessment=RiskAssessment(score=0, matched=()),
            now=NOW,
        )


def _leash(agent_id: str) -> Leash:
    return Leash(
        entries=tuple(
            LeashEntry(
                agent_id=agent_id,
                target=target,
                scope=Scope.unrestricted(),
                rung=AutonomyTier.AUTONOMOUS,
            )
            for target in ("client.read_summary", "ticket.read_status")
        )
    )


def test_the_catalogue_an_agent_offers_is_the_intersection_and_not_its_tool_list() -> None:
    """The ceiling this record produces is handed to the real `invoke`, so the tool list is
    projected rather than described.

    The agent is allowed both tools and the caller reaches one of them. What comes back is the
    one, and the other is absent rather than listed and refused: a tool described to a model
    and then refused teaches the model the capability exists, and the model says so."""
    record = _record(
        authority=_authority(
            "read:client.name",
            "read:ticket.status",
            tools=frozenset({"client.read_summary", "ticket.read_status"}),
        )
    )
    invocation = invoke(
        principal_id=STEWARD,
        agent_id=record.agent_id,
        registry=_registry(),
        entitlement=_caller("read:client.name").intersect(entitlement_ceiling(record)),
        ceiling=tool_ceiling(record),
        leash=_leash(record.agent_id),
        assessment=RiskAssessment(score=0, matched=()),
        now=NOW,
    )
    assert invocation.reachable == ("client.read_summary",)


def test_the_tool_ceiling_carries_the_agent_s_declared_tools_and_effect() -> None:
    """The producer's positive case. A ceiling that dropped the side effect would let a
    reporting agent send, and one that dropped the required set would let
    `brain.gate.catalogue.project` hand a model a catalogue missing the tool the agent cannot
    work without."""
    authority = _authority(
        "read:client.name",
        tools=frozenset({"client.read_summary", "ticket.read_status"}),
        required=frozenset({"client.read_summary"}),
        effect=SideEffect.DRAFT,
    )
    ceiling = tool_ceiling(_record(authority=authority))
    assert ceiling.agent_id == TRIAGE
    assert ceiling.allowed_tools == frozenset({"client.read_summary", "ticket.read_status"})
    assert ceiling.required_tools == frozenset({"client.read_summary"})
    assert ceiling.max_side_effect is SideEffect.DRAFT


def test_an_agent_may_not_require_a_tool_it_is_not_allowed() -> None:
    """Refused when the record is built, by building the `AgentCeiling` that owns the rule
    rather than by restating it. Such an agent can never resolve its catalogue, so without
    this the misconfiguration is discovered by every request instead of by the person who
    saved it."""
    with pytest.raises(ValueError, match="requires tools it is not allowed"):
        _record(
            authority=_authority(
                "read:client.name",
                tools=frozenset({"client.read_summary"}),
                required=frozenset({"ticket.read_status"}),
            )
        )


# ------------------------------------------------------------------------ tier and defaults
def test_an_agent_cannot_be_pinned_to_the_fast_lane_s_tier() -> None:
    """`Tier.NONE` is the absence of the routing ladder rather than a rung on it:
    `RoutingChain.select` returns an empty selection for it. An agent pinned there is one
    that is chosen, starts, and produces no answer and no error."""
    with pytest.raises(ValueError, match="not a rung on the routing ladder"):
        _record(tier=Tier.NONE)


def test_an_agent_that_names_no_tier_lands_on_one_the_router_can_route() -> None:
    """The sibling of the refusal above, and the guard on the default.

    Asserted against `TIER_LADDER` rather than against the default's own value, which would
    compare the constant with itself and stay green for any tier it could hold, `NONE`
    included."""
    assert _record().tier in TIER_LADDER


# ------------------------------------------------- what selection is actually handed (M13.1.4)
def test_a_disabled_agent_is_not_offered_to_selection_but_is_still_listed_to_its_owner() -> None:
    """Two axes, and the conjunction belongs to selection alone.

    A disabled agent stays visible, so its owner can see it in a console and enable it again;
    it is not runnable, so nothing can choose it. Folding the two would mean disabling an
    agent made it vanish from the place where you turn it back on."""
    record = _record(disabled_at=NOW)
    viewer = _viewer(STEWARD)
    assert record.state is AgentState.DISABLED
    assert visible_agent_ids([record], viewer) == frozenset({TRIAGE})
    assert runnable_agent_ids([record], viewer) == frozenset()


def test_an_archived_agent_is_not_offered_to_selection() -> None:
    """The same for the terminal state. An archived agent that could still be selected is an
    archived agent that answers, and the person who retired it has no way to find out."""
    record = _record(archived_at=NOW)
    assert record.state is AgentState.ARCHIVED
    assert runnable_agent_ids([record], _viewer(STEWARD)) == frozenset()


def test_an_enabled_visible_agent_is_offered_to_selection() -> None:
    """The positive case every refusal above needs. A runnable set tested only by what it
    excludes is satisfied by a function returning nothing, and an empty set makes
    `select_agent` fall through to the default for every question anybody asks."""
    assert runnable_agent_ids([_record()], _viewer(STEWARD)) == frozenset({TRIAGE})


def test_selection_falls_through_a_binding_naming_an_agent_the_caller_cannot_run() -> None:
    """The seam this module exists to feed, driven through the real `select_agent`.

    A conversation is bound to an agent the caller cannot see. Selection skips it in silence
    and lands on the default, because saying "that agent is not available to you" would
    confirm it exists. This is what `runnable_agent_ids` is for: the set is the only thing
    standing between a binding somebody wrote and an agent somebody else's question reaches.

    Delete this and the two halves can drift apart without anything noticing, which is the
    state the repository was in before this record existed: `select_agent` has taken a
    `visible_agents` argument since it was written and nothing in `src` produced one."""
    private = _record(agent_id=CHASER, audience=_personal(SOMEBODY_ELSE))
    mine = _record(agent_id=TRIAGE, audience=_personal(STEWARD))
    viewer = _viewer(STEWARD)
    selection = select_agent(
        "chase the renewal",
        Channel.CONSOLE,
        visible_agents=runnable_agent_ids([mine, private], viewer),
        default_agent=TRIAGE,
        bindings=(AgentBinding(channel=Channel.CONSOLE, agent_id=CHASER),),
    )
    assert selection.agent_id == TRIAGE
    assert selection.stage is SelectionStage.DEFAULT
    assert CHASER not in selection.reason


def test_a_binding_to_an_agent_the_caller_can_run_is_honoured() -> None:
    """The sibling. A visibility filter tested only by what it hides is satisfied by one that
    hides everything, and every binding in the company would quietly stop working."""
    mine = _record(agent_id=CHASER, audience=_company())
    selection = select_agent(
        "chase the renewal",
        Channel.CONSOLE,
        visible_agents=runnable_agent_ids([mine], _viewer(SOMEBODY_ELSE)),
        default_agent=TRIAGE,
        bindings=(AgentBinding(channel=Channel.CONSOLE, agent_id=CHASER),),
    )
    assert selection.agent_id == CHASER
    assert selection.stage is SelectionStage.BINDING


# --------------------------------------------------------------------------- the record itself
def test_an_agent_record_cannot_be_changed_in_place() -> None:
    """Frozen, so both sides of a change are holdable and a decision made from a record
    cannot be invalidated half way through by whoever else holds it."""
    record = _record()
    with pytest.raises(ValueError, match="frozen"):
        record.persona = "something else"


def test_a_naive_lifecycle_timestamp_is_refused() -> None:
    """A naive timestamp compared against a UTC `now` is hours out in whichever direction the
    host sits, and neither direction announces itself. `Principal.not_after` refuses one for
    the same reason."""
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(disabled_at=datetime(2026, 9, 6, 9, 0))


def test_the_scope_on_the_ceiling_narrows_the_rows_a_run_reads() -> None:
    """The row axis of the ceiling, which is separate from the capability axis and easy to
    lose: a ceiling that kept the capability and dropped the scope would let a departmental
    agent read the whole company's rows while still looking correctly restricted."""
    narrow = _authority("read:client.name", scope=department_scope(WEB))
    caller = _caller("read:client.name")
    reach = caller.intersect(entitlement_ceiling(_record(authority=narrow)))
    scope = reach.scope_for(Capability(value="read:client.name"))
    assert scope is not None
    assert scope.matches({"department": WEB})
    assert not scope.matches({"department": FINANCE})


def test_two_ceilings_narrow_rather_than_replace() -> None:
    """A caller already restricted to one department, through an agent restricted to another,
    reaches nothing. Intersection is conjunction: the conservative answer is the empty one,
    and a ceiling that replaced the caller's scope would hand them another department's rows.
    """
    caller = _caller("read:client.name", scope=department_scope(FINANCE))
    record = _record(authority=_authority("read:client.name", scope=department_scope(WEB)))
    scope = caller.intersect(entitlement_ceiling(record)).scope_for(
        Capability(value="read:client.name")
    )
    assert scope is not None
    assert scope.clauses == (
        Clause(field="department", op=Op.EQ, value=FINANCE),
        Clause(field="department", op=Op.EQ, value=WEB),
    )
    assert not scope.matches({"department": WEB})
    assert not scope.matches({"department": FINANCE})
