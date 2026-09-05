"""One piece of knowledge, who vouched for it, and when somebody must look again.

Knowledge is distinguished from memory by authorship. A memory is something the system
worked out; a knowledge item is something a person wrote down and put their name to, which
is why every field here that memory does not have is about a human being answerable for the
content: an owner, a verifier, a date they verified it, and a date by which somebody must
check it is still true.

**A review date nothing reads is documentation, not a control.** That is the whole reason
`due_for_reverification` exists as a function rather than as a column somebody eyeballs in
the console. The same mistake is a circuit breaker with no scheduled caller: the mechanism
is present, correct, and never invoked.

**Supersession replaces, it does not delete.** A newer version marks the older superseded
and the older stays on file, so it is always possible to see what the company used to
believe and when that changed. Deleting it would make an answer given last month
unexplainable, and an answer nobody can explain is one nobody can correct.

**Supersession is not a promotion path.** The check that a successor is never wider than
the thing it replaces is the load-bearing line in this file. Without it, widening a
department SOP to the whole company is a matter of uploading version two with a different
level, and every control in `brain.knowledge.visibility` is bypassed by the ordinary
mechanism people use every week.

**The badge states what is on file and never argues with it.** An item nobody has verified
renders as unverified rather than as wrong. The two are different, and a badge that treated
them the same would train people to ignore it on the day it mattered.

Two alternatives were rejected. A stored `needs_review` flag, which goes stale the moment
the clock passes it and is then a control that is silently off; the state is derived from
`review_by` and a `now` that is always passed in. And a global curator role, rejected in the
architecture for the reason it is rejected here: one person curating everything is one
person doing nothing, so stewardship is per object.

The record here is the domain shape only. No SQLAlchemy model and no migration is written
in this package, the same division `brain.core.department` draws for its own table shapes.

Task ids: M7.4.1, M7.4.5, M7.4.6, M7.4.7
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.scope import Scope
from brain.knowledge.visibility import (
    KnowledgeVisibility,
    Visibility,
    VisibilityError,
    is_wider,
)

#: Identifier grammar for an item. The same shape as `brain.gate.provenance`'s reference
#: pattern, restated rather than imported so that this module's guarantee does not move when
#: somebody widens an unrelated one. A knowledge id ends up inside a citation, and a
#: citation that cannot be resolved is a citation nobody checks.
ITEM_ID_PATTERN: Final = r"^[A-Za-z0-9_.@-]{1,128}$"


class KnowledgeError(Exception):
    """A knowledge record that would be unsafe or unanswerable to write.

    Outside the `brain.core.errors` taxonomy for the reason `VisibilityError` is: those five
    outcomes describe an answer to a person, and this describes a refusal to write a row.
    """


class KnowledgeState(enum.StrEnum):
    """Where an item is in its life.

    Four states, and none of them is "needs review". Review is a question about the clock,
    answered by comparing `review_by` against a `now` that is passed in, so it cannot be
    stored without going stale. A stored state that quietly stops being true is worse than
    no state at all, because the console keeps rendering it.
    """

    #: Written, not yet vouched for. Retrievable only by its owner.
    DRAFT = "draft"
    #: In the knowledge layer and reachable by whatever its scope admits.
    PUBLISHED = "published"
    #: A newer version exists. Kept, never deleted, so an old answer stays explainable.
    SUPERSEDED = "superseded"
    #: Withdrawn deliberately. Distinct from superseded, because nothing replaced it and
    #: an asker who finds nothing should not be told a successor exists.
    ARCHIVED = "archived"


#: The states an item may be retrieved from. Written out rather than expressed as "not
#: superseded and not archived", so that adding a fifth state is a decision somebody makes
#: here rather than a silent inclusion in retrieval.
RETRIEVABLE_STATES: Final[frozenset[KnowledgeState]] = frozenset(
    {KnowledgeState.DRAFT, KnowledgeState.PUBLISHED}
)


class KnowledgeItem(BaseModel):
    """The knowledge record (M7.4.1): content, scope, owner, verification and state.

    Frozen. Every operation here returns a new item, so both sides of a change can be held
    at once, which is what a supersession and an audit record both need.

    The scope is derived from the visibility rather than stored beside it. Two fields that
    must agree are one field with a constructor: a stored predicate that disagreed with the
    stored level would make "who can read this" a question with two answers, and the wrong
    one is whichever the query happens to use.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1, max_length=128, pattern=ITEM_ID_PATTERN)
    #: What the item says. Held as text because everything in the knowledge layer is text
    #: by the time it is stored; the original file lives in the object store.
    content: str = Field(min_length=1)
    title: str = Field(default="", max_length=300)
    visibility: KnowledgeVisibility
    #: The steward, not the author. Whoever is answerable for this being right.
    owner_id: str = Field(min_length=1, max_length=128)
    state: KnowledgeState = KnowledgeState.DRAFT
    verified_by: str = Field(default="", max_length=128)
    verified_at: datetime | None = None
    review_by: datetime | None = None
    #: The item this one replaced, if any. Set by `supersede` and by nothing else.
    supersedes: str = Field(default="", max_length=128)

    @field_validator("verified_at", "review_by")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        """A naive date compared against a UTC `now` is wrong by the machine's offset.

        The same rule `Principal.not_after` enforces, and for the same reason: the error is
        silent, is a few hours wide, and only shows up as a review that fired on the wrong
        day.
        """
        if v is not None and v.tzinfo is None:
            msg = "knowledge dates must be timezone-aware; a naive date is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if bool(self.verified_by) != (self.verified_at is not None):
            # Half a verification is worse than none: it renders as verified while naming
            # nobody, or names somebody without saying when, and the badge looks authoritative
            # either way. The same rule `Anchor` applies to half a character span.
            msg = (
                f"{self.item_id!r} carries half a verification "
                f"(by={self.verified_by!r}, at={self.verified_at!r}); "
                "a verification is a person and a date, or it is nothing"
            )
            raise ValueError(msg)
        if (
            self.review_by is not None
            and self.verified_at is not None
            and self.review_by <= self.verified_at
        ):
            msg = (
                f"{self.item_id!r} is due for review at or before it was verified; "
                "a review date behind the verification opens a task the day it is written"
            )
            raise ValueError(msg)
        if (
            self.state is KnowledgeState.PUBLISHED
            and self.visibility.level is Visibility.COMPANY
            and self.verified_at is None
        ):
            # The one combination that must not exist. Company scope is the level everybody
            # reads without asking who wrote it, so an unverified item there is an anonymous
            # claim with the company's authority behind it.
            msg = (
                f"{self.item_id!r} is published to the whole company and nobody has "
                "verified it; company scope is the level nobody double-checks"
            )
            raise ValueError(msg)

    @property
    def scope(self) -> Scope:
        """The predicate deciding who reaches this. Derived, never stored twice."""
        return self.visibility.scope()

    @property
    def is_retrievable(self) -> bool:
        return self.state in RETRIEVABLE_STATES

    def verified(self, *, by: str, at: datetime, review_by: datetime | None = None) -> Self:
        """A copy carrying a verification. The only way `verified_by` is ever set."""
        return self.model_copy(
            update={"verified_by": by, "verified_at": at, "review_by": review_by}
        )


# --------------------------------------------------------- supersession (M7.4.5)


def supersede(
    predecessor: KnowledgeItem, successor: KnowledgeItem
) -> tuple[KnowledgeItem, KnowledgeItem]:
    """Replace one item with another, and return both.

    Both, always, and that is the point of the signature. A function that returned only the
    successor would leave the caller to remember to write the predecessor back, and the
    forgotten write leaves two live items saying different things with nothing recording
    which is current.

    Three refusals:

    An item cannot supersede itself, which is a loop nothing resolves and reads in the
    console as a document that replaced itself.

    An already-superseded item cannot be superseded again. Version three must replace
    version two, not version one, or two successors both claim to be current and retrieval
    returns whichever the index reached first.

    A successor may never be wider than what it replaces. Without this line, the whole of
    `brain.knowledge.visibility` is optional: upload version two of the department SOP at
    company visibility and it is published, with no proposal, no approver and no review
    date. Narrowing is allowed, because showing the item to fewer people needs no gate.
    """
    if predecessor.item_id == successor.item_id:
        msg = f"{predecessor.item_id!r} cannot supersede itself"
        raise KnowledgeError(msg)
    if predecessor.state is KnowledgeState.SUPERSEDED:
        msg = (
            f"{predecessor.item_id!r} is already superseded; a newer version replaces the "
            "current one, or two versions both claim to be current"
        )
        raise KnowledgeError(msg)
    if is_wider(successor.visibility.level, predecessor.visibility.level):
        msg = (
            f"{successor.item_id!r} is {successor.visibility.level} and replaces something "
            f"{predecessor.visibility.level}; supersession is not the promotion path"
        )
        raise VisibilityError(msg)
    return (
        predecessor.model_copy(update={"state": KnowledgeState.SUPERSEDED}),
        successor.model_copy(update={"supersedes": predecessor.item_id}),
    )


# --------------------------------------------------- re-verification (M7.4.6)


@dataclass(frozen=True)
class ReverificationTask:
    """One item whose review date has arrived, addressed to whoever owns it.

    Carries the owner rather than a department, because the architecture makes knowledge
    stewardship a per-object relation. A task addressed to a department is a task addressed
    to nobody.
    """

    item_id: str
    owner_id: str
    title: str
    review_by: datetime

    def message(self) -> str:
        """What the owner is asked. Names the item and the date, and nothing else.

        No count, and nowhere to put one. A nag that opened with "you have 14 items overdue"
        is dismissed as a batch, and the one that mattered goes with the rest.
        """
        named = self.title or self.item_id
        return f"{named} was due for review on {self.review_by.date().isoformat()}."


def due_for_reverification(
    items: Iterable[KnowledgeItem], *, now: datetime, lead_time: timedelta = timedelta()
) -> tuple[ReverificationTask, ...]:
    """The items a scheduled sweep should open tasks for (M7.4.6).

    A pure function over items the caller already holds. The scheduler that calls it on a
    cadence is not written here, and neither is the task store; what is here is the decision,
    which is the part that has to be right and the part a test can reach.

    `lead_time` opens the task early, so a monthly review can be done before it lapses rather
    than after. Zero by default, because a lead time that arrived without being asked for
    would make every item look overdue a week before it was.

    Superseded and archived items are never due. Asking somebody to re-verify a document
    that has already been replaced is the fastest way to teach them that these notifications
    are noise, and once that is learnt it applies to all of them.

    Sorted by date and then by id, so two runs over the same items produce the same list.
    An order that followed the input would make the console's list depend on whatever
    `ORDER BY` the query happened to carry.
    """
    threshold = now + lead_time
    due: list[ReverificationTask] = []
    for item in items:
        if item.review_by is None:
            continue
        if not item.is_retrievable:
            continue
        if item.review_by > threshold:
            continue
        due.append(
            ReverificationTask(
                item_id=item.item_id,
                owner_id=item.owner_id,
                title=item.title,
                review_by=item.review_by,
            )
        )
    return tuple(sorted(due, key=lambda task: (task.review_by, task.item_id)))


# ------------------------------------------------------- the badge (M7.4.7)


class VerificationState(enum.StrEnum):
    """What the badge on an answer says about the evidence behind it."""

    #: Verified by a named person, and the review date has not passed.
    VERIFIED = "verified"
    #: Verified, and past its review date. Still shown, and shown as needing a look.
    DUE = "due"
    #: Nobody has vouched for it. Not the same as wrong, and rendered as neither.
    UNVERIFIED = "unverified"
    #: A newer version exists. Retrieval should not have reached this, and if it did the
    #: reader is told rather than left to assume the item is current.
    SUPERSEDED = "superseded"


#: What each state says. A mapping rather than branches in the renderer, so that the four
#: sentences sit together where somebody reviewing the wording can read them at once.
BADGE_TEXT: Final[dict[VerificationState, str]] = {
    VerificationState.VERIFIED: "verified by {who} on {when}",
    VerificationState.DUE: "verified by {who} on {when}, due for review",
    VerificationState.UNVERIFIED: "not verified by anyone",
    VerificationState.SUPERSEDED: "replaced by a newer version",
}


@dataclass(frozen=True)
class VerificationBadge:
    """The badge shown beside an answer drawn from an item (M7.4.7).

    Carries no scope, no owner and no count. The scope is why the asker reached the item at
    all and repeating it back tells them about the permission model; a count of anything
    would be a count of things they did not see. What is left is what the badge is for: who
    vouched for this, when, and whether that is still current.
    """

    state: VerificationState
    verified_by: str = ""
    verified_at: datetime | None = None

    def render(self) -> str:
        template = BADGE_TEXT[self.state]
        if self.verified_at is None:
            return template
        return template.format(who=self.verified_by, when=self.verified_at.date().isoformat())


def badge(item: KnowledgeItem, *, now: datetime) -> VerificationBadge:
    """The badge for one item, as of `now`.

    Superseded is checked before verification, because a replaced item that was verified
    last year would otherwise render as verified, which is true and misleading: the reader
    needs to know a newer version exists far more than they need to know who signed the old
    one. Archived items render as their verification state, because nothing replaced them
    and there is nothing else to say.
    """
    if item.state is KnowledgeState.SUPERSEDED:
        return VerificationBadge(state=VerificationState.SUPERSEDED)
    if item.verified_at is None:
        return VerificationBadge(state=VerificationState.UNVERIFIED)
    overdue = item.review_by is not None and item.review_by <= now
    return VerificationBadge(
        state=VerificationState.DUE if overdue else VerificationState.VERIFIED,
        verified_by=item.verified_by,
        verified_at=item.verified_at,
    )


def retrievable(items: Sequence[KnowledgeItem]) -> tuple[KnowledgeItem, ...]:
    """The items retrieval may consider, in the order given.

    A filter rather than a count. It returns what survived and never reports how much did
    not, because the difference between the two numbers is exactly the thing a person is not
    allowed to learn by subtraction.
    """
    return tuple(item for item in items if item.is_retrievable)
