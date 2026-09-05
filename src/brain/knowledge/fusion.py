"""Combining two rankings that do not share a scale, by rank rather than by score.

A hybrid retriever asks two questions of the same corpus and gets two orderings back. The
lexical side returns `ts_rank_cd`, an unbounded number whose size depends on how many times
the terms appear and on nothing else; the vector side returns a cosine distance in a fixed
interval. **The two numbers are not on one scale and never will be**, so any function that
adds, averages or weights them is comparing a length against a temperature.

**Normalising first is the obvious repair and it is worse than the disease.** Min-max
normalisation over each list makes the top result of every list exactly 1.0, whether it was
an excellent match or the least bad of a uniformly bad set, so a query with no lexical match
at all still contributes a full-strength vote. It is also not a function of the document: the
normalised score of a result changes when a *different* result is added to or removed from
the list, so the same document scores differently depending on what else came back. A
z-score has the same defect with more arithmetic. Both were rejected.

**A tuned weighted sum was rejected too, and for a duller reason.** It needs a labelled
evaluation set to tune against, that set does not exist here, and a weight chosen by
intuition is a score by another name with somebody's confidence attached. There is
deliberately no `weights` argument below: the day there is an evaluation set is the day one
can be added with something to justify it.

So the ranks are combined and the scores are discarded, which is reciprocal rank fusion.
**The function has nowhere to put a score**: a `Ranking` is a sequence of references in
order, and there is no field a number could be passed in. That is the guarantee, expressed
as a shape rather than as a rule somebody has to remember, in the same way
`brain.knowledge.chunking.Chunk` cannot be built outside `chunk_document`.

**`k` damps the top of each list, and that is the whole behaviour worth understanding.**
With `k = 0` the first place in one list scores 1.0 and second place scores 0.5, so a single
retriever's favourite beats anything the other one says. With `k = 60` first place is
1/61 and a document placed second by *both* retrievers scores 2/62, which is more. Two
retrievers agreeing beats one retriever being certain, and that is the property a hybrid
exists for.

**A document only one retriever ranked still surfaces.** It scores once instead of twice, so
it sits below the corroborated results and above nothing at all. The alternative, an
intersection, would throw away exactly the results hybrid retrieval is for: the passage
whose wording shares no term with the question, and the passage whose wording shares every
term but whose meaning is unrelated.

Nothing here reads a clock, opens a connection or knows what a chunk is. It is arithmetic
over two lists of strings.

Task ids: M15.2.5
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: The damping constant from the original description of the method. It is not tuned and it
#: is not claimed to be optimal here; it is the published default, and a number changed
#: without a measurement is a number changed for a feeling. What it buys is stated in the
#: module docstring: with `k` this large, agreement between retrievers outweighs a single
#: retriever's confidence, which is the reason to fuse at all.
RRF_K: Final = 60

#: Why the scores never enter the arithmetic. Named so a reviewer can point at the sentence
#: rather than at a comment somebody deleted while tidying.
RANKS_ARE_COMBINED_NEVER_SCORES: Final = (
    "a lexical rank and a vector distance are not on one scale, so they are combined by "
    "position and never by value; a normalised score is a function of the rest of the list "
    "rather than of the document"
)

#: The other half of the contract, and the one an intersection would break.
A_RESULT_ONE_RETRIEVER_FOUND_STILL_SURFACES: Final = (
    "a document ranked by one retriever alone scores once rather than twice, so it ranks "
    "below the corroborated results and above nothing; an intersection would discard the "
    "passage that shares no term with the question, which is what the vector leg is for"
)


class FusionError(Exception):
    """A fusion input that would silently produce the wrong ordering.

    Outside the `brain.core.errors` taxonomy, like every other refusal in this package:
    those five outcomes describe an answer given to a person, and this describes a refusal
    to compute one.
    """


@dataclass(frozen=True)
class Ranking:
    """One retriever's answer: references, best first, and nothing else.

    There is no score field and no room for one. The type is the enforcement: a caller
    holding `ts_rank_cd` values cannot pass them in, so nothing downstream can start
    reading them "just for the tie-break", which is how a rank-based method quietly becomes
    a score-based one.

    `retriever` is carried so a fused result can say which legs found it. That is a
    diagnostic about our own retrieval and not a fact about the corpus, so it discloses
    nothing: it names which of our two queries matched, for a document the caller was
    already entitled to see.
    """

    retriever: str
    order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.retriever.strip():
            msg = "a ranking has to name the retriever it came from"
            raise FusionError(msg)
        seen = set(self.order)
        if len(seen) != len(self.order):
            # One list voting for the same reference twice gives that reference two
            # reciprocal contributions from one opinion, which is exactly the corroboration
            # the method is trying to measure, faked. It arrives from a query that joined
            # and forgot to be distinct, so it looks like nothing at all in a diff.
            duplicated = sorted({ref for ref in self.order if self.order.count(ref) > 1})
            msg = (
                f"{self.retriever!r} ranks {duplicated} more than once; one retriever "
                "voting twice for one reference counts as corroboration that never happened"
            )
            raise FusionError(msg)

    @classmethod
    def of(cls, retriever: str, refs: Sequence[str]) -> Ranking:
        """Build from any sequence. The tuple conversion is the only work done here."""
        return cls(retriever=retriever, order=tuple(refs))

    def rank_of(self, ref: str) -> int | None:
        """This reference's one-based position, or None when this retriever missed it.

        One-based rather than zero-based because the reciprocal is taken of it. At zero the
        first result would be worth `1 / k` and the second `1 / (k + 1)`, which are almost
        equal, so the ordering inside each list would stop mattering.
        """
        try:
            return self.order.index(ref) + 1
        except ValueError:
            return None


@dataclass(frozen=True)
class Fused:
    """One reference, its combined score, and where it came from.

    `ranks` is a sorted tuple of pairs rather than a mapping, so the whole value is frozen
    and two identical fusions compare equal. It holds only the retrievers that found this
    reference; a retriever that missed it is absent rather than present with a null, because
    "not found by the vector leg" and "found at rank None" are the same fact written twice.
    """

    ref: str
    score: float
    ranks: tuple[tuple[str, int], ...]

    @property
    def contributors(self) -> tuple[str, ...]:
        """The retrievers that ranked this, in name order."""
        return tuple(name for name, _rank in self.ranks)

    @property
    def corroborated(self) -> bool:
        """True when more than one retriever found it."""
        return len(self.ranks) > 1


def reciprocal_rank(position: int, k: int = RRF_K) -> float:
    """One retriever's contribution for a reference at `position`.

    Split out rather than inlined because it is the whole method, and a method written on
    one line inside a loop is a method nobody reviews. It is also the only place `k` is
    read, so the damping argument in the module docstring has exactly one implementation.
    """
    if position < 1:
        msg = f"position {position} is not a one-based rank"
        raise FusionError(msg)
    return 1.0 / (k + position)


def fuse(rankings: Sequence[Ranking], *, k: int = RRF_K) -> tuple[Fused, ...]:
    """Combine rankings by reciprocal rank (M15.2.5).

    Every reference any retriever returned appears in the result, scored by the sum of its
    reciprocal ranks. Sorted by score, and then by reference so that two runs over the same
    input produce the same order: without the second key the order of equally scored results
    would follow whichever dictionary iteration produced them, and a console list would
    reshuffle between two identical requests.

    An empty sequence fuses to nothing, and that is not the dangerous empty case the rest of
    this codebase argues about. `brain.core.department.compose` refuses an empty list because
    the identity element of conjunction is the unrestricted scope, so an empty input would
    widen; the identity element here is the empty result, so an empty input narrows to
    nothing, which is the safe direction and the true answer.

    `k` is checked rather than trusted. A negative `k` can make a denominator zero or
    negative, which turns one result's score into an infinity or a sign flip and puts the
    worst-ranked document first. That is not an ordering that looks wrong: it looks like a
    retrieval quality problem, and it would be investigated as one for a week.
    """
    if k < 1:
        msg = (
            f"k must be at least 1, not {k}; below that the damping disappears and one "
            "retriever's first place outranks anything the other one agrees on, and at or "
            "below the negative of a rank the denominator reaches zero"
        )
        raise FusionError(msg)

    names = [ranking.retriever for ranking in rankings]
    if len(set(names)) != len(names):
        # Two lists under one name are one retriever voting twice, which `Ranking` already
        # refuses inside a single list. Refusing it across lists too closes the same hole
        # from the other side, and the hole arrives the same way: a caller that built the
        # lexical ranking twice by copy and paste.
        msg = f"two rankings share a retriever name in {sorted(names)}; each leg names itself once"
        raise FusionError(msg)

    scores: dict[str, float] = {}
    positions: dict[str, list[tuple[str, int]]] = {}
    for ranking in rankings:
        for position, ref in enumerate(ranking.order, start=1):
            scores[ref] = scores.get(ref, 0.0) + reciprocal_rank(position, k)
            positions.setdefault(ref, []).append((ranking.retriever, position))

    fused = [
        Fused(ref=ref, score=scores[ref], ranks=tuple(sorted(positions[ref]))) for ref in scores
    ]
    return tuple(sorted(fused, key=lambda item: (-item.score, item.ref)))
