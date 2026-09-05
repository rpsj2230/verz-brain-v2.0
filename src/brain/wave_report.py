"""What a wave actually delivered, generated from the commits rather than written by hand.

A wave report written by the person who did the work is a report about what they remember
doing. This one is derived: every task it claims closed was closed by a commit that named
it, and the commit is cited beside it. That makes the report checkable, which is the only
property that makes it worth reading.

**It reports what is not done as prominently as what is.** A report listing only
achievements is a report nobody trusts twice, and the open list is the part somebody has to
act on. Overdue items lead, because a wave that closed thirty tasks and left two overdue has
a problem the thirty do not cancel.

**Nothing here estimates.** Every number is a count of something that happened. A projected
completion date belongs to the schedule, which computes it from capacity, and duplicating
that calculation here would give two answers that drift.

Task ids: M38.2.1.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from brain.status import closed_task_ids, load_wbs


@dataclass(frozen=True)
class ModuleLine:
    """One module's contribution to a wave."""

    module: str
    name: str
    closed: tuple[str, ...]
    open: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.closed) + len(self.open)


@dataclass
class WaveReport:
    """A wave, as the commits describe it."""

    wave: int
    name: str
    generated_at: datetime
    modules: list[ModuleLine] = field(default_factory=list)
    #: Commits that closed something in this wave, newest first.
    commits: list[dict[str, str]] = field(default_factory=list)
    overdue: list[tuple[str, str]] = field(default_factory=list)

    @property
    def closed_count(self) -> int:
        return sum(len(m.closed) for m in self.modules)

    @property
    def open_count(self) -> int:
        return sum(len(m.open) for m in self.modules)

    @property
    def total(self) -> int:
        return self.closed_count + self.open_count

    @property
    def percent(self) -> float:
        return round(100 * self.closed_count / self.total, 1) if self.total else 0.0

    @property
    def is_complete(self) -> bool:
        return self.open_count == 0


def build_wave_report(
    repo: Path,
    wbs: dict[str, Any],
    wave: int,
    *,
    now: datetime | None = None,
    due_dates: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> WaveReport:
    """Assemble the report for one wave from the WBS and the commit history.

    `due_dates` is optional and comes from the schedule. Without it the report simply has
    no overdue section, which is better than inventing one: a report that guesses a
    deadline and then reports against its own guess is worse than a report with a gap.
    """
    at = now or datetime.now(UTC)
    closed, commits = closed_task_ids(repo, ref)
    wave_names: dict[str, str] = wbs.get("wave_names", {})

    report = WaveReport(
        wave=wave,
        name=wave_names.get(str(wave), f"Wave {wave}"),
        generated_at=at,
    )

    in_this_wave: set[str] = set()
    for module in wbs.get("modules", []):
        module_wave = int(module.get("wave", 0))
        leaf_waves: dict[str, int] = module.get("leaf_waves", {})
        leaves = [
            leaf
            for leaf in module.get("leaf_ids", [])
            if int(leaf_waves.get(leaf, module_wave)) == wave
        ]
        if not leaves:
            continue
        in_this_wave.update(leaves)
        done = tuple(leaf for leaf in leaves if leaf in closed)
        todo = tuple(leaf for leaf in leaves if leaf not in closed)
        report.modules.append(
            ModuleLine(module=module["id"], name=module["name"], closed=done, open=todo)
        )

    # Only commits that touched this wave. A wave report listing every commit in the repo
    # is a git log with a title.
    report.commits = [
        c for c in commits if any(tid in in_this_wave for tid in c["closed"].split(","))
    ]

    if due_dates:
        today = at.date()
        for module_line in report.modules:
            for leaf in module_line.open:
                raw = due_dates.get(leaf)
                if raw and date.fromisoformat(raw) < today:
                    report.overdue.append((leaf, raw))
        report.overdue.sort(key=lambda pair: pair[1])

    return report


def render_markdown(report: WaveReport) -> str:
    """The report as a person reads it. Overdue first, then open, then what closed.

    That order is deliberate and is the opposite of how these are usually written. What
    still needs doing is the part somebody has to act on; what closed is the part they can
    read later if they want to.
    """
    lines: list[str] = [
        f"# Wave {report.wave}: {report.name}",
        "",
        f"**{report.closed_count} of {report.total} tasks closed ({report.percent}%).**"
        + ("" if report.is_complete else f" {report.open_count} still open."),
        "",
        f"Generated {report.generated_at.date().isoformat()} from the commit history. Every",
        "task listed as closed was named by a commit, and the commit is cited below.",
        "",
    ]

    if report.overdue:
        lines += [f"## Overdue: {len(report.overdue)}", ""]
        lines += [f"- `{leaf}` was due {when}" for leaf, when in report.overdue]
        lines += [""]

    if not report.is_complete:
        lines += ["## Still open", ""]
        for module_line in sorted(report.modules, key=lambda m: -len(m.open)):
            if module_line.open:
                lines.append(
                    f"- **{module_line.module}** {module_line.name}: "
                    f"{len(module_line.open)} of {module_line.total}"
                )
        lines += [""]

    lines += ["## Closed, by module", ""]
    for module_line in sorted(report.modules, key=lambda m: m.module):
        if module_line.closed:
            lines.append(
                f"- **{module_line.module}** {module_line.name}: "
                f"{len(module_line.closed)} of {module_line.total}"
            )
    lines += [""]

    if report.commits:
        lines += ["## The commits behind it", ""]
        for commit in report.commits:
            lines.append(f"- `{commit['sha']}` {commit['subject']}")
        lines += [""]

    return "\n".join(lines)


def main() -> int:
    """Write the report for a wave. `python -m brain.wave_report <wave>`."""
    import sys

    repo = Path(__file__).resolve().parents[2]
    wave = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    wbs_path = repo / "docs" / "wbs.json"
    if not wbs_path.exists():
        print(f"no WBS at {wbs_path}")
        return 1

    report = build_wave_report(repo, load_wbs(wbs_path), wave)
    out = repo / "docs" / f"wave-{wave}-report.md"
    out.write_text(render_markdown(report), encoding="utf-8")
    print(f"wave {wave}: {report.closed_count}/{report.total} ({report.percent}%) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
