"""Rules the scope engine must never break. A failure here blocks deploy.

Three families of rule live in this file and they fail in different ways.

Composition rules fail loudly: a widened scope shows up as a person seeing another
department. Compilation rules fail quietly: the query is well formed, it just returns more
rows than it should, and nothing in a log says so. Disclosure rules fail invisibly: the
answer is correct and the refusal beside it tells the asker something about what they
cannot see.

Task ids: M2.1.2, M2.1.3, M2.1.4, M2.1.5, M2.2.2, M2.2.3, M2.2.4
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import pytest
from pydantic import ValidationError

from brain.core.department import (
    GAP_TEMPLATE,
    REQUEST_ACCESS_TEMPLATE,
    Gap,
    admits_department,
    assign_department_admin,
    check_slug_collisions,
    compose,
    create_department,
    department_scope,
    membership_scope,
    plan_cross_department,
    starter_scopes,
)
from brain.core.scope import Clause, Op, Scope
from brain.core.scope_sql import (
    ColumnLayout,
    PredicateRefusedError,
    compile_where,
    is_unsatisfiable,
    scope_narrows,
)

pytestmark = pytest.mark.invariant

INJECTION = "'; DROP TABLE grants--"

#: Scopes used wherever a test needs a spread rather than one case. Deliberately mixed:
#: one unrestricted, one single clause, one two-clause, one membership, one prefix.
SPREAD: tuple[Scope, ...] = (
    Scope.unrestricted(),
    department_scope("maintenance"),
    Scope(
        clauses=(
            Clause(field="department", op=Op.EQ, value="sales"),
            Clause(field="partner_visible", op=Op.EQ, value="true"),
        )
    ),
    membership_scope(("sales", "web")),
    Scope(clauses=(Clause(field="scope_path", op=Op.PREFIX, value="web."),)),
)

ROWS: tuple[dict[str, str], ...] = (
    {"department": "maintenance", "scope_path": "maintenance.triage"},
    {"department": "sales", "partner_visible": "true", "scope_path": "sales.pipeline"},
    {"department": "sales", "partner_visible": "false", "scope_path": "sales.pipeline"},
    {"department": "web", "scope_path": "web.projects"},
    {"department": "web", "scope_path": "webhooks"},
    {"department": "finance", "scope_path": "finance.ledger"},
    {},
)


def like_to_regex(pattern: str, escape: str = "\\") -> re.Pattern[str]:
    """SQL LIKE semantics, in Python, so the compiler can be checked without a database.

    Only the three constructs LIKE has: `%` is any run, `_` is any single character, and
    the escape character removes the special meaning of the next one.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out), re.DOTALL)


# --------------------------------------------------------- INV: composition
def test_a_scope_can_never_widen_through_composition() -> None:
    """The rule the entire permission model rests on. Delete this and a bug that makes
    composition union rather than intersect ships silently, because every scope still
    works on its own and only the combination is wrong."""
    for one in SPREAD:
        for other in SPREAD:
            composed = compose((one, other))
            assert scope_narrows(composed, one)
            assert scope_narrows(composed, other)
            for row in ROWS:
                if composed.matches(row):
                    assert one.matches(row)
                    assert other.matches(row)


def test_a_composed_scope_admits_exactly_the_rows_both_inputs_admit() -> None:
    """Narrowing is only half of it: composition must not lose rows either, or a person
    holding two overlapping grants silently sees less than either one alone and files a
    bug that gets closed as permissions working correctly."""
    for one in SPREAD:
        for other in SPREAD:
            composed = compose((one, other))
            for row in ROWS:
                assert composed.matches(row) == (one.matches(row) and other.matches(row))


def test_composing_one_scope_repeatedly_changes_nothing() -> None:
    """Idempotence. Without it, resolving the same grant twice produces a different
    serialisation, a different ent_hash, and a caller who misses their own cache entry."""
    scope = membership_scope(("sales", "web"))
    assert compose((scope, scope, scope)) == scope


def test_composing_no_scopes_refuses_instead_of_returning_everything() -> None:
    """The identity element of conjunction is the unrestricted scope, and returning it
    here would hand the whole company to a principal whose scope query came back empty.
    Delete this and the failure mode is a silent, total permission bypass."""
    with pytest.raises(Exception, match="empty list is not the unrestricted scope"):
        compose(())


def test_two_single_department_grants_never_compose_into_reach_over_both() -> None:
    """The disjunction that would break everything, tried the obvious way. Holding
    maintenance and holding web must not add up to holding either one; it adds up to
    holding nothing, which is the conservative answer."""
    composed = compose((department_scope("maintenance"), department_scope("web")))
    assert is_unsatisfiable(composed)
    assert not admits_department(composed, "maintenance")
    assert not admits_department(composed, "web")
    assert composed != membership_scope(("maintenance", "web"))


def test_a_multi_department_scope_is_written_once_rather_than_composed() -> None:
    """A person in two departments is one grant with a membership test, authored by
    someone who could have written the wide grant anyway. If this ever became reachable
    from `compose`, two narrow grants would start adding up."""
    both = membership_scope(("sales", "web"))
    assert admits_department(both, "sales")
    assert admits_department(both, "web")
    assert not admits_department(both, "finance")
    # one department is an equality, not a one-member membership test: two spellings of
    # one meaning would hash as two different principals
    assert membership_scope(("web",)) == department_scope("web")


def test_narrowing_is_sound_against_the_row_evaluator() -> None:
    """`scope_narrows` is what proves an admin stays inside their department and a starter
    scope stays inside the department that generated it. If it ever answers True for a
    pair that is not actually a subset, every check built on it becomes decorative."""
    for narrow in SPREAD:
        for wide in SPREAD:
            if scope_narrows(narrow, wide):
                for row in ROWS:
                    if narrow.matches(row):
                        assert wide.matches(row), f"{narrow} claimed to narrow {wide}"


# ------------------------------------------------------------ INV: the gate
def test_a_department_admin_can_never_hold_an_unrestricted_scope() -> None:
    """Null and unrestricted are the same mistake in this model, and it is the mistake
    that hands one team's admin the whole company without anyone appointing them."""
    draft = create_department("verz", "web", "Web")
    with pytest.raises(ValueError, match="unrestricted"):
        assign_department_admin(
            draft.department,
            draft.defining_scope.model_copy(update={"scope": Scope.unrestricted()}),
            "u_siti",
        )


def test_a_department_admin_never_reaches_outside_their_department() -> None:
    """The console is one build with a scope filter rather than two products, so this
    predicate is the only thing separating a department admin's view from a super
    admin's."""
    draft = create_department("verz", "web", "Web")
    admin = assign_department_admin(draft.department, draft.defining_scope, "u_siti")
    assert scope_narrows(admin.scope, draft.defining_scope.scope)
    assert admits_department(admin.scope, "web")
    assert not admits_department(admin.scope, "finance")


def test_narrowing_an_admin_appointment_can_only_shrink_it() -> None:
    """`within` is intersected rather than substituted. Substitution would let a narrower
    appointment name a wider scope and quietly replace the department bound."""
    draft = create_department("verz", "web", "Web")
    admin = assign_department_admin(
        draft.department,
        draft.defining_scope,
        "u_siti",
        within=Scope(clauses=(Clause(field="scope_path", op=Op.PREFIX, value="web.projects"),)),
    )
    assert scope_narrows(admin.scope, draft.defining_scope.scope)
    assert admin.scope.matches({"department": "web", "scope_path": "web.projects.alpha"})
    assert not admin.scope.matches({"department": "web", "scope_path": "web.support"})


def test_an_appointment_narrowed_out_of_its_own_department_is_refused() -> None:
    """Bounding the Web admin to Finance composes to a scope that matches nothing, which
    is the right arithmetic and the wrong appointment. Accepting it would create an admin
    whose every screen is empty, and the first fix anyone reaches for is widening."""
    draft = create_department("verz", "web", "Web")
    with pytest.raises(ValueError, match="empty scope"):
        assign_department_admin(
            draft.department,
            draft.defining_scope,
            "u_siti",
            within=department_scope("finance"),
        )


def test_every_starter_scope_stays_inside_the_department_that_generated_it() -> None:
    """Starter scopes are created unattended by a wizard. One that reaches outside its own
    department would be a cross-department grant nobody reviewed, on a new department that
    nobody is watching yet."""
    for record in starter_scopes("web"):
        assert scope_narrows(record.scope, department_scope("web"))
        assert admits_department(record.scope, "web")
        assert not admits_department(record.scope, "finance")


def test_exactly_one_starter_scope_carries_the_department_flag() -> None:
    """The flag is what an admin appointment and a department row are checked against. Two
    flagged scopes would make "the department's scope" ambiguous, and zero would make an
    appointment impossible to bound."""
    flagged = [r for r in starter_scopes("web") if r.is_department]
    assert len(flagged) == 1
    assert flagged[0].slug == "web"


# --------------------------------------------------- INV: cross-department
def test_an_unreachable_department_reads_the_same_whether_or_not_it_exists() -> None:
    """A denial and an absence must be indistinguishable. If a real department produced
    different text from an invented one, an asker could enumerate the org chart by typing
    names at it and reading the refusals."""
    plan = plan_cross_department(department_scope("web"), ["finance", "nosuchdepartment"])
    real, invented = plan.gaps
    assert real.message.replace("finance", "X") == invented.message.replace("nosuchdepartment", "X")
    assert real.request_access.replace("finance", "X") == invented.request_access.replace(
        "nosuchdepartment", "X"
    )


def test_a_gap_never_carries_a_count_of_what_is_hidden() -> None:
    """ "Twelve records hidden" tells an asker the department is busy, which client work is
    live, and whether their guess about who holds an account was right. The type has
    nowhere to put a number and the wording has no room for one."""
    assert {f.name for f in dataclasses.fields(Gap)} == {"department"}
    for template in (GAP_TEMPLATE, REQUEST_ACCESS_TEMPLATE):
        assert not any(ch.isdigit() for ch in template)
        assert template.count("{department}") == 1
        assert "{" not in template.replace("{department}", "")

    plan = plan_cross_department(department_scope("web"), ["finance"])
    for gap in plan.gaps:
        assert not any(ch.isdigit() for ch in gap.message + gap.request_access)


def test_a_cross_department_plan_is_never_given_the_rows_it_is_hiding() -> None:
    """The strongest available form of "never emit a count": the function cannot see the
    hidden side at all. Delete this and someone adds a `hidden_rows` argument for a
    progress indicator, and the count is one f-string away."""
    parameters = inspect.signature(plan_cross_department).parameters
    assert list(parameters) == ["asker", "departments"]
    rendered = str(inspect.signature(plan_cross_department))
    for forbidden in ("row", "count", "total", "record", "Registry"):
        assert forbidden not in rendered


def test_a_gap_is_raised_only_for_a_department_the_question_named() -> None:
    """The gap list is derived from the question, never from the registry. A plan built
    over every department in the company would answer a question about Web with a list of
    the departments the asker cannot see, which is the leak this whole module avoids."""
    asked = ["web", "web", "finance"]
    plan = plan_cross_department(department_scope("web"), asked)
    assert [g.department for g in plan.gaps] == ["finance"]
    assert plan.reachable == ("web",)


def test_nothing_reachable_never_becomes_an_unrestricted_filter() -> None:
    """The classic empty-filter-list bug: no departments reachable, so no WHERE clause, so
    every row. The empty case has a value of its own type instead of a value that happens
    to mean everything."""
    plan = plan_cross_department(department_scope("web"), ["finance"])
    assert plan.combined is None
    assert not plan.answerable
    assert plan.filters == ()


def test_a_reachable_plan_is_never_wider_than_the_asker() -> None:
    """Cross-department work is one plan with two filters, and both filters have to sit
    inside what the asker already held. A filter built from the requested departments
    alone would widen a narrow asker up to whatever they asked about."""
    asker = membership_scope(("sales", "web"))
    plan = plan_cross_department(asker, ["sales", "web", "finance"])
    assert plan.combined is not None
    assert scope_narrows(plan.combined, asker)
    for one in plan.filters:
        assert scope_narrows(one.scope, asker)


# -------------------------------------------------------- INV: compilation
def test_a_predicate_that_would_widen_in_sql_is_refused_before_it_compiles() -> None:
    """`op=IN, value="abc"` is refused by the Python evaluator and admitted by SQL as
    three separate values. Delete this and one scope means two things, and the SQL half is
    the one that decides what a person actually receives."""
    with pytest.raises(ValidationError, match="needs a tuple"):
        Clause(field="department", op=Op.IN, value="abc")

    # And the same for a PREFIX that is not a string: `matches` admits nothing while
    # `to_sql` renders str(None) and produces LIKE 'None%', which matches real rows.
    with pytest.raises(ValidationError, match="needs a string"):
        Clause(field="department", op=Op.PREFIX, value=None)


def test_a_wildcard_in_a_prefix_value_cannot_widen_the_match() -> None:
    """SQL LIKE reads `%` and `_`; `str.startswith` does not. A stored prefix of `web_`
    narrows in Python and widens in SQL, so a scope written to reach one team reaches
    every team whose name is one character longer."""
    candidates = ("web_projects", "webXprojects", "web", "webhooks", "anything%")
    for value in ("web_", "web%", "web", "any\\thing"):
        scope = Scope(clauses=(Clause(field="scope_path", op=Op.PREFIX, value=value),))
        compiled = compile_where(scope)
        pattern = like_to_regex(str(compiled.params["s0"]))
        for candidate in candidates:
            assert bool(pattern.match(candidate)) == candidate.startswith(value), (
                f"{value!r} disagrees with SQL on {candidate!r}"
            )


def test_a_compiled_predicate_never_interpolates_a_value() -> None:
    """A client name, a department name and a path all reach this function as data. If any
    operator built its fragment by concatenation, a value would become SQL, and the values
    here come from a console form."""
    clauses = (
        Clause(field="department", op=Op.EQ, value=INJECTION),
        Clause(field="scope_path", op=Op.PREFIX, value=INJECTION),
        Clause(field="tier", op=Op.IN, value=(INJECTION, "managed")),
    )
    for clause in clauses:
        compiled = compile_where(Scope(clauses=(clause,)))
        assert "DROP TABLE" not in compiled.where
        assert any(INJECTION in str(v) for v in compiled.params.values())


def test_an_identifier_that_could_close_a_quote_is_refused() -> None:
    """Column names and table aliases cannot be parameterised, so they are constrained
    rather than quoted. Quoting only moves the problem to the quote character."""
    for bad in ("row_data'; --", 'row"data', "row data", "RowData", ""):
        with pytest.raises(PredicateRefusedError):
            ColumnLayout(jsonb_column=bad)
    with pytest.raises(PredicateRefusedError):
        compile_where(department_scope("web"), param_prefix="s'; --")


def test_an_impossible_scope_compiles_to_false_and_says_so() -> None:
    """An impossible scope is fail-closed and indistinguishable, at the far end of a
    query, from an empty table or a permission bug. The flag is the only way a caller can
    say "your scopes do not overlap" instead of "no results"."""
    impossible = compose((department_scope("web"), department_scope("finance")))
    compiled = compile_where(impossible)
    assert compiled.where == "FALSE"
    assert compiled.certainly_empty
    assert compiled.params == {}


def test_an_unrestricted_scope_and_an_impossible_one_never_compile_alike() -> None:
    """They are opposite answers with the same shape: no usable clauses. Collapsing them
    to the same fragment turns a person who can see nothing into a person who sees
    everything, or the reverse, depending on which way the collapse goes."""
    everything = compile_where(Scope.unrestricted())
    nothing = compile_where(compose((department_scope("web"), department_scope("finance"))))
    assert everything.where == "TRUE"
    assert not everything.certainly_empty
    assert nothing.where != everything.where


def test_no_compiled_fragment_treats_a_missing_field_as_a_match() -> None:
    """A partially projected row must not widen access by omission, which in SQL means
    never reaching for a NULL-tolerant comparison. `jsonb ->> 'missing'` is NULL and every
    comparison against it is NULL, so absence excludes the row; a COALESCE or an IS NULL
    added later to fix a bug would quietly reverse that."""
    for scope in SPREAD:
        compiled = compile_where(scope)
        assert "COALESCE" not in compiled.where.upper()
        assert "IS NULL" not in compiled.where.upper()
        assert "IS NOT NULL" not in compiled.where.upper()


# ----------------------------------------------------------- INV: namespace
def test_a_scope_slug_can_never_be_claimed_by_an_agent_or_a_tool() -> None:
    """One typed name, three registries. If an agent and a scope are both called finance,
    "grant Priya finance" has two readings and the safe one is not the one a resolver
    picks by declaration order."""
    assert check_slug_collisions(["web", "finance"], ["triage_bot"], ["client"]) == []
    assert check_slug_collisions(["finance"], ["finance"], []) != []
    assert check_slug_collisions(["client"], [], ["client"]) != []


def test_two_names_only_a_machine_can_tell_apart_are_a_collision() -> None:
    """`client-ops` and `client_ops` are one name to everyone reading the console and two
    rows to the database. The interface is where the ambiguity is resolved wrongly."""
    assert check_slug_collisions(["client_ops"], [], ["client-ops"]) != []
    assert check_slug_collisions(["client_ops"], ["Client_Ops"], []) != []
