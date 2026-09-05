"""Change-signal subscriptions: every test is a way a projection drifts without saying so.

Two failures dominate and neither is visible from inside the mechanism that has it. A
webhook delivery that never arrives leaves nothing behind, and an updated-since cursor never
mentions a record that was deleted. The tests below are mostly about those two.

Nothing here subscribes to anything. The module is a declaration and a classification, and
`brain.connectors.projection` is where the age it produces is turned into a sentence.

Task ids: M11.4.6
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.connectors.change_signal import (
    MAX_RECONCILE_INTERVAL,
    SIGNAL_FACTS,
    ChangeSubscription,
    DeletionCheck,
    Delivery,
    facts_for,
    promise_for,
)
from brain.connectors.manifest import ChangeSignal, ManifestError
from brain.connectors.projection import ProjectedRecord, assess_staleness
from brain.gate.provenance import Freshness

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
PUSH_LATENCY = timedelta(minutes=1)
FULL_PASS = timedelta(hours=6)


def a_subscription(
    *,
    kind: ChangeSignal = ChangeSignal.WEBHOOK,
    notify_within: timedelta = PUSH_LATENCY,
    reconcile_every: timedelta = FULL_PASS,
    deletion_check: DeletionCheck = DeletionCheck.SIGNALLED,
) -> ChangeSubscription:
    return ChangeSubscription(
        source="xero",
        entity="invoice",
        kind=kind,
        notify_within=notify_within,
        reconcile_every=reconcile_every,
        deletion_check=deletion_check,
    )


def a_record(*, age: timedelta) -> ProjectedRecord:
    return ProjectedRecord(
        source="xero",
        entity="invoice",
        source_id="INV-1041",
        last_seen_at=NOW - age,
    )


# ------------------------------------------------------------------- the classification
def test_every_change_signal_kind_is_classified() -> None:
    """A mechanism with no entry has no answer to "can this see a deletion", and a lookup
    with a default would answer whatever the default said. Deleting this lets a fifth kind
    be added to the enum and classified by accident, which for that question is the answer
    that loses records quietly."""
    assert set(SIGNAL_FACTS) == set(ChangeSignal)
    for kind in ChangeSignal:
        assert facts_for(kind).kind is kind


@pytest.mark.parametrize(
    ("kind", "sees_deletions"),
    [
        (ChangeSignal.WEBHOOK, True),
        (ChangeSignal.CDC, True),
        (ChangeSignal.UPDATED_SINCE, False),
        (ChangeSignal.NONE, False),
    ],
)
def test_each_kind_declares_whether_it_can_see_a_deletion(
    kind: ChangeSignal, sees_deletions: bool
) -> None:
    """The table is the whole classification, and the cursor row is the one that matters: a
    record removed at the source is never "updated", so it is one the cursor never mentions
    again. Deleting this lets the cursor row be flipped to True, after which a cursor source
    passes review with no deletion handling and keeps every record the source ever had."""
    assert facts_for(kind).sees_deletions is sees_deletions


def test_an_updated_since_cursor_cannot_see_a_deletion() -> None:
    """Stated on its own as well as in the table above, because it is the claim the module
    exists to enforce and a parametrised row is easy to edit without reading. Deleting this
    removes the only place the rule is asserted about the specific mechanism it constrains."""
    assert facts_for(ChangeSignal.UPDATED_SINCE).sees_deletions is False
    assert facts_for(ChangeSignal.UPDATED_SINCE).delivery is Delivery.PULL


def test_a_lost_webhook_is_silent_and_a_lost_poll_is_not() -> None:
    """This is why the floor exists at all. A poll that did not run leaves a cursor that did
    not advance; a webhook that was not delivered leaves nothing. Deleting this lets the two
    be classified the same, and the reconciliation floor then looks like belt and braces
    rather than the only thing that would ever notice."""
    assert facts_for(ChangeSignal.WEBHOOK).misses_are_silent is True
    assert facts_for(ChangeSignal.UPDATED_SINCE).misses_are_silent is False
    assert facts_for(ChangeSignal.CDC).misses_are_silent is False


def test_cdc_is_the_one_mechanism_that_needs_access_most_clients_will_not_grant() -> None:
    """CDC is exact and that is not a reason to prefer it: it needs replication access to
    somebody else's production database. Deleting this lets the flag be cleared, after which
    a review reads CDC as an ordinary option and a connector is designed around an access
    grant that never arrives."""
    assert facts_for(ChangeSignal.CDC).needs_privileged_access is True
    assert not any(
        facts_for(k).needs_privileged_access
        for k in (ChangeSignal.WEBHOOK, ChangeSignal.UPDATED_SINCE)
    )


# ------------------------------------------------------------------ the reconciliation floor
def test_a_subscription_cannot_be_declared_without_a_reconciliation_interval() -> None:
    """The floor is enforced by the field being required, so a webhook-only projection is
    not a thing anybody can express. Deleting this lets `reconcile_every` acquire a default,
    and a default is a periodic full pass nobody argued for and nobody scheduled."""
    with pytest.raises(TypeError, match="reconcile_every"):
        ChangeSubscription(  # type: ignore[call-arg]
            source="xero",
            entity="invoice",
            kind=ChangeSignal.WEBHOOK,
            notify_within=PUSH_LATENCY,
            deletion_check=DeletionCheck.SIGNALLED,
        )
    floor = {f.name: f for f in dataclasses.fields(ChangeSubscription)}["reconcile_every"]
    assert floor.default is dataclasses.MISSING
    assert floor.default_factory is dataclasses.MISSING


def test_a_webhook_source_still_carries_a_reconciliation_floor() -> None:
    """A push mechanism is the case where somebody argues the floor is unnecessary, and it
    is the case where it is load-bearing: a missed delivery is silent for ever. Deleting
    this lets the cap be raised or dropped for the one kind that most needs it."""
    with pytest.raises(ManifestError, match="floor every subscription carries"):
        a_subscription(reconcile_every=MAX_RECONCILE_INTERVAL + timedelta(seconds=1))

    within = a_subscription(reconcile_every=MAX_RECONCILE_INTERVAL)
    assert within.reconcile_every == MAX_RECONCILE_INTERVAL
    assert within.facts.misses_are_silent is True


def test_reconciling_at_least_as_often_as_the_signal_arrives_is_refused() -> None:
    """A full pass as frequent as the notification means the pass is the mechanism, the
    declared kind is decoration, and the source is read far harder than its ceiling was
    sized for. Deleting this admits a declaration that reads as a subscription and is really
    a poll of everything, which is what the connector's rate limit was not sized for."""
    with pytest.raises(ManifestError, match="the full pass is the signal"):
        a_subscription(notify_within=timedelta(hours=6), reconcile_every=timedelta(hours=6))


def test_an_interval_of_zero_is_refused_on_either_side() -> None:
    """A zero notification delay makes the comparison above meaningless and a zero
    reconciliation interval is a full pass in a loop. Deleting this leaves both expressible,
    and both look like a tightened configuration rather than a mistake."""
    with pytest.raises(ManifestError, match="not an interval"):
        a_subscription(notify_within=timedelta())
    with pytest.raises(ManifestError, match="not an interval"):
        a_subscription(reconcile_every=timedelta())


def test_reconciliation_is_due_once_the_declared_interval_has_passed() -> None:
    """The boundary is the case a rewrite gets wrong, and getting it wrong by an instant
    means a pass that is due is never scheduled. Deleting this leaves the only decision the
    floor actually drives untested."""
    subscription = a_subscription()
    last = NOW - FULL_PASS
    assert subscription.reconciliation_due(now=NOW, last_reconciled_at=last)
    assert not subscription.reconciliation_due(
        now=NOW - timedelta(microseconds=1), last_reconciled_at=last
    )
    assert subscription.next_reconciliation(last) == NOW


def test_a_naive_time_is_refused_when_asking_whether_reconciliation_is_due() -> None:
    """Singapore reads a naive UTC timestamp as eight hours old, which is a third of the
    daily cap. Deleting this lets a naive `last_reconciled_at` make a pass look due eight
    hours early, or a naive `now` make one look eight hours late."""
    subscription = a_subscription()
    with pytest.raises(ManifestError, match="timezone-aware"):
        subscription.reconciliation_due(
            now=datetime(2026, 9, 6, 9, 0),  # naive, which is the point
            last_reconciled_at=NOW - FULL_PASS,
        )


# --------------------------------------------------------------------------- deletions
def test_a_cursor_source_may_not_claim_the_source_signals_its_deletions() -> None:
    """The mechanism cannot carry a deletion, so a declaration that it does is a projection
    that keeps every record the source ever had while reporting them as current. Deleting
    this lets a cursor connector pass review with `SIGNALLED`, which is the single easiest
    box to tick and the one nothing downstream can check."""
    with pytest.raises(ManifestError, match="cannot do"):
        a_subscription(
            kind=ChangeSignal.UPDATED_SINCE,
            deletion_check=DeletionCheck.SIGNALLED,
        )


def test_a_cursor_source_learns_about_deletions_by_enumerating_what_is_still_there() -> None:
    """The positive sibling, and the remedy the Lark Base credential actually has: it holds
    a read scope and nothing wider, so absence from a full enumeration is the only evidence
    a record has gone. Deleting this leaves the refusal above satisfied by refusing every
    cursor source, which would rule out three of the first connectors."""
    subscription = a_subscription(
        kind=ChangeSignal.UPDATED_SINCE,
        deletion_check=DeletionCheck.ID_SWEEP,
    )
    assert subscription.needs_an_absence_check is True
    assert subscription.sees_deletions_by_itself is False

    feed = a_subscription(
        kind=ChangeSignal.UPDATED_SINCE,
        deletion_check=DeletionCheck.DELETED_FEED,
    )
    assert feed.needs_an_absence_check is False


def test_a_webhook_source_may_declare_that_the_source_sends_its_deletions() -> None:
    """A push mechanism can carry a delete event and most do, so refusing `SIGNALLED` for
    every kind would be a guard satisfied by refusing everything. Deleting this hides that,
    and the floor is what covers the delete event that goes missing anyway."""
    subscription = a_subscription(deletion_check=DeletionCheck.SIGNALLED)
    assert subscription.sees_deletions_by_itself is True
    assert subscription.needs_an_absence_check is False


# ------------------------------------------------------------- what it promises downstream
def test_a_source_with_no_change_signal_cannot_be_subscribed_to() -> None:
    """`ChangeSignal.NONE` is an absence written down, and a subscription built from it would
    be indistinguishable in a console row from one that works. Deleting this creates a second
    spelling of "this source promises nothing", and the two would eventually disagree."""
    with pytest.raises(ManifestError, match="not a mechanism to subscribe to"):
        a_subscription(kind=ChangeSignal.NONE)


def test_a_source_with_no_subscription_is_stale_at_every_age() -> None:
    """The rule `brain.connectors.projection` already holds, reached through this module's
    own seam so the two cannot drift. Deleting this lets `promise_for` invent a promise for
    a source that made none, after which a row nothing will ever refresh reads as current
    for the first interval of its life."""
    assert promise_for(None) is None
    reading = assess_staleness(a_record(age=timedelta(seconds=1)), now=NOW, promise=None)
    assert reading.freshness is Freshness.STALE


def test_freshness_is_measured_against_the_full_pass_not_against_the_push_latency() -> None:
    """A webhook fires on change, so a record nobody edited is never re-seen and its age
    grows without bound. Measured against a one-minute push latency, a correct row reads
    STALE ten minutes later; measured against the six-hour full pass it reads LIVE, which is
    the truth. Deleting this lets `promise` hand over `notify_within`, and every quiet row in
    the projection then carries a staleness notice that means nothing."""
    subscription = a_subscription(notify_within=PUSH_LATENCY, reconcile_every=FULL_PASS)
    assert subscription.promise().interval == FULL_PASS

    quiet = assess_staleness(
        a_record(age=timedelta(minutes=10)), now=NOW, promise=subscription.promise()
    )
    assert quiet.freshness is Freshness.LIVE

    abandoned = assess_staleness(
        a_record(age=FULL_PASS * 4), now=NOW, promise=subscription.promise()
    )
    assert abandoned.freshness is Freshness.STALE


def test_a_subscription_names_a_connector_and_an_entity_kind() -> None:
    """A subscription that matches no entity refreshes nothing and reports that it is
    working, which is the failure shape of every part of this pipeline. Deleting this lets a
    typo in either name produce a scheduled pass over records that do not exist."""
    with pytest.raises(ManifestError, match="not a connector name"):
        ChangeSubscription(
            source="Not A Source",
            entity="invoice",
            kind=ChangeSignal.WEBHOOK,
            notify_within=PUSH_LATENCY,
            reconcile_every=FULL_PASS,
            deletion_check=DeletionCheck.SIGNALLED,
        )
    with pytest.raises(ManifestError, match="is not a name"):
        ChangeSubscription(
            source="xero",
            entity="Not An Entity",
            kind=ChangeSignal.WEBHOOK,
            notify_within=PUSH_LATENCY,
            reconcile_every=FULL_PASS,
            deletion_check=DeletionCheck.SIGNALLED,
        )
