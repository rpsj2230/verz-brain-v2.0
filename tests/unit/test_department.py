"""Departments, the scope registry, and the predicate boundary either side of it.

The rules that must never break live in `tests/invariants/test_scope_invariants.py`. What
is here is the mechanical half: parsing, rendering, satisfiability, the wizard, and the
messages. `brain.core.scope_sql` is tested here too rather than in a file of its own,
because the two modules are one change and splitting the tests would mean reading both to
find out where a behaviour is pinned.

Task ids: M2.1.1, M2.1.2, M2.1.3, M2.1.4, M2.1.5, M2.2.1, M2.2.2, M2.2.3, M2.2.4
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain.core.department import (
    Department,
    DepartmentAdmin,
    DepartmentError,
    ScopeRecord,
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
    CompiledPredicate,
    PredicateRefusedError,
    assert_conjunctive,
    check_grammar,
    clause_entails,
    compile_where,
    is_unsatisfiable,
    parse_predicate,
    scope_narrows,
    to_predicate,
)


def prefix(field_name: str, value: str) -> Scope:
    return Scope(clauses=(Clause(field=field_name, op=Op.PREFIX, value=value),))


# ------------------------------------------------------- the document form
def test_a_string_matcher_reads_as_an_equality() -> None:
    """The architecture writes a scope as `{"department":"web"}` and this is the line that
    keeps that literal form working. Without it the stored shape drifts from the document
    and every example anyone has read becomes wrong."""
    assert parse_predicate({"department": "web"}) == department_scope("web")


def test_every_matcher_form_survives_a_round_trip() -> None:
    """The console renders a saved scope by parsing it and rendering it back. A lossy round
    trip shows an author something other than what they saved, which is how a scope gets
    edited into a wider one by someone trying to leave it alone."""
    document = {
        "department": "web",
        "tier": ["managed", "retainer"],
        "scope_path": {"prefix": "web."},
        "region": {"any": True},
    }
    assert to_predicate(parse_predicate(document)) == document


def test_a_list_matcher_reads_as_a_membership_test() -> None:
    """A list is the only disjunction in the grammar and it is bounded to one field.
    Losing it would force multi-value scopes to be written as several grants, which is
    exactly the shape that composes to nothing."""
    scope = parse_predicate({"department": ["sales", "web"]})
    assert scope.matches({"department": "sales"})
    assert not scope.matches({"department": "finance"})


@pytest.mark.parametrize("key", ["or", "$or", "not", "any_of", "either", "unless"])
def test_a_predicate_naming_disjunction_is_refused_by_name(key: str) -> None:
    """An author who writes `$or` has a model of the grammar in their head. Falling
    through to "unknown field" would let them keep it, and the clause would silently
    become a field test on a field called `or`."""
    with pytest.raises(PredicateRefusedError, match="disjunction and negation"):
        parse_predicate({key: ["a", "b"]})


@pytest.mark.parametrize("value", [True, False, 4471, 1.5, None])
def test_a_non_string_scalar_is_refused_rather_than_coerced(value: object) -> None:
    """jsonb renders a boolean as `true` and Python renders it as `True`. Coercing here
    would produce a predicate that means one thing in the row evaluator and another in
    SQL, which is the divergence this whole module exists to prevent."""
    with pytest.raises(PredicateRefusedError, match="stored as strings"):
        parse_predicate({"active": value})


def test_a_predicate_that_is_not_an_object_is_refused_with_a_sentence() -> None:
    """The argument is whatever the jsonb column held. A list arrives the moment someone
    writes an UPDATE by hand, and an AttributeError three frames down says nothing about
    what to do next."""
    with pytest.raises(PredicateRefusedError, match="json object"):
        parse_predicate(["department", "web"])


def test_an_unknown_matcher_key_is_refused() -> None:
    """`{"gt": 5}` looks like it should work to anyone who has used a document database.
    Accepting it silently as an unknown shape would drop the clause and widen the scope."""
    with pytest.raises(PredicateRefusedError, match="unknown matcher key"):
        parse_predicate({"created": {"gt": "2026-01-01"}})


def test_a_matcher_object_carries_exactly_one_key() -> None:
    """Two keys is two clauses on one field, which the document form cannot represent and
    which would otherwise resolve by dictionary order."""
    with pytest.raises(PredicateRefusedError, match="exactly one key"):
        parse_predicate({"scope_path": {"prefix": "web.", "any": True}})


def test_a_field_name_that_could_not_be_a_column_is_refused() -> None:
    """The field reaches SQL as a jsonb key inside a quoted literal, so it is constrained
    rather than escaped."""
    with pytest.raises(PredicateRefusedError, match="not a usable field name"):
        parse_predicate({"Department Name": "web"})


def test_rendering_two_clauses_on_one_field_is_refused() -> None:
    """A composed scope frequently has two clauses on `department`. Writing it back to a
    document would keep one of them, and the one it keeps is the wider."""
    composed = compose((department_scope("web"), membership_scope(("sales", "web"))))
    with pytest.raises(PredicateRefusedError, match="cannot hold"):
        to_predicate(composed)


# ----------------------------------------------------------- the grammar
def test_a_membership_clause_holding_a_bare_string_cannot_be_built_at_all() -> None:
    """`list("abc")` is three members. The Python evaluator says no and the SQL renderer
    says yes to three values nobody wrote.

    This was originally caught here, one layer downstream. It is now refused by `Clause`
    itself, which is the better place: a shape the two evaluators read differently should
    not be constructible, rather than constructible and rejected by whoever remembers to
    ask. The grammar check below remains as defence in depth for scopes arriving as jsonb.
    """
    with pytest.raises(ValidationError, match="needs a tuple"):
        Clause(field="department", op=Op.IN, value="abc")


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        (Clause(field="d", op=Op.IN, value=()), "empty member list"),
        (Clause(field="d", op=Op.EQ, value=None), "needs a string value"),
        # IN-with-a-string and PREFIX-with-a-non-string are absent on purpose: `Clause`
        # now refuses both, so there is no such value to hand this validator.
        (Clause(field="d", op=Op.EQ, value=""), "empty value"),
        (Clause(field="d", op=Op.PREFIX, value=""), "non-empty string"),
        (Clause(field="d", op=Op.ANY, value="x"), "carries a value"),
    ],
)
def test_a_clause_whose_value_does_not_fit_its_operator_is_refused(
    clause: Clause, expected: str
) -> None:
    """`Clause` types `value` the same way for all four operators, so every mismatch
    constructs cleanly and then behaves differently on each evaluator."""
    with pytest.raises(PredicateRefusedError, match=expected):
        assert_conjunctive(Scope(clauses=(clause,)))


def test_a_well_formed_scope_passes_silently() -> None:
    """A validator that objects to correct input gets worked around rather than fixed."""
    assert_conjunctive(compose((department_scope("web"), prefix("scope_path", "web."))))


def test_every_violation_is_reported_at_once() -> None:
    """One at a time turns authoring a scope into a guessing game where each fix reveals
    the next objection."""
    scope = Scope(
        clauses=(
            Clause(field="a", op=Op.IN, value=()),
            Clause(field="b", op=Op.PREFIX, value=""),
        )
    )
    assert len(check_grammar(scope)) == 2


# --------------------------------------------------------- satisfiability
@pytest.mark.parametrize(
    "scope",
    [
        compose((department_scope("web"), department_scope("sales"))),
        compose((department_scope("web"), membership_scope(("sales", "finance")))),
        compose((membership_scope(("web", "sales")), membership_scope(("finance", "hr")))),
        compose((prefix("scope_path", "web."), prefix("scope_path", "sales."))),
        compose((department_scope("web"), prefix("department", "sal"))),
        Scope(clauses=(Clause(field="d", op=Op.IN, value=()),)),
    ],
)
def test_a_scope_that_cannot_match_anything_is_recognised(scope: Scope) -> None:
    """An impossible scope is indistinguishable from an empty table at the far end of a
    query, and both look like a permission bug. Recognising it is what lets a caller say
    which one it was."""
    assert is_unsatisfiable(scope)


@pytest.mark.parametrize(
    "scope",
    [
        Scope.unrestricted(),
        department_scope("web"),
        compose((department_scope("web"), prefix("scope_path", "web."))),
        compose((membership_scope(("web", "sales")), department_scope("web"))),
        compose((prefix("scope_path", "web."), prefix("scope_path", "web.pro"))),
    ],
)
def test_a_scope_that_can_match_is_not_called_impossible(scope: Scope) -> None:
    """False positives here refuse a legitimate grant, and the person refused goes and
    writes a wider one that does save."""
    assert not is_unsatisfiable(scope)


# --------------------------------------------------------------- narrowing
@pytest.mark.parametrize(
    ("narrow", "wide", "expected"),
    [
        (Clause(field="d", op=Op.EQ, value="web"), Clause(field="d", op=Op.EQ, value="web"), True),
        (Clause(field="d", op=Op.EQ, value="web"), Clause(field="d", op=Op.ANY), True),
        (
            Clause(field="d", op=Op.EQ, value="web"),
            Clause(field="d", op=Op.IN, value=("web", "sales")),
            True,
        ),
        (
            Clause(field="d", op=Op.IN, value=("web", "sales")),
            Clause(field="d", op=Op.EQ, value="web"),
            False,
        ),
        (
            Clause(field="p", op=Op.PREFIX, value="web.pro"),
            Clause(field="p", op=Op.PREFIX, value="web."),
            True,
        ),
        (
            Clause(field="p", op=Op.PREFIX, value="web."),
            Clause(field="p", op=Op.EQ, value="web.projects"),
            False,
        ),
        (Clause(field="a", op=Op.EQ, value="x"), Clause(field="b", op=Op.EQ, value="x"), False),
    ],
)
def test_entailment_answers_only_when_it_is_certain(
    narrow: Clause, wide: Clause, expected: bool
) -> None:
    """Every check that an admin stays inside their department is built on this. A wrong
    True is a permission bug; a wrong False costs a caller a shortcut."""
    assert clause_entails(narrow, wide) is expected


def test_a_scope_narrows_the_unrestricted_scope() -> None:
    """Vacuously true, and worth pinning: the unrestricted scope has no clauses, so a
    loop written the other way round would answer False for every input."""
    assert scope_narrows(department_scope("web"), Scope.unrestricted())
    assert not scope_narrows(Scope.unrestricted(), department_scope("web"))


# ------------------------------------------------------------ compilation
def test_an_unrestricted_scope_compiles_to_true_with_no_parameters() -> None:
    """A caller pastes this into a WHERE clause. An empty string would produce a syntax
    error at best and a dangling AND at worst."""
    assert compile_where(Scope.unrestricted()) == CompiledPredicate(where="TRUE", params={})


def test_a_promoted_field_compiles_to_a_real_column() -> None:
    """A scope that cannot use an index becomes a scan, and a scan under a narrow scope
    returns thin results that nobody files a bug about."""
    layout = ColumnLayout(promoted=frozenset({"department"}), alias="t")
    compiled = compile_where(department_scope("web"), layout)
    assert compiled.where == "(t.department = :s0)"
    assert compiled.params == {"s0": "web"}


def test_an_unpromoted_field_compiles_to_a_jsonb_lookup() -> None:
    """The default layout has to match `Clause.matches`, which reads a flat dict, so the
    key is looked up literally rather than as a path."""
    compiled = compile_where(Scope(clauses=(Clause(field="a.b", op=Op.EQ, value="x"),)))
    assert compiled.where == "(row_data ->> 'a.b' = :s0)"


def test_a_membership_clause_compiles_to_a_parameterised_array() -> None:
    """Rendering the members into the fragment would be the one place a scope value
    becomes SQL."""
    compiled = compile_where(membership_scope(("sales", "web")))
    assert "= ANY(:s0)" in compiled.where
    assert compiled.params == {"s0": ["sales", "web"]}


def test_a_prefix_clause_declares_its_escape_character() -> None:
    """Postgres defaults to backslash, and the default is a server setting. A permission
    predicate must not depend on one."""
    compiled = compile_where(prefix("scope_path", "web."))
    assert compiled.where == "(row_data ->> 'scope_path' LIKE :s0 ESCAPE '\\')"
    assert compiled.params == {"s0": "web.%"}


def test_two_fragments_merge_only_when_their_parameters_are_distinct() -> None:
    """Silently merging would bind one scope's value into the other's placeholder, which
    is a permission bug that reads as a typo."""
    left = compile_where(department_scope("web"), param_prefix="a")
    right = compile_where(prefix("scope_path", "web."), param_prefix="b")
    merged = left.and_(right)
    assert merged.params == {"a0": "web", "b0": "web.%"}
    assert " AND " in merged.where
    with pytest.raises(PredicateRefusedError, match="both fragments"):
        left.and_(compile_where(department_scope("sales"), param_prefix="a"))


def test_an_any_clause_compiles_to_true_rather_than_disappearing() -> None:
    """A clause that vanishes leaves a conjunction one term shorter than the scope it came
    from, and if it was the last term, an empty conjunction is the unrestricted scope."""
    mixed = Scope(
        clauses=(Clause(field="region", op=Op.ANY), Clause(field="d", op=Op.EQ, value="web"))
    )
    compiled = compile_where(mixed)
    assert compiled.where.count("TRUE") == 1
    assert " AND " in compiled.where
    # a scope of nothing but ANY is the unrestricted scope, and says so
    assert compile_where(Scope(clauses=(Clause(field="region", op=Op.ANY),))).where == "TRUE"


# --------------------------------------------------------- the scope table
def test_a_scope_record_validates_its_predicate_on_construction() -> None:
    """Validation in a helper is validation a loader can be written around. The record
    itself refuses, so there is no path into the registry that skips it.

    The grammar raises its own error type through pydantic rather than being flattened
    into a validation message, because it carries every violation and the author needs all
    of them."""
    with pytest.raises(PredicateRefusedError, match="empty member list"):
        ScopeRecord(slug="broken", scope=Scope(clauses=(Clause(field="d", op=Op.IN, value=()),)))


def test_a_scope_that_can_never_match_is_refused_at_authoring_time() -> None:
    """Saved dead configuration is indistinguishable from a permission bug once it is in
    the table, and the person who hits it has no way to tell which they are looking at."""
    with pytest.raises(ValidationError, match="cannot match any row"):
        ScopeRecord(slug="nowhere", scope=compose((department_scope("a"), department_scope("b"))))


def test_a_department_scope_must_actually_restrict_something() -> None:
    """A department flag on the unrestricted scope makes every department the company."""
    with pytest.raises(ValidationError, match="restricts nothing"):
        ScopeRecord(slug="everything", scope=Scope.unrestricted(), is_department=True)


def test_a_scope_record_round_trips_through_the_stored_form() -> None:
    """The registry holds validated scopes and the document form exists only at the
    boundary. If the two disagreed, a reload would change what a scope means."""
    record = ScopeRecord.from_predicate("web_managed", {"department": "web", "tier": "managed"})
    assert record.predicate() == {"department": "web", "tier": "managed"}


@pytest.mark.parametrize("slug", ["Web", "web ops", "web.ops", "w", "_web", "web-ops"])
def test_a_slug_that_could_be_read_two_ways_is_refused(slug: str) -> None:
    """Slugs share a namespace with agent names and tool objects. Dots are excluded
    because a tool name is `source.verb_noun`, and a slug with a dot in it can be typed
    where a tool is expected."""
    with pytest.raises(ValidationError):
        ScopeRecord(slug=slug, scope=department_scope("web"))


# ------------------------------------------------------ the department table
def test_a_department_names_the_scope_that_defines_it() -> None:
    """A department with no predicate is a label, and a label cannot decide who sees
    what."""
    draft = create_department("verz", "web", "Web")
    assert draft.department.scope_slug == draft.defining_scope.slug
    assert draft.defining_scope.is_department


def test_a_department_has_no_parent_field() -> None:
    """Nesting departments makes an entitlement question recursive. What people mean by a
    sub-department is a team, and a hierarchy that is wanted anyway is a prefix scope."""
    assert "parent" not in Department.model_fields
    assert "parent_slug" not in Department.model_fields


def test_the_wizard_produces_a_draft_rather_than_writing_as_it_goes() -> None:
    """A wizard that saves each step leaves half a department behind when step three
    fails, and half a department is one with a name and no predicate."""
    draft = create_department("verz", "web", "Web")
    assert [r.slug for r in draft.scopes] == ["web", "web_tree", "web_shared"]
    assert draft.department.company_id == "verz"


def test_a_starter_scope_carries_a_label_a_person_can_read() -> None:
    """These are generated unattended. Without a label the console shows three slugs and
    the first thing anyone does is delete the two they do not recognise."""
    for record in starter_scopes("web"):
        assert record.label
        assert "web" in record.label


def test_the_hierarchical_starter_scope_keeps_its_department_clause() -> None:
    """A prefix on `scope_path` alone would admit a row in another department that happens
    to carry a matching path, which is a cross-department grant created by a wizard."""
    tree = starter_scopes("web")[1]
    assert tree.scope.matches({"department": "web", "scope_path": "web.projects"})
    assert not tree.scope.matches({"department": "sales", "scope_path": "web.projects"})


def test_an_admin_appointment_needs_the_departments_own_scope_record() -> None:
    """The appointment cannot be written without producing the predicate that bounds it.
    Looking the scope up inside would let a caller appoint against a department whose
    scope row is missing."""
    draft = create_department("verz", "web", "Web")
    other = create_department("verz", "sales", "Sales")
    with pytest.raises(DepartmentError, match="is not the scope of"):
        assign_department_admin(draft.department, other.defining_scope, "u_siti")


def test_an_unflagged_scope_cannot_bound_an_admin() -> None:
    """`web_shared` is a scope inside Web, not the scope that is Web. Appointing against
    it would produce an admin who administers a subset and reads as the department's."""
    draft = create_department("verz", "web", "Web")
    shared = draft.scopes[2].model_copy(update={"slug": draft.department.scope_slug})
    with pytest.raises(DepartmentError, match="not flagged"):
        assign_department_admin(draft.department, shared, "u_siti")


def test_an_admin_record_refuses_an_unrestricted_scope_however_it_is_built() -> None:
    """The check is on the record, not only on the constructor helper, because a loader
    reading the admin table builds the record directly."""
    with pytest.raises(ValidationError, match="unrestricted"):
        DepartmentAdmin(principal_id="u", department_slug="web", scope=Scope.unrestricted())


# --------------------------------------------------------- multi-department
def test_a_membership_scope_is_stable_whatever_order_it_is_written_in() -> None:
    """Two spellings of one reach hash differently, so the same person would miss their
    own cache entry and appear in traces as two principals."""
    assert membership_scope(("web", "sales")) == membership_scope(("sales", "web", "sales"))


def test_a_membership_scope_over_nothing_is_refused() -> None:
    """An empty membership test matches nothing, which is safe, and it arrives from a
    failed lookup, which is not. Refusing says which happened."""
    with pytest.raises(DepartmentError, match="match nothing"):
        membership_scope(())


def test_a_scope_that_never_mentions_a_department_reaches_all_of_them() -> None:
    """Reachability is a satisfiability question, not a search for a department clause.
    A scope of `partner_visible = true` genuinely does reach every department."""
    partner = Scope(clauses=(Clause(field="partner_visible", op=Op.EQ, value="true"),))
    assert admits_department(partner, "web")
    assert admits_department(Scope.unrestricted(), "finance")


# ------------------------------------------------------- cross-department
def test_a_question_touching_two_reachable_departments_becomes_two_filters() -> None:
    """One plan with two filters is what the architecture describes for an asker who holds
    both scopes. A single merged filter would work here and stop working the moment the
    two departments live in different tables."""
    plan = plan_cross_department(membership_scope(("sales", "web")), ["sales", "web"])
    assert plan.reachable == ("sales", "web")
    assert len(plan.filters) == 2
    assert plan.gaps == ()
    assert plan.answerable


def test_a_partly_reachable_question_answers_and_states_the_gap() -> None:
    """Refusing the whole question because one department is out of reach teaches people
    to ask narrower questions rather than to request access."""
    plan = plan_cross_department(membership_scope(("sales", "web")), ["web", "finance"])
    assert plan.reachable == ("web",)
    assert [g.department for g in plan.gaps] == ["finance"]
    assert plan.answerable


def test_every_gap_offers_a_route_to_ask_for_access() -> None:
    """A stated gap with no route is a dead end, and the workaround people find is to ask
    a colleague to run the query for them."""
    plan = plan_cross_department(department_scope("web"), ["finance"])
    gap = plan.gaps[0]
    assert gap.route == "request-access/department/finance"
    assert "finance" in gap.request_access


def test_a_repeated_department_in_a_question_is_planned_once() -> None:
    """ "Compare web and web" is a real thing people type. Two identical filters would run
    the query twice and two identical gaps would print the same sentence twice."""
    plan = plan_cross_department(department_scope("web"), ["web", "web"])
    assert plan.reachable == ("web",)


def test_a_question_naming_no_department_is_answerable_by_nobody() -> None:
    """An empty department list means the question did not name one, and the plan must not
    invent a filter for it. The caller runs its own unfiltered path or refuses."""
    plan = plan_cross_department(Scope.unrestricted(), [])
    assert plan.combined is None
    assert not plan.answerable


# ------------------------------------------------------------- collisions
def test_a_registry_with_no_repeated_name_reports_nothing() -> None:
    """A check that fires on correct input gets switched off in CI within a week."""
    assert check_slug_collisions(["web", "sales"], ["triage_bot"], ["client", "ticket"]) == []


def test_two_scopes_with_the_same_slug_are_a_collision() -> None:
    """The scope table is the registry this check exists to protect, and a duplicate slug
    inside it is the case a cross-registry check would miss."""
    findings = check_slug_collisions(["web", "web"])
    assert len(findings) == 1
    assert "scope registry" in findings[0].detail


def test_a_collision_says_which_two_registries_claimed_the_name() -> None:
    """A finding that names only the slug leaves the reader to grep three tables."""
    findings = check_slug_collisions(["finance"], ["finance"])
    assert "agent" in str(findings[0])
    assert "scope" in str(findings[0])
