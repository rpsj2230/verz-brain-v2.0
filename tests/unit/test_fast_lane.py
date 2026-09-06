"""The fast lane: what it will answer, what it cannot reach, and what a rule may say.

Every test here is about one of three failures, and they fail in three different ways.

**Answering the wrong question** fails silently and confidently. There is no model in this
lane, so nothing downstream reads the answer and nothing can notice it was for a question
nobody asked. So the matcher tests come in pairs throughout: a refusal test with no sibling
proving the right question still matches is satisfied by a matcher that matches nothing.

**Reaching past the gate** fails invisibly. A fast path that skipped the entitlement check
would look identical to one that did not, in every test that happens to use a wide caller,
and the tests below therefore assert on the compiled statement and on whether the database
was asked at all, rather than on what came back.

**A rule becoming code** fails later. A template is data today, and the edit that makes it a
pattern is one import and one call, in a module that will by then have several. The checks
for that are structural and are applied to the real module rather than described.

The migration and the model are two hand-written descriptions of one table, and nothing but a
test compares them, which is the arrangement every other migration in this repository has.

Task ids: M6.1.1, M6.1.2, M6.1.3, M6.1.4
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import Table, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.fast_path import (
    MAX_TEMPLATE_CHARS,
    MIN_LITERAL_CHARS,
    MIN_TEMPLATE_CHARS,
    check_template,
)
from brain.core.field_policy import Classification
from brain.core.scope import Clause, Op, Scope
from brain.db import metadata
from brain.gate import classify, fast_lane
from brain.gate.fast_lane import (
    DECLARED_FIELDS,
    FAST_LANE_ROW_LIMIT,
    FastLaneAnswer,
    FastLaneError,
    FastPathRule,
    RuleMatch,
    assert_reaches_no_tool_and_no_model,
    assert_rules_are_never_compiled,
    entities_served,
    match_rule,
    respond,
    rules_from_rows,
)
from brain.knowledge.columns import ColumnRule, TableClassification
from brain.knowledge.rows import RowQuery, RowTool, assert_no_sql_is_built_by_interpolation
from brain.ops.migration_policy import check_all, check_file
from brain.tables.fast_lane import FastPathRuleRow

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations" / "versions"
MIGRATION = VERSIONS / "0019_fast_path_rule.py"

#: A PostgreSQL dialect to render against. Taken from an engine rather than from
#: `postgresql.dialect()` because that constructor is untyped and mypy runs strict here.
#: Creating an engine performs no I/O; nothing below ever connects it.
DIALECT = create_engine("postgresql+psycopg://", poolclass=NullPool).dialect

CLIENTS = TableClassification(
    entity="client",
    rules=(
        ColumnRule(
            column="name",
            required_capability=Capability(value="read:client.name"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="hours_remaining",
            required_capability=Capability(value="read:client.hours_remaining"),
            classification=Classification.CONFIDENTIAL,
        ),
    ),
)

CLIENT_TOOL = RowTool(
    source="laravel",
    classification=CLIENTS,
    description="Read a client record.",
)

SEES_CLIENT_HOURS = ("read:client", "read:client.name", "read:client.hours_remaining")

#: The rule the whole file is written around. A real question shape: three words of literal
#: text, one hole, and two different projected fields for matching and answering.
HOURS = FastPathRule(
    rule_id="client_hours_remaining",
    template="hours left on {client}",
    slot="client",
    source="laravel",
    entity="client",
    match_field="name",
    answer_field="hours_remaining",
)

#: A second shape over the same entity, with the hole at the front. Used where a test needs
#: two rules that do not overlap, so that "two rules matched" means something specific.
EXPIRY = FastPathRule(
    rule_id="client_hosting_expiry",
    template="{client} hosting expiry date",
    slot="client",
    source="laravel",
    entity="client",
    match_field="name",
    answer_field="hosting_expires_at",
)

#: The row a fake source hands back. Keyed the way `read_rows` reads it: the redactor's two
#: reserved keys plus the projected columns.
ACME = {"entity": "client", "id": "c_447", "name": "Acme", "hours_remaining": "12"}
ZEPHYR = {"entity": "client", "id": "c_448", "name": "Acme", "hours_remaining": "40"}


def ents(*caps: str, scope: Scope | None = None, principal: str = "p_priya") -> EntitlementSet:
    """An entitlement set holding these capabilities in one scope."""
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=scope or Scope()) for c in caps),
    )


class RecordingSource:
    """A `RowSource` that answers with fixed rows and remembers every query it was handed.

    Recording rather than asserting inside, because the interesting question in several
    tests below is whether it was called at all: a caller who may not see the entity must
    reach a statement the compiler already knows is empty, and the evidence for that is an
    empty call log rather than an empty result.
    """

    def __init__(self, *returns: Mapping[str, Any]) -> None:
        self.returns = list(returns)
        self.queries: list[RowQuery] = []

    def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]:
        self.queries.append(query)
        return self.returns


def readers_for(source: RecordingSource) -> dict[tuple[str, str], fast_lane.RowReader]:
    """The one reader this lane is wired with, keyed the way `respond` looks it up."""
    return {("laravel", "client"): CLIENT_TOOL.reader(source)}


def parameterised(query: RowQuery) -> str:
    """The statement as it is actually sent: placeholders, not values."""
    return str(query.statement.compile(dialect=DIALECT))


def bound(query: RowQuery) -> dict[str, Any]:
    """The parameters the statement carries, without inlining anything."""
    return dict(query.statement.compile(dialect=DIALECT).params)


def a_module(tmp_path: Path, name: str, source: str) -> ModuleType:
    """Load a written-out module, so a structural check can be run against a real one.

    The checks under test read `inspect.getsource`, so a module has to exist on disk. A
    string handed straight to the checker would test a different function from the one the
    real module is put through.
    """
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8", newline="\n")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- a rule is a row (M6.1.1)


def test_a_template_with_one_hole_is_a_rule_and_a_template_with_two_is_not() -> None:
    """The whole of "a rule is data" rests on a template having a shape simple enough to be
    checked in two places, and the count is that shape. A second hole means two values to
    pull out of one question, which is a parser, and a parser in a rule row is the thing this
    design exists to avoid.

    Both directions, because a validator that refused every template would pass the refusal
    half on its own and disable the lane completely.

    Delete this and a rule can carry as many holes as somebody types, with the matcher
    reading only the first and reporting an answer for a question it half read."""
    assert HOURS.before == "hours left on " and HOURS.after == ""

    with pytest.raises(ValidationError, match="exactly one hole"):
        FastPathRule(
            rule_id="two_holes",
            template="hours left on {client} in {month}",
            slot="client",
            source="laravel",
            entity="client",
            match_field="name",
            answer_field="hours_remaining",
        )


def test_a_template_whose_braces_are_the_wrong_way_round_says_so() -> None:
    """`} client {` is refused whichever check gets to it: the slot-name comparison would
    also refuse it, because the text between the braces of a reversed template is empty and
    no rule declares an empty slot. So this pins the message rather than the refusal.

    That is worth a test rather than a comment. The two messages send an operator to
    different places, and being told that a template naming `{client}` does not declare
    `client` is the kind of diagnostic somebody spends twenty minutes on.

    Found by a mutation: removing the ordering check left every other test in this file
    green, because the slot-name check refused the same template with a different sentence.

    Delete this and the ordering check is dead code that reads as a guard."""
    with pytest.raises(ValidationError, match="closes its hole before it opens one"):
        FastPathRule(
            rule_id="reversed",
            template="} hours left on client {",
            slot="client",
            source="laravel",
            entity="client",
            match_field="name",
            answer_field="hours_remaining",
        )


def test_a_template_naming_a_slot_the_rule_does_not_declare_is_refused() -> None:
    """The template and the `slot` column are read by different code: the matcher splits on
    the braces, and the loader and the database check the name. A rule where they disagree
    matches on one thing and reports another, and the report is what gets cited.

    Delete this and `slot` becomes decoration that the database is still checking, so the
    constraint fails at insert time on a rule that passed every test."""
    with pytest.raises(ValidationError, match="declares slot"):
        FastPathRule(
            rule_id="mismatched",
            template="hours left on {customer}",
            slot="client",
            source="laravel",
            entity="client",
            match_field="name",
            answer_field="hours_remaining",
        )


def test_a_template_that_is_almost_all_hole_is_refused_and_a_real_question_is_not() -> None:
    """A template of `{client}` matches any one-word question and answers it from a projected
    field. The floor is what makes a template a question shape rather than a wildcard.

    Both templates are written out rather than generated from `MIN_LITERAL_CHARS`, so this
    pins the constant from both sides: raising it fails the acceptance below, lowering it
    fails the refusal, and a test that built its fixtures from the constant would be green
    for every value the constant could hold.

    Delete this and one row in a configuration table answers every question in the estate."""
    with pytest.raises(ValidationError):
        FastPathRule(
            rule_id="wildcard",
            template="{client} hours",
            slot="client",
            source="laravel",
            entity="client",
            match_field="name",
            answer_field="hours_remaining",
        )
    # Fourteen literal characters, and accepted. The pair is what fixes the floor.
    assert (
        FastPathRule(
            rule_id="narrow",
            template="hours left on {client}",
            slot="client",
            source="laravel",
            entity="client",
            match_field="name",
            answer_field="hours_remaining",
        ).slot
        == "client"
    )


def test_a_rule_may_not_carry_a_field_nobody_declared() -> None:
    """`extra="forbid"` is part of the rule rather than tidiness. A row carrying `handler`,
    `pattern` or `python` is refused rather than ignored, so a column somebody adds to smuggle
    behaviour in cannot sit unread until a later edit starts reading it.

    Delete this and a rule table quietly becomes an interface."""
    row = {name: getattr(HOURS, name) for name in DECLARED_FIELDS}

    with pytest.raises(ValidationError, match="handler"):
        FastPathRule.model_validate({**row, "handler": "brain.evil:run"})


def test_a_column_added_to_the_rule_table_does_not_reach_the_matcher() -> None:
    """The loader reads the fields it names and nothing else, which is what keeps a
    configuration table from becoming an interface: a column added to `gate.fast_path_rule`
    is inert until somebody adds it to `DECLARED_FIELDS` and to the type.

    Asserted on a row carrying a real extra column rather than on the tuple's contents,
    because comparing `DECLARED_FIELDS` against the model's columns would compare two lists
    somebody keeps in step by hand and would say nothing about what the loader does.

    Delete this and the loader can be changed to splat the row, at which point
    `extra="forbid"` turns every new column into a load failure instead."""
    row = {name: getattr(HOURS, name) for name in DECLARED_FIELDS}
    row["created_by"] = "p_ops"
    row["deleted_at"] = None

    loaded = rules_from_rows([row])

    assert loaded == (HOURS,)


def test_a_rule_set_naming_one_id_twice_is_refused() -> None:
    """A rule id names one rule: it is the primary key of the table and the only thing in the
    log line when two rules match. Two rows sharing one would make that line ambiguous
    exactly where an operator needs it.

    Delete this and a duplicate loads, and the message saying which rules collided names the
    same rule twice."""
    row = {name: getattr(HOURS, name) for name in DECLARED_FIELDS}

    with pytest.raises(FastLaneError, match="appears twice"):
        rules_from_rows([row, dict(row)])


def test_a_rule_row_missing_a_field_is_refused_and_the_message_names_the_field() -> None:
    """A rule set is refused whole rather than partly, because a skipped row is a rule an
    operator believes is live and silently is not, and the symptom is a question answered by
    a model instead: correctly, a little slower, and reported by nothing.

    Delete this and a rule table with a null column disables one rule in silence."""
    row = {name: getattr(HOURS, name) for name in DECLARED_FIELDS if name != "answer_field"}

    with pytest.raises(FastLaneError, match="answer_field"):
        rules_from_rows([row])


def test_a_rule_row_that_is_not_a_valid_rule_is_refused_without_quoting_it_back() -> None:
    """The rules are configuration rather than anybody's data, so quoting a bad row would
    leak nothing today. The habit is the point: a message that quotes what it read is a
    message that one day quotes a value, and this one is raised from a request path.

    Delete this and the loader's message becomes a place to put the row."""
    row = {name: getattr(HOURS, name) for name in DECLARED_FIELDS}
    row["template"] = "{client} x"

    with pytest.raises(FastLaneError) as raised:
        rules_from_rows([row])

    assert "{client} x" not in str(raised.value)
    assert "row 0" in str(raised.value)


# --------------------------------------------------------- the matcher (M6.1.2)


def test_a_question_that_is_the_template_matches_and_a_longer_one_does_not() -> None:
    """A template describes the whole question. The extra words in a longer question almost
    always carry a qualifier that changes the answer, and there is nothing in this lane able
    to notice one was ignored.

    Both directions in one test, because the matcher that refuses the longer question and
    also refuses the exact one is a matcher that has switched the lane off.

    Delete this and the fast lane answers the general question when the narrow one was
    asked, instantly and with a citation."""
    exact = match_rule("hours left on Acme", [HOURS], entities=frozenset({"client"}))
    assert exact is not None and exact.value == "Acme"

    assert match_rule("the hours left on Acme", [HOURS], entities=frozenset({"client"})) is None
    # A template with literal text after the hole is anchored at that end too, which is the
    # half `HOURS` cannot demonstrate: its hole runs to the end of the question.
    tail = match_rule("Acme hosting expiry date", [EXPIRY], entities=frozenset({"client"}))
    assert tail is not None and tail.value == "Acme"
    assert (
        match_rule("Acme hosting expiry date please", [EXPIRY], entities=frozenset({"client"}))
        is None
    )


def test_the_boundary_between_the_literal_and_the_slot_falls_on_a_space() -> None:
    """Anchoring the template at both ends is necessary and not sufficient. `hours left on`
    matches the front of `hours left onacme` perfectly well, and the slot then swallows the
    last letter of the literal: the lane would look up a client called `acme` for a question
    about somebody else, or nobody.

    Delete this and a typo becomes a lookup of a different name."""
    assert match_rule("hours left onAcme", [HOURS], entities=frozenset({"client"})) is None
    assert match_rule("hours left on Acme", [HOURS], entities=frozenset({"client"})) is not None


def test_a_template_ending_in_its_slot_gives_the_slot_the_rest_of_the_question() -> None:
    """**A limitation, asserted rather than left to be discovered.** A template whose hole
    runs to the end has nothing to anchor against on that side, so `hours left on Acme
    please` puts `Acme please` in the slot. What bounds it is the qualifier list and the word
    count, both of which `brain.gate.classify` already applies, and neither of which knows
    about politeness. The lane then looks up a client of that name and finds none, which is
    the same empty answer a typo produces.

    The alternative is a template ending in a literal, which is a rule an operator can write
    today. Stating the trade here rather than in a docstring means an edit that changes it
    fails a test that says what the old behaviour was.

    Delete this and somebody later reads the anchoring test above and believes the slot is
    bounded on both sides whatever the template says."""
    trailing = match_rule("hours left on Acme please", [HOURS], entities=frozenset({"client"}))

    assert trailing is not None
    assert trailing.value == "Acme please"


def test_a_slot_that_swallowed_a_qualifier_does_not_match() -> None:
    """`hours left on Acme after the November work` is not a question about a client called
    `Acme after the November work`. `brain.gate.classify` found this the hard way and the
    rule lives there; this asserts the fast lane calls that function rather than carrying a
    copy of it, because the copy is the one that drifts.

    Delete this and the qualifier rule holds in the lane that has a model and not in the lane
    that does not."""
    assert vars(fast_lane)["is_a_name_not_a_phrase"] is classify.is_a_name_not_a_phrase

    swallowed = match_rule(
        "hours left on Acme after the November work", [HOURS], entities=frozenset({"client"})
    )
    assert swallowed is None


def test_matching_ignores_case_and_spacing_while_the_slot_value_keeps_both() -> None:
    """People type questions, so the shape has to survive a capital and a double space. The
    value must not be folded with it: it goes into a comparison against a projected field,
    and a name lowercased here matches nothing at all.

    Delete this and the lane answers nothing for anybody who capitalises a sentence, or
    matches everything and looks up a name no record holds."""
    found = match_rule("Hours  Left   On   Acme Pte Ltd?", [HOURS], entities=frozenset({"client"}))

    assert found is not None
    assert found.value == "Acme Pte Ltd"


def test_a_slot_value_is_bounded_at_both_ends() -> None:
    """The slot is the one part of a question that reaches a comparison, so it is the one
    part with a length. One character is not a client name, it is what is left when somebody
    typed the question wrong; sixty-one characters is a sentence that happens to contain no
    qualifier, and matching it would put that sentence into a filter.

    The three lengths are written out rather than built from the constants, so the pair pins
    both bounds: move either and one of the three assertions below stops holding. Found by a
    mutation that removed the bound entirely and left every other test in this file green.

    Delete this and a question with a one-letter tail is looked up as a client."""
    entities = frozenset({"client"})

    assert match_rule("hours left on A", [HOURS], entities=entities) is None
    assert match_rule("hours left on " + "A" * 61, [HOURS], entities=entities) is None
    assert match_rule("hours left on " + "A" * 60, [HOURS], entities=entities) is not None


def test_a_rule_set_larger_than_the_cap_is_refused() -> None:
    """The matcher walks every rule on every question, so the rule count is a cost paid on
    every request in the lane whose whole purpose is being quick. Past a couple of hundred
    entries a rule table is a corpus somebody should be searching rather than a vocabulary
    somebody wrote.

    Two hundred and two hundred and one, written out, so the cap is pinned rather than
    restated. Found by a mutation that raised the cap by a factor of a thousand with nothing
    failing.

    Delete this and the cap is a comment."""
    rows = [
        {**{name: getattr(HOURS, name) for name in DECLARED_FIELDS}, "rule_id": f"rule_{n}"}
        for n in range(201)
    ]

    assert len(rules_from_rows(rows[:200])) == 200
    with pytest.raises(FastLaneError, match="matches on every question"):
        rules_from_rows(rows)


def test_two_rules_matching_one_question_answer_neither() -> None:
    """Two rules matching means the question is not the exact question either was written
    for, and picking the first is picking by insertion order. The answer lane reads it
    instead, which costs one model call and makes an operator error visible.

    The overlapping pair is built here rather than taken from the fixtures, so the test says
    what an overlap is: two templates whose literal parts differ only in what they leave to
    the hole.

    Delete this and whichever rule was inserted first silently owns the question."""
    other = FastPathRule(
        rule_id="client_hours_alias",
        template="hours left on {customer}",
        slot="customer",
        source="laravel",
        entity="client",
        match_field="trading_name",
        answer_field="hours_remaining",
    )

    assert match_rule("hours left on Acme", [HOURS, other], entities=frozenset({"client"})) is None
    # And each on its own still answers, so this is a refusal of the pair rather than of both.
    assert match_rule("hours left on Acme", [HOURS], entities=frozenset({"client"})) is not None
    assert match_rule("hours left on Acme", [other], entities=frozenset({"client"})) is not None


def test_a_rule_for_an_entity_the_row_plane_does_not_serve_is_never_considered() -> None:
    """The entity filter is about wiring and not about a person: it is the set of entities a
    reader exists for, derived from the readers themselves so the two cannot disagree. A rule
    for a source nobody has wired matches nothing, which is the same outcome as no rule.

    Delete this and `respond` raises for a rule naming an entity it cannot fetch, in a
    request path, on a question somebody asked."""
    source = RecordingSource(ACME)
    assert entities_served(readers_for(source)) == frozenset({"client"})

    ticketing = FastPathRule(
        rule_id="ticket_status",
        template="status of ticket {ticket}",
        slot="ticket",
        source="freshdesk",
        entity="ticket",
        match_field="number",
        answer_field="status",
    )

    assert match_rule("status of ticket 4471", [ticketing], entities=frozenset({"client"})) is None
    assert match_rule("status of ticket 4471", [ticketing], entities=frozenset({"ticket"}))


# ------------------------------------ the gate is not skipped, only the model


def test_a_caller_with_no_grant_on_the_entity_never_reaches_the_database() -> None:
    """**The property the whole module is arranged around.** The fast lane skips the model
    and never the gate. A caller holding no grant on the entity compiles to FALSE, and
    `read_rows` does not run a statement it already knows is empty.

    Asserted on whether the source was asked, not on what came back. An empty result is what
    a wrong implementation returns too, after fetching the row and dropping it, and the
    difference between those is the whole of `THE_SCOPE_PREDICATE_GOES_INSIDE_THE_QUERY`.

    Delete this and the fast path can be made quick by reading first and checking after,
    which is the single worst change anybody could make to this system."""
    source = RecordingSource(ACME)

    answer = respond(
        "hours left on Acme",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents("read:ticket"),
    )

    assert answer is not None
    assert answer.result.records == ()
    assert source.queries == [], "the fast lane asked the database about a denied entity"


def test_a_caller_who_holds_the_grant_gets_the_row() -> None:
    """The sibling of the refusal above. A guard tested only by what it refuses is satisfied
    by a lane that answers nobody, and a fast lane that answers nobody looks exactly like a
    fast lane nobody's questions match.

    Delete this and every refusal test above stays green with the lane switched off."""
    source = RecordingSource(ACME)

    answer = respond(
        "hours left on Acme",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS),
    )

    assert answer is not None
    assert answer.rule_id == "client_hours_remaining"
    assert answer.field == "hours_remaining"
    assert answer.grounded
    assert [r.id for r in answer.result.records] == ["c_447"]
    assert answer.result.records[0].model_dump()["hours_remaining"] == "12"


def test_the_callers_own_scope_is_in_the_where_clause_of_the_statement_that_runs() -> None:
    """`E_run(caller, agent) = E(caller) ∩ agent_ceiling` holds here by the fast lane having
    no query of its own: the statement is compiled by the row plane from the grant the caller
    holds, scope and all. This asserts the scope reached the SQL rather than that a function
    was called with it.

    Delete this and a narrowing grant can be honoured for the answer lane and dropped for the
    fast one, which is a permission bug that shows up as a person seeing more when they are
    in a hurry."""
    source = RecordingSource(ACME)
    web_only = Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),))

    respond(
        "hours left on Acme",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS, scope=web_only),
    )

    assert len(source.queries) == 1
    statement = parameterised(source.queries[0])
    assert "fields ->> 'department' = %(s0)s" in statement
    assert bound(source.queries[0])["s0"] == "web"
    # And the tool's own pin is in the same clause, so this is the caller's scope narrowing a
    # statement that was already narrowed rather than the only predicate there is.
    assert "entity = %(t0)s" in statement


def test_the_question_narrows_on_the_field_the_rule_matches_on() -> None:
    """A rule names two projected fields and they do different jobs: `match_field` is what
    the slot value is compared against, and `answer_field` is what comes back. Swapping them
    compiles perfectly well and returns nothing, for everybody, silently, and it looks
    exactly like a client nobody has heard of.

    Asserted on the compiled predicate rather than on the result, because the fake source
    ignores the query: a test reading what came back would pass with the filter on any
    column at all. Found by a mutation, which is why the fixture uses a rule whose two
    fields differ.

    Delete this and the two fields can be swapped without a single test noticing."""
    source = RecordingSource(ACME)
    assert HOURS.match_field != HOURS.answer_field

    answer = respond(
        "hours left on Acme",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS),
    )

    assert answer is not None and answer.field == "hours_remaining"
    assert "fields ->> 'name' = %(q0)s" in parameterised(source.queries[0])


def test_the_slot_value_is_bound_as_a_parameter_and_never_reaches_the_statement() -> None:
    """The one string a person supplies in this lane is the slot value, and it travels inside
    a `Scope`, which `compile_where` binds. A value that reached the SQL text would be an
    injection in the lane whose defence is the scope predicate.

    The hostile value is chosen to pass the name check rather than to look alarming: a value
    containing `or` is refused by the qualifier rule and would test nothing at all, which is
    exactly the fixture that reads well and discriminates nothing.

    Delete this and a rule could be answered by a statement built from the question."""
    source = RecordingSource()

    respond(
        "hours left on Acme'; DROP TABLE",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS),
    )

    assert len(source.queries) == 1
    assert "DROP" not in parameterised(source.queries[0])
    assert bound(source.queries[0])["q0"] == "Acme'; DROP TABLE"


def test_the_fast_lane_builds_no_sql_of_its_own() -> None:
    """There is one path to a row and it is the row plane's. A second one here would be a
    second place the scope predicate can be left out, and it would be shorter and faster,
    which is how it would get written.

    Checked with the row plane's own checker rather than a copy, so a module that formats a
    statement is refused by the same rule that governs the row plane.

    Delete this and the quickest way to make this lane quicker is also the way past it."""
    assert_no_sql_is_built_by_interpolation(fast_lane)


def test_two_records_answering_to_one_name_answer_neither() -> None:
    """A fast-lane question names one thing. Choosing between two records that both answer to
    the name is a coin toss presented as a fact with a citation on it.

    The limit is two rather than one so the second record exists to be seen: at a limit of
    one, an ambiguous name and an unambiguous one return exactly the same thing, and the
    lane would confidently answer with whichever row sorted first.

    Delete this and an ambiguous client name is answered by whichever record has the lower
    id, for ever, without anybody finding out."""
    source = RecordingSource(ACME, ZEPHYR)

    answer = respond(
        "hours left on Acme",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS),
    )

    assert answer is None
    assert source.queries[0].statement.compile(dialect=DIALECT).params["param_1"] == 2
    assert FAST_LANE_ROW_LIMIT == 2


def test_a_question_no_rule_matches_returns_nothing_and_asks_nothing() -> None:
    """Falling through is the ordinary outcome and it costs a model call, which is the cheap
    side of the asymmetry this lane is built on. It must also cost no query: a lane that
    fetched rows for questions it then declined to answer would be doing the expensive half
    of the work for the traffic it cannot help.

    Delete this and every unmatched question runs a query whose result nothing reads."""
    source = RecordingSource(ACME)

    answer = respond(
        "what did we invoice Acme for in November",
        rules=[HOURS],
        readers=readers_for(source),
        entitlement=ents(*SEES_CLIENT_HOURS),
    )

    assert answer is None
    assert source.queries == []


def test_a_rule_naming_a_pair_this_lane_cannot_fetch_is_a_wiring_error_and_says_so() -> None:
    """Unreachable through `respond`, which derives the entity set from the same mapping, and
    checked anyway. The two part company the day somebody passes the entity set separately,
    and the symptom would be a `KeyError` in a request path rather than a sentence naming the
    rule and the pair.

    Delete this and that day produces a stack trace instead of a message."""
    source = RecordingSource(ACME)
    readers = readers_for(source)
    match = RuleMatch(rule=EXPIRY, value="Acme")
    assert match.rule.entity in entities_served(readers)

    wrong_source = HOURS.model_copy(update={"source": "xero"})
    with pytest.raises(FastLaneError, match="no reader for"):
        respond(
            "hours left on Acme",
            rules=[wrong_source],
            readers=readers,
            entitlement=ents(*SEES_CLIENT_HOURS),
        )


# ------------------------------------------- the catalogue is empty (M6.1.4)


def test_a_fast_lane_answer_has_nowhere_to_put_a_tool() -> None:
    """**The structural half of the empty catalogue.** The prose says the fast lane shows a
    model no tools; this says there is no attribute to put one in.

    A rule stated in a docstring holds until somebody has a bad afternoon. A frozen dataclass
    with five named fields and no room for a sixth means adding a tool is an edit to the
    model, in a module whose docstring argues against it, which is a decision somebody makes
    rather than a line they add.

    Delete this and the catalogue is empty by habit."""
    names = {f.name for f in dataclasses.fields(FastLaneAnswer)}

    assert names == {"rule_id", "entity", "source", "field", "result"}
    annotations = " ".join(f"{f.name}:{f.type}" for f in dataclasses.fields(FastLaneAnswer))
    for forbidden in ("tool", "catalogue", "catalog", "prompt", "model", "message"):
        assert forbidden not in annotations.lower()


def test_the_fast_lane_imports_nothing_that_could_build_a_catalogue_or_call_a_model() -> None:
    """Checked on the imports rather than on the words, because the words are in the module's
    own docstring and a text search for them would be satisfied by the sentence arguing
    against them. An import is the only way any of these names gets into scope.

    The one non-core module the fast lane imports is checked too, because the check reads
    direct imports and a name re-exported by a neighbour would be in scope without appearing
    here. Two modules is the whole path: everything else the fast lane imports is
    `brain.core`, which holds no registry and no driver.

    Delete this and the empty catalogue survives exactly until one fast answer needs one
    tool, which is a reasonable-sounding change that nothing would flag."""
    import brain.core.fast_path as core_fast_path
    import brain.knowledge.rows as row_plane

    assert_reaches_no_tool_and_no_model(fast_lane)
    assert_reaches_no_tool_and_no_model(row_plane)
    assert_reaches_no_tool_and_no_model(core_fast_path)


def test_the_import_check_refuses_a_module_that_does_reach_a_tool(tmp_path: Path) -> None:
    """The positive control for the check above. A checker that passed everything would pass
    the real module too, and the green run would be read as evidence.

    Delete this and `assert_reaches_no_tool_and_no_model` can be gutted to `return None` with
    every test in this file still green."""
    reaching = a_module(
        tmp_path,
        "reaching",
        "from brain.tools.registry import ToolRegistry\n\nREGISTRY = ToolRegistry\n",
    )

    with pytest.raises(FastLaneError, match=r"brain\.tools\.registry"):
        assert_reaches_no_tool_and_no_model(reaching)


def test_no_rule_field_is_ever_compiled() -> None:
    """A rule is data, and the edit that makes it code is one import and one call. Both
    modules that hold the grammar are checked: the matcher, and the core module the table
    generates its constraints from.

    Delete this and a template becomes a pattern the first time somebody needs alternation,
    in the one lane with no model downstream able to notice the answer is wrong."""
    import brain.core.fast_path as core_fast_path

    assert_rules_are_never_compiled(fast_lane)
    assert_rules_are_never_compiled(core_fast_path)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        pytest.param("compiles", "import re\n\nSHAPE = re.compile('^a$')\n", id="compiles"),
        # `re.split` is not on `COMPILING_CALLS`, so this module is refused by the import
        # rule alone. Without it the import rule has no test of its own: every other case
        # here also calls something on the list, and a mutation disabling the import check
        # survived because `re.compile` caught it instead.
        pytest.param(
            "imports_only", "import re\n\nPARTS = re.split(',', 'a,b')\n", id="imports re at all"
        ),
        pytest.param(
            "evaluates", "def run(rule):\n    return eval(rule['template'])\n", id="evaluates"
        ),
        pytest.param(
            "dispatches",
            "def run(rule, mod):\n    return getattr(mod, rule['handler'])()\n",
            id="dispatches",
        ),
    ],
)
def test_the_compile_check_refuses_each_way_a_rule_could_become_code(
    tmp_path: Path, name: str, source: str
) -> None:
    """The positive control, one case per spelling. A checker looking for `re.compile` alone
    passes the module that reaches `eval`, and one looking for both passes the module that
    looks a function up by a name out of the row.

    Delete this and the check is green for whichever spelling nobody thought of."""
    with pytest.raises(FastLaneError):
        assert_rules_are_never_compiled(a_module(tmp_path, name, source))


def test_the_lane_cannot_be_asked_for_rows_without_saying_whose_reach() -> None:
    """`entitlement` is keyword-only and has no default, on `respond` and on the reader
    protocol alike. A default would be the one thing that lets a caller read a row without
    saying who is reading it, and the default that would get written is an empty set, which
    is a legitimate value that flows onward and produces a confident "I could not find that".

    Delete this and the signature can acquire a default in a refactor about argument order."""
    parameter = inspect.signature(respond).parameters["entitlement"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty

    reader = inspect.signature(fast_lane.RowReader.__call__).parameters["entitlement"]
    assert reader.kind is inspect.Parameter.KEYWORD_ONLY
    assert reader.default is inspect.Parameter.empty


# ------------------------------- the fast lane reaches projected tables only (M6.1.3)


def test_no_migration_in_this_repository_widens_what_the_fast_lane_reaches() -> None:
    """M6.1.3 is a property of every migration rather than of any one of them: 0001 made the
    role, 0008 gave it SELECT on the one projected table, and the way it would be lost is a
    file written next year granting it a document table because a fast answer needed one.

    Run over the real directory, so a migration added after this test was written is covered
    by it without anybody remembering.

    Delete this and the fast lane's reach becomes a matter of everyone reading every
    migration."""
    widening = [f for f in check_all() if "fast lane" in f.rule]

    assert widening == []


@pytest.mark.parametrize(
    ("label", "source"),
    [
        pytest.param(
            "a table outside proj",
            'G = ("GRANT SELECT ON know.chunk TO brain_fastlane",)',
            id="another schema",
        ),
        pytest.param(
            "a second verb",
            'G = ("GRANT SELECT, UPDATE ON proj.record TO brain_fastlane",)',
            id="a second verb",
        ),
        pytest.param(
            "every privilege",
            'G = ("GRANT ALL ON proj.record TO brain_fastlane",)',
            id="every privilege",
        ),
        pytest.param(
            "another role",
            'G = ("GRANT brain_app TO brain_fastlane",)',
            id="another role",
        ),
        pytest.param(
            "a policy off proj",
            'R = ("CREATE POLICY p ON gate.scope FOR ALL TO brain_fastlane",)',
            id="a policy off proj",
        ),
        pytest.param(
            "a writing policy",
            'R = ("CREATE POLICY p ON proj.record FOR ALL TO brain_fastlane",)',
            id="a writing policy",
        ),
    ],
)
def test_the_grant_check_refuses_each_way_the_fast_lane_could_be_widened(
    tmp_path: Path, label: str, source: str
) -> None:
    """Six ways to widen one role, and a check that caught only the first would report "ok"
    about the other five. The verb cases matter as much as the schema ones: `GRANT SELECT,
    UPDATE` still contains the word SELECT, so a rule looking for a substring passes it.

    `GRANT brain_app TO brain_fastlane` is the case that looks like the allowed shape and is
    its opposite: the allowed statement hands the fast-lane role to somebody, and this one
    hands the application's role to the fast lane.

    Delete this and the checker can be loosened one pattern at a time with nothing failing."""
    path = tmp_path / "0099_widening.py"
    path.write_text(f"def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n\n\n{source}\n")

    findings = [f for f in check_file(path) if "fast lane" in f.rule]

    assert len(findings) == 1, label


def test_the_grant_check_reads_statements_and_not_the_prose_around_them(tmp_path: Path) -> None:
    """Half the migrations here argue about `brain_fastlane` in their docstrings, and a
    migration explaining why it does not widen the role would naturally write out the
    statement it is refusing to write. A text search would report that explanation as the
    violation, and a check that cries wolf about a docstring is a check somebody switches off.

    **The docstring quotes a statement the checker would refuse**, which is the whole test.
    The first version of this fixture said "granted brain_fastlane SELECT", and `\\bGRANT\\b`
    does not match "granted", so a mutation replacing the parse tree with the raw file text
    survived it. A fixture that reads like the real thing and cannot tell the two
    implementations apart is the same failure as no test at all.

    Delete this and the next migration to explain itself fails the build."""
    path = tmp_path / "0098_discussion.py"
    path.write_text(
        '"""A later migration must never write\n'
        "GRANT SELECT ON know.chunk TO brain_fastlane,\n"
        'however tempting a fast answer over documents becomes."""\n'
        "\n\ndef upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
    )

    assert [f for f in check_file(path) if "fast lane" in f.rule] == []


def test_the_rule_table_hands_the_fast_lane_nothing() -> None:
    """A rule is configuration and is read by the application, which matches in memory and
    then fetches rows under the fast-lane role. Granting that role a table in `gate` would be
    the first crack in the one property M6.1.3 asks for.

    Asserted on the rendered SQL rather than on the constants, so a statement sitting in a
    tuple that `upgrade` never executes cannot satisfy it and neither can one that does.

    Delete this and the obvious way to let the lane read its own rules is also the way past
    the boundary."""
    assert "brain_fastlane" not in squash(rendered("upgrade"))
    assert "brain_fastlane" not in squash(rendered("downgrade"))


# --------------------------------------------------------- the migration (0019)


def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0019", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rendered(direction: str) -> str:
    """The SQL the migration emits, rendered without a database.

    Alembic's `--sql` mode driven in-process. It matters that the tests read this rather than
    the file's text: a statement sitting in a constant that `upgrade` never executes would
    pass a source-text search and build nothing.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": metadata},
    )
    with Operations.context(context):
        getattr(migration(), direction)()
    return buffer.getvalue()


def squash(text: str) -> str:
    """Collapse whitespace, so a statement wrapped across lines still compares."""
    return " ".join(text.split())


def table() -> Table:
    """The mapped table, narrowed to `Table`. `__table__` is annotated `FromClause`, which
    has no `indexes` worth reading and cannot be handed to `CreateTable`."""
    mapped = FastPathRuleRow.__table__
    assert isinstance(mapped, Table)
    return mapped


def test_the_migration_follows_the_one_before_it() -> None:
    """A revision that does not chain is a migration Alembic never runs, and the symptom is a
    table missing in production while every test passes. Two migrations were numbered 0018 on
    the same afternoon this one was written, which is how the number here was chosen.

    Delete this and a branch point in the revision graph reaches production."""
    module = migration()
    assert module.revision == "0019"
    assert module.down_revision == "0018"


def test_the_migration_builds_the_table_the_model_declares() -> None:
    """The migration copies the model's predicates rather than importing them, so that it
    keeps describing the database it actually built, which means the copy needs something
    comparing it or it rots without saying so.

    Compared on rendered DDL rather than on source text, so a difference in type, width,
    nullability, default or constraint is caught rather than a difference in wording. The
    indexes are compared too: `SoftDeleteMixin` declares one that is easy to leave out, and
    the unique one is a constraint rather than a performance choice.

    Delete this and the five template checks exist in Python and not in the database."""
    assert migration().TABLES == ("gate.fast_path_rule",)
    upgrade = squash(rendered("upgrade"))
    assert squash(str(CreateTable(table()).compile(dialect=DIALECT))) in upgrade
    indexes = sorted(table().indexes, key=lambda i: i.name or "")
    assert [i.name for i in indexes] == [
        "ix_gate_fast_path_rule_deleted_at",
        "uq_fast_path_rule_template_live",
    ]
    for index in indexes:
        assert squash(str(CreateIndex(index).compile(dialect=DIALECT))) in upgrade


def test_the_database_checks_the_same_template_grammar_the_type_does() -> None:
    """Stated separately from the DDL comparison above, because that one fails with a wall of
    SQL and this one names the number. The migration writes the bounds out and the model
    generates them from `brain.core.fast_path`, so the two are independent copies and this is
    what makes them agree.

    Delete this and raising `MIN_LITERAL_CHARS` leaves the database enforcing the old floor,
    which surfaces as a rule the code accepts and the insert rejects."""
    module = migration()
    declared = str(module.TEMPLATE_LENGTH)
    floor = str(module.LITERAL_IS_LONG_ENOUGH)
    assert declared == f"length(template) BETWEEN {MIN_TEMPLATE_CHARS} AND {MAX_TEMPLATE_CHARS}"
    assert floor.endswith(f">= {MIN_LITERAL_CHARS}")
    checks = squash(rendered("upgrade"))
    assert "position('{' in template) < position('}' in template)" in checks
    assert "= slot" in checks


def test_the_migration_enables_row_level_security() -> None:
    """A policy on a table without row-level security enabled is a policy PostgreSQL never
    consults, and `sweep_rls` fails the build on a table in a named schema without it. The
    two statements are separate and forgetting the first is silent.

    Delete this and the table ships readable to anything holding the role."""
    sql = squash("\n".join(migration().RLS))
    assert "ALTER TABLE gate.fast_path_rule ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY fast_path_rule_readable ON gate.fast_path_rule" in sql


def test_the_migration_grants_no_delete() -> None:
    """A rule that stops being right is retired with `deleted_at`, not removed: "which rule
    answered that question in March" is asked after a wrong answer, and a fast-lane answer
    had no model in it to explain itself. The one DELETE grant in this system belongs to
    `auth.directory_role_grant`.

    Delete this and a rule can be removed without trace, which makes a past answer
    unexplainable."""
    assert migration().GRANTS == (
        "GRANT SELECT, INSERT, UPDATE ON gate.fast_path_rule TO brain_app",
    )
    assert all("DELETE" not in statement for statement in migration().GRANTS)


def test_the_downgrade_drops_what_the_upgrade_built() -> None:
    """A migration with no way back is a deploy with no way back. `gate` is not dropped: 0001
    created all nine schemas and 0001's downgrade owns them.

    Delete this and a failed deploy has nowhere to go."""
    down = squash(rendered("downgrade"))
    assert "DROP TABLE gate.fast_path_rule" in down
    assert "DROP SCHEMA" not in down


def test_the_migration_satisfies_the_migration_policy() -> None:
    """The mechanical rules: a downgrade that exists and does something, no unreviewed
    autogeneration markers, no schema and data change in one file, and the fast-lane grant
    rule this migration is half of.

    Delete this and the file this leaf is written in is the one file the policy never sees."""
    assert check_file(MIGRATION) == []


def test_a_template_the_type_accepts_is_one_the_database_would_accept() -> None:
    """The type and the table are two statements of one grammar, and the thing that goes
    wrong is that they diverge in the middle: a template the type accepts and the constraint
    refuses fails at insert time on a rule that passed review.

    Checked by putting the same templates through `check_template` and through the
    constraints' own arithmetic, evaluated here in Python rather than in PostgreSQL, because
    there is no database in this suite. That makes it a check of the arithmetic rather than
    of the SQL, which is the half that is easy to get wrong.

    Delete this and the two drift, and the failure lands on whoever adds a rule."""
    good = "hours left on {client}"
    bad = "{client} hours"

    check_template(good, "client")
    with pytest.raises(ValueError, match="literal characters"):
        check_template(bad, "client")

    for template, expected in ((good, True), (bad, False)):
        literal_length = len(template) - (template.index("}") - template.index("{") + 1)
        assert (literal_length >= MIN_LITERAL_CHARS) is expected
