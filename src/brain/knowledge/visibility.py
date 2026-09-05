"""How wide a piece of knowledge reaches, and why widening it is never a side effect.

Three levels, one store. Everything uploaded lands in the same knowledge layer, and what
differs between a working note and the company handbook is the scope predicate attached to
it. There is no second store per department, because a second store is a second permission
mechanism, and the two would disagree within a quarter.

**Uploading is not publishing.** A document added by somebody in Web stays inside Web's
scope, and widening it is a deliberate act with an approver, an owner and a review date
attached. Without that rule every upload silently widens the company's exposure and the
damage is invisible: nothing fails, nobody is told, and a year later nobody can say why the
whole company can read a client contract. `admit_upload` is where the rule lives, and it
refuses rather than quietly narrowing, for the reason `brain.core.redaction.redact` refuses
an opaque request it cannot honour: an uploader who asked for company visibility and
silently received department visibility believes the handbook is published and never checks.

**Promotion is a gated path, not a parameter.** `apply_promotion` cannot be called without
an `Approval`, and an `Approval` cannot be built without a second principal who holds the
capability and a review date. The approval also pins the level it was granted against, so
one cannot be kept and replayed against an item that has moved on since.

Two alternatives were rejected. A `visibility` column the uploader writes directly, which
is the obvious design and puts the whole gate in whatever form last touched the row. And a
boolean `is_public`, which cannot express the department level at all and therefore forces
every department document to be either private to one person or open to 126.

Nothing here reads a clock. `now` is a parameter for the reason
`brain.gate.provenance` gives: a rule about dates that reads the clock itself cannot be
tested at its own boundary, and the boundary is the part that goes wrong.

Task ids: M7.4.2, M7.4.3, M7.4.4
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.department import department_scope
from brain.core.entitlement import Capability, EntitlementSet
from brain.core.scope import Clause, Op, Scope

#: The field a personal scope tests. Named once here so the scope builder, the publishing
#: check and anything that later writes the column cannot drift into two spellings, which
#: is the shape of a bug that reads as "this person cannot see their own upload".
OWNER_FIELD: Final = "owner_id"

#: Who may widen something to a level they do not already own. A capability rather than a
#: role, because the architecture's promotion path is "a Department Admin proposing and a
#: Super Admin approving", and both of those are grants somebody holds in a scope.
PROMOTION_CAPABILITY: Final = Capability(value="approve:knowledge.visibility")


class VisibilityError(Exception):
    """A visibility change that would widen reach without going through the gate.

    Deliberately outside the `brain.core.errors` taxonomy, like `DepartmentError` and
    `PolicyConflictError`. Those five outcomes describe an answer to a person; this
    describes a refusal to write a row, and nobody asking a question ever sees it.
    """


class Visibility(enum.StrEnum):
    """Who a knowledge item is reachable by, before field-level redaction.

    Three levels rather than a scope written by hand, because these three are the ones a
    person can be asked to choose between in a form. A free-form predicate at upload time
    would be a permission model authored by whoever is uploading a PDF at the time.
    """

    #: Only the uploader, until published. Working notes, a draft proposal, a call
    #: transcript nobody has agreed to share yet.
    PERSONAL = "personal"
    #: That department's scope. The default for anything uploaded by somebody who has one.
    DEPARTMENT = "department"
    #: Everyone, still subject to field-level redaction. HR policy, brand guidelines, the
    #: standard price list.
    COMPANY = "company"


#: The order the three levels sit in, narrowest first. Written out rather than relying on
#: declaration order, for the reason `CLASSIFICATION_ORDER` is: declaration order is not
#: part of an enum's contract, and a reordering during a merge would silently invert every
#: widening check in this module while every test about the enum kept passing.
VISIBILITY_ORDER: Final[tuple[Visibility, ...]] = (
    Visibility.PERSONAL,
    Visibility.DEPARTMENT,
    Visibility.COMPANY,
)


def width(level: Visibility) -> int:
    """Position in `VISIBILITY_ORDER`. Higher reaches more people."""
    return VISIBILITY_ORDER.index(level)


def is_wider(candidate: Visibility, than: Visibility) -> bool:
    """True when moving to `candidate` would show the item to somebody new."""
    return width(candidate) > width(than)


def scope_for(level: Visibility, *, owner_id: str = "", department: str = "") -> Scope:
    """The row predicate a level means (M7.4.2).

    Each level demands exactly the identifier it needs, and refuses when it is missing.
    That refusal is the whole function. A personal scope built without an owner would be
    `Scope()`, which is the unrestricted scope, so the narrowest level in the system would
    turn into the widest one through a blank field on a form. The same mistake is available
    at department level and is the one `brain.core.department` refuses at admin assignment
    time; this is the same rule at knowledge level.
    """
    match level:
        case Visibility.PERSONAL:
            if not owner_id:
                msg = (
                    "a personal scope needs an owner; without one it is the unrestricted "
                    "scope, which is the widest level rather than the narrowest"
                )
                raise VisibilityError(msg)
            return Scope(clauses=(Clause(field=OWNER_FIELD, op=Op.EQ, value=owner_id),))
        case Visibility.DEPARTMENT:
            if not department:
                msg = (
                    "a department scope needs a department; without one it is the "
                    "unrestricted scope, and the item reaches the whole company"
                )
                raise VisibilityError(msg)
            return department_scope(department)
        case Visibility.COMPANY:
            return Scope.unrestricted()


def default_for_upload(uploader_department: str) -> Visibility:
    """What an upload gets when nobody chose anything.

    Department when the uploader has one, personal otherwise. Personal is not the default
    for everybody, because a default nobody can reach makes the knowledge layer useless and
    people work around it by emailing the file, which is worse than either level.
    """
    return Visibility.DEPARTMENT if uploader_department else Visibility.PERSONAL


def admit_upload(requested: Visibility | None, *, uploader_department: str) -> Visibility:
    """The level an upload is actually stored at (M7.4.3).

    Narrower than the default is allowed, because narrowing shows the item to nobody new.
    Wider is refused, and refused loudly: silently storing it at the default would leave the
    uploader believing the whole company can read something only their own team can, which
    is the failure that gets discovered by somebody asking why nobody read the handbook.

    There is no argument here for "publish now". Publishing is `propose_promotion` followed
    by an approval, and keeping the two paths separate is what makes the gate a gate rather
    than a flag with a stern name.
    """
    default = default_for_upload(uploader_department)
    if requested is None:
        return default
    if is_wider(requested, default):
        msg = (
            f"an upload cannot be stored at {requested} when the uploader's default is "
            f"{default}; widening is a promotion with an approver and a review date"
        )
        raise VisibilityError(msg)
    return requested


class PromotionProposal(BaseModel):
    """A request to widen one item, waiting for somebody else to agree (M7.4.4).

    Frozen, and carrying the level it was written against. Both matter to `apply_promotion`:
    a proposal that could be edited after approval is an approval of something else, and one
    that did not record where the item started could be applied to an item that has since
    been narrowed for a reason.

    `owner_id` is the steward the widened item will answer to, not the proposer. The
    architecture is explicit that knowledge ownership is a per-object stewardship relation,
    because a single global curator is one person curating everything, which is wrong and
    unstaffable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    from_level: Visibility
    to_level: Visibility
    proposer_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    #: When somebody must look at this again. Required, because a widening with no review
    #: date is permanent by default, and the architecture treats a review date nothing
    #: reads as documentation rather than a control.
    review_by: datetime
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("review_by")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        """A naive review date is a silent bug, as it is on `Principal.not_after`.

        Compared against `now` in a scheduled sweep running in UTC, a naive date is either
        eight hours early or eight hours late depending on where the machine is, and neither
        failure announces itself.
        """
        if v.tzinfo is None:
            msg = "review_by must be timezone-aware; a naive review date is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if not is_wider(self.to_level, self.from_level):
            msg = (
                f"{self.item_id!r} is already at {self.from_level} and this proposes "
                f"{self.to_level}; narrowing needs no approval and must not travel this path"
            )
            raise ValueError(msg)

    def digest(self) -> str:
        """A stable identity for exactly what was proposed.

        An approval carries this rather than a copy of the proposal, so that an approval and
        the thing it approved cannot come apart. Sorted fields are not needed because the
        order is fixed here; the separator is safe because none of the parts can contain a
        newline (the identifiers are bounded strings from a form, and the levels and the
        date are machine-generated).
        """
        parts = (
            self.item_id,
            self.from_level.value,
            self.to_level.value,
            self.proposer_id,
            self.owner_id,
            self.review_by.isoformat(),
            self.reason,
        )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def propose_promotion(
    *,
    item_id: str,
    from_level: Visibility,
    to_level: Visibility,
    proposer_id: str,
    owner_id: str,
    review_by: datetime,
    reason: str,
    now: datetime,
) -> PromotionProposal:
    """Write a proposal, refusing one whose review date has already passed.

    The date check needs `now` and therefore cannot live in the model, which is why it is
    here. A review date in the past is not a small mistake: the sweep that opens
    re-verification tasks would fire on the day the item was promoted, the owner would be
    asked to re-verify something nobody has read yet, and the second time that happens the
    notification stops being read.

    The proposal is built before the date is compared, and the order matters. A naive
    datetime cannot be compared with an aware one at all, so checking first turns a missing
    timezone into a `TypeError` from deep inside this function rather than into the model's
    own message about why a naive date is refused.
    """
    proposal = PromotionProposal(
        item_id=item_id,
        from_level=from_level,
        to_level=to_level,
        proposer_id=proposer_id,
        owner_id=owner_id,
        review_by=review_by,
        reason=reason,
    )
    if proposal.review_by <= now:
        msg = (
            f"review_by {review_by.isoformat()} is not in the future; "
            "a review date already behind us opens a task on the day of the promotion"
        )
        raise VisibilityError(msg)
    return proposal


class Approval(BaseModel):
    """A second principal agreeing to a specific proposal.

    Holds the digest rather than a second copy of the fields, so there is exactly one
    statement of what was approved and nothing to keep in step with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_digest: str = Field(min_length=32, max_length=32)
    approver_id: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    #: Repeated from the proposal so `apply_promotion` can check the item has not moved
    #: since, without being handed the proposal a second time.
    item_id: str = Field(min_length=1, max_length=128)
    from_level: Visibility
    to_level: Visibility


def approve_promotion(
    proposal: PromotionProposal,
    *,
    approver_id: str,
    entitlement: EntitlementSet,
    now: datetime,
) -> Approval:
    """The gate itself (M7.4.4). Three checks, and each one has been the whole failure.

    **The approver is not the proposer.** Self-approval is the gate defeated while leaving
    every audit record looking correct: there is a proposal, there is an approval, and they
    are the same person twice.

    **The approver holds the capability.** Absence of a grant is why this refuses; nothing
    subtracts. There is no deny list anywhere in this system, so an approver who should not
    be approving is one nobody granted, and revocation is deletion of the grant.

    **The entitlement belongs to the approver.** Without this check the caller passes a name
    in one argument and somebody else's reach in another, and the function dutifully
    approves on behalf of a person who was never asked. It is the kind of mistake that
    happens once, in a handler that had the wrong variable in scope.
    """
    if approver_id == proposal.proposer_id:
        msg = (
            f"{approver_id!r} proposed this promotion and cannot approve it; "
            "a gate one person passes alone is not a gate"
        )
        raise VisibilityError(msg)
    if entitlement.principal_id != approver_id:
        msg = (
            f"the entitlement offered belongs to {entitlement.principal_id!r}, not to the "
            f"approver {approver_id!r}; an approval must be made out of the approver's own reach"
        )
        raise VisibilityError(msg)
    if not entitlement.holds(PROMOTION_CAPABILITY, now):
        msg = (
            f"{approver_id!r} does not hold {PROMOTION_CAPABILITY.value}; "
            "nobody has granted them the promotion path"
        )
        raise VisibilityError(msg)
    return Approval(
        proposal_digest=proposal.digest(),
        approver_id=approver_id,
        approved_at=now,
        item_id=proposal.item_id,
        from_level=proposal.from_level,
        to_level=proposal.to_level,
    )


def apply_promotion(
    proposal: PromotionProposal, approval: Approval, *, current_level: Visibility
) -> Visibility:
    """The only way a level widens, and the last place a stale approval is caught.

    `current_level` is read from the item now rather than taken from the proposal, so an
    approval collected in June cannot be applied in September to an item that was narrowed
    in July. The proposal records where the item was; if it has moved, the agreement was
    about something that no longer exists and the widening starts again.
    """
    if approval.proposal_digest != proposal.digest():
        msg = (
            "this approval was given for a different proposal; an approval and the thing "
            "it approved must not come apart"
        )
        raise VisibilityError(msg)
    if current_level is not proposal.from_level:
        msg = (
            f"{proposal.item_id!r} is now at {current_level} and this was approved from "
            f"{proposal.from_level}; the item moved after the approval was given"
        )
        raise VisibilityError(msg)
    return approval.to_level


class KnowledgeVisibility(BaseModel):
    """A level and the identifiers it needs, resolved to one predicate.

    Kept as a value rather than as three loose arguments because the scope and the level
    have to agree, and two things that must agree are one thing with a constructor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Visibility
    owner_id: str = Field(default="", max_length=128)
    department: str = Field(default="", max_length=60)

    @classmethod
    def personal(cls, owner_id: str) -> Self:
        return cls(level=Visibility.PERSONAL, owner_id=owner_id)

    @classmethod
    def of_department(cls, department: str, *, owner_id: str = "") -> Self:
        return cls(level=Visibility.DEPARTMENT, department=department, owner_id=owner_id)

    @classmethod
    def company(cls, *, owner_id: str = "") -> Self:
        return cls(level=Visibility.COMPANY, owner_id=owner_id)

    def scope(self) -> Scope:
        """The predicate. Recomputed rather than stored, so it cannot disagree with the level."""
        return scope_for(self.level, owner_id=self.owner_id, department=self.department)

    def widened_to(self, level: Visibility, approval: Approval) -> Self:
        """A copy at the approved level. Never mutates, so both sides of a change are holdable."""
        applied = apply_promotion_level(self.level, level, approval)
        return type(self)(level=applied, owner_id=self.owner_id, department=self.department)


def apply_promotion_level(
    current: Visibility, requested: Visibility, approval: Approval
) -> Visibility:
    """`apply_promotion` without the proposal in hand, for a caller holding only the item.

    The digest check is not available here, so this checks the two facts the approval
    carries in the clear: that it was granted from where the item actually is, and that it
    was granted for the level being asked for. A caller that has the proposal should use
    `apply_promotion`, which checks all three.
    """
    if current is not approval.from_level:
        msg = (
            f"the item is at {current} and the approval was given from {approval.from_level}; "
            "the item moved after the approval was given"
        )
        raise VisibilityError(msg)
    if requested is not approval.to_level:
        msg = (
            f"the approval widens to {approval.to_level}, not to {requested}; "
            "an approval is for one level and cannot be spent on another"
        )
        raise VisibilityError(msg)
    return approval.to_level
