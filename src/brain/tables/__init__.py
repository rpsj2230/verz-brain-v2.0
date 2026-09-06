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

`config.py` is the one module that mirrors nothing, and the exception is deliberate rather
than an omission. Configuration as data has no domain type above it because a setting is
whatever an operator needs to tune next; what it has instead is a declared value type and a
check constraint pinning the value to it, which is the same job done from the other end.

**Importing this package is what registers the tables.** `Base.metadata` is populated as a
side effect of the imports at the foot of this file, which is why they are here rather than
left to whoever needs a model. `migrations/env.py` imports this package for exactly that
reason, so `alembic revision --autogenerate` compares the database against the real
metadata - which only holds while the imports below stay exhaustive.

That is not a property anybody can be relied on to maintain by hand, and it has already
failed once: `routing.py` was added by a change that did not own this file, so for as long
as that lasted `import brain.tables` left three tables off the metadata and autogenerate
would have proposed dropping them. `tests/unit/test_tables.py` now imports this package in a
subprocess and compares what lands on the metadata against the tuple below, so a table
module that is never imported is a failing build rather than a quiet gap.

Task ids: M0.2.3, M1.1.5, M1.2.1, M1.2.2, M1.4.1, M1.4.3, M4.2.1, M24.1.1, M31.3.1.4
"""

from __future__ import annotations

# Imported for its side effect: `know.chunk` is declared in the module that reasons about
# how it is searched, so importing the package has to be what registers it. Without this
# line the table is absent from `Base.metadata` and autogenerate proposes dropping it.
from brain.knowledge import search as _search  # noqa: F401
from brain.tables.agent import AgentRow
from brain.tables.audit import AuditEntryRow
from brain.tables.chat import ConversationRow, MessageRole, MessageRow
from brain.tables.config import SettingRow, SettingType
from brain.tables.gate import (
    CapabilityGrantRow,
    CapabilityPackAssignmentRow,
    CapabilityPackRow,
    CapabilityRegistryRow,
    DepartmentRow,
    FieldPolicyRow,
    GrantsVersionRow,
    PolicyEpochRow,
    ScopeRow,
    TeamRow,
)
from brain.tables.identity import (
    DirectoryRoleGrantRow,
    PrincipalIdentityRow,
    PrincipalRow,
    SessionRow,
    one_of,
)
from brain.tables.projection import ProjectedRecordRow
from brain.tables.routing import ModelAttemptRow, RoutingRungRow, RoutingTierRow
from brain.tables.template import TemplateInstanceRow, TemplateVersionRow

#: Every table, in the order a migration must create them: a table appears after everything
#: it points at. The order is the migrations' own tuples end to end - 0002's seven, 0003's
#: nine, 0004's two, 0005's two, 0006's one, 0008's one, 0009's one, 0014's one - so
#: `tests/unit/test_tables.py` can compare this against their concatenation rather than
#: against a hand-maintained second copy. Each migration's downgrade reverses its own slice.
#: 0007 built no table: it widened two check constraints, which is why there is no slice for
#: it here, and neither did 0010 through 0013.
TABLES_IN_DEPENDENCY_ORDER: tuple[str, ...] = (
    # 0002_core_tables
    "auth.principal",
    "auth.principal_identity",
    "gate.capability_grant",
    "gate.capability_pack",
    "gate.capability_pack_assignment",
    "gate.field_policy",
    "obs.audit_entry",
    # 0003_resolver_and_tables
    "gate.scope",
    "gate.department",
    "gate.team",
    "auth.session",
    "gate.grants_version",
    "gate.policy_epoch",
    "ops.routing_tier",
    "ops.routing_rung",
    "ops.model_attempt",
    # 0004_capability_registry_and_config
    "gate.capability_registry",
    "ops.setting",
    # 0005_chat
    "chat.conversation",
    "chat.message",
    # 0006_directory_role_grant
    "auth.directory_role_grant",
    # 0008_projection
    "proj.record",
    # 0009_search. After the row plane and pointing at none of it: a chunk names its
    # document by id and the document plane owns no foreign key into the row plane.
    "know.chunk",
    # 0014_agent. References nothing, deliberately: the steward and the creator are plain
    # columns rather than foreign keys, so an agent outlives the account that built it.
    "agent.agent",
    # 0016_template. The instance follows the version it pins, which is the one foreign key
    # in this pair: an instance pinned to a manifest that does not exist is an agent nobody
    # can materialise. Neither points at `agent.agent`; see `brain.tables.template`.
    "agent.template_version",
    "agent.template_instance",
)

__all__ = [
    "TABLES_IN_DEPENDENCY_ORDER",
    "AgentRow",
    "AuditEntryRow",
    "CapabilityGrantRow",
    "CapabilityPackAssignmentRow",
    "CapabilityPackRow",
    "CapabilityRegistryRow",
    "ConversationRow",
    "DepartmentRow",
    "DirectoryRoleGrantRow",
    "FieldPolicyRow",
    "GrantsVersionRow",
    "MessageRole",
    "MessageRow",
    "ModelAttemptRow",
    "PolicyEpochRow",
    "PrincipalIdentityRow",
    "PrincipalRow",
    "ProjectedRecordRow",
    "RoutingRungRow",
    "RoutingTierRow",
    "ScopeRow",
    "SessionRow",
    "SettingRow",
    "SettingType",
    "TeamRow",
    "TemplateInstanceRow",
    "TemplateVersionRow",
    "one_of",
]
