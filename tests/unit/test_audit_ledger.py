"""The audit ledger: redaction, entry grammar, the digest, and the chain walk.

The invariant suite next door asserts the rules that block deploy. This file covers the
mechanics underneath them, and in particular the parts that look like plumbing and are
not: what the digest covers, what a details mapping is allowed to say, and what a
retention sweep is allowed to remove.

Task ids: M24.1.1, M24.1.2, M24.1.3, M24.1.4, M24.2.1
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from brain.audit.ledger import (
    DIGEST_CHARS,
    GENESIS_HASH,
    REDACTED,
    AuditAction,
    AuditChain,
    BreakReason,
    LegalHold,
    changed_fields,
    compute_entry_hash,
    is_held,
    redact_details,
)
from tests.fixtures.company import CANARIES, NOW, person

#: A real ent_hash, produced by the module that produces them in anger. Hard-coding a
#: plausible-looking 32 hex string here would hide a width disagreement between the two
#: modules, which is the kind of thing that only ever surfaces in production.
ENT = person("u_weiling").entitlement().ent_hash()

#: Same instant as NOW, written by a worker on Singapore time.
SGT = timezone(timedelta(hours=8))


def a_chain(count: int = 3) -> AuditChain:
    """A short chain of distinguishable entries, all from the fixed fixture clock."""
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


def digest(
    *,
    seq: int = 0,
    at: datetime = NOW,
    actor_id: str = "u_rupash",
    action: AuditAction = AuditAction.GRANT,
    subject: str = "principal:u_weiling",
    ent_hash: str = ENT,
    trace_id: str = "t1",
    details: Mapping[str, str] | None = None,
    prev_hash: str = GENESIS_HASH,
) -> str:
    """One entry's digest, with everything defaulted so a test can vary one thing."""
    return compute_entry_hash(
        seq=seq,
        at=at,
        actor_id=actor_id,
        action=action,
        subject=subject,
        ent_hash=ent_hash,
        trace_id=trace_id,
        details=details or {},
        prev_hash=prev_hash,
    )


# ------------------------------------------------------------------ redaction
def test_a_field_name_survives_redaction_and_its_value_does_not() -> None:
    """The ledger's whole shape in one assertion: it says which field was touched and
    refuses to say what was in it."""
    out = redact_details({"client.contract_value": CANARIES["client.contract_value"]})
    assert out == {"client.contract_value": REDACTED}


def test_a_key_that_is_not_a_field_name_is_dropped_rather_than_redacted() -> None:
    """When the key is the leak, redacting the value keeps the leak. A key of "SNM
    Construction Pte Ltd" names the client whatever happens to the value beside it."""
    out = redact_details({"SNM Construction Pte Ltd": "overdue", "client.name": "client.name"})
    assert out == {"client.name": "client.name"}


def test_every_redacted_value_looks_identical() -> None:
    """A marker reading `<redacted:int:5>` would give away the order of magnitude of the
    salary underneath it. The shape of a value is still information about the value, so a
    short int, a long string and a None have to be indistinguishable afterwards."""
    out = redact_details(
        {"hr.salary": 91000, "client.name": "SNM Construction Pte Ltd", "note": None}
    )
    assert set(out.values()) == {REDACTED}
    assert "91000" not in str(out)


def test_a_digest_survives_because_a_digest_is_not_a_value() -> None:
    """An ent_hash in the details is how a grant entry says the subject's reach changed.
    It is admitted because a sha256 over a whole grant set is not enumerable, unlike a
    sha256 over a five-digit salary, which is a lookup table away from being the salary."""
    out = redact_details({"subject_ent_hash": ENT})
    assert out == {"subject_ent_hash": ENT}


def test_a_list_of_field_names_survives_and_a_mixed_list_does_not() -> None:
    """All or nothing. If one element of a list is a value and the rest are names, keeping
    the names tells the reader which element was the interesting one."""
    assert redact_details({"changed": ["margin", "contract_value"]}) == {
        "changed": "contract_value,margin"
    }
    assert redact_details({"changed": ["margin", CANARIES["client.margin"]]}) == {
        "changed": REDACTED
    }


def test_a_nested_record_is_reduced_to_the_names_of_its_fields() -> None:
    """This is how before-and-after state reaches the ledger at all (M24.1.4)."""
    out = redact_details({"before": {"contract_value": 48000, "margin": 0.3}})
    assert out == {"before": "contract_value,margin"}


def test_booleans_survive_because_there_are_only_two_of_them() -> None:
    assert redact_details({"break_glass": True, "approved": False}) == {
        "break_glass": "true",
        "approved": "false",
    }


def test_redaction_is_idempotent() -> None:
    """Redacting an already-redacted mapping must not degrade it, or a details mapping
    would lose a little more of itself every time it crossed a layer boundary."""
    once = redact_details({"hr.salary": 91000, "client.name": "client.name"})
    assert redact_details(once) == once


# -------------------------------------------------------------- changed fields
def test_changed_fields_names_what_moved_and_never_what_it_moved_to() -> None:
    """The honest reading of "before and after state": the ledger proves the field
    changed, and the row's own history says what it changed to."""
    before = {"contract_value": 48000, "name": "SNM", "margin": 0.3}
    after = {"contract_value": 51000, "name": "SNM", "margin": 0.3}
    assert changed_fields(before, after) == ("contract_value",)


def test_changed_fields_notices_a_field_that_appeared_or_vanished() -> None:
    """A field going from absent to present is a change. Comparing with a plain `.get()`
    and no sentinel would call `{"x": None}` and `{}` identical, and hide a deletion."""
    assert changed_fields({}, {"margin": None}) == ("margin",)
    assert changed_fields({"margin": None}, {}) == ("margin",)


# ---------------------------------------------------------------- entry grammar
def test_an_entry_accepts_exactly_what_the_entitlement_module_produces() -> None:
    """The two modules have to agree on the width of an ent_hash, or entries silently stop
    being writable the first time somebody changes the truncation in entitlement.py."""
    entry = a_chain(1).entries[0]
    assert entry.ent_hash == ENT
    assert len(ENT) == 32


def test_a_subject_must_be_a_known_kind_and_an_identifier() -> None:
    """The client-visible audit view filters on subject kind (M24.1.5). A free-text kind
    makes "everything that happened to this principal" a full scan and a guess."""
    chain = AuditChain()
    for bad in ("u_weiling", "customer:u_weiling", "principal:", "principal:Wei Ling Tan"):
        with pytest.raises(ValidationError):
            chain.append(
                action=AuditAction.DENY,
                actor_id="u_rupash",
                subject=bad,
                ent_hash=ENT,
                trace_id="t1",
                at=NOW,
            )


def test_a_naive_timestamp_is_refused() -> None:
    """A ledger ordered by timestamps of mixed awareness is not ordered at all."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        AuditChain().append(
            action=AuditAction.PUBLISH,
            actor_id="u_rupash",
            subject="artifact:a_1",
            ent_hash=ENT,
            trace_id="t1",
            at=datetime(2026, 9, 4, 12, 0),
        )


def test_an_entry_is_frozen_against_rebinding() -> None:
    entry = a_chain(1).entries[0]
    with pytest.raises(ValidationError):
        entry.subject = "principal:someone_else"


def test_an_entry_does_not_validate_its_own_digest_on_construction() -> None:
    """Deliberate, and it reads like a bug. A tampered row has to load so that verify can
    report which row is tampered. A validator here would raise on load instead, and a
    ledger that cannot load its own damaged rows cannot tell anybody which ones they are."""
    entry = a_chain(1).entries[0]
    forged = entry.model_copy(update={"entry_hash": "f" * DIGEST_CHARS})
    assert forged.entry_hash != forged.recompute_hash()  # it loaded, and it is wrong


# ----------------------------------------------------------------- the digest
def test_the_digest_is_unambiguous_about_where_one_field_ends() -> None:
    """Length-prefixing, as a property. Concatenating fields with a separator would let
    ("ab", "c") and ("a", "bc") collide, and one entry could then stand in for another."""
    assert digest(actor_id="ab", subject="principal:c") != digest(
        actor_id="a", subject="principal:bc"
    )


def test_the_digest_does_not_depend_on_the_order_details_were_built_in() -> None:
    """A dict keeps insertion order, so two entries meaning the same thing would otherwise
    digest differently. EntitlementSet.ent_hash sorts its grants for the same reason."""
    assert digest(details={"a": "a", "b": "b"}) == digest(details={"b": "b", "a": "a"})


def test_the_digest_does_not_depend_on_how_the_timestamp_was_written() -> None:
    """One instant recorded by a worker on +08:00 and one on UTC is one instant. Two
    digests for it would make a correct chain look broken."""
    assert digest(at=NOW) == digest(at=NOW.astimezone(SGT))


def test_dropping_a_detail_changes_the_digest() -> None:
    """Details are inside the digest, so quietly removing the awkward one from an entry
    is a tamper like any other rather than a tidy-up."""
    assert digest(details={"a": "a", "b": "b"}) != digest(details={"a": "a"})


# ------------------------------------------------------------------ the chain
def test_an_empty_chain_verifies_and_its_head_is_genesis() -> None:
    """The empty case has to be meaningful, or an anchor taken before the first entry has
    nothing to point at."""
    chain = AuditChain()
    assert chain.verify() is None
    assert chain.head() == GENESIS_HASH
    assert len(chain) == 0


def test_the_first_entry_links_to_genesis_and_the_rest_to_their_predecessor() -> None:
    chain = a_chain(3)
    assert chain.entries[0].prev_hash == GENESIS_HASH
    assert chain.entries[1].prev_hash == chain.entries[0].entry_hash
    assert chain.entries[2].prev_hash == chain.entries[1].entry_hash
    assert chain.head() == chain.entries[-1].entry_hash


def test_the_chain_and_not_the_caller_chooses_seq_and_both_hashes() -> None:
    """A caller who can choose seq, prev_hash or entry_hash can forge a link, and then
    there is no such thing as a well-formed entry, only a conventional one."""
    parameters = set(inspect.signature(AuditChain.append).parameters)
    assert parameters.isdisjoint({"seq", "prev_hash", "entry_hash"})
    assert [e.seq for e in a_chain(3).entries] == [0, 1, 2]


def test_a_break_names_the_reason_and_not_only_the_index() -> None:
    """An index on its own sends an operator to read that many rows by hand. The reason is
    what tells them whether they are looking at a tamper, a deletion or a bad migration."""
    chain = a_chain(3)
    tampered = AuditChain(
        [chain.entries[0], chain.entries[1].model_copy(update={"trace_id": "elsewhere"})]
    )
    found = tampered.first_break()
    assert found is not None
    assert found.index == 1
    assert found.seq == 1
    assert found.reason is BreakReason.CONTENT_ALTERED


def test_a_window_of_a_longer_ledger_verifies_against_its_start_hash() -> None:
    """A verification job that can only start from genesis gets slower every day and is
    eventually switched off. One that checks last month against last month's digest keeps
    running, which is the difference between a control and a ceremony."""
    chain = a_chain(5)
    window = AuditChain(chain.entries[2:], start_hash=chain.entries[1].entry_hash)
    assert window.verify() is None
    unanchored = AuditChain(chain.entries[2:], start_hash=GENESIS_HASH)
    assert unanchored.verify() == 0


# ------------------------------------------------------------------ legal hold
def a_hold(**kw: object) -> LegalHold:
    base: dict[str, object] = {
        "id": "hold_1",
        "reason_code": "pending_litigation",
        "subjects": frozenset({"principal:u_subject1"}),
        "placed_at": NOW - timedelta(days=1),
    }
    return LegalHold(**(base | kw))  # type: ignore[arg-type]


def test_a_hold_that_names_nothing_is_refused() -> None:
    """The failure this prevents: a hold is placed, the sweep runs, nothing is held, and
    it is discovered when the data is asked for and is gone."""
    with pytest.raises(ValidationError, match="must name subjects or actors"):
        LegalHold(id="hold_x", reason_code="pending_litigation", placed_at=NOW)


def test_a_hold_reaches_an_entry_by_subject_or_by_actor() -> None:
    chain = a_chain(3)
    by_subject = a_hold()
    by_actor = a_hold(subjects=frozenset(), actors=frozenset({"u_rupash"}))
    assert by_subject.covers(chain.entries[1])
    assert not by_subject.covers(chain.entries[2])
    assert all(by_actor.covers(e) for e in chain.entries)


def test_a_released_hold_stops_holding_and_is_not_deleted() -> None:
    """Which entries were held, on whose authority and for how long is itself a thing that
    gets asked about, so a release is recorded rather than the hold being removed."""
    entry = a_chain(3).entries[1]
    released = a_hold(released_at=NOW - timedelta(hours=1))
    assert not released.is_active(NOW)
    assert released.covers(entry)  # it still says what it once held
    assert not is_held(entry, [released], NOW)


def test_a_hold_is_not_active_before_it_was_placed() -> None:
    future = a_hold(placed_at=NOW + timedelta(days=1))
    assert not future.is_active(NOW)
    assert future.is_active(NOW + timedelta(days=2))


def test_a_reason_code_cannot_carry_the_name_of_a_party() -> None:
    """Free text on a legal hold is where the complainant, the allegation and the
    counterparty end up, in the one table that outlives every retention policy."""
    with pytest.raises(ValidationError):
        a_hold(reason_code="Grievance raised by Wei Ling Tan against Aaron Lim")


# --------------------------------------------------------------- retention sweep
def test_a_sweep_removes_the_released_prefix_and_what_remains_still_verifies() -> None:
    """Pruning has to leave a chain, not a heap. The retained window carries the last
    removed digest as its start hash, so it verifies instead of looking forged."""
    chain = a_chain(5)
    retained, removed = chain.prune_before(NOW + timedelta(minutes=3), holds=(), now=NOW)
    assert [e.seq for e in removed] == [0, 1, 2]
    assert [e.seq for e in retained.entries] == [3, 4]
    assert retained.verify() is None
    assert retained.start_hash == removed[-1].entry_hash


def test_a_sweep_that_removes_nothing_leaves_the_chain_verifiable_from_genesis() -> None:
    chain = a_chain(3)
    retained, removed = chain.prune_before(NOW - timedelta(days=1), holds=(), now=NOW)
    assert removed == ()
    assert retained.start_hash == GENESIS_HASH
    assert retained.verify() is None


def test_a_sweep_does_not_mutate_the_chain_it_was_asked_about() -> None:
    """A sweep that is going to refuse should be inspectable before anything is written
    back, so the prune returns a new chain rather than editing this one."""
    chain = a_chain(5)
    chain.prune_before(NOW + timedelta(minutes=3), holds=(), now=NOW)
    assert len(chain) == 5
    assert chain.verify() is None
