"""Widen the channel check constraints once more, for `Channel.TELEGRAM`.

No table, no column, no data. `0011` did this for Slack and `0007` for the widget before it;
this is the third in the same sequence and is deliberately their twin rather than a new idea.

**Two channels were added at once, and that is why this is a separate migration rather than
an edit to 0011.** Slack and Telegram were written in parallel, each needing the same two
check constraints widened. Editing 0011 to name both would have been smaller, and it would
have been wrong: 0011 has a revision id, and a migration that has run anywhere is a fact
about that database rather than a file to revise. Amending it would leave any server that
already applied 0011 permanently one channel short, with alembic reporting itself up to
date. So the vocabulary widens twice, in order, which is what the chain is for.

**The substitution chains rather than restating the vocabulary.** `SUPERSEDES` maps 0011's
list to this one, and `tests/unit/test_tables.py` applies the substitutions in migration
order: 0002 and 0003 create the constraint, 0007 widens it for the widget, 0011 for Slack,
and this one for Telegram. Declaring the previous list rather than the original is what keeps
that chain honest; naming the pre-widget vocabulary here would be a fourth hand-maintained
copy and would stop matching the moment any earlier one changed.

**Why this is a schema change at all, restated because it is the part that surprises.**
`brain.tables.identity.one_of` generates the constraint from the `Channel` enum itself, so
adding a member updates the model with no edit. It does not update a deployed database.
Without this migration the model and the migration chain disagree, which is exactly what
`test_tables.py` reported the moment `Channel.TELEGRAM` appeared, and without the model
check it would instead have been a perfectly valid `Binding` that the database refuses on
the first insert after deploy.

**The downgrade is real and it can fail, which is correct.** Narrowing the list rejects the
migration if any row already carries `telegram`, because recreating a check constraint
validates the rows already there. Whoever needs to go back deletes those bindings and
sessions first, deliberately, rather than discovering later that the schema forbids rows the
table contains.

Task ids: M10.5.4
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: No table is created here. Named for symmetry with the migrations that do, and empty so
#: the model-versus-migration comparison does not look for one.
TABLES: tuple[str, ...] = ()

#: Every place the channel vocabulary is written into the database, from 0002 and 0003.
CONSTRAINED: tuple[tuple[str, str], ...] = (
    ("auth", "principal_identity"),
    ("auth", "session"),
)

#: Alphabetical, matching how `tables.identity.one_of` sorts, so the model and the migration
#: compare equal as text rather than only as meaning.
WITH_TELEGRAM = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'slack', 'telegram', "
    "'webhook', 'whatsapp', 'widget')"
)
WITHOUT_TELEGRAM = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'slack', 'webhook', "
    "'whatsapp', 'widget')"
)

#: What this migration replaces: 0011's list, not the original. See the docstring.
SUPERSEDES: dict[str, str] = {WITHOUT_TELEGRAM: WITH_TELEGRAM}


def upgrade() -> None:
    for schema, table in CONSTRAINED:
        # The bare name. Alembic applies `NAMING_CONVENTION["ck"]` on top, so passing the
        # already-prefixed `ck_<table>_channel` renders
        # `ck_principal_identity_ck_principal_identity_channel`, and the DROP names a
        # constraint that has never existed. 0007 learned this; 0011 copied it; so does this.
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITH_TELEGRAM, schema=schema)


def downgrade() -> None:
    """Narrow the list again. Fails if a row already carries `telegram`, deliberately."""
    for schema, table in CONSTRAINED:
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITHOUT_TELEGRAM, schema=schema)
