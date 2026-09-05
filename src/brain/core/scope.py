"""Where a capability applies.

A Scope is a row predicate, stored as jsonb and evaluated both in Python (for local
objects) and in SQL (for queries). It composes by conjunction only.

That restriction is the whole design. Disjunction would let two narrow grants combine
into a wider one, which means you could never answer "what can this person see?" by
reading their grants — you would have to solve a satisfiability problem. Conjunction-only
means composing scopes can only ever narrow, so the reachable set of any grant set is
computable by inspection.

Task ids: M0.2.2
"""

from __future__ import annotations

import enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Op(enum.StrEnum):
    """The predicate grammar. Deliberately small.

    There is no NOT and no OR. Adding either would break the narrowing guarantee that
    `Scope.intersect` depends on.
    """

    EQ = "eq"
    IN = "in"
    PREFIX = "prefix"
    ANY = "any"


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a prefix means the same thing in SQL as in Python.

    Backslash first, or the escapes added afterwards would themselves be escaped.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Clause(BaseModel):
    """One field test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.]*$")
    op: Op
    value: str | tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _value_shape_cannot_make_sql_wider_than_python(self) -> Self:
        """Refuse only the shapes where the two evaluators disagree, and nothing else.

        This is a narrow rule on purpose. Plenty of odd clauses are merely useless: `EQ`
        with a None value, or an empty `IN`, match nothing in Python and nothing in SQL.
        They agree, and they fail closed, so refusing them here would be a different job
        (authoring-time sanity, which `scope_sql` does) and would make them impossible to
        write a test about.

        Two shapes genuinely diverge, and in both the SQL side is the wider one:

        - `IN` given a bare string. `matches` requires a tuple and admits nothing, while
          `to_sql` calls `list("abc")` and admits "a", "b" and "c" as three values.
        - `PREFIX` given a non-string. `matches` admits nothing, while `to_sql` renders
          `str(None)` and produces `LIKE 'None%'`, which matches any row whose value
          starts with "None".
        """
        if self.op is Op.IN and not isinstance(self.value, tuple):
            msg = (
                f"an IN clause needs a tuple, not {type(self.value).__name__}; "
                "a bare string is admitted character by character in SQL"
            )
            raise ValueError(msg)
        if self.op is Op.PREFIX and not isinstance(self.value, str):
            msg = (
                f"a PREFIX clause needs a string, not {type(self.value).__name__}; "
                "anything else is rendered into the LIKE pattern and matches rows"
            )
            raise ValueError(msg)
        return self

    def matches(self, row: dict[str, Any]) -> bool:
        if self.op is Op.ANY:
            return True
        actual = row.get(self.field)
        if actual is None:
            # Absent is not permitted. A missing field must never satisfy a predicate,
            # or a partially-projected row would widen access by omission.
            return False
        actual_s = str(actual)
        if self.op is Op.EQ:
            return actual_s == self.value
        if self.op is Op.IN:
            return isinstance(self.value, tuple) and actual_s in self.value
        return isinstance(self.value, str) and actual_s.startswith(self.value)

    def to_sql(self, param_prefix: str) -> tuple[str, dict[str, Any]]:
        """Render to a parameterised SQL fragment. Never interpolates a value.

        The rule this has to satisfy is stronger than "no injection": the SQL must admit
        exactly the rows `matches` admits. Anywhere the two disagree, the SQL side is the
        one that runs against the whole table.
        """
        col = f"row_data ->> '{self.field}'"
        match self.op:
            case Op.ANY:
                return "TRUE", {}
            case Op.EQ:
                return f"{col} = :{param_prefix}", {param_prefix: self.value}
            case Op.IN:
                return f"{col} = ANY(:{param_prefix})", {param_prefix: list(self.value or ())}
            case Op.PREFIX:
                # LIKE reads % and _ as wildcards; str.startswith does not. Without
                # escaping, a stored prefix of `web_` narrows in Python and widens in SQL,
                # matching `webXnorth` as well as `web_north`. Escape first, then say so
                # with ESCAPE, because the default escape character is backslash only by
                # convention and not in every configuration.
                return (
                    f"{col} LIKE :{param_prefix} ESCAPE '\\'",
                    {param_prefix: f"{_escape_like(str(self.value))}%"},
                )


class Scope(BaseModel):
    """A conjunction of clauses. Empty means unrestricted; that is only ever legitimate
    on a grant that was explicitly written as company-wide.

    Clauses are normalised on construction: deduplicated and sorted. Conjunction is both
    commutative and idempotent, so this changes no meaning — but it makes two scopes that
    admit the same rows serialise identically, which `EntitlementSet.ent_hash` depends on.
    Without it, intersecting a scope with itself yields duplicate clauses and a different
    hash, so the same caller would miss their own cache entry and appear in traces as a
    different principal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    clauses: tuple[Clause, ...] = ()

    @field_validator("clauses")
    @classmethod
    def _normalise(cls, v: tuple[Clause, ...]) -> tuple[Clause, ...]:
        seen: dict[tuple[str, str, str], Clause] = {}
        for c in v:
            seen[(c.field, str(c.op), repr(c.value))] = c
        return tuple(seen[k] for k in sorted(seen))

    @classmethod
    def unrestricted(cls) -> Self:
        return cls(clauses=())

    @classmethod
    def department(cls, name: str) -> Self:
        return cls(clauses=(Clause(field="department", op=Op.EQ, value=name),))

    def matches(self, row: dict[str, Any]) -> bool:
        return all(c.matches(row) for c in self.clauses)

    def intersect(self, other: Scope) -> Scope:
        """Conjunction. The result can only be narrower than either input, never wider —
        which is the property the whole permission model rests on."""
        return Scope(clauses=self.clauses + other.clauses)

    def to_sql(self, param_prefix: str = "s") -> tuple[str, dict[str, Any]]:
        if not self.clauses:
            return "TRUE", {}
        frags: list[str] = []
        params: dict[str, Any] = {}
        for i, c in enumerate(self.clauses):
            frag, p = c.to_sql(f"{param_prefix}{i}")
            frags.append(frag)
            params.update(p)
        return "(" + " AND ".join(frags) + ")", params

    def is_unrestricted(self) -> bool:
        return len(self.clauses) == 0 or all(c.op is Op.ANY for c in self.clauses)
