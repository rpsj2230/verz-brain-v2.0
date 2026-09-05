"""The parts of this system that are files rather than code, and fail silently.

Everything here is configuration: a directory layout, a task runner, a migration's
extension list, a database role's flags, an environment example. None of it has a type
checker or a test suite of its own, and all of it breaks in the same way - quietly, by
something being absent rather than wrong.

The coverage floor is the clearest case. Drop `fail_under` and every test still passes,
CI still goes green, and the only visible change is that the number stops being enforced.
Nothing about that reads as a regression in a diff.

Task ids: M0.1.4, M0.1.5, M0.3.1, M0.3.3, M0.3.6, M0.4.3, M0.5.1, M0.5.2
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _pyproject() -> dict[str, object]:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------- the layout (M0.1.4)
#: One directory per governed noun, plus the shared core. The architecture's own list.
#:
#: The layout is the first thing that decays, and it decays by accretion rather than by
#: anybody deciding: a connector lands in `gate/` because that is where the caller was, and
#: three months later the gate imports a connector and nobody can say when that started.
EXPECTED_PACKAGES = (
    "agents",
    "audit",
    "chat",
    "connectors",
    "console",
    "core",
    "ext",
    "gate",
    "knowledge",
    "memory",
    "ops",
    "tools",
)


@pytest.mark.parametrize("package", EXPECTED_PACKAGES)
def test_every_governed_noun_has_its_own_package(package: str) -> None:
    """Deleting this lets the layout drift back to whatever import was convenient, and the
    boundary between a lens and a principal stops being visible in the file tree."""
    where = REPO / "src" / "brain" / package
    assert where.is_dir(), f"src/brain/{package} is missing"
    assert (where / "__init__.py").exists(), f"src/brain/{package} is not a package"


def test_the_gate_does_not_import_a_connector() -> None:
    """The direction of the dependency is the architecture. A connector is a source of
    records; the gate decides what a caller may see of them. If the gate imported a
    connector it would be deciding *and* fetching, and the seam where every permission
    decision happens would no longer be a seam.

    Checked over imports rather than over a diagram, because a diagram cannot fail.
    """
    gate = REPO / "src" / "brain" / "gate"
    offenders = [
        f"{path.relative_to(REPO)}:{i}"
        for path in gate.rglob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"\s*(from|import)\s+brain\.connectors", line)
    ]
    assert not offenders, f"the gate imports a connector: {offenders}"


# ------------------------------------------------ the task runner (M0.1.5)
@pytest.mark.parametrize(
    "target", ["dev", "test", "migrate", "seed", "lint", "invariants", "types", "check"]
)
def test_the_task_runner_offers_every_named_command(target: str) -> None:
    """A runner missing a target means everybody invents their own incantation, and the
    invocation CI runs stops being the invocation a person runs. The two then drift, and the
    difference is discovered on a red build nobody can reproduce locally."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(rf"^{target}:", makefile, re.M), f"make {target} does not exist"


# --------------------------------------------- lint, types, floor (M0.5.1, M0.5.2)
def test_the_coverage_floor_exists_and_is_a_real_number() -> None:
    """The clearest silent failure in the repository. Remove `fail_under` and every test
    still passes, CI still goes green, and the only change is that the number stops being
    enforced - which reads in a diff as deleting a line of configuration.

    Not asserted at a specific value, because raising or lowering the floor is a normal
    decision. Asserted as "there is one, and it is not nought", because nought is how a
    floor gets disabled while still appearing to exist.
    """
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    report = tool["coverage"]["report"]
    assert isinstance(report["fail_under"], int)
    assert report["fail_under"] > 0


def test_the_type_checker_is_strict_and_covers_the_tests_too() -> None:
    """Tests are where a wrong type is most likely to be silently accepted, because a test
    asserting on a mistyped value passes as happily as one asserting on a correct one.
    Excluding them from mypy is the single easiest way to make the strictness cosmetic."""
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    mypy = tool["mypy"]
    assert mypy.get("strict") is True
    files = " ".join(mypy.get("files", []))
    assert "src" in files and "tests" in files


def test_the_linter_and_the_formatter_agree_about_line_length() -> None:
    """Two different lengths means the formatter writes lines the linter then rejects, and
    `make fmt` followed by `make lint` fails on its own output. People respond by turning one
    of them off."""
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    ruff = tool["ruff"]
    assert isinstance(ruff.get("line-length"), int)


# ------------------------------------------------ the database (M0.3.1, M0.3.3, M0.3.6)
@pytest.mark.parametrize("extension", ["vector", "pg_trgm", "fuzzystrmatch", "unaccent"])
def test_the_first_migration_installs_every_extension_the_system_needs(extension: str) -> None:
    """Each of these fails late and unhelpfully when absent. Without `vector` the embedding
    column will not create; without `pg_trgm` fuzzy name matching silently becomes exact
    matching, which returns fewer records rather than an error - and fewer records from a
    permission-aware system reads as a permission decision."""
    first = (REPO / "migrations" / "versions" / "0001_foundation.py").read_text(encoding="utf-8")
    assert extension in first


def test_the_application_role_cannot_bypass_row_level_security() -> None:
    """The single most important line in the schema. A role with BYPASSRLS makes every
    policy in the system decorative, and nothing else fails: queries return more rows, which
    looks like the system working.

    Asserted against the migration that creates the role. CI asserts it again against a
    real database, and both are worth having: this one fails in ninety seconds on a laptop,
    that one catches a role created by hand on a server.
    """
    text = " ".join(
        p.read_text(encoding="utf-8") for p in (REPO / "migrations" / "versions").glob("*.py")
    )
    assert "NOBYPASSRLS" in text
    assert "NOSUPERUSER" in text


@pytest.mark.parametrize("setting", ["shared_buffers", "work_mem"])
def test_every_compose_profile_sets_its_memory_explicitly(setting: str) -> None:
    """Postgres defaults are sized for a machine from a decade ago, and this host runs a
    second production system belonging to a different project. An unset `shared_buffers` is
    not a performance problem here, it is a neighbour's outage."""
    for name in ("docker-compose.yml", "docker-compose.lite.yml", "docker-compose.staging.yml"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert setting in text, f"{name} does not set {setting}"


# ------------------------------------------------ the environment example (M0.4.3)
def test_every_variable_the_example_lists_carries_an_explanation() -> None:
    """An example file that is only a list of names is a list of things to guess at. The
    explanation is the whole value of the file: `POSTGRES_PASSWORD=` tells nobody whether it
    is the one the app uses, the one the pooler uses, or both."""
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    unexplained: list[str] = []
    for i, line in enumerate(lines):
        if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
            continue
        # A comment directly above, or on a line before the blank that precedes it.
        preceding = [text.strip() for text in lines[max(0, i - 3) : i]]
        if not any(text.startswith("#") for text in preceding):
            unexplained.append(line.split("=")[0])
    assert not unexplained, f"variables with no explanation: {unexplained}"


def test_the_example_holds_no_real_looking_secret() -> None:
    """An example file is committed, public in any repository that ever becomes public, and
    copied verbatim by whoever sets the system up next. A plausible-looking value in it gets
    used in production by somebody in a hurry."""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        # A long opaque string is what a real credential looks like. Placeholders are short,
        # or obviously not secrets, or say what to put there.
        assert not (len(value) > 24 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", value)), (
            f"{line.split('=')[0]} looks like a real credential"
        )
