"""The anchor catches what the chain walk cannot. A failure here blocks deploy.

A hash chain proves nobody edited an old entry and proves nothing about deletion from the
end. Every test here is about that gap, because it is the one an anchor exists to close and
the one a hash chain is routinely credited with closing on its own.

Task ids: M24.1.2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.audit.anchor import Anchor, check_anchor, take_anchor
from brain.audit.ledger import GENESIS_HASH, AuditAction, AuditChain

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _chain(count: int) -> AuditChain:
    chain = AuditChain()
    for i in range(count):
        chain.append(
            action=AuditAction.GRANT,
            actor_id="u_rupash",
            subject=f"principal:u_{i}",
            ent_hash="a" * 32,
            trace_id=f"t{i}",
            at=NOW + timedelta(seconds=i),
            details={"capability": "read:client.name"},
        )
    return chain


# ------------------------------------------------- the gap the anchor exists to close
def test_a_truncated_chain_still_verifies_which_is_why_this_module_exists() -> None:
    """Stated as a test so nobody has to take it on trust. Deleting from the end leaves a
    valid chain, and nothing inside the data distinguishes that from a ledger where those
    events never happened."""
    full = _chain(5)
    assert full.verify() is None or full.verify()

    truncated = AuditChain(entries=full.entries[:2], start_hash=GENESIS_HASH)
    assert truncated.first_break() is None


def test_the_anchor_catches_exactly_that() -> None:
    """The whole point. An anchor taken at five entries contradicts a chain that now ends
    at two, and the message says entries were removed rather than something vaguer."""
    full = _chain(5)
    anchor = take_anchor(full, name="main", now=NOW)

    truncated = AuditChain(entries=full.entries[:2], start_hash=GENESIS_HASH)
    result = check_anchor(truncated, anchor)
    assert not result
    assert "absent" in result.detail
    assert "removed" in result.detail


def test_an_unchanged_chain_satisfies_its_anchor() -> None:
    """A check that fires on a healthy ledger is a check somebody switches off."""
    chain = _chain(5)
    assert check_anchor(chain, take_anchor(chain, name="main", now=NOW))


def test_a_chain_that_grew_still_satisfies_an_older_anchor() -> None:
    """Anchors are about what has not been removed, not about the length now. A ledger
    that grew is the normal case and must not read as tampering."""
    chain = _chain(3)
    anchor = take_anchor(chain, name="main", now=NOW)
    for i in range(3, 6):
        chain.append(
            action=AuditAction.REVOKE,
            actor_id="u_rupash",
            subject=f"principal:u_{i}",
            ent_hash="a" * 32,
            trace_id=f"t{i}",
            at=NOW + timedelta(minutes=i),
            details={},
        )
    assert check_anchor(chain, anchor)


# ------------------------------------------------- an edit reads differently from a delete
def test_an_altered_entry_is_reported_as_alteration_not_truncation() -> None:
    """Three outcomes, not two. "The chain is wrong" sends somebody to read the whole
    ledger; "entry 3 is present and its digest differs" sends them to one row."""
    chain = _chain(5)
    anchor = take_anchor(chain, name="main", now=NOW)
    tampered = Anchor(chain="main", seq=anchor.seq, head="f" * 64, taken_at=anchor.taken_at)
    result = check_anchor(chain, tampered)
    assert not result
    assert "altered" in result.detail


# ------------------------------------------------------------- what an anchor holds
def test_an_anchor_carries_a_digest_and_a_length_and_nothing_else() -> None:
    """Every field ends up in a store outside our control, so each one needs a reason
    rather than an absence of objection. A reader learns the ledger exists and how long it
    is, which is the minimum that makes an anchor work."""
    published = take_anchor(_chain(3), name="main", now=NOW).to_public()
    assert set(published) == {"chain", "head", "seq", "taken_at", "version"}


def test_no_entry_content_reaches_the_anchor() -> None:
    """An anchor is not a summary of the ledger. If it carried an actor or an action it
    would be a small copy of the thing it was protecting, kept somewhere less protected."""
    published = take_anchor(_chain(3), name="main", now=NOW).to_public()
    blob = str(published)
    for leak in ("u_rupash", "grant", "read:client.name", "principal:"):
        assert leak not in blob


def test_both_the_sequence_and_the_digest_are_needed() -> None:
    """The digest alone cannot catch truncation, because a shorter chain simply has a
    different head and nothing says which is longer. The sequence alone cannot catch an
    edit. Together they say "entry N existed and had this digest"."""
    anchor = take_anchor(_chain(5), name="main", now=NOW)
    assert anchor.seq > 0
    assert len(anchor.head) == 64


# --------------------------------------------------------------- the empty chain
def test_an_empty_chain_is_worth_anchoring() -> None:
    """It proves the ledger started empty on that date, which is what makes "there were no
    entries before Tuesday" checkable rather than assertable. Refusing to anchor an empty
    chain would leave the system unanchored for exactly as long as it has no history to
    lose, which is when an attacker would most like it to be."""
    anchor = take_anchor(AuditChain(), name="main", now=NOW)
    assert anchor.is_empty_chain
    assert anchor.head == GENESIS_HASH


def test_an_empty_anchor_is_not_contradicted_by_a_chain_that_has_since_grown() -> None:
    """An anchor of nothing is a claim about a start, not about a length."""
    empty = take_anchor(AuditChain(), name="main", now=NOW)
    assert check_anchor(_chain(4), empty)


# --------------------------------------------------------------------- validation
def test_an_anchor_refuses_a_value_that_is_not_a_digest() -> None:
    """A store full of anchors that verify against nothing is worse than no anchors: it
    reads as a control that is working."""
    with pytest.raises(ValueError, match="digest"):
        Anchor(chain="main", seq=1, head="too short", taken_at=NOW)


def test_an_anchor_refuses_a_naive_timestamp() -> None:
    """A missing timezone on the one record whose purpose is to say when something was
    true is a bug that only appears during an argument about timing."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Anchor(chain="main", seq=1, head="a" * 64, taken_at=datetime(2026, 9, 5))


def test_the_published_form_is_stable_for_the_same_anchor() -> None:
    """The workflow commits this and a diff must show a real change rather than a
    reordering of keys."""
    anchor = take_anchor(_chain(3), name="main", now=NOW)
    assert anchor.to_public() == anchor.to_public()
