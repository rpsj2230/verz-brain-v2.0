"""The queue seam: the connection it refuses, the slots it allocates, and the one file rule.

Every test here is about a worker that would run and appear to be working.

Task ids: M32.4.2.1, M32.4.2.2, M32.4.2.3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain.gate.context import TrafficClass
from brain.ops.queue import (
    CONCURRENCY,
    DRIVER_IMPLEMENTATIONS,
    DRIVER_MODULE,
    MAX_ARGUMENT_CHARS,
    MIB_PER_SLOT,
    SWAP_CANDIDATES,
    Job,
    QueueDriver,
    QueueError,
    SwapCandidate,
    concurrency_gaps,
    queue_url_refusals,
    total_slots,
)
from brain.ops.wiring import component

SRC = Path(__file__).resolve().parents[2] / "src" / "brain"

APP_URL = "postgresql+psycopg://brain:pw@pgbouncer:5432/brain"
DIRECT_URL = "postgresql+psycopg://brain:pw@db:5432/brain"


# --------------------------------------------------- the pooler rule
def test_a_worker_handed_the_applications_own_connection_string_is_refused() -> None:
    """The mistake that is easy to write and impossible to see. The application's URL goes
    through the transaction pooler; a LISTEN behind one is moved to another backend and
    stops receiving notifications, so the worker polls on its fallback timer and every
    metric says the queue is empty. Delete this and the third instance of this bug in this
    repository ships."""
    refusals = queue_url_refusals(APP_URL, app_url=APP_URL)
    assert any("application's own connection string" in r for r in refusals)


def test_a_queue_url_pointing_at_the_pooler_is_refused_on_its_own() -> None:
    """The same fault reached differently: a worker with its own variable, set to the pooler
    because that is what the other variable said. Delete this and only the exact-match case
    is caught."""
    assert any("transaction pooler" in r for r in queue_url_refusals(APP_URL))


def test_a_url_carrying_a_pooler_workaround_parameter_is_refused() -> None:
    """A confession in the query string. Nobody sets `prepare_threshold` on a connection
    that is not behind a pooler, so its presence says the author knew and worked around it -
    and a queue does not work around a pooler, it does not use one. Delete this and the
    signal is thrown away."""
    refusals = queue_url_refusals(f"{DIRECT_URL}?prepare_threshold=0")
    assert any("prepare_threshold" in r for r in refusals)


def test_a_direct_connection_is_accepted_without_comment() -> None:
    """A check that refuses everything is a check that gets disabled. Delete this and
    `queue_url_refusals` could return a finding unconditionally and every test above would
    still pass."""
    assert queue_url_refusals(DIRECT_URL, app_url=APP_URL) == ()


def test_the_pooler_is_reported_once_for_one_mistake() -> None:
    """The hostname is checked as a hostname and the markers are checked in the query
    string. Matching markers against the whole URL reports the pooler twice for one fault,
    which trains whoever reads the output to skim it. Delete this and the duplicate comes
    back."""
    refusals = queue_url_refusals(APP_URL)
    assert len(refusals) == 1


# --------------------------------------------------- concurrency
def test_every_traffic_class_has_a_number_of_slots_decided_for_it() -> None:
    """A class with no allocation is a class whose jobs are never fetched, which presents as
    a queue that fills and never drains and takes a day to find. Delete this and adding a
    member to `TrafficClass` silently strands its jobs."""
    assert set(CONCURRENCY) == set(TrafficClass)
    assert concurrency_gaps() == ()


def test_interactive_traffic_gets_no_queue_slots_at_all() -> None:
    """Not an oversight and not a small number. A person watching a cursor is answered
    inside the request; a queue in that path adds a hop whose only possible effect is delay.
    Delete this and the zero reads as a bug and gets 'fixed'."""
    assert CONCURRENCY[TrafficClass.HUMAN_INTERACTIVE] == 0
    assert CONCURRENCY[TrafficClass.HUMAN_ASYNC] > CONCURRENCY[TrafficClass.AUTOMATION]


def test_the_declared_slots_fit_inside_the_workers_memory_limit() -> None:
    """On this host the limit is real: exceeding it is an OOM kill, not a slowdown, and the
    box runs a second production system. Delete this and concurrency can be raised to fix a
    backlog, which converts a queue depth problem into a neighbour's outage."""
    assert total_slots() * MIB_PER_SLOT <= component("brain-worker").memory_mib


def test_raising_concurrency_past_the_workers_memory_is_reported() -> None:
    """The check has to fail when it should. Delete this and `concurrency_gaps` could
    compare the total against itself."""
    greedy = {**CONCURRENCY, TrafficClass.HUMAN_ASYNC: 100}
    gaps = concurrency_gaps(greedy)
    assert any("over the" in gap for gap in gaps), gaps


def test_a_traffic_class_with_no_allocation_is_reported() -> None:
    """The other half of the same check. A class missing from the mapping is a class whose
    jobs are enqueued and never fetched, and nothing else in the system would report it.
    Delete this and the closure check is never seen to fail."""
    partial: dict[TrafficClass, int] = {
        k: v for k, v in CONCURRENCY.items() if k is not TrafficClass.SYSTEM
    }
    assert any("never fetched" in gap for gap in concurrency_gaps(partial))


# --------------------------------------------------- jobs carry references
def test_a_job_argument_long_enough_to_be_content_is_refused() -> None:
    """A queue table is a copy of business data with a different retention, no row-level
    security, and a habit of being read in psql during an incident. Delete this and the
    first task that takes a question as an argument puts every question in a table nobody
    redacts."""
    with pytest.raises(QueueError, match="content rather than a reference"):
        Job(
            task="answer.compose",
            traffic_class=TrafficClass.HUMAN_ASYNC,
            args={"question": "x" * (MAX_ARGUMENT_CHARS + 1)},
        )
    Job(
        task="answer.compose",
        traffic_class=TrafficClass.HUMAN_ASYNC,
        args={"question_id": "x" * MAX_ARGUMENT_CHARS},
    )


def test_a_job_with_no_task_name_is_refused() -> None:
    """An empty task name is a row nothing will ever fetch and nothing will ever report.
    Delete this and it sits in the table for ever looking like backlog."""
    with pytest.raises(QueueError, match="no task name"):
        Job(task="  ", traffic_class=TrafficClass.SYSTEM)


# --------------------------------------------------- the seam (M32.4.2.2)
def test_no_module_outside_the_driver_names_a_queue_implementation() -> None:
    """This is what makes "the swap changes one file" a fact rather than an intention. Both
    names are checked, not only the one in use, because a half-finished migration leaving
    imports of the old one in three modules is exactly the state worth catching. Delete this
    and the seam closes quietly the first time somebody imports the driver directly."""
    driver = SRC / "ops" / "queue.py"
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == driver:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for name in DRIVER_IMPLEMENTATIONS:
            if name in text:
                offenders.append(f"{path.relative_to(SRC)}: {name}")
    assert not offenders, offenders
    assert DRIVER_MODULE == "brain.ops.queue"


def test_the_driver_protocol_hands_out_no_connection_session_or_transaction() -> None:
    """A driver that returned one would let a caller take a session-level lock through it,
    which is the failure this module exists to prevent, and it would make the recorded swap
    impossible because Hatchet has no Postgres session to hand out. Delete this and the
    protocol grows a `connection()` method the first time a task needs one."""
    methods = {name for name in vars(QueueDriver) if not name.startswith("_")}
    assert methods == {"enqueue", "fetch", "complete", "fail"}


# --------------------------------------------------- the recorded decision (M32.4.2.1, .3)
def test_the_recorded_alternative_carries_a_trigger_and_a_measurement() -> None:
    """An alternative with no trigger is a note that somebody once read a comparison page.
    The trigger is what makes it revisitable by evidence, and `measured_by` is what stops
    the trigger being a feeling about how things seem. Delete this and the fallback seam
    becomes a paragraph."""
    assert [c.name for c in SWAP_CANDIDATES] == ["hatchet"]
    for candidate in SWAP_CANDIDATES:
        assert candidate.trigger.strip()
        assert candidate.measured_by.strip()
        assert candidate.costs.strip()


def test_an_alternative_with_no_trigger_cannot_be_recorded() -> None:
    """The constructor guard behind the test above. Delete this and the next candidate is
    added with three fields filled in and two left empty, which is how the list becomes
    marketing."""
    with pytest.raises(QueueError, match="missing trigger"):
        SwapCandidate(name="x", gives="y", costs="z", trigger="", measured_by="m")
