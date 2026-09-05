"""The FastAPI application.

Liveness and readiness are separate endpoints and the distinction is load-bearing.
Liveness answers "is this process alive"; readiness answers "can this process answer a
question correctly". A container that is up but cannot reach the database, the cache or
the secret store must fail readiness, because a half-connected instance does not refuse —
it answers from whatever it can still reach, which is how a permission-aware system
quietly starts returning wrong answers.

Task ids: M31.1.1, M31.1.2, M31.1.3
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from brain.core.errors import BrainError, Outcome, to_public
from brain.docs_routes import router as docs_router
from brain.migrate import run_migrations
from brain.session import (
    check_reachable,
    dispose,
    make_app_engine,
    make_session_factory,
)

log = structlog.get_logger()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BRAIN_", extra="ignore")

    env: Literal["development", "staging", "production"] = "development"
    commit_sha: str = "unknown"

    # DATABASE_URL and VALKEY_URL are read under their plain names as well as the
    # prefixed ones. The prefix exists so BRAIN_ENV cannot collide with anything else on
    # a shared host, but these two have universal names that every tool, compose file and
    # operator already uses — including alembic/env.py two directories away.
    #
    # Having two names for one setting is not a naming preference, it is a bug waiting to
    # happen, and it did: the deployed app read BRAIN_DATABASE_URL, found nothing, and
    # skipped migrations in silence while reporting healthy.
    database_url: str = Field(
        default="", validation_alias=AliasChoices("BRAIN_DATABASE_URL", "DATABASE_URL")
    )
    valkey_url: str = Field(
        default="", validation_alias=AliasChoices("BRAIN_VALKEY_URL", "VALKEY_URL")
    )
    #: The console and the widget only. Not a wildcard, in any environment.
    cors_origins: tuple[str, ...] = ()
    #: Off in tests, on everywhere else. A deployment that wants migrations applied
    #: by hand sets this false and runs `alembic upgrade head` itself.
    run_migrations: bool = True
    request_timeout_seconds: float = 30.0


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    commit: str
    checks: dict[str, bool] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("starting", env=settings.env, commit=settings.commit_sha)
    # Dependency handles attach here as they land: database pool, cache, secret store,
    # model registry. Readiness reads app.state.ready, so an unattached dependency shows
    # as not-ready rather than as a working instance.
    app.state.ready = {}

    if settings.run_migrations and not settings.database_url and settings.env != "development":
        # Loud on purpose. Skipping migrations because a variable was unset is exactly
        # how the deployed app came up healthy against an empty schema.
        log.error(
            "no database configured outside development; migrations skipped",
            env=settings.env,
            hint="set DATABASE_URL or BRAIN_DATABASE_URL",
        )
        app.state.ready["database_configured"] = False

    if settings.database_url and settings.run_migrations:
        # Before readiness, deliberately: the application must not answer a question
        # against a schema it does not match. Concurrency between replicas is handled by
        # an advisory lock inside run_migrations, not by hoping.
        try:
            applied = await asyncio.to_thread(run_migrations, settings.database_url)
            app.state.ready["migrations"] = True
            if applied:
                log.info("schema migrated", revisions=applied)
        except Exception:
            # Left unready rather than crashed, so the failure is visible on
            # /health/ready and in the logs instead of a container restart loop that
            # discards the traceback.
            log.exception("migrations failed")
            app.state.ready["migrations"] = False

    if settings.database_url:
        # The pool is attached after migrations, so a replica never serves against a
        # schema the migration is still changing.
        app.state.db_engine = make_app_engine(settings.database_url)
        app.state.db_sessions = make_session_factory(app.state.db_engine)
        app.state.ready["database"] = await check_reachable(app.state.db_engine)
    else:
        app.state.db_engine = None
        app.state.db_sessions = None

    try:
        yield
    finally:
        # Drain before the socket closes. Uvicorn stops accepting first, so in-flight
        # requests finish against a live pool rather than a disposed one.
        log.info("shutting down")
        await dispose(getattr(app.state, "db_engine", None))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    in_production = settings.env == "production"
    app = FastAPI(
        title="Verz Company Brain",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if in_production else "/docs",
        # Turning off `/docs` without turning off `/openapi.json` closes the reading room
        # and leaves the catalogue on the doorstep. Production served the complete schema
        # unauthenticated: fourteen paths, every operation, every response model's field
        # names, and no security scheme described. `/docs` was correctly 404 the whole
        # time, which is what made it look handled.
        #
        # Nothing sensitive was behind those paths yet, because no route is behind the gate
        # yet. That is luck rather than design: the schema is generated from whatever is
        # mounted, so the first real endpoint would have published itself.
        openapi_url=None if in_production else "/openapi.json",
        redoc_url=None if in_production else "/redoc",
    )
    app.state.settings = settings
    app.state.ready = {}

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["authorization", "content-type", "x-trace-id"],
        )

    @app.middleware("http")
    async def trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """A trace id is minted before anything else runs, including identification.

        It has to exist before we know who is asking, or a request that fails during
        identification would have no id and could not be found in the ledger afterwards.
        """
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(trace_id=trace_id, path=request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["x-trace-id"] = trace_id
        response.headers["server-timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["x-frame-options"] = "DENY"
        response.headers["referrer-policy"] = "no-referrer"
        return response

    @app.exception_handler(BrainError)
    async def handle_brain_error(_request: Request, exc: BrainError) -> JSONResponse:
        """Maps the taxonomy to a response, and is the only place an error becomes text.

        DENIED and ABSENT both leave here as 404 with the same body. A 403 on a hidden
        record would confirm the record exists, which is the leak the taxonomy exists to
        prevent.
        """
        status = {
            Outcome.DENIED: 404,
            Outcome.ABSENT: 404,
            Outcome.UNRESOLVED: 409,
            Outcome.DEGRADED: 503,
            Outcome.FAILED: 500,
        }[exc.outcome]
        log.warning("request failed", outcome=exc.outcome, detail=exc.detail)
        return JSONResponse(status_code=status, content={"message": to_public(exc)})

    app.include_router(docs_router)

    @app.get("/health/live", response_model=Health, tags=["health"])
    async def live() -> Health:
        """The process is running. Says nothing about whether it can answer anything."""
        return Health(status="ok", commit=settings.commit_sha)

    @app.get("/health/ready", response_model=Health, tags=["health"])
    async def ready(response: Response) -> Health:
        """Every dependency is reachable. Deployment gates on this, not on liveness."""
        checks: dict[str, bool] = dict(app.state.ready)
        ok = all(checks.values()) if checks else True
        if not ok:
            response.status_code = 503
        return Health(status="ok" if ok else "degraded", commit=settings.commit_sha, checks=checks)

    return app


app = create_app()
