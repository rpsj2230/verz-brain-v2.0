"""One table: the upgrade somebody said no to, in a row nothing can delete.

Mirrors `brain.agents.upgrade`, which is the definition; this is storage for it. The
widths, the grammars and the digest shape are generated from that layer's own constants
rather than retyped, so a change to the domain breaks a test instead of a deploy.

**Durability is the whole feature, and the grant is what supplies it.** A decline is worth
recording only if it survives: one that is forgotten by the next restart, or by the next
publish, is a nag, and a nag is how somebody accepts an upgrade they did not read. So this
table is granted SELECT and INSERT and never UPDATE or DELETE, which is
`agent.template_version`'s arrangement for the same kind of reason. A decline cannot be
edited into a different version, and it cannot be quietly withdrawn to make a badge come
back. Changing your mind is `accept`, which moves the pin and leaves the row standing.

**The key is the instance, the template and the version, and the version is in it
deliberately.** Declining version 3 says nothing about version 4, so the row has to name a
version rather than a template; keyed by template alone, one decline would silence every
future version of it for ever, which is not a decline, it is an install nobody will ever
look at again. `brain.agents.upgrade.A_DECLINE_IS_PINNED_TO_A_VERSION_AND_A_BODY` argues it.

**`content_digest` is a column and not part of the key.** It records which body was
declined, so a version republished with a different one is shown again rather than covered
by a decline of something else. It stays out of the primary key because the foreign key
below points at `agent.template_version`, where the pair is unique: admitting two declines
of one version would be admitting two bodies the target table cannot tell apart.

**Two foreign keys, and both point at tables that already exist when a decline is made.**
An install exists before anybody can decline an upgrade to it, and the version being
declined has to have been published. Neither key blocks anything anybody wants to do,
because no DELETE grant exists on either target. This is the opposite case from the one
`brain.tables.template` refuses: a key from the instance into `agent.agent` would have had
to run the other way and would have been an ALTER on a deployed table.

**No audience columns**, for the reason the template tables give: who may see an agent lives
on `agent.agent` and in one place. A decline is not a visibility decision and carries no
opinion about who may read it.

**No `updated_at` and no `deleted_at`.** A row that can never be updated with an
`updated_at` on it is a column that tells a reader something untrue, which is
`obs.audit_entry`'s argument and `agent.template_version`'s. `declined_at` is the time
anybody asks about: when a person said no, by their own clock, rather than when the row
landed.

**No index beyond the primary key.** The only question asked of this table is whether one
instance has declined one version of one template, which is that key exactly. An index on
`template_id` alone would serve a listing across the estate, and there is no such listing:
`brain.agents.upgrade` argues that one has to apply the agent audience first and holds no
agent records to apply it with.

Task ids: M13.4.5
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.agents.model import AGENT_ID_CHARS, OWNER_ID_CHARS
from brain.audit.ledger import DIGEST_CHARS
from brain.core.department import SLUG_PATTERN
from brain.db import Base

#: A colon inside a check constraint is a bind parameter unless it is escaped.
#:
#: The fourth copy of this escape in `brain.tables`, beside `agent.py`, `gate.py` and
#: `template.py`, and it is copied rather than imported for the same reason each of those
#: is: the constant is one character and the thing that keeps it right is a test reading the
#: *compiled* DDL, not a shared helper. `CheckConstraint` parses its argument as `text()`,
#: which reads `:name` as a parameter to bind later, and `SLUG_PATTERN` contains one colon
#: in `(?:`. Unescaped it renders as `(?NULL[a-z0-9]+)*`, the DDL is accepted, and the first
#: INSERT fails with "invalid regular expression: quantifier operand invalid". 0015 exists
#: because three tables shipped exactly that way.
_ESCAPED_COLON = "\\:"


def _slug_grammar(column: str) -> str:
    """The shared slug grammar, escaped, applied to one column."""
    return f"{column} ~ '" + SLUG_PATTERN.replace(":", _ESCAPED_COLON) + "'"


def _digest_shape(column: str) -> str:
    """A full sha256 hexdigest, written from `DIGEST_CHARS` rather than with a literal 64."""
    return f"{column} ~ '^[0-9a-f]{{{DIGEST_CHARS}}}$'"


def _present(column: str) -> str:
    return f"length(btrim({column})) > 0"


class UpgradeDeclineRow(Base):
    """`agent.upgrade_decline`. One version of one template, refused for one install.

    Carries no `TimestampMixin`: the mixin brings `updated_at`, and there is no update to
    this table at all. `created_at` is declared here on its own, and it is not `declined_at`:
    one is when the row arrived and the other is when a person decided.
    """

    __tablename__ = "upgrade_decline"

    #: The agent slug the decline is for. `AgentRecord.agent_id`'s grammar and
    #: `agent.template_instance.id`'s, because it is that row.
    instance_id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), primary_key=True)

    #: Which template. Carried rather than derived through the instance, so the row says
    #: what was declined without a join, and so the foreign key below can be the pair
    #: `agent.template_version` is keyed by.
    template_id: Mapped[str] = mapped_column(String(AGENT_ID_CHARS), primary_key=True)

    #: Which version. In the key, because a decline of version 3 must say nothing at all
    #: about version 4: the badge comes back when a newer one is published, and stays down
    #: for this one for ever.
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    #: The digest of the body that was declined. Not in the key; see the module docstring.
    content_digest: Mapped[str] = mapped_column(String(DIGEST_CHARS), nullable=False)

    #: Who said no. Not a foreign key, for the reason `agent.agent` gives about its own
    #: creator: the decision has to outlive the account that took it.
    declined_by: Mapped[str] = mapped_column(String(OWNER_ID_CHARS), nullable=False)

    #: No server default. This is when a person decided, not when the row arrived, and the
    #: two differ by however long the path took. `agent.template_version.signed_at` and
    #: `obs.audit_entry.at` both refuse a default for the same reason.
    declined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: When the row landed, which is not `declined_at`. Declared here rather than inherited,
    #: because the mixin brings `updated_at` with it and this row is never updated.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Named by hand and carrying the `fk_` prefix `brain.db.NAMING_CONVENTION` would
        # have given them, as `fk_template_instance_pinned_version` is: an explicit name is
        # what lets the migration and the model be compared on rendered DDL at all.
        ForeignKeyConstraint(
            ["template_id", "version"],
            ["agent.template_version.template_id", "agent.template_version.version"],
            name="fk_upgrade_decline_declined_version",
        ),
        ForeignKeyConstraint(
            ["instance_id"],
            ["agent.template_instance.id"],
            name="fk_upgrade_decline_instance",
        ),
        CheckConstraint(_slug_grammar("instance_id"), name="instance_slug_grammar"),
        CheckConstraint(_slug_grammar("template_id"), name="template_slug_grammar"),
        CheckConstraint("version >= 1", name="version_is_positive"),
        CheckConstraint(_digest_shape("content_digest"), name="content_digest_shape"),
        CheckConstraint(_present("declined_by"), name="declined_by_present"),
        {"schema": "agent"},
    )
