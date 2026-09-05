"""Turning a credential from the identity provider into somebody the gate can reason about.

Everything downstream starts from a `Principal`, and this is the only place one is produced
from something a browser sent us. What arrives is an attacker-controlled string. What leaves
is a principal or a refusal, and the module is arranged so that nothing in between can be
read before it has been proven.

Four things break without it.

**A token gets read before it is checked.** The obvious implementation decodes the payload,
looks at `sub`, finds the person, and verifies the signature somewhere near the end because
that is the fiddly part. Every claim read before the signature check is user input wearing
the shape of an identity, and the bug does not look like a bug: the happy path is identical.
`validate_token` reads the header (which selects a key, and is treated as hostile), verifies,
and only then touches a claim. `VerifiedClaims` is the only type the mapping functions
accept, so "I already checked it upstream" is not something a caller can assert by habit.

**`alg: none`, and the same key used two ways.** Both are the same failure: the token tells
us how to check the token. `none` is refused by name because it is the canonical attack and
deserves its own line in a log. Everything else is refused by allow-list, and the allow-list
holds asymmetric algorithms only, so an RSA public key can never be handed to an HMAC
verifier as a shared secret. A `kid` we do not hold is refused outright: trying the other
keys is how a retired key, or an attacker's, gets one free attempt per key on file.

**The identity provider quietly becomes the permission model.** A `groups` claim is the most
tempting place in any system to put entitlements, because it is one line and it works. It
also moves the answer to "who can see the margin on this client" into a directory nobody in
this company reviews, which a client's own IdP administrator may control, and which has no
scope grammar. So group sync maps to `Role` and to nothing else, `roles_from_groups` returns
`frozenset[Role]` by signature, and `assert_no_capability_from_claims` refuses a function
whose return type could carry a capability at all.

**An unknown subject becomes a principal with nothing in it.** A valid token from a real
person we have never onboarded is a normal event, and the wrong answer is an empty
`Principal`: it type-checks everywhere a real one does, so it flows into the gate, resolves
to an empty entitlement set and produces a confident "I could not find that". It gets
`UnmappedSubject` instead, which is `brain.gate.ingress.Unrecognised` for a token: no
entitlement to intersect, no id to cache under, and one instruction.

**No network call is made anywhere in this module, and no dependency was added.** Validation
is a pure function over a decoded token and a key set handed in. The signature check itself
is an injected callback, because the standard library cannot verify RS256 and inventing a
verifier here would be the worst thing in the repository. JWKS caching is a cache with a
fetch callback, the shape `brain.gate.resolve` uses for the entitlement store. Nothing here
has been run against a live Keycloak.

No SQLAlchemy model and no migration is written here. Where a leaf implies a table
(`principal_identity`), this is the type and the rules only.

Task ids: M1.1.2, M1.1.3, M1.1.4, M1.1.5
"""

from __future__ import annotations

import base64
import binascii
import enum
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain.core.principal import Principal
from brain.core.scope import Scope
from brain.identity.roles import SCOPE_REQUIRED, IdentityError, Role, RoleGrant

# ------------------------------------------------------------------ refusals
#: The only thing a person is ever told about a refused token. Every reason below collapses
#: into this one sentence, for the reason `brain.gate.ingress` has one prompt for every
#: unrecognised sender: "unknown key" and "bad signature" and "wrong audience" tell somebody
#: forging a token which part to fix next, and the difference is invisible in a screenshot.
SIGN_IN_PROMPT: Final = "I could not accept that sign-in. Please sign in again."


class TokenRefusal(enum.StrEnum):
    """Why a token was refused. For the operator's log, never for the presenter.

    A closed vocabulary rather than free text, for the reason `BreakGlassReason` is one:
    "how many tokens were refused for an unknown key this week" should be a count rather
    than a reading exercise, and a rise in `UNKNOWN_KEY` is a key rotation nobody finished
    while a rise in `BAD_SIGNATURE` is somebody trying things.
    """

    MALFORMED = "malformed"
    ALG_NONE = "alg_none"
    ALG_NOT_ALLOWED = "alg_not_allowed"
    ALG_MISMATCH = "alg_mismatch"
    NO_KEY_ID = "no_key_id"
    UNKNOWN_KEY = "unknown_key"
    KEY_NOT_FOR_SIGNING = "key_not_for_signing"
    BAD_SIGNATURE = "bad_signature"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    ISSUED_IN_FUTURE = "issued_in_future"
    MISSING_CLAIM = "missing_claim"
    WRONG_TYPE = "wrong_type"
    NO_KEYS_AVAILABLE = "no_keys_available"
    LOGGED_OUT = "logged_out"
    SESSION_UNKNOWN = "session_unknown"
    SESSION_EXPIRED = "session_expired"
    SESSION_MISMATCH = "session_mismatch"
    PRINCIPAL_INACTIVE = "principal_inactive"
    NOT_A_SERVICE_ACCOUNT = "not_a_service_account"
    UNKNOWN_SERVICE_ACCOUNT = "unknown_service_account"
    SERVICE_ACCOUNT_EXPIRED = "service_account_expired"
    OWNER_INACTIVE = "owner_inactive"


class TokenRefusedError(IdentityError):
    """A credential was not acceptable. Carries a reason for the log and never for the user.

    Outside the `brain.core.errors` taxonomy on purpose, exactly as `IdentityError` is:
    those five outcomes each describe an answer to a question, and a request that failed to
    authenticate never got to ask one. `BindingRefusedError` in `brain.gate.ingress` is the
    same shape for the same reason.
    """

    def __init__(self, reason: TokenRefusal, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else str(reason))
        self.reason = reason
        #: What may be shown to a person. Identical for every reason, deliberately.
        self.public_message = SIGN_IN_PROMPT


def _refuse(reason: TokenRefusal, detail: str = "") -> TokenRefusedError:
    return TokenRefusedError(reason, detail)


# ---------------------------------------------------------------- algorithms
#: The algorithms a token may be signed with. Asymmetric only.
#:
#: HS256 is absent and that is the point. An HMAC-signed token is signed by anything holding
#: the client secret, which is every copy of the configuration; and the classic confusion
#: attack is to take the RSA *public* key, which is published, and present it as the HMAC
#: secret. An allow-list of asymmetric algorithms makes that attack unrepresentable rather
#: than merely checked for.
#:
#: Rejected: taking the permitted algorithm from the key set alone and skipping this list.
#: The key set comes from the IdP, so a compromised or misconfigured IdP could introduce a
#: symmetric key and this client would follow it. Two independent gates that must agree is
#: the cheap version of not trusting one input.
ALLOWED_ALGORITHMS: Final[frozenset[str]] = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)

#: Spelled out so `alg: none` is refused by name and appears as itself in a log, before the
#: allow-list would have refused it anyway. Two checks, because this one is worth counting.
#: The casings are here because a case-insensitive comparison would also match a real
#: algorithm spelled oddly, and this set is about one specific value.
ALG_NONE: Final[frozenset[str]] = frozenset({"none", "None", "NONE", "nOnE", ""})

#: The `typ` values a token presented as a credential may carry. A refresh token presented
#: where an access token belongs is a real attack: it lives far longer, and a resource server
#: that accepts one has turned its short access-token lifetime into decoration.
ACCEPTED_TYPES: Final[frozenset[str]] = frozenset({"Bearer", "JWT", "ID"})

#: The widest clock skew anybody may ask for. Two minutes covers a badly synchronised VPS;
#: beyond that `exp` stops meaning anything, which is what this bound exists to prevent.
#: Unbounded leeway is no expiry check with extra steps.
MAX_LEEWAY: Final = timedelta(seconds=120)

#: What a caller gets if they say nothing. Small enough to be honest about.
DEFAULT_LEEWAY: Final = timedelta(seconds=30)


# ----------------------------------------------------------------- key sets
class SigningKey(BaseModel):
    """One key from the identity provider's JWKS, as this module needs it.

    `material` is opaque here: a PEM, a JWK blob, whatever the injected verifier
    understands. Parsing it would mean owning a cryptography dependency and a second
    implementation of a well-specified format, and getting either wrong is silent.

    `algorithm` is stored on the key rather than taken from the token, so a key published
    for RS256 cannot be used to check an ES256 token that names its `kid`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kid: str = Field(min_length=1, max_length=200)
    algorithm: str = Field(min_length=1, max_length=20)
    material: str = Field(min_length=1)
    #: JWKS `use`. A key published for encryption must never verify a signature.
    use: str = "sig"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.algorithm not in ALLOWED_ALGORITHMS:
            msg = (
                f"key {self.kid!r} is published for {self.algorithm!r}, which is not in the "
                f"allow-list {sorted(ALLOWED_ALGORITHMS)}"
            )
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class KeySet:
    """The keys one issuer is currently publishing.

    Duplicate `kid`s are refused at construction. Two keys with one name means the choice
    between them is arbitrary, and an arbitrary choice inside a signature check is a coin
    flip an attacker gets to call: publish a key with a live `kid` and half the tokens
    verify.
    """

    issuer: str
    keys: tuple[SigningKey, ...]
    fetched_at: datetime

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for key in self.keys:
            if key.kid in seen:
                msg = f"key set for {self.issuer} publishes {key.kid!r} twice"
                raise ValueError(msg)
            seen.add(key.kid)

    def by_kid(self, kid: str) -> SigningKey | None:
        """The key with this id, or None. Never a fallback, never "the only one there is"."""
        return next((k for k in self.keys if k.kid == kid), None)


class JwksFetch(Protocol):
    """Fetches a key set for an issuer. Injected, so this module makes no network call.

    The same seam as `EntitlementStore` in `brain.gate.resolve`: the slow, failing, outside
    thing is a protocol the caller satisfies, and everything here is testable without it.
    """

    def __call__(self, issuer: str) -> KeySet: ...


#: How long a fetched key set is treated as current. Keycloak rotates on a schedule measured
#: in months, so an hour costs nothing and bounds how long a key retired for cause stays
#: usable here.
JWKS_TTL: Final = timedelta(hours=1)

#: How long a stale set may still be served when the IdP is unreachable. Public keys do not
#: become secret, so serving a slightly old set is safe; refusing every sign-in in the
#: company because one HTTP call failed is not. The grace is bounded, so an IdP down for a
#: day cannot keep a rotated-out key alive indefinitely.
JWKS_STALE_GRACE: Final = timedelta(hours=6)

#: The floor between two fetches triggered by an unrecognised `kid`. Without it, anybody can
#: make this process hammer the identity provider by presenting tokens with random key ids,
#: which is a denial of service carried out with our own credentials.
JWKS_MIN_REFETCH: Final = timedelta(seconds=30)


class JwksCache:
    """A key-set cache with the fetch injected, and a rate-limited path for key rotation.

    The awkward case is a legitimate rotation: a new `kid` appears before the TTL expires,
    and every token signed with it is refused until the cache happens to expire. Refetching
    on an unknown `kid` fixes that and creates the amplification problem above, so the
    refetch is rate limited per issuer.

    Rejected: trying every key when the `kid` is unknown. It turns one guess into one guess
    per key on file and defeats the point of naming a key at all.
    """

    def __init__(
        self,
        fetch: JwksFetch,
        *,
        ttl: timedelta = JWKS_TTL,
        stale_grace: timedelta = JWKS_STALE_GRACE,
        min_refetch: timedelta = JWKS_MIN_REFETCH,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._stale_grace = stale_grace
        self._min_refetch = min_refetch
        self._sets: dict[str, KeySet] = {}
        self._last_attempt: dict[str, datetime] = {}

    def keys_for(self, issuer: str, now: datetime) -> KeySet:
        """The current key set for this issuer, fetching when there is nothing fresh."""
        cached = self._sets.get(issuer)
        if cached is not None and now - cached.fetched_at < self._ttl:
            return cached
        try:
            fetched = self._fetch(issuer)
        except Exception as exc:
            # Any transport failure lands here. Serving a cached set inside the grace window
            # is deliberate; raising past it is equally deliberate.
            if cached is not None and now - cached.fetched_at < self._ttl + self._stale_grace:
                return cached
            raise _refuse(TokenRefusal.NO_KEYS_AVAILABLE, f"{issuer}: {exc}") from exc
        self._sets[issuer] = fetched
        self._last_attempt[issuer] = now
        return fetched

    def key_for(self, issuer: str, kid: str, now: datetime) -> SigningKey:
        """The named key, refetching once when the id is unknown and we are not rate limited.

        Raises rather than returning None. A caller holding an `Optional[SigningKey]` is one
        `if key is None: key = keys[0]` away from the fallback this exists to refuse.
        """
        keys = self.keys_for(issuer, now)
        found = keys.by_kid(kid)
        if found is not None:
            return found

        last = self._last_attempt.get(issuer)
        if last is not None and now - last < self._min_refetch:
            raise _refuse(TokenRefusal.UNKNOWN_KEY, f"{kid!r} not published by {issuer}")
        self._last_attempt[issuer] = now
        try:
            refreshed = self._fetch(issuer)
        except Exception as exc:
            raise _refuse(TokenRefusal.UNKNOWN_KEY, f"{kid!r}: {exc}") from exc
        self._sets[issuer] = refreshed
        found = refreshed.by_kid(kid)
        if found is None:
            raise _refuse(TokenRefusal.UNKNOWN_KEY, f"{kid!r} not published by {issuer}")
        return found


# ------------------------------------------------------------------- tokens
@dataclass(frozen=True)
class RawToken:
    """A decoded token that nothing has been proven about. Not an identity.

    Named so a reader of a call site can see what it is. `payload` is attacker input in
    dictionary form, and the only function in this system that may read it is
    `validate_token`. Everything else takes `VerifiedClaims`.
    """

    header: Mapping[str, object]
    payload: Mapping[str, object]
    #: `<header>.<payload>` exactly as it arrived. Re-encoding it here would change the
    #: bytes that were signed, because JSON serialisation is not canonical.
    signing_input: bytes
    signature: bytes


class SignatureVerifier(Protocol):
    """Checks one signature against one key. Injected, and deliberately not implemented here.

    The standard library cannot verify RS256 or ES256, and this milestone adds no
    dependency. Writing a verifier by hand would be worse than declaring the seam, so
    whoever wires this up supplies one backed by a reviewed library, and every test here
    supplies one it controls.
    """

    def __call__(self, *, signing_input: bytes, signature: bytes, key: SigningKey) -> bool: ...


@dataclass(frozen=True)
class VerifiedClaims:
    """Claims from a token whose signature, issuer, audience and lifetime all checked out.

    Constructed by `validate_token` and by nothing else in this package. It carries
    `key_id`, `algorithm` and `verified_at` because a claim about a token should say what
    made it believable: an audit entry reading "verified by kid abc123 at 09:14" can be
    argued with, and "trusted" cannot.

    `claims` is a read-only view. A caller who could mutate it could rewrite `sub` after the
    check, which is reading before the check with the order reversed.
    """

    issuer: str
    subject: str
    audience: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    #: Keycloak's `sid`. None on a token belonging to no interactive session, which is what
    #: a service-account token looks like.
    session_id: str | None
    key_id: str
    algorithm: str
    verified_at: datetime
    claims: Mapping[str, object]

    def claim(self, name: str) -> object | None:
        """One claim by name. A method rather than dictionary access at every call site, so
        that grepping for `.claim(` finds every place a claim is consulted."""
        return self.claims.get(name)


def _b64url(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise _refuse(TokenRefusal.MALFORMED, "segment is not base64url") from exc


def parse_unverified(compact: str) -> RawToken:
    """Split a compact JWS into a `RawToken`. Proves nothing, and is named so that is plain.

    This exists because the alternative is every caller writing their own split-and-decode,
    and the hand-written one is always shorter than this because it leaves out the segment
    count. Five segments is a JWE, which we cannot decrypt and must never treat as an
    unencrypted token with an odd shape.
    """
    parts = compact.split(".")
    if len(parts) != 3:
        raise _refuse(TokenRefusal.MALFORMED, f"expected 3 segments, got {len(parts)}")
    header_raw, payload_raw, signature_raw = parts
    try:
        header = json.loads(_b64url(header_raw))
        payload = json.loads(_b64url(payload_raw))
    except json.JSONDecodeError as exc:
        raise _refuse(TokenRefusal.MALFORMED, "header or payload is not JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise _refuse(TokenRefusal.MALFORMED, "header and payload must be JSON objects")
    return RawToken(
        header=header,
        payload=payload,
        signing_input=f"{header_raw}.{payload_raw}".encode("ascii"),
        signature=_b64url(signature_raw),
    )


def _as_str(value: object, name: str, reason: TokenRefusal) -> str:
    if not isinstance(value, str) or not value:
        raise _refuse(reason, f"{name} is missing or not a string")
    return value


def _as_epoch(value: object, name: str) -> datetime:
    """A numeric date claim, as an aware datetime.

    `bool` is refused explicitly. `isinstance(True, int)` is True in Python, so `exp: true`
    would otherwise become 1 January 1970 and read as an *expired* token rather than as the
    malformed one it is. A string is refused for the same reason: a coercion that turns
    `"0"` into zero makes a forged token look expired, and the two are logged differently.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _refuse(TokenRefusal.MISSING_CLAIM, f"{name} is missing or not a number")
    return datetime.fromtimestamp(value, tz=UTC)


def _audience_of(payload: Mapping[str, object]) -> tuple[str, ...]:
    """`aud` as a tuple, whether it arrived as a string or a list. Anything else is empty."""
    raw = payload.get("aud")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(a for a in raw if isinstance(a, str))
    return ()


def validate_token(
    token: RawToken,
    *,
    keys: KeySet,
    verify: SignatureVerifier,
    expected_issuer: str,
    expected_audience: str,
    now: datetime,
    leeway: timedelta = DEFAULT_LEEWAY,
    accepted_types: frozenset[str] = ACCEPTED_TYPES,
) -> VerifiedClaims:
    """Check a token completely, or refuse it. Signature first, always (M1.1.2).

    The order below is the whole function. Header fields are read first because they select
    a key, and they are treated as hostile throughout: `alg` is checked against a list we own
    rather than used to pick an implementation, and `kid` is a lookup that either finds a key
    or ends the function. The signature is checked before a single claim is read, so no state
    in this function is derived from unverified input.

    Rejected: verifying and validating in two public functions. It reads better and it means
    the second one can be forgotten, which is exactly the bug where a token with a valid
    signature from the right IdP but the wrong audience is accepted by a service it was
    never issued for.
    """
    if not timedelta(0) <= leeway <= MAX_LEEWAY:
        # Not a token refusal: nothing is wrong with the token. A caller asking for an hour
        # of skew is asking for expiry to stop working, and that is a bug in the caller.
        msg = f"clock skew must be between 0 and {MAX_LEEWAY}, not {leeway}"
        raise IdentityError(msg)

    # --- header: hostile, and read only to choose how to check the signature -------------
    alg_value = token.header.get("alg")
    if not isinstance(alg_value, str) or alg_value in ALG_NONE:
        raise _refuse(TokenRefusal.ALG_NONE, "token declares no signing algorithm")
    if alg_value not in ALLOWED_ALGORITHMS:
        raise _refuse(TokenRefusal.ALG_NOT_ALLOWED, f"{alg_value!r} is not permitted")

    kid_value = token.header.get("kid")
    if not isinstance(kid_value, str) or not kid_value:
        # No fallback to "the only key" or "the newest key". A token that does not say which
        # key signed it would otherwise get one attempt per key on file, and retired keys
        # stay on file.
        raise _refuse(TokenRefusal.NO_KEY_ID, "token names no key")
    key = keys.by_kid(kid_value)
    if key is None:
        raise _refuse(TokenRefusal.UNKNOWN_KEY, f"{kid_value!r} is not in the key set")
    if key.algorithm != alg_value:
        # Algorithm confusion, caught even with a known key: the token asks for HS256 and
        # names an RS256 key, hoping the public key gets used as a shared secret.
        raise _refuse(
            TokenRefusal.ALG_MISMATCH,
            f"key {kid_value!r} signs {key.algorithm}, token claims {alg_value}",
        )
    if key.use != "sig":
        raise _refuse(TokenRefusal.KEY_NOT_FOR_SIGNING, f"key {kid_value!r} has use={key.use!r}")

    if not verify(signing_input=token.signing_input, signature=token.signature, key=key):
        raise _refuse(TokenRefusal.BAD_SIGNATURE, f"signature fails against {kid_value!r}")

    # --- below this line the payload is evidence rather than input ------------------------
    issuer = _as_str(token.payload.get("iss"), "iss", TokenRefusal.WRONG_ISSUER)
    if issuer != expected_issuer:
        # Exact string equality. Rejected: normalising trailing slashes, or comparing hosts.
        # Both are how `https://idp.example.com.attacker.net/` gets accepted by a comparison
        # somebody wrote to be forgiving about configuration typos.
        raise _refuse(TokenRefusal.WRONG_ISSUER, f"{issuer!r} is not {expected_issuer!r}")

    audience = _audience_of(token.payload)
    if expected_audience not in audience:
        raise _refuse(TokenRefusal.WRONG_AUDIENCE, f"{expected_audience!r} not in {audience}")
    if len(audience) > 1 and token.payload.get("azp") != expected_audience:
        # OIDC's own rule. Several audiences means the token was minted for more than one
        # party, and `azp` is the only claim that says which one was asking.
        raise _refuse(TokenRefusal.WRONG_AUDIENCE, "multi-audience token with wrong azp")

    typ = token.payload.get("typ")
    if isinstance(typ, str) and typ not in accepted_types:
        raise _refuse(TokenRefusal.WRONG_TYPE, f"{typ!r} is not one of {sorted(accepted_types)}")

    expires_at = _as_epoch(token.payload.get("exp"), "exp")
    if now - leeway >= expires_at:
        raise _refuse(TokenRefusal.EXPIRED, f"expired at {expires_at.isoformat()}")

    nbf = token.payload.get("nbf")
    if nbf is not None:
        not_before = _as_epoch(nbf, "nbf")
        if now + leeway < not_before:
            raise _refuse(TokenRefusal.NOT_YET_VALID, f"valid from {not_before.isoformat()}")

    # `iat` is required rather than optional, because logout propagation is expressed as a
    # not-before floor over it (see `brain.identity.sessions`). A token with no issue time
    # cannot be placed on either side of a logout, so it would have to be trusted or
    # refused, and refusing here fails at sign-in rather than after one.
    issued_at = _as_epoch(token.payload.get("iat"), "iat")
    if issued_at - leeway > now:
        raise _refuse(TokenRefusal.ISSUED_IN_FUTURE, f"issued at {issued_at.isoformat()}")

    subject = _as_str(token.payload.get("sub"), "sub", TokenRefusal.MISSING_CLAIM)
    sid = token.payload.get("sid")

    return VerifiedClaims(
        issuer=issuer,
        subject=subject,
        audience=audience,
        issued_at=issued_at,
        expires_at=expires_at,
        session_id=sid if isinstance(sid, str) and sid else None,
        key_id=key.kid,
        algorithm=key.algorithm,
        verified_at=now,
        claims=MappingProxyType(dict(token.payload)),
    )


# ------------------------------------------------------- claims to a principal
#: What somebody sees when their token is perfectly valid and we have never heard of them.
#: It says what to do and confirms nothing about who else exists, the same discipline as
#: `brain.gate.ingress.UNRECOGNISED_PROMPT`.
UNMAPPED_PROMPT: Final = (
    "Your sign-in worked, but this account has not been set up on the Brain yet. "
    "Ask an administrator to add you."
)


@dataclass(frozen=True)
class UnmappedSubject:
    """A verified subject with no principal: the shape `ingress.Unrecognised` uses.

    Carries no `EntitlementSet`, empty or otherwise, for the reason `NoStandingEntitlement`
    carries none. An empty set is a thing that can be intersected with a ceiling, hashed
    into a cache key and passed down a delegation chain, so the day somebody adds a default
    grant to the resolver, every stranger holding a valid token acquires it.

    It does carry the issuer and subject, which `Unrecognised` deliberately does not. The
    difference is what the identifier is: a phone number is on the projection denylist and a
    table of them is a company phone book, whereas a Keycloak `sub` is an opaque uuid that
    means nothing outside the IdP, and an operator asked "why can Priya not sign in" needs
    it to answer at all.
    """

    issuer: str
    subject: str
    prompt: str = UNMAPPED_PROMPT


class PrincipalDirectory(Protocol):
    """Our own record of who exists, keyed by issuer and IdP subject.

    A protocol rather than a query, for the reason `EntitlementStore` is one: this module
    stays testable without a database, and the table behind it (`principal_identity`,
    M1.2.2) belongs to whoever owns `src/brain/tables`.
    """

    def principal_for_subject(self, issuer: str, subject: str) -> Principal | None: ...


def principal_for(
    claims: VerifiedClaims,
    directory: PrincipalDirectory,
    *,
    now: datetime,
) -> Principal | UnmappedSubject:
    """The person behind a verified token, or the fact that there is not one yet (M1.1.3).

    The directory is the authority, not the token. A `Principal` is never assembled out of
    claims, and the reason is `employment`: a claim that could set it to STAFF would erase
    the mandatory `not_after` on a contractor, which `brain.core.principal` calls the single
    most common way a permission model rots. The IdP says who signed in. This company says
    what they are.

    An expired principal returns `UnmappedSubject` rather than an inactive `Principal`, so a
    leaver whose Keycloak account still works cannot reach a code path holding a real
    principal object. `Principal.is_active` is still enforced at entitlement time; this is
    the earlier of two gates rather than a replacement for it.
    """
    found = directory.principal_for_subject(claims.issuer, claims.subject)
    if found is None or not found.is_active(now):
        return UnmappedSubject(issuer=claims.issuer, subject=claims.subject)
    return found


class ClaimMapping(BaseModel):
    """Which claim carries which fact (M1.1.3).

    Configuration rather than constants, because every federated IdP names things
    differently and the alternative is a fork of this module per client. What is *not*
    configurable is the set of facts: there is no `employment_claim` and no
    `capability_claim`, and adding either is a diff somebody reviews rather than a line in a
    YAML file that nobody does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name_claim: str = "name"
    department_claim: str = "department"
    groups_claim: str = "groups"
    email_claim: str = "email"


@dataclass(frozen=True)
class MappedIdentity:
    """What the IdP says about somebody, in this system's vocabulary.

    A *proposal*, never an authority. It is what a reconciliation job compares against the
    principal record (the architecture lists "reconcile its own user model with Keycloak" as
    a task of its own), and it is the input to group sync. Nothing in it widens anybody's
    access on its own: the only field that reaches the permission model is `groups`, and
    `roles_from_groups` is the only thing that reads it.
    """

    subject: str
    issuer: str
    display_name: str | None
    primary_department: str | None
    email: str | None
    groups: tuple[str, ...]


def map_claims(claims: VerifiedClaims, mapping: ClaimMapping) -> MappedIdentity:
    """Read the configured claims off a verified token (M1.1.3).

    Takes `VerifiedClaims` and not a `RawToken` or a dict, which is the type-level version
    of "validate before trusting": calling this with something unchecked means constructing
    a `VerifiedClaims` by hand first, which is a visible act rather than an oversight.
    """

    def text(name: str) -> str | None:
        value = claims.claim(name)
        return value if isinstance(value, str) and value else None

    raw_groups = claims.claim(mapping.groups_claim)
    groups: tuple[str, ...] = ()
    if isinstance(raw_groups, list):
        groups = tuple(g for g in raw_groups if isinstance(g, str))

    return MappedIdentity(
        subject=claims.subject,
        issuer=claims.issuer,
        display_name=text(mapping.display_name_claim),
        primary_department=text(mapping.department_claim),
        email=text(mapping.email_claim),
        groups=groups,
    )


# ------------------------------------------------------- groups to roles, only
class GroupRoleRule(BaseModel):
    """One IdP group, one platform role, and a scope when the role requires one (M1.1.5).

    The scope requirement is read from `SCOPE_REQUIRED` rather than restated, so the two
    cannot drift. It matters more here than anywhere: a `department_admin` grant with no
    scope reads as company-wide, and a group in a client's own directory that produced one
    would be the widest row in the system, created by somebody who does not work here.

    There is no capability field on this type and there is no version of it that has one.
    That is the M1.1.5 rule written as a field list: an IdP group maps to a role and to
    nothing else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The group as the IdP spells it, e.g. `/brain/approver/web`. Compared exactly, because
    #: a prefix match would make `/brain/approver/web-archive` an approver for `web`.
    group: str = Field(min_length=1, max_length=300)
    role: Role
    scope: Scope | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.role in SCOPE_REQUIRED and self.scope is None:
            msg = (
                f"group {self.group!r} maps to {self.role}, which needs a scope; without one "
                "the grant reads as company-wide and nothing about it fails loudly"
            )
            raise ValueError(msg)
        if self.role not in SCOPE_REQUIRED and self.scope is not None:
            msg = f"{self.role} is company-wide; a scope stored against it would be read by nobody"
            raise ValueError(msg)
        if self.scope is not None and self.scope.is_unrestricted():
            msg = f"the scope on {self.group!r} restricts nothing"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class SyncedRoles:
    """The outcome of one sync: the roles matched, and the groups nobody mapped.

    The second half is the point. A group that maps to nothing is the normal state of a
    client directory full of groups that are not about us, and it is also what a renamed
    group looks like the morning after somebody renames it. Ignoring both silently makes
    them indistinguishable, and the second is an outage that presents as "my permissions
    disappeared overnight".
    """

    roles: frozenset[Role]
    unmapped_groups: tuple[str, ...]


def roles_from_groups(identity: MappedIdentity, rules: Sequence[GroupRoleRule]) -> SyncedRoles:
    """Map IdP groups onto platform roles, and report what did not map (M1.1.5).

    The return type is the enforcement. `frozenset[Role]` cannot carry a capability, a grant
    or a scope, so no amount of later editing inside this function can make a claim confer
    access; somebody would have to change the signature first, which is a diff with a
    reviewer on it. `assert_no_capability_from_claims` pins that from the outside.
    """
    by_group = {rule.group: rule for rule in rules}
    matched: set[Role] = set()
    unmapped: list[str] = []
    for group in identity.groups:
        rule = by_group.get(group)
        if rule is None:
            unmapped.append(group)
            continue
        matched.add(rule.role)
    return SyncedRoles(roles=frozenset(matched), unmapped_groups=tuple(unmapped))


def role_grants_from_groups(
    identity: MappedIdentity,
    principal: Principal,
    rules: Sequence[GroupRoleRule],
    *,
    now: datetime,
) -> tuple[RoleGrant, ...]:
    """The role grants an IdP group membership implies (M1.1.5).

    `RoleGrant` and never `SubjectGrant`: the first governs the platform, the second governs
    data, and this function can only produce the first because that is the only one it
    imports. `granted_by` names the issuer, so a row that appeared with no human behind it
    says so, which is the difference between "somebody appointed her" and "a directory did".

    Rejected: writing these rows straight to the table on every login. Group membership
    disappearing has to remove the grant, and a login-time upsert only ever adds; the
    reconciliation that removes belongs to whoever owns the table, with this as the
    statement of what should be there.
    """
    granted_by = f"idp:{identity.issuer}"
    if len(granted_by) > 128:
        msg = f"issuer {identity.issuer!r} is too long to record as a grantor"
        raise IdentityError(msg)

    by_group = {rule.group: rule for rule in rules}
    grants: list[RoleGrant] = []
    for group in identity.groups:
        rule = by_group.get(group)
        if rule is None:
            continue
        grants.append(
            RoleGrant(
                principal_id=principal.id,
                role=rule.role,
                scope=rule.scope,
                granted_by=granted_by,
                reason=f"member of {group} at {identity.issuer}",
                granted_at=now,
            )
        )
    return tuple(grants)


#: Names that would mean a claim carries access. Checked against the return annotation of a
#: function that reads claims, so the rule survives a refactor by whoever needs it not to.
#: Matched on the annotation rather than the body: a body check is removable by the person
#: adding the feature, a signature change is not.
CAPABILITY_SHAPED: Final[tuple[str, ...]] = (
    "Capability",
    "Grant",
    "EntitlementSet",
    "PackAssignment",
)

#: Named so a reader of the invariant suite finds the sentence and not only the assertion.
CLAIMS_NEVER_GRANT: Final = (
    "A claim from the identity provider maps to a platform role and to nothing else. "
    "Capabilities come from grants written in this system, which somebody here reviews."
)


def _capability_shaped_in(annotation: object, depth: int = 1) -> set[str]:
    """Capability-shaped names reachable from an annotation, one level into its fields.

    One level rather than zero because the way this rule actually gets broken is not
    somebody changing a return type to `tuple[Grant, ...]`, which no reviewer would miss.
    It is somebody adding a `capabilities` field to the result type in a hurry, leaving the
    signature reading `-> SyncedRoles` exactly as before.
    """
    reduced = str(annotation).replace("RoleGrant", "")
    found = {name for name in CAPABILITY_SHAPED if name in reduced}
    if depth <= 0 or not isinstance(annotation, type):
        return found

    inner: list[object] = []
    model_fields = getattr(annotation, "model_fields", None)
    if isinstance(model_fields, Mapping):
        inner = [getattr(field, "annotation", None) for field in model_fields.values()]
    elif is_dataclass(annotation):
        inner = [field.type for field in dataclass_fields(annotation)]
    for item in inner:
        found |= _capability_shaped_in(item, depth - 1)
    return found


def assert_no_capability_from_claims(fn: Callable[..., object]) -> None:
    """Refuse a claim-reading function that could return anything capability-shaped (M1.1.5).

    `RoleGrant` is removed before the check, even though it contains the word Grant. A role
    grant confers a role, and `brain.identity.roles` already guarantees that no role implies
    a capability, including Super Admin; what is refused here is a function that could hand
    back a `Grant`, a `Capability` or an `EntitlementSet` assembled from what a directory
    said about somebody.

    `eval_str=True` so the annotation is the real class rather than the string
    `from __future__ import annotations` leaves behind, which is what makes the one-level
    field walk possible at all.
    """
    annotation = inspect.signature(fn, eval_str=True).return_annotation
    offending = sorted(_capability_shaped_in(annotation))
    if offending:
        msg = (
            f"{fn.__name__} returns {annotation}, which can carry {offending}; an "
            "identity-provider claim must never confer a capability, or the permission "
            "model moves into a directory nobody here reviews"
        )
        raise IdentityError(msg)


# ------------------------------------------------------------ SAML federation
class SamlFederation(BaseModel):
    """A client's SAML identity provider, brokered by Keycloak (M1.1.4).

    **This is configuration, not a protocol implementation, and the distinction is the whole
    reason the type exists.** We never speak SAML. Keycloak brokers the client's IdP, and
    what arrives here is still an OIDC token from our own realm, which is why every rule in
    this module applies unchanged to a federated user. Modelling the federation as config
    keeps the dangerous parts reviewable in the repository rather than in an admin console
    where the only record of a change is that somebody remembers making it.

    The three validated fields are the three that are always got wrong:

    - `want_assertions_signed` must hold. An unsigned assertion is a form post claiming to
      be a person, which is `alg: none` wearing different clothes.
    - `validate_signature` must hold, for the same reason.
    - `principal_attribute` becomes the subject. A transient NameID changes every session,
      so a federation configured that way makes a new user on every login and
      `principal_for` never recognises anybody twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(min_length=2, max_length=60)
    entity_id: str = Field(min_length=1, max_length=500)
    sso_url: str = Field(min_length=1, max_length=500)
    #: The client's signing certificate as configured in Keycloak. Public material.
    signing_certificate: str = Field(min_length=1)
    principal_attribute: str = Field(min_length=1, max_length=120)
    want_assertions_signed: bool = True
    validate_signature: bool = True
    #: Attribute name in the assertion, to claim name in the token Keycloak then mints.
    attribute_to_claim: Mapping[str, str] = MappingProxyType({})

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.want_assertions_signed or not self.validate_signature:
            msg = (
                f"federation {self.alias!r} would accept an unsigned or unchecked assertion; "
                "that is a form post claiming to be a person"
            )
            raise ValueError(msg)
        if not self.sso_url.startswith("https://"):
            msg = f"federation {self.alias!r} would post assertions over an insecure transport"
            raise ValueError(msg)
        if "transient" in self.principal_attribute.lower():
            msg = (
                f"federation {self.alias!r} identifies people by a transient value, which "
                "changes every session; nobody would ever be recognised twice"
            )
            raise ValueError(msg)
        return self

    def claims_produced(self) -> frozenset[str]:
        """The claim names this federation can put into a token. For review, and for checking
        that a `ClaimMapping` reads something the federation actually sends."""
        return frozenset(self.attribute_to_claim.values())


def federation_gaps(federation: SamlFederation, mapping: ClaimMapping) -> tuple[str, ...]:
    """Claims the mapping reads that this federation never sends.

    A report rather than a refusal. A missing `department` is normal at a client who does not
    record one, and refusing the federation over it would stop everybody signing in; but a
    missing claim also looks exactly like a working configuration until somebody asks why
    every federated user has no department, so it is worth saying out loud once.
    """
    produced = federation.claims_produced()
    wanted = {
        mapping.display_name_claim,
        mapping.department_claim,
        mapping.groups_claim,
        mapping.email_claim,
    }
    return tuple(sorted(name for name in wanted if name not in produced))
