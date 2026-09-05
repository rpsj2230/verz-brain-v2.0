"""Which settings are required where, and what happens when they are missing.

Task ids: M31.3.1.1, M31.3.1.2, M31.3.1.3
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import AliasChoices, AliasPath
from pydantic.fields import FieldInfo

from brain.app import Settings
from brain.config import assert_valid, check, required_for
from brain.ops.wiring import DEFAULT_PROFILE

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / ".env.example"

#: Where a variable can actually be read from. Documentation is deliberately absent: a
#: variable mentioned only in a runbook is a variable nothing reads, and a `.env.example`
#: that accumulates those becomes a list of things to guess at rather than a list of what
#: to set.
READERS = (
    "src",
    "migrations",
    "ops",
    ".github",
    "Makefile",
    "Dockerfile",
    "alembic.ini",
    "docker-compose.yml",
    "docker-compose.lite.yml",
    "docker-compose.staging.yml",
)


# --------------------------------------------------------------- requirements
def test_development_requires_nothing() -> None:
    """Running the documents locally with no Postgres is a normal thing to do."""
    assert required_for("development") == ()
    assert check("development", {}) == []


def test_requirements_are_cumulative() -> None:
    """A setting required in staging cannot be forgotten in production."""
    assert set(required_for("staging")) <= set(required_for("production"))


def test_an_unknown_environment_gets_the_strictest_rules() -> None:
    """A typo in BRAIN_ENV must not be a way to relax the checks."""
    assert required_for("prod") == required_for("production")
    assert required_for("") == required_for("production")


def test_the_bug_that_actually_happened_is_caught() -> None:
    """DATABASE_URL was unset in production, the app skipped migrations because of an
    `if`, and reported healthy. An empty string is a perfectly valid string, which is how
    a missing setting became a working configuration that did the wrong thing."""
    problems = check("production", {"database_url": "", "valkey_url": "redis://x"})
    assert any(p.setting == "database_url" for p in problems)


def test_whitespace_is_not_a_value() -> None:
    assert check("production", {"database_url": "   ", "valkey_url": "redis://x"})


# ------------------------------------------------------------------ defaults
def test_a_well_known_password_is_rejected_outside_development() -> None:
    """Each of these has been a real breach somewhere. They are in every word list."""
    problems = check(
        "production",
        {
            "database_url": "postgresql://real",
            "valkey_url": "redis://x",
            "app_role_password": "change-me",
        },
    )
    assert any(p.setting == "app_role_password" for p in problems)


def test_development_tolerates_a_throwaway_password() -> None:
    assert check("development", {"app_role_password": "change-me"}) == []


def test_a_cors_wildcard_in_production_is_a_problem() -> None:
    """A wildcard plus the interactive docs publishes the name of every tool and
    capability in the system."""
    problems = check(
        "production",
        {"database_url": "postgresql://real", "valkey_url": "redis://x", "cors_origins": "*"},
    )
    assert any(p.setting == "cors_origins" for p in problems)


@pytest.mark.parametrize(
    "origins",
    [
        "*,https://console.example.com",
        "https://console.example.com,*",
        "https://console.example.com, * ",
    ],
)
def test_a_wildcard_beside_a_real_origin_is_still_a_wildcard(origins: str) -> None:
    """The spelling this check missed, and the likely one.

    `serve.py` hands the setting over comma-joined, and this compared the whole joined string
    to `"*"`. So the wildcard on its own was caught and the wildcard *plus* a real origin was
    not, which is the version that actually happens: somebody adds the console origin and
    forgets to take the wildcard out, and the setting now looks configured.

    For CORS alone that is a browser convenience. It stops being one on the widget mint path,
    where an allowed origin is permission to mint anonymous credentials against a client's
    brain.

    Delete this and the check is satisfiable by appending anything to the wildcard."""
    problems = check(
        "production",
        {"database_url": "postgresql://real", "valkey_url": "redis://x", "cors_origins": origins},
    )

    assert any(p.setting == "cors_origins" for p in problems)


def test_a_cors_wildcard_is_a_problem_in_staging_too() -> None:
    """Staging holds a copy of the same shape of data and is reachable from the same
    internet. The check fired only in production, which is the one environment where somebody
    is most careful anyway."""
    problems = check(
        "staging",
        {"database_url": "postgresql://real", "valkey_url": "redis://x", "cors_origins": "*"},
    )

    assert any(p.setting == "cors_origins" for p in problems)


def test_development_keeps_the_wildcard_escape_hatch() -> None:
    """So the check cannot be widened into refusing every wildcard everywhere, which would
    satisfy both tests above and make local work annoying enough to be switched off. A
    developer's ports move, and a machine with no client data on it is not what this
    protects."""
    problems = check("development", {"cors_origins": "*"})

    assert not any(p.setting == "cors_origins" for p in problems)


def test_a_named_origin_list_is_not_flagged() -> None:
    """The positive case. A check that flagged every non-empty list would satisfy the three
    above and stop anybody configuring CORS at all."""
    problems = check(
        "production",
        {
            "database_url": "postgresql://real",
            "valkey_url": "redis://x",
            "cors_origins": "https://console.example.com,https://www.client.example",
        },
    )

    assert not any(p.setting == "cors_origins" for p in problems)


# ------------------------------------------------------------------ reporting
def test_a_lite_deployment_pointed_at_a_trace_ledger_fails_before_the_port_is_bound() -> None:
    """The profile flag reaching the one place it can still stop a deployment.

    `brain.ops.wiring` can compute the conflict all it likes; if nothing calls it at
    startup, a lite install carrying a LANGFUSE_HOST from a standard one ships spans to it
    for the rest of its life, and neither outcome is visible from the application. Delete
    this and the check goes back to being a function nobody invokes."""
    problems = check(
        "production",
        {
            "database_url": "postgresql://brain:s3cret@db:5432/brain",
            "valkey_url": "redis://cache:6379/0",
            "profile": "lite",
            "langfuse_host": "https://cloud.langfuse.com",
        },
    )

    assert [p.setting for p in problems] == ["profile"]


def test_a_profile_nobody_defined_stops_the_deployment_rather_than_selecting_nothing() -> None:
    """Every other problem here is a value to fix and is collected. This one is not: an
    unknown profile cannot select any components, so reporting it alongside the others
    would mean the component set had already been chosen, from an empty set. Delete this
    and BRAIN_PROFILE=lte deploys the four base services and silently nothing else."""
    with pytest.raises(Exception, match="unknown profile"):
        check("production", {"database_url": "x", "valkey_url": "y", "profile": "lte"})


def test_an_install_that_never_sets_a_profile_is_quiet_and_runs_no_trace_ledger() -> None:
    """The default has to be both safe and quiet, and those are two separate claims.

    **Quiet** is the easy half: every deployment in existence today sets no BRAIN_PROFILE,
    so a default that raised would refuse the running production stack.

    **Safe** is the half a mutation caught. Asserting only that an unset profile produces
    no problems does not pin the default at all: with no `langfuse_*` set, `lite` and
    `full` both produce zero problems, and changing the default to `full` passed the
    earlier version of this test untouched. So the default is pinned by the case where the
    profiles differ, which is a trace destination being present. Under a `full` default
    that is permitted and this test fails.

    Delete this and the default can drift to a profile that does not fit the host and
    silently starts shipping spans."""
    base = {"database_url": "postgresql://brain:s3cret@db:5432/brain", "valkey_url": "redis://c"}

    assert check("production", base) == []
    assert [p.setting for p in check("production", {**base, "langfuse_host": "https://x"})] == [
        "profile"
    ], "the default profile must be one that runs no trace ledger"


def test_the_default_profile_is_spelled_in_exactly_one_place() -> None:
    """`Settings.profile` and `brain.config.check` both need a default, and for a short
    while both spelled it. Two defaults for one setting disagree the first time one is
    edited, and the disagreement is silent: the settings object says lite, the validator
    says something else, and which one you get depends on whether the key reached the dict.

    Delete this and the literal can come back in either file."""
    assert Settings().profile == DEFAULT_PROFILE
    assert DEFAULT_PROFILE == "lite"


def test_every_problem_is_reported_at_once() -> None:
    """Reporting one at a time turns a misconfigured deployment into a sequence of
    restarts, each revealing the next thing."""
    problems = check("production", {})
    assert len(problems) >= 2


def test_each_problem_says_how_to_fix_it() -> None:
    for p in check("production", {}):
        assert p.fix.strip()
        assert p.setting in str(p)


def test_assert_valid_raises_with_everything_in_the_message() -> None:
    with pytest.raises(RuntimeError, match="configuration is not valid for production") as exc:
        assert_valid("production", {})
    assert "database_url" in str(exc.value)
    assert "valkey_url" in str(exc.value)


def test_assert_valid_is_silent_when_there_is_nothing_wrong() -> None:
    assert_valid("production", {"database_url": "postgresql://real", "valkey_url": "redis://x"})


# ------------------------------------- the example file documents them all (M31.3.1.2)
def _documented() -> set[str]:
    """Every variable the example actually assigns. A name inside a comment is an
    explanation, not a declaration, so only assignments count."""
    return {
        line.split("=")[0]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^[A-Z][A-Z0-9_]*=", line)
    }


def _accepted_names(name: str, field: FieldInfo) -> set[str]:
    """Every environment variable pydantic-settings would populate this field from.

    Derived from the model rather than typed out, which is the whole point: a field added
    with no alias gets `BRAIN_` plus its name, a field with `AliasChoices` gets exactly the
    names it lists, and neither has to be remembered by whoever adds the next one.
    """
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        return {str(choice) for choice in alias.choices if not isinstance(choice, AliasPath)}
    if isinstance(alias, str):
        return {alias}
    prefix = str(Settings.model_config.get("env_prefix") or "")
    return {f"{prefix}{name}".upper()}


@pytest.mark.parametrize("setting", sorted(Settings.model_fields))
def test_every_setting_the_application_reads_is_documented_in_the_example(setting: str) -> None:
    """Parametrised from `Settings` itself, so a new setting arrives here already failing.

    This is the whole of M31.3.1.2 and it only works in this direction. A test asserting the
    example is well formed passes on an example that is missing half the settings, because a
    shorter file is still a valid file. What makes the example worth having is that it is
    complete, and completeness is only checkable against the object doing the reading.

    Deleting this means the next setting is added with a default, works everywhere the
    default is right, and is discovered by whoever deploys into the environment where it is
    not - with nothing in the example to tell them the variable exists.
    """
    accepted = _accepted_names(setting, Settings.model_fields[setting])
    assert accepted & _documented(), (
        f"{setting} is read from {sorted(accepted)} and .env.example documents none of them"
    )


@pytest.mark.parametrize("variable", sorted(_documented()))
def test_every_variable_the_example_lists_is_read_by_something(variable: str) -> None:
    """The other direction, and it decays faster than the first. A setting that is removed
    from the code leaves its line in the example, and the line reads exactly like the ones
    that still matter: somebody sets it, nothing happens, and they go looking for the bug in
    their own configuration.

    `Settings` is checked first because pydantic derives most of these names rather than
    spelling them, so `BRAIN_CORS_ORIGINS` appears nowhere in the source and is read all the
    same. Everything else has to appear somewhere that runs.
    """
    for name, field in Settings.model_fields.items():
        if variable in _accepted_names(name, field):
            return

    for root in READERS:
        where = REPO / root
        paths = (
            [p for p in where.rglob("*") if p.is_file() and p.suffix not in {".md", ".pyc"}]
            if where.is_dir()
            else [where]
        )
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            if variable in path.read_text(encoding="utf-8", errors="ignore"):
                return
    pytest.fail(f"{variable} is documented in .env.example and nothing reads it")


def test_the_launcher_refuses_to_bind_a_port_on_a_bad_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check runs before uvicorn starts, so a misconfigured container fails to start
    rather than serving wrongly."""
    import brain.serve as serve

    monkeypatch.setenv("BRAIN_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("VALKEY_URL", raising=False)
    monkeypatch.delenv("BRAIN_VALKEY_URL", raising=False)

    started = False

    def spy(*_a: object, **_k: object) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr("uvicorn.run", spy)
    with pytest.raises(RuntimeError, match="configuration is not valid"):
        serve.main()
    assert not started, "uvicorn was started despite an invalid configuration"
