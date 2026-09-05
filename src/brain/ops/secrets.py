"""Borrowing a credential for one run, and giving it back whether or not the run worked.

Connector credentials are not configuration. A key pasted into an environment variable is a
key that lives as long as the container, appears in every process listing that dumps the
environment, and survives in whatever the deployment tool stored it in. So nothing here
holds a credential: it holds a *reference*, and exchanges that reference for a short-lived
lease at the moment of use.

Three properties do the work, and each one exists because of a specific way keys leak.

**A secret never reaches a string.** `Lease.__repr__` and `__str__` are overridden, because
the single most common way a key reaches a log is an exception handler that formats the
object it was holding. A dataclass's default repr prints every field.

**A lease is given back even when the run fails.** Especially when the run fails: an
exception is exactly the path nobody tests, and a leaked lease outlives the run that needed
it. `LeaseGuard` revokes in a `finally`, and revokes even when revocation itself is the
thing that failed, so one bad revocation cannot strand the rest.

**An expired lease refuses to be read at all.** Not "the source will reject it": the source
might not, if its clock differs or the revocation has not propagated, and the failure would
then be a credential working after we believed it withdrawn.

Task ids: M31.3.2.3, M31.3.2.4
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

#: How long a lease runs by default. One answer-lane request is seconds; a task-lane run is
#: minutes. Fifteen minutes covers both with room, and is short enough that a lease which
#: escaped revocation is worthless before anyone could find it.
DEFAULT_LEASE = timedelta(minutes=15)

#: Nothing may ask for longer than this. A ceiling an operator can raise is one that gets
#: raised during the incident that made it inconvenient, which is the same argument the
#: break-glass window carries.
MAX_LEASE = timedelta(hours=1)


class VaultRole(enum.StrEnum):
    """Who is asking. One policy per role, and the roles are compiled in rather than named
    by a caller: a caller who can name its own role can name a wider one."""

    APPLICATION = "application"
    WORKER = "worker"
    BROWSER_RUNNER = "browser_runner"


class SecretsUnavailableError(Exception):
    """The vault could not issue. Deliberately not a credential-shaped fallback.

    There is no path here that returns an empty or default credential. A caller handed one
    would send it to the source, get a permission error, and report a permission problem
    for what is actually an outage in the vault.
    """


@dataclass(frozen=True)
class SecretRef:
    """A pointer to a credential, safe to store in configuration and in a database row.

    This is what a connector row holds. It names where the credential lives and which role
    may borrow it, and it is useless to anybody who cannot already reach the vault.
    """

    path: str
    role: VaultRole

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("a secret reference needs a path")
        # A reference that looks like a value is how a real credential ends up pasted into
        # the field meant to point at one.
        if len(self.path) > 200 or "\n" in self.path:
            raise ValueError(f"{self.path[:40]!r} does not look like a vault path")


@dataclass(frozen=True)
class Lease:
    """A borrowed credential with an expiry.

    `secret` is the only field that ever holds a real value, and it is the reason this class
    overrides both string methods rather than relying on nobody printing it.
    """

    lease_id: str
    ref: SecretRef
    secret: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at <= self.issued_at:
            raise ValueError("a lease that expires before it is issued is not a lease")
        if self.expires_at - self.issued_at > MAX_LEASE:
            raise ValueError(f"a lease may run at most {MAX_LEASE}")

    def __repr__(self) -> str:
        """Never the secret. A dataclass repr prints every field, and the commonest way a
        key reaches a log is an exception handler formatting the object it was holding."""
        return (
            f"Lease(lease_id={self.lease_id!r}, path={self.ref.path!r}, "
            f"expires_at={self.expires_at!r})"
        )

    __str__ = __repr__

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at

    def reveal(self, now: datetime) -> str:
        """The one way to read the secret, and it checks the clock first.

        Not a property, on purpose: `lease.secret` reads like an attribute access and gets
        written without thought, while `reveal(now)` cannot be written without deciding what
        time it is. The check is here rather than left to the source, because the source
        might accept an expired credential if its clock differs or the revocation has not
        propagated, and a credential working after we believed it withdrawn is the failure
        this whole module exists to prevent.
        """
        if not self.is_live(now):
            raise SecretsUnavailableError(
                f"lease {self.lease_id} expired at {self.expires_at.isoformat()}"
            )
        return self.secret


class Vault(Protocol):
    """What a secrets backend must do. Narrow on purpose: two verbs and no read-by-path.

    There is deliberately no `get(path)`. A vault that can hand over a standing credential
    is a vault whose credentials are as long-lived as the caller that asked, and the whole
    point of leasing is that they are not.
    """

    def issue(self, ref: SecretRef, ttl: timedelta) -> Lease: ...
    def revoke(self, lease_id: str) -> None: ...


@contextmanager
def borrow(
    vault: Vault,
    ref: SecretRef,
    *,
    now: datetime,
    ttl: timedelta = DEFAULT_LEASE,
) -> Iterator[Lease]:
    """Take a lease for the duration of a block, and give it back on the way out.

    Revocation is in a `finally`, so it happens on the exception path too. That path is the
    one nobody tests and the one where a leaked lease matters most, because a run that
    crashed halfway is exactly the run somebody re-runs while the first lease is still live.
    """
    if ttl > MAX_LEASE:
        raise ValueError(f"a lease may run at most {MAX_LEASE}, not {ttl}")

    try:
        lease = vault.issue(ref, ttl)
    except SecretsUnavailableError:
        raise
    except Exception as exc:
        # Whatever the backend raises becomes one typed failure. A vault client's own
        # exception carries its request, and its request carries the path and sometimes the
        # token, straight into whatever logs the traceback.
        raise SecretsUnavailableError(f"could not issue a lease for {ref.path}") from exc

    try:
        yield lease
    finally:
        _revoke_quietly(vault, lease)


def _revoke_quietly(vault: Vault, lease: Lease) -> None:
    """Revoke, and never raise out of the cleanup path.

    A revocation that throws would replace the run's real exception with its own, and the
    real one is the one somebody needs. The lease has a short expiry precisely so that a
    failed revocation is bounded rather than permanent.
    """
    try:
        vault.revoke(lease.lease_id)
    except Exception:
        return


def revoke_all(vault: Vault, leases: list[Lease]) -> list[str]:
    """Give back every lease, and return the ids that would not revoke.

    One at a time and each in its own try, so a single stubborn lease cannot strand the
    rest. Returning the failures rather than raising: the caller is usually shutting down,
    and something that raises during shutdown loses the list of what it had not finished.
    """
    stuck: list[str] = []
    for lease in leases:
        try:
            vault.revoke(lease.lease_id)
        except Exception:
            stuck.append(lease.lease_id)
    return stuck
