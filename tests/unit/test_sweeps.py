"""The schema and registry sweeps.

Task ids: M0.5.4, M0.5.5, M0.5.6, M0.5.7, M0.5.8
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from brain.core import department
from brain.ops import sweeps


def test_sweep_failure_carries_every_finding() -> None:
    exc = sweeps.SweepFailure(["a", "b", "c"])
    assert exc.findings == ["a", "b", "c"]
    assert "3 finding" in str(exc)


# ------------------------------------------------------------- dispatcher
def test_main_rejects_an_unknown_sweep() -> None:
    assert sweeps.main(["not_a_sweep"]) == 2


def test_main_rejects_no_argument() -> None:
    assert sweeps.main([]) == 2


@pytest.mark.parametrize("name", sorted(sweeps.SWEEPS))
def test_every_registered_sweep_runs_and_passes_on_this_tree(name: str) -> None:
    """The repository must satisfy its own sweeps at all times, not just in CI.

    Parametrising over the registry rather than listing names means adding a sweep
    without wiring it into this test is impossible.
    """
    assert sweeps.main([name]) == 0


def test_main_reports_failure_as_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise sweeps.SweepFailure(["deliberate"])

    monkeypatch.setitem(sweeps.SWEEPS, "tool_registry", boom)
    assert sweeps.main(["tool_registry"]) == 1


# ------------------------------------------------------- db-backed sweeps
def test_db_sweeps_skip_cleanly_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer with no Postgres must not be blocked, but the skip has to be loud
    enough that nobody mistakes it for a pass. CI always sets DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sweeps.sweep_rls()
    sweeps.sweep_grant_isolation()


def test_needs_db_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/x")
    assert sweeps._needs_db() == "postgresql://localhost/x"
    monkeypatch.delenv("DATABASE_URL")
    assert sweeps._needs_db() is None


# --------------------------------------------------------------- grammars
def test_tool_name_grammar_accepts_source_dot_action() -> None:
    for good in ("laravel.get_client", "xero.list_invoices", "lark_base.read_row"):
        assert sweeps.TOOL_NAME_RE.match(good)


def test_tool_name_grammar_rejects_malformed_names() -> None:
    for bad in ("getClient", "Laravel.get", "laravel.", ".get_client", "laravel..get"):
        assert not sweeps.TOOL_NAME_RE.match(bad)


def test_task_id_grammar_matches_nested_ids() -> None:
    """Ids run to four levels — M0.1.1.1 and deeper — because the tracker does."""
    for good in ("M0.1", "M0.2.4", "M38.1.5", "M0.1.1.1", "M12.3.4.5.6"):
        assert sweeps.TASK_ID_RE.fullmatch(good), good


def test_task_id_grammar_ignores_bare_module_ids() -> None:
    """`M0` alone is a module, not a task, and must not be treated as a claim."""
    assert not sweeps.TASK_ID_RE.fullmatch("M0")
    assert not sweeps.TASK_ID_RE.fullmatch("M38")


def test_dependency_sweep_fails_when_the_lock_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """An unpinned dependency tree cannot be audited at all, so a missing lock is a
    failure rather than a skip."""
    monkeypatch.setattr(sweeps, "REPO", tmp_path)
    with pytest.raises(sweeps.SweepFailure, match="finding"):
        sweeps.sweep_dependencies()


# ------------------------------------------------------ slug collisions (M2.1.5)
def test_an_agent_named_after_a_scope_is_a_collision() -> None:
    """ "Grant Priya finance" has two meanings if a scope and an agent are both called
    finance, and the safe reading is not the one a resolver picks by declaration order."""
    found = department.check_slug_collisions(["finance"], ["finance"], [])
    assert len(found) == 1
    assert "finance" in str(found[0])


def test_names_that_differ_only_by_a_separator_collide() -> None:
    """Two names only a machine can tell apart are a collision in the interface even when
    the database is content with them."""
    assert department.check_slug_collisions(["client-ops"], [], ["client_ops"])


def test_distinct_names_are_not_a_collision() -> None:
    """A check that fires on correct input is a check somebody switches off."""
    assert department.check_slug_collisions(["finance"], ["reporter"], ["client"]) == []


def test_the_collision_sweep_is_registered_so_ci_can_run_it() -> None:
    """A sweep nothing invokes is a function, not a check. This one was written during M2
    and left unregistered, which is how it stayed unrun."""
    assert "slug_collisions" in sweeps.SWEEPS


def test_the_collision_sweep_reports_how_many_names_it_compared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The important one. Scopes and agents are rows that mostly do not exist yet, so this
    sweep compares very little today. Printing "ok" over an empty comparison is exactly the
    failure `sweep_traceability` had for its whole life: green in CI, checking nothing, and
    nobody looking again. Saying the counts out loud keeps the gap visible."""
    sweeps.sweep_slug_collisions()
    out = capsys.readouterr().out
    assert "scope(s)" in out
    assert "agent(s)" in out
    assert "tool object(s)" in out


# ------------------------------------------------ the deployment profiles (M0.4.1)
def test_the_lite_profile_and_the_deployed_compose_do_not_drift() -> None:
    """Two files that must agree, with nothing enforcing it, is how a profile quietly stops
    being the thing it is named after. Coolify deploys `docker-compose.yml` by that name and
    the architecture asks for the profiles to be named files an operator can point at, so
    both exist and this is what keeps them the same stack."""
    repo = Path(__file__).resolve().parents[2]
    base = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    lite = (repo / "docker-compose.lite.yml").read_text(encoding="utf-8")
    assert lite.endswith(base), (
        "docker-compose.lite.yml no longer contains docker-compose.yml verbatim; "
        "change one and copy it across, or the profiles describe different stacks"
    )


def test_the_lite_profile_says_why_there_is_no_full_one() -> None:
    """A missing file reads as an oversight. The full profile adds a worker and a
    session-mode pooler beside it, and there is no worker process yet for it to serve, so a
    full profile today would declare a container with no command."""
    repo = Path(__file__).resolve().parents[2]
    lite = (repo / "docker-compose.lite.yml").read_text(encoding="utf-8")
    assert "docker-compose.full.yml" in lite
    assert "worker" in lite


# ------------------------------------------------ staging shares nothing (M38.1.4.2)
def _compose(name: str) -> dict[str, Any]:
    """Parsed rather than grepped. A compose file is YAML, and a substring search over one
    passes happily on a line that is commented out."""
    import yaml

    repo = Path(__file__).resolve().parents[2]
    parsed: dict[str, Any] = yaml.safe_load((repo / name).read_text(encoding="utf-8"))
    return parsed


def test_staging_is_its_own_compose_project() -> None:
    """A separate project gives staging its own network, volume namespace and container
    names, so `docker compose down` in the wrong directory cannot reach production's
    database and a typo in a service name resolves to nothing rather than to the wrong
    thing. A profile inside the production file would share all three."""
    staging = _compose("docker-compose.staging.yml")
    production = _compose("docker-compose.yml")
    assert staging["name"] == "brain-staging"
    assert staging["name"] != production.get("name")


def test_the_two_stacks_share_no_volume() -> None:
    """The failure this prevents is not subtle and is entirely plausible: one command run
    from the wrong directory, against the volume holding real client data."""
    staging = set(_compose("docker-compose.staging.yml").get("volumes") or {})
    production = set(_compose("docker-compose.yml").get("volumes") or {})
    assert staging
    assert production
    assert not (staging & production)


def test_staging_does_not_reuse_a_production_credential() -> None:
    """If staging read `POSTGRES_PASSWORD`, the two databases would share a password and
    staging would stop being a boundary at all: anything that leaked from the weaker stack
    would open the stronger one."""
    import re

    repo = Path(__file__).resolve().parents[2]
    text = (repo / "docker-compose.staging.yml").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z_]+)", text))
    for shared in ("POSTGRES_PASSWORD", "APP_ROLE_PASSWORD", "APP_IMAGE"):
        assert shared not in referenced, f"staging reads production's {shared}"


def test_staging_does_not_call_itself_production() -> None:
    """`brain.config` requires different settings per environment, and more importantly the
    name decides how the app describes itself. A staging box reporting itself as production
    is how somebody investigates the wrong incident."""
    staging = _compose("docker-compose.staging.yml")
    app = staging["services"]["app"]
    assert app["environment"]["BRAIN_ENV"] == "staging"


def test_staging_pools_the_same_way_production_does() -> None:
    """The pooling mode is the single setting most likely to produce a bug that appears
    only in production. Prepared statements and session-level advisory locks both behave
    differently behind a transaction pooler, and both have already bitten this project.
    Staging exercising a different mode would be staging that cannot catch either."""
    staging = _compose("docker-compose.staging.yml")
    production = _compose("docker-compose.yml")
    left = staging["services"]["pgbouncer"]["environment"]["POOL_MODE"]
    right = production["services"]["pgbouncer"]["environment"]["POOL_MODE"]
    assert left == right == "transaction"


# ------------------------------------------------- the licence check (M0.5.7)
def test_the_licence_allowlist_is_actually_applied() -> None:
    """It was not. The allowlist was built inside `sweep_dependencies` and the only thing
    that touched it was the line printing its length; no package's licence was ever compared
    against it, and the sweep printed "ok" on every run.

    That is the third sweep in this tree found reporting success about a rule it was not
    applying. This asserts the check reaches a real answer rather than a reassuring one."""
    from brain.ops import sweeps as mod

    # Reading the metadata is not the same as acting on it, and the first version of this
    # test only proved the reading. Stub in a dependency nobody would accept and require the
    # sweep to fail: that is the behaviour, and it is what survived a mutation removing the
    # comparison entirely.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_installed_licences", lambda: {"something": "AGPL-3.0-only"})
    try:
        with pytest.raises(mod.SweepFailure) as caught:
            mod.sweep_dependencies()
    finally:
        monkeypatch.undo()
    assert "AGPL-3.0-only" in str(caught.value.findings)

    # And it passes on a set it should accept, so the check is not simply always failing.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_installed_licences", lambda: {"something": "MIT"})
    try:
        mod.sweep_dependencies()
    finally:
        monkeypatch.undo()

    real = mod._installed_licences()
    assert len(real) > 20, "no distributions were inspected at all"


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("Apache-2.0", True),
        ("LGPL-3.0-only", True),
        ("GPL-3.0-only", False),
        ("AGPL-3.0-only", False),
        # Real metadata from this dependency set. A plain set membership test refuses all
        # three, which is how a working allowlist gets deleted for being wrong.
        ("MIT OR Apache-2.0", True),
        ("MIT AND PSF-2.0", True),
        ("Apache-2.0 OR BSD-2-Clause", True),
        # An OR needs only one allowed operand; an AND needs all of them.
        ("MIT OR GPL-3.0-only", True),
        ("MIT AND GPL-3.0-only", False),
        # Refused rather than guessed at, because a wrong answer here is silent.
        ("(MIT OR Apache-2.0) AND ISC", False),
        ("Apache-2.0 WITH LLVM-exception", False),
        ("MIT AND Apache-2.0 OR ISC", False),
        ("", False),
    ],
)
def test_an_spdx_expression_is_read_rather_than_matched_as_a_string(
    expression: str, allowed: bool
) -> None:
    """The two halves that matter: an OR is a choice, an AND is a conjunction. Getting them
    the wrong way round admits a GPL dependency that declared `MIT AND GPL-3.0-only`."""
    from brain.ops.sweeps import licence_is_allowed

    assert licence_is_allowed(expression) is allowed


def test_the_copyleft_licences_that_would_reach_a_client_are_not_allowed() -> None:
    """This is a client-hosted product: the client receives and runs the software. GPL and
    AGPL reach into that in a way MIT and Apache do not, and AGPL reaches it over a network
    too. LGPL is allowed and is a separate, argued decision recorded beside the allowlist -
    it holds only while nothing here modifies or vendors an LGPL dependency."""
    from brain.ops.sweeps import ALLOWED_LICENCES

    for refused in ("GPL-3.0-only", "GPL-2.0-only", "AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1"):
        assert refused not in ALLOWED_LICENCES
