"""Telling somebody a run of denials is happening, without telling them what was denied.

`brain.ops.limits.assess_denials` classifies a run of permission denials and stops there.
Commit c0ae875 left M23.2.2 unclaimed with the honest reason "no notification channel";
`brain.channels` now exists, so the leaf is buildable and this is it.

**What breaks without it.** `DenialShape` is computed and read by nobody. Somebody locked
out of a record they need waits until they think to complain, and somebody walking the
estate finding out what exists is denied on every attempt with nothing anywhere saying so.
The gate is the defence in both cases; this is the part that makes a person find out.

This is the easiest place in the system to break the rule everything else keeps, because an
alert is written for an operator and an operator wants numbers. "Priya was denied 40 times
on read:client.contract_value" is a report about hidden things: it says the field exists,
it says at least forty somethings sit behind it, and in front of the wrong reader it is a
map. DENIED and ABSENT must stay indistinguishable to a person, and no count of hidden
items may be emitted, including by subtraction. Four decisions follow.

**An alert is not exempt from the entitlement model.** A recipient is told only what they
already hold enough to have found out for themselves, and that is decided by the same
machinery every other narrowing uses: `EntitlementSet.intersect`, `scope_for` and
`Scope.matches`, in that order, exactly as `brain.audit.view` decides whether a reader may
see one ledger row. There is deliberately no second rule here. A second implementation of
the central invariant is a second place for it to be wrong, and the permissive one wins the
day the two disagree. See `AN_ALERT_IS_NOT_EXEMPT_FROM_THE_ENTITLEMENT_MODEL`.

**The alert names a shape and never a thing.** It carries who, and what the run looked
like, and nothing else: no capability, no entity, no field, no target, no value, and no
number at all. "One person, a spread of different places" is an operational fact about
behaviour; "denied on client.contract_value" is a fact about what exists. What that costs
is real and is stated rather than hidden: the recipient cannot act on the alert alone, and
has to go to the audit view, which filters per reader and is the one surface built to hold
the answer. See `THE_ALERT_NAMES_A_SHAPE_AND_NEVER_A_THING`.

**The subject of a run never receives the alert about themselves.** Told "you were denied
across many places", a person has been handed a probe oracle: they can measure the boundary
of what exists by watching whether the message arrives. So it is refused twice, in the
routing and again in the constructor, because the routing check is the one a refactor drops
and the constructor is the one a hand-built alert goes around. See
`THE_SUBJECT_IS_NEVER_A_RECIPIENT`.

**Alerts are digested into a window, and the window is a privacy parameter.** One alert per
recipient, subject and shape per window. An operator woken on every denial learns nothing,
and worse, the alert stream then *is* the denial log for anybody who can see alerts: at a
one-minute window an observer reads the subject's denial activity minute by minute, which is
the reconstruction the digest exists to prevent. So the window is chosen long rather than
short. It costs nothing to collapse several runs into one alert precisely because the alert
carries no count: there is no number to sum and none to lose. See
`A_DIGEST_IS_LOSSLESS_BECAUSE_THERE_IS_NO_COUNT`.

**Nothing here refuses anything**, the same rule and the same reason as `assess_denials` and
`brain.gate.injection`. There is no value on this module's public surface meaning "block",
no function returning a bare bool that could be read as a verdict, and nothing that can
reach the request path. A caller cannot start refusing on a denial pattern without adding a
way to say so and being seen in review. See `THIS_MODULE_HAS_NOWHERE_TO_REFUSE`.

Rejected: delivering the alert from here. `ChannelAdapter.send` raises
`DeliveryRefusedError`, so importing it would put a refusal on this module's surface by
inheritance, and the one thing this module must not have is somewhere to express one. The
caller holds the adapter; `ALERT_CLASSIFICATION` is what this module contributes to that
decision, decided once here rather than guessed at each call site.

Rejected: putting the counts in and trusting the recipient. The recipient is entitled to the
capability, so the argument goes that they could count it themselves. They could - through
the audit view, which is filtered, retained and audited in its own right. An alert is none
of those things: it is forwarded, pasted, and read by whoever is standing behind the person
on call. The two are not the same artefact and must not carry the same facts.

Rejected: grouping several capabilities into one pattern. The entitlement check would then
have to ask which of them a recipient must hold, and the only safe answer is all of them,
which quietly stops anybody being told anything. A run spanning several capabilities is
several patterns, so a recipient entitled to one and not the other is told about one and not
the other, which is the correct outcome rather than a compromise.

Nothing here reads a clock, opens a connection or stores anything. `now` is a parameter and
`AlertLog` is a snapshot somebody else keeps, for the same reason `LimiterState` is: a
debouncer owning its own store could not be tested at the window boundary, which is the only
part of a debouncer that is ever wrong.

Task ids: M23.2.2
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.scope import Clause, Op, Scope
from brain.ops.limits import DenialAssessment, DenialShape

# ------------------------------------------------------------------ written-down reasons
#: Why an alert goes through the entitlement model rather than around it.
AN_ALERT_IS_NOT_EXEMPT_FROM_THE_ENTITLEMENT_MODEL = (
    "An alert saying a person was denied is a statement that something exists which they "
    "could not see, so it is a disclosure like any other and it is entitled like any other. "
    "The recipient must hold what was denied, in a scope that admits where it happened, and "
    "that is decided by EntitlementSet.intersect, scope_for and Scope.matches rather than by "
    "a rule written here. One definition of narrower, so an alert and a run cannot disagree "
    "about what a person reaches; a second implementation would be a second place for the "
    "central invariant to be wrong, and the permissive one wins the day they differ."
)

#: Why the sentence names a shape rather than a capability or an object.
THE_ALERT_NAMES_A_SHAPE_AND_NEVER_A_THING = (
    "'One principal, a spread of different places' is a fact about behaviour. 'Denied on "
    "client.contract_value' is a fact about what exists, and 'denied 40 times' is a count of "
    "hidden things arrived at by subtraction. So the alert carries the shape, the person and "
    "nothing else: no capability, no entity, no field, no target, no value and no number. "
    "The cost is that the alert is not self-sufficient - the recipient has to open the audit "
    "view, which filters per reader and is retained and audited as a disclosure surface, "
    "which an alert forwarded into a chat window is not."
)

#: Why the person a pattern is about is never sent that pattern.
THE_SUBJECT_IS_NEVER_A_RECIPIENT = (
    "Tell somebody 'you were denied across many places' and they have a probe oracle: they "
    "can find the boundary of what exists by moving and watching whether the message comes. "
    "That is the enumeration the shape was computed to report, handed to the person doing "
    "it. The entitlement check does not catch this on its own, because the routing row is "
    "coarser than the predicate that denied them, so somebody denied by a clause the row "
    "does not carry still looks entitled at the row's granularity. Refused in the routing "
    "and again in the constructor, because a refactor drops the first and a hand-built alert "
    "goes around it."
)

#: Why alerts are digested, and why the window is long rather than short.
A_DIGEST_IS_LOSSLESS_BECAUSE_THERE_IS_NO_COUNT = (
    "An alert per denial reconstructs the denial log for anybody who can see alerts, and an "
    "operator woken four hundred times learns nothing from any of them. So one alert per "
    "recipient, subject and shape per window. The window is a privacy parameter before it is "
    "an ergonomic one: a short window makes the alert stream a fine-grained measurement of "
    "the subject's denial activity, which is the reconstruction being prevented, so the "
    "longer of two defensible values is the right one. Collapsing costs nothing because the "
    "alert carries no count: there is no number to sum, and none to lose."
)

#: Why this module has no way to say no, and why that is structural rather than agreed.
THIS_MODULE_HAS_NOWHERE_TO_REFUSE = (
    "The same argument as brain.gate.injection and assess_denials. A denial pattern is a "
    "heuristic over ordinary behaviour, and a heuristic that refuses teaches legitimate "
    "people to work around it while anybody adapting deliberately walks through it faster. "
    "What actually stops an enumerator is the gate, which is already denying every one of "
    "their attempts. So there is no value here meaning block, no public callable returning a "
    "bare bool that could be read as a verdict, and nothing that reaches the request path: a "
    "future caller cannot start refusing without adding somewhere to say it, in a diff."
)


# ------------------------------------------------------------------------------ the shape
#: What each alerting shape says, in words a person can act on and a stranger cannot use.
#:
#: Written as whole sentences rather than codes, for the reason `models.health.DepthAlert`
#: gives: this lands in front of whoever is on call, and "denial_shape=enumeration" makes
#: them go and read the source to find out whether it matters.
#:
#: `DenialShape.ORDINARY` is deliberately absent. A run below the noticing threshold has
#: nothing to say, and a sentence for it would be an alert nobody reads by the end of the
#: first week.
ALERT_TEXT: Mapping[DenialShape, str] = MappingProxyType(
    {
        DenialShape.ACCESS_NEEDED: (
            "A colleague keeps being told there is nothing there, always in the same place. "
            "That is what somebody missing a grant looks like from the outside. The audit "
            "view will say which grant, to whoever may read it."
        ),
        DenialShape.ENUMERATION: (
            "A colleague is being told there is nothing there across a spread of different "
            "places. That is breadth rather than persistence, and it is worth looking at "
            "before it is worth granting. Each attempt was stopped by the gate as it "
            "happened, so this is a thing to understand rather than a thing to stop."
        ),
    }
)

#: How sensitive an alert is, for whoever hands it to a channel adapter.
#:
#: Decided here so it is decided once. An alert names a colleague and says their access is
#: being looked at, which is not a thing to put on a consumer messaging app installed on a
#: personal phone - and `ChannelCapabilities.max_classification` defaults to INTERNAL, so a
#: surface has to have declared itself fit for this before `assert_can_send` will pass it.
#: Not RESTRICTED: the alert carries no field value, and classifying it alongside salary
#: would make every operator surface claim a ceiling it does not need for anything else.
ALERT_CLASSIFICATION: Classification = Classification.CONFIDENTIAL

#: How long one recipient, subject and shape waits before it may be said again.
#:
#: An hour. A run worth alerting on is minutes long at any human rate, so an hour still puts
#: an enumeration in front of somebody during the working session it happens in; and it caps
#: an operator at a handful of alerts a day per subject even if somebody enumerates all day.
#: The reason it is not five minutes is not comfort: the alert stream is visible to whoever
#: receives alerts, and a short window turns it into a minute-by-minute readout of the
#: subject's denial activity, which is the log this digest exists to not reproduce.
DIGEST_WINDOW = timedelta(hours=1)

#: The principal id on the requirement built by `requirement`. Not a person, and named so
#: it cannot be mistaken for one: a requirement that borrowed a real principal's id would
#: read in a trace as that person holding something they do not.
ALERT_REQUIREMENT = "requirement:denial_alert"

#: The grammar a routing attribute's name must follow. The same as `Scope`'s own
#: `Clause.field`, restated so the failure lands where the mistake was made rather than
#: inside a pydantic validator three calls away.
_ATTRIBUTE_RE = re.compile(r"^[a-z][a-z0-9_.]*$")

_NOWHERE: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class DenialPattern:
    """One run of denials, ready to be routed. What it is about, never what it found.

    `capability` and `where` exist for the entitlement check and for nothing else. Neither
    is ever rendered: they decide who may be told, and the thing they are told is
    `assessment.shape`. Keeping them on the input rather than on the output is the whole
    separation this module rests on.

    There is deliberately no timestamp of any denial. The digest window is keyed on when an
    alert is raised, not on when a hidden thing was asked for, and carrying the second would
    put the timing of the denial log into an artefact designed not to carry it.
    """

    #: The principal the denials are attributed to. Named in the alert, because an alert
    #: whose purpose is "grant this person access" cannot omit the person and still be
    #: actionable. `refusal_record` keeps the principal out of its subject on the grounds
    #: that a log line would be a second copy of an identity with its own retention; an
    #: alert is a message to one entitled recipient rather than a retained ops row, so the
    #: trade lands the other way and is stated rather than assumed.
    subject_id: str
    #: What was denied. Used to decide who may hear about it. Never rendered.
    capability: Capability
    #: The classifier's own answer, imported rather than recomputed. One definition of what
    #: a run of denials looks like, in `brain.ops.limits`, where the thresholds are.
    assessment: DenialAssessment
    #: The closed attributes of where the denials happened - `{"department": "maintenance"}`.
    #: Used to build the requirement and to evaluate a recipient's scope against, exactly as
    #: `brain.audit.view._scope_row` does. Never rendered.
    #:
    #: Empty is allowed and fails closed rather than open: an empty row satisfies no clause
    #: (`Clause.matches` refuses an absent field), so a pattern with no place attached can
    #: only reach somebody whose grant is company-wide.
    where: Mapping[str, str] = _NOWHERE

    def __post_init__(self) -> None:
        if not self.subject_id:
            msg = "a denial pattern is about somebody; an unattributed run routes to nobody"
            raise ValueError(msg)
        for name, value in self.where.items():
            if not _ATTRIBUTE_RE.match(name):
                msg = (
                    f"routing attribute {name!r} is not a scope field name; a clause built "
                    "from it would be rejected further in, where the mistake is invisible"
                )
                raise ValueError(msg)
            if not value:
                # An empty value is an EQ clause matching nothing, which reads in the
                # console as "nobody is entitled" when what happened is a blank in a row.
                msg = f"routing attribute {name!r} has an empty value and would admit nobody"
                raise ValueError(msg)

    @property
    def shape(self) -> DenialShape:
        return self.assessment.shape


@dataclass(frozen=True)
class DenialAlert:
    """One sentence, addressed to one person. Everything it could have said is missing.

    There is no capability, no entity, no field, no target, no value, and no number of any
    kind. The absences are the design and `THE_ALERT_NAMES_A_SHAPE_AND_NEVER_A_THING` is the
    argument; a test asserts the text carries no digit, because a count is the one thing an
    operator will add back in a hurry.
    """

    #: Who is being told. Established as entitled by `reach` before this is built.
    recipient_id: str
    #: Who it is about.
    subject_id: str
    shape: DenialShape
    #: When the alert was raised, which is the digest pass. Deliberately not the time of any
    #: denial: that would be a fact about when a hidden thing was asked for.
    raised_at: datetime
    text: str

    def __post_init__(self) -> None:
        if self.recipient_id == self.subject_id:
            # The second of the two refusals in `THE_SUBJECT_IS_NEVER_A_RECIPIENT`. Not a
            # refusal of anybody's request - the same distinction `limits.MintDecision`
            # draws - but a shape that cannot be built wrongly. The routing check below is
            # the one a refactor drops; this is the one a hand-built alert goes around.
            msg = (
                f"{self.recipient_id} is the subject of this pattern and cannot be told "
                "about it: an alert about your own denials is a probe oracle, and the "
                "boundary of what exists can be measured by watching whether it arrives"
            )
            raise ValueError(msg)
        if not self.text:
            msg = "an alert with no sentence in it is a notification that explains nothing"
            raise ValueError(msg)


#: One row of the debounce: who was told, about whom, in what shape.
AlertKey = tuple[str, str, DenialShape]

_NOTHING_SENT: Mapping[AlertKey, datetime] = MappingProxyType({})


@dataclass(frozen=True)
class AlertLog:
    """When each key was last said, as somebody else stored it.

    A snapshot rather than a store, for the same reason `LimiterState` is one: a debouncer
    holding its own client could not be tested at the window boundary, and the boundary is
    the only part of a debouncer that is ever wrong. In production these rows live wherever
    the limiter's windows live; nothing here knows that.
    """

    sent: Mapping[AlertKey, datetime] = _NOTHING_SENT

    def last_sent(self, key: AlertKey) -> datetime | None:
        return self.sent.get(key)

    def record(self, keys: Iterable[AlertKey], now: datetime) -> AlertLog:
        """Note that these keys have just been said. Returns a new log."""
        updated = dict(self.sent)
        for key in keys:
            updated[key] = now
        return AlertLog(sent=MappingProxyType(updated))


@dataclass(frozen=True)
class Digest:
    """What one pass produced, and the log to keep for the next one.

    Both together, and that is the opposite choice from `limits.check`, which hands back a
    decision and leaves recording to a second call. There the separation is load-bearing: a
    refused request must not extend its own window. Here there is nothing to refuse, and the
    failure mode runs the other way - a caller who forgets to store the log debounces
    nothing at all, silently, while every test of the window still passes.
    """

    alerts: tuple[DenialAlert, ...]
    log: AlertLog


def requirement(pattern: DenialPattern) -> EntitlementSet:
    """What somebody must hold before this pattern may be mentioned to them.

    One grant: the capability that was denied, in the place it was denied. Expressed as an
    `EntitlementSet` rather than as a pair of values so that `reach` can narrow it with the
    ordinary intersection instead of comparing scopes by hand.

    The scope is built from `where` rather than carried alongside it. Two fields would be
    two things that can disagree, and the disagreement is silent in whichever direction the
    caller happened to write.
    """
    clauses = tuple(
        Clause(field=name, op=Op.EQ, value=value) for name, value in sorted(pattern.where.items())
    )
    return EntitlementSet(
        principal_id=ALERT_REQUIREMENT,
        grants=(Grant(capability=pattern.capability, scope=Scope(clauses=clauses)),),
    )


def reach(pattern: DenialPattern, recipient: EntitlementSet, *, now: datetime) -> Scope | None:
    """The scope in which this recipient already reaches what the pattern is about.

    None means they reach none of it, and none of it is what they are told. Not a bool: a
    function returning one here would be a verdict with no name, and this returns the scope
    so the console can show *why* somebody is on the list rather than that they are.

    The intersection runs requirement-first, and that direction is load-bearing rather than
    stylistic. `EntitlementSet.intersect` keeps a grant of the receiver's only when the
    ceiling covers it, and `Capability.covers` expands only a trailing `.*` - so narrowing
    the *recipient* by a specific capability would drop the wildcard grant of somebody who
    plainly holds it. Narrowing the requirement by the recipient asks the question that was
    meant: does what this person holds cover what was denied.

    Three existing pieces and no fourth rule. `intersect` decides what narrower means,
    `scope_for` decides what holding it means (and refuses an expired recipient, which is
    where a leaver stops being told about their old department), and `Scope.matches` decides
    whether the grant admits the place - the same predicate `brain.audit.view._may_see`
    evaluates against a ledger row.
    """
    shared = requirement(pattern).intersect(recipient)
    scope = shared.scope_for(pattern.capability, now)
    if scope is None or not scope.matches(dict(pattern.where)):
        return None
    return scope


def digest(
    *,
    now: datetime,
    patterns: Sequence[DenialPattern],
    recipients: Sequence[EntitlementSet],
    log: AlertLog,
    window: timedelta = DIGEST_WINDOW,
) -> Digest:
    """Route every pattern to everybody entitled to hear it, at most once per window.

    `recipients` are entitlement sets rather than a wrapper carrying an id beside one: the
    set already knows whose it is, and a second field is a second place for the two to
    disagree about which person's reach is being evaluated.

    The order of the guards is deliberate. The subject exclusion runs before the entitlement
    check, so that a subject who would have passed it never reaches a code path that could
    build them an alert. The window check runs last, so a recipient who is not entitled
    never appears in the log at all - a debounce row for somebody who may not be told would
    be a record that the pattern exists, keyed by their name.

    An assessment whose shape has no sentence produces nothing, rather than raising. A shape
    nobody has written words for is a shape nobody has decided about, and the honest answer
    to that is silence - the same reading `brain.audit.view._may_see` takes for a subject
    kind with no capability governing it. A test pins that every alerting shape has one, so
    the silence is a guard rather than a gap.
    """
    emitted: dict[AlertKey, DenialAlert] = {}
    for pattern in patterns:
        if not pattern.assessment.is_worth_alerting:
            continue
        text = ALERT_TEXT.get(pattern.shape)
        if text is None:
            continue
        for recipient in recipients:
            if recipient.principal_id == pattern.subject_id:
                # The first of the two refusals in `THE_SUBJECT_IS_NEVER_A_RECIPIENT`.
                continue
            if reach(pattern, recipient, now=now) is None:
                continue
            key = (recipient.principal_id, pattern.subject_id, pattern.shape)
            if key in emitted:
                continue
            last = log.last_sent(key)
            # A `now` behind the log is a replay rather than a fresh pass, and silence is
            # the conservative answer to one: an alert re-raised from history says a run is
            # happening now when it is not.
            if last is not None and now - last < window:
                continue
            emitted[key] = DenialAlert(
                recipient_id=recipient.principal_id,
                subject_id=pattern.subject_id,
                shape=pattern.shape,
                raised_at=now,
                text=text,
            )

    alerts = tuple(sorted(emitted.values(), key=lambda a: (a.recipient_id, a.subject_id, a.shape)))
    return Digest(alerts=alerts, log=log.record(emitted.keys(), now))
