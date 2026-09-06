"""The CI workflow, read as data rather than trusted as configuration.

A workflow file is the one place in this repository where deleting a line makes everything
pass. Remove the invariants step and CI goes green faster; remove a sweep and the sweep
still exists, still works, and never runs again. Neither shows up in a diff review as
anything more alarming than a shorter file.

So the gates are asserted here, from the same source of truth the code uses: the sweep
names come from `brain.ops.sweeps.SWEEPS`, not from a list typed out twice. Adding a sweep
and forgetting to wire it up now fails a test rather than passing silently, which is what
happened to `one_tool_grammar` on the day it was written.

Task ids: M12.1.6, M38.1.2.1
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.ops.sweeps import SWEEPS

REPO = Path(__file__).resolve().parents[2]


def _workflow() -> dict[Any, Any]:
    """Parsed, not grepped. A substring search over YAML passes happily on a commented-out
    line, and a disabled gate is exactly the thing this file exists to catch.

    The key type is `Any` rather than `str` because of one key: GitHub's `on:` is the
    boolean `True` after a YAML 1.1 parse, so a `dict[str, Any]` would be a lie about what
    comes back.
    """
    parsed: dict[Any, Any] = yaml.safe_load(
        (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    return parsed


def _all_run_commands() -> str:
    """Every `run:` in the workflow, concatenated. What CI actually executes."""
    parts: list[str] = []
    for job in _workflow()["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                parts.append(str(step["run"]))
    return "\n".join(parts)


# ------------------------------------------------------------------ the gates
@pytest.mark.parametrize(
    ("gate", "command"),
    [
        ("format", "ruff format --check"),
        ("lint", "ruff check"),
        ("types", "mypy"),
        ("invariants", "pytest tests/invariants"),
        ("unit tests", "pytest --cov"),
    ],
)
def test_ci_runs_every_gate(gate: str, command: str) -> None:
    """Named individually so a red test says which gate went missing rather than that the
    workflow changed. Deleting any one of these makes CI faster and makes it mean less."""
    assert command in _all_run_commands(), f"CI no longer runs the {gate} gate"


@pytest.mark.parametrize("name", sorted(SWEEPS))
def test_ci_runs_every_registered_sweep(name: str) -> None:
    """Parametrised from the registry itself, so a new sweep arrives here already failing.

    This is the failure it was written for: `one_tool_grammar` was added, tested, and
    registered, and CI would have kept passing without ever running it. A sweep that does
    not run in CI is a sweep that protects the person who wrote it and nobody else.
    """
    assert f"brain.ops.sweeps {name}" in _all_run_commands(), (
        f"the {name!r} sweep is registered but CI never runs it"
    )


# ------------------------------------------------- the tool registry sweep (M12.1.6)
def _sweep_invocations() -> dict[str, str]:
    """Sweep name to the step that runs it, over live shell lines only.

    `_all_run_commands` above concatenates whole `run:` blocks, and a `run:` block may
    contain shell comments: several in this workflow do. A substring found there names a
    sweep that CI discusses rather than one CI executes, and the difference is invisible in
    the assertion. This strips comments first, so what comes back is what the runner would
    actually execute.
    """
    invoked: dict[str, str] = {}
    for job_name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            live: list[str] = []
            for line in str(step.get("run", "")).splitlines():
                if line.lstrip().startswith("#"):
                    continue
                live.append(line.split(" # ")[0])
            for sweep in re.findall(
                r"python\s+-m\s+brain\.ops\.sweeps\s+([a-z_]+)", "\n".join(live)
            ):
                invoked[sweep] = f"{job_name}: {step.get('name', '(unnamed step)')}"
    return invoked


def test_the_tool_registry_sweep_is_run_by_a_live_step_and_not_merely_mentioned() -> None:
    """The tool registry is the thing that decides a name means one capability, and the sweep
    is what stops two names meaning the same one. A sweep that exists, works, and never runs
    protects the person who wrote it and nobody else.

    Deleting this leaves the sweep asserted only by a substring over the whole workflow, which
    a shell comment inside any `run:` block can satisfy. That is not hypothetical here: this
    workflow's run blocks carry comments naming the sweeps they sit beside, and a step
    commented out with its explanation left behind would read as a live gate.
    """
    invoked = _sweep_invocations()
    assert "tool_registry" in invoked, (
        f"CI does not execute the tool registry sweep; it executes {sorted(invoked)}"
    )
    # The step names a sweep the registry actually has. A typo would fail CI loudly, but only
    # on the run after the one that introduced it, and only if anybody reads the log.
    assert "tool_registry" in SWEEPS


def test_the_sweeps_run_on_a_change_rather_than_after_it_has_landed() -> None:
    """A sweep is a gate or it is a report, and which one it is depends entirely on when it
    runs. The job holding it must not be conditioned on anything, and the workflow must fire
    on a pull request: a registry sweep that runs only on `main` finds the collision after the
    merge that introduced it, when the fix is somebody else's problem.

    Deleting this lets `if: github.ref == 'refs/heads/main'` be added to the sweeps job during
    a slow CI week, which makes every check here pass while the gate stops being one.
    """
    job = _workflow()["jobs"]["sweeps"]
    assert "if" not in job, f"the sweeps job is conditional: {job.get('if')!r}"
    triggers = _workflow()[True]
    assert "pull_request" in triggers


def test_the_invariant_suite_runs_before_anything_expensive() -> None:
    """The invariants encode the rules that must never break, and they need no database.
    Running them in the first job means a permission rule failing is a red check in ninety
    seconds rather than after a Docker build."""
    jobs = _workflow()["jobs"]
    assert "static" in jobs
    static_steps = "\n".join(str(s.get("run", "")) for s in jobs["static"]["steps"])
    assert "pytest tests/invariants" in static_steps
    # And the sweeps wait for it, so a broken invariant does not spend a runner on sweeps
    # that are about to be irrelevant.
    assert jobs["sweeps"].get("needs") == "static"


def test_the_workflow_runs_on_a_pull_request_and_not_only_on_main() -> None:
    """A gate that runs only after the merge is a report, not a gate. `on` parses as the
    boolean True in YAML 1.1, which is why this reads the key that way rather than by the
    name it has in the file."""
    triggers = _workflow()[True]
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]


# --------------------------------- the sweeps that need a database, and did not get one
#
# `sweep_rls` and `sweep_grant_isolation` skip when DATABASE_URL is unset. The standalone
# sweeps job has no database service, so for the whole life of this pipeline the row-level
# security sweep printed a skip and exited 0. It had never run once.


DB_DEPENDENT_SWEEPS = ("rls", "grant_isolation")


def _stack_run_commands() -> str:
    """Every `run:` in the job that starts the whole stack, which is the job with a database."""
    job = _workflow()["jobs"]["stack"]
    return "\n".join(str(step["run"]) for step in job.get("steps", []) if "run" in step)


@pytest.mark.parametrize("sweep", DB_DEPENDENT_SWEEPS)
def test_a_sweep_that_needs_a_database_is_run_where_one_exists(sweep: str) -> None:
    """The bug: a security check that exits 0 without checking anything is worse than one
    that is absent, because the absent one is visible.

    A table shipped without row-level security is one forgotten WHERE clause from returning
    every row to every caller, and it looks correct in every test that happens to use a wide
    principal. That is precisely what this sweep exists to catch, and it had never run.

    Asserted against the *stack* job specifically, not against the workflow as a whole. The
    sweeps job invokes the same command and skips, so a test over every `run:` in the file
    passes while nothing is checked - which is the shape of the original bug, one level up.

    Delete this and the sweep can quietly return to skipping everywhere."""
    assert f"brain.ops.sweeps {sweep}" in _stack_run_commands()


def test_the_job_that_runs_the_database_sweeps_actually_has_a_database() -> None:
    """The other half, and the one that makes the test above mean something. Asserting a
    command appears in a job proves nothing if that job has no database: the sweep would skip
    there too, exactly as it did before.

    The stack job's whole purpose is starting the real compose stack, so what is checked here
    is that it still does, and that the sweeps run against it rather than beside it."""
    stack = _stack_run_commands()

    assert "docker compose up" in stack, "the stack job no longer starts a stack"
    for sweep in DB_DEPENDENT_SWEEPS:
        assert f"docker compose exec -T app python -m brain.ops.sweeps {sweep}" in stack, (
            f"the {sweep} sweep is not run inside the container that has DATABASE_URL"
        )


@pytest.mark.parametrize("job", ["tests", "sweeps"])
def test_a_job_that_reads_commit_history_is_given_some(job: str) -> None:
    """**CI failed three pushes in a row for a reason that could not happen on a laptop.**

    `actions/checkout` defaults to `fetch-depth: 1`, a shallow clone holding exactly one
    commit. Everything in this repository that asks what has been closed reads `Closes:`
    trailers out of `git log`, so in CI it was reading one commit and concluding almost
    nothing had ever been closed.

    That is worse than the failing test it produced. The traceability sweep runs in the
    sweeps job, and its three advisory counts are computed from that same empty set: they
    printed reassuring numbers in CI while verifying nothing at all about commit claims. A
    check that reports "ok" over no inputs is the exact defect this repository keeps finding,
    and here it was the CI gate itself.

    Asserted per job rather than globally, because the other four have no business reading
    history and a blanket rule would slow every checkout for no reason.

    Delete this and the depth silently returns to one: every test still passes locally, where
    the clone is complete, and the gate quietly stops gating."""
    steps = _workflow()["jobs"][job]["steps"]
    checkouts = [s for s in steps if "actions/checkout" in str(s.get("uses", ""))]

    assert checkouts, f"the {job} job does not check anything out"
    for step in checkouts:
        depth = (step.get("with") or {}).get("fetch-depth")
        assert depth == 0, (
            f"the {job} job checks out at depth {depth!r}, so git log sees one commit and "
            "every claim read from history is computed against an empty set"
        )


def test_the_skip_message_claims_nothing_about_where_it_does_run() -> None:
    """It used to read "(CI always sets it)", which was false in the one place anybody read
    it: printed by CI, in a job with no database.

    A message that asserts a check happens elsewhere is worse than silence, because it
    answers the question a reader would otherwise go and ask. Deleting this test lets the
    next reassuring parenthetical go in unchallenged."""
    from brain.ops.sweeps import SKIPPED_FOR_WANT_OF_A_DATABASE

    assert "always" not in SKIPPED_FOR_WANT_OF_A_DATABASE
    assert "nothing was checked" in SKIPPED_FOR_WANT_OF_A_DATABASE
