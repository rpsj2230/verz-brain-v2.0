"""Live progress, generated from git history rather than typed by anyone.

A task is done when a commit closing it is on `main` and CI passed. Nothing is marked done
by hand, which is the only way a progress figure stays honest: a ticked checkbox is a
claim, a merged commit is evidence.

The rule: a task id in a commit **subject**, or on a `Closes:` line, closes that task.
Body prose closes nothing, and an ancestor id closes none of its children. Both of those
restrictions were added after each let the number read higher than the truth.

The deliberate consequence is that forgetting to write an id means the work does not
count. That is a nuisance exactly once and then never again, and it fails in the safe
direction.

Task ids: M38.3.1, M38.3.2, M38.3.3, M38.3.4
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

TASK_ID_RE = re.compile(r"\bM\d+(?:\.\d+){1,4}\b")


class WaveProgress(BaseModel):
    wave: int
    name: str
    total: int
    done: int
    percent: float = 0.0


class ModuleProgress(BaseModel):
    module: str
    name: str
    wave: int
    total: int
    done: int


class Status(BaseModel):
    """What is built, as of a specific commit."""

    generated_at: str
    commit: str
    commit_subject: str = ""
    total: int = 0
    done: int = 0
    percent: float = 0.0
    current_wave: int | None = None
    waves: list[WaveProgress] = []
    modules: list[ModuleProgress] = []
    done_task_ids: list[str] = []
    #: How many leaves closed since midnight UTC. Zero is a real and common answer, and
    #: showing it is the point: a page that only ever shows movement cannot show a stall.
    closed_today: int = 0
    #: The next few unclosed leaves in the current wave, in plan order. Answers "what is
    #: next" without anybody opening the tracker and reading down it.
    next_up: list[str] = []
    recent: list[dict[str, str]] = []


#: Only these lines carry claims. Prose does not.
CLOSES_RE = re.compile(r"^\s*closes:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def claimed_ids(subject: str, body: str) -> set[str]:
    """Task ids a commit actually claims.

    Read from the subject line and from `Closes:` lines only — never from body prose.

    That restriction was added after a commit whose body listed ten ids under
    "Deliberately NOT claimed, with the reason" and thereby claimed all ten. The parser
    has no concept of negation and cannot be given one reliably: "not M0.6.5", "M0.6.5 is
    not done" and "blocked: M0.6.5" all read identically to a scanner, and the failure is
    silent and in the wrong direction.

    So the rule is positional rather than semantic. A commit may discuss any id it likes
    in its body; only the subject and an explicit trailer count.
    """
    ids: set[str] = set(TASK_ID_RE.findall(subject))
    for line in CLOSES_RE.findall(body):
        ids.update(TASK_ID_RE.findall(line))
    return ids


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def closed_task_ids(repo: Path, ref: str = "HEAD") -> tuple[set[str], list[dict[str, str]]]:
    """Every task id claimed by a commit reachable from `ref`, newest first.

    See claimed_ids for what counts as a claim: the subject line and `Closes:` trailers,
    never body prose.
    """
    raw = _git("log", ref, "--pretty=format:%H%x1f%ct%x1f%s%x1f%b%x1e", cwd=repo)
    if not raw:
        return set(), []

    found: set[str] = set()
    recent: list[dict[str, str]] = []
    for entry in raw.split("\x1e"):
        # Strip line endings only, never str.strip(). Python counts \x1c through \x1f as
        # whitespace, so a bare .strip() eats the trailing unit separator — and a commit
        # with an empty body (any one-line message, which is most of them) then splits
        # into three fields instead of four and is silently dropped. That would have
        # under-counted progress with no error anywhere.
        parts = entry.lstrip("\r\n").split("\x1f", 3)
        if len(parts) < 3:
            continue
        sha, ts, subject = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        ids = sorted(set(claimed_ids(subject, body)))
        found.update(ids)
        if ids and len(recent) < 20:
            recent.append(
                {
                    "sha": sha[:7],
                    "at": datetime.fromtimestamp(int(ts), UTC).isoformat(),
                    "subject": subject,
                    "closed": ",".join(ids),
                }
            )
    return found, recent


def closed_since(repo: Path, when: datetime, ref: str = "HEAD") -> set[str]:
    """Task ids claimed by commits made on or after `when`.

    **Filtered in Python, not with `git log --since`.** `--since` prunes the walk: it
    stops descending a line of history at the first commit older than the cutoff, so a
    single old commit sitting at the tip hides every newer commit behind it. That is not
    hypothetical - a rebase, a cherry-pick or any amended date produces exactly that
    shape, and the count comes back zero with no error anywhere. Found by a test that
    dated one commit to last week and expected the other three to still count.

    A separate walk rather than a filter over `recent`, because `recent` is capped at
    twenty entries: counting from it would be right on a quiet day and quietly wrong on a
    busy one, which is the worse of the two failures.

    `when` is passed in rather than computed here so the caller decides what "today"
    means. It has to be UTC: the build runs on a CI runner in one timezone, the server is
    in another and the person reading is in a third, and a day boundary that depends on
    who is asking gives three different answers to one question.
    """
    raw = _git("log", ref, "--pretty=format:%ct%x1f%s%x1f%b%x1e", cwd=repo)
    cutoff = when.timestamp()
    found: set[str] = set()
    for entry in raw.split("\x1e"):
        if not entry.strip():
            continue
        # `lstrip` of line endings only, never `strip()`: Python counts \x1c through
        # \x1f as whitespace, so a bare strip eats the unit separator and a one-line
        # message then splits wrongly. The same trap as in `closed_task_ids` above.
        parts = entry.lstrip("\r\n").split("\x1f", 2)
        if len(parts) < 2:
            continue
        try:
            if int(parts[0]) < cutoff:
                continue
        except ValueError:
            continue
        found.update(claimed_ids(parts[1], parts[2] if len(parts) > 2 else ""))
    return found


def _leaf_ids(node: Any, prefix: str, out: list[str]) -> None:
    """Walk the WBS exactly as the renderer numbers it, so ids line up with the tracker."""
    children = node.get("s") or []
    keys = node.get("k") or []
    for i, child in enumerate(children, start=1):
        cid = f"{prefix}.{i}"
        if isinstance(child, str):
            out.append(cid)
        else:
            _leaf_ids(child, cid, out)
    for i in range(1, len(keys) + 1):
        out.append(f"{prefix}.{len(children) + i}")


def _is_closed(leaf: str, closed: set[str]) -> bool:
    """A leaf counts as closed only when its own id is named. Ancestors close nothing.

    This was the other way round until 2026-09-04, on the reasoning that nobody lists
    forty leaf ids when a whole subtree lands together. That convenience was quietly
    inflating the number: a commit saying `M0.6` closed all seven children including
    connector cassettes, which were not written, and a commit saying `M0.3` closed the
    PgBouncer tasks, which do not exist yet either.

    Nothing warned, because an ancestor id is exactly what an honest commit for a large
    piece of work looks like. The page's entire claim is that it cannot show progress that
    does not exist, so the rule has to be the strict one and commits have to list what
    they actually closed.
    """
    return leaf in closed


def build_status(repo: Path, wbs: dict[str, Any], ref: str = "HEAD") -> Status:
    closed, recent = closed_task_ids(repo, ref)
    wave_names: dict[str, str] = wbs.get("wave_names", {})

    modules: list[ModuleProgress] = []
    per_wave: dict[int, list[int]] = {}
    total = done = 0
    matched: set[str] = set()

    for m in wbs.get("modules", []):
        leaves: list[str] = m.get("leaf_ids", [])
        wave = int(m.get("wave", 0))
        # A leaf can sit in a later wave than its module. M38 is the reason: the delivery
        # pipeline is wave 0, but "what is live after wave 3" cannot be done before wave 3.
        # Counting those against wave 0 puts work in the denominator that wave 0 cannot do,
        # so the wave could never reach 100% and the figure understated real progress.
        leaf_waves: dict[str, int] = m.get("leaf_waves", {})
        m_done = 0
        for leaf in leaves:
            leaf_done = _is_closed(leaf, closed)
            if leaf_done:
                m_done += 1
                matched.add(leaf)
            bucket = per_wave.setdefault(int(leaf_waves.get(leaf, wave)), [0, 0])
            bucket[0] += 1
            bucket[1] += int(leaf_done)
        modules.append(
            ModuleProgress(
                module=m["id"], name=m["name"], wave=wave, total=len(leaves), done=m_done
            )
        )
        total += len(leaves)
        done += m_done

    waves = [
        WaveProgress(
            wave=w,
            name=wave_names.get(str(w), f"Wave {w}"),
            total=t,
            done=d,
            percent=round(100 * d / t, 1) if t else 0.0,
        )
        for w, (t, d) in sorted(per_wave.items())
    ]
    # The first unfinished wave. None means every wave is complete, so there is no
    # current wave rather than a misleading "wave 5".
    current = next((w.wave for w in waves if w.done < w.total), None)

    # Midnight UTC rather than local. The build runs on a CI runner in one timezone, the
    # server is in another and Rupash is in a third; "today" has to mean one thing or the
    # number changes depending on who is asking.
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    closed_today = len(closed_since(repo, midnight, ref) & set(matched))

    # Plan order, not id order. The WBS lists leaves in the sequence they are meant to be
    # done, and re-sorting them would answer a different question: `M12.1.10` sorts before
    # `M12.1.2` as a string, and the plan does not mean that.
    next_up: list[str] = []
    for m in wbs.get("modules", []):
        waves_by_leaf: dict[str, int] = m.get("leaf_waves", {})
        for leaf in m.get("leaf_ids", []):
            if leaf in closed:
                continue
            if current is not None and waves_by_leaf.get(leaf, int(m.get("wave", 0))) != current:
                continue
            next_up.append(leaf)
            if len(next_up) == 5:
                break
        if len(next_up) == 5:
            break

    return Status(
        generated_at=datetime.now(UTC).isoformat(),
        commit=_git("rev-parse", "--short", "HEAD", cwd=repo) or "unknown",
        commit_subject=_git("log", "-1", "--pretty=%s", cwd=repo),
        total=total,
        done=done,
        percent=round(100 * done / total, 1) if total else 0.0,
        current_wave=current,
        waves=waves,
        modules=modules,
        done_task_ids=sorted(matched),
        closed_today=closed_today,
        next_up=next_up,
        recent=recent,
    )


def load_wbs(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for m in data.get("modules", []):
        if "leaf_ids" not in m:
            ids: list[str] = []
            for i, task in enumerate(m.get("tasks", []), start=1):
                _leaf_ids(task, f"{m['id']}.{i}", ids)
            m["leaf_ids"] = ids
    return data


def main() -> int:
    """Write docs/status.json. Runs in CI before the image is built."""
    repo = Path(__file__).resolve().parents[2]
    wbs_path = repo / "docs" / "wbs.json"
    if not wbs_path.exists():
        print(f"no WBS at {wbs_path}")
        return 1
    status = build_status(repo, load_wbs(wbs_path))
    out = repo / "docs" / "status.json"
    out.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    print(f"{status.done}/{status.total} tasks ({status.percent}%) - wave {status.current_wave}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
