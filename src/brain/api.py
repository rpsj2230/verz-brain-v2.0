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


class ErrorBody(BaseModel):
    """The only error shape. Documented once and returned by everything."""

    message: str = Field(description="Safe to show a person. Never explains a refusal.")
    trace_id: str = Field(default="", description="Quote this and the run can be found.")
    outcome: str = Field(
        default="failed", description="denied, absent, unresolved, degraded, failed"
    )


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
                content=ErrorBody(
                    message="That took too long to answer. Nothing was changed.",
                    trace_id=request.headers.get("x-trace-id", ""),
                    outcome="degraded",
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
