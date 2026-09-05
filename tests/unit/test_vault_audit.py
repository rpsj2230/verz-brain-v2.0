"""Reading the vault's audit log. Every test is about a way it could leak or mislead.

Task ids: M31.3.2.6
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.ops.vault_audit import (
    AuditLogError,
    audit_is_enabled,
    parse_line,
    read_log,
    summarise,
)

HMAC = "hmac-sha256:" + "a" * 64
OTHER_HMAC = "hmac-sha256:" + "b" * 64


def _entry(**overrides: object) -> str:
    base: dict[str, object] = {
        "time": "2026-09-05T12:00:00Z",
        "type": "response",
        "auth": {"accessor": HMAC},
        "request": {
            "operation": "read",
            "path": "secret/data/xero",
            "path_hmac": HMAC,
            "remote_address": "172.18.0.4",
        },
    }
    base.update(overrides)
    return json.dumps(base)


# ------------------------------------------------------------- what is kept
def test_a_credential_read_is_recorded() -> None:
    """The happy path. Without it nothing else in this file is testing a parser that can
    read anything at all."""
    entry = parse_line(_entry())
    assert entry is not None
    assert entry.operation == "read"
    assert entry.accessor_hmac == HMAC
    assert not entry.refused


def test_a_refusal_is_kept_and_marked() -> None:
    """The interesting entry. A successful read is routine; a refusal is either a
    misconfiguration or somebody asking for something they should not have, and a
    summariser that only counted volume would lose both."""
    entry = parse_line(_entry(error="permission denied"))
    assert entry is not None
    assert entry.refused


def test_the_vaults_own_chatter_is_not_kept() -> None:
    """A vault serves health and token-lookup endpoints constantly. A log that is 99% the
    vault talking to itself hides the 1% that is not, and the 1% is the whole point."""
    for path in ("sys/health", "auth/token/lookup-self", "sys/leases/renew"):
        assert parse_line(_entry(request={"operation": "read", "path": path})) is None


def test_a_blank_line_is_not_an_error() -> None:
    """A log file being appended to has a trailing newline. Raising on it would make every
    read of a healthy log fail at the end."""
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_a_malformed_line_raises_rather_than_being_skipped() -> None:
    """Skipping it would make a corrupt or truncated audit log read as a quiet one, and a
    quiet audit log is exactly what somebody tampering with it would want it to look like."""
    with pytest.raises(AuditLogError):
        parse_line('{"time": "2026-09')


def test_an_entry_with_no_usable_time_raises() -> None:
    """Entries are ordered and summarised by time. One that cannot be placed would sort
    arbitrarily and quietly change the window a summary claims to cover."""
    with pytest.raises(AuditLogError, match="time"):
        parse_line(_entry(time="not a timestamp"))


def test_a_naive_timestamp_is_refused() -> None:
    """Two vaults in different timezones would disagree about the order of the same events,
    which is the one thing an audit log has to get right."""
    with pytest.raises(AuditLogError, match="timezone"):
        parse_line(_entry(time="2026-09-05T12:00:00"))


# ----------------------------------------------------- what is never kept
def test_a_path_that_was_not_hashed_is_dropped_rather_than_recorded() -> None:
    """`log_raw=true` puts real paths and real secret values in the log. This module must
    not become the thing that copies them somewhere else.

    Dropping the value makes a misconfigured vault produce a log this system finds useless.
    Useless is the safe failure; helpful is not. Deleting this test turns the parser into a
    plaintext exfiltration path that looks like a feature."""
    entry = parse_line(
        _entry(request={"operation": "read", "path": "secret/data/xero", "path_hmac": None})
    )
    assert entry is not None
    assert entry.path_hmac == ""


def test_an_accessor_that_was_not_hashed_is_dropped() -> None:
    """A raw token accessor in a log is a working credential in a file kept for years."""
    entry = parse_line(_entry(auth={"accessor": "hvs.CAESIJ-a-real-looking-token"}))
    assert entry is not None
    assert entry.accessor_hmac == ""


def test_the_type_has_nowhere_to_put_a_secret() -> None:
    """Structural, and it is the mechanism rather than a reminder. A rule saying "do not log
    the path" holds until somebody is debugging at eleven at night; a type with no field for
    it cannot be talked into carrying one.

    Mirrors the same check in the injection and capacity suites."""
    import dataclasses

    entry = parse_line(_entry())
    assert entry is not None
    names = {f.name for f in dataclasses.fields(entry)}
    for forbidden in ("path", "secret", "value", "token", "data", "raw", "line", "body"):
        assert forbidden not in names, f"VaultAccess has a {forbidden!r} field"


def test_a_summary_counts_and_never_names() -> None:
    """A summary listing the most-read paths would be a ranked list of the most valuable
    secrets in the system, derived from the file kept precisely so that list need not exist
    anywhere. Counts answer "is something odd happening"; names answer "what is worth
    stealing"."""
    import dataclasses

    summary = summarise([e for e in (parse_line(_entry()),) if e is not None])
    for field in dataclasses.fields(summary):
        assert field.type in ("int", "datetime | None"), (
            f"AccessSummary.{field.name} could hold something other than a count"
        )


# ------------------------------------------------------------- summarising
def test_a_summary_separates_identities_from_reads() -> None:
    """One identity reading a thousand times and a thousand identities reading once look
    identical on a total, and only one of them is a problem."""
    entries = [
        parse_line(_entry()),
        parse_line(_entry()),
        parse_line(_entry(auth={"accessor": OTHER_HMAC})),
    ]
    summary = summarise([e for e in entries if e is not None])
    assert summary.total == 3
    assert summary.distinct_identities == 2


def test_the_refusal_rate_is_reported(tmp_path: Path) -> None:
    """The number worth alerting on. A vault that starts refusing is either misconfigured
    or under attack, and both need somebody to look."""
    log = tmp_path / "audit.log"
    log.write_text(_entry() + "\n" + _entry(error="permission denied") + "\n", encoding="utf-8")
    summary = summarise(read_log(log))
    assert summary.total == 2
    assert summary.refused == 1
    assert summary.refusal_rate == 0.5


def test_an_empty_summary_has_a_refusal_rate_of_zero_not_a_crash() -> None:
    """Nothing read yet is the normal state before go-live. Dividing by it would make the
    first health check of a new vault an exception."""
    assert summarise([]).refusal_rate == 0.0


def test_no_log_file_yet_is_not_an_error(tmp_path: Path) -> None:
    """Before the audit device is enabled there is no file. A reader that raised would make
    every scheduled check fail on a fresh install."""
    assert list(read_log(tmp_path / "nothing.log")) == []


def test_the_window_a_summary_covers_is_reported(tmp_path: Path) -> None:
    """Otherwise "twelve reads" is unreadable: twelve in an hour and twelve in a year are
    different facts and the number is the same."""
    log = tmp_path / "audit.log"
    log.write_text(
        _entry(time="2026-09-05T09:00:00Z") + "\n" + _entry(time="2026-09-05T17:00:00Z") + "\n",
        encoding="utf-8",
    )
    summary = summarise(read_log(log))
    assert summary.first_at == datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    assert summary.last_at == datetime(2026, 9, 5, 17, 0, tzinfo=UTC)


# ---------------------------------------------------- the device being on at all
def test_an_enabled_device_is_recognised() -> None:
    assert audit_is_enabled(
        "Path      Type    Description\n----      ----    -----------\nfile/     file    n/a\n"
    )


def test_no_devices_reads_as_not_enabled() -> None:
    """The failure this guards is quiet and specific: audit devices are a runtime mount in
    OpenBao, so recreating the container restores the listener and the storage and does not
    restore the audit device. A vault audited on Monday is unaudited on Tuesday with nothing
    having failed, and `bao audit list` returning nothing is the only visible sign."""
    assert not audit_is_enabled("")
    assert not audit_is_enabled("Path      Type    Description\n----      ----    -----------\n")
