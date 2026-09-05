"""How one source tells us a projected record moved, and what it cannot tell us at all.

`brain.connectors.manifest.ChangeSignal` names the three mechanisms and an explicit absence.
That is the vocabulary. This is the declaration: for one source and one entity kind, which
mechanism is in use, how fast it is expected to arrive, how often a full pass runs anyway,
and how a deletion is ever learned about. **The three are not interchangeable and the choice
is per source, so nothing here picks one.** What it does is refuse the combinations that
look like a subscription and are not.

**A webhook is push, so it is fast and it is unreliable, and the unreliability is silent.**
A delivery that fails leaves nothing behind: no gap in a sequence, no cursor that failed to
advance, no error anybody owns. A projection refreshed only by pushes therefore drifts, and
the drift has no symptom until somebody quotes a figure that stopped being true a month ago.
So `reconcile_every` is a required field on every subscription, with no default and a hard
cap, and the floor exists for CDC and cursors too: a stream that stopped and a poll that
stopped are also things nobody notices from inside the mechanism that stopped.

**An updated-since cursor cannot see a deletion, ever.** A record removed at the source is
not "updated": it is simply one the cursor never mentions again, so it stays in the
projection for good, is filtered and counted on, and reads as current. This system has met
that exact failure before, on Lark Base, where the bot holds `base:record:read` and nothing
wider, so a deletion is detectable only as an *absence*: re-enumerate the ids the source
still returns and treat everything in the projection that is missing from that enumeration
as gone. That is `DeletionCheck.ID_SWEEP`, and a cursor-based source is refused unless it
declares that or a real deleted-since feed. See `A_CURSOR_CANNOT_SEE_A_DELETION`.

**CDC is exact and is the one most clients will not grant.** A log-based stream carries the
DELETE as a row and carries a sequence, so both of the failures above are visible from
inside it. It also needs replication access to somebody else's production database, which
for six of the seven first connectors is not on offer at any price. It is declared here and
argued for per source rather than recommended, because a mechanism that cannot be obtained
is not a better default.

**A source that promises nothing stays stale at every age, and this does not restate that.**
`brain.connectors.projection.assess_staleness` already answers STALE for `promise=None`, and
`RefreshPromise` already refuses to be built from `ChangeSignal.NONE`. So `ChangeSubscription`
refuses `NONE` as well and `promise_for(None)` returns None: there is one spelling of "no
promise" and this module produces the same one rather than a second.

**Which of a source's two intervals is the freshness interval, which is the decision at the
centre of this.** `assess_staleness` compares now against `last_seen_at`, and `last_seen_at`
moves when we *see* a record, not when it changes. A webhook fires only on change, so a
record that nobody edited is never re-seen and its age grows without bound. Handing
`RefreshPromise` the push latency would therefore mark every quiet row STALE within minutes,
which is not what `brain.connectors.projection` wants from the sentence "a webhook is
seconds": it is the reading that makes the whole projection permanently stale. The interval
that actually governs when every row is re-seen is the reconciliation pass, so `promise()`
hands over `reconcile_every`. `notify_within` stays a declared fact about the source, is what
the reconciliation floor is checked against, and is the number an operator compares an
observed delay with.

Scope: domain logic. Nothing here subscribes to anything, opens a connection or reads a
clock. `now` is a parameter, for the reason `brain.models.routing.CircuitBreaker` gives.

Task ids: M11.4.6
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from brain.connectors.manifest import NO_SIGNAL_MEANS_NO_PROJECTION, ChangeSignal, ManifestError
from brain.connectors.projection import RefreshPromise
from brain.core.envelope import OBJECT_NAME_PATTERN

# ------------------------------------------------------------------ written-down reasons
#: Why a push mechanism does not remove the need for a periodic full pass.
A_WEBHOOK_MISS_IS_SILENT = (
    "A webhook that was never delivered leaves no trace on our side: no gap in a sequence, "
    "no cursor that failed to advance, no queue that grew, no error with an owner. The "
    "source believes it told us and we believe nothing happened, and those two beliefs are "
    "consistent for ever. Every other mechanism in this system reports its own failure "
    "somehow; this one cannot, which is why the floor is a required field rather than a "
    "recommendation, and why it applies to a source whose webhook is working perfectly."
)

#: What an updated-since source has to do about records that were removed.
A_CURSOR_CANNOT_SEE_A_DELETION = (
    "An updated-since cursor asks 'what changed after this timestamp', and a deleted record "
    "does not change: it stops being mentioned. So a cursor-driven projection keeps every "
    "record the source ever had, filters and counts on them, and reports them as current "
    "indefinitely. Nothing about that is visible from the cursor. The only remedy available "
    "to a read-only integration is absence: enumerate the ids the source still returns and "
    "treat everything in the projection that is missing from the enumeration as gone. That "
    "is how deletions in Lark Base are detected here, where the credential holds "
    "base:record:read and nothing wider, and it is why a cursor source must declare either "
    "an ID_SWEEP or a real deleted-since feed before it may be subscribed to at all."
)

#: Why CDC is not the recommendation even though it is the only exact one.
CDC_IS_EXACT_AND_RARELY_GRANTED = (
    "Change data capture reads the source's own write log, so a DELETE is a row like any "
    "other and a gap in the stream is visible as a gap in a sequence. Both of the failures "
    "the other two mechanisms have are simply absent. What it needs is replication access to "
    "somebody else's production database, which is not on offer for a SaaS source at any "
    "price and is a serious ask even for a system the client runs. It is declared per source "
    "and argued for per source, because a mechanism nobody will grant is not a better default."
)

#: Why the freshness interval is the reconciliation interval rather than the push latency.
FRESHNESS_IS_MEASURED_AGAINST_RECONCILIATION = (
    "Staleness is computed from last_seen_at, which moves when a record is seen rather than "
    "when it changes. A push mechanism fires only on change, so a record nobody edited is "
    "never re-seen and its age grows without bound; measured against a push latency of "
    "seconds it would read STALE within a minute of being perfectly correct. The interval "
    "that actually governs when every row is re-seen is the periodic full pass, so that is "
    "the interval handed to RefreshPromise. The push latency is still declared, because it "
    "is what the floor is checked against and what an operator compares a real delay with."
)


# ----------------------------------------------------------------- classifying a mechanism
class Delivery(enum.StrEnum):
    """Who initiates. The one distinction that decides everything else about a mechanism.

    A pushed notification is fast and its loss is silent; a pulled one is as fast as the poll
    and its failure is visible, because the thing that pulls either ran or did not. Both
    facts follow from this field, which is why it is here rather than being inferred at each
    call site from the signal's name.
    """

    PUSH = "push"
    PULL = "pull"
    #: Neither. `ChangeSignal.NONE` is an absence written down, not a third mechanism.
    NONE = "none"


@dataclass(frozen=True)
class SignalKindFacts:
    """What one kind of change signal can and cannot tell us, independent of any source.

    Per kind rather than per source, deliberately: these are properties of the mechanism.
    Nothing a Xero webhook is configured to do makes a lost delivery detectable, and nothing
    a HubSpot cursor is configured to do lets it mention a record that no longer exists. The
    per-source declaration is `ChangeSubscription`, and it is checked against this table.
    """

    kind: ChangeSignal
    delivery: Delivery
    #: Whether a record removed at the source can be learned from this signal alone.
    sees_deletions: bool
    #: Whether a lost notification leaves anything behind on our side to notice it by.
    misses_are_silent: bool
    #: Whether using it needs access a client has to grant beyond an ordinary API key.
    needs_privileged_access: bool
    #: One sentence for a review or a console row. Never the whole argument, which is in the
    #: reason constants above; enough that a reviewer knows which one to go and read.
    note: str


#: Every member of `ChangeSignal`, and the mapping is total on purpose.
#:
#: A `dict.get` with a default would let a fifth mechanism be added and silently classified as
#: whatever the default said, which for a question like "can this see deletions" is the answer
#: that loses records quietly. A test asserts the table covers the enum, so adding a member
#: fails the build in front of whoever added it. `MappingProxyType` for the reason
#: `brain.ops.limits` uses it on its own registries: a module-level dict is a table any
#: importer can edit, and a table edited at run time is a classification nobody reviewed.
SIGNAL_FACTS: Final[MappingProxyType[ChangeSignal, SignalKindFacts]] = MappingProxyType(
    {
        ChangeSignal.WEBHOOK: SignalKindFacts(
            kind=ChangeSignal.WEBHOOK,
            delivery=Delivery.PUSH,
            # A delete is an event a webhook source can send, and most do. That is not the
            # same as a deletion being reliably learned about: a delete delivery is lost as
            # silently as any other, which is the floor's job rather than this flag's.
            sees_deletions=True,
            misses_are_silent=True,
            needs_privileged_access=False,
            note=(
                "immediate, and a lost delivery leaves no trace anywhere; the periodic full "
                "pass is what makes a projection built on one trustworthy"
            ),
        ),
        ChangeSignal.CDC: SignalKindFacts(
            kind=ChangeSignal.CDC,
            delivery=Delivery.PUSH,
            sees_deletions=True,
            # A log-based stream is sequenced, so a gap is a gap in numbers rather than an
            # absence of events, and the reader can tell it missed something.
            misses_are_silent=False,
            needs_privileged_access=True,
            note=(
                "exact, including deletions and writes made behind the source's own API, and "
                "needs replication access most clients will not grant"
            ),
        ),
        ChangeSignal.UPDATED_SINCE: SignalKindFacts(
            kind=ChangeSignal.UPDATED_SINCE,
            delivery=Delivery.PULL,
            sees_deletions=False,
            # A poll that did not run is a cursor that did not advance, which is visible from
            # the cursor itself the next time anybody looks.
            misses_are_silent=False,
            needs_privileged_access=False,
            note=(
                "sees every update and no deletion; a removed record is one the cursor never "
                "mentions again, so absence has to be checked for separately"
            ),
        ),
        ChangeSignal.NONE: SignalKindFacts(
            kind=ChangeSignal.NONE,
            delivery=Delivery.NONE,
            sees_deletions=False,
            misses_are_silent=True,
            needs_privileged_access=False,
            note=(
                "the source has undertaken nothing, so a projected field would be quoted as "
                "current for ever; project nothing and fetch live"
            ),
        ),
    }
)


def facts_for(kind: ChangeSignal) -> SignalKindFacts:
    """The classification of one mechanism.

    A function rather than a bare subscript so the totality of `SIGNAL_FACTS` is asserted in
    one place, and so a caller reads `facts_for(kind).sees_deletions` rather than reaching
    into a module-level mapping and inventing its own fallback when a lookup misses.
    """
    try:
        return SIGNAL_FACTS[kind]
    except KeyError as exc:  # pragma: no cover  the totality test is what keeps this unreached
        msg = (
            f"{kind!r} has no entry in SIGNAL_FACTS, so nothing knows whether it can see a "
            "deletion; classify it before any source may declare it"
        )
        raise ManifestError(msg) from exc


# ---------------------------------------------------------------- how deletions are learned
class DeletionCheck(enum.StrEnum):
    """How a source's removals ever reach the projection.

    Three, and they are ordered by how much the source does for us. `SIGNALLED` is only
    available to a mechanism that can carry a deletion at all; the other two are what is left
    for one that cannot, and a subscription has to pick one of them explicitly. There is
    deliberately no member meaning "not handled": a projection that never learns about
    deletions is a projection that answers counts wrongly for ever, and leaving the question
    answerable by omission is how it ends up unanswered.
    """

    #: The source tells us: a delete event, or a DELETE in the write log.
    SIGNALLED = "signalled"
    #: The source offers a deleted-since endpoint listing what was removed.
    DELETED_FEED = "deleted_feed"
    #: We enumerate the ids the source still returns; anything in the projection that is
    #: missing from that enumeration is gone. The only option a read-only credential has.
    ID_SWEEP = "id_sweep"


# --------------------------------------------------------------------- the floor
#: The longest a projection may go without a full reconciliation pass, whatever mechanism it
#: subscribes to. A day, matching `brain.gate.provenance.DEFAULT_HORIZON.stale_after` and
#: `brain.gate.leash.MAX_APPROVAL_WINDOW`, and the shared number is deliberate: a day is the
#: unit in which somebody notices that an answer was wrong yesterday too.
#:
#: A cap and not a default. `RefreshPromise` refuses a module-level interval applied on a
#: caller's behalf, and this module makes the same refusal for the same reason: the right
#: interval is a property of one connector's deployment, and a default supplied here would be
#: an inference presented as a declaration. What is enforced is that no declaration may be
#: looser than this.
MAX_RECONCILE_INTERVAL: Final = timedelta(hours=24)

_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)


@dataclass(frozen=True)
class ChangeSubscription:
    """One source's declaration of how it tells us one entity kind moved (M11.4.6).

    Every field is required. That is the enforcement rather than a convention: a defaulted
    `reconcile_every` is a webhook-only projection that nobody had to argue for, and a
    defaulted `deletion_check` is a cursor that quietly never notices a removal. Both are
    exactly the declarations this exists to refuse, and both look complete at a glance.

    Frozen, like every declaration in this package, and for the reason
    `ConnectorManifest` gives about its own: a subscription that could be edited after review
    is a subscription whose reviewed value and live value can differ inside one process.
    """

    source: str
    entity: str
    kind: ChangeSignal
    #: How quickly a change is expected to reach us once the source knows about it. Declared,
    #: not measured: it is what a real observed delay is compared against.
    notify_within: timedelta
    #: How often every record is re-read regardless of notifications. The floor.
    reconcile_every: timedelta
    deletion_check: DeletionCheck

    def __post_init__(self) -> None:
        self._assert_named()
        self._assert_the_source_promised_something()
        self._assert_the_floor_holds()
        self._assert_deletions_are_learnable()

    def _assert_named(self) -> None:
        if not _NAME_RE.match(self.source):
            msg = (
                f"subscription source {self.source!r} is not a connector name; the manifest, "
                "the ceiling and the projected rows are all looked up by this string"
            )
            raise ManifestError(msg)
        if not _NAME_RE.match(self.entity):
            msg = (
                f"subscription entity {self.entity!r} is not a name; a subscription that "
                "matches no entity kind refreshes nothing and reports that it is working"
            )
            raise ManifestError(msg)

    def _assert_the_source_promised_something(self) -> None:
        """`ChangeSignal.NONE` is not a mechanism, so it cannot be subscribed to.

        Refused rather than accepted and treated as a subscription that never fires, because
        the second is indistinguishable from a working one in every console row. The spelling
        of "this source promises nothing" is the absence of a subscription, which
        `promise_for` turns into the `promise=None` that `assess_staleness` already answers
        STALE for at every age. One spelling, as `RefreshPromise` insists.
        """
        if not self.kind.is_a_signal:
            msg = (
                f"{self.source}.{self.entity} declares {self.kind}, which is not a mechanism "
                "to subscribe to. A source that promises nothing has no subscription at all "
                f"and its rows are stale at every age. {NO_SIGNAL_MEANS_NO_PROJECTION}"
            )
            raise ManifestError(msg)

    def _assert_the_floor_holds(self) -> None:
        """The two intervals, and the relationship between them that has to hold.

        The cap is the floor itself. The comparison between them is what stops the floor
        being declared so tight that it *is* the mechanism: a full pass at least as often as
        the signal is expected to arrive means the reconciliation is doing all the work, the
        declared kind is decoration, and the source is being read far harder than the
        connector's ceiling was sized for.
        """
        if self.notify_within <= timedelta():
            msg = (
                f"{self.source}.{self.entity} declares a notification delay of "
                f"{self.notify_within}, which is not an interval; a zero one makes the floor "
                "check below meaningless"
            )
            raise ManifestError(msg)
        if self.reconcile_every <= timedelta():
            msg = (
                f"{self.source}.{self.entity} declares a reconciliation interval of "
                f"{self.reconcile_every}, which is not an interval"
            )
            raise ManifestError(msg)
        if self.reconcile_every > MAX_RECONCILE_INTERVAL:
            msg = (
                f"{self.source}.{self.entity} would reconcile every {self.reconcile_every}, "
                f"past the {MAX_RECONCILE_INTERVAL} floor every subscription carries. "
                f"{A_WEBHOOK_MISS_IS_SILENT}"
            )
            raise ManifestError(msg)
        if self.reconcile_every <= self.notify_within:
            msg = (
                f"{self.source}.{self.entity} would reconcile every {self.reconcile_every} "
                f"while expecting {self.kind} within {self.notify_within}; a full pass at "
                "least as often as the signal means the full pass is the signal, the declared "
                "mechanism is decoration, and the source is read far harder than its ceiling "
                "was sized for"
            )
            raise ManifestError(msg)

    def _assert_deletions_are_learnable(self) -> None:
        """A mechanism that cannot carry a deletion may not claim the source will send one."""
        if self.deletion_check is DeletionCheck.SIGNALLED and not self.facts.sees_deletions:
            msg = (
                f"{self.source}.{self.entity} subscribes by {self.kind} and declares that "
                f"deletions are {DeletionCheck.SIGNALLED}, which that mechanism cannot do. "
                f"Declare {DeletionCheck.ID_SWEEP} or {DeletionCheck.DELETED_FEED}. "
                f"{A_CURSOR_CANNOT_SEE_A_DELETION}"
            )
            raise ManifestError(msg)

    # ------------------------------------------------------------------ what it means
    @property
    def facts(self) -> SignalKindFacts:
        """The mechanism's classification. Looked up, never restated on the declaration."""
        return facts_for(self.kind)

    @property
    def sees_deletions_by_itself(self) -> bool:
        """Whether the mechanism alone would notice a removal. False for a cursor, always."""
        return self.facts.sees_deletions

    @property
    def needs_an_absence_check(self) -> bool:
        """Whether a removal is learned about only by enumerating what is still there.

        True for exactly the case in `A_CURSOR_CANNOT_SEE_A_DELETION`: the mechanism cannot
        carry a deletion and no deleted-since feed exists, so the reconciliation pass has to
        compare the projection against the ids the source still returns.
        """
        return self.deletion_check is DeletionCheck.ID_SWEEP

    def promise(self) -> RefreshPromise:
        """The undertaking this subscription amounts to, in `projection`'s own vocabulary.

        The interval is `reconcile_every` and not `notify_within`. See
        `FRESHNESS_IS_MEASURED_AGAINST_RECONCILIATION`: `last_seen_at` moves when a record is
        seen, and a push mechanism does not re-send a record nobody edited, so the full pass
        is the only thing that refreshes a quiet row.
        """
        return RefreshPromise(signal=self.kind, interval=self.reconcile_every)

    def next_reconciliation(self, last_reconciled_at: datetime) -> datetime:
        return last_reconciled_at + self.reconcile_every

    def reconciliation_due(self, *, now: datetime, last_reconciled_at: datetime) -> bool:
        """Whether the full pass is owed, which is the only question the floor answers.

        Takes the last pass rather than reading a clock or a table, matching every other
        decision in this package: a scheduler that owned its own clock could not be tested
        for the boundary, which is the case that decides whether a projection is refreshed
        at all.
        """
        if now.tzinfo is None or last_reconciled_at.tzinfo is None:
            msg = (
                "reconciliation times must be timezone-aware; Singapore reads a naive UTC "
                "timestamp as eight hours old, which is a third of the daily floor"
            )
            raise ManifestError(msg)
        return now >= self.next_reconciliation(last_reconciled_at)


def promise_for(subscription: ChangeSubscription | None) -> RefreshPromise | None:
    """The promise to hand `assess_staleness`, including for a source that made none.

    The seam this module exists to provide, and it is one line on purpose. `None` in means
    `None` out, which `assess_staleness` already answers STALE at every age, so a source with
    no subscription is treated exactly as `brain.connectors.projection` already treats one
    and there is no second answer to disagree with the first.
    """
    return None if subscription is None else subscription.promise()
