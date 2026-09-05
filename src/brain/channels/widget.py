"""A session for somebody the system has never heard of, and the several ways of refusing one.

Every other caller here arrives carrying an identity: a token, a channel binding, a service
account. A website visitor arrives with none, and the widget still has to hand them
something to hold for the length of a conversation. That something is a bearer credential
minted for nobody in particular, which is why most of this module is about not minting one.

Six decisions are load-bearing.

**An anonymous visitor is not a stored principal.** No `PrincipalRow`, no `PrincipalKind`
member, no row anywhere keyed to a person. A row per website visitor turns the principal
table into a website analytics table, and every row in it is personal data belonging to
somebody who never identified themselves: retention, subject access and erasure obligations
incurred for a stranger who asked about opening hours. The session is the only artefact it
produces, it names a browser rather than a person, and it dies within the hour.

**The session carries `gate.ingress.Unrecognised`, which holds no `EntitlementSet` at all.**
Not an empty one. Entitlements here are additive: `gate.admission.admit` intersects and
never unions, and `identity.roles.NoStandingEntitlement` exists precisely so a caller who
holds nothing cannot be cached, hashed or intersected as a caller who holds zero grants. An
anonymous caller therefore holds exactly what has been granted to anonymous callers, which
is nothing, and there is no object representing nothing. An empty set would be a thing that
could be passed along, and the first person to write `held or public_set()` would have
invented a public grant that nobody reviewed. Rejected for the same reason `Unrecognised`
was written that way to begin with. Note that `ASSURANCE_VERBS[Assurance.UNVERIFIED]` is
already empty, so even a wired-up anonymous caller narrows to nothing; the absence here is
belt and braces on a rule that is stated twice on purpose.

**The audit actor is the session id, and it is not dressed up as a person.** See
`WidgetSession.audit_actor`.

**An anonymous session is shorter than a signed-in one, because nothing except the clock
can end it.** `identity.sessions` gives a person who authenticated thirty idle minutes and
ten absolute hours, and it can afford to: that session has a principal behind it, so
`SessionRegistry.end_all_for` can raise a not-before floor for them, an operator can
deactivate them, and the identity provider can be asked at each refresh whether they still
exist. None of those three remedies exists here. There is no principal to suspend, the
logout floor is keyed by principal id so there is nothing to key one on, and there is no
provider to ask. When the clock is the only thing that ends a session, the clock has to be
short. See `WIDGET_SESSION_IDLE` and `WIDGET_SESSION_ABSOLUTE_MAX` for the numbers and the
argument for each. Deliberately there is no `end` method: a visitor who closes the tab tells
us nothing, so an ending we cannot observe must not be something the design relies on.

**The origin is checked before the limiter is, and that ordering is a security property.**
`Origin` is a header, and a header is whatever the sender typed; `curl` will send any value
at all. The mint window in `ops.limits` is keyed on the origin, so checking the rate first
would let one caller create a window per invented origin, in a store that is keyed on
attacker-chosen strings and expires on its own schedule. An unlisted origin therefore
touches nothing: no window is read, none is created, and nothing is recorded.

**The two refusals are told apart, and that is a considered departure from
`gate.ingress.UNRECOGNISED_PROMPT`.** There, one prompt answers every unrecognised sender
because the thing being concealed is whether a phone number belongs to somebody at this
company, which is not published anywhere and which is exactly the question an attacker
holding a stolen handset is asking. Here the fact in question is whether a given website
embeds this widget, and that fact is published by the website itself in plain HTML, echoed
back by `Access-Control-Allow-Origin` on every response, and visible to anyone who loads the
page. A refusal that concealed it would conceal nothing, and it would cost something real:
every misconfigured embed would present to the site owner as a rate limit, sending them to
wait for a window that will never open instead of to their allowlist. So the visitor is told
which of the two happened, in one sentence that names no origin and no number. What is *not*
distinguishable is anything about who is behind the widget, because there is nobody: nothing
was claimed, so there is nothing to confirm or deny. That argument depends on the allowlist
holding only origins the customer has published; an entry for an unpublished staging host
would be a secret in a list that is otherwise public, and this reasoning would not cover it.

Nothing here opens a connection and nothing here re-implements a guard. The mint rate and
the live-session ceiling are both decided by `ops.limits.mint_widget_session`; this module
supplies the inputs, consumes the answer, and records the hit that the answer admitted.

Task ids: M10.5.5, M23.1.4
"""

from __future__ import annotations

import enum
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

from brain.audit.ledger import IDENTIFIER
from brain.gate.context import Channel
from brain.gate.ingress import Unrecognised
from brain.ops.limits import (
    MINUTE_SECONDS,
    WIDGET_LIVE_SESSIONS_PER_ORIGIN,
    WIDGET_MINTS_PER_MINUTE,
    Limit,
    LimiterState,
    LimitScope,
    mint_widget_session,
)

# ------------------------------------------------------------------ written-down reasons
#: Why the anonymous numbers are not the signed-in ones. Kept as a statement rather than a
#: runtime check, for the reason `limits.PRINCIPAL_FAIR_SHARE` is: the relationship is pinned
#: by a test that names both constants, so raising one produces a failure saying which rule
#: was broken rather than an import error in every module that touches a widget.
ANONYMOUS_SESSIONS_ARE_SHORTER = (
    "The signed-in numbers in identity.sessions can afford to be generous because such a "
    "session can be ended three other ways: a logout floor keyed on the principal, "
    "deactivating the principal, and the identity provider declining the next refresh. None "
    "of those exists for a browser nobody can name. There is no principal id to key a floor "
    "on, the session id is itself the credential, and there is nothing to re-ask at refresh "
    "time. When the clock is the only mechanism that ends a credential, the clock has to run "
    "out, so both anonymous windows are strictly shorter than their signed-in counterparts."
)

#: Why the allowlist is consulted before anything reads or writes a limiter window.
ORIGIN_IS_CHECKED_BEFORE_THE_LIMITER = (
    "An Origin header is whatever the sender typed, and the mint window in ops.limits is "
    "keyed on it. A rate check that ran first would let one caller create a window per "
    "invented origin, so the store would fill with keys named by strings the attacker chose "
    "and nothing would have refused a single request. So an origin that is not on the "
    "allowlist touches nothing: no window is read, none is created, and nothing is recorded."
)

#: Why this module tells its two refusals apart where gate.ingress deliberately does not.
THE_TWO_REFUSALS_ARE_TOLD_APART = (
    "gate.ingress answers every unrecognised sender with one prompt because the thing being "
    "concealed is whether a phone number belongs to somebody here, which is published "
    "nowhere. The fact concealed by merging these two refusals would be whether a given "
    "website embeds this widget, which that website publishes itself in plain HTML and which "
    "Access-Control-Allow-Origin echoes on every response. Concealing it protects nothing and "
    "costs every misconfigured embed a correct diagnosis: the site owner waits for a rate "
    "window that will never open instead of fixing an allowlist. Nothing about who is behind "
    "the widget is disclosed either way, because nobody is: nothing was claimed, so there is "
    "nothing to confirm or deny. The argument holds only while the allowlist contains origins "
    "the customer has published; an unlisted staging host would be a secret in a public list."
)


# ------------------------------------------------------------------------------- lifetimes
# The two numbers below are chosen *against* `identity.sessions.SESSION_IDLE` and
# `SESSION_ABSOLUTE_MAX`, and neither is imported here. An import used only to make a comment
# read well is noise, and a copy of "thirty minutes" written into one would stop being a
# comparison the moment somebody changed the original. The comparison lives where it can
# fail: `test_widget_channel` imports both pairs and asserts the relationship.
#: How long an anonymous session survives with nothing happening on it. Half the signed-in
#: window, and the halving is an argument rather than a round number.
#:
#: `SESSION_IDLE` is thirty minutes because a person who authenticated may read a long answer,
#: go to a meeting and come back to the same question. A website visitor does not come back:
#: they either type again within a minute or two or they close the tab, and the tab they left
#: open is on a machine nobody at this company administers, in a browser we cannot log out.
#: Fifteen minutes is comfortably longer than any real gap between two messages in a website
#: chat and short enough that an abandoned tab stops holding a credential over lunch.
WIDGET_SESSION_IDLE: Final = timedelta(minutes=15)

#: The longest an anonymous session may live however often it is used. One hour against the
#: signed-in ten.
#:
#: Ten hours is defensible for a person because it is one long working day *and* because that
#: session can be ended early by three independent mechanisms: the logout floor in
#: `identity.sessions.SessionRegistry`, deactivating the principal, and the identity provider
#: declining the next refresh. An anonymous session has none of them. It cannot be revoked
#: individually, because there is no principal id to key a floor on and the id is the
#: credential; it cannot be attributed, so nobody can be told their session was ended; and
#: there is nothing to re-ask at refresh time. A credential whose only expiry mechanism is the
#: clock has to be given a clock that runs out, and an hour is already several times the
#: longest plausible conversation with a website widget.
#:
#: The ratio is also deliberate. Signed in, the absolute bound is twenty idle windows, so it
#: is a genuine backstop that almost never bites. Here it is four, so a session kept alive by
#: a script sending a keystroke every fourteen minutes is gone within the hour rather than at
#: closing time. That is the abuse shape the live-session ceiling in `ops.limits` cannot see,
#: because a session held open costs nothing per minute and trips no rate.
WIDGET_SESSION_ABSOLUTE_MAX: Final = timedelta(hours=1)

#: Bytes of entropy in a session id. Twice `ingress.NONCE_BYTES`, and the difference is what
#: the value is for. A binding nonce is one-time, lives ten minutes, and is compared against a
#: specific principal's record, so guessing one is useless without also knowing whose it is.
#: A widget session id is the entire credential: it is what the holder presents, it is what
#: the audit chain records, and there is nothing else to check it against. 256 bits costs
#: eleven characters and removes the question.
WIDGET_SESSION_ID_BYTES: Final = 32

#: Marks a widget session id as one. Present so an operator reading an audit row can see at a
#: glance that the actor was a browser and not a person, without looking anything up.
SESSION_ID_PREFIX: Final = "ws_"

_IDENTIFIER_RE = re.compile(IDENTIFIER)

#: What a widget visitor is told, in place of `ingress.UNRECOGNISED_PROMPT`.
#:
#: The ingress prompt says to sign in to the console and add this channel from a profile,
#: which is the right instruction for somebody whose phone number we do not recognise and the
#: wrong one for a website visitor, who has no profile, no console account and nothing to add.
#: The ingress prompt is also worded to conceal whether a number is known to us, and that
#: question does not arise here: nothing was claimed, so there is nothing to confirm or deny.
#: This one therefore says the true thing plainly.
WIDGET_PROMPT: Final = (
    "I can answer from what this site publishes. Anything about a specific account needs a "
    "signed-in session, which this chat window is not."
)


# --------------------------------------------------------------------------------- origins
#: The schemes a browser will ever put in an `Origin` header for a page that can embed this.
#:
#: `http` is admitted rather than refused. A widget served over plain HTTP puts its session id
#: on the wire in clear, which is a real problem and is not this function's to solve: the
#: allowlist is the control, `http://localhost:3000` is how anybody develops against this, and
#: refusing the scheme here would push a developer into turning the allowlist off, which is
#: strictly worse. A production allowlist containing an `http` entry is worth an operator's
#: attention, and it is visible in the configuration where somebody can see it.
_ORIGIN_SCHEMES: Final = frozenset({"http", "https"})

#: An allowlist entry that would match every site on the internet.
WILDCARD: Final = "*"


class WidgetConfigurationError(Exception):
    """Raised when the widget is wired up in a way that cannot be made safe.

    Not a `BrainError` and not a `WidgetRefusal`, for the reason
    `channels.adapter.DeliveryRefusedError` is neither: it is not an outcome of a request. A
    visitor did nothing wrong and there is nothing they can retry. Reporting it as a refusal
    would put a wiring fault into the same bucket as a busy minute and hide it in whatever
    counts refusals.
    """


def normalise_origin(value: str) -> str:
    """One origin as a comparable string, or `""` for anything that is not an origin.

    Returns rather than raises, because an unparseable `Origin` is a request outcome and not a
    programming error: any client can send one, and the answer is the refusal an unlisted
    origin gets. `WidgetConfigurationError` is reserved for what an operator did.

    Three properties, each with teeth.

    **No path, query or fragment.** A serialised origin is scheme, host and port and nothing
    else (RFC 6454), so `https://example.com/a` is not an origin. Accepting one would put an
    attacker-chosen path into the limiter subject, and `https://a.com/1`, `/2` and `/3` are
    three subjects and three windows: the mint rate for that site becomes however many paths
    somebody cares to invent. A bare trailing slash is allowed and dropped, because browsers
    never send one and operators type them, and refusing a configured
    `https://example.com/` would present a typo as an attack.

    **Case-folded scheme and host.** Both are case-insensitive, so `HTTPS://Example.COM` and
    `https://example.com` are one origin. If they normalised differently they would be two
    allowlist answers and, worse, two limiter subjects, which is the same window bypass
    spelled differently.

    **`null` is refused explicitly.** It is what a sandboxed iframe, a `file://` document and
    some cross-origin redirects send, and it is the *absence* of an origin rather than a value
    for one. An allowlist entry that matched it would admit every sandboxed frame on the
    internet at once, and it would look like an ordinary entry in the configuration.

    Two of those three are currently stated twice, and mutation testing said so rather than a
    reviewer: deleting the `null` branch and deleting the case folding both left every test
    passing. Neither is dead by accident and neither has been removed. See the comments at
    each, which record what the other statement of the rule is and what would make this one
    load-bearing again.
    """
    text = value.strip()
    # The `null` half of this test is redundant today: `urlsplit("null")` yields an empty
    # scheme, so the scheme check below already refuses it. Kept because `Origin: null` is a
    # specific, named thing a reviewer looks for here, and because it is what still refuses it
    # if a later edit ever admits a schemeless origin for developer convenience. A surviving
    # mutant, recorded rather than papered over with a test that cannot fail.
    if not text or text.casefold() == "null":
        return ""
    parts = urlsplit(text)
    if parts.scheme.casefold() not in _ORIGIN_SCHEMES:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return ""
    if parts.username or parts.password:
        # `https://user:pw@example.com` carries a credential into the limiter key and into
        # every log line that names the origin, and no browser sends one.
        return ""
    try:
        port = parts.port
    except ValueError:
        # `.port` raises on a port outside 1-65535. A malformed authority is not an origin.
        return ""
    host = parts.hostname
    if not host:
        return ""
    suffix = f":{port}" if port is not None else ""
    # Both folds are redundant today, and mutation testing is what established that rather
    # than a reading of the code: `urlsplit` lowercases the scheme and `SplitResult.hostname`
    # lowercases the host, so removing them changes nothing and no test noticed. Kept because
    # the rule belongs where somebody looks for it. Inheriting case-insensitivity from another
    # module's documented behaviour is how it stops holding without anything saying so, and
    # what it protects is not cosmetic: two spellings of one host would be two limiter
    # subjects, so varying the case of a host would reset the mint rate at will.
    return f"{parts.scheme.casefold()}://{host.casefold()}{suffix}"


def allowed_origins(configured: Sequence[str]) -> frozenset[str]:
    """The configured origins, normalised once, with a wildcard refused outright.

    Normalised at configuration time rather than at each mint, so the comparison at mint time
    is a set membership on two strings that were produced by the same function. Comparing a
    normalised header against a raw configuration entry is how a trailing slash or a capital
    letter turns into "the widget does not work on our site" with nothing in the logs.

    The wildcard is refused here, in every environment, and that is deliberately stricter than
    `brain.config.check`, which only refuses a `cors_origins` of exactly `"*"` and only in
    production. Two gaps in that check are visible from here: staging is not covered, and
    `serve.py` passes the setting as a comma-joined string, so `"*,https://console.example"`
    is a production wildcard that compares unequal to `"*"` and passes. Neither gap matters
    much for CORS, which is a browser convenience; both matter a great deal here, because a
    wildcard on this path means every site on the internet may mint anonymous credentials
    against a customer's brain. Refusing per entry closes it whatever the joined string looks
    like.

    Rejected: treating a wildcard as an allowlist that matches nothing. It fails closed, which
    is the right direction, and it fails quietly, which is the wrong one: the operator sees a
    widget that refuses everybody and no statement anywhere about why.
    """
    out: set[str] = set()
    for entry in configured:
        if entry.strip() == WILDCARD:
            msg = (
                "a widget origin allowlist may not contain '*'. A wildcard here is not a CORS "
                "convenience, it is permission for any site on the internet to mint anonymous "
                "sessions against this deployment; name the sites that embed the widget"
            )
            raise WidgetConfigurationError(msg)
        normalised = normalise_origin(entry)
        if not normalised:
            msg = (
                f"widget origin {entry!r} is not an origin. An origin is a scheme, a host and "
                "an optional port, with no path, and it is compared as one"
            )
            raise WidgetConfigurationError(msg)
        out.add(normalised)
    return frozenset(out)


# -------------------------------------------------------------------------------- refusals
class RefusedBecause(enum.StrEnum):
    """Why no session was minted. Two members, because there are two things to say.

    There is deliberately no third member separating "over the mint rate" from "at the live
    session ceiling". `ops.limits.mint_widget_session` owns that distinction and already
    words it, and re-deriving which of its two guards fired would mean repeating its
    comparison here: a second copy of the ceiling test that agrees today and mislabels the day
    somebody changes the operator. The operator gets `limits`' own sentence in `reason`
    instead, which names the numbers and cannot drift from the decision it explains.
    """

    #: This site is not on the allowlist. No window was read and none was created.
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    #: The site is allowed and is over one of the two mint guards.
    OVER_LIMIT = "over_limit"


@dataclass(frozen=True)
class WidgetRefusal:
    """No session, in two registers: one for the log and one for the browser.

    Two strings rather than one, deliberately. `reason` names the origin and the numbers and
    is what an operator reads; `public_message` is what a stranger's browser is handed.
    Collapsing them is how a refusal ends up quoting a customer's configured ceiling into
    somebody else's chat window.
    """

    because: RefusedBecause
    #: The normalised origin, or a fixed placeholder when there was not one. Never the raw
    #: header: it is attacker-controlled and this string reaches a log, where a newline in it
    #: forges a second line.
    origin: str
    retry_after_seconds: float
    reason: str

    @property
    def public_message(self) -> str:
        """One sentence for the visitor, naming nothing about anybody.

        The two cases differ, and the module docstring argues why concealing the difference
        would protect nothing and would cost every misconfigured embed a correct diagnosis.
        Neither sentence contains an origin, a count or a ceiling.
        """
        if self.because is RefusedBecause.ORIGIN_NOT_ALLOWED:
            return "This site is not set up to use this assistant."
        return "Too many chat sessions have been started here just now; please try again shortly."


# --------------------------------------------------------------------------------- session
def new_session_id() -> str:
    """A session id that has to be three things at once.

    It is the credential, so it is unguessable: `WIDGET_SESSION_ID_BYTES` of `secrets`.

    It is the audit actor, so it has to satisfy `audit.ledger.IDENTIFIER` or the ledger write
    fails after the work is done. `token_urlsafe` emits base64url, whose alphabet is a subset
    of that grammar, so the two agree by construction rather than by luck; `WidgetSession`
    checks it anyway, because "by construction" is a claim about a line somebody may change.

    It is what an operator reads in an audit row, so it is prefixed. `ws_...` in an actor
    column says "a browser, nobody identified" without a lookup.
    """
    return f"{SESSION_ID_PREFIX}{secrets.token_urlsafe(WIDGET_SESSION_ID_BYTES)}"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware; a naive timestamp is a silent bug"
        raise ValueError(msg)


@dataclass(frozen=True)
class WidgetSession:
    """One anonymous conversation, and everything that is known about who is having it.

    Which is: the site it is happening on, and nothing else. There is no `principal_id`, no
    `subject`, no `visitor_id` and no field that could hold one, which is the difference
    between this and `identity.sessions.Session` and is the point rather than an omission.

    `reach` is `gate.ingress.Unrecognised`, an object that has no `EntitlementSet` to hand
    out. It is a field rather than a method so that "this session confers nothing" is
    something a reader sees in the shape of the type, and so that a future edit granting an
    anonymous caller something has to change a type here and be seen in review.

    `expires_at` and `absolute_expiry` are separate stored values for the reason
    `identity.sessions.Session` keeps them separate: they answer different questions, and
    computing the second from the first at refresh time is how it quietly becomes the first.
    """

    session_id: str
    origin: str
    opened_at: datetime
    expires_at: datetime
    absolute_expiry: datetime
    reach: Unrecognised

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.match(self.session_id):
            # Checked at construction rather than at the audit write. A session whose id the
            # ledger will refuse is one whose every action is unauditable, and discovering
            # that at the write means discovering it after the work was done.
            msg = (
                f"session id {self.session_id!r} does not match the audit ledger's identifier "
                "grammar, so nothing this session did could be recorded"
            )
            raise ValueError(msg)
        if not self.origin:
            msg = "a widget session belongs to a site; an unkeyed session belongs to every site"
            raise ValueError(msg)
        _require_aware(self.opened_at, "opened_at")
        _require_aware(self.expires_at, "expires_at")
        _require_aware(self.absolute_expiry, "absolute_expiry")
        if self.expires_at <= self.opened_at:
            msg = "a session must expire after it opens"
            raise ValueError(msg)
        if self.absolute_expiry < self.expires_at:
            msg = (
                "the absolute expiry is before the idle expiry, so the idle window would "
                "outlive the bound that exists to cap it"
            )
            raise ValueError(msg)
        lifetime = self.absolute_expiry - self.opened_at
        if lifetime > WIDGET_SESSION_ABSOLUTE_MAX:
            msg = (
                f"an anonymous session may run at most {WIDGET_SESSION_ABSOLUTE_MAX}; this one "
                f"is bounded at {lifetime}. Nothing but the clock can end it: there is no "
                "principal to suspend, no logout floor to raise and no provider to re-ask"
            )
            raise ValueError(msg)

    def is_live(self, now: datetime) -> bool:
        """True while this session is inside both its idle window and its hard bound."""
        return self.opened_at <= now < min(self.expires_at, self.absolute_expiry)

    @property
    def audit_actor(self) -> str:
        """What goes in `audit.ledger.AuditEntry.actor_id` for anything this session does.

        The session id. `actor_id` is a plain identifier and takes one, and this one is
        honest: it names a browser that held a credential for under an hour.

        Deliberately not a fabricated principal. A constant `p_anonymous`, or a synthetic id
        minted per visitor, would put a name in the actor column of an append-only chain that
        exists to answer "who did this". A false attribution there is worse than an honest
        non-person: the chain is the artefact somebody reaches for when the question is
        serious, and an answer that looks like a person and is not is the one failure mode it
        must not have. `ws_...` reads at a glance as "nobody was identified", which is true.
        """
        return self.session_id


# ---------------------------------------------------------------------------- the registry
def mint_window(origin: str, *, mints_per_minute: int = WIDGET_MINTS_PER_MINUTE) -> Limit:
    """The window `ops.limits.mint_widget_session` reads, so an admitted hit lands in it.

    `mint_widget_session` builds this window internally and does not hand it back, and it
    records nothing: `limits.check` decides and `LimiterState.record` writes, and keeping
    those two apart is what stops a refusal from extending the window
    (`limits.REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW`). So whoever admits a mint has to
    record it, and to record it has to name the same window. This names it.

    The failure this risks is drift. A window whose key differs from the one the guard reads
    is a rate limit that silently never binds, because every hit is written where nobody
    looks, and the symptom is a guard that passes every test and stops nothing. `Limit.key` is
    the scope, the subject and the period, so those three are what must agree, and
    `test_the_window_a_mint_records_into_is_the_one_the_rate_guard_reads` pins it from outside
    by filling this window and asserting the guard then refuses.

    Rejected: deciding here as well, so that the check and the record share one expression.
    That is a second copy of the guard, in the module that is supposed to consume the first
    one, and the drift it removes is smaller than the drift it introduces.
    """
    return Limit(
        scope=LimitScope.WIDGET_ORIGIN,
        subject=origin,
        period="minute",
        limit=mints_per_minute,
        window_seconds=MINUTE_SECONDS,
        reason="new anonymous sessions one site may open a minute",
    )


@dataclass
class WidgetSessions:
    """Live anonymous sessions and the mint windows behind them, for one process.

    In memory, and that is a limitation stated rather than hidden, exactly as
    `channels.webhook.SeenNonces` states its own. Two replicas each keep their own count, so
    the effective ceiling across a deployment is `replicas x WIDGET_LIVE_SESSIONS_PER_ORIGIN`
    rather than the number in `ops.limits`. Closing that needs the shared store, and the seam
    already exists: `windows` is a `LimiterState`, which is precisely what
    `ops.limit_store.ValkeyWindowStore` loads and writes back, and `WIDGET_ORIGIN` is already
    declared `FAIL_OPEN` there. This type exists so that seam is visible, not so the problem
    is solved.

    `live_sessions` is computed here rather than passed in by whoever calls `mint`. A count
    the caller supplies is a count the next call site computes differently or forgets, and the
    ceiling then reads as working while counting nothing. Same argument as
    `channels.adapter.assert_can_send` being called by `send` rather than by `send`'s caller.
    """

    #: Normalised origins, from `allowed_origins`. Frozen because an allowlist that could be
    #: widened between the check and the mint is not an allowlist.
    allowed: frozenset[str]
    #: The mint windows. Public because production replaces it: read from Valkey before a
    #: mint, written back after one.
    windows: LimiterState = field(default_factory=LimiterState)
    _live: dict[str, WidgetSession] = field(default_factory=dict)

    # -- reading ------------------------------------------------------------------------
    def _prune(self, now: datetime) -> None:
        """Drop sessions that have expired.

        On read rather than on a timer, like `SeenNonces`: a timer is a second thing to run
        and get wrong, and every path that cares already passes through here. Without it the
        live count only ever rises, so a site reaches its ceiling once and never mints again
        while every session it is counting is dead.
        """
        dead = [sid for sid, session in self._live.items() if not session.is_live(now)]
        for sid in dead:
            del self._live[sid]

    def live(self, origin: str, now: datetime) -> int:
        """How many sessions this origin holds right now, expired ones dropped first."""
        self._prune(now)
        return sum(1 for session in self._live.values() if session.origin == origin)

    def get(self, session_id: str, now: datetime) -> WidgetSession | None:
        """The session behind a presented id, or None.

        Deliberately does not slide the idle window, for the reason
        `SessionRegistry.admit` does not: a read is a read, and sliding on every request means
        a script polling an endpoint keeps a session alive with nobody at the keyboard, which
        is the failure the idle window exists for. `touch` is the call that extends.
        """
        session = self._live.get(session_id)
        if session is None:
            return None
        if not session.is_live(now):
            del self._live[session_id]
            return None
        return session

    # -- extending ----------------------------------------------------------------------
    def touch(
        self, session_id: str, now: datetime, *, idle: timedelta = WIDGET_SESSION_IDLE
    ) -> WidgetSession | None:
        """Slide the idle window, never past the absolute expiry (M10.5.5).

        `min` against `absolute_expiry` is the load-bearing half. An idle window that slid
        freely would never close, and the session would live as long as somebody kept typing
        into it; the absolute bound is what makes `WIDGET_SESSION_ABSOLUTE_MAX` a bound rather
        than a suggestion.

        Returns None for a session that is absent or already dead, and evicts the dead one on
        the way past. A caller cannot tell those two apart, which is correct: both mean the
        credential presented is no longer worth anything.
        """
        session = self._live.get(session_id)
        if session is None:
            return None
        if not session.is_live(now):
            del self._live[session_id]
            return None
        extended = replace(session, expires_at=min(now + idle, session.absolute_expiry))
        self._live[session_id] = extended
        return extended

    # -- minting ------------------------------------------------------------------------
    def mint(
        self,
        *,
        origin: str,
        now: datetime,
        idle: timedelta = WIDGET_SESSION_IDLE,
        absolute: timedelta = WIDGET_SESSION_ABSOLUTE_MAX,
        mints_per_minute: int = WIDGET_MINTS_PER_MINUTE,
        max_live: int = WIDGET_LIVE_SESSIONS_PER_ORIGIN,
        channel: Channel = Channel.WIDGET,
    ) -> WidgetSession | WidgetRefusal:
        """Issue an anonymous session for an allowed origin, or say why not (M10.5.5, M23.1.4).

        Returns a union rather than raising or returning None. A refusal is an ordinary
        outcome here, not an exception: a busy site reaches its ceiling on an ordinary
        afternoon. And a union is what stops the result being ignored, which is the objection
        `channels.webhook.verify` raises against returning a bool: a caller cannot use a
        `WidgetRefusal` where a session is wanted, so under mypy they have to look at it.

        The order of the checks is the design. The origin first, before any window is read or
        created, because the limiter is keyed on a string the sender chose; then
        `limits.mint_widget_session`, which owns both guards; then, only on a yes, the session
        and the recorded hit.

        `channel` is `Channel.WIDGET`, which was added for this. The stand-in it replaced was
        `Channel.API`, and the reason that was not good enough is the one thing the traffic
        class exists to decide: `traffic_class_for(API)` is `AUTOMATION`, and a person is very
        much waiting at a widget. Anything degrading by traffic class would have queued an
        answer for somebody watching a cursor blink, and queueing is the one response that is
        wrong for a person who is present.

        The verb ceiling is `read` alone, narrower than WhatsApp's. That changes no outcome
        while an anonymous reach is empty, and it is what holds if a widget visitor ever
        identifies themselves: a person may sign in through a widget and may not approve a
        payment through one.
        """
        normalised = normalise_origin(origin)
        if not normalised or normalised not in self.allowed:
            # Before the limiter, and nothing is touched. `Origin` is whatever the sender
            # typed, the mint window is keyed on it, and checking the rate first would let one
            # caller create a window per invented origin in a store keyed on strings they
            # choose. See the module docstring.
            #
            # The raw header is never echoed. It reaches a log line from here, and a newline
            # inside it forges a second one.
            shown = normalised or "(not an origin)"
            return WidgetRefusal(
                because=RefusedBecause.ORIGIN_NOT_ALLOWED,
                origin=shown,
                # Nought, and honestly so: waiting changes nothing about an allowlist. A
                # hint borrowed from the rate refusal to make the two look alike would send a
                # site owner to wait for a window that will never open.
                retry_after_seconds=0.0,
                reason=(
                    f"{shown} is not on this deployment's widget allowlist, so no session was "
                    "minted and no rate window was read or created"
                ),
            )

        decision = mint_widget_session(
            now=now,
            origin=normalised,
            state=self.windows,
            live_sessions=self.live(normalised, now),
            mints_per_minute=mints_per_minute,
            max_live=max_live,
        )
        if not decision.minted:
            # Nothing recorded, nothing evicted. A refused mint must not extend the window,
            # and it must not disturb a session somebody is in the middle of using: refusing
            # to hand out another credential is the whole reason this guard is allowed to
            # refuse at all, per `limits.MintDecision`.
            return WidgetRefusal(
                because=RefusedBecause.OVER_LIMIT,
                origin=normalised,
                retry_after_seconds=decision.retry_after_seconds,
                # `limits`' own sentence, verbatim. It names which of the two guards fired and
                # with what numbers, and copying it here would be a second wording to drift.
                reason=decision.reason,
            )

        hard_stop = now + absolute
        session = WidgetSession(
            session_id=new_session_id(),
            origin=normalised,
            opened_at=now,
            # `min` for the same reason `identity.sessions.open_session` uses one: a caller
            # passing an idle window longer than the absolute bound must get the bound, not a
            # session that fails its own validation.
            expires_at=min(now + idle, hard_stop),
            absolute_expiry=hard_stop,
            reach=Unrecognised(channel=channel, prompt=WIDGET_PROMPT),
        )
        self._live[session.session_id] = session
        # Recorded after the session exists, so a session that fails its own checks leaves no
        # hit behind. Recorded at all, because `mint_widget_session` deliberately does not:
        # see `mint_window`.
        self.windows = self.windows.record(
            now, (mint_window(normalised, mints_per_minute=mints_per_minute),)
        )
        return session
