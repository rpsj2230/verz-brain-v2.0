"""`gate.fast_path_rule`: the row that makes a fast-lane question shape configuration.

`brain.gate.classify` carries four question shapes as regular expressions in a source file,
which means the fifth is a deployment. This is where the fifth goes instead. The argument for
the shape of a rule is in `brain.gate.fast_lane`; what this module adds is the half a
constructor cannot do, which is refusing the row that arrived some other way.

**The template checks are here as well as in the type, and that is the point of the table.**
`FastPathRule` refuses a second hole, a hole naming a different slot and a template that is
nearly all hole. All three of those are our code, and the rows that get a table into trouble
are the ones that did not come through it: a seed script, a hand-written INSERT during an
incident, a backfill somebody writes next year. `proj.record` carries its field cap twice for
exactly this reason and says so. Every one of the four checks is expressible in SQL because
the template grammar is countable rather than parsed: one `{`, one `}`, in that order, and the
text between them equal to the `slot` column.

**There is no column a pattern, an expression or a callable could live in.** That is the
whole of M6.1.1 arriving in the schema. The columns are a template, a slot name, two field
names, a source and an entity, all held to the object-name grammar, plus who added the rule.
A rule table with a `handler` column would be a deployment mechanism wearing a configuration
table's clothes, and it would run inside the one lane with no model downstream to notice.

**No DELETE, and `deleted_at` instead.** A rule that stops being right is retired rather than
removed, for the reason every other table here is: "which rule answered that question in
March" is asked after a wrong answer, not before one, and a rule table nobody can reconstruct
makes a fast-lane answer unexplainable. The one DELETE grant in this system belongs to
`auth.directory_role_grant` and 0006 argues for it there.

**The fast lane is granted nothing on this table, and the absence is deliberate.** The rule
set is configuration and it is read by the application, which then matches in memory and
fetches rows under `brain_fastlane`. Granting the fast-lane role a table outside `proj` would
break the one property M6.1.3 asks for, which is that the role restricted to answering
without a model can reach projected tables and nothing else. See
`brain.ops.migration_policy.THE_FAST_LANE_REACHES_PROJECTED_TABLES_AND_NOTHING_ELSE`, which
refuses a migration that grants it anything else.

**`created_by` is on the row rather than left to the ledger.** A rule answers a question with
no model in the loop and no reviewer between the insert and the answer, so who added it is the
only accountability there is. It is an operator identifier, not a person's data, and it is not
a second copy of anything: nothing else records who wrote a configuration row.

Rejected: a `priority` column to break ties between two rules matching one question. It would
make an operator error survivable and therefore permanent, and the survivable form of that
error is a rule answering a question a different rule was written for. `brain.gate.fast_lane`
refuses an ambiguous match outright and the answer lane takes the question, which costs one
model call and makes the mistake visible.

Rejected: a `pattern` column beside the template, for the shapes a template cannot express.
There is no such thing as a bounded caller-supplied pattern, and the lane it would run in has
nothing downstream able to notice a wrong answer.

Task ids: M6.1.1
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from brain.core.envelope import OBJECT_NAME_PATTERN
from brain.core.fast_path import (
    MAX_TEMPLATE_CHARS,
    MIN_LITERAL_CHARS,
    MIN_TEMPLATE_CHARS,
    SLOT_CLOSE,
    SLOT_OPEN,
)
from brain.db import Base, SoftDeleteMixin, TimestampMixin

#: The object-name grammar, which every name column here is held to. Sixty characters,
#: matching `brain.core.envelope.Entity.entity` and `proj.record`'s own columns, so a name
#: the types accept cannot be one a column truncates.
NAME_CHARS = 60

#: Who added the rule. The same width as every locally minted identifier in this system,
#: `brain.tables.identity.PRINCIPAL_ID_CHARS`, restated rather than imported because an
#: operator identifier and a principal id moving together is a coincidence, not a rule.
CREATED_BY_CHARS = 128

#: One `{`, counted the way a check constraint can count: length minus the length with the
#: character removed. Generated from the constants the type uses rather than written out, for
#: the reason `brain.tables.projection.FIELDS_WITHIN_THE_CAP` gives about its own cap.
ONE_SLOT_OPEN = f"length(template) - length(replace(template, '{SLOT_OPEN}', '')) = 1"
ONE_SLOT_CLOSE = f"length(template) - length(replace(template, '{SLOT_CLOSE}', '')) = 1"

#: And in that order. `position` returns zero when the character is absent, so this would be
#: satisfied by a template with neither; the two counts above are what rule that out, and all
#: three are needed rather than any one of them.
SLOT_OPENS_BEFORE_IT_CLOSES = (
    f"position('{SLOT_OPEN}' in template) < position('{SLOT_CLOSE}' in template)"
)

#: The text between the braces is the `slot` column. The database checks the template against
#: the declared slot rather than taking the template's word for what it declares, because the
#: two are read by different code: the matcher splits on the braces and the loader validates
#: the name.
SLOT_IS_THE_NAME_IN_THE_TEMPLATE = (
    "substring(template from position('" + SLOT_OPEN + "' in template) + 1 "
    "for position('" + SLOT_CLOSE + "' in template) "
    "- position('" + SLOT_OPEN + "' in template) - 1) = slot"
)

#: Enough literal text that the template is a question shape rather than a wildcard. The hole
#: is everything from `{` to `}` inclusive, so the literal length is what is left of the
#: template once that span is removed. See
#: `brain.core.fast_path.A_TEMPLATE_THAT_IS_ALL_HOLE_MATCHES_EVERY_QUESTION`.
LITERAL_IS_LONG_ENOUGH = (
    "length(template) - (position('" + SLOT_CLOSE + "' in template) "
    "- position('" + SLOT_OPEN + "' in template) + 1) >= " + str(MIN_LITERAL_CHARS)
)

#: The bounds the type declares, restated as a constraint. A template under the floor cannot
#: hold a hole and enough literal text, and one over the ceiling is a corpus.
TEMPLATE_LENGTH = f"length(template) BETWEEN {MIN_TEMPLATE_CHARS} AND {MAX_TEMPLATE_CHARS}"


class FastPathRuleRow(TimestampMixin, SoftDeleteMixin, Base):
    """`gate.fast_path_rule`. One fast-lane question shape, as configuration (M6.1.1).

    Named `FastPathRuleRow` rather than `FastPathRule` because
    `brain.gate.fast_lane.FastPathRule` is the validated value the matcher runs on, and two
    classes one word apart in sibling packages is an import somebody eventually gets wrong.
    The way you find out is a rule reaching the matcher without passing the validator that
    refuses a template naming a slot it does not declare.

    The columns are exactly `brain.gate.fast_lane.DECLARED_FIELDS` plus provenance. That is
    not a coincidence and it is not enforced by anything at run time: the loader reads the
    fields it names, so a column added here is inert until somebody adds it there too, which
    is the property that keeps a configuration table from becoming an interface.
    """

    __tablename__ = "fast_path_rule"

    #: The operator's name for this rule, and the id in every log line. Natural rather than
    #: surrogate: a rule is known by its name, and two rows with the same name would both
    #: match and the lane would refuse to answer either.
    rule_id: Mapped[str] = mapped_column(String(NAME_CHARS), primary_key=True)

    #: The question shape, with exactly one `{slot}` in it.
    template: Mapped[str] = mapped_column(String(MAX_TEMPLATE_CHARS), nullable=False)

    #: The name inside the braces, carried separately so the constraint above can compare
    #: the template against it.
    slot: Mapped[str] = mapped_column(String(NAME_CHARS), nullable=False)

    #: Which connector's records answer this, and which kind of record. Both, because
    #: `proj.record` is keyed by both.
    source: Mapped[str] = mapped_column(String(NAME_CHARS), nullable=False)
    entity: Mapped[str] = mapped_column(String(NAME_CHARS), nullable=False)

    #: The projected field the slot value is compared against, and the one holding the answer.
    match_field: Mapped[str] = mapped_column(String(NAME_CHARS), nullable=False)
    answer_field: Mapped[str] = mapped_column(String(NAME_CHARS), nullable=False)

    #: Who added it. See the module docstring: a rule answers with no model in the loop.
    created_by: Mapped[str] = mapped_column(String(CREATED_BY_CHARS), nullable=False)

    __table_args__ = (
        CheckConstraint(f"rule_id ~ '{OBJECT_NAME_PATTERN}'", name="rule_id_is_a_name"),
        CheckConstraint(f"slot ~ '{OBJECT_NAME_PATTERN}'", name="slot_is_a_name"),
        CheckConstraint(f"source ~ '{OBJECT_NAME_PATTERN}'", name="source_is_a_name"),
        CheckConstraint(f"entity ~ '{OBJECT_NAME_PATTERN}'", name="entity_is_a_name"),
        CheckConstraint(f"match_field ~ '{OBJECT_NAME_PATTERN}'", name="match_field_is_a_name"),
        CheckConstraint(f"answer_field ~ '{OBJECT_NAME_PATTERN}'", name="answer_field_is_a_name"),
        CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        CheckConstraint(TEMPLATE_LENGTH, name="template_length"),
        CheckConstraint(ONE_SLOT_OPEN, name="template_opens_one_hole"),
        CheckConstraint(ONE_SLOT_CLOSE, name="template_closes_one_hole"),
        CheckConstraint(SLOT_OPENS_BEFORE_IT_CLOSES, name="template_hole_is_the_right_way_round"),
        CheckConstraint(SLOT_IS_THE_NAME_IN_THE_TEMPLATE, name="template_names_the_declared_slot"),
        CheckConstraint(LITERAL_IS_LONG_ENOUGH, name="template_is_not_all_hole"),
        # One live rule per template. Deliberately weaker than the matcher, which compares
        # templates with whitespace collapsed and case folded, so this refuses the exact
        # duplicate and the matcher still refuses the pair that differ only in spacing. A
        # constraint that tried to be as strong would need the matcher's normalisation in
        # SQL, which is the second implementation this repository keeps refusing to write.
        Index(
            "uq_fast_path_rule_template_live",
            "template",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "gate"},
    )
