"""Recording the ledger's head somewhere the database administrator cannot reach.

The audit chain proves nobody edited an old entry: each entry's digest covers the one
before, so altering entry twelve invalidates twelve and everything after it. It proves
nothing at all about deletion from the end. Remove the newest twenty entries and what
remains verifies perfectly, because a chain has no idea how long it was meant to be, and
the newest entries are exactly the ones somebody covering their tracks would remove.

An anchor closes that. Write the head digest somewhere else, on a schedule, and "the ledger
now ends at 900 but Tuesday's anchor says it reached 1,240" becomes a detectable fact.

**The anchor is pulled, not pushed, and that is the whole design.** A scheduled job outside
the server reads this and records it. The alternative, having the server write its own
anchor to an external store, needs a write credential on the server, which is the one
machine the anchor is supposed to be independent of. Anyone who can delete audit entries
could then also write an anchor agreeing with the deletion.

**What an anchor may contain.** A digest and a sequence number, and nothing else. Not the
entry, not the actor, not the action. A reader of the anchor store learns that the ledger
existed and how long it was, which is the minimum that makes the anchor work.

Task ids: M24.1.2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from brain.audit.ledger import DIGEST_CHARS, GENESIS_HASH, AuditChain

#: The published shape. Deliberately tiny: every field here ends up in a public-ish store,
#: so each one needs a reason rather than an absence of objection.
ANCHOR_VERSION = 1


@dataclass(frozen=True)
class Anchor:
    """One recorded observation of where a chain had reached.

    `seq` is the length claim and `head` is the content claim, and both are needed. The
    digest alone cannot catch a truncation, because a shorter chain has a different head
    and nothing says which of the two is longer. The sequence number alone cannot catch an
    edit. Together they say "entry 1,240 existed and had this digest", which is exactly the
    statement a truncation contradicts.
    """

    chain: str
    seq: int
    head: str
    taken_at: datetime
    version: int = ANCHOR_VERSION

    def __post_init__(self) -> None:
        if len(self.head) != DIGEST_CHARS:
            raise ValueError(
                f"an anchor head is a {DIGEST_CHARS}-character digest, not {len(self.head)}"
            )
        if self.seq < 0:
            raise ValueError("an anchor sequence cannot be negative")
        if self.taken_at.tzinfo is None:
            # A naive timestamp on the one record whose purpose is to say *when* would be
            # a bug that only shows up in an argument about timing.
            raise ValueError("an anchor must be timezone-aware")

    @property
    def is_empty_chain(self) -> bool:
        """True when this anchors a chain with no entries yet.

        Worth anchoring anyway. An anchor taken before the first entry proves the ledger
        started empty on that date, which is what makes "there were no entries before
        Tuesday" a claim somebody can check rather than assert.
        """
        return self.head == GENESIS_HASH

    def to_public(self) -> dict[str, object]:
        """What the scheduled job publishes. Sorted, so two identical anchors are identical
        bytes and a diff shows a real change rather than a reordering."""
        return {
            "chain": self.chain,
            "head": self.head,
            "seq": self.seq,
            "taken_at": self.taken_at.astimezone(UTC).isoformat(),
            "version": self.version,
        }


def take_anchor(chain: AuditChain, *, name: str, now: datetime) -> Anchor:
    """Read the current head. Does not store it; storing is somebody else's job by design."""
    entries = chain.entries
    return Anchor(
        chain=name,
        seq=entries[-1].seq if entries else 0,
        head=chain.head(),
        taken_at=now,
    )


@dataclass(frozen=True)
class AnchorCheck:
    """The result of asking a chain whether it still covers what was anchored."""

    anchor: Anchor
    holds: bool
    detail: str

    def __bool__(self) -> bool:
        return self.holds


def check_anchor(chain: AuditChain, anchor: Anchor) -> AnchorCheck:
    """Whether this chain still contains the anchored entry, unchanged.

    Three outcomes and they are not the same, which is why this returns a sentence rather
    than a boolean:

    - the anchored entry is present and matches, so nothing was removed before it
    - the entry is present and its digest differs, which is tampering rather than truncation
    - the entry is absent, which is the truncation the anchor exists to catch

    An empty anchor is treated as holding when the chain is at least as long, because an
    anchor of nothing is a claim about a start rather than about a length.
    """
    if anchor.is_empty_chain:
        return AnchorCheck(anchor, True, "anchored an empty chain; nothing to contradict")

    if chain.covers_anchor(seq=anchor.seq, entry_hash=anchor.head):
        return AnchorCheck(anchor, True, f"entry {anchor.seq} present and unchanged")

    present = any(entry.seq == anchor.seq for entry in chain.entries)
    if present:
        return AnchorCheck(
            anchor,
            False,
            f"entry {anchor.seq} is present but its digest differs from the anchor "
            "taken at "
            f"{anchor.taken_at.isoformat()}; the entry was altered",
        )
    return AnchorCheck(
        anchor,
        False,
        f"entry {anchor.seq} was anchored at {anchor.taken_at.isoformat()} and is now "
        "absent; entries have been removed from the end",
    )
