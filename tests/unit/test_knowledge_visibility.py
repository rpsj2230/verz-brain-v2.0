"""Levels, the upload default, and the two-person gate on widening.

Task ids: M7.4.2, M7.4.3, M7.4.4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.knowledge.visibility import (
    PROMOTION_CAPABILITY,
    Approval,
    KnowledgeVisibility,
    PromotionProposal,
    Visibility,
    VisibilityError,
    admit_upload,
    apply_promotion,
    apply_promotion_level,
    approve_promotion,
    default_for_upload,
    is_wider,
    propose_promotion,
    scope_for,
)

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=90)


def _ents(principal: str, *caps: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
    )


def _proposal(
    *, from_level: Visibility = Visibility.DEPARTMENT, to_level: Visibility = Visibility.COMPANY
) -> PromotionProposal:
    return propose_promotion(
        item_id="k_deployment_sop",
        from_level=from_level,
        to_level=to_level,
        proposer_id="p_wei_ling",
        owner_id="p_wei_ling",
        review_by=LATER,
        reason="every department follows this checklist now",
        now=NOW,
    )


# ------------------------------------------------------------- levels (M7.4.2)
def test_a_personal_scope_without_an_owner_is_refused() -> None:
    """The blank-field case. `Scope()` is the unrestricted scope, so a personal level built
    with no owner is not merely wrong, it is the widest level in the system wearing the name
    of the narrowest. Deleting this turns an empty form field into a company-wide publish."""
    with pytest.raises(VisibilityError, match="needs an owner"):
        scope_for(Visibility.PERSONAL)


def test_a_department_scope_without_a_department_is_refused() -> None:
    """Same failure one level up, and the one `brain.core.department` refuses when appointing
    an admin. Without this, a department upload from somebody whose department was not
    resolved reaches all 126 staff and nothing says so."""
    with pytest.raises(VisibilityError, match="needs a department"):
        scope_for(Visibility.DEPARTMENT, owner_id="p_wei_ling")


def test_company_visibility_is_the_unrestricted_scope() -> None:
    """The one level that is legitimately unrestricted. If this stopped being true, company
    knowledge would carry a predicate nobody matches and the handbook would be unreachable
    for everyone, which is a failure that looks like an empty knowledge base."""
    assert scope_for(Visibility.COMPANY).is_unrestricted()


def test_a_personal_scope_admits_its_owner_and_nobody_else() -> None:
    """The predicate has to actually discriminate. A personal scope that matched every row
    would pass every test about its shape while publishing every working note."""
    scope = scope_for(Visibility.PERSONAL, owner_id="p_wei_ling")
    assert scope.matches({"owner_id": "p_wei_ling"})
    assert not scope.matches({"owner_id": "p_priya"})
    assert not scope.matches({})


def test_the_levels_are_ordered_narrowest_first() -> None:
    """Every widening check in the module reads this order. Reversing it during a merge would
    invert `admit_upload` and `supersede` at once, and both would keep passing their own
    tests because they would agree with each other."""
    assert is_wider(Visibility.COMPANY, Visibility.DEPARTMENT)
    assert is_wider(Visibility.DEPARTMENT, Visibility.PERSONAL)
    assert not is_wider(Visibility.PERSONAL, Visibility.COMPANY)
    assert not is_wider(Visibility.COMPANY, Visibility.COMPANY)


# ------------------------------------------------------- the upload (M7.4.3)
def test_an_upload_lands_in_the_uploader_s_department_by_default() -> None:
    """The default is the whole rule in practice, because almost nobody chooses. If it drifted
    to company, every upload would silently widen the company's exposure, which is the failure
    the architecture describes as impossible to explain a year later."""
    assert default_for_upload("web") is Visibility.DEPARTMENT
    assert admit_upload(None, uploader_department="web") is Visibility.DEPARTMENT


def test_an_upload_from_somebody_with_no_department_is_personal() -> None:
    """Falling back to department with an empty department name would build an unrestricted
    scope, which `scope_for` refuses. Deleting this leaves that refusal as the only thing
    between an unassigned account and a company-wide publish."""
    assert admit_upload(None, uploader_department="") is Visibility.PERSONAL


def test_an_upload_may_choose_a_narrower_level_than_its_default() -> None:
    """Narrowing shows the item to nobody new, so it needs no gate. Refusing it would make
    people upload drafts at department level because the form would not let them do otherwise,
    which is the opposite of what this milestone is for."""
    assert admit_upload(Visibility.PERSONAL, uploader_department="web") is Visibility.PERSONAL


def test_an_upload_asking_for_a_wider_level_is_refused() -> None:
    """M7.4.3 itself. Without it there is no promotion gate at all: the upload form is the
    promotion path, and it has one participant and no review date."""
    with pytest.raises(VisibilityError, match="cannot be stored at"):
        admit_upload(Visibility.COMPANY, uploader_department="web")


# ---------------------------------------------------- the gated path (M7.4.4)
def test_a_proposal_that_would_narrow_is_refused() -> None:
    """Narrowing through the approval path would teach people that the path is for any
    visibility change, and an approver who sees mostly harmless narrowings stops reading the
    ones that widen."""
    with pytest.raises(ValueError, match="narrowing needs no approval"):
        _proposal(from_level=Visibility.COMPANY, to_level=Visibility.DEPARTMENT)


def test_a_proposal_with_a_review_date_already_behind_us_is_refused() -> None:
    """A lapsed review date opens a re-verification task on the day of the promotion. The
    second time an owner is nagged about something nobody has read yet, the notification stops
    being read at all, and that is the control lost."""
    with pytest.raises(VisibilityError, match="not in the future"):
        propose_promotion(
            item_id="k_deployment_sop",
            from_level=Visibility.DEPARTMENT,
            to_level=Visibility.COMPANY,
            proposer_id="p_wei_ling",
            owner_id="p_wei_ling",
            review_by=NOW - timedelta(days=1),
            reason="overdue",
            now=NOW,
        )


def test_a_proposal_with_a_naive_review_date_is_refused() -> None:
    """A naive date compared against a UTC sweep is wrong by the machine's offset, silently
    and by a few hours. Deleting this makes review dates fire on the wrong day in a way no
    test about the sweep would catch."""
    with pytest.raises(ValueError, match="timezone-aware"):
        propose_promotion(
            item_id="k_deployment_sop",
            from_level=Visibility.DEPARTMENT,
            to_level=Visibility.COMPANY,
            proposer_id="p_wei_ling",
            owner_id="p_wei_ling",
            review_by=datetime(2026, 12, 1),
            reason="published",
            now=NOW,
        )


def test_a_proposer_cannot_approve_their_own_proposal() -> None:
    """The gate defeated while every audit record still looks right: a proposal, an approval,
    and the same person twice. This is the single check that makes it a two-person path."""
    proposal = _proposal()
    with pytest.raises(VisibilityError, match="cannot approve it"):
        approve_promotion(
            proposal,
            approver_id="p_wei_ling",
            entitlement=_ents("p_wei_ling", PROMOTION_CAPABILITY.value),
            now=NOW,
        )


def test_an_approver_without_the_capability_is_refused() -> None:
    """Nobody granted them the path. Deleting this makes the approval step a formality any
    logged-in account can perform, which is a gate with a door and no lock."""
    proposal = _proposal()
    with pytest.raises(VisibilityError, match="does not hold"):
        approve_promotion(
            proposal,
            approver_id="p_priya",
            entitlement=_ents("p_priya", "read:client.name"),
            now=NOW,
        )


def test_an_approval_is_made_out_of_the_approver_s_own_reach() -> None:
    """The wrong-variable-in-scope bug: a handler passes a name in one argument and somebody
    else's entitlement in another, and the promotion is approved on behalf of a person who was
    never asked. Nothing else in the signature notices."""
    proposal = _proposal()
    with pytest.raises(VisibilityError, match="belongs to"):
        approve_promotion(
            proposal,
            approver_id="p_priya",
            entitlement=_ents("p_ravi", PROMOTION_CAPABILITY.value),
            now=NOW,
        )


def test_an_approval_carries_the_promotion_through() -> None:
    """The happy path. If this fails nothing can ever be published to the company, and the
    workaround is somebody adding a visibility field to a form."""
    proposal = _proposal()
    approval = approve_promotion(
        proposal,
        approver_id="p_priya",
        entitlement=_ents("p_priya", PROMOTION_CAPABILITY.value),
        now=NOW,
    )
    assert apply_promotion(proposal, approval, current_level=Visibility.DEPARTMENT) is (
        Visibility.COMPANY
    )


def test_an_approval_for_one_proposal_does_not_apply_to_another() -> None:
    """An approval and the thing it approved must not come apart. Without the digest, an
    approval collected for a narrow widening could be spent on a different item's promotion by
    a caller that reused the variable."""
    approved = _proposal()
    approval = approve_promotion(
        approved,
        approver_id="p_priya",
        entitlement=_ents("p_priya", PROMOTION_CAPABILITY.value),
        now=NOW,
    )
    other = propose_promotion(
        item_id="k_client_contract",
        from_level=Visibility.DEPARTMENT,
        to_level=Visibility.COMPANY,
        proposer_id="p_wei_ling",
        owner_id="p_wei_ling",
        review_by=LATER,
        reason="different document entirely",
        now=NOW,
    )
    with pytest.raises(VisibilityError, match="different proposal"):
        apply_promotion(other, approval, current_level=Visibility.DEPARTMENT)


def test_an_approval_cannot_be_spent_on_a_level_it_was_not_given_for() -> None:
    """`apply_promotion_level` is the door for a caller holding only the item, so it has to
    check what the approval was actually for. Without it, an approval to widen from personal to
    department would widen to company."""
    approval = Approval(
        proposal_digest="0" * 32,
        approver_id="p_priya",
        approved_at=NOW,
        item_id="k_notes",
        from_level=Visibility.PERSONAL,
        to_level=Visibility.DEPARTMENT,
    )
    with pytest.raises(VisibilityError, match="cannot be spent"):
        apply_promotion_level(Visibility.PERSONAL, Visibility.COMPANY, approval)


def test_a_visibility_value_recomputes_its_predicate_rather_than_storing_one() -> None:
    """Two fields that must agree are one field with a constructor. A stored predicate that
    drifted from the stored level would make "who can read this" a question with two answers,
    and the query would use the wrong one."""
    personal = KnowledgeVisibility.personal("p_wei_ling")
    assert personal.scope().matches({"owner_id": "p_wei_ling"})
    department = KnowledgeVisibility.of_department("web")
    assert department.scope().matches({"department": "web"})
    assert not department.scope().matches({"department": "finance"})
