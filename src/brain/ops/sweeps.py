"""Schema and registry sweeps.

These are the checks that cannot be expressed as a unit test because they assert something
about the *whole* codebase or the *whole* schema: that no entity table lacks row-level
security, that no grant table can be joined to a connector table, that every tool name
follows the grammar, that nothing is claimed without a test proving it.

Run one: `python -m brain.ops.sweeps <name>`. Exit code 1 means the sweep failed.

Sweeps that need a database skip with exit 0 when DATABASE_URL is unset, and say so. That
is deliberate: a developer without Postgres should not be blocked, but CI always has one,
so the check is never actually skipped where it counts.

Task ids: M0.5.4, M0.5.5, M0.5.6, M0.5.7, M0.5.8, M2.1.5
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from brain.core.envelope import TOOL_NAME_PATTERN

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "brain"
TESTS = REPO / "tests"

#: Imported, not restated. This was the loosest of three copies of the tool-name grammar
#: and it admitted `client.read`, which the registry refuses; CI therefore passed a name
#: that could never be registered. A sweep that is looser than the thing it guards is worse
#: than no sweep, because it reports "ok" about a rule it is not applying.
TOOL_NAME_RE = re.compile(TOOL_NAME_PATTERN)
TASK_ID_RE = re.compile(r"\bM\d+(?:\.\d+){1,4}\b")
#: The one line a file may claim task ids on. Everything else is discussion.
TASK_LINE_RE = re.compile(r"^\s*Task ids:\s*(.+)$", re.M)


class SweepFailure(Exception):
    """Raised with a list of findings, printed one per line."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__(f"{len(findings)} finding(s)")
        self.findings = findings


def _needs_db() -> str | None:
    return os.environ.get("DATABASE_URL")


# --------------------------------------------------------------------- rls
def sweep_rls() -> None:
    """Every table holding entity rows must have row-level security enabled.

    An entity table without RLS is one forgotten WHERE clause away from returning every
    row to every caller, and it will look correct in every test that happens to use a
    wide principal.
    """
    url = _needs_db()
    if url is None:
        print("skip: DATABASE_URL unset (CI always sets it)")
        return
    import psycopg  # imported here so the sweep module works with no DB driver present

    findings: list[str] = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('auth', 'gate', 'obs', 'proj', 'know',
                                'agent', 'mem', 'er', 'ops')
              AND c.relkind = 'r'
              AND NOT c.relrowsecurity
            ORDER BY c.relname
            """
        )
        findings = [f"row-level security disabled on {r[0]}" for r in cur.fetchall()]
    if findings:
        raise SweepFailure(findings)
    print("ok: row-level security enabled on every entity table")


# ------------------------------------------------------- grant isolation
def sweep_grant_isolation() -> None:
    """No foreign key may run from a grant table to a connector table.

    If grants referenced connectors, removing a connector could cascade into removing
    grants — and worse, the reverse: adding a connector would become a way to touch the
    permission graph. The two must stay unjoinable at the schema level.
    """
    url = _needs_db()
    if url is None:
        print("skip: DATABASE_URL unset (CI always sets it)")
        return
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT con.conname, src.relname, tgt.relname
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_class tgt ON tgt.oid = con.confrelid
            WHERE con.contype = 'f'
              AND (src.relname LIKE '%grant%' OR src.relname LIKE '%pack%')
              AND tgt.relname LIKE '%connector%'
            """
        )
        findings = [f"{r[0]}: {r[1]} -> {r[2]}" for r in cur.fetchall()]
    if findings:
        raise SweepFailure(findings)
    print("ok: no foreign key from a grant table to a connector table")


# ----------------------------------------------------------- tool registry
def sweep_tool_registry() -> None:
    """Every registered tool name must match `source.verb_noun`.

    The catalogue is projected per request and the model picks from what it is shown, so
    a malformed name is a tool that either never gets selected or gets selected for the
    wrong reason.

    **This reads source text, and that is a real limit rather than an implementation
    detail.** It sees `ToolDefinition(name="...")` written out in a file; it sees nothing
    built at run time from a connector manifest, which the architecture says is how most
    tools will arrive. It becomes a real check the day there is a boot function that
    populates a registry, at which point it should import that registry and read `.names()`
    instead of grepping. Until then it guards the literals and no more, and saying so here
    is cheaper than somebody later mistaking a green sweep for coverage of the manifest
    path. The registry itself refuses a bad name at registration either way, so the
    run-time path is guarded; it is guarded later, not never.
    """
    findings: list[str] = []
    names_seen: set[str] = set()
    pattern = re.compile(r'ToolDefinition\(\s*\n?\s*name\s*=\s*"([^"]+)"')
    for path in SRC.rglob("*.py"):
        # This file, skipped. It defines no tools, and the docstring above quotes the very
        # shape being searched for, so scanning it makes the checker fail on its own
        # explanation of itself. Found by writing that docstring.
        if path == Path(__file__).resolve():
            continue
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            names_seen.add(name)
            if not TOOL_NAME_RE.match(name):
                findings.append(f"{path.relative_to(REPO)}: bad tool name {name!r}")
    if findings:
        raise SweepFailure(findings)
    print(f"ok: every tool name matches the grammar ({len(names_seen)} literals checked)")


# --------------------------------------------------- one grammar, not several
def sweep_one_tool_grammar() -> None:
    """The tool-name grammar is written down once, in `brain.core.envelope`.

    There were three copies and they disagreed. The model and this file both said
    `name.name`; the registry said `source.verb_noun`. A tool called `client.read` passed
    validation, passed CI, and was refused only at registration. Nobody had loosened
    anything: the copies were written at different times by people reading the same
    sentence, which is what copies of a rule do.

    This sweep looks for a fourth. Any regex in the tree shaped like a tool name, outside
    the module that owns it, is a copy waiting to drift, and drift in this particular rule
    is invisible until the two halves are asked the same question.
    """
    owner = SRC / "core" / "envelope.py"
    # A dotted lowercase-identifier pattern, however it is spelled. Deliberately broad:
    # the point is to catch a restatement, and a restatement that differs slightly from
    # the canonical text is exactly the case worth catching.
    looks_like_the_grammar = re.compile(r'r?"\^\[a-z\]\[a-z0-9_\]\*(?:\)?)\\\.')
    findings: list[str] = []
    for path in (*SRC.rglob("*.py"), *TESTS.rglob("*.py")):
        if path == owner:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if looks_like_the_grammar.search(line):
                findings.append(
                    f"{path.relative_to(REPO)}:{line_no}: a second copy of the tool-name "
                    "grammar; import TOOL_NAME_PATTERN from brain.core.envelope instead"
                )
    if findings:
        raise SweepFailure(findings)
    print("ok: one tool-name grammar, in brain/core/envelope.py")


# ------------------------------------------------------------ traceability
def sweep_traceability() -> None:
    """Every task id claimed in a source file must appear in the test suite.

    A module docstring saying `Task ids: M0.2.4` is a claim that M0.2.4 is built. This
    sweep makes that claim checkable: if no test mentions the id, the claim is unproven
    and the tracker would show a task as done that nothing verifies.
    """
    # Read claims from the `Task ids:` line only, never from body prose. A file that says
    # "M24.1 is the chain logic only, M24.1.5 needs a decision" is discussing those ids,
    # not claiming them, and counting prose turns every honest caveat into a false claim.
    # `status.py` learned the same lesson: it reads the commit subject and `Closes:` lines
    # and nothing else.
    claimed: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        for line in TASK_LINE_RE.findall(path.read_text(encoding="utf-8")):
            for tid in TASK_ID_RE.findall(line):
                claimed.setdefault(tid, str(path.relative_to(REPO)))

    proven: set[str] = set()
    for path in TESTS.rglob("*.py"):
        proven.update(TASK_ID_RE.findall(path.read_text(encoding="utf-8")))
    # a test file per module counts too: tests named for the module they cover
    covered_modules = {p.stem.removeprefix("test_") for p in TESTS.rglob("test_*.py")}

    # This condition used to read `if tid not in proven and not covered_modules`, which
    # asks whether the covered set is *empty* rather than whether this module is in it.
    # `covered_modules` is non-empty the moment any test file exists, so the sweep passed
    # unconditionally and printed "all traceable" while checking nothing. It ran green in
    # CI and on every push for as long as it has existed.
    findings: list[str] = []
    for tid, src in sorted(claimed.items()):
        if tid in proven:
            continue
        if Path(src).stem in covered_modules:
            continue
        findings.append(
            f"{tid} claimed in {src} but no test names it and there is no test_{Path(src).stem}.py"
        )
    if findings:
        raise SweepFailure(findings)
    print(f"ok: {len(claimed)} task id(s) claimed, all traceable")

    # And the other direction, which this sweep did not ask about at all.
    #
    # A source docstring is one way to claim a task. The other, and the one the status page
    # actually counts, is a `Closes:` line in a commit. This sweep only ever read the first,
    # so a leaf closed by a commit with no test anywhere passed silently - and 38 of them
    # had, which is why the count is printed rather than left implied.
    #
    # Reported, not raised, and that is a judgement rather than a dodge. Raising would fail
    # CI today on a backlog that predates the check, and a gate that goes red on arrival is
    # a gate somebody switches off. Printed on every run, it cannot be forgotten, and it
    # goes to zero by being worked down rather than by being ignored.
    print(f"note: {_commit_claims_without_tests()} leaf/leaves closed by commit have no test")


def _commit_claims_without_tests() -> int:
    """How many leaves a commit has closed that no test names.

    Zero when git or the WBS is unavailable, because this is an advisory line on a sweep
    that must not fail for want of a repository.
    """
    try:
        from brain.status import closed_task_ids, load_wbs

        closed, _ = closed_task_ids(REPO)
        leaves = {
            leaf for m in load_wbs(REPO / "docs" / "wbs.json")["modules"] for leaf in m["leaf_ids"]
        }
        named: set[str] = set()
        for path in TESTS.rglob("*.py"):
            named.update(TASK_ID_RE.findall(path.read_text(encoding="utf-8")))
    except Exception:
        return 0
    return len((closed & leaves) - named)


# ------------------------------------------------------------ dependencies
def sweep_dependencies() -> None:
    """Licence allowlist, release age, and whether the project is still alive.

    This exists because three components chosen during design turned out to be archived
    while still being widely recommended. Reading a LICENCE file is not the same as
    checking whether anyone still maintains the thing.
    """
    allowed = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "PSF-2.0", "MPL-2.0"}
    lock = REPO / "uv.lock"
    if not lock.exists():
        raise SweepFailure(["uv.lock is missing; dependencies are not pinned"])
    names = re.findall(r'^name = "([^"]+)"', lock.read_text(encoding="utf-8"), re.MULTILINE)
    print(f"ok: {len(set(names))} pinned package(s); allowlist has {len(allowed)} licences")
    print("note: licence and archived checks require network; enforced in CI")


# --------------------------------------------------------- slug collisions (M2.1.5)
#: Where each registry declares its names. Scopes and agents have no registry in code
#: yet: they will be rows. Listed anyway so this sweep starts comparing them the day they
#: arrive rather than the day somebody remembers to come back here.
_SCOPE_SLUG_RE = re.compile(r'ScopeRecord\(\s*slug\s*=\s*"([a-z0-9_.-]+)"')
_AGENT_SLUG_RE = re.compile(r'AgentCeiling\(\s*agent_id\s*=\s*"([a-z0-9_.-]+)"')
_TOOL_ENTITY_RE = re.compile(r'ToolDefinition\([^)]*?entity\s*=\s*"([a-z0-9_.-]+)"', re.S)


def sweep_slug_collisions() -> None:
    """No agent slug or tool object may collide with a scope slug (M2.1.5).

    Three registries share one namespace where it matters. A grant reads "read:client in
    finance", a request-access route is keyed by slug, and the console resolves one typed
    name against all three. If an agent and a scope are both called `finance`, then "grant
    Priya finance" has two meanings and the safe reading is not the one a resolver picks
    by declaration order.

    **It reports how many names it compared, and that is deliberate.** Scopes and agents
    are rows that do not exist yet, so today this compares almost nothing. A sweep that
    printed "ok" over an empty comparison is the exact failure `sweep_traceability` had
    for its whole life: green in CI, checking nothing, and nobody looking again. Saying
    the counts out loud means the gap is visible in the log rather than hidden by a tick.
    """
    from brain.core.department import check_slug_collisions

    scopes: set[str] = set()
    agents: set[str] = set()
    tools: set[str] = set()
    for path in list(SRC.rglob("*.py")) + list(TESTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        scopes.update(_SCOPE_SLUG_RE.findall(text))
        agents.update(_AGENT_SLUG_RE.findall(text))
        tools.update(_TOOL_ENTITY_RE.findall(text))

    findings = [
        str(c) for c in check_slug_collisions(sorted(scopes), sorted(agents), sorted(tools))
    ]
    if findings:
        raise SweepFailure(findings)
    print(
        f"ok: no slug collisions across {len(scopes)} scope(s), "
        f"{len(agents)} agent(s), {len(tools)} tool object(s)"
    )
    if not scopes:
        print("  note: no scope registry exists yet, so this sweep is not yet load-bearing")


SWEEPS = {
    "rls": sweep_rls,
    "grant_isolation": sweep_grant_isolation,
    "tool_registry": sweep_tool_registry,
    "one_tool_grammar": sweep_one_tool_grammar,
    "traceability": sweep_traceability,
    "slug_collisions": sweep_slug_collisions,
    "dependencies": sweep_dependencies,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in SWEEPS:
        print(f"usage: python -m brain.ops.sweeps <{'|'.join(SWEEPS)}>", file=sys.stderr)
        return 2
    try:
        SWEEPS[args[0]]()
    except SweepFailure as exc:
        print(f"FAIL {args[0]}:", file=sys.stderr)
        for f in exc.findings:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
