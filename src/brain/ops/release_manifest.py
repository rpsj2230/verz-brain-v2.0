"""What is in this build, carried by the build itself.

A deployment record that says only "commit abc1234 went out at 14:02" answers the wrong
question. The question anybody actually asks afterwards is "when did *this* start
happening", and the answer is a list of task ids, which lives in the commit messages
between this release and the last one.

**Computed where git is, carried where git is not.** The server that runs the deploy has
no repository, and the container has no `.git` directory. So CI - which does have both -
writes this manifest into the image at build time, and everything downstream reads a file
instead of running `git log`. The alternative, calling back to GitHub from the deploy
script, makes a deploy depend on an API being reachable at the moment the deploy happens,
which is the moment it is least likely to be.

**The manifest is the image's own account of itself, and that is a limitation worth
stating.** It says what the person who built it believed. It is not evidence: an image
rebuilt from a modified tree would carry a manifest saying whatever that tree said. The
evidence is the signature over the image digest, which is a different mechanism (M38.1.2.4)
and answers a different question. This answers "what changed", not "is this what we think
it is".

Rejected: writing the task ids into a label on the image instead of a file. Labels are
readable without running the container, which is genuinely better, but they are also
trivially rewritable with `docker build --label` on top of a pulled image, and a record
that a person can alter without leaving a trace reads as stronger evidence than it is.
A file inside the layer is no harder to alter, but nobody mistakes it for a guarantee.

Task ids: M38.1.3.5
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brain.status import claimed_ids

#: Where the manifest lives inside the image. Read at startup, absent in development, and
#: absence is not an error: a developer running from a checkout has git and does not need
#: it, and treating "no manifest" as a failure would make the container refuse to start
#: for the one person who could fix it.
MANIFEST_PATH = Path("/app/RELEASE.json")


class ManifestError(Exception):
    """Raised when a manifest exists but cannot be believed."""


@dataclass(frozen=True)
class ReleaseManifest:
    """The commit, when it was built, and every leaf id closed since the last release.

    `task_ids` is sorted and deduplicated, because it is compared between two builds and a
    list that differs only in order would read as a different release.
    """

    commit: str
    built_at: datetime
    task_ids: tuple[str, ...] = ()
    previous_commit: str = ""
    #: Anything the builder wants to record and nothing downstream parses. Kept a flat
    #: string map so a manifest written by a future version stays readable by this one.
    extra: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "commit": self.commit,
                "built_at": self.built_at.isoformat(),
                "task_ids": list(self.task_ids),
                "previous_commit": self.previous_commit,
                "extra": self.extra,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> ReleaseManifest:
        """Parse, and refuse anything that would make a downstream record say a false thing.

        A manifest is written by one program and read by another across a version boundary,
        which is the situation in which "be liberal in what you accept" produces a
        deployment record with an empty commit in it.
        """
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"release manifest is not JSON: {exc}"
            raise ManifestError(msg) from exc
        if not isinstance(raw, dict):
            msg = "release manifest is not an object"
            raise ManifestError(msg)

        commit = str(raw.get("commit", ""))
        if len(commit) != 40 or not all(c in "0123456789abcdef" for c in commit):
            msg = f"release manifest commit is not a full sha: {commit!r}"
            raise ManifestError(msg)
        try:
            built_at = datetime.fromisoformat(str(raw.get("built_at", "")))
        except ValueError as exc:
            msg = f"release manifest built_at is not a timestamp: {raw.get('built_at')!r}"
            raise ManifestError(msg) from exc
        if built_at.tzinfo is None:
            # A naive timestamp read as local time on one machine and UTC on another puts
            # two deployments in the wrong order, which is the one thing the record exists
            # to get right.
            msg = "release manifest built_at has no timezone"
            raise ManifestError(msg)

        ids = raw.get("task_ids", [])
        if not isinstance(ids, list):
            msg = "release manifest task_ids is not a list"
            raise ManifestError(msg)
        extra = raw.get("extra", {})
        return cls(
            commit=commit,
            built_at=built_at,
            task_ids=tuple(sorted({str(i) for i in ids})),
            previous_commit=str(raw.get("previous_commit", "")),
            extra={str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else {},
        )


def _git(*args: str, repo: Path) -> str:
    result = subprocess.run(  # noqa: S603  arguments are literals from this module
        ["git", *args],  # noqa: S607  git on PATH, as everywhere else in this repo
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def build_manifest(
    repo: Path, *, ref: str = "HEAD", since: str = "", now: datetime | None = None
) -> ReleaseManifest:
    """Assemble the manifest for what `ref` would deploy.

    `since` names the previous release. Left empty, the most recent `wave-*` tag is used,
    and if there is no tag at all the manifest carries no task ids rather than every id in
    the history: the first release closing nine hundred tasks is not information, it is a
    dump of the plan.
    """
    commit = _git("rev-parse", ref, repo=repo)
    if not commit:
        msg = f"cannot resolve {ref!r} in {repo}"
        raise ManifestError(msg)

    previous = since or _git("describe", "--tags", "--abbrev=0", "--match", "wave-*", repo=repo)
    ids: set[str] = set()
    if previous:
        span = f"{previous}..{ref}"
        raw = _git("log", span, "--pretty=format:%s%x1f%b%x1e", repo=repo)
        for entry in raw.split("\x1e"):
            if not entry.strip():
                continue
            subject, _, body = entry.strip().partition("\x1f")
            # The same rule the status page uses, imported rather than reimplemented. Two
            # parsers for "what does this commit close" is two answers, and the one that
            # disagrees with the tracker is the one printed on the deployment record.
            ids.update(claimed_ids(subject, body))

    return ReleaseManifest(
        commit=commit,
        built_at=now or datetime.now(UTC),
        task_ids=tuple(sorted(ids)),
        previous_commit=previous,
    )


def read_manifest(path: Path | None = None) -> ReleaseManifest | None:
    """The manifest baked into this image, or None outside a built image.

    None rather than a raise, because a developer running from a checkout has no manifest
    and needs the process to start anyway. A manifest that exists and is malformed *does*
    raise: that is a build that produced a record nobody can trust, and it should be loud.
    """
    where = path or MANIFEST_PATH
    if not where.exists():
        return None
    return ReleaseManifest.from_json(where.read_text(encoding="utf-8"))


def main() -> int:
    """`python -m brain.ops.release_manifest [since] > RELEASE.json`, run by CI."""
    import sys

    repo = Path(__file__).resolve().parents[3]
    since = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        manifest = build_manifest(repo, since=since)
    except ManifestError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(manifest.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
