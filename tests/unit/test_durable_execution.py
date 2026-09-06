"""The durable execution runtime: where a job lands, what installs it, and what a run may save.

Three things are under test here and they share one failure. A parse fetched into a slot a
fifth its size, a queue installed with row-level security off, and a checkpoint holding the
passages a run retrieved are all states in which every process reports itself healthy. None
of them raises, none of them appears in a metric, and each is discovered by the thing it was
meant to prevent.

No task ids. `brain.ops.queue`, `brain.ops.worker` and `brain.ops.checkpoints` claim none of
M32.4.1.1, .2 or .4 between them: there is no queue driver in `uv.lock`, no graph is built,
and neither worker container has ever been started. These tests exist because the decisions
are real where the components are not, and because two of the three checks below stop a
container today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.db import SCHEMAS
from brain.gate.context import TrafficClass
from brain.ops.checkpoints import (
    CONTENT_CHANNELS,
    PERSISTABLE_CHANNELS,
    CheckpointerError,
    CheckpointHeader,
    channel_policy_gaps,
    checkpoint_refusals,
    may_resume,
)
from brain.ops.queue import (
    DEPLOY_PLAN,
    DRIVER_IMPORT_NAME,
    DRIVER_SCHEMA,
    MAX_ARGUMENT_CHARS,
    MIB_PER_SLOT,
    NO_DRIVER_IS_INSTALLED,
    POOLER_HOSTNAMES,
    WHOLE_CONTAINER_SLOTS,
    DeployStep,
    Job,
    QueueError,
    Shard,
    SlotClass,
    concurrency_gaps,
    deploy_plan_gaps,
    driver_is_installed,
    driver_rls_statements,
    queue_name_for,
    worker_shards,
)
from brain.ops.wiring import component
from brain.ops.worker import (
    EXIT_NO_DRIVER,
    SLOT_CLASS_ENV,
    WORKER_COMPONENT_ENV,
    declared_slot_class,
    declared_slots,
    main,
    plan_for,
    preflight,
    slot_env_name,
)

REPO = Path(__file__).resolve().parents[2]
GENERAL_WORKER_COMPOSE = "docker-compose.worker.yml"
PARSE_WORKER_COMPOSE = "docker-compose.parse-worker.yml"

#: The component the parse worker is budgeted as. Spelled here rather than imported from
#: `brain.knowledge.parse_budget`, so the arithmetic below compares this file's idea of the
#: container against the wiring's measured limit rather than against the knowledge layer's
#: idea of both.
PARSE_COMPONENT = "brain-parse-worker"


def _compose(name: str) -> dict[str, Any]:
    """One compose file, parsed. Never grepped.

    A regex over the text finds `POOL_MODE: transaction` inside a comment, and a comment
    explaining why something is not a pooler is exactly the text a grep would match.
    """
    loaded: dict[str, Any] = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    return loaded


def _environment(name: str) -> dict[str, str]:
    """A worker service's environment with the deploy-time password substituted.

    Substituted so the URLs are the ones the container receives, which is what the refusal
    functions have to be asked about. Left as `${POSTGRES_PASSWORD}` they parse as a
    hostname-free string and every connection check passes for the wrong reason.
    """
    services = _compose(name)["services"]
    raw = next(iter(services.values()))["environment"]
    return {key: str(value).replace("${POSTGRES_PASSWORD}", "pw") for key, value in raw.items()}


def _allocation(name: str) -> dict[TrafficClass, int]:
    allocation, problems = declared_slots(_environment(name))
    assert problems == (), problems
    return dict(allocation)


def _compose_files() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in REPO.glob("docker-compose*.yml")))


# ------------------------------------------------- where a job lands (M32.4.1.4)
def test_a_whole_container_job_is_not_queued_where_the_standard_worker_would_fetch_it() -> None:
    """The whole of the placement rule. A parse and an ordinary housekeeping job are both
    `TrafficClass.SYSTEM`, so before the slot class existed they shared one queue name and the
    general worker could fetch the 50 MiB PDF into its 48 MiB slot.

    Delete this and `queue_name_for` can go back to ignoring its second argument, which
    changes nothing any other test here can see: the shards still come out, the arithmetic
    still fits, and the two containers quietly drain the same queue again."""
    for traffic_class in TrafficClass:
        standard = queue_name_for(traffic_class, SlotClass.STANDARD)
        whole = queue_name_for(traffic_class, SlotClass.WHOLE_CONTAINER)

        assert standard != whole


def test_the_standard_queue_keeps_the_name_a_worker_on_the_previous_image_drains() -> None:
    """The asymmetry in `queue_name_for`, asserted rather than left as a comment. Renaming
    `system` to `system.standard` would leave a container running the previous image draining
    a queue nothing enqueues onto, which is indistinguishable from an idle queue in every
    metric there is, and it would do that to the container that was always safe to run.

    Delete this and the naming is tidied into a symmetric scheme, which reads better and
    strands whichever worker has not been redeployed yet."""
    for traffic_class in TrafficClass:
        assert queue_name_for(traffic_class) == traffic_class.value
        assert queue_name_for(traffic_class, SlotClass.STANDARD) == traffic_class.value


def test_a_shard_cannot_be_given_a_queue_name_nobody_derived() -> None:
    """The guard behind `queue_name_for`. Deriving the name in one function and letting a
    caller pass any string beside it enforces the rule only where somebody remembered to use
    the function, and a shard is the one place a queue name is written down for a process to
    drain.

    Delete this and a hand-written queue name puts a whole-container process on the standard
    queue, which is the original defect reintroduced one argument at a time."""
    with pytest.raises(QueueError, match="not the name"):
        Shard(
            queue="system",
            traffic_class=TrafficClass.SYSTEM,
            concurrency=1,
            slot_class=SlotClass.WHOLE_CONTAINER,
        )


def test_a_container_serving_whole_container_slots_may_hold_exactly_one() -> None:
    """A whole-container slot is the whole of what is left after the container's overhead, so
    two of them promise the same memory twice and none of them is a container that drains
    nothing while reporting itself up. `brain.knowledge.parse_budget.PARSES_AT_ONCE` reaches
    the same number from the size of the budget; this is the queue's half, which is how many
    slots the allocation may name.

    Delete this and the whole-container branch of `concurrency_gaps` accepts any number,
    which on this container means a second parse allowed the whole of a container that has
    one copy of it."""
    one = {t: (WHOLE_CONTAINER_SLOTS if t is TrafficClass.SYSTEM else 0) for t in TrafficClass}
    two = {**one, TrafficClass.SYSTEM: 2}
    none = {**one, TrafficClass.SYSTEM: 0}
    kwargs = {"worker_component": PARSE_COMPONENT, "slot_class": SlotClass.WHOLE_CONTAINER}

    assert concurrency_gaps(one, **kwargs) == ()  # type: ignore[arg-type]
    assert concurrency_gaps(two, **kwargs)  # type: ignore[arg-type]
    assert concurrency_gaps(none, **kwargs)  # type: ignore[arg-type]


def test_a_whole_container_allocation_is_not_priced_as_though_it_were_a_standard_slot() -> None:
    """The arithmetic that hid the defect. Two parses at `MIB_PER_SLOT` is 96 MiB against a
    512 MiB container, so the standard sum says a second parse fits comfortably. It does not:
    one parse is the whole container. The point of a per-class slot cost is that the sum stops
    being the question.

    Asserted against the container's measured limit rather than against `MIB_PER_SLOT`
    alone, so the test states the property that makes the refusal right rather than restating
    the constant the code already holds.

    Delete this and `concurrency_gaps` can price whole-container slots with the standard
    figure, which is green for every allocation the parse worker could be given."""
    two = {t: (2 if t is TrafficClass.SYSTEM else 0) for t in TrafficClass}
    limit = component(PARSE_COMPONENT).memory_mib

    assert limit > 2 * MIB_PER_SLOT, (
        "the standard sum has to look comfortable, or nothing is proved"
    )
    assert concurrency_gaps(
        two, worker_component=PARSE_COMPONENT, slot_class=SlotClass.WHOLE_CONTAINER
    )


def test_the_two_deployed_workers_do_not_drain_a_single_queue_between_them() -> None:
    """The live defect, asserted against the two files that would be deployed rather than
    against the rule. Both containers run the same image and the same command and were both
    allocated a `system` slot, so either could fetch a parse, and the general worker's slot is
    a fifth the size of the job.

    This is the test that fails on the tree as it was: with no `BRAIN_WORKER_SLOT_CLASS` both
    services derive `system` and the intersection is non-empty.

    Delete this and the two compose files can drift back into overlap in one line, in a diff
    that looks like a variable being tidied away."""
    general = worker_shards(
        _allocation(GENERAL_WORKER_COMPOSE),
        declared_slot_class(_environment(GENERAL_WORKER_COMPOSE))[0],
    )
    parse = worker_shards(
        _allocation(PARSE_WORKER_COMPOSE),
        declared_slot_class(_environment(PARSE_WORKER_COMPOSE))[0],
    )

    assert {s.queue for s in general} & {s.queue for s in parse} == set()
    assert {s.queue for s in parse}, "a parse worker draining nothing proves nothing"


def test_a_container_sized_for_a_parse_and_draining_the_cheap_queues_refuses_to_start() -> None:
    """Getting one of the two variables right is worse than getting both wrong: a container
    with the parse worker's 512 MiB and the general worker's queues reports a healthy fleet
    while the parse it exists for is fetched by a container a fifth its size.

    Delete this and the pairing check can be removed from the preflight with every other test
    in this file still green, because every other test asks the two functions separately."""
    findings = preflight(
        {**_environment(PARSE_WORKER_COMPOSE), SLOT_CLASS_ENV: SlotClass.STANDARD.value}
    )

    assert any("draining the queue for the other" in f for f in findings), findings


def test_an_unreadable_slot_class_falls_back_to_the_cheap_queues_and_still_refuses() -> None:
    """Two properties, and the second is why the first is safe. A typo must not start a
    container, and while it has not started it must not have been silently promoted to the
    class that can fetch the expensive work. Falling back to standard is the direction in
    which a mistake takes only the work it can hold.

    Delete this and a mistyped value can default to whichever class was written last, which
    on the parse worker is the one that fetches a job five times its slot."""
    env = {**_environment(GENERAL_WORKER_COMPOSE), SLOT_CLASS_ENV: "whole-container"}

    assert declared_slot_class(env)[0] is SlotClass.STANDARD
    assert any("is not a slot class" in f for f in preflight(env)), preflight(env)


def test_the_deployed_general_worker_still_satisfies_the_preflight_after_the_new_variable() -> None:
    """The positive sibling of every refusal above. A preflight tested only by what it refuses
    is satisfied by one that refuses everything, and a slot-class check that refused every
    container would be invisible until a deploy.

    Delete this and the new variable can be given a value nothing accepts, with every refusal
    test here green."""
    assert preflight(_environment(GENERAL_WORKER_COMPOSE)) == ()
    assert preflight(_environment(PARSE_WORKER_COMPOSE)) == ()


def test_the_plan_a_whole_container_worker_prints_does_not_report_a_standard_slots_worth() -> None:
    """`slot_memory_mib` is the standard-slot sum and it understates this container by an
    order of magnitude: one slot at 48 MiB of a 512 MiB limit reads as a container eight times
    larger than it needs to be, and an operator who believed it would shrink it.

    Delete this and the misleading line comes back, which is the line
    `brain.knowledge.parse_budget.parse_budget_note` was written to correct after the fact."""
    described = plan_for(
        _allocation(PARSE_WORKER_COMPOSE),
        worker_component=PARSE_COMPONENT,
        slot_class=SlotClass.WHOLE_CONTAINER,
    ).describe()

    assert f"{MIB_PER_SLOT} MiB of" not in described
    assert SlotClass.WHOLE_CONTAINER.value in described


# --------------------------------------------- the connection the worker actually gets
def test_every_pool_this_stack_deploys_is_named_in_the_constant_that_refuses_one() -> None:
    """`POOLER_HOSTNAMES` is the whole of the hostname half of the pooler rule, and it is a
    hand-written set of service names. Rename the pgbouncer service and every existing test
    stays green while the check stops matching anything: the URLs in those tests spell
    `pgbouncer` themselves, so they compare the constant against a copy of itself.

    Asserted against the compose files instead, which is the thing outside the constant that
    decides what a hostname resolves to on the container network.

    Delete this and the guard can be turned into a set of names nothing deploys, which refuses
    nothing and reports nothing."""
    deployed = {
        name
        for compose in _compose_files()
        for name, body in (_compose(compose).get("services") or {}).items()
        if "POOL_MODE" in (body.get("environment") or {})
    }

    assert deployed, "no pooler was found at all, so this test is comparing nothing"
    assert deployed <= POOLER_HOSTNAMES, sorted(deployed - POOLER_HOSTNAMES)


def test_no_pool_in_this_repository_is_in_the_session_mode_a_listener_would_need() -> None:
    """The premise worth checking rather than repeating. A queue can live behind a pooler in
    session mode, and this repository has none: every pool it deploys is in transaction mode,
    which is why the worker goes straight to Postgres instead.

    Delete this and a pool could be switched to session mode, which would make
    `pooler_url_findings` refuse a connection that had become safe, and nothing would say
    that the reason in the message had stopped being true."""
    modes = {
        (compose, name): (body.get("environment") or {})["POOL_MODE"]
        for compose in _compose_files()
        for name, body in (_compose(compose).get("services") or {}).items()
        if "POOL_MODE" in (body.get("environment") or {})
    }

    assert modes
    assert set(modes.values()) == {"transaction"}, modes


def test_both_workers_reach_postgres_without_passing_through_a_pool_at_all() -> None:
    """What the worker actually gets, asserted from the deployment rather than assumed from a
    docstring. `QUEUE_URL` names the database service directly on both worker containers, so
    the LISTEN a driver would take is on a backend nothing moves.

    Asserted by naming the host and checking it against the set of services that are pools,
    rather than by checking the string is not `pgbouncer`: the second passes the moment
    somebody adds a second pool under another name.

    Delete this and either worker can be pointed at the pooler by one edit, which is the
    failure with no error message anywhere."""
    pools = {
        name
        for compose in _compose_files()
        for name, body in (_compose(compose).get("services") or {}).items()
        if "POOL_MODE" in (body.get("environment") or {})
    }

    for compose in (GENERAL_WORKER_COMPOSE, PARSE_WORKER_COMPOSE):
        env = _environment(compose)
        queue_host = env["QUEUE_URL"].split("@")[1].split(":")[0]
        app_host = env["DATABASE_URL"].split("@")[1].split(":")[0]

        assert queue_host not in pools, f"{compose}: the queue goes through {queue_host}"
        assert app_host in pools, f"{compose}: the application no longer goes through a pool"


# ------------------------------------------------- what installs the queue (M32.4.1.1)
def test_the_row_level_security_step_comes_after_the_tables_it_acts_on_exist() -> None:
    """The ordering that cannot be fixed by a migration: the driver's tables do not exist
    until its own command has run, so nothing earlier in the deploy can enable row-level
    security on them.

    Delete this and the plan can be reordered into something that reads tidier, runs cleanly
    and enables nothing, because the ALTER has no table to name."""
    assert deploy_plan_gaps() == ()

    ddl = next(s for s in DEPLOY_PLAN if DRIVER_IMPORT_NAME in s.what and "schema" in s.what)
    rls = next(s for s in DEPLOY_PLAN if "ROW LEVEL SECURITY" in s.what)

    assert rls.order > ddl.order


def test_a_plan_that_secures_the_tables_before_creating_them_is_reported() -> None:
    """The check has to fail when it should. Delete this and `deploy_plan_gaps` could return
    an empty tuple unconditionally and the test above would still be green."""
    reversed_plan = (
        DeployStep(order=1, what="ALTER TABLE x ENABLE ROW LEVEL SECURITY", why="because"),
        DeployStep(order=2, what=f"{DRIVER_IMPORT_NAME} schema --apply", why="because"),
    )

    assert any("nothing to enable it on" in g for g in deploy_plan_gaps(reversed_plan))


def test_the_step_that_enables_row_level_security_may_not_be_marked_optional() -> None:
    """Every other step in this plan fails loudly when it is skipped. Skipping this one
    produces a working queue whose tables are readable by any role that reaches the database,
    and the only thing that would say so is `sweep_rls`, whose remedy is this step.

    Delete this and it can be marked optional in one keyword, in an edit that reads as
    acknowledging that a schema sometimes already has it."""
    optional = (
        DeployStep(order=1, what=f"{DRIVER_IMPORT_NAME} schema --apply", why="because"),
        DeployStep(
            order=2,
            what="ALTER TABLE x ENABLE ROW LEVEL SECURITY",
            why="because",
            optional=True,
        ),
    )

    gaps = deploy_plan_gaps(optional)

    assert len(gaps) == 1, gaps
    assert "tables nothing protects" in gaps[0]
    assert not any(s.optional for s in DEPLOY_PLAN if "ROW LEVEL SECURITY" in s.what)


def test_only_the_step_that_creates_the_schema_may_be_skipped() -> None:
    """Written because a mutation survived. Marking the driver's own command optional was
    caught by nothing: the security-step check names one step, and `DeployStep.optional` said
    in a docstring that only the schema-creation step may take it, which is a claim nothing
    enforced. A skippable "apply the schema" step leaves a queue with no tables at all.

    Delete this and any step in the plan can be marked optional, which is one keyword and
    reads as an operator being given the benefit of the doubt."""
    skippable_ddl = (
        DeployStep(
            order=1, what=f"{DRIVER_IMPORT_NAME} schema --apply", why="because", optional=True
        ),
        DeployStep(order=2, what="ALTER TABLE x ENABLE ROW LEVEL SECURITY", why="because"),
    )

    gaps = deploy_plan_gaps(skippable_ddl)

    assert len(gaps) == 1, gaps
    assert "no tables at all" in gaps[0]
    assert [s.order for s in DEPLOY_PLAN if s.optional] == [
        s.order for s in DEPLOY_PLAN if "CREATE SCHEMA" in s.what
    ]


def test_a_plan_with_no_row_level_security_step_at_all_is_reported() -> None:
    """The gap the plan exists to close. A runbook that stops after the driver's own command
    leaves tables with row-level security off in a schema the sweep enumerates, so the deploy
    succeeds and the next sweep is red with nothing beside it saying what to run.

    Delete this and the step can be removed entirely, which the ordering check above would
    not notice because there is then nothing to order."""
    assert any(
        "no step enables row-level security" in g
        for g in deploy_plan_gaps(
            (DeployStep(order=1, what=f"{DRIVER_IMPORT_NAME} schema --apply", why="because"),)
        )
    )


def test_the_security_statements_are_built_from_the_tables_that_are_there() -> None:
    """A transcribed list of the driver's tables forks at the driver's next release exactly as
    a transcribed CREATE would, and it fails in the quietest direction: a table the driver
    added that the list does not name is a table with no row-level security.

    Delete this and `driver_rls_statements` can hold its own list, which is green on the day
    it is written and wrong from the first upgrade."""
    statements = driver_rls_statements(["procrastinate_jobs", "procrastinate_events"])

    assert statements == (
        f"ALTER TABLE {DRIVER_SCHEMA}.procrastinate_jobs ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {DRIVER_SCHEMA}.procrastinate_events ENABLE ROW LEVEL SECURITY",
    )
    assert driver_rls_statements([]) == ()


def test_no_statement_grants_back_what_enabling_row_level_security_denied() -> None:
    """The reason there is no policy here and there is one in every migration. `USING (true)`
    reads as thoroughness and grants everything straight back, leaving a green sweep over a
    table nothing protects: a check that passes while checking nothing, which is the failure
    this repository keeps finding.

    Delete this and a policy is added the first time somebody cannot select from a queue table
    in psql, and the sweep goes on saying the same thing it said before."""
    for statement in driver_rls_statements(["procrastinate_jobs"]):
        assert "CREATE POLICY" not in statement
        assert "USING" not in statement


def test_a_table_name_that_is_not_a_bare_identifier_never_reaches_the_ddl() -> None:
    """DDL takes no parameters, so this builds SQL by interpolation. The names come from the
    catalogue of a database we have just written to, which is not a reason to skip the check:
    the cost of being wrong is arbitrary DDL and the cost of the check is a regular expression.

    Delete this and the interpolation is unguarded, which is the shape of a finding a security
    review writes up whether or not the source is hostile."""
    with pytest.raises(QueueError, match="bare identifier"):
        driver_rls_statements(["jobs; DROP SCHEMA ops CASCADE"])


def test_the_deploy_plan_installs_into_a_schema_the_security_sweep_enumerates() -> None:
    """The plan and `DRIVER_SCHEMA` have to agree, and the statements have to name the same
    schema the driver's command puts the tables in. A plan that created the tables in one
    schema and secured another would run cleanly and secure nothing.

    Delete this and the two can drift, which produces a deploy where every step succeeds."""
    assert DRIVER_SCHEMA in SCHEMAS
    create = next(s for s in DEPLOY_PLAN if "CREATE SCHEMA" in s.what)
    apply = next(s for s in DEPLOY_PLAN if DRIVER_IMPORT_NAME in s.what and "schema" in s.what)

    assert DRIVER_SCHEMA in create.what
    assert f"search_path={DRIVER_SCHEMA}" in apply.what
    assert driver_rls_statements(["t"])[0].startswith(f"ALTER TABLE {DRIVER_SCHEMA}.")


def test_a_deploy_step_with_no_reason_cannot_be_recorded() -> None:
    """`why` says what is still broken before this step rather than what the step does. A
    runbook whose steps describe themselves is a runbook whose steps get skipped when they
    look redundant, and the one that looks most redundant here is the last.

    Delete this and the next step is added with a command and no argument."""
    with pytest.raises(QueueError, match="has no why"):
        DeployStep(order=1, what="something", why="  ")


def test_the_absence_of_a_driver_is_asked_rather_than_asserted() -> None:
    """The run mode printed "no queue driver is installed" unconditionally, which was true and
    would have gone on being printed on the first day it was false. A sentence that cannot
    stop being said is not a report.

    Asserted in both directions: the fact today, and that the plan's first step reports the
    same fact rather than carrying a hand-written status.

    Delete this and the answer goes back to being a constant, and the day the dependency lands
    the deploy plan says it has not."""
    assert driver_is_installed() is False
    assert DRIVER_IMPORT_NAME in DEPLOY_PLAN[0].what


def test_the_deploy_plan_mode_prints_the_step_that_has_not_been_done(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mode exists because the steps existed nowhere: not in a runbook, not in a comment,
    and not in `ops/`. An operator asking what installs the queue is usually asking because it
    is not installed, which is the state in which every other mode of this process refuses, so
    this one answers before the preflight and without an environment.

    Delete this and the plan is data nothing prints, which is this repository's most common
    defect wearing a docstring."""
    assert main(["--deploy-plan"], env={}) == 0
    printed = capsys.readouterr().out

    assert "NOT DONE" in printed
    assert all(str(step.order) in printed for step in DEPLOY_PLAN)
    assert "ENABLE ROW LEVEL SECURITY" in printed


def test_the_preflight_surfaces_a_broken_deploy_plan_rather_than_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written because a mutation survived. `deploy_plan_gaps` reads a constant, so on today's
    plan it has nothing to say and removing the call from the preflight changes nothing
    observable, which is exactly the shape of a check somebody deletes as dead code. Patched
    to speak so the wiring can be seen rather than assumed, in the same way `test_worker.py`
    does for `driver_schema_gaps`.

    Delete this and the deploy plan can stop being checked at the one place a container reads
    it, with every other test in this file green."""
    monkeypatch.setattr("brain.ops.worker.deploy_plan_gaps", lambda: ("the plan is out of order",))

    assert "the plan is out of order" in preflight(_environment(GENERAL_WORKER_COMPOSE))


def test_the_run_mode_stops_saying_the_driver_is_missing_once_it_is_not(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of asking rather than asserting, and the half no test could otherwise
    reach: the branch only differs on a machine where the dependency is installed, which is
    the thing M32.4.1.1 needs and does not have.

    It still exits 69, because installing the dependency does not implement `QueueDriver`.
    The two states are different sentences to whoever is paged: one says the build is missing
    a package and the other says the code is missing an implementation.

    Delete this and the run mode goes back to printing one sentence whatever is true, which
    is what it did before and which nothing could have caught."""
    monkeypatch.setattr("brain.ops.worker.driver_is_installed", lambda: True)

    assert main([], env=_environment(GENERAL_WORKER_COMPOSE)) == EXIT_NO_DRIVER
    installed = capsys.readouterr().err

    monkeypatch.setattr("brain.ops.worker.driver_is_installed", lambda: False)
    assert main([], env=_environment(GENERAL_WORKER_COMPOSE)) == EXIT_NO_DRIVER
    absent = capsys.readouterr().err

    assert NO_DRIVER_IS_INSTALLED in absent
    assert NO_DRIVER_IS_INSTALLED not in installed
    assert "nothing implements" in installed


# ----------------------------------------------- what a run may save (M32.4.1.2)
def test_the_passages_a_run_retrieved_may_not_be_written_to_the_checkpoint_store() -> None:
    """The whole of the checkpointer's data argument. A checkpoint that carried retrieved rows
    would be a copy of business data in a store with its own retention and no row-level
    security, and it would be a widening no re-resolution could undo: the rows are already in
    the state, so a resumed attempt would never ask the gate for them again.

    Delete this and the first graph to save its working state puts every retrieved record in a
    table nobody redacts, and no test anywhere would fail."""
    refusals = checkpoint_refusals({"passages": "the client's contract value is 40000"})

    assert refusals
    assert any("not one a checkpoint may hold" in r for r in refusals)


def test_a_channel_nobody_declared_is_refused_whether_or_not_it_holds_anything() -> None:
    """Default-deny, in the shape `brain.core.field_policy` and
    `brain.ops.tracing.SAFE_ATTRIBUTES` both use. The failure is a graph author adding a
    working field and the store keeping it, and asking about the value first would teach them
    that emptying the field is the fix.

    Delete this and an undeclared channel passes whenever it happens to be short, which is
    every channel on the run that has not got anywhere yet."""
    assert checkpoint_refusals({"scratch": ""})
    assert checkpoint_refusals({"scratch": "x"})


def test_a_checkpoint_value_is_refused_at_the_bound_a_job_argument_is_refused_at() -> None:
    """A queue row and a checkpoint row are the same question asked twice: what may be written
    to a store the redactor does not reach. Two numbers for it would drift towards whichever
    store somebody was debugging, and the looser one would then be the one that matters.

    Asserted by putting the same string through both boundaries rather than by comparing the
    two constants, because comparing them passes whether or not either is read.

    Delete this and `brain.ops.checkpoints` grows a bound of its own, which is a second
    copy of a rule this repository has already argued about in three places."""
    at_the_bound = "x" * MAX_ARGUMENT_CHARS
    over_it = "x" * (MAX_ARGUMENT_CHARS + 1)

    assert checkpoint_refusals({"question_id": at_the_bound}) == ()
    Job(task="t", traffic_class=TrafficClass.SYSTEM, args={"question_id": at_the_bound})

    assert any(
        "content rather than a reference" in r
        for r in checkpoint_refusals({"question_id": over_it})
    )
    with pytest.raises(QueueError, match="content rather than a reference"):
        Job(task="t", traffic_class=TrafficClass.SYSTEM, args={"question_id": over_it})


def test_a_declared_channel_holding_a_container_is_refused_however_short_it_is() -> None:
    """The rule `brain.ops.tracing._keep` reached from the other side: a container under a
    declared name is where somebody puts a record while meaning to put a summary, and the
    passages a run retrieved are a list. A length check alone lets a list of three record
    identifiers through, and the fourth version of that list holds the records.

    Delete this and `record_refs` becomes the channel every payload arrives in."""
    assert any("record_refs" in r for r in checkpoint_refusals({"record_refs": ["c_1", "c_2"]}))
    assert checkpoint_refusals({"record_refs": "c_1,c_2"}) == ()


def test_a_checkpoint_of_references_is_written_without_comment() -> None:
    """The positive sibling of every refusal above. A boundary tested only by what it refuses
    is satisfied by one that refuses everything, and that boundary would make a durable graph
    unable to save at all while every refusal test here stayed green.

    Delete this and `checkpoint_refusals` can return a finding unconditionally."""
    assert (
        checkpoint_refusals({"run_id": "r_1", "principal_id": "p_1", "node": "retrieve", "step": 3})
        == ()
    )


def test_no_channel_a_graph_may_save_is_one_that_holds_a_copy_of_an_answer() -> None:
    """The allowlist is the whole of the protection, so the thing that has to be checked is
    the allowlist itself. Adding `passages` to it is one line, reads as making the saver work,
    and moves every retrieved row into the store.

    Delete this and the set can be widened during a debugging session, and the refusal tests
    above all keep passing because they ask about a channel that is still not on it."""
    assert channel_policy_gaps() == ()
    assert not PERSISTABLE_CHANNELS & CONTENT_CHANNELS


def test_an_allowlist_that_has_grown_a_content_channel_is_reported() -> None:
    """The check has to fail when it should. Delete this and `channel_policy_gaps` could
    return an empty tuple unconditionally and the test above would still be green."""
    gaps = channel_policy_gaps({*PERSISTABLE_CHANNELS, "passages"})

    assert any("copy of what somebody was allowed to see" in g for g in gaps)


def test_an_empty_allowlist_is_reported_rather_than_read_as_maximum_safety() -> None:
    """An allowlist of nothing is not the safest configuration, it is a checkpointer that is
    configured, connected and unable to resume anything, which presents as a durable graph
    that silently is not one.

    Delete this and emptying the set reads as tightening it."""
    assert any("save nothing" in g for g in channel_policy_gaps(()))


def test_a_worker_whose_checkpoint_allowlist_has_drifted_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`channel_policy_gaps` reads a constant, so on today's value it has nothing to say and
    removing the call from the preflight would change nothing observable. That is the shape of
    a check somebody deletes as dead code, and the day it would have mattered is the day the
    constant moved. Patched to speak, so the wiring is seen rather than assumed.

    Asked of the deployed environment rather than of one built here, because the general
    worker's compose file is where a checkpointer URL is actually set, and the point is that
    the container that would be deployed is the container that is asked.

    Delete this and the one call site this pair of functions has can be removed silently."""
    monkeypatch.setattr("brain.ops.worker.channel_policy_gaps", lambda: ("the allowlist moved",))

    assert "the allowlist moved" in preflight(_environment(GENERAL_WORKER_COMPOSE))


def test_a_worker_with_no_checkpointer_is_not_asked_about_a_channel_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The condition on the call. An install with no durable graph has no allowlist to have
    drifted, and reporting a channel policy at a container that saves nothing puts a line
    nobody can act on into a list whose whole value is that every line names a fix.

    The parse worker is that container rather than a constructed one: it sets no
    `BRAIN_CHECKPOINTER_URL`, because a parse is not a graph, so the pair of tests reads the
    two deployments that exist rather than one deployment and one hypothesis.

    Delete this and the check fires on every worker, including a container that saves
    nothing and could do nothing about the finding."""
    monkeypatch.setattr("brain.ops.worker.channel_policy_gaps", lambda: ("the allowlist moved",))
    env = _environment(PARSE_WORKER_COMPOSE)

    assert "BRAIN_CHECKPOINTER_URL" not in env
    assert preflight(env) == ()


def test_a_saved_run_may_only_be_resumed_by_the_principal_it_was_started_for() -> None:
    """A checkpoint is not a bearer token and it is not a record either. The state inside it
    was assembled under one person's reach, and a second person with a wider reach still did
    not ask the question, so admitting them would hand somebody an answer composed for
    somebody else.

    Delete this and a resume becomes reachable by whoever holds the run id, which is a
    capability handed to whoever the reference was forwarded to."""
    header = CheckpointHeader(run_id="r_1", principal_id="p_1")

    assert may_resume(header, "p_1") is True
    assert may_resume(header, "p_2") is False
    assert may_resume(header, "") is False


def test_a_saved_run_that_names_no_owner_cannot_be_constructed() -> None:
    """The guard behind the check above. An empty string is a valid value for a string field,
    and a header whose owner is empty would be resumable by anybody who also passed an empty
    string, which is what a missing header field looks like on the way in.

    Delete this and the ownership check is satisfied by two blanks agreeing."""
    with pytest.raises(CheckpointerError, match="no principal_id"):
        CheckpointHeader(run_id="r_1", principal_id="   ")
    with pytest.raises(CheckpointerError, match="no run_id"):
        CheckpointHeader(run_id="", principal_id="p_1")


# ------------------------------------------------------------------ the deployment
def test_both_worker_containers_say_which_slot_class_they_serve() -> None:
    """The two files run the same image and the same command and differ in two variables. A
    difference expressed as one file saying something and the other saying nothing is a
    difference nobody sees in a diff, and the default is the value that would be assumed.

    Delete this and the general worker's declaration is removed as redundant, which leaves the
    only visible statement of the rule on the container that is not deployed today."""
    for compose, expected in (
        (GENERAL_WORKER_COMPOSE, SlotClass.STANDARD),
        (PARSE_WORKER_COMPOSE, SlotClass.WHOLE_CONTAINER),
    ):
        env = _environment(compose)

        assert env[SLOT_CLASS_ENV] == expected.value
        assert declared_slot_class(env) == (expected, ())


def test_the_parse_worker_names_both_the_component_it_is_and_the_slots_it_serves() -> None:
    """Getting one of the two right is the state this whole section is about: a container
    sized for a parse, drained from the queue for ordinary housekeeping. The pairing is what
    `preflight` checks, and it can only check what the file declares.

    Delete this and one of the two variables can be dropped, and the remaining one reads as
    though it settled both questions."""
    env = _environment(PARSE_WORKER_COMPOSE)

    assert env[WORKER_COMPONENT_ENV] == PARSE_COMPONENT
    assert env[SLOT_CLASS_ENV] == SlotClass.WHOLE_CONTAINER.value
    assert env[slot_env_name(TrafficClass.SYSTEM)] == str(WHOLE_CONTAINER_SLOTS)
