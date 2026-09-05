"""The badge one reader may be shown, and the sweep that opens re-verification tasks.

Task ids: M7.4.6, M7.4.7
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Clause, Op, Scope
from brain.knowledge.item import (
    KnowledgeError,
    KnowledgeItem,
    KnowledgeState,
    VerificationState,
)
from brain.knowledge.search import KNOWLEDGE_READ
from brain.knowledge.verification import (
    ATTRIBUTABLE_STATES,
    DEFAULT_CADENCE,
    NO_TASKS_OPENED,
    TASKS_PER_OWNER_PER_RUN,
    UNATTRIBUTED_BADGE_TEXT,
    VERIFIER_CAPABILITY,
    DisclosedBadge,
    ReverificationRun,
    disclose,
    key_for,
    may_name_verifier,
    open_reverification_tasks,
)
from brain.knowledge.visibility import KnowledgeVisibility, Visibility

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
VERIFIED_AT = NOW - timedelta(days=200)
REVIEW_BY = NOW + timedelta(days=30)
LAPSED = NOW - timedelta(days=1)

VERIFIER = "p_priya"
OWNER = "p_wei_ling"

ANY_KNOWLEDGE = Capability(value="read:knowledge.*")
WEB = Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),))
FINANCE = Scope(clauses=(Clause(field="department", op=Op.EQ, value="finance"),))


def _item(
    item_id: str = "k_deployment_sop",
    *,
    level: Visibility = Visibility.DEPARTMENT,
    state: KnowledgeState = KnowledgeState.PUBLISHED,
    verified: bool = True,
    owner_id: str = OWNER,
    review_by: datetime | None = REVIEW_BY,
) -> KnowledgeItem:
    visibility = (
        KnowledgeVisibility.of_department("web")
        if level is Visibility.DEPARTMENT
        else KnowledgeVisibility(level=level, owner_id=owner_id)
    )
    return KnowledgeItem(
        item_id=item_id,
        content="Deploy on a Tuesday. Never on a Friday.",
        title="Web deployment SOP",
        visibility=visibility,
        owner_id=owner_id,
        state=state,
        verified_by=VERIFIER if verified else "",
        verified_at=VERIFIED_AT if verified else None,
        review_by=review_by,
    )


def _reader(
    principal_id: str = "p_reader",
    *,
    capability: Capability = VERIFIER_CAPABILITY,
    scope: Scope | None = None,
    not_after: datetime | None = None,
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=(Grant(capability=capability, scope=scope or Scope.unrestricted()),),
        not_after=not_after,
    )


NOBODY = EntitlementSet(principal_id="p_stranger")


# ------------------------------------------------------- the badge as disclosed (M7.4.7)
def test_a_reader_who_may_not_see_the_verifier_is_still_told_the_item_is_verified() -> None:
    """This is the whole leaf. A badge that vanished for an unentitled reader would read as
    reassurance on the documents nobody has checked, and a badge that named the verifier
    anyway would disclose a colleague's activity to somebody holding less than they do.
    Deleting this lets either half regress without anything failing."""
    shown = disclose(_item(), reader=NOBODY, now=NOW)
    assert shown.state is VerificationState.VERIFIED
    assert not shown.names_the_verifier
    assert VERIFIER not in shown.render()
    assert shown.render() == UNATTRIBUTED_BADGE_TEXT[VerificationState.VERIFIED]


def test_a_verified_and_an_unverified_item_never_render_alike_to_the_same_reader() -> None:
    """The distinction the badge exists for. If withholding the name collapsed the two
    states, the badge's presence would train people to trust everything, which is worse than
    having no badge at all."""
    verified = disclose(_item(), reader=NOBODY, now=NOW).render()
    unverified = disclose(_item(verified=False, review_by=None), reader=NOBODY, now=NOW).render()
    assert verified != unverified
    assert verified and unverified


def test_a_reader_holding_the_capability_in_scope_is_told_who_vouched_and_when() -> None:
    """The positive case beside the refusals. A guard tested only by what it withholds is
    satisfied by a function that withholds everything, and a badge that never names anybody
    is a verification record nobody can follow up."""
    shown = disclose(_item(), reader=_reader(scope=WEB), now=NOW)
    assert shown.names_the_verifier
    assert VERIFIER in shown.render()
    assert VERIFIED_AT.date().isoformat() in shown.render()


def test_reaching_a_document_does_not_confer_the_name_of_whoever_signed_it() -> None:
    """`read:knowledge` is what retrieval needs and it must not carry the verifier with it,
    because the item's scope decides who reads the content and says nothing about whose name
    travels beside it. Deleting this makes every reader of a document a reader of its
    verifier, which is the disclosure this module exists to gate."""
    assert not may_name_verifier(_item(), _reader(capability=KNOWLEDGE_READ), now=NOW)
    assert may_name_verifier(_item(), _reader(capability=ANY_KNOWLEDGE), now=NOW)


def test_a_grant_outside_the_items_place_does_not_name_its_verifier() -> None:
    """The scope half of the check. Without it the capability alone would name verifiers
    company-wide, so a grant written for one department would disclose activity in every
    other one."""
    assert not may_name_verifier(_item(), _reader(scope=FINANCE), now=NOW)
    assert may_name_verifier(_item(), _reader(scope=WEB), now=NOW)


def test_a_company_item_names_its_verifier_only_to_an_unrestricted_grant() -> None:
    """A company-visibility item carries no department, so a departmental clause admits
    nothing and the badge fails closed. Deleting this lets the fail-closed direction invert
    silently the day somebody makes an absent field permissive."""
    company = _item(level=Visibility.COMPANY)
    assert not may_name_verifier(company, _reader(scope=WEB), now=NOW)
    assert may_name_verifier(company, _reader(), now=NOW)


def test_the_person_who_verified_an_item_is_always_told_they_verified_it() -> None:
    """Withholding somebody's own act from them protects nobody and makes the badge look
    broken to the one reader who knows the answer. It is a decision rather than something the
    grant model produces, so nothing else would catch its removal."""
    assert may_name_verifier(_item(), EntitlementSet(principal_id=VERIFIER), now=NOW)


def test_an_expired_reader_is_told_nothing_about_who_verified_anything() -> None:
    """Expiry arrives through `scope_for` rather than through a check written here, which is
    where a leaver stops being told about their old department. Deleting this lets a
    contractor's badge keep naming colleagues after their access ended."""
    assert not may_name_verifier(_item(), _reader(not_after=NOW - timedelta(days=1)), now=NOW)
    assert may_name_verifier(_item(), _reader(not_after=NOW + timedelta(days=3650)), now=NOW)


def test_an_unattributed_badge_cannot_hold_the_name_it_may_not_render() -> None:
    """Structural rather than cosmetic. A badge carrying the verifier and declining to print
    them would leak through the first trace, log line or serialisation that touched it, and
    that is the failure a formatting decision cannot prevent."""
    withheld = disclose(_item(), reader=NOBODY, now=NOW)
    assert withheld.verified_by == ""
    assert withheld.verified_at is None
    with pytest.raises(ValueError, match="cannot carry a verifier"):
        DisclosedBadge(
            state=VerificationState.UNVERIFIED, verified_by=VERIFIER, verified_at=VERIFIED_AT
        )


def test_half_a_verification_cannot_be_disclosed() -> None:
    """A name with no date renders as authoritative while saying when it was true is
    impossible, and a date with no name is answerable to nobody. The record refuses the same
    shape; a badge can be built without one, so it refuses it too."""
    with pytest.raises(ValueError, match="half a verification"):
        DisclosedBadge(state=VerificationState.VERIFIED, verified_by=VERIFIER)


def test_the_states_that_name_nobody_read_identically_to_every_reader() -> None:
    """An unverified or replaced item withholds nothing, so its sentence must not vary with
    the reader. If it did, the badge would tell somebody about their own entitlement on items
    where there was never anything to disclose."""
    for item in (_item(verified=False, review_by=None), _item(state=KnowledgeState.SUPERSEDED)):
        assert disclose(item, reader=NOBODY, now=NOW) == disclose(item, reader=_reader(), now=NOW)


def test_no_unattributed_wording_carries_a_name_a_date_or_a_number() -> None:
    """A count or a date would be added back by whoever wanted the badge to be more useful,
    and both are facts about a colleague's activity. Every state needs wording, or the badge
    silently disappears from the answer it was meant to qualify."""
    assert set(UNATTRIBUTED_BADGE_TEXT) == set(VerificationState)
    for text in UNATTRIBUTED_BADGE_TEXT.values():
        assert text.strip()
        assert not any(character.isdigit() for character in text)
        assert "{" not in text


def test_a_superseded_item_reports_replacement_rather_than_naming_its_old_verifier() -> None:
    """The ordering is inherited from `item.badge` and must survive the disclosure layer. A
    replaced item verified last year would otherwise render as verified to an entitled
    reader, which is true and misleading."""
    replaced = disclose(_item(state=KnowledgeState.SUPERSEDED), reader=_reader(), now=NOW)
    assert replaced.state is VerificationState.SUPERSEDED
    assert VERIFIER not in replaced.render()
    assert VerificationState.SUPERSEDED not in ATTRIBUTABLE_STATES


def test_a_lapsed_item_is_reported_as_due_to_a_reader_who_may_not_see_the_verifier() -> None:
    """The review state is a fact about the document rather than about a colleague, so it is
    shown to everybody. Deleting this lets an out-of-date document read as merely verified to
    most of the company."""
    shown = disclose(_item(review_by=LAPSED), reader=NOBODY, now=NOW)
    assert shown.state is VerificationState.DUE
    assert not shown.names_the_verifier
    assert shown.render() != disclose(_item(), reader=NOBODY, now=NOW).render()


# -------------------------------------------------------- the scheduled job (M7.4.6)
def _due(item_id: str, *, owner_id: str = OWNER, days_over: int = 1) -> KnowledgeItem:
    return _item(item_id, owner_id=owner_id, review_by=NOW - timedelta(days=days_over))


def test_a_second_run_over_the_same_overdue_item_opens_no_second_task() -> None:
    """Idempotence across runs, which is the property that decides whether anybody reads the
    queue. A sweep on a daily cadence would otherwise open one task per morning per lapsed
    document, and the queue becomes noise within a week."""
    first = open_reverification_tasks([_due("k_a")], now=NOW)
    assert [task.item_id for task in first.tasks] == ["k_a"]
    second = open_reverification_tasks([_due("k_a")], now=NOW + DEFAULT_CADENCE, log=first.log)
    assert second.tasks == ()


def test_one_item_may_not_appear_twice_in_a_single_sweep() -> None:
    """Two rows for one item id mean the caller handed us the same document twice, and
    resolving it silently would make the version somebody is asked to re-verify depend on the
    order the query returned. Deleting this turns a broken query into a duplicate task."""
    with pytest.raises(KnowledgeError, match="appears twice"):
        open_reverification_tasks([_due("k_a"), _due("k_a", days_over=9)], now=NOW)


def test_no_owner_is_handed_more_than_the_per_run_bound() -> None:
    """A review date passing for a whole day's import hands one person the entire backlog in
    one morning, which gets the same amount done as handing them none. Deleting this makes
    the bound cosmetic and the queue unusable on exactly the day it matters."""
    items = [_due(f"k_{index:02d}", days_over=index + 1) for index in range(20)]
    run = open_reverification_tasks(items, now=NOW)
    assert len(run.tasks) == TASKS_PER_OWNER_PER_RUN
    assert run.more_waiting


def test_the_bound_is_counted_per_owner_rather_than_over_the_whole_run() -> None:
    """A run-wide ceiling would be silently unfair: whoever sorted first would take the entire
    allowance and everybody else would be given nothing with no record saying so. The bound
    has to follow the person whose queue it is."""
    items = [
        _due(f"k_{owner}_{index}", owner_id=owner, days_over=index + 1)
        for owner in ("p_wei_ling", "p_arun")
        for index in range(TASKS_PER_OWNER_PER_RUN + 2)
    ]
    run = open_reverification_tasks(items, now=NOW)
    opened = [task.owner_id for task in run.tasks]
    assert opened.count("p_wei_ling") == TASKS_PER_OWNER_PER_RUN
    assert opened.count("p_arun") == TASKS_PER_OWNER_PER_RUN


def test_an_item_held_back_by_the_bound_is_still_due_on_the_next_run() -> None:
    """This is what makes a small bound safe rather than lossy: nothing is written to the log
    for a deferred item, so it returns at the head of the next run. Deleting this lets a
    future change record deferred items and silently drop them forever."""
    items = [_due(f"k_{index:02d}", days_over=index + 1) for index in range(8)]
    first = open_reverification_tasks(items, now=NOW)
    second = open_reverification_tasks(items, now=NOW + DEFAULT_CADENCE, log=first.log)
    assert len(first.tasks) + len(second.tasks) == len(items)
    assert not {task.item_id for task in first.tasks} & {task.item_id for task in second.tasks}


def test_an_item_already_in_the_log_does_not_consume_its_owners_allowance() -> None:
    """The order of the two skips. If a logged item counted against the bound, an owner with
    five open tasks would never be given a sixth however long the sweep ran, and the queue
    would deadlock on its own memory with nothing reporting it."""
    items = [_due(f"k_{index:02d}", days_over=index + 1) for index in range(8)]
    first = open_reverification_tasks(items, now=NOW)
    second = open_reverification_tasks(items, now=NOW + DEFAULT_CADENCE, log=first.log)
    assert len(second.tasks) == min(TASKS_PER_OWNER_PER_RUN, len(items) - len(first.tasks))


def test_a_run_reports_that_it_deferred_without_reporting_how_much() -> None:
    """A number here would be a count of documents the operator reading it may not be
    entitled to see, arrived at by the same subtraction the whole system refuses. A boolean
    still tells them the sweep is behind."""
    quiet = open_reverification_tasks([_due("k_a")], now=NOW)
    assert quiet.more_waiting is False
    busy = open_reverification_tasks(
        [_due(f"k_{index:02d}", days_over=index + 1) for index in range(9)], now=NOW
    )
    assert busy.more_waiting is True
    assert all(field.type != "int" for field in fields(ReverificationRun))


def test_re_verifying_an_item_lets_a_later_review_date_open_a_new_task() -> None:
    """The log is keyed on the review date as well as the item, so the control survives its
    own memory. Keyed on the item alone, a document verified again would never be reviewed
    again, and the sweep would go quiet without anybody noticing."""
    first = open_reverification_tasks([_due("k_a")], now=NOW)
    later = NOW + timedelta(days=400)
    renewed = _item("k_a", review_by=later - timedelta(days=1))
    second = open_reverification_tasks([renewed], now=later, log=first.log)
    assert [task.item_id for task in second.tasks] == ["k_a"]
    assert key_for(second.tasks[0]) != key_for(first.tasks[0])


def test_the_same_items_in_a_different_order_open_the_same_tasks() -> None:
    """The bound picks a subset, so which subset must not depend on the order the query
    returned. Without this, two runs over one corpus would ask different people about
    different documents and neither would be the most overdue."""
    items = [_due(f"k_{index:02d}", days_over=index + 1) for index in range(9)]
    forwards = open_reverification_tasks(items, now=NOW)
    backwards = open_reverification_tasks(list(reversed(items)), now=NOW)
    assert forwards.tasks == backwards.tasks


def test_the_longest_overdue_item_is_the_one_the_bound_lets_through() -> None:
    """A bound that kept the newest would leave the oldest lapse growing forever while the
    console showed a queue that was being worked. Deleting this lets the selection become
    whatever order the input happened to have."""
    items = [_due(f"k_{index:02d}", days_over=index + 1) for index in range(9)]
    run = open_reverification_tasks(items, now=NOW)
    assert [task.item_id for task in run.tasks] == ["k_08", "k_07", "k_06", "k_05", "k_04"]


def test_a_lead_time_shorter_than_the_cadence_is_refused() -> None:
    """They are set independently and are silently inconsistent together: a monthly sweep
    with no lead time opens a task up to a month after the item lapsed, and the only symptom
    is a queue that is always late with nothing explaining why."""
    with pytest.raises(ValueError, match="shorter than"):
        open_reverification_tasks(
            [_due("k_a")], now=NOW, cadence=timedelta(days=30), lead_time=timedelta(days=7)
        )


def test_a_bound_below_one_is_refused_rather_than_opening_nothing() -> None:
    """A sweep configured to open no tasks looks exactly like a sweep with nothing to do, and
    the review control would be off with every dashboard green."""
    with pytest.raises(ValueError, match="per-owner bound"):
        open_reverification_tasks([_due("k_a")], now=NOW, per_owner=0)


def test_a_naive_now_is_refused_by_the_sweep() -> None:
    """A naive `now` is the whole width of a timezone offset, and left to the comparison
    inside the knowledge model it surfaces as a TypeError out of datetime arithmetic, which
    reads as a bug in the record rather than in the caller."""
    with pytest.raises(ValueError, match="timezone-aware"):
        open_reverification_tasks([_due("k_a")], now=datetime(2026, 9, 5))


def test_an_empty_log_opens_tasks_and_a_run_hands_back_the_log_to_keep() -> None:
    """The two halves come back together because a caller who forgets to store the log
    debounces nothing at all, silently, while every test of the key still passes."""
    run = open_reverification_tasks([_due("k_a")], now=NOW, log=NO_TASKS_OPENED)
    assert run.tasks
    assert run.log.was_opened(key_for(run.tasks[0]))
    assert not NO_TASKS_OPENED.was_opened(key_for(run.tasks[0]))
