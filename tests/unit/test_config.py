"""Which settings are required where, and what happens when they are missing.

Task ids: M31.3.1.1, M31.3.1.3
"""

from __future__ import annotations

import pytest

from brain.config import assert_valid, check, required_for


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


# ------------------------------------------------------------------ reporting
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
