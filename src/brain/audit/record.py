"""The recording seam: one typed call per auditable action, and no other way in.

**What breaks without it.** The action vocabulary exists and nothing connects it to the code
that performs the actions. `AuditAction` is closed, `AuditChain.append` will write anything
it is handed, and between the two there is a gap that every auditable event has to be walked
across by hand. The failure that gap produces is always the same one: somebody adds a code
path, does not write an entry, and nothing anywhere notices, because the absence of an audit
entry looks exactly like an action that never happened. The ledger's own docstring names
this as the reason the vocabulary is closed. This module is the other half of that argument.

Three things make forgetting harder, and none of them is a comment asking people to
remember.

**One function per action, and no general one.** There is no `record(action, details)`. A
caller cannot pass a wrong-shaped detail set because a caller does not assemble a detail set
at all: they pass the things the action is made of, typed, and the shape is built here. A
grant takes a `Capability` and not a string, so "granted her everything" cannot be written
by passing a list, and the 5 September rule that one capability may be recorded and a list
may not is enforced by the signature before `redact_details` ever sees it.

**The subject kind is fixed by the method wherever the action fixes it.** A grant is about a
principal, a leash change is about an agent, a publish is about an artefact. Only `deny`
takes a subject kind, because a refusal genuinely can be about anything, and pretending
otherwise would push callers into writing `principal:` in front of an entity id.

**The identity of the run is bound once.** `actor_id`, `ent_hash` and `trace_id` come from
the request, not from each call site, so there is no argument order in which a grant gets
recorded under the wrong actor. `clock` is a callable rather than a default of
`datetime.now(UTC)`, for the reason `AuditChain.append` gives at length: application time is
the clock of whichever container served the write, and the ledger's ordering has to come
from one authoritative reading.

**What this module deliberately does not do** is wire itself into the identity, leash and
gate layers. Those are other files, and the direction of the dependency is the point: they
import this, and this imports nothing of theirs at runtime. The two enums it borrows for
typing are imported under `TYPE_CHECKING` only, so the audit package stays underneath the
layers that record into it and a future import of this module from `brain.gate` cannot
produce a cycle.

Task ids: M24.1.3
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from brain.audit.ledger import SUBJECT_KINDS, AuditAction, AuditEntry
from brain.core.entitlement import Capability

if TYPE_CHECKING:
    # Type-only, on purpose. A runtime import would put the audit package above the gate and
    # the identity layer, which are the things that will import this module; the cycle would
    # then arrive on the day somebody wired the recorder in, in a traceback about imports
    # rather than about audit. Rejected alternative: restating the rung and reason names
    # here as local strings, which is two definitions of one vocabulary, and the one that
    # gets updated is whichever the person was looking at.
    from brain.gate.injection import AutonomyTier
    from brain.identity.roles import BreakGlassReason


class DenyReason(enum.StrEnum):
    """Why a request was refused, as a token the ledger can hold.

    Closed for the same reason `AuditAction` is closed, and each member names a refusal path
    that exists in this codebase rather than a category somebody imagined. Every value is
    field-name shaped, so it survives `redact_details` intact: a free-text reason would be
    redacted to nothing, and an audit trail of `<redacted>` is not an audit trail.

    Note what this vocabulary is allowed to be, and where. `brain.core.errors` collapses
    DENIED into ABSENT before anything reaches a person, precisely so that a refusal cannot
    confirm that the thing exists. The ledger is the one place the real reason is written
    down, which is what that module means by "DENIED exists only so the audit log can record
    what actually happened". Showing it to somebody is the audit view's problem, and the
    view is entitlement-filtered.
    """

    #: No grant the principal holds covers the capability.
    NO_GRANT = "no_grant"
    #: A grant covers it and the row falls outside the grant's scope.
    OUT_OF_SCOPE = "out_of_scope"
    #: The principal's `not_after` has passed. The grants are still on file.
    PRINCIPAL_EXPIRED = "principal_expired"
    #: The leash rung for this agent, target and scope refused the action.
    LEASH_REFUSED = "leash_refused"
    #: The risk ceiling tightened autonomy below what the action needed.
    RISK_CEILING = "risk_ceiling"


class LedgerWriter(Protocol):
    """What the recorder needs from whatever holds the chain.

    A protocol rather than `AuditChain` itself, so the same recorder serves the in-memory
    chain the tests use and whatever eventually appends to `obs.audit_entry`, without this
    module learning what a database is. `AuditChain` satisfies it structurally; nothing had
    to be added to `ledger.py` to make that true.
    """

    def append(
        self,
        *,
        action: AuditAction,
        actor_id: str,
        subject: str,
        ent_hash: str,
        trace_id: str,
        at: datetime,
        details: Mapping[str, object] | None = None,
    ) -> AuditEntry: ...


#: Every method on `AuditRecorder` that writes an entry, and the action it writes.
#:
#: This exists to be asserted against `AuditAction`. A member added to the vocabulary with no
#: method here, or a method here that writes an action nobody declared, fails the invariant
#: that pins the pair - which is what turns "we must record every leash change" from a
#: sentence in a delivery document into something a build can refuse.
ACTION_BY_METHOD: Final[Mapping[str, AuditAction]] = MappingProxyType(
    {
        "grant": AuditAction.GRANT,
        "deny": AuditAction.DENY,
        "revoke": AuditAction.REVOKE,
        "leash_change": AuditAction.LEASH_CHANGE,
        "entity_merge": AuditAction.ENTITY_MERGE,
        "publish": AuditAction.PUBLISH,
        "break_glass": AuditAction.BREAK_GLASS,
    }
)


def subject(kind: str, ident: str) -> str:
    """`<kind>:<id>` in the ledger's grammar, refusing an unknown kind by name.

    `AuditEntry` would refuse it anyway. This refuses it one frame earlier and says which
    kinds exist, because the caller who gets this wrong is writing a new code path and the
    useful message is the list, not a regex.
    """
    if kind not in SUBJECT_KINDS:
        msg = f"subject kind {kind!r} is not one of {sorted(SUBJECT_KINDS)}"
        raise ValueError(msg)
    return f"{kind}:{ident}"


def _with_names(details: dict[str, object], key: str, names: Sequence[str]) -> None:
    """Attach a list of field names under `key`, or attach nothing.

    Nothing, rather than an empty list, because `redact_details` turns an empty sequence into
    `<redacted>` and a detail reading `<redacted>` tells a reader that something was hidden.
    An unrestricted scope hid nothing, and the honest way to say so is to say nothing.
    """
    if names:
        details[key] = tuple(sorted(names))


class AuditRecorder:
    """One request's worth of recording, bound to the actor doing it.

    Constructed per request from what the gate already knows: `GateContext` carries
    `principal.id`, `ent_hash` and `trace_id`, which are exactly the three fields bound here.
    It is not constructed *from* a `GateContext`, because that would make the audit package
    import the gate; the four values are passed in and the direction of the dependency stays
    the right way up.
    """

    def __init__(
        self,
        writer: LedgerWriter,
        *,
        actor_id: str,
        ent_hash: str,
        trace_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._writer = writer
        self._actor_id = actor_id
        self._ent_hash = ent_hash
        self._trace_id = trace_id
        self._clock = clock

    # ------------------------------------------------------------------ the seam

    def _write(
        self, action: AuditAction, subject_ref: str, details: Mapping[str, object]
    ) -> AuditEntry:
        """The single call into the ledger. Private, so no caller can name an action.

        Details are handed over unredacted and the ledger redacts them, rather than being
        assembled here to satisfy the redactor. The two rules about what may appear in a
        ledger row live in one place and this is not that place; anything here that turns
        out to be a value gets replaced rather than stored.
        """
        return self._writer.append(
            action=action,
            actor_id=self._actor_id,
            subject=subject_ref,
            ent_hash=self._ent_hash,
            trace_id=self._trace_id,
            at=self._clock(),
            details=details,
        )

    # ------------------------------------------------------------ the seven actions

    def grant(
        self,
        *,
        principal_id: str,
        capability: Capability,
        scope_fields: Sequence[str] = (),
    ) -> AuditEntry:
        """Record that a capability was given to a principal.

        `capability` is a `Capability` and not a string, and that single choice carries the
        5 September decision: one capability may be recorded because an audit view that
        cannot say what was granted is not an audit view, and a list may not be, because a
        list is the permission map. There is no parameter here that could take a list.

        `scope_fields` names the fields the grant's scope constrains, never their values.
        Rejected: recording the scope itself. `department=maintenance` looks exactly like a
        field name to `redact_details`, so a scope value would survive redaction by
        accident, and a scope clause naming a client would then be in the ledger for good.
        """
        details: dict[str, object] = {"capability": capability.value}
        _with_names(details, "scope_fields", scope_fields)
        return self._write(AuditAction.GRANT, subject("principal", principal_id), details)

    def deny(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        capability: Capability,
        reason: DenyReason,
    ) -> AuditEntry:
        """Record that a request was refused at runtime.

        The only method that takes a subject kind, because a refusal is about whatever was
        being reached for: a principal, an entity, an agent, a connector. The alternative was
        one method per kind, which is seven methods that differ by a string.
        """
        return self._write(
            AuditAction.DENY,
            subject(subject_kind, subject_id),
            {"capability": capability.value, "reason": reason.value},
        )

    def revoke(self, *, principal_id: str, capability: Capability) -> AuditEntry:
        """Record that a capability was taken away from a principal.

        Separate from `deny` because the ledger keeps them separate, and the ledger keeps
        them separate because "who removed her access, and when" is otherwise unanswerable
        without reading the details of every routine refusal in between.
        """
        return self._write(
            AuditAction.REVOKE,
            subject("principal", principal_id),
            {"capability": capability.value},
        )

    def leash_change(
        self,
        *,
        agent_id: str,
        target: str,
        from_rung: AutonomyTier,
        to_rung: AutonomyTier,
        scope_fields: Sequence[str] = (),
    ) -> AuditEntry:
        """Record that an agent's autonomy on a target changed.

        Both rungs, not just the new one. A leash entry saying only "now autonomous" cannot
        answer whether that was a loosening or the restoration of a rung somebody had
        tightened during an incident, and those are different events with different people
        to ask about them.

        `AutonomyTier` is an `IntEnum`, so `.name` is the readable half and `.value` is 0, 1
        or 2. The names are recorded: a ledger row reading `to_rung: 2` is a row somebody has
        to hold the enum alongside to read, five years after the enum moved.
        """
        details: dict[str, object] = {
            "target": target,
            "from_rung": from_rung.name.lower(),
            "to_rung": to_rung.name.lower(),
        }
        _with_names(details, "scope_fields", scope_fields)
        return self._write(AuditAction.LEASH_CHANGE, subject("agent", agent_id), details)

    def entity_merge(
        self,
        *,
        kept_entity_id: str,
        merged_entity_id: str,
        changed: Sequence[str] = (),
    ) -> tuple[AuditEntry, AuditEntry]:
        """Record that two entities were merged, as two entries: one per side.

        Two entries for one event, which reads like an inefficiency and is the only correct
        shape available. The ledger addresses an entry by its subject, and both sides of a
        merge have to be findable: somebody looking up the id that disappeared must not be
        told nothing happened to it. Putting the other id in `details` does not work, because
        `redact_details` admits field names and not identifiers, so `recuA1B2C3` would be
        stored as `<redacted>` and `c_0447` would survive by looking like a field name. A
        detail that survives only for some ids is worse than one that never does.

        Both entries share the recorder's trace id, so the pair is one event again to anybody
        querying by trace.
        """
        details: dict[str, object] = {}
        _with_names(details, "changed", changed)
        kept = self._write(AuditAction.ENTITY_MERGE, subject("entity", kept_entity_id), details)
        merged = self._write(AuditAction.ENTITY_MERGE, subject("entity", merged_entity_id), details)
        return kept, merged

    def publish(self, *, artifact_id: str, fields: Sequence[str] = ()) -> AuditEntry:
        """Record that an artefact was published.

        `fields` names what the artefact carried, by name. Names rather than a rendered
        artefact, or a digest of one, for the reason `changed_fields` gives: the ledger
        proves that something was published and what it was about, and the artefact's own
        store says what was in it. Two records, two retentions, two access controls.
        """
        details: dict[str, object] = {}
        _with_names(details, "fields", fields)
        return self._write(AuditAction.PUBLISH, subject("artifact", artifact_id), details)

    def break_glass(
        self,
        *,
        session_id: str,
        principal_id: str,
        reason: BreakGlassReason,
        authorised_by: str,
        notified: Sequence[str] = (),
    ) -> AuditEntry:
        """Record that a break-glass session was opened.

        The subject is the session and not the principal, which is what
        `BreakGlassSession.audit_subject` already says: the session is the thing with a
        start, an end and an authorisation, and the principal is one of its fields.

        The pieces are passed rather than the session object, so this module does not import
        the identity layer at runtime. `BreakGlassSession.audit_details` builds the same
        keys from the same values; if the two ever disagree the ledger holds two shapes for
        one event, which is a reason to converge them and not a reason for this module to
        reach upwards.
        """
        details: dict[str, object] = {
            "reason": reason.value,
            "principal": principal_id,
            "authorised_by": authorised_by,
        }
        _with_names(details, "notified", notified)
        return self._write(AuditAction.BREAK_GLASS, subject("session", session_id), details)
