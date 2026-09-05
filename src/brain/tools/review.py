"""The queue of skills waiting for somebody to read them.

A skill is instructions an agent follows with the tools its caller already holds. That is
the whole risk: a skill needs no permission of its own, because it borrows the reader's.
"Check the client's contract value and email the finance team" is a procedure, and if an
agent runs it for somebody who can do both, it happens - so the review is not a formality
about code quality, it is the only place a person decides whether a procedure should exist.

**The queue is ordered by how long something has waited, not by who submitted it.** A
priority field would be a way for whoever submits to jump the queue, and the person who most
wants their skill approved is exactly the person who would set it.

**A diff shows what changed, never a version number.** `diff_skills` names the fields, and a
review that showed "version 1.0.0 to 1.0.1" would let an author change the body while
bumping a patch number. The body is the part that matters and the version is the part an
author types.

**An approved skill that is then edited returns to the queue with no argument.**
`ImportedSkill.with_content` clears the review, and this module never re-approves anything:
there is no path here that takes something out of the queue except a person deciding.

Task ids: M12.2.6
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.tools.skills import ImportedSkill, Skill, SkillState, diff_skills

#: How long something may sit unreviewed before it is called out. Not an expiry: nothing is
#: auto-rejected, because an auto-rejection is a decision nobody made and the author would
#: simply resubmit. It is the line the queue view leads with.
STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class QueueEntry:
    """One thing waiting, and what a reviewer needs to decide about it.

    `changed` is empty for a first submission and holds field names for an edit. The
    distinction matters to a reviewer more than anything else on this type: a new skill has
    to be read in full, and an edit to an approved one needs only the changed fields read -
    which is the difference between a review that happens and one that is postponed.
    """

    skill: ImportedSkill
    waiting_since: datetime
    changed: tuple[str, ...] = ()

    @property
    def is_edit(self) -> bool:
        return bool(self.changed)

    def waited(self, now: datetime) -> timedelta:
        return now - self.waiting_since

    def is_stale(self, now: datetime) -> bool:
        return self.waited(now) >= STALE_AFTER


def pending(
    imported: Sequence[ImportedSkill],
    *,
    previous: dict[str, Skill] | None = None,
    submitted_at: dict[str, datetime] | None = None,
    now: datetime,
) -> tuple[QueueEntry, ...]:
    """Everything waiting for a decision, oldest first.

    **Oldest first, and there is no priority argument.** A priority field is a way for
    whoever submits to jump the queue, and the person who most wants their skill approved is
    exactly the person who would set it. Time waited is the one ordering nobody can game by
    caring more.

    Rejected skills are not here. A rejection is a decision, and a queue that showed
    decisions alongside things awaiting one would make the count meaningless - the number a
    person looks at is "how many are waiting for me".

    `previous` maps a skill name to the version last approved, so an edit can be shown as a
    diff. Absent, everything reads as a first submission, which is the safe direction: it
    asks for more reading rather than less.
    """
    prior = previous or {}
    when = submitted_at or {}
    entries: list[QueueEntry] = []
    for item in imported:
        if item.state is not SkillState.IMPORTED:
            continue
        name = item.skill.name
        was = prior.get(name)
        entries.append(
            QueueEntry(
                skill=item,
                # `now` when nobody recorded a submission time. Not the epoch: an unknown
                # time defaulting to 1970 would put every such entry at the top of a queue
                # ordered by age, which is the opposite of what an unknown means.
                waiting_since=when.get(name, now),
                changed=diff_skills(was, item.skill) if was is not None else (),
            )
        )
    return tuple(sorted(entries, key=lambda e: e.waiting_since))


def stale(entries: Sequence[QueueEntry], now: datetime) -> tuple[QueueEntry, ...]:
    """The ones that have waited too long.

    Reported rather than acted on. Nothing here auto-rejects: an auto-rejection is a
    decision nobody made, the author would resubmit, and the queue would be the same length
    with one more round trip in it.
    """
    return tuple(e for e in entries if e.is_stale(now))


@dataclass(frozen=True)
class QueueSummary:
    """What the console shows above the list. Counts only.

    No names. A summary naming the skills waiting would be readable by whoever can see the
    console, and a skill's name describes a procedure somebody wants to run - which is a
    fact about what a team is doing. The list below it is the place for names, and it is
    behind the same permission the console is.
    """

    waiting: int = 0
    edits: int = 0
    stale: int = 0

    @property
    def first_submissions(self) -> int:
        return self.waiting - self.edits


def summarise(entries: Sequence[QueueEntry], now: datetime) -> QueueSummary:
    return QueueSummary(
        waiting=len(entries),
        edits=sum(1 for e in entries if e.is_edit),
        stale=len(stale(entries, now)),
    )
