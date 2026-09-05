"""Routing a denial pattern to somebody, without the alert becoming the thing it reports.

`brain.ops.denial_alerts` is the one artefact in this system whose whole purpose is to say
that hidden things were asked for. Every other module keeps DENIED and ABSENT apart by
staying quiet; this one has to speak and stay inside the same rule, so most of what is here
is about what an alert does *not* carry.

Four properties, and each has a test that fails on its own if the guard goes:

- the subject of a run is never told about their own run, in the routing and in the shape
- somebody without the entitlement is told nothing, rather than something with holes in it
- the sentence carries no capability, no object, no value and no number of any kind
- a run of denials produces one alert in a window, not one alert per denial

The structural guards at the end are the ones that survive a refactor: this module has
nowhere to express a refusal, no public callable returning a bare bool, and no import that
could reach the request path.

Task ids: M23.2.2
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.adapter import (
    ChannelCapabilities,
    DeliveryRefusedError,
    assert_can_send,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.core.scope import Clause, Op, Scope
from brain.gate.context import Channel
from brain.ops import denial_alerts as module
from brain.ops.denial_alerts import (
    A_DIGEST_IS_LOSSLESS_BECAUSE_THERE_IS_NO_COUNT,
    ALERT_CLASSIFICATION,
    ALERT_REQUIREMENT,
    ALERT_TEXT,
    AN_ALERT_IS_NOT_EXEMPT_FROM_THE_ENTITLEMENT_MODEL,
    DIGEST_WINDOW,
    THE_ALERT_NAMES_A_SHAPE_AND_NEVER_A_THING,
    THE_SUBJECT_IS_NEVER_A_RECIPIENT,
    THIS_MODULE_HAS_NOWHERE_TO_REFUSE,
    AlertLog,
    DenialAlert,
    DenialPattern,
    digest,
    reach,
    requirement,
)
from brain.ops.limits import DenialShape, assess_denials
from tests.fixtures.company import CANARIES, NOW, person

CONTRACT_VALUE = Capability(value="read:client.contract_value")

MAINTENANCE = {"department": "maintenance"}


def _ents(
    principal_id: str,
    *capabilities: str,
    scope: Scope | None = None,
    not_after: datetime | None = None,
) -> EntitlementSet:
    """One recipient. Grants written out here rather than through a defaulting helper, for
    the reason the company fixture gives: a helper with defaults hides the thing under test."""
    where = Scope.unrestricted() if scope is None else scope
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(Grant(capability=Capability(value=c), scope=where) for c in capabilities),
        not_after=not_after,
    )


def _pattern(
    subject_id: str = "u_weiling",
    *,
    capability: Capability = CONTRACT_VALUE,
    denials: int = 20,
    distinct_targets: int = 18,
    where: dict[str, str] | None = None,
) -> DenialPattern:
    return DenialPattern(
        subject_id=subject_id,
        capability=capability,
        assessment=assess_denials(denials=denials, distinct_targets=distinct_targets),
        where=dict(MAINTENANCE) if where is None else where,
    )


# ============================================================ who may receive one


def test_a_recipient_without_the_entitlement_is_told_nothing_rather_than_a_redacted_alert() -> None:
    """The central rule, applied to the alert itself. An alert saying somebody was denied is
    a statement that something exists which they could not see, so a recipient who does not
    hold what was denied must get no alert at all - not a shorter one, not one with the
    detail stripped, not a placeholder saying an alert was withheld. Any of those is the
    hidden-item count this system never emits, arrived at by counting placeholders.

    Deleting this test lets the entitlement check be removed and every operator on the list
    starts learning which fields other people cannot reach."""
    jason = person("u_jason").entitlement()  # invoke:agent only; reaches no client field
    result = digest(
        now=NOW,
        patterns=(_pattern(),),
        recipients=(jason,),
        log=AlertLog(),
    )
    assert result.alerts == ()


def test_the_debounce_log_holds_no_row_for_somebody_who_may_not_be_told() -> None:
    """A debounce row is keyed by recipient, subject and shape, so a row written for an
    unentitled recipient is a record that the pattern exists, filed under the name of
    somebody who may not know it does. The window check therefore runs after the entitlement
    check and never before it.

    Deleting this test lets the two guards be reordered, which changes nothing an operator
    can see and quietly puts the fact into a store."""
    result = digest(
        now=NOW,
        patterns=(_pattern(),),
        recipients=(person("u_jason").entitlement(),),
        log=AlertLog(),
    )
    assert dict(result.log.sent) == {}


def test_a_grant_in_another_department_does_not_reach_a_pattern_in_this_one() -> None:
    """Siti holds `read:client.*` across the whole of Web and none of Maintenance. The
    intersection of her scope with the pattern's is a conjunction that admits no row, which
    is what `Scope.matches` is asked and what stops the alert.

    Deleting this test lets the scope half of the check be dropped while the capability half
    still passes, so every department admin hears about every other department."""
    siti = person("u_siti").entitlement()
    assert reach(_pattern(), siti, now=NOW) is None


def test_the_department_admin_who_can_grant_access_is_the_one_told() -> None:
    """The other half, and the reason the module exists at all. Aaron holds `read:client.*`
    in Maintenance, so a run of denials there is something he could already have found out
    and is something he can act on.

    Deleting this test lets the entitlement check tighten to nobody, which reads in
    production as a quiet system rather than as a broken one."""
    aaron = person("u_aaron").entitlement()
    result = digest(now=NOW, patterns=(_pattern(),), recipients=(aaron,), log=AlertLog())
    assert [a.recipient_id for a in result.alerts] == ["u_aaron"]


def test_a_wildcard_grant_is_enough_to_be_told_about_a_field_inside_it() -> None:
    """`EntitlementSet.intersect` keeps a grant only when the ceiling covers it, and
    `Capability.covers` expands only a trailing `.*`. So intersecting the recipient by the
    denied capability would drop the wildcard grant of somebody who plainly holds the field,
    and the whole alert list would collapse to holders of the exact capability string.

    Deleting this test lets the intersection be written the intuitive way round, which
    silences the alert for every wildcard holder - which is to say for every admin."""
    wildcard = _ents("u_wide", "read:client.*", scope=Scope.department("maintenance"))
    assert reach(_pattern(), wildcard, now=NOW) is not None


def test_a_recipient_whose_access_has_expired_is_told_nothing() -> None:
    """Expiry is checked where it is always checked, by passing `now` into `scope_for`. A
    leaver whose grants are still on file must stop being told about the department they
    left on the day their access ends, not on the day somebody tidies the recipient list.

    Deleting this test lets `now` be dropped from the entitlement check, at which point the
    only thing standing between a former contractor and a stream of alerts is whoever
    remembers to remove them."""
    far_future = datetime(2100, 1, 1, tzinfo=UTC)
    leaver = _ents(
        "u_leaver",
        "read:client.contract_value",
        scope=Scope.department("maintenance"),
        not_after=datetime(2099, 1, 1, tzinfo=UTC),
    )
    assert reach(_pattern(), leaver, now=far_future) is None
    result = digest(now=far_future, patterns=(_pattern(),), recipients=(leaver,), log=AlertLog())
    assert result.alerts == ()


def test_a_pattern_with_no_place_reaches_only_a_company_wide_grant() -> None:
    """An empty routing row fails closed rather than open. `Clause.matches` refuses an
    absent field, so a departmental grant admits nothing, and the only reader left is
    somebody whose grant was written as company-wide in the first place.

    Deleting this test lets an empty row be treated as "matches everything", which turns a
    pattern nobody attached a place to into a broadcast."""
    nowhere = _pattern(where={})
    assert reach(nowhere, person("u_aaron").entitlement(), now=NOW) is None
    assert reach(nowhere, person("u_rupash").entitlement(), now=NOW) is not None


def test_the_requirement_names_no_person() -> None:
    """`requirement` builds an entitlement set to intersect against, and an entitlement set
    carries a principal id. Borrowing a real one would make traces and logs show that person
    holding a grant they do not have.

    Deleting this test lets the subject's own id be used as the convenient thing to hand,
    which is the version that reads correctly and is a fabricated grant."""
    required = requirement(_pattern("u_weiling"))
    assert required.principal_id == ALERT_REQUIREMENT
    assert "u_weiling" not in required.principal_id


# ============================================================ the subject is never told


def test_the_subject_of_a_pattern_never_receives_the_alert_about_themselves() -> None:
    """The probe oracle, and the reason the entitlement check is not enough on its own.

    Daniel holds `read:client.contract_value` in Sales, so a run of denials attributed to him
    in Sales passes the entitlement check outright: the routing row is department-level and
    the predicate that actually denied him is narrower than that. Told about it, he can find
    the boundary of what exists by moving and watching whether the message arrives.

    Deleting this test lets the exclusion be dropped from the routing loop with no visible
    change to anybody else's alerts, and hands the person under observation a measuring
    instrument for the thing being observed."""
    daniel = person("u_dual").entitlement()
    sales = _pattern("u_dual", where={"department": "sales"})
    assert reach(sales, daniel, now=NOW) is not None, "the entitlement check alone admits him"

    result = digest(now=NOW, patterns=(sales,), recipients=(daniel,), log=AlertLog())
    assert result.alerts == ()
    assert dict(result.log.sent) == {}


def test_an_alert_cannot_be_addressed_to_the_person_it_is_about() -> None:
    """The second refusal, in the shape rather than in the routing. The loop above is the one
    a refactor drops; this is the one a hand-built alert somewhere else in the system goes
    around. Two guards for one rule, because the rule has two ways of being lost.

    Deleting this test lets the constructor check be removed, and the only thing left
    protecting the invariant is one `continue` in one loop."""
    with pytest.raises(ValueError, match="probe oracle"):
        DenialAlert(
            recipient_id="u_weiling",
            subject_id="u_weiling",
            shape=DenialShape.ENUMERATION,
            raised_at=NOW,
            text=ALERT_TEXT[DenialShape.ENUMERATION],
        )


def test_the_subject_is_excluded_before_anything_asks_whether_they_are_entitled() -> None:
    """Order, not just presence. A subject who passes the entitlement check must never reach
    a code path that could build them an alert, so the exclusion is the first thing in the
    loop rather than a filter applied to the finished list.

    Asserted over the source rather than over the output, because swapping the two changes
    no result today: it is invisible until somebody adds a side effect - a metric, a log
    line, a debounce row - between them, and by then the ordering is nobody's decision.

    Deleting this test lets them be reordered during a tidy-up with every behavioural test
    still green."""
    routing = inspect.getsource(module.digest)
    assert routing.index("== pattern.subject_id") < routing.index("reach(pattern")


# ============================================================ what the sentence may say


def test_no_alert_text_names_a_capability_an_object_or_a_canary_value() -> None:
    """The canary, inverted the way `test_no_canary_value_survives_into_an_entry` is: this
    fails if the data arrives. A pattern about `read:client.contract_value` is routed, and
    the sentence that comes out must contain no part of that - not the capability, not the
    entity, not the field, not the department it happened in, and none of the fixture's
    canary values, which cannot be confused with something a sentence happened to say.

    Naming the capability is the whole leak: "denied on client.contract_value" tells the
    reader the field exists and is populated, which is exactly what the DENIED-as-ABSENT
    rule spends the rest of the system not saying.

    Deleting this test lets the capability be interpolated into the sentence to make the
    alert more useful, which it would be, and the alert becomes a report about hidden
    things."""
    result = digest(
        now=NOW,
        patterns=(_pattern(),),
        recipients=(person("u_aaron").entitlement(),),
        log=AlertLog(),
    )
    assert len(result.alerts) == 1
    text = result.alerts[0].text
    for token in (*CANARIES.values(), "contract_value", "client", "read:", "maintenance"):
        assert token not in text, f"the alert names {token!r}"


def test_no_alert_carries_a_count_of_anything() -> None:
    """A count of denials is a count of hidden things arrived at by subtraction, and a count
    of distinct targets is one arrived at directly. Neither may leave this module, so the
    alert has no numeric field and its sentence has no digit in it: "denied 40 times across
    12 targets" is a lower bound on what exists, and it moves, which makes it a dial.

    Asserted over the whole shape rather than over one string, because the natural way to
    add a count back is a field rather than a word.

    Deleting this test lets the numbers return the first time somebody says the alert is not
    actionable enough, and they are right that it is not - and the audit view is where that
    is answered."""
    result = digest(
        now=NOW,
        patterns=(_pattern(denials=40, distinct_targets=12),),
        recipients=(person("u_aaron").entitlement(),),
        log=AlertLog(),
    )
    alert = result.alerts[0]
    assert not any(character.isdigit() for character in alert.text)
    numeric = {
        f.name for f in dataclasses.fields(DenialAlert) if f.type in {"int", "float", "int | None"}
    }
    assert not numeric, f"DenialAlert carries {numeric}, which is a count of hidden things"


def test_the_alert_says_who_it_is_about_or_nobody_can_act_on_it() -> None:
    """The one identifier that is deliberately kept. An alert whose purpose is "this person
    is missing a grant" cannot omit the person; the recipient already holds the capability in
    that place, so being told a colleague was denied there discloses nothing they could not
    have found out, which is precisely what the entitlement check establishes first.

    Deleting this test lets the subject be pseudonymised for tidiness, and the ACCESS_NEEDED
    shape stops routing to anybody who can act on it."""
    result = digest(
        now=NOW,
        patterns=(_pattern("u_weiling", denials=20, distinct_targets=1),),
        recipients=(person("u_aaron").entitlement(),),
        log=AlertLog(),
    )
    assert result.alerts[0].subject_id == "u_weiling"
    assert result.alerts[0].shape is DenialShape.ACCESS_NEEDED


def test_a_run_below_the_noticing_threshold_says_nothing_to_anybody() -> None:
    """People mistype client names. `assess_denials` already decided that a handful of
    denials is ORDINARY; this is the half that makes the decision have an effect.

    Deleting this test lets every ordinary run become an alert, and the alerting channel is
    unusable by the end of the first week - at which point somebody mutes it and the
    enumeration alert goes with it."""
    result = digest(
        now=NOW,
        patterns=(_pattern(denials=3, distinct_targets=3),),
        recipients=(person("u_aaron").entitlement(),),
        log=AlertLog(),
    )
    assert result.alerts == ()


def test_an_ordinary_run_stays_silent_even_when_a_sentence_exists_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two things stand between an ordinary run and an alert: the classifier's own
    `is_worth_alerting`, and the absence of any words for `DenialShape.ORDINARY`. They have
    the same effect today, so a behavioural test of one is a test of the other, and whichever
    is deleted first goes unnoticed. This removes the coincidence and asserts the
    classifier's answer on its own.

    Found by mutation: dropping `is_worth_alerting` altogether left the suite green, because
    the missing sentence caught it. The day somebody writes words for the ordinary shape -
    for a console listing, say - that second guard stops holding and every mistyped client
    name becomes a page.

    Deleting this test lets `is_worth_alerting` be removed as redundant, which it is until
    it is the only thing left."""
    monkeypatch.setattr(
        module,
        "ALERT_TEXT",
        {**ALERT_TEXT, DenialShape.ORDINARY: "Somebody mistyped a name a few times."},
    )
    result = digest(
        now=NOW,
        patterns=(_pattern(denials=3, distinct_targets=3),),
        recipients=(person("u_aaron").entitlement(),),
        log=AlertLog(),
    )
    assert result.alerts == ()


def test_every_alerting_shape_has_a_sentence() -> None:
    """`digest` skips a shape with no words rather than raising, which is right for a shape
    nobody has decided about and wrong for one somebody forgot. This is what makes the
    silence a guard rather than a gap.

    Deleting this test lets a new `DenialShape` be added and route to nobody, silently, with
    every other test still green."""
    alerting = {s for s in DenialShape if s is not DenialShape.ORDINARY}
    assert alerting == set(ALERT_TEXT), "a shape that alerts has no sentence to say"
    for shape, text in ALERT_TEXT.items():
        assert len(text) > 80, shape


def test_an_alert_with_no_sentence_in_it_is_refused() -> None:
    """A notification carrying only a shape enum is one an operator has to look up, and an
    empty one is a page in the middle of the night that says nothing at all.

    Deleting this test lets an alert be constructed with an empty body, which reads as
    delivered everywhere it is counted."""
    with pytest.raises(ValueError, match="explains nothing"):
        DenialAlert(
            recipient_id="u_aaron",
            subject_id="u_weiling",
            shape=DenialShape.ENUMERATION,
            raised_at=NOW,
            text="",
        )


def test_an_alert_is_too_sensitive_for_a_channel_that_has_not_said_otherwise() -> None:
    """The other half of "who may receive it": the surface, not just the person. An alert
    naming a colleague and saying their access is being looked at should not arrive on a
    consumer messaging app on somebody's personal phone. `ChannelCapabilities` defaults to
    INTERNAL, so a surface has to have declared itself fit for this before it may carry one.

    Deleting this test lets `ALERT_CLASSIFICATION` drop to INTERNAL to make delivery work,
    and the alert goes wherever the answer goes."""
    assert ALERT_CLASSIFICATION.rank > Classification.INTERNAL.rank
    with pytest.raises(DeliveryRefusedError, match="may carry at most"):
        assert_can_send(
            ChannelCapabilities(channel=Channel.WHATSAPP),
            ChannelPayload(),
            highest=ALERT_CLASSIFICATION,
        )


# ============================================================ the digest window


def test_repeated_denials_inside_the_window_produce_one_alert_and_not_many() -> None:
    """The alert stream must not be the denial log. Three runs against the same subject
    inside one window are one alert, and the second pass produces none at all - otherwise
    anybody who can see alerts reads the subject's denial activity as it happens, which is
    the reconstruction the whole module is arranged to prevent.

    Deleting this test lets the debounce be removed, and the two failures arrive together:
    an operator woken four hundred times, and a log they were never meant to be able to
    rebuild."""
    aaron = person("u_aaron").entitlement()
    three_runs = (_pattern(), _pattern(), _pattern())
    first = digest(now=NOW, patterns=three_runs, recipients=(aaron,), log=AlertLog())
    assert len(first.alerts) == 1

    later = digest(
        now=NOW + timedelta(minutes=59),
        patterns=three_runs,
        recipients=(aaron,),
        log=first.log,
    )
    assert later.alerts == ()


def test_the_alert_stream_resumes_once_the_window_has_passed() -> None:
    """A debounce that never expires is a mute. Enumeration that continues past the window
    has to be said again, or the one alert anybody ever gets about a subject is the first.

    Deleting this test lets the window be widened without limit to quieten the channel, and
    the alert becomes a thing that happened once."""
    aaron = person("u_aaron").entitlement()
    first = digest(now=NOW, patterns=(_pattern(),), recipients=(aaron,), log=AlertLog())
    again = digest(
        now=NOW + DIGEST_WINDOW,
        patterns=(_pattern(),),
        recipients=(aaron,),
        log=first.log,
    )
    assert len(again.alerts) == 1


def test_the_window_is_long_enough_that_the_alert_stream_is_not_a_denial_log() -> None:
    """The window is a privacy parameter before it is an ergonomic one. At a minute it is a
    minute-by-minute readout of the subject's denial activity for anybody who receives
    alerts; the digest only stops being a log because the window is coarse.

    Deleting this test lets the window be shortened to make alerting feel responsive, which
    is the change that reads as an improvement and undoes the mechanism."""
    assert timedelta(minutes=30) <= DIGEST_WINDOW


def test_a_change_of_shape_is_a_different_alert_to_a_different_person() -> None:
    """The debounce is keyed by shape as well as by subject, because ACCESS_NEEDED and
    ENUMERATION go to different people and one suppressing the other means one of them never
    arrives. Bounded at two per window, which is the cost of that.

    Deleting this test lets the key collapse to the subject, and a run that escalates from
    persistence to breadth is silently swallowed by the alert about persistence."""
    aaron = person("u_aaron").entitlement()
    result = digest(
        now=NOW,
        patterns=(
            _pattern(denials=20, distinct_targets=1),
            _pattern(denials=20, distinct_targets=18),
        ),
        recipients=(aaron,),
        log=AlertLog(),
    )
    assert {a.shape for a in result.alerts} == {
        DenialShape.ACCESS_NEEDED,
        DenialShape.ENUMERATION,
    }


def test_a_pass_run_against_a_clock_that_has_gone_backwards_says_nothing() -> None:
    """A `now` behind the log is a replay rather than a fresh pass, and an alert re-raised
    from history says a run is happening when it is not.

    Deleting this test lets the window comparison be written as an absolute difference,
    which turns every replayed pass into a fresh page."""
    aaron = person("u_aaron").entitlement()
    first = digest(now=NOW, patterns=(_pattern(),), recipients=(aaron,), log=AlertLog())
    replay = digest(
        now=NOW - timedelta(hours=6),
        patterns=(_pattern(),),
        recipients=(aaron,),
        log=first.log,
    )
    assert replay.alerts == ()


def test_alerts_come_out_in_a_stable_order() -> None:
    """Two recipients, and the same two alerts every time. An unordered result makes every
    downstream diff, snapshot and delivery log noisy for no reason, and the noise is what
    stops anybody noticing a real change in it.

    Handed to `digest` in the wrong order on purpose, so the sort is the only thing that can
    produce the expected list.

    Deleting this test lets the order follow dictionary insertion, which follows the
    recipient list, which follows whatever query built it."""
    recipients = (
        _ents("u_zoe", "read:client.*", scope=Scope.department("maintenance")),
        person("u_aaron").entitlement(),
    )
    result = digest(now=NOW, patterns=(_pattern(),), recipients=recipients, log=AlertLog())
    assert [a.recipient_id for a in result.alerts] == ["u_aaron", "u_zoe"]


# ============================================================ the shape of the input


def test_a_pattern_must_be_about_somebody() -> None:
    """An unattributed run routes to nobody and reads in the console as a quiet week.
    Refusing it here puts the failure where the mistake was made.

    Deleting this test lets a blank subject through, where the subject-exclusion check then
    silently matches every recipient with a blank principal id and nobody else."""
    with pytest.raises(ValueError, match="routes to nobody"):
        DenialPattern(
            subject_id="",
            capability=CONTRACT_VALUE,
            assessment=assess_denials(denials=20, distinct_targets=18),
        )


def test_a_routing_attribute_that_is_not_a_scope_field_is_refused() -> None:
    """The routing row is turned into scope clauses, and `Clause.field` has a grammar. A
    name that breaks it fails inside a pydantic validator two calls away, where the message
    is about a clause nobody wrote.

    Deleting this test lets a malformed row surface as an unrelated validation error during
    an incident."""
    with pytest.raises(ValueError, match="not a scope field name"):
        DenialPattern(
            subject_id="u_weiling",
            capability=CONTRACT_VALUE,
            assessment=assess_denials(denials=20, distinct_targets=18),
            where={"Department": "maintenance"},
        )


def test_a_blank_routing_value_admits_nobody_and_is_refused() -> None:
    """An empty value is an EQ clause matching nothing, so the pattern routes to nobody and
    reads in the console as "nobody is entitled" when what happened is a blank in a row.

    Deleting this test lets a missing department produce a permanently silent pattern that
    looks identical to a correctly routed one with no eligible readers."""
    with pytest.raises(ValueError, match="admit nobody"):
        DenialPattern(
            subject_id="u_weiling",
            capability=CONTRACT_VALUE,
            assessment=assess_denials(denials=20, distinct_targets=18),
            where={"department": ""},
        )


def test_the_requirement_is_built_from_the_routing_row_and_not_carried_beside_it() -> None:
    """One source for the place, so a scope and a row cannot disagree about where the
    denials happened - and the disagreement would be silent in whichever direction the
    caller wrote.

    Deleting this test lets a second scope field be added for convenience, and the check
    starts evaluating one place while the alert is about another."""
    required = requirement(_pattern(where={"department": "web", "partner_visible": "true"}))
    assert required.grants[0].scope == Scope(
        clauses=(
            Clause(field="department", op=Op.EQ, value="web"),
            Clause(field="partner_visible", op=Op.EQ, value="true"),
        )
    )


# ============================================================ structural guards


#: The vocabulary of a refusal. Matched against names rather than prose, because a name is
#: what a caller reads and acts on. Same list as the capacity invariants use, plus the
#: words a notification layer reaches for when it starts deciding things.
FORBIDDEN = frozenset(
    {
        "allowed",
        "banned",
        "block",
        "blocked",
        "deny",
        "denied",
        "mute",
        "muted",
        "permitted",
        "quarantine",
        "refuse",
        "refused",
        "reject",
        "rejected",
        "suspend",
        "suspended",
        "throttle",
        "throttled",
    }
)


def _public_callables() -> list[tuple[str, object]]:
    """Every function, method and property getter this module puts on its public surface."""
    found: list[tuple[str, object]] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            found.append((name, obj))
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            for attribute, member in vars(obj).items():
                if attribute.startswith("_"):
                    continue
                if inspect.isfunction(member):
                    found.append((f"{name}.{attribute}", member))
                elif isinstance(member, property) and member.fget is not None:
                    found.append((f"{name}.{attribute}", member.fget))
    return found


def test_this_module_has_nowhere_to_express_a_refusal() -> None:
    """M23.2.2, enforced structurally rather than by review, exactly as
    `ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE` is for the detector this reads from. A denial
    pattern is a heuristic, and a heuristic that refuses teaches legitimate people to work
    around it while anybody adapting deliberately walks through faster. If there is no value
    meaning "block", a future caller cannot start blocking without adding one and being seen
    in a diff; a boolean nobody currently reads would not be.

    Deleting this test lets a `blocked` flag appear on an alert, be set by whoever delivers
    it, and become a permission decision taken by a notifier."""
    for kind in (DenialPattern, DenialAlert, AlertLog, module.Digest):
        names = {f.name.lower() for f in dataclasses.fields(kind)}
        names |= {n.lower() for n in dir(kind) if not n.startswith("_")}
        assert not (names & FORBIDDEN), f"{kind.__name__} has somewhere to refuse"


def test_no_public_callable_here_returns_a_bare_bool() -> None:
    """A function returning a bool is a verdict with no name: the caller writes `if not
    x(...)` and has invented a refusal this module never agreed to. `reach` returns the scope
    somebody reaches instead, which answers the same question and says why.

    Deleting this test lets `reach` be simplified to a predicate, at which point the module's
    public surface has a value meaning no."""
    for name, fn in _public_callables():
        annotation = str(inspect.signature(fn).return_annotation)
        assert "bool" not in annotation, f"{name} returns {annotation}"


def test_nothing_here_can_reach_the_request_path() -> None:
    """The detector may score and alert; it may not participate in a decision about whether
    somebody's question runs. So the limiter's decision types are not imported, the error
    taxonomy is not imported, and no channel is - importing `ChannelAdapter` would put
    `DeliveryRefusedError` on this module's surface by inheritance, which is the one thing it
    must not have.

    Deleting this test lets an import creep in, and the first caller to catch a `BrainError`
    from here has wired abuse scoring into the answer path."""
    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    assert not any(m.startswith(("brain.channels", "brain.gate")) for m in modules), modules
    assert "brain.core.errors" not in modules
    assert not (
        names
        & {
            "BrainError",
            "CapacityRefused",
            "Denied",
            "LimitDecision",
            "LimiterState",
            "Outcome",
            "QuotaExceeded",
            "check",
        }
    )


def test_the_only_thing_this_module_raises_is_a_programming_error() -> None:
    """Every `raise` here is a `ValueError` about a malformed input, in the same register as
    `assess_denials` refusing more distinct targets than denials. Nothing raises an outcome,
    because an outcome is something a person is told and this module never speaks to the
    person it is about.

    Deleting this test lets a `Denied` be raised from the routing loop, which is a refusal
    dressed as a validation error."""
    tree = ast.parse(inspect.getsource(module))
    raised = {
        node.exc.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert raised == {"ValueError"}, raised


def test_nothing_here_reads_a_clock() -> None:
    """`now` is a parameter, as it is everywhere else in the ops layer. A debouncer that read
    the clock could not be tested at the window boundary, which is the only part of a
    debouncer that is ever wrong.

    Deleting this test lets `datetime.now()` appear in the window comparison, and every test
    of the window becomes a test of how long the suite took to run."""
    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint({"now", "utcnow", "today", "time", "monotonic", "sleep"})


def test_every_written_down_reason_is_still_written_down() -> None:
    """These constants are the arguments for the four decisions the module is made of, and a
    mechanism whose argument has been deleted is one somebody removes next quarter because
    nothing explains why it is there. Importing them is most of the guard; the length check
    is what stops one being emptied to a stub to make a lint pass.

    Deleting this test lets the reasons be trimmed one by one until the module looks like a
    for-loop over a counter, which is what it would have been."""
    reasons = (
        AN_ALERT_IS_NOT_EXEMPT_FROM_THE_ENTITLEMENT_MODEL,
        THE_ALERT_NAMES_A_SHAPE_AND_NEVER_A_THING,
        THE_SUBJECT_IS_NEVER_A_RECIPIENT,
        A_DIGEST_IS_LOSSLESS_BECAUSE_THERE_IS_NO_COUNT,
        THIS_MODULE_HAS_NOWHERE_TO_REFUSE,
    )
    for reason in reasons:
        assert len(reason) > 120, reason
    assert "probe oracle" in THE_SUBJECT_IS_NEVER_A_RECIPIENT
    assert "privacy parameter" in A_DIGEST_IS_LOSSLESS_BECAUSE_THERE_IS_NO_COUNT
