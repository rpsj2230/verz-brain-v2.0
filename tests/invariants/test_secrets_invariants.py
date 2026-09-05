"""A credential must not outlive the run that borrowed it. A failure here blocks deploy.

Every test is about one of the three ways a key escapes: it gets printed, it gets kept, or
it keeps working after we believed it withdrawn.

Task ids: M31.3.2.3, M31.3.2.4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.ops.secrets import (
    DEFAULT_LEASE,
    MAX_LEASE,
    Lease,
    SecretRef,
    SecretsUnavailableError,
    VaultRole,
    borrow,
    revoke_all,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
REF = SecretRef(path="connectors/xero", role=VaultRole.APPLICATION)
CANARY = "sk-live-CANARY-7Q4XZ"


class FakeVault:
    def __init__(self, *, fail_issue: bool = False, fail_revoke: bool = False) -> None:
        self.issued: list[str] = []
        self.revoked: list[str] = []
        self.fail_issue = fail_issue
        self.fail_revoke = fail_revoke

    def issue(self, ref: SecretRef, ttl: timedelta) -> Lease:
        if self.fail_issue:
            raise RuntimeError(f"vault down; token=hvs.SECRET path={ref.path}")
        lease_id = f"lease-{len(self.issued)}"
        self.issued.append(lease_id)
        return Lease(
            lease_id=lease_id,
            ref=ref,
            secret=CANARY,
            issued_at=NOW,
            expires_at=NOW + ttl,
        )

    def revoke(self, lease_id: str) -> None:
        if self.fail_revoke:
            raise RuntimeError("revocation refused")
        self.revoked.append(lease_id)


# --------------------------------------------------------------- it is given back
def test_a_lease_is_revoked_when_the_run_finishes() -> None:
    """A lease nobody gives back is a standing credential with extra steps."""
    vault = FakeVault()
    with borrow(vault, REF, now=NOW) as lease:
        assert lease.reveal(NOW) == CANARY
    assert vault.revoked == vault.issued


def test_a_lease_is_revoked_when_the_run_raises() -> None:
    """The path nobody tests, and the one where it matters most: a run that crashed halfway
    is exactly the run somebody re-runs while the first lease is still live."""
    vault = FakeVault()
    with pytest.raises(ZeroDivisionError), borrow(vault, REF, now=NOW):
        raise ZeroDivisionError
    assert vault.revoked == vault.issued


def test_a_failing_revocation_does_not_replace_the_real_exception() -> None:
    """The run's own exception is the one somebody needs. A revocation that threw would
    bury it, and the short expiry is what makes a failed revocation bounded instead."""
    vault = FakeVault(fail_revoke=True)
    with pytest.raises(ValueError, match="the real problem"), borrow(vault, REF, now=NOW):
        raise ValueError("the real problem")


def test_one_stubborn_lease_does_not_strand_the_others() -> None:
    """Shutdown revokes many at once. Raising on the first failure leaves the rest live and
    loses the list of what was not finished."""
    vault = FakeVault()
    leases = [vault.issue(REF, DEFAULT_LEASE) for _ in range(3)]
    vault.fail_revoke = True
    stuck = revoke_all(vault, leases)
    assert len(stuck) == 3


# ------------------------------------------------------------ it is never printed
def test_the_secret_is_absent_from_the_repr() -> None:
    """The commonest way a key reaches a log is an exception handler formatting the object
    it was holding, and a dataclass repr prints every field."""
    vault = FakeVault()
    with borrow(vault, REF, now=NOW) as lease:
        assert CANARY not in repr(lease)
        assert CANARY not in str(lease)
        assert CANARY not in f"{lease}"


def test_the_secret_is_absent_from_an_exception_that_carried_the_lease() -> None:
    """Formatting the exception is what a log handler does, so this is the shape the leak
    actually takes rather than somebody printing the lease on purpose."""
    vault = FakeVault()
    with borrow(vault, REF, now=NOW) as lease:
        err = RuntimeError(f"failed while using {lease}")
        assert CANARY not in str(err)


def test_a_backend_failure_does_not_carry_the_backend_request_outward() -> None:
    """A vault client's own exception carries its request, and its request carries the path
    and sometimes the token, straight into whatever logs the traceback."""
    vault = FakeVault(fail_issue=True)
    with pytest.raises(SecretsUnavailableError) as exc, borrow(vault, REF, now=NOW):
        pass
    assert "hvs.SECRET" not in str(exc.value)


# --------------------------------------------------------- it stops working on time
def test_an_expired_lease_refuses_to_be_read() -> None:
    """Not "the source will reject it". The source might not, if its clock differs or the
    revocation has not propagated, and a credential working after we believed it withdrawn
    is the failure this module exists to prevent."""
    vault = FakeVault()
    lease = vault.issue(REF, DEFAULT_LEASE)
    with pytest.raises(SecretsUnavailableError, match="expired"):
        lease.reveal(NOW + DEFAULT_LEASE + timedelta(seconds=1))


def test_reading_the_secret_requires_saying_what_time_it_is() -> None:
    """A property reads like an attribute access and gets written without thought.
    `reveal(now)` cannot be written without deciding what time it is, which is the whole
    point of the check."""
    import inspect

    assert "now" in inspect.signature(Lease.reveal).parameters
    assert not isinstance(inspect.getattr_static(Lease, "reveal"), property)


def test_a_lease_longer_than_the_ceiling_is_refused() -> None:
    """A maximum a caller can exceed is a maximum that gets exceeded during the incident
    that made it inconvenient."""
    vault = FakeVault()
    with (
        pytest.raises(ValueError, match="at most"),
        borrow(vault, REF, now=NOW, ttl=MAX_LEASE + timedelta(seconds=1)),
    ):
        pass


def test_there_is_no_way_to_read_a_standing_credential() -> None:
    """The vault protocol has two verbs and no read-by-path on purpose. A vault that can
    hand over a standing credential is a vault whose credentials live as long as whoever
    asked, which is the thing leasing exists to stop."""
    from brain.ops.secrets import Vault

    assert set(dir(Vault)) & {"issue", "revoke"} == {"issue", "revoke"}
    assert "get" not in dir(Vault)


# ------------------------------------------------------------------- references
def test_a_reference_is_not_a_credential() -> None:
    """This is what a connector row holds. It names where the credential lives and which
    role may borrow it, and is useless to anybody who cannot already reach the vault."""
    assert CANARY not in repr(REF)
    assert REF.path == "connectors/xero"


def test_something_that_looks_like_a_value_is_refused_as_a_path() -> None:
    """How a real credential ends up pasted into the field meant to point at one."""
    with pytest.raises(ValueError, match="does not look like a vault path"):
        SecretRef(path="x" * 300, role=VaultRole.APPLICATION)


def test_a_caller_cannot_name_its_own_role() -> None:
    """A caller that can name its role can name a wider one. The roles are compiled in."""
    assert set(VaultRole) == {
        VaultRole.APPLICATION,
        VaultRole.WORKER,
        VaultRole.BROWSER_RUNNER,
    }


# ------------------------------------------------- rotation without redeploy (M31.3.2.5)
class RotatingVault(FakeVault):
    """A vault whose stored credential changes between calls, as rotation does."""

    def __init__(self) -> None:
        super().__init__()
        self.current = "sk-live-BEFORE"

    def issue(self, ref: SecretRef, ttl: timedelta) -> Lease:
        lease = super().issue(ref, ttl)
        return Lease(
            lease_id=lease.lease_id,
            ref=ref,
            secret=self.current,
            issued_at=NOW,
            expires_at=NOW + ttl,
        )


def test_a_rotated_credential_is_picked_up_without_a_restart() -> None:
    """M31.3.2.5. The whole reason nothing holds a credential: rotation is somebody changing
    a value in the vault, and the next run simply borrows the new one. A process that read
    its key once at startup would keep using the old one until somebody redeployed, and the
    old one is the one being rotated away from."""
    vault = RotatingVault()
    with borrow(vault, REF, now=NOW) as before:
        assert before.reveal(NOW) == "sk-live-BEFORE"

    vault.current = "sk-live-AFTER"

    with borrow(vault, REF, now=NOW) as after:
        assert after.reveal(NOW) == "sk-live-AFTER"


def test_nothing_caches_a_secret_between_borrows() -> None:
    """A cache would reintroduce exactly the lifetime that leasing removes, and it would do
    it invisibly: the code would still look like it borrowed."""
    vault = RotatingVault()
    with borrow(vault, REF, now=NOW):
        pass
    vault.current = "sk-live-AFTER"
    with borrow(vault, REF, now=NOW) as second:
        assert second.reveal(NOW) == "sk-live-AFTER"
    assert len(vault.issued) == 2, "the second borrow did not reach the vault"


def test_a_reference_survives_rotation_unchanged() -> None:
    """The reference is what configuration and the connector row hold, so rotating a
    credential must not require editing either. If it did, rotation would be a deploy."""
    vault = RotatingVault()
    with borrow(vault, REF, now=NOW):
        pass
    vault.current = "sk-live-AFTER"
    with borrow(vault, REF, now=NOW) as after:
        assert after.ref == REF
