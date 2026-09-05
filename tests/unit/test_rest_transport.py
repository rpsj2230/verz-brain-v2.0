"""The REST adapter: what the spec is allowed to say, and what comes back from a response.

Three properties are being pinned, and they are the three ways this file's subject goes
wrong.

An endpoint is added by editing data. `test_a_second_endpoint_is_added_by_data_and_not_by_
code` is the whole claim of "OpenAPI plus field mapping" in one assertion, and if it ever
stops being true the design has quietly reverted to a hand-written client per endpoint.

A response arrives with more in it than we asked for, always, and only what the mapping
names may survive. Everything downstream is written against named fields.

And a connector spec is configuration, which is somewhere an address gets typed. Every
address rule is `brain.tools.fetch`'s and is tested there; what is tested here is that this
module routes through it, including on the hop nobody controls, which is the redirect.

The fakes are the fixture that matters. A resolver whose answer changes between two calls,
and a fetcher with a scripted redirect chain, are both unreachable against a real network.

Task ids: M11.1.3
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from brain.connectors.contract import FetchRequest, assert_fetches_only
from brain.connectors.rest import (
    OperationSpec,
    ParameterSpec,
    RestOperation,
    RestSpecError,
    assert_maps_only,
    load_spec,
)
from brain.connectors.transports import FieldMapping, RestTransport
from brain.core.entitlement import EntitlementSet
from brain.tools.fetch import FetchedBytes, UnsafeAddressError

PUBLIC = "93.184.216.34"
METADATA = "169.254.169.254"
FETCHED_AT = "2026-09-06T09:00:00Z"


class Resolver:
    """Whatever the test says a name resolves to; anything unnamed is public.

    Modelled on the one in `test_fetch.py` so a reader moving between the two files is not
    learning a second fake.
    """

    def __init__(self, answers: dict[str, Sequence[str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[str] = []

    def resolve(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        return self.answers.get(host, [PUBLIC])


class FlippingResolver:
    """A name that answers publicly and then privately. DNS rebinding, in a fixture."""

    def __init__(self, answers: Sequence[Sequence[str]]) -> None:
        self.answers = list(answers)
        self.calls = 0

    def resolve(self, host: str) -> Sequence[str]:
        del host
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer


class Hops:
    """A fetcher with a scripted chain: each URL either redirects or answers with bytes."""

    def __init__(self, script: dict[str, str | bytes] | None = None) -> None:
        self.script = script or {}
        self.connected: list[tuple[str, str]] = []

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str:
        del max_bytes
        self.connected.append((url, address))
        answer = self.script.get(url, BODY_BYTES)
        if isinstance(answer, bytes):
            return FetchedBytes(body=answer, final_url=url)
        return answer


# ---------------------------------------------------------------------------- the spec
INVOICE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoices": {"type": "array", "items": {"$ref": "#/components/schemas/Invoice"}}
    },
}


def _json_200(ref: str = "#/components/schemas/InvoiceList") -> dict[str, Any]:
    return {"200": {"content": {"application/json": {"schema": {"$ref": ref}}}}}


def _document() -> dict[str, Any]:
    """A minimal but complete document: two read operations, one templated path, one $ref."""
    return {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/invoices": {
                "get": {
                    "operationId": "listInvoices",
                    "parameters": [{"name": "status", "in": "query"}],
                    "responses": _json_200(),
                },
                "post": {
                    "operationId": "createInvoice",
                    "responses": _json_200(),
                },
            },
            "/clients/{clientId}/invoices": {
                "get": {
                    "operationId": "listClientInvoices",
                    "parameters": [{"name": "clientId", "in": "path", "required": True}],
                    "responses": _json_200(),
                }
            },
        },
        "components": {
            "schemas": {
                "InvoiceList": INVOICE_LIST_SCHEMA,
                "Invoice": {"type": "object", "properties": {"id": {"type": "string"}}},
            }
        },
    }


def _mapping(operation: str = "listInvoices") -> RestTransport:
    return RestTransport(
        spec_ref="example-v1.yaml",
        operation=operation,
        entity="invoice",
        fields=(
            FieldMapping(target="id", source_path="InvoiceID"),
            FieldMapping(target="status", source_path="Status"),
            FieldMapping(target="client_name", source_path="Contact.Name"),
        ),
    )


#: One invoice as the source actually sends it. Three of its fields are mapped and four are
#: not, and two of the four are exactly the kind nobody has classified: a total and a note.
BODY: dict[str, Any] = {
    "invoices": [
        {
            "InvoiceID": "INV-1",
            "Status": "AUTHORISED",
            "Contact": {"Name": "SNM", "TaxNumber": "T-9"},
            "Total": 4200,
            "InternalNotes": "chase finance before quarter end",
        }
    ]
}
BODY_BYTES = json.dumps(BODY).encode()


def _bound(
    resolver: Resolver | FlippingResolver | None = None, operation: str = "listInvoices"
) -> RestOperation:
    spec = load_spec(_document(), resolver=resolver or Resolver())
    return spec.bind(_mapping(operation))


# ------------------------------------------------------------------ the address (M11.1.3)
def test_a_public_spec_loads_and_builds_the_address_it_declares() -> None:
    """If this fails, every refusal below passes for the wrong reason: a loader that refuses
    everything satisfies all of them. It also pins the URL shape, which is what a log line is
    later compared against."""
    operation = _bound()

    assert operation.base_url == "https://api.example.com/v1"
    assert operation.url_for({"status": "AUTHORISED"}) == (
        "https://api.example.com/v1/invoices?status=AUTHORISED"
    )


def test_a_spec_naming_a_private_address_is_refused() -> None:
    """A connector spec is configuration, and configuration is somewhere an address gets
    typed. On a client-hosted deployment that address reaches anything the client's network
    reaches.

    Delete this and a connector pointed at 10.0.0.5 installs, and the first anybody hears of
    it is whatever comes back in an error message."""
    document = _document()
    document["servers"] = [{"url": "https://10.0.0.5/v1"}]

    with pytest.raises(UnsafeAddressError, match="reachable only from inside"):
        load_spec(document, resolver=Resolver())


def test_a_spec_whose_server_resolves_inside_the_network_is_refused() -> None:
    """The literal case above is the easy one. A hostname that answers with the cloud
    metadata endpoint is the one that reads as an ordinary vendor address in review.

    Delete this and only literal addresses are checked, which is the same as not checking."""
    document = _document()
    document["servers"] = [{"url": "https://metadata.vendor.example/v1"}]

    with pytest.raises(UnsafeAddressError, match="reachable only from inside"):
        load_spec(document, resolver=Resolver({"metadata.vendor.example": [METADATA]}))


def test_an_address_is_checked_again_when_the_call_is_built() -> None:
    """Checking at load and connecting later by name is a check that is true when it is made
    and false when it is used: the second lookup can answer differently, which is DNS
    rebinding and is the standard bypass for exactly this defence.

    Delete this and the load-time check alone looks sufficient, and a name that answers
    publicly once is trusted for the life of the process."""
    resolver = FlippingResolver([[PUBLIC], [METADATA]])
    operation = _bound(resolver)

    with pytest.raises(UnsafeAddressError, match="reachable only from inside"):
        operation.prepare({}, resolver=resolver)


def test_a_redirect_to_a_private_address_is_refused() -> None:
    """A permitted public URL that answers `302 Location: http://169.254.169.254/` defeats a
    check made only on the first address, and following redirects is the default in every
    HTTP client anybody reaches for.

    Delete this and the module could follow a chain itself, or hand the whole fetch to a
    client that follows one, and the address rules would apply to hop zero only."""
    operation = _bound()
    hops = Hops({"https://api.example.com/v1/invoices": "https://internal.example.com/invoices"})
    resolver = Resolver({"internal.example.com": [METADATA]})

    with pytest.raises(UnsafeAddressError, match="reachable only from inside"):
        operation.read({}, fetcher=hops, resolver=resolver, fetched_at=FETCHED_AT)

    # The refusal happened before the second connection, not after reading its answer.
    assert [url for url, _address in hops.connected] == ["https://api.example.com/v1/invoices"]


def test_a_redirect_to_a_public_address_is_followed() -> None:
    """The sibling of the refusal above. A guard tested only by what it stops is satisfied by
    a fetch that follows nothing at all, and a vendor moving a path to a CDN is ordinary.

    Delete this and refusing every redirect would pass the suite."""
    operation = _bound()
    hops = Hops({"https://api.example.com/v1/invoices": "https://cdn.example.com/invoices"})

    result = operation.read({}, fetcher=hops, resolver=Resolver(), fetched_at=FETCHED_AT)

    assert [url for url, _address in hops.connected] == [
        "https://api.example.com/v1/invoices",
        "https://cdn.example.com/invoices",
    ]
    assert result.record_count() == 1


def test_a_connection_is_made_to_the_address_that_was_checked() -> None:
    """`Fetchable.address` exists so a transport connects to the address the rules were
    applied to rather than to whatever a fresh lookup answers. Delete this and the resolved
    address could be dropped on the floor, which reopens rebinding at the socket."""
    operation = _bound()
    hops = Hops()

    operation.read({}, fetcher=hops, resolver=Resolver(), fetched_at=FETCHED_AT)

    assert hops.connected == [("https://api.example.com/v1/invoices", PUBLIC)]


def test_a_path_argument_cannot_change_which_endpoint_is_called() -> None:
    """A client id arrives from outside. Interpolated raw, `../../admin` is a different
    endpoint, and one that the spec never declared.

    Delete this and path arguments are concatenated, which is the oldest bug in the file."""
    operation = _bound(operation="listClientInvoices")

    url = operation.url_for({"clientId": "../../admin"})

    assert url == "https://api.example.com/v1/clients/..%2F..%2Fadmin/invoices"
    assert "/admin" not in url


# ------------------------------------------------------------- what the mapping does not name
def test_a_field_the_mapping_names_arrives_under_our_own_name() -> None:
    """The positive case. Without it a projection that dropped everything would satisfy every
    refusal in this section, and the connector would look like a permission failure."""
    operation = _bound()

    result = operation.records(BODY, fetched_at=FETCHED_AT)

    assert result.record_count() == 1
    record = result.records[0]
    assert record.entity == "invoice"
    assert record.id == "INV-1"
    assert record.model_dump()["client_name"] == "SNM"


def test_a_field_the_mapping_does_not_name_is_dropped() -> None:
    """A passthrough is how a connector quietly starts carrying fields nobody classified, and
    the twelve-field projection cap and every field-policy rule are written against named
    fields. `Total` and `InternalNotes` were sent and neither was declared.

    Delete this and copying the row and deleting the unwanted keys reads identically, right
    up to the day the vendor adds a column."""
    operation = _bound()

    record = operation.records(BODY, fetched_at=FETCHED_AT).records[0]

    # The exact field set, asserted as a set rather than by looking for absences one at a
    # time: a new unmapped field arriving would not be caught by a list of things to check.
    assert set(record.model_dump()) == {"entity", "id", "status", "client_name"}


def test_a_nested_field_the_mapping_does_not_name_is_dropped_too() -> None:
    """`Contact.Name` is mapped and `Contact.TaxNumber` sits beside it in the same object. A
    projection that copied the container would carry both.

    Delete this and mapping one field of a nested object could bring the whole object."""
    operation = _bound()

    values = operation.project(BODY)[0]

    assert values["client_name"] == "SNM"
    assert "Contact" not in values
    assert "TaxNumber" not in values


def test_a_mapped_path_that_is_absent_contributes_nothing_rather_than_a_null() -> None:
    """A vendor omitting an optional field has said something different from a vendor sending
    an empty one, and an invented null is a value nobody sent placed in front of a reader.

    Delete this and every unmapped-in-this-response field becomes an explicit null, which the
    redactor then has to classify."""
    operation = _bound()
    body = {"invoices": [{"InvoiceID": "INV-2", "Status": "DRAFT"}]}

    record = operation.records(body, fetched_at=FETCHED_AT).records[0]

    assert set(record.model_dump()) == {"entity", "id", "status"}


def test_a_row_whose_id_path_is_absent_is_dropped() -> None:
    """A generated id cannot be cited, cannot be matched to the same record on the next
    fetch, and cannot be pointed at by a request-access route. `transports.normalise` refuses
    to invent one and this proves the adapter goes through it.

    Delete this and a mapping typo on the id path produces records that are reported twice
    and audited never."""
    operation = _bound()
    body = {"invoices": [{"Status": "DRAFT"}, {"InvoiceID": "INV-3", "Status": "PAID"}]}

    result = operation.records(body, fetched_at=FETCHED_AT)

    assert [record.id for record in result.records] == ["INV-3"]


def test_a_mapping_that_names_no_id_is_refused() -> None:
    """Every row would be dropped, so the connector returns nothing and reads exactly like a
    source with no records in it. Delete this and the failure moves from install time to a
    silent empty answer."""
    spec = load_spec(_document(), resolver=Resolver())
    transport = RestTransport(
        spec_ref="example-v1.yaml",
        operation="listInvoices",
        entity="invoice",
        fields=(FieldMapping(target="status", source_path="Status"),),
    )

    with pytest.raises(RestSpecError, match="names no 'id' target"):
        spec.bind(transport)


# ----------------------------------------------------- nowhere to express a capability filter
@dataclass(frozen=True)
class CapabilityMapping(FieldMapping):
    """A field mapping that also says who may see the field. What must not be constructible."""

    required_capability: str = ""


@dataclass(frozen=True)
class EntitledMapping(FieldMapping):
    """The same idea wearing a type rather than a name."""

    holder: EntitlementSet | None = None


def test_a_mapping_has_nowhere_to_express_a_capability_filter() -> None:
    """A mapping that could say "this field only for finance" would be a second permission
    model: expressed in configuration, evaluated before the redactor, invisible to it, and
    edited by whoever is adding an endpoint rather than by whoever owns the field policy.

    Delete this and the refusal is a paragraph in a docstring, which is what it was before
    somebody added the field."""
    spec = load_spec(_document(), resolver=Resolver())
    named = RestTransport(
        spec_ref="example-v1.yaml",
        operation="listInvoices",
        entity="invoice",
        fields=(CapabilityMapping(target="id", source_path="InvoiceID"),),
    )

    with pytest.raises(RestSpecError, match="named for a permission"):
        spec.bind(named)


def test_a_mapping_carrying_the_input_to_a_decision_is_refused_by_type() -> None:
    """The name check alone is defeated by calling the field something else, and the type
    check alone is defeated by storing capabilities as strings, which is how they are
    actually stored. Both, or neither is worth having.

    Delete this and `holder: EntitlementSet` passes review because it is not called
    anything suspicious."""
    spec = load_spec(_document(), resolver=Resolver())
    typed = RestTransport(
        spec_ref="example-v1.yaml",
        operation="listInvoices",
        entity="invoice",
        fields=(EntitledMapping(target="id", source_path="InvoiceID"),),
    )

    with pytest.raises(RestSpecError, match="EntitlementSet"):
        spec.bind(typed)


def test_the_declarations_this_adapter_is_built_from_carry_no_decision() -> None:
    """The positive half. A checker that refused every declaration would satisfy both
    refusals above, and this is what says the real ones pass.

    It also pins the fetch signature: a connector that is never handed an entitlement set
    cannot filter by one, which is the property `contract.assert_fetches_only` exists for and
    the reason the adapter builds a closure rather than exposing a method."""
    operation = _bound()

    for declaration in (
        RestOperation,
        OperationSpec,
        ParameterSpec,
        FieldMapping,
        RestTransport,
    ):
        assert_maps_only(declaration)

    assert_fetches_only(
        operation.as_fetch(fetcher=Hops(), resolver=Resolver(), fetched_at=FETCHED_AT)
    )


def test_a_fetch_built_from_the_adapter_returns_the_projection() -> None:
    """The connector-shaped seam has to actually work, not merely pass its own contract
    check. Delete this and `as_fetch` could return something that raises on every call and
    the structural test above would still pass."""
    operation = _bound()
    fetch = operation.as_fetch(fetcher=Hops(), resolver=Resolver(), fetched_at=FETCHED_AT)

    result = fetch(FetchRequest(entity="invoice"))

    assert result.record_count() == 1
    assert result.source == "example-v1.yaml"
    assert result.fetched_at == FETCHED_AT


# ------------------------------------------------------------------ what the spec may say
def test_a_second_endpoint_is_added_by_data_and_not_by_code() -> None:
    """The claim the whole design rests on. A second operation is a path and a mapping, both
    data, and reaching it needs no module, no branch and no deploy.

    Delete this and nothing anywhere says that adding an endpoint must not be a code change,
    and the third endpoint gets added by widening the second."""
    spec = load_spec(_document(), resolver=Resolver())

    operation = spec.bind(_mapping("listClientInvoices"))

    assert operation.url_for({"clientId": "c_0447"}) == (
        "https://api.example.com/v1/clients/c_0447/invoices"
    )
    assert sorted(spec.operations) == ["createInvoice", "listClientInvoices", "listInvoices"]


def test_a_mapping_naming_an_operation_the_spec_does_not_have_is_refused() -> None:
    """A vendor renaming an operation is the ordinary way a mapping goes stale. Delete this
    and it surfaces as a failed call at request time rather than at review."""
    spec = load_spec(_document(), resolver=Resolver())

    with pytest.raises(RestSpecError, match="declares no operation"):
        spec.bind(_mapping("getPayments"))


def test_an_argument_the_operation_does_not_declare_is_refused() -> None:
    """A spec-driven adapter cannot invent a parameter, because nothing would say where it
    goes or whether the source reads it. Delete this and a misspelt filter is sent as a query
    string the source ignores, and the answer is silently unfiltered."""
    operation = _bound()

    with pytest.raises(RestSpecError, match="declares no parameter"):
        operation.url_for({"Status": "AUTHORISED"})


def test_a_response_declaring_two_arrays_is_refused() -> None:
    """Which one holds the records would be decided by key order, and reading the wrong list
    returns plausible data from the wrong place.

    Delete this and the parser picks one, and the connector that reads `errors` instead of
    `invoices` looks like a source with no data in it."""
    document = _document()
    document["components"]["schemas"]["InvoiceList"] = {
        "type": "object",
        "properties": {
            "invoices": {"type": "array", "items": {}},
            "errors": {"type": "array", "items": {}},
        },
    }

    with pytest.raises(RestSpecError, match="decided by key order"):
        load_spec(document, resolver=Resolver())


def test_a_response_declaring_no_array_is_read_as_one_record() -> None:
    """`GET /invoices/{id}` is the most common shape in REST, and a spec-driven adapter that
    cannot express it sends its author back to hand-written code for the endpoint they were
    trying to add as data.

    Delete this and the by-id shape is refused at load, which reads as the parser being
    broken rather than as a deliberate restriction."""
    document = _document()
    document["components"]["schemas"]["InvoiceList"] = {
        "type": "object",
        "properties": {"InvoiceID": {"type": "string"}, "Status": {"type": "string"}},
    }
    spec = load_spec(document, resolver=Resolver())
    operation = spec.bind(_mapping())

    result = operation.records({"InvoiceID": "INV-9", "Status": "PAID"}, fetched_at=FETCHED_AT)

    assert [record.id for record in result.records] == ["INV-9"]


def test_a_spec_naming_two_servers_is_refused() -> None:
    """Which host is called would be list order, so the address that was checked is not
    necessarily the one connected to. Delete this and a document listing production and
    sandbox installs, and which one runs depends on how the vendor sorted their YAML."""
    document = _document()
    document["servers"] = [
        {"url": "https://api.example.com/v1"},
        {"url": "https://sandbox.example.com/v1"},
    ]

    with pytest.raises(RestSpecError, match="exactly one server"):
        load_spec(document, resolver=Resolver())


def test_a_header_parameter_is_refused() -> None:
    """A header is where `Authorization: Bearer ...` gets typed into a configuration file,
    and a connector borrows a lease for one call rather than holding a credential at all.

    Delete this and a spec grows a place to put a token, which then lives wherever specs
    live."""
    document = _document()
    document["paths"]["/invoices"]["get"]["parameters"] = [
        {"name": "Authorization", "in": "header", "required": True}
    ]

    with pytest.raises(RestSpecError, match="only path and query"):
        load_spec(document, resolver=Resolver())


def test_an_external_ref_is_refused() -> None:
    """Following one would make loading a specification into an outbound request from inside
    the client's network, which is the entire surface the address checks exist to close.

    Delete this and a spec becomes a fetch, and a fetch that nothing checked."""
    document = _document()
    document["paths"]["/invoices"]["get"]["responses"] = _json_200(
        "https://schemas.example.net/InvoiceList"
    )

    with pytest.raises(RestSpecError, match="points outside this document"):
        load_spec(document, resolver=Resolver())


def test_a_path_placeholder_with_no_declared_parameter_is_refused() -> None:
    """The URL would be built from an argument nobody wrote down, so nothing checks its name
    or whether it is required. Delete this and a templated path silently accepts anything."""
    document = _document()
    document["paths"]["/clients/{clientId}/invoices"]["get"]["parameters"] = []

    with pytest.raises(RestSpecError, match="declares no parameter for them"):
        load_spec(document, resolver=Resolver())


def test_an_operation_with_no_operation_id_is_refused() -> None:
    """Naming a path and a method instead means the mapping breaks silently when the vendor
    reorganises its paths. Delete this and a mapping can point at a path that moved."""
    document = _document()
    del document["paths"]["/invoices"]["get"]["operationId"]

    with pytest.raises(RestSpecError, match="declares no operationId"):
        load_spec(document, resolver=Resolver())


# ------------------------------------------------------------------------ calling it
def test_a_body_that_is_not_json_is_refused_rather_than_read_as_empty() -> None:
    """A source answering with an HTML error page has failed, and reporting that as "no
    records" summarises an outage as an absence, which is the one thing this platform must
    never do by accident.

    Delete this and a broken gateway looks like a client with no invoices."""
    operation = _bound()
    hops = Hops({"https://api.example.com/v1/invoices": b"<html>502 Bad Gateway</html>"})

    with pytest.raises(RestSpecError, match="not JSON"):
        operation.read({}, fetcher=hops, resolver=Resolver(), fetched_at=FETCHED_AT)


def test_a_non_get_operation_is_not_called_by_this_adapter() -> None:
    """A write needs the read-back rule in `brain.connectors.throttle.is_retryable`, and this
    module has no way to satisfy it. Refusing is honest; a POST issued as a GET is not.

    Delete this and binding to a create operation reads as supported."""
    spec = load_spec(_document(), resolver=Resolver())
    operation = spec.bind(_mapping("createInvoice"))

    with pytest.raises(RestSpecError, match="this adapter reads"):
        operation.read({}, fetcher=Hops(), resolver=Resolver(), fetched_at=FETCHED_AT)
