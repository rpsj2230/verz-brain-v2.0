"""A fast-lane question shape becomes a row, and the fast lane is handed nothing new.

One table in the `gate` schema, which 0001 created and 0001's downgrade owns. No function, no
trigger and no rows, so dropping the table is the whole reversal.

**Every check the constructor makes is made here too, and that is why this table exists at
all.** `brain.gate.fast_lane.FastPathRule` refuses a template with two holes, a hole naming a
slot the row does not declare, and a template that is nearly all hole. All three are our code,
and the rows that get a table into trouble are the ones that did not come through it: a seed
script, a hand-written INSERT during an incident, a backfill somebody writes next year.
`proj.record` carries its field cap twice for the same reason and says so in as many words.

The template grammar is countable rather than parsed, which is what makes those checks
expressible in SQL at all. `length(x) - length(replace(x, '{', ''))` counts one character
without a set-returning function, and `substring(template from ... for ...)` pulls the slot
name out with two `position` calls. A rule carrying a regular expression could not have been
checked here by anything, which is a second argument for the template and against the pattern.

**Nothing here grants `brain_fastlane` anything, and the absence is the leaf.** M6.1.3 says
the fast lane reaches projected tables and nothing else. 0001 gave the role `USAGE ON SCHEMA
proj` and no table privileges, and 0008 handed it `SELECT` on the one table in `proj`. This
table is configuration in `gate`: the application reads the rule set, matches in memory, and
fetches rows under the fast-lane role, so the role never needs to see a rule. Granting it one
would be the first crack in a property that is otherwise absolute, and
`brain.ops.migration_policy` now refuses a migration that opens it, in this file and in every
file written after it.

**No DELETE, and `deleted_at` instead.** "Which rule answered that question in March" is asked
after a wrong answer, not before one, and a fast-lane answer had no model in it to explain
itself afterwards. The one DELETE grant in this system belongs to `auth.directory_role_grant`
and 0006 argues for it there.

**Row-level security is enabled and the two policies are unconditional**, for the reason 0014,
0016 and 0017 give. A rule carries no audience: it is configuration for the estate, and who
may edit configuration is decided where the console decides it, against a viewer this table
knows nothing about. The absence of a DELETE policy sits underneath the missing DELETE grant,
because PostgreSQL denies what no policy admits and 0001 left no role able to bypass it.

**One live rule per template.** A partial unique index rather than a plain one, because a
retired rule and its replacement share a template and both are rows. It is deliberately
weaker than the matcher, which collapses whitespace and folds case before comparing: making
it as strong would mean the matcher's normalisation written a second time in SQL, and the
matcher already refuses an ambiguous pair by answering neither.

**Nothing imports `brain.tables`.** The predicates below are the same ones the model declares,
copied deliberately, so this migration goes on describing the database it actually built
rather than whatever the models say today. `tests/unit/test_fast_lane.py` compares the two on
rendered DDL, which is what turns the copy into a check rather than a duplication.

Task ids: M6.1.1, M6.1.3

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"
FAST_ROLE = "brain_fastlane"

#: The one table this migration builds. `downgrade` walks it in reverse, which for one table
#: is the same order and is written that way so adding a second cannot forget it.
TABLES: tuple[str, ...] = ("gate.fast_path_rule",)

#: `brain.core.envelope.OBJECT_NAME_PATTERN`, copied for the reason 0004, 0006 and 0008 give:
#: a migration describes the database it built.
NAME = "^[a-z][a-z0-9_]*$"

#: `brain.core.fast_path.MIN_TEMPLATE_CHARS` and `MAX_TEMPLATE_CHARS`, copied. The floor is
#: the literal floor plus the shortest possible hole, `{a}`.
TEMPLATE_LENGTH = "length(template) BETWEEN 15 AND 200"

#: `brain.core.fast_path.MIN_LITERAL_CHARS`, copied. The hole is everything from `{` to `}`
#: inclusive, so what is left of the template once that span is removed is the literal part.
LITERAL_IS_LONG_ENOUGH = (
    "length(template) - (position('}' in template) - position('{' in template) + 1) >= 12"
)

#: One `{` and one `}`, counted without a set-returning function so a check constraint may
#: hold it. Both are needed and neither implies the other.
ONE_SLOT_OPEN = "length(template) - length(replace(template, '{', '')) = 1"
ONE_SLOT_CLOSE = "length(template) - length(replace(template, '}', '')) = 1"

#: And in that order. `position` returns zero for an absent character, so on its own this
#: passes a template with neither brace; the two counts above are what rule that out.
SLOT_OPENS_BEFORE_IT_CLOSES = "position('{' in template) < position('}' in template)"

#: The text between the braces is the `slot` column. Checked rather than assumed, because the
#: matcher splits the template on the braces while the loader validates the `slot` name, and a
#: row where the two disagree is a rule that matches one thing and reports another.
SLOT_IS_THE_NAME_IN_THE_TEMPLATE = (
    "substring(template from position('{' in template) + 1 "
    "for position('}' in template) - position('{' in template) - 1) = slot"
)

RLS: tuple[str, ...] = (
    "ALTER TABLE gate.fast_path_rule ENABLE ROW LEVEL SECURITY",
    # SELECT, INSERT and UPDATE, matching the grants. A rule is retired by setting
    # `deleted_at`, which is an UPDATE; there is no DELETE policy and no DELETE grant, and
    # PostgreSQL denies what no policy admits.
    """
    CREATE POLICY fast_path_rule_readable ON gate.fast_path_rule
        FOR SELECT TO brain_app
        USING (true)
    """,
    """
    CREATE POLICY fast_path_rule_writable ON gate.fast_path_rule
        FOR INSERT TO brain_app
        WITH CHECK (true)
    """,
    """
    CREATE POLICY fast_path_rule_retirable ON gate.fast_path_rule
        FOR UPDATE TO brain_app
        USING (true)
        WITH CHECK (true)
    """,
)

#: No DELETE, as everywhere but 0006. And nothing for `brain_fastlane`: see the docstring.
GRANTS: tuple[str, ...] = ("GRANT SELECT, INSERT, UPDATE ON gate.fast_path_rule TO brain_app",)


def upgrade() -> None:
    # The statement names the role literally, the way 0001 through 0017 do; this keeps the
    # constant honest rather than decorative.
    assert all(APP_ROLE in statement for statement in GRANTS)
    assert all("DELETE" not in statement for statement in GRANTS)
    # And the half of M6.1.3 this file is responsible for, asserted rather than left to the
    # docstring: a rule table is not a projected table, so the fast lane is granted nothing
    # on it. `brain.ops.migration_policy` applies the same rule to every other migration.
    assert all(FAST_ROLE not in statement for statement in GRANTS + RLS)

    op.create_table(
        "fast_path_rule",
        # The operator's name for the rule, and the id in every log line. Natural rather
        # than surrogate: two rows sharing a name would both match and the lane would
        # answer neither.
        sa.Column("rule_id", sa.String(60), primary_key=True, nullable=False),
        # The question shape, with exactly one `{slot}` in it. Everything else is literal
        # and is matched literally; there is no pattern anywhere in this table.
        sa.Column("template", sa.String(200), nullable=False),
        sa.Column("slot", sa.String(60), nullable=False),
        # Which connector's records answer this and which kind, because `proj.record` is
        # keyed by both: one source's record 42 is not another's.
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("entity", sa.String(60), nullable=False),
        # The projected field the slot value is compared against, and the one that holds
        # the answer. Two columns, because "which client" and "what about them" differ.
        sa.Column("match_field", sa.String(60), nullable=False),
        sa.Column("answer_field", sa.String(60), nullable=False),
        # Who added it. A rule answers with no model in the loop and no reviewer between
        # the insert and the answer, so this is the only accountability there is.
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # Declared in the order the model declares them, so the rendered DDL is
        # character-for-character what `CreateTable` produces from the model. A comparison
        # on rendered SQL is sensitive to constraint order.
        sa.CheckConstraint(f"rule_id ~ '{NAME}'", name="rule_id_is_a_name"),
        sa.CheckConstraint(f"slot ~ '{NAME}'", name="slot_is_a_name"),
        sa.CheckConstraint(f"source ~ '{NAME}'", name="source_is_a_name"),
        sa.CheckConstraint(f"entity ~ '{NAME}'", name="entity_is_a_name"),
        sa.CheckConstraint(f"match_field ~ '{NAME}'", name="match_field_is_a_name"),
        sa.CheckConstraint(f"answer_field ~ '{NAME}'", name="answer_field_is_a_name"),
        sa.CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        sa.CheckConstraint(TEMPLATE_LENGTH, name="template_length"),
        sa.CheckConstraint(ONE_SLOT_OPEN, name="template_opens_one_hole"),
        sa.CheckConstraint(ONE_SLOT_CLOSE, name="template_closes_one_hole"),
        sa.CheckConstraint(
            SLOT_OPENS_BEFORE_IT_CLOSES, name="template_hole_is_the_right_way_round"
        ),
        sa.CheckConstraint(
            SLOT_IS_THE_NAME_IN_THE_TEMPLATE, name="template_names_the_declared_slot"
        ),
        sa.CheckConstraint(LITERAL_IS_LONG_ENOUGH, name="template_is_not_all_hole"),
        schema="gate",
    )
    # From `SoftDeleteMixin`, which declares `index=True`. Named by the metadata's `ix`
    # convention, which renders the schema and table into the name.
    op.create_index(
        "ix_gate_fast_path_rule_deleted_at", "fast_path_rule", ["deleted_at"], schema="gate"
    )
    # One live rule per template. Partial, because a retired rule and the rule that replaced
    # it share a template and both are rows.
    op.create_index(
        "uq_fast_path_rule_template_live",
        "fast_path_rule",
        ["template"],
        schema="gate",
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The policies, the indexes and the table privileges belong to the table and go with it,
    # and this migration creates no function and no trigger. `gate` is not dropped: 0001
    # created all nine schemas and 0001's downgrade owns them.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
