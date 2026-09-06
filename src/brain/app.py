"""The FastAPI application.

Liveness and readiness are separate endpoints and the distinction is load-bearing.
Liveness answers "is this process alive"; readiness answers "can this process answer a
question correctly". A container that is up but cannot reach the database, the cache or
the secret store must fail readiness, because a half-connected instance does not refuse —
it answers from whatever it can still reach, which is how a permission-aware system
quietly starts returning wrong answers.

Task ids: M31.1.1.1, M31.1.1.2, M31.1.1.4, M31.1.1.5
Task ids: M31.1.3.1, M31.1.3.2, M31.1.3.3, M31.1.3.4, M31.1.3.5
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

from brain.api import ErrorBody, TimeoutMiddleware
from brain.api_routes import router as api_router
from brain.classification_routes import router as classification_router
from brain.core.errors import BrainError, Outcome, to_public
from brain.docs_routes import router as docs_router
from brain.identity.bearer import log_refusal, refusal_headers
from brain.identity.oidc import SIGN_IN_PROMPT, TokenRefusedError
from brain.migrate import run_migrations
from brain.ops.wiring import DEFAULT_PROFILE
from brain.routing_routes import router as routing_router
from brain.session import (
    check_reachable,
    dispose,
    make_app_engine,
    make_session_factory,
)
from brain.tools.startup import build_registry

log = structlog.get_logger()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BRAIN_", extra="ignore")

    env: Literal["development", "staging", "production"] = "development"
    #: Read from the environment, and corrected from the image's own manifest when the
    #: environment lies. See `resolved_commit`.
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
    #: Which set of wave-2 components this install runs. See `brain.ops.wiring`.
    #:
    #: The default comes from `wiring.DEFAULT_PROFILE` rather than being spelled again
    #: here. It was written in both places for about ten minutes, which is exactly long
    #: enough for a mutation test to show that changing one of them changed nothing
    #: observable.
    profile: Literal["lite", "standard", "full"] = DEFAULT_PROFILE
    #: Where spans go. Read here so that `brain.config.check` can refuse them being set on
    #: a profile that runs no trace ledger; see `wiring.trace_config_conflicts`.
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    #: Where text goes to have a model read it. Read here for the same reason the three
    #: above are, and with a heavier consequence: `brain.config.check` refuses it being set
    #: on a profile that deploys no inference server, because a span is metadata about a
    #: request and this address receives the text of the document itself. See
    #: `brain.ops.inference.inference_config_conflicts`.
    inference_url: str = ""
    #: Which system the built-in row tools read from, and therefore the first half of every
    #: tool name in the catalogue. `RowTool` refuses an empty source, because two systems'
    #: record ids collide by coincidence of integers, so this carries a real default rather
    #: than an empty string that would fail startup on a fresh install.
    tool_source: str = "local"
    #: The console and the widget only. Not a wildcard, in any environment.
    cors_origins: tuple[str, ...] = ()
    #: Off in tests, on everywhere else. A deployment that wants migrations applied
    #: by hand sets this false and runs `alembic upgrade head` itself.
    run_migrations: bool = True
    request_timeout_seconds: float = 30.0

    def resolved_commit(self) -> str:
        """Which commit this process is running, believing the image over the environment.

        The environment is not trustworthy for this, measured rather than assumed. On the
        live server the image carries `BRAIN_COMMIT_SHA=724cf3f` and the container was
        created with `BRAIN_COMMIT_SHA=unknown`: Coolify stores its own copy of the compose
        file, that copy resolved a `${COMMIT_SHA:-unknown}` default to the literal string
        at save time, and an explicit environment entry in compose beats an image's ENV.
        So `/health/live` answered "unknown" while the status page beside it reported the
        truth, and a deployment nobody could identify is a deployment nobody can roll back
        with confidence.

        Baking the value into the image was the previous fix for this and it was not
        enough, because compose can override anything the image sets. A file cannot be
        overridden by an environment variable, so the manifest wins: it is written by CI
        into the image immediately before the build, from the same git repository that
        produced the code.

        The environment is still preferred when it says something, because a developer
        running `BRAIN_COMMIT_SHA=wip` locally means it, and because the manifest is absent
        outside a built image. Only the specific value "unknown" - which is the default,
        the thing a variable says when nobody set it - falls through to the file.
        """
        if self.commit_sha and self.commit_sha != "unknown":
            return self.commit_sha
        try:
            from brain.ops.release_manifest import read_manifest

            manifest = read_manifest()
        except Exception:
            # A malformed manifest must not stop the process answering health checks. The
            # honest answer to "which commit is this" is then still "unknown", which is
            # what the caller already had.
            return self.commit_sha
        return manifest.commit[:7] if manifest else self.commit_sha


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    commit: str
    checks: dict[str, bool] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("starting", env=settings.env, commit=settings.resolved_commit())
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

    # Built and frozen here, and this is the first process that has ever built one. Every
    # rule in `brain.tools.registry` runs at registration and `freeze` runs the ones that
    # need the whole set, so a registry nobody constructs is a set of rules that has never
    # refused anything. See `brain.tools.startup`.
    #
    # It raises rather than degrading, which is the opposite of how everything else in this
    # lifespan handles a failure, and deliberately: an unreachable database leaves an
    # instance that answers what it can, whereas a catalogue that failed its own checks is
    # a set of tools nobody validated being offered to a model.
    # **This check says the catalogue is valid, and it does not say there is anything in
    # it.** Measured on production on 2026-09-06: `tools=0` in the log beside
    # `/health/ready` returning `{"tools": true}`. Both are accurate and together they read
    # as something that is not true, because this module's own docstring defines readiness
    # as "can this process answer a question correctly" and a process with no tools cannot
    # answer anything.
    #
    # The emptiness is expected and disclosed: `build_registry` is called with no
    # `records=`, because `RowSource.rows` is synchronous and this application has an
    # `AsyncEngine`. `brain.tools.startup` sets out the three ways to resolve that and why
    # each one changes the deployed connection profile and therefore deserves its own
    # measurement rather than riding along here.
    #
    # It is left reporting True rather than flipped, deliberately. Nothing asks this
    # instance a question yet, there is no route to ask through, and a readiness check that
    # fails on a known and intended state is a container restart loop rather than a signal.
    # What is refused is leaving it undocumented: the day somebody passes a row source, the
    # test named below fails and this paragraph has to be rewritten by whoever does it.
    app.state.tools = build_registry(source=settings.tool_source)
    app.state.ready["tools"] = True
    log.info("tool registry frozen", tools=len(app.state.tools))

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
    # Set to None rather than left unset, so "this process has no gate wiring" is a value a
    # route reads rather than an AttributeError it recovers from. Nothing constructs one
    # today: `brain.identity.oidc.SignatureVerifier` is an injected callback because the
    # standard library cannot verify RS256 and this repository has added no cryptography
    # dependency, so there is no verifier to put in a `TokenAuthority` and no
    # `EntitlementStore` implementation to put beside it. Every route under `API_PREFIX`
    # therefore refuses, which is what a missing authenticator has to mean.
    app.state.gate = None

    # Registered first, which makes it innermost: Starlette inserts each new middleware at
    # the front of the stack, so the last one registered runs outermost. Inside `trace`
    # means the trace id is bound when a deadline fires, so the body of a timed-out response
    # carries the same id as the log line that recorded it.
    #
    # **Attached at all, which it was not until today.** `api.TimeoutMiddleware` existed,
    # was tested, and was mounted by nothing, so every request this application has ever
    # served ran without a deadline while M31.1.2.4 was closed and `request_timeout_seconds`
    # sat at 30.0 read by nobody. Behind a pooler with a fixed number of client slots, enough
    # held connections is an outage, which is the failure it was written to prevent.
    app.middleware("http")(TimeoutMiddleware(seconds=settings.request_timeout_seconds))

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

        **The body is `api.ErrorBody`, and it used not to be.** That model calls itself "the
        only error shape, documented once and returned by everything" and this handler
        returned a bare dict, so the documented shape and the real one had never met. Two
        costs, and neither was hypothetical: the trace id was reachable only by a caller who
        knew to read a response header, so "quote this and the run can be found" was untrue
        of the thing a person is actually shown; and no endpoint declared the model, so the
        generated OpenAPI described no error shape at all and the console's typed client was
        typed against nothing.

        The trace id comes from the contextvar the `trace` middleware binds before anything
        else runs, which is the same value that middleware puts in `x-trace-id`. Read rather
        than minted here, so the body and the header cannot disagree, and defaulted to empty
        rather than invented if the middleware has not run.
        """
        status = {
            Outcome.DENIED: 404,
            Outcome.ABSENT: 404,
            Outcome.UNRESOLVED: 409,
            Outcome.DEGRADED: 503,
            Outcome.FAILED: 500,
        }[exc.outcome]
        log.warning("request failed", outcome=exc.outcome, detail=exc.detail)
        bound = structlog.contextvars.get_contextvars()
        body = ErrorBody(
            message=to_public(exc),
            trace_id=str(bound.get("trace_id", "")),
        )
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.exception_handler(TokenRefusedError)
    async def handle_token_refused(request: Request, exc: TokenRefusedError) -> JSONResponse:
        """A credential that was not acceptable, in one sentence, whatever was wrong with it.

        Beside `handle_brain_error` rather than inside it, because the two answer different
        questions. That one maps an outcome of a question that was asked; this fires before
        any question exists, so there is no outcome to map and nothing about what exists to
        give away. Both return `api.ErrorBody`, so a client parses one shape.

        The reason is a closed enumeration and it goes to the log only. Telling the presenter
        that the key was unknown rather than the signature bad tells somebody forging a token
        which part to fix next, one attempt at a time. See
        `brain.identity.bearer.EVERY_REFUSAL_SAYS_THE_SAME_SENTENCE`.
        """
        log_refusal(exc, path=request.url.path)
        bound = structlog.contextvars.get_contextvars()
        body = ErrorBody(message=SIGN_IN_PROMPT, trace_id=str(bound.get("trace_id", "")))
        return JSONResponse(
            status_code=401, content=body.model_dump(), headers=dict(refusal_headers())
        )

    app.include_router(docs_router)
    # Mounted here and nowhere else. An unmounted router is the failure this repository keeps
    # finding, and the timeout middleware three paragraphs up is the most recent one.
    app.include_router(api_router)
    # The routing matrix. A second router rather than more routes on the first, because the
    # rules differ: `api_routes` answers about entities, where the name itself is enumerable,
    # and this one answers about the model chain, where it is not. Both take the same
    # `asking` dependency, which `api_routes` declares once and this imports.
    app.include_router(routing_router)
    # Field-level classification. A third router for the reason there is a second: the rules
    # differ again. This one answers about the policy over a document's columns rather than
    # about its rows, it takes no session because there is nothing stored to read, and its
    # write verb is `admin` rather than `write` because what it governs is what other people
    # may see. The same `asking` dependency, imported rather than re-declared.
    app.include_router(classification_router)

    @app.get("/health/live", response_model=Health, tags=["health"])
    async def live() -> Health:
        """The process is running. Says nothing about whether it can answer anything."""
        return Health(status="ok", commit=settings.resolved_commit())

    @app.get("/health/ready", response_model=Health, tags=["health"])
    async def ready(response: Response) -> Health:
        """Every dependency is reachable. Deployment gates on this, not on liveness."""
        checks: dict[str, bool] = dict(app.state.ready)
        ok = all(checks.values()) if checks else True
        if not ok:
            response.status_code = 503
        return Health(
            status="ok" if ok else "degraded",
            commit=settings.resolved_commit(),
            checks=checks,
        )

    return app


app = create_app()
