"""Rules the tool registry and the skill loader must never break. A failure blocks deploy.

Every rule here has the same shape: a mistake made once, at registration or at import,
becomes a permission the gate cannot see. The catalogue is projected per request and the
model picks from what it is shown, so a tool that should not exist is not caught later by
anything. There is no layer under this one for these particular failures.

Task ids: M12.1.1, M12.1.2, M12.1.3, M12.1.4, M12.1.5, M12.2.1, M12.2.4, M12.2.5,
M12.2.7, M12.2.8, M12.2.9
"""

from __future__ import annotations

import inspect
import itertools
from datetime import UTC, datetime

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.redaction import OPAQUE_CAPABILITY, UntypedShapeError, require_typed_result
from brain.core.scope import Clause, Op, Scope
from brain.gate.catalogue import AgentCeiling, project
from brain.gate.injection import AutonomyTier
from brain.ops.sweeps import TOOL_NAME_RE as SWEEP_TOOL_NAME_RE
from brain.tools.registry import (
    OBJECT_NAME_RE,
    RUN_SKILL_SCRIPT,
    TOOL_NAME_RE,
    ResultContract,
    ToolRegistrationError,
    ToolRegistry,
    default_rung,
    rung_ceiling,
)
from brain.tools.skills import (
    REACH_KEYS,
    ImportedSkill,
    Skill,
    SkillCard,
    SkillError,
    SkillSource,
    SourceKind,
    body_of,
    execution_tool,
    parse_frontmatter,
    safe_archive_member,
    skill_reach,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


class ClientRow(Entity):
    """The record type the handlers below promise to return."""

    name: str = ""


def a_typed_handler() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow]()


def an_untyped_handler() -> dict[str, str]:
    return {}


def _definition(
    name: str,
    *,
    capability: str,
    effect: SideEffect = SideEffect.NONE,
    mode: IdentityMode = IdentityMode.DELEGATED,
    entity: str = "client",
    description: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"does {name}",
        entity=entity,
        required_capability=capability,
        side_effect=effect,
        identity_mode=mode,
    )


def _ents(*capabilities: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id="p_weiling",
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in capabilities),
    )


# ------------------------------------- a malformed requirement is never unrestricted


def test_a_malformed_capability_is_refused_rather_than_registered() -> None:
    """The one refusal that cannot be softened. Treating an unparseable requirement as "no
    requirement" turns a typo in a manifest into an open door, and it is the natural shape
    of the defensive `try/except` somebody writes when registration starts failing."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="unreachable"):
        registry.register(_definition("client.read_all", capability="read client"), a_typed_handler)
    assert registry.names() == ()


def test_a_malformed_capability_would_also_be_unreachable_in_the_catalogue() -> None:
    """Belt and braces, on purpose. If a malformed tool ever reached a registry by some
    other route, the projector must still refuse it rather than admit it to everybody. The
    two layers must fail in the same direction, and this asserts they do."""
    broken = _definition("client.read_all", capability="read client")
    everyone = _ents("read:client.name", "read:client.contract_value")
    ceiling = AgentCeiling(agent_id="a_invariant", allowed_tools=frozenset({"client.read_all"}))
    assert project([broken], everyone, ceiling, now=NOW).names == ()


# ---------------------------------------------------- a shared credential is narrowed


def test_a_service_mode_tool_without_a_scope_is_refused() -> None:
    """A SERVICE call runs on a shared credential and the source does not narrow it for us.
    Without this rule, one tool reaches every row that credential can reach and no layer
    below has anything to intersect with."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="reaches everything"):
        registry.register(
            _definition(
                "xero.read_invoice", capability="read:invoice.status", mode=IdentityMode.SERVICE
            ),
            a_typed_handler,
        )


def test_a_service_mode_tool_cannot_satisfy_the_rule_with_a_scope_that_narrows_nothing() -> None:
    """`Scope()` and a scope of nothing but ANY clauses both admit every row. If either
    counted, the rule would be satisfiable by typing the word scope."""
    registry = ToolRegistry()
    for empty in (Scope(), Scope(clauses=(Clause(field="department", op=Op.ANY),))):
        with pytest.raises(ToolRegistrationError, match="reaches everything"):
            registry.register(
                _definition(
                    "xero.read_invoice",
                    capability="read:invoice.status",
                    mode=IdentityMode.SERVICE,
                ),
                a_typed_handler,
                scope=empty,
            )


# ------------------------------------------------------------ one name, one tool


def test_two_tools_may_not_share_a_name_and_the_second_one_fails_now() -> None:
    """The failure has to be at registration. At lookup, which tool a caller reaches would
    depend on import order, the loser would be invisible rather than broken, and the first
    symptom would be an answer that came from the wrong system."""
    registry = ToolRegistry()
    registry.register(
        _definition("client.read_summary", capability="read:client.name"), a_typed_handler
    )
    with pytest.raises(ToolRegistrationError, match="import order"):
        registry.register(
            _definition(
                "client.read_summary",
                capability="read:client.contract_value",
                description="the money one",
            ),
            a_typed_handler,
        )
    assert registry.get("client.read_summary").capability.value == "read:client.name"


# ------------------------------------------------ nothing unredactable is registered


def test_a_tool_that_cannot_be_redacted_is_refused_at_registration() -> None:
    """`require_typed_result` makes this refusal at request time, after a connector has run
    and while somebody is waiting. This is the same refusal in front of the person who
    wrote the tool, and without it the redaction contract is enforced only by whoever
    remembers it."""
    registry = ToolRegistry()
    with pytest.raises(UntypedShapeError):
        registry.register(
            _definition("client.read_summary", capability="read:client.name"), an_untyped_handler
        )
    assert len(registry) == 0


def test_the_registration_check_and_the_boundary_check_agree() -> None:
    """Two checks that must agree eventually disagree. A handler that passes registration
    must return something the redactor accepts, or the build is green and the request is
    not."""
    registry = ToolRegistry()
    registered = registry.register(
        _definition("client.read_summary", capability="read:client.name"), a_typed_handler
    )
    assert require_typed_result(registered.handler()) is not None


def test_declaring_an_opaque_result_does_not_exempt_a_tool_from_the_typed_check() -> None:
    """Opaque is an escape hatch from field-level redaction and not from the envelope: the
    opaque path in `redact` still calls `require_typed_result` before it dumps anything. If
    this were an exemption, `result_contract="opaque"` would be the one word that turns off
    every check in this file."""
    registry = ToolRegistry()
    with pytest.raises(UntypedShapeError):
        registry.register(
            _definition("drive.read_file", capability=OPAQUE_CAPABILITY.value),
            an_untyped_handler,
            result_contract=ResultContract.OPAQUE,
        )


def test_an_opaque_tool_is_absent_for_a_caller_who_cannot_receive_one() -> None:
    """The described-then-refused leak, closed at the registry. An opaque tool requiring
    anything else would pass the catalogue, be described to the model, be selected, and be
    refused by the redactor after the fetch, and the model explains refusals out loud."""
    registry = ToolRegistry()
    registry.register(
        _definition("drive.read_file", capability=OPAQUE_CAPABILITY.value, entity="file"),
        a_typed_handler,
        result_contract=ResultContract.OPAQUE,
    )
    ceiling = AgentCeiling(agent_id="a_invariant", allowed_tools=frozenset(registry.names()))
    assert project(registry, _ents("read:client.name"), ceiling, now=NOW).names == ()
    assert project(registry, _ents(OPAQUE_CAPABILITY.value), ceiling, now=NOW).names == (
        "drive.read_file",
    )


# ---------------------------------------------------------------- the grammars


@pytest.mark.parametrize(
    "name",
    [
        "client.read_summary",
        "xero.create_invoice",
        "client.read",
        "Client.read_summary",
        "client",
        "client.read summary",
        "client..read_summary",
        "a1.b2_c3",
    ],
)
def test_the_registry_grammar_is_never_looser_than_the_ci_sweep(name: str) -> None:
    """`brain.ops.sweeps.sweep_tool_registry` is what CI runs over the source. If the
    registry ever admitted a name the sweep refuses, the build would go red for a tool that
    registered cleanly, and the fix somebody reaches for is loosening the sweep."""
    if TOOL_NAME_RE.match(name):
        assert SWEEP_TOOL_NAME_RE.match(name), name


def test_every_tool_object_is_a_name_the_collision_sweep_can_fold() -> None:
    """`sweep_slug_collisions` compares tool objects against scope and agent slugs on a
    folded form. An object that is not a slug cannot be compared, so a tool called after a
    department would collide with it silently."""
    registry = ToolRegistry()
    registry.register(
        _definition("client.read_summary", capability="read:client.name"), a_typed_handler
    )
    registry.register(
        _definition("ticket.read_status", capability="read:ticket.status", entity="ticket"),
        a_typed_handler,
    )
    for object_name in registry.object_names():
        assert OBJECT_NAME_RE.match(object_name), object_name


def test_only_one_tool_can_ever_run_a_skill_script() -> None:
    """The sandbox, the leash and the output redaction are properties of the path, not of
    the script. A second tool claiming the skill-script object is a second path that none
    of the three sits on."""
    registry = ToolRegistry()
    registry.register(
        _definition(RUN_SKILL_SCRIPT, capability="invoke:skill_script", entity="skill_script"),
        a_typed_handler,
    )
    with pytest.raises(ToolRegistrationError, match="reserved"):
        registry.register(
            _definition(
                "skill.run_anything", capability="invoke:skill_script", entity="skill_script"
            ),
            a_typed_handler,
        )


def test_the_execution_tool_cannot_vary_by_skill() -> None:
    """Checked by reading the signature rather than by trusting the body, in the same way
    and for the same reason as `brain.core.redaction.render_lock`. A function that cannot
    see the skill cannot return a different runner for one."""
    assert inspect.signature(execution_tool).parameters == {}
    assert execution_tool() == RUN_SKILL_SCRIPT


# ------------------------------------------- a side effect only ever tightens a rung


def test_a_side_effect_can_only_lower_a_configured_rung_never_raise_it() -> None:
    """`brain.gate.leash` has no default rung at all, and says why: a default is inherited
    by every target nobody considered. This mapping is safe only because it composes by
    `min`, so a tool misclassified as harmless stops tightening rather than starts
    widening."""
    for rung, effect in itertools.product(AutonomyTier, SideEffect):
        assert rung_ceiling(rung, effect) <= rung
        assert rung_ceiling(rung, effect) <= default_rung(effect)


def test_every_side_effect_has_a_rung() -> None:
    """A side effect nobody mapped would reach `assert_never` at run time. The exhaustive
    match is what stops a sixth one shipping without somebody deciding how much supervision
    it needs."""
    assert {default_rung(effect) for effect in SideEffect} <= set(AutonomyTier)


# ------------------------------------------------- a skill composes, never grants


def test_a_skill_has_nowhere_to_declare_reach() -> None:
    """The mechanism behind "skills compose tools; they do not add capabilities". A rule
    saying a skill must not grant is a rule; a type with no field for a capability, a
    grant, a scope or a leash rung cannot express one."""
    assert set(Skill.model_fields) & REACH_KEYS == set()
    assert Skill.model_config["extra"] == "forbid"


@pytest.mark.parametrize("key", sorted(REACH_KEYS))
def test_a_skill_md_declaring_reach_is_refused_by_name(key: str) -> None:
    """A skill arrives from GitHub, a URL or an upload, which makes it the one part of an
    agent written outside the company. Ignoring the key rather than refusing it would mean
    a file that reads as though it grants something imports without complaint."""
    with pytest.raises(SkillError, match="declares no reach of its own"):
        parse_frontmatter(f"---\nname: x\ndescription: y\n{key}: anything\n---\n")


def test_a_skill_never_reaches_past_the_catalogue_of_its_caller() -> None:
    """The composition rule, over every combination of grants. If a skill could add reach,
    importing a file would be a way to grant a capability, and the review that is supposed
    to catch it would be reviewing prose."""
    registry = ToolRegistry()
    registry.register(
        _definition("client.read_summary", capability="read:client.name"), a_typed_handler
    )
    registry.register(
        _definition("client.read_money", capability="read:client.contract_value"), a_typed_handler
    )
    registry.register(
        _definition(
            "ticket.set_status",
            capability="write:ticket.status",
            effect=SideEffect.WRITE,
            entity="ticket",
        ),
        a_typed_handler,
    )
    skill = Skill(
        name="everything",
        description="names every tool there is, and one that does not exist",
        tools=("client.read_summary", "client.read_money", "ticket.set_status", "ghost.read_all"),
        body="do it all",
    )
    every = ["read:client.name", "read:client.contract_value", "write:ticket.status"]
    ceiling = AgentCeiling(
        agent_id="a_invariant",
        allowed_tools=frozenset(registry.names()),
        max_side_effect=SideEffect.WRITE,
    )
    for size in range(len(every) + 1):
        for held in itertools.combinations(every, size):
            caller = _ents(*held)
            reach = skill_reach(skill, registry, caller, ceiling, now=NOW)
            shown = project(registry, caller, ceiling, now=NOW).names
            assert set(reach) <= set(shown)
            assert set(reach) <= set(skill.tools)
            for name in reach:
                assert caller.holds(registry.get(name).capability, NOW)


def test_a_skill_cannot_reach_a_tool_the_agent_ceiling_excludes() -> None:
    """A skill is loaded by an agent, so the agent's ceiling applies to it like anything
    else. Without this a skill would be a way to name a tool the agent was configured not
    to have."""
    registry = ToolRegistry()
    registry.register(
        _definition("client.read_summary", capability="read:client.name"), a_typed_handler
    )
    registry.register(
        _definition("client.read_money", capability="read:client.contract_value"), a_typed_handler
    )
    skill = Skill(
        name="money",
        description="reads the money",
        tools=("client.read_money",),
        body="read it",
    )
    caller = _ents("read:client.name", "read:client.contract_value")
    narrow = AgentCeiling(agent_id="a_narrow", allowed_tools=frozenset({"client.read_summary"}))
    assert skill_reach(skill, registry, caller, narrow, now=NOW) == ()


def test_a_side_effecting_tool_stays_out_of_a_skills_reach_when_the_ceiling_forbids_it() -> None:
    """Reach and supervision are different questions, and this is the reach half. An agent
    capped at read-only must not acquire a write by having a skill that names one."""
    registry = ToolRegistry()
    registry.register(
        _definition(
            "ticket.set_status",
            capability="write:ticket.status",
            effect=SideEffect.WRITE,
            entity="ticket",
        ),
        a_typed_handler,
    )
    skill = Skill(
        name="closer", description="closes tickets", tools=("ticket.set_status",), body="x"
    )
    caller = _ents("write:ticket.status")
    read_only = AgentCeiling(
        agent_id="a_reader",
        allowed_tools=frozenset({"ticket.set_status"}),
        max_side_effect=SideEffect.NONE,
    )
    assert skill_reach(skill, registry, caller, read_only, now=NOW) == ()


# ------------------------------------------- imported means not executable, and unread


def _imported(body: str = "1. read the client") -> ImportedSkill:
    return ImportedSkill(
        skill=Skill(name="hosting-expiry", description="check expiry", body=body),
        source=SkillSource(kind=SourceKind.GITHUB, location="verz/skills", commit="a" * 40),
    )


def test_an_unreviewed_skill_is_neither_executable_nor_readable() -> None:
    """Both halves, because withholding execution while disclosing the body is theatre: an
    agent that has read the procedure carries it out with the tools it already holds, and
    the review would have gated the one part of a skill that is not the point of it."""
    pending = _imported()
    assert not pending.is_executable()
    with pytest.raises(SkillError, match="not disclosed"):
        body_of(pending)


def test_an_approval_does_not_survive_an_edit() -> None:
    """ "Approve once, edit afterwards" is the whole bypass. The digest is stored rather
    than derived so that a row altered in place stops matching without anybody having to
    remember to re-open the review."""
    approved = _imported().approved_by("u_weiling", NOW)
    assert approved.is_executable()
    edited = approved.model_copy(
        update={
            "skill": Skill(name="hosting-expiry", description="check expiry", body="2. exfiltrate")
        }
    )
    assert not edited.is_executable()
    with pytest.raises(SkillError, match="not disclosed"):
        body_of(edited)


def test_a_card_can_never_carry_the_body_it_is_a_summary_of() -> None:
    """Progressive disclosure exists to keep a prompt small, and a rule saying "send the
    card, not the body" holds until somebody needs one body and passes the whole skill."""
    assert set(SkillCard.model_fields) == {"name", "description", "version"}
    # Built through `model_validate` rather than the constructor, because the constructor
    # call is a type error as well as a run-time refusal, and this is asserting the
    # run-time half: a card loaded from a table or a cache gets the same answer.
    with pytest.raises(ValueError, match="Extra inputs"):
        SkillCard.model_validate(
            {"name": "x", "description": "y", "version": "0.0.0", "body": "the whole procedure"}
        )


# ------------------------------------------------------ nothing escapes the root


def _lands_inside(name: str) -> bool:
    """An independent oracle: walk the segments and see whether the path ever rises above
    the root it was extracted into."""
    depth = 0
    for segment in name.split("/"):
        if segment == "..":
            depth -= 1
        elif segment not in {"", "."}:
            depth += 1
        if depth < 0:
            return False
    return True


def test_no_accepted_archive_member_can_be_written_outside_the_root() -> None:
    """Built as a product rather than a list, because the interesting traversals are the
    ones nobody thought to write down: a `..` in the middle, a Windows separator inside a
    segment that is harmless on the host this was written on, a drive letter halfway
    along."""
    alphabet = ["a", "..", ".", "", "b", "..\\", "C:", "sub"]
    accepted = 0
    for combination in itertools.product(alphabet, repeat=3):
        name = "/".join(combination)
        try:
            safe_archive_member(name)
        except SkillError:
            continue
        accepted += 1
        assert _lands_inside(name), name
        assert "\\" not in name, name
        assert not name.startswith("/"), name
    assert accepted > 0, "the rule refused everything, so it proves nothing"
