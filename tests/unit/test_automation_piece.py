"""The custom piece: every way a canvas step could see more than the person who wired it.

The fixtures here are adversarial on purpose. Tools are registered in an order that is not
their sort order, the registry always holds more tools than any flow may reach, and the flow
ceiling is narrower than the caller in one direction and wider in the other, so an
implementation that dropped either half of the intersection would still look right in one
test and wrong in the next.

Task ids: M32.6.1.3
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload, UntypedShapeError
from brain.core.scope import Scope
from brain.gate.injection import AutonomyTier, RiskAssessment
from brain.gate.invoke import Invocation
from brain.gate.leash import Leash, LeashEntry
from brain.ops.automation import StepKind, assert_deterministic
from brain.ops.automation_piece import (
    PIECE_STEP_KIND,
    RESERVED_ARGUMENT_NAMES,
    TOOL_NOT_AVAILABLE,
    PieceRefusedError,
    PieceStep,
    offered_tools,
    piece_ceiling,
    plan_piece_call,
    resolve_step,
    run_step,
)
from brain.tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "src" / "brain" / "ops" / "automation_piece.py"
COMPOSE = REPO / "docker-compose.automation.yml"

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
FLOW = "flow_nightly_reminder"
CLEAN = RiskAssessment(score=0, matched=())

#: Every tool the fixture registry holds, and every one of them is a tool some flow in this
#: file may not reach. A registry that only ever held reachable tools would let a projection
#: that returned the whole registry pass every test in the DENIED-and-ABSENT section.
ALL_TOOL_NAMES = (
    "archive.read_note",
    "client.read_summary",
    "invoice.send_reminder",
    "ticket.read_status",
)


class ClientRow(Entity):
    name: str = "SNM Holdings"
    margin: str = "34%"


def a_handler() -> TypedResult[ClientRow]:
    """A registrable handler. `assert_tool_returns_typed_result` reads the annotation."""
    return TypedResult[ClientRow]()


POLICY = FieldPolicy(
    rules=(
        FieldRule.of("client", "name", "read:client.name", Classification.PUBLIC),
        FieldRule.of("client", "margin", "read:client.margin", Classification.RESTRICTED),
    )
)


def _definition(
    name: str,
    capability: str,
    effect: SideEffect = SideEffect.NONE,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"a tool called {name}",
        entity=name.split(".")[0],
        required_capability=capability,
        side_effect=effect,
        identity_mode=IdentityMode.DELEGATED,
    )


def _registry(*, frozen: bool = True, only: tuple[str, ...] = ALL_TOOL_NAMES) -> ToolRegistry:
    """The fixture registry, registered in an order that is not its sort order.

    `invoice` first and `archive` last, so a projection that returned registration order
    rather than the catalogue's sorted order is visible, and so is one that returned the
    registry rather than the projection.
    """
    declarations = {
        "invoice.send_reminder": _definition(
            "invoice.send_reminder", "write:invoice.status", SideEffect.SEND
        ),
        "ticket.read_status": _definition("ticket.read_status", "read:ticket.status"),
        "client.read_summary": _definition("client.read_summary", "read:client.name"),
        "archive.read_note": _definition("archive.read_note", "read:archive.note"),
    }
    registry = ToolRegistry()
    for name, definition in declarations.items():
        if name in only:
            registry.register(definition, a_handler)
    return registry.freeze() if frozen else registry


def _entitlement(*capabilities: str, principal_id: str = "u_weiling") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _leash(rung: AutonomyTier = AutonomyTier.AUTONOMOUS) -> Leash:
    """Every fixture tool leashed for this flow, so an unleashed one does not mask a
    property being tested by dragging the whole run to shadow."""
    return Leash(
        entries=tuple(
            LeashEntry(agent_id=FLOW, target=name, scope=Scope.unrestricted(), rung=rung)
            for name in ALL_TOOL_NAMES
        )
    )


def _plan(**overrides: Any) -> Invocation:
    kwargs: dict[str, Any] = {
        "flow_id": FLOW,
        "caller": _entitlement("read:client.name", "read:ticket.status"),
        "flow_ceiling": _entitlement("read:client.name", "read:ticket.status"),
        "declared_tools": frozenset({"client.read_summary", "ticket.read_status"}),
        "registry": _registry(),
        "leash": _leash(),
        "assessment": CLEAN,
        "now": NOW,
    }
    kwargs.update(overrides)
    return plan_piece_call(**kwargs)


class _Recorder:
    """A tool caller that runs nothing and remembers everything it was handed."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        *,
        tool: ToolDefinition,
        arguments: Any,
        entitlement: EntitlementSet,
        now: datetime | None,
    ) -> object:
        self.calls.append(
            {"tool": tool, "arguments": arguments, "entitlement": entitlement, "now": now}
        )
        return self.result


def _plan_reach() -> EntitlementSet:
    """The reach `_plan()`'s defaults produce, so a test can hand `run_step` the right one."""
    caller = _entitlement("read:client.name", "read:ticket.status")
    return caller.intersect(_entitlement("read:client.name", "read:ticket.status"))


def _one_client_row() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow](
        records=(ClientRow(entity="client", id="c_snm"),),
        source="local",
        fetched_at=NOW.isoformat(),
    )


# ------------------------------------------------- a piece never exceeds its principal
def test_a_flow_ceiling_narrower_than_its_caller_is_what_the_piece_gets() -> None:
    """The reach is `E(caller)` intersected with the flow's ceiling, so a flow declaring
    less than its caller holds comes out holding less. This is the direction that catches an
    implementation which passed the caller's own entitlement to the projector and treated the
    flow ceiling as decoration.

    Delete this and a flow scoped down to one capability silently inherits everything the
    person who triggered it can see."""
    inv = _plan(
        caller=_entitlement("read:client.name", "read:ticket.status"),
        flow_ceiling=_entitlement("read:client.name"),
    )

    assert offered_tools(inv) == ("client.read_summary",)


def test_a_flow_declaring_more_than_its_caller_holds_gets_nothing_extra() -> None:
    """The other direction, and the one a canvas author can actually reach: the flow
    declaration is a ceiling and never a grant. This catches an implementation that used the
    flow's declaration as the entitlement set, which would let anybody who can trigger an
    automation read whatever the automation was declared to reach.

    Delete this and a flow authored on the canvas can name any capability it likes."""
    inv = _plan(
        caller=_entitlement("read:client.name"),
        flow_ceiling=_entitlement("read:client.name", "read:ticket.status"),
    )

    assert offered_tools(inv) == ("client.read_summary",)


def test_the_automation_runs_as_the_person_who_triggered_it_and_never_as_the_flow() -> None:
    """Whose id the invocation carries decides whose name is on every audit row the run
    writes, and it is also what an intersection written the wrong way round quietly changes.
    The signature is asserted as well as the value: a `principal_id` parameter here would be
    a flow choosing whose reach it runs at.

    Delete this and `flow_ceiling.intersect(caller)` reads as the same line of code."""
    inv = _plan(
        caller=_entitlement("read:client.name", principal_id="u_weiling"),
        flow_ceiling=_entitlement("read:client.name", principal_id="flow_service_account"),
    )

    assert inv.principal_id == "u_weiling"
    assert "principal_id" not in inspect.signature(plan_piece_call).parameters


def test_a_tool_outside_the_flows_declared_set_is_absent_even_when_the_caller_holds_it() -> None:
    """The tool ceiling is the second half of the intersection. A caller entitled to
    everything must still only reach what the flow was built to use, or a flow that was
    reviewed as "reads tickets" becomes whatever its author's permissions allow.

    Delete this and `declared_tools` stops being load-bearing while every entitlement test
    here still passes."""
    inv = _plan(
        caller=_entitlement("read:client.name", "read:ticket.status", "read:archive.note"),
        flow_ceiling=_entitlement("read:client.name", "read:ticket.status", "read:archive.note"),
        declared_tools=frozenset({"ticket.read_status"}),
    )

    assert offered_tools(inv) == ("ticket.read_status",)


def test_a_flow_reads_and_changes_nothing_until_somebody_raises_its_side_effect_ceiling() -> None:
    """Both halves, because a guard tested only by its refusal is satisfied by a function
    that refuses everything. A sending tool is absent under the default ceiling and present
    when the ceiling is raised deliberately.

    Delete this and adding one write tool to a declared set turns every existing flow into
    one that can write."""
    holds_everything = _entitlement("read:client.name", "write:invoice.status")
    declared = frozenset({"client.read_summary", "invoice.send_reminder"})

    default = _plan(caller=holds_everything, flow_ceiling=holds_everything, declared_tools=declared)
    raised = _plan(
        caller=holds_everything,
        flow_ceiling=holds_everything,
        declared_tools=declared,
        max_side_effect=SideEffect.SEND,
    )

    assert offered_tools(default) == ("client.read_summary",)
    assert offered_tools(raised) == ("client.read_summary", "invoice.send_reminder")


def test_no_declared_tool_is_required_so_an_unreachable_one_never_names_itself() -> None:
    """`project` raises naming the required tools that did not resolve, which is right for an
    agent manifest and is an enumeration when the manifest is a canvas somebody is editing.
    Leaving `required_tools` empty is what stops a flow author learning which of their
    declared names is the one this caller cannot reach.

    Delete this and a `required_tools=declared_tools` line reads as obviously correct."""
    ceiling = piece_ceiling(flow_id=FLOW, declared_tools=frozenset(ALL_TOOL_NAMES))

    assert ceiling.required_tools == frozenset()
    # And behaviourally: a declared tool the caller cannot reach is simply not offered.
    inv = _plan(
        caller=_entitlement("read:client.name"),
        flow_ceiling=_entitlement("read:client.name"),
        declared_tools=frozenset({"client.read_summary", "archive.read_note"}),
    )
    assert offered_tools(inv) == ("client.read_summary",)


def test_a_piece_call_cannot_be_planned_against_an_unfrozen_registry() -> None:
    """`freeze` runs the checks that can only be made once every tool is present, and the
    application's one frozen registry is the one `brain.tools.startup.build_registry`
    returns. An unfrozen one is a catalogue nobody validated being offered to the least
    trusted caller in the system, and it is also one more tool could still be added to after
    the projection ran.

    Delete this and a piece can be planned against a registry somebody assembled that
    afternoon."""
    with pytest.raises(PieceRefusedError, match="unfrozen"):
        _plan(registry=_registry(frozen=False))

    # The sibling: the frozen one works, so the guard is not satisfied by refusing everything.
    assert offered_tools(_plan(registry=_registry())) == (
        "client.read_summary",
        "ticket.read_status",
    )


# ------------------------------------------------- denied and absent are one refusal
def _refusal_for(tool: str, **overrides: Any) -> str:
    inv = _plan(**overrides)
    with pytest.raises(PieceRefusedError) as caught:
        resolve_step(PieceStep(tool=tool), inv)
    return str(caught.value)


def test_a_hidden_tool_and_a_missing_tool_refuse_with_the_same_sentence() -> None:
    """Five situations, one sentence. A tool that does not exist, one the caller is not
    entitled to, one outside the flow's declared set, one whose side effect is over the
    flow's ceiling, and a name that could never be a tool at all. Anything that told them
    apart would let a flow author enumerate the catalogue one step at a time from inside a
    sandbox whose whole premise is that its contents are untrusted.

    Delete this and a helpful "you are not permitted that tool" arrives in the next
    refactor, which is a sentence that names something the reader did not know existed."""
    caller = _entitlement("read:client.name", "read:ticket.status", "write:invoice.status")
    declared = frozenset({"client.read_summary", "archive.read_note", "invoice.send_reminder"})
    common: dict[str, Any] = {
        "caller": caller,
        "flow_ceiling": caller,
        "declared_tools": declared,
    }

    refusals = {
        # Not in the registry at all.
        _refusal_for("nowhere.read_thing", **common),
        # In the registry, declared by the flow, and the caller holds nothing for it.
        _refusal_for("archive.read_note", **common),
        # In the registry, held by the caller, and not declared by the flow.
        _refusal_for("ticket.read_status", **common),
        # In the registry, held, declared, and over the flow's side-effect ceiling.
        _refusal_for("invoice.send_reminder", **common),
        # Not a tool name in any grammar.
        _refusal_for("Robert'); DROP TABLE tools;--", **common),
    }

    assert refusals == {TOOL_NOT_AVAILABLE}


def test_a_refusal_names_nothing_and_counts_nothing() -> None:
    """The refusal has to survive being read by whoever wired the flow. A tool name in it is
    a fact about the catalogue; a number in it is a count of what was withheld, which is the
    same disclosure arriving as arithmetic.

    Delete this and `f"{step.tool} is not available"` looks like an improvement."""
    message = _refusal_for("archive.read_note")

    assert not re.search(r"\d", message), message
    for name in ALL_TOOL_NAMES:
        assert name not in message
    assert "{" not in TOOL_NOT_AVAILABLE, "an interpolated refusal is one that grows a reason"


def test_the_refusal_is_the_same_whether_two_tools_are_reachable_or_one() -> None:
    """The subtraction case, which is the one that gets missed. "Showing 1 of 4" is never
    written deliberately; it arrives because a message was built from something that knew how
    many tools there were. The two arrangements here differ in exactly that: the same caller,
    the same flow and the same absent tool, over a registry that gives them two reachable
    tools and one that gives them one. The refusals must be byte-identical.

    Delete this and a count can be recovered by asking twice from two flows."""
    caller = _entitlement("read:client.name", "read:ticket.status")
    common: dict[str, Any] = {
        "caller": caller,
        "flow_ceiling": caller,
        "declared_tools": frozenset({"client.read_summary", "ticket.read_status"}),
    }

    two_reachable = _refusal_for("nowhere.read_thing", registry=_registry(), **common)
    one_reachable = _refusal_for(
        "nowhere.read_thing",
        registry=_registry(only=("client.read_summary",)),
        **common,
    )

    assert two_reachable == one_reachable == TOOL_NOT_AVAILABLE


def test_a_tool_the_automation_may_reach_resolves_to_the_projected_definition() -> None:
    """The positive case for the refusal above. It also pins where the definition comes
    from: the object returned is the one in the projected catalogue, so a lookup that went
    to the registry instead would be visible even though the name would match.

    Delete this and `resolve_step` is satisfied by a function that always refuses."""
    inv = _plan()

    definition = resolve_step(PieceStep(tool="client.read_summary"), inv)

    assert definition in inv.catalogue.tools
    assert definition.name == "client.read_summary"


def test_the_offered_list_is_names_only_and_holds_nothing_the_caller_cannot_reach() -> None:
    """What a canvas puts in a dropdown. Names, in the catalogue's own order, and nothing
    else: no descriptions, no count, and nothing that could be differenced against a total,
    because the total is never published.

    Delete this and `offered_tools` grows a second return value that somebody finds useful
    for a progress message."""
    inv = _plan(
        caller=_entitlement("read:client.name"),
        flow_ceiling=_entitlement("read:client.name"),
        declared_tools=frozenset(ALL_TOOL_NAMES),
    )

    offered = offered_tools(inv)

    assert offered == ("client.read_summary",)
    assert offered == inv.catalogue.names
    assert all(isinstance(name, str) for name in offered)
    for hidden in ("archive.read_note", "invoice.send_reminder", "ticket.read_status"):
        assert hidden not in offered


# ------------------------------------------------- the step names a tool, never an address
def test_a_step_has_nowhere_to_name_an_address_or_an_identity() -> None:
    """The structural half of the whole leaf. A piece is safe because it cannot express a
    direct call, not because it is asked not to make one. Every field named here is one
    somebody would reasonably add, and each is the field that would make the gate optional.

    Delete this and a `url` field arrives in a diff that reads as making the piece more
    useful."""
    assert set(PieceStep.model_fields) == {"tool", "arguments"}

    for forbidden in (
        "url",
        "host",
        "method",
        "headers",
        "body",
        "opaque",
        "principal_id",
        "agent_id",
        "capability",
        "scope",
    ):
        # Typed as `dict[str, Any]` rather than built inline. `PieceStep.arguments` is
        # `dict[str, Any]`, dict is invariant in its value type, and a `dict[str, str]`
        # splatted in is therefore not assignable to it. `mypy src` never saw this because
        # it does not read the tests; the pre-push hook runs `mypy` over both and did.
        extra: dict[str, Any] = {forbidden: "anything"}
        with pytest.raises(ValidationError):
            PieceStep(tool="client.read_summary", **extra)


def test_a_step_argument_may_not_be_named_for_a_handler_s_wiring() -> None:
    """A handler takes its request first and its wiring as keywords, so the only way a step
    could reach the wiring is by supplying a key with the same name. A step that could set
    `entitlement` would be choosing its own reach, which is the escalation the rest of this
    file is arranged against. Refused when the step is built, so one cannot exist to be
    dispatched later by something that splats it.

    Delete this and the guard becomes "no dispatcher will ever use **kwargs"."""
    assert "entitlement" in RESERVED_ARGUMENT_NAMES

    with pytest.raises(ValidationError, match="wiring"):
        PieceStep(tool="client.read_summary", arguments={"entitlement": {"grants": []}})
    with pytest.raises(ValidationError, match="wiring"):
        PieceStep(tool="client.read_summary", arguments={"limit": 5, "now": "2026-01-01"})

    # The sibling: an ordinary argument is untouched, or the rule refuses everything.
    assert PieceStep(tool="client.read_summary", arguments={"limit": 5}).arguments == {"limit": 5}


def test_a_piece_step_is_a_step_the_flow_boundary_already_admits() -> None:
    """The two modules have to agree about what a piece step is called, or a descriptor
    carrying one is refused by `assert_deterministic` before it reaches anything here.
    Asserted through the boundary's own function rather than by comparing two strings.

    Delete this and the step kind can be renamed on one side, or bound to the HTTP step,
    which is the one kind a piece exists to replace."""
    assert PIECE_STEP_KIND is StepKind.TOOL_CALL

    assert_deterministic([{"kind": PIECE_STEP_KIND.value}])


def test_this_module_opens_no_socket_and_holds_no_address() -> None:
    """`EGRESS_IS_ENFORCED_BY_THE_NETWORK_AND_NOT_BY_THIS_MODULE` says the network boundary
    is configuration. This is the half of that claim which can be checked here: the module
    imports nothing that could connect and contains no literal that could be connected to,
    so a piece cannot acquire a route out by way of the code that runs its steps.

    Parsed rather than grepped, so a mention in a docstring is not an import.

    Delete this and a convenience `httpx.post` to a webhook lands in the module whose whole
    argument is that it opens nothing."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for connector in ("socket", "http", "httpx", "requests", "urllib", "aiohttp", "ftplib"):
        assert connector not in imported, f"the piece module imports {connector}"

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in literals if "://" in text], "the module names an address"


def test_the_sandbox_is_pointed_at_the_proxy_for_everything_that_is_not_its_own() -> None:
    """The configuration half, and the one line nothing else checks. `NO_PROXY` is the list
    of hosts the canvas contacts directly, so a name added to it is a route that skips the
    allowlist entirely. Today it holds the sandbox's own two services and loopback; an
    application hostname appearing there would be a flow reaching us without the gate, which
    is the failure this leaf exists to prevent.

    Delete this and `NO_PROXY: ...,app` is a one-word change that no test notices."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = set(compose["services"])

    exempt = {
        entry.strip()
        for entry in str(compose["services"]["activepieces"]["environment"]["NO_PROXY"]).split(",")
    }

    assert exempt <= services | {"localhost", "127.0.0.1"}, (
        f"these are contacted without passing the egress proxy: {sorted(exempt - services)}"
    )


# ------------------------------------------------- what leaves, and the reach it leaves under
def test_the_redactor_is_refused_a_reach_the_catalogue_was_not_projected_from() -> None:
    """`Invocation` carries a hash and never the entitlement, so the reach has to be handed
    in a second time, and the second time is where the caller's unnarrowed set gets passed by
    accident. A run whose catalogue was projected from one reach and whose output was
    redacted against a wider one would show the right tools and return the wrong fields.

    Delete this and the two halves can disagree silently, which is the only way this module
    could leak a field while every catalogue test above still passes."""
    caller = _entitlement("read:client.name", "read:client.margin")
    inv = _plan(caller=caller, flow_ceiling=_entitlement("read:client.name"))
    recorder = _Recorder(_one_client_row())

    with pytest.raises(PieceRefusedError, match="projected from"):
        run_step(
            PieceStep(tool="client.read_summary"),
            inv,
            reach=caller,
            tools=recorder,
            policy=POLICY,
            now=NOW,
        )

    assert recorder.calls == [], "the tool was called before the reach was checked"


def test_a_step_receives_only_the_fields_its_narrowed_reach_admits() -> None:
    """The end of the whole path, and the positive case for every refusal above. The caller
    holds the margin, the flow's ceiling does not, and the record comes back carrying both:
    what leaves must hold the name and not the margin.

    This is the property a canvas author cannot inspect and cannot influence, and it is the
    reason a piece is safer than an HTTP step rather than merely tidier.

    Delete this and the redactor could be handed the caller's own entitlement and nothing
    would notice, because the tool list would still be right."""
    caller = _entitlement("read:client.name", "read:client.margin")
    flow_ceiling = _entitlement("read:client.name")
    inv = _plan(caller=caller, flow_ceiling=flow_ceiling)
    reach = caller.intersect(flow_ceiling)
    recorder = _Recorder(_one_client_row())

    payload = run_step(
        PieceStep(tool="client.read_summary", arguments={"limit": 1}),
        inv,
        reach=reach,
        tools=recorder,
        policy=POLICY,
        now=NOW,
    )

    assert isinstance(payload, ChannelPayload)
    assert payload.records == ({"entity": "client", "id": "c_snm", "name": "SNM Holdings"},)
    assert "margin" not in payload.records[0]


def test_the_tool_caller_is_handed_the_projected_definition_and_the_narrowed_reach() -> None:
    """What crosses the seam is the object the projection produced, not a name a caller could
    have supplied, and the entitlement is the narrowed one rather than whatever the step's
    author holds. An implementation of `ToolCaller` therefore cannot be reached for a tool
    that never came out of the catalogue.

    Delete this and the seam can be handed a bare string, which any caller can build."""
    caller = _entitlement("read:client.name", "read:client.margin")
    flow_ceiling = _entitlement("read:client.name")
    inv = _plan(caller=caller, flow_ceiling=flow_ceiling)
    reach = caller.intersect(flow_ceiling)
    recorder = _Recorder(_one_client_row())

    run_step(
        PieceStep(tool="client.read_summary"),
        inv,
        reach=reach,
        tools=recorder,
        policy=POLICY,
        now=NOW,
    )

    (seen,) = recorder.calls
    assert seen["tool"] in inv.catalogue.tools
    assert isinstance(seen["tool"], ToolDefinition)
    assert seen["entitlement"].ent_hash() == inv.ent_hash
    assert seen["entitlement"].ent_hash() != caller.ent_hash()


def test_what_leaves_a_step_has_nowhere_to_put_a_redaction_trace() -> None:
    """`serialise_for_channel` rather than `redact`, so a flow holding the result cannot
    reach the half of the answer that names what was withheld. The canvas is a channel like
    any other, and this one is read by whoever wired the automation.

    Delete this and returning the `RedactedAnswer` looks like returning more information to
    a caller who is entitled to it."""
    inv = _plan()
    recorder = _Recorder(_one_client_row())

    payload = run_step(
        PieceStep(tool="client.read_summary"),
        inv,
        reach=_plan_reach(),
        tools=recorder,
        policy=POLICY,
        now=NOW,
    )

    assert isinstance(payload, ChannelPayload)
    for absent in ("trace", "redactions", "dropped", "policy_epoch", "ent_hash"):
        assert absent not in ChannelPayload.model_fields
    assert not hasattr(payload, "trace")


def test_the_canvas_never_receives_the_opaque_payload() -> None:
    """A caller who genuinely holds the opaque capability still gets a field-walked answer
    through this path. There is no expression in which a step could ask for the escape
    hatch, and an unredacted record leaving into a canvas would land in a store this system
    does not own, written to by flows nobody here reviews.

    Delete this and `opaque=step.opaque` becomes a two-line feature."""
    caller = _entitlement("read:client.name", "read:opaque_payload")
    inv = _plan(
        caller=caller, flow_ceiling=caller, declared_tools=frozenset({"client.read_summary"})
    )
    reach = caller.intersect(caller)
    recorder = _Recorder(_one_client_row())

    payload = run_step(
        PieceStep(tool="client.read_summary"),
        inv,
        reach=reach,
        tools=recorder,
        policy=POLICY,
        now=NOW,
    )

    assert payload.label != OPAQUE_LABEL
    assert payload.label == ""
    # Walked rather than passed through: the margin has no grant even for this caller.
    assert payload.records == ({"entity": "client", "id": "c_snm", "name": "SNM Holdings"},)


def test_a_result_the_redactor_cannot_walk_never_reaches_the_canvas() -> None:
    """A tool caller returning a bare dictionary is a tool that cannot be field-redacted, and
    the honest outcome is a refusal rather than a payload nobody walked. Enforced twice, by
    the type this module demands and by the redactor's own contract, which is why deleting
    one of the two is caught by the type checker rather than here.

    Delete this and an untyped result is a plausible thing for the seam to return."""
    inv = _plan()
    recorder = _Recorder({"entity": "client", "id": "c_snm", "margin": "34%"})

    with pytest.raises(UntypedShapeError):
        run_step(
            PieceStep(tool="client.read_summary"),
            inv,
            reach=_plan_reach(),
            tools=recorder,
            policy=POLICY,
            now=NOW,
        )
