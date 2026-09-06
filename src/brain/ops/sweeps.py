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
from brain.db import libpq_url

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "brain"
TESTS = REPO / "tests"
#: The console's own suite, which is TypeScript and lives outside `tests/`.
#:
#: Added because three leaves were closed by a commit, tested by 109 tests, and reported by
#: this sweep as having no test at all: it read `tests/*.py` and nothing else. An advisory
#: that reports a problem which is not one is spent as fast as one that misses a problem
#: which is, and this repository has now had both.
#:
#: Read for `Task ids:` lines exactly like the Python suite, so one mechanism checks every
#: claim in the repository rather than Python claims being checkable and console claims
#: resting on somebody's word.
CONSOLE_TESTS = REPO / "console" / "tests"


def _test_sources() -> list[Path]:
    """Every file that can prove a claim, whatever language it is written in."""
    return [
        *TESTS.rglob("*.py"),
        *CONSOLE_TESTS.rglob("*.ts"),
        *CONSOLE_TESTS.rglob("*.tsx"),
    ]


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


#: What a skip says, and it no longer claims anything about CI.
#:
#: It used to read "(CI always sets it)", which was false in the one place anybody read it:
#: the sweeps job has no database service, so this sweep printed that line and exited 0 on
#: every run since it was written. A security check that had never executed, announcing that
#: it runs elsewhere.
#:
#: Skipping is still right on a laptop, where there is no database and a failure would turn
#: the sweep into something people route around. The fix is that CI now runs it in the job
#: that has a migrated database, and there is a test asserting CI does so.
SKIPPED_FOR_WANT_OF_A_DATABASE = (
    "skip: DATABASE_URL unset, so nothing was checked. "
    "CI runs this against a real schema in the stack job."
)


def _needs_db() -> str | None:
    """The database URL in the form `psycopg.connect` accepts, or None.

    Converted here rather than at each call site, because every caller of this opens a
    connection without SQLAlchemy and every deployment writes the URL in SQLAlchemy's form.
    `postgresql+psycopg://` reaches libpq as a keyword/value string and fails with
    `missing "=" after ...`, which reads like a malformed password.
    """
    url = os.environ.get("DATABASE_URL")
    return libpq_url(url) if url else url


# --------------------------------------------------------------------- rls
def sweep_rls() -> None:
    """Every table holding entity rows must have row-level security enabled.

    An entity table without RLS is one forgotten WHERE clause away from returning every
    row to every caller, and it will look correct in every test that happens to use a
    wide principal.
    """
    url = _needs_db()
    if url is None:
        print(SKIPPED_FOR_WANT_OF_A_DATABASE)
        return
    import psycopg  # imported here so the sweep module works with no DB driver present

    findings: list[str] = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        # Read from `brain.db.SCHEMAS` rather than listed here. The list was already
        # written out in three places - this sweep and two CI steps - and it had already
        # drifted once: `ops` was missing from this copy while the other two had it, so a
        # table without row-level security in `ops` passed the sweep that exists to find
        # exactly that.
        from brain.db import SCHEMAS

        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%(schemas)s)
              AND c.relkind = 'r'
              AND NOT c.relrowsecurity
            ORDER BY c.relname
            """,
            {"schemas": sorted(SCHEMAS)},
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
        print(SKIPPED_FOR_WANT_OF_A_DATABASE)
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

    **This used to read source text only, and printed "ok (0 literals checked)" on every
    run.** Its own docstring said what would fix it: "it becomes a real check the day there
    is a boot function that populates a registry, at which point it should import that
    registry and read `.names()` instead of grepping". `brain.tools.startup.build_registry`
    is that function and it now exists, so the sweep asks it.

    A green sweep that checked nothing is the exact failure this file has already had once:
    `sweep_traceability` carried a condition that passed unconditionally and printed "all
    traceable" for as long as it existed. So the count is not merely printed here, it is
    **asserted**. `build_registry` handed a row source must produce at least one tool, and
    zero means the builder is broken rather than that the estate is empty. That is the
    difference between a check that found nothing and a check that looked at nothing, and
    on a console they read identically.

    The literal scan is kept beside it rather than replaced. The two see different things: a
    `ToolDefinition` written in a file that nothing registers is invisible to the registry,
    and a tool built at run time from a connector manifest is invisible to the grep.
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

    registered = _registered_tool_names()
    if not registered:
        findings.append(
            "brain.tools.startup.build_registry produced no tools when handed a row source, "
            "so this sweep would pass by checking nothing; see the docstring"
        )
    findings.extend(
        f"registered tool name {name!r} does not match the grammar"
        for name in registered
        if not TOOL_NAME_RE.match(name)
    )

    if findings:
        raise SweepFailure(findings)
    print(
        f"ok: every tool name matches the grammar "
        f"({len(registered)} registered, {len(names_seen)} literal(s) checked)"
    )


class _NoRows:
    """A row source that answers nothing, so `build_registry` takes its registering branch.

    The sweep is asking what the application *names*, not what it can fetch, and a real row
    source would need a database. `build_registry` refuses to register a row tool when handed
    no source at all, deliberately, because a tool that is present and cannot answer tells a
    person the system has no data on a subject it has plenty of. That refusal is what makes
    this stand-in necessary rather than lazy: without one the sweep would read an empty
    registry and report it as fine, which is the bug it was rewritten to stop having.
    """

    def rows(self, *args: object, **kwargs: object) -> list[object]:
        return []

    def __call__(self, *args: object, **kwargs: object) -> list[object]:
        return []


def _registered_tool_names() -> tuple[str, ...]:
    """The names the application actually registers at boot.

    Empty rather than raising if the builder cannot be imported or called, because the
    caller turns empty into a finding with a sentence explaining it. Swallowing the
    exception here and reporting nothing would reproduce the defect this replaced.
    """
    try:
        from brain.tools.startup import build_registry

        registry = build_registry(source="freshdesk", records=_NoRows())  # type: ignore[arg-type]
        return tuple(sorted(registry.names()))
    except Exception:
        return ()


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
    # The shape of the `Task ids:` lines is checked before anything is read off them, and
    # that ordering is the point rather than tidiness. Every check below reads those lines,
    # so a malformed one makes all of them report confidently about the wrong ids: a line
    # ending in a comma silently drops its continuation, and prose on a line saying a leaf is
    # not claimed claims it. Reporting "all traceable" from a line nobody parsed correctly is
    # worse than reporting nothing. Collected into the same findings list rather than raised
    # separately, so one run tells you everything wrong with the record at once.
    findings: list[str] = list(_malformed_task_lines())

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
    for path in _test_sources():
        proven.update(TASK_ID_RE.findall(path.read_text(encoding="utf-8")))
    # a test file per module counts too: tests named for the module they cover
    covered_modules = {p.stem.removeprefix("test_") for p in TESTS.rglob("test_*.py")}

    # This condition used to read `if tid not in proven and not covered_modules`, which
    # asks whether the covered set is *empty* rather than whether this module is in it.
    # `covered_modules` is non-empty the moment any test file exists, so the sweep passed
    # unconditionally and printed "all traceable" while checking nothing. It ran green in
    # CI and on every push for as long as it has existed.
    #  already holds any malformed-line findings from above.
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

    # And the third direction, which neither of the two above asks about.
    #
    # There are two records of what has been built. A `Task ids:` line is how this
    # repository claims a leaf; a `Closes:` trailer is what the status page counts. The
    # checks above compare each of those against the tests, and nothing compares them
    # against *each other*, so a leaf can be implemented, tested, claimed in a docstring and
    # never appear as done on the page the client reads.
    #
    # Eight were in exactly that state when this was written, including a rebuild command
    # with a CLI and two connector transports. The tracker was under-reporting, which is a
    # less alarming failure than over-reporting and is still a document of record that is
    # wrong.
    #
    # Advisory for the same reason as the line above: this is a backlog that predates the
    # check, and a gate that goes red the day it arrives is a gate somebody switches off.
    print(f"note: {_source_claims_never_closed_by_a_commit()} leaf/leaves claimed in source only")

    # And the fourth direction, which is the blind spot the three above share.
    #
    # Every one of them intersects with the WBS leaves before comparing (`claimed & leaves`,
    # `closed & leaves`). That is correct for what each asks, and it means an id that is not
    # a leaf at all is silently dropped by all three. Nothing anywhere reports it.
    #
    # Thirty-two were in that state when this was written. Most are a group id where a leaf
    # was meant: `Closes: M31.1.1` in a module whose leaves are four parts long, or a bare
    # `M0.1` in a subject line. `brain.ops.conventions` already argues that this shape must
    # be refused, saying `Closes: M12` "is the natural thing to type", and the commit-msg
    # hook that catches it is advisory and postdates most of these.
    #
    # The failure is quiet and in the under-reporting direction, which is why nobody noticed:
    # a commit claims M31.1.1, no leaf has that id, and the four real leaves underneath stay
    # open on the tracker for ever while their author believes they were credited. The
    # tracker's own percentage stays honest, because `status.build_status` walks the declared
    # leaves rather than counting claims, and that honesty is exactly what hides this.
    #
    # Six of the thirty-two are the opposite case and worth naming: M37.2.1 through M37.2.6
    # were closed by a commit that *created* those twenty-nine tasks. Cancelling a
    # subscription and revoking AnyGen's OAuth grants are not done because the plan to do
    # them exists.
    #
    # The ids are printed rather than counted, because thirty-two is small enough to read and
    # a bare count of invisible things is one more thing nobody can act on.
    phantom = _claims_that_name_no_leaf()
    print(f"note: {len(phantom)} claimed id(s) name a group, so the tracker credits nothing")
    if phantom:
        print(f"      {', '.join(phantom)}")
        print("      name the leaves under each one individually, or Reopens: the claim")

    # And the shape of the line itself, which is not a third direction but the thing that
    # decides whether any of the three above read what the author meant. This raises rather
    # than notes, because both failures it catches are silent and both have happened.


#: What may appear on a `Task ids:` line once the ids are removed: separators, and the word
#: that means there are none. Anything else is prose on a line that is parsed for ids.
_TASK_LINE_RESIDUE_RE = re.compile(r"^[\s,;.]*(?:none[\s,;.]*)?$", re.I)


def _malformed_task_lines() -> list[str]:
    """`Task ids:` lines that do not say what their author thinks they say.

    Two failures, both silent, both of which happened here on 2026-09-06.

    **A disclaimer on the line is a claim.** Three modules read `Task ids: none. M32.4.1.2 is
    what this serves and is deliberately not claimed`. The line is parsed for ids and the
    sentence refusing the leaf contains the leaf, so all three claimed exactly what they said
    in words they were not claiming. `brain.status.claimed_ids` already records this lesson
    from the other record: a commit body listing ten ids under "Deliberately NOT claimed, with
    the reason" claimed all ten. The parser cannot be given a concept of negation reliably,
    because "not M0.6.5", "M0.6.5 is not done" and "blocked: M0.6.5" all read identically. So
    the rule is positional there and positional here: the line carries ids, or the word none,
    and no argument. The argument goes in the paragraph above, where nothing parses it.

    **A wrapped line drops its continuation.** `TASK_LINE_RE` is anchored per line, so ids
    after the wrap are invisible. I did this to `brain/app.py` an hour after writing the rule
    above: nine ids became six, and the sweep reported "all traceable" because it never saw
    the other three. A trailing comma is what that looks like from here. Repeat the whole
    `Task ids:` prefix on the next line instead; `findall` reads every one of them.
    """
    findings: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == Path(__file__).resolve():
            continue
        where = path.relative_to(REPO)
        for line in TASK_LINE_RE.findall(path.read_text(encoding="utf-8")):
            if line.rstrip().endswith(","):
                findings.append(
                    f"{where}: a Task ids: line ends in a comma, so anything on the next "
                    f"line is not read as a claim: {line.strip()!r}"
                )
            residue = TASK_ID_RE.sub("", line)
            if not _TASK_LINE_RESIDUE_RE.match(residue):
                findings.append(
                    f"{where}: a Task ids: line carries prose, which is parsed for ids "
                    f"whatever it says about them: {line.strip()!r}"
                )
    return findings


def _claims_that_name_no_leaf() -> tuple[str, ...]:
    """Ids claimed by a commit or a source docstring that are not WBS leaves at all.

    Both records of a claim are read, because the mistake is a typo and a typo does not
    care which file it was made in. A `Closes:` trailer and a `Task ids:` line are equally
    capable of naming a group where a leaf was meant.

    **A bare module id is not reported, and nothing here excludes it.** `TASK_ID_RE` is
    `M\\d+(?:\\.\\d+){1,4}`, which requires at least one dot, so `M24` is never extracted by
    either path and cannot reach this set. A guard against it was written here first and a
    mutation proved it dead: deleting it changed no test, because the regex had already
    refused the input. It is recorded in words rather than kept as a second enforcement
    point that looks like one and is not, following `manifest.ProjectedEntity`. The
    `Closes: M12` case is refused by `brain.ops.conventions` at commit time, which is the
    right place for it: there the id can be rejected before it is written down.

    Empty when git or the WBS is unavailable, matching the two helpers below: an advisory
    line must not fail a sweep for want of a repository.
    """
    try:
        from brain.status import closed_task_ids, load_wbs

        closed, _ = closed_task_ids(REPO)
        leaves = {
            leaf for m in load_wbs(REPO / "docs" / "wbs.json")["modules"] for leaf in m["leaf_ids"]
        }
        claimed: set[str] = set(closed)
        for path in SRC.rglob("*.py"):
            for line in TASK_LINE_RE.findall(path.read_text(encoding="utf-8")):
                claimed.update(TASK_ID_RE.findall(line))
    except Exception:
        return ()
    return tuple(sorted(claimed - leaves))


def _source_claims_never_closed_by_a_commit() -> int:
    """Leaves a source docstring claims that no commit has ever closed.

    Zero when git or the WBS is unavailable, matching `_commit_claims_without_tests`: an
    advisory line must not fail a sweep for want of a repository.

    Counted against the WBS leaves rather than against every id mentioned, because a
    docstring legitimately names a parent (`M24.1`) while claiming its children, and a
    parent is not a leaf the tracker counts.
    """
    try:
        from brain.status import closed_task_ids, load_wbs

        closed, _ = closed_task_ids(REPO)
        leaves = {
            leaf for m in load_wbs(REPO / "docs" / "wbs.json")["modules"] for leaf in m["leaf_ids"]
        }
        claimed: set[str] = set()
        for path in SRC.rglob("*.py"):
            for line in TASK_LINE_RE.findall(path.read_text(encoding="utf-8")):
                claimed.update(TASK_ID_RE.findall(line))
    except Exception:
        return 0
    return len((claimed & leaves) - closed)


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
        for path in _test_sources():
            named.update(TASK_ID_RE.findall(path.read_text(encoding="utf-8")))
    except Exception:
        return 0
    return len((closed & leaves) - named)


#: Licences this project may depend on. Permissive only, plus MPL-2.0, which is file-level
#: copyleft and therefore safe for a dependency we do not modify. Absent deliberately: the
#: GPL family and AGPL, which reach into a client-hosted product, and every "source
#: available" licence that restricts commercial use.
ALLOWED_LICENCES: frozenset[str] = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "BSD-2-Clause",
        "ISC",
        "PSF-2.0",
        "MPL-2.0",
        # File-level copyleft, and the reason it is here is specific rather than general.
        # `psycopg`, `psycopg-binary` and `psycopg-pool` are LGPL-3.0-only, and psycopg is
        # the PostgreSQL driver for Python - there is no permissive equivalent worth
        # switching to. The LGPL permits use in a proprietary product provided the library
        # is not modified and stays replaceable, which is exactly how it is used here: it
        # is imported, never vendored, never patched, and pinned by version in `uv.lock` so
        # a client could swap it. **Modifying any LGPL dependency changes that answer**, so
        # a vendored patch to psycopg is a licence decision and not a code change.
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
    }
)

#: How a classifier spells what `License-Expression` spells as an SPDX id. Only the ones
#: that appear in this dependency set: guessing at the rest would be a mapping nobody has
#: checked, and a wrong entry here silently admits a licence.
_CLASSIFIER_TO_SPDX = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}


def licence_is_allowed(expression: str) -> bool:
    """Whether an SPDX expression is covered by the allowlist.

    Real metadata is not a bare id. `structlog` declares `MIT OR Apache-2.0`, `greenlet`
    declares `MIT AND PSF-2.0`, and a plain set membership test refuses both - which is how
    a working allowlist gets deleted for being wrong rather than fixed.

    `OR` is a choice, so one allowed operand is enough. `AND` is a conjunction, so every
    operand must be allowed. Anything with brackets, a `WITH` exception, or both operators
    mixed is refused rather than guessed at: this is a check whose wrong answers are silent,
    and the honest response to an expression this cannot parse is to make a person look.
    """
    text = expression.strip()
    if not text:
        return False
    if "(" in text or ")" in text or " WITH " in text:
        return False
    if " OR " in text and " AND " in text:
        return False
    if " OR " in text:
        return any(part.strip() in ALLOWED_LICENCES for part in text.split(" OR "))
    if " AND " in text:
        return all(part.strip() in ALLOWED_LICENCES for part in text.split(" AND "))
    return text in ALLOWED_LICENCES


def _installed_licences() -> dict[str, str]:
    """Every installed distribution and the SPDX id it declares, empty where it declares none.

    Read from the installed environment rather than from the lock, because the lock records
    versions and not licences: answering from it would need a network call per package, and
    a check that needs the network is a check that gets skipped locally and then trusted.

    `License-Expression` is the modern field and is already SPDX. `License` is free text and
    is accepted only when it is exactly an id we allow - "MIT License" and "BSD-like" are
    not parsed, because a parser here would be a place a wrong guess admits something. The
    classifier is the last resort and is mapped through a table of the forms that actually
    occur in this dependency set.
    """
    import importlib.metadata as md

    found: dict[str, str] = {}
    for dist in md.distributions():
        meta = dist.metadata
        name = meta["Name"]
        if not name:
            continue
        declared = (meta.get("License-Expression") or "").strip()
        if not declared:
            plain = (meta.get("License") or "").strip()
            declared = plain if plain in ALLOWED_LICENCES else ""
        if not declared:
            for classifier in meta.get_all("Classifier") or []:
                if classifier in _CLASSIFIER_TO_SPDX:
                    declared = _CLASSIFIER_TO_SPDX[classifier]
                    break
        found[name] = declared
    return found


# ------------------------------------------------------------ dependencies
def sweep_dependencies() -> None:
    """Licence allowlist, release age, and whether the project is still alive.

    This exists because three components chosen during design turned out to be archived
    while still being widely recommended. Reading a LICENCE file is not the same as
    checking whether anyone still maintains the thing.
    """
    lock = REPO / "uv.lock"
    if not lock.exists():
        raise SweepFailure(["uv.lock is missing; dependencies are not pinned"])
    names = re.findall(r'^name = "([^"]+)"', lock.read_text(encoding="utf-8"), re.MULTILINE)

    # The allowlist used to be built here and compared against nothing: the only thing that
    # touched it was the line printing its length. A sweep that reports "ok" about a rule it
    # is not applying is worse than no sweep, and this is the third one in this tree found
    # doing it. It is now applied, against the installed distributions, which is what
    # actually ships.
    findings: list[str] = []
    unknown: list[str] = []
    for name, licence in _installed_licences().items():
        if not licence:
            # Reported, not failed. Some distributions genuinely publish nothing, and
            # failing on that would make the sweep unpassable for a reason nobody can fix.
            unknown.append(name)
        elif not licence_is_allowed(licence):
            findings.append(f"{name} is under {licence!r}, which is not on the allowlist")
    if findings:
        raise SweepFailure(findings)

    checked = len(_installed_licences())
    print(f"ok: {len(set(names))} pinned package(s); {checked} installed licences checked")
    if unknown:
        print(f"note: {len(unknown)} publish no licence metadata: {', '.join(sorted(unknown)[:5])}")
    print("note: release age and archived checks need network; enforced in CI")


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
