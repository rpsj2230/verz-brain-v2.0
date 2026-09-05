"""The rules the audit view and the verification job must never break. A failure blocks deploy.

Two families, and they are here together because they are the two ways the last leaves of
M24.1 can fail quietly.

The first is about the view. It is the one place the ledger is shown to a person who may see
part of it, so every rule the rest of the system applies to a permission boundary applies
here at once: an entry the reader may not see is absent rather than refused, no count of what
was withheld escapes in any shape, and the numbering of the chain - which is a count of the
gaps, written down - never reaches a row, a page or a cursor. These are canaries in the
manner of the ones next door: they fail if the wrong thing arrives.

The second is about the verification job. Its failure mode is not a leak but a false
assurance: a hash chain proves continuity and never completeness, and a report that says
"verified" to a reader who hears "nothing is missing" has done more damage than no report at
all. So the job is pinned to say what it did not check, every time.

Task ids: M24.1.2, M24.1.3, M24.1.5
"""

from __future__ import annotations

import base64
import inspect
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from brain.audit.ledger import REDACTED, SUBJECT_KINDS, AuditAction, AuditChain, AuditEntry
from brain.audit.record import ACTION_BY_METHOD, AuditRecorder
from brain.audit.verify import Completeness, verify_window
from brain.audit.view import (
    CAPABILITY_BY_KIND,
    MAX_PAGE_SIZE,
    AuditFilter,
    AuditPage,
    AuditRow,
    AuditView,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from tests.fixtures.company import NOW, person

pytestmark = pytest.mark.invariant

ENT = person("u_weiling").entitlement().ent_hash()

#: Words a screen reaches for when it wants to say how much it hid. None of them may be a
#: field on anything this module returns.
COUNTING_NAMES = frozenset(
    {
        "withheld",
        "hidden",
        "total",
        "count",
        "omitted",
        "filtered",
        "denied",
        "suppressed",
        "remaining",
    }
)


def a_ledger() -> AuditChain:
    """Six entries. Two are about Wei Ling; the other four are not her business."""
    chain = AuditChain()
    plan = (
        (AuditAction.GRANT, "u_aaron", "principal:u_weiling"),
        (AuditAction.DENY, "u_jason", "entity:c_0447"),
        (AuditAction.LEASH_CHANGE, "u_rupash", "agent:sentinel"),
        (AuditAction.PUBLISH, "u_siti", "artifact:rep_2026_08"),
        (AuditAction.REVOKE, "u_aaron", "principal:u_weiling"),
        (AuditAction.BREAK_GLASS, "u_rupash", "session:bg_1"),
    )
    for i, (action, actor, subject) in enumerate(plan):
        chain.append(
            action=action,
            actor_id=actor,
            subject=subject,
            ent_hash=ENT,
            trace_id=f"trace{i}",
            at=NOW + timedelta(minutes=i),
        )
    return chain


def reader(pid: str, *capabilities: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id=pid,
        grants=tuple(
            Grant(capability=Capability(value=c), scope=Scope.unrestricted()) for c in capabilities
        ),
    )


def a_view(entitlement: EntitlementSet, entries: tuple[AuditEntry, ...] | None = None) -> AuditView:
    return AuditView(
        entries if entries is not None else a_ledger().entries,
        reader=entitlement,
        now=NOW,
    )


# ------------------------------------------- denied is absent, and absent is silent
def test_an_entry_the_reader_may_not_see_is_absent_rather_than_refused() -> None:
    """The rule the whole error taxonomy exists for, applied to a list. A refusal that
    explains itself has already confirmed the thing exists, so the view raises nothing,
    marks nothing, and simply does not have the row."""
    view = a_view(reader("u_weiling"))

    page = view.page(limit=MAX_PAGE_SIZE)

    assert len(page.rows) == 2
    assert {r.subject_id for r in page.rows} == {"u_weiling"}
    # and asking specifically for what she may not see is answered the same way
    empty = view.page(AuditFilter(subject_kinds=frozenset({"agent"})), limit=MAX_PAGE_SIZE)
    assert empty.rows == ()
    assert empty.next_cursor is None


def test_a_view_over_entries_the_reader_may_not_see_is_identical_to_one_without_them() -> None:
    """The strong form, and the test that catches every clever way of leaking the gap.

    A placeholder row, a count, a reason, a different cursor, a raised exception, an extra
    lookup that changes the output at all: any of them makes these two objects differ. If
    they are identical, a reader holding the result cannot tell whether the entries they
    cannot see exist or were never written, which is what indistinguishability means.
    """
    entitlement = reader("u_weiling")
    everything = a_ledger().entries
    only_visible = tuple(e for e in everything if e.subject == "principal:u_weiling")

    for limit in (1, 2, MAX_PAGE_SIZE):
        with_hidden = a_view(entitlement, everything).page(limit=limit)
        without = a_view(entitlement, only_visible).page(limit=limit)
        assert with_hidden.model_dump_json() == without.model_dump_json(), limit


def test_nothing_the_view_returns_can_carry_a_count_of_what_it_withheld() -> None:
    """ "3 results hidden" is precisely the fact a person is not entitled to, and repeated
    with different filters it is a search interface over data they cannot read. Two guards:
    no field is named like a count, and no field can be added by a caller later."""
    for model in (AuditPage, AuditRow):
        assert not COUNTING_NAMES & set(model.model_fields), model.__name__

    with pytest.raises(ValidationError):
        AuditPage(rows=(), withheld=2)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AuditRow(  # type: ignore[call-arg]
            at=NOW,
            action=AuditAction.GRANT,
            actor_id="u_aaron",
            subject_kind="principal",
            subject_id="u_weiling",
            hidden=1,
        )


def test_a_row_carries_no_chain_position_so_the_gaps_cannot_be_counted() -> None:
    """A row carrying `seq` hands the reader the hidden count directly: 100, 103, 107 says
    four entries exist that they may not read. `entry_hash` and `prev_hash` say the same
    thing sideways, because two of them together reveal whether two rows are adjacent.

    The field set is pinned exactly, in both directions. A field added here has to be argued
    for in front of this test rather than appearing in a diff about a screen.
    """
    assert set(AuditRow.model_fields) == {
        "at",
        "action",
        "actor_id",
        "subject_kind",
        "subject_id",
        "details",
    }

    rows = a_view(reader("u_auditor", "read:audit.*")).page(limit=MAX_PAGE_SIZE).rows
    dumped = "".join(r.model_dump_json() for r in rows)
    for entry in a_ledger().entries:
        assert entry.entry_hash not in dumped
        assert entry.prev_hash not in dumped
        assert entry.ent_hash not in dumped


def test_a_cursor_carries_no_sequence_number() -> None:
    """A cursor travels back and forth through the client and base64 is not encryption. A
    `seq` in one is the chain position of every row the reader can see, handed over one
    decode at a time."""
    cursor = a_view(reader("u_auditor", "read:audit.*")).page(limit=2).next_cursor
    assert cursor is not None

    decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))

    assert "seq" not in decoded
    assert not COUNTING_NAMES & set(decoded)
    assert all(not isinstance(v, int) for v in decoded.values())


def test_a_page_is_filled_from_visible_rows_so_its_length_says_nothing_about_the_rest() -> None:
    """The subtraction attack, applied to paging. If the view took a page from the ledger and
    then filtered it, a reader asking for four rows and getting one would know three entries
    they may not see sit in that stretch."""
    view = a_view(reader("u_weiling"))

    first = view.page(limit=1)
    assert len(first.rows) == 1
    assert first.next_cursor is not None

    second = view.page(limit=1, cursor=first.next_cursor)
    assert len(second.rows) == 1
    # exactly two visible rows exist, so the second page ends the list rather than the
    # four entries in between doing it
    assert second.next_cursor is None


def test_the_filter_admits_only_closed_vocabularies() -> None:
    """A free-text filter over a ledger is a search engine over a permission map: run a word
    against everyone's entries and the shape of what you cannot see is the answer."""
    with pytest.raises(ValidationError):
        AuditFilter(query="contract")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AuditFilter(subject_kinds=frozenset({"invoice"}))
    with pytest.raises(ValidationError):
        AuditFilter(actors=frozenset({"u_wei*"}))

    assert set(AuditFilter.model_fields) == {
        "actions",
        "subject_kinds",
        "actors",
        "since",
        "until",
    }


def test_a_list_of_capabilities_is_still_refused_at_the_surface() -> None:
    """The 5 September exception admits one capability, not a permission map. The ledger
    enforces it and the view must not undo it on the way out, nor assemble one by joining
    rows: there is no method here that returns a set of capabilities."""
    chain = AuditChain()
    chain.append(
        action=AuditAction.GRANT,
        actor_id="u_aaron",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace1",
        at=NOW,
        details={"capability": "read:client.name", "capabilities": ["read:hr.salary"]},
    )

    row = a_view(reader("u_weiling"), chain.entries).page().rows[0]

    assert row.details["capability"] == "read:client.name"
    assert row.details["capabilities"] == REDACTED
    assert not [n for n in dir(AuditView) if not n.startswith("_") and n != "page"]


def test_every_subject_kind_is_governed_by_a_capability() -> None:
    """A kind with no capability behind it is a kind the view has no rule for, and the
    honest answer to a shape nobody decided about is no. Building the table from
    SUBJECT_KINDS means a new kind cannot arrive ungoverned."""
    assert set(CAPABILITY_BY_KIND) == SUBJECT_KINDS


# ------------------------------------------------- the recording seam (M24.1.3)
def test_every_auditable_action_has_a_recorder_and_no_recorder_invents_one() -> None:
    """The pairing that makes forgetting hard. An action added to the vocabulary with no way
    to record it is a rule with no mechanism behind it, and a recorder writing an action
    nobody declared is a code path nothing else in the system knows about."""
    assert set(ACTION_BY_METHOD.values()) == set(AuditAction)
    assert len(ACTION_BY_METHOD) == len(AuditAction)


def test_no_recorder_method_lets_a_caller_choose_the_action_or_the_details() -> None:
    """One function per action is the whole design: a caller who can name an action can
    write a grant that says deny, and a caller who can pass a details mapping can put a
    value in the longest-retained table in the system."""
    public = {name for name in dir(AuditRecorder) if not name.startswith("_")}
    assert public == set(ACTION_BY_METHOD)

    for name in public:
        parameters = set(inspect.signature(getattr(AuditRecorder, name)).parameters)
        assert "action" not in parameters, name
        assert "details" not in parameters, name


# ----------------------------------------------- the verification job (M24.1.2)
def test_a_verification_run_never_reports_continuity_without_saying_what_it_did_not_check() -> None:
    """The false-assurance failure, which is the one this job can cause.

    A chain walk proves that nothing inside the window was quietly edited. It cannot prove
    that the window is all there ever was, because deleting the newest entries leaves a
    shorter chain that verifies perfectly. A report that says "verified" and stops has told
    a reader something stronger than it checked, and a green tick over a truncated ledger is
    worse than no tick at all.
    """
    chain = a_ledger()
    report = verify_window(chain.entries, at=NOW)

    assert report.continuous
    assert report.completeness is Completeness.UNANCHORED
    assert report.caveats, "a run with no anchor reported nothing it had failed to check"
    assert any("completeness" in caveat for caveat in report.caveats)
    assert all(caveat in report.summary() for caveat in report.caveats)

    # and there is no attribute a dashboard could read as an unqualified pass
    assert not {"verified", "ok", "passed", "valid"} & set(type(report).model_fields)


def test_a_report_always_carries_at_least_one_caveat() -> None:
    """Including the strongest run this job can produce. An anchor proves the ledger still
    holds the entry it names; it says nothing about entries written after it, and a reader
    told only "anchored" will assume otherwise."""
    from brain.audit.export import Anchor

    chain = a_ledger()
    anchor = Anchor(
        seq=chain.entries[-1].seq,
        entry_hash=chain.entries[-1].entry_hash,
        recorded_at=NOW,
        recorded_by="svc_verifier",
        where="offsite_object_store",
    )

    report = verify_window(chain.entries, at=NOW, anchors=[anchor])

    assert report.completeness is Completeness.ANCHORED
    assert report.caveats


def test_a_run_that_found_something_wrong_cannot_become_the_next_run_s_baseline() -> None:
    """The failure mode of every checkpointed check ever written: the bad run is recorded as
    the new starting point, the next run begins after the tamper, and the ledger reports
    clean for ever."""
    entries = list(a_ledger().entries)
    entries[3] = entries[3].model_copy(update={"actor_id": "u_somebody_else"})

    broken = verify_window(entries, at=NOW)
    assert not broken.continuous
    assert broken.next_checkpoint(recorded_by="svc_verifier") is None

    from brain.audit.export import Anchor

    truncated = verify_window(
        a_ledger().entries[:2],
        at=NOW,
        anchors=[
            Anchor(
                seq=a_ledger().entries[-1].seq,
                entry_hash=a_ledger().entries[-1].entry_hash,
                recorded_at=NOW,
                recorded_by="svc_verifier",
                where="offsite_object_store",
            )
        ],
    )
    assert truncated.continuous
    assert truncated.completeness is Completeness.ANCHOR_MISSING
    assert truncated.next_checkpoint(recorded_by="svc_verifier") is None
