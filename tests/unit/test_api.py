"""The conventions every endpoint follows.

Task ids: M31.1.2.4, M31.1.4.1, M31.1.4.3, M31.1.4.4
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api import (
    API_PREFIX,
    COMMON_RESPONSES,
    ErrorBody,
    Page,
    decode_cursor,
    encode_cursor,
)
from brain.app import Settings, create_app


# --------------------------------------------------------------- versioning
def test_the_version_is_in_the_path_not_a_header() -> None:
    """A version you cannot see in a log line is a version nobody can debug."""
    assert API_PREFIX.startswith("/api/v")


# ------------------------------------------------------------------ errors
def test_the_error_body_carries_the_trace_id() -> None:
    """The only field that makes a support conversation short."""
    e = ErrorBody(message="I could not find that.", trace_id="abc123", outcome="absent")
    assert e.trace_id == "abc123"


def test_denied_and_absent_share_a_documented_status() -> None:
    """404 describes both, in one sentence, so the schema itself does not give the game
    away."""
    assert "Deliberately the same" in COMMON_RESPONSES[404]["description"]
    assert COMMON_RESPONSES[404]["model"] is ErrorBody


def test_degraded_says_nothing_stale_was_substituted() -> None:
    assert "stale" in COMMON_RESPONSES[503]["description"]


# --------------------------------------------------------------- pagination
def test_a_cursor_round_trips() -> None:
    pos = {"after_id": "c_0447", "sort": "name"}
    assert decode_cursor(encode_cursor(pos)) == pos


def test_a_cursor_is_opaque_to_the_caller() -> None:
    """Opaque so a client cannot couple itself to the ordering, which is ours to change.
    Not encrypted, which is why nothing a caller may not see goes in one."""
    c = encode_cursor({"after_id": "c_0447"})
    assert "c_0447" not in c


@pytest.mark.parametrize("bad", ["", "!!!!", "bm90LWpzb24=", "eyJhIjogMX0=x", "W10="])
def test_a_malformed_cursor_is_a_clean_error(bad: str) -> None:
    """A client error, not a server one, and never a traceback describing the ordering."""
    with pytest.raises(ValueError, match="malformed cursor"):
        decode_cursor(bad)


def test_a_page_reports_no_more_as_a_null_cursor() -> None:
    p: Page[str] = Page(items=["a", "b"])
    assert p.next_cursor is None


def test_total_is_optional() -> None:
    """A count behind a permission predicate costs a full scan. 'About 4,000' is not worth
    a second of someone's question."""
    assert Page[str]().total is None


# ----------------------------------------------------------------- timeout
def test_a_slow_request_returns_degraded_not_gateway_timeout() -> None:
    """503, not 504. The caller did not time out; one of our dependencies did, and the
    taxonomy already has a word for that."""
    import asyncio

    from brain.api import TimeoutMiddleware

    app: FastAPI = create_app(Settings(env="development"))
    app.middleware("http")(TimeoutMiddleware(seconds=0.05))

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(2)
        return {"never": "arrives"}

    with TestClient(app) as c:
        r = c.get("/slow")
        assert r.status_code == 503
        assert r.json()["outcome"] == "degraded"
        assert "Nothing was changed" in r.json()["message"]


def test_a_fast_request_is_untouched() -> None:
    from brain.api import TimeoutMiddleware

    app: FastAPI = create_app(Settings(env="development"))
    app.middleware("http")(TimeoutMiddleware(seconds=5))
    with TestClient(app) as c:
        assert c.get("/health/live").status_code == 200
