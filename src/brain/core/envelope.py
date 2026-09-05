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

# ------------------------------------------------------------------- the grammars

#: `source.verb_noun`. Defined here and imported by everything that checks it.
#:
#: There used to be three copies of this, and they did not agree: this field and
#: `brain.ops.sweeps.TOOL_NAME_RE` were both written as `name.name`, while
#: `brain.tools.registry` required the second half to carry a verb and a noun. A tool named
#: `client.read` therefore passed the model and passed CI and was refused only at
#: registration, which is the worst of the three places to find out.
#:
#: The stricter one won because the second half is what tells a model what the tool does *to
#: what*. The model has one line of description and this name to choose from, and `read` on
#: its own says only that something is read.
#:
#: Rejected: also requiring the verb to be one of the capability verbs. The architecture's
#: own examples include `ticket.set_status`, and `set` is not a capability verb. A grammar
#: that refuses the specification's own examples is one somebody edits out of the way, and
#: then nothing checks the shape at all.
#:
#: The three parts are named groups, so a module that needs to split a tool name reads them
#: off this one match rather than splitting on a dot of its own.
TOOL_NAME_PATTERN = (
    r"^(?P<source>[a-z][a-z0-9_]*)\.(?P<verb>[a-z][a-z0-9]*)_(?P<noun>[a-z][a-z0-9_]*)$"
)

#: What a tool's object may be called, and the same shape `Entity.entity` requires.
#: `brain.core.field_policy` looks its rules up by this name, so a tool declaring
#: `Client Ltd` as its object matches no policy rule, and default-deny then withholds every
#: field of every record it returns. That reads as a permission failure rather than as the
#: typo it is.
OBJECT_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class Entity(BaseModel):
    """A record with a type tag. Subclass per entity; the tag drives redaction."""

    model_config = ConfigDict(extra="forbid")

    entity: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)
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

    name: str = Field(min_length=1, max_length=80, pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=400)
    entity: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)
    args_schema: dict[str, Any] = Field(default_factory=dict)
    required_capability: str
    side_effect: SideEffect = SideEffect.NONE
    identity_mode: IdentityMode = IdentityMode.DELEGATED
    source: str = ""

    def is_read_only(self) -> bool:
        return self.side_effect is SideEffect.NONE

    def crosses_money_boundary(self) -> bool:
        return self.side_effect is SideEffect.MONEY
