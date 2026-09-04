"""Startup migrations and the advisory lock that makes them safe with replicas.

Task ids: M0.3.2, M31.1.1
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain import migrate
from brain.app import Settings, create_app


def test_the_lock_id_is_a_fixed_constant() -> None:
    """Every replica must ask for the same lock. Deriving it from anything that varies —
    a hostname, a pid, the database name — would let two replicas each take a different
    lock and migrate at the same time, which is the exact failure the lock prevents."""
    assert isinstance(migrate.MIGRATION_LOCK_ID, int)
    assert migrate.MIGRATION_LOCK_ID == 8_274_419_003


def test_alembic_config_points_at_the_repository() -> None:
    cfg = migrate._alembic_config("postgresql+psycopg://u:p@h/d")
    assert Path(cfg.get_main_option("script_location") or "").name == "migrations"
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+psycopg://u:p@h/d"


def test_percent_in_a_password_is_escaped_for_configparser() -> None:
    """alembic.ini is read by configparser, which treats % as interpolation. A password
    containing one would raise rather than connect."""
    cfg = migrate._alembic_config("postgresql+psycopg://u:pa%%ss@h/d")
    assert "%%" in (cfg.get_main_option("sqlalchemy.url") or "")


# ------------------------------------------------------------------ startup
def test_startup_skips_migrations_when_no_database_is_configured() -> None:
    """The documents and the status page serve without a database, so a missing
    DATABASE_URL must not stop the app booting."""
    app = create_app(Settings(env="development", database_url=""))
    with TestClient(app) as c:
        assert c.get("/health/ready").status_code == 200
        assert "migrations" not in c.get("/health/ready").json()["checks"]


def test_a_failed_migration_leaves_the_app_unready_rather_than_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unready is visible on /health/ready and keeps the traceback. A crash would put the
    container in a restart loop and discard the reason on every cycle."""

    def boom(_url: str) -> list[str]:
        msg = "deliberate"
        raise RuntimeError(msg)

    monkeypatch.setattr("brain.app.run_migrations", boom)
    app = create_app(Settings(env="development", database_url="postgresql://u:p@h/d"))
    with TestClient(app) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["migrations"] is False


def test_migrations_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who applies migrations by hand sets this and the app does not try."""
    called = False

    def spy(_url: str) -> list[str]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("brain.app.run_migrations", spy)
    app = create_app(
        Settings(env="development", database_url="postgresql://u:p@h/d", run_migrations=False)
    )
    with TestClient(app):
        pass
    assert not called


def test_a_successful_migration_marks_the_app_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.app.run_migrations", lambda _url: ["0001"])
    app = create_app(Settings(env="development", database_url="postgresql://u:p@h/d"))
    with TestClient(app) as c:
        r = c.get("/health/ready")
        assert r.status_code == 200
        assert r.json()["checks"]["migrations"] is True


# ------------------------------------------------------------- regression
def test_the_plain_database_url_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression, found by the CI stack job on 2026-09-04.

    Settings carries env_prefix="BRAIN_", so `database_url` looked only at
    BRAIN_DATABASE_URL. Compose, alembic, and every operator on earth write
    DATABASE_URL. The deployed app therefore found nothing, skipped migrations, and
    reported healthy against an empty schema - and nothing failed, because an app with no
    database is perfectly able to serve documents.
    """
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    assert Settings().database_url == "postgresql://u:p@h/d"


def test_the_prefixed_name_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are accepted; the explicit one takes precedence so a host-wide DATABASE_URL
    cannot quietly override a deliberate per-app setting."""
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://deliberate@h/d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://ambient@h/d")
    assert Settings().database_url == "postgresql://deliberate@h/d"


def test_a_missing_database_outside_development_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence was the actual failure. Outside development, no database means unready."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    app = create_app(Settings(env="staging", database_url=""))
    with TestClient(app) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["database_configured"] is False


def test_development_without_a_database_stays_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running the docs locally with no Postgres is a normal thing to do."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    app = create_app(Settings(env="development", database_url=""))
    with TestClient(app) as c:
        assert c.get("/health/ready").status_code == 200
