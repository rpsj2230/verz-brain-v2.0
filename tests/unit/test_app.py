"""The application shell: health, tracing, error mapping, headers.

Task ids: M31.1.1, M31.1.2, M31.1.3, M31.1.1.1, M31.1.1.4, M31.1.3.1, M31.1.3.2,
M31.1.3.3, M31.1.3.4, M31.1.3.5, M31.2.2.1

Deliberately not claimed here: M31.1.1.2 and M31.1.1.5, which ask for the lifespan to
attach Valkey, OpenBao and the model registry and for readiness to gate on all three.
Only the database is attached today. The cache client exists and is untested against a
live server; nothing wires it. Claiming those two would mark as done the exact thing
that makes a half-connected instance answer from whatever it can still reach.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.app import Settings, create_app
from brain.core.errors import Absent, Degraded, Denied, Unresolved
from brain.ops.release_manifest import ReleaseManifest


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


def test_the_running_commit_comes_from_the_image_when_the_environment_says_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on the live server rather than imagined. The image carried
    `BRAIN_COMMIT_SHA=724cf3f` and the container was created with `BRAIN_COMMIT_SHA=unknown`,
    because Coolify keeps its own copy of the compose file, that copy had resolved a
    `${COMMIT_SHA:-unknown}` default to the literal string at save time, and an explicit
    environment entry in compose beats an image's ENV. `/health/live` answered "unknown"
    while the status page beside it reported the truth.

    Baking the value into the image was the previous fix and was not enough, for the same
    reason: compose can override anything ENV sets. A file cannot be overridden by an
    environment variable.

    Deleting this leaves the endpoint answering "unknown" on a server nobody can then
    identify, which is a deployment nobody can roll back with confidence."""
    manifest = tmp_path / "RELEASE.json"
    manifest.write_text(
        ReleaseManifest(commit="c" * 40, built_at=datetime(2026, 9, 5, tzinfo=UTC)).to_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr("brain.ops.release_manifest.MANIFEST_PATH", manifest)

    app = create_app(Settings(env="development", commit_sha="unknown"))
    with TestClient(app) as c:
        assert c.get("/health/live").json()["commit"] == "c" * 7


def test_an_environment_that_names_a_commit_is_believed_over_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer running `BRAIN_COMMIT_SHA=wip` means it. Only the default value - the
    thing a variable says when nobody set it - falls through to the file."""
    manifest = tmp_path / "RELEASE.json"
    manifest.write_text(
        ReleaseManifest(commit="c" * 40, built_at=datetime(2026, 9, 5, tzinfo=UTC)).to_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr("brain.ops.release_manifest.MANIFEST_PATH", manifest)

    app = create_app(Settings(env="development", commit_sha="wip"))
    with TestClient(app) as c:
        assert c.get("/health/live").json()["commit"] == "wip"


def test_a_broken_manifest_does_not_stop_the_health_check_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest answer is then still "unknown", which is what the caller already had. A
    liveness endpoint that raises because a metadata file is malformed turns a cosmetic
    problem into a restart loop."""
    manifest = tmp_path / "RELEASE.json"
    manifest.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("brain.ops.release_manifest.MANIFEST_PATH", manifest)

    app = create_app(Settings(env="development", commit_sha="unknown"))
    with TestClient(app) as c:
        assert c.get("/health/live").json()["commit"] == "unknown"


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
        assert c.get("/redoc").status_code == 404


def test_fastapis_own_schema_is_off_in_production_not_only_the_docs_page() -> None:
    """This test used to check `/docs` alone, and read as though the schema were handled.

    It was not. Production served the complete generated schema at `/openapi.json`:
    fourteen paths, every operation, every response model's field names, and no security
    scheme described. `/docs` was correctly 404 the whole time, which is exactly what made
    it look covered. Turning off the reading room and leaving the catalogue on the doorstep.

    Nothing sensitive was behind those paths, because no route is behind the gate yet. That
    is luck rather than design: the schema is generated from whatever is mounted, so the
    first real endpoint would have published itself.
    """
    prod = create_app(Settings(env="production"))
    assert prod.openapi_url is None, "FastAPI is still generating its own schema route"


def test_the_public_schema_that_is_served_describes_authentication() -> None:
    """A schema with no security scheme tells a reader the API is open. Ours is not, and a
    document that implies otherwise is worse than no document."""
    prod = create_app(Settings(env="production"))
    with TestClient(prod) as c:
        body = c.get("/openapi.json").json()
    assert body["components"]["securitySchemes"]


def test_the_public_schema_enumerates_no_scopes() -> None:
    """A scopes map is the permission map, served unauthenticated. It is the document this
    endpoint exists to avoid publishing."""
    prod = create_app(Settings(env="production"))
    with TestClient(prod) as c:
        body = c.get("/openapi.json").json()
    for scheme in body["components"]["securitySchemes"].values():
        assert "flows" not in scheme, "an OAuth2 flows block carries a scopes map"


def test_a_route_under_the_api_prefix_is_absent_from_the_public_schema() -> None:
    """The property that matters going forward. Today every mounted path happens to be a
    public build route; the first real endpoint must not publish itself."""
    from brain.api import API_PREFIX

    prod = create_app(Settings(env="production"))

    @prod.get(f"{API_PREFIX}/clients", tags=["docs"])
    async def _clients() -> dict[str, str]:
        return {}

    with TestClient(prod) as c:
        body = c.get("/openapi.json").json()
    assert f"{API_PREFIX}/clients" not in body["paths"], (
        "a path under the API prefix reached the public schema even though it is tagged "
        "public; the two conditions are meant to be independent"
    )


def test_an_untagged_route_is_absent_from_the_public_schema() -> None:
    """Deny by default. An operation that loses its tags in a refactor must disappear from
    the public document rather than appear in it."""
    prod = create_app(Settings(env="production"))

    @prod.get("/whatever")
    async def _whatever() -> dict[str, str]:
        return {}

    with TestClient(prod) as c:
        body = c.get("/openapi.json").json()
    assert "/whatever" not in body["paths"]


def test_docs_are_available_outside_production(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


def test_a_configured_origin_is_the_only_one_allowed() -> None:
    """The other half of the CORS rule, and the half that matters. "Closed by default" is
    worth nothing if configuring one origin opens all of them, which is what a wildcard in
    the allow list would do.

    Also asserts the methods and headers, because a permissive `allow_methods=["*"]` beside
    a strict origin list is the usual way this ends up open: the origin looks tight and the
    surface behind it is not."""
    app = create_app(Settings(cors_origins=("https://console.example.com",)))
    with TestClient(app) as c:
        allowed = c.options(
            "/health/live",
            headers={
                "Origin": "https://console.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.headers.get("access-control-allow-origin") == "https://console.example.com"

        refused = c.options(
            "/health/live",
            headers={
                "Origin": "https://somewhere-else.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert refused.headers.get("access-control-allow-origin") is None


def test_the_trace_id_reaches_every_log_line_and_not_only_the_response() -> None:
    """The response header is for the person reporting the problem; the log line is for
    whoever then has to find it. A trace id on one and not the other means an id somebody
    can quote and nobody can search for.

    Bound in a contextvar rather than passed as an argument, so a log call five frames deep
    in the gate carries it without every function in between taking a parameter it does not
    use. Asserted by capturing what structlog actually emitted during a request, because the
    binding is the kind of thing a refactor drops without any test noticing."""
    captured: list[dict[str, object]] = []

    def capture(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> str:
        # Returns a string because structlog hands the last processor's return value to the
        # underlying logger, and a dict arrives there as keyword arguments PrintLogger has
        # no parameters for. Swallowing the line also keeps test output readable.
        captured.append(dict(event_dict))
        return ""

    app = create_app(Settings(env="development", commit_sha="abc1234"))

    @app.get("/_log_something")
    async def _log_something() -> dict[str, str]:
        # A stand-in for any code the gate runs five frames deeper. It takes no trace id
        # as an argument, which is the property being tested.
        structlog.get_logger().info("did a thing")
        return {"ok": "yes"}

    original = structlog.get_config()["processors"]
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, capture])
    try:
        with TestClient(app) as c:
            r = c.get("/_log_something", headers={"x-trace-id": "tr_findme"})
    finally:
        structlog.configure(processors=original)

    assert r.headers["x-trace-id"] == "tr_findme"

    during_request = [e for e in captured if e.get("event") == "did a thing"]
    assert during_request, f"the route's own log line never appeared: {captured}"
    assert all(e.get("trace_id") == "tr_findme" for e in during_request), during_request
    assert all(e.get("path") == "/_log_something" for e in during_request), during_request

    # And the lines that belong to no request carry no trace id, which is right rather than
    # a gap: startup and shutdown happen outside any request, and inventing an id for them
    # would put a searchable identifier on a line no report will ever quote. The contextvar
    # is cleared in a `finally`, so a leaked id from a previous request cannot appear here.
    lifecycle = [e for e in captured if e.get("event") in {"starting", "shutting down"}]
    assert lifecycle, "the lifespan logged nothing, so this half asserts nothing"
    assert all("trace_id" not in e for e in lifecycle), lifecycle


def test_cors_is_closed_unless_origins_are_configured() -> None:
    """No wildcard default. An unset origin list means no cross-origin access at all,
    rather than any origin."""
    assert create_app(Settings()).state.settings.cors_origins == ()


# ------------------------------------------------- what readiness does not say (M31.1.1.5)
def test_readiness_reports_tools_ready_while_the_catalogue_is_empty() -> None:
    """**Measured on production on 2026-09-06: `tools=0` in the log, `{"tools": true}` on
    `/health/ready`.** Both statements are accurate and together they read as something that
    is not true.

    This module's docstring defines readiness as "can this process answer a question
    correctly", and a process holding no tools cannot answer anything. The check is really
    asserting that the catalogue was built and passed `freeze`, and an empty catalogue passes
    trivially, so the flag cannot distinguish a wired application from an unwired one.

    The emptiness itself is expected and argued in `brain.tools.startup`: `RowSource.rows` is
    synchronous, this application has an `AsyncEngine`, and each of the three ways out changes
    the deployed connection profile. Nothing is being hidden. What this test refuses is the
    gap being undocumented at the place an operator actually looks.

    **The assertion is deliberately the awkward way round.** It pins the *current* state, so
    it fails the day somebody passes a row source and the application starts registering
    tools. That is the point: whoever wires it has to come back here, read the paragraph in
    `lifespan`, and decide what readiness should mean once the answer can be yes.

    Delete this and a readiness check that has been green for months while the system could
    not answer a single question stays green, and stays unexamined."""
    app = create_app(Settings(env="development", commit_sha="abc1234"))
    with TestClient(app) as c:
        body = c.get("/health/ready").json()

        assert body["checks"]["tools"] is True
        assert len(app.state.tools) == 0, (
            "the application now registers tools, so the readiness paragraph in "
            "brain.app.lifespan is out of date and needs rewriting by whoever wired it"
        )


def test_the_gap_between_a_valid_catalogue_and_a_useful_one_is_written_down() -> None:
    """The guard on the test above. That one pins behaviour; this one pins the explanation,
    because a future reader meeting `ready["tools"] = True` beside `tools=0` needs the reason
    at the line rather than in a commit message nobody will find.

    Matched on whitespace-collapsed source so re-wrapping the paragraph does not fail it, and
    on a phrase that carries the argument rather than on a word that could appear anywhere.

    Delete this and the paragraph can be removed as noise by somebody tidying comments."""
    import inspect

    from brain import app as app_module

    # Comment markers are stripped before the lines are joined. Collapsing whitespace alone
    # leaves a "#" in the middle of any sentence that wraps, so a phrase spanning two comment
    # lines never matches and the test passes or fails on where the author happened to wrap.
    raw = inspect.getsource(app_module.lifespan)
    prose = " ".join(line.lstrip().lstrip("#").strip() for line in raw.splitlines())
    source = " ".join(prose.split())

    assert "does not say there is anything in it" in source
    assert "restart loop rather than a signal" in source
