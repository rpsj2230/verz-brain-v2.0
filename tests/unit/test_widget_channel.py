"""Anonymous widget sessions: what has to be true before one is handed to a stranger.

Every test here is a way a bearer credential gets handed to somebody nobody can name, or a
way one that was already handed out lives longer, reaches further or counts for less than it
should. The module under test refuses far more often than it mints, and most of what follows
is about the refusals.

Three of them are worth naming up front. A session that carried an `EntitlementSet`, even an
empty one, would be an anonymous caller with a reach that can be intersected and cached, and
the first `held or public_set()` written against it invents a public grant. A session that
lived as long as a signed-in one would be a credential with no revocation mechanism at all,
because none of the three that end a real session exists for a browser. And an origin check
that ran after the rate guard would let one caller create a limiter window per invented
origin, in a store keyed on strings they choose.

Task ids: M10.5.5, M23.1.4
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta

import pytest

from brain.audit.ledger import IDENTIFIER
from brain.channels.widget import (
    WIDGET_PROMPT,
    WIDGET_SESSION_ABSOLUTE_MAX,
    WIDGET_SESSION_IDLE,
    RefusedBecause,
    WidgetConfigurationError,
    WidgetRefusal,
    WidgetSession,
    WidgetSessions,
    allowed_origins,
    mint_window,
    new_session_id,
    normalise_origin,
)
from brain.gate.context import Channel
from brain.gate.ingress import Unrecognised
from brain.identity.sessions import SESSION_ABSOLUTE_MAX, SESSION_IDLE
from brain.ops.limits import (
    WIDGET_LIVE_SESSIONS_PER_ORIGIN,
    WIDGET_MINTS_PER_MINUTE,
    LimiterState,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)

ORIGIN = "https://example.com"
OTHER_ORIGIN = "https://shop.example.com"
UNLISTED = "https://not-a-customer.example"


def _registry(*origins: str) -> WidgetSessions:
    return WidgetSessions(allowed=allowed_origins(origins or (ORIGIN,)))


def _mint_many(
    sessions: WidgetSessions,
    count: int,
    *,
    origin: str = ORIGIN,
    start: datetime = NOW,
    step: timedelta = timedelta(seconds=10),
) -> list[WidgetSession]:
    """Mint `count` sessions, spaced far enough apart that the mint rate never binds.

    Ten seconds apart puts at most six mints in any sixty-second window against a limit of
    ten, so these tests exercise `WIDGET_MINTS_PER_MINUTE` as configured rather than a number
    invented to make a test pass.
    """
    minted: list[WidgetSession] = []
    for index in range(count):
        result = sessions.mint(origin=origin, now=start + step * index)
        assert isinstance(result, WidgetSession), result
        minted.append(result)
    return minted


def _fill_mint_window(origin: str, count: int, *, now: datetime = NOW) -> LimiterState:
    """A limiter state with `count` mints already recorded against `origin`, all in window."""
    limit = mint_window(origin)
    state = LimiterState()
    for offset in range(count):
        state = state.record(now - timedelta(seconds=offset), (limit,))
    return state


# --------------------------------------------------------------------------- the happy path
def test_an_allowed_origin_under_both_guards_gets_a_session() -> None:
    """The one test a change that refused every mint would still pass without. Every other
    test here asserts that something is refused, so with this deleted a module that returned
    a refusal unconditionally would look correct."""
    sessions = _registry()

    minted = sessions.mint(origin=ORIGIN, now=NOW)

    assert isinstance(minted, WidgetSession)
    assert minted.origin == ORIGIN
    assert minted.opened_at == NOW
    assert minted.is_live(NOW)
    assert sessions.get(minted.session_id, NOW) is minted


def test_two_sessions_never_share_an_id() -> None:
    """A collision is one visitor holding another visitor's conversation, and the widget has
    no second factor, no principal and no cookie signature to catch it: the id is the whole
    credential. Deleting this leaves a change from `secrets` to a counter, a timestamp or a
    per-origin sequence undetected, and every one of those is guessable."""
    assert len({new_session_id() for _ in range(1_000)}) == 1_000


def test_a_minted_session_carries_no_entitlement_set_at_all() -> None:
    """The rule the whole module exists to keep. An empty `EntitlementSet` would be a thing
    that could be intersected, hashed into a cache key and passed along, and the first person
    to write `held or public_set()` against it would have invented a public grant that nobody
    reviewed.

    Asserted against the type as well as the instance, because the regression is somebody
    adding a field, not somebody populating one: a `WidgetSession` with an `entitlement`
    field defaulting to an empty set would pass an instance-only check on the day it was
    written and be full of grants a release later."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    assert isinstance(minted.reach, Unrecognised)
    assert not hasattr(minted.reach, "grants")
    assert not hasattr(minted.reach, "entitlement")

    annotations = " ".join(str(f.type) for f in dataclasses.fields(WidgetSession))
    assert "EntitlementSet" not in annotations
    assert "Grant" not in annotations
    # No principal either. A stored id for a website visitor is a principal table full of
    # personal data belonging to people who never identified themselves.
    assert not any("principal" in f.name for f in dataclasses.fields(WidgetSession))


def test_a_widget_visitor_is_not_told_to_sign_in_to_a_console() -> None:
    """`ingress.UNRECOGNISED_PROMPT` is worded for somebody whose phone number we do not
    recognise: it tells them to sign in and add the channel from their profile. A website
    visitor has no profile and no console account, so that instruction is unfollowable, and
    an unfollowable instruction reads to the visitor as the product being broken."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    assert minted.reach.prompt == WIDGET_PROMPT
    assert "profile" not in minted.reach.prompt
    # `Channel.WIDGET`, not the `Channel.API` stand-in this module first used. The traffic
    # class is the reason: API is AUTOMATION, and anything degrading by traffic class would
    # queue an answer for somebody watching a cursor blink.
    assert minted.reach.channel is Channel.WIDGET


# -------------------------------------------------------------------------- the rate guard
def test_nothing_is_minted_once_the_mint_rate_guard_refuses() -> None:
    """A script minting sessions in a loop is the fast version of widget abuse, and every
    session it opens is another door onto the answer path. Deleting this leaves a module that
    calls `limits.mint_widget_session`, ignores the answer and mints anyway, which is the
    exact shape the leaf was left open for."""
    sessions = _registry()
    _mint_many(sessions, WIDGET_MINTS_PER_MINUTE, step=timedelta(seconds=1))

    refused = sessions.mint(origin=ORIGIN, now=NOW + timedelta(seconds=10))

    assert isinstance(refused, WidgetRefusal)
    assert refused.because is RefusedBecause.OVER_LIMIT
    assert "is at its limit of" in refused.reason
    assert refused.retry_after_seconds > 0


def test_the_window_a_mint_records_into_is_the_one_the_rate_guard_reads() -> None:
    """`limits.mint_widget_session` builds its own window and does not hand it back, and it
    records nothing, so this module has to name the same window to write the admitted hit
    into it. If the two names disagree the rate limit silently never binds: every hit is
    written where nobody looks, the guard passes every test about its own arithmetic, and it
    stops nothing in production.

    Pinned from outside rather than by comparing keys, so it tests the behaviour rather than
    restating the construction: fill the window this module records into, and the guard must
    then refuse."""
    sessions = WidgetSessions(
        allowed=allowed_origins([ORIGIN]),
        windows=_fill_mint_window(ORIGIN, WIDGET_MINTS_PER_MINUTE),
    )

    refused = sessions.mint(origin=ORIGIN, now=NOW)

    assert isinstance(refused, WidgetRefusal)
    assert refused.because is RefusedBecause.OVER_LIMIT


def test_a_mint_the_rate_guard_refused_does_not_extend_the_window() -> None:
    """`limits.REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW`. Recording a refusal pushes the
    retry time further away every time a client obeys the hint it was given, so a
    sixty-second limit becomes a permanent lockout and the hint becomes a lie the client
    cannot detect. Deleting this lets a `record` call move above the `minted` check, which
    reads as a tidy-up."""
    sessions = WidgetSessions(
        allowed=allowed_origins([ORIGIN]),
        windows=_fill_mint_window(ORIGIN, WIDGET_MINTS_PER_MINUTE),
    )
    before = sessions.windows

    for offset in range(5):
        assert isinstance(
            sessions.mint(origin=ORIGIN, now=NOW + timedelta(seconds=offset)), WidgetRefusal
        )

    assert sessions.windows is before


# ------------------------------------------------------------------ the live-session ceiling
def test_nothing_is_minted_once_the_live_session_ceiling_is_reached() -> None:
    """The slow version of the same abuse, and the one the rate guard cannot see: a script
    opening one session a minute all day never trips a rate limit and still ends up holding
    every session the origin is allowed. Deleting this lets the live count be computed wrong,
    or not at all, while the rate test above still passes."""
    sessions = _registry()
    held = _mint_many(sessions, WIDGET_LIVE_SESSIONS_PER_ORIGIN)
    at = NOW + timedelta(seconds=10) * WIDGET_LIVE_SESSIONS_PER_ORIGIN

    refused = sessions.mint(origin=ORIGIN, now=at)

    assert isinstance(refused, WidgetRefusal)
    assert refused.because is RefusedBecause.OVER_LIMIT
    # `limits` words this refusal itself, and its wording is what tells an operator which of
    # the two guards fired. Asserting on it is asserting that the distinction survives.
    assert "live sessions" in refused.reason
    assert sessions.live(ORIGIN, at) == len(held)


def test_a_minted_session_counts_towards_its_own_origins_ceiling() -> None:
    """The ceiling is only a ceiling if minting raises the count. A `mint` that produced a
    session without registering it would pass every other test in this file and let one
    origin hold sessions without limit, because the number handed to the guard would never
    move off nought."""
    sessions = _registry()

    assert sessions.live(ORIGIN, NOW) == 0
    _mint_many(sessions, 3)
    assert sessions.live(ORIGIN, NOW + timedelta(seconds=30)) == 3


def test_one_origins_sessions_do_not_fill_anothers_ceiling() -> None:
    """A shared count means one busy customer's widget takes every other customer's widget
    off the air, and the outage looks like a bug in a site nobody has touched."""
    sessions = _registry(ORIGIN, OTHER_ORIGIN)
    _mint_many(sessions, WIDGET_LIVE_SESSIONS_PER_ORIGIN)
    at = NOW + timedelta(seconds=10) * WIDGET_LIVE_SESSIONS_PER_ORIGIN

    assert sessions.live(OTHER_ORIGIN, at) == 0
    assert isinstance(sessions.mint(origin=OTHER_ORIGIN, now=at), WidgetSession)


def test_an_expired_session_stops_counting_towards_its_origins_ceiling() -> None:
    """Without pruning, a live count only ever rises: an origin reaches its ceiling once,
    every session it is counting dies, and it never mints again. Nothing raises, nothing is
    logged, and the widget on that site simply stops working."""
    sessions = _registry()
    _mint_many(sessions, WIDGET_LIVE_SESSIONS_PER_ORIGIN)
    later = NOW + WIDGET_SESSION_IDLE + timedelta(minutes=5)

    assert sessions.live(ORIGIN, later) == 0
    assert isinstance(sessions.mint(origin=ORIGIN, now=later), WidgetSession)


# ---------------------------------------------------------------------- the origin allowlist
def test_an_origin_that_is_not_on_the_allowlist_gets_nothing() -> None:
    """A widget is embedded on a named site. Without this, the mint endpoint issues anonymous
    credentials against a customer's brain to any page on the internet that copies the embed
    snippet, and the only thing standing in the way is a CORS header, which is a browser
    convenience and not a control."""
    sessions = _registry()

    refused = sessions.mint(origin=UNLISTED, now=NOW)

    assert isinstance(refused, WidgetRefusal)
    assert refused.because is RefusedBecause.ORIGIN_NOT_ALLOWED
    assert refused.retry_after_seconds == 0.0


def test_an_unlisted_origin_reads_and_creates_no_rate_window() -> None:
    """The ordering, which is a security property and not a preference. `Origin` is whatever
    the sender typed and the mint window is keyed on it, so a rate check that ran first would
    let one caller create a limiter window per invented origin: unbounded keys in a shared
    store, all named by strings the attacker chose. Deleting this lets the allowlist check
    drift below the limiter call, which reads in a diff as putting the cheap check second."""
    sessions = _registry()

    for index in range(50):
        refused = sessions.mint(origin=f"https://probe-{index}.example", now=NOW)
        assert isinstance(refused, WidgetRefusal)

    assert not sessions.windows.windows


def test_a_refusal_never_repeats_the_origin_header_back_into_a_log() -> None:
    """`Origin` is attacker-controlled and the refusal reason reaches a log line. A newline
    in an echoed header forges a second log entry, which is how an audit trail acquires
    events nobody caused. Deleting this makes `reason=f"{origin} is not allowed"` look
    obviously fine."""
    sessions = _registry()

    refused = sessions.mint(origin="https://ok.example/\nlevel=critical widget=disabled", now=NOW)

    assert isinstance(refused, WidgetRefusal)
    assert "\n" not in refused.reason
    assert "critical" not in refused.reason
    assert "(not an origin)" in refused.reason


def test_a_wildcard_allowlist_entry_is_refused_outright() -> None:
    """`config.check` refuses a `cors_origins` of exactly `"*"`, only in production, and
    `serve.py` hands it the setting comma-joined, so `"*,https://console.example"` passes that
    check. For CORS that is a browser convenience; here it is permission for every site on the
    internet to mint anonymous credentials against a customer's brain, so it is refused per
    entry in every environment."""
    with pytest.raises(WidgetConfigurationError, match=r"may not contain"):
        allowed_origins(["https://example.com", "*"])
    with pytest.raises(WidgetConfigurationError, match=r"may not contain"):
        allowed_origins([" * "])


def test_the_null_origin_cannot_be_configured_or_matched() -> None:
    """`null` is what a sandboxed iframe, a `file://` document and some cross-origin redirects
    send, and it is the absence of an origin rather than a value for one. An allowlist entry
    matching it would admit every sandboxed frame on the internet while looking like an
    ordinary line of configuration."""
    assert normalise_origin("null") == ""
    assert normalise_origin("NULL") == ""
    with pytest.raises(WidgetConfigurationError, match=r"is not an origin"):
        allowed_origins(["null"])

    sessions = _registry()
    assert isinstance(sessions.mint(origin="null", now=NOW), WidgetRefusal)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/embed",
        "https://example.com?a=1",
        "https://example.com#x",
        "https://user:pw@example.com",
        "javascript:alert(1)",
        "//example.com",
        "example.com",
        "https://example.com:99999",
        "   ",
    ],
)
def test_a_string_that_is_not_a_serialised_origin_normalises_to_nothing(value: str) -> None:
    """An origin is a scheme, a host and an optional port and nothing else (RFC 6454). A path
    that survived normalisation would go into the limiter subject, and `/1`, `/2`, `/3` are
    three subjects and three windows: the mint rate for a site becomes however many paths
    somebody cares to invent. The rest are the shapes a client can send that look close
    enough to an origin to be accepted by a lenient comparison."""
    assert normalise_origin(value) == ""


def test_one_site_spelled_two_ways_is_one_allowlist_entry_and_one_window() -> None:
    """Scheme and host are case-insensitive, so `HTTPS://Example.COM` is the same origin. If
    the two normalised differently they would be two limiter subjects, and varying the case
    of a host would reset the mint rate at will. The allowlist half is the milder version of
    the same bug: a configured origin with a capital letter would refuse the site it names.

    A trailing slash is accepted and dropped for the same reason from the other direction:
    browsers never send one and operators type them, and refusing a configured
    `https://example.com/` presents a typo to its author as an attack."""
    assert allowed_origins(["HTTPS://Example.COM/"]) == frozenset({ORIGIN})

    sessions = _registry()
    _mint_many(sessions, WIDGET_MINTS_PER_MINUTE, step=timedelta(seconds=1))
    refused = sessions.mint(origin="HTTPS://EXAMPLE.com", now=NOW + timedelta(seconds=10))

    assert isinstance(refused, WidgetRefusal)
    assert refused.because is RefusedBecause.OVER_LIMIT


def test_the_two_refusals_are_told_apart_without_naming_the_site() -> None:
    """The considered departure from `ingress.UNRECOGNISED_PROMPT`. One prompt for every
    refusal would conceal only whether a given website embeds this widget, which that website
    publishes in plain HTML, while costing every misconfigured embed a correct diagnosis: the
    site owner waits for a rate window that will never open instead of fixing an allowlist.

    What must stay true is the other half. Neither sentence names an origin, a count or a
    ceiling, because a refusal is rendered into a stranger's browser and the numbers belong to
    the customer. Deleting this lets the two messages collapse into one, or lets the operator
    reason be used as the public one."""
    sessions = _registry()
    unlisted = sessions.mint(origin=UNLISTED, now=NOW)
    _mint_many(sessions, WIDGET_MINTS_PER_MINUTE, step=timedelta(seconds=1))
    over = sessions.mint(origin=ORIGIN, now=NOW + timedelta(seconds=10))

    assert isinstance(unlisted, WidgetRefusal)
    assert isinstance(over, WidgetRefusal)
    assert unlisted.public_message != over.public_message
    for refusal in (unlisted, over):
        assert ORIGIN not in refusal.public_message
        assert UNLISTED not in refusal.public_message
        assert str(WIDGET_MINTS_PER_MINUTE) not in refusal.public_message
    # The operator's copy does carry the detail, which is the point of there being two.
    assert ORIGIN in over.reason


# ------------------------------------------------------------------------------- lifetimes
def test_an_anonymous_session_expires_sooner_than_a_signed_in_one() -> None:
    """Ten hours is defensible for somebody who authenticated because three separate
    mechanisms can end that session early: the logout floor in `SessionRegistry`, deactivating
    the principal, and the identity provider declining the next refresh. An anonymous session
    has none of them. There is no principal id to key a floor on, the session id is itself the
    credential, and there is nothing to re-ask. A credential whose only expiry is the clock
    needs a clock that runs out, and deleting this lets these numbers be raised to the
    signed-in ones by somebody who reads them as inconsistent."""
    assert WIDGET_SESSION_ABSOLUTE_MAX < SESSION_ABSOLUTE_MAX
    assert WIDGET_SESSION_IDLE < SESSION_IDLE

    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    assert minted.absolute_expiry - minted.opened_at < SESSION_ABSOLUTE_MAX
    assert minted.expires_at - minted.opened_at == WIDGET_SESSION_IDLE
    assert not minted.is_live(NOW + SESSION_ABSOLUTE_MAX)


def test_a_session_may_not_be_constructed_beyond_the_anonymous_bound() -> None:
    """The bound has to bind on the type, not only on the one function that mints. A caller
    passing a longer `absolute` to `mint`, or constructing a session directly in some later
    endpoint, would otherwise get a ten-hour anonymous credential with nothing complaining."""
    with pytest.raises(ValueError, match=r"may run at most"):
        WidgetSession(
            session_id=new_session_id(),
            origin=ORIGIN,
            opened_at=NOW,
            expires_at=NOW + WIDGET_SESSION_IDLE,
            absolute_expiry=NOW + SESSION_ABSOLUTE_MAX,
            reach=Unrecognised(channel=Channel.API, prompt=WIDGET_PROMPT),
        )


def test_no_amount_of_use_pushes_a_session_past_its_absolute_expiry() -> None:
    """Sliding an idle window on every message is right and is not enough: refreshed often
    enough the window never closes, and a session opened at nine in the morning is still
    authenticating requests at midnight. The absolute bound is what makes
    `WIDGET_SESSION_ABSOLUTE_MAX` a bound rather than a suggestion, and deleting this lets
    `min(now + idle, absolute_expiry)` become `now + idle`, which reads as a simplification."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    step = timedelta(minutes=10)
    at = NOW + step
    while at < NOW + WIDGET_SESSION_ABSOLUTE_MAX:
        refreshed = sessions.touch(minted.session_id, at)
        assert refreshed is not None, at
        assert refreshed.expires_at <= refreshed.absolute_expiry
        assert refreshed.absolute_expiry == minted.absolute_expiry
        at += step

    assert sessions.touch(minted.session_id, NOW + WIDGET_SESSION_ABSOLUTE_MAX) is None
    assert sessions.get(minted.session_id, NOW + WIDGET_SESSION_ABSOLUTE_MAX) is None


def test_an_idle_session_dies_without_being_touched() -> None:
    """The idle window is the whole defence against a tab left open on a machine nobody at
    this company administers. Deleting this lets `expires_at` be set from the absolute bound
    instead of the idle one, which looks like one fewer field to keep straight."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    assert sessions.get(minted.session_id, NOW + WIDGET_SESSION_IDLE - timedelta(minutes=1))
    assert sessions.get(minted.session_id, NOW + WIDGET_SESSION_IDLE) is None


def test_reading_a_session_does_not_slide_its_idle_window() -> None:
    """Same argument as `SessionRegistry.admit`: a window that slid on every read means a
    poller keeps a session alive with nobody at the keyboard, which is the failure the idle
    window exists for. Deleting this lets `get` quietly become `touch`, and the idle window
    then only ever expires for a visitor who has already left."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    for offset in range(1, 15):
        assert sessions.get(minted.session_id, NOW + timedelta(minutes=offset)) is not None

    assert sessions.get(minted.session_id, NOW + WIDGET_SESSION_IDLE) is None


# --------------------------------------------------------------------------------- the audit
def test_a_session_id_matches_the_audit_ledgers_identifier_pattern() -> None:
    """`ledger.actor_id` is validated against `IDENTIFIER`, so an id outside that grammar is a
    session whose every action fails at the audit write, after the work is done and with
    nothing recorded about it. Checked at construction rather than at the write for exactly
    that reason, and deleting this lets the id gain a colon, a slash or a `+` from a change of
    encoding that looks cosmetic."""
    for _ in range(50):
        assert re.match(IDENTIFIER, new_session_id())

    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)
    assert re.match(IDENTIFIER, minted.session_id)


def test_a_session_id_the_ledger_would_refuse_cannot_be_constructed() -> None:
    """The check has to be on the type, because the point is to fail before any work is done
    rather than at the audit write afterwards. Deleting this leaves the grammar agreeing with
    the ledger only by the happy accident that base64url is a subset of it."""
    with pytest.raises(ValueError, match=r"identifier grammar"):
        WidgetSession(
            session_id="ws:1/2",
            origin=ORIGIN,
            opened_at=NOW,
            expires_at=NOW + WIDGET_SESSION_IDLE,
            absolute_expiry=NOW + WIDGET_SESSION_ABSOLUTE_MAX,
            reach=Unrecognised(channel=Channel.API, prompt=WIDGET_PROMPT),
        )


def test_the_audit_actor_is_the_session_id_and_never_a_fabricated_person() -> None:
    """A constant `p_anonymous`, or a synthetic per-visitor id, would put a name in the actor
    column of an append-only chain whose whole purpose is to answer "who did this". A false
    attribution there is worse than an honest non-person: the chain is what somebody reaches
    for when the question is serious. Deleting this lets the actor become something
    principal-shaped, which is the convenient thing to do the first time a caller wants to
    join the audit view to the principal table."""
    sessions = _registry()
    minted = sessions.mint(origin=ORIGIN, now=NOW)
    assert isinstance(minted, WidgetSession)

    assert minted.audit_actor == minted.session_id
    assert minted.audit_actor.startswith("ws_")
    assert re.match(IDENTIFIER, minted.audit_actor)


# ------------------------------------------------------------------- refusals change nothing
def test_refusing_to_mint_leaves_every_existing_session_untouched() -> None:
    """Refusing to hand out another credential is the only reason this guard is allowed to
    refuse at all, per `limits.MintDecision`: it costs an anonymous stranger a new session and
    costs nobody an existing one. A refusal that evicted, expired or renumbered live sessions
    would turn a rate limit into an outage for everybody already mid-conversation, and would
    make the ceiling self-clearing, so a caller at the ceiling could empty it by asking
    again."""
    sessions = _registry()
    held = _mint_many(sessions, WIDGET_LIVE_SESSIONS_PER_ORIGIN)
    at = NOW + timedelta(seconds=10) * WIDGET_LIVE_SESSIONS_PER_ORIGIN

    for origin in (ORIGIN, UNLISTED, "null"):
        assert isinstance(sessions.mint(origin=origin, now=at), WidgetRefusal)

    assert sessions.live(ORIGIN, at) == len(held)
    for session in held:
        assert sessions.get(session.session_id, at) == session
