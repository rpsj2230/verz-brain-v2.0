"""The peak-concurrency load test the capacity model asks for (M22.3.3).

`brain.ops.admission.LOAD_TEST_TARGET` is a specification rather than an aspiration, and it
is quoted here so the two cannot drift: two arrivals a second for ten minutes, with a
production mix of workload classes rather than interactive only, holding the service levels
while batch work runs concurrently, and **zero interactive requests shed while any batch
work is still being admitted**.

**What this exercises, stated before anything else, because the honest scope is the point.**
This drives `decide` at the target arrival rate against evolving `CapacityState`. It is a
load test of the admission controller, which is the component that decides what gets shed,
and it is not a load test of the deployed system: no HTTP, no database, no model, no
network. The latency pass conditions in the target are properties of a running stack and
cannot be observed from here, so they are not asserted here and are named below as still
open.

That is worth being blunt about because a file called "load test" that quietly checks
something narrower is the kind of green tick that gets read as coverage it does not have.
What is genuinely tested is the shedding invariant, and that is the condition in the target
that is a *policy* rather than a measurement: it holds or fails regardless of how fast the
hardware is, which is precisely why it can be tested without hardware.

**The invariant that matters.** Under pressure the system may shed. Which work it sheds is
the whole question, and the answer in §25 is that a person waiting is never sacrificed for
work nobody is waiting on. A run where interactive requests are refused while batch jobs are
still being let in is the failure this asserts against, and it is not hypothetical: it is
what every priority scheme that shares one pool does the first time the pool is contended.

Task ids: M22.3.3
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.lane import Lane
from brain.gate.context import TrafficClass
from brain.ops.admission import (
    LOAD_TEST_TARGET,
    AdmissionRequest,
    CapacityState,
    Resource,
    Verdict,
    WorkloadClass,
    decide,
    seed_budgets,
)

START = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

#: Straight out of `LOAD_TEST_TARGET`. Held as numbers here and asserted against the
#: sentence below, so editing the target without editing the drive is a failure rather than
#: a test that quietly stops reproducing what it claims to.
ARRIVALS_PER_SECOND = 2
DURATION_SECONDS = 600
TOTAL_ARRIVALS = ARRIVALS_PER_SECOND * DURATION_SECONDS

#: The production mix, as (traffic class, lane, resource, connector key, share).
#:
#: Interactive dominates because people asking questions is what the system is for. The two
#: shapes that make this worth running are both deliberate:
#:
#: **System work answers on `model_calls`, the same budget interactive uses.** `TrafficClass.SYSTEM`
#: maps to the batch workload class, so this is batch and interactive contending for one
#: ceiling of forty. That is precisely where a priority scheme over a shared pool fails and
#: where the per-class share has to hold, so a mix that kept them on separate resources
#: would test nothing.
#:
#: **The fast lane asks for a source call and never a model call.** `AdmissionRequest`
#: refuses the fast lane a model slot outright, because the fast lane's whole guarantee is
#: that no model saw the question. Asking for a `source_calls` slot on a named connector is
#: what that path actually consumes.
MIX: tuple[tuple[TrafficClass, Lane, Resource, str, float], ...] = (
    (TrafficClass.HUMAN_INTERACTIVE, Lane.ANSWER, Resource.MODEL_CALLS, "", 0.50),
    (TrafficClass.HUMAN_INTERACTIVE, Lane.FAST, Resource.SOURCE_CALLS, "lark_base", 0.20),
    (TrafficClass.HUMAN_ASYNC, Lane.ANSWER, Resource.MODEL_CALLS, "", 0.14),
    (TrafficClass.AUTOMATION, Lane.ANSWER, Resource.MODEL_CALLS, "", 0.06),
    (TrafficClass.SYSTEM, Lane.ANSWER, Resource.MODEL_CALLS, "", 0.06),
    (TrafficClass.SYSTEM, Lane.TASK, Resource.DOCUMENT_JOBS, "", 0.04),
)


def _arrivals(seed: int = 20260906) -> list[AdmissionRequest]:
    """One run's worth of arrivals, drawn from the mix.

    Seeded, because a load test that draws a different population each run reports a
    different answer each run, and the one that fails is then dismissed as flaky. The seed
    is the date rather than 0 so it is obviously a choice.
    """
    rng = random.Random(seed)  # noqa: S311 - an arrival mix, not a secret
    shapes = [(t, lane, resource, key) for t, lane, resource, key, _ in MIX]
    weights = [share for *_, share in MIX]

    arrivals: list[AdmissionRequest] = []
    for n in range(TOTAL_ARRIVALS):
        traffic, lane, resource, key = rng.choices(shapes, weights=weights, k=1)[0]
        arrivals.append(
            AdmissionRequest(
                trace_id=f"load-{n:05d}",
                lane=lane,
                traffic_class=traffic,
                resource=resource,
                key=key,
            )
        )
    return arrivals


def _run() -> tuple[list[tuple[AdmissionRequest, Verdict]], Counter[str]]:
    """Drive the controller for the whole window, holding in-flight counts as it goes.

    Work is released on a fixed service time per class rather than a sampled one. A
    distribution would be more realistic and would make the result depend on the draw; the
    question here is which class gets shed when the budget is full, and that does not turn
    on whether one request took 3.1 or 3.4 seconds.
    """
    budgets = seed_budgets()
    service = {
        WorkloadClass.INTERACTIVE: 4,
        WorkloadClass.BACKGROUND: 20,
        WorkloadClass.BATCH: 90,
    }

    in_flight: dict[tuple[Resource, str], int] = {}
    releases: dict[int, list[tuple[Resource, str]]] = {}
    outcomes: list[tuple[AdmissionRequest, Verdict]] = []
    tally: Counter[str] = Counter()

    for index, request in enumerate(_arrivals()):
        second = index // ARRIVALS_PER_SECOND

        for key in releases.pop(second, []):
            in_flight[key] = max(0, in_flight.get(key, 0) - 1)

        state = CapacityState(used=dict(in_flight))
        decision = decide(request, budgets, state, now=START + timedelta(seconds=second))
        outcomes.append((request, decision.verdict))
        tally[f"{request.workload_class.value}:{decision.verdict.value}"] += 1

        if decision.verdict is Verdict.ADMITTED:
            key = request.budget_key
            in_flight[key] = in_flight.get(key, 0) + 1
            done = second + service[request.workload_class]
            releases.setdefault(done, []).append(key)

    return outcomes, tally


RESULTS, TALLY = _run()


# --------------------------------------------------------------- the target is reproduced
def test_the_drive_reproduces_the_target_the_capacity_model_wrote_down() -> None:
    """`LOAD_TEST_TARGET` is a specification and this file is supposed to be it. Two numbers
    in two places drift, so the sentence is asserted against what is actually driven.

    Delete this and the target can be raised to a rate the test never generates, leaving a
    capacity claim backed by a run a tenth its size."""
    assert f"{ARRIVALS_PER_SECOND} arrivals a second" in LOAD_TEST_TARGET
    assert "ten minutes" in LOAD_TEST_TARGET
    assert DURATION_SECONDS == 10 * 60
    assert len(RESULTS) == TOTAL_ARRIVALS == 1200


def test_the_run_is_a_mix_of_classes_and_not_interactive_only() -> None:
    """The target says "with the mix of workload classes seen in production rather than
    interactive only", and it says so because an interactive-only run cannot produce the
    contention the shedding rule exists for. A run with no batch work in it would satisfy
    the invariant below vacuously."""
    seen = {workload for workload, _ in ((r.workload_class, v) for r, v in RESULTS)}

    assert WorkloadClass.INTERACTIVE in seen
    assert WorkloadClass.BATCH in seen or WorkloadClass.BACKGROUND in seen
    assert len(seen) >= 2


# --------------------------------------------------------------- the invariant under load
def test_no_interactive_request_is_shed_while_batch_work_is_still_admitted() -> None:
    """**The condition in the target that is policy rather than hardware**, and the reason
    this can be tested without a running stack at all.

    A person waiting is never sacrificed for work nobody is waiting on. Every priority
    scheme over a single shared pool breaks this the first time the pool is contended,
    because priority decides who gets the next free slot and not who is holding the ones
    already taken. §25's answer is separate budgets per class, and this is that answer
    holding across a full peak window.

    Delete this and the class ceilings can be merged into one global pool, which reads as a
    simplification and turns every busy minute into a refused question."""
    batch_admitted_at: set[int] = set()
    interactive_shed_at: set[int] = set()

    for index, (request, verdict) in enumerate(RESULTS):
        second = index // ARRIVALS_PER_SECOND
        if request.workload_class is WorkloadClass.BATCH and verdict is Verdict.ADMITTED:
            batch_admitted_at.add(second)
        if request.workload_class is WorkloadClass.INTERACTIVE and verdict is Verdict.SHED:
            interactive_shed_at.add(second)

    overlap = sorted(batch_admitted_at & interactive_shed_at)

    assert not overlap, (
        f"in second(s) {overlap[:5]} an interactive request was shed while batch work was "
        "still being admitted; a person waiting was sacrificed for work nobody is waiting on"
    )


def test_the_interactive_success_rate_holds_across_the_window() -> None:
    """The service-level half of the target that is observable here. Latency is not: it is a
    property of a running stack and this drives a pure function.

    So what is asserted is the admission side of "successful request rate above 99.5%",
    which is that the controller does not refuse people at the peak the system is sized for.
    A controller that shed one interactive request in a hundred at design load would be one
    whose budgets are wrong, and that is visible from here."""
    interactive = [(r, v) for r, v in RESULTS if r.workload_class is WorkloadClass.INTERACTIVE]
    assert interactive, "the mix produced no interactive work, so this asserts nothing"

    shed = sum(1 for _, v in interactive if v is Verdict.SHED)
    rate = 1 - (shed / len(interactive))

    assert rate >= 0.995, f"interactive requests succeeded {rate:.4%} of the time, target 99.5%"


def test_every_arrival_got_exactly_one_of_the_three_verdicts() -> None:
    """Totality, under load rather than in a unit test. `decide` is documented as total, and
    a fourth outcome, or a request that fell through without one, would be work started
    against no budget at all.

    Delete this and a run could silently drop arrivals and the rates above would improve."""
    verdicts = Counter(v for _, v in RESULTS)

    assert sum(verdicts.values()) == TOTAL_ARRIVALS
    assert set(verdicts) <= {Verdict.ADMITTED, Verdict.QUEUED, Verdict.SHED}


def test_the_run_actually_reached_saturation() -> None:
    """**The guard that stops all of the above being vacuous.** A window in which nothing was
    ever queued or shed proves the budgets are generous, not that the shedding rule works,
    and every assertion above would pass over a system that admitted everything.

    So the run has to contend for something. If this fails, the arrival rate or the service
    times need raising until it does, and the invariant tests mean something again."""
    contended = sum(1 for _, v in RESULTS if v in (Verdict.QUEUED, Verdict.SHED))

    assert contended > 0, (
        "nothing was queued or shed across the whole window, so the shedding invariant was "
        "never exercised: this run does not test what it claims to"
    )


@pytest.mark.parametrize("unmeasurable", ["ANSWER p95", "FAST p95"])
def test_the_latency_conditions_are_named_as_still_open(unmeasurable: str) -> None:
    """The target has three pass conditions and this file can only observe two. Latency needs
    a deployed stack, a database and a model, and asserting it against a pure function would
    be inventing a number.

    Named in a test rather than left in prose so the gap is in the report somebody reads
    rather than in a docstring somebody skims. When a staging environment exists, M38.2.1.2
    runs the suite against it and these become measurable.

    Delete this and the file reads as though it covered the whole specification."""
    assert unmeasurable.split()[0] in LOAD_TEST_TARGET
