"""The knowledge record, supersession, the review sweep and the badge.

Task ids: M7.4.1, M7.4.5, M7.4.6, M7.4.7
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.knowledge.item import (
    BADGE_TEXT,
    KnowledgeError,
    KnowledgeItem,
    KnowledgeState,
    VerificationState,
    badge,
    due_for_reverification,
    retrievable,
    supersede,
)
from brain.knowledge.visibility import KnowledgeVisibility, Visibility

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
VERIFIED_AT = NOW - timedelta(days=200)
REVIEW_BY = NOW + timedelta(days=30)


def _item(
    item_id: str = "k_deployment_sop",
    *,
    level: Visibility = Visibility.DEPARTMENT,
    state: KnowledgeState = KnowledgeState.PUBLISHED,
    verified: bool = True,
    review_by: datetime | None = REVIEW_BY,
) -> KnowledgeItem:
    visibility = (
        KnowledgeVisibility.of_department("web")
        if level is Visibility.DEPARTMENT
        else KnowledgeVisibility(level=level, owner_id="p_wei_ling", department="web")
    )
    return KnowledgeItem(
        item_id=item_id,
        content="Deploy on a Tuesday. Never on a Friday.",
        title="Web deployment SOP",
        visibility=visibility,
        owner_id="p_wei_ling",
        state=state,
        verified_by="p_priya" if verified else "",
        verified_at=VERIFIED_AT if verified else None,
        review_by=review_by,
    )


# ---------------------------------------------------------- the record (M7.4.1)
def test_half_a_verification_is_refused() -> None:
    """A verifier with no date renders as authoritative while saying when it was true is
    impossible; a date with no verifier names nobody answerable. Deleting this lets the badge
    claim verification that nobody can be asked about."""
    with pytest.raises(ValueError, match="half a verification"):
        KnowledgeItem(
            item_id="k_x",
            content="something",
            visibility=KnowledgeVisibility.personal("p_wei_ling"),
            owner_id="p_wei_ling",
            verified_by="p_priya",
        )


def test_a_review_date_at_or_before_the_verification_is_refused() -> None:
    """It opens a re-verification task the day the item is written. Two of those and the owner
    filters the notification, which loses the control for every item they own."""
    with pytest.raises(ValueError, match="due for review at or before"):
        _item(review_by=VERIFIED_AT - timedelta(days=1))


def test_a_naive_date_is_refused() -> None:
    """The same silent, offset-wide error `Principal.not_after` refuses. A review that fires
    eight hours early is indistinguishable from one that fires correctly until somebody looks
    at a timezone."""
    with pytest.raises(ValueError, match="timezone-aware"):
        _item(review_by=datetime(2026, 12, 1))


def test_company_knowledge_cannot_be_published_unverified() -> None:
    """Company scope is the level nobody double-checks, because everybody assumes somebody
    else did. Deleting this puts anonymous claims behind the company's authority, which is the
    exact content a "verified" badge is supposed to distinguish."""
    with pytest.raises(ValueError, match="nobody has verified it"):
        _item(level=Visibility.COMPANY, verified=False)


def test_the_predicate_comes_from_the_visibility_and_is_not_stored_twice() -> None:
    """If the scope were a second stored field it could disagree with the level, and whichever
    the query used would be the one that mattered. This keeps one answer to "who can read
    this"."""
    assert _item().scope.matches({"department": "web"})
    assert not _item().scope.matches({"department": "finance"})


# ------------------------------------------------------ supersession (M7.4.5)
def test_a_newer_version_marks_the_older_superseded_and_keeps_it() -> None:
    """Both halves matter. Without the mark, two versions both claim to be current and
    retrieval returns whichever the index reached first. Without keeping it, an answer given
    last month becomes unexplainable, and an answer nobody can explain is one nobody can
    correct."""
    old, new = supersede(_item("k_sop_v1"), _item("k_sop_v2"))
    assert old.state is KnowledgeState.SUPERSEDED
    assert old.content
    assert new.supersedes == "k_sop_v1"
    assert new.state is KnowledgeState.PUBLISHED


def test_an_item_cannot_supersede_itself() -> None:
    """A loop nothing resolves, and one that renders in the console as a document that
    replaced itself. It arrives from a re-upload path that reuses the id."""
    with pytest.raises(KnowledgeError, match="cannot supersede itself"):
        supersede(_item("k_sop_v1"), _item("k_sop_v1"))


def test_an_already_superseded_item_is_not_superseded_again() -> None:
    """Version three must replace version two, not version one. Deleting this leaves two
    successors both claiming to be current, which is the state supersession exists to
    prevent."""
    old = _item("k_sop_v1", state=KnowledgeState.SUPERSEDED)
    with pytest.raises(KnowledgeError, match="already superseded"):
        supersede(old, _item("k_sop_v3"))


def test_a_successor_may_be_narrower_than_what_it_replaces() -> None:
    """Narrowing needs no gate, and refusing it would mean a document that turned out to be
    sensitive could not be replaced by a restricted version without deleting the history."""
    old, new = supersede(
        _item("k_sop_v1"),
        _item("k_sop_v2", level=Visibility.PERSONAL),
    )
    assert new.visibility.level is Visibility.PERSONAL
    assert old.state is KnowledgeState.SUPERSEDED


# ----------------------------------------------------- re-verification (M7.4.6)
def test_only_items_past_their_review_date_are_due() -> None:
    """A sweep that returned everything would open a task per document per run, and the owner
    would dismiss the batch. The date is the whole filter."""
    due = due_for_reverification(
        [_item("k_current"), _item("k_lapsed", review_by=NOW - timedelta(days=1))], now=NOW
    )
    assert [task.item_id for task in due] == ["k_lapsed"]


def test_an_item_with_no_review_date_is_never_due() -> None:
    """Personal working notes carry no review date and must not generate tasks. Deleting this
    makes the sweep fire on every draft anybody has ever uploaded."""
    assert due_for_reverification([_item(review_by=None)], now=NOW) == ()


def test_a_superseded_item_is_never_due_for_review() -> None:
    """Asking somebody to re-verify a document that has already been replaced is the fastest
    way to teach them the notification is noise, and once that is learnt it applies to every
    other item they own."""
    lapsed = _item("k_sop_v1", state=KnowledgeState.SUPERSEDED, review_by=NOW - timedelta(days=1))
    assert due_for_reverification([lapsed], now=NOW) == ()


def test_a_lead_time_opens_the_task_before_the_date_rather_than_after() -> None:
    """A review that can only be noticed once it has lapsed is a review that is always late.
    Without a lead time the console can only ever show overdue work."""
    soon = _item("k_soon", review_by=NOW + timedelta(days=3))
    assert due_for_reverification([soon], now=NOW) == ()
    early = due_for_reverification([soon], now=NOW, lead_time=timedelta(days=7))
    assert [task.item_id for task in early] == ["k_soon"]


def test_the_due_list_is_ordered_the_same_way_on_every_run() -> None:
    """An order that followed the input would make the console list depend on whatever
    `ORDER BY` the query happened to carry, so two operators looking at the same screen would
    disagree about what is most overdue."""
    items = [
        _item("k_b", review_by=NOW - timedelta(days=1)),
        _item("k_a", review_by=NOW - timedelta(days=1)),
        _item("k_c", review_by=NOW - timedelta(days=9)),
    ]
    assert [task.item_id for task in due_for_reverification(items, now=NOW)] == [
        "k_c",
        "k_a",
        "k_b",
    ]
    assert due_for_reverification(items, now=NOW) == due_for_reverification(
        list(reversed(items)), now=NOW
    )


def test_a_reverification_message_names_one_item_and_no_total() -> None:
    """A nag opening with "you have 14 items overdue" is dismissed as a batch, and the one
    that mattered goes with the rest. One task, one item, no arithmetic."""
    (task,) = due_for_reverification([_item(review_by=NOW - timedelta(days=1))], now=NOW)
    assert task.message() == "Web deployment SOP was due for review on 2026-09-04."


# -------------------------------------------------------------- badge (M7.4.7)
def test_a_verified_item_says_who_vouched_for_it_and_when() -> None:
    """A badge that said only "verified" is a badge nobody can follow up. The point of the
    verification record is that there is a person to ask."""
    rendered = badge(_item(), now=NOW).render()
    assert "p_priya" in rendered
    assert VERIFIED_AT.date().isoformat() in rendered


def test_a_badge_goes_due_when_the_review_date_passes() -> None:
    """The state is derived from the clock rather than stored, so it cannot go stale. If it
    were stored, an item would keep rendering as verified for as long as nothing rewrote the
    row."""
    lapsed = _item(review_by=NOW - timedelta(days=1))
    assert badge(lapsed, now=NOW).state is VerificationState.DUE
    assert badge(lapsed, now=NOW - timedelta(days=30)).state is VerificationState.VERIFIED


def test_an_unverified_item_is_reported_as_unverified_rather_than_as_wrong() -> None:
    """They are different things, and a badge that conflated them would be ignored on the day
    it mattered. Nobody has vouched for it is a fact; it is incorrect is a claim we cannot
    make."""
    rendered = badge(_item(verified=False, review_by=None), now=NOW).render()
    assert rendered == BADGE_TEXT[VerificationState.UNVERIFIED]
    assert "wrong" not in rendered
    assert "incorrect" not in rendered


def test_a_superseded_item_says_so_before_it_says_verified() -> None:
    """A replaced item verified last year would otherwise render as verified, which is true
    and misleading. The reader needs to know a newer version exists far more than they need to
    know who signed the old one."""
    replaced = _item(state=KnowledgeState.SUPERSEDED)
    assert badge(replaced, now=NOW).state is VerificationState.SUPERSEDED
    assert "p_priya" not in badge(replaced, now=NOW).render()


def test_every_badge_state_has_wording() -> None:
    """A state added without a sentence renders as a key error or as nothing at all, and the
    badge silently disappears from the answer it was meant to qualify."""
    assert set(BADGE_TEXT) == set(VerificationState)
    assert all(text.strip() for text in BADGE_TEXT.values())


def test_retrievable_returns_what_survived_and_never_how_much_did_not() -> None:
    """The filter has no second return value on purpose. A count of what it removed is a count
    of things the asker was not allowed to see, arrived at by subtraction."""
    items = [_item("k_live"), _item("k_old", state=KnowledgeState.SUPERSEDED)]
    assert [item.item_id for item in retrievable(items)] == ["k_live"]
