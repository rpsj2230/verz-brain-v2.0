"""The custom piece: the one way a canvas step reaches company data, and the gate it runs.

An automation canvas normally lets somebody drop in a step that calls an external API
directly. On this platform that step would walk straight around the permission gate: the
flow author would be choosing the address, the credential and the query, and nothing
between the canvas and the data would ever ask who the automation runs as. `StepKind` in
`brain.ops.automation` keeps `HTTP_REQUEST` for exactly the plumbing that deserves it, and
the egress allowlist is what stops that step being pointed anywhere interesting. This
module is the other half: **the step kind that reaches our own data, and the reason it is
safe is that it is not a request at all.**

**A piece step names a tool and has nowhere to name an address.** `PieceStep` has no url,
no host, no method, no header and no body. That is the structural half of
`THE_STEP_NAMES_A_TOOL_AND_NEVER_AN_ADDRESS`, and it is the same construction
`brain.tools.run_skill.SandboxSpec` uses about credentials and `brain.ops.automation`
uses about agent control flow: a rule saying "do not call an arbitrary endpoint" holds
until the first person who needs to, whereas a type with no field for one cannot.

**The reach is `E(caller) intersected with the flow's ceiling, and it is computed by
calling `flow_reach`.** Not reimplemented, not approximated, not "the same arithmetic".
`brain.gate.invoke.invoke` and `brain.ops.automation.flow_reach` both reach
`EntitlementSet.intersect` and neither writes the set arithmetic itself; a third copy
would be a third place for the platform's central rule to be subtly wrong, and this one
would be the copy running underneath code a client's own staff assembled. So the only
thing this module adds is the order the parts go in.

**The tool list is `brain.gate.catalogue.project`'s, reached through `invoke`.** Rejected:
filtering the registry here against the flow's declared tools, which is the obvious
implementation and is wrong twice. It would skip the entitlement check that decides which
tools exist for this caller at all, and it would produce a plain tuple where `invoke`
produces a `ProjectedCatalogue`, which is a type only the projector can build. A function
taking the projected catalogue has proof the projection ran; one taking a tuple of names
has a promise.

**DENIED and ABSENT are one refusal, and the refusal is a constant rather than a message.**
`TOOL_NOT_AVAILABLE` is a module-level string with nothing interpolated into it, so it
cannot come to vary with the tool, the reason or a count. A piece that failed differently
for "that tool exists and you may not have it" and "there is no such tool" would let a flow
author enumerate the catalogue one step at a time, from inside a system whose whole premise
is that its contents are untrusted. A malformed name gets the same sentence for the same
reason: two refusals is two facts.

**The redactor runs on the reach the catalogue was built from, and that is checked rather
than assumed.** `Invocation` deliberately carries the entitlement hash and never the
entitlement, so `run_step` has to be handed the reach a second time, and the second time is
where somebody hands it the caller's unnarrowed set by accident. `assert_same_reach`
compares the hashes. See `THE_REDACTOR_SEES_THE_REACH_THE_CATALOGUE_WAS_BUILT_FROM`.

**What leaves is a `ChannelPayload`.** `serialise_for_channel` is the only path from a
typed result to something outside, and what makes it the right one here is what it does not
return: no trace, no redaction reasons, no dropped-record count, no policy epoch. The
canvas is a channel like any other, and a channel that could read the trace would learn the
names of the fields it was refused.

**Egress is not decided here, and this module could not decide it.** The enforcement point
for "a piece cannot reach the network directly" is configuration, in two files:
`docker-compose.automation.yml`, where the `automation` network is `internal: true` and the
proxy is the only service also on a network that reaches out, and `ops/automation/egress.conf`,
where the allowlist is applied outside the sandbox. See
`EGRESS_IS_ENFORCED_BY_THE_NETWORK_AND_NOT_BY_THIS_MODULE`. Saying "this is policy and the
enforcement is elsewhere" is the correct move where it is true, which is what
`brain.tools.run_skill` does about its container and `brain.ops.wiring` does about a compose
file that has never run.

Not claimed, and each for a reason rather than for want of time.

*The TypeScript piece is not written.* The Activepieces plugin format is a package that is
built, linted and published by a toolchain this repository does not have, so a `.ts` file
here would be the seventh mechanism in this tree that nothing calls: nothing would compile
it, nothing would type-check it, and it would drift from this module silently. What that
file has to contain is fully determined by what is here, which is a step declaring a tool
name and a bag of arguments and nothing else.

*The transport does not exist.* The sandbox has no route to this application at all: the
`automation` network is internal, the proxy is the only way out, and the allowlist names
five third-party hosts and nothing of ours. `docker-compose.automation.yml` says so in as
many words. Giving the canvas a route is a deployment decision with two real options, a
second network carrying the application and the canvas alone, or the application's public
hostname on the allowlist, and both change the deployed network topology inside a commit
about something else.

*Nothing calls `run_step` yet, and the reason is not this leaf.* There is no HTTP route
behind the gate anywhere in this repository, and `brain.app.create_app` says so where it
turns off the schema; and no code in this tree has ever called a registered tool handler,
so there is no dispatcher for `ToolCaller` to be implemented against. Inventing one here
would be a second dispatch path, private to the automation canvas, that the real one would
later have to be reconciled with. `plan_piece_call` refusing an unfrozen registry is the
part of the wiring that can be done today: the only frozen `ToolRegistry` in the
application is the one `brain.tools.startup.build_registry` returns and `brain.app.lifespan`
puts on `app.state`, so a piece call cannot be planned against a registry somebody
assembled that afternoon.

Task ids: M32.6.1.3
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.entitlement import EntitlementSet
from brain.core.envelope import SideEffect, ToolDefinition
from brain.core.field_policy import FieldPolicy
from brain.core.redaction import ChannelPayload, require_typed_result, serialise_for_channel
from brain.gate.catalogue import AgentCeiling
from brain.gate.injection import RiskAssessment
from brain.gate.invoke import Invocation, invoke
from brain.gate.leash import Leash
from brain.ops.automation import AutomationError, StepKind, flow_reach
from brain.tools.registry import ToolRegistry

# ------------------------------------------------------------------ written-down reasons

#: The invariant this leaf exists to keep, stated where a reader meets it.
A_PIECE_NEVER_EXCEEDS_THE_PRINCIPAL_THE_AUTOMATION_RUNS_AS: Final = (
    "A step inside an automation runs at the reach of the principal the automation runs "
    "as, narrowed by the flow's own ceiling, and can never widen either. The reach is "
    "E(caller) intersected with the flow ceiling, computed by calling flow_reach and by "
    "nothing else, and the tool list is the projection of that reach rather than the "
    "registry. A step that could exceed its principal is a privilege escalation with a "
    "friendly name on a canvas, and the person who wired it would not know they had done it."
)

#: Why one sentence covers four different situations.
A_HIDDEN_TOOL_AND_A_MISSING_TOOL_ARE_ONE_REFUSAL: Final = (
    "A tool the caller is not entitled to, a tool outside the flow's declared set, a tool "
    "whose side effect is over the flow's ceiling, and a tool that does not exist all "
    "refuse with the same constant sentence. Anything else lets a flow author enumerate "
    "the catalogue one step at a time. There is no count of what was withheld, and none by "
    "subtraction either: the refusal interpolates nothing, so it cannot come to carry one."
)

#: The structural half of "the piece calls our tool API rather than an API".
THE_STEP_NAMES_A_TOOL_AND_NEVER_AN_ADDRESS: Final = (
    "PieceStep has no url, host, method, header or body field, so a canvas step cannot "
    "express a direct call to anything. That is the whole difference between this step and "
    "the HTTP step it replaces: the gate is not something the piece is asked to go through, "
    "it is the only thing the piece can express."
)

#: Why the reach is handed in twice and checked the second time.
THE_REDACTOR_SEES_THE_REACH_THE_CATALOGUE_WAS_BUILT_FROM: Final = (
    "Invocation carries an entitlement hash and never the entitlement, so the reach has to "
    "be supplied again to redact the result, and the second supply is where the caller's "
    "unnarrowed set gets passed by accident. The hashes are compared instead of trusted. A "
    "run whose catalogue was projected from one reach and whose output was redacted against "
    "a wider one would show the right tools and return the wrong fields."
)

#: Why the escape hatch is not reachable from a canvas.
THE_CANVAS_NEVER_RECEIVES_AN_OPAQUE_PAYLOAD: Final = (
    "redact's opaque passthrough returns whole records with no field-level redaction, and "
    "PieceStep has no field that could ask for it. An opaque payload leaving into a canvas "
    "would be an unredacted record landing in a store this system does not own, written to "
    "by flows nobody here reviews. A caller who genuinely holds the opaque capability still "
    "gets a field-walked answer through this path, which is narrower than their reach and "
    "deliberately so."
)

#: Where the network boundary actually is, since it is not in this file.
EGRESS_IS_ENFORCED_BY_THE_NETWORK_AND_NOT_BY_THIS_MODULE: Final = (
    "A piece cannot reach the network directly, and the enforcement point is configuration "
    "rather than code. docker-compose.automation.yml puts the canvas on an internal network "
    "with no route out and gives the proxy the only membership of a network that reaches "
    "one; ops/automation/egress.conf applies the allowlist outside the sandbox, on exact "
    "hostnames, denying by default. An allowlist checked inside the sandbox is an allowlist "
    "the sandbox can edit. Nothing in this module opens a socket, names a host or holds a "
    "credential, and there is nothing here that could."
)

#: The one thing a refused step is ever told. A constant with nothing interpolated into it,
#: which is what makes `A_HIDDEN_TOOL_AND_A_MISSING_TOOL_ARE_ONE_REFUSAL` a shape rather
#: than a habit: a message assembled per case is a message that grows a reason.
TOOL_NOT_AVAILABLE: Final = "no tool by that name is available to this automation"

#: Argument names a step may not use, because a handler takes them as wiring. `entitlement`
#: is the one that matters and the rest are here because the list is read by whoever adds
#: the next handler: a step that could set `entitlement` would be choosing its own reach,
#: which is the escalation this whole module is arranged against.
#:
#: Refused at construction rather than dropped at dispatch. A dropped argument is one the
#: flow author believes arrived, and the belief survives longer than the flow does.
RESERVED_ARGUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "agent_ceiling",
        "agent_id",
        "entitlement",
        "now",
        "opaque",
        "policy",
        "principal_id",
        "records",
        "registry",
        "scope",
    }
)

#: The step kind this module is the contract for. Named rather than restated, so the closed
#: set in `brain.ops.automation` stays the one place a step kind is spelled.
PIECE_STEP_KIND: Final = StepKind.TOOL_CALL


class PieceRefusedError(AutomationError):
    """A step may not run. A subclass of `AutomationError` rather than a new taxonomy.

    The automation boundary already has one exception type covering a flow, an egress target
    and a container environment, and its argument holds here: the refusals differ in what
    caused them and not in what the flow author can do about them, which is edit the flow.
    A distinct type exists only so an operator can tell a refused step from a malformed
    descriptor while reading the same handler.
    """


# ------------------------------------------------------------------ what a step may say


class PieceStep(BaseModel):
    """The whole of what a canvas step may say to this platform.

    **Read the absences.** There is no url, so a step cannot name an endpoint. There is no
    method, header or body, so it cannot shape a request. There is no principal, agent or
    flow identity, so it cannot choose whose reach it runs at. There is no capability,
    scope or ceiling, so it cannot ask for more than the automation was granted. There is no
    opaque flag, so it cannot ask for the unredacted payload. Each of those is a field
    somebody would reasonably add, and each is the field that would make the gate optional.

    `arguments` is a mapping and not a string, so nothing here is parsed or split by
    anything downstream, which is the argument `brain.tools.run_skill.ScriptRequest` makes
    about argv.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Deliberately unpatterned. A name that could never be a tool is refused by the same
    #: sentence as one that could, because a shape-based refusal here would be a second
    #: outcome a flow author can tell apart, and two outcomes is two facts.
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _no_argument_names_a_wiring_parameter(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Refuse a step that names a parameter a handler takes as wiring.

        Named rather than positional is what makes this checkable at all: a handler in this
        repository takes its request first and its wiring as keywords, so the only way a
        step could reach the wiring is by supplying a key with the same name. Refused when
        the step is built, so a step carrying one cannot exist to be dispatched later by
        something that splats it.
        """
        clashing = sorted(set(v) & RESERVED_ARGUMENT_NAMES)
        if clashing:
            msg = (
                f"a step argument is named {clashing}, which a tool handler takes as wiring; "
                "a step that could set its own entitlement would be choosing its own reach"
            )
            raise ValueError(msg)
        return v


class ToolCaller(Protocol):
    """Whatever actually runs a tool. The one thing this module does not do.

    A protocol for the reason `brain.tools.run_skill.ScriptRunner` and
    `brain.knowledge.rows.RowSource` are protocols, and for one more that is specific to
    here: **no code in this repository has ever called a registered tool handler.** There is
    no dispatcher to reuse, and writing one inside the automation canvas would be a second
    dispatch path, owned by the least trusted caller in the system, that the real one would
    afterwards have to be reconciled with.

    It is handed the `ToolDefinition` rather than a tool name, so an implementation cannot
    be called for a tool that did not come out of the projected catalogue. It returns
    `object` rather than a typed result, so the redaction contract's own refusal is what
    decides whether the value can be walked.
    """

    def call(
        self,
        *,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        entitlement: EntitlementSet,
        now: datetime | None,
    ) -> object: ...


# ------------------------------------------------------------------ planning the call


def piece_ceiling(
    *,
    flow_id: str,
    declared_tools: frozenset[str],
    max_side_effect: SideEffect = SideEffect.NONE,
) -> AgentCeiling:
    """The flow's tool ceiling, in the type the projector already understands.

    `required_tools` is deliberately left empty. A required tool that does not resolve makes
    `project` raise `EmptyCatalogueError` naming it, which is right for an agent manifest
    somebody wrote and wrong for a canvas: the flow author would learn which of their
    declared names is the one this caller cannot reach, which is the enumeration
    `A_HIDDEN_TOOL_AND_A_MISSING_TOOL_ARE_ONE_REFUSAL` exists to close.

    `max_side_effect` defaults to NONE, so a flow reads and changes nothing until somebody
    raises it deliberately. Defaulting to whatever the tools allow would let one write tool
    added to a declared set turn every existing flow into one that can write.
    """
    return AgentCeiling(
        agent_id=flow_id,
        allowed_tools=declared_tools,
        max_side_effect=max_side_effect,
    )


def plan_piece_call(
    *,
    flow_id: str,
    caller: EntitlementSet,
    flow_ceiling: EntitlementSet,
    declared_tools: frozenset[str],
    registry: ToolRegistry,
    leash: Leash,
    assessment: RiskAssessment,
    now: datetime,
    max_side_effect: SideEffect = SideEffect.NONE,
    row: dict[str, str] | None = None,
) -> Invocation:
    """Everything a piece may reach, decided before any step runs (M32.6.1.3).

    **There is no `principal_id` parameter and there must not be one.** The principal is
    read off the reach, which carries the caller's own id because `EntitlementSet.intersect`
    keeps it, so a flow cannot run as somebody else by naming them. A flow acting under its
    own identity is a flow whose actions cannot be traced to the person who triggered it.

    The order is the same one `invoke` insists on and for the same reason. The entitlement
    intersection first, so the tool list is projected from the narrowed reach rather than
    from the caller's; the tool ceiling second, expressed as an `AgentCeiling` because that
    is what the projector takes. A flow declaring `client.read_summary` on behalf of a caller
    who cannot read clients comes out with neither.

    `assessment` is required rather than defaulted to a clean one. A flow asks no model
    anything, so there is usually nothing to steer, but a trigger payload is text somebody
    outside this company can write and the rung it lands on is not a decision this module
    should be making silently on a caller's behalf.

    `registry` must be frozen. `ToolRegistry.freeze` runs the checks that can only be made
    once every tool is present, and the application's one frozen registry is the one
    `brain.tools.startup.build_registry` returns. A piece planned against an unfrozen
    registry would be offering a catalogue nobody validated to the least trusted caller in
    the system.
    """
    if not registry.is_frozen:
        msg = (
            "a piece call was planned against an unfrozen tool registry; freeze runs the "
            "checks that need the whole set, and the catalogue a canvas is offered must not "
            "be one somebody assembled beside the builder"
        )
        raise PieceRefusedError(msg)

    reach = flow_reach(caller, flow_ceiling)
    return invoke(
        principal_id=reach.principal_id,
        agent_id=flow_id,
        registry=registry,
        entitlement=reach,
        ceiling=piece_ceiling(
            flow_id=flow_id,
            declared_tools=declared_tools,
            max_side_effect=max_side_effect,
        ),
        leash=leash,
        assessment=assessment,
        now=now,
        row=row,
    )


def offered_tools(invocation: Invocation) -> tuple[str, ...]:
    """Every tool this automation may offer its author, by name and nothing else.

    What a canvas puts in a dropdown. Names only: there is no description here, no count,
    and nothing that could be differenced against a published total, because the total is
    never published. `brain.gate.catalogue` makes the same argument about an unreachable
    tool being absent rather than described and refused.
    """
    return invocation.catalogue.names


def resolve_step(step: PieceStep, invocation: Invocation) -> ToolDefinition:
    """The tool this step names, if this automation may reach it (M32.6.1.3).

    Matched against the **projected catalogue** and never the registry, which is the whole
    of `A_PIECE_NEVER_EXCEEDS_THE_PRINCIPAL_THE_AUTOMATION_RUNS_AS` at the point a step is
    dispatched. A registry lookup here would find every tool the platform has and leave the
    entitlement check to whoever wrote the caller.

    One refusal, always the same sentence. See `A_HIDDEN_TOOL_AND_A_MISSING_TOOL_ARE_ONE_REFUSAL`.
    """
    for definition in invocation.catalogue.tools:
        if definition.name == step.tool:
            return definition
    raise PieceRefusedError(TOOL_NOT_AVAILABLE)


def assert_same_reach(invocation: Invocation, reach: EntitlementSet) -> None:
    """Refuse a run whose redaction reach is not the one its catalogue was projected from.

    See `THE_REDACTOR_SEES_THE_REACH_THE_CATALOGUE_WAS_BUILT_FROM`. Compared on the hash
    because that is what an `Invocation` carries, and the hash is order-independent and
    includes the time bound, so a set rebuilt in a different order still compares equal and
    one whose expiry differs does not.
    """
    if reach.ent_hash() != invocation.ent_hash:
        msg = (
            "the reach offered for redaction is not the one this call's catalogue was "
            "projected from; a run that showed one reach's tools and redacted against "
            "another would return fields its own catalogue never admitted"
        )
        raise PieceRefusedError(msg)


def run_step(
    step: PieceStep,
    invocation: Invocation,
    *,
    reach: EntitlementSet,
    tools: ToolCaller,
    policy: FieldPolicy,
    now: datetime | None = None,
) -> ChannelPayload:
    """Run one piece step and return only what may leave (M32.6.1.3).

    Four things in an order that is not rearrangeable. The reach is checked against the one
    the catalogue was projected from, before anything is looked up, so a mismatched pair
    cannot get as far as a tool. The tool is resolved out of the projected catalogue, so a
    tool this automation may not reach is refused before it is called rather than after.
    The call goes through `ToolCaller`, which is handed the resolved definition and the
    narrowed reach and nothing else. What comes back is walked by the redactor against that
    same reach.

    **`opaque` is never passed.** See `THE_CANVAS_NEVER_RECEIVES_AN_OPAQUE_PAYLOAD`. It is
    not that a canvas is refused the escape hatch; it is that there is no expression in
    which a canvas could ask for it.

    Returns a `ChannelPayload`, which has nowhere to put a trace, a redaction reason, a
    dropped-record count or a policy epoch. `serialise_for_channel` is used rather than
    `redact` for exactly that: a caller holding the payload cannot reach the half of the
    answer that names what was withheld.
    """
    assert_same_reach(invocation, reach)
    definition = resolve_step(step, invocation)
    returned = tools.call(
        tool=definition,
        arguments=step.arguments,
        entitlement=reach,
        now=now,
    )
    # Called here as well as inside the redactor, and deliberately. It is what types the
    # value for everything below, and the redactor's own call is not something this module
    # should be relying on having happened.
    result = require_typed_result(returned)
    return serialise_for_channel(result, entitlement=reach, policy=policy, now=now)
