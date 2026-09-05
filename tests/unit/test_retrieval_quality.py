"""Deciding whether reranking is worth its latency, and logging retrieval without logging
what a caller could not see.

Two halves, two failure modes, and neither of them looks like a failure.

**An evaluation harness fails by adopting.** Every default in a measurement tool points the
same way: an empty set averages to something, a missing threshold compares as true, a mean
hides the tail a person actually waits through. A harness that fell through to adoption when
it could not measure would adopt on exactly the occasions nobody looked, and the added latency
would then be permanent, because removing a stage that is already in production needs an
argument that putting it there never did.

**A retrieval log fails by counting.** The log is written for an operator and an operator
wants numbers, so "showing 3 of 47" arrives helpfully during an incident and stays for the
retention period. `brain.ops.tracing` settled the shape of the answer: the allowlist is names
and never values. The tests below hold this module to that same grammar and to the harder rule
underneath it, which is that a number attributing a short result list to permissions is a count
of hidden things whatever it is called.

Task ids: M15.3.1, M15.3.4
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from brain.knowledge import quality
from brain.knowledge.quality import (
    EVALUATION_DEPTH,
    MINIMUM_EVENTS_FOR_A_SIGNAL,
    MINIMUM_JUDGED_CASES,
    MINIMUM_NDCG_GAIN,
    RERANK_BUDGET_MS,
    RERANKING,
    AdoptionRecord,
    JudgedCase,
    QualityError,
    RetrievalEvent,
    Trial,
    Verdict,
    evaluate,
    ndcg,
    percentile_ms,
    signal,
)
from brain.ops.tracing import ATTRIBUTE_KEY_RE, SAFE_VALUE_MAX_CHARS, VALUE_TOKEN_RE

POOL = 10


def ordering(case: int, place: int) -> tuple[str, ...]:
    """One case's ordering, with its single relevant reference at `place`, one-based.

    The filler references are shared between a case's baseline and its candidate, so a
    candidate built this way is a permutation of its baseline and never introduces anything.
    That is the ordinary shape of a rerank, and building it here rather than in each test
    keeps the introduction refusal a deliberate act instead of an accident of a fixture.
    """
    refs = [f"f{case}_{j}" for j in range(POOL - 1)]
    refs.insert(place - 1, f"r{case}")
    return tuple(refs)


def judged(count: int = MINIMUM_JUDGED_CASES) -> tuple[JudgedCase, ...]:
    return tuple(JudgedCase(case_id=f"q{i}", relevant=frozenset({f"r{i}"})) for i in range(count))


def tried(
    cases: tuple[JudgedCase, ...],
    *,
    baseline_place: int,
    candidate_place: int,
    latency_ms: float = 10.0,
) -> tuple[Trial, ...]:
    return tuple(
        Trial(
            case_id=case.case_id,
            baseline=ordering(i, baseline_place),
            candidate=ordering(i, candidate_place),
            added_latency_ms=latency_ms,
        )
        for i, case in enumerate(cases)
    )


def event(**overrides: object) -> RetrievalEvent:
    fields: dict[str, object] = {
        "retrievers": ("lexical", "vector"),
        "returned": 10,
        "corroborated": 3,
        "used": (),
        "latency_ms": 40,
    }
    fields.update(overrides)
    return RetrievalEvent(**fields)  # type: ignore[arg-type]


# ------------------------------------------------- what a reranker may do (M15.3.1)
def test_a_reranker_may_reorder_and_may_truncate_but_never_introduce() -> None:
    """**The disclosure guard on this half.** The candidate set came out of a query carrying
    the caller's reach predicate, so a stage able to add a reference is a stage able to add one
    from outside that reach, and the addition would arrive labelled as a retrieval result.
    Truncation is safe for the mirror-image reason and is therefore allowed: a shorter list
    cannot contain anything the longer one did not.

    Delete this and a reranker that consults its own index, or that falls back to a second
    query when the candidates look thin, passes the evaluation and ships.
    """
    baseline = ("a", "b", "c")
    Trial(case_id="q", baseline=baseline, candidate=("c", "a", "b"), added_latency_ms=1.0)
    Trial(case_id="q", baseline=baseline, candidate=("c", "a"), added_latency_ms=1.0)
    with pytest.raises(QualityError, match="reranked into"):
        Trial(case_id="q", baseline=baseline, candidate=("a", "d"), added_latency_ms=1.0)


def test_a_reference_ranked_twice_in_one_ordering_is_refused() -> None:
    """The same refusal `Ranking.__post_init__` makes, for the same reason and with an extra
    one here: a reference in two places contributes gain twice, so the metric reads as an
    improvement produced by a bug in whoever built the list.

    Delete this and a reranker that emits a reference twice scores better than one that does
    not.
    """
    with pytest.raises(QualityError, match="more than once"):
        Trial(case_id="q", baseline=("a", "a"), candidate=("a",), added_latency_ms=1.0)


def test_a_case_judging_nothing_relevant_is_refused() -> None:
    """Its ideal gain is nought, so it has no nDCG at all, and both available treatments are
    wrong: skipping it makes the evaluation set smaller than it says it is, and scoring it as
    perfect rewards a reranker for a question that has no answer. Refusing it keeps the fixed
    set honest, which is the only thing making the comparison a comparison.

    Delete this and the denominator can reach nought, and whichever branch handles that
    decides the verdict.
    """
    with pytest.raises(QualityError, match="judges nothing relevant"):
        JudgedCase(case_id="q", relevant=frozenset())


# ------------------------------------------------------- the metric (M15.3.1)
def test_moving_a_relevant_result_up_the_list_scores_better() -> None:
    """The sanity check the whole harness rests on. A metric that did not respond to position
    would make every verdict a coin flip, and the coin would be flipped once and then written
    into a constant.

    Delete this and the discount can be inverted or the slice taken from the wrong end, and
    every other test in this file still passes because they all compare two numbers from the
    same function.
    """
    case = JudgedCase(case_id="q", relevant=frozenset({"r0"}))
    assert ndcg(ordering(0, 1), case) > ndcg(ordering(0, 3), case)
    assert ndcg(ordering(0, 1), case) == pytest.approx(1.0)


def test_a_relevant_result_below_the_evaluation_depth_scores_nothing() -> None:
    """Ten is what an answer reads. A stage that improves places 40 to 50 has improved nothing
    anybody sees, and a metric measuring the whole candidate list would report that as a gain
    worth paying latency for.

    Delete this and the depth can be widened to the candidate depth, at which point reranking
    the tail earns its budget.
    """
    case = JudgedCase(case_id="q", relevant=frozenset({"deep"}))
    beyond = (*(f"f{j}" for j in range(EVALUATION_DEPTH)), "deep")
    assert ndcg(beyond, case) == 0.0


def test_a_percentile_is_a_latency_that_actually_happened() -> None:
    """Nearest-rank, never interpolated, restated here from `brain.connectors.throttle` rather
    than imported across a package boundary. An interpolated percentile returns a number
    nobody experienced, and a budget is a claim about what happened.

    Delete this and the function can be changed to interpolate, which moves every measured p95
    a little below the truth, in the direction of adoption.
    """
    assert percentile_ms([10.0, 20.0, 1000.0], 0.95) == 1000.0
    assert percentile_ms([], 0.95) == 0.0


# ------------------------------------------------------ the verdict (M15.3.1)
def test_the_smallest_judged_set_is_the_size_at_which_one_case_cannot_decide_it() -> None:
    """**The threshold is derived rather than chosen, and this is the derivation.** With `n`
    cases a single case moves the mean by at most `1/n`, so below `1 / gain` cases one question
    decides adoption on its own and the verdict becomes a property of which questions somebody
    happened to write down.

    Delete this and the two constants drift: lowering the gain to make a stage adoptable would
    silently also make a smaller set sufficient, which is the same loosening twice.
    """
    assert math.ceil(1 / MINIMUM_NDCG_GAIN) == MINIMUM_JUDGED_CASES
    assert 1 / MINIMUM_JUDGED_CASES <= MINIMUM_NDCG_GAIN


def test_a_set_too_small_to_decide_produces_no_verdict_and_no_numbers() -> None:
    """The default direction in the absence of evidence is never adoption. The metrics are
    reported as nought rather than computed, deliberately: a mean over three cases is a number
    somebody screenshots, and the screenshot outlives the caveat next to it.

    Delete this and a three-case set returns a verdict, and the first reranking trial anybody
    runs is the one that decides.
    """
    cases = judged(3)
    outcome = evaluate(cases, tried(cases, baseline_place=10, candidate_place=1))
    assert outcome.verdict is Verdict.NOT_MEASURABLE
    assert not outcome.verdict.adopted
    assert (outcome.baseline_ndcg, outcome.candidate_ndcg) == (0.0, 0.0)


def test_an_ordering_that_is_better_and_fast_enough_is_adopted() -> None:
    """The positive sibling of every refusal in this section. A harness that never adopts is
    satisfied by a function returning `NOT_ADOPTED`, and the whole point of building it is
    that it would say yes if a reranker earned it.

    Delete this and the harness can decay into a constant, and the decay reads in a diff as
    caution.
    """
    cases = judged()
    outcome = evaluate(cases, tried(cases, baseline_place=3, candidate_place=1))
    assert outcome.verdict is Verdict.ADOPTED
    assert outcome.verdict.adopted
    assert outcome.gain > MINIMUM_NDCG_GAIN


def test_an_ordering_that_is_better_but_too_slow_is_not_adopted() -> None:
    """The leaf in one sentence: a gain is not adoption if it is not earned. Without this the
    budget is decorative, and a stage that doubles the answer path ships because the ordering
    improved.

    Delete this and the latency half of the condition stops being checked, which is the half
    the leaf is named after.
    """
    cases = judged()
    slow = tried(cases, baseline_place=3, candidate_place=1, latency_ms=RERANK_BUDGET_MS + 1)
    outcome = evaluate(cases, slow)
    assert outcome.verdict is Verdict.NOT_ADOPTED
    assert "budget" in outcome.because


def test_an_ordering_that_is_fast_but_barely_better_is_not_adopted() -> None:
    """The other half. A stage costing a tenth of the budget and buying nothing measurable has
    been adopted because a number moved, which is how a request path acquires four such stages
    in a year and nobody can say which one to remove.

    Delete this and "no worse" becomes the standard, and every stage clears it.
    """
    cases = judged()
    outcome = evaluate(cases, tried(cases, baseline_place=1, candidate_place=1))
    assert outcome.verdict is Verdict.NOT_ADOPTED
    assert outcome.gain == pytest.approx(0.0)
    assert "nDCG" in outcome.because


def test_both_reasons_are_reported_when_a_stage_fails_both_conditions() -> None:
    """A report naming only the first failure invites a second attempt that fixes it and runs
    straight into the other one, which is two evaluation cycles spent learning something one
    could have said.

    Delete this and the verdict short-circuits, and the slow-and-useless case reads as merely
    slow.
    """
    cases = judged()
    outcome = evaluate(
        cases, tried(cases, baseline_place=1, candidate_place=1, latency_ms=RERANK_BUDGET_MS + 1)
    )
    assert "nDCG" in outcome.because
    assert "budget" in outcome.because


def test_the_latency_that_decides_is_the_tail_and_never_the_mean() -> None:
    """A rerank stage's cost is a tail problem: a batch that spills, a cold model, a provider
    retry. The mean hides all three. Here the mean is comfortably inside the budget and the
    ninety-fifth percentile is far outside it, so a harness averaging instead of ranking would
    adopt a stage that stalls one request in twenty.

    Delete this and the percentile can be swapped for a mean during a tidy-up, and the change
    is invisible in every other test because they use one latency per run.
    """
    cases = judged()
    latencies = [10.0] * (len(cases) - 2) + [1000.0, 1000.0]
    trials = tuple(
        Trial(
            case_id=case.case_id,
            baseline=ordering(i, 3),
            candidate=ordering(i, 1),
            added_latency_ms=latencies[i],
        )
        for i, case in enumerate(cases)
    )
    outcome = evaluate(cases, trials)
    assert sum(latencies) / len(latencies) < RERANK_BUDGET_MS
    assert outcome.added_latency_p95_ms == 1000.0
    assert outcome.verdict is Verdict.NOT_ADOPTED


def test_an_evaluation_whose_set_and_whose_runs_disagree_is_refused() -> None:
    """A comparison over a fixed set is the whole claim, and a set that merely overlaps the
    runs is not fixed. The cases quietly left out are the ones somebody looked at first, so the
    silent version of this is the one that produces a favourable answer.

    Delete this and an evaluation can be run over whichever cases happened to succeed.
    """
    cases = judged()
    with pytest.raises(QualityError, match="tried without a judgement"):
        evaluate(cases[:-1], tried(cases, baseline_place=3, candidate_place=1))
    with pytest.raises(QualityError, match="judged without a trial"):
        evaluate(cases, tried(cases, baseline_place=3, candidate_place=1)[:-1])


def test_only_one_verdict_means_adopt() -> None:
    """`NOT_MEASURABLE` and `NOT_ADOPTED` are different facts about the world and lead to
    different next steps, and they must behave identically at the one place it matters. A
    caller reading `verdict is not Verdict.NOT_ADOPTED` as approval is the bug this property
    exists to make impossible.

    Delete this and a third non-adopting verdict added later reads as adoption at any call
    site using an inequality.
    """
    assert [v for v in Verdict if v.adopted] == [Verdict.ADOPTED]


def test_no_reranker_is_implemented_beside_the_harness_that_judges_one() -> None:
    """Writing one and leaving it unwired is how an unadopted thing gets adopted: it sits
    there looking finished, and the person who wires it up is not the person who read the
    verdict. The harness compares orderings, so it needs no reranker to be useful and outlives
    whichever one is eventually tried.

    Delete this and a helpful cross-encoder wrapper lands in this module, unused, until it is
    not.
    """
    public = {n for n in dir(quality) if not n.startswith("_")}
    implementations = {n for n in public if callable(getattr(quality, n)) and "rerank" in n.lower()}
    assert not implementations, f"a reranker is implemented here: {sorted(implementations)}"


def test_the_recorded_verdict_is_not_an_adoption_and_says_what_would_reverse_it() -> None:
    """**The leaf's actual answer.** There is no judged set in this repository, so reranking is
    recorded as not measurable and therefore not adopted, and the record names the evidence
    that would reopen it. A negative verdict with no falsifier is a door somebody argues past
    with no idea what the argument is, and the argument that eventually works is the loudest.

    Delete this and the recorded decision can be flipped to adoption without anybody having to
    produce the set the reason says is missing.
    """
    assert isinstance(RERANKING, AdoptionRecord)
    assert not RERANKING.verdict.adopted
    assert RERANKING.verdict is Verdict.NOT_MEASURABLE
    assert "no judged evaluation set exists" in RERANKING.because
    assert RERANKING.what_would_change_it.strip()


def test_an_adoption_record_without_a_reason_cannot_be_written() -> None:
    """The same rule `Retention.because` enforces. A decision nobody can explain is one that
    gets reversed the first time somebody wants the other answer, and the reversal is permanent
    because nobody knows what the original was protecting.

    Delete this and a record can be written with an empty reason, which is a mood with a date
    on it.
    """
    with pytest.raises(QualityError, match="what_would_change_it"):
        AdoptionRecord(
            decided_on=RERANKING.decided_on,
            verdict=Verdict.ADOPTED,
            because="it felt quick",
            what_would_change_it="  ",
        )


# ------------------------------------------------- the retrieval log (M15.3.4)
def test_a_retrieval_event_has_nowhere_to_record_what_was_withheld() -> None:
    """**The rule this platform is built on, expressed as a field set.** A withheld count is a
    count of hidden things directly; a pre-filter candidate count is the same number by
    subtraction; the caller's departments say why the list was short. None of the three has a
    field, and none of them appears among the attributes the record emits, so adding one is an
    edit to a dataclass in a diff rather than a dictionary key somebody adds at three in the
    morning.

    Delete this and the first incident that needs to know why an answer was thin adds
    `withheld`, and it is then in the log store for the retention period.
    """
    names = {f.name for f in dataclasses.fields(RetrievalEvent)}
    assert names == {"retrievers", "returned", "corroborated", "used", "latency_ms"}
    forbidden = {
        "withheld",
        "filtered",
        "denied",
        "hidden",
        "excluded",
        "out_of_reach",
        "total",
        "available",
        "candidates",
        "depth",
        "reach",
        "departments",
        "principal",
        "principal_id",
        "question",
        "query",
        "refs",
        "references",
        "chunk_id",
        "document_id",
    }
    surface = names | {n for n in dir(RetrievalEvent) if not n.startswith("_")}
    assert not (surface & forbidden), f"the retrieval log records an absence: {surface}"
    assert not (set(event().attributes()) & forbidden)


def test_every_value_a_retrieval_event_emits_would_survive_the_trace_masks_grammar() -> None:
    """`brain.ops.tracing` decides what may leave this process, and the decision is that a
    value is kept only if it still looks like system vocabulary. This module is not a trace and
    its record is not a span, so nothing forces the two together except this test, which is the
    checkable half of "nothing here is a payload": every key is system-shaped, every value is a
    scalar, and every string is a short lowercase token.

    Delete this and a helpful `question` or `title` lands among the attributes, passes every
    other test in this file, and is written wherever the log goes.
    """
    attributes = event(used=(2,)).attributes()
    for key, value in attributes.items():
        assert ATTRIBUTE_KEY_RE.match(key), f"{key} is not a system-shaped attribute key"
        assert isinstance(value, int | float | str), f"{key} carries a container"
        if isinstance(value, str):
            assert len(value) <= SAFE_VALUE_MAX_CHARS
            assert VALUE_TOKEN_RE.match(value), f"{key}={value!r} is not system vocabulary"


def test_a_use_recorded_past_the_end_of_the_result_list_is_refused() -> None:
    """A position beyond what the caller received is either a bug or a record of something they
    were not shown, and there is no third possibility worth keeping the field loose for. It is
    also the shape a "position in the unfiltered candidate list" would arrive in.

    Delete this and a caller passing candidate-list positions instead of result positions puts
    the pre-filter ranking into the log, which is the one number that attributes.
    """
    with pytest.raises(QualityError, match="outside"):
        event(returned=3, corroborated=0, used=(4,))


def test_more_results_corroborated_than_returned_is_refused() -> None:
    """Corroboration is a property of the results the caller received, so a figure larger than
    the list is a count of something that was not in it. The arithmetic is the giveaway and the
    refusal is where it is noticed.

    Delete this and a caller counting corroboration over the pre-fusion candidates rather than
    over the page records a number about results nobody saw.
    """
    with pytest.raises(QualityError, match="corroborated"):
        event(returned=2, corroborated=3)


def test_a_retrieval_that_returned_nothing_is_still_recordable() -> None:
    """The positive sibling that matters most. A log refusing the empty retrieval would be
    missing exactly the retrievals worth learning from, and the shape of the learning signal
    would then be decided by which retrievals happened to succeed.

    Delete this and a validator tightened to "a retrieval returns something" silently drops the
    unanswered questions out of the signal.
    """
    empty = event(returned=0, corroborated=0, used=())
    assert empty.attributes()["outcome"] == "unused"
    assert empty.first_used_position == 0


def test_two_records_of_one_retrieval_compare_equal() -> None:
    """The retrievers are held as a sorted set, so a caller building the list in whichever
    order their loop ran produces the same record. Without it the same retrieval is two
    different rows, and every aggregate over the log double-counts by however many orderings
    the callers happen to use.

    Delete this and the canonical form is lost, which nothing notices until a rate is wrong by
    a factor nobody can explain.
    """
    assert event() == event()
    with pytest.raises(QualityError, match="sorted set of retriever names"):
        event(retrievers=("vector", "lexical"))


def test_a_retriever_carrying_the_separator_in_its_name_is_refused() -> None:
    """The names are joined into one attribute value, so a name containing the separator would
    split into retrievers nobody ran. The same trap `brain.knowledge.search` avoids by holding
    the department column to a grammar that admits no comma.

    Delete this and a leg named `hybrid.v2` becomes two legs in every aggregate over the log.
    """
    with pytest.raises(QualityError, match="not a retriever name"):
        event(retrievers=("hybrid.v2",))


# ------------------------------------------------------- the signal (M15.3.4)
def test_no_signal_is_reported_until_there_are_enough_retrievals_to_be_one() -> None:
    """A rate computed over one retrieval is that retrieval, and a learning rule promoted from
    it is a rule promoted from one person's afternoon. None rather than a number with a
    caveat, because the caveat is dropped and the number is not.

    Delete this and the first three retrievals after a deploy decide what the ranking learns.
    """
    assert signal([event() for _ in range(MINIMUM_EVENTS_FOR_A_SIGNAL - 1)]) is None
    assert signal([event() for _ in range(MINIMUM_EVENTS_FOR_A_SIGNAL)]) is not None


def test_the_signal_says_where_people_acted_and_never_what_they_acted_on() -> None:
    """**The learning signal the leaf asks for, derived without recording anything a caller
    could not see.** It is a measurement of our own ranking: given that retrieval found the
    useful thing, how far down the list did it put it. No reference, no question and no
    principal contributes to it, so it aggregates across callers without any of them
    contributing an object identity.

    Delete this and the aggregate stops being exercised, and the obvious next change is to
    carry the reference "so the boost knows what to boost".
    """
    events = (
        [event(used=(1,)) for _ in range(4)]
        + [event(used=(5,)) for _ in range(2)]
        + [event(used=()) for _ in range(4)]
    )
    measured = signal(events)
    assert measured is not None
    assert measured.events == 10
    assert measured.used_share == pytest.approx(0.6)
    assert measured.mean_first_used_position == pytest.approx((4 * 1 + 2 * 5) / 6)


def test_the_share_of_first_places_is_over_the_retrievals_that_found_something() -> None:
    """Two different questions, and folding them together makes a fall in either look like a
    fall in the other. "How often does retrieval find the useful thing" and "when it does, does
    it put it first" have different remedies, and the second is the one that would justify
    reranking.

    Delete this and the denominator becomes every event, at which point improving recall lowers
    the ordering score and the two signals point in opposite directions.
    """
    events = [event(used=(1,)) for _ in range(4)] + [event(used=(5,)) for _ in range(2)]
    events += [event(used=()) for _ in range(4)]
    measured = signal(events)
    assert measured is not None
    assert measured.top_position_share == pytest.approx(4 / 6)


def test_a_batch_where_nobody_acted_reports_a_signal_rather_than_dividing_by_nothing() -> None:
    """The empty numerator is the ordinary case for a quiet hour, and a signal that raised on
    it would put a `try` around the only caller. Nought is the honest answer to "how far down
    was the useful result" when there was not one.

    Delete this and the first quiet window takes the aggregation job down.
    """
    measured = signal([event(used=()) for _ in range(MINIMUM_EVENTS_FOR_A_SIGNAL)])
    assert measured is not None
    assert measured.used_share == 0.0
    assert measured.top_position_share == 0.0
    assert measured.mean_first_used_position == 0.0
