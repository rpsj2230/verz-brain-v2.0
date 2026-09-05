"""Issuing and revoking a lease against OpenBao. Every test is a way a credential outlives
the run that borrowed it, or a way a secret reaches somewhere it should not.

No real vault is contacted. The HTTP call is replaced at the one seam that makes it, so
these test what this module does with an answer rather than testing that OpenBao works.

Task ids: M31.3.2.3
"""

from __future__ import annotations

from datetime import timedelta
from email.message import Message
from typing import Any

import pytest

from brain.ops.openbao import OpenBaoVault
from brain.ops.secrets import SecretRef, SecretsUnavailableError, VaultRole, borrow

REF = SecretRef(path="database/creds/xero-reader", role=VaultRole.APPLICATION)


class FakeVault(OpenBaoVault):
    """The real class with its one network call replaced.

    A subclass rather than an assignment to `_call` on an instance: the override is then a
    normal method with a normal signature, so a change to the real one's arguments breaks
    this at type-check time instead of at run time in one test.
    """

    def __init__(
        self,
        responses: list[dict[str, Any]] | dict[str, Any] | None = None,
        *,
        fail: Exception | None = None,
    ) -> None:
        super().__init__("http://vault:8200", "a-token")
        self.calls: list[tuple[str, str]] = []
        self._queue = list(responses) if isinstance(responses, list) else [responses or {}]
        self._fail = fail

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path))
        if self._fail is not None:
            raise self._fail
        return self._queue.pop(0) if self._queue else {}


def _good_response(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "lease_id": "database/creds/xero-reader/abc123",
        "lease_duration": 3600,
        "data": {"username": "v-token-xero-9f", "password": "A1-secret-value"},
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------- construction
def test_a_vault_with_no_token_is_refused_at_construction() -> None:
    """An empty token produces a 403 on the first credential request, which reads as a
    policy problem and sends somebody to look at the wrong file. Refusing here says what is
    actually wrong."""
    with pytest.raises(ValueError, match="no vault token"):
        OpenBaoVault("http://vault:8200", "")


def test_an_address_that_is_not_a_url_is_refused() -> None:
    with pytest.raises(ValueError, match="not a URL"):
        OpenBaoVault("vault:8200", "a-token")


def test_the_token_never_appears_in_the_repr() -> None:
    """The commonest way a credential reaches a log is an exception handler formatting the
    object that was holding it. `Lease` overrides its repr for the same reason; this is the
    other object in the path that holds one."""
    text = repr(OpenBaoVault("http://vault:8200", "s.SUPERSECRETTOKEN"))
    assert "SUPERSECRET" not in text
    assert "vault:8200" in text


# --------------------------------------------------------------------- issuing
def test_a_lease_carries_the_credential_and_an_expiry() -> None:
    """The happy path. If this fails nothing else here is testing a vault that works."""
    v = FakeVault(_good_response())
    lease = v.issue(REF, timedelta(minutes=30))
    assert lease.lease_id == "database/creds/xero-reader/abc123"
    assert lease.expires_at - lease.issued_at == timedelta(hours=1)


def test_the_expiry_comes_from_the_vault_and_not_from_what_we_asked_for() -> None:
    """The two differ whenever the mount's maximum is shorter than the request. Taking our
    own number means the application believes it holds a working credential after the server
    has already withdrawn it, and credentials then fail while the code is certain they
    should not.

    Asked for thirty minutes, told five. Five wins."""
    v = FakeVault(_good_response(lease_duration=300))
    lease = v.issue(REF, timedelta(minutes=30))
    assert lease.expires_at - lease.issued_at == timedelta(minutes=5)


def test_a_static_engine_is_refused_before_the_call_is_made() -> None:
    """`kv` stores a value you wrote and returns it forever. A lease over that is a promise
    nothing keeps: the caller believes the credential stops working and it does not.

    Refused before the request, so a misconfigured connector cannot even read the value."""
    v = FakeVault(_good_response())
    with pytest.raises(SecretsUnavailableError, match="not a dynamic engine"):
        v.issue(SecretRef(path="secret/data/xero", role=VaultRole.APPLICATION), timedelta(hours=1))
    assert v.calls == [], "a static path was fetched before being refused"


def test_a_response_with_no_lease_id_is_refused() -> None:
    """A credential that cannot be revoked is a credential that lives until it expires on
    its own, whatever the run does. That is what a static engine looks like from here even
    when it is mounted under a dynamic-sounding path."""
    v = FakeVault(_good_response(lease_id=""))
    with pytest.raises(SecretsUnavailableError, match="no lease id"):
        v.issue(REF, timedelta(hours=1))


def test_a_response_with_no_duration_is_refused_rather_than_given_a_default() -> None:
    """An invented expiry is worse than no expiry: the caller believes the credential stops
    working at a time nothing enforces. Deleting this makes the safest-looking failure mode
    the most dangerous one."""
    v = FakeVault(_good_response(lease_duration=None))
    with pytest.raises(SecretsUnavailableError, match="refusing to invent"):
        v.issue(REF, timedelta(hours=1))


@pytest.mark.parametrize("duration", [0, -1])
def test_a_duration_of_zero_or_less_is_refused_as_a_secrets_problem(duration: int) -> None:
    """Not left to blow up further in. Without this guard a zero reaches `Lease`, which
    refuses it with a `ValueError` - a real error, but the wrong type: every caller here
    catches `SecretsUnavailableError`, so the one that does not is the one that crashes the
    request instead of degrading it.

    Found by mutation. The `is not an int` half of the check was tested and the `<= 0` half
    was not, and the two fail differently."""
    v = FakeVault(_good_response(lease_duration=duration))
    with pytest.raises(SecretsUnavailableError, match="refusing to invent"):
        v.issue(REF, timedelta(hours=1))


def test_a_username_and_password_pair_becomes_one_secret() -> None:
    """`Lease.secret` is one string. Widening it to a mapping would mean every caller
    decides which field is the secret, and the one that picks wrong picks the username -
    which is not secret, and logs it."""
    v = FakeVault(_good_response())
    lease = v.issue(REF, timedelta(hours=1))
    from datetime import UTC, datetime

    assert lease.reveal(datetime.now(UTC)) == "v-token-xero-9f:A1-secret-value"


def test_fields_this_does_not_recognise_are_refused() -> None:
    """Guessing which field is the credential is how the wrong string gets sent to a source
    as a password, producing an authentication error that looks like a permission problem."""
    v = FakeVault(_good_response(data={"something_else": "x"}))
    with pytest.raises(SecretsUnavailableError, match="does not recognise"):
        v.issue(REF, timedelta(hours=1))


# ---------------------------------------------------------------- what leaks
def test_an_http_error_never_quotes_the_path_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenBao's error bodies quote the path, and a path names which credential was being
    borrowed. That is exactly the fact the audit log takes care to hash, so repeating it in
    an exception message - which ends up in a log, a trace and possibly a response - would
    undo that at the first failure."""
    import urllib.error

    def boom(*_a: object, **_k: object) -> None:
        # Built the way urllib really builds one, with the URL in it, because that URL is
        # precisely what must not survive into the message.
        raise urllib.error.HTTPError(
            "http://vault:8200/v1/database/creds/xero-reader", 403, "Forbidden", Message(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)
    v = OpenBaoVault("http://vault:8200", "a-token")
    with pytest.raises(SecretsUnavailableError) as caught:
        v.issue(REF, timedelta(hours=1))

    message = str(caught.value)
    assert "xero-reader" not in message
    assert "creds" not in message
    assert "database" in message, "the engine is named, so the message is still useful"


# --------------------------------------------------------------- revoking
def test_a_revocation_is_retried() -> None:
    """A failed revocation leaves a live credential. One network blip must not turn into a
    key that outlives the run that borrowed it."""

    class Flaky(FakeVault):
        """Fails twice, then works. The shape of a real network blip."""

        def _call(
            self, method: str, path: str, body: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            self.calls.append((method, path))
            if len(self.calls) < 3:
                raise SecretsUnavailableError("blip")
            return {}

    v = Flaky()
    v.revoke("some/lease/id")
    assert len(v.calls) == 3


def test_a_revocation_that_never_succeeds_raises() -> None:
    """Swallowing it would leave a live credential and a clean log, which is the pairing
    that makes this class of bug survive for months."""
    v = FakeVault(fail=SecretsUnavailableError("gone"))
    with pytest.raises(SecretsUnavailableError):
        v.revoke("some/lease/id")


def test_issuing_is_not_retried() -> None:
    """Deliberately different from revoking. A credential request that failed may still have
    minted something on the far side, and the id needed to revoke it came back only on the
    response that never arrived - so a retry can leave orphaned credentials nobody will ever
    give back."""
    v = FakeVault(fail=SecretsUnavailableError("blip"))
    with pytest.raises(SecretsUnavailableError):
        v.issue(REF, timedelta(hours=1))
    assert len(v.calls) == 1


def test_borrowing_revokes_even_when_the_body_raises() -> None:
    """The property the whole module exists for, exercised through the real context manager
    rather than asserted about it. A credential that outlives its run is a credential nobody
    is watching."""
    from datetime import UTC, datetime

    v = FakeVault([_good_response(), {}])
    with pytest.raises(RuntimeError), borrow(v, REF, now=datetime.now(UTC), ttl=timedelta(hours=1)):
        raise RuntimeError("the work failed")
    methods = [m for m, _ in v.calls]
    assert methods == ["GET", "PUT"], v.calls
