"""The template grammar a fast-path rule is written in, and the floor under it.

A fast-path rule says which question it answers, and the whole of M6.1.1 is that it says so
as data rather than as code. The grammar it says it in is one sentence: **a template is
literal text with exactly one `{slot}` in it**, and every character outside the braces is
matched exactly. `brain.gate.fast_lane` argues at length for why that is a template and not a
regular expression; what lives here is the part two other modules both need to agree on.

Here rather than in `brain.gate.fast_lane`, which holds the matcher, or in
`brain.tables.fast_lane`, which holds the row. Both need these numbers: the matcher validates
a rule on the way in and the table refuses the row that arrived some other way, and the two
must enforce the same grammar or the second is decoration. `brain.core.projection` holds
`MAX_PROJECTED_FIELDS` for `brain.tables.projection` to generate its check constraint from,
and this is the same arrangement for the same reason. The direct import between the gate and
the table was tried first and is a cycle: the gate reaches the row plane, the row plane
imports a table, and importing a table imports the package that registers all of them.

**The floor on the literal part is the only interesting number here.** A template of
`{client}` is not a question shape, it is a wildcard that answers any two-word question from
a projected field, in the one lane with no model downstream able to notice. Twelve characters
is about three short words, which is the shortest thing anybody would recognise as a question.
The figure is arbitrary in the way every threshold is; what is not arbitrary is that there
has to be one.

Task ids: M6.1.1
"""

from __future__ import annotations

from typing import Final

#: The one hole a template may have. Two constants rather than a pair, so the counting rules
#: in `brain.tables.fast_lane` can be generated one brace at a time.
SLOT_OPEN: Final = "{"
SLOT_CLOSE: Final = "}"

#: How much literal text a template must carry outside its hole.
MIN_LITERAL_CHARS: Final = 12

#: The longest template accepted. Long enough for any question somebody would write a rule
#: for, short enough that a rule table cannot quietly become a corpus.
MAX_TEMPLATE_CHARS: Final = 200

#: And the shortest, which is the literal floor plus the smallest hole that can exist, `{a}`.
MIN_TEMPLATE_CHARS: Final = MIN_LITERAL_CHARS + 3

#: Why a template made almost entirely of hole is refused at construction.
A_TEMPLATE_THAT_IS_ALL_HOLE_MATCHES_EVERY_QUESTION = (
    "A rule whose template is '{client}' matches any question of one or two words and "
    "answers it from a projected field. The literal part is the whole of what makes a "
    "template a question shape rather than a wildcard, so there is a floor on how much of "
    "it there has to be. The floor is arbitrary in the way every threshold is; what is not "
    "arbitrary is that a template with no literal part is a rule that answers everything."
)


def literal_parts(template: str) -> tuple[str, str]:
    """The literal text either side of the hole. Either half may be empty, not both.

    Called only on a template `check_template` has accepted, so the braces are known to be
    there and in the right order. Written to raise rather than to guess if that ever stops
    being true: `str.index` on an absent brace is a `ValueError`, which is louder than the
    `-1` that `str.find` would return and then slice with.
    """
    open_at = template.index(SLOT_OPEN)
    close_at = template.index(SLOT_CLOSE)
    return template[:open_at], template[close_at + 1 :]


def check_template(template: str, slot: str) -> None:
    """Refuse a template that is not one hole named for the declared slot (M6.1.1).

    Raises `ValueError` rather than a type of its own, because both callers want that:
    pydantic turns it into a validation error on the way in, and a plain caller gets the
    sentence. The four checks are counting rather than parsing, deliberately, because
    `gate.fast_path_rule` states all four as check constraints and a check constraint cannot
    hold a parser. A grammar that could only be enforced in Python would be a grammar the
    database could not refuse a row against.
    """
    opens = template.count(SLOT_OPEN)
    closes = template.count(SLOT_CLOSE)
    if opens != 1 or closes != 1:
        msg = (
            f"a template has exactly one hole; this one has {opens} {SLOT_OPEN!r} and "
            f"{closes} {SLOT_CLOSE!r}, and a second hole is a second thing to get wrong in "
            "a lane with nothing reading the answer"
        )
        raise ValueError(msg)
    open_at = template.index(SLOT_OPEN)
    close_at = template.index(SLOT_CLOSE)
    if close_at < open_at:
        msg = "a template closes its hole before it opens one"
        raise ValueError(msg)
    named = template[open_at + 1 : close_at]
    if named != slot:
        msg = (
            f"the template names {named!r} and the rule declares slot {slot!r}; the two are "
            "read by different code and a rule that matches one thing and reports another "
            "is a wrong answer with a citation on it"
        )
        raise ValueError(msg)
    before, after = literal_parts(template)
    literal = f"{before}{after}".strip()
    if len(literal) < MIN_LITERAL_CHARS:
        msg = (
            f"the template has {len(literal)} literal characters, under {MIN_LITERAL_CHARS}. "
            f"{A_TEMPLATE_THAT_IS_ALL_HOLE_MATCHES_EVERY_QUESTION}"
        )
        raise ValueError(msg)
