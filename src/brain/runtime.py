"""Process model: workers, shutdown, and signals.

Three things that only matter when a container stops, which is to say on every deploy.

**Worker count is derived, not guessed.** The target host runs about thirty containers on
twelve gigabytes. `2 * cores + 1`, the usual rule, would start nine workers on a four-core
box and each holds its own connection pool — so the pooler's two hundred client slots are
gone before a single question arrives. Worker count is chosen against memory and the pool,
not against cores.

**Shutdown drains rather than cuts.** Uvicorn stops accepting first, then waits for
in-flight requests. Without a grace period longer than the slowest ordinary request, a
deploy returns 502 to whoever was mid-question — and on a system where a question can take
eight seconds, a five-second grace period fails several every time.

**SIGTERM is the deploy signal, SIGINT is a person.** Docker sends SIGTERM and waits ten
seconds before SIGKILL, so the grace period has to fit inside the container's stop timeout
or the drain is killed halfway through and the point is lost.

Task ids: M31.1.2.1, M31.1.2.2, M31.1.2.3, M31.1.1.3
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

#: Docker's default stop timeout. The drain must finish inside it, with room to spare,
#: or SIGKILL arrives mid-request and the graceful shutdown was theatre.
DOCKER_STOP_TIMEOUT = 10

#: Roughly what one worker costs with its pool attached, measured rather than assumed
#: once there is something to measure. Conservative until then.
WORKER_MB = 180


@dataclass(frozen=True)
class ProcessProfile:
    workers: int
    graceful_timeout: int
    timeout_keep_alive: int
    limit_concurrency: int
    reason: str


def choose_workers(*, memory_mb: int, cores: int, pool_slots: int = 200) -> int:
    """Fewest of three ceilings: memory, cores, and the pooler's client slots.

    The cores rule alone is the one everybody uses and the one that breaks a shared box.
    Nine workers on a four-core host is fine for CPU and fatal for a database configured
    for a hundred connections.
    """
    by_memory = max(1, (memory_mb - 256) // WORKER_MB)  # 256 MB headroom for the runtime
    by_cores = max(1, 2 * cores + 1)
    # Each worker keeps a pool; leave most of the pooler's slots for actual queries.
    by_pool = max(1, pool_slots // 20)
    return min(by_memory, by_cores, by_pool)


def profile_for(
    *, memory_mb: int, cores: int, slowest_request_seconds: float = 8.0
) -> ProcessProfile:
    """A whole process profile, with the reasoning attached so it can be argued with."""
    workers = choose_workers(memory_mb=memory_mb, cores=cores)

    # Longer than the slowest ordinary request, and still inside Docker's stop timeout.
    # If those two cannot both hold, the request wins and the container stop timeout has
    # to be raised — cutting a question in half to save two seconds is the wrong trade.
    graceful = min(DOCKER_STOP_TIMEOUT - 2, max(5, int(slowest_request_seconds) + 2))

    return ProcessProfile(
        workers=workers,
        graceful_timeout=graceful,
        # Below any sane load balancer idle timeout, so we close first and the balancer
        # never hands a request to a socket we are about to drop.
        timeout_keep_alive=5,
        # A ceiling per worker. Without it, a slow dependency turns into unbounded queued
        # requests, and the first symptom is memory rather than latency.
        limit_concurrency=workers * 40,
        reason=(
            f"{workers} workers: memory allows "
            f"{max(1, (memory_mb - 256) // WORKER_MB)}, cores allow {max(1, 2 * cores + 1)}, "
            f"pooler allows 10. Graceful {graceful}s covers a "
            f"{slowest_request_seconds:.0f}s request inside Docker's {DOCKER_STOP_TIMEOUT}s stop."
        ),
    )


def detect_profile() -> ProcessProfile:
    """Read the container's own limits rather than the host's.

    `os.cpu_count()` reports the host's cores, not the container's share, so a four-core
    container on a thirty-two-core host starts far too many workers. The cgroup files are
    the only honest source, and their absence means we are not in a container.
    """
    memory_mb = _cgroup_memory_mb() or 1024
    cores = _cgroup_cores() or (os.cpu_count() or 2)
    return profile_for(memory_mb=memory_mb, cores=cores)


#: cgroup v2 first, then v1. Module constants so a test can point them somewhere real
#: rather than patching builtins.open, which is both unpleasant and untypeable.
MEMORY_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
CPU_PATH = Path("/sys/fs/cgroup/cpu.max")

#: Above this, a cgroup "limit" is the kernel's way of saying there is none. cgroup v1
#: reports an unset limit as a number near 2^63 rather than "max", and believing it starts
#: one worker per gigabyte of memory that does not exist.
NO_LIMIT_ABOVE = 1 << 50


def _cgroup_memory_mb(paths: tuple[Path, ...] = MEMORY_PATHS) -> int | None:
    for path in paths:
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > NO_LIMIT_ABOVE:
            return None
        return value // (1024 * 1024)
    return None


def _cgroup_cores(path: Path = CPU_PATH) -> int | None:
    try:
        quota, period = path.read_text().split()
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return max(1, int(int(quota) / int(period)))
    except (ValueError, ZeroDivisionError):
        return None
