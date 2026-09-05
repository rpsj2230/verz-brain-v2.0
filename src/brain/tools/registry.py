"""What a tool is allowed to be, decided at registration rather than per request.

`brain.gate.catalogue` already computes what a caller may see. This module is what it
computes over, and the split is the point: projection is a per-request question about a
person, while every rule here is a question about the tool itself, asked once, in front of
whoever wrote it. A tool is also the only grantable thing in the platform. A skill is a
procedure an agent loads and a connector is a deployment unit; a capability is asked for by
a tool and by nothing else, so this is where a mistake about reach is still cheap.

**What breaks without it.** Every refusal below moves to request time, where it is an
answer going wrong rather than a build going red.

*A malformed name reaches the model as an option.* The model picks from what it is shown,
so a name that does not read as `source.verb_noun` is either never selected or selected for
the wrong reason, and neither failure raises anything anybody sees.

*A tool that cannot be redacted raises inside a request that has already fetched rows.*
`brain.core.redaction.require_typed_result` makes exactly that refusal at the boundary, and
it is the right refusal one request too late: the connector has run, a person is waiting,
and the outcome is an exception instead of a fix.

*A malformed required capability fails closed in `catalogue._admits` and tells nobody.* The
tool then disappears from every catalogue for every caller, which looks exactly like a
permission problem, while the typo that caused it sits in a file nobody is reading. Failing
closed is right at request time and useless as a way of finding out.

*A SERVICE identity-mode tool with no scope predicate reaches everything its shared
credential reaches.* The source will not narrow it for us. That is the whole difference
between DELEGATED, where the source enforces its own permissions as well, and SERVICE,
where ours are the only ones there are.

*Two tools sharing a name resolve by import order*, and which one runs is decided by
whichever module happened to be imported second.

Scope: this is domain logic. Nothing here opens a connection, reads a table or calls a
model. `tool_definition` is a table somebody else owns; what is here is the type and the
rules that govern it.

Task ids: M12.1.1, M12.1.2, M12.1.3, M12.1.4, M12.1.5
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Self, assert_never

from brain.core.entitlement import Capability
from brain.core.envelope import IdentityMode, SideEffect, ToolDefinition
from brain.core.redaction import OPAQUE_CAPABILITY, assert_tool_returns_typed_result
from brain.core.scope import Scope
from brain.gate.injection import AutonomyTier

# --------------------------------------------------------------------- grammars

#: `source.verb_noun`, and deliberately stricter than either grammar already in the tree.
#:
#: `brain.core.envelope.ToolDefinition.name` and `brain.ops.sweeps.TOOL_NAME_RE` both admit
#: `client.read`, because both are written as `name.name`. That is a floor rather than the
#: rule: the second half is what tells a model what the tool does *to what*, and `read` on
#: its own says only that something is read. The model has one line of description and this
#: name to choose from, so the name carrying its object is not decoration.
#:
#: Rejected: also requiring the verb to be one of `brain.core.entitlement.VERBS`. The
#: architecture's own examples include `ticket.set_status`, and `set` is not a capability
#: verb. A grammar that refuses the specification's own examples is a grammar somebody
#: edits out of the way, and then nothing checks the shape at all.
TOOL_NAME_RE: Final = re.compile(
    r"^(?P<source>[a-z][a-z0-9_]*)\.(?P<verb>[a-z][a-z0-9]*)_(?P<noun>[a-z][a-z0-9_]*)$"
)

#: What a tool's object may be called. `ToolDefinition.entity` carries no pattern of its
#: own, only a length, while `brain.core.envelope.Entity.entity` is `^[a-z][a-z0-9_]*$` and
#: `brain.core.field_policy` looks its rules up by that name. A tool declaring `Client Ltd`
#: as its object would therefore match no policy rule, and default-deny would withhold every
#: field of every record it returned, which reads as a permission failure rather than a typo.
OBJECT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")

#: The one object name that may only be claimed by one tool, and the tool that claims it.
#:
#: The architecture is explicit that a skill's scripts run through a single tool carrying
#: the sandbox, the leash and output redaction. "Single" has to be enforced somewhere, and
#: a constant defined where it is not enforced is a constant somebody redefines. It lives
#: here rather than in `brain.tools.skills` because this module is the one that can refuse
#: a second claimant, and `skills` imports it back for the same reason.
SKILL_SCRIPT_OBJECT: Final = "skill_script"
RUN_SKILL_SCRIPT: Final = "skill.run_script"


class ResultContract(enum.StrEnum):
    """What shape a tool promises to return (M12.1.4).

    Two members and no third. `TYPED` is the ordinary case, walked field by field by
    `brain.core.redaction`. `OPAQUE` is the escape hatch for data that genuinely cannot be
    field-typed, and it is a declaration on the tool rather than a flag on a call so that
    "which tools bypass field-level redaction" is a list somebody can read.

    Declaring `OPAQUE` does **not** exempt a tool from returning a `TypedResult`. The
    opaque path in `redact` still calls `require_typed_result` before it dumps anything, so
    a tool that cannot be walked cannot be registered under either contract.
    """

    TYPED = "typed"
    OPAQUE = "opaque"


class ToolRegistrationError(Exception):
    """A tool was declared in a way that cannot be made safe.

    Outside the user-facing taxonomy in `brain.core.errors`, deliberately, and for the
    reason `brain.core.redaction.UntypedShapeError` gives: nobody asking a question should
    ever see this. It is a contract violation by whoever wrote the tool, and it should stop
    that tool existing rather than degrade somebody's answer at request time.

    One exception type for every refusal, as `ChannelPathError` is. The refusals differ in
    message and not in what the caller can do about them, and a taxonomy of registration
    errors would invite a caller to catch some of them.
    """


# ------------------------------------------------------------------ the refusals


def assert_tool_name(name: str) -> None:
    """Refuse anything that is not `source.verb_noun` (M12.1.1)."""
    if not TOOL_NAME_RE.match(name):
        msg = (
            f"tool name {name!r} is not source.verb_noun; the catalogue is projected per "
            "request and the model picks from what it is shown, so a malformed name is a "
            "tool that is either never selected or selected for the wrong reason"
        )
        raise ToolRegistrationError(msg)


def assert_object_name(definition: ToolDefinition) -> None:
    """Refuse an object name no field policy could ever match."""
    if not OBJECT_NAME_RE.match(definition.entity):
        msg = (
            f"tool {definition.name!r} declares object {definition.entity!r}, which is not a "
            "name; a field policy is looked up by entity, so every field of every record it "
            "returns would be withheld as unclassified and read as a permission failure"
        )
        raise ToolRegistrationError(msg)


def assert_source_agrees(definition: ToolDefinition) -> None:
    """The name's first segment is the source, so a declared source must be the same one.

    Checked only when `source` is set, because it defaults to empty and most tools carry
    the source in the name alone. Where both are present and they disagree, one of them is
    wrong about which system the call goes to, and the identity mode and the credential
    follow the source rather than the name.
    """
    if not definition.source:
        return
    declared = definition.name.split(".", 1)[0]
    if definition.source != declared:
        msg = (
            f"tool {definition.name!r} declares source {definition.source!r} but is named "
            f"for {declared!r}; the name is what a reader and a trace believe, and the "
            "credential follows the source"
        )
        raise ToolRegistrationError(msg)


def capability_for(definition: ToolDefinition) -> Capability:
    """The tool's requirement, parsed (M12.1.2).

    `ToolDefinition.required_capability` is a string, so this is the one place it becomes a
    `Capability`. An unparseable one is refused here rather than carried: `catalogue._admits`
    already treats it as unreachable, which is the correct request-time behaviour and a
    terrible way to find out, because the tool simply never appears for anybody.

    What must never happen is the other reading. Treating an unparseable requirement as "no
    requirement" turns a typo into an open door, and it is the natural shape of a defensive
    `try: ... except: pass` written by somebody who wanted registration to stop failing.
    """
    try:
        return Capability(value=definition.required_capability)
    except ValueError as exc:
        msg = (
            f"tool {definition.name!r} requires {definition.required_capability!r}, which is "
            "not a capability; an unparseable requirement must make a tool unreachable and "
            "never unrestricted, so it is refused here rather than skipped at request time"
        )
        raise ToolRegistrationError(msg) from exc


def assert_effect_matches_capability(definition: ToolDefinition, capability: Capability) -> None:
    """A tool that changes something may not ask only for permission to read it.

    Asymmetric on purpose. A read-only tool demanding `write:ticket.status` is merely
    over-strict: fewer people reach it than need to, and nothing is disclosed. A SEND tool
    demanding `read:invoice.status` is an escalation, because everybody who can read an
    invoice can then send one, and the catalogue will happily show it to them.

    Rejected: requiring the verb to match the effect exactly, so that WRITE implies `write:`
    and MONEY implies `approve:`. The architecture's own money example requires
    `approve:payment.release` while its send example requires `write:invoice.status`, so an
    exact mapping would refuse one of the two, and the pair is what the mapping was drawn
    from.
    """
    if definition.side_effect is not SideEffect.NONE and capability.verb == "read":
        msg = (
            f"tool {definition.name!r} has side effect {definition.side_effect.value!r} and "
            f"requires {capability.value!r}; a tool that changes something while asking only "
            "to read it makes everybody who can read the record able to change it"
        )
        raise ToolRegistrationError(msg)


def assert_service_tool_is_scoped(definition: ToolDefinition, scope: Scope | None) -> None:
    """A SERVICE identity-mode tool must carry a scope predicate that narrows something.

    `ToolDefinition` has nowhere to put a scope, so the registry carries it beside the
    definition. That is why this is a registration rule and not a validator on the model.

    An unrestricted scope is refused as firmly as a missing one. `Scope()` satisfies "it has
    a scope" and admits every row, so accepting it would leave the rule enforceable only by
    whoever remembers what it was for, and a shared credential reaching everything is exactly
    the failure the rule exists to name.
    """
    if definition.identity_mode is not IdentityMode.SERVICE:
        return
    if scope is None or scope.is_unrestricted():
        msg = (
            f"tool {definition.name!r} runs as SERVICE and carries "
            f"{'no scope' if scope is None else 'an unrestricted scope'}; a shared credential "
            "is not narrowed by the source, so a service tool with nothing to narrow it "
            "reaches everything that credential can reach"
        )
        raise ToolRegistrationError(msg)


def assert_result_contract(
    definition: ToolDefinition, capability: Capability, contract: ResultContract
) -> None:
    """An opaque tool must require the opaque capability, or it is shown and then refused.

    `redact(opaque=True)` raises `Denied` for a caller who does not hold
    `read:opaque_payload`. If an opaque tool required anything else, it would pass the
    catalogue's entitlement check, be described to the model, be selected, and fail after the
    fetch. `brain.gate.catalogue` exists to stop precisely that: an unreachable tool is
    absent, never described and refused, because the model explains the refusal and the
    explanation is the leak.

    The cost is real and accepted. `ToolDefinition` carries one requirement, so an opaque
    tool cannot also demand `read:client.name`; its reach is governed by a capability almost
    nobody holds instead of by the entity. That is the right trade only because the opaque
    path returns everything anyway, so an entity-level requirement in front of it would be
    decoration.
    """
    if contract is ResultContract.OPAQUE and capability != OPAQUE_CAPABILITY:
        msg = (
            f"tool {definition.name!r} declares an opaque result but requires "
            f"{capability.value!r}; the redactor demands {OPAQUE_CAPABILITY.value!r} for an "
            "opaque payload, so this tool would be shown to callers who cannot receive it "
            "and refused after the fetch"
        )
        raise ToolRegistrationError(msg)


def assert_object_not_reserved(definition: ToolDefinition) -> None:
    """Only `skill.run_script` may claim the skill-script object.

    A second tool that ran a skill's scripts would be a second path to execution, and the
    sandbox, the leash and the output redaction are properties of the path rather than of
    the script. The reservation is checked on the object rather than on the name because the
    object is what a policy, a leash target and a slug collision are all written about.
    """
    if definition.entity != SKILL_SCRIPT_OBJECT:
        return
    if definition.name != RUN_SKILL_SCRIPT:
        msg = (
            f"tool {definition.name!r} claims the {SKILL_SCRIPT_OBJECT!r} object, which is "
            f"reserved for {RUN_SKILL_SCRIPT!r}; a second way to run a skill's scripts is a "
            "second path that the sandbox and the leash do not sit on"
        )
        raise ToolRegistrationError(msg)


# ------------------------------------------------------- side effect to leash (M12.1.3)


def default_rung(effect: SideEffect) -> AutonomyTier:
    """The rung a side effect suggests, for an admin writing a leash entry.

    Exhaustive by `match`, so a sixth side effect cannot reach production without somebody
    deciding how much supervision it needs. A dictionary with a `.get` default would accept
    it silently as whatever the default was, which is the argument
    `brain.gate.leash.route_for` makes about routes.

    The mapping is not injective and does not need to be. DRAFT, WRITE and SEND all land on
    ASSISTED: they differ in how far the mistake travels, which is what `max_side_effect` on
    an agent ceiling is for, and not in whether a person should see it first. MONEY is
    SHADOW because the architecture pins the money-touching templates to Shadow at manifest
    level regardless of promotion criteria.
    """
    match effect:
        case SideEffect.NONE:
            return AutonomyTier.AUTONOMOUS
        case SideEffect.DRAFT | SideEffect.WRITE | SideEffect.SEND:
            return AutonomyTier.ASSISTED
        case SideEffect.MONEY:
            return AutonomyTier.SHADOW
        case _:
            assert_never(effect)


def rung_ceiling(rung: AutonomyTier, effect: SideEffect) -> AutonomyTier:
    """The configured rung, tightened by what the side effect suggests. Never loosened.

    This is the only shape in which a default may be applied to a leash. `brain.gate.leash`
    has no default field at all, and says why: a default is read as a convenience, written
    once by whoever installed the agent, and inherited from then on by every target nobody
    considered. `min` cannot inherit anything. A tool classified NONE by mistake does not
    raise a SHADOW pin to AUTONOMOUS; it simply stops tightening.

    Mirrors `brain.gate.injection.autonomy_ceiling`, which composes the same way for the
    same reason. Two rules that must agree eventually disagree, and the one that disagrees
    in the permissive direction is the one nobody notices.
    """
    return min(rung, default_rung(effect))


# ----------------------------------------------------------------- the registration


@dataclass(frozen=True)
class RegisteredTool:
    """One tool that passed every refusal, with what the registry had to add.

    `capability` is a `Capability` and never the string it was parsed from, so no caller
    downstream has to re-parse it and no caller can decide for itself what an unparseable
    one means.

    `scope` is here rather than on `ToolDefinition` because that model is frozen with
    `extra="forbid"` and belongs to `brain.core.envelope`. It is None for a DELEGATED tool,
    where the source enforces its own permissions, and mandatory for a SERVICE one.
    """

    definition: ToolDefinition
    handler: Callable[..., object]
    capability: Capability
    result_contract: ResultContract = ResultContract.TYPED
    scope: Scope | None = None

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def object_name(self) -> str:
        """The entity this tool returns. `sweep_slug_collisions` calls this a tool object."""
        return self.definition.entity

    @property
    def source(self) -> str:
        """The system the call goes to, taken from the name so it is always present."""
        return self.definition.name.split(".", 1)[0]


@dataclass
class ToolRegistry:
    """Every tool that may be called, and the door each one came through.

    An instance rather than a module-level singleton, for the reason
    `brain.core.redaction.ChannelAdapterRegistry` gives about its own: a singleton is
    process state in a layer that otherwise holds none, one test's registration would be
    visible to the next, and "which tools exist" would depend on import order in exactly the
    way the duplicate-name rule exists to prevent.

    Iterating a registry yields `ToolDefinition`s in name order, which is what
    `brain.gate.catalogue.project` takes. The registry therefore feeds the projector without
    either of them importing the other, and there is no second path by which a raw list of
    tools could reach a connector to be filtered there.
    """

    _tools: dict[str, RegisteredTool] = field(default_factory=dict)
    _frozen: bool = False

    # ------------------------------------------------------------------ registering
    def register(
        self,
        definition: ToolDefinition,
        handler: Callable[..., object],
        *,
        result_contract: ResultContract = ResultContract.TYPED,
        scope: Scope | None = None,
    ) -> RegisteredTool:
        """Check a tool against every rule in this module, then record it.

        The handler is required and not optional. A definition registered without one could
        not be checked against `assert_tool_returns_typed_result`, so "registration refuses a
        tool that cannot be redacted" would hold for every tool except the ones somebody was
        in a hurry about. Rejected for the same reason: registering a definition now and
        attaching the handler later, which makes the check a step rather than a door.

        `UntypedShapeError` is allowed to propagate rather than being wrapped. It is the
        redaction contract's own refusal, documented where the contract lives, and wrapping
        it here would make a rule of the redactor look like an opinion of the registry, which
        is a thing a registry can be replaced without.
        """
        if self._frozen:
            msg = (
                f"tool {definition.name!r} was registered after the registry was frozen; a "
                "tool that appears once a catalogue has already been projected is one whose "
                "presence depends on when the request arrived"
            )
            raise ToolRegistrationError(msg)

        assert_tool_name(definition.name)

        existing = self._tools.get(definition.name)
        if existing is not None:
            # Refused outright, including for an identical re-registration.
            # `ChannelAdapterRegistry` permits re-registering the same object, because an
            # adapter is a function and a module can be imported under two names. A registry
            # is an instance built by one owner, so a second registration of a name is a bug
            # in that owner's wiring, and "identical" is a judgement about handler identity
            # that a decorator quietly breaks.
            msg = (
                f"two tools are registered as {definition.name!r}; which of them a caller "
                "reaches would be decided by import order, and the loser is invisible rather "
                "than broken"
            )
            raise ToolRegistrationError(msg)

        assert_object_name(definition)
        assert_source_agrees(definition)
        assert_object_not_reserved(definition)
        capability = capability_for(definition)
        assert_effect_matches_capability(definition, capability)
        assert_service_tool_is_scoped(definition, scope)
        assert_result_contract(definition, capability, result_contract)
        # Last, because it is the only check that inspects something other than the
        # declaration, and its message is about the function rather than the tool.
        assert_tool_returns_typed_result(handler)

        registered = RegisteredTool(
            definition=definition,
            handler=handler,
            capability=capability,
            result_contract=result_contract,
            scope=scope,
        )
        self._tools[definition.name] = registered
        return registered

    # --------------------------------------------------------------------- reading
    def get(self, name: str) -> RegisteredTool:
        """One tool by name, or a refusal. Never None.

        Returning None would put the decision about a missing tool in every caller, and the
        cheapest thing a caller does with None is skip it.
        """
        found = self._tools.get(name)
        if found is None:
            msg = f"no tool is registered as {name!r}"
            raise ToolRegistrationError(msg)
        return found

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def object_names(self) -> tuple[str, ...]:
        """Every distinct tool object, sorted.

        `brain.ops.sweeps.sweep_slug_collisions` compares tool objects against scope and
        agent slugs by reading `ToolDefinition(...)` literals out of the source text. That
        finds tools written as literals and misses tools built at run time from a connector's
        manifest, which is how most of them will arrive. This is the same list computed from
        what is actually registered, so the sweep has something to read once it can.
        """
        return tuple(sorted({t.definition.entity for t in self._tools.values()}))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """What `brain.gate.catalogue.project` consumes, in a stable order.

        Sorted here as well as in `project`, so that anything else reading a registry gets
        the same order and two identical builds produce identical catalogues.
        """
        return tuple(self._tools[name].definition for name in sorted(self._tools))

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self.definitions())

    def __len__(self) -> int:
        return len(self._tools)

    # ------------------------------------------------------- startup check (M12.1.5)
    def validate(self) -> tuple[str, ...]:
        """Everything that can only be checked once the whole registry is present.

        Today that is one rule: two tools may not carry the same description. Names are
        already unique, and the description is the other half of what the model chooses
        from, so two tools described identically are chosen between by position in a list.
        That is the same failure as a duplicate name arriving one layer later, and it cannot
        be caught at `register` time because it is a property of a pair.

        Compared on a folded form, so "Reads a client" and "reads a client." collide.
        Two descriptions only a machine can tell apart are one description to the reader
        that matters, which is a model.
        """
        by_description: dict[str, list[str]] = {}
        for name in sorted(self._tools):
            text = self._tools[name].definition.description
            folded = " ".join(text.lower().split()).rstrip(".")
            by_description.setdefault(folded, []).append(name)
        return tuple(
            f"tools {names} share the description {folded!r}; the model chooses between "
            "them by position rather than by meaning"
            for folded, names in sorted(by_description.items())
            if len(names) > 1
        )

    def freeze(self) -> Self:
        """Run the whole-registry checks and refuse further registration (M12.1.5).

        Called at startup, and it raises rather than logging. A registry that starts with a
        finding is a registry somebody has to notice; a warning at boot is a warning nobody
        reads after the first week.

        Every finding is reported at once rather than the first one. Whoever is fixing this
        is looking at a build, and a build that reveals one problem per run is a build that
        takes an afternoon.
        """
        findings = self.validate()
        if findings:
            msg = "the tool registry cannot be frozen:\n  " + "\n  ".join(findings)
            raise ToolRegistrationError(msg)
        self._frozen = True
        return self

    @property
    def is_frozen(self) -> bool:
        return self._frozen
