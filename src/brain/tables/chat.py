"""Conversations and the messages in them.

**A transcript is a record of what was shown, and never a source the system answers from.**
That single sentence is the whole design, and the two halves pull in opposite directions.

The person who asked already saw the answer, so keeping it and letting them read it back
takes nothing from anybody: deleting their history when a grant is revoked would be
theatre, since they read it at the time. But a *follow-up* must not draw on it, because by
then the grant may be gone and a model told "the client is Acme" does not stop to ask
whether it still may know that. `brain.chat.turns.context_for` is the enforcement of the
second half; this module is the storage that makes the first half safe.

**A conversation belongs to exactly one principal, and there is no column that could make it
belong to two.** No `team_id`, no `department`, no `shared_with`. That is the M9.1.3 rule -
"conversation search restricted to the asker" - written as a schema rather than as a WHERE
clause somebody has to remember. A shared transcript is one person's answers, produced at
their reach, readable by somebody with a different reach; the two people never see the same
system, so the sharing is a leak with a friendly name on it.

**Thread continuity is per conversation, not per channel (M9.1.2).** A question asked in the
console and followed up in Lark is one thread, so `channel` lives on the message and not on
the conversation. Putting it on the conversation would make continuing elsewhere a new
thread, and a person would lose their own context by switching app - or, worse, somebody
would add a lookup that stitches threads together by principal and time, which is a join
that guesses.

**A message stores what was shown and the identifiers behind it.** `refs` is jsonb holding
entity and record ids only, never values: it is what `RecordRef` carries, and the reason is
the same. A copy of a record still reads perfectly after the grant behind it is revoked; an
identifier yields nothing when it is re-checked.

**What is deliberately absent.** No embedding column, and that is not an oversight to fix
later. Making transcripts searchable by similarity means one person's phrasing of a question
pulls up another person's conversation in a nearest-neighbour scan, and a vector index does
not carry a principal. If conversation search across people is ever wanted, it is a feature
with its own permission model, not a column added here.

Task ids: M9.1.1, M9.1.2, M9.1.3, M9.1.4
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.tables.identity import PRINCIPAL_ID_CHARS, one_of

#: Long enough for a real title and short enough that it is a title. Titles are generated
#: from the first question, so this is a truncation point rather than a limit somebody hits.
TITLE_CHARS = 200

#: A channel name, as `brain.gate.context.Channel` spells it. Stored as text rather than as
#: a foreign key to an enum table: the set is closed in code, and a table would let a
#: channel exist in the database that no code path can produce.
CHANNEL_CHARS = 32


class MessageRole(enum.StrEnum):
    """Who said it. Closed, and the members are not interchangeable.

    `SYSTEM` is separate from `ASSISTANT` because a system note - "this conversation was
    exported", "an approval expired" - is not something the assistant said, and folding the
    two would make the transcript claim the assistant said things nobody wrote.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


class ConversationRow(TimestampMixin, SoftDeleteMixin, Base):
    """`chat.conversation`. One thread, owned by one person (M9.1.1).

    `principal_id` is the owner and it is not nullable. A conversation with no owner is a
    conversation no row-level security policy can restrict, which is the one shape this
    table must not be able to hold.

    Not a foreign key to `auth.principal`, for the reason `CapabilityGrantRow.granted_by`
    gives: a transcript has to outlive the account that produced it, or an offboarding
    deletes the record of what was asked - which is exactly the record somebody wants after
    an offboarding.
    """

    __tablename__ = "conversation"
    __table_args__ = (
        CheckConstraint(_present("principal_id"), name="owned"),
        # Live rows only. A retired conversation keeps its title; the index exists so a
        # person's conversation list is a single index scan on the one column every query
        # here filters by.
        Index(
            "ix_conversation_owner_live",
            "principal_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    #: The one person this belongs to. There is no second column that could widen it, and
    #: `tests/unit/test_chat_tables.py` fails on one being added.
    principal_id: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)

    #: Generated from the first question, so a list of conversations reads as a list of
    #: questions. Nullable: a conversation exists from the moment it is opened, and the
    #: title arrives with the first message.
    title: Mapped[str | None] = mapped_column(String(TITLE_CHARS), nullable=True)


class MessageRow(TimestampMixin, Base):
    """`chat.message`. One turn, in whichever channel it happened in (M9.1.2).

    No soft delete, unlike almost everything else here. A message that can be hidden makes
    the transcript a thing somebody can edit the meaning of by removal, and a transcript
    that can be edited is not worth keeping. A conversation is retired as a whole or not at
    all.
    """

    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint(one_of("role", MessageRole), name="role"),
        CheckConstraint(_present("channel"), name="channel_present"),
        # `refs` holds identifiers and never values. A check cannot prove that, so it
        # asserts the shape it can: an array, so a caller cannot put a record object in it
        # and have it read as a reference list later.
        CheckConstraint("jsonb_typeof(refs) = 'array'", name="refs_is_an_array"),
        Index("ix_message_conversation", "conversation_id", "created_at"),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    #: Cascades on delete, and this is the one foreign key here. A message without its
    #: conversation is a fragment nothing can restrict: the owner is on the conversation, so
    #: an orphaned message is a row row-level security cannot reason about.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat.conversation.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Where this turn happened. On the message rather than the conversation, so a thread
    #: continued in another app is the same thread.
    channel: Mapped[str] = mapped_column(String(CHANNEL_CHARS), nullable=False)

    #: What was said or shown. For an assistant turn this is the rendered answer, locks
    #: included, exactly as the asker saw it - the transcript is a record of what was shown.
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    #: Entity and record identifiers the answer drew on. Never values. See the module
    #: docstring, and `brain.chat.turns.RecordRef`, which is the same rule in the type.
    refs: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
