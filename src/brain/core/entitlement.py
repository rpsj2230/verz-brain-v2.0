"""Capabilities, and the set of them a principal holds.

A Capability is an atomic permission string. An EntitlementSet is every capability a
principal holds, each bound to the scope it applies in, reduced to a stable hash so a
cache key can encode "who was asking" without encoding who they were.

The one rule this module exists to enforce: entitlements are **additive only**. There is
no deny clause and no subtraction. A field is invisible because no grant covers it, never
because a rule removed it. Deny rules are what make permission systems unanswerable —
once you have them, "can X see Y" stops being a lookup and becomes an evaluation order
problem.

Task ids: M0.2.3, M0.2.4
"""

from __future__ import annotations

import hashlib
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.scope import Scope

#: verb:noun[.field] — lowercase, dot-separated, no wildcards except a trailing `.*`
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*|\.\*)*$")

VERBS = frozenset({"read", "write", "invoke", "approve", "admin"})


class Capability(BaseModel):
    """One atomic permission, e.g. `read:client.hours_remaining`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=3, max_length=200)

    @field_validator("value")
    @classmethod
    def _grammar(cls, v: str) -> str:
        if not CAPABILITY_RE.match(v):
            msg = f"capability {v!r} does not match verb:noun[.field] grammar"
            raise ValueError(msg)
        verb = v.split(":", 1)[0]
        if verb not in VERBS:
            msg = f"unknown verb {verb!r}; known verbs are {sorted(VERBS)}"
            raise ValueError(msg)
        return v

    @property
    def verb(self) -> str:
        return self.value.split(":", 1)[0]

    @property
    def noun(self) -> str:
        return self.value.split(":", 1)[1].split(".", 1)[0]

    def covers(self, other: Capability) -> bool:
        """True when holding self implies holding other.

        Only a trailing `.*` expands. `read:client.*` covers `read:client.name`;
        `read:client` does not, because an entity-level grant must not silently
        confer every field on it.
        """
        if self.value == other.value:
            return True
        if self.value.endswith(".*"):
            return other.value.startswith(self.value[:-1])
        return False


class Grant(BaseModel):
    """A capability bound to the scope it applies in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: Capability
    scope: Scope


class EntitlementSet(BaseModel):
    """Everything a principal holds. Additive only; there is no deny list by design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str
    grants: tuple[Grant, ...] = ()

    def scope_for(self, capability: Capability) -> Scope | None:
        """The scope in which this principal holds `capability`, or None.

        Where several grants cover it, the scopes are intersected. That is deliberately
        the conservative reading: holding a capability twice must never be wider than
        holding it once.
        """
        matched = [g.scope for g in self.grants if g.capability.covers(capability)]
        if not matched:
            return None
        result = matched[0]
        for s in matched[1:]:
            result = result.intersect(s)
        return result

    def holds(self, capability: Capability) -> bool:
        return self.scope_for(capability) is not None

    def intersect(self, ceiling: EntitlementSet) -> Self:
        """`E_run(caller, agent) = E(caller) ∩ agent_ceiling`.

        The core invariant of the whole platform. A run gets a capability only when the
        caller holds it *and* the agent's ceiling admits it, and the scope is the
        conjunction of both. An agent can therefore only ever narrow.
        """
        out: list[Grant] = []
        for g in self.grants:
            ceiling_scope = ceiling.scope_for(g.capability)
            if ceiling_scope is None:
                continue
            out.append(Grant(capability=g.capability, scope=g.scope.intersect(ceiling_scope)))
        return type(self)(principal_id=self.principal_id, grants=tuple(out))

    def ent_hash(self) -> str:
        """Stable, order-independent digest of this entitlement set.

        Used as part of a cache key so two callers with identical reach share a cached
        answer and two callers without it never can. Sorting before hashing is what makes
        it order-independent; without that, the same entitlement built in a different
        order would miss the cache and, worse, look like a different principal in traces.
        """
        parts = sorted(f"{g.capability.value}|{g.scope.model_dump_json()}" for g in self.grants)
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        return digest[:32]
