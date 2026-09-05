"""The repository conventions that are checkable, checked.

Three of them, and they share one property: each is the kind of rule that a team agrees
to, follows for a fortnight, and then stops following without anybody deciding to. A
convention nothing enforces is a convention that describes the past.

**A commit message names the leaf ids it closes.** The status page is generated from
those ids, so a commit that closes a task without saying so leaves the plan describing
work that is finished, and the next person to read it plans around a gap that is not
there. Enforced by `ops/hooks/commit-msg`, which calls into here.

**A branch is named for its module.** Twelve tracks run concurrently and they collide in
exactly one way: two branches that sound alike get reviewed as though they were the same
work. The module id in the name makes that impossible to do by accident.

**Only leaf ids close anything.** A parent id is a summary of its children; letting one
close a task would mark work done that nobody did. This is the rule that most wants
enforcing, because writing `Closes: M12` is the natural thing to type and it is always
wrong.

Rejected: enforcing any of this in CI alone. CI runs after the commit exists, so the
message is already written and the fix is a rebase. A hook that refuses at commit time
costs one retry; the same rule in CI costs a rewrite of history or a second commit
apologising for the first.

Task ids: M38.1.1.1, M38.1.1.2
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: A leaf id has at least three parts. `M12` is a module, `M12.1` is a group, and neither
#: is a thing anybody does; `M12.1.1` is. Requiring the third part is what makes
#: `Closes: M12` fail rather than silently closing ten tasks.
LEAF_ID_RE = re.compile(r"^M\d+(?:\.\d+){2,4}$")

#: Any id at all, including the parents, so the refusal can say which kind was written.
#: Zero dots is allowed here and nowhere else: `M12` is a module id, which is the most
#: likely thing to be typed and the one whose refusal most needs to explain itself. A
#: pattern that did not match it fell through to "not a task id", which is both unhelpful
#: and untrue.
ANY_ID_RE = re.compile(r"\bM\d+(?:\.\d+){0,4}\b")

#: The one line a commit may close tasks on. Deliberately not "anywhere in the message":
#: a message that discusses M12.1.1 in a paragraph about why it was *not* done would
#: otherwise close it.
CLOSES_LINE_RE = re.compile(r"^\s*Closes:\s*(.+)$", re.M)

#: `<module-id>/<short-name>`, e.g. `M12/tool-registry`. The module id first so the
#: branch list sorts by module rather than by whoever named theirs "fix".
BRANCH_RE = re.compile(r"^M\d+(?:\.\d+)*/[a-z][a-z0-9-]*$")

#: Branches that exist for reasons other than a track. `main` is the trunk; the rest are
#: what a person types when they are about to throw the branch away, and refusing those
#: would make the rule something people work around rather than follow.
EXEMPT_BRANCHES = frozenset({"main", "HEAD"})


@dataclass(frozen=True)
class Refusal:
    """Why a message or a branch name was refused, in words a person can act on.

    Carries the offending text as well as the reason. A hook that prints only "invalid
    commit message" makes the author guess which of three rules they broke, and guessing
    wrong twice is how a rule gets disabled.
    """

    reason: str
    subject: str

    def __str__(self) -> str:
        return f"{self.reason}\n  got: {self.subject}"


def leaf_ids_in(message: str) -> tuple[str, ...]:
    """The ids a message actually closes, from the `Closes:` line only.

    Returns them in the order written rather than sorted, because a refusal that quotes
    them back should quote what was typed.
    """
    ids: list[str] = []
    for line in CLOSES_LINE_RE.findall(message):
        ids.extend(part.strip() for part in re.split(r"[,\s]+", line) if part.strip())
    return tuple(ids)


def check_commit_message(message: str) -> Refusal | None:
    """None if the message may be committed, a Refusal otherwise.

    A message with no `Closes:` line at all is allowed. Not every commit closes a task:
    a fix to a fix, a revert, a formatting pass and a merge all legitimately close
    nothing, and a rule that demanded an id from them would be satisfied with a made-up
    one. What is refused is a `Closes:` line that does not mean what it says.
    """
    subject = message.strip().splitlines()[0] if message.strip() else ""
    if not subject:
        return Refusal("a commit message needs a subject line", "(empty)")

    claimed = leaf_ids_in(message)
    if not claimed:
        # No claim, nothing to check. If the body mentions ids elsewhere, that is prose:
        # a message explaining why M12.1.1 was left alone must not close it.
        return None

    for one in claimed:
        if LEAF_ID_RE.match(one):
            continue
        if ANY_ID_RE.fullmatch(one):
            return Refusal(
                f"{one} is a module or group id, and those close nothing. A parent is a "
                "summary of its children; closing one would mark work done that nobody "
                "did. Name the leaves.",
                subject,
            )
        return Refusal(f"{one!r} on the Closes: line is not a task id", subject)
    return None


def check_branch_name(name: str) -> Refusal | None:
    """None if the branch may exist, a Refusal otherwise."""
    if name in EXEMPT_BRANCHES:
        return None
    if BRANCH_RE.match(name):
        return None
    return Refusal(
        "a branch is named <module-id>/<short-name>, for example M12/tool-registry. "
        "Twelve tracks run at once and the module id is what stops two of them being "
        "reviewed as though they were the same work.",
        name,
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m brain.ops.conventions <path-to-commit-message-file>`.

    Reads a file rather than a string because that is the interface git's `commit-msg`
    hook offers: git writes the message to a temporary file and passes the path.
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m brain.ops.conventions <commit-message-file>", file=sys.stderr)
        return 2

    message = Path(args[0]).read_text(encoding="utf-8")
    refusal = check_commit_message(message)
    if refusal is not None:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
