"""Reading the vault's audit log, so "who read which credential" has an answer.

The application sees a lease. It cannot see a history, and it must not: a component that
could read its own access history could also decide what that history said. The vault
writes the log; this reads it.

**Every sensitive value in the log is already an HMAC, and this module must never undo
that.** OpenBao hashes paths, tokens and accessors with a key it holds, so the log answers
"was this the same identity as that one" without answering "what is the identity". Two
consequences follow, and both are enforced here rather than remembered. Nothing in
`VaultAccess` can hold a raw value, so there is nowhere to put one. And the raw line is
never retained after parsing, so a caller cannot reach past the type to the text.

**A refused request is the interesting one.** A successful credential read is routine; a
refusal is either a misconfiguration or somebody asking for something they should not have.
Both are worth surfacing, and they are the entries a summariser that only counted volume
would lose.

**Shipping these into `brain.audit.ledger` is not built here, and that is a boundary
rather than an omission.** The ledger's `AuditAction` and `SUBJECT_KINDS` are closed sets
whose members are about principals and grants; a vault read is performed by the
application's own service identity, not by a person, so putting it there would widen a
client-facing vocabulary for a fact about infrastructure. `brain.ops.deployments` met the
same boundary and made the same choice, and this module produces the parsed facts a
shipper would need whichever chain is eventually chosen.

Task ids: M31.3.2.6
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: What an OpenBao HMAC looks like in the log. Anything not matching is a value that was
#: written in the clear, which means `log_raw` was turned on somewhere.
HMAC_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")

#: Paths worth reporting on. A vault serves its own health and status endpoints constantly
#: and those entries are noise: a log that is 99% health checks is a log nobody reads.
CREDENTIAL_PATH_RE = re.compile(r"^(secret|kv|database|aws|gcp)/")


class AuditLogError(Exception):
    """Raised when the log says something that should be impossible."""


@dataclass(frozen=True)
class VaultAccess:
    """One request the vault handled.

    There is no field here that could hold a secret, a raw path or a usable token, and that
    is the mechanism rather than an oversight. A type with nowhere to put a plaintext value
    cannot leak one, and a rule saying "remember not to log the path" holds only until
    somebody is debugging at eleven at night.
    """

    at: datetime
    #: `request` or `response`. Both are logged; a request with no matching response is a
    #: call that never came back, which is its own kind of finding.
    kind: str
    operation: str
    #: The HMAC of the path, not the path. Two reads of the same secret share this value,
    #: which is what makes "how often is this being read" answerable without naming it.
    path_hmac: str
    #: The HMAC of the token accessor. Identifies *an* identity consistently without being
    #: replayable as that identity.
    accessor_hmac: str
    remote_address: str = ""
    error: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.error)


def _hmac_or_empty(value: object) -> str:
    """An HMAC, or empty. Never the value itself.

    A path that arrives unhashed means `log_raw=true` was set, or a field OpenBao does not
    hash by default. Returning empty rather than the raw text means a misconfigured vault
    produces a log this system finds useless, instead of one it happily copies plaintext
    secrets out of. Useless is the safe failure; helpful is not.
    """
    text = str(value or "")
    return text if HMAC_RE.match(text) else ""


def parse_line(line: str) -> VaultAccess | None:
    """One JSON line from the audit log, or None if it is not an entry worth keeping.

    None rather than an exception for an uninteresting line, because the log is mostly
    uninteresting: health checks, token self-lookups, and the vault's own internal calls.
    An exception is reserved for a line that is malformed, which is a different problem and
    should not be swallowed alongside the routine ones.
    """
    if not line.strip():
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = f"audit log line is not JSON: {line[:80]!r}"
        raise AuditLogError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"audit log line is not an object: {line[:80]!r}"
        raise AuditLogError(msg)

    request = raw.get("request") or {}
    if not isinstance(request, dict):
        return None
    path = str(request.get("path", ""))
    if not path or not CREDENTIAL_PATH_RE.match(path.lstrip("/")):
        # A health check or a token lookup. Skipped rather than counted: a summary in which
        # 99% of entries are the vault talking to itself hides the 1% that is not.
        return None

    try:
        at = datetime.fromisoformat(str(raw.get("time", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        msg = f"audit entry has no usable time: {raw.get('time')!r}"
        raise AuditLogError(msg) from exc
    if at.tzinfo is None:
        msg = "audit entry timestamp has no timezone"
        raise AuditLogError(msg)

    auth = raw.get("auth") or {}
    return VaultAccess(
        at=at,
        kind=str(raw.get("type", "")),
        operation=str(request.get("operation", "")),
        # The path itself is hashed by OpenBao only when configured to; the *value* fields
        # always are. Hashing here would be a second key to manage, so what is kept is the
        # HMAC when there is one and nothing when there is not.
        path_hmac=_hmac_or_empty(request.get("path_hmac") or request.get("path")),
        accessor_hmac=_hmac_or_empty(
            (auth.get("accessor") if isinstance(auth, dict) else "")
            or request.get("client_token_accessor")
        ),
        remote_address=str(request.get("remote_address", "")),
        error=str(raw.get("error", "")),
    )


def read_log(path: Path) -> Iterator[VaultAccess]:
    """Every credential access in the log, oldest first.

    Streamed rather than read whole. An audit log on a busy vault is the largest file this
    system will ever read, and the one place a `read_text()` would turn a disk problem into
    a memory problem.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            entry = parse_line(line)
            if entry is not None:
                yield entry


@dataclass(frozen=True)
class AccessSummary:
    """What the log says, in counts. Never in names.

    Counts and distinct-identity totals only. A summary listing which paths were read most
    would be a ranked list of the most valuable secrets in the system, derived from a file
    kept precisely so that list does not have to exist anywhere.
    """

    total: int = 0
    refused: int = 0
    distinct_identities: int = 0
    distinct_paths: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def refusal_rate(self) -> float:
        return round(self.refused / self.total, 4) if self.total else 0.0


def summarise(entries: Iterator[VaultAccess] | list[VaultAccess]) -> AccessSummary:
    """Roll a log up into something a person can read in one line."""
    total = refused = 0
    identities: set[str] = set()
    paths: set[str] = set()
    first: datetime | None = None
    last: datetime | None = None
    for entry in entries:
        total += 1
        if entry.refused:
            refused += 1
        if entry.accessor_hmac:
            identities.add(entry.accessor_hmac)
        if entry.path_hmac:
            paths.add(entry.path_hmac)
        first = entry.at if first is None or entry.at < first else first
        last = entry.at if last is None or entry.at > last else last
    return AccessSummary(
        total=total,
        refused=refused,
        distinct_identities=len(identities),
        distinct_paths=len(paths),
        first_at=first,
        last_at=last,
    )


def audit_is_enabled(devices: str) -> bool:
    """Whether `bao audit list` output shows a device.

    A string rather than a call, so the check can run against output captured over ssh from
    a machine that has no vault client. The failure this guards is specific and quiet:
    audit devices are a *runtime* mount in OpenBao, so recreating the container restores
    the listener and the storage and does not restore the audit device. A vault audited on
    Monday is unaudited on Tuesday with nothing having failed.
    """
    return any(
        line.strip() and not line.startswith("Path") and "/" in line.split()[0]
        for line in devices.splitlines()
    )
