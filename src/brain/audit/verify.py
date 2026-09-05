"""The verification job: what a walk of the chain proves, and what it must not be read as
proving.

**What breaks without it.** The ledger's guarantee is that tampering *would be* detectable.
`AuditChain.verify` is a method somebody has to call, and until something calls it on a
schedule and writes down what it found, nothing has actually been detected. A hash chain
nobody walks is a promise, not a control.

Three decisions shape this module, and each of them is about honesty rather than mechanism.

**It verifies a window against a stored digest, never the whole ledger every run.** A job
that can only start at genesis gets slower every day and is eventually switched off, and a
control that is switched off protects nothing. `AuditChain` already accepts a `start_hash`
for exactly this. The previous run leaves a `Checkpoint` behind, the next run resumes from
it, and walking four years to check last month never happens.

**A checkpoint is a convenience and an anchor is evidence.** A checkpoint recorded in the
same database as the ledger is rewritable by anybody who can rewrite the ledger, so it
shortens the walk and proves nothing on its own. An `Anchor` is a digest recorded where the
database administrator cannot reach, and it is the only thing that closes the truncation
hole. The report keeps the two apart and never lets one stand in for the other.

**There is deliberately no attribute on the report called `verified`, `ok` or `passed`.**
That is the whole point of the module. A chain walk proves *continuity*: no entry inside the
window was quietly edited, removed or reordered. It does not prove *completeness*, because
deleting the newest entries leaves a shorter chain that verifies perfectly. A field named
`verified` would be read as the second while meaning only the first, and a dashboard showing
a green tick over a truncated ledger is worse than no dashboard, because somebody has now
been told the thing is fine. So the report says `continuous`, which is named for exactly what
was checked, and carries `caveats`, which is never empty.

Scope: this is domain logic, like `brain.audit.ledger` next door. Nothing here opens a
connection. What does not exist yet, and is named in the report rather than papered over:
the reader that loads a window of `obs.audit_entry` rows, the store the checkpoint is
written to, and the anchor store itself, which is an open decision (see
`docs/needs-rupash.md`, section 8) about *where* a digest lives rather than about how one is
checked.

Task ids: M24.1.2
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `Anchor` is imported rather than restated. It is already the right object - a digest, the
# sequence number it belongs to, who recorded it and where - and two anchor models would be
# two anchor stores the moment anybody wrote a second one. The dependency runs from the job
# to the export and not the other way, so there is no cycle.
from brain.audit.export import Anchor
from brain.audit.ledger import (
    DIGEST,
    GENESIS_HASH,
    IDENTIFIER,
    AuditChain,
    AuditEntry,
    BreakReason,
    ChainBreak,
)

# ------------------------------------------------------------------ the caveats

#: Fixed strings, carried on every report, for the reason `brain.audit.export.LIMITATIONS`
#: gives: a reader told that something is "verified" and not told the limits of the
#: verification has been misled by omission. These are the operator-facing half of the same
#: sentence, worded for somebody reading a job's output at eight in the morning.

BROKEN_CAVEAT: Final = (
    "the walk stopped at the first entry that did not hold; nothing after it was checked, "
    "so this report describes where the ledger stopped being trustworthy and not how much "
    "of it is wrong"
)

UNMOORED_CAVEAT: Final = (
    "no checkpoint was supplied and this window does not start at the first entry, so its "
    "start hash was taken from the same database as the entries; continuity with anything "
    "before this window is asserted rather than checked"
)

UNANCHORED_CAVEAT: Final = (
    "no anchor was checked, so this run proves continuity and not completeness: deleting "
    "the newest entries leaves a shorter chain that verifies perfectly, and nothing inside "
    "the data can see that"
)

ANCHORED_CAVEAT: Final = (
    "the anchor proves the ledger still holds the entry it names, unchanged; entries "
    "written after that sequence number rest on the chain walk alone"
)

ANCHOR_MISSING_CAVEAT: Final = (
    "an anchor names an entry this window does not hold unchanged, which is what a removed "
    "or rewritten tail looks like; treat this as a finding and not as a misconfigured job "
    "until the window has been checked"
)

UNCHECKED_ANCHOR_CAVEAT: Final = (
    "one or more anchors name entries older than this window and were not checked here; "
    "they are the business of the run that covered them"
)


class Baseline(enum.StrEnum):
    """What the window's first link was checked against.

    Three states rather than a boolean, because "we started somewhere" hides the difference
    between a run that resumed from a digest and a run that took the database's word for
    where it was.
    """

    #: The window starts at entry 0 and its first link was checked against genesis.
    GENESIS = "genesis"
    #: The window resumed from a digest a previous run recorded.
    CHECKPOINT = "checkpoint"
    #: No checkpoint, and the window does not start at the beginning. The start hash came
    #: from the same rows being checked, so it can only ever agree with them.
    UNMOORED = "unmoored"


class Completeness(enum.StrEnum):
    """What, if anything, was proved about entries that should be here and are not."""

    #: Every anchor this window was responsible for is present, unchanged.
    ANCHORED = "anchored"
    #: No anchor was checked. Completeness is unknown, which is not the same as fine.
    UNANCHORED = "unanchored"
    #: An anchor named an entry the window does not hold unchanged.
    ANCHOR_MISSING = "anchor_missing"


class Checkpoint(BaseModel):
    """Where a previous run stopped, so the next one does not start at the beginning.

    Explicitly *not* an `Anchor`, and the difference is the whole reason both exist. A
    checkpoint is written by us, wherever our own state lives, and anybody able to rewrite
    the ledger can rewrite it to match. It buys a short walk and nothing else. An anchor is
    written where we cannot reach it afterwards, and it is what makes a missing tail
    visible. Merging the two types would let a cheap thing be read as an expensive one.

    `recorded_by` is an identifier rather than free text, for the reason
    `brain.audit.ledger.LegalHold.reason_code` gives: any field on an audit artefact that
    accepts prose is a field somebody eventually writes a name into.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The last sequence number the previous run checked, and found whole.
    through_seq: int = Field(ge=0)
    #: That entry's digest. The next window's first entry must name it as its parent.
    entry_hash: str = Field(pattern=DIGEST)
    recorded_at: datetime
    recorded_by: str = Field(pattern=IDENTIFIER)

    @field_validator("recorded_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "recorded_at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v


class VerificationReport(BaseModel):
    """What one run of the job found, and what it did not look at.

    Every consistency rule below is enforced here rather than left to the function that
    builds it, because a report can also be constructed by hand - by a test, by a fixture,
    by whatever eventually stores and reloads these - and a report that says one thing in
    its verdict and another in its parts is read as whichever the reader looked at first.
    `brain.audit.export.ExportManifest` refuses the same disagreement for the same reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    entry_count: int = Field(ge=0)
    first_seq: int | None = None
    last_seq: int | None = None
    start_hash: str = Field(pattern=DIGEST)
    head: str = Field(pattern=DIGEST)

    baseline: Baseline
    #: The chain held from the start hash to the head. Named for what was checked. This is
    #: not a synonym for "the ledger is complete"; see the module docstring.
    continuous: bool
    #: Present exactly when `continuous` is false.
    break_found: ChainBreak | None = None

    completeness: Completeness
    #: The anchors this window was responsible for and did check.
    anchors_checked: tuple[Anchor, ...] = ()
    #: Anchors older than this window. Not this run's business, and named so that nobody
    #: reads their absence from `anchors_checked` as a silent pass.
    anchors_before_window: tuple[Anchor, ...] = ()

    @field_validator("checked_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "checked_at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if self.continuous != (self.break_found is None):
            msg = "continuous and break_found disagree; the report would mislead its reader"
            raise ValueError(msg)
        if (self.entry_count == 0) != (self.first_seq is None):
            msg = "an empty window has no first_seq, and a non-empty one has one"
            raise ValueError(msg)
        if (self.first_seq is None) != (self.last_seq is None):
            msg = "first_seq and last_seq are both present or both absent"
            raise ValueError(msg)
        if self.completeness is Completeness.UNANCHORED and self.anchors_checked:
            msg = "completeness says no anchor was checked, and anchors_checked lists some"
            raise ValueError(msg)
        if self.completeness is not Completeness.UNANCHORED and not self.anchors_checked:
            msg = "completeness claims an anchor verdict with no anchor behind it"
            raise ValueError(msg)

    @property
    def caveats(self) -> tuple[str, ...]:
        """What this run did not check, in words, and never empty.

        A derived property rather than a field, which is the guard that matters. A field
        could be constructed empty, or dropped by a caller assembling a summary, and the
        report would then say "continuous" with nothing beside it - which is exactly the
        reading the module exists to prevent. Deriving it means suppressing a caveat is an
        edit to this function, in front of the test that asserts it is here.
        """
        out: list[str] = []
        if not self.continuous:
            out.append(BROKEN_CAVEAT)
        if self.baseline is Baseline.UNMOORED:
            out.append(UNMOORED_CAVEAT)
        match self.completeness:
            case Completeness.ANCHORED:
                out.append(ANCHORED_CAVEAT)
            case Completeness.UNANCHORED:
                out.append(UNANCHORED_CAVEAT)
            case Completeness.ANCHOR_MISSING:
                out.append(ANCHOR_MISSING_CAVEAT)
        if self.anchors_before_window:
            out.append(UNCHECKED_ANCHOR_CAVEAT)
        return tuple(out)

    def next_checkpoint(self, *, recorded_by: str) -> Checkpoint | None:
        """The digest to store so the next run resumes here, or None when there is nothing
        safe to store.

        None in three cases, and the third is the interesting one. An empty window has
        nothing new to record. A broken window must not become the new baseline, or the
        next run starts *after* the tamper and reports clean forever. A window whose anchor
        came back missing must not either: pinning a baseline there blesses a truncated
        ledger as the normal length, which is the failure the anchor was bought to catch.
        """
        if not self.continuous or self.last_seq is None:
            return None
        if self.completeness is Completeness.ANCHOR_MISSING:
            return None
        return Checkpoint(
            through_seq=self.last_seq,
            entry_hash=self.head,
            recorded_at=self.checked_at,
            recorded_by=recorded_by,
        )

    def summary(self) -> str:
        """One operator-readable block. The caveats are part of it, not a footnote.

        Rejected: a one-line summary with the caveats available separately. The line is what
        gets pasted into a ticket and the separate thing is what gets left behind.
        """
        window = (
            f"seq {self.first_seq}..{self.last_seq}" if self.first_seq is not None else "no entries"
        )
        if self.break_found is None:
            state = "continuous"
        else:
            state = (
                f"broken at index {self.break_found.index} "
                f"(seq {self.break_found.seq}, {self.break_found.reason})"
            )
        lines = [
            f"audit window {window}: {self.entry_count} entries, "
            f"baseline {self.baseline}, {state}, completeness {self.completeness}"
        ]
        lines.extend(f"  caveat: {caveat}" for caveat in self.caveats)
        return "\n".join(lines)


def verify_window(
    entries: Sequence[AuditEntry],
    *,
    at: datetime,
    checkpoint: Checkpoint | None = None,
    anchors: Iterable[Anchor] = (),
) -> VerificationReport:
    """Walk one window and report what held.

    `entries` is a contiguous run of the ledger, oldest first, as loaded from wherever the
    rows live. The start hash is derived here rather than accepted from the caller: a caller
    who supplies both the entries and the digest they are checked against can supply one
    that agrees with them, and the job would then verify the caller's arithmetic rather than
    the ledger.

    `at` has no default and `datetime.now()` is deliberately not one, for the reason
    `AuditChain.append` gives about its own timestamp. This one is milder - a verification
    run is not inside a digest - but the date on a report that is later read out as evidence
    should be one authoritative clock's reading and not whichever container ran the job.
    """
    window = tuple(entries)
    supplied = tuple(anchors)

    if checkpoint is not None:
        baseline = Baseline.CHECKPOINT
        start_hash = checkpoint.entry_hash
    elif window and window[0].seq == 0:
        baseline = Baseline.GENESIS
        start_hash = GENESIS_HASH
    elif window:
        # Rejected: refusing outright. A window with nothing to check its first link against
        # is a real thing an operator will ask for - "show me last month" against a ledger
        # whose earlier runs were never checkpointed - and refusing it teaches people to
        # pass a checkpoint they invented. Reporting it is the honest middle.
        baseline = Baseline.UNMOORED
        start_hash = window[0].prev_hash
    else:
        # Empty, and no checkpoint. Indistinguishable from an empty ledger, so it is
        # reported as the weaker of the two readings rather than the flattering one.
        baseline = Baseline.UNMOORED
        start_hash = GENESIS_HASH

    break_found = _join_break(window, checkpoint)
    chain = AuditChain(window, start_hash=start_hash)
    if break_found is None:
        break_found = chain.first_break()

    checked, before = _partition_anchors(supplied, window=window, checkpoint=checkpoint)
    if not checked:
        completeness = Completeness.UNANCHORED
    elif all(chain.covers_anchor(seq=a.seq, entry_hash=a.entry_hash) for a in checked):
        completeness = Completeness.ANCHORED
    else:
        completeness = Completeness.ANCHOR_MISSING

    return VerificationReport(
        checked_at=at,
        entry_count=len(window),
        first_seq=window[0].seq if window else None,
        last_seq=window[-1].seq if window else None,
        start_hash=start_hash,
        head=chain.head(),
        baseline=baseline,
        continuous=break_found is None,
        break_found=break_found,
        completeness=completeness,
        anchors_checked=checked,
        anchors_before_window=before,
    )


def _join_break(window: Sequence[AuditEntry], checkpoint: Checkpoint | None) -> ChainBreak | None:
    """Whether the window starts where the checkpoint said the ledger had got to.

    Redundant with the link check, which would also catch this, and worth doing anyway for
    the reason `AuditChain.first_break` checks sequence before digest: "seq jumped from 4 to
    9" reads as missing rows and sends an operator to the retention job, where "digest
    mismatch" reads as a tamper and sends them somewhere else entirely.
    """
    if checkpoint is None or not window:
        return None
    expected = checkpoint.through_seq + 1
    if window[0].seq == expected:
        return None
    return ChainBreak(
        index=0,
        seq=window[0].seq,
        reason=BreakReason.SEQUENCE_BROKEN,
        expected=str(expected),
        actual=str(window[0].seq),
    )


def _partition_anchors(
    supplied: Sequence[Anchor],
    *,
    window: Sequence[AuditEntry],
    checkpoint: Checkpoint | None,
) -> tuple[tuple[Anchor, ...], tuple[Anchor, ...]]:
    """Split anchors into the ones this window is answerable for and the ones it is not.

    The rule is asymmetric on purpose, and the asymmetry is the mechanism working.

    An anchor *older* than the window belongs to an earlier run. Reporting it as missing
    here would be a verifier crying wolf on every windowed run, and a verifier that cries
    wolf is one somebody switches off - which the ledger's own invariant suite says in as
    many words.

    An anchor at or after the window's first entry must be inside it, unchanged. That
    includes an anchor whose sequence number is *higher* than anything in the window, and
    that case is the entire reason anchors exist: a ledger whose newest entries were deleted
    looks exactly like a window that happens to end early. Classing it as "not checked"
    would file the truncation signal under housekeeping.
    """
    floor = window[0].seq if window else (checkpoint.through_seq + 1 if checkpoint else 0)
    checked = tuple(a for a in supplied if a.seq >= floor)
    before = tuple(a for a in supplied if a.seq < floor)
    return checked, before
