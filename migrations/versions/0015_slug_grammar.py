"""The slug grammar reaches PostgreSQL as the pattern Python enforces.

**Four deployed check constraints carried a regular expression nobody wrote.**

`brain.tables.gate` declared them as `CheckConstraint(f"slug ~ '{SLUG_PATTERN}'")`.
`CheckConstraint` parses its argument as `text()`, and `text()` reads `:name` as a bind
parameter. `SLUG_PATTERN` contains one colon, in `(?:`, so the non-capturing group became a
null bind and what compiled was:

    CHECK (slug ~ '^[a-z][a-z0-9]*(?NULL[a-z0-9]+)*$')

0003 copied the same unescaped text, so the model and the migration agreed with each other
and both disagreed with `SLUG_PATTERN`. That is why `tests/unit/test_tables.py`'s
model-versus-migration comparison passed for the life of these tables: it was comparing two
copies of one mistake, and a comparison of a thing against itself is the failure this
repository keeps finding in other forms.

**It is a live defect rather than a quietly loose grammar, and the difference was measured
rather than reasoned about.** Against PostgreSQL 18.6, `(?N` is not a valid ARE construct and
a CHECK is not evaluated when the table is created, so 0003 applied cleanly against an empty
schema and the first write was where it bit:

    CREATE TABLE                                    -- the constraint is accepted
    INSERT INTO gate.scope (slug, predicate) ...
    ERROR:  invalid regular expression: quantifier operand invalid

So `gate.scope`, `gate.department` and `gate.team` could not take a row at all. The same
server accepts the corrected pattern and enforces it: `web` and `client_services` are
admitted, `9web`, `web_` and `Web` are refused.

**Four constraints, not three.** `gate.department` carries `scope_slug_grammar` as well as
`slug_grammar`, and it was written from the same f-string. A fix that took the three obvious
ones would have left the department table refusing every row for the same reason.

**Dropped and recreated rather than altered**, because PostgreSQL has no `ALTER CONSTRAINT`
for a check. Recreating validates the rows already there, which here is a guarantee rather
than a risk: any row that exists was written before the constraint could be evaluated, and
the corrected pattern is the one the application layer has been enforcing all along through
`brain.core.department.SLUG_RE`, so a row that fails it was never reachable through the
model.

**The pattern is copied rather than imported**, following 0003 and the convention its
docstring gives: a migration describes the schema as it was at a moment, and one that
imported a constant would silently change meaning the day the constant changed.

Task ids: none
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

#: No table is created here. Named for symmetry with the migrations that do, and empty so the
#: model-versus-migration comparison does not look for one.
TABLES: tuple[str, ...] = ()

#: The pattern as `brain.core.department.SLUG_PATTERN` spells it, with its one colon escaped
#: so `text()` carries it through as a literal instead of reading it as a bind parameter.
#: Copied rather than imported; see the docstring.
SLUG_SQL_PATTERN = r"^[a-z][a-z0-9]*(?\:_[a-z0-9]+)*$"

#: The same pattern **without** the escape, which is what 0003 wrote and therefore what the
#: downgrade has to put back.
#:
#: Named rather than written inline in `downgrade`, because the only difference between the
#: two is one backslash and the broken one looks like the correct one with a typo. Somebody
#: tidying this file would "fix" it and turn the downgrade into a step to a schema that never
#: existed. The name is the warning; `test_the_downgrade_restores_the_broken_pattern_because_
#: that_is_what_shipped` is the enforcement.
SLUG_SQL_PATTERN_AS_0003_SHIPPED_IT = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"

#: Every constraint 0003 built from the unescaped pattern: schema, table, constraint name and
#: the column it tests. `gate.department` appears twice, and that second row is the one a fix
#: written from the obvious three would have missed.
CONSTRAINED: tuple[tuple[str, str, str, str], ...] = (
    ("gate", "scope", "slug_grammar", "slug"),
    ("gate", "department", "slug_grammar", "slug"),
    ("gate", "department", "scope_slug_grammar", "scope_slug"),
    ("gate", "team", "slug_grammar", "slug"),
)

#: What 0003 rendered, mapped to what this migration renders instead. Keyed by the whole
#: clause rather than by the pattern alone, because the two columns produce two different
#: clauses and `tests/unit/test_tables.py::as_amended` substitutes text.
#:
#: The left-hand sides are the mangled forms, which is what makes this readable as a
#: correction rather than as a widening: nobody typed `(?NULL`.
SUPERSEDES: dict[str, str] = {
    "slug ~ '^[a-z][a-z0-9]*(?NULL[a-z0-9]+)*$'": ("slug ~ '^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$'"),
    "scope_slug ~ '^[a-z][a-z0-9]*(?NULL[a-z0-9]+)*$'": (
        "scope_slug ~ '^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$'"
    ),
}


def upgrade() -> None:
    for schema, table, name, column in CONSTRAINED:
        # The bare name. Alembic applies `NAMING_CONVENTION["ck"]` on top, so passing the
        # already-prefixed `ck_scope_slug_grammar` renders
        # `ck_scope_ck_scope_slug_grammar` and the DROP names a constraint that has never
        # existed. 0007 learned this; 0011, 0012 and 0013 copied it, and so does this.
        op.drop_constraint(name, table, schema=schema, type_="check")
        op.create_check_constraint(name, table, f"{column} ~ '{SLUG_SQL_PATTERN}'", schema=schema)


def downgrade() -> None:
    """Put the broken pattern back.

    A downgrade that restored a working constraint would be a downgrade to a schema that
    never existed, and the whole value of the history is that each revision describes what
    was really there. So this restores the mangled form, and the table it is applied to
    stops accepting writes again, which is what 0014 and earlier actually shipped.
    """
    for schema, table, name, column in CONSTRAINED:
        op.drop_constraint(name, table, schema=schema, type_="check")
        op.create_check_constraint(
            name,
            table,
            f"{column} ~ '{SLUG_SQL_PATTERN_AS_0003_SHIPPED_IT}'",
            schema=schema,
        )
