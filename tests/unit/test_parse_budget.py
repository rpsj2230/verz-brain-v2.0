"""The bound on one parse, the container sized for it, and what neither of them contains.

Every test here is about a document somebody outside this company chose, and about the two
ways that ends badly: a parse that takes the container down with every other job in it, and a
door that accepts a file the parser will always refuse.

The `parse_scanned` tests live here rather than in `test_scanning.py` because the guard they
exercise is this leaf and not M7.2.5, and because the sibling file is where the ordering
property is argued and adding a memory argument to it would blur the two. What is asserted
there and not repeated here is that a parser cannot be reached with unscanned bytes at all.

**Two of these assert a relation rather than a value, on purpose.** `PARSE_WORKER_RESERVE_MIB`
and the entries in `PARSE_EXPANSION` are judgements, and a test that compared one of them with
itself would be green for every value it could hold, which is the failure the repository's own
notes record catching three authors in one afternoon. So the reserve is asserted through the
property it exists for, which is that the process-side ceiling sits strictly below the cgroup
limit, and the expansion factors through the property that a compressed container costs more
to open than a file that is already the bytes it will be read as.

Task ids: M7.2.6
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.gate.context import TrafficClass
from brain.knowledge.ingest import (
    CAUSE_TEXT,
    TYPE_LIMITS,
    AdmittedUpload,
    Container,
    MediaType,
    ParseCause,
    ParseFailure,
    ceiling_for,
)
from brain.knowledge.parse_budget import (
    MIB,
    PARSE_EXPANSION,
    PARSE_WORKER_COMPONENT,
    PARSES_AT_ONCE,
    declared_cost_bytes,
    fits_parse_budget,
    parse_budget_bytes,
    parse_budget_note,
    parse_worker_gaps,
    worst_declared_cost,
)
from brain.knowledge.scanning import (
    ParsedDocument,
    ScannedContent,
    parse_scanned,
    scan_for_parsing,
)
from brain.knowledge.uploads import ingestion_request
from brain.ops.queue import CONCURRENCY, MIB_PER_SLOT
from brain.ops.wiring import component
from brain.ops.worker import (
    WORKER_COMPONENT_ENV,
    declared_component,
    main,
    preflight,
    slot_env_name,
)
from tests.unit.test_scanning import FakeParser, FakeScanner

REPO = Path(__file__).resolve().parents[2]
COMPOSE = "docker-compose.parse-worker.yml"
GENERAL_WORKER_COMPOSE = "docker-compose.worker.yml"

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"

#: A parse worker's allocation: one parse, on the class ingestion arrives on, and an explicit
#: zero everywhere else. Written out rather than derived from the compose file, so a test that
#: is about the arithmetic does not fail because a deployment changed.
SOUND_ALLOCATION = {
    TrafficClass.HUMAN_INTERACTIVE: 0,
    TrafficClass.HUMAN_ASYNC: 0,
    TrafficClass.AUTOMATION: 0,
    TrafficClass.SYSTEM: 1,
}


def _upload(media_type: MediaType, size_bytes: int) -> AdmittedUpload:
    """An admission record of a given type and size, without the bytes behind it.

    Built directly because the functions under test read two fields off it, and producing a
    50 MiB PDF to assert something about arithmetic would spend a second of every test run to
    say nothing extra. The tests that need real bytes go through the real door.
    """
    return AdmittedUpload(
        filename="sop", media_type=media_type, size_bytes=size_bytes, digest="a" * 64
    )


def _cleared(content: bytes = PDF) -> ScannedContent:
    """Bytes through the real door and the real scan gate. There is no other way to make one."""
    from brain.knowledge.ingest import admit_upload

    upload = admit_upload(filename="sop.pdf", declared_type=MediaType.PDF.value, content=content)
    return scan_for_parsing(upload, content, scanner=FakeScanner())


def _compose_service(name: str = COMPOSE) -> dict[str, Any]:
    """The service as compose would read it. Parsed, never grepped.

    A regex over the text finds `memory: 512M` inside a comment, which is exactly the state a
    half-finished edit leaves this file in.
    """
    raw = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    services = raw["services"]
    service: dict[str, Any] = next(iter(services.values()))
    return service


def _environment(name: str = COMPOSE, **overrides: str) -> dict[str, str]:
    """The container's environment with the deploy-time password substituted.

    Substituted rather than left as `${POSTGRES_PASSWORD}`, so the connection checks in the
    preflight are asked the question the container will actually ask them.
    """
    raw = _compose_service(name)["environment"]
    assert isinstance(raw, dict)
    env = {key: str(value).replace("${POSTGRES_PASSWORD}", "pw") for key, value in raw.items()}
    env.update(overrides)
    return env


def _cgroup_limit_mib(name: str = COMPOSE) -> int:
    limits = _compose_service(name)["deploy"]["resources"]["limits"]["memory"]
    return int(str(limits).rstrip("M"))


# --------------------------------------------------------------- the two caps are two caps
def test_the_parse_budget_sits_strictly_below_the_container_limit_it_runs_under() -> None:
    """The mistake that makes a second cap useless, and it is the tempting one: give the
    process the whole container so that nothing is wasted.

    The cgroup counts the interpreter, the imports, the database connection and the copy of
    the file the scanned-content value is holding while the parser reads it. A process told it
    may spend exactly the container's limit will therefore exceed the container's limit and be
    killed while believing it is inside budget.

    Asserted as a relation rather than against `PARSE_WORKER_RESERVE_MIB`, because a test that
    imports the constant it is checking compares it with itself and stays green for every
    value it could hold, including zero. Delete this and the reserve can be reclaimed as
    waste, after which the only enforcement left is the kill."""
    limit_bytes = component(PARSE_WORKER_COMPONENT).memory_mib * MIB

    assert parse_budget_bytes() < limit_bytes
    assert parse_budget_bytes() > 0, "a container with no budget parses nothing at all"


def test_the_parse_worker_is_large_enough_for_the_largest_file_the_door_admits() -> None:
    """The relation the whole module exists to keep. `brain.knowledge.ingest.TYPE_LIMITS` is
    edited by whoever is adding a format and `brain.ops.wiring` by whoever is sizing a host,
    and neither edit mentions the other. When they disagree the symptom is a file that was
    accepted, fetched, scanned and stored, and then refused by the parser, which is the worst
    order to discover a limit in.

    Delete this and a ceiling can be raised in one file with nothing in the other noticing."""
    _, cost = worst_declared_cost()

    assert cost <= parse_budget_bytes()


def test_the_general_worker_cannot_hold_what_the_door_admits_and_the_check_says_so() -> None:
    """The finding that forced a second container, and the proof that this check can fail.

    `brain-worker` is 384 MiB across seven slots, so a job gets about 48 MiB and the door
    admits a 50 MiB PDF: a parse there cannot hold its own input. A check that only ever ran
    against the container it was written for would pass whether or not it worked, which is the
    reason `parse_worker_gaps` takes the component as a parameter at all.

    Delete this and the arithmetic could be inverted and every other test here would stay
    green, because the parse worker is sized to satisfy it either way."""
    findings = parse_worker_gaps(CONCURRENCY, worker_component="brain-worker")

    assert any("over the" in finding and "budget" in finding for finding in findings), findings


def test_a_reserve_of_nothing_turns_the_second_cap_into_a_copy_of_the_first() -> None:
    """The reserve is what makes the process-side ceiling a second cap rather than a
    restatement of the cgroup limit, and reclaiming it looks in a diff like removing a fudge
    factor. This is the check that refuses.

    Delete this and `PARSE_WORKER_RESERVE_MIB` can be set to zero, after which the process is
    told it may use everything the kernel is counting, plus the interpreter that is not in the
    figure at all."""
    findings = parse_worker_gaps(SOUND_ALLOCATION, reserve_mib=0)

    assert any("enforces by killing" in finding for finding in findings), findings


def test_a_parse_worker_allocated_more_than_one_parse_at_a_time_is_refused() -> None:
    """The budget is the whole of what is left after the reserve, so two concurrent parses are
    two times the container's memory promised out of one container's worth. Raising the slot
    count is also the obvious response to a parse backlog, which is what makes this the guard
    that gets tested rather than the one that gets remembered.

    Delete this and a queue depth problem is fixed by a number that makes the container get
    killed on a shared host, which is somebody else's outage."""
    crowded = {**SOUND_ALLOCATION, TrafficClass.SYSTEM: PARSES_AT_ONCE + 1}

    findings = parse_worker_gaps(crowded)

    assert any("at a time" in finding for finding in findings), findings


def test_a_correctly_sized_parse_worker_reports_nothing_to_fix() -> None:
    """The positive sibling of the three refusals above. A check tested only by what it
    refuses is satisfied by one that refuses everything, and that check would make the parse
    worker undeployable while every refusal test above stayed green."""
    assert parse_worker_gaps(SOUND_ALLOCATION) == ()


# --------------------------------------------------------------- what a declared cost is
def test_the_declared_cost_is_read_from_the_size_the_door_measured() -> None:
    """A declared length is a claim by whoever is uploading, and the whole of
    `uploads.read_within` exists because that claim is sometimes a lie. `AdmittedUpload.size_bytes`
    is the length of the buffer the door actually held, which is why the cost is computed from
    it and from nothing else.

    Asserted by scaling rather than against a factor imported from the module under test:
    doubling the file doubles the cost, whatever the factor is. Delete this and the cost can
    become a constant, which passes every budget check for every file."""
    small = declared_cost_bytes(_upload(MediaType.PDF, 1_000_000))
    large = declared_cost_bytes(_upload(MediaType.PDF, 2_000_000))

    assert large == small * 2
    assert small > 1_000_000, "a parse costs more than the bytes it is reading"


def test_two_files_of_one_size_cost_different_amounts_by_what_the_bytes_proved_to_be() -> None:
    """The reason the cost is a table over containers rather than one factor over sizes. A
    megabyte of markdown is a megabyte of text when it is decoded; a megabyte of Office
    document is a compressed archive whose parts are held decompressed and then held again as
    a tree. Collapsing the two into one number means the figure is either useless for the
    first or wrong for the second.

    Delete this and the lookup can be replaced by a single multiplier, or by the wrong
    container's, and every arithmetic test above still passes."""
    same_size = 1_000_000
    zipped = declared_cost_bytes(_upload(MediaType.DOCX, same_size))
    plain = declared_cost_bytes(_upload(MediaType.PLAIN, same_size))

    assert TYPE_LIMITS[MediaType.DOCX].container is Container.ZIP
    assert TYPE_LIMITS[MediaType.PLAIN].container is Container.TEXT
    assert zipped > plain, "opening a compressed container costs more than decoding text"


def test_every_container_the_sniff_can_return_has_an_expansion_declared() -> None:
    """A missing row is a `KeyError` raised inside a memory guard, which is a guard that fails
    open: the parse never happens, the failure is an exception rather than a `ParseFailure`,
    and it reaches a worker as a bug in our code rather than as a refusal about a file.

    `Container.UNKNOWN` cannot arrive through the door, which refuses what it cannot name, and
    it is in the table anyway so that the totality is a property rather than a coincidence of
    which types are currently accepted. Delete this and adding a container to the sniff is a
    change that raises in production and nowhere else."""
    assert set(PARSE_EXPANSION) == set(Container)
    assert all(factor >= 1 for factor in PARSE_EXPANSION.values())


def test_the_worst_case_is_the_type_that_expands_most_and_not_the_largest_file() -> None:
    """The two are different and the difference is the point: PDF and PPTX share the largest
    ceiling the door allows, and a zip costs more to open than a PDF does, so the worst case is
    the one whose container expands rather than the one whose file is biggest.

    Delete this and the search can be inverted or narrowed to a single type, after which the
    container is sized against something smaller than the door will admit and the failure
    arrives as a refusal after the file is already stored."""
    media_type, cost = worst_declared_cost()
    biggest_pdf = declared_cost_bytes(_upload(MediaType.PDF, ceiling_for(MediaType.PDF)))

    assert ceiling_for(MediaType.PPTX) == ceiling_for(MediaType.PDF)
    assert media_type is MediaType.PPTX
    assert cost > biggest_pdf


# --------------------------------------------------------------- the bound at the parser
def test_a_file_over_the_budget_is_refused_before_the_parser_is_ever_called() -> None:
    """The whole leaf in one assertion. A limit enforced by watching memory climb is a limit
    that was already exceeded when it fired, and if it climbed fast enough the kernel has
    chosen a process to kill before anything in this repository runs. So the refusal happens
    before the parse, and the proof is that the parser was never handed anything.

    Delete this and the check can move to after the call, or be removed entirely, and the
    positive test below would still pass."""
    parser = FakeParser([])

    outcome = parse_scanned(_cleared(), parser=parser, budget_bytes=1)

    assert isinstance(outcome, ParseFailure)
    assert outcome.cause is ParseCause.OUT_OF_MEMORY
    assert parser.seen == [], "the parser was called for a file that was over the budget"


def test_a_file_within_the_budget_still_reaches_the_parser() -> None:
    """The positive sibling. A guard tested only by its refusals is satisfied by one that
    refuses everything, and a knowledge layer that parses nothing passes every memory test
    perfectly. Delete this and the bound can be set to zero as a safety measure."""
    cleared = _cleared()
    parser = FakeParser([])

    parse_scanned(cleared, parser=parser)

    assert parser.seen == [cleared]


def test_the_refusal_names_the_stage_that_refused_and_not_a_stage_of_the_parse() -> None:
    """A budget refusal happens before the container is opened, so reporting it as `open`
    would send whoever reads a run of them to the parser, which is the one place the fault is
    not: the fault is in two numbers, the door's ceiling and the container's limit, in two
    files.

    The expected detail is written out rather than built from `ParseStage.ADMIT`, because a
    test that formats the enum it is checking compares the value with itself and passes for
    every string the member could hold. Delete this and the stage can be changed to one that
    reads plausibly and points at the wrong file."""
    outcome = parse_scanned(_cleared(), parser=FakeParser([]), budget_bytes=1)

    assert isinstance(outcome, ParseFailure)
    assert outcome.detail == "stage:admit"


def test_the_message_an_uploader_reads_describes_their_file_and_not_the_machine() -> None:
    """DENIED and ABSENT are indistinguishable here as everywhere. "This file is too large to
    hold" is a fact about a document the person brought with them. "The queue is full because
    of twelve other jobs" is a count of other people's work, and a person who reads it has
    learned that twelve other people uploaded something.

    Asserted by what the message may not contain rather than by comparing it with the wording
    constant, because that comparison moves with the constant. Delete this and a helpful
    sentence about how busy the parser is becomes an obvious improvement."""
    outcome = parse_scanned(_cleared(), parser=FakeParser([]), budget_bytes=1)

    assert isinstance(outcome, ParseFailure)
    message = outcome.message()

    assert "sop.pdf" in message
    assert not any(character.isdigit() for character in message), message
    assert "queue" not in message.lower()
    assert CAUSE_TEXT[ParseCause.OUT_OF_MEMORY] in message


def test_a_file_refused_for_its_size_is_not_put_back_on_the_queue() -> None:
    """Sending the same bytes again produces the same refusal, so marking this retryable fills
    the queue with work that cannot ever succeed and crowds out the uploads that would. The
    remedy in the wording is a smaller document, which is a thing a person does rather than a
    thing a retry does.

    Delete this and `OUT_OF_MEMORY` can join the retryable causes on the grounds that memory
    pressure passes, which is true of the machine and not of the file."""
    outcome = parse_scanned(_cleared(), parser=FakeParser([]), budget_bytes=1)

    assert isinstance(outcome, ParseFailure)
    assert outcome.is_retryable is False


def test_the_bound_is_checked_in_the_one_place_a_parser_is_called() -> None:
    """The ordering argument this module's sibling opens with, applied to memory. A bound
    enforced in a worker loop is a bound the second worker loop forgets, and there is nowhere
    else `Parser.parse` is reached from.

    Asserted by making the budget refuse everything and showing that a parse which would
    otherwise succeed does not happen, rather than by reading the source. Delete this and the
    guard can be moved to a caller, where the next caller will not have it."""
    from brain.knowledge.chunking import Block, BlockKind

    blocks = [Block(kind=BlockKind.PROSE, text="Escalate a P1 within thirty minutes.", start=0)]
    cleared = _cleared()

    assert isinstance(parse_scanned(cleared, parser=FakeParser(blocks)), ParsedDocument)
    assert isinstance(
        parse_scanned(cleared, parser=FakeParser(blocks), budget_bytes=1), ParseFailure
    )


def test_no_file_the_door_admits_can_be_refused_by_the_deployed_budget() -> None:
    """The guard above cannot fire on the parse worker today, and that is the design rather
    than dead code: `parse_worker_gaps` is what keeps the door and the container in agreement,
    and this is that agreement stated from the file's side instead of the container's.

    The guard is still live, because `budget_bytes` is how a caller in a smaller container
    says so, and because the day somebody raises a ceiling is the day it fires. Delete this and
    the two files can drift apart with the only symptom being refusals after storage."""
    for media_type in MediaType:
        at_the_ceiling = _upload(media_type, ceiling_for(media_type))

        assert fits_parse_budget(at_the_ceiling), media_type

    over_every_ceiling = _upload(MediaType.PPTX, parse_budget_bytes())

    assert not fits_parse_budget(over_every_ceiling)


# --------------------------------------------------------------- the deployment
def test_the_parse_worker_container_is_sized_as_the_component_it_is_budgeted_as() -> None:
    """The budget is arithmetic over `COMPONENTS` and the compose file is what actually runs,
    and the parse budget is computed from the component rather than from the file. Two copies
    of one number is only safe while something compares them.

    Delete this and the container can be given whatever makes it start while every piece of
    arithmetic in `parse_budget` keeps reasoning about the old figure."""
    assert _cgroup_limit_mib() == component(PARSE_WORKER_COMPONENT).memory_mib


def test_the_parse_worker_names_which_component_it_is() -> None:
    """One image and one command run two differently sized containers, and this variable is
    the whole of the difference. Without it this container sizes itself as the general worker,
    the parse checks do not run, and a parse is admitted against a budget its cgroup will not
    honour.

    Delete this and the variable can be dropped in a tidy-up, which produces a container that
    starts, passes its preflight and is killed by the kernel on the first large document."""
    env = _environment()

    assert env[WORKER_COMPONENT_ENV] == PARSE_WORKER_COMPONENT
    assert declared_component(env) == PARSE_WORKER_COMPONENT
    assert next(iter(yaml.safe_load((REPO / COMPOSE).read_text("utf-8"))["services"])) == (
        PARSE_WORKER_COMPONENT
    )


def test_the_general_worker_is_unchanged_by_the_variable_it_does_not_set() -> None:
    """A variable added later must not change what a container already running does. The
    general worker's compose file does not name a component and has to keep sizing itself as
    `brain-worker`.

    Delete this and the default can be changed to the parse worker, which would size every
    existing worker against a limit it does not have."""
    general = _environment(GENERAL_WORKER_COMPOSE)

    assert WORKER_COMPONENT_ENV not in general
    assert declared_component(general) == "brain-worker"
    assert preflight(general) == ()


def test_the_deployed_parse_worker_satisfies_the_preflight_it_will_be_checked_by() -> None:
    """The strongest thing this file asserts: the deployment as written would start. Every
    other test here checks a rule; this one checks the artefact against all of them at once,
    which is what catches a rule and a file drifting apart in opposite directions.

    Delete this and the compose file can be edited into a state the worker refuses, which is
    discovered by deploying it."""
    assert preflight(_environment()) == ()


def test_the_preflight_refuses_a_parse_worker_that_would_run_two_parses_at_once() -> None:
    """`parse_worker_gaps` is a mechanism and this is its call site. The repository's most
    common defect is a correct, tested check that nothing invokes, and the way that happens is
    exactly this: the check is written with the module it belongs to and the wiring is left for
    later.

    Delete this and the call can be removed from `preflight` with every arithmetic test in this
    file still green."""
    findings = preflight(_environment(**{slot_env_name(TrafficClass.SYSTEM): "2"}))

    assert any("at a time" in finding for finding in findings), findings


def test_a_worker_naming_a_component_nobody_budgets_refuses_to_start() -> None:
    """Every remaining check is arithmetic against a memory limit, and an unknown component
    has none. Continuing against the default would print figures for a container this one is
    not, which is worse than printing nothing because it looks like an answer.

    Delete this and a typo in the variable silently sizes the parse worker as the general
    worker, which is the configuration this whole file exists to prevent."""
    findings = preflight(_environment(**{WORKER_COMPONENT_ENV: "brain-parse-wroker"}))

    assert len(findings) == 1
    assert "brain-parse-wroker" in findings[0]
    assert "how much memory" in findings[0]


def test_an_unknown_component_stops_the_arithmetic_rather_than_answering_from_the_default() -> None:
    """The half of the rule above that a sound environment cannot show, and it survived a
    mutation until this test existed. Falling back to `brain-worker` after reporting the
    unknown name changes nothing on the deployed file, because its one slot fits either
    container, so the mutant and the original print the same line.

    They stop agreeing the moment there is a second thing wrong. A hundred async slots is over
    the general worker's limit and says nothing about the container this one was meant to be,
    so reporting it alongside a name nobody budgets is an operator sent to fix a number that
    is not the problem. `preflight` returns everything else it finds; this is its one early
    return, and that is what this pins.

    Delete this and the early return becomes a fallback, which reads tidier and answers a
    question about a different container."""
    findings = preflight(
        _environment(
            **{
                WORKER_COMPONENT_ENV: "brain-parse-wroker",
                slot_env_name(TrafficClass.HUMAN_ASYNC): "100",
            }
        )
    )

    assert len(findings) == 1, findings
    assert "brain-parse-wroker" in findings[0]


def test_the_parse_worker_drains_only_the_class_ingestion_arrives_on() -> None:
    """A parse is enqueued as `TrafficClass.SYSTEM`, because `uploads.ingestion_request` takes
    no traffic-class parameter and cannot be promoted out of it. A parse worker allocated any
    other class is a container holding a connection for jobs that will never be parses, inside
    a memory limit sized for one that will.

    Asserted against `ingestion_request` rather than against the enum member, so that the day
    ingestion's class changes this fails instead of quietly draining the wrong queue."""
    env = _environment()
    allocated = {
        traffic_class
        for traffic_class in TrafficClass
        if int(env[slot_env_name(traffic_class)]) > 0
    }

    assert allocated == {ingestion_request("trace-1").traffic_class}


def test_the_parse_worker_names_every_class_including_the_three_it_does_not_drain() -> None:
    """An omitted class is reported as one whose jobs are never fetched, and a class
    deliberately allocated zero is a decision. The two must not look alike in a file somebody
    reads during an incident.

    Delete this and adding a traffic class silently strands its jobs, or the three zeroes get
    tidied out and the container starts reporting a fault it does not have."""
    env = _environment()

    assert all(slot_env_name(traffic_class) in env for traffic_class in TrafficClass)


def test_the_parse_worker_runs_the_same_command_as_the_worker_it_is_a_variant_of() -> None:
    """A second container is a configuration, not a second program. A command of its own would
    be a second entry point with its own preflight and its own readiness check, which is a copy
    of the module for one constant's worth of difference and the copy that goes stale.

    Delete this and the parse worker can acquire an entry point that skips the checks."""
    assert _compose_service()["command"] == _compose_service(GENERAL_WORKER_COMPOSE)["command"]
    assert set(_compose_service()["profiles"]) == component(PARSE_WORKER_COMPONENT).profiles


def test_the_parse_worker_reaches_the_database_the_way_the_worker_it_copies_does() -> None:
    """Both containers run `python -m brain.ops.worker`, so they open the same connection and
    take the same LISTEN on it. A component that declares no session state can later be wired
    through the transaction pooler with `pooler_misuse` saying nothing, and that is the failure
    with no error message: the listener is moved to another backend, stops receiving, and every
    metric reports an empty queue.

    Asserted against the general worker's declaration rather than against `True` and
    `Wiring.DIRECT` written out, so the two containers cannot answer one question differently
    while running one command. Delete this and the copy can be declared as though it were an
    ordinary client."""
    parse_worker = component(PARSE_WORKER_COMPONENT)
    general = component("brain-worker")

    assert parse_worker.wiring is general.wiring
    assert parse_worker.needs_session_state is general.needs_session_state
    assert parse_worker.needs_session_state is True, "a queue listener needs a session"


def test_the_worker_prints_what_a_parse_costs_and_not_only_what_a_slot_costs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The plan reports one slot at `MIB_PER_SLOT` of a 512 MiB limit, and an operator reading
    only that would reasonably conclude the container is eight times larger than it needs to be
    and shrink it. What one parse may actually cost is a different figure by an order of
    magnitude, so it is printed beside the plan rather than left in a source file to be found
    during the incident.

    Delete this and the note becomes a function nothing calls, which is the defect this
    repository has shipped seven times."""
    assert main(["--check"], env=_environment()) == 0
    printed = capsys.readouterr().out

    assert "parse budget" in printed
    assert f"{parse_budget_bytes() // MIB} MiB" in printed
    assert parse_budget_bytes() // MIB > PARSES_AT_ONCE * MIB_PER_SLOT, (
        "the note exists because the slot figure understates a parse; if it does not, "
        "the note is noise and the container is the wrong size"
    )


def test_the_note_says_a_parse_over_the_bound_is_refused_and_one_that_grows_is_not() -> None:
    """The honest half. The bound refuses a job before it starts, which contains that failure;
    a parser that starts inside the bound and allocates past it is killed by the kernel, and no
    handler in this process sees SIGKILL. Saying the bound protects the container would be
    claiming an isolation nobody wrote, and the place an operator meets that claim is this
    line in a container log.

    Delete this and the note can be shortened to the reassuring half."""
    note = parse_budget_note()

    assert "refused before the file is opened" in note
    assert "OOM kill" in note
