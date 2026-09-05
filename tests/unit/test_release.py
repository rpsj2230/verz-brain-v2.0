"""Tagging a wave. Every test is about a tag that would say something untrue.

Task ids: M38.2.1.1
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brain.release import ReleaseRefusedError, plan_release, tag_release

WBS = {
    "wave_names": {"0": "Foundation"},
    "modules": [
        {
            "id": "M90",
            "name": "Alpha",
            "wave": 0,
            "leaf_ids": ["M90.1.1", "M90.1.2"],
            "leaf_waves": {},
        }
    ],
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with an `origin/main` that matches HEAD, so nothing is unpushed.

    The remote is a bare clone rather than a stub, because `plan_release` asks git a real
    question about it and a fake would only prove the fake behaves.
    """
    import json

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")

    (work / "docs").mkdir()
    (work / "docs" / "wbs.json").write_text(json.dumps(WBS), encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "M90.1.1: the first thing")

    bare = tmp_path / "remote.git"
    _git(work, "clone", "--bare", "-q", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "fetch", "-q", "origin")
    _git(work, "branch", "--set-upstream-to=origin/main", "main")
    return work


# ------------------------------------------------------- what the tag contains
def test_a_plan_carries_the_report_and_the_counts(repo: Path) -> None:
    """A tag called v0.3.1 says the order releases went out and nothing else. This one
    carries the report, so `git show wave-0` answers what was in the release without a
    separate changelog that drifts."""
    plan = plan_release(repo, 0)
    assert plan.tag == "wave-0"
    assert "M90" in plan.message
    assert plan.closed == 1
    assert plan.still_open == 1


def test_an_incomplete_wave_says_so_in_the_tag_itself(repo: Path) -> None:
    """A wave shipped early is a real decision. A wave shipped early and recorded as though
    it were finished is how a plan stops describing what happened."""
    plan = plan_release(repo, 0)
    assert not plan.is_complete
    assert "still open" in plan.message
    assert plan.message.startswith("# Wave 0 tagged with")


def test_tagging_writes_an_annotated_tag_a_reader_can_open(repo: Path) -> None:
    plan = plan_release(repo, 0)
    tag_release(repo, plan)
    assert _git(repo, "tag", "--list", "wave-0") == "wave-0"
    assert "M90.1.1" in _git(repo, "tag", "-l", "-n99", "wave-0")


# --------------------------------------------------------------- the refusals
def test_a_dirty_tree_is_refused(repo: Path) -> None:
    """A tag points at a commit. With uncommitted changes, the thing that was tested is not
    the thing being tagged, and the difference is invisible afterwards."""
    (repo / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(ReleaseRefusedError, match="working tree has changes"):
        plan_release(repo, 0)


def test_an_unpushed_commit_is_refused(repo: Path) -> None:
    """A tag on a commit nobody else has is a release nobody else can fetch."""
    (repo / "later.txt").write_text("later", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "M90.1.2: something later")
    with pytest.raises(ReleaseRefusedError, match="not been pushed"):
        plan_release(repo, 0)


def test_an_existing_tag_is_never_moved(repo: Path) -> None:
    """Moving a tag rewrites what a previous release meant, and anybody who already fetched
    it keeps a different history from anybody who fetches now."""
    tag_release(repo, plan_release(repo, 0))
    with pytest.raises(ReleaseRefusedError, match="already exists"):
        plan_release(repo, 0)


def test_planning_writes_nothing(repo: Path) -> None:
    """The checks are readable, testable and printable without a repository being modified.
    A function that checked and acted in one pass could only answer "what would this do" by
    doing it."""
    before = _git(repo, "tag", "--list")
    plan_release(repo, 0)
    assert _git(repo, "tag", "--list") == before


def test_tagging_does_not_push(repo: Path) -> None:
    """Pushing is the irreversible half: once a tag is on the remote, other people and other
    machines have it. Leaving that to a person means a mistaken tag is a local mistake."""
    tag_release(repo, plan_release(repo, 0))
    remote_tags = _git(repo, "ls-remote", "--tags", "origin")
    assert "wave-0" not in remote_tags
