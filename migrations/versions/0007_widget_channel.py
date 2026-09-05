"""Widen the channel check constraints so `Channel.WIDGET` is a value the database accepts.

No table, no column, no data. Two check constraints, dropped and recreated with one more
value in the list.

**Adding a member to `gate.context.Channel` is a schema change, and that is the whole lesson
of this file.** The enum is written into the database twice as `channel IN (...)`, on
`auth.principal_identity` (0002) and `auth.session` (0003). Adding `WIDGET` to the Python
enum and stopping there produces code that constructs a perfectly valid object and a database
that refuses to store it, and it fails at the first insert on the first deployment rather
than anywhere a test would ordinarily look.

It did not get that far here. `test_the_migration_builds_each_table_exactly_as_the_model_
declares_it` compares the model's rendered `CREATE TABLE` against the migration's, so the
mismatch surfaced the moment the enum member was added, which is exactly what that test is
for and why it is parameterised per table rather than written as one assertion over the lot.

**Dropped and recreated rather than altered, because PostgreSQL has no `ALTER CONSTRAINT`
for a check.** This is the case the constraint style was chosen for: `0002`'s own comment
records that a native enum type was rejected because a member cannot be removed at all and
adding one needs `ALTER TYPE`, which on older servers cannot run inside the single
transaction `migrations/env.py` wraps a migration in. A check constraint is dropped and
recreated like anything else, so the downgrade below is ordinary.

**The downgrade is real and it can fail, which is correct.** Narrowing the list back rejects
the migration if any row already carries `widget`, because recreating a check constraint
validates the existing rows. That is the right behaviour: a downgrade that silently kept
rows the schema says are impossible would leave a table nothing can subsequently validate.
Whoever needs to go back deletes those sessions first, deliberately.

Task ids: M10.5.5
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
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

#: Alphabetical, matching how SQLAlchemy renders `one_of`, so the model and the migration
#: compare equal as text rather than only as meaning.
WITH_WIDGET = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'webhook', 'whatsapp', 'widget')"
)
WITHOUT_WIDGET = (
    "channel IN ('api', 'console', 'email', 'lark', 'scheduler', 'webhook', 'whatsapp')"
)

#: What this migration replaces, so that the model-versus-migration comparison in
#: `tests/unit/test_tables.py` can read it here rather than being taught about this change.
#:
#: That comparison renders the *creating* migration and holds it against the current model,
#: which is the right check right up until a later migration legitimately amends a
#: constraint - at which point the model matches the database and not the file that first
#: built it, and the test fails for being correct. Declaring the substitution here keeps the
#: comparison honest without weakening it: it still compares two hand-maintained copies, it
#: just knows which copy is current. The next migration that amends a constraint exports the
#: same name and needs no change to any test.
SUPERSEDES: dict[str, str] = {WITHOUT_WIDGET: WITH_WIDGET}


def upgrade() -> None:
    for schema, table in CONSTRAINED:
        op.drop_constraint(f"ck_{table}_channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITH_WIDGET, schema=schema)


def downgrade() -> None:
    """Narrow the list again. Fails if a row already carries `widget`, deliberately."""
    for schema, table in CONSTRAINED:
        op.drop_constraint(f"ck_{table}_channel", table, schema=schema, type_="check")
        op.create_check_constraint("channel", table, WITHOUT_WIDGET, schema=schema)
