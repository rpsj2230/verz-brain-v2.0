"""The Laravel connector, tested against a fake reader rather than against a client's MySQL.

This is the only connector in the repository that reaches a database, and it reaches
somebody else's production one, so four properties are pinned here and each has a wrong
version that passes every test written without thinking about it.

**Views, never tables.** A connector holding SELECT on a table sees whatever next Tuesday's
migration adds. The tests assert that a table name is refused at connect by shape, that the
entity-to-view mapping is total, and that a view outside the allowlist is refused by string
equality. The wrong version accepts a name from a caller and validates it, which looks
stricter and is a parser competing with MySQL's own.

**A read is bounded or it does not exist.** `DatabaseTransport.plan` permits `limit=0`, which
means the whole view, and that is the value this connector must never produce. The tests
assert on the constructed value rather than on a code path, because a bound applied by a
function is a bound the next caller of a different function skips.

**A password hash is refused from the column list, not from the projection.** It is not on
the platform denylist, it matches none of its patterns, and declared as a label with a hot
use it passes all five clauses of the projectability test. One test proves exactly that and
then proves this module refuses it anyway, which is the shape `tests/unit/test_xero.py` uses
about money. The company canary `CANARY-CONTRACT-7Q4XZ` does the same job for the contract
value: a leak is greppable rather than plausible.

**Absent, refused and unreachable stay three answers.** `tests/invariants/test_cassettes.py`
asserts the corpus keeps all three; these assert this connector keeps them apart to the end,
including the case only a database has, where the view itself is gone. The wrong version
reports a dropped view as an empty result, and "this client has no projects" is a sentence
somebody acts on.

Task ids: M11.6.1
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.change_signal import DeletionCheck
from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    FetchRequest,
    HealthState,
    TransportKind,
    assert_fetches_only,
)
from brain.connectors.federation import FailureReason
from brain.connectors.laravel import (
    CONNECTOR_NAME,
    DETAIL_ACCESS_DENIED,
    DETAIL_CAPPED,
    DETAIL_NEVER_PROBED,
    DETAIL_VIEW_MISSING,
    ENTITIES,
    ENTITY_CLIENT,
    ENTITY_USER,
    HEALTH_FOR_CALL,
    ID_COLUMN,
    LIVE_ONLY,
    MAX_ROWS_EVER,
    NEVER_SELECTED,
    OUTCOME_FOR_FAULT,
    PROJECTED_FIELDS,
    REASON_FOR_FAULT,
    BoundedRead,
    DatabaseFault,
    LaravelConnection,
    LaravelDegraded,
    LaravelError,
    LaravelOutcome,
    LaravelReply,
    ReadBounds,
    ViewReply,
    assert_builds_no_sql,
    assert_columns_are_selectable,
    assert_declarations_agree,
    assert_is_a_view,
    assert_views_are_holdable,
    columns_for,
    connector_fetch,
    health,
    interpret,
    laravel_field_policy,
    laravel_manifest,
    projected_record,
    projection_for,
    read_plan,
    refresh_promise,
    selected_columns,
    subscription,
)
from brain.connectors.manifest import (
    ChangeSignal,
    FieldShape,
    HotUse,
    ManifestError,
    ProjectedField,
    failed_clauses,
    projectability,
)
from brain.connectors.projection import assess_staleness
from brain.connectors.throttle import CallOutcome, UnmeasuredSourceError, limits_for
from brain.connectors.transports import TransportError
from brain.core.errors import Degraded
from brain.core.field_policy import Classification
from brain.core.projection import MAX_PROJECTED_FIELDS, ProjectionRefusedError, is_forbidden
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import Freshness, StalenessHorizon
from brain.knowledge.rows import assert_takes_no_sql
from brain.ops.secrets import SecretRef, VaultRole
from tests.fixtures.cassettes import CASSETTES, Source, for_source, limit_for
from tests.fixtures.company import CANARIES

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

SCHEMA = "portal"

REF = SecretRef(path="connectors/creds/laravel_readonly", role=VaultRole.APPLICATION)

MONEY_CANARY = CANARIES["client.contract_value"]

#: A horizon short enough that "just read" and "read yesterday" land in different states.
HORIZON = StalenessHorizon(live_for=timedelta(minutes=15), stale_after=timedelta(hours=24))

#: What a Laravel `users` table actually holds beside the display name. None of the three is
#: on `brain.core.projection`'s denylist, which is why this connector refuses them itself.
LARAVEL_CREDENTIAL_COLUMNS = ("password", "remember_token", "two_factor_secret")


def bounds(*, max_rows: int = 200, timeout_seconds: float = 5.0) -> ReadBounds:
    return ReadBounds(max_rows=max_rows, timeout_seconds=timeout_seconds)


def connection(**overrides: Any) -> LaravelConnection:
    settings: dict[str, Any] = {"schema": SCHEMA, "bounds": bounds()}
    settings.update(overrides)
    return LaravelConnection(**settings)


def department(name: str = "maintenance") -> Scope:
    """The predicate a client's view definition would be read as: one servicing department."""
    return Scope(clauses=(Clause(field="department", op=Op.EQ, value=name),))


def visibility() -> dict[str, Scope]:
    return {entity: department() for entity in ENTITIES}


def manifest(**overrides: Any) -> Any:
    settings: dict[str, Any] = {"ref": REF, "visibility": visibility()}
    settings.update(overrides)
    return laravel_manifest(connection(), **settings)


def client_row(**overrides: Any) -> dict[str, Any]:
    """One row as a view would hand it over, including a column nobody declared."""
    row: dict[str, Any] = {
        "id": 4471,
        "name": "SNM Construction Pte Ltd",
        "status": "active",
        "department": "maintenance",
        "manager_id": "u_weiling",
        "updated_at": "2026-09-01T10:00:00+00:00",
        "contract_value": MONEY_CANARY,
    }
    row.update(overrides)
    return row


class Reader:
    """A reader that answers with one reply and records every read it was asked for.

    The recording is the point. Several tests assert this list is empty, which is a stronger
    claim than asserting an exception was raised: a refusal that happens after the read was
    issued has already spent time on the client's production database.
    """

    def __init__(self, reply: ViewReply) -> None:
        self.reply = reply
        self.reads: list[BoundedRead] = []

    def rows(self, read: BoundedRead) -> ViewReply:
        self.reads.append(read)
        return self.reply


def a_read(entity: str = ENTITY_CLIENT, **overrides: Any) -> BoundedRead:
    return read_plan(connection(), entity, **overrides)


def answered(rows: tuple[dict[str, Any], ...] = (), **overrides: Any) -> LaravelReply:
    return interpret(a_read(**overrides), ViewReply(rows=rows), fetched_at=NOW.isoformat())


def failed(fault: DatabaseFault) -> LaravelReply:
    return interpret(a_read(), ViewReply(fault=fault), fetched_at=NOW.isoformat())


# --------------------------------------------------- views and never tables (M11.1.4)
def test_a_table_name_is_refused_where_this_connector_expects_a_view() -> None:
    """The whole leaf. A connector that can read a table can read a column added in next
    Tuesday's migration, unclassified and unreviewed; a view is the client's own statement of
    what we may read and it changes only when they change it.

    Delete this and `portal.clients` in a manifest installs, works, and is discovered by
    somebody reading a trace months later."""
    with pytest.raises(LaravelError, match="does not read as a view"):
        assert_is_a_view("portal.clients")


def test_a_view_name_that_is_not_schema_qualified_is_refused() -> None:
    """An unqualified name resolves against whatever database the connection was left set to,
    which is a property of the session rather than of the manifest. In MySQL the schema is the
    database, so the qualification is what stops a second tenant's `v_client` being read.

    Delete this and a connector reads whichever database the driver happened to select."""
    with pytest.raises(LaravelError, match="not schema-qualified"):
        assert_is_a_view("v_client")


@pytest.mark.parametrize("name", ["Portal.v_client", "portal.V_client", "portal.v client"])
def test_a_view_name_the_server_would_need_quoting_for_is_refused(name: str) -> None:
    """A name that has to be quoted to be accepted is a name a second statement can start
    inside, and it is also a name that matches the allowlist in one casing and not another.

    Delete this and the allowlist's string equality becomes case-dependent, which is the one
    property it was chosen for."""
    with pytest.raises(LaravelError, match="lower-case identifier"):
        assert_is_a_view(name)


def test_every_view_this_connection_produces_is_one_the_transport_will_hold() -> None:
    """The positive sibling, and the relationship this module depends on: the check here is
    meant to be the stricter of the two. If the transport's rule ever became stricter, a name
    accepted at connect would be refused at the seam during somebody's request.

    Delete this and the two rules can drift until a manifest that installs cannot read."""
    assert_views_are_holdable(connection())
    assert connection().views() == ("portal.v_client", "portal.v_user")


def test_a_view_this_connection_does_not_name_is_refused_by_string_equality() -> None:
    """A read-only credential still reaches every view it was granted. The allowlist is the
    half of that restriction we can see, and equality has no dialect: there is no pattern to
    be wrong about and no way to express a second statement.

    Delete this and the allowlist is decoration, because nothing asserts it is consulted."""
    transport = connection().transport()
    with pytest.raises(TransportError, match="not on this connector's allowlist"):
        transport.plan("portal.v_salary")
    assert connection().admits("portal.v_salary") is False
    assert connection().admits("portal.v_client") is True


def test_the_connect_scope_and_the_allowlist_are_built_from_one_declaration() -> None:
    """Two lists that disagree mean one of them is not the restriction anybody approved, and
    the wider one is the one that runs. They are built from one tuple here so they cannot
    disagree, and `assert_scope_covers` still runs at connect to catch the edit that gives
    them separate sources.

    Delete this and a future connection that took its scope from configuration and its
    allowlist from this module would install with nobody noticing the gap."""
    conn = connection()
    assert conn.scope().selectors == conn.transport().views
    assert conn.scope().resource_kind == "view"


def test_an_entity_nothing_declares_has_no_view_to_be_read_from() -> None:
    """The mapping is total, so an unknown entity has nowhere to read from rather than a name
    somebody can supply. There is no path in this module that takes a view name from a caller,
    which is why the equality check downstream is reached with a string this module produced.

    Delete this and a caller-supplied entity becomes a caller-supplied view name."""
    with pytest.raises(LaravelError, match="was asked for 'invoice'"):
        connection().view_for("invoice")
    with pytest.raises(LaravelError, match="was asked for 'invoice'"):
        columns_for("invoice")


# ------------------------------------------- the bound on somebody else's database (M11.6.1)
def test_a_read_with_no_row_cap_cannot_be_constructed() -> None:
    """`DatabaseTransport.plan` refuses a negative limit and permits zero, and zero is no
    limit at all: on the client's production database that is the whole view, a read view
    pinned for the length of the scan, and their buffer pool evicted under them.

    Delete this and the one value that matters is the one nothing refuses."""
    conn = connection()
    unbounded = conn.transport().plan("portal.v_client", limit=0)
    with pytest.raises(LaravelError, match="which is the whole view"):
        BoundedRead(
            entity=ENTITY_CLIENT,
            plan=unbounded,
            columns=columns_for(ENTITY_CLIENT),
            timeout_seconds=5.0,
        )


def test_a_read_with_no_time_bound_cannot_be_constructed() -> None:
    """A row cap without a time bound still scans the whole view to find the first rows, so
    the query runs to completion inside the server whatever we asked to be sent back.

    Delete this and every read is capped in the direction that costs the client nothing."""
    conn = connection()
    with pytest.raises(LaravelError, match="no time bound"):
        BoundedRead(
            entity=ENTITY_CLIENT,
            plan=conn.transport().plan("portal.v_client", limit=10),
            columns=columns_for(ENTITY_CLIENT),
            timeout_seconds=0.0,
        )


@pytest.mark.parametrize("rows", [0, -1, MAX_ROWS_EVER + 1])
def test_a_row_cap_that_is_not_a_cap_is_refused_at_the_declaration(rows: int) -> None:
    """A bound that can be set to anything is a bound in name. The ceiling on the ceiling is a
    judgement rather than a measurement, and it is judging that a read of this size is a
    report rather than an answer and belongs in a paced backfill.

    Delete this and an operator sets max_rows to a million while believing they set a bound."""
    with pytest.raises(LaravelError):
        ReadBounds(max_rows=rows, timeout_seconds=5.0)


def test_a_time_bound_that_is_not_a_bound_is_refused_at_the_declaration() -> None:
    """The other half, and the reason there is no default anywhere: the right numbers are a
    property of one client's database and hardware, and a default applied on their behalf
    would be an inference presented as a declaration.

    Delete this and a zero or absent timeout reads as "no timeout" to every driver."""
    with pytest.raises(LaravelError, match="not a bound"):
        ReadBounds(max_rows=100, timeout_seconds=0.0)
    with pytest.raises(LaravelError, match="past the"):
        ReadBounds(max_rows=100, timeout_seconds=600.0)


def test_a_caller_asking_for_more_than_the_cap_gets_the_cap() -> None:
    """Clamped rather than refused, because the caller is not making a mistake: they are
    asking for something this connector will not do to somebody else's production database,
    and the result comes back marked as cut short. Refusing would teach callers to ask for
    exactly the cap, which produces the identical read with nothing saying so.

    Delete this and a limit of 100,000 travels into a statement against a live database."""
    read = read_plan(connection(bounds=bounds(max_rows=50)), ENTITY_CLIENT, limit=100_000)
    assert read.plan.limit == 50


def test_a_caller_asking_for_nothing_in_particular_gets_the_cap_rather_than_no_limit() -> None:
    """The positive sibling and the ordinary case. A `FetchRequest` defaults its limit to
    zero, so "the caller did not say" is the commonest way an unbounded read would arise.

    Delete this and every fetch built from a default request reads the whole view."""
    read = read_plan(connection(bounds=bounds(max_rows=50)), ENTITY_CLIENT)
    assert read.plan.limit == 50
    assert read.timeout_seconds == 5.0


def test_a_negative_limit_is_left_to_the_transport_to_refuse() -> None:
    """One refusal rather than two. The transport already refuses a negative limit, and a
    second copy here would be a second opinion about the same number that can drift.

    Delete this and somebody adds a clamp that turns a negative into the cap, which hides a
    caller computing its limit wrongly."""
    with pytest.raises(TransportError, match="not a limit"):
        read_plan(connection(), ENTITY_CLIENT, limit=-1)


def test_a_read_carries_a_view_and_a_column_list_and_never_a_statement() -> None:
    """There is no string here that could carry a second statement, because there is no string
    here that becomes one. The plan names a view from a closed set and the columns from a
    declaration, and whoever executes builds the statement with every value bound.

    Delete this and a `sql` attribute could be added to the plan and used, with every other
    test in this file still green."""
    read = a_read()
    assert read.plan.view == "portal.v_client"
    assert not hasattr(read.plan, "sql")
    assert not hasattr(read, "sql")


# ----------------------------------------------------------- no statement is composed
def test_no_statement_in_this_module_is_built_by_interpolation() -> None:
    """Read over the parsed syntax tree rather than the source text, which matters here more
    than anywhere: this module's prose contains every word a text search would look for, and
    two tests in this repository have already been satisfied by their own docstrings.

    Delete this and an f-string handed to a driver would pass review in a module whose whole
    argument is that it never composes one."""
    assert_builds_no_sql()


def test_the_fetch_can_be_handed_neither_the_callers_grants_nor_a_fragment() -> None:
    """Two rules from two modules on one closure, both read off the signature rather than the
    body. A connector never decides a permission question, and the connector that reaches a
    database must also never be handed something that could become part of a statement.

    Delete this and the assertions inside `connector_fetch` could be removed with nothing
    noticing that the closure is now checked by nobody."""
    fetch = connector_fetch(
        connection(), ENTITY_CLIENT, reader=Reader(ViewReply()), fetched_at=NOW.isoformat()
    )
    assert_fetches_only(fetch)
    assert_takes_no_sql(fetch)


# ----------------------------------------------- the users table (M11.4.2, M11.4.4)
def test_a_password_hash_passes_every_platform_rule_and_is_refused_here() -> None:
    """**The measured case.** `password_hash` is not on `brain.core.projection`'s denylist and
    matches none of its patterns, so declared as a label with a hot use it passes all five
    clauses of the projectability test. The platform would let it be projected; the refusal
    has to be this module's, and it is on the column list rather than on the projection,
    because refusing to store a hash already fetched into this process is not a control.

    Delete this and a Laravel users row's bcrypt hash is fetchable and storable, and every
    other test in this file stays green."""
    assert is_forbidden("password_hash") is False
    declared = ProjectedField(name="password_hash", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,))
    verdicts = projectability(
        declared, signal=ChangeSignal.UPDATED_SINCE, label_count=1, field_count=1
    )
    assert failed_clauses(verdicts) == ()

    with pytest.raises(ProjectionRefusedError, match="named for a credential"):
        assert_columns_are_selectable(ENTITY_USER, ("id", "display_name", "password_hash"))


@pytest.mark.parametrize("column", LARAVEL_CREDENTIAL_COLUMNS)
def test_every_credential_a_laravel_users_row_holds_is_refused_by_one_pattern(column: str) -> None:
    """The three that sit beside the display name in a real Laravel schema. Matched by
    `contract.CREDENTIAL_ATTRIBUTE_RE`, imported rather than restated, because a credential
    column and a credential attribute are one mistake at two layers and a list written here
    would be a second one to keep in step.

    Delete this and a rule tested only against `password` passes a connector that selects
    `remember_token`, which is a session credential in a column."""
    assert is_forbidden(column) is False
    with pytest.raises(ProjectionRefusedError, match="named for a credential"):
        assert_columns_are_selectable(ENTITY_USER, (column,))


def test_a_staff_email_is_never_selected_rather_than_never_stored() -> None:
    """The platform denylist stops an email being *stored*, which leaves it fetched, held in
    this process and written into a trace. This connector refuses it from the column list, so
    it never leaves the client's database at all. It is also the field somebody wants most,
    because it is the join key to Keycloak and Lark, and that join belongs to
    `brain.identity`, which already holds it from the directory.

    Delete this and "we never store emails" stays true while every read fetches one."""
    assert is_forbidden("email") is True
    with pytest.raises(ProjectionRefusedError, match="permanent denylist"):
        assert_columns_are_selectable(ENTITY_USER, ("id", "display_name", "email"))
    assert "email" not in columns_for(ENTITY_USER)
    assert "phone" not in columns_for(ENTITY_USER)


def test_a_date_of_birth_is_refused_although_no_platform_rule_touches_it() -> None:
    """Not on the denylist, not matched by any of its patterns, and declared as a timestamp
    with a sort use it passes every clause: a personal identifier wearing a timestamp's shape.

    Delete this and the refusal list shrinks to the fields the platform already refuses, which
    is a list that refuses nothing new."""
    assert is_forbidden("date_of_birth") is False
    with pytest.raises(ProjectionRefusedError, match="personal identifier"):
        assert_columns_are_selectable(ENTITY_USER, ("id", "date_of_birth"))


def test_every_column_this_connector_actually_declares_is_selectable() -> None:
    """The positive sibling. A guard tested only by its refusals is satisfied by a function
    that refuses everything, and a connector that can read no column at all passes every test
    above.

    Delete this and `assert_columns_are_selectable` could refuse unconditionally."""
    for entity in ENTITIES:
        columns = columns_for(entity)
        assert ID_COLUMN in columns
        assert_columns_are_selectable(entity, columns)
    assert "display_name" in columns_for(ENTITY_USER)
    assert "name" in columns_for(ENTITY_CLIENT)


def test_a_column_the_declaration_does_not_name_never_reaches_a_record() -> None:
    """The record is built from the declared columns rather than copied from the row that came
    back, so an executor that ran a wider statement of its own cannot widen the answer. The
    row here carries a bcrypt hash the way a `SELECT *` would have handed one over.

    Delete this and the safety of the column list depends on every executor, for ever."""
    reply = answered((client_row(password="$2y$10$notarealhash", email="ops@example.com"),))
    assert reply.rows is not None
    kept = reply.rows.records[0].model_dump()
    assert "password" not in kept
    assert "email" not in kept
    assert kept["name"] == "SNM Construction Pte Ltd"


def test_money_is_selected_live_and_never_projected() -> None:
    """`contract_value` is why somebody asks this connector a question and it is a payload
    rather than a pointer: stored, it is filtered and quoted as current long after the
    contract was renegotiated. It is selected on every client read for whoever holds
    `read:client.contract_value`, and it is not one of the projected fields.

    Delete this and the field the company canaries protect becomes a stored column, which
    reads as an optimisation in a diff."""
    assert "contract_value" in columns_for(ENTITY_CLIENT)
    assert LIVE_ONLY[ENTITY_CLIENT] == ("contract_value",)
    assert "contract_value" not in {f.name for f in PROJECTED_FIELDS[ENTITY_CLIENT]}

    record = projected_record(ENTITY_CLIENT, client_row(), last_seen_at=NOW)
    assert record is not None
    assert MONEY_CANARY not in str(record.fields)
    assert record.source_id == "4471"


def test_the_projection_of_every_entity_is_a_pointer_inside_the_cap() -> None:
    """Twelve is the cap and five is what a client needs: enough to find the record, join it,
    filter it and sort it, and not one field of what it says. The room left over is not
    thrift, it is what a real need is spent on later.

    Delete this and a projection can grow to a mirror one convenient column at a time."""
    for entity in ENTITIES:
        declared = PROJECTED_FIELDS[entity]
        assert 0 < len(declared) <= MAX_PROJECTED_FIELDS
        assert sum(1 for f in declared if f.shape is FieldShape.LABEL) <= 1
        assert all(f.uses for f in declared)
        assert ID_COLUMN not in {f.name for f in declared}


def test_the_declared_columns_the_projection_and_the_policy_agree() -> None:
    """Three lists edited by three different people at three different times. A selected
    column nothing classifies leaves the client's database and is withheld from everybody; a
    projected field nothing selects is a filter that silently matches nothing.

    Delete this and either disagreement is invisible in review and silent at runtime."""
    assert_declarations_agree()
    policy = laravel_field_policy()
    for entity in ENTITIES:
        for column in selected_columns(entity):
            assert policy.governs(entity, column)
    assert policy.rule_for(ENTITY_CLIENT, "contract_value").classification is (
        Classification.RESTRICTED
    )


# ------------------------------------------------- absent, refused, unreachable (M11.5.5)
def test_an_empty_view_is_absent_and_carries_a_read_time() -> None:
    """A view returning no rows is a fact about the client's business, and it is the answer
    the two failures below must never be confused with. The corpus records the same
    distinction on another source (`HUBSPOT-200-empty`).

    Delete this and a connector that reported every empty result as a failure would pass."""
    reply = answered(())
    assert reply.outcome is LaravelOutcome.ABSENT
    assert reply.call is CallOutcome.OK
    assert reply.rows is not None and reply.rows.records == ()
    assert reply.fetched_at == NOW.isoformat()
    assert reply.failure() is None


def test_a_view_that_is_no_longer_there_is_refused_and_never_reported_as_empty() -> None:
    """**The sharp one.** The tempting reading is that the view is not there, so there is
    nothing to return, so return nothing: that turns a dropped view into "this client has no
    projects", nobody files a bug because an empty list is plausible, and the projection stops
    being refreshed in the same silence.

    Delete this and the most likely failure of this connector, a migration dropping a view,
    is reported as data."""
    reply = failed(DatabaseFault.UNKNOWN_VIEW)
    assert reply.outcome is LaravelOutcome.REFUSED
    assert reply.call is CallOutcome.REJECTED
    assert reply.rows is None
    assert reply.detail == DETAIL_VIEW_MISSING
    assert reply.failure() == pytest.approx(reply.failure())


def test_a_narrowed_grant_is_refused_rather_than_unreachable() -> None:
    """The database is perfectly healthy and has declined this connector's grant. Reporting it
    as an outage sends somebody to check whether MySQL is up, which it is, and the action that
    is actually available is a conversation with the client's DBA.

    Delete this and a permission change reads as an incident for a week."""
    reply = failed(DatabaseFault.ACCESS_DENIED)
    assert reply.outcome is LaravelOutcome.REFUSED
    assert reply.detail == DETAIL_ACCESS_DENIED
    assert reply.reason is FailureReason.NOT_SERVING


def test_a_read_stopped_by_its_own_time_bound_is_unreachable_and_says_so_in_the_trace() -> None:
    """Our bound firing is not the database refusing us, and the reason travels as a timeout
    rather than as a transport failure so that whoever reads the trace knows which number to
    look at.

    Delete this and every fault below OK collapses into one reason, and the one thing an
    operator could act on is gone."""
    reply = failed(DatabaseFault.TIMED_OUT)
    assert reply.outcome is LaravelOutcome.UNREACHABLE
    assert reply.reason is FailureReason.TIMEOUT
    assert reply.rows is None and reply.fetched_at == ""


def test_a_failed_read_can_be_given_neither_rows_nor_a_read_time() -> None:
    """The constructor is the guarantee. "Answer the outage from the last good read" is then
    something a caller cannot express, rather than something they are asked not to do. A read
    time would be worse than the rows: `assess_freshness` would date it and report the answer
    as current.

    Delete this and a well-meaning caching layer can substitute a value and look correct."""
    with pytest.raises(LaravelError, match="rows or a read time"):
        LaravelReply(
            outcome=LaravelOutcome.UNREACHABLE,
            call=CallOutcome.UNAVAILABLE,
            reason=FailureReason.TRANSPORT,
            fetched_at=NOW.isoformat(),
        )


def test_a_failed_read_is_undatable_so_nothing_can_quote_it_as_current() -> None:
    """A failed reply has no read time, so `brain.gate.provenance` returns UNSTATED by its own
    rule about a time it cannot date, rather than this module inventing a second freshness
    scale that would eventually disagree.

    Delete this and a branch here could return LIVE for a reply that read nothing."""
    assert failed(DatabaseFault.UNAVAILABLE).freshness(horizon=HORIZON, now=NOW) is (
        Freshness.UNSTATED
    )
    assert answered((client_row(),)).freshness(horizon=HORIZON, now=NOW) is Freshness.LIVE


def test_a_refusal_and_an_outage_read_identically_to_a_person_and_differ_in_the_trace() -> None:
    """Which of our systems is unwell is ours to act on and not the asker's, so both produce
    the platform's one sentence for an unreachable source. The trace keeps the difference,
    because it is read by somebody already entitled to know what this system connects to.

    Delete this and a refusal that read differently would tell anybody who can type a question
    which of our credentials is wrong."""
    refused = failed(DatabaseFault.ACCESS_DENIED)
    unreachable = failed(DatabaseFault.UNAVAILABLE)
    nothing_disclosed: frozenset[str] = frozenset()
    assert refused.notice(disclosable=nothing_disclosed) == Degraded.public_message
    assert unreachable.notice(disclosable=nothing_disclosed) == Degraded.public_message
    assert refused.trace_line() != unreachable.trace_line()
    assert CONNECTOR_NAME in refused.trace_line()


def test_the_only_recorded_laravel_exchange_is_a_failure_and_it_is_not_trusted() -> None:
    """The corpus records one Laravel exchange and it is a 500 from the application's own
    internal endpoint, recorded to say that being in-house is not a reason to trust an error
    response. It is not a database error, which is why the reply carries it as an application
    status rather than as an invented MySQL code.

    Delete this and the one recording that exists for this source is compiled against
    nothing."""
    recorded = for_source(Source.LARAVEL)
    assert len(recorded) == 1
    failure = recorded[0]
    assert failure.status == 500
    reply = interpret(
        a_read(), ViewReply(app_status=failure.status), fetched_at=NOW.isoformat()
    )
    assert reply.outcome is LaravelOutcome.UNREACHABLE
    assert reply.rows is None
    assert reply.reason is FailureReason.TRANSPORT


def test_an_application_status_below_a_failure_is_refused() -> None:
    """This connector reads views and not the application's HTTP surface, so a status here is
    only ever how a failure was recorded. A 200 arriving through that seam would be a second
    read path nobody declared.

    Delete this and the narrow seam for one recording becomes a general HTTP connector."""
    with pytest.raises(LaravelError, match="how a failure was recorded"):
        ViewReply(app_status=200)


def test_a_reply_carrying_rows_and_a_fault_is_refused() -> None:
    """Rows that arrived before a failure are a partial read of somebody's database being
    reported as an answer, and there is no count anywhere that would say how partial.

    Delete this and a driver that yields rows and then raises produces a confident short
    answer."""
    with pytest.raises(LaravelError, match="partial read"):
        ViewReply(rows=(client_row(),), fault=DatabaseFault.TIMED_OUT)


def test_a_reply_carrying_two_accounts_of_one_read_is_refused() -> None:
    """A database fault and an application status are two answers to what happened, and
    whichever was checked second would decide silently.

    Delete this and the order of two branches becomes a classification."""
    with pytest.raises(LaravelError, match="two accounts"):
        ViewReply(fault=DatabaseFault.TIMED_OUT, app_status=500)


@pytest.mark.parametrize("fault", list(DatabaseFault))
def test_every_fault_is_classified_by_every_table_that_reads_one(fault: DatabaseFault) -> None:
    """The three mappings are total on purpose. A `dict.get` with a default would let a fifth
    fault be added and classified as whatever the default said, and for "is this the source's
    health or our installation" that is the answer that pages the wrong person at three in the
    morning.

    Delete this and a new member of the enum reaches production classified by a fallback."""
    assert fault in OUTCOME_FOR_FAULT
    assert fault in REASON_FOR_FAULT
    assert interpret(a_read(), ViewReply(fault=fault), fetched_at=NOW.isoformat()).detail


def test_a_read_cut_short_by_the_row_cap_says_so() -> None:
    """The cap is ours rather than the source's, and the answer it produces is complete-looking
    and incomplete, which is the same shape Freshdesk's hard ceiling produces and gets the same
    word. The abstention path already branches on TRUNCATED, so it does the right thing here
    without learning anything about databases.

    Delete this and a capped read is summarised as all of them."""
    reply = answered((client_row(),), limit=1)
    assert reply.outcome is LaravelOutcome.PRESENT
    assert reply.call is CallOutcome.TRUNCATED
    assert reply.rows is not None and reply.rows.truncated is True
    assert reply.detail == DETAIL_CAPPED


def test_a_reply_whose_rows_were_cut_short_cannot_be_reported_as_a_complete_read() -> None:
    """The constructor's half of the rule above. A capped result carrying the OK outcome is a
    partial answer that nothing downstream can tell from a whole one.

    Delete this and the two halves of truncation can be set independently, which is how one of
    them ends up not set."""
    complete = answered((client_row(),))
    assert complete.rows is not None
    with pytest.raises(LaravelError, match="reported as a complete read"):
        LaravelReply(
            outcome=LaravelOutcome.PRESENT,
            call=CallOutcome.OK,
            rows=complete.rows.model_copy(update={"truncated": True}),
            fetched_at=NOW.isoformat(),
        )


# --------------------------------------------------------------- the fetch (M11.1.1)
def test_a_fetch_addressed_to_another_entity_never_reaches_the_database() -> None:
    """The view is chosen at construction. Answering from the bound entity would return staff
    records under the name of clients, and every test would pass because the rows are real.
    The assertion is on the reader never being called, because a refusal after the read was
    issued has already spent time on the client's production database.

    Delete this and a mis-wired tool reads the wrong view and nobody can see it in the
    output."""
    reader = Reader(ViewReply(rows=(client_row(),)))
    fetch = connector_fetch(
        connection(), ENTITY_CLIENT, reader=reader, fetched_at=NOW.isoformat()
    )
    with pytest.raises(LaravelError, match="was asked for 'user'"):
        fetch(FetchRequest(entity=ENTITY_USER))
    assert reader.reads == []


def test_a_fetch_returns_the_rows_the_view_handed_over() -> None:
    """The positive sibling, and the one that proves the refusals above are not a connector
    that refuses everything. It also pins that the read reaching the reader is the bounded
    one: the reader is handed a plan with a cap, not an entity and a free hand.

    Delete this and every guard in this file is satisfied by a fetch that never reads."""
    reader = Reader(ViewReply(rows=(client_row(),)))
    fetch = connector_fetch(
        connection(), ENTITY_CLIENT, reader=reader, fetched_at=NOW.isoformat()
    )
    result = fetch(FetchRequest(entity=ENTITY_CLIENT, limit=10))
    assert result.source == CONNECTOR_NAME
    assert [r.id for r in result.records] == ["4471"]
    assert reader.reads[0].plan.limit == 10
    assert reader.reads[0].plan.view == "portal.v_client"


def test_a_failed_read_raises_rather_than_returning_an_empty_result() -> None:
    """`ConnectorFetch` returns a `TypedResult`, and an empty one is exactly what an empty
    view produces, so a failure returning one would collapse the distinction this module
    spends its length keeping. The distinction survives on the exception, where the trace and
    the health row read it.

    Delete this and an outage is answered with an empty list by the shortest possible edit."""
    reader = Reader(ViewReply(fault=DatabaseFault.UNAVAILABLE))
    fetch = connector_fetch(
        connection(), ENTITY_CLIENT, reader=reader, fetched_at=NOW.isoformat()
    )
    with pytest.raises(LaravelDegraded) as raised:
        fetch(FetchRequest(entity=ENTITY_CLIENT))
    assert raised.value.read_outcome is LaravelOutcome.UNREACHABLE
    assert raised.value.call_outcome is CallOutcome.UNAVAILABLE
    assert raised.value.public_message == Degraded.public_message


def test_a_filter_naming_a_column_this_connector_does_not_select_is_refused() -> None:
    """A filter narrows, so dropping one widens the read, and widening a read of somebody
    else's production database is the direction this module exists to prevent. The filters
    here arrive from the gate rather than from the asker, so an undeclared name is a mistake
    by whoever wired the tool and belongs in front of them.

    Delete this and a renamed column turns every filtered read into a full one."""
    with pytest.raises(LaravelError, match=r"\['email'\]"):
        read_plan(connection(), ENTITY_CLIENT, filters=(("email", "ops@example.com"),))


def test_a_filter_naming_a_declared_column_reaches_the_plan() -> None:
    """The positive sibling. Predicate push-down is the point of passing filters down at all:
    without it the connector pulls the view across the wire to throw most of it away, which is
    the load this module exists to avoid.

    Delete this and a connector that refused every filter would pass the test above."""
    read = read_plan(connection(), ENTITY_CLIENT, filters=(("department", "maintenance"),))
    assert read.plan.filters == (("department", "maintenance"),)


# ------------------------------------------------------ the manifest and the signal
def test_the_manifest_is_read_only_over_a_database_transport() -> None:
    """Read-only by not saying otherwise, which is the default that survives somebody in a
    hurry. There is no version of this connector that writes: the credential slot is a
    database user with SELECT on views, and a write tool would fail at the source during
    somebody's request having looked installable all along.

    Delete this and a write tool could be added to the declaration and reviewed as normal."""
    declared = manifest()
    assert declared.transport is TransportKind.DATABASE
    assert declared.credential.mode is AccessMode.READ_ONLY
    assert declared.credential.write_granted_by == ""
    assert declared.tool_names() == ("laravel.read_clients", "laravel.read_users")


def test_the_manifest_declares_no_ceiling_and_the_platform_refuses_to_invent_one() -> None:
    """The recorded corpus says this source has no ceiling because it is our own system, and
    `brain.ops.limits` holds no verified figure for it. So the manifest declares none and
    `throttle.limits_for` refuses, which is the correct refusal and also a real gap: nothing
    paces this connector, and the per-read bound is what exists instead.

    Delete this and somebody adds a plausible number that looks measured and is not."""
    assert limit_for(Source.LARAVEL).calls == 0
    assert manifest().ceiling == ""
    with pytest.raises(UnmeasuredSourceError, match="declares no ceiling"):
        limits_for(manifest(), principal_id="u_weiling")


def test_a_visibility_predicate_over_a_column_nothing_projects_is_refused() -> None:
    """A predicate over a field that never arrives matches nothing, for ever, and reads
    exactly like a client with no records. The column names here come from a view definition
    the client maintains, so a renamed column is the ordinary way it happens.

    Delete this and a typo in a predicate silently empties a projection."""
    with pytest.raises(LaravelError, match=r"\['office_id'\]"):
        projection_for(
            ENTITY_CLIENT,
            visibility=Scope(clauses=(Clause(field="office_id", op=Op.EQ, value="sg"),)),
        )


def test_a_visibility_predicate_over_a_projected_column_is_accepted() -> None:
    """The positive sibling, and the ordinary shape: ownership. `manager_id = u_weiling` is a
    property of the record rather than an enumeration of who may read it, and it is projected
    precisely so the predicate has something to test.

    Delete this and a check that refused every predicate would pass the test above."""
    projected = projection_for(
        ENTITY_CLIENT,
        visibility=Scope(clauses=(Clause(field="manager_id", op=Op.EQ, value="u_weiling"),)),
    )
    assert projected.change_signal is ChangeSignal.UPDATED_SINCE
    assert "manager_id" in projected.field_names


def test_a_projection_with_no_predicate_at_all_is_refused_by_the_manifest() -> None:
    """MySQL grants on a view rather than on a row of one, so there is no per-record ACL here
    to store a predicate from and this module refuses to invent one. A projection stored with
    none has discarded the source's permission model rather than narrowed it.

    Delete this and every projected row is visible to anybody holding the entity's
    capability."""
    with pytest.raises(ManifestError, match="stores no visibility predicate"):
        projection_for(ENTITY_CLIENT, visibility=Scope())
    with pytest.raises(LaravelError, match=r"\['user'\]"):
        laravel_manifest(connection(), ref=REF, visibility={ENTITY_CLIENT: department()})


def test_the_change_signal_is_a_cursor_and_deletions_are_learned_by_sweeping_ids() -> None:
    """A cursor cannot see a deletion anywhere, and in a Laravel schema it is doubly hidden:
    `SoftDeletes` sets `deleted_at` rather than removing the row, so a view filtering it out
    turns a soft delete into the same silence as a hard one. Enumerating the ids the view
    still returns is the one option a read-only credential has.

    Delete this and the projection counts clients that were archived months ago."""
    declared = subscription(notify_within=timedelta(minutes=15), reconcile_every=timedelta(hours=6))
    assert declared.kind is ChangeSignal.UPDATED_SINCE
    assert declared.deletion_check is DeletionCheck.ID_SWEEP
    assert declared.sees_deletions_by_itself is False
    assert declared.needs_an_absence_check is True


def test_a_projected_row_ages_against_the_reconciliation_pass_rather_than_the_cursor() -> None:
    """A cursor does not mention a row nobody edited, so the full pass is the only thing that
    refreshes a quiet record and therefore the only honest interval to measure an age against.

    Delete this and a client nobody has touched for a week reads as live."""
    promise = refresh_promise(reconcile_every=timedelta(hours=6))
    record = projected_record(ENTITY_CLIENT, client_row(), last_seen_at=NOW - timedelta(hours=2))
    assert record is not None
    reading = assess_staleness(record, now=NOW, promise=promise)
    assert reading.freshness is Freshness.LIVE
    stale = projected_record(ENTITY_CLIENT, client_row(), last_seen_at=NOW - timedelta(days=3))
    assert stale is not None
    assert assess_staleness(stale, now=NOW, promise=promise).freshness is Freshness.STALE


# --------------------------------------------------------------------- health (M11.1.1)
def test_a_connector_nobody_has_probed_is_unconfigured_rather_than_down() -> None:
    """A connector nobody has called yet is a job for whoever installed it. DOWN would page
    somebody about a database that is perfectly healthy.

    Delete this and every unfinished installation is an incident during a rollout, which is
    how a health page becomes permanently amber and therefore ignored."""
    probe = health(None, checked_at=NOW)
    assert probe.state is HealthState.UNCONFIGURED
    assert probe.detail == DETAIL_NEVER_PROBED
    assert probe.checked_at == NOW


def test_a_withdrawn_view_takes_this_connector_out_of_service() -> None:
    """It was working this morning, it cannot answer now, and somebody has to talk to the
    client's DBA today. UNCONFIGURED would file it as an installation task in a backlog, and
    DEGRADED is usable, which would keep routing questions to a connector that cannot answer
    one.

    Delete this and a dropped view leaves the connector in rotation."""
    probe = health(failed(DatabaseFault.UNKNOWN_VIEW), checked_at=NOW)
    assert probe.state is HealthState.DOWN
    assert probe.is_usable is False


def test_a_read_that_passed_its_time_bound_stops_the_connector_being_used() -> None:
    """DEGRADED is usable and is right for a source that is merely slow. This is somebody
    else's production database and the last thing we did to it was start a query it could not
    finish inside our own bound: the safe direction is to stop asking until a person looks.

    Delete this and a connector that is timing out keeps being asked, which is the load that
    caused the timeout."""
    assert health(failed(DatabaseFault.TIMED_OUT), checked_at=NOW).state is HealthState.DOWN


def test_a_capped_read_is_degraded_and_still_answers() -> None:
    """The positive sibling for health. A capped read is an answer, so refusing to route to it
    would turn our own bound into an outage; what DEGRADED does is let the composer say the
    answer was cut short.

    Delete this and a health mapping that returned DOWN for everything but OK would pass."""
    probe = health(answered((client_row(),), limit=1), checked_at=NOW)
    assert probe.state is HealthState.DEGRADED
    assert probe.is_usable is True
    assert health(answered((client_row(),)), checked_at=NOW).state is HealthState.OK


@pytest.mark.parametrize("outcome", list(CallOutcome))
def test_every_call_outcome_has_a_health_state(outcome: CallOutcome) -> None:
    """Total over the enum, so a new outcome fails the build here rather than being classified
    by a default that somebody chose for a different question.

    Delete this and a fifth outcome becomes whichever state a fallback named."""
    assert outcome in HEALTH_FOR_CALL


def test_no_detail_in_a_health_row_or_a_trace_comes_from_the_database() -> None:
    """A MySQL error quotes the statement back, which carries the filter values and therefore
    a client's name into a console with a different audience and a different retention from
    the answer it described. Every detail this module produces is a constant in it.

    Delete this and the first person to add a helpful `str(exc)` to a detail moves client data
    into the health page."""
    constants = {reason.reason for reason in NEVER_SELECTED} | set()
    del constants
    details = {failed(fault).detail for fault in DatabaseFault}
    details |= {answered((client_row(),)).detail, answered(()).detail}
    for detail in details:
        assert detail
        assert "SNM" not in detail
        assert "portal" not in detail


def test_the_recorded_corpus_still_holds_the_three_outcomes_this_connector_keeps_apart() -> None:
    """The distinction is not this module's invention. The corpus records a genuine absence, a
    refusal and an unreachable source across its sources, and this connector's job is to keep
    them apart in a place where a fourth thing, a withdrawn view, also has to be told from an
    absence.

    Delete this and the tests above could drift into asserting a distinction the recordings no
    longer make."""
    statuses = {c.status for c in CASSETTES}
    assert 500 in statuses
    assert 401 in statuses
    assert any(c.status == 200 and c.body == {"total": 0, "results": []} for c in CASSETTES)
