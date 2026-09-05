"""The wave report is derived from commits, so it can be checked rather than believed.

Task ids: M38.2.1.6
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.wave_report import build_wave_report, render_markdown

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

WBS = {
    "wave_names": {"0": "Foundation", "1": "The gate"},
    "modules": [
        {
            "id": "M90",
            "name": "Alpha",
            "wave": 0,
            "leaf_ids": ["M90.1.1", "M90.1.2", "M90.1.3"],
            # M90.1.3 belongs to wave 1 despite its module sitting in wave 0.
            "leaf_waves": {"M90.1.3": 1},
        },
        {
            "id": "M91",
            "name": "Beta",
            "wave": 1,
            "leaf_ids": ["M91.1.1", "M91.1.2"],
            "leaf_waves": {},
        },
    ],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo whose commits close known ids.

    Module ids are numeric (`M90`, `M91`) because that is the real grammar: `M` then
    digits. An earlier version of this fixture used `MA` and `MB`, which match no task id
    at all, so every commit closed nothing and half these tests passed by agreeing that
    nothing had happened.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "M90.1.1: the first thing")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "Something in the gate\n\nCloses: M91.1.1, M90.1.3")
    return tmp_path


# --------------------------------------------------------------- which wave
def test_a_report_covers_only_the_wave_it_names(repo: Path) -> None:
    """A report that quietly included a neighbouring wave would show progress the wave did
    not make, and the wave nobody is working on would appear to be moving."""
    report = build_wave_report(repo, WBS, 0, now=NOW)
    leaves = {leaf for m in report.modules for leaf in (*m.closed, *m.open)}
    assert leaves == {"M90.1.1", "M90.1.2"}


def test_a_leaf_assigned_to_a_later_wave_is_reported_there(repo: Path) -> None:
    """The per-leaf override exists because M38's pipeline is wave 0 while its wave-three
    exit criterion is not. A report ignoring it would put undoable work in wave 0."""
    report = build_wave_report(repo, WBS, 1, now=NOW)
    leaves = {leaf for m in report.modules for leaf in (*m.closed, *m.open)}
    assert "M90.1.3" in leaves
    assert "M90.1.1" not in leaves


def test_every_leaf_is_either_closed_or_open_and_never_both(repo: Path) -> None:
    """The two counts have to sum to the total, or the headline percentage is arithmetic
    on numbers that do not describe the same set."""
    for wave in (0, 1):
        report = build_wave_report(repo, WBS, wave, now=NOW)
        for module_line in report.modules:
            assert not set(module_line.closed) & set(module_line.open)
        assert report.closed_count + report.open_count == report.total


# ------------------------------------------------------------- what closed
def test_a_task_counts_as_closed_only_when_a_commit_named_it(repo: Path) -> None:
    """The property that makes the report checkable. Anything else is a report about what
    somebody remembers doing."""
    report = build_wave_report(repo, WBS, 0, now=NOW)
    alpha = next(m for m in report.modules if m.module == "M90")
    assert alpha.closed == ("M90.1.1",)
    assert alpha.open == ("M90.1.2",)


def test_a_closes_trailer_counts_as_a_claim(repo: Path) -> None:
    """Most commits close several ids and only one fits in a subject line."""
    report = build_wave_report(repo, WBS, 1, now=NOW)
    closed = {leaf for m in report.modules for leaf in m.closed}
    assert closed == {"M91.1.1", "M90.1.3"}


def test_only_commits_touching_this_wave_are_listed(repo: Path) -> None:
    """A wave report listing every commit in the repo is a git log with a title on it."""
    report = build_wave_report(repo, WBS, 0, now=NOW)
    assert [c["subject"] for c in report.commits] == ["M90.1.1: the first thing"]


def test_the_commits_are_cited_so_a_reader_can_check_them(repo: Path) -> None:
    report = build_wave_report(repo, WBS, 1, now=NOW)
    assert report.commits
    for commit in report.commits:
        assert len(commit["sha"]) == 7


# ---------------------------------------------------------------- overdue
def test_nothing_is_overdue_when_no_due_dates_were_supplied(repo: Path) -> None:
    """A report that guesses a deadline and then reports against its own guess is worse
    than a report with a gap in it."""
    assert build_wave_report(repo, WBS, 0, now=NOW).overdue == []


def test_an_open_task_past_its_date_is_overdue(repo: Path) -> None:
    report = build_wave_report(repo, WBS, 0, now=NOW, due_dates={"M90.1.2": "2026-09-01"})
    assert report.overdue == [("M90.1.2", "2026-09-01")]


def test_a_closed_task_is_never_overdue(repo: Path) -> None:
    """It is done. Reporting it as late would put permanent noise at the top of every
    report, and the top of the report is the part that has to stay worth reading."""
    report = build_wave_report(repo, WBS, 0, now=NOW, due_dates={"M90.1.1": "2026-09-01"})
    assert report.overdue == []


def test_overdue_items_come_oldest_first(repo: Path) -> None:
    wbs = {**WBS, "modules": [{**WBS["modules"][0], "leaf_ids": ["M90.1.1", "M90.1.2", "M90.1.4"]}]}  # type: ignore[index]
    report = build_wave_report(
        repo, wbs, 0, now=NOW, due_dates={"M90.1.2": "2026-09-03", "M90.1.4": "2026-08-20"}
    )
    assert [leaf for leaf, _ in report.overdue] == ["M90.1.4", "M90.1.2"]


# ----------------------------------------------------------------- rendering
def test_the_report_leads_with_what_still_needs_doing(repo: Path) -> None:
    """The opposite of how these are usually written, and deliberate: what is open is the
    part somebody has to act on, and what closed can be read later."""
    text = render_markdown(
        build_wave_report(repo, WBS, 0, now=NOW, due_dates={"M90.1.2": "2026-09-01"})
    )
    assert text.index("Overdue") < text.index("Still open") < text.index("Closed, by module")


def test_a_complete_wave_does_not_claim_open_work(repo: Path) -> None:
    """A section headed "still open" with nothing under it reads as a bug."""
    wbs = {**WBS, "modules": [{**WBS["modules"][0], "leaf_ids": ["M90.1.1"], "leaf_waves": {}}]}  # type: ignore[index]
    report = build_wave_report(repo, wbs, 0, now=NOW)
    assert report.is_complete
    assert "Still open" not in render_markdown(report)


def test_the_headline_states_both_numbers(repo: Path) -> None:
    """A report giving only the achievement is one nobody trusts twice."""
    text = render_markdown(build_wave_report(repo, WBS, 0, now=NOW))
    assert "1 of 2" in text
    assert "1 still open" in text
