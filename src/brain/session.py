"""Engines and sessions.

Two engines, deliberately, and the reason is PgBouncer.

**The application engine runs through PgBouncer in transaction mode**, which hands a
different backend connection to every transaction. That is what makes a hundred
application connections survivable on a database configured for twenty — but it breaks
server-side prepared statements, because the statement is prepared on one backend and
executed on another. psycopg raises `InvalidSqlStatementName` or, worse, silently reuses a
plan built for different parameters. So `prepare_threshold=None` is not a tuning knob
here; without it the application works in development, where there is no PgBouncer, and
fails in production under load.

**The worker engine bypasses the pooler**, or uses session mode. `LISTEN`/`NOTIFY` binds
to a backend connection and transaction pooling moves it; a listener behind a transaction
pooler simply stops receiving notifications, with no error anywhere.

Task ids: M0.3.4, M0.3.5, M31.2.1.2, M31.2.1.3, M31.2.1.4
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain.db import normalise_database_url

log = structlog.get_logger()


def _async_url(url: str) -> str:
    """psycopg 3 speaks both sync and async; SQLAlchemy needs the async dialect named."""
    return normalise_database_url(url)


def make_app_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """The engine every request uses. Assumes PgBouncer in transaction mode.

    `poolclass=NullPool` because PgBouncer *is* the pool. Stacking SQLAlchemy's pool on
    top of it means two pools with different ideas about connection lifetime, and the
    symptom is connections that look idle to one and busy to the other.
    """
    return create_async_engine(
        _async_url(url),
        echo=echo,
        poolclass=NullPool,
        connect_args={
            # Not optional behind a transaction pooler. See the module docstring: a
            # prepared statement created on one backend and executed on another is the
            # bug that only appears in production.
            "prepare_threshold": None,
        },
    )


def make_worker_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """The engine for background work, on a session-mode pool or direct.

    Keeps a real pool, because a worker holds long-lived connections for LISTEN/NOTIFY
    and re-establishing one per transaction would drop notifications between them.
    """
    return create_async_engine(
        _async_url(url),
        echo=echo,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,  # objects stay usable after commit, inside one request
        autoflush=False,  # flush where it is meant to happen, not implicitly mid-read
    )


@asynccontextmanager
async def request_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session per request, rolled back on any exception.

    Committing is the caller's job. A context manager that commits on the way out turns
    every unhandled path into a write, which is precisely wrong for a system whose default
    should be reading.
    """
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_reachable(engine: AsyncEngine) -> bool:
    """Readiness. A trivial query, not a connection test.

    Opening a connection proves the pooler is alive; PgBouncer will happily accept a
    connection it cannot fulfil. Running a statement proves there is a database behind it.
    """
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("database unreachable", error=str(exc)[:200])
        return False
    return True


async def dispose(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


def engine_kwargs_for_profile(profile: str) -> dict[str, Any]:
    """Sizing per deployment profile, kept in one place rather than in a compose file.

    The numbers are deliberately modest. The target box runs about thirty containers on
    twelve gigabytes, and a connection pool sized for a machine we do not have is how a
    shared host falls over.
    """
    return {
        "lite": {"pool_size": 5, "max_overflow": 5},
        "standard": {"pool_size": 10, "max_overflow": 10},
        "full": {"pool_size": 20, "max_overflow": 20},
    }.get(profile, {"pool_size": 5, "max_overflow": 5})
