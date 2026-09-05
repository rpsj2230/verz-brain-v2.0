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
    #: What `task_ids` is a list of: `release` (since the last wave tag), `commit` (since
    #: the parent of this commit), or `none`. On the record rather than inferred, because
    #: an empty list means two very different things and a reader cannot tell them apart -
    #: "this release closed nothing" and "there was no span to look at" look identical.
    span: str = "none"
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
                "span": self.span,
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
            span=str(raw.get("span", "none")),
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
    repo: Path,
    *,
    ref: str = "HEAD",
    since: str = "",
    span: str = "",
    now: datetime | None = None,
) -> ReleaseManifest:
    """Assemble the manifest for what `ref` would deploy.

    `since` names where the list starts and `span` says what kind of span that is. Left
    empty, `choose_span` decides: the last `wave-*` tag if there is one, otherwise the
    parent of this commit.

    Never the whole history. A first release listing nine hundred ids is not information,
    it is a dump of the plan.
    """
    commit = _git("rev-parse", ref, repo=repo)
    if not commit:
        msg = f"cannot resolve {ref!r} in {repo}"
        raise ManifestError(msg)

    previous, kind = (since, span or "commit") if since else choose_span(repo, ref=ref)
    ids: set[str] = set()
    if previous:
        raw = _git("log", f"{previous}..{ref}", "--pretty=format:%s%x1f%b%x1e", repo=repo)
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
        span=kind if previous else "none",
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


#: What GitHub sends as `github.event.before` when there is no before: the first push to a
#: branch, or a force-push it declines to describe. Not a commit, and asking git about it
#: produces an error rather than an empty answer.
_NO_SUCH_COMMIT = "0" * 40


def choose_span(repo: Path, *, ref: str = "HEAD", candidate: str = "") -> tuple[str, str]:
    """Where this build's list of task ids starts, and what kind of span that is.

    Returns `(since, kind)`. The kind is on the manifest so a reader cannot mistake one
    thing for the other, which is the whole reason this returns two values instead of one.

    **`release`** - since the last `wave-*` tag. What shipped in this release, which is the
    question the manifest exists to answer.

    **`commit`** - since the parent of the commit being built. Used when nothing is tagged
    yet. It is honestly a smaller claim: it says what this one commit closed, not what the
    release contains. It is still worth having, because otherwise the manifest carries no
    ids at all until somebody cuts the first release, and a mechanism nobody has seen work
    is a mechanism discovered broken at the first release that matters.

    **`none`** - a root commit with no parent and no tag. There is nothing before it.

    An explicit `candidate` wins over the parent when it names a commit this clone has, so a
    caller that knows the real previous deploy can say so.

    Rejected: `github.event.before` from GitHub Actions, which is what this used to pass and
    which was always the empty string. That context exists on a `push` event; this workflow
    runs on `workflow_run`, where the payload is `github.event.workflow_run` and carries no
    `before` at all. It was asserted rather than checked, and it silently produced an empty
    argument for every build.
    """
    tag = _git("describe", "--tags", "--abbrev=0", "--match", "wave-*", repo=repo)
    if tag:
        return tag, "release"

    named = candidate and candidate != _NO_SUCH_COMMIT
    # `--verify` with `^{commit}` refuses a ref that exists but is not a commit, and refuses
    # a sha this clone does not have, which is the shallow-checkout case.
    if named and _git("rev-parse", "--verify", f"{candidate}^{{commit}}", repo=repo):
        return candidate, "commit"

    parent = _git("rev-parse", "--verify", f"{ref}^", repo=repo)
    return (parent, "commit") if parent else ("", "none")


def main() -> int:
    """`python -m brain.ops.release_manifest [fallback-since] > RELEASE.json`, run by CI."""
    import sys

    repo = Path(__file__).resolve().parents[3]
    candidate = sys.argv[1] if len(sys.argv) > 1 else ""
    since, kind = choose_span(repo, candidate=candidate)
    try:
        manifest = build_manifest(repo, since=since, span=kind)
    except ManifestError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(manifest.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
