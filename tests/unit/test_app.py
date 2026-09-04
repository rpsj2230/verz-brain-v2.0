"""The application shell: health, tracing, error mapping, headers.

Task ids: M31.1.1, M31.1.2, M31.1.3
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.app import Settings, create_app
from brain.core.errors import Absent, Degraded, Denied, Unresolved


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(env="development", commit_sha="abc1234"))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ health
def test_liveness_reports_the_running_commit(client: TestClient) -> None:
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "commit": "abc1234", "checks": {}}


def test_readiness_is_ok_when_nothing_has_registered_a_check(client: TestClient) -> None:
    assert client.get("/health/ready").status_code == 200


def test_readiness_fails_when_any_dependency_is_unreachable(app: FastAPI) -> None:
    """Deployment gates on readiness, so a half-connected instance must fail it.

    An instance that is up but cannot reach the database does not refuse — it answers
    from whatever it can still reach. That is how a permission-aware system starts
    returning wrong answers without anything appearing to be down.
    """
    with TestClient(app) as c:
        app.state.ready = {"database": True, "cache": False, "secrets": True}
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"
        assert r.json()["checks"]["cache"] is False


def test_liveness_stays_ok_while_readiness_fails(app: FastAPI) -> None:
    """The two must be able to disagree, or there is no point having both."""
    with TestClient(app) as c:
        app.state.ready = {"database": False}
        assert c.get("/health/live").status_code == 200
        assert c.get("/health/ready").status_code == 503


# ------------------------------------------------------------------ traces
def test_a_trace_id_is_minted_when_the_caller_sends_none(client: TestClient) -> None:
    r = client.get("/health/live")
    assert len(r.headers["x-trace-id"]) == 32


def test_a_caller_supplied_trace_id_is_preserved(client: TestClient) -> None:
    """So one id spans the channel adapter and the application."""
    r = client.get("/health/live", headers={"x-trace-id": "abc123"})
    assert r.headers["x-trace-id"] == "abc123"


def test_timing_is_reported(client: TestClient) -> None:
    assert client.get("/health/live").headers["server-timing"].startswith("app;dur=")


# ------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("error", "status"),
    [(Denied, 404), (Absent, 404), (Unresolved, 409), (Degraded, 503)],
)
def test_the_taxonomy_maps_to_status_codes(app: FastAPI, error: type, status: int) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise error("internal detail")

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/boom").status_code == status


def test_denied_and_absent_are_indistinguishable_over_http(app: FastAPI) -> None:
    """The taxonomy's whole point, enforced at the boundary. A 403 on a hidden record
    would confirm the record exists."""

    @app.get("/denied")
    async def denied() -> None:
        raise Denied("client 4471 contract_value")

    @app.get("/absent")
    async def absent() -> None:
        raise Absent("no such client")

    with TestClient(app, raise_server_exceptions=False) as c:
        a, b = c.get("/denied"), c.get("/absent")
        assert a.status_code == b.status_code == 404
        assert a.json() == b.json()


def test_internal_detail_never_reaches_the_response_body(app: FastAPI) -> None:
    @app.get("/denied")
    async def denied() -> None:
        raise Denied("client 4471 contract_value is 48000 SGD")

    with TestClient(app, raise_server_exceptions=False) as c:
        assert "4471" not in c.get("/denied").text
        assert "48000" not in c.get("/denied").text


# ---------------------------------------------------------------- headers
def test_security_headers_are_set(client: TestClient) -> None:
    h = client.get("/health/live").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"


# ------------------------------------------------------------------ config
def test_interactive_docs_are_off_in_production() -> None:
    """The schema names every tool and capability. Not something to serve publicly."""
    prod = create_app(Settings(env="production"))
    with TestClient(prod) as c:
        assert c.get("/docs").status_code == 404


def test_docs_are_available_outside_production(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


def test_cors_is_closed_unless_origins_are_configured() -> None:
    """No wildcard default. An unset origin list means no cross-origin access at all,
    rather than any origin."""
    assert create_app(Settings()).state.settings.cors_origins == ()
