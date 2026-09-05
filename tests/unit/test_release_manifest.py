"""What a build says is in it, and every way that statement could be false.

Against a real repository rather than a stub. The manifest is assembled by asking git
questions, and a fake git would only prove the fake answers them.

Task ids: M38.1.3.5
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.ops.release_manifest import (
    ManifestError,
    ReleaseManifest,
    build_manifest,
    choose_span,
    read_manifest,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a tagged release and three commits after it.

    One of the three closes nothing, one closes a leaf, and one mentions a leaf only in
    prose. That mix is the whole point: the manifest must contain exactly one id.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")

    _commit(work, "a.txt", "M90.1.1: the thing before the release")
    _git(work, "tag", "-a", "wave-0", "-m", "wave 0")

    _commit(work, "b.txt", "Tidy a comment")
    _commit(work, "c.txt", "Add the registry\n\nCloses: M90.2.1")
    _commit(work, "d.txt", "Explain a deferral\n\nM90.2.2 waits on a decision from Rupash.")
    return work


# --------------------------------------------------------- what the manifest contains
def test_the_manifest_names_the_tasks_closed_since_the_last_release(repo: Path) -> None:
    """The question anybody asks after a deploy is "when did this start happening", and the
    answer is a list of task ids. A record carrying only a SHA answers a different
    question, and answering it needs the repository the server does not have."""
    manifest = build_manifest(repo, now=NOW)
    assert manifest.task_ids == ("M90.2.1",)
    assert manifest.previous_commit == "wave-0"
    assert manifest.commit == _git(repo, "rev-parse", "HEAD")


def test_an_id_mentioned_in_prose_is_not_in_the_manifest(repo: Path) -> None:
    """The same rule the status page uses, and it is imported rather than reimplemented.
    Two parsers for "what does this commit close" is two answers, and the one that
    disagrees with the tracker is the one printed on the deployment record."""
    assert "M90.2.2" not in build_manifest(repo, now=NOW).task_ids


def test_a_release_before_the_first_tag_carries_no_task_ids(tmp_path: Path) -> None:
    """A first release listing nine hundred ids is not information, it is a dump of the
    plan. Deleting this makes the first deployment record unreadable, which is the one
    people will look at hardest."""
    work = tmp_path / "fresh"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _commit(work, "a.txt", "Start\n\nCloses: M90.1.1")
    assert build_manifest(work, now=NOW).task_ids == ()


def test_ids_are_sorted_and_deduplicated(repo: Path) -> None:
    """Two builds of the same release must produce the same manifest. A list differing only
    in order reads as a different release to anything comparing them."""
    _commit(repo, "e.txt", "Again\n\nCloses: M90.2.1, M90.1.9")
    manifest = build_manifest(repo, now=NOW)
    assert manifest.task_ids == ("M90.1.9", "M90.2.1")


def test_a_named_starting_point_overrides_the_tag(repo: Path) -> None:
    """A release cut from something other than the last tag is a normal thing to do, and
    the manifest has to describe the span that was actually built."""
    head_1 = _git(repo, "rev-parse", "HEAD~1")
    assert build_manifest(repo, since=head_1, now=NOW).task_ids == ()


# ------------------------------------------------------------- choosing the span
def test_a_release_tag_wins_over_everything_else(repo: Path) -> None:
    """A release is what shipped since the last release. Once a tag exists, no caller's
    idea of "previous" may override it, or a release describes only its final push."""
    head_1 = _git(repo, "rev-parse", "HEAD~1")
    assert choose_span(repo, candidate=head_1) == ("wave-0", "release")


def test_with_no_tag_the_span_is_this_commit_and_says_so(tmp_path: Path) -> None:
    """Honestly a smaller claim than a release, which is why the kind travels with it. It
    exists because otherwise the manifest carries no ids at all until somebody cuts the
    first release, and a mechanism nobody has seen work is a mechanism discovered broken at
    the first release that matters."""
    work = tmp_path / "untagged"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _commit(work, "a.txt", "First")
    _commit(work, "b.txt", "Second closing something\n\nCloses: M90.1.1")

    since, kind = choose_span(work)
    assert kind == "commit"
    assert since == _git(work, "rev-parse", "HEAD~1")

    manifest = build_manifest(work, now=NOW)
    assert manifest.task_ids == ("M90.1.1",)
    assert manifest.span == "commit"


def test_a_root_commit_has_no_span_at_all(tmp_path: Path) -> None:
    """No tag and no parent. The honest answer is "none" rather than an empty list that
    reads as "this release closed nothing"."""
    work = tmp_path / "root"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _commit(work, "a.txt", "First\n\nCloses: M90.1.1")
    assert choose_span(work) == ("", "none")
    assert build_manifest(work, now=NOW).span == "none"


def test_the_kind_is_on_the_manifest_so_an_empty_list_is_not_ambiguous(repo: Path) -> None:
    """An empty `task_ids` means two very different things - "this release closed nothing"
    and "there was nothing to look at" - and they look identical without this field.

    Deleting it makes a deployment record that closed nothing indistinguishable from one
    built before any of this worked."""
    assert build_manifest(repo, now=NOW).span == "release"


def test_the_all_zero_sha_is_refused_and_falls_through_to_the_parent(tmp_path: Path) -> None:
    """Forty zeroes is what GitHub sends when there is no before. It looks like a real
    argument and resolves to nothing, so it must not be taken at face value."""
    work = tmp_path / "zeros"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _commit(work, "a.txt", "First")
    _commit(work, "b.txt", "Second")
    since, kind = choose_span(work, candidate="0" * 40)
    assert (since, kind) == (_git(work, "rev-parse", "HEAD~1"), "commit")


def test_a_commit_this_clone_does_not_have_falls_through_to_the_parent(tmp_path: Path) -> None:
    """The shallow-checkout case. A sha real elsewhere and absent here would make `git log`
    fail and break the build on a bookkeeping detail."""
    work = tmp_path / "absent"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _commit(work, "a.txt", "First")
    _commit(work, "b.txt", "Second")
    since, kind = choose_span(work, candidate="f" * 40)
    assert (since, kind) == (_git(work, "rev-parse", "HEAD~1"), "commit")


# ------------------------------------------------------------------- the round trip
def test_a_manifest_survives_being_written_and_read(repo: Path, tmp_path: Path) -> None:
    """It is written by CI and read by a different process on a different machine. A round
    trip that lost the timezone would put two deployments in the wrong order, which is the
    one thing the record exists to get right."""
    original = build_manifest(repo, now=NOW)
    path = tmp_path / "RELEASE.json"
    path.write_text(original.to_json(), encoding="utf-8")
    assert read_manifest(path) == original


def test_no_manifest_is_not_an_error(tmp_path: Path) -> None:
    """A developer running from a checkout has git and no manifest. Treating absence as a
    failure would make the container refuse to start for the one person who could fix it."""
    assert read_manifest(tmp_path / "nothing.json") is None


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("not json", "{"),
        ("not an object", "[]"),
        ("short commit", '{"commit": "abc1234", "built_at": "2026-09-05T12:00:00+00:00"}'),
        ("no commit", '{"built_at": "2026-09-05T12:00:00+00:00"}'),
        ("no timestamp", '{"commit": "' + "a" * 40 + '"}'),
        ("naive timestamp", '{"commit": "' + "a" * 40 + '", "built_at": "2026-09-05T12:00:00"}'),
    ],
)
def test_a_manifest_that_cannot_be_believed_is_refused(label: str, text: str) -> None:
    """Loud rather than lenient, and that is the opposite of the usual advice about parsing.
    A manifest is written by one program and read by another across a version boundary,
    which is exactly the situation where being liberal in what you accept produces a
    deployment record with an empty commit in it - and a deployment record nobody can tie
    to a commit is worse than none, because it looks like evidence."""
    with pytest.raises(ManifestError):
        ReleaseManifest.from_json(text)


def test_a_naive_timestamp_is_named_as_the_problem() -> None:
    """The refusal has to say which of six things was wrong, or the person reading a failed
    build guesses."""
    text = '{"commit": "' + "a" * 40 + '", "built_at": "2026-09-05T12:00:00"}'
    with pytest.raises(ManifestError, match="timezone"):
        ReleaseManifest.from_json(text)
