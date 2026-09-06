"""The one tool that runs a skill's script, and an honest account of what its sandbox is not.

`brain.tools.skills` refuses a skill a capability, a grant, a scope and a leash rung, so
what a skill can do is whatever its tools would have done for this caller anyway. Its
*scripts* are the exception that has to be handled somewhere. A script is code that arrived
from outside the company, and running code is the one part of a skill that a review of prose
cannot make safe on its own.

**Single.** There is exactly one tool that runs a script, and a skill cannot register a tool
of its own. N execution entry points is N places to get the sandbox wrong, and the one
somebody adds in a hurry is the one without the leash: the confinement, the resource
ceilings and the empty environment are properties of the *path*, never of the script, so a
second path is a second set of them written by whoever needed a script to run that
afternoon. Three doors are already shut elsewhere and this module shuts the fourth.
`Skill` has no field for a runner, `parse_frontmatter` refuses an unknown key rather than
ignoring it, and `assert_object_not_reserved` refuses a second tool claiming the
skill-script object. What none of them catches is a second executor under a *different*
object, registered by somebody who never read this file, so `assert_single_execution_path`
asks the whole registry rather than one registration.

Rejected: enforcing "single" by convention, or by a comment on the registry. The rule
survives exactly as long as the person who wrote it, and a convention is not visible in the
diff that breaks it.

**Sandboxed, and precise about which half.** A sandbox has to deny the filesystem outside a
working directory, network egress, environment variables carrying credentials, subprocess
spawning, and unbounded CPU, memory and wall clock. `SANDBOX_PROPERTIES` lists all of them
with the mechanism that actually denies each, and it is honest in both directions: four are
enforced by code in this module, and the other six are declarations a container has to honour.
Saying "this is policy and the enforcement is elsewhere" is the correct move where it is
true, which is what `brain.ops.wiring` does about a compose file that has never run and what
`brain.ops.queue` does about a worker that does not exist. **This module never opens a
process, a socket or a file**, so nothing here can be read as a claim of isolation.

Rejected: pretending otherwise by naming the module after the container. A type called
`Sandbox` that starts nothing would be read as isolation by the next person to call it, and
they would stop looking for the part that is missing.

**Leashed.** A hard wall-clock deadline, a memory ceiling and an output size cap, and no
ambient credentials. A script that runs forever is an outage; a script that returns two
gigabytes of output is an outage in whatever called it. `ScriptLeash` refuses a per-call
value above the module ceiling, so a leash can be tightened and never loosened, which is the
composition `brain.tools.registry.rung_ceiling` uses and for the same reason: a default that
can be raised is inherited by every caller nobody considered.

Every ceiling is applied to **what came back**, never to what was asked for. That is the
rule `brain.tools.fetch` states about `Content-Length`, and it matters more here, because the
thing reporting the numbers is the runner, and a runner that could be trusted to honour the
deadline would not need one. An outcome that overran is a timeout whatever it says it is,
and its output is discarded rather than returned late.

**The invariant this leaf exists for: a skill script runs at the caller's reach and can
never widen it.** `E_run(caller, agent) = E(caller) ∩ agent_ceiling`, computed by calling
`EntitlementSet.intersect` and by nothing else, exactly as `brain.gate.invoke.invoke` and
`brain.ops.automation.flow_reach` do. The script itself receives no capability at all: it
holds no credential, reaches no network and is handed no registry, so it cannot call a tool.
The intersection is still computed and recorded on the run, because the day somebody adds a
callback into the tool API the ceiling it must run under is already decided and already
audited, rather than being invented by whoever adds it.

The other half is structural. `SandboxSpec` has nowhere to put a grant, a token, a
credential or a tool list, in the same way and for the same reason as `Skill` having nowhere
to put a capability and `SkillCard` having nowhere to put a body. A rule saying "do not pass
the credentials" holds until somebody needs one; a type with no field for them cannot carry
them.

`ScriptRequest` is the whole of what a model may say, and it names a skill and one of that
skill's own declared scripts. There is no field for an interpreter, a command line or an
environment, so the shell is never involved: arguments travel as a sequence and there is
nowhere to write a string for something else to split.

Not claimed: the container. No `brain.ops.wiring.Component` exists for a script sandbox and
no image is built, so `SkillScriptTool` requires a runner and has no default. A caller with
no runner registers no execution tool, which is the shape `brain.tools.startup` uses for a
row tool with no source: a tool that is present and cannot answer safely is worse than one
that is absent.

Task ids: M12.2.9
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.ops.automation import credential_leaks
from brain.tools.registry import SKILL_SCRIPT_OBJECT, TOOL_NAME_RE, ToolRegistry
from brain.tools.skills import (
    SKILL_NAME_RE,
    ImportedSkill,
    Skill,
    SkillError,
    SkillPin,
    execution_tool,
    resolve_pin,
    safe_archive_member,
)

# ------------------------------------------------------------------ written-down reasons

#: Why there is one execution tool rather than one per thing that wants to run something.
SINGLE_EXECUTION_PATH: Final = (
    "A skill's scripts run through one tool and no other. N execution entry points is N "
    "places to get the sandbox wrong, and the one somebody adds in a hurry is the one "
    "without the leash. The confinement, the resource ceilings and the empty environment "
    "are properties of the path rather than of the script, so a second path is a second "
    "set of them, written by whoever needed a script to run that afternoon."
)

#: The invariant the whole leaf serves, stated where a reader meets it.
A_SCRIPT_NEVER_WIDENS_REACH: Final = (
    "A skill script runs at the caller's reach and can never widen it. It receives no "
    "capability the caller lacks and it has nowhere to ask for one: the run is planned "
    "under E(caller) intersected with the agent ceiling, SandboxSpec has no field that "
    "could hold a grant, a token or a tool list, and the process is handed an environment "
    "built from a closed set of names rather than the one this server is running under."
)

#: Why the environment is assembled rather than filtered.
THE_ENVIRONMENT_IS_BUILT_NEVER_INHERITED: Final = (
    "A child process inherits its parent's environment unless somebody stops it, and this "
    "parent holds a database URL, a vault token and whatever else the deployment set. So "
    "the environment is built from an allowlist of names and then checked again for a "
    "credential by value, because a name check alone misses TMPDIR carrying a connection "
    "string and a value check alone misses a token that looks like an opaque word."
)

#: Why an outcome is measured rather than believed.
THE_CEILING_APPLIES_TO_WHAT_CAME_BACK: Final = (
    "Every ceiling is applied to what the runner returned, never to what the spec asked "
    "for. A runner that could be trusted to stop at the deadline would not need one, and a "
    "declared size is a claim the thing being measured makes about itself."
)


class SkillScriptError(SkillError):
    """A script could not be run, or an outcome could not be believed.

    A subclass of `SkillError` so the import and review path's existing handling covers it,
    and a distinct type so an operator can tell "this skill is malformed" from "the sandbox
    reported something impossible", which is the same split `UnsafeAddressError` makes.
    """


# ------------------------------------------------------------------ the leash

#: How long a script may run before the runner must kill it. Thirty seconds is generous for
#: the thing a script is for, which is shaping data a tool already fetched, and short enough
#: that a wedged run does not hold a worker slot for a noticeable fraction of an hour.
MAX_WALL_CLOCK_SECONDS: Final = 30

#: What the sandbox may allocate. Sized against `brain.ops.wiring`, where the host budget
#: lives: this is memory taken from a box that already runs somebody else's production, and
#: a sandbox with no ceiling is the neighbour's outage that module exists to prevent. There
#: is no `Component` entry for a script sandbox yet, and there will have to be one.
MAX_MEMORY_MIB: Final = 256

#: What a script may print. Sixty-four kilobytes is a large answer and a small file. The cap
#: exists because the output travels into a model's context and into whatever called the
#: tool, so an unbounded one is an outage in the caller rather than in the sandbox.
MAX_OUTPUT_BYTES: Final = 64 * 1024

#: How many arguments a script may be given, and how long each may be. Bounded for the
#: reason `brain.ops.queue.Job` bounds its own: past a certain length an argument is content
#: rather than a reference, and content belongs in a file the script reads, not in an argv
#: assembled by a model.
MAX_ARGUMENTS: Final = 16
MAX_ARGUMENT_CHARS: Final = 512


@dataclass(frozen=True)
class ScriptLeash:
    """The three numbers a runner must apply, and the ceiling none of them may exceed.

    A per-call leash may only tighten. `rung_ceiling` composes with `min` for the same
    reason and states it plainly: a value that can be raised at the call site is a ceiling
    written by whoever is in a hurry, and it is inherited from then on by every caller
    nobody considered. So the constructor refuses anything above the module constant rather
    than quietly clamping it, because a clamp is a silent disagreement between what a caller
    asked for and what it got.
    """

    wall_clock_seconds: int = MAX_WALL_CLOCK_SECONDS
    memory_mib: int = MAX_MEMORY_MIB
    output_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        ceilings = {
            "wall_clock_seconds": MAX_WALL_CLOCK_SECONDS,
            "memory_mib": MAX_MEMORY_MIB,
            "output_bytes": MAX_OUTPUT_BYTES,
        }
        for name, ceiling in ceilings.items():
            value = int(getattr(self, name))
            if value < 1:
                msg = (
                    f"leash {name} is {value}; zero is how 'unlimited' gets spelled in a "
                    "dataclass and a script with no ceiling is an outage in whatever is "
                    "waiting for it"
                )
                raise SkillScriptError(msg)
            if value > ceiling:
                msg = (
                    f"leash {name} is {value}, over the {ceiling} this module allows; a "
                    "leash may be tightened per call and never loosened, because a ceiling "
                    "that can be raised at the call site is not a ceiling"
                )
                raise SkillScriptError(msg)


# ------------------------------------------------------------------ what the process gets


class Egress(enum.StrEnum):
    """Whether the sandbox may reach the network. One member, and no second.

    The absence is the mechanism, as it is in `brain.ops.automation.StepKind`: a flow cannot
    express agent control flow because there is no member for it, and a script sandbox
    cannot express network access for the same reason. A skill script composes tools that
    have already fetched what they fetched, so an outbound connection from inside one is a
    path around the gate rather than a feature.

    `brain.ops.automation.EGRESS_ALLOWLIST` is the shape a second member would take, and it
    is deliberately not imported here. That allowlist exists because the automation canvas's
    whole job is plumbing between other people's systems; this thing's job is arithmetic on
    data the caller could already see.
    """

    DENIED = "denied"


#: Environment variable names a sandbox may be given. Everything else is refused rather than
#: dropped, because a variable that vanishes silently is one whoever set it believes arrived.
#: Nothing on this list identifies anybody or authorises anything.
#:
#: Note what an allowlist of names cannot see, which is why it is not the only check.
#: `TMPDIR` is here because a sandbox needs somewhere to write, and `TMPDIR` holding a
#: connection string is a full credential under a name no rule about names will ever flag.
PERMITTED_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"}
)


def build_environment(supplied: Mapping[str, str]) -> dict[str, str]:
    """The environment the process gets, assembled rather than filtered.

    Two independent checks, and neither alone is enough, which is the argument
    `brain.ops.automation.credential_leaks` makes about its own pair. The allowlist is over
    names, so it refuses `VAULT_TOKEN` without looking at what it holds. `credential_leaks`
    is over names *and* values, so it still catches an allowlisted name carrying a
    connection string, which is the case an allowlist cannot see.

    Neither check ever quotes a value. A checker that put what it found into its own error
    message would write the credential into whatever log reads that message, which is the
    failure it is looking for, one layer up.
    """
    unknown = sorted(name for name in supplied if name not in PERMITTED_ENV_NAMES)
    if unknown:
        msg = (
            f"the sandbox was handed {unknown}, which is not in {sorted(PERMITTED_ENV_NAMES)}. "
            f"{THE_ENVIRONMENT_IS_BUILT_NEVER_INHERITED}"
        )
        raise SkillScriptError(msg)
    leaks = credential_leaks(supplied)
    if leaks:
        msg = f"the sandbox was handed a credential: {list(leaks)}"
        raise SkillScriptError(msg)
    return {name: supplied[name] for name in sorted(supplied)}


@dataclass(frozen=True)
class SandboxSpec:
    """Everything the process is given, and there is nowhere here to give it anything else.

    **This type is the structural half of `A_SCRIPT_NEVER_WIDENS_REACH`.** There is no field
    for a capability, a grant, a token, a credential, a connection or a tool registry, in
    the same way and for the same reason as `Skill` having no field for a capability. A rule
    saying "do not pass the credentials to the sandbox" holds until the first person who
    needs one; a type that cannot hold one does not.

    `digest` is the approved digest of the skill, carried so the runner materialises the
    exact bytes a named person reviewed. A runner that unpacked whatever is on disk under
    that skill's name would be running something nobody approved, and the pin in
    `brain.tools.skills` would have been checking a version that never ran.

    `reach_hash` is the hash of `E(caller) ∩ agent_ceiling`. A hash and not a grant: it is
    the reach this run is attributed to, so an audit row can say what the run was allowed to
    be, and it is deliberately not something the process can spend.
    """

    skill: str
    digest: str
    #: A path relative to the skill's own folder. Checked twice; see `plan_run`.
    script: str
    #: argv, never a command line. There is nowhere here to write a string for a shell to
    #: split, which is the whole of the quoting problem removed by construction.
    arguments: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    leash: ScriptLeash = field(default_factory=ScriptLeash)
    network: Egress = Egress.DENIED
    reach_hash: str = ""


# --------------------------------------------------- what a sandbox must deny (M12.2.9)


@dataclass(frozen=True)
class SandboxProperty:
    """One thing a sandbox must deny, and the mechanism that actually denies it.

    `enforced_by` is required prose whether or not the property is enforced here, and that
    is the point of the type. `brain.ops.queue.SwapCandidate` refuses an alternative with no
    trigger on the grounds that it is a rumour; a sandbox property with no named enforcer is
    worse than a rumour, because it reads as a guarantee.
    """

    denies: str
    enforced_here: bool
    enforced_by: str

    def __post_init__(self) -> None:
        if not self.denies.strip() or not self.enforced_by.strip():
            msg = (
                "a sandbox property names what it denies and what denies it; one without "
                "an enforcer reads as a guarantee and is a sentence"
            )
            raise SkillScriptError(msg)


#: The honest table. Four rows are code in this module and the other six are declarations a
#: container has to honour, which is why `SkillScriptTool` cannot be built without a runner.
SANDBOX_PROPERTIES: Final[tuple[SandboxProperty, ...]] = (
    SandboxProperty(
        denies="running a file the reviewed skill did not declare, or a path leaving its folder",
        enforced_here=True,
        enforced_by=(
            "plan_run, which refuses a script that is not in Skill.scripts and re-runs "
            "safe_archive_member over the requested path"
        ),
    ),
    SandboxProperty(
        denies="running a skill that is unapproved, unpinned, or edited since it was pinned",
        enforced_here=True,
        enforced_by="resolve_pin, reused rather than restated",
    ),
    SandboxProperty(
        denies="an environment variable carrying a credential",
        enforced_here=True,
        enforced_by="build_environment: an allowlist of names, then credential_leaks by value",
    ),
    SandboxProperty(
        denies="output large enough to be an outage in the caller",
        enforced_here=True,
        enforced_by="accept_outcome, counting the bytes that came back rather than a declared size",
    ),
    SandboxProperty(
        denies="a run that outlives its deadline",
        enforced_here=False,
        enforced_by=(
            "the runner must kill the process at ScriptLeash.wall_clock_seconds. What is "
            "enforced here is only that an outcome arriving after the deadline is refused "
            "as a timeout rather than accepted late, which bounds the damage and not the run"
        ),
    ),
    SandboxProperty(
        denies="reading or writing a file outside the working directory",
        enforced_here=False,
        enforced_by=(
            "a mount namespace, or a container whose only writable path is the skill's own "
            "unpacked folder. Nothing in Python confines a process to a directory"
        ),
    ),
    SandboxProperty(
        denies="network egress",
        enforced_here=False,
        enforced_by=(
            "a network namespace with no interface. Egress.DENIED is the declaration the "
            "runner has to implement; this module opens no socket and closes none"
        ),
    ),
    SandboxProperty(
        denies="spawning a subprocess, or gaining privilege by executing one",
        enforced_here=False,
        enforced_by="a seccomp profile and no_new_privs, plus a pids cgroup limit",
    ),
    SandboxProperty(
        denies="unbounded memory",
        enforced_here=False,
        enforced_by="memory.max on the sandbox cgroup, set from ScriptLeash.memory_mib",
    ),
    SandboxProperty(
        denies="unbounded CPU",
        enforced_here=False,
        enforced_by="cpu.max on the sandbox cgroup. The wall clock deadline is not a CPU limit",
    ),
)


def unenforced_properties() -> tuple[SandboxProperty, ...]:
    """The properties this module declares and does not implement.

    Returned rather than logged, so an operator, a test and a readiness check all read the
    same list. A gap that only appears in a docstring is a gap nobody counts.
    """
    return tuple(p for p in SANDBOX_PROPERTIES if not p.enforced_here)


def sandbox_gaps() -> tuple[str, ...]:
    """Every declaration a runner has to honour, in words somebody can build against.

    The shape `brain.ops.wiring.budget_breaches` uses: all of them, every time, rather than
    the first. Whoever is building the container is building it once, and a checklist that
    reveals one item per run is a checklist that takes a week.
    """
    return tuple(f"{p.denies}: {p.enforced_by}" for p in unenforced_properties())


# ------------------------------------------------------------------ the request and the run


class ScriptRequest(BaseModel):
    """The whole of what a model may say about running a script.

    Note what is missing, because the absences are the argument. There is no interpreter, so
    a skill cannot choose what runs its file. There is no environment, so nothing a model
    says can reach the process's environment at all. There is no working directory, so a
    request cannot name where it would like to be. And `arguments` is a sequence rather than
    a string, so there is nowhere to write a command line for something else to split.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: str = Field(min_length=1, max_length=80)
    #: A path relative to the skill's own folder, and one the skill declared.
    script: str = Field(min_length=1, max_length=200)
    arguments: tuple[str, ...] = ()

    @field_validator("skill")
    @classmethod
    def _is_a_skill_name(cls, v: str) -> str:
        if not SKILL_NAME_RE.match(v):
            msg = f"skill name {v!r} is not a lowercase slug"
            raise ValueError(msg)
        return v

    @field_validator("arguments")
    @classmethod
    def _bounded_and_printable(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse an argv a model assembled out of content rather than references.

        A control character is refused as well as a long value. An argument carrying a
        newline is the shape that turns one argument into two wherever something later
        renders argv as a line, which is every log this run appears in.
        """
        if len(v) > MAX_ARGUMENTS:
            msg = f"{len(v)} arguments, over the {MAX_ARGUMENTS} limit"
            raise ValueError(msg)
        for argument in v:
            if len(argument) > MAX_ARGUMENT_CHARS:
                msg = (
                    f"an argument is {len(argument)} characters, over {MAX_ARGUMENT_CHARS}; "
                    "past that it is content rather than a reference"
                )
                raise ValueError(msg)
            if any(character < " " or character == "\x7f" for character in argument):
                msg = "an argument carries a control character"
                raise ValueError(msg)
        return v


class RunStatus(enum.StrEnum):
    """How a run ended. Closed, and there is deliberately no `UNKNOWN`.

    `brain.ops.queue.Verdict` gives the argument: a value meaning "not sure" is the one
    every ambiguous case gets, and those are exactly the cases somebody needs to look at.
    A runner that cannot say what happened says `KILLED`, which is a fact about the process
    rather than a shrug.
    """

    #: Ran to completion and exited zero.
    COMPLETED = "completed"
    #: Ran to completion and exited non-zero.
    FAILED = "failed"
    #: Did not finish inside the wall clock deadline.
    TIMED_OUT = "timed_out"
    #: Stopped from outside: the memory ceiling, the host, an operator.
    KILLED = "killed"


class ScriptRun(Entity):
    """One run of one script, tagged so the redactor can walk it.

    Every field is a fact about the run rather than about the business, which is why this is
    a typed result and not an opaque one. The output is the one field carrying anything a
    person would read, and it cannot contain something the caller may not see: the process
    reached no network, held no credential and was handed no tool, so its output is a
    function of the reviewed skill and of arguments the caller supplied.

    `truncated` is a separate field from an empty output on purpose. A caller that could not
    tell "the script printed nothing" from "the output was discarded" would report the first
    when the second happened, which is a wrong answer rather than a missing one.
    """

    status: RunStatus
    exit_code: int
    output: str
    output_bytes: int
    truncated: bool
    elapsed_seconds: float
    #: The hash of the reach this run was attributed to. See `SandboxSpec.reach_hash`.
    reach_hash: str


@dataclass(frozen=True)
class RunOutcome:
    """What a runner reports, before any of it is believed.

    Every number here is checked in `accept_outcome` against the leash the spec carried.
    That is not defensiveness about a colleague's code: the runner is the thing under load,
    the thing that gets replaced, and the thing that is wrong when a script wedges, so it is
    the one component whose self-report cannot be the record.
    """

    run_id: str
    status: RunStatus
    exit_code: int
    output: str
    elapsed_seconds: float


class ScriptRunner(Protocol):
    """Whatever actually starts a process. The one thing this module does not do.

    A protocol for the reason `brain.knowledge.rows.RowSource` and
    `brain.gate.answer_cache.AnswerStore` are protocols: the cases that matter here are the
    refusals, the timeout and the flood, and none of them is reachable in a test against a
    real sandbox. A module that spawned its own process could not be tested on the paths
    that are ever wrong.
    """

    def run(self, spec: SandboxSpec) -> RunOutcome: ...


class SkillLibrary(Protocol):
    """Where an agent's pinned, approved skill is looked up.

    Returns None for every reason a skill is not runnable here, and the collapsing is
    deliberate; see `SKILL_NOT_AVAILABLE`.
    """

    def pinned_skill(
        self, agent_id: str, skill_name: str
    ) -> tuple[SkillPin, ImportedSkill] | None: ...


#: One refusal for "no such skill", "not approved", "not pinned to this agent" and "edited
#: since it was pinned". Collapsed on purpose, and this is the one message in this module a
#: model ever sees.
#:
#: `brain.tools.skills` names the state in its refusals because its reader is an author or a
#: reviewer. The reader here is a model, and a model explains what it just tried: "the
#: hosting-expiry skill exists but has not been approved" is a sentence that reaches a person
#: through the one channel nobody audits, and it discloses both that the skill exists and
#: what it is called. `brain.gate.catalogue` makes the same argument about an unreachable
#: tool being absent rather than described and refused.
SKILL_NOT_AVAILABLE: Final = "no approved script by that name is available to this agent"


# ------------------------------------------------------------------ planning a run


def plan_run(
    skill: Skill,
    request: ScriptRequest,
    *,
    leash: ScriptLeash,
    environment: Mapping[str, str],
    reach_hash: str,
) -> SandboxSpec:
    """Everything decided before anything runs, or a refusal (M12.2.9).

    The script is checked twice and the order is the argument. `safe_archive_member` runs
    first, so `../../etc/shadow` is refused as a traversal rather than as an undeclared
    name, which is what an operator reading the message needs to know. Membership of
    `Skill.scripts` runs second and is the stronger check: it is the reviewed list, and a
    path that is in it has been through the same validation at import.

    Both, rather than either. Membership alone would rest on `Skill.scripts` having been
    validated at construction, which is true today and is a property of a different module;
    the path check alone would let a model run any file that happened to be in the folder,
    including one an archive dropped there that nobody reviewed.
    """
    safe_archive_member(request.script)
    if request.script not in skill.scripts:
        msg = (
            f"skill {skill.name!r} declares {list(skill.scripts)} and this run asked for "
            f"{request.script!r}; a script that is not in the reviewed list is a file "
            "nobody read, whatever else is sitting in the folder"
        )
        raise SkillScriptError(msg)

    return SandboxSpec(
        skill=skill.name,
        digest=skill.digest(),
        script=request.script,
        arguments=request.arguments,
        environment=build_environment(environment),
        leash=leash,
        network=Egress.DENIED,
        reach_hash=reach_hash,
    )


def _capped(output: str, ceiling: int) -> tuple[str, int, bool]:
    """The output, its size in bytes, and whether it was cut.

    Counted and cut in **bytes rather than characters**, because the ceiling is about what
    travels: a character cap lets four-byte codepoints spend four times the budget, and the
    thing being protected is a context window and a message queue, both of which are sized
    in bytes. Cutting on a byte boundary can split a codepoint, so the tail is decoded with
    errors ignored; losing one partial character off the end of an already truncated string
    is not worth a second decoder.
    """
    encoded = output.encode("utf-8")
    if len(encoded) <= ceiling:
        return output, len(encoded), False
    return encoded[:ceiling].decode("utf-8", errors="ignore"), len(encoded), True


def accept_outcome(spec: SandboxSpec, outcome: RunOutcome) -> ScriptRun:
    """Turn what the runner reported into the record of what happened (M12.2.9).

    Three things are decided here rather than taken on trust, and every one of them is the
    rule in `THE_CEILING_APPLIES_TO_WHAT_CAME_BACK`.

    **An outcome that overran the deadline is a timeout, whatever its status says, and its
    output is discarded.** Accepting a late result makes the deadline advisory: the run
    already held whatever it was holding for longer than it was allowed to, and returning
    the output teaches every caller that the number is a suggestion. The record says
    `truncated`, so nobody reads the empty output as a script that printed nothing.

    **A non-zero exit is a failure even when the runner calls it a success.** The exit code
    is the one fact a caller acts on, and a runner whose word is taken for it is a runner
    that can turn a crashed script into a clean empty answer.

    **The output is cut to the leash.** By bytes, and on what arrived.

    A nonsensical outcome is refused rather than recorded. An empty run id or a negative
    elapsed time is a runner that is broken, and writing the record anyway would put a run
    into the audit trail that cannot be pointed at.
    """
    if not outcome.run_id.strip():
        msg = "the runner reported a run with no id; a run nobody can point at is not a record"
        raise SkillScriptError(msg)
    if outcome.elapsed_seconds < 0:
        msg = (
            f"the runner reported {outcome.elapsed_seconds} seconds elapsed; a negative "
            "duration is a broken clock or a broken runner and neither is a result"
        )
        raise SkillScriptError(msg)

    if outcome.elapsed_seconds > spec.leash.wall_clock_seconds:
        return ScriptRun(
            entity=SKILL_SCRIPT_OBJECT,
            id=outcome.run_id,
            status=RunStatus.TIMED_OUT,
            exit_code=outcome.exit_code,
            output="",
            output_bytes=0,
            truncated=True,
            elapsed_seconds=outcome.elapsed_seconds,
            reach_hash=spec.reach_hash,
        )

    status = outcome.status
    if status is RunStatus.COMPLETED and outcome.exit_code != 0:
        status = RunStatus.FAILED

    output, produced, truncated = _capped(outcome.output, spec.leash.output_bytes)
    return ScriptRun(
        entity=SKILL_SCRIPT_OBJECT,
        id=outcome.run_id,
        status=status,
        exit_code=outcome.exit_code,
        output=output,
        output_bytes=produced,
        truncated=truncated,
        elapsed_seconds=outcome.elapsed_seconds,
        reach_hash=spec.reach_hash,
    )


# ------------------------------------------------------------------ the tool

#: What this tool asks for. `invoke:` rather than `read:`, because running a script is not
#: reading a record, and the verb is what an entitlement report is read by.
SCRIPT_CAPABILITY: Final = Capability(value=f"invoke:{SKILL_SCRIPT_OBJECT}")

#: The system the call goes to, which is this one. Matches the first segment of the tool's
#: name, as `assert_source_agrees` requires.
SCRIPT_SOURCE: Final = "skill"

SCRIPT_TOOL_DESCRIPTION: Final = (
    "Run one script belonging to an approved skill, in a sandbox with no network, no "
    "credentials and a hard time limit, and return what it printed"
)


@dataclass(frozen=True)
class SkillScriptTool:
    """The single execution tool (M12.2.9).

    **The runner is required and there is no default.** A default would be the moment this
    module stopped being honest: `SANDBOX_PROPERTIES` says six of ten properties are
    declarations a container has to honour, and a tool that could be registered without one
    would be a tool running imported code under whatever isolation the machine happened to
    provide. `brain.tools.startup` makes the same choice about a row tool with no source,
    with the same argument: a tool that is present and cannot answer safely is worse than a
    tool that is absent, because a missing tool is a gap somebody notices.

    The skill library is injected for the same reason, and it is a second seam rather than a
    convenience: the interesting cases are the unapproved skill, the edited one, and the one
    that is not there at all, and none of them is reachable through a real store.
    """

    library: SkillLibrary
    runner: ScriptRunner
    leash: ScriptLeash = field(default_factory=ScriptLeash)
    #: What the sandbox's environment is built from. Wiring, never a request: see
    #: `ScriptRequest`, which has no field a model could put a variable in.
    environment: Mapping[str, str] = field(default_factory=dict)

    def definition(self) -> ToolDefinition:
        """What the catalogue describes to a model.

        The name comes from `brain.tools.skills.execution_tool` rather than being written
        again here, so there is one answer to "which tool runs a script" and this module
        cannot drift from the one that reserves the object.

        `SideEffect.NONE` is the honest declaration and it is load-bearing on the sandbox
        being real: a process with no network, no credential and no writable path outside
        its own folder changes nothing the world can see. That is precisely why the runner
        has no default. `IdentityMode.DELEGATED` for the same kind of reason: there is no
        credential of either kind here, and DELEGATED is the half of the pair that does not
        claim a shared one is being spent. Declaring SERVICE would additionally demand a
        scope predicate under `assert_service_tool_is_scoped`, over rows that do not exist.
        """
        return ToolDefinition(
            name=execution_tool(),
            description=SCRIPT_TOOL_DESCRIPTION,
            entity=SKILL_SCRIPT_OBJECT,
            args_schema=ScriptRequest.model_json_schema(),
            required_capability=SCRIPT_CAPABILITY.value,
            side_effect=SideEffect.NONE,
            identity_mode=IdentityMode.DELEGATED,
            source=SCRIPT_SOURCE,
        )

    def handler(self) -> Callable[..., TypedResult[ScriptRun]]:
        """The callable a registry registers, bound to its library and its runner.

        A closure rather than a method, for the reason `brain.knowledge.rows.RowTool.reader`
        gives: the signature a registry and a dispatcher inspect then carries only what a
        model may pass, plus the wiring a dispatcher supplies. `agent_id`, `entitlement` and
        `agent_ceiling` are keyword-only and are not in `args_schema`, so a model has no way
        to name a different agent's pin or a wider ceiling than the one it was invoked
        under.
        """

        def run_skill_script(
            request: ScriptRequest,
            *,
            agent_id: str,
            entitlement: EntitlementSet,
            agent_ceiling: EntitlementSet,
            now: datetime | None = None,
        ) -> TypedResult[ScriptRun]:
            """Plan the run, hand the spec to the runner, and check what comes back.

            The reach is computed by calling `EntitlementSet.intersect` and by nothing else.
            There is no second implementation of the platform's central rule here, for the
            reason the repository's own invariant suite gives: the second copy is the one
            that is subtly wrong, and it is the one running under the imported code.
            """
            pinned = self.library.pinned_skill(agent_id, request.skill)
            if pinned is None:
                raise SkillScriptError(SKILL_NOT_AVAILABLE)
            pin, imported = pinned
            try:
                # Three refusals in one call: a renamed skill, one edited since it was
                # pinned, and one that is not executable. Reused rather than restated.
                skill = resolve_pin(pin, imported).skill
            except SkillError as exc:
                raise SkillScriptError(SKILL_NOT_AVAILABLE) from exc

            spec = plan_run(
                skill,
                request,
                leash=self.leash,
                environment=self.environment,
                reach_hash=entitlement.intersect(agent_ceiling).ent_hash(),
            )
            record = accept_outcome(spec, self.runner.run(spec))
            return TypedResult(
                records=(record,),
                source=SCRIPT_SOURCE,
                # No clock is read here, for the reason `brain.knowledge.rows.read_rows`
                # gives: a module that reads the clock cannot be tested at the boundary
                # that goes wrong.
                fetched_at=now.isoformat() if now is not None else "",
                truncated=record.truncated,
            )

        return run_skill_script


# --------------------------------------------------- exactly one execution path (M12.2.9)

#: Verbs in a tool name that mean "this starts something". Paired with a noun below rather
#: than used alone, because `xero.run_report` is an ordinary tool and refusing it would get
#: this rule edited out of the way.
EXECUTION_VERBS: Final[frozenset[str]] = frozenset(
    {"run", "exec", "execute", "eval", "spawn", "launch", "interpret", "sh", "bash", "python"}
)

#: Nouns that mean "something somebody supplied, that we are about to run".
CODE_NOUNS: Final[frozenset[str]] = frozenset(
    {
        "script",
        "scripts",
        "code",
        "command",
        "commands",
        "program",
        "snippet",
        "shell",
        "binary",
        "executable",
        "process",
        "subprocess",
        "sandbox",
        "python",
        "node",
    }
)


def _reads_as_execution(definition: ToolDefinition) -> bool:
    """Whether this tool looks like a way to run supplied code.

    Two independent readings, and the first is exact. **The object is the reliable one**:
    `SKILL_SCRIPT_OBJECT` is what a leash entry, a field policy rule and a slug collision are
    all written about, so a tool claiming it is an execution tool by definition.

    The second is a heuristic over the name, and it is honestly a heuristic, in the same way
    `brain.knowledge.rows.SQL_ARGUMENT_NAME_RE` refuses a parameter *named* for SQL. It
    catches the tool somebody adds in a hurry, which is the case in `SINGLE_EXECUTION_PATH`
    and the one that actually happens. It does not catch a determined author who calls their
    executor `reports.build_summary`, and nothing about a name ever will.

    A name that does not parse counts as execution. It cannot happen for a registered tool,
    since `assert_tool_name` ran first, and failing closed on the impossible case is cheaper
    than a branch that returns False for a shape nobody has thought about.
    """
    if definition.entity == SKILL_SCRIPT_OBJECT:
        return True
    match = TOOL_NAME_RE.match(definition.name)
    if match is None:
        return True
    return match.group("verb") in EXECUTION_VERBS and match.group("noun") in CODE_NOUNS


def execution_tools(registry: ToolRegistry | Sequence[ToolDefinition]) -> tuple[str, ...]:
    """Every registered tool that reads as a way to run supplied code, in name order.

    Takes anything iterable of definitions, which is what a `ToolRegistry` is, so a caller
    holding a proposed tool list can ask the same question before registering any of it.
    """
    return tuple(sorted(d.name for d in registry if _reads_as_execution(d)))


def assert_single_execution_path(registry: ToolRegistry | Sequence[ToolDefinition]) -> None:
    """Refuse a registry with a second way to run a skill's script (M12.2.9).

    `brain.tools.registry.assert_object_not_reserved` already refuses a second tool claiming
    the skill-script object, one registration at a time. This is the question that check
    cannot ask, because it only ever sees one tool: whether some *other* tool, under some
    other object, is also an executor. That is the tool somebody adds in a hurry, and it is
    the one without the leash.

    Both directions are checked. A second executor is refused, and so is the execution tool
    claiming the wrong object, which registration lets through: a `skill.run_script` that
    declares some other entity leaves the reserved object claimable by nobody, since the one
    name permitted to claim it is taken.

    Zero execution tools is not a finding. A deployment with no script sandbox registers no
    execution tool, which is the correct state today.
    """
    found = execution_tools(registry)
    extra = [name for name in found if name != execution_tool()]
    if extra:
        msg = (
            f"tools {extra} read as ways to run supplied code beside {execution_tool()!r}. "
            f"{SINGLE_EXECUTION_PATH}"
        )
        raise SkillScriptError(msg)
    for definition in registry:
        if definition.name == execution_tool() and definition.entity != SKILL_SCRIPT_OBJECT:
            msg = (
                f"{execution_tool()!r} declares object {definition.entity!r} rather than "
                f"{SKILL_SCRIPT_OBJECT!r}; the object is what a leash entry and a field "
                "policy rule are written about, and the reserved one would then be "
                "claimable by nothing, because the only name allowed to claim it is taken"
            )
            raise SkillScriptError(msg)
