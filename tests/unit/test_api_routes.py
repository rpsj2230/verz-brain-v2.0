"""The HTTP surface, driven as a client drives it, with the gate underneath.

This is `tests/e2e/test_wave_two_lark_question.py` with the transport changed. That test
proves one question asked by two people produces two different answers when the gate's parts
are composed by hand; these prove the same property when the parts are composed by an
application serving requests, which is the only composition a person ever meets.

Every request here goes through the real stack: a bearer token parsed and validated, a
subject mapped to a principal, a reach resolved and narrowed by the channel and the
sign-in, a row tool called, and the redactor deciding what may leave. Nothing is stubbed
except the three things that are seams by design and have no implementation in this
repository: the signature verifier, the entitlement store and the row source.

**The signature is a marker naming the key that produced it**, which is the same stand-in
`tests/unit/test_oidc.py` uses and for the same reason: a token signed by the wrong key
fails here exactly as a real one would, and the standard library cannot verify RS256.

**The row source deliberately ignores the compiled statement.** It hands back every seeded
row whatever the query narrowed to, which is not what the deployed source will do and is the
right shape for this test: it means the permission-correctness asserted below is produced by
the projection and the redactor rather than by the double having been polite. A source that
filtered would make every one of these tests pass with the redactor removed.

Task ids: M31.1.4.1, M31.1.4.3, M31.1.4.4, M32.5.2.1
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from brain import api_routes
from brain.api import API_PREFIX
from brain.api_routes import (
    FILTER_PARAM,
    FILTER_SEPARATOR,
    MAX_FILTER_TERM_LENGTH,
    MAX_FILTERS,
    GateWiring,
    RecordPage,
    filter_scope,
)
from brain.app import Settings, create_app
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.redaction import LOCK_TEXT
from brain.core.scope import Clause, Op, Scope
from brain.identity.bearer import SECOND_FACTOR_METHODS, TokenAuthority
from brain.identity.oidc import SIGN_IN_PROMPT, KeySet, SigningKey
from brain.knowledge.rows import ID_KEY, RowQuery
from brain.tools.startup import build_registry

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
ISSUER = "https://id.verz.example/realms/brain"
AUDIENCE = "brain-api"
KID = "k1"
SOURCE = "local"

#: Improbable on purpose, for the reason `tests/fixtures/company.py` gives about its own
#: canaries: a cost of 400 leaking into a response looks like data, and this cannot be
#: confused with anything a test row would plausibly hold. If it appears in a body it leaked.
CANARY_COST = "CANARY-COST-4TQ9M"
CANARY_MARGIN = "CANARY-MARGIN-8HZLR"

#: The subject each person's token carries. Opaque, as a Keycloak `sub` is.
SUBJECTS: dict[str, str] = {
    "u_narrow": "1f2e3d4c-0000-4000-8000-00000000000a",
    "u_wide": "1f2e3d4c-0000-4000-8000-00000000000b",
    "u_prefix": "1f2e3d4c-0000-4000-8000-00000000000c",
    "u_none": "1f2e3d4c-0000-4000-8000-00000000000d",
    "u_admin": "1f2e3d4c-0000-4000-8000-00000000000e",
    "u_elsewhere": "1f2e3d4c-0000-4000-8000-00000000000f",
}

#: Two seeded rows, one in each prefix, so a scope that admits one of them can be shown to
#: drop the other rather than merely to keep what it was given.
SEEDED_ROWS: tuple[dict[str, Any], ...] = (
    {
        ID_KEY: "p_web_1",
        "sku": "WEB-1001",
        "name": "Managed hosting, small",
        "sell_price": "1200",
        "cost": CANARY_COST,
        "margin": CANARY_MARGIN,
    },
    {
        ID_KEY: "p_mnt_1",
        "sku": "MNT-2002",
        "name": "Maintenance retainer",
        "sell_price": "900",
        "cost": CANARY_COST,
        "margin": CANARY_MARGIN,
    },
)


# ----------------------------------------------------------------- the seams
def cap(value: str) -> Capability:
    return Capability(value=value)


def _grants(*capabilities: str, scope: Scope) -> tuple[Grant, ...]:
    return tuple(Grant(capability=cap(value), scope=scope) for value in capabilities)


WHOLE = Scope.unrestricted()
WEB_ONLY = Scope(clauses=(Clause(field="sku", op=Op.PREFIX, value="WEB-"),))
#: A scope that reaches the entity and none of the seeded rows, so an unfiltered request from
#: this person is answered with an empty page by the redactor rather than by a 404. It is the
#: only way to obtain an empty page that was fetched, which is what a filtered empty page has
#: to be indistinguishable from.
NOWHERE = Scope(clauses=(Clause(field="sku", op=Op.PREFIX, value="ZZZ-"),))

#: What each person holds. Written out per person in one place, for the reason the company
#: fixture gives: a helper with defaults hides the thing under test.
GRANTS: dict[str, tuple[Grant, ...]] = {
    # Sees the record and never the money. The price-list shape of Wei Ling.
    "u_narrow": _grants(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        scope=WHOLE,
    ),
    # The same, plus the two confidential columns.
    "u_wide": _grants(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        "read:price_list.cost",
        "read:price_list.margin",
        scope=WHOLE,
    ),
    # Everything the wide caller holds, in one prefix only.
    "u_prefix": _grants(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        scope=WEB_ONLY,
    ),
    # A real principal holding nothing over this entity.
    "u_none": (),
    # Holds an admin capability company-wide, which the assurance ceiling must take away
    # from a session with no second factor in it.
    "u_admin": _grants("admin:grant", "read:price_list", "read:price_list.sku", scope=WHOLE),
    # Reaches the entity in a scope no seeded row satisfies, so every request of theirs is
    # answered with an empty page that was nonetheless fetched.
    "u_elsewhere": _grants(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        scope=NOWHERE,
    ),
}


def principal(pid: str) -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=Employment.STAFF,
        display_name=f"Person {pid}",
        primary_department="web",
    )


class Directory:
    """A `PrincipalDirectory` over the subjects above. The real one reads a table."""

    def principal_for_subject(self, issuer: str, subject: str) -> Principal | None:
        if issuer != ISSUER:
            return None
        for pid, sub in SUBJECTS.items():
            if sub == subject:
                return principal(pid)
        return None


class Store:
    """A `brain.gate.resolve.EntitlementStore` over `GRANTS`."""

    def load(self, principal_id: str) -> EntitlementSet:
        return EntitlementSet(principal_id=principal_id, grants=GRANTS[principal_id])


class Versions:
    """A `VersionSource`. One version for everybody; nothing here mutates grants."""

    def grants_version(self, principal_id: str) -> int:
        return 1


class NoCache:
    """An `EntitlementCache` that forgets everything, which it is allowed to do."""

    def get(self, key: str) -> EntitlementSet | None:
        return None

    def set(self, key: str, value: EntitlementSet, ttl_seconds: int) -> None:
        return None


class UnfilteredRows:
    """A `RowSource` that returns every seeded row, whatever the statement asked for.

    Deliberately unhelpful. A double that applied the compiled predicate would make the
    assertions below pass with the redactor deleted, because the rows the caller may not see
    would never have arrived. This one hands the route more than the query asked for, so
    anything absent from a response was removed by the projection or by the redactor.

    `asked` records whether it was consulted at all, which is how the short-circuit for a
    caller with no reachable column is proved rather than assumed.

    `seen` keeps the compiled queries. A filter is a claim about what went into the WHERE
    clause, and a double that ignores the statement can say nothing about that; keeping the
    query is how a test can ask whether the caller's scope survived beside the asker's term
    rather than inferring it from rows this double did not filter.
    """

    def __init__(self) -> None:
        self.asked = 0
        self.seen: list[RowQuery] = []

    def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]:
        self.asked += 1
        self.seen.append(query)
        return SEEDED_ROWS


def signing_key(kid: str = KID) -> SigningKey:
    return SigningKey(kid=kid, algorithm="RS256", material=f"-----PUBLIC {kid}-----", use="sig")


class Keys:
    """A `brain.identity.bearer.KeySource` holding one key. No network anywhere."""

    def keys_for(self, issuer: str, now: datetime) -> KeySet:
        return KeySet(issuer=ISSUER, keys=(signing_key(),), fetched_at=now)

    def key_for(self, issuer: str, kid: str, now: datetime) -> SigningKey:
        return signing_key(kid)


def marker_for(kid: str) -> bytes:
    """The stand-in for a signature. Naming the key is what makes a wrong key fail."""
    return b"signed-by:" + kid.encode()


def verifier(*, signing_input: bytes, signature: bytes, key: SigningKey) -> bool:
    assert signing_input, "the signing input must be the bytes that were signed"
    return signature == marker_for(key.kid)


def token_for(
    pid: str,
    *,
    kid: str = KID,
    signed_by: str | None = None,
    now: datetime | None = None,
    claims: Mapping[str, object] | None = None,
) -> str:
    """A compact JWS for one person, built from raw parts.

    Assembled rather than borrowed from a helper that returns a `VerifiedClaims`, because the
    thing under test is what the server makes of bytes a client sent. A test that handed the
    application an already-verified object would be testing the object.

    Minted against the real clock rather than against `NOW`, and that is not a convenience.
    The route reads `datetime.now(UTC)` because a request's deadline is the wall clock and
    nothing else, so a token stamped with a fixed hour is an expired token and every one of
    these tests would pass for the wrong reason: 401 on every request, and each assertion
    about what a person may see never reached.
    """
    now = now or datetime.now(UTC)
    header: dict[str, object] = {"alg": "RS256", "typ": "JWT", "kid": kid}
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECTS[pid],
        "typ": "Bearer",
        "sid": "sess-1",
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iat": int(now.timestamp()),
    }
    payload.update(claims or {})

    def seg(blob: bytes) -> str:
        return base64.urlsafe_b64encode(blob).decode().rstrip("=")

    return ".".join(
        (
            seg(json.dumps(header).encode()),
            seg(json.dumps(payload).encode()),
            seg(marker_for(signed_by or kid)),
        )
    )


def wiring() -> GateWiring:
    return GateWiring(
        authority=TokenAuthority(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys=Keys(),
            verify=verifier,
            directory=Directory(),
        ),
        versions=Versions(),
        store=Store(),
        cache=NoCache(),
    )


@pytest.fixture
def rows() -> UnfilteredRows:
    return UnfilteredRows()


@pytest.fixture
def client(rows: UnfilteredRows) -> Iterator[TestClient]:
    """The real application, wired with the three seams and nothing else changed.

    The registry is replaced after startup rather than built here, and that is the point:
    `brain.tools.startup.build_registry` is called with `records=`, which is the one argument
    that module says stands between this application and a working data plane. Everything
    else the route touches is what `create_app` produced.
    """
    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = wiring()
        app.state.tools = build_registry(source=SOURCE, records=rows)
        yield c


def ask(
    c: TestClient,
    pid: str,
    entity: str = "price_list",
    *,
    signed_by: str | None = None,
    claims: Mapping[str, object] | None = None,
    terms: Sequence[str] = (),
) -> Response:
    token = token_for(pid, signed_by=signed_by, claims=claims)
    # Annotated rather than returned straight through: this TestClient's `get` is typed as
    # returning Any, and a helper every assertion below reads through is the wrong place for
    # a value nothing describes.
    #
    # The terms are handed over as repeated pairs rather than as a mapping, because the route
    # declares one repeatable parameter and a mapping cannot express two of them. An empty
    # sequence produces no query string at all, which is what every test written before the
    # filter existed asked for and still asks for.
    response: Response = c.get(
        f"{API_PREFIX}/records/{entity}",
        headers={"authorization": f"Bearer {token}"},
        params=[(FILTER_PARAM, term) for term in terms],
    )
    return response


# ------------------------------------------------------ the milestone, over HTTP
def test_the_same_request_gets_two_different_answers(client: TestClient) -> None:
    """**The sentence the wave-1 milestone is written as, with HTTP in the middle.** One
    request path, two people, two answers, and the difference is their grants rather than
    anything in the request.

    `tests/e2e/test_wave_two_lark_question.py` proves this when the gate's parts are composed
    by hand. Nothing proved it when they are composed by an application, and a route is
    exactly where the composition goes wrong: an entitlement resolved and then not passed on,
    a redactor called with the nominal reach instead of the narrowed one, a handler reading a
    record to decide whether to show it.

    Delete this and the two halves of the route can be wired to different entitlements with
    every other test in this file still green, because each of them looks at one person."""
    narrow = RecordPage.model_validate(ask(client, "u_narrow").json())
    wide = RecordPage.model_validate(ask(client, "u_wide").json())

    assert narrow.items != wide.items
    assert set(narrow.items[0]) < set(wide.items[0]), (
        "the narrower person must see strictly fewer fields, not merely different ones"
    )


def test_the_person_who_may_not_see_the_cost_does_not_see_the_cost(client: TestClient) -> None:
    """Asserted on the canary against the whole response text rather than on the field's
    absence from the parsed records, because a value can be absent from a record and present
    in a label, a lock, an error message or a header."""
    response = ask(client, "u_narrow")

    assert CANARY_COST not in response.text
    assert CANARY_MARGIN not in response.text


def test_she_still_gets_the_answer_she_asked_for(client: TestClient) -> None:
    """The half that makes the other half worth having. A route that withheld everything
    would satisfy every leak test here and serve nobody, and "it refused" is the failure
    people actually report."""
    page = RecordPage.model_validate(ask(client, "u_narrow").json())

    assert [row["sku"] for row in page.items] == ["WEB-1001", "MNT-2002"]
    assert page.items[0]["sell_price"] == "1200"


def test_the_person_entitled_to_the_cost_receives_it(client: TestClient) -> None:
    """The other positive case. Withholding a confidential column from somebody who holds its
    capability is the same bug as leaking it, arriving from the other direction, and it is
    the one nobody files a security report about."""
    page = RecordPage.model_validate(ask(client, "u_wide").json())

    assert page.items[0]["cost"] == CANARY_COST
    assert page.items[0]["margin"] == CANARY_MARGIN


def test_the_row_plane_leaves_nothing_for_the_redactor_to_lock(client: TestClient) -> None:
    """**A finding rather than a preference, and it is worth reading before screen 3 is
    built.** The lock is meant to be the product: a client record showing contract value
    marked `Restricted`, with the account manager seeing the figure in the same place.
    Nothing is locked here, and the two modules that produce that outcome are each correct.

    `brain.knowledge.rows` compiles the SELECT list from the caller's capabilities, so a
    column they may not read is never fetched. `brain.core.redaction.compute_mask` locks a
    field only when the record carries it, so that "a lock never advertises a column this
    record does not hold". Put in series, the column is absent by the time the redactor
    looks, and there is nothing to lock.

    So a caller reading through the row plane learns nothing about the columns they cannot
    see, which is stricter than the design asks for and is a real loss: the lock is what makes
    the request-access route reachable, and a person who cannot see that a field exists cannot
    ask for it.

    This route deliberately does not manufacture the missing locks from the classification. It
    could: the withheld set is the entity's columns minus the admitted ones, and both are in
    reach here. That would be a second implementation of what a caller may not see, living in
    a route, computed by different arithmetic from the one the redactor uses, and the day the
    two disagree the route's answer is the one on screen.

    Delete this and the gap becomes invisible: locks are empty, every leak test still passes,
    and screen 3 gets built against a payload that never carries one."""
    page = RecordPage.model_validate(ask(client, "u_narrow").json())

    assert page.locked == (), (
        "the row plane now returns columns the caller may not read, which is the change this "
        "test exists to make somebody notice"
    )
    assert "cost" not in page.items[0]
    # The lock text itself is unreachable through this route today, and the constant is
    # asserted against the payload rather than against a copy of the word, so the day a lock
    # does arrive this test fails on the first assertion rather than on a stale string.
    assert LOCK_TEXT not in ask(client, "u_narrow").text


# ------------------------------------------------- denied and absent are one answer
def test_an_entity_nobody_reaches_answers_exactly_as_an_entity_that_does_not_exist(
    client: TestClient,
) -> None:
    """**The rule the whole taxonomy exists for, at the entity level.** A caller holding no
    grant over the price list and a caller asking for a table this company does not run must
    receive the same status and the same bytes.

    An empty page for the first would be the friendly answer and it is the leak: it says the
    price list exists here, and repeated with different names it maps the installation.

    Compared as whole bodies rather than field by field, because a difference introduced
    later will be in whichever field the comparison did not name. The trace id is the one
    exclusion and it is the documented one: it is minted per request before anything is
    known about the question, so it differs between any two requests and says nothing about
    either. `brain.api.A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT` makes exactly
    that argument for keeping it in the body at all.

    Delete this and an unreachable entity can start answering 200 with an empty list, which
    reads in review as a kindness."""
    refused = ask(client, "u_none")
    missing = ask(client, "u_none", entity="finance_ledger")

    assert refused.status_code == missing.status_code == 404
    assert refused.json()["message"] == "I could not find that."
    assert {k: v for k, v in refused.json().items() if k != "trace_id"} == {
        k: v for k, v in missing.json().items() if k != "trace_id"
    }
    assert refused.json()["trace_id"] and missing.json()["trace_id"]
    assert refused.headers.get("content-length") == missing.headers.get("content-length"), (
        "the two refusals differ in length, so they are distinguishable without being read"
    )


def test_the_row_source_is_not_asked_about_an_entity_the_caller_cannot_reach(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """The refusal happens before the read, not after it. A route that fetched and then
    dropped would have had the rows in this process, in a log line, in a traceback and in
    whatever a retry path held, and "we removed them before rendering" would be a claim about
    every code path rather than a property of one.

    Delete this and the check can move below the read, which changes no assertion anywhere
    else and quietly makes the guarantee unprovable."""
    assert ask(client, "u_none").status_code == 404
    assert rows.asked == 0


def test_a_row_outside_the_callers_scope_is_dropped_rather_than_returned_empty(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """The source hands back both rows and the caller may see one of them.

    This is the assertion the unfiltered double exists for. The maintenance row reaches the
    process, every one of its fields fails the caller's scope predicate, the record has
    nothing left on it, and `brain.core.redaction` drops it whole rather than returning a
    husk that announces it exists.

    Delete this and a source that stops applying the predicate produces a route that returns
    every row, with no test anywhere disagreeing."""
    page = RecordPage.model_validate(ask(client, "u_prefix").json())

    assert rows.asked == 1, "the double was not consulted, so nothing was filtered"
    assert [row["sku"] for row in page.items] == ["WEB-1001"]


def test_no_response_carries_a_count_of_what_was_withheld(client: TestClient) -> None:
    """A page saying "showing 1 of 2" hands the reader one fact they did not have, and it is
    the same leak whether the number is called total, count or matches.

    `brain.api.Page` carries an optional `total` precisely because some listings can afford
    one, which is what makes this worth pinning here: the field exists, it is inherited, and
    this route must never populate it.

    Asserted on the raw body rather than on the parsed model, because a model with a default
    of None reads as absent whatever the server sent.

    Delete this and `total` becomes an obviously useful addition for a list view."""
    body = ask(client, "u_prefix").json()

    assert body["total"] is None
    for banned in ("withheld", "hidden", "redacted", "of 2", "1 of"):
        assert banned not in ask(client, "u_prefix").text.lower()


# ------------------------------------------------------------- the credential
def test_a_request_with_no_token_is_refused(client: TestClient) -> None:
    """The floor. Delete this and the whole surface is public the first time a dependency is
    dropped from a signature."""
    response = client.get(f"{API_PREFIX}/records/price_list")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("what", "signed_by", "claims"),
    [
        ("a signature from another key", "k2", None),
        ("an issuer we do not accept", None, {"iss": "https://evil.example/realms/brain"}),
        ("an audience the token was not minted for", None, {"aud": "some-other-client"}),
        ("an expiry in the past", None, {"exp": int((NOW - timedelta(days=1)).timestamp())}),
        ("a subject nobody here is", None, {"sub": "1f2e3d4c-0000-4000-8000-ffffffffffff"}),
    ],
)
def test_a_token_that_fails_any_check_is_refused_with_the_same_sentence(
    client: TestClient, what: str, signed_by: str | None, claims: Mapping[str, object] | None
) -> None:
    """Four checks and an unknown subject, each producing one status and one body.

    The point is the sameness rather than the refusal. Telling the presenter that the key was
    unknown rather than the audience wrong tells somebody forging a token which part to fix
    next, one attempt at a time, and the difference is invisible in a screenshot of the screen
    a real person is looking at.

    `sub` is parametrised alongside the four because an unrecognised person is the case an
    implementation is most tempted to be helpful about, and "your account is not set up" is a
    sentence that confirms which subjects exist.

    Delete this and each refusal grows its own message, which is what every framework's
    default does.

    Task ids: M1.1.2"""
    response = ask(client, "u_wide", signed_by=signed_by, claims=claims)

    assert response.status_code == 401, what
    assert response.json()["message"] == SIGN_IN_PROMPT, what
    assert set(response.json()) == {"message", "trace_id"}, what


def test_a_refused_credential_carries_a_trace_id_somebody_can_quote(client: TestClient) -> None:
    """Without one the only thing a person can report is "it says sign in again", and the one
    sentence every refusal shares is exactly what makes the trace id load-bearing here rather
    than decorative."""
    response = ask(client, "u_wide", signed_by="k2")

    assert response.json()["trace_id"]
    assert response.json()["trace_id"] == response.headers["x-trace-id"]


def test_an_application_with_no_token_authority_refuses_rather_than_admits() -> None:
    """**Fail closed, and it is the deployed state.** No signature verifier is wired into this
    application, so `create_app` attaches no gate wiring and there is nothing that could check
    a credential.

    The tempting reading of a missing authority is "authentication is off in this
    environment", which is one code path in every environment decided by whether a variable
    was set. There is no branch here that produces a caller without a verified token.

    Driven against the application `create_app` actually returns, rather than one this test
    assembled, because the shape being refused is precisely a mechanism that works when
    somebody wires it and is wired by nobody."""
    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get(
            f"{API_PREFIX}/records/price_list",
            headers={"authorization": f"Bearer {token_for('u_wide')}"},
        )

    assert response.status_code == 401
    assert response.json()["message"] == SIGN_IN_PROMPT


# ------------------------------------------------------------------ the ceilings
def test_a_password_only_session_cannot_exercise_an_admin_capability(client: TestClient) -> None:
    """`gate.admission` gives an AUTHENTICATED caller the read, write and invoke verbs and
    withholds approve and admin, and until this route nothing called it, so the ceiling was a
    description rather than a thing that happened.

    Asserted on the ent_hash rather than on a capability list, because `/me` deliberately
    publishes no list: the same person authenticated two ways must be answered at two
    different reaches, and the hash is the observable that says so without naming anything.

    Delete this and `admit` can be dropped from the route, at which point every grant a person
    holds is exercisable from any channel on any sign-in.

    Task ids: M3.3.4"""

    def hash_for(claims: Mapping[str, object] | None = None) -> str:
        token = token_for("u_admin", claims=claims)
        body = client.get(f"{API_PREFIX}/me", headers={"authorization": f"Bearer {token}"}).json()
        return str(body["ent_hash"])

    # A literal rather than a value taken out of `SECOND_FACTOR_METHODS`. Reading the
    # constant here would compare it against itself: drop `otp` from it and both this token
    # and the expectation move together, and the test stays green for any set at all. That is
    # the shape that caught three authors in this repository on one afternoon.
    weak = hash_for()
    strong = hash_for({"amr": ["otp"]})

    assert weak != strong, (
        "the same person signed in two ways was answered at one reach, so the assurance "
        "ceiling is not being applied"
    )


def test_the_admin_capability_is_the_one_that_moves(client: TestClient) -> None:
    """The guard on the test above, which only proves two hashes differ and would pass if the
    route varied the reach by anything at all.

    Computed here from `admit` over the grants the fixture declares, and compared against what
    the server answered. That is not the route's own arithmetic replayed: the route resolves
    through `gate.resolve`, narrows by a channel this test does not name, and hashes the
    result, so agreement means the whole chain reached the set this fixture describes.

    Delete this and the ceiling can narrow by the wrong axis and still produce two hashes."""
    from brain.gate.admission import Assurance, admit
    from brain.gate.context import Channel

    held = EntitlementSet(principal_id="u_admin", grants=GRANTS["u_admin"])
    expected_weak = admit(held, Channel.CONSOLE, Assurance.AUTHENTICATED).ent_hash()
    expected_strong = admit(held, Channel.CONSOLE, Assurance.STRONG).ent_hash()

    token = token_for("u_admin")
    weak = client.get(f"{API_PREFIX}/me", headers={"authorization": f"Bearer {token}"}).json()

    assert weak["ent_hash"] == expected_weak
    assert expected_weak != expected_strong, "the fixture holds no capability the ceiling moves"
    assert {g.capability.value for g in admit(held, Channel.CONSOLE, Assurance.STRONG).grants} - {
        g.capability.value for g in admit(held, Channel.CONSOLE, Assurance.AUTHENTICATED).grants
    } == {"admin:grant"}


def test_a_password_is_not_a_second_factor(client: TestClient) -> None:
    """The constant beside the ceiling, asserted against a property rather than against
    itself.

    `SECOND_FACTOR_METHODS` decides who holds the approve and admin verbs, and every RFC 8176
    value describing somebody typing a secret they know belongs outside it. A list widened by
    one word promotes every ordinary sign-in in the company, silently and in the permissive
    direction.

    Driven through the route rather than read off the frozenset, so that a change to how `amr`
    is interpreted fails here too, and the membership check is stated as the negative property
    it is rather than as a copy of the set.

    Delete this and `pwd` can be added to the list by somebody wiring up a realm that emits
    it, which reads as making the constant match reality."""
    for first_factor in ("pwd", "pin", "user", "kba"):
        assert first_factor not in SECOND_FACTOR_METHODS
    assert SECOND_FACTOR_METHODS, "an empty set makes every session AUTHENTICATED for ever"

    def assurance(amr: object) -> str:
        token = token_for("u_admin", claims={"amr": amr})
        body = client.get(f"{API_PREFIX}/me", headers={"authorization": f"Bearer {token}"}).json()
        return str(body["assurance"])

    assert assurance(["pwd"]) == "authenticated"
    assert assurance(["pwd", "otp"]) == "strong"
    # A string rather than a list is refused rather than split. Choosing a separator would be
    # a guess that decides whether somebody holds the approve verb.
    assert assurance("mfa") == "authenticated"


def test_a_credential_that_is_not_a_bearer_token_is_refused(client: TestClient) -> None:
    """A scheme this API does not accept is refused rather than parsed hopefully.

    The failure it prevents is small and real: a client sending `Basic` gets a 401 and a log
    line naming the scheme, rather than the header's second half being fed to a JWT parser
    and refused as malformed, which tells the operator nothing about what the client did.

    **The second half is the one that tests the check, and the first half survived a mutation
    without it.** Removing the scheme comparison entirely left `Basic <base64>` still refused,
    because the base64 blob is not three dot-separated segments and the parser refuses it as
    malformed. So that case proves the parser works and says nothing about the scheme. A real
    token under the wrong scheme is the case where the two differ, and without it the check
    could be deleted with this file green.

    Delete this and the scheme check can go, at which point `Authorization: Token <jwt>` and
    `Authorization: <jwt>` both work, and so does anything else that happens to split on a
    space."""
    basic = base64.b64encode(b"someone:hunter2").decode()
    refused = client.get(f"{API_PREFIX}/me", headers={"authorization": f"Basic {basic}"})

    assert refused.status_code == 401
    assert refused.json()["message"] == SIGN_IN_PROMPT
    assert "hunter2" not in refused.text

    good = token_for("u_admin")
    assert (
        client.get(f"{API_PREFIX}/me", headers={"authorization": f"Bearer {good}"}).status_code
        == 200
    ), "the token itself is not acceptable, so the next assertion proves nothing"

    for scheme in ("Token", "JWT", "bearer2"):
        wrong = client.get(f"{API_PREFIX}/me", headers={"authorization": f"{scheme} {good}"})
        assert wrong.status_code == 401, f"{scheme} was accepted as though it were Bearer"


def test_the_bearer_scheme_is_matched_without_regard_to_case(client: TestClient) -> None:
    """The sibling of the test above, and the reason it is a sibling rather than a line in it.

    RFC 7235 makes the scheme token case-insensitive, so a client sending `bearer` is correct
    and refusing it would be our bug. A check tightened until only the exact string `Bearer`
    passes would satisfy every refusal test above and break real clients, which is the failure
    a guard tested only by its refusals always has."""
    token = token_for("u_admin")

    for spelling in ("Bearer", "bearer", "BEARER", "BeArEr"):
        response = client.get(f"{API_PREFIX}/me", headers={"authorization": f"{spelling} {token}"})
        assert response.status_code == 200, f"{spelling} was refused"


def test_an_entity_two_tools_answer_for_is_refused_rather_than_picked_between(
    rows: UnfilteredRows,
) -> None:
    """Two sources carrying one entity is a misconfigured install, and the caller is told
    nothing about it.

    Answering from whichever tool sorted first would make which system replies depend on a
    name, and answering 409 would tell the caller that two systems here carry this entity,
    which is a fact about the installation they asked one question to learn.

    Built by registering two row tools by hand, because `build_registry` takes one source and
    therefore cannot produce the state under test. That is the right shape for the builder and
    the wrong shape for a test that needs the ambiguity to exist.

    Delete this and `len(matching) != 1` can become `not matching`, which reads as a
    simplification and picks arbitrarily."""
    from brain.knowledge.columns import PRICE_LIST
    from brain.knowledge.rows import RowTool
    from brain.tools.registry import ResultContract, ToolRegistry

    registry = ToolRegistry()
    for source in ("local", "other"):
        tool = RowTool(
            source=source, classification=PRICE_LIST, description=f"price list from {source}"
        )
        registry.register(
            tool.definition(),
            tool.reader(rows),
            result_contract=ResultContract.TYPED,
            scope=tool.scope,
        )

    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = wiring()
        app.state.tools = registry.freeze()
        response = ask(c, "u_wide")

    assert response.status_code == 404
    assert response.json()["message"] == "I could not find that."
    assert rows.asked == 0, "an ambiguous entity was read from anyway"


def test_a_token_belonging_to_no_session_is_held_to_the_api_ceiling(client: TestClient) -> None:
    """A token with no `sid` belongs to no interactive session, which is what a
    service-account token looks like, and `CHANNEL_VERBS` gives `API` neither approve nor
    admin for that reason: a client-credentials grant is a secret in a configuration file.

    Read from the claims rather than from a header, because a header naming the channel is a
    header a caller sets, and the caller would then be choosing their own ceiling.

    Delete this and a service token acquires the console's verbs by carrying the console's
    header.

    Task ids: M3.3.3"""
    interactive = client.get(
        f"{API_PREFIX}/me", headers={"authorization": f"Bearer {token_for('u_admin')}"}
    ).json()
    without_session = token_for("u_admin", claims={"sid": None})
    headless = client.get(
        f"{API_PREFIX}/me", headers={"authorization": f"Bearer {without_session}"}
    ).json()

    assert interactive["channel"] == "console"
    assert headless["channel"] == "api"


# ---------------------------------------------------------------- what /me says
def test_me_answers_the_callers_own_facts(client: TestClient) -> None:
    """The route that proves the credential path end to end before a data plane exists.

    Delete this and a token can stop being mapped to a principal without any test noticing,
    because every other test here reads a record and a record can be empty for many
    reasons."""
    token = token_for("u_narrow")
    body = client.get(f"{API_PREFIX}/me", headers={"authorization": f"Bearer {token}"}).json()

    assert body["principal_id"] == "u_narrow"
    assert body["display_name"] == "Person u_narrow"
    assert body["assurance"] == "authenticated"


def test_me_publishes_no_list_of_what_the_caller_holds(client: TestClient) -> None:
    """The caller's own capabilities would be safe in the narrow sense and would become the
    thing a browser caches and renders from, which is a permission model in the copy an
    attacker edits.

    Asserted over the field set rather than over one response, because the risk is somebody
    adding the field and a later response populating it."""
    from brain.api_routes import CallerView

    assert set(CallerView.model_fields) == {
        "principal_id",
        "display_name",
        "primary_department",
        "employment",
        "assurance",
        "channel",
        "ent_hash",
    }
    for banned in ("capabilities", "grants", "scopes", "roles", "permissions"):
        assert banned not in CallerView.model_fields


# ------------------------------------------------------------- the mounted set
def test_every_route_under_the_prefix_authenticates_its_caller() -> None:
    """**Asserted over what is mounted, not over what each route was written to do.** The
    failure this catches is a route added later without the dependency: it works, it is
    reviewed, and it serves company data to anybody who can reach the port.

    Driven by request rather than by reading signatures, because a signature can name a
    dependency that resolves to nothing, and because the property is about the response a
    stranger gets.

    Delete this and the next route under this prefix is public until somebody notices."""
    app: FastAPI = create_app(Settings(env="development"))
    paths = [p for p in app.openapi()["paths"] if p.startswith(API_PREFIX)]

    assert paths, "no route is mounted under the API prefix"

    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = wiring()
        for path in paths:
            response = c.get(path.replace("{entity}", "price_list"))
            assert response.status_code == 401, f"{path} answered {response.status_code} unsigned"


def test_no_route_under_the_prefix_reaches_the_public_schema() -> None:
    """The document served unauthenticated is a projection of what is already
    unauthenticated. These routes are not, and the prefix rule is what keeps them out
    whatever tag they carry.

    Delete this and the first route tagged `docs` by habit publishes its response shape,
    field names and all, to anyone who asks for `/openapi.json`."""
    from brain.openapi import public_operations

    app: FastAPI = create_app(Settings(env="production"))

    for path in public_operations(app):
        assert not path.startswith(API_PREFIX)


# ------------------------------------------------------ the asker's own filter
#
# The route now takes a filter, and every test below is about one of the five ways that is
# easy to get wrong: a filter becoming a way to read a column the response withholds, a
# filter widening the scope it was supposed to narrow inside, a refusal that tells a caller
# which of the two it was, a value reaching the statement as syntax, and a count arriving
# because somebody wanted to say how many matched.
#
# Task ids: M32.5.2.1

#: A PostgreSQL dialect to render a compiled statement against. Taken from an engine because
#: `postgresql.dialect()` is untyped and mypy runs strict; creating one performs no I/O and
#: nothing here ever connects it. The same device `tests/unit/test_row_plane.py` uses.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

RECORDS_OPERATION = f"{API_PREFIX}/records/{{entity}}"


def rendered(query: RowQuery) -> str:
    """The statement as PostgreSQL would receive it, with values inlined so a test can read
    them. The production path binds them; `literal_binds` is a rendering choice made here."""
    return str(query.statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))


def declared_query_parameters() -> dict[str, Any]:
    """The query parameters the mounted route publishes, keyed by the name a client sends.

    Read out of the generated document rather than off the function's signature, because the
    document is what the console reads to decide what it may send, and a parameter that works
    but is undocumented is a parameter the console will correctly never use.
    """
    app: FastAPI = create_app(Settings(env="development"))
    operation = app.openapi()["paths"][RECORDS_OPERATION]["get"]
    return {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}


def declared_term_pattern() -> re.Pattern[str]:
    """The filter term grammar as the document publishes it.

    Compiled here from the document's own string, so every assertion about what the grammar
    admits is made against what a client would be told rather than against the constant the
    route was written with. `fullmatch` rather than `match`, because Python's `$` also
    matches before a trailing newline and the server's engine does not.
    """
    schema = declared_query_parameters()[FILTER_PARAM]["schema"]
    return re.compile(str(schema["items"]["pattern"]))


def longest_field_name() -> str:
    """The longest column name `brain.core.scope.Clause` will hold, found by asking it.

    Derived rather than written down, so that the bound in the route's declared grammar is
    compared against the model that enforces it instead of against a copy of the same number.
    """
    longest = ""
    for length in range(1, 4097):
        candidate = "a" * length
        try:
            Clause(field=candidate, op=Op.EQ, value="x")
        except ValidationError:
            break
        longest = candidate
    assert longest, "Clause accepts no field name at all, so there is nothing to bound"
    return longest


def test_the_route_declares_the_filter_it_answers() -> None:
    """**The whole reason the filter has this shape.** FastAPI drops a query parameter no
    signature names, without a word and with a 200 in front of it, so a console sending a
    filter the route does not declare gets unfiltered rows back and shows them as the
    matching ones. `console/tests/records-page.test.tsx` reads these names out of this
    document and refuses to send anything absent from them, which is why a filter that works
    and is not declared is a filter nothing will ever send.

    Asserted over the document rather than over the signature, and over the item schema
    rather than only the name, because the grammar is the half a client needs in order to
    refuse a malformed term without spending a request.

    Delete this and the parameter can be renamed, aliased away or dropped from the schema
    while every behavioural test below still passes, at which point the console is back to
    having no filter it is allowed to send.

    Task ids: M32.5.2.1"""
    parameters = declared_query_parameters()

    assert FILTER_PARAM in parameters, "the route answers a filter it does not publish"
    schema = parameters[FILTER_PARAM]["schema"]
    assert schema["type"] == "array", "one term per occurrence, so the parameter repeats"
    assert schema["items"]["pattern"], (
        "a client cannot refuse a malformed term it is not told about"
    )
    assert isinstance(schema["maxItems"], int)
    assert isinstance(schema["items"]["maxLength"], int)
    # The pagination half of the same leaf, pinned here so that adding the filter cannot be
    # what removes it.
    assert "limit" in parameters


def test_the_declared_filter_grammar_admits_only_terms_that_build_a_clause() -> None:
    """The join between the parameter's declaration and `brain.core.scope.Clause`.

    Everything the declaration lets through reaches `filter_scope`, which builds a `Clause`
    out of it. If the grammar were the wider of the two, a term could pass validation and
    then raise inside the route, and whether it raised would depend on how long a column name
    somebody typed. The bound is not copied here: the longest name `Clause` accepts is found
    by asking `Clause`, and the grammar is then required to stop one character later.

    The refusals are the interesting half and each is a real shape: a term with no separator
    at all, a term whose value is empty, which `check_grammar` refuses further down as a
    predicate no projected row can satisfy, and a name outside the field pattern.

    Delete this and the grammar can be widened to `.*`, at which point a filter is refused by
    a `ValidationError` in the handler rather than by the parameter, and a malformed filter
    starts being answered differently depending on which entity was asked for.

    Task ids: M32.5.2.1"""
    grammar = declared_term_pattern()
    longest = longest_field_name()

    admitted = ["sku:x", "a.b:x", "sku:a:b", "sku: leading space", "sku:%_\\", f"{longest}:x"]
    for term in admitted:
        assert grammar.fullmatch(term), f"{term!r} is refused and it is a term somebody means"
        clauses = filter_scope([term]).clauses
        assert len(clauses) == 1
        assert clauses[0].field == term.split(FILTER_SEPARATOR, 1)[0]
        assert clauses[0].op is Op.EQ

    refused = ["sku", "sku:", "", FILTER_SEPARATOR + "x", "Sku:x", "1sku:x", f"{longest}a:x"]
    for term in refused:
        assert not grammar.fullmatch(term), f"{term!r} is admitted and nothing downstream wants it"

    with pytest.raises(ValidationError):
        # The reason the grammar has to stop where it does, stated as the failure it prevents.
        Clause(field=f"{longest}a", op=Op.EQ, value="x")


def test_a_term_splits_at_the_first_separator_so_a_value_may_contain_one() -> None:
    """A column name cannot contain the separator and a value can, so the split is at the
    first one and never the last. The same argument the console makes about its locked-cell
    separator, and it has the same failure mode: split at the last one instead and
    `sku:WEB:1001` becomes a filter on a column called `sku:WEB`, which is not a column
    anybody has and is not the question that was asked.

    Delete this and `partition` becomes `rpartition` in a tidy-up, and every filter whose
    value contains a colon quietly asks about a different column.

    Task ids: M32.5.2.1"""
    clauses = filter_scope([f"sku{FILTER_SEPARATOR}WEB{FILTER_SEPARATOR}1001"]).clauses

    assert len(clauses) == 1
    assert clauses[0].field == "sku"
    assert clauses[0].value == f"WEB{FILTER_SEPARATOR}1001"
    # And the separator is one the field grammar cannot hold, which is what makes the split
    # unambiguous rather than merely conventional.
    with pytest.raises(ValidationError):
        Clause(field=f"sku{FILTER_SEPARATOR}web", op=Op.EQ, value="x")


def test_a_filter_on_a_column_the_caller_reads_reaches_the_statement_bound(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**The positive case, and the one a guard tested only by its refusals never has.** A
    route that ignored every filter would satisfy every leak test in this section and would
    be the failure the console spent a constant refusing to commit: rows arrive, they are not
    the filtered ones, and nothing says so.

    Asserted on the compiled statement rather than on the rows, because the double here hands
    back every seeded row whatever the query narrowed to. That is deliberate everywhere else
    in this file and it is what makes this assertion honest: the route's job is to put the
    caller's term into the WHERE clause, and the row plane's job, tested where the rows are,
    is to turn that into rows.

    Delete this and `filters=` can be dropped from the `RowRequest` the route builds, with
    every refusal below still green because refusing everything is what they check.

    Task ids: M32.5.2.1"""
    response = ask(client, "u_wide", terms=["sku:WEB-1001"])

    assert response.status_code == 200
    assert rows.asked == 1, "the row source was never consulted, so nothing was filtered"
    query = rows.seen[0]
    assert query.certainly_empty is False
    assert "WEB-1001" in query.statement.compile(dialect=DIALECT).params.values(), (
        "the value did not arrive as a bound parameter, so it arrived some other way"
    )
    assert "fields ->> 'sku' = 'WEB-1001'" in rendered(query)


def test_a_filter_never_replaces_the_scope_it_narrows_inside(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**A filter narrows within the caller's row scope and can never widen it.** The scope,
    the tool's own pin and the asker's filter are three predicates conjoined into one WHERE
    clause, and the failure this catches is the filter arriving in place of the scope rather
    than beside it: a caller restricted to one prefix asking about a row outside it would
    then be asking the database for that row.

    A person who holds a prefix scope asks for a row in the other prefix, and both fragments
    have to survive into the statement. Asserted on the rendered SQL because the double
    ignores the statement, so the rows coming back say nothing about what was asked for.

    Delete this and `pinned.and_(caller).and_(asked)` can lose its middle term, which reads in
    review as a simplification and is a table handed to a stranger.

    Task ids: M32.5.2.1"""
    assert ask(client, "u_prefix", terms=["sku:MNT-2002"]).status_code == 200

    sql = rendered(rows.seen[0])
    # Matched up to the prefix value rather than through it, because the renderer doubles the
    # LIKE wildcard for the driver's paramstyle and a test spelling `%%` would be asserting on
    # that rendering rather than on the predicate.
    assert "->> 'sku' LIKE 'WEB-" in sql, "the caller's own scope is no longer in the statement"
    assert "->> 'sku' = 'MNT-2002'" in sql, "the asker's filter is no longer in the statement"
    assert "entity = 'price_list'" in sql, "the tool's pin is no longer in the statement"


def test_whether_a_column_may_be_filtered_on_moves_with_the_caller_and_not_the_entity(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**The rule the whole filter rests on, stated as the thing that distinguishes it from
    the wrong implementation.** A filter is answered by which rows come back, so a filter on
    a column the response withholds reads that column one comparison at a time. The bound is
    therefore the compiled projection, which is what this caller may read, and never the
    entity's classification, which is what the entity has.

    Both are in reach at the route, and the difference between them is invisible in a diff:
    a check written against `classification.columns()` would admit `cost` for everybody,
    because the price list does classify a cost. Two people ask the same question about the
    same column here, and only the one entitled to read it reaches the database.

    Delete this and the projection check can move to the route, or be widened to the
    classification, with every other test in this section still passing, because every other
    test looks at one person.

    Task ids: M32.5.2.1"""
    withheld = ask(client, "u_narrow", terms=[f"cost:{CANARY_COST}"])

    assert withheld.status_code == 200
    assert RecordPage.model_validate(withheld.json()).items == []
    assert rows.asked == 0, "a filter on a column this caller cannot read reached the database"
    assert CANARY_COST not in withheld.text, "the filter was echoed back into the answer"

    entitled = ask(client, "u_wide", terms=[f"cost:{CANARY_COST}"])

    assert entitled.status_code == 200
    assert rows.asked == 1, "the caller entitled to the column cannot filter on it either"
    assert f"= '{CANARY_COST}'" in rendered(rows.seen[0])


def test_a_filter_on_a_column_the_caller_may_not_read_is_answered_as_one_that_does_not_exist(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**Denied and absent are one answer, at the column level.** A refusal naming the column
    would confirm the column exists, and a caller comparing a refusal against an empty page
    reads a table's schema off the difference one guess at a time. It is the same argument
    the 404 above makes about entities, one level down.

    Compared as whole bodies rather than field by field, because a difference introduced later
    will be in whichever field a narrower comparison did not name. There is no trace id to
    exclude: both answers are a 200 with a page on it.

    The two also take the same path through the row plane, which is the part a body comparison
    cannot see: neither is fetched, so they are not distinguishable by how long they took
    either.

    Delete this and an unreadable column can start answering 422, or an unknown one can start
    answering 404, and either difference maps the columns of every entity in the install.

    Task ids: M32.5.2.1"""
    denied = ask(client, "u_narrow", terms=[f"cost:{CANARY_COST}"])
    absent = ask(client, "u_narrow", terms=[f"no_such_column:{CANARY_COST}"])

    assert denied.status_code == absent.status_code == 200
    assert denied.content == absent.content
    assert rows.asked == 0, "one of the two was fetched, so they differ in more than the body"


def test_a_filtered_empty_page_is_the_unfiltered_empty_page_byte_for_byte(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**An empty page must not say why it is empty**, and the two ways of arriving at one
    here could hardly be more different: the first fetches two rows and loses both to the
    redactor, the second never opens the query at all.

    `brain.core.redaction.ChannelPayload` suppresses the source, the timestamp and the
    truncation flag when nothing survives, which is what makes this comparison possible at
    all: a page naming the source it found nothing in would answer a question nobody may ask,
    and a `fetched_at` would differ between any two requests whatever else did.

    Delete this and a filtered empty page can grow a field the unfiltered one lacks - a
    `filtered: true`, a source, an echo of the term - and each of them tells a caller that
    their filter was the reason, which tells them there was something for it to exclude.

    Task ids: M32.5.2.1"""
    unfiltered = ask(client, "u_elsewhere")
    assert rows.asked == 1, "the unfiltered page was never fetched, so it is empty for a reason"

    filtered = ask(client, "u_elsewhere", terms=[f"cost:{CANARY_COST}"])
    assert rows.asked == 1, "the filtered page was fetched, so the two are not the same event"

    assert unfiltered.status_code == filtered.status_code == 200
    assert RecordPage.model_validate(unfiltered.json()).items == []
    assert unfiltered.content == filtered.content


def test_two_terms_naming_one_column_answer_nothing_rather_than_either_of_them(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """Conjunction means what it means everywhere else in this system. Two terms on one column
    are two clauses on one field, which `is_unsatisfiable` reads as a contradiction and
    compiles to an empty page, exactly as two conflicting grants on one field do.

    Pinned rather than left to be discovered, because the obvious improvement is to fold
    repeated terms into a membership test, and that would make the asker's own narrowing the
    one place in this system where combining two predicates produces a wider one.

    Delete this and repeated terms can quietly become an OR, at which point a filter is no
    longer something that only narrows.

    Task ids: M32.5.2.1"""
    response = ask(client, "u_wide", terms=["sku:WEB-1001", "sku:MNT-2002"])

    assert response.status_code == 200
    assert RecordPage.model_validate(response.json()).items == []
    assert rows.asked == 0, "an impossible predicate was sent to the database anyway"


def test_a_malformed_term_is_refused_identically_whatever_entity_was_asked_for(
    client: TestClient, rows: UnfilteredRows
) -> None:
    """**A refusal that depended on the entity would be the enumeration this route's single
    404 exists to prevent.** The term's grammar is declared on the parameter, so a malformed
    one is refused before the handler runs and therefore before anything has been read about
    which entity was named. Put the check in the handler instead, after the classification
    lookup, and the same malformed term answers 422 for an entity that exists and 404 for one
    that does not, which is a two-request oracle over the whole install.

    Compared as whole bodies, and the bodies are `HTTPValidationError` rather than `ErrorBody`
    on purpose: the console mirrors the declared grammar for the same reason it mirrors the
    declared limit bounds, so a person never meets this. It is what a hand-edited address
    gets.

    Delete this and the parameter's pattern can move into the handler, which reads as putting
    the validation where the error message can be friendlier.

    Task ids: M32.5.2.1"""
    known = ask(client, "u_wide", terms=["NOPE"])
    unknown = ask(client, "u_wide", entity="finance_ledger", terms=["NOPE"])

    assert known.status_code == unknown.status_code == 422
    assert known.content == unknown.content
    assert rows.asked == 0


def test_more_terms_than_the_route_declares_are_refused_and_the_declared_number_is_accepted(
    client: TestClient,
) -> None:
    """A repeated parameter is otherwise a statement whose size the caller chooses, and every
    term is one more conjunct in the WHERE clause.

    The number is read out of the document rather than written here, so that a bound which is
    declared and not enforced fails this, and so does one enforced and not declared. Both
    halves are asked: a request carrying exactly the declared number is answered, which is the
    sibling that stops the bound being tightened until nothing passes.

    Delete this and `maxItems` becomes decoration, or the enforced number drifts below the
    declared one and a client built from the document starts getting 422s it was told it
    would not.

    Task ids: M32.5.2.1"""
    declared = int(declared_query_parameters()[FILTER_PARAM]["schema"]["maxItems"])

    at_the_bound = ask(client, "u_wide", terms=[f"sku:v{i}" for i in range(declared)])
    over_it = ask(client, "u_wide", terms=[f"sku:v{i}" for i in range(declared + 1)])

    assert at_the_bound.status_code == 200
    assert over_it.status_code == 422
    # The constant and the document are the same number by construction; what this catches is
    # a declaration that stopped reading the constant, not a change to its value.
    assert declared == MAX_FILTERS


def test_the_filter_bounds_are_wide_enough_for_the_entities_this_application_ships() -> None:
    """The bounds asserted against something outside themselves, which is the only way a
    figure can be checked at all: a test comparing a constant with the constant it imported
    is green for every value that constant could hold.

    A caller must be able to filter on every column of the widest entity this application
    registers, or the bound is a limit on the product rather than on a statement's size. And
    one term must be able to carry the longest column name `Clause` accepts plus a separator
    plus a value, or a legitimately long column name is unfilterable by everybody.

    Delete this and either bound can be tightened to a number that looks sensible and refuses
    a question somebody is entitled to ask.

    Task ids: M32.5.2.1"""
    from brain.tools.startup import BUILT_IN_ROW_ENTITIES

    widest = max(len(c.columns()) for c in BUILT_IN_ROW_ENTITIES)

    assert widest <= MAX_FILTERS, "a caller cannot filter on every column they can already see"
    assert len(longest_field_name()) + len(FILTER_SEPARATOR) < MAX_FILTER_TERM_LENGTH, (
        "the longest column name Clause accepts cannot be named in one term, so a column "
        "somebody is entitled to read is unfilterable by everybody"
    )


def test_the_route_builds_no_statement_out_of_a_formatted_string() -> None:
    """M15.1.3's second half, applied to the module that now takes a value from a query string
    and turns it into a predicate. A filter value is data and is bound as a parameter by
    `compile_where`; nothing here composes a fragment, and this is what says so about the
    source rather than about the intention.

    Read over the parsed syntax tree rather than over the text, because a check that searched
    for the word SQL would be satisfied by the docstring above explaining the rule.

    Delete this and the first `text(f"...")` written in a route goes in unremarked, at which
    point the value the caller typed is syntax.

    Task ids: M32.5.2.1"""
    from brain.knowledge.rows import assert_no_sql_is_built_by_interpolation

    assert_no_sql_is_built_by_interpolation(api_routes)


def test_a_filtered_page_carries_no_count_of_what_the_filter_removed(client: TestClient) -> None:
    """The count rule and the filter meeting, which is where somebody adds "3 matching" and
    it reads as helpful. A count behind a permission predicate is the hidden-item leak
    whatever it is called, and a count beside a filter is that leak with a reason attached:
    the reader now knows both how many there are and that their filter was what removed the
    rest.

    Asserted on the raw body rather than on the parsed model, because a model whose default is
    None reads as absent whatever the server sent.

    Delete this and `total` becomes an obviously useful addition the moment a grid has a
    filter box on it.

    Task ids: M32.5.2.1"""
    response = ask(client, "u_wide", terms=["sku:WEB-1001"])

    assert response.json()["total"] is None
    for banned in ("matching", "matches", "withheld", "hidden", "of 2", "1 of"):
        assert banned not in response.text.lower()
