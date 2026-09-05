"""Filling the projection for the first time, against ceilings that belong to somebody else.

Everything asserted here is about a failure that is invisible while it happens. A backfill
issues well-formed, correctly authenticated, individually reasonable requests; nothing in a
log looks wrong; and the first symptom is the client's finance team finding their other
integrations stopped working until midnight, because Xero's 5,000 a day is per tenant and
shared with every one of them.

So the tests are built around three properties that each have an obvious wrong version.

**It spends the real allowance.** Asserted against the limiter's own windows rather than
against a call count on a fake, because a backfill with its own budget passes every
call-counting test ever written and is exactly the bug.

**It gives way to people.** The interesting assertion is the pair: at the same moment, the
backfill is told to yield and an ordinary caller is admitted. Either half alone is satisfied
by a limiter that refuses everybody or by one that refuses nobody.

**It resumes.** A backfill that restarts after failing three quarters through does not cost a
quarter more, it costs everything it already spent, again, and the second run is the one that
breaks somebody else's tools.

Task ids: M11.4.8
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.connectors.backfill import (
    DEFAULT_PAGE_SIZE,
    INTERACTIVE_RESERVE,
    BackfillAction,
    BackfillCursor,
    backfill_limits,
    backfill_share_of,
    next_step,
    record_page,
)
from brain.connectors.contract import ConnectorScope, CredentialBinding, TransportKind
from brain.connectors.manifest import ConnectorManifest
from brain.connectors.throttle import UnmeasuredSourceError
from brain.ops.limits import (
    FRESHDESK_SEARCH_MAX_RECORDS,
    SOURCE_CEILINGS,
    LimiterState,
    LimitKey,
    LimitScope,
    WindowState,
    check,
    principal_share_of,
    source_limits,
)
from brain.ops.secrets import SecretRef, VaultRole

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

BACKFILL = "p_svc_backfill"
ASKER = "u_weiling"

#: The three windows a Xero call is checked against, spelled out rather than read from
#: `source_limits`, so that a test asserting the backfill consumed them has something
#: independent to disagree with.
XERO_MINUTE: LimitKey = (LimitScope.CONNECTOR, "xero", "minute")
XERO_DAY: LimitKey = (LimitScope.CONNECTOR, "xero", "day")
BACKFILL_SHARE: LimitKey = (LimitScope.PRINCIPAL_CONNECTOR, f"{BACKFILL}:xero", "minute")

REF = SecretRef(path="connectors/xero_ro", role=VaultRole.APPLICATION)


def a_manifest(**overrides: object) -> ConnectorManifest:
    defaults: dict[str, object] = {
        "name": "xero",
        "version": "1.0.0",
        "transport": TransportKind.REST,
        "scope": ConnectorScope(resource_kind="tenant", selectors=("tenant_0447",)),
        "credential": CredentialBinding(ref=REF),
        "ceiling": "xero",
    }
    defaults.update(overrides)
    return ConnectorManifest(**defaults)  # type: ignore[arg-type]


def a_cursor(**overrides: object) -> BackfillCursor:
    defaults: dict[str, object] = {"connector": "xero", "entity": "invoice"}
    defaults.update(overrides)
    return BackfillCursor(**defaults)  # type: ignore[arg-type]


def window_with(key: LimitKey, hits: int, *, now: datetime = NOW) -> LimiterState:
    """A limiter state holding `hits` recent hits in one window and nothing anywhere else.

    Spaced a second apart and ending a second before `now`, so every one of them is inside a
    sixty-second window without any sitting exactly on its edge, which is a different test.
    """
    times = tuple(now - timedelta(seconds=hits - i) for i in range(hits))
    return LimiterState(windows={key: WindowState(hits=times)})


def run_pages(
    pages: int, *, state: LimiterState | None = None
) -> tuple[BackfillCursor, LimiterState]:
    """Drive a backfill for `pages` pages, the way a runner would."""
    cursor = a_cursor()
    limiter = state if state is not None else LimiterState()
    for page in range(pages):
        step = next_step(
            now=NOW,
            manifest=a_manifest(),
            cursor=cursor,
            state=limiter,
            principal_id=BACKFILL,
        )
        assert step.may_fetch, step.reason
        cursor, limiter = record_page(
            now=NOW,
            cursor=cursor,
            state=limiter,
            limits=step.limits,
            returned=DEFAULT_PAGE_SIZE,
            next_cursor=f"page_{page + 1}",
            exhausted=False,
        )
    return cursor, limiter


# ------------------------------------------------- it spends the real allowance (M11.4.8)
def test_a_backfill_consumes_the_windows_ordinary_traffic_is_checked_against() -> None:
    """**Asserted against the limiter, not against a call count.** A backfill that kept its
    own budget would satisfy any test that counted calls on a fake, and it is precisely the
    bug: the connector's real window then has no idea what was spent, and the person who
    finds out is the client's finance team.

    All three windows, not just the binding one. A request that consumed a Xero call consumed
    it from the connector's minute, the connector's day and the caller's share, and recording
    one of them makes the other two drift until they mean nothing.

    Delete this and a private counter passes review."""
    _cursor, state = run_pages(3)
    assert state.window_for(XERO_MINUTE).count(NOW, 60.0) == 3
    assert state.window_for(XERO_DAY).count(NOW, 86_400.0) == 3
    assert state.window_for(BACKFILL_SHARE).count(NOW, 60.0) == 3


def test_the_keys_a_backfill_records_into_are_the_keys_an_ordinary_call_reads() -> None:
    """The reserve narrows a limit's *number*. If it changed the scope, the subject or the
    period it would change `Limit.key`, and the backfill would then be spending a window
    nobody else looks at while the shared one reads as idle.

    Delete this and narrowing can be implemented by renaming the subject, which looks
    tidier and quietly gives the backfill a private allowance."""
    ordinary = source_limits("xero", principal_id=BACKFILL)
    narrowed = backfill_limits(ordinary)
    assert [limit.key for limit in narrowed] == [limit.key for limit in ordinary]
    _cursor, state = run_pages(3)
    assert set(state.windows) == {limit.key for limit in ordinary}


def test_a_refused_step_records_nothing() -> None:
    """`next_step` decides and `record_page` records, and they are separate calls for the
    reason `brain.ops.limits.check` is separate from `LimiterState.record`: a refusal that
    entered the window would push the backfill's own retry time further away every time it
    asked, and the hint it was given becomes a lie.

    Delete this and deciding acquires a side effect, which is invisible until a backfill
    that is being throttled starts throttling itself harder."""
    before = window_with(XERO_MINUTE, 30)
    for _attempt in range(5):
        step = next_step(
            now=NOW, manifest=a_manifest(), cursor=a_cursor(), state=before, principal_id=BACKFILL
        )
        assert step.action is BackfillAction.YIELD
    assert before.window_for(XERO_MINUTE).count(NOW, 60.0) == 30


# ------------------------------------------------------- it gives way to people (M11.4.8)
def test_a_backfill_yields_while_a_person_asking_would_still_be_admitted() -> None:
    """**The pair is the assertion.** At one moment, with one state, the backfill is told to
    stand down and an ordinary caller is let through. Either half on its own is satisfied by
    a limiter that refuses everybody or one that refuses nobody.

    Half of Xero's minute is spent. The per-principal fair share would happily let the
    backfill take a quarter of the rest, because a fair share is about fairness between
    peers, and somebody watching a cursor blink is not the backfill's peer.

    Delete this and the reserve can be removed with every other test still green."""
    # Thirty, written out rather than read from `backfill_share_of`. A test that derives its
    # own premise from the code under test moves with it: with the reserve set to nothing,
    # a derived figure would fill the window to the new limit and the backfill would still
    # be refused, so the mutation would survive.
    state = window_with(XERO_MINUTE, 30)
    step = next_step(
        now=NOW, manifest=a_manifest(), cursor=a_cursor(), state=state, principal_id=BACKFILL
    )
    assert step.action is BackfillAction.YIELD
    assert step.request is None
    asker = check(now=NOW, limits=source_limits("xero", principal_id=ASKER), state=state)
    assert asker.allowed


def test_a_backfill_over_its_own_share_waits_rather_than_yielding() -> None:
    """The two refusals go to different people, so they are different actions. A backfill at
    its own limit is running as fast as it is allowed to and needs nothing; one that is
    yielding is being held back so somebody else gets the connector first.

    Delete this and a progress page says "waiting" for a week, and nobody can tell whether
    that means the backfill is working or being starved."""
    state = window_with(BACKFILL_SHARE, principal_share_of(60))
    step = next_step(
        now=NOW, manifest=a_manifest(), cursor=a_cursor(), state=state, principal_id=BACKFILL
    )
    assert step.action is BackfillAction.WAIT
    assert 0.0 < step.retry_after_seconds <= 60.0


def test_the_reserve_is_taken_out_of_the_shared_windows_and_not_the_backfills_own() -> None:
    """Reserving headroom inside the backfill's own share would be the backfill holding room
    open against itself: it slows the backfill down and gives nobody anything, because no
    other caller is ever checked against that window.

    Delete this and the reserve gets applied uniformly, which looks simpler and halves the
    backfill's speed for no benefit anybody can name."""
    ordinary = source_limits("xero", principal_id=BACKFILL)
    narrowed = {limit.key: limit.limit for limit in backfill_limits(ordinary)}
    # The numbers rather than the formula, because the numbers are the decision: half of
    # Xero's published 60 a minute and half of its published 5,000 a day are held for people,
    # and the backfill's own quarter-share is untouched.
    assert narrowed[XERO_MINUTE] == 30
    assert narrowed[XERO_DAY] == 2_500
    assert narrowed[BACKFILL_SHARE] == principal_share_of(60)


def test_the_daily_ceiling_is_reserved_too_so_a_backfill_cannot_take_the_whole_day() -> None:
    """The failure `brain.ops.limits.BOTH_LIMITS_APPLY` names first: a single backfill takes
    Xero's whole daily allowance and everybody else's questions fail for the rest of the day.
    A minute-scale reserve does not prevent that at all, because a backfill under the minute
    limit all day still reaches 5,000.

    Delete this and the reserve protects the wrong window on the one source that publishes a
    daily figure."""
    # Half of Xero's published 5,000, written out for the reason the minute test gives.
    state = window_with(XERO_DAY, 2_500, now=NOW)
    step = next_step(
        now=NOW, manifest=a_manifest(), cursor=a_cursor(), state=state, principal_id=BACKFILL
    )
    assert step.action is BackfillAction.YIELD
    assert check(now=NOW, limits=source_limits("xero", principal_id=ASKER), state=state).allowed


@pytest.mark.parametrize("ceiling", sorted({c.per_minute for c in SOURCE_CEILINGS}))
def test_a_backfills_share_always_leaves_room_and_is_never_nothing(ceiling: int) -> None:
    """Both directions, against every ceiling anybody has actually verified. A share that
    rounds to nothing takes the backfill out of service while the connector reads as idle,
    and somebody then raises the reserve looking for the bug; a share equal to the ceiling is
    a reserve that reserves nothing.

    Delete this and the arithmetic can be inverted without a test noticing at the two
    ceilings we have."""
    share = backfill_share_of(ceiling)
    assert 1 <= share < ceiling


def test_half_of_a_shared_window_is_held_for_callers_who_are_waiting() -> None:
    """The reserve is a judgement, so the test states its consequences in numbers rather than
    restating the formula. Against Xero's 60 a minute it keeps 30 open for questions, which
    is five times the estate's whole measured rate of about six questions a minute across 126
    people, on one source.

    Delete this and the reserve can be quietly reduced to a token, which nothing else here
    would notice because every other test only asks that some room is left."""
    assert INTERACTIVE_RESERVE == 0.5
    assert backfill_share_of(60) == 30
    assert backfill_share_of(100) == 50
    assert backfill_share_of(5_000) == 2_500


def test_a_connector_that_serialises_everybody_is_said_so_rather_than_divided() -> None:
    """A ceiling of one has nothing to divide, and `principal_share_of` gives the same answer
    for the same reason. Returning zero would be arithmetically tidy and would take the
    backfill out of service permanently against a connector that is merely very small."""
    assert backfill_share_of(1) == 1
    assert backfill_share_of(2) == 1
    with pytest.raises(ValueError, match="at least 1"):
        backfill_share_of(0)


# --------------------------------------------------------------- it resumes (M11.4.8)
def test_a_backfill_resumes_from_its_cursor_rather_than_from_the_start() -> None:
    """Against a per-day ceiling, restarting after failing three quarters of the way through
    does not cost a quarter more: it costs everything already spent, again, and the calls do
    not come back. The whole position is a value, so resuming is reconstructing it.

    Delete this and the request can be built with an empty cursor, which reads as a
    simplification and quietly makes every retry a full re-run."""
    cursor, _state = run_pages(3)
    assert cursor.cursor == "page_3"
    assert cursor.pages == 3

    resumed = BackfillCursor(
        connector=cursor.connector,
        entity=cursor.entity,
        cursor=cursor.cursor,
        pages=cursor.pages,
        records=cursor.records,
    )
    step = next_step(
        now=NOW,
        manifest=a_manifest(),
        cursor=resumed,
        state=LimiterState(),
        principal_id=BACKFILL,
    )
    assert step.request is not None
    assert step.request.cursor == "page_3"
    assert step.request.entity == "invoice"


def test_a_page_moves_the_cursor_and_the_limiter_in_one_call() -> None:
    """Drift between the two is silent in both directions. A page counted in the cursor and
    not in the limiter is a call nobody can see; a call recorded without the cursor moving is
    a page that will be fetched again, at full cost.

    Delete this and the two can be split into separate calls, and the one that is skipped is
    whichever an exception path skipped."""
    cursor, state = record_page(
        now=NOW,
        cursor=a_cursor(),
        state=LimiterState(),
        limits=backfill_limits(source_limits("xero", principal_id=BACKFILL)),
        returned=100,
        next_cursor="page_1",
        exhausted=False,
    )
    assert (cursor.pages, cursor.records, cursor.cursor) == (1, 100, "page_1")
    assert state.window_for(XERO_MINUTE).count(NOW, 60.0) == 1


def test_a_backfill_that_pages_past_the_end_is_refused() -> None:
    """An advance past an exhausted cursor is a caller that ignored DONE and is about to page
    from the beginning, which is the restart the whole value exists to prevent.

    Delete this and a runner with a `while True` around it spends the ceiling nightly."""
    finished = a_cursor(cursor="", pages=9, records=900, exhausted=True)
    with pytest.raises(ValueError, match="already finished"):
        finished.advance(cursor="page_1", returned=100, exhausted=False)


def test_a_source_that_returns_the_cursor_it_was_given_is_refused_as_a_loop() -> None:
    """A loop against a per-day ceiling spends the whole allowance overnight and projects one
    page. Refused rather than logged, because the thing that notices a logged loop is the
    ceiling being gone in the morning.

    Delete this and a source with an off-by-one in its pagination takes the client's day."""
    cursor = a_cursor(cursor="page_3", pages=3)
    with pytest.raises(ValueError, match="loop"):
        cursor.advance(cursor="page_3", returned=100, exhausted=False)
    with pytest.raises(ValueError, match="loop"):
        cursor.advance(cursor="", returned=100, exhausted=False)


# ------------------------------------------------------- the result ceiling (M11.4.8)
def test_a_search_that_hit_its_ceiling_is_capped_and_never_reported_as_done() -> None:
    """Freshdesk's search returns at most 300 records ever. It is a ceiling, not a page size:
    the source reports the 300th page exactly as it reports a genuinely final one, so the two
    arrive together and whichever is checked first wins.

    Called DONE, the projection then holds 300 of 4,000 tickets and every count over it is
    silently wrong in the direction of reassurance, forever.

    Delete this and the truncation is indistinguishable from success in every test anybody
    writes, because no test has more than 300 matching tickets."""
    manifest = a_manifest(name="freshdesk", ceiling="freshdesk")
    cursor = BackfillCursor(
        connector="freshdesk",
        entity="ticket",
        pages=3,
        records=FRESHDESK_SEARCH_MAX_RECORDS,
        exhausted=True,
    )
    step = next_step(
        now=NOW, manifest=manifest, cursor=cursor, state=LimiterState(), principal_id=BACKFILL
    )
    assert step.action is BackfillAction.CAPPED
    assert step.is_finished
    assert str(FRESHDESK_SEARCH_MAX_RECORDS) in step.reason


def test_a_backfill_that_genuinely_reached_the_end_is_done() -> None:
    """The positive case the one above needs. A CAPPED-for-everything implementation would
    satisfy the truncation test and would mean no backfill ever completes, which reads as a
    connector problem rather than as a bug here."""
    cursor = a_cursor(cursor="", pages=9, records=900, exhausted=True)
    step = next_step(
        now=NOW, manifest=a_manifest(), cursor=cursor, state=LimiterState(), principal_id=BACKFILL
    )
    assert step.action is BackfillAction.DONE
    assert step.is_finished
    assert step.request is None


# ------------------------------------------------------------------ refusing to guess
def test_a_backfill_against_an_unmeasured_source_is_refused() -> None:
    """`throttle.limits_for` refuses a connector that names no verified ceiling rather than
    inventing one, and that refusal matters more here than anywhere else: an unmeasured
    source run in a loop is how a ceiling nobody knew about gets discovered.

    Delete this and the first backfill of a new connector runs at whatever rate the loop
    manages."""
    with pytest.raises(UnmeasuredSourceError):
        next_step(
            now=NOW,
            manifest=a_manifest(name="hubspot", ceiling=""),
            cursor=a_cursor(connector="hubspot", entity="company"),
            state=LimiterState(),
            principal_id=BACKFILL,
        )


def test_a_cursor_belonging_to_another_connector_is_refused() -> None:
    """A backfill resumed against a different connector would page one source using another's
    position, which produces a request the source accepts and answers with the wrong records.
    Nothing downstream can tell, because a cursor is opaque by design."""
    with pytest.raises(ValueError, match="resumed against a different connector"):
        next_step(
            now=NOW,
            manifest=a_manifest(),
            cursor=a_cursor(connector="freshdesk"),
            state=LimiterState(),
            principal_id=BACKFILL,
        )


def test_a_page_of_no_records_is_refused() -> None:
    """Every published ceiling here counts calls rather than records, so a page size of zero
    is a call that costs a call and returns nothing, repeated until the allowance is gone."""
    with pytest.raises(ValueError, match="page of zero"):
        next_step(
            now=NOW,
            manifest=a_manifest(),
            cursor=a_cursor(),
            state=LimiterState(),
            principal_id=BACKFILL,
            page_size=0,
        )
