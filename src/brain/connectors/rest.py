"""A spec and a mapping, and no code per endpoint.

`brain.connectors.transports.RestTransport` is the *declaration*: a spec reference, an
operation id, an entity and a field mapping, all checked at manifest review. This module is
the other half, which is the part that runs: it parses the specification, builds the address,
checks it, and turns a response body into `SourceRecord`s.

**Adding an endpoint has to be data, not a deployment.** That is the whole argument for
"OpenAPI plus field mapping" over a hand-written client per endpoint. A hand-written adapter
is a Python file, a review, a release and a restart for every operation a source exposes, so
in practice the third endpoint never gets added and somebody widens the second one instead.
Here an operation is a path, a method, its parameters and its response shape, read out of a
document, and a mapping is a list of pairs. `load_spec` refuses whatever it cannot read
rather than guessing, because a spec parser that guesses produces a connector that reads a
different endpoint from the one the author declared.

**Anything the mapping does not name is dropped, and passthrough is not an option anywhere
in here.** A passthrough is how a connector quietly starts carrying fields nobody
classified. The twelve-field projection cap in `brain.core.projection` and every rule in
`brain.core.field_policy` are written against *named* fields, so a field arriving unnamed is
a field outside both. `project` copies mapped targets into a fresh dictionary and never
copies a row; the difference is that a fresh dictionary cannot acquire a field by the source
adding a column, and a copied row does exactly that on the vendor's schedule.

**This is a place where somebody else's data becomes ours, and it must not be a place where
our permission model is decided.** `brain.connectors` says a connector returns everything it
fetched and the redactor removes what is not covered. A mapping that could say "this field
only for finance" would be a second permission model, expressed in configuration, evaluated
before the redactor and invisible to it. `assert_maps_only` refuses one structurally, on the
declaration's field names and annotations rather than on its behaviour, in the same form and
for the same reason as `brain.connectors.contract.assert_fetches_only` and
`brain.core.redaction.assert_channel_adapter`: a signature is checkable, a body is not. See
`A_MAPPING_NAMES_FIELDS_AND_NEVER_PEOPLE`.

**A connector spec is configuration, and configuration is somewhere an address gets typed.**
So every address here goes through `brain.tools.fetch.assert_fetchable`, which already
refuses plain http, credentials in the URL, an unbracketed IPv6 literal, every range that
means "inside this network", and every hop of a redirect chain. It is imported rather than
restated: a second address checker is a second opinion about what "inside" means, and the
laxer opinion is the one an attacker uses. The address is checked twice on the first hop,
once by `prepare` and once inside `fetch`, and that is deliberate: `prepare` is what a
caller uses to hold a checked address without connecting, so the check has to live there
too, and DNS can move between the two.

Rejected, and worth stating because both look tidier:

*Accepting a full JSONPath in a mapping.* `FieldMapping` already refuses one, and the reason
carries here: a mapping that can filter and compute is a program, a program in a
configuration file is reviewed by nobody, and the fields it computes are fields nobody
declared. A dotted path with numeric subscripts can be read aloud.

*Letting the adapter own an HTTP client.* Then the redirect chain, which is the only part of
this that is ever wrong, could not be tested. `Fetcher` is one hop and is supplied by the
caller, exactly as `brain.tools.fetch` does it.

*Re-raising `UnsafeAddressError` as a connector error.* It would give an operator two names
for one refusal and make the skill-import path and the connector path look like two separate
defences instead of the single one they are.

Scope: domain logic and parsing. Nothing here opens a socket, resolves a name or reads a
clock; the resolver, the fetcher and `fetched_at` are all parameters.

Task ids: M11.1.3
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode

from brain.connectors.contract import DECIDING_TYPE_NAMES, FetchRequest, assert_fetches_only
from brain.connectors.transports import RestTransport, SourceRecord, TransportError, normalise
from brain.core.envelope import TypedResult
from brain.tools.fetch import Fetchable, Fetcher, Resolver, assert_fetchable, fetch

# ------------------------------------------------------------------ written-down reasons
#: Why a field mapping has nowhere to say who may see a field.
A_MAPPING_NAMES_FIELDS_AND_NEVER_PEOPLE = (
    "A mapping says where one of our field names comes from in somebody else's response, "
    "and that is the entire vocabulary it has. Giving it a capability, a role or a "
    "visibility clause would create a second permission model: expressed in configuration, "
    "evaluated before the redactor, and invisible to the one audit that is supposed to be "
    "sufficient. It would also be the permissive copy, because a mapping is edited by "
    "whoever is adding an endpoint rather than by whoever owns the field policy. So the "
    "refusal is structural: a declaration carrying a field named for a permission, or "
    "annotated as one of the deciding types, cannot be used to build an adapter at all."
)

#: Why nothing unmapped survives the projection.
WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE = (
    "The projection builds a new record out of the mapped targets rather than copying the "
    "source row and removing what is unwanted. The two read the same on the day they are "
    "written and diverge the first time the vendor adds a column: a copy carries it, a "
    "build does not. Everything downstream is written against named fields, the "
    "twelve-field projection cap and the field policy included, so a field that arrives "
    "unnamed is outside both and is a field nobody has classified, retained, or agreed to "
    "hold."
)

#: Why the specification is parsed strictly and refuses rather than guessing.
A_SPEC_PARSER_THAT_GUESSES_READS_THE_WRONG_ENDPOINT = (
    "Two arrays in a response body, two servers in a document, two operations sharing an id: "
    "in each case there is an answer that works most of the time and is decided by key "
    "order. A connector that reads the wrong list, or calls the sandbox host, fails by "
    "returning plausible data from the wrong place, which is the failure nobody notices. "
    "Refusing at load time puts it in front of whoever wrote the spec, which is the only "
    "moment anybody can fix it cheaply."
)

# ------------------------------------------------------------------------------ grammars
#: What an operation id may be called. The mapping names an operation by this string, so it
#: has to be a token rather than a sentence.
_OPERATION_ID_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,79}$")

#: What a parameter may be called. Vendors use mixed case and dots (`Invoice.Status`), so
#: this is wider than our own name grammar and deliberately does not admit a slash, a brace
#: or a percent, none of which can appear in a name that is about to be put into a URL.
_PARAMETER_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,79}$")

#: A placeholder in a templated path: the `{InvoiceID}` in `/Invoices/{InvoiceID}`.
_PLACEHOLDER_RE: Final = re.compile(r"\{([^{}]+)\}")

#: One step of a source path: a name, or a numeric subscript. The grammar itself is
#: `transports._SOURCE_PATH_RE`, which `FieldMapping` has already enforced; this only splits
#: a string that has been accepted.
_PATH_STEP_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\[\d{1,6}\]")

#: Attribute names that would let a declaration decide who may see something. Checked by
#: name as well as by type for the reason `contract.CREDENTIAL_ATTRIBUTE_RE` gives about
#: credentials: the thing being smuggled is nearly always a `str` or a `tuple[str, ...]`, so
#: a type-only rule would pass `required_capability: str` while refusing an honest one.
#:
#: `required` is deliberately absent. It is OpenAPI's own word for a parameter that must be
#: supplied, and forbidding it would forbid the specification's own vocabulary. `scope` is
#: absent too, for the reason `contract.A_SCOPE_PREDICATE_IS_NOT_A_GRANT` gives: a scope
#: predicate is a row filter the gate already computed, and pushing it down can only narrow.
DECIDING_ATTRIBUTE_RE: Final = re.compile(
    r"(^|_)(capability|capabilities|entitlement|entitlements|grant|grants|permission"
    r"|permissions|acl|acls|role|roles|principal|principals|clearance|visible_to"
    r"|allowed_for|restricted_to)(_|$)"
)

#: HTTP methods an operation may declare. A closed set, because an unknown key under a path
#: is a vendor extension or a typo, and treating either as an operation invents an endpoint.
HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: The one method this module will actually call. `Fetcher` has a single `get_once`, on
#: purpose: a transport that could send a body would be a write path, and a write path needs
#: the read-back rule in `brain.connectors.throttle.is_retryable` rather than this module's
#: silence. A spec may declare anything; `read` refuses everything else.
READ_METHOD: Final = "get"

#: How deep a response schema is walked looking for the record array. Six is far past any
#: real envelope (`data.attributes.items` is three) and stops a mutually recursive pair of
#: `$ref`s from being walked forever by way of the depth counter rather than the ref counter.
MAX_SCHEMA_DEPTH: Final = 6

#: How many `$ref` hops one schema node may take. A chain longer than this is a document
#: that points at itself, and following it is a parse that does not terminate.
MAX_REF_HOPS: Final = 8

#: The ceiling on one REST response. Four megabytes, against `MAX_FETCH_BYTES`'s twenty: a
#: skill import is an archive, and this is one page of records with at most twelve projected
#: fields on each. Enforced by `brain.tools.fetch.fetch` against bytes received rather than
#: against `Content-Length`, which is a claim made by the thing being fetched.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

#: What `normalise` reads a record's id out of, and therefore a target the mapping must
#: name. A row without one is dropped rather than given a generated id, so a mapping that
#: forgets it produces an empty result that reads exactly like an empty source.
ID_TARGET: Final = "id"


class RestSpecError(TransportError):
    """A specification, or an argument to one, that cannot be turned into a call.

    A `TransportError` and therefore a `ConnectorContractError`, deliberately: like every
    other refusal in this package it is a mistake by whoever declared the connector, it
    should stop the connector being installed, and nobody asking a question should ever see
    it.
    """


# ------------------------------------------------------------------ the parsed operation
@dataclass(frozen=True)
class ParameterSpec:
    """One parameter of one operation, as the specification declares it.

    `location` is `path` or `query` and nothing else. A header parameter is refused at parse
    time and the reason is not tidiness: a header is where `Authorization: Bearer ...` gets
    typed into a configuration file, and a connector never holds a credential at all
    (`contract.assert_holds_no_credential`). A cookie is the same thing with a different
    name.
    """

    name: str
    location: str
    required: bool = False

    def __post_init__(self) -> None:
        if not _PARAMETER_NAME_RE.match(self.name):
            msg = (
                f"parameter {self.name!r} is not a name; it is put into a URL, so a slash or "
                "a brace in it would change which endpoint is called"
            )
            raise RestSpecError(msg)
        if self.location not in ("path", "query"):
            msg = (
                f"parameter {self.name!r} is declared in {self.location!r}; only path and "
                "query are read. A header parameter is where a credential gets typed into a "
                "spec, and a connector borrows a lease rather than holding one"
            )
            raise RestSpecError(msg)


@dataclass(frozen=True)
class OperationSpec:
    """One operation: the four things needed to build a call, and where the records are.

    `records_at` is read out of the response schema rather than configured beside it. A
    second place to say where the list lives is a second thing to keep in step with the
    vendor, and the copy that drifts is the one that silently reads an empty array.
    """

    operation_id: str
    method: str
    path: str
    parameters: tuple[ParameterSpec, ...] = ()
    #: Dotted path to the records in the response body. Empty means the body itself, which is
    #: what a spec whose top-level response schema is an array (or a single object) declares.
    records_at: str = ""
    #: Whether what sits at `records_at` is an array. False is the by-id shape: a response
    #: declaring no array declares one record, and it is projected as a list of one. The
    #: alternative was refusing `GET /invoices/{id}`, which is the most common shape in REST,
    #: and a spec-driven adapter that cannot express it sends its author back to hand-written
    #: code for exactly the endpoint they were trying to add as data.
    returns_list: bool = True

    def __post_init__(self) -> None:
        if not _OPERATION_ID_RE.match(self.operation_id):
            msg = (
                f"operation id {self.operation_id!r} is not a token; a field mapping names an "
                "operation by this string and has to be able to spell it"
            )
            raise RestSpecError(msg)
        if self.method not in HTTP_METHODS:
            msg = f"operation {self.operation_id!r} declares method {self.method!r}"
            raise RestSpecError(msg)
        if not self.path.startswith("/"):
            msg = (
                f"operation {self.operation_id!r} has path {self.path!r}, which does not start "
                "with a slash; joined to a base URL that is a different address"
            )
            raise RestSpecError(msg)

    def parameter(self, name: str) -> ParameterSpec | None:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        return None


# --------------------------------------------------------------------- reading a document
def _pointer(document: Mapping[str, Any], ref: str) -> Any:
    """Resolve one local JSON pointer, `#/components/schemas/Invoice`.

    Local only. An external `$ref` is a URL, and following one would make loading a
    specification into an outbound request from inside the client's network, which is the
    entire surface `brain.tools.fetch` exists to close. A spec that fetches is not data.
    """
    if not ref.startswith("#/"):
        msg = (
            f"$ref {ref!r} points outside this document. Loading a spec would then be an "
            "outbound request, and a spec that fetches is not configuration"
        )
        raise RestSpecError(msg)
    node: Any = document
    for raw in ref[2:].split("/"):
        step = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or step not in node:
            msg = f"$ref {ref!r} does not resolve in this document"
            raise RestSpecError(msg)
        node = node[step]
    return node


def _deref(node: Any, document: Mapping[str, Any]) -> Any:
    """Follow `$ref` until the node is a schema, or until the chain is absurd."""
    for _hop in range(MAX_REF_HOPS):
        if not isinstance(node, Mapping) or "$ref" not in node:
            return node
        ref = node["$ref"]
        if not isinstance(ref, str):
            msg = "a $ref is a string pointer"
            raise RestSpecError(msg)
        node = _pointer(document, ref)
    msg = f"a $ref chain longer than {MAX_REF_HOPS} hops points at itself"
    raise RestSpecError(msg)


def _array_paths(
    schema: Any, document: Mapping[str, Any], *, prefix: str = "", depth: int = 0
) -> tuple[str, ...]:
    """Every dotted path in a response schema whose value is an array.

    Sorted by property name rather than by document order, so the refusal below reports the
    same pair whichever way the document was written. It is a list rather than a first
    match because finding two is the interesting case: see
    `A_SPEC_PARSER_THAT_GUESSES_READS_THE_WRONG_ENDPOINT`.
    """
    if depth > MAX_SCHEMA_DEPTH:
        return ()
    node = _deref(schema, document)
    if not isinstance(node, Mapping):
        return ()
    if node.get("type") == "array":
        return (prefix,)
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    found: list[str] = []
    for name, child in sorted(properties.items()):
        if not isinstance(name, str):
            continue
        below = f"{prefix}.{name}" if prefix else name
        found.extend(_array_paths(child, document, prefix=below, depth=depth + 1))
    return tuple(found)


def _response_shape(
    operation_id: str, body: Mapping[str, Any], document: Mapping[str, Any]
) -> tuple[str, bool]:
    """Where this operation's records live and whether there are several, from its own schema.

    Read out of the response schema rather than declared beside it, so there is one place
    that can be wrong when a vendor reshapes an envelope. A response with exactly one array
    is a list at that path; a response with none is a single record at the body itself; a
    response with two is refused, because which one holds the records would be decided by
    key order.
    """
    responses = body.get("responses")
    if not isinstance(responses, Mapping):
        msg = f"operation {operation_id!r} declares no responses, so it declares no records"
        raise RestSpecError(msg)
    schema: Any = None
    for status in sorted(str(code) for code in responses):
        if not status.startswith("2"):
            continue
        content = _deref(responses[status], document)
        if not isinstance(content, Mapping):
            continue
        media = content.get("content")
        if not isinstance(media, Mapping):
            continue
        for kind, entry in sorted(media.items()):
            if not str(kind).startswith("application/json"):
                continue
            if isinstance(entry, Mapping) and "schema" in entry:
                schema = entry["schema"]
                break
        if schema is not None:
            break
    if schema is None:
        msg = (
            f"operation {operation_id!r} declares no JSON success response; a mapping "
            "projects records out of a body, and there is no declared body to project from"
        )
        raise RestSpecError(msg)

    arrays = _array_paths(schema, document)
    if len(arrays) > 1:
        msg = (
            f"operation {operation_id!r} declares arrays at {list(arrays)}; which one holds "
            "the records would be decided by key order. "
            f"{A_SPEC_PARSER_THAT_GUESSES_READS_THE_WRONG_ENDPOINT}"
        )
        raise RestSpecError(msg)
    if not arrays:
        return "", False
    return arrays[0], True


def _parameters(operation_id: str, body: Mapping[str, Any]) -> tuple[ParameterSpec, ...]:
    declared = body.get("parameters", ())
    if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
        msg = f"operation {operation_id!r} declares parameters that are not a list"
        raise RestSpecError(msg)
    parsed: list[ParameterSpec] = []
    for entry in declared:
        if not isinstance(entry, Mapping):
            msg = f"operation {operation_id!r} has a parameter that is not an object"
            raise RestSpecError(msg)
        name = entry.get("name")
        location = entry.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            msg = f"operation {operation_id!r} has a parameter with no name or no location"
            raise RestSpecError(msg)
        parsed.append(
            ParameterSpec(name=name, location=location, required=bool(entry.get("required", False)))
        )
    return tuple(parsed)


@dataclass(frozen=True)
class RestSpec:
    """One document's worth of operations, and the one address they are reached at."""

    base_url: str
    operations: Mapping[str, OperationSpec]

    def operation(self, operation_id: str) -> OperationSpec:
        """The named operation, or a refusal naming what the document does declare.

        Listing the alternatives is safe here and would not be in a request path: a
        specification is configuration read by whoever wrote it, not somebody else's data,
        so there is no ABSENT-versus-DENIED question to get wrong.
        """
        found = self.operations.get(operation_id)
        if found is None:
            msg = (
                f"the spec declares no operation {operation_id!r}; it declares "
                f"{sorted(self.operations)}. A mapping naming an operation that moved would "
                "otherwise fail at the first call rather than at review"
            )
            raise RestSpecError(msg)
        return found

    def bind(self, transport: RestTransport) -> RestOperation:
        """Tie a declared transport to the operation it names. Where the two are compared."""
        return RestOperation(
            base_url=self.base_url,
            operation=self.operation(transport.operation),
            transport=transport,
        )


def load_spec(document: Mapping[str, Any], *, resolver: Resolver) -> RestSpec:
    """Parse a minimal OpenAPI document, and refuse the address before anything is built.

    The address check runs here as well as at call time, and the two are not redundant. Here
    it turns a private or credential-bearing server URL into a red build in front of whoever
    wrote the spec. At call time it is the one that is load-bearing, because a name that
    resolved publicly at load can resolve privately at connect, and that difference is the
    whole of DNS rebinding.

    Exactly one server, deliberately. A document listing production and sandbox is a
    document where which one we call is decided by list order, and the checked address is
    then not necessarily the connected one. Naming the server in the connector's own
    configuration is a smaller decision made in a more visible place.
    """
    servers = document.get("servers")
    if not isinstance(servers, Sequence) or isinstance(servers, str | bytes) or len(servers) != 1:
        msg = (
            "a connector spec names exactly one server; a document listing several leaves "
            "which host is called to list order, and only one of them was checked"
        )
        raise RestSpecError(msg)
    entry = servers[0]
    url = entry.get("url") if isinstance(entry, Mapping) else None
    if not isinstance(url, str) or not url.strip():
        msg = "the spec's server has no url"
        raise RestSpecError(msg)
    # The refusal an author reads. Every rule about where a connection may go lives in
    # `assert_fetchable`, imported rather than restated, so this cannot come to a more
    # generous conclusion than the skill importer does about the same address.
    assert_fetchable(url, resolver)

    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        msg = "a connector spec declares paths; without them it declares no operations"
        raise RestSpecError(msg)

    operations: dict[str, OperationSpec] = {}
    for path, methods in sorted(paths.items()):
        if not isinstance(path, str) or not isinstance(methods, Mapping):
            msg = f"path {path!r} does not declare methods"
            raise RestSpecError(msg)
        for method, body in sorted(methods.items()):
            if str(method).lower() not in HTTP_METHODS or not isinstance(body, Mapping):
                continue
            operation_id = body.get("operationId")
            if not isinstance(operation_id, str):
                msg = (
                    f"{str(method).upper()} {path} declares no operationId; naming a path and "
                    "a method instead means the mapping breaks silently when the vendor "
                    "reorganises its paths"
                )
                raise RestSpecError(msg)
            if operation_id in operations:
                msg = (
                    f"operation id {operation_id!r} is declared twice; which endpoint a "
                    "mapping means would be decided by document order"
                )
                raise RestSpecError(msg)
            parameters = _parameters(operation_id, body)
            records_at, returns_list = _response_shape(operation_id, body, document)
            spec = OperationSpec(
                operation_id=operation_id,
                method=str(method).lower(),
                path=path,
                parameters=parameters,
                records_at=records_at,
                returns_list=returns_list,
            )
            _assert_placeholders_are_declared(spec)
            operations[operation_id] = spec

    if not operations:
        msg = "the spec declares no operations, so no endpoint can be added by mapping to it"
        raise RestSpecError(msg)
    return RestSpec(base_url=url.rstrip("/"), operations=operations)


def _assert_placeholders_are_declared(spec: OperationSpec) -> None:
    """Every `{name}` in a path is a declared path parameter, and every path parameter is used.

    Both directions, because they fail differently. An undeclared placeholder means the URL
    is built from an argument nobody wrote down, so nothing checks its name or whether it is
    required. A declared path parameter that appears nowhere in the template is an argument
    that will be accepted and then silently dropped, which reads at the call site as a filter
    that is being applied.
    """
    in_path = {p.name for p in spec.parameters if p.location == "path"}
    placeholders = set(_PLACEHOLDER_RE.findall(spec.path))
    undeclared = sorted(placeholders - in_path)
    if undeclared:
        msg = (
            f"operation {spec.operation_id!r} templates {undeclared} into its path and "
            "declares no parameter for them; the URL would be built from an argument nobody "
            "wrote down"
        )
        raise RestSpecError(msg)
    unused = sorted(in_path - placeholders)
    if unused:
        msg = (
            f"operation {spec.operation_id!r} declares path parameters {unused} that appear "
            f"nowhere in {spec.path!r}; an argument that is accepted and dropped reads as a "
            "filter being applied"
        )
        raise RestSpecError(msg)


# ------------------------------------------------------------ the structural refusal
def _names_in(annotation: object) -> frozenset[str]:
    """Every identifier in an annotation, however it is spelled.

    Crude on purpose, and the same crudeness as `contract._names_in` for the same reason:
    `Capability`, `"Capability | None"` and `entitlement.Capability` all have to read the
    same, and a parser that understood the type algebra would be a second opinion about what
    an annotation means. The *rule set* is imported rather than restated, which is the half
    that must not fork.
    """
    return frozenset(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(annotation)))


def assert_maps_only(declaration: type | object) -> None:
    """Refuse a declaration that could express a permission decision (M11.1.3).

    Checked over annotations rather than over values, so it runs on a class before anything
    has been constructed and cannot be defeated by a field that happens to be empty at
    inspection time. Two rules, mirroring `contract.assert_holds_no_credential`:

    **An attribute whose name says permission.** A capability list is a `tuple[str, ...]`, so
    a type-only rule would pass `required_capabilities: tuple[str, ...]` while refusing the
    honest `policy: FieldPolicy`.

    **An attribute whose type is one of the deciding types.** `DECIDING_TYPE_NAMES` is
    imported from `brain.connectors.contract`, so this cannot come to a different conclusion
    from the rule that governs a connector's fetch signature.

    What this does not do is read a body. A determined author can still consult a module
    global, and that is why the redactor runs regardless: this refuses the mistake, and the
    layer below refuses the consequence. See `A_MAPPING_NAMES_FIELDS_AND_NEVER_PEOPLE`.
    """
    target = declaration if isinstance(declaration, type) else type(declaration)
    annotations: dict[str, object] = {}
    for base in reversed(target.__mro__):
        annotations.update(getattr(base, "__annotations__", {}) or {})

    offenders: list[str] = []
    for attribute, annotation in annotations.items():
        if DECIDING_ATTRIBUTE_RE.search(attribute.casefold()):
            offenders.append(f"{attribute} (named for a permission)")
            continue
        deciding = sorted(_names_in(annotation) & DECIDING_TYPE_NAMES)
        if deciding:
            offenders.append(f"{attribute}: {', '.join(deciding)}")

    if offenders:
        msg = (
            f"{target.__name__} carries {offenders}, which would let a mapping decide who "
            f"may see a field. {A_MAPPING_NAMES_FIELDS_AND_NEVER_PEOPLE}"
        )
        raise RestSpecError(msg)


# ------------------------------------------------------------------- resolving a source path
#: Returned by `_resolve` for a path that is not present. A sentinel rather than None,
#: because a source that returns a JSON null for a field has said something different from a
#: source that omitted it, and collapsing the two would silently invent a value.
_MISSING: Final = object()


def _steps(source_path: str) -> tuple[str | int, ...]:
    """Split an already-validated source path into names and subscripts."""
    steps: list[str | int] = []
    for match in _PATH_STEP_RE.finditer(source_path):
        text = match.group()
        steps.append(int(text[1:-1]) if text.startswith("[") else text)
    return tuple(steps)


def _resolve(node: Any, steps: Sequence[str | int]) -> Any:
    """Walk a dotted path into a decoded body, or report that it is not there."""
    current: Any = node
    for step in steps:
        if isinstance(step, int):
            if not isinstance(current, list) or step >= len(current):
                return _MISSING
            current = current[step]
            continue
        if not isinstance(current, Mapping) or step not in current:
            return _MISSING
        current = current[step]
    return current


# ------------------------------------------------------------------------- the adapter
@dataclass(frozen=True)
class RestOperation:
    """One operation, one mapping, one base address. What a REST connector actually is.

    Holds no client, no credential and no entitlement, and `__post_init__` proves the last
    of those structurally over every declaration it was built from rather than over this
    class alone: a mapping is the thing an author edits, so a mapping subclass is where a
    permission clause would arrive.
    """

    base_url: str
    operation: OperationSpec
    transport: RestTransport

    def __post_init__(self) -> None:
        for declaration in (type(self), type(self.operation), type(self.transport)):
            assert_maps_only(declaration)
        for mapping in self.transport.fields:
            assert_maps_only(type(mapping))
        for parameter in self.operation.parameters:
            assert_maps_only(type(parameter))
        if self.transport.operation != self.operation.operation_id:
            msg = (
                f"mapping names operation {self.transport.operation!r} and was bound to "
                f"{self.operation.operation_id!r}"
            )
            raise RestSpecError(msg)
        if not any(field.target == ID_TARGET for field in self.transport.fields):
            msg = (
                f"the mapping for {self.transport.operation!r} names no {ID_TARGET!r} target; "
                "a record with no id is dropped rather than given a generated one, so this "
                "mapping returns nothing and reads exactly like an empty source"
            )
            raise RestSpecError(msg)

    # ------------------------------------------------------------------ building the call
    def url_for(self, arguments: Mapping[str, str]) -> str:
        """The address this operation is reached at with these arguments. Checked by `prepare`.

        Path arguments are percent-encoded with nothing left safe, which is the whole of the
        defence against an argument changing which endpoint is called: an id containing
        `../` or a query string is a value, and after `quote` it is still one segment.

        The query is sorted, so one call renders to one string. An address that varies by
        dictionary order cannot be compared between a log line and a trace, and comparing
        those two is how anybody finds out which endpoint was actually reached.
        """
        unknown = sorted(name for name in arguments if self.operation.parameter(name) is None)
        if unknown:
            msg = (
                f"operation {self.operation.operation_id!r} declares no parameter {unknown}; a "
                "spec-driven adapter cannot invent one, because nothing would say where it "
                "goes or whether the source reads it"
            )
            raise RestSpecError(msg)
        missing = sorted(
            p.name for p in self.operation.parameters if p.required and p.name not in arguments
        )
        if missing:
            msg = f"operation {self.operation.operation_id!r} requires {missing}"
            raise RestSpecError(msg)

        path = self.operation.path
        for parameter in self.operation.parameters:
            if parameter.location != "path":
                continue
            path = path.replace(
                "{" + parameter.name + "}", quote(arguments[parameter.name], safe="")
            )
        left = _PLACEHOLDER_RE.findall(path)
        if left:
            msg = f"path {self.operation.path!r} still holds {sorted(left)} after substitution"
            raise RestSpecError(msg)

        query = sorted(
            (p.name, arguments[p.name])
            for p in self.operation.parameters
            if p.location == "query" and p.name in arguments
        )
        suffix = f"?{urlencode(query)}" if query else ""
        return f"{self.base_url}{path}{suffix}"

    def prepare(self, arguments: Mapping[str, str], *, resolver: Resolver) -> Fetchable:
        """Build the address and refuse it before anything opens a connection.

        Returns the `Fetchable` rather than a bare string so a caller connects to the address
        that was checked and carries the hostname in `Host` and in SNI. `Fetchable`'s own
        docstring says plainly that nothing can force that, and repeating the claim here
        would be implying a guarantee this module does not provide either.
        """
        return assert_fetchable(self.url_for(arguments), resolver)

    # ------------------------------------------------------------------- the projection
    def project(self, body: Any) -> tuple[Mapping[str, Any], ...]:
        """Turn a decoded response into rows holding only what the mapping names.

        A fresh dictionary per row, built from the mapped targets. Not a copy of the row with
        unwanted keys removed: see `WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE`.

        A path that is not present contributes nothing rather than a null. A vendor omitting
        an optional field has said something different from a vendor sending an empty one,
        and a mapping that invented a null would put a value nobody sent in front of a
        reader. The record id is the exception only in effect: a row whose id path is absent
        has no id, and `normalise` drops it.
        """
        at = self.operation.records_at
        found = _resolve(body, _steps(at)) if at else body
        if found is _MISSING:
            msg = (
                f"the response holds nothing at {self.operation.records_at!r}, which is where "
                f"{self.operation.operation_id!r} declares its records; the source and its "
                "own specification disagree"
            )
            raise RestSpecError(msg)
        if not self.operation.returns_list:
            rows: list[Any] = [found]
        elif isinstance(found, list):
            rows = found
        else:
            msg = (
                f"{self.operation.records_at or 'the response body'} is not a list, and the "
                "spec declares an array there; treating one object as a row of it would be "
                "guessing at a shape the source did not send"
            )
            raise RestSpecError(msg)

        projected: list[Mapping[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                msg = (
                    f"a record in {self.operation.records_at or 'the response body'} is not an "
                    "object, so there is nothing for a field mapping to name"
                )
                raise RestSpecError(msg)
            mapped: dict[str, Any] = {}
            for field in self.transport.fields:
                value = _resolve(row, _steps(field.source_path))
                if value is not _MISSING:
                    mapped[field.target] = value
            projected.append(mapped)
        return tuple(projected)

    def records(
        self, body: Any, *, fetched_at: str, truncated: bool = False
    ) -> TypedResult[SourceRecord]:
        """The projection, in the one contract everything above the connector layer reads.

        `truncated` is the caller's, mirroring `transports.normalise`: the thing that
        truncates is usually invisible from here, because Freshdesk's search stops at 300
        records and says nothing about it.
        """
        return normalise(
            self.transport.entity,
            self.project(body),
            source=self.transport.spec_ref,
            fetched_at=fetched_at,
            id_field=ID_TARGET,
            truncated=truncated,
        )

    # ------------------------------------------------------------------------- the call
    def read(
        self,
        arguments: Mapping[str, str],
        *,
        fetcher: Fetcher,
        resolver: Resolver,
        fetched_at: str,
        max_bytes: int = MAX_RESPONSE_BYTES,
        truncated: bool = False,
    ) -> TypedResult[SourceRecord]:
        """Fetch, and return what the mapping names. The only outbound path in this module.

        `brain.tools.fetch.fetch` owns the loop, so every redirect is a new address and gets
        the whole address check again. Writing the loop here would apply the rules to the
        first address only, which is the same as not applying them, and it is exactly the
        mistake every HTTP client's default `follow_redirects=True` makes on our behalf.

        A JSON body is decoded here because decoding is not a connection. A body that is not
        JSON is a refusal rather than an empty result: a source answering with an HTML error
        page has failed, and reporting that as "no records" is how an outage is summarised as
        an absence.
        """
        if self.operation.method != READ_METHOD:
            msg = (
                f"operation {self.operation.operation_id!r} is a "
                f"{self.operation.method.upper()}, and this adapter reads. A write needs the "
                "read-back rule in brain.connectors.throttle rather than this module's silence"
            )
            raise RestSpecError(msg)
        checked = self.prepare(arguments, resolver=resolver)
        fetched = fetch(checked.url, fetcher=fetcher, resolver=resolver, max_bytes=max_bytes)
        try:
            body = json.loads(fetched.body)
        except ValueError as exc:
            msg = (
                f"{self.operation.operation_id!r} answered with something that is not JSON; "
                "reporting that as an empty result would summarise an outage as an absence"
            )
            raise RestSpecError(msg) from exc
        return self.records(body, fetched_at=fetched_at, truncated=truncated)

    def as_fetch(
        self,
        *,
        fetcher: Fetcher,
        resolver: Resolver,
        fetched_at: str,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
        """This operation as a connector fetch, checked against the contract before it is used.

        `assert_fetches_only` runs on the closure rather than on this method, and that is the
        point of building one: it is the object a registry would actually call, so it is the
        object whose signature has to be shown never to receive the caller's grants.

        A `limit` or a `cursor` is refused rather than ignored. Paging is a per-vendor
        parameter name this module has no way to guess, and answering a request for the first
        fifty records with all of them, or with an unpaged first page, is a wrong answer that
        looks like a right one.
        """

        def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
            if request.entity != self.transport.entity:
                msg = (
                    f"this operation maps {self.transport.entity!r} and was asked for "
                    f"{request.entity!r}"
                )
                raise RestSpecError(msg)
            if request.limit or request.cursor:
                msg = (
                    "this adapter has nowhere to put a limit or a cursor: paging is a "
                    "parameter name only the vendor's spec knows. Answering without applying "
                    "it would be a wrong answer that reads as a right one"
                )
                raise RestSpecError(msg)
            return self.read(
                dict(request.filters),
                fetcher=fetcher,
                resolver=resolver,
                fetched_at=fetched_at,
                max_bytes=max_bytes,
            )

        assert_fetches_only(_fetch)
        return _fetch
