"""What a connector is, stated before any connector exists.

The package docstring says what does not belong here. This module says what does, and every
rule in it is a refusal that fires at registration rather than at request time, for the
reason `brain.tools.registry` gives about tools: a refusal at request time is an answer
going wrong, and a refusal at registration is a build going red.

Four properties are load-bearing, and each is checked by reading a declaration rather than
by trusting a body.

**A connector fetches and does not decide.** `assert_fetches_only` refuses a fetch function
that could be handed the caller's grants. An adapter that never receives an `EntitlementSet`
cannot filter by one, so "the redactor is the only place a permission question is answered"
is a shape rather than a convention. This is the same argument, in the same form, as
`brain.core.redaction.assert_channel_adapter`: a signature is checkable, a body is not.

**A connector borrows a credential and never reads one.** `brain.ops.secrets.borrow` is a
context manager that revokes in a `finally`, and a connector is handed the `Lease` it
yields. `assert_fetches_only` therefore also refuses a `Vault` or a `SecretRef` parameter,
because either one lets an adapter mint or name credentials of its own, and
`assert_holds_no_credential` refuses a connector that keeps one between calls. Rotation then
costs nothing: see `ROTATION_NEEDS_NO_REDEPLOY`.

**Scope is fixed at connect.** One Drive folder, one Base table, one set of views. A
connector connected to everything has the source's own blast radius, and narrowing it later
does not un-fetch anything.

**Read-only unless somebody deliberately said otherwise.** `AccessMode.READ_ONLY` is the
default value of the field rather than a convention applied by whoever filled the form in,
and a write grant naming nobody is refused: who agreed to it is the only part of a write
grant that can be audited afterwards.

Scope: domain logic. Nothing here opens a connection, reads a table or calls a source. `now`
is a parameter for the reason `brain.models.routing.CircuitBreaker` gives.

Task ids: M11.1.1, M11.2.1, M11.2.3, M11.2.4, M11.2.5
"""

from __future__ import annotations

import enum
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from brain.core.envelope import Entity, IdentityMode, SideEffect, TypedResult
from brain.ops.secrets import SecretRef

# ------------------------------------------------------------------ written-down reasons
#: Why a fetch function's signature, rather than its body, is what gets checked.
A_CONNECTOR_NEVER_DECIDES = (
    "A connector returns everything it fetched, typed, and the redactor removes what is not "
    "covered. The rule is enforced on what a fetch function can be given rather than on what "
    "it does: a function never handed an EntitlementSet cannot filter by one. The "
    "alternative is auditing every connector for permission logic instead of auditing one "
    "redactor, and that audit has to be redone every time any connector is edited."
)

#: Why `Scope` is deliberately absent from `DECIDING_TYPE_NAMES`.
A_SCOPE_PREDICATE_IS_NOT_A_GRANT = (
    "A scope predicate is a row filter the gate has already computed, and handing it down "
    "for the source to apply can only narrow the result set. The caller's grants are the "
    "input to a decision, and those are what a connector must never hold. Forbidding Scope "
    "as well would forbid predicate push-down, which is how a database adapter avoids "
    "pulling a table across the wire in order to throw most of it away."
)

#: Why nothing here reloads, restarts or redeploys when a credential changes.
ROTATION_NEEDS_NO_REDEPLOY = (
    "A credential is borrowed per run and revoked in a finally, so nothing holds one between "
    "calls and there is no cached value for a rotation to invalidate. The vault mints from "
    "the same path with a new value and the next run picks it up; moving to a different path "
    "is a registry edit rather than a deploy. This is a property of never holding the "
    "credential, not a feature added on top: a connector with an api_key attribute would "
    "need a restart, and `assert_holds_no_credential` is what stops one existing."
)

#: Why a write grant must name the person who made it.
A_WRITE_GRANT_NAMES_SOMEBODY = (
    "Read-only is the default value of the field, so a connector installed by somebody in a "
    "hurry is read-only. Write is a separate deliberate grant, and it records who granted "
    "it, because the question asked after a connector wrote something unexpected is always "
    "'who agreed to this' and a boolean cannot answer it."
)


# ------------------------------------------------------------------------------ vocabulary
class TransportKind(enum.StrEnum):
    """How a connector reaches its source.

    Four, from architecture section 12, and the set is closed. All four normalise to the same
    entity-tagged typed contract, which is what makes the transport invisible above this
    layer; a fifth kind added without a normaliser would be visible everywhere at once.
    """

    MCP = "mcp"
    REST = "rest"
    DATABASE = "database"
    CUSTOM = "custom"


class DataTier(enum.StrEnum):
    """Which of the three tiers a field belongs to.

    The tier is a property of the field rather than of the connector, which is why it is an
    enum on a declaration and not a flag on a manifest. One source contributes fields to two
    tiers routinely: a ticket's status is projected and its body never is.
    """

    #: Ours. We are the source, so retention and deletion are ours to answer for.
    LOCAL = "local"
    #: A bounded copy of somebody else's, at most twelve fields per entity kind.
    PROJECTED = "projected"
    #: Fetched per request and never stored. Storing one of these would turn a system of
    #: record's data into ours, with none of its retention or deletion rules attached.
    FEDERATED = "federated"


class AccessMode(enum.StrEnum):
    """What a connector may do to its source.

    Ordered by nothing: there are two, and the default is the safe one. See
    `A_WRITE_GRANT_NAMES_SOMEBODY`.
    """

    READ_ONLY = "read_only"
    WRITE = "write"


class HealthState(enum.StrEnum):
    """What a connector's last probe found.

    `UNCONFIGURED` is separate from `DOWN` because they go to different people. A connector
    nobody finished installing is a task for whoever installed it; a connector that was
    working this morning is an incident. Collapsing the two produces a health dashboard that
    is permanently amber during rollout and therefore permanently ignored.
    """

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNCONFIGURED = "unconfigured"


class ConnectorContractError(Exception):
    """A connector was declared in a shape the platform cannot hold.

    Outside the user-facing taxonomy in `brain.core.errors`, deliberately and for the reason
    `brain.core.redaction.UntypedShapeError` gives: nobody asking a question should ever see
    this. It is a contract violation by whoever wrote the connector, and it should stop that
    connector being registered rather than degrade somebody's answer at request time.
    """


# ------------------------------------------------------------- scope at connect (M11.2.3)
#: Selectors that mean "everything the credential can reach". A connector scope is a
#: narrowing, so a selector that narrows nothing is refused rather than accepted and warned
#: about: a warning at connect time is read once, by the person who is already installing.
UNBOUNDED_SELECTORS: Final[frozenset[str]] = frozenset(
    {"", "*", "**", "/", "%", ".", "all", "any", "everything", "root"}
)

#: What a resource kind and a selector may be called. The kind is a name so it can be shown
#: in a console row and matched by an operator; the selector is the source's own identifier,
#: which is why it admits mixed case and the punctuation real ids carry.
_KIND_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_SELECTOR_RE: Final = re.compile(r"^[A-Za-z0-9_.:@/\\-]{1,200}$")


@dataclass(frozen=True)
class ConnectorScope:
    """What this connector was connected to, decided once, at connect.

    One Drive folder rather than the whole Drive; one Base table rather than the tenant; the
    three views a report needs rather than the schema. The architecture states this as a
    property enforced regardless of transport, so it is a field on the manifest rather than
    an argument each transport interprets for itself.

    Rejected: expressing this as a `brain.core.scope.Scope`. A Scope is a row predicate
    evaluated against records we already have, and this is a decision about which resources
    are reachable at all. Reusing the type would put two different questions behind one
    vocabulary, and the day somebody composes them the narrowing guarantee that makes Scope
    inspectable stops being about anything.
    """

    resource_kind: str
    selectors: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _KIND_RE.match(self.resource_kind):
            msg = (
                f"resource kind {self.resource_kind!r} is not a name; it is shown in a "
                "console row and matched by an operator, so it has to read as one"
            )
            raise ConnectorContractError(msg)
        if not self.selectors:
            msg = (
                f"a {self.resource_kind} scope names nothing, which reaches everything the "
                "credential reaches; scope at connect is a narrowing or it is not a scope"
            )
            raise ConnectorContractError(msg)
        unbounded = sorted(s for s in self.selectors if s.strip().casefold() in UNBOUNDED_SELECTORS)
        if unbounded:
            msg = (
                f"{self.resource_kind} scope {unbounded} narrows nothing; one folder, not "
                "the whole Drive. Narrowing it later does not un-fetch what was already read"
            )
            raise ConnectorContractError(msg)
        for selector in self.selectors:
            if not _SELECTOR_RE.match(selector):
                msg = (
                    f"{self.resource_kind} selector {selector!r} is not an identifier the "
                    "source would recognise; a selector that cannot be matched is a scope "
                    "that admits whatever the transport decides it meant"
                )
                raise ConnectorContractError(msg)

    def admits(self, selector: str) -> bool:
        """Whether this scope covers one resource. Exact membership, never a prefix.

        Prefix matching was the first version and it is wrong in the direction that matters:
        a scope of `folder_17` would admit `folder_170`, which is a different folder
        belonging to somebody else, and the mistake reads as correct in every test where the
        ids happen not to share a prefix.
        """
        return selector in self.selectors


# ------------------------------------------------------ the credential binding (M11.2.1)
@dataclass(frozen=True)
class CredentialBinding:
    """Where a connector's credential lives, and what it is allowed to be used for.

    A `SecretRef` and never a value: the reference is safe in a configuration row and in a
    database, and is useless to anybody who cannot already reach the vault. Everything about
    why is in `brain.ops.secrets`; this adds one thing, which is that the access mode travels
    with the reference rather than beside it.

    That matters because the two are decided together and used together. A read-only mode
    pointing at a path that mints an administrative credential is not read-only, and keeping
    the mode somewhere else makes the mismatch invisible in the row an operator reads.
    """

    ref: SecretRef
    mode: AccessMode = AccessMode.READ_ONLY
    #: Who granted write. Empty for a read-only binding, required for a write one.
    write_granted_by: str = ""

    def __post_init__(self) -> None:
        if self.mode is AccessMode.WRITE and not self.write_granted_by.strip():
            msg = (
                f"the write binding for {self.ref.path!r} names nobody who granted it; "
                "write is a separate deliberate grant and the question afterwards is always "
                "who agreed to it"
            )
            raise ConnectorContractError(msg)
        if self.mode is AccessMode.READ_ONLY and self.write_granted_by.strip():
            # Not pedantry. A read-only binding carrying a granter reads as a write grant
            # that somebody downgraded, and the next person to widen it will believe the
            # approval already exists.
            msg = (
                f"the read-only binding for {self.ref.path!r} names a write granter; a "
                "granter on a read-only binding reads as an approval that was already given"
            )
            raise ConnectorContractError(msg)

    def permits(self, effect: SideEffect) -> bool:
        """Whether this binding covers a tool with that side effect.

        `SideEffect.NONE` is the only one a read-only binding covers. DRAFT is deliberately
        not exempt: a draft is a row in somebody else's system, created by us, and a
        connector that may create drafts has write access whatever the drafts are called.
        """
        if self.mode is AccessMode.WRITE:
            return True
        return effect is SideEffect.NONE


# ----------------------------------------------------------------------- health (M11.1.1)
@dataclass(frozen=True)
class ConnectorHealth:
    """One probe's result. A fact with a time on it, never a live reading.

    `checked_at` is not decoration: a health dashboard showing OK with no time on it is a
    dashboard that shows OK after the prober itself has stopped, which is the failure mode
    that makes a health page worse than no health page. `brain.gate.provenance` makes the
    same argument at greater length about a citation's read time.
    """

    connector: str
    state: HealthState
    checked_at: datetime
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        """Whether a request may be routed here at all.

        DEGRADED is usable, and that is the point of it existing. A connector answering
        slowly is still answering, and refusing it would turn a latency problem into an
        outage; what DEGRADED does is let the composer say so.
        """
        return self.state in (HealthState.OK, HealthState.DEGRADED)


# ------------------------------------------------------------------ the fetch contract
@dataclass(frozen=True)
class FetchRequest:
    """What a connector is asked for. Deliberately small, and deliberately not a query.

    `filters` are the source's own vocabulary, passed through: the gate has already decided
    what may be seen, and a filter here is about which records are worth fetching, never
    about which a caller may have. `limit` is the caller's, and it is a request rather than a
    guarantee, because a source with a hard result ceiling (Freshdesk's 300) will return less
    than asked for while looking like it returned everything.
    """

    entity: str
    filters: tuple[tuple[str, str], ...] = ()
    limit: int = 0
    #: Where the caller left off. Opaque, because a cursor's shape is the source's business
    #: and parsing one here would make us wrong the day the source changes it.
    cursor: str = ""


class ConnectorFetch(Protocol):
    """The one shape a connector's read side may take.

    Synchronous, matching `brain.models.adapter.Transport` and for the same reason: the
    domain layer above is synchronous, and an async fetch would make every caller of it async
    all the way up to the gate. Fan-out concurrency lives in `brain.connectors.federation`,
    where it is a plan over independent calls rather than a property of one call's signature.
    """

    def __call__(self, request: FetchRequest) -> TypedResult[Entity]: ...


# --------------------------------------------------- what a fetch may never be handed
#: Types that carry a permission decision or its input. A fetch function holding one of these
#: is a fetch function that can filter by entitlement, and every one of those has to be
#: audited separately for what it does with it.
#:
#: `Scope` is deliberately absent: see `A_SCOPE_PREDICATE_IS_NOT_A_GRANT`.
DECIDING_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "EntitlementSet",
        "EntitlementStore",
        "Capability",
        "FieldPolicy",
        "Principal",
        "PrincipalRef",
        "Mask",
        "RedactedAnswer",
        "RedactionTrace",
    }
)

#: Types that let an adapter reach a credential on its own terms rather than being handed one
#: for the duration of a call. A `Vault` can mint; a `SecretRef` can name a path and then be
#: minted from. `Lease` is deliberately absent, because being handed a lease is the whole
#: mechanism: it expires, it is revoked in a `finally`, and it cannot be read after either.
CREDENTIAL_SOURCE_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {"Vault", "OpenBaoVault", "SecretRef", "SecretsProvider", "VaultRole"}
)

#: Attribute names that hold a credential whatever they are annotated as. Checked by name
#: because a stored credential is usually a `str`, and a rule that only looked at types would
#: pass `api_key: str` while refusing the honest `lease: Lease` that is actually safe.
CREDENTIAL_ATTRIBUTE_RE: Final = re.compile(
    r"(^|_)(secret|password|passwd|token|api_key|apikey|access_key|private_key|credential"
    r"|credentials|bearer|session_key)(_|$)"
)


def _annotation_text(annotation: object) -> str:
    """One rendering of an annotation, whether it arrived as a string or an object.

    The same two-case problem `brain.core.redaction._annotation_text` solves, and solved the
    same way: a module with `from __future__ import annotations` hands over the text as
    written, one without it hands over an object. Matching text rather than resolving the
    object is what lets this run against a connector whose module has not finished importing,
    which is when a registry actually runs it.
    """
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _names_in(annotation: object) -> frozenset[str]:
    """Every identifier in an annotation, however it is spelled.

    Crude on purpose, for the reason `brain.core.redaction._names_in` gives: `EntitlementSet`,
    `"EntitlementSet | None"` and `entitlement.EntitlementSet` all have to read the same, and
    a parser that understood the type algebra would be a second opinion about what an
    annotation means.
    """
    return frozenset(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _annotation_text(annotation)))


def assert_fetches_only(fetch: Callable[..., object]) -> None:
    """Refuse a fetch function that could decide, or could mint a credential (M11.1.1).

    Four refusals, and the first two are the module's whole claim.

    **A parameter naming a deciding type.** The leak itself. A connector handed an
    `EntitlementSet` will eventually use it, not out of malice but because filtering at the
    source is faster, and the day it does there are two places that answer a permission
    question and the permissive one wins silently.

    **A parameter naming a vault or a secret reference.** Either lets the adapter reach a
    credential by path, on its own schedule, outside `borrow`'s `finally`. A `Lease` is
    fine and is the intended shape.

    **`*args` or `**kwargs`.** A signature that accepts anything has declared nothing, so it
    cannot be shown not to accept the two above.

    **An unannotated parameter.** Default-deny, the same answer `assert_channel_adapter`
    gives: an unannotated parameter can hold anything, including the caller's grants.

    Note what this does not check. It does not read the body, and a determined author can
    still reach a module-level singleton. That is why `assert_holds_no_credential` exists
    beside it and why the redactor runs regardless: this refuses the mistake, and the layers
    below refuse the consequence.
    """
    try:
        signature = inspect.signature(fetch)
    except (TypeError, ValueError) as exc:
        msg = f"{getattr(fetch, '__name__', fetch)!r} has no readable signature to check"
        raise ConnectorContractError(msg) from exc

    name = getattr(fetch, "__name__", repr(fetch))
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            # A bound method's receiver carries the connector itself, which is checked by
            # `assert_holds_no_credential` rather than here. Refusing it would make every
            # method-shaped connector illegal, and the class is the ordinary way to hold a
            # transport.
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            msg = (
                f"connector fetch {name!r} takes {parameter.name!r} as *args or **kwargs; a "
                "signature that accepts anything has declared nothing, so it cannot be shown "
                "never to receive the caller's grants"
            )
            raise ConnectorContractError(msg)
        if parameter.annotation is inspect.Parameter.empty:
            msg = (
                f"connector fetch {name!r} has an unannotated parameter {parameter.name!r}; "
                "an unannotated parameter can hold an entitlement set, so it is refused for "
                "the same reason an unclassified field is withheld"
            )
            raise ConnectorContractError(msg)
        names = _names_in(parameter.annotation)
        deciding = sorted(names & DECIDING_TYPE_NAMES)
        if deciding:
            msg = (
                f"connector fetch {name!r} would be handed {deciding} in {parameter.name!r}; "
                "a connector returns everything it fetched and the redactor removes what is "
                "not covered, so nothing here may be given the input to that decision"
            )
            raise ConnectorContractError(msg)
        credential_source = sorted(names & CREDENTIAL_SOURCE_TYPE_NAMES)
        if credential_source:
            msg = (
                f"connector fetch {name!r} would be handed {credential_source} in "
                f"{parameter.name!r}; a connector borrows a lease for the duration of a call "
                "and reads no credential by path, so it takes a Lease and never a vault"
            )
            raise ConnectorContractError(msg)


def assert_holds_no_credential(connector: type | object) -> None:
    """Refuse a connector that keeps a credential between calls (M11.2.6).

    Checked over annotations rather than over instance state, so it runs on a class before
    anything has been constructed and cannot be defeated by a value that happens to be None
    at inspection time.

    Two rules. An attribute whose *name* says credential is refused whatever its type,
    because a stored credential is nearly always a `str` and a type-only rule would pass
    `api_key: str` while refusing the honest `lease: Lease`. And an attribute whose *type* is
    a vault or a secret reference is refused, because either one is read-by-path with an
    extra step.

    `Lease` is allowed as an attribute name and as a type, and that is not an oversight: a
    connector constructed inside a `borrow` block, used, and dropped when the block exits is
    exactly the intended lifetime. What is refused is the field that outlives the block.
    Nothing here can tell those apart from annotations alone, which is why the lease itself
    carries an expiry and refuses to be read after it.

    This is what makes `ROTATION_NEEDS_NO_REDEPLOY` true rather than hoped for. A connector
    with no credential attribute has nothing to invalidate when the vault mints a new value,
    so rotation is a vault-side event and the next run picks it up.
    """
    target = connector if isinstance(connector, type) else type(connector)
    annotations: dict[str, object] = {}
    for base in reversed(target.__mro__):
        annotations.update(getattr(base, "__annotations__", {}) or {})

    offenders: list[str] = []
    for attribute, annotation in annotations.items():
        if CREDENTIAL_ATTRIBUTE_RE.search(attribute.casefold()):
            offenders.append(f"{attribute} (named for a credential)")
            continue
        held = sorted(_names_in(annotation) & CREDENTIAL_SOURCE_TYPE_NAMES)
        if held:
            offenders.append(f"{attribute}: {', '.join(held)}")

    if offenders:
        msg = (
            f"connector {target.__name__} holds {offenders}; a credential is borrowed for "
            "one run and revoked in a finally, so a connector that keeps one has a value "
            "no rotation can invalidate and no revocation can reach"
        )
        raise ConnectorContractError(msg)


def identity_mode_default() -> IdentityMode:
    """The requester's own credentials, unless a tool declares otherwise (M11.2.5).

    A function rather than a constant so that the default has one definition and reads the
    same at every call site. `IdentityMode.DELEGATED` means the source enforces its own
    permissions in addition to ours, which is a second independent check for free; SERVICE
    means ours are the only ones there are, which is why `brain.tools.registry` refuses a
    SERVICE tool that carries no scope predicate.
    """
    return IdentityMode.DELEGATED
