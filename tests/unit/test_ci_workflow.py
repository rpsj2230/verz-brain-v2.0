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
