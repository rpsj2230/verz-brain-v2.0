"""An OpenBao-backed vault: the thing that actually mints a lease.

`brain.ops.secrets` defines what a vault must do and what a lease is. This is the one
implementation that talks to a real server, and everything about it is shaped by one rule
from that module: **there is no read-by-path**. A vault that can hand over a standing
credential is a vault whose credentials live as long as the caller, and leasing exists so
they do not.

**Only dynamic engines, and that is a refusal rather than a limitation.** OpenBao's `kv`
engine stores a value you wrote and returns it forever; its `database` and cloud engines
mint a fresh credential per request with the server's own expiry attached. Asking `kv` for
a lease produces one with no `lease_id` and no TTL, which this treats as an error rather
than inventing an expiry for it. An invented expiry is worse than none: the caller believes
the credential stops working and it does not.

**Two clocks, and the vault's wins.** The lease carries an expiry computed from the
server's `lease_duration`, not from the TTL we asked for. Those differ whenever the mount's
maximum is shorter than the request, and taking our own number would have the application
believing it holds a working credential after the server has already withdrawn it -
credentials failing while the code is certain they should not.

**No retries on issue.** A credential request that failed is a request that may or may not
have created a credential on the far side; retrying it can leave orphaned leases nobody
will revoke, because the lease id we would need in order to revoke them came back only on
the response we did not get. `revoke` is different and does retry, because a failed
revocation leaves a live credential and that is the failure worth being noisy about.

Task ids: M31.3.2.3
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from brain.ops.secrets import Lease, SecretRef, SecretsUnavailableError, VaultRole

#: How long to wait on the vault. Short on purpose: the vault sits on the same host on the
#: container network, so a slow answer means it is sealed, wedged or gone, and none of
#: those get better with waiting. A caller blocked here is a request the person is waiting
#: on.
TIMEOUT_SECONDS = 5.0

#: Engines that mint a fresh credential per request. Anything else stores and returns, and
#: is refused below.
DYNAMIC_MOUNTS = ("database/", "aws/", "gcp/", "azure/", "consul/")

#: The one prefix `read_static_kv` may read. Everything else in the vault is leased.
STATIC_PREFIX = "providers/"


def assert_static_path(path: str) -> None:
    """Refuse anything outside the static prefix.

    Public and separate so the refusal can be tested directly rather than only through a
    call that needs a server. `providers/anthropic` is a key nobody can lease;
    `connectors/creds/xero` is one somebody should, and reading the second one this way
    would work perfectly and be invisible.
    """
    if not path.startswith(STATIC_PREFIX):
        msg = (
            f"{path!r} is not a provider key. Everything outside {STATIC_PREFIX!r} is leased "
            "through brain.ops.secrets.borrow, which revokes it when the run ends; reading "
            "it here would hand back a standing credential nobody gives back."
        )
        raise SecretsUnavailableError(msg)


class OpenBaoVault:
    """Issues and revokes leases against a running OpenBao.

    Holds a token and never logs it. The token comes from the environment the container was
    started with, is read once here, and does not appear in `__repr__` for the same reason
    `Lease` overrides its own: the commonest way a credential reaches a log is an exception
    handler formatting the object that was holding it.
    """

    def __init__(self, address: str, token: str, *, role: VaultRole | None = None) -> None:
        if not address.startswith(("http://", "https://")):
            msg = f"vault address {address!r} is not a URL"
            raise ValueError(msg)
        if not token:
            # Refused at construction rather than at first use. An empty token produces a
            # 403 on the first credential request, which reads as a policy problem and
            # sends somebody to look at the wrong file.
            msg = "no vault token; the application cannot borrow a credential without one"
            raise ValueError(msg)
        self._address = address.rstrip("/")
        self._token = token
        self._role = role

    def __repr__(self) -> str:
        return f"OpenBaoVault(address={self._address!r}, role={self._role!r})"

    __str__ = __repr__

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._address}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310  scheme checked in __init__
        request.add_header("X-Vault-Token", self._token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The body is read but never included in the message. OpenBao's error bodies
            # quote the path that failed, and a path names which credential was being
            # borrowed - which is exactly the fact the audit log takes care to hash.
            msg = f"vault refused {method} on a {self._mount_of(path)} path: HTTP {exc.code}"
            raise SecretsUnavailableError(msg) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            msg = f"vault at {self._address} did not answer within {TIMEOUT_SECONDS}s"
            raise SecretsUnavailableError(msg) from exc

        if not raw:
            return {}
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = "vault returned something that is not JSON"
            raise SecretsUnavailableError(msg) from exc
        return parsed

    @staticmethod
    def _mount_of(path: str) -> str:
        """The engine a path lives under, for error messages. Never the full path."""
        return path.lstrip("/").split("/", 1)[0] or "unknown"

    def issue(self, ref: SecretRef, ttl: timedelta) -> Lease:
        """Mint a credential. Fails rather than inventing anything it was not told."""
        if not any(ref.path.lstrip("/").startswith(m) for m in DYNAMIC_MOUNTS):
            msg = (
                f"{self._mount_of(ref.path)} is not a dynamic engine, so it returns a stored "
                "value rather than minting one. A stored value has no expiry the vault will "
                "enforce, and a lease over it would be a promise nothing keeps."
            )
            raise SecretsUnavailableError(msg)

        issued_at = datetime.now(UTC)
        payload = self._call("GET", f"{ref.path}?ttl={int(ttl.total_seconds())}s")

        lease_id = str(payload.get("lease_id", ""))
        if not lease_id:
            msg = (
                "the vault returned a credential with no lease id, so it cannot be revoked. "
                "That is what a static engine looks like from here."
            )
            raise SecretsUnavailableError(msg)

        # The server's duration, not the one we asked for. They differ whenever the mount's
        # maximum is shorter than the request, and believing our own number means holding a
        # credential the vault has already withdrawn.
        duration = payload.get("lease_duration")
        if not isinstance(duration, int) or duration <= 0:
            msg = "the vault returned no lease duration; refusing to invent an expiry"
            raise SecretsUnavailableError(msg)

        data = payload.get("data")
        if not isinstance(data, dict):
            msg = "the vault returned no credential data"
            raise SecretsUnavailableError(msg)
        secret = self._single_value(data)

        return Lease(
            lease_id=lease_id,
            ref=ref,
            secret=secret,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=duration),
        )

    @staticmethod
    def _single_value(data: dict[str, Any]) -> str:
        """The credential out of the response body, as one string.

        A database engine returns `username` and `password` separately. They are joined
        rather than returned as a pair because `Lease.secret` is one string and widening it
        to a mapping would mean every caller decides which field is the secret - and the
        one that picks wrong picks the username, which is not secret, and logs it.
        """
        if "password" in data and "username" in data:
            return f"{data['username']}:{data['password']}"
        for key in ("password", "secret_key", "token", "value"):
            if key in data:
                return str(data[key])
        msg = f"the vault returned fields this does not recognise: {sorted(data)[:5]}"
        raise SecretsUnavailableError(msg)

    def read_static_kv(self, path: str) -> dict[str, Any]:
        """One value out of the kv engine, and only from under `STATIC_PREFIX`.

        This is read-by-path, which the `Vault` protocol deliberately does not have, so it
        lives here as a named exception with the refusal attached rather than as a general
        capability. A model provider's API key cannot be leased: OpenAI, Anthropic and
        Moonshot each issue a key that is valid until somebody revokes it in a dashboard,
        and there is no engine that mints a fresh one per request. Wrapping one in a `Lease`
        with an invented expiry would be worse than admitting it - the caller would believe
        the credential stops working at a time nothing enforces.

        **The prefix check is what stops this being the hole in the design.** Without it,
        `read_static_kv("connectors/creds/xero")` works perfectly, hands out a standing
        connector credential with nothing to revoke and no record of which run held it, and
        nothing anywhere notices. The guard is on this method rather than on the caller
        precisely because a caller can be bypassed.
        """
        assert_static_path(path)
        # kv version 2 answers on `<mount>/data/<rest>`, not on the logical path. Getting
        # this wrong returns a 404 that reads as "the slot is empty" rather than "the path
        # is wrong", and somebody then writes the key in twice.
        mount, _, rest = path.partition("/")
        payload = self._call("GET", f"{mount}/data/{rest}")
        data = payload.get("data")
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                # kv v2 nests twice: {"data": {"data": {...}, "metadata": {...}}}.
                return inner
        return data if isinstance(data, dict) else {}

    def revoke(self, lease_id: str) -> None:
        """Give the credential back. Retries, unlike `issue`.

        A failed revocation leaves a live credential, so one network blip must not turn into
        a key that outlives the run that borrowed it. `issue` deliberately does not retry:
        a request that failed may still have minted something, and the id needed to revoke
        it came back only on the response that never arrived.
        """
        last: Exception | None = None
        for _ in range(3):
            try:
                self._call("PUT", f"sys/leases/revoke/{lease_id}")
            except SecretsUnavailableError as exc:
                last = exc
                continue
            return
        if last is not None:
            raise last
