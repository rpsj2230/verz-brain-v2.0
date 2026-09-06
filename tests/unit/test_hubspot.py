"""The HubSpot connector, tested against the recordings rather than against a HubSpot key.

A CRM is the connector where the storage rules nearly run out of things to allow, so most of
what is pinned here is a refusal with a positive sibling beside it. Five properties do the
work, and each has a wrong version that reviews cleanly.

**The projection of a contact has no name on it.** Not because somebody forgot to add one:
because HubSpot splits a name into two properties, `manifest.projectability` admits one label
per entity kind, and a stored list of names is a copy of the client's contact list. The tests
assert the shape of that projection directly, because "we kept the useful bits" is exactly the
sentence that widens a projection one field at a time.

**A deal amount is a contract value in another vocabulary.** `contract_value` and `margin` are
on the platform denylist and `amount` is not, so nothing outside the module refuses it, and
declared as a status enum it passes all five clauses. One test proves exactly that and then
proves this module refuses it anyway. The recorded canary for `client.contract_value` is used
as the deal amount there, because a leak should be greppable rather than plausible.

**An association is a second question, and the specification is what enforces it.** The test
that matters is not "the connector chooses not to inline"; it is that a document declaring an
inlined association is refused by `brain.connectors.rest.load_spec` before anything is built.

**Absent, refused and unreachable stay three answers.** `HUBSPOT-200-empty` is the recording
the whole corpus keeps for that purpose, and it belongs to this connector.

**The properties argument is the silent one.** HubSpot answers a call that does not name its
properties with a perfectly well-formed record containing none of them, so the connector looks
correct and returns rows with holes in them. One test demonstrates the failure and then
asserts that the call this module builds cannot make it.

What is deliberately not tested here, stated rather than hidden: the corpus records exactly
one HubSpot response and it is a success. There is no recorded HubSpot 429, 401 or 5xx, so the
failure branches below are exercised against status codes rather than against recordings. The
classification itself is `brain.connectors.throttle.classify`'s, which is shared with the
sources whose failures are recorded.

Task ids: M11.6.6
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.change_signal import DeletionCheck
from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    FetchRequest,
    HealthState,
    assert_fetches_only,
)
from brain.connectors.federation import FANOUT_BUDGET_MS, FailureReason, FanOutPlan, SourceCall
from brain.connectors.hubspot import (
    CEILING_NAME,
    CONNECTOR_NAME,
    CURSOR_POLL_INTERVAL,
    ENTITY_ASSOCIATION,
    ENTITY_CLIENT,
    ENTITY_CONTACT,
    ENTITY_DEAL,
    MAX_ASSOCIATION_HOPS,
    MAX_PAGE_SIZE,
    RECONCILIATION_INTERVAL,
    RETRY_AFTER_WHEN_UNSTATED,
    AssociationEdge,
    HubSpotConnection,
    HubSpotError,
    HubSpotOutcome,
    HubSpotReply,
    assert_declarations_agree,
    assert_federated_only,
    assert_hops_within_cap,
    assert_policy_merges_with,
    association_edges,
    ceiling_is_verified,
    connector_fetch,
    day_ceiling,
    default_arguments,
    health,
    hubspot_field_policy,
    hubspot_manifest,
    hubspot_retry_delay,
    interpret,
    mapped_targets,
    operation_for,
    parse_hubspot_timestamp,
    projected_record,
    projection_for,
    refresh_promise,
    requested_properties,
    retry_after,
    spec_document,
    subscription,
    traversal_plan,
)
from brain.connectors.manifest import (
    ChangeSignal,
    FieldShape,
    HotUse,
    ManifestError,
    ProjectedField,
    failed_clauses,
    manifest_digest,
    projectability,
)
from brain.connectors.projection import assess_staleness
from brain.connectors.rest import RestSpecError, load_spec
from brain.connectors.throttle import (
    CallOutcome,
    UnmeasuredSourceError,
    connector_breaker,
    record_outcome,
)
from brain.connectors.transports import FieldMapping
from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import IdentityMode
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.projection import MAX_PROJECTED_FIELDS, ProjectionRefusedError, is_forbidden
from brain.core.scope import Op
from brain.gate.provenance import Freshness, StalenessHorizon
from brain.ops.limits import MAX_BACKOFF_SECONDS, connector_ceiling
from brain.ops.secrets import SecretRef, VaultRole
from brain.tools.fetch import FetchedBytes
from tests.fixtures.cassettes import CASSETTES, Source, limit_for
from tests.fixtures.company import CANARIES

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

PORTAL = "24681357"
#: One digit longer. A prefix match would admit it, and it is a different company.
OTHER_PORTAL = "246813570"

REF = SecretRef(path="connectors/hubspot_ro", role=VaultRole.APPLICATION)

#: A deal amount is a client's contract value spelled the way HubSpot spells it, so the canary
#: the invariant suite already protects for `client.contract_value` is the right one to put in
#: that field: a leak is then greppable rather than plausible.
MONEY_CANARY = CANARIES["client.contract_value"]

#: A horizon short enough that "just read" and "read yesterday" land in different states.
HORIZON = StalenessHorizon(live_for=timedelta(minutes=15), stale_after=timedelta(hours=24))


class Resolver:
    """Every name answers with one public address. Modelled on the one in test_fetch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return ["93.184.216.34"]


class Fetcher:
    """A fetcher that answers with one body and records every connection it was asked for.

    The recording is the point: the portal tests assert this list is empty, which is a stronger
    claim than asserting an exception was raised. A refusal after the request was assembled has
    already resolved a name and may already have spent a call.
    """

    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode("utf-8")
        self.connected: list[str] = []

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str:
        del address, max_bytes
        self.connected.append(url)
        return FetchedBytes(body=self.body, final_url=url)


def cassette(cid: str) -> Any:
    """One recording by id, so a test names the recording it is written against."""
    return next(c for c in CASSETTES if c.cid == cid)


#: The one HubSpot response anybody has recorded: a genuine absence, as distinct from a
#: refusal and from an outage.
EMPTY_CRM = cassette("HUBSPOT-200-empty")

COMPANY_ROW: dict[str, Any] = {
    "id": "88",
    "properties": {
        "name": "SNM Construction Pte Ltd",
        "domain": "snm.example",
        "lifecyclestage": "customer",
        "hubspot_owner_id": "9911",
        "hs_lastmodifieddate": "1794700800000",
    },
}

DEAL_ROW: dict[str, Any] = {
    "id": "4471",
    "properties": {
        "dealname": "SNM website revamp",
        "amount": MONEY_CANARY,
        "dealstage": "contractsent",
        "pipeline": "default",
        "closedate": "1794700800000",
        "hubspot_owner_id": "9911",
    },
}

#: A contact row with an association inlined onto it, in both shapes that happens: a nested
#: `associations` envelope, and the far record's own fields flattened onto this one. The
#: second is the dangerous one, because `name` and `domain` are real classified fields on a
#: client and the redactor has no way to know they belong to a different record.
CONTACT_ROW_WITH_AN_INLINED_COMPANY: dict[str, Any] = {
    "id": "301",
    "properties": {
        "firstname": "Wei Ling",
        "lastname": "Tan",
        "jobtitle": "Operations Manager",
        "email": "weiling@snm.example",
        "phone": "+65 6555 0100",
        "lifecyclestage": "customer",
        "associatedcompanyid": "88",
        "hubspot_owner_id": "9911",
        "hs_lastmodifieddate": "1794700800000",
    },
    "associations": {"companies": {"results": [{"id": "88", "type": "contact_to_company"}]}},
    "name": "SNM Construction Pte Ltd",
    "domain": "snm.example",
}


def connection() -> HubSpotConnection:
    return HubSpotConnection(portal_id=PORTAL)


def manifest() -> Any:
    return hubspot_manifest(connection(), ref=REF)


def operation(entity: str = ENTITY_CLIENT) -> Any:
    return operation_for(entity, resolver=Resolver())


def body_of(*rows: Mapping[str, Any]) -> dict[str, Any]:
    """The `results` envelope both the list and the search endpoints answer with."""
    return {"results": list(rows)}


def reply_for(status: int, body: Any = None, **overrides: Any) -> HubSpotReply:
    """Interpret one response, so a failure test names the status it is written against."""
    arguments: dict[str, Any] = {"status": status, "body": body, "fetched_at": ""}
    arguments.update(overrides)
    return interpret(operation(), **arguments)


# ------------------------------------ a CRM is mostly the denylist (M11.4.2, M11.4.4)
def test_a_projected_contact_carries_no_field_that_names_the_person() -> None:
    """**The finding this connector exists to state.** Work a HubSpot contact through the
    storage rules and what survives is a lifecycle stage, two join keys and a timestamp: it
    can be counted, filtered and joined, and it cannot be shown to anybody, because every
    field that would say who the person is may not be stored. That is the honest outcome of
    federating a CRM, and it means any answer naming a person is a live fetch every time.

    Delete this and a name creeps back into the projection one field at a time, each addition
    looking reasonable, until the projection is a copy of the client's contact list."""
    projection = projection_for(ENTITY_CONTACT, connection())
    assert projection.field_names == ("lifecycle_stage", "company_id", "owner_id", "updated_at")
    assert [f for f in projection.fields if f.shape is FieldShape.LABEL] == []
    for personal in ("first_name", "last_name", "job_title", "email", "phone"):
        assert personal not in projection.field_names


def test_the_projection_still_keeps_the_pointers_that_make_a_client_findable() -> None:
    """The positive sibling for every refusal below. A projection that stored nothing would
    satisfy all of them and make the fast lane useless, which is the failure that gets the
    twelve-field cap widened rather than respected. A company name is a company's, not a
    person's, so it is the one label this connector keeps.

    Delete this and a projection builder returning an empty tuple is green."""
    projection = projection_for(ENTITY_CLIENT, connection())
    assert projection.field_names == (
        "name",
        "domain",
        "lifecycle_stage",
        "owner_id",
        "updated_at",
    )
    assert sum(1 for f in projection.fields if f.shape is FieldShape.LABEL) == 1
    assert len(projection.fields) <= MAX_PROJECTED_FIELDS


def test_a_contacts_means_of_contact_is_neither_mapped_nor_classified() -> None:
    """These are the fields the platform denylist does spell, and this connector relies on
    that rather than restating it. It maps none of them either, so nothing about how to reach
    a person travels through this process at all, and nothing classifies them, so default-deny
    withholds them from everybody.

    Delete this and a mapping added for a "send them the proposal" feature is unopposed, and
    the field arrives, is traced, and is withheld only by a rule somebody could relax."""
    for name in ("email", "phone", "mobile", "address"):
        assert is_forbidden(name) is True
    assert set(mapped_targets(ENTITY_CONTACT)) == {
        "first_name",
        "last_name",
        "job_title",
        "lifecycle_stage",
        "company_id",
        "owner_id",
        "updated_at",
    }
    policy = hubspot_field_policy()
    for name in ("email", "phone", "mobilephone", "address"):
        assert policy.rule_for(ENTITY_CONTACT, name) is None


def test_a_persons_name_arrives_in_two_halves_and_one_half_passes_every_platform_clause() -> None:
    """**Why the refusal has to live in this module.** HubSpot splits a name into `firstname`
    and `lastname`. Declaring both as labels fails the pointer clause, which is the platform
    catching it; declaring only `lastname` passes all five clauses, is not on the denylist, and
    a reviewer sees nothing wrong. Half a person's name is still a person's name.

    The vendor's own camel-case spelling is refused by the same guard, and the refusal names
    the field back in the spelling it was handed rather than in the normalised one: a message
    that renames the caller's field sends them looking through their declaration for a string
    they never typed.

    Delete this and dropping this module's own guard leaves a contact's surname projectable
    with every platform rule still green."""
    first = ProjectedField(name="first_name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,))
    last = ProjectedField(name="last_name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,))
    both = projectability(first, signal=ChangeSignal.UPDATED_SINCE, label_count=2, field_count=6)
    assert [v.clause for v in failed_clauses(both)] == ["pointer-shaped"]

    alone = projectability(last, signal=ChangeSignal.UPDATED_SINCE, label_count=1, field_count=5)
    assert failed_clauses(alone) == ()
    assert is_forbidden("last_name") is False
    assert is_forbidden("lastName") is False
    with pytest.raises(ProjectionRefusedError, match="last_name"):
        assert_federated_only(ENTITY_CONTACT, ["last_name"])
    with pytest.raises(ProjectionRefusedError, match="lastName"):
        assert_federated_only(ENTITY_CONTACT, ["lastName"])


def test_a_contacts_name_and_job_title_are_returned_live_and_classified() -> None:
    """The other half of the same decision, and the one that keeps the connector useful. The
    name is refused from storage and is still returned to somebody holding the capability,
    which `brain.core.field_policy` names as the ordinary case rather than the exception: a
    field can be returnable and unstorable at the same time.

    Delete this and refusing the field everywhere passes every storage test above while making
    the CRM connector unable to answer the question it exists for."""
    policy = hubspot_field_policy()
    for name in ("first_name", "last_name", "job_title"):
        rule = policy.rule_for(ENTITY_CONTACT, name)
        assert rule is not None
        assert rule.classification is Classification.INTERNAL
        assert rule.required_capability.verb == "read"
        with pytest.raises(ProjectionRefusedError):
            assert_federated_only(ENTITY_CONTACT, [name])


# ---------------------------------------------------- money in a CRM (M11.4.4, M4.2.1)
def test_a_deal_amount_passes_every_platform_clause_and_is_refused_here() -> None:
    """`contract_value` and `margin` are on the platform's permanent denylist and `amount` is
    not, which is a difference in vocabulary rather than in kind. Declared as a status enum
    with a filter use it passes all five clauses of `manifest.projectability`.

    Delete this and dropping this module's guard leaves a client's contract value projectable
    under the vendor's own spelling, with every platform rule still green."""
    disguised = ProjectedField(
        name="amount", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.SORT)
    )
    verdicts = projectability(
        disguised, signal=ChangeSignal.UPDATED_SINCE, label_count=1, field_count=6
    )
    assert failed_clauses(verdicts) == ()
    assert is_forbidden("amount") is False
    assert is_forbidden("contract_value") is True
    with pytest.raises(ProjectionRefusedError, match="amount"):
        assert_federated_only(ENTITY_DEAL, ["amount"])


def test_the_deal_amount_is_fetched_and_never_stored() -> None:
    """**The canary test.** The deal carries the `client.contract_value` canary as its amount.
    It has to arrive, because it is the answer people ask for, and it must not reach the
    projection, because a stored figure is quoted as current long after the deal closed at a
    different number.

    The projection is built from the declared fields rather than copied from the row, which is
    the only version that survives somebody adding a mapping target.

    Delete this and a projection that copies the mapped row and removes what it does not want
    stores the amount the first time the mapping changes."""
    projected_rows = operation(ENTITY_DEAL).project(body_of(DEAL_ROW))
    assert projected_rows[0]["amount"] == MONEY_CANARY
    record = projected_record(ENTITY_DEAL, projected_rows[0], last_seen_at=NOW)
    assert record is not None
    assert "amount" not in record.fields
    assert MONEY_CANARY not in json.dumps(dict(record.fields), default=str)


def test_a_pipeline_figure_is_confidential_and_a_payroll_figure_is_restricted() -> None:
    """**Said plainly, because the classification is a decision rather than a habit.**
    RESTRICTED is where a salary sits, and a salary is one identified person's private
    financial position. A deal amount is an organisation's commercial figure whose legitimate
    audience is most of the commercial side of the business. Classifying it RESTRICTED would
    make RESTRICTED mean "money" instead of "a person's private data", and the level stops
    discriminating the moment it has to be granted to everybody in sales.

    Delete this and either reading passes: CONFIDENTIAL quietly becoming INTERNAL puts client
    pricing in front of everybody with any deal grant, and RESTRICTED quietly spreading makes
    the level meaningless for the payroll figures it exists for."""
    rule = hubspot_field_policy().rule_for(ENTITY_DEAL, "amount")
    assert rule is not None
    assert rule.classification is Classification.CONFIDENTIAL
    assert rule.required_capability == Capability(value="read:deal.amount")
    assert Classification.CONFIDENTIAL.rank < Classification.RESTRICTED.rank
    assert Classification.INTERNAL.rank < Classification.CONFIDENTIAL.rank


# ------------------------------------------- an association is a second question (M11.5.2)
def test_a_response_that_inlines_an_association_is_refused_by_the_specification() -> None:
    """**The enforcement, and it is not a promise in a comment.** A list response declaring
    associations has two arrays in it, and `load_spec` refuses such a document because which
    array held the records would be decided by key order. So the connector cannot inline an
    association even if somebody wants it to: the transport refuses the shape.

    Delete this and adding `associations` to the schema looks like a convenience, and the
    resulting rows carry another record's fields under this record's tag."""
    document = json.loads(json.dumps(spec_document()))
    schema = document["paths"]["/crm/v3/objects/companies"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    listed = load_spec(document, resolver=Resolver()).operation("getCompanies")
    assert listed.records_at == "results"
    schema["properties"]["associations"] = {"type": "array", "items": {"type": "object"}}
    with pytest.raises(RestSpecError, match="decided by key order"):
        load_spec(document, resolver=Resolver())


def test_an_inlined_association_never_reaches_the_record_it_was_attached_to() -> None:
    """The second defence, against a source that flattens the far record's fields onto this
    one. `name` and `domain` are real classified fields on a client, so an inlined pair is not
    unclassified and default-deny cannot save us: it is misattributed, and the redactor has no
    way to know the value belongs to a different record with a different visibility.

    A fresh mapping over declared targets is what makes it harmless.

    Delete this and a projection that copied the row, or a mapping that passed unknown fields
    through, would return a company's name as a contact's."""
    rows = operation(ENTITY_CONTACT).project(body_of(CONTACT_ROW_WITH_AN_INLINED_COMPANY))
    assert "name" not in rows[0]
    assert "domain" not in rows[0]
    assert "associations" not in rows[0]
    record = projected_record(ENTITY_CONTACT, rows[0], last_seen_at=NOW)
    assert record is not None
    assert "SNM Construction Pte Ltd" not in json.dumps(dict(record.fields), default=str)


def test_an_edge_has_nowhere_to_put_the_record_on_the_far_end() -> None:
    """An edge is a fact about a record the caller already holds; the record on the far end is
    a separate question with its own entitlement check. The class has no field that could hold
    the far record, so "attach the company while we are here" is unexpressible rather than
    discouraged, and the mapping reads two values and no property of the associated object.

    Delete this and a `properties` field added to the edge for convenience turns every
    traversal back into the inlining the whole design refuses."""
    edges = association_edges(
        from_entity=ENTITY_CONTACT,
        from_id="301",
        to_entity=ENTITY_CLIENT,
        rows=operation(ENTITY_ASSOCIATION).project(
            body_of({"id": "88", "type": "contact_to_company"})
        ),
    )
    assert edges == (
        AssociationEdge(
            from_entity=ENTITY_CONTACT,
            from_id="301",
            to_entity=ENTITY_CLIENT,
            to_id="88",
            kind="contact_to_company",
        ),
    )
    assert mapped_targets(ENTITY_ASSOCIATION) == ("kind",)
    with pytest.raises(TypeError):
        AssociationEdge(  # type: ignore[call-arg]
            from_entity=ENTITY_CONTACT,
            from_id="301",
            to_entity=ENTITY_CLIENT,
            to_id="88",
            properties={"name": "SNM Construction Pte Ltd"},
        )


def test_an_edge_whose_far_end_cannot_be_named_is_dropped_rather_than_followed() -> None:
    """A generated or empty id cannot be cited, cannot be pointed at by a request-access route
    and cannot be matched to the same record on the next fetch, so following it would mean
    guessing which record was meant.

    Delete this and an association response with a malformed row produces an edge pointing at
    nothing, and the traversal fetches whatever that resolves to."""
    assert (
        association_edges(
            from_entity=ENTITY_DEAL,
            from_id="4471",
            to_entity=ENTITY_CLIENT,
            rows=({"kind": "deal_to_company"},),
        )
        == ()
    )
    with pytest.raises(HubSpotError, match="to_id"):
        AssociationEdge(
            from_entity=ENTITY_DEAL, from_id="4471", to_entity=ENTITY_CLIENT, to_id="  "
        )


def test_a_traversal_is_two_dependent_calls_and_a_third_hop_cannot_be_afforded() -> None:
    """**The cap is arithmetic rather than a judgement.** The fan-out budget is two federated
    timeouts, so an object fetch plus a dependent association fetch is exactly the budget and a
    second hop is over it. The dependency is what makes the plan cost 1,600ms rather than 800:
    the association call cannot start until the first has answered.

    Delete this and a plan that declared the two calls independent looks affordable, runs them
    at once, and the second one has no id to ask about."""
    plan = traversal_plan(entity=ENTITY_CONTACT, to_entity=ENTITY_CLIENT)
    assert plan.waves() == ((ENTITY_CONTACT,), (f"{ENTITY_CONTACT}_to_{ENTITY_CLIENT}",))
    assert plan.critical_path_ms() == FANOUT_BUDGET_MS
    plan.assert_within()

    third = FanOutPlan(
        calls=(
            *plan.calls,
            SourceCall(
                call_id="second_hop",
                connector=CONNECTOR_NAME,
                entity=ENTITY_ASSOCIATION,
                depends_on=(f"{ENTITY_CONTACT}_to_{ENTITY_CLIENT}",),
            ),
        )
    )
    assert third.critical_path_ms() > FANOUT_BUDGET_MS
    with pytest.raises(Exception, match="critical path"):
        third.assert_within()


def test_a_traversal_deeper_than_one_hop_is_refused_before_a_plan_exists() -> None:
    """The same rule caught earlier, for the caller that never builds a plan. An agent asking
    for a three-step walk should be refused where it asked rather than by a budget check it
    might not reach.

    Delete this and an uncapped traversal reaches the whole CRM from one readable record, and
    nothing refuses it until somebody happens to assemble a plan."""
    assert MAX_ASSOCIATION_HOPS == 1
    assert_hops_within_cap(MAX_ASSOCIATION_HOPS)
    with pytest.raises(HubSpotError, match="deeper than"):
        assert_hops_within_cap(MAX_ASSOCIATION_HOPS + 1)
    with pytest.raises(HubSpotError):
        assert_hops_within_cap(-1)


def test_an_association_fetch_that_names_no_object_never_reaches_the_source() -> None:
    """The association operation takes the object in its path, so a call that does not name
    one has no address to build. Asserted on the fetcher rather than on the exception: a
    refusal after the request was assembled has already resolved a name and may already have
    spent a call.

    Delete this and a traversal with a missing id fetches the collection endpoint instead,
    which returns somebody else's associations and looks like data."""
    fetcher = Fetcher(body_of())
    fetch = connector_fetch(
        connection(),
        ENTITY_ASSOCIATION,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    with pytest.raises(RestSpecError, match="requires"):
        fetch(FetchRequest(entity=ENTITY_ASSOCIATION, filters=(("objectType", "contacts"),)))
    assert fetcher.connected == []


def test_a_named_association_fetch_reaches_the_object_it_was_asked_about() -> None:
    """The positive sibling. A connector that refused every traversal would satisfy the test
    above and make the CRM's own structure unreachable, which is the half of a CRM that
    answers "which deals belong to this client".

    Delete this and refusing every association call unconditionally is green."""
    fetcher = Fetcher(body_of({"id": "88", "type": "contact_to_company"}))
    fetch = connector_fetch(
        connection(),
        ENTITY_ASSOCIATION,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    result = fetch(
        FetchRequest(
            entity=ENTITY_ASSOCIATION,
            filters=(
                ("objectType", "contacts"),
                ("objectId", "301"),
                ("toObjectType", "companies"),
            ),
        )
    )
    assert [r.id for r in result.records] == ["88"]
    assert fetcher.connected == [
        "https://api.hubapi.com/crm/v3/objects/contacts/301/associations/companies"
    ]


# ------------------------------------------------- the properties argument (M11.1.3)
def test_a_call_that_did_not_name_its_properties_returns_records_with_nothing_on_them() -> None:
    """**The silent failure, demonstrated and then closed.** HubSpot answers a call that does
    not name its properties with a well-formed record whose properties bag is empty, so every
    mapped path resolves to absent, every projected field is dropped, and the connector returns
    rows that are correct, empty and useless. The call this module builds names every property
    the mapping reads, derived from the mapping rather than from a second list.

    Delete this and dropping the properties argument passes every other test in this file,
    because every other test supplies its own body."""
    bare = operation().project(body_of({"id": "88", "properties": {}}))
    record = projected_record(ENTITY_CLIENT, bare[0], last_seen_at=NOW)
    assert record is not None
    assert dict(record.fields) == {}

    url = operation().url_for(dict(default_arguments(ENTITY_CLIENT)))
    for vendor_property in requested_properties(ENTITY_CLIENT):
        assert vendor_property in url, f"{vendor_property} is mapped and never asked for"
    assert requested_properties(ENTITY_ASSOCIATION) == ()


def test_the_property_request_is_derived_from_the_mapping_rather_than_kept_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derivation is the guard, so the check is that nobody replaced it with a list. A
    hand-maintained property list is a list that goes one field out of date, and the field it
    is behind on arrives absent from a record that is otherwise perfect.

    Delete this and a hard-coded property string that has drifted from the mapping installs
    cleanly."""
    assert_declarations_agree()
    from brain.connectors import hubspot as module

    monkeypatch.setattr(module, "default_arguments", lambda entity: {"properties": "name"})
    with pytest.raises(HubSpotError, match="does not ask the source for them"):
        assert_declarations_agree()


def test_a_page_larger_than_the_vendor_allows_is_refused_rather_than_clamped() -> None:
    """A silently clamped page is an under-count that reads as a complete answer, which is the
    same mistake `brain.ops.limits.SEARCH_CAP_IS_NOT_A_PAGE_SIZE` describes from the other
    side: the caller asked for 500, got 100, and nothing said so.

    Delete this and clamping looks helpful."""
    fetcher = Fetcher(body_of(COMPANY_ROW))
    fetch = connector_fetch(
        connection(),
        ENTITY_CLIENT,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    with pytest.raises(HubSpotError, match="over the vendor"):
        fetch(FetchRequest(entity=ENTITY_CLIENT, limit=MAX_PAGE_SIZE + 1))
    assert fetcher.connected == []


def test_a_limit_and_a_cursor_become_the_vendors_own_paging_parameters() -> None:
    """The positive sibling, and the one thing a per-vendor connector adds over the generic
    adapter: `brain.connectors.rest.as_fetch` refuses a limit and a cursor because paging is a
    parameter name only the vendor's specification knows, and this module knows it.

    Delete this and a caller's page request is dropped, which returns the first page for every
    page anybody asks for and looks exactly like a source with only one page."""
    fetcher = Fetcher(body_of(COMPANY_ROW))
    fetch = connector_fetch(
        connection(),
        ENTITY_CLIENT,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    result = fetch(FetchRequest(entity=ENTITY_CLIENT, limit=25, cursor="eyJvZmZzZXQiOjEwMH0"))
    assert [r.id for r in result.records] == ["88"]
    assert "limit=25" in fetcher.connected[0]
    assert "after=eyJvZmZzZXQiOjEwMH0" in fetcher.connected[0]


# ------------------------------------------------------------ the portal pin (M11.2.3)
def test_a_connection_admits_its_own_portal_and_no_other() -> None:
    """A connector row naming portal A while the vault path holds a token for portal B is the
    case nothing else would notice: every row that came back would be real. Membership is
    exact rather than by prefix, which is why 24681357 does not admit 246813570.

    Delete this and a prefix match, or no match at all, passes."""
    pinned = connection()
    assert pinned.admits(PORTAL) is True
    assert pinned.admits(OTHER_PORTAL) is False
    with pytest.raises(HubSpotError, match="pinned to one HubSpot account"):
        pinned.assert_admits(OTHER_PORTAL)


def test_a_scope_that_narrows_nothing_is_refused_at_connect() -> None:
    """A connector connected to everything the credential reaches has the credential's blast
    radius, and narrowing it later does not un-fetch anything.

    Delete this and a portal id read from an empty configuration value installs a connector
    scoped to whatever the token can see."""
    for selector in ("", "*", "all"):
        with pytest.raises(ConnectorContractError):
            HubSpotConnection(portal_id=selector)


def test_a_fetch_for_another_portal_never_reaches_the_transport() -> None:
    """**Asserted on the fetcher, not on the exception.** A check that runs after the request
    was assembled has already resolved a name and may already have spent a call, and the call
    is what cannot be taken back.

    Delete this and a refusal moved below the address builder still passes a test that only
    looked for the exception."""
    fetcher = Fetcher(body_of(COMPANY_ROW))
    fetch = connector_fetch(
        connection(),
        ENTITY_CLIENT,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    with pytest.raises(HubSpotError):
        fetch(FetchRequest(entity=ENTITY_CLIENT, filters=(("portal", OTHER_PORTAL),)))
    assert fetcher.connected == []


def test_a_fetch_that_names_the_pinned_portal_reaches_the_source() -> None:
    """The positive sibling, and it also proves the portal filter is removed rather than passed
    on: the specification declares no `portal` parameter, so a filter that survived would be
    refused by the address builder and the failure would look like a scope problem.

    Delete this and a connector that refuses every portal, including its own, is green."""
    fetcher = Fetcher(body_of(COMPANY_ROW))
    fetch = connector_fetch(
        connection(),
        ENTITY_CLIENT,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    result = fetch(FetchRequest(entity=ENTITY_CLIENT, filters=(("portal", PORTAL),)))
    assert [r.id for r in result.records] == ["88"]
    assert len(fetcher.connected) == 1


def test_a_fetch_that_names_no_portal_inherits_the_pin() -> None:
    """The connection already decided which account this is. Making every caller repeat it
    gives them somewhere to get it wrong, and the wrong version reads another company's
    pipeline under the right company's name.

    Delete this and requiring the filter on every call passes, and every existing caller breaks
    at the same time."""
    fetcher = Fetcher(body_of(COMPANY_ROW))
    fetch = connector_fetch(
        connection(),
        ENTITY_CLIENT,
        fetcher=fetcher,
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    assert fetch(FetchRequest(entity=ENTITY_CLIENT)).records


# --------------------------------------------------------------- the contract (M11.1.1)
def test_the_fetch_can_never_be_handed_the_callers_grants() -> None:
    """A connector returns everything it fetched and the redactor removes what is not covered.
    That is why this module does not check reach on a traversal either: a connector that could
    would be a connector holding the caller's grants, and there would be two places answering a
    permission question with the permissive one winning silently.

    Delete this and a wrapper that took an entitlement set "just to check the association" is
    installable."""
    fetch = connector_fetch(
        connection(),
        ENTITY_CONTACT,
        fetcher=Fetcher(body_of(CONTACT_ROW_WITH_AN_INLINED_COMPANY)),
        resolver=Resolver(),
        fetched_at=NOW.isoformat(),
    )
    assert_fetches_only(fetch)
    assert EntitlementSet(principal_id="u_weiling") is not None


def test_a_connection_that_kept_a_token_is_refused_at_construction() -> None:
    """A credential held between calls is a value no rotation can invalidate and no revocation
    can reach. Checked over annotations, so it fails on the first construction rather than on
    the first expiry.

    Delete this and a `private_app_token` attribute added for convenience survives review."""

    class Leaky(HubSpotConnection):
        private_app_token: str = ""

    with pytest.raises(ConnectorContractError, match="private_app_token"):
        Leaky(portal_id=PORTAL)


# ------------------------------------------------------- the ceiling (M11.3.1, M11.3.5)
def test_the_ceiling_is_read_from_the_verified_table_and_never_invented() -> None:
    """**A finding rather than a feature.** The corpus records 10,000 calls a day per app per
    account and `brain.ops.limits` does not carry that figure, so there is nothing verified for
    this connector to run against and `throttle.ceiling_for` refuses. Refusing is the intended
    behaviour: a number restated here would sit in a console beside three that were measured
    and look exactly like them.

    The branch means this test keeps working the day somebody verifies the figure and adds the
    row, which is the only edit that should be needed.

    Delete this and a hard-coded ConnectorLimit in this module reads as a measurement."""
    assert limit_for(Source.HUBSPOT).calls == 10_000
    assert manifest().ceiling == CEILING_NAME
    verified = connector_ceiling(CEILING_NAME)
    if verified is None:
        assert ceiling_is_verified() is False
        with pytest.raises(UnmeasuredSourceError):
            day_ceiling(manifest())
    else:
        assert ceiling_is_verified() is True
        assert day_ceiling(manifest()) == verified


def test_the_ceiling_this_connector_runs_against_is_its_own_and_never_another_sources() -> None:
    """**Written because a mutation walked straight through the test above.** That test reads
    `connector_ceiling(CEILING_NAME)` and branches on the answer, and it compares the manifest
    against `CEILING_NAME` rather than against anything independent, so both sides move
    together: point `CEILING_NAME` at "freshdesk" and the lookup returns a real measured limit,
    the `else` branch is taken, every assertion holds, and the suite is green.

    What would actually have happened is that HubSpot began running against Freshdesk's
    verified 100 a minute. Not a crash, not a refusal, and not a number anybody typed here:
    this connector's entire argument for refusing to invent a ceiling would have been replaced
    by silently borrowing somebody else's measurement, and `ceiling_is_verified()` would have
    flipped to True to say so.

    A constant asserted against itself proves nothing about its value. The independent fact is
    that the ceiling a connector runs against must be the connector's own name, which is what
    makes `connector_ceiling` returning None an honest statement about HubSpot rather than an
    accident of spelling.

    Delete this and the ceiling can be repointed at any measured source in the table without a
    single test noticing."""
    assert CEILING_NAME == CONNECTOR_NAME
    assert manifest().ceiling == CONNECTOR_NAME


def test_a_wait_is_never_shorter_than_the_source_asked_for() -> None:
    """The source knows when its own window reopens and we do not. The platform caps a backoff
    at 300 seconds, which is right for a window that refills in sixty and wrong for the daily
    allowance HubSpot publishes, so coming back early spends a call on a refusal we were told
    about.

    Delete this and the platform's cap silently shortens every HubSpot wait."""
    asked = MAX_BACKOFF_SECONDS * 3
    waited = hubspot_retry_delay(retry_after_seconds=asked, consecutive_refusals=1)
    assert waited >= asked


def test_a_source_that_asked_for_nothing_still_gets_the_platforms_backoff() -> None:
    """The positive sibling, and the one that fails silently. `backoff_seconds` multiplies
    the figure the source asked for, so a refusal carrying no `Retry-After` header multiplies
    zero and returns zero however many refusals came before it: the doubling looks like a
    backoff and produces none. A client that comes back immediately is the one that turns a
    burst into a rate limit and then keeps it there.

    The unstated case travels as None rather than as a zero somebody converted, which is why
    `retry_after` refuses to invent a figure and this accepts what it returns.

    Delete this and `max` collapsing to the source's own figure is green, and every 429 with
    no header on it is retried at once."""
    waited = hubspot_retry_delay(retry_after_seconds=0.0, consecutive_refusals=4)
    assert waited > 0.0
    assert waited >= RETRY_AFTER_WHEN_UNSTATED
    unstated = hubspot_retry_delay(retry_after_seconds=retry_after({}), consecutive_refusals=1)
    assert unstated == RETRY_AFTER_WHEN_UNSTATED


def test_a_retry_instruction_is_read_whatever_case_the_header_arrived_in() -> None:
    """Header names are case-insensitive on the wire and case-sensitive in a dictionary, so a
    connector reading only the vendor's own capitalisation stops reading the instruction the
    day something normalises the headers on the way through. A header that is absent means we
    learned nothing rather than that there is nothing to wait for.

    Delete this and a proxy that lower-cases headers silently removes every wait."""
    assert retry_after({"Retry-After": "60"}) == 60.0
    assert retry_after({"retry-after": "60"}) == 60.0
    assert retry_after({}) is None
    assert retry_after({"Retry-After": "soon"}) is None


# --------------------------------------------- the change signal (M11.4.6, M11.4.9)
def test_a_cursor_cannot_see_a_deletion_so_an_id_sweep_is_declared() -> None:
    """A removed record is one the cursor never mentions again, so a cursor-driven projection
    keeps every record the source ever had and reports them as current. The only remedy a
    read-only integration has is absence: enumerate the ids the source still returns and treat
    everything missing from that enumeration as gone.

    Delete this and declaring the deletions signalled looks tidier and loses records
    silently."""
    subscribed = subscription(ENTITY_CONTACT)
    assert subscribed.kind is ChangeSignal.UPDATED_SINCE
    assert subscribed.sees_deletions_by_itself is False
    assert subscribed.needs_an_absence_check is True
    with pytest.raises(ManifestError, match="cannot do"):
        subscribed.__class__(
            source=CONNECTOR_NAME,
            entity=ENTITY_CONTACT,
            kind=ChangeSignal.UPDATED_SINCE,
            notify_within=CURSOR_POLL_INTERVAL,
            reconcile_every=RECONCILIATION_INTERVAL,
            deletion_check=DeletionCheck.SIGNALLED,
        )


def test_freshness_is_measured_against_the_full_pass_rather_than_the_poll() -> None:
    """Staleness is computed from when a record was last seen, and a cursor poll only mentions
    records that changed, so a quiet record is re-seen only by the full pass. Measured against
    the poll interval, every unedited row would read as stale within the hour.

    Delete this and handing `RefreshPromise` the poll interval marks the whole projection stale
    while it is perfectly correct."""
    promise = refresh_promise(ENTITY_CLIENT)
    assert promise.interval == RECONCILIATION_INTERVAL
    assert promise.interval > CURSOR_POLL_INTERVAL
    record = projected_record(
        ENTITY_CLIENT, operation().project(body_of(COMPANY_ROW))[0], last_seen_at=NOW
    )
    assert record is not None
    assert assess_staleness(record, now=NOW, promise=promise).freshness is Freshness.LIVE
    stale = assess_staleness(record, now=NOW + timedelta(days=1), promise=promise)
    assert stale.freshness is Freshness.STALE


# ------------------------------------------------------------- HubSpot's own dates
def test_a_property_timestamp_is_read_as_milliseconds_rather_than_seconds() -> None:
    """HubSpot returns property timestamps as milliseconds since the epoch, rendered as a
    string. Read as seconds, 1794700800000 is the year 58854, which sorts, filters and renders
    without complaint.

    Delete this and an off-by-1000 error produces close dates fifty-six thousand years out
    that no assertion anywhere notices."""
    assert parse_hubspot_timestamp("1794700800000") == datetime(2026, 11, 15, tzinfo=UTC)
    assert parse_hubspot_timestamp(1794700800000) == datetime(2026, 11, 15, tzinfo=UTC)
    parsed = parse_hubspot_timestamp("2026-11-15T00:00:00+00:00")
    assert parsed is not None and parsed.tzinfo is not None


def test_a_timestamp_that_cannot_be_dated_is_dropped_rather_than_guessed() -> None:
    """A naive timestamp read in Singapore is eight hours older than it is, which is the whole
    width of the ageing band in `brain.gate.provenance`. None means "not stated", and the
    caller drops the field rather than inventing a value nobody sent.

    Delete this and a lenient parser attaches a real timestamp to a value nobody sent, and the
    row sorts by it."""
    assert parse_hubspot_timestamp("2026-11-15T00:00:00") is None
    assert parse_hubspot_timestamp("not a date") is None
    assert parse_hubspot_timestamp(None) is None
    row = {"id": "88", "lifecycle_stage": "customer", "updated_at": "2026-11-15T00:00:00"}
    record = projected_record(ENTITY_CLIENT, row, last_seen_at=NOW)
    assert record is not None
    assert "updated_at" not in record.fields
    assert record.fields["lifecycle_stage"] == "customer"


def test_a_row_with_no_id_is_dropped_rather_than_given_one() -> None:
    """A generated id cannot be cited, cannot be pointed at by a request-access route and
    cannot be matched to the same record on the next fetch, so the row would be reported twice
    and audited never.

    Delete this and a row missing its id acquires an invented identity."""
    assert projected_record(ENTITY_CLIENT, {"name": "SNM"}, last_seen_at=NOW) is None


# ------------------------------------------- the four declarations (M11.4.5, M4.2.1)
def test_every_mapped_field_is_classified_by_the_policy() -> None:
    """A mapped field nothing classifies is withheld from everybody by default-deny, which is
    safe and pointless: it travels through this process and into traces in exchange for
    nothing.

    Delete this and a field added to the mapping to "see if it is useful" ships."""
    policy = hubspot_field_policy()
    for entity in (ENTITY_CLIENT, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_ASSOCIATION):
        for name in mapped_targets(entity):
            assert policy.governs(entity, name), f"{entity}.{name} is mapped and unclassified"


def test_every_projected_field_is_also_mapped() -> None:
    """A projected field nothing maps is a column that never arrives, so a fast-lane filter on
    it silently matches nothing and the answer is an empty list nobody questions.

    Delete this and a rename on one side of the pair goes unnoticed until somebody asks a
    question that returns nothing."""
    assert_declarations_agree()
    for entity in (ENTITY_CLIENT, ENTITY_CONTACT, ENTITY_DEAL):
        mapped = set(mapped_targets(entity))
        projected = {f.name for f in projection_for(entity, connection()).fields}
        assert projected <= mapped


def test_a_mapping_nothing_classifies_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check itself, exercised rather than assumed. Four lists edited by four people at
    four different times disagree quietly, and every disagreement is invisible in review.

    The disagreement is staged by widening one entity's mapping by a single target the policy
    says nothing about, which is what a field added to "see if it is useful" looks like.
    `mapped_targets` is what `assert_declarations_agree` reads, so replacing it stages exactly
    that one disagreement and leaves the other three comparisons in the function true, which
    is what makes the refusal below attributable to this field rather than to any of them.

    Delete this and `assert_declarations_agree` could return without comparing anything."""
    from brain.connectors import hubspot as module

    declared = module.mapped_targets

    def widened(entity: str) -> tuple[str, ...]:
        if entity == ENTITY_DEAL:
            return (*declared(entity), "hs_forecast_amount")
        return declared(entity)

    monkeypatch.setattr(module, "mapped_targets", widened)
    with pytest.raises(HubSpotError, match=r"hs_forecast_amount.*nothing classifies"):
        assert_declarations_agree()


def test_a_rule_that_disagrees_with_another_sources_opinion_is_refused() -> None:
    """Two connectors contributing to one entity kind is the ordinary case here, because the
    entity registry exists to join a CRM company to a ledger client. `FieldPolicy` refuses two
    different opinions about one field rather than resolving them by merge order, and that
    refusal fires a long way from whoever edited one of them.

    Delete this and a classification changed on one side turns into a `PolicyConflictError`
    inside whatever merges the fragments, which reads as a bug in the merge."""
    agreeing = FieldPolicy(
        rules=(
            FieldRule.of(ENTITY_CLIENT, "name", "read:client.name", Classification.INTERNAL),
            FieldRule.of(
                ENTITY_CLIENT,
                "hours_remaining",
                "read:client.hours_remaining",
                Classification.INTERNAL,
            ),
        )
    )
    assert_policy_merges_with(agreeing)

    disagreeing = FieldPolicy(
        rules=(FieldRule.of(ENTITY_CLIENT, "name", "read:client.name", Classification.PUBLIC),)
    )
    with pytest.raises(HubSpotError, match=r"client\.name"):
        assert_policy_merges_with(disagreeing)


def test_the_house_spelling_is_used_for_a_field_another_source_already_classifies() -> None:
    """The positive form of the rule above, and the reason there is no conflict to resolve:
    `client.name` is spelled exactly as `tests/invariants/test_redaction_invariants.py` and
    `brain.connectors.xero` already spell their shared fields.

    Delete this and a capability of `read:hubspot.client_name`, which reads as tidier, makes
    every existing grant for `read:client.name` stop reaching this connector's rows."""
    policy = hubspot_field_policy()
    name = policy.rule_for(ENTITY_CLIENT, "name")
    assert name is not None
    assert name.required_capability == Capability(value="read:client.name")
    assert name.classification is Classification.INTERNAL
    updated = policy.rule_for(ENTITY_CONTACT, "updated_at")
    assert updated is not None
    assert updated.required_capability == Capability(value="read:contact.updated_at")


# ------------------------------------------------- three answers, not one (M11.5.5)
def test_an_empty_crm_a_refused_one_and_an_unreachable_one_are_three_answers() -> None:
    """`HUBSPOT-200-empty` is the recording that exists for exactly this: a genuine absence, as
    distinct from a refusal and from an outage. An empty result for a 429 produces "there are
    no deals with that client" out of "I could not read the CRM", and somebody acts on it.

    Delete this and collapsing every failure into an empty list is green."""
    present = interpret(
        operation(), status=200, body=body_of(COMPANY_ROW), fetched_at=NOW.isoformat()
    )
    absent = interpret(operation(), status=200, body=EMPTY_CRM.body, fetched_at=NOW.isoformat())
    unreachable = reply_for(429, {"message": "rate limit"})
    refused = reply_for(401, {"message": "expired authentication"})
    assert present.outcome is HubSpotOutcome.PRESENT
    assert absent.outcome is HubSpotOutcome.ABSENT
    assert unreachable.outcome is HubSpotOutcome.UNREACHABLE
    assert refused.outcome is HubSpotOutcome.REFUSED
    assert len({r.outcome for r in (present, absent, unreachable, refused)}) == 4


def test_the_recorded_absence_is_answered_and_a_failure_is_not() -> None:
    """The distinction that matters downstream: an absence is a result with a read time on it,
    and a failure has neither rows nor a time. Both are "no records" to anything that only
    counts.

    Delete this and `answered` could return True for everything, which makes the four outcomes
    decorative."""
    absent = interpret(operation(), status=200, body=EMPTY_CRM.body, fetched_at=NOW.isoformat())
    assert absent.outcome.answered is True
    assert absent.rows is not None and absent.rows.records == ()
    assert reply_for(429, {"message": "rate limit"}).outcome.answered is False


def test_a_rate_limited_reply_carries_no_rows_and_no_read_time() -> None:
    """**The structural half of "never answer from memory".** A failed reply has nowhere to put
    rows and nowhere to put a read time, so substituting the last good response is not
    something a caller can express. A read time would be worse than the rows: freshness is
    computed from it, and the answer would be reported as current.

    Delete this and a well-meaning cache layer fills the failure branch in."""
    refusal = reply_for(429, {"message": "rate limit"})
    assert refusal.rows is None
    assert refusal.fetched_at == ""
    with pytest.raises(HubSpotError, match="rows or a read time"):
        HubSpotReply(
            outcome=HubSpotOutcome.UNREACHABLE,
            call=CallOutcome.QUOTA,
            reason=FailureReason.QUOTA,
            fetched_at=NOW.isoformat(),
        )


def test_a_reply_cannot_claim_records_it_does_not_hold() -> None:
    """PRESENT and ABSENT are decided by what came back, and a reply free to disagree with its
    own rows is a reply that can report an empty CRM as a full one. It is the same mistake as
    the failure branch above, arrived at from the other side.

    Delete this and the outcome becomes a label a caller chooses rather than a fact."""
    empty = interpret(operation(), status=200, body=EMPTY_CRM.body, fetched_at=NOW.isoformat()).rows
    with pytest.raises(HubSpotError, match="present and absent are decided"):
        HubSpotReply(
            outcome=HubSpotOutcome.PRESENT,
            call=CallOutcome.OK,
            rows=empty,
            fetched_at=NOW.isoformat(),
        )
    with pytest.raises(HubSpotError, match="carries no rows"):
        HubSpotReply(outcome=HubSpotOutcome.ABSENT, call=CallOutcome.OK, fetched_at=NOW.isoformat())


def test_a_failure_is_never_reported_as_current() -> None:
    """It follows from the rule above rather than from a second one: with no read time,
    `brain.gate.provenance.assess_freshness` returns UNSTATED by its own argument about a time
    it cannot date.

    Delete this and a freshness branch of this module's own could quietly call a failure
    live."""
    assert reply_for(429, {}).freshness(horizon=HORIZON, now=NOW) is Freshness.UNSTATED
    assert reply_for(401, {}).freshness(horizon=HORIZON, now=NOW) is Freshness.UNSTATED


def test_a_reply_read_a_moment_ago_is_live_and_one_read_yesterday_is_not() -> None:
    """The positive sibling. A connector that reported UNSTATED for everything would satisfy
    the test above and make every answer in the building carry a caveat, which trains people to
    skip the line that matters.

    Delete this and freshness could be hard-coded to UNSTATED."""
    fresh = interpret(
        operation(), status=200, body=body_of(COMPANY_ROW), fetched_at=NOW.isoformat()
    )
    assert fresh.freshness(horizon=HORIZON, now=NOW) is Freshness.LIVE
    assert fresh.freshness(horizon=HORIZON, now=NOW + timedelta(days=2)) is Freshness.STALE


def test_the_error_body_of_a_failure_is_never_projected() -> None:
    """A 429's body holds a message and no `results`, so running the field mapping over it
    would raise a specification error in the middle of somebody's question. The projection
    happens on the success branch only.

    Delete this and projecting before classifying turns every rate limit into a crash."""
    body = {"status": "error", "message": "rate limit", "correlationId": "abc"}
    assert "results" not in body
    assert reply_for(429, body).outcome is HubSpotOutcome.UNREACHABLE


def test_a_person_is_told_the_same_thing_whether_we_were_refused_or_unreachable() -> None:
    """The distinction is ours to act on rather than theirs. "HubSpot declined our credentials"
    tells somebody that we hold HubSpot credentials, which is a disclosure, and there is
    nothing they can do with it either way.

    Delete this and a helpfully specific message enumerates the company's systems for anybody
    who can type a question."""
    disclosable: frozenset[str] = frozenset()
    spoken = reply_for(429, {}).notice(disclosable=disclosable)
    assert spoken == reply_for(401, {}).notice(disclosable=disclosable)
    assert spoken != ""
    assert CONNECTOR_NAME not in spoken.casefold()


def test_a_notice_names_hubspot_only_when_the_askers_catalogue_already_did() -> None:
    """Naming a source is a disclosure, and the rule is `federation.PartialAnswer.notice`'s
    rather than a second one written here.

    Delete this and this module could grow its own sentence, which would be the generous copy
    because it is the one somebody edits while debugging an outage."""
    named = reply_for(429, {}).notice(disclosable=frozenset({CONNECTOR_NAME}))
    assert CONNECTOR_NAME in named


def test_an_answered_reply_says_nothing_about_itself() -> None:
    """A reassurance attached to every successful answer is a claim offered where nobody asked
    for one, and it trains a reader to skip the line that matters when it eventually says
    something else.

    Delete this and every answer carries a sentence about HubSpot."""
    present = interpret(
        operation(), status=200, body=body_of(COMPANY_ROW), fetched_at=NOW.isoformat()
    )
    assert present.notice(disclosable=frozenset({CONNECTOR_NAME})) == ""
    assert present.failure() is None


def test_the_trace_keeps_the_distinction_the_notice_drops() -> None:
    """An auditor is already entitled to know what this system connects to, and the two
    failures go to different people: a declined authorisation is somebody re-authorising a
    connection, and a rate limit is somebody asking for less.

    Delete this and the two become indistinguishable everywhere, including in the console row
    that decides who gets called."""
    quota = reply_for(429, {}).failure()
    declined = reply_for(401, {}).failure()
    assert quota is not None and quota.reason is FailureReason.QUOTA
    assert declined is not None and declined.reason is FailureReason.NOT_SERVING
    assert reply_for(429, {}).trace_line() != reply_for(401, {}).trace_line()


def test_nothing_a_reply_renders_carries_a_value_from_the_response_body() -> None:
    """A detail assembled from a response would put a filter value, and therefore a client's
    name, into a trace and a health row that have a different audience and a different
    retention from the answer they describe.

    Delete this and echoing the vendor's error text into the detail looks helpful."""
    body = {"message": MONEY_CANARY, "context": {"company": "SNM Construction"}}
    reply = interpret(operation(), status=429, body=body, fetched_at="")
    rendered = f"{reply.trace_line()} {reply.detail} {reply.notice(disclosable=frozenset())}"
    assert MONEY_CANARY not in rendered
    assert "SNM" not in rendered


def test_a_rate_limit_is_never_counted_against_the_breaker() -> None:
    """A 429 is the rate limiter working and the source saying so. Counting it as ill health
    opens the circuit whenever a connector is popular, so the busiest connector in the company
    is the intermittently unavailable one.

    Delete this and mapping a quota refusal onto a breaker failure passes, and the fix somebody
    reaches for is a longer cooldown, which makes it worse."""
    breaker = connector_breaker(CONNECTOR_NAME)
    after = record_outcome(breaker, reply_for(429, {}).call, now=NOW)
    assert after.consecutive_failures == 0


def test_a_source_that_did_not_answer_is_counted_against_the_breaker() -> None:
    """The positive sibling. A classification that never counted anything would satisfy the
    test above and leave a dead source in rotation for ever.

    Delete this and returning QUOTA for every failure is green."""
    breaker = connector_breaker(CONNECTOR_NAME)
    reply = reply_for(500, {"message": "Server Error"})
    assert reply.call is CallOutcome.UNAVAILABLE
    assert record_outcome(breaker, reply.call, now=NOW).consecutive_failures == 1


def test_a_timeout_is_unreachable_and_says_so_in_the_trace() -> None:
    """A source that never answered has no status to classify, and a caller holding a stale
    status variable would otherwise have it read.

    Delete this and a timeout with a leftover 200 in scope reports rows that never arrived."""
    reply = interpret(operation(), status=None, body=None, fetched_at="", timed_out=True)
    assert reply.outcome is HubSpotOutcome.UNREACHABLE
    assert reply.detail.endswith("in time")


# ---------------------------------------------------------------------- health (M11.1.1)
def test_a_rate_limit_is_degraded_rather_than_down() -> None:
    """The source is healthy and we asked for too much. DOWN sends somebody to check whether
    HubSpot is up, which it is, and the only action available is to ask for less.

    Delete this and a busy afternoon reads as an outage."""
    state = health(reply_for(429, {}), checked_at=NOW)
    assert state.state is HealthState.DEGRADED
    assert state.is_usable is True
    assert state.checked_at == NOW


def test_a_declined_authorisation_is_down_rather_than_unconfigured() -> None:
    """It was working this morning, so it is an incident for whoever owns the connection.
    UNCONFIGURED would file it as an installation task and it would sit there.

    Delete this and an expired token joins the permanently amber rollout rows nobody reads."""
    assert health(reply_for(401, {}), checked_at=NOW).state is HealthState.DOWN


def test_a_connector_nobody_has_probed_is_unconfigured_rather_than_down() -> None:
    """A connector nobody has called yet is a job for whoever installed it, and reporting DOWN
    would page somebody about a system that may be perfectly healthy.

    Delete this and every connector is DOWN between installation and its first call."""
    unprobed = health(None, checked_at=NOW)
    assert unprobed.state is HealthState.UNCONFIGURED
    assert unprobed.is_usable is False


def test_a_healthy_call_is_reported_as_healthy() -> None:
    """The positive sibling for the three above. A health function that never returns OK is a
    dashboard nobody looks at twice.

    Delete this and returning DEGRADED unconditionally is green."""
    present = interpret(
        operation(), status=200, body=body_of(COMPANY_ROW), fetched_at=NOW.isoformat()
    )
    assert health(present, checked_at=NOW).state is HealthState.OK


def test_every_call_outcome_has_a_health_state() -> None:
    """The mapping is total on purpose. A `dict.get` with a default would let a sixth outcome
    be classified as whatever the default said, and for a health state the convenient default
    is OK.

    Totality is asserted directly as well as exercised, because the two catch the same fault
    at different moments and only one of them is readable. Exercising it catches a missing row
    as a KeyError raised inside somebody's probe; asserting it names the outcome nobody
    classified, in a test whose name says what the rule is.

    Delete this and a new outcome raises a KeyError inside a probe, which reads as the
    connector being broken."""
    from brain.connectors import hubspot as module

    assert set(module._HEALTH_FOR) == set(CallOutcome)
    rows = interpret(operation(), status=200, body=EMPTY_CRM.body, fetched_at=NOW.isoformat()).rows
    for outcome in CallOutcome:
        reply = HubSpotReply(
            outcome=HubSpotOutcome.ABSENT,
            call=outcome,
            rows=rows,
            fetched_at=NOW.isoformat(),
            detail="",
        )
        assert isinstance(health(reply, checked_at=NOW).state, HealthState)


# ------------------------------------------------------------------ the manifest (M11.1.7)
def test_the_manifest_is_read_only_and_carries_a_predicate_for_its_service_tools() -> None:
    """A HubSpot private app is one credential for one account and nobody has a personal
    HubSpot login that maps to their principal, so the identity mode is SERVICE. The
    consequence is that the source will not narrow anything for us, which is why every
    projection stores a predicate rather than an unrestricted scope.

    Delete this and a write binding, or a projection with no predicate, installs cleanly, and
    the second one is the absence of the source's permission model rather than a narrowing of
    it."""
    installed = manifest()
    assert installed.credential.mode is AccessMode.READ_ONLY
    assert installed.credential.write_granted_by == ""
    for tool in installed.tools:
        assert tool.identity_mode is IdentityMode.SERVICE
    for projection in installed.projections:
        assert projection.visibility.is_unrestricted() is False
        clause = projection.visibility.clauses[0]
        assert (clause.field, clause.op, clause.value) == ("portal_id", Op.EQ, PORTAL)


def test_the_pinned_digest_moves_when_a_tool_description_changes() -> None:
    """A third-party server can redefine what a tool does without changing a single name, and
    the description is what the model reads when it chooses. Two manifests differing only in a
    description have to produce different digests.

    Delete this and pinning the tool names alone passes, which pins the shape of the catalogue
    and not its meaning."""
    installed = manifest()
    assert manifest_digest(installed) == manifest_digest(manifest())
    rewritten = installed.__class__(
        name=installed.name,
        version=installed.version,
        transport=installed.transport,
        scope=installed.scope,
        credential=installed.credential,
        tools=(
            installed.tools[0].__class__(
                name=installed.tools[0].name,
                description="Every contact in the account, with their email addresses.",
                entity=installed.tools[0].entity,
                identity_mode=installed.tools[0].identity_mode,
            ),
            *installed.tools[1:],
        ),
        projections=installed.projections,
        ceiling=installed.ceiling,
    )
    assert manifest_digest(rewritten) != manifest_digest(installed)


def test_a_field_mapping_that_writes_one_target_twice_is_refused() -> None:
    """Two source paths writing one target means which value survives is decided by
    declaration order, and the losing one is the field somebody meant.

    Delete this and mapping both `firstname` and `lastname` onto a single `name` target, which
    is the obvious way to get a label back into the contact projection, installs cleanly and
    stores whichever half the loop reached last."""
    from brain.connectors.transports import RestTransport, TransportError

    with pytest.raises(TransportError, match="more than one source path"):
        RestTransport(
            spec_ref="hubspot",
            operation="getContacts",
            entity=ENTITY_CONTACT,
            fields=(
                FieldMapping(target="name", source_path="properties.firstname"),
                FieldMapping(target="name", source_path="properties.lastname"),
            ),
        )
