"""Tagging a wave, so a release names what it contains rather than when it happened.

A tag called `v0.3.1` tells somebody the order releases went out and nothing else. A tag
whose message is the wave report tells them which tasks closed, which commits closed them,
and what was still open at the time. `git show wave-1` then answers "what was in this" without
anybody having kept a separate changelog that drifts.

Three refusals, and each is about a tag that would be a lie.

**A dirty tree.** A tag points at a commit. If the working tree has changes, the thing that
was tested is not the thing being tagged, and the difference is invisible afterwards.

**An unpushed commit.** A tag on a commit nobody else has is a release nobody else can get.

**A tag that already exists.** Moving one silently rewrites what a previous release meant,
and anybody who fetched the old one keeps a different history from anybody who fetches now.

An incomplete wave *can* be tagged, because shipping before a wave closes is a real and
sometimes correct decision. It cannot be tagged quietly: the count goes in the message, so
the tag says so forever.

Task ids: M38.2.1.1
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from brain.status import load_wbs
from brain.wave_report import build_wave_report, render_markdown


class ReleaseRefusedError(Exception):
    """Raised when tagging would produce a tag that does not mean what it says."""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603  arguments are literals from this module
        ["git", *args],  # noqa: S607  git on PATH, as everywhere else in this repo
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseRefusedError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass(frozen=True)
class ReleasePlan:
    """What would be tagged, assembled before anything is written.

    Separated from the tagging so the checks can be read, tested and printed without a
    repository being modified. A function that checked and acted in one pass would have no
    way to answer "what would this do" except by doing it.
    """

    tag: str
    wave: int
    commit: str
    message: str
    closed: int
    still_open: int

    @property
    def is_complete(self) -> bool:
        return self.still_open == 0


def plan_release(repo: Path, wave: int, *, ref: str = "HEAD") -> ReleasePlan:
    """Work out what tagging this wave would produce, and refuse if it would lie."""
    tag = f"wave-{wave}"

    dirty = _git("status", "--porcelain", cwd=repo)
    if dirty:
        raise ReleaseRefusedError(
            "the working tree has changes, so the commit being tagged is not what was "
            f"tested:\n{dirty}"
        )

    existing = _git("tag", "--list", tag, cwd=repo)
    if existing:
        raise ReleaseRefusedError(
            f"{tag} already exists. Moving a tag rewrites what a previous release meant, "
            "and anybody who already fetched it keeps a different history from anybody "
            "who fetches now. Delete it deliberately if that is really the intent."
        )

    commit = _git("rev-parse", ref, cwd=repo)
    unpushed = _git("log", "--oneline", f"origin/main..{ref}", cwd=repo)
    if unpushed:
        raise ReleaseRefusedError(
            "there are commits that have not been pushed, so this tag would point at a "
            f"release nobody else can fetch:\n{unpushed}"
        )

    report = build_wave_report(repo, load_wbs(repo / "docs" / "wbs.json"), wave, ref=ref)
    body = render_markdown(report)
    if not report.is_complete:
        # Loud, and in the tag itself. A wave shipped early is a real decision; a wave
        # shipped early and recorded as if it were finished is how a plan stops describing
        # what happened.
        body = (
            f"# Wave {wave} tagged with {report.open_count} tasks still open\n\n"
            "This wave was tagged before it closed. That is a decision somebody made, and\n"
            "it is recorded here rather than in anybody's memory.\n\n" + body
        )

    return ReleasePlan(
        tag=tag,
        wave=wave,
        commit=commit,
        message=body,
        closed=report.closed_count,
        still_open=report.open_count,
    )


def tag_release(repo: Path, plan: ReleasePlan) -> str:
    """Create the annotated tag. Does not push it, on purpose.

    Pushing a tag is the irreversible half: once it is on the remote, other people and other
    machines have it. Leaving that as a separate command run by a person means a mistaken
    tag is a local mistake rather than a published one.
    """
    _git("tag", "-a", plan.tag, plan.commit, "-m", plan.message, cwd=repo)
    return plan.tag


def main() -> int:
    """`python -m brain.release <wave>`."""
    import sys

    repo = Path(__file__).resolve().parents[2]
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("usage: python -m brain.release <wave>", file=sys.stderr)
        return 2

    try:
        plan = plan_release(repo, int(sys.argv[1]))
    except ReleaseRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    tag_release(repo, plan)
    state = "complete" if plan.is_complete else f"{plan.still_open} still open"
    print(f"tagged {plan.tag} at {plan.commit[:7]}: {plan.closed} closed, {state}")
    print(f"push it when you mean to: git push origin {plan.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
