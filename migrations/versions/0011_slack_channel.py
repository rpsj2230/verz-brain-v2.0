"""Widen the channel check constraints so `Channel.SLACK` is a value the database accepts.

No table, no column, no data. The same two check constraints `0007` widened for the widget,
widened once more, and this file is deliberately that one's twin rather than a new idea.

**Adding a member to `gate.context.Channel` is a schema change.** The enum is written into
the database twice as `channel IN (...)`, on `auth.principal_identity` (0002) and
`auth.session` (0003). Adding `SLACK` to the Python enum and stopping there produces code
that constructs a perfectly valid `Binding` and a database that refuses to store it, and it
fails at the first insert on the first deployment rather than anywhere a test would look.
`0007` records that lesson; this migration exists because the lesson held.

**Dropped and recreated rather than altered, because PostgreSQL has no `ALTER CONSTRAINT`
for a check.** `0002` chose a check constraint over a native enum type precisely so that this
change is ordinary: a member cannot be removed from a native enum at all, and adding one
needs `ALTER TYPE`, which on older servers cannot run inside the single transaction
`migrations/env.py` wraps a migration in.

**The substitution chains rather than restating the whole vocabulary from scratch.**
`SUPERSEDES` here maps `0007`'s widened list to this one, and `tests/unit/test_tables.py`
applies the substitutions in migration order, so the creating migration's SQL is carried
forward through 0007 and then through this one. Declaring the pre-widget list instead would
be a third hand-maintained copy of the vocabulary and would stop matching the moment 0007
changed.

**The downgrade is real and it can fail, which is correct.** Narrowing the list rejects the
migration if any row already carries `slack`, because recreating a check constraint validates
the existing rows. A downgrade that silently kept rows the schema says are impossible would
leave a table nothing can subsequently validate. Whoever needs to go back deletes those
bindings and sessions first, deliberately.

Task ids: M10.5.1
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

#: No table is created here. Named for symmetry with the other migrations, and empty so the
#: model-versus-migration comparison in `tests/unit/test_tables.py` does not look for one.
TABLES: tuple[str, ...] = ()

#: Every place the channel vocabulary is written into the database. Both carry the same
#: constraint under the same name, from 0002 and 0003 respectively.
CONSTRAINED: tuple[tuple[str, str], ...] = (
    ("auth", "principal_identity"),
    ("auth", "session"),
)

#: Alphabetical, matching how `tables.identity.one_of` sorts, so the model and the migration
#: compare equal as text rather than only as meaning.
WITH_SLACK = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'slack', 'webhook', "
    "'whatsapp', 'widget')"
)
WITHOUT_SLACK = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'webhook', 'whatsapp', 'widget')"
)

#: What this migration replaces. Read by `tests/unit/test_tables.py`, which renders the
#: *creating* migration and holds it against the current model: that comparison is right
#: until a later migration legitimately amends a constraint, at which point the model matches
#: the database and not the file that first built it. Declaring the substitution keeps the
#: comparison honest without weakening it, and `test_a_migration_that_declares_a_supersession
#: _actually_performs_it` checks the declaration against the SQL `upgrade` emits.
SUPERSEDES: dict[str, str] = {WITHOUT_SLACK: WITH_SLACK}


def upgrade() -> None:
    for schema, table in CONSTRAINED:
        # The bare name. Alembic applies `NAMING_CONVENTION["ck"]` on top, so passing the
        # already-prefixed `ck_<table>_channel` renders
        # `ck_principal_identity_ck_principal_identity_channel`, and the DROP names a
        # constraint that has never existed. 0007 learned this; the idiom is copied from it.
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITH_SLACK, schema=schema)


def downgrade() -> None:
    """Narrow the list again. Fails if a row already carries `slack`, deliberately."""
    for schema, table in CONSTRAINED:
        op.drop_constraint("channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITHOUT_SLACK, schema=schema)
