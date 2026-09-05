"""Model provider API keys, which cannot be leased and must not pretend to be.

`brain.ops.secrets` refuses read-by-path on purpose: a vault that hands over a standing
credential is a vault whose credentials live as long as the caller, and `brain.ops.openbao`
refuses a static engine outright for the same reason.

**A provider API key breaks that rule and there is no way to make it not.** OpenAI,
Anthropic and Moonshot issue a key that is valid until somebody revokes it in a dashboard.
There is no engine that mints a fresh one per request, so there is nothing to lease and
nothing to hand back. Pretending otherwise - wrapping a static value in a `Lease` with an
invented expiry - would be worse than this module: the caller would believe the credential
stops working at a time nothing enforces.

So this is a separate type, deliberately not implementing `Vault`, with the difference in
the name. Somebody reaching for `borrow()` and finding it does not work here should have to
notice why.

**What keeps it from becoming a general read-by-path.** The refusal lives on
`OpenBaoVault.read_static_kv`, not here, and that placement is the point: a guard on the
caller is a guard somebody bypasses by calling the other thing. Any path outside
`providers/` is refused by the object that would otherwise do the reading. Without it,
`read_static_kv("connectors/creds/xero")` works perfectly, hands out a standing connector
credential with nothing to revoke and no record of which run held it, and nobody sees the
difference until an audit asks.

**The key is read once and never travels.** It goes straight into the process environment
where the provider SDK finds it, which is where `brain.models.adapter` already argues it
should live: a key in the environment cannot be in a request object, cannot be in a trace,
and cannot be in an exception the adapter builds. This module therefore never returns the
value to application code at all - `load_into_environment` sets it and returns only the
names it set.

**Rotation is a restart.** A key changed in the vault does not reach a running process, and
this does not poll for one. Polling would mean holding a re-read loop over a value that must
not be logged, for a change that happens perhaps twice a year; a restart is cheap here and
the deploy path already does one in three minutes.

Task ids: M5.1.2
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from brain.ops.openbao import STATIC_PREFIX, OpenBaoVault, assert_static_path
from brain.ops.secrets import SecretsUnavailableError

__all__ = [
    "PROVIDER_SLOTS",
    "STATIC_PREFIX",
    "ProviderSlot",
    "assert_static_path",
    "load_into_environment",
    "read_static",
]

#: What a provider slug may look like. Interpolated into a vault path and used to build an
#: environment variable name, so it is validated rather than trusted.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


@dataclass(frozen=True)
class ProviderSlot:
    """One provider's key: where it lives and what the SDK expects it to be called.

    The environment variable name is declared rather than derived. Deriving it - upper-case
    the slug and append `_API_KEY` - is right for three providers and wrong for the fourth,
    and being wrong means the SDK silently falls back to an unauthenticated call or to a key
    left over from something else.
    """

    slug: str
    env_var: str
    description: str = ""

    def __post_init__(self) -> None:
        if not SLUG_RE.match(self.slug):
            msg = f"{self.slug!r} is not a provider slug"
            raise ValueError(msg)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", self.env_var):
            msg = f"{self.env_var!r} is not an environment variable name"
            raise ValueError(msg)

    @property
    def path(self) -> str:
        return f"{STATIC_PREFIX}{self.slug}"


#: Every provider this system can route to. Closed, and a member is added deliberately: a
#: provider that can be configured but was never argued about is a provider whose key
#: nobody decided to trust.
PROVIDER_SLOTS: tuple[ProviderSlot, ...] = (
    ProviderSlot(
        slug="anthropic",
        env_var="ANTHROPIC_API_KEY",
        description="Claude, the default reasoner",
    ),
    ProviderSlot(
        slug="openai",
        env_var="OPENAI_API_KEY",
        description="Embeddings, and a fallback for completion",
    ),
    ProviderSlot(
        slug="moonshot",
        env_var="MOONSHOT_API_KEY",
        description="The cheaper reasoner; the v1 system routes to it by default",
    ),
)


def read_static(vault: OpenBaoVault, path: str) -> str:
    """One value out of the vault's kv engine. Never returned to application code.

    Public only so `load_into_environment` can be read as two steps rather than one long
    one. A caller that wants a provider key wants the environment variable set, not the
    string; anything holding the string is a place the string can be logged from.
    """
    # The prefix refusal and the kv v2 path rewrite both live on the vault, because a guard
    # on the caller is a guard somebody bypasses by calling the other thing.
    data = vault.read_static_kv(path)
    if not data:
        msg = f"no value in the vault at {path!r}; the slot exists and is empty"
        raise SecretsUnavailableError(msg)

    for key in ("api_key", "key", "value", "token"):
        if key in data:
            value = str(data[key])
            if not value.strip():
                msg = f"the slot at {path!r} holds an empty value"
                raise SecretsUnavailableError(msg)
            return value
    msg = f"the slot at {path!r} holds fields this does not recognise: {sorted(data)[:5]}"
    raise SecretsUnavailableError(msg)


def load_into_environment(
    vault: OpenBaoVault,
    slots: tuple[ProviderSlot, ...] = PROVIDER_SLOTS,
    *,
    environ: dict[str, str] | None = None,
    required: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Set each provider's environment variable from the vault. Returns the names set.

    **Returns names, never values.** A function that handed back the keys would be a
    function whose return value must not be logged, and every caller would have to know
    that. Returning the names means the result is safe to print, which is what makes the
    startup log line "loaded ANTHROPIC_API_KEY, OPENAI_API_KEY" possible at all.

    **A missing slot is skipped unless it is required.** Before go-live every slot is empty
    by design, and refusing to start would mean the system cannot run at all until every
    provider is configured - including the ones this deployment does not use. `required`
    names the ones this deployment genuinely cannot work without.

    **An existing value is not overwritten.** A developer running with a key in their shell
    means it; and on the server there is nothing in the environment to collide with, because
    the compose file deliberately sets none of these.
    """
    env = environ if environ is not None else os.environ
    loaded: list[str] = []
    missing: list[str] = []

    for slot in slots:
        if env.get(slot.env_var):
            # Already set, and deliberately left alone. Overwriting would make a developer's
            # explicit local key silently ineffective, which is a confusing hour.
            loaded.append(slot.env_var)
            continue
        try:
            env[slot.env_var] = read_static(vault, slot.path)
        except SecretsUnavailableError:
            # Not re-raised, and the message is not logged here: it names the path, and a
            # path names which provider is unconfigured, which is fine - but the caller
            # decides what to say. What matters is that a missing key is not a crash.
            missing.append(slot.slug)
            continue
        loaded.append(slot.env_var)

    unmet = sorted(set(required) & set(missing))
    if unmet:
        msg = (
            f"no key in the vault for {', '.join(unmet)}, and this deployment requires them. "
            "The slot exists and is empty; put a key in it rather than setting an "
            "environment variable, or the vault stops being the record of what is configured."
        )
        raise SecretsUnavailableError(msg)
    return tuple(loaded)
