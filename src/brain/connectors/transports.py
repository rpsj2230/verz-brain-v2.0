"""The four transports, and the one typed contract they all normalise to.

Architecture section 12 lists four ways a connector reaches a source, and says the transport
is invisible above the connector layer. That sentence is the requirement this file exists to
make true: everything here turns a source's own shape into `TypedResult[SourceRecord]`, so
the gate, the redactor and the composer never learn whether a client record arrived over MCP
or out of a database view.

Each transport is a *declaration* validated at manifest review, plus a normaliser. There is
no client here and no socket: a transport that owned an HTTP session could not be tested for
the cases that matter, which are a server redefining a tool and a view that is not on the
allowlist. The real clients belong behind `ConnectorFetch`, which is a callable and is
faked in tests.

Three decisions are worth stating before the code.

**A record is tagged and otherwise untyped, and that is deliberate.** `SourceRecord` carries
the entity tag and the record id, and lets everything else through as extras. The tag is
what makes redaction possible at all; the extras arrive at `compute_mask`, match no field
policy rule, and are withheld under default-deny. So a source that adds a column ships that
column to nobody until somebody classifies it, which is the correct direction for a field
nobody has thought about.

**An MCP tool that was not declared is not exposed.** MCP servers define their own tools and
can add one between connections. Auto-naming a new remote tool into our grammar would put it
in front of a model on the strength of the far side having invented it, so the mapping is
explicit and an unmapped remote tool is dropped. `brain.connectors.registry.reconnect`
catches the redefinition of a *declared* tool; this catches the arrival of an undeclared one.

**The database adapter does not accept SQL.** It accepts an allowlisted view name and
filters, and the statement is built downstream from those. The rejected alternative was to
accept SQL and validate it, which sounds stricter and is weaker: a validator is a parser, and
a parser that disagrees with the database's own parser is a bypass rather than a check. There
is no string here that could carry `; DROP` because there is no string here that becomes SQL.

Scope: domain logic. Nothing here opens a connection, reads a table or calls a source.

Task ids: M11.1.2, M11.1.3, M11.1.4, M11.1.5
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ConfigDict

from brain.connectors.contract import ConnectorContractError, ConnectorScope, TransportKind
from brain.core.envelope import OBJECT_NAME_PATTERN, TOOL_NAME_PATTERN, Entity, TypedResult

# ------------------------------------------------------------------ written-down reasons
#: Why there is no SQL anywhere in the database adapter.
NO_SQL_CROSSES_THIS_SEAM = (
    "The database adapter takes a view name from an allowlist and a set of filters, and the "
    "statement is built from those. Accepting SQL and validating it was rejected: a "
    "validator is a parser, a parser that disagrees with the database's own parser is a "
    "bypass, and every SQL allowlist ever written has been defeated by a comment, a nested "
    "select or a dialect quirk. A view name that is not on the list is refused by string "
    "equality, which has no dialect."
)

#: Why an undeclared remote tool is dropped rather than named for us.
AN_UNDECLARED_REMOTE_TOOL_IS_NOT_EXPOSED = (
    "An MCP server can add a tool between connections. Deriving our name from the remote one "
    "would put it in front of a model because the far side invented it, which is the same "
    "trust the manifest pin exists to withhold. The mapping is declared, and an unmapped "
    "remote tool is dropped: a connector that offers less than the server does is a smaller "
    "problem than one that offers whatever the server thought of this morning."
)

#: Why custom code is a declaration here and a sandbox somewhere else.
THE_SANDBOX_IS_NOT_IN_THIS_MODULE = (
    "`CustomTransport` refuses to be declared without a sandbox profile, and that refusal is "
    "all this module provides. The sandbox itself is a process boundary with a filesystem, a "
    "network policy and a memory ceiling, and it belongs to whoever owns the runtime. Naming "
    "the profile here means a custom connector cannot be installed without somebody having "
    "chosen one; it does not mean the profile has been enforced, and a module that implied "
    "otherwise would be the worst possible place to be wrong."
)

_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)
_TOOL_NAME_RE: Final = re.compile(TOOL_NAME_PATTERN)

#: A JSON pointer-ish path into a response body: `data.items.status`, `data.items[0].id`.
#: Deliberately not a full JSONPath. Expressions, filters and wildcards make a mapping into a
#: program, and a field mapping that can compute is a field mapping nobody can review.
_SOURCE_PATH_RE: Final = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:\[\d{1,6}\])|(?:\.[A-Za-z_][A-Za-z0-9_]*))*$"
)

#: What a database view may be called. Schema-qualified is allowed and required in practice,
#: because an unqualified name resolves against `search_path` and therefore against whatever
#: the connection was left set to.
_VIEW_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}$")


class TransportError(ConnectorContractError):
    """A transport was declared in a shape that cannot be installed."""


# ------------------------------------------------------------------ the typed contract
class SourceRecord(Entity):
    """One record from any source: tagged, identified, and otherwise passed through.

    `extra="allow"`, against `Entity`'s own `extra="forbid"`, and the difference is the
    design. A connector cannot know in advance which columns a source will return, and a
    model that refused unknown ones would drop a column the day the source added it, which
    reads as data loss rather than as a schema change.

    Allowing them is safe precisely because of what happens next: an extra field reaches
    `brain.core.redaction.compute_mask`, matches no rule in the field policy, and is withheld
    as `UNCLASSIFIED`. So an unclassified column is visible to nobody until somebody
    classifies it, and the failure mode of a source adding a column is that nothing changes.
    """

    model_config = ConfigDict(extra="allow")


def normalise(
    entity: str,
    rows: tuple[Mapping[str, Any], ...],
    *,
    source: str,
    fetched_at: str,
    id_field: str = "id",
    truncated: bool = False,
) -> TypedResult[SourceRecord]:
    """Turn a source's rows into the one contract everything above this layer reads.

    A row with no usable id is dropped rather than given a generated one. A generated id
    cannot be cited, cannot be pointed at by a request-access route, and cannot be matched to
    the same record on the next fetch, so a record carrying one is a record that will be
    reported twice and audited never. This is the same refusal
    `brain.core.redaction._walk_mapping` makes about an unidentified object, made one layer
    earlier so the drop is attributable to the connector that produced it.

    `truncated` is passed in rather than inferred from the row count, because the thing that
    truncates is usually invisible from here: Freshdesk's search stops at 300 records and says
    nothing, so only the caller that knows which endpoint it used can tell.
    """
    if not _NAME_RE.match(entity):
        msg = f"entity {entity!r} is not a name; the redactor looks a policy up by this string"
        raise TransportError(msg)

    records: list[SourceRecord] = []
    for row in rows:
        raw_id = row.get(id_field)
        if not isinstance(raw_id, str | int) or not str(raw_id).strip():
            continue
        payload = {k: v for k, v in row.items() if k not in ("entity", "id")}
        records.append(SourceRecord(entity=entity, id=str(raw_id), **payload))

    return TypedResult[SourceRecord](
        records=tuple(records),
        source=source,
        fetched_at=fetched_at,
        truncated=truncated,
    )


# ---------------------------------------------------------------------- MCP (M11.1.2)
@dataclass(frozen=True)
class McpTransport:
    """An MCP server, and the exact set of its tools we are willing to expose.

    `tool_names` maps the server's own name to ours. It is required and it is exhaustive:
    see `AN_UNDECLARED_REMOTE_TOOL_IS_NOT_EXPOSED`.

    `endpoint` is a string here and is not parsed into a URL. Parsing it would mean deciding
    what a valid endpoint is, and an MCP server is reached over stdio as often as over HTTP,
    so a URL validator would refuse half the legitimate configurations and be relaxed within
    a week.
    """

    endpoint: str
    tool_names: Mapping[str, str]

    kind: TransportKind = TransportKind.MCP

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            msg = "an MCP transport needs an endpoint"
            raise TransportError(msg)
        if not self.tool_names:
            msg = (
                "an MCP transport declares no tool mapping, so every tool the server offers "
                "would be either invisible or auto-named. "
                f"{AN_UNDECLARED_REMOTE_TOOL_IS_NOT_EXPOSED}"
            )
            raise TransportError(msg)
        illegal = sorted(ours for ours in self.tool_names.values() if not _TOOL_NAME_RE.match(ours))
        if illegal:
            msg = (
                f"MCP tool mapping produces {illegal}, which are not source.verb_noun; a name "
                "the model cannot read is a tool it picks for the wrong reason"
            )
            raise TransportError(msg)
        counts = Counter(self.tool_names.values())
        collided = sorted(name for name, count in counts.items() if count > 1)
        if collided:
            msg = (
                f"MCP tool mapping sends more than one remote tool to {collided}; which one "
                "runs would be decided by iteration order"
            )
            raise TransportError(msg)

    def exposed(self, remote_tools: tuple[str, ...]) -> tuple[str, ...]:
        """Our names for the remote tools we declared, dropping everything else.

        Sorted, and deduplicated by construction. A tool the server offers and we did not
        declare is absent from the result and from the catalogue, silently as far as the
        model is concerned and visibly to whoever compares this against the server's list.
        """
        return tuple(sorted(self.tool_names[r] for r in remote_tools if r in self.tool_names))

    def undeclared(self, remote_tools: tuple[str, ...]) -> tuple[str, ...]:
        """Remote tools we are not exposing. What an operator asks this object.

        Kept separate from `exposed` rather than logged inside it, because the interesting
        event is not "we dropped one" but "the set changed", and only somebody holding both
        lists can see that.
        """
        return tuple(sorted(set(remote_tools) - set(self.tool_names)))


# ------------------------------------------------------------- REST / OpenAPI (M11.1.3)
@dataclass(frozen=True)
class FieldMapping:
    """One field of one entity, and where in the response it comes from.

    `source_path` is a dotted path with optional numeric subscripts and nothing else. A full
    JSONPath would let a mapping filter, select and compute, at which point the mapping is a
    program: it stops being reviewable, and the thing it computes is data nobody declared.
    """

    target: str
    source_path: str

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.target):
            msg = (
                f"mapping target {self.target!r} is not a name; the field policy is looked up "
                "by it, and a name nothing matches is withheld from everybody"
            )
            raise TransportError(msg)
        if not _SOURCE_PATH_RE.match(self.source_path):
            msg = (
                f"source path {self.source_path!r} is not a plain dotted path; expressions "
                "and wildcards make a mapping into a program nobody can review"
            )
            raise TransportError(msg)


@dataclass(frozen=True)
class RestTransport:
    """An OpenAPI spec plus a field mapping. No code for a well-behaved API.

    `spec_ref` names the specification rather than embedding it: a spec is hundreds of
    kilobytes, it changes on the vendor's schedule, and putting it inside the manifest would
    put it inside the pinned digest, so every unrelated vendor edit would quarantine the
    connector. What is pinned is the mapping, which is the part that decides what we read.
    """

    spec_ref: str
    operation: str
    entity: str
    fields: tuple[FieldMapping, ...]

    kind: TransportKind = TransportKind.REST

    def __post_init__(self) -> None:
        if not self.spec_ref.strip():
            msg = "a REST transport needs a spec reference"
            raise TransportError(msg)
        if not self.operation.strip():
            msg = (
                "a REST transport needs an operation id; naming a path and a method instead "
                "means the mapping breaks silently when the vendor reorganises its paths"
            )
            raise TransportError(msg)
        if not _NAME_RE.match(self.entity):
            msg = f"REST transport entity {self.entity!r} is not a name"
            raise TransportError(msg)
        if not self.fields:
            msg = (
                f"REST operation {self.operation!r} maps no fields; a mapping with nothing in "
                "it returns records that are a bare entity tag, which the redactor drops"
            )
            raise TransportError(msg)
        counts = Counter(f.target for f in self.fields)
        collided = sorted(name for name, count in counts.items() if count > 1)
        if collided:
            msg = (
                f"REST mapping writes {collided} from more than one source path; which value "
                "survives would be decided by declaration order"
            )
            raise TransportError(msg)


# --------------------------------------------------------------- database views (M11.1.4)
@dataclass(frozen=True)
class ViewRead:
    """A plan to read one allowlisted view. Not a statement, and not a string that becomes one.

    Whoever executes this builds the statement from the view name and the parameters, both of
    which arrived from a closed set. See `NO_SQL_CROSSES_THIS_SEAM`.
    """

    view: str
    filters: tuple[tuple[str, str], ...] = ()
    limit: int = 0


@dataclass(frozen=True)
class DatabaseTransport:
    """A read-only credential against allowlisted views only. How the Laravel app connects.

    Two independent restrictions, and neither is redundant. The credential is read-only at the
    database, so a bug here cannot write; the allowlist is ours, so a credential that turns
    out to be wider than believed still reaches only the views somebody named. Relying on the
    first alone means trusting a grant nobody in this repository can see; relying on the
    second alone means trusting our own code with a credential that could write.
    """

    views: tuple[str, ...]

    kind: TransportKind = TransportKind.DATABASE

    def __post_init__(self) -> None:
        if not self.views:
            msg = (
                "a database transport allowlists no views, so it reaches whatever the "
                "credential reaches; scope at connect is a narrowing or it is not a scope"
            )
            raise TransportError(msg)
        malformed = sorted(v for v in self.views if not _VIEW_RE.match(v))
        if malformed:
            msg = (
                f"views {malformed} are not schema-qualified lower-case names; an unqualified "
                "name resolves against search_path, which is whatever the connection was left "
                "set to rather than what the manifest says"
            )
            raise TransportError(msg)

    def plan(
        self, view: str, *, filters: tuple[tuple[str, str], ...] = (), limit: int = 0
    ) -> ViewRead:
        """A read against one allowlisted view, or a refusal.

        String equality against a closed tuple, which is the whole security argument: there is
        no pattern to be wrong about, no dialect to disagree with, and no way to express a
        second statement.
        """
        if view not in self.views:
            msg = (
                f"view {view!r} is not on this connector's allowlist {list(self.views)}; a "
                "read-only credential still reaches every view it was granted, and the "
                "allowlist is the half of that restriction we can see"
            )
            raise TransportError(msg)
        if limit < 0:
            msg = "a negative limit is not a limit"
            raise TransportError(msg)
        return ViewRead(view=view, filters=filters, limit=limit)


# ------------------------------------------------------------- custom code (M11.1.5)
#: Sandbox profiles a custom connector may name. A closed set, because a free-text profile is
#: one nobody has implemented: it would read in a manifest as though a boundary existed.
SANDBOX_PROFILES: Final[frozenset[str]] = frozenset(
    {"no_network", "egress_allowlist", "read_only_fs"}
)


@dataclass(frozen=True)
class CustomTransport:
    """A sandboxed module for anything odd: legacy SOAP, a scraped portal.

    The module refuses to be declared without a sandbox profile and an egress allowlist where
    the profile needs one, and that is the entire contribution of this class. See
    `THE_SANDBOX_IS_NOT_IN_THIS_MODULE`, which says plainly what has and has not been built.
    """

    module: str
    sandbox_profile: str
    egress_allowlist: tuple[str, ...] = ()

    kind: TransportKind = TransportKind.CUSTOM

    def __post_init__(self) -> None:
        if not self.module.strip():
            msg = "a custom transport needs a module to run"
            raise TransportError(msg)
        if self.sandbox_profile not in SANDBOX_PROFILES:
            msg = (
                f"custom transport {self.module!r} names sandbox profile "
                f"{self.sandbox_profile!r}, which is not one of {sorted(SANDBOX_PROFILES)}; a "
                "profile nobody implemented reads in a manifest as though a boundary existed"
            )
            raise TransportError(msg)
        if self.sandbox_profile == "egress_allowlist" and not self.egress_allowlist:
            msg = (
                f"custom transport {self.module!r} declares an egress allowlist profile and "
                "lists no hosts, which permits every host while reading as a restriction"
            )
            raise TransportError(msg)
        if self.sandbox_profile != "egress_allowlist" and self.egress_allowlist:
            msg = (
                f"custom transport {self.module!r} lists egress hosts under the "
                f"{self.sandbox_profile!r} profile, which does not consult them; a list that "
                "is not read is a permission somebody believes they have granted"
            )
            raise TransportError(msg)


# ------------------------------------------------------------------ the four together
Transport = McpTransport | RestTransport | DatabaseTransport | CustomTransport


def assert_scope_covers(transport: Transport, scope: ConnectorScope) -> None:
    """A database transport's views must all sit inside the connector's connect scope.

    Only the database transport is checked, and that is not laziness. Its allowlist and the
    connect scope are the same kind of thing said twice, so they can disagree, and a view on
    one list and not the other is a reach nobody approved. The other three name resources in
    the source's own vocabulary (a folder id, an operation id, a module path), where there is
    no second list to compare against and inventing a comparison would be inventing a rule.
    """
    if not isinstance(transport, DatabaseTransport):
        return
    outside = sorted(view for view in transport.views if not scope.admits(view))
    if outside:
        msg = (
            f"views {outside} are allowlisted by the transport and outside the connector's "
            f"{scope.resource_kind} scope; two lists that disagree mean one of them is not "
            "the restriction anybody approved"
        )
        raise TransportError(msg)
