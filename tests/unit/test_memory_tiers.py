"""Learning tiers, held to the ordering that decides everything.

Confidence decides whether to act. Blast radius decides who has to agree. Almost every test
here is about keeping those apart, because the natural implementation mixes them: confidence
is a number already sitting there and blast radius has to be worked out, and what that
produces is a system that widens somebody's access quietly whenever it happens to be sure
enough.

Task ids: M16.3.1, M16.3.2, M16.3.3, M16.3.4, M16.3.5, M16.3.6
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.memory.signals import Signal
from brain.memory.tiers import (
    BLAST_RADIUS,
    CHANGES_WHAT_ANYBODY_MAY_SEE,
    LEARNABLE_SIGNALS,
    PROMOTION_AGREEMENT,
    PROMOTION_WINDOW,
    Change,
    Occurrence,
    Proposal,
    Tier,
    TierError,
    blast_radius,
    independent,
    may_promote,
    propose,
    tier_gaps,
    tier_of,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def occurrences(*conversations: str, at: datetime = NOW) -> list[Occurrence]:
    return [Occurrence(conversation_id=one, on=at) for one in conversations]


def test_everything_that_changes_who_may_see_what_is_gated() -> None:
    """**The property the module exists for.**

    Scope widening, company knowledge, a leash increase, a money-boundary merge and a
    capability addition are the five ways a learning can change what somebody may see. Every
    one waits for a person.

    Checked against a list written out separately rather than derived from the tier map,
    because derived the two would agree by construction: lowering one in `BLAST_RADIUS` would
    move both sides of the comparison together, which is the constant-compared-against-itself
    defect this repository keeps finding.

    Delete this and one of the five can be quietly moved to automatic, and the system starts
    widening access on its own."""
    assert len(CHANGES_WHAT_ANYBODY_MAY_SEE) == 5

    for change in CHANGES_WHAT_ANYBODY_MAY_SEE:
        assert BLAST_RADIUS[change] is Tier.GATED, f"{change.value} is not gated"


def test_a_tier_is_never_computed_from_a_confidence() -> None:
    """Confidence decides whether to act; blast radius decides who has to agree. Mixing them
    produces a system that widens access whenever it is sure enough, which is the one thing
    this module is arranged to prevent.

    Asserted on the signatures rather than on behaviour, because behaviour today says nothing
    about whether a confidence parameter can be added tomorrow, and the day it is added it
    will be added with a sensible-looking default.

    Delete this and `blast_radius(change, confidence=0.99)` appears, and it will read as an
    improvement."""
    for function in (blast_radius, propose, tier_of):
        parameters = set(inspect.signature(function).parameters)
        for forbidden in ("confidence", "score", "certainty", "probability", "similarity"):
            assert forbidden not in parameters, (
                f"{function.__name__} takes a {forbidden}, so a tier can be lowered by being "
                "sure about something"
            )


def test_a_change_nobody_classified_is_refused_rather_than_defaulted() -> None:
    """A default has to be chosen by somebody who has not seen the change, and the only
    defensible choice is the most cautious one, which is what refusing amounts to: an
    unclassified learning waits for a person.

    A stand-in member is used rather than deleting a real one, so this tests the lookup's
    behaviour on a miss rather than the map's contents.

    Delete this and a change kind added without a tier gets whatever `dict.get` returns, and
    the safest-looking fix is a default that is wrong exactly once."""

    class Unclassified(str):
        value = "something_nobody_thought_about"

    with pytest.raises(TierError, match="no blast radius is declared"):
        blast_radius(Unclassified())  # type: ignore[arg-type]


def test_a_proposal_cannot_claim_a_tier_lower_than_its_reach() -> None:
    """The tier is not the proposer's to choose. `propose` takes it from `blast_radius`, and
    the constructor refuses one that disagrees, so building the dataclass directly does not
    get round it either.

    Both halves are asserted, because a check only in `propose` is a check somebody bypasses
    by constructing the object, which is the obvious thing to do in a test fixture and then in
    production.

    Delete this and a caller can propose a capability addition at tier one, and everything
    downstream will believe it."""
    assert propose(Change.CAPABILITY_ADDITION, subject="s").tier is Tier.GATED

    with pytest.raises(TierError, match="not the proposer's to choose"):
        Proposal(change=Change.CAPABILITY_ADDITION, tier=Tier.AUTOMATIC, subject="s")


def test_a_proposal_has_nowhere_to_record_an_approval() -> None:
    """A gated change is not approved by adding a field to this model. It is approved by a
    surface that does not exist yet, and keeping it that way is what stops the approval
    becoming a default argument.

    Asserted on the fields rather than on the absence of a function, because the field is what
    a hurried afternoon adds and the function is what somebody would notice in review.

    Delete this and `approved_by: str = "system"` appears, and it will be true."""
    names = {f.name for f in dataclass_fields(Proposal)}

    assert names == {"change", "tier", "subject"}
    for forbidden in ("approved_by", "approved_at", "decision", "approver", "granted"):
        assert forbidden not in names, f"a proposal can carry {forbidden}"


def test_nothing_in_this_module_applies_anything() -> None:
    """No memory written, no rule installed, no grant changed, no leash moved. This decides
    what tier a change is and the machinery that would act on the answer does not exist.

    Checked by reading the module's own names, because the property is about the whole module
    rather than one function, and because the first thing anybody adds when wiring this up is
    an `apply` that "just does the tier one ones".

    Delete this and that function appears, and the tier check becomes advisory."""
    from brain.memory import tiers

    public = {name for name in dir(tiers) if not name.startswith("_")}

    for forbidden in ("apply", "commit", "install", "grant", "approve", "promote_now"):
        assert forbidden not in public, f"the module exposes {forbidden}"


def test_a_batch_carrying_one_gated_change_is_a_gated_batch() -> None:
    """The alternative, applying the automatic ones now and holding the gated one back, is how
    a change that only makes sense as a whole gets applied in half.

    An empty set is tier zero, because nothing is being changed and nobody needs to agree.

    Delete this and `tier_of` can take the first, the last or the most common tier, and each
    of those is wrong in a way that shows up only on mixed batches."""
    assert tier_of([Change.PREFERENCE, Change.LEASH_INCREASE]) is Tier.GATED
    assert tier_of([Change.PREFERENCE, Change.RETRIEVAL_BOOST]) is Tier.AUTOMATIC
    assert tier_of([]) is Tier.SESSION


def test_agreement_is_counted_over_conversations_and_never_over_repetitions() -> None:
    """**The arithmetic worth arguing about.**

    Somebody asking the same thing four times in one conversation is one piece of evidence
    about a shortcut and four pieces of evidence that an answer was unclear. Counting it as
    four is how a shortcut gets promoted on the strength of one bad afternoon.

    Delete this and the threshold is reachable inside a single conversation, which is the
    conversation most likely to be going badly."""
    one_conversation = occurrences("c1", "c1", "c1", "c1")
    three_conversations = occurrences("c1", "c2", "c3")

    assert independent(one_conversation, now=NOW) == 1
    assert independent(three_conversations, now=NOW) == 3


def test_a_shortcut_is_promoted_only_once_enough_separate_conversations_agree() -> None:
    """M16.3.4. Both sides of the threshold, because a threshold never reached is a tier that
    never promotes and one always reached is a tier that is really tier one.

    Delete this and the agreement count stops deciding anything."""
    proposal = propose(Change.FAST_PATH_RULE, subject="hours-left")

    assert may_promote(proposal, occurrences("c1", "c2"), now=NOW) is False
    assert may_promote(proposal, occurrences("c1", "c2", "c3"), now=NOW) is True
    assert PROMOTION_AGREEMENT == 3


def test_occurrences_outside_the_window_do_not_count_towards_a_promotion() -> None:
    """Three agreements spread across a year are not a pattern: an agency's processes change
    faster than that, and a shortcut promoted on evidence from three seasons is a shortcut
    about a way of working nobody uses any more.

    The future is excluded too. Clock skew that could manufacture agreement is skew that
    promotes a rule.

    Delete this and the window stops bounding anything in either direction."""
    stale = [
        Occurrence(conversation_id="c1", on=NOW - PROMOTION_WINDOW - timedelta(days=1)),
        Occurrence(conversation_id="c2", on=NOW - timedelta(days=1)),
        Occurrence(conversation_id="c3", on=NOW + timedelta(days=1)),
    ]

    assert independent(stale, now=NOW) == 1


def test_a_gated_change_can_never_be_promoted_by_agreement() -> None:
    """**Three people repeating a pattern is not a person deciding somebody may see more.**

    This is the single most important refusal in the module, because promotion is the
    mechanism that exists to turn repeated evidence into action, and pointing it at a tier
    three change is how the gate is bypassed without anybody removing it.

    Delete this and enough repetition widens access, which is the outcome the whole tier
    system exists to prevent."""
    gated = propose(Change.SCOPE_WIDENING, subject="finance")
    plenty = occurrences("c1", "c2", "c3", "c4", "c5", "c6")

    assert may_promote(gated, plenty, now=NOW) is False


def test_a_tier_one_change_is_not_promotable_either() -> None:
    """It does not need promoting, so asking whether it may be is a question about the wrong
    thing, and the honest answer is no rather than a vacuous yes.

    Delete this and `may_promote` returns True for anything with enough occurrences, which
    reads as working and means the tier is being ignored."""
    automatic = propose(Change.PREFERENCE, subject="brevity")

    assert may_promote(automatic, occurrences("c1", "c2", "c3", "c4"), now=NOW) is False


def test_every_change_has_exactly_one_tier_and_every_tier_rule_has_a_change() -> None:
    """M16.3.6. A member with no entry is a change whose tier is decided by whatever the lookup
    does on a miss; an entry with no member is a rule about something that cannot happen.

    Checked in both directions because they are different mistakes and only one of them is
    visible at the call site.

    Delete this and the vocabulary and the map drift apart silently."""
    assert tier_gaps() == ()
    assert set(BLAST_RADIUS) == set(Change)


def test_the_session_tier_tells_nobody_and_the_gated_tier_tells_a_person() -> None:
    """The ordering is what makes `max` the right operation over a batch, and it is what makes
    "at least tier two" a comparison rather than a set membership test.

    Asserted as an ordering rather than as four numbers, so renumbering is fine and reordering
    is not.

    Delete this and the tiers can be reordered, after which a gated change in a mixed batch
    stops dominating."""
    assert Tier.SESSION < Tier.AUTOMATIC < Tier.PROMOTED < Tier.GATED


def test_every_signal_is_evidence_a_tier_one_learning_may_be_formed_from() -> None:
    """M16.3.2 asks for "all negative signals" at tier one, read literally.

    A signal is already a statement that something went badly, and recording that is not a
    decision about anybody's access, so waiting for a person buys nothing and loses the
    evidence while it waits.

    Compared against the signal vocabulary rather than a list here, so a signal added there is
    covered rather than silently left out.

    Delete this and a new signal kind can arrive with no tier, and the safest-looking fix is
    to leave it out of learning entirely."""
    assert frozenset(Signal) == LEARNABLE_SIGNALS
    assert BLAST_RADIUS[Change.NEGATIVE_SIGNAL] is Tier.AUTOMATIC


def test_an_occurrence_that_cannot_be_told_apart_from_another_is_refused() -> None:
    """Agreement is counted over distinct conversations, so an occurrence with no conversation
    id collapses into every other one and inflates the count towards a promotion.

    Delete this and a batch of occurrences with a missing field promotes a shortcut on
    evidence that is one thing counted many times."""
    with pytest.raises(TierError, match="cannot be told apart"):
        Occurrence(conversation_id="", on=NOW)

    with pytest.raises(TierError, match="naive"):
        Occurrence(conversation_id="c1", on=datetime(2026, 9, 7, 12, 0))


def test_a_proposal_about_nothing_cannot_be_constructed() -> None:
    """A proposal with no subject is a thing to approve that nobody can look at, and a gated
    proposal that cannot be reviewed is one somebody approves to clear the queue.

    Delete this and the review surface fills with entries nobody can act on, which is
    indistinguishable from entries nobody has read."""
    with pytest.raises(TierError, match="cannot be reviewed"):
        Proposal(change=Change.PREFERENCE, tier=Tier.AUTOMATIC, subject="")


def test_tier_gaps_reports_an_access_changing_kind_that_has_stopped_being_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Written because a mutation survived.** `tier_gaps` has three checks and only two of
    them were exercised: every test called it on a healthy map and asserted an empty tuple,
    which passes whether the third check is there or not.

    The third is the one that matters. It is what compares the tier map against the separately
    written list of changes that alter who may see what, and it is the check that fires when
    somebody lowers one of the five. Its sibling
    `test_everything_that_changes_who_may_see_what_is_gated` asserts the same property
    directly, so the map is covered; what was not covered is that the diagnostic *reports* it,
    and a diagnostic nobody has seen report anything is a diagnostic nobody can rely on.

    The map is patched rather than edited, so the module is left as it was and the test says
    what a broken map looks like rather than requiring one.

    Delete this and `tier_gaps` can quietly stop checking the thing it exists to check, while
    still returning an empty tuple and looking healthy."""
    broken = dict(BLAST_RADIUS)
    broken[Change.SCOPE_WIDENING] = Tier.AUTOMATIC
    monkeypatch.setattr("brain.memory.tiers.BLAST_RADIUS", broken)

    gaps = tier_gaps()

    assert any("scope_widening" in one and "not gated" in one for one in gaps), gaps
    assert any("without anybody agreeing" in one for one in gaps), gaps
