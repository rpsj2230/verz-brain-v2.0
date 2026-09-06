"""The single execution path, what its sandbox actually enforces, and the leash on a run.

Three groups, one per word in the leaf.

*Single* is checked structurally rather than by reading the module: over a whole registry,
and over the source tree, in the style `tests/invariants/test_single_implementation.py`
uses. The failure being prevented is not malice. It is a second executor written by somebody
who never opened `brain/tools/run_skill.py`, which is exactly how this repository ended up
auditing for a second `compile_where`.

*Sandboxed* is checked in both directions, and the second direction is the one that matters:
there are tests here asserting that the module does **not** claim isolation it has not
implemented. A test suite that only proves the guards work would be satisfied by a module
whose docstring promised a container.

*Leashed* is checked against outcomes a runner reports rather than against a process,
because the interesting cases are the flood and the overrun and neither is reachable in a
test that spawns something real.

Task ids: M12.2.9
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import IdentityMode, SideEffect, ToolDefinition
from brain.core.scope import Scope
from brain.tools.registry import (
    RUN_SKILL_SCRIPT,
    SKILL_SCRIPT_OBJECT,
    ResultContract,
    ToolRegistrationError,
    ToolRegistry,
)
from brain.tools.run_skill import (
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENTS,
    MAX_OUTPUT_BYTES,
    MAX_WALL_CLOCK_SECONDS,
    PERMITTED_ENV_NAMES,
    SANDBOX_PROPERTIES,
    SCRIPT_CAPABILITY,
    SKILL_NOT_AVAILABLE,
    Egress,
    RunOutcome,
    RunStatus,
    SandboxProperty,
    SandboxSpec,
    ScriptLeash,
    ScriptRequest,
    ScriptRun,
    SkillScriptError,
    SkillScriptTool,
    accept_outcome,
    assert_single_execution_path,
    build_environment,
    execution_tools,
    plan_run,
    sandbox_gaps,
    unenforced_properties,
)
from brain.tools.skills import (
    ImportedSkill,
    Skill,
    SkillError,
    SkillPin,
    SkillSource,
    SourceKind,
    execution_tool,
    skill_from_markdown,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "brain"

REVIEWED_AT = datetime(2026, 9, 6, 9, 30, tzinfo=UTC)


# ------------------------------------------------------------------ fixtures and doubles


def _skill(*, body: str = "check the expiry, then open a ticket") -> Skill:
    return Skill(
        name="hosting_expiry",
        description="Check which hosting accounts expire this month",
        version="1.0.0",
        scripts=("scripts/check.py",),
        body=body,
    )


def _approved(skill: Skill) -> ImportedSkill:
    imported = ImportedSkill(
        skill=skill,
        source=SkillSource(
            kind=SourceKind.UPLOAD, location="hosting_expiry.zip", content_digest="a" * 64
        ),
    )
    return imported.approved_by("rupash", REVIEWED_AT)


def _pin(skill: Skill, *, agent_id: str = "ops_agent") -> SkillPin:
    return SkillPin(agent_id=agent_id, skill_name=skill.name, digest=skill.digest())


class _Library:
    """A `SkillLibrary` holding whatever a test put in it.

    A dict rather than a store, because every case worth testing here is one a real store
    cannot easily be made to produce: a skill that is not there, one that is there and
    unapproved, and one that has been edited since it was pinned."""

    def __init__(self, entries: dict[tuple[str, str], tuple[SkillPin, ImportedSkill]]) -> None:
        self._entries = entries

    def pinned_skill(self, agent_id: str, skill_name: str) -> tuple[SkillPin, ImportedSkill] | None:
        return self._entries.get((agent_id, skill_name))


class _Runner:
    """A `ScriptRunner` that starts nothing and reports whatever it was told to report.

    It keeps every spec it was handed, which is how the tests about what the process is
    given assert on the object rather than on a message."""

    def __init__(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        self.specs: list[SandboxSpec] = []

    def run(self, spec: SandboxSpec) -> RunOutcome:
        self.specs.append(spec)
        return self.outcome


def _outcome(**overrides: object) -> RunOutcome:
    fields: dict[str, object] = {
        "run_id": "run-0001",
        "status": RunStatus.COMPLETED,
        "exit_code": 0,
        "output": "3 accounts expire this month",
        "elapsed_seconds": 1.5,
    }
    fields.update(overrides)
    return RunOutcome(**fields)  # type: ignore[arg-type]


def _entitlements(principal: str, *values: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=v), scope=Scope()) for v in values),
    )


def _tool(library: _Library, runner: _Runner, **overrides: object) -> SkillScriptTool:
    return SkillScriptTool(library=library, runner=runner, **overrides)  # type: ignore[arg-type]


def _spec(**overrides: object) -> SandboxSpec:
    fields: dict[str, object] = {"skill": "hosting_expiry", "digest": "b" * 64, "script": "run.py"}
    fields.update(overrides)
    return SandboxSpec(**fields)  # type: ignore[arg-type]


def _definitions(name: str) -> list[str]:
    """Every module under `src/brain` defining a function or class with this name.

    Parsed rather than grepped, exactly as `tests/invariants/test_single_implementation.py`
    does it, so a mention in a docstring or a comment is not a definition."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that will not parse fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if node.name == name:
                found.append(path.relative_to(SRC).as_posix())
                break
    return found


# ================================================================== single (M12.2.9)


def test_the_execution_tool_takes_its_name_from_the_module_that_reserves_the_object() -> None:
    """`brain.tools.skills.execution_tool` is the one answer to "which tool runs a script",
    and `brain.tools.registry` reserves the object for exactly that name. A tool that spelled
    the name again here would be a second answer, and the day the two disagree the registry
    refuses the real tool and admits the copy.

    Delete this and the name can be written as a literal in `definition`, which passes every
    other test in this file and stops matching the reservation."""
    definition = _tool(_Library({}), _Runner(_outcome())).definition()

    assert definition.name == execution_tool()
    assert definition.name == RUN_SKILL_SCRIPT
    assert definition.entity == SKILL_SCRIPT_OBJECT
    assert definition.required_capability == SCRIPT_CAPABILITY.value


def test_a_registry_holding_only_the_execution_tool_has_one_execution_path() -> None:
    """The positive case, and the one that stops `assert_single_execution_path` being
    satisfied by a function that refuses everything.

    A registry with no execution tool at all is also fine, and that is the state of this
    deployment today: no sandbox exists, so nothing registers one."""
    tool = _tool(_Library({}), _Runner(_outcome()))
    registry = ToolRegistry()
    registry.register(tool.definition(), tool.handler(), result_contract=ResultContract.TYPED)

    assert execution_tools(registry) == (RUN_SKILL_SCRIPT,)
    assert_single_execution_path(registry)
    assert_single_execution_path(ToolRegistry())


def test_a_second_tool_that_runs_supplied_code_is_refused_under_any_object() -> None:
    """**The gap this function exists for.** `assert_object_not_reserved` refuses a second
    tool claiming the skill-script object, one registration at a time, so a second executor
    only has to pick a different object to get past it. That tool is the one somebody adds in
    a hurry, and it is the one without the leash.

    Delete this and a `sandbox.exec_command` registers cleanly beside `skill.run_script`,
    with its own idea of a timeout and its own environment."""
    tool = _tool(_Library({}), _Runner(_outcome()))
    second = ToolDefinition(
        name="sandbox.exec_command",
        description="Run a command in the connector sandbox",
        entity="command_run",
        required_capability="invoke:command_run",
    )

    with pytest.raises(SkillScriptError, match=r"sandbox\.exec_command"):
        assert_single_execution_path([tool.definition(), second])


def test_a_tool_claiming_the_skill_script_object_is_an_execution_path_however_named() -> None:
    """The object is the exact half of the detection and the name is the heuristic half. A
    second executor called `reports.build_summary` gets past the name check, and it does not
    get past the object, because the object is what a leash entry, a field policy rule and a
    slug collision are all written about.

    Delete this and the object test can be dropped on the grounds that the name test already
    catches the canonical tool, which is true and misses every tool named dishonestly."""
    disguised = ToolDefinition(
        name="reports.build_summary",
        description="Build a summary, whatever that turns out to involve",
        entity=SKILL_SCRIPT_OBJECT,
        required_capability="invoke:skill_script",
    )

    assert execution_tools([disguised]) == ("reports.build_summary",)
    with pytest.raises(SkillScriptError, match="run supplied code"):
        assert_single_execution_path([disguised])


def test_an_ordinary_tool_whose_verb_happens_to_be_run_is_not_an_execution_path() -> None:
    """The sibling of the test above, and it is what keeps that rule alive. A check that
    refused `xero.run_report` would be edited out of the way by the first person who wrote a
    reporting tool, and then nothing would check for a second executor at all.

    Delete this and the verb list can be widened until it matches half the catalogue."""
    reporting = ToolDefinition(
        name="xero.run_report",
        description="Run a saved Xero report and return its rows",
        entity="report",
        required_capability="read:report",
    )

    assert execution_tools([reporting]) == ()
    assert_single_execution_path([reporting])


def test_a_definition_whose_name_never_passed_the_grammar_counts_as_an_execution_path() -> None:
    """The fail-closed branch, and it is reachable rather than theoretical. `model_copy` does
    not re-validate in pydantic, so a `ToolDefinition` can exist whose name never went through
    `TOOL_NAME_PATTERN`, which is exactly what a manifest-driven connector building tools at
    run time is one line away from producing.

    Failing closed on a shape nobody has thought about is cheaper than a branch that waves it
    through. Delete this and the unparseable case returns False, and an executor with a
    malformed name becomes the one tool this check never looks at."""
    unvalidated = ToolDefinition(
        name="xero.run_report",
        description="Run a saved Xero report and return its rows",
        entity="report",
        required_capability="read:report",
    ).model_copy(update={"name": "not a tool name at all"})

    assert execution_tools([unvalidated]) == ("not a tool name at all",)
    with pytest.raises(SkillScriptError, match="run supplied code"):
        assert_single_execution_path([unvalidated])


def test_the_execution_tool_declaring_another_object_is_refused() -> None:
    """The direction registration cannot see. A `skill.run_script` that declares some other
    entity registers fine, and then the reserved object is claimable by nobody, because the
    only name permitted to claim it is already taken. The leash entry and the field policy
    rule are both written about the object.

    Delete this and the reservation in `brain.tools.registry` can be defeated by a typo in
    one field."""
    misdeclared = ToolDefinition(
        name=RUN_SKILL_SCRIPT,
        description="Run one script belonging to an approved skill",
        entity="report",
        required_capability="invoke:report",
    )

    with pytest.raises(SkillScriptError, match="rather than"):
        assert_single_execution_path([misdeclared])


def test_a_second_tool_claiming_the_skill_script_object_never_reaches_the_registry() -> None:
    """The composition, asserted rather than assumed. This module's whole-registry check is
    the second of two doors, and it would be pointless if the first one had quietly stopped
    refusing.

    Delete this and `assert_object_not_reserved` can be removed from `register` with every
    other test here still green, because they all call the whole-registry check directly."""
    registry = ToolRegistry()
    imposter = ToolDefinition(
        name="sandbox.run_script",
        description="Run a script in the connector sandbox",
        entity=SKILL_SCRIPT_OBJECT,
        required_capability="invoke:skill_script",
    )

    with pytest.raises(ToolRegistrationError, match="reserved"):
        registry.register(imposter, lambda: None)


def test_the_runner_seam_and_the_spec_are_defined_once_in_the_whole_source_tree() -> None:
    """Two of these existing would mean two sandboxes with two ideas of what a process gets,
    and the one that is wrong is whichever was written second by somebody who could not see
    the first. This is the check `tests/invariants/test_single_implementation.py` makes about
    `compile_where`, applied to the thing that runs imported code.

    Delete this and a parallel build adds `connectors/sandbox.py` with its own `ScriptRunner`,
    which reads as reasonable in the file it appears in."""
    home = "tools/run_skill.py"

    assert _definitions("ScriptRunner") == [home]
    assert _definitions("SandboxSpec") == [home]
    assert _definitions("run_skill_script") == [home]
    assert _definitions("accept_outcome") == [home]


def test_a_skill_cannot_name_a_runner_of_its_own() -> None:
    """A skill that could declare its interpreter would be choosing what runs its file, and
    the file arrived from outside the company. `parse_frontmatter` refuses an unknown key
    rather than ignoring it, which is what makes this hold; the test is here because the
    property belongs to this leaf and nothing else asserts it from this side.

    Delete this and `FRONTMATTER_KEYS` can be opened up to unknown keys, and a `runner:` line
    starts reading as honoured because it is no longer refused."""
    text = "---\nname: x\ndescription: y\nrunner: /bin/bash\n---\nbody"

    with pytest.raises(SkillError, match="runner"):
        skill_from_markdown(text)


# ================================================================== sandboxed (M12.2.9)


def test_the_environment_is_built_from_a_closed_set_of_names_rather_than_inherited() -> None:
    """A child process inherits its parent's environment unless somebody stops it, and this
    parent holds a database URL and a vault token. The refusal names the variable and never
    its value, because a checker that quoted what it found would write the credential into
    the log that reads its message.

    `LD_PRELOAD` rather than something obviously named for a secret, and that choice is the
    test. A credential-shaped name is caught by the value check next door, so a variable
    tested with one would pass with the allowlist deleted; `LD_PRELOAD` is caught by the
    allowlist and nothing else, and it is the classic way one process makes another load code
    it never asked for.

    Delete this and `build_environment` can be reduced to a copy, and the sandbox is handed
    whatever the deployment set."""
    with pytest.raises(SkillScriptError) as raised:
        build_environment({"LD_PRELOAD": "/opt/hook.so"})

    assert "LD_PRELOAD" in str(raised.value)
    assert "/opt/hook.so" not in str(raised.value)


def test_an_allowlisted_name_holding_a_connection_string_is_still_refused() -> None:
    """The case a name allowlist cannot see, which is why there are two checks and not one.
    `TMPDIR` is a perfectly ordinary variable and a connection string in it is a full
    credential under an innocent name, which is the argument
    `brain.ops.automation.credential_leaks` makes about `BRAIN_UPSTREAM`.

    Delete this and the value check can be dropped, leaving a rule that only refuses
    variables somebody named honestly."""
    assert "TMPDIR" in PERMITTED_ENV_NAMES

    with pytest.raises(SkillScriptError) as raised:
        build_environment({"TMPDIR": "postgresql://brain:hunter2@db:5432/brain"})

    assert "TMPDIR" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_a_permitted_environment_reaches_the_sandbox_unchanged() -> None:
    """The positive case. A guard tested only by its refusals is satisfied by a function that
    refuses everything, and a sandbox with no `PATH` runs nothing at all.

    Delete this and `build_environment` can be reduced to `raise`, and every refusal test in
    this file still passes."""
    built = build_environment({"PATH": "/usr/bin", "TZ": "UTC"})

    assert built == {"PATH": "/usr/bin", "TZ": "UTC"}
    assert set(built) <= PERMITTED_ENV_NAMES


def test_a_script_the_reviewed_skill_did_not_declare_never_runs() -> None:
    """`Skill.scripts` is the list a named person approved. A run that could name any file in
    the folder would run whatever an archive dropped there, and the review would have covered
    the files somebody happened to read.

    Delete this and the membership check goes, leaving only a path check, which is a rule
    about strings rather than about what was reviewed."""
    skill = _skill()
    request = ScriptRequest(skill=skill.name, script="scripts/other.py")

    with pytest.raises(SkillScriptError, match="reviewed list"):
        plan_run(skill, request, leash=ScriptLeash(), environment={}, reach_hash="")


def test_a_script_path_that_leaves_the_skill_folder_never_runs() -> None:
    """Refused as a traversal rather than as an undeclared name, which is what an operator
    reading the message needs to know. `safe_archive_member` is reused rather than restated,
    so the platform difference it handles (a backslash is a separator on the machine this is
    developed on and a character on the one it runs on) is handled here too.

    Asserted on the exception **type** rather than on the message, and that is what makes the
    order load-bearing rather than decorative: `safe_archive_member` raises a plain
    `SkillError` and the membership check raises a `SkillScriptError`, so a traversal refused
    by the wrong one is visible here. Without that, both checks refuse all three of these
    paths and either could be deleted with the test still green.

    Delete this and the path check can be dropped on the grounds that membership already
    covers it, which is true only for as long as `Skill.scripts` keeps validating."""
    skill = _skill()

    for path in ("../../etc/shadow", "..\\..\\etc\\shadow", "/etc/shadow"):
        request = ScriptRequest(skill=skill.name, script=path)
        with pytest.raises(SkillError) as raised:
            plan_run(skill, request, leash=ScriptLeash(), environment={}, reach_hash="")
        assert not isinstance(raised.value, SkillScriptError)


def test_the_spec_has_nowhere_to_put_a_grant_a_token_or_a_tool_list() -> None:
    """**The structural half of the invariant.** A rule saying "do not pass the credentials
    to the sandbox" holds until the first person who needs one. A type with no field for them
    cannot carry them, which is the construction `Skill` uses for capabilities and `SkillCard`
    uses for a body.

    Asserted over `dataclasses.fields` rather than over the source text, because a test that
    searched the file would be satisfied by this docstring.

    Delete this and a `credentials` field is added to help a script call an API, and the
    script stops running at the caller's reach."""
    reach_carrying = {
        "EntitlementSet",
        "Capability",
        "Grant",
        "Scope",
        "ToolRegistry",
        "ToolDefinition",
        "Principal",
    }
    credential_named = {"token", "secret", "password", "credential", "credentials", "key", "auth"}

    for spec_field in dataclasses.fields(SandboxSpec):
        annotation = str(spec_field.type)
        assert not (reach_carrying & set(annotation.replace("[", " ").replace("]", " ").split()))
        assert spec_field.name.lower() not in credential_named


def test_the_model_facing_request_carries_no_interpreter_environment_or_command_line() -> None:
    """What a model may say is the whole attack surface a model has. There is no interpreter,
    so a skill cannot choose what runs its file; no environment, so nothing a model says
    reaches the process's environment; and `arguments` is a sequence rather than a string, so
    there is nowhere to write a command line for a shell to split.

    Delete this and a `command` field is added for convenience, and the quoting problem comes
    back along with it."""
    assert set(ScriptRequest.model_fields) == {"skill", "script", "arguments"}
    assert ScriptRequest.model_fields["arguments"].annotation == tuple[str, ...]


def test_the_sandbox_can_only_declare_that_egress_is_denied() -> None:
    """The absence is the mechanism, as it is in `brain.ops.automation.StepKind`. A skill
    script composes tools that have already fetched what they fetched, so an outbound
    connection from inside one is a path around the gate, and there is no member of this enum
    in which to say otherwise.

    Delete this and an `ALLOWED` member is added for one skill that needed an API, and the
    default stops being a decision anybody reviews."""
    assert [member.value for member in Egress] == ["denied"]
    assert _spec().network is Egress.DENIED


def test_every_property_this_module_does_not_enforce_names_what_would_enforce_it() -> None:
    """**The test that keeps the module honest rather than the one that proves it works.**
    Six of these ten properties are declarations a container has to honour, and a module that
    listed them without naming an enforcer would be read as a guarantee by the next person.

    Delete this and `SANDBOX_PROPERTIES` becomes a list of things the sandbox does, which is
    a claim of isolation nobody implemented."""
    enforced = [prop for prop in SANDBOX_PROPERTIES if prop.enforced_here]

    # Pinned at four rather than derived, for the reason `brain.ops.wiring` pins a memory
    # budget somewhere that is not the thing being budgeted: a count taken from the table
    # agrees with the table for every possible value, including the one where every row
    # claims to be enforced. Each of these four has a named test above proving it.
    assert len(enforced) == 4
    assert len(unenforced_properties()) == len(SANDBOX_PROPERTIES) - 4
    for prop in SANDBOX_PROPERTIES:
        assert prop.enforced_by.strip()
    assert len(sandbox_gaps()) == len(unenforced_properties())

    denied = " ".join(p.denies for p in SANDBOX_PROPERTIES)
    for must_be_named in ("network", "memory", "CPU", "subprocess", "working directory"):
        assert must_be_named in denied


def test_a_sandbox_property_with_no_named_enforcer_cannot_be_declared() -> None:
    """`brain.ops.queue.SwapCandidate` refuses an alternative with no trigger on the grounds
    that it is a rumour. A sandbox property with no enforcer is worse than a rumour, because
    it reads as a guarantee.

    Delete this and a row can be added claiming the sandbox denies something, with nothing
    saying what does the denying."""
    with pytest.raises(SkillScriptError, match="enforcer"):
        SandboxProperty(denies="everything bad", enforced_here=False, enforced_by="   ")


def test_the_spec_carries_the_digest_so_a_runner_cannot_unpack_another_version() -> None:
    """A runner that materialised whatever is on disk under the skill's name would run
    something nobody approved, and the pin in `brain.tools.skills` would have been checking a
    version that never ran.

    Delete this and the digest stops travelling with the run, and version locking ends at the
    edge of the process."""
    skill = _skill()
    request = ScriptRequest(skill=skill.name, script="scripts/check.py")

    spec = plan_run(skill, request, leash=ScriptLeash(), environment={}, reach_hash="")

    assert spec.digest == skill.digest()
    assert spec.script == "scripts/check.py"


# ================================================================== leashed (M12.2.9)


def test_a_leash_may_be_tightened_per_call_and_never_loosened() -> None:
    """A ceiling that can be raised at the call site is not a ceiling. `rung_ceiling` composes
    with `min` for the same reason and says so: a value written by whoever is in a hurry is
    inherited from then on by every caller nobody considered.

    Refused rather than clamped, because a clamp is a silent disagreement between what a
    caller asked for and what it got. Delete this and one skill that needed five minutes gets
    five minutes, for everybody, for ever."""
    tightened = ScriptLeash(wall_clock_seconds=5, output_bytes=1024)

    assert tightened.wall_clock_seconds == 5
    assert tightened.output_bytes == 1024

    with pytest.raises(SkillScriptError, match="tightened"):
        ScriptLeash(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS + 1)
    with pytest.raises(SkillScriptError, match="tightened"):
        ScriptLeash(output_bytes=MAX_OUTPUT_BYTES * 2)


def test_a_leash_of_zero_is_refused_because_that_is_how_unlimited_gets_spelled() -> None:
    """Zero is how "no limit" is written in a dataclass, and it must not be constructible.
    `brain.ops.wiring.Component` refuses a memory limit of zero for the same reason and calls
    it a neighbour's outage.

    Delete this and `ScriptLeash(wall_clock_seconds=0)` becomes a run with no deadline, which
    reads in a diff as somebody turning a limit off deliberately."""
    for kwargs in ({"wall_clock_seconds": 0}, {"memory_mib": 0}, {"output_bytes": -1}):
        with pytest.raises(SkillScriptError, match="unlimited"):
            ScriptLeash(**kwargs)


def test_an_outcome_that_overran_the_deadline_is_a_timeout_with_no_output() -> None:
    """**Accepting a late result makes the deadline advisory.** The run already held whatever
    it was holding for longer than it was allowed to, and returning its output teaches every
    caller that the number is a suggestion.

    `truncated` is set, so nobody reads the empty output as a script that printed nothing.
    Delete this and a runner that never kills anything produces records saying every run
    completed."""
    spec = _spec(leash=ScriptLeash(wall_clock_seconds=5))
    record = accept_outcome(spec, _outcome(elapsed_seconds=5.4, output="most of an answer"))

    assert record.status is RunStatus.TIMED_OUT
    assert record.output == ""
    assert record.output_bytes == 0
    assert record.truncated is True


def test_a_run_that_finished_inside_its_deadline_returns_what_the_script_printed() -> None:
    """The sibling of the timeout test. A guard tested only by its refusals is satisfied by a
    function that refuses everything, and an execution tool that returns nothing is an
    execution tool nobody can use.

    Delete this and `accept_outcome` can be made to discard every output, and the timeout test
    above still passes."""
    spec = _spec(leash=ScriptLeash(wall_clock_seconds=5))
    record = accept_outcome(spec, _outcome(elapsed_seconds=4.9, output="17 accounts"))

    assert record.status is RunStatus.COMPLETED
    assert record.output == "17 accounts"
    assert record.truncated is False
    assert record.elapsed_seconds == 4.9


def test_output_over_the_cap_is_cut_in_bytes_and_says_that_it_was_cut() -> None:
    """Counted in bytes rather than characters, because the thing being protected is a context
    window and a message queue and both are sized in bytes: a character cap lets a four-byte
    codepoint spend four times the budget.

    `output_bytes` reports what the script actually produced rather than what survived, so an
    operator can see how far over it went. Delete this and a script that prints a database
    dump becomes an outage in whatever called the tool."""
    spec = _spec(leash=ScriptLeash(output_bytes=16))
    flood = "é" * 100  # two bytes each, so a character count would pass 100 as 100

    record = accept_outcome(spec, _outcome(output=flood))

    assert record.truncated is True
    assert record.output_bytes == 200
    assert len(record.output.encode("utf-8")) <= 16


def test_a_non_zero_exit_is_a_failure_even_when_the_runner_calls_it_a_success() -> None:
    """The exit code is the one fact a caller acts on. A runner whose word is taken for it is
    a runner that can turn a crashed script into a clean empty answer, which is a wrong result
    rather than a missing one.

    Delete this and a sandbox with a bug in its status reporting silently converts every
    failure into a completion."""
    record = accept_outcome(_spec(), _outcome(status=RunStatus.COMPLETED, exit_code=2))

    assert record.status is RunStatus.FAILED
    assert record.exit_code == 2


def test_a_runner_reporting_something_impossible_is_refused_rather_than_recorded() -> None:
    """A run with no id cannot be pointed at afterwards, and a negative duration is a broken
    clock or a broken runner. Writing the record anyway would put a row into the audit trail
    that nothing can be reconstructed from.

    Delete this and the leash arithmetic runs on numbers nobody checked, and a negative
    elapsed time passes the deadline comparison by being smaller than everything."""
    with pytest.raises(SkillScriptError, match="no id"):
        accept_outcome(_spec(), _outcome(run_id="  "))
    with pytest.raises(SkillScriptError, match="negative"):
        accept_outcome(_spec(), _outcome(elapsed_seconds=-1.0))


def test_an_argv_built_out_of_content_rather_than_references_is_refused() -> None:
    """Bounded for the reason `brain.ops.queue.Job` bounds its own arguments: past a certain
    length an argument is content, and content belongs in a file the script reads rather than
    in an argv a model assembled. A control character is refused too, because an argument
    carrying a newline turns into two arguments wherever argv is later rendered as a line.

    Delete this and one tool call can hand a sandbox a megabyte of prompt."""
    with pytest.raises(ValueError, match="over"):
        ScriptRequest(skill="x", script="s.py", arguments=("a" * (MAX_ARGUMENT_CHARS + 1),))
    with pytest.raises(ValueError, match="over"):
        ScriptRequest(skill="x", script="s.py", arguments=tuple("a" * (MAX_ARGUMENTS + 1)))
    with pytest.raises(ValueError, match="control character"):
        ScriptRequest(skill="x", script="s.py", arguments=("--to\nrm -rf /",))


# ================================================================== reach (M12.2.9)


def test_an_approved_and_pinned_skill_runs_and_the_spec_says_what_the_process_gets() -> None:
    """The positive case for the whole path, and the only test here that goes through the
    registered handler end to end. Everything else in this file proves a refusal, and a suite
    of refusals is satisfied by a tool that never runs anything.

    Delete this and the handler can be reduced to a raise, and every other test passes."""
    skill = _skill()
    runner = _Runner(_outcome())
    tool = _tool(
        _Library({("ops_agent", skill.name): (_pin(skill), _approved(skill))}),
        runner,
        environment={"PATH": "/usr/bin"},
    )

    result = tool.handler()(
        ScriptRequest(skill=skill.name, script="scripts/check.py", arguments=("--month", "9")),
        agent_id="ops_agent",
        entitlement=_entitlements("rupash", "read:client.name"),
        agent_ceiling=_entitlements("ops_agent", "read:client.name"),
        now=REVIEWED_AT,
    )

    assert result.record_count() == 1
    record = result.records[0]
    assert isinstance(record, ScriptRun)
    assert record.status is RunStatus.COMPLETED
    assert record.output == "3 accounts expire this month"
    assert result.fetched_at == REVIEWED_AT.isoformat()

    spec = runner.specs[0]
    assert spec.arguments == ("--month", "9")
    assert spec.environment == {"PATH": "/usr/bin"}
    assert spec.network is Egress.DENIED
    assert spec.leash.wall_clock_seconds == MAX_WALL_CLOCK_SECONDS


def test_a_script_runs_at_the_intersection_of_the_caller_and_the_agent_ceiling() -> None:
    """**The invariant the leaf is security-relevant for.** `E_run(caller, agent) = E(caller)
    intersected with agent_ceiling`, computed by calling `EntitlementSet.intersect` and by
    nothing else. The run is recorded against that reach and not against the caller's own,
    which is what an audit reads.

    Asserted against the entitlement objects rather than against a hash alone, so a second
    implementation of the intersection cannot satisfy it by producing a plausible string.

    Delete this and the reach recorded on a run becomes the caller's whole entitlement, and
    the agent ceiling stops appearing anywhere the run can be audited from."""
    skill = _skill()
    runner = _Runner(_outcome())
    tool = _tool(_Library({("ops_agent", skill.name): (_pin(skill), _approved(skill))}), runner)

    caller = _entitlements("rupash", "read:client.name", "read:invoice.total")
    ceiling = _entitlements("ops_agent", "read:client.name", "read:ticket.status")
    narrowed = caller.intersect(ceiling)

    result = tool.handler()(
        ScriptRequest(skill=skill.name, script="scripts/check.py"),
        agent_id="ops_agent",
        entitlement=caller,
        agent_ceiling=ceiling,
    )

    assert narrowed.holds(Capability(value="read:client.name"))
    assert not narrowed.holds(Capability(value="read:invoice.total"))
    assert not narrowed.holds(Capability(value="read:ticket.status"))
    assert result.records[0].reach_hash == narrowed.ent_hash()
    assert result.records[0].reach_hash != caller.ent_hash()
    assert result.records[0].reach_hash != ceiling.ent_hash()
    assert runner.specs[0].reach_hash == narrowed.ent_hash()


def test_a_skill_that_is_absent_and_one_that_is_unapproved_refuse_identically() -> None:
    """**DENIED and ABSENT have to be indistinguishable**, and the reader here is a model,
    which explains what it just tried. "The hosting-expiry skill exists but has not been
    approved" is a sentence that reaches a person through the one channel nobody audits, and
    it discloses both that the skill exists and what it is called.

    Delete this and the refusal grows a helpful explanation, and the review queue becomes
    something an agent can enumerate."""
    skill = _skill()
    runner = _Runner(_outcome())
    unapproved = ImportedSkill(
        skill=skill,
        source=SkillSource(
            kind=SourceKind.UPLOAD, location="hosting_expiry.zip", content_digest="a" * 64
        ),
    )
    request = ScriptRequest(skill=skill.name, script="scripts/check.py")
    caller = _entitlements("rupash", "read:client.name")

    def run(library: _Library) -> str:
        with pytest.raises(SkillScriptError) as raised:
            _tool(library, runner).handler()(
                request, agent_id="ops_agent", entitlement=caller, agent_ceiling=caller
            )
        return str(raised.value)

    absent = run(_Library({}))
    present_but_unreviewed = run(_Library({("ops_agent", skill.name): (_pin(skill), unapproved)}))

    assert absent == present_but_unreviewed == SKILL_NOT_AVAILABLE
    assert skill.name not in absent
    assert not runner.specs


def test_a_skill_edited_since_it_was_pinned_does_not_run() -> None:
    """`resolve_pin` refuses a rename, an edit since the pin, and an unapproved skill, and it
    is called rather than restated. An agent that silently followed an edit would run a
    procedure it was never tested with, in a sandbox, with whatever the edit added.

    Delete this and the pin stops being consulted on the one path that executes code, which
    is the path where following an edit actually does something."""
    pinned_version = _skill()
    edited = _skill(body="check the expiry, then email the client")
    runner = _Runner(_outcome())
    tool = _tool(
        _Library({("ops_agent", pinned_version.name): (_pin(pinned_version), _approved(edited))}),
        runner,
    )
    caller = _entitlements("rupash", "read:client.name")

    with pytest.raises(SkillScriptError, match=SKILL_NOT_AVAILABLE):
        tool.handler()(
            ScriptRequest(skill=pinned_version.name, script="scripts/check.py"),
            agent_id="ops_agent",
            entitlement=caller,
            agent_ceiling=caller,
        )

    assert not runner.specs


def test_another_agents_pin_does_not_run_for_this_agent() -> None:
    """The pin is per agent, and the agent id is wiring supplied by the dispatcher rather than
    a field in the request. A model that could name an agent would borrow whichever agent had
    the widest set of approved skills.

    Delete this and `agent_id` can be moved into `ScriptRequest`, which is one field and the
    end of per-agent version locking."""
    skill = _skill()
    runner = _Runner(_outcome())
    tool = _tool(
        _Library(
            {
                ("finance_agent", skill.name): (
                    _pin(skill, agent_id="finance_agent"),
                    _approved(skill),
                )
            }
        ),
        runner,
    )
    caller = _entitlements("rupash", "read:client.name")

    assert "agent_id" not in ScriptRequest.model_fields
    with pytest.raises(SkillScriptError, match=SKILL_NOT_AVAILABLE):
        tool.handler()(
            ScriptRequest(skill=skill.name, script="scripts/check.py"),
            agent_id="ops_agent",
            entitlement=caller,
            agent_ceiling=caller,
        )


# ================================================================== registration


def test_the_execution_tool_passes_every_door_of_the_real_registry() -> None:
    """Asserted against a real `ToolRegistry` rather than against a stub, because the value of
    this module's declarations is that they satisfy rules written somewhere else: the name
    grammar, the reserved object, the capability parse, the effect-to-capability rule and the
    redactor's shape check all run on the way in.

    Delete this and the definition can drift into something that would be refused at startup,
    and nothing here would notice until a process tried to boot."""
    tool = _tool(_Library({}), _Runner(_outcome()))
    registry = ToolRegistry()

    registered = registry.register(tool.definition(), tool.handler())
    registry.freeze()

    assert registered.capability == SCRIPT_CAPABILITY
    assert registered.object_name == SKILL_SCRIPT_OBJECT
    assert registered.definition.side_effect is SideEffect.NONE
    assert registered.definition.identity_mode is IdentityMode.DELEGATED
    assert registry.names() == (RUN_SKILL_SCRIPT,)


def test_the_tool_cannot_be_built_without_a_runner() -> None:
    """Six of the ten sandbox properties are declarations a container has to honour, so a tool
    that could be registered without a runner would be a tool running imported code under
    whatever isolation the machine happened to provide. `brain.tools.startup` makes the same
    choice about a row tool with no source.

    Delete this and a default runner appears, and the deployment with no sandbox registers an
    execution tool anyway."""
    fields = {f.name: f for f in dataclasses.fields(SkillScriptTool)}

    assert fields["runner"].default is dataclasses.MISSING
    assert fields["runner"].default_factory is dataclasses.MISSING
    assert fields["library"].default is dataclasses.MISSING
