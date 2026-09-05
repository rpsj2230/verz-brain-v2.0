"""Conversations and the messages in them, restricted to the person who asked.

Two tables and a schema. The schema is the notable part: `chat` is the tenth, and it exists
because a transcript is neither of the two things it might otherwise have gone in. `mem`
holds the three memory kinds - things the system learnt and recalls - and a transcript is
not learnt, it is what happened. `obs` holds metadata and deliberately no payloads, which is
why the ledger can be kept for years, and a message has text in it. Putting conversations in
either would make one of those two rules stop being true, and both are load-bearing.

**The row-level security policy here is the first one in this system that restricts by
principal rather than by liveness**, and that is the point of the pair of tables. Every
earlier policy says `deleted_at IS NULL`, because every earlier table is company-wide data
whose per-caller narrowing happens in the gate. A conversation is not: it belongs to one
person, and "conversation search restricted to the asker" (M9.1.3) is a promise that a
`WHERE principal_id = ...` in application code cannot keep, because the promise has to
survive the one query somebody writes without it.

So the policy reads `app.principal_id`, a session setting the application sets per request.
Two consequences are worth stating plainly, because both are ways this can be wrong.

**An unset setting admits nothing.** `current_setting('app.principal_id', true)` returns
NULL when nobody set it, and `principal_id = NULL` is NULL rather than true, so a connection
that forgot to identify itself sees an empty table rather than everybody's. That is the
correct direction and it is worth knowing it is the *default* direction, not a special case
somebody wrote.

**A pooled connection must set it every time.** PgBouncer runs in transaction mode here, so
a `SET` outside a transaction can land on a connection that is then handed to somebody else.
`SET LOCAL` inside the transaction is the only safe form, and this is the same class of trap
that made `pg_advisory_lock` unusable in `brain.migrate` and had to become
`pg_advisory_xact_lock`. Whoever wires the session factory owns that; it is written here
because this file is what a person reads when they wonder why a query returned nothing.

**`chat.message` has no policy of its own and that is deliberate.** It has no
`principal_id`: the owner is on the conversation, and a message is reachable only through
it. Giving the message table its own copy of the owner would be a second answer to "whose is
this", and the day the two disagree - a conversation reassigned, a message inserted with the
wrong id - the permissive one wins. The foreign key cascades, so a message cannot outlive
the row that says who it belongs to.

**No DELETE grant on `chat.conversation`, and no soft delete on `chat.message`.** A
conversation is retired as a whole; a message that could be hidden makes a transcript
something whose meaning can be edited by removal, and a transcript that can be edited is not
worth keeping.

Task ids: M9.1.1, M9.1.2, M9.1.3, M9.1.4

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "brain_app"

#: Ordered so a table appears after everything it points at. `downgrade` walks it in
#: reverse, which is what makes the two provably inverse without a second list.
TABLES: tuple[str, ...] = (
    "chat.conversation",
    "chat.message",
)

#: The session setting the policy reads. Named with a dot so PostgreSQL treats it as a
#: customised option rather than rejecting it; `app` matches nothing PostgreSQL owns.
PRINCIPAL_SETTING = "app.principal_id"

#: `brain.tables.chat.MessageRole`, copied rather than imported for the reason 0004 gives:
#: a migration describes the database it built, not whatever the models say today.
ROLE_IN = "role IN ('assistant', 'system', 'user')"

RLS: tuple[str, ...] = (
    "ALTER TABLE chat.conversation ENABLE ROW LEVEL SECURITY",
    # Written out rather than assembled, as 0001 through 0004 are. Nothing here interpolates
    # a value into a statement, so there is no question to ask about what could be in one -
    # and the policy body is the last place a reader should have to resolve a constant to
    # know what the database actually enforces.
    #
    # `current_setting(..., true)` returns NULL instead of raising when the setting is
    # absent. Raising would be defensible; NULL is better, because `principal_id = NULL` is
    # NULL, so an unidentified connection sees nothing rather than everything.
    """
    CREATE POLICY conversation_owner ON chat.conversation
        FOR ALL TO brain_app
        USING (
            deleted_at IS NULL
            AND principal_id = current_setting('app.principal_id', true)
        )
        WITH CHECK (
            principal_id = current_setting('app.principal_id', true)
        )
    """,
    # The message policy restricts through the conversation rather than repeating the
    # owner. A second copy of "whose is this" is a second answer, and the permissive one
    # wins the day they disagree.
    "ALTER TABLE chat.message ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY message_through_conversation ON chat.message
        FOR ALL TO brain_app
        USING (
            EXISTS (
                SELECT 1 FROM chat.conversation c
                WHERE c.id = chat.message.conversation_id
                  AND c.deleted_at IS NULL
                  AND c.principal_id = current_setting('app.principal_id', true)
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM chat.conversation c
                WHERE c.id = chat.message.conversation_id
                  AND c.principal_id = current_setting('app.principal_id', true)
            )
        )
    """,
)

#: No DELETE anywhere, as everywhere else. A conversation is retired with `deleted_at`; a
#: message is not retired at all.
GRANTS: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA chat TO brain_app",
    "GRANT SELECT, INSERT, UPDATE ON chat.conversation TO brain_app",
    "GRANT SELECT, INSERT ON chat.message TO brain_app",
)


def _create_conversation() -> None:
    op.create_table(
        "conversation",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
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
        sa.CheckConstraint("length(btrim(principal_id)) > 0", name="owned"),
        schema="chat",
    )
    op.create_index(
        "ix_conversation_owner_live",
        "conversation",
        ["principal_id", "created_at"],
        schema="chat",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def _create_message() -> None:
    op.create_table(
        "message",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
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
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat.conversation.id"],
            name="fk_message_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(ROLE_IN, name="role"),
        sa.CheckConstraint("length(btrim(channel)) > 0", name="channel_present"),
        # A check cannot prove that `refs` holds identifiers and no values. It asserts the
        # shape it can: an array, so a record object cannot be put here and read as a
        # reference list later.
        sa.CheckConstraint("jsonb_typeof(refs) = 'array'", name="refs_is_an_array"),
        schema="chat",
    )
    op.create_index(
        "ix_message_conversation", "message", ["conversation_id", "created_at"], schema="chat"
    )


def upgrade() -> None:
    assert all(APP_ROLE in statement for statement in GRANTS)

    op.execute("CREATE SCHEMA IF NOT EXISTS chat")
    _create_conversation()
    _create_message()

    for statement in RLS:
        op.execute(statement)
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # Policies, indexes and privileges belong to their tables and go with them. The schema
    # is dropped last and only when empty, so a table added to `chat` by a later migration
    # that forgot to drop itself fails loudly here rather than being taken with it.
    for qualified in reversed(TABLES):
        schema, _, name = qualified.partition(".")
        op.drop_table(name, schema=schema)
    op.execute("DROP SCHEMA IF EXISTS chat RESTRICT")
