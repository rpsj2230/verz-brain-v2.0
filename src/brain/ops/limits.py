"""How often one caller, one channel, one agent or one connector may be asked for.

Admission (`brain.ops.admission`) answers "is the machine full". This answers a different
question that arrives first: "is this caller entitled to ask again yet". They are separate
because the remedies are separate. A full machine is answered by adding capacity; a caller
over their allowance is answered by raising their allowance, or by leaving it alone and
letting them wait, and telling an operator the wrong one of those wastes an incident.

Four things are load-bearing.

**Both limits apply, always.** A limit is per principal *and* per connector, and neither
subsumes the other. Without the per-principal one, a single backfill takes Xero's whole
daily allowance and everybody else's questions fail for the rest of the day. Without the
per-connector one, twenty people each being individually reasonable add up to a ceiling
nobody individually crossed. `PRINCIPAL_FAIR_SHARE` is what keeps the first from being
possible, and it is checked rather than assumed: see `BOTH_LIMITS_APPLY`.

**The verified source ceilings are constraints, not guidance.** Xero is 5,000 calls a day
per tenant, shared with every other integration the client runs. Lark Base is 100 requests
a minute and their documentation states it cannot be raised, so sizing against a higher
number is sizing against a number that does not exist. Freshdesk's *search* returns at most
300 records ever, which is not a page size and is the one that looks correct in testing:
"all tickets matching" is simply wrong beyond 300 and nothing reports that it was.
`search_completeness` exists so that failure has somewhere to be noticed.

**A refused request does not extend the window.** Only admitted requests are recorded.
Counting refusals turns a client that retries too eagerly into a client that is locked out
permanently, with the retry hint receding faster than the client can obey it, and there is
no message that explains that to the person on the other end.

**Abuse detection never blocks a question.** Same rule, and the same reason, as
`brain.gate.injection`: it scores, and its public surface has nowhere to express a refusal,
so a future caller cannot start refusing without adding one and being seen in review.
Volume and denial patterns are heuristics over ordinary behaviour, and the failure mode of
a heuristic that refuses is that legitimate users learn to work around it while anybody
adapting deliberately walks through it. What actually protects a connector is the declared
limit above, which is a published number with a retry hint attached, not a guess about
intent. See `ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE`.

Nothing here opens a connection. The sliding window is the algorithm; Valkey is where the
hits live in production, and a state machine that owned a Redis client could not be tested
for the case that matters, which is the boundary between two windows.

Task ids: M23.1.1, M23.1.2, M23.1.3, M23.1.4, M23.1.5, M23.2.1, M23.2.2, M23.2.3
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from brain.core.errors import BrainError, Outcome
from brain.core.principal import PrincipalKind
from brain.gate.context import TrafficClass
from brain.ops.admission import Ceiling, RefusalKind, refusal_record

# ------------------------------------------------------------------ written-down reasons
#: Why the per-principal and per-connector limits are both evaluated, every time.
BOTH_LIMITS_APPLY = (
    "One person must not be able to exhaust a connector for everybody, and one connector "
    "must not be exhaustible by the sum of everyone being individually reasonable. Those "
    "are two different failures and neither limit catches the other's: the per-principal "
    "share stops the backfill that eats Xero's day, and the per-connector ceiling stops "
    "twenty reasonable people from adding up to a 429 nobody individually caused."
)

#: The order the two gates run in, and why it is that way round.
LIMITS_ARE_CHECKED_BEFORE_CAPACITY = (
    "A caller's own allowance is checked before the machine's capacity. Both decide before "
    "work starts, so neither is cheaper, but the reasons they give are not equally useful: "
    "telling somebody who is looping that the system is busy hides the cause from them and "
    "sends the operator to look at a machine that is fine. When both would refuse, the "
    "caller's own limit is the one they can act on, so it is the one they are told about. "
    "Neither module composes the other, deliberately, so this is a contract for whoever "
    "runs both and there is nothing here that can enforce it. Putting the composition in "
    "one of them would make that one depend on the other's state snapshot, and the gate "
    "already owns both."
)

#: Why a refusal is not recorded as a hit.
REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW = (
    "Only admitted requests enter the window. Counting refusals means a client that retries "
    "too eagerly pushes its own retry time further away every time it tries, so a "
    "thirty-second limit becomes a permanent lockout and the hint the client was given "
    "becomes a lie. The client cannot tell, and neither can the person waiting on it."
)

#: Why this module scores abuse and never refuses on it.
ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE = (
    "The same argument as brain.gate.injection. A heuristic that refuses teaches legitimate "
    "users to rephrase until they get through, while anybody adapting deliberately does the "
    "same thing faster; what is left is a detector that annoys the people it should not "
    "affect and stops nobody it should. So volume and denial patterns produce a score and "
    "an alert, and the public surface here has no value meaning 'refuse'. What protects a "
    "connector is the declared limit, which is a published number with a retry hint, not a "
    "guess about intent."
)

#: Why automated traffic is not detected.
AUTOMATED_TRAFFIC_IS_DECLARED_NOT_SNIFFED = (
    "Every channel declares its traffic class at ingress and it has no default, and every "
    "principal declares whether it is a person or a service. So identifying machine traffic "
    "is a lookup, not a heuristic: no user-agent string is consulted and none can be forged "
    "into or out of the metrics. A sniffer would be a second, weaker answer to a question "
    "already answered, and the two would disagree on exactly the traffic that matters."
)

#: Why the exact hint is preferred to an exponential one.
THE_HINT_IS_MEASURED_NOT_GUESSED = (
    "The window knows exactly when the oldest hit falls out of it, so the retry hint is a "
    "fact rather than a doubling guess. Exponential backoff appears only after repeated "
    "refusals, because a client refused several times in a row has demonstrated that it is "
    "not reading the hint, and at that point spacing it out protects the connector from a "
    "client nobody can fix today."
)


# ------------------------------------------------------------------------------- limits
class LimitScope(enum.StrEnum):
    """Whose allowance is being counted.

    `PRINCIPAL_CONNECTOR` is separate from `PRINCIPAL` on purpose. A person's overall rate
    and their share of one connector are different allowances with different consequences,
    and folding them into one subject string would make the console show "p_alice is over
    her limit" when what happened is that she is over her share of Xero.
    """

    PRINCIPAL = "principal"
    PRINCIPAL_CONNECTOR = "principal_connector"
    CHANNEL = "channel"
    AGENT = "agent"
    CONNECTOR = "connector"
    WIDGET_ORIGIN = "widget_origin"


#: A window is addressed by its scope, its subject and the period it covers. The period is
#: part of the key because one subject legitimately has several: Xero is 60 a minute *and*
#: 5,000 a day, and both bind.
LimitKey = tuple[LimitScope, str, str]

MINUTE_SECONDS = 60.0
DAY_SECONDS = 86_400.0


@dataclass(frozen=True)
class Limit:
    """One allowance: how many, over how long, for whom.

    A configuration row rather than a constant, like every budget in `admission`. `raisable`
    records whether money can move it, because an alert about a ceiling nobody can raise
    must not send somebody looking for an upgrade button that does not exist.
    """

    scope: LimitScope
    subject: str
    period: str
    limit: int
    window_seconds: float
    raisable: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if self.limit < 1:
            # Zero is how somebody switches a caller off by editing a number, and it reads
            # in the console as a configured limit rather than as a suspension. Suspending
            # a principal is a different change and belongs where it can be seen.
            msg = f"limit {self.scope}:{self.subject} is {self.limit}; minimum is 1"
            raise ValueError(msg)
        if self.window_seconds <= 0:
            msg = f"limit {self.scope}:{self.subject} has a non-positive window"
            raise ValueError(msg)
        if not self.subject:
            msg = f"a {self.scope} limit needs a subject; an unkeyed window counts everybody"
            raise ValueError(msg)

    @property
    def key(self) -> LimitKey:
        return (self.scope, self.subject, self.period)


class QuotaExceeded(BrainError):  # noqa: N818 - the taxonomy in core.errors has no suffixes
    """The caller is over an allowance of their own.

    Not a `Denied`: they hold every capability involved, and DENIED is the outcome that
    means "this exists and you may not see it". Not a `CapacityRefused` either: the machine
    has room, and telling an operator to add capacity because one caller is looping is how
    a system gets bigger without getting better.
    """

    outcome = Outcome.FAILED
    public_message = "That is more quickly than I can keep up with; please try again shortly."


# ---------------------------------------------------------------------- the sliding window
@dataclass(frozen=True)
class WindowState:
    """The timestamps of admitted requests, oldest first.

    A log rather than a counter, and that is a deliberate choice against the two cheaper
    options.

    A *fixed* window is two integers in Valkey and is wrong at the boundary: with a limit of
    thirty a minute it admits sixty inside two adjacent seconds, and the thing that notices
    is the connector returning 429 while our own counter says we were within the limit.

    A sliding-window *counter*, which interpolates between two fixed buckets, is right on
    average and approximate on any individual decision. At these volumes the limits are
    small integers, so an approximation produces refusals that cannot be reproduced from
    the log, and a rate limit nobody can reproduce is a rate limit nobody can argue with.

    The log is bounded by construction rather than by a cap: only admitted requests are
    recorded, and an admitted request is one that was under the limit, so the retained
    count never exceeds the limit for longer than one window.
    """

    hits: tuple[datetime, ...] = ()

    def pruned(self, now: datetime, window_seconds: float) -> WindowState:
        """Drop everything that has fallen out of the window. Idempotent."""
        cutoff = now - timedelta(seconds=window_seconds)
        kept = tuple(h for h in self.hits if h > cutoff)
        return self if len(kept) == len(self.hits) else WindowState(hits=kept)

    def count(self, now: datetime, window_seconds: float) -> int:
        return len(self.pruned(now, window_seconds).hits)

    def record(self, now: datetime, window_seconds: float) -> WindowState:
        """Record one admitted request. Never called for a refusal.

        See `REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW`. Kept sorted by construction: `now`
        moves forward, and a caller that passes a `now` behind an existing hit is replaying
        history, which the sort makes harmless rather than silently mis-ordered.
        """
        pruned = self.pruned(now, window_seconds)
        return WindowState(hits=tuple(sorted((*pruned.hits, now))))

    def retry_after(self, now: datetime, limit: Limit) -> float:
        """Seconds until this window has room again. Exact, not a guess.

        Room appears when the hit at index `count - limit` falls out. For a window that is
        exactly full that is the oldest hit; the general form covers a limit that was
        lowered while requests were already in the window, which otherwise reports a
        negative index and hands back a hint from the wrong end of the log.
        """
        pruned = self.pruned(now, limit.window_seconds)
        spare = len(pruned.hits) - limit.limit
        if spare < 0:
            return 0.0
        expires = pruned.hits[spare] + timedelta(seconds=limit.window_seconds)
        return max(0.0, (expires - now).total_seconds())


_NO_WINDOWS: Mapping[LimitKey, WindowState] = MappingProxyType({})


@dataclass(frozen=True)
class LimiterState:
    """Every window in play, as somebody else stored them.

    In production these rows live in Valkey, one key per `LimitKey`. Nothing here knows
    that: a limiter holding its own client could not be tested for the window boundary,
    which is the only part of a sliding window that is ever wrong.
    """

    windows: Mapping[LimitKey, WindowState] = _NO_WINDOWS

    def window_for(self, key: LimitKey) -> WindowState:
        return self.windows.get(key, WindowState())

    def record(self, now: datetime, limits: Sequence[Limit]) -> LimiterState:
        """Record one admitted request against every limit that governed it.

        All of them, not just the binding one: a request that consumed a Xero call consumed
        it from the connector's minute, the connector's day and the caller's share, and
        recording only one of those makes the other two drift until they mean nothing.
        """
        updated = dict(self.windows)
        for limit in limits:
            updated[limit.key] = self.window_for(limit.key).record(now, limit.window_seconds)
        return LimiterState(windows=MappingProxyType(updated))


# -------------------------------------------------------------------------- the decision
@dataclass(frozen=True)
class LimitDecision:
    """Whether this may proceed, which allowance stopped it, and when to come back."""

    allowed: bool
    #: The limit that actually bound, or None when nothing did.
    binding: Limit | None
    retry_after_seconds: float
    reason: str
    #: Every limit that was over, not only the binding one. An operator seeing one caller
    #: over three allowances at once is looking at a different incident from one caller
    #: over a single allowance.
    over: tuple[Limit, ...] = ()

    def as_error(self) -> QuotaExceeded:
        if self.allowed:
            msg = "this request was within every limit; there is no refusal to raise"
            raise ValueError(msg)
        return QuotaExceeded(self.reason)

    def log_record(self) -> Mapping[str, str]:
        """The operator-facing line, distinguishable from a capacity refusal by its kind."""
        if self.allowed or self.binding is None:
            return MappingProxyType({"verdict": "allowed"})
        subject = f"{self.binding.scope}:{self.binding.subject}:{self.binding.period}"
        return refusal_record(RefusalKind.QUOTA, subject=subject, detail=self.reason)


def check(*, now: datetime, limits: Sequence[Limit], state: LimiterState) -> LimitDecision:
    """Evaluate every applicable limit. All must admit; the longest hint is the one reported.

    Reporting the longest rather than the first is the difference between a client that
    comes back once and a client that comes back, is refused again, and stops trusting the
    hint. A one-second per-principal hint handed out while a fifty-second connector limit is
    also over is worse than no hint at all.

    Nothing is recorded here. `LimiterState.record` is a separate call the caller makes
    after the work is admitted, which is what keeps a refusal from extending the window.
    """
    over: list[tuple[Limit, float]] = []
    for limit in limits:
        window = state.window_for(limit.key)
        if window.count(now, limit.window_seconds) + 1 > limit.limit:
            over.append((limit, window.retry_after(now, limit)))

    if not over:
        return LimitDecision(
            allowed=True,
            binding=None,
            retry_after_seconds=0.0,
            reason=f"within all {len(limits)} applicable limit(s)",
        )

    binding, wait = max(over, key=lambda pair: (pair[1], pair[0].key))
    fixed = "" if binding.raisable else ", and no plan raises it"
    return LimitDecision(
        allowed=False,
        binding=binding,
        retry_after_seconds=wait,
        reason=(
            f"{binding.scope}:{binding.subject} is at its limit of {binding.limit} per "
            f"{binding.period}{fixed}; room in {wait:.1f}s"
        ),
        over=tuple(limit for limit, _ in over),
    )


#: Refusals in a row after which the exact hint stops being obeyed and spacing takes over.
BACKOFF_AFTER_REFUSALS = 3

#: The longest hint we will hand out, whatever the arithmetic says. Five minutes: long
#: enough to protect a connector from a client nobody can fix today, short enough that a
#: client which has been fixed is not still waiting after the fix shipped.
MAX_BACKOFF_SECONDS = 300.0


def backoff_seconds(
    retry_after_seconds: float, *, consecutive_refusals: int, jitter: float = 0.0
) -> float:
    """The retry hint, exact until a client demonstrates it is not reading it.

    The first few refusals hand back the measured time until the window has room. After
    `BACKOFF_AFTER_REFUSALS` in a row, the hint doubles per extra refusal up to the ceiling:
    a client refused four times in a row is looping, and the exact hint has already been
    given to it three times.

    Jitter is the caller's and only ever lengthens, matching `CircuitBreaker`. Production
    passes `random.random()`; a test passes a constant. Its job is to decorrelate clients
    that were all refused in the same second, so generating it here from anything stable
    would look like jitter and do nothing.
    """
    if consecutive_refusals < 0:
        msg = "consecutive_refusals counts refusals and cannot be negative"
        raise ValueError(msg)
    base = max(0.0, retry_after_seconds)
    if consecutive_refusals > BACKOFF_AFTER_REFUSALS:
        base *= 2.0 ** (consecutive_refusals - BACKOFF_AFTER_REFUSALS)
    return min(MAX_BACKOFF_SECONDS, base * (1.0 + max(0.0, jitter)))


# ------------------------------------------------------------------- the source ceilings
#: The share of a connector's ceiling any one principal may take. A quarter, so it takes
#: four simultaneous heavy callers to saturate a connector rather than one, and so a
#: backfill started by one person leaves three quarters of the ceiling for everybody's
#: questions. Must stay strictly below 1.0: at 1.0 the per-principal limit is the connector
#: limit, one person can take all of it, and the second half of `BOTH_LIMITS_APPLY` stops
#: being true. An invariant test pins that.
PRINCIPAL_FAIR_SHARE = 0.25


@dataclass(frozen=True)
class ConnectorLimit:
    """A verified external ceiling. Facts, not guidance, and dated where they were checked.

    Verified 4 September 2026 against architecture §8's table.
    """

    name: str
    per_minute: int
    per_day: int | None = None
    #: Whether anything we can do moves this number. False covers two different cases and
    #: they lead to the same place: a vendor who states the ceiling is fixed, and a ceiling
    #: that belongs to the client's tenant rather than to our subscription. In both, the
    #: only lever is asking for less, and an operator must not go looking for an upgrade.
    raisable: bool = True
    note: str = ""


SOURCE_CEILINGS: tuple[ConnectorLimit, ...] = (
    ConnectorLimit(
        name="xero",
        per_minute=60,
        per_day=5_000,
        raisable=False,
        note=(
            "5,000 calls a day per tenant, shared with every other integration the client "
            "runs, so our own share is smaller than the number suggests. The ceiling is on "
            "the client's tenant rather than on our subscription, so there is no plan we "
            "can buy that moves it. This is the ceiling a backfill reaches first."
        ),
    ),
    ConnectorLimit(
        name="freshdesk",
        per_minute=100,
        note=(
            "100 / 400 / 700 a minute by plan, per account. Recorded at the lowest, because "
            "sizing against a plan we may not hold produces 429s on the day of a downgrade. "
            "Separately, search returns at most 300 records ever; see "
            "FRESHDESK_SEARCH_MAX_RECORDS."
        ),
    ),
    ConnectorLimit(
        name="lark_base",
        per_minute=100,
        raisable=False,
        note=(
            "100 requests a minute, fixed. Their documentation states it cannot be raised, "
            "so it is 1.67 calls a second for the whole tenant permanently. Sizing against "
            "a higher number is sizing against a number that does not exist."
        ),
    ),
)

_BY_NAME: Mapping[str, ConnectorLimit] = MappingProxyType({c.name: c for c in SOURCE_CEILINGS})


def connector_ceiling(connector: str) -> ConnectorLimit | None:
    return _BY_NAME.get(connector)


def source_limits(connector: str, *, principal_id: str) -> tuple[Limit, ...]:
    """Every allowance that governs one caller making one call to one connector.

    Three or four windows, and all of them apply: the connector's minute, the connector's
    day where it has one, and the caller's share of the connector's minute. An unknown
    connector returns nothing rather than a default, because inventing a ceiling for a
    source nobody has measured produces a number that looks verified and is not.
    """
    ceiling = connector_ceiling(connector)
    if ceiling is None:
        return ()
    limits = [
        Limit(
            scope=LimitScope.CONNECTOR,
            subject=connector,
            period="minute",
            limit=ceiling.per_minute,
            window_seconds=MINUTE_SECONDS,
            raisable=ceiling.raisable,
            reason=ceiling.note,
        ),
        Limit(
            scope=LimitScope.PRINCIPAL_CONNECTOR,
            subject=f"{principal_id}:{connector}",
            period="minute",
            limit=principal_share_of(ceiling.per_minute),
            window_seconds=MINUTE_SECONDS,
            reason=(
                f"{PRINCIPAL_FAIR_SHARE:.0%} of {connector}'s minute, so one caller cannot "
                "take the whole of it"
            ),
        ),
    ]
    if ceiling.per_day is not None:
        limits.append(
            Limit(
                scope=LimitScope.CONNECTOR,
                subject=connector,
                period="day",
                limit=ceiling.per_day,
                window_seconds=DAY_SECONDS,
                raisable=ceiling.raisable,
                reason=ceiling.note,
            )
        )
    return tuple(limits)


def principal_share_of(connector_limit: int) -> int:
    """One caller's share of a connector's ceiling. Always strictly below it above 1.

    Never zero, because a share that rounds to nothing takes every individual caller out of
    service while the connector reads as idle. Never the whole ceiling either, which is the
    property that makes "one person cannot exhaust a connector for everybody" true rather
    than aspirational.
    """
    if connector_limit < 1:
        msg = "a connector ceiling is at least 1"
        raise ValueError(msg)
    if connector_limit == 1:
        # Nothing can be shared out of a ceiling of one. The honest answer is that this
        # connector serialises every caller in the company, which is a fact about the
        # connector rather than something a fair-share rule can fix.
        return 1
    return max(1, min(connector_limit - 1, math.floor(connector_limit * PRINCIPAL_FAIR_SHARE)))


#: One person's questions a minute. The whole estate runs at about 0.1 requests a second,
#: which is six questions a minute across 126 people, so thirty a minute for one person is
#: five times what everybody together does. It is deliberately far above any human rate: the
#: thing this catches is a loop, and a limit tuned close to real behaviour catches a busy
#: Monday instead.
DEFAULT_PRINCIPAL_PER_MINUTE = 30

#: One channel's questions a minute, across everybody on it. Twenty times the estate's whole
#: measured rate. This is the limit that contains a misbehaving integration on the API
#: channel without any single caller looking unreasonable.
DEFAULT_CHANNEL_PER_MINUTE = 120

#: One agent's runs a minute, across everybody using it. Below the channel limit because an
#: agent is a smaller blast radius and a looping agent is a much more likely accident than a
#: looping channel.
DEFAULT_AGENT_PER_MINUTE = 60


def request_limits(
    *,
    principal_id: str,
    channel: str,
    agent_id: str | None = None,
    connector: str | None = None,
    principal_per_minute: int = DEFAULT_PRINCIPAL_PER_MINUTE,
    channel_per_minute: int = DEFAULT_CHANNEL_PER_MINUTE,
    agent_per_minute: int = DEFAULT_AGENT_PER_MINUTE,
) -> tuple[Limit, ...]:
    """Every allowance governing one request, assembled in one place.

    Assembled here rather than at each call site, because "which limits apply" is exactly
    the decision that gets a line dropped from it during a refactor, and a dropped line is
    a limit that silently stops existing while the console still lists it. The list is
    additive by construction: an agent adds a window, a connector adds three more, and
    nothing anywhere removes one.

    See `BOTH_LIMITS_APPLY` for why the connector windows are not folded into the principal
    one.
    """
    limits = [
        Limit(
            scope=LimitScope.PRINCIPAL,
            subject=principal_id,
            period="minute",
            limit=principal_per_minute,
            window_seconds=MINUTE_SECONDS,
            reason="one person's questions a minute; far above any human rate, so it catches loops",
        ),
        Limit(
            scope=LimitScope.CHANNEL,
            subject=channel,
            period="minute",
            limit=channel_per_minute,
            window_seconds=MINUTE_SECONDS,
            reason="everybody on one channel; contains a misbehaving integration",
        ),
    ]
    if agent_id is not None:
        limits.append(
            Limit(
                scope=LimitScope.AGENT,
                subject=agent_id,
                period="minute",
                limit=agent_per_minute,
                window_seconds=MINUTE_SECONDS,
                reason="everybody using one agent; a looping agent is a likely accident",
            )
        )
    if connector is not None:
        limits.extend(source_limits(connector, principal_id=principal_id))
    return tuple(limits)


#: Minutes in a day. Named because multiplying a per-minute ceiling by it produces a
#: sustained-rate daily figure, and a bare 1440 in that expression reads like a typo.
MINUTES_PER_DAY = 1_440


def effective_per_day(ceiling: ConnectorLimit) -> tuple[int, bool]:
    """A daily ceiling for a source, and whether we had to derive it.

    Only Xero publishes one. For the others the sustained rate is the best daily figure
    available, and it flatters them: it assumes traffic arrives evenly across 1,440 minutes,
    which office traffic does not. So the derived flag travels with the number, and
    `admission.Ceiling.derived` carries it into the bottleneck ladder rather than letting a
    calculated figure sit beside a published one looking equally solid.
    """
    if ceiling.per_day is not None:
        return ceiling.per_day, False
    return ceiling.per_minute * MINUTES_PER_DAY, True


def ceilings() -> tuple[Ceiling, ...]:
    """Every verified source ceiling, in the shape `admission.first_bottleneck` reads.

    All three, with the derived ones marked. Omitting the two that publish no daily figure
    was the first version and it was worse: the ladder then showed one source, which reads
    as "only Xero is a constraint" when what is true is "only Xero's constraint is
    expressible in this unit".
    """
    result: list[Ceiling] = []
    for source in SOURCE_CEILINGS:
        per_day, derived = effective_per_day(source)
        result.append(
            Ceiling(name=source.name, per_day=per_day, raisable=source.raisable, derived=derived)
        )
    return tuple(result)


# --------------------------------------------------------------- the search-result ceiling
#: Freshdesk's search returns at most this many records, ever. It is not a page size and it
#: cannot be paged past.
FRESHDESK_SEARCH_MAX_RECORDS = 300

#: Why this is tracked at all, when it is not a rate limit.
SEARCH_CAP_IS_NOT_A_PAGE_SIZE = (
    "A cap on the size of a result set looks exactly like a full page of results, and "
    "'which clients have an open P1 older than five days' returns 300 rows and looks "
    "complete in every test anybody writes, because no test has more than 300 matching "
    "tickets. Beyond 300 the answer is silently wrong with nothing anywhere reporting it. "
    "So a search that comes back at the cap is marked incomplete, and what to do about that "
    "belongs to the abstention path: say what was searched and what could not be seen."
)

SEARCH_RESULT_CAPS: Mapping[str, int] = MappingProxyType(
    {"freshdesk": FRESHDESK_SEARCH_MAX_RECORDS}
)


@dataclass(frozen=True)
class SearchCompleteness:
    """Whether a result set can be spoken about as if it were everything."""

    connector: str
    returned: int
    cap: int | None
    complete: bool
    reason: str


def search_completeness(connector: str, returned: int) -> SearchCompleteness:
    """Whether a search result may be treated as the whole answer.

    Deliberately conservative at exactly the cap. A result of exactly 300 from Freshdesk is
    indistinguishable from a result of 300 out of 4,000, and treating it as complete is the
    failure this function exists for. A caller wanting certainty has to narrow the search
    until it comes back short of the cap, which is the only signal the API gives.
    """
    if returned < 0:
        msg = "a search cannot return a negative number of records"
        raise ValueError(msg)
    cap = SEARCH_RESULT_CAPS.get(connector)
    if cap is None:
        return SearchCompleteness(
            connector=connector,
            returned=returned,
            cap=None,
            complete=True,
            reason=f"{connector} declares no search-result ceiling",
        )
    if returned >= cap:
        return SearchCompleteness(
            connector=connector,
            returned=returned,
            cap=cap,
            complete=False,
            reason=(
                f"{connector} search returned {returned} against a hard ceiling of {cap}; "
                "there is no way to tell how many more there were, so this is not 'all' of "
                "anything and must not be summarised as if it were"
            ),
        )
    return SearchCompleteness(
        connector=connector,
        returned=returned,
        cap=cap,
        complete=True,
        reason=f"{returned} is short of {connector}'s {cap}-record ceiling, so nothing was cut off",
    )


# ------------------------------------------------------------------ widget session minting
#: New anonymous widget sessions one origin may mint per minute.
WIDGET_MINTS_PER_MINUTE = 10

#: Live anonymous sessions one origin may hold at once. A website chat widget on a normal
#: business site has a handful; a hundred is somebody enumerating.
WIDGET_LIVE_SESSIONS_PER_ORIGIN = 20


@dataclass(frozen=True)
class MintDecision:
    """Whether a new anonymous widget session may be issued.

    Refusing to mint is not refusing a question, and the distinction is the whole reason
    this can refuse at all while the abuse detectors below cannot. A mint hands out a
    credential to an unauthenticated caller; declining to hand out another one leaves every
    existing session working and every identified person unaffected. Refusing a *question*
    on a heuristic is the thing this module never does.
    """

    minted: bool
    retry_after_seconds: float
    reason: str


def mint_widget_session(
    *,
    now: datetime,
    origin: str,
    state: LimiterState,
    live_sessions: int,
    mints_per_minute: int = WIDGET_MINTS_PER_MINUTE,
    max_live: int = WIDGET_LIVE_SESSIONS_PER_ORIGIN,
) -> MintDecision:
    """Issue a widget session, or say why not and when to come back.

    Two guards, because they catch different things. The rate guard catches a script minting
    sessions in a loop. The live-session cap catches the slower version, which mints one a
    minute all day and would never trip a rate limit, and it is the one that actually costs
    money because every live session is an open door to the answer path.
    """
    if not origin:
        msg = "a widget session is minted for an origin; an unkeyed mint counts every site"
        raise ValueError(msg)
    if live_sessions < 0:
        msg = "live_sessions counts sessions and cannot be negative"
        raise ValueError(msg)

    limit = Limit(
        scope=LimitScope.WIDGET_ORIGIN,
        subject=origin,
        period="minute",
        limit=mints_per_minute,
        window_seconds=MINUTE_SECONDS,
        reason="new anonymous sessions one site may open a minute",
    )
    if live_sessions >= max_live:
        return MintDecision(
            minted=False,
            # A whole window, because a live session ends when its holder goes away and
            # nothing here knows when that will be. An optimistic hint would have the
            # caller back immediately for the same answer.
            retry_after_seconds=MINUTE_SECONDS,
            reason=(
                f"{origin} already holds {live_sessions} live sessions against a ceiling of "
                f"{max_live}; existing sessions are unaffected"
            ),
        )
    decision = check(now=now, limits=(limit,), state=state)
    if decision.allowed:
        return MintDecision(
            minted=True,
            retry_after_seconds=0.0,
            reason=f"{origin} is within {mints_per_minute} mints a minute and {max_live} live",
        )
    return MintDecision(
        minted=False,
        retry_after_seconds=decision.retry_after_seconds,
        reason=decision.reason,
    )


# ---------------------------------------------------------------------- abuse detection
class VolumeBand(enum.IntEnum):
    """How unusual a principal's recent volume is against their own history.

    Ordered so "at least this unusual" is a comparison. Bands rather than a raw ratio
    because the number a person acts on is "is this worth looking at", and three answers
    is as many as anybody triages.
    """

    ORDINARY = 0
    NOTABLE = 1
    EXTREME = 2


#: Multiples of a principal's own baseline at which their volume becomes worth a look, and
#: then worth an alert. Against their own baseline rather than a company-wide one: the
#: finance director legitimately asks ten times as much as anybody else, and a company-wide
#: threshold would page somebody about her every month while missing the sales account that
#: went from four questions a day to sixty.
VOLUME_NOTABLE_RATIO = 4.0
VOLUME_EXTREME_RATIO = 10.0

#: Below this many observations, a ratio means nothing. Two questions against a baseline of
#: 0.2 is a 10x spike and is also somebody asking two questions.
VOLUME_MIN_OBSERVATIONS = 20


@dataclass(frozen=True)
class VolumeAssessment:
    """A ratio, a band, and nothing that could be read as a verdict.

    There is no field here meaning "stop them", and adding one is the regression. See
    `ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE`.
    """

    observed: int
    baseline: float
    ratio: float
    band: VolumeBand
    note: str

    @property
    def is_notable(self) -> bool:
        return self.band >= VolumeBand.NOTABLE

    @property
    def is_extreme(self) -> bool:
        return self.band >= VolumeBand.EXTREME


def assess_volume(
    *, observed: int, baseline: float, min_observations: int = VOLUME_MIN_OBSERVATIONS
) -> VolumeAssessment:
    """Score one principal's recent volume against their own baseline. Decides nothing.

    A baseline of zero is a principal with no history, and the honest answer for them is
    ORDINARY rather than infinity: everybody's first busy day would otherwise be an alert,
    and the first week after a rollout would be nothing but alerts.
    """
    if observed < 0:
        msg = "observed volume cannot be negative"
        raise ValueError(msg)
    if baseline < 0:
        msg = "a baseline cannot be negative"
        raise ValueError(msg)
    ratio = observed / baseline if baseline > 0 else 0.0
    if observed < min_observations:
        return VolumeAssessment(
            observed=observed,
            baseline=baseline,
            ratio=ratio,
            band=VolumeBand.ORDINARY,
            note=(
                f"{observed} observation(s) is below the {min_observations} needed for a "
                "ratio to mean anything"
            ),
        )
    if ratio >= VOLUME_EXTREME_RATIO:
        band = VolumeBand.EXTREME
    elif ratio >= VOLUME_NOTABLE_RATIO:
        band = VolumeBand.NOTABLE
    else:
        band = VolumeBand.ORDINARY
    return VolumeAssessment(
        observed=observed,
        baseline=baseline,
        ratio=ratio,
        band=band,
        note=f"{observed} against a baseline of {baseline:.1f} is {ratio:.1f}x",
    )


class DenialShape(enum.StrEnum):
    """What a run of permission denials looks like, which decides who it goes to.

    The distinction is the point. Twenty denials against one record is somebody who needs
    access to that record, and it belongs in front of the department admin who can grant
    it. Twenty denials against twenty different records is somebody finding out what exists,
    and it belongs in front of whoever watches for that. Reporting both as "denial rate"
    sends both to the same place and the first one never gets fixed.
    """

    ORDINARY = "ordinary"
    ACCESS_NEEDED = "access_needed"
    ENUMERATION = "enumeration"


#: Denials in the window below which nothing is worth saying. People mistype client names.
DENIALS_WORTH_NOTICING = 8

#: Distinct targets at or above which a run of denials is breadth rather than persistence.
ENUMERATION_DISTINCT_TARGETS = 5


@dataclass(frozen=True)
class DenialAssessment:
    """A shape and a sentence. Like `VolumeAssessment`, it cannot refuse anything.

    It reads the DENIED outcome from the audit log, which exists for exactly this: the
    person asking was told ABSENT, because telling them "you may not see the contract value
    for SNM" confirms SNM has one. The audit record is the only place the difference
    survives, and this is what it survives *for*.
    """

    denials: int
    distinct_targets: int
    shape: DenialShape
    note: str

    @property
    def is_worth_alerting(self) -> bool:
        return self.shape is not DenialShape.ORDINARY


def assess_denials(*, denials: int, distinct_targets: int) -> DenialAssessment:
    """Classify a run of permission denials. Alerts a person; never touches the request path.

    Note what this deliberately cannot do. It cannot make the next request fail, it cannot
    raise the assurance required, and it cannot narrow anybody's entitlements. A caller who
    is enumerating is being denied by the gate already, on every single attempt, which is
    the actual defence; this only makes sure somebody finds out it is happening.
    """
    if denials < 0 or distinct_targets < 0:
        msg = "denials and distinct targets are counts and cannot be negative"
        raise ValueError(msg)
    if distinct_targets > denials:
        msg = f"{distinct_targets} distinct targets across {denials} denials is not possible"
        raise ValueError(msg)
    if denials < DENIALS_WORTH_NOTICING:
        return DenialAssessment(
            denials=denials,
            distinct_targets=distinct_targets,
            shape=DenialShape.ORDINARY,
            note=f"{denials} denial(s) is below the {DENIALS_WORTH_NOTICING} worth noticing",
        )
    if distinct_targets >= ENUMERATION_DISTINCT_TARGETS:
        return DenialAssessment(
            denials=denials,
            distinct_targets=distinct_targets,
            shape=DenialShape.ENUMERATION,
            note=(
                f"{denials} denials across {distinct_targets} different targets is breadth, "
                "not persistence; somebody is finding out what exists"
            ),
        )
    return DenialAssessment(
        denials=denials,
        distinct_targets=distinct_targets,
        shape=DenialShape.ACCESS_NEEDED,
        note=(
            f"{denials} denials against only {distinct_targets} target(s) is somebody who "
            "needs access to something specific; route it to whoever can grant it"
        ),
    )


# ------------------------------------------------------------------- automated traffic
def is_automated(kind: PrincipalKind, traffic: TrafficClass) -> bool:
    """Whether this traffic is a machine's. A lookup, never a heuristic.

    Both halves are needed and neither is sufficient. A SERVICE principal is a machine
    whatever channel it arrives on. And a human principal on the scheduler or the webhook
    path is machine traffic too: a report that runs nightly under somebody's name is not
    that person asking a question, and counting it as one is how "fast lane share" and
    "questions per person" both quietly stop meaning anything.
    """
    if kind is PrincipalKind.SERVICE:
        return True
    return traffic in (TrafficClass.AUTOMATION, TrafficClass.SYSTEM)


def counts_towards_metrics(kind: PrincipalKind, traffic: TrafficClass) -> bool:
    """Whether this request belongs in the human-facing numbers.

    Architecture §22 asks for the fast lane's share "excluding machine traffic", and §25's
    telemetry is what a person's experience is judged against. Machine traffic is not
    removed from the *system* metrics, which still have to include it or capacity planning
    is done against a fiction; it is excluded from the numbers about people.
    """
    return not is_automated(kind, traffic)
