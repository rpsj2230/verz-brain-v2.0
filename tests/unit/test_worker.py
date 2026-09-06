"""The worker: what it refuses to start with, how it lays its processes out, and its sizing.

Every test here is about a container that would come up, hold a connection, and report
itself healthy while draining nothing.

The queue-side tests for `Shard`, `worker_shards` and `driver_schema_gaps` are here rather
than in `tests/unit/test_queue.py` because those functions exist for this deployment: they
are the translation from a per-class allocation into processes, and the thing that consumes
them is `brain.ops.worker`. Keeping them beside their consumer also means this change touches
one fewer file that other work is in.

No task ids. `brain.ops.worker` and `docker-compose.worker.yml` claim none: the container has
never been started, because the process it runs has no queue driver to fetch with. M32.4.1.4
is served rather than closed, on the same grounds `docker-compose.langfuse.yml` refuses
M32.1.1.1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from brain.db import SCHEMAS
from brain.gate.context import TrafficClass
from brain.ops.checkpoints import connection_refusals
from brain.ops.queue import (
    CONCURRENCY,
    DRIVER_SCHEMA,
    FALLBACK_POLL_SECONDS,
    HEARTBEAT_SECONDS,
    MIB_PER_SLOT,
    QueueError,
    Shard,
    driver_schema_gaps,
    queue_name_for,
    queue_url_refusals,
    stale_after,
    worker_shards,
)
from brain.ops.wiring import component
from brain.ops.worker import (
    EXIT_MISCONFIGURED,
    EXIT_NO_DRIVER,
    EXIT_NOT_READY,
    declared_slots,
    is_ready,
    main,
    plan_for,
    preflight,
    slot_env_name,
)

REPO = Path(__file__).resolve().parents[2]
COMPOSE = "docker-compose.worker.yml"

NOW = datetime(2026, 9, 6, 14, 0, tzinfo=UTC)


def _compose_service() -> dict[str, object]:
    """The worker service as compose would read it. Parsed, never grepped.

    A regex over the text finds `memory: 384M` inside a comment, which is precisely the state
    a half-finished edit leaves this file in.
    """
    raw = yaml.safe_load((REPO / COMPOSE).read_text(encoding="utf-8"))
    service: dict[str, object] = raw["services"]["brain-worker"]
    return service


def _worker_environment() -> dict[str, str]:
    """The service's environment with the deploy-time password substituted.

    Substituted rather than left as `${POSTGRES_PASSWORD}`, so the URLs below are the URLs
    the container actually gets and the refusal functions are asked the real question.
    """
    raw = _compose_service()["environment"]
    assert isinstance(raw, dict)
    return {key: str(value).replace("${POSTGRES_PASSWORD}", "pw") for key, value in raw.items()}


def _sound_environment(**overrides: str) -> dict[str, str]:
    """An environment a worker would start on, before the override under test."""
    env = {
        "QUEUE_URL": "postgresql+psycopg://brain:pw@db:5432/brain",
        "DATABASE_URL": "postgresql+psycopg://brain:pw@pgbouncer:5432/brain",
        **{slot_env_name(t): str(v) for t, v in CONCURRENCY.items()},
    }
    env.update(overrides)
    return env


# --------------------------------------------------- what the worker refuses to start on
def test_a_worker_with_no_queue_url_refuses_to_start() -> None:
    """A worker with no connection of its own is a worker somebody is about to give
    `DATABASE_URL` to, which points it at the transaction pooler. Delete this and the
    absence is discovered by a queue that reports itself empty for a week."""
    findings = preflight(_sound_environment(QUEUE_URL=""))

    assert any("QUEUE_URL is not set" in f for f in findings)


def test_a_worker_handed_the_applications_own_connection_string_refuses_to_start() -> None:
    """The mistake that is easy to write and impossible to see, checked where it can actually
    stop a container. `queue_url_refusals` has existed and been tested since the queue was
    written, and nothing called it; this is its call site. Delete this and it goes back to
    being a mechanism nobody runs."""
    env = _sound_environment()
    findings = preflight(_sound_environment(QUEUE_URL=env["DATABASE_URL"]))

    assert any("application's own connection string" in f for f in findings)


def test_a_worker_whose_checkpointer_is_behind_the_pooler_refuses_to_start() -> None:
    """A different failure from the queue's and it needs its own check: the saver prepares
    statements server-side, and a pooler hands the next one to a backend that never saw the
    prepare. Delete this and only the queue half of the connection rule is enforced, on a
    process that holds both."""
    findings = preflight(
        _sound_environment(BRAIN_CHECKPOINTER_URL="postgresql+psycopg://brain:pw@pgbouncer:5432/x")
    )

    assert any("transaction pooler" in f for f in findings)


def test_a_worker_with_no_checkpointer_at_all_is_not_a_misconfiguration() -> None:
    """An install with no durable graph has no checkpointer, and refusing that would make
    every worker deployment carry a variable for a component that does not exist. A wrong
    checkpointer is a refusal; an absent one is not. Delete this and the two collapse, which
    blocks the only deployment shape currently possible."""
    assert preflight(_sound_environment()) == ()


def test_the_preflight_surfaces_a_queue_schema_gap_rather_than_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`driver_schema_gaps` reads a constant, so on today's value it has nothing to say and
    removing the call would change nothing observable. That is exactly the shape of a check
    somebody deletes as dead code, and the day it would have mattered is the day the constant
    moved. Patched to speak so the wiring can be seen rather than assumed.

    Delete this and the preflight can stop asking, with every other test here green."""
    monkeypatch.setattr("brain.ops.worker.driver_schema_gaps", lambda: ("the schema moved",))

    assert "the schema moved" in preflight(_sound_environment())


def test_a_correctly_configured_worker_reports_nothing_to_fix() -> None:
    """The positive sibling of every refusal above. A preflight tested only by what it
    refuses is satisfied by one that refuses everything, and that worker never starts on any
    configuration while every refusal test stays green."""
    env = _sound_environment(BRAIN_CHECKPOINTER_URL="postgresql+psycopg://brain:pw@db:5432/brain")

    assert preflight(env) == ()


def test_a_slot_count_that_is_not_a_number_is_reported_rather_than_ignored() -> None:
    """A typo in one variable would otherwise leave that class out of the mapping entirely,
    which is reported as a class whose jobs are never fetched: the right alarm for the wrong
    reason, and it sends whoever reads it to the wrong file. Delete this and a mistyped slot
    count reads as a missing allocation."""
    findings = preflight(_sound_environment(BRAIN_WORKER_SLOTS_HUMAN_ASYNC="four"))

    assert any("is not a number of slots" in f for f in findings)


def test_a_negative_slot_count_is_refused() -> None:
    """Zero is how a class is declared undrained and it is a decision. A negative number is
    not a smaller version of that, it is a value nothing can act on, and it would make the
    memory arithmetic below understate the total. Delete this and a stray minus sign buys
    slots elsewhere."""
    findings = preflight(_sound_environment(BRAIN_WORKER_SLOTS_SYSTEM="-1"))

    assert any("negative" in f for f in findings)


def test_a_class_missing_from_the_environment_is_reported_as_never_fetched() -> None:
    """A class with no allocation presents as a queue that fills and never drains and takes a
    day to find. The environment is not defaulted back to `CONCURRENCY` when a variable is
    absent, deliberately: substituting the constant would make a deployment that forgot a
    class work here and fail on a version whose constant differs. Delete this and it does."""
    env = _sound_environment()
    del env[slot_env_name(TrafficClass.SYSTEM)]

    assert any("never fetched" in f for f in preflight(env))


def test_an_allocation_over_the_containers_memory_limit_refuses_to_start() -> None:
    """The second cap, doing the only thing it can do. CPython has no heap ceiling, so what
    bounds this container is the number of jobs it will run at once, checked before the first
    fetch. Delete this and a backlog is fixed by raising a slot count, which converts a queue
    depth problem into a neighbour's outage on a shared host."""
    findings = preflight(_sound_environment(BRAIN_WORKER_SLOTS_HUMAN_ASYNC="100"))

    assert any("over the" in f for f in findings), findings


# --------------------------------------------------- the process layout
def test_a_class_allocated_no_slots_gets_no_worker_process() -> None:
    """`HUMAN_INTERACTIVE` is zero on purpose, and the natural loop over the mapping produces
    a worker for it: a process that holds a database connection, drains a queue nothing is
    meant to enqueue onto, and reports itself up. Delete this and the zero becomes an idle
    worker rather than an absent one."""
    shards = worker_shards()

    assert TrafficClass.HUMAN_INTERACTIVE not in {shard.traffic_class for shard in shards}
    assert {shard.traffic_class for shard in shards} == {
        t for t, slots in CONCURRENCY.items() if slots > 0
    }


def test_a_shard_with_no_slots_cannot_be_constructed() -> None:
    """The guard behind the omission above. Delete this and `worker_shards` can be changed to
    emit every class, and the idle process comes back with nothing to report it."""
    with pytest.raises(QueueError, match="still holds a connection"):
        Shard(queue="system", traffic_class=TrafficClass.SYSTEM, concurrency=0)


def test_every_drained_class_gets_a_queue_named_after_it() -> None:
    """A queue name chosen per task would let a task author choose a priority, and the thing
    that decides priority here is whether a person is waiting, which the channel declared at
    ingress with no default. Delete this and a task can promote itself into the interactive
    share by being named well."""
    for shard in worker_shards():
        assert shard.queue == queue_name_for(shard.traffic_class)
        assert shard.queue == shard.traffic_class.value


def test_the_worker_processes_come_out_in_the_same_order_every_time() -> None:
    """Ordered by the enum rather than by the mapping handed in, so two runs of a deploy
    produce the same process list and a diff of the plan is a diff of the decision rather
    than of whatever order an environment happened to be read in.

    Asserted with a mapping built in a different order, because a mapping built in enum
    order agrees with both implementations and a test over it cannot tell them apart.
    Delete this and an unchanged deployment can read as a changed one."""
    scrambled = {
        TrafficClass.SYSTEM: 1,
        TrafficClass.AUTOMATION: 2,
        TrafficClass.HUMAN_ASYNC: 4,
    }

    assert [shard.traffic_class for shard in worker_shards(scrambled)] == [
        TrafficClass.HUMAN_ASYNC,
        TrafficClass.AUTOMATION,
        TrafficClass.SYSTEM,
    ]


def test_the_slot_total_is_the_sum_of_the_shards_and_not_of_the_mapping() -> None:
    """The two differ by whatever the undrained classes were allocated, and the number that
    matters for memory is the one that has processes behind it. Delete this and the plan can
    report a budget for slots that no worker holds."""
    plan = plan_for(CONCURRENCY)

    assert plan.slots == sum(shard.concurrency for shard in plan.shards)
    assert plan.slot_memory_mib == plan.slots * MIB_PER_SLOT
    assert plan.memory_mib == component("brain-worker").memory_mib


def test_the_plan_reports_the_latency_a_lost_notification_costs() -> None:
    """The fallback poll interval is the queue's entire latency once notifications stop being
    delivered, and behind a transaction pooler that day has no error in it. Printing it at
    start means the number is already in the log during the incident rather than being looked
    up while it is happening.

    Delete this and the constant goes back to being a value in a source file that nothing
    reads, which is how it gets raised to a minute as a tidy default."""
    described = plan_for(CONCURRENCY).describe()

    assert f"{FALLBACK_POLL_SECONDS}s" in described
    assert DRIVER_SCHEMA in described


def test_a_worker_never_reports_itself_alive_more_often_than_it_looks_for_work() -> None:
    """The fallback poll interval is what the queue's latency degrades to when notifications
    stop being delivered, which behind a transaction pooler happens with no error at all. A
    poll slower than the heartbeat means the worker writes "I am alive" several times between
    looks at the queue, so a monitor reads a healthy fleet while jobs sit: which is exactly
    the failure this module is about, arrived at from the inside.

    **Added because a mutation raising the interval to a tidy minute survived every other
    test here.** The value itself is a judgement and no test can pin it without restating the
    source; its relation to the heartbeat is not a judgement, and that is what this asserts.

    Delete this and the interval can be raised to whatever makes an idle worker quiet."""
    assert 0 < FALLBACK_POLL_SECONDS <= HEARTBEAT_SECONDS


def test_the_queue_schema_is_one_the_row_level_security_sweep_enumerates() -> None:
    """`brain.ops.sweeps.sweep_rls` reads `brain.db.SCHEMAS` and looks nowhere else. The
    driver installs its own tables with no row-level security on them, so the schema is the
    difference between a red sweep somebody acts on and nothing at all.

    Delete this and `DRIVER_SCHEMA` can be set to a name that reads sensibly and is not
    enumerated, which is the same outcome as leaving it at the driver's default."""
    assert DRIVER_SCHEMA in SCHEMAS
    assert driver_schema_gaps() == ()


def test_a_queue_schema_nobody_declared_is_reported() -> None:
    """The check has to fail when it should. Delete this and `driver_schema_gaps` could
    return an empty tuple unconditionally and the test above would still be green."""
    assert any("sweep_rls" in gap for gap in driver_schema_gaps("public"))


def test_a_queue_with_no_schema_at_all_is_reported_separately() -> None:
    """An empty schema is not an unknown one: the tables land wherever `search_path` points,
    which on a fresh connection is `public`, so the message has to say that rather than
    listing the schemas that exist. Delete this and an unset value is reported as a typo."""
    gaps = driver_schema_gaps("  ")

    assert len(gaps) == 1
    assert "search_path" in gaps[0]


# --------------------------------------------------- readiness
def test_a_container_that_has_never_written_a_heartbeat_is_not_ready(tmp_path: Path) -> None:
    """Which is the true answer today: nothing writes the heartbeat, because there is no
    driver to fetch with. A readiness check that passed anyway would put a container that
    drains nothing into rotation. Delete this and a missing file reads as a fresh one."""
    assert is_ready(tmp_path / "absent", now=NOW) is False


def test_a_fresh_heartbeat_is_ready(tmp_path: Path) -> None:
    """The positive sibling. A readiness check that never passes is a container that is never
    in rotation, and the failure looks identical to the worker being broken. Delete this and
    `is_ready` could return False unconditionally."""
    beat = tmp_path / "heartbeat"
    beat.write_text("", encoding="utf-8")

    assert is_ready(beat, now=datetime.now(tz=UTC)) is True


def test_readiness_uses_the_same_staleness_as_the_re_drive_sweep(tmp_path: Path) -> None:
    """Two thresholds for one condition produce the two states that are both wrong: a
    container reporting ready while the recovery sweep re-drives its jobs, or one taken out
    of rotation while it still holds work nothing will reclaim.

    Asserted by moving the clock rather than by comparing two constants, because a constant
    comparison passes whether or not `is_ready` reads it. Delete this and readiness grows a
    timeout of its own that looks tidier and disagrees."""
    beat = tmp_path / "heartbeat"
    beat.write_text("", encoding="utf-8")
    now = datetime.now(tz=UTC)

    assert is_ready(beat, now=now + stale_after() - timedelta(seconds=1)) is True
    assert is_ready(beat, now=now + stale_after() + timedelta(seconds=5)) is False


# --------------------------------------------------- the process refuses out loud
def test_a_misconfigured_worker_exits_with_a_configuration_code_and_not_with_one() -> None:
    """Exit 1 means everything, so it means nothing. The two ways this process refuses need
    different people: 78 says an operator wrote something wrong, 69 says the build is missing
    a dependency. Delete this and both become 1, and whoever is paged reads the log to find
    out which."""
    assert main([], env=_sound_environment(QUEUE_URL="")) == EXIT_MISCONFIGURED


def test_a_correctly_configured_worker_still_refuses_because_it_has_no_driver() -> None:
    """The honest behaviour, and the reason this leaf is not claimed. A worker that started
    against no driver would poll a queue that does not exist and report itself healthy, and
    an empty queue is indistinguishable from an absent one in every metric there is.

    Delete this and starting anyway becomes a small change with no test against it."""
    assert main([], env=_sound_environment()) == EXIT_NO_DRIVER


def test_the_check_mode_reports_a_sound_configuration_as_sound() -> None:
    """`--check` is an operator asking whether a deployment would start, and it has to be
    able to answer yes on a machine with no driver installed. Delete this and the only way to
    validate a worker's environment is to try to run it, which on this host means editing a
    compose file to find out."""
    assert main(["--check"], env=_sound_environment()) == 0


def test_the_three_exit_codes_are_three_different_numbers() -> None:
    """The property the codes exist for. Delete this and two of them can be given the same
    value in a tidy-up, which is invisible until an alert routes to the wrong person."""
    assert len({EXIT_MISCONFIGURED, EXIT_NO_DRIVER, EXIT_NOT_READY, 0}) == 4


def test_the_readiness_mode_answers_without_looking_at_the_queue_configuration() -> None:
    """A healthcheck that ran the preflight would report a misconfigured container as
    unhealthy, which is true and useless: it restarts for ever instead of exiting once with
    the reason. Delete this and readiness and configuration are answered by one command, and
    the container loops instead of failing."""
    assert main(["--ready"], env={"BRAIN_WORKER_HEARTBEAT": str(REPO / "absent")}) == EXIT_NOT_READY


# --------------------------------------------------- the deployment
def test_the_worker_container_carries_an_explicit_memory_limit() -> None:
    """The rule the whole of `brain.ops.wiring` exists for. This host runs a second production
    system belonging to the same owner, and an unlimited container is not a sizing mistake on
    a box like that, it is somebody else's outage. Delete this and the limit can be dropped in
    an edit that looks like it removes clutter."""
    limits = _compose_service()["deploy"]

    assert isinstance(limits, dict)
    assert limits["resources"]["limits"]["memory"] == "384M"


def test_the_worker_container_is_sized_as_the_component_it_is_budgeted_as() -> None:
    """The budget is arithmetic over `COMPONENTS` and the compose file is what actually runs.
    Two copies of one number is only safe while something compares them.

    Delete this and the container can be given whatever makes it start while
    `budget_breaches` keeps reporting the old figure."""
    limits = _compose_service()["deploy"]
    assert isinstance(limits, dict)
    declared = component("brain-worker").memory_mib

    assert limits["resources"]["limits"]["memory"] == f"{declared}M"


def test_the_workers_slot_budget_sits_strictly_below_its_cgroup_limit() -> None:
    """The second cap, and the mistake that makes it useless is the tempting one: raise the
    slots until they exactly fill the container so nothing is wasted.

    `MIB_PER_SLOT` counts a job's working set. The cgroup counts that plus the interpreter,
    the imports and the connection pool, so a worker allowed exactly its container's limit
    will exceed it and be killed while believing it is within budget.

    Delete this and the gap gets closed one slot at a time, each edit looking like reclaimed
    waste."""
    limits = _compose_service()["deploy"]
    assert isinstance(limits, dict)
    cgroup = int(str(limits["resources"]["limits"]["memory"]).rstrip("M"))

    assert plan_for(_declared_from_compose()).slot_memory_mib < cgroup


def _declared_from_compose() -> dict[TrafficClass, int]:
    allocation, problems = declared_slots(_worker_environment())
    assert problems == (), problems
    return dict(allocation)


def test_the_compose_file_declares_the_same_allocation_the_queue_decided() -> None:
    """`brain.ops.queue.CONCURRENCY` is where these numbers are argued and the container is
    configured by its environment, so there are two copies and something has to hold them
    equal. Delete this and the deployed allocation drifts from the one every test in
    `test_queue.py` reasons about."""
    assert _declared_from_compose() == dict(CONCURRENCY)


def test_the_compose_file_names_every_traffic_class_including_the_undrained_one() -> None:
    """An omitted class is reported as one whose jobs are never fetched, and a class
    deliberately allocated zero is a decision. The two must not look alike in a file somebody
    reads during an incident. Delete this and adding a traffic class silently strands its
    jobs on the deployed worker."""
    env = _worker_environment()

    assert all(slot_env_name(traffic_class) in env for traffic_class in TrafficClass)
    assert env[slot_env_name(TrafficClass.HUMAN_INTERACTIVE)] == "0"


def test_the_deployed_environment_satisfies_the_preflight_it_will_be_checked_by() -> None:
    """The strongest thing this file asserts: the deployment as written would start. Every
    other test here checks a rule; this one checks the artefact against all of them at once,
    which is the check that catches a rule and a file drifting apart in opposite directions.

    Delete this and the compose file can be edited into a state the worker refuses, which is
    discovered by deploying it."""
    assert preflight(_worker_environment()) == ()


def test_the_worker_is_not_pointed_at_the_pooler_by_its_own_compose_file() -> None:
    """Asserted in both directions, because the first half passes for a file whose refusal
    function does nothing. The queue URL goes straight to the database; the application's URL
    does not, and feeding it to the same check has to produce a refusal.

    Delete this and the two URLs can be made identical, which is one line and is the failure
    with no error message."""
    env = _worker_environment()

    assert queue_url_refusals(env["QUEUE_URL"], app_url=env["DATABASE_URL"]) == ()
    assert connection_refusals(env["BRAIN_CHECKPOINTER_URL"], app_url=env["DATABASE_URL"]) == ()
    assert queue_url_refusals(env["DATABASE_URL"], app_url=env["DATABASE_URL"])


def test_the_worker_runs_only_in_the_profiles_that_budget_it() -> None:
    """`brain.ops.wiring` puts `brain-worker` in `standard` and `full`, and lite is what is
    deployed today. Compose profiles are how that becomes true of the deployment rather than
    of a document. Delete this and the service can acquire a third profile, or lose the key
    entirely, which starts a worker on every install that composes this file."""
    service = _compose_service()
    profiles = service["profiles"]

    assert isinstance(profiles, list)
    assert set(profiles) == set(component("brain-worker").profiles)


def test_the_container_runs_the_module_that_refuses_rather_than_a_shell() -> None:
    """The image has no shell for its user, and the command is the preflight. A command that
    was anything else would start a process that has not been checked, on a host where the
    check is the only thing between a worker and the application's own connection string.
    Delete this and the command can become something that skips it."""
    assert _compose_service()["command"] == ["python", "-m", "brain.ops.worker"]


def test_the_healthcheck_asks_for_readiness_rather_than_liveness() -> None:
    """Liveness is free: the process is up. A worker that is up and draining nothing is the
    state this whole file is about, and a TCP or process check passes for it. Delete this and
    the healthcheck becomes something that always passes."""
    healthcheck = _compose_service()["healthcheck"]

    assert isinstance(healthcheck, dict)
    assert healthcheck["test"] == ["CMD", "python", "-m", "brain.ops.worker", "--ready"]
