"""The two ends of a scope predicate: where it arrives from, and where it goes.

A scope is authored in a console, stored as jsonb, read back by a worker that never met
the author, and finally becomes the WHERE clause deciding what a person may read. Four
things go wrong along that path and none of them show up in a diff.

**A stored predicate can arrive in a shape the two evaluators read differently.** A clause
of `op=IN, value="abc"` is refused by `Clause.matches`, which wants a tuple, and admitted
by SQL rendering, where `list("abc")` becomes `["a", "b", "c"]`. One scope, two answers,
and the SQL answer is the wider one. `assert_conjunctive` refuses any clause whose value
shape disagrees with its operator, so such a scope reaches neither evaluator.

**A prefix can carry a wildcard.** SQL LIKE reads `%` and `_`; Python `str.startswith`
does not. A stored prefix of `web_` narrows in Python and widens in SQL. Every value bound
here is escaped and the escape character is declared, so the two agree on every input.

**A predicate document can smuggle in disjunction.** Conjunction-only is the property that
makes a grant set readable by inspection rather than by solving for satisfiability. The
document grammar has no `or` and no `not`, and names them explicitly when refusing, so an
author learns the rule rather than guessing at a parse error.

**An empty predicate and an impossible one look alike and mean opposite things.** No
clauses means unrestricted. Contradictory clauses mean nothing at all. Compiling either
one to a missing WHERE clause is the difference between a person seeing nothing and a
person seeing the whole company, so both are named here rather than left to the caller.

Nothing here performs I/O or imports a database driver. It emits a fragment and its bound
parameters as data; binding them is the caller's job.

Task ids: M2.1.2, M2.1.3
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from brain.core.scope import Clause, Op, Scope

#: SQL identifiers cannot be parameterised, so they are constrained rather than quoted.
#: Same reasoning as the field pattern on `Clause`: a name that cannot contain a quote
#: cannot close one.
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Document keys that would introduce disjunction or negation. They are refused by name
#: rather than by falling through to "unknown field", because an author who writes `$or`
#: has a model of the grammar in their head and deserves to be told it is wrong.
DISJUNCTION_KEYS = frozenset(
    {
        "or",
        "$or",
        "not",
        "$not",
        "nor",
        "$nor",
        "any_of",
        "anyof",
        "one_of",
        "oneof",
        "either",
        "except",
        "unless",
        "union",
    }
)

#: Matcher object keys the document grammar understands. Anything else is refused.
MATCHER_KEYS = frozenset({"prefix", "any"})

#: LIKE needs an escape character and Postgres defaults to backslash. It is declared
#: explicitly in the rendered fragment anyway, because the default depends on a server
#: setting and a permission predicate should not.
LIKE_ESCAPE = "\\"


class PredicateRefusedError(Exception):
    """Raised when a predicate could widen, diverge, or match nothing.

    Deliberately not part of the user-facing error taxonomy in `brain.core.errors`. Nobody
    asking a question ever sees this: it is an authoring-time and load-time failure, and it
    should stop a scope from being saved rather than degrade an answer.
    """


@dataclass(frozen=True)
class GrammarViolation:
    """One reason a predicate is not allowed."""

    where: str
    reason: str

    def __str__(self) -> str:
        return f"{self.where}: {self.reason}"


# --------------------------------------------------------------- the grammar
def check_grammar(scope: Scope) -> list[GrammarViolation]:
    """Every reason this scope may not be used, not just the first.

    Returning one at a time turns authoring a scope into a guessing game, where each fix
    reveals the next objection. The same reasoning as `projection.check_projection`.
    """
    violations: list[GrammarViolation] = []
    for clause in scope.clauses:
        violations.extend(_check_clause(clause))
    return violations


def _check_clause(clause: Clause) -> list[GrammarViolation]:
    """The value shape must match the operator, on both evaluators.

    This is the whole point of the validator. `Clause` types `value` as
    `str | tuple[str, ...] | None` for all four operators, so every mismatched pairing
    constructs cleanly and then behaves differently in Python and in SQL.
    """
    out: list[GrammarViolation] = []
    where = f"{clause.field} {clause.op}"

    match clause.op:
        case Op.ANY:
            if clause.value is not None:
                out.append(
                    GrammarViolation(
                        where,
                        "carries a value, which nothing reads; write it as a real test "
                        "or drop the clause",
                    )
                )
        case Op.EQ:
            if not isinstance(clause.value, str):
                out.append(
                    GrammarViolation(where, "needs a string value; anything else never matches")
                )
            elif clause.value == "":
                out.append(
                    GrammarViolation(where, "has an empty value, which no projected row carries")
                )
        case Op.IN:
            if not isinstance(clause.value, tuple):
                # The dangerous case: `list("abc")` is three members in SQL and no match
                # at all in Python, so a string here is wider on the side that counts.
                out.append(
                    GrammarViolation(
                        where,
                        "needs a tuple of strings; a bare string becomes one member per "
                        "character in SQL and matches nothing in Python",
                    )
                )
            elif not clause.value:
                out.append(
                    GrammarViolation(where, "has an empty member list, so it can never match")
                )
            elif any(not isinstance(v, str) or v == "" for v in clause.value):
                out.append(GrammarViolation(where, "has a member that is not a non-empty string"))
        case Op.PREFIX:
            if not isinstance(clause.value, str) or clause.value == "":
                out.append(
                    GrammarViolation(
                        where,
                        "needs a non-empty string; an empty prefix matches every row and "
                        "is a scope written as a no-op",
                    )
                )
    return out


def assert_conjunctive(scope: Scope) -> None:
    """Raise with every violation at once. Called wherever a scope enters the system.

    The name is the invariant: this is what stands between a stored predicate and a scope
    that could widen. There is no disjunction to check for, because `Op` has none; what is
    checked is everything that would let a clause mean one thing to `Clause.matches` and
    another thing to `compile_where`.
    """
    violations = check_grammar(scope)
    if not violations:
        return
    listed = "\n".join(f"  - {v}" for v in violations)
    msg = f"predicate refused:\n{listed}"
    raise PredicateRefusedError(msg)


# ------------------------------------------------------- the document form
def parse_predicate(document: object) -> Scope:
    """Turn the stored jsonb form into a validated Scope.

    Typed as `object` rather than as a mapping because this is the boundary: the argument
    is whatever the jsonb column held, and a column that used to hold an object can hold a
    list the moment a migration or a hand-written UPDATE says so. Narrowing it here means
    the wrong shape is a refusal with a sentence in it rather than an AttributeError.

    The document is an object mapping a field to a matcher, and the object itself is the
    conjunction. Four matcher forms, and no fifth:

    - `"web"`                 field equals web
    - `["web", "sales"]`      field is one of these
    - `{"prefix": "web."}`    field starts with this
    - `{"any": true}`         field is not tested, only declared

    Values must already be strings. Coercing them here looks helpful and is not: jsonb
    `->>` renders a boolean as `true` and Python `str()` renders it as `True`, so a
    coerced boolean is a predicate that means different things on the two evaluators. The
    author is told to store a string instead.
    """
    if not isinstance(document, Mapping):
        msg = "a predicate is a json object mapping a field to a matcher"
        raise PredicateRefusedError(msg)

    clauses: list[Clause] = []
    violations: list[GrammarViolation] = []

    for key, matcher in document.items():
        name = str(key)
        if name.lower() in DISJUNCTION_KEYS:
            violations.append(
                GrammarViolation(
                    name,
                    "disjunction and negation are not in the grammar; two narrow grants "
                    "must never combine into a wider one",
                )
            )
            continue
        clause_or_violation = _matcher_to_clause(name, matcher)
        if isinstance(clause_or_violation, GrammarViolation):
            violations.append(clause_or_violation)
        else:
            clauses.append(clause_or_violation)

    if violations:
        listed = "\n".join(f"  - {v}" for v in violations)
        msg = f"predicate refused:\n{listed}"
        raise PredicateRefusedError(msg)

    scope = Scope(clauses=tuple(clauses))
    assert_conjunctive(scope)
    return scope


def _matcher_to_clause(name: str, matcher: object) -> Clause | GrammarViolation:
    try:
        if isinstance(matcher, str):
            return Clause(field=name, op=Op.EQ, value=matcher)
        if isinstance(matcher, list | tuple):
            members = tuple(matcher)
            if any(not isinstance(m, str) for m in members):
                return GrammarViolation(name, "every member of a list matcher must be a string")
            return Clause(field=name, op=Op.IN, value=members)
        if isinstance(matcher, Mapping):
            return _object_matcher_to_clause(name, matcher)
    except ValidationError as exc:
        # Pydantic's message names the pattern, which is the useful half; the field name is
        # the other half and it is not in there.
        return GrammarViolation(name, f"is not a usable field name or value ({exc.error_count()})")
    return GrammarViolation(
        name,
        "must be a string, a list of strings, {'prefix': ...} or {'any': true}; numbers "
        "and booleans must be stored as strings, because jsonb renders them as text and "
        "Python does not render them the same way",
    )


def _object_matcher_to_clause(name: str, matcher: Mapping[Any, Any]) -> Clause | GrammarViolation:
    keys = {str(k) for k in matcher}
    unknown = keys - MATCHER_KEYS
    if unknown:
        return GrammarViolation(name, f"unknown matcher key(s) {sorted(unknown)}")
    if len(keys) != 1:
        return GrammarViolation(name, "a matcher object carries exactly one key")
    if "prefix" in keys:
        value = matcher["prefix"]
        if not isinstance(value, str) or value == "":
            return GrammarViolation(name, "a prefix matcher needs a non-empty string")
        return Clause(field=name, op=Op.PREFIX, value=value)
    if matcher["any"] is not True:
        return GrammarViolation(name, "an any matcher is written {'any': true}")
    return Clause(field=name, op=Op.ANY)


def to_predicate(scope: Scope) -> dict[str, Any]:
    """Render a Scope back to the stored form.

    The round trip has to be lossless in both directions or the console shows an author
    something other than what they saved. Two clauses on the same field cannot be
    represented, because the document is keyed by field; that is a limit of the stored
    form and not of `Scope`, and it is why this raises rather than silently dropping one.
    """
    out: dict[str, Any] = {}
    for clause in scope.clauses:
        if clause.field in out:
            msg = (
                f"{clause.field} carries more than one clause, which the document form "
                "cannot hold; store the composed scope by reference instead"
            )
            raise PredicateRefusedError(msg)
        match clause.op:
            case Op.ANY:
                out[clause.field] = {"any": True}
            case Op.EQ:
                out[clause.field] = clause.value
            case Op.IN:
                out[clause.field] = list(clause.value or ())
            case Op.PREFIX:
                out[clause.field] = {"prefix": clause.value}
    return out


# ------------------------------------------------------------ satisfiability
def is_unsatisfiable(scope: Scope) -> bool:
    """True when no row can satisfy this scope, whatever the data.

    Composition intersects, so a person holding `department = sales` and `department = web`
    as two separate grants ends up with a scope that matches nothing. That is the correct
    conservative answer and it is also indistinguishable, at the far end of a query, from
    a permission bug or an empty table. Naming it here lets a caller say "your scopes do
    not overlap" instead of "no results".

    Sound, not complete: when it answers True the scope really is impossible, and when it
    answers False the scope may still return nothing for reasons in the data. That is the
    safe direction, since the only thing this decides is whether to bother asking.
    """
    by_field: dict[str, list[Clause]] = {}
    for clause in scope.clauses:
        if clause.op is Op.ANY:
            continue
        by_field.setdefault(clause.field, []).append(clause)
    return any(_field_is_unsatisfiable(clauses) for clauses in by_field.values())


def _field_is_unsatisfiable(clauses: Sequence[Clause]) -> bool:
    equals = {c.value for c in clauses if c.op is Op.EQ and isinstance(c.value, str)}
    if len(equals) > 1:
        return True
    prefixes = [c.value for c in clauses if c.op is Op.PREFIX and isinstance(c.value, str)]
    for one in prefixes:
        for other in prefixes:
            # Every value satisfying both must have one prefix as a prefix of the other.
            if not one.startswith(other) and not other.startswith(one):
                return True

    candidates: set[str] | None = None
    for clause in clauses:
        if clause.op is Op.IN and isinstance(clause.value, tuple):
            members = set(clause.value)
            candidates = members if candidates is None else candidates & members
    if candidates is not None:
        if not candidates:
            return True
        candidates = {v for v in candidates if all(v.startswith(p) for p in prefixes)}
        if not candidates:
            return True

    if equals:
        only = next(iter(equals))
        if candidates is not None and only not in candidates:
            return True
        if any(not only.startswith(p) for p in prefixes):
            return True
    return False


def clause_entails(narrow: Clause, wide: Clause) -> bool:
    """True when every row matching `narrow` also matches `wide`.

    Sound and incomplete, and the incompleteness is deliberate: a False answer costs a
    caller a shortcut, while a wrong True answer would let a narrow grant stand in for a
    wide one. Only one of those is a security bug, so the analysis is written to fail
    towards False.
    """
    if wide.op is Op.ANY:
        return True
    if narrow.field != wide.field:
        return False
    if narrow.op is Op.ANY:
        return False

    match (narrow.op, wide.op):
        case (Op.EQ, Op.EQ):
            return narrow.value == wide.value
        case (Op.EQ, Op.IN):
            return isinstance(wide.value, tuple) and narrow.value in wide.value
        case (Op.EQ, Op.PREFIX):
            return (
                isinstance(narrow.value, str)
                and isinstance(wide.value, str)
                and narrow.value.startswith(wide.value)
            )
        case (Op.IN, Op.IN):
            return (
                isinstance(narrow.value, tuple)
                and isinstance(wide.value, tuple)
                and set(narrow.value) <= set(wide.value)
            )
        case (Op.IN, Op.EQ):
            return isinstance(narrow.value, tuple) and set(narrow.value) == {wide.value}
        case (Op.IN, Op.PREFIX):
            return (
                isinstance(narrow.value, tuple)
                and isinstance(wide.value, str)
                and all(v.startswith(wide.value) for v in narrow.value)
            )
        case (Op.PREFIX, Op.PREFIX):
            return (
                isinstance(narrow.value, str)
                and isinstance(wide.value, str)
                and narrow.value.startswith(wide.value)
            )
    # A prefix admits unboundedly many values, so it never entails an equality or a
    # membership test.
    return False


def scope_narrows(narrow: Scope, wide: Scope) -> bool:
    """True when every row matching `narrow` also matches `wide`.

    Both sides are conjunctions, so it is enough that each clause of the wider scope is
    entailed by some clause of the narrower one. An unrestricted `wide` is vacuously
    satisfied, which is right: everything narrows the unrestricted scope.
    """
    return all(any(clause_entails(n, w) for n in narrow.clauses) for w in wide.clauses)


# ------------------------------------------------------------- compilation
@dataclass(frozen=True)
class ColumnLayout:
    """Where a predicate field lives in a particular table.

    Two shapes, because both exist in this schema. A field in `promoted` is a real column
    and compiles to one, which is what makes an index usable; everything else compiles to
    a jsonb lookup. The architecture's note about pgvector filtering after the index scan
    is the same problem one layer up: a scope that cannot use an index turns into a scan
    that quietly returns thin results.
    """

    jsonb_column: str = "row_data"
    promoted: frozenset[str] = field(default_factory=frozenset)
    alias: str = ""

    def __post_init__(self) -> None:
        for name in (self.jsonb_column, *sorted(self.promoted)):
            if not IDENT_RE.match(name):
                msg = f"{name!r} is not a plain lowercase identifier and cannot be a column"
                raise PredicateRefusedError(msg)
        if self.alias and not IDENT_RE.match(self.alias):
            msg = f"{self.alias!r} is not a plain lowercase identifier and cannot be an alias"
            raise PredicateRefusedError(msg)

    def column_for(self, name: str) -> str:
        prefix = f"{self.alias}." if self.alias else ""
        if name in self.promoted:
            return f"{prefix}{name}"
        # The key is looked up literally, dots included, because `Clause.matches` does
        # `row.get(name)` on a flat dict. Rendering `a.b` as a jsonb path here would make
        # the SQL evaluator read a nested object the Python one never looks at.
        return f"{prefix}{self.jsonb_column} ->> '{name}'"


@dataclass(frozen=True)
class CompiledPredicate:
    """A WHERE fragment and its bound parameters, as data.

    `certainly_empty` carries the one fact the fragment cannot: `FALSE` is a correct
    compilation of an impossible scope, and a caller that runs it gets an empty result set
    with no way to tell it apart from a table with nothing in it.
    """

    where: str
    params: dict[str, Any]
    certainly_empty: bool = False

    def and_(self, other: CompiledPredicate) -> CompiledPredicate:
        """Conjunction, mirroring `Scope.intersect` at the SQL level."""
        collisions = sorted(set(self.params) & set(other.params))
        if collisions:
            # Silently merging would bind one scope's value into the other's placeholder,
            # which is a permission bug that looks like a typo.
            msg = (
                f"parameter name(s) {collisions} are used by both fragments; "
                "give them distinct prefixes"
            )
            raise PredicateRefusedError(msg)
        return CompiledPredicate(
            where=f"({self.where} AND {other.where})",
            params={**self.params, **other.params},
            certainly_empty=self.certainly_empty or other.certainly_empty,
        )


def _escape_like(value: str) -> str:
    """Neutralise LIKE's wildcards so a prefix means what `str.startswith` means."""
    out = value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        out = out.replace(wildcard, f"{LIKE_ESCAPE}{wildcard}")
    return out


def compile_where(
    scope: Scope,
    layout: ColumnLayout | None = None,
    param_prefix: str = "s",
) -> CompiledPredicate:
    """Compile a scope to a parameterised WHERE fragment.

    Values are never interpolated, at any operator. Identifiers are validated instead,
    because they cannot be parameterised at all and quoting them would only move the
    problem to the quote character.

    The grammar is checked first, so nothing that would mean two different things on the
    two evaluators can be compiled at all.
    """
    if not IDENT_RE.match(param_prefix):
        msg = f"{param_prefix!r} is not a usable parameter prefix"
        raise PredicateRefusedError(msg)
    assert_conjunctive(scope)
    columns = layout if layout is not None else ColumnLayout()

    if scope.is_unrestricted():
        return CompiledPredicate(where="TRUE", params={})
    if is_unsatisfiable(scope):
        # FALSE rather than a raise: an impossible scope is a correct, fail-closed answer,
        # and refusing here would turn a narrow principal into a 500 rather than an empty
        # list. The flag is how the caller says why it was empty.
        return CompiledPredicate(where="FALSE", params={}, certainly_empty=True)

    fragments: list[str] = []
    params: dict[str, Any] = {}
    for i, clause in enumerate(scope.clauses):
        name = f"{param_prefix}{i}"
        column = columns.column_for(clause.field)
        match clause.op:
            case Op.ANY:
                fragments.append("TRUE")
            case Op.EQ:
                fragments.append(f"{column} = :{name}")
                params[name] = clause.value
            case Op.IN:
                fragments.append(f"{column} = ANY(:{name})")
                params[name] = list(clause.value or ())
            case Op.PREFIX:
                fragments.append(f"{column} LIKE :{name} ESCAPE '{LIKE_ESCAPE}'")
                params[name] = f"{_escape_like(str(clause.value))}%"
    return CompiledPredicate(where="(" + " AND ".join(fragments) + ")", params=params)
