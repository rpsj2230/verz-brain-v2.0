"""The schema and registry sweeps.

Task ids: M0.5.4, M0.5.5, M0.5.6, M0.5.7, M0.5.8
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from brain.core import department
from brain.ops import sweeps


def test_sweep_failure_carries_every_finding() -> None:
    exc = sweeps.SweepFailure(["a", "b", "c"])
    assert exc.findings == ["a", "b", "c"]
    assert "3 finding" in str(exc)


# ------------------------------------------------------------- dispatcher
def test_main_rejects_an_unknown_sweep() -> None:
    assert sweeps.main(["not_a_sweep"]) == 2


def test_main_rejects_no_argument() -> None:
    assert sweeps.main([]) == 2


@pytest.mark.parametrize("name", sorted(sweeps.SWEEPS))
def test_every_registered_sweep_runs_and_passes_on_this_tree(name: str) -> None:
    """The repository must satisfy its own sweeps at all times, not just in CI.

    Parametrising over the registry rather than listing names means adding a sweep
    without wiring it into this test is impossible.
    """
    assert sweeps.main([name]) == 0


def test_main_reports_failure_as_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise sweeps.SweepFailure(["deliberate"])

    monkeypatch.setitem(sweeps.SWEEPS, "tool_registry", boom)
    assert sweeps.main(["tool_registry"]) == 1


# ------------------------------------------------------- db-backed sweeps
def test_db_sweeps_skip_cleanly_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer with no Postgres must not be blocked, but the skip has to be loud
    enough that nobody mistakes it for a pass. CI always sets DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sweeps.sweep_rls()
    sweeps.sweep_grant_isolation()


def test_needs_db_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/x")
    assert sweeps._needs_db() == "postgresql://localhost/x"
    monkeypatch.delenv("DATABASE_URL")
    assert sweeps._needs_db() is None


# --------------------------------------------------------------- grammars
def test_tool_name_grammar_accepts_source_dot_action() -> None:
    for good in ("laravel.get_client", "xero.list_invoices", "lark_base.read_row"):
        assert sweeps.TOOL_NAME_RE.match(good)


def test_tool_name_grammar_rejects_malformed_names() -> None:
    for bad in ("getClient", "Laravel.get", "laravel.", ".get_client", "laravel..get"):
        assert not sweeps.TOOL_NAME_RE.match(bad)


def test_task_id_grammar_matches_nested_ids() -> None:
    """Ids run to four levels — M0.1.1.1 and deeper — because the tracker does."""
    for good in ("M0.1", "M0.2.4", "M38.1.5", "M0.1.1.1", "M12.3.4.5.6"):
        assert sweeps.TASK_ID_RE.fullmatch(good), good


def test_task_id_grammar_ignores_bare_module_ids() -> None:
    """`M0` alone is a module, not a task, and must not be treated as a claim."""
    assert not sweeps.TASK_ID_RE.fullmatch("M0")
    assert not sweeps.TASK_ID_RE.fullmatch("M38")


def test_dependency_sweep_fails_when_the_lock_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """An unpinned dependency tree cannot be audited at all, so a missing lock is a
    failure rather than a skip."""
    monkeypatch.setattr(sweeps, "REPO", tmp_path)
    with pytest.raises(sweeps.SweepFailure, match="finding"):
        sweeps.sweep_dependencies()


# ------------------------------------------------------ slug collisions (M2.1.5)
def test_an_agent_named_after_a_scope_is_a_collision() -> None:
    """ "Grant Priya finance" has two meanings if a scope and an agent are both called
    finance, and the safe reading is not the one a resolver picks by declaration order."""
    found = department.check_slug_collisions(["finance"], ["finance"], [])
    assert len(found) == 1
    assert "finance" in str(found[0])


def test_names_that_differ_only_by_a_separator_collide() -> None:
    """Two names only a machine can tell apart are a collision in the interface even when
    the database is content with them."""
    assert department.check_slug_collisions(["client-ops"], [], ["client_ops"])


def test_distinct_names_are_not_a_collision() -> None:
    """A check that fires on correct input is a check somebody switches off."""
    assert department.check_slug_collisions(["finance"], ["reporter"], ["client"]) == []


def test_the_collision_sweep_is_registered_so_ci_can_run_it() -> None:
    """A sweep nothing invokes is a function, not a check. This one was written during M2
    and left unregistered, which is how it stayed unrun."""
    assert "slug_collisions" in sweeps.SWEEPS


def test_the_collision_sweep_reports_how_many_names_it_compared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The important one. Scopes and agents are rows that mostly do not exist yet, so this
    sweep compares very little today. Printing "ok" over an empty comparison is exactly the
    failure `sweep_traceability` had for its whole life: green in CI, checking nothing, and
    nobody looking again. Saying the counts out loud keeps the gap visible."""
    sweeps.sweep_slug_collisions()
    out = capsys.readouterr().out
    assert "scope(s)" in out
    assert "agent(s)" in out
    assert "tool object(s)" in out


# ------------------------------------------------ the deployment profiles (M0.4.1)
def test_the_lite_profile_and_the_deployed_compose_do_not_drift() -> None:
    """Two files that must agree, with nothing enforcing it, is how a profile quietly stops
    being the thing it is named after. Coolify deploys `docker-compose.yml` by that name and
    the architecture asks for the profiles to be named files an operator can point at, so
    both exist and this is what keeps them the same stack."""
    repo = Path(__file__).resolve().parents[2]
    base = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    lite = (repo / "docker-compose.lite.yml").read_text(encoding="utf-8")
    assert lite.endswith(base), (
        "docker-compose.lite.yml no longer contains docker-compose.yml verbatim; "
        "change one and copy it across, or the profiles describe different stacks"
    )


def test_the_lite_profile_says_why_there_is_no_full_one() -> None:
    """A missing file reads as an oversight. The full profile adds a worker and a
    session-mode pooler beside it, and there is no worker process yet for it to serve, so a
    full profile today would declare a container with no command."""
    repo = Path(__file__).resolve().parents[2]
    lite = (repo / "docker-compose.lite.yml").read_text(encoding="utf-8")
    assert "docker-compose.full.yml" in lite
    assert "worker" in lite


# ------------------------------------------------ staging shares nothing (M38.1.4.2)
def _compose(name: str) -> dict[str, Any]:
    """Parsed rather than grepped. A compose file is YAML, and a substring search over one
    passes happily on a line that is commented out."""
    import yaml

    repo = Path(__file__).resolve().parents[2]
    parsed: dict[str, Any] = yaml.safe_load((repo / name).read_text(encoding="utf-8"))
    return parsed


def test_staging_is_its_own_compose_project() -> None:
    """A separate project gives staging its own network, volume namespace and container
    names, so `docker compose down` in the wrong directory cannot reach production's
    database and a typo in a service name resolves to nothing rather than to the wrong
    thing. A profile inside the production file would share all three."""
    staging = _compose("docker-compose.staging.yml")
    production = _compose("docker-compose.yml")
    assert staging["name"] == "brain-staging"
    assert staging["name"] != production.get("name")


def test_the_two_stacks_share_no_volume() -> None:
    """The failure this prevents is not subtle and is entirely plausible: one command run
    from the wrong directory, against the volume holding real client data."""
    staging = set(_compose("docker-compose.staging.yml").get("volumes") or {})
    production = set(_compose("docker-compose.yml").get("volumes") or {})
    assert staging
    assert production
    assert not (staging & production)


def test_staging_does_not_reuse_a_production_credential() -> None:
    """If staging read `POSTGRES_PASSWORD`, the two databases would share a password and
    staging would stop being a boundary at all: anything that leaked from the weaker stack
    would open the stronger one."""
    import re

    repo = Path(__file__).resolve().parents[2]
    text = (repo / "docker-compose.staging.yml").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z_]+)", text))
    for shared in ("POSTGRES_PASSWORD", "APP_ROLE_PASSWORD", "APP_IMAGE"):
        assert shared not in referenced, f"staging reads production's {shared}"


def test_staging_does_not_call_itself_production() -> None:
    """`brain.config` requires different settings per environment, and more importantly the
    name decides how the app describes itself. A staging box reporting itself as production
    is how somebody investigates the wrong incident."""
    staging = _compose("docker-compose.staging.yml")
    app = staging["services"]["app"]
    assert app["environment"]["BRAIN_ENV"] == "staging"


def test_staging_pools_the_same_way_production_does() -> None:
    """The pooling mode is the single setting most likely to produce a bug that appears
    only in production. Prepared statements and session-level advisory locks both behave
    differently behind a transaction pooler, and both have already bitten this project.
    Staging exercising a different mode would be staging that cannot catch either."""
    staging = _compose("docker-compose.staging.yml")
    production = _compose("docker-compose.yml")
    left = staging["services"]["pgbouncer"]["environment"]["POOL_MODE"]
    right = production["services"]["pgbouncer"]["environment"]["POOL_MODE"]
    assert left == right == "transaction"


# ------------------------------------------------- the licence check (M0.5.7)
def test_the_licence_allowlist_is_actually_applied() -> None:
    """It was not. The allowlist was built inside `sweep_dependencies` and the only thing
    that touched it was the line printing its length; no package's licence was ever compared
    against it, and the sweep printed "ok" on every run.

    That is the third sweep in this tree found reporting success about a rule it was not
    applying. This asserts the check reaches a real answer rather than a reassuring one."""
    from brain.ops import sweeps as mod

    # Reading the metadata is not the same as acting on it, and the first version of this
    # test only proved the reading. Stub in a dependency nobody would accept and require the
    # sweep to fail: that is the behaviour, and it is what survived a mutation removing the
    # comparison entirely.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_installed_licences", lambda: {"something": "AGPL-3.0-only"})
    try:
        with pytest.raises(mod.SweepFailure) as caught:
            mod.sweep_dependencies()
    finally:
        monkeypatch.undo()
    assert "AGPL-3.0-only" in str(caught.value.findings)

    # And it passes on a set it should accept, so the check is not simply always failing.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_installed_licences", lambda: {"something": "MIT"})
    try:
        mod.sweep_dependencies()
    finally:
        monkeypatch.undo()

    real = mod._installed_licences()
    assert len(real) > 20, "no distributions were inspected at all"


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("Apache-2.0", True),
        ("LGPL-3.0-only", True),
        ("GPL-3.0-only", False),
        ("AGPL-3.0-only", False),
        # Real metadata from this dependency set. A plain set membership test refuses all
        # three, which is how a working allowlist gets deleted for being wrong.
        ("MIT OR Apache-2.0", True),
        ("MIT AND PSF-2.0", True),
        ("Apache-2.0 OR BSD-2-Clause", True),
        # An OR needs only one allowed operand; an AND needs all of them.
        ("MIT OR GPL-3.0-only", True),
        ("MIT AND GPL-3.0-only", False),
        # Refused rather than guessed at, because a wrong answer here is silent.
        ("(MIT OR Apache-2.0) AND ISC", False),
        ("Apache-2.0 WITH LLVM-exception", False),
        ("MIT AND Apache-2.0 OR ISC", False),
        ("", False),
    ],
)
def test_an_spdx_expression_is_read_rather_than_matched_as_a_string(
    expression: str, allowed: bool
) -> None:
    """The two halves that matter: an OR is a choice, an AND is a conjunction. Getting them
    the wrong way round admits a GPL dependency that declared `MIT AND GPL-3.0-only`."""
    from brain.ops.sweeps import licence_is_allowed

    assert licence_is_allowed(expression) is allowed


def test_the_copyleft_licences_that_would_reach_a_client_are_not_allowed() -> None:
    """This is a client-hosted product: the client receives and runs the software. GPL and
    AGPL reach into that in a way MIT and Apache do not, and AGPL reaches it over a network
    too. LGPL is allowed and is a separate, argued decision recorded beside the allowlist -
    it holds only while nothing here modifies or vendors an LGPL dependency."""
    from brain.ops.sweeps import ALLOWED_LICENCES

    for refused in ("GPL-3.0-only", "GPL-2.0-only", "AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1"):
        assert refused not in ALLOWED_LICENCES


# ---------------------------------------------- the two records of what is built (M0.5.8)
def test_a_source_claim_that_no_commit_closed_is_counted() -> None:
    """**A counter that always returns nought is this sweep's own history.**

    `sweep_traceability` carried a condition that asked whether the covered set was empty
    rather than whether a module was in it, so it passed unconditionally and printed "all
    traceable" while checking nothing, on every push, for as long as it existed. The note
    lines beside it are the same shape of risk: a helper that returns 0 because it found
    nothing and one that returns 0 because it looked at nothing read identically on the
    console.

    So this drives the helper with a claim that is genuinely in neither record and asserts
    the number moves. Delete this and `_source_claims_never_closed_by_a_commit` can be
    reduced to `return 0` with every sweep still green.

    Eight leaves were in this state when the check was written, including a rebuild command
    with a CLI and two connector transports: implemented, tested, claimed in a docstring,
    and absent from the page the client reads."""

    from brain.ops import sweeps
    from brain.status import closed_task_ids, load_wbs

    real = sweeps._source_claims_never_closed_by_a_commit()

    # A leaf the WBS knows about that no commit has closed. Chosen from the real records
    # rather than hard-coded, because the first version of this test used `M0.1.1`, which is
    # closed, so the intersection was empty and the helper correctly returned nought while
    # the test insisted it should have counted one. The test was wrong and said the code was.
    closed, _ = closed_task_ids(sweeps.REPO)
    wbs = load_wbs(sweeps.REPO / "docs" / "wbs.json")
    leaves = [
        leaf for module in wbs["modules"] for leaf in module["leaf_ids"] if leaf not in closed
    ]
    assert leaves, "every leaf is closed, so this test has nothing to drive the helper with"
    open_leaf = leaves[0]

    class _OneFile:
        """A source tree of exactly one file, claiming a leaf no commit has closed."""

        def __init__(self, claim: str) -> None:
            self._claim = claim

        def rglob(self, pattern: str) -> list[Any]:
            del pattern
            return [_Claiming(self._claim)]

    class _Claiming:
        def __init__(self, claim: str) -> None:
            self._claim = claim

        def read_text(self, **kwargs: Any) -> str:
            del kwargs
            return f"Task ids: {self._claim}\n"

    original = sweeps.SRC
    try:
        sweeps.SRC = _OneFile(open_leaf)  # type: ignore[assignment]
        counted = sweeps._source_claims_never_closed_by_a_commit()
    finally:
        sweeps.SRC = original

    assert isinstance(real, int)
    assert counted == 1, f"{open_leaf} is claimed in source and closed by no commit"


def test_an_id_that_names_a_group_rather_than_a_leaf_is_reported() -> None:
    """**The blind spot the other three traceability checks share.**

    Every one of them intersects with the WBS leaves before comparing, which is right for
    what each asks and means an id that is not a leaf at all is dropped by all three. Thirty
    seven were in that state when this was written, and nothing anywhere printed one.

    The failure is quiet and in the under-reporting direction, which is why it survived: a
    commit says `Closes: M31.1.1`, no leaf has that id because M31's leaves are four parts
    long, and the five real leaves underneath stay open on the tracker for ever while their
    author believes they were credited. `status.build_status` walks the declared leaves
    rather than counting claims, so the percentage stays honest, and that honesty is exactly
    what hides this.

    The group id is taken from the WBS rather than invented, so this keeps testing the real
    shape if the numbering ever changes. Asserting the leaf case too is the positive half: a
    check that reported every id would be satisfied by `return everything`.

    Delete this and the helper can be reduced to `return ()` with every sweep still green,
    which is precisely the history `sweep_traceability` already has."""
    from brain.ops import sweeps
    from brain.status import load_wbs

    wbs = load_wbs(sweeps.REPO / "docs" / "wbs.json")
    leaves = {leaf for module in wbs["modules"] for leaf in module["leaf_ids"]}
    deep = sorted(leaf for leaf in leaves if leaf.count(".") >= 3)
    assert deep, "no four-part leaf ids, so there is no group id to build from"
    leaf = deep[0]
    group = leaf.rsplit(".", 1)[0]
    assert group not in leaves, f"{group} is itself a leaf, so it is not the shape this tests"

    class _OneFile:
        """A source tree of exactly one file, claiming whatever it is handed."""

        def __init__(self, claim: str) -> None:
            self._claim = claim

        def rglob(self, pattern: str) -> list[Any]:
            del pattern
            return [_Claiming(self._claim)]

    class _Claiming:
        def __init__(self, claim: str) -> None:
            self._claim = claim

        def read_text(self, **kwargs: Any) -> str:
            del kwargs
            return f"Task ids: {self._claim}\n"

    original = sweeps.SRC
    try:
        sweeps.SRC = _OneFile(group)  # type: ignore[assignment]
        with_group = sweeps._claims_that_name_no_leaf()
        sweeps.SRC = _OneFile(leaf)  # type: ignore[assignment]
        with_leaf = sweeps._claims_that_name_no_leaf()
    finally:
        sweeps.SRC = original

    assert group in with_group, f"{group} names no leaf and was not reported"
    assert leaf not in with_leaf, f"{leaf} is a real leaf and must not be reported"


def test_a_bare_module_id_is_not_reported_as_a_broken_claim() -> None:
    """A docstring naming `M24` is discussing the module the file belongs to. That is
    ordinary prose, it is the commonest thing on a `Task ids:` line after the leaves
    themselves, and reporting it would bury the real findings under noise.

    **The enforcement point is `TASK_ID_RE`, not anything in the helper.** The pattern
    requires at least one dot, so a bare module id is never extracted in the first place.
    That is worth a test anyway, and worth stating in the docstring: an explicit guard was
    written in the helper first and a mutation proved it dead, because the regex had already
    refused the input. Asserting the behaviour here rather than the guard means this keeps
    testing the property whichever layer happens to hold it.

    Delete this and the pattern can be loosened to accept a dotless id, which reads as a
    generalisation, and the note line fills with module ids. That is how an advisory nobody
    can act on gets switched off rather than worked down."""
    from brain.ops import sweeps
    from brain.status import load_wbs

    wbs = load_wbs(sweeps.REPO / "docs" / "wbs.json")
    module = wbs["modules"][0]["id"]
    assert "." not in module, "this test is about the dotless shape"

    class _OneFile:
        def rglob(self, pattern: str) -> list[Any]:
            del pattern
            return [_Claiming()]

    class _Claiming:
        def read_text(self, **kwargs: Any) -> str:
            del kwargs
            return f"Task ids: {module}\n"

    original = sweeps.SRC
    try:
        sweeps.SRC = _OneFile()  # type: ignore[assignment]
        reported = sweeps._claims_that_name_no_leaf()
    finally:
        sweeps.SRC = original

    assert module not in reported


def test_a_broken_claim_in_a_commit_is_reported_and_not_only_one_in_a_docstring() -> None:
    """**A mutation found this missing.** Every other test here stubs the source tree, so
    replacing the commit half of the helper with an empty set left all of them green: the
    stubbed docstring claim was still reported and nothing noticed that git had stopped
    being read.

    That is the half that matters more. A `Closes:` trailer is what `status.build_status`
    counts, so a broken one is a leaf that stays open on the client's tracker for ever, and
    thirty two of the thirty seven found when this was written came from commits rather than
    from docstrings.

    The expected set is recomputed here from `closed_task_ids` and the WBS rather than taken
    from the helper, so this compares two readings of the same primary sources instead of
    comparing the helper against itself.

    Delete this and the helper can stop reading git entirely while every sweep stays green."""
    from brain.ops import sweeps
    from brain.status import closed_task_ids, load_wbs

    closed, _ = closed_task_ids(sweeps.REPO)
    wbs = load_wbs(sweeps.REPO / "docs" / "wbs.json")
    leaves = {leaf for module in wbs["modules"] for leaf in module["leaf_ids"]}
    from_commits = closed - leaves
    assert from_commits, "no commit claims a broken id, so this test cannot drive the helper"

    class _NoFiles:
        """A source tree with nothing in it, so only the commit half can contribute."""

        def rglob(self, pattern: str) -> list[Any]:
            del pattern
            return []

    original = sweeps.SRC
    try:
        sweeps.SRC = _NoFiles()  # type: ignore[assignment]
        reported = set(sweeps._claims_that_name_no_leaf())
    finally:
        sweeps.SRC = original

    assert reported == from_commits


def test_the_counter_reports_nothing_rather_than_failing_without_a_repository() -> None:
    """An advisory line on a sweep must not fail the sweep for want of git. `sweep_dependencies`
    and `_commit_claims_without_tests` both make this choice and this follows them.

    Delete this and a checkout with no history turns an informational note into a red
    build, which is how a useful advisory gets deleted rather than fixed."""
    from brain.ops import sweeps

    original = sweeps.REPO
    try:
        sweeps.REPO = Path("/nonexistent-for-this-test")
        assert sweeps._source_claims_never_closed_by_a_commit() == 0
    finally:
        sweeps.REPO = original
