"""The Projected tier: the row it lands in, the cap that keeps it a pointer, and its age.

Three things are asserted here and they fail in three different ways.

The **cap** fails politely. Nobody adds a thirteenth column; somebody adds one field holding
six, and the count stays at twelve while the projection stops being a pointer. So the tests
below cover both the arithmetic and the shape of a value, and both directions of the
arithmetic: a refusal test with no sibling proving twelve still works is satisfied by a
constructor that refuses everything.

The **table** fails silently. The model and the migration are two hand-written descriptions
of one database and nothing but a test compares them, which is the same arrangement every
other migration in this repository has and the same reason each has a test for it.

**Staleness** fails invisibly, which is the worst of the three. A figure read a fortnight ago
and quoted as current produces no error, no exception and no bug report, because the answer
was true when it was fetched. The tests that matter here are the ones asserting a stale row
is *still served* and *never silent*: either half on its own is a design somebody would
recognise as wrong, and it is the pair that is easy to break one at a time.

Task ids: M11.4.1, M11.4.9
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from brain.connectors.manifest import ChangeSignal
from brain.connectors.projection import (
    MISSED_REFRESHES_BEFORE_STALE,
    UNNAMED_STALENESS_NOTICE,
    ProjectedReading,
    ProjectedRecord,
    RefreshPromise,
    assess_staleness,
)
from brain.core.projection import MAX_LABEL_CHARS, MAX_PROJECTED_FIELDS, ProjectionRefusedError
from brain.db import metadata
from brain.gate.provenance import Freshness
from brain.tables.projection import (
    FIELDS_WITHIN_THE_CAP,
    LOCAL_ID_CHARS,
    SOURCE_ID_CHARS,
    ProjectedRecordRow,
)

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0008_projection.py"

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: An hour, which is what an updated-since cursor running hourly promises. Every freshness
#: assertion below is stated in multiples of this rather than in absolute times, so the
#: thresholds are read as "one interval" and "three intervals" rather than as clock values
#: somebody would have to re-derive.
HOURLY = timedelta(hours=1)

#: Twelve pointer names, none of them on the permanent denylist. Exactly the cap, so the
#: positive case and the negative case are one field apart.
TWELVE = (
    "id",
    "status",
    "stage",
    "kind",
    "priority",
    "owner_ref",
    "company_ref",
    "source_ref",
    "opened_at",
    "closed_at",
    "updated_at",
    "display_name",
)

#: A PostgreSQL dialect to render DDL against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
_DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect


def a_record(**overrides: object) -> ProjectedRecord:
    defaults: dict[str, object] = {
        "source": "xero",
        "entity": "invoice",
        "source_id": "INV-0447",
        "last_seen_at": NOW - HOURLY,
        "fields": {"status": "AUTHORISED", "updated_at": "2026-09-06T08:00:00+00:00"},
    }
    defaults.update(overrides)
    return ProjectedRecord(**defaults)  # type: ignore[arg-type]


def hourly() -> RefreshPromise:
    return RefreshPromise(signal=ChangeSignal.UPDATED_SINCE, interval=HOURLY)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0008", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that the tests read this rather than
    the file's text: a statement sitting in a constant that `upgrade` never executes would
    pass a source-text search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    with Operations.context(context):
        getattr(_migration(), direction)()
    return buffer.getvalue()


def _squash(text: str) -> str:
    """Collapse whitespace, so a statement wrapped across lines still compares."""
    return " ".join(text.split())


def _table() -> Table:
    """The mapped table, narrowed to `Table`.

    `__table__` on a declarative class is annotated `FromClause`, which has no `indexes`
    worth reading and cannot be handed to `CreateTable`. Narrowed once here rather than with
    an ignore comment at each use, so a genuine type error is still reported.
    """
    mapped = ProjectedRecordRow.__table__
    assert isinstance(mapped, Table)
    return mapped


# ------------------------------------------------------- the cap, at construction (M11.4.2)
def test_a_projection_of_exactly_twelve_fields_is_accepted() -> None:
    """The sibling every refusal needs. A cap tested only by what it rejects is satisfied by
    a constructor that rejects everything, and the way that ships is somebody tightening the
    comparison to `<` while fixing something else.

    Twelve is the number in the architecture's tier table, so this also pins that the limit
    is inclusive rather than exclusive: eleven-and-a-cap would be a quieter bug than
    thirteen."""
    record = a_record(fields=dict.fromkeys(TWELVE, "x"))
    assert len(record.fields) == MAX_PROJECTED_FIELDS
    assert record.field_names == tuple(sorted(TWELVE))


def test_a_thirteenth_field_is_refused_when_the_projection_is_built() -> None:
    """The whole point of the tier. Without this the projection grows one useful field at a
    time until it is a second copy of the source system, with its own retention, its own
    staleness and its own breach surface, and nobody ever decided to build that.

    Delete this test and the cap survives only in `check_projection`, which is a function a
    writer has to remember to call."""
    fields = dict.fromkeys((*TWELVE, "resolution"), "x")
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields=fields)
    assert str(MAX_PROJECTED_FIELDS) in str(caught.value)
    assert "13 fields" in str(caught.value)


def test_the_cap_is_refused_at_construction_and_not_by_a_later_check() -> None:
    """`__post_init__` and nothing else. A projection validated by a separate function is a
    projection that exists as a value first, so the thirteenth field has already been
    fetched, held and passed to whichever writer did not call the validator.

    Delete this and the refusal can be moved into a helper nobody has to call, which reads in
    review as a refactor."""
    source = inspect.getsource(ProjectedRecord)
    assert "__post_init__" in source
    with pytest.raises(ProjectionRefusedError):
        ProjectedRecord(
            source="xero",
            entity="invoice",
            source_id="INV-1",
            last_seen_at=NOW,
            fields=dict.fromkeys((*TWELVE, "resolution"), "x"),
        )


def test_a_nested_object_is_refused_rather_than_counted_as_one_field() -> None:
    """The way a twelve-field cap is actually defeated, and it is never deliberate: the
    source returns `contact: {...}` and copying it whole is less work than choosing. Counted
    as one field it is a payload inside the cap; counted as its leaves the cap becomes a
    property of a value rather than of a declaration, so the same field is inside it for one
    record and outside it for the next.

    Delete this test and the projection can hold an entire source record in one key while
    every count in the system says twelve."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields={"contact": {"name": "Wei Ling", "role": "director"}})
    assert "nested object is not one field" in str(caught.value)


def test_a_list_of_values_is_refused_for_the_same_reason_as_an_object() -> None:
    """The other container, and the one that arrives second. A list of tags is a payload that
    grows without any field being added, so a rule that refused only mappings would be
    routed around within a week by returning a list instead."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields={"tags": ["urgent", "renewal"]})
    assert "nested object is not one field" in str(caught.value)


def test_a_forbidden_field_is_still_refused_at_construction() -> None:
    """The cap and the denylist are different rules and both have to survive this boundary.
    A projection of one field is inside every cap there is, and if that field is an email
    address the tier has been broken by a projection nothing counted.

    Delete this and the constructor enforces size without enforcing content."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields={"contact_email": "wl@example.com"})
    assert "denylist" in str(caught.value)


def test_a_label_longer_than_the_limit_is_refused() -> None:
    """A label identifies a record; anything longer is a payload wearing a label's name. The
    120-character limit is what stops a ticket subject becoming a ticket body one edit later.

    Delete this and the cap counts twelve fields, one of which is the whole document."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields={"display_name": "x" * (MAX_LABEL_CHARS + 1)})
    assert str(MAX_LABEL_CHARS) in str(caught.value)


def test_every_reason_a_projection_is_refused_is_reported_together() -> None:
    """One at a time turns writing a connector into a guessing game where each fix reveals
    the next objection, which is the argument `check_projection` makes about its own
    violations and the reason this constructor gathers rather than raising on the first.

    Delete this and the container clause can be moved above the rest, hiding every other
    objection behind it."""
    fields: dict[str, object] = dict.fromkeys(TWELVE, "x")
    fields["contact"] = {"name": "Wei Ling"}
    fields["salary_band"] = "C"
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(fields=fields)
    message = str(caught.value)
    assert "nested object is not one field" in message
    assert "denylist" in message
    assert f"over the {MAX_PROJECTED_FIELDS} limit" in message


def test_an_empty_projection_is_allowed() -> None:
    """A record whose whole projection is its identity, its join key and its age is the
    smallest useful one, and it is the direction the cap pushes. A constructor that required
    at least one field would push authors to add one, which is the opposite of the point."""
    record = a_record(fields={})
    assert record.field_names == ()


# --------------------------------------------------------------- identifying a record
def test_a_projected_record_without_a_source_id_is_refused() -> None:
    """A record that cannot be named cannot be refreshed, cited, or matched to itself on the
    next fetch, so it is reported twice and audited never. `transports.normalise` drops such
    a row one layer earlier; this refuses the one that got past it."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(source_id="   ")
    assert "carries no source id" in str(caught.value)


@pytest.mark.parametrize("bad", ["Xero", "xero-uk", "1xero", ""])
def test_a_source_that_is_not_a_connector_name_is_refused(bad: str) -> None:
    """The manifest, the verified ceiling and the row are all looked up by this string. A
    source spelled differently from the connector's own name is a projection nothing can
    refresh and no limiter can attribute."""
    with pytest.raises(ProjectionRefusedError):
        a_record(source=bad)


@pytest.mark.parametrize("bad", ["Invoice", "invoice-line", ""])
def test_an_entity_that_is_not_a_name_is_refused(bad: str) -> None:
    """The field policy is looked up by this string, and a name nothing matches is withheld
    from everybody. That failure looks like a permission problem and is a typo."""
    with pytest.raises(ProjectionRefusedError):
        a_record(entity=bad)


def test_a_naive_last_seen_at_is_refused() -> None:
    """Singapore reads a naive UTC timestamp as eight hours old, which is the entire width of
    the ageing band for an hourly signal. Accepting one would make every projected row from a
    connector that forgot the zone read as older than it is, or newer, depending on which way
    the machine is configured.

    Delete this and the freshness classification silently depends on a server's locale."""
    with pytest.raises(ProjectionRefusedError) as caught:
        a_record(last_seen_at=datetime(2026, 9, 6, 8, 0))
    assert "naive" in str(caught.value)


def test_a_record_is_unresolved_until_the_registry_gives_it_a_local_id() -> None:
    """`local_id` is the entity registry's answer and is empty during a backfill, which is
    the ordinary state of a freshly projected row. A default that looked resolved would make
    a federation join silently match nothing while reading as a successful lookup."""
    assert not a_record().is_resolved
    assert a_record(local_id="c_0447").is_resolved


# ------------------------------------------------------------------ staleness (M11.4.9)
def test_a_row_inside_the_sources_refresh_interval_is_live() -> None:
    """The positive case for the classification. Without it every assertion below is
    satisfied by a function that returns STALE for everything, which would be a system that
    can never answer from the projection at all."""
    reading = assess_staleness(a_record(last_seen_at=NOW - HOURLY / 2), now=NOW, promise=hourly())
    assert reading.freshness is Freshness.LIVE
    assert reading.is_current
    assert reading.notice(disclosable=frozenset({"xero"})) == ""


def test_a_row_that_has_missed_a_refresh_is_ageing_and_not_yet_stale() -> None:
    """The middle band, and the reason there are three states rather than two. A single
    missed webhook or a cursor run that started late is ordinary; collapsing it into STALE
    makes the stale notice appear constantly and teaches everybody to ignore it."""
    record = a_record(last_seen_at=NOW - HOURLY * 2)
    assert assess_staleness(record, now=NOW, promise=hourly()).freshness is Freshness.AGEING


def test_a_row_older_than_the_missed_refresh_budget_is_stale() -> None:
    """Three consecutive misses is the change signal not working, at which point the row's
    age has stopped being evidence about the record and started being evidence about the
    pipeline.

    Delete this and the ageing band extends forever, which is the exact failure the tier is
    designed against: a value quoted as current, indefinitely, with nothing reporting it."""
    over = HOURLY * MISSED_REFRESHES_BEFORE_STALE + timedelta(minutes=1)
    record = a_record(last_seen_at=NOW - over)
    assert assess_staleness(record, now=NOW, promise=hourly()).freshness is Freshness.STALE


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (HOURLY, Freshness.LIVE),
        (HOURLY + timedelta(seconds=1), Freshness.AGEING),
        (HOURLY * MISSED_REFRESHES_BEFORE_STALE, Freshness.AGEING),
        (HOURLY * MISSED_REFRESHES_BEFORE_STALE + timedelta(seconds=1), Freshness.STALE),
    ],
)
def test_the_two_thresholds_are_inclusive_at_exactly_the_boundary(
    age: timedelta, expected: Freshness
) -> None:
    """Both boundaries, from both sides. A band's edges are where an off-by-one lives, and an
    off-by-one here is invisible: the state is wrong for one interval's width and then right
    again, so it reproduces only when somebody asks at exactly the wrong moment."""
    record = a_record(last_seen_at=NOW - age)
    assert assess_staleness(record, now=NOW, promise=hourly()).freshness is expected


def test_a_stale_row_is_still_returned_and_carries_its_age() -> None:
    """**The decision this leaf turns on.** A stale row must not be silently refused and must
    not be silently served. This is the first half: the fields come back, with the age beside
    them, so an answer can be given and weighed.

    Delete this and the obvious next edit is to refuse on staleness, which turns a sync
    falling behind into an outage for questions the projection could still answer."""
    record = a_record(last_seen_at=NOW - timedelta(days=4), fields={"status": "AUTHORISED"})
    reading = assess_staleness(record, now=NOW, promise=hourly())
    assert reading.freshness is Freshness.STALE
    assert reading.record.fields == {"status": "AUTHORISED"}
    assert reading.age == timedelta(days=4)
    fields, notice = reading.speak(disclosable=frozenset({"xero"}))
    assert fields == {"status": "AUTHORISED"}
    assert notice


def test_a_stale_row_is_never_served_without_something_being_said_about_it() -> None:
    """The second half, and the one that fails invisibly. A figure read four days ago and
    quoted as current produces no error and no bug report, because the answer was true when
    it was fetched.

    Delete this and `notice` can quietly return an empty string for every state, which reads
    in a diff as a simplification."""
    for age in (HOURLY * 2, timedelta(days=4), timedelta(days=400)):
        reading = assess_staleness(a_record(last_seen_at=NOW - age), now=NOW, promise=hourly())
        assert reading.notice(disclosable=frozenset({"xero"})) != ""
        assert reading.notice(disclosable=frozenset()) != ""


def test_the_reading_has_nowhere_to_express_a_refusal() -> None:
    """Stated against the shape rather than against behaviour, in the same form
    `test_abuse_detection_has_nowhere_to_express_a_refusal` uses. A field meaning "withhold"
    is the regression, and it would arrive as a helpful addition during an incident.

    Delete this and the next person to be woken by a stale answer adds `should_serve`."""
    names = {f.name for f in dataclasses.fields(ProjectedReading)}
    names |= {n for n in dir(ProjectedReading) if not n.startswith("_")}
    forbidden = {"refuse", "refused", "withhold", "suppress", "block", "blocked", "deny", "hide"}
    assert not (names & forbidden), f"ProjectedReading has somewhere to refuse: {names}"


def test_a_source_with_no_change_signal_is_stale_at_every_age() -> None:
    """A projection with no change signal is not a stale projection: it is a value that will
    be quoted as current forever. Age is only evidence when something is expected to arrive,
    and with no signal nothing ever will, so recency says nothing.

    Delete this and a signal-less row reads as fresh for the first hour after somebody
    happened to look at it."""
    for age in (timedelta(seconds=1), timedelta(days=90)):
        reading = assess_staleness(a_record(last_seen_at=NOW - age), now=NOW, promise=None)
        assert reading.freshness is Freshness.STALE
        assert reading.age == age


def test_a_promise_cannot_be_built_from_a_source_that_promises_nothing() -> None:
    """`ChangeSignal.NONE` is an absence written down, and a horizon built from it would make
    a signal-less source's rows classify exactly like a webhook's. The manifest already
    refuses the projection at review; this refuses the quieter version, where somebody
    constructs a horizon for it afterwards."""
    with pytest.raises(ProjectionRefusedError) as caught:
        RefreshPromise(signal=ChangeSignal.NONE, interval=HOURLY)
    assert "promised nothing" in str(caught.value)


@pytest.mark.parametrize("interval", [timedelta(), timedelta(seconds=-1)])
def test_a_refresh_interval_that_is_not_an_interval_is_refused(interval: timedelta) -> None:
    """A zero interval makes every row stale on arrival, which hides a real sync failure
    behind a permanent warning. A negative one inverts the horizon, which
    `StalenessHorizon` also refuses, and refusing it here names the field rather than the
    threshold derived from it."""
    with pytest.raises(ProjectionRefusedError):
        RefreshPromise(signal=ChangeSignal.WEBHOOK, interval=interval)


def test_the_horizon_is_derived_from_the_sources_own_interval() -> None:
    """The classification is a function of the age *and the promise*, not of a constant. A
    webhook that arrives in seconds and a nightly cursor cannot share one threshold: with a
    fixed horizon, either the nightly source is permanently stale or the webhook source is
    permanently live.

    Delete this and `MISSED_REFRESHES_BEFORE_STALE` can be replaced by a fixed number of
    hours without a single test noticing."""
    horizon = RefreshPromise(signal=ChangeSignal.WEBHOOK, interval=timedelta(minutes=5)).horizon
    assert horizon.live_for == timedelta(minutes=5)
    assert horizon.stale_after == timedelta(minutes=5) * MISSED_REFRESHES_BEFORE_STALE


def test_a_last_seen_time_in_the_future_is_not_treated_as_the_freshest_thing_we_have() -> None:
    """A clock is wrong somewhere, and skew is the one condition under which "definitely
    current" is exactly the claim that cannot be made. Treating it as LIVE would make a
    misconfigured connector the most trusted source in the company.

    Inherited from `brain.gate.provenance.assess_freshness` rather than re-implemented, which
    is what this test pins: reimplement the classification here and this is what breaks."""
    reading = assess_staleness(a_record(last_seen_at=NOW + HOURLY), now=NOW, promise=hourly())
    assert reading.freshness is Freshness.UNSTATED
    assert reading.notice(disclosable=frozenset({"xero"})) != ""


def test_a_naive_now_is_refused_rather_than_compared() -> None:
    """Comparing a naive clock with a recorded read time produces a number that is wrong by a
    whole timezone and looks entirely plausible. Raised rather than absorbed: a naive `now`
    is a bug in the calling layer, and absorbing it would mark every row UNSTATED, which
    reads in a console as a connector problem and sends somebody to the wrong system."""
    for promise in (hourly(), None):
        with pytest.raises(ValueError, match="timezone-aware"):
            assess_staleness(a_record(), now=datetime(2026, 9, 6, 9, 0), promise=promise)


# ------------------------------------------------ what the asker is told (decision 24)
def test_a_notice_names_a_source_only_when_the_asker_could_already_see_it() -> None:
    """ "The Xero projection is three days behind" tells somebody entitled to no finance data
    that a finance system exists and that we are connected to it. Asked with different
    phrasings it enumerates the estate one question at a time, which is the disclosure
    decision 24 in `docs/needs-rupash.md` is about.

    Delete this and the honest-sounding message ships, and it is honest to the wrong
    audience."""
    reading = assess_staleness(
        a_record(last_seen_at=NOW - timedelta(days=4)), now=NOW, promise=hourly()
    )
    named = reading.notice(disclosable=frozenset({"xero"}))
    unnamed = reading.notice(disclosable=frozenset({"freshdesk"}))
    assert "xero" in named
    assert "xero" not in unnamed
    assert unnamed == UNNAMED_STALENESS_NOTICE


def test_two_undisclosable_sources_produce_the_same_sentence() -> None:
    """The property that makes the fallback worth having. A message that varied by source,
    even in word order, would let somebody distinguish "the one I cannot see is stale" from
    "a different one I cannot see is stale", which is the estate map by another route."""
    stale = timedelta(days=4)
    xero = assess_staleness(a_record(last_seen_at=NOW - stale), now=NOW, promise=hourly())
    desk = assess_staleness(
        a_record(source="freshdesk", entity="ticket", last_seen_at=NOW - stale),
        now=NOW,
        promise=hourly(),
    )
    assert xero.notice(disclosable=frozenset()) == desk.notice(disclosable=frozenset())


def test_the_trace_names_the_source_and_the_age_whatever_the_asker_may_see() -> None:
    """The same split `federation.PartialAnswer` makes between a payload and a trace. An
    auditor is already entitled to know what the system connects to, and without this line
    nobody can tell afterwards why an answer carried a vague notice.

    Delete this and the disclosure rule costs the operator the diagnosis as well."""
    reading = assess_staleness(
        a_record(last_seen_at=NOW - timedelta(days=4)), now=NOW, promise=hourly()
    )
    line = reading.trace_line()
    assert "xero" in line
    assert "INV-0447" in line
    assert str(Freshness.STALE) in line


def test_the_notice_is_reached_through_the_disclosable_set_and_not_a_flag() -> None:
    """Decision 24 is open, and this is what keeps it open. A boolean would have to be given
    a default, the default would be whatever the first caller needed, and the decision would
    then have been made here rather than by the person it belongs to.

    Delete this and `notice(names_sources=True)` appears, which pre-empts the answer."""
    signature = inspect.signature(ProjectedReading.notice)
    assert list(signature.parameters) == ["self", "disclosable"]
    assert signature.parameters["disclosable"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["disclosable"].default is inspect.Parameter.empty


# ------------------------------------------------------------------- the table (M11.4.1)
def test_the_key_is_the_source_the_entity_kind_and_the_source_id() -> None:
    """A surrogate key would let one source record be projected twice, and the second row
    goes on serving the value it was written with while the first is refreshed. Every count
    the fast lane makes is then wrong, with nothing reporting it.

    Delete this test and a `uuid` primary key looks like a tidy-up."""
    assert [c.name for c in _table().primary_key.columns] == ["source", "entity", "source_id"]


def test_the_local_id_is_a_column_and_never_part_of_the_key() -> None:
    """Two reasons and both matter. A merge is a pointer move, so a merge that rewrote a
    primary key would be a delete plus an insert and would read in the ledger as the record
    being removed and re-added. And an unresolved record has no local id at all, which cannot
    sit in a key, so a key containing it could not hold the rows a backfill has just written.

    Delete this and the join key gets promoted into the key during a performance review."""
    columns = _table().columns
    assert "local_id" in columns
    assert columns["local_id"].nullable
    assert "local_id" not in [c.name for c in _table().primary_key.columns]


def test_last_seen_at_is_a_column_of_its_own_and_not_updated_at() -> None:
    """A source confirming an unchanged record moves `last_seen_at` and leaves `updated_at`
    alone. Compute staleness from `updated_at` and a record confirmed five minutes ago reads
    as a month old; drop `last_seen_at` and a record nothing has confirmed for a fortnight
    reads as fresh for as long as nobody edits it.

    Delete this and the two are merged by somebody who sees two timestamps and one fact."""
    columns = _table().columns
    assert "last_seen_at" in columns
    assert not columns["last_seen_at"].nullable
    assert "updated_at" in columns


def test_the_twelve_field_cap_is_written_into_the_column_definition() -> None:
    """The constructor is our code and the rows that get a table into trouble are the ones
    that did not come through it: a seed, a hand-written INSERT during an incident, a
    backfill written next year against the table directly. `auth.principal` carries its own
    bounded-engagement rule twice for the same reason.

    Delete this and the cap exists only for as long as everybody uses the constructor."""
    checks = {
        str(c.name): str(c.sqltext) for c in _table().constraints if isinstance(c, CheckConstraint)
    }
    assert str(MAX_PROJECTED_FIELDS) in checks["ck_record_fields_within_the_cap"]
    assert "jsonb_path_query_array" in checks["ck_record_fields_within_the_cap"]
    # Not redundant: jsonpath runs in lax mode, so `$.keyvalue()` over a scalar yields
    # nothing and counts as zero keys, which passes a cap that only counts.
    assert checks["ck_record_fields_is_an_object"] == "jsonb_typeof(fields) = 'object'"


def test_the_database_cap_is_generated_from_the_same_constant_the_code_counts_against() -> None:
    """Stated separately from the DDL comparison below, because that one fails with a wall of
    SQL and this one names the constant. Raising `MAX_PROJECTED_FIELDS` without a migration
    would otherwise leave the database enforcing the old number, and the failure appears as
    an insert rejected in production for a projection the code considered valid."""
    assert FIELDS_WITHIN_THE_CAP.endswith(f"<= {MAX_PROJECTED_FIELDS}")
    assert _migration().FIELDS_WITHIN_THE_CAP == FIELDS_WITHIN_THE_CAP


def test_the_column_widths_match_the_identifiers_that_write_them() -> None:
    """A width the type accepts and the column truncates is a projected row that points at a
    record which does not exist, and truncation is silent in PostgreSQL only for the varchar
    that fits, so the failure arrives as a foreign record id nobody can resolve."""
    columns = _table().columns
    assert columns["source_id"].type.length == SOURCE_ID_CHARS  # type: ignore[attr-defined]
    assert columns["local_id"].type.length == LOCAL_ID_CHARS  # type: ignore[attr-defined]
    source = inspect.getsource(_migration())
    assert f"sa.String({SOURCE_ID_CHARS})" in source
    assert f"sa.String({LOCAL_ID_CHARS})" in source


# ---------------------------------------------------------------- the migration (0008)
def test_the_migration_follows_the_one_before_it() -> None:
    """A revision that does not chain is a migration Alembic never runs, and the symptom is a
    table missing in production while every test passes."""
    module = _migration()
    assert module.revision == "0008"
    assert module.down_revision == "0007"


def test_the_migration_builds_the_table_the_model_declares() -> None:
    """The migration copies the model's predicates rather than importing them, so that it
    keeps describing the database it actually built, which means the copy needs something
    comparing it or it rots without saying so.

    Compared on rendered DDL rather than on source text, so a difference in type, width,
    nullability, default or constraint is caught rather than a difference in wording. The
    indexes are compared too: `SoftDeleteMixin` declares one that is easy to leave out of a
    migration, and an index the model believes exists is a query plan nobody measured."""
    assert _migration().TABLES == ("proj.record",)
    upgrade = _squash(_rendered("upgrade"))
    assert _squash(str(CreateTable(_table()).compile(dialect=_DIALECT))) in upgrade
    indexes = sorted(_table().indexes, key=lambda i: i.name or "")
    assert [i.name for i in indexes] == ["ix_proj_record_deleted_at", "ix_record_local_id_live"]
    for index in indexes:
        assert _squash(str(CreateIndex(index).compile(dialect=_DIALECT))) in upgrade


def test_the_migration_enables_row_level_security() -> None:
    """A policy on a table without row-level security enabled is a policy PostgreSQL never
    consults, and `sweep_rls` fails the build on a table in a named schema without it. The
    two statements are separate and forgetting the first is silent."""
    sql = _squash("\n".join(_migration().RLS))
    assert "ALTER TABLE proj.record ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY record_live ON proj.record" in sql
    assert "USING (deleted_at IS NULL)" in sql


def test_the_fast_lane_can_read_the_projection_and_do_nothing_else() -> None:
    """The fast lane answers without a model, from the local projection only, and 0001 gave
    it no privilege beyond USAGE on this schema. This is the first table in `proj`, so it is
    the first migration that can hand it anything, and both halves are needed: without the
    grant it cannot select, and without a policy naming the role, row-level security returns
    an empty table to it, which is indistinguishable from an empty database.

    Delete this and the fast lane silently answers from nothing."""
    policies = _squash("\n".join(_migration().RLS))
    assert "CREATE POLICY record_fastlane_read ON proj.record FOR SELECT TO brain_fastlane" in (
        policies
    )
    # Matched as the whole statement rather than as a substring, so a widened grant fails
    # here instead of passing because SELECT is still one of the verbs in the list.
    fastlane = [g for g in _migration().GRANTS if "brain_fastlane" in g]
    assert fastlane == ["GRANT SELECT ON proj.record TO brain_fastlane"]


def test_the_migration_grants_no_delete() -> None:
    """A projected record that disappears from the source is retired with `deleted_at`, not
    removed: "was that ticket real, and when did it stop existing" is asked after a wrong
    answer. The one DELETE grant in this system belongs to `auth.directory_role_grant`, and
    `test_no_other_table_in_any_migration_grants_delete` is what keeps it the only one."""
    assert all("DELETE" not in statement for statement in _migration().GRANTS)


def test_the_downgrade_drops_what_the_upgrade_built() -> None:
    """A migration with no way back is a deploy with no way back. `proj` is not dropped: 0001
    created all nine schemas and 0001's downgrade owns them."""
    down = _squash(_rendered("downgrade"))
    assert "DROP TABLE proj.record" in down
    assert "DROP SCHEMA" not in down


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists and does something, no unreviewed
    autogeneration markers, no schema and data change in one file."""
    from brain.ops.migration_policy import check_file

    assert check_file(MIGRATION) == []


def test_the_table_is_in_the_package_tuple() -> None:
    """`brain.tables.TABLES_IN_DEPENDENCY_ORDER` is the one list anything outside the package
    reads, and a partial list is worse than none: the table it omits looks accounted for, and
    `alembic revision --autogenerate` would propose dropping it."""
    from brain.db import Base
    from brain.tables import TABLES_IN_DEPENDENCY_ORDER

    # Membership and position relative to what it depends on, not an index. This asserted
    # `[-1] == "proj.record"`, which means "last" and broke the moment the next table was
    # appended, in a test about the projection that had nothing to say about the new one. The
    # order matters because a downgrade runs it backwards; being at the end does not.
    assert "proj.record" in TABLES_IN_DEPENDENCY_ORDER
    assert TABLES_IN_DEPENDENCY_ORDER.index("proj.record") > TABLES_IN_DEPENDENCY_ORDER.index(
        "auth.principal"
    ), "a projected record is keyed by a source and must be built after the principals it maps to"
    assert set(TABLES_IN_DEPENDENCY_ORDER) == set(Base.metadata.tables)
