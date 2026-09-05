"""Whether an agent simulates an action, shows it to a person, or simply does it.

Catalogue projection has already decided what an agent can *reach*: a tool the caller
cannot use is absent, so there is nothing to argue toward. This module decides the separate
question of what an agent may do with that reach when nobody is watching.

**What breaks without it.** The moment an agent holds one tool with a side effect, it
exercises that tool at full autonomy on every target, because nothing else in the gate
distinguishes reading a ticket from emailing a client about one. Projection is about reach
and says nothing about supervision. The two are genuinely different questions, and a system
that answers only the first is a system whose first real mistake is unrecoverable.

Five properties hold this together.

**The rung is per (agent, target, scope), never per agent.** An agent trusted to update a
ticket status is not thereby trusted to update an invoice, and one trusted in maintenance is
not trusted in finance. `Leash` therefore has no per-agent default field at all: a fallback
of that shape would let trust earned on cheap targets spend itself on expensive ones, and
the widening would be invisible because it would look like configuration working.

**A missing entry means SHADOW.** Fail closed. An agent acting on a target nobody has
configured is precisely the case where nobody has thought about it, and the honest answer
for an unconsidered case is to simulate it and show somebody.

**Rungs compose by intersection, like every other ceiling here.** Where several entries
match, the strictest wins. The rejected alternative was most-specific-wins, which is what
most policy engines do; it was rejected for the reason `brain.core.entitlement` gives for
having no deny clause. Once specificity decides, "what may this agent do" stops being a
lookup and becomes an evaluation-order problem, and a company-wide SHADOW pin can be
quietly cancelled by adding a narrower row. The cost is real and accepted: an admin cannot
write "SHADOW everywhere except maintenance". They write the entries they mean, and every
entry only ever subtracts.

**The effective tier is the leash intersected with the risk ceiling**, and that intersection
is `brain.gate.injection.autonomy_ceiling`, reused rather than restated. Two rules that must
agree eventually disagree, and the one that disagrees in the permissive direction is the one
nobody notices.

**Shadow is indistinguishable to the agent and obvious to the person.** An agent that can
tell it is being simulated is an agent whose simulated behaviour proves nothing, so what
comes back from a shadow run is the same type carrying the same shape as a real one, and
`TypedResult` has nowhere to say otherwise. The person gets a `Notice` that says so plainly.

Scope: this is policy, not a runtime. M3.8.1, the agent loop itself, does not exist yet, so
simulation and execution arrive as callables. Nothing here opens a connection, reads a
table or calls a model.

Task ids: M3.8.2, M3.8.3, M3.8.4, M3.8.5, M3.8.6
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Self, assert_never
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import Entity, ToolDefinition, TypedResult
from brain.core.field_policy import FieldPolicy
from brain.core.redaction import compute_mask
from brain.core.scope import Scope
from brain.gate.injection import AutonomyTier, RiskAssessment, autonomy_ceiling

# --------------------------------------------------------------------- grammars

#: A reference to an agent or a person. Deliberately the same shape as
#: `brain.audit.ledger.IDENTIFIER`: an id that can be leashed has to be an id that can be
#: audited, and two grammars for the same thing is one grammar somebody writes around.
IDENTIFIER: Final = r"^[A-Za-z0-9_.@-]{1,128}$"

#: What an action is aimed at: an entity (`invoice`) or a tool-shaped name
#: (`ticket.update_status`). Both are admitted because a leash is genuinely written at both
#: granularities, and neither expands into the other. `ticket` does not cover
#: `ticket.update_status`, on purpose: an entity-level entry that silently conferred every
#: operation on it is the same mistake `Capability.covers` refuses for `read:client`.
TARGET: Final = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$"

#: A sha256 hexdigest, as `brain.audit.ledger.DIGEST` defines it.
DIGEST: Final = r"^[0-9a-f]{64}$"

#: Domain separation for every digest this module produces, and a warning attached to it.
#: Changing the covered fields changes every digest ever computed, so an approval raised
#: before the change stops matching after it and cannot be resumed. That is the correct
#: failure, and it is a migration rather than an edit to this line.
DIGEST_SCHEMA: Final = "brain.leash.v1"


# ------------------------------------------------------------------ the leash (M3.8.2)

#: What a lookup returns when nothing matches. Named, so the fail-closed rule is one
#: constant that a test can pin rather than a literal repeated at each return.
MISSING_ENTRY_RUNG: Final = AutonomyTier.SHADOW


class LeashEntry(BaseModel):
    """One rung, for one agent, on one target, within one scope.

    Frozen because a rung is read once per call and an entry that could be mutated between
    two calls in the same run would produce a run nobody can reconstruct afterwards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(pattern=IDENTIFIER)
    target: str = Field(pattern=TARGET, max_length=120)
    #: Where this entry applies, evaluated against the target's own values. A scope rather
    #: than a department string because scopes already compose by conjunction only, so an
    #: entry can never be widened by adding a clause to it.
    scope: Scope
    rung: AutonomyTier


class Leash(BaseModel):
    """Every entry, and the lookup that refuses to guess.

    There is no `default` field and no `default_rung`, and their absence is the design
    rather than an omission. A per-agent default is the widening this whole module exists to
    prevent: it reads as a convenience, it is written once by whoever installs the agent, and
    from then on every target nobody configured inherits the trust earned on the targets
    somebody did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[LeashEntry, ...] = ()

    def matching(
        self, agent_id: str, target: str, row: Mapping[str, str]
    ) -> tuple[LeashEntry, ...]:
        """Every entry that applies to this agent, this target and this row.

        All three must match. Agent and target are compared exactly; the scope is evaluated
        as a predicate over the target's own values, which is what makes "trusted in
        maintenance, not in finance" expressible without a second concept.
        """
        candidate = dict(row)
        return tuple(
            entry
            for entry in self.entries
            if entry.agent_id == agent_id
            and entry.target == target
            and entry.scope.matches(candidate)
        )

    def rung_for(self, agent_id: str, target: str, row: Mapping[str, str]) -> AutonomyTier:
        """The rung configured here, or SHADOW when nothing is.

        `min` rather than "the most specific one" or "the last one loaded". Both of those
        make the answer depend on something other than the entries themselves: the first on
        a specificity metric nobody agrees on, the second on a table's `ORDER BY`.
        """
        found = self.matching(agent_id, target, row)
        if not found:
            return MISSING_ENTRY_RUNG
        return min(entry.rung for entry in found)

    def with_entry(self, entry: LeashEntry) -> Self:
        """A new leash with one more entry. Returns a copy so a proposed change can be
        held beside the live one, which is what a console preview needs."""
        return type(self)(entries=(*self.entries, entry))


# ------------------------------------------------------------------- the action


def _digest(parts: tuple[str, ...]) -> str:
    """Length-prefixed concatenation, then sha256.

    Prefixed for the reason `brain.audit.ledger._digest` gives: joining with a separator
    makes two different actions share a digest the moment any part can contain the
    separator, and an approval that can be satisfied by a different action than the one
    shown is worse than no approval at all.
    """
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class Action(BaseModel):
    """One side effect an agent is about to have. The unit the leash governs.

    `touched_fields` is declared rather than inferred, and every argument key must appear in
    it. Without that rule a tool could write a field it never declared, the mask check below
    would pass because it had nothing to check, and the approval artefact would be rendered
    for a field the person was never shown. Declaring is cheap; inferring from the arguments
    alone would miss the fields a call reads in order to decide what to write.

    Arguments are strings. The artefact renders them and the digest covers them, and both
    want one unambiguous rendering rather than whatever `str()` does to a float this week.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(pattern=IDENTIFIER)
    tool: ToolDefinition
    #: What this call is aimed at. Matched against `LeashEntry.target` exactly.
    target: str = Field(pattern=TARGET, max_length=120)
    #: Fields on the target entity this call reads or writes.
    touched_fields: tuple[str, ...] = ()
    #: The target's own values, for evaluating scope predicates. Names and values as they
    #: stand *before* the call, because that is what a scope was written about.
    row: dict[str, str] = Field(default_factory=dict)
    #: The change. Every key must be in `touched_fields`.
    args: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, _context: object, /) -> None:
        undeclared = sorted(set(self.args) - set(self.touched_fields))
        if undeclared:
            msg = (
                f"arguments {undeclared} are not in touched_fields; an undeclared field is "
                "one the mask check cannot see and the approver is never shown"
            )
            raise ValueError(msg)

    def digest(self) -> str:
        """A digest over everything that decides what happens.

        This is what binds an approval to the action it was granted for. Anything that could
        change the outcome is covered: a resume that recomputed a different digest is a
        resume of a different action, whatever the artefact still says.
        """
        parts: list[str] = [
            DIGEST_SCHEMA,
            self.agent_id,
            self.tool.name,
            self.tool.required_capability,
            self.tool.side_effect.value,
            self.tool.identity_mode.value,
            self.tool.entity,
            self.target,
            *sorted(self.touched_fields),
        ]
        # Sorted, because a dict preserves insertion order and the same action built in two
        # orders must not digest differently. Keys and values are both covered: changing a
        # value without changing a key is exactly the swap this is here to catch.
        for key in sorted(self.row):
            parts.extend(("row", key, self.row[key]))
        for key in sorted(self.args):
            parts.extend(("arg", key, self.args[key]))
        return _digest(tuple(parts))


def render_artefact(action: Action) -> str:
    """The real artefact a person approves.

    It carries values, deliberately, and it is the only thing in this module that does. The
    approver is deciding whether *this* should happen, and an artefact reduced to
    "invoke:xero.create_invoice on invoice" is a request nobody can judge; the same argument
    `brain.core.redaction.OwnerNotice` makes about showing an access request in the asker's
    own words. `ActionRecord` is the values-free half, and it is the half that is retained.
    """
    lines = [
        f"{action.tool.name} on {action.target}",
        f"agent: {action.agent_id}",
        f"effect: {action.tool.side_effect.value}",
    ]
    lines.extend(f"  {key}: {action.args[key]}" for key in sorted(action.args))
    return "\n".join(lines)


# ------------------------------------------------------- the three checks (M3.8.6)


class CheckName(enum.StrEnum):
    """The three questions asked before every call, in the order they are asked."""

    #: Does the caller hold the capability, in a scope that admits this row?
    CAPABILITY = "capability"
    #: Is this agent allowed to act unsupervised here?
    RUNG = "rung"
    #: Is the field even visible to the caller?
    MASK = "mask"


#: The order, written out rather than relying on enum declaration order, for the reason
#: `brain.core.field_policy.CLASSIFICATION_ORDER` gives: declaration order is not part of an
#: enum's contract, and a reordering during a merge would silently change the sequence.
#:
#: The order is meaning. Capability asks about the *caller*, rung about the *agent*, mask
#: about the *field*, and each is meaningless without the one before it: there is nothing to
#: ask about supervision on behalf of somebody who may not act at all.
CHECK_ORDER: Final[tuple[CheckName, ...]] = (
    CheckName.CAPABILITY,
    CheckName.RUNG,
    CheckName.MASK,
)


class CheckReason(enum.StrEnum):
    """Why a check came out as it did. A closed vocabulary, never prose.

    Closed for the reason `brain.core.redaction.RedactionReason` is closed: this is recorded,
    and a free-text reason is where somebody eventually writes the value that was refused.
    """

    HELD = "held"
    NO_GRANT = "no grant"
    OUT_OF_SCOPE = "out of scope"
    UNSUPERVISED = "unsupervised"
    SUPERVISED = "supervised"
    VISIBLE = "visible"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class CheckOutcome:
    """One check, and whether it lets the call happen at all."""

    name: CheckName
    #: False stops the call. The rung check never sets this false, and that is deliberate:
    #: a shadow run is the system working, not a refusal. Treating a tight leash as a
    #: refusal would hand an agent errors instead of simulations, so nothing would ever be
    #: learned about what it would have done, which is the entire value of the rung.
    permits: bool
    reason: CheckReason


@dataclass(frozen=True)
class Decision:
    """What the three checks produced: a tier, a verdict, and the evidence for both."""

    checks: tuple[CheckOutcome, ...]
    tier: AutonomyTier
    permitted: bool
    #: The hash of `E(caller) ∩ agent_ceiling`, the reach this decision was computed under.
    ent_hash: str

    @property
    def consulted(self) -> tuple[CheckName, ...]:
        """The checks that actually ran, in order. Always a prefix of `CHECK_ORDER`."""
        return tuple(check.name for check in self.checks)

    @property
    def refused_by(self) -> CheckName | None:
        for check in self.checks:
            if not check.permits:
                return check.name
        return None


def effective_tier(leash: Leash, action: Action, assessment: RiskAssessment) -> AutonomyTier:
    """The rung, intersected with the risk ceiling. One rule, deliberately not two.

    `autonomy_ceiling` is imported rather than reimplemented. A second copy of "the score
    can only tighten" would have to be kept in step with the first forever, and the day they
    diverge, the divergence is discovered by an action happening that should not have.
    """
    return autonomy_ceiling(leash.rung_for(action.agent_id, action.target, action.row), assessment)


def _capability_check(
    action: Action, *, entitlement: EntitlementSet, now: datetime | None
) -> CheckOutcome:
    """Does the caller hold what this tool requires, here?

    Asked of the run entitlement, which is already `E(caller) ∩ agent_ceiling`, so an agent
    can only ever have narrowed it. The scope is evaluated against the target's own row,
    because holding `write:ticket.status` in maintenance is not holding it in finance.
    """
    scope = entitlement.scope_for(Capability(value=action.tool.required_capability), now)
    if scope is None:
        return CheckOutcome(CheckName.CAPABILITY, permits=False, reason=CheckReason.NO_GRANT)
    if not scope.matches(dict(action.row)):
        return CheckOutcome(CheckName.CAPABILITY, permits=False, reason=CheckReason.OUT_OF_SCOPE)
    return CheckOutcome(CheckName.CAPABILITY, permits=True, reason=CheckReason.HELD)


def _rung_check(tier: AutonomyTier) -> CheckOutcome:
    """Is this agent allowed to act unsupervised here? It never refuses; see `CheckOutcome`."""
    unsupervised = tier is AutonomyTier.AUTONOMOUS
    return CheckOutcome(
        CheckName.RUNG,
        permits=True,
        reason=CheckReason.UNSUPERVISED if unsupervised else CheckReason.SUPERVISED,
    )


def _mask_check(
    action: Action,
    *,
    entitlement: EntitlementSet,
    policy: FieldPolicy,
    now: datetime | None,
) -> CheckOutcome:
    """Is every field this call touches visible to the caller?

    An agent that can change a field it cannot see is an agent whose approval artefact is
    blank and whose shadow result discloses the field by the back door. So the mask is
    consulted before the call, not only after it.

    This is a pre-flight and not the last line of defence: `brain.core.redaction` still walks
    whatever comes back, so a tool that under-declares its fields is caught on the way out.
    That is why a call declaring no fields passes here rather than being refused. The place
    to insist that a tool declares its fields is the tool definition, not a check that only
    sees what it was handed.
    """
    mask = compute_mask(
        action.tool.entity,
        action.touched_fields,
        entitlement=entitlement,
        policy=policy,
        row=action.row,
        now=now,
    )
    if set(action.touched_fields) - mask.allowed:
        return CheckOutcome(CheckName.MASK, permits=False, reason=CheckReason.WITHHELD)
    return CheckOutcome(CheckName.MASK, permits=True, reason=CheckReason.VISIBLE)


def decide(
    action: Action,
    *,
    caller: EntitlementSet,
    agent_ceiling: EntitlementSet,
    policy: FieldPolicy,
    leash: Leash,
    assessment: RiskAssessment,
    now: datetime | None = None,
) -> Decision:
    """Run all three checks, in order, and say what may happen (M3.8.6).

    The intersection is computed here rather than trusted from a parameter. A signature
    taking one already-narrowed entitlement would work exactly as well until the day a caller
    passed the caller's own reach and nothing anywhere noticed the agent's ceiling had
    stopped applying. An agent is a lens, so the lens is applied where the decision is made.

    Short-circuiting on the first refusal is fine and skipping a check is not, so the loop
    walks `CHECK_ORDER` and dispatches exhaustively. Adding a fourth check without wiring it
    in is a type error; deleting one changes a constant that two invariant tests pin.
    """
    run = caller.intersect(agent_ceiling)
    outcomes: list[CheckOutcome] = []
    tier = MISSING_ENTRY_RUNG
    permitted = True

    for name in CHECK_ORDER:
        match name:
            case CheckName.CAPABILITY:
                outcome = _capability_check(action, entitlement=run, now=now)
            case CheckName.RUNG:
                # Computed here rather than before the loop, so that a capability refusal
                # short-circuits before the leash is read at all. A refused caller's leash
                # is not a question anybody asked.
                tier = effective_tier(leash, action, assessment)
                outcome = _rung_check(tier)
            case CheckName.MASK:
                outcome = _mask_check(action, entitlement=run, policy=policy, now=now)
            case _:
                assert_never(name)
        outcomes.append(outcome)
        if not outcome.permits:
            permitted = False
            break

    return Decision(
        checks=tuple(outcomes),
        tier=tier,
        permitted=permitted,
        ent_hash=run.ent_hash(),
    )


# ----------------------------------------------------------------- what happens next


class Route(enum.StrEnum):
    """What the gate does with a call, once decided."""

    REFUSED = "refused"
    #: M3.8.3
    SIMULATE = "simulate"
    #: M3.8.4
    SUSPEND = "suspend"
    #: M3.8.5
    EXECUTE = "execute"


def route_for(decision: Decision) -> Route:
    """The one place a tier becomes a behaviour.

    `assert_never` is the point, as it is in `brain.gate.context.traffic_class_for`. A fourth
    autonomy tier cannot reach production without somebody deciding what happens to it, and a
    dictionary with a `.get` default would accept it silently as whatever the default was.
    """
    if not decision.permitted:
        return Route.REFUSED
    match decision.tier:
        case AutonomyTier.SHADOW:
            return Route.SIMULATE
        case AutonomyTier.ASSISTED:
            return Route.SUSPEND
        case AutonomyTier.AUTONOMOUS:
            return Route.EXECUTE
        case _:
            assert_never(decision.tier)


#: What a person is told. Four constants, and the refusal is one string for every reason it
#: could have been refused, for the reason `brain.core.redaction.render_lock` takes no
#: arguments: a message that varies by cause is a side channel that two people can read by
#: comparing screens.
SIMULATED_LABEL: Final = "Simulated. Nothing left the building."
SUSPENDED_LABEL: Final = "Waiting for approval."
EXECUTED_LABEL: Final = "Done."
REFUSAL_NOTICE: Final = "I could not do that."


@dataclass(frozen=True)
class Notice:
    """What a person is told about one call. Never handed to the agent."""

    simulated: bool
    text: str


def notice_for(route: Route) -> Notice:
    """The person-facing half. Exhaustive, so a new route cannot arrive unlabelled."""
    match route:
        case Route.SIMULATE:
            return Notice(simulated=True, text=SIMULATED_LABEL)
        case Route.SUSPEND:
            return Notice(simulated=False, text=SUSPENDED_LABEL)
        case Route.EXECUTE:
            return Notice(simulated=False, text=EXECUTED_LABEL)
        case Route.REFUSED:
            return Notice(simulated=False, text=REFUSAL_NOTICE)
        case _:
            assert_never(route)


class ActionRecord(BaseModel):
    """What happened, in names and digests, with nothing that could be a value.

    One shape for all four routes rather than a shadow-specific one, so that a route cannot
    quietly become the unrecorded route. Every field here is an identifier, a digest, an enum
    or a timestamp; there is nowhere to put an argument, a row value or an artefact, which is
    the same discipline `brain.audit.ledger.AuditEntry` enforces and for the same reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(pattern=IDENTIFIER)
    agent_id: str = Field(pattern=IDENTIFIER)
    tool_name: str = Field(min_length=1, max_length=80)
    target: str = Field(pattern=TARGET, max_length=120)
    principal_id: str = Field(pattern=IDENTIFIER)
    ent_hash: str
    action_digest: str = Field(pattern=DIGEST)
    route: Route
    tier: AutonomyTier
    #: The checks actually consulted, in order. This is the evidence for M3.8.6: an
    #: auditor can see that all three ran, or exactly where the call stopped.
    checks: tuple[CheckName, ...] = ()
    at: datetime

    @field_validator("at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v


def _record(
    action: Action,
    decision: Decision,
    route: Route,
    *,
    principal_id: str,
    trace_id: str,
    at: datetime,
) -> ActionRecord:
    return ActionRecord(
        trace_id=trace_id,
        agent_id=action.agent_id,
        tool_name=action.tool.name,
        target=action.target,
        principal_id=principal_id,
        ent_hash=decision.ent_hash,
        action_digest=action.digest(),
        route=route,
        tier=decision.tier,
        checks=decision.consulted,
        at=at,
    )


# --------------------------------------------------------------- shadow (M3.8.3)


def run_shadow[T: Entity](
    action: Action, *, simulate: Callable[[Action], TypedResult[T]]
) -> TypedResult[T]:
    """Simulate the call and return what the tool would have returned.

    This function's signature is the guarantee: it has no `execute` parameter, so a shadow
    run cannot reach the real tool by any path through here. `govern` necessarily holds both
    callables because it is the router, and that is the one place to read carefully.

    What this cannot check is whether the simulator itself touches the world. There is no way
    to verify that from inside a policy module, and pretending otherwise would be worse than
    saying so: the compensating control is that a real connector's simulator is its recorded
    fixture or dry-run path, which is tested where the connector is.

    The result carries no marker. `TypedResult` has no field that could hold one, which is
    what makes "indistinguishable to the agent" a shape rather than a promise.
    """
    return simulate(action)


def run_real[T: Entity](
    action: Action, *, execute: Callable[[Action], TypedResult[T]]
) -> TypedResult[T]:
    """Do it (M3.8.5). Separate from `run_shadow` so neither can see the other's callable."""
    return execute(action)


# ------------------------------------------------------------- assisted (M3.8.4)


class ApprovalState(enum.StrEnum):
    """Where a suspended action has got to. Closed; there is no fifth state."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


#: How long an approval may stand before it has to be raised again. An artefact that can be
#: approved forever is a standing grant with extra steps: it survives the reorganisation, the
#: leaver and the policy change that would each have stopped it being granted today.
MAX_APPROVAL_WINDOW: Final = timedelta(hours=24)
DEFAULT_APPROVAL_WINDOW: Final = timedelta(hours=4)


class ApprovalWindowError(Exception):
    """A suspension was raised with a window longer than the maximum.

    Deliberately not part of the user-facing taxonomy, like `StepOutOfOrderError`: nobody
    asking a question sees this. It is a caller error, and it should stop the suspension being
    created rather than quietly clamp the window, because a clamped window is one the console
    would then display wrongly.
    """


class SuspendedAction(BaseModel):
    """An action waiting for a person, with everything needed to resume it (M3.8.4).

    Everything means: what was going to happen (`action`, and `artefact` as it was shown), to
    what (`action.target`), under whose entitlement (`principal_id`), and the `ent_hash` at
    the time. The hash is the point. An approval granted on Monday must not execute on Friday
    under permissions that changed in between, and the only way to notice is to have recorded
    what the permissions were.

    `action_digest` is stored as well as being derivable, so that a stored action edited in
    place no longer matches the approval that was granted for it. Deriving it on read would
    make an altered artefact agree with itself forever, which is the mistake
    `brain.audit.ledger.AuditEntry` avoids by storing its own digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=IDENTIFIER)
    trace_id: str = Field(pattern=IDENTIFIER)
    action: Action
    #: Whose reach this was going to run under.
    principal_id: str = Field(pattern=IDENTIFIER)
    ent_hash: str
    #: What the person was shown, kept verbatim. An approval of a re-rendered artefact is an
    #: approval of something nobody read.
    artefact: str = Field(min_length=1, max_length=8000)
    action_digest: str = Field(pattern=DIGEST)
    raised_at: datetime
    expires_at: datetime
    state: ApprovalState = ApprovalState.PENDING
    decided_by: str = ""
    decided_at: datetime | None = None

    @field_validator("raised_at", "expires_at", "decided_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "suspension timestamps must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        # Enforced on the model and not only in `suspend`, for the reason
        # `brain.audit.ledger.AuditEntry` gives about its own details: an artefact also
        # arrives by being loaded from a table or written by an older version of the code,
        # and a row that grants itself a year must not be loadable.
        if self.expires_at <= self.raised_at:
            msg = "an approval that expires before it was raised can never be granted"
            raise ValueError(msg)
        if self.expires_at - self.raised_at > MAX_APPROVAL_WINDOW:
            msg = (
                f"approval window {self.expires_at - self.raised_at} exceeds "
                f"{MAX_APPROVAL_WINDOW}; an approval that stands indefinitely is a "
                "standing grant with extra steps"
            )
            raise ValueError(msg)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def is_open(self, now: datetime) -> bool:
        """Whether a person may still decide this. Bounded on both counts."""
        return self.state is ApprovalState.PENDING and not self.is_expired(now)

    def _decided(self, state: ApprovalState, by: str, at: datetime) -> Self:
        if not self.is_open(at):
            msg = (
                f"suspension {self.id} is {self.state.value} and expires at "
                f"{self.expires_at.isoformat()}; it cannot be decided at {at.isoformat()}"
            )
            raise ValueError(msg)
        return self.model_copy(update={"state": state, "decided_by": by, "decided_at": at})

    def approved_by(self, who: str, at: datetime) -> Self:
        """A new artefact carrying the approval. The original is not mutated, so what was
        shown and what was decided are two records rather than one overwritten one."""
        return self._decided(ApprovalState.APPROVED, who, at)

    def rejected_by(self, who: str, at: datetime) -> Self:
        return self._decided(ApprovalState.REJECTED, who, at)


def suspend(
    action: Action,
    decision: Decision,
    *,
    principal_id: str,
    trace_id: str,
    now: datetime,
    window: timedelta = DEFAULT_APPROVAL_WINDOW,
    suspension_id: str | None = None,
) -> SuspendedAction:
    """Render the artefact and stop, carrying everything a resume will need to re-check.

    `now` has no default and `datetime.now(UTC)` is deliberately not one, for the argument
    `brain.audit.ledger.AuditChain.append` makes: application clocks drift, and an expiry
    computed from whichever container happened to serve the request is subtly wrong exactly
    when it matters.
    """
    if window > MAX_APPROVAL_WINDOW:
        msg = f"approval window {window} exceeds {MAX_APPROVAL_WINDOW}"
        raise ApprovalWindowError(msg)
    return SuspendedAction(
        id=suspension_id or uuid4().hex,
        trace_id=trace_id,
        action=action,
        principal_id=principal_id,
        ent_hash=decision.ent_hash,
        artefact=render_artefact(action),
        action_digest=action.digest(),
        raised_at=now,
        expires_at=now + window,
    )


class ResumeRefusal(enum.StrEnum):
    """Why a resume did not happen. Recorded, and never shown to the asker as a reason."""

    NOT_APPROVED = "not approved"
    EXPIRED = "expired"
    PRINCIPAL_CHANGED = "principal changed"
    ARTEFACT_ALTERED = "artefact altered"
    ENTITLEMENT_CHANGED = "entitlement changed"
    CHECKS_FAILED = "checks failed"
    RUNG_LOWERED = "rung lowered"


@dataclass(frozen=True)
class Resumption[T: Entity]:
    """The outcome of trying to resume an approved action."""

    resumed: bool
    refusal: ResumeRefusal | None = None
    decision: Decision | None = None
    result: TypedResult[T] | None = None
    record: ActionRecord | None = None

    def notice(self) -> Notice:
        return notice_for(Route.EXECUTE if self.resumed else Route.REFUSED)


def resume[T: Entity](
    suspension: SuspendedAction,
    *,
    caller: EntitlementSet,
    agent_ceiling: EntitlementSet,
    policy: FieldPolicy,
    leash: Leash,
    assessment: RiskAssessment,
    trace_id: str,
    now: datetime,
    execute: Callable[[Action], TypedResult[T]],
) -> Resumption[T]:
    """Re-check everything, then do it (M3.8.4).

    An approval is a statement about a moment, not a permit. Between the approval and the
    resume a person can change department, a grant can be revoked, an agent's ceiling can be
    narrowed and the leash itself can be lowered, and none of those events knows that an
    approved artefact is sitting in a queue. So every one of them is re-checked here, against
    the reach as it stands now rather than the reach the artefact remembers.

    The `ent_hash` comparison is what makes the Monday-to-Friday case detectable at all: the
    artefact carries the hash of the reach it was granted under, and a different hash now
    means something moved, without this module needing to know what.

    Order matters only for what gets reported first. Expiry and state come before anything
    expensive; identity, then the artefact's own integrity; then the reach; then the three
    checks again, because a leash lowered since the approval must still bite.
    """
    if suspension.is_expired(now):
        return Resumption(resumed=False, refusal=ResumeRefusal.EXPIRED)
    if suspension.state is not ApprovalState.APPROVED:
        return Resumption(resumed=False, refusal=ResumeRefusal.NOT_APPROVED)
    if suspension.principal_id != caller.principal_id:
        # Two principals with identical grants share an ent_hash, by design: that is what
        # makes the cache key work. So the hash alone cannot answer "is this the same
        # person", and asking it separately is the difference between a re-check and a
        # coincidence.
        return Resumption(resumed=False, refusal=ResumeRefusal.PRINCIPAL_CHANGED)
    if suspension.action.digest() != suspension.action_digest:
        return Resumption(resumed=False, refusal=ResumeRefusal.ARTEFACT_ALTERED)

    run = caller.intersect(agent_ceiling)
    if run.ent_hash() != suspension.ent_hash:
        return Resumption(resumed=False, refusal=ResumeRefusal.ENTITLEMENT_CHANGED)

    decision = decide(
        suspension.action,
        caller=caller,
        agent_ceiling=agent_ceiling,
        policy=policy,
        leash=leash,
        assessment=assessment,
        now=now,
    )
    if not decision.permitted:
        return Resumption(
            resumed=False,
            refusal=ResumeRefusal.CHECKS_FAILED,
            decision=decision,
            record=_record(
                suspension.action,
                decision,
                Route.REFUSED,
                principal_id=caller.principal_id,
                trace_id=trace_id,
                at=now,
            ),
        )
    if decision.tier < AutonomyTier.ASSISTED:
        # The leash was lowered to SHADOW after the approval was granted. An approval cannot
        # outrank the rung: lowering a leash is how an admin stops an agent acting, and a
        # queue of pre-approved actions that ignored it would make the lever useless exactly
        # when it is pulled in anger.
        return Resumption(
            resumed=False,
            refusal=ResumeRefusal.RUNG_LOWERED,
            decision=decision,
            record=_record(
                suspension.action,
                decision,
                Route.REFUSED,
                principal_id=caller.principal_id,
                trace_id=trace_id,
                at=now,
            ),
        )

    return Resumption(
        resumed=True,
        decision=decision,
        result=run_real(suspension.action, execute=execute),
        record=_record(
            suspension.action,
            decision,
            Route.EXECUTE,
            principal_id=caller.principal_id,
            trace_id=trace_id,
            at=now,
        ),
    )


# ------------------------------------------------------------------- the router


@dataclass(frozen=True)
class Governed[T: Entity]:
    """One call, decided and acted on.

    Two audiences, kept apart in the type. `result` is what goes back into the agent loop and
    is identical in type and shape whether the run was simulated or real. `notice()` is what a
    person is told, and it is the only half that ever says "simulated". A single object
    carrying both, with a flag, would be one attribute access away from handing the agent the
    thing that tells it which world it is in.
    """

    action: Action
    decision: Decision
    route: Route
    record: ActionRecord
    result: TypedResult[T] | None = None
    suspension: SuspendedAction | None = None

    def for_agent(self) -> TypedResult[T] | None:
        """What the loop sees. None on suspend and refuse, because nothing happened yet."""
        return self.result

    def notice(self) -> Notice:
        """What a person sees."""
        return notice_for(self.route)


def govern[T: Entity](
    action: Action,
    *,
    caller: EntitlementSet,
    agent_ceiling: EntitlementSet,
    policy: FieldPolicy,
    leash: Leash,
    assessment: RiskAssessment,
    trace_id: str,
    now: datetime,
    simulate: Callable[[Action], TypedResult[T]],
    execute: Callable[[Action], TypedResult[T]],
    window: timedelta = DEFAULT_APPROVAL_WINDOW,
    suspension_id: str | None = None,
) -> Governed[T]:
    """Decide, then route: simulate, suspend, execute or refuse.

    The one entry point, so there is a single place to read for the answer to "can this agent
    do that". A second path to a side effect is a second chance to skip a check, which is the
    same argument `brain.gate.context` makes about there being one gate.
    """
    decision = decide(
        action,
        caller=caller,
        agent_ceiling=agent_ceiling,
        policy=policy,
        leash=leash,
        assessment=assessment,
        now=now,
    )
    route = route_for(decision)
    record = _record(
        action,
        decision,
        route,
        principal_id=caller.principal_id,
        trace_id=trace_id,
        at=now,
    )

    match route:
        case Route.SIMULATE:
            return Governed(
                action=action,
                decision=decision,
                route=route,
                record=record,
                result=run_shadow(action, simulate=simulate),
            )
        case Route.SUSPEND:
            return Governed(
                action=action,
                decision=decision,
                route=route,
                record=record,
                suspension=suspend(
                    action,
                    decision,
                    principal_id=caller.principal_id,
                    trace_id=trace_id,
                    now=now,
                    window=window,
                    suspension_id=suspension_id,
                ),
            )
        case Route.EXECUTE:
            return Governed(
                action=action,
                decision=decision,
                route=route,
                record=record,
                result=run_real(action, execute=execute),
            )
        case Route.REFUSED:
            # No result and no suspension. A refusal that returned an empty envelope would
            # be indistinguishable from a call that found nothing, and the agent would go on
            # to report an absence it was never told about.
            return Governed(action=action, decision=decision, route=route, record=record)
        case _:
            assert_never(route)
