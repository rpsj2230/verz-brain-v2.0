"""An agent as a record: who may see it, what it may reach, and why those are two things.

An agent is a lens. `E_run(caller, agent) = E(caller) ∩ agent_ceiling` is computed by
`brain.gate.invoke.invoke`, which calls `EntitlementSet.intersect`, and nothing in this
module recomputes it. What is here is the record that intersection is made from, and the
two producers that hand it to the gate in the shapes the gate already takes.

**Audience and authority are different axes and they live in different objects.** Audience
is who may see and start an agent: `AgentAudience`, a visibility level plus the identifiers
that level needs. Authority is what a run may reach: `AgentAuthority`, a scope, the
capabilities the ceiling admits, the tools it may ask for and the largest side effect it may
have. `audience_scope` is never handed an `AgentAuthority` and neither ceiling producer is
ever handed an `AgentAudience`, so the separation is readable in the signatures rather than
promised in a comment.

Conflating the two is how a permission-aware system develops a hole, and it can be done from
either end. An agent published to everybody would come to reach everything, which turns a
visibility change made in a console into a grant nobody wrote. An agent given a wide ceiling
so that it can do its job would become visible to everybody, which is how a finance
assistant appears in the picker of all 126 staff. Both readings are natural, both are wrong,
and **a test that checks one direction is satisfied by code that ties them together in the
other**. `AUDIENCE_IS_NOT_AUTHORITY` states the rule and the property is asserted over the
cross product of levels and ceilings, so neither direction is the untested one.

**The visibility levels are `brain.knowledge.visibility`'s, not a second set.** Same three
members, same `scope_for`, same refusal when the identifier a level needs is missing. The
work breakdown calls the widest level global and the enum calls it `COMPANY`: one level,
two words, and a second enum would be a second vocabulary for one column, which is how a
rename becomes a data migration. Reusing `scope_for` inherits the refusal that matters most
here, that a personal level built with no owner is `Scope()`, which is the unrestricted
scope, so the narrowest level in the system would turn into the widest through a blank
field on a form.

**The audience predicate is matched against the viewer, never against a row of business
data.** `scope_for(PERSONAL, owner_id=...)` yields `owner_id = <the steward>`, and the row
it is tested against is a description of the person asking. That is why one predicate
builder serves both this module and the knowledge layer: the shape of "who does this reach"
is the same question in both places, and the thing being narrowed is what differs.

**Nothing here counts what it hid.** `visible_agent_ids` returns a frozenset of ids, which
has no field a total could be put in later, and no function in this module is given both a
listing and the records it dropped. An agent outside a person's audience and an agent that
was never created produce the same answer, which is the same rule
`brain.core.department.plan_cross_department` keeps for departments.

Four designs were rejected.

*An `is_global` boolean.* It cannot express the department level at all, which is the level
almost every agent actually wants, so every agent would be either one person's or everyone's.
That is the same argument `brain.knowledge.visibility` makes about `is_public`.

*A second `AgentVisibility` enum with a `GLOBAL` member.* Two vocabularies for one column,
and the promotion rules already written for knowledge would not apply to it.

*An audience `Scope` column beside the ceiling scope.* Two scope columns in one row, both
holding `Scope.model_dump()`, indistinguishable in a `psql` session and one copy-paste apart
from the audience becoming the authority. The audience is stored as a level plus the
identifiers it needs, and its predicate is computed, so there is nothing to copy.

*Deriving the audience from the ceiling*, on the reading that an agent which can reach
everything is meant for everyone. That is exactly the conflation this module exists to
refuse, and it is attractive because it removes a column.

**An empty capability ceiling reaches nothing, deliberately.** `EntitlementSet.intersect`
keeps a caller's grant only where the ceiling covers its capability, so an agent that
declares none narrows its caller to nothing and `brain.gate.invoke.invoke` refuses the run
rather than starting one with no tools. A new agent therefore does nothing until somebody
says what it may reach, which is the direction a default has to fail in.

**What consults this, and what does not yet.** `runnable_agent_ids` produces exactly the
`visible_agents` argument `brain.gate.select.select_agent` requires, and it is the only
supported source for it: selection skips an agent the caller cannot see, silently, and it
can only do that if somebody computed the set from the audience. `tool_ceiling` produces the
`ceiling` argument `brain.gate.invoke.invoke` and `brain.gate.catalogue.project` require,
and `entitlement_ceiling` produces the argument `EntitlementSet.intersect` takes. Those are
the three seams and the tests drive the real functions through them rather than a stand-in.

No HTTP route calls any of it today, and the reason is not this leaf: there is no route
behind the gate in this repository at all, and `select_agent` has had no caller in `src`
since it was written. Inventing a request pipeline here to give these a caller would be a
second pipeline for the real one to be reconciled with later, which is the shape
`brain.ops.automation_piece` refused for the same reason.

Task ids: M13.1.1, M13.1.2, M13.1.3
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.department import SLUG_PATTERN
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import SideEffect
from brain.core.scope import Scope
from brain.gate.catalogue import AgentCeiling
from brain.knowledge.visibility import Visibility, scope_for
from brain.models.routing import DEFAULT_TIER, TIER_LADDER, Tier

# ------------------------------------------------------------------ written-down reasons

#: The rule this module exists to keep, stated where a reader meets it.
AUDIENCE_IS_NOT_AUTHORITY: Final = (
    "Audience is who may see and start an agent. Authority is what a run through it may "
    "reach. They are separate axes and neither reads the other: widening the audience must "
    "not widen the ceiling, and widening the ceiling must not widen the audience. A "
    "globally visible agent sees no more than a personal one with the same ceiling, and an "
    "agent whose ceiling admits everything is visible to nobody who was not given it. "
    "Tying them together in either direction is a permission hole that reads as a "
    "convenience, and the direction nobody tested is the one it happens in."
)

#: Why a listing says nothing about what it left out.
A_HIDDEN_AGENT_AND_A_MISSING_AGENT_ARE_ONE_ANSWER: Final = (
    "An agent outside somebody's audience and an agent that does not exist produce the "
    "same answer: absence. No listing here reports how many it withheld, directly or by "
    "subtraction, because a count of hidden agents is a count of teams, projects and "
    "clients the reader was not told about. The listings return a set of ids, which has "
    "nowhere to put a total."
)

#: Why the ceiling is expressed as an `EntitlementSet` without an agent becoming a principal.
AN_AGENT_CEILING_IS_NOT_A_PRINCIPAL: Final = (
    "The ceiling is carried in the same type a principal's reach is carried in, because "
    "EntitlementSet.intersect is the one implementation of the platform's invariant and "
    "that is the type it takes. It confers nothing: intersect keeps the caller's own "
    "principal id, so a run through an agent is still the caller's run, and the ceiling's "
    "id is prefixed so that a set which is a ceiling can never be read in a trace as a "
    "person."
)

#: `EntitlementSet.principal_id` on a ceiling, so a ceiling is never mistaken for a person.
#: `brain.core.entitlement` puts no grammar on that field, and a bare agent slug sitting in
#: it would read in a trace exactly like a principal id.
CEILING_PRINCIPAL_PREFIX: Final = "agent:"

#: A persona is a paragraph or two of instruction, not a document. Long enough for the
#: register, the refusals and the house voice; short enough that a manifest is reviewable.
PERSONA_CHARS: Final = 2000

#: The human label, as `ScopeRecord.label` and `Department.name` are bounded.
DISPLAY_NAME_CHARS: Final = 120

#: `Principal.id` is `Field(max_length=128)`, and an owner is one.
OWNER_ID_CHARS: Final = 128

#: `Department.slug` is `Field(max_length=60)`.
DEPARTMENT_CHARS: Final = 60

#: An agent slug, bounded like every other slug in the shared namespace.
AGENT_ID_CHARS: Final = 60


class AgentError(Exception):
    """A refusal to write or move an agent record.

    Outside the `brain.core.errors` taxonomy, like `DepartmentError` and `VisibilityError`
    and for the same reason: those five outcomes describe an answer given to a person, and
    this describes a refusal to store something. Nobody asking a question ever sees it.
    """


class AgentState(enum.StrEnum):
    """Whether an agent may be selected, may be brought back, or is finished.

    Derived from two timestamps rather than stored, so there is one fact and not two that
    can disagree. See `AgentRecord.state` for the precedence between them.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    ARCHIVED = "archived"


# --------------------------------------------------------------------------- the two axes
class AgentAudience(BaseModel):
    """Who may see and start this agent. Nothing about what it may reach.

    A level and the identifiers that level needs, which is `KnowledgeVisibility`'s shape
    applied to an agent rather than to a document. The predicate is recomputed by
    `audience_scope` rather than stored, so it cannot disagree with the level.

    `owner_id` is the current steward and it is part of the audience because at the personal
    level the steward *is* the audience. Ownership transfer therefore changes who can see a
    personal agent, which is the point of transferring it, and changes nothing about what it
    can reach.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Visibility
    owner_id: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    #: Empty except at the department level. A department recorded on a personal or company
    #: agent is a field that reads as an audience and is not one, and the first query to
    #: filter on it would be filtering on a fact nobody set deliberately.
    department: str = Field(default="", max_length=DEPARTMENT_CHARS)

    def model_post_init(self, _context: object, /) -> None:
        if self.level is Visibility.DEPARTMENT and not self.department:
            msg = (
                "a department audience needs a department; without one the level's "
                "predicate is the unrestricted scope, which is the widest audience "
                "wearing the name of the middle one"
            )
            raise ValueError(msg)
        if self.level is not Visibility.DEPARTMENT and self.department:
            msg = (
                f"{self.level} carries no department, and {self.department!r} on this row "
                "would read as an audience that nothing applies"
            )
            raise ValueError(msg)


class AgentAuthority(BaseModel):
    """What a run through this agent may reach, at most. Nothing about who may see it.

    A ceiling in four parts, each of which narrows something the caller already holds:
    `scope` narrows rows, `capabilities` narrows what the intersection keeps, `allowed_tools`
    narrows the catalogue, and `max_side_effect` narrows what any of it may do.

    `scope` defaults to the unrestricted scope, and that is not a company-wide grant. It
    means this agent narrows no rows of its own, so a run reaches whatever its caller
    reaches and no more, because `E_run` is an intersection with the caller's set. The
    dangerous default would be the other way round: a capability list defaulting to
    everything would hand every new agent the caller's whole reach.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope = Field(default_factory=Scope)
    capabilities: tuple[Capability, ...] = ()
    allowed_tools: frozenset[str] = frozenset()
    #: The subset without which this agent cannot do its job. `brain.gate.catalogue.project`
    #: raises rather than handing a model a catalogue missing one of these, which is right
    #: for an agent somebody configured and is deliberately not what a canvas flow gets.
    required_tools: frozenset[str] = frozenset()
    max_side_effect: SideEffect = SideEffect.NONE


@dataclass(frozen=True)
class AgentViewer:
    """The person a visibility question is asked about.

    Departments are a set because membership is: `brain.core.department.membership_scope`
    exists precisely because somebody can sit in Sales and Web at once, and an audience test
    that took one department would hide every agent belonging to the other one.

    This carries no entitlement and no capability, and that absence is the audience half of
    `AUDIENCE_IS_NOT_AUTHORITY`. A viewer's reach cannot make an agent visible, so there is
    nowhere here to put it.
    """

    principal_id: str
    departments: frozenset[str] = field(default_factory=frozenset)


# ------------------------------------------------------------------------------ the record
class AgentRecord(BaseModel):
    """One row of the agent table: persona, scope, model tier and visibility level (M13.1.1).

    Frozen. Every change to an agent returns a new record, so both sides of a change are
    holdable and a caller cannot mutate the thing a decision was made from half way through.

    The lifecycle is two nullable timestamps rather than a state column. A column plus the
    timestamps would be two facts that can disagree; a column alone loses the *when*, and
    "when did this stop answering, and did somebody archive it or disable it" is the
    question asked after an agent goes quiet, never before.

    `created_by` is history and never moves. `audience.owner_id` is the current steward and
    does. Keeping them apart is what makes M13.1.5 expressible: after a transfer the record
    still says who built the thing, which is the question an audit asks, and says who
    answers for it now, which is the question everybody else asks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One namespace with scopes and tool objects, checked by
    #: `brain.core.department.check_slug_collisions`. The pattern is imported rather than
    #: restated so an agent slug cannot be legal here and illegal there.
    agent_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_CHARS)
    #: The instruction that makes this agent this agent. Stored as text and never parsed
    #: here: it is prompt material, and anything in it that could decide a permission would
    #: be a permission decided by whoever last edited a text box.
    persona: str = Field(min_length=1, max_length=PERSONA_CHARS)
    #: The model pool this agent's runs are routed to, as `brain.models.routing` spells it.
    #: The enum rather than a string, so a tier this record can hold is one the router knows;
    #: a free string would let an agent name a pool that classifies to nothing and answers
    #: from whatever the default happens to be that month.
    tier: Tier = DEFAULT_TIER
    audience: AgentAudience
    authority: AgentAuthority
    created_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    #: Reversible. Set by `brain.agents.lifecycle.disable`, cleared by `enable`.
    disabled_at: datetime | None = None
    #: Terminal. See `ARCHIVE_IS_TERMINAL` in `brain.agents.lifecycle`.
    archived_at: datetime | None = None

    @field_validator("tier")
    @classmethod
    def _a_tier_the_router_can_route(cls, v: Tier) -> Tier:
        """`Tier.NONE` is the absence of the ladder, not a rung on it.

        `RoutingChain.select` returns an empty selection for it, so an agent pinned there
        would be configured to answer nothing at all, and the failure arrives as an agent
        that is selected, starts, and produces no answer. Checked against `TIER_LADDER`
        rather than against a list written here, so a fourth pool is admitted the day the
        router gains one.
        """
        if v not in TIER_LADDER:
            msg = (
                f"{v} is not a rung on the routing ladder; an agent pinned to it reaches no "
                "model and answers nothing"
            )
            raise ValueError(msg)
        return v

    @field_validator("disabled_at", "archived_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        """A naive lifecycle timestamp is a silent bug, as it is on `Principal.not_after`.

        Compared against a `now` in UTC by anything that reports on the estate, a naive
        timestamp is hours out in whichever direction the host happens to sit, and neither
        direction announces itself.
        """
        if v is not None and v.tzinfo is None:
            msg = "a lifecycle timestamp must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        """Refuse a record whose required tools are outside its allowed set.

        The check is made by building the `AgentCeiling`, which owns the rule, rather than
        by restating it. A restatement is a second copy that agrees today: `project` would
        go on raising `EmptyCatalogueError` for an agent that can never resolve, and the
        misconfiguration would be discovered by every request instead of by the person
        saving the record.
        """
        tool_ceiling(self)

    @property
    def state(self) -> AgentState:
        """Archived beats disabled beats enabled.

        The precedence is stated rather than implied because both columns can be set at
        once, legitimately: archiving something that was already disabled leaves the earlier
        timestamp in place, and that record is worth more than a tidier row.
        """
        if self.archived_at is not None:
            return AgentState.ARCHIVED
        if self.disabled_at is not None:
            return AgentState.DISABLED
        return AgentState.ENABLED

    @property
    def is_selectable(self) -> bool:
        """True when this agent may answer. Lifecycle only; says nothing about who sees it."""
        return self.state is AgentState.ENABLED


# ---------------------------------------------------------------- audience, and only audience
def audience_scope(audience: AgentAudience) -> Scope:
    """The predicate a level means, built by `brain.knowledge.visibility.scope_for` (M13.1.2).

    Takes an `AgentAudience` and nothing else. There is no parameter here that could carry a
    ceiling, a capability or an entitlement, which is the structural half of
    `AUDIENCE_IS_NOT_AUTHORITY`: this function could not consult authority if it wanted to.
    """
    return scope_for(audience.level, owner_id=audience.owner_id, department=audience.department)


def _viewer_rows(viewer: AgentViewer) -> tuple[dict[str, str], ...]:
    """The viewer, as the rows an audience predicate is tested against.

    One row without a department and one per department they sit in. The bare row is what
    makes a personal or company audience answerable for somebody with no department at all,
    and a `Clause` refuses a row whose field is absent, so it can never satisfy a department
    predicate by omission.
    """
    base = {"owner_id": viewer.principal_id}
    return (base, *({**base, "department": d} for d in sorted(viewer.departments)))


def visible_to(audience: AgentAudience, viewer: AgentViewer) -> bool:
    """Whether this person may see and start this agent (M13.1.2).

    Any of the viewer's rows satisfying the predicate is enough, because a person in two
    departments is one person: requiring every row to match would hide a Web agent from
    somebody who is also in Sales, which is the failure that gets read as the agent being
    broken.
    """
    scope = audience_scope(audience)
    return any(scope.matches(row) for row in _viewer_rows(viewer))


def visible_agent_ids(records: Iterable[AgentRecord], viewer: AgentViewer) -> frozenset[str]:
    """The ids this person may see. Audience only, whatever state the agents are in.

    A set of ids and nothing else. There is no second return value holding what was dropped
    and no count of it, per `A_HIDDEN_AGENT_AND_A_MISSING_AGENT_ARE_ONE_ANSWER`, and a
    frozenset has nowhere for one to be added later without somebody changing the signature.
    """
    return frozenset(r.agent_id for r in records if visible_to(r.audience, viewer))


def runnable_agent_ids(records: Iterable[AgentRecord], viewer: AgentViewer) -> frozenset[str]:
    """The ids `brain.gate.select.select_agent` may choose between: visible and enabled.

    Two axes are conjoined here and neither is authority. Selection has to see the
    conjunction rather than the audience alone, because a disabled agent that can still be
    selected is a disabled agent that answers, and the person who disabled it would have no
    way to tell.

    A disabled agent is absent from this set for the same reason an unreachable one is: the
    caller is told nothing about why an agent they may have used yesterday is not chosen
    today. That belongs in the trace, which `select_agent` writes, and not in an answer.
    """
    return frozenset(
        r.agent_id for r in records if r.is_selectable and visible_to(r.audience, viewer)
    )


# -------------------------------------------------------------- authority, and only authority
def tool_ceiling(record: AgentRecord) -> AgentCeiling:
    """The tool ceiling, in the type `brain.gate.catalogue.project` already takes.

    Takes the record because it needs the agent id, and reads only `authority` off it. The
    audience is on the same object and is not consulted, which no signature can prove; what
    can be proved is the behaviour, and the cross-product test in
    `tests/unit/test_agent_model.py` proves it by varying the level and comparing the
    ceilings this returns.
    """
    return AgentCeiling(
        agent_id=record.agent_id,
        allowed_tools=record.authority.allowed_tools,
        required_tools=record.authority.required_tools,
        max_side_effect=record.authority.max_side_effect,
    )


def entitlement_ceiling(record: AgentRecord) -> EntitlementSet:
    """The capability ceiling, in the type `EntitlementSet.intersect` takes.

    **This is not an entitlement and confers nothing.** It is the right-hand side of the
    platform's one intersection, and `intersect` keeps the caller's principal id, so a run
    through an agent is still the caller's run. The id here is prefixed so that a ceiling
    set which reaches a trace or a log cannot be read as a person. See
    `AN_AGENT_CEILING_IS_NOT_A_PRINCIPAL`.

    Every capability is bound to the agent's own scope, so the ceiling narrows on both axes
    at once: a caller holding `read:client.name` company-wide, through an agent whose scope
    is the Web department, comes out holding it in Web.

    No `not_after`. An agent does not expire; it is disabled or archived, and both of those
    are decisions with a person behind them rather than a clock. A time-boxed ceiling would
    be a fourth lifecycle state that nothing lists and nobody is told about.
    """
    return EntitlementSet(
        principal_id=f"{CEILING_PRINCIPAL_PREFIX}{record.agent_id}",
        grants=tuple(
            Grant(capability=capability, scope=record.authority.scope)
            for capability in record.authority.capabilities
        ),
    )
