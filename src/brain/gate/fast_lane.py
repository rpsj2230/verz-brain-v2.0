"""Answering from the projection with no model in the loop, on rules that are rows.

`brain.gate.classify` decides that a question belongs in the fast lane. This is what the
fast lane then does, and the whole of it is arranged around one sentence: **the fast lane
skips the model, never the gate.** A path that skipped the entitlement check in order to be
quick would be the worst defect this system could ship, because it would be quiet, general,
and indistinguishable from the system working.

So nothing here decides a permission. The reach is decided where it is already decided:
`brain.knowledge.rows.compile_row_query` composes the caller's scope into the WHERE clause
and compiles the SELECT list from the caller's grants, and a caller who holds no grant on
the entity compiles to `FALSE` and never reaches the database at all. This module builds a
`RowRequest` and hands it to a reader that was bound to a source and an entity by whoever
wired the lane. There is no second path to a row and no argument here that could become one.

**A rule is a row, and a row cannot be code.** `INTENTS` in `brain.gate.classify` is four
regular expressions in a source file, so a fifth question shape is a deployment. That is the
wrong shape for a thing an operator learns about weekly. The alternative that suggests itself
is a rule row carrying a pattern, and it is worse than the code it replaces: a caller-supplied
regular expression is a program, with an execution time nobody bounded, evaluated inside the
one lane that has no model downstream able to notice the answer is wrong. `FastPathRule`
therefore carries a **template with exactly one hole**, `hours left on {client}`, and every
character outside the hole is matched literally. A template cannot express alternation,
repetition, backtracking or a lookahead, because there is no syntax in it to express them
with. See `A_RULE_IS_A_ROW_AND_A_ROW_MUST_NOT_BE_ABLE_TO_BECOME_CODE`, and
`assert_rules_are_never_compiled`, which refuses a module that could compile one anyway.

**The fast lane has nowhere to put a tool.** M6.1.4 asks for the empty tool catalogue to be
structural, and a rule saying "the fast lane has no tools" holds until the first person who
needs one. `FastLaneAnswer` has no field for a tool, a catalogue or a prompt, and this module
imports nothing that could build any of them: not `brain.tools.registry`, not
`brain.gate.catalogue`, not `brain.models`. `assert_reaches_no_tool_and_no_model` checks the
imports rather than trusting the sentence, which is the same construction
`brain.ops.automation_piece` makes about addresses and `brain.gate.catalogue` makes about
`ProjectedCatalogue`: a type that cannot hold the thing beats a rule saying it must not.

**Ambiguity is a fall-through, never a choice.** Two rules matching one question, or two
records matching one name, both mean the question was not the exact one anybody wrote a rule
for. In a lane with a model, picking one and being wrong is recoverable, because something
reads the answer. Here nothing does. Both cases therefore return nothing and let the answer
lane read the actual words, which costs one model call and is the cheap side of the asymmetry
`brain.gate.classify` is built on.

**An empty answer is the same answer for a denial and an absence.** A caller with no grant on
the entity gets a result with no records, and so does a caller asking about a client that does
not exist. They are the same object, produced by the same path, in the same time: the query
that would distinguish them is never run in either case, because `certainly_empty` short
circuits it. Returning nothing and falling through for the denial would have made the two
distinguishable by how long the reply took.

Rejected: matching the caller's entitlements in the matcher, so that a rule about tickets is
not considered for somebody who cannot read tickets. It reads like a saving and it is a second
implementation of the central rule, sitting in front of the real one. The matcher filters on
which entities the row plane actually serves, which is a fact about wiring and not about a
person, and the permission is decided once, downstream, where it already was.

Rejected: a rule row carrying the SQL, the column list or a scope. Every one of those is the
row plane's decision, and a rule that could narrow rows would be a grant written by whoever
can insert a row into a configuration table.

Rejected: rendering the answer here. Redaction runs after this and before anything is
composed, and it runs on the reach the query was compiled from; a sentence built in this
module would be a payload that had not been through `brain.core.redaction`.

Scope: nothing here opens a connection, reads a clock or loads a rule. The rules arrive as
mappings and the rows arrive through a `RowReader`, for the reason `brain.ops.limits` gives
about holding no client: the cases worth testing in a lane like this are the empty ones and
the ambiguous ones, and neither is reachable through a module that owns a socket.

**Nothing calls this yet.** The gate is not assembled end to end anywhere in this repository:
`classify_lane` has no caller in `src` either, and there is no implementation of
`brain.knowledge.rows.RowSource`, so the reader this module needs cannot be built against a
real database today. What is here is the part that can be written and checked before that
wiring exists, and the wiring is a request path rather than a lane.

Task ids: M6.1.1, M6.1.2, M6.1.4
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from brain.core.entitlement import EntitlementSet
from brain.core.envelope import OBJECT_NAME_PATTERN, TypedResult
from brain.core.fast_path import (
    MAX_TEMPLATE_CHARS,
    MIN_TEMPLATE_CHARS,
    check_template,
    literal_parts,
)
from brain.core.scope import Clause, Op, Scope
from brain.gate.classify import is_a_name_not_a_phrase
from brain.knowledge.rows import RowRecord, RowRequest

log = structlog.get_logger()

# ------------------------------------------------------------------ written-down reasons

#: Why a rule carries a template with one hole rather than a pattern.
A_RULE_IS_A_ROW_AND_A_ROW_MUST_NOT_BE_ABLE_TO_BECOME_CODE = (
    "A rule row exists so that a fifth question shape is an insert rather than a deployment. "
    "The obvious column for the shape is a regular expression, and that is a program: an "
    "execution time nobody bounded, written by whoever can insert a configuration row, "
    "evaluated in the one lane with no model downstream able to notice the answer is wrong. "
    "A template with one hole has no syntax for alternation, repetition or backtracking, so "
    "there is nothing to bound and nothing to compile."
)

#: Why the fast lane has no tool list at all, and why that is a shape rather than a rule.
THE_FAST_LANE_HAS_NOWHERE_TO_PUT_A_TOOL = (
    "The fast lane answers with no model, so the tool catalogue it shows a model is empty. "
    "Written as a rule that would hold until the first person who needed one tool. Written "
    "as a shape it cannot be undone by accident: FastLaneAnswer has no field for a tool, a "
    "catalogue or a prompt, and this module imports nothing that can build one. Adding a "
    "tool here means adding an import and a field, in a module whose docstring argues "
    "against both, which is a decision somebody makes rather than a line they add."
)

#: Why two rules matching one question produces no answer rather than the first one.
TWO_RULES_MATCHING_ONE_QUESTION_IS_A_FALL_THROUGH = (
    "Two rules matching means the question is not the exact question either rule was "
    "written for, and picking the first is picking by insertion order. In a lane with a "
    "model, answering the wrong question is caught by something reading the answer. Here "
    "nothing reads it. So neither rule answers, the answer lane takes the question, and the "
    "cost is one model call against an operator error that would otherwise be invisible."
)

#: Why two records matching one name produces no answer rather than the first row.
TWO_RECORDS_MATCHING_ONE_NAME_IS_A_FALL_THROUGH = (
    "A fast-lane question names one thing. Two records answering to that name is an "
    "ambiguous name, and choosing between them is a coin toss whose result is presented as "
    "a fact with a citation on it. The row limit is two rather than one so that the second "
    "record is visible at all: at a limit of one, an ambiguous name and an unambiguous one "
    "return exactly the same thing."
)

#: Why a denial and an absence come back as the same empty answer.
AN_EMPTY_ANSWER_IS_THE_SAME_ANSWER_FOR_A_DENIAL_AND_AN_ABSENCE = (
    "A caller holding no grant on the entity compiles to FALSE and the query is never run. "
    "A caller asking about a client that does not exist runs the query and gets nothing "
    "back. Both return a result with no records, from the same path, and neither is told "
    "which happened. Falling through to the answer lane for the denial alone would have "
    "made the two distinguishable by how long the reply took, which is one bit about what "
    "exists, available to anybody willing to ask twice and count."
)

# ---------------------------------------------------------------------- bounds

#: What a slot value may be. The upper bound is `brain.gate.classify`'s own client pattern,
#: `[\\w &.'-]{2,60}`, restated as two numbers because a template has no pattern to carry it.
MIN_SLOT_VALUE_CHARS: Final = 2
MAX_SLOT_VALUE_CHARS: Final = 60

#: How many rules may be loaded at once. The matcher is linear in this and runs on every
#: question, and a rule set past a couple of hundred entries is a corpus somebody should be
#: searching rather than a vocabulary somebody wrote.
MAX_RULES: Final = 200

#: How many rows a fast-lane question may fetch. Two, so that an ambiguous name is visible.
#: See `TWO_RECORDS_MATCHING_ONE_NAME_IS_A_FALL_THROUGH`.
FAST_LANE_ROW_LIMIT: Final = 2

#: The fields a rule row must carry, and the only ones read off it. Named rather than
#: splatted, so a column added to `gate.fast_path_rule` cannot reach the matcher by
#: accident: a new column is inert until somebody adds it here and to the type below.
DECLARED_FIELDS: Final[tuple[str, ...]] = (
    "rule_id",
    "template",
    "slot",
    "source",
    "entity",
    "match_field",
    "answer_field",
)


class FastLaneError(Exception):
    """A rule set that cannot be made safe, or a lane wired wrongly.

    Outside the user-facing taxonomy in `brain.core.errors`, for the reason
    `brain.knowledge.rows.RowPlaneError` gives about its own: nobody asking a question ever
    sees this. It is a contract violation by whoever wrote the rule or wired the lane.

    Note what does *not* raise it. A question that matches nothing, a question two rules
    match, a caller with no grant and a name nothing answers to are all ordinary outcomes
    and none of them is an error.
    """


# ------------------------------------------------------------- the rule (M6.1.1)


class FastPathRule(BaseModel):
    """One question shape, as a row: a template with one hole and where to look.

    A pydantic model rather than a dataclass because every one of these arrives as a
    database row and has to be validated on the way in. `extra="forbid"` is part of the
    rule rather than tidiness: a row carrying `handler` or `pattern` or `python` is refused
    rather than ignored, so a column somebody adds to smuggle behaviour in cannot arrive
    quietly and sit unread until a later edit starts reading it.

    `match_field` and `answer_field` are projected field names, and neither is checked
    against a classification here. That check belongs to the row plane, which compiles the
    SELECT list from the caller's grants: a field the caller may not read is not in the
    projection, and a filter naming it compiles the whole request to nothing. A rule naming
    a field nobody may read is therefore a rule that answers nothing, which is the correct
    outcome and needs no second opinion here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The operator's name for this rule. In every log line, and never a question.
    rule_id: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)

    #: The question shape, with exactly one `{slot}` in it. Everything else is literal.
    template: str = Field(min_length=MIN_TEMPLATE_CHARS, max_length=MAX_TEMPLATE_CHARS)

    #: The name inside the braces. Carried as its own column so the database can check the
    #: template against it rather than take the template's word for what it declares.
    slot: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)

    #: Which connector's records answer this, and which kind. Both, because `proj.record` is
    #: keyed by both: Freshdesk company 42 and Xero contact 42 are different companies.
    source: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)
    entity: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)

    #: The projected field the slot value is compared against, and the one holding the
    #: answer. Separate, because "which client" and "what about them" are different columns.
    match_field: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)
    answer_field: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)

    @model_validator(mode="after")
    def _template_has_exactly_one_hole(self) -> FastPathRule:
        """One hole, named for the declared slot, with enough literal text around it.

        The grammar itself lives in `brain.core.fast_path` and is called rather than copied,
        because `gate.fast_path_rule` states the same four checks as check constraints and
        the three of them have to agree. This refuses the rule on the way in; the constraints
        refuse the row that arrived some other way, which is why `proj.record` carries its
        field cap twice and says so.
        """
        try:
            check_template(self.template, self.slot)
        except ValueError as exc:
            msg = f"rule {self.rule_id}: {exc}"
            raise ValueError(msg) from exc
        return self

    @property
    def before(self) -> str:
        """The literal text ahead of the hole. Empty when the template opens with it."""
        return literal_parts(self.template)[0]

    @property
    def after(self) -> str:
        """The literal text after the hole. Empty when the template ends with it."""
        return literal_parts(self.template)[1]


def rules_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[FastPathRule, ...]:
    """Validate a rule table into rules, or refuse the whole set (M6.1.1).

    Refuses rather than skipping a bad row, and the choice is worth stating because the
    other one looks kinder. A skipped row is a rule an operator believes is live and which
    silently is not, and the fast lane is exactly where nobody would notice: a question that
    should have been answered instantly is answered by a model instead, correctly, a little
    slower. Refusing is loud and it is safe, because a caller that catches this and runs with
    no rules has the answer lane, which is what every question got before this module existed.

    Only `DECLARED_FIELDS` are read off each row. A column added to the table is inert until
    somebody adds it to that tuple and to `FastPathRule`, so a rule table cannot grow a field
    the matcher starts honouring without an edit to this file.
    """
    out: list[FastPathRule] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        missing = [name for name in DECLARED_FIELDS if name not in row]
        if missing:
            msg = f"rule row {index} is missing {sorted(missing)}"
            raise FastLaneError(msg)
        try:
            rule = FastPathRule(**{name: row[name] for name in DECLARED_FIELDS})
        except ValidationError as exc:
            # The row's own values are not repeated into the message. A rule is
            # configuration rather than a person's data, but the habit is the point: a
            # message that quotes what it read is a message that will one day quote a value.
            msg = f"rule row {index} is not a valid rule"
            raise FastLaneError(msg) from exc
        if rule.rule_id in seen:
            msg = f"rule {rule.rule_id} appears twice; a rule id names one rule"
            raise FastLaneError(msg)
        seen.add(rule.rule_id)
        out.append(rule)
    if len(out) > MAX_RULES:
        msg = f"{len(out)} rules, over the {MAX_RULES} this lane matches on every question"
        raise FastLaneError(msg)
    return tuple(out)


# ------------------------------------------------------------ the matcher (M6.1.2)


@dataclass(frozen=True)
class RuleMatch:
    """The rule that matched, and the one value the question supplied.

    `value` is the slot as the asker wrote it, whitespace tidied and case untouched. Case is
    left alone because it goes into a comparison against a projected field, and a name
    lowercased here would match nothing at all.
    """

    rule: FastPathRule
    value: str


def _tidy(text: str) -> str:
    """Whitespace collapsed and question marks dropped. Case deliberately untouched.

    Case is compared separately, on equal-length slices, so that indices into the tidied
    question stay valid for pulling the slot value out of it. Casefolding first would be
    simpler and would be wrong for any string whose folded form is a different length, where
    an index taken from the folded text points at the wrong character in the original.
    """
    return " ".join(text.replace("?", " ").split())


def _same(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def entities_served(readers: Mapping[tuple[str, str], RowReader]) -> frozenset[str]:
    """The entity kinds the row plane can actually answer about, from the wiring itself.

    Derived rather than passed, so the set the matcher filters on and the mapping the
    answer is fetched through cannot disagree. A rule naming an entity nothing serves is
    then simply never considered, which is the same outcome as no rule matching.
    """
    return frozenset(entity for _, entity in readers)


def match_rule(
    question: str, rules: Sequence[FastPathRule], *, entities: frozenset[str]
) -> RuleMatch | None:
    """The one rule this question is exactly, or nothing (M6.1.2).

    **A template describes the whole question, never a fragment of it.** `hours left on
    {client}` does not match `what are the hours left on Acme`, for the reason
    `brain.gate.classify.Intent` anchors its patterns at both ends: the extra words in a
    longer question almost always carry a qualifier that changes the answer, and there is
    nothing downstream in this lane able to notice one was ignored.

    Matching is literal on both sides of the hole and case-insensitive, with the boundary
    required to fall on a space. Without the space, `hours left on` would match
    `hours left onacme` and the lane would answer a question nobody asked; anchoring alone
    does not catch it, because the near miss is inside the literal rather than outside it.

    The slot value is then held to the same rule `brain.gate.classify` holds its own slots
    to, by calling the same function: a value carrying a qualifier has swallowed a condition
    that changes the answer, and `hours left on Acme after the November work` is not a
    question about a client called `Acme after the November work`.

    Nothing about the caller reaches this function. `entities` is what the row plane serves,
    which is wiring; the permission is decided downstream, once, by the row plane.
    """
    tidy = _tidy(question)
    found: list[RuleMatch] = []
    for rule in rules:
        if rule.entity not in entities:
            continue
        match = _apply(rule, tidy)
        if match is not None:
            found.append(match)
    if len(found) > 1:
        # The rule ids and never the question. A question is a person's words, and a log
        # line is read by more people, for longer, than the answer was.
        log.warning("fast_lane.two_rules_matched", rules=sorted(m.rule.rule_id for m in found))
        return None
    return found[0] if found else None


def _apply(rule: FastPathRule, tidy: str) -> RuleMatch | None:
    """One rule against one tidied question. Literal on both sides, bounded in the middle."""
    before = _tidy(rule.before)
    after = _tidy(rule.after)
    # The `+ 1` on each side is the space the boundary has to fall on. A question shorter
    # than the literals plus their spaces cannot match, and slicing it would wrap.
    lead = len(before) + 1 if before else 0
    trail = len(after) + 1 if after else 0
    if len(tidy) < lead + trail:
        return None
    if before and not _same(tidy[:lead], before + " "):
        return None
    if after and not _same(tidy[len(tidy) - trail :], " " + after):
        return None
    value = tidy[lead : len(tidy) - trail].strip()
    if not MIN_SLOT_VALUE_CHARS <= len(value) <= MAX_SLOT_VALUE_CHARS:
        return None
    if not is_a_name_not_a_phrase(value):
        return None
    return RuleMatch(rule=rule, value=value)


# ------------------------------------------------- answering, with no tools (M6.1.4)


class RowReader(Protocol):
    """Whatever fetches rows for one source and one entity, with the caller's reach.

    This is `brain.knowledge.rows.RowTool.reader`'s return type, and it is stated as a
    protocol here so that nothing in this module has to import a tool to describe one. What
    matters about the signature is that `entitlement` is required and keyword-only: there is
    no spelling of this call that reads a row without saying whose reach it is read under.
    """

    def __call__(
        self,
        request: RowRequest,
        *,
        entitlement: EntitlementSet,
        now: datetime | None = ...,
    ) -> TypedResult[RowRecord]: ...


@dataclass(frozen=True)
class FastLaneAnswer:
    """What one fast-lane question produced: which rule, which field, and the rows.

    **There is no field here for a tool, a catalogue, a prompt or a model**, and that is
    M6.1.4 rather than an omission. See `THE_FAST_LANE_HAS_NOWHERE_TO_PUT_A_TOOL`.

    It carries the `TypedResult` rather than a sentence, because redaction has not run yet.
    The result goes through `brain.gate.compose.redact_for_gate` like any other lane's, and a
    sentence built here would be a payload that skipped the walker. `field` names the column
    the rule was written to answer with; it is a name and never a value, for the reason
    `brain.gate.compose.Citation` gives about its own.
    """

    rule_id: str
    entity: str
    source: str
    field: str
    result: TypedResult[RowRecord]

    @property
    def grounded(self) -> bool:
        """Whether a record came back. False for a denial and for an absence alike."""
        return bool(self.result.records)


def respond(
    question: str,
    *,
    rules: Sequence[FastPathRule],
    readers: Mapping[tuple[str, str], RowReader],
    entitlement: EntitlementSet,
    now: datetime | None = None,
) -> FastLaneAnswer | None:
    """Match, fetch, and hand back rows. No model, no tools, no second permission decision.

    One entry point rather than a match step and an answer step a caller pairs up, so that
    the entity set the matcher filtered on and the mapping the rows come from are the same
    object. Two arguments would be two things a caller can get out of step, and the way you
    find out is a rule matching for an entity nothing can fetch.

    Returns None for every ordinary reason a fast-lane answer is not the right one: no rule
    matched, two did, or two records answered to the name. It returns an answer with no
    records for a caller who may not see the entity, which is the same answer somebody gets
    for a name that does not exist. See
    `AN_EMPTY_ANSWER_IS_THE_SAME_ANSWER_FOR_A_DENIAL_AND_AN_ABSENCE`.
    """
    match = match_rule(question, rules, entities=entities_served(readers))
    if match is None:
        return None
    reader = readers.get((match.rule.source, match.rule.entity))
    if reader is None:
        # Unreachable through `entities_served`, which is derived from this same mapping, and
        # checked anyway: the two would part company the day somebody passes the entity set
        # separately, and the symptom would be a KeyError in a request path.
        msg = (
            f"rule {match.rule.rule_id} names {match.rule.source}.{match.rule.entity}, "
            "which this lane has no reader for"
        )
        raise FastLaneError(msg)

    request = RowRequest(
        # A `Scope` rather than a filter type of this module's own, so the asker's narrowing
        # is the same kind of object as the system's and is bound as a parameter by the same
        # `compile_where`. There is no shape it can take that renders as SQL text.
        filters=Scope(clauses=(Clause(field=match.rule.match_field, op=Op.EQ, value=match.value),)),
        limit=FAST_LANE_ROW_LIMIT,
    )
    result = reader(request, entitlement=entitlement, now=now)
    if len(result.records) > 1:
        log.warning("fast_lane.two_records_matched", rule=match.rule.rule_id)
        return None
    return FastLaneAnswer(
        rule_id=match.rule.rule_id,
        entity=match.rule.entity,
        source=match.rule.source,
        field=match.rule.answer_field,
        result=result,
    )


# --------------------------------------------- the rules stay data (M6.1.1, M6.1.4)

#: Calls that turn text into something the interpreter runs. A rule field reaching any of
#: these is the failure this whole shape exists to prevent, whoever wrote the line.
COMPILING_CALLS: Final[frozenset[str]] = frozenset(
    {"compile", "eval", "exec", "__import__", "getattr", "re.compile", "re.fullmatch", "re.match"}
)

#: Modules that cannot appear in the fast lane without the empty catalogue stopping being
#: empty. `brain.models` is the model drivers, `brain.tools` is the registry, and
#: `brain.gate.catalogue` and `brain.gate.invoke` are the two places a `ProjectedCatalogue`
#: is built and consumed.
TOOL_BEARING_MODULES: Final[tuple[str, ...]] = (
    "brain.models",
    "brain.tools",
    "brain.gate.catalogue",
    "brain.gate.invoke",
    "brain.gate.leash",
)


def _call_name(node: ast.Call) -> str:
    """`re.compile` for an attribute call, `eval` for a bare one, empty for anything else."""
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every module name the source imports, however it spells the import."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def assert_rules_are_never_compiled(module: ModuleType) -> None:
    """Refuse a module that could turn a rule field into something the interpreter runs.

    Sound and incomplete, in that order and deliberately. What it catches is every spelling
    of "compile this string": `re.compile`, `eval`, `exec`, `compile`, `__import__`, and
    `getattr`, which is how a name in a row becomes a call. What it cannot catch is a
    dictionary of callables keyed by a rule field, because that is an ordinary lookup and
    looks like one. The remaining protection there is `FastPathRule`, which has no field a
    key could live in, and `extra="forbid"`, which refuses a row that grows one.

    Importing `re` at all is refused rather than only its calls. A module that imports it
    has a pattern one line away, and this module has no need of one: the template is matched
    with `startswith` and `endswith`, which is the whole point of a template.
    """
    tree = ast.parse(inspect.getsource(module))
    imported = _imported_modules(tree)
    if "re" in imported:
        msg = (
            f"{module.__name__} imports re; a fast-lane rule is matched literally and a "
            "module holding a pattern engine is one edit from compiling a rule field. "
            f"{A_RULE_IS_A_ROW_AND_A_ROW_MUST_NOT_BE_ABLE_TO_BECOME_CODE}"
        )
        raise FastLaneError(msg)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in COMPILING_CALLS:
            msg = (
                f"{module.__name__} calls {_call_name(node)} on line {node.lineno}. "
                f"{A_RULE_IS_A_ROW_AND_A_ROW_MUST_NOT_BE_ABLE_TO_BECOME_CODE}"
            )
            raise FastLaneError(msg)


def assert_reaches_no_tool_and_no_model(module: ModuleType) -> None:
    """Refuse a module that can reach a tool, a catalogue or a model driver (M6.1.4).

    Checked on the imports rather than on the words, because the words are in the docstring
    above and a search for them would be satisfied by the sentence that argues against them.

    **Direct imports only, and the limit is worth stating.** A module could in principle
    reach a registry through something it does import that re-exports one. What closes that
    here is not this function but the shape of what the fast lane imports: `brain.core.*`,
    and `brain.knowledge.rows`, which this same check is applied to in the test. Reaching a
    catalogue would then mean adding an import to one of those, in a file whose own tests
    would fail.

    Note what it does not claim. `brain.knowledge.rows` builds a `ToolDefinition`, and a
    definition is not a catalogue: `ProjectedCatalogue` can only be constructed by
    `brain.gate.catalogue.project`, which nothing on this path imports.
    """
    for name in _imported_modules(ast.parse(inspect.getsource(module))):
        for forbidden in TOOL_BEARING_MODULES:
            if name == forbidden or name.startswith(f"{forbidden}."):
                msg = f"{module.__name__} imports {name}. {THE_FAST_LANE_HAS_NOWHERE_TO_PUT_A_TOOL}"
                raise FastLaneError(msg)
