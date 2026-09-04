"""Live progress, generated from git history rather than typed by anyone.

A task is done when a commit closing it is on `main` and CI passed. Nothing is marked done
by hand, which is the only way a progress figure stays honest: a ticked checkbox is a
claim, a merged commit is evidence.

The rule is one line — a task id in a commit subject or body on `main` closes that task —
and it has one deliberate consequence. Forgetting to write the id means the work does not
count, which is a nuisance exactly once and then never again.

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
    recent: list[dict[str, str]] = []


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
    """Every task id mentioned by a commit reachable from `ref`, newest first.

    Reads the whole message, not just the subject, so a commit closing eight leaves can
    list them in the body instead of cramming them into 72 characters.
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
        ids = sorted(set(TASK_ID_RE.findall(f"{subject}\n{body}")))
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
    """A leaf counts as closed when it is named, or when any ancestor id is named.

    Closing `M0.2` closes the leaves beneath it, because that is how commits are actually
    written — nobody lists forty leaf ids when the whole subtree landed together.
    """
    if leaf in closed:
        return True
    parts = leaf.split(".")
    return any(".".join(parts[: i + 1]) in closed for i in range(1, len(parts)))


def build_status(repo: Path, wbs: dict[str, Any], ref: str = "HEAD") -> Status:
    closed, recent = closed_task_ids(repo, ref)
    wave_names: dict[str, str] = wbs.get("wave_names", {})

    modules: list[ModuleProgress] = []
    per_wave: dict[int, list[int]] = {}
    total = done = 0
    matched: set[str] = set()

    for m in wbs.get("modules", []):
        leaves: list[str] = m.get("leaf_ids", [])
        m_done = 0
        for leaf in leaves:
            if _is_closed(leaf, closed):
                m_done += 1
                matched.add(leaf)
        wave = int(m.get("wave", 0))
        modules.append(
            ModuleProgress(
                module=m["id"], name=m["name"], wave=wave, total=len(leaves), done=m_done
            )
        )
        bucket = per_wave.setdefault(wave, [0, 0])
        bucket[0] += len(leaves)
        bucket[1] += m_done
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
