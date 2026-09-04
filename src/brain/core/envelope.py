"""The typed result envelope, and the tool contract.

Everything a tool returns is wrapped in a TypedResult carrying an entity tag and a record
id. That is not bookkeeping — it is what makes field-level redaction possible at all. The
redactor walks the envelope, and for each field asks "does the caller hold
`read:<entity>.<field>` in a scope that admits this row?". A tool that returned an untyped
dict would have no entity to ask about, so the redactor would have to either pass it
through unchecked or drop it whole.

This is also why Surface (browser, desktop) is a separate governed noun in the
architecture rather than a kind of Tool: a screenshot has no fields, so this mechanism is
mathematically inapplicable to it and it needs a weaker, differently-shaped guarantee.

Task ids: M0.2.5, M0.2.6
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Entity(BaseModel):
    """A record with a type tag. Subclass per entity; the tag drives redaction."""

    model_config = ConfigDict(extra="forbid")

    entity: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    id: str = Field(min_length=1, max_length=128)


class Redaction(BaseModel):
    """A field that was removed, and why. Retained so traces can show the shape of an
    answer without showing its contents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    record_id: str
    field: str
    reason: str = "no grant"


class TypedResult[T: BaseModel](BaseModel):
    """What every tool returns.

    `redactions` is populated by the redactor, not by the tool. A tool never decides what
    a caller may see; it returns everything it fetched and the gate removes what is not
    covered. Putting that decision in the tool would mean auditing every connector for
    permission logic instead of auditing one redactor.
    """

    model_config = ConfigDict(extra="forbid")

    records: tuple[T, ...] = ()
    redactions: tuple[Redaction, ...] = ()
    source: str = ""
    fetched_at: str = ""
    truncated: bool = False

    def record_count(self) -> int:
        return len(self.records)

    def was_redacted(self) -> bool:
        return len(self.redactions) > 0


class SideEffect(enum.StrEnum):
    """What running this tool does to the world. Drives which leash rung applies."""

    NONE = "none"
    DRAFT = "draft"
    WRITE = "write"
    SEND = "send"
    MONEY = "money"


class IdentityMode(enum.StrEnum):
    """Whose credentials the call runs under.

    DELEGATED means the caller's own token, so the source enforces its own permissions
    too. SERVICE means a shared credential, which is why any SERVICE tool must carry a
    scope predicate — the source will not narrow it for us.
    """

    DELEGATED = "delegated"
    SERVICE = "service"


class ToolDefinition(BaseModel):
    """The contract for one tool. The catalogue is projected per request: a caller only
    ever sees the tools their entitlement admits, so an unreachable tool is not described
    to the model at all rather than described and refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=400)
    entity: str = Field(min_length=1, max_length=60)
    args_schema: dict[str, Any] = Field(default_factory=dict)
    required_capability: str
    side_effect: SideEffect = SideEffect.NONE
    identity_mode: IdentityMode = IdentityMode.DELEGATED
    source: str = ""

    def is_read_only(self) -> bool:
        return self.side_effect is SideEffect.NONE

    def crosses_money_boundary(self) -> bool:
        return self.side_effect is SideEffect.MONEY
