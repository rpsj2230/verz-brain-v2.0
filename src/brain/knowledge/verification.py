"""Who is told that a colleague vouched for something, and how a review queue stays workable.

`brain.knowledge.item` holds the record and the two decisions over it: which items a sweep
should open tasks for, and what state an item's badge is in. Both are pure functions over
items the caller already holds, and both stop short of the thing that makes them real. This
module is the rest: the job a scheduler calls, and the badge one particular reader may be
shown.

**A verification badge is a disclosure, and it is entitled like any other.** "Verified by
Priya on 3 March" names a colleague and dates an act of theirs. A reader may be perfectly
entitled to the document and not entitled to know who signed it: the item's own scope
decides who reaches the content, and it says nothing at all about whose name may travel
beside it. So the name is behind its own capability, and the decision is made by the
machinery every other narrowing in this system uses, `EntitlementSet.intersect`,
`scope_for` and `Scope.matches`, in that order, exactly as `brain.ops.denial_alerts.reach`
decides who may be told about a run of denials. There is deliberately no second rule here.
See `A_BADGE_IS_A_DISCLOSURE_LIKE_ANY_OTHER`.

The capability falls out of the existing grammar rather than being invented for it.
`brain.knowledge.search.KNOWLEDGE_READ` is `read:knowledge`, and `Capability.covers`
expands only a trailing `.*`, so an entity-level grant does not confer `read:knowledge.verifier`.
Reaching a passage and being told who signed it are therefore already two different grants,
and nobody had to remember to keep them apart.

**Withholding the name must never withhold the state.** A badge that said nothing at all to
an unentitled reader would be worse than no badge, because its absence would be read as
"this one is fine" on exactly the documents nobody has checked. So every reader gets a
badge, always, and the four states stay distinguishable to all of them; what varies is
whether the sentence carries a person and a date. An unverified item reads "not verified by
anyone" to everybody. See `AN_UNVERIFIED_ITEM_STILL_CARRIES_A_BADGE`.

`DisclosedBadge` cannot hold a name it may not render. That is the difference between a
structural decision and a formatting one: a badge that carried the verifier and merely
declined to print them would leak through the first trace, log line or JSON serialisation
that touched it, which is how `brain.gate.provenance.DocumentCitation` argues against
carrying passage text it does not display.

**A scheduled job that opens tasks is a job that can open thousands of them.** Two hundred
documents imported on one Tuesday carry the same review date, and a year later one person is
handed the lot in a single morning, which is indistinguishable from being handed none. The
bound is therefore per owner rather than per run: a queue belongs to a person, and a global
ceiling would be a bound on the machine, which was never the thing that got overwhelmed. It
would also be silently unfair, because whoever sorted first would take the whole allowance
and everybody else would get nothing with no record saying so. See
`THE_BOUND_IS_PER_OWNER_BECAUSE_A_QUEUE_BELONGS_TO_A_PERSON`.

**Bounding is safe because nothing is dropped.** An item that does not fit this run has no
log entry written for it, so it is still due on the next one and arrives at the top of the
order, oldest review date first. The bound defers; it does not discard. That is the whole
reason a small number is defensible here, and it is why the run reports `more_waiting` as a
boolean rather than a count: an operator needs to know the sweep is behind, and a number
would be a count of documents that operator may not be entitled to see. See
`A_DEFERRED_ITEM_IS_STILL_DUE`.

**One task per item per review date, and never a second.** The log is keyed on the item and
the date it was due, so a sweep running every morning over the same overdue item opens one
task in total, and a fresh task appears only when somebody re-verifies the item and sets a
new date. `brain.ops.denial_alerts` debounces on a time window instead, and the difference
is the artefact: a repeated notification is noise, while a repeated work item is a duplicate
in somebody's queue, and two of those teach a person to ignore the queue. Deduplication by
identity is what a queue needs; recency is what a notification needs. See
`ONE_TASK_PER_REVIEW_DATE_AND_NEVER_A_SECOND`.

Rejected: a stored `needs_review` flag the job clears. `brain.knowledge.item` already
refuses that for the state; it goes stale the moment the clock passes it. The log here is
not the same thing, because it records an event that happened (a task was opened) rather
than a condition that is claimed to hold.

Rejected: opening the task against the item's department rather than its owner. A task
addressed to a department is a task addressed to nobody, which the architecture settles for
the knowledge layer by making stewardship a per-object relation, and `ReverificationTask`
already carries the owner for that reason.

Rejected: choosing between two rows for one item when the caller hands us both. The sweep
would then open a task for whichever page the query returned first, and which document a
person is asked to re-verify would depend on the `ORDER BY`. It is refused instead, loudly
at the caller, rather than resolved quietly in the queue where nobody would ever see it.

Scope: domain logic. Nothing here opens a connection, stores anything, or reads a clock.
`now` is a parameter and the log is a snapshot somebody else keeps, for the reason
`brain.ops.denial_alerts.AlertLog` gives about its own: a debouncer owning its own store
cannot be tested at the boundary, and the boundary is the only part that is ever wrong.

Task ids: M7.4.6, M7.4.7
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from brain.core.department import DEPARTMENT_FIELD
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Clause, Op, Scope
from brain.knowledge.item import (
    BADGE_TEXT,
    KnowledgeError,
    KnowledgeItem,
    ReverificationTask,
    VerificationState,
    badge,
    due_for_reverification,
)
from brain.knowledge.visibility import OWNER_FIELD

# ------------------------------------------------------------------ written-down reasons

#: Why the verifier's name goes through the entitlement model rather than around it.
A_BADGE_IS_A_DISCLOSURE_LIKE_ANY_OTHER: Final = (
    "A badge saying Priya verified this on 3 March names a colleague and dates an act of "
    "theirs, so it is a disclosure like any other and it is entitled like any other. The "
    "item's own scope decides who reaches the content and says nothing about whose name may "
    "travel beside it, which is why reaching a passage and being told who signed it are two "
    "different grants: Capability.covers expands only a trailing .*, so read:knowledge does "
    "not confer read:knowledge.verifier. The decision itself is EntitlementSet.intersect, "
    "scope_for and Scope.matches, and not a rule written here, because a second "
    "implementation of the central invariant is a second place for it to be wrong and the "
    "permissive copy is the one that wins the day they disagree."
)

#: Why an unentitled reader still sees a badge, and why the states stay distinguishable.
AN_UNVERIFIED_ITEM_STILL_CARRIES_A_BADGE: Final = (
    "A badge that vanished for a reader who may not see the verifier would be read as "
    "reassurance on precisely the documents nobody has checked, because the eye learns that "
    "no badge means nothing to report. So every reader gets a badge in every state, and what "
    "the entitlement decides is only whether it carries a person and a date. An item nobody "
    "has vouched for reads as not verified by anyone, to everybody, which is a fact on file "
    "and not a claim that the content is wrong."
)

#: Why the sweep's ceiling is counted per owner and not per run.
THE_BOUND_IS_PER_OWNER_BECAUSE_A_QUEUE_BELONGS_TO_A_PERSON: Final = (
    "Two hundred documents imported on one Tuesday share a review date, and a year later "
    "one person is handed all of them in a morning, which gets the same amount done as "
    "handing them none. A ceiling on the whole run would bound the machine, and the machine "
    "was never what got overwhelmed; worse, it would be silently unfair, because whichever "
    "owner sorted first would take the entire allowance and the rest would be given nothing "
    "with no record anywhere saying so. The queue belongs to a person, so the bound is the "
    "number of items one person is asked to look at between two runs."
)

#: Why a small per-owner bound is safe rather than lossy.
A_DEFERRED_ITEM_IS_STILL_DUE: Final = (
    "An item that does not fit this run has nothing written to the log for it, so it is "
    "still due on the next run and arrives at the head of the order, oldest review date "
    "first. The bound defers and never discards, which is what makes a number this small "
    "defensible. The run says whether it deferred anything as a boolean and never as a "
    "count: an operator has to know the sweep is behind, and a number would be a count of "
    "documents that operator may not be entitled to see."
)

#: Why the log is keyed on the review date rather than debounced on a window.
ONE_TASK_PER_REVIEW_DATE_AND_NEVER_A_SECOND: Final = (
    "A repeated notification is noise; a repeated work item is a duplicate in somebody's "
    "queue, and two of those are what teach a person to stop reading the queue. So the key "
    "is the item and the date it fell due, not a window: a sweep running every morning over "
    "the same overdue item opens exactly one task, and a new one appears only when somebody "
    "re-verifies the item and sets a new date. brain.ops.denial_alerts debounces on time "
    "instead, correctly, because it is sending notifications rather than opening work."
)


# ----------------------------------------------------------- the badge (M7.4.7)

#: What a reader must hold before a badge may name the person who verified an item.
#:
#: A field-level capability on the same noun `brain.knowledge.search.KNOWLEDGE_READ` uses, so
#: one word covers the knowledge layer. The relationship between the two is the point:
#: `read:knowledge` does not cover this, because `Capability.covers` expands only a trailing
#: `.*`, while `read:knowledge.*` does. Somebody who administers the knowledge layer
#: therefore holds it already and nobody else acquires it by accident.
VERIFIER_CAPABILITY: Final = Capability(value="read:knowledge.verifier")

#: The principal id on the requirement `requirement_to_name_the_verifier` builds. Not a
#: person, and named so it cannot be read as one: a requirement borrowing a real principal's
#: id would appear in a trace as that person holding something nobody granted them.
BADGE_REQUIREMENT: Final = "requirement:verification_badge"

#: The two states that have a person and a date to withhold. The other two name nobody in
#: the first place, so they read identically to every reader and there is nothing for an
#: entitlement to decide about them.
ATTRIBUTABLE_STATES: Final[frozenset[VerificationState]] = frozenset(
    {VerificationState.VERIFIED, VerificationState.DUE}
)

#: What each state says when the reader may not be told who verified it.
#:
#: A second wording of the same four states, never a second vocabulary: `VerificationState`
#: is imported rather than restated, so a fifth state cannot arrive here with a sentence and
#: be missing from the attributed mapping, or the other way about.
#:
#: No name, no date and no digit of any kind. The state is what a reader needs in order to
#: weigh the answer; the person and the date are the follow-up path, and the follow-up path
#: is the part that is entitled. The last two sentences are word for word the ones in
#: `BADGE_TEXT`, because those states withhold nothing and a reader should see the same
#: sentence either way.
UNATTRIBUTED_BADGE_TEXT: Final[Mapping[VerificationState, str]] = MappingProxyType(
    {
        VerificationState.VERIFIED: "verified",
        VerificationState.DUE: "verified, and due for review",
        VerificationState.UNVERIFIED: BADGE_TEXT[VerificationState.UNVERIFIED],
        VerificationState.SUPERSEDED: BADGE_TEXT[VerificationState.SUPERSEDED],
    }
)


@dataclass(frozen=True)
class DisclosedBadge:
    """The badge as one particular reader may be shown it (M7.4.7).

    Distinct from `brain.knowledge.item.VerificationBadge`, which is the badge as the record
    holds it. Two types because they answer two different questions: that one is what is on
    file, and this one is what a reader is told, which needs an entitlement and a person
    asking. Rejected: giving `item.badge` a reader argument. It would put a permission
    decision inside the module that owns the record, and it would leave the console, which
    legitimately shows everything to whoever may administer the knowledge layer, with no way
    to ask for the unfiltered fact.

    **An unattributed badge cannot hold the name.** The refusals below are the mechanism.
    Carrying the verifier and declining to print them would survive into every trace, log
    line and serialisation that touched this value, which is the argument
    `brain.gate.provenance.DocumentCitation` makes about carrying passage text.
    """

    state: VerificationState
    verified_by: str = ""
    verified_at: datetime | None = None

    def __post_init__(self) -> None:
        if bool(self.verified_by) != (self.verified_at is not None):
            # The same rule `KnowledgeItem` applies to the record, restated because a badge
            # can be built without one. Half a verification renders as authoritative while
            # naming nobody, or names somebody without saying when.
            msg = (
                f"a disclosed badge carries half a verification (by={self.verified_by!r}, "
                f"at={self.verified_at!r}); a verification is a person and a date, or nothing"
            )
            raise ValueError(msg)
        if self.verified_by and self.state not in ATTRIBUTABLE_STATES:
            msg = (
                f"a {self.state} badge cannot carry a verifier; a replaced or unverified item "
                "names nobody, and a name on one would render as an endorsement of it"
            )
            raise ValueError(msg)

    @property
    def names_the_verifier(self) -> bool:
        """Whether this badge is the attributed one. The presence of the name, not a flag.

        A separate boolean would be a second statement of the same fact, and the day it
        disagreed with the field the renderer would follow whichever it happened to read.
        """
        return bool(self.verified_by)

    def render(self) -> str:
        """The sentence. Attributed wording when there is a name, unattributed otherwise.

        `BADGE_TEXT` is imported rather than copied, so the attributed wording has one home
        and a change to it cannot land in one of two places.
        """
        if not self.names_the_verifier:
            return UNATTRIBUTED_BADGE_TEXT[self.state]
        template = BADGE_TEXT[self.state]
        when = self.verified_at.date().isoformat() if self.verified_at is not None else ""
        return template.format(who=self.verified_by, when=when)


def _place(item: KnowledgeItem) -> dict[str, Any]:
    """The fields a `read:knowledge.verifier` grant's scope may be written against.

    Two, both named by the constants their scope builders already use, so a grant scoped on
    `department` cannot be evaluated against a row spelling it something else. That is the
    failure `OWNER_FIELD` and `DEPARTMENT_FIELD` exist to prevent, and it reads as a
    permission problem rather than as a typo.

    A company-visibility item carries no department, so the value is empty and a departmental
    grant admits nothing: `Clause.matches` refuses an absent or unequal field. That fails
    closed, which is the safe direction and worth stating because it is not the convenient
    one. A reader who should be told the verifier of company-wide knowledge holds a grant
    whose scope says so, which is an unrestricted scope or a clause over a field the item
    actually carries.
    """
    return {DEPARTMENT_FIELD: item.visibility.department, OWNER_FIELD: item.owner_id}


def requirement_to_name_the_verifier(item: KnowledgeItem) -> EntitlementSet:
    """What somebody must hold before this item's verifier may be named to them.

    One grant: the capability, in the place the item sits. An `EntitlementSet` rather than a
    pair of values so that `may_name_verifier` can narrow it with the ordinary intersection
    rather than comparing scopes by hand, which is the shape
    `brain.ops.denial_alerts.requirement` uses for the same reason.

    The scope is built from the place rather than carried beside it. Two fields would be two
    things that can disagree, and the disagreement is silent in whichever direction the
    caller happened to write it.
    """
    clauses = tuple(
        Clause(field=name, op=Op.EQ, value=str(value))
        for name, value in sorted(_place(item).items())
    )
    return EntitlementSet(
        principal_id=BADGE_REQUIREMENT,
        grants=(Grant(capability=VERIFIER_CAPABILITY, scope=Scope(clauses=clauses)),),
    )


def may_name_verifier(item: KnowledgeItem, reader: EntitlementSet, *, now: datetime) -> bool:
    """Whether this reader may be told who verified this item (M7.4.7).

    Two ways in, and no third.

    **The reader is the verifier.** Withholding somebody's own act from them protects
    nobody, and a badge reading "verified" to the person who verified it is a system that
    looks broken. This is a decision rather than something falling out of the grant model,
    and it can be removed by deleting one branch.

    **A grant covers the capability in a scope admitting the item's place.**
    `intersect` decides what narrower means, `scope_for` decides what holding it means and
    refuses an expired reader, and `Scope.matches` decides whether the grant admits the
    place. The intersection runs requirement-first for the reason
    `brain.ops.denial_alerts.reach` gives: narrowing the *reader* by a specific capability
    would drop the wildcard grant of somebody who plainly holds it, because `covers` expands
    only a trailing `.*`. Narrowing the requirement by the reader asks the question that was
    meant.

    The owner is deliberately not a third way in. An owner is answerable for the item being
    right, which is not the same as being entitled to the identity of everybody who has
    touched it, and the surface that gives an owner what they need is the re-verification
    task below: it is addressed to them by name and carries no verifier at all.

    A bool rather than the scope `denial_alerts.reach` returns, and the difference is not
    laziness. That function has one way in, so a scope is available for every admission and
    the console can show why somebody is on a list. Here the first branch admits on identity
    and has no scope to hand back, so the honest common type is the answer to the question
    the function's name asks.
    """
    if not item.verified_by:
        return False
    if reader.principal_id == item.verified_by:
        return True
    shared = requirement_to_name_the_verifier(item).intersect(reader)
    scope = shared.scope_for(VERIFIER_CAPABILITY, now)
    return scope is not None and scope.matches(_place(item))


def disclose(item: KnowledgeItem, *, reader: EntitlementSet, now: datetime) -> DisclosedBadge:
    """The badge this reader may be shown, as of `now` (M7.4.7).

    The state comes from `brain.knowledge.item.badge` and is not recomputed. That module
    already decides that a superseded item reports as replaced before it reports as verified,
    and that the review date is compared against a `now` passed in rather than stored; a
    second copy of the state machine here would be a second answer to what the badge says.

    The entitlement is evaluated only when there is a name to withhold. A superseded or
    unverified item names nobody, so asking who may hear about a person that is not there
    would be permission work whose result is discarded, and a discarded result is one a
    refactor can make reachable.
    """
    on_file = badge(item, now=now)
    if on_file.state not in ATTRIBUTABLE_STATES:
        return DisclosedBadge(state=on_file.state)
    if not may_name_verifier(item, reader, now=now):
        return DisclosedBadge(state=on_file.state)
    return DisclosedBadge(
        state=on_file.state,
        verified_by=on_file.verified_by,
        verified_at=on_file.verified_at,
    )


# ------------------------------------------------- the scheduled job (M7.4.6)

#: How many items one owner may be asked to look at between two runs.
#:
#: Five, and the number is judging a rate rather than measuring one, so it is worth saying
#: what it is judging. A re-verification is reading a document and confirming it still holds,
#: which is minutes rather than seconds, and five is already at the top of what somebody does
#: in a day beside their actual work. A larger number does not get more done; it produces a
#: list that is visibly hopeless, and a list that is visibly hopeless is closed.
#:
#: The cost is stated rather than hidden. A five hundred item cliff on one owner takes a
#: hundred runs to open, so the last of them is asked about long after it lapsed. That is
#: worse than it sounds and better than the alternative, which opens five hundred tasks in
#: one morning and gets none of them done. The real fix for a cliff is upstream, in staggered
#: review dates at import, and nothing here can undo an import that already happened.
TASKS_PER_OWNER_PER_RUN: Final = 5

#: How often the job is expected to run. A default for the argument below rather than a
#: schedule this module installs: nothing here owns a timer, for the same reason nothing here
#: owns a clock.
DEFAULT_CADENCE: Final = timedelta(days=1)

#: How far ahead of the review date a task is opened. Seven days, which is a working week, so
#: a review can be done before the item lapses rather than after. It must be at least one
#: cadence, and `open_reverification_tasks` refuses otherwise; see that function.
DEFAULT_LEAD_TIME: Final = timedelta(days=7)

#: One row of the log: which item, and the review date it fell due on.
ReviewKey = tuple[str, datetime]

_NOTHING_OPENED: Mapping[ReviewKey, datetime] = MappingProxyType({})


@dataclass(frozen=True)
class ReverificationLog:
    """When a task was opened for each item and review date, as somebody else stored it.

    A snapshot rather than a store, matching `brain.ops.denial_alerts.AlertLog`: a job that
    held its own client could not be tested at the boundary that matters, which here is the
    second run over an item whose task is already open.

    Keyed on the review date as well as the item, so re-verifying an item and setting a new
    date opens a new task while the sweep running again over the same lapsed date opens
    nothing. See `ONE_TASK_PER_REVIEW_DATE_AND_NEVER_A_SECOND`.
    """

    opened: Mapping[ReviewKey, datetime] = _NOTHING_OPENED

    def was_opened(self, key: ReviewKey) -> bool:
        return key in self.opened

    def record(self, keys: Iterable[ReviewKey], now: datetime) -> ReverificationLog:
        """Note that tasks have just been opened for these. Returns a new log."""
        updated = dict(self.opened)
        for key in keys:
            updated[key] = now
        return ReverificationLog(opened=MappingProxyType(updated))


@dataclass(frozen=True)
class ReverificationRun:
    """What one pass opened, and the log to keep for the next one.

    Both together, which is the choice `brain.ops.denial_alerts.Digest` makes and the
    opposite of the one `brain.ops.limits.check` makes. There the separation is load-bearing,
    because a refused request must not extend its own window. Here there is nothing to
    refuse, and the failure runs the other way: a caller who forgets to store the log opens
    every task again tomorrow, silently, while every test of the key still passes.
    """

    tasks: tuple[ReverificationTask, ...]
    log: ReverificationLog
    #: Whether the per-owner bound held anything back. A boolean and never a count: an
    #: operator has to know the sweep is behind, and a number here would be a count of
    #: documents that operator may not be entitled to see. See `A_DEFERRED_ITEM_IS_STILL_DUE`.
    more_waiting: bool = False


#: A log with nothing in it, for a first run. A module constant rather than a default built
#: at the call site, in the shape `brain.gate.provenance.NO_DOCUMENTS` uses: the type is
#: frozen and its mapping is a proxy, so one instance is safe to share.
NO_TASKS_OPENED: Final = ReverificationLog()


def key_for(task: ReverificationTask) -> ReviewKey:
    """The log key a task is recorded under. One spelling, used by the job and by callers."""
    return (task.item_id, task.review_by)


def open_reverification_tasks(
    items: Sequence[KnowledgeItem],
    *,
    now: datetime,
    log: ReverificationLog = NO_TASKS_OPENED,
    cadence: timedelta = DEFAULT_CADENCE,
    lead_time: timedelta = DEFAULT_LEAD_TIME,
    per_owner: int = TASKS_PER_OWNER_PER_RUN,
) -> ReverificationRun:
    """One pass of the scheduled sweep (M7.4.6).

    The decision about which items are due is `brain.knowledge.item.due_for_reverification`
    and is not restated: this adds the three things that turn a decision into a job, which
    are a bound, a memory, and a refusal.

    **The lead time is at least one cadence, and this refuses otherwise.** They are set
    independently and are silently inconsistent together: a monthly sweep with no lead time
    opens a task up to a month after the item lapsed, and the symptom is a queue that is
    always late with nothing anywhere explaining why. The relationship is checkable, so it
    is checked.

    **One item may appear once.** Two rows for one item id mean the caller has handed us the
    same document twice, which is a broken query rather than a state to resolve, and
    resolving it would make the document somebody is asked about depend on which page the
    query returned first. It is refused here, at the caller, rather than in the queue where
    nobody would see it. The emission below is keyed by item id as well, so a refactor that
    dropped this refusal still could not open two tasks for one document.

    **The bound is per owner and it defers.** See
    `THE_BOUND_IS_PER_OWNER_BECAUSE_A_QUEUE_BELONGS_TO_A_PERSON` and
    `A_DEFERRED_ITEM_IS_STILL_DUE`. Items arrive oldest review date first, so what is held
    back is the least overdue, and it is at the head of the next run.

    **The allowance is spent by a task that is opened and by nothing else.** The counter
    moves in the same step as the emission, below every skip, so an item already in the log
    costs its owner nothing. Written the other way about, an owner with five tasks already
    open would never be given a sixth however long the sweep ran, and the queue would
    deadlock on its own memory with every dashboard showing a bound working as designed.

    That property comes from where the counter moves and not from the order of the checks,
    which is worth saying because the order looks like it is doing the work and is not: a
    mutation swapping the two skips changes nothing at all, and the one that breaks it moves
    the increment above them. Both were run.
    """
    if now.tzinfo is None:
        # Guarded here rather than left to the comparison inside `due_for_reverification`,
        # where a naive `now` surfaces as a TypeError out of datetime arithmetic and reads
        # as a bug in the knowledge model rather than in the caller.
        msg = "now must be timezone-aware; comparing it with a review date otherwise lies"
        raise ValueError(msg)
    if cadence <= timedelta():
        msg = "a cadence of zero is not a schedule; the job would be a loop rather than a sweep"
        raise ValueError(msg)
    if lead_time < cadence:
        msg = (
            f"a lead time of {lead_time} is shorter than the {cadence} cadence; an item can "
            "then lapse and be noticed a whole cadence later, so the queue is always late "
            "and nothing in it says why"
        )
        raise ValueError(msg)
    if per_owner < 1:
        msg = "a per-owner bound below one opens no tasks at all and reads as a working sweep"
        raise ValueError(msg)

    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            msg = (
                f"{item.item_id!r} appears twice in one sweep; which version a person is "
                "asked to re-verify would depend on the order the query returned them"
            )
            raise KnowledgeError(msg)
        seen.add(item.item_id)

    opened: dict[str, ReverificationTask] = {}
    per_owner_count: dict[str, int] = {}
    more_waiting = False
    for task in due_for_reverification(items, now=now, lead_time=lead_time):
        if log.was_opened(key_for(task)):
            continue
        if task.item_id in opened:
            continue
        if per_owner_count.get(task.owner_id, 0) >= per_owner:
            more_waiting = True
            continue
        opened[task.item_id] = task
        per_owner_count[task.owner_id] = per_owner_count.get(task.owner_id, 0) + 1

    tasks = tuple(opened.values())
    return ReverificationRun(
        tasks=tasks,
        log=log.record((key_for(task) for task in tasks), now),
        more_waiting=more_waiting,
    )
