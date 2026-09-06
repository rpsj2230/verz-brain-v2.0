"""Turning an `Authorization` header into somebody the gate can compute a reach for.

`brain.identity.oidc` is a set of pure functions over a token that somebody else has to
hand it. Until this module there was nobody: `validate_token` had never been called by
anything that serves a request, `principal_for` had never been called at all outside a
test, and `brain.openapi.DOCUMENTED_BEFORE_IT_IS_ENFORCED` said so in as many words. A
validator nothing calls is the same shape as the timeout middleware nothing mounted, and
this repository has now found that shape nine times.

**Nothing here re-decides anything `oidc` already decides.** The order of checks, the
algorithm allow-list, the `kid` lookup, the issuer comparison, the audience rule, the
expiry and the not-before all live in `validate_token`, and this module's whole job is to
call it with the right arguments and to turn the two refusals it can produce into one
answer. A second opinion about any of those would be a second place for the wrong one to
win, which is what `CLAUDE.md` says about the intersection and is just as true here.

**An unconfigured authority accepts nothing, and that is the deployed state today.** The
signature check is an injected callback because the standard library cannot verify RS256
and `oidc` refuses to invent one. This install has no verifier wired, so
`brain.app.create_app` attaches no authority and every request under the versioned prefix
is refused. That is the correct behaviour and it is stated rather than left to be
discovered: fail-closed is what a missing authenticator has to mean, and the alternative,
letting a request through when nothing could check it, is the bug that this shape makes
unrepresentable. See `AN_UNCONFIGURED_AUTHORITY_ACCEPTS_NOTHING`.

**Every refusal says one sentence.** `oidc.SIGN_IN_PROMPT` already argues why: "unknown
key" and "bad signature" and "wrong audience" tell somebody forging a token which part to
fix next, and the difference is invisible in a screenshot. The reason goes to the log as a
closed enumeration and never into the response.

**The assurance level is read from the token rather than assumed.** A password-only sign-in
and one carrying a second factor are not the same evidence, and `gate.admission` already
turns that difference into a ceiling: an `AUTHENTICATED` caller cannot exercise an `admin:`
or `approve:` capability however many grants they hold. Assuming `STRONG` would make that
ceiling decorative, and assuming `AUTHENTICATED` unconditionally would make a step-up flow
unbuildable without editing this file. So `amr` is read, against a deliberately short list
of values this realm can actually mint. See `A_SECOND_FACTOR_IS_A_CLAIM_ABOUT_THIS_SESSION`.

Rejected: reading `acr` and comparing it against a configured level of assurance. It is the
more standard mechanism and it is a number whose meaning is defined in the realm's own
authentication flow, so the comparison would be correct only for as long as nobody edited
that flow, and being wrong in the permissive direction is silent.

Rejected: middleware that authenticates every request and attaches a principal to the
request state. It reads better at each route and it fails open in the one case that
matters: a route mounted outside whatever path prefix the middleware matched on is a route
with no authentication, and nothing about it looks different. A dependency is named at each
route, so a route without one is visible in the diff that adds it, and
`tests/unit/test_api_routes.py` asserts over the mounted set rather than over a habit.

Scope: no network call is made here. The key set arrives through a `KeySource` the caller
supplies, which is what `oidc.JwksCache` already is.

Task ids: M1.1.2
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

import structlog

from brain.core.principal import Principal
from brain.gate.admission import Assurance
from brain.identity.oidc import (
    DEFAULT_LEEWAY,
    KeySet,
    PrincipalDirectory,
    SignatureVerifier,
    SigningKey,
    TokenRefusal,
    TokenRefusedError,
    UnmappedSubject,
    VerifiedClaims,
    parse_unverified,
    principal_for,
    validate_token,
)

log = structlog.get_logger()


# ------------------------------------------------------------ written-down reasons

#: Why a missing authority refuses rather than waves a request through.
AN_UNCONFIGURED_AUTHORITY_ACCEPTS_NOTHING: Final = (
    "A request cannot be authenticated by a component that is not there. The tempting "
    "reading of a missing authority is 'authentication is switched off in this "
    "environment', which is the same code path in every environment and is decided by "
    "whether a variable happened to be set. So there is no branch here that produces a "
    "caller without a verified token: an absent authority is a refusal, and it is the "
    "same refusal a bad token gets, because a caller has no business learning which of "
    "the two happened."
)

#: Why the refusal never says which check failed.
EVERY_REFUSAL_SAYS_THE_SAME_SENTENCE: Final = (
    "Unknown key, bad signature, wrong audience and an expired token are one sentence to "
    "the presenter and four values in the log. Distinguishing them tells somebody forging "
    "a token which part to fix next, one attempt at a time, and the difference is "
    "invisible in a screenshot of the screen a real person is looking at."
)

#: Why the assurance level is read from the token rather than fixed.
A_SECOND_FACTOR_IS_A_CLAIM_ABOUT_THIS_SESSION: Final = (
    "Assurance is about now, not about the account. A token whose `amr` names a second "
    "factor is evidence that one was presented in the session this token came from; a "
    "token that names only a password is not. Fixing the level at AUTHENTICATED would "
    "make a step-up flow unreachable, and fixing it at STRONG would hand every "
    "password-only session the approve and admin verbs that gate.admission exists to "
    "withhold from them."
)


# --------------------------------------------------------------------- the pieces

#: `amr` values that count as a second factor actually presented.
#:
#: Deliberately short. RFC 8176 registers around twenty methods and most of them are ways of
#: describing a first factor: `pwd`, `pin` and `user` are all somebody typing a secret they
#: know. Widening this list would raise the assurance of a session on the strength of a value
#: this realm has never been observed to mint, and the failure is silent and permissive.
#:
#: `mfa` is the one Keycloak emits when its browser flow completed more than one factor, and
#: `otp` is the one its OTP form contributes. `hwk` is included because a hardware key is the
#: factor this company would move to next and it is a second factor by definition.
SECOND_FACTOR_METHODS: Final[frozenset[str]] = frozenset({"mfa", "otp", "hwk"})

#: The scheme the `Authorization` header must name, compared case-insensitively because the
#: header's scheme token is case-insensitive by RFC 7235 and a client that sends `bearer` is
#: correct.
BEARER_PREFIX: Final = "bearer"


class KeySource(Protocol):
    """Where the issuer's current signing keys come from.

    A protocol rather than a `KeySet` so that key rotation is somebody's job rather than a
    restart. `oidc.JwksCache` satisfies it, including the rate-limited refetch when an
    unrecognised `kid` appears mid-window.
    """

    def keys_for(self, issuer: str, now: datetime) -> KeySet: ...

    def key_for(self, issuer: str, kid: str, now: datetime) -> SigningKey: ...


@dataclass(frozen=True)
class Caller:
    """A verified person, and how strongly we know it is them, right now.

    Three fields and no entitlement. Reach is resolved per request from grants this company
    writes, and carrying a set on the caller would be an invitation to compute it once at
    sign-in and reuse it, which is how a revocation stops taking effect until somebody logs
    out.

    `claims` is kept because an audit entry that says "verified by kid abc123 at 09:14" can
    be argued with and "trusted" cannot, and because the session id is what a logout has to
    match against.
    """

    principal: Principal
    claims: VerifiedClaims
    assurance: Assurance

    @property
    def principal_id(self) -> str:
        return self.principal.id


def assurance_from(claims: VerifiedClaims) -> Assurance:
    """How strongly this token's own session authenticated the person (M3.3.4 input).

    `AUTHENTICATED` is the floor rather than the default, and the distinction matters: this
    function is only ever reached with claims that `validate_token` has already accepted, so
    there is a live, signed, in-date credential from the right issuer for the right audience.
    That is an authenticated session by definition. What is being decided here is only
    whether to go one rung higher.

    An `amr` that is absent, empty, or not a list of strings leaves the floor, and does not
    raise: Keycloak emits `amr` only when the flow was configured to, so its absence is the
    ordinary case at a client who has not turned on a second factor, and refusing the token
    over it would stop everybody signing in to gain nothing.
    """
    raw = claims.claim("amr")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        # A string `amr` is refused rather than split. A single-valued claim spelled as a
        # string is a real shape, and splitting it would mean choosing a separator, which is
        # a guess that decides whether somebody holds the approve verb.
        return Assurance.AUTHENTICATED
    methods = {value for value in raw if isinstance(value, str)}
    if methods & SECOND_FACTOR_METHODS:
        return Assurance.STRONG
    return Assurance.AUTHENTICATED


def token_from_header(header: str | None) -> str:
    """The compact token out of an `Authorization` header, or a refusal.

    Refuses rather than returning None, for the reason `oidc.JwksCache.key_for` refuses: a
    caller holding an optional token is one `if token is None: token = ""` away from handing
    an empty string to the parser, and an empty string is a malformed token rather than an
    absent credential. Here the two are the same refusal anyway, which is the point.
    """
    if not header:
        raise TokenRefusedError(TokenRefusal.MALFORMED, "no authorization header")
    scheme, _, rest = header.partition(" ")
    if scheme.strip().lower() != BEARER_PREFIX:
        # The scheme is named in the log and not in the response. A caller sending Basic
        # authentication has made a mistake worth telling an operator about, and telling the
        # sender that Bearer is the accepted scheme is already in the published document.
        raise TokenRefusedError(TokenRefusal.MALFORMED, f"scheme {scheme.strip()!r} is not bearer")
    value = rest.strip()
    if not value:
        raise TokenRefusedError(TokenRefusal.MALFORMED, "bearer scheme with no token")
    return value


@dataclass(frozen=True)
class TokenAuthority:
    """Everything needed to turn a header into a caller, held once per process.

    One object rather than four arguments threaded through the routes, because the four have
    to agree: a key source for one issuer and an `expected_issuer` naming another is a
    configuration that accepts nothing and explains itself as a bad signature. Holding them
    together means the mismatch is visible where the thing is built.

    `verify` is injected and there is no default. `oidc.SignatureVerifier` explains why at
    length: the standard library cannot verify RS256, and a verifier written here would be
    the worst thing in the repository. What this type adds is that the absence is now
    load-bearing rather than theoretical, because a route asks for one of these and there is
    nowhere else to get a caller from.
    """

    issuer: str
    audience: str
    keys: KeySource
    verify: SignatureVerifier
    directory: PrincipalDirectory
    leeway: timedelta = DEFAULT_LEEWAY

    def authenticate(self, header: str | None, *, now: datetime) -> Caller:
        """A verified caller, or `TokenRefusedError`. Never anything in between.

        The `kid` is read off the unverified header before validation, and only to give the
        key source a chance to refetch after a rotation. Nothing is decided by that read:
        `validate_token` reads the same field again, checks it against the allow-list, looks
        it up in the key set it is handed, and refuses if the two disagree. Warming a cache
        with an attacker-controlled string is safe precisely because the value is re-derived
        and re-checked on the path that matters, and skipping the warm would mean a rotated
        key refusing every sign-in in the company until a TTL expired.
        """
        raw = parse_unverified(token_from_header(header))

        kid = raw.header.get("kid")
        if isinstance(kid, str) and kid:
            self.keys.key_for(self.issuer, kid, now)

        claims = validate_token(
            raw,
            keys=self.keys.keys_for(self.issuer, now),
            verify=self.verify,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
            now=now,
            leeway=self.leeway,
        )

        found = principal_for(claims, self.directory, now=now)
        if isinstance(found, UnmappedSubject):
            # A valid token from somebody with no live principal here. `oidc` argues why this
            # is not an empty `Principal`: an empty one type-checks everywhere a real one
            # does, flows into the gate, resolves to an empty reach, and produces a confident
            # "I could not find that" for a person who should have been told to ask an
            # administrator. The subject and issuer are recorded because an operator asked
            # "why can Priya not sign in" cannot answer without them, and because a Keycloak
            # `sub` is an opaque uuid rather than a phone number.
            raise TokenRefusedError(
                TokenRefusal.NO_PRINCIPAL, f"{claims.subject} at {claims.issuer}"
            )

        return Caller(principal=found, claims=claims, assurance=assurance_from(claims))


def authenticate(authority: TokenAuthority | None, header: str | None, *, now: datetime) -> Caller:
    """The only way a request becomes a caller, including when nothing is configured.

    The `None` case is handled here rather than at each route, so that "this deployment has
    no authority" and "this token is not acceptable" are one refusal produced by one line.
    Handled at the route it would be a branch per route, and the route that forgot it would
    be a route serving unauthenticated traffic while every other one refused.

    See `AN_UNCONFIGURED_AUTHORITY_ACCEPTS_NOTHING`.
    """
    if authority is None:
        raise TokenRefusedError(
            TokenRefusal.NO_KEYS_AVAILABLE, "no token authority is configured on this process"
        )
    return authority.authenticate(header, now=now)


def log_refusal(error: TokenRefusedError, *, path: str) -> None:
    """One log line per refused credential, with the reason and never the token.

    The token is deliberately absent, including a prefix of it. A bearer token is a
    credential for as long as it is valid, and a log store is read by more people and kept
    for longer than the ten minutes an access token lives. The `kid` and the subject are
    equally absent, because both come from a token nothing has verified and recording an
    unverified claim as though it were a fact is how a log becomes evidence of something
    that did not happen.
    """
    log.warning("credential refused", reason=str(error.reason), path=path)


def refusal_headers() -> Mapping[str, str]:
    """What a 401 carries so a client knows what to present.

    `Bearer` and the realm are omitted deliberately: the realm parameter is a free-text
    label that installations fill in with the identity provider's URL, and a refusal is not
    the place to publish where the identity provider lives to somebody who has not
    authenticated. The scheme alone is what a client actually needs.
    """
    return {"www-authenticate": "Bearer"}
