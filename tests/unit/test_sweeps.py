"""The schema and registry sweeps.

Task ids: M0.5.4, M0.5.5, M0.5.6, M0.5.7, M0.5.8
"""

from __future__ import annotations

import pytest

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
