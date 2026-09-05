"""Rules the leash must never break. A failure here blocks deploy.

The leash is the only thing standing between "an agent can reach this tool" and "an agent
uses this tool on a client, at three in the morning, with nobody watching". Projection
decided reach; none of it says anything about supervision.

Every rule below fails in the same direction when it is broken: something happens that
nobody approved, and it looks like the system working. That is why these are invariants
rather than unit tests. The unit file next door checks that each rung does what it says;
this one checks that no path exists to a longer leash than was configured.

Task ids: M3.8.2, M3.8.3, M3.8.4, M3.8.5, M3.8.6
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.scope import Scope
from brain.gate import leash as leash_module
from brain.gate.injection import ELEVATED, HIGH, AutonomyTier, RiskAssessment
from brain.gate.leash import (
    CHECK_ORDER,
    MAX_APPROVAL_WINDOW,
    MISSING_ENTRY_RUNG,
    REFUSAL_NOTICE,
    Action,
    ActionRecord,
    CheckName,
    Decision,
    Governed,
    Leash,
    LeashEntry,
    ResumeRefusal,
    Resumption,
    Route,
    SuspendedAction,
    decide,
    effective_tier,
    govern,
    notice_for,
    resume,
    route_for,
    run_shadow,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CLEAN = RiskAssessment(score=0, matched=())
ELEVATED_SCORE = RiskAssessment(score=ELEVATED, matched=("urgency_pressure",))
HIGH_SCORE = RiskAssessment(score=HIGH, matched=("instruction_override",))
ASSESSMENTS = (CLEAN, ELEVATED_SCORE, HIGH_SCORE)

AGENT = "ag_support"
TARGET = "ticket.update_status"


class Ticket(Entity):
    status: str = ""


TOOL = ToolDefinition(
    name="ticket.update_status",
    description="Set the status of a support ticket",
    entity="ticket",
    required_capability="write:ticket.status",
    side_effect=SideEffect.WRITE,
    identity_mode=IdentityMode.DELEGATED,
)

POLICY = FieldPolicy(
    rules=(FieldRule.of("ticket", "status", "read:ticket.status", Classification.INTERNAL),)
)


def entitlement(
    *capabilities: str, principal_id: str = "u_weiling", scope: Scope | None = None
) -> EntitlementSet:
    where = scope if scope is not None else Scope.unrestricted()
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=value), scope=where) for value in capabilities
        ),
    )


CALLER = entitlement("write:ticket.status", "read:ticket.status")
CEILING = entitlement("write:ticket.status", "read:ticket.status", principal_id="ag_support")

ACTION = Action(
    agent_id=AGENT,
    tool=TOOL,
    target=TARGET,
    touched_fields=("status",),
    row={"department": "maintenance"},
    args={"status": "closed"},
)


def leash_at(rung: AutonomyTier, *, scope: Scope | None = None) -> Leash:
    return Leash(
        entries=(
            LeashEntry(
                agent_id=AGENT,
                target=TARGET,
                scope=scope if scope is not None else Scope.unrestricted(),
                rung=rung,
            ),
        )
    )


def result(_: Action) -> TypedResult[Ticket]:
    return TypedResult[Ticket](records=(Ticket(entity="ticket", id="t_9", status="closed"),))


def decide_at(
    leash: Leash, *, caller: EntitlementSet = CALLER, ceiling: EntitlementSet = CEILING
) -> Decision:
    return decide(
        ACTION,
        caller=caller,
        agent_ceiling=ceiling,
        policy=POLICY,
        leash=leash,
        assessment=CLEAN,
        now=NOW,
    )


def govern_at(
    leash: Leash,
    *,
    execute: Callable[[Action], TypedResult[Ticket]] = result,
    caller: EntitlementSet = CALLER,
) -> Governed[Ticket]:
    return govern(
        ACTION,
        caller=caller,
        agent_ceiling=CEILING,
        policy=POLICY,
        leash=leash,
        assessment=CLEAN,
        trace_id="tr_1",
        now=NOW,
        simulate=result,
        execute=execute,
    )


# ------------------------------------------------------------- fail closed
def test_a_missing_leash_entry_means_shadow() -> None:
    """The single most important line in the module. Break it and every target nobody
    configured, which is every target nobody has thought about, runs at full autonomy."""
    assert MISSING_ENTRY_RUNG is AutonomyTier.SHADOW
    empty = Leash()
    for agent in ("ag_support", "ag_accountant", "", "unknown"):
        for target in ("ticket", "ticket.update_status", "xero.create_invoice"):
            for row in ({}, {"department": "finance"}, {"department": "maintenance"}):
                assert empty.rung_for(agent, target, row) is AutonomyTier.SHADOW


def test_a_populated_leash_still_fails_closed_for_anything_it_does_not_name() -> None:
    """The realistic version of the rule above. A leash is never empty in production, and
    the failure it must not have is inheriting a configured rung onto an unconfigured pair."""
    configured = Leash(
        entries=(
            LeashEntry(
                agent_id=AGENT,
                target=TARGET,
                scope=Scope.department("maintenance"),
                rung=AutonomyTier.AUTONOMOUS,
            ),
            LeashEntry(
                agent_id="ag_accountant",
                target="xero.create_invoice",
                scope=Scope.unrestricted(),
                rung=AutonomyTier.ASSISTED,
            ),
        )
    )
    unnamed = (
        (AGENT, "xero.create_invoice", {"department": "maintenance"}),
        ("ag_accountant", TARGET, {"department": "maintenance"}),
        (AGENT, TARGET, {"department": "finance"}),
        (AGENT, "ticket", {"department": "maintenance"}),
        ("ag_new", TARGET, {"department": "maintenance"}),
    )
    for agent, target, row in unnamed:
        assert configured.rung_for(agent, target, row) is AutonomyTier.SHADOW


def test_the_leash_has_no_per_agent_default_to_fall_back_to() -> None:
    """Enforced structurally, not by review. A per-agent default reads as a convenience and
    is the widening this module exists to prevent: trust earned updating a ticket status
    would be spent raising an invoice, and the diff that added it would look like ergonomics."""
    forbidden = {
        "default",
        "default_rung",
        "default_tier",
        "agent_default",
        "fallback",
        "otherwise",
    }
    assert not (set(Leash.model_fields) & forbidden)
    assert not (set(LeashEntry.model_fields) & forbidden)
    assert not ({name.lower() for name, _ in inspect.getmembers(leash_module)} & forbidden)


def test_a_leash_entry_cannot_name_a_wildcard_target() -> None:
    """A wildcard is a per-agent default wearing a target's clothes."""
    for target in ("ticket.*", "*", "ticket.**", "TICKET"):
        with pytest.raises(ValueError, match="target"):
            LeashEntry(
                agent_id=AGENT,
                target=target,
                scope=Scope.unrestricted(),
                rung=AutonomyTier.AUTONOMOUS,
            )


# ------------------------------------------------------- only ever tightens
@pytest.mark.parametrize("rung", list(AutonomyTier))
@pytest.mark.parametrize("assessment", ASSESSMENTS)
def test_the_effective_tier_never_exceeds_the_leash(
    rung: AutonomyTier, assessment: RiskAssessment
) -> None:
    """The same rule as everywhere else in the platform: nothing adds reach. A clean score on
    an agent pinned to Shadow must never promote it."""
    assert effective_tier(leash_at(rung), ACTION, assessment) <= rung


@pytest.mark.parametrize("rung", list(AutonomyTier))
def test_a_high_score_forces_simulation_whatever_the_leash_says(rung: AutonomyTier) -> None:
    """The risk ceiling is the other input to the same decision, and it is `autonomy_ceiling`
    rather than a second copy of the rule. Two rules that must agree eventually disagree."""
    assert effective_tier(leash_at(rung), ACTION, HIGH_SCORE) is AutonomyTier.SHADOW


def test_adding_a_leash_entry_can_only_ever_tighten() -> None:
    """Rungs compose by intersection, like every other ceiling here. Break this and a
    company-wide Shadow pin becomes advisory: anyone able to add a narrower row cancels it."""
    base = leash_at(AutonomyTier.ASSISTED)
    for rung in AutonomyTier:
        widened = base.with_entry(
            LeashEntry(agent_id=AGENT, target=TARGET, scope=Scope.unrestricted(), rung=rung)
        )
        assert widened.rung_for(AGENT, TARGET, {}) <= base.rung_for(AGENT, TARGET, {})


def test_an_agent_ceiling_can_only_narrow_what_the_caller_holds() -> None:
    """`E_run = E(caller) ∩ agent_ceiling`. An agent is a lens, never a principal, so a
    capability the caller lacks cannot be supplied by the agent's own ceiling."""
    caller_without = entitlement("read:ticket.status")
    generous_ceiling = entitlement(
        "write:ticket.status", "read:ticket.status", principal_id="ag_support"
    )
    decision = decide_at(
        leash_at(AutonomyTier.AUTONOMOUS), caller=caller_without, ceiling=generous_ceiling
    )
    assert not decision.permitted
    assert decision.refused_by is CheckName.CAPABILITY


# ------------------------------------------------------- M3.8.6 three checks
def test_the_declared_check_order_is_capability_then_rung_then_mask() -> None:
    """The order is meaning: caller, then agent, then field. Reordering it would ask about
    supervision on behalf of somebody who may not act at all, and log the answer."""
    assert CHECK_ORDER == (CheckName.CAPABILITY, CheckName.RUNG, CheckName.MASK)
    assert set(CHECK_ORDER) == set(CheckName)


@pytest.mark.parametrize("rung", list(AutonomyTier))
def test_all_three_checks_are_consulted_before_any_call_proceeds(rung: AutonomyTier) -> None:
    """M3.8.6, stated as the thing that must not regress. Short-circuiting on a refusal is
    fine; a permitted call that skipped a check is a permission bypass that reads as a
    refactor, because the call still succeeds and nothing looks different.

    Written against `CheckName` and the literal sequence rather than against `CHECK_ORDER`,
    deliberately. A test comparing the checks that ran to the constant that drives them
    passes whatever that constant says, so deleting a check from it would delete the test's
    only claim in the same edit."""
    decision = decide_at(leash_at(rung))
    assert decision.permitted
    assert set(decision.consulted) == set(CheckName)
    assert decision.consulted == (CheckName.CAPABILITY, CheckName.RUNG, CheckName.MASK)


@pytest.mark.parametrize(
    "caller",
    [
        entitlement("write:ticket.status", "read:ticket.status"),
        entitlement("write:ticket.status"),
        entitlement("read:ticket.status"),
        entitlement(),
    ],
)
def test_the_checks_consulted_are_always_a_prefix_of_the_declared_order(
    caller: EntitlementSet,
) -> None:
    """Whatever went wrong, the sequence run is the start of the declared sequence. A gap in
    the middle would mean a check was skipped rather than short-circuited past."""
    decision = decide_at(leash_at(AutonomyTier.AUTONOMOUS), caller=caller)
    consulted = decision.consulted
    assert consulted == CHECK_ORDER[: len(consulted)]
    assert consulted, "at least one check always runs"
    if not decision.permitted:
        # The last one consulted is the one that refused; nothing runs after a refusal.
        assert decision.refused_by is consulted[-1]


def test_a_refused_call_never_reaches_the_tool() -> None:
    """The checks have to actually stop something. A decision recorded and then ignored is
    the shape of every audit trail that documented a breach it did not prevent."""
    calls: list[str] = []

    def recording(_: Action) -> TypedResult[Ticket]:
        calls.append("executed")
        return result(_)

    governed = govern_at(
        leash_at(AutonomyTier.AUTONOMOUS),
        execute=recording,
        caller=entitlement("read:ticket.status"),
    )
    assert governed.route is Route.REFUSED
    assert calls == []
    assert governed.for_agent() is None


# ------------------------------------------------------------ M3.8.3 shadow
def test_a_shadow_run_has_no_way_to_reach_the_real_tool() -> None:
    """Checked on the signature rather than the body, for the reason `render_lock` is checked
    on its signature: a function that cannot be passed the real executor cannot call it,
    whatever a later edit does inside."""
    params = set(inspect.signature(run_shadow).parameters)
    assert params == {"action", "simulate"}
    assert "execute" not in params


def test_the_result_handed_to_an_agent_has_nowhere_to_say_it_was_simulated() -> None:
    """An agent that can tell it is being simulated is an agent whose simulated behaviour
    proves nothing. The marker lives on `Notice`, which the loop never sees."""
    forbidden = {"simulated", "shadow", "is_simulated", "rehearsal", "dry_run", "marked", "tier"}
    assert not (set(TypedResult.model_fields) & forbidden)

    shadow = govern_at(leash_at(AutonomyTier.SHADOW)).for_agent()
    real = govern_at(leash_at(AutonomyTier.AUTONOMOUS)).for_agent()
    assert shadow is not None and real is not None
    assert type(shadow) is type(real)
    assert shadow.model_dump() == real.model_dump()


def test_the_person_is_always_told_when_a_run_was_simulated() -> None:
    """The other half. Exactly one route is marked simulated, and it is the simulating one."""
    marked = {route for route in Route if notice_for(route).simulated}
    assert marked == {Route.SIMULATE}


@pytest.mark.parametrize("rung", list(AutonomyTier))
def test_nothing_below_the_autonomous_rung_reaches_the_real_tool(rung: AutonomyTier) -> None:
    """The rungs have to differ in what they *do*, not only in what they report. Break this
    and Shadow becomes a label on a real side effect."""
    calls: list[str] = []

    def recording(_: Action) -> TypedResult[Ticket]:
        calls.append("executed")
        return result(_)

    govern_at(leash_at(rung), execute=recording)
    assert calls == (["executed"] if rung is AutonomyTier.AUTONOMOUS else [])


# ---------------------------------------------------------- M3.8.4 assisted
def _approved_suspension() -> SuspendedAction:
    governed = govern_at(leash_at(AutonomyTier.ASSISTED))
    assert governed.suspension is not None
    return governed.suspension.approved_by("u_director", NOW + timedelta(minutes=5))


def _resume(
    suspension: SuspendedAction,
    *,
    caller: EntitlementSet = CALLER,
    ceiling: EntitlementSet = CEILING,
) -> Resumption[Ticket]:
    return resume(
        suspension,
        caller=caller,
        agent_ceiling=ceiling,
        policy=POLICY,
        leash=leash_at(AutonomyTier.ASSISTED),
        assessment=CLEAN,
        trace_id="tr_2",
        now=NOW + timedelta(minutes=10),
        execute=result,
    )


def test_an_approval_does_not_survive_a_change_to_the_entitlement_it_was_granted_under() -> None:
    """An approval granted on Monday must not execute on Friday under permissions that changed
    in between. The artefact carries the hash of the reach it was granted under precisely so
    this check is possible; without it, an approval becomes a permit that outlives the grant
    it depended on, and the mover case is where permission systems actually leak."""
    approved = _approved_suspension()
    assert _resume(approved).resumed

    # This case first, because it is the one nothing else catches. The same grants, in the
    # same scopes, now carrying a time bound: every one of the three checks still passes, so
    # if the hash is not compared the action simply runs. `ent_hash` covers `not_after` for
    # exactly this reason, and a contractor being given an end date is not a rare event.
    time_bounded = EntitlementSet(
        principal_id=CALLER.principal_id,
        grants=CALLER.grants,
        not_after=NOW + timedelta(days=30),
    )
    silent = _resume(approved, caller=time_bounded)
    assert not silent.resumed
    assert silent.refusal is ResumeRefusal.ENTITLEMENT_CHANGED

    # And the loud cases, where the reach genuinely narrowed. These would eventually be
    # caught by the checks below, but they must be reported as what they are.
    moved = (
        # A grant revoked between the approval and the resume.
        entitlement("read:ticket.status"),
        # The same grants, narrowed to a department.
        entitlement("write:ticket.status", "read:ticket.status", scope=Scope.department("m")),
    )
    for changed in moved:
        outcome = _resume(approved, caller=changed)
        assert not outcome.resumed
        assert outcome.refusal is ResumeRefusal.ENTITLEMENT_CHANGED


def test_a_grant_outside_the_agents_ceiling_does_not_invalidate_an_approval() -> None:
    """The hash is over `E(caller) ∩ agent_ceiling`, not over the caller's nominal grants,
    and that is the difference between a re-check and an alarm nobody can act on. Somebody
    being granted an unrelated capability does not change what this run may reach, so it must
    not invalidate their queue; a check that fired on it would be turned off within a week."""
    approved = _approved_suspension()
    unrelated = entitlement("write:ticket.status", "read:ticket.status", "admin:ticket")
    assert unrelated.ent_hash() != CALLER.ent_hash()
    assert _resume(approved, caller=unrelated).resumed


def test_an_approval_does_not_survive_a_change_to_the_agents_own_ceiling() -> None:
    """The other side of the same intersection. Narrowing an agent's ceiling is how an admin
    reduces what every run of it can reach, and a queue of approvals that ignored it would
    make the lever apply only to work that had not started."""
    approved = _approved_suspension()
    outcome = _resume(
        approved, ceiling=entitlement("read:ticket.status", principal_id="ag_support")
    )
    assert not outcome.resumed
    assert outcome.refusal is ResumeRefusal.ENTITLEMENT_CHANGED


def test_a_suspension_altered_after_it_was_approved_cannot_be_resumed() -> None:
    """The digest is stored rather than derived, so an action edited in place stops matching
    the approval granted for it. Derived, it would agree with itself forever."""
    approved = _approved_suspension()
    swapped = approved.model_copy(
        update={
            "action": ACTION.model_copy(update={"args": {"status": "deleted"}}),
        }
    )
    outcome = _resume(swapped)
    assert not outcome.resumed
    assert outcome.refusal is ResumeRefusal.ARTEFACT_ALTERED


def test_an_approval_is_bounded_in_time_and_the_bound_cannot_be_widened() -> None:
    """An artefact that can be approved forever is a standing grant with extra steps. The
    bound is enforced on the model as well as in `suspend`, because an artefact also arrives
    by being loaded from a table or written by an older version of the code."""
    governed = govern_at(leash_at(AutonomyTier.ASSISTED))
    held = governed.suspension
    assert held is not None
    assert held.expires_at > held.raised_at
    assert held.expires_at - held.raised_at <= MAX_APPROVAL_WINDOW

    # Checked through construction rather than `model_copy`, which skips validation by
    # design. This is the path a row loaded from a table takes, and a row that granted
    # itself a year has to be unloadable rather than merely never written.
    stored = held.model_dump()
    stored["expires_at"] = held.raised_at + MAX_APPROVAL_WINDOW + timedelta(seconds=1)
    with pytest.raises(ValueError, match="standing grant"):
        type(held)(**stored)


def test_an_expired_approval_can_neither_be_granted_nor_resumed() -> None:
    """One bound covering both, rather than an approval window plus a longer execution
    window. Two bounds is two places for an off-by-one, and the second is invariably the
    longer one."""
    governed = govern_at(leash_at(AutonomyTier.ASSISTED))
    held = governed.suspension
    assert held is not None
    after = held.expires_at + timedelta(seconds=1)
    assert not held.is_open(after)
    with pytest.raises(ValueError, match="cannot be decided"):
        held.approved_by("u_director", after)

    approved = held.approved_by("u_director", NOW + timedelta(minutes=1))
    outcome = resume(
        approved,
        caller=CALLER,
        agent_ceiling=CEILING,
        policy=POLICY,
        leash=leash_at(AutonomyTier.ASSISTED),
        assessment=CLEAN,
        trace_id="tr_2",
        now=after,
        execute=result,
    )
    assert not outcome.resumed
    assert outcome.refusal is ResumeRefusal.EXPIRED


def test_a_resume_re_runs_all_three_checks() -> None:
    """A re-check that skipped the checks would be a re-check of the clock only. The leash,
    the capability and the mask can all have moved since the approval."""
    approved = _approved_suspension()
    outcome = _resume(approved)
    assert outcome.decision is not None
    assert outcome.decision.consulted == CHECK_ORDER


# ------------------------------------------------------------------ routing
@pytest.mark.parametrize("tier", list(AutonomyTier))
def test_every_autonomy_tier_has_a_route(tier: AutonomyTier) -> None:
    """`assert_never` makes a fourth tier a type error rather than a silent default. This is
    the runtime half: every tier that exists today resolves to a distinct behaviour."""
    decision = decide_at(leash_at(tier))
    assert route_for(decision) in set(Route)
    assert route_for(decision) is not Route.REFUSED


def test_the_three_rungs_route_to_three_different_places() -> None:
    """If two rungs did the same thing there would not be three rungs, and the middle one
    would be documentation."""
    routes = {tier: route_for(decide_at(leash_at(tier))) for tier in AutonomyTier}
    assert len(set(routes.values())) == len(AutonomyTier)


def test_a_refusal_tells_a_person_the_same_thing_whatever_the_reason() -> None:
    """A message that varies by cause is a side channel two people can read by comparing
    screens, which is the same rule `render_lock` enforces by taking no arguments."""
    assert notice_for(Route.REFUSED).text == REFUSAL_NOTICE
    assert not notice_for(Route.REFUSED).simulated
    for caller in (entitlement(), entitlement("write:ticket.status")):
        governed = govern_at(leash_at(AutonomyTier.AUTONOMOUS), caller=caller)
        assert governed.route is Route.REFUSED
        assert governed.notice().text == REFUSAL_NOTICE


# ------------------------------------------------------------------ the record
def test_the_record_of_an_action_can_carry_no_value() -> None:
    """The record outlives the call. Break this and every ticket subject, invoice amount and
    client name written by an agent lands in it, in the one table nobody prunes.

    The field set is pinned rather than described, so adding a field that could hold an
    argument is a deliberate edit in two places instead of an omission in one. That is the
    same device `brain.audit.ledger.AuditAction` uses to keep its vocabulary closed."""
    assert set(ActionRecord.model_fields) == {
        "trace_id",
        "agent_id",
        "tool_name",
        "target",
        "principal_id",
        "ent_hash",
        "action_digest",
        "route",
        "tier",
        "checks",
        "at",
    }

    secretive = ACTION.model_copy(
        update={
            "row": {"client_name": "SNM Construction Pte Ltd"},
            "args": {"status": "invoiced 48000"},
        }
    )
    governed = govern(
        secretive,
        caller=CALLER,
        agent_ceiling=CEILING,
        policy=POLICY,
        leash=leash_at(AutonomyTier.AUTONOMOUS),
        assessment=CLEAN,
        trace_id="tr_1",
        now=NOW,
        simulate=result,
        execute=result,
    )
    dumped = governed.record.model_dump_json()
    assert "SNM Construction" not in dumped
    assert "48000" not in dumped
    assert "client_name" not in dumped


def test_a_record_is_written_for_every_route() -> None:
    """A route without a record is the unrecorded route, and it will be the one that
    matters. One record shape for all four is what stops that happening by omission."""
    for caller, leash in (
        (CALLER, leash_at(AutonomyTier.SHADOW)),
        (CALLER, leash_at(AutonomyTier.ASSISTED)),
        (CALLER, leash_at(AutonomyTier.AUTONOMOUS)),
        (entitlement(), leash_at(AutonomyTier.AUTONOMOUS)),
    ):
        governed = govern_at(leash, caller=caller)
        assert isinstance(governed.record, ActionRecord)
        assert governed.record.route is governed.route
        assert governed.record.checks == governed.decision.consulted


def test_no_reason_vocabulary_in_this_module_is_open_text() -> None:
    """Every reason recorded anywhere here is a closed enum. A free-text reason is where
    somebody eventually writes the value that was refused, in the longest-lived record."""
    for vocabulary in (CheckName, Route, ResumeRefusal, leash_module.CheckReason):
        assert issubclass(vocabulary, enum.StrEnum)
        assert len(set(vocabulary)) == len(list(vocabulary))
