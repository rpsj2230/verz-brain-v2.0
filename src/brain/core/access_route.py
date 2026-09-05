"""The one place a refusal becomes a request somebody can act on.

`brain.core.redaction` already holds the two ends of this: a `LockedField` is what the
asker sees where a field was withheld, and `AccessRequest`, `OwnerNotice` and
`ASKER_ACKNOWLEDGEMENT` are what an owner and an asker respectively receive. What was
missing was the middle: the function that turns the first into the second, decides who the
owner is, and is the only place that transition happens (M4.3.4).

**What breaks without it.** The lock is a dead end. Screen 3 shows a client record with
contract value marked Restricted and no way forward, so the person asks their manager on
WhatsApp, the manager forwards it to whoever they think owns finance data, and the grant
that eventually gets made is made in a chat thread with no record of what was asked or
why. The field-level permission model survives that, and the argument for it does not: a
company only tolerates fine-grained refusal if there is a visible way to ask.

**The information flows one way, and that is the entire design.** The owner learns
everything: who asked, the question in the asker's own words, and which capability would
answer it. The asker learns nothing, and nothing is meant literally. Not whether an owner
exists, not whether the field is classified, not whether the record exists, not whether
the request went anywhere at all. Every one of those is an oracle: "sent to the Finance
owner" names a department, "there is no owner for that field" says the field does not
exist, and either can be asked repeatedly with different guesses until the shape of the
company falls out. So the asker's half of this is a type with no fields in it, for the
same reason `render_lock` is a function with no parameters. A thing with nowhere to put an
answer cannot leak one.

**The owner is derived from the capability, never from the field.** A field is reachable
because a capability reaches it, and the person who can grant that capability is the only
person who can act on the request. Keying owners by field instead would let two fields
governed by one capability drift to two different owners, and then a request would arrive
at somebody who cannot grant it.

Scope: domain logic. Nothing here notifies anybody, writes a row or touches a channel. It
returns the two halves and lets the layers that own those jobs do them.

Task ids: M4.3.4
"""

from __future__ import annotations

from typing import Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from brain.core.entitlement import Capability
from brain.core.field_policy import FieldPolicy
from brain.core.redaction import (
    ASKER_ACKNOWLEDGEMENT,
    AccessRequest,
    LockedField,
    OwnerNotice,
)

log = structlog.get_logger()


class CapabilityOwner(BaseModel):
    """One capability, and the principal who can grant it.

    A principal id rather than a role or a department. A role is not a person and cannot
    approve anything, and a department is not a person either: routing to "finance" means
    routing to whoever happens to read that inbox, which is how a request sits unread for
    three weeks and the asker concludes the system does not work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: Capability
    principal_id: str = Field(min_length=1, max_length=128)


class OwnerDirectory(BaseModel):
    """Who owns which capability. A value passed in, not a table read.

    The same shape as `brain.core.field_policy.FieldPolicy` and for the same reasons: it is
    frozen, it indexes itself once at construction rather than scanning per lookup, and it
    is a value so that a caller can hold two of them at once and compare.

    A wildcard entry is honoured through `Capability.covers`, so `read:client.*` owned by
    one person covers every client field nobody named explicitly. Where several entries
    cover the same capability the most specific one wins, which is the only rule that lets
    a general owner be overridden for one sensitive field without rewriting the directory.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    owners: tuple[CapabilityOwner, ...] = ()

    _index: dict[str, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, _context: object, /) -> None:
        index: dict[str, str] = {}
        clashes: list[str] = []
        for entry in self.owners:
            existing = index.get(entry.capability.value)
            if existing is not None and existing != entry.principal_id:
                clashes.append(f"{entry.capability.value}: {existing} against {entry.principal_id}")
            index[entry.capability.value] = entry.principal_id
        if clashes:
            # Refused rather than resolved, for the reason `PolicyConflictError` gives: two
            # owners for one capability makes "who approves this" depend on the order rows
            # came back from a table, and the loser never learns a request existed.
            listed = "\n".join(f"  - {c}" for c in clashes)
            msg = f"owner directory names two owners for one capability:\n{listed}"
            raise ValueError(msg)
        self._index = index

    def owner_for(self, capability: Capability) -> str | None:
        """The principal who can grant this capability, or None.

        None is a real answer and the caller must be able to act on it without telling the
        asker. An unowned capability is an operations problem: somebody classified a field
        and nobody said who decides about it.

        Longest match wins. Length is a proxy for specificity that works because the
        capability grammar has one wildcard and it is a suffix, so a longer pattern that
        covers the same capability is necessarily narrower.
        """
        matches = [
            (owned, principal)
            for owned, principal in self._index.items()
            if Capability(value=owned).covers(capability)
        ]
        if not matches:
            return None
        return max(matches, key=lambda pair: len(pair[0]))[1]

    def __len__(self) -> int:
        return len(self._index)


class AskerAcknowledgement(BaseModel):
    """What the asker gets back. Identical for every request, by construction.

    **This model has no fields, and that is the mechanism rather than an oversight.** It is
    the same argument as `render_lock` taking no arguments: a reply that could vary by
    entity, field, owner or outcome would let two people compare replies and read the
    difference, and a reply with nowhere to put a difference cannot have one. Any two
    instances are equal, so a test can assert that the asker's half of a hundred different
    requests is one value.

    Rejected: a `text: str = ASKER_ACKNOWLEDGEMENT` field, which is the shape
    `brain.gate.ingress.Unrecognised` uses for its prompt. It is right there, where the
    prompt is the only thing the object carries and there is nothing else it could be
    confused with. Here it would be a field somebody sets, and the first thing anybody
    would want to set it to is "sent to the Finance owner".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def render(self) -> str:
        """The constant, whatever the request was about."""
        return ASKER_ACKNOWLEDGEMENT


class RoutedRequest(BaseModel):
    """Both halves of one routed refusal, kept apart.

    Two objects inside one, in the same shape and for the same reason as
    `brain.core.redaction.RedactedAnswer`: the thing that goes to the owner and the thing
    that goes to the asker must not be reachable through one variable by a caller who then
    sends whichever it happens to have.

    `notice` is None when nobody owns the capability. The asker's half is unchanged in that
    case, and unchanged is the point: an unowned field and a well-owned one produce byte
    identical replies, so a person cannot map which parts of the company have owners by
    asking about each in turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    notice: OwnerNotice | None = None
    #: Who the notice goes to. Carried here rather than on `OwnerNotice`, because the
    #: notice is what the owner reads and the owner already knows who they are. Whoever
    #: delivers it needs the address, and a routed request that named nobody would be
    #: delivered nowhere while looking like it had been handled.
    owner_id: str = ""
    acknowledgement: AskerAcknowledgement = Field(default_factory=AskerAcknowledgement)

    @model_validator(mode="after")
    def _a_notice_has_somewhere_to_go(self) -> Self:
        """A notice and an owner arrive together or not at all.

        The half-built shape is the one that fails silently: a notice with no owner id gets
        dropped by whatever tries to deliver it, the asker has already been told their
        request was passed on, and nobody finds out for a quarter.
        """
        if (self.notice is None) != (self.owner_id == ""):
            msg = (
                "a routed request carries a notice and an owner together or neither; "
                f"notice={'set' if self.notice else 'none'} owner_id={self.owner_id!r}"
            )
            raise ValueError(msg)
        return self

    def for_asker(self) -> str:
        """The only thing that may be said to the person who asked."""
        return self.acknowledgement.render()


def route_access_request(
    locked: LockedField,
    *,
    asker_id: str,
    question: str,
    policy: FieldPolicy,
    owners: OwnerDirectory,
) -> RoutedRequest:
    """Turn one lock into a request an owner can act on, and a reply that says nothing.

    It takes a `LockedField` rather than an entity and a field, and that is a constraint
    rather than a convenience. A lock is only ever offered on a record the caller was
    already entitled to see, so asking about it discloses nothing new. A record withheld
    whole produces no lock at all, which means this route cannot be pointed at one, and the
    rule that a refusal and an absence are the same event survives contact with a feature
    that would otherwise break it: "request access to the record you were not shown"
    confirms the record exists.

    Two ways there is no notice, and they are deliberately indistinguishable from outside.
    The policy may not classify the field, in which case no capability reaches it and there
    is nothing to request; or nobody owns the capability, in which case there is nobody to
    request it from. Both are logged, because both are somebody's job to fix, and neither
    changes a syllable of what the asker is told.

    The question is never logged. It is free text from a person and it will contain a
    client name, a figure or a colleague's name sooner rather than later, and a log line is
    the second-longest-lived artifact in the system after the trace. Only the owner sees
    it, because only the owner is deciding.
    """
    rule = policy.rule_for(locked.entity, locked.field)
    if rule is None:
        # The lock was rendered under a policy that classified this field and the policy
        # has since changed, because an unclassified field is never locked in the first
        # place. Failing closed here means the asker is told the same sentence as ever.
        log.warning("access_route.unclassified", entity=locked.entity, field=locked.field)
        return RoutedRequest()

    owner_id = owners.owner_for(rule.required_capability)
    if owner_id is None:
        log.warning(
            "access_route.unowned",
            entity=locked.entity,
            field=locked.field,
            capability=rule.required_capability.value,
        )
        return RoutedRequest()

    # The request is built through `AccessRequest` rather than by constructing an
    # `OwnerNotice` directly, so the domain object's own validation (an asker id that is
    # there, a question within bounds, names that are names) runs on every routed request
    # and not only on the ones somebody remembered to build longhand.
    request = AccessRequest(
        asker_id=asker_id,
        entity=locked.entity,
        field=locked.field,
        question=question,
        requested_capability=rule.required_capability,
    )
    log.info(
        "access_route.routed",
        entity=locked.entity,
        field=locked.field,
        owner_id=owner_id,
        capability=rule.required_capability.value,
    )
    return RoutedRequest(notice=request.for_owner(), owner_id=owner_id)
