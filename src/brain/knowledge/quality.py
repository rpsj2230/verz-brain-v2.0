"""Measuring retrieval, and logging it without logging what somebody could not see.

Two halves, and neither of them retrieves anything. The first decides whether a reranking
stage is worth its latency and records the answer. The second is the log the learning signal
is read from, written so that the signal is derivable without any record of what a caller was
not shown.

---

**Reranking is an evaluation here, not a feature, and the burden of proof is on the
reranker.** The leaf asks for it to be adopted only if it earns its latency, so what is built
below is the thing that decides: a fixed judged set, a stated budget, a metric, and a recorded
verdict that a later reading can disagree with. **No reranker is implemented.** Writing one
and leaving it unwired is how an unadopted thing gets adopted by a later hand who finds it
sitting there looking finished.

The comparison is over orderings rather than over a model, which is what makes the harness
outlive whichever reranker somebody eventually tries: a cross-encoder, a listwise model behind
an API and a hand-written boost table all produce the same input to `evaluate`.

*The metric is nDCG at ten with binary gains.* Ten because that is what an answer reads, and
because reranking's whole claim is about the top of a list rather than about its tail. Binary
gains because a judged set built here would be built by hand, and graded relevance asks the
person building it a question they cannot answer consistently twice. **Recall is not the
measure**, and this is the mistake worth naming: a reranker that reorders a fixed candidate
set cannot change what is in it, so recall at the candidate depth is identical by
construction, and an evaluation reporting it would show a reassuring nought-point-nought
difference for a stage that made everything worse.

*The verdict needs two things and both are necessary.* The gain must clear
`MINIMUM_NDCG_GAIN`, and the added latency must stay inside `RERANK_BUDGET_MS` at the
ninety-fifth percentile. The percentile is nearest-rank, for the reason
`brain.connectors.throttle` gives about its own: an interpolated percentile is a number that
never happened.

*A verdict is refused rather than guessed when the set is too small to carry one.* With `n`
judged cases a single case moves the mean by at most `1/n`, so a set smaller than
`1 / MINIMUM_NDCG_GAIN` cases lets one question decide adoption on its own. That is where
`MINIMUM_JUDGED_CASES` comes from: it is derived from the threshold rather than chosen, and a
test holds the two together.

**The recorded verdict is that reranking is not adopted, and the reason is the absence of
evidence rather than evidence against.** There is no judged set in this repository.
`brain.knowledge.fusion` already records the same absence as its reason for having no weights,
and inventing one here to justify a latency spend would be tuning against a set written by the
person who wanted the answer. Two further costs are stated so a later reader is weighing all
of them: a reranking model reads every candidate passage, which puts the caller's question and
their reachable passages into a second provider context that `brain.ops.tracing` and
`brain.ops.pii` exist because of; and a local cross-encoder is a Python machine-learning
dependency in the request path, which M14's calibration leaf already refuses for entity
resolution. `RERANKING` names what would change the verdict, so it is falsifiable rather than
merely negative.

---

**A retrieval log that records how many results were withheld is a count of hidden things,
and there is no field here one could go in.** `brain.ops.tracing` settles the shape of this:
the allowlist is names and never values, and a value is kept only if it still looks like
system vocabulary. This module is not a trace, and its record is not a span, but it holds
itself to the same grammar, and a test asserts that every value a `RetrievalEvent` emits would
pass `VALUE_TOKEN_RE` and every key `ATTRIBUTE_KEY_RE`. That is the checkable half of "nothing
in here is a payload".

**A count of what was shown is not a count of what was hidden, and the difference is the whole
design.** `returned` is on the record, deliberately. A short result list has four
indistinguishable causes: little exists, the lexical leg shared no term with the question,
iterative scan reached `hnsw.max_scan_tuples`, or most of the corpus is out of reach. That
indistinguishability is what `brain.knowledge.search` is built to preserve, so a returned count
attributes nothing. What must never appear is any number that *does* attribute: a candidate
count taken before the reach predicate, a filtered or withheld or denied count, the caller's
departments, or a flag saying the query was narrowed. None of those has a field here.

**The log is about our ranking and never about the corpus.** No reference, no question text,
no principal. The signal it carries is positional: the useful result was at place four rather
than at place one. That is a measurement of fusion rather than of the knowledge base, it
aggregates across callers without any of them contributing an object identity, and it is
exactly the evidence the reranking verdict above says it lacks. `principal_id` is absent for
the reason `brain.ops.tracing` leaves it off its allowlist: a log is not somewhere a person's
movements should be reconstructable by an operator who cannot read the underlying data.

Rejected: recording `(principal, chunk_id, used)` so that a per-document boost could be
learned. It is the obvious design and it is a second memory store with none of M16's controls:
no capability set recorded at formation, no re-check at read, no decay. Worse, a boost formed
from one caller's reach is applied to the next caller's ranking, which is one person's view of
what exists leaking into another's ordering. Per-reference learning belongs in the memory
layer, where those controls are the point.

Rejected: reporting a signal over however many events there happen to be. A rate computed over
one retrieval is that retrieval, and a learning rule promoted from it is a rule promoted from
one person's afternoon. `signal` returns None below `MINIMUM_EVENTS_FOR_A_SIGNAL`, which is
the same shape `reach_for` uses for "there is nothing here", rather than a number with a
disclaimer beside it that nothing reads.

Nothing here reads a clock, opens a connection or retrieves anything. It is arithmetic over
records somebody else kept.

Task ids: M15.3.1, M15.3.4
"""

from __future__ import annotations

import enum
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final


class QualityError(Exception):
    """An evaluation or a log record that would be measured wrongly rather than answered
    wrongly.

    Outside the `brain.core.errors` taxonomy, like every other refusal in this package: those
    five outcomes describe an answer given to a person, and this describes a refusal to
    compute a number.
    """


# ------------------------------------------------------------- named reasons


#: What a reranking stage is allowed to do to a candidate list.
A_RERANKER_MAY_ONLY_REORDER: Final = (
    "a reranked ordering may be a permutation of the candidate set or a prefix of one, and it "
    "may never carry a reference the candidate set did not; the candidates came out of a "
    "query carrying the caller's reach predicate, so a stage able to introduce a reference is "
    "a stage able to introduce one from outside their reach, and truncating is safe for the "
    "same reason introducing is not"
)

#: Why the default in the absence of evidence is not adoption.
AN_UNMEASURED_RERANKER_IS_NOT_AN_ADOPTED_ONE: Final = (
    "the leaf says adopted only if it earns its latency, so the burden of proof sits with the "
    "reranker and the absence of a judged set is a verdict rather than a gap in one; a "
    "harness that fell through to adoption when it could not measure would adopt on exactly "
    "the occasions nobody looked"
)

#: The sentence that keeps a returned count on the record and a withheld count off it.
A_COUNT_OF_WHAT_WAS_SHOWN_IS_NOT_A_COUNT_OF_WHAT_WAS_HIDDEN: Final = (
    "how many results a caller received is a fact they already hold and it attributes "
    "nothing, because a short list means the corpus is small, or the lexical leg shared no "
    "term, or the scan reached its bound, or most of it is out of reach, and those four are "
    "indistinguishable by design; a number that does attribute is the one that must not "
    "exist, so there is no field here for a pre-filter candidate count, a withheld count, a "
    "denied count or a flag saying the query was narrowed"
)

#: Why the log carries positions rather than references.
THE_LOG_MEASURES_OUR_RANKING_AND_NEVER_THE_CORPUS: Final = (
    "the record carries which of our own retrievers contributed and where in the caller's own "
    "list the useful result sat, and it carries no reference, no question and no principal; "
    "that is a measurement of the ranking function, it aggregates across callers without any "
    "of them contributing an object identity, and per-reference learning belongs in the "
    "memory layer where a capability set is recorded at formation and re-checked at read"
)


# --------------------------------------------------- the budget and the thresholds


#: What a reranking stage may add to the answer path, at the ninety-fifth percentile.
#:
#: The answer lane targets under four seconds at p50 with a model call inside it, a live
#: federated fetch is already conceded 800ms there, and the fast lane's entire p95 target is
#: 500ms. A rerank stage is extra work over results already in hand, so it competes directly
#: with the model's time to first token rather than with anything a person is waiting on
#: separately. A hundred and fifty milliseconds is the largest addition that leaves the shape
#: of the answer path unchanged: three of them would equal one federated fetch, which is the
#: smallest latency this architecture already treats as significant.
#:
#: p95 rather than mean, because a rerank stage's cost is a tail problem. A batch that spills,
#: a cold model, a provider retry: the mean hides all three and the ninety-fifth percentile is
#: where a person notices them.
RERANK_BUDGET_MS: Final = 150

#: How much better the ordering has to be before the latency is earned.
#:
#: Not zero, and not "no worse". A stage that costs 150ms and buys 0.01 nDCG has been adopted
#: because the number moved, which is how a request path acquires four such stages in a year.
MINIMUM_NDCG_GAIN: Final = 0.05

#: How deep the metric looks. Ten is what an answer reads, and reranking's claim is about the
#: top of a list rather than its tail: a stage that improves places 40 to 50 has improved
#: nothing anybody sees.
EVALUATION_DEPTH: Final = 10

#: The smallest judged set that can carry a verdict, derived rather than chosen.
#:
#: With `n` cases a single case moves the mean nDCG by at most `1/n`. Below `1 / gain` cases
#: one question can therefore decide adoption on its own, and the verdict becomes a property
#: of which questions somebody happened to write down. A test holds this to the threshold, so
#: lowering the gain raises the required set size rather than quietly permitting a smaller one.
MINIMUM_JUDGED_CASES: Final = math.ceil(1 / MINIMUM_NDCG_GAIN)

#: Below this many events, a rate is that afternoon rather than a signal. Ten, matching
#: `brain.connectors.throttle.QUIET_WINDOW_REQUESTS`, because the estate runs at roughly a
#: tenth of a request a second and a stricter floor would mean no signal ever arrives.
MINIMUM_EVENTS_FOR_A_SIGNAL: Final = 10


# --------------------------------------------------------- the judged set (M15.3.1)


@dataclass(frozen=True)
class JudgedCase:
    """One question, and the references a person judged relevant to it.

    Binary rather than graded. A graded set asks whoever builds it to distinguish a three from
    a four twice in a row, which nobody does, and the inconsistency lands inside the metric
    where it looks like measurement.

    A case with nothing relevant is refused. Its ideal gain is nought, so it has no nDCG at
    all, and the two available treatments are both wrong: skipping it makes the evaluation set
    smaller than it says it is, and scoring it as perfect rewards a reranker for a question
    that has no answer.
    """

    case_id: str
    relevant: frozenset[str]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            msg = "a judged case needs an id; an evaluation addresses its cases by one"
            raise QualityError(msg)
        if not self.relevant:
            msg = (
                f"{self.case_id!r} judges nothing relevant, so it has no ideal ordering to be "
                "measured against; a case with no answer is not a case that was judged"
            )
            raise QualityError(msg)


@dataclass(frozen=True)
class Trial:
    """One case run twice: the ordering retrieval produced, and the reranked one.

    `added_latency_ms` is the reranking stage's own cost rather than the whole retrieval's,
    because the budget is about what adoption *adds*. Measuring the total would let a fast
    baseline pay for a slow reranker.
    """

    case_id: str
    baseline: tuple[str, ...]
    candidate: tuple[str, ...]
    added_latency_ms: float

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            msg = "a trial names the case it ran; an unattributed trial matches no judgement"
            raise QualityError(msg)
        for name in ("baseline", "candidate"):
            order: tuple[str, ...] = getattr(self, name)
            repeated = sorted({ref for ref in order if order.count(ref) > 1})
            if repeated:
                msg = (
                    f"{self.case_id!r} ranks {repeated} more than once in its {name}; one "
                    "reference in two places scores twice and the metric reads as a gain"
                )
                raise QualityError(msg)
        introduced = sorted(set(self.candidate) - set(self.baseline))
        if introduced:
            msg = (
                f"{self.case_id!r} reranked into {introduced}, which the candidate set did "
                f"not carry; {A_RERANKER_MAY_ONLY_REORDER}"
            )
            raise QualityError(msg)
        if self.added_latency_ms < 0:
            msg = (
                f"{self.case_id!r} reports {self.added_latency_ms}ms of added latency; a "
                "negative addition would offset a real one in the percentile"
            )
            raise QualityError(msg)


def discounted_gain(order: Sequence[str], relevant: frozenset[str], depth: int) -> float:
    """The discounted cumulative gain of one ordering, with binary gains.

    Split out rather than inlined because it is the metric, and a metric written on one line
    inside a loop is a metric nobody reviews. It is also the only place the discount is
    written, so the log base has one implementation.
    """
    if depth < 1:
        msg = f"a depth of {depth} measures nothing"
        raise QualityError(msg)
    return sum(
        1.0 / math.log2(place + 1)
        for place, ref in enumerate(order[:depth], start=1)
        if ref in relevant
    )


def ndcg(order: Sequence[str], case: JudgedCase, depth: int = EVALUATION_DEPTH) -> float:
    """How good one ordering is for one case, on nought to one.

    The ideal is the gain of putting every relevant reference first, capped at `depth`: a case
    with twenty relevant references cannot score against an ideal that assumes all twenty fit
    in ten places, or every ordering scores badly and the difference between two of them
    shrinks below the adoption threshold for a reason that has nothing to do with either.

    `JudgedCase` refuses an empty judgement, so the denominator is never nought here.
    """
    ideal = sum(
        1.0 / math.log2(place + 1) for place in range(1, min(len(case.relevant), depth) + 1)
    )
    return discounted_gain(order, case.relevant, depth) / ideal


def percentile_ms(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank, never interpolated.

    Restated rather than imported from `brain.connectors.throttle`, which has the same
    function and the same argument for it, because a knowledge module reaching into the
    connector package for four lines of arithmetic is a dependency in the wrong direction.
    `brain.knowledge.search._one_of` is restated from `brain.tables.identity` for the same
    reason.

    Interpolating between two samples returns a latency that nobody experienced, and a budget
    is a claim about what happened rather than about what the numbers average to.
    """
    if not 0.0 < fraction <= 1.0:
        msg = f"a percentile fraction is in (0, 1], not {fraction}"
        raise QualityError(msg)
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


class Verdict(enum.StrEnum):
    """What an evaluation concluded, and only one of the three means adopt.

    Three rather than two, because "measured and not worth it" and "not measurable" are
    different facts about the world and lead to different next steps. They behave identically
    at the one place it matters, which is `adopted`.
    """

    ADOPTED = "adopted"
    NOT_ADOPTED = "not_adopted"
    NOT_MEASURABLE = "not_measurable"

    @property
    def adopted(self) -> bool:
        """The one reading that changes what runs in production.

        A property rather than a comparison written at each call site, so that adding a fourth
        verdict is one edit here instead of a search for every `is Verdict.ADOPTED`.
        """
        return self is Verdict.ADOPTED


@dataclass(frozen=True)
class Evaluation:
    """What one comparison over a fixed set concluded, with the numbers it concluded it from.

    `because` is required and non-empty, for the reason `brain.ops.tracing.Retention.because`
    is: a verdict nobody can explain is one that gets reversed the first time somebody wants
    the other answer, and the reversal is permanent because nobody knows what the original was
    protecting.
    """

    cases: int
    baseline_ndcg: float
    candidate_ndcg: float
    added_latency_p95_ms: float
    verdict: Verdict
    because: str

    def __post_init__(self) -> None:
        if not self.because.strip():
            msg = "an evaluation states its reason; a bare verdict is a number with no argument"
            raise QualityError(msg)

    @property
    def gain(self) -> float:
        return self.candidate_ndcg - self.baseline_ndcg


def evaluate(
    cases: Sequence[JudgedCase],
    trials: Sequence[Trial],
    *,
    budget_ms: float = RERANK_BUDGET_MS,
    minimum_gain: float = MINIMUM_NDCG_GAIN,
    depth: int = EVALUATION_DEPTH,
) -> Evaluation:
    """Decide whether a reranking stage earns its latency, over a fixed set (M15.3.1).

    Fixed means what it says: every judged case is tried exactly once and every trial answers
    a judged case. An evaluation where the two sets merely overlap is an evaluation over a set
    nobody stated, and the cases that were quietly left out are the ones somebody looked at
    first.

    Both conditions must hold. A gain below `minimum_gain` is not adoption however fast the
    stage is, and a stage inside the gain is not adoption if its p95 addition is outside the
    budget. Where both fail, both are said, because a report naming only the first failure
    invites a second attempt that fixes it and runs into the other one.

    Too small a set produces `NOT_MEASURABLE` rather than a number with a caveat. See
    `AN_UNMEASURED_RERANKER_IS_NOT_AN_ADOPTED_ONE`: the default direction in the absence of
    evidence is never adoption.
    """
    judged = {case.case_id: case for case in cases}
    if len(judged) != len(cases):
        named = [case.case_id for case in cases]
        repeated = sorted({name for name in named if named.count(name) > 1})
        msg = f"the judged set carries {repeated} twice; a case judged twice is weighted twice"
        raise QualityError(msg)
    tried = {trial.case_id: trial for trial in trials}
    if len(tried) != len(trials):
        msg = "a case was tried more than once; the repeated trial would be weighted twice"
        raise QualityError(msg)
    unmatched = sorted(set(judged) ^ set(tried))
    if unmatched:
        msg = (
            f"{unmatched} is judged without a trial or tried without a judgement; an "
            "evaluation whose set and whose runs disagree is an evaluation over a set nobody "
            "stated, and the cases left out are the ones somebody looked at first"
        )
        raise QualityError(msg)

    if len(judged) < MINIMUM_JUDGED_CASES:
        return Evaluation(
            cases=len(judged),
            # The metrics are reported as nought rather than computed, deliberately. A mean
            # over three cases is a number somebody screenshots, and the screenshot outlives
            # the caveat printed next to it. The latency is still measured, because it is a
            # property of the stage rather than of the set and needs no judgement to be true.
            baseline_ndcg=0.0,
            candidate_ndcg=0.0,
            added_latency_p95_ms=percentile_ms([t.added_latency_ms for t in trials], 0.95),
            verdict=Verdict.NOT_MEASURABLE,
            because=(
                f"{len(judged)} judged cases is below {MINIMUM_JUDGED_CASES}, at which one "
                f"case can move the mean by more than the {minimum_gain} adoption threshold "
                f"on its own; {AN_UNMEASURED_RERANKER_IS_NOT_AN_ADOPTED_ONE}"
            ),
        )

    baseline = sum(ndcg(tried[cid].baseline, judged[cid], depth) for cid in judged) / len(judged)
    candidate = sum(ndcg(tried[cid].candidate, judged[cid], depth) for cid in judged) / len(judged)
    added = percentile_ms([t.added_latency_ms for t in trials], 0.95)

    failures: list[str] = []
    if candidate - baseline < minimum_gain:
        failures.append(
            f"the ordering improves by {candidate - baseline:.4f} nDCG at {depth}, below the "
            f"{minimum_gain} a reranking stage has to clear before its latency is earned"
        )
    if added > budget_ms:
        failures.append(
            f"it adds {added}ms at the ninety-fifth percentile, past the {budget_ms}ms budget"
        )
    return Evaluation(
        cases=len(judged),
        baseline_ndcg=baseline,
        candidate_ndcg=candidate,
        added_latency_p95_ms=added,
        verdict=Verdict.NOT_ADOPTED if failures else Verdict.ADOPTED,
        because=(
            "; ".join(failures)
            if failures
            else (
                f"the ordering improves by {candidate - baseline:.4f} nDCG at {depth} for "
                f"{added}ms at the ninety-fifth percentile, inside the {budget_ms}ms budget"
            )
        ),
    )


# --------------------------------------------------------- the recorded verdict


@dataclass(frozen=True)
class AdoptionRecord:
    """A decision taken on a date, with the reason and with what would reverse it.

    `what_would_change_it` is the field that makes this a decision rather than an opinion. A
    negative verdict with no falsifier is a door somebody has to argue their way past with no
    idea what the argument is, and the argument that eventually works is whichever one is
    made loudest.
    """

    decided_on: date
    verdict: Verdict
    because: str
    what_would_change_it: str

    def __post_init__(self) -> None:
        for name in ("because", "what_would_change_it"):
            if not str(getattr(self, name)).strip():
                msg = f"an adoption record states its {name}; without it the decision is a mood"
                raise QualityError(msg)


#: The verdict on reranking as it stands (M15.3.1). Not adopted, on the absence of evidence.
#:
#: There is no judged set in this repository and none can honestly be built by the person who
#: wants the answer. `brain.knowledge.fusion` records the same absence as its reason for
#: carrying no weights, and this is the same absence one layer up.
RERANKING: Final = AdoptionRecord(
    decided_on=date(2026, 9, 6),
    verdict=Verdict.NOT_MEASURABLE,
    because=(
        "no judged evaluation set exists here, so the ordering gain a reranking stage would "
        "buy has not been measured and cannot be; two costs are known without it, and both "
        "raise the bar rather than lower it. A reranking model reads every candidate passage, "
        "which puts the caller's question and their reachable passages into a second provider "
        "context. A local cross-encoder is a machine-learning dependency in the request path, "
        "which this architecture already refuses for entity resolution. Adopting on the "
        "assumption that reranking helps would spend a latency budget against an unmeasured "
        "gain, which is what the leaf asks not to do"
    ),
    what_would_change_it=(
        "a judged set of at least the minimum number of cases, run through evaluate, showing "
        "a gain past the threshold inside the budget; or, before that set exists, a retrieval "
        "signal over enough events showing the result people act on sitting well below first "
        "place, which is evidence that the ordering has room in it"
    ),
)


# ------------------------------------------------- the retrieval log (M15.3.4)


#: A retriever names itself in one lowercase token. Narrow on purpose: the names are joined
#: into one attribute value, and a name carrying the separator would split into retrievers
#: nobody ran, which is the same trap `brain.knowledge.search.DEPARTMENT_SEPARATOR` avoids by
#: checking the column's grammar.
RETRIEVER_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")

#: How the retriever names are joined for the one attribute that carries them. A dot, because
#: `RETRIEVER_RE` admits none, and because the joined value still passes the value grammar
#: `brain.ops.tracing` applies to anything it keeps.
RETRIEVER_SEPARATOR: Final = "."


@dataclass(frozen=True)
class RetrievalEvent:
    """One retrieval, as much of it as may be written down (M15.3.4).

    **Read the absences, they are the design.** There is no reference, no question, no
    principal, no reach, no candidate count taken before the reach predicate, no withheld
    count, no denied count and no flag saying the query was narrowed. A test asserts the field
    set against a list of the names those would arrive under, in the shape
    `test_a_ranking_has_nowhere_to_put_a_score` uses, because the pressure to add one is real
    and arrives during an incident.

    What is here: which of our own retrievers contributed, how many results the caller
    received, how many of those more than one retriever found, where in that list the caller
    acted, and how long it took. Every one of those is a fact about our ranking or about what
    the caller is already holding. See `A_COUNT_OF_WHAT_WAS_SHOWN_IS_NOT_A_COUNT_OF_WHAT_WAS_HIDDEN`
    and `THE_LOG_MEASURES_OUR_RANKING_AND_NEVER_THE_CORPUS`.

    `used` is one-based positions into the caller's own result list. A position past
    `returned` is refused: it is either a bug or a record of something the caller was not
    shown, and there is no third possibility worth keeping the field loose for.
    """

    retrievers: tuple[str, ...]
    returned: int
    corroborated: int = 0
    used: tuple[int, ...] = ()
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if not self.retrievers:
            msg = "a retrieval names the retrievers that ran; none of them is not a retrieval"
            raise QualityError(msg)
        if sorted(set(self.retrievers)) != list(self.retrievers):
            msg = (
                f"{list(self.retrievers)} is not a sorted set of retriever names; two records "
                "of one retrieval must compare equal, and an order that follows the caller's "
                "loop makes them differ"
            )
            raise QualityError(msg)
        unnamed = sorted(n for n in self.retrievers if not RETRIEVER_RE.match(n))
        if unnamed:
            msg = (
                f"{unnamed} is not a retriever name; the names are joined on "
                f"{RETRIEVER_SEPARATOR!r} into one value, so a name outside the grammar would "
                "split into retrievers nobody ran"
            )
            raise QualityError(msg)
        if self.returned < 0:
            msg = f"a retrieval returned {self.returned} results, which is not a count"
            raise QualityError(msg)
        if not 0 <= self.corroborated <= self.returned:
            msg = (
                f"{self.corroborated} of {self.returned} results were corroborated; more "
                "corroborated than returned is a count of something that was not shown"
            )
            raise QualityError(msg)
        if sorted(set(self.used)) != list(self.used):
            msg = (
                f"{list(self.used)} is not a sorted set of positions; one place used twice "
                "is one use, and an unsorted list makes two records of one retrieval differ"
            )
            raise QualityError(msg)
        outside = sorted(p for p in self.used if not 1 <= p <= self.returned)
        if outside:
            msg = (
                f"{outside} is outside the {self.returned} results this caller received; a "
                "position past the end is a record of something they were not shown"
            )
            raise QualityError(msg)
        if self.latency_ms < 0:
            msg = f"a retrieval took {self.latency_ms}ms, which is not a duration"
            raise QualityError(msg)

    @property
    def first_used_position(self) -> int:
        """Where the caller first acted, or nought when they did not act at all.

        Nought rather than None, because this is rendered into an attribute value and a None
        there would be masked as `[masked:none]` by anything applying the trace masks, which
        reads as a value somebody withheld rather than as a thing that did not happen.
        """
        return self.used[0] if self.used else 0

    def attributes(self) -> Mapping[str, object]:
        """The record as an attribute mapping, every value a scalar and every string a token.

        Written to the grammar `brain.ops.tracing` uses rather than to a grammar of its own.
        Most of these keys are not on that module's allowlist, so a caller putting them on a
        span gets them masked, and widening `SAFE_ATTRIBUTES` is a deliberate edit in a diff
        rather than something this module can do to itself from over here. What is guaranteed
        from this side is the weaker and checkable half: nothing emitted is a payload.

        `outcome` rather than a boolean, because it is one of the keys that module already
        trusts, and because "used" and "unused" say what happened where True and False need
        the reader to remember which way round the field was named.
        """
        return {
            "retrievers": RETRIEVER_SEPARATOR.join(self.retrievers),
            "returned": self.returned,
            "corroborated": self.corroborated,
            "used_count": len(self.used),
            "first_used_position": self.first_used_position,
            "latency_ms": self.latency_ms,
            "outcome": "used" if self.used else "unused",
        }


@dataclass(frozen=True)
class RetrievalSignal:
    """What a batch of retrievals says about the ranking, and nothing about the corpus.

    Rates rather than counts wherever a count would be about people: `used_share` says how
    often retrieval produced something somebody acted on, which is a property of the ranking.
    `events` is the denominator and is carried because a rate with no denominator is a number
    nobody can weigh.
    """

    events: int
    used_share: float
    top_position_share: float
    mean_first_used_position: float
    latency_p95_ms: float


def signal(events: Sequence[RetrievalEvent]) -> RetrievalSignal | None:
    """The learning signal, or None when there is not yet enough to have one (M15.3.4).

    None rather than a `RetrievalSignal` full of noise, and there is no way to confuse the
    two: a caller who wants a number below the floor has to write the arithmetic themselves,
    in a diff, where somebody can ask why. That is the shape `reach_for` uses for a caller
    holding no grant, and for the same reason: the empty answer must not be constructible by
    accident.

    `top_position_share` is over the events where something was used rather than over all of
    them, because it answers a different question: given that retrieval found the useful
    thing, did it put it first. Dividing by every event instead would fold the two questions
    together, and a fall in either would look like a fall in the other.

    `mean_first_used_position` is the number the reranking verdict names as the evidence that
    would reopen it. A mean well above one says the ordering has room in it, without saying
    anything about which documents exist or who reached them.
    """
    if len(events) < MINIMUM_EVENTS_FOR_A_SIGNAL:
        return None
    acted = [event for event in events if event.used]
    return RetrievalSignal(
        events=len(events),
        used_share=len(acted) / len(events),
        top_position_share=(
            sum(1 for event in acted if event.first_used_position == 1) / len(acted)
            if acted
            else 0.0
        ),
        mean_first_used_position=(
            sum(event.first_used_position for event in acted) / len(acted) if acted else 0.0
        ),
        latency_p95_ms=percentile_ms([float(event.latency_ms) for event in events], 0.95),
    )
