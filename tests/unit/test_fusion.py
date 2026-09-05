"""Reciprocal rank fusion: what it must never read, and what it must never discard.

Two properties carry this file and they fail in opposite directions.

**Reading a score fails loudly the first time somebody tries.** The type has no field for
one, so the tests about it are tests about a shape rather than about behaviour, and they are
here because the pressure to add one is real: the tie-break looks arbitrary without it, and
a score is right there in the query result.

**Discarding a result fails silently and forever.** A fusion that only kept references both
retrievers returned would still work, would still be tested by every ordering assertion, and
would quietly throw away the two cases hybrid retrieval exists for: the passage that shares
no term with the question, and the passage that shares every term and means something else.
Nobody files a bug against results that merely never appear.

Task ids: M15.2.5
"""

from __future__ import annotations

import dataclasses

import pytest

from brain.knowledge.fusion import (
    RRF_K,
    Fused,
    FusionError,
    Ranking,
    fuse,
    reciprocal_rank,
)


def order_of(fused: tuple[Fused, ...]) -> tuple[str, ...]:
    return tuple(item.ref for item in fused)


def ranked_by(scores: dict[str, float]) -> tuple[str, ...]:
    """The order a retriever would return for these scores, best first.

    Used by the scale test below to build two rankings out of two score distributions that
    are nothing alike and produce the same order. Sorting here rather than in the module
    under test is the point: this is what a caller does, and it is the last place a score
    exists.
    """
    return tuple(sorted(scores, key=lambda ref: (-scores[ref], ref)))


# ------------------------------------------------------- what is never read
def test_a_ranking_has_nowhere_to_put_a_score() -> None:
    """Stated against the shape rather than against behaviour, in the same form
    `test_the_reading_has_nowhere_to_express_a_refusal` uses in `test_projection.py`.

    A `score` field would be added helpfully, during a week when the ordering looked wrong,
    and the next change would read it "only for the tie-break". At that point the lexical
    leg's unbounded `ts_rank_cd` is being compared against a cosine distance in [0, 2], and
    the comparison is meaningless in a way no test would report.

    Delete this and the field arrives as a convenience."""
    names = {f.name for f in dataclasses.fields(Ranking)}
    assert names == {"retriever", "order"}
    forbidden = {"score", "scores", "weight", "weights", "relevance", "distance", "confidence"}
    surface = names | {n for n in dir(Ranking) if not n.startswith("_")}
    assert not (surface & forbidden), f"Ranking has somewhere to put a score: {surface}"


def test_the_fusion_is_the_same_whatever_scale_the_scores_were_on() -> None:
    """**The property the whole method exists for.** Two retrievers produce numbers that are
    not comparable, and any function that adds or averages them is comparing a length against
    a temperature. Here the same three documents are ranked by two score distributions four
    orders of magnitude apart, and the fusion is identical, because the numbers never reach
    it.

    A normalised weighted sum would fail this: min-max normalisation makes each list's top
    result exactly 1.0 whether it was an excellent match or the least bad of a bad set, so
    the two distributions below would contribute differently.

    Delete this and a "small improvement" that reads the score back in passes every other
    test in this file."""
    tight = ranked_by({"a": 0.5001, "b": 0.5000, "c": 0.4999})
    wide = ranked_by({"a": 9000.0, "b": 12.0, "c": 0.0001})
    assert tight == wide == ("a", "b", "c")
    lexical = Ranking.of("lexical", tight)
    vector = Ranking.of("vector", ("c", "a", "b"))
    again = Ranking.of("vector", ("c", "a", "b"))
    assert fuse((lexical, vector)) == fuse((Ranking.of("lexical", wide), again))


# ------------------------------------------------------- what is never discarded
def test_a_document_ranked_by_one_retriever_alone_still_surfaces() -> None:
    """**The leaf's own sentence.** The vector leg finds the passage that answers the
    question in words the question did not use; the lexical leg finds the passage nobody
    would have embedded close by. An intersection discards both, and a hybrid retriever that
    discards both is two retrievers doing the work of the weaker one.

    Delete this and `fuse` can be rewritten as a set intersection, which is shorter, passes
    every ordering assertion here, and silently halves recall."""
    lexical = Ranking.of("lexical", ("shared", "lexical_only"))
    vector = Ranking.of("vector", ("shared", "vector_only"))
    fused = fuse((lexical, vector))
    assert set(order_of(fused)) == {"shared", "lexical_only", "vector_only"}
    assert order_of(fused)[0] == "shared"
    solitary = {item.ref: item for item in fused if not item.corroborated}
    assert set(solitary) == {"lexical_only", "vector_only"}


def test_every_reference_either_retriever_returned_appears_exactly_once() -> None:
    """The positive case for the one above, and the guard against the opposite mistake. A
    fusion that concatenated rather than merged would put a corroborated document in the
    result twice, once per leg, and the page shown to a person would repeat a passage while
    dropping the one it pushed off the end."""
    fused = fuse((Ranking.of("l", ("a", "b", "c")), Ranking.of("v", ("c", "d"))))
    assert sorted(order_of(fused)) == ["a", "b", "c", "d"]
    assert len(order_of(fused)) == len(set(order_of(fused)))


def test_a_leg_that_returned_nothing_changes_no_ordering() -> None:
    """The lexical leg returns nothing when the question shares no term with the corpus, and
    the vector leg returns nothing before anything has been embedded. Both are ordinary. A
    fusion that treated an empty list as a vote against everything, or that refused, would
    turn the ordinary case into an outage.

    Delete this and an empty leg becomes a special case somebody handles wrongly."""
    alone = fuse((Ranking.of("lexical", ("a", "b")),))
    with_empty = fuse((Ranking.of("lexical", ("a", "b")), Ranking.of("vector", ())))
    assert order_of(alone) == order_of(with_empty) == ("a", "b")


def test_fusing_no_rankings_at_all_is_empty_rather_than_a_refusal() -> None:
    """`brain.core.department.compose` refuses an empty sequence because the identity element
    of conjunction is the unrestricted scope, so an empty input would widen. The identity
    element here is the empty result, so an empty input narrows to nothing, which is both
    safe and true. The distinction is worth pinning, because the two functions look alike."""
    assert fuse(()) == ()


# ------------------------------------------------------- what the damping buys
def test_two_retrievers_agreeing_outrank_one_retriever_being_certain() -> None:
    """**Why `k` is 60 rather than 0.** At the default, a document placed second by both legs
    scores 2/62 and a document placed first by one leg alone scores 1/61, so agreement wins.
    That is the entire reason to run two retrievers instead of trusting the better one.

    Delete this and `RRF_K` can be dropped to a small number during a relevance
    investigation, which turns the hybrid into whichever leg happened to be confident."""
    fused = fuse(
        (
            Ranking.of("lexical", ("confident", "agreed")),
            Ranking.of("vector", ("other", "agreed")),
        )
    )
    assert order_of(fused)[0] == "agreed"


def test_a_smaller_k_makes_a_single_confident_leg_win_and_that_is_the_trade_off() -> None:
    """The same arithmetic from the other side, so the constant is understood rather than
    copied. At `k = 1` a first place is worth 1/2 and two fourth places are worth 2/5, so the
    confident leg wins; at 60 the two fourth places win. `k` is how deep corroboration keeps
    beating confidence, and this is the test that says so in numbers.

    Delete this and `RRF_K` is a magic number with a comment."""
    lexical = Ranking.of("lexical", ("confident", "x", "y", "z", "agreed"))
    vector = Ranking.of("vector", ("p", "q", "r", "agreed"))
    assert order_of(fuse((lexical, vector), k=1))[0] == "confident"
    assert order_of(fuse((lexical, vector), k=RRF_K))[0] == "agreed"


def test_a_k_below_one_is_refused_rather_than_producing_an_infinity() -> None:
    """At `k = -1` the first result's denominator is zero. Python raises there, but at
    `k = -2` the second result's score is negative and the third's is positive, so the
    ordering inverts for part of the list and nothing raises at all. That does not look like
    a bug: it looks like a relevance problem, and it would be investigated as one.

    Delete this and `k` becomes a tuning knob with a hole in the middle of it."""
    for bad in (0, -1, -60):
        with pytest.raises(FusionError, match="at least 1"):
            fuse((Ranking.of("lexical", ("a",)),), k=bad)


def test_a_position_below_one_is_refused() -> None:
    """Ranks are one-based because the reciprocal is taken of them. A zero-based first place
    would score `1 / k` and second place `1 / (k + 1)`, which at 60 differ by under two per
    cent, so the order inside each list would stop mattering."""
    assert reciprocal_rank(1) == pytest.approx(1 / (RRF_K + 1))
    with pytest.raises(FusionError, match="one-based"):
        reciprocal_rank(0)


# ------------------------------------------------------- stability and refusals
def test_the_fused_order_does_not_change_between_two_identical_runs() -> None:
    """Equally scored references are ordered by reference, not by whichever dictionary
    iteration produced them. Without the second key a console list reshuffles between two
    identical requests, and the person reading it concludes the results are random.

    It also matters upstream: a page taken from an unstable ranking contains different
    documents each time, so "the answer changed and nothing changed" becomes a real report
    nobody can reproduce."""
    lexical = Ranking.of("lexical", ("b", "a"))
    vector = Ranking.of("vector", ("a", "b"))
    fused = fuse((lexical, vector))
    assert [item.score for item in fused] == pytest.approx([item.score for item in fused][::-1]), (
        "the two references must be tied for this test to be about the tie-break"
    )
    assert order_of(fused) == ("a", "b")
    assert order_of(fuse((vector, lexical))) == ("a", "b")


def test_a_retriever_that_ranks_one_reference_twice_is_refused() -> None:
    """One list voting twice for one reference gives it two reciprocal contributions from a
    single opinion, which is exactly the corroboration the method measures, faked. It arrives
    from a query that joined and forgot to be distinct, so it is invisible in a diff and the
    only symptom is that one document keeps winning.

    Delete this and a duplicated candidate list is a relevance investigation."""
    with pytest.raises(FusionError, match="corroboration that never happened"):
        Ranking.of("lexical", ("a", "b", "a"))


def test_two_rankings_cannot_share_a_retriever_name() -> None:
    """The same hole from the other side: `Ranking` refuses a duplicate inside one list, and
    this refuses one built twice by copy and paste. Both are one retriever voting twice, and
    the second form would also make `contributors` claim corroboration between a leg and
    itself."""
    with pytest.raises(FusionError, match="share a retriever name"):
        fuse((Ranking.of("lexical", ("a",)), Ranking.of("lexical", ("b",))))


def test_a_ranking_names_the_retriever_it_came_from() -> None:
    """A fused result says which legs found it, which is how a retrieval problem is diagnosed
    at all: "the vector leg never returns anything" is invisible in a merged list. An unnamed
    ranking makes that diagnosis impossible and makes the duplicate-name refusal above
    unenforceable."""
    with pytest.raises(FusionError, match="name the retriever"):
        Ranking.of("  ", ("a",))
    fused = fuse((Ranking.of("lexical", ("a",)), Ranking.of("vector", ("a",))))
    assert fused[0].contributors == ("lexical", "vector")
    assert fused[0].corroborated


def test_a_reference_ranked_by_one_leg_reports_only_that_leg() -> None:
    """The positive case for `contributors`. A result that listed every retriever, with a
    null rank for the ones that missed it, would say "found at rank None", which is the same
    fact written twice and reads as a rank of zero to anything that sorts on it."""
    fused = {item.ref: item for item in fuse((Ranking.of("l", ("a",)), Ranking.of("v", ("b",))))}
    assert fused["a"].ranks == (("l", 1),)
    assert fused["b"].ranks == (("v", 1),)
    assert not fused["a"].corroborated
