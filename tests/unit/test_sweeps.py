"""The schema and registry sweeps.

Task ids: M0.5.4, M0.5.5, M0.5.6, M0.5.7, M0.5.8
"""

from __future__ import annotations

from pathlib import Path

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
