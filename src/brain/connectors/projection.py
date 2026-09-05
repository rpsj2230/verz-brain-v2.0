"""Building a projected record, and saying how old it is when somebody quotes it.

`brain.core.projection` states what may be copied and how much. `brain.connectors.manifest`
refuses a declaration that breaks those rules, at review, in front of whoever wrote the
connector. This is the third and last point: the value that is actually constructed from a
source's rows, on the way to `proj.record`.

**The cap is enforced here rather than checked afterwards, and the difference is not
pedantry.** A check that runs after a projection exists is a check that runs after the
thirteenth field was fetched, held in memory, logged in a trace and written by whichever
path did not call the checker. `ProjectedRecord` cannot be constructed over the cap at all,
so there is no window in which an oversized projection exists as a value. The arithmetic is
not restated: `check_projection` counts, and this raises what it returns. This is the
ingest-time half of M11.4.2, whose review-time half `manifest.projectability` already owns,
so the leaf is not claimed twice.

**A nested object is not one field.** See `A_NESTED_OBJECT_IS_NOT_ONE_FIELD`: a value that
is a mapping or a list is refused outright rather than counted as its leaves. This is the
clause a cap is defeated through, and it is defeated politely: nobody adds a thirteenth
column, somebody adds `contact: {...}` and the count stays at twelve.

**Staleness is a pure function of an age and a promise, and the promise is the source's.**
A row is LIVE while the source has not yet broken its undertaking to tell us about changes,
AGEING while one or two of those notifications have been missed, and STALE once enough have
been missed that the age has stopped being evidence about the record and started being
evidence about the pipeline. See `MISSED_REFRESHES_BEFORE_STALE`.

**The classification is `brain.gate.provenance`'s, not a second one.** That module already
holds four freshness states, the horizon type, the rule about a read time in the future and
the rule about a naive timestamp, all with the boundary cases argued. A second freshness
scale here would be a second answer to "may this number be quoted", and the day the two
disagree the generous one wins, exactly as `throttle.ONE_BUCKET_AND_ONE_BREAKER` says about
the bucket and the breaker. The cost is one line that formats a `datetime` and hands it
straight back to a parser, which looks silly and is cheaper than a rule maintained twice.

**What happens at STALE is the interesting decision, and it is neither of the two easy
ones.** A stale row is served, always: there is no value anywhere in this module that means
"withhold", for the same reason `brain.ops.limits.ABUSE_DETECTION_HAS_NOWHERE_TO_REFUSE`
gives about its own assessments. And it is never served silently: every state but LIVE
produces a notice, and `ProjectedReading.speak` hands the fields and the sentence back
together so a composer has to go out of its way to drop one.

Note the limit of that honestly. Nothing in a pure value can stop a caller reading
`.record.fields` and ignoring the notice; what a value can do is make the notice impossible
not to have, and make a refusal impossible to express. `brain.connectors.transports` draws
the same distinction about its sandbox profile: naming one means somebody chose it, and it
does not mean a boundary has been enforced.

**Whether the notice may name the source is decision 24 in `docs/needs-rupash.md`, and this
does not pre-empt it.** `notice` takes the set of sources the asker's own catalogue already
disclosed, names only those, and folds everything else into one constant sentence, which is
the reading `federation.PartialAnswer.notice` already built and the recommendation in that
decision. If the answer comes back the other way, the call site passes every source name and
nothing in this module changes. If it comes back as recorded, the call site passes the
caller's catalogue. The seam is the argument, so both answers are one call site apart.

Scope: domain logic. Nothing here opens a connection, reads a clock or touches a table. `now`
is a parameter, for the reason `brain.models.routing.CircuitBreaker` gives.

Task ids: M11.4.9
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from brain.connectors.manifest import ChangeSignal
from brain.core.envelope import OBJECT_NAME_PATTERN
from brain.core.projection import (
    MAX_PROJECTED_FIELDS,
    ProjectionRefusedError,
    ProjectionViolation,
    check_projection,
)
from brain.gate.provenance import (
    FRESHNESS_TEXT,
    Freshness,
    StalenessHorizon,
    assess_freshness,
)

# ------------------------------------------------------------------ written-down reasons
#: Why a mapping or a list is refused instead of counted.
A_NESTED_OBJECT_IS_NOT_ONE_FIELD = (
    "A twelve-field cap counted over top-level keys is defeated by one field called "
    "`contact` holding six of them, and nobody does that deliberately: they do it because "
    "the source returns a nested object and copying it whole is less work than choosing. "
    "Counting the leaves instead was rejected for two reasons. It admits unlimited nesting "
    "so long as the arithmetic happens to work, and it makes the cap a property of a value "
    "rather than of a declaration, so the same declared field is inside the cap for one "
    "record and outside it for the next. A cap that a reviewer cannot evaluate from the "
    "manifest is a cap nobody reviews. Every one of the five pointer shapes in the tier "
    "table is a scalar: an id, a join key, a status, a timestamp, one short label. A "
    "container is not one of them, so it is refused and the author names the leaves they "
    "actually filter on, or fetches it live."
)

#: Why the count happens in a constructor rather than in a validator called afterwards.
THE_CAP_IS_ENFORCED_WHERE_THE_PROJECTION_IS_BUILT = (
    "A projection checked after construction has already existed: the thirteenth field was "
    "fetched, held, and passed to whatever did not call the checker. Refusing in "
    "__post_init__ means there is no moment at which an oversized projection is a value in "
    "this process, so a later writer cannot find one to store. The database carries the same "
    "rule a second time, for the row that arrived through a hand-written statement."
)

#: Why a stale row is served with its age rather than refused or quietly used.
A_STALE_ROW_IS_SERVED_WITH_ITS_AGE = (
    "Refusing on staleness turns a source's sync falling behind into an outage for questions "
    "the projection could still answer usefully, and the person asking is given nothing they "
    "can act on. Serving it silently is worse: a figure read a fortnight ago is quoted as "
    "though somebody had just looked, nobody files a bug because the answer was true when it "
    "was fetched, and the failure is invisible in exactly the questions this system exists "
    "for. So the row is returned and the age travels with it. There is deliberately no value "
    "in this module meaning 'withhold', which is the same shape brain.ops.limits gives its "
    "abuse assessments: a future caller cannot start refusing without adding somewhere to "
    "express it and being seen in review."
)


# --------------------------------------------------------------- what a projected value is
#: The scalar types a pointer can be. Every one of the tier table's five shapes is one of
#: these; nothing here can hold a body. `bool` is redundant beside `int` at runtime and is
#: named anyway, because the set is read by a person deciding what is allowed.
ProjectedValue = str | int | float | bool | datetime | None

_SCALAR_TYPES: Final = (str, int, float, bool, datetime)

_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, _SCALAR_TYPES)


# ---------------------------------------------------------- the record itself (M11.4.1)
@dataclass(frozen=True)
class ProjectedRecord:
    """One record's worth of projection: which record, what we kept, and when we last saw it.

    Frozen, like every declaration in this package. A projection that could be mutated after
    construction would be a projection whose thirteenth field was added after the count.

    `local_id` is the entity registry's identifier and defaults to empty, because a record
    that has been fetched and not yet resolved is the ordinary state during a backfill. It is
    an empty string rather than None so that the type has one absent value instead of two;
    `brain.tables.projection` stores null, which is the database's spelling of the same fact.
    """

    source: str
    entity: str
    source_id: str
    last_seen_at: datetime
    fields: Mapping[str, ProjectedValue] = field(default_factory=dict)
    local_id: str = ""

    def __post_init__(self) -> None:
        self._assert_identified()
        self._assert_within_the_tier()

    def _assert_identified(self) -> None:
        """The three parts of the key, and a timestamp that can be compared with a clock.

        The same predicates `proj.record` carries as check constraints, deliberately: the
        constructor catches it on the way in and the column catches the row that arrived
        another way, which is the split `auth.principal` already uses for its own.
        """
        if not _NAME_RE.match(self.source):
            msg = (
                f"projected source {self.source!r} is not a connector name; the manifest, the "
                "ceiling and the row are all looked up by this string"
            )
            raise ProjectionRefusedError(msg)
        if not _NAME_RE.match(self.entity):
            msg = (
                f"projected entity {self.entity!r} is not a name; the field policy is looked "
                "up by it, and a name nothing matches is withheld from everybody"
            )
            raise ProjectionRefusedError(msg)
        if not self.source_id.strip():
            msg = (
                f"a projected {self.entity} from {self.source} carries no source id; a record "
                "that cannot be named cannot be refreshed, cited or matched to itself later"
            )
            raise ProjectionRefusedError(msg)
        if self.last_seen_at.tzinfo is None:
            # The argument `brain.gate.provenance.read_time` makes about its own input.
            # Singapore reads a naive UTC timestamp as eight hours old, which is the
            # difference between LIVE and AGEING for every answer in the building.
            msg = (
                f"a projected {self.entity} from {self.source} has a naive last_seen_at; a "
                "timestamp with no zone is read here as eight hours older than it is, which "
                "is the whole width of the ageing band"
            )
            raise ProjectionRefusedError(msg)

    def _assert_within_the_tier(self) -> None:
        """Every reason this projection may not be stored, reported together.

        All of them rather than the first, for the reason `check_projection` gives: one at a
        time turns writing a connector into a guessing game where each fix reveals the next
        objection. The container refusals are computed here and the rest are
        `check_projection`'s, so the denylist, the label length and the cap have exactly one
        implementation and this adds the one clause a mapping of scalars cannot express.
        """
        violations: list[ProjectionViolation] = [
            ProjectionViolation(
                field=name,
                reason=(
                    f"is a {type(value).__name__} rather than a pointer; a nested object is "
                    f"not one field. Name the leaves the fast lane filters on, each counted "
                    f"against the {MAX_PROJECTED_FIELDS}, or fetch it live"
                ),
            )
            for name, value in self.fields.items()
            if not _is_scalar(value)
        ]
        violations.extend(check_projection(self.entity, dict(self.fields)))
        if not violations:
            return
        listed = "\n".join(f"  - {v}" for v in violations)
        msg = f"{self.source}.{self.entity} {self.source_id} cannot be projected:\n{listed}"
        raise ProjectionRefusedError(msg)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.fields))

    @property
    def is_resolved(self) -> bool:
        """Whether entity resolution has given this record a local id to join on."""
        return bool(self.local_id.strip())


# ------------------------------------------------------------------- staleness (M11.4.9)
#: How many of the source's own refresh intervals may pass before a row is stale.
#:
#: Three, and the number is a judgement rather than a measurement, so it is worth saying what
#: it is judging. One missed notification is a delivery that will be retried. Two is a
#: coincidence, which happens. Three consecutive misses is the change signal not working, and
#: at that point the row's age has stopped being evidence about the record and become
#: evidence about the pipeline. `brain.ops.limits.BACKOFF_AFTER_REFUSALS` is the same number
#: reached the same way about a different subject, and matching it is deliberate: a person
#: reading both should not have to work out whether the difference means something.
MISSED_REFRESHES_BEFORE_STALE: Final = 3

#: What the asker is told when the source may not be named. One sentence for every source and
#: every age, so that two sources in different states are indistinguishable to somebody
#: probing, which is the property `federation.NAMING_A_SOURCE_IS_A_DISCLOSURE` protects and
#: the reading decision 24 recommends keeping.
UNNAMED_STALENESS_NOTICE = "Part of this answer comes from information that may not be current."


@dataclass(frozen=True)
class RefreshPromise:
    """What the source has undertaken to tell us, and how quickly it arrives.

    A value passed in rather than a constant read here, matching `StalenessHorizon`: the
    interval is a property of one connector's deployment (a webhook is seconds, a nightly
    updated-since cursor is a day) and the console owns the numbers. A module-level default
    applied on a caller's behalf would be the inference `brain.gate.provenance` exists to
    refuse, wearing a helpful face.

    `ChangeSignal.NONE` cannot be made into a promise, and that is the point of refusing it
    here: a source that tells us nothing has promised nothing, so there is no interval to
    classify against. `manifest.NO_SIGNAL_MEANS_NO_PROJECTION` already refuses the projection
    at review; this refuses the second, quieter version, where somebody builds a horizon for
    a signal-less source and the rows then read as fresh.
    """

    signal: ChangeSignal
    interval: timedelta

    def __post_init__(self) -> None:
        if not self.signal.is_a_signal:
            msg = (
                "a source with no change signal has promised nothing, so there is no refresh "
                "interval to measure an age against. Such a source projects no fields at all: "
                "see manifest.NO_SIGNAL_MEANS_NO_PROJECTION"
            )
            raise ProjectionRefusedError(msg)
        if self.interval <= timedelta():
            msg = (
                f"a refresh interval of {self.interval} is not an interval; a zero one makes "
                "every row stale on arrival and hides a real sync failure behind noise"
            )
            raise ProjectionRefusedError(msg)

    @property
    def horizon(self) -> StalenessHorizon:
        """The promise as the two thresholds `brain.gate.provenance` classifies against.

        `live_for` is one interval, because inside it the source has not yet broken its
        undertaking and the row is as current as it was ever going to be. `stale_after` is
        `MISSED_REFRESHES_BEFORE_STALE` of them, which is the ageing band: long enough that a
        retried webhook or a cursor run that started late does not read as a failure, short
        enough that a signal which has actually stopped is reported the same day.
        """
        return StalenessHorizon(
            live_for=self.interval,
            stale_after=self.interval * MISSED_REFRESHES_BEFORE_STALE,
        )


@dataclass(frozen=True)
class ProjectedReading:
    """A projected record, how old it is, and the sentence that has to travel with it.

    There is no field here meaning "do not serve this", and adding one is the regression.
    See `A_STALE_ROW_IS_SERVED_WITH_ITS_AGE`, which is the same shape and the same argument
    as `brain.ops.limits.VolumeAssessment`.
    """

    record: ProjectedRecord
    freshness: Freshness
    age: timedelta

    @property
    def is_current(self) -> bool:
        """Whether this may be quoted with nothing said about its age."""
        return self.freshness is Freshness.LIVE

    def notice(self, *, disclosable: frozenset[str]) -> str:
        """What the asker is told about the age. Empty only when there is nothing to say.

        A LIVE row produces no sentence, deliberately. A reassurance that the figure is
        current, attached to every answer, is a claim offered where nobody asked for one, and
        it trains a reader to skip the line that matters when it eventually says something
        else. `federation.PartialAnswer.notice` makes the same choice about a complete fetch.

        The source is named only when the asker's own catalogue already disclosed it.
        Everything else becomes `UNNAMED_STALENESS_NOTICE`, one sentence for every source, so
        that "the Xero projection is three days behind" is not a fact obtainable by anybody
        who can type a question. See the module docstring on decision 24: this is the seam,
        and it does not decide the question.
        """
        if self.is_current:
            return ""
        if self.record.source not in disclosable:
            return UNNAMED_STALENESS_NOTICE
        return (
            f"This uses {self.record.source} data that is "
            f"{FRESHNESS_TEXT[self.freshness]}: last confirmed "
            f"{self.record.last_seen_at.isoformat()}."
        )

    def speak(self, *, disclosable: frozenset[str]) -> tuple[Mapping[str, ProjectedValue], str]:
        """The fields and the notice, handed back together.

        A convenience rather than a guarantee, and the difference is worth stating: nothing
        here can stop a caller reading `.record.fields` and dropping the sentence. What this
        does is remove the ordinary way that happens, which is a composer reaching for the
        values and never learning there was anything else to collect.
        """
        return self.record.fields, self.notice(disclosable=disclosable)

    def trace_line(self) -> str:
        """The full statement, for an auditor rather than for the asker.

        Names the source and the exact age unconditionally, which is safe here for the reason
        `federation.PartialAnswer.trace_lines` is safe: a trace is read by somebody who is
        already entitled to know what the system connects to, and nothing in this module can
        put this string into a channel payload.
        """
        return (
            f"{self.record.source}.{self.record.entity} {self.record.source_id}: "
            f"{self.freshness}, last seen {self.record.last_seen_at.isoformat()}, "
            f"age {self.age}"
        )


def assess_staleness(
    record: ProjectedRecord, *, now: datetime, promise: RefreshPromise | None
) -> ProjectedReading:
    """How old this projected row is, and therefore how it may be spoken about (M11.4.9).

    Pure: an age, a promise, and nothing else. The classification itself is
    `brain.gate.provenance.assess_freshness`, reached by formatting the timestamp the
    projection already holds. That round trip is deliberate and it is the cheap half of the
    trade: it inherits the rule about a read time in the future, which returns UNSTATED
    rather than LIVE because a clock is wrong somewhere and "definitely current" is the one
    claim that cannot be made under skew.

    `promise=None` means the source has undertaken nothing, and the answer is STALE at every
    age. That is not an oversight about young rows. Age is only evidence when something is
    expected to arrive: with no change signal nothing ever will, so time can make the row
    worse and can never make it better, and calling it LIVE for the first fifteen minutes
    would be inventing a promise the source never made. `RefreshPromise` refuses to be built
    from `ChangeSignal.NONE`, so None is the only spelling of "no promise" and there are not
    two ways to say it.
    """
    if now.tzinfo is None:
        # Guarded here as well as inside `assess_freshness`, because the promise-less branch
        # below never reaches that function and would otherwise fail as a TypeError out of
        # datetime subtraction, which reads as a bug in this module rather than in the caller.
        msg = "now must be timezone-aware; comparing it with a recorded read time otherwise lies"
        raise ValueError(msg)
    age = now - record.last_seen_at
    if promise is None:
        return ProjectedReading(record=record, freshness=Freshness.STALE, age=age)
    state = assess_freshness(record.last_seen_at.isoformat(), horizon=promise.horizon, now=now)
    return ProjectedReading(record=record, freshness=state, age=age)
