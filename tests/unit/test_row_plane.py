"""The row plane: what is in the SELECT list, what is in the WHERE clause, and who wrote it.

Every assertion here is made against a statement compiled to PostgreSQL rather than against
the Python objects that built it, because the thing being claimed is about the SQL. A test
that asserted `query.columns` would pass with the projection computed correctly and the
statement built from something else entirely.

Task ids: M15.1.1, M15.1.2, M15.1.3, M15.1.4
"""

from __future__ import annotations

import dataclasses
import importlib.util
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Select, create_engine
from sqlalchemy.pool import NullPool

from brain.core.department import department_scope
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import IdentityMode, TypedResult
from brain.core.field_policy import Classification
from brain.core.redaction import RESERVED_KEYS, compute_mask
from brain.core.scope import Clause, Op, Scope
from brain.knowledge import rows as row_plane
from brain.knowledge.columns import PRICE_LIST, ColumnRule, TableClassification
from brain.knowledge.rows import (
    ENTITY_KEY,
    ID_KEY,
    MAX_ROW_LIMIT,
    RECORD,
    RowPlaneError,
    RowQuery,
    RowRequest,
    RowTool,
    assert_no_sql_is_built_by_interpolation,
    assert_takes_no_sql,
    compile_projection,
    compile_row_query,
    read_rows,
    row_scope_for,
)
from brain.tools.registry import ToolRegistry

#: A PostgreSQL dialect to render statements against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

#: A classification with no derivations between its columns, used everywhere the derivation
#: closure would otherwise be a second variable in the experiment. `PRICE_LIST` is the one
#: with derivations and is used only where they are the subject.
TICKETS = TableClassification(
    entity="ticket",
    rules=(
        ColumnRule(
            column="subject",
            required_capability=Capability(value="read:ticket.subject"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="status",
            required_capability=Capability(value="read:ticket.status"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="hours_remaining",
            required_capability=Capability(value="read:ticket.hours_remaining"),
            classification=Classification.CONFIDENTIAL,
        ),
    ),
)

TICKET_TOOL = RowTool(
    source="freshdesk",
    classification=TICKETS,
    description="Read a maintenance ticket for a client.",
)

PRICE_TOOL = RowTool(
    source="xero",
    classification=PRICE_LIST,
    description="Read a row of the standard price list.",
)

SEES_SUBJECT = ("read:ticket", "read:ticket.subject")
SEES_EVERY_TICKET_COLUMN = (
    "read:ticket",
    "read:ticket.subject",
    "read:ticket.status",
    "read:ticket.hours_remaining",
)


def ents(*caps: str, scope: Scope | None = None, principal: str = "p_wei_ling") -> EntitlementSet:
    """An entitlement set holding these capabilities in one scope."""
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=scope or Scope()) for c in caps),
    )


def rendered(query: RowQuery) -> str:
    """The statement as PostgreSQL would receive it, with values inlined so a test can read
    them. The production path binds them; `literal_binds` is a rendering choice made here."""
    return str(query.statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))


def bound(query: RowQuery) -> dict[str, Any]:
    """The parameters the statement carries, without inlining anything."""
    return dict(query.statement.compile(dialect=DIALECT).params)


def parameterised(query: RowQuery) -> str:
    """The statement as it is actually sent: placeholders, not values."""
    return str(query.statement.compile(dialect=DIALECT))


class Rows:
    """A `RowSource` that hands back whatever it was given and remembers being asked."""

    def __init__(self, *records: dict[str, Any]) -> None:
        self.records = list(records)
        self.asked = 0

    def rows(self, query: RowQuery) -> list[dict[str, Any]]:
        self.asked += 1
        return self.records


class Refuses:
    """A `RowSource` that fails if it is ever asked."""

    def rows(self, query: RowQuery) -> list[dict[str, Any]]:
        raise AssertionError("a statement that cannot return a row was sent to the database")


# ------------------------------------------------- the projection (M15.1.2)


def test_a_callers_capabilities_decide_the_select_list() -> None:
    """M15.1.2 itself. Deleting this lets the projection be computed from the entity's
    columns rather than from the asker's grants, which is `SELECT *` with extra steps: every
    caller gets every column and the field policy stops meaning anything at the row plane."""
    narrow = rendered(compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT)))
    wide = rendered(
        compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_EVERY_TICKET_COLUMN))
    )
    assert "AS subject" in narrow
    assert "AS hours_remaining" not in narrow
    assert "AS subject" in wide
    assert "AS hours_remaining" in wide


def test_a_column_the_caller_lacks_is_absent_from_the_sql_entirely() -> None:
    """The stronger half of the claim above, and the one worth asserting separately: not
    "trimmed from the result" and not "selected and then dropped", but never named. Deleting
    this permits a projection that selects everything and filters afterwards, at which point
    the hidden column has crossed the socket and the guarantee is about what this process
    remembered to do rather than about what the database was asked for."""
    query = compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT))
    assert "hours_remaining" not in rendered(query)
    # Not hiding in a bound parameter either. The column name is bound rather than spliced,
    # so a test reading only the SQL text would miss a column smuggled in as a value.
    assert "hours_remaining" not in parameterised(query)
    assert "hours_remaining" not in bound(query).values()


def test_a_column_added_to_the_table_does_not_appear_for_a_caller_who_lacks_it() -> None:
    """The reason default-deny has to reach the SELECT list rather than stopping at the
    redactor. A business column here is a key in the projected `fields` object, so adding one
    is adding a rule; with `SELECT *` the new column would be fetched for everybody from the
    moment it existed. Deleting this test removes the only thing standing between "safe
    today" and "safe until somebody adds a column"."""
    widened = dataclasses.replace(
        TICKETS,
        rules=(
            *TICKETS.rules,
            ColumnRule(
                column="internal_escalation_note",
                required_capability=Capability(value="read:ticket.internal_escalation_note"),
                classification=Classification.RESTRICTED,
            ),
        ),
    )
    tool = dataclasses.replace(TICKET_TOOL, classification=widened)

    unchanged = compile_row_query(tool, RowRequest(), entitlement=ents(*SEES_EVERY_TICKET_COLUMN))
    assert "internal_escalation_note" not in rendered(unchanged)
    assert "AS subject" in rendered(unchanged)

    granted = compile_row_query(
        tool,
        RowRequest(),
        entitlement=ents(*SEES_EVERY_TICKET_COLUMN, "read:ticket.internal_escalation_note"),
    )
    assert "AS internal_escalation_note" in rendered(granted)


def test_a_key_nobody_classified_is_never_in_the_select_list() -> None:
    """Default-deny again, from the other side: the SELECT list is built from the
    classification rather than from whatever keys a connector happened to project. Without
    this a wildcard grant plus an unclassified key is an open column, and an over-returned
    column looks exactly like one that was meant to be public."""
    query = compile_row_query(
        TICKET_TOOL, RowRequest(), entitlement=ents("read:ticket", "read:ticket.*")
    )
    assert "AS subject" in rendered(query)
    assert "unclassified_note" not in rendered(query)


def test_a_column_whose_grant_does_not_cover_every_row_is_not_selected() -> None:
    """A grant carries a scope, so a column grant narrower than the row grant would be a
    per-row column decision, and there is nowhere to make one except after the fetch. Deleting
    this makes a departmental column grant behave like a company-wide one for every row the
    query returns, which is the exact mistake a per-person permission cache makes."""
    finance_only = EntitlementSet(
        principal_id="p_wei_ling",
        grants=(
            Grant(capability=Capability(value="read:ticket"), scope=Scope()),
            Grant(capability=Capability(value="read:ticket.subject"), scope=Scope()),
            Grant(
                capability=Capability(value="read:ticket.hours_remaining"),
                scope=department_scope("finance"),
            ),
        ),
    )
    assert "AS hours_remaining" not in rendered(
        compile_row_query(TICKET_TOOL, RowRequest(), entitlement=finance_only)
    )

    everywhere = EntitlementSet(
        principal_id="p_wei_ling",
        grants=(
            Grant(capability=Capability(value="read:ticket"), scope=department_scope("finance")),
            Grant(capability=Capability(value="read:ticket.subject"), scope=Scope()),
            Grant(
                capability=Capability(value="read:ticket.hours_remaining"),
                scope=department_scope("finance"),
            ),
        ),
    )
    assert "AS hours_remaining" in rendered(
        compile_row_query(TICKET_TOOL, RowRequest(), entitlement=everywhere)
    )


def test_the_derivation_closure_runs_before_the_query_is_built() -> None:
    """Withholding `cost` achieves nothing while `sell_price` and `margin` are both selected,
    because cost is the subtraction. Deleting this test lets the closure move to after the
    fetch, where both inputs have already been read out of the database and the cost is
    derivable from data sitting in this process. `brain.knowledge.columns` explains why the
    most sensitive input is the one withheld."""
    without_cost = ents(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        "read:price_list.margin",
    )
    narrowed = rendered(compile_row_query(PRICE_TOOL, RowRequest(), entitlement=without_cost))
    assert "AS sell_price" in narrowed
    assert "AS margin" not in narrowed
    assert "AS cost" not in narrowed

    everything = ents(
        "read:price_list",
        "read:price_list.sku",
        "read:price_list.name",
        "read:price_list.sell_price",
        "read:price_list.margin",
        "read:price_list.cost",
    )
    complete = rendered(compile_row_query(PRICE_TOOL, RowRequest(), entitlement=everything))
    assert "AS margin" in complete
    assert "AS cost" in complete


def test_the_projection_never_admits_a_column_the_redactor_would_withhold() -> None:
    """The row plane must not become a second permission mechanism that disagrees with the
    first. It narrows early using the same grants and the same classification, so what it
    admits has to be a subset of what `compute_mask` admits for any row the query can return.
    Deleting this lets the two drift, and the one that disagrees permissively is the one
    nobody notices."""
    entitlement = EntitlementSet(
        principal_id="p_wei_ling",
        grants=(
            Grant(capability=Capability(value="read:ticket"), scope=department_scope("web")),
            Grant(capability=Capability(value="read:ticket.subject"), scope=Scope()),
            Grant(capability=Capability(value="read:ticket.status"), scope=department_scope("web")),
        ),
    )
    row = {"subject": "Contact form broken", "status": "open", "department": "web"}
    columns = compile_projection(
        TICKETS,
        entitlement=entitlement,
        rows=row_scope_for("ticket", entitlement),
    )
    mask = compute_mask(
        "ticket",
        TICKETS.columns(),
        entitlement=entitlement,
        policy=TICKETS.policy(),
        row=row,
    )
    assert columns, "the positive half: this caller can see something"
    assert set(columns) <= set(mask.allowed)


# ------------------------------------------ the scope predicate (M15.1.4)


def test_the_scope_predicate_is_in_the_where_clause_of_the_statement_that_fetches_rows() -> None:
    """M15.1.4, and the exact claim is worth stating because it is narrower than the leaf's
    name suggests.

    This proves the predicate is *in the statement*: the FROM is the base table rather than a
    subquery, there is one SELECT and one WHERE, and the compiled WHERE clause of that
    statement contains the scope fragment. It does **not** prove PostgreSQL pushes the
    predicate down to the scan, or that no row is read off disk before the filter applies.
    That is a question about a plan, it needs `EXPLAIN` against a live server, and there is no
    such test in this repository: the place it would live is the `stack` job in
    `.github/workflows/ci.yml`, which is the only CI job with a database in it.

    Deleting this test allows the predicate to move into a wrapper - `SELECT * FROM (SELECT
    ... ) WHERE department = ...` - which passes any test that merely searches the SQL for the
    fragment, and which reads every row of the inner query before filtering."""
    query = compile_row_query(
        TICKET_TOOL,
        RowRequest(),
        entitlement=ents(*SEES_SUBJECT, scope=department_scope("web")),
    )
    statement: Select[Any] = query.statement

    assert list(statement.get_final_froms()) == [RECORD], "the statement reads the table itself"

    sql = rendered(query)
    assert sql.count("SELECT") == 1, "a second SELECT would mean the predicate wraps a subquery"
    assert sql.count("WHERE") == 1

    where = statement.whereclause
    assert where is not None
    compiled_where = str(where.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))
    assert "fields ->> 'department' = 'web'" in compiled_where


def test_a_scope_that_admits_nothing_compiles_to_false_rather_than_to_no_where() -> None:
    """The mistake this whole leaf exists to prevent. A caller with no grant on the entity
    must produce a query that returns nothing; a missing predicate is not a narrow query, it
    is the widest one there is, and it looks perfectly correct in a diff. Deleting this test
    permits "no scope" to be compiled as "no restriction", which hands the table to a caller
    who holds nothing."""
    query = compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents("read:ticket.subject"))
    sql = rendered(query)
    assert "WHERE" in sql
    assert "FALSE" in sql
    assert query.certainly_empty is True

    reachable = compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT))
    assert "FALSE" not in rendered(reachable)
    assert reachable.certainly_empty is False


def test_two_grants_that_do_not_overlap_compile_to_a_query_returning_nothing() -> None:
    """Composition intersects, so somebody holding the same capability in two departments
    ends up with a scope no row satisfies. That is the correct conservative answer and it has
    to survive compilation. Without this, an impossible scope could compile to a fragment that
    matches nothing in Python and something in SQL, which is the divergence
    `brain.core.scope_sql` exists to refuse."""
    contradictory = EntitlementSet(
        principal_id="p_wei_ling",
        grants=(
            Grant(capability=Capability(value="read:ticket"), scope=department_scope("web")),
            Grant(capability=Capability(value="read:ticket"), scope=department_scope("finance")),
            Grant(capability=Capability(value="read:ticket.subject"), scope=Scope()),
        ),
    )
    query = compile_row_query(TICKET_TOOL, RowRequest(), entitlement=contradictory)
    assert "FALSE" in rendered(query)
    assert query.certainly_empty is True


def test_two_callers_with_different_scopes_compile_to_different_sql() -> None:
    """The same question from two people is two different statements, which is what "the
    scope predicate is inside the query" means in practice. If it compiled to one statement
    the difference would have to be applied afterwards, in this process, to rows both callers
    had already caused to be read."""
    web = compile_row_query(
        TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT, scope=department_scope("web"))
    )
    finance = compile_row_query(
        TICKET_TOOL,
        RowRequest(),
        entitlement=ents(*SEES_SUBJECT, scope=department_scope("finance")),
    )
    assert rendered(web) != rendered(finance)
    assert web.columns == finance.columns, "only the predicate differs, not the projection"


def test_every_value_in_the_statement_is_bound_rather_than_spliced() -> None:
    """The parameterised half of M15.1.1. A value spliced into a statement is a value that
    can end the statement, and the only reason the scope predicate cannot be escaped from is
    that nothing in it is text. The jsonb key name is not a caller-supplied value: it comes
    from `Clause.field`, which is validated against a pattern that admits no quote."""
    query = compile_row_query(
        TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT, scope=department_scope("web"))
    )
    sql = parameterised(query)
    assert "'web'" not in sql
    assert "web" in bound(query).values()
    assert "ticket" in bound(query).values()


def test_a_retired_row_is_never_in_the_result_set() -> None:
    """Rows are retired rather than removed, so every query has to say so. Deleting this lets
    a soft-deleted record answer a question; row-level security is the second wall and the
    point of having both is that neither is the only one."""
    query = compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT))
    assert "deleted_at IS NULL" in rendered(query)


def test_the_statement_is_pinned_to_one_source_and_one_entity() -> None:
    """`proj.record` holds every source's records for every entity, and record ids are each
    source's own namespace: Freshdesk company 42 and Xero contact 42 are different companies.
    Without the pin a ticket tool reads invoices, and the field policy looked up by entity
    would withhold every column of them, which reads as a permission failure rather than as
    the missing predicate it is."""
    sql = rendered(compile_row_query(TICKET_TOOL, RowRequest(), entitlement=ents(*SEES_SUBJECT)))
    assert "entity = 'ticket'" in sql
    assert "source = 'freshdesk'" in sql


# --------------------------------------- the asker's own filters (M15.1.1)


def test_a_filter_on_a_visible_column_is_bound_into_the_where_clause() -> None:
    """The positive case for the filter path. A guard tested only by its refusals is
    satisfied by a function that refuses everything, and a row tool that quietly ignored every
    filter would return more rows than were asked for while every refusal test still passed."""
    query = compile_row_query(
        TICKET_TOOL,
        RowRequest(filters=Scope(clauses=(Clause(field="status", op=Op.EQ, value="open"),))),
        entitlement=ents(*SEES_EVERY_TICKET_COLUMN),
    )
    assert "fields ->> 'status' = 'open'" in rendered(query)
    assert "open" in bound(query).values()
    assert query.certainly_empty is False


def test_a_filter_on_a_column_the_caller_cannot_see_returns_nothing() -> None:
    """A filter is answered by which rows come back, so filtering on a hidden column reads it
    one comparison at a time: `hours_remaining = 3` returning a row says what the number is.
    Deleting this turns the WHERE clause into a value oracle over exactly the columns the
    SELECT list was careful to withhold."""
    query = compile_row_query(
        TICKET_TOOL,
        RowRequest(filters=Scope(clauses=(Clause(field="hours_remaining", op=Op.EQ, value="3"),))),
        entitlement=ents(*SEES_SUBJECT),
    )
    assert query.certainly_empty is True
    assert "FALSE" in rendered(query)
    assert "hours_remaining" not in rendered(query)


def test_a_filter_on_a_column_that_does_not_exist_is_answered_the_same_way() -> None:
    """A typo and a permission have to be indistinguishable, or the difference between them
    is a probe: ask for a column, see whether you get a refusal or an empty result, and the
    schema falls out one guess at a time. Deleting this permits an error message that
    confirms which columns exist."""
    query = compile_row_query(
        TICKET_TOOL,
        RowRequest(filters=Scope(clauses=(Clause(field="no_such_column", op=Op.EQ, value="x"),))),
        entitlement=ents(*SEES_EVERY_TICKET_COLUMN),
    )
    assert query.certainly_empty is True
    assert "FALSE" in rendered(query)


def test_a_request_cannot_ask_for_an_unbounded_number_of_rows() -> None:
    """One question must not become a table scan on a database the whole company shares.
    Deleting the bound lets a model ask for every row of a projected entity, and the cost
    lands on everybody else's requests rather than on the asker's."""
    with pytest.raises(ValidationError):
        RowRequest(limit=0)
    with pytest.raises(ValidationError):
        RowRequest(limit=MAX_ROW_LIMIT + 1)
    assert RowRequest(limit=MAX_ROW_LIMIT).limit == MAX_ROW_LIMIT


# --------------------------------- no model ever writes SQL (M15.1.3)


def test_a_row_tool_has_no_parameter_that_could_carry_sql() -> None:
    """M15.1.3, on the tool a registry actually registers. This is the positive case: the
    real handler passes every refusal below, so the refusals are not satisfied by a check that
    rejects everything. Deleting it lets the rule tighten until no real tool can be
    registered, at which point somebody removes the rule."""
    assert_takes_no_sql(TICKET_TOOL.reader(Rows()))


def test_a_parameter_typed_as_free_text_is_refused() -> None:
    """The refusal that matters. An argument typed `str` is an argument that can hold
    `OR 1=1`, and a value the asker legitimately needs travels inside a `RowRequest`, where
    it is bound as a parameter. Deleting this permits exactly the design this leaf exists to
    forbid: a model handing over text that becomes part of a statement."""

    def reads(request: RowRequest, *, term: str) -> TypedResult[row_plane.RowRecord]:
        raise NotImplementedError

    with pytest.raises(RowPlaneError, match="free text"):
        assert_takes_no_sql(reads)


def test_a_parameter_named_for_sql_is_refused_whatever_its_type() -> None:
    """A fragment arrives as an ordinary string, so a type-only rule would pass the dishonest
    spelling. This mirrors `assert_holds_no_credential`, which refuses an attribute named for
    a credential whatever it is annotated as, and for the same reason. Deleting it lets
    `where` back in as long as somebody annotates it plausibly."""

    def reads(request: RowRequest, *, where: RowRequest) -> TypedResult[row_plane.RowRecord]:
        raise NotImplementedError

    with pytest.raises(RowPlaneError, match="named for SQL"):
        assert_takes_no_sql(reads)


def test_an_unannotated_parameter_is_refused() -> None:
    """Default-deny, the same answer an unclassified field gets. An unannotated parameter can
    hold a string, so it cannot be shown safe, and "probably fine" is not a property."""

    # The missing annotation is the subject of the test, so the type checker is told to
    # leave it alone rather than the test being rewritten into something that annotates it.
    def reads(request, *, entitlement: EntitlementSet) -> TypedResult[row_plane.RowRecord]:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    with pytest.raises(RowPlaneError, match="unannotated"):
        assert_takes_no_sql(reads)


def test_a_variadic_parameter_is_refused() -> None:
    """A signature that accepts anything has declared nothing, so it cannot be shown never to
    receive a fragment. Deleting this leaves a hole big enough for every other refusal to be
    routed around by spelling the argument `**kwargs`."""

    def reads(request: RowRequest, **rest: RowRequest) -> TypedResult[row_plane.RowRecord]:
        raise NotImplementedError

    with pytest.raises(RowPlaneError, match=r"\*args or \*\*kwargs"):
        assert_takes_no_sql(reads)


def test_the_row_plane_builds_no_statement_out_of_a_formatted_string() -> None:
    """The other half of M15.1.3, over the source rather than over the signature: with no
    argument able to hold text, there must also be nothing that composes text into a
    statement. This is the positive case, run against the real module, so the check is known
    to pass on code that is meant to pass."""
    assert_no_sql_is_built_by_interpolation(row_plane)


def _module_from(tmp_path: Any, name: str, source: str) -> Any:
    """Import a throwaway module from a real file, because the check parses real source."""
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8", newline="\n")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_f_string_handed_to_a_statement_is_refused(tmp_path: Any) -> None:
    """The shape injection actually arrives in. Checked over the parsed syntax tree rather
    than over the source text, because a text search for the word SQL is satisfied by the
    docstring explaining the rule - two tests in this repository have already been passed by
    their own explanations. Deleting this leaves the rule as a sentence in a module docstring,
    which is the same class of control as a line in a prompt."""
    module = _module_from(
        tmp_path,
        "interpolates_directly",
        'def build(value):\n    return text(f"select 1 where x = {value}")\n',
    )
    with pytest.raises(RowPlaneError, match="formatted string"):
        assert_no_sql_is_built_by_interpolation(module)


def test_a_composed_string_reaching_a_statement_through_a_name_is_refused(tmp_path: Any) -> None:
    """The same mistake one line apart, which is how it is actually written. A check that only
    looked at the call site would pass every real occurrence of this bug, because nobody
    writes the concatenation inside the parentheses."""
    module = _module_from(
        tmp_path,
        "interpolates_via_a_name",
        "def build(value):\n"
        '    fragment = "select 1 where x = " + value\n'
        "    return text(fragment)\n",
    )
    with pytest.raises(RowPlaneError, match="fragment"):
        assert_no_sql_is_built_by_interpolation(module)


def test_a_literal_statement_is_not_refused(tmp_path: Any) -> None:
    """The positive case. A rule that refused every `text()` call would refuse the scope
    predicate itself, and the fix somebody reaches for is deleting the rule."""
    module = _module_from(tmp_path, "literal_only", 'def build():\n    return text("select 1")\n')
    assert_no_sql_is_built_by_interpolation(module)


# --------------------------------------------- the tool and its records


def test_a_row_tool_passes_every_registration_rule_the_tool_registry_applies() -> None:
    """A typed tool per entity is only a tool if a registry accepts it (M15.1.1). This is the
    one test that runs the whole door: the name grammar, the object name, the typed return
    the redactor can walk, and the SERVICE rule that refuses a shared-credential tool with
    nothing to narrow it. Deleting it lets the row plane drift into a shape no registry would
    take, and the discovery happens at startup in production."""
    registry = ToolRegistry()
    definition = TICKET_TOOL.definition()
    registered = registry.register(definition, TICKET_TOOL.reader(Rows()), scope=TICKET_TOOL.scope)
    assert registered.name == "freshdesk.read_ticket"
    assert definition.identity_mode is IdentityMode.SERVICE
    assert definition.required_capability == "read:ticket"
    assert registry.names() == ("freshdesk.read_ticket",)


def test_a_record_is_built_from_the_projection_rather_than_from_the_row() -> None:
    """A source handing back a key the projection did not ask for must not widen the answer.
    Deleting this permits a record assembled by copying the row wholesale, which turns the
    projection into a request rather than a boundary and moves the guarantee back to "every
    code path remembered to filter"."""
    source = Rows({ENTITY_KEY: "ticket", ID_KEY: "t_1", "subject": "Form broken", "status": "open"})
    answer = read_rows(
        TICKET_TOOL,
        RowRequest(),
        entitlement=ents(*SEES_SUBJECT),
        records=source,
    )
    assert source.asked == 1
    dumped = answer.records[0].model_dump()
    assert dumped["subject"] == "Form broken"
    assert "status" not in dumped


def test_a_statement_that_cannot_return_a_row_is_never_sent() -> None:
    """`is_unsatisfiable` says its own purpose is deciding whether to bother asking. Deleting
    this spends a round trip to be told what the compiler already knew, on every request from
    a caller who holds nothing, which is exactly the caller a probe is run as."""
    answer = read_rows(
        TICKET_TOOL,
        RowRequest(),
        entitlement=ents("read:ticket.subject"),
        records=Refuses(),
    )
    assert answer.records == ()


def test_a_classification_naming_a_reserved_column_is_refused() -> None:
    """`entity` and `id` are the record's tag, not fields. A column by either name would
    overwrite the tag, and the redactor drops an untagged object whole, so the failure would
    be a silently empty answer rather than an error. Refused where the classification is
    written, which is the only place anybody is looking at the column names."""
    assert {ENTITY_KEY, ID_KEY} <= RESERVED_KEYS
    clashing = TableClassification(
        entity="ticket",
        rules=(
            ColumnRule(
                column=ID_KEY,
                required_capability=Capability(value="read:ticket.id"),
                classification=Classification.INTERNAL,
            ),
        ),
    )
    with pytest.raises(RowPlaneError, match="tag"):
        RowTool(source="freshdesk", classification=clashing, description="Read a ticket.")


def test_a_row_tool_needs_the_source_it_reads() -> None:
    """Without a source the pin narrows to an entity across every connected system, and two
    sources' record ids collide by coincidence of integers. Deleting this makes an empty
    source string compile to a predicate matching nothing, which fails closed and is therefore
    discovered as "the tool returns no rows" rather than as the missing pin it is."""
    with pytest.raises(RowPlaneError, match="source"):
        RowTool(source="", classification=TICKETS, description="Read a ticket.")
