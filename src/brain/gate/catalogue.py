"""Which tools a model is shown, and why the list is computed rather than configured.

This is where "an agent is a lens, never a principal" stops being a slogan. An agent has a
ceiling, not a permission set: the catalogue it works from is what the caller already holds,
intersected with what the agent is allowed to reach for. Nothing an agent does can exceed
its caller, because the tools that would exceed them are not in the list at all.

**Unreachable tools are absent, not refused.** A tool described to a model and then refused
teaches the model that the capability exists, and the model will say so: "I tried to look up
the contract value but was not permitted". That sentence is the leak, and it arrives through
the one channel nobody audits, which is the model's own explanation of what it just did.

**The projection happens here and nowhere else.** A connector is never handed the full
catalogue and asked to filter it. It is a remote system we do not control, and a filter it
performs is a filter we cannot prove happened.

Task ids: M3.7.1, M3.7.2, M3.7.3, M3.7.4
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import SideEffect, ToolDefinition
from brain.core.errors import BrainError, Outcome


class EmptyCatalogueError(BrainError):
    """A manifest declared required tools and none of them resolved.

    M3.7.4. This is deliberately an error rather than an empty list. An agent handed no
    tools does not stop: it answers from what it already believes, confidently and without
    citations, and the trace shows a clean run. An agent whose required tools are missing
    is misconfigured, and the honest outcome is to say so rather than to produce something
    that reads like an answer.
    """

    outcome = Outcome.FAILED
    public_message = "That assistant is not set up correctly. Someone has been told."


@dataclass(frozen=True)
class AgentCeiling:
    """What an agent may reach for, at most.

    A ceiling, never a grant. The names are tool names rather than capabilities because an
    agent is configured in terms of what it does, and the capability each tool needs is a
    property of the tool rather than of the agent.

    `required` is the subset without which the agent cannot function. A reporting agent
    that cannot read clients has nothing to report on, and that is the empty-catalogue case.
    """

    agent_id: str
    allowed_tools: frozenset[str]
    required_tools: frozenset[str] = frozenset()
    #: The largest side effect this agent may have, whatever its tools could do. Separate
    #: from the leash, which decides supervision; this decides reach.
    max_side_effect: SideEffect = SideEffect.NONE

    def __post_init__(self) -> None:
        missing = self.required_tools - self.allowed_tools
        if missing:
            # A required tool outside the allowed set can never resolve, so the agent is
            # permanently broken and the manifest is wrong. Better to refuse it at
            # configuration time than to fail every request at run time.
            raise ValueError(
                f"agent {self.agent_id} requires tools it is not allowed: {sorted(missing)}"
            )


#: Ordered by how much damage a mistake does, so a ceiling can be compared rather than
#: matched. SideEffect itself is a StrEnum and carries no order.
SIDE_EFFECT_ORDER: tuple[SideEffect, ...] = (
    SideEffect.NONE,
    SideEffect.DRAFT,
    SideEffect.WRITE,
    SideEffect.SEND,
    SideEffect.MONEY,
)


def _rank(effect: SideEffect) -> int:
    return SIDE_EFFECT_ORDER.index(effect)


#: Only `project` holds this, so only `project` can build a ProjectedCatalogue. See the
#: class docstring for why that is the enforcement mechanism rather than a convention.
_PROJECTION_TOKEN = object()


@dataclass(frozen=True)
class ProjectedCatalogue:
    """The tools this caller, through this agent, may actually use.

    **This type cannot be constructed outside `project`**, and that is M3.7.3. The failure
    it prevents is not malice but a shortcut: handing a raw tool list to a connector or an
    SDK and relying on that to filter. A remote system's filtering is a filter we cannot
    prove ran, and the first sign it did not would be an answer nobody can explain.

    Making the type unconstructable is worth the small ugliness of a token because the
    alternatives do not work. Checking that the shown list is smaller than the registry
    falsely accuses a Super Admin, who legitimately holds everything. Checking a flag
    trusts whoever set it. A dispatcher that accepts only this type, and a type only the
    projector can make, is a guarantee rather than a habit.

    `static` and `caller_specific` are separated for prompt caching, not for tidiness. A
    provider caches on a prefix, so a catalogue whose every entry varies per person never
    hits the cache and every request pays full price for the same preamble. The tools
    everyone can reach are stable across callers and go first; the rest follow.

    They are one list to the model. The split is only about where the cache boundary falls.
    """

    static: tuple[ToolDefinition, ...]
    caller_specific: tuple[ToolDefinition, ...]
    #: Not data. The constructor guard, and the reason a catalogue has one origin.
    token: object = None

    def __post_init__(self) -> None:
        if self.token is not _PROJECTION_TOKEN:
            raise EmptyCatalogueError(
                "a catalogue may only be built by brain.gate.catalogue.project; "
                "projection happens in our dispatcher and is never delegated"
            )

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return self.static + self.caller_specific

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools)

    def cache_prefix_length(self) -> int:
        """How many leading tools are identical for every caller.

        Reported so a trace can show why a request did or did not hit the provider cache.
        A number nobody can see is a number nobody will notice going to zero.
        """
        return len(self.static)


def _admits(entitlement: EntitlementSet, tool: ToolDefinition, now: datetime | None) -> bool:
    """Whether the caller holds what this tool needs.

    A malformed `required_capability` means the tool is unreachable rather than
    unrestricted. The alternative, treating an unparseable capability as "no requirement",
    turns a typo in a manifest into an open door.
    """
    try:
        needed = Capability(value=tool.required_capability)
    except ValueError:
        return False
    return entitlement.scope_for(needed, now) is not None


def project(
    registry: Iterable[ToolDefinition],
    entitlement: EntitlementSet,
    ceiling: AgentCeiling,
    *,
    now: datetime | None = None,
    universal: frozenset[str] = frozenset(),
) -> ProjectedCatalogue:
    """`tools_run = tools(caller) ∩ agent_ceiling`, and nothing else.

    `universal` names the tools every principal in the company holds, which is what makes a
    stable cache prefix possible at all. It is an optimisation hint and never an authority:
    a tool named here still has to pass the entitlement check like any other, so a mistake
    in the list costs cache hits rather than permissions.
    """
    static: list[ToolDefinition] = []
    specific: list[ToolDefinition] = []
    resolved: set[str] = set()

    # Sorted so two identical requests produce byte-identical catalogues. An unstable
    # ordering would defeat prompt caching just as thoroughly as a varying membership, and
    # would do it invisibly, because the list would still look right.
    for tool in sorted(registry, key=lambda t: t.name):
        if tool.name not in ceiling.allowed_tools:
            continue
        if _rank(tool.side_effect) > _rank(ceiling.max_side_effect):
            continue
        if not _admits(entitlement, tool, now):
            continue
        resolved.add(tool.name)
        (static if tool.name in universal else specific).append(tool)

    missing_required = ceiling.required_tools - resolved
    if missing_required:
        raise EmptyCatalogueError(
            f"agent {ceiling.agent_id} requires {sorted(missing_required)}, "
            "none of which resolved for this caller"
        )

    return ProjectedCatalogue(
        static=tuple(static), caller_specific=tuple(specific), token=_PROJECTION_TOKEN
    )
