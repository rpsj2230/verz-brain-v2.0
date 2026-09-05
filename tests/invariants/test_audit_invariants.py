"""The audit rules that must never break. A failure here blocks deploy.

Two families of rule, and they pull in opposite directions, which is why they are tested
side by side.

The first is that the ledger must be able to prove it has not been edited: altering,
removing or reordering an entry has to be visible, and an honest append must never be
mistaken for any of those. A verifier that cries wolf is a verifier somebody switches off,
so the false-positive case is as load-bearing as the detection cases.

The second is that the ledger must not become the thing it exists to police. It is the
longest-retained and most widely read table in the system, so a value that reaches it is a
value that outlives every retention policy and is visible to everyone with audit access.
These tests ask for the wrong data and fail if it arrives, in the manner of the permission
canaries next door.

Task ids: M24.1.1, M24.1.2, M24.1.3, M24.1.4, M24.2.1
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from brain.audit.ledger import (
    DIGEST_CHARS,
    GENESIS_HASH,
    REDACTED,
    AuditAction,
    AuditChain,
    AuditEntry,
    BreakReason,
    LegalHold,
)
from tests.fixtures.company import CANARIES, NOW, canary_tokens, person

pytestmark = pytest.mark.invariant

ENT = person("u_weiling").entitlement().ent_hash()


def a_chain(count: int = 5) -> AuditChain:
    chain = AuditChain()
    for i in range(count):
        chain.append(
            action=AuditAction.GRANT,
            actor_id="u_rupash",
            subject=f"principal:u_subject{i}",
            ent_hash=ENT,
            trace_id=f"trace{i}",
            at=NOW + timedelta(minutes=i),
            details={"hours_remaining": "hours_remaining"},
        )
    return chain


def a_hold(**kw: object) -> LegalHold:
    base: dict[str, object] = {
        "id": "hold_1",
        "reason_code": "pending_litigation",
        "placed_at": NOW - timedelta(days=1),
    }
    return LegalHold(**(base | kw))  # type: ignore[arg-type]


# ----------------------------------------------------- the chain holds (M24.1.1)
def test_the_digest_covers_every_field_an_entry_carries() -> None:
    """A field outside the digest is a field that can be edited without trace, which is
    worse than not recording it: the entry looks verified and says the wrong thing.

    The set comparison is the part that keeps working. Adding a field to AuditEntry and
    forgetting to hash it fails here, naming the field, rather than producing a ledger
    with one quietly editable column that nothing ever notices.
    """
    base = a_chain(1).entries[0]
    variations: dict[str, object] = {
        "seq": base.seq + 1,
        "at": base.at + timedelta(seconds=1),
        "actor_id": "u_somebody_else",
        "action": AuditAction.BREAK_GLASS,
        "subject": "principal:u_somebody_else",
        "ent_hash": "0" * 32,
        "trace_id": "another_trace",
        "details": {"margin": REDACTED},
        "prev_hash": "f" * DIGEST_CHARS,
    }
    assert set(variations) | {"entry_hash"} == set(AuditEntry.model_fields)

    for name, value in variations.items():
        altered = base.model_copy(update={name: value})
        assert altered.recompute_hash() != base.entry_hash, f"{name} is not inside the digest"


def test_altering_a_field_in_the_middle_of_the_chain_is_detected() -> None:
    """The naive tamper: change a row, leave its digest alone."""
    entries = list(a_chain(5).entries)
    entries[2] = entries[2].model_copy(update={"subject": "principal:u_somebody_else"})
    broken = AuditChain(entries)

    assert broken.verify() == 2
    found = broken.first_break()
    assert found is not None
    assert found.reason is BreakReason.CONTENT_ALTERED


def test_altering_an_entry_and_recomputing_its_digest_is_detected_at_the_next_entry() -> None:
    """The tamper that a per-entry hash would miss, and the reason the chain exists.

    Anyone editing a row can also recompute that row's own digest; then the entry agrees
    with itself perfectly. What they cannot do without rewriting the rest of the table is
    make the *next* entry's prev_hash agree, because that digest was fixed when the next
    entry was written. So one edit anywhere invalidates everything after it.
    """
    entries = list(a_chain(5).entries)
    altered = entries[2].model_copy(update={"subject": "principal:u_somebody_else"})
    entries[2] = altered.model_copy(update={"entry_hash": altered.recompute_hash()})
    broken = AuditChain(entries)

    # entry 2 is now internally consistent, and entry 3 is what gives it away
    assert broken.entries[2].recompute_hash() == broken.entries[2].entry_hash
    assert broken.verify() == 3
    found = broken.first_break()
    assert found is not None
    assert found.reason is BreakReason.LINK_BROKEN


def test_deleting_an_entry_is_detected() -> None:
    """Deletion changes no entry's content, so only the link and the sequence catch it.
    Both are checked, because the reason an operator is given decides where they look."""
    chain = a_chain(5)

    from_the_middle = AuditChain([e for e in chain.entries if e.seq != 2])
    assert from_the_middle.verify() == 2
    middle_break = from_the_middle.first_break()
    assert middle_break is not None
    assert middle_break.reason is BreakReason.SEQUENCE_BROKEN

    from_the_front = AuditChain(chain.entries[1:])
    assert from_the_front.verify() == 0
    front_break = from_the_front.first_break()
    assert front_break is not None
    assert front_break.reason is BreakReason.LINK_BROKEN


def test_reordering_two_entries_is_detected() -> None:
    """Order is the whole meaning of an audit trail: the same events in a different order
    describe a different sequence of decisions."""
    entries = list(a_chain(5).entries)
    entries[2], entries[3] = entries[3], entries[2]

    assert AuditChain(entries).verify() == 2


def test_appending_legitimately_is_never_flagged() -> None:
    """The false-positive case, and it matters as much as the others. A verifier that
    reports a break on an honest chain is a verifier somebody switches off, and then none
    of the tests above protect anything."""
    chain = a_chain(3)
    assert chain.verify() is None

    for i in range(3, 40):
        chain.append(
            action=AuditAction.DENY,
            actor_id="u_weiling",
            subject=f"principal:u_subject{i}",
            ent_hash=ENT,
            trace_id=f"trace{i}",
            at=NOW + timedelta(minutes=i),
        )
        assert chain.verify() is None, f"an honest append was flagged at {i}"

    assert len(chain) == 40


def test_truncating_the_tail_is_invisible_to_the_chain_and_visible_to_an_anchor() -> None:
    """A limitation, written down as a test so it cannot be forgotten.

    Remove the newest entries and what remains is a valid chain that simply ends earlier.
    Nothing inside the data distinguishes that from a ledger where those events never
    happened, so a hash chain on its own does not prove completeness, only continuity.

    The only thing that closes it is a digest recorded somewhere the database
    administrator does not control, and then asked for again later. That is what `head`
    produces and `covers_anchor` checks, and this test is the difference between the two.
    """
    chain = a_chain(5)
    anchor_seq = chain.entries[4].seq
    anchor_hash = chain.entries[4].entry_hash

    truncated = AuditChain(chain.entries[:3])
    assert truncated.verify() is None  # the chain alone sees nothing wrong
    assert not truncated.covers_anchor(seq=anchor_seq, entry_hash=anchor_hash)
    assert chain.covers_anchor(seq=anchor_seq, entry_hash=anchor_hash)


# -------------------------------------------- what must be recorded (M24.1.3)
def test_the_auditable_action_set_is_closed_and_complete() -> None:
    """The six things the delivery document says must always be recorded, and nothing
    invented alongside them.

    An open action vocabulary is how an auditable event ends up unaudited: someone adds a
    code path, invents a string for it, and nothing anywhere notices that no entry was
    ever written. Pinning the exact member set here means a seventh action is a deliberate
    edit in two files rather than an omission in one, in either direction: a member added
    without a test fails, and a member removed fails too.

    Note that the document's "deny" and "revoke" are one item and two members here. A deny
    is a request refused at runtime, a revoke is a grant taken away by an administrator;
    they differ by orders of magnitude in frequency and they answer different questions.
    """
    required = {
        "grant": AuditAction.GRANT,
        "deny": AuditAction.DENY,
        "revoke": AuditAction.REVOKE,
        "leash change": AuditAction.LEASH_CHANGE,
        "entity merge": AuditAction.ENTITY_MERGE,
        "publish": AuditAction.PUBLISH,
        "break glass": AuditAction.BREAK_GLASS,
    }
    assert set(required.values()) == set(AuditAction)
    assert {action.value for action in AuditAction} == {
        "grant",
        "deny",
        "revoke",
        "leash_change",
        "entity_merge",
        "publish",
        "break_glass",
    }


def test_every_auditable_action_can_actually_be_written() -> None:
    """An action in the enum that no entry can carry is a rule with no mechanism behind
    it, and it would pass the test above unnoticed."""
    chain = AuditChain()
    for i, action in enumerate(AuditAction):
        chain.append(
            action=action,
            actor_id="u_rupash",
            subject="principal:u_weiling",
            ent_hash=ENT,
            trace_id="trace1",
            at=NOW + timedelta(minutes=i),
        )
    assert [e.action for e in chain.entries] == list(AuditAction)
    assert chain.verify() is None


# ----------------------------------------- what must never be recorded (M24.1.4)
def test_no_canary_value_survives_into_an_entry() -> None:
    """The core canary, inverted from an ordinary test: it fails if the data arrives.

    Every restricted field in the fixture holds an improbable string rather than a
    plausible number, so a leak here is unmistakable and greppable rather than something
    that could be mistaken for test data. Each of these details is a shape a caller
    genuinely passes: a flat value, a nested before-state, a list of changed fields.
    """
    entry = AuditChain().append(
        action=AuditAction.GRANT,
        actor_id="u_rupash",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace1",
        at=NOW,
        details={
            "client.contract_value": CANARIES["client.contract_value"],
            "hr.salary": CANARIES["hr.salary"],
            "hr.performance_note": CANARIES["hr.performance_note"],
            "invoice.amount_due": CANARIES["invoice.amount_due"],
            "agent.system_prompt": CANARIES["agent.system_prompt"],
            "before": {"client.margin": CANARIES["client.margin"]},
            "changed": ["ticket.internal_note", CANARIES["ticket.internal_note"]],
        },
    )

    serialised = entry.model_dump_json()
    for token in canary_tokens():
        assert token not in serialised, f"{token} reached the ledger"

    # and the names survive, which is the entire point: the entry still says what was
    # touched, so an investigator knows where to look without the ledger telling them.
    assert entry.details["client.contract_value"] == REDACTED
    assert entry.details["before"] == "client.margin"
    assert entry.details["changed"] == REDACTED


def test_an_entry_cannot_be_built_around_the_redactor() -> None:
    """`append` always redacts, but entries also arrive by being loaded from a table, and
    a row written by an older version of the code, by a migration or by hand must not be
    able to introduce a value the redactor would have caught. A check that lives only in
    the helper is a check somebody can construct their way around."""
    with pytest.raises(ValidationError, match="would put a value in the ledger"):
        AuditEntry(
            seq=0,
            at=NOW,
            actor_id="u_rupash",
            action=AuditAction.GRANT,
            subject="principal:u_weiling",
            ent_hash=ENT,
            trace_id="trace1",
            details={"client.contract_value": CANARIES["client.contract_value"]},
            prev_hash=GENESIS_HASH,
            entry_hash="0" * DIGEST_CHARS,
        )


def test_the_ledger_records_an_entitlement_hash_and_never_the_capabilities() -> None:
    """A ledger of capabilities is a map of who can see what, kept for longer than
    anything else and readable by everyone with audit access. That document should not
    exist, so the entry carries a 32-character hash of the reach instead of the reach.

    The hash still does the work it is there for: two entries written under the same
    entitlement match, and any change to the actor's grants shows up as a different value.
    """
    entitlement = person("u_weiling").entitlement()
    entry = AuditChain().append(
        action=AuditAction.GRANT,
        actor_id="u_rupash",
        subject="principal:u_weiling",
        ent_hash=entitlement.ent_hash(),
        trace_id="trace1",
        at=NOW,
        details={"capability": "read:client.contract_value"},
    )

    dumped = entry.model_dump_json()
    for grant in entitlement.grants:
        assert grant.capability.value not in dumped, f"{grant.capability.value} reached the ledger"
    assert entry.details["capability"] == REDACTED
    assert entry.ent_hash == entitlement.ent_hash()
    assert len(entry.ent_hash) == 32


def test_a_wider_actor_and_a_narrower_one_are_distinguishable_without_naming_either() -> None:
    """The hash has to be worth carrying. If every actor's reach hashed alike, the field
    would be decoration and the ledger could not answer "was this done under an
    entitlement anyone still holds"."""
    wide = person("u_rupash").entitlement().ent_hash()
    narrow = person("u_jason").entitlement().ent_hash()
    assert wide != narrow


# ---------------------------------------------------- legal hold (M24.2.1)
def test_a_held_entry_is_not_removed_by_a_retention_sweep() -> None:
    """The rule in one line: retention does not outrank a hold. Every entry here is far
    past the cutoff and the sweep still takes nothing."""
    chain = a_chain(5)
    hold = a_hold(subjects=frozenset({"principal:u_subject0"}))

    retained, removed = chain.prune_before(NOW + timedelta(days=365), holds=[hold], now=NOW)

    assert removed == ()
    assert len(retained) == 5
    assert retained.verify() is None


def test_one_held_entry_pins_every_entry_after_it() -> None:
    """A hash chain can only be pruned from the oldest end. Removing an entry from the
    middle would leave the next one pointing at a digest that no longer exists, so the
    sweep stops dead at the first held entry and everything newer stays.

    This is expensive and it is correct: a single held entry from three years ago keeps
    three years of ledger on disk, which is what a hold is for.
    """
    chain = a_chain(5)
    hold = a_hold(subjects=frozenset({"principal:u_subject2"}))

    retained, removed = chain.prune_before(NOW + timedelta(days=365), holds=[hold], now=NOW)

    assert [e.seq for e in removed] == [0, 1]
    assert [e.seq for e in retained.entries] == [2, 3, 4]
    assert retained.verify() is None


def test_a_hold_covers_entries_written_after_it_was_placed() -> None:
    """A hold is placed before anyone knows which entries it will need to cover, so it has
    to be a predicate and not a flag written onto the rows that happen to exist. A
    flag-based hold silently fails to hold the entries a live dispute is still generating,
    which are the ones being asked for."""
    chain = a_chain(2)
    hold = a_hold(actors=frozenset({"u_rupash"}), placed_at=NOW + timedelta(minutes=1))
    later = chain.append(
        action=AuditAction.BREAK_GLASS,
        actor_id="u_rupash",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace9",
        at=NOW + timedelta(minutes=5),
    )
    moment = NOW + timedelta(minutes=5)

    assert hold.is_active(moment)
    assert hold.covers(later)
    _, removed = chain.prune_before(NOW + timedelta(days=365), holds=[hold], now=moment)
    assert removed == ()


def test_releasing_a_hold_lets_the_sweep_proceed() -> None:
    """The opposite failure. A hold implementation that never releases would pass every
    test above while quietly making retention unenforceable, so this one has to sit beside
    them."""
    chain = a_chain(5)
    released = a_hold(
        subjects=frozenset({"principal:u_subject0"}),
        released_at=NOW - timedelta(hours=1),
    )

    retained, removed = chain.prune_before(NOW + timedelta(days=365), holds=[released], now=NOW)

    assert len(removed) == 5
    assert len(retained) == 0
    assert retained.verify() is None
