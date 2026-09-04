# Multi-stage. The builder carries uv and the toolchain; the runtime carries neither.
# Task ids: M0.4, M38.1.2

FROM ghcr.io/astral-sh/uv:0.12.9-python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, as their own layer: application code changes on every commit,
# the lock file does not, so this layer survives most rebuilds.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.13-slim-bookworm AS runtime

# The image runs as a non-root user with no shell. A container that cannot open a shell
# is one fewer thing to reason about if an injected instruction ever reaches a tool call.
RUN groupadd --system --gid 1001 brain \
 && useradd --system --uid 1001 --gid brain --shell /usr/sbin/nologin --no-create-home brain

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=builder --chown=brain:brain /app/.venv /app/.venv
COPY --from=builder --chown=brain:brain /app/src /app/src

USER brain
EXPOSE 8000

# Readiness, not liveness. Coolify must not route traffic to a container that is up but
# cannot reach the database, the cache or the secret store — a half-connected instance
# answers questions wrongly rather than not at all.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status==200 else 1)"

CMD ["uvicorn", "brain.app:app", "--host", "0.0.0.0", "--port", "8000"]
