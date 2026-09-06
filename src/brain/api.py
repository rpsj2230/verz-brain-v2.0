"""Conventions every endpoint follows.

Written once, here, because a convention that each route implements for itself is a
convention that holds until someone is in a hurry. Three things:

**One error shape.** Every failure returns the same body, so a client parses one thing.
It carries the trace id, which is the only field that makes a support conversation short.

**One pagination shape.** Cursor, not offset. Offset pagination re-reads and re-filters on
every page, and under a permission predicate that means the same row can appear twice or
vanish between pages when someone's grants change mid-scroll. A cursor is a position in a
stable order.

**One request deadline**, distinct from the model's. A model call that takes ninety
seconds is slow; a request that takes ninety seconds is a held connection, and behind a
pooler with two hundred client slots, enough of those is an outage.

Task ids: M31.1.2.4, M31.1.4.1, M31.1.4.3, M31.1.4.4
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = structlog.get_logger()

#: Every route lives under this. A version in the path rather than a header, because a
#: version you cannot see in a log line is a version nobody can debug.
API_PREFIX = "/api/v1"


#: Why the error shape carries no outcome, and why removing the field was not a tidy-up.
#:
#: This model used to carry `outcome: str` described as "denied, absent, unresolved,
#: degraded, failed". `brain.app.handle_brain_error` maps DENIED and ABSENT to the same
#: status and the same body on purpose, because a caller who can tell "you may not see this"
#: from "this does not exist" can map what exists by asking, which is the whole leak the
#: taxonomy is built to prevent. Populating that field would have made the two
#: distinguishable in the one place a client actually reads.
#:
#: It never leaked, and the reason is worth stating rather than being relieved about: the
#: handler has never returned this model. A documented error shape that nothing returns was
#: what kept the field harmless, which is a poor kind of safety, and the fix is to return the
#: model and delete the field rather than to keep both apart and hope.
A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT = (
    "The status and the body are identical for DENIED and ABSENT. Anything that varies "
    "between them is a side channel, whatever it is called and however useful it would be "
    "in a log: a client that can tell a refusal from an absence can enumerate what exists "
    "by asking for things at random and reading which answer comes back. The trace id is "
    "safe to carry because it is minted per request and says nothing about what was asked "
    "for or who asked; the outcome is not, and is deliberately absent from this model."
)


class ErrorBody(BaseModel):
    """The only error shape. Documented once and returned by everything.

    Two fields, and the second one is the reason a person can get help: the trace id is what
    somebody quotes so a run can be found in the ledger, and without it in the body a caller
    has to know to read a response header before they can be told anything useful.

    There is no `outcome`. See `A_REFUSAL_AND_AN_ABSENCE_LOOK_THE_SAME_TO_A_CLIENT`.
    """

    message: str = Field(description="Safe to show a person. Never explains a refusal.")
    trace_id: str = Field(default="", description="Quote this and the run can be found.")


class Page[T](BaseModel):
    """A page of results.

    `next_cursor` is null when there are no more. It is deliberately opaque: a client that
    can construct one has coupled itself to the ordering, and the ordering is ours to
    change.
    """

    items: list[T] = []
    next_cursor: str | None = None
    #: Absent unless it is cheap. A count behind a permission predicate costs a full scan,
    #: and "about 4,000" is not worth a second of someone's question.
    total: int | None = None


def encode_cursor(position: dict[str, Any]) -> str:
    """Opaque, not encrypted. It hides the shape, not a secret — never put a row a caller
    cannot see into one, because a cursor travels back and forth through the client."""
    return base64.urlsafe_b64encode(json.dumps(position, sort_keys=True).encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    """A malformed cursor is a client error, not a server one, and must not leak a
    traceback describing the ordering."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        value = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        msg = "malformed cursor"
        raise ValueError(msg) from exc
    if not isinstance(value, dict):
        msg = "malformed cursor"
        raise ValueError(msg)
    return value


class TimeoutMiddleware:
    """A deadline on the whole request, separate from any model or connector timeout.

    Returns 503 DEGRADED rather than 504: the caller's request did not time out, one of
    our dependencies did, and the taxonomy already has a word for that.

    **This existed, was tested, and was mounted by nothing until today.** `create_app`
    installed CORS, tracing and security headers and never this, so every request the
    deployed application has ever served ran without a deadline, while `M31.1.2.4` was
    closed and `Settings.request_timeout_seconds` sat at 30.0 read by nobody. The test that
    covered it constructed an application and attached the middleware itself, which is the
    shape that makes an unmounted mechanism look tested. `brain.app.create_app` now attaches
    it from the setting, and a test asserts the deployed stack contains it rather than
    asserting that it works when somebody adds it.

    It is attached innermost, inside the trace middleware, for two reasons. The trace id is
    bound by then, so a timed-out response carries the same id as the log line that recorded
    it; and the deadline covers the route rather than the response headers being written
    after it, which would count our own work against the caller's budget.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def __call__(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.seconds)
        except TimeoutError:
            elapsed = time.perf_counter() - started
            log.warning("request deadline exceeded", path=request.url.path, seconds=elapsed)
            return JSONResponse(
                status_code=503,
                # The bound id first, the caller's header as a fallback. Attached inside
                # the trace middleware, so a minted id is available here and the body
                # matches the `x-trace-id` header the caller gets back. Reading only the
                # request header would have answered "" for every caller who did not send
                # one, which is most of them.
                content=ErrorBody(
                    message="That took too long to answer. Nothing was changed.",
                    trace_id=str(
                        structlog.contextvars.get_contextvars().get("trace_id")
                        or request.headers.get("x-trace-id", "")
                    ),
                ).model_dump(),
            )


#: Attached to every route so the generated schema documents one shape rather than
#: FastAPI's default per-route guess.
COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorBody,
        "description": "Absent, or present and not yours. Deliberately the same.",
    },
    409: {"model": ErrorBody, "description": "The name matched more than one thing."},
    503: {
        "model": ErrorBody,
        "description": "A source was unreachable. No stale value was substituted.",
    },
}
