"""Progress computed from git history, and the pages that serve it.

Task ids: M38.3.1, M38.3.2, M38.3.3, M38.3.4, M38.3.5, M38.3.6
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import docs_routes, status
from brain.app import Settings, create_app

WBS = {
    "wave_names": {"0": "Foundation", "1": "The gate"},
    "modules": [
        {"id": "M0", "name": "Foundation", "wave": 0, "leaf_ids": ["M0.1.1", "M0.1.2", "M0.2.1"]},
        {"id": "M1", "name": "Identity", "wave": 1, "leaf_ids": ["M1.1.1", "M1.1.2"]},
    ],
}


def git(cwd: Path, *args: str) -> None:
    """The status generator's only input is git history, so the tests build real repos."""
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.txt").write_text("1")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "M0.1.1: first thing")
    (tmp_path / "f.txt").write_text("2")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "second thing\n\nAlso closes M0.1.2 and M1.1.1.")
    return tmp_path


# ------------------------------------------------------------- extraction
def test_ids_are_read_from_subject_and_body(repo: Path) -> None:
    """A commit closing eight leaves lists them in the body rather than cramming them
    into a 72-character subject, so both are read."""
    found, _ = status.closed_task_ids(repo)
    assert found == {"M0.1.1", "M0.1.2", "M1.1.1"}


def test_recent_commits_are_recorded_newest_first(repo: Path) -> None:
    _, recent = status.closed_task_ids(repo)
    assert recent[0]["subject"] == "second thing"
    assert "M1.1.1" in recent[0]["closed"]


def test_a_commit_naming_no_task_is_not_listed(repo: Path) -> None:
    git(repo, "commit", "--allow-empty", "-m", "tidy up")
    _, recent = status.closed_task_ids(repo)
    assert all(r["subject"] != "tidy up" for r in recent)


def test_a_directory_that_is_not_a_repo_yields_nothing(tmp_path: Path) -> None:
    """No history must mean zero progress, never a crash and never a wrong number."""
    found, recent = status.closed_task_ids(tmp_path)
    assert found == set()
    assert recent == []


# ---------------------------------------------------------------- rollup
def test_progress_counts_only_what_commits_closed(repo: Path) -> None:
    s = status.build_status(repo, WBS)
    assert s.total == 5
    assert s.done == 3
    assert s.percent == 60.0


def test_a_parent_id_closes_nothing(repo: Path) -> None:
    """Changed 2026-09-04 after finding the number inflated.

    An ancestor id used to close every leaf beneath it, which is what an honest commit
    for a large piece of work looks like - and exactly why it was dangerous. A commit
    saying M0.6 closed connector cassettes that were never written. Nothing warned.
    """
    git(repo, "commit", "--allow-empty", "-m", "M0.2: the whole subtree")
    s = status.build_status(repo, WBS)
    assert "M0.2.1" not in s.done_task_ids


def test_a_leaf_id_does_not_close_a_sibling(repo: Path) -> None:
    s = status.build_status(repo, WBS)
    assert "M1.1.2" not in s.done_task_ids


def test_waves_roll_up_separately(repo: Path) -> None:
    s = status.build_status(repo, WBS)
    by_wave = {w.wave: w for w in s.waves}
    assert (by_wave[0].done, by_wave[0].total) == (2, 3)
    assert (by_wave[1].done, by_wave[1].total) == (1, 2)


def test_current_wave_is_the_first_unfinished_one(repo: Path) -> None:
    assert status.build_status(repo, WBS).current_wave == 0


def test_current_wave_is_none_when_everything_is_done(repo: Path) -> None:
    """None means finished. Reporting a current wave forever would be a quiet lie.

    Note the ids: a bare `M0` is a module, not a task, and deliberately closes nothing.
    """
    git(repo, "commit", "--allow-empty", "-m", "M0.1.1 M0.1.2 M0.2.1 M1.1.1 M1.1.2 everything")
    s = status.build_status(repo, WBS)
    assert s.done == s.total
    assert s.current_wave is None


def test_leaf_ids_are_derived_the_way_the_renderer_numbers_them(tmp_path: Path) -> None:
    """If these drifted, a commit closing M0.2.4 would tick a different box in the
    tracker than the one the status page counts."""
    wbs = {
        "modules": [
            {
                "id": "M0",
                "name": "x",
                "wave": 0,
                "tasks": [{"n": "group", "s": [{"n": "sub", "k": ["a", "b"]}, "leaf"]}],
            }
        ]
    }
    p = tmp_path / "wbs.json"
    p.write_text(json.dumps(wbs), encoding="utf-8")
    assert status.load_wbs(p)["modules"][0]["leaf_ids"] == ["M0.1.1.1", "M0.1.1.2", "M0.1.2"]


# ------------------------------------------------------------- regression
def test_a_one_line_commit_message_is_counted(repo: Path) -> None:
    r"""Regression, found by test_ids_are_read_from_subject_and_body on 2026-09-04.

    Python counts \x1c through \x1f as whitespace, so `entry.strip()` ate the trailing
    unit separator. A commit with an empty body — every one-line message, and so most
    commits — then split into three fields instead of four and was dropped with no error
    anywhere. Progress simply read low.
    """
    git(repo, "commit", "--allow-empty", "-m", "M1.1.2: one line, no body")
    found, recent = status.closed_task_ids(repo)
    assert "M1.1.2" in found
    assert recent[0]["subject"] == "M1.1.2: one line, no body"


def test_a_body_containing_the_separator_does_not_split_the_record(repo: Path) -> None:
    """maxsplit=3 keeps the body whole, so pasted output cannot corrupt the parse."""
    git(repo, "commit", "--allow-empty", "-m", "M0.2.1: x\n\nlog said a\x1fb about M1.1.2")
    found, _ = status.closed_task_ids(repo)
    assert {"M0.2.1", "M1.1.2"} <= found


# ----------------------------------------------------------------- routes
@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "status.json").write_text(
        json.dumps(
            {
                "commit": "abc1234",
                "total": 100,
                "done": 25,
                "percent": 25.0,
                "current_wave": 1,
                "waves": [
                    {"wave": 1, "name": "The gate", "total": 100, "done": 25, "percent": 25.0}
                ],
                "modules": [],
                "done_task_ids": ["M0.1.1"],
                "recent": [
                    {"sha": "abc1234", "subject": "M0.1.1: a thing", "closed": "M0.1.1", "at": ""}
                ],
            }
        ),
        encoding="utf-8",
    )
    (docs / "tracker.html").write_text("<h1>tracker</h1>", encoding="utf-8")
    monkeypatch.setattr(docs_routes, "DOCS", docs)
    app: FastAPI = create_app(Settings(env="production"))
    with TestClient(app) as c:
        yield c


def test_status_endpoint_serves_the_baked_file(client: TestClient) -> None:
    body = client.get("/api/status.json").json()
    assert body["percent"] == 25.0
    assert body["done_task_ids"] == ["M0.1.1"]


def test_status_is_never_cached(client: TestClient) -> None:
    """A cached status page is a page that can show yesterday's progress."""
    assert client.get("/api/status.json").headers["cache-control"] == "no-store"


def test_index_shows_the_percentage_and_the_commit(client: TestClient) -> None:
    text = client.get("/build").text
    assert "25.0%" in text
    assert "abc1234" in text
    assert "The gate" in text


def test_tracker_is_served(client: TestClient) -> None:
    assert client.get("/build/tracker").status_code == 200


def test_a_missing_document_is_a_404_not_a_crash(client: TestClient) -> None:
    """Architecture is not in this fixture. A missing doc must not take the app down."""
    assert client.get("/build/architecture").status_code == 404


def test_docs_pages_are_reachable_in_production(client: TestClient) -> None:
    """Unlike /docs these carry no company data and stay on in production — the whole
    point is that progress is visible without asking anyone."""
    assert client.get("/build").status_code == 200
    assert client.get("/api/status.json").status_code == 200
