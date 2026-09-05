"""The verification job, the recording seam and the client-visible audit view.

The invariant suite next door asserts the rules that block deploy: that a withheld entry is
absent rather than refused, that no count of them escapes, that a run never reports
continuity without saying what it did not check. This file covers the mechanics underneath
those rules - what a window is checked against, what each recorder writes, how a filter
narrows and how a page ends.

Note the file name. These three modules were built together as the last of M24.1 and share
one test file per layer; `verify.py` and `record.py` have no test module of their own, which
is worth fixing when somebody next opens this directory.

Task ids: M24.1.2, M24.1.3, M24.1.5
"""

from __future__ import annotations

import base64
import inspect
import itertools
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from brain.audit.export import Anchor
from brain.audit.ledger import (
    REDACTED,
    AuditAction,
    AuditChain,
    AuditEntry,
    BreakReason,
    ChainBreak,
)
from brain.audit.record import ACTION_BY_METHOD, AuditRecorder, DenyReason, subject
from brain.audit.verify import (
    ANCHOR_MISSING_CAVEAT,
    UNANCHORED_CAVEAT,
    UNMOORED_CAVEAT,
    Baseline,
    Checkpoint,
    Completeness,
    VerificationReport,
    verify_window,
)
from brain.audit.view import (
    MAX_PAGE_SIZE,
    AuditFilter,
    AuditPage,
    AuditRow,
    AuditView,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Clause, Op, Scope
from brain.gate.injection import AutonomyTier
from brain.identity.roles import BreakGlassReason
from tests.fixtures.company import CANARIES, NOW, person

ENT = person("u_weiling").entitlement().ent_hash()


def cap(value: str) -> Capability:
    return Capability(value=value)


def ticking(start: datetime = NOW) -> Callable[[], datetime]:
    """A clock that moves. The recorder takes one rather than reading the wall, so a test
    supplies one that is deterministic and still distinguishes two entries."""
    counter = itertools.count()
    return lambda: start + timedelta(seconds=next(counter))


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
        )
    return chain


def an_anchor(entry: AuditEntry, **kw: object) -> Anchor:
    base: dict[str, object] = {
        "seq": entry.seq,
        "entry_hash": entry.entry_hash,
        "recorded_at": NOW,
        "recorded_by": "svc_verifier",
        "where": "offsite_object_store",
    }
    return Anchor(**(base | kw))  # type: ignore[arg-type]


def a_checkpoint(entry: AuditEntry) -> Checkpoint:
    return Checkpoint(
        through_seq=entry.seq,
        entry_hash=entry.entry_hash,
        recorded_at=NOW,
        recorded_by="svc_verifier",
    )


# =========================================================== the job (M24.1.2)
def test_a_window_verifies_against_the_digest_the_previous_run_left() -> None:
    """The whole reason the job is windowed. Without this, verification starts at genesis
    every run, gets slower every day, and is eventually switched off."""
    chain = a_chain(6)
    checkpoint = a_checkpoint(chain.entries[2])

    report = verify_window(chain.entries[3:], at=NOW, checkpoint=checkpoint)

    assert report.continuous
    assert report.baseline is Baseline.CHECKPOINT
    assert report.entry_count == 3
    assert (report.first_seq, report.last_seq) == (3, 5)
    assert report.head == chain.entries[5].entry_hash


def test_a_window_that_does_not_start_where_the_checkpoint_said_reads_as_missing_rows() -> None:
    """Deleting entries between two runs is the failure this catches, and the reason it is
    reported as a sequence break rather than a digest mismatch: an operator sent to look for
    a bad retention sweep and one sent to look for a tamper go to different places."""
    chain = a_chain(6)
    checkpoint = a_checkpoint(chain.entries[1])

    report = verify_window(chain.entries[4:], at=NOW, checkpoint=checkpoint)

    assert not report.continuous
    assert report.break_found is not None
    assert report.break_found.reason == "sequence_broken"
    assert (report.break_found.expected, report.break_found.actual) == ("2", "4")


def test_a_window_whose_first_link_disagrees_with_the_checkpoint_is_a_break() -> None:
    """The checkpoint is not decoration: a window with the right sequence numbers and the
    wrong parent digest is a rewritten history that lines up on paper."""
    chain = a_chain(4)
    forged = Checkpoint(
        through_seq=1,
        entry_hash="f" * 64,
        recorded_at=NOW,
        recorded_by="svc_verifier",
    )

    report = verify_window(chain.entries[2:], at=NOW, checkpoint=forged)

    assert not report.continuous
    assert report.break_found is not None
    assert report.break_found.reason == "link_broken"


def test_an_empty_window_after_a_checkpoint_is_continuous_and_moves_nothing() -> None:
    """The ordinary case: the job runs, nothing happened since the last run. It must not
    report a break, and the checkpoint it hands forward must not move backwards."""
    chain = a_chain(3)
    checkpoint = a_checkpoint(chain.entries[2])

    report = verify_window((), at=NOW, checkpoint=checkpoint)

    assert report.continuous
    assert report.entry_count == 0
    assert report.head == checkpoint.entry_hash
    assert report.next_checkpoint(recorded_by="svc_verifier") is None


def test_a_window_starting_at_the_first_entry_is_checked_against_genesis() -> None:
    """The first run ever. It has no checkpoint and does not need one, because genesis is a
    digest nobody chose."""
    report = verify_window(a_chain(4).entries, at=NOW)

    assert report.baseline is Baseline.GENESIS
    assert report.continuous
    assert report.start_hash == "0" * 64


def test_a_window_with_no_checkpoint_and_no_beginning_says_it_is_unmoored() -> None:
    """The honest middle. Refusing would teach people to invent a checkpoint; reporting
    "verified" would hide that the start hash came from the same rows being checked."""
    report = verify_window(a_chain(5).entries[2:], at=NOW)

    assert report.baseline is Baseline.UNMOORED
    assert report.continuous
    assert UNMOORED_CAVEAT in report.caveats


def test_an_anchor_the_window_still_holds_makes_the_run_anchored() -> None:
    """The only path to a completeness verdict at all. Everything else in the job is
    continuity."""
    chain = a_chain(5)
    report = verify_window(chain.entries, at=NOW, anchors=[an_anchor(chain.entries[3])])

    assert report.completeness is Completeness.ANCHORED
    assert len(report.anchors_checked) == 1
    assert UNANCHORED_CAVEAT not in report.caveats


def test_an_anchor_newer_than_the_window_is_a_finding_and_not_a_shrug() -> None:
    """Truncation, which is the whole reason anchors are bought. A ledger whose newest
    entries were deleted looks exactly like a window that happens to end early, so an anchor
    above the window must be checked rather than filed as out of scope."""
    chain = a_chain(6)
    anchor = an_anchor(chain.entries[5])
    truncated = chain.entries[:3]

    report = verify_window(truncated, at=NOW, anchors=[anchor])

    assert report.continuous  # the chain alone still sees nothing wrong
    assert report.completeness is Completeness.ANCHOR_MISSING
    assert ANCHOR_MISSING_CAVEAT in report.caveats


def test_an_anchor_older_than_the_window_is_not_checked_here() -> None:
    """The false-positive case. A verifier that reports a finding on every windowed run is a
    verifier somebody switches off, and then nothing above protects anything."""
    chain = a_chain(6)
    checkpoint = a_checkpoint(chain.entries[3])

    report = verify_window(
        chain.entries[4:], at=NOW, checkpoint=checkpoint, anchors=[an_anchor(chain.entries[1])]
    )

    assert report.completeness is Completeness.UNANCHORED
    assert len(report.anchors_before_window) == 1
    assert report.anchors_checked == ()


def test_a_run_that_found_a_break_offers_no_checkpoint_to_carry_forward() -> None:
    """Otherwise the next run starts after the tamper and reports clean for ever, which is
    the failure mode of every checkpointed check ever written."""
    entries = list(a_chain(5).entries)
    entries[2] = entries[2].model_copy(update={"actor_id": "u_somebody_else"})

    report = verify_window(entries, at=NOW)

    assert not report.continuous
    assert report.next_checkpoint(recorded_by="svc_verifier") is None


def test_a_run_whose_anchor_went_missing_offers_no_checkpoint_either() -> None:
    """A continuous window with a missing anchor is the truncated case, and pinning a
    baseline there blesses the shortened ledger as the normal length."""
    chain = a_chain(6)
    report = verify_window(chain.entries[:3], at=NOW, anchors=[an_anchor(chain.entries[5])])

    assert report.continuous
    assert report.next_checkpoint(recorded_by="svc_verifier") is None


def test_a_clean_run_hands_its_head_to_the_next_one() -> None:
    """The other half of windowing: a run that proves nothing to the next run leaves it
    starting from genesis again."""
    chain = a_chain(4)
    report = verify_window(chain.entries, at=NOW, anchors=[an_anchor(chain.entries[3])])
    handed = report.next_checkpoint(recorded_by="svc_verifier")

    assert handed is not None
    assert handed.through_seq == 3
    assert handed.entry_hash == chain.entries[3].entry_hash
    assert verify_window((), at=NOW, checkpoint=handed).continuous


def test_a_report_cannot_say_continuous_and_carry_a_break() -> None:
    """A report can be built by hand, and one whose verdict disagrees with its parts is read
    as whichever half the reader looked at first."""
    with pytest.raises(ValidationError, match="would mislead its reader"):
        VerificationReport(
            checked_at=NOW,
            entry_count=2,
            first_seq=1,
            last_seq=2,
            start_hash="0" * 64,
            head="a" * 64,
            baseline=Baseline.CHECKPOINT,
            continuous=True,
            break_found=ChainBreak(
                index=0,
                seq=1,
                reason=BreakReason.LINK_BROKEN,
                expected="b" * 64,
                actual="c" * 64,
            ),
            completeness=Completeness.UNANCHORED,
        )


def test_a_report_cannot_claim_an_anchor_verdict_with_no_anchor_behind_it() -> None:
    """`anchored` with no anchors is the strongest thing this report can say, said by
    nobody. It is one keyword argument away and has to be refused at the type."""
    with pytest.raises(ValidationError, match="no anchor behind it"):
        VerificationReport(
            checked_at=NOW,
            entry_count=0,
            start_hash="0" * 64,
            head="0" * 64,
            baseline=Baseline.GENESIS,
            continuous=True,
            completeness=Completeness.ANCHORED,
        )


def test_the_summary_carries_the_caveats_rather_than_leaving_them_beside_it() -> None:
    """The summary line is what gets pasted into a ticket. Caveats available separately are
    caveats left behind."""
    summary = verify_window(a_chain(3).entries, at=NOW).summary()

    assert "continuous" in summary
    assert UNANCHORED_CAVEAT in summary


# ====================================================== the recorder (M24.1.3)
CALLS: dict[str, dict[str, object]] = {
    "grant": {"principal_id": "u_weiling", "capability": cap("read:client.name")},
    "deny": {
        "subject_kind": "entity",
        "subject_id": "c_0447",
        "capability": cap("read:client.contract_value"),
        "reason": DenyReason.NO_GRANT,
    },
    "revoke": {"principal_id": "u_weiling", "capability": cap("read:client.name")},
    "leash_change": {
        "agent_id": "sentinel",
        "target": "ticket.update_status",
        "from_rung": AutonomyTier.SHADOW,
        "to_rung": AutonomyTier.ASSISTED,
    },
    "entity_merge": {"kept_entity_id": "c_0447", "merged_entity_id": "c_0331"},
    "publish": {"artifact_id": "rep_2026_08"},
    "break_glass": {
        "session_id": "bg_1",
        "principal_id": "u_weiling",
        "reason": BreakGlassReason.INCIDENT_RESPONSE,
        "authorised_by": "u_rupash",
    },
}


def a_recorder(chain: AuditChain | None = None) -> tuple[AuditRecorder, AuditChain]:
    target = chain if chain is not None else AuditChain()
    return (
        AuditRecorder(
            target,
            actor_id="u_rupash",
            ent_hash=ENT,
            trace_id="trace1",
            clock=ticking(),
        ),
        target,
    )


def test_every_declared_recorder_writes_the_action_it_declares() -> None:
    """The mapping and the method bodies are two places, and this is what stops them
    drifting. Without it, `ACTION_BY_METHOD` is a comment that happens to be a dict."""
    for name, action in ACTION_BY_METHOD.items():
        recorder, chain = a_recorder()
        getattr(recorder, name)(**CALLS[name])
        assert chain.entries, f"{name} wrote nothing"
        assert all(e.action is action for e in chain.entries), name
        assert chain.verify() is None, name


def test_the_recorder_binds_the_actor_once_so_a_call_site_cannot_get_it_wrong() -> None:
    """Actor, reach and trace come from the request. Passing them per call is an argument
    order in which a grant gets recorded under the wrong person's name."""
    recorder, chain = a_recorder()
    recorder.grant(principal_id="u_weiling", capability=cap("read:client.name"))
    recorder.revoke(principal_id="u_weiling", capability=cap("read:client.name"))

    assert {e.actor_id for e in chain.entries} == {"u_rupash"}
    assert {e.ent_hash for e in chain.entries} == {ENT}
    assert {e.trace_id for e in chain.entries} == {"trace1"}


def test_the_clock_is_read_once_per_entry_and_never_from_the_wall() -> None:
    """`ledger.append` refuses a default timestamp because application clocks drift. The
    recorder must not quietly reintroduce one."""
    recorder, chain = a_recorder()
    recorder.grant(principal_id="u_weiling", capability=cap("read:client.name"))
    recorder.grant(principal_id="u_jason", capability=cap("read:client.name"))

    assert [e.at for e in chain.entries] == [NOW, NOW + timedelta(seconds=1)]

    signature = inspect.signature(AuditRecorder.__init__)
    assert signature.parameters["clock"].default is inspect.Parameter.empty


def test_a_grant_records_one_capability_and_the_names_of_the_scope_fields() -> None:
    """The 5 September decision, as a signature: a capability is a `Capability`, the scope is
    a list of field names, and there is no parameter that could take a scope value."""
    recorder, chain = a_recorder()
    recorder.grant(
        principal_id="u_weiling",
        capability=cap("read:client.contract_value"),
        scope_fields=("partner_visible", "department"),
    )

    entry = chain.entries[0]
    assert entry.subject == "principal:u_weiling"
    assert entry.details["capability"] == "read:client.contract_value"
    assert entry.details["scope_fields"] == "department,partner_visible"


def test_an_unrestricted_scope_leaves_no_detail_rather_than_a_redaction_marker() -> None:
    """`<redacted>` tells a reader that something was hidden. An unrestricted scope hid
    nothing, and the honest way to say so is to say nothing."""
    recorder, chain = a_recorder()
    recorder.grant(principal_id="u_weiling", capability=cap("read:client.name"))

    assert "scope_fields" not in chain.entries[0].details


def test_a_deny_records_what_was_sought_and_why_it_was_refused() -> None:
    """DENIED is collapsed into ABSENT before anything reaches a person. The ledger is the
    one place the real reason is written down, which is what it is for."""
    recorder, chain = a_recorder()
    recorder.deny(
        subject_kind="entity",
        subject_id="c_0447",
        capability=cap("read:client.contract_value"),
        reason=DenyReason.OUT_OF_SCOPE,
    )

    entry = chain.entries[0]
    assert entry.subject == "entity:c_0447"
    assert entry.details == {
        "capability": "read:client.contract_value",
        "reason": "out_of_scope",
    }


def test_a_leash_change_records_both_rungs_by_name() -> None:
    """One rung cannot say whether an incident tightened autonomy or somebody loosened it,
    and the numbers behind an IntEnum are unreadable five years after the enum moves."""
    recorder, chain = a_recorder()
    recorder.leash_change(
        agent_id="sentinel",
        target="ticket.update_status",
        from_rung=AutonomyTier.SHADOW,
        to_rung=AutonomyTier.AUTONOMOUS,
    )

    entry = chain.entries[0]
    assert entry.subject == "agent:sentinel"
    assert entry.details["from_rung"] == "shadow"
    assert entry.details["to_rung"] == "autonomous"
    assert entry.details["target"] == "ticket.update_status"


def test_an_entity_merge_records_both_sides_so_neither_id_becomes_unfindable() -> None:
    """The ledger is addressed by subject. Somebody looking up the id that disappeared must
    not be told that nothing ever happened to it, and the other id cannot go in the details
    because the redactor admits field names and not identifiers."""
    recorder, chain = a_recorder()
    kept, merged = recorder.entity_merge(
        kept_entity_id="c_0447", merged_entity_id="c_0331", changed=("hours_remaining",)
    )

    assert kept.subject == "entity:c_0447"
    assert merged.subject == "entity:c_0331"
    assert kept.trace_id == merged.trace_id
    assert kept.details["changed"] == "hours_remaining"
    assert chain.verify() is None


def test_a_break_glass_entry_is_about_the_session_and_names_who_authorised_it() -> None:
    """A session has a start, an end and an authorisation; the principal is one of its
    fields. `BreakGlassSession.audit_subject` says the same, and the two must not disagree."""
    recorder, chain = a_recorder()
    recorder.break_glass(
        session_id="bg_1",
        principal_id="u_weiling",
        reason=BreakGlassReason.INCIDENT_RESPONSE,
        authorised_by="u_rupash",
        notified=("u_aaron",),
    )

    entry = chain.entries[0]
    assert entry.subject == "session:bg_1"
    assert entry.details["reason"] == "incident_response"
    assert entry.details["authorised_by"] == "u_rupash"
    assert entry.details["notified"] == "u_aaron"


def test_a_subject_kind_the_ledger_does_not_know_is_refused_by_name() -> None:
    """The caller getting this wrong is writing a new code path, and the useful message is
    the list of kinds rather than a regex."""
    with pytest.raises(ValueError, match="not one of"):
        subject("invoice", "inv_1")


def test_a_value_cannot_be_smuggled_through_the_recorder() -> None:
    """The recorder hands details to the ledger unredacted and the ledger redacts them. A
    recorder that assembled details to satisfy the redactor would be a second place where
    the rule lives, and the second place is the one that gets it wrong."""
    recorder, chain = a_recorder()
    recorder.publish(
        artifact_id="rep_2026_08", fields=("margin", CANARIES["client.contract_value"])
    )

    assert chain.entries[0].details["fields"] == REDACTED
    assert CANARIES["client.contract_value"] not in chain.entries[0].model_dump_json()


# =========================================================== the view (M24.1.5)
def a_ledger() -> AuditChain:
    """Six entries across four subject kinds and four actors."""
    chain = AuditChain()
    plan: Sequence[tuple[AuditAction, str, str, dict[str, object]]] = (
        (AuditAction.GRANT, "u_aaron", "principal:u_weiling", {"capability": "read:client.name"}),
        (AuditAction.DENY, "u_jason", "entity:c_0447", {"reason": "no_grant"}),
        (AuditAction.LEASH_CHANGE, "u_rupash", "agent:sentinel", {"target": "ticket.status"}),
        (AuditAction.PUBLISH, "u_siti", "artifact:rep_2026_08", {}),
        (AuditAction.REVOKE, "u_aaron", "principal:u_weiling", {"capability": "read:client.name"}),
        (AuditAction.BREAK_GLASS, "u_rupash", "session:bg_1", {"reason": "incident_response"}),
    )
    for i, (action, actor, subj, details) in enumerate(plan):
        chain.append(
            action=action,
            actor_id=actor,
            subject=subj,
            ent_hash=ENT,
            trace_id=f"trace{i}",
            at=NOW + timedelta(minutes=i),
            details=details,
        )
    return chain


def reader(
    pid: str,
    *capabilities: str,
    scope: Scope | None = None,
    not_after: datetime | None = None,
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=pid,
        grants=tuple(
            Grant(capability=cap(c), scope=scope or Scope.unrestricted()) for c in capabilities
        ),
        not_after=not_after,
    )


def a_view(entitlement: EntitlementSet, entries: Sequence[AuditEntry] | None = None) -> AuditView:
    return AuditView(
        entries if entries is not None else a_ledger().entries,
        reader=entitlement,
        now=NOW,
    )


def test_a_reader_sees_the_entries_their_grant_covers_and_no_others() -> None:
    """The whole point of the screen. A grant on one subject kind is not a grant on the
    ledger."""
    view = a_view(reader("u_auditor", "read:audit.principal"))

    rows = view.page(limit=MAX_PAGE_SIZE).rows

    assert {r.subject_kind for r in rows} == {"principal"}
    assert [r.action for r in rows] == [AuditAction.GRANT, AuditAction.REVOKE]


def test_a_reader_with_no_audit_grant_sees_nothing_and_is_told_nothing() -> None:
    """Fail closed, quietly. An empty page and not an error, because an error would confirm
    there was something to refuse."""
    page = a_view(reader("u_nobody")).page(limit=MAX_PAGE_SIZE)

    assert page.rows == ()
    assert page.next_cursor is None


def test_a_person_may_read_their_own_audit_trail() -> None:
    """A subject access request asks for exactly this, and every fact in it is a fact about
    them. It is a decision rather than something falling out of the grant model, so it needs
    a test that names it."""
    page = a_view(reader("u_weiling")).page(limit=MAX_PAGE_SIZE)

    assert {r.subject_id for r in page.rows} == {"u_weiling"}
    assert len(page.rows) == 2


def test_a_wildcard_audit_grant_covers_every_subject_kind() -> None:
    """The auditor's grant. If `read:audit.*` did not expand, the only way to see the whole
    ledger would be eight separate grants, and somebody would forget one."""
    page = a_view(reader("u_auditor", "read:audit.*")).page(limit=MAX_PAGE_SIZE)

    assert len(page.rows) == 6


def test_an_expired_reader_holds_nothing_whatever_the_grant_table_says() -> None:
    """Expiry beats the grant, and it is checked with the moment the view was asked for
    rather than the moment the session opened."""
    expired = reader("u_expired", "read:audit.*", not_after=NOW - timedelta(days=1))

    assert a_view(expired).page(limit=MAX_PAGE_SIZE).rows == ()


def test_a_scope_over_a_field_the_ledger_does_not_carry_admits_nothing() -> None:
    """Surprising and correct. The ledger holds no business attributes, so a departmental
    scope has nothing to match on, and a clause over an absent field admits nothing. The
    failure is in the safe direction and audit grants have to be scoped on what a row has."""
    departmental = reader("u_aaron", "read:audit.*", scope=Scope.department("maintenance"))

    assert a_view(departmental).page(limit=MAX_PAGE_SIZE).rows == ()


def test_a_scope_over_a_field_the_ledger_does_carry_narrows_as_written() -> None:
    """The other half. Without this, the test above would pass on a view that simply ignored
    scopes."""
    only_grants = reader(
        "u_auditor",
        "read:audit.*",
        scope=Scope(clauses=(Clause(field="action", op=Op.EQ, value="grant"),)),
    )

    rows = a_view(only_grants).page(limit=MAX_PAGE_SIZE).rows

    assert [r.action for r in rows] == [AuditAction.GRANT]


def test_filters_narrow_by_action_subject_kind_and_actor() -> None:
    """The three closed vocabularies, each on its own, so a bug in one is not hidden by
    another."""
    view = a_view(reader("u_auditor", "read:audit.*"))

    by_action = view.page(AuditFilter(actions=frozenset({AuditAction.DENY})), limit=MAX_PAGE_SIZE)
    by_kind = view.page(AuditFilter(subject_kinds=frozenset({"agent"})), limit=MAX_PAGE_SIZE)
    by_actor = view.page(AuditFilter(actors=frozenset({"u_aaron"})), limit=MAX_PAGE_SIZE)

    assert [r.action for r in by_action.rows] == [AuditAction.DENY]
    assert [r.subject_id for r in by_kind.rows] == ["sentinel"]
    assert {r.actor_id for r in by_actor.rows} == {"u_aaron"}


def test_the_date_range_is_half_open_so_two_consecutive_ranges_do_not_double_count() -> None:
    """An inclusive upper bound makes every boundary entry appear on two screens, and an
    auditor counting events then counts one twice."""
    view = a_view(reader("u_auditor", "read:audit.*"))
    boundary = NOW + timedelta(minutes=3)

    first = view.page(AuditFilter(until=boundary), limit=MAX_PAGE_SIZE)
    second = view.page(AuditFilter(since=boundary), limit=MAX_PAGE_SIZE)

    assert len(first.rows) == 3
    assert len(second.rows) == 3
    assert not {r.at for r in first.rows} & {r.at for r in second.rows}


def test_a_range_that_ends_before_it_starts_is_refused() -> None:
    """An empty range returns nothing, which looks exactly like a reader who may see
    nothing. Refusing it means the two cannot be confused."""
    with pytest.raises(ValidationError, match="until must be after since"):
        AuditFilter(since=NOW, until=NOW - timedelta(days=1))


def test_a_free_text_filter_cannot_be_constructed() -> None:
    """The filter model is the whole defence against a ledger acquiring a search box."""
    with pytest.raises(ValidationError):
        AuditFilter(text="salary")  # type: ignore[call-arg]


def test_an_unknown_subject_kind_is_refused_with_the_list_of_known_ones() -> None:
    """A filter is a lookup over a closed set. Accepting an unknown kind would silently
    return nothing, which reads as "there is none of that"."""
    with pytest.raises(ValidationError, match="unknown subject kind"):
        AuditFilter(subject_kinds=frozenset({"salary"}))


def test_an_actor_filter_that_is_a_pattern_rather_than_a_reference_is_refused() -> None:
    """`u_wei%` is a search over principal ids, and a search over a ledger enumerates the
    people in it."""
    with pytest.raises(ValidationError, match="not identifiers"):
        AuditFilter(actors=frozenset({"u_wei%"}))


def test_paging_returns_every_visible_row_exactly_once() -> None:
    """The ordinary correctness of a cursor, and the thing a tie-break bug breaks: two
    entries sharing a timestamp must not hide or repeat each other."""
    view = a_view(reader("u_auditor", "read:audit.*"))
    seen: list[AuditRow] = []
    cursor: str | None = None

    while True:
        page = view.page(limit=2, cursor=cursor)
        seen.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 6
    assert len({(r.at, r.action, r.subject_id) for r in seen}) == 6


def test_a_page_carries_a_cursor_only_while_more_visible_rows_remain() -> None:
    """A cursor on the last page sends a reader round again, and its absence is the only
    end-of-list signal they get."""
    view = a_view(reader("u_auditor", "read:audit.*"))

    assert view.page(limit=5).next_cursor is not None
    assert view.page(limit=6).next_cursor is None


def test_a_malformed_cursor_is_refused_without_touching_an_entry() -> None:
    """Silently starting from the beginning makes a client bug look like data loss. The
    refusal is a pure function of the string, so it cannot take a different path for an
    entry the reader may not see."""
    view = a_view(reader("u_auditor", "read:audit.*"))

    with pytest.raises(ValueError, match="malformed cursor"):
        view.page(cursor="not-a-cursor")


def test_a_limit_outside_the_bounds_is_refused() -> None:
    """A page of fifty thousand is a request for the whole ledger through a screen built to
    show a fortnight of it."""
    view = a_view(reader("u_auditor", "read:audit.*"))

    with pytest.raises(ValueError, match="limit must be between"):
        view.page(limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValueError, match="limit must be between"):
        view.page(limit=0)


def test_a_row_shows_a_single_capability_and_never_a_list_of_them() -> None:
    """Rupash's 5 September decision, at the surface it was decided for. A capability is
    legible; a list of them is the permission map."""
    chain = AuditChain()
    chain.append(
        action=AuditAction.GRANT,
        actor_id="u_aaron",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace1",
        at=NOW,
        details={"capability": "read:client.name"},
    )
    chain.append(
        action=AuditAction.GRANT,
        actor_id="u_aaron",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace2",
        at=NOW + timedelta(minutes=1),
        details={"capabilities": ["read:client.name", "read:client.contract_value"]},
    )

    rows = a_view(reader("u_weiling"), chain.entries).page(limit=MAX_PAGE_SIZE).rows

    assert rows[0].details["capability"] == "read:client.name"
    assert rows[1].details["capabilities"] == REDACTED


def test_a_page_is_a_shape_that_could_not_carry_a_count_if_somebody_wanted_one() -> None:
    """`extra="forbid"` is what stops "showing 12 of 40" being added later by a screen that
    thought it would be helpful."""
    with pytest.raises(ValidationError):
        AuditPage(rows=(), withheld=3)  # type: ignore[call-arg]


def test_a_cursor_is_a_position_in_the_visible_order_and_not_a_row_number() -> None:
    """A cursor travels through the client, and base64 is not encryption. Anything in it is
    something the reader can read."""
    view = a_view(reader("u_auditor", "read:audit.*"))
    cursor = view.page(limit=2).next_cursor
    assert cursor is not None

    decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))

    assert set(decoded) == {"at", "tie"}
    assert datetime.fromisoformat(decoded["at"]) == NOW + timedelta(minutes=1)
