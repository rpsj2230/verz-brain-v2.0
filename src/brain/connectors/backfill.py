"""Filling the projection for the first time without spending somebody else's day doing it.

A backfill is the single most dangerous thing this system does to a connector, and none of
what makes it dangerous is visible while it runs. It is a loop over everything, it issues
well-formed requests at a reasonable rate, each one is indistinguishable from a person asking
a question, and the first symptom is the client's finance team finding that their other
integrations stopped working until midnight. Xero is 5,000 calls a day *per tenant*, shared
with every other integration the client runs, and nothing we operate can give those calls
back.

So this module is a plan rather than a loop. It decides, one page at a time, whether the next
call may be made, and it says why when the answer is no.

**It consumes the same limiter as everything else.** Not a copy, not a private budget: the
windows in `brain.ops.limits`, addressed by the same keys ordinary traffic reads. A backfill
with its own allowance is a second answer to "how much of this connector is left", and the
two are wrong in the direction that matters, because the backfill's copy is the one nobody
looks at. In production those windows live in Valkey behind
`brain.ops.limit_store.ValkeyWindowStore.check_and_record`, and a caller hands it the
`limits` off the step below: the reserve narrows a limit's *number* and never its *key*, so
the check is the backfill's and the recorded hit is one everybody else can see. When that
store cannot be reached, `UNREACHABLE_POLICY` fails connector scopes closed, which is the
right direction here more than anywhere: an unreadable window is not evidence that there is
room.

**It yields to interactive traffic, which the fair share alone does not do.**
`PRINCIPAL_FAIR_SHARE` already stops one caller taking a whole connector, but a fair share is
about fairness *between peers*, and a person watching a cursor blink is not the backfill's
peer. `brain.gate.context.TrafficClass.SYSTEM` says the same thing in words: our own
housekeeping is never allowed to hold a slot that interactive traffic needs. The mechanism is
a reserve held in the shared windows and nowhere else; see
`THE_RESERVE_IS_HELD_IN_SHARED_WINDOWS_ONLY`.

**It resumes and never restarts.** The whole state is a `BackfillCursor`, which is a value:
resuming is constructing the next step from the one that was persisted, and there is no
in-memory position that a restart loses. This matters more than it sounds. Against a per-day
ceiling, a backfill that restarts after failing at 80% does not cost 20% more, it costs 180%,
and the second attempt is the one that breaks the client's other tools. See
`RESUMING_IS_NOT_RESTARTING`.

**A capped search is not a finished backfill.** Freshdesk's search returns at most 300
records ever. That is a ceiling and not a page size: it cannot be paged past, and the source
reports the 300th page exactly as it reports a genuinely final one. A backfill that called
that DONE would leave the projection holding 300 of 4,000 tickets while every count over it
reads as complete, forever, with nothing anywhere reporting it. `CAPPED` is a separate
outcome for that reason, and it is checked before `DONE`, because the two arrive together.

Scope: domain logic. Nothing here opens a connection, starts a thread or reads a clock. `now`
is a parameter, a page is fetched by whoever executes the step, and the result is handed back
to `record_page`.

Task ids: M11.4.8
"""

from __future__ import annotations

import enum
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from brain.connectors.contract import FetchRequest
from brain.connectors.manifest import ConnectorManifest
from brain.connectors.throttle import limits_for
from brain.ops.limits import Limit, LimiterState, LimitScope, check, search_completeness

# ------------------------------------------------------------------ written-down reasons
#: Why a backfill needs anything more than the ordinary per-caller limit.
A_BACKFILL_LOOKS_LIKE_ORDINARY_TRAFFIC = (
    "Every request a backfill makes is well-formed, correctly authenticated and individually "
    "reasonable, which is why nothing catches it. What makes it different is not any single "
    "call but that there are a hundred thousand of them and nobody is waiting for any one. "
    "The per-principal fair share bounds how much of a connector it can hold at once and says "
    "nothing about priority, so on its own it lets a backfill take its quarter of Xero's "
    "minute while somebody watches a spinner for the answer that needed the other three."
)

#: Why the reserve is subtracted from some windows and not others.
THE_RESERVE_IS_HELD_IN_SHARED_WINDOWS_ONLY = (
    "The reserve is room kept back for callers who are waiting, so it is held in the windows "
    "those callers actually share: the connector's minute and the connector's day. The "
    "per-principal window belongs to the backfill alone, and reserving headroom inside it "
    "would be the backfill holding room open against itself, which slows the backfill down "
    "and gives nobody anything. Narrowing the number does not touch the key, so the hits the "
    "backfill records land in exactly the windows ordinary traffic is checked against."
)

#: Why the position is a value the caller persists rather than a loop variable.
RESUMING_IS_NOT_RESTARTING = (
    "A backfill that restarts from the beginning after failing three quarters of the way "
    "through does not cost a quarter more; it costs everything it already spent, again. "
    "Against Xero's 5,000 a day, shared with every other integration the client runs, the "
    "restart is the run that takes their finance team's other tools out until midnight, and "
    "the calls do not come back. So the entire position is a value: a cursor, a page count, a "
    "record count and whether the source said it was finished. There is nothing in this "
    "module for a crash to lose."
)

#: Why the Freshdesk ceiling gets an outcome of its own rather than being folded into DONE.
A_CAPPED_SEARCH_IS_NOT_A_FINISHED_BACKFILL = (
    "A hard result ceiling looks exactly like the last page: the source returns records, "
    "reports no more, and is telling the truth about what it will give us rather than about "
    "what exists. Recorded as DONE, the projection then holds 300 of 4,000 tickets and every "
    "count over it is silently wrong in the direction of reassurance. Recorded as CAPPED it "
    "is a backfill somebody has to narrow and run again in pieces, which is the only thing "
    "the API allows. See brain.ops.limits.SEARCH_CAP_IS_NOT_A_PAGE_SIZE."
)


# ------------------------------------------------------------------------ the reserve
#: The share of a connector's shared windows held back for callers who are waiting. A half,
#: and the arithmetic is worth stating rather than asserting. Against Xero's 60 a minute it
#: keeps 30 a minute open for questions, which is five times the whole estate's measured rate
#: of roughly six questions a minute across 126 people, on one source. Against Xero's 5,000 a
#: day it caps a backfill at 2,500 and leaves the rest of the day for everybody, which is the
#: exact failure `brain.ops.limits.BOTH_LIMITS_APPLY` names first. The other half still lets a
#: backfill run at 30 calls a minute, or 43,200 a day, so on Xero the daily ceiling binds long
#: before the reserve does and the reserve is doing its work on the minute-limited sources.
INTERACTIVE_RESERVE: Final = 0.5

#: Records to ask for per call, absent a figure from the source. The only thing this module
#: knows about a page size is which way it should point: every published ceiling here counts
#: *calls* rather than records, so halving the page size doubles what the same backfill costs
#: against Xero's 5,000 a day. A backfill therefore asks for the largest page the source
#: permits, and 100 is a floor rather than a recommendation.
DEFAULT_PAGE_SIZE: Final = 100


def backfill_share_of(connector_limit: int) -> int:
    """What a backfill may take from a shared window, after the reserve.

    Deliberately the same shape as `brain.ops.limits.principal_share_of`, including the floor
    of one: a share that rounds to nothing takes the backfill out of service entirely while
    the connector reads as idle, and somebody then raises the reserve looking for the bug.

    A ceiling of one has nothing to divide, and the honest answer is the one
    `principal_share_of` gives about the same case: that connector serialises every caller,
    which is a fact about the connector rather than something a reserve can fix.
    """
    if connector_limit < 1:
        msg = "a connector ceiling is at least 1"
        raise ValueError(msg)
    held = max(1, math.floor(connector_limit * INTERACTIVE_RESERVE))
    return max(1, connector_limit - held)


def backfill_limits(limits: Sequence[Limit]) -> tuple[Limit, ...]:
    """The same windows a request is checked against, with the shared ones narrowed.

    Only `LimitScope.CONNECTOR` is narrowed. See `THE_RESERVE_IS_HELD_IN_SHARED_WINDOWS_ONLY`
    for why the backfill's own share is left alone, and note what is preserved here: the
    scope, the subject and the period are untouched, so `Limit.key` is unchanged and a hit
    recorded against one of these narrowed limits lands in the window everybody reads.
    """
    return tuple(
        replace(
            limit,
            limit=backfill_share_of(limit.limit),
            reason=(
                f"{INTERACTIVE_RESERVE:.0%} of {limit.subject}'s {limit.period} is held for "
                "callers who are waiting; a backfill is never one of them"
            ),
        )
        if limit.scope is LimitScope.CONNECTOR
        else limit
        for limit in limits
    )


# ---------------------------------------------------------------------- the position
@dataclass(frozen=True)
class BackfillCursor:
    """Where a backfill has got to. The whole of its state, and a value on purpose.

    Frozen and complete, so persisting it is persisting the backfill: a run resumed from a
    stored cursor is indistinguishable from one that never stopped. See
    `RESUMING_IS_NOT_RESTARTING`.

    `cursor` is the source's own and is never parsed, matching `FetchRequest.cursor`: its
    shape is the source's business and reading one here makes us wrong the day they change
    it. `records` is counted because a source with a hard result ceiling is measured in
    records rather than in pages, and `pages` because that is what the limiter counts.
    """

    connector: str
    entity: str
    cursor: str = ""
    pages: int = 0
    records: int = 0
    #: The source said there is nothing after this. Not the same as "we have everything":
    #: see `A_CAPPED_SEARCH_IS_NOT_A_FINISHED_BACKFILL`.
    exhausted: bool = False

    def __post_init__(self) -> None:
        if not self.connector.strip() or not self.entity.strip():
            msg = "a backfill cursor names one connector and one entity kind"
            raise ValueError(msg)
        if self.pages < 0 or self.records < 0:
            msg = "pages and records are counts and cannot be negative"
            raise ValueError(msg)

    def advance(self, *, cursor: str, returned: int, exhausted: bool) -> BackfillCursor:
        """Move on by one page. Refuses the two ways a backfill fails to terminate.

        **An advance past an exhausted cursor.** The caller has ignored `DONE` and is about to
        page from the beginning, which is the restart this whole value exists to prevent.

        **A next cursor that is the one we just used, or empty.** A source that hands back the
        cursor it was given is a loop, and a loop against a per-day ceiling spends the client's
        whole allowance overnight and projects one page. Both are refused rather than logged,
        because the thing that notices a logged loop is the ceiling being gone in the morning.
        """
        if returned < 0:
            msg = "a page cannot return a negative number of records"
            raise ValueError(msg)
        if self.exhausted:
            msg = (
                f"the {self.connector}.{self.entity} backfill is already finished; advancing "
                "past the end starts it again, and against a per-day ceiling the second run "
                "is the one that breaks somebody else's tools"
            )
            raise ValueError(msg)
        if not exhausted and (not cursor.strip() or cursor == self.cursor):
            msg = (
                f"{self.connector} returned the cursor it was given ({cursor!r}) and did not "
                "say it was finished, which is a loop; a loop against a per-day ceiling "
                "spends the whole allowance and projects one page"
            )
            raise ValueError(msg)
        return replace(
            self,
            cursor="" if exhausted else cursor,
            pages=self.pages + 1,
            records=self.records + returned,
            exhausted=exhausted,
        )


# ----------------------------------------------------------------------- the decision
class BackfillAction(enum.StrEnum):
    """What a backfill may do next. Closed, because everything above it branches on this.

    `WAIT` and `YIELD` are separate although both mean "not now", because they go to
    different people. A backfill over its own share is running as fast as it is allowed to
    and needs nothing; a backfill yielding is being held back so that somebody waiting on an
    answer gets the connector first, and an operator asking why a backfill has taken three
    days deserves to be told which of those it is. Collapsing them produces a progress page
    that says "waiting" for a week.
    """

    FETCH = "fetch"
    #: Over an allowance of its own. Come back after the hint.
    WAIT = "wait"
    #: There is room, and it is being kept for callers who are waiting.
    YIELD = "yield"
    #: The source has no more pages, and nothing capped the result.
    DONE = "done"
    #: The source's hard result ceiling was reached. Not done: see
    #: `A_CAPPED_SEARCH_IS_NOT_A_FINISHED_BACKFILL`.
    CAPPED = "capped"


@dataclass(frozen=True)
class BackfillStep:
    """One decision, and everything the caller needs to act on it.

    Carries `limits` even when the action is not `FETCH`. They are what a caller hands to
    `brain.ops.limit_store` to record the hit afterwards, and returning them from the same
    call that decided makes it impossible to check against one set of windows and record
    against another.
    """

    action: BackfillAction
    reason: str
    request: FetchRequest | None = None
    limits: tuple[Limit, ...] = ()
    retry_after_seconds: float = 0.0

    @property
    def may_fetch(self) -> bool:
        return self.action is BackfillAction.FETCH

    @property
    def is_finished(self) -> bool:
        """Whether there is anything left to do, whatever the reason. Not the same as
        complete: a `CAPPED` backfill is finished and does not hold everything."""
        return self.action in (BackfillAction.DONE, BackfillAction.CAPPED)


def next_step(
    *,
    now: datetime,
    manifest: ConnectorManifest,
    cursor: BackfillCursor,
    state: LimiterState,
    principal_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> BackfillStep:
    """Whether the next page may be fetched, and if not, which of the reasons it is.

    The order of the branches is the rule.

    **The result ceiling is checked before the source's own end-of-pages.** They arrive
    together on a capped search, and asking "is it finished" first records a truncated
    backfill as a complete one, which is the failure that is invisible afterwards.

    **The limits come from the manifest through `throttle.limits_for`**, which refuses a
    connector naming no verified ceiling rather than inventing one. That refusal matters more
    for a backfill than for anything else: an unmeasured source run in a loop is how a ceiling
    nobody knew about is discovered.

    Nothing is recorded here. `record_page` is a separate call the caller makes after the page
    comes back, which is what keeps a refused step from consuming an allowance. That is the
    same split `brain.ops.limits.check` makes from `LimiterState.record`, and for the same
    reason.
    """
    if cursor.connector != manifest.name:
        msg = (
            f"the cursor is for {cursor.connector!r} and the manifest is {manifest.name!r}; a "
            "backfill resumed against a different connector would page one source with "
            "another's position"
        )
        raise ValueError(msg)
    if page_size < 1:
        msg = "a page of zero records is a call that costs a call and returns nothing"
        raise ValueError(msg)

    # The ceiling registry and the search-cap registry in `brain.ops.limits` are both keyed by
    # the verified source name, and `manifest.ceiling` is the field that names it. Looking
    # this up by `manifest.name` would silently find nothing for a connector installed under a
    # client's own name, which is the ordinary case.
    completeness = search_completeness(manifest.ceiling, cursor.records)
    if not completeness.complete:
        return BackfillStep(action=BackfillAction.CAPPED, reason=completeness.reason)
    if cursor.exhausted:
        return BackfillStep(
            action=BackfillAction.DONE,
            reason=(
                f"{cursor.connector}.{cursor.entity} reported no further pages after "
                f"{cursor.pages} page(s) and {cursor.records} record(s)"
            ),
        )

    limits = backfill_limits(limits_for(manifest, principal_id=principal_id))
    decision = check(now=now, limits=limits, state=state)
    if not decision.allowed:
        # `binding` is never None when a decision refuses, and the scope of the window that
        # bound is what separates the two refusals: a shared window is room being held for
        # somebody waiting, and the backfill's own share is the backfill at full speed.
        shared = decision.binding is not None and decision.binding.scope is LimitScope.CONNECTOR
        return BackfillStep(
            action=BackfillAction.YIELD if shared else BackfillAction.WAIT,
            reason=decision.reason,
            limits=limits,
            retry_after_seconds=decision.retry_after_seconds,
        )

    return BackfillStep(
        action=BackfillAction.FETCH,
        reason=decision.reason,
        request=FetchRequest(entity=cursor.entity, limit=page_size, cursor=cursor.cursor),
        limits=limits,
    )


def record_page(
    *,
    now: datetime,
    cursor: BackfillCursor,
    state: LimiterState,
    limits: Sequence[Limit],
    returned: int,
    next_cursor: str,
    exhausted: bool,
) -> tuple[BackfillCursor, LimiterState]:
    """Record one completed page: the new position, and the call it cost.

    Both together, in one call, because they are one event and drift between them is silent
    in both directions. A page counted in the cursor and not in the limiter is a call nobody
    can see, and after a few thousand of them the connector's window is a fiction. A call
    recorded without the cursor moving is a page that will be fetched again, at full cost.

    The hits land in the real windows even though `limits` are the narrowed ones: narrowing
    changes a limit's number and never its key, so ordinary traffic is checked against
    everything a backfill has spent.
    """
    return (
        cursor.advance(cursor=next_cursor, returned=returned, exhausted=exhausted),
        state.record(now, tuple(limits)),
    )
