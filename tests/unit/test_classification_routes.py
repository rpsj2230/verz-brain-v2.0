"""A column classification over HTTP: who may read it, who may review a change, and what a
change does.

Driven through the real application. The token machinery, the identity directory and the key
source are imported from `tests/unit/test_api_routes.py` rather than rebuilt, for the reason
`tests/unit/test_routing_routes.py` gives about the same borrowing: what is under test here
is a capability rather than an identity, so a second copy of a JWS builder would be a second
place for a token to be minted subtly differently, and a test failing for that reason looks
like a permission bug.

**There is no session factory anywhere in this file, and that is a property rather than a
saving.** This router opens nothing, reads nothing from `app.state` beyond the gate, and
stores nothing, because a `TableClassification` is a constant compiled into the process.
`test_a_review_is_answered_on_a_process_with_no_database` is where that is asserted instead
of described.

**The two refusals are compared body to body rather than status to status.** A caller who
may not read a classification and a caller asking about an entity nothing classifies must be
answered identically, and the way that rule breaks is never a different status: it is a
different sentence, or a header, or a word in a log that reaches a body.

**The widening arithmetic is checked against `project_row` rather than against a second
closure written here.** `test_the_columns_a_review_calls_exposed_are_the_columns_a_caller_
would_newly_see` asks the shipped projection what a caller actually sees before and after,
which is the only check that cannot be satisfied by two copies of the same mistake.

Task ids: M7.5.3
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from brain.api import API_PREFIX
from brain.app import Settings, create_app
from brain.classification_routes import (
    CLASSIFICATION_READ,
    CLASSIFICATION_WRITE,
    WIDENING_CHANGES,
    Change,
    ColumnEdit,
    ReviewView,
    changes_between,
    newly_reachable,
    replacing,
    review,
    view_of,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.errors import Absent
from brain.core.field_policy import Classification
from brain.core.scope import Clause, Op, Scope
from brain.gate.admission import ASSURANCE_VERBS, CHANNEL_VERBS, Assurance
from brain.gate.context import Channel
from brain.knowledge.columns import PRICE_LIST, ColumnRule, TableClassification, project_row
from tests.unit.test_api_routes import (
    Directory,
    Keys,
    NoCache,
    Versions,
    token_for,
    verifier,
)

ENTITY = PRICE_LIST.entity
CLASSIFICATIONS = f"{API_PREFIX}/classifications"

#: One row of the price list, in the shape `brain.knowledge.columns.project_row` reads. The
#: same row `tests/unit/test_knowledge_columns.py` uses, because the cross-check below
#: compares this file's answer with that module's behaviour and a different row would make
#: the two incomparable for a reason that is not the one under test.
ROW: Mapping[str, Any] = {
    "sku": "PKG-CARE-1",
    "name": "Care Plan",
    "sell_price": 1200,
    "cost": 400,
    "margin": 800,
}

WHOLE = Scope.unrestricted()
ELSEWHERE = Scope(clauses=(Clause(field="department", op=Op.EQ, value="finance"),))


def _grant(value: str, scope: Scope) -> Grant:
    return Grant(capability=Capability(value=value), scope=scope)


#: What each of `test_api_routes`' people holds over a classification.
#:
#: `u_wide` holds the write capability and not the read one, which is the oracle case: a
#: review compares the proposal with the classification that stands, so answering one to a
#: caller who may not read the classification would hand it to them a probe at a time.
#:
#: `u_elsewhere` holds a grant over the price list's own rows and nothing over the policy
#: about them, which is the other confusion available here.
GRANTS: dict[str, tuple[Grant, ...]] = {
    "u_none": (),
    "u_narrow": (_grant(CLASSIFICATION_READ.value, WHOLE),),
    "u_prefix": (_grant(CLASSIFICATION_READ.value, ELSEWHERE),),
    "u_wide": (_grant(CLASSIFICATION_WRITE.value, WHOLE),),
    "u_admin": (
        _grant(CLASSIFICATION_READ.value, WHOLE),
        _grant(CLASSIFICATION_WRITE.value, WHOLE),
    ),
    "u_elsewhere": (
        _grant("read:price_list.cost", WHOLE),
        _grant("read:price_list.margin", WHOLE),
    ),
}

#: An `amr` a Keycloak session carries when a second factor was used. A literal rather than a
#: value read out of `SECOND_FACTOR_METHODS`, for the reason `test_routing_routes` gives about
#: its own copy: reading the constant would compare it against itself and stay green for any
#: set at all.
SECOND_FACTOR: Mapping[str, object] = {"amr": ["otp"]}

#: A rule identical to the one `PRICE_LIST` ships for `cost`, spelled out rather than read
#: off the classification. Read off it, every assertion below would compare the module with
#: itself and would stay green for whatever the module happened to hold.
COST_AS_IT_STANDS = ColumnEdit(
    required_capability="read:price_list.cost",
    classification=Classification.CONFIDENTIAL,
    derived_from=["margin", "sell_price"],
)


class Store:
    """A `brain.gate.resolve.EntitlementStore` over `GRANTS`."""

    def load(self, principal_id: str) -> EntitlementSet:
        return EntitlementSet(principal_id=principal_id, grants=GRANTS[principal_id])


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
        store=Store(),
        cache=NoCache(),
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The real application, with a gate and deliberately without a database.

    `create_app` produced everything else, the router registration under test included: a
    test that mounted the router itself would prove the routes work and not that they are
    served. Nothing sets `app.state.db_sessions`, which is what every deployment of this
    system is today and is also all this router ever needs.
    """
    app: FastAPI = create_app(Settings(env="development"))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.gate = _wiring()
        yield c


def read(
    c: TestClient, pid: str, *, entity: str = ENTITY, claims: Mapping[str, object] | None = None
) -> Response:
    token = token_for(pid, claims=SECOND_FACTOR if claims is None else claims)
    response: Response = c.get(
        f"{CLASSIFICATIONS}/{entity}", headers={"authorization": f"Bearer {token}"}
    )
    return response


def propose(
    c: TestClient,
    pid: str,
    *,
    entity: str = ENTITY,
    column: str = "cost",
    body: Mapping[str, object] | None = None,
    claims: Mapping[str, object] | None = None,
) -> Response:
    token = token_for(pid, claims=SECOND_FACTOR if claims is None else claims)
    sent = COST_AS_IT_STANDS.model_dump() if body is None else dict(body)
    response: Response = c.post(
        f"{CLASSIFICATIONS}/{entity}/columns/{column}/review",
        headers={"authorization": f"Bearer {token}"},
        json=sent,
    )
    return response


def app_of(c: TestClient) -> FastAPI:
    """The application behind a client, typed.

    `TestClient.app` is annotated as a bare ASGI callable, which is true of the protocol and
    useless here: the fixture put a `FastAPI` there two lines ago and the assertions below
    read its document and its state. A cast rather than an assertion, because proving the
    structural match buys nothing that the fixture has not already established.
    """
    return cast(FastAPI, c.app)


def refusal(answer: Response) -> dict[str, Any]:
    """One refusal body, with the trace id taken out and its presence asserted instead.

    The trace id is the one field two refusals are allowed to differ in, and
    `brain.api.A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT` says why: it is minted per
    request and says nothing about what was asked for or who asked. Dropping the value rather
    than the key means a body that stopped carrying one at all still fails a comparison.
    """
    body = dict(answer.json())
    assert "trace_id" in body, "a refusal with no trace id leaves a person nothing to quote"
    body["trace_id"] = "<per request>"
    return body


def ents(*caps: str) -> EntitlementSet:
    """One caller's entitlement over the price list's own columns."""
    return EntitlementSet(
        principal_id="p_reader",
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
    )


# --------------------------------------------------------------- who may read one
def test_a_caller_holding_no_grant_over_a_classification_is_told_there_is_none(
    client: TestClient,
) -> None:
    """The taxonomy's own sentence, with nothing about classification in it.

    A refusal saying "you may not read the classification of the price list" confirms that
    this installation has a price list and that somebody thought its columns worth
    classifying, which is two facts about the estate handed to a caller holding nothing.

    Delete this and the route can grow a helpful message, or answer 403, either of which
    tells an unentitled caller the surface exists and is worth asking about again."""
    answer = read(client, "u_none")

    assert answer.status_code == 404
    assert answer.json()["message"] == Absent.public_message
    assert "classification" not in answer.text.lower()
    assert "price" not in answer.text.lower()


def test_a_caller_holding_the_read_grant_is_answered_the_whole_classification(
    client: TestClient,
) -> None:
    """The sibling of the refusal above, which a route that refused everybody would pass.

    Asserted against `PRICE_LIST` itself rather than against a list written here, so a
    column added to the classification and dropped by the view fails.

    Delete this and every refusal in this file is satisfied by a route that answers 404 to
    the entitled caller too."""
    answer = read(client, "u_narrow")

    assert answer.status_code == 200
    assert [column["column"] for column in answer.json()["columns"]] == list(PRICE_LIST.columns())


def test_a_grant_over_the_rows_of_a_table_does_not_reach_the_policy_over_its_columns(
    client: TestClient,
) -> None:
    """`read:price_list.cost` is permission to see a number. It is not permission to read
    the rule that decides who sees it, which is a statement about everybody rather than
    about one row, and the two are separate capabilities on purpose.

    Delete this and the natural simplification is to let any read over the entity answer
    here, at which point everybody who may see the sell price learns what it takes to see
    the cost."""
    answer = read(client, "u_elsewhere")

    assert answer.status_code == 404
    assert answer.json()["message"] == Absent.public_message


def test_an_entity_nothing_classifies_answers_exactly_what_an_unentitled_caller_gets(
    client: TestClient,
) -> None:
    """DENIED and ABSENT, indistinguishable, on the surface where telling them apart maps
    the installation: a caller who could tell "there is no such classification" from "you
    may not read it" could learn which tables this company classifies by trying names.

    The whole body is compared rather than the status, because the status is the half that
    never drifts. A sentence, a header or a field appearing in one and not the other is the
    shape this actually breaks in.

    Delete this and the two refusals drift the first time somebody adds a helpful word to
    one of them."""
    stranger = read(client, "u_none")
    missing = read(client, "u_narrow", entity="finance_ledger")

    assert stranger.status_code == missing.status_code
    assert refusal(stranger) == refusal(missing)


def test_a_classification_is_answered_whole_however_narrow_the_grants_scope_is(
    client: TestClient,
) -> None:
    """`u_prefix` holds the read capability in a scope naming a department the price list
    has never heard of, and is answered every column. A classification is one decision
    rather than a filtered list, so there is nothing on the page for a count to disclose by
    subtraction and no per-column refusal to explain.

    Delete this and somebody adds a scope predicate over the columns, at which point two
    people comparing screens can read each other's grants off the difference."""
    narrow = read(client, "u_prefix")
    wide = read(client, "u_narrow")

    assert narrow.status_code == 200
    assert narrow.json()["columns"] == wide.json()["columns"]


def test_the_view_carries_every_field_of_the_rule_it_copies(client: TestClient) -> None:
    """The response is a hand-written copy of `ColumnRule`, so the thing that keeps it
    honest is a comparison with the rule itself. Asserted for every column rather than for
    one, and in both directions, because a copy loop that dropped `derived_from` would
    answer four correct columns and one silent lie about the fifth.

    Delete this and a field added to `ColumnRule` never reaches a console, which is the
    failure nobody notices: the screen looks complete."""
    answered = {column["column"]: column for column in read(client, "u_narrow").json()["columns"]}

    assert set(answered) == set(PRICE_LIST.columns())
    for name, shown in answered.items():
        rule = PRICE_LIST.rule_for(name)
        assert rule is not None
        assert shown["required_capability"] == rule.required_capability.value
        assert shown["classification"] == rule.classification.value
        assert shown["derived_from"] == sorted(rule.derived_from)


# ------------------------------------------------------------- who may review one
def test_the_review_capability_carries_a_verb_a_service_account_cannot_exercise() -> None:
    """The one line of this whole surface that decides who may change what other people
    see. `admin` is the verb `CHANNEL_VERBS` withholds from API and `ASSURANCE_VERBS`
    withholds from anything below STRONG, so choosing it here is what makes a
    client-credentials token and a password-only session both unable to reach the review,
    without a line of either rule being restated in this module.

    Asserted against the two ceilings rather than against the string `"admin"`, so a
    capability repointed at a verb that happens to spell differently and is admitted
    everywhere fails here.

    Delete this and `admin:field_classification` can quietly become
    `write:field_classification`, which reads like a tidy-up and hands the estate's
    disclosure rules to every service account in it."""
    verb = CLASSIFICATION_WRITE.verb

    assert verb not in CHANNEL_VERBS[Channel.API]
    assert verb in CHANNEL_VERBS[Channel.CONSOLE]
    assert verb not in ASSURANCE_VERBS[Assurance.AUTHENTICATED]
    assert verb in ASSURANCE_VERBS[Assurance.STRONG]
    assert CLASSIFICATION_READ.verb in ASSURANCE_VERBS[Assurance.AUTHENTICATED]


def test_a_password_only_session_cannot_have_a_change_reviewed(client: TestClient) -> None:
    """The assurance ceiling, driven rather than asserted. `u_admin` holds both capabilities
    and signs in without a second factor, so the admin verb is not in reach and the review
    is the refusal a stranger gets. The same token still reads the classification, which is
    what makes this a test of the ceiling rather than of a broken token.

    Delete this and the verb on the capability becomes decoration: nothing would notice a
    review answered to somebody who typed a password an hour ago."""
    refused = propose(client, "u_admin", claims={})
    still_reads = read(client, "u_admin", claims={})

    assert refused.status_code == 404
    assert refused.json()["message"] == Absent.public_message
    assert still_reads.status_code == 200


def test_a_token_with_no_session_cannot_have_a_change_reviewed(client: TestClient) -> None:
    """The channel ceiling, driven. A token with no `sid` is what a client-credentials grant
    looks like, `channel_for` reads it as API, and `CHANNEL_VERBS` gives API no admin verb.
    A change to what everybody may see, made by a secret in a configuration file, is a change
    nobody made.

    Delete this and a service account can review, which is one small step from a service
    account applying."""
    refused = propose(client, "u_admin", claims={**SECOND_FACTOR, "sid": None})

    assert refused.status_code == 404
    assert refused.json()["message"] == Absent.public_message


def test_a_reader_who_may_not_review_is_refused_in_the_words_a_stranger_gets(
    client: TestClient,
) -> None:
    """A caller who may read a classification and not change one gets the answer a caller
    who may do neither gets, so the reply says nothing about which half they are missing.

    Delete this and the natural refusal for a reader is a 403 with an explanation, which
    tells them exactly which capability to ask for and tells anybody who steals their token
    the same."""
    reader = propose(client, "u_narrow")
    stranger = propose(client, "u_none")

    assert reader.status_code == stranger.status_code
    assert refusal(reader) == refusal(stranger)


def test_a_caller_who_may_review_and_may_not_read_is_refused(client: TestClient) -> None:
    """The oracle this closes: a review answers what changed about a column, so a caller who
    could review without reading would learn the rule that stands one proposal at a time.
    `u_wide` holds the write capability alone and is refused.

    Delete this and the read capability becomes advisory, because everything it guards is
    reachable through the route that guards the other one."""
    answer = propose(client, "u_wide")

    assert answer.status_code == 404
    assert answer.json()["message"] == Absent.public_message


def test_a_caller_holding_both_grants_has_the_change_reviewed(client: TestClient) -> None:
    """The positive case, without which every refusal above is satisfied by a route that
    refuses everybody.

    Delete this and the surface can be dead and still look correct."""
    answer = propose(client, "u_admin")

    assert answer.status_code == 200
    assert answer.json()["entity"] == ENTITY
    assert answer.json()["column"] == "cost"


def test_the_editable_flag_is_true_exactly_when_a_review_would_be_answered(
    client: TestClient,
) -> None:
    """`editable` is presentation, and presentation that lies is worse than none: a console
    drawing an editor for somebody whose every review is refused has invented a permission,
    and one hiding it from somebody entitled has invented a refusal. Driven for every person
    in the table rather than for two, so a flag that is simply always false fails.

    Delete this and the flag drifts from the capability it reports, silently, because
    nothing on a screen looks different until somebody presses save."""
    seen: set[bool] = set()
    for pid in ("u_narrow", "u_prefix", "u_admin"):
        answered = read(client, pid)
        assert answered.status_code == 200
        flag = bool(answered.json()["editable"])
        seen.add(flag)
        assert flag is (propose(client, pid).status_code == 200), pid

    assert seen == {True, False}


def test_an_entity_nothing_classifies_is_refused_before_a_proposal_is_looked_at(
    client: TestClient,
) -> None:
    """A review of an entity that does not exist here answers the refusal rather than a
    verdict, so a caller cannot map the installation by proposing rules for names they
    guessed. The body sent is a valid one, so what is under test is the entity check rather
    than the body's shape.

    Delete this and the review becomes the enumeration route this module refused to write."""
    answer = propose(client, "u_admin", entity="finance_ledger")

    assert answer.status_code == 404
    assert answer.json()["message"] == Absent.public_message


# --------------------------------------------------------------- what a review says
def test_a_review_is_answered_on_a_process_with_no_database(client: TestClient) -> None:
    """Nothing is stored, so nothing needs a pool, and the review answers on a process that
    has none.

    This is the assertable form of "no audit row is written": a route that had begun writing
    one would need a session, and there is none to be had.

    **The absence is created rather than assumed, and CI is why.** An earlier version asserted
    that the fixture had left no session factory on the app, which is true on a laptop with no
    `DATABASE_URL` and false in CI, where the workflow sets one and the lifespan builds a real
    sessionmaker. It passed locally and failed on the runner, which is the worst place to find
    out and the reason the condition is now constructed: whatever the environment gave the
    app, this removes it, so the test means the same thing everywhere.

    Delete this and a later change can quietly acquire a database dependency, at which point
    the surface starts failing on exactly the deployments it was written to serve."""
    state = app_of(client).state
    had = getattr(state, "db_sessions", None)
    if had is not None:
        del state.db_sessions
    try:
        assert getattr(state, "db_sessions", None) is None

        answer = propose(client, "u_admin")

        assert answer.status_code == 200
    finally:
        if had is not None:
            state.db_sessions = had


def test_a_rule_identical_to_the_one_that_stands_is_reported_as_no_change(
    client: TestClient,
) -> None:
    """The zero of this arithmetic. Every other assertion about a change is satisfied by a
    comparison that reports everything as changed, and this is the one that is not.

    The body is written out rather than read off `PRICE_LIST`, so this compares the route
    with the classification rather than the classification with itself.

    Delete this and `changes_between` can return every member of the enum for every input
    and the widening tests still pass."""
    body = review(ENTITY, "cost", COST_AS_IT_STANDS)

    assert body.changes == []
    assert body.widens is False
    assert body.exposed == []
    assert body.would_not_load == ""
    assert body.epoch_now == body.epoch_after


def test_dropping_a_derivation_is_a_widening_and_the_review_names_what_it_exposes(
    client: TestClient,
) -> None:
    """The case the whole feature is about. `cost` is declared reconstructable from
    `sell_price` and `margin`; removing that declaration does not change who holds
    `read:price_list.cost` and does change what everybody who lacks it sees, because the
    closure stops withholding `margin`. A review that reported only the rule that changed
    would say `derivation_dropped` about `cost` and leave the reader to work out that the
    consequence lands on a different column.

    Delete this and the one edit an administrator is most likely to make while believing it
    harmless stops being called a widening."""
    answer = propose(
        client,
        "u_admin",
        body={
            "required_capability": "read:price_list.cost",
            "classification": Classification.CONFIDENTIAL.value,
            "derived_from": [],
        },
    )

    assert answer.status_code == 200
    assert answer.json()["changes"] == [Change.DERIVATION_DROPPED.value]
    assert answer.json()["widens"] is True
    assert answer.json()["exposed"] == ["margin"]


def test_the_columns_a_review_calls_exposed_are_the_columns_a_caller_would_newly_see() -> None:
    """The cross-check, and the only assertion here that cannot be satisfied by two copies of
    one mistake. `newly_reachable` runs the shipped closure; this asks the shipped
    *projection* what a real caller actually receives, before and after, and insists the two
    agree.

    The caller holds every price-list capability except the cost one, which is the shape the
    classification was written for.

    Delete this and the review's arithmetic is checked only against itself, which is the
    failure this repository has been bitten by four times: a constant compared with a
    constant is green for every value it could hold."""
    reader = ents(
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        "read:price_list.margin",
    )
    loosened = replacing(
        PRICE_LIST,
        ColumnRule(
            column="cost",
            required_capability=Capability(value="read:price_list.cost"),
            classification=Classification.CONFIDENTIAL,
        ),
    )

    before = set(project_row(PRICE_LIST, ROW, entitlement=reader).values)
    after = set(project_row(loosened, ROW, entitlement=reader).values)

    assert after - before == set(newly_reachable(PRICE_LIST, loosened))
    assert after - before == {"margin"}


def test_the_only_column_of_a_new_classification_is_reported_as_newly_reachable() -> None:
    """The edge of the closure sweep, and the one case the caller-missing-nothing pass
    exists for.

    `newly_reachable` compares what a caller short of exactly one column sees, and on a
    classification with a single column that sweep removes the only column from both sides
    and compares two empty sets. Everywhere else the whole-set comparison is subsumed, which
    mutation established rather than a comment asserting it: dropping the extra pass changes
    no other answer in this file.

    The classification going from nothing to one column is not reachable through the route
    today, because `classification_for` answers the price list or nothing, and this function
    is exported and general.

    Delete this and the extra pass becomes a line with no reason behind it, which is the line
    a future reader removes to make the sweep read cleanly."""
    nothing_classified = TableClassification(entity=ENTITY, rules=())
    one_column = TableClassification(
        entity=ENTITY,
        rules=(
            ColumnRule(
                column="sku",
                required_capability=Capability(value="read:price_list.sku"),
                classification=Classification.INTERNAL,
            ),
        ),
    )

    assert newly_reachable(nothing_classified, one_column) == ("sku",)
    assert newly_reachable(one_column, nothing_classified) == ()


def test_a_dropped_derivation_is_a_change_the_epoch_does_not_record() -> None:
    """**A finding about `brain.core.field_policy`, held here rather than fixed here.**
    `FieldPolicy.epoch` digests the entity, the field, the capability, the classification and
    the count declaration, and not `derived_from`. So the edit above changes what every
    caller short of the cost capability sees and leaves the epoch identical, which means the
    answer cache would go on serving rows computed under the old closure. That is the exact
    failure the epoch's own docstring describes for `counts`, with the sign reversed.

    Asserted rather than described, so the day somebody adds `derived_from` to the digest
    this test fails and the person changing it reads the paragraph above.

    Delete this and the gap goes back to being invisible, and the review's two epochs start
    reading as proof that a proposal is not a change."""
    answer = review(
        ENTITY,
        "cost",
        ColumnEdit(
            required_capability="read:price_list.cost",
            classification=Classification.CONFIDENTIAL,
            derived_from=[],
        ),
    )

    assert answer.widens is True
    assert answer.epoch_now == answer.epoch_after


def test_classifying_a_column_nothing_classified_is_a_widening(client: TestClient) -> None:
    """Default-deny is the rule `brain.core.field_policy` rests on: a field no rule mentions
    is withheld from everybody. So the act of classifying one is the act of making it
    reachable, whatever capability the new rule names, and a review that called it "added"
    without calling it a widening would be describing the safest-looking edit on the screen.

    Delete this and the first column somebody classifies is reported as a neutral change."""
    answer = propose(
        client,
        "u_admin",
        column="discount",
        body={
            "required_capability": "read:price_list.discount",
            "classification": Classification.RESTRICTED.value,
            "derived_from": [],
        },
    )

    assert answer.json()["changes"] == [Change.ADDED.value]
    assert answer.json()["widens"] is True
    assert answer.json()["exposed"] == ["discount"]


def test_lowering_a_classification_widens_even_when_no_column_becomes_reachable() -> None:
    """**The one case that pins `WIDENING_CHANGES` rather than the closure.** `widens` is the
    syntactic verdict or the closure's, and on almost every edit both fire, so a membership
    removed from `WIDENING_CHANGES` would go unnoticed. `sku` is the column that separates
    them: nothing is derived from it and it derives from nothing, so lowering it exposes no
    column at all and the verdict has to come from the change itself.

    A classification never permits, so lowering one hands nobody a capability. What it does
    widen is which channels may carry the value and how long an artifact built from it is
    kept, and it moves the column's place in the tie-break that decides which input the
    closure withholds when a derivation has to be broken. That is exposure.

    Both directions, because a comparison that called every classification change a widening
    would pass the first half on its own and would train a reader to ignore the verdict.

    Delete this and the sensitivity level becomes a label nothing reads, and the whole
    widening verdict collapses onto the closure, which does not see this edit."""
    lowered = review(
        ENTITY,
        "sku",
        ColumnEdit(
            required_capability="read:price_list.sku",
            classification=Classification.PUBLIC,
            derived_from=[],
        ),
    )
    raised = review(
        ENTITY,
        "sku",
        ColumnEdit(
            required_capability="read:price_list.sku",
            classification=Classification.RESTRICTED,
            derived_from=[],
        ),
    )

    assert lowered.changes == [Change.LESS_SENSITIVE]
    assert lowered.exposed == []
    assert lowered.widens is True
    assert raised.changes == [Change.MORE_SENSITIVE]
    assert raised.exposed == []
    assert raised.widens is False


def test_a_changed_capability_is_reported_without_a_direction() -> None:
    """Whether `read:finance.sell_price` reaches more people than `read:price_list.
    sell_price` is a question about who holds what, and this module has no grant store in
    front of it. Calling it a narrowing would be a guess in the dangerous direction and
    calling it a widening would make every rename alarming, so it is reported as a change
    and left there.

    The membership assertion is against `WIDENING_CHANGES` rather than against the boolean
    alone, because the boolean would also be false for a comparison that noticed nothing.

    Delete this and somebody adds the guess, in whichever direction they happen to prefer."""
    answer = review(
        ENTITY,
        "sell_price",
        ColumnEdit(
            required_capability="read:finance.sell_price",
            classification=Classification.INTERNAL,
            derived_from=[],
        ),
    )

    assert answer.changes == [Change.CAPABILITY]
    assert Change.CAPABILITY not in WIDENING_CHANGES
    assert answer.widens is False


def test_two_changes_to_one_column_are_both_reported() -> None:
    """A rule can move in two directions at once, and a verdict that reported the first
    would hide whichever the reader needed. Sorted, so two identical proposals answer
    identically rather than in whatever order the comparison happened to run.

    Delete this and `changes_between` can return early on the first difference it finds,
    which reads as a tidy guard clause and loses the second half of every interesting
    edit."""
    found = changes_between(
        PRICE_LIST.rule_for("cost"),
        ColumnRule(
            column="cost",
            required_capability=Capability(value="read:finance.cost"),
            classification=Classification.INTERNAL,
            derived_from=frozenset({"sell_price"}),
        ),
    )

    assert list(found) == sorted(
        [Change.CAPABILITY, Change.LESS_SENSITIVE, Change.DERIVATION_DROPPED]
    )


def test_a_proposal_that_would_not_construct_is_reviewed_rather_than_refused(
    client: TestClient,
) -> None:
    """A classification that raises on construction leaves the previous rules in place while
    a person believes they changed something, which is the worst outcome this surface has.
    So it is a finding with the classification layer's own words in it, not a 422 about a
    malformed body, and every consequence field is empty because a rule that does not load
    has none.

    Three shapes, each refused by a different module: the self-derivation by `ColumnRule`,
    the unknown input by `TableClassification`, and the non-read verb by `FieldRule`. All
    three arrive here as an answer.

    Delete this and the first of them becomes a 500, because nothing else catches it."""
    for body in (
        {"required_capability": "read:price_list.cost", "derived_from": ["cost"]},
        {"required_capability": "read:price_list.cost", "derived_from": ["nothing_like_that"]},
        {"required_capability": "write:price_list.cost", "derived_from": []},
    ):
        answer = propose(
            client,
            "u_admin",
            body={"classification": Classification.CONFIDENTIAL.value, **body},
        )

        assert answer.status_code == 200, body
        assert answer.json()["would_not_load"] != "", body
        assert answer.json()["changes"] == [], body
        assert answer.json()["widens"] is False, body
        assert answer.json()["epoch_after"] == "", body


def test_the_body_cannot_name_the_entity_or_the_column_the_address_already_names(
    client: TestClient,
) -> None:
    """Both are path segments, and a value that arrives twice is a value two readers
    disagree about. `extra="forbid"` is what turns "the console does not send one" from a
    habit into an answer: a body carrying either is refused naming the key rather than
    accepted and quietly ignored, so a console cannot believe it changed a column the
    address does not name.

    Delete this and the model can go back to ignoring unknown keys, at which point a console
    bug becomes a policy bug."""
    for extra in ({"entity": "finance_ledger"}, {"column": "margin"}, {"epoch_now": "x"}):
        answer = propose(client, "u_admin", body={**COST_AS_IT_STANDS.model_dump(), **extra})

        assert answer.status_code == 422, extra


def test_a_review_carries_no_handle_of_anything_stored(client: TestClient) -> None:
    """Nothing is saved, so nothing may look saved. A field named like the identifier of a
    stored proposal, or a time at which one was applied, is the first thing a console renders
    as a receipt, and a receipt for a thing that did not happen is worse than no editor at
    all.

    Asserted over the model's declared fields, so a field added later has to be argued for
    here.

    Delete this and the response grows an `id` the day somebody sketches a store, and the
    console starts saying "saved" months before anything is."""
    banned = ("id", "created_at", "applied_at", "applied", "saved", "revision", "version")

    for name in banned:
        assert name not in ReviewView.model_fields

    assert set(ReviewView.model_fields) == {
        "entity",
        "column",
        "would_not_load",
        "changes",
        "widens",
        "exposed",
        "epoch_now",
        "epoch_after",
    }


# ------------------------------------------------------------------ the mounted set
def test_nothing_mounted_here_can_change_a_classification(client: TestClient) -> None:
    """**The claim the console has to be able to make honestly.** There is no store behind
    this surface, so there is no route that writes one, and the way to check that is the
    application's own document rather than anybody's recollection: every classification path
    declares GET and nothing else, except the review, which declares POST and stores nothing.

    Asserted over what is mounted rather than over this module's source, because a second
    router could mount a write at the same prefix and nothing in this file would notice.

    Delete this and a save can appear without the screen that describes the absence of one
    being corrected."""
    app = app_of(client)
    paths = {
        path: sorted(methods)
        for path, methods in app.openapi()["paths"].items()
        if path.startswith(CLASSIFICATIONS)
    }

    assert paths == {
        f"{CLASSIFICATIONS}/{{entity}}": ["get"],
        f"{CLASSIFICATIONS}/{{entity}}/columns/{{column}}/review": ["post"],
    }


def test_no_route_here_lists_what_this_installation_classifies(client: TestClient) -> None:
    """A list of classified entities is a map of what this company keeps and treats as
    sensitive, handed over for the price of one capability. `brain.api_routes` refuses the
    same enumeration one level down and this surface has no better claim to it, so a person
    names the entity and is answered or refused identically either way.

    Delete this and the obvious convenience arrives, because a console that makes somebody
    type a name looks unfinished."""
    app = app_of(client)

    assert CLASSIFICATIONS not in app.openapi()["paths"]
    assert f"{CLASSIFICATIONS}/" not in app.openapi()["paths"]


def test_the_view_is_not_a_page_and_carries_no_count(client: TestClient) -> None:
    """A classification is every column of one table, answered entire. A page shape over it
    would carry `total` and `next_cursor`, and a field that exists is a field one line away
    from a screen, which is how "showing 3 of 5" arrives on a surface where three of the
    five are the confidential ones.

    Delete this and the next person to make this consistent with the other list endpoints
    makes it consistent with the one rule they must not copy."""
    body = read(client, "u_narrow").json()

    assert set(body) == {"entity", "columns", "epoch", "editable"}
    assert "total" not in read(client, "u_narrow").text
    assert "next_cursor" not in read(client, "u_narrow").text


def test_the_view_reports_columns_in_a_sorted_order_rather_than_the_declared_one() -> None:
    """Two classifications holding the same rules answer identically. An order following the
    rule tuple would put the order somebody declared them in onto the wire, where a reader
    could take it for significance and where a merge could change it without changing a rule.

    Built from a shuffled classification rather than from `PRICE_LIST`, because `PRICE_LIST`
    is already close to sorted and a test over it would pass with the sort removed.

    Delete this and the console's grid silently starts reflecting source order."""
    shuffled = replacing(
        PRICE_LIST,
        ColumnRule(
            column="cost",
            required_capability=Capability(value="read:price_list.cost"),
            classification=Classification.CONFIDENTIAL,
            derived_from=frozenset({"margin", "sell_price"}),
        ),
    )

    assert [column.column for column in view_of(shuffled, editable=False).columns] == sorted(
        PRICE_LIST.columns()
    )
    assert [rule.column for rule in shuffled.rules] != sorted(PRICE_LIST.columns())
