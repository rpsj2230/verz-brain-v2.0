"""Entry point. Sizes the process against the container, then hands over to uvicorn.

Exists because `uvicorn --workers N` needs N decided before the process starts, and the
only honest source for N is the container's own cgroup limits — which are not knowable
from a Dockerfile.

Task ids: M31.1.2.1, M31.1.2.2, M31.1.2.3
"""

from __future__ import annotations

import os

import structlog
import uvicorn

from brain.app import Settings
from brain.config import assert_valid
from brain.runtime import detect_profile

log = structlog.get_logger()


def main() -> None:
    # Before the port is bound. A container that will never work should not be in a load
    # balancer's rotation at all, so this is the one place crashing beats degrading.
    settings = Settings()
    assert_valid(
        settings.env,
        {
            "database_url": settings.database_url,
            "valkey_url": settings.valkey_url,
            "app_role_password": os.environ.get("APP_ROLE_PASSWORD", ""),
            "cors_origins": ",".join(settings.cors_origins),
        },
    )

    profile = detect_profile()
    log.info(
        "starting server",
        workers=profile.workers,
        graceful_timeout=profile.graceful_timeout,
        limit_concurrency=profile.limit_concurrency,
        reason=profile.reason,
    )
    uvicorn.run(
        "brain.app:app",
        host="0.0.0.0",  # noqa: S104 - the container is the boundary, not the interface
        port=8000,
        workers=profile.workers,
        # Uvicorn stops accepting, then waits this long for in-flight requests. Cutting
        # them instead returns 502 to whoever was mid-question on every deploy.
        timeout_graceful_shutdown=profile.graceful_timeout,
        timeout_keep_alive=profile.timeout_keep_alive,
        limit_concurrency=profile.limit_concurrency,
        access_log=False,  # structlog already emits one line per request, with the trace id
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
