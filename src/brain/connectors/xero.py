"""Xero: one tenant's ledger, on an allowance that belongs to the client and refills at midnight.

Everything here follows from one fact that no other connector in this repository has to
deal with. **Xero's ceiling is 5,000 calls a day, per tenant, and it is the client's tenant
rather than our subscription**, so it is shared with every other integration the client
runs and there is no plan anybody can buy that moves it. `brain.ops.limits` records the
number, dated, with that consequence written beside it, and this module reads it rather
than restating it.

A daily allowance is a different kind of thing from a per-second one, and the difference is
the design:

**A day can be exhausted, and then nothing helps.** A minute window refills while somebody
is still reading the error. A day window does not: spend it before lunch and there is no
accounting data in the building until the reset. So the platform's backoff arithmetic is
not sufficient on its own here. `brain.ops.limits.backoff_seconds` caps a wait at 300
seconds, which is right for a minute window and wrong for this one: the recorded 429
carries `Retry-After: 1847` with `X-DayLimit-Remaining: 0`, and coming back after five
minutes spends calls we do not have on a refusal we already knew about. `retry_delay`
below takes the longest of the three honest waits. See
`A_DAILY_CEILING_OUTLASTS_THE_BACKOFF_CAP`.

**Our own window counts our own calls, and the tenant's day is not ours to count.**
`brain.ops.limits` holds the sliding window and this module does not open a second one. It
adds the one measurement that window structurally cannot have: `X-DayLimit-Remaining`, the
tenant-wide figure that already includes whatever the client's payroll export spent
overnight. Both apply, and neither subsumes the other. See
`BOTH_THE_WINDOW_AND_THE_TENANT_FIGURE_APPLY`.

**Absent, refused and unreachable are three answers and stay three.** An empty
`Invoices` array is a fact about the ledger; a 401 is Xero declining to talk to us; a 429
or a 5xx is Xero not answering. Collapsing them produces the failure this whole system
exists to avoid, which is an outage summarised as "nothing owing". `XeroReply` refuses at
construction to carry rows or a read time on a failure, so answering a rate limit from
memory is not something a caller can express rather than something they are asked not to
do. What a *person* is told is the same sentence for a refusal and for an unreachable
source, because that distinction is ours to act on and not theirs; the trace keeps it.

**The tenant is pinned at connect, not passed at call time.** A Xero token can reach every
organisation its connection was authorised for, so a connector scoped to "whatever the
token reaches" has the token's blast radius. `ConnectorScope` argues this at length and is
used here rather than imitated: exact membership, never a prefix, so a pin of `tenant_0447`
does not quietly admit `tenant_04471`. The tenant travels as `Xero-Tenant-Id`, which is the
only header this module contributes, and it is deliberately not a spec parameter:
`brain.connectors.rest.ParameterSpec` refuses header parameters because a header is where
`Authorization` gets typed into a configuration file, and that refusal is worth more than
the convenience of expressing the tenant as data.

**The projection is where this becomes dangerous, and money is the sharp end.** The rules
are stated where they belong and this module meets them:

- `amount_due` is **RESTRICTED**, it is **mapped and returned live**, and it is **never
  projected**. It is the field that makes the feature useful, and it is also the field the
  permission canaries protect (`invoice.amount_due` in `tests/fixtures/company.py`). It is
  not on `brain.core.projection.NEVER_PROJECT`, and that is exactly why the refusal has to
  live here: declared as a `FieldShape.STATUS` it passes all five clauses of
  `manifest.projectability` without anybody noticing. See
  `MONEY_IS_ANSWERED_LIVE_AND_NEVER_STORED`.
- A contact's email, phone, address and bank details are refused by the platform denylist
  by shape, and this module maps none of them and classifies none of them, so
  default-deny withholds them from everybody. Adding a rule for one is a decision for
  whoever owns the field policy, not a side effect of somebody adding a connector.
- `tax_number` is the gap worth naming. The denylist spells `nric`, `passport`, `nin` and
  `ssn`; it does not spell the tax identity number that appears on every invoice, and for
  a sole trader that number is a personal identity number. It is refused from the
  projection here, by name, and classified RESTRICTED so it can still answer a compliance
  question live.

**A field is mapped only if something classifies it.** Three declarations have to agree:
what the REST mapping names, what the manifest projects, and what the field policy
classifies. A mapped field with no rule is withheld from everybody and travels through
this process for nothing; a projected field with no mapping is a column that never
arrives. The relation is checked rather than asserted, which is what stops the three
drifting the next time somebody adds an endpoint.

Two more traps the recordings caught, and both fail silently:

**A Xero date is not ISO.** It is `/Date(1794700800000+0000)/`, milliseconds since the
epoch. Read as seconds it lands in the year 58854; parsed as ISO it raises, which is at
least loud. `parse_xero_timestamp` accepts that form and nothing else, and an undatable
value is dropped rather than guessed: a bare ISO string with no zone would have to be
assumed UTC for a ledger keeping New Zealand time, which is thirteen hours of error on a
due date and the difference between "overdue" and "due tomorrow".

**The reset hour is a parameter.** Xero's day resets at 00:00 NZT, which is UTC+12 or
UTC+13 depending on the season, and `tzdata` is not a dependency on this platform. A
hard-coded offset would be an hour wrong for half the year, and the hour it is wrong is
the hour the allowance refills. So the reset instant is supplied by the caller, in the
shape `brain.models.routing.CircuitBreaker` uses for `now`.

Rejected, and worth stating:

*Catching a spec disagreement and reporting UNREACHABLE.* A 200 whose body does not hold
`Invoices` is our declaration or the vendor's envelope being wrong, and
`brain.connectors.rest` already refuses it rather than reporting an absence. Softening
that here would turn a shape change into a degradation nobody investigates while it went
on spending the client's day.

*A second rate limiter.* See `throttle.ONE_BUCKET_AND_ONE_BREAKER`. The bucket is
`brain.ops.limits` and the breaker is `brain.models.routing.CircuitBreaker`, reached
through `brain.connectors.throttle`; this module classifies nothing about health that
`throttle.classify` already classifies.

*Trusting `X-DayLimit-Remaining` upwards.* Within one day the figure only falls. A reading
that goes up inside one window is a response that arrived out of order, and believing it
is how a burst spends an allowance that was already gone.

One disagreement is left standing rather than papered over.
`tests/fixtures/cassettes.py` records Xero's ceiling as raisable and `brain.ops.limits`
records it as not raisable, with the argument that it belongs to the client's tenant. This
module follows `brain.ops.limits`, which is the module the console and the ladder read, and
`day_limit` returns its figures so there is one place to correct.

Scope: domain logic. Nothing here opens a connection, resolves a name, reads a clock or
holds a credential. The resolver, the fetcher, `now` and the reset instant are all
parameters, and `assert_holds_no_credential` runs on the connection at construction.

Task ids: M11.6.5
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    ConnectorHealth,
    ConnectorScope,
    CredentialBinding,
    FetchRequest,
    HealthState,
    TransportKind,
    assert_fetches_only,
    assert_holds_no_credential,
)
from brain.connectors.federation import FailureReason, PartialAnswer, SourceFailure
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.projection import ProjectedRecord, ProjectedValue, RefreshPromise
from brain.connectors.rest import ID_TARGET, RestOperation, RestSpec, load_spec
from brain.connectors.throttle import CallOutcome, ceiling_for, classify, retry_delay
from brain.connectors.transports import FieldMapping, RestTransport, SourceRecord
from brain.core.envelope import IdentityMode, SideEffect, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.projection import ProjectionRefusedError
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import Freshness, StalenessHorizon, assess_freshness
from brain.ops.limits import ConnectorLimit, LimitDecision
from brain.ops.secrets import SecretRef
from brain.tools.fetch import Fetcher, Resolver

# ------------------------------------------------------------------ written-down reasons
#: Why a daily allowance is modelled at all, when a token bucket already exists.
A_DAILY_CEILING_IS_A_THING_THAT_CAN_BE_EXHAUSTED = (
    "A per-minute ceiling is a pace and a per-day ceiling is a budget, and only one of them "
    "can be spent. Against 5,000 calls a day per tenant, a backfill running at 30 calls a "
    "minute has spent the lot by lunchtime, and the client has no accounting data until "
    "midnight while every retry makes the queue longer and nothing shorter. So exhaustion is "
    "a state this connector can be in and report, rather than a 429 it keeps rediscovering."
)

#: Why the platform's backoff is not the whole answer to a Xero 429.
A_DAILY_CEILING_OUTLASTS_THE_BACKOFF_CAP = (
    "brain.ops.limits caps a backoff at 300 seconds, which is correct for a window that "
    "refills in sixty. The recorded daily refusal carries Retry-After: 1847 and "
    "X-DayLimit-Remaining: 0, so a client obeying the cap comes back six times inside the "
    "wait the source asked for, is refused six times, and each refusal is a call. The wait "
    "is therefore the longest of three honest numbers: what the platform computed, what the "
    "source asked for, and how long the day has left to run."
)

#: Why the source's own figure is read when we already count our calls.
BOTH_THE_WINDOW_AND_THE_TENANT_FIGURE_APPLY = (
    "Our sliding window counts calls this platform made. X-DayLimit-Remaining counts calls "
    "the tenant made, which includes the client's payroll export, their invoicing add-on and "
    "whatever their bookkeeper's app does overnight. Neither number can be derived from the "
    "other, and a connector trusting only its own is optimistic in exactly the way that ends "
    "the client's day early. This is not a second bucket: it holds no window and no log, and "
    "it answers 'has the tenant's day got room' rather than 'are we within our own share'."
)

#: Why an unreachable ledger is never reported as an empty one.
AN_UNREACHABLE_LEDGER_IS_NOT_AN_EMPTY_ONE = (
    "'Nothing is owing' and 'I could not read the ledger' are opposite answers and one of "
    "them is actionable. A connector that returns an empty list for a 429 produces the first "
    "sentence from the second fact, nobody files a bug because the answer looked like data, "
    "and the mistake is invisible in precisely the questions a finance connector exists for. "
    "So a failed reply has nowhere to put rows and nowhere to put a read time, which makes "
    "answering from memory unexpressible rather than merely discouraged."
)

#: Why the tenant is fixed at connect rather than chosen per call.
A_TENANT_IS_PINNED_AT_CONNECT = (
    "A Xero connection can be authorised for several organisations, and the token reaches "
    "every one of them. Choosing the organisation per call means the blast radius of a bug "
    "in a filter is another company's ledger, and 'we only ever query our own' is a property "
    "of the code rather than of the connection. Pinning it at connect makes the scope "
    "inspectable in a console row, and ConnectorScope matches by exact membership so "
    "tenant_0447 never admits tenant_04471."
)

#: Why the field that makes this connector useful is the field it must not store.
MONEY_IS_ANSWERED_LIVE_AND_NEVER_STORED = (
    "AmountDue is why anybody wants this connector and it is a payload rather than a "
    "pointer: it is the answer itself, not a way of finding the record that holds it. "
    "Stored, it is filtered and quoted as current long after the invoice was paid, which is "
    "the one number nobody forgives being wrong. It is not on the platform denylist, so "
    "nothing outside this module refuses it: declared as a status enum it passes all five "
    "clauses of the projectability test. The refusal is therefore here, by name, beside the "
    "mapping that fetches it live for whoever holds read:invoice.amount_due."
)

#: Why three declarations are checked against each other rather than reviewed separately.
A_FIELD_IS_MAPPED_ONLY_IF_SOMETHING_CLASSIFIES_IT = (
    "A mapping says what arrives, a projection says what is kept and a policy says who may "
    "read it, and they are edited by different people at different times. A mapped field "
    "with no rule is withheld from everybody by default-deny, which is safe and pointless: "
    "it travels through this process and into traces for nothing. A projected field with no "
    "mapping is a column that never arrives and therefore a filter that silently matches "
    "nothing. Both are invisible in review and cheap to check."
)

#: Why the source's own remaining figure is only ever believed downwards.
A_REMAINING_FIGURE_ONLY_FALLS_WITHIN_A_DAY = (
    "Inside one day window the tenant's remaining calls cannot go up. A reading that says "
    "otherwise is a response that arrived out of order, or a clock that is wrong, and "
    "believing it hands a burst the room it has already spent. Across a reset the figure "
    "does go up, which is why the reset instant is part of the value rather than inferred "
    "from the numbers moving."
)

#: What Xero's own permission model is, said plainly rather than implied by an empty predicate.
XERO_HAS_NO_PER_RECORD_VISIBILITY = (
    "Xero's unit of access is the organisation: anybody connected to it sees all of its "
    "invoices, and there is no per-record ACL to store a predicate from. So the visibility "
    "predicate is the tenant, which is the true statement, and all the narrowing for a Xero "
    "row comes from the capability the field policy requires and from the caller's own "
    "scope. Storing an unrestricted predicate instead would say the same thing while looking "
    "like the source's model had been carried across, and ProjectedEntity refuses it for "
    "exactly that reason."
)


# ------------------------------------------------------------------------------- names
CONNECTOR_NAME: Final = "xero"

#: The name this connector's verified ceiling is registered under in `brain.ops.limits`.
#: The same string as the connector name, and read rather than assumed: `day_limit` refuses
#: an unknown ceiling rather than inventing one.
CEILING_NAME: Final = "xero"

#: What `TypedResult.source` and `ProjectedRecord.source` carry. The specification is named
#: rather than embedded, for the reason `RestTransport.spec_ref` gives.
SPEC_REF: Final = "xero"

MANIFEST_VERSION: Final = "1.0.0"

BASE_URL: Final = "https://api.xero.com"

ENTITY_INVOICE: Final = "invoice"
ENTITY_CONTACT: Final = "contact"

#: The one header this connector contributes to a call. Not `Authorization`: that is minted
#: from a lease by whoever borrowed it, and nothing here ever sees it.
TENANT_HEADER: Final = "Xero-Tenant-Id"

#: The tenant-wide remaining call count for the day, which is the figure our own window
#: cannot see. The minute figure (`X-MinLimit-Remaining`) is deliberately not read: it is
#: superseded within sixty seconds and the minute is paced by `brain.ops.limits`, whereas a
#: spent day cannot be waited out inside the question that found it.
DAY_LIMIT_HEADER: Final = "X-DayLimit-Remaining"

RETRY_AFTER_HEADER: Final = "Retry-After"

#: A filter key naming the organisation. Accepted so a caller may state which tenant they
#: believe they are addressing, and checked against the pin; never used to choose one.
TENANT_FILTER: Final = "tenant"

#: How often a reconciliation pass reads the ledger back, and therefore how quickly a change
#: is guaranteed to reach us. Deliberately the pass's period and not the webhook's latency: a
#: webhook delivery that is lost leaves nothing behind to notice it by
#: (`brain.connectors.change_signal.SignalKindFacts`), so a promise made on the notification
#: is a promise nothing keeps. The arithmetic is affordable and worth stating: an hourly pass
#: over 2,000 open invoices at 100 records a page is 20 calls an hour, 480 a day, under a
#: tenth of the 5,000 the tenant has.
RECONCILIATION_INTERVAL: Final = timedelta(hours=1)

#: What a person is told, and what an operator is told, and they are different lengths on
#: purpose. Every one of these is a constant: a detail assembled from a response body would
#: put a filter value, and therefore a client's name, into a health row and a trace that have
#: a different audience from the answer they describe.
DETAIL_ANSWERED: Final = "answering"
DETAIL_RATE_LIMITED: Final = "the tenant's call allowance refused this call"
DETAIL_DAY_SPENT: Final = "the tenant's daily allowance is spent until the reset"
DETAIL_UNAUTHORISED: Final = "the source declined this connector's authorisation"
DETAIL_UNAVAILABLE: Final = "the source did not answer"
DETAIL_TIMED_OUT: Final = "the source did not answer in time"
DETAIL_NEVER_PROBED: Final = "nothing has probed this connector since it was installed"


class XeroError(ConnectorContractError):
    """A Xero connector was declared, or asked, for something it cannot hold.

    A `ConnectorContractError` for the reason that class gives: every refusal in this
    package is a mistake by whoever wrote or called the connector, it should stop the
    connector rather than degrade an answer, and nobody asking a question should ever see
    it. A request for a tenant this connection is not pinned to is that kind of mistake and
    not an outcome: there is no answer to give, so there is no reply shape for one.
    """


# --------------------------------------------------------------------- Xero's own dates
#: `/Date(1794700800000+0000)/` and `/Date(1794700800000)/`. The offset is the
#: organisation's local offset and is a display hint: the instant is unambiguous from the
#: milliseconds alone, so it is required to be well formed and then ignored.
_DOTNET_DATE_RE: Final = re.compile(r"^/Date\((?P<ms>-?\d{1,15})(?P<offset>[+-]\d{4})?\)/$")


def parse_xero_timestamp(value: object) -> datetime | None:
    """Xero's .NET epoch string as an aware instant, or None when it cannot be dated.

    Strict in the two directions that both fail quietly.

    **Milliseconds, never seconds.** The recorded due date is 1794700800000. Divided by a
    thousand it is November 2026; read as seconds it is the year 58854, which sorts, filters
    and renders without complaint and is wrong by fifty-six thousand years.

    **This form and no other.** A bare ISO string is refused rather than parsed, because
    Xero's ISO renderings carry no zone and this ledger keeps New Zealand time: assuming UTC
    would move every due date by thirteen hours, which is the difference between an invoice
    that is overdue and one that is not. None means "not stated", exactly as
    `brain.gate.provenance.read_time` means it, and the caller drops the field rather than
    inventing a value nobody sent.
    """
    if not isinstance(value, str):
        return None
    match = _DOTNET_DATE_RE.match(value.strip())
    if match is None:
        return None
    return datetime.fromtimestamp(int(match.group("ms")) / 1000.0, UTC)


# ------------------------------------------------------- the tenant's day (M11.3.1, M11.3.3)
def day_ceiling(manifest: ConnectorManifest) -> ConnectorLimit:
    """The verified ceiling this connector runs against, from where it was verified.

    Delegated to `brain.connectors.throttle.ceiling_for`, so a connector cannot declare its
    own ceiling and this module cannot be optimistic about one. The manifest is a parameter
    rather than built here, because the number an operator reads has to be the number the
    connector they are looking at actually runs against.

    It carries `raisable`, which an operator needs: there is no upgrade button for a limit
    that belongs to the client's tenant, and sending somebody to look for one wastes the
    hour in which the only available answer is to ask for less.
    """
    return ceiling_for(manifest)


def day_limit(manifest: ConnectorManifest) -> int:
    """How many calls the tenant's day holds. 5,000, and read rather than restated."""
    per_day = day_ceiling(manifest).per_day
    if per_day is None:
        msg = (
            f"the verified ceiling for {CEILING_NAME!r} publishes no daily figure; this "
            "connector's whole shape assumes a day that can be exhausted, and deriving one "
            "from the minute rate would produce a number that looks measured and is not"
        )
        raise XeroError(msg)
    return per_day


@dataclass(frozen=True)
class DayBudget:
    """What Xero says is left of this tenant's day, and when it refills.

    One observation rather than a counter, and the distinction matters: `remaining` is the
    source's own tenant-wide figure, which already includes every other integration the
    client runs. See `BOTH_THE_WINDOW_AND_THE_TENANT_FIGURE_APPLY`.

    `resets_at` is supplied rather than computed. Xero resets at 00:00 New Zealand time,
    which is UTC+12 or UTC+13 by season, and `tzdata` is not a dependency here; a fixed
    offset compiled in would be an hour wrong for half the year, over the hour the
    allowance refills.
    """

    remaining: int
    resets_at: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.remaining < 0:
            msg = (
                f"a remaining call count of {self.remaining} is not a count; the source "
                "reports zero when the day is spent, and a negative figure is a parse that "
                "went wrong being read as room"
            )
            raise XeroError(msg)
        for name, moment in (("resets_at", self.resets_at), ("observed_at", self.observed_at)):
            if moment.tzinfo is None:
                # The argument `brain.gate.provenance.read_time` makes about its own input.
                # A naive reset instant read in Singapore is eight hours out, which is eight
                # hours of believing an allowance has refilled when it has not.
                msg = (
                    f"{name} has no timezone; a reset instant compared against a naive clock "
                    "is off by whatever the reader's offset happens to be"
                )
                raise XeroError(msg)

    @property
    def is_exhausted(self) -> bool:
        """Whether the tenant has nothing left today. Not ill health: see `throttle`."""
        return self.remaining <= 0

    def seconds_until_reset(self, now: datetime) -> float:
        """How long the day has left to run. Zero once the reset has passed."""
        return max(0.0, (self.resets_at - now).total_seconds())

    def spend(self, calls: int = 1) -> DayBudget:
        """Our own decrement between two observations of the source's figure.

        Needed because the header only arrives with a response: three calls issued between
        two readings would each see the same remaining figure and each believe there was
        room. Floors at zero rather than going negative, so the value can only ever say
        "none left" and never "less than none", which nothing downstream would know how to
        compare.
        """
        if calls < 0:
            msg = "a call count cannot be negative; spending backwards is a refund nobody issued"
            raise XeroError(msg)
        return replace(self, remaining=max(0, self.remaining - calls))

    def merge(self, other: DayBudget) -> DayBudget:
        """Two readings of one tenant's day, resolved without inventing room.

        Across a reset the later reading wins whole, which is how the allowance refills. In
        one window the smaller remaining wins, whichever arrived last. See
        `A_REMAINING_FIGURE_ONLY_FALLS_WITHIN_A_DAY`.
        """
        if other.resets_at > self.resets_at:
            return other
        if self.resets_at > other.resets_at:
            return self
        return other if other.remaining < self.remaining else self


def day_remaining(headers: Mapping[str, str]) -> int | None:
    """The tenant's remaining calls from the response headers, or None when not stated.

    None rather than a default, for the reason `brain.ops.limits` refuses to invent a
    ceiling: a missing header means we did not learn anything, and a zero substituted for
    it would stop the connector on evidence nobody produced.
    """
    return _int_header(headers, DAY_LIMIT_HEADER)


def retry_after(headers: Mapping[str, str]) -> float | None:
    """What the source asked us to wait, in seconds, or None when it did not say."""
    raw = _int_header(headers, RETRY_AFTER_HEADER)
    return None if raw is None else float(raw)


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    """One header as a whole number, matched without regard to case.

    Header names are case-insensitive on the wire and case-sensitive in a dictionary, and a
    connector that only recognised the vendor's own capitalisation would silently stop
    reading the budget the day something normalised the headers on the way through.
    """
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() != wanted:
            continue
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    return None


def observe(
    previous: DayBudget | None,
    headers: Mapping[str, str],
    *,
    at: datetime,
    resets_at: datetime,
) -> DayBudget | None:
    """Fold a response's day figure into what we already believed.

    Returns `previous` unchanged when the source said nothing, rather than an optimistic
    fresh budget: a header that is absent is not a report of room.
    """
    remaining = day_remaining(headers)
    if remaining is None:
        return previous
    latest = DayBudget(remaining=remaining, resets_at=resets_at, observed_at=at)
    return latest if previous is None else previous.merge(latest)


@dataclass(frozen=True)
class CallVerdict:
    """Whether one call to Xero may be made now, and how long to wait when it may not.

    A value rather than an exception, because the caller has a decision to make with it: a
    question that cannot afford a Xero call is a question that can still be answered from
    the projection with its age attached, and raising here would take that choice away.
    """

    allowed: bool
    reason: str = ""
    wait_seconds: float = 0.0


def may_call(*, decision: LimitDecision, budget: DayBudget | None, now: datetime) -> CallVerdict:
    """Both allowances, and the longer wait when both refuse (M11.3.1).

    `decision` is `brain.ops.limits.check`'s, computed by the caller against the windows
    `throttle.limits_for` produces: this module does not re-run it, and could not come to a
    different conclusion if it tried. What is added is the tenant's own day figure. See
    `BOTH_THE_WINDOW_AND_THE_TENANT_FIGURE_APPLY`.

    A missing observation admits the call. It has to: nothing has answered yet today, so
    there is no figure, and refusing on its absence would make the first call of every day
    impossible and the connector permanently silent.
    """
    waits: list[float] = []
    reasons: list[str] = []
    if not decision.allowed:
        waits.append(decision.retry_after_seconds)
        reasons.append(decision.reason)
    if budget is not None and budget.is_exhausted:
        waits.append(budget.seconds_until_reset(now))
        reasons.append(DETAIL_DAY_SPENT)
    if not reasons:
        return CallVerdict(allowed=True)
    return CallVerdict(allowed=False, reason="; ".join(reasons), wait_seconds=max(waits))


def xero_retry_delay(
    *,
    retry_after_seconds: float,
    consecutive_refusals: int,
    budget: DayBudget | None,
    now: datetime,
    jitter: float = 0.0,
) -> float:
    """How long to wait after a refusal. The longest of three honest numbers.

    The platform's arithmetic is `brain.connectors.throttle.retry_delay` and is not
    restated. What is added is the pair of waits it cannot know about: what the source
    actually asked for, which its 300-second cap discards, and how long a spent day has
    left to run. See `A_DAILY_CEILING_OUTLASTS_THE_BACKOFF_CAP`.

    Taking the longest is deliberate against taking the platform's. A wait that is too long
    costs one question its freshness; a wait that is too short costs a call out of an
    allowance that does not refill until midnight, and then does it again.
    """
    platform = retry_delay(
        retry_after_seconds=retry_after_seconds,
        consecutive_refusals=consecutive_refusals,
        jitter=jitter,
    )
    until_reset = (
        budget.seconds_until_reset(now) if budget is not None and budget.is_exhausted else 0.0
    )
    return max(platform, max(0.0, retry_after_seconds), until_reset)


# ----------------------------------------------------------- the connection (M11.2.3)
@dataclass(frozen=True)
class XeroConnection:
    """One organisation, decided at connect, and nothing else.

    No client, no session and no credential: `assert_holds_no_credential` runs on the class
    at construction rather than being promised in a comment, so a later attribute called
    `api_token` fails the first time anybody builds one. See
    `contract.ROTATION_NEEDS_NO_REDEPLOY` for what that buys.

    The scope is built rather than stored, so the refusals in `ConnectorScope` are this
    class's refusals: a selector of `*`, of `all`, or of anything that narrows nothing is
    refused at connect, in front of whoever is installing the connector.
    """

    tenant_id: str

    def __post_init__(self) -> None:
        assert_holds_no_credential(type(self))
        # Constructing the scope is the check: ConnectorScope refuses an unbounded selector
        # and a selector the source would not recognise. Repeating either rule here would be
        # a second opinion about what "narrows nothing" means.
        self.scope()

    def scope(self) -> ConnectorScope:
        """What this connector was connected to. One tenant, named."""
        return ConnectorScope(resource_kind="tenant", selectors=(self.tenant_id,))

    def admits(self, tenant_id: str) -> bool:
        """Whether this connection covers that organisation. Exact membership, never a prefix."""
        return self.scope().admits(tenant_id)

    def assert_admits(self, tenant_id: str) -> None:
        """Refuse a call addressed to an organisation this connection is not pinned to.

        Refused rather than answered from the pinned tenant, which is the tempting version
        and is worse: it returns another company's figures under the name of the one that
        was asked for, and every test passes because the rows are real.
        """
        if not self.admits(tenant_id):
            msg = (
                f"this connection is pinned to one Xero organisation and was asked for "
                f"{tenant_id!r}; a token reaches every organisation it was authorised for, "
                f"so the pin is the only thing narrowing it. {A_TENANT_IS_PINNED_AT_CONNECT}"
            )
            raise XeroError(msg)

    def call_headers(self) -> Mapping[str, str]:
        """The headers this connector contributes to every call, and there is exactly one.

        The tenant, because Xero routes by it and a call without it is a call to whichever
        organisation the token happens to list first. Not the credential: that is minted
        from a lease by whoever borrowed it, for the duration of one run, and nothing in
        this module can see it or would have anywhere to keep it.
        """
        return MappingProxyType({TENANT_HEADER: self.tenant_id})

    def visibility(self) -> Scope:
        """Xero's own permission model as a predicate. See `XERO_HAS_NO_PER_RECORD_VISIBILITY`."""
        return Scope(clauses=(Clause(field="tenant_id", op=Op.EQ, value=self.tenant_id),))


# -------------------------------------------------------------- the spec (M11.1.3)
def _string_property_schema(*names: str) -> Mapping[str, Any]:
    return {"type": "object", "properties": {name: {"type": "string"} for name in names}}


def spec_document() -> Mapping[str, Any]:
    """The minimum OpenAPI this connector needs, as data.

    Two operations and one server, because `load_spec` refuses a document listing several
    and the reason carries: a document naming production and sandbox leaves which host is
    called to list order, and only one of them was checked.

    `LineItems` is deliberately absent from the invoice schema. Declaring it would give the
    response two arrays and `load_spec` would refuse the document, which is the correct
    outcome twice over: which array holds the records would be decided by key order, and
    `invoice_line` is on the platform's permanent denylist, so there is nothing there this
    connector may keep anyway.
    """
    invoice = {
        "type": "object",
        "properties": {
            "InvoiceID": {"type": "string"},
            "InvoiceNumber": {"type": "string"},
            "Status": {"type": "string"},
            "DueDate": {"type": "string"},
            "AmountDue": {"type": "string"},
            "Contact": _string_property_schema("ContactID", "Name"),
        },
    }
    contact = _string_property_schema(
        "ContactID", "Name", "ContactStatus", "UpdatedDateUTC", "TaxNumber"
    )
    query = [
        {"name": "where", "in": "query", "required": False},
        {"name": "page", "in": "query", "required": False},
    ]
    return {
        "openapi": "3.0.3",
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/api.xro/2.0/Invoices": {
                "get": {
                    "operationId": "getInvoices",
                    "parameters": query,
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "Invoices": {"type": "array", "items": invoice}
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/api.xro/2.0/Contacts": {
                "get": {
                    "operationId": "getContacts",
                    "parameters": query,
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "Contacts": {"type": "array", "items": contact}
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
        },
    }


def load_xero_spec(*, resolver: Resolver) -> RestSpec:
    """Parse the document and refuse its address before anything is built.

    The resolver is a parameter for the reason `brain.connectors.rest` gives: the address
    check is the same one the skill importer applies, imported rather than restated, and a
    module that resolved names itself could not be tested against the case that matters,
    which is a name answering publicly and then privately.
    """
    return load_spec(spec_document(), resolver=resolver)


#: What arrives from each endpoint. Every target is classified by `xero_field_policy` and
#: the relation is checked, not asserted: see `A_FIELD_IS_MAPPED_ONLY_IF_SOMETHING_CLASSIFIES_IT`.
#:
#: `amount_due` is here and is deliberately not in the projection below. It is the answer to
#: the question people actually ask, it is RESTRICTED, and it is fetched live every time.
INVOICE_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="InvoiceID"),
    FieldMapping(target="invoice_number", source_path="InvoiceNumber"),
    FieldMapping(target="contact_id", source_path="Contact.ContactID"),
    FieldMapping(target="status", source_path="Status"),
    FieldMapping(target="due_date", source_path="DueDate"),
    FieldMapping(target="amount_due", source_path="AmountDue"),
)

#: No EmailAddress, no Phones, no Addresses, no BankAccountDetails. Each is refused by the
#: platform denylist by shape, and none of them is mapped either: a field nothing
#: classifies is withheld from everybody, so mapping one would move a contact's personal
#: details through this process and into a trace in exchange for nothing at all.
CONTACT_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="ContactID"),
    FieldMapping(target="name", source_path="Name"),
    FieldMapping(target="status", source_path="ContactStatus"),
    FieldMapping(target="updated_at", source_path="UpdatedDateUTC"),
    FieldMapping(target="tax_number", source_path="TaxNumber"),
)


def invoice_transport() -> RestTransport:
    return RestTransport(
        spec_ref=SPEC_REF, operation="getInvoices", entity=ENTITY_INVOICE, fields=INVOICE_FIELDS
    )


def contact_transport() -> RestTransport:
    return RestTransport(
        spec_ref=SPEC_REF, operation="getContacts", entity=ENTITY_CONTACT, fields=CONTACT_FIELDS
    )


def operation_for(entity: str, *, resolver: Resolver) -> RestOperation:
    """The bound operation for one entity kind, spec and mapping compared."""
    transports = {ENTITY_INVOICE: invoice_transport, ENTITY_CONTACT: contact_transport}
    build = transports.get(entity)
    if build is None:
        msg = (
            f"this connector reads {sorted(transports)} and was asked for {entity!r}; an "
            "entity nothing maps would be fetched as an empty result, which reads as an "
            "empty ledger"
        )
        raise XeroError(msg)
    return load_xero_spec(resolver=resolver).bind(build())


def connector_fetch(
    connection: XeroConnection,
    entity: str,
    *,
    fetcher: Fetcher,
    resolver: Resolver,
    fetched_at: str,
) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
    """This connection's read side, as the one shape a connector's fetch may take (M11.1.1).

    The tenant is checked before an address is built, so a request naming another
    organisation never reaches the transport, never resolves a name and never spends a call
    out of anybody's allowance. A request naming no tenant inherits the pin, which is the
    ordinary case: the connection already decided, and making every caller repeat it would
    give them somewhere to get it wrong.

    `assert_fetches_only` runs on the closure rather than on this function, because the
    closure is the object a registry would call and therefore the object whose signature has
    to be shown never to receive the caller's grants.
    """
    inner = operation_for(entity, resolver=resolver).as_fetch(
        fetcher=fetcher, resolver=resolver, fetched_at=fetched_at
    )

    def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
        for key, value in request.filters:
            if key == TENANT_FILTER:
                connection.assert_admits(value)
        kept = tuple((k, v) for k, v in request.filters if k != TENANT_FILTER)
        return inner(replace(request, filters=kept))

    assert_fetches_only(_fetch)
    return _fetch


# ----------------------------------------------------- the projection (M11.4.2, M11.4.3)
#: Fields this connector fetches live and may never store, in Xero's spelling and in ours.
#: Everything here is either money or an identity number, and none of it is caught by
#: `brain.core.projection.NEVER_PROJECT`: `contract_value` and `margin` are on that list and
#: `amount_due` is not, which is a difference in vocabulary rather than in kind. `nric`,
#: `passport`, `nin` and `ssn` are on it and `tax_number` is not, and a tax identity number
#: for a sole trader is a personal identity number.
NEVER_PROJECTED_FROM_XERO: Final[frozenset[str]] = frozenset(
    {
        "amount_due",
        "amount_paid",
        "amount_credited",
        "total",
        "sub_total",
        "total_tax",
        "currency_rate",
        "tax_number",
        "line_items",
        "invoice_line",
    }
)

_CAMEL_BOUNDARY: Final = re.compile(r"(.)([A-Z][a-z]+)")
_LOWER_UPPER: Final = re.compile(r"([a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    """`AmountDue` and `amount_due` as one string, so the guard reads both spellings.

    A connector's own targets are snake case and the vendor's are camel case, and a rule
    written against one of them is a rule that a mapping evades by using the other.
    """
    stepped = _CAMEL_BOUNDARY.sub(r"\1_\2", name.strip())
    return _LOWER_UPPER.sub(r"\1_\2", stepped).lower()


def assert_federated_only(entity: str, names: Iterable[str]) -> None:
    """Refuse a declaration that would store what this connector answers live (M11.4.4).

    A `ProjectionRefusedError` rather than an error of this module's own, because it is
    exactly that refusal: the platform's denylist and this list are one rule with two
    vocabularies, and a caller catching one should not have to know about the other.

    This is the only enforcement point, deliberately. An identical check at ingest would
    look like a second defence and be an equivalent mutant, because the projection is built
    from the declared fields rather than copied from a row, so nothing undeclared can arrive
    to be caught. `brain.connectors.manifest.ProjectedEntity` records the same lesson about
    its own signal clause.
    """
    refused = sorted({n for n in names if _snake(n) in NEVER_PROJECTED_FROM_XERO})
    if refused:
        msg = (
            f"{entity} would project {refused}, which this connector fetches live and never "
            f"stores. {MONEY_IS_ANSWERED_LIVE_AND_NEVER_STORED}"
        )
        raise ProjectionRefusedError(msg)


#: What is kept locally about an invoice: enough to find it, join it and filter it, and not
#: one field of what it says. The record id is not listed because it is not one of the
#: fields: `ProjectedRecord.source_id` carries it, and declaring it again would count it
#: twice against the twelve.
INVOICE_PROJECTED: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(name="invoice_number", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,)),
    ProjectedField(name="contact_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN,)),
    ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)),
    ProjectedField(name="due_date", shape=FieldShape.TIMESTAMP, uses=(HotUse.FILTER, HotUse.SORT)),
)

CONTACT_PROJECTED: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(name="name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY, HotUse.JOIN)),
    ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
    ProjectedField(name="updated_at", shape=FieldShape.TIMESTAMP, uses=(HotUse.SORT,)),
)

PROJECTED_FIELDS: Final[Mapping[str, tuple[ProjectedField, ...]]] = MappingProxyType(
    {ENTITY_INVOICE: INVOICE_PROJECTED, ENTITY_CONTACT: CONTACT_PROJECTED}
)


def projection_for(entity: str, connection: XeroConnection) -> ProjectedEntity:
    """One entity kind's projection, refused here before the manifest ever sees it.

    Two refusals run before `ProjectedEntity`'s own five clauses, and they catch what those
    cannot. `assert_federated_only` catches the money and the identity number, which pass
    every clause when declared as a status. The visibility predicate is the tenant, which is
    Xero's actual model: see `XERO_HAS_NO_PER_RECORD_VISIBILITY`.
    """
    declared = PROJECTED_FIELDS.get(entity)
    if declared is None:
        msg = f"this connector projects {sorted(PROJECTED_FIELDS)} and was asked for {entity!r}"
        raise XeroError(msg)
    assert_federated_only(entity, (f.name for f in declared))
    return ProjectedEntity(
        entity=entity,
        fields=declared,
        change_signal=ChangeSignal.WEBHOOK,
        visibility=connection.visibility(),
    )


def refresh_promise() -> RefreshPromise:
    """What the source has undertaken, at the interval the reconciliation pass keeps.

    The signal is the webhook and the interval is the pass, and that pairing is the honest
    one: a lost webhook delivery leaves nothing behind to notice it by, so an interval taken
    from the notification's latency would be a promise made on a mechanism that cannot
    report its own failure.
    """
    return RefreshPromise(signal=ChangeSignal.WEBHOOK, interval=RECONCILIATION_INTERVAL)


def projected_record(
    entity: str, row: Mapping[str, Any], *, last_seen_at: datetime
) -> ProjectedRecord | None:
    """One projected row, built from what was declared rather than copied from what arrived.

    A fresh mapping over the declared fields, which is the shape
    `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE` argues for one
    layer up and the reason `amount_due` cannot land here even though the mapping fetches
    it: a copy would carry it the day somebody adds a target, and a build cannot.

    A declared field the row does not hold contributes nothing rather than a null, matching
    `RestOperation.project`. A `TIMESTAMP` field that cannot be dated is dropped for the same
    reason: `parse_xero_timestamp` refuses to guess, and a projection that stored the raw
    `/Date(...)/` string would be sorting invoices by a string that starts with a slash.

    Returns None for a row with no id, mirroring `transports.normalise`: a record that
    cannot be named cannot be refreshed, cited or matched to itself on the next fetch.
    """
    declared = PROJECTED_FIELDS.get(entity)
    if declared is None:
        msg = f"this connector projects {sorted(PROJECTED_FIELDS)} and was asked for {entity!r}"
        raise XeroError(msg)
    raw_id = row.get(ID_TARGET)
    if not isinstance(raw_id, str | int) or not str(raw_id).strip():
        return None

    fields: dict[str, ProjectedValue] = {}
    for declaration in declared:
        if declaration.name not in row:
            continue
        value = row[declaration.name]
        if declaration.shape is FieldShape.TIMESTAMP:
            dated = parse_xero_timestamp(value)
            if dated is not None:
                fields[declaration.name] = dated
            continue
        # Passed through rather than coerced. A value that is not a pointer is refused by
        # `ProjectedRecord` with the argument attached, and stringifying it here would turn
        # a nested object into a short label and defeat `A_NESTED_OBJECT_IS_NOT_ONE_FIELD`.
        fields[declaration.name] = value

    return ProjectedRecord(
        source=CONNECTOR_NAME,
        entity=entity,
        source_id=str(raw_id),
        last_seen_at=last_seen_at,
        fields=fields,
    )


# --------------------------------------------------------- the classifications (M4.2.1)
#: Every field this connector can return, and the capability that reaches it.
#:
#: `amount_due` is RESTRICTED and it is the point of the table. It is the same
#: classification the invariant suite already pins for `invoice.amount_due`, reached
#: independently: it is money, it is what the permission canaries protect, and it is
#: returnable to somebody holding `read:invoice.amount_due` while never being storable.
#: `brain.core.field_policy` names that combination as the ordinary case rather than the
#: exception, and this is one of them.
#:
#: `tax_number` is RESTRICTED for a reason worth stating: a contact may be a company or a
#: sole trader, the field cannot tell you which, and for a sole trader it is a personal
#: identity number. Classified at the higher of the two readings, because the alternative
#: is a classification that is correct for most rows.
#:
#: Nothing classifies a contact's email, phone, address or bank details. That is the answer
#: rather than an omission: default-deny withholds them from everybody, and adding a rule is
#: a deliberate act by whoever owns the policy.
XERO_FIELD_RULES: Final[tuple[FieldRule, ...]] = (
    FieldRule.of(
        ENTITY_INVOICE, "invoice_number", "read:invoice.invoice_number", Classification.INTERNAL
    ),
    FieldRule.of(ENTITY_INVOICE, "contact_id", "read:invoice.contact_id", Classification.INTERNAL),
    FieldRule.of(ENTITY_INVOICE, "status", "read:invoice.status", Classification.INTERNAL),
    FieldRule.of(ENTITY_INVOICE, "due_date", "read:invoice.due_date", Classification.INTERNAL),
    FieldRule.of(
        ENTITY_INVOICE, "amount_due", "read:invoice.amount_due", Classification.RESTRICTED
    ),
    FieldRule.of(ENTITY_CONTACT, "name", "read:contact.name", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "status", "read:contact.status", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "updated_at", "read:contact.updated_at", Classification.INTERNAL),
    FieldRule.of(
        ENTITY_CONTACT, "tax_number", "read:contact.tax_number", Classification.RESTRICTED
    ),
)


def xero_field_policy() -> FieldPolicy:
    """What this connector's fields require, as a policy fragment somebody merges."""
    return FieldPolicy(rules=XERO_FIELD_RULES)


def mapped_targets(entity: str) -> tuple[str, ...]:
    """Every field name this connector's mapping produces for one entity, except the id.

    The id is excluded because it is structural rather than classified: `SourceRecord`
    carries it as the record's name, and no field policy rule governs it anywhere in this
    system.
    """
    mappings = {ENTITY_INVOICE: INVOICE_FIELDS, ENTITY_CONTACT: CONTACT_FIELDS}
    fields = mappings.get(entity)
    if fields is None:
        msg = f"this connector maps {sorted(mappings)} and was asked for {entity!r}"
        raise XeroError(msg)
    return tuple(f.target for f in fields if f.target != ID_TARGET)


def assert_declarations_agree() -> None:
    """The mapping, the projection and the policy, checked against each other (M11.4.5).

    Three lists edited by three different people at three different times, and every
    disagreement between them is invisible in review and silent at runtime. See
    `A_FIELD_IS_MAPPED_ONLY_IF_SOMETHING_CLASSIFIES_IT`.
    """
    policy = xero_field_policy()
    problems: list[str] = []
    for entity in (ENTITY_INVOICE, ENTITY_CONTACT):
        mapped = set(mapped_targets(entity))
        projected = {f.name for f in PROJECTED_FIELDS[entity]}
        unclassified = sorted(name for name in mapped if not policy.governs(entity, name))
        if unclassified:
            problems.append(
                f"{entity} maps {unclassified}, which nothing classifies; default-deny "
                "withholds them from everybody, so they travel for nothing"
            )
        unmapped = sorted(projected - mapped)
        if unmapped:
            problems.append(
                f"{entity} projects {unmapped}, which nothing maps; the column never "
                "arrives, so a filter on it silently matches nothing"
            )
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        msg = f"this connector's declarations disagree:\n{listed}"
        raise XeroError(msg)


# ------------------------------------------------------------------- the manifest (M11.1.7)
XERO_TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    ToolDeclaration(
        name="xero.read_invoices",
        description=(
            "Invoices in this Xero organisation: number, contact, status, due date and the "
            "amount still owing. Read-only, and the amount is fetched live rather than "
            "stored."
        ),
        entity=ENTITY_INVOICE,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
    ToolDeclaration(
        name="xero.read_contacts",
        description=(
            "Contacts in this Xero organisation: name, status and when they last changed. "
            "Read-only, and no contact details of any kind."
        ),
        entity=ENTITY_CONTACT,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
)


def xero_manifest(
    connection: XeroConnection, *, ref: SecretRef, version: str = MANIFEST_VERSION
) -> ConnectorManifest:
    """Everything this connector declares, in one value that can be hashed and pinned.

    `IdentityMode.SERVICE` rather than the DELEGATED default, and the override is the honest
    declaration rather than a preference: a Xero connection is one set of credentials for
    one organisation, and nobody in this company has a personal Xero login that maps to
    their principal. The consequence is stated in `brain.tools.registry`: a SERVICE tool
    must be registered beside a scope predicate, because the source will not narrow it for
    us. `XeroConnection.visibility` is that predicate, and it is the same one the projection
    stores.

    The binding is read-only by not saying otherwise, which is `CredentialBinding`'s default
    and the whole of `A_WRITE_GRANT_NAMES_SOMEBODY`. A connector that could raise an invoice
    is a different connector, approved by somebody named, and this is not it.
    """
    assert_declarations_agree()
    return ConnectorManifest(
        name=CONNECTOR_NAME,
        version=version,
        transport=TransportKind.REST,
        scope=connection.scope(),
        credential=CredentialBinding(ref=ref, mode=AccessMode.READ_ONLY),
        tools=XERO_TOOLS,
        projections=(
            projection_for(ENTITY_INVOICE, connection),
            projection_for(ENTITY_CONTACT, connection),
        ),
        ceiling=CEILING_NAME,
    )


# ------------------------------------------------------ what one call produced (M11.5.5)
class XeroOutcome(enum.StrEnum):
    """The four answers a call to Xero can produce, and they stay four.

    `ABSENT` is a fact about the ledger. `REFUSED` is Xero declining to talk to this
    connector, which is a job for whoever owns the connection. `UNREACHABLE` is Xero not
    answering, whether it said 429 or said nothing at all. See
    `AN_UNREACHABLE_LEDGER_IS_NOT_AN_EMPTY_ONE`.

    A person is told the same thing for the last two, because the difference is ours to act
    on rather than theirs, and the trace keeps it.
    """

    PRESENT = "present"
    ABSENT = "absent"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"

    @property
    def answered(self) -> bool:
        """Whether the source answered at all. The only place the four collapse to two."""
        return self in (XeroOutcome.PRESENT, XeroOutcome.ABSENT)


@dataclass(frozen=True)
class XeroReply:
    """One call's result: what it was, and rows only where there are rows.

    The constructor is the guarantee. A failed reply cannot carry rows and cannot carry a
    read time, so "answer the 429 from the last good response" is not something a caller can
    express here. A read time on a failure would be worse than the rows: `assess_freshness`
    would date it and report the answer as current.
    """

    outcome: XeroOutcome
    call: CallOutcome
    rows: TypedResult[SourceRecord] | None = None
    fetched_at: str = ""
    reason: FailureReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome.answered:
            if self.rows is None:
                msg = (
                    f"a {self.outcome} reply carries no rows; an answered call produces a "
                    "result even when the result is empty, and None here would make an "
                    "absence indistinguishable from a failure"
                )
                raise XeroError(msg)
            has_records = bool(self.rows.records)
            if has_records is not (self.outcome is XeroOutcome.PRESENT):
                msg = (
                    f"a {self.outcome} reply holds {len(self.rows.records)} record(s); "
                    "present and absent are decided by what came back, and a mismatch here "
                    "is an empty ledger being reported as a full one or the reverse"
                )
                raise XeroError(msg)
            return
        if self.rows is not None or self.fetched_at:
            msg = (
                f"a {self.outcome} reply was given rows or a read time. "
                f"{AN_UNREACHABLE_LEDGER_IS_NOT_AN_EMPTY_ONE}"
            )
            raise XeroError(msg)
        if self.reason is None:
            msg = (
                f"a {self.outcome} reply names no failure reason; the trace is the only "
                "place a refusal and an outage stay distinguishable, and it is assembled "
                "from this"
            )
            raise XeroError(msg)

    def freshness(self, *, horizon: StalenessHorizon, now: datetime) -> Freshness:
        """How old this is, in `brain.gate.provenance`'s vocabulary and not a second one.

        Unconditional: a failed reply has no read time, so `assess_freshness` returns
        UNSTATED by its own rule about a time it cannot date. A branch here would be a
        second implementation of that rule, and the constructor above is what makes this one
        sufficient.
        """
        return assess_freshness(self.fetched_at, horizon=horizon, now=now)

    def failure(self) -> SourceFailure | None:
        """This reply as the federation layer's failure record, or None when it answered."""
        if self.outcome.answered or self.reason is None:
            return None
        return SourceFailure(connector=CONNECTOR_NAME, reason=self.reason, detail=self.detail)

    def notice(self, *, disclosable: frozenset[str]) -> str:
        """What the asker is told. Named only if their own catalogue already named Xero.

        Delegated whole to `federation.PartialAnswer.notice`, so a Xero outage produces
        exactly the sentence every other unreachable source produces and the two are
        indistinguishable to somebody probing. Restating it here would be a second
        disclosure rule, and the generous copy is the one that gets read.
        """
        failure = self.failure()
        if failure is None:
            return ""
        return PartialAnswer(failed=(failure,)).notice(disclosable=disclosable)

    def trace_line(self) -> str:
        """The full statement, for an auditor. Keeps what the notice drops.

        Safe here for the reason `federation.PartialAnswer.trace_lines` is safe: a trace is
        read by somebody already entitled to know what this system connects to, and nothing
        in this module can put this string into a channel payload. It carries no value from
        the response body either: every detail is a constant in this module, so a client's
        name cannot arrive in it by way of a filter.
        """
        count = len(self.rows.records) if self.rows is not None else 0
        return f"{CONNECTOR_NAME}: {self.outcome} ({self.call}), {count} record(s), {self.detail}"


#: How a call's outcome becomes the reason the trace records. `REJECTED` maps to
#: `NOT_SERVING` because `FailureReason` has no member for "the source declined our
#: credential", and inventing one in another module's enum is not this connector's decision:
#: this connector is not serving requests until somebody re-authorises it, which is the
#: nearest true statement, and `detail` carries the distinction.
_REASON_FOR: Final[Mapping[CallOutcome, FailureReason]] = MappingProxyType(
    {
        CallOutcome.QUOTA: FailureReason.QUOTA,
        CallOutcome.REJECTED: FailureReason.NOT_SERVING,
        CallOutcome.UNAVAILABLE: FailureReason.TRANSPORT,
        CallOutcome.TRUNCATED: FailureReason.TRUNCATED,
    }
)

_DETAIL_FOR: Final[Mapping[CallOutcome, str]] = MappingProxyType(
    {
        CallOutcome.QUOTA: DETAIL_RATE_LIMITED,
        CallOutcome.REJECTED: DETAIL_UNAUTHORISED,
        CallOutcome.UNAVAILABLE: DETAIL_UNAVAILABLE,
    }
)


def interpret(
    operation: RestOperation,
    *,
    status: int | None,
    body: Any = None,
    fetched_at: str,
    timed_out: bool = False,
    connection_failed: bool = False,
    more_pages: bool = False,
) -> XeroReply:
    """One response, as an answer. The classification is `throttle.classify`'s (M11.3.3).

    The branch order is the rule and it is not this module's: `classify` checks a timeout
    before a status, and 429 before the generic client-error branch, and both orderings have
    arguments attached where they live. What this adds is the projection, and it happens
    only on the success branch. Projecting first would run the field mapping over a 429's
    error body, which does not hold `Invoices`, so a rate limit would surface as a
    specification error during somebody's question.

    `more_pages` is the caller's, matching `transports.normalise`: Xero pages at 100 records
    and publishes no result-set ceiling, so the only thing that knows an answer was cut
    short is whoever stopped asking for pages.
    """
    outcome = classify(status=status, timed_out=timed_out, connection_failed=connection_failed)
    if outcome is CallOutcome.OK:
        rows = operation.records(body, fetched_at=fetched_at, truncated=more_pages)
        answer = XeroOutcome.PRESENT if rows.records else XeroOutcome.ABSENT
        return XeroReply(
            outcome=answer, call=outcome, rows=rows, fetched_at=fetched_at, detail=DETAIL_ANSWERED
        )
    answer = XeroOutcome.REFUSED if outcome is CallOutcome.REJECTED else XeroOutcome.UNREACHABLE
    detail = DETAIL_TIMED_OUT if timed_out else _DETAIL_FOR.get(outcome, DETAIL_UNAVAILABLE)
    return XeroReply(
        outcome=answer,
        call=outcome,
        reason=_REASON_FOR.get(outcome, FailureReason.TRANSPORT),
        detail=detail,
    )


# ------------------------------------------------------------------- health (M11.1.1)
def health(
    reply: XeroReply | None, *, budget: DayBudget | None, checked_at: datetime
) -> ConnectorHealth:
    """What the last probe found, as a fact with a time on it.

    Three judgements, and each sends the row to a different person.

    **A spent day is DEGRADED, never DOWN.** The source is healthy and we are out of
    allowance, which is `throttle.A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH` stated as a health
    state. DOWN would send somebody to check whether Xero is up, which it is, and the only
    action available is to ask for less until the reset. It is checked before the reply's
    own outcome because a connector that answered one call and has nothing left for the next
    is not OK, whatever that call did.

    **A declined authorisation is DOWN.** It was working this morning and it is an incident
    for whoever owns the connection. UNCONFIGURED would file it as somebody's installation
    task and it would sit there.

    **No probe at all is UNCONFIGURED.** A connector nobody has called yet is a job for
    whoever installed it, and reporting DOWN would page somebody about a system that may be
    perfectly healthy.

    Every detail is a constant from this module. A health row assembled from a response body
    would carry a filter value, and therefore a client's name, into a console with a
    different audience and a different retention from the answer it described.
    """
    if reply is None:
        return ConnectorHealth(
            connector=CONNECTOR_NAME,
            state=HealthState.UNCONFIGURED,
            checked_at=checked_at,
            detail=DETAIL_NEVER_PROBED,
        )
    if budget is not None and budget.is_exhausted:
        return ConnectorHealth(
            connector=CONNECTOR_NAME,
            state=HealthState.DEGRADED,
            checked_at=checked_at,
            detail=DETAIL_DAY_SPENT,
        )
    states: Mapping[CallOutcome, HealthState] = {
        CallOutcome.OK: HealthState.OK,
        CallOutcome.TRUNCATED: HealthState.DEGRADED,
        CallOutcome.QUOTA: HealthState.DEGRADED,
        CallOutcome.REJECTED: HealthState.DOWN,
        CallOutcome.UNAVAILABLE: HealthState.DOWN,
    }
    return ConnectorHealth(
        connector=CONNECTOR_NAME,
        state=states[reply.call],
        checked_at=checked_at,
        detail=reply.detail,
    )
