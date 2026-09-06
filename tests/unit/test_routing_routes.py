"""The routing matrix over HTTP: who may read it, who may change it, and what may change.

Driven through the real application. The token machinery, the identity directory and the
key source are imported from `tests/unit/test_api_routes.py` rather than rebuilt, because
what is under test here is a capability and not an identity: the same six people sign in the
same way, and only what they hold over the matrix differs. A second copy of a JWS builder
would be a second place for a token to be minted subtly differently, and a test that failed
for that reason would look like a permission bug.

**The database is a stub session and not a double for the matrix.** This repository has no
PostgreSQL, so `async_sessionmaker(class_=...)` supplies a session whose `execute` returns a
canned result. That is honest about what it proves and what it does not: the route's
arithmetic, its refusals and the statements it compiles are all exercised, and the SQL is
never run, so nothing here says the WHERE clause matches what PostgreSQL would match.
`live_rungs` and `apply_edit` are therefore also asserted as compiled statements, which is
the part a stub cannot reach.

**The order of the two checks is the property most of this file exists for.** A capability
checked after the database is looked at makes an unentitled caller's answer depend on
whether the process has a pool, which publishes the deployment's state to anybody who can
reach the port.

Task ids: M5.3.3
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Numeric, SmallInteger, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.api import API_PREFIX
from brain.app import Settings, create_app
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.errors import Absent
from brain.core.scope import Clause, Op, Scope
from brain.routing_routes import (
    DEFAULT_RUNGS_PER_PAGE,
    MATRIX_READ,
    MATRIX_WRITE,
    MAX_RUNGS_PER_PAGE,
    MAX_TIMEOUT_SECONDS,
    MIN_ATTEMPTS,
    MIN_CONCURRENCY,
    SMALLINT_MAX,
    RungEdit,
    apply_edit,
    live_rungs,
)
from brain.tables.routing import RoutingRungRow
from tests.unit.test_api_routes import (
    Directory,
    Keys,
    NoCache,
    Versions,
    token_for,
    verifier,
)

RUNGS_PATH = f"{API_PREFIX}/routing/rungs"

#: The four columns a console may change. Written out because it is the claim under test;
#: every assertion that uses it also checks it against `ops.routing_rung` itself, so this is
#: a subject rather than an oracle.
EDITABLE = ("attempts", "timeout_seconds", "max_concurrency", "enabled")

WHOLE = Scope.unrestricted()
ELSEWHERE = Scope(clauses=(Clause(field="department", op=Op.EQ, value="finance"),))


def _grant(value: str, scope: Scope) -> Grant:
    return Grant(capability=Capability(value=value), scope=scope)


#: What each of `test_api_routes`' people holds over the matrix. Deliberately unrelated to
#: what they hold over a price list: the point of reusing them is the token, not the grants.
#:
#: `u_prefix` holds the read capability in a scope no rung mentions, which is how the
#: whole-collection rule below is checked rather than asserted. A route that narrowed the
#: matrix by the caller's scope would answer them an empty page.
MATRIX_GRANTS: dict[str, tuple[Grant, ...]] = {
    "u_none": (),
    "u_narrow": (_grant(MATRIX_READ.value, WHOLE),),
    "u_prefix": (_grant(MATRIX_READ.value, ELSEWHERE),),
    "u_wide": (_grant(MATRIX_READ.value, WHOLE),),
    "u_admin": (_grant(MATRIX_READ.value, WHOLE), _grant(MATRIX_WRITE.value, WHOLE)),
    "u_elsewhere": (_grant(MATRIX_WRITE.value, WHOLE),),
}

#: An `amr` a Keycloak session carries when a second factor was used. A literal rather than a
#: value read out of `SECOND_FACTOR_METHODS`, for the reason
#: `test_a_password_only_session_cannot_exercise_an_admin_capability` gives about its own:
#: reading the constant would compare it against itself and stay green for any set at all.
SECOND_FACTOR: Mapping[str, object] = {"amr": ["otp"]}


class MatrixStore:
    """A `brain.gate.resolve.EntitlementStore` over `MATRIX_GRANTS`."""

    def load(self, principal_id: str) -> EntitlementSet:
        return EntitlementSet(principal_id=principal_id, grants=MATRIX_GRANTS[principal_id])


def rung(
    *,
    position: int = 0,
    tier: str = "main",
    role: str = "primary",
    attempts: int = 1,
    timeout_seconds: float = 12.0,
    max_concurrency: int = 40,
    enabled: bool = True,
) -> RoutingRungRow:
    """One row of the matrix, built in memory.

    A real `RoutingRungRow` rather than a dictionary, so the view below is exercised against
    the mapped attributes the route will actually read. Nothing is added to a session.
    """
    row = RoutingRungRow(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{tier}/{position}"),
        tier=tier,
        scope={"clauses": []},
        position=position,
        role=role,
        deployment_id=f"anthropic-{tier}-{position}",
        provider="anthropic",
        model="claude-sonnet-5",
        attempts=attempts,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        enabled=enabled,
    )
    row.deleted_at = None
    return row


class StubResult:
    """What `AsyncSession.execute` hands back, in the two shapes the routes read."""

    def __init__(self, rows: Sequence[RoutingRungRow]) -> None:
        self._rows = tuple(rows)

    def scalars(self) -> StubResult:
        return self

    def all(self) -> tuple[RoutingRungRow, ...]:
        return self._rows

    def scalar_one_or_none(self) -> RoutingRungRow | None:
        return self._rows[0] if self._rows else None


class Executed:
    """Every statement the route asked the session to run, and what it was answered."""

    def __init__(self, rows: Sequence[RoutingRungRow]) -> None:
        self.rows = tuple(rows)
        self.statements: list[Any] = []
        self.committed = 0
        self.rolled_back = 0


#: The recorder for the session below. A module-level handle rather than an argument, because
#: `async_sessionmaker` constructs the session itself and there is nowhere to pass one.
_EXECUTED = Executed(())


class StubSession(AsyncSession):
    """An `AsyncSession` that runs nothing and records everything.

    Subclassed rather than faked, so `sessions_of`'s `isinstance` check is satisfied by the
    real factory type. That check is not test scaffolding: it is what stops a bare test
    application answering 500 from an `AttributeError` that reads like a bug in the gate.
    """

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        _EXECUTED.statements.append(statement)
        return StubResult(_EXECUTED.rows)

    async def commit(self) -> None:
        _EXECUTED.committed += 1

    async def rollback(self) -> None:
        _EXECUTED.rolled_back += 1

    async def close(self) -> None:
        return None


@pytest.fixture
def executed() -> Iterator[Executed]:
    """A fresh recorder per test, so one test's statements are never another's evidence."""
    global _EXECUTED
    _EXECUTED = Executed((rung(position=0), rung(position=1, role="same_provider_failover")))
    yield _EXECUTED


@pytest.fixture
def client(executed: Executed) -> Iterator[TestClient]:
    """The real application, with a stub session factory where the pool would be.

    `create_app` produced everything else, including the router registration under test: a
    test that mounted the router itself would prove the routes work and not that they are
    served.
    """
    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = _wiring()
        app.state.db_sessions = async_sessionmaker(class_=StubSession)
        yield c


@pytest.fixture
def unwired() -> Iterator[TestClient]:
    """The same application with no session factory, which is every deployment today."""
    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = _wiring()
        app.state.db_sessions = None
        yield c


def _wiring() -> Any:
    from brain.api_routes import GateWiring
    from brain.identity.bearer import TokenAuthority

    return GateWiring(
        authority=TokenAuthority(
            issuer="https://id.verz.example/realms/brain",
            audience="brain-api",
            keys=Keys(),
            verify=verifier,
            directory=Directory(),
        ),
        versions=Versions(),
        store=MatrixStore(),
        cache=NoCache(),
    )


def read(c: TestClient, pid: str, *, limit: int | None = None) -> Response:
    token = token_for(pid, claims=SECOND_FACTOR)
    response: Response = c.get(
        RUNGS_PATH,
        headers={"authorization": f"Bearer {token}"},
        params={} if limit is None else {"limit": limit},
    )
    return response


def write(
    c: TestClient,
    pid: str,
    *,
    body: Mapping[str, object] | None = None,
    rung_id: str | None = None,
    claims: Mapping[str, object] | None = None,
) -> Response:
    token = token_for(pid, claims=SECOND_FACTOR if claims is None else claims)
    edit: Mapping[str, object] = body or {
        "attempts": 2,
        "timeout_seconds": 15.0,
        "max_concurrency": 8,
        "enabled": True,
    }
    response: Response = c.patch(
        f"{RUNGS_PATH}/{rung_id or uuid.uuid4()}",
        headers={"authorization": f"Bearer {token}"},
        json=dict(edit),
    )
    return response


def columns() -> set[str]:
    return {column.name for column in RoutingRungRow.__table__.columns}


# ------------------------------------------------------------------- who may read it
def test_a_caller_holding_no_grant_over_the_matrix_is_told_it_is_not_there(
    client: TestClient,
) -> None:
    """The taxonomy's own sentence, and nothing about routing in it.

    A refusal that said "you may not change the routing matrix" would confirm there is one,
    which is a fact about the estate rather than about the caller. It is the same answer
    `brain.api_routes` gives an entity nothing classifies, and for the same reason one level
    across.

    Delete this and the route can grow a helpful message, or answer 403, either of which
    tells an unentitled caller that the matrix exists and is worth asking about again."""
    response = read(client, "u_none")

    assert response.status_code == 404
    assert response.json()["message"] == Absent.public_message
    assert "routing" not in response.text.lower()


def test_a_caller_holding_the_read_grant_is_answered_the_matrix(client: TestClient) -> None:
    """The sibling of the refusal above, which a route that refused everybody would pass.

    Delete this and every assertion about a refusal in this file is satisfied by a route
    that answers 404 to the entitled caller too."""
    response = read(client, "u_narrow")

    assert response.status_code == 200
    assert [item["position"] for item in response.json()["items"]] == [0, 1]


def test_a_caller_with_no_grant_cannot_tell_whether_this_process_has_a_database(
    unwired: TestClient,
) -> None:
    """The capability is checked before the wiring, so the two answers differ only for
    somebody already entitled to know.

    This is the ordering property, driven against an application with no session factory,
    which is what every deployment of this system is today. An unentitled caller sees the
    refusal they would see anywhere; the entitled one sees the process fault. Swap the two
    checks in `rungs` and the unentitled caller receives a 500, which says the matrix would
    have been answered if only the database were up.

    Delete this and the order of two lines becomes a matter of taste, and the taste that
    reads better is the wrong one: checking the wiring first is the natural way to write a
    guard clause."""
    assert read(unwired, "u_none").status_code == 404
    assert read(unwired, "u_narrow").status_code == 500


def test_every_reader_of_the_matrix_is_answered_every_live_rung(client: TestClient) -> None:
    """No per-caller row filtering, which is what makes the absence of a count safe here.

    `u_prefix` holds the read capability in a scope naming a department, and `u_wide` holds
    it unrestricted. If the route narrowed the matrix by the caller's scope, these two would
    be answered differently and a page would then be a filtered list with a page size beside
    it, which is the subtraction `paging.ts` and `brain.core.redaction` both refuse.

    Delete this and somebody adds a scope predicate to the statement, reasonably, and the
    console's footer becomes a disclosure without a line of the console changing."""
    narrow = read(client, "u_prefix").json()
    wide = read(client, "u_wide").json()

    assert narrow["items"] == wide["items"]
    assert len(wide["items"]) == 2


# ------------------------------------------------------------------ what it answers
def test_a_page_of_the_matrix_carries_no_count_of_anything(client: TestClient) -> None:
    """`total` is inherited from `brain.api.Page` and never populated.

    Asserted over every key in the body rather than on `total` alone, because the failure is
    a number arriving under a different name: `count`, `matches`, `rungs`. The keys are
    compared against the response model's own fields, so a field added to the model is
    checked here rather than waved through by a list written in this file.

    Delete this and the first person to want a footer adds `total=len(found)`, which is one
    word and is the leak `A_PAGE_NEVER_CARRIES_A_COUNT` describes."""
    body = read(client, "u_narrow").json()

    assert body["total"] is None
    # A bool is an int in Python, and `truncated` and `editable` are the two flags this page
    # carries on purpose. Excluded by type rather than by name, so a field added as a number
    # is caught and a field added as a flag is not mistaken for one.
    numbers = {
        key for key, value in body.items() if isinstance(value, int) and not isinstance(value, bool)
    }
    assert numbers == set(), f"a page carries a number of its own: {sorted(numbers)}"


def test_a_full_page_says_there_is_more_without_saying_how_much(client: TestClient) -> None:
    """`truncated` is the page having come back full, and it is a boolean.

    Both directions, because a flag that is always true and a flag that is always false each
    satisfy half of this. The stub answers two rows whatever the statement asked for, so what
    varies is the limit the caller sent and nothing else.

    Delete this and `truncated` can be computed as `len(found) > limit`, which is false for
    every page this route can produce, and a person reading the first hundred rungs of a
    larger matrix is told there is nothing more."""
    assert read(client, "u_narrow", limit=2).json()["truncated"] is True
    assert read(client, "u_narrow", limit=3).json()["truncated"] is False


def test_the_page_says_whether_this_caller_may_change_it(client: TestClient) -> None:
    """`editable` is the caller's own fact about this collection, recomputed per request.

    Both values are asserted. A flag that is always false hides the editor from everybody,
    which reads as a broken screen rather than as a permission, and a flag that is always
    true is the one that matters: it would put an editor in front of somebody every one of
    whose saves is refused.

    Delete this and the flag can be hard-coded either way, and neither the console nor any
    other test here would notice, because the PATCH's own check is separate and correct."""
    assert read(client, "u_narrow").json()["editable"] is False
    assert read(client, "u_admin").json()["editable"] is True


def test_a_rung_is_answered_with_its_role_and_the_edit_cannot_carry_one(
    client: TestClient,
) -> None:
    """The role is readable and not writable, which is the whole of what M5.3.2 asks of a
    console.

    Both halves, because either alone is satisfied by the wrong shape: a view without the
    role leaves the screen unable to show a primary sitting third, and an edit that accepts
    one lets a browser write the label a trigger is supposed to derive.

    Delete this and `role` migrates onto `RungEdit` the first time somebody wants to correct
    one by hand, which is exactly the correction the derivation exists to make impossible."""
    item = read(client, "u_narrow").json()["items"][0]

    assert item["role"] == "primary"
    assert "role" not in RungEdit.model_fields


# ----------------------------------------------------------------- who may change it
def test_a_caller_who_may_read_the_matrix_and_not_change_it_is_refused_in_the_same_words(
    client: TestClient,
) -> None:
    """One refusal for both halves, so the reply says nothing about which one is missing.

    Compared body against body rather than status against status, because two 404s with
    different sentences are two answers, and the difference between "no such rung" and "not
    yours" is the whole thing this system spends itself hiding.

    **The comparison crosses the two routes, and the first version of this test did not.** It
    compared two writers with each other, and both of them leave through the same branch, so a
    branch given its own wording produced two identical new sentences and the assertion held.
    A mutation adding "You may not change the routing matrix." to the PATCH survived it. What
    the property actually says is that the matrix has one refusal, whichever route is asked and
    whichever half of the capability is missing, so the reader's refusal is the other side of
    the comparison and the taxonomy's own sentence is the third.

    Delete this and the PATCH grows its own message, which is where the difference between a
    reader and an editor becomes readable off a response."""
    refused = write(client, "u_narrow")
    stranger = write(client, "u_none")
    reader = read(client, "u_none")

    assert refused.status_code == 404
    assert refused.json()["message"] == stranger.json()["message"]
    assert refused.json()["message"] == reader.json()["message"]
    assert refused.json()["message"] == Absent.public_message
    assert _EXECUTED.statements == [], "a refused edit reached the database"


def test_an_editor_signed_in_with_one_factor_cannot_exercise_the_write(
    client: TestClient,
) -> None:
    """The assurance ceiling reaches the matrix, because the capability carries the admin
    verb.

    `gate.admission` withholds `admin` from an AUTHENTICATED caller, so a password-only
    session holding `admin:routing_matrix` holds nothing exercisable. That is not something
    this module implements; it is something this module must not route around, and the way
    it would be routed around is a capability spelled `write:` for a screen that retunes the
    estate.

    Delete this and the verb can be changed to one every ordinary sign-in carries, which
    reads in review as making the capability match what the screen does."""
    weak = write(client, "u_admin", claims={"amr": ["pwd"]})
    strong = write(client, "u_admin")

    assert weak.status_code == 404
    assert strong.status_code == 200


def test_an_edit_that_matches_no_live_rung_is_the_same_refusal_again(
    client: TestClient, executed: Executed
) -> None:
    """A retired rung and a rung that never existed are one answer.

    The stub is emptied so `scalar_one_or_none` answers None, which is what the UPDATE
    returns for a row that is absent and for one whose `deleted_at` is set. Reporting those
    differently would let somebody holding a retired rung's id learn that it used to exist.

    Delete this and the missing-row branch can answer 200 with a null body, or 500, and the
    console renders an empty form for a rung nobody has."""
    executed.rows = ()

    response = write(client, "u_admin")

    assert response.status_code == 404
    assert response.json()["message"] == Absent.public_message
    assert executed.committed == 0, "a refused edit committed a transaction"
    assert executed.rolled_back == 1, "a refused edit left its transaction open"


def test_an_accepted_edit_answers_the_row_the_database_holds(
    client: TestClient, executed: Executed
) -> None:
    """The response is the row the statement returned, not the body that was sent.

    The stub answers a rung with one attempt and a twelve-second timeout whatever the UPDATE
    said, so a route echoing its own request would answer the two the request carried. That
    is the case M5.3.2 creates: a trigger changes a column on write, and a console showing
    what it sent would report a value the database does not hold.

    Delete this and the route can build its response from `edit`, which is one line shorter
    and is wrong the day anything derives a column."""
    response = write(
        client,
        "u_admin",
        body={
            "attempts": 3,
            "timeout_seconds": 30.0,
            "max_concurrency": 9,
            "enabled": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["attempts"] == 1
    assert response.json()["timeout_seconds"] == 12.0
    assert executed.committed == 1


def test_a_body_naming_a_column_the_edit_does_not_carry_is_refused_rather_than_ignored(
    client: TestClient,
) -> None:
    """`extra="forbid"`, so a console that sent a role is told rather than obeyed in part.

    Every one of the refused keys is asserted, because they fail for different reasons and a
    model that dropped one of them would still refuse the others. A body that was accepted
    with the extra key silently dropped is the worst of the three outcomes: the console
    believes it moved a rung, the matrix did not move, and the response is a 200.

    Delete this and the model can be relaxed to `ignore`, which is what somebody does the
    first time a console sends a field the API has not caught up with."""
    for column in ("role", "tier", "position", "deployment_id", "provider", "model", "scope"):
        body = {
            "attempts": 1,
            "timeout_seconds": 12.0,
            "max_concurrency": 8,
            "enabled": True,
            column: "anything",
        }
        response = write(client, "u_admin", body=body)
        assert response.status_code == 422, f"{column} was accepted"
        assert column in response.text, f"{column} was refused without being named"


def test_the_editable_columns_are_the_operational_ones_and_the_rest_of_the_row_is_not(
    client: TestClient,
) -> None:
    """What may change, stated against `ops.routing_rung` rather than against a list here.

    The editable set is compared with the table's own columns, so a column added to the
    matrix is either deliberately editable or deliberately not, and either way somebody has
    to come here. The excluded names are asserted individually because each is excluded for
    its own reason and a set comparison alone would pass if the whole row became editable
    and the table grew nothing.

    Delete this and the four dials quietly become five, most likely `role`."""
    editable = set(RungEdit.model_fields)

    assert editable == set(EDITABLE)
    assert editable < columns(), "the edit names a column the matrix does not have"
    assert columns() - editable >= {
        "id",
        "role",
        "tier",
        "position",
        "scope",
        "deployment_id",
        "provider",
        "model",
        "deleted_at",
    }


# ------------------------------------------------------------------ the statements
def test_the_matrix_is_read_live_and_in_chain_order(client: TestClient) -> None:
    """The SELECT filters retired rows and orders by the chain, in the statement rather than
    in the policy alone.

    Compiled and read as SQL, because a stub session runs nothing and could answer any
    statement at all. The row-level policy also excludes retired rows; the reason for both is
    that a statement whose correctness depends on a policy is wrong on a database restored
    without one.

    Delete this and the ORDER BY can go, at which point the order of a chain is whatever the
    planner returns and the console renders a fallback above its primary."""
    sql = str(live_rungs(DEFAULT_RUNGS_PER_PAGE).compile(compile_kwargs={"literal_binds": True}))

    assert "deleted_at IS NULL" in sql
    assert "ORDER BY ops.routing_rung.tier, ops.routing_rung.position" in sql
    assert f"LIMIT {DEFAULT_RUNGS_PER_PAGE}" in sql


def test_the_update_touches_only_the_columns_the_edit_carries(client: TestClient) -> None:
    """The SET list is the four dials and nothing else, and the WHERE clause keeps a retired
    rung retired.

    Read off the compiled statement's own parameters rather than off `RungEdit`, so this is
    the statement being checked against the model instead of the model against itself.

    Delete this and `role` can be added to `values()` while `RungEdit` still refuses it,
    which is the shape that looks safest: the API refuses the key and writes the column
    anyway, from something the route computed."""
    edit = RungEdit(attempts=2, timeout_seconds=15.0, max_concurrency=8, enabled=True)
    statement = apply_edit(uuid.uuid4(), edit)
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert set(statement.compile().binds) >= set(EDITABLE)
    assert "role" not in sql.split("WHERE")[0]
    assert "deleted_at IS NULL" in sql


# ---------------------------------------------------------------------- the bounds
def test_the_declared_bounds_are_the_columns_own_bounds() -> None:
    """Every number the route refuses on is read off `ops.routing_rung`, not typed twice.

    `MAX_TIMEOUT_SECONDS` is derived from the column's precision and scale, so this compares
    the derivation with the column rather than with itself; the two minimums are compared
    with the check constraints that enforce them. A bound copied from memory is the one that
    offers a person a number the database then refuses, which arrives as a 500 rather than as
    a message naming the field.

    Delete this and `Numeric(6, 2)` can be widened with the route still refusing at 9999.99,
    or narrowed with the route accepting a value that overflows."""
    # `__table__` is typed as a `FromClause`, which has no `constraints`. A cast rather than
    # a runtime check, because a declarative model's `__table__` is a `Table` by construction
    # and proving it here would buy nothing.
    table = cast(Table, RoutingRungRow.__table__)
    timeout = table.c.timeout_seconds.type
    assert isinstance(timeout, Numeric)
    assert (timeout.precision, timeout.scale) == (6, 2)
    assert MAX_TIMEOUT_SECONDS == 9999.99

    for name in ("attempts", "max_concurrency", "position"):
        assert isinstance(table.c[name].type, SmallInteger)
    assert SMALLINT_MAX == 32767

    # Keyed on the constraint's own generated name, which `brain.db`'s naming convention
    # prefixes with `ck_<table>_`. A `KeyError` here is a constraint that was renamed or
    # removed, which is louder than a lookup that quietly found nothing to compare.
    checks = {c.name: str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")}
    assert checks["ck_routing_rung_at_least_one_attempt"] == f"attempts >= {MIN_ATTEMPTS}"
    assert (
        checks["ck_routing_rung_concurrency_at_least_one"]
        == f"max_concurrency >= {MIN_CONCURRENCY}"
    )


def test_a_value_the_column_cannot_hold_is_refused_before_the_database_sees_it(
    client: TestClient,
) -> None:
    """A timeout above the column's ceiling, and an attempt count below the constraint, are
    422s naming the field.

    The sibling case is the important half: the largest value the column *can* hold is
    accepted, so this is a bound rather than a smaller limit somebody guessed at. Without it
    a route that refused every timeout would pass.

    Delete this and an over-large timeout reaches PostgreSQL, which answers a numeric
    overflow that arrives as `Failed` and reads to a person as "Something went wrong.\""""
    over = write(
        client,
        "u_admin",
        body={
            "attempts": 1,
            "timeout_seconds": MAX_TIMEOUT_SECONDS + 1,
            "max_concurrency": 1,
            "enabled": True,
        },
    )
    zero = write(
        client,
        "u_admin",
        body={
            "attempts": 0,
            "timeout_seconds": 1.0,
            "max_concurrency": 1,
            "enabled": True,
        },
    )
    at_the_ceiling = write(
        client,
        "u_admin",
        body={
            "attempts": 1,
            "timeout_seconds": MAX_TIMEOUT_SECONDS,
            "max_concurrency": SMALLINT_MAX,
            "enabled": True,
        },
    )

    assert over.status_code == 422
    assert "timeout_seconds" in over.text
    assert zero.status_code == 422
    assert "attempts" in zero.text
    assert at_the_ceiling.status_code == 200


def test_a_page_larger_than_the_route_admits_is_refused(client: TestClient) -> None:
    """The page size is bounded, so one request cannot ask for an unbounded statement.

    The default is asserted to be inside the bound as well, because a default outside it
    would make every request that named no limit a 422, which is a screen that never loads.

    Delete this and `limit` becomes whatever a caller types, and the LIMIT clause with it."""
    assert read(client, "u_narrow", limit=MAX_RUNGS_PER_PAGE + 1).status_code == 422
    assert read(client, "u_narrow", limit=MAX_RUNGS_PER_PAGE).status_code == 200
    assert DEFAULT_RUNGS_PER_PAGE <= MAX_RUNGS_PER_PAGE


def test_both_routes_refuse_a_request_carrying_no_credential(client: TestClient) -> None:
    """The gate is on the matrix as it is on everything else under the prefix.

    `test_every_route_under_the_prefix_authenticates_its_caller` asserts this over the
    mounted set and would catch it too; it is here as well because that test drives GET only,
    and a PATCH is a route somebody can add with the dependency left off.

    Delete this and the write route is the one that gets mounted without `Asked`."""
    assert client.get(RUNGS_PATH).status_code == 401
    assert client.patch(f"{RUNGS_PATH}/{uuid.uuid4()}", json={}).status_code == 401


def test_the_matrix_is_absent_from_the_publicly_served_document() -> None:
    """The routes describe the estate's providers, so they stay out of the public schema.

    `brain.openapi.public_operations` projects what is already unauthenticated, and the
    prefix is what keeps these out whatever tag they carry. Asserted here rather than relying
    on the general test, because the general one would still pass if this router were mounted
    without the prefix.

    Delete this and a router mounted at the root publishes which providers this company holds
    keys for, to anybody who asks for the document."""
    from brain.openapi import public_operations

    app: FastAPI = create_app(Settings(env="production"))

    for path in public_operations(app):
        assert "routing" not in path
