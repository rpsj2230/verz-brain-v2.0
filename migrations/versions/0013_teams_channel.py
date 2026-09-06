"""Widen the channel check constraints once more, for `Channel.TEAMS`.

No table, no column, no data. `0007` did this for the widget, `0011` for Slack and `0012` for
Telegram; this is the fourth in the same sequence and is deliberately their twin rather than a
new idea.

**Adding a member to `gate.context.Channel` is a schema change, which is the part that
surprises.** `brain.tables.identity.one_of` generates the constraint from the enum itself, so
adding `TEAMS` updates the model with no edit here at all. It does not update a deployed
database. The enum is written into the database twice as `channel IN (...)`, on
`auth.principal_identity` (0002) and `auth.session` (0003), and without this migration the
model and the migration chain disagree, which is what `tests/unit/test_tables.py` reports the
moment the member appears. Without that test it would instead have been a perfectly valid
`Binding` the database refuses on the first insert after a deploy.

**A separate migration rather than an edit to 0012.** 0012 has a revision id, and a migration
that has run anywhere is a fact about that database rather than a file to revise. Amending it
would leave any server that already applied it permanently one channel short, with alembic
reporting itself up to date. So the vocabulary widens once per channel, in order, which is what
the chain is for.

**The substitution chains rather than restating the vocabulary.** `SUPERSEDES` maps 0012's list
to this one, and `tests/unit/test_tables.py` applies the substitutions in migration order: 0002
and 0003 create the constraint, 0007 widens it for the widget, 0011 for Slack, 0012 for Telegram
and this one for Teams. Declaring the previous list rather than the original is what keeps that
chain honest; naming the pre-widget vocabulary here would be a fifth hand-maintained copy and
would stop matching the moment any earlier one changed.

**Dropped and recreated rather than altered, because PostgreSQL has no `ALTER CONSTRAINT` for a
check.** 0002 chose a check constraint over a native enum type precisely so that this change is
ordinary: a member cannot be removed from a native enum at all, and adding one needs
`ALTER TYPE`, which on older servers cannot run inside the single transaction
`migrations/env.py` wraps a migration in.

**The downgrade is real and it can fail, which is correct.** Narrowing the list rejects the
migration if any row already carries `teams`, because recreating a check constraint validates
the rows already there. Whoever needs to go back deletes those bindings and sessions first,
deliberately, rather than discovering later that the schema forbids rows the table contains.

Task ids: M10.5.2
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

#: No table is created here. Named for symmetry with the migrations that do, and empty so the
#: model-versus-migration comparison does not look for one.
TABLES: tuple[str, ...] = ()

#: Every place the channel vocabulary is written into the database, from 0002 and 0003.
CONSTRAINED: tuple[tuple[str, str], ...] = (
    ("auth", "principal_identity"),
    ("auth", "session"),
)

#: Alphabetical, matching how `tables.identity.one_of` sorts, so the model and the migration
#: compare equal as text rather than only as meaning. `teams` sorts before `telegram`, which is
#: the sort deciding it rather than the order the channels were built in.
WITH_TEAMS = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'slack', 'teams', "
    "'telegram', 'webhook', 'whatsapp', 'widget')"
)
WITHOUT_TEAMS = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'slack', 'telegram', "
    "'webhook', 'whatsapp', 'widget')"
)

#: What this migration replaces: 0012's list, not the original. See the docstring.
SUPERSEDES: dict[str, str] = {WITHOUT_TEAMS: WITH_TEAMS}


def upgrade() -> None:
    for schema, table in CONSTRAINED:
        # The bare name. Alembic applies `NAMING_CONVENTION["ck"]` on top, so passing the
        # already-prefixed `ck_<table>_channel` renders
        # `ck_principal_identity_ck_principal_identity_channel`, and the DROP names a
        # constraint that has never existed. 0007 learned this; 0011 and 0012 copied it, and
        # so does this one.
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITH_TEAMS, schema=schema)


def downgrade() -> None:
    """Narrow the list again. Fails if a row already carries `teams`, deliberately."""
    for schema, table in CONSTRAINED:
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITHOUT_TEAMS, schema=schema)
