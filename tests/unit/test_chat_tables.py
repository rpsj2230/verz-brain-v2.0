"""The conversation and message tables. Every test is a way one person's transcript
becomes reachable by somebody else.

Asserted against the models and against the migration's rendered DDL, because the two are
written separately on purpose - the migration describes the database it built, the model
describes what the code expects - and the only thing comparing them is a test.

Task ids: M9.1.1, M9.1.2, M9.1.3, M9.1.4
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from brain.db import SCHEMAS, Base
from brain.tables.chat import ConversationRow, MessageRole, MessageRow

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "versions" / "0005_chat.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0005", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy_sql() -> str:
    return "\n".join(_migration().RLS)


def _constraint_names(table: object) -> set[str]:
    """Constraint names as PostgreSQL will see them.

    `brain.db`'s naming convention prefixes every one - `ck_message_role`, not `role` - so a
    test asserting the bare name passes only by accident of how it was written. Matching on
    the suffix keeps these readable and independent of the prefix.
    """
    return {c.name for c in table.constraints if c.name}  # type: ignore[attr-defined]


def _has_constraint(table: object, suffix: str) -> bool:
    return any(name.endswith(suffix) for name in _constraint_names(table))


# ----------------------------------------------------- one owner, and only one
#: Every word that would make a conversation belong to more than one person. A shared
#: transcript is one person's answers, produced at their reach, readable by somebody with a
#: different reach - the two never see the same system, so the sharing is a leak with a
#: friendly name.
WOULD_SHARE = (
    "team",
    "team_id",
    "department",
    "shared_with",
    "shared",
    "visibility",
    "public",
    "group_id",
    "audience",
    "scope",
)


@pytest.mark.parametrize("column", WOULD_SHARE)
def test_a_conversation_has_no_column_that_could_make_it_belong_to_two_people(
    column: str,
) -> None:
    """M9.1.3 written as a schema rather than as a WHERE clause somebody has to remember.

    Deleting this invites a `shared_with` column, which reads in review as a feature and is
    a permission change: the second reader sees answers produced at the first reader's
    reach, and the redaction that produced them was never evaluated for them."""
    assert column not in ConversationRow.__table__.columns, (
        f"chat.conversation.{column} would let a transcript belong to two people"
    )


def test_a_conversation_must_have_an_owner() -> None:
    """A conversation with no owner is one no row-level security policy can restrict, and it
    is the one shape this table must not be able to hold."""
    principal = ConversationRow.__table__.columns["principal_id"]
    assert not principal.nullable
    assert _has_constraint(ConversationRow.__table__, "owned"), (
        "an empty string would satisfy NOT NULL"
    )


def test_a_message_carries_no_owner_of_its_own() -> None:
    """The owner is on the conversation and a message is reachable only through it. A second
    copy of "whose is this" is a second answer, and the day they disagree - a conversation
    reassigned, a message inserted with the wrong id - the permissive one wins."""
    assert "principal_id" not in MessageRow.__table__.columns


def test_a_message_cannot_outlive_the_row_that_says_who_it_belongs_to() -> None:
    """An orphaned message is a row the policy cannot reason about, because the policy reads
    the owner through the foreign key."""
    fks = list(MessageRow.__table__.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.fullname == "chat.conversation"
    assert fks[0].ondelete == "CASCADE"


def _clause(policy: str, keyword: str) -> str:
    """The text of one clause of one policy.

    `USING` and `WITH CHECK` are not interchangeable and a substring search over the whole
    statement cannot tell them apart. `USING` decides which rows are *visible*; `WITH CHECK`
    decides which rows may be *written*. Dropping the owner test from `USING` while leaving
    it in `WITH CHECK` means everybody reads everybody's conversations and nobody can write
    somebody else's - which is the wrong half kept, and it survived a test that searched the
    whole policy for the owner comparison.
    """
    statements = [s for s in _policy_sql().split("CREATE POLICY ") if s.startswith(policy)]
    assert len(statements) == 1, f"expected one policy named {policy}, found {len(statements)}"
    body = statements[0]
    # Each policy here has exactly two clauses, in this order, so splitting on the second
    # keyword separates them without a parser.
    using, _, with_check = body.partition("WITH CHECK")
    return using[using.index("USING") :] if keyword == "USING" else with_check


# ---------------------------------------------- restricted to the asker (M9.1.3)
def test_the_policy_restricts_by_principal_and_not_only_by_liveness() -> None:
    """The first policy in this system that does. Every earlier one says `deleted_at IS
    NULL`, because every earlier table holds company-wide data narrowed by the gate. A
    conversation is not company-wide, and "restricted to the asker" is a promise application
    code cannot keep - it has to survive the one query somebody writes without a WHERE."""
    assert "CREATE POLICY conversation_owner" in _policy_sql()
    # In the USING clause specifically. That is the one that decides what is *visible*, and
    # a test searching the whole statement is satisfied by the WITH CHECK clause alone -
    # which leaves reads unrestricted while writes stay locked down. Found by mutation.
    using = _clause("conversation_owner", "USING")
    assert "principal_id = current_setting('app.principal_id', true)" in using
    assert "deleted_at IS NULL" in using


def test_an_unidentified_connection_sees_nothing_rather_than_everything() -> None:
    """`current_setting(..., true)` returns NULL when nobody set it, and `principal_id =
    NULL` is NULL rather than true. So a connection that forgot to identify itself gets an
    empty table.

    Asserted on the second argument specifically. Written as `current_setting('...')` with
    one argument it *raises* instead, which is also safe - but a policy that raises turns a
    missing session variable into a 500 on every query, and somebody fixes that by removing
    the policy."""
    assert "current_setting('app.principal_id', true)" in _policy_sql()
    assert "current_setting('app.principal_id')" not in _policy_sql().replace(
        "current_setting('app.principal_id', true)", ""
    )


def test_the_message_policy_reads_the_owner_through_the_conversation() -> None:
    """Rather than repeating it. The join is what makes there be one answer to whose message
    this is."""
    assert "CREATE POLICY message_through_conversation" in _policy_sql()
    using = _clause("message_through_conversation", "USING")
    assert "FROM chat.conversation c" in using
    assert "c.principal_id = current_setting('app.principal_id', true)" in using


def test_both_tables_have_row_level_security_turned_on() -> None:
    """A policy on a table without RLS enabled is a policy PostgreSQL never consults. The
    two statements are separate and forgetting the first is silent."""
    sql = _policy_sql()
    assert "ALTER TABLE chat.conversation ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE chat.message ENABLE ROW LEVEL SECURITY" in sql


def test_nothing_may_delete_a_row() -> None:
    """A conversation is retired with `deleted_at`; a message is not retired at all. A
    message that can be removed makes the transcript something whose meaning can be edited
    by removal, and a transcript that can be edited is not worth keeping."""
    grants = "\n".join(_migration().GRANTS)
    assert "DELETE" not in grants
    assert "GRANT SELECT, INSERT ON chat.message" in grants, "a message may not be updated"


# ------------------------------------------- continuity across channels (M9.1.2)
def test_the_channel_is_on_the_message_and_not_on_the_conversation() -> None:
    """A question asked in the console and followed up in Lark is one thread. On the
    conversation, continuing elsewhere would start a new one - and a person would lose their
    own context by switching app, or somebody would add a lookup that stitches threads
    together by principal and time, which is a join that guesses."""
    assert "channel" in MessageRow.__table__.columns
    assert "channel" not in ConversationRow.__table__.columns


# --------------------------------------------------- what a message may hold
def test_refs_hold_identifiers_and_the_constraint_says_so_far_as_it_can() -> None:
    """A check cannot prove a jsonb column holds no values. It asserts the shape it can: an
    array, so a record object cannot be put here and read as a reference list later."""
    assert _has_constraint(MessageRow.__table__, "refs_is_an_array")


def test_there_is_no_embedding_column() -> None:
    """Not an oversight to fix later. Making transcripts searchable by similarity means one
    person's phrasing pulls up another person's conversation in a nearest-neighbour scan,
    and a vector index carries no principal.

    If search across people is ever wanted it is a feature with its own permission model,
    not a column added here - and this test is what makes somebody argue for it rather than
    add it."""
    for table in (ConversationRow.__table__, MessageRow.__table__):
        for column in table.columns:
            assert "embedding" not in column.name
            assert "vector" not in str(column.type).lower()


def test_the_role_vocabulary_is_closed() -> None:
    """A system note is not something the assistant said. Folding the two would make the
    transcript claim the assistant said things nobody wrote."""
    assert {r.value for r in MessageRole} == {"user", "assistant", "system"}
    assert _has_constraint(MessageRow.__table__, "role")


# ------------------------------------------------------------------ the schema
def test_chat_is_a_schema_of_its_own() -> None:
    """Neither `mem` nor `obs` would do. `mem` holds the three memory kinds - things the
    system learnt and recalls - and a transcript is not learnt, it is what happened. `obs`
    holds metadata and no payloads, which is why the ledger can be kept for years, and a
    message has text in it. Either placement would make one of those two rules stop being
    true, and both are load-bearing."""
    assert "chat" in SCHEMAS
    assert ConversationRow.__table__.schema == "chat"
    assert MessageRow.__table__.schema == "chat"


def test_the_migration_creates_the_schema_before_the_tables() -> None:
    """It is the tenth, so it does not exist yet. Creating a table in a schema that is not
    there fails with an error naming the table, which sends somebody to look at the table."""
    import inspect

    source = inspect.getsource(_migration().upgrade)
    assert source.index("CREATE SCHEMA") < source.index("_create_conversation")


def test_every_table_here_is_in_the_package_tuple() -> None:
    """`brain.tables.TABLES_IN_DEPENDENCY_ORDER` is the one list anything outside the
    package reads, and a partial list is worse than none: the tables it omits look
    accounted for."""
    from brain.tables import TABLES_IN_DEPENDENCY_ORDER

    assert "chat.conversation" in TABLES_IN_DEPENDENCY_ORDER
    assert "chat.message" in TABLES_IN_DEPENDENCY_ORDER
    assert set(TABLES_IN_DEPENDENCY_ORDER) == set(Base.metadata.tables)
