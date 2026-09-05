"""The mechanics of `brain.identity.oidc` and `brain.identity.sessions`.

The rules that must never break live next door in
`tests/invariants/test_oidc_invariants.py`. What is here is the behaviour underneath them:
every refusal reachable from a malformed or hostile token, the key cache and its rotation
path, the claim mapping, group sync, and the session and service-account lifecycles.

Nothing here contacts a Keycloak. The signature check is the injected verifier the module
declares as a seam, and the "signature" is a marker naming the key that produced it, so a
token verified with the wrong key fails for the same reason a real one would.

Task ids: M1.1.1, M1.1.2, M1.1.3, M1.1.4, M1.1.5, M1.1.6, M1.1.7
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.scope import Scope
from brain.gate.admission import Assurance
from brain.identity.oidc import (
    MAX_LEEWAY,
    ClaimMapping,
    GroupRoleRule,
    JwksCache,
    KeySet,
    MappedIdentity,
    RawToken,
    SamlFederation,
    SigningKey,
    TokenRefusal,
    TokenRefusedError,
    UnmappedSubject,
    VerifiedClaims,
    federation_gaps,
    map_claims,
    parse_unverified,
    principal_for,
    role_grants_from_groups,
    roles_from_groups,
    validate_token,
)
from brain.identity.roles import IdentityError, NoStandingEntitlement, Role
from brain.identity.sessions import (
    SESSION_ABSOLUTE_MAX,
    SESSION_IDLE,
    ServiceAccount,
    Session,
    SessionRegistry,
    authenticate_service_account,
    open_session,
    reach_for,
)

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
ISSUER = "https://id.verz.example/realms/brain"
AUDIENCE = "brain-api"
KID = "k1"
SUBJECT = "1f2e3d4c-0000-4000-8000-000000000001"


# ------------------------------------------------------------------- fixtures
def signing_key(kid: str = KID, algorithm: str = "RS256", use: str = "sig") -> SigningKey:
    return SigningKey(kid=kid, algorithm=algorithm, material=f"-----PUBLIC {kid}-----", use=use)


def key_set(*keys: SigningKey, fetched_at: datetime = NOW) -> KeySet:
    return KeySet(issuer=ISSUER, keys=keys or (signing_key(),), fetched_at=fetched_at)


def marker_for(kid: str) -> bytes:
    """The stand-in for a signature. Naming the key is what makes a wrong key fail."""
    return b"signed-by:" + kid.encode()


def verifier(*, signing_input: bytes, signature: bytes, key: SigningKey) -> bool:
    assert signing_input, "the signing input must be the bytes that were signed"
    return signature == marker_for(key.kid)


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "typ": "Bearer",
        "sid": "sess-1",
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        "iat": int(NOW.timestamp()),
        "name": "Priya Menon",
        "department": "web",
        "email": "priya@example.com",
        "groups": ["/brain/member"],
    }
    base.update(overrides)
    return base


def raw(
    *, kid: str = KID, alg: str = "RS256", signed_by: str | None = None, **claims: object
) -> RawToken:
    header: dict[str, object] = {"alg": alg, "typ": "JWT"}
    if kid:
        header["kid"] = kid
    return RawToken(
        header=header,
        payload=payload(**claims),
        signing_input=b"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0",
        signature=marker_for(signed_by or kid),
    )


def check(
    token: RawToken, *, keys: KeySet | None = None, now: datetime = NOW, **kwargs: object
) -> VerifiedClaims:
    return validate_token(
        token,
        keys=keys or key_set(),
        verify=verifier,
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        now=now,
        **kwargs,  # type: ignore[arg-type]
    )


def person(
    pid: str = "u_priya",
    employment: Employment = Employment.STAFF,
    not_after: datetime | None = None,
) -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=employment,
        display_name="Priya Menon",
        primary_department="web",
        not_after=not_after,
    )


class Directory:
    """A `PrincipalDirectory` over a dictionary. The real one reads `principal_identity`."""

    def __init__(self, people: dict[tuple[str, str], Principal]) -> None:
        self._people = people

    def principal_for_subject(self, issuer: str, subject: str) -> Principal | None:
        return self._people.get((issuer, subject))


def compact(header: dict[str, object], claims: dict[str, object], signature: bytes) -> str:
    def seg(blob: bytes) -> str:
        return base64.urlsafe_b64encode(blob).decode().rstrip("=")

    return ".".join(
        (seg(json.dumps(header).encode()), seg(json.dumps(claims).encode()), seg(signature))
    )


# ------------------------------------------------------------------ parsing
def test_a_compact_token_splits_into_a_header_a_payload_and_a_signature():
    """Delete this and every caller writes their own base64 split, which is the version that
    forgets the segment count and hands a JWE to the validator as though it were a JWS."""
    wire = compact({"alg": "RS256", "kid": KID}, payload(), marker_for(KID))
    token = parse_unverified(wire)

    assert token.header["kid"] == KID
    assert token.payload["sub"] == SUBJECT
    assert token.signature == marker_for(KID)
    # The signed bytes are the ones that arrived, never a re-encoding: JSON serialisation
    # is not canonical, so re-encoding changes what was signed.
    assert token.signing_input == wire.rsplit(".", 1)[0].encode()


def test_a_token_with_five_segments_is_refused():
    """Five segments is a JWE. Without this it would be split into three by a lenient parser
    and its ciphertext read as a payload, which is nonsense that reaches the validator."""
    with pytest.raises(TokenRefusedError) as caught:
        parse_unverified("a.b.c.d.e")
    assert caught.value.reason is TokenRefusal.MALFORMED


def test_a_token_whose_payload_is_not_json_is_refused():
    """Without it, a payload of arbitrary bytes reaches `validate_token` and every claim
    lookup returns None, which is a token with no issuer rather than a malformed one."""
    wire = compact({"alg": "RS256", "kid": KID}, payload(), marker_for(KID))
    head, _, sig = wire.split(".")
    with pytest.raises(TokenRefusedError):
        parse_unverified(f"{head}.bm90LWpzb24.{sig}")


# --------------------------------------------------------------- validation
def test_a_well_formed_token_from_the_right_issuer_and_audience_verifies():
    """The happy path. If this breaks, everything below is passing for the wrong reason."""
    claims = check(raw())

    assert claims.subject == SUBJECT
    assert claims.issuer == ISSUER
    assert claims.audience == (AUDIENCE,)
    assert claims.session_id == "sess-1"
    assert claims.issued_at == NOW


def test_the_verified_claims_record_which_key_checked_them():
    """An audit entry saying "verified by kid k1 at 09:00" can be argued with; one saying
    "trusted" cannot. Delete this and the provenance quietly stops being carried."""
    claims = check(raw())

    assert claims.key_id == KID
    assert claims.algorithm == "RS256"
    assert claims.verified_at == NOW


def test_a_token_declaring_alg_none_is_refused():
    """The canonical attack: strip the signature, set alg to none, keep the claims. Delete
    this and an unsigned token is a valid one for anybody who can post it."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(alg="none"))
    assert caught.value.reason is TokenRefusal.ALG_NONE


def test_a_symmetric_algorithm_cannot_even_be_published_as_a_key():
    """The other end of algorithm confusion. If an HS256 key could be constructed, an RSA
    public key could be published as a shared secret and every forged token would verify."""
    with pytest.raises(ValidationError, match="allow-list"):
        signing_key(algorithm="HS256")


def test_a_token_asking_for_hs256_is_refused_before_any_key_is_looked_up():
    """Delete this and the refusal depends on no HS256 key happening to be in the set, which
    is a property of the identity provider's configuration rather than of this code."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(alg="HS256"))
    assert caught.value.reason is TokenRefusal.ALG_NOT_ALLOWED


def test_a_token_naming_a_key_published_for_another_algorithm_is_refused():
    """Confusion caught even when the `kid` is real: the token asks for ES256 and names an
    RS256 key. Without this, the key's own published algorithm is decoration."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(alg="ES256"))
    assert caught.value.reason is TokenRefusal.ALG_MISMATCH


def test_a_token_that_names_no_key_is_refused():
    """The alternative is trying every key on file, which gives one free attempt per key,
    including every key retired but not yet removed."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(kid=""))
    assert caught.value.reason is TokenRefusal.NO_KEY_ID


def test_a_token_signed_with_an_unknown_key_is_refused():
    """No fallback to "the only key" or "the newest key". Delete this and a rotated-out key
    id, or an invented one, gets tried against whatever happens to be in the set."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(kid="k-not-ours"))
    assert caught.value.reason is TokenRefusal.UNKNOWN_KEY


def test_a_key_published_for_encryption_never_verifies_a_signature():
    """JWKS `use` exists precisely to keep these apart, and a key set commonly carries both.
    Without the check, an encryption key is a signing key with a different label."""
    keys = key_set(signing_key(use="enc"))
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(), keys=keys)
    assert caught.value.reason is TokenRefusal.KEY_NOT_FOR_SIGNING


def test_a_token_whose_signature_does_not_check_out_is_refused():
    """The whole point. A token signed by the wrong key, or edited after signing, must not
    become an identity because its claims happen to read correctly."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(signed_by="somebody-else"))
    assert caught.value.reason is TokenRefusal.BAD_SIGNATURE


def test_a_token_from_another_issuer_is_refused():
    """A correctly signed token from a realm we do not accept is still not ours. Without the
    check, any Keycloak whose keys we happen to hold can mint identities here."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(iss="https://id.other.example/realms/brain"))
    assert caught.value.reason is TokenRefusal.WRONG_ISSUER


def test_an_issuer_that_merely_starts_the_same_way_is_refused():
    """Exact equality, never a prefix or a host comparison. This is the shape of the bug that
    a forgiving comparison written to tolerate a trailing slash introduces."""
    with pytest.raises(TokenRefusedError):
        check(raw(iss=ISSUER + ".attacker.example"))
    with pytest.raises(TokenRefusedError):
        check(raw(iss=ISSUER + "/"))


def test_a_token_minted_for_another_audience_is_refused():
    """The confused deputy in one line: a token the user legitimately holds for a different
    service must not authenticate them here."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(aud="some-other-service"))
    assert caught.value.reason is TokenRefusal.WRONG_AUDIENCE


def test_a_multi_audience_token_must_name_us_as_the_authorised_party():
    """OIDC's own rule. Several audiences means several parties could present it, and `azp`
    is the only claim saying which one asked for it."""
    accepted = check(raw(aud=[AUDIENCE, "reporting"], azp=AUDIENCE))
    assert accepted.audience == (AUDIENCE, "reporting")

    with pytest.raises(TokenRefusedError) as caught:
        check(raw(aud=[AUDIENCE, "reporting"], azp="reporting"))
    assert caught.value.reason is TokenRefusal.WRONG_AUDIENCE


def test_an_expired_token_is_refused():
    """Without it the access-token lifetime is a number in a realm export with no effect."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(), now=NOW + timedelta(minutes=10))
    assert caught.value.reason is TokenRefusal.EXPIRED


def test_a_token_just_past_expiry_is_accepted_inside_the_leeway_and_not_beyond_it():
    """Both halves matter. Leeway that does not apply makes a badly synchronised clock look
    like an attack; leeway without a bound makes expiry decorative."""
    just_past = NOW + timedelta(minutes=5, seconds=10)
    assert check(raw(), now=just_past).subject == SUBJECT

    with pytest.raises(TokenRefusedError):
        check(raw(), now=NOW + timedelta(minutes=5) + MAX_LEEWAY + timedelta(seconds=1))


def test_a_token_that_is_not_yet_valid_is_refused():
    """`nbf` is how an identity provider says "not before". Ignoring it accepts a token that
    was minted for a window that has not started."""
    future = int((NOW + timedelta(minutes=30)).timestamp())
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(nbf=future))
    assert caught.value.reason is TokenRefusal.NOT_YET_VALID


def test_a_token_issued_in_the_future_is_refused():
    """An `iat` ahead of our clock is either a broken IdP or a token built to sit above a
    logout floor forever. Both must fail rather than being taken at face value."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(iat=int((NOW + timedelta(hours=2)).timestamp())))
    assert caught.value.reason is TokenRefusal.ISSUED_IN_FUTURE


def test_a_refresh_token_presented_as_an_access_token_is_refused():
    """A refresh token lives as long as the session. Accepting one where an access token
    belongs turns a five-minute credential into a ten-hour one."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(typ="Refresh"))
    assert caught.value.reason is TokenRefusal.WRONG_TYPE


def test_a_boolean_expiry_is_refused_rather_than_read_as_1970():
    """`isinstance(True, int)` is True in Python, so `exp: true` silently becomes 1 January
    1970 and reads as an ordinary expired token. The two are logged very differently."""
    with pytest.raises(TokenRefusedError) as caught:
        check(raw(exp=True))
    assert caught.value.reason is TokenRefusal.MISSING_CLAIM


def test_a_token_with_no_issue_time_is_refused():
    """Logout propagation is a floor over `iat`. A token without one cannot be placed either
    side of a logout, so accepting it would exempt it from revocation entirely."""
    claims = payload()
    del claims["iat"]
    token = RawToken(
        header={"alg": "RS256", "kid": KID},
        payload=claims,
        signing_input=b"h.p",
        signature=marker_for(KID),
    )
    with pytest.raises(TokenRefusedError) as caught:
        check(token)
    assert caught.value.reason is TokenRefusal.MISSING_CLAIM


def test_asking_for_more_skew_than_the_maximum_is_a_programming_error():
    """It raises `IdentityError` and not a token refusal, because nothing is wrong with the
    token. Delete this and a caller can switch expiry off by passing an hour."""
    with pytest.raises(IdentityError, match="clock skew"):
        check(raw(), leeway=timedelta(hours=1))
    with pytest.raises(IdentityError, match="clock skew"):
        check(raw(), leeway=timedelta(seconds=-1))


# ------------------------------------------------------------- JWKS caching
class Fetcher:
    """A `JwksFetch` that counts calls and can be made to fail."""

    def __init__(self, keys: tuple[SigningKey, ...], fetched_at: datetime = NOW) -> None:
        self.keys = keys
        self.fetched_at = fetched_at
        self.calls = 0
        self.broken = False

    def __call__(self, issuer: str) -> KeySet:
        self.calls += 1
        if self.broken:
            msg = "connection refused"
            raise RuntimeError(msg)
        return KeySet(issuer=issuer, keys=self.keys, fetched_at=self.fetched_at)


def test_the_cache_fetches_once_and_then_serves_from_memory():
    """A JWKS fetch on every request is an outage waiting for the identity provider to have
    a slow minute, and a rate limit waiting to be hit."""
    fetch = Fetcher((signing_key(),))
    cache = JwksCache(fetch)

    cache.keys_for(ISSUER, NOW)
    cache.keys_for(ISSUER, NOW + timedelta(minutes=5))
    assert fetch.calls == 1


def test_a_cache_entry_older_than_its_ttl_is_refetched():
    """Without a TTL, a key retired because it leaked stays usable here until the process
    restarts, which on a healthy deployment could be weeks."""
    fetch = Fetcher((signing_key(),))
    cache = JwksCache(fetch)

    cache.keys_for(ISSUER, NOW)
    cache.keys_for(ISSUER, NOW + timedelta(hours=2))
    assert fetch.calls == 2


def test_an_unrecognised_key_id_triggers_exactly_one_refetch():
    """This is what makes a routine key rotation invisible to users instead of an outage
    lasting until the TTL expires."""
    fetch = Fetcher((signing_key(),))
    cache = JwksCache(fetch)
    cache.keys_for(ISSUER, NOW)

    fetch.keys = (signing_key(), signing_key(kid="k2"))
    found = cache.key_for(ISSUER, "k2", NOW + timedelta(minutes=1))

    assert found.kid == "k2"
    assert fetch.calls == 2


def test_a_flood_of_unknown_key_ids_does_not_hammer_the_identity_provider():
    """Without the rate limit, anybody who can post a token can make this process fetch the
    JWKS as fast as it will go, which is a denial of service using our own credentials."""
    fetch = Fetcher((signing_key(),))
    cache = JwksCache(fetch)
    cache.keys_for(ISSUER, NOW)

    first_guess = NOW + timedelta(minutes=1)
    with pytest.raises(TokenRefusedError):
        cache.key_for(ISSUER, "guess-0", first_guess)
    assert fetch.calls == 2

    for offset in range(1, 6):
        with pytest.raises(TokenRefusedError):
            cache.key_for(ISSUER, f"guess-{offset}", first_guess + timedelta(seconds=offset))
    assert fetch.calls == 2


def test_a_stale_key_set_is_served_while_the_provider_is_unreachable():
    """Public keys do not become secret. Refusing every sign-in in the company because one
    HTTP call failed is a worse failure than serving a set that is an hour old."""
    fetch = Fetcher((signing_key(),))
    cache = JwksCache(fetch)
    cache.keys_for(ISSUER, NOW)
    fetch.broken = True

    served = cache.keys_for(ISSUER, NOW + timedelta(hours=2))
    assert served.by_kid(KID) is not None

    with pytest.raises(TokenRefusedError) as caught:
        cache.keys_for(ISSUER, NOW + timedelta(days=2))
    assert caught.value.reason is TokenRefusal.NO_KEYS_AVAILABLE


def test_a_provider_that_has_never_been_reached_refuses_rather_than_inventing_keys():
    """The dangerous default here is an empty key set, which reads as "no key matched" and
    would send every refusal down the unknown-key path instead of saying the IdP is down."""
    fetch = Fetcher((signing_key(),))
    fetch.broken = True
    with pytest.raises(TokenRefusedError) as caught:
        JwksCache(fetch).keys_for(ISSUER, NOW)
    assert caught.value.reason is TokenRefusal.NO_KEYS_AVAILABLE


def test_two_keys_published_under_one_id_are_refused():
    """Otherwise the choice between them is arbitrary, and an arbitrary choice inside a
    signature check is a coin flip an attacker gets to call by publishing a live `kid`."""
    with pytest.raises(ValueError, match="twice"):
        KeySet(issuer=ISSUER, keys=(signing_key(), signing_key()), fetched_at=NOW)


# ------------------------------------------------------------ claim mapping
def test_claims_map_onto_the_shape_the_principal_record_uses():
    """M1.1.3. Without a single mapping step, every consumer reads raw claim names and a
    federated client who spells `department` differently breaks each of them separately."""
    mapped = map_claims(check(raw()), ClaimMapping())

    assert mapped.subject == SUBJECT
    assert mapped.display_name == "Priya Menon"
    assert mapped.primary_department == "web"
    assert mapped.email == "priya@example.com"
    assert mapped.groups == ("/brain/member",)


def test_a_groups_claim_that_is_not_a_list_yields_no_groups():
    """A string `groups` claim is common at federated IdPs. Iterating it would produce one
    group per character, and `/brain/super-admin` contains every character in it."""
    assert map_claims(check(raw(groups="/brain/super-admin")), ClaimMapping()).groups == ()


def test_a_subject_the_directory_does_not_know_is_not_a_principal():
    """A real person with a valid token who has not been onboarded is a normal event. An
    empty `Principal` would flow onward and produce a confident "I could not find that"."""
    result = principal_for(check(raw()), Directory({}), now=NOW)

    assert isinstance(result, UnmappedSubject)
    assert result.subject == SUBJECT
    assert "administrator" in result.prompt


def test_a_principal_whose_time_has_run_out_is_unmapped_rather_than_inactive():
    """A leaver whose Keycloak account still works must not reach a code path holding a real
    principal object, even one that would fail later at entitlement time."""
    contractor = person(employment=Employment.CONTRACTOR, not_after=NOW - timedelta(days=1))
    directory = Directory({(ISSUER, SUBJECT): contractor})

    assert isinstance(principal_for(check(raw()), directory, now=NOW), UnmappedSubject)


def test_a_known_subject_resolves_to_the_principal_the_directory_holds():
    """The other half: the directory is the authority on who somebody is, and the token only
    says which record to look up."""
    directory = Directory({(ISSUER, SUBJECT): person()})
    found = principal_for(check(raw()), directory, now=NOW)

    assert isinstance(found, Principal)
    assert found.id == "u_priya"


# --------------------------------------------------------- group and role sync
def identity(*groups: str) -> MappedIdentity:
    return MappedIdentity(
        subject=SUBJECT,
        issuer=ISSUER,
        display_name="Priya Menon",
        primary_department="web",
        email="priya@example.com",
        groups=groups,
    )


def test_an_idp_group_maps_to_a_platform_role():
    """M1.1.5. Without it, every new joiner needs a role assigned by hand in two systems and
    the two disagree the first time somebody does only one of them."""
    rules = [GroupRoleRule(group="/brain/member", role=Role.MEMBER)]
    assert roles_from_groups(identity("/brain/member"), rules).roles == frozenset({Role.MEMBER})


def test_a_group_nobody_mapped_is_reported_rather_than_ignored():
    """A renamed group and a group that is none of our business look identical from here.
    Silence makes the first one present as "my permissions disappeared overnight"."""
    rules = [GroupRoleRule(group="/brain/member", role=Role.MEMBER)]
    synced = roles_from_groups(identity("/brain/member", "/finance/all"), rules)

    assert synced.unmapped_groups == ("/finance/all",)


def test_a_group_is_matched_exactly_and_never_by_prefix():
    """A prefix match would make `/brain/approver/web-archive` an approver for `web`, which
    is a scope widened by a group name nobody thought of as a permission."""
    rules = [GroupRoleRule(group="/brain/member", role=Role.MEMBER)]
    assert roles_from_groups(identity("/brain/member-readonly"), rules).roles == frozenset()


def test_a_scope_required_role_cannot_be_mapped_from_a_group_without_a_scope():
    """A `department_admin` grant with no scope reads as company-wide. Delete this and a
    group in a client's own directory can produce the widest row in the system."""
    with pytest.raises(ValidationError, match="needs a scope"):
        GroupRoleRule(group="/brain/department-admin", role=Role.DEPARTMENT_ADMIN)


def test_a_scope_that_restricts_nothing_does_not_satisfy_the_requirement():
    """The gap the scope requirement is actually about, and it was untested: removing this
    guard passed every other test in the file.

    `Scope.unrestricted()` is a scope by type and not by effect. A `department_admin` rule
    carrying one satisfies "this role needs a scope" while conferring exactly what that rule
    exists to prevent - company-wide admin - and it reads in review as somebody having done
    the right thing. The bare-scope refusal above does not catch it, because there *is* a
    scope."""
    with pytest.raises(ValidationError, match="restricts nothing"):
        GroupRoleRule(
            group="/brain/department-admin",
            role=Role.DEPARTMENT_ADMIN,
            scope=Scope.unrestricted(),
        )


def test_a_company_wide_role_cannot_carry_a_scope_from_a_group():
    """A scope that is written, stored and read by nobody is worse than none, because
    whoever wrote it believes they narrowed the grant."""
    with pytest.raises(ValidationError, match="read by nobody"):
        GroupRoleRule(group="/brain/auditor", role=Role.AUDITOR, scope=Scope.department("web"))


def test_group_membership_becomes_role_grants_that_say_a_directory_made_them():
    """A row that appeared with no human behind it has to say so. "Somebody appointed her"
    and "a directory did" are different facts, and only one of them can be reviewed."""
    rules = [
        GroupRoleRule(group="/brain/member", role=Role.MEMBER),
        GroupRoleRule(
            group="/brain/approver/web", role=Role.APPROVER, scope=Scope.department("web")
        ),
    ]
    grants = role_grants_from_groups(
        identity("/brain/member", "/brain/approver/web"), person(), rules, now=NOW
    )

    assert {g.role for g in grants} == {Role.MEMBER, Role.APPROVER}
    assert all(g.granted_by == f"idp:{ISSUER}" for g in grants)
    assert all("member of" in g.reason for g in grants)


# ------------------------------------------------------------ SAML federation
def federation(**overrides: object) -> SamlFederation:
    base: dict[str, object] = {
        "alias": "client-idp",
        "entity_id": "https://idp.client.example/metadata",
        "sso_url": "https://idp.client.example/sso",
        "signing_certificate": "MIIC-not-a-real-certificate",
        "principal_attribute": "employeeId",
        "attribute_to_claim": {"displayName": "name", "dept": "department"},
    }
    base.update(overrides)
    return SamlFederation(**base)  # type: ignore[arg-type]


def test_a_federation_that_would_accept_an_unsigned_assertion_is_refused():
    """M1.1.4. An unsigned assertion is a form post claiming to be a person: `alg: none` in
    a different protocol. The setting defaults to safe and must not be turnable off here."""
    with pytest.raises(ValidationError, match="unsigned"):
        federation(want_assertions_signed=False)
    with pytest.raises(ValidationError, match="unsigned"):
        federation(validate_signature=False)


def test_a_federation_keyed_on_a_transient_identifier_is_refused():
    """A transient NameID changes every session, so every login creates a new stranger and
    `principal_for` returns `UnmappedSubject` to the same person every morning."""
    with pytest.raises(ValidationError, match="transient"):
        federation(principal_attribute="transientNameId")


def test_a_federation_posting_assertions_over_plaintext_is_refused():
    """The assertion is the credential. Posting it over http means anybody on the path is
    that person for as long as the assertion is valid."""
    with pytest.raises(ValidationError, match="insecure transport"):
        federation(sso_url="http://idp.client.example/sso")


def test_a_claim_the_mapping_reads_but_the_federation_never_sends_is_reported():
    """A missing claim looks exactly like a working configuration until somebody asks why
    every federated user has no department. Reported rather than refused: it is common."""
    assert federation_gaps(federation(), ClaimMapping()) == ("email", "groups")


# ------------------------------------------------------------------ sessions
def open_one(*, second_factor: bool = False, principal: Principal | None = None) -> Session:
    return open_session(
        claims=check(raw()),
        principal=principal or person(),
        now=NOW,
        second_factor=second_factor,
    )


def test_a_session_opens_from_a_verified_token_and_carries_both_bounds():
    """M1.1.6. The two expiries answer different questions, and computing the second from
    the first at refresh time is how it quietly becomes the first."""
    session = open_one()

    assert session.expires_at == NOW + SESSION_IDLE
    assert session.absolute_expiry == NOW + SESSION_ABSOLUTE_MAX
    assert session.is_live(NOW)


def test_a_token_with_no_session_id_cannot_open_a_session():
    """That is a service-account token. Opening an interactive session for one would hand a
    robot the AUTHENTICATED ceiling the API channel is deliberately not given."""
    claims = check(raw(sid=None))
    with pytest.raises(TokenRefusedError) as caught:
        open_session(claims=claims, principal=person(), now=NOW)
    assert caught.value.reason is TokenRefusal.SESSION_UNKNOWN


def test_a_session_cannot_outlive_the_contractor_it_belongs_to():
    """Without this the session outlives the principal, and everything afterwards presents
    as an unexplained empty answer rather than as "your access ended"."""
    ends = NOW + timedelta(hours=2)
    contractor = person(employment=Employment.CONTRACTOR, not_after=ends)

    assert open_one(principal=contractor).absolute_expiry == ends


def test_a_live_session_admits_its_own_token():
    """The happy path for `admit`. If this breaks, every refusal test below passes for the
    wrong reason."""
    registry = SessionRegistry()
    registry.register(open_one())

    assert registry.admit(check(raw()), person(), NOW).session_id == "sess-1"


def test_a_token_naming_someone_elses_session_is_refused():
    """One comparison rules out the catastrophic case, the same check `gate.resolve` makes
    about a store returning the wrong row."""
    registry = SessionRegistry()
    registry.register(open_one())

    with pytest.raises(TokenRefusedError) as caught:
        registry.admit(check(raw()), person("u_someone_else"), NOW)
    assert caught.value.reason is TokenRefusal.SESSION_MISMATCH


def test_an_expired_session_is_removed_and_refused():
    """Delete this and an idle session lingers in memory as an admissible one until the
    process restarts, which makes the idle timeout a number nothing reads."""
    registry = SessionRegistry()
    registry.register(open_one())
    later = NOW + SESSION_IDLE + timedelta(minutes=1)

    with pytest.raises(TokenRefusedError) as caught:
        registry.admit(
            check(raw(exp=int((later + timedelta(minutes=5)).timestamp())), now=later),
            person(),
            later,
        )
    assert caught.value.reason is TokenRefusal.SESSION_EXPIRED
    assert registry.get("sess-1") is None


def test_refresh_slides_the_idle_window():
    """Without it a session dies thirty minutes after sign-in however busy the person is,
    and the first workaround anybody reaches for is a longer idle timeout."""
    registry = SessionRegistry()
    registry.register(open_one())
    later = NOW + timedelta(minutes=20)

    assert registry.refresh("sess-1", person(), later).expires_at == later + SESSION_IDLE


def test_refresh_can_never_push_a_session_past_its_absolute_expiry():
    """Sliding without a hard bound makes a sign-in from March still authenticate requests
    in September, which is the whole reason the absolute expiry exists."""
    hard_stop = NOW + SESSION_ABSOLUTE_MAX
    # A session refreshed all day, now a few minutes from its hard bound.
    long_lived = Session(
        session_id="sess-1",
        principal_id="u_priya",
        issuer=ISSUER,
        subject=SUBJECT,
        opened_at=NOW,
        expires_at=hard_stop - timedelta(minutes=2),
        absolute_expiry=hard_stop,
    )
    registry = SessionRegistry()
    registry.register(long_lived)

    refreshed = registry.refresh("sess-1", person(), hard_stop - timedelta(minutes=5))
    assert refreshed.expires_at == hard_stop

    with pytest.raises(TokenRefusedError) as caught:
        registry.refresh("sess-1", person(), hard_stop)
    assert caught.value.reason is TokenRefusal.SESSION_EXPIRED


def test_refreshing_a_session_for_somebody_who_has_left_is_refused():
    """Refresh is the moment a sign-in gets extended. Extending one for a leaver is how they
    keep working for another ten hours after their last day."""
    ends = NOW + timedelta(hours=1)
    contractor = person(employment=Employment.CONTRACTOR, not_after=ends)
    registry = SessionRegistry()
    registry.register(open_one(principal=contractor))

    with pytest.raises(TokenRefusedError):
        registry.refresh("sess-1", contractor, ends + timedelta(minutes=1))


def test_logout_ends_the_session_and_raises_the_floor():
    """Deleting the row alone changes nothing about a token already in somebody's hand. The
    floor is the half that does."""
    registry = SessionRegistry()
    registry.register(open_one())

    ended = registry.end_session("sess-1", NOW + timedelta(minutes=1))
    assert ended is not None
    assert registry.get("sess-1") is None
    assert registry.not_before_for("u_priya") == NOW + timedelta(minutes=1)


def test_a_logout_floor_never_moves_backwards():
    """A floor that can be lowered is one an out-of-order or replayed logout event undoes,
    re-admitting tokens that were already being refused."""
    registry = SessionRegistry()
    registry.end_all_for("u_priya", NOW + timedelta(minutes=5))
    registry.end_all_for("u_priya", NOW)

    assert registry.not_before_for("u_priya") == NOW + timedelta(minutes=5)


def test_floors_older_than_the_longest_possible_session_are_pruned():
    """Without pruning the map grows once per logout forever. The bound is what makes
    dropping one safe: every token it would refuse has expired on its own."""
    registry = SessionRegistry()
    registry.end_all_for("u_priya", NOW)

    assert registry.prune_floors(NOW + timedelta(hours=1)) == 0
    assert registry.prune_floors(NOW + SESSION_ABSOLUTE_MAX + timedelta(minutes=1)) == 1
    assert registry.not_before_for("u_priya") is None


def test_a_live_session_is_authenticated_and_a_second_factor_makes_it_strong():
    """These two values are what `brain.gate.admission` narrows every request by. If a
    password-only session returned STRONG, `approve` and `admin` would be reachable."""
    assert open_one().assurance(NOW) is Assurance.AUTHENTICATED
    assert open_one(second_factor=True).assurance(NOW) is Assurance.STRONG


def test_a_dead_session_is_worth_nothing_rather_than_worth_a_binding():
    """BOUND is what a channel binding proved separately. Returning it here would hand a
    read to somebody whose session ended, on the strength of it once having existed."""
    assert open_one().assurance(NOW + SESSION_ABSOLUTE_MAX) is Assurance.UNVERIFIED


# ---------------------------------------------------------- service accounts
def account(**overrides: object) -> ServiceAccount:
    base: dict[str, object] = {
        "client_id": "brain-sync",
        "subject": "service-account-subject",
        "owner_principal_id": "u_priya",
        "ceiling": (Capability(value="read:client.name"), Capability(value="read:hr.salary")),
        "not_after": NOW + timedelta(days=90),
    }
    base.update(overrides)
    return ServiceAccount(**base)  # type: ignore[arg-type]


def service_claims(azp: str = "brain-sync") -> VerifiedClaims:
    return check(raw(sid=None, sub="service-account-subject", azp=azp))


def test_a_service_account_token_authenticates_against_its_own_directory():
    """M1.1.7. The two paths are kept apart by two disjoint directories rather than by a
    flag in the token, so nothing in the token decides which check runs."""
    found = authenticate_service_account(
        service_claims(), {"service-account-subject": account()}, NOW
    )
    assert found.client_id == "brain-sync"


def test_a_persons_browser_token_cannot_act_as_a_service_account():
    """Otherwise a signed-in person picks up the service account's ceiling in addition to
    their own, which is exactly the union authority the model exists to prevent."""
    with pytest.raises(TokenRefusedError) as caught:
        authenticate_service_account(check(raw()), {SUBJECT: account()}, NOW)
    assert caught.value.reason is TokenRefusal.NOT_A_SERVICE_ACCOUNT


def test_a_token_whose_authorised_party_disagrees_with_the_account_is_refused():
    """If `azp` and `sub` point at different clients, one of the two mappings is wrong and
    neither is safe to guess at."""
    with pytest.raises(TokenRefusedError) as caught:
        authenticate_service_account(
            service_claims(azp="some-other-client"), {"service-account-subject": account()}, NOW
        )
    assert caught.value.reason is TokenRefusal.NOT_A_SERVICE_ACCOUNT


def test_an_expired_service_account_is_refused():
    """A service account created for one integration in 2026 and still working in 2031 with
    nobody able to say what uses it is the normal end state without a mandatory expiry."""
    with pytest.raises(TokenRefusedError) as caught:
        authenticate_service_account(
            service_claims(), {"service-account-subject": account()}, NOW + timedelta(days=365)
        )
    assert caught.value.reason is TokenRefusal.SERVICE_ACCOUNT_EXPIRED


def test_a_service_account_with_no_ceiling_cannot_be_written():
    """An empty ceiling either confers nothing, in which case write it out, or gets read as
    "unset" by somebody later, in which case it confers everything."""
    with pytest.raises(ValidationError, match="no ceiling"):
        account(ceiling=())


def test_a_service_account_holds_its_owners_reach_narrowed_by_its_ceiling():
    """The architecture's rule in one assertion: the account holds a capability only when
    the owner holds it and the ceiling admits it, and it keeps the owner's scope."""
    owner = person()
    held = EntitlementSet(
        principal_id="u_priya",
        grants=(
            Grant(capability=Capability(value="read:client.name"), scope=Scope.department("web")),
            Grant(capability=Capability(value="read:client.margin"), scope=Scope.department("web")),
        ),
    )
    reach = reach_for(account(), owner, held, NOW)

    assert isinstance(reach, EntitlementSet)
    assert reach.principal_id == "brain-sync"
    assert reach.holds(Capability(value="read:client.name"), NOW)
    # In the owner's grants but not in the ceiling.
    assert not reach.holds(Capability(value="read:client.margin"), NOW)
    # In the ceiling but not in the owner's grants: a ceiling never adds.
    assert not reach.holds(Capability(value="read:hr.salary"), NOW)
    assert reach.scope_for(Capability(value="read:client.name"), NOW) == Scope.department("web")


def test_a_service_accounts_reach_expires_with_the_account():
    """Without the bound riding on the entitlement set, an answer computed while the account
    was live could be served from cache after it lapsed."""
    held = EntitlementSet(
        principal_id="u_priya",
        grants=(
            Grant(capability=Capability(value="read:client.name"), scope=Scope.department("web")),
        ),
    )
    reach = reach_for(account(), person(), held, NOW)

    assert isinstance(reach, EntitlementSet)
    assert reach.not_after == NOW + timedelta(days=90)


def test_a_service_account_owned_by_a_partner_holds_nothing_rather_than_an_empty_set():
    """`NoStandingEntitlement` cannot be intersected, hashed or cached. Returning an empty
    set here for convenience would undo that distinction for every partner-owned robot."""
    partner = person("u_partner", Employment.PARTNER, not_after=NOW + timedelta(days=30))
    reach = reach_for(
        account(owner_principal_id="u_partner"),
        partner,
        NoStandingEntitlement(principal_id="u_partner"),
        NOW,
    )

    assert isinstance(reach, NoStandingEntitlement)
    assert reach.principal_id == "brain-sync"


def test_a_service_account_whose_owner_is_not_the_principal_given_is_a_programming_error():
    """Resolving against the wrong owner is the catastrophic failure here: the robot would
    silently borrow somebody else's reach."""
    with pytest.raises(IdentityError, match="is owned by"):
        reach_for(account(), person("u_someone_else"), EntitlementSet(principal_id="x"), NOW)
