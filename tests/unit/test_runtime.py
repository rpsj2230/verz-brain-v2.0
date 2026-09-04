"""Worker count, drain timing, and reading the container's own limits.

Task ids: M31.1.1.3, M31.1.2.1, M31.1.2.2, M31.1.2.3
"""

from __future__ import annotations

from pathlib import Path

from brain.runtime import (
    DOCKER_STOP_TIMEOUT,
    ProcessProfile,
    _cgroup_cores,
    _cgroup_memory_mb,
    choose_workers,
    detect_profile,
    profile_for,
)


# ------------------------------------------------------------------ workers
def test_memory_can_be_the_binding_constraint() -> None:
    """The rule everybody uses is 2*cores+1, and on a shared box it is the wrong rule.
    A 512 MB container gets one worker however many cores the host advertises."""
    assert choose_workers(memory_mb=512, cores=16) == 1


def test_cores_can_be_the_binding_constraint() -> None:
    assert choose_workers(memory_mb=8192, cores=1) == 3


def test_the_pooler_can_be_the_binding_constraint() -> None:
    """Each worker keeps a pool. Nine workers on a four-core host is fine for CPU and
    fatal for a database configured for a hundred connections."""
    assert choose_workers(memory_mb=32768, cores=32, pool_slots=40) == 2


def test_never_fewer_than_one() -> None:
    assert choose_workers(memory_mb=64, cores=1) == 1


def test_the_real_target_box_gets_a_sane_number() -> None:
    """1 GiB limit, as set in docker-compose.yml, on a shared four-core host."""
    assert choose_workers(memory_mb=1024, cores=4) == 4


# ------------------------------------------------------------------- drain
def test_the_drain_outlasts_the_slowest_ordinary_request() -> None:
    """Without this a deploy returns 502 to whoever was mid-question, and on a system
    where a question can take eight seconds that is several people every time."""
    p = profile_for(memory_mb=1024, cores=4, slowest_request_seconds=6)
    assert p.graceful_timeout > 6


def test_the_drain_fits_inside_dockers_stop_timeout() -> None:
    """Docker sends SIGTERM then SIGKILL after ten seconds. A grace period longer than
    that is killed halfway through and the graceful shutdown was theatre."""
    p = profile_for(memory_mb=1024, cores=4, slowest_request_seconds=30)
    assert p.graceful_timeout < DOCKER_STOP_TIMEOUT


def test_keep_alive_closes_before_a_load_balancer_would() -> None:
    """We close first, so the balancer never hands a request to a socket we are about to
    drop."""
    assert profile_for(memory_mb=1024, cores=4).timeout_keep_alive <= 5


def test_concurrency_is_bounded() -> None:
    """Without a ceiling, a slow dependency becomes unbounded queued requests, and the
    first symptom is memory rather than latency."""
    p = profile_for(memory_mb=1024, cores=4)
    assert 0 < p.limit_concurrency <= p.workers * 50


def test_the_profile_carries_its_own_reasoning() -> None:
    """A number with no argument attached gets changed by whoever is annoyed by it."""
    assert "workers" in profile_for(memory_mb=1024, cores=4).reason


# ------------------------------------------------------------------ cgroup
def test_an_unlimited_cgroup_reads_as_no_limit(tmp_path: Path) -> None:
    """cgroup v1 reports an unset limit as a number near 2^63 rather than "max", and
    believing it starts one worker per gigabyte of memory that does not exist."""
    f = tmp_path / "memory.max"
    f.write_text("9223372036854771712")
    assert _cgroup_memory_mb((f,)) is None


def test_a_real_cgroup_limit_is_read(tmp_path: Path) -> None:
    f = tmp_path / "memory.max"
    f.write_text(str(1024 * 1024 * 1024))  # 1 GiB, as docker-compose.yml sets
    assert _cgroup_memory_mb((f,)) == 1024


def test_the_literal_max_reads_as_no_limit(tmp_path: Path) -> None:
    """cgroup v2 is honest about it and writes the word."""
    f = tmp_path / "memory.max"
    f.write_text("max")
    assert _cgroup_memory_mb((f,)) is None


def test_a_missing_cgroup_file_is_not_an_error(tmp_path: Path) -> None:
    """Not being in a container is normal - it is how the tests run."""
    assert _cgroup_memory_mb((tmp_path / "absent",)) is None


def test_cpu_quota_becomes_cores(tmp_path: Path) -> None:
    """os.cpu_count() reports the host's cores. A four-core share of a thirty-two-core
    host would otherwise start sixty-five workers, each with its own pool."""
    f = tmp_path / "cpu.max"
    f.write_text("400000 100000")
    assert _cgroup_cores(f) == 4


def test_an_unquotaed_cpu_reads_as_no_limit(tmp_path: Path) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("max 100000")
    assert _cgroup_cores(f) is None


def test_detect_falls_back_when_there_is_no_cgroup() -> None:
    """Not being in a container is normal - it is how the tests and local development
    run - so absence must produce a working profile rather than an exception."""
    p = detect_profile()
    assert isinstance(p, ProcessProfile)
    assert p.workers >= 1
