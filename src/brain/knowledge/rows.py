"""The row plane: business records reached by a typed tool, never by a model writing SQL.

Two rules carry this module, and they are the same rule applied to the two axes of a
result set. Everything else here is machinery for keeping them true.

**A row the caller may not see must never leave the database.** The scope predicate is
composed into the WHERE clause of the statement that fetches the rows. Filtering afterwards
would mean the row was in this process: in a log line, in a traceback, in whatever a
debugger attached to, in the object a retry path held on to. "We dropped it before
rendering" is then a claim about every code path that ever touches the result, and it has
to be re-proved every time somebody adds one. Inside the query it is a property of the
query, and there is one of those.

**A column the caller may not see must not be in the SELECT list.** Same argument, and a
second one on top of it. `SELECT *` plus post-filtering means that adding a column to a
table silently widens every query already written against it, so the safety of today's code
depends on nobody adding a column tomorrow. Here the SELECT list is built from the
capability set, so a new column is invisible until somebody classifies it and grants it,
which is the default-deny rule `brain.core.field_policy` already runs on.

**The two decisions are made at different times, and that is not an accident.** A grant can
be evaluated without a row, so the column list is compiled first. A scope cannot: it is a
predicate *over* a row, so the only honest place to evaluate it is where the rows are. That
split is the whole shape of this module.

**No model ever writes SQL, and it is structural rather than a line in a prompt.** A tool
takes a `RowRequest` and an entitlement set, and there is no argument anywhere that can hold
SQL text: `assert_takes_no_sql` refuses a parameter that is unannotated, variadic, typed as
free text, or merely *named* for SQL, and `assert_no_sql_is_built_by_interpolation` refuses
a module that builds a statement by formatting one. A model that can pass a fragment can
pass `OR 1=1`, and the scope predicate is exactly what that defeats. Asking a model nicely
not to is a control that fails silently and leaves no trace of having failed.

**The asker may filter only on columns they can already see; the system may narrow on
anything.** The asymmetry is deliberate. A filter is answered by which rows come back, so
`cost = 400` over a column the asker may not read is a value oracle built out of a WHERE
clause, and repeated with different guesses it reads the column one comparison at a time. A
filter naming a column outside the compiled projection therefore compiles to a query that
returns nothing, rather than to a refusal: a refusal would say the column exists. The scope
predicate has no such limit, because it is the system narrowing rather than the asker asking,
and a departmental scope necessarily tests a column most callers cannot read.

**An empty predicate and a missing one are opposites.** A caller holding no grant for this
entity compiles to `FALSE`, never to a statement with no WHERE clause. This is the mistake
worth the constant below: a scope that goes missing does not fail closed, it fails to the
whole table, and the query still looks correct in a diff.

Three alternatives were considered and rejected.

*Fetch the row and let `brain.core.redaction` remove what the caller may not see.* The
redactor is the last line of defence and it still runs over whatever comes back; what it
cannot do is un-fetch. Making it the only line means every hidden row and every hidden
column crosses the socket, and the guarantee degrades from "the database never returned it"
to "nothing in this process wrote it down", which is not checkable.

*Give the model a SQL string parameter and validate it.* Every validator of this kind is a
parser competing with PostgreSQL's, and the one that matters is the one the server agrees
with. There is no argument to validate here, which is a shorter argument.

*Compile the scope to SQLAlchemy expression objects here rather than reusing
`brain.core.scope_sql.compile_where`.* It would remove the one `text()` call in this module,
and it would put a third rendering of the scope grammar in the repository beside
`Clause.to_sql` and `compile_where`. The LIKE escaping alone is worth not writing twice: a
stored prefix of `web_` narrows in Python and widens in SQL, and the copy that gets that
wrong is the one nobody re-reads.

Scope: nothing here opens a connection. A `RowSource` is passed in, for the reason
`brain.ops.limits` gives about holding no client: a query builder that owns a socket cannot
be tested on the cases that matter, and the cases that matter here are the empty ones.

Task ids: M15.1.1, M15.1.2, M15.1.3, M15.1.4
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, select, text

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.scope import Clause, Op, Scope
from brain.core.scope_sql import ColumnLayout, CompiledPredicate, compile_where, scope_narrows
from brain.knowledge.columns import TableClassification, close_over_derivations
from brain.tables.projection import ProjectedRecordRow

log = structlog.get_logger()

# ------------------------------------------------------------------ written-down reasons

#: Why the predicate is composed into the statement rather than applied to the result.
THE_SCOPE_PREDICATE_GOES_INSIDE_THE_QUERY = (
    "A row the caller may not see must never leave the database. Filtering after the fetch "
    "means the row was held by this process, so it was in a log line, a traceback, a "
    "debugger and whatever a retry path kept, and 'we dropped it before rendering' becomes "
    "a claim about every code path that touches the result rather than a property of the "
    "query. There is one query and there are many code paths."
)

#: Why the column list is compiled before the statement exists.
THE_COLUMN_LIST_IS_DECIDED_BEFORE_THE_QUERY = (
    "A column the caller may not see must not be in the SELECT list. Trimming it from the "
    "result set has the same problem the row has, and one more: SELECT * plus post-filtering "
    "means adding a column to a table silently widens every query already written against "
    "it, so today's code is safe only until somebody adds a column tomorrow."
)

#: Why there is no SQL-shaped argument, and why that is checked rather than asked for.
NO_ARGUMENT_CARRIES_SQL = (
    "A model that can pass a SQL fragment can pass OR 1=1, and the scope predicate is "
    "exactly what that defeats. So the tools take typed arguments whose values are bound as "
    "parameters, no argument is a string the caller composes, and the rule is checked over "
    "the signature and the source. A rule that lives in a prompt fails silently and leaves "
    "nothing behind saying it failed."
)

# --------------------------------------------------------------------- the table

#: The one table the row plane reads. `brain.tables.projection` explains why a projected
#: record is keyed by (source, entity, source_id) and why its hot fields live in one jsonb
#: column; this module only needs to know that the fields are in `fields` and the key
#: columns are real columns.
RECORD: Final = ProjectedRecordRow.__table__

#: Where a predicate field lives on `proj.record`. `source` and `entity` are promoted
#: because they are real columns and part of the primary key, so a tool's own pin compiles
#: to an indexable comparison rather than a jsonb lookup. Everything else is a projected
#: field and compiles to `fields ->> 'name'`.
ROW_LAYOUT: Final = ColumnLayout(jsonb_column="fields", promoted=frozenset({"source", "entity"}))

#: The two keys the redactor reads as the record's tag rather than as fields. Spelled out
#: here and checked against `brain.core.redaction.RESERVED_KEYS` by the test, because a
#: record that arrives without them is dropped whole as untagged.
ENTITY_KEY: Final = "entity"
ID_KEY: Final = "id"

#: Distinct parameter prefixes for the three predicates that make up one WHERE clause.
#: `CompiledPredicate.and_` refuses a collision rather than binding one scope's value into
#: another's placeholder, which is a permission bug that reads as a typo.
TOOL_PREFIX: Final = "t"
SCOPE_PREFIX: Final = "s"
FILTER_PREFIX: Final = "q"

#: The compiled predicate for a caller who reaches nothing. `FALSE` rather than an omitted
#: WHERE clause, and the difference is the whole of M15.1.4: an omitted predicate is not a
#: narrow query, it is the widest one there is.
NOTHING: Final = CompiledPredicate(where="FALSE", params={}, certainly_empty=True)

DEFAULT_ROW_LIMIT: Final = 50
MAX_ROW_LIMIT: Final = 500


class RowPlaneError(Exception):
    """A row tool was declared in a way that cannot be made safe.

    Outside the user-facing taxonomy in `brain.core.errors`, for the reason
    `brain.tools.registry.ToolRegistrationError` gives about its own: nobody asking a
    question ever sees this. It is a contract violation by whoever wrote the tool, and it
    should stop the tool existing rather than degrade an answer at request time.

    Note what does *not* raise it. A request filtering on a column the caller cannot reach
    compiles to a query returning nothing, because a refusal would confirm the column
    exists. Only the author of a tool is ever told anything by this class.
    """


# ---------------------------------------------------------------- what a tool returns


class RowRecord(Entity):
    """One projected row, tagged so the redactor can walk it.

    `extra="allow"` overrides `Entity`, and it has to: the fields on a row are whatever the
    compiled projection admitted, so they cannot be declared. The looseness is bounded on
    both sides. The projection decides which keys are constructed at all, and
    `brain.core.redaction` then asks the field policy about every one of them, so a key that
    arrived here by mistake is withheld rather than returned.

    Rejected: a declared `values: dict[str, Any]` holding the fields. The redactor walks a
    mapping by asking for its entity tag, and a nested untagged mapping is dropped whole
    (`DropReason.UNTAGGED`), so the whole answer would disappear. Fields belong at the top
    level of a record because that is where the walker looks for them.
    """

    model_config = ConfigDict(extra="allow")


class RowSource(Protocol):
    """Whatever runs a statement and hands back mappings keyed by the labels.

    A protocol rather than a session, so this module holds no connection. The split is the
    one `brain.ops.limits` and `brain.ops.limit_store` are built on: the interesting cases
    here are the empty ones, and a query builder that opens a socket cannot be tested on
    them.
    """

    def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]: ...


# ------------------------------------------------------------------- the request


class RowRequest(BaseModel):
    """What a model may ask for. Every field of it is typed, bounded, and never SQL.

    `filters` is a `Scope` rather than a bespoke filter type, and that is the point of it: a
    `Scope` is already a conjunction of field tests with a validated field name, a closed
    operator set and a value that `compile_where` binds as a parameter. There is no shape it
    can take that renders as SQL text. Reusing it also means the asker's narrowing and the
    system's narrowing are the same kind of object and compose with the same function.

    Rejected: a free-text `query` string with a validator in front of it. Every such
    validator is a SQL parser competing with the server's, and only the server's opinion
    decides what runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filters: Scope = Scope()
    #: Bounded on both ends. Zero would be a query that cannot answer anything, and an
    #: unbounded limit is how one question becomes a table scan on a shared database.
    limit: int = Field(default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT)


@dataclass(frozen=True)
class RowQuery:
    """A compiled statement, the columns it selects, and whether it can return anything.

    `certainly_empty` carries the fact the statement cannot: `FALSE` is a correct
    compilation, and a caller running it gets an empty result indistinguishable from a table
    with nothing in it. That indistinguishability is right for the asker and useless for the
    caller deciding whether to bother asking, which is what this flag is for.
    """

    entity: str
    source: str
    #: The compiled projection, sorted. Sorted so two identical entitlements produce an
    #: identical statement, which is what makes the compiled SQL comparable between callers.
    columns: tuple[str, ...]
    statement: Select[Any]
    certainly_empty: bool


# ------------------------------------------------------ the projection (M15.1.2)


def entity_capability(entity: str) -> Capability:
    """`read:<entity>`: the capability that admits a row of this kind at all.

    Distinct from the field capabilities by design. `Capability.covers` deliberately does
    not let `read:client.*` confer `read:client`, and the converse holds too, so reaching a
    row and reading a column are two separate grants. That separation is what lets the
    scope on the row grant be the WHERE clause while the field grants are the SELECT list.
    """
    return Capability(value=f"read:{entity}")


def row_scope_for(
    entity: str, entitlement: EntitlementSet, now: datetime | None = None
) -> Scope | None:
    """The scope in which this caller may read rows of `entity`, or None for no grant.

    None rather than an empty `Scope`, and the distinction is the one this module exists to
    keep. `Scope()` is the *unrestricted* scope: returning it for a caller who holds nothing
    would compile to `TRUE` and hand them the table. Callers of this function are required
    to read None as "nothing", and `compile_row_query` is where that reading is enforced.
    """
    return entitlement.scope_for(entity_capability(entity), now)


def compile_projection(
    classification: TableClassification,
    *,
    entitlement: EntitlementSet,
    rows: Scope | None,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Which columns go in the SELECT list, decided before any statement exists (M15.1.2).

    Three steps, and the middle one is the one that is easy to leave out.

    **A column needs a grant.** No grant covering the column's required capability, no
    column. A column nothing classifies never had a rule, so it is never considered, which
    is `brain.core.field_policy`'s default-deny arriving here for free.

    **A column needs a grant that covers every row this query can return.** A grant carries
    a scope, and a column grant narrower than the row grant would be a per-row column
    decision. There is nowhere to make that decision except after the fetch, which is the
    thing this module exists not to do, so such a column is dropped instead. `scope_narrows`
    is sound and incomplete, so the failure direction is losing a column somebody could have
    seen rather than showing one they could not.

    **The derivation closure runs here, not afterwards.** `close_over_derivations` withholds
    a visible column that reconstructs a withheld one, and it is a property of the *set* of
    columns, so it can only be computed once the set is known. Running it after the fetch
    would mean the cost column was derivable from data already inside this process, which is
    the same failure as post-filtering a row and is invisible for the same reason.

    This is not a second permission decision. It uses the same grants and the same
    classification the redactor uses, and it drops anything it cannot prove, so what it
    admits is a subset of what `brain.core.redaction.compute_mask` would admit for any row
    the query can return. The redactor still runs over the result: this narrows early, it
    does not replace the last line of defence.
    """
    if rows is None:
        # No grant on the entity. Not "no restriction": see `row_scope_for`.
        return ()
    admitted: set[str] = set()
    for rule in classification.rules:
        held = entitlement.scope_for(rule.required_capability, now)
        if held is None:
            continue
        if not scope_narrows(rows, held):
            continue
        admitted.add(rule.column)
    return tuple(sorted(close_over_derivations(frozenset(admitted), classification)))


# -------------------------------------------------------------- the tool (M15.1.1)


@dataclass(frozen=True)
class RowTool:
    """One typed tool over one entity of one source (M15.1.1).

    Per source as well as per entity, because `proj.record` is keyed that way: Freshdesk
    company 42 and Xero contact 42 are different companies, and a tool that pinned only the
    entity would read both. The pin is a `Scope` rather than a pair of comparisons written
    here, so that it composes with the caller's scope through the same `compile_where` and
    the same `and_`, and so that the SERVICE registration rule in `brain.tools.registry` has
    a real predicate to check rather than a decorative one.

    `IdentityMode.SERVICE` is the honest declaration. A projected row was fetched under
    somebody else's credentials long before this query runs, so the source is not enforcing
    anything now and ours are the only permissions there are. That is precisely the case
    `assert_service_tool_is_scoped` refuses to let through unscoped.
    """

    source: str
    classification: TableClassification
    description: str

    def __post_init__(self) -> None:
        if not self.source:
            msg = (
                "a row tool needs the source it reads; without one it would read every "
                "system's records for this entity, and two sources' record ids collide by "
                "coincidence of integers"
            )
            raise RowPlaneError(msg)
        reserved = sorted(c for c in self.classification.columns() if c in (ENTITY_KEY, ID_KEY))
        if reserved:
            msg = (
                f"{self.entity} classifies {reserved}, which the redactor reads as the "
                "record's tag rather than as fields; a column by either of those names would "
                "overwrite the tag and the record would be dropped as untagged"
            )
            raise RowPlaneError(msg)

    @property
    def entity(self) -> str:
        return self.classification.entity

    @property
    def name(self) -> str:
        """`source.read_entity`, which is the `source.verb_noun` grammar the registry wants."""
        return f"{self.source}.read_{self.entity}"

    @property
    def scope(self) -> Scope:
        """The tool's own pin: this source, this entity, and nothing else."""
        return Scope(
            clauses=(
                Clause(field="source", op=Op.EQ, value=self.source),
                Clause(field=ENTITY_KEY, op=Op.EQ, value=self.entity),
            )
        )

    def definition(self) -> ToolDefinition:
        """What the catalogue describes to a model.

        `args_schema` is `RowRequest`'s own JSON schema, so the argument surface a model sees
        is the argument surface this module compiles. A hand-written schema is a second
        description of the same thing, and the two disagree the first time a field moves.
        """
        return ToolDefinition(
            name=self.name,
            description=self.description,
            entity=self.entity,
            args_schema=RowRequest.model_json_schema(),
            required_capability=entity_capability(self.entity).value,
            side_effect=SideEffect.NONE,
            identity_mode=IdentityMode.SERVICE,
            source=self.source,
        )

    def reader(self, records: RowSource) -> Callable[..., TypedResult[RowRecord]]:
        """The handler a registry registers, bound to where the rows come from.

        A closure rather than a method, so that the signature a registry inspects carries
        only what a model may pass. `RowSource` and the tool itself are wiring: they are
        supplied by whoever builds the registry, never by a caller and never by a model, and
        a parameter a model cannot reach is a parameter that cannot carry a fragment.
        """

        def read(
            request: RowRequest,
            *,
            entitlement: EntitlementSet,
            now: datetime | None = None,
        ) -> TypedResult[RowRecord]:
            return read_rows(self, request, entitlement=entitlement, records=records, now=now)

        return read


# ----------------------------------------------------- the statement (M15.1.1, M15.1.4)


def _selected(columns: Sequence[str]) -> list[Any]:
    """The SELECT list: the tag, the id, and exactly the admitted columns.

    The two key columns are always selected, and that is not a hole in the projection. The
    redactor treats them as the record's tag rather than as fields, and a record arriving
    without them is dropped whole as untagged, so withholding them would turn "you may see
    two of these columns" into "you may see nothing". A record left holding nothing but its
    tag is dropped anyway, by `brain.core.redaction.has_substance`.

    Every field column is rendered as `fields ->> :param`, so the column *name* is a bound
    parameter rather than text spliced into the statement. Only the label is an identifier,
    and SQLAlchemy quotes that.
    """
    chosen: list[Any] = [
        RECORD.c[ENTITY_KEY].label(ENTITY_KEY),
        RECORD.c.source_id.label(ID_KEY),
    ]
    chosen.extend(RECORD.c.fields[name].astext.label(name) for name in columns)
    return chosen


def compile_row_query(
    tool: RowTool,
    request: RowRequest,
    *,
    entitlement: EntitlementSet,
    now: datetime | None = None,
) -> RowQuery:
    """Compile one caller's question into one statement (M15.1.1, M15.1.2, M15.1.4).

    The order is the argument. The projection is computed first, from capabilities alone,
    because it can be; the predicate is composed second and goes into the statement, because
    a scope is a predicate over a row and there is no row yet.

    One path, deliberately. The tool's pin, the caller's scope and the asker's filters are
    all compiled by the same function and combined by the same `and_`, and there is no
    branch that can skip the middle one. A branch that skipped the filters when there were
    none would be the same shape as a branch that skips the scope when there is none, and
    the second one is a table handed to a stranger.

    Note what `certainly_empty` does and does not mean. It says the statement cannot return
    a row; it never says the caller may see anything, because a satisfiable predicate over
    an empty table is also empty and the two must read identically to the asker.
    """
    rows = row_scope_for(tool.entity, entitlement, now)
    columns = compile_projection(tool.classification, entitlement=entitlement, rows=rows, now=now)

    pinned = compile_where(tool.scope, ROW_LAYOUT, param_prefix=TOOL_PREFIX)
    caller = NOTHING if rows is None else compile_where(rows, ROW_LAYOUT, param_prefix=SCOPE_PREFIX)
    asked = _compile_filters(tool, request, columns)
    predicate = pinned.and_(caller).and_(asked)

    statement = (
        select(*_selected(columns))
        .where(RECORD.c.deleted_at.is_(None))
        # The whole of M15.1.4 is this line being here rather than in a wrapper around the
        # result. `text` is handed a fragment `compile_where` built out of a validated
        # scope, and every value in it is bound; nothing a caller supplied is ever spliced.
        .where(text(predicate.where).bindparams(**predicate.params))
        # A LIMIT without an ORDER BY returns an arbitrary subset, and an arbitrary subset
        # that differs between two identical questions reads as a permission problem.
        .order_by(RECORD.c.source_id)
        .limit(request.limit)
    )
    return RowQuery(
        entity=tool.entity,
        source=tool.source,
        columns=columns,
        statement=statement,
        certainly_empty=predicate.certainly_empty,
    )


def _compile_filters(
    tool: RowTool, request: RowRequest, columns: Sequence[str]
) -> CompiledPredicate:
    """The asker's own narrowing, refused into emptiness when it reaches past the projection.

    A filter is answered by which rows come back, so filtering on a column the caller may not
    read is a value oracle: `cost = 400` returning a row says what the cost is, one guess at
    a time, through a column the SELECT list correctly withheld. The whole request therefore
    compiles to nothing.

    Nothing rather than a refusal, and nothing rather than dropping the clause. A refusal
    would confirm the column exists, which is the distinction `brain.core.errors` collapses
    everywhere else. Dropping the clause would *widen* the result the asker asked to narrow,
    and they would read the extra rows as an answer.

    An undeclared column takes the same path as an unreachable one, so the asker cannot tell
    a typo from a permission, which is the same rule one level down.

    The operator is told, because a filter that silently matches nothing is otherwise
    undebuggable. The log is read by an operator and not by the asker, exactly as
    `brain.core.redaction` logs a dropped object.
    """
    unreachable = sorted({c.field for c in request.filters.clauses} - set(columns))
    if unreachable:
        log.warning(
            "row_plane.filter_outside_projection",
            tool=tool.name,
            entity=tool.entity,
            fields=unreachable,
        )
        return NOTHING
    return compile_where(request.filters, ROW_LAYOUT, param_prefix=FILTER_PREFIX)


def read_rows(
    tool: RowTool,
    request: RowRequest,
    *,
    entitlement: EntitlementSet,
    records: RowSource,
    now: datetime | None = None,
) -> TypedResult[RowRecord]:
    """Compile, fetch, and build records out of the projection rather than out of the rows.

    The record is assembled from `query.columns`, so a source handing back a key the
    projection did not ask for cannot widen the answer. That is a shape rather than a
    promise: there is no code path here that copies a row wholesale, so there is nothing to
    audit for whether it remembered to filter.

    A statement that cannot return a row is not run. `is_unsatisfiable` says its own purpose
    is deciding whether to bother asking, and asking anyway would spend a round trip to be
    told what the compiler already knew.
    """
    query = compile_row_query(tool, request, entitlement=entitlement, now=now)
    fetched: Sequence[Mapping[str, Any]] = () if query.certainly_empty else records.rows(query)
    built = tuple(
        RowRecord(
            entity=query.entity,
            id=str(row[ID_KEY]),
            **{name: row[name] for name in query.columns if name in row},
        )
        for row in fetched
    )
    return TypedResult(
        records=built,
        source=tool.source,
        # No clock is read here, for the reason `brain.knowledge.visibility` gives: a module
        # that reads the clock cannot be tested at the boundary that goes wrong.
        fetched_at=now.isoformat() if now is not None else "",
        truncated=len(built) == request.limit,
    )


# ------------------------------------------------- no model writes SQL (M15.1.3)

#: Parameter names that read as SQL whatever they are typed as. Refused by name for the
#: reason `brain.connectors.contract.assert_holds_no_credential` refuses an attribute named
#: for a credential: a smuggled fragment is nearly always an ordinary `str`, so a type-only
#: rule would pass `where: str` while refusing an honestly typed parameter.
SQL_ARGUMENT_NAME_RE: Final = re.compile(
    r"sql|query|where|having|clause|statement|stmt|predicate|expression|expr|order_by|raw"
)

#: Annotations that can hold text or a statement. `str` is on the list on purpose: a tool
#: argument typed `str` is an argument that can hold `OR 1=1`, and a value that genuinely
#: needs to be a string reaches the query inside a `RowRequest`, where it is bound.
SQL_CAPABLE_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "str",
        "bytes",
        "Any",
        "object",
        "AnyStr",
        "LiteralString",
        "TextClause",
        "Executable",
        "ClauseElement",
        "Select",
        "Connection",
        "Session",
        "Engine",
        "text",
    }
)

#: Calls that turn a string into something a database runs. Anything reaching one of these
#: by way of a formatted string is the injection shape, whoever wrote it.
SQL_SINKS: Final[frozenset[str]] = frozenset(
    {"text", "execute", "exec_driver_sql", "executemany", "scalar", "scalars"}
)


def _annotation_text(annotation: object) -> str:
    """One rendering of an annotation, whether it arrived as a string or an object.

    The same helper `brain.core.redaction` and `brain.connectors.contract` each carry, and
    for the same reason: a module with `from __future__ import annotations` hands over the
    text as written, and one without it hands over an object.
    """
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _names_in(annotation: object) -> frozenset[str]:
    """Every identifier in an annotation, however it is spelled.

    Crude on purpose, exactly as in `brain.core.redaction._names_in`: `str`, `"str | None"`,
    `builtins.str` and `list[str]` all have to read the same, and a parser that understood
    the type algebra would be a second opinion about what an annotation means.
    """
    return frozenset(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _annotation_text(annotation)))


def assert_takes_no_sql(tool: Callable[..., object]) -> None:
    """Refuse a tool with any parameter that could carry SQL text (M15.1.3).

    Enforced on what a tool can be *given* rather than on what it does, which is the shape
    `brain.core.redaction.assert_channel_adapter` uses and for the same reason: what a
    function does is a body somebody edits, and what it can be handed is a signature a test
    can read.

    Four refusals.

    **`*args` or `**kwargs`.** A signature that accepts anything has declared nothing, so it
    cannot be shown not to accept a fragment.

    **An unannotated parameter.** Default-deny, the same answer an unclassified field gets.
    An unannotated parameter can hold a string.

    **A parameter named for SQL.** Refused whatever its type. `where`, `query` and `sql` are
    what a fragment arrives as, and it arrives as a `str`, so a rule that only looked at
    types would pass the dishonest spelling and refuse nothing.

    **A parameter typed as free text or as a statement.** The refusal that matters. There is
    no argument on a row tool that needs to be a bare `str`: a value the asker supplies
    travels inside `RowRequest.filters`, where `compile_where` binds it.

    What this does not do is read the body, so a determined author can still reach a module
    global. That is why `assert_no_sql_is_built_by_interpolation` exists beside it and why
    the scope predicate is in the statement regardless: this refuses the argument, that
    refuses the construction, and the predicate refuses the consequence.
    """
    try:
        signature = inspect.signature(tool)
    except (TypeError, ValueError) as exc:
        msg = f"{getattr(tool, '__name__', tool)!r} has no readable signature to check"
        raise RowPlaneError(msg) from exc

    name = getattr(tool, "__name__", repr(tool))
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            # A bound method's receiver carries the tool, not an argument a model supplies.
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            msg = (
                f"row tool {name!r} takes {parameter.name!r} as *args or **kwargs; a "
                "signature that accepts anything has declared nothing, so it cannot be "
                "shown never to receive a SQL fragment"
            )
            raise RowPlaneError(msg)
        if parameter.annotation is inspect.Parameter.empty:
            msg = (
                f"row tool {name!r} has an unannotated parameter {parameter.name!r}; an "
                "unannotated parameter can hold a SQL fragment, so it is refused for the "
                "same reason an unclassified field is withheld"
            )
            raise RowPlaneError(msg)
        if SQL_ARGUMENT_NAME_RE.search(parameter.name.casefold()):
            msg = (
                f"row tool {name!r} takes {parameter.name!r}, which is named for SQL; a "
                "fragment arrives as an ordinary string, so the name is refused whatever "
                "the annotation says"
            )
            raise RowPlaneError(msg)
        carried = sorted(_names_in(parameter.annotation) & SQL_CAPABLE_TYPE_NAMES)
        if carried:
            msg = (
                f"row tool {name!r} would be handed {carried} in {parameter.name!r}; a "
                "value the asker supplies travels inside a RowRequest, where it is bound as "
                "a parameter, and nothing on a row tool needs to be free text"
            )
            raise RowPlaneError(msg)


def _is_interpolating(node: ast.expr) -> bool:
    """Whether this expression builds a string out of other values.

    Four shapes, which is every way Python composes a string without a helper: an f-string,
    `%` formatting, `+` concatenation, and `.format`/`.join`. A call to something else that
    returns a composed string is out of reach here, which the caller's docstring says
    plainly rather than implying otherwise.
    """
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (node.func.attr in ("format", "join"))
    )


def _sink_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def assert_no_sql_is_built_by_interpolation(module: ModuleType) -> None:
    """Refuse a module that composes a statement out of a formatted string (M15.1.3).

    Read over the parsed syntax tree rather than over the source text, because a text search
    is satisfied by its own explanation. Two tests in this repository have been passed by
    their own docstrings, and a rule about SQL is exactly the kind whose docstring contains
    the string it is looking for.

    Two refusals, both about an argument to a call that runs text:
    `text(f"... {value} ...")`, and the same thing one line apart, where the f-string is
    assigned to a name and the name is passed.

    **What this proves and what it does not.** It proves no statement in this module is
    built by the four shapes Python composes strings with. It is name-based and does not
    follow a value through a container, an attribute or a function call, so it is a check on
    the shape injection actually arrives in rather than an information-flow proof. The other
    half of M15.1.3 is `assert_takes_no_sql`, which closes the door a model knocks on: with
    no argument able to hold text, there is nothing for an interpolation to interpolate.
    """
    tree = ast.parse(inspect.getsource(module))

    composed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_interpolating(node.value):
            composed.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_interpolating(node.value)
            and isinstance(node.target, ast.Name)
        ):
            composed.add(node.target.id)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _sink_name(node) not in SQL_SINKS:
            continue
        for argument in node.args:
            if _is_interpolating(argument):
                offenders.append(f"line {node.lineno}: {_sink_name(node)}(<formatted string>)")
            elif isinstance(argument, ast.Name) and argument.id in composed:
                offenders.append(f"line {node.lineno}: {_sink_name(node)}({argument.id})")

    if offenders:
        listed = "\n".join(f"  - {o}" for o in offenders)
        msg = (
            f"{module.__name__} builds SQL by interpolation:\n{listed}\n"
            "every value in a row-plane statement is bound as a parameter, because a value "
            "spliced into a statement is a value that can end the statement"
        )
        raise RowPlaneError(msg)
