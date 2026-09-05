"""Every table in the system. Until this package existed there were none.

Everything this platform enforces was, before now, a rule about objects in memory: a
`Principal` that had to be constructed correctly, an `EntitlementSet` that intersected
properly, an `AuditEntry` that hashed over the right fields. All of it correct, none of it
durable. A gate that recomputes an entitlement from nothing on every request is a gate with
no grants in it, and a ledger that lives in a process ends when the process does.

**What breaks without this package.** Four modules' worth of leaves are blocked on somewhere
to put a row. A principal cannot be disabled, a channel identity cannot be bound, a grant
cannot be made or revoked, a field cannot be classified, and the audit trail - the one
artefact whose entire value is that it outlives whatever wrote it - does not survive a
restart.

**The direction of mirroring is one-way.** Every model here mirrors a type that already
exists in `brain.core`, `brain.gate` or `brain.audit`. The domain type is the definition;
the table is storage for it. Where the two could drift, the check constraint is generated
from the domain type's own constant - `one_of` in `identity.py` explains what that buys and
what it does not - so a change to an enum or a pattern shows up as a failing test rather
than as a row the database refuses at three in the morning.

**Importing this package is what registers the tables.** `Base.metadata` is populated as a
side effect of the imports at the foot of this file, which is why they are here rather than
left to whoever needs a model. Note what that does *not* fix: `migrations/env.py` imports
`brain.db` and never this package, so `alembic revision --autogenerate` compares the
database against an empty metadata and would propose creating all seven tables again. The
migrations in this repository are written by hand, so nothing is broken today; the import
belongs in `env.py` before anybody runs autogenerate in anger.

Task ids: M1.2.1, M1.2.2, M1.4.1, M1.4.3, M4.2.1, M24.1.1
"""

from __future__ import annotations

from brain.tables.audit import AuditEntryRow
from brain.tables.gate import (
    CapabilityGrantRow,
    CapabilityPackAssignmentRow,
    CapabilityPackRow,
    FieldPolicyRow,
)
from brain.tables.identity import PrincipalIdentityRow, PrincipalRow, one_of

#: Every table, in the order a migration must create them: a table appears after everything
#: it points at. `migrations/versions/0002_core_tables.py` keeps the same order and its
#: downgrade reverses it, which is the property `tests/unit/test_tables.py` checks.
TABLES_IN_DEPENDENCY_ORDER: tuple[str, ...] = (
    "auth.principal",
    "auth.principal_identity",
    "gate.capability_grant",
    "gate.capability_pack",
    "gate.capability_pack_assignment",
    "gate.field_policy",
    "obs.audit_entry",
)

__all__ = [
    "TABLES_IN_DEPENDENCY_ORDER",
    "AuditEntryRow",
    "CapabilityGrantRow",
    "CapabilityPackAssignmentRow",
    "CapabilityPackRow",
    "FieldPolicyRow",
    "PrincipalIdentityRow",
    "PrincipalRow",
    "one_of",
]
