"""Turning an agent off, retiring it for good, and handing it to somebody else.

Three states and one move between people, and every one of them is a decision a person made
about a record rather than a fact about a clock. Nothing here reads the time; `now` is a
parameter, for the reason `brain.gate.provenance` gives, because a rule about dates that
reads the clock itself cannot be tested at its own boundary.

**Disable is reversible and archive is not, and that difference is the only reason there
are two.** A reversible archive is a disable with a longer name: the two controls collapse
into one with two spellings, and a console showing both would be offering a choice that
makes no difference. Archiving says the agent is finished, and something finished that can
come back is something nobody can say the state of. Bringing one back is creating an agent,
with a new record, a new persona review and a new ceiling somebody signed off, which is
exactly the work that should not be skippable by clearing a column.

**A state change never touches the ceiling.** Disabling an agent does not narrow what it may
reach and enabling it does not widen anything, because neither function is given an
`AgentAuthority` to change. Reach is decided per run, by intersecting the caller's
entitlement with the agent's ceiling, and a disabled agent simply never gets that far:
`runnable_agent_ids` leaves it out, so `brain.gate.select.select_agent` cannot choose it.
That is one enforcement point rather than two, and the alternative, emptying the ceiling on
disable and restoring it on enable, is a data migration disguised as a toggle.

**Ownership transfer moves the steward and nothing else (M13.1.5).** When somebody leaves,
their agents need a person who answers for them, and that is all a transfer is. It does not
move authority: an agent handed to a Super Admin does not thereby reach more, because
`E_run` is computed against the caller of each run and not against whoever owns the record.
It does move audience, but only where the audience *is* the owner: a personal agent becomes
visible to its new steward and invisible to the person who left, which is the point of
transferring it, and a department agent's audience does not move at all.

`transfer_ownership` takes a `Principal` rather than an id, because the check that matters
is whether the new steward is still there, and `Principal.is_active` is where that already
lives. An id would mean re-deriving it from a lookup the caller may not have made, and the
failure that produces is silent: a leaver's agents handed to a second leaver on the same
offboarding run, with every record looking correct.

**`agents_needing_transfer` is the one listing in this package not filtered by audience,
and that is its whole purpose.** A personal agent whose owner has gone is reachable by
nobody: `visible_agent_ids` correctly returns nothing for it, for every viewer, for ever.
Finding those rows therefore cannot be an audience question. It is keyed by the ids the
caller named, never by a scan of who owns what, which is the rule
`brain.core.department.plan_cross_department` states about the department list it is handed:
a function that took the estate and reported every owner would be an org chart with agent
counts on it.

Task ids: M13.1.4, M13.1.5
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from brain.agents.model import AgentAudience, AgentError, AgentRecord, AgentState
from brain.core.principal import Principal

#: Why archive has no inverse, stated where a reader meets it.
ARCHIVE_IS_TERMINAL: Final = (
    "Archiving an agent is a decision that it is finished, and there is no function here "
    "that undoes it. Enable and disable refuse an archived record rather than quietly "
    "doing nothing, so a caller that believes it brought an agent back is told otherwise. "
    "A reversible archive is a disable with a longer name, and two controls that differ "
    "only in wording are one control somebody will use interchangeably."
)

#: Why a transfer is safe to perform without re-approving anything.
A_TRANSFER_MOVES_THE_STEWARD_AND_NOT_THE_REACH: Final = (
    "Ownership names who answers for an agent. It is not a grant, it is not a ceiling and "
    "it is not an entitlement: a run's reach is its caller's entitlement intersected with "
    "the agent's ceiling, and neither side of that mentions the owner. So handing an agent "
    "to somebody with wider access does not widen the agent, and handing it to somebody "
    "with narrower access does not narrow it. What a transfer does change is the audience "
    "of a personal agent, because at that level the steward is the audience."
)


def _revalidated(record: AgentRecord, **changes: object) -> AgentRecord:
    """A copy of a record with the changes applied and every validator run again.

    `model_copy(update=...)` is the obvious way to write this and is wrong here: it skips
    validation entirely, so a naive `disabled_at` would be stored by the one path that ever
    writes that column, and the validator refusing naive timestamps would be dead code that
    still passes its own test.
    """
    fields = {name: getattr(record, name) for name in type(record).model_fields}
    return AgentRecord.model_validate({**fields, **changes})


def _refuse_if_archived(record: AgentRecord, verb: str) -> None:
    """Archived is terminal, and saying so beats doing nothing quietly.

    A no-op return would leave the caller believing the agent is available again, and the
    next thing they do is wonder why it never answers.
    """
    if record.state is AgentState.ARCHIVED:
        msg = (
            f"{record.agent_id!r} was archived on {record.archived_at} and cannot be "
            f"{verb}; an archived agent is finished, and bringing one back is creating one"
        )
        raise AgentError(msg)


def enable(record: AgentRecord) -> AgentRecord:
    """Make an agent selectable again (M13.1.4).

    Takes no `now`, because enabling clears a timestamp rather than writing one. When it
    happened belongs in `obs.audit_entry`, which records who did it as well; a
    `re_enabled_at` column here would be a second, partial history that nothing reads and
    that disagrees with the ledger the first time one of the two writes fails.

    Enabling an already enabled agent returns the same record rather than raising. A retry
    after a timeout must not fail, and there is nothing to refuse: the caller asked for a
    state the record is already in.
    """
    _refuse_if_archived(record, "enabled")
    if record.disabled_at is None:
        return record
    return _revalidated(record, disabled_at=None)


def disable(record: AgentRecord, *, now: datetime) -> AgentRecord:
    """Stop an agent being selected, reversibly (M13.1.4).

    Disabling an already disabled agent keeps the original timestamp. The first disabling is
    the one somebody decided; overwriting it on a retry would move the date of a decision to
    the date of a duplicate request, and the moved date is the one an incident review reads.
    """
    _refuse_if_archived(record, "disabled")
    if record.disabled_at is not None:
        return record
    return _revalidated(record, disabled_at=now)


def archive(record: AgentRecord, *, now: datetime) -> AgentRecord:
    """Retire an agent for good (M13.1.4).

    The row stays. Nothing here deletes an agent, because the ledger refers to it by id and
    a run recorded against a row that no longer exists is a trace nobody can read. An
    archived agent keeps its persona, its ceiling and its history, and is selectable by
    nobody: `AgentRecord.is_selectable` is false, so `runnable_agent_ids` leaves it out.

    Archiving an already archived agent keeps the first timestamp, for the reason `disable`
    gives. It does not raise: the record is in the state that was asked for, and the
    refusals in `enable` and `disable` are about a state change that would undo this one.
    """
    if record.state is AgentState.ARCHIVED:
        return record
    return _revalidated(record, archived_at=now)


def transfer_ownership(record: AgentRecord, *, to_owner: Principal, now: datetime) -> AgentRecord:
    """Hand an agent to a new steward (M13.1.5).

    Three refusals, and each one is a way an offboarding run silently does nothing useful.

    **The new steward is somebody else.** A transfer to the current owner writes a record of
    a decision nobody made, and on a leaver's estate it would report success for exactly the
    agents that still have no live owner.

    **The new steward is still here.** `Principal.is_active` is the existing check and it is
    asked with the caller's `now`. Without it, an offboarding that runs down a list hands one
    leaver's agents to another leaver, and both sets of rows look correctly owned.

    **The agent is not archived.** An archived agent cannot be seen, started or brought back,
    so there is nothing for a steward to answer for. Transferring one would put a decision in
    the record that changes nothing observable, and `agents_needing_transfer` leaves archived
    rows out for the same reason.

    What comes back differs from what went in by one field. `created_by` does not move: it is
    the record of who built the agent, which is what an audit asks, and the steward is what
    everybody else asks. The authority is passed through untouched, and there is no argument
    here that could change it.
    """
    if record.state is AgentState.ARCHIVED:
        msg = (
            f"{record.agent_id!r} is archived and has no steward to transfer; "
            "an archived agent is answerable for nothing"
        )
        raise AgentError(msg)
    if to_owner.id == record.audience.owner_id:
        msg = (
            f"{record.agent_id!r} already belongs to {to_owner.id!r}; a transfer to the "
            "current owner records a decision nobody made and leaves the agent unowned "
            "when the owner is the person leaving"
        )
        raise AgentError(msg)
    if not to_owner.is_active(now):
        msg = (
            f"{to_owner.id!r} is not active at {now.isoformat()} and cannot take "
            f"{record.agent_id!r}; handing a leaver's agents to another leaver leaves rows "
            "that look owned and are not"
        )
        raise AgentError(msg)
    # Built rather than copied with an update, and the reason is stronger than the one
    # `_revalidated` gives: pydantic does not revalidate a model instance handed to a model
    # field, so an audience produced by `model_copy` would slip past this constructor's
    # checks and past the record's rebuild as well, which is every check there is.
    moved = AgentAudience(
        level=record.audience.level,
        owner_id=to_owner.id,
        department=record.audience.department,
    )
    return _revalidated(record, audience=moved)


def agents_needing_transfer(
    records: Iterable[AgentRecord], *, departing: frozenset[str]
) -> tuple[str, ...]:
    """The ids of live agents whose steward is on the way out (M13.1.5).

    `departing` is who the caller asked about, never a set this function derives. Reading
    every owner off the estate and reporting the ones who have left would be a different
    function with a different risk: an inventory of who owns what, produced by anybody who
    can call it.

    Archived agents are left out. They are answerable for nothing and `transfer_ownership`
    refuses them, so listing them would produce a queue of work that cannot be done.

    Sorted, so an offboarding runbook processes the same list in the same order twice, and
    returned as ids alone. There is no count of anything omitted.
    """
    return tuple(
        sorted(
            r.agent_id
            for r in records
            if r.state is not AgentState.ARCHIVED and r.audience.owner_id in departing
        )
    )
