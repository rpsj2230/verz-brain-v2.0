"""Progress computed from git history, and the pages that serve it.

Task ids: M38.3.1, M38.3.2, M38.3.3, M38.3.4, M38.3.5, M38.3.6,
M38.3.1.1, M38.3.1.3, M38.3.1.4, M38.3.2.1, M38.3.2.2, M38.3.2.3, M38.3.2.4, M38.3.2.5

Deliberately not claimed: M38.3.1.2, which asks for the status file to be written back
to the repository on each merge. It is generated in CI and baked into the image, and
committing it back would store a derived value in the tree it is derived from - a bot
commit on every merge, each of which is itself a merge. See the note on it below.
"""

from __future__ import annotations

import json
import os
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
    git(tmp_path, "commit", "-m", "second thing\n\nCloses: M0.1.2 M1.1.1\n")
    return tmp_path


# ------------------------------------------------------------- extraction
def test_ids_are_read_from_subject_and_body(repo: Path) -> None:
    """A commit closing eight leaves lists them on a Closes: trailer rather than cramming
    them into a 72-character subject, so both places are read."""
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


def test_closed_today_counts_the_days_work(repo: Path) -> None:
    """M38.3.1.4. Both commits in the fixture are made now, so all three of their leaves
    are today's. Deleting this leaves the number free to be anything: it is displayed
    prominently and checked by nobody, which is the worst combination for a figure a person
    reads to decide whether to ask what happened."""
    assert status.build_status(repo, WBS).closed_today == 3


def test_a_commit_from_before_today_is_not_counted(repo: Path) -> None:
    """The boundary, and it has to be real rather than assumed. Without this the count is
    "every leaf ever closed" on a repository whose history is short, which is exactly the
    situation now and exactly when nobody would notice."""
    # Dated at commit time rather than amended afterwards. `--since` reads the *committer*
    # date, and `--date` sets only the author date, so an amend that looks like it moved the
    # commit leaves the filter reading the original moment.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "old work\n\nCloses: M0.2.1"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": "2026-09-01T09:00:00+00:00",
            "GIT_AUTHOR_DATE": "2026-09-01T09:00:00+00:00",
        },
    )
    s = status.build_status(repo, WBS)
    # Counted as done, because it is. Not counted as today's, because it is not.
    assert "M0.2.1" in s.done_task_ids
    assert s.closed_today == 3


def test_next_up_names_unclosed_leaves_in_the_current_wave(repo: Path) -> None:
    """M38.3.1.4. In plan order, not id order: the WBS lists leaves in the sequence they
    are meant to be done, and re-sorting answers a different question."""
    s = status.build_status(repo, WBS)
    assert s.next_up
    assert all(leaf not in s.done_task_ids for leaf in s.next_up)


def test_next_up_is_empty_when_the_current_wave_is_finished(repo: Path) -> None:
    """Otherwise the page suggests work in a wave that has none left, which reads as the
    plan being wrong rather than the page being wrong."""
    git(repo, "commit", "--allow-empty", "-m", "rest of wave 0\n\nCloses: M0.2.1")
    s = status.build_status(repo, WBS)
    assert s.current_wave == 1
    assert all(leaf.startswith("M1") for leaf in s.next_up), s.next_up


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
    git(repo, "commit", "--allow-empty", "-m", "M0.2.1: x\n\nlog said a\x1fb\n\nCloses: M1.1.2\n")
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
                "closed_today": 3,
                "next_up": ["M1.2.3", "M1.2.4"],
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


def test_the_page_says_how_much_closed_today(client: TestClient) -> None:
    """M38.3.2.3. The number a person actually wants when they open this: not the total,
    which barely moves, but whether anything happened since they last looked."""
    assert "3 closed today" in client.get("/build").text


def test_a_day_with_nothing_closed_says_so_rather_than_showing_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is a real answer and is the one worth showing. A page that only ever displays
    movement cannot display a stall, and hiding the line on a quiet day means a stalled
    project looks identical to a project nobody has checked."""
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
                "closed_today": 0,
                "next_up": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(docs_routes, "DOCS", docs)
    with TestClient(create_app(Settings(env="production"))) as c:
        text = c.get("/build").text
    assert "nothing closed today yet" in text
    assert "this wave is finished" in text


def test_the_page_says_what_is_next(client: TestClient) -> None:
    """M38.3.2.3. "What is next" without anybody opening the tracker and reading down it.
    In plan order rather than id order, because the WBS lists leaves in the sequence they
    are meant to be done and `M12.1.10` sorts before `M12.1.2` as a string."""
    text = client.get("/build").text
    assert "Next up" in text
    assert "M1.2.3" in text
    assert text.index("M1.2.3") < text.index("M1.2.4")


def test_the_page_names_the_commit_it_was_built_from(client: TestClient) -> None:
    """M38.3.2.4, and the half that always worked. A status page that cannot say which
    version produced it is a page nobody can check against the running system."""
    assert "abc1234" in client.get("/build").text


def test_the_page_is_reachable_without_signing_in(client: TestClient) -> None:
    """M38.3.2.5. The whole point: progress needs no meeting, which means it needs no
    account either. Built with `env="production"`, where the interactive API docs are off,
    so this asserts the build pages are deliberately exempt rather than accidentally open."""
    for path in ("/build", "/build/tracker", "/api/status.json"):
        assert client.get(path).status_code == 200, path
    assert client.get("/docs").status_code == 404


def test_the_status_file_is_never_written_by_hand(client: TestClient) -> None:
    """M38.3.1.3. There is no endpoint, flag or environment variable that marks a task
    done. The only way a leaf becomes closed is a commit on main naming it, which means the
    page cannot show progress that does not exist.

    Asserted structurally over the module rather than by trying every URL: a route that
    accepted a task id would have to exist as a function, and this is the check that fails
    when somebody adds one for convenience during a demo."""
    import inspect

    source = inspect.getsource(docs_routes)
    for verb in ("@router.post", "@router.put", "@router.patch", "@router.delete"):
        assert verb not in source, f"{verb} on the build pages: progress could be set by hand"


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


# ------------------------------------------------------------- regression
def test_body_prose_does_not_claim_anything(repo: Path) -> None:
    """Regression, found by reading my own status page on 2026-09-04.

    A commit body listed ten ids under "Deliberately NOT claimed" and the parser counted
    all ten. It has no concept of negation and cannot be given one: "not M0.6.5",
    "M0.6.5 is not done" and "blocked: M0.6.5" are identical to a scanner, and the
    failure is silent and in the direction that flatters.

    The rule is now positional, not semantic.
    """
    git(repo, "commit", "--allow-empty", "-m", "tidy up\n\nStill outstanding: M1.1.2 and M0.2.1.")
    found, _ = status.closed_task_ids(repo)
    assert "M1.1.2" not in found
    assert "M0.2.1" not in found


def test_a_closes_trailer_claims(repo: Path) -> None:
    git(repo, "commit", "--allow-empty", "-m", "some work\n\nCloses: M1.1.2, M0.2.1\n")
    found, _ = status.closed_task_ids(repo)
    assert {"M1.1.2", "M0.2.1"} <= found


def test_the_subject_still_claims(repo: Path) -> None:
    """The common case stays as it was: one id, in the subject, where it is visible in
    every log listing."""
    git(repo, "commit", "--allow-empty", "-m", "M1.1.2: a thing")
    found, _ = status.closed_task_ids(repo)
    assert "M1.1.2" in found


def test_claimed_ids_reads_subject_and_trailer_only() -> None:
    ids = status.claimed_ids(
        "M0.1.1: subject",
        "Body mentions M9.9.9 in prose.\n\nCloses: M0.1.2 M0.1.3\n",
    )
    assert ids == {"M0.1.1", "M0.1.2", "M0.1.3"}


# ------------------------------------- the open count is the open count (M38.3.2.3)
def test_the_needs_count_stops_at_the_answered_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to count every `## ` heading in the file, so the badge on the status page was
    the number of items ever *raised*: it read twenty-two while two were actually open, and
    it could only ever go up.

    A count that never falls is a count nobody acts on, because answering something does not
    change it. Deleting this test lets the badge drift back into being a running total, which
    looks identical to a working one until you notice it has never gone down."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "needs-rupash.md").write_text(
        "# Needs Rupash\n\n# Open\n\n## 24. still open\n\n## 25. also open\n\n"
        "# Answered\n\n## 23. decided\n\n## 22. decided\n\n## 21. decided\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(docs_routes, "DOCS", docs)
    assert docs_routes._needs_count() == 2


def test_a_document_with_nothing_answered_yet_counts_everything() -> None:
    """The boundary in the other direction. Before anything is decided there is no
    `# Answered` heading, and every item is open - so a reader that broke on a missing
    heading would report zero, which is the most reassuring wrong answer available."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp)
        (docs / "needs-rupash.md").write_text(
            "# Needs Rupash\n\n# Open\n\n## 1. a\n\n## 2. b\n", encoding="utf-8"
        )
        original = docs_routes.DOCS
        docs_routes.DOCS = docs
        try:
            assert docs_routes._needs_count() == 2
        finally:
            docs_routes.DOCS = original


def test_the_real_document_reports_what_its_own_first_paragraph_says() -> None:
    """The badge and the sentence at the top of the page are the same fact asked twice, and
    they are written in different places. This is what stops them disagreeing - which they
    did, for as long as the count was a running total."""
    import re

    from brain.docs_routes import DOCS, NEEDS_FILE, _needs_count

    text = (DOCS / NEEDS_FILE).read_text(encoding="utf-8")
    stated = re.search(r"\*\*(\d+) items are open", text)
    assert stated, "the document no longer states how many items are open"
    assert int(stated.group(1)) == _needs_count()


# ------------------------------- the two pages that show progress must agree
#
# `/build` reads `docs/status.json`, written by `brain.status`. `/build/tracker` is
# `docs/tracker.html`, written by `docs/wbs/render.js`. Two programs, two languages, one
# WBS, and both shown to the client on the same site.


def _tracker_leaves_per_wave() -> dict[int, int]:
    """Every leaf checkbox in the tracker, counted by the wave the page assigns it."""
    import collections
    import re

    html = (Path(__file__).resolve().parents[2] / "docs" / "tracker.html").read_text(
        encoding="utf-8"
    )
    found = re.findall(r'class="cb"[^>]*data-wave="(\d+)"', html)
    counted = collections.Counter(int(w) for w in found)
    return dict(counted)


def _wbs_leaves_per_wave() -> dict[int, int]:
    """The partition the WBS itself declares: a leaf's own wave, else its module's.

    Derived from the source rather than read from `docs/status.json`, which is generated at
    build time and not committed - the first version of this test read that file, passed on
    my machine and failed in CI with `FileNotFoundError`, which is the whole reason a test
    should compare against the source and not against another derived artefact.

    It is also the stronger comparison. `status.json` is one program's output; the WBS is
    what both programs claim to be reading.
    """
    import collections

    wbs = status.load_wbs(Path(__file__).resolve().parents[2] / "docs" / "wbs.json")
    counted: collections.Counter[int] = collections.Counter()
    for module in wbs["modules"]:
        module_wave = int(module.get("wave", 0))
        leaf_waves = module.get("leaf_waves", {})
        for leaf in module["leaf_ids"]:
            counted[int(leaf_waves.get(leaf, module_wave))] += 1
    return dict(counted)


def test_the_tracker_and_the_status_page_put_each_leaf_in_the_same_wave() -> None:
    """The bug this exists for was visible on the live site and the owner found it, not a
    test: `/build` said wave 0 was 110/112 and `/build/tracker` said 110/129, from one WBS.

    The tracker bucketed each leaf by its *module's* wave. A leaf can sit later than its
    module - M38's delivery pipeline is wave 0, and "what is live after wave 3" cannot be
    done before wave 3 - so seventeen leaves nobody could start sat in wave 0's denominator,
    and wave 0 could never reach 100%. `render.js` already had `waveOfLeaf` for exactly this
    and used it for the schedule sizing, just not for the rollup shown to a reader.

    Compared per wave rather than on the totals, because two different partitions of 1150
    leaves sum to 1150 either way. Delete this and the two pages drift again, silently, and
    the person who notices is the client."""
    assert _tracker_leaves_per_wave() == _wbs_leaves_per_wave()


def test_the_generated_status_agrees_with_the_wbs_about_the_waves() -> None:
    """The other half of the same property, and the half that decides what `/build` shows.

    `build_status` is run rather than its output read, because `docs/status.json` is written
    at build time and is not in the repository. Running it needs only the WBS and git, both
    of which are here."""
    repo = Path(__file__).resolve().parents[2]
    wbs = status.load_wbs(repo / "docs" / "wbs.json")

    built = status.build_status(repo, wbs)

    assert {w.wave: w.total for w in built.waves} == _wbs_leaves_per_wave()


def test_every_leaf_appears_exactly_once_on_the_tracker() -> None:
    """The denominator has to be the whole plan. A partition that dropped a leaf would still
    let the test above pass if both sides dropped it, so this checks the total against the
    WBS itself rather than against the other page."""
    wbs = status.load_wbs(Path(__file__).resolve().parents[2] / "docs" / "wbs.json")
    expected = sum(len(m["leaf_ids"]) for m in wbs["modules"])

    assert sum(_tracker_leaves_per_wave().values()) == expected


# ------------------------------------------------- taking back a claim that was not true
#
# A leaf can be closed by mistake, because an id mentioned in a subject line closes it and a
# subject line is a sentence about something else. `M0.4.2` is the case: a commit whose
# subject read "M0.4.2: mount the Postgres volume at the path 18 expects" closed the leaf for
# `docker-compose.full.yml`, a file that deliberately does not exist.


def _message(subject: str, trailer: str) -> str:
    """A commit message with a real blank line between subject and trailer.

    Built with an explicit newline rather than written as a literal, because these
    messages reach the file through a shell heredoc and a backslash-n has been eaten
    three separate times in this repository. The trap is in the tooling, not the test."""
    return subject + chr(10) + chr(10) + trailer


def test_a_reopens_trailer_takes_an_id_back() -> None:
    """Without this there is no way to correct a false claim except rewriting history, and
    the plan shows work as delivered that nobody did."""
    assert status.reopened_ids("Reopens: M0.4.2") == {"M0.4.2"}


def test_a_subject_line_cannot_reopen_anything() -> None:
    """Trailer only, and deliberately asymmetric with closing. A subject mention is what
    causes the mistake this exists to correct, so letting a subject reopen would hand the
    same loose mechanism the power to un-deliver work as well.

    Delete this and "Reopens: ..." in a subject silently starts removing leaves."""
    assert status.reopened_ids("") == set()
    assert status.claimed_ids("Reopens: M0.4.2", "") == {"M0.4.2"}


def test_the_newest_statement_about_an_id_is_the_one_that_counts(tmp_path: Path) -> None:
    """`git log` walks newest first, so the first commit to mention an id settles it.

    Without that, an older `Closes:` puts back what a newer `Reopens:` took away, and the
    correction works on the day it is written and silently stops working the moment anybody
    looks further back. That is the failure mode worth testing, because it is invisible: the
    count is simply wrong again, with nothing saying so.

    A real repository rather than a fake, because the ordering being tested is git's."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a").write_text("1", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "M1.1.1: the original claim")
    (repo / "a").write_text("2", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", _message("Take it back", "Reopens: M1.1.1"))

    closed, _ = status.closed_task_ids(repo)

    assert "M1.1.1" not in closed, "a newer Reopens was overridden by an older claim"


def test_a_claim_after_a_reopen_closes_it_again(tmp_path: Path) -> None:
    """The other direction, so reopening cannot become permanent. Work that was wrongly
    marked done and is then genuinely done has to be closeable, or the correction becomes a
    worse error than the one it fixed."""
    import subprocess

    repo = tmp_path / "r2"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a").write_text("1", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "M1.1.1: the original claim")
    (repo / "a").write_text("2", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", _message("Take it back", "Reopens: M1.1.1"))
    (repo / "a").write_text("3", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", _message("Actually build it", "Closes: M1.1.1"))

    closed, _ = status.closed_task_ids(repo)

    assert "M1.1.1" in closed
