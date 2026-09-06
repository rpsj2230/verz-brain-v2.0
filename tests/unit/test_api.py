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
    e = ErrorBody(message="I could not find that.", trace_id="abc123")
    assert e.trace_id == "abc123"
    # No `outcome` here, and its absence is the point rather than an omission: see
    # `api.A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT`. The field used to exist and
    # this test used to pass it.


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

    app: FastAPI = create_app(Settings(env="development", request_timeout_seconds=0.05))

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(2)
        return {"never": "arrives"}

    with TestClient(app) as c:
        r = c.get("/slow")

    # 503 is what says "degraded" now. The body used to carry an `outcome` field and no
    # longer does: `handle_brain_error` maps DENIED and ABSENT to one status and one body on
    # purpose, and a field naming the outcome would have made those two distinguishable in
    # the one place a client reads. See `api.A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT`.
    assert r.status_code == 503
    assert "Nothing was changed" in r.json()["message"]
    assert "outcome" not in r.json()

    # And the deadline came from the setting rather than from this test attaching the
    # middleware by hand, which is what the previous version did and is exactly how an
    # unmounted mechanism looks tested.
    assert r.json()["trace_id"], "a timed-out response carries no trace id to quote"


def test_a_fast_request_is_untouched() -> None:
    from brain.api import TimeoutMiddleware

    app: FastAPI = create_app(Settings(env="development"))
    app.middleware("http")(TimeoutMiddleware(seconds=5))
    with TestClient(app) as c:
        assert c.get("/health/live").status_code == 200


def test_the_deployed_application_actually_has_a_request_deadline() -> None:
    """**`TimeoutMiddleware` existed, was tested, and was mounted by nothing.** `create_app`
    installed CORS, tracing and security headers and never this one, so every request the
    deployed application has ever served ran without a deadline, while M31.1.2.4 was closed
    and `Settings.request_timeout_seconds` sat at 30.0 read by nobody.

    The test beside this one did not notice, because it built an application and attached the
    middleware itself. That is the shape that makes an unmounted mechanism look covered: it
    proves the middleware works when somebody adds it, and says nothing about whether anybody
    does.

    So this asserts over the stack `create_app` actually returns. Behind a pooler with a fixed
    number of client slots, enough held connections is an outage, which is the failure the
    middleware was written to prevent and the one it was not preventing.

    Delete this and the middleware can be unmounted again with every other test in this file
    still green, which is precisely what happened.

    Task ids: M31.1.2.4"""
    from brain.api import TimeoutMiddleware

    app: FastAPI = create_app(Settings(env="development"))

    dispatchers = [m.kwargs.get("dispatch") for m in app.user_middleware if "dispatch" in m.kwargs]
    timeouts = [d for d in dispatchers if isinstance(d, TimeoutMiddleware)]

    assert timeouts, "create_app returns an application with no request deadline on it"
    assert timeouts[0].seconds == Settings().request_timeout_seconds, (
        "the deadline is a number typed in create_app rather than the configured setting"
    )


def test_the_deadline_sits_inside_the_tracing_middleware() -> None:
    """Order, and it is not cosmetic. Starlette inserts each new middleware at the front, so
    the last registered runs outermost. The deadline is registered first and therefore sits
    innermost, inside `trace`.

    That is what lets a timed-out response carry a trace id: the id is bound by the tracing
    middleware on the way in, so the body of the 503 and the `x-trace-id` header the caller
    gets back name the same run. Registered last instead, the deadline would fire outside the
    binding and answer with an empty id for every caller who did not send one, which is most
    of them.

    Delete this and the two can be reordered by somebody tidying `create_app`, and the only
    symptom is support tickets nobody can trace.

    Task ids: M31.1.2.4"""
    from brain.api import TimeoutMiddleware

    app: FastAPI = create_app(Settings(env="development"))
    names = [
        type(m.kwargs["dispatch"]).__name__
        if not callable(m.kwargs.get("dispatch"))
        else getattr(m.kwargs["dispatch"], "__name__", type(m.kwargs["dispatch"]).__name__)
        for m in app.user_middleware
        if "dispatch" in m.kwargs
    ]
    positions = {n: i for i, n in enumerate(names)}

    assert "trace" in positions, f"the tracing middleware is gone; stack is {names}"
    assert TimeoutMiddleware.__name__ in positions, f"the deadline is gone; stack is {names}"
    assert positions["trace"] < positions[TimeoutMiddleware.__name__], (
        f"the deadline is outside tracing, so a timed-out response has no id: {names}"
    )
